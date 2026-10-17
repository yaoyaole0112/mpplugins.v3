# 自动生成ED2K链接

MoviePilot 本地目录 ED2K 插件。路径填写容器内路径，例如 `/media/movies`，而非未挂载的宿主机路径。

链接中直接保留中文等可显示 Unicode 文件名，空格、`|`、`%`、换行等字符继续进行百分号转义，避免破坏链接结构。升级启用后自动更新历史缓存及已启用的 TXT，只改文件名显示，不重新计算哈希，不重发 Telegram，也不删除源文件。

## 实时监控与增量处理

- 默认启用实时监控：创建、移动、修改文件后，等待指定稳定时间再处理；持续写入会延迟处理。文件夹整体移入也会发现其中的文件。
- 单工作线程串行哈希，重复事件合并；处理中发生变化会重新入队。临时下载文件、符号链接、插件输出和缓存不参与生成。
- 默认不启用定时任务。网络挂载等可能丢失文件事件的环境，可开启「定时增量补扫」作为兜底。
- 保存后勾选「立即扫描一次」可补齐现有文件；平时实时监控仅响应新的事件，不会主动重新哈希整个目录。
- 历史链接累计保留，已删除源文件的记录也保留。相同路径大小和修改时间未变时不重新哈希。修改原内容会更新该路径的链接。

## TXT 输出与删除

- 「生成本地 TXT 文件」默认开启，输出路径留空使用监控目录下 `ed2k.txt`。关闭后不会创建或改写 TXT，也不会删除已有 TXT。
- 增量缓存存放在 MoviePilot 的插件数据目录 `Ed2kFolderWatcher/records.json`，与 TXT 路径无关。首次使用自动迁移原输出路径旁的 `.ed2k.txt.cache.json`。
- TXT 和缓存分别使用临时文件、刷新磁盘、原子替换，防止写入一半留下空白清单。
- 「删除源文件」默认关闭。启用时，缓存和已启用 TXT 必须成功保存，已启用 Telegram 必须发送成功，且删除前大小、时间、设备和 inode 未变，才允许删除。
- TXT、Telegram 都关闭时绝不自动删除源文件。请先用测试目录验证删除行为。TXT 和缓存各自原子写入，并非跨文件事务；写入失败会保留源文件并重试。

## Telegram 配置

### 个人账号模式（发送给第三方接收机器人）

选择「Telegram 发送方式 → 个人账号（Telethon）」。这时不使用 Bot Token，也不要求接收方开启机器人互发。使用独立的个人账号 Session，不导入其他插件的会话，不监听或自动回复聊天消息。

1. 在 `my.telegram.org` 用你自己的账号申请 API ID/API Hash。
2. 在服务器的**交互终端**执行（容器名称不是 `moviepilot` 时自行替换）：

   ```bash
   docker exec -it moviepilot python /app/app/plugins/ed2kfolderwatcher/telegram_user_login.py --data-dir /config/plugins/Ed2kFolderWatcher
   ```

3. 按提示输入 API ID、API Hash、手机号、登录验证码及可能需要的两步验证密码。除 API ID 外均隐藏输入；验证码、手机号和密码不保存到插件配置或凭证 JSON。不要在聊天里提供这些信息。
4. 在插件中保留接收机器人 `@用户名`，保存配置并勾选一次「立即扫描」，补发已生成但未成功发送的链接。

实际数据目录以插件「查看数据」页面展示的命令为准。只验证授权、不发送消息：

```bash
docker exec -it moviepilot python /app/app/plugins/ed2kfolderwatcher/telegram_user_login.py --data-dir /config/plugins/Ed2kFolderWatcher --status
```

