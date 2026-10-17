# 本地文件夹 ED2K 链接

在 MoviePilot 的「插件管理 → 本地插件安装」中加载后，配置 MoviePilot 容器内可见的本地目录，例如 `/downloads/complete` 或 `/media/movies`。

插件会按计划扫描目录，等待文件修改时间超过稳定等待秒数后计算标准 ED2K 哈希，并将链接写入 `ed2k.txt`。默认同时写入隐藏缓存文件 `.ed2k.txt.cache.json`，文件大小和修改时间未变化时不会重复计算。

注意：路径必须是 MoviePilot 容器内的路径，不是宿主机路径；宿主机目录需要先通过 Docker volume 挂载到容器中。
