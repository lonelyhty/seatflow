"""Real Chromium sessions against one localhost origin, using synthetic accounts only."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

from seat_reserver.batch import BatchRunner
from seat_reserver.batch_model import BatchSettings, Profile
from seat_reserver.model import Settings, now_china
from seat_reserver.runner import ReservationRunner
from seat_reserver.test_app import FixtureHandler, FixtureServer


class MultiHandler(FixtureHandler):
    def account(self):
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        value = cookie.get("fixture-auth")
        return value.value.removeprefix("ok-") if value else ""

    def use_account_view(self):
        root = self.server
        account = self.account()
        if account not in root.accounts:
            return False
        self.server = SimpleNamespace(**{**root.__dict__, **root.accounts[account]})
        return True

    def do_GET(self):
        if not self.use_account_view():
            return self.login_form()
        super().do_GET()

    def do_POST(self):
        if self.path == "/login":
            body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
            values = {k: v[0] for k, v in parse_qs(body).items()}
            account = values.get("account", "")
            with self.server.guard:
                self.server.logins.append(account)
            if account in self.server.bad_accounts or values.get("password") != "password-" + account or account not in self.server.accounts:
                return self.login_form("账号或密码错误")
            return self.redirect("/front/third/apps/seat/index?fidEnc=fixture", cookie=f"fixture-auth=ok-{account}; Path=/; HttpOnly; SameSite=Lax")
        if not self.use_account_view():
            return self.login_form()
        super().do_POST()


class MultiServer(FixtureServer):
    def __init__(self, count):
        super().__init__()
        self.RequestHandlerClass = MultiHandler
        self.accounts = {f"testuser{i}": {"records": [], "submissions": []} for i in range(count)}
        self.logins = []
        self.bad_accounts = set()
        self.guard = threading.Lock()
        self.maps["3F"] = tuple(str(101 + i) for i in range(count))
        self.blocked = set()


class BatchBrowserTests(unittest.TestCase):
    def config(self, count, release=None):
        return BatchSettings([
            Profile(name=f"测试 {i + 1}", settings=Settings(account=f"testuser{i}", password=f"password-testuser{i}",
                    seats=str(101 + i), start_time="08:00", end_time="08:30")) for i in range(count)
        ], headless=True, max_parallel=max(20, count), release_time=release or "00:00")

    def run_batch(self, site, config, directory, *, mode="reserve", events=None):
        def factory(emit, stop, **kwargs):
            return ReservationRunner(emit, stop, entry_url=site.entry, test_site=True,
                                     verify_delays=(0.05, 0.1, 0.2), **kwargs)
        runner = BatchRunner(events.append if events is not None else lambda _: None, runner_factory=factory, log_root=Path(directory))
        try:
            runner.start(config, mode=mode)
            self.assertTrue(runner.wait(100), "Local browser batch exceeded 100 seconds")
        finally:
            runner.stop()
            runner.join()
        return runner

    def test_isolated_accounts_release_together_and_each_submit_once(self):
        count = int(os.environ.get("SEAT_BATCH_BROWSER_COUNT", "3"))
        site = MultiServer(count)
        events = []
        trigger = now_china() + timedelta(seconds=30 if count > 5 else 12)
        config = self.config(count, trigger.strftime("%H:%M:%S"))
        actual_trigger = config.jobs("reserve")[0][1].trigger.timestamp()
        try:
            with tempfile.TemporaryDirectory() as directory:
                runner = self.run_batch(site, config, directory, events=events)
                states = runner.snapshot()
                self.assertEqual([s["code"] for s in states], ["SUCCESS"] * count,
                                 [(s["phase"], s["text"]) for s in states])
                for i in range(count):
                    submissions = site.accounts[f"testuser{i}"]["submissions"]
                    self.assertEqual(len(submissions), 1)
                    self.assertEqual(submissions[0]["seat"], str(101 + i))
                    self.assertGreaterEqual(submissions[0]["timestamp"], actual_trigger)
                self.assertEqual(len([e for e in events if e["kind"] == "ready"]), count)
                delays = [s["delay_ms"] for s in states]
                self.assertLess(max(delays), 1800, delays)
                self.assertLess(max(delays) - min(delays), 1200, delays)
                self.assertEqual(len(site.logins), count)
                self.assertTrue(all(s["closed"] for s in states))
                print(f"\nLOCAL_BATCH count={count} results=SUCCESS:{count} release_delay_ms={delays}", flush=True)
                report_copy = os.environ.get("SEAT_BATCH_TEST_REPORT")
                if report_copy:
                    Path(report_copy).write_text(runner.report_path.read_text(encoding="utf-8"), encoding="utf-8")
        finally:
            site.close()

    def test_bad_login_does_not_block_other_accounts(self):
        site = MultiServer(3)
        site.bad_accounts.add("testuser1")
        try:
            with tempfile.TemporaryDirectory() as directory:
                runner = self.run_batch(site, self.config(3), directory)
                self.assertEqual([s["code"] for s in runner.snapshot()], ["SUCCESS", "ERROR", "SUCCESS"])
                self.assertEqual(site.accounts["testuser1"]["submissions"], [])
                self.assertEqual(len(site.logins), 3)
        finally:
            site.close()

    def test_bulk_login_never_submits_or_shares_sessions(self):
        site = MultiServer(3)
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = self.config(3)
                runner = self.run_batch(site, config, directory, mode="login")
                self.assertEqual([s["code"] for s in runner.snapshot()], ["LOGIN_READY"] * 3)
                self.assertCountEqual(site.logins, [p.settings.account for p in config.profiles])
                self.assertTrue(all(not a["submissions"] for a in site.accounts.values()))
        finally:
            site.close()


if __name__ == "__main__":
    unittest.main()
