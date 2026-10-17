import json
import hashlib
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

from apscheduler.triggers.cron import CronTrigger

try:
    from Crypto.Hash import MD4 as _CryptoMD4
except ImportError:
    _CryptoMD4 = None

from app.log import logger
from app.plugins import _PluginBase

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:
    FileSystemEventHandler = None
    Observer = None

try:
    from app.modules.telegram.botapi import TelegramApiError, TelegramBotApi, TelegramNetworkError
except ImportError:
    TelegramApiError = TelegramBotApi = TelegramNetworkError = None


ED2K_PART_SIZE = 9_728_000
DEFAULT_CRON = "*/10 * * * *"
DEFAULT_EXTENSIONS = ""


def _left_rotate(value: int, amount: int) -> int:
    return ((value << amount) | (value >> (32 - amount))) & 0xFFFFFFFF


def _md4(data: bytes) -> bytes:
    """Small pure-Python MD4 implementation for environments without OpenSSL MD4."""
    message = bytearray(data)
    bit_length = len(message) * 8
    message.append(0x80)
    message.extend(b"\0" * ((56 - len(message) % 64) % 64))
    message.extend(bit_length.to_bytes(8, "little"))
    a, b, c, d = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476

    def f(x: int, y: int, z: int) -> int:
        return (x & y) | (~x & z)

    def g(x: int, y: int, z: int) -> int:
        return (x & y) | (x & z) | (y & z)

    def h(x: int, y: int, z: int) -> int:
        return x ^ y ^ z

    for offset in range(0, len(message), 64):
        block = message[offset:offset + 64]
        words = [int.from_bytes(block[index:index + 4], "little") for index in range(0, 64, 4)]
        aa, bb, cc, dd = a, b, c, d
        for index in range(0, 16, 4):
            a = _left_rotate((a + f(b, c, d) + words[index]) & 0xFFFFFFFF, 3)
            d = _left_rotate((d + f(a, b, c) + words[index + 1]) & 0xFFFFFFFF, 7)
            c = _left_rotate((c + f(d, a, b) + words[index + 2]) & 0xFFFFFFFF, 11)
            b = _left_rotate((b + f(c, d, a) + words[index + 3]) & 0xFFFFFFFF, 19)
        for position, index in enumerate((0, 4, 8, 12, 1, 5, 9, 13, 2, 6, 10, 14, 3, 7, 11, 15)):
            shift = (3, 5, 9, 13)[position % 4]
            if position % 4 == 0:
                a = _left_rotate((a + g(b, c, d) + words[index] + 0x5A827999) & 0xFFFFFFFF, shift)
            elif position % 4 == 1:
                d = _left_rotate((d + g(a, b, c) + words[index] + 0x5A827999) & 0xFFFFFFFF, shift)
            elif position % 4 == 2:
                c = _left_rotate((c + g(d, a, b) + words[index] + 0x5A827999) & 0xFFFFFFFF, shift)
            else:
                b = _left_rotate((b + g(c, d, a) + words[index] + 0x5A827999) & 0xFFFFFFFF, shift)
        for position, (index, shift) in enumerate(zip((0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15), (3, 9, 11, 15) * 4)):
            if position % 4 == 0:
                a = _left_rotate((a + h(b, c, d) + words[index] + 0x6ED9EBA1) & 0xFFFFFFFF, shift)
            elif position % 4 == 1:
                d = _left_rotate((d + h(a, b, c) + words[index] + 0x6ED9EBA1) & 0xFFFFFFFF, shift)
            elif position % 4 == 2:
                c = _left_rotate((c + h(d, a, b) + words[index] + 0x6ED9EBA1) & 0xFFFFFFFF, shift)
            else:
                b = _left_rotate((b + h(c, d, a) + words[index] + 0x6ED9EBA1) & 0xFFFFFFFF, shift)
        a = (a + aa) & 0xFFFFFFFF
        b = (b + bb) & 0xFFFFFFFF
        c = (c + cc) & 0xFFFFFFFF
        d = (d + dd) & 0xFFFFFFFF
    return b"".join(value.to_bytes(4, "little") for value in (a, b, c, d))


