"""Local fake transport and encrypted dummy session tests; no live accounts."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from seat_reserver.model import ENTRY_URL, ACTIVE_STATUSES
from seat_reserver.protocol_session import (ProtocolSession, ProtocolError, VerificationRequired,
                               encrypt_login, save_session, load_session, apply_cookies)


LOGIN_HTML = '''<input id="phone"><input id="pwd"><input id="t" value="true">
<input id="needVcode" value=""><input id="shownotice" value="1">
<input id="fid" value="-1"><input id="refer" value="fixture">'''


class SessionTests(unittest.TestCase):
    def test_stale_other_school_sso_callback_returns_to_selected_entry(self):
        selected = ENTRY_URL.replace('214d62ddb0e920e7', 'dummy-other').replace('4309667', '42')
        calls = []
        authenticated = False
        def handle(request):
            nonlocal authenticated
            calls.append(request)
            if request.url.host == 'office.chaoxing.com':
                if not authenticated:
                    return httpx.Response(302, headers={'location': 'https://passport2.chaoxing.com/login'})
                return httpx.Response(200, text='selected school' if str(request.url) == selected else 'old school')
            if request.method == 'GET':
                return httpx.Response(200, text=LOGIN_HTML)
            authenticated = True
            return httpx.Response(200, json={'status': True, 'url': ENTRY_URL})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            session = ProtocolSession(client, selected)
            session.login('dummy-account', 'dummy-password')
        self.assertEqual(str(calls[-1].url), selected)
        self.assertEqual(session.entry_html, 'selected school')
        self.assertEqual(sum(r.method == 'POST' for r in calls), 1)

    def test_entry_redirect_to_another_school_is_rejected_before_login(self):
        selected = ENTRY_URL.replace('214d62ddb0e920e7', 'dummy-other')
        calls = []
        def handle(request):
            calls.append(request)
            if str(request.url) == selected:
                return httpx.Response(302, headers={'location': ENTRY_URL})
            return httpx.Response(200, text='<button>预约选座</button>')
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaisesRegex(ProtocolError, '学校入口不一致'):
                ProtocolSession(client, selected).login('dummy-account', 'dummy-password')
        self.assertFalse(any(r.method == 'POST' for r in calls))

    def test_visible_binding_prompt_and_dedicated_binding_redirect_stop_login(self):
        for redirect in (False, True):
            with self.subTest(redirect=redirect):
                def handle(request):
                    if redirect and str(request.url) == ENTRY_URL:
                        return httpx.Response(302, headers={'location': '/front/identity/bind'})
                    return httpx.Response(200, text='<script>const ready=true;</script>请先绑定学号/工号')
                with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                    session = ProtocolSession(client)
                    with self.assertRaisesRegex(VerificationRequired, '核对当前预约入口'):
                        session.login('dummy-account', 'dummy-password')
                    self.assertFalse(session.login_attempted)

    def test_inactive_binding_templates_do_not_block_authenticated_entry(self):
        content = '''<p>预约选座</p><script>const msg="请绑定学号";</script>
        <template>请绑定工号</template><p hidden>请绑定学号</p>
        <div aria-hidden="true">请绑定单位身份</div>
        <p style="display: none">请绑定学号</p><div style="visibility:hidden">请绑定工号</div>
        <p>无需绑定学号，已认证单位身份</p>'''
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=content))) as client:
            session = ProtocolSession(client)
            session.login('dummy-account', 'dummy-password')
            self.assertEqual(session.entry_html, content)

    def test_normal_public_login_fields_and_one_attempt(self):
        calls = []
        authenticated = False
        def handle(request):
            nonlocal authenticated
            calls.append(request)
            if request.url.host == 'office.chaoxing.com':
                return httpx.Response(200, text='<html>entry</html>') if authenticated else httpx.Response(302, headers={'location':'https://passport2.chaoxing.com/login'})
            if request.method == 'GET':
                return httpx.Response(200, text=LOGIN_HTML)
            self.assertNotIn(b'dummy-password', request.content)
            authenticated = True
            return httpx.Response(200, json={'status':True, 'url':ENTRY_URL})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            session = ProtocolSession(client)
            session.login('dummy-account', 'dummy-password')
            self.assertTrue(session.login_attempted)
            self.assertEqual(sum(r.method=='POST' for r in calls), 1)

    def test_captcha_prevents_password_post(self):
        calls = []
        def handle(request):
            calls.append(request)
            if request.url.host=='office.chaoxing.com':
                return httpx.Response(302, headers={'location':'https://passport2.chaoxing.com/login'})
            return httpx.Response(200, text=LOGIN_HTML.replace('id="needVcode" value=""','id="needVcode" value="1"'))
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(VerificationRequired):
                ProtocolSession(client).login('dummy-account','dummy-password')
        self.assertFalse(any(r.method=='POST' for r in calls))

    def test_other_origin_never_receives_credentials(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(302, headers={'location':'https://other.example/login'})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(ProtocolError):
                ProtocolSession(client).login('dummy-account','dummy-password')
        self.assertEqual(len(calls),1)

    def test_cookie_domain_and_expiry_are_preserved(self):
        with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200))) as client:
            apply_cookies(client,[{'name':'session','value':'dummy','domain':'.chaoxing.com','path':'/', 'secure':True,'expires':time.time()+60},
                                  {'name':'foreign','value':'dummy','domain':'other.example','path':'/'},
                                  {'name':'expired','value':'dummy','domain':'.chaoxing.com','path':'/','expires':time.time()-1}])
            cookies=list(client.cookies.jar)
            self.assertEqual(len(cookies),1)
            self.assertTrue(cookies[0].secure)
            self.assertEqual(cookies[0].domain,'.chaoxing.com')

    def test_dummy_cache_dpapi_roundtrip_has_no_plain_cookie(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'session.json'
            with patch('seat_reserver.protocol_session.cache_path',return_value=path):
                save_session('dummy-account',[{'name':'dummy-name','value':'dummy-cookie-secret'}],'dummy-agent')
                self.assertNotIn('dummy-cookie-secret',path.read_text())
                self.assertEqual(load_session('dummy-account')['cookies'][0]['value'],'dummy-cookie-secret')

    def test_completed_or_active_frontend_statuses_are_known(self):
        for status in ('学习中','暂离中','被监督中'):
            self.assertIn(status,ACTIVE_STATUSES)

    def test_transport_encryption_is_deterministic_but_not_plaintext(self):
        encrypted=encrypt_login('dummy-password')
        self.assertNotIn('dummy-password',encrypted)
        self.assertEqual(encrypted,encrypt_login('dummy-password'))


if __name__=='__main__':
    unittest.main()
