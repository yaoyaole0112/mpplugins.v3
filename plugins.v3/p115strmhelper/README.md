# 115网盘STRM助手（MoviePilot V3 适配）

## 3.0.3 更新说明

- 修复批量转存时生活事件只读取最新一页（最多 1000 条），导致较早文件可能遗漏整理的问题：按接口游标分页拉取，缺少游标时使用偏移量翻页。
- 分页失败或页码未推进时保留原监听游标，避免漏掉尚未处理的文件；新增分页回归测试。
- 升级不会自动重放过去已经跳过的事件。请检查待整理目录及目标媒体库，并通过「手动网盘整理」补扫仍留在待整理目录中的文件。

基于 DDSRem 的 P115StrmHelper，适配 MoviePilot V3：

- 整理接管改为 V3 `_plan_checkpoint_and_execute` 拦截
- 历史查询改用 `media_source` / `media_id`
- 恢复 Vue 联邦 `dist/`，本地目录浏览兼容 V3 `storage/list` 包装，并优先走插件 `browse_dir`
- 增量同步 405 风控熔断与 proapi 优先

已被旧版监听游标越过的文件不会因升级自动重放。请在插件的「手动网盘整理」中对仍位于待整理目录的文件所在文件夹发起补扫；补扫前先检查目标媒体库中是否已有同集，尤其留意自动删除低质量源文件的设置。

安装：MoviePilot 插件市场添加仓库 `https://github.com/yaoyaole0112/mpplugins.v3` 后搜索「115网盘STRM助手」。本仓库未提供 GitHub Release 压缩包，请使用源码安装（`release=false`）。