def ed2k_hash(path: Path, stop_event=None) -> str:
    part_hashes = []
    with path.open("rb") as file_handle:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("任务已停止")
            chunk = file_handle.read(ED2K_PART_SIZE)
            digest = _CryptoMD4.new(chunk).digest() if _CryptoMD4 else _md4(chunk)
            part_hashes.append(digest)
            if len(chunk) < ED2K_PART_SIZE:
                break
    if len(part_hashes) == 1:
        return part_hashes[0].hex()
    combined = b"".join(part_hashes)
    return (_CryptoMD4.new(combined).digest() if _CryptoMD4 else _md4(combined)).hex()


def ed2k_filename(name: str) -> str:
    return "".join(character if character.isprintable() and character not in "|%/\\?#" else quote(character, safe="") for character in name)


def ed2k_link(path: Path, file_hash: str) -> str:
    return f"ed2k://|file|{ed2k_filename(path.name)}|{path.stat().st_size}|{file_hash}|/"


def telegram_destination(value: str) -> str:
    value = value.strip()
    if value.startswith(("https://", "http://", "t.me/")):
        parsed = urlparse(value if "://" in value else f"https://{value}")
        username = parsed.path.strip("/")
        if parsed.netloc.lower() not in {"t.me", "telegram.me", "www.t.me"} or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", username):
            raise ValueError("请填写目标 @用户名 或数字 Chat ID，不要填写消息链接、私有邀请链接")
        value = f"@{username}"
    if re.fullmatch(r"-?\d+", value):
        return value
    if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]*", value):
        return f"@{value.lstrip('@')}"
    raise ValueError("接收目标格式错误：请填写数字 Chat ID 或 @用户名")


def telegram_error_hint(error: Exception) -> str:
    description = str(getattr(error, "description", error)).lower()
    if "chat not found" in description:
        return "接收目标不存在或发送机器人无权访问：个人须先向发送机器人 /start；群组/频道须加入发送机器人并授权；接收方若是机器人，请用 @用户名（非数字 Bot ID），且双方在 @BotFather 开启 Bot-to-Bot Communication"
    if getattr(error, "error_code", None) == 401:
        return "发送机器人 Token 无效，请重新核对 BotFather 提供的 Token"
    if getattr(error, "error_code", None) == 403:
        return "接收方拒绝访问：检查用户是否屏蔽机器人、机器人是否已加入群组，以及频道发消息权限"
    if "thread" in description or "topic" in description:
        return "话题线程 ID 无效或话题不可用；普通私聊/频道请留空"
    return "请检查目标、权限与网络；源文件及链接记录已保留"


