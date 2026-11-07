# SeatFlow · 座流

让每个账号，按自己的时间就位。

[![Windows tests](https://github.com/lonelyhty/seatflow/actions/workflows/tests.yml/badge.svg)](https://github.com/lonelyhty/seatflow/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](pyproject.toml)

SeatFlow 是面向 Windows 的学习通图书馆座位预约工具。用一个桌面窗口管理多个账号，分别设置学校入口、房间、候选座位和放号时间；提前完成准备，到点执行预约，并核对本人预约记录。

当前版本 **0.6.1**。项目独立运行，不依赖 Crawl4AI，也不需要复制开发者的 Python 环境。

## 功能

- **多账号并发**：独立登录会话、任务状态和停止控制；默认最多同时运行 20 个账号，可配置 1–64。超出上限会提示，实际容量取决于电脑、网络和服务端限制。
- **每账号学校配置**：学校入口、校区、二级/三级区域、房间编号、放号时间和预约天数分别保存。
- **两种运行方式**：浏览器模式可查看网页；HTTP 模式直接准备请求，需要另行配置本地第三方脚本。
- **先测试再预约**：支持登录测试和座位预览；候选座位按学校及房间保存，切换位置不会混用。
- **核对结果**：匹配日期、地点、座位、使用时段及有效状态。HTTP 模式每个任务最多尝试提交一次；结果不明时核对记录，不重复提交。
- **本地保存**：密码和协议会话使用 Windows DPAPI 加密；配置、日志和结果存储在当前用户电脑。

## 支持范围

当前适配 `office.chaoxing.com/front/third/apps/seat/` 座位系统，已有河北工业大学的实际验证记录。其他学校完成的是配置支持和本地模拟验证，尚未逐校实测。学习通里打开的应用可能属于其他系统，不能保证全部兼容。

学校名称只是备注，实际学校由完整预约链接确定。其他学校首次接入需要提供本校入口，不能仅凭账号或学校名称自动发现。支持普通数字座位号、网格座位布局和同日连续时段；验证码、身份绑定、学校统一认证、特殊预约规则等可能需要人工处理或另行适配。

## 安装与启动

环境：Windows 10/11、Python 3.11 或更新版本（安装时保留 Tcl/Tk）。浏览器模式还需安装 Playwright Chromium；HTTP 模式另需 Node.js 20+ 和[本地校验脚本](docs/PROTOCOL.md)。目前没有发布预编译 EXE，也未发布 PyPI 包。

在 PowerShell 中执行：

```powershell
git clone https://github.com/lonelyhty/seatflow.git
cd seatflow
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe .\seat_reserver_batch.py
```

上述命令直接使用虚拟环境中的 Python，无需修改 PowerShell 执行策略。启动单账号窗口可运行 `seat_reserver_app.py --no-autostart`。

## 第一次使用

1. 在多账号窗口添加账号，填写学习通账号和密码。
2. 点击“学校与放号规则…”。选择内置河北工大入口，或选择“其他学校”并粘贴本校完整预约链接。
3. 点击“登录并查询该校房间”，选中实际房间，再核对放号时间、提前天数和时段步长。该操作不提交预约。
4. 填写使用日期、使用时段和候选座位，保存账号。先测试登录或读取座位，确认配置。
5. 多校运行时勾选“使用各账号独立放号时间”。在放号前启动预约，保持电脑联网且不休眠。

指定房间编号时请使用 HTTP 模式，以核对准确房间。未准备本地脚本时可使用浏览器模式并留空房间编号，但同名区域或复杂规则仍可能需要进一步适配。详细说明见[学校接入](docs/SCHOOLS.md)和[HTTP 模式](docs/PROTOCOL.md)。

## 命令行

```powershell
# 显示帮助；不读取账号、不预约
.\.venv\Scripts\python.exe .\seat_reserver_batch.py --help

# 打开空白演示窗口；不读取已保存账号
.\.venv\Scripts\python.exe .\seat_reserver_batch.py --preview

# 检查已保存的配置；不登录、不预约
.\.venv\Scripts\python.exe .\seat_reserver_batch.py --check --per-profile-release

# HTTP 登录测试；不需要校验脚本，不提交预约
.\.venv\Scripts\python.exe .\seat_reserver_batch.py --protocol --test-login

# 实际预约：按已保存的启用账号执行
.\.venv\Scripts\python.exe .\seat_reserver_batch.py --protocol --per-profile-release --run
```

安装后也可使用 `.venv\Scripts\seatflow.exe` 启动多账号程序。未带 `--run`、`--test-login`、`--test-seats` 或 `--check` 时默认打开图形界面。

## 数据与开发

默认数据目录为 `%LOCALAPPDATA%\SeatFlow`。可用 `SEATFLOW_DATA_DIR` 指定其他本地目录。旧开发版的 `HebutSeatReserver` 目录不会被自动读取；加密文件只能由原 Windows 用户解密。

请仅使用本人或获得明确授权的账号，并遵守各校预约规则。程序不自动完成身份绑定，不绕过验证码或风控，不承诺抢位成功。

```powershell
# 使用临时配置目录运行测试，不读取真实账号
.\.venv\Scripts\python.exe .\scripts\check.py
```

测试覆盖本地浏览器流程、模拟 HTTP、会话隔离、配置和界面。上游脚本未导入时会明确跳过依赖它的测试，其余测试仍可运行。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

项目原创代码使用 [MIT License](LICENSE)。依赖及未分发脚本说明见 [THIRD_PARTY.md](THIRD_PARTY.md)。本项目非学习通、超星或任何学校官方产品。
