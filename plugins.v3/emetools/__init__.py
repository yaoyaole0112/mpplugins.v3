"""Standalone MoviePilot tools ported from the MediaEnhance workflows."""

import asyncio
import copy
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Tuple

from apscheduler.triggers.cron import CronTrigger
from fastapi import HTTPException
import httpx
from pydantic import BaseModel, Field

from app.plugins import _PluginBase
from app.scheduler import Scheduler
from app.sdk.logging import logger

from .invalid_data import InvalidDataCleaner
from .p115 import P115Client
from .subscription_monitor import SubscriptionMonitor, normalize_channel


DEFAULT_SCHEDULE = {
    "tools": {"enabled": False, "cron": "0 3 * * *", "path": "/strm", "auto_delete": True,
              "confirm_cleanup": False},
    "p115_cleanup": {"enabled": False, "cron": "0 */2 * * *", "dir_ids": [], "dir_names": []},
    "p115_trash": {"enabled": False, "cron": "0 3 * * *"},
    "p115_move": {"enabled": False, "check_interval": 120, "rules": []},
}
SCHEDULE_FIELDS = {
    "tools": {"enabled", "cron", "path", "auto_delete", "confirm_cleanup"},
    "p115_cleanup": {"enabled", "cron", "dir_ids", "dir_names"},
    "p115_trash": {"enabled", "cron"},
    "p115_move": {"enabled", "check_interval", "rules"},
}


class ToolAction(BaseModel):
    operation: str
    path: str = ""
    cid: str = "0"
    paths: List[str] = Field(default_factory=list)
    scan_token: str = ""
    token: str = ""


class ScheduleChange(BaseModel):
    section: str
    settings: Dict[str, Any]


class MonitorChange(BaseModel):
    operation: str
    scope: str = "sub"
    channels: List[str] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)
    blacklist: List[str] = Field(default_factory=list)
    phone: str = ""
    code: str = ""
    password: str = ""


