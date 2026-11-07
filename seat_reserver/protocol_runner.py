"""Browser-free HTTP preparation and at-most-once seat reservation."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from lxml import html

from .account_lock import AccountLease
from .credentials import load_settings
from .model import CHINA_TZ, now_china, parse_time
from .protocol_api import ProtocolAPI, ProtocolQueryError
from .protocol_session import (ProtocolError, ProtocolSession, VerificationRequired,
                               apply_cookies, load_session, save_session)
from .protocol_signing import PublicSubmitSigner, SigningError
from .runner import ReservationRunner, Stopped
from .schools import OFFICE_ORIGIN, SchoolEntry, location_key, binding_prompt, SCHOOL_BINDING_MESSAGE

DEFAULT_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36')


class HTTPSubmission:
    """A transport call may have reached the server even when it raises."""
    def __init__(self, client, origin='https://office.chaoxing.com'):
        self.client = client
        self.origin = origin.rstrip('/')
        self.attempted = False

    def send(self, request):
        if self.attempted:
            raise ProtocolError('本次已经尝试提交，禁止重复发送。')
        if (request.method != 'POST' or str(request.url) != self.origin + '/data/apps/seat/submit'):
            raise ProtocolError('提交目标与已核实的接口不一致。')
        self.attempted = True
        return self.client.send(request, follow_redirects=False)


def require_no_challenge(info, risk, at_ms):
    config = info.config
    if type(config.get('securityVerify')) is not int or config['securityVerify'] != 0:
        raise VerificationRequired('该配置要求页面安全校验，请通过正常浏览器完成；协议模式未提交。')
    # The risk window is checked for the actual send time, not the preparation time.
    required = risk.enabled
    if risk.enabled and risk.type == 0 and info.before_open_timestamp:
        required = (info.before_open_timestamp - risk.before_ms <= at_ms
                    <= info.before_open_timestamp + risk.after_ms)
    if required:
        raise VerificationRequired('当前时刻要求正常风控验证，协议模式未提交，请使用浏览器流程。')
    if type(config.get('reserveMode')) is not int or config['reserveMode'] != 0:
        raise VerificationRequired('特殊预约规则尚未支持直接请求，请使用正常浏览器流程。')


def validate_rules(info, plan):
    config = info.config
    if (type(config.get('timeType')) is not int or config['timeType'] != 0
            or type(config.get('timeUnit')) is not int or config['timeUnit'] != plan.time_unit):
        raise VerificationRequired('该时段规则尚未支持直接请求，请使用浏览器流程。')
    if hasattr(info, 'open_start'):
        opening = info.open_start.hour * 60 + info.open_start.minute
        if any((clock.hour * 60 + clock.minute - opening) % plan.time_unit for clock in (plan.start, plan.end)):
            raise ProtocolError('所选时段未对齐学校房间的时间网格，未准备提交。')
    if info.common_pause_times or info.seat_interval_map:
        raise VerificationRequired('该房间有特殊暂停或座位时段规则，请使用浏览器流程。')
    hours = ((plan.end.hour * 60 + plan.end.minute) - (plan.start.hour * 60 + plan.start.minute)) / 60
    for key in ('minReserveDuration', 'reserveDuration'):
        value = config.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ProtocolError('预约时长规则无法核对，未准备提交。')
    if ((config['reserveDuration'] > 0 and hours > config['reserveDuration'])
            or (config['minReserveDuration'] > 0 and hours < config['minReserveDuration'])):
        raise ProtocolError('预约时长不符合学校规则，未准备提交。')


def booking_window(info, plan, at_ms):
    """Honor the same before/after day and clock fields as the public frontend."""
    config = info.config
    opening_ms = 0
    for prefix in ('reserveBefore', 'reserveAfter'):
        day_key, time_key = prefix + 'Day', prefix + 'Time'
        if day_key not in config and time_key not in config:
            continue
        days = config.get(day_key)
        if prefix == 'reserveAfter' and type(days) is int and days == -1:
            continue
        if type(days) is not int or not 0 <= days <= 30:
            raise ProtocolError('学校放号或截止天数规则无法识别，未准备提交。')
        try:
            clock = parse_time(config.get(time_key, ''))
        except (TypeError, ValueError):
            raise ProtocolError('学校放号或截止时间规则无法识别，未准备提交。') from None
        stamp = int(datetime.combine(plan.day - timedelta(days=days), clock, CHINA_TZ).timestamp() * 1000)
        if prefix == 'reserveBefore':
            opening_ms = stamp
        elif at_ms >= stamp:
            raise ProtocolError('已超过学校规定的预约截止时间，未提交。')
    return opening_ms


class ProtocolRunner(ReservationRunner):
    def __init__(self, emit, stop, *, client_factory=None, signer_factory=PublicSubmitSigner,
                 session_state=None, submit_enabled=True, **kwargs):
        super().__init__(emit, stop, **kwargs)
        self.client_factory = client_factory
        self.signer_factory = signer_factory
        self.session_state = session_state
        self.submit_enabled = submit_enabled
        self.sender = None
        self._has_run = False
        self.report = {'started_at': now_china().isoformat(), 'mode': 'direct-http',
                       'browser_launched': False, 'submission_attempts': 0,
                       'credentials_exported': False, 'timings_ms': {}}

    def _timed(self, name, operation):
        started = time.perf_counter()
        value = operation()
        self.report['timings_ms'][name] = round((time.perf_counter() - started) * 1000, 3)
        return value

    def _new_client(self, state):
        if self.client_factory:
            return self.client_factory()
        headers = {'User-Agent': state['user_agent'] if state else DEFAULT_USER_AGENT,
                   'Referer': self.entry_url, 'Accept': 'application/json, text/plain, */*'}
        # No transport retries. Connections are pooled and kept warm before release.
        transport = httpx.HTTPTransport(retries=0, http2=bool(importlib.util.find_spec('h2')),
                                        limits=httpx.Limits(max_connections=4, max_keepalive_connections=4,
                                                            keepalive_expiry=120))
        return httpx.Client(headers=headers, transport=transport, follow_redirects=False,
                            timeout=httpx.Timeout(15, connect=8))

    def _authenticate(self, client, state):
        if state:
            apply_cookies(client, state['cookies'])
        else:
            session = ProtocolSession(client, self.entry_url)
            session.login(self.settings.account, self.settings.password)
        self.report['used_cached_session'] = bool(state)
        # Normal identity authorization is still required after loading cookies.
        entry = SchoolEntry.parse(self.entry_url)
        response = client.get(OFFICE_ORIGIN + '/data/apps/seat/identity/verify',
                              params=entry.identity_parameters,
                              headers={'X-Requested-With': 'XMLHttpRequest'}, follow_redirects=False)
        try:
            payload = response.json()
        except ValueError:
            raise ProtocolError('身份查询未正常返回，请重新登录。') from None
        if not isinstance(payload, dict):
            raise ProtocolError('学校身份查询返回结构无法识别，已停止。')
        if response.status_code != 200 or payload.get('success') is not True:
            if any(binding_prompt(payload.get(key, '')) for key in ('msg', 'message', 'errorMsg')):
                raise VerificationRequired(SCHOOL_BINDING_MESSAGE)
            raise ProtocolError('当前账号未通过所选学校的身份查询。请先核对学校预约入口，再在学习通本校入口检查账号身份。')
        if not state:
            save_session(self.settings.account,
                         [{'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path,
                           'secure': c.secure, 'expires': c.expires or -1} for c in client.cookies.jar],
                         client.headers['User-Agent'], scope=self.entry_url)

    def _page_key(self, client, room, plan):
        response = client.get('https://office.chaoxing.com/front/third/apps/seat/select',
                              params={'deptIdEnc': self.api.dept_id_enc, 'id': room.id, 'day': str(plan.day)},
                              follow_redirects=False)
        if response.status_code != 200 or len(response.content) > 2_000_000:
            raise ProtocolError('选座参数页未正常返回，未准备提交。')
        tree = html.fromstring(response.text)
        keys = tree.xpath("//*[@id='submit_enc']/@value")
        if len(keys) != 1 or not keys[0] or len(keys[0]) > 256:
            raise ProtocolError('正常页面校验参数缺失，未准备提交。')
        return keys[0]

    def _prepare(self, client, signer, plan, room=None):
        room = room or self.api.select_room(plan)
        info = self.api.room_info(plan, room)
        validate_rules(info, plan)
        seats = self.api.seat_map(plan, room, info)
        self.emit('catalog', campus=plan.campus, floor=plan.floor, day=str(plan.day), interval=plan.interval,
                  location_key=location_key(self.entry_url, plan.campus, plan.floor, plan.third_level, plan.room_id),
                  labels=sorted(seats, key=int))
        risk = self.api.risk_config(room.dept_id)
        seat = self.api.resolve_candidate(seats, plan.seats)
        self.report.update(seat_count=len(seats), security_verify=info.config.get('securityVerify'),
                           risk_enabled=risk.enabled)
        if seat is None:
            raise ProtocolQueryError('候选座位均不可预约；本次未提交。')
        at_ms = int(now_china().timestamp() * 1000)
        trigger_ms = max(int(plan.trigger.timestamp() * 1000), info.before_open_timestamp,
                         booking_window(info, plan, at_ms))
        booking_window(info, plan, max(at_ms, trigger_ms))
        require_no_challenge(info, risk, max(trigger_ms, int(now_china().timestamp() * 1000)))
        key = self._page_key(client, room, plan)
        fields = {'deptIdEnc': self.api.dept_id_enc, 'roomId': str(room.id),
                  'day': str(plan.day), 'startTime': plan.start.strftime('%H:%M'),
                  'endTime': plan.end.strftime('%H:%M'), 'seatNum': seat,
                  'captcha': '', 'wyToken': ''}
        fields['enc'] = signer.sign(fields, key)
        request = client.build_request('POST', self.api.origin + '/data/apps/seat/submit', data=fields,
                                      headers={'X-Requested-With': 'XMLHttpRequest',
                                               'Referer': str(httpx.URL('https://office.chaoxing.com/front/third/apps/seat/select',
                                                                        params={'deptIdEnc': self.api.dept_id_enc,
                                                                                'id': room.id, 'day': str(plan.day)}))})
        self.report['seat'] = seat
        self.report['request_body_keys'] = sorted(fields)
        return room, info, risk, seat, request, trigger_ms

    def _wait_until(self, target):
        # Event waits are interruptible; there is no CPU spin or request polling.
        last_count = None
        while True:
            self.checkpoint()
            remaining = target - time.perf_counter()
            if remaining <= 0:
                return
            count = math.ceil(remaining)
            if not self.submission_started and remaining > 1 and count != last_count:
                self.emit('countdown', seconds=remaining)
                last_count = count
            if remaining < 0.5:
                # Python uses a high-resolution waitable timer on current Windows.
                # Event.wait uses a coarser kernel wait on this machine.
                time.sleep(min(0.001, remaining))
            else:
                self.stop.wait(min(0.1, remaining))

    def _run(self, settings, plan, *, preview=False, login_only=False):
        self.settings = settings
        settings.validate_account()
        self.entry_url = self._entry_override or SchoolEntry.parse(settings.entry_url).url
        if self._has_run:
            return self.finish('ERROR', '本次协议任务实例已运行，不能再次执行提交。')
        self._has_run = True
        try:
            self.checkpoint()
            state = self.session_state if self.session_state is not None else ({} if login_only else load_session(settings.account, scope=self.entry_url))
            with self._new_client(state) as client:
                self.emit('phase', '正在建立协议登录会话')
                self._timed('authentication', lambda: self._authenticate(client, state))
                params = parse_qs(urlsplit(self.entry_url).query)
                self.api = ProtocolAPI(client, params['fidEnc'][0])
                if login_only:
                    return self.finish('LOGIN_READY', '协议登录与学校身份查询通过，未提交预约。')
                self.report['plan'] = plan.summary()
                self.emit('phase', '正在直接查询本人预约记录')
                records = self._timed('initial_records', self.api.records)
                existing = self.existing(records, plan)
                self.report['existing_target_record'] = bool(existing)
                if existing and not preview:
                    return self.finish('EXISTING', f'目标日期已有有效预约：{existing.floor} {existing.seat}号，{existing.interval}；本次未提交。')
                self.emit('phase', '正在直接准备楼层、座位和提交参数')
                with self.signer_factory() as signer:
                    prepared = self._timed('prepare', lambda: self._prepare(client, signer, plan))
                    room, info, risk, seat, request, trigger_ms = prepared
                    self.report['signer_sources'] = signer.metadata
                    self.report['prepared'] = True
                    # Bind result verification to the exact resolved room. Older
                    # record responses without roomId can still verify a unique
                    # campus/floor/region, but never an ambiguous room selection.
                    plan = replace(plan, third_level=room.third_level,
                                   room_id=room.id if plan.room_id else 0)
                    if preview or not self.submit_enabled:
                        return self.finish('PREVIEW_READY', f'直接请求准备完成：{plan.day} {plan.floor} {seat}号，{plan.interval}；未提交。')
                    remaining = trigger_ms / 1000 - now_china().timestamp()
                    waiting = remaining > 0
                    deadline = time.perf_counter() + remaining
                    if remaining > 10:
                        self.emit('ready', '协议参数已准备，等待开放前最后检查。', seconds=remaining,
                                  trigger_time=trigger_ms)
                        self._wait_until(deadline - 10)
                    # Keep the connection warm; refresh records, room, occupancy and
                    # risk while still before release. No queries on the timed path.
                    self.emit('phase', '正在进行提交前协议检查')
                    if self.existing(self.api.records(), plan):
                        return self.finish('EXISTING', '提交前发现已有有效预约；本次未提交。')
                    prepared = self._timed('preflight', lambda: self._prepare(client, signer, plan, room))
                    room, info, risk, seat, request, fresh_trigger_ms = prepared
                    trigger_ms = max(trigger_ms, fresh_trigger_ms)
                    remaining = trigger_ms / 1000 - now_china().timestamp()
                    deadline = time.perf_counter() + remaining
                    waiting = waiting or remaining > 0
                    self.sender = HTTPSubmission(client)
                    self.emit('ready', '协议请求已准备，等待一次发送。', seconds=max(0, remaining),
                              trigger_time=trigger_ms)
                    self._wait_until(deadline)
                    require_no_challenge(info, risk, int(now_china().timestamp() * 1000))
                    booking_window(info, plan, int(now_china().timestamp() * 1000))
                    self.checkpoint()
                    self.submission_started = True
                    sent_at = now_china()
                    started = time.perf_counter()
                    self.report['client_release_delay_ms'] = max(0, round((started-deadline)*1000, 3)) if waiting else None
                    payload = None
                    try:
                        response = self.sender.send(request)
                        self.report['http_status'] = response.status_code
                        payload = response.json()
                        self.report['response_success'] = payload.get('success') if isinstance(payload, dict) else None
                    except (httpx.HTTPError, ValueError):
                        self.report['transport_uncertain'] = True
                    self.report['timings_ms']['submit_round_trip'] = round((time.perf_counter()-started)*1000, 3)
                    self.report['submission_attempts'] = int(self.sender.attempted)
                    # Report after transmission so GUI callbacks and file writes
                    # cannot delay the timed network call.
                    self.emit('release', '已发送一次协议预约请求', actual_time=sent_at.isoformat(),
                              triggered_at=sent_at.isoformat(),
                              delay_ms=self.report['client_release_delay_ms'])
                    self.emit('phase', '正在通过接口核对预约结果')
                    for delay in (0, 0.2, 0.5, 1, 2):
                        self._wait_until(time.perf_counter() + delay)
                        try:
                            matches = [r for r in self.api.records() if r.matches(plan, seat)]
                        except ProtocolQueryError:
                            continue
                        if matches:
                            self.report.update(record_verified=True, record_status=matches[0].status)
                            return self.finish('SUCCESS', f'全协议预约已核对成功：{plan.day} {plan.floor} {seat}号，{plan.interval}。')
                    self.report['record_verified'] = False
                    if isinstance(payload, dict) and payload.get('success') is False:
                        return self.finish('REJECTED', '服务端拒绝本次预约；本次不重新提交。')
                    return self.finish('UNKNOWN', '提交结果未确认；只核对记录，不再次提交。')
        except Stopped:
            return self.finish('UNKNOWN' if self.submission_started else 'STOPPED', '任务已停止；提交过的任务请核对预约记录。')
        except KeyboardInterrupt:
            self.stop.set()
            return self.finish('UNKNOWN' if self.submission_started else 'STOPPED', '任务已中断；提交过的任务请核对预约记录。')
        except VerificationRequired as error:
            return self.finish('MANUAL_REQUIRED', str(error))
        except (ProtocolError, ProtocolQueryError, SigningError) as error:
            return self.finish('UNKNOWN' if self.submission_started else 'ERROR', str(error))
        except Exception as error:
            return self.finish('UNKNOWN' if self.submission_started else 'ERROR', f'协议流程异常（{type(error).__name__}），已停止。')
        finally:
            if self.sender:
                self.report['submission_attempts'] = int(self.sender.attempted)
            self.report.update(code=self.outcome, ended_at=now_china().isoformat())


def main(argv=None):
    parser = argparse.ArgumentParser(description='全HTTP单账号预约；默认只读准备，不提交')
    parser.add_argument('--date', help='YYYY-MM-DD；省略时沿用已保存日期设置')
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--floor')
    parser.add_argument('--school-name')
    parser.add_argument('--entry-url', help='学校学习通座位预约完整入口')
    parser.add_argument('--campus')
    parser.add_argument('--third-level')
    parser.add_argument('--room-id', type=int)
    parser.add_argument('--days-ahead', type=int)
    parser.add_argument('--release-days-before', type=int)
    parser.add_argument('--time-unit', type=int)
    parser.add_argument('--seats')
    parser.add_argument('--release-time')
    parser.add_argument('--submit', action='store_true')
    parser.add_argument('--fresh-login', action='store_true', help='不使用缓存会话，正常密码登录一次')
    parser.add_argument('--list-rooms', action='store_true', help='只登录和查询该校全部房间，不准备或提交预约')
    parser.add_argument('--output', type=Path, default=Path('output/playwright/direct-http.json'))
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.list_rooms and args.submit:
        parser.error('--list-rooms 不能与 --submit 同时使用')
    overrides = {name: getattr(args, name) for name in ('school_name', 'entry_url', 'campus', 'floor',
                 'third_level', 'room_id', 'days_ahead', 'release_days_before', 'time_unit', 'seats', 'release_time')
                 if getattr(args, name) is not None}
    if args.date:
        overrides.update(date_mode='specific', booking_date=args.date)
    if args.start is not None:
        overrides['start_time'] = args.start
    if args.end is not None:
        overrides['end_time'] = args.end
    settings = replace(settings, **overrides)
    if args.list_rooms:
        from .school_catalog import read_school_catalog
        catalog = read_school_catalog(settings, day=args.date)
        for room in catalog['rooms']:
            print(f"{room['id']} · {room['campus']} / {room['floor']} / {room['third_level']} · {room['capacity']} 席")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'房间查询报告：{args.output.resolve()}')
        return 0
    plan = settings.plan(for_preview=not args.submit)
    def emit(event):
        if event['kind'] in {'phase', 'result'}:
            print(event['text'], flush=True)
    runner = ProtocolRunner(emit, threading.Event(), session_state={} if args.fresh_login else None,
                            submit_enabled=args.submit, keep_open=False)
    try:
        code = runner.run(settings, plan, preview=not args.submit)
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(runner.report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'脱敏报告：{args.output.resolve()}')
    return 0 if code in {'SUCCESS', 'EXISTING', 'PREVIEW_READY'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
