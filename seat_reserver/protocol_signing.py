"""Execute the normal public submitVerify module without rewriting its algorithm.

The worker is started while preparing the session, so the timed submission path
does not start a process or read JavaScript files. Signing is only parameter
encoding: callers must independently complete the normal CAPTCHA/risk checks.
Request parameters and the page's submit_enc value remain in memory.
"""
from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import threading
from pathlib import Path

from .signing_assets import asset_directory, verify_assets, SigningAssetsError


class SigningError(RuntimeError):
    """A deliberately generic error that never includes request values."""


_WORKER = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const readline = require('readline');
const crypto = require('crypto');
const reply = value => process.stdout.write(JSON.stringify(value) + '\n');
let context;
try {
  const signingSource = fs.readFileSync(process.argv[1], 'utf8');
  const md5Source = fs.readFileSync(process.argv[2], 'utf8');
  context = vm.createContext(Object.create(null), {
    codeGeneration: { strings: false, wasm: false }
  });
  vm.runInContext(`
    var window = this;
    var __pageKey = '';
    var document = {
      getElementById: function(id) {
        if (id !== 'submit_enc') throw new Error('unexpected document access');
        return {value: __pageKey};
      }
    };
    var __signingModule;
    function define(dependencies, factory) {
      if (!Array.isArray(dependencies) || dependencies.length !== 1
          || dependencies[0] !== 'md5') throw new Error('unsupported dependencies');
      __signingModule = factory();
    }
  `, context, {timeout: 2000});
  vm.runInContext(md5Source, context, {timeout: 2000});
  vm.runInContext(signingSource, context, {timeout: 2000});
  if (!vm.runInContext("__signingModule && typeof __signingModule.verifyParam === 'function'",
      context, {timeout: 1000})) throw new Error('missing normal function');
  reply({ready: true,
    signing_sha256: crypto.createHash('sha256').update(signingSource).digest('hex'),
    md5_sha256: crypto.createHash('sha256').update(md5Source).digest('hex')});
} catch (_) {
  reply({error: 'public-module-load-failed'});
  process.exit(2);
}
const lines = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
lines.on('line', line => {
  try {
    const request = JSON.parse(line);
    context.__requestJson = JSON.stringify(request);
    const enc = vm.runInContext(`
      (function() {
        var request = JSON.parse(__requestJson);
        __pageKey = request.submit_enc;
        try { return __signingModule.verifyParam(request.parameters); }
        finally { __pageKey = ''; }
      })()
    `, context, {timeout: 1000});
    context.__requestJson = '';
    if (typeof enc !== 'string' || !/^[a-f0-9]{32}$/i.test(enc)) {
      throw new Error('unexpected normal module result');
    }
    reply({enc});
  } catch (_) {
    context.__requestJson = '';
    reply({error: 'public-signing-failed'});
  }
});
"""


class PublicSubmitSigner:
    """Persistent adapter around browser-delivered public signing JavaScript.

    Only public source paths appear on the command line. Parameter values travel
    through private stdin/stdout pipes and are never written to a log or file.
    The JavaScript gets a minimal context without Node's filesystem/network APIs;
    this is an execution adapter for trusted public sources, not a security
    boundary for arbitrary or untrusted JavaScript.
    """

    def __init__(self, signing_js: Path | str | None = None, md5_js: Path | str | None = None,
                 *, node_path: Path | str | None = None, timeout: float = 5):
        vendor = asset_directory()
        self._default_assets = signing_js is None and md5_js is None
        self.signing_js = Path(signing_js or vendor / "submitVerify.min.js").resolve()
        self.md5_js = Path(md5_js or vendor / "md5.js").resolve()
        self.node_path = str(node_path) if node_path is not None else shutil.which("node")
        self.timeout = timeout
        self.process = None
        self.metadata = {}
        self._responses = queue.Queue()
        self._lock = threading.RLock()

    def _read_stdout(self, process, responses):
        try:
            while True:
                line = process.stdout.readline(4096)
                if not line:
                    break
                if not line.endswith("\n"):
                    responses.put(None)
                    return
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    value = None
                responses.put(value)
        except (OSError, ValueError):
            pass
        finally:
            responses.put(None)

    def _response(self):
        try:
            result = self._responses.get(timeout=self.timeout)
        except queue.Empty:
            self.close()
            raise SigningError("公开校验模块未及时返回，未生成提交参数。") from None
        if not isinstance(result, dict) or "error" in result:
            raise SigningError("公开校验模块执行失败，未生成提交参数。")
        return result

    def start(self):
        with self._lock:
            if self.process is not None:
                if self.process.poll() is not None:
                    raise SigningError("公开校验模块进程已结束。")
                return self
            if self._default_assets:
                try:
                    verify_assets(self.signing_js.parent)
                except SigningAssetsError:
                    raise SigningError('协议模式缺少已核对的校验脚本。请按 docs/PROTOCOL.md 导入本地依赖，或使用浏览器模式。') from None
            if not self.node_path or not self.signing_js.is_file() or not self.md5_js.is_file():
                raise SigningError("缺少 Node 或正常页面的公开校验模块。")
            self._responses = queue.Queue()
            try:
                self.process = subprocess.Popen(
                    [self.node_path, "-e", _WORKER, str(self.signing_js), str(self.md5_js)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                raise SigningError("无法启动公开校验模块。") from None
            threading.Thread(target=self._read_stdout, args=(self.process, self._responses), daemon=True).start()
            try:
                result = self._response()
                if result.get("ready") is not True:
                    raise SigningError("公开校验模块未准备完成。")
                self.metadata = {key: result[key] for key in ("signing_sha256", "md5_sha256")}
            except Exception:
                self.close()
                raise
            return self

    def sign(self, parameters: dict, submit_enc: str) -> str:
        # Real submission fields are strings; avoiding implicit coercion keeps
        # their values exactly the same as the normal page's parameters.
        if (not isinstance(parameters, dict) or not parameters
                or any(not isinstance(key, str) or not isinstance(value, str)
                       or key in {"__proto__", "constructor", "prototype"}
                       for key, value in parameters.items())
                or not isinstance(submit_enc, str) or not submit_enc):
            raise SigningError("提交字段或页面校验值格式不正确。")
        payload = json.dumps({"parameters": parameters, "submit_enc": submit_enc},
                             ensure_ascii=True, separators=(",", ":"))
        with self._lock:
            if self.process is None or self.process.poll() is not None:
                raise SigningError("公开校验模块尚未预启动或已结束。")
            try:
                self.process.stdin.write(payload + "\n")
                self.process.stdin.flush()
                result = self._response()
            except (OSError, ValueError):
                self.close()
                raise SigningError("公开校验模块通信失败。") from None
            enc = result.get("enc")
            if not isinstance(enc, str) or re.fullmatch(r"[a-fA-F0-9]{32}", enc) is None:
                self.close()
                raise SigningError("公开校验模块返回格式不正确。")
            return enc

    def close(self):
        with self._lock:
            process = self.process
            self.process = None
            if process is None:
                return
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            if process.stdout:
                process.stdout.close()
            # A later start creates a new response queue; this worker's reader
            # cannot deliver an old reply to a different process.

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
