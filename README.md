# mpplugins.v3

## 115 目录浏览

「115网盘Plus」由 `P115Disk` 提供，「115网盘」由 MoviePilot 的 `u115` 提供。
`P115Disk` 3.0.6 不会在目录读取失败时改用另一个存储账号。

旧版 MoviePilot 的文件管理接口可能把查询异常当成空目录。
对于匹配的 V3 宿主，可在启动后端前执行 `python3 scripts/patch_moviepilot_115_listing.py --app /app/app`，
让文件管理 API 返回查询失败而不是空列表。此脚本不解除 115 服务端访问限制，也不修改 STRM 助手。
