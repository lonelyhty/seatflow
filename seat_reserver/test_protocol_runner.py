"""Browser-free reservation checks; all HTTP stays inside MockTransport.

The fixtures never load settings, password-login, cache sessions or acquire an
account file lock. A virtual clock makes scheduled-send assertions deterministic.
"""
from __future__ import annotations

import copy
import json
import statistics
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs
from unittest.mock import patch

import httpx

from .batch import BatchRunner
from .batch_model import BatchSettings, Profile
from .model import CHINA_TZ, Plan, Settings, now_china
from .protocol_runner import HTTPSubmission, ProtocolRunner, validate_rules
from .protocol_signing import PublicSubmitSigner
from .protocol_session import ProtocolError, VerificationRequired
from .test_protocol_api import plan, responses as query_responses, stamp


SUBMIT_PATH = "/data/apps/seat/submit"
ORIGIN = "https://office.chaoxing.com"


def responses():
    payloads = query_responses()
    payloads["/data/apps/seat/room/info"]["seatConfig"].update(
        timeType=0, timeUnit=30, minReserveDuration=0.0, reserveDuration=0.0,
    )
    return payloads


class VirtualClock:
    def __init__(self):
        self.base = datetime(2026, 10, 12, tzinfo=CHINA_TZ)
        self.elapsed = 0.0

    def now(self):
        return self.base + timedelta(seconds=self.elapsed)

    def counter(self):
        return self.elapsed

    def advance(self, seconds):
        # datetime resolves microseconds. Keep the virtual monotonic clock at
        # the same resolution instead of inventing sub-microsecond early sends.
        self.elapsed = round(self.elapsed + max(seconds, 0.000001), 6)


class VirtualStop:
    def __init__(self, clock):
        self.clock = clock
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        if not self.stopped:
            self.clock.advance(seconds)
        return self.stopped


class FixtureSigner:
    metadata = {"source": "in-memory-test"}

    def __enter__(self):
        self.calls = []
        return self

    def __exit__(self, *_):
        return False

    def sign(self, fields, key):
        if key != "mock-page-key":
            raise AssertionError("Unexpected fixture page key")
        self.calls.append(copy.deepcopy(fields))
        return "mock-only-signature"


class AuthenticatedFixtureRunner(ProtocolRunner):
    def _authenticate(self, client, state):
        if state != {}:
            raise AssertionError("Fixture must not use a saved session")
        client.cookies.set("session", "mock-only-session", domain="office.chaoxing.com")
        self.report["used_cached_session"] = False


