# HTTP 模式与本地依赖

HTTP 模式使用正常学习通密码登录、学校身份核验、房间和座位查询，提前准备请求并在触发时发送。它不启动浏览器，每个任务最多尝试提交一次；超时或结果不明时仅核对本人记录。遇到验证码、双因子、身份绑定或未支持的风控规则时会停止。

## 为什么有额外依赖

请求校验适配器调用学校正常网页所使用的 `submitVerify.min.js` 和 `md5.js`。这两个文件不是项目原创源码；仓库及安装包均不分发它们，也不把它们纳入 MIT 许可。浏览器模式由网页自行加载脚本，无需本地导入。

HTTP 模式需要 Node.js 20+，以及你从有权访问的正常预约页取得的两个原始脚本。可以在浏览器开发者工具的 Network/Sources 中按文件名查找并保存，保持原始字节不变。不要把账户信息或完整网络抓包放进本目录。

当前只接受以下已经验证的版本，其他版本会停止，需维护者重新验证后更新：

| 文件 | SHA-256 |
| --- | --- |
| `submitVerify.min.js` | `1b0282c64421fae9f018cd341a355c3df215b3f3383ef5f114cf226a3592df7a` |
| `md5.js` | `3e16fe48572dcb08da6583ba5acecfac5f5b04841dc7c2c34396913fa20094e2` |

将两个文件放在同一个本地文件夹后导入：

```powershell
.\.venv\Scripts\python.exe -m seat_reserver.signing_assets --from-directory 'C:\path\to\local-scripts'
# 仅检查已导入文件；不联网、不登录、不预约
.\.venv\Scripts\python.exe -m seat_reserver.signing_assets
```

导入器先检查两个文件的哈希，全部通过后写入 `%LOCALAPPDATA%\SeatFlow\signing`。环境变量 `SEATFLOW_SIGNING_DIR` 可指定其他本地脚本目录。文件缺失或版本不符时不会执行校验脚本。无法取得已验证版本时，请使用浏览器模式；不要用未知脚本替换。

## 启动

多账号窗口勾选“本次使用协议模式（直接请求）”。先测试登录，再配置日期、使用时段和候选座位，在放号前启动。多校批次勾选“使用各账号独立放号时间”。

```powershell
# 打开预选 HTTP 模式的窗口
.\.venv\Scripts\python.exe .\seat_reserver_batch.py --protocol

# 读取本机单账号配置，查询学校房间；不提交
.\.venv\Scripts\python.exe -m seat_reserver.protocol_runner --list-rooms

# 单账号 HTTP 参数说明；缺少 --submit 时不会实际提交
.\.venv\Scripts\python.exe -m seat_reserver.protocol_runner --help
```

登录测试和只读房间查询本身不执行请求签名，因此可在未导入脚本时先检查账号和学校入口。准备或实际预约阶段才需要本地校验脚本。

会话缓存按账号与学校入口隔离，并在通过学校身份核验后加密保存。当前适配普通数字座位与同日连续时段，不支持所有学校认证方式和特殊规则，也不保证整点成功率。
