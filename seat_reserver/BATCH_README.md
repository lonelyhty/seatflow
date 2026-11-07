# SeatFlow · 座流：多账号使用说明

1. 点击“添加账号”，填写学习通账号和密码。
2. 点击“学校与放号规则…”，选择学校入口，登录查询并选择房间。其他学校需要本校完整预约链接；名称只是备注。
3. 核对使用日期、使用时段、放号时间和候选座位，保存到账号列表。
4. 先测试登录或读取座位。多个学校放号时间不同时，勾选“使用各账号独立放号时间”。
5. 确认配置后，点击“启动全部启用账号预约”。保持网络连接并避免电脑休眠。

HTTP 模式需要 Node.js 和另外导入的本地校验脚本；指定房间编号也需要 HTTP 模式。脚本未配置时，可用浏览器模式并留空房间编号。遇到身份绑定或验证码请按学校正常流程处理。

账号和会话使用 Windows DPAPI 加密，默认保存在 %LOCALAPPDATA%\SeatFlow。开源版本不会自动读取旧开发版的账号配置。

完整文档：https://github.com/lonelyhty/seatflow
学校接入：https://github.com/lonelyhty/seatflow/blob/main/docs/SCHOOLS.md
HTTP 模式：https://github.com/lonelyhty/seatflow/blob/main/docs/PROTOCOL.md
