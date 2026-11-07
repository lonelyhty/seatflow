"""Read-only API adapter checks using an in-memory HTTP transport only."""
from __future__ import annotations

import copy
import unittest
from datetime import date, datetime, time

import httpx

from .model import CAMPUS, CHINA_TZ, Plan
from .protocol_api import ProtocolAPI, ProtocolQueryError, Room, SeatAvailability


def stamp(hour, minute=0):
    return int(datetime(2026, 10, 12, hour, minute, tzinfo=CHINA_TZ).timestamp() * 1000)


def clock_stamp(hour, minute=0):
    return ((hour - 8) * 60 + minute) * 60_000


def plan():
    return Plan(date(2026, 10, 12), time(20), time(21),
                datetime(2026, 10, 11, 22, 30, tzinfo=CHINA_TZ), CAMPUS, "2F", ("1", "2", "3"))


def room_payload():
    return {"id": 12, "deptId": 7, "firstLevelName": CAMPUS,
            "secondLevelName": "2F", "thirdLevelName": "", "capacity": 3,
            "startSeatNum": 1, "picSeatMode": 2, "isOpen": 0,
            "startTime": 0, "endTime": 50_400_000}


def responses():
    return {
        "/data/apps/seat/room/list": {"totalPage": 1, "totalRow": 1, "seatRoomList": [room_payload()]},
        "/data/apps/seat/room/info": {"seatRoom": room_payload(),
            "seatAttributes": [{"seatNum": "001", "roomId": 12, "isReserve": 0}],
            "seatConfig": {"securityVerify": 0, "reserveMode": 0, "timeType": 0,
                           "timeUnit": 30, "minReserveDuration": 0.5, "reserveDuration": 4},
            "beforeOpenTimeStamp": 0,
            "commonPauseTimes": [], "seatIntervalMap": {}},
        "/data/apps/seat/seatgrid/roomid": {"seatDatas": [
            {"roomId": 12, "seatNum": label, "reserveStatus": 0} for label in ("001", "002", "003")]},
        "/data/apps/seat/getusedseatnums": {"seatReserves": [
            {"roomId": 12, "seatNum": "002", "today": "2026-10-12", "status": 0,
             "startTime": stamp(20), "endTime": stamp(22)}]},
        "/data/apps/seat/config": {"seatConfig": {"securityVerify": 0}},
        "/data/apps/seat/risk/check/config": {"riskCheckOpen": False, "riskCheckType": 0,
            "riskCheckBeforeMilliSecond": 3000, "riskCheckAfterMilliSecond": 5000},
        "/data/apps/seat/reservelist": {"exitNextPage": False, "reserveList": [
            {"id": 9, "today": "2026-10-12", "firstLevelName": CAMPUS, "secondLevelName": "2F",
             "thirdLevelName": "", "seatNum": "003", "status": 101, "type": -1,
             "startTime": stamp(20), "endTime": stamp(21)}]},
    }