class EmeTools(_PluginBase):
    plugin_name = "媒体清理转存工具"
    plugin_desc = "订阅频道监控、无效数据清理、115 文件清理、回收站清空与文件转存。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot/v3/docs/images/moviepilot.png"
    plugin_version = "2.2.2"
    plugin_author = "helios"
    plugin_order = 46
    plugin_config_prefix = "emetools_"
    auth_level = 2

    def init_plugin(self, config: dict = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled", True))
        self._show_sidebar_nav = bool(config.get("show_sidebar_nav", True))
        self._strm_root = str(config.get("strm_root") or "/strm").strip()
        self._rb_password = str(config.get("rb_password") or "000000")
        self._tg_api_id = str(config.get("tg_api_id") or "").strip()
        self._tg_api_hash = str(config.get("tg_api_hash") or "").strip()
        self._tg_forward_token = str(config.get("tg_forward_token") or "").strip()
        self._tg_session = str(config.get("tg_session") or "")
        saved_monitor = config.get("monitor") or {}
        self._monitor_config = {
            scope: {"enabled": bool((saved_monitor.get(scope) or {}).get("enabled", False)),
                    "channels": list((saved_monitor.get(scope) or {}).get("channels") or []),
                    "keywords": list((saved_monitor.get(scope) or {}).get("keywords") or []),
                    "blacklist": list((saved_monitor.get(scope) or {}).get("blacklist") or [])}
            for scope in ("sub", "kw")
        }
        self._monitor = SubscriptionMonitor(self)
        self._schedule = copy.deepcopy(DEFAULT_SCHEDULE)
        for name, defaults in self._schedule.items():
            saved = (config.get("schedule") or {}).get(name) or {}
            defaults.update({key: value for key, value in saved.items() if key in SCHEDULE_FIELDS[name]})
        self._cleaner = InvalidDataCleaner(self._strm_root)
        self._locks = {name: threading.Lock() for name in self._schedule}
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._last_run = {}
        if "cookie" in config or "proxy" in config:
            self._persist()
        if self._enabled and self._tg_session and any(item["enabled"] for item in self._monitor_config.values()):
            self._monitor._ensure_loop()
            asyncio.run_coroutine_threadsafe(self._monitor.resume(), self._monitor.loop)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[dict]:
        return []

    def stop_service(self) -> None:
        monitor = getattr(self, "_monitor", None)
        if monitor and monitor.loop and monitor.thread and monitor.thread.is_alive():
            try:
                asyncio.run_coroutine_threadsafe(monitor.shutdown(), monitor.loop).result(timeout=8)
            except Exception as exc:
                logger.warning(f"媒体清理转存工具 Telegram 客户端关闭失败: {type(exc).__name__}")
            monitor.loop.call_soon_threadsafe(monitor.loop.stop)
            monitor.thread.join(timeout=3)

    @staticmethod
    def get_render_mode() -> Tuple[str, str]:
        return "vue", "dist/assets"

    def get_sidebar_nav(self) -> List[dict]:
        if not self._enabled or not self._show_sidebar_nav:
            return []
        return [{"nav_key": "main", "title": "媒体清理转存工具", "icon": "mdi-tools",
                 "section": "organize", "permission": "manage", "order": 46}]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [], self._settings()

    @staticmethod
    def get_page() -> List[dict]:
        return []

    def get_service(self) -> List[dict]:
        if not self._enabled:
            return []
        services = []
        for name in ("tools", "p115_cleanup", "p115_trash"):
            config = self._schedule[name]
            if config.get("enabled"):
                try:
                    trigger = CronTrigger.from_crontab(config["cron"])
                except (KeyError, ValueError) as error:
                    logger.error(f"媒体清理转存工具 {name} 定时表达式无效：{error}")
                    continue
                services.append({"id": f"EmeTools_{name}", "name": f"媒体清理转存工具 {name}",
                                 "trigger": trigger, "func": self._run_scheduled,
                                 "kwargs": {}, "func_kwargs": {"section": name}})
        move = self._schedule["p115_move"]
        if move.get("enabled") and move.get("rules"):
            services.append({"id": "EmeTools_p115_move", "name": "媒体清理转存工具文件转存",
                             "trigger": "interval", "func": self._run_scheduled,
                             "kwargs": {"seconds": max(60, int(move.get("check_interval") or 120))},
                             "func_kwargs": {"section": "p115_move"}})
        return services

    def _settings(self) -> dict:
        return {"enabled": self._enabled, "show_sidebar_nav": self._show_sidebar_nav,
                "strm_root": self._strm_root, "cookie_configured": bool(self._source_cookie()),
                "rb_password_configured": self._rb_password != "000000", "tg_api_id": self._tg_api_id,
                "tg_api_hash_configured": bool(self._tg_api_hash),
                "tg_forward_token_configured": bool(self._tg_forward_token)}

    def _persist(self) -> None:
        self.update_config({"enabled": self._enabled, "show_sidebar_nav": self._show_sidebar_nav,
                            "strm_root": self._strm_root, "rb_password": self._rb_password,
                            "schedule": copy.deepcopy(self._schedule),
                            "tg_api_id": self._tg_api_id, "tg_api_hash": self._tg_api_hash,
                            "tg_forward_token": self._tg_forward_token, "tg_session": self._tg_session,
                            "monitor": copy.deepcopy(self._monitor_config)})

    def _source_cookie(self) -> str:
        config = self.get_config("P115StrmHelper") or {}
        return str(config.get("cookies") or "").strip() if isinstance(config, dict) else ""

    @staticmethod
    def _validate_cid(value: Any) -> str:
        cid = str(value or "").strip()
        if not cid.isdecimal() or cid == "0":
            raise HTTPException(status_code=400, detail="115 文件夹 CID 必须是非零数字")
        return cid

    def _client(self) -> P115Client:
        return P115Client(self._source_cookie())

    def _locked(self, section: str, callback):
        lock = self._locks[section]
        if not lock.acquire(blocking=False):
            return {"ok": False, "message": "该任务正在执行，请稍后重试"}
        try:
            return callback()
        finally:
            lock.release()

    def _remember(self, kind: str, snapshot: dict) -> str:
        with self._pending_lock:
            now = time.monotonic()
            self._pending = {key: item for key, item in self._pending.items() if now - item[0] < 1800}
            token = uuid.uuid4().hex
            self._pending[token] = (now, kind, snapshot)
            return token

    def _consume(self, token: str, kind: str):
        with self._pending_lock:
            pending = self._pending.pop(token, None)
        if not pending or pending[1] != kind or time.monotonic() - pending[0] >= 1800:
            raise HTTPException(status_code=400, detail="确认已过期或已处理，请重新预览")
        return pending[2]

    async def status(self) -> dict:
        return {"settings": self._settings(), "schedule": copy.deepcopy(self._schedule),
                "last_run": dict(self._last_run)}

    async def save_settings(self, settings: dict) -> dict:
        allowed = {"enabled", "show_sidebar_nav", "strm_root", "rb_password",
                   "tg_api_id", "tg_api_hash", "tg_forward_token"}
        if set(settings) - allowed:
            raise HTTPException(status_code=400, detail="连接设置包含未知字段")
        root = str(settings.get("strm_root", self._strm_root)).strip()
        try:
            InvalidDataCleaner(root).resolve(root)
        except (ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        reset_tools_path = False
        try:
            InvalidDataCleaner(root).resolve(self._schedule["tools"]["path"])
        except (ValueError, OSError) as error:
            if self._schedule["tools"]["enabled"]:
                raise HTTPException(status_code=400, detail="请先关闭定时清理，再更换 STRM 根目录") from error
            reset_tools_path = True
        password = str(settings.get("rb_password") or self._rb_password)
        if not re.fullmatch(r"\d{6}", password):
            raise HTTPException(status_code=400, detail="115 回收站安全密钥必须为 6 位数字")
        api_id = str(settings.get("tg_api_id", self._tg_api_id) or "").strip()
        api_hash = str(settings.get("tg_api_hash") or self._tg_api_hash).strip()
        token = str(settings.get("tg_forward_token") or self._tg_forward_token).strip()
        if api_id and (not api_id.isdecimal() or int(api_id) < 1):
            raise HTTPException(status_code=400, detail="Telegram API ID 必须是正整数")
        if api_hash and not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
            raise HTTPException(status_code=400, detail="Telegram API Hash 必须是 32 位十六进制字符串")
        if token and not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", token):
            raise HTTPException(status_code=400, detail="转发 Bot Token 格式不正确")
        if (api_id, api_hash) != (self._tg_api_id, self._tg_api_hash) and self._tg_session:
            raise HTTPException(status_code=400, detail="更改 Telegram API 凭据前，请先退出当前账号")
        self._enabled = bool(settings.get("enabled", self._enabled))
        self._show_sidebar_nav = bool(settings.get("show_sidebar_nav", self._show_sidebar_nav))
        self._strm_root = root
        self._cleaner = InvalidDataCleaner(root)
        if reset_tools_path:
            self._schedule["tools"]["path"] = root
        self._rb_password = password
        self._tg_api_id, self._tg_api_hash, self._tg_forward_token = api_id, api_hash, token
        if not self._enabled:
            for scope in ("sub", "kw"):
                self._monitor_config[scope]["enabled"] = False
                self._monitor.channel_ids[scope].clear()
        self._persist()
        Scheduler().update_plugin_job(self.__class__.__name__)
        return {"ok": True, "settings": self._settings()}

    @staticmethod
    def _subscription_items() -> List[dict]:
        from app.db.oper.subscribe import SubscribeOper
        fields = ("id", "name", "year", "type", "media_source", "media_id", "season",
                  "poster", "state", "lack_episode", "total_episode", "start_episode")
        return [{key: getattr(item, key, None) for key in fields} for item in SubscribeOper().list()
                if getattr(item, "name", None) or getattr(item, "media_id", None)]

    async def monitor_status(self) -> dict:
        return await self._monitor.call(self._monitor.status())

    async def monitor_action(self, change: MonitorChange) -> dict:
        operation, scope = change.operation, change.scope
        if operation == "send_code":
            coro = self._monitor.send_code(change.phone)
        elif operation == "sign_in":
            coro = self._monitor.sign_in(change.code, change.password)
        elif operation == "logout":
            coro = self._monitor.logout()
        elif scope not in ("sub", "kw"):
            raise HTTPException(status_code=400, detail="未知监控类型")
        elif operation == "save":
            if self._monitor_config[scope]["enabled"]:
                raise HTTPException(status_code=400, detail="请先停止监控，再修改频道或关键词")
            try:
                channels = list(dict.fromkeys(normalize_channel(item) for item in change.channels))
                keywords = [str(item).strip() for item in change.keywords if str(item).strip()]
                blacklist = [str(item).strip() for item in change.blacklist if str(item).strip()]
                if len(channels) > 40 or len(keywords) > 100 or len(blacklist) > 100:
                    raise ValueError("频道或关键词数量超过限制")
                if any(len(value) > 150 for value in keywords + blacklist):
                    raise ValueError("关键词过长")
                for value in keywords + blacklist:
                    re.compile(value)
            except (ValueError, re.error) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            self._monitor_config[scope].update(channels=channels, keywords=keywords, blacklist=blacklist)
            self._persist()
            return {"ok": True}
        elif operation in ("start", "stop"):
            if operation == "start" and not self._enabled:
                raise HTTPException(status_code=400, detail="请先启用插件")
            coro = self._monitor.start(scope) if operation == "start" else self._monitor.stop(scope)
        else:
            raise HTTPException(status_code=400, detail="未知监控操作")
        try:
            return await self._monitor.call(coro)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning(f"媒体清理转存工具 Telegram 操作失败: {type(exc).__name__}")
            raise HTTPException(status_code=400, detail=f"Telegram 操作失败：{type(exc).__name__}；请检查登录信息和网络连接") from exc

    async def save_schedule(self, change: ScheduleChange) -> dict:
        section = change.section
        if section not in SCHEDULE_FIELDS or set(change.settings) - SCHEDULE_FIELDS.get(section, set()):
            raise HTTPException(status_code=400, detail="不支持的工具配置字段")
        updated = {**self._schedule[section], **change.settings}
        updated["enabled"] = bool(updated.get("enabled"))
        if section == "tools":
            self._cleaner.resolve(str(updated.get("path") or self._strm_root))
            updated["path"] = str(updated.get("path") or self._strm_root).strip()
            updated["auto_delete"] = bool(updated.get("auto_delete"))
            updated["confirm_cleanup"] = bool(updated.get("confirm_cleanup"))
        if section == "p115_cleanup":
            identifiers = [self._validate_cid(value) for value in updated.get("dir_ids") or []]
            if len(identifiers) != len(set(identifiers)):
                raise HTTPException(status_code=400, detail="115 清理目录不能重复")
            names = [str(item or "").strip() for item in updated.get("dir_names") or []]
            updated["dir_ids"] = identifiers
            updated["dir_names"] = (names + [""] * len(identifiers))[:len(identifiers)]
            if updated["enabled"] and not identifiers:
                raise HTTPException(status_code=400, detail="请先填写清理目录")
        if section == "p115_move":
            rules = []
            for rule in updated.get("rules") or []:
                if not isinstance(rule, dict):
                    raise HTTPException(status_code=400, detail="转存规则格式不正确")
                source = self._validate_cid(rule.get("src_id"))
                target = self._validate_cid(rule.get("dst_id"))
                if source == target:
                    raise HTTPException(status_code=400, detail="源目录与目标目录不能相同")
                rules.append({"src_id": source, "src_name": str(rule.get("src_name") or "").strip(),
                              "dst_id": target, "dst_name": str(rule.get("dst_name") or "").strip()})
            updated["rules"] = rules
            try:
                updated["check_interval"] = max(60, int(updated.get("check_interval") or 120))
            except (TypeError, ValueError) as error:
                raise HTTPException(status_code=400, detail="监控间隔必须是数字") from error
            if updated["enabled"] and not rules:
                raise HTTPException(status_code=400, detail="请先填写转存规则")
        else:
            try:
                CronTrigger.from_crontab(str(updated.get("cron") or ""))
            except ValueError as error:
                raise HTTPException(status_code=400, detail=f"cron 表达式无效：{error}") from error
        if updated["enabled"] and section != "tools" and not self._source_cookie():
            raise HTTPException(status_code=400, detail="请先在 115 网盘 STRM 助手中配置 Cookie")
        if self._locks[section].locked():
            raise HTTPException(status_code=409, detail="任务执行中，暂不能更改配置")
        self._schedule[section] = updated
        self._persist()
        Scheduler().update_plugin_job(self.__class__.__name__)
        return {"saved": True, "message": "配置已保存，定时任务由 MoviePilot 执行"}

    def _p115_dirs(self, cid: str):
        with self._client() as client:
            return {"ok": True, "cid": cid, "dirs": client.directories(cid)}

    def _trash_info(self):
        with self._client() as client:
            info = client.rb_list()
        token = self._remember("trash", {"count": info["count"]})
        return {"ok": True, "count": info["count"],
                "size_bytes": sum(int(item.get("file_size") or 0) for item in info["items"]),
                "token": token}

    def _trash_clear(self, token: str):
        expected = self._consume(token, "trash")

        def clear():
            with self._client() as client:
                current = client.rb_list()
                if current["count"] != expected["count"]:
                    return {"ok": False, "message": "回收站内容已变化，请重新查询"}
                if not current["count"]:
                    return {"ok": True, "message": "回收站为空"}
                result = client.clear_recyclebin(self._rb_password)
                if not result.get("state"):
                    return {"ok": False, "message": str(result.get("error") or result.get("msg") or "清空失败")}
                return {"ok": True, "count": current["count"], "message": f"已彻底清空 {current['count']} 个文件"}

        return self._locked("p115_trash", clear)

    def _cleanup_preview(self):
        configured = self._schedule["p115_cleanup"]
        identifiers = configured.get("dir_ids") or []
        if not identifiers:
            return {"ok": False, "message": "请先保存 115 清理目录"}
        folders, snapshots = [], {}
        with self._client() as client:
            for index, cid in enumerate(identifiers):
                names = configured.get("dir_names") or []
                name = (names[index] if index < len(names) else "") or cid
                try:
                    files = client.list_files_in_dir(cid)
                    _, children = client.list_children(cid)
                    snapshots[cid] = {"files": sorted(file["fid"] for file in files),
                                      "dirs": sorted(item["cid"] for item in children)}
                    folders.append({"cid": cid, "name": name, "files": len(files), "dirs": len(children),
                                    "size": sum(file["size"] for file in files), "error": ""})
                except (RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
                    folders.append({"cid": cid, "name": name, "files": 0, "dirs": 0, "size": 0, "error": str(error)})
        return {"ok": True, "folders": folders, "file_count": sum(item["files"] for item in folders),
                "dir_count": sum(item["dirs"] for item in folders), "total_bytes": sum(item["size"] for item in folders),
                "snapshots": snapshots}

    def _cleanup_request(self):
        preview = self._cleanup_preview()
        if not preview.get("ok"):
            return preview
        if len(preview["snapshots"]) != len(self._schedule["p115_cleanup"]["dir_ids"]):
            return {"ok": False, "message": "部分目录读取失败，已停止清理"}
        if not preview["file_count"] and not preview["dir_count"]:
            return {"ok": True, "empty": True, "message": "清理目录均为空"}
        snapshot = preview.pop("snapshots")
        token = self._remember("cleanup", snapshot)
        return {"ok": True, "pending": True, "token": token,
                "message": f"确认将 {preview['file_count']} 个文件及 {preview['dir_count']} 个文件夹移入回收站？"}

    def _cleanup_confirm(self, token: str):
        snapshot = self._consume(token, "cleanup")

        def clean():
            if set(snapshot) != set(self._schedule["p115_cleanup"]["dir_ids"]):
                return {"ok": False, "message": "清理目录配置已变化，请重新预览"}
            deleted, deleted_dirs, errors = 0, 0, []
            with self._client() as client:
                for cid, expected in snapshot.items():
                    directory_failed = False
                    try:
                        files = client.list_files_in_dir(cid)
                        _, children = client.list_children(cid)
                        if (sorted(item["fid"] for item in files) != expected["files"] or
                                sorted(item["cid"] for item in children) != expected["dirs"]):
                            errors.append(f"目录 {cid} 内容已变化，跳过")
                            continue
                        identifiers = [item["fid"] for item in files]
                        directories = [item["cid"] for item in children]
                        for batch in (identifiers[index:index + 50] for index in range(0, len(identifiers), 50)):
                            response = client.delete_files(batch)
                            if response.get("state"):
                                deleted += len(batch)
                            else:
                                directory_failed = True
                                errors.append(f"目录 {cid} 文件删除失败：{response.get('msg') or response.get('error')}")
                        if directory_failed:
                            continue
                        for batch in (directories[index:index + 50] for index in range(0, len(directories), 50)):
                            response = client.delete_files(batch)
                            if response.get("state"):
                                deleted_dirs += len(batch)
                            else:
                                errors.append(f"目录 {cid} 文件夹删除失败：{response.get('msg') or response.get('error')}")
                    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
                        errors.append(f"目录 {cid} 失败：{error}")
            return {"ok": not errors, "deleted": deleted, "dir_count": deleted_dirs, "errors": errors,
                    "message": f"清理结束：文件 {deleted} 个，文件夹 {deleted_dirs} 个"}

        return self._locked("p115_cleanup", clean)

    def _move_info(self):
        rules = self._schedule["p115_move"]["rules"]
        pending = {}
        if rules:
            with self._client() as client:
                for rule in rules:
                    try:
                        files, folders = client.list_children(rule["src_id"])
                        pending[rule["src_id"]] = {"name": rule["src_name"] or rule["src_id"],
                                                    "count": len(files) + len(folders)}
                    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
                        pending[rule["src_id"]] = {"name": rule["src_name"] or rule["src_id"],
                                                    "count": -1, "error": str(error)}
        return {"ok": True, "pending": pending, "rules": rules, "last_run": self._last_run.get("p115_move", "")}

    def _move_run(self):
        rules = self._schedule["p115_move"]["rules"]
        if not rules:
            return {"ok": False, "message": "请先保存转存规则"}

        def move():
            moved, errors = 0, []
            with self._client() as client:
                for rule in rules:
                    source, destination = rule["src_id"], rule["dst_id"]
                    try:
                        files, directories = client.list_children(source)
                        identifiers = [item["fid"] for item in files] + [item["cid"] for item in directories]
                        if not identifiers:
                            continue
                        for index in range(0, len(identifiers), 500):
                            batch = identifiers[index:index + 500]
                            result = client.move_files(batch, destination)
                            if result.get("state"):
                                moved += len(batch)
                            else:
                                errors.append(f"{source} → {destination}：{result.get('error') or result.get('msg')}")
                                break
                    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
                        errors.append(f"{source} → {destination}：{error}")
            self._last_run["p115_move"] = datetime.now().isoformat()
            return {"ok": not errors, "moved": moved, "errors": errors,
                    "message": f"转存完成：移动 {moved} 项，失败 {len(errors)} 条"}

        return self._locked("p115_move", move)

    def _run_scheduled(self, section: str) -> None:
        if not self._enabled or not self._schedule.get(section, {}).get("enabled"):
            return
        try:
            if section == "tools":
                config = self._schedule[section]
                snapshot = self._cleaner.scan(config["path"])
                count = len(snapshot["items"])
                if count and config["auto_delete"] and not config["confirm_cleanup"]:
                    result = self._locked("tools", lambda: self._cleaner.quarantine(snapshot, [item["path"] for item in snapshot["items"]]))
                    text = f"已隔离 {len(result.get('deleted', []))} 项，跳过 {len(result.get('failed', []))} 项"
                elif count and config["auto_delete"] and config["confirm_cleanup"]:
                    token = self._remember("scheduled_tools", snapshot)
                    text = f"发现 {count} 项待确认，请在媒体清理转存工具打开待办并确认（有效期 30 分钟，令牌 {token[:8]}）"
                else:
                    text = f"扫描完成，发现 {count} 项；自动清理未开启"
            elif section == "p115_cleanup":
                preview = self._cleanup_preview()
                if preview.get("file_count") or preview.get("dir_count"):
                    snapshot = preview.pop("snapshots")
                    result = self._cleanup_confirm(self._remember("cleanup", snapshot))
                    text = result["message"]
                else:
                    text = "没有待清理文件"
            elif section == "p115_trash":
                info = self._trash_info()
                result = self._trash_clear(info["token"])
                text = result["message"]
            else:
                result = self._move_run()
                text = result["message"]
            self._last_run[section] = datetime.now().isoformat()
            if text and not text.startswith(("没有", "扫描完成，发现 0", "回收站为空")):
                self.post_message(title="媒体清理转存工具定时任务", text=f"{section}：{text}")
        except Exception as error:
            logger.error(f"媒体清理转存工具 {section} 定时任务失败：{error}")

    def _pending_info(self):
        with self._pending_lock:
            now = time.monotonic()
            return {"ok": True, "pending": [{"token": token, "kind": kind, "count": len(snapshot.get("items", []))}
                                           for token, (created, kind, snapshot) in self._pending.items()
                                           if kind == "scheduled_tools" and now - created < 1800]}

    def _confirm_scheduled_tools(self, token: str):
        snapshot = self._consume(token, "scheduled_tools")
        return self._locked("tools", lambda: self._cleaner.quarantine(snapshot, [item["path"] for item in snapshot["items"]]))

    async def action(self, action: ToolAction) -> dict:
        operation = action.operation
        try:
            if operation == "scan":
                return await asyncio.to_thread(self._cleaner.start_scan, action.path)
            if operation == "delete":
                if not action.scan_token or not action.paths:
                    raise HTTPException(status_code=400, detail="请先扫描并选择清理项目")
                return await asyncio.to_thread(self._locked, "tools", lambda: self._cleaner.delete(action.scan_token, action.paths))
            if operation == "request_delete":
                if not action.scan_token or not action.paths:
                    raise HTTPException(status_code=400, detail="请先扫描并选择清理项目")
                token = self._remember("manual_tools", {"scan_token": action.scan_token, "paths": action.paths})
                return {"ok": True, "token": token, "pending": True, "message": "请在本页面再次确认隔离"}
            if operation == "confirm_delete":
                saved = self._consume(action.token, "manual_tools")
                return await asyncio.to_thread(self._locked, "tools", lambda: self._cleaner.delete(saved["scan_token"], saved["paths"]))
            if operation == "pending":
                return self._pending_info()
            if operation == "confirm_scheduled":
                return await asyncio.to_thread(self._confirm_scheduled_tools, action.token)
            if operation == "dirs":
                root = self._cleaner.resolve(action.path)
                directories = sorted(name for name in os.listdir(root)
                                     if not name.startswith(".") and os.path.isdir(os.path.join(root, name))
                                     and not os.path.islink(os.path.join(root, name)))
                return {"ok": True, "path": root, "dirs": directories}
            if operation == "root_dirs":
                root = os.path.abspath(action.path or self._strm_root)
                if not os.path.isabs(action.path or self._strm_root) or os.path.realpath(root) != root or not os.path.isdir(root):
                    raise ValueError("请选择 MoviePilot 容器内的真实目录")
                directories = sorted(name for name in os.listdir(root)
                                     if not name.startswith(".") and os.path.isdir(os.path.join(root, name))
                                     and not os.path.islink(os.path.join(root, name)))
                return {"ok": True, "path": root, "dirs": directories}
            if operation == "p115_dirs":
                return await asyncio.to_thread(self._p115_dirs, action.cid)
            if operation == "trash_info":
                return await asyncio.to_thread(self._trash_info)
            if operation == "trash_clear":
                return await asyncio.to_thread(self._trash_clear, action.token)
            if operation == "cleanup_preview":
                preview = await asyncio.to_thread(self._cleanup_preview)
                preview.pop("snapshots", None)
                return preview
            if operation == "cleanup_request":
                return await asyncio.to_thread(self._cleanup_request)
            if operation == "cleanup_confirm":
                return await asyncio.to_thread(self._cleanup_confirm, action.token)
            if operation == "move_info":
                return await asyncio.to_thread(self._move_info)
            if operation == "move_run":
                return await asyncio.to_thread(self._move_run)
            raise HTTPException(status_code=400, detail="未知的工具操作")
        except (OSError, RuntimeError, ValueError, httpx.HTTPError) as error:
            return {"ok": False, "message": str(error)}

    def get_api(self) -> List[dict]:
        return [
            {"path": "/status", "endpoint": self.status, "methods": ["GET"], "auth": "bear", "summary": "MP 工具配置"},
            {"path": "/settings", "endpoint": self.save_settings, "methods": ["POST"], "auth": "bear", "summary": "保存插件连接"},
            {"path": "/schedule", "endpoint": self.save_schedule, "methods": ["POST"], "auth": "bear", "summary": "保存插件任务"},
            {"path": "/action", "endpoint": self.action, "methods": ["POST"], "auth": "bear", "summary": "执行插件工具"},
            {"path": "/monitor/status", "endpoint": self.monitor_status, "methods": ["GET"], "auth": "bear", "summary": "Telegram 监控状态"},
            {"path": "/monitor/action", "endpoint": self.monitor_action, "methods": ["POST"], "auth": "bear", "summary": "Telegram 监控操作"},
        ]