- 个人账号模式仅发送完整 ED2K 链接，不加文件标题或其他说明，方便接收机器人解析。接收方服务如果要求先 `/start` 或注册，请用自己的账号先完成。
- 授权文件仅在插件数据目录 `telegram-user/`：`credentials.json` 保存 API ID/API Hash 和用户 ID，`account.session` 保存 Telegram 登录会话。目录权限 700，文件权限 600，源码内不存凭证，也不会上传 GitHub。备份 MoviePilot 数据时也要保护这些文件。
- Session 具有账号访问权限，不是普通配置。可在 Telegram「设置 → 设备」中撤销本工具的授权；再次使用需重新运行登录命令。使用不同 API ID/API Hash 重新授权时，先停用插件并安全移除旧 `telegram-user/` 数据，再登录；不要共享同一 Session 给其他程序。
- 登录与发送使用文件锁，防止同时操作 SQLite 会话。发送使用独立事件循环和连接，最多等待 60 秒；沿用 MoviePilot 的 HTTP/SOCKS 代理设置（不支持 HTTPS 代理隧道）。同一插件成功发送间隔至少 3 秒，并遵守 Telegram 返回的限速等待。
- 未登录、会话失效或权限不足时保留链接与源文件，不会退回 Bot 身份发送。登录完成后保存插件配置解除冷却，再立即扫描一次。
- 依赖见 `requirements.txt`；当前服务器已具备 Telethon。其他环境应通过插件依赖安装流程安装，插件不会自行联网执行 pip。
- 不保证严格恰好一次：网络超时、发送成功后缓存保存失败或进程退出时，补发可能产生重复消息。第三方接收服务是否成功解析，不等于 Telegram 投递成功。

### Bot Token 模式（保留兼容）

- **Bot Token 是发送者凭证**：程序调用 Telegram Bot API 需要以某个机器人身份发送消息，不是输入接收方用户名就能匿名发送。从 `@BotFather` 获取发送机器人 Token。
- **Chat ID 是接收位置**：用户私聊 ID、群组/频道的 `-100…` ID，或公开频道 `@channel_username`。私聊用户须先启动机器人；群组须加入机器人，频道须授予发消息权限。
- **话题线程 ID 可留空**：只在启用了话题的超级群组中指定某一个话题使用。普通私聊、普通群组、频道不填；它不是群组 Chat ID。
- **接收方是另一个机器人**：填写它的 `@用户名`，不要填数字 Bot ID；发送与接收机器人双方都必须在 `@BotFather` 开启 **Bot-to-Bot Communication Mode**。若第三方接收机器人没有开启该模式，插件无法替它开启，需联系其所有者。此前“机器人完全无法互发”的说明已更正，依据当前官方功能文档。
- 接收目标也支持公开 `https://t.me/username` 链接或不带 `@` 的用户名，自动规范化。消息链接和私有邀请链接不能代替 Chat ID。
- 使用 MoviePilot 配置的网络代理。日志隐藏当前 Token；发送失败会保留记录。网络失败冷却 60 秒；配置/权限错误冷却 300 秒，避免大量文件重复刷屏。修改配置并保存后立即解除冷却。
- `400: chat not found` 表示目标不存在或当前发送机器人不可访问，而非网络不通。个人先向发送机器人发 `/start`；群组添加发送机器人；频道授予发消息权限。不要将发送机器人的 Bot ID 当成自己的个人 Chat ID。
- 网络超时或发送成功后进程在记录状态前退出，可能重复发送，不保证严格恰好一次。重启后的待处理文件可用「立即扫描一次」或增量补扫重新入队。

## 哈希实现与参考

采用 9,728,000 字节分块的 eMule 兼容 ED2K。小文件直接 MD4，大文件合并各块 MD4 后再次 MD4；文件恰为整块倍数时包含尾部空块 MD4。优先使用 PyCryptodome 原生 MD4，无该模块时使用纯 Python 回退。实现规则对照 RHash 的 `librhash/ed2k.c`；NanShare 当前部署的是编译后程序，未取得可核验的 ED2K 源码。

40 项隔离测试覆盖个人账号发送路由、账号/模式去重隔离、纯链接与数字目标、未授权/机器人 Session 拒绝、会话权限、并发锁、凭证保存、错误脱敏、代理与 FloodWait，以及原有目录监控、增量、哈希、TXT、Telegram Bot 和安全删除。六组哈希向量（空文件、1 字节、分块边界前后、两整块）已与独立 RHash 库逐项比对。测试使用模拟个人账号客户端，实际个人账号登录及投递必须由账号所有者完成，发布时不声称已经完成真实发送。

参考：
- RHash: https://github.com/rhash/RHash/blob/master/librhash/ed2k.c
- Telegram 机器人互发: https://core.telegram.org/bots/features#bot-to-bot-communication
- Telegram sendMessage / message_thread_id: https://core.telegram.org/bots/api#sendmessage
- Telegram API 凭证: https://core.telegram.org/api/obtaining_api_id
- Telethon 登录与 Session: https://docs.telethon.dev/en/stable/basic/signing-in.html