class ProtocolAPITests(unittest.TestCase):
    def setUp(self):
        self.payloads = responses()
        self.requests = []
        def handler(request):
            self.requests.append(request)
            payload = copy.deepcopy(self.payloads[request.url.path])
            return httpx.Response(200, json={"success": True, "data": payload})
        self.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
        self.client.cookies.set("session", "local-test-session", domain="office.chaoxing.com")
        # Code 101 is deliberately synthetic; it is not a claimed school mapping.
        self.api = ProtocolAPI(self.client, "local-department", record_status_names={101: "待履约"})
        self.addCleanup(self.client.close)

    def _prepared(self):
        room = self.api.select_room(plan())
        info = self.api.room_info(plan(), room)
        return room, info

    def test_queries_share_session_keep_labels_and_resolve_candidate_order(self):
        room, info = self._prepared()
        seats = self.api.seat_map(plan(), room, info)
        self.assertEqual(list(seats), ["001", "002", "003"])
        self.assertFalse(seats["001"].available)
        self.assertFalse(seats["002"].available)
        self.assertTrue(seats["003"].available)
        self.assertEqual(self.api.resolve_candidate(seats, plan().seats), "003")
        self.assertTrue(all("session=local-test-session" in request.headers["cookie"] for request in self.requests))
        self.assertEqual([(request.method, request.url.path) for request in self.requests], [
            ("GET", "/data/apps/seat/room/list"), ("POST", "/data/apps/seat/room/info"),
            ("GET", "/data/apps/seat/seatgrid/roomid"), ("POST", "/data/apps/seat/getusedseatnums")])
        used_body = self.requests[-1].content.decode()
        self.assertIn("startTime=20%3A00", used_body)
        self.assertIn("endTime=21%3A00", used_body)
        self.assertIn("day=2026-10-12", used_body)
        self.assertEqual(self.requests[0].url.params["time"], "")

    def test_no_mutation_endpoint_or_changed_method_is_eligible(self):
        for method, path in (("POST", "/data/apps/seat/submit"), ("POST", "/data/apps/seat/cancel"),
                             ("GET", "/data/apps/seat/signin"), ("POST", "/data/apps/seat/config"),
                             ("GET", "/data/apps/seat/room/info")):
            with self.subTest(method=method, path=path), self.assertRaises(ValueError):
                self.api.request(method, path, {})
        self.assertEqual(self.requests, [])

    def test_config_is_get_and_risk_config_is_read_only_post(self):
        self.assertEqual(self.api.config(), {"securityVerify": 0})
        self.assertFalse(self.api.risk_config(7).enabled)
        self.assertEqual([request.method for request in self.requests], ["GET", "POST"])
        self.assertEqual(self.requests[-1].content, b"appId=7&appType=0")

    def test_cross_campus_duplicate_floor_and_wrong_room_stop(self):
        self.payloads["/data/apps/seat/room/list"]["seatRoomList"][0]["firstLevelName"] = "other"
        with self.assertRaises(ProtocolQueryError):
            self.api.select_room(plan())
        self.payloads = responses()
        self.payloads["/data/apps/seat/room/list"]["seatRoomList"].append(dict(room_payload(), id=13))
        self.payloads["/data/apps/seat/room/list"]["totalRow"] = 2
        with self.assertRaises(ProtocolQueryError):
            self.api.select_room(plan())
        self.payloads = responses()
        room = self.api.select_room(plan())
        self.payloads["/data/apps/seat/room/info"]["seatRoom"]["id"] = 13
        with self.assertRaises(ProtocolQueryError):
            self.api.room_info(plan(), room)

    def test_missing_closed_and_out_of_range_room_stop(self):
        for change in ({"isOpen": 1}, {"startTime": clock_stamp(21)}, {"endTime": clock_stamp(20, 30)},
                       {"startTime": "08:00"}):
            with self.subTest(change=change):
                self.payloads = responses()
                room = self.api.select_room(plan())
                self.payloads["/data/apps/seat/room/info"]["seatRoom"].update(change)
                with self.assertRaises(ProtocolQueryError):
                    self.api.room_info(plan(), room)
        self.payloads = responses()
        room = self.api.select_room(plan())
        self.payloads["/data/apps/seat/room/info"].pop("seatAttributes")
        with self.assertRaises(ProtocolQueryError):
            self.api.room_info(plan(), room)

    def test_unknown_grid_status_duplicate_labels_and_wrong_room_stop(self):
        for change in ({"reserveStatus": 9}, {"reserveStatus": "0"}, {"roomId": 13}, {"seatNum": "2"}):
            with self.subTest(change=change):
                self.payloads = responses()
                room, info = self._prepared()
                self.payloads["/data/apps/seat/seatgrid/roomid"]["seatDatas"][0].update(change)
                with self.assertRaises(ProtocolQueryError):
                    self.api.seat_map(plan(), room, info)

    def test_unknown_layout_stops_before_seat_request(self):
        self.payloads["/data/apps/seat/room/list"]["seatRoomList"][0]["picSeatMode"] = 0
        self.payloads["/data/apps/seat/room/info"]["seatRoom"]["picSeatMode"] = 0
        room, info = self._prepared()
        with self.assertRaises(ProtocolQueryError):
            self.api.seat_map(plan(), room, info)
        self.assertEqual(len(self.requests), 2)

    def test_occupancy_for_wrong_day_time_and_nonexistent_seat_stops(self):
        for change in ({"today": "2026-10-13"}, {"roomId": 13}, {"seatNum": "004"},
                       {"startTime": stamp(19), "endTime": stamp(20)}, {"startTime": stamp(22)}):
            with self.subTest(change=change):
                self.payloads = responses()
                room, info = self._prepared()
                self.payloads["/data/apps/seat/getusedseatnums"]["seatReserves"][0].update(change)
                with self.assertRaises(ProtocolQueryError):
                    self.api.seat_map(plan(), room, info)

    def test_query_errors_are_redacted_and_do_not_follow_redirects(self):
        cases = [httpx.Response(302, headers={"location": "https://example.invalid/private"}),
                 httpx.Response(200, text="private token invalid"),
                 httpx.Response(200, json={"success": False, "msg": "private token invalid"}),
                 httpx.Response(200, json={"success": True, "data": None})]
        for response in cases:
            calls = []
            def handler(request):
                calls.append(request)
                return response
            with self.subTest(status=response.status_code), httpx.Client(
                    transport=httpx.MockTransport(handler), follow_redirects=True) as client:
                api = ProtocolAPI(client, "local-department")
                with self.assertRaises(ProtocolQueryError) as caught:
                    api.config()
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(len(calls), 1)

    def test_unsafe_origin_rejected_without_network(self):
        for origin in ("http://office.chaoxing.com", "https://other.invalid", "https://office.chaoxing.com:444",
                       "https://user@office.chaoxing.com", "https://office.chaoxing.com/path",
                       "https://office.chaoxing.com?token=private"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                ProtocolAPI(self.client, "local-department", origin=origin)
        ProtocolAPI(self.client, "local-department", origin="http://127.0.0.1:12345")

    def test_records_use_verified_mapping_and_keep_leading_zero(self):
        records = self.api.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].seat, "003")
        self.assertTrue(records[0].matches(plan(), "3"))
        params = self.requests[0].url.params
        self.assertEqual(params["indexId"], "0")
        self.assertEqual(params["pageSize"], "10")
        self.assertEqual(params["type"], "-1")
        self.assertEqual(params["showQrCode"], "1")

    def test_missing_mapping_stops_before_records_query(self):
        with self.assertRaises(ProtocolQueryError):
            ProtocolAPI(self.client, "local-department", record_status_names={}).records()
        self.assertEqual(self.requests, [])

    def test_public_record_switch_and_violation_branch_are_reproduced(self):
        api = ProtocolAPI(self.client, "local-department")
        row = self.payloads["/data/apps/seat/reservelist"]["reserveList"][0]
        for value, expected in ((0, "待履约"), (1, "学习中"), (2, "已履约"),
                                (3, "暂离中"), (5, "被监督中"), (7, "已取消")):
            with self.subTest(status=value):
                row["status"] = value
                self.assertEqual(api.records()[0].status, expected)
        row["status"] = 8
        with self.assertRaises(ProtocolQueryError):
            api.records()
        for record_type, status in ((0, 8), (0, 0), (1, 99), (2, 7)):
            with self.subTest(record_type=record_type, status=status):
                row.update(type=record_type, status=status)
                record = api.records()[0]
                self.assertEqual(record.status, "违约")
                self.assertFalse(record.active)

    def test_record_type_must_be_an_integer_at_least_minus_one(self):
        for value in (-2, "-1", False, None):
            with self.subTest(record_type=value):
                self.payloads["/data/apps/seat/reservelist"]["reserveList"][0]["type"] = value
                with self.assertRaises(ProtocolQueryError):
                    self.api.records()

    def test_room_hours_use_observed_day_clock_values_not_modern_epochs(self):
        room, info = self._prepared()
        self.assertEqual(info.open_start, time(8))
        self.assertEqual(info.open_end, time(22))
        for value in (-1, 86_400_001, stamp(8), "0", False):
            with self.subTest(start_time=value):
                self.payloads["/data/apps/seat/room/info"]["seatRoom"]["startTime"] = value
                with self.assertRaises(ProtocolQueryError):
                    self.api.room_info(plan(), room)

    def test_observed_occupancy_without_today_uses_both_timestamp_dates(self):
        room, info = self._prepared()
        row = self.payloads["/data/apps/seat/getusedseatnums"]["seatReserves"][0]
        row.pop("today")
        row.update(deptId=7, id=19, uid=1234)
        self.assertFalse(self.api.seat_map(plan(), room, info)["002"].available)
        for field in ("startTime", "endTime"):
            with self.subTest(field=field):
                old = row[field]
                row[field] += 86_400_000
                with self.assertRaises(ProtocolQueryError):
                    self.api.used_seats(plan(), room)
                row[field] = old

    def test_public_pause_and_special_interval_fields_are_retained_for_guard(self):
        data = self.payloads["/data/apps/seat/room/info"]
        data["commonPauseTimes"] = [[clock_stamp(12), clock_stamp(13)]]
        data["seatIntervalMap"] = {"1": [{"startTime": "08:00", "endTime": "09:00"}]}
        _, info = self._prepared()
        self.assertEqual(info.common_pause_times, ((clock_stamp(12), clock_stamp(13)),))
        self.assertEqual(info.seat_interval_map, data["seatIntervalMap"])
        self.assertNotIn("common_pause_times", repr(info))
        self.assertNotIn("seat_interval_map", repr(info))

    def test_missing_or_unknown_pause_and_special_interval_shapes_stop(self):
        changes = [("commonPauseTimes", None), ("commonPauseTimes", {}),
                   ("commonPauseTimes", [[1]]), ("commonPauseTimes", [[2, 1]]),
                   ("commonPauseTimes", [[False, 4]]), ("seatIntervalMap", []),
                   ("seatIntervalMap", {"8": []}), ("seatIntervalMap", {"1": {}}),
                   ("seatIntervalMap", {"1": ["08:00"]}),
                   ("seatIntervalMap", {"1": [{"startTime": "08:00"}]}),
                   ("seatIntervalMap", {"1": [{"startTime": "09:00", "endTime": "08:00"}]}),
                   ("seatIntervalMap", {"1": [{"startTime": "08:00", "endTime": "24:00"}]})]
        for name, value in changes:
            with self.subTest(name=name, value=value):
                self.payloads = responses()
                room = self.api.select_room(plan())
                self.payloads["/data/apps/seat/room/info"][name] = value
                with self.assertRaises(ProtocolQueryError):
                    self.api.room_info(plan(), room)

    def test_record_unknown_status_date_and_time_do_not_become_success(self):
        for change in ({"status": 102}, {"status": "101"}, {"today": "2026-10-13"},
                       {"startTime": 123}, {"startTime": stamp(22)}, {"seatNum": "invalid"}):
            with self.subTest(change=change):
                self.payloads = responses()
                self.payloads["/data/apps/seat/reservelist"]["reserveList"][0].update(change)
                with self.assertRaises(ProtocolQueryError):
                    self.api.records()

    def test_repeated_record_pagination_stops_instead_of_looping(self):
        self.payloads["/data/apps/seat/reservelist"]["exitNextPage"] = True
        with self.assertRaises(ProtocolQueryError):
            self.api.records()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[-1].url.params["indexId"], "9")


if __name__ == "__main__":
    unittest.main()
