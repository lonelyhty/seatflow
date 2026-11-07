# Contributing

欢迎改进学校适配、运行可靠性和文档。提交问题时请说明系统、Python 版本、学校使用的座位系统和脱敏后的错误提示，不要上传真实账号、密码、Cookie、加密凭证文件或完整抓包。

## Development

Follow the installation steps in the README, then run:

```powershell
.\.venv\Scripts\python.exe .\scripts\check.py
```

The runner creates a temporary application data directory, so tests do not read the current user's saved accounts. HTTP tests use mock transports; browser tests use localhost or intercepted offline pages. The complete suite currently requires Windows for DPAPI and desktop UI coverage.

Optional comparisons against the original upstream signing scripts are skipped when those scripts are absent. To include them after importing valid local copies, set `SEATFLOW_SIGNING_DIR` to their directory and run the same command. CI runs without these third-party scripts and still tests the subprocess adapter with original synthetic fixtures. Test fixtures must never call live reservation endpoints.

## Changes

- Preserve separate sessions for each account and school.
- Keep verification and booking actions explicit; do not retry an ambiguous submission.
- Document unsupported rules rather than guessing server behavior.
- Test with synthetic data. A new school's successful mock test is not proof of live compatibility.
- Run the packaging and offline tests before submitting a pull request.

```powershell
.\.venv\Scripts\python.exe -m pip wheel --no-deps . --wheel-dir dist
```

The repository license is MIT for original code. Do not commit private data or third-party page assets without redistribution rights.