class HTTPSubmissionTests(unittest.TestCase):
    def test_one_send_marks_attempt_before_transport_and_never_repeats(self):
        calls = []
        sender = None

        def handle(request):
            self.assertTrue(sender.attempted)
            calls.append(request)
            return httpx.Response(200, json={"success": True})

        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            sender = HTTPSubmission(client)
            request = client.build_request("POST", ORIGIN + SUBMIT_PATH, data={"seatNum": "003"})
            self.assertEqual(sender.send(request).status_code, 200)
            with self.assertRaises(ProtocolError):
                sender.send(request)
        self.assertEqual(len(calls), 1)

    def test_timeout_consumes_single_attempt_and_cannot_resend(self):
        calls = []

        def handle(request):
            calls.append(request)
            raise httpx.ReadTimeout("synthetic timeout", request=request)

        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            sender = HTTPSubmission(client)
            request = client.build_request("POST", ORIGIN + SUBMIT_PATH)
            with self.assertRaises(httpx.ReadTimeout):
                sender.send(request)
            self.assertTrue(sender.attempted)
            with self.assertRaises(ProtocolError):
                sender.send(request)
        self.assertEqual(len(calls), 1)

    def test_wrong_url_or_method_sends_zero_requests(self):
        invalid = [("GET", ORIGIN + SUBMIT_PATH), ("PUT", ORIGIN + SUBMIT_PATH),
                   ("POST", "http://office.chaoxing.com" + SUBMIT_PATH),
                   ("POST", "https://other.invalid" + SUBMIT_PATH),
                   ("POST", "https://office.chaoxing.com:444" + SUBMIT_PATH),
                   ("POST", "https://user@office.chaoxing.com" + SUBMIT_PATH),
                   ("POST", ORIGIN + "/data/apps/seat/cancel"),
                   ("POST", ORIGIN + SUBMIT_PATH + "/"),
                   ("POST", ORIGIN + SUBMIT_PATH + "?token=synthetic"),
                   ("POST", ORIGIN + SUBMIT_PATH + "#synthetic")]
        calls = []
        with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
            for method, url in invalid:
                with self.subTest(method=method, url=url):
                    sender = HTTPSubmission(client)
                    with self.assertRaises(ProtocolError):
                        sender.send(client.build_request(method, url))
                    self.assertFalse(sender.attempted)
        self.assertEqual(calls, [])

    def test_submission_redirect_is_not_followed(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(307, headers={"location": "https://other.invalid/submit"})

        with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
            sender = HTTPSubmission(client)
            self.assertEqual(sender.send(client.build_request("POST", ORIGIN + SUBMIT_PATH)).status_code, 307)
        self.assertEqual(len(calls), 1)


class ProtocolRunnerTests(unittest.TestCase):
    def setUp(self):
        self.clock = VirtualClock()
        self.stop = VirtualStop(self.clock)
        self.payloads = responses()
        self.payloads["/data/apps/seat/reservelist"]["reserveList"] = []
        self.trace = []
        self.events = []
        self.submit_mode = "success"
        self.records_queries = 0
        self.preflight_record = None
        self.request_latency = 0.002
        self.settings = Settings(account="mock-account", password="mock-password", floor="2F",
                                 seats="1,2,3", date_mode="specific", booking_date="2026-10-12",
                                 start_time="20:00", end_time="21:00")
        for target in ("seat_reserver.protocol_runner.load_session",
                       "seat_reserver.protocol_runner.save_session",
                       "seat_reserver.runner.sync_playwright"):
            self.addCleanup(patch.stopall)
            patch(target, side_effect=AssertionError("Forbidden fixture external state")).start()
        patch("seat_reserver.protocol_runner.now_china", self.clock.now).start()
        patch("seat_reserver.protocol_runner.time.perf_counter", self.clock.counter).start()
        patch("seat_reserver.protocol_runner.time.sleep", self.clock.advance).start()

    @staticmethod
    def record(seat="003", status=0, floor="2F", start=20, end=21):
        return {"id": 9, "today": "2026-10-12", "firstLevelName": plan().campus,
                "secondLevelName": floor, "thirdLevelName": "", "seatNum": seat,
                "status": status, "type": -1, "startTime": stamp(start), "endTime": stamp(end)}

    def handle(self, request):
        self.assertEqual(request.url.host, "office.chaoxing.com")
        self.trace.append(("request", request.method, request.url.path, self.clock.elapsed, request))
        self.clock.advance(self.request_latency)
        path = request.url.path
        if path == SUBMIT_PATH:
            if self.submit_mode in {"success", "timeout_committed", "rejected_committed"}:
                self.payloads["/data/apps/seat/reservelist"]["reserveList"] = [self.record()]
            elif self.submit_mode == "wrong_record":
                self.payloads["/data/apps/seat/reservelist"]["reserveList"] = [self.record(seat="002")]
            if self.submit_mode.startswith("timeout"):
                raise httpx.ReadTimeout("mock transport private text", request=request)
            if self.submit_mode == "redirect":
                return httpx.Response(307, headers={"location": "https://other.invalid/submit"})
            if self.submit_mode.startswith("rejected"):
                return httpx.Response(200, json={"success": False, "msg": "mock private response"})
            return httpx.Response(200, json={"success": True})
        if path == "/front/third/apps/seat/select":
            return httpx.Response(200, text='<html><input id="submit_enc" value="mock-page-key"></html>')
        if path == "/data/apps/seat/reservelist":
            self.records_queries += 1
            if self.records_queries == 2 and self.preflight_record is not None:
                self.payloads[path]["reserveList"] = [self.preflight_record]
        if path not in self.payloads:
            raise AssertionError(f"Unexpected mock HTTP path: {path}")
        return httpx.Response(200, json={"success": True, "data": copy.deepcopy(self.payloads[path])})

    def emit(self, event):
        self.events.append(event)
        self.trace.append(("event", event["kind"], event, self.clock.elapsed))

    def run_fixture(self, target=None, *, preview=False, submit_enabled=True):
        self.runner = AuthenticatedFixtureRunner(
            self.emit, self.stop, session_state={}, submit_enabled=submit_enabled,
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(self.handle), follow_redirects=True),
            signer_factory=FixtureSigner, keep_open=False,
        )
        # Bypass only the unrelated filesystem account lease; _run is the entire
        # protocol flow. Authentication is mocked above instead of reading cache.
        result = self.runner._run(self.settings, target or plan(), preview=preview)
        return result

    def submissions(self):
        return [entry for entry in self.trace if entry[0] == "request" and entry[2] == SUBMIT_PATH]

    def assert_zero_submissions(self, code):
        self.assertEqual(self.run_fixture(), code, self.events)
        self.assertEqual(self.submissions(), [])
        self.assertEqual(self.runner.report["submission_attempts"], 0)

    def test_complete_query_prepare_submit_record_verification(self):
        self.assertEqual(self.run_fixture(), "SUCCESS", self.events)
        self.assertEqual(len(self.submissions()), 1)
        self.assertTrue(self.runner.report["record_verified"])
        self.assertEqual(self.runner.report["submission_attempts"], 1)
        self.assertFalse(self.runner.report["browser_launched"])
        self.assertEqual(self.runner.report["seat"], "003")
        request = self.submissions()[0][4]
        fields = parse_qs(request.content.decode(), keep_blank_values=True)
        self.assertEqual(fields, {"deptIdEnc": ["214d62ddb0e920e7"], "roomId": ["12"],
                                 "day": ["2026-10-12"], "startTime": ["20:00"], "endTime": ["21:00"],
                                 "seatNum": ["003"], "captcha": [""], "wyToken": [""],
                                 "enc": ["mock-only-signature"]})
        self.assertEqual(request.headers["x-requested-with"], "XMLHttpRequest")
        self.assertIn("session=mock-only-session", request.headers["cookie"])
        self.assertEqual(self.records_queries, 3)
        serialized = json.dumps(self.runner.report) + json.dumps(self.events)
        for secret in ("mock-password", "mock-account", "mock-only-session", "mock-page-key", "mock-only-signature"):
            self.assertNotIn(secret, serialized)

    def test_existing_same_day_active_in_other_floor_blocks_submission(self):
        self.payloads["/data/apps/seat/reservelist"]["reserveList"] = [self.record(seat="017", floor="1F", end=22)]
        self.assert_zero_submissions("EXISTING")
        self.assertEqual(len([entry for entry in self.trace if entry[0] == "request"]), 1)

    def test_cancelled_record_allows_one_submission(self):
        self.payloads["/data/apps/seat/reservelist"]["reserveList"] = [self.record(status=7)]
        self.assertEqual(self.run_fixture(), "SUCCESS", self.events)
        self.assertEqual(len(self.submissions()), 1)

    def test_existing_record_appearing_at_preflight_blocks_submission(self):
        self.preflight_record = self.record()
        self.assert_zero_submissions("EXISTING")
        self.assertEqual(self.records_queries, 2)

    def test_preview_and_submit_disabled_prepare_without_post(self):
        for options in ({"preview": True}, {"submit_enabled": False}):
            with self.subTest(options=options):
                self.trace.clear()
                self.assertEqual(self.run_fixture(**options), "PREVIEW_READY", self.events)
                self.assertTrue(self.runner.report["prepared"])
                self.assertEqual(self.submissions(), [])
                self.assertEqual(self.runner.report["submission_attempts"], 0)

    def test_timeout_unconfirmed_success_and_rejection_never_resubmit(self):
        for mode, expected in (("timeout", "UNKNOWN"), ("unknown", "UNKNOWN"),
                               ("wrong_record", "UNKNOWN"), ("redirect", "UNKNOWN"),
                               ("rejected", "REJECTED")):
            with self.subTest(mode=mode):
                self.payloads = responses()
                self.payloads["/data/apps/seat/reservelist"]["reserveList"] = []
                self.trace.clear()
                self.submit_mode = mode
                self.assertEqual(self.run_fixture(), expected, self.events)
                self.assertEqual(len(self.submissions()), 1)
                self.assertEqual(self.runner.report["submission_attempts"], 1)
                self.assertFalse(self.runner.report["record_verified"])
                submit_index = next(index for index, entry in enumerate(self.trace)
                                    if entry[0] == "request" and entry[2] == SUBMIT_PATH)
                after = [entry[2] for entry in self.trace[submit_index + 1:] if entry[0] == "request"]
                self.assertEqual(after, ["/data/apps/seat/reservelist"] * 5)

    def test_record_proof_wins_over_timeout_or_error_response(self):
        for mode in ("timeout_committed", "rejected_committed"):
            with self.subTest(mode=mode):
                self.payloads = responses()
                self.payloads["/data/apps/seat/reservelist"]["reserveList"] = []
                self.trace.clear()
                self.submit_mode = mode
                self.assertEqual(self.run_fixture(), "SUCCESS", self.events)
                self.assertTrue(self.runner.report["record_verified"])
                self.assertEqual(len(self.submissions()), 1)

    def test_security_verify_special_mode_and_risk_block_submission(self):
        for config, risk in (({"securityVerify": 1, "reserveMode": 0}, False),
                             ({"securityVerify": 0, "reserveMode": 1}, False),
                             ({"reserveMode": 0}, False),
                             ({"securityVerify": 0, "reserveMode": 0}, True)):
            with self.subTest(config=config, risk=risk):
                self.payloads = responses()
                self.payloads["/data/apps/seat/reservelist"]["reserveList"] = []
                self.payloads["/data/apps/seat/room/info"]["seatConfig"].update(config)
                if "securityVerify" not in config:
                    self.payloads["/data/apps/seat/room/info"]["seatConfig"].pop("securityVerify")
                self.payloads["/data/apps/seat/risk/check/config"]["riskCheckOpen"] = risk
                self.trace.clear()
                self.assert_zero_submissions("MANUAL_REQUIRED")

    def test_risk_window_checked_for_release_time_not_preparation(self):
        target = replace(plan(), trigger=self.clock.now() + timedelta(seconds=20))
        self.payloads["/data/apps/seat/room/info"]["beforeOpenTimeStamp"] = int(target.trigger.timestamp() * 1000)
        self.payloads["/data/apps/seat/risk/check/config"]["riskCheckOpen"] = True
        self.assertEqual(self.run_fixture(target), "MANUAL_REQUIRED", self.events)
        self.assertEqual(self.submissions(), [])

    def test_scheduled_send_never_early_and_no_query_between_ready_and_post(self):
        target = replace(plan(), trigger=self.clock.now() + timedelta(seconds=20))
        self.assertEqual(self.run_fixture(target), "SUCCESS", self.events)
        submission = self.submissions()[0]
        self.assertGreaterEqual(submission[3], 20)
        self.assertLess(submission[3], 20.01)
        submit_index = self.trace.index(submission)
        final_ready = max(index for index, entry in enumerate(self.trace[:submit_index])
                          if entry[0] == "event" and entry[1] == "ready")
        self.assertEqual([entry for entry in self.trace[final_ready + 1:submit_index] if entry[0] == "request"], [])
        self.assertIsNotNone(self.runner.report["client_release_delay_ms"])
        self.assertLess(self.runner.report["client_release_delay_ms"], 10)

    def test_room_open_timestamp_defers_send_beyond_plan_trigger(self):
        target = replace(plan(), trigger=self.clock.now() + timedelta(seconds=10))
        self.payloads["/data/apps/seat/room/info"]["beforeOpenTimeStamp"] = int(
            (self.clock.now() + timedelta(seconds=20)).timestamp() * 1000)
        self.assertEqual(self.run_fixture(target), "SUCCESS", self.events)
        self.assertGreaterEqual(self.submissions()[0][3], 20)

    def test_stop_before_start_produces_no_http_or_submission(self):
        self.stop.set()
        self.assert_zero_submissions("STOPPED")
        self.assertEqual([entry for entry in self.trace if entry[0] == "request"], [])

    def test_stop_on_final_ready_produces_no_submission(self):
        original_emit = self.emit

        def stopping_emit(event):
            original_emit(event)
            if event["kind"] == "ready" and "一次发送" in event["text"]:
                self.stop.set()

        self.emit = stopping_emit
        self.assert_zero_submissions("STOPPED")

    def test_strict_security_and_reserve_mode_types_block_submission(self):
        for key in ("securityVerify", "reserveMode"):
            for value in (False, 0.0, "0", None):
                with self.subTest(key=key, value=value):
                    self.payloads = responses()
                    self.payloads["/data/apps/seat/reservelist"]["reserveList"] = []
                    self.payloads["/data/apps/seat/room/info"]["seatConfig"][key] = value
                    self.trace.clear()
                    self.assert_zero_submissions("MANUAL_REQUIRED")

    def test_time_type_and_unit_unknown_values_block_submission(self):
        for key, values in (("timeType", (False, 0.0, 1, "0", None)),
                            ("timeUnit", (30.0, 60, "30", None))):
            for value in values:
                with self.subTest(key=key, value=value):
                    self.payloads = responses()
                    self.payloads["/data/apps/seat/reservelist"]["reserveList"] = []
                    self.payloads["/data/apps/seat/room/info"]["seatConfig"][key] = value
                    self.trace.clear()
                    self.assert_zero_submissions("MANUAL_REQUIRED")

    def test_duration_unknown_or_outside_bounds_blocks_submission(self):
        cases = [(key, value) for key in ("minReserveDuration", "reserveDuration")
                 for value in (False, -1, "0", None)]
        cases.extend((("minReserveDuration", 2.0), ("reserveDuration", 0.5)))
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.payloads = responses()
                self.payloads["/data/apps/seat/reservelist"]["reserveList"] = []
                self.payloads["/data/apps/seat/room/info"]["seatConfig"][key] = value
                self.trace.clear()
                self.assert_zero_submissions("ERROR")

    def test_duration_nonfinite_values_fail_closed_before_transport(self):
        for key in ("minReserveDuration", "reserveDuration"):
            for value in (float("nan"), float("inf"), -float("inf")):
                with self.subTest(key=key, value=value):
                    config = responses()["/data/apps/seat/room/info"]["seatConfig"]
                    config[key] = value
                    info = SimpleNamespace(config=config, common_pause_times=(), seat_interval_map={})
                    with self.assertRaises(ProtocolError):
                        validate_rules(info, plan())
        self.assertEqual(self.trace, [])

    def test_duration_equal_to_minimum_and_maximum_allows_submission(self):
        self.payloads["/data/apps/seat/room/info"]["seatConfig"].update(
            minReserveDuration=1.0, reserveDuration=1,
        )
        self.assertEqual(self.run_fixture(), "SUCCESS", self.events)
        self.assertEqual(len(self.submissions()), 1)

    def test_special_pause_and_seat_intervals_require_normal_flow(self):
        for key, value in (("commonPauseTimes", [[14_400_000, 18_000_000]]),
                           ("seatIntervalMap", {"1": [{"startTime": "20:00", "endTime": "21:00"}]})):
            with self.subTest(key=key):
                self.payloads = responses()
                self.payloads["/data/apps/seat/reservelist"]["reserveList"] = []
                self.payloads["/data/apps/seat/room/info"][key] = value
                self.trace.clear()
                self.assert_zero_submissions("MANUAL_REQUIRED")

    def test_same_runner_instance_cannot_run_again_after_submission(self):
        self.assertEqual(self.run_fixture(), "SUCCESS", self.events)
        requests_before = len([entry for entry in self.trace if entry[0] == "request"])
        self.assertEqual(self.runner._run(self.settings, plan()), "ERROR")
        self.assertEqual(len([entry for entry in self.trace if entry[0] == "request"]), requests_before)
        self.assertEqual(len(self.submissions()), 1)
        self.assertEqual(self.runner.report["submission_attempts"], 1)

    def test_late_preflight_reports_actual_release_delay(self):
        target = replace(plan(), trigger=self.clock.now() + timedelta(seconds=20))
        original_emit = self.emit

        def delayed_preflight(event):
            original_emit(event)
            if event["kind"] == "phase" and "提交前协议检查" in event["text"]:
                self.request_latency = 3.0

        self.emit = delayed_preflight
        self.assertEqual(self.run_fixture(target), "SUCCESS", self.events)
        submitted_at = self.submissions()[0][3]
        self.assertGreater(submitted_at, 20)
        self.assertAlmostEqual(self.runner.report["client_release_delay_ms"], (submitted_at - 20) * 1000, places=2)
        self.assertGreater(self.runner.report["client_release_delay_ms"], 1000)

    def test_keyboard_interrupt_during_submit_is_unknown_without_resend(self):
        original_handler = self.handle

        def interrupted(request):
            response = original_handler(request)
            if request.url.path == SUBMIT_PATH:
                raise KeyboardInterrupt()
            return response

        self.handle = interrupted
        self.assertEqual(self.run_fixture(), "UNKNOWN", self.events)
        self.assertEqual(len(self.submissions()), 1)
        self.assertEqual(self.runner.report["submission_attempts"], 1)
        self.assertTrue(self.stop.is_set())

    def test_release_callback_cost_cannot_delay_the_actual_send(self):
        target = replace(plan(), trigger=self.clock.now() + timedelta(seconds=20))
        original_emit = self.emit

        def slow_release_consumer(event):
            original_emit(event)
            if event["kind"] == "release":
                self.clock.advance(1.0)

        self.emit = slow_release_consumer
        self.assertEqual(self.run_fixture(target), "SUCCESS", self.events)
        submitted_at = self.submissions()[0][3]
        self.assertLess(submitted_at, 20.01)
        self.assertLess(self.runner.report["client_release_delay_ms"], 10)
        release = next(event for event in self.events if event["kind"] == "release")
        self.assertEqual(release["actual_time"], release["triggered_at"])
        self.assertEqual(datetime.fromisoformat(release["triggered_at"]), self.clock.base + timedelta(seconds=submitted_at))
        submit_index = self.trace.index(self.submissions()[0])
        release_index = next(index for index, entry in enumerate(self.trace)
                             if entry[0] == "event" and entry[1] == "release")
        self.assertGreater(release_index, submit_index)


