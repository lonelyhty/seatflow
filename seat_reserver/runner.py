from __future__ import annotations

import os
import re
import threading
import time as monotonic_time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as BrowserError, Page, sync_playwright

from .account_lock import AccountBusy, AccountLease
from .model import ACTIVE_STATUSES, ENTRY_URL, Plan, Record, Settings, now_china, parse_record, parse_time, records_url, seat_key
from .schools import SchoolEntry, location_key, binding_prompt, SCHOOL_BINDING_MESSAGE


class Stopped(Exception):
    pass


class FlowError(Exception):
    """A message safe to display without exposing browser URLs or credentials."""


class ManualRequired(FlowError):
    pass


class ReservationRunner:
    def __init__(self, emit: Callable[[dict], None], stop: threading.Event, *,
                 entry_url: str | None = None, headless: bool = False,
                 keep_open: bool = True, test_site: bool = False,
                 verify_delays: tuple[float, ...] = (0.5, 1, 2, 3, 4),
                 log_path: Path | None = None, preflight_seconds: float = 90,
                 service_workers: str = "allow"):
        self.emit_callback = emit
        self.stop = stop
        self.entry_url = entry_url or ENTRY_URL
        self._entry_override = entry_url
        self.headless = headless
        self.keep_open = keep_open
        self.test_site = test_site
        self.verify_delays = verify_delays
        self.settings = Settings()
        self.context = None
        self.page = None
        self.outcome = ""
        self.submission_started = False
        self.auth_error = False
        self.identity_binding = False
        self.verification_waited = False
        self.expected_seat_count = 0
        self.stage = "准备启动"
        self.log_path = log_path
        self.preflight_seconds = preflight_seconds
        self.service_workers = service_workers
        self.last_login_at = None

    def emit(self, kind: str, text: str = "", **data) -> None:
        for secret in (self.settings.password, self.settings.account):
            if secret:
                text = text.replace(secret, "[已隐藏]")
        text = re.sub(r"https?://\S+", "[网站地址已隐藏]", text)
        if kind == "phase":
            self.stage = text
        if self.log_path is not None and kind in {"phase", "log", "result", "closed"}:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as stream:
                    stream.write(f"{now_china():%Y-%m-%d %H:%M:%S} [{kind}] {text}\n")
            except OSError:
                self.log_path = None
                self.emit_callback({"kind": "log", "text": "本次日志文件无法写入，请在窗口运行记录中查看进度。"})
        self.emit_callback({"kind": kind, "text": text, **data})

    def checkpoint(self) -> None:
        if self.stop.is_set():
            raise Stopped()

    def pause(self, seconds: float, page: Page | None = None) -> None:
        deadline = monotonic_time.monotonic() + seconds
        while monotonic_time.monotonic() < deadline:
            self.checkpoint()
            remaining = min(0.2, deadline - monotonic_time.monotonic())
            if remaining <= 0:
                break
            if page is not None and not page.is_closed():
                page.wait_for_timeout(remaining * 1000)
            elif self.stop.wait(remaining):
                raise Stopped()

    def wait_for(self, predicate: Callable[[], bool], seconds: float, page: Page) -> bool:
        deadline = monotonic_time.monotonic() + seconds
        while monotonic_time.monotonic() < deadline:
            self.checkpoint()
            if predicate():
                return True
            self.pause(0.15, page)
        return bool(predicate())

    @staticmethod
    def visible(locator) -> bool:
        return locator.count() == 1 and locator.is_visible()

    def safe_login_origin(self, page: Page) -> bool:
        url = urlsplit(page.url)
        if self.test_site:
            return url.hostname in {"127.0.0.1", "localhost"}
        return url.scheme == "https" and url.netloc == "passport2.chaoxing.com"

    def _dialog(self, dialog) -> None:
        message = dialog.message
        if binding_prompt(message):
            self.identity_binding = True
        if re.search(r"账号或密码错误|密码错误|密码不正确|账号不存在|用户不存在|账号已被锁定", message):
            self.auth_error = True
        if dialog.type == "alert":
            dialog.accept()
        elif (dialog.type == "confirm" and self.submission_started
              and re.search(r"预约|提交", message)
              and not re.search(r"协议|条款|隐私|验证", message)):
            dialog.accept()
        elif dialog.type == "beforeunload":
            dialog.accept()
        else:
            dialog.dismiss()
            self.emit("log", "页面出现未识别的确认提示；程序不会据此重复提交。")

    def _goto(self, page: Page, url: str) -> None:
        self.checkpoint()
        page.goto(url, wait_until="domcontentloaded", timeout=25000)

    def _ready(self, page: Page) -> bool:
        url = urlsplit(page.url)
        if not self.test_site and (url.scheme != 'https' or url.netloc != "office.chaoxing.com"):
            return False
        if not self.test_site and not SchoolEntry.parse(self.entry_url).matches_page(page.url):
            raise FlowError('登录后页面与所选学校入口不一致，请核对该校预约链接。')
        self._check_binding(page)
        return (
            self.visible(page.get_by_text("预约选座", exact=True))
            or (self.visible(page.locator("li.active h6.mtab_time"))
                 and page.locator("li.scr_list .scr_sch").count() > 0)
        )

    def _check_binding(self, page: Page) -> None:
        if self.identity_binding or binding_prompt(page.locator('body').inner_text(timeout=4000)):
            raise ManualRequired(SCHOOL_BINDING_MESSAGE)

    def login(self, page: Page) -> None:
        self.auth_error = False
        self.identity_binding = False
        self.emit("phase", "正在登录")
        self._goto(page, self.entry_url)
        phone = page.locator("#phone")
        password = page.locator("#pwd")
        if not self.wait_for(lambda: self._ready(page) or self.visible(phone), 25, page):
            raise FlowError("预约入口没有加载出登录或预约页面，请检查网络后重试。")
        if self._ready(page):
            self.dismiss_notice(page)
            self.last_login_at = now_china()
            return
        if not self.safe_login_origin(page) or not self.visible(password):
            raise FlowError("登录页面与已验证的学习通登录入口不同，已停止填写账号。")
        phone.fill(self.settings.account.strip())
        password.fill(self.settings.password)
        page.locator("#loginBtn").click()
        started = monotonic_time.monotonic()
        manual_reported = False
        entry_reopen_attempts = 0
        while monotonic_time.monotonic() - started < 180:
            self.checkpoint()
            if self.auth_error:
                raise FlowError("学习通提示账号或密码有误，或账号不可用，请检查本地配置。")
            url = urlsplit(page.url)
            at_office = url.scheme == 'https' and url.netloc == 'office.chaoxing.com'
            if self.test_site:
                at_office = (url.hostname in {'127.0.0.1', 'localhost'}
                             and url.path in {'/front/third/apps/seat/index', '/front/third/apps/seat/list'})
            if at_office:
                if self.test_site:
                    selected_entry = url.hostname in {'127.0.0.1', 'localhost'} and url.path in {
                        '/front/third/apps/seat/index', '/front/third/apps/seat/list'}
                else:
                    selected_entry = SchoolEntry.parse(self.entry_url).matches_page(page.url)
                if not selected_entry:
                    if entry_reopen_attempts >= 3:
                        raise FlowError('登录后页面与所选学校入口不一致，请核对该校预约链接。')
                    # SSO can complete with a stale callback to a previously selected school.
                    self.identity_binding = False
                    self._goto(page, self.entry_url)
                    entry_reopen_attempts += 1
                    continue
            if self._ready(page):
                self.dismiss_notice(page)
                self.last_login_at = now_china()
                self.emit("log", "学习通登录完成，已进入预约页面。")
                return
            body = page.locator("body").inner_text(timeout=4000)
            if self.identity_binding or binding_prompt(body):
                raise ManualRequired(SCHOOL_BINDING_MESSAGE)
            invalid = re.search(r"账号或密码错误|密码错误|密码不正确|账号不存在|用户不存在|账号已被锁定|账户被锁定", body)
            if invalid:
                raise FlowError("学习通提示账号或密码有误，或账号不可用，请检查本地配置。")
            if not manual_reported and monotonic_time.monotonic() - started > 8:
                if self.headless:
                    raise ManualRequired("登录需要人工检查。请改用显示浏览器模式，完成验证后再启动该账号。")
                self.emit("phase", "等待登录验证")
                self.emit("log", "登录尚未完成。如需验证码、协议确认或其他验证，请在浏览器中处理；程序会继续等待。")
                manual_reported = True
            self.pause(0.25, page)
        raise FlowError("登录等待超时。请完成浏览器里的验证，或检查账号后重新启动。")

    def dismiss_notice(self, page: Page) -> None:
        notice = page.locator(".rp_sure").filter(has_text="开始预约")
        if self.visible(notice):
            # This is the observed school notice, not an account agreement.
            if re.search(r"用户协议|隐私政策|服务条款", page.locator("body").inner_text()):
                if self.headless:
                    raise ManualRequired("页面需要人工确认，请改用显示浏览器模式处理。")
                self.emit("phase", "等待页面确认")
                self.emit("log", "页面需要确认协议，请在浏览器中操作。")
                if not self.wait_for(lambda: not self.visible(notice), 180, page):
                    raise FlowError("页面确认等待超时。")
            else:
                notice.click()

    def open_list(self, page: Page, plan: Plan) -> None:
        self.emit("phase", "正在打开楼层列表")
        if "/seat/list" not in urlsplit(page.url).path:
            self._goto(page, self.entry_url)
            if not self.wait_for(lambda: self._ready(page) or self.visible(page.locator("#phone")), 20, page):
                raise FlowError("预约页面加载超时。")
            if self.visible(page.locator("#phone")):
                self.login(page)
            self.dismiss_notice(page)
            page.get_by_text("预约选座", exact=True).click()
        day = page.get_by_role("heading", name=plan.day.strftime("%m-%d"), exact=True)
        # Vue initially exposes an unmounted li.scr_list with {{...}} placeholders.
        # Wait for rendered dates and real floor data, not just template nodes.
        self.emit("log", f"等待 {plan.day:%m-%d} 日期和 {plan.floor} 楼层信息加载完成。")
        if not self.wait_for(lambda: self.visible(day) and self.floor_loaded(page, plan), 25, page):
            raise FlowError(f"等待页面加载后，仍未找到 {plan.day:%m-%d} 的日期或 {plan.floor} 楼层。请查看网络和页面的实际预约日期。")
        day.click()
        selected = page.locator("li.active h6.mtab_time").filter(has_text=re.compile("^" + re.escape(plan.day.strftime("%m-%d")) + "$"))
        if not self.wait_for(lambda: self.visible(selected) and self.floor_loaded(page, plan), 25, page):
            raise FlowError("目标日期尚未切换完成，或楼层数据仍未加载。请查看页面提示。")
        self.emit("log", f"已打开 {plan.day:%Y-%m-%d} 的楼层列表，准备进入 {plan.floor}。")

    @staticmethod
    def floor_card(page: Page, plan: Plan):
        suffix = re.escape(plan.third_level) + r"(?:\s|$)" if plan.third_level else ""
        return page.locator("li.scr_list").filter(
            has_text=re.compile(re.escape(plan.campus) + r"\s*-\s*" + re.escape(plan.floor) + r"\s*-\s*" + suffix))

    def floor_loaded(self, page: Page, plan: Plan) -> bool:
        card = self.floor_card(page, plan)
        if card.count() != 1 or not self.visible(card.locator(".scr_sch")):
            return False
        capacity = card.locator(".scr_use span").first
        return capacity.count() == 1 and bool(re.fullmatch(r"\d+", capacity.inner_text().strip()))

    def open_floor(self, page: Page, plan: Plan) -> bool:
        self.emit("phase", f"正在点击 {plan.floor} 的选座按钮")
        card = self.floor_card(page, plan)
        if card.count() != 1:
            raise FlowError("找不到唯一的目标楼层，请检查校区和楼层。")
        count = card.locator(".scr_use span").first
        self.expected_seat_count = int(count.inner_text().strip()) if count.count() and count.inner_text().strip().isdigit() else 0
        card.locator(".scr_sch").click()
        blocked = page.get_by_text(re.compile("当前不在可预约时间范围内"))
        if not self.wait_for(lambda: "/seat/select" in urlsplit(page.url).path or self.visible(blocked), 12, page):
            raise FlowError("选座页面未打开；可能有新的提示需要在浏览器中处理。")
        if self.visible(blocked):
            self.emit("log", "页面提示尚未到开放时间，将等待后再尝试。")
            ok = page.get_by_text("确定", exact=True)
            if self.visible(ok):
                ok.click()
            return False
        query = parse_qs(urlsplit(page.url).query)
        if query.get("day", [""])[0] != plan.day.isoformat():
            raise FlowError("选座页面日期与配置不一致，已停止。")
        if not self.wait_for(lambda: self.visible(page.locator(".time_sure")), 12, page):
            raise FlowError("没有出现已验证的时段选择器，已停止。")
        self.verify_floor(page, plan)
        self.emit("log", f"已进入 {plan.floor}，页面日期 {plan.day:%Y-%m-%d}。")
        return True

    def verify_floor(self, page: Page, plan: Plan) -> None:
        query = parse_qs(urlsplit(page.url).query)
        heading = page.get_by_role("heading").first.inner_text()
        if query.get("day", [""])[0] != plan.day.isoformat():
            raise FlowError("选座页面日期与配置不一致，已停止。")
        if not re.search(re.escape(plan.campus) + r"\s*-\s*" + re.escape(plan.floor) + r"\s*-", heading):
            raise FlowError("选座页面楼层与配置不一致，已停止。")
        if plan.third_level and not re.search(r"-\s*" + re.escape(plan.third_level) + r"\s*$", heading.strip()):
            raise FlowError("选座页面三级区域与配置不一致，已停止。")

    def read_seat_map(self, page: Page, plan: Plan) -> dict[str, str]:
        self.verify_floor(page, plan)
        if not self.interval_confirmed(page, plan):
            raise FlowError("必须先选择使用时段并点击确认，才能读取座位。")
        self.emit("phase", f"正在读取 {plan.floor} 座位图")
        def labels():
            # The page can contain a hidden old map. Only read rendered cells after confirmation.
            rendered = page.locator(".grid_cell").evaluate_all("""elements => elements.filter(el => {
                const box = el.getBoundingClientRect(), style = getComputedStyle(el);
                return box.width > 0 && box.height > 0 && style.display !== 'none'
                    && style.visibility !== 'hidden' && style.opacity !== '0';
            }).map(el => el.innerText)""")
            return [text.strip() for text in rendered
                    if re.fullmatch(r"\d{1,4}", text.strip()) and int(text.strip()) > 0]
        def loaded():
            count = len(labels())
            return count >= self.expected_seat_count if self.expected_seat_count else count > 0
        if not self.wait_for(loaded, 15, page):
            raise FlowError(f"{plan.floor} 的座位图尚未完整加载，未使用其他楼层的编号。")
        result = {}
        for label in labels():
            key = seat_key(label)
            if key in result:
                raise FlowError(f"{plan.floor} 座位图中存在重复编号，已停止自动选择。")
            result[key] = label
        if self.expected_seat_count and len(result) != self.expected_seat_count:
            raise FlowError(f"{plan.floor} 座位图与楼层列表的数量不一致，请查看页面。")
        self.emit("log", f"时段 {plan.interval} 已确认，已读取 {plan.day:%Y-%m-%d} {plan.floor} 的 {len(result)} 个实际座位号。")
        self.emit("catalog", campus=plan.campus, floor=plan.floor, day=plan.day.isoformat(), interval=plan.interval,
                  location_key=location_key(self.settings.entry_url, plan.campus, plan.floor, plan.third_level, plan.room_id),
                  labels=sorted(result.values(), key=lambda label: int(label)))
        return result

    def preview_interval(self, page: Page, plan: Plan) -> Plan:
        available = {}
        cells = page.locator("li.time_cell")
        for index in range(cells.count()):
            cell = cells.nth(index)
            label = cell.inner_text().strip()
            if re.fullmatch(r"\d{2}:\d{2}-\d{2}:\d{2}", label) and "noSelect" not in (cell.get_attribute("class") or "").split():
                start, end = label.split("-")
                available[label] = (parse_time(start), parse_time(end))
        required = []
        cursor = datetime.combine(plan.day, plan.start)
        end = datetime.combine(plan.day, plan.end)
        while cursor < end:
            following = cursor + timedelta(minutes=plan.time_unit)
            required.append(f"{cursor:%H:%M}-{following:%H:%M}")
            cursor = following
        now = now_china()
        requested_is_future = datetime.combine(plan.day, plan.start, now.tzinfo) >= now
        if requested_is_future and all(label in available for label in required):
            return plan
        for label, (start, end) in sorted(available.items()):
            if datetime.combine(plan.day, start, now.tzinfo) >= now:
                actual = replace(plan, start=start, end=end)
                self.emit("log", f"预设时段在本次测试日期不可选，测试临时使用 {actual.day:%Y-%m-%d} {label}；保存的正式预约时段仍为 {plan.interval}。")
                return actual
        raise FlowError("本次测试日期没有可选的使用时段，无法确认时段或读取座位。请换一个可预约日期后测试；明日未开放时需等开放后再测试。")

    def interval_confirmed(self, page: Page, plan: Plan) -> bool:
        interval = page.locator(".o_use > em")
        return (not self.visible(page.locator(".time_sure")) and interval.count() == 1
                and re.sub(r"\s+", "", interval.inner_text()) == plan.interval)

    def preview_seats(self, page: Page, plan: Plan) -> str:
        self.emit("phase", f"正在读取 {plan.floor} 座位")
        self.open_list(page, plan)
        actual = plan
        fallback = False
        if not self.open_floor(page, plan):
            if plan.day == now_china().date():
                raise FlowError("今天的选座页暂时无法打开，请查看学校预约提示。")
            fallback = True
            actual = replace(plan, day=now_china().date())
            self.emit("log", "明日预约尚未开放。本次读取今天同层的座位编号；明日空闲状态将在正式预约时重新检查。")
            self.open_list(page, actual)
            if not self.open_floor(page, actual):
                raise FlowError("今天的座位图也无法打开，请查看学校预约提示。")
        actual = self.preview_interval(page, actual)
        self.choose_interval(page, actual)
        catalog = self.read_seat_map(page, actual)
        missing = [seat for seat in plan.seats if seat_key(seat) not in catalog]
        if missing:
            self.emit("log", f"{plan.floor} 没有这些座位：{','.join(missing)}。请从本层实际座位中重新选择。")
        selected = ""
        for candidate in plan.seats:
            label = catalog.get(seat_key(candidate))
            if label is None:
                continue
            cell = self.seat_cell(page, label)
            classes = (cell.get_attribute("class") or "").split()
            if "grid_seat" in classes and "grid_lock" not in classes:
                cell.click()
                self.verify_draft(page, actual, label)
                selected = label
                break
        detail = f"测试已确认 {actual.day:%Y-%m-%d} {actual.interval}，进入 {actual.floor} 并读取 {len(catalog)} 个实际座位号。"
        if fallback:
            detail += "明日尚未开放，今天的图仅供查看编号。"
        if selected:
            detail += f"已测试选中 {selected}，未提交。"
        if (actual.start, actual.end) != (plan.start, plan.end):
            detail += f"临时测试时段不更改正式预约时段 {plan.interval}。"
        if missing:
            detail += f"请更换本层不存在的编号：{','.join(missing)}。"
        detail += "可从本层座位目录选择，再点击保存并启动预约。"
        return self.finish("PREVIEW_READY", detail)

    @staticmethod
    def seat_cell(page: Page, label: str):
        return page.locator(".grid_cell").filter(has_text=re.compile(r"^\s*" + re.escape(label) + r"\s*$"))

    def choose_interval(self, page: Page, plan: Plan) -> None:
        self.emit("phase", f"正在选择并确认时段 {plan.interval}")
        first, last = plan.time_cells
        for index, text in enumerate((first, last)):
            if index == 1 and first == last:
                continue
            cell = page.locator("li.time_cell").filter(has_text=re.compile("^" + re.escape(text) + "$"))
            if cell.count() != 1 or "noSelect" in (cell.get_attribute("class") or ""):
                raise FlowError("目标使用时段不可选择，请检查时段和预约日期。")
            cell.click()
        page.locator(".time_sure").click()
        if not self.wait_for(lambda: self.interval_confirmed(page, plan), 12, page):
            raise FlowError("时段尚未确认成功，未读取座位。请查看浏览器里的时段和确认提示。")
        self.emit("log", f"已点击时段确认，页面使用时间为 {plan.interval}，接下来加载座位图。")

    def read_records(self) -> list[Record]:
        self.checkpoint()
        page = self.context.new_page()
        try:
            self.emit("phase", "正在等待预约记录加载")
            self._goto(page, records_url(self.entry_url))
            def records_loaded():
                # A static tab label or an unmounted record template is not a result.
                if page.locator(".rec_per").filter(has_text=re.compile(r"\{\{")).count():
                    return False
                loading = page.get_by_text(re.compile(r"^加载中\.*$")).first
                if self.visible(loading):
                    return False
                return (page.locator(".rec_per:visible").count() > 0
                        or self.visible(page.locator(".gnorecord").filter(has_text="无预约记录")))
            if not self.wait_for(records_loaded, 25, page):
                raise FlowError("预约记录尚未加载出完整记录或明确的空记录提示，暂时不能确认结果。")
            if not self.test_site and urlsplit(page.url).hostname != "office.chaoxing.com":
                raise FlowError("查询记录时登录已失效，暂时不能确认结果。")
            result = []
            cards = page.locator(".rec_per:visible")
            for index in range(cards.count()):
                card = cards.nth(index)
                title = card.locator(".rec_title").inner_text()
                address = card.locator(".rec_addr").inner_text()
                day = card.locator(".rec_tip").inner_text()
                status = card.locator(".rec_title span").inner_text()
                record = parse_record(title, address, day, status)
                if record:
                    if record.status not in ACTIVE_STATUSES | {"已取消", "违约", "已结束", "已完成"}:
                        raise FlowError("预约记录出现未识别的状态，已停止自动提交。")
                    result.append(record)
                else:
                    raise FlowError("存在无法识别的预约记录，已停止自动提交。")
            return result
        finally:
            page.close()

    def existing(self, records: list[Record], plan: Plan) -> Record | None:
        return next((r for r in records if r.active and r.day == plan.day), None)

    def wait_release(self, page: Page, plan: Plan) -> None:
        self.emit("phase", "等待开放预约")
        # A just-completed login does not need another simultaneous login burst.
        refreshed = ((plan.trigger - now_china()).total_seconds() <= self.preflight_seconds
                     and self.last_login_at is not None
                     and (now_china() - self.last_login_at).total_seconds() < 60)
        self.emit("ready", "登录和预约日期已就绪。", trigger=plan.trigger.isoformat())
        previous = None
        while True:
            self.checkpoint()
            remaining = (plan.trigger - now_china()).total_seconds()
            if remaining <= 0:
                self.emit("countdown", seconds=0)
                actual = now_china()
                self.emit("release", "已到开放时间，开始预约流程。", trigger=plan.trigger.isoformat(),
                          triggered_at=actual.isoformat(), delay_ms=max(0, round((actual - plan.trigger).total_seconds() * 1000)))
                return
            seconds = int(remaining + 0.999)
            if seconds != previous:
                self.emit("countdown", seconds=seconds)
                previous = seconds
            # One preflight close to opening; no continuous polling of the website.
            if remaining <= self.preflight_seconds and not refreshed:
                self.emit("log", "开放前检查登录状态和预约日期。")
                self.login(page)
                self.open_list(page, plan)
                self.emit("phase", "等待开放预约")
                refreshed = True
                continue
            self.pause(min(0.05 if remaining < 2 else 0.25, remaining), page)

    def verify_draft(self, page: Page, plan: Plan, seat: str) -> None:
        self.verify_floor(page, plan)
        query = parse_qs(urlsplit(page.url).query)
        if query.get("day", [""])[0] != plan.day.isoformat():
            raise FlowError("提交前发现日期不一致，已停止。")
        heading = page.get_by_role("heading").first.inner_text()
        if not re.search(re.escape(plan.campus) + r"\s*-\s*" + re.escape(plan.floor) + r"\s*-", heading):
            raise FlowError("提交前发现校区或楼层不一致，已停止。")
        interval = re.sub(r"\s+", "", page.locator(".o_use > em").inner_text())
        chosen = page.locator(".o_use_res label")
        checked = page.locator(".grid_checked")
        if (interval != plan.interval or chosen.count() != 1 or checked.count() != 1
                or chosen.inner_text().strip() != seat or checked.inner_text().strip() != seat):
            raise FlowError("提交前发现座位或使用时段不一致，已停止。")
        submit = page.locator(".order_submit")
        if submit.inner_text().strip() != "提交" or "noClick" in (submit.get_attribute("class") or ""):
            raise FlowError("页面提交按钮不可用，已停止。")

    def finish(self, code: str, text: str) -> str:
        self.outcome = code
        self.emit("result", text, code=code)
        self.emit("log", text)
        return code

    def _confirm_explicit_booking(self, page: Page) -> None:
        # Only explicit booking confirmations are eligible; generic buttons are left alone.
        button = page.get_by_text(re.compile(r"^(确认预约|确定预约|确认提交)$"))
        if self.visible(button):
            if re.search(r"用户协议|隐私政策|服务条款", page.locator("body").inner_text()):
                if self.headless:
                    raise ManualRequired("提交页面需要人工确认，提交状态须核对；请使用显示浏览器模式。")
                self.emit("phase", "等待页面确认")
                self.emit("log", "提交页面需要额外协议确认，请在浏览器中处理。")
            else:
                button.click()

    def _await_verification(self, page: Page) -> None:
        if self.verification_waited:
            return
        challenge = re.compile(r"请完成.{0,8}验证|安全验证|拖动滑块|滑动.{0,8}验证|请输入验证码")
        if not challenge.search(page.locator("body").inner_text(timeout=5000)):
            return
        self.verification_waited = True
        if self.headless:
            self.emit("log", "预约需要人工验证；无界面模式接下来只核对记录，不重复提交。")
            return
        self.emit("phase", "等待预约验证")
        self.emit("log", "页面要求验证，请在浏览器中手动完成；程序会等待最多 3 分钟并继续核对记录。")
        if not self.wait_for(lambda: not challenge.search(page.locator("body").inner_text(timeout=5000)), 180, page):
            self.emit("log", "验证尚未完成，接下来仅核对预约记录，不重复提交。")
        self.emit("phase", "正在核对预约结果")

    def after_submit(self, page: Page, plan: Plan, seat: str) -> str:
        rejection = ""
        for delay in self.verify_delays:
            self.pause(delay, page)
            self._confirm_explicit_booking(page)
            self._await_verification(page)
            body = page.locator("body").inner_text(timeout=5000)
            match = re.search(r"座位已被(?:预约|占用)|该座位已被(?:预约|占用)|预约失败|提交失败|座位不可用", body)
            if match:
                rejection = match.group(0)
            try:
                records = self.read_records()
            except (BrowserError, FlowError):
                continue
            if any(r.matches(plan, seat) for r in records):
                try:
                    self._goto(page, records_url(self.entry_url))
                except BrowserError:
                    self.emit("log", "记录已经核对成功，主窗口暂未加载出记录页面。")
                return self.finish("SUCCESS", f"预约成功，已在预约记录中核对：{plan.day:%Y-%m-%d} {plan.floor} {seat}号座位，{plan.interval}。")
            if self.existing(records, plan):
                return self.finish("UNKNOWN", "预约记录出现了另一条有效预约，已停止。请在浏览器核对具体结果。")
            if rejection:
                self.emit("log", "页面明确拒绝了本次预约，预约记录中也未发现成功记录。")
                return "REJECTED"
        return self.finish("UNKNOWN", "提交结果尚未确认，已停止重复提交。请在浏览器查看预约记录；确认无预约后再重新启动。")

    def reserve(self, page: Page, plan: Plan) -> str:
        self.emit("phase", "正在预约")
        rejected_seats = set()
        for attempt in range(1, 4):
            self.checkpoint()
            self.emit("log", f"第 {attempt}/3 轮检查目标日期和候选座位。")
            self.open_list(page, plan)
            if not self.open_floor(page, plan):
                self.pause(2, page)
                continue
            self.choose_interval(page, plan)
            catalog = self.read_seat_map(page, plan)
            missing = [seat for seat in plan.seats if seat_key(seat) not in catalog]
            if missing:
                self.emit("log", f"{plan.floor} 没有这些编号：{','.join(missing)}，仅检查本层存在的候选座位。")
            candidates = [catalog[seat_key(seat)] for seat in plan.seats if seat_key(seat) in catalog]
            if not candidates:
                raise FlowError(f"所填座位号都不在 {plan.floor}。请读取本层座位并重新选择。")
            for seat in candidates:
                self.checkpoint()
                if seat in rejected_seats:
                    continue
                cell = self.seat_cell(page, seat)
                if cell.count() != 1:
                    self.emit("log", f"{seat}号座位不在目标楼层，将检查下一个候选。")
                    continue
                classes = (cell.get_attribute("class") or "").split()
                if "grid_seat" not in classes or "grid_lock" in classes:
                    self.emit("log", f"{seat}号座位当前不可选，将检查下一个候选。")
                    continue
                cell.click()
                self.verify_draft(page, plan, seat)
                current = self.existing(self.read_records(), plan)
                if current:
                    return self.finish("EXISTING", f"目标日期已有有效预约：{current.floor} {current.seat}号座位，{current.interval}；本次不再提交。")
                self.checkpoint()
                if now_china() < plan.trigger:
                    raise FlowError("尚未达到配置的开放时间，已停止提交。")
                self.verify_draft(page, plan, seat)
                self.emit("log", f"正在提交 {seat}号座位，使用时段 {plan.interval}。")
                self.checkpoint()
                self.submission_started = True
                self.verification_waited = False
                try:
                    page.locator(".order_submit").click()
                except BrowserError:
                    # The click can reach the server even when the local wait times out.
                    self.emit("log", "提交动作未正常返回，先查询预约记录核对结果。")
                self.emit("phase", "正在核对预约结果")
                result = self.after_submit(page, plan, seat)
                if result != "REJECTED":
                    return result
                self.submission_started = False
                rejected_seats.add(seat)
                # Refresh the entire draft after an explicit rejection.
                break
            self.pause(2, page)
        return self.finish("UNAVAILABLE", "已完成 3 轮检查，候选座位没有预约成功。请调整座位或查看页面提示。")

    def run(self, settings: Settings, plan: Plan | None, *, preview: bool = False, login_only: bool = False) -> str:
        self.settings = settings
        settings.validate_account()
        self.entry_url = self._entry_override or SchoolEntry.parse(settings.entry_url).url
        try:
            with AccountLease(settings.account):
                return self._run(settings, plan, preview=preview, login_only=login_only)
        except AccountBusy as exc:
            return self.finish("ERROR", str(exc))

    def _run(self, settings: Settings, plan: Plan | None, *, preview: bool = False, login_only: bool = False) -> str:
        self.settings = settings
        settings.validate_account()
        if preview and plan is None:
            plan = settings.plan(for_preview=True)
        if not login_only and plan is None:
            raise ValueError("缺少预约计划。")
        if plan is not None and plan.room_id:
            return self.finish('MANUAL_REQUIRED', '指定房间编号的跨校预约请勾选协议模式，以核对准确房间和预约记录。')
        root = Path(__file__).resolve().parents[1]
        bundled = root / ".runtime" / "browsers"
        if bundled.is_dir():
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled))
        with sync_playwright() as playwright:
            browser = None
            try:
                self.emit("phase", "正在打开独立浏览器")
                self.checkpoint()
                browser = playwright.chromium.launch(headless=self.headless, args=["--window-size=1000,850"])
                # Fresh non-persistent context on every run: no cookie file or shared profile.
                self.context = browser.new_context(viewport={"width": 980, "height": 760}, locale="zh-CN", timezone_id="Asia/Shanghai",
                                                  service_workers=self.service_workers)
                self.context.set_default_timeout(8000)
                self.page = self.context.new_page()
                self.page.on("dialog", self._dialog)
                self.login(self.page)
                if login_only:
                    return self.finish("LOGIN_READY", "登录测试完成。可以在浏览器检查预约页面；本次不会提交预约。")
                if preview:
                    return self.preview_seats(self.page, plan)
                current = self.existing(self.read_records(), plan)
                if current:
                    return self.finish("EXISTING", f"目标日期已有有效预约：{current.floor} {current.seat}号座位，{current.interval}；本次不再提交。")
                self.open_list(self.page, plan)
                self.emit("log", plan.summary())
                self.emit("log", f"开放时间：{plan.trigger:%Y-%m-%d %H:%M:%S}（北京时间）。")
                self.wait_release(self.page, plan)
                return self.reserve(self.page, plan)
            except Stopped:
                if self.submission_started:
                    return self.finish("UNKNOWN", "已停止，但此前提交可能已经到达服务器，请重新登录查看预约记录后再决定是否重试。")
                return self.finish("STOPPED", "已停止，本次浏览器会话即将关闭。")
            except ManualRequired as exc:
                return self.finish("UNKNOWN" if self.submission_started else "MANUAL_REQUIRED", str(exc))
            except FlowError as exc:
                return self.finish("UNKNOWN" if self.submission_started else "ERROR", str(exc))
            except BrowserError as exc:
                if self.submission_started:
                    return self.finish("UNKNOWN", "提交后浏览器操作未完成，请核对预约记录；程序不会重复提交。")
                if self.stop.is_set():
                    return self.finish("STOPPED", "已停止，本次浏览器会话即将关闭。")
                self.emit("log", f"失败步骤：{self.stage}。{type(exc).__name__}: {str(exc).splitlines()[0]}")
                return self.finish("ERROR", f"“{self.stage}”没有完成，请查看运行记录和浏览器提示；如已点击提交，先检查预约记录。")
            except Exception as exc:
                return self.finish("UNKNOWN" if self.submission_started else "ERROR", f"程序遇到异常（{type(exc).__name__}），已停止；提交过的任务请核对预约记录。")
            finally:
                if self.keep_open and browser is not None and browser.is_connected() and self.page is not None:
                    self.emit("log", "浏览器保留供查看；点击“停止 / 关闭浏览器”可结束本次会话。")
                    try:
                        while not self.stop.is_set() and browser.is_connected() and not self.page.is_closed():
                            self.page.wait_for_timeout(200)
                    except BrowserError:
                        pass
                if self.context is not None:
                    try:
                        self.context.close()
                    except BrowserError:
                        pass
                if browser is not None:
                    try:
                        browser.close()
                    except BrowserError:
                        pass
                self.emit("closed", "浏览器已关闭，临时会话已清除。")
