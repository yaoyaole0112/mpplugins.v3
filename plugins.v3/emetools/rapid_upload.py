"""Local file SHA1 rapid-upload workflow for 115."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from app.sdk.logging import logger


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
    def __init__(self, owner, saved: Any = None):
        self._owner = owner
        self._configs = validate_configs((saved or {}).get("configs", []) if isinstance(saved, dict) else [])
        self._logs: list[dict] = []
        self._running: str | None = None

    @property
    def configs(self) -> list[dict]:
        return self._configs

    def save(self, values: Any) -> list[dict]:
        self._configs = validate_configs(values)
        return [dict(item) for item in self._configs]

    def status(self) -> dict:
        return {"configs": [dict(item) for item in self._configs],
                "logs": [dict(item) for item in self._logs[-200:]],
                "running": self._running or ""}

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
        return client.upload_init(
            filename=path.name,
            filesize=size,
            filesha1=sha1,
            pid=target,
            read_range_bytes_or_hash=lambda sign_check: self._range_hash(path, sign_check),
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
                    logger.warning("媒体增强 本地秒传失败：配置=%s 文件=%s 原因=%s", config["name"], path.name, type(error).__name__)
            return {"ok": not failed, "message": f"本轮处理 {len(source_files)} 个文件，成功 {len(succeeded)} 个，失败 {len(failed)} 个",
                    "total": len(source_files), "succeeded": succeeded, "failed": failed}
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
            self._running = None
