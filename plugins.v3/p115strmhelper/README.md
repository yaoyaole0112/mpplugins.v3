# 115网盘STRM助手（MoviePilot V3 适配）

基于 DDSRem 的 P115StrmHelper，适配 MoviePilot V3：

- 整理接管改为 V3 `_plan_checkpoint_and_execute` 拦截
- 历史查询改用 `media_source` / `media_id`
- 恢复 Vue 联邦 `dist/`，本地目录浏览兼容 V3 `storage/list` 包装，并优先走插件 `browse_dir`
- 增量同步 405 风控熔断与 proapi 优先

安装：MoviePilot 插件市场添加仓库 `https://github.com/yaoyaole0112/mpplugins.v3` 后搜索「115网盘STRM助手」。本仓库未提供 GitHub Release 压缩包，请使用源码安装（`release=false`）。
