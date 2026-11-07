"""Cross-school checks. Only dummy accounts and in-memory HTTP are used."""
from __future__ import annotations

import copy
import json
import tempfile
import threading
import tkinter as tk
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs
from unittest.mock import patch

import httpx

from .batch import BatchRunner
from .batch_gui import ProfileEditor
from .batch_model import BatchSettings, Profile, load_batch, save_batch
from .credentials import load_settings, save_settings
from .gui import SeatApp
from .model import CHINA_TZ, ENTRY_URL, Settings, now_china, parse_record
from .protocol_api import ProtocolAPI, ProtocolQueryError
from .protocol_runner import ProtocolRunner, booking_window, validate_rules
from .protocol_session import ProtocolError, VerificationRequired, cache_path
from .runner import ReservationRunner
from .school_catalog import read_school_catalog
from .schools import SchoolEntry, binding_prompt
from .school_gui import SchoolDialog, SCHOOL_CHOICES
from .test_protocol_api import plan, responses
from .test_protocol_runner import FixtureSigner


def school_settings(index=1):
    return Settings(account=f'dummy-school-user-{index}', password='dummy-only-password',
                    school_name=f'模拟学校{index}',
                    entry_url=f'https://office.chaoxing.com/front/third/apps/seat/index?fidEnc=dummy{index}&mappId={index}',
                    campus='东馆', floor='阅览区', third_level='自习室A', room_id=12, seats='3',
                    days_ahead=7, release_days_before=2, release_time='07:30', start_time='06:00',
                    end_time='07:00', time_unit=15, autostart=False)