class MockAccountRunner(AuthenticatedFixtureRunner):
    """One private in-memory HTTP service and cookie jar per dummy account."""
    def __init__(self, emit, stop, **kwargs):
        self.requests = []
        self.saved_records = []
        self.target = None
        self.submitted_fields = []
        super().__init__(emit, stop, session_state={}, signer_factory=FixtureSigner,
                         client_factory=lambda: httpx.Client(transport=httpx.MockTransport(self.handle)), **kwargs)

    def run(self, settings, target, **kwargs):
        # No account lease files or actual settings are touched by this fixture.
        self.target = target
        self.settings = settings
        return self._run(settings, target, **kwargs)

    def _authenticate(self, client, state):
        if state != {}:
            raise AssertionError("Fixture session cache must stay disabled")
        client.cookies.set("session", "mock-session-" + self.settings.account, domain="office.chaoxing.com")

    def handle(self, request):
        if request.url.host != "office.chaoxing.com":
            raise AssertionError("Unexpected mock fixture host")
        expected_cookie = "session=mock-session-" + self.settings.account
        if request.headers.get("cookie") != expected_cookie:
            raise AssertionError("Account cookie jar mixed with another worker")
        self.requests.append((request.method, request.url.path, request, time.perf_counter()))
        path = request.url.path
        if path == SUBMIT_PATH:
            fields = parse_qs(request.content.decode(), keep_blank_values=True)
            if fields["day"] != [str(self.target.day)] or fields["seatNum"] != [f"{int(self.settings.seats):03d}"]:
                raise AssertionError("Mock request plan belongs to a different account")
            self.submitted_fields.append(fields)
            self.saved_records.append({
                "id": 9, "today": str(self.target.day), "firstLevelName": self.target.campus,
                "secondLevelName": self.target.floor, "thirdLevelName": "", "seatNum": fields["seatNum"][0],
                "status": 0, "type": -1,
                "startTime": int(datetime.combine(self.target.day, self.target.start, CHINA_TZ).timestamp() * 1000),
                "endTime": int(datetime.combine(self.target.day, self.target.end, CHINA_TZ).timestamp() * 1000),
            })
            return httpx.Response(200, json={"success": True})
        if path == "/front/third/apps/seat/select":
            return httpx.Response(200, text='<input id="submit_enc" value="mock-page-key">')
        payloads = responses()
        for room_data in (payloads["/data/apps/seat/room/list"]["seatRoomList"][0],
                          payloads["/data/apps/seat/room/info"]["seatRoom"]):
            room_data["capacity"] = 12
        payloads["/data/apps/seat/room/info"]["seatAttributes"] = []
        payloads["/data/apps/seat/seatgrid/roomid"]["seatDatas"] = [
            {"roomId": 12, "seatNum": f"{number:03d}", "reserveStatus": 0} for number in range(1, 13)
        ]
        payloads["/data/apps/seat/getusedseatnums"]["seatReserves"] = []
        payloads["/data/apps/seat/reservelist"]["reserveList"] = copy.deepcopy(self.saved_records)
        if path not in payloads:
            raise AssertionError("Unexpected mock request path")
        return httpx.Response(200, json={"success": True, "data": payloads[path]})


