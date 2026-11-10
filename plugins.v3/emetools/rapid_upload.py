"""Local file SHA1 rapid-upload workflow for 115."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from app.sdk.logging import logger
from .record_history import recent_records
from .rapid_transfer import (
    LimitedReader, SourceChanged, TransferStopped, UploadTransportError, UploadUncertain, upload_oss,
)
from .p115 import P115DirectoryUnavailable

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
    "upload_on_unmatched": False,
    "hash_limit_mib": 32,
    "upload_limit_mib": 8,
    "retry_limit": 2,
    "realtime_enabled": False,
    "cron_enabled": False,
    "cron_expression": "*/10 * * * *",
}

_RANGE_PATTERN = re.compile(r"(?<!\d)(\d+)\s*[-:]\s*(\d+)(?!\d)")
_FILE_SIGNATURE_FIELDS = ("device", "inode", "size", "mtime_ns", "ctime_ns")


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
    result["upload_on_unmatched"] = bool(result.get("upload_on_unmatched"))
    for field in ("hash_limit_mib", "upload_limit_mib"):
        try:
            result[field] = max(1, min(1024, int(result[field])))
        except (TypeError, ValueError):
            result[field] = DEFAULT_CONFIG[field]
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
    _HASH_CACHE_KEY = "rapid_upload_hash_cache"
    _LOG_KEY = "rapid_upload_logs"
    _REALTIME_POLL_SECONDS = 300
    _HASH_CACHE_LIMIT = 20000
    _HASH_CACHE_SAVE_INTERVAL = 50
    _UPLOAD_KEY = "rapid_upload_queue"
    _UPLOAD_LIMIT = 2000
    _UPLOAD_SLOT = threading.Lock()
    _HASH_CONTEXT = threading.local()

    def __init__(self, owner, saved: Any = None, upload_slot=None, scan_slot=None):
        self._owner = owner
        self._configs = validate_configs((saved or {}).get("configs", []) if isinstance(saved, dict) else [])
        saved_logs = owner.get_data(self._LOG_KEY) if hasattr(owner, "get_data") else []
        self._logs = recent_records(saved_logs, timestamp_key="timestamp", limit=200)
        for record in self._logs:
            if record.get("status") == "failed" and "未命中秒传" in str(record.get("message") or ""):
                record["status"] = "unmatched"
                record["message"] = "115 未命中：网盘中没有相同 SHA1 与大小的文件，未上传原文件"
        if hasattr(owner, "save_data") and self._logs != saved_logs:
            owner.save_data(self._LOG_KEY, self._logs)
        self._running: str | None = None
        self._observer = None
        self._observer_monitor_thread = None
        self._poll_thread = None
        self._poll_stop = threading.Event()
        self._poll_snapshots: dict[str, dict[str, tuple[int, int]]] = {}
        self._watch_timers: dict[str, Any] = {}
        self._pending_paths: dict[str, dict[str, dict[str, Any]]] = {}
        self._realtime_dispatch_paths: dict[str, set[str]] = {}
        self._realtime_dispatching: set[str] = set()
        self._activity: dict[str, dict[str, Any]] = {}
        self._watch_lock = threading.RLock()
        self._realtime_callback = None
        self._realtime_watched: list[str] = []
        self._realtime_error = ""
        self._realtime_unwatched: list[dict] = []
        saved_completed = owner.get_data(self._DATA_KEY) if hasattr(owner, "get_data") else {}
        self._completed = dict(saved_completed) if isinstance(saved_completed, dict) else {}
        saved_hashes = owner.get_data(self._HASH_CACHE_KEY) if hasattr(owner, "get_data") else {}
        self._hash_cache = dict(saved_hashes) if isinstance(saved_hashes, dict) else {}
        self._hash_cache_dirty = 0
        self._state_lock = threading.RLock()
        self._scan_lock = scan_slot or threading.Lock()
        self._stop = threading.Event()
        self._upload_wake = threading.Event()
        self._upload_thread = None
        self._upload_slot = upload_slot or self._UPLOAD_SLOT
        saved_uploads = owner.get_data(self._UPLOAD_KEY) if hasattr(owner, "get_data") else {}
        self._uploads = {}
        if isinstance(saved_uploads, dict):
            for key, entry in list(saved_uploads.items())[:self._UPLOAD_LIMIT]:
                if (not isinstance(entry, dict) or not isinstance(entry.get("config"), dict)
                        or not isinstance(entry.get("path"), str)
                        or not isinstance(entry.get("size"), int) or entry["size"] < 0
                        or not isinstance(entry.get("sha1"), str)
                        or not isinstance(entry.get("signature"), dict)):
                    continue
                restored = copy.deepcopy(entry)
                restored["config"] = normalize_config(restored["config"])
                if restored.get("state") == "uploading" and restored.get("commit_started") is False:
                    restored["state"] = "queued"
                elif restored.get("state") != "queued":
                    restored["state"] = "verify_queued"
                self._uploads[key] = restored
                self._show_upload(restored)

    @property
    def configs(self) -> list[dict]:
        return self._configs

    def save(self, values: Any) -> list[dict]:
        self._configs = validate_configs(values)
        return [dict(item) for item in self._configs]

    def status(self) -> dict:
        with self._state_lock:
            retained = recent_records(self._logs, timestamp_key="timestamp", limit=200)
            if retained != self._logs:
                self._logs = retained
                if hasattr(self._owner, "save_data"):
                    self._owner.save_data(self._LOG_KEY, list(self._logs))
            logs = copy.deepcopy(self._logs[-200:])
            upload_counts = {state: sum(entry["state"] == state for entry in self._uploads.values())
                             for state in ("queued", "uploading", "verifying", "uncertain", "verify_queued")}
        with self._watch_lock:
            activity = sorted(self._activity.values(),
                              key=lambda item: (
                                  item.get("status") in {"processing", "uploading", "verifying"},
                                  item.get("timestamp", 0)), reverse=True)
            activity_total = len(activity)
            realtime_mode = (
                "event" if self._observer is not None
                else "polling" if self._poll_thread is not None
                else "off"
            )
        return {"configs": [dict(item) for item in self._configs],
                "logs": logs,
                "upload_queue": upload_counts,
                "activity": activity[:500],
                "activity_total": activity_total,
                "running": self._running or "",
                "realtime_mode": realtime_mode,
                "realtime_available": Observer is not None and FileSystemEventHandler is not None,
                "realtime_watched_configs": list(self._realtime_watched),
                "realtime_error": self._realtime_error,
                "realtime_unwatched": [dict(item) for item in self._realtime_unwatched],
                "realtime_poll_seconds": self._REALTIME_POLL_SECONDS}

    def set_realtime_callback(self, callback) -> None:
        self._realtime_callback = callback
        self.refresh_realtime_watchers()
        self._start_upload_worker()

    def _stop_realtime_watchers(self) -> None:
        with self._watch_lock:
            for timer in self._watch_timers.values():
                timer.cancel()
            self._watch_timers.clear()
            self._pending_paths.clear()
            self._realtime_dispatch_paths.clear()
            self._realtime_dispatching.clear()
            self._activity = {key: item for key, item in self._activity.items()
                              if item.get("status") in {"upload_queued", "uploading", "verifying", "uncertain"}}
            observer = self._observer
            self._observer = None
            observer_monitor_thread = self._observer_monitor_thread
            self._observer_monitor_thread = None
            poll_thread = self._poll_thread
            self._poll_thread = None
            self._poll_stop.set()
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
        if (observer_monitor_thread and observer_monitor_thread.is_alive()
                and observer_monitor_thread is not threading.current_thread()):
            observer_monitor_thread.join(timeout=5)
        if poll_thread and poll_thread.is_alive() and poll_thread is not threading.current_thread():
            poll_thread.join(timeout=5)

    def close(self) -> None:
        with self._state_lock:
            self._stop.set()
        self._upload_wake.set()
        self._stop_realtime_watchers()
        worker = self._upload_thread
        if worker and worker is not threading.current_thread():
            worker.join(timeout=3)

    @staticmethod
    def _activity_key(config_id: str, path: str) -> str:
        return f"{config_id}:{path}"

    def _set_activity(self, config: dict, path: str, status: str,
                      message: str, size: int = 0, progress: dict | None = None) -> None:
        key = self._activity_key(config["id"], path)
        file_path = Path(path)
        record = {
            "config_id": config["id"],
            "config": config["name"],
            "name": file_path.name or f"{config['name']} 扫描任务",
            "path": path,
            "size": size,
            "status": status,
            "message": message,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": time.time(),
            "activity": True,
        }
        if progress is not None:
            record["progress"] = dict(progress)
        with self._watch_lock:
            current = self._activity.get(key)
            if current and current["status"] in {"upload_queued", "uploading", "verifying", "uncertain"} \
                    and status not in {"upload_queued", "uploading", "verifying", "uncertain"}:
                return
            self._activity[key] = record

    @staticmethod
    def _progress(phase: str, completed: int = 0, total: int = 0) -> dict:
        completed = max(0, min(completed, total))
        return {"phase": phase, "completed": completed, "total": total,
                "percent": round(completed / total * 100, 1)
                if total > 0 and phase in {"hashing", "uploading"} else None}

    def _progress_callback(self, config: dict, path: Path, status: str,
                           message: str, phase: str, total: int):
        last_reported = [None]

        def report(completed, reported_total=None):
            now = time.monotonic()
            if (last_reported[0] is not None and now - last_reported[0] < 1
                    and completed < total):
                return
            last_reported[0] = now
            self._set_activity(config, str(path), status, message, total,
                               progress=self._progress(phase, completed, total))

        return report

    def _clear_activity(self, config_id: str, path: str, include_upload=False) -> None:
        with self._watch_lock:
            key = self._activity_key(config_id, path)
            current = self._activity.get(key)
            if (current and current["status"] == "stabilizing"
                    and path in self._pending_paths.get(config_id, {})):
                return
            if include_upload or not current or current["status"] not in {
                    "upload_queued", "uploading", "verifying", "uncertain"}:
                self._activity.pop(key, None)

    def queue_scan(self, config_id: str, paths: list[str] | None = None) -> None:
        config = self._config(config_id)
        if not paths:
            self._set_activity(config, f"扫描任务：{config['name']}", "queued",
                               "等待 115 任务队列；开始扫描后显示文件")
            return
        root = Path(config["source_path"])
        if not root.is_dir():
            return
        allowed = {f".{item}" for item in config["media_extensions"].split(",") if item}
        source_root = root.resolve()
        for requested_path in dict.fromkeys(os.path.realpath(path) for path in paths):
            path = Path(requested_path)
            try:
                path.resolve().relative_to(source_root)
                size = path.stat().st_size
            except (OSError, ValueError):
                continue
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in allowed:
                self._set_activity(config, str(path), "queued", "等待 115 任务队列", size)

    def clear_scan_queue(self, config_id: str, paths: list[str] | None = None) -> None:
        config = next((item for item in self._configs if item["id"] == str(config_id)), None)
        if config:
            self._clear_activity(config_id, f"扫描任务：{config['name']}")
        for path in paths or []:
            self._clear_activity(config_id, os.path.realpath(path))

    def _queue_realtime_event(self, config_id: str, path: str) -> None:
        config = next((item for item in self._configs if item["id"] == config_id), None)
        if not config or not config.get("realtime_enabled"):
            return
        self._wait_for_stable_file(config, path)

    def _wait_for_stable_file(self, config: dict, path: str, delay: float = 0) -> None:
        if self._stop.is_set():
            return
        config_id = config["id"]
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
            not_before = max(pending.get(str(candidate), {}).get("not_before", 0),
                             time.monotonic() + delay)
            pending[str(candidate)] = {"signature": signature, "stable_checks": 0,
                                       "not_before": not_before}
            self._set_activity(config, str(candidate), "stabilizing",
                               "文件变化，等待稳定后自动复检" if delay else "等待文件写入完成", stat.st_size)
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
                    self._clear_activity(config_id, path_text)
                    continue
                if signature != state["signature"]:
                    state.update(signature=signature, stable_checks=0)
                else:
                    state["stable_checks"] += 1
                    if state["stable_checks"] >= 2 and time.monotonic() >= state.get("not_before", 0):
                        stable_paths.append(path_text)
                        config = next((item for item in self._configs if item["id"] == config_id), None)
                        if config:
                            self._set_activity(config, path_text, "queued", "等待 115 任务队列",
                                               signature[0])
                        pending.pop(path_text, None)
            if pending:
                timer = threading.Timer(3.0, self._dispatch_realtime, args=(config_id,))
                timer.daemon = True
                self._watch_timers[config_id] = timer
                timer.start()
            if not pending:
                self._pending_paths.pop(config_id, None)
        if stable_paths:
            self._queue_stable_realtime_paths(config_id, stable_paths)

    def _queue_stable_realtime_paths(self, config_id: str, paths: list[str]) -> None:
        callback = self._realtime_callback
        if not callback:
            for path in paths:
                self._clear_activity(config_id, path)
            return
        config = next((item for item in self._configs if item["id"] == config_id), None)
        if config:
            for path in paths:
                self._set_activity(config, path, "queued", "等待 115 任务队列")
        with self._watch_lock:
            queued = self._realtime_dispatch_paths.setdefault(config_id, set())
            queued.update(paths)
            if config_id in self._realtime_dispatching:
                return
            self._realtime_dispatching.add(config_id)
        worker = threading.Thread(target=self._drain_realtime_dispatch,
                                  args=(config_id, self._poll_stop),
                                  name=f"emetools-rapid-{config_id}", daemon=True)
        worker.start()

    def _drain_realtime_dispatch(self, config_id: str,
                                 stop_event: threading.Event | None = None) -> None:
        stop_event = stop_event or self._poll_stop
        retry_delay = 2
        while not stop_event.is_set():
            with self._watch_lock:
                paths = sorted(self._realtime_dispatch_paths.pop(config_id, set()))
                if not paths:
                    self._realtime_dispatching.discard(config_id)
                    return
            callback = self._realtime_callback
            if not callback:
                with self._watch_lock:
                    self._realtime_dispatching.discard(config_id)
                return
            try:
                result = callback(config_id, paths)
            except Exception:
                logger.exception("媒体增强 本地秒传：实时任务回调异常，配置=%s", config_id)
                with self._watch_lock:
                    self._realtime_dispatch_paths.setdefault(config_id, set()).update(paths)
                stop_event.wait(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
                continue
            retry_after_busy = (
                isinstance(result, dict)
                and "正在执行" in str(result.get("message") or "")
            )
            retryable = isinstance(result, dict) and result.get("retryable")
            if isinstance(result, dict) and (result.get("queued") or retry_after_busy or retryable):
                with self._watch_lock:
                    self._realtime_dispatch_paths.setdefault(config_id, set()).update(paths)
                if result.get("queued") or retry_after_busy or retryable:
                    stop_event.wait(retry_delay if retryable else 2)
                if retryable:
                    retry_delay = min(retry_delay * 2, 60)
            else:
                retry_delay = 2
        with self._watch_lock:
            self._realtime_dispatching.discard(config_id)

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
            self._poll_snapshots[config["id"]] = {}
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
                self._observer_monitor_thread = threading.Thread(
                    target=self._monitor_observer,
                    args=(observer, folders, self._poll_stop),
                    name="emetools-rapid-watchdog-health",
                    daemon=True,
                )
                self._observer_monitor_thread.start()
                logger.info("媒体增强 本地秒传：目录事件监控已启动，共 %d 个目录；使用事件驱动，不执行全目录轮询",
                            len(folders))
                return
            except Exception as error:
                if self._observer is observer:
                    self._observer = None
                self._realtime_error = (
                    f"目录事件不可用（{type(error).__name__}），改由每 {self._REALTIME_POLL_SECONDS} 秒低频扫描兜底"
                )
                logger.warning("媒体增强 本地秒传：%s", self._realtime_error)
                if observer is not None:
                    observer.stop()
                    observer.join(timeout=5)
        else:
            self._realtime_error = (
                f"watchdog 不可用，改由每 {self._REALTIME_POLL_SECONDS} 秒低频扫描兜底"
            )
            logger.warning("媒体增强 本地秒传：%s", self._realtime_error)

        self._poll_thread = threading.Thread(target=self._poll_realtime_directories,
                                             args=(folders, self._poll_stop, time.time_ns()),
                                             name="emetools-rapid-poll", daemon=True)
        self._poll_thread.start()
        logger.info("媒体增强 本地秒传：低频兜底监控已启动，目录=%d；首次建立目录基线可能较慢",
                    len(folders))

    def _monitor_observer(self, observer, folders, stop_event: threading.Event) -> None:
        while not stop_event.wait(30):
            try:
                if observer.is_alive():
                    continue
            except Exception:
                pass
            with self._watch_lock:
                if self._observer is not observer or stop_event.is_set():
                    return
                self._observer = None
                self._realtime_error = (
                    f"目录事件监控意外停止，改由每 {self._REALTIME_POLL_SECONDS} 秒低频扫描兜底"
                )
            logger.warning("媒体增强 本地秒传：%s", self._realtime_error)
            self._poll_thread = threading.Thread(
                target=self._poll_realtime_directories,
                args=(folders, stop_event, time.time_ns()),
                name="emetools-rapid-poll",
                daemon=True,
            )
            self._poll_thread.start()
            return

    @staticmethod
    def _directory_snapshot(root: Path, extensions: str,
                            stop_event: threading.Event | None = None) -> dict[str, tuple[int, int]]:
        allowed = {f".{item}" for item in extensions.split(",") if item}
        snapshot = {}
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            if stop_event and stop_event.is_set():
                break
            dirnames[:] = [name for name in dirnames if not (Path(directory) / name).is_symlink()]
            for name in filenames:
                if stop_event and stop_event.is_set():
                    break
                path = Path(directory) / name
                if path.is_symlink() or path.suffix.lower() not in allowed:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns)
        return snapshot

    def _poll_realtime_directories(self, folders=None,
                                   stop_event: threading.Event | None = None,
                                   baseline_started_ns: int | None = None) -> None:
        stop_event = stop_event or self._poll_stop
        baseline_started_ns = baseline_started_ns or time.time_ns()
        folders = folders or [
            (config, Path(config["source_path"]))
            for config in self._configs
            if config.get("realtime_enabled") and config.get("source_path")
            and config["id"] in self._poll_snapshots
        ]
        started = time.monotonic()
        created_during_startup = []
        for config, path in folders:
            if stop_event.is_set():
                return
            try:
                snapshot = self._directory_snapshot(
                    path, config["media_extensions"], stop_event
                )
            except OSError as error:
                logger.warning("媒体增强 本地秒传：初始目录快照失败，配置=%s，原因=%s",
                               config["name"], type(error).__name__)
                continue
            if stop_event.is_set():
                return
            self._poll_snapshots[config["id"]] = snapshot
            created_during_startup.extend(
                (config["id"], item_path)
                for item_path, signature in snapshot.items()
                if signature[1] >= baseline_started_ns
            )
        if stop_event.is_set():
            return
        logger.info("媒体增强 本地秒传：后台目录基线完成，目录=%d，媒体文件=%d，耗时=%.1f秒",
                    len(folders), sum(len(items) for items in self._poll_snapshots.values()),
                    time.monotonic() - started)
        for config_id, path in created_during_startup:
            if stop_event.is_set():
                return
            self._queue_realtime_event(config_id, path)
        if created_during_startup:
            logger.info("媒体增强 本地秒传：启动期间发现新增或变更文件=%d，已补入实时队列",
                        len(created_during_startup))
        while not stop_event.wait(self._REALTIME_POLL_SECONDS):
            self._poll_realtime_once(stop_event)

    def _poll_realtime_once(self, stop_event: threading.Event | None = None) -> None:
        stop_event = stop_event or self._poll_stop
        for config in self._configs:
            if stop_event.is_set():
                return
            config_id = config["id"]
            if not config.get("realtime_enabled") or config_id not in self._poll_snapshots:
                continue
            try:
                current = self._directory_snapshot(
                    Path(config["source_path"]), config["media_extensions"], stop_event
                )
            except OSError as error:
                logger.warning("媒体增强 本地秒传：实时轮询目录失败，配置=%s，原因=%s",
                               config["name"], type(error).__name__)
                continue
            if stop_event.is_set():
                return
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

    def browse_directory(self, config_id: str, directory: str) -> dict:
        config = self._config(config_id)
        source = Path(config["source_path"])
        if not source.is_absolute() or not source.is_dir() or source.is_symlink():
            raise ValueError("本地源目录不存在或不是可读取目录")

        source_path = Path(os.path.abspath(source))
        current_path = Path(os.path.abspath(directory))
        try:
            relative_path = current_path.relative_to(source_path)
        except ValueError as error:
            raise ValueError("只能浏览当前配置的本地源目录") from error

        candidate = source_path
        for part in relative_path.parts:
            candidate /= part
            if candidate.is_symlink():
                raise ValueError("不能浏览符号链接目录")

        source_root = source_path.resolve()
        current_path = current_path.resolve()
        try:
            current_path.relative_to(source_root)
        except ValueError as error:
            raise ValueError("只能浏览当前配置的本地源目录") from error
        if not current_path.is_dir():
            raise ValueError("所选本地目录不存在或不可读取")

        allowed = {f".{item}" for item in config["media_extensions"].split(",") if item}
        directories = []
        files = []
        with os.scandir(current_path) as entries:
            for entry in entries:
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    directories.append({"name": entry.name, "path": entry.path})
                elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in allowed:
                    files.append({
                        "name": entry.name,
                        "path": entry.path,
                        "size": entry.stat(follow_symlinks=False).st_size,
                    })

        directories.sort(key=lambda item: item["name"].casefold())
        files.sort(key=lambda item: item["name"].casefold())
        return {"path": str(current_path), "dirs": directories, "files": files}

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
            stop = getattr(RapidUpload._HASH_CONTEXT, "stop", threading.Event())
            reader = LimitedReader(stream, stop, getattr(RapidUpload._HASH_CONTEXT, "limit", 0))
            while True:
                chunk = reader.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                progress = getattr(RapidUpload._HASH_CONTEXT, "progress", None)
                if progress:
                    progress(size)
        return digest.hexdigest().upper(), size

    @staticmethod
    def _file_signature(stat_result: os.stat_result) -> dict[str, int]:
        return {
            "device": int(getattr(stat_result, "st_dev", 0)),
            "inode": int(getattr(stat_result, "st_ino", 0)),
            "size": int(stat_result.st_size),
            "mtime_ns": int(getattr(stat_result, "st_mtime_ns", stat_result.st_mtime * 1_000_000_000)),
            "ctime_ns": int(getattr(stat_result, "st_ctime_ns", stat_result.st_ctime * 1_000_000_000)),
        }

    @staticmethod
    def _signature_matches(record: dict | None, signature: dict[str, int]) -> bool:
        saved = record.get("signature") if isinstance(record, dict) else None
        return (isinstance(saved, dict)
                and all(saved.get(field) == signature[field] for field in _FILE_SIGNATURE_FIELDS))

    def _cache_hash(self, key: str, sha1: str, size: int,
                    signature: dict[str, int]) -> None:
        self._hash_cache[key] = {
            "sha1": sha1,
            "size": size,
            "signature": dict(signature),
        }
        if len(self._hash_cache) > self._HASH_CACHE_LIMIT:
            self._hash_cache = dict(list(self._hash_cache.items())[-self._HASH_CACHE_LIMIT:])
        self._hash_cache_dirty += 1
        if self._hash_cache_dirty >= self._HASH_CACHE_SAVE_INTERVAL:
            self._flush_hash_cache()

    def _flush_hash_cache(self) -> None:
        if not self._hash_cache_dirty:
            return
        try:
            if hasattr(self._owner, "save_data"):
                self._owner.save_data(self._HASH_CACHE_KEY, dict(self._hash_cache))
            self._hash_cache_dirty = 0
        except Exception as error:
            logger.warning("媒体增强 本地秒传：SHA1 缓存写入失败（%s）", type(error).__name__)

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
                path.name,
                sha1,
                size,
                "",
                read_range,
                target,
                timeout=30,
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
            nested = response.get("data") if isinstance(response.get("data"), dict) else {}
            normalized["reuse"] = (
                bool(response.get("reuse"))
                or bool(nested.get("reuse"))
                or normalized["status"] == 2
            )
            return normalized
        response = client.upload_init(
            {
                "filename": path.name,
                "filesize": size,
                "fileid": sha1,
                "target": target,
            },
            read_range_bytes_or_hash=read_range,
            timeout=30,
        )
        if not isinstance(response, dict):
            raise TypeError("115 秒传接口返回了无效响应")
        nested = response.get("data") if isinstance(response.get("data"), dict) else {}
        status = response.get("status", nested.get("status"))
        try:
            status = int(status or 0)
        except (TypeError, ValueError):
            status = 0
        normalized = dict(response)
        normalized["status"] = status
        normalized["reuse"] = (
            bool(response.get("reuse"))
            or bool(nested.get("reuse"))
            or status == 2
        )
        return normalized

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

    def _short_request(self, callback):
        def guarded():
            if self._stop.is_set():
                raise TransferStopped("插件已停止，未发送115请求")
            from .p115 import _request_gate
            import httpx
            gate = _request_gate(self._owner._source_cookie())
            with gate.lock:
                gate.check()
                if gate.last_finished is not None:
                    delay = 1 - (time.monotonic() - gate.last_finished)
                    if delay > 0 and self._stop.wait(delay):
                        raise TransferStopped("插件已停止，未发送115请求")
                try:
                    result = callback()
                    gate.failures = 0
                    return result
                except (httpx.RequestError, ConnectionError, TimeoutError):
                    delay = gate.defer(minimum=60, retry_reads=False)
                    raise UploadUncertain(f"115 网络请求结果待核实，已暂停短请求{delay}秒，未重放提交") from None
                finally:
                    gate.last_finished = time.monotonic()
        coordinator = getattr(self._owner, "_run_p115_task", None)
        if callable(coordinator):
            return coordinator(f"rapid_api:{threading.get_ident()}", "本地秒传接口请求", guarded)
        return guarded()

    @staticmethod
    def _upload_key(config: dict, path: Path) -> str:
        return f"{config['target_folder_id']}:{path.resolve()}"

    def _save_uploads(self):
        if hasattr(self._owner, "save_data"):
            self._owner.save_data(self._UPLOAD_KEY, copy.deepcopy(self._uploads))

    def _show_upload(self, entry):
        state = entry["state"]
        status = "upload_queued" if state == "queued" else "verifying" if state == "verify_queued" else state
        message = {
            "queued": "独立队列排队中；其他文件可继续秒传",
            "uploading": "检查目标文件与上传凭据，不占用秒传通道",
            "verify_queued": "重启或上次结果不确定，等待只读复核，不重复上传",
            "verifying": "核对网盘名称、大小与 SHA1",
            "uncertain": "保留源文件；立即扫描可再次只读复核",
        }.get(state, state)
        if state == "queued" and entry.get("retry_at", 0) > time.time():
            message = entry.get("retry_reason") or "目标目录暂不可读，稍后自动复查；尚未提交普通上传"
        phase = "preparing" if state == "uploading" else "verifying" if state == "verifying" else None
        self._set_activity(entry["config"], entry["path"], status, message, entry["size"],
                           progress=self._progress(phase) if phase else None)

    def _queue_upload(self, config, path, sha1, size, signature):
        key = self._upload_key(config, path)
        with self._state_lock:
            if self._stop.is_set():
                raise TransferStopped("插件已停止，未加入普通上传队列")
            if key in self._uploads:
                return False
            if len(self._uploads) >= self._UPLOAD_LIMIT:
                raise ValueError("普通上传待办已达2000项上限，源文件保留，请等待队列消化后再扫描")
            entry = {"config": copy.deepcopy(config), "path": str(path), "sha1": sha1,
                     "size": size, "signature": dict(signature), "state": "queued", "commit_started": False}
            self._uploads[key] = entry
            try:
                self._save_uploads()
            except Exception:
                self._uploads.pop(key, None)
                raise
            self._show_upload(entry)
        self._start_upload_worker()
        return True

    def _start_upload_worker(self):
        if self._stop.is_set():
            return
        with self._state_lock:
            self._upload_wake.set()
            if not self._uploads or self._upload_thread and self._upload_thread.is_alive():
                return
            worker = threading.Thread(target=self._drain_uploads, name="emetools-normal-upload", daemon=True)
            self._upload_thread = worker
            worker.start()

    def _verify_upload(self, client, entry):
        try:
            return self._read_uploaded_file(client, entry)
        except TransferStopped:
            raise
        except Exception as error:
            raise UploadUncertain("只读复核失败，保留源文件与待办，不重复提交上传") from error

    def _read_uploaded_file(self, client, entry):
        offset = 0
        while True:
            response = self._short_request(lambda: client.fs_files({
                "cid": entry["config"]["target_folder_id"], "offset": offset, "limit": 200,
                "show_dir": 0, "o": "user_ptime", "asc": 0,
            }, timeout=30))
            if not isinstance(response, dict) or not response.get("state"):
                raise UploadUncertain("无法读取目标目录，待核实结果")
            items = response.get("data")
            if not isinstance(items, list):
                raise UploadUncertain("目标目录返回不完整，待核实结果")
            for item in items:
                if (str(item.get("n") or item.get("name") or item.get("file_name") or "") == Path(entry["path"]).name
                        and str(item.get("sha") or item.get("sha1") or item.get("file_sha1") or "").upper() == entry["sha1"]
                        and int(item.get("s") or item.get("size") or item.get("file_size") or 0) == entry["size"]):
                    return True
            offset += len(items)
            total = response.get("count")
            if total is None or (not items and offset < int(total)):
                raise UploadUncertain("目标目录分页不完整，待核实结果")
            if offset >= int(total):
                return False

    def _upload_original(self, path: Path, config: dict, client, entry):
        self._set_activity(config, str(path), "uploading", "申请上传凭据，不占用秒传通道", entry["size"],
                           progress=self._progress("preparing"))

        def initialize():
            with self._state_lock:
                if self._stop.is_set():
                    raise TransferStopped("插件已停止，未提交上传初始化")
                entry["commit_started"] = True
                self._save_uploads()
            return self._rapid_init(client, path, entry["sha1"], entry["size"],
                                    f"U_1_{config['target_folder_id']}")

        try:
            response = self._short_request(initialize)
        except (TransferStopped, P115DirectoryUnavailable):
            raise
        except Exception as error:
            raise UploadUncertain("上传初始化结果待核实，未自动重放") from error
        if response.get("reuse") or response.get("status") == 2:
            return
        if response.get("status") != 1:
            raise RuntimeError("115 上传初始化失败，未上传原文件")
        with self._state_lock:
            entry["commit_started"] = False
            self._save_uploads()
        progress = self._progress_callback(config, path, "uploading", "不占用秒传通道",
                                           "uploading", entry["size"])
        progress(0)

        def before_complete():
            if path.is_symlink() or not self._signature_matches(entry, self._file_signature(path.stat())):
                raise SourceChanged("源文件在上传过程中变化，未提交完成")
            with self._state_lock:
                if self._stop.is_set():
                    raise TransferStopped("插件已停止，未提交上传完成")
                entry["commit_started"] = True
                self._save_uploads()
            self._set_activity(config, str(path), "uploading", "文件已发送，等待网盘确认", entry["size"],
                               progress=self._progress("committing", entry["size"], entry["size"]))

        upload_oss(client, path, response, self._stop, config["upload_limit_mib"],
                   self._short_request, progress, before_complete)

    def _drain_uploads(self):
        from .p115 import _request_gate

        while not self._stop.is_set():
            with self._state_lock:
                candidate = next(((key, item) for key, item in self._uploads.items()
                                  if item["state"] in {"queued", "verify_queued"}
                                  and item.get("retry_at", 0) <= time.time()), None)
            if candidate is None:
                self._upload_wake.wait(1)
                self._upload_wake.clear()
                continue
            remaining = _request_gate(self._owner._source_cookie()).remaining()
            if remaining:
                self._stop.wait(min(remaining, 5))
                continue
            if not self._upload_slot.acquire(timeout=0.5):
                continue
            try:
                if not self._stop.is_set():
                    self._process_upload(*candidate)
            finally:
                self._upload_slot.release()

    def _process_upload(self, key, entry):
        client = None
        verified = False
        config = entry["config"]
        path = Path(entry["path"])
        try:
            client = self._p115_client(self._owner._source_cookie())
            recovery = entry["state"] == "verify_queued"
            if self._stop.is_set():
                return
            with self._state_lock:
                entry["state"] = "verifying" if recovery else "uploading"
                self._save_uploads()
                self._show_upload(entry)
            if recovery:
                if not self._verify_upload(client, entry):
                    raise UploadUncertain("未确认网盘中存在完整文件，保留源文件；不自动重新上传")
            else:
                if path.is_symlink() or not self._signature_matches(entry, self._file_signature(path.stat())):
                    raise SourceChanged("待上传源文件已变化，未上传或移动")
                try:
                    exists = self._verify_upload(client, entry)
                except UploadUncertain:
                    with self._state_lock:
                        if self._stop.is_set():
                            return
                        entry.update(state="queued", retry_at=time.time() + 60)
                        self._save_uploads()
                        self._show_upload(entry)
                    logger.warning("媒体增强 本地秒传：目标目录暂不可读，配置=%s 文件=%s；60秒后复查，未提交普通上传",
                                   config["name"], path.name)
                    return
                if not exists:
                    self._upload_original(path, config, client, entry)
                    if self._stop.is_set():
                        return
                    with self._state_lock:
                        entry["state"] = "verifying"
                        self._save_uploads()
                        self._show_upload(entry)
                    if not self._verify_upload(client, entry):
                        raise UploadUncertain("上传已提交但目录尚未确认，保留源文件，不重复上传")
            verified = True
            if self._stop.is_set():
                return
            with self._state_lock:
                if self._stop.is_set():
                    return
                if (config["delete_source_on_success"] and path.exists() and not path.is_symlink()
                        and self._signature_matches(entry, self._file_signature(path.stat()))):
                    path.unlink()
                self._remember_completed(config, path, entry["sha1"], entry["size"], entry["signature"])
                self._append_log(config, path, "uploaded", "普通上传成功，已复核网盘大小与 SHA1",
                                 entry["size"], entry["sha1"])
                self._uploads.pop(key, None)
                self._save_uploads()
                self._clear_activity(config["id"], str(path), include_upload=True)
        except TransferStopped:
            return
        except Exception as error:
            if self._stop.is_set():
                return
            with self._state_lock:
                if self._stop.is_set():
                    return
                if isinstance(error, P115DirectoryUnavailable) and not entry.get("commit_started"):
                    entry.update(state="queued", retry_at=time.time() + 60)
                    self._save_uploads()
                    self._show_upload(entry)
                    return
                if verified or isinstance(error, UploadUncertain) or entry.get("commit_started"):
                    entry["state"] = "uncertain"
                    self._save_uploads()
                    self._show_upload(entry)
                    status = "uncertain"
                    detail = str(error) if isinstance(error, UploadUncertain) else type(error).__name__
                    message = f"普通上传结果待核实：{detail}；源文件保留，立即扫描可只读复核"
                elif isinstance(error, SourceChanged):
                    self._uploads.pop(key, None)
                    self._save_uploads()
                    self._clear_activity(config["id"], str(path), include_upload=True)
                    self._wait_for_stable_file(config, str(path), delay=30)
                    status = "stabilizing"
                    message = f"{error}；保留源文件，等待稳定后自动复检"
                elif (isinstance(error, UploadTransportError) and error.retryable
                      and entry.get("upload_retries", 0) < min(config["retry_limit"], 2)):
                    retries = entry.get("upload_retries", 0) + 1
                    message = f"{error}；未提交完成，保留源文件，{60 * retries}秒后自动重试"
                    entry.update(state="queued", upload_retries=retries, retry_at=time.time() + 60 * retries,
                                 retry_reason=message)
                    self._save_uploads()
                    self._show_upload(entry)
                    status = "retrying"
                else:
                    status = "failed"
                    detail = str(error) if isinstance(error, UploadTransportError) else type(error).__name__
                    message = f"普通上传失败：{detail}；未提交上传完成"
                    if (path.exists() and not path.is_symlink() and config.get("failure_directory")
                            and self._signature_matches(entry, self._file_signature(path.stat()))):
                        try:
                            self._move_failure(path, config)
                            message += "；已移至失败目录"
                        except Exception:
                            message += "；移动失败，源文件保留"
                    else:
                        message += "；源文件保留"
                    self._uploads.pop(key, None)
                    self._save_uploads()
                    self._clear_activity(config["id"], str(path), include_upload=True)
                self._append_log(config, path, status, message, entry["size"], entry["sha1"])
                logger.warning("媒体增强 本地秒传普通上传未完成：配置=%s 文件=%s 状态=%s 原因=%s",
                               config["name"], path.name, status, message)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def _append_log(self, config: dict, path: Path, status: str, message: str, size: int = 0, sha1: str = ""):
        with self._state_lock:
            self._logs.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "timestamp": time.time(),
                               "config": config["name"],
                               "path": str(path), "name": path.name, "size": size,
                               "sha1": sha1, "status": status, "message": message})
            self._logs = recent_records(self._logs, limit=200)
            if hasattr(self._owner, "save_data"):
                self._owner.save_data(self._LOG_KEY, list(self._logs))

    def clear_logs(self):
        with self._state_lock:
            self._logs.clear()
            if hasattr(self._owner, "save_data"):
                self._owner.save_data(self._LOG_KEY, [])

    def _remember_completed(self, config: dict, path: Path, sha1: str, size: int,
                            signature: dict[str, int]) -> None:
        with self._state_lock:
            self._save_completed(config, path, sha1, size, signature)

    def _save_completed(self, config: dict, path: Path, sha1: str, size: int,
                        signature: dict[str, int]) -> None:
        key = f"{config['id']}:{path.resolve()}"
        self._completed[key] = {
            "sha1": sha1,
            "size": size,
            "target": config["target_folder_id"],
            "signature": dict(signature),
        }
        if len(self._completed) > 20000:
            self._completed = dict(list(self._completed.items())[-20000:])
        if hasattr(self._owner, "save_data"):
            self._owner.save_data(self._DATA_KEY, dict(self._completed))

    def run(self, config_id: str, paths: list[str] | None = None) -> dict:
        if not self._scan_lock.acquire(blocking=False):
            return {"ok": False, "message": f"配置“{self._running or '其他配置'}”正在执行，请稍后再试"}
        try:
            return self._run_serial(config_id, paths)
        finally:
            self._scan_lock.release()

    def _run_serial(self, config_id: str, paths: list[str] | None = None) -> dict:
        config = copy.deepcopy(self._config(config_id))
        if self._stop.is_set():
            return {"ok": False, "message": "插件已停止，未提交秒传或上传"}
        self._clear_activity(config_id, f"扫描任务：{config['name']}")
        if self._running:
            return {"ok": False, "message": f"配置“{self._running}”正在执行，请稍后再试"}
        cookie = self._owner._source_cookie()
        if not cookie:
            raise ValueError("请先在 115 网盘 STRM 助手中配置 Cookie")
        if not config["source_path"]:
            raise ValueError("请先填写本地源目录")
        if not config["target_folder_id"]:
            raise ValueError("请先选择 115 目标目录")
        if paths:
            root = Path(config["source_path"])
            if not root.is_absolute() or not root.is_dir() or root.is_symlink():
                raise ValueError("本地源目录不存在或不是可读取目录")
            allowed = {f".{item}" for item in config["media_extensions"].split(",") if item}
            source_root = root.resolve()
            source_files = []
            for requested_path in dict.fromkeys(os.path.realpath(path) for path in paths):
                path = Path(requested_path)
                try:
                    path.resolve().relative_to(source_root)
                except (OSError, ValueError):
                    continue
                if path.is_file() and not path.is_symlink() and path.suffix.lower() in allowed:
                    source_files.append(path)
            source_files.sort(key=lambda item: str(item).lower())
        else:
            source_files = self._iter_files(config["source_path"], config["media_extensions"])
        if not source_files:
            if paths:
                for path in paths:
                    self._clear_activity(config_id, os.path.realpath(path))
            self._clear_activity(config_id, f"扫描任务：{config['name']}")
            return {"ok": True, "message": "未发现符合后缀的本地文件", "total": 0, "succeeded": [], "failed": []}
        self._running = config["name"]
        self._HASH_CONTEXT.stop = self._stop
        self._HASH_CONTEXT.limit = config["hash_limit_mib"]
        self._start_upload_worker()
        for path in source_files:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            self._set_activity(config, str(path), "queued", "等待当前秒传任务处理", size)
        succeeded: list[dict] = []
        uploaded: list[dict] = []
        skipped: list[dict] = []
        unmatched: list[dict] = []
        failed: list[dict] = []
        stabilizing: list[dict] = []
        upload_queued: list[dict] = []
        client = None
        try:
            client = self._p115_client(cookie)
            target = f"U_1_{config['target_folder_id']}"
            for path in source_files:
                if self._stop.is_set():
                    break
                size = 0
                sha1 = ""
                signature = None
                self._set_activity(config, str(path), "processing", "正在检查文件状态与缓存")
                try:
                    before = path.stat()
                    signature = self._file_signature(before)
                    with self._watch_lock:
                        waiting = self._pending_paths.get(config_id, {}).get(str(path))
                        if waiting and time.monotonic() < waiting.get("not_before", 0):
                            self._set_activity(config, str(path), "stabilizing", "等待文件稳定后自动复检",
                                               signature["size"])
                            stabilizing.append({"path": str(path), "name": path.name, "size": signature["size"]})
                            continue
                    with self._state_lock:
                        pending = self._uploads.get(self._upload_key(config, path))
                        if pending:
                            if pending["state"] == "uncertain":
                                pending["state"] = "verify_queued"
                                self._save_uploads()
                                self._show_upload(pending)
                                self._upload_wake.set()
                            skipped.append({"path": str(path), "name": path.name, "size": before.st_size,
                                            "message": "已在普通上传队列中，未重复提交"})
                            continue
                    key = f"{config['id']}:{path.resolve()}"
                    completed = self._completed.get(key)
                    if (completed and completed.get("target") == config["target_folder_id"]
                            and completed.get("size") == signature["size"]
                            and self._signature_matches(completed, signature)):
                        sha1 = str(completed.get("sha1") or "")
                        size = signature["size"]
                        skipped.append({"path": str(path), "name": path.name, "size": size, "sha1": sha1})
                        continue
                    cached_hash = self._hash_cache.get(key)
                    if (cached_hash and cached_hash.get("size") == signature["size"]
                            and self._signature_matches(cached_hash, signature)):
                        sha1 = str(cached_hash.get("sha1") or "")
                        size = signature["size"]
                        if not sha1:
                            cached_hash = None
                    if not cached_hash or not sha1:
                        self._HASH_CONTEXT.progress = self._progress_callback(
                            config, path, "processing", "正在计算 SHA1", "hashing", signature["size"])
                        self._HASH_CONTEXT.progress(0)
                        sha1, size = self._sha1(path)
                        self._HASH_CONTEXT.progress = None
                        after_signature = self._file_signature(path.stat())
                        if signature != after_signature or size != signature["size"]:
                            raise SourceChanged("文件在计算 SHA1 期间发生变化")
                        self._cache_hash(key, sha1, size, signature)
                    if (completed and completed.get("sha1") == sha1 and completed.get("size") == size
                            and completed.get("target") == config["target_folder_id"]):
                        self._remember_completed(config, path, sha1, size, signature)
                        skipped.append({"path": str(path), "name": path.name, "size": size, "sha1": sha1})
                        continue
                    response = None
                    attempts = min(config["retry_limit"] + 1, 4)
                    for attempt in range(attempts):
                        try:
                            self._set_activity(config, str(path), "processing",
                                               "等待 115 返回秒传结果", size,
                                               progress=self._progress("rapid"))
                            response = self._short_request(
                                lambda: self._rapid_init(client, path, sha1, size, target))
                            break
                        except Exception:
                            if attempt + 1 >= attempts:
                                raise
                    if self._stop.is_set():
                        break
                    status = int((response or {}).get("status") or 0)
                    if not (response or {}).get("reuse") and status not in (1, 2):
                        raise RuntimeError(f"115 秒传初始化未完成（返回状态 {status}）")
                    if not (response or {}).get("reuse") and status == 1:
                        item = {"path": str(path), "name": path.name, "size": size,
                                "sha1": sha1}
                        if config["upload_on_unmatched"]:
                            try:
                                created = self._queue_upload(config, path, sha1, size, signature)
                                upload_queued.append(item)
                                if created:
                                    self._append_log(config, path, "upload_queued",
                                                     "115 未命中（正常结果），已加入独立普通上传队列", size, sha1)
                            except TransferStopped:
                                raise
                            except Exception as error:
                                message = f"普通上传排队失败（{type(error).__name__}），源文件保留"
                                item["message"] = message
                                failed.append(item)
                                self._append_log(config, path, "failed", message, size, sha1)
                        else:
                            message = "115 未命中（状态 1）：网盘中没有相同 SHA1 与大小的文件，未上传原文件"
                            item["message"] = message
                            unmatched.append(item)
                            self._append_log(config, path, "unmatched", message, size, sha1)
                        continue
                    item = {"path": str(path), "name": path.name, "size": size, "sha1": sha1}
                    succeeded.append(item)
                    self._remember_completed(config, path, sha1, size, signature)
                    self._append_log(config, path, "succeeded", "115 秒传成功", size, sha1)
                    if (config["delete_source_on_success"] and not self._stop.is_set()
                            and self._signature_matches({"signature": signature}, self._file_signature(path.stat()))):
                        path.unlink()
                except TransferStopped:
                    break
                except SourceChanged as error:
                    message = f"{error}；保留源文件，等待稳定后自动复检"
                    stabilizing.append({"path": str(path), "name": path.name, "size": size, "message": message})
                    self._wait_for_stable_file(config, str(path), delay=30)
                    self._append_log(config, path, "stabilizing", message, size)
                    logger.info("媒体增强 本地秒传：配置=%s 文件=%s %s", config["name"], path.name, message)
                except Exception as error:
                    message = f"{type(error).__name__}：{str(error)[:160]}"
                    moved = ""
                    try:
                        if (signature is not None and path.exists() and not path.is_symlink()
                                and self._signature_matches({"signature": signature}, self._file_signature(path.stat()))):
                            moved = self._move_failure(path, config)
                    except Exception as move_error:
                        message += f"；失败目录移动失败：{type(move_error).__name__}"
                    item = {"path": str(path), "name": path.name, "size": size,
                            "sha1": sha1, "message": message}
                    if moved:
                        item["failure_path"] = moved
                    failed.append(item)
                    self._append_log(config, path, "failed", message, size, sha1)
                    logger.warning("媒体增强 本地秒传失败：配置=%s 文件=%s 原因=%s：%s",
                                   config["name"], path.name, type(error).__name__, str(error)[:160])
                finally:
                    self._HASH_CONTEXT.progress = None
                    self._clear_activity(config_id, str(path))
            return {
                "ok": not failed and not self._stop.is_set(),
                "message": (
                    f"本轮检查 {len(source_files)} 个文件，成功 {len(succeeded)} 个，"
                    f"普通上传已排队 {len(upload_queued)} 个（独立执行），已处理跳过 {len(skipped)} 个，"
                    f"等待稳定 {len(stabilizing)} 个，未命中 {len(unmatched)} 个，失败 {len(failed)} 个"
                ),
                "total": len(source_files), "succeeded": succeeded, "skipped": skipped,
                "uploaded": uploaded, "upload_queued": upload_queued, "stabilizing": stabilizing,
                "unmatched": unmatched, "failed": failed,
            }
        finally:
            self._flush_hash_cache()
            close = getattr(client, "close", None)
            if callable(close):
                close()
            self._running = None
            self._HASH_CONTEXT.limit = 0
            self._HASH_CONTEXT.progress = None
            self._HASH_CONTEXT.stop = threading.Event()
            self.clear_scan_queue(config_id)
            for path in source_files:
                self._clear_activity(config_id, str(path))
