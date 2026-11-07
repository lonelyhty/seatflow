from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .schools import DEFAULT_ENTRY_URL, SchoolEntry, location_key, validate_location, validate_school_target

CHINA_TZ = timezone(timedelta(hours=8))
ENTRY_URL = DEFAULT_ENTRY_URL
CAMPUS = "北辰校区图书馆"


def now_china() -> datetime:
    return datetime.now(CHINA_TZ)


def parse_time(value: str, *, half_hour: bool = False) -> time:
    if not re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", value):
        raise ValueError("时间请填写为 HH:MM，例如 22:30。")
    try:
        result = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("时间不正确，请检查小时和分钟。") from exc
    if half_hour and (result.minute not in (0, 30) or result.second):
        raise ValueError("座位使用时段需要按半小时选择。")
    return result


def parse_seats(value: str) -> tuple[str, ...]:
    parts = re.split(r"[,，、;；\s]+", value.strip())
    if not value.strip() or any(not re.fullmatch(r"\d{1,4}", s) for s in parts if s):
        raise ValueError("请填写所选楼层的座位号；多个候选座位用逗号分隔。")
    # Keep the label the user saw. Resolve leading zeros against the actual floor map.
    labels = {}
    for label in parts:
        if label:
            labels.setdefault(seat_key(label), label)
    result = tuple(labels.values())
    if any(int(s) < 1 for s in result) or len(result) > 8:
        raise ValueError("座位号必须大于 0，最多填写 8 个候选座位。")
    return result


def seat_key(label: str) -> str:
    return str(int(label))


def floor_key(campus: str, floor: str) -> str:
    return f"{campus}|{floor}"


@dataclass
class Settings:
    account: str = ""
    password: str = ""
    campus: str = CAMPUS
    floor: str = "3F"
    seats: str = ""
    date_mode: str = "tomorrow"
    booking_date: str = ""
    start_time: str = "08:00"
    end_time: str = "12:00"
    release_time: str = "22:30"
    autostart: bool = True
    floor_seats: dict[str, str] = field(default_factory=dict)
    school_name: str = "河北工业大学"
    entry_url: str = ENTRY_URL
    third_level: str = ""
    room_id: int = 0
    release_days_before: int = 1
    days_ahead: int = 1
    time_unit: int = 30

    @classmethod
    def new_account(cls) -> Settings:
        # Defaults on Settings itself remain the legacy school's migration path.
        # New interactive accounts require their own explicit school selection.
        return cls(school_name='', entry_url='', campus='', floor='', autostart=False)

    def __post_init__(self) -> None:
        if not isinstance(self.floor_seats, dict):
            raise ValueError("各楼层的座位配置格式不正确。")
        self.floor_seats = {str(k): str(v) for k, v in self.floor_seats.items()}
        # Migrate old settings only to their originally selected floor.
        self.floor_seats[self.location_key()] = self.seats

    def location_key(self) -> str:
        return location_key(self.entry_url, self.campus, self.floor, self.third_level, self.room_id)

    def public_dict(self) -> dict:
        result = asdict(self)
        result.pop("account")
        result.pop("password")
        return result

    def validate_account(self) -> None:
        if not self.account.strip() or not self.password:
            raise ValueError("请填写学习通账号和密码。账号可使用手机号或超星号。")
        validate_school_target(self.school_name, self.entry_url)

    def validate_school(self) -> None:
        validate_school_target(self.school_name, self.entry_url)
        validate_location(self.campus, self.floor, self.third_level, self.room_id)
        if not isinstance(self.school_name, str) or not self.school_name.strip() or len(self.school_name) > 120:
            raise ValueError("请填写学校名称，最多 120 字。")
        for value, name in ((self.days_ahead, "可预约天数"), (self.release_days_before, "提前放号天数")):
            if type(value) is not int or not 0 <= value <= 30:
                raise ValueError(f"{name}应为 0–30 的整数。")
        if type(self.time_unit) is not int or not 1 <= self.time_unit <= 120:
            raise ValueError("时段步长应为 1–120 分钟，须与学校页面一致。")

    def plan(self, now: datetime | None = None, *, for_preview: bool = False) -> Plan:
        self.validate_account()
        now = now or now_china()
        if now.tzinfo is None:
            now = now.replace(tzinfo=CHINA_TZ)
        now = now.astimezone(CHINA_TZ)
        self.validate_school()
        if self.date_mode == "tomorrow":
            target = now.date() + timedelta(days=1)
        elif self.date_mode == "specific":
            try:
                target = date.fromisoformat(self.booking_date)
            except ValueError as exc:
                raise ValueError("预约日期请填写为 YYYY-MM-DD。") from exc
        else:
            raise ValueError("预约日期选项不正确。")
        if not now.date() <= target <= now.date() + timedelta(days=self.days_ahead):
            raise ValueError(f"日期应在今天至未来 {self.days_ahead} 天内；请按学校实际规则设置可预约天数。")
        start = parse_time(self.start_time)
        end = parse_time(self.end_time)
        if (start >= end or any(t.second or t.microsecond for t in (start, end))
                or (end.hour * 60 + end.minute - start.hour * 60 - start.minute) % self.time_unit):
            raise ValueError("使用时段需在同一天内，结束晚于开始，时长应为学校时段步长的整数倍。")
        # The default remains the old half-hour alignment. Other grids are checked
        # against the actual room opening time by the protocol adapter.
        if self.time_unit == 30 and any(t.minute not in (0, 30) for t in (start, end)):
            raise ValueError("当前时段步长为 30 分钟，请按半小时填写。")
        if not for_preview and target == now.date() and datetime.combine(target, start, CHINA_TZ) <= now:
            raise ValueError("今天的开始使用时间已经过去，请修改时段或预约明天。")
        release = parse_time(self.release_time)
        trigger = datetime.combine(target - timedelta(days=self.release_days_before), release, CHINA_TZ)
        seats = parse_seats(self.seats) if self.seats.strip() or not for_preview else ()
        return Plan(target, start, end, trigger, self.campus, self.floor, seats,
                    self.third_level, self.room_id, self.time_unit)


