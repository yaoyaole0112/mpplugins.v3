"""Local file SHA1 rapid-upload workflow for 115."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from app.sdk.logging import logger

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:
    FileSystemEventHandler = None
    Observer = None


DEFAULT_RAPID_CONFIG = {
    "configs": [],
}

DEFAULT_CONFIG = {
    "id": None,
    "name": "",
    "source_path": "",
    "target_folder_id": "",
    "target_folder_path": "",
    "media_extensions": "mp4,mkv,ts,iso,rmvb,avi,mov,mpeg,mpg,wmv,3gp,asf,m4v,flv,m2ts,tp,f4v",
    "failure_directory": "",
    "delete_source_on_success": False,
    "retry_limit": 2,
    "realtime_enabled": False,
    "cron_enabled": False,
    "cron_expression": "*/10 * * * *",
}

_RANGE_PATTERN = re.compile(r"(?<!\d)(\d+)\s*[-:]\s*(\d+)(?!\d)")


def _normalize_extensions(value: Any) -> str:
    values = []
    for item in re.split(r"[,\s]+", str(value or "")):
        suffix = item.strip().lower().lstrip(".")
        if suffix and re.fullmatch(r"[a-z0-9]{1,12}", suffix) and suffix not in values:
            values.append(suffix)
    return ",".join(values)


def normalize_config(value: Any, index: int = 0) -> dict:
    source = value if isinstance(value, dict) else {}
    result = {**DEFAULT_CONFIG, **source}
    result["id"] = str(result.get("id") or f"rapid_{index + 1}").strip()
    result["name"] = str(result.get("name") or f"配置 {index + 1}").strip()[:80]
    result["source_path"] = str(result.get("source_path") or "").strip()
    result["target_folder_id"] = str(result.get("target_folder_id") or "").strip()
    result["target_folder_path"] = str(result.get("target_folder_path") or "").strip()[:300]
    result["media_extensions"] = _normalize_extensions(result.get("media_extensions"))
    result["failure_directory"] = str(result.get("failure_directory") or "").strip()
    result["delete_source_on_success"] = bool(result.get("delete_source_on_success"))
    result["realtime_enabled"] = bool(result.get("realtime_enabled"))
    result["cron_enabled"] = bool(result.get("cron_enabled"))
    result["cron_expression"] = str(result.get("cron_expression") or "*/10 * * * *").strip()
    try:
        result["retry_limit"] = max(0, min(100, int(result.get("retry_limit") or 0)))
    except (TypeError, ValueError):
        result["retry_limit"] = 2
    return result


def validate_configs(values: Any) -> list[dict]:
    if not isinstance(values, list) or len(values) > 50:
        raise ValueError("本地秒传配置最多保存 50 条")
    configs = []
    seen = set()
    for index, value in enumerate(values):
        config = normalize_config(value, index)
        if config["id"] in seen:
            raise ValueError("本地秒传配置 ID 不能重复")
        seen.add(config["id"])
        if not config["name"]:
            raise ValueError("本地秒传配置名称不能为空")
        if config["source_path"] and not os.path.isabs(config["source_path"]):
            raise ValueError("本地源目录必须是绝对路径")
        if config["failure_directory"] and not os.path.isabs(config["failure_directory"]):
            raise ValueError("失败目录必须是绝对路径")
        if config["target_folder_id"] and (not config["target_folder_id"].isdecimal() or config["target_folder_id"] == "0"):
            raise ValueError("115 目标目录 CID 必须是非零数字")
        if not config["media_extensions"]:
            raise ValueError("媒体后缀不能为空")
        if config["failure_directory"] and config["source_path"]:
            source = os.path.realpath(config["source_path"])
            failure = os.path.realpath(config["failure_directory"])
            if failure == source or failure.startswith(source + os.sep):
                raise ValueError("失败目录不能位于源目录内")
        configs.append(config)
    return configs


class RapidUpload:
    _DATA_KEY = "rapid_upload_completed"
    _REALTIME_POLL_SECONDS = 30

    def __init__(self, owner, saved: Any = None):
        self._owner = owner
        self._configs = validate_configs((saved or {}).get("configs", []) if isinstance(saved, dict) else [])
        self._logs: list[dict] = []
        self._running: str | None = None
        self._observer = None
        self._poll_thread = None
        self._poll_stop = threading.Event()
        self._poll_snapshots: dict[str, dict[str, tuple[int, int]]] = {}
        self._watch_timers: dict[str, Any] = {}
        self._pending_paths: dict[str, dict[str, dict[str, Any]]] = {}
        self._watch_lock = threading.RLock()
        self._realtime_callback = None
        self._realtime_watched: list[str] = []
        self._realtime_error = ""
        self._realtime_unwatched: list[dict] = []
        saved_completed = owner.get_data(self._DATA_KEY) if hasattr(owner, "get_data") else {}
        self._completed = dict(saved_completed) if isinstance(saved_completed, dict) else {}

    @property
    def configs(self) -> list[dict]:
        return self._configs

    def save(self, values: Any) -> list[dict]:
        self._configs = validate_configs(values)
        return [dict(item) for item in self._configs]

    def status(self) -> dict:
        return {"configs": [dict(item) for item in self._configs],
                "logs": [dict(item) for item in self._logs[-200:]],
                "running": self._running or "",
                "realtime_available": Observer is not None and FileSystemEventHandler is not None,
                "realtime_watched_configs": list(self._realtime_watched),
                "realtime_error": self._realtime_error,
                "realtime_unwatched": [dict(item) for item in self._realtime_unwatched],
                "realtime_poll_seconds": self._REALTIME_POLL_SECONDS}

    def set_realtime_callback(self, callback) -> None:
        self._realtime_callback = callback
        self.refresh_realtime_watchers()

    def _stop_realtime_watchers(self) -> None:
        with self._watch_lock:
            for timer in self._watch_timers.values():
                timer.cancel()
            self._watch_timers.clear()
            self._pending_paths.clear()
            observer = self._observer
            self._observer = None
            poll_thread = self._poll_thread
            self._poll_thread = None
            self._poll_stop.set()
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
        if poll_thread and poll_thread.is_alive() and poll_thread is not threading.current_thread():
            poll_thread.join(timeout=5)

    def close(self) -> None:
        self._stop_realtime_watchers()

    def _queue_realtime_event(self, config_id: str, path: str) -> None:
        config = next((item for item in self._configs if item["id"] == config_id), None)
        if not config or not config.get("realtime_enabled"):
            return
        candidate = Path(path)
        if candidate.is_symlink() or not candidate.is_file():
            return
        allowed = {f".{item}" for item in config["media_extensions"].split(",") if item}
        if candidate.suffix.lower() not in allowed:
            return
        with self._watch_lock:
            previous = self._watch_timers.pop(config_id, None)
            if previous:
                previous.cancel()
            try:
                stat = candidate.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                return
            pending = self._pending_paths.setdefault(config_id, {})
            pending[str(candidate)] = {"signature": signature, "stable_checks": 0}
            timer = threading.Timer(3.0, self._dispatch_realtime, args=(config_id,))
            timer.daemon = True
            self._watch_timers[config_id] = timer
            timer.start()

    def _dispatch_realtime(self, config_id: str) -> None:
        with self._watch_lock:
            self._watch_timers.pop(config_id, None)
            pending = self._pending_paths.get(config_id, {})
            stable_paths = []
            for path_text, state in list(pending.items()):
                path = Path(path_text)
                try:
                    stat = path.stat()
                    signature = (stat.st_size, stat.st_mtime_ns)
                except OSError:
                    pending.pop(path_text, None)
                    continue
                if signature != state["signature"]:
                    state.update(signature=signature, stable_checks=0)
                else:
                    state["stable_checks"] += 1
                    if state["stable_checks"] >= 2:
                        stable_paths.append(path_text)
                        pending.pop(path_text, None)
            if pending:
                timer = threading.Timer(3.0, self._dispatch_realtime, args=(config_id,))
                timer.daemon = True
                self._watch_timers[config_id] = timer
                timer.start()
            if not pending:
                self._pending_paths.pop(config_id, None)
        callback = self._realtime_callback
        if callback and stable_paths:
            callback(config_id, stable_paths)

    def refresh_realtime_watchers(self) -> None:
        self._stop_realtime_watchers()
        self._realtime_watched = []
        self._realtime_error = ""
        self._realtime_unwatched = []
        self._poll_stop = threading.Event()
        self._poll_snapshots = {}
        folders = []
        for item in self._configs:
            if not item.get("realtime_enabled"):
                continue
            path = Path(item["source_path"]) if item.get("source_path") else None
            reason = "未设置本地源目录"
            if path and path.is_symlink():
                reason = "源目录是符号链接，为避免监控到意外路径已跳过"
            elif path and not path.exists():
                reason = "源目录在 MoviePilot 容器内不存在或当前不可见"
            elif path and not path.is_dir():
                reason = "源路径不是目录"
            elif path and not os.access(path, os.R_OK | os.X_OK):
                reason = "MoviePilot 进程没有源目录的读取/遍历权限"
            elif path:
                folders.append((item, path))
                continue
            self._realtime_unwatched.append({"id": item["id"], "name": item["name"], "reason": reason})
        if not folders:
            return
        plugin = self

        for config, path in folders:
            self._poll_snapshots[config["id"]] = self._directory_snapshot(path, config["media_extensions"])
            self._realtime_watched.append(config["id"])
        observer = None
        if Observer is not None and FileSystemEventHandler is not None:
            class Handler(FileSystemEventHandler):
                def __init__(self, config_id: str):
                    super().__init__()
                    self.config_id = config_id

                def _queue(self, path: str) -> None:
                    plugin._queue_realtime_event(self.config_id, path)

                def on_created(self, event):
                    if not event.is_directory:
                        self._queue(event.src_path)

                def on_modified(self, event):
                    if not event.is_directory:
                        self._queue(event.src_path)

                def on_closed(self, event):
                    if not event.is_directory:
                        self._queue(event.src_path)

                def on_moved(self, event):
                    if not event.is_directory:
                        self._queue(event.dest_path)

            try:
                observer = Observer()
                for config, path in folders:
                    observer.schedule(Handler(config["id"]), str(path), recursive=True)
                observer.start()
                if not observer.is_alive():
                    raise RuntimeError("目录事件监控线程未能运行")
                self._observer = observer
                logger.info("媒体增强 本地秒传：目录事件监控已启动，共 %d 个目录", len(folders))
            except Exception as error:
                self._realtime_error = f"目录事件不可用（{type(error).__name__}），由 {self._REALTIME_POLL_SECONDS} 秒轮询兜底"
                logger.warning("媒体增强 本地秒传：%s", self._realtime_error)
                if observer is not None:
                    observer.stop()
                    observer.join(timeout=5)
        else:
            self._realtime_error = f"watchdog 不可用，由 {self._REALTIME_POLL_SECONDS} 秒轮询兜底"
            logger.warning("媒体增强 本地秒传：%s", self._realtime_error)

        self._poll_thread = threading.Thread(target=self._poll_realtime_directories,
                                             name="emetools-rapid-poll", daemon=True)
        self._poll_thread.start()
        logger.info("媒体增强 本地秒传：监控已就绪，目录=%d，差异轮询间隔=%d秒",
                    len(folders), self._REALTIME_POLL_SECONDS)

    @staticmethod
    def _directory_snapshot(root: Path, extensions: str) -> dict[str, tuple[int, int]]:
        allowed = {f".{item}" for item in extensions.split(",") if item}
        snapshot = {}
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [name for name in dirnames if not (Path(directory) / name).is_symlink()]
            for name in filenames:
                path = Path(directory) / name
                if path.is_symlink() or path.suffix.lower() not in allowed:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns)
        return snapshot

    def _poll_realtime_directories(self) -> None:
        while not self._poll_stop.wait(self._REALTIME_POLL_SECONDS):
            self._poll_realtime_once()

    def _poll_realtime_once(self) -> None:
        for config in self._configs:
            config_id = config["id"]
            if not config.get("realtime_enabled") or config_id not in self._poll_snapshots:
                continue
            try:
                current = self._directory_snapshot(Path(config["source_path"]), config["media_extensions"])
            except OSError as error:
                logger.warning("媒体增强 本地秒传：实时轮询目录失败，配置=%s，原因=%s",
                               config["name"], type(error).__name__)
                continue
            previous = self._poll_snapshots.get(config_id, {})
            changed = [path for path, signature in current.items() if previous.get(path) != signature]
            self._poll_snapshots[config_id] = current
            for path in changed:
                self._queue_realtime_event(config_id, path)

    def _config(self, config_id: str) -> dict:
        config = next((item for item in self._configs if item["id"] == str(config_id)), None)
        if not config:
            raise ValueError("本地秒传配置不存在")
        return config

    @staticmethod
    def _iter_files(root: str, extensions: str) -> list[Path]:
        path = Path(root)
        if not path.is_absolute() or not path.is_dir() or path.is_symlink():
            raise ValueError("本地源目录不存在或不是可读取目录")
        allowed = {f".{item}" for item in extensions.split(",") if item}
        return sorted((item for item in path.rglob("*") if item.is_file() and not item.is_symlink()
                       and item.suffix.lower() in allowed), key=lambda item: str(item).lower())

    @staticmethod
    def _sha1(path: Path, chunk_size: int = 8 * 1024 * 1024) -> tuple[str, int]:
        digest = hashlib.sha1()
        size = 0
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest().upper(), size

    @staticmethod
    def _range_hash(path: Path, sign_check: Any) -> str:
        match = _RANGE_PATTERN.search(str(sign_check or ""))
        if match:
            start, end = int(match.group(1)), int(match.group(2))
        else:
            start, end = 0, min(path.stat().st_size, 1024 * 1024) - 1
        if end < start:
            raise ValueError("115 二次校验范围无效")
        with path.open("rb") as stream:
            stream.seek(start)
            return hashlib.sha1(stream.read(end - start + 1)).hexdigest().upper()

    @staticmethod
    def _p115_client(cookie: str):
        try:
            from p115client import P115Client
        except ImportError as error:
            raise RuntimeError("MoviePilot 未安装 p115client 依赖，无法执行 115 秒传") from error
        return P115Client(cookie)

    def _rapid_init(self, client, path: Path, sha1: str, size: int, target: str):
        read_range = lambda sign_check: self._range_hash(path, sign_check)
        upload_file_init = getattr(client, "upload_file_init", None)
        if callable(upload_file_init):
            response = upload_file_init(
                filename=path.name,
                filesize=size,
                filesha1=sha1,
                pid=target,
                read_range_bytes_or_hash=read_range,
            )
            if not isinstance(response, dict):
                raise TypeError("115 秒传接口返回了无效响应")
            data = response.get("data")
            status = response.get("status")
            if status is None and isinstance(data, dict):
                status = data.get("status")
            try:
                status = int(status or 0)
            except (TypeError, ValueError):
                status = 0
            normalized = dict(response)
            normalized["status"] = status
            normalized["reuse"] = bool(response.get("reuse")) or normalized["status"] == 2
            return normalized
        return client.upload_init(
            {
                "filename": path.name,
                "filesize": size,
                "fileid": sha1,
                "target": target,
            },
            read_range_bytes_or_hash=read_range,
        )

    def _move_failure(self, path: Path, config: dict) -> str:
        failure_root = str(config.get("failure_directory") or "").strip()
        if not failure_root:
            return ""
        source_root = Path(config["source_path"]).resolve()
        destination_root = Path(failure_root).resolve()
        relative = path.resolve().relative_to(source_root)
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(destination))
        return str(destination)

    def _append_log(self, config: dict, path: Path, status: str, message: str, size: int = 0, sha1: str = ""):
        self._logs.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "config": config["name"],
                           "path": str(path), "name": path.name, "size": size,
                           "sha1": sha1, "status": status, "message": message})
        del self._logs[:-200]

    def _remember_completed(self, config: dict, path: Path, sha1: str, size: int) -> None:
        key = f"{config['id']}:{path.resolve()}"
        self._completed[key] = {"sha1": sha1, "size": size, "target": config["target_folder_id"]}
        if len(self._completed) > 20000:
            self._completed = dict(list(self._completed.items())[-20000:])
        if hasattr(self._owner, "save_data"):
            self._owner.save_data(self._DATA_KEY, dict(self._completed))

    def run(self, config_id: str, paths: list[str] | None = None) -> dict:
        config = self._config(config_id)
        if self._running:
            return {"ok": False, "message": f"配置“{self._running}”正在执行，请稍后再试"}
        cookie = self._owner._source_cookie()
        if not cookie:
            raise ValueError("请先在 115 网盘 STRM 助手中配置 Cookie")
        if not config["source_path"]:
            raise ValueError("请先填写本地源目录")
        if not config["target_folder_id"]:
            raise ValueError("请先选择 115 目标目录")
        source_files = self._iter_files(config["source_path"], config["media_extensions"])
        if paths:
            requested = {os.path.realpath(path) for path in paths}
            source_files = [path for path in source_files if os.path.realpath(path) in requested]
        if not source_files:
            return {"ok": True, "message": "未发现符合后缀的本地文件", "total": 0, "succeeded": [], "failed": []}
        self._running = config["name"]
        succeeded: list[dict] = []
        skipped: list[dict] = []
        failed: list[dict] = []
        client = None
        try:
            client = self._p115_client(cookie)
            target = f"U_1_{config['target_folder_id']}"
            for path in source_files:
                try:
                    before = path.stat()
                    sha1, size = self._sha1(path)
                    after = path.stat()
                    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                        raise RuntimeError("文件在计算 SHA1 期间发生变化，已跳过")
                    completed = self._completed.get(f"{config['id']}:{path.resolve()}")
                    if (completed and completed.get("sha1") == sha1 and completed.get("size") == size
                            and completed.get("target") == config["target_folder_id"]):
                        skipped.append({"path": str(path), "name": path.name, "size": size, "sha1": sha1})
                        continue
                    response = None
                    attempts = min(config["retry_limit"] + 1, 4)
                    for attempt in range(attempts):
                        try:
                            response = self._rapid_init(client, path, sha1, size, target)
                            break
                        except Exception:
                            if attempt + 1 >= attempts:
                                raise
                    status = int((response or {}).get("status") or 0)
                    if not (response or {}).get("reuse") and status != 2:
                        raise RuntimeError("115 未命中秒传，未上传原文件")
                    item = {"path": str(path), "name": path.name, "size": size, "sha1": sha1}
                    succeeded.append(item)
                    self._remember_completed(config, path, sha1, size)
                    self._append_log(config, path, "succeeded", "115 秒传成功", size, sha1)
                    if config["delete_source_on_success"]:
                        path.unlink()
                except Exception as error:
                    message = f"{type(error).__name__}：{str(error)[:160]}"
                    moved = ""
                    try:
                        if path.exists():
                            moved = self._move_failure(path, config)
                    except Exception as move_error:
                        message += f"；失败目录移动失败：{type(move_error).__name__}"
                    item = {"path": str(path), "name": path.name, "message": message}
                    if moved:
                        item["failure_path"] = moved
                    failed.append(item)
                    self._append_log(config, path, "failed", message)
                    logger.warning("媒体增强 本地秒传失败：配置=%s 文件=%s 原因=%s：%s",
                                   config["name"], path.name, type(error).__name__, str(error)[:160])
            return {"ok": not failed, "message": f"本轮检查 {len(source_files)} 个文件，成功 {len(succeeded)} 个，已处理跳过 {len(skipped)} 个，失败 {len(failed)} 个",
                    "total": len(source_files), "succeeded": succeeded, "skipped": skipped, "failed": failed}
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
            self._running = None