class SchoolConfigTests(unittest.TestCase):
    def test_new_account_requires_entry_before_any_network_or_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = load_settings(Path(folder) / 'not-yet-saved.json')
        self.assertEqual((settings.school_name, settings.entry_url, settings.campus, settings.floor), ('', '', '', ''))
        self.assertEqual(Profile().settings.entry_url, '')
        self.assertFalse(settings.autostart)
        settings.account, settings.password = 'dummy-new', 'dummy-password'
        with patch('seat_reserver.runner.sync_playwright') as browser, patch(
                'seat_reserver.protocol_runner.ProtocolRunner._new_client') as client:
            with self.assertRaisesRegex(ValueError, '尚未设置学校预约入口'):
                read_school_catalog(settings)
            with self.assertRaisesRegex(ValueError, '尚未设置学校预约入口'):
                ProtocolRunner(lambda _: None, threading.Event())._run(settings, None, login_only=True)
            browser.assert_not_called()
            client.assert_not_called()

    def test_renaming_hebut_does_not_silently_reuse_its_entry(self):
        settings = Settings(account='dummy', password='dummy', school_name='其他学校')
        with self.assertRaisesRegex(ValueError, '链接仍属于河北工业大学'):
            settings.validate_account()
        with self.assertRaisesRegex(ValueError, '学校名称改为'):
            replace(school_settings(), school_name='河北工业大学').validate_account()

    def test_legacy_saved_account_retains_original_school(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            save_settings(Settings(account='dummy-legacy', password='dummy-password'), path)
            payload = json.loads(path.read_text(encoding='utf-8'))
            payload['settings'].pop('school_name')
            payload['settings'].pop('entry_url')
            path.write_text(json.dumps(payload), encoding='utf-8')
            restored = load_settings(path)
            restored.validate_account()
        self.assertEqual(restored.entry_url, ENTRY_URL)
        self.assertEqual(restored.school_name, '河北工业大学')

    def test_school_page_match_rejects_other_application_and_duplicate_identifiers(self):
        entry = SchoolEntry.parse(school_settings().entry_url)
        self.assertTrue(entry.matches_page(entry.url))
        self.assertTrue(entry.matches_page(entry.url.replace('/index?', '/list?').split('&mappId')[0]))
        for value in (ENTRY_URL, entry.url + '&fidEnc=dummy1', entry.url + '#fragment',
                      entry.url.replace('mappId=1', 'mappId=99')):
            with self.subTest(value=value):
                self.assertFalse(entry.matches_page(value))

    def test_binding_messages_distinguish_required_and_completed_identity(self):
        for text in ('请先绑定学号/工号', '需要认证单位身份', '学号未绑定', '绑定您的工号'):
            self.assertTrue(binding_prompt(text), text)
        for text in ('无需绑定学号', '不用认证单位身份', '已绑定工号', '学号：123456', '已认证单位身份'):
            self.assertFalse(binding_prompt(text), text)

    def test_failed_identity_never_caches_or_queries_rooms(self):
        for payload in ({'success': False, 'msg': '请先绑定学号'}, {'success': False}, []):
            calls = []
            def handle(request):
                calls.append(request)
                return httpx.Response(200, json=payload)
            with self.subTest(payload=payload), patch('seat_reserver.protocol_runner.ProtocolSession'), patch(
                    'seat_reserver.protocol_runner.save_session') as cache:
                with self.assertRaises(VerificationRequired if isinstance(payload, dict) and payload.get('msg') else ProtocolError):
                    read_school_catalog(school_settings(), client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handle)))
                cache.assert_not_called()
            self.assertEqual([r.url.path for r in calls], ['/data/apps/seat/identity/verify'])

    def test_entry_normalization_preserves_school_and_application(self):
        source = 'https://office.chaoxing.com/front/third/apps/seat/list?deptIdEnc=dummy& mappId=7'.replace('& ', '&')
        entry = SchoolEntry.parse(source)
        self.assertEqual(entry.identity_parameters, {'fidEnc': 'dummy', 'mappId': '7'})
        self.assertEqual(entry.url, 'https://office.chaoxing.com/front/third/apps/seat/index?fidEnc=dummy&mappId=7')

    def test_foreign_hosts_wrong_paths_and_ambiguous_identifiers_are_rejected(self):
        for value in ('http://office.chaoxing.com/front/third/apps/seat/index?fidEnc=x&mappId=1',
                      ENTRY_URL.replace('office.chaoxing.com', 'office.chaoxing.com.example.org'),
                      ENTRY_URL.replace('office.chaoxing.com', 'user@office.chaoxing.com'),
                      ENTRY_URL.replace('office.chaoxing.com', 'office.chaoxing.com:444'),
                      ENTRY_URL.replace('/index', '/cancel'), ENTRY_URL + '&fidEnc=other',
                      ENTRY_URL + '&deptIdEnc=other', ENTRY_URL.split('&mappId')[0], ENTRY_URL + '#secret'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SchoolEntry.parse(value)

    def test_other_school_hours_days_and_non_numeric_floor_are_configurable(self):
        settings = school_settings()
        now = datetime(2026, 10, 27, 12, tzinfo=CHINA_TZ)
        settings.date_mode = 'specific'
        settings.booking_date = '2026-10-30'
        target = settings.plan(now)
        self.assertEqual(target.trigger, datetime(2026, 10, 28, 7, 30, tzinfo=CHINA_TZ))
        self.assertEqual(target.start, time(6))
        self.assertEqual(target.room_id, 12)
        self.assertEqual(target.time_unit, 15)
        self.assertIn('自习室A', target.summary())
        self.assertEqual(target.time_cells, ('06:00-06:15', '06:45-07:00'))

    def test_school_fields_roundtrip_and_candidates_cannot_leak_between_schools(self):
        first, second = school_settings(), school_settings(2)
        self.assertNotEqual(first.location_key(), second.location_key())
        config = BatchSettings([Profile(settings=first), Profile(settings=second)], use_profile_release=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'batch.json'
            save_batch(config, path)
            self.assertEqual(load_batch(path), config)
            self.assertNotIn('dummy-only-password', path.read_text(encoding='utf-8'))

    def test_mixed_school_jobs_use_each_release_time_and_preserve_global_option(self):
        first, second = school_settings(), school_settings(2)
        second.release_time = '19:45'
        second.release_days_before = 0
        config = BatchSettings([Profile(settings=first), Profile(settings=second)], use_profile_release=True)
        jobs = config.jobs('reserve', datetime(2026, 10, 27, 12, tzinfo=CHINA_TZ))
        self.assertEqual(jobs[0][1].trigger, datetime(2026, 10, 26, 7, 30, tzinfo=CHINA_TZ))
        self.assertEqual(jobs[1][1].trigger, datetime(2026, 10, 28, 19, 45, tzinfo=CHINA_TZ))
        config.use_profile_release = False
        jobs = config.jobs('reserve', datetime(2026, 10, 27, 12, tzinfo=CHINA_TZ))
        self.assertEqual(jobs[0][1].trigger.time(), time(22, 30))
        self.assertEqual(first.release_time, '07:30')

    def test_cache_scopes_do_not_share_same_accounts_school_context(self):
        self.assertNotEqual(cache_path('dummy', school_settings().entry_url), cache_path('dummy', school_settings(2).entry_url))

    def test_identity_verification_uses_each_school_application_and_scoped_cache(self):
        for settings in (school_settings(), school_settings(2)):
            calls = []
            def handle(request):
                calls.append(request)
                return httpx.Response(200, json={'success': True})
            with httpx.Client(transport=httpx.MockTransport(handle)) as client, patch(
                    'seat_reserver.protocol_runner.ProtocolSession') as session, patch(
                    'seat_reserver.protocol_runner.save_session') as save:
                runner = ProtocolRunner(lambda _: None, threading.Event())
                runner.settings, runner.entry_url = settings, settings.entry_url
                runner._authenticate(client, {})
                session.assert_called_once_with(client, settings.entry_url)
                self.assertEqual(dict(calls[0].url.params), SchoolEntry.parse(settings.entry_url).identity_parameters)
                self.assertEqual(save.call_args.kwargs['scope'], settings.entry_url)

    def test_generic_browser_record_keeps_third_level(self):
        record = parse_record('06:00-07:00 待履约', '东馆 - 阅览区 - 自习室A 座位 003', '2026.10.28', '待履约')
        self.assertIsNotNone(record)
        self.assertEqual((record.campus, record.floor, record.third_level), ('东馆', '阅览区', '自习室A'))


class SchoolAPITests(unittest.TestCase):
    def setUp(self):
        self.payloads = responses()
        self.requests = []
        def handle(request):
            self.requests.append(request)
            return httpx.Response(200, json={'success': True, 'data': self.payloads[request.url.path]})
        self.client = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(self.client.close)
        self.api = ProtocolAPI(self.client, 'dummy-school', record_status_names={101: '待履约'})

    def test_room_region_or_id_must_disambiguate_same_floor(self):
        rows = self.payloads['/data/apps/seat/room/list']['seatRoomList']
        rows[0]['thirdLevelName'] = 'A区'
        rows.append(dict(rows[0], id=13, thirdLevelName='B区'))
        self.payloads['/data/apps/seat/room/list']['totalRow'] = 2
        with self.assertRaises(ProtocolQueryError):
            self.api.select_room(plan())
        self.assertEqual(self.api.select_room(replace(plan(), third_level='B区')).id, 13)
        self.assertEqual(self.api.select_room(replace(plan(), room_id=12)).third_level, 'A区')
        with self.assertRaises(ProtocolQueryError):
            self.api.select_room(replace(plan(), third_level='A区', room_id=13))

    def test_catalog_queries_all_campuses_and_does_not_guess_names(self):
        rows = self.payloads['/data/apps/seat/room/list']['seatRoomList']
        rows.append(dict(rows[0], id=13, firstLevelName='东馆', secondLevelName='研讨室'))
        self.payloads['/data/apps/seat/room/list']['totalRow'] = 2
        rooms = self.api.list_rooms(plan().day)
        self.assertEqual(len(rooms), 2)
        self.assertEqual(self.requests[0].url.params['firstLevelName'], '')
        self.assertEqual(rooms[1].floor, '研讨室')

    def test_record_verification_requires_exact_room_and_region(self):
        row = self.payloads['/data/apps/seat/reservelist']['reserveList'][0]
        row.update(firstLevelName='东馆', secondLevelName='阅览区', thirdLevelName='自习室A', roomId=12)
        target = replace(plan(), campus='东馆', floor='阅览区', third_level='自习室A', room_id=12)
        record = self.api.records()[0]
        self.assertTrue(record.matches(target, '3'))
        self.assertFalse(record.matches(replace(target, third_level='自习室B'), '3'))
        self.assertFalse(record.matches(replace(target, room_id=13), '3'))

    def test_school_time_grid_is_checked_against_actual_room(self):
        config = self.payloads['/data/apps/seat/room/info']['seatConfig']
        config['timeUnit'] = 15
        info = SimpleNamespace(config=config, open_start=time(8, 10), common_pause_times=(), seat_interval_map={})
        target = replace(plan(), start=time(20, 10), end=time(21, 10), time_unit=15)
        validate_rules(info, target)
        with self.assertRaises(ProtocolError):
            validate_rules(info, replace(target, start=time(20, 15), end=time(21, 15)))

    def test_room_hours_can_start_before_eight(self):
        info_data = self.payloads['/data/apps/seat/room/info']
        info_data['seatRoom']['startTime'] = -7_200_000
        room = self.api.select_room(plan())
        info = self.api.room_info(replace(plan(), start=time(6), end=time(7)), room)
        self.assertEqual(info.open_start, time(6))

    def test_server_opening_and_deadline_are_enforced(self):
        target = plan()
        info = SimpleNamespace(config={'reserveBeforeDay': 2, 'reserveBeforeTime': '07:30',
                                       'reserveAfterDay': 0, 'reserveAfterTime': '19:00'})
        opening = booking_window(info, target, 0)
        self.assertEqual(datetime.fromtimestamp(opening/1000, CHINA_TZ), datetime(2026, 10, 10, 7, 30, tzinfo=CHINA_TZ))
        with self.assertRaisesRegex(ProtocolError, '截止'):
            booking_window(info, target, int(datetime(2026, 10, 12, 19, tzinfo=CHINA_TZ).timestamp()*1000))

    def test_read_only_catalog_returns_no_identity_or_submission(self):
        self.payloads['/data/apps/seat/config']['seatConfig'].update(reserveBeforeDay=2, reserveBeforeTime='07:30', timeUnit=15,
                                                                    privateToken='must-not-export')
        def authenticate(runner, client, state):
            self.assertEqual(runner.entry_url, school_settings().entry_url)
        with patch.object(ProtocolRunner, '_authenticate', authenticate):
            catalog = read_school_catalog(school_settings(), client_factory=lambda: self.client)
        self.assertEqual(catalog['submission_attempts'], 0)
        self.assertNotIn('must-not-export', json.dumps(catalog))
        self.assertEqual([r.url.path for r in self.requests], ['/data/apps/seat/room/list', '/data/apps/seat/config'])


class SchoolGuiTests(unittest.TestCase):
    def test_other_school_selection_clears_inherited_entry_and_blocks_query(self):
        root = tk.Tk()
        dialog = SchoolDialog(root, Settings(account='dummy', password='dummy-password'), lambda _: None)
        try:
            dialog.school_choice.set(SCHOOL_CHOICES[2])
            dialog.choose_school()
            for name in ('entry_url', 'school_name', 'campus', 'floor', 'room_id', 'release_time', 'time_unit'):
                self.assertEqual(dialog.vars[name].get(), '', name)
            with patch.object(dialog, 'work') as work:
                dialog.query()
                work.assert_not_called()
                self.assertIsNone(dialog.worker)
                self.assertIn('尚未设置学校预约入口', dialog.status.get())
            dialog.school_choice.set(SCHOOL_CHOICES[1])
            dialog.choose_school()
            self.assertEqual(dialog.collect().entry_url, ENTRY_URL)
            self.assertEqual(dialog.vars['release_time'].get(), '22:30')
        finally:
            dialog.close()
            root.destroy()

    def test_manual_entry_change_discards_previous_room_and_catalog(self):
        root = tk.Tk()
        dialog = SchoolDialog(root, school_settings(), lambda _: None)
        try:
            dialog.catalog = {'rooms': []}
            dialog.catalog_entry = school_settings().entry_url
            dialog.vars['entry_url'].set(school_settings(2).entry_url)
            self.assertIsNone(dialog.catalog)
            self.assertIsNone(dialog.catalog_entry)
            self.assertEqual(dialog.vars['campus'].get(), '')
            self.assertEqual(dialog.vars['room_id'].get(), '')
            self.assertEqual(dialog.vars['release_time'].get(), '')
        finally:
            dialog.close()
            root.destroy()

    def test_new_profile_editor_can_open_unconfigured_school_dialog(self):
        root = tk.Tk()
        editor = ProfileEditor(root, Profile(settings=Settings.new_account()), lambda _: None)
        try:
            self.assertIn('未设置学校', editor.school_text.get())
            editor.edit_school()
            dialog = editor.school_dialog
            self.assertEqual(dialog.school_choice.get(), SCHOOL_CHOICES[0])
            self.assertEqual(dialog.vars['entry_url'].get(), '')
            dialog.window.geometry('820x760')
            root.update()
            self.assertLessEqual(dialog.apply_button.winfo_rooty() + dialog.apply_button.winfo_height(),
                                 dialog.window.winfo_rooty() + dialog.window.winfo_height())
        finally:
            editor.close()

    def test_switching_school_clears_seats_then_restores_only_own_choices(self):
        root = tk.Tk()
        try:
            app = SeatApp(root, preview=True)
            app.vars['seats'].set('061')
            app.apply_school(school_settings())
            self.assertEqual(app.vars['seats'].get(), '')
            app.vars['seats'].set('3')
            app.apply_school(school_settings(2))
            self.assertEqual(app.vars['seats'].get(), '')
            app.apply_school(school_settings())
            self.assertEqual(app.vars['seats'].get(), '3')
            self.assertEqual(app.collect().room_id, 12)
        finally:
            app.close()

    def test_profile_editor_preserves_school_fields_and_release_time(self):
        root = tk.Tk()
        committed = []
        try:
            editor = ProfileEditor(root, Profile(settings=school_settings()), committed.append)
            editor.vars['release_time'].set('09:00')
            with patch.object(editor, 'close'):
                editor.save()
            self.assertEqual(committed[0].settings.entry_url, school_settings().entry_url)
            self.assertEqual(committed[0].settings.release_time, '09:00')
            self.assertEqual(committed[0].settings.time_unit, 15)
        finally:
            editor.close()

    def test_editing_floor_clears_previously_selected_room_identity(self):
        root = tk.Tk()
        try:
            app = SeatApp(root, preview=True)
            app.apply_school(school_settings())
            app.vars['floor'].set('新区域')
            settings = app.collect()
            self.assertEqual(settings.room_id, 0)
            self.assertEqual(settings.third_level, '')
        finally:
            app.close()

    def test_school_dialog_query_fills_exact_room_without_booking(self):
        root = tk.Tk()
        try:
            app = SeatApp(root, preview=True)
            app.vars['account'].set('dummy-query')
            app.vars['password'].set('dummy-password')
            app.edit_school()
            dialog = app.school_dialog
            dialog.catalog = {'rooms': [{'id': 12, 'campus': '东馆', 'floor': '阅览区', 'third_level': 'A区', 'capacity': 30}],
                              'default_rules': {'reserveBeforeDay': 2, 'reserveBeforeTime': '07:30', 'timeUnit': 15}}
            dialog.table.insert('', 'end', iid='12', values=(12, '东馆', '阅览区', 'A区', 30))
            dialog.table.selection_set('12')
            dialog.select_room()
            self.assertEqual(dialog.vars['room_id'].get(), '12')
            self.assertEqual(dialog.vars['release_days_before'].get(), '2')
            root.update()
            self.assertLessEqual(dialog.apply_button.winfo_rooty()+dialog.apply_button.winfo_height(),
                                 dialog.window.winfo_rooty()+dialog.window.winfo_height())
            dialog.apply()
            self.assertEqual(app.collect().third_level, 'A区')
            self.assertIsNone(app.worker)
        finally:
            app.close()


class SchoolBrowserLoginTests(unittest.TestCase):
    """Intercept every browser request; no school site or real credentials are used."""
    def run_login(self, *, binding=False, wrong_redirect=False, hidden_binding=False):
        settings = replace(school_settings(), room_id=0)
        requests, events = [], []
        authenticated = False
        selected = settings.entry_url

        def handle(route):
            nonlocal authenticated
            request = route.request
            requests.append(request)
            if request.url == selected and (not authenticated or wrong_redirect):
                target = ENTRY_URL if wrong_redirect else 'https://passport2.chaoxing.com/login'
                route.fulfill(content_type='text/html', body=f'<script>location.replace({json.dumps(target)});</script>')
            elif request.url == selected:
                body = '<p>请绑定您的学号/工号</p>' if binding else '<button>预约选座</button>'
                if hidden_binding:
                    body += '<div style="display:none">请绑定学号</div><script>const prompt="请绑定工号";</script>'
                route.fulfill(content_type='text/html; charset=utf-8', body=body)
            elif request.url == 'https://passport2.chaoxing.com/login':
                route.fulfill(content_type='text/html; charset=utf-8', body=f'''<input id="phone" name="account">
                    <input id="pwd" name="password" type="password">
                    <button id="loginBtn" onclick='fetch("/fixture-login", {{method: "POST"}}).then(() => {{location.href={json.dumps(ENTRY_URL)};}})'>登录</button>''')
            elif request.url == 'https://passport2.chaoxing.com/fixture-login':
                self.assertEqual(request.method, 'POST')
                authenticated = True
                route.fulfill(content_type='application/json', body='{"status":true}')
            elif request.url == ENTRY_URL:
                # A stale callback can land on another school's binding prompt.
                body = '<p>请先绑定工号</p>' if wrong_redirect else '<button>预约选座</button>'
                route.fulfill(content_type='text/html; charset=utf-8', body=body)
            else:
                route.fulfill(status=404, body='Unknown fixture URL')

        class RoutedRunner(ReservationRunner):
            def login(self, page):
                self.context.route('**/*', handle)
                self.context.set_offline(True)
                return super().login(page)

        runner = RoutedRunner(events.append, threading.Event(), headless=True, keep_open=False,
                              service_workers='block')
        code = runner.run(settings, None, login_only=True)
        return code, requests, events

    def test_browser_stale_callback_returns_to_selected_school_and_ignores_hidden_template(self):
        code, requests, events = self.run_login(hidden_binding=True)
        self.assertEqual(code, 'LOGIN_READY', events)
        self.assertEqual(requests[-1].url, school_settings().entry_url)
        self.assertEqual(sum(r.method == 'POST' for r in requests), 1)

    def test_browser_selected_school_binding_stops_after_one_login_without_submission(self):
        code, requests, events = self.run_login(binding=True)
        self.assertEqual(code, 'MANUAL_REQUIRED', events)
        self.assertEqual(sum(r.method == 'POST' for r in requests), 1)
        self.assertTrue(any('核对当前预约入口' in e['text'] for e in events))
        self.assertFalse(any('/data/apps/seat/' in r.url for r in requests))

    def test_browser_wrong_school_entry_does_not_report_login_success(self):
        code, requests, events = self.run_login(wrong_redirect=True)
        self.assertEqual(code, 'ERROR', events)
        self.assertTrue(any('学校入口不一致' in e['text'] for e in events))
        self.assertFalse(any(r.method == 'POST' for r in requests))


class MixedSchoolHTTPTests(unittest.TestCase):
    def test_two_school_workers_send_to_their_own_departments_and_verify_once(self):
        workers = []
        settings = [school_settings(1), school_settings(2)]
        class Worker(ProtocolRunner):
            def __init__(self, *args, **kwargs):
                self.saved = []
                self.sent = []
                super().__init__(*args, session_state={}, signer_factory=FixtureSigner,
                                 client_factory=lambda: httpx.Client(transport=httpx.MockTransport(self.handle)), **kwargs)
                workers.append(self)

            def _authenticate(self, client, state):
                client.cookies.set('dummy', SchoolEntry.parse(self.entry_url).department, domain='office.chaoxing.com')

            def handle(self, request):
                dept = SchoolEntry.parse(self.entry_url).department
                if request.headers.get('cookie') != f'dummy={dept}':
                    raise AssertionError('Cross-school cookie leak')
                target = self.settings.plan()
                path = request.url.path
                if path == '/front/third/apps/seat/select':
                    return httpx.Response(200, text='<input id="submit_enc" value="mock-page-key">')
                if path == '/data/apps/seat/submit':
                    fields = parse_qs(request.content.decode(), keep_blank_values=True)
                    if fields['deptIdEnc'] != [dept] or fields['roomId'] != ['12']:
                        raise AssertionError('Wrong school or room')
                    self.sent.append(fields)
                    self.saved.append({'id': 99, 'today': str(target.day), 'firstLevelName': target.campus,
                                       'secondLevelName': target.floor, 'thirdLevelName': target.third_level,
                                       'roomId': 12, 'seatNum': '003', 'status': 0, 'type': -1,
                                       'startTime': int(datetime.combine(target.day, target.start, CHINA_TZ).timestamp()*1000),
                                       'endTime': int(datetime.combine(target.day, target.end, CHINA_TZ).timestamp()*1000)})
                    return httpx.Response(200, json={'success': True})
                payloads = responses()
                for row in (payloads['/data/apps/seat/room/list']['seatRoomList'][0], payloads['/data/apps/seat/room/info']['seatRoom']):
                    row.update(firstLevelName=target.campus, secondLevelName=target.floor, thirdLevelName=target.third_level,
                               startTime=-7_200_000, endTime=54_000_000)
                payloads['/data/apps/seat/room/info']['seatConfig']['timeUnit'] = 15
                payloads['/data/apps/seat/getusedseatnums']['seatReserves'] = []
                payloads['/data/apps/seat/reservelist']['reserveList'] = self.saved
                return httpx.Response(200, json={'success': True, 'data': payloads[path]})
        with tempfile.TemporaryDirectory() as folder, patch('seat_reserver.runner.sync_playwright', side_effect=AssertionError('No browser')):
            batch = BatchRunner(lambda _: None, runner_factory=Worker, log_root=Path(folder))
            batch.start(BatchSettings([Profile(settings=s) for s in settings], use_profile_release=True))
            self.assertTrue(batch.wait(10))
            batch.join()
            self.assertEqual([s['code'] for s in batch.snapshot()], ['SUCCESS', 'SUCCESS'])
            self.assertEqual([len(w.sent) for w in workers], [1, 1])
            self.assertTrue(all(w.report['record_verified'] for w in workers))


if __name__ == '__main__':
    unittest.main()