@dataclass(frozen=True)
class Plan:
    day: date
    start: time
    end: time
    trigger: datetime
    campus: str
    floor: str
    seats: tuple[str, ...]
    third_level: str = ""
    room_id: int = 0
    time_unit: int = 30

    @property
    def interval(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"

    @property
    def time_cells(self) -> tuple[str, str]:
        start = datetime.combine(self.day, self.start)
        end = datetime.combine(self.day, self.end)
        return (
            f"{start:%H:%M}-{start + timedelta(minutes=self.time_unit):%H:%M}",
            f"{end - timedelta(minutes=self.time_unit):%H:%M}-{end:%H:%M}",
        )

    def summary(self) -> str:
        location = " ".join(value for value in (self.campus, self.floor, self.third_level) if value)
        return f"{self.day:%Y-%m-%d} · {location} · {self.interval} · 座位 {','.join(self.seats)}"


ACTIVE_STATUSES = {"待履约", "待签到", "已预约", "预约成功", "已签到", "使用中", "履约中", "已履约",
                   "学习中", "暂离中", "被监督中"}


@dataclass(frozen=True)
class Record:
    day: date
    campus: str
    floor: str
    seat: str
    interval: str
    status: str
    third_level: str = ""
    room_id: int = 0

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def matches(self, plan: Plan, seat: str) -> bool:
        return (
            self.active and self.day == plan.day and self.campus == plan.campus
            and self.floor == plan.floor and int(self.seat) == int(seat)
            and self.interval == plan.interval
            and (not plan.third_level or self.third_level == plan.third_level)
            and (not plan.room_id or self.room_id == plan.room_id)
        )


def parse_record(title: str, address: str, day_text: str, status: str) -> Record | None:
    date_match = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", day_text)
    interval_match = re.search(r"(\d{2}:\d{2})\s*[-–至]\s*(\d{2}:\d{2})", title)
    seat_match = re.search(r"座位\s*(\d+)", address)
    if not all((date_match, interval_match, seat_match)):
        return None
    parts = [part.strip() for part in re.split(r"\s*-\s*", address[:seat_match.start()].strip())]
    if len(parts) != 3 or not parts[0] or not parts[1]:
        return None
    try:
        record_day = date(*(int(v) for v in date_match.groups()))
    except ValueError:
        return None
    return Record(record_day, parts[0], parts[1], seat_match.group(1),
                  "-".join(interval_match.groups()), status.strip(), parts[2])


def records_url(entry_url: str = ENTRY_URL) -> str:
    url = urlsplit(entry_url)
    dept = parse_qs(url.query).get("fidEnc", [""])[0]
    if not dept:
        raise ValueError("预约入口缺少校区标识。")
    return urlunsplit((url.scheme, url.netloc, "/front/third/apps/seat/reserve/list", urlencode({"deptIdEnc": dept}), ""))
