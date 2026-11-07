"""Strict, read-only HTTP queries for the observed Chaoxing seat API.

The caller owns the authenticated ``httpx.Client`` and its pooled connections.
This module has no reservation, cancellation, sign-in or arbitrary-path method.
Unknown response shapes fail closed instead of treating missing data as free seats.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

import httpx

from .model import CHINA_TZ, Plan, Record, parse_time, seat_key
from .schools import validate_location


class ProtocolQueryError(RuntimeError):
    """A query could not be safely interpreted; the message contains no payload."""


READ_ONLY_METHODS = MappingProxyType({
    "/data/apps/seat/room/list": "GET",
    "/data/apps/seat/seatgrid/roomid": "GET",
    "/data/apps/seat/reservelist": "GET",
    "/data/apps/seat/config": "GET",
    "/data/apps/seat/room/info": "POST",
    "/data/apps/seat/getusedseatnums": "POST",
    "/data/apps/seat/risk/check/config": "POST",
})

# The normal record page's public getStatusName(status, type) function uses
# this switch only for type == -1; every other type takes its "违约" branch.
# Unknown switch values still fail closed instead of guessing their meaning.
VERIFIED_RECORD_STATUSES = MappingProxyType({
    0: "待履约", 1: "学习中", 2: "已履约", 3: "暂离中", 5: "被监督中", 7: "已取消",
})


def _dict(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise ProtocolQueryError(f"{name}字段结构变化，已停止。")
    return value


def _list(value: Any, name: str) -> list:
    if not isinstance(value, list):
        raise ProtocolQueryError(f"{name}字段结构变化，已停止。")
    return value


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ProtocolQueryError(f"{name}字段不正确，已停止。")
    return value


def _text(value: Any, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or value != value.strip() or (not empty and not value):
        raise ProtocolQueryError(f"{name}字段不正确，已停止。")
    return value


def _label(value: Any) -> str:
    label = _text(value, "座位号")
    if not re.fullmatch(r"[0-9]{1,4}", label) or int(label) <= 0:
        raise ProtocolQueryError("座位号字段不正确，已停止。")
    return label


def _epoch(value: Any, name: str) -> datetime:
    stamp = _integer(value, name)
    if not 100_000_000_000 <= stamp <= 99_999_999_999_999:
        raise ProtocolQueryError(f"{name}不是已核实的毫秒时间戳，已停止。")
    try:
        return datetime.fromtimestamp(stamp / 1000, CHINA_TZ)
    except (ValueError, OverflowError, OSError) as exc:
        raise ProtocolQueryError(f"{name}时间戳超出范围，已停止。") from exc


def _clock_stamp(value: Any, name: str) -> time:
    # Room opening hours are epoch-relative milliseconds of a day, not modern
    # booking timestamps: observed 0 and 50_400_000 render as 08:00 and 22:00
    # through the normal page's moment(value) in the China time zone.
    stamp = _integer(value, name, -28_800_000)
    if stamp >= 57_600_000 or stamp % 60_000:
        raise ProtocolQueryError(f"{name}不是已核实的日内毫秒时间，已停止。")
    return datetime.fromtimestamp(stamp / 1000, CHINA_TZ).time()


def _date(value: Any, name: str = "日期") -> date:
    text = _text(value, name)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ProtocolQueryError(f"{name}格式变化，已停止。")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ProtocolQueryError(f"{name}不正确，已停止。") from exc


def validate_plan(plan: Plan) -> None:
    if not isinstance(plan, Plan) or type(plan.day) is not date:
        raise ValueError("预约计划日期不正确。")
    validate_location(plan.campus, plan.floor, plan.third_level, plan.room_id)
    if not all(isinstance(value, time) and value.tzinfo is None and value.second == 0
               and value.microsecond == 0
               for value in (plan.start, plan.end)):
        raise ValueError("预约时段必须精确到分钟。")
    if (not plan.start < plan.end or type(plan.time_unit) is not int or not 1 <= plan.time_unit <= 120
            or ((plan.end.hour * 60 + plan.end.minute) - (plan.start.hour * 60 + plan.start.minute)) % plan.time_unit):
        raise ValueError("预约时段或时段步长不正确。")
    for label in plan.seats:
        _label(label)


@dataclass(frozen=True)
class Room:
    id: int
    campus: str
    floor: str
    third_level: str
    capacity: int
    layout_mode: int
    start_seat: int
    dept_id: int


@dataclass(frozen=True)
class RoomInfo:
    room: Room
    day: date
    open_start: time
    open_end: time
    disabled_seats: frozenset[str]
    config: dict = field(repr=False)
    before_open_timestamp: int = 0
    common_pause_times: tuple[tuple[int, int], ...] = field(default=(), repr=False)
    seat_interval_map: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class SeatAvailability:
    label: str
    available: bool
    reason: str = ""


@dataclass(frozen=True)
class RiskConfig:
    enabled: bool
    type: int
    before_ms: int
    after_ms: int


class ProtocolAPI:
    def __init__(self, client: httpx.Client, dept_id_enc: str, *,
                 origin: str = "https://office.chaoxing.com", timeout: float = 15,
                 record_status_names: dict[int, str] | None = None):
        parts = urlsplit(origin)
        local = parts.hostname in {"127.0.0.1", "localhost", "::1"}
        remote = parts.scheme == "https" and parts.netloc == "office.chaoxing.com"
        if (not (remote or (local and parts.scheme == "http")) or parts.path not in {"", "/"}
                or parts.query or parts.fragment or parts.username or parts.password):
            raise ValueError("查询来源必须是学校预约域名或本地测试服务。")
        if not isinstance(dept_id_enc, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", dept_id_enc):
            raise ValueError("校区会话标识不正确。")
        if not 0 < timeout <= 60:
            raise ValueError("查询超时应在 0 至 60 秒之间。")
        self.client = client
        self.dept_id_enc = dept_id_enc
        self.origin = origin.rstrip("/")
        self.timeout = timeout
        # Only a verified frontend mapping may be supplied. No integer status is
        # inferred from a successful query or from the common meaning of a code.
        names = dict(VERIFIED_RECORD_STATUSES) if record_status_names is None else record_status_names
        if not isinstance(names, dict) or any(type(key) is not int or not isinstance(value, str)
                                              or not value.strip() for key, value in names.items()):
            raise ValueError("预约记录状态映射不正确。")
        self.record_status_names = MappingProxyType(dict(names))

    def request(self, method: str, path: str, parameters: dict | None = None) -> dict:
        if READ_ONLY_METHODS.get(path) != method:
            raise ValueError("该路径不属于已核实的只读接口。")
        options = {"timeout": self.timeout, "follow_redirects": False,
                   "headers": {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}}
        options["params" if method == "GET" else "data"] = parameters or {}
        try:
            response = self.client.request(method, self.origin + path, **options)
        except httpx.HTTPError as exc:
            raise ProtocolQueryError("只读查询网络异常，已停止。") from exc
        if response.status_code != 200:
            raise ProtocolQueryError(f"只读查询返回 HTTP {response.status_code}，已停止。")
        if len(response.content) > 4_000_000:
            raise ProtocolQueryError("只读查询响应超出限制，已停止。")
        try:
            payload = _dict(response.json(), "响应")
        except (ValueError, UnicodeError) as exc:
            raise ProtocolQueryError("只读查询未返回 JSON，登录可能已失效。") from exc
        if payload.get("success") is not True:
            raise ProtocolQueryError("只读查询未返回成功状态，已停止。")
        return _dict(payload.get("data"), "查询数据")

    def _query(self, path: str, parameters: dict) -> dict:
        method = READ_ONLY_METHODS.get(path)
        if method is None:
            raise ValueError("该路径不属于已核实的只读接口。")
        return self.request(method, path, parameters)

    @staticmethod
    def _room(value: Any) -> Room:
        row = _dict(value, "房间")
        return Room(
            _integer(row.get("id"), "房间编号", 1),
            _text(row.get("firstLevelName"), "校区"),
            _text(row.get("secondLevelName"), "楼层"),
            _text(row.get("thirdLevelName"), "区域", empty=True),
            _integer(row.get("capacity"), "座位容量", 1),
            _integer(row.get("picSeatMode"), "布局模式"),
            _integer(row.get("startSeatNum"), "起始座位号", 1),
            _integer(row.get("deptId"), "学校编号", 1),
        )

    def rooms(self, plan: Plan) -> tuple[Room, ...]:
        validate_plan(plan)
        return self.list_rooms(plan.day, plan.campus)

    def list_rooms(self, day: date, campus: str = "") -> tuple[Room, ...]:
        """Read a complete catalog, including all campuses when none is selected."""
        if type(day) is not date or not isinstance(campus, str) or campus != campus.strip():
            raise ValueError("查询房间的日期或校区格式不正确。")
        results = []
        total_pages = None
        total_rows = None
        for page in range(1, 33):
            data = self._query("/data/apps/seat/room/list", {
                "deptIdEnc": self.dept_id_enc, "day": day.isoformat(),
                "cpage": page, "pageSize": 100, "firstLevelName": campus,
                "secondLevelName": "", "thirdLevelName": "", "time": "",
            })
            pages = _integer(data.get("totalPage"), "房间页数")
            rows_count = _integer(data.get("totalRow"), "房间总数")
            if (pages > 32 or (total_pages is not None and pages != total_pages)
                    or (total_rows is not None and rows_count != total_rows)):
                raise ProtocolQueryError("房间分页发生变化，已停止。")
            total_pages = pages
            total_rows = rows_count
            rows = [self._room(row) for row in _list(data.get("seatRoomList"), "房间列表")]
            if len(rows) > 100 or (campus and any(room.campus != campus for room in rows)):
                raise ProtocolQueryError("房间列表与目标校区不一致，已停止。")
            results.extend(rows)
            if page >= pages:
                break
        if len({room.id for room in results}) != len(results):
            raise ProtocolQueryError("房间列表包含重复编号，已停止。")
        if len(results) != total_rows:
            raise ProtocolQueryError("房间列表数量与分页总数不一致，已停止。")
        return tuple(results)

    def select_room(self, plan: Plan) -> Room:
        choices = [room for room in self.rooms(plan) if room.floor == plan.floor
                   and (not plan.third_level or room.third_level == plan.third_level)
                   and (not plan.room_id or room.id == plan.room_id)]
        if len(choices) != 1:
            raise ProtocolQueryError("未找到唯一的目标房间，请先查询房间并填写校区、楼层、三级区域或房间编号。")
        return choices[0]

    @staticmethod
    def _check_room(plan: Plan, room: Room) -> None:
        validate_plan(plan)
        if (room.campus != plan.campus or room.floor != plan.floor or room.id <= 0
                or (plan.third_level and room.third_level != plan.third_level)
                or (plan.room_id and room.id != plan.room_id)):
            raise ProtocolQueryError("房间与预约校区、楼层不一致，已停止。")

    def room_info(self, plan: Plan, room: Room) -> RoomInfo:
        self._check_room(plan, room)
        data = self._query("/data/apps/seat/room/info", {
            "fidEnc": self.dept_id_enc, "id": room.id,
            "toDay": plan.day.isoformat(), "queryReserve": "true",
        })
        actual = self._room(data.get("seatRoom"))
        if actual != room:
            raise ProtocolQueryError("房间详情与所选楼层不一致，已停止。")
        raw_room = data["seatRoom"]
        if _integer(raw_room.get("isOpen"), "房间开放状态") != 0:
            raise ProtocolQueryError("目标房间未开放，已停止。")
        start = _clock_stamp(raw_room.get("startTime"), "房间开始时间")
        end = _clock_stamp(raw_room.get("endTime"), "房间结束时间")
        if not start <= plan.start < plan.end <= end:
            raise ProtocolQueryError("预约时段超出房间开放时间，已停止。")
        disabled = set()
        attributes = _list(data.get("seatAttributes"), "座位属性")
        seen = set()
        for raw in attributes:
            attribute = _dict(raw, "座位属性条目")
            label = _label(attribute.get("seatNum"))
            if label in seen or _integer(attribute.get("roomId"), "属性房间编号", 1) != room.id:
                raise ProtocolQueryError("座位属性房间或编号不一致，已停止。")
            seen.add(label)
            enabled = _integer(attribute.get("isReserve"), "座位可预约属性")
            if enabled not in (0, 1):
                raise ProtocolQueryError("未知座位可预约属性，已停止。")
            if enabled == 0:
                disabled.add(label)
        config = dict(_dict(data.get("seatConfig"), "座位配置"))
        timestamp = _integer(data.get("beforeOpenTimeStamp"), "预约开放时间")
        pauses = []
        for raw_pause in _list(data.get("commonPauseTimes"), "暂停预约时段"):
            pair = _list(raw_pause, "暂停预约时段条目")
            if len(pair) != 2:
                raise ProtocolQueryError("暂停预约时段字段结构变化，已停止。")
            begin = _integer(pair[0], "暂停开始时间")
            finish = _integer(pair[1], "暂停结束时间")
            if begin >= finish:
                raise ProtocolQueryError("暂停预约时段不正确，已停止。")
            pauses.append((begin, finish))
        intervals = _dict(data.get("seatIntervalMap"), "特殊座位时段")
        checked_intervals = {}
        # Public seat_select_third.js indexes this map by weekday 1..7 and
        # constructs intervals from each entry's startTime/endTime strings.
        for weekday, raw_intervals in intervals.items():
            if not isinstance(weekday, str) or weekday not in {str(number) for number in range(1, 8)}:
                raise ProtocolQueryError("特殊座位时段星期字段变化，已停止。")
            checked_rows = []
            for raw_interval in _list(raw_intervals, "特殊座位时段列表"):
                interval = _dict(raw_interval, "特殊座位时段条目")
                values = []
                for name in ("startTime", "endTime"):
                    text = _text(interval.get(name), "特殊座位时段时间")
                    if not re.fullmatch(r"[0-9]{2}:[0-9]{2}", text):
                        raise ProtocolQueryError("特殊座位时段时间格式变化，已停止。")
                    try:
                        values.append(parse_time(text))
                    except ValueError as error:
                        raise ProtocolQueryError("特殊座位时段时间不正确，已停止。") from error
                if values[0] >= values[1]:
                    raise ProtocolQueryError("特殊座位时段顺序不正确，已停止。")
                checked_rows.append(dict(interval))
            checked_intervals[weekday] = checked_rows
        return RoomInfo(room, plan.day, start, end, frozenset(disabled), config, timestamp,
                        tuple(pauses), checked_intervals)

    def used_seats(self, plan: Plan, room: Room) -> frozenset[str]:
        self._check_room(plan, room)
        data = self._query("/data/apps/seat/getusedseatnums", {
            "fidEnc": self.dept_id_enc, "roomId": room.id, "day": plan.day.isoformat(),
            "startTime": plan.start.strftime("%H:%M"), "endTime": plan.end.strftime("%H:%M"),
        })
        used = set()
        for raw in _list(data.get("seatReserves"), "占用座位"):
            row = _dict(raw, "占用座位条目")
            label = _label(row.get("seatNum"))
            if (_integer(row.get("roomId"), "占用房间编号", 1) != room.id
                    or ("today" in row and _date(row["today"]) != plan.day)):
                raise ProtocolQueryError("占用座位与目标房间、日期不一致，已停止。")
            start = _epoch(row.get("startTime"), "占用开始时间")
            end = _epoch(row.get("endTime"), "占用结束时间")
            if (start.date() != plan.day or end.date() != plan.day or start >= end
                    or not (start.time() < plan.end and plan.start < end.time())):
                raise ProtocolQueryError("占用座位与查询时段不一致，已停止。")
            _integer(row.get("status"), "占用状态")
            used.add(label)
        return frozenset(used)

    def seat_map(self, plan: Plan, room: Room, info: RoomInfo) -> dict[str, SeatAvailability]:
        self._check_room(plan, room)
        if info.room != room or info.day != plan.day:
            raise ProtocolQueryError("座位详情与目标日期、房间不一致，已停止。")
        if room.layout_mode != 2:
            raise ProtocolQueryError("该楼层布局尚未验证为网格座位，已停止。")
        data = self._query("/data/apps/seat/seatgrid/roomid", {
            "fidEnc": self.dept_id_enc, "roomId": room.id,
        })
        states = {}
        keys = set()
        for raw in _list(data.get("seatDatas"), "座位网格"):
            row = _dict(raw, "座位网格条目")
            label = _label(row.get("seatNum"))
            if (label in states or seat_key(label) in keys
                    or _integer(row.get("roomId"), "网格房间编号", 1) != room.id):
                raise ProtocolQueryError("座位网格房间或编号不一致，已停止。")
            status = row.get("reserveStatus")
            if type(status) is not int or status not in (0, -1, -2, -3):
                raise ProtocolQueryError("未知座位网格状态，已停止。")
            disabled = label in info.disabled_seats or status < 0
            states[label] = SeatAvailability(label, not disabled, "不可预约" if disabled else "")
            keys.add(seat_key(label))
        if not states or len(states) > room.capacity:
            raise ProtocolQueryError("座位网格数量异常，已停止。")
        if not info.disabled_seats.issubset(states):
            raise ProtocolQueryError("禁用座位不在实际座位网格中，已停止。")
        used = self.used_seats(plan, room)
        if not used.issubset(states):
            raise ProtocolQueryError("占用座位不在实际座位网格中，已停止。")
        for label in used:
            states[label] = SeatAvailability(label, False, "所选时段已占用")
        return states

    @staticmethod
    def resolve_candidate(seats: dict[str, SeatAvailability], candidates: tuple[str, ...]) -> str | None:
        for candidate in candidates:
            _label(candidate)
            matches = [label for label in seats if seat_key(label) == seat_key(candidate)]
            if len(matches) > 1:
                raise ProtocolQueryError("候选座位对应多个实际标签，已停止。")
            if matches and seats[matches[0]].available:
                return matches[0]
        return None

    def config(self) -> dict:
        data = self._query("/data/apps/seat/config", {"fidEnc": self.dept_id_enc})
        return dict(_dict(data.get("seatConfig"), "座位配置"))

    def records(self) -> tuple[Record, ...]:
        if not self.record_status_names:
            raise ProtocolQueryError("预约记录的状态映射尚未核实，已停止。")
        records = []
        seen = set()
        index = 0
        for _ in range(32):
            data = self._query("/data/apps/seat/reservelist", {
                "fidEnc": self.dept_id_enc, "indexId": index, "pageSize": 10,
                "showQrCode": 1, "type": -1,
            })
            rows = _list(data.get("reserveList"), "预约记录列表")
            more = data.get("exitNextPage")
            if type(more) is not bool or len(rows) > 10 or (more and not rows):
                raise ProtocolQueryError("预约记录分页结构变化，已停止。")
            for raw in rows:
                row = _dict(raw, "预约记录")
                row_id = _integer(row.get("id"), "预约记录编号", 1)
                if row_id in seen:
                    raise ProtocolQueryError("预约记录分页重复，已停止。")
                seen.add(row_id)
                day = _date(row.get("today"))
                start = _epoch(row.get("startTime"), "预约开始时间")
                end = _epoch(row.get("endTime"), "预约结束时间")
                if start.date() != day or end.date() != day or start >= end:
                    raise ProtocolQueryError("预约记录日期、时段不一致，已停止。")
                campus = _text(row.get("firstLevelName"), "预约校区")
                floor = _text(row.get("secondLevelName"), "预约楼层")
                third_level = _text(row.get("thirdLevelName"), "预约区域", empty=True)
                room_id = _integer(row["roomId"], "预约房间编号", 1) if "roomId" in row else 0
                status = _integer(row.get("status"), "预约记录状态")
                record_type = _integer(row.get("type"), "预约记录类型", -1)
                if record_type == -1:
                    if status not in self.record_status_names:
                        raise ProtocolQueryError("预约记录含未知状态，已停止。")
                    status_name = self.record_status_names[status]
                else:
                    status_name = "违约"
                records.append(Record(day, campus, floor, _label(row.get("seatNum")),
                                      f"{start:%H:%M}-{end:%H:%M}", status_name, third_level, room_id))
                index = row_id
            if not more:
                return tuple(records)
        raise ProtocolQueryError("预约记录超出分页检查范围，已停止。")

    def risk_config(self, app_id: int, app_type: int = 0) -> RiskConfig:
        _integer(app_id, "风险校验学校编号", 1)
        _integer(app_type, "风险校验应用类型")
        data = self._query("/data/apps/seat/risk/check/config", {"appId": app_id, "appType": app_type})
        enabled = data.get("riskCheckOpen")
        if type(enabled) is not bool:
            raise ProtocolQueryError("风险校验开关格式变化，已停止。")
        return RiskConfig(enabled, _integer(data.get("riskCheckType"), "风险校验类型"),
                          _integer(data.get("riskCheckBeforeMilliSecond"), "风险校验前窗口"),
                          _integer(data.get("riskCheckAfterMilliSecond"), "风险校验后窗口"))
