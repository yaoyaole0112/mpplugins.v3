# 订阅规则自动填充

MoviePilot v3 插件，保留现有订阅和下载事件的媒体身份匹配逻辑。

## 功能

- 下载电视剧资源后，为尚未设置对应属性的订阅填充分辨率、资源质量、特效、制作组和站点。
- 新建订阅时，按二级分类配置填充订阅规则、过滤规则组、站点和保存路径。
- 查看填充历史，支持中文、表情符号和包含反斜杠的正则表达式。
- 独立开关控制两种填充方式；支持在配置页主动清理历史记录或已处理标记。

## 安装与配置

在 MoviePilot v3 的插件市场添加本仓库 `https://github.com/yaoyaole0112/mpplugins.v3`，安装“订阅规则自动填充”。

配置页中打开所需功能，选择需要自动填充的资源属性。二级分类规则每行一条，字段以 `#` 分隔，字段名与值以 `:` 分隔。支持字段：`category`、`resolution`、`quality`、`effect`、`include`、`exclude`、`savepath`、`sites`、`filter_groups`。多个分类、站点或规则组用英文逗号分隔，站点填写已启用站点的名称。

```text
category:欧美剧#resolution:1080p#filter_groups:📺剧集规则
category:港台剧,日韩剧#include:国粤
```

沿用上游作者 thsrite 的插件元信息，无额外第三方依赖；完整运行代码位于 `__init__.py`。历史记录和配置由 MoviePilot 持久化，不随代码发布，也不应上传到仓库。

## 3.0.1 修复说明

旧版历史页面使用 `unicode_escape` 解码 JSON。当规则组名称包含 `📺`、`🆙` 等表情时，JSON 中的 UTF-16 代理对被错误拆开，MoviePilot 序列化页面响应时触发 `UnicodeEncodeError: surrogates not allowed`，最终显示“插件数据加载失败”。这一处理还可能破坏中文或正则表达式的反斜杠。

新版使用 JSON 解析和 `ensure_ascii=False` 序列化来展示历史内容；非 JSON 文本原样展示，并兼容空内容和结构化历史内容。不清空历史，不更改用户配置，也不改变订阅自动填充逻辑。

## 回归测试

使用 Python 标准库即可运行，无需启动 MoviePilot：

```bash
python3 -m unittest discover -s plugins.v3/subscribegroup/tests -v
```

测试隔离宿主导入，覆盖页面 UTF-8 响应序列化、转义及未转义表情、中文、正则表达式、普通文本、空内容、结构化内容、旧版单条历史和排序时不修改原数据。
