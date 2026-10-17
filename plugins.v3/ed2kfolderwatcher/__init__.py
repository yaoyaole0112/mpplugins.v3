import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from apscheduler.triggers.cron import CronTrigger

from app.log import logger
from app.plugins import _PluginBase


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
    plugin_name = "本地文件夹 ED2K 链接"
    plugin_desc = "监控本地文件夹，为稳定的新文件自动生成标准 ED2K 链接。"
    plugin_icon = "https://raw.githubusercontent.com/MoviePilot/MoviePilot-Plugins/main/icons/link.png"
    plugin_version = "1.1.0"
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
                        if self._delete_source:
                            delete_candidates.append(path)
                        continue
                    file_hash = ed2k_hash(path)
                    final_stat = path.stat()
                    if final_stat.st_size != stat.st_size or final_stat.st_mtime_ns != stat.st_mtime_ns:
                        logger.info(f"本地文件夹 ED2K 链接：文件仍在变化，留待下次扫描：{path}")
                        continue
                    records[key] = {**signature, "name": path.name, "link": ed2k_link(path, file_hash)}
                    if self._delete_source:
                        delete_candidates.append(path)
                    logger.info(f"本地文件夹 ED2K 链接：已处理 {path}")
                except (OSError, ValueError) as err:
                    logger.warning(f"本地文件夹 ED2K 链接：跳过 {path}：{err}")
            ordered = sorted(records.values(), key=lambda item: item["link"].lower())
            output.write_text("\n".join(item["link"] for item in ordered) + ("\n" if ordered else ""), encoding="utf-8")
            cache_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            if self._delete_source:
                for path in delete_candidates:
                    try:
                        path.unlink()
                        logger.info(f"本地文件夹 ED2K 链接：已删除源文件：{path}")
                    except OSError as err:
                        logger.warning(f"本地文件夹 ED2K 链接：删除源文件失败（已保留）：{path}：{err}")
            logger.info(f"本地文件夹 ED2K 链接：扫描完成，共 {len(ordered)} 个文件，输出：{output}")
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
        self.update_config({"enabled": self._enabled, "folder": self._folder, "recursive": self._recursive, "cron": self._cron, "output": self._output, "extensions": self._extensions, "min_age": self._min_age, "delete_source": self._delete_source, "onlyonce": self._onlyonce})

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [{"component": "VForm", "content": [
            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}},
            {"component": "VTextField", "props": {"model": "folder", "label": "监控目录", "placeholder": "/downloads/complete", "hint": "必须是 MoviePilot 容器内可见的本地路径。", "persistent-hint": True}},
            {"component": "VSwitch", "props": {"model": "recursive", "label": "扫描子目录"}},
            {"component": "VTextField", "props": {"model": "output", "label": "输出文件路径", "placeholder": "留空则写入监控目录/ed2k.txt"}},
            {"component": "VTextField", "props": {"model": "extensions", "label": "文件扩展名过滤", "placeholder": "留空处理全部文件，例如 mkv,mp4,flac"}},
            {"component": "VTextField", "props": {"model": "min_age", "label": "文件稳定等待秒数", "type": "number", "hint": "文件修改时间距现在不足此秒数时跳过。"}},
            {"component": "VSwitch", "props": {"model": "delete_source", "label": "生成完成后删除源文件", "hint": "ED2K 生成并成功写入清单后按队列逐个删除；删除失败会保留源文件。", "persistent-hint": True}},
            {"component": "VTextField", "props": {"model": "cron", "label": "扫描计划", "hint": "默认每 10 分钟：*/10 * * * *"}},
            {"component": "VSwitch", "props": {"model": "onlyonce", "label": "保存后立即扫描一次"}},
        ]}], {"enabled": False, "folder": "", "recursive": True, "output": "", "extensions": "", "min_age": 60, "delete_source": False, "cron": DEFAULT_CRON, "onlyonce": False}
