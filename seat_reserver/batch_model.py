"""Multi-account configuration. Credentials only leave memory as DPAPI ciphertext."""
from __future__ import annotations

import base64
import copy
import json
import os
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from .credentials import _crypt, data_dir, load_settings
from .model import Plan, Settings, now_china, parse_time


def masked_account(account: str) -> str:
    value = account.strip()
    return value[:2] + "****" + value[-2:] if len(value) > 4 else "****"


@dataclass
class Profile:
    name: str = "新账号"
    settings: Settings = field(default_factory=Settings.new_account, repr=False)
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class BatchSettings:
    profiles: list[Profile] = field(default_factory=list)
    max_parallel: int = 20
    release_time: str = "22:30"
    headless: bool = False
    use_profile_release: bool = False

    def validate(self) -> None:
        if type(self.max_parallel) is not int or not 1 <= self.max_parallel <= 64:
            raise ValueError("同时运行上限请填写 1–64 的整数；实际上限取决于本机资源。")
        parse_time(self.release_time)
        if type(self.headless) is not bool:
            raise ValueError("显示浏览器选项格式不正确。")
        if type(self.use_profile_release) is not bool:
            raise ValueError("各账号放号时间选项格式不正确。")
        ids, accounts = set(), set()
        for index, profile in enumerate(self.profiles, 1):
            if not re.fullmatch(r"[a-f0-9]{32}", profile.id) or profile.id in ids:
                raise ValueError(f"第 {index} 项的任务编号无效或重复。")
            if not isinstance(profile.name, str) or not profile.name.strip() or len(profile.name) > 60:
                raise ValueError(f"第 {index} 项请填写不超过 60 字的备注。")
            if type(profile.enabled) is not bool:
                raise ValueError(f"第 {index} 项的启用选项无效。")
            profile.settings.validate_account()
            account = profile.settings.account.strip().casefold()
            if account in accounts:
                raise ValueError(f"第 {index} 项的学习通账号重复；同一账号只保留一项。")
            accounts.add(account)
            ids.add(profile.id)

    def jobs(self, mode: str, now: datetime | None = None) -> list[tuple[Profile, Plan | None]]:
        if mode not in {"reserve", "login", "preview"}:
            raise ValueError("不支持的运行模式。")
        self.validate()
        selected = [p for p in self.profiles if p.enabled]
        if not selected:
            raise ValueError("请至少添加并启用一个账号。")
        if len(selected) > self.max_parallel:
            raise ValueError(f"已启用 {len(selected)} 个账号，超过同时运行上限 {self.max_parallel}。请调整上限或停用部分账号；程序不会排队到预约时间之后。")
        now = now or now_china()
        result = []
        for index, original in enumerate(selected, 1):
            profile = copy.deepcopy(original)
            profile.settings = replace(profile.settings, autostart=False,
                                       release_time=profile.settings.release_time if self.use_profile_release else self.release_time)
            try:
                plan = None if mode == "login" else profile.settings.plan(now, for_preview=mode == "preview")
            except ValueError as exc:
                raise ValueError(f"启用账号第 {index} 项：{exc}") from exc
            result.append((profile, plan))
        return result


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def save_batch(config: BatchSettings, path: Path | None = None) -> Path:
    config.validate()
    path = path or data_dir() / "batch.json"
    secrets = {p.id: {"account": p.settings.account, "password": p.settings.password} for p in config.profiles}
    encrypted = _crypt(json.dumps(secrets, ensure_ascii=False).encode("utf-8"), decrypt=False)
    payload = {
        "version": 1, "max_parallel": config.max_parallel, "release_time": config.release_time,
        "headless": config.headless,
        "use_profile_release": config.use_profile_release,
        "profiles": [{"id": p.id, "name": p.name, "enabled": p.enabled,
                      "settings": p.settings.public_dict()} for p in config.profiles],
        "credentials_dpapi": base64.b64encode(encrypted).decode("ascii"),
    }
    atomic_json(path, payload)
    return path


def load_batch(path: Path | None = None) -> BatchSettings:
    default_path = path is None
    path = path or data_dir() / "batch.json"
    if not path.exists():
        config = BatchSettings()
        if default_path:
            legacy = load_settings()
            if legacy.account and legacy.password:
                config.profiles.append(Profile(name="原单账号配置", settings=legacy))
        return config
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1:
        raise ValueError("多账号配置版本不受支持。")
    secrets = json.loads(_crypt(base64.b64decode(payload["credentials_dpapi"], validate=True), decrypt=True).decode("utf-8"))
    profiles = []
    for item in payload["profiles"]:
        public = {k: v for k, v in item["settings"].items()
                  if k in Settings.__dataclass_fields__ and k not in {"account", "password"}}
        secret = secrets[item["id"]]
        settings = Settings(**public, account=secret["account"], password=secret["password"])
        profiles.append(Profile(item["name"], settings, item["enabled"], item["id"]))
    result = BatchSettings(profiles, payload["max_parallel"], payload["release_time"], payload["headless"],
                           payload.get("use_profile_release", False))
    result.validate()
    return result