class Ed2kFolderWatcher(_PluginBase):
    plugin_name = "自动生成ED2K链接"
    plugin_desc = "监控本地文件夹，为新增且稳定的文件自动生成标准 ED2K 链接。"
    plugin_icon = "https://raw.githubusercontent.com/MoviePilot/MoviePilot-Plugins/main/icons/link.png"
    plugin_version = "1.6.0"
    plugin_author = "OpenAI"
    plugin_order = 99
    plugin_config_prefix = "ed2kfolderwatcher_"
    auth_level = 1

    _enabled = False
    _folder = ""
    _recursive = True
    _cron = DEFAULT_CRON
    _output = ""
    _extensions = DEFAULT_EXTENSIONS
    _min_age = 60
    _delete_source = False
    _telegram_enabled = False
    _telegram_mode = "user"
    _telegram_token = ""
    _telegram_chat_id = ""
    _telegram_thread_id = ""
    _telegram_retry_after = 0
    _telegram_user_last_send = 0
    _monitor_enabled = True
    _output_enabled = True
    _observer = None
    _worker = None
    _stopping = None
    _pending = None
    _condition = None
    _scan_enabled = False
    _onlyonce = False
    _running = False
    _lock = threading.Lock()
    _web_login = None
    _web_login_lock = threading.Lock()

    def init_plugin(self, config: dict = None) -> None:
        self.stop_service()
        self._stopping = threading.Event()
        self._condition = threading.Condition()
        self._pending = {}
        self._lock = threading.RLock()
        self._web_login_lock = threading.Lock()
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._folder = str(config.get("folder") or "").strip()
        self._recursive = bool(config.get("recursive", True))
        self._cron = str(config.get("cron") or DEFAULT_CRON).strip()
        self._output = str(config.get("output") or "").strip()
        self._extensions = str(config.get("extensions") or DEFAULT_EXTENSIONS).strip()
        self._delete_source = bool(config.get("delete_source", False))
        self._telegram_enabled = bool(config.get("telegram_enabled", False))
        self._telegram_mode = "user"
        self._telegram_token = str(config.get("telegram_token") or "").strip()
        self._telegram_chat_id = str(config.get("telegram_chat_id") or "").strip()
        self._telegram_thread_id = str(config.get("telegram_thread_id") or "").strip()
        self._telegram_retry_after = 0
        self._telegram_user_last_send = 0
        self._monitor_enabled = bool(config.get("monitor_enabled", True))
        self._output_enabled = bool(config.get("output_enabled", True))
        try:
            self._min_age = max(0, int(config.get("min_age", 60)))
            CronTrigger.from_crontab(self._cron)
        except Exception as err:
            self._min_age = 60
            self._cron = DEFAULT_CRON
            logger.error(f"本地文件夹 ED2K 链接：配置无效：{err}")
        self._scan_enabled = bool(config.get("scan_enabled", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        if self._folder and (self._enabled or self._onlyonce):
            try:
                self._refresh_link_names()
            except (OSError, ValueError) as err:
                logger.warning(f"自动生成ED2K链接：更新历史链接文件名失败，保留原记录：{type(err).__name__}")
            self._worker = threading.Thread(target=self._work, name="ed2k-worker", daemon=True)
            self._worker.start()
        if self._onlyonce and self._folder:
            self._onlyonce = False
            self._save_config()
            self.scan()
        if self._enabled and self._folder and self._monitor_enabled:
            self._start_monitor()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_render_mode() -> Tuple[str, str]:
        return "vue", "dist/assets"

    def _get_web_login(self):
        with self._web_login_lock:
            if self._web_login is None:
                from .telegram_web import TelegramWebLogin
                from .user_account import TelegramUserAccount
                try:
                    from app.runtime.settings import get_runtime_setting
                    proxy = get_runtime_setting("PROXY")
                except ImportError:
                    proxy = None
                self._web_login = TelegramWebLogin(TelegramUserAccount(Path(self.get_data_path()), proxy=proxy))
            return self._web_login

    async def telegram_account_status(self):
        return await self._get_web_login().call("status")

    async def telegram_account_action(self, change):
        return await self._get_web_login().call(str(change.get("operation") or ""), change.get("values") or {})

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def _paths(self) -> Tuple[Path, Path, Path]:
        folder = Path(self._folder).expanduser()
        output = Path(self._output).expanduser() if self._output else folder / "ed2k.txt"
        cache = Path(self.get_data_path()) / "records.json"
        legacy = output.with_name(f".{output.name}.cache.json")
        if not cache.exists() and legacy.exists():
            records = json.loads(legacy.read_text(encoding="utf-8"))
            self._atomic_write(cache, json.dumps(records, ensure_ascii=False))
        return folder, output, cache

    def _start_monitor(self) -> None:
        if Observer is None or FileSystemEventHandler is None:
            logger.warning("自动生成ED2K链接：watchdog 不可用，实时监控未启动")
            return
        folder = Path(self._folder).expanduser()
        if not folder.is_dir():
            logger.warning(f"自动生成ED2K链接：监控目录不存在：{folder}")
            return
        plugin = self

        class Handler(FileSystemEventHandler):
            def _queue(self, path: str) -> None:
                plugin._queue_event(Path(path))

            def on_created(self, event):
                if event.is_directory:
                    for path in plugin._iter_files(Path(event.src_path)):
                        plugin._queue_event(path)
                else:
                    self._queue(event.src_path)

            def on_modified(self, event):
                if not event.is_directory:
                    self._queue(event.src_path)

            def on_moved(self, event):
                if event.is_directory:
                    for path in plugin._iter_files(Path(event.dest_path)):
                        plugin._queue_event(path)
                else:
                    self._queue(event.dest_path)

        try:
            self._observer = Observer()
            self._observer.schedule(Handler(), str(folder), recursive=self._recursive)
            self._observer.start()
            logger.info(f"自动生成ED2K链接：已启动实时文件监控：{folder}")
        except Exception as err:
            self._observer = None
            logger.error(f"自动生成ED2K链接：启动实时文件监控失败：{err}")

    def _eligible(self, path: Path) -> bool:
        folder, output, cache = self._paths()
        resolved = path.resolve()
        if path.is_symlink() or not path.is_file() or not resolved.is_relative_to(folder.resolve()):
            return False
        excluded = {output.resolve(), cache.resolve(), output.with_name(f".{output.name}.cache.json").resolve()}
        if resolved in excluded or path.name.startswith(".ed2k-write-"):
            return False
        if not self._recursive and resolved.parent != folder.resolve():
            return False
        if path.suffix.lower() in {".part", ".tmp", ".!qb", ".crdownload"}:
            return False
        extensions = self._extension_set()
        return not extensions or path.suffix.lower().lstrip(".") in extensions

    def _queue_event(self, path: Path, delay=None) -> None:
        if self._stopping.is_set():
            return
        try:
            if not self._eligible(path):
                return
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            with self._condition:
                self._pending[str(path)] = (time.monotonic() + (max(1, self._min_age) if delay is None else delay), signature)
                self._condition.notify()
        except OSError:
            return

    def _work(self) -> None:
        while not self._stopping.is_set():
            with self._condition:
                if not self._pending:
                    self._condition.wait(timeout=1)
                    continue
                key = min(self._pending, key=lambda item: self._pending[item][0])
                due, signature = self._pending[key]
                remaining = due - time.monotonic()
                if remaining > 0:
                    self._condition.wait(timeout=min(remaining, 1))
                    continue
                del self._pending[key]
            path = Path(key)
            try:
                stat = path.stat()
                if signature != (stat.st_size, stat.st_mtime_ns, stat.st_ino):
                    self._queue_event(path)
                    continue
                self._process_file(path)
            except FileNotFoundError:
                pass
            except InterruptedError:
                return
            except Exception as err:
                logger.warning(f"自动生成ED2K链接：处理失败，60秒后重试：{path}：{type(err).__name__}")
                self._queue_event(path, delay=60)

    def _extension_set(self) -> set[str]:
        return {item.strip().lower().lstrip(".") for item in self._extensions.split(",") if item.strip()}

    def _telegram_target_key(self) -> str:
        from .user_account import TelegramUserAccount
        identity = TelegramUserAccount(Path(self.get_data_path())).identity()
        value = f"user|{identity}|{self._telegram_chat_id}|{self._telegram_thread_id}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _send_telegram(self, path: Path, link: str) -> bool:
        if not self._telegram_enabled:
            return True
        return self._send_telegram_user(path, link)

        # if self._telegram_mode == "user":
        #     return self._send_telegram_user(path, link)
        # if not self._telegram_token or not self._telegram_chat_id:
        #     logger.warning("自动生成ED2K链接：Telegram 通知已启用，但 Bot Token 或 Chat ID 为空")
        #     return False
        # if TelegramBotApi is None:
        #     logger.warning("自动生成ED2K链接：当前 MoviePilot 未提供 Telegram Bot API")
        #     return False
        # if time.monotonic() < self._telegram_retry_after:
        #     return False
        # bot = None
        # try:
        #     destination = telegram_destination(self._telegram_chat_id)
        #     thread_id = int(self._telegram_thread_id) if self._telegram_thread_id else None
        #     try:
        #         from app.runtime.settings import get_runtime_setting
        #         proxy = get_runtime_setting("PROXY")
        #     except ImportError:
        #         from app.core.config import settings
        #         proxy = settings.PROXY
        #     bot = TelegramBotApi(self._telegram_token, proxy=proxy)
        #     bot.send_message(
        #         destination,
        #         f"自动生成 ED2K 链接\n文件：{path.name}\n\n{link}",
        #         disable_web_page_preview=True,
        #         message_thread_id=thread_id,
        #     )
        #     logger.info(f"自动生成ED2K链接：Telegram 通知已发送：{path.name}")
        #     return True
        # except Exception as err:
        #     cooldown = 300 if getattr(err, "error_code", None) in (400, 401, 403) or isinstance(err, ValueError) else 60
        #     self._telegram_retry_after = time.monotonic() + cooldown
        #     detail = str(err).replace(self._telegram_token, "[REDACTED]")
        #     hint = (str(err) if isinstance(err, ValueError) else telegram_error_hint(err)).replace(self._telegram_token, "[REDACTED]")
        #     logger.warning(f"自动生成ED2K链接：Telegram 发送失败：{type(err).__name__}: {detail}；{hint}；保留源文件，{cooldown}秒后允许重试；修改配置保存后立即解除冷却")
        #     return False
        # finally:
        #     if bot is not None:
        #         try:
        #             bot.close()
        #         except Exception:
        #             logger.warning("自动生成ED2K链接：关闭 Telegram 连接失败")

    def _send_telegram_user(self, path: Path, link: str) -> bool:
        if time.monotonic() < self._telegram_retry_after:
            return False
        try:
            from .user_account import TelegramUserAccount
            try:
                from app.runtime.settings import get_runtime_setting
                proxy = get_runtime_setting("PROXY")
            except ImportError:
                from app.core.config import settings
                proxy = settings.PROXY
            destination = telegram_destination(self._telegram_chat_id)
            thread_id = int(self._telegram_thread_id) if self._telegram_thread_id else None
            if thread_id is not None and thread_id <= 0:
                raise ValueError("话题线程 ID 必须为正整数或留空")
            account = TelegramUserAccount(Path(self.get_data_path()), proxy=proxy)
            remaining = max(0, 3 - (time.monotonic() - self._telegram_user_last_send))
            if self._stopping.wait(remaining):
                return False
            account.send(destination, link, thread_id=thread_id)
            self._telegram_user_last_send = time.monotonic()
            logger.info(f"自动生成ED2K链接：已使用 Telegram 个人账号发送：{path.name}")
            return True
        except Exception as err:
            from .user_account import UserAccountError
            detail = str(err) if isinstance(err, (UserAccountError, ValueError)) else f"个人账号发送异常（{type(err).__name__}）"
            cooldown = max(60, int(getattr(err, "retry_after", 300)))
            self._telegram_retry_after = time.monotonic() + cooldown
            logger.warning(f"自动生成ED2K链接：Telegram 个人账号发送失败：{detail}；保留源文件，{cooldown}秒后允许重试，不会退回 Bot 模式")
            return False

    def _notify_record(self, path: Path, record: dict) -> None:
        if not self._telegram_enabled:
            return
        target_key = self._telegram_target_key()
        if record.get("telegram_sent") and record.get("telegram_target") == target_key:
            return
        record["telegram_sent"] = self._send_telegram(path, record["link"])
        record["telegram_target"] = target_key if record["telegram_sent"] else ""

    def _iter_files(self, folder: Path):
        if self._recursive:
            iterator = os.walk(folder)
            for root, _, filenames in iterator:
                for filename in filenames:
                    yield Path(root) / filename
        else:
            for item in folder.iterdir():
                if item.is_file():
                    yield item

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        import tempfile
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".ed2k-write-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _write_results(self, output: Optional[Path], cache_path: Path, records: dict) -> None:
        self._atomic_write(cache_path, json.dumps(records, ensure_ascii=False, indent=2))
        if output is not None:
            links = sorted({item["link"] for item in records.values()})
            self._atomic_write(output, "\n".join(links) + ("\n" if links else ""))

    def _refresh_link_names(self) -> None:
        folder, output, cache_path = self._paths()
        if not cache_path.exists():
            return
        records = json.loads(cache_path.read_text(encoding="utf-8"))
        changed = False
        for record in records.values():
            parts = record["link"].split("|")
            if len(parts) != 6 or parts[:2] != ["ed2k://", "file"]:
                continue
            parts[2] = ed2k_filename(record.get("name") or unquote(parts[2]))
            link = "|".join(parts)
            if link != record["link"]:
                record["link"] = link
                changed = True
        if changed or (records and self._output_enabled):
            self._write_results(output if self._output_enabled else None, cache_path, records)

    def _process_file(self, path: Path) -> None:
        with self._lock:
            if self._stopping.is_set() or not self._eligible(path):
                return
            folder, output, cache_path = self._paths()
            output = output if self._output_enabled else None
            stat = path.stat()
            if stat.st_mtime + self._min_age > time.time():
                self._queue_event(path)
                return
            records = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
            key = str(path.resolve())
            record = records.get(key)
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino, stat.st_dev)
            unchanged = record and (record.get("size"), record.get("mtime_ns")) == signature[:2]
            legacy_boundary = record and stat.st_size > 0 and stat.st_size % ED2K_PART_SIZE == 0 and record.get("hash_version") != 2
            if unchanged and not legacy_boundary and not self._delete_source:
                if not self._telegram_enabled or (record.get("telegram_sent") and record.get("telegram_target") == self._telegram_target_key()):
                    return
            if not unchanged or legacy_boundary:
                file_hash = ed2k_hash(path, self._stopping)
                final = path.stat()
                if signature != (final.st_size, final.st_mtime_ns, final.st_ino, final.st_dev):
                    self._queue_event(path)
                    return
                record = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "name": path.name,
                          "link": ed2k_link(path, file_hash), "hash_version": 2, "telegram_sent": False, "telegram_target": "",
                          "generated_at": time.time(), "source_deleted": False}
                records[key] = record
                logger.info(f"自动生成ED2K链接：生成完成：{path}")
            self._write_results(output, cache_path, records)
            self._notify_record(path, record)
            self._write_results(output, cache_path, records)
            delivered = not self._telegram_enabled or record.get("telegram_sent")
            if not delivered:
                self._queue_event(path, delay=60)
            if self._delete_source and delivered and (output is not None or self._telegram_enabled):
                final = path.stat()
                if signature == (final.st_size, final.st_mtime_ns, final.st_ino, final.st_dev) and not self._stopping.is_set():
                    try:
                        path.unlink()
                    except OSError:
                        record["delete_failed"] = True
                        self._write_results(output, cache_path, records)
                        raise
                    record["source_deleted"] = True
                    record["deleted_at"] = time.time()
                    record.pop("delete_failed", None)
                    self._write_results(output, cache_path, records)
                    logger.info(f"自动生成ED2K链接：已删除源文件：{path}")

    def scan(self) -> None:
        if not self._folder or self._stopping.is_set():
            return
        folder, output, cache_path = self._paths()
        if not folder.is_dir():
            logger.warning(f"自动生成ED2K链接：目录不存在：{folder}")
            return
        with self._lock:
            records = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
            if self._output_enabled:
                self._write_results(output, cache_path, records)
        for path in self._iter_files(folder):
            if self._stopping.is_set():
                break
            self._queue_event(path)
        logger.info("自动生成ED2K链接：增量补扫已入队，已有链接不会重复计算")

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._folder or not self._scan_enabled:
            return []
        return [{"id": "Ed2kFolderWatcher", "name": "扫描本地文件夹并生成 ED2K", "trigger": CronTrigger.from_crontab(self._cron), "func": self.scan}]

    def get_api(self) -> List[Dict[str, Any]]:
        from .web_api import build_routes
        return build_routes(self)

    def get_page(self) -> Optional[List[dict]]:
        return [{"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
                 "text": "最近生成的 ED2K 链接记录请使用新版查看页面；修改配置请在插件卡片三个点菜单中选择设置。"}]

    def recent_records(self) -> dict:
        cache_path = Path(self.get_data_path()) / "records.json"
        try:
            records = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
            if not isinstance(records, dict) or any(not isinstance(record, dict) for record in records.values()):
                raise ValueError()
            ordered = sorted(enumerate(records.items()),
                             key=lambda item: (float(item[1][1].get("generated_at") or 0), item[0]), reverse=True)
            items = []
            for index, (path, record) in ordered[:100]:
                if record.get("source_deleted") is True:
                    source_status = "deleted"
                elif record.get("delete_failed"):
                    source_status = "delete_failed"
                elif record.get("source_deleted") is False:
                    source_status = "retained"
                else:
                    source_status = "unknown"
                items.append({"name": record.get("name") or Path(path).name, "link": record.get("link", ""),
                              "generated_at": record.get("generated_at"), "source_status": source_status,
                              "deleted_at": record.get("deleted_at")})
            return {"ok": True, "items": items, "total": len(records), "limit": 100,
                    "delete_source_enabled": self._delete_source}
        except (OSError, ValueError, TypeError):
            return {"ok": False, "message": "读取生成记录失败，原记录已保留，请检查日志或文件权限"}

    def stop_service(self) -> None:
        if self._stopping is not None:
            self._stopping.set()
        if self._condition is not None:
            with self._condition:
                self._condition.notify_all()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join()
            self._worker = None
        if self._web_login is not None:
            self._web_login.close()
            self._web_login = None

    def _save_config(self) -> None:
        self.update_config({"enabled": self._enabled, "folder": self._folder, "recursive": self._recursive, "cron": self._cron, "output": self._output, "output_enabled": self._output_enabled, "monitor_enabled": self._monitor_enabled, "scan_enabled": self._scan_enabled, "extensions": self._extensions, "min_age": self._min_age, "delete_source": self._delete_source, "telegram_enabled": self._telegram_enabled, "telegram_mode": self._telegram_mode, "telegram_token": self._telegram_token, "telegram_chat_id": self._telegram_chat_id, "telegram_thread_id": self._telegram_thread_id, "onlyonce": self._onlyonce})

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        def field(component: str, model: str, label: str, **props: Any) -> dict:
            if "persistent_hint" in props:
                props["persistent-hint"] = props.pop("persistent_hint")
            props.update({"model": model, "label": label, "class": "mb-5"})
            return {"component": component, "props": props}

        return [{"component": "VForm", "content": [
            field("VSwitch", "enabled", "启用插件"),
            field("VTextField", "folder", "监控目录", placeholder="/downloads/complete", hint="必须是 MoviePilot 容器内可见的本地路径。", persistent_hint=True),
            field("VSwitch", "recursive", "扫描子目录"),
            field("VSwitch", "monitor_enabled", "监控新增文件", hint="文件创建或移动到监控目录后自动处理；定时补扫仍可作为兜底。", persistent_hint=True),
            field("VSwitch", "output_enabled", "生成本地 TXT 文件", hint="关闭后只保存内部缓存，并可继续发送 Telegram；开启后写入下面的路径。", persistent_hint=True),
            field("VTextField", "output", "输出文件路径", placeholder="留空则写入监控目录/ed2k.txt"),
            field("VTextField", "extensions", "文件扩展名过滤", placeholder="留空处理全部文件，例如 mkv,mp4,flac"),
            field("VTextField", "min_age", "文件稳定等待秒数", type="number", hint="创建、移动或修改后等待稳定；持续写入会自动延后处理。", persistent_hint=True),
            field("VSwitch", "delete_source", "生成完成后删除源文件", hint="仅在 TXT 写入成功且已启用的 TG 发送成功后删除；两种输出都关闭时不删除。", persistent_hint=True),
            field("VSwitch", "scan_enabled", "启用定时增量补扫", hint="默认关闭；实时监控无需定时计划。网络挂载漏事件时可开启兜底。", persistent_hint=True),
            field("VTextField", "cron", "补扫计划（开启补扫时生效）", hint="默认每 10 分钟：*/10 * * * *", persistent_hint=True),
            field("VSwitch", "telegram_enabled", "发送 ED2K 链接到 Telegram"),
            # field("VSelect", "telegram_mode", "Telegram 发送方式", items=[{"title": "Bot Token（机器人）", "value": "bot"}, {"title": "个人账号（Telethon）", "value": "user"}]),
            # field("VTextField", "telegram_token", "Telegram Bot Token", type="password"),
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-5"}, "text": "仅使用个人账号发送，可在插件页面完成授权；请使用可信网络或 HTTPS。"},
            field("VTextField", "telegram_chat_id", "Telegram 接收目标", placeholder="数字 Chat ID、@用户名 或公开 t.me 链接", hint="可直接填接收机器人 @用户名；数字目标须是该账号已认识的聊天，推荐使用 @用户名。", persistent_hint=True),
            field("VTextField", "telegram_thread_id", "Telegram 话题线程 ID", placeholder="普通私聊、群组、频道留空", hint="仅带话题的超级群组使用，表示将消息发到哪个话题，不是 Chat ID。", persistent_hint=True),
            field("VSwitch", "onlyonce", "保存后立即扫描一次"),
        ]}], {"enabled": False, "folder": "", "recursive": True, "monitor_enabled": True, "scan_enabled": False, "output_enabled": True, "output": "", "extensions": "", "min_age": 60, "delete_source": False, "telegram_enabled": False, "telegram_mode": "user", "telegram_chat_id": "", "telegram_thread_id": "", "cron": DEFAULT_CRON, "onlyonce": False}
