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
from app.sdk.events import eventmanager, Event
from app.scheduler import Scheduler
from app.sdk.logging import logger
from app.schemas.message import Message
from app.schemas.types import EventType, MessageType, NotificationChannel
from app.schemas.types import SystemConfigKey
from app.schemas.system import NotificationConf
from app.application.service import get_service_configs
from app.application.messaging.channel.admin import matches_channel_admin

from .invalid_data import InvalidDataCleaner
from .p115 import P115Client
from .subscription_monitor import SubscriptionMonitor, normalize_channel
from .missing_episodes import DEFAULT_MISSING, MissingAction, MissingEpisodeDetector
from .media_cleanup import MediaCleanup, DEFAULT_CONFIG as DEFAULT_MEDIA_CLEANUP, DEFAULT_RULES, validate_rules
from . import tool_notifications as notices

ICON_URL = "https://raw.githubusercontent.com/yaoyaole0112/mpplugins.v3/main/plugins.v3/emetools/icon.jpeg"
SECTION_NAMES = {"tools": "清理数据", "p115_cleanup": "清理文件",
                 "p115_trash": "清空回收站", "p115_move": "文件转存"}


def _log_label(value: Any) -> str:
    """Bound untrusted file/folder names before using them in one-line logs."""
    text = re.sub(r"[\r\n\x00-\x1f]", " ", str(value or ""))
    text = re.sub(r"https?://\S+", "[链接已隐藏]", text, flags=re.I)
    text = re.sub(r"(?i)(?:cookie|token|password|secret|session|api[_-]?hash)\s*[:=]\s*\S+",
                  "[凭据已隐藏]", text)
    return text[:100]


