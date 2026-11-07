"""Stateful local tests. No school accounts or real reservation requests are used."""
from __future__ import annotations

import html
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from seat_reserver.credentials import load_settings, save_settings
from seat_reserver.model import CHINA_TZ, Settings, floor_key, now_china, parse_record, parse_seats
from seat_reserver.runner import ReservationRunner


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.records = []
        self.submissions = []
        self.login_count = 0
        self.blocked = {"584"}
        self.reject_seat = ""
        self.commit = True
        self.tamper = False
        self.wrong_day = False
        self.bad_login = False
        self.closed_tomorrow = False
        self.maps = {"1F": ("01", "02", "003"), "2F": ("110", "120", "0007"), "3F": ("001", "061", "487", "584")}
        self.wrong_floor = False
        self.disabled_times = set()
        self.confirm_delay_ms = 0
        self.map_delay_ms = 0
        self.hidden_old_map = False
        self.hydration_delay_ms = 0
        self.floor_delay_ms = 0
        self.day_change_delay_ms = 0
        self.records_delay_ms = 0
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def entry(self):
        return f"http://127.0.0.1:{self.server_port}/front/third/apps/seat/index?fidEnc=fixture"

    def close(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_html(self, content, *, cookie=None):
        page = """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><style>
        body{font:16px sans-serif} .time_data{display:grid;grid-template-columns:repeat(4,1fr)}
        .time_cell{list-style:none;padding:5px;border:1px solid #ddd;cursor:pointer}
        .grid_cell{display:inline-block;padding:16px;cursor:pointer} .grid_checked{background:#aaf}
        .scr_sch,.order_submit{display:block;padding:16px;cursor:pointer;background:#eee}
        </style><body>""" + content + "</body></html>"
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, path, *, cookie=None):
        self.send_response(303)
        self.send_header("Location", path)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def login_form(self, error=""):
        self.send_html(f"""<h3>用户登录</h3><form action='/login' method='post'>
        <input id='phone' name='account' placeholder='手机号/超星号'>
        <input id='pwd' name='password' type='password' placeholder='学习通密码'>
        <button id='loginBtn'>登录</button></form><p>{error}</p>""")

    def do_GET(self):
        path = urlsplit(self.path).path
        if "fixture-auth=ok" not in (self.headers.get("Cookie") or ""):
            return self.login_form()
        if path.endswith("/index"):
            return self.send_html("""<button onclick="location.href='/front/third/apps/seat/list'">预约选座</button>
            <button onclick="location.href='/front/third/apps/seat/reserve/list?deptIdEnc=fixture'">预约记录</button>""")
        if path.endswith("/reserve/list"):
            cards = []
            for record in self.server.records:
                cards.append(f"""<div class='rec_per'><h6 class='rec_title'>{record['interval']}<span>{record['status']}</span></h6>
                <p class='rec_addr'>北辰校区图书馆<span>-{record['floor']}</span><span>-三楼自习室</span>
                <span class='rec_num'>座位<em>{record['seat']}</em></span></p><p class='rec_tip'>{record['day'].replace('-', '.')}</p></div>""")
            result = "".join(cards) if cards else "<p class='gnorecord'>无预约记录</p>"
            # Match the real page's initial Vue template and late data/empty state.
            return self.send_html("<nav><span>全部</span><span>待履约</span><span>已履约</span><span>已取消</span></nav>"
                + "<div id='records'><div class='rec_per'><h6 class='rec_title'>{{data.startTime}}<span>{{data.status}}</span></h6>"
                + "<p class='rec_addr'>{{data.room}}</p><p class='rec_tip'>{{data.date}}</p></div><p class='gnorecord'>无预约记录</p></div>"
                + f"<script>setTimeout(()=>{{document.querySelector('#records').innerHTML={json.dumps(result)};}}, {self.server.records_delay_ms});</script>")
        if path.endswith("/list"):
            today = now_china().date()
            tomorrow = today + timedelta(days=1)
            cards = "".join(f"""<li class='scr_list'><h6>北辰校区图书馆<span>-{floor}</span><span>-自习室</span></h6>
            <p class='scr_use'>座位数<span>{len(labels)}</span></p><span class='scr_sch' onclick="openFloor('{floor}')">选座</span></li>""" for floor, labels in self.server.maps.items())
            closed = "true" if self.server.closed_tomorrow else "false"
            dates = f"""<li class='active'><h6 class='mtab_time' onclick="selectDay(this,'{today}')">{today:%m-%d}</h6></li>
            <li><h6 class='mtab_time' onclick="selectDay(this,'{tomorrow}')">{tomorrow:%m-%d}</h6></li>"""
            pending = "<li class='scr_list'><h6>{{data.firstLevelName}}-{{data.secondLevelName}}</h6><p class='scr_use'>座位数<span>{{data.capacity}}</span></p><span class='scr_sch'></span></li>"
            return self.send_html(f"""<ul id='dates'></ul><ul id='floors'>{pending}</ul>
            <div id='blocked' style='display:none'>当前不在可预约时间范围内，今日22:30开放预约
            <button onclick="document.querySelector('#blocked').style.display='none'">确定</button></div>
            <script>let chosenDay='{today}',cards={json.dumps(cards)},pending={json.dumps(pending)};
            setTimeout(()=>{{document.querySelector('#dates').innerHTML={json.dumps(dates)};
            setTimeout(()=>{{document.querySelector('#floors').innerHTML=cards;}}, {self.server.floor_delay_ms});}}, {self.server.hydration_delay_ms});
            function selectDay(el,day){{chosenDay=day;
            document.querySelectorAll('#dates li').forEach(e=>e.className='');el.closest('li').className='active';
            document.querySelector('#floors').innerHTML=pending;
            setTimeout(()=>{{document.querySelector('#floors').innerHTML=cards;}}, {self.server.day_change_delay_ms});}}
            function openFloor(floor){{
            if({closed}&&chosenDay=='{tomorrow}'){{document.querySelector('#blocked').style.display='block';return;}}
            location.href='/front/third/apps/seat/select?day='+chosenDay+'&floor='+floor;}}</script>""")
        if path.endswith("/select"):
            query = parse_qs(urlsplit(self.path).query)
            chosen_day = query["day"][0]
            floor = query.get("floor", ["3F"])[0]
            if self.server.wrong_day and chosen_day != "2020-01-01":
                return self.redirect("/front/third/apps/seat/select?day=2020-01-01&floor=" + floor)
            cells = []
            for minute in range(480, 1320, 30):
                text = f"{minute // 60:02d}:{minute % 60:02d}-{(minute + 30) // 60:02d}:{(minute + 30) % 60:02d}"
                unavailable = " noSelect" if text in self.server.disabled_times else ""
                cells.append(f"<li class='time_cell{unavailable}' onclick='chooseTime(this)'>{text}</li>")
            seats = []
            for seat in self.server.maps[floor]:
                cls = "grid_lock" if seat in self.server.blocked else "grid_seat"
                seats.append(f"<div class='grid_cell {cls}' onclick='chooseSeat(this)'>{seat}</div>")
            tamper = "'099'" if self.server.tamper else "el.textContent.trim()"
            displayed_floor = "7F" if self.server.wrong_floor else floor
            old_map = "<div style='display:none'><div class='grid_cell grid_lock'>001</div><div class='grid_cell grid_lock'>999</div></div>" if self.server.hidden_old_map else ""
            return self.send_html(f"""<h6>北辰校区图书馆 -{displayed_floor} -自习室</h6><p>{chosen_day} 座位</p>
            <div class='o_use'><span>选择使用时间</span><em>请选择</em><div class='o_use_res'><span>未选择</span></div></div>
            <div id='picker'><span class='time_cal' onclick="document.querySelector('#picker').style.display='none'">取消</span>
            <span class='time_sure' onclick='finishTime()'>确认</span><ul class='time_data'>{''.join(cells)}</ul></div>
            <div id='seatMap' style='display:none'>{''.join(seats)}</div>{old_map}
            <p class='order_submit noClick' onclick='submitBooking()'>请选择时间和座位</p><p id='message'></p>
            <script>
            let startCell='',endCell='',interval='',seat='';
            function chooseTime(el){{if(el.classList.contains('noSelect'))return;if(!startCell) startCell=el.textContent; else endCell=el.textContent;}}
            function finishTime(){{if(!startCell)return; interval=startCell.split('-')[0]+'-'+(endCell||startCell).split('-')[1];
            setTimeout(()=>{{document.querySelector('.o_use>em').textContent=interval;document.querySelector('#picker').style.display='none';
            setTimeout(()=>{{document.querySelector('#seatMap').style.display='block';}}, {self.server.map_delay_ms});}}, {self.server.confirm_delay_ms});}}
            function chooseSeat(el){{if(el.classList.contains('grid_lock'))return;
            document.querySelectorAll('.grid_checked').forEach(x=>x.className='grid_cell grid_seat');
            el.className='grid_cell grid_checked';seat=el.textContent.trim();
            document.querySelector('.o_use_res').innerHTML='<label>'+{tamper}+'</label>';
            document.querySelector('.order_submit').textContent='提交';document.querySelector('.order_submit').className='order_submit';}}
            async function submitBooking(){{if(!seat||!interval)return;
            let response=await fetch('/submit',{{method:'POST',body:new URLSearchParams({{day:'{chosen_day}',floor:'{floor}',seat,interval}})}});
            let data=await response.json();document.querySelector('#message').textContent=data.message;}}
            </script>""")
        self.send_html("页面未找到")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
        values = {k: v[0] for k, v in parse_qs(body).items()}
        if self.path == "/login":
            self.server.login_count += 1
            if self.server.bad_login:
                return self.login_form("账号或密码错误")
            return self.redirect("/front/third/apps/seat/index?fidEnc=fixture", cookie="fixture-auth=ok; Path=/; HttpOnly; SameSite=Lax")
        if self.path == "/submit":
            self.server.submissions.append({**values, "timestamp": now_china().timestamp()})
            rejected = values.get("seat") == self.server.reject_seat
            if not rejected and self.server.commit:
                self.server.records.append({**values, "status": "待履约"})
            data = json.dumps({"message": "座位已被预约" if rejected else ""}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_html("错误请求")


class ModelTests(unittest.TestCase):
    def test_schedule_and_seat_normalization(self):
        settings = Settings(account="fixture-user", password="fixture-password", seats="061,61，584")
        plan = settings.plan(datetime(2026, 10, 10, 18, 0, tzinfo=CHINA_TZ))
        self.assertEqual(plan.seats, ("061", "584"))
        self.assertEqual(plan.day, date(2026, 10, 11))
        self.assertEqual(plan.trigger, datetime(2026, 10, 10, 22, 30, tzinfo=CHINA_TZ))
        self.assertEqual(settings.plan(datetime(2026, 10, 10, 23, 0, tzinfo=CHINA_TZ)).day, plan.day)

    def test_invalid_dates_and_half_hour_rules(self):
        settings = Settings(account="fixture-user", password="fixture-password", seats="584", start_time="08:15")
        with self.assertRaises(ValueError):
            settings.plan()
        settings.start_time = "08:00"
        settings.date_mode = "specific"
        settings.booking_date = "2020-01-01"
        with self.assertRaises(ValueError):
            settings.plan()
        for value in ("", "0", "584/../../file", "1,2,3,4,5,6,7,8,9"):
            with self.assertRaises(ValueError):
                parse_seats(value)

    def test_labels_keep_actual_length_and_candidate_order(self):
        self.assertEqual(parse_seats("2,01,0002,7"), ("2", "01", "7"))
        self.assertEqual(parse_seats("0007,7,110"), ("0007", "110"))

    def test_preview_plan_does_not_require_a_seat(self):
        settings = Settings(account="fixture-user", password="fixture-password", seats="")
        self.assertEqual(settings.plan(for_preview=True).seats, ())
        with self.assertRaises(ValueError):
            settings.plan()

    def test_old_settings_migrate_only_to_the_original_floor(self):
        settings = Settings(floor="2F", seats="110", floor_seats={floor_key("北辰校区图书馆", "1F"): "003"})
        self.assertEqual(settings.floor_seats[floor_key(settings.campus, "2F")], "110")
        self.assertNotIn(floor_key(settings.campus, "3F"), settings.floor_seats)

    def test_record_requires_date_floor_time_and_active_status(self):
        plan = Settings(account="fixture-user", password="fixture-password", seats="061", start_time="08:00", end_time="08:30").plan()
        record = parse_record("08:00-08:30 待履约", "北辰校区图书馆 -3F -三楼自习室 座位 061", plan.day.strftime("%Y.%m.%d"), "待履约")
        self.assertTrue(record.matches(plan, "061"))
        self.assertFalse(replace(record, floor="2F").matches(plan, "061"))
        self.assertFalse(replace(record, status="已取消").matches(plan, "061"))
        self.assertFalse(replace(record, interval="08:00-09:00").matches(plan, "061"))

    def test_dpapi_roundtrip_without_plaintext_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            settings = Settings(account="fixture-user-unique", password="fixture-password-unique", seats="584")
            save_settings(settings, path)
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn(settings.password, raw)
            self.assertNotIn(settings.account, raw)
            self.assertEqual(load_settings(path), settings)

    def test_runtime_logs_hide_credentials_and_token_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run.log"
            runner = ReservationRunner(lambda _: None, threading.Event(), log_path=path)
            runner.settings = Settings(account="private-account-991", password="private-password-882")
            runner.emit("log", "private-account-991 private-password-882 https://example.test/path?token=never-log-this")
            result = path.read_text(encoding="utf-8")
            for value in ("private-account-991", "private-password-882", "never-log-this"):
                self.assertNotIn(value, result)
            self.assertIn("已隐藏", result)


class BrowserFlowTests(unittest.TestCase):
    def setUp(self):
        self.site = FixtureServer()
        self.events = []
        self.settings = Settings(account="fixture-user", password="fixture-password", seats="584,061", start_time="08:00", end_time="08:30")
        self.plan = replace(self.settings.plan(), trigger=now_china() - timedelta(seconds=1))

    def tearDown(self):
        self.site.close()

    def run_job(self, *, login_only=False, preview=False, emit=None, stop=None, plan=None, runner_class=ReservationRunner):
        runner = runner_class(emit or self.events.append, stop or threading.Event(), entry_url=self.site.entry,
                                   headless=True, keep_open=False, test_site=True, verify_delays=(0.05, 0.1, 0.2))
        return runner.run(self.settings, plan or self.plan, login_only=login_only, preview=preview)

    def test_waits_for_release_and_falls_back_to_available_seat(self):
        plan = replace(self.plan, trigger=now_china() + timedelta(seconds=3))
        self.assertEqual(self.run_job(plan=plan), "SUCCESS")
        self.assertEqual(len(self.site.submissions), 1)
        self.assertEqual(self.site.submissions[0]["seat"], "061")
        self.assertGreaterEqual(self.site.submissions[0]["timestamp"], plan.trigger.timestamp())

    def test_ambiguous_response_is_verified_without_repeating_submit(self):
        self.assertEqual(self.run_job(), "SUCCESS")
        self.assertEqual(len(self.site.submissions), 1)

    def test_unconfirmed_result_stops_after_one_submission(self):
        self.site.commit = False
        self.assertEqual(self.run_job(), "UNKNOWN")
        self.assertEqual(len(self.site.submissions), 1)

    def test_existing_reservation_prevents_any_submission(self):
        self.site.records = [{"day": self.plan.day.isoformat(), "floor": "2F", "seat": "061", "interval": self.plan.interval, "status": "待履约"}]
        self.assertEqual(self.run_job(), "EXISTING")
        self.assertEqual(self.site.submissions, [])

    def test_cancelled_record_is_not_mistaken_for_success(self):
        self.site.records = [{"day": self.plan.day.isoformat(), "floor": "3F", "seat": "061", "interval": self.plan.interval, "status": "已取消"}]
        self.assertEqual(self.run_job(), "SUCCESS")
        self.assertEqual(len(self.site.submissions), 1)

    def test_mismatched_draft_blocks_submission(self):
        self.site.tamper = True
        self.assertEqual(self.run_job(), "ERROR")
        self.assertEqual(self.site.submissions, [])

    def test_explicit_rejection_tries_next_candidate(self):
        self.site.blocked.clear()
        self.site.reject_seat = "584"
        self.assertEqual(self.run_job(), "SUCCESS")
        self.assertEqual([x["seat"] for x in self.site.submissions], ["584", "061"])

    def test_bad_password_is_not_repeated(self):
        self.site.bad_login = True
        self.assertEqual(self.run_job(login_only=True), "ERROR", self.events)
        self.assertEqual(self.site.login_count, 1, self.events)
        self.assertEqual(self.site.submissions, [])

    def test_login_only_and_fresh_session_on_each_run(self):
        self.assertEqual(self.run_job(login_only=True), "LOGIN_READY")
        self.assertEqual(self.run_job(login_only=True), "LOGIN_READY")
        self.assertEqual(self.site.login_count, 2)
        self.assertEqual(self.site.submissions, [])

    def test_preview_reaches_requested_floor_without_submission(self):
        self.settings.floor = "2F"
        self.settings.seats = ""
        plan = self.settings.plan(for_preview=True)
        self.assertEqual(self.run_job(preview=True, plan=plan), "PREVIEW_READY")
        catalog = next(e for e in self.events if e["kind"] == "catalog")
        self.assertEqual(catalog["floor"], "2F")
        self.assertEqual(set(catalog["labels"]), set(self.site.maps["2F"]))
        self.assertNotIn("584", catalog["labels"])
        self.assertEqual(self.site.submissions, [])

    def test_preview_waits_for_vue_dates_and_floor_data(self):
        self.site.hydration_delay_ms = 450
        self.site.floor_delay_ms = 450
        self.assertEqual(self.run_job(preview=True), "PREVIEW_READY")
        self.assertEqual(next(e for e in self.events if e["kind"] == "catalog")["floor"], "3F")
        self.assertEqual(self.site.submissions, [])

    def test_reservation_waits_for_floor_reload_after_date_change(self):
        self.site.day_change_delay_ms = 450
        self.assertEqual(self.run_job(), "SUCCESS")
        self.assertEqual(len(self.site.submissions), 1)
        self.assertEqual(self.site.submissions[0]["day"], self.plan.day.isoformat())

    def test_delayed_existing_record_is_not_mistaken_for_an_empty_list(self):
        self.site.records_delay_ms = 550
        self.site.records = [{"day": self.plan.day.isoformat(), "floor": "2F", "seat": "110", "interval": self.plan.interval, "status": "待履约"}]
        self.assertEqual(self.run_job(), "EXISTING")
        self.assertEqual(self.site.submissions, [])

    def test_delayed_empty_record_list_is_waited_for_before_reserving(self):
        self.site.records_delay_ms = 350
        self.assertEqual(self.run_job(), "SUCCESS")
        self.assertEqual(len(self.site.submissions), 1)

    def test_closed_tomorrow_preview_reads_today_without_submission(self):
        self.site.closed_tomorrow = True
        clock = now_china().replace(hour=19, minute=45, second=0, microsecond=0)
        with patch("seat_reserver.runner.now_china", return_value=clock):
            self.assertEqual(self.run_job(preview=True), "PREVIEW_READY")
        catalog = next(e for e in self.events if e["kind"] == "catalog")
        self.assertEqual(catalog["day"], now_china().date().isoformat())
        self.assertEqual(self.site.submissions, [])
        self.assertTrue(any("明日尚未开放" in e["text"] for e in self.events if e["kind"] == "result"))

    def test_preview_confirms_available_time_when_tomorrows_time_is_past_today(self):
        self.site.closed_tomorrow = True
        self.site.disabled_times = {f"{m // 60:02d}:{m % 60:02d}-{(m + 30) // 60:02d}:{(m + 30) % 60:02d}" for m in range(480, 1200, 30)}
        clock = now_china().replace(hour=19, minute=45, second=0, microsecond=0)
        with patch("seat_reserver.runner.now_china", return_value=clock):
            self.assertEqual(self.run_job(preview=True), "PREVIEW_READY")
        catalog = next(e for e in self.events if e["kind"] == "catalog")
        self.assertEqual(catalog["interval"], "20:00-20:30")
        self.assertEqual((self.settings.start_time, self.settings.end_time), ("08:00", "08:30"))
        self.assertEqual(self.site.submissions, [])

    def test_today_preview_preserves_a_selectable_requested_interval(self):
        self.site.closed_tomorrow = True
        self.settings.start_time, self.settings.end_time = "20:00", "21:30"
        plan = self.settings.plan(for_preview=True)
        clock = now_china().replace(hour=19, minute=45, second=0, microsecond=0)
        with patch("seat_reserver.runner.now_china", return_value=clock):
            self.assertEqual(self.run_job(preview=True, plan=plan), "PREVIEW_READY")
        self.assertEqual(next(e for e in self.events if e["kind"] == "catalog")["interval"], "20:00-21:30")
        self.assertEqual(self.site.submissions, [])

    def test_preview_waits_for_confirmation_then_map_loading(self):
        self.site.confirm_delay_ms = 350
        self.site.map_delay_ms = 650
        self.assertEqual(self.run_job(preview=True), "PREVIEW_READY")
        self.assertEqual(next(e for e in self.events if e["kind"] == "catalog")["interval"], self.plan.interval)
        self.assertEqual(self.site.submissions, [])

    def test_hidden_old_map_does_not_contaminate_catalog(self):
        self.site.hidden_old_map = True
        self.assertEqual(self.run_job(preview=True), "PREVIEW_READY")
        labels = next(e for e in self.events if e["kind"] == "catalog")["labels"]
        self.assertEqual(set(labels), set(self.site.maps["3F"]))
        self.assertNotIn("999", labels)
        self.assertEqual(self.site.submissions, [])

    def test_no_available_time_does_not_cancel_and_read_an_unconfirmed_map(self):
        self.site.disabled_times = {f"{m // 60:02d}:{m % 60:02d}-{(m + 30) // 60:02d}:{(m + 30) % 60:02d}" for m in range(480, 1320, 30)}
        self.assertEqual(self.run_job(preview=True), "ERROR")
        self.assertFalse(any(e["kind"] == "catalog" for e in self.events))
        self.assertEqual(self.site.submissions, [])

    def test_reservation_cannot_read_or_submit_without_confirmed_time(self):
        class SkippedConfirmation(ReservationRunner):
            def choose_interval(self, page, plan):
                pass
        self.assertEqual(self.run_job(runner_class=SkippedConfirmation), "ERROR")
        self.assertFalse(any(e["kind"] == "catalog" for e in self.events))
        self.assertEqual(self.site.submissions, [])

    def test_preview_selects_candidate_but_never_submits(self):
        self.assertEqual(self.run_job(preview=True), "PREVIEW_READY")
        self.assertTrue(any("已测试选中 061" in e["text"] for e in self.events if e["kind"] == "result"))
        self.assertEqual(self.site.submissions, [])

    def test_resolves_seat_label_from_the_selected_floor(self):
        self.settings.floor = "2F"
        self.settings.seats = "7"
        plan = replace(self.settings.plan(), trigger=now_china() - timedelta(seconds=1))
        self.assertEqual(self.run_job(plan=plan), "SUCCESS")
        self.assertEqual(self.site.submissions[0]["floor"], "2F")
        self.assertEqual(self.site.submissions[0]["seat"], "0007")

    def test_seat_only_present_on_another_floor_is_not_submitted(self):
        self.settings.floor = "1F"
        self.settings.seats = "584"
        plan = replace(self.settings.plan(), trigger=now_china() - timedelta(seconds=1))
        self.assertEqual(self.run_job(plan=plan), "ERROR")
        self.assertEqual(self.site.submissions, [])

    def test_duplicate_numeric_labels_fail_closed(self):
        self.site.maps["3F"] = ("061", "61")
        self.assertEqual(self.run_job(), "ERROR")
        self.assertEqual(self.site.submissions, [])

    def test_wrong_floor_or_day_prevents_submission(self):
        self.site.wrong_floor = True
        self.assertEqual(self.run_job(), "ERROR")
        self.site.wrong_floor = False
        self.site.wrong_day = True
        self.assertEqual(self.run_job(), "ERROR")
        self.assertEqual(self.site.submissions, [])

    def test_stop_during_countdown_does_not_submit(self):
        stop = threading.Event()
        def emit(event):
            self.events.append(event)
            if event["kind"] == "phase" and event["text"] == "等待开放预约":
                stop.set()
        plan = replace(self.plan, trigger=now_china() + timedelta(minutes=2))
        self.assertEqual(self.run_job(emit=emit, stop=stop, plan=plan), "STOPPED")
        self.assertEqual(self.site.submissions, [])

    def test_stop_after_submit_reports_uncertain_result(self):
        stop = threading.Event()
        def emit(event):
            self.events.append(event)
            if event["kind"] == "phase" and event["text"] == "正在核对预约结果":
                stop.set()
        self.assertEqual(self.run_job(emit=emit, stop=stop), "UNKNOWN")
        self.assertLessEqual(len(self.site.submissions), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
