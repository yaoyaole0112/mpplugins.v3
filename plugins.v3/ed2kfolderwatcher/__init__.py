import json
import hashlib
import os
import re
import stat as stat_module
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

from apscheduler.triggers.cron import CronTrigger
from .file_size import minimum_file_bytes, minimum_gb_setting, minimum_gb_value
from .file_order import filename_order

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
EMPTY_MD4 = "31d6cfe0d16ae931b73c59d7e0c089c0"
TELEGRAM_MESSAGE_LIMIT = 3800
CLOUD_LIFE_SILENT_SKIP_REASONS = {"不在所选网盘存储或监控目录", "子目录未开启监控"}


def record_problem(record: dict) -> str:
    parts = str(record.get("link", "")).split("|")
    if record.get("size") == 0 or (len(parts) == 6 and parts[3] == "0"):
        return "历史链接无效：当时文件大小为 0；需要原始完整文件重新生成"
    if len(parts) != 6 or parts[:2] != ["ed2k://", "file"] or parts[-1] != "/" or not parts[3].isdigit() or int(parts[3]) <= 0 or not re.fullmatch(r"[0-9a-fA-F]{32}", parts[4]):
        return "链接格式或文件大小无效，需要重新生成"
    if parts[4].lower() == EMPTY_MD4 or (record.get("size") is not None and int(parts[3]) != record["size"]):
        return "链接哈希或大小不一致，需要原始完整文件重新生成"
    return ""


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
        initial = os.fstat(file_handle.fileno())
        bytes_read = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("任务已停止")
            chunk = file_handle.read(ED2K_PART_SIZE)
            bytes_read += len(chunk)
            digest = _CryptoMD4.new(chunk).digest() if _CryptoMD4 else _md4(chunk)
            part_hashes.append(digest)
            if len(chunk) < ED2K_PART_SIZE:
                break
        final = os.fstat(file_handle.fileno())
        if bytes_read != initial.st_size or (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (final.st_size, final.st_mtime_ns, final.st_ctime_ns):
            raise OSError("读取字节数或文件状态变化，拒绝生成链接")
    if len(part_hashes) == 1:
        return part_hashes[0].hex()
    combined = b"".join(part_hashes)
    return (_CryptoMD4.new(combined).digest() if _CryptoMD4 else _md4(combined)).hex()


def ed2k_filename(name: str) -> str:
    return "".join(character if character.isprintable() and character not in "|%/\\?#" else quote(character, safe="") for character in name)


def ed2k_link(path: Path, file_hash: str, size=None) -> str:
    return f"ed2k://|file|{ed2k_filename(path.name)}|{path.stat().st_size if size is None else size}|{file_hash}|/"


def telegram_destination(value: str) -> str:
    from .telegram_targets import telegram_destination as normalize
    return normalize(value)


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


def telegram_aggregate_key(name: str):
    match = re.search(r"(?:^|[. _-])S(\d{1,3})(?:E\d{1,4})?(?:[. _-]|$)", name, re.IGNORECASE)
    if not match:
        return None
    title = re.sub(r"[. _-]+$", "", name[:match.start()]).strip()
    if not title:
        return None
    return title.casefold(), int(match.group(1))


def telegram_message_chunks(records: list) -> list:
    chunks = []
    current = []
    length = 0
    for record in records:
        link = record["link"]
        extra = len(link) + (1 if current else 0)
        if current and length + extra > TELEGRAM_MESSAGE_LIMIT:
            chunks.append(current)
            current = []
            length = 0
        current.append(record)
        length += len(link) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append(current)
    return chunks


class Ed2kFolderWatcher(_PluginBase):
    plugin_name = "自动生成ED2K链接"
    plugin_desc = "监控本地目录或增量扫描115/CloudDrive存储，流式生成ED2K链接。"
    plugin_icon = "https://raw.githubusercontent.com/MoviePilot/MoviePilot-Plugins/main/icons/link.png"
    plugin_version = "1.17.2"
    plugin_author = "helios"
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
    _min_file_size = ""
    _delete_source = False
    _cloud_delete_enabled = False
    _cloud_delete_scope = ""
    _delete_small_files = False
    _telegram_enabled = False
    _telegram_aggregate = False
    _telegram_aggregate_window = 600.0
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
    _pending_batches = None
    _batches = None
    _batch_sequence = 0
    _active_batch_id = None
    _live_batch_id = None
    _live_batch_until = 0
    _condition = None
    _scan_enabled = False
    _cloud_enabled = False
    _cloud_scan_enabled = False
    _cloud_storage = "115网盘Plus"
    _cloud_folder = ""
    _cloud_recursive = True
    _cloud_cron = DEFAULT_CRON
    _cloud_read_timeout = 300
    _cloud_monitor_enabled = False
    _cloud_life_source = "shared"
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
        self._pending_batches = {}
        self._batches = {}
        self._batch_sequence = 0
        self._active_batch_id = None
        self._live_batch_id = None
        self._live_batch_until = 0
        self._cloud_iterator = None
        self._cloud_adapter = None
        self._cloud_status = {"scanning": False, "file": "", "bytes_read": 0, "size": 0, "error": "", "completed": 0, "last_scan": None}
        self._cloud_observed = {}
        self._cloud_failures = {}
        self._cloud_life_lock = threading.Lock()
        self._cloud_life_cursor = 0
        self._cloud_life_started = time.time()
        self._cloud_life_failures = 0
        self._cloud_life_retry_after = 0
        self._cloud_life_candidates = {}
        self._cloud_scan_batch_id = None
        self._cloud_status.update(reconnecting=False, stream_retries=0, retry_at=None, error_key="", life_last_poll=None, life_error="", life_events=0, life_retry_at=None, life_truncated=False)
        self._lock = threading.RLock()
        self._web_login_lock = threading.Lock()
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        shared_recursive = config.get("include_subdirectories")
        if "include_subdirectories" in config and not isinstance(shared_recursive, bool):
            self._enabled = False
            logger.error("自动生成ED2K链接：请明确选择是否包含子目录，已停止插件")
        self._folder = str(config.get("folder") or "").strip()
        try:
            from .cloud_storage import monitor_roots
            self._folder = ",".join(monitor_roots(self._folder))
        except ValueError as failure:
            self._enabled = False
            self._folder = ""
            logger.error(f"自动生成ED2K链接：本地目录配置无效：{failure}")
        self._recursive = shared_recursive if isinstance(shared_recursive, bool) else bool(config.get("recursive", True))
        self._cron = str(config.get("cron") or DEFAULT_CRON).strip()
        self._output = str(config.get("output") or "").strip()
        self._extensions = str(config.get("extensions") or DEFAULT_EXTENSIONS).strip()
        self._min_file_size = str(config.get("min_file_size") or "").strip()
        try:
            if "min_file_size_gb" in config:
                self._min_file_size = minimum_gb_setting(config["min_file_size_gb"], previous=self._min_file_size if "min_file_size" in config else None)
            minimum_file_bytes(self._min_file_size)
        except ValueError as failure:
            self._enabled = False
            logger.error(f"自动生成ED2K链接：{failure}，已停止插件，请修正配置")
        self._delete_source = bool(config.get("delete_source", False))
        self._cloud_delete_enabled = config.get("cloud_delete_enabled") is True
        self._cloud_delete_scope = str(config.get("cloud_delete_scope") or "")
        self._delete_small_files = bool(config.get("delete_small_files", False))
        self._telegram_enabled = bool(config.get("telegram_enabled", False))
        self._telegram_aggregate = bool(config.get("telegram_aggregate", False))
        try:
            self._telegram_aggregate_window = max(0, min(3600, int(config.get("telegram_aggregate_window", 600))))
        except (TypeError, ValueError):
            self._telegram_aggregate_window = 600
        self._telegram_mode = "user"
        self._telegram_token = str(config.get("telegram_token") or "").strip()
        self._telegram_chat_id = str(config.get("telegram_chat_id") or "").strip()
        self._telegram_thread_id = str(config.get("telegram_thread_id") or "").strip()
        self._telegram_retry_after = 0
        self._telegram_target_retry = {}
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
        self._cloud_enabled = bool(config.get("cloud_enabled", False))
        self._cloud_scan_enabled = bool(config.get("cloud_scan_enabled", self._cloud_enabled))
        self._cloud_storage = str(config.get("cloud_storage") or "115网盘Plus").strip()
        self._cloud_folder = str(config.get("cloud_folder") or "").strip()
        self._cloud_recursive = shared_recursive if isinstance(shared_recursive, bool) else bool(config.get("cloud_recursive", True))
        self._cloud_cron = str(config.get("cloud_cron") or DEFAULT_CRON).strip()
        try:
            self._cloud_read_timeout = max(30, min(1800, int(config.get("cloud_read_timeout", 300))))
        except (ValueError, TypeError):
            self._cloud_read_timeout = 300
        self._cloud_monitor_enabled = bool(config.get("cloud_monitor_enabled", False))
        self._cloud_life_source = str(config.get("cloud_life_source") or "shared")
        if self._cloud_storage != "115网盘Plus" or self._cloud_life_source not in {"shared", "direct"}:
            self._cloud_monitor_enabled = False
        try:
            CronTrigger.from_crontab(self._cloud_cron)
        except (ValueError, TypeError):
            self._cloud_cron = DEFAULT_CRON
        if self._cloud_enabled:
            from .cloud_storage import SUPPORTED_STORAGES, monitor_roots, CloudError
            try:
                if self._cloud_storage not in SUPPORTED_STORAGES:
                    raise CloudError("不支持此网盘存储")
                roots = monitor_roots(self._cloud_folder, cloud=True)
                if not roots:
                    raise CloudError("请至少指定一个网盘监控目录")
                self._cloud_folder = ",".join(roots)
            except ValueError as failure:
                self._cloud_enabled = False
                self._cloud_status["error"] = str(failure)
                logger.warning(f"自动生成ED2K链接：网盘来源配置无效：{failure}")
        self._onlyonce = bool(config.get("onlyonce", False))
        if (self._folder or self._cloud_enabled) and (self._enabled or self._onlyonce):
            try:
                self._refresh_link_names()
            except (OSError, ValueError) as err:
                logger.warning(f"自动生成ED2K链接：更新历史链接文件名失败，保留原记录：{type(err).__name__}")
            self._worker = threading.Thread(target=self._work, name="ed2k-worker", daemon=True)
            self._worker.start()
        if self._onlyonce and (self._folder or self._cloud_enabled):
            self._onlyonce = False
            self._save_config()
            self.scan()
            if self._cloud_enabled:
                self.queue_cloud_scan()
        if self._enabled and self._folder and self._monitor_enabled:
            self._start_monitor()
        if self._enabled and self._cloud_enabled and self._cloud_delete_enabled and self._delete_source:
            self._restore_cloud_delete_checks()

    def _restore_cloud_delete_checks(self):
        try:
            _, _, cache_path = self._paths()
            with self._lock:
                records = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
                for record in records.values():
                    if record.get("cloud_delete_state") != "check_retry" or record.get("source_deleted"):
                        continue
                    if record.get("cloud_delete_scope") != self._cloud_delete_scope or not self._cloud_delete_scope:
                        continue
                    if record.get("cloud_delete_check_attempts", 0) >= 3 or record.get("cloud_delete_attempts", 0) >= 3:
                        continue
                    identity = record.get("cloud_fingerprint")
                    if not isinstance(identity, list) or len(identity) != 6 or record_problem(record):
                        continue
                    item = dict(zip(("storage", "fileid", "path", "name", "size", "modify_time"), identity))
                    item["type"] = "file"
                    if item["storage"] == "115网盘Plus" and self._cloud_item_is_eligible(item):
                        self._queue_cloud_file(item, delay=max(1, record.get("cloud_delete_retry_at", 0) - time.time()))
        except (OSError, ValueError, TypeError) as failure:
            logger.warning(f"自动生成ED2K链接：恢复删源校验队列失败，保留源文件（{type(failure).__name__}）")

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
        from .cloud_storage import monitor_roots
        roots = monitor_roots(self._folder)
        folder = Path(roots[0]) if roots else Path()
        output = Path(self._output).expanduser() if self._output else (folder if self._folder else Path(self.get_data_path())) / "ed2k.txt"
        cache = Path(self.get_data_path()) / "records.json"
        legacy = output.with_name(f".{output.name}.cache.json")
        if not cache.exists() and legacy.exists():
            records = json.loads(legacy.read_text(encoding="utf-8"))
            self._atomic_write(cache, json.dumps(records, ensure_ascii=False))
        return folder, output, cache

    def _local_roots(self):
        from .cloud_storage import monitor_roots
        roots = [Path(root) for root in monitor_roots(self._folder)]
        unique = {}
        for root in roots:
            unique.setdefault(root.resolve(), root)
        return [root for resolved, root in unique.items() if not self._recursive or not any(
            resolved != parent and resolved.is_relative_to(parent) for parent in unique)]

    def _local_root_for(self, path):
        resolved = path.resolve()
        for root in self._local_roots():
            folder = root.resolve()
            if resolved.is_relative_to(folder) and (self._recursive or resolved.parent == folder):
                return root
        return None

    def _start_monitor(self) -> None:
        if Observer is None or FileSystemEventHandler is None:
            logger.warning("自动生成ED2K链接：watchdog 不可用，实时监控未启动")
            return
        folders = []
        for folder in self._local_roots():
            if folder.is_dir():
                folders.append(folder)
            else:
                logger.warning(f"自动生成ED2K链接：监控目录不存在：{folder}")
        if not folders:
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
            handler = Handler()
            for folder in folders:
                self._observer.schedule(handler, str(folder), recursive=self._recursive)
            self._observer.start()
            logger.info(f"自动生成ED2K链接：已启动实时文件监控，共 {len(folders)} 个目录")
        except Exception as err:
            if self._observer is not None:
                self._observer.stop()
                if self._observer.is_alive():
                    self._observer.join(timeout=10)
            self._observer = None
            logger.error(f"自动生成ED2K链接：启动实时文件监控失败：{err}")

    def _eligible(self, path: Path) -> bool:
        folder, output, cache = self._paths()
        resolved = path.resolve()
        if path.is_symlink() or not path.is_file() or self._local_root_for(path) is None:
            return False
        excluded = {output.resolve(), cache.resolve(), output.with_name(f".{output.name}.cache.json").resolve()}
        if resolved in excluded or path.name.startswith(".ed2k-write-"):
            return False
        if path.suffix.lower() in {".part", ".tmp", ".!qb", ".crdownload"}:
            return False
        extensions = self._extension_set()
        return not extensions or path.suffix.lower().lstrip(".") in extensions

    def _new_batch(self, source: str) -> str:
        with self._condition:
            self._batch_sequence += 1
            batch_id = f"{source}-{self._batch_sequence}"
            self._batches[batch_id] = {"source": source, "created": time.time(), "deadline": time.monotonic() + 1800,
                                       "expected": set(), "results": set(), "sealed": False, "flushing": False,
                                       "contexts": {}, "groups": {}, "group_expected": {}, "group_results": {},
                                       "group_flush_at": {}, "group_flushing": set(), "group_flushed": set()}
        return batch_id

    def _merge_batch_for_group(self, batch_id: str, group_key):
        if not self._telegram_aggregate or group_key is None:
            return batch_id
        now = time.monotonic()
        with self._condition:
            for candidate_id, candidate in reversed(list(self._batches.items())):
                if candidate_id == batch_id or not candidate["sealed"] or candidate["flushing"]:
                    continue
                if group_key not in candidate.get("group_expected", {}) or group_key in candidate.get("group_flushing", set()):
                    continue
                if group_key in candidate.get("group_flushed", set()) or candidate.get("group_flush_at", {}).get(group_key, 0) <= now:
                    continue
                candidate["group_flush_at"][group_key] = now + self._telegram_aggregate_window
                return candidate_id
        return batch_id

    def _seal_batch(self, batch_id: str) -> None:
        if not batch_id:
            return
        empty = False
        with self._condition:
            batch = self._batches.get(batch_id)
            if batch:
                batch["sealed"] = True
                flush_at = time.monotonic() + self._telegram_aggregate_window if self._telegram_enabled and self._telegram_aggregate else time.monotonic()
                for group_key in batch.get("group_expected", {}):
                    if group_key not in batch["group_flushed"] and group_key not in batch["group_flushing"]:
                        batch["group_flush_at"][group_key] = flush_at
                empty = not batch.get("group_expected")
                if empty:
                    self._batches.pop(batch_id, None)
        if not empty:
            self._maybe_flush_batch(batch_id)

    def _maybe_flush_batch(self, batch_id: str) -> None:
        with self._condition:
            batch = self._batches.get(batch_id)
            if not batch or batch["flushing"] or not batch["sealed"]:
                return
            now = time.monotonic()
            ready = [group_key for group_key, expected in batch.get("group_expected", {}).items()
                     if group_key not in batch["group_flushing"]
                     and group_key not in batch["group_flushed"]
                     and expected.issubset(batch["group_results"].get(group_key, set()))
                     and batch["group_flush_at"].get(group_key, 0) <= now]
            if not ready:
                return
            batch["group_flushing"].update(ready)
        self._flush_batch(batch_id)

    def _expire_batch(self, batch_id: str) -> None:
        with self._condition:
            batch = self._batches.get(batch_id)
        if not batch:
            return
        with self._condition:
            pending = [key for key in batch["expected"] - batch["results"]
                       if self._pending_batches.get(key) == batch_id]
        if pending:
            retry_batch = self._new_batch(f"{batch['source']}-timeout")
            for key in pending:
                with self._condition:
                    self._pending_batches[key] = retry_batch
                    name = self._batch_item_name(key)
                    self._register_batch_item(retry_batch, key, name)
                    batch["results"].add(key)
                    group_key = batch["groups"].get(key)
                    if group_key is not None:
                        batch["group_results"].setdefault(group_key, set()).add(key)
            self._seal_batch(retry_batch)
            logger.warning(f"自动生成ED2K链接：批次等待超过30分钟，{len(pending)}个未完成文件转入后续批次：{batch_id}")
        self._seal_batch(batch_id)

    def _flush_batch(self, batch_id: str) -> None:
        with self._condition:
            batch = self._batches.get(batch_id)
            group_keys = list(batch.get("group_flushing", set())) if batch else []
        for group_key in group_keys:
            self._flush_group(batch_id, group_key)

    def _flush_group(self, batch_id: str, group_key) -> None:
        with self._condition:
            batch = self._batches.get(batch_id)
            if not batch:
                return
            entries = [(key, context) for key, context in batch["contexts"].items()
                       if batch["groups"].get(key) == group_key]
        if not entries:
            with self._condition:
                batch = self._batches.get(batch_id)
                if batch:
                    batch["group_flushing"].discard(group_key)
                    batch["group_flushed"].add(group_key)
                    if batch["group_flushed"] >= set(batch["group_expected"]):
                        self._batches.pop(batch_id, None)
            return
        cache_path = entries[0][1]["cache_path"]
        output = entries[0][1]["output"]
        try:
            records = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        except (OSError, ValueError):
            records = {}
        eligible = [(key, context, records.get(key)) for key, context in entries]
        eligible = [(key, context, record) for key, context, record in eligible if record]
        grouped = {}
        for key, context, record in eligible:
            if record.get("telegram_sent"):
                continue
            grouped.setdefault(group_key, []).append(record)
        if self._telegram_enabled:
            for unsent in grouped.values():
                self._notify_records(unsent, checkpoint=lambda: self._write_results(output, cache_path, records))
            self._write_results(output, cache_path, records)
        failed = []
        for key, context, unused in eligible:
            record = records.get(key)
            if not record:
                continue
            if self._telegram_enabled and not record.get("telegram_sent"):
                failed.append(context)
                continue
            if context["kind"] == "cloud":
                self._maybe_delete_cloud_source(context["storage"], context["item"], record, output, cache_path, records, self._stopping)
            elif self._delete_source and (output is not None or self._telegram_enabled):
                path = context["path"]
                try:
                    final = path.stat()
                    if context["signature"] == (final.st_size, final.st_mtime_ns, final.st_ino, final.st_dev) and not self._stopping.is_set():
                        self._unlink_source(path, final)
                        record["source_deleted"] = True
                        record["deleted_at"] = time.time()
                        record.pop("delete_failed", None)
                        self._write_results(output, cache_path, records)
                except OSError:
                    record["delete_failed"] = True
                    self._write_results(output, cache_path, records)
        if failed:
            retry_batch = self._new_batch(f"{batch['source']}-retry")
            for context in failed:
                if context["kind"] == "cloud":
                    self._queue_cloud_file(context["item"], delay=300, batch_id=retry_batch)
                else:
                    self._queue_event(context["path"], delay=60, batch_id=retry_batch)
            self._seal_batch(retry_batch)

        with self._condition:
            batch = self._batches.get(batch_id)
            if batch:
                batch["group_flushing"].discard(group_key)
                batch["group_flushed"].add(group_key)
                if batch["group_flushed"] >= set(batch["group_expected"]):
                    self._batches.pop(batch_id, None)

    def _live_batch(self) -> str:
        with self._condition:
            now = time.monotonic()
            expired = self._live_batch_id and now >= self._live_batch_until
            missing = self._live_batch_id and self._live_batch_id not in self._batches
            old_batch = self._live_batch_id if expired or missing else None
            if old_batch:
                self._live_batch_id = None
        if old_batch:
            self._seal_batch(old_batch)
        with self._condition:
            if not self._live_batch_id or self._live_batch_id not in self._batches:
                self._live_batch_id = self._new_batch("local-live")
            self._live_batch_until = time.monotonic() + 10
            return self._live_batch_id

    def _batch_item_name(self, key: str, item=None) -> str:
        if item is not None:
            return str(item.get("name") or item.get("path") or key) if isinstance(item, dict) else str(item)
        pending = self._pending.get(key)
        if isinstance(pending, tuple) and len(pending) > 1 and isinstance(pending[1], dict):
            return str(pending[1].get("name") or pending[1].get("path") or key)
        return Path(key).name if not key.startswith("cloud:") else key

    def _register_batch_item(self, batch_id: str, key: str, name: str = None) -> None:
        with self._condition:
            batch = self._batches.get(batch_id)
            if batch:
                item_name = name or self._batch_item_name(key)
                group_key = telegram_aggregate_key(item_name) if self._telegram_aggregate else ("__single__", key)
                target_batch_id = self._merge_batch_for_group(batch_id, group_key)
                if target_batch_id != batch_id:
                    batch = self._batches.get(target_batch_id)
                if batch:
                    batch["expected"].add(key)
                    batch["groups"][key] = group_key
                    batch["group_expected"].setdefault(group_key, set()).add(key)
                    batch["group_results"].setdefault(group_key, set())
                    return target_batch_id
        return batch_id

    def _queue_event(self, path: Path, delay=None, batch_id=None) -> None:
        if self._stopping.is_set():
            return
        try:
            if not self._eligible(path):
                return
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            batch_id = batch_id or self._active_batch_id or self._live_batch()
            completed_old = None
            with self._condition:
                old_batch = self._pending_batches.get(str(path))
                if old_batch and old_batch != batch_id:
                    old_state = self._batches.get(old_batch)
                    if old_state:
                        old_state["results"].add(str(path))
                        completed_old = old_batch
                self._pending[str(path)] = (time.monotonic() + (max(1, self._min_age) if delay is None else delay), signature)
                batch_id = self._register_batch_item(batch_id, str(path), path.name)
                self._pending_batches[str(path)] = batch_id
                self._condition.notify()
            if completed_old:
                self._maybe_flush_batch(completed_old)
        except OSError:
            return

    def _next_pending_key(self):
        now = time.monotonic()
        ready = [key for key, (due, _) in self._pending.items() if due <= now]
        def order(key):
            item = self._pending[key][1]
            path = item.get("path", key) if key.startswith("cloud:") else key
            return key != "__cloud_scan__", filename_order(path)
        return min(ready, key=order) if ready else min(self._pending, key=lambda key: self._pending[key][0])

    def _work(self) -> None:
        while not self._stopping.is_set():
            flush = []
            expired = []
            with self._condition:
                now = time.monotonic()
                if self._live_batch_id and now >= self._live_batch_until:
                    flush.append(self._live_batch_id)
                    self._live_batch_id = None
                ready = [batch_id for batch_id, batch in self._batches.items()
                         if batch["sealed"] and not batch["flushing"]
                         and any(group_key not in batch["group_flushing"]
                                 and group_key not in batch["group_flushed"]
                                 and expected.issubset(batch["group_results"].get(group_key, set()))
                                 and batch["group_flush_at"].get(group_key, 0) <= now
                                 for group_key, expected in batch["group_expected"].items())]
                expired = [batch_id for batch_id, batch in self._batches.items()
                           if not batch["sealed"] and batch["source"] != "cloud-scan"
                           and time.monotonic() >= batch["deadline"]]
                flush.extend(ready)
                flush.extend(expired)
                if flush:
                    key = None
                    signature = None
                elif not self._pending:
                    self._condition.wait(timeout=1)
                    continue
                else:
                    key = self._next_pending_key()
                    due, signature = self._pending[key]
                    remaining = due - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(timeout=min(remaining, 1))
                        continue
                    del self._pending[key]
                    batch_id = self._pending_batches.pop(key, None)
            for batch_id in flush:
                if batch_id in expired:
                    self._expire_batch(batch_id)
                elif batch_id in self._batches and not self._batches[batch_id]["sealed"]:
                    self._seal_batch(batch_id)
                else:
                    self._maybe_flush_batch(batch_id)
            if key is None:
                continue
            batch = [(key, signature)]
            batch_ids = {key: batch_id}
            if self._telegram_aggregate:
                with self._condition:
                    now = time.monotonic()
                    extras = sorted(
                        ((extra_key, extra_signature) for extra_key, (extra_due, extra_signature) in self._pending.items()
                         if not extra_key.startswith("__") and extra_due <= now),
                        key=lambda item: filename_order(item[1].get("name", item[0]) if isinstance(item[1], dict) else item[0]),
                    )
                    for extra_key, extra_signature in extras:
                        del self._pending[extra_key]
                        batch_ids[extra_key] = self._pending_batches.pop(extra_key, None)
                        batch.append((extra_key, extra_signature))
            for batch_key, batch_signature in batch:
                item_batch_id = batch_ids.get(batch_key)
                self._active_batch_id = item_batch_id
                self._work_item(batch_key, batch_signature)
                self._active_batch_id = None
                with self._condition:
                    still_pending = batch_key in self._pending
                    state = self._batches.get(item_batch_id) if item_batch_id and not still_pending else None
                    if state:
                        state["results"].add(batch_key)
                        group_key = state["groups"].get(batch_key)
                        if group_key is not None:
                            state["group_results"].setdefault(group_key, set()).add(batch_key)
                if state:
                    self._maybe_flush_batch(item_batch_id)

    def _work_item(self, key, signature):
            path = Path(key)
            try:
                if key == "__cloud_scan__":
                    self._cloud_scan_step()
                    return
                if key.startswith("cloud:"):
                    self._process_cloud_file(signature)
                    return
                stat = path.stat()
                if signature != (stat.st_size, stat.st_mtime_ns, stat.st_ino):
                    self._queue_event(path)
                    return
                self._process_file(path)
            except InterruptedError:
                return
            except Exception as err:
                if key == "__cloud_scan__" or key.startswith("cloud:"):
                    from .cloud_storage import CloudError
                    detail = str(err) if isinstance(err, CloudError) else f"网盘任务失败（{type(err).__name__}）"
                    self._cloud_status.update(error=detail, file="")
                    filename = signature.get("name", "未知文件") if isinstance(signature, dict) else "目录补扫"
                    logger.warning(f"自动生成ED2K链接：{filename}：{detail}")
                if key == "__cloud_scan__":
                    self._cloud_iterator = None
                    self._cloud_adapter = None
                    self._cloud_status["scanning"] = False
                    self._seal_batch(self._cloud_scan_batch_id)
                    self._cloud_scan_batch_id = None
                else:
                    failures = min(5, self._cloud_failures.get(key, 0) + 1)
                    self._cloud_failures[key] = failures
                    delay = min(3600, 300 * 2 ** (failures - 1))
                    self._cloud_status.update(reconnecting=False, error_key=key, retry_at=time.time() + delay)
                    self._queue_cloud_file(signature, delay=delay)
                return
                if isinstance(err, FileNotFoundError):
                    return
                logger.warning(f"自动生成ED2K链接：处理失败，60秒后重试：{path}：{type(err).__name__}")
                self._queue_event(path, delay=60)

    def cloud_status(self) -> dict:
        from .cloud_storage import available_storages
        try:
            storages = available_storages()
        except Exception:
            storages = []
        with self._condition:
            pending = sum(key.startswith("cloud:") for key in self._pending)
        return {"ok": True, "storages": storages, "enabled": self._cloud_enabled,
                "pending": pending, "scan_enabled": self._cloud_scan_enabled, "life_enabled": self._cloud_monitor_enabled,
                "life_source": self._cloud_life_source, **self._cloud_status}

    def poll_cloud_events(self) -> None:
        if not self._enabled or not self._cloud_enabled or not self._cloud_monitor_enabled or self._stopping.is_set():
            return
        if time.monotonic() < self._cloud_life_retry_after:
            return
        life_lock = self._cloud_life_lock
        if not life_lock.acquire(blocking=False):
            return
        stopping = self._stopping
        try:
            from .cloud_storage import CloudStorage, CloudError
            storage = self._cloud_adapter or CloudStorage(self._cloud_storage)
            backlog_full = self._cloud_life_source == "shared" and len(self._cloud_life_candidates) > 448
            events = [] if backlog_full else storage.life_events(self._cloud_life_source, after_id=self._cloud_life_cursor, since_time=self._cloud_life_started)
            if stopping.is_set() or stopping is not self._stopping:
                return
            parsed = []
            for event in events:
                try:
                    parsed.append((int(event["id"]), int(event["type"]), float(event.get("update_time") or 0), event))
                except (ValueError, TypeError, KeyError):
                    continue
            new = [entry for entry in parsed if entry[0] > self._cloud_life_cursor and entry[2] >= self._cloud_life_started]
            if parsed:
                self._cloud_life_cursor = max(self._cloud_life_cursor, max(entry[0] for entry in parsed))
            self._cloud_status.update(life_last_poll=time.time(), life_truncated=backlog_full or len(events) >= 64)
            latest = getattr(storage, "life_source_latest", None)
            if self._cloud_life_source == "shared" and isinstance(latest, (int, float)):
                self._cloud_status["life_source_latest"] = latest
                self._cloud_status["life_source_notice"] = "复用事件库已有15分钟未出现新记录；可能暂无事件，也可能115插件正等待整理任务或监听延迟。最近检查不代表115端实时同步；定时补扫仍独立生效。" if time.time() - latest > 900 else ""
            now = time.monotonic()
            for file_id, candidate in list(self._cloud_life_candidates.items()):
                if candidate["expires"] <= now or candidate["attempts"] >= 10:
                    self._cloud_life_candidates.pop(file_id, None)
                    reason = candidate.get("reason") or "文件详情尚未就绪"
                    if reason not in CLOUD_LIFE_SILENT_SKIP_REASONS:
                        logger.info(f"自动生成ED2K链接：生活事件文件复查到期，未加入计算队列：文件ID {file_id}；最后原因：{reason}；如确认符合条件可手动补扫")
            for _, event_type, _, event in sorted(new, key=lambda entry: entry[0]):
                try:
                    file_id = str(int(event["file_id"]))
                    is_file = int(event["file_category"]) != 0
                except (KeyError, TypeError, ValueError):
                    continue
                if event_type == 22:
                    self._cloud_life_candidates.pop(file_id, None)
                    continue
                if not is_file:
                    continue
                candidate = self._cloud_life_candidates.get(file_id)
                if event_type in {20, 24} and candidate:
                    candidate["event"] = event
                    candidate["due"] = min(candidate["due"], now)
                elif event_type in {1, 2, 5, 6, 14, 23}:
                    if candidate:
                        candidate["event"] = event
                    elif len(self._cloud_life_candidates) < 512:
                        self._cloud_life_candidates[file_id] = {"event": event, "expires": now + 900, "attempts": 0, "due": now}
                    else:
                        logger.warning("自动生成ED2K链接：生活事件待复查文件达到512项上限，请按需手动补扫")
            queued = 0
            checks = 0
            ready_items = []
            lookup_failures = 0
            lookup_retry_delays = []
            for file_id, candidate in sorted(self._cloud_life_candidates.items(), key=lambda entry: entry[1]["due"]):
                if checks >= 8 or stopping.is_set() or stopping is not self._stopping:
                    break
                if candidate["due"] > now:
                    continue
                checks += 1
                candidate["attempts"] += 1
                candidate["due"] = now + 60
                try:
                    item = storage.item_from_life_event(candidate["event"])
                except Exception as failure:
                    if stopping.is_set() or stopping is not self._stopping:
                        return
                    lookup_failures += 1
                    retry_delay = min(300, 60 * 2 ** min(candidate["attempts"] - 1, 3))
                    candidate["due"] = now + retry_delay
                    candidate["reason"] = str(failure) if isinstance(failure, CloudError) else f"生活事件文件路径查询失败（{type(failure).__name__}）"
                    candidate["lookup_failed"] = True
                    lookup_retry_delays.append(retry_delay)
                    if candidate["attempts"] == 1:
                        logger.info(f"自动生成ED2K链接：生活事件文件路径查询暂时失败，单文件退避复查：文件ID {file_id}（{type(failure).__name__}）")
                    continue
                if stopping.is_set() or stopping is not self._stopping:
                    return
                candidate.pop("lookup_failed", None)
                reason = self._cloud_item_skip_reason(item) if item else "文件路径或详情尚未就绪"
                candidate["reason"] = reason
                label = item.get("name") if item else None
                label = label or f"文件ID {file_id}"
                if item and not reason:
                    ready_items.append(item)
                    self._cloud_life_candidates.pop(file_id, None)
                elif candidate["attempts"] >= 10:
                    self._cloud_life_candidates.pop(file_id, None)
                    if reason not in CLOUD_LIFE_SILENT_SKIP_REASONS:
                        logger.info(f"自动生成ED2K链接：生活事件文件复查10次未加入队列：{label}（文件ID {file_id}）；原因：{reason}；如确认符合条件可手动补扫")
                elif candidate["attempts"] == 1 and reason not in CLOUD_LIFE_SILENT_SKIP_REASONS:
                    logger.info(f"自动生成ED2K链接：新增事件暂未加入队列，保留限次复查：{label}（文件ID {file_id}）；原因：{reason}")
            life_batch_id = self._new_batch("cloud-life") if ready_items else None
            with self._condition:
                for item in sorted(ready_items, key=lambda item: filename_order(item["path"])):
                    if self._queue_cloud_file(item, batch_id=life_batch_id):
                        queued += 1
            if life_batch_id:
                self._seal_batch(life_batch_id)
            self._cloud_status["life_events"] += queued
            self._cloud_life_failures = 0
            self._cloud_life_retry_after = 0
            if lookup_failures:
                self._cloud_status.update(
                    life_error=f"{lookup_failures}个新增事件的文件路径暂时查询失败，已单独退避复查；其他候选继续处理",
                    life_retry_at=time.time() + min(lookup_retry_delays),
                )
            else:
                self._cloud_status.update(life_error="", life_retry_at=None)
        except Exception as failure:
            if stopping is self._stopping and not stopping.is_set():
                self._cloud_status["life_error"] = str(failure) if isinstance(failure, CloudError) else f"生活事件读取失败（{type(failure).__name__}），定时补扫仍可用"
                self._cloud_life_failures = min(5, self._cloud_life_failures + 1)
                delay = min(3600, 300 * 2 ** (self._cloud_life_failures - 1))
                self._cloud_life_retry_after = time.monotonic() + delay
                self._cloud_status["life_retry_at"] = time.time() + delay
        finally:
            life_lock.release()

    def _cloud_succeeded(self, key):
        self._cloud_failures.pop(key, None)
        if self._cloud_status.get("error_key") == key:
            self._cloud_status.update(error="", error_key="", retry_at=None)

    def queue_cloud_scan(self) -> dict:
        if not self._enabled or not self._cloud_enabled or self._worker is None:
            return {"ok": False, "message": "请先启用插件，并保存网盘来源、目录和增量计划"}
        with self._condition:
            if not self._cloud_status["scanning"] and "__cloud_scan__" not in self._pending:
                self._pending["__cloud_scan__"] = (time.monotonic(), None)
                self._condition.notify_all()
        return {"ok": True, "message": "网盘增量扫描已加入后台队列；不会删除网盘文件，首次读取会消耗完整文件流量"}

    def _queue_cloud_file(self, item: dict, delay=0, batch_id=None) -> bool:
        from .cloud_storage import record_key
        if self._stopping.is_set():
            return False
        key = record_key(item)
        batch_id = batch_id or self._active_batch_id
        created_batch = False
        if batch_id is None:
            with self._condition:
                batch_id = self._pending_batches.get(key)
            if batch_id is None:
                batch_id = self._new_batch("cloud-retry")
                created_batch = True
        with self._condition:
            if key not in self._pending:
                self._pending[key] = (time.monotonic() + delay, item)
                batch_id = self._register_batch_item(batch_id, key, self._batch_item_name(key, item))
                self._pending_batches[key] = batch_id
                queued = True
            else:
                queued = False
            self._condition.notify_all()
        if created_batch:
            self._seal_batch(batch_id)
        return queued

    def _cloud_item_is_eligible(self, item: dict) -> bool:
        return not self._cloud_item_skip_reason(item)

    def _cloud_item_skip_reason(self, item: dict) -> str:
        from .cloud_storage import allowed_roots
        if not allowed_roots(item, self._cloud_storage, self._cloud_folder):
            return "不在所选网盘存储或监控目录"
        if not allowed_roots(item, self._cloud_storage, self._cloud_folder, self._cloud_recursive):
            return "子目录未开启监控"
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            return "文件大小无效"
        suffix = Path(item.get("name") or item.get("path") or "").suffix.lower()
        extensions = self._extension_set()
        if size <= 0:
            return "文件大小尚为0，等待写入或元数据更新"
        if size < minimum_file_bytes(self._min_file_size):
            return "小于生成链接最小文件大小"
        if suffix in {".part", ".tmp", ".!qb", ".crdownload"}:
            return "临时下载文件，等待完成"
        if extensions and suffix.lstrip(".") not in extensions:
            return "扩展名不符合过滤设置"
        return ""

    def _cloud_scan_step(self) -> None:
        from .cloud_storage import CloudStorage, CloudError, monitor_roots
        if self._stopping.is_set() or not self._cloud_enabled:
            return
        if self._cloud_iterator is None:
            self._cloud_scan_batch_id = self._new_batch("cloud-scan")
            self._cloud_adapter = CloudStorage(self._cloud_storage)
            adapter = self._cloud_adapter
            roots = monitor_roots(self._cloud_folder, cloud=True, recursive=self._cloud_recursive)

            def walk_roots():
                for root in roots:
                    if self._stopping.is_set():
                        return
                    try:
                        yield from adapter.walk(root, self._cloud_recursive, self._stopping)
                    except CloudError as failure:
                        self._cloud_status["error"] = f"目录 {root}：{failure}"
                        logger.warning(f"自动生成ED2K链接：网盘目录补扫未完成：{root}：{failure}")
                        yield None

            self._cloud_iterator = walk_roots()
            self._cloud_status.update(scanning=True, error="")
        with self._condition:
            count = sum(key.startswith("cloud:") for key in self._pending)
        if count < 512:
            for index in range(min(32, 512 - count)):
                try:
                    item = next(self._cloud_iterator)
                except StopIteration:
                    self._cloud_iterator = None
                    self._cloud_status.update(scanning=False, last_scan=time.time())
                    self._seal_batch(self._cloud_scan_batch_id)
                    self._cloud_scan_batch_id = None
                    return
                if item is None:
                    break
                if self._cloud_item_is_eligible(item):
                    self._queue_cloud_file(item, batch_id=self._cloud_scan_batch_id)
        with self._condition:
            self._pending["__cloud_scan__"] = (time.monotonic() + 1, None)

    def _process_cloud_file(self, item: dict) -> None:
        from .cloud_storage import CloudStorage, CloudError, fingerprint, fingerprint_matches, hash_remote, record_key, legacy_record_key
        if not self._cloud_enabled or self._stopping.is_set() or not self._cloud_item_is_eligible(item):
            return
        stopping = self._stopping
        import datetime
        delete_scope_at_start = self._cloud_delete_scope if self._delete_source and self._cloud_delete_enabled else ""
        modified = item.get("modify_time")
        if isinstance(modified, (int, float)):
            modified_at = modified / 1000 if modified > 10_000_000_000 else modified
        elif isinstance(modified, str):
            try:
                modified_at = float(modified)
                if modified_at > 10_000_000_000:
                    modified_at /= 1000
            except ValueError:
                try:
                    modified_at = datetime.datetime.fromisoformat(modified.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    modified_at = None
        else:
            modified_at = None
        if self._min_age and modified_at is not None and modified_at + self._min_age > time.time():
            self._queue_cloud_file(item, delay=max(60, min(self._min_age, int(modified_at + self._min_age - time.time()))))
            return
        if self._min_age and modified_at is None:
            observation_key = record_key(item)
            now = time.monotonic()
            observed = self._cloud_observed.get(observation_key)
            if not observed or observed[0] != fingerprint(item):
                if len(self._cloud_observed) >= 2048:
                    self._cloud_observed.pop(next(iter(self._cloud_observed)))
                self._cloud_observed[observation_key] = (fingerprint(item), now)
                self._queue_cloud_file(item, delay=max(60, self._min_age))
                return
            if now - observed[1] < self._min_age:
                self._queue_cloud_file(item, delay=max(1, int(self._min_age - (now - observed[1]))))
                return
        with self._lock:
            storage = self._cloud_adapter or CloudStorage(self._cloud_storage)
            _, output, cache_path = self._paths()
            output = output if self._output_enabled else None
            records = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
            key = record_key(item)
            record = records.get(key)
            old_key = legacy_record_key(item)
            key_migrated = record is None and old_key in records
            if key_migrated:
                record = records.pop(old_key)
                records[key] = record
            if record and (record.get("source_deleted") or record.get("cloud_delete_state") in {"inflight", "uncertain"}):
                return
            unchanged = record and not record_problem(record) and fingerprint_matches(record.get("cloud_fingerprint"), item) and record.get("hash_version") == 2
            migrated = unchanged and (key_migrated or record.get("cloud_fingerprint") != fingerprint(item))
            if migrated:
                record["cloud_fingerprint"] = fingerprint(item)
            if unchanged and (not self._telegram_enabled or (record.get("telegram_sent") and record.get("telegram_target") == self._telegram_target_key())):
                if output is not None or migrated:
                    self._write_results(output, cache_path, records)
                self._cloud_succeeded(key)
                self._maybe_delete_cloud_source(storage, item, record, output, cache_path, records, stopping)
                return
            fresh = storage.fresh(item)
            if fingerprint(fresh) != fingerprint(item):
                self._queue_cloud_file(fresh, delay=max(60, self._min_age))
                return
            item = fresh
            self._cloud_status.update(file=item["name"], bytes_read=0, size=item["size"], error="", reconnecting=False, stream_retries=0)
            delete_evidence_scope = ""
            if not unchanged:
                if delete_scope_at_start and delete_scope_at_start == self._cloud_delete_scope and item["storage"] == "115网盘Plus":
                    try:
                        if storage.delete_scope(self._cloud_folder, self._cloud_recursive) == delete_scope_at_start:
                            item = {**item, "sha1": storage.file_sha1(item)}
                            delete_evidence_scope = delete_scope_at_start
                    except Exception as failure:
                        logger.warning(f"自动生成ED2K链接：未获得删源所需SHA1或账号证据，仅生成链接并保留网盘源文件：{item['name']}（{type(failure).__name__}）")
                digest = lambda data: _CryptoMD4.new(data).digest() if _CryptoMD4 else _md4(data)
                def progress(done, size):
                    self._cloud_status.update(bytes_read=done, size=size, reconnecting=False)
                def on_retry(attempt):
                    self._cloud_status.update(reconnecting=True, stream_retries=attempt)
                file_hash = hash_remote(storage, item, stopping, digest, ED2K_PART_SIZE, progress,
                                        read_timeout=self._cloud_read_timeout, on_retry=on_retry)
                if stopping is not self._stopping or stopping.is_set():
                    return
                record = {"name": item["name"], "size": item["size"], "link": ed2k_link(Path(item["name"]), file_hash, item["size"]),
                          "source_type": "cloud", "storage": item["storage"], "source_path": item["path"],
                          "cloud_fingerprint": fingerprint(item), "generated_at": time.time(), "source_deleted": False,
                          "hash_version": 2, "telegram_sent": False, "telegram_target": ""}
                if record_problem(record):
                    raise CloudError("网盘链接校验失败，不保存或发送")
                if delete_evidence_scope and delete_evidence_scope == self._cloud_delete_scope and stopping is self._stopping and not stopping.is_set():
                    try:
                        scope = storage.delete_scope(self._cloud_folder, self._cloud_recursive)
                        sha1 = item.get("sha1")
                        if scope == self._cloud_delete_scope and isinstance(sha1, str) and re.fullmatch(r"[0-9a-fA-F]{40}", sha1):
                            record.update(cloud_delete_scope=scope, cloud_sha1=sha1.lower(), cloud_delete_note="等待所选输出完成后校验删源")
                    except CloudError:
                        pass
                records[key] = record
                self._cloud_status["completed"] += 1
                logger.info(f"自动生成ED2K链接：网盘文件生成完成：{item['storage']}：{item['name']}")
            self._write_results(output, cache_path, records)
            if self._telegram_aggregate and self._active_batch_id in self._batches:
                with self._condition:
                    batch = self._batches.get(self._active_batch_id)
                    if batch:
                        batch["contexts"][key] = {"kind": "cloud", "item": item, "storage": storage,
                            "output": output, "cache_path": cache_path,
                            "batch_group": batch["groups"].get(key)}
                self._cloud_observed.pop(record_key(item), None)
                self._cloud_succeeded(key)
                self._cloud_status["reconnecting"] = False
                self._cloud_status["file"] = ""
                return
            if self._telegram_aggregate:
                candidates = self._aggregate_candidates(record, cache_path, records)
                if candidates:
                    self._notify_records(candidates, checkpoint=lambda: self._write_results(output, cache_path, records))
                else:
                    record.setdefault("telegram_aggregate_waiting_since", time.time())
                    self._write_results(output, cache_path, records)
            else:
                self._notify_record(Path(item["name"]), record, checkpoint=lambda: self._write_results(output, cache_path, records))
            self._write_results(output, cache_path, records)
            self._cloud_observed.pop(record_key(item), None)
            self._cloud_succeeded(key)
            self._cloud_status["reconnecting"] = False
            self._cloud_status["file"] = ""
            if self._telegram_enabled and not record.get("telegram_sent"):
                delay = self._telegram_aggregate_window if self._telegram_aggregate else 300
                self._queue_cloud_file(item, delay=delay)
            else:
                self._maybe_delete_cloud_source(storage, item, record, output, cache_path, records, stopping)

    def _maybe_delete_cloud_source(self, storage, item, record, output, cache_path, records, stopping):
        from .cloud_storage import CloudError, transient_read_error
        if stopping is not self._stopping or stopping.is_set() or not self._delete_source or not self._cloud_delete_enabled:
            return
        if item.get("storage") != "115网盘Plus" or not self._cloud_delete_scope or record.get("cloud_delete_scope") != self._cloud_delete_scope:
            return
        if record_problem(record) or record.get("source_deleted") or record.get("cloud_delete_state") in {"inflight", "uncertain", "blocked"}:
            return
        if output is None and not self._telegram_enabled:
            return
        if self._telegram_enabled and (not record.get("telegram_sent") or record.get("telegram_target") != self._telegram_target_key()):
            return
        if record.get("cloud_delete_attempts", 0) >= 3:
            return
        if record.get("cloud_delete_check_attempts", 0) >= 3:
            return
        if record.get("cloud_delete_retry_at", 0) > time.time():
            self._queue_cloud_file(item, delay=max(1, record["cloud_delete_retry_at"] - time.time()))
            return
        try:
            current = storage.prepare_delete(item, record.get("cloud_sha1"), self._cloud_folder, self._cloud_recursive, self._cloud_delete_scope)
        except Exception as failure:
            if transient_read_error(failure):
                attempts = record.get("cloud_delete_check_attempts", 0) + 1
                delay = 300 * 2 ** (attempts - 1)
                note = "删源前查询网络超时或连接失败，尚未发送删除请求"
                note += "；稍后重新校验" if attempts < 3 else "；已达3次上限，保留源文件"
                record.update(cloud_delete_state="check_retry", cloud_delete_note=note,
                              cloud_delete_check_attempts=attempts, cloud_delete_check_error=type(failure).__name__,
                              cloud_delete_retry_at=time.time() + delay, delete_failed=True)
                self._write_results(output, cache_path, records)
                if attempts < 3 and stopping is self._stopping and not stopping.is_set():
                    self._queue_cloud_file(item, delay=delay)
                logger.warning(f"自动生成ED2K链接：{note}：{item['name']}（{type(failure).__name__}，校验{attempts}/3）")
                return
            note = str(failure) if isinstance(failure, CloudError) else f"删源前查询异常（{type(failure).__name__}），未发送删除请求"
            record.update(cloud_delete_state="blocked", cloud_delete_note=note, delete_failed=True)
            self._write_results(output, cache_path, records)
            logger.warning(f"自动生成ED2K链接：网盘删源校验未通过，保留文件：{item['name']}（{type(failure).__name__}）")
            return
        if stopping is not self._stopping or stopping.is_set():
            return
        record.pop("cloud_delete_check_attempts", None)
        record.pop("cloud_delete_check_error", None)
        record.update(cloud_delete_state="inflight", cloud_delete_note="删除请求处理中，异常中断后不自动重删", cloud_delete_requested_at=time.time(), cloud_delete_attempts=record.get("cloud_delete_attempts", 0) + 1)
        self._write_results(output, cache_path, records)
        try:
            deleted = storage.delete_prepared(current, self._cloud_folder, self._cloud_recursive, self._cloud_delete_scope, stopping)
        except Exception as failure:
            record.update(cloud_delete_state="uncertain", cloud_delete_note="删除结果未确认，请到115核实；不会自动重删", delete_failed=True)
            logger.warning(f"自动生成ED2K链接：网盘删除结果未确认，暂停重试：{item['name']}（{type(failure).__name__}）")
        else:
            if deleted is True:
                record.update(source_deleted=True, deleted_at=time.time(), delete_method="cloud_auto", cloud_delete_state="deleted", cloud_delete_note="115接口确认删除成功")
                record.pop("delete_failed", None)
                record.pop("cloud_delete_retry_at", None)
                logger.info(f"自动生成ED2K链接：115源文件删除成功，链接及记录已保留：{item['name']}")
            else:
                record.update(cloud_delete_state="failed", cloud_delete_note="115接口未确认成功，最多尝试3次", delete_failed=True, cloud_delete_retry_at=time.time() + 300)
                logger.warning(f"自动生成ED2K链接：115明确返回删除失败，保留链接记录：{item['name']}；尝试{record['cloud_delete_attempts']}/3，重试前再次核验文件身份")
                if record["cloud_delete_attempts"] < 3:
                    self._queue_cloud_file(item, delay=300)
        self._write_results(output, cache_path, records)

    def _extension_set(self) -> set[str]:
        return {item.strip().lower().lstrip(".") for item in self._extensions.split(",") if item.strip()}

    def _telegram_target_key(self) -> str:
        targets = self._telegram_targets()
        keys = sorted(self._telegram_delivery_key(*target) for target in targets)
        return hashlib.sha256("|".join(keys).encode("utf-8")).hexdigest()

    def _telegram_targets(self):
        from .telegram_targets import telegram_targets
        return telegram_targets(self._telegram_chat_id, self._telegram_thread_id)

    def _telegram_delivery_key(self, destination: str, thread_id) -> str:
        from .user_account import TelegramUserAccount
        identity = TelegramUserAccount(Path(self.get_data_path())).identity()
        value = f"user|{identity}|{destination.lower()}|{thread_id or ''}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _send_telegram(self, path: Path, link: str, target=None) -> bool:
        if not self._telegram_enabled:
            return True
        if target is None:
            return self._send_telegram_user(path, link)
        return self._send_telegram_user(path, link, target=target)

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

    def _send_telegram_user(self, path: Path, link: str, target=None) -> bool:
        if time.monotonic() < self._telegram_retry_after:
            return False
        retry_key = target or (self._telegram_chat_id, self._telegram_thread_id)
        if time.monotonic() < self._telegram_target_retry.get(retry_key, 0):
            return False
        try:
            from .user_account import TelegramUserAccount
            try:
                from app.runtime.settings import get_runtime_setting
                proxy = get_runtime_setting("PROXY")
            except ImportError:
                from app.core.config import settings
                proxy = settings.PROXY
            destination, thread_id = target or self._telegram_targets()[0]
            account = TelegramUserAccount(Path(self.get_data_path()), proxy=proxy)
            remaining = max(0, 3 - (time.monotonic() - self._telegram_user_last_send))
            if self._stopping.wait(remaining):
                return False
            self._telegram_user_last_send = time.monotonic()
            account.send(destination, link, thread_id=thread_id)
            self._telegram_target_retry.pop(retry_key, None)
            logger.info(f"自动生成ED2K链接：已使用 Telegram 个人账号发送至 {destination}：{path.name}")
            return True
        except Exception as err:
            from .user_account import UserAccountError
            detail = str(err) if isinstance(err, (UserAccountError, ValueError)) else f"个人账号发送异常（{type(err).__name__}）"
            cooldown = max(60, int(getattr(err, "retry_after", 300)))
            if getattr(err, "account_wide", False):
                self._telegram_retry_after = time.monotonic() + cooldown
            self._telegram_target_retry[retry_key] = time.monotonic() + cooldown
            logger.warning(f"自动生成ED2K链接：Telegram 个人账号发送失败（目标 {retry_key[0]}）：{detail}；保留源文件，{cooldown}秒后允许重试，不会退回 Bot 模式")
            return False

    def _send_telegram_text(self, text: str, target=None, path_name="聚合链接") -> bool:
        return self._send_telegram(Path(path_name), text, target=target)

    def _notify_record(self, path: Path, record: dict, checkpoint=None) -> None:
        if not self._telegram_enabled:
            return
        target_key = self._telegram_target_key()
        if record.get("telegram_sent") and record.get("telegram_target") == target_key:
            return
        targets = self._telegram_targets()
        deliveries = record.setdefault("telegram_deliveries", {})
        if record.get("telegram_sent") and record.get("telegram_target"):
            from .user_account import TelegramUserAccount
            identity = TelegramUserAccount(Path(self.get_data_path())).identity()
            for destination, thread_id in targets:
                aliases = {destination, destination.lstrip("@"), f"https://t.me/{destination.lstrip('@')}", f"t.me/{destination.lstrip('@')}"}
                if destination.startswith("@"):
                    for host in ("t.me", "telegram.me", "www.t.me"):
                        for scheme in ("http://", "https://"):
                            address = f"{scheme}{host}/{destination.lstrip('@')}"
                            aliases.update({address, f"{address}/"})
                if len(targets) == 1:
                    aliases.add(self._telegram_chat_id)
                for alias in aliases:
                    legacy = f"user|{identity}|{alias}|{thread_id or ''}"
                    if hashlib.sha256(legacy.encode("utf-8")).hexdigest() == record["telegram_target"]:
                        deliveries[self._telegram_delivery_key(destination, thread_id)] = True
        record["telegram_sent"] = False
        record["telegram_target"] = ""
        for target in targets:
            delivery_key = self._telegram_delivery_key(*target)
            if deliveries.get(delivery_key):
                continue
            sent = self._send_telegram(path, record["link"]) if len(targets) == 1 else self._send_telegram(path, record["link"], target=target)
            if sent:
                deliveries[delivery_key] = True
                if checkpoint is not None:
                    checkpoint()
        record["telegram_sent"] = all(deliveries.get(self._telegram_delivery_key(*target)) for target in targets)
        record["telegram_target"] = target_key if record["telegram_sent"] else ""

    def _notify_records(self, records: list, checkpoint=None) -> None:
        if not records:
            return
        if not self._telegram_enabled:
            return
        targets = self._telegram_targets()
        if not targets:
            return
        for record in records:
            record.setdefault("telegram_deliveries", {})
        grouped = {}
        for record in records:
            key = telegram_aggregate_key(record.get("name", "")) if self._telegram_aggregate else None
            grouped.setdefault(key or ("__single__", id(record)), []).append(record)
        target_key = self._telegram_target_key()
        for group in grouped.values():
            group.sort(key=lambda item: filename_order(item.get("name", "")))
            chunks = telegram_message_chunks(group)
            for target in targets:
                delivery_key = self._telegram_delivery_key(*target)
                pending = [item for item in group if not item["telegram_deliveries"].get(delivery_key)]
                if not pending:
                    continue
                for chunk in telegram_message_chunks(pending):
                    text = "\n".join(item["link"] for item in chunk)
                    sent = self._send_telegram_text(text, target=target)
                    if not sent:
                        break
                    for item in chunk:
                        item["telegram_deliveries"][delivery_key] = True
                        if checkpoint:
                            checkpoint()
        for record in records:
            deliveries = record["telegram_deliveries"]
            record["telegram_sent"] = all(deliveries.get(self._telegram_delivery_key(*target)) for target in targets)
            record["telegram_target"] = target_key if record["telegram_sent"] else ""
            if record["telegram_sent"]:
                record.pop("telegram_aggregate_waiting_since", None)

    def _aggregate_candidates(self, record, cache_path, records=None):
        if not self._telegram_aggregate:
            return [record]
        if records is None:
            try:
                records = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
            except (OSError, ValueError):
                return [record]
        key = telegram_aggregate_key(record.get("name", ""))
        if key is None:
            return [record]
        candidates = [item for item in records.values()
                      if not item.get("telegram_sent")
                      and telegram_aggregate_key(item.get("name", "")) == key
                      and not record_problem(item)
                      and time.time() - float(item.get("generated_at", time.time())) <= self._telegram_aggregate_window]
        candidates = [item for item in candidates if item.get("link") != record.get("link")]
        candidates.append(record)
        if len(candidates) == 1:
            waiting_since = float(record.get("telegram_aggregate_waiting_since", record.get("generated_at", time.time())))
            if time.time() - waiting_since < self._telegram_aggregate_window:
                return []
        candidates.sort(key=lambda item: filename_order(item.get("name", "")))
        return candidates

    def _iter_files(self, folder: Path):
        if self._recursive:
            iterator = os.walk(folder)
            for root, directories, filenames in iterator:
                directories.sort(key=filename_order)
                for filename in sorted(filenames, key=filename_order):
                    yield Path(root) / filename
        else:
            for item in sorted(folder.iterdir(), key=lambda item: filename_order(item.name)):
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
            links = sorted({item["link"] for item in records.values() if not record_problem(item)})
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
            if stat.st_size <= 0:
                logger.warning(f"自动生成ED2K链接：跳过空文件，保留源文件并等待后续写入：{path.name}")
                self._queue_event(path, delay=max(60, self._min_age))
                return
            if stat.st_size < minimum_file_bytes(self._min_file_size):
                if self._delete_small_files:
                    if stat.st_mtime + self._min_age > time.time():
                        self._queue_event(path)
                        return
                    if not self._stopping.is_set():
                        self._unlink_source(path, stat)
                        logger.info(f"自动生成ED2K链接：低于最小大小阈值，按独立开关删除本地源文件（未生成或发送链接）：{path}")
                return
            if stat.st_mtime + self._min_age > time.time():
                self._queue_event(path)
                return
            records = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
            key = str(path.resolve())
            record = records.get(key)
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino, stat.st_dev)
            unchanged = record and not record_problem(record) and (record.get("size"), record.get("mtime_ns")) == signature[:2]
            if unchanged and record.get("inode") is not None:
                unchanged = (record.get("inode"), record.get("device"), record.get("ctime_ns")) == (stat.st_ino, stat.st_dev, stat.st_ctime_ns)
            legacy_boundary = record and stat.st_size > 0 and stat.st_size % ED2K_PART_SIZE == 0 and record.get("hash_version") != 2
            if unchanged and not legacy_boundary and not self._delete_source:
                if not self._telegram_enabled or (record.get("telegram_sent") and record.get("telegram_target") == self._telegram_target_key()):
                    return
            if not unchanged or legacy_boundary:
                file_hash = ed2k_hash(path, self._stopping)
                final = path.stat()
                if signature != (final.st_size, final.st_mtime_ns, final.st_ino, final.st_dev) or stat.st_ctime_ns != final.st_ctime_ns:
                    self._queue_event(path)
                    return
                record = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "name": path.name,
                          "link": ed2k_link(path, file_hash, size=stat.st_size), "hash_version": 2, "telegram_sent": False, "telegram_target": "",
                          "inode": stat.st_ino, "device": stat.st_dev, "ctime_ns": stat.st_ctime_ns,
                          "generated_at": time.time(), "source_deleted": False}
                if record_problem(record):
                    logger.warning(f"自动生成ED2K链接：大小或哈希校验失败，保留源文件：{path.name}")
                    self._queue_event(path, delay=60)
                    return
                records[key] = record
                logger.info(f"自动生成ED2K链接：生成完成：{path}")
            self._write_results(output, cache_path, records)
            if self._telegram_aggregate and self._active_batch_id in self._batches:
                with self._condition:
                    batch = self._batches.get(self._active_batch_id)
                    if batch:
                        batch["contexts"][key] = {"kind": "local", "path": path, "signature": signature,
                            "output": output, "cache_path": cache_path,
                            "batch_group": batch["groups"].get(key)}
                return
            if self._telegram_aggregate:
                candidates = self._aggregate_candidates(record, cache_path, records)
                if candidates:
                    self._notify_records(candidates, checkpoint=lambda: self._write_results(output, cache_path, records))
                else:
                    record.setdefault("telegram_aggregate_waiting_since", time.time())
                    self._write_results(output, cache_path, records)
            else:
                self._notify_record(path, record, checkpoint=lambda: self._write_results(output, cache_path, records))
            self._write_results(output, cache_path, records)
            delivered = not self._telegram_enabled or record.get("telegram_sent")
            if not delivered:
                delay = self._telegram_aggregate_window if self._telegram_aggregate else 60
                self._queue_event(path, delay=delay)
            if self._delete_source and delivered and (output is not None or self._telegram_enabled):
                final = path.stat()
                if signature == (final.st_size, final.st_mtime_ns, final.st_ino, final.st_dev) and stat.st_ctime_ns == final.st_ctime_ns and not self._stopping.is_set():
                    try:
                        self._unlink_source(path, final)
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
        with self._lock:
            records = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
            if self._output_enabled:
                self._write_results(output, cache_path, records)
        batch_id = self._new_batch("local-scan")
        for folder in self._local_roots():
            if not folder.is_dir():
                logger.warning(f"自动生成ED2K链接：目录不存在：{folder}")
                continue
            for path in self._iter_files(folder):
                if self._stopping.is_set():
                    return
                self._queue_event(path, batch_id=batch_id)
        self._seal_batch(batch_id)
        logger.info("自动生成ED2K链接：增量补扫已入队，已有链接不会重复计算")

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        services = []
        if self._folder and self._scan_enabled:
            services.append({"id": "Ed2kFolderWatcher", "name": "扫描本地文件夹并生成 ED2K", "trigger": CronTrigger.from_crontab(self._cron), "func": self.scan})
        if self._cloud_enabled:
            if self._cloud_scan_enabled:
                services.append({"id": "Ed2kFolderWatcherCloud", "name": "网盘定时增量补扫", "trigger": CronTrigger.from_crontab(self._cloud_cron), "func": self.queue_cloud_scan})
            if self._cloud_monitor_enabled:
                services.append({"id": "Ed2kFolderWatcherLife", "name": "115生活事件新增文件监控", "trigger": CronTrigger.from_crontab("* * * * *"), "func": self.poll_cloud_events})
        return services

    def get_api(self) -> List[Dict[str, Any]]:
        from .web_api import build_routes
        return build_routes(self)

    def get_page(self) -> Optional[List[dict]]:
        return [{"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
                 "text": "最近生成的 ED2K 链接记录请使用新版查看页面；修改配置请在插件卡片三个点菜单中选择设置。"}]

    @staticmethod
    def _record_id(path: str, record: dict) -> str:
        value = [path, record.get("size"), record.get("mtime_ns"), record.get("link"), record.get("generated_at")]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _source_matches(self, path: Path, record: dict) -> bool:
        if record.get("source_type") == "cloud":
            return False
        if record_problem(record) or record.get("source_deleted") is True or not self._folder or not self._eligible(path):
            return False
        current = path.stat()
        if (record.get("size"), record.get("mtime_ns")) != (current.st_size, current.st_mtime_ns):
            return False
        if record.get("inode") is not None:
            return (record.get("inode"), record.get("device"), record.get("ctime_ns")) == (current.st_ino, current.st_dev, current.st_ctime_ns)
        return True

    def _unlink_source(self, path: Path, expected) -> None:
        root = self._local_root_for(path)
        if root is None:
            raise ValueError("源文件不在当前监控目录，取消删除")
        folder = root.resolve()
        try:
            relative = path.absolute().relative_to(root.absolute())
        except ValueError:
            relative = path.absolute().relative_to(folder)
        if not relative.parts or ".." in relative.parts:
            raise ValueError("源文件路径不安全")
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in relative.parts[:-1]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            current = os.stat(relative.name, dir_fd=descriptor, follow_symlinks=False)
            if not stat_module.S_ISREG(current.st_mode) or (current.st_size, current.st_mtime_ns, current.st_ctime_ns, current.st_ino, current.st_dev) != (expected.st_size, expected.st_mtime_ns, expected.st_ctime_ns, expected.st_ino, expected.st_dev):
                raise ValueError("源文件已变化或不是普通文件，取消删除")
            os.unlink(relative.name, dir_fd=descriptor)
        finally:
            os.close(descriptor)

    def delete_source(self, record_id: str) -> dict:
        with self._lock:
            try:
                folder, output, cache_path = self._paths()
                records = json.loads(cache_path.read_text(encoding="utf-8"))
                if not isinstance(records, dict) or any(not isinstance(record, dict) for record in records.values()):
                    raise ValueError("记录格式错误")
                entry = next(((path, record) for path, record in records.items() if self._record_id(path, record) == record_id), None)
                if entry is None:
                    return {"ok": False, "message": "记录不存在或已更新，请刷新后重试"}
                key, record = entry
                path = Path(key)
                if not self._source_matches(path, record):
                    return {"ok": False, "message": "源文件不存在、已变化、链接无效或不在当前监控目录，拒绝删除"}
                expected = path.stat()
                file_hash = ed2k_hash(path, self._stopping)
                if file_hash.lower() != record["link"].split("|")[4].lower():
                    return {"ok": False, "message": "源文件内容与链接不一致，拒绝删除，请重新生成"}
                output = output if self._output_enabled else None
                self._write_results(output, cache_path, records)
                try:
                    self._unlink_source(path, expected)
                except OSError:
                    record["delete_failed"] = True
                    self._write_results(output, cache_path, records)
                    raise
                record["source_deleted"] = True
                record["deleted_at"] = time.time()
                record["delete_method"] = "manual"
                record.pop("delete_failed", None)
                self._write_results(output, cache_path, records)
                logger.info(f"自动生成ED2K链接：用户手动删除源文件：{path.name}")
                return {"ok": True, "message": "源文件已删除，ED2K 链接及记录已保留"}
            except (OSError, ValueError, TypeError, InterruptedError) as failure:
                logger.warning(f"自动生成ED2K链接：手动删除未完成：{type(failure).__name__}")
                return {"ok": False, "message": "删除未完成，请刷新核对实际状态并检查日志、文件权限或文件是否变化"}

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
                elif record.get("cloud_delete_state") in {"inflight", "uncertain"}:
                    source_status = "delete_uncertain"
                elif record.get("delete_failed"):
                    source_status = "delete_failed"
                elif record.get("source_deleted") is False:
                    source_status = "retained"
                else:
                    source_status = "unknown"
                problem = record_problem(record)
                try:
                    can_delete = self._source_matches(Path(path), record)
                except (OSError, ValueError):
                    can_delete = False
                items.append({"name": record.get("name") or Path(path).name, "link": record.get("link", ""),
                              "id": self._record_id(path, record), "can_delete": can_delete,
                              "link_valid": not problem, "link_error": problem,
                              "generated_at": record.get("generated_at"), "source_status": source_status,
                              "source_type": record.get("source_type", "local"), "storage": record.get("storage", "local"),
                              "source_path": record.get("source_path", path),
                              "deleted_at": record.get("deleted_at"), "cloud_delete_state": record.get("cloud_delete_state", ""), "cloud_delete_note": record.get("cloud_delete_note", "")})
            return {"ok": True, "items": items, "total": len(records), "limit": 100,
                    "delete_source_enabled": self._delete_source, "cloud_delete_enabled": self._cloud_delete_enabled}
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
        fields = ("enabled", "folder", "recursive", "cron", "output", "output_enabled", "monitor_enabled", "scan_enabled", "extensions", "min_age", "min_file_size", "delete_source", "delete_small_files", "cloud_delete_enabled", "telegram_enabled", "telegram_aggregate", "telegram_aggregate_window", "telegram_mode", "telegram_chat_id", "telegram_thread_id", "cloud_enabled", "cloud_scan_enabled", "cloud_storage", "cloud_folder", "cloud_recursive", "cloud_cron", "cloud_read_timeout", "cloud_monitor_enabled", "cloud_life_source")
        config = {name: getattr(self, f"_{name}") for name in fields}
        config.update(telegram_token=self._telegram_token, onlyonce=self._onlyonce, cloud_delete_scope=self._cloud_delete_scope)
        self.update_config(config)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        def field(component: str, model: str, label: str, **props: Any) -> dict:
            if "persistent_hint" in props:
                props["persistent-hint"] = props.pop("persistent_hint")
            props.update({"model": model, "label": label, "class": "mb-5"})
            return {"component": component, "props": props}

        return [{"component": "VForm", "content": [
            {"component": "VCardTitle", "text": "通用设置"},
            field("VSwitch", "enabled", "启用插件"),
            field("VSwitch", "delete_source", "完成后删除源文件", hint="输出全部成功且文件未变化后删除；115需另行授权，CloudDrive只读。", persistent_hint=True),
            field("VSwitch", "include_subdirectories", "包含子目录", hint="本地与网盘共用；旧配置不一致时请先选择再保存。", persistent_hint=True),
            field("VSwitch", "output_enabled", "生成本地 TXT 文件", hint="本地与网盘链接合并输出；关闭仍保留内部记录。", persistent_hint=True),
            field("VTextField", "output", "TXT 输出路径", placeholder="留空自动选择 ed2k.txt 路径"),
            field("VTextField", "extensions", "公共扩展名过滤", placeholder="留空处理全部文件，例如 mkv,mp4,flac"),
            field("VTextField", "min_age", "公共稳定等待秒数", type="number", hint="本地和网盘共用，请选择上传完成目录。", persistent_hint=True),
            field("VTextField", "min_file_size_gb", "生成链接最小文件大小（GB）", type="number", step="any", min=0, placeholder="例如0.5、1、1.5，仅填数值", hint="小于此值不生成、不转发；留空或0不限，单位GB。", persistent_hint=True),
            {"component": "VCardTitle", "text": "本地监控"},
            field("VTextField", "folder", "本地监控目录", placeholder="/downloads/complete,/media/movies", hint="容器内绝对路径，多个用英文逗号分隔，最多32个。", persistent_hint=True),
            field("VSwitch", "monitor_enabled", "本地实时监控文件"),
            field("VSwitch", "delete_small_files", "低于阈值文件自动删源", hint="稳定等待后直接删除低于阈值的非空本地文件，不生成、不发送；不可恢复。", persistent_hint=True),
            field("VSwitch", "scan_enabled", "本地定时增量补扫"),
            field("VTextField", "cron", "本地补扫计划", hint="默认每 10 分钟：*/10 * * * *", persistent_hint=True),
            {"component": "VCardTitle", "text": "网盘监控"},
            field("VSwitch", "cloud_enabled", "启用网盘增量生成"),
            field("VSelect", "cloud_storage", "网盘存储", items=[{"title": "115网盘Plus", "value": "115网盘Plus"}, {"title": "CloudDrive储存", "value": "CloudDrive储存"}]),
            field("VTextField", "cloud_folder", "网盘监控目录", placeholder="/电影,/电视剧", hint="同一存储内的绝对路径，多个用英文逗号分隔，最多32个。", persistent_hint=True),
            field("VSwitch", "cloud_monitor_enabled", "115生活事件监控（近实时）", hint="仅处理开启后的新增事件，不扫描已有文件；每分钟检查。", persistent_hint=True),
            field("VTextField", "cloud_cron", "网盘补扫计划", hint="仅开启网盘定时增量补扫时生效，默认每10分钟。", persistent_hint=True),
            field("VTextField", "cloud_read_timeout", "网盘读取超时（秒）", type="number", hint="30至1800秒，默认300。", persistent_hint=True),
            field("VSwitch", "cloud_scan_enabled", "网盘定时增量补扫", hint="开启后才按网盘补扫计划执行；关闭不影响手动补扫或生活事件触发。", persistent_hint=True),
            field("VSelect", "cloud_life_source", "生活事件来源", items=[{"title": "复用115插件监听（不额外请求生活接口）", "value": "shared"}, {"title": "独立轮询（每分钟一次生活接口）", "value": "direct"}]),
            {"component": "VCardTitle", "text": "ED2K链接自动转发"},
            field("VSwitch", "telegram_enabled", "发送 ED2K 链接到 Telegram"),
            field("VSwitch", "telegram_aggregate", "聚合转发", hint="同剧名同季数链接合并为一条换行消息。", persistent_hint=True),
            field("VTextField", "telegram_aggregate_window", "聚合转发延迟等待秒数", type="number", min=0, max=3600, hint="批次完成后继续等待；等待期间发现的同剧同季文件会合并，默认600秒。", persistent_hint=True),
            # field("VSelect", "telegram_mode", "Telegram 发送方式", items=[{"title": "Bot Token（机器人）", "value": "bot"}, {"title": "个人账号（Telethon）", "value": "user"}]),
            # field("VTextField", "telegram_token", "Telegram Bot Token", type="password"),
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-5"}, "text": "仅使用个人账号发送，可在插件页面完成授权；请使用可信网络或 HTTPS。"},
            field("VTextarea", "telegram_chat_id", "Telegram 接收目标（多个）", rows=1, placeholder="每行一个 @用户名、数字 Chat ID 或公开 t.me 链接", hint="支持换行、逗号或分号分隔；指定群组话题可填 @群组|话题ID，最多50个目标。", persistent_hint=True),
            field("VTextField", "telegram_thread_id", "Telegram 话题线程 ID", placeholder="普通私聊、群组、频道留空", hint="仅用于带话题的超级群组。", persistent_hint=True),
            field("VSwitch", "onlyonce", "保存后立即扫描一次"),
        ]}], {"enabled": False, "folder": "", "recursive": True, "include_subdirectories": self._recursive if self._recursive == self._cloud_recursive else None, "monitor_enabled": True, "scan_enabled": False, "output_enabled": True, "output": "", "extensions": "", "min_age": 60, "min_file_size": "", "min_file_size_gb": minimum_gb_value(self._min_file_size), "delete_source": False, "delete_small_files": False, "cloud_delete_enabled": False, "telegram_enabled": False, "telegram_aggregate": False, "telegram_aggregate_window": 600, "telegram_mode": "user", "telegram_chat_id": "", "telegram_thread_id": "", "cron": DEFAULT_CRON, "onlyonce": False, "cloud_enabled": False, "cloud_scan_enabled": False, "cloud_storage": "115网盘Plus", "cloud_folder": "", "cloud_recursive": True, "cloud_cron": DEFAULT_CRON, "cloud_read_timeout": 300, "cloud_monitor_enabled": False, "cloud_life_source": "shared"}
