from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from seat_reserver.account_lock import AccountBusy, AccountLease
from seat_reserver.batch import BatchRunner
from seat_reserver.batch_model import BatchSettings, Profile, load_batch, save_batch
from seat_reserver.model import Settings


def profiles(count=3):
    return [Profile(name=f"客户 {i + 1}", settings=Settings(account=f"fixture-multi-{i}", password=f"secret-multi-{i}", seats="061"))
            for i in range(count)]


class BatchConfigTests(unittest.TestCase):
    def test_credentials_are_encrypted_and_disabled_accounts_roundtrip(self):
        config = BatchSettings(profiles(), max_parallel=12, release_time="22:30:01", headless=True)
        config.profiles[1].enabled = False
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.json"
            save_batch(config, path)
            text = path.read_text(encoding="utf-8")
            for profile in config.profiles:
                self.assertNotIn(profile.settings.account, text)
                self.assertNotIn(profile.settings.password, text)
            loaded = load_batch(path)
            self.assertEqual(loaded, config)
            self.assertEqual(len(loaded.jobs("reserve")), 2)

    def test_duplicates_over_capacity_and_bad_plans_fail_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            factory_calls = []
            runner = BatchRunner(lambda _: None, runner_factory=lambda *a, **k: factory_calls.append(a), log_root=Path(directory))
            config = BatchSettings(profiles(), max_parallel=2)
            with self.assertRaisesRegex(ValueError, "不会排队"):
                runner.start(config)
            config.max_parallel = 20
            config.profiles[1].settings.account = config.profiles[0].settings.account.upper()
            with self.assertRaisesRegex(ValueError, "重复"):
                runner.start(config)
            config.profiles = profiles()
            config.profiles[1].settings.seats = "错误编号"
            with self.assertRaises(ValueError):
                runner.start(config)
            self.assertEqual(factory_calls, [])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_login_test_allows_empty_seats_and_jobs_are_snapshots(self):
        config = BatchSettings(profiles())
        config.profiles[0].settings.seats = ""
        jobs = config.jobs("login")
        config.profiles[0].settings.password = "edited-after-start"
        self.assertIsNone(jobs[0][1])
        self.assertNotEqual(jobs[0][0].settings.password, "edited-after-start")
        with self.assertRaises(ValueError):
            config.jobs("reserve")

    def test_account_lock_blocks_other_process_and_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with AccountLease("lease-fixture", root):
                with self.assertRaises(AccountBusy):
                    with AccountLease("LEASE-FIXTURE", root):
                        pass
                program = (
                    "from pathlib import Path; from seat_reserver.account_lock import AccountLease, AccountBusy\n"
                    "import sys\n"
                    "try:\n"
                    " with AccountLease('lease-fixture', Path(sys.argv[1])): pass\n"
                    "except AccountBusy: sys.exit(23)\n"
                )
                child = subprocess.run([sys.executable, "-c", program, directory], capture_output=True, timeout=10)
                self.assertEqual(child.returncode, 23, child.stderr.decode())
            with AccountLease("lease-fixture", root):
                pass


class BatchWorkerTests(unittest.TestCase):
    def test_workers_are_parallel_and_one_failure_does_not_block_others(self):
        barrier = threading.Barrier(4)
        class Worker:
            def __init__(self, emit, stop, **kwargs):
                self.emit = emit
            def run(self, settings, plan, **kwargs):
                barrier.wait(timeout=4)
                if settings.account.endswith("-1"):
                    raise RuntimeError(settings.account + settings.password)
                self.emit({"kind": "result", "code": "LOGIN_READY", "text": "通过 " + settings.password})
                return "LOGIN_READY"
        with tempfile.TemporaryDirectory() as directory:
            events = []
            runner = BatchRunner(events.append, runner_factory=Worker, log_root=Path(directory))
            config = BatchSettings(profiles(4))
            runner.start(config, mode="login")
            self.assertTrue(runner.wait(8))
            runner.join()
            codes = [s["code"] for s in runner.snapshot()]
            self.assertEqual(codes.count("LOGIN_READY"), 3)
            self.assertEqual(codes.count("ERROR"), 1)
            report = runner.report_path.read_text(encoding="utf-8")
            for profile in config.profiles:
                for secret in (profile.settings.account, profile.settings.password):
                    self.assertNotIn(secret, report)
                    self.assertNotIn(secret, json.dumps(events))
            self.assertTrue(json.loads(report)["finished_at"])

    def test_stopping_one_account_leaves_other_workers_running(self):
        barrier = threading.Barrier(4)
        finish_others = threading.Event()
        class Worker:
            def __init__(self, emit, stop, **kwargs):
                self.emit, self.stop = emit, stop
            def run(self, settings, plan, **kwargs):
                barrier.wait(timeout=4)
                while not finish_others.is_set() and not self.stop.wait(0.02):
                    pass
                code = "STOPPED" if self.stop.is_set() else "LOGIN_READY"
                self.emit({"kind": "result", "code": code, "text": code})
                return code
        with tempfile.TemporaryDirectory() as directory:
            runner = BatchRunner(lambda _: None, runner_factory=Worker, log_root=Path(directory))
            config = BatchSettings(profiles())
            try:
                runner.start(config, mode="login")
                barrier.wait(timeout=4)
                runner.stop(config.profiles[0].id)
                self.assertFalse(runner.wait(0.15))
                self.assertFalse(runner.stops[config.profiles[1].id].is_set())
            finally:
                finish_others.set()
                self.assertTrue(runner.wait(5))
                runner.join()
            self.assertEqual([s["code"] for s in runner.snapshot()], ["STOPPED", "LOGIN_READY", "LOGIN_READY"])

    def test_exception_after_submission_is_uncertain_and_never_restarted(self):
        calls = []
        class Worker:
            submission_started = True
            def __init__(self, *args, **kwargs):
                pass
            def run(self, settings, plan, **kwargs):
                calls.append(settings.account)
                raise RuntimeError("connection failed")
        with tempfile.TemporaryDirectory() as directory:
            runner = BatchRunner(lambda _: None, runner_factory=Worker, log_root=Path(directory))
            runner.start(BatchSettings(profiles(1)))
            self.assertTrue(runner.wait(5))
            runner.join()
            self.assertEqual(runner.snapshot()[0]["code"], "UNKNOWN")
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
