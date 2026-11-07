"""Independent browser workers with no queue or all-account barrier at release time."""
from __future__ import annotations

import copy
import re
import threading
import uuid
from pathlib import Path
from typing import Callable

from .batch_model import BatchSettings, atomic_json, masked_account
from .credentials import data_dir
from .model import now_china
from .runner import ReservationRunner

RESULT_NAMES = {
    "SUCCESS": "预约成功", "EXISTING": "已有有效预约", "LOGIN_READY": "登录测试通过",
    "PREVIEW_READY": "选座测试通过", "UNAVAILABLE": "未约到候选座位", "UNKNOWN": "需要核对结果",
    "ERROR": "运行失败", "STOPPED": "已停止", "MANUAL_REQUIRED": "需要人工验证",
}
OK_RESULTS = {"SUCCESS", "EXISTING", "LOGIN_READY", "PREVIEW_READY"}


class BatchRunner:
    def __init__(self, emit: Callable[[dict], None], *, runner_factory=ReservationRunner,
                 log_root: Path | None = None):
        self.callback = emit
        self.runner_factory = runner_factory
        self.log_root = log_root or data_dir() / "batch-runs"
        self.report_path: Path | None = None
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.done.set()
        self.threads: dict[str, threading.Thread] = {}
        self.stops: dict[str, threading.Event] = {}
        self.states: dict[str, dict] = {}
        self.remaining = 0
        self.secrets: tuple[str, ...] = ()
        self.report_error = False
        self.mode = ""
        self.started_at = ""
        self.finished_at = ""

    @property
    def running(self) -> bool:
        return not self.done.is_set()

    def scrub(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[已隐藏]")
        return re.sub(r"https?://\S+", "[网站地址已隐藏]", value)

    def emit(self, event: dict) -> None:
        safe = {k: self.scrub(v) if isinstance(v, str) and k in {"text", "name"} else v
                for k, v in event.items()}
        try:
            self.callback(safe)
        except Exception:
            # An unavailable UI/consumer must never retry or abort another account.
            pass

    def snapshot(self) -> list[dict]:
        with self.lock:
            return copy.deepcopy(list(self.states.values()))

    def _write_report(self) -> None:
        # Called only under self.lock (or before any worker exists).
        if self.report_path is None:
            return
        try:
            atomic_json(self.report_path, {
                "version": 1, "mode": self.mode, "started_at": self.started_at,
                "finished_at": self.finished_at, "jobs": list(self.states.values()),
            })
        except OSError:
            if not self.report_error:
                self.report_error = True
                self.emit({"kind": "log", "text": "结果文件无法写入，请保留窗口中的运行结果。"})

    def start(self, config: BatchSettings, *, mode: str = "reserve") -> None:
        if self.running or any(t.is_alive() for t in self.threads.values()):
            raise ValueError("当前批次尚未结束，请先停止或等待完成。")
        jobs = config.jobs(mode)
        self.mode = mode
        self.started_at = now_china().isoformat()
        self.finished_at = ""
        self.report_error = False
        self.secrets = tuple(sorted({v for p, _ in jobs for v in (p.settings.account, p.settings.password) if v}, key=len, reverse=True))
        run_dir = self.log_root / f"{now_china():%Y%m%d-%H%M%S-%f}-{uuid.uuid4().hex[:8]}"
        run_dir.mkdir(parents=True, exist_ok=False)
        self.report_path = run_dir / "results.json"
        self.stops = {p.id: threading.Event() for p, _ in jobs}
        self.states = {
            p.id: {"id": p.id, "name": self.scrub(p.name), "account": masked_account(p.settings.account),
                   "plan": plan.summary() if plan is not None else "只测试登录，不提交预约",
                   "trigger": plan.trigger.isoformat() if plan is not None else None,
                   "phase": "准备启动", "ready": False, "code": "", "text": "", "seconds": None,
                   "triggered_at": None, "delay_ms": None, "closed": False}
            for p, plan in jobs
        }
        self.remaining = len(jobs)
        self.threads = {}
        self._write_report()
        self.done.clear()
        self.emit({"kind": "batch_started", "text": f"启动 {len(jobs)} 个独立账号任务。", "mode": mode, "count": len(jobs)})
        for index, (profile, plan) in enumerate(jobs):
            worker = threading.Thread(target=self._work, args=(profile, plan, config.headless, index),
                                      name=f"seat-{profile.id[:8]}", daemon=False)
            self.threads[profile.id] = worker
            try:
                worker.start()
            except Exception:
                self._event(profile.id, {"kind": "result", "code": "ERROR", "text": "本机无法创建这个账号的工作线程。"})
                self._completed(profile.id)

    def _event(self, profile_id: str, event: dict) -> None:
        event = dict(event)
        event["text"] = self.scrub(event.get("text", ""))
        with self.lock:
            state = self.states[profile_id]
            kind = event["kind"]
            if kind == "phase" and not state["code"]:
                state["phase"] = event["text"]
            elif kind == "ready":
                state["ready"] = True
            elif kind == "countdown":
                state["seconds"] = event["seconds"]
            elif kind == "release":
                state["triggered_at"] = event["triggered_at"]
                state["delay_ms"] = event["delay_ms"]
            elif kind == "result":
                state["code"] = event["code"]
                state["text"] = event["text"]
                state["phase"] = RESULT_NAMES.get(event["code"], "任务结束")
                self._write_report()
            elif kind == "closed":
                state["closed"] = True
            event.update(profile_id=profile_id, name=state["name"])
        self.emit(event)

    def _work(self, profile, plan, headless: bool, index: int) -> None:
        runner = None
        try:
            if self.stops[profile.id].is_set():
                self._event(profile.id, {"kind": "result", "code": "STOPPED", "text": "启动前已停止。"})
                return
            # Every thread owns its Playwright instance, browser and context.
            runner = self.runner_factory(lambda e: self._event(profile.id, e), self.stops[profile.id],
                                         headless=headless, keep_open=False,
                                         log_path=self.report_path.parent / f"{profile.id}.log",
                                         preflight_seconds=90 + 2 * index)
            code = runner.run(profile.settings, plan, login_only=self.mode == "login", preview=self.mode == "preview")
            with self.lock:
                reported = bool(self.states[profile.id]["code"])
            if not reported:
                self._event(profile.id, {"kind": "result", "code": code or "ERROR", "text": "任务已结束。"})
        except Exception as exc:
            uncertain = runner is not None and getattr(runner, "submission_started", False)
            self._event(profile.id, {"kind": "result", "code": "UNKNOWN" if uncertain else "ERROR",
                                    "text": f"账号任务异常（{type(exc).__name__}）；其他账号继续运行。"})
        finally:
            self._completed(profile.id)

    def _completed(self, profile_id: str) -> None:
        with self.lock:
            self.states[profile_id]["closed"] = True
            self.remaining -= 1
            last = self.remaining == 0
            if last:
                self.finished_at = now_china().isoformat()
            self._write_report()
        self.emit({"kind": "job_done", "profile_id": profile_id, "text": "账号任务已结束。"})
        if last:
            self.done.set()
            self.emit({"kind": "batch_done", "text": "本批次全部结束，结果已汇总。"})

    def stop(self, profile_id: str | None = None) -> None:
        with self.lock:
            targets = list(self.stops) if profile_id is None else [profile_id]
            for key in targets:
                if key in self.stops:
                    self.stops[key].set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)

    def join(self) -> None:
        for worker in self.threads.values():
            if worker.ident is not None:
                worker.join()