class ProtocolBatchIntegrationTests(unittest.TestCase):
    def test_twelve_workers_keep_sessions_records_and_release_events_independent(self):
        profiles = [Profile(name=f"模拟 {index}", settings=Settings(
            account=f"mock-concurrent-{index:02d}", password=f"mock-secret-{index:02d}", floor="2F",
            seats=str(index), date_mode="tomorrow", start_time="20:00", end_time="21:00",
        )) for index in range(1, 13)]
        config = BatchSettings(profiles, max_parallel=12, release_time="00:00", headless=True)
        workers = []
        events = []

        def factory(*args, **kwargs):
            worker = MockAccountRunner(*args, **kwargs)
            workers.append(worker)
            return worker

        with tempfile.TemporaryDirectory() as directory, patch(
                "seat_reserver.protocol_runner.load_session", side_effect=AssertionError("No real cache")), patch(
                "seat_reserver.protocol_runner.save_session", side_effect=AssertionError("No real cache")), patch(
                "seat_reserver.runner.sync_playwright", side_effect=AssertionError("No browser")):
            batch = BatchRunner(events.append, runner_factory=factory, log_root=Path(directory))
            try:
                batch.start(config)
                self.assertTrue(batch.wait(10), "Dummy protocol workers did not finish")
            finally:
                batch.stop()
                batch.join()
            states = batch.snapshot()
            self.assertEqual(len(states), 12)
            self.assertEqual([state["code"] for state in states], ["SUCCESS"] * 12, states)
            self.assertTrue(all(state["triggered_at"] for state in states))
            self.assertEqual(len([event for event in events if event["kind"] == "release"]), 12)
            self.assertEqual(len(workers), 12)
            self.assertEqual(len({id(worker) for worker in workers}), 12)
            self.assertEqual(len({id(worker.saved_records) for worker in workers}), 12)
            for worker in workers:
                self.assertEqual(len(worker.submitted_fields), 1)
                self.assertEqual(worker.report["submission_attempts"], 1)
                self.assertTrue(worker.report["record_verified"])
                self.assertEqual(worker.saved_records[0]["seatNum"], f"{int(worker.settings.seats):03d}")
                self.assertEqual(len([request for request in worker.requests if request[1] == SUBMIT_PATH]), 1)
            report = batch.report_path.read_text(encoding="utf-8")
            self.assertEqual(len(json.loads(report)["jobs"]), 12)
            for profile in profiles:
                self.assertNotIn(profile.settings.account, report)
                self.assertNotIn(profile.settings.password, report)