DEFAULT_SCHEDULE = {
    "tools": {"enabled": False, "cron": "0 3 * * *", "path": "/strm", "auto_delete": True,
              "confirm_cleanup": False, "confirm_mode": "none"},
    "p115_cleanup": {"enabled": False, "cron": "0 */2 * * *", "dir_ids": [], "dir_names": []},
    "p115_trash": {"enabled": False, "cron": "0 3 * * *"},
    "p115_move": {"enabled": False, "check_interval": 120, "rules": []},
}
SCHEDULE_FIELDS = {
    "tools": {"enabled", "cron", "path", "auto_delete", "confirm_cleanup", "confirm_mode"},
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
    plugin_name = "增强工具"
    plugin_desc = "订阅频道监控、缺集检测、媒体清理、无效数据清理、115 文件清理、回收站清空与文件转存。"
    plugin_icon = ICON_URL
    plugin_version = "2.8.13"
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
        saved_missing = config.get("missing")
        if not isinstance(saved_missing, dict):
            legacy = self.get_config("EpisodeMissingSubscribe") or {}
            saved_missing = {key: legacy[key] for key in DEFAULT_MISSING if key in legacy}
            # Do not migrate the old task's enabled state: both plugins may still be active.
            saved_missing["enabled"] = False
            if self.get_data("missing_episodes") is None:
                old_results = self.get_data("missing_episodes", plugin_id="EpisodeMissingSubscribe")
                if isinstance(old_results, list):
                    self.save_data("missing_episodes", old_results)
                    old_time = self.get_data("last_scan_time", plugin_id="EpisodeMissingSubscribe")
                    if old_time:
                        self.save_data("last_scan_time", old_time)
        self._missing_config = copy.deepcopy(DEFAULT_MISSING)
        self._missing_config.update({key: value for key, value in saved_missing.items() if key in DEFAULT_MISSING})
        self._missing = MissingEpisodeDetector(self, self._missing_config)
        saved_media = config.get("media_cleanup") or {}
        self._media_config = copy.deepcopy(DEFAULT_MEDIA_CLEANUP)
        if isinstance(saved_media, dict):
            for key in self._media_config:
                if key in saved_media:
                    self._media_config[key] = copy.deepcopy(saved_media[key])
        # Manual scan scope is stored separately from scheduled-cleanup scope.
        saved_scan_ids = config.get("media_scan_library_ids", [])
        self._media_scan_library_ids = (list(saved_scan_ids) if isinstance(saved_scan_ids, list)
                                        and len(saved_scan_ids) <= 200
                                        and all(isinstance(value, str) for value in saved_scan_ids) else [])
        try:
            self._media_config["rules"] = validate_rules(self._media_config["rules"])
        except ValueError:
            self._media_config["rules"] = copy.deepcopy(DEFAULT_RULES)
        self._media = MediaCleanup(self, self._media_config)
        self._schedule = copy.deepcopy(DEFAULT_SCHEDULE)
        for name, defaults in self._schedule.items():
            saved = (config.get("schedule") or {}).get(name) or {}
            defaults.update({key: value for key, value in saved.items() if key in SCHEDULE_FIELDS[name]})
            if name == "tools" and "confirm_mode" not in saved:
                defaults["confirm_mode"] = "moviepilot" if defaults["confirm_cleanup"] else "none"
        self._cleaner = InvalidDataCleaner(self._strm_root)
        self._locks = {name: threading.Lock() for name in self._schedule}
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._last_run = {}
        if "cookie" in config or "proxy" in config:
            self._persist()
        logger.info("ME工具：初始化完成，启用=%s，STRM=%s，任务=%s，订阅/关键词监控=%s/%s",
                    self._enabled, _log_label(self._strm_root),
                    ", ".join(SECTION_NAMES[key] for key, value in self._schedule.items() if value["enabled"]) or "无",
                    self._monitor_config["sub"]["enabled"], self._monitor_config["kw"]["enabled"])
        if self._enabled and self._tg_session and any(item["enabled"] for item in self._monitor_config.values()):
            logger.info("ME工具：尝试恢复 Telegram 监控（不会输出账号和登录凭据）")
            self._monitor._ensure_loop()
            asyncio.run_coroutine_threadsafe(self._monitor.resume(), self._monitor.loop)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[dict]:
        return [{"cmd": command, "event": EventType.PluginAction, "desc": description,
                 "category": "工具", "data": {"action": f"emetools_{action}"}}
                for command, action, description in (
                    ("/cleanup", "cleanup", "扫描无效数据（在插件页面确认清理）"),
                    ("/cleanfiles", "cleanfiles", "预览 115 清理目录（在插件页面确认）"),
                    ("/cleartrash", "cleartrash", "查看 115 回收站（在插件页面确认清空）"),
                    ("/cleandupes", "cleandupes", "扫描并确认清理低质版本"),
                    ("/ememove", "move", "执行已保存的 115 文件转存规则"))]

    @eventmanager.register(EventType.PluginAction)
    def tool_command(self, event: Event) -> None:
        data = event.event_data if event else None
        action = (data or {}).get("action", "")
        if not self._enabled or action not in {"emetools_cleanup", "emetools_cleanfiles", "emetools_cleartrash", "emetools_move", "emetools_cleandupes"}:
            return
        # Command replies must have a real destination; never turn them into broadcasts.
        if not data.get("user") or not data.get("channel") or not data.get("source"):
            logger.warning("ME工具 Bot 命令缺少用户、渠道或来源，已拒绝回复以避免广播")
            return
        threading.Thread(target=self._run_tool_command, args=(action, data.copy()), daemon=True).start()

    def _run_tool_command(self, action: str, data: dict) -> None:
        try:
            logger.info("ME工具 Bot 命令开始：%s，来源=%s", action, _log_label(data["source"]))
            if action == "emetools_cleanup":
                result = self._cleaner.start_scan(self._strm_root)
                text = f"发现 {result['count']} 项无效数据。请在插件「清理无效数据」页面重新扫描并确认隔离。" if result["count"] else "未发现无效数据。"
            elif action == "emetools_cleanfiles":
                result = self._cleanup_preview()
                text = (f"待清理 {result['file_count']} 个文件、{result['dir_count']} 个文件夹。请在插件「清理文件」页面预览并二次确认。"
                        if result.get("ok") else result.get("message", "预览失败"))
            elif action == "emetools_cleartrash":
                result = self._trash_info()
                text = f"回收站有 {result['count']} 项；请在插件「清空回收站」页面重新查询并二次确认。" if result["count"] else "回收站为空。"
            elif action == "emetools_cleandupes":
                self._request_media_bot_cleanup(data)
                return
            else:
                result = self._move_run()
                text = result.get("message") or f"文件转存完成：移动 {result.get('moved', 0)} 项，失败 {len(result.get('errors') or [])} 项。"
                notice = notices.file_move(result) if "moved" in result else None
                if notice:
                    # A command is a private reply to its originating bot, not a scheduled
                    # notification to every bot subscribed to the Plugin message type.
                    text = f"{notice[0]}\n{notice[1]}"
            self.chain.post_message(Message(channel=data["channel"], source=data["source"],
                                            userid=str(data["user"]), title="ME工具", text=text))
            logger.info("ME工具 Bot 命令完成：%s", action)
        except Exception as exc:
            logger.warning("ME工具命令 %s 执行失败: %s", action, type(exc).__name__)
            self.chain.post_message(Message(channel=data["channel"], source=data["source"], userid=str(data["user"]),
                                            title="ME工具", text=f"命令执行失败：{type(exc).__name__}，请查看插件日志。"))

    @staticmethod
    def _telegram_command_sources() -> dict:
        """Resolve only the originating enabled Telegram Bot; never broadcast a command."""
        return {conf.name: conf.config for conf in get_service_configs(SystemConfigKey.Notifications, NotificationConf)
                if conf.enabled and str(conf.type).lower() == "telegram" and conf.name and conf.config}

    def _media_bot_reply(self, source: str, user: str, title: str, text: str, buttons=None) -> None:
        self.chain.post_message(Message(channel=NotificationChannel.Telegram, source=source, userid=user,
                                        title=title, text=text, buttons=buttons, parse_mode="plain"))

    def _request_media_bot_cleanup(self, data: dict) -> None:
        source, user = str(data["source"]), str(data["user"])
        if data["channel"] not in {NotificationChannel.Telegram, NotificationChannel.Telegram.value}:
            return
        config = self._telegram_command_sources().get(source)
        if not config or not matches_channel_admin(NotificationChannel.Telegram, config, user):
            self._media_bot_reply(source, user, "ME工具", "❌ 当前 Telegram 用户没有清理权限。")
            return
        self._media_bot_reply(source, user, "ME工具", "🔄 正在扫描低质版本，可能需要几分钟…")
        try:
            result = self._media.scan(self._media_config["library_ids"])
            versions = [version for group in result["results"] for version in group["versions"]
                        if not version["is_best"]]
            if not versions:
                self._media_bot_reply(source, user, "ME工具", "✅ 未发现需删除的低质版本。")
                return
            if len(versions) > 1000:
                self._media_bot_reply(source, user, "ME工具", "❌ 待删除版本超过 1000 个，请在插件页面缩小媒体库范围后再执行。")
                return
            token = self._remember("media_bot", {"source": source, "user": user,
                                                  "paths": [v["file_path"] for v in versions],
                                                  "scan": self._media.last_scan})
            title, text = notices.media_confirmation(versions)
            buttons = [[{"text": "✅ 确认清理", "callback_data": f"[PLUGIN]EmeTools|media:{token}:y"},
                        {"text": "取消", "callback_data": f"[PLUGIN]EmeTools|media:{token}:n"}]]
            self._media_bot_reply(source, user, title, text, buttons)
            logger.info("增强工具 媒体清理命令：已向发起 Bot 的管理员提交 %d 个候选确认", len(versions))
        except Exception as error:
            logger.warning("增强工具 媒体清理命令扫描失败：%s", type(error).__name__)
            self._media_bot_reply(source, user, "ME工具", f"❌ 扫描或发送确认失败：{_log_label(error)}")

    def _handle_media_bot_confirmation(self, token: str, approve: bool, data: dict) -> None:
        source, user = str(data.get("source") or ""), str(data.get("userid") or "")
        if (data.get("channel") not in {NotificationChannel.Telegram, NotificationChannel.Telegram.value}
                or not source or not user or str(data.get("original_chat_id") or "") != user
                or not matches_channel_admin(NotificationChannel.Telegram,
                                             self._telegram_command_sources().get(source), user)):
            logger.warning("增强工具 媒体清理：Telegram 确认被拒绝（需要原 Bot 管理员私聊）")
            return
        try:
            # Validate ownership before consuming: another administrator must not
            # invalidate the originating bot's confirmation by pressing its button.
            with self._pending_lock:
                pending = self._pending.get(token)
                if (not pending or pending[1] != "media_bot" or time.monotonic() - pending[0] >= 1800
                        or pending[2]["source"] != source or pending[2]["user"] != user):
                    raise ValueError("确认已过期或发起的 Bot、用户不匹配")
                record = self._pending.pop(token)[2]
            if not approve:
                text = "已取消本次清理。"
            else:
                if record["scan"] != self._media.last_scan:
                    raise ValueError("扫描结果已更新，请重新发送命令")
                result = self._media.delete(record["paths"])
                if result["deleted"]:
                    self._media.refresh_emby()
                notice = notices.media_cleanup(result)
                text = f"{notice[0]}\n{notice[1]}" if notice else "✅ 未发现需删除的低质版本。"
            self._media_bot_reply(source, user, "ME工具", text)
        except Exception as error:
            logger.warning("增强工具 媒体清理：Telegram 确认失败：%s", type(error).__name__)
            self._media_bot_reply(source, user, "ME工具", f"❌ 确认已过期、已处理或执行失败：{_log_label(error)}")

    def stop_service(self) -> None:
        logger.info("ME工具：停止插件服务和 Telegram 监控")
        monitor = getattr(self, "_monitor", None)
        if monitor and monitor.loop and monitor.thread and monitor.thread.is_alive():
            try:
                asyncio.run_coroutine_threadsafe(monitor.shutdown(), monitor.loop).result(timeout=8)
            except Exception as exc:
                logger.warning(f"ME工具 Telegram 客户端关闭失败: {type(exc).__name__}")
            monitor.loop.call_soon_threadsafe(monitor.loop.stop)
            monitor.thread.join(timeout=3)

    @staticmethod
    def get_render_mode() -> Tuple[str, str]:
        return "vue", "dist/assets"

    def get_sidebar_nav(self) -> List[dict]:
        if not self._enabled or not self._show_sidebar_nav:
            return []
        return [{"nav_key": "main", "title": "增强工具", "icon": "mdi-creation-outline",
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
                    logger.error(f"ME工具 {name} 定时表达式无效：{error}")
                    continue
                services.append({"id": f"EmeTools_{name}", "name": f"ME工具 {name}",
                                 "trigger": trigger, "func": self._run_scheduled,
                                 "kwargs": {}, "func_kwargs": {"section": name}})
        move = self._schedule["p115_move"]
        if move.get("enabled") and move.get("rules"):
            services.append({"id": "EmeTools_p115_move", "name": "ME工具文件转存",
                             "trigger": "interval", "func": self._run_scheduled,
                             "kwargs": {"seconds": max(60, int(move.get("check_interval") or 120))},
                             "func_kwargs": {"section": "p115_move"}})
        if self._missing_config["enabled"]:
            if self._legacy_missing_active():
                logger.warning("ME工具 缺集检测：旧剧集缺集检测订阅插件仍启用，已跳过新定时任务")
            else:
                try:
                    trigger = CronTrigger.from_crontab(self._missing_config["cron"])
                    services.append({"id": "EmeTools_missing", "name": "ME工具 缺集检测",
                                     "trigger": trigger, "func": self._run_missing_scheduled,
                                     "kwargs": {}})
                except ValueError as error:
                    logger.error("ME工具 缺集检测定时表达式无效：%s", error)
        if self._media_config["enabled"]:
            try:
                trigger = CronTrigger.from_crontab(self._media_config["cron"])
                services.append({"id": "EmeTools_media_cleanup", "name": "增强工具 媒体清理",
                                 "trigger": trigger, "func": self._run_media_scheduled, "kwargs": {}})
            except ValueError as error:
                logger.error("增强工具 媒体清理周期无效：%s", error)
        return services

    def _run_media_scheduled(self) -> None:
        if not self._enabled or not self._media_config["enabled"]:
            return
        try:
            result = self._media.scan(self._media_config["library_ids"])
            paths = [version["file_path"] for group in result["results"]
                     for version in group["versions"] if not version["is_best"]]
            logger.info("增强工具 媒体清理定时扫描：重复组=%d，待清理=%d", result["duplicate_groups"], len(paths))
            if paths:
                outcome = {"deleted": [], "failures": []}
                for start in range(0, len(paths), 1000):
                    batch = self._media.delete(paths[start:start + 1000])
                    outcome["deleted"].extend(batch["deleted"])
                    outcome["failures"].extend(batch["failures"])
                if outcome["deleted"]:
                    self._media.refresh_emby()
                notice = notices.media_cleanup(outcome)
                if notice:
                    self._send_tool_notice(*notice)
                logger.info("增强工具 媒体清理定时任务：已清理=%d，失败=%d",
                            len(outcome["deleted"]), len(outcome["failures"]))
        except Exception as error:
            logger.warning("增强工具 媒体清理定时任务失败：%s：%s", type(error).__name__, _log_label(error))
            if "STRM 根目录不存在" not in str(error):
                self._send_tool_notice("", f"⏰ 定时去重执行失败：{_log_label(error)}")

    def _legacy_missing_active(self) -> bool:
        legacy = self.get_config("EpisodeMissingSubscribe") or {}
        return isinstance(legacy, dict) and bool(legacy.get("enabled"))

    def _run_missing_scheduled(self) -> None:
        if not self._enabled or not self._missing_config["enabled"] or self._legacy_missing_active():
            logger.warning("ME工具 缺集检测：插件已关闭或原缺集插件仍启用，跳过本次检测")
            return
        self._missing.scan_missing_episodes()

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
                            "monitor": copy.deepcopy(self._monitor_config),
                            "missing": copy.deepcopy(self._missing_config),
                            "media_cleanup": copy.deepcopy(self._media_config),
                            "media_scan_library_ids": list(self._media_scan_library_ids)})

    async def media_status(self) -> dict:
        return {"config": copy.deepcopy(self._media_config),
                "scan_library_ids": list(self._media_scan_library_ids), "running": self._media.running,
                "progress": self._media.progress, "last_scan": self._media.last_scan,
                "last_error": self._media.last_error,
                "result": copy.deepcopy(self._media.result)}

    async def media_libraries(self) -> dict:
        return {"libraries": await asyncio.to_thread(self._media.libraries)}

    async def media_action(self, action: dict) -> dict:
        operation = action.get("operation")
        if operation == "save":
            if self._media.running or self._media.lock.locked():
                raise HTTPException(status_code=409, detail="清理任务进行中，请稍后保存")
            changes = action.get("config")
            if not isinstance(changes, dict) or set(changes) - set(DEFAULT_MEDIA_CLEANUP):
                raise HTTPException(status_code=400, detail="媒体清理配置不合法")
            updated = {**self._media_config, **changes}
            if not isinstance(updated["library_ids"], list) or len(updated["library_ids"]) > 200 or any(
                    not isinstance(value, str) for value in updated["library_ids"]):
                raise HTTPException(status_code=400, detail="媒体库选择不合法")
            try:
                updated["rules"] = validate_rules(updated["rules"])
                CronTrigger.from_crontab(str(updated["cron"]))
            except (ValueError, TypeError) as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            if updated["enabled"] and not self._enabled:
                raise HTTPException(status_code=409, detail="请先启用插件，再启用定时清理")
            updated["enabled"] = bool(updated["enabled"])
            self._media_config = updated
            self._media.config = updated
            self._media.result = None
            self._media._snapshot = {}
            self._persist()
            Scheduler().update_plugin_job(self.__class__.__name__)
            return {"message": "媒体清理配置已保存；定时删除仅在启用时运行"}
        if operation == "scan":
            if self._media.running or self._media.lock.locked():
                raise HTTPException(status_code=409, detail="媒体清理正在执行")
            selected = action.get("library_ids", self._media_scan_library_ids)
            if not isinstance(selected, list) or len(selected) > 200 or any(not isinstance(v, str) for v in selected):
                raise HTTPException(status_code=400, detail="媒体库选择不合法")
            self._media_scan_library_ids = list(selected)
            self._persist()
            def background_scan():
                try:
                    self._media.scan(selected)
                except Exception as error:
                    logger.warning("增强工具 媒体清理扫描失败：%s：%s",
                                   type(error).__name__, _log_label(error))
                    self._media.last_error = str(error)[:160]
            self._media.running = True
            threading.Thread(target=background_scan, daemon=True).start()
            return {"message": "已开始扫描，结果将在扫描完成后自动更新"}
        if operation == "preview_delete":
            selected = action.get("paths")
            if not isinstance(selected, list) or not selected or len(selected) > 1000 or any(not isinstance(path, str) for path in selected) or len(set(selected)) != len(selected) or any(
                    path not in self._media._snapshot or self._media._snapshot[path]["is_best"] for path in selected):
                raise HTTPException(status_code=400, detail="仅可选择本次扫描的低质版本")
            token = self._remember("media_delete", {"paths": selected, "scan": self._media.last_scan})
            return {"token": token, "count": len(selected)}
        if operation == "confirm_delete":
            record = self._consume(action.get("token", ""), "media_delete")
            if record["scan"] != self._media.last_scan:
                raise HTTPException(status_code=409, detail="扫描结果已更新，请重新确认")
            result = await asyncio.to_thread(self._media.delete, record["paths"])
            if result["deleted"]:
                await asyncio.to_thread(self._media.refresh_emby)
            notice = notices.media_cleanup(result)
            if notice:
                self._send_tool_notice(*notice)
            return result
        if operation == "reset_rules":
            return {"rules": copy.deepcopy(DEFAULT_RULES)}
        raise HTTPException(status_code=400, detail="未知媒体清理操作")

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

    async def missing_status(self) -> dict:
        self._missing._load_saved_data() if not self._missing._is_scanning else None
        return {"config": copy.deepcopy(self._missing_config),
                "legacy_enabled": self._legacy_missing_active(),
                "scanning": self._missing._is_scanning,
                "last_scan_time": self._missing._last_scan_time,
                "results": copy.deepcopy(self._missing._results)}

    async def missing_options(self) -> dict:
        servers, libraries, series = await asyncio.to_thread(self._missing._get_form_options)
        return {"servers": servers, "libraries": libraries, "series": series}

    async def missing_action(self, action: dict) -> dict:
        operation = action.get("operation")
        if operation == "save":
            changes = action.get("config")
            if not isinstance(changes, dict) or set(changes) - set(DEFAULT_MISSING):
                raise HTTPException(status_code=400, detail="缺集检测配置格式不正确")
            updated = {**self._missing_config, **changes}
            updated["enabled"] = bool(updated["enabled"])
            for key in ("only_existing_seasons", "ignore_season_zero", "ignore_future"):
                updated[key] = bool(updated[key])
            if updated["missing_action"] not in {item.value for item in MissingAction}:
                raise HTTPException(status_code=400, detail="缺集检测处理方式无效")
            for key in ("server_names", "library_names", "skip_series_ids"):
                if not isinstance(updated[key], list) or len(updated[key]) > 1000:
                    raise HTTPException(status_code=400, detail=f"{key} 应为多选列表")
                updated[key] = self._missing._parse_names(updated[key])
            if any(not name.isdecimal() for name in updated["skip_series_ids"]):
                raise HTTPException(status_code=400, detail="跳过剧集的 TMDB ID 必须是数字")
            try:
                CronTrigger.from_crontab(str(updated["cron"]))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"cron 表达式无效：{exc}") from exc
            if updated["enabled"] and self._legacy_missing_active():
                raise HTTPException(status_code=409, detail="请先停用原「剧集缺集检测订阅」插件，再启用 ME工具缺集定时检测")
            if self._missing._is_scanning:
                raise HTTPException(status_code=409, detail="扫描进行中，请稍后保存配置")
            newly_skipped = set(updated["skip_series_ids"]) - set(self._missing_config["skip_series_ids"])
            self._missing_config = updated
            self._missing.configure(updated)
            self._persist()
            Scheduler().update_plugin_job(self.__class__.__name__)
            if newly_skipped:
                # The old plugin cancels subscriptions for skipped series on save.
                # Never do this during automatic migration/initialization.
                original = self._missing._skip_series_ids
                try:
                    self._missing._skip_series_ids = newly_skipped
                    await asyncio.to_thread(self._missing._cancel_skipped_subscriptions)
                finally:
                    self._missing._skip_series_ids = original
            return {"message": "缺集检测配置已保存"}
        if operation == "scan":
            if self._missing._is_scanning:
                raise HTTPException(status_code=409, detail="缺集检测正在扫描")
            if self._legacy_missing_active() and self._missing_config["missing_action"] == MissingAction.ADD_SUBSCRIBE.value:
                raise HTTPException(status_code=409, detail="原缺集插件仍启用；请先停用它，避免两处同时添加订阅")
            threading.Thread(target=self._missing.scan_missing_episodes, daemon=True).start()
            return {"message": "已开始扫描，请稍后刷新检测结果"}
        if operation == "clear":
            if self._missing._is_scanning:
                raise HTTPException(status_code=409, detail="缺集检测正在扫描")
            self._missing._results = []
            self._missing._last_scan_time = "从未扫描"
            self.save_data("missing_episodes", [])
            self.save_data("last_scan_time", "从未扫描")
            return {"message": "检测记录已清空（原插件记录仍保留）"}
        raise HTTPException(status_code=400, detail="未知缺集检测操作")

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
        try:
            return await asyncio.wait_for(self._monitor.call(self._monitor.status()), timeout=12)
        except asyncio.TimeoutError:
            return {"configured": bool(self._tg_api_id and self._tg_api_hash), "logged_in": False,
                    "dependency_ready": True, "last_error": "Telegram 状态查询超时，请检查网络后重试",
                    "last_event": "", "listening_channels": {"sub": 0, "kw": 0},
                    "subscription_count": 0, "hits": [],
                    "sub": dict(self._monitor_config["sub"]), "kw": dict(self._monitor_config["kw"])}

    async def monitor_action(self, change: MonitorChange) -> dict:
        operation, scope = change.operation, change.scope
        logger.info("ME工具 Telegram：执行%s，监控=%s",
                    operation if operation in {"send_code", "sign_in", "logout", "save", "start", "stop"} else "未知操作",
                    scope if scope in ("sub", "kw") else "未知监控")
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
            logger.info("ME工具 Telegram：%s 配置已保存，频道=%d，关键词=%d，黑名单=%d",
                        scope, len(channels), len(keywords), len(blacklist))
            return {"ok": True}
        elif operation in ("start", "stop"):
            if operation == "start" and not self._enabled:
                raise HTTPException(status_code=400, detail="请先启用插件")
            coro = self._monitor.start(scope) if operation == "start" else self._monitor.stop(scope)
        else:
            raise HTTPException(status_code=400, detail="未知监控操作")
        try:
            result = await self._monitor.call(coro)
            logger.info("ME工具 Telegram：%s %s，结果=%s", scope, operation,
                        "成功" if result.get("ok") else "需要继续验证")
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning(f"ME工具 Telegram 操作失败: {type(exc).__name__}")
            raise HTTPException(status_code=400, detail=f"Telegram 操作失败：{type(exc).__name__}；请检查登录信息和网络连接") from exc

    async def save_schedule(self, change: ScheduleChange) -> dict:
        section = change.section
        if section not in SCHEDULE_FIELDS or set(change.settings) - SCHEDULE_FIELDS.get(section, set()):
            raise HTTPException(status_code=400, detail="不支持的工具配置字段")
        updated = {**self._schedule[section], **change.settings}
        if section == "tools" and "confirm_mode" not in change.settings and "confirm_cleanup" in change.settings:
            updated["confirm_mode"] = "moviepilot" if change.settings["confirm_cleanup"] else "none"
        updated["enabled"] = bool(updated.get("enabled"))
        if section == "tools":
            self._cleaner.resolve(str(updated.get("path") or self._strm_root))
            updated["path"] = str(updated.get("path") or self._strm_root).strip()
            updated["auto_delete"] = bool(updated.get("auto_delete"))
            mode = updated.get("confirm_mode")
            if mode not in {"none", "moviepilot", "telegram"}:
                raise HTTPException(status_code=400, detail="请选择无需确认、MoviePilot 页面确认或 Telegram 按钮确认")
            updated["confirm_cleanup"] = mode != "none"
            if mode == "telegram" and updated["enabled"] and updated["auto_delete"]:
                if not self._telegram_confirmation_sources():
                    raise HTTPException(status_code=400, detail="请先在 MoviePilot 通知渠道启用 Telegram Bot，并配置管理员和插件类型通知")
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
        logger.info("ME工具 %s：运行配置已保存，启用=%s，目录/规则=%d",
                    SECTION_NAMES[section], updated["enabled"],
                    len(updated.get("dir_ids") or updated.get("rules") or []))
        return {"saved": True, "message": "配置已保存，定时任务由 MoviePilot 执行"}

    def _p115_dirs(self, cid: str):
        with self._client() as client:
            return {"ok": True, "cid": cid, "dirs": client.directories(cid)}

    def _trash_info(self):
        logger.info("ME工具 清空115回收站：查询当前回收站")
        with self._client() as client:
            info = client.rb_list()
        logger.info("ME工具 清空115回收站：查询完成，文件=%d，大小=%d 字节",
                    info["count"], sum(int(item.get("file_size") or 0) for item in info["items"]))
        token = self._remember("trash", {"count": info["count"]})
        return {"ok": True, "count": info["count"],
                "size_bytes": sum(int(item.get("file_size") or 0) for item in info["items"]),
                "token": token}

    def _trash_clear(self, token: str):
        expected = self._consume(token, "trash")
        logger.info("ME工具 清空115回收站：开始二次核验，预计文件=%d", expected["count"])

        def clear():
            with self._client() as client:
                current = client.rb_list()
                if current["count"] != expected["count"]:
                    logger.warning("ME工具 清空115回收站：内容已变化，原=%d，现=%d，取消清空",
                                   expected["count"], current["count"])
                    return {"ok": False, "message": "回收站内容已变化，请重新查询"}
                if not current["count"]:
                    logger.info("ME工具 清空115回收站：回收站为空，跳过")
                    return {"ok": True, "message": "回收站为空"}
                result = client.clear_recyclebin(self._rb_password)
                if not result.get("state"):
                    logger.warning("ME工具 清空115回收站：请求失败，已保留文件（服务端错误未记录以保护凭据）")
                    return {"ok": False, "message": str(result.get("error") or result.get("msg") or "清空失败")}
                logger.info("ME工具 清空115回收站：成功清空 %d 个文件", current["count"])
                return {"ok": True, "count": current["count"],
                        "size_bytes": sum(int(item.get("file_size") or 0) for item in current["items"]),
                        "message": f"已彻底清空 {current['count']} 个文件"}

        return self._locked("p115_trash", clear)

    def _cleanup_preview(self):
        configured = self._schedule["p115_cleanup"]
        identifiers = configured.get("dir_ids") or []
        if not identifiers:
            logger.info("ME工具 清理文件：没有配置目录，跳过预览")
            return {"ok": False, "message": "请先保存 115 清理目录"}
        logger.info("ME工具 清理文件：开始预览 %d 个目录", len(identifiers))
        folders, snapshots = [], {}
        with self._client() as client:
            for index, cid in enumerate(identifiers):
                names = configured.get("dir_names") or []
                name = (names[index] if index < len(names) else "") or cid
                try:
                    files = client.list_files_in_dir(cid)
                    _, children = client.list_children(cid)
                    snapshots[cid] = {"files": sorted(file["fid"] for file in files),
                                      "dirs": sorted(item["cid"] for item in children), "name": name}
                    folders.append({"cid": cid, "name": name, "files": len(files), "dirs": len(children),
                                    "size": sum(file["size"] for file in files), "error": ""})
                    logger.info("ME工具 清理文件：目录 %s（CID %s），文件=%d，子目录=%d",
                                _log_label(name), cid, len(files), len(children))
                except (RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
                    folders.append({"cid": cid, "name": name, "files": 0, "dirs": 0, "size": 0, "error": str(error)})
                    logger.warning("ME工具 清理文件：目录 %s（CID %s）预览失败：%s",
                                   _log_label(name), cid, type(error).__name__)
        logger.info("ME工具 清理文件：预览完成，文件=%d，子目录=%d，失败目录=%d",
                    sum(item["files"] for item in folders), sum(item["dirs"] for item in folders),
                    sum(bool(item["error"]) for item in folders))
        return {"ok": True, "folders": folders, "file_count": sum(item["files"] for item in folders),
                "dir_count": sum(item["dirs"] for item in folders), "total_bytes": sum(item["size"] for item in folders),
                "snapshots": snapshots}

    def _cleanup_request(self):
        preview = self._cleanup_preview()
        if not preview.get("ok"):
            return preview
        if len(preview["snapshots"]) != len(self._schedule["p115_cleanup"]["dir_ids"]):
            logger.warning("ME工具 清理文件：部分目录预览失败，停止确认流程")
            return {"ok": False, "message": "部分目录读取失败，已停止清理"}
        if not preview["file_count"] and not preview["dir_count"]:
            logger.info("ME工具 清理文件：目录均为空，无需确认")
            return {"ok": True, "empty": True, "message": "清理目录均为空"}
        snapshot = preview.pop("snapshots")
        token = self._remember("cleanup", snapshot)
        logger.info("ME工具 清理文件：已生成二次确认，文件=%d，子目录=%d",
                    preview["file_count"], preview["dir_count"])
        self._send_tool_notice(*notices.file_cleanup_confirmation(preview))
        return {"ok": True, "pending": True, "token": token,
                "message": f"确认将 {preview['file_count']} 个文件及 {preview['dir_count']} 个文件夹移入回收站？"}

    def _cleanup_confirm(self, token: str):
        snapshot = self._consume(token, "cleanup")
        logger.info("ME工具 清理文件：开始复核并清理 %d 个目录", len(snapshot))

        def clean():
            if set(snapshot) != set(self._schedule["p115_cleanup"]["dir_ids"]):
                logger.warning("ME工具 清理文件：目录配置已变化，终止清理")
                return {"ok": False, "message": "清理目录配置已变化，请重新预览"}
            deleted, deleted_dirs, errors, folders = 0, 0, [], []
            with self._client() as client:
                for cid, expected in snapshot.items():
                    directory_failed = False
                    detail = {"name": expected.get("name") or cid, "files": 0, "dirs": 0,
                              "size": 0, "error": ""}
                    folders.append(detail)
                    try:
                        files = client.list_files_in_dir(cid)
                        _, children = client.list_children(cid)
                        if (sorted(item["fid"] for item in files) != expected["files"] or
                                sorted(item["cid"] for item in children) != expected["dirs"]):
                            detail["error"] = "内容已变化，跳过"
                            errors.append(f"目录 {cid} {detail['error']}")
                            logger.warning("ME工具 清理文件：目录 %s 内容变化，跳过", _log_label(detail["name"]))
                            continue
                        identifiers = [item["fid"] for item in files]
                        directories = [item["cid"] for item in children]
                        for batch in (identifiers[index:index + 50] for index in range(0, len(identifiers), 50)):
                            response = client.delete_files(batch)
                            if response.get("state"):
                                deleted += len(batch)
                                detail["files"] += len(batch)
                                detail["size"] += sum(int(item.get("size") or 0) for item in files
                                                      if item["fid"] in batch)
                            else:
                                directory_failed = True
                                detail["error"] = f"文件删除失败：{response.get('msg') or response.get('error')}"
                                errors.append(f"目录 {cid} {detail['error']}")
                        if directory_failed:
                            continue
                        for batch in (directories[index:index + 50] for index in range(0, len(directories), 50)):
                            response = client.delete_files(batch)
                            if response.get("state"):
                                deleted_dirs += len(batch)
                                detail["dirs"] += len(batch)
                            else:
                                detail["error"] = f"文件夹删除失败：{response.get('msg') or response.get('error')}"
                                errors.append(f"目录 {cid} {detail['error']}")
                    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
                        detail["error"] = f"失败：{error}"
                        errors.append(f"目录 {cid} {detail['error']}")
                    logger.info("ME工具 清理文件：目录 %s 完成，文件=%d，子目录=%d，结果=%s",
                                _log_label(detail["name"]), detail["files"], detail["dirs"],
                                "失败" if detail["error"] else "成功")
            logger.info("ME工具 清理文件：执行完毕，文件=%d，子目录=%d，失败=%d",
                        deleted, deleted_dirs, len(errors))
            return {"ok": not errors, "deleted": deleted, "dir_count": deleted_dirs,
                    "errors": errors, "folders": folders,
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
        logger.info("ME工具 文件转存：待转存查询完成，规则=%d，待转存=%d，查询失败=%d",
                    len(rules), sum(max(0, item["count"]) for item in pending.values()),
                    sum(item["count"] < 0 for item in pending.values()))
        return {"ok": True, "pending": pending, "rules": rules, "last_run": self._last_run.get("p115_move", "")}

    def _move_run(self):
        rules = self._schedule["p115_move"]["rules"]
        if not rules:
            logger.info("ME工具 文件转存：未配置规则，跳过")
            return {"ok": False, "message": "请先保存转存规则"}
        logger.info("ME工具 文件转存：开始检查 %d 条转存规则", len(rules))

        def move():
            moved, errors, details = 0, [], []
            with self._client() as client:
                for rule in rules:
                    source, destination = rule["src_id"], rule["dst_id"]
                    detail = {"src_id": source, "dst_id": destination,
                              "src_name": rule.get("src_name"), "dst_name": rule.get("dst_name"),
                              "status": "empty", "file_count": 0, "total_bytes": 0}
                    details.append(detail)
                    try:
                        files, directories = client.list_children(source)
                        identifiers = [item["fid"] for item in files] + [item["cid"] for item in directories]
                        logger.info("ME工具 文件转存：%s → %s，源文件=%d，源目录=%d",
                                    _log_label(detail["src_name"] or source),
                                    _log_label(detail["dst_name"] or destination), len(files), len(directories))
                        if not identifiers:
                            logger.info("ME工具 文件转存：%s 源目录为空，跳过",
                                        _log_label(detail["src_name"] or source))
                            continue
                        moved_ids = set()
                        for index in range(0, len(identifiers), 500):
                            batch = identifiers[index:index + 500]
                            result = client.move_files(batch, destination)
                            if result.get("state"):
                                moved += len(batch)
                                moved_ids.update(batch)
                            else:
                                errors.append(f"{source} → {destination}：{result.get('error') or result.get('msg')}")
                                detail["status"] = "failed"
                                logger.warning("ME工具 文件转存：%s → %s 移动失败，批次=%d",
                                               _log_label(detail["src_name"] or source),
                                               _log_label(detail["dst_name"] or destination), len(batch))
                                break
                        if moved_ids:
                            moved_files = [item for item in files if item["fid"] in moved_ids]
                            moved_dirs = [item for item in directories if item["cid"] in moved_ids]
                            count, size = self._move_tree_stats(client, moved_dirs)
                            detail["file_count"] = len(moved_files) + count
                            detail["total_bytes"] = sum(int(item.get("size") or 0) for item in moved_files) + size
                            detail["status"] = "success"
                            logger.info("ME工具 文件转存：%s → %s 已移动 %d 项（包含 %d 个文件），大小=%d 字节",
                                        _log_label(detail["src_name"] or source),
                                        _log_label(detail["dst_name"] or destination), len(moved_ids),
                                        detail["file_count"], detail["total_bytes"])
                    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
                        errors.append(f"{source} → {destination}：{error}")
                        detail["status"] = "failed"
                        logger.warning("ME工具 文件转存：%s → %s 查询或转存异常：%s",
                                       _log_label(detail["src_name"] or source),
                                       _log_label(detail["dst_name"] or destination), type(error).__name__)
            self._last_run["p115_move"] = datetime.now().isoformat()
            logger.info("ME工具 文件转存：本次完成，已移动=%d，失败=%d，空目录=%d",
                        moved, len(errors), sum(item["status"] == "empty" for item in details))
            return {"ok": not errors, "moved": moved, "errors": errors, "details": details,
                    "message": f"转存完成：移动 {moved} 项，失败 {len(errors)} 条"}

        return self._locked("p115_move", move)

    @staticmethod
    def _move_tree_stats(client, directories, visited=None):
        visited = visited if visited is not None else set()
        count, size = 0, 0
        for item in directories:
            cid = str(item.get("cid") or "")
            known_size = int(item.get("size") or 0)
            if not cid or cid in visited:
                size += known_size
                continue
            visited.add(cid)
            try:
                files, children = client.list_children(cid)
                count += len(files)
                size += sum(int(file.get("size") or 0) for file in files)
                nested_count, nested_size = EmeTools._move_tree_stats(client, children, visited)
                count += nested_count
                size += nested_size
            except (RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
                logger.warning(f"ME工具统计目录 {cid} 大小失败：{error}")
                size += known_size
        return count, size

    def _send_tool_notice(self, title: str, text: str) -> None:
        # Skip _PluginBase.post_message's automatic "查看详情" plugin link: EME's
        # Telegram notification contains only its configured title and body.
        self.chain.post_message(Message(channel=NotificationChannel.Telegram, mtype=MessageType.Plugin,
                                        title=title or None, text=text, link=None))
        logger.info("ME工具：已提交 MP 插件类型通知，标题=%s（由通知渠道决定实际接收 Bot）",
                    _log_label(title or "无标题"))

    @staticmethod
    def _telegram_confirmation_sources() -> dict:
        """Only bots with an explicit Plugin subscription and an admin may approve cleanup."""
        return {conf.name: conf.config for conf in get_service_configs(SystemConfigKey.Notifications, NotificationConf)
                if conf.enabled and str(conf.type).lower() == "telegram" and conf.name
                and MessageType.Plugin.value in (conf.switchs or []) and conf.config
                and (conf.config.get("TELEGRAM_ADMINS") or conf.config.get("TELEGRAM_CHAT_ID"))}

    def _send_scheduled_telegram_confirmation(self, token: str, snapshot: dict) -> None:
        title = "⚠️【清理无效数据】待确认"
        text = (f"扫描目录：{_log_label(snapshot['root'])}\n待清理：{len(snapshot['items'])} 项\n"
                "确认有效期：发送后 30 分钟；重启后失效。二次核验后移入隔离区，不会立即永久删除。")
        preview = "\n".join("• " + _log_label(os.path.basename(item["path"])) for item in snapshot["items"][:8])
        buttons = [[{"text": "✅ 确认清理", "callback_data": f"[PLUGIN]EmeTools|data:{token}:y"},
                    {"text": "取消", "callback_data": f"[PLUGIN]EmeTools|data:{token}:n"}]]
        self.chain.post_message(Message(channel=NotificationChannel.Telegram, mtype=MessageType.Plugin,
                                        title=title, text=f"{text}\n{preview}",
                                        buttons=buttons, parse_mode="plain"))
        logger.info("ME工具 清理数据：已提交 Telegram 按钮确认，候选=%d", len(snapshot["items"]))

    @eventmanager.register(EventType.MessageAction)
    def scheduled_confirmation_action(self, event: Event) -> None:
        data = event.event_data if event else None
        if not data or str(data.get("plugin_id") or "").lower() != "emetools":
            return
        media_match = re.fullmatch(r"media:([a-f0-9]{32}):([yn])", str(data.get("text") or ""))
        if media_match:
            threading.Thread(target=self._handle_media_bot_confirmation,
                             args=(media_match.group(1), media_match.group(2) == "y", data.copy()), daemon=True).start()
            return
        match = re.fullmatch(r"data:([a-f0-9]{32}):([yn])", str(data.get("text") or ""))
        if not match:
            return
        threading.Thread(target=self._handle_scheduled_confirmation,
                         args=(match.group(1), match.group(2) == "y", data.copy()), daemon=True).start()

    def _handle_scheduled_confirmation(self, token: str, approve: bool, data: dict) -> None:
        source, user = str(data.get("source") or ""), str(data.get("userid") or "")
        channel = data.get("channel")
        if channel not in {NotificationChannel.Telegram, NotificationChannel.Telegram.value} or not source or not user:
            return
        # Confirmation is private and fail-closed: a forwarded button or group message
        # cannot authorize cleanup, even if it carries the original callback token.
        if str(data.get("original_chat_id") or "") != user or not matches_channel_admin(
                NotificationChannel.Telegram, self._telegram_confirmation_sources().get(source), user):
            logger.warning("ME工具 清理数据：Telegram 确认被拒绝（非该 Bot 管理员私聊）")
            return
        if self._schedule["tools"].get("confirm_mode") != "telegram":
            return
        try:
            if approve:
                result = self._confirm_scheduled_tools(token, notify=False)
                text = f"已隔离 {len(result.get('deleted') or [])} 项，跳过 {len(result.get('failed') or [])} 项。"
            else:
                self._consume(token, "scheduled_tools")
                text = "已取消本次清理。"
            logger.info("ME工具 清理数据：Telegram %s完成", "确认" if approve else "取消")
        except Exception as exc:
            logger.warning("ME工具 清理数据：Telegram 确认失败：%s", type(exc).__name__)
            text = "确认已过期、已处理或执行失败，请在插件页面重新扫描并查看日志。"
        self.chain.post_message(Message(channel=NotificationChannel.Telegram, source=source,
                                        userid=user, title="ME工具", text=text))

    @staticmethod
    def _log_scan(snapshot: dict) -> None:
        items = snapshot.get("items") or []
        logger.info("ME工具 清理无效数据：扫描完成，目录=%s，检查目录=%d，候选=%d，读取错误=%d",
                    _log_label(snapshot.get("root")), snapshot.get("total", 0), len(items),
                    len(snapshot.get("errors") or []))
        for item in items[:50]:
            logger.info("ME工具 清理无效数据：候选 %s，类型=%s，包含文件=%d，原因=%s",
                        _log_label(item.get("path") or item.get("name")),
                        _log_label(item.get("kind")), item.get("files", 0), _log_label(item.get("reason")))
        if len(items) > 50:
            logger.info("ME工具 清理无效数据：另有 %d 个候选未逐条显示", len(items) - 50)

    @staticmethod
    def _log_quarantine(result: dict) -> None:
        logger.info("ME工具 清理无效数据：隔离结果，成功=%d，失败=%d，状态=%s",
                    len(result.get("deleted") or []), len(result.get("failed") or []), result.get("ok"))
        for item in (result.get("deleted") or [])[:50]:
            logger.info("ME工具 清理无效数据：已隔离 %s", _log_label(item.get("path")))
        for item in (result.get("failed") or [])[:50]:
            logger.warning("ME工具 清理无效数据：未清理 %s，原因=%s",
                           _log_label(item.get("path")), _log_label(item.get("message")))

    def _run_scheduled(self, section: str) -> None:
        if not self._enabled or not self._schedule.get(section, {}).get("enabled"):
            return
        label = SECTION_NAMES.get(section, section)
        logger.info("ME工具 %s：定时任务开始", label)
        try:
            notification = None
            if section == "tools":
                config = self._schedule[section]
                snapshot = self._cleaner.scan(config["path"])
                count = len(snapshot["items"])
                self._log_scan(snapshot)
                mode = config.get("confirm_mode", "moviepilot" if config.get("confirm_cleanup") else "none")
                if count and config["auto_delete"] and mode == "none":
                    result = self._locked("tools", lambda: self._cleaner.quarantine(snapshot, [item["path"] for item in snapshot["items"]]))
                    self._log_quarantine(result)
                    if result.get("ok"):
                        notification = notices.invalid_cleanup(result, snapshot["root"])
                elif count and config["auto_delete"] and mode in {"moviepilot", "telegram"}:
                    token = self._remember("scheduled_tools", snapshot)
                    if mode == "telegram":
                        if not self._telegram_confirmation_sources():
                            logger.warning("ME工具 清理数据：Telegram 确认渠道已失效，保留待办并改发 MoviePilot 页面确认通知")
                            notification = notices.invalid_confirmation(snapshot)
                        else:
                            try:
                                self._send_scheduled_telegram_confirmation(token, snapshot)
                            except Exception as exc:
                                logger.warning("ME工具 清理数据：Telegram 确认发送失败：%s，保留页面待办", type(exc).__name__)
                                notification = notices.invalid_confirmation(snapshot)
                    else:
                        logger.info("ME工具 清理数据：%d 项等待插件页面二次确认", count)
                        notification = notices.invalid_confirmation(snapshot)
                else:
                    logger.info("ME工具 清理无效数据：无项目或自动清理已关闭，未执行隔离")
            elif section == "p115_cleanup":
                if not self._source_cookie():
                    logger.warning("ME工具 清理文件：跳过，未配置 115 Cookie")
                    notification = ("", "⏰ 115 定时清理跳过：未配置 115 Cookie")
                else:
                    preview = self._cleanup_preview()
                    if not preview.get("ok"):
                        logger.warning("ME工具 清理文件：预览未通过，取消清理")
                    elif preview.get("file_count") or preview.get("dir_count"):
                        result = self._cleanup_confirm(self._remember("cleanup", preview["snapshots"]))
                        if "deleted" in result:
                            notification = notices.file_cleanup(result, preview)
                    elif any(item.get("error") for item in preview.get("folders") or []):
                        notification = notices.file_cleanup({}, preview)
                    else:
                        logger.info("ME工具 清理文件：所有目录为空，本次不清理")
            elif section == "p115_trash":
                if not self._source_cookie():
                    logger.warning("ME工具 清空115回收站：跳过，未配置 115 Cookie")
                    notification = ("", "⏰ 115 回收站清空跳过：未配置 115 Cookie")
                else:
                    info = self._trash_info()
                    if info.get("count"):
                        result = self._trash_clear(info["token"])
                        if result.get("ok"):
                            notification = notices.empty_trash(info)
                        else:
                            logger.warning("ME工具 清空115回收站：清空失败")
                            notification = ("", f"⏰ 115 回收站定时清空失败：{result.get('message') or '未知错误'}")
                    else:
                        logger.info("ME工具 清空115回收站：回收站为空，本次不执行")
            else:
                if not self._source_cookie():
                    logger.warning("ME工具 文件转存：跳过，未配置 115 Cookie")
                    notification = ("", "⏰ 115 监控转存跳过：未配置 115 Cookie")
                elif not self._schedule[section].get("rules"):
                    logger.warning("ME工具 文件转存：跳过，未配置转存规则")
                    notification = ("", "⏰ 115 监控转存跳过：未配置源/目标文件夹")
                else:
                    result = self._move_run()
                    if "moved" in result:
                        notification = notices.file_move(result)
            self._last_run[section] = datetime.now().isoformat()
            if notification:
                self._send_tool_notice(*notification)
            logger.info("ME工具 %s：定时任务结束，通知=%s", label, bool(notification))
        except Exception as error:
            logger.error("ME工具 %s：定时任务异常：%s（敏感详情未记录）", label, type(error).__name__)

    def _pending_info(self):
        with self._pending_lock:
            now = time.monotonic()
            return {"ok": True, "pending": [{"token": token, "kind": kind, "count": len(snapshot.get("items", []))}
                                           for token, (created, kind, snapshot) in self._pending.items()
                                           if kind == "scheduled_tools" and now - created < 1800]}

    def _confirm_scheduled_tools(self, token: str, notify: bool = True):
        snapshot = self._consume(token, "scheduled_tools")
        logger.info("ME工具 清理无效数据：确认定时任务待办，候选=%d", len(snapshot["items"]))
        result = self._locked("tools", lambda: self._cleaner.quarantine(snapshot, [item["path"] for item in snapshot["items"]]))
        self._log_quarantine(result)
        if notify and result.get("ok") and (result.get("deleted") or result.get("failed")):
            title, text = notices.invalid_cleanup(result, snapshot["root"])
            self._send_tool_notice(title, text)
        return result

    async def action(self, action: ToolAction) -> dict:
        operation = action.operation
        if operation not in {"dirs", "root_dirs", "p115_dirs", "pending", "move_info"}:
            logger.info("ME工具：页面操作 %s 开始", _log_label(operation))
        try:
            if operation == "scan":
                result = await asyncio.to_thread(self._cleaner.start_scan, action.path)
                self._log_scan(result)
                return result
            if operation == "delete":
                if not action.scan_token or not action.paths:
                    raise HTTPException(status_code=400, detail="请先扫描并选择清理项目")
                result = await asyncio.to_thread(self._locked, "tools", lambda: self._cleaner.delete(action.scan_token, action.paths))
                self._log_quarantine(result)
                if result.get("ok") and (result.get("deleted") or result.get("failed")):
                    self._send_tool_notice(*notices.invalid_cleanup(result, self._cleaner.resolve(action.path or self._strm_root)))
                return result
            if operation == "request_delete":
                if not action.scan_token or not action.paths:
                    raise HTTPException(status_code=400, detail="请先扫描并选择清理项目")
                root = self._cleaner.resolve(action.path or self._strm_root)
                token = self._remember("manual_tools", {"scan_token": action.scan_token, "paths": action.paths,
                                                        "root": root})
                logger.info("ME工具 清理无效数据：手动申请隔离，待确认=%d 项，目录=%s",
                            len(action.paths), _log_label(root))
                self._send_tool_notice(*notices.invalid_confirmation(
                    {"root": root, "items": [{"path": item} for item in action.paths]}))
                return {"ok": True, "token": token, "pending": True, "message": "请在本页面再次确认隔离"}
            if operation == "confirm_delete":
                saved = self._consume(action.token, "manual_tools")
                result = await asyncio.to_thread(self._locked, "tools", lambda: self._cleaner.delete(saved["scan_token"], saved["paths"]))
                self._log_quarantine(result)
                if result.get("ok") and (result.get("deleted") or result.get("failed")):
                    self._send_tool_notice(*notices.invalid_cleanup(result, saved["root"]))
                return result
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
                result = await asyncio.to_thread(self._trash_clear, action.token)
                if result.get("ok") and result.get("count"):
                    self._send_tool_notice(*notices.empty_trash(result))
                elif not result.get("ok"):
                    self._send_tool_notice("", f"🧹 清空115 回收站失败：{result.get('message') or '未知错误'}")
                return result
            if operation == "cleanup_preview":
                preview = await asyncio.to_thread(self._cleanup_preview)
                preview.pop("snapshots", None)
                return preview
            if operation == "cleanup_request":
                return await asyncio.to_thread(self._cleanup_request)
            if operation == "cleanup_confirm":
                result = await asyncio.to_thread(self._cleanup_confirm, action.token)
                notification = notices.file_cleanup(result, {"folders": []}) if "deleted" in result else None
                if notification:
                    self._send_tool_notice(*notification)
                return result
            if operation == "move_info":
                return await asyncio.to_thread(self._move_info)
            if operation == "move_run":
                return await asyncio.to_thread(self._move_run)
            raise HTTPException(status_code=400, detail="未知的工具操作")
        except (OSError, RuntimeError, ValueError, httpx.HTTPError) as error:
            logger.warning("ME工具：页面操作 %s 失败：%s（敏感详情未记录）",
                           _log_label(operation), type(error).__name__)
            return {"ok": False, "message": str(error)}

    def get_api(self) -> List[dict]:
        return [
            {"path": "/status", "endpoint": self.status, "methods": ["GET"], "auth": "bear", "summary": "MP 工具配置"},
            {"path": "/settings", "endpoint": self.save_settings, "methods": ["POST"], "auth": "bear", "summary": "保存插件连接"},
            {"path": "/schedule", "endpoint": self.save_schedule, "methods": ["POST"], "auth": "bear", "summary": "保存插件任务"},
            {"path": "/action", "endpoint": self.action, "methods": ["POST"], "auth": "bear", "summary": "执行插件工具"},
            {"path": "/monitor/status", "endpoint": self.monitor_status, "methods": ["GET"], "auth": "bear", "summary": "Telegram 监控状态"},
            {"path": "/monitor/action", "endpoint": self.monitor_action, "methods": ["POST"], "auth": "bear", "summary": "Telegram 监控操作"},
            {"path": "/missing/status", "endpoint": self.missing_status, "methods": ["GET"], "auth": "bear", "summary": "缺集检测状态"},
            {"path": "/missing/options", "endpoint": self.missing_options, "methods": ["GET"], "auth": "bear", "summary": "缺集检测可选服务器和媒体库"},
            {"path": "/missing/action", "endpoint": self.missing_action, "methods": ["POST"], "auth": "bear", "summary": "缺集检测操作"},
            {"path": "/media-cleanup/status", "endpoint": self.media_status, "methods": ["GET"], "auth": "bear", "summary": "媒体清理状态"},
            {"path": "/media-cleanup/libraries", "endpoint": self.media_libraries, "methods": ["GET"], "auth": "bear", "summary": "媒体清理媒体库"},
            {"path": "/media-cleanup/action", "endpoint": self.media_action, "methods": ["POST"], "auth": "bear", "summary": "媒体清理操作"},
        ]
