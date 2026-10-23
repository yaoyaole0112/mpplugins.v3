# NanShare 115 转存

通过 MoviePilot 已配置的 Telegram Bot 接收链接，并调用 115 接口完成操作：

- 115 分享链接：解析分享码和 4 位提取码，转存分享根目录到指定 115 目录。
- magnet、ed2k、HTTP(S) .torrent：提交 115 离线下载任务到指定目录。
- 支持 /nanshare 链接、/ns 链接，也支持直接发送链接。

## 配置

1. 在 MoviePilot 插件设置中启用插件。
2. 填写 115 Cookie（不要提交到 GitHub 或日志）。
3. 填写目标目录，例如 /媒体/NanShare；目录不存在时插件会请求 115 自动创建。
4. 在 MoviePilot 通知渠道中启用 Telegram Bot，并确保 Bot 能接收命令和消息。

本插件不把 Cookie、消息正文或完整链接写入日志。转存前请确认你拥有相关内容的合法访问和存储权限。