def create_local_benchmark(output):
    """Explicit helper: public JS with dummy data and a local scheduled POST."""
    signing_times = []
    public_parameters = {"deptIdEnc": "dummy-department", "roomId": "1234", "day": "2026-10-12",
                         "startTime": "20:00", "endTime": "21:00", "seatNum": "0003",
                         "captcha": "", "wyToken": ""}
    startup_started = time.perf_counter()
    with PublicSubmitSigner() as signer:
        startup_ms = (time.perf_counter() - startup_started) * 1000
        source_metadata = dict(signer.metadata)
        for _ in range(100):
            started = time.perf_counter()
            signer.sign(public_parameters, "dummy-page-key")
            signing_times.append((time.perf_counter() - started) * 1000)
    settings = Settings(account="mock-benchmark", password="mock-benchmark-password", floor="2F",
                        seats="3", start_time="20:00", end_time="21:00")
    current = now_china()
    target = Plan((current + timedelta(days=1)).date(), plan().start, plan().end,
                  current + timedelta(milliseconds=150), plan().campus, "2F", ("3",))
    events = []
    with patch("seat_reserver.protocol_runner.load_session", side_effect=AssertionError("No real cache")), patch(
            "seat_reserver.protocol_runner.save_session", side_effect=AssertionError("No real cache")), patch(
            "seat_reserver.runner.sync_playwright", side_effect=AssertionError("No browser")):
        runner = MockAccountRunner(events.append, threading.Event(), keep_open=False)
        code = runner.run(settings, target)
    posts = [request for request in runner.requests if request[1] == SUBMIT_PATH]
    if code != "SUCCESS" or len(posts) != 1 or not runner.report.get("record_verified"):
        raise AssertionError("Local benchmark failed its single-submit contract")
    ordered = sorted(signing_times)
    report = {
        "kind": "local-only synthetic benchmark",
        "network": "httpx.MockTransport; no socket or school request",
        "signature": "PublicSubmitSigner with locally stored public JS and dummy fields; no school request",
        "public_signing_dummy_parameters": {"rounds": 100, "worker_startup_ms": round(startup_ms, 6),
                          "mean_ms": round(statistics.mean(signing_times), 6),
                          "median_ms": round(statistics.median(signing_times), 6),
                          "p95_ms": round(ordered[94], 6), "public_asset_sha256": source_metadata},
        "scheduled_mock_submission": {"scheduled_after_ms": 150, "uses_real_timer": True,
                                      "final_wait_method": "time.sleep (last 0.5 seconds)",
                                      "code": code, "submission_attempts": 1,
                                      "client_release_delay_ms": runner.report["client_release_delay_ms"],
                                      "submit_round_trip_ms": runner.report["timings_ms"]["submit_round_trip"],
                                      "record_verified": True},
        "timer_sample_comparison": {"prior_wait_method": "threading.Event.wait",
                                    "prior_single_sample_delay_ms": 9.824,
                                    "current_wait_method": "time.sleep (last 0.5 seconds)",
                                    "current_single_sample_delay_ms": runner.report["client_release_delay_ms"],
                                    "single_samples_only": True,
                                    "does_not_measure_network_speed": True},
        "real_network_speed_comparison_performed": False,
        "real_booking_created": False,
        "credentials_exported": False,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    unittest.main(verbosity=2)
