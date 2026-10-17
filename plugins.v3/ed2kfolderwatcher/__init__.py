import json
import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from apscheduler.triggers.cron import CronTrigger

try:
    from Crypto.Hash import MD4 as _CryptoMD4
except ImportError:
    _CryptoMD4 = None

from app.log import logger
from app.plugins import _PluginBase

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


def ed2k_hash(path: Path) -> str:
    if _CryptoMD4 is not None:
        part_hashes = []
        with path.open("rb") as file_handle:
            while True:
                chunk = file_handle.read(ED2K_PART_SIZE)
                if not chunk:
                    break
                digest = _CryptoMD4.new()
                digest.update(chunk)
                part_hashes.append(digest.digest())
        if not part_hashes:
            return _CryptoMD4.new().hexdigest()
        if len(part_hashes) == 1:
            return part_hashes[0].hex()
        digest = _CryptoMD4.new()
        digest.update(b"".join(part_hashes))
        return digest.hexdigest()
    part_hashes = []
    with path.open("rb") as file_handle:
        while True:
            chunk = file_handle.read(ED2K_PART_SIZE)
            if not chunk:
                break
            part_hashes.append(_md4(chunk))
    if not part_hashes:
        return _md4(b"").hex()
    return _md4(b"".join(part_hashes)).hex() if len(part_hashes) > 1 else part_hashes[0].hex()


def ed2k_link(path: Path, file_hash: str) -> str:
    return f"ed2k://|file|{quote(path.name, safe='')}|{path.stat().st_size}|{file_hash}|/"


