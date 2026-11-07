"""Read school configuration and room names without any booking operation."""
from __future__ import annotations

import threading
from datetime import date, timedelta

from .account_lock import AccountLease
from .model import Settings, now_china
from .protocol_api import ProtocolAPI
from .schools import SchoolEntry


def read_school_catalog(settings: Settings, *, day: str | date | None = None,
                        stop=None, client_factory=None) -> dict:
    from .protocol_runner import ProtocolRunner

    settings.validate_account()
    entry = SchoolEntry.parse(settings.entry_url)
    if day is None:
        day = now_china().date()
    elif isinstance(day, str):
        day = date.fromisoformat(day)
    if type(day) is not date or not now_china().date() <= day <= now_china().date() + timedelta(days=30):
        raise ValueError("房间查询日期应在今天至未来 30 天内。")
    runner = ProtocolRunner(lambda _: None, stop or threading.Event(), client_factory=client_factory,
                            submit_enabled=False, keep_open=False)
    runner.settings = settings
    runner.entry_url = entry.url
    with AccountLease(settings.account):
        runner.checkpoint()
        with runner._new_client(None) as client:
            runner._authenticate(client, {})
            api = ProtocolAPI(client, entry.department)
            runner.checkpoint()
            rooms = api.list_rooms(day)
            runner.checkpoint()
            config = api.config()
    # Only public configuration fields are returned. No identity, cookies,
    # request URLs, signatures or full server responses leave this function.
    return {
        "school_name": settings.school_name, "day": str(day), "submission_attempts": 0,
        "default_rules": {name: config[name] for name in
                          ("reserveBeforeDay", "reserveBeforeTime", "timeType", "timeUnit")
                          if type(config.get(name)) in (str, int)},
        "rooms": [{"id": room.id, "campus": room.campus, "floor": room.floor,
                   "third_level": room.third_level, "capacity": room.capacity,
                   "layout_mode": room.layout_mode} for room in rooms],
    }
