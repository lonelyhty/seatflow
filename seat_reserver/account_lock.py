"""Cross-process leases; leave lock files in place to avoid unlink races."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .credentials import data_dir


class AccountBusy(Exception):
    pass


class AccountLease:
    def __init__(self, account: str, directory: Path | None = None):
        digest = hashlib.sha256(account.strip().casefold().encode("utf-8")).hexdigest()
        self.path = (directory or data_dir() / "locks") / f"{digest}.lock"
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise AccountBusy("这个账号已有任务或测试浏览器正在运行，请先停止原任务。") from exc
        self.stream = stream
        return self

    def __exit__(self, *_):
        if self.stream is not None:
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None
