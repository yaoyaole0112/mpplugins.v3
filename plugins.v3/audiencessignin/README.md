# 观众签到

MoviePilot 插件，专门处理观众站 https://audiences.me/attendance.php 的每日签到。

观众站签到页会先显示 Cloudflare Turnstile（请验证您是真人），验证通过后自动 POST attendance.php 领取爆米花。官方「站点自动签到」只是打开页面，检测到已登录就报成功，并不会勾验证码，所以会假成功。

## 安装

1. 用「插件管理 -> 本地插件安装」上传 AudiencesSignIn-1.0.1.zip
2. 或把本目录挂到容器 /app/app/plugins/audiencessignin 后重启 MoviePilot
3. 也可添加第三方插件仓库：https://github.com/yaoyaole0112/mpplugins.v3

## 使用

1. 站点管理里先配好观众站 Cookie 和 UA（插件默认读取这一份）
2. 把观众从「站点自动签到」名单里去掉，避免假成功通知
3. 在 YesCaptcha 个人中心复制完整 ClientKey，填到「打码平台 ClientKey」
4. 打码平台选 YesCaptcha，启用插件。Docker 无头浏览器过不了勾选框，配置打码后会跳过浏览器直接拿 Token
5. 需要的话打开「立即运行一次」

远程命令：/audiences_signin
