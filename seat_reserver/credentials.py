"""Encrypt credentials with Windows DPAPI for the current Windows user."""
from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path

from .model import Settings


def data_dir() -> Path:
    override = os.environ.get('SEATFLOW_DATA_DIR')
    return Path(override) if override else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SeatFlow"


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(value: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("账号加密保存目前仅支持 Windows。")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(value)
    source = DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = DataBlob()
    # No machine-wide flag: encryption is bound to the current Windows user.
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    path = path or data_dir() / "settings.json"
    secret = json.dumps({"account": settings.account, "password": settings.password}, ensure_ascii=False).encode("utf-8")
    payload = {"version": 1, "settings": settings.public_dict(),
               "credentials_dpapi": base64.b64encode(_crypt(secret, decrypt=False)).decode("ascii")}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return path


def load_settings(path: Path | None = None) -> Settings:
    path = path or data_dir() / "settings.json"
    if not path.exists():
        return Settings.new_account()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1:
        raise ValueError("配置文件版本不受支持。")
    encrypted = base64.b64decode(payload["credentials_dpapi"], validate=True)
    secret = json.loads(_crypt(encrypted, decrypt=True).decode("utf-8"))
    fields = Settings.__dataclass_fields__
    values = {k: v for k, v in payload.get("settings", {}).items()
              if k in fields and k not in {"account", "password"}}
    return Settings(**values, account=secret["account"], password=secret["password"])
