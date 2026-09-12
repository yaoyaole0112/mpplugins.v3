# 115网盘储存（MoviePilot V3 适配）

基于 DDSRem 的 P115Disk。本版本修复当前 115 WAF 对 `webapi.115.com` 返回 405 的问题：

- 路径/文件夹 ID 查询优先走 proapi（android / ios）
- `fs_search` 不再强制 `webapi.115.com`
- 全通道 405 时短冷却，避免 get_file_item 递归打爆日志

安装：插件市场添加 `https://github.com/yaoyaole0112/mpplugins.v3` 后搜索「115网盘储存」，源码安装 3.0.2。
