"""Normal Chaoxing password login over HTTP; challenges require manual handling."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from lxml import html
from lxml.etree import ParserError

from .model import ENTRY_URL
from .credentials import _crypt, data_dir
from .schools import SchoolEntry, SCHOOL_BINDING_MESSAGE, binding_prompt


class ProtocolError(Exception):
    pass


class VerificationRequired(ProtocolError):
    pass


def cache_path(account, scope=""):
    identity = account.strip().casefold() + ("\0" + scope if scope else "")
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()
    return data_dir() / 'protocol-sessions' / f'{digest}.json'


def save_session(account, cookies, user_agent, scope=""):
    # Private session state uses the same per-Windows-user encryption as passwords.
    raw = json.dumps({'created_at': time.time(), 'cookies': cookies,
                      'user_agent': user_agent}).encode('utf-8')
    path = cache_path(account, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = base64.b64encode(_crypt(raw, decrypt=False)).decode('ascii')
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({'version': 1, 'session_dpapi': encrypted}), encoding='utf-8')
    os.replace(temp, path)


def load_session(account, scope=""):
    path = cache_path(account, scope)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        if payload['version'] != 1:
            return None
        raw = _crypt(base64.b64decode(payload['session_dpapi'], validate=True), decrypt=True)
        session = json.loads(raw)
        age = time.time() - session['created_at']
        if not 0 <= age <= 7200 or not isinstance(session['cookies'], list):
            return None
        return session
    except Exception:
        raise ProtocolError('已保存的协议会话无法读取，请重新登录。') from None


def apply_cookies(client, cookies):
    from http.cookiejar import Cookie
    for item in cookies:
        domain = item['domain']
        if not (domain.lstrip('.') == 'chaoxing.com' or domain.lstrip('.').endswith('.chaoxing.com')):
            continue
        expires = item.get('expires', -1)
        if expires > 0 and expires <= time.time():
            continue
        cookie = Cookie(0, item['name'], item['value'], None, False,
                        domain, domain.startswith('.'), domain.startswith('.'),
                        item.get('path', '/'), True, bool(item.get('secure', False)),
                        int(expires) if expires > 0 else None, expires <= 0, None, None,
                        {'HttpOnly': bool(item.get('httpOnly', False))}, False)
        client.cookies.jar.set_cookie(cookie)


def encrypt_login(value: str) -> str:
    # Same transport encryption as the publicly served loginByPhoneAndPwdSubmit.
    key = b"u2oh6Vu^HWe4_AES"
    padder = padding.PKCS7(128).padder()
    padded = padder.update(value.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


class ProtocolSession:
    allowed_hosts = frozenset({"office.chaoxing.com", "passport2.chaoxing.com", "sso.chaoxing.com"})

    def __init__(self, client: httpx.Client, entry_url=ENTRY_URL):
        self.client = client
        self.entry = SchoolEntry.parse(entry_url)
        self.entry_url = self.entry.url
        self.login_attempted = False
        self.entry_html = ""
        self.trace = []

    def get_follow(self, url):
        for _ in range(10):
            parts = urlsplit(url)
            if (parts.scheme != "https" or parts.netloc not in self.allowed_hosts
                    or parts.username or parts.password):
                raise ProtocolError("登录跳转到未经核实的网站，已停止。")
            response = self.client.get(url, follow_redirects=False)
            self.trace.append({"method": "GET", "host": parts.hostname,
                               "path": parts.path, "status": response.status_code})
            if response.is_redirect:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            if response.status_code != 200:
                raise ProtocolError(f"登录页面或预约入口未正常返回（HTTP {response.status_code}）。")
            return response
        raise ProtocolError("登录跳转次数异常，已停止。")

    def _accept_entry(self, response):
        # Only inspect visible static content. Scripts and hidden form templates
        # often contain generic binding text even for a fully authenticated user.
        try:
            tree = html.document_fromstring(response.text)
        except (ValueError, ParserError):
            raise ProtocolError('学校预约入口内容无法识别。') from None
        for element in tree.xpath('//script|//style|//template|//*[@hidden]|//*[@aria-hidden="true"]'):
            if element.getparent() is not None:
                element.drop_tree()
        for element in tree.xpath('//*[@style]'):
            style = ''.join(element.get('style', '').lower().split())
            if ('display:none' in style or 'visibility:hidden' in style) and element.getparent() is not None:
                element.drop_tree()
        if binding_prompt(tree.text_content()):
            raise VerificationRequired(SCHOOL_BINDING_MESSAGE)
        if not self.entry.matches_page(str(response.url)):
            raise ProtocolError('登录后页面与所选学校入口不一致，请检查学校预约链接；本次未继续查询或提交。')
        self.entry_html = response.text

    def login(self, account, password):
        response = self.get_follow(self.entry_url)
        if response.url.host == "office.chaoxing.com":
            self._accept_entry(response)
            return
        if response.url.host != "passport2.chaoxing.com":
            raise ProtocolError("未进入已核实的学习通登录页面。")
        tree = html.fromstring(response.text)
        fields = {element.get("id", ""): element.get("value", "")
                  for element in tree.xpath("//input[@id]")}
        if not tree.xpath("//input[@id='phone']") or not tree.xpath("//input[@id='pwd']"):
            raise ProtocolError("学习通登录页面结构已经变化。")
        # shownotice only displays the public login-page notice below the form.
        if fields.get("needVcode") == "1":
            raise VerificationRequired("登录需要验证码，请使用正常浏览器完成。")
        if self.login_attempted:
            raise ProtocolError("本次已经尝试密码登录，禁止重复发送。")
        if fields.get("t") != "true":
            raise ProtocolError("登录密码加密配置与已核实的正常页面不一致。")
        data = {key: fields.get(key, "") for key in
                ("fid", "refer", "t", "forbidotherlogin", "validate", "doubleFactorLogin",
                 "independentId", "independentNameId")}
        data.update(uname=encrypt_login(account.strip()), password=encrypt_login(password))
        self.login_attempted = True
        result = self.client.post("https://passport2.chaoxing.com/fanyalogin", data=data,
                                  headers={"Referer": str(response.url), "X-Requested-With": "XMLHttpRequest"},
                                  follow_redirects=False)
        self.trace.append({"method": "POST", "host": "passport2.chaoxing.com",
                           "path": "/fanyalogin", "status": result.status_code})
        if result.status_code != 200:
            raise ProtocolError("学习通登录接口未正常返回；本次不重试密码。")
        try:
            payload = result.json()
        except ValueError:
            raise ProtocolError("学习通登录返回内容无法识别。") from None
        if not isinstance(payload, dict):
            raise ProtocolError("学习通登录返回结构无法识别。")
        if payload.get("status") is not True:
            raise VerificationRequired("学习通未完成登录，请在正常浏览器检查密码、验证码或账号提示。")
        if payload.get("containTwoFactorLogin"):
            raise VerificationRequired("学习通要求双因子验证，请通过正常浏览器完成。")
        target = unquote(payload.get("url", ""))
        response = self.get_follow(urljoin(str(response.url), target))
        # The SSO callback can point to another school's previous application.
        # Complete it, then always return to this task's selected school entry.
        response = self.get_follow(self.entry_url)
        self._accept_entry(response)