class Ed2kFolderWatcher(_PluginBase):
    plugin_name = "自动生成ED2K链接"
    plugin_desc = "监控本地文件夹，为新增且稳定的文件自动生成标准 ED2K 链接。"
    plugin_icon = "https://raw.githubusercontent.com/MoviePilot/MoviePilot-Plugins/main/icons/link.png"
    plugin_version = "1.2.0"
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
    _telegram_token = ""
    _telegram_chat_id = ""
    _telegram_thread_id = ""
    _onlyonce = False
    _running = False
    _lock = threading.Lock()

    def init_plugin(self, config: dict = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._folder = str(config.get("folder") or "").strip()
        self._recursive = bool(config.get("recursive", True))
        self._cron = str(config.get("cron") or DEFAULT_CRON).strip()
        self._output = str(config.get("output") or "").strip()
        self._extensions = str(config.get("extensions") or DEFAULT_EXTENSIONS).strip()
        self._delete_source = bool(config.get("delete_source", False))
        self._telegram_enabled = bool(config.get("telegram_enabled", False))
        self._telegram_token = str(config.get("telegram_token") or "").strip()
        self._telegram_chat_id = str(config.get("telegram_chat_id") or "").strip()
        self._telegram_thread_id = str(config.get("telegram_thread_id") or "").strip()
        try:
            self._min_age = max(0, int(config.get("min_age", 60)))
            CronTrigger.from_crontab(self._cron)
        except Exception as err:
            self._min_age = 60
            self._cron = DEFAULT_CRON
            logger.error(f"本地文件夹 ED2K 链接：配置无效：{err}")
        self._onlyonce = bool(config.get("onlyonce", False))
        if self._onlyonce and self._folder:
            self._onlyonce = False
            self._save_config()
            threading.Thread(target=self.scan, name="ed2k-folder-scan-once", daemon=True).start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def _paths(self) -> Tuple[Path, Path, Path]:
        folder = Path(self._folder).expanduser()
        output = Path(self._output).expanduser() if self._output else folder / "ed2k.txt"
        cache = output.with_name(f".{output.name}.cache.json")
        return folder, output, cache

    def _extension_set(self) -> set[str]:
        return {item.strip().lower().lstrip(".") for item in self._extensions.split(",") if item.strip()}

    def _telegram_target_key(self) -> str:
        value = f"{self._telegram_token}|{self._telegram_chat_id}|{self._telegram_thread_id}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest() if value.strip("|") else ""

    def _send_telegram(self, path: Path, link: str) -> bool:
        if not self._telegram_enabled:
            return True
        if not self._telegram_token or not self._telegram_chat_id:
            logger.warning("自动生成ED2K链接：Telegram 通知已启用，但 Bot Token 或 Chat ID 为空")
            return False
        if TelegramBotApi is None:
            logger.warning("自动生成ED2K链接：当前 MoviePilot 未提供 Telegram Bot API")
            return False
        try:
            thread_id = int(self._telegram_thread_id) if self._telegram_thread_id else None
            bot = TelegramBotApi(self._telegram_token)
            bot.send_message(
                self._telegram_chat_id,
                f"自动生成 ED2K 链接\n文件：{path.name}\n\n{link}",
                disable_web_page_preview=True,
                message_thread_id=thread_id,
            )
            bot.close()
            logger.info(f"自动生成ED2K链接：Telegram 通知已发送：{path.name}")
            return True
        except (TelegramApiError, TelegramNetworkError, ValueError) as err:
            logger.warning(f"自动生成ED2K链接：Telegram 通知失败（保留待重试）：{type(err).__name__}: {err}")
            return False
        except Exception as err:
            logger.warning(f"自动生成ED2K链接：Telegram 通知异常（保留待重试）：{type(err).__name__}: {err}")
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
    def _write_results(output: Path, cache_path: Path, records: dict) -> None:
        ordered = sorted(records.values(), key=lambda item: item["link"].lower())
        output.write_text("\n".join(item["link"] for item in ordered) + ("\n" if ordered else ""), encoding="utf-8")
        cache_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    def scan(self) -> None:
        if not self._folder or not self._lock.acquire(blocking=False):
            return
        self._running = True
        try:
            folder, output, cache_path = self._paths()
            if not folder.is_dir():
                logger.warning(f"本地文件夹 ED2K 链接：目录不存在：{folder}")
                return
            output.parent.mkdir(parents=True, exist_ok=True)
            try:
                cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
            except Exception:
                cache = {}
            extensions = self._extension_set()
            records = {}
            delete_candidates = []
            for path in self._iter_files(folder):
                try:
                    resolved = path.resolve()
                    if resolved == output.resolve() or resolved == cache_path.resolve():
                        continue
                    stat = path.stat()
                    if extensions and path.suffix.lower().lstrip(".") not in extensions:
                        continue
                    if self._min_age and (stat.st_mtime + self._min_age > time.time()):
                        continue
                    key = str(resolved)
                    signature = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
                    cached = cache.get(key)
                    if cached and cached.get("size") == signature["size"] and cached.get("mtime_ns") == signature["mtime_ns"]:
                        records[key] = {**signature, "name": path.name, "link": cached["link"]}
                        records[key]["telegram_sent"] = bool(cached.get("telegram_sent"))
                        records[key]["telegram_target"] = cached.get("telegram_target", "")
                        self._write_results(output, cache_path, records)
                        self._notify_record(path, records[key])
                        self._write_results(output, cache_path, records)
                        if self._delete_source:
                            delete_candidates.append(path)
                        continue
                    file_hash = ed2k_hash(path)
                    final_stat = path.stat()
                    if final_stat.st_size != stat.st_size or final_stat.st_mtime_ns != stat.st_mtime_ns:
                        logger.info(f"本地文件夹 ED2K 链接：文件仍在变化，留待下次扫描：{path}")
                        continue
                    records[key] = {
                        **signature,
                        "name": path.name,
                        "link": ed2k_link(path, file_hash),
                        "telegram_sent": False,
                        "telegram_target": "",
                    }
                    self._write_results(output, cache_path, records)
                    self._notify_record(path, records[key])
                    self._write_results(output, cache_path, records)
                    if self._delete_source:
                        delete_candidates.append(path)
                    logger.info(f"本地文件夹 ED2K 链接：已处理 {path}")
                except (OSError, ValueError) as err:
                    logger.warning(f"本地文件夹 ED2K 链接：跳过 {path}：{err}")
            self._write_results(output, cache_path, records)
            if self._delete_source:
                for path in delete_candidates:
                    try:
                        path.unlink()
                        logger.info(f"本地文件夹 ED2K 链接：已删除源文件：{path}")
                    except OSError as err:
                        logger.warning(f"本地文件夹 ED2K 链接：删除源文件失败（已保留）：{path}：{err}")
            logger.info(f"本地文件夹 ED2K 链接：扫描完成，共 {len(records)} 个文件，输出：{output}")
        except Exception as err:
            logger.error(f"本地文件夹 ED2K 链接扫描失败：{err}", exc_info=True)
        finally:
            self._running = False
            self._lock.release()

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._folder:
            return []
        return [{"id": "Ed2kFolderWatcher", "name": "扫描本地文件夹并生成 ED2K", "trigger": CronTrigger.from_crontab(self._cron), "func": self.scan}]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{"path": "/run", "endpoint": self.scan, "methods": ["GET"], "summary": "立即扫描并生成 ED2K 链接", "description": "扫描配置的本地文件夹并更新 ed2k.txt。"}]

    def get_page(self) -> Optional[List[dict]]:
        return None

    def stop_service(self) -> None:
        return None

    def _save_config(self) -> None:
        self.update_config({"enabled": self._enabled, "folder": self._folder, "recursive": self._recursive, "cron": self._cron, "output": self._output, "extensions": self._extensions, "min_age": self._min_age, "delete_source": self._delete_source, "telegram_enabled": self._telegram_enabled, "telegram_token": self._telegram_token, "telegram_chat_id": self._telegram_chat_id, "telegram_thread_id": self._telegram_thread_id, "onlyonce": self._onlyonce})

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
            field("VTextField", "output", "输出文件路径", placeholder="留空则写入监控目录/ed2k.txt"),
            field("VTextField", "extensions", "文件扩展名过滤", placeholder="留空处理全部文件，例如 mkv,mp4,flac"),
            field("VTextField", "min_age", "文件稳定等待秒数", type="number", hint="文件修改时间距现在不足此秒数时跳过。", persistent_hint=True),
            field("VSwitch", "delete_source", "生成完成后删除源文件", hint="ED2K 生成并成功写入清单后按队列逐个删除；删除失败会保留源文件。", persistent_hint=True),
            field("VTextField", "cron", "扫描计划", hint="默认每 10 分钟：*/10 * * * *", persistent_hint=True),
            field("VSwitch", "telegram_enabled", "发送 ED2K 链接到 Telegram"),
            field("VTextField", "telegram_token", "Telegram Bot Token", type="password", hint="从 @BotFather 获取；仅保存在 MoviePilot 配置中。", persistent_hint=True),
            field("VTextField", "telegram_chat_id", "Telegram Chat ID", placeholder="例如 @my_channel 或 -1001234567890", hint="机器人必须已加入目标群组或频道并有发消息权限。", persistent_hint=True),
            field("VTextField", "telegram_thread_id", "Telegram 话题线程 ID", placeholder="可选，仅超级群组话题需要填写"),
            field("VSwitch", "onlyonce", "保存后立即扫描一次"),
        ]}], {"enabled": False, "folder": "", "recursive": True, "output": "", "extensions": "", "min_age": 60, "delete_source": False, "telegram_enabled": False, "telegram_token": "", "telegram_chat_id": "", "telegram_thread_id": "", "cron": DEFAULT_CRON, "onlyonce": False}
