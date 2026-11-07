# 论坛 RSS 种子下载

插件按以下顺序工作：

1. 读取论坛 RSS 的最新条目。
2. 用配置的登录 Cookie 请求帖子页面。
3. 提取 Discuz `mod=attachment` 链接并下载 `.torrent` 文件。
4. 校验响应不是登录页，再把文件内容上传到已配置的 qBittorrent 下载器。
5. 按附件 URL 保存历史，避免重复添加。

插件不保存账号密码，也不负责验证码登录。Cookie 应从你自己的浏览器开发者工具复制，并只填写在 MoviePilot 插件配置中。Cookie 过期后更新配置即可。
