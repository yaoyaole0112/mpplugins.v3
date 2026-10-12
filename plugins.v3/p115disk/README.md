# 115网盘储存（MoviePilot V3 适配）

基于 DDSRem 的 P115Disk。本版本修复当前 115 WAF 对 `webapi.115.com` 返回 405 的问题：

- 路径/文件夹 ID 查询优先走 proapi（android / ios）
- `fs_search` 不再强制 `webapi.115.com`
- 全通道 405 时短冷却，避免 get_file_item 递归打爆日志

安装：插件市场添加 `https://github.com/yaoyaole0112/mpplugins.v3` 后搜索「115网盘储存」，源码安装 3.0.4。

3.0.4 整理目标优先使用 MoviePilot 内置 U115Pan 的 Open 严格查询契约，不受 Cookie 路径查询冷却影响，也不经过 STRM 助手的非严格 get_item 补丁。确认不存在与请求失败分别处理；Open 不可用时才回退 Cookie 有限重试。需在 MoviePilot 设置中完成 115 Open 授权。

3.0.3 对整理目标状态查询增加串行复核：锁等待与冷却等待共用 65 秒预算，冷却结束后再查询；遇到 405 最多重试一次。网络请求仍使用客户端自身超时，因此 65 秒不是整个调用的硬超时。持续风控或无法确认目标状态时仍抛出查询异常，不会将未知状态视为文件不存在，也不会因此删除源文件。
