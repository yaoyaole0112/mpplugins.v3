# 自动生成ED2K链接

在 MoviePilot 的「插件管理 → 本地插件安装」中加载后，配置 MoviePilot 容器内可见的本地目录，例如 `/downloads/complete` 或 `/media/movies`。

插件会按计划扫描目录，只对缓存中不存在或大小/修改时间发生变化的新增文件计算标准 ED2K 哈希，并将链接增量写入 `ed2k.txt`。默认同时写入隐藏缓存文件 `.ed2k.txt.cache.json`，已处理文件不会重复计算。

可选启用「生成完成后删除源文件」，以及通过 Telegram Bot 将新生成的链接发送到私聊、群组或频道。频道/群组需要填写 Bot 已加入且有发消息权限的 Chat ID；超级群组话题可额外填写线程 ID。

注意：路径必须是 MoviePilot 容器内的路径，不是宿主机路径；宿主机目录需要先通过 Docker volume 挂载到容器中。
