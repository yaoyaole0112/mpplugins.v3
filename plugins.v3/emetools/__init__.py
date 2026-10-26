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
from app.sdk.config import settings as mp_settings
from app.schemas.message import Message
from app.schemas.types import EventType, MessageType, NotificationChannel
from app.schemas.types import SystemConfigKey
from app.schemas.system import NotificationConf
from app.application.service import get_service_configs
from app.application.messaging.channel.admin import matches_channel_admin
from app.sdk.services import NotificationHelper

from .invalid_data import InvalidDataCleaner
from .p115 import P115Client, P115DirectoryUnavailable
from .p115_tasks import P115TaskCoordinator
from .subscription_monitor import SubscriptionMonitor, normalize_channel
from .missing_episodes import DEFAULT_MISSING, MissingAction, MissingEpisodeDetector, normalize_cancel_config
from .missing_fill import MissingFill
from .media_cleanup import MediaCleanup, DEFAULT_CONFIG as DEFAULT_MEDIA_CLEANUP, DEFAULT_RULES, validate_rules
from .data_enrichment import DataEnrichment, DEFAULT_ENRICH_CONFIG, validate_enrich_config
from .enrichment_llm import llm_available, llm_setting
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
DEFAULT_NOTIFICATIONS = {
    "enabled": True,
    "only_failures": False,
    "subscription": True,
    "missing": True,
    "media": True,
    "enrichment": True,
    "invalid": True,
    "cleanup": True,
    "move": True,
    "trash": True,
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


class EnrichmentAction(BaseModel):
    operation: str
    keyword: str = ""
    series_id: str = ""
    mode: str = "all"
    episode_ids: List[str] = Field(default_factory=list)
    series_ids: List[str] = Field(default_factory=list)
    library_ids: List[str] = Field(default_factory=list)
    force: bool = False
    metadata_source: str = ""
    allow_mediainfo: bool = False


class EmeTools(_PluginBase):
    plugin_name = "媒体增强"
    plugin_desc = "在 MoviePilot 中提供订阅监控、缺集检测、媒体清理、电影与剧集数据补全、无效数据清理及 115 文件管理。"
    plugin_icon = ICON_URL
    plugin_version = "2.10.49"
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
        saved_notifications = config.get("notifications") or {}
        self._notifications = {key: bool(saved_notifications.get(key, default))
                               for key, default in DEFAULT_NOTIFICATIONS.items()}
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
        saved_missing = normalize_cancel_config(saved_missing)
        self._missing_config.update({key: value for key, value in saved_missing.items() if key in DEFAULT_MISSING})
        self._missing = MissingEpisodeDetector(self, self._missing_config)
        self._fill = MissingFill(self)
        self._missing_manual_scan_id = 0
        self._missing_finished_scan_id = 0
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
        try:
            self._enrichment_config = validate_enrich_config(config.get("enrichment") or {})
        except ValueError:
            self._enrichment_config = dict(DEFAULT_ENRICH_CONFIG)
        self._enrichment = DataEnrichment(self)
        self._schedule = copy.deepcopy(DEFAULT_SCHEDULE)
        for name, defaults in self._schedule.items():
            saved = (config.get("schedule") or {}).get(name) or {}
            defaults.update({key: value for key, value in saved.items() if key in SCHEDULE_FIELDS[name]})
            if name == "tools" and "confirm_mode" not in saved:
                defaults["confirm_mode"] = "moviepilot" if defaults["confirm_cleanup"] else "none"
        self._cleaner = InvalidDataCleaner(self._strm_root)
        self._p115_stop = threading.Event()
        self._locks = {name: threading.Lock() for name in self._schedule}
        if not hasattr(self, "_p115_tasks"):
            self._p115_tasks = P115TaskCoordinator()
        self._p115_tasks.reopen()
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._cleanup_telegram_sources = {}
        self._cleanup_command_origins = {}
        self._cleanup_status = {}
        self._cleanup_message_refs = {}
        self._cleanup_message_lock = threading.Lock()
        self._last_run = {}
        if "cookie" in config or "proxy" in config:
            self._persist()
        logger.info("媒体增强：初始化完成，启用=%s，STRM=%s，任务=%s，订阅/关键词监控=%s/%s",
                    self._enabled, _log_label(self._strm_root),
                    ", ".join(SECTION_NAMES[key] for key, value in self._schedule.items() if value["enabled"]) or "无",
                    self._monitor_config["sub"]["enabled"], self._monitor_config["kw"]["enabled"])
        if self._enabled and self._tg_session and any(item["enabled"] for item in self._monitor_config.values()):
            logger.info("媒体增强：尝试恢复 Telegram 监控（不会输出账号和登录凭据）")
            self._monitor._ensure_loop()
            asyncio.run_coroutine_threadsafe(self._monitor.resume(), self._monitor.loop)
        if self._enabled and self._enrichment_config["auto_on_import"]:
            self._enrichment.resume_imports()

    @eventmanager.register(EventType.WebhookMessage)
    def on_series_import(self, event: Event):
        """Debounce Emby movie additions and episode additions by parent series."""
        if not getattr(self, "_enabled", False) or not self._enrichment_config["auto_on_import"]:
            return
        data = getattr(event, "event_data", None)
        if not data:
            return
        raw = getattr(data, "json_object", None) or {}
        item = raw.get("Item") if isinstance(raw, dict) else {}
        item = item if isinstance(item, dict) else {}
        event_name = str(getattr(data, "event", None) or "").strip()
        channel = str(getattr(data, "channel", None) or "").strip()
        media_type = (getattr(data, "media_type", None) or getattr(data, "item_type", None)
                      or item.get("Type") or "")
        # Emby Webhooks has used both ``media_type`` and ``item_type`` in
        # MoviePilot releases.  Keep the filter strict on the payload item,
        # but do not discard a valid Episode merely because one normalized
        # field is absent.  This is intentionally diagnostic-only and never
        # includes webhook URLs, tokens, paths, or message bodies.
        if event_name not in ("library.new", "ItemAdded"):
            return
        movie = str(media_type).lower() in ("movie", "电影")
        if not movie and str(media_type).lower() not in ("episode", "剧集"):
            return
        if channel and "emby" not in channel.lower():
            source_name = raw.get("Server", {}).get("Name") if isinstance(raw, dict) and isinstance(raw.get("Server"), dict) else ""
            if "emby" not in str(source_name).lower():
                return
        has_series_id = bool(item.get("SeriesId") or item.get("SeriesIdStr") or
                            ((item.get("Series") or {}).get("Id")
                             if isinstance(item.get("Series"), dict) else ""))
        logger.info("媒体增强 数据补全：收到 Emby 入库事件 event=%s channel=%s type=%s series_id=%s",
                    event_name[:32], channel[:32] or "未知", str(media_type)[:32], has_series_id)
        source = raw.get("Server") if isinstance(raw, dict) else None
        server = str(getattr(data, "server_name", "") or
                     (source.get("Name") if isinstance(source, dict) else "") or "")
        if not server:
            servers = list(self._enrichment._services())
            if len(servers) == 1:
                server = servers[0]
        # Emby ItemAdded's item_id is the Episode ID.  Automatic enrichment
        # must debounce by the parent Series ID, otherwise _enrich receives an
        # Episode and silently fails the "Type == Series" validation.
        identifier = str(item.get("SeriesId") or item.get("SeriesIdStr") or
                         ((item.get("Series") or {}).get("Id") if isinstance(item.get("Series"), dict) else "") or "")
        if movie:
            identifier = str(item.get("Id") or getattr(data, "item_id", "") or "")
        if not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", identifier) or not server:
            logger.warning("媒体增强 数据补全：Emby 入库事件缺少电影 ID／剧集 SeriesId 或媒体服务器，已跳过自动补全")
            return
        try:
            self._enrichment.queue_import(f"{server}::{identifier}")
        except (ValueError, OSError, RuntimeError) as exc:
            logger.warning("媒体增强 数据补全：忽略不完整的 Emby 入库事件：%s", type(exc).__name__)

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        """Use MoviePilot's transfer event when Emby Webhook is unavailable."""
        if not getattr(self, "_enabled", False) or not self._enrichment_config["auto_on_import"]:
            return
        data = getattr(event, "event_data", None)
        if not isinstance(data, dict):
            return
        media = data.get("mediainfo") or {}
        tmdb_id = (media.get("tmdb_id") if isinstance(media, dict) else
                   getattr(media, "tmdb_id", None) or getattr(media, "tmdbid", None))
        if not tmdb_id:
            meta = data.get("meta") or {}
            tmdb_id = meta.get("tmdb_id") if isinstance(meta, dict) else getattr(meta, "tmdb_id", None)
        if str(tmdb_id).isdigit():
            media_type = media.get("type") if isinstance(media, dict) else getattr(media, "type", None)
            if not media_type:
                meta = data.get("meta") or {}
                media_type = meta.get("type") if isinstance(meta, dict) else getattr(meta, "type", None)
            media_type = str(getattr(media_type, "value", media_type) or "").lower()
            logger.info("媒体增强 数据补全：整理完成事件触发入库兜底（TMDB=%s）", str(tmdb_id)[:20])
            self._enrichment.queue_import_by_tmdb(tmdb_id, "Movie" if media_type in ("movie", "电影") else "Series")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[dict]:
        return [{"cmd": command, "event": EventType.PluginAction, "desc": description,
                 "category": "工具", "data": {"action": f"emetools_{action}"}}
                for command, action, description in (
                    ("/cleanup", "cleanup", "扫描无效数据并确认隔离（TG/网页同步）"),
                    ("/cleanfiles", "cleanfiles", "预览 115 清理目录并确认清理"),
                    ("/cleartrash", "cleartrash", "预览并确认清空 115 回收站（不可恢复）"),
                    ("/cleandupes", "cleandupes", "扫描并确认清理低质版本"),
                    ("/ememove", "move", "预览并确认执行已保存的 115 文件转存规则"))]

    @eventmanager.register(EventType.PluginAction)
    def tool_command(self, event: Event) -> None:
        data = event.event_data if event else None
        action = (data or {}).get("action", "")
        if not self._enabled or action not in {"emetools_cleanup", "emetools_cleanfiles", "emetools_cleartrash", "emetools_move", "emetools_cleandupes"}:
            return
        # Command replies must have a real destination; never turn them into broadcasts.
        if not data.get("user") or not data.get("channel") or not data.get("source"):
            logger.warning("媒体增强 Bot 命令缺少用户、渠道或来源，已拒绝回复以避免广播")
            return
        threading.Thread(target=self._run_tool_command, args=(action, data.copy()), daemon=True).start()

    def _run_tool_command(self, action: str, data: dict) -> None:
        if action == "emetools_cleanup":
            return self._run_tool_command_serial(action, data)
        return self._p115_tasks.run(f"bot:{action}", "Bot手动115任务",
                                    lambda: self._run_tool_command_serial(action, data), manual=True)

    def _run_tool_command_serial(self, action: str, data: dict) -> None:
        try:
            if action in {"emetools_cleanup", "emetools_cleartrash", "emetools_move"}:
                source, user = str(data["source"]), str(data["user"])
                if (data["channel"] not in {NotificationChannel.Telegram, NotificationChannel.Telegram.value}
                        or not matches_channel_admin(NotificationChannel.Telegram,
                                                     self._telegram_command_sources().get(source), user)):
                    self._media_bot_reply(source, user, "媒体增强", "❌ 当前 Telegram 用户没有执行权限。")
                    return
            logger.info("媒体增强 Bot 命令开始：%s，来源=%s", action, _log_label(data["source"]))
            if action == "emetools_cleanup":
                result = self._cleaner.start_scan(self._strm_root)
                if result["count"]:
                    self._request_tool_bot_confirmation("manual_tools", data, scan=result)
                    return
                text = "未发现无效数据。"
            elif action == "emetools_cleanfiles":
                self._request_file_bot_cleanup(data)
                return
            elif action == "emetools_cleartrash":
                self._request_tool_bot_confirmation("trash", data)
                return
            elif action == "emetools_cleandupes":
                self._request_media_bot_cleanup(data)
                return
            else:
                self._request_tool_bot_confirmation("move", data)
                return
            self.chain.post_message(Message(channel=data["channel"], source=data["source"],
                                            userid=str(data["user"]), title="媒体增强", text=text))
            logger.info("媒体增强 Bot 命令完成：%s", action)
        except Exception as exc:
            logger.warning("媒体增强命令 %s 执行失败: %s", action, type(exc).__name__)
            self.chain.post_message(Message(channel=data["channel"], source=data["source"], userid=str(data["user"]),
                                            title="媒体增强", text=f"命令执行失败：{type(exc).__name__}，请查看插件日志。"))

    @staticmethod
    def _telegram_command_sources() -> dict:
        """Resolve only the originating enabled Telegram Bot; never broadcast a command."""
        return {conf.name: conf.config for conf in get_service_configs(SystemConfigKey.Notifications, NotificationConf)
                if conf.enabled and str(conf.type).lower() == "telegram" and conf.name and conf.config}

    def _media_bot_reply(self, source: str, user: str, title: str, text: str, buttons=None) -> None:
        if self._send_tracked_cleanup_confirmation(title, text, buttons, source=source, user=user):
            return
        self.chain.post_message(Message(channel=NotificationChannel.Telegram, source=source, userid=user,
                                        title=title, text=text, buttons=buttons, parse_mode="plain"))

    def _request_file_bot_cleanup(self, data: dict) -> None:
        source, user = str(data["source"]), str(data["user"])
        if data["channel"] not in {NotificationChannel.Telegram, NotificationChannel.Telegram.value}:
            return
        config = self._telegram_command_sources().get(source)
        if not config or not matches_channel_admin(NotificationChannel.Telegram, config, user):
            self._media_bot_reply(source, user, "媒体增强", "❌ 当前 Telegram 用户没有清理权限。")
            return
        result = self._cleanup_request(command_origin=(source, user))
        if not result.get("pending"):
            self._media_bot_reply(source, user, "媒体增强", result.get("message") or "预览失败，未执行清理。")

    def _file_cleanup_actor_allowed(self, token: str, source: str, user: str) -> bool:
        with self._pending_lock:
            origin = self._cleanup_command_origins.get(token)
        if origin is not None:
            if origin != (source, user):
                return False
            config = self._telegram_command_sources().get(source)
        else:
            config = self._telegram_confirmation_sources().get(source)
        return bool(config) and matches_channel_admin(NotificationChannel.Telegram, config, user)

    def _request_media_bot_cleanup(self, data: dict) -> None:
        source, user = str(data["source"]), str(data["user"])
        if data["channel"] not in {NotificationChannel.Telegram, NotificationChannel.Telegram.value}:
            return
        config = self._telegram_command_sources().get(source)
        if not config or not matches_channel_admin(NotificationChannel.Telegram, config, user):
            self._media_bot_reply(source, user, "媒体增强", "❌ 当前 Telegram 用户没有清理权限。")
            return
        self._media_bot_reply(source, user, "媒体增强", "🔄 正在扫描低质版本，可能需要几分钟…")
        try:
            result = self._media.scan(self._media_config["library_ids"])
            versions = [version for group in result["results"] for version in group["versions"]
                        if not version["is_best"]]
            if not versions:
                self._media_bot_reply(source, user, "媒体增强", "✅ 未发现需删除的低质版本。")
                return
            if len(versions) > 1000:
                self._media_bot_reply(source, user, "媒体增强", "❌ 待删除版本超过 1000 个，请在插件页面缩小媒体库范围后再执行。")
                return
            token = self._remember("media_bot", {"source": source, "user": user,
                                                  "paths": [v["file_path"] for v in versions],
                                                  "scan": self._media.last_scan})
            title, text = notices.media_confirmation(versions)
            buttons = [[{"text": "✅ 确认清理", "callback_data": f"[PLUGIN]EmeTools|media:{token}:y"},
                        {"text": "取消", "callback_data": f"[PLUGIN]EmeTools|media:{token}:n"}]]
            self._media_bot_reply(source, user, title, text, buttons)
            logger.info("媒体增强 媒体清理命令：已向发起 Bot 的管理员提交 %d 个候选确认", len(versions))
        except Exception as error:
            logger.warning("媒体增强 媒体清理命令扫描失败：%s", type(error).__name__)
            self._media_bot_reply(source, user, "媒体增强", f"❌ 扫描或发送确认失败：{_log_label(error)}")

    def _handle_media_bot_confirmation(self, token: str, approve: bool, data: dict) -> None:
        if not approve:
            return self._handle_media_bot_confirmation_serial(token, approve, data)
        return self._p115_tasks.run("media_bot_confirm:" + token, "Bot确认媒体清理",
                                    lambda: self._handle_media_bot_confirmation_serial(token, approve, data), manual=True)

    def _handle_media_bot_confirmation_serial(self, token: str, approve: bool, data: dict) -> None:
        source, user = str(data.get("source") or ""), str(data.get("userid") or "")
        if (not self._enabled or data.get("channel") not in {NotificationChannel.Telegram, NotificationChannel.Telegram.value}
                or not source or not user or str(data.get("original_chat_id") or "") != user
                or not matches_channel_admin(NotificationChannel.Telegram,
                                             self._telegram_command_sources().get(source), user)):
            logger.warning("媒体增强 媒体清理：Telegram 确认被拒绝（需要原 Bot 管理员私聊）")
            return
        try:
            with self._pending_lock:
                pending = self._pending.get(token)
                if (not pending or pending[1] != "media_bot" or time.monotonic() - pending[0] >= 1800
                        or pending[2]["source"] != source or pending[2]["user"] != user):
                    return
            self._register_confirmation_message(token, data)
            if not approve:
                if self._set_cleanup_status(token, "cancelled", "已取消本次清理。", expected_state="pending"):
                    self._consume(token, "media_bot")
                return
            if not self._set_cleanup_status(token, "processing", "正在复核并清理低质版本，请勿重复操作。",
                                            expected_state="pending"):
                return
            record = self._consume(token, "media_bot")
            if record["scan"] != self._media.last_scan:
                raise ValueError("扫描结果已更新，请重新发送命令")
            result = self._media.delete(record["paths"])
            if result["deleted"]:
                self._media.refresh_emby()
            self._finish_confirmation(token, "failed" if result.get("failures") else "completed",
                notices.media_cleanup(result) or ("媒体增强 · 媒体清理", "✅ 未发现需删除的低质版本。"),
                command_origin=(source, user))
        except Exception as error:
            logger.warning("媒体增强 媒体清理：Telegram 确认失败：%s", type(error).__name__)
            self._finish_confirmation(token, "failed", ("媒体增强 · 媒体清理",
                f"❌ 确认已过期、已处理或执行失败：{_log_label(error)}"), command_origin=(source, user))

    def stop_service(self) -> None:
        logger.info("媒体增强：停止插件服务和 Telegram 监控")
        if hasattr(self, "_p115_stop"):
            self._p115_stop.set()
        if hasattr(self, "_p115_tasks"):
            self._p115_tasks.close()
        enrichment = getattr(self, "_enrichment", None)
        if enrichment:
            enrichment.close()
        monitor = getattr(self, "_monitor", None)
        if monitor and monitor.loop and monitor.thread and monitor.thread.is_alive():
            try:
                asyncio.run_coroutine_threadsafe(self._fill.shutdown(), monitor.loop).result(timeout=8)
                asyncio.run_coroutine_threadsafe(monitor.shutdown(), monitor.loop).result(timeout=8)
            except Exception as exc:
                logger.warning(f"媒体增强 Telegram 客户端关闭失败: {type(exc).__name__}")
            monitor.loop.call_soon_threadsafe(monitor.loop.stop)
            monitor.thread.join(timeout=3)

    @staticmethod
    def get_render_mode() -> Tuple[str, str]:
        return "vue", "dist/assets"

    def get_sidebar_nav(self) -> List[dict]:
        if not self._enabled or not self._show_sidebar_nav:
            return []
        return [{"nav_key": "main", "title": "媒体增强", "icon": "mdi-shimmer",
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
                    logger.error(f"媒体增强 {name} 定时表达式无效：{error}")
                    continue
                services.append({"id": f"EmeTools_{name}", "name": f"媒体增强 {name}",
                                 "trigger": trigger, "func": self._run_scheduled,
                                 "kwargs": {}, "func_kwargs": {"section": name}})
        move = self._schedule["p115_move"]
        if move.get("enabled") and move.get("rules"):
            services.append({"id": "EmeTools_p115_move", "name": "媒体增强文件转存",
                             "trigger": "interval", "func": self._run_scheduled,
                             "kwargs": {"seconds": max(60, int(move.get("check_interval") or 120))},
                             "func_kwargs": {"section": "p115_move"}})
        if self._missing_config["enabled"]:
            if self._legacy_missing_active():
                logger.warning("媒体增强 缺集检测：旧剧集缺集检测订阅插件仍启用，已跳过新定时任务")
            else:
                try:
                    trigger = CronTrigger.from_crontab(self._missing_config["cron"])
                    services.append({"id": "EmeTools_missing", "name": "媒体增强 缺集检测",
                                     "trigger": trigger, "func": self._run_missing_scheduled,
                                     "kwargs": {}})
                except ValueError as error:
                    logger.error("媒体增强 缺集检测定时表达式无效：%s", error)
        if self._media_config["enabled"]:
            try:
                trigger = CronTrigger.from_crontab(self._media_config["cron"])
                services.append({"id": "EmeTools_media_cleanup", "name": "媒体增强 媒体清理",
                                 "trigger": trigger, "func": self._run_media_scheduled, "kwargs": {}})
            except ValueError as error:
                logger.error("媒体增强 媒体清理周期无效：%s", error)
        if self._enrichment_config["auto_on_import"]:
            services.append({"id": "EmeTools_enrichment_import_poll",
                             "name": "媒体增强 入库补全兜底检查",
                             "trigger": "interval", "func": self._enrichment.poll_recent_imports,
                             "kwargs": {"seconds": 300}})
        return services

    def _run_media_scheduled(self) -> None:
        return self._p115_tasks.run("media_cleanup", "定时媒体清理", self._run_media_scheduled_serial)

    def _run_media_scheduled_serial(self) -> None:
        if not self._enabled or not self._media_config["enabled"]:
            return
        try:
            result = self._media.scan(self._media_config["library_ids"])
            paths = [version["file_path"] for group in result["results"]
                     for version in group["versions"] if not version["is_best"]]
            logger.info("媒体增强 媒体清理定时扫描：重复组=%d，待清理=%d", result["duplicate_groups"], len(paths))
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
                logger.info("媒体增强 媒体清理定时任务：已清理=%d，失败=%d",
                            len(outcome["deleted"]), len(outcome["failures"]))
        except Exception as error:
            logger.warning("媒体增强 媒体清理定时任务失败：%s：%s", type(error).__name__, _log_label(error))
            if "STRM 根目录不存在" not in str(error):
                self._send_tool_notice("", f"⏰ 定时去重执行失败：{_log_label(error)}")

    def _legacy_missing_active(self) -> bool:
        legacy = self.get_config("EpisodeMissingSubscribe") or {}
        return isinstance(legacy, dict) and bool(legacy.get("enabled"))

    def _run_missing_scheduled(self) -> None:
        if not self._enabled or not self._missing_config["enabled"] or self._legacy_missing_active():
            logger.warning("媒体增强 缺集检测：插件已关闭或原缺集插件仍启用，跳过本次检测")
            return
        self._missing.scan_missing_episodes()

    def _settings(self) -> dict:
        return {"enabled": self._enabled, "show_sidebar_nav": self._show_sidebar_nav,
                "strm_root": self._strm_root, "cookie_configured": bool(self._source_cookie()),
                "rb_password_configured": self._rb_password != "000000", "tg_api_id": self._tg_api_id,
                "tg_api_hash_configured": bool(self._tg_api_hash),
                "tg_forward_token_configured": bool(self._tg_forward_token),
                "notifications": copy.deepcopy(self._notifications)}

    def _persist(self) -> None:
        self.update_config({"enabled": self._enabled, "show_sidebar_nav": self._show_sidebar_nav,
                            "strm_root": self._strm_root, "rb_password": self._rb_password,
                            "schedule": copy.deepcopy(self._schedule),
                            "tg_api_id": self._tg_api_id, "tg_api_hash": self._tg_api_hash,
                            "tg_forward_token": self._tg_forward_token, "tg_session": self._tg_session,
                            "monitor": copy.deepcopy(self._monitor_config),
                            "missing": copy.deepcopy(self._missing_config),
                            "media_cleanup": copy.deepcopy(self._media_config),
                            "enrichment": copy.deepcopy(self._enrichment_config),
                            "notifications": copy.deepcopy(self._notifications),
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
        if action.get("operation") == "confirm_delete":
            saved = copy.deepcopy(action)
            job = self._p115_tasks.submit("media_delete:" + str(saved.get("token") or ""), "手动媒体清理",
                                          lambda: asyncio.run(self._media_action_serial(saved)))
            return await self._p115_submission(job)
        return await self._media_action_serial(action)

    async def _media_action_serial(self, action: dict) -> dict:
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
            return {"message": "媒体清理配置已保存；定时删除仅在启用时运行", "config": copy.deepcopy(updated)}
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
                    logger.warning("媒体增强 媒体清理扫描失败：%s：%s",
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
            try:
                result = await asyncio.to_thread(self._media.delete, record["paths"])
            except ValueError as error:
                logger.warning("媒体增强 媒体清理删除校验失败：%s", _log_label(error))
                return {"ok": False, "deleted": [], "failures": [{"error": str(error)[:160]}]}
            except Exception as error:
                logger.exception("媒体增强 媒体清理删除失败")
                return {"ok": False, "deleted": [], "failures": [{"error": f"{type(error).__name__}：{str(error)[:120]}"}]}
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

    def _p115_cooldown_remaining(self) -> int:
        return P115Client.cooldown_remaining_for_cookie(self._source_cookie())

    def _p115_deferred_message(self, remaining: int) -> str:
        return f"115 网络暂不可用，约 {remaining} 秒后自动复查；本轮未发送请求"

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
            if kind in {"cleanup", "trash", "move", "manual_tools", "scheduled_tools", "media_bot"}:
                self._cleanup_status[token] = {"state": "pending", "kind": kind, "updated": now}
                self._cleanup_status = {key: value for key, value in self._cleanup_status.items()
                                        if now - value["updated"] < 3600}
            return token

    def _set_cleanup_status(self, token: str, state: str, message: str = "", *, expected_state=None) -> bool:
        with self._cleanup_message_lock:
            return self._sync_cleanup_status(token, state, message, expected_state=expected_state)

    def _sync_cleanup_status(self, token: str, state: str, message: str, *, expected_state=None) -> bool:
        with self._pending_lock:
            previous_status = self._cleanup_status.get(token, {})
            previous = previous_status.get("state")
            if expected_state is not None and previous != expected_state:
                return False
            if previous in {"completed", "cancelled", "failed", "expired"} and state != previous:
                return False
            kind = self._cleanup_status.get(token, {}).get("kind", "cleanup")
            self._cleanup_status[token] = {**previous_status, "state": state, "kind": kind, "message": message,
                                           "updated": time.monotonic()}
            references = list(self._cleanup_message_refs.get(token, []))
        for reference in references:
            synced = reference.get("synced_result")
            if synced and synced[0] == state and (
                    state not in {"completed", "cancelled", "failed", "expired"} or synced[1] == message):
                continue
            if kind != "cleanup":
                edited = self._edit_tool_confirmation_message(reference, token, kind, state, message)
            else:
                edited = self._edit_file_cleanup_message(reference, token, message or (
                    "⌛️ 清理中：正在排队或复核目录，请勿重复操作。" if state in {"queued", "processing"} else
                    "🗑️ 清理完成。" if state == "completed" else
                    "已取消本次文件清理。" if state == "cancelled" else "清理未完成，请重新预览并核实结果。"),
                    processing=state in {"queued", "processing"}, completed=state == "completed")
            if edited:
                reference["synced_result"] = (state, message)
        return True

    def _finish_confirmation(self, token, state, notice, *, command_origin=None):
        title, text = notice
        with self._cleanup_message_lock:
            with self._pending_lock:
                status = self._cleanup_status.get(token)
                if not status or status.get("notice_sent"):
                    return
            if not self._sync_cleanup_status(token, state, text):
                return
            with self._pending_lock:
                self._cleanup_status[token]["notice_sent"] = True
                references = list(self._cleanup_message_refs.get(token, []))
        origins = list(dict.fromkeys((str(reference["source"]), str(reference["userid"]))
            for reference in references if reference.get("synced_result") != (state, text))) if references else [command_origin]
        for origin in origins:
            try:
                if origin:
                    self._media_bot_reply(*origin, title, text)
                else:
                    self._send_tool_notice(title, text)
            except Exception as error:
                logger.warning("媒体增强 确认结果兜底通知失败：%s，不影响执行结果", type(error).__name__)

    def _register_confirmation_message(self, token, data):
        reference = {key: data.get(key) for key in
                     ("source", "userid", "original_chat_id", "original_message_id")}
        identity = lambda item: tuple(str(item.get(key)) for key in
                                     ("source", "original_chat_id", "original_message_id"))
        with self._pending_lock:
            references = self._cleanup_message_refs.setdefault(token, [])
            if not any(identity(saved) == identity(reference) for saved in references):
                references.append(reference)

    def _get_cleanup_status(self, token: str) -> dict:
        with self._pending_lock:
            status = self._cleanup_status.get(token)
            pending = self._pending.get(token)
            if status and status["state"] == "pending" and (
                    not pending or time.monotonic() - pending[0] >= 1800):
                return {"state": "expired"}
            if status and time.monotonic() - status["updated"] < 3600:
                return {key: value for key, value in status.items() if key not in {"updated", "notice_sent"}}
            if pending and time.monotonic() - pending[0] < 1800:
                return {"state": "pending"}
            return {"state": "expired"}

    def _consume(self, token: str, kind: str):
        with self._pending_lock:
            pending = self._pending.get(token)
            if not pending or pending[1] != kind or time.monotonic() - pending[0] >= 1800:
                raise HTTPException(status_code=400, detail="确认已过期或已处理，请重新预览")
            self._pending.pop(token)
            self._cleanup_telegram_sources.pop(token, None)
            self._cleanup_command_origins.pop(token, None)
        return pending[2]

    @staticmethod
    def _tool_confirmation_labels(kind):
        return {"trash": ("trash", "清空回收站", "清空"), "move": ("move", "文件转存", "转存"),
                "manual_tools": ("data", "清理无效数据", "清理"),
                "scheduled_tools": ("data", "清理无效数据", "清理"),
                "media_bot": ("media", "媒体清理", "清理")}[kind]

    @staticmethod
    def _trash_fingerprint(items):
        return sorted(tuple(str(item.get(key) or "") for key in
                            ("id", "file_id", "file_name", "file_size", "dtime")) for item in items)

    @staticmethod
    def _move_fingerprint(files, folders):
        return sorted((kind, str(item.get(identifier) or ""), str(item.get("size") or 0),
                       str(item.get("name") or ""))
                      for kind, identifier, items in (("file", "fid", files), ("directory", "cid", folders))
                      for item in items)

    def _publish_tool_confirmation(self, token, kind, text, command_origin=None):
        prefix, label, verb = self._tool_confirmation_labels(kind)
        try:
            sources = {command_origin[0]: True} if command_origin else self._telegram_confirmation_sources()
        except (RuntimeError, ValueError, OSError) as error:
            logger.warning("媒体增强 %s：通知渠道读取失败（%s），保留网页确认", label, type(error).__name__)
            sources = {}
        with self._pending_lock:
            self._cleanup_telegram_sources[token] = set(sources)
            if command_origin:
                self._cleanup_command_origins[token] = command_origin
        buttons = [[{"text": f"✅ 确认{verb}", "callback_data": f"[PLUGIN]EmeTools|{prefix}:{token}:y"},
                    {"text": "取消", "callback_data": f"[PLUGIN]EmeTools|{prefix}:{token}:n"}]] if sources else None
        text += "\n\n确认有效期：30 分钟；服务重启后失效。网页和 TG 两处共用一次性确认，任一处操作后另一处同步失效。"
        title = f"⚠️【{label}】待确认"
        try:
            if command_origin:
                self._media_bot_reply(*command_origin, title, text, buttons=buttons)
            else:
                self._send_tool_notice(title, text, buttons=buttons)
        except Exception:
            if command_origin:
                self._consume(token, kind)
                self._set_cleanup_status(token, "failed", "确认通知发送失败，请重新发起")
                raise
            logger.warning("媒体增强 %s：确认通知发送失败，保留网页待办", label)

    def _trash_request(self, command_origin=None):
        result = self._trash_info()
        if not result["count"]:
            self._consume(result["token"], "trash")
            self._set_cleanup_status(result["token"], "completed", "回收站为空")
            return {"ok": True, "empty": True, "count": 0, "message": "回收站为空"}
        self._publish_tool_confirmation(result["token"], "trash",
            f"待彻底删除：{result['count']} 项（{notices.format_bytes(result['size_bytes'])}）。\n"
            "⚠️ 彻底删除不可恢复！确认前请核对回收站内容；执行前将再次核验。", command_origin)
        return {**result, "pending": True}

    def _move_request(self, command_origin=None):
        result = self._move_info(snapshot=True)
        if not result["rules"] or any(item["count"] < 0 for item in result["pending"].values()):
            return {"ok": False, "message": "未配置转存规则或部分源目录读取失败，请核实后重新预览"}
        if not any(item["count"] for item in result["pending"].values()):
            result.pop("entries", None)
            result.pop("copy_records", None)
            return {**result, "empty": True, "message": "没有新增待转存项目"}
        token = self._remember("move", {"rules": result["rules"], "entries": result.pop("entries"),
                                        "copy_records": result.pop("copy_records"),
                                        "preview": copy.deepcopy(result)})
        lines = [f"{'复制新增' if rule.get('mode') == 'copy' else '移动'}："
                 f"{rule.get('src_name') or rule['src_id']} → {rule.get('dst_name') or rule['dst_id']}"
                 f"（{result['pending'][rule['src_id']]['count']} 项）" for rule in result["rules"]]
        self._publish_tool_confirmation(token, "move", "\n".join(lines) +
            "\n仅执行已保存规则；规则或源目录项目变化后需重新预览。复制保留增量去重。", command_origin)
        return {**result, "token": token, "pending_confirmation": True}

    def _data_request(self, scan, paths, command_origin=None):
        token = self._remember("manual_tools", {"scan_token": scan["scan_token"], "paths": list(paths),
                                                "root": scan["root"]})
        self._publish_tool_confirmation(token, "manual_tools",
            f"扫描目录：{_log_label(scan['root'])}\n待隔离：{len(paths)} 项。\n"
            "二次核验后移入插件隔离区，不会立即永久删除。", command_origin)
        return {"ok": True, "token": token, "pending": True, "message": "请确认隔离，也可在 Telegram 管理员私聊确认"}

    def _request_tool_bot_confirmation(self, kind, data, scan=None):
        origin = (str(data["source"]), str(data["user"]))
        if kind == "trash":
            result = self._trash_request(origin)
        elif kind == "move":
            result = self._move_request(origin)
        else:
            result = self._data_request(scan, [item["path"] for item in scan["items"]], origin)
        if not result.get("token"):
            self._media_bot_reply(*origin, "媒体增强", result.get("message") or "没有待处理项目")

    def _claim_tool_confirmation(self, token, kind):
        with self._pending_lock:
            pending = self._pending.get(token)
            invalid = not pending or pending[1] != kind or time.monotonic() - pending[0] >= 1800
        if invalid:
            if pending and pending[1] == kind:
                self._set_cleanup_status(token, "expired", "确认已过期，请重新预览", expected_state="pending")
            raise HTTPException(status_code=400, detail="确认已过期或已处理，请重新预览")
        if not self._set_cleanup_status(token, "processing", "已确认，正在排队或复核", expected_state="pending"):
            raise HTTPException(status_code=409, detail="本次操作已提交或已结束，请勿重复确认")

    def _execute_tool_confirmation(self, token, kind, *, command_origin=None):
        try:
            if not self._enabled:
                raise ValueError("插件已停用，未执行操作")
            if command_origin and not self._file_cleanup_actor_allowed(token, *command_origin):
                raise ValueError("权限配置已变化，未执行操作")
            if kind == "trash":
                result = self._trash_clear(token)
                notice = notices.empty_trash(result) if result.get("ok") and result.get("count") else None
            elif kind == "move":
                saved = self._consume(token, kind)
                result = self._move_run(expected=saved)
                notice = notices.file_move(result) if "moved" in result else None
            elif kind == "scheduled_tools":
                with self._pending_lock:
                    root = self._pending.get(token, (0, "", {}))[2].get("root", "")
                result = self._confirm_scheduled_tools(token, notify=False)
                notice = notices.invalid_cleanup(result, root)
            else:
                saved = self._consume(token, kind)
                result = self._locked("tools", lambda: self._cleaner.delete(saved["scan_token"], saved["paths"]))
                self._log_quarantine(result)
                notice = notices.invalid_cleanup(result, saved["root"])
            successful = bool(result.get("ok")) and not result.get("failed") and not result.get("deferred")
            prefix, label, verb = self._tool_confirmation_labels(kind)
            title, text = notice or (f"媒体增强 · {label}", result.get("message") or
                                     f"已隔离 {len(result.get('deleted') or [])} 项，跳过 {len(result.get('failed') or [])} 项。")
            self._finish_confirmation(token, "completed" if successful else "failed", (title, text),
                                      command_origin=command_origin)
            return result
        except Exception:
            self._finish_confirmation(token, "failed", ("媒体增强", "操作未完成，请核实结果后重新预览；未自动重发提交"),
                                      command_origin=command_origin)
            raise

    def _edit_tool_confirmation_message(self, data, token, kind, state, message=""):
        if not data.get("original_message_id") or not data.get("original_chat_id"):
            return False
        prefix, label, verb = self._tool_confirmation_labels(kind)
        status = f"✅{verb}完成" if state == "completed" else f"⌛️{verb}中" if state in {"processing", "queued"} else \
                 "已取消" if state == "cancelled" else "已过期" if state == "expired" else f"⚠️{verb}未完成"
        try:
            self.chain.post_message(Message(channel=NotificationChannel.Telegram, source=data.get("source"),
                userid=str(data.get("userid") or ""), title=f"【{label}】", text=message or status,
                buttons=[[{"text": status, "callback_data": f"[PLUGIN]EmeTools|{prefix}:{token}:p"}]],
                parse_mode="plain", original_message_id=data["original_message_id"],
                original_chat_id=data["original_chat_id"]))
            return True
        except Exception as error:
            logger.warning("媒体增强 %s：原确认通知状态更新失败（%s），不影响执行状态", label, type(error).__name__)
            return False

    def _handle_tool_confirmation(self, token, approve, data):
        source, user = str(data.get("source") or ""), str(data.get("userid") or "")
        if (not self._enabled or data.get("channel") not in {NotificationChannel.Telegram, NotificationChannel.Telegram.value}
                or not source or not user or str(data.get("original_chat_id") or "") != user
                or not self._file_cleanup_actor_allowed(token, source, user)):
            return
        with self._pending_lock:
            pending = self._pending.get(token)
            if (not pending or pending[1] not in {"trash", "move", "manual_tools", "scheduled_tools"}
                    or source not in self._cleanup_telegram_sources.get(token, set())):
                return
            kind = pending[1]
            references = self._cleanup_message_refs.setdefault(token, [])
            reference = {key: data.get(key) for key in ("source", "userid", "original_chat_id", "original_message_id")}
            identity = lambda item: (str(item.get("source")), str(item.get("original_chat_id")),
                                     str(item.get("original_message_id")))
            if not any(identity(saved) == identity(reference) for saved in references):
                references.append(reference)
        try:
            if not approve:
                if self._set_cleanup_status(token, "cancelled", "本次操作已取消，网页确认同步失效", expected_state="pending"):
                    self._consume(token, kind)
                return
            self._claim_tool_confirmation(token, kind)
            execute = lambda: self._execute_tool_confirmation(token, kind, command_origin=(source, user))
            if kind in {"trash", "move"}:
                self._p115_tasks.run(f"confirm:{token}", self._tool_confirmation_labels(kind)[1], execute, manual=True)
            else:
                execute()
        except HTTPException:
            return
        except (RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
            self._finish_confirmation(token, "failed", ("媒体增强", f"操作未完成（{type(error).__name__}），请核实结果并查看日志；未自动重发提交。"),
                                      command_origin=(source, user))

    async def status(self) -> dict:
        return {"settings": self._settings(), "schedule": copy.deepcopy(self._schedule),
                "last_run": dict(self._last_run), "p115_tasks": self._p115_tasks.status()}

    async def missing_status(self) -> dict:
        if not self._missing._is_scanning:
            self._missing._load_saved_data()
            await asyncio.to_thread(self._missing.backfill_airing_status)
        return {"config": copy.deepcopy(self._missing_config),
                "legacy_enabled": self._legacy_missing_active(),
                "scanning": self._missing._is_scanning,
                "manual_scan_id": self._missing_manual_scan_id,
                "finished_scan_id": self._missing_finished_scan_id,
                "last_scan_time": self._missing._last_scan_time,
                "scan_status": copy.deepcopy(self._missing._scan_status),
                "cancelled_subscriptions": copy.deepcopy(self._missing._cancelled_subscriptions),
                "results": copy.deepcopy(self._missing._results)}

    async def missing_options(self) -> dict:
        servers, libraries, series = await asyncio.to_thread(self._missing._get_form_options)
        return {"servers": servers, "libraries": libraries, "series": series}

    async def missing_fill_status(self) -> dict:
        """查询半自动 Telegram 补全任务，不连接机器人。"""
        return await self._monitor.call(self._fill.status())

    async def missing_fill_action(self, action: dict) -> dict:
        """所有补全操作均在现有用户会话的事件循环中执行。"""
        try:
            return await self._monitor.call(self._fill.action(action))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    async def missing_action(self, action: dict) -> dict:
        operation = action.get("operation")
        skip_series_id = None
        if operation == "skip_series":
            if self._missing._is_scanning:
                raise HTTPException(status_code=409, detail="缺集检测正在扫描，请稍后重试")
            skip_series_id = str(action.get("tmdb_id") or "")
            if not skip_series_id.isdecimal() or not any(
                str(item.get("TmdbId")) == skip_series_id for item in self._missing._results
            ):
                raise HTTPException(status_code=400, detail="剧集不在当前检测结果中，请刷新后重试")
            action = {"config": {"skip_series_ids": [
                *self._missing_config["skip_series_ids"], skip_series_id,
            ]}}
            operation = "save"
        if operation == "save":
            changes = action.get("config")
            if not isinstance(changes, dict) or set(changes) - (set(DEFAULT_MISSING) | {"auto_cancel_completed", "auto_cancel_aired_season"}):
                raise HTTPException(status_code=400, detail="缺集检测配置格式不正确")
            if "auto_cancel_completed" in changes or "auto_cancel_aired_season" in changes:
                changes = normalize_cancel_config(changes)
            updated = {**self._missing_config, **changes}
            updated["enabled"] = bool(updated["enabled"])
            for key in ("only_existing_seasons", "ignore_season_zero", "ignore_future",
                        "auto_cancel_enabled", "auto_episode_correction"):
                updated[key] = bool(updated[key])
            if not isinstance(updated["auto_cancel_mode"], str) or updated["auto_cancel_mode"] not in {"ended", "aired", "ended_or_aired"}:
                raise HTTPException(status_code=400, detail="自动取消判定方式无效")
            if updated["missing_action"] not in {item.value for item in MissingAction}:
                raise HTTPException(status_code=400, detail="缺集检测处理方式无效")
            for key in ("server_names", "library_names", "skip_series_ids"):
                if not isinstance(updated[key], list) or len(updated[key]) > 1000:
                    raise HTTPException(status_code=400, detail=f"{key} 应为多选列表")
                updated[key] = self._missing._parse_names(updated[key])
            if any(not name.isdecimal() for name in updated["skip_series_ids"]):
                raise HTTPException(status_code=400, detail="跳过剧集的 TMDB ID 必须是数字")
            overrides = updated.get("episode_overrides")
            if not isinstance(overrides, list) or len(overrides) > 500:
                raise HTTPException(status_code=400, detail="集数修正配置格式不正确")
            normalized_overrides = []
            seen_overrides = set()
            for item in overrides:
                if not isinstance(item, dict) or set(item) - {"tmdb_id", "season", "total_episodes"}:
                    raise HTTPException(status_code=400, detail="集数修正配置格式不正确")
                tmdb_id = str(item.get("tmdb_id") or "")
                if not tmdb_id.isdecimal():
                    raise HTTPException(status_code=400, detail="集数修正的 TMDB ID 必须是数字")
                try:
                    season = int(item.get("season"))
                    total = int(item.get("total_episodes"))
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail="集数修正的季号和集数必须是整数") from exc
                if not 0 <= season <= 99 or not 1 <= total <= 999:
                    raise HTTPException(status_code=400, detail="集数修正的季号应为 0–99，正确集数应为 1–999")
                key = (tmdb_id, season)
                if key in seen_overrides:
                    raise HTTPException(status_code=400, detail="同一剧集和季度不能重复设置集数修正")
                seen_overrides.add(key)
                normalized_overrides.append({"tmdb_id": tmdb_id, "season": season, "total_episodes": total})
            updated["episode_overrides"] = normalized_overrides
            try:
                CronTrigger.from_crontab(str(updated["cron"]))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"cron 表达式无效：{exc}") from exc
            if updated["enabled"] and self._legacy_missing_active():
                raise HTTPException(status_code=409, detail="请先停用原「剧集缺集检测订阅」插件，再启用 媒体增强缺集定时检测")
            newly_skipped = set(updated["skip_series_ids"]) - set(self._missing_config["skip_series_ids"])
            self._missing_config = updated
            deferred = self._missing.configure(updated)
            self._persist()
            Scheduler().update_plugin_job(self.__class__.__name__)
            if skip_series_id:
                self._missing._results = [item for item in self._missing._results
                                          if str(item.get("TmdbId")) != skip_series_id]
                self.save_data(self._missing._DATA_KEY, self._missing._results)
            if newly_skipped and not skip_series_id and not deferred:
                # The old plugin cancels subscriptions for skipped series on save.
                # Never do this during automatic migration/initialization.
                original = self._missing._skip_series_ids
                try:
                    self._missing._skip_series_ids = newly_skipped
                    await asyncio.to_thread(self._missing._cancel_skipped_subscriptions)
                finally:
                    self._missing._skip_series_ids = original
            return {"message": "配置已保存，本轮检测不受影响；本轮结束后生效，下次检测使用新配置" if deferred else
                    "已跳过该剧集的缺集检测" if skip_series_id else "缺集检测配置已保存",
                    "deferred": bool(deferred),
                    "config": copy.deepcopy(updated)}
        if operation == "scan":
            if self._missing._is_scanning:
                raise HTTPException(status_code=409, detail="缺集检测正在扫描")
            if self._legacy_missing_active() and self._missing_config["missing_action"] == MissingAction.ADD_SUBSCRIBE.value:
                raise HTTPException(status_code=409, detail="原缺集插件仍启用；请先停用它，避免两处同时添加订阅")
            self._missing_manual_scan_id += 1
            scan_id = self._missing_manual_scan_id

            def scan_and_mark_finished():
                try:
                    self._missing.scan_missing_episodes()
                finally:
                    self._missing_finished_scan_id = scan_id

            threading.Thread(target=scan_and_mark_finished, daemon=True).start()
            return {"message": "已开始扫描，检测结果将在完成后自动更新", "scan_id": scan_id}
        if operation == "clear":
            if self._missing._is_scanning:
                raise HTTPException(status_code=409, detail="缺集检测正在扫描")
            self._missing._results = []
            self._missing._cancelled_subscriptions = []
            self._missing._last_scan_time = "从未扫描"
            self._missing._scan_status = {}
            self.save_data("missing_episodes", [])
            self.save_data("missing_cancelled_subscriptions", [])
            self.save_data("last_scan_time", "从未扫描")
            self.save_data("missing_scan_status", {})
            return {"message": "检测记录已清空（原插件记录仍保留）"}
        raise HTTPException(status_code=400, detail="未知缺集检测操作")

    async def save_settings(self, settings: dict) -> dict:
        allowed = {"enabled", "show_sidebar_nav", "strm_root", "rb_password",
                   "tg_api_id", "tg_api_hash", "tg_forward_token", "notifications"}
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
        if "notifications" in settings:
            notification_values = settings["notifications"]
            if not isinstance(notification_values, dict) or set(notification_values) - set(DEFAULT_NOTIFICATIONS):
                raise HTTPException(status_code=400, detail="通知设置包含未知字段")
            self._notifications = {key: bool(notification_values.get(key, default))
                                  for key, default in DEFAULT_NOTIFICATIONS.items()}
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
        logger.info("媒体增强 Telegram：执行%s，监控=%s",
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
            logger.info("媒体增强 Telegram：%s 配置已保存，频道=%d，关键词=%d，黑名单=%d",
                        scope, len(channels), len(keywords), len(blacklist))
            return {"ok": True, "config": copy.deepcopy(self._monitor_config[scope])}
        elif operation in ("start", "stop"):
            if operation == "start" and not self._enabled:
                raise HTTPException(status_code=400, detail="请先启用插件")
            coro = self._monitor.start(scope) if operation == "start" else self._monitor.stop(scope)
        else:
            raise HTTPException(status_code=400, detail="未知监控操作")
        try:
            result = await self._monitor.call(coro)
            logger.info("媒体增强 Telegram：%s %s，结果=%s", scope, operation,
                        "成功" if result.get("ok") else "需要继续验证")
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning(f"媒体增强 Telegram 操作失败: {type(exc).__name__}")
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
                mode = str(rule.get("mode") or "move").strip().lower()
                if mode not in {"move", "copy"}:
                    raise HTTPException(status_code=400, detail="转存方式只能选择移动或复制")
                rules.append({"src_id": source, "src_name": str(rule.get("src_name") or "").strip(),
                              "dst_id": target, "dst_name": str(rule.get("dst_name") or "").strip(),
                              "mode": mode})
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
        logger.info("媒体增强 %s：运行配置已保存，启用=%s，目录/规则=%d",
                    SECTION_NAMES[section], updated["enabled"],
                    len(updated.get("dir_ids") or updated.get("rules") or []))
        return {"saved": True, "message": "配置已保存，定时任务由 MoviePilot 执行",
                "config": copy.deepcopy(updated)}

    def _p115_dirs(self, cid: str):
        with self._client() as client:
            return {"ok": True, "cid": cid, "dirs": client.directories(cid)}

    def _trash_info(self):
        logger.info("媒体增强 清空115回收站：查询当前回收站")
        with self._client() as client:
            info = client.rb_list()
        logger.info("媒体增强 清空115回收站：查询完成，文件=%d，大小=%d 字节",
                    info["count"], sum(int(item.get("file_size") or 0) for item in info["items"]))
        token = self._remember("trash", {"count": info["count"], "items": copy.deepcopy(info["items"])})
        return {"ok": True, "count": info["count"],
                "size_bytes": sum(int(item.get("file_size") or 0) for item in info["items"]),
                "token": token}

    def _trash_clear(self, token: str):
        expected = self._consume(token, "trash")
        logger.info("媒体增强 清空115回收站：开始二次核验，预计文件=%d", expected["count"])

        def clear():
            with self._client() as client:
                current = client.rb_list()
                if (current["count"] != expected["count"] or
                        self._trash_fingerprint(current["items"]) != self._trash_fingerprint(expected["items"])):
                    logger.warning("媒体增强 清空115回收站：内容已变化，原=%d，现=%d，取消清空",
                                   expected["count"], current["count"])
                    return {"ok": False, "message": "回收站内容已变化，请重新查询"}
                if not current["count"]:
                    logger.info("媒体增强 清空115回收站：回收站为空，跳过")
                    return {"ok": True, "message": "回收站为空"}
                result = client.clear_recyclebin(self._rb_password)
                if not result.get("state"):
                    logger.warning("媒体增强 清空115回收站：请求失败，已保留文件（服务端错误未记录以保护凭据）")
                    return {"ok": False, "message": str(result.get("error") or result.get("msg") or "清空失败")}
                logger.info("媒体增强 清空115回收站：成功清空 %d 个文件", current["count"])
                return {"ok": True, "count": current["count"],
                        "size_bytes": sum(int(item.get("file_size") or 0) for item in current["items"]),
                        "message": f"已彻底清空 {current['count']} 个文件"}

        return self._locked("p115_trash", clear)

    def _cleanup_preview(self):
        configured = self._schedule["p115_cleanup"]
        identifiers = list(configured.get("dir_ids") or [])
        if not identifiers:
            logger.info("媒体增强 清理文件：没有配置目录，跳过预览")
            return {"ok": False, "message": "请先保存 115 清理目录"}
        remaining = self._p115_cooldown_remaining()
        if remaining and not P115Client.read_recovery_available_for_cookie(self._source_cookie()):
            message = self._p115_deferred_message(remaining)
            logger.info("媒体增强 清理文件：%s", message)
            names = configured.get("dir_names") or []
            return {
                "ok": False,
                "deferred": True,
                "message": message,
                "file_count": 0,
                "dir_count": 0,
                "total_bytes": 0,
                "folders": [{
                    "cid": cid,
                    "name": (names[index] if index < len(names) else "") or cid,
                    "files": 0,
                    "dirs": 0,
                    "size": 0,
                    "error": message,
                } for index, cid in enumerate(identifiers)],
                "snapshots": {},
            }
        logger.info("媒体增强 清理文件：开始预览 %d 个目录", len(identifiers))
        folders, snapshots = [], {}
        deferred = False
        with self._client() as client:
            client.enable_read_recovery(self._p115_stop)
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
                    logger.info("媒体增强 清理文件：目录 %s（CID %s），文件=%d，子目录=%d",
                                _log_label(name), cid, len(files), len(children))
                except (RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
                    reason = _log_label(error)
                    folders.append({"cid": cid, "name": name, "files": 0, "dirs": 0, "size": 0, "error": reason})
                    logger.warning("媒体增强 清理文件：目录 %s（CID %s）预览失败：%s：%s",
                                   _log_label(name), cid, type(error).__name__, reason)
                    if isinstance(error, P115DirectoryUnavailable):
                        deferred = not error.exhausted
                        names = configured.get("dir_names") or []
                        for position in range(index + 1, len(identifiers)):
                            folders.append({"cid": identifiers[position],
                                            "name": (names[position] if position < len(names) else "") or identifiers[position],
                                            "files": 0, "dirs": 0, "size": 0,
                                            "error": "115 请求已暂停，本轮未读取此目录"})
                        break
        logger.info("媒体增强 清理文件：预览完成，文件=%d，子目录=%d，失败目录=%d",
                    sum(item["files"] for item in folders), sum(item["dirs"] for item in folders),
                    sum(bool(item["error"]) for item in folders))
        complete = len(snapshots) == len(identifiers)
        return {"ok": complete, "deferred": deferred,
                "message": "预览完成" if complete else ("115 网络暂不可用，本轮未执行清理，等待下一轮自动复查"
                                                           if deferred else "部分目录预览失败，本轮未执行任何清理，请稍后重新预览"),
                "folders": folders, "file_count": sum(item["files"] for item in folders),
                "dir_count": sum(item["dirs"] for item in folders), "total_bytes": sum(item["size"] for item in folders),
                "snapshots": snapshots}

    def _cleanup_request(self, command_origin=None):
        preview = self._cleanup_preview()
        if not preview.get("ok"):
            return preview
        if len(preview["snapshots"]) != len(self._schedule["p115_cleanup"]["dir_ids"]):
            logger.warning("媒体增强 清理文件：部分目录预览失败，停止确认流程")
            return {"ok": False, "message": "部分目录读取失败，已停止清理"}
        if not preview["file_count"] and not preview["dir_count"]:
            logger.info("媒体增强 清理文件：目录均为空，无需确认")
            return {"ok": True, "empty": True, "message": "清理目录均为空"}
        snapshot = preview.pop("snapshots")
        token = self._remember("cleanup", snapshot)
        logger.info("媒体增强 清理文件：已生成二次确认，文件=%d，子目录=%d",
                    preview["file_count"], preview["dir_count"])
        try:
            sources = ({command_origin[0]: True} if command_origin else self._telegram_confirmation_sources())
        except (RuntimeError, ValueError, OSError) as error:
            logger.warning("媒体增强 清理文件：Telegram渠道读取失败：%s，保留网页确认", type(error).__name__)
            sources = {}
        buttons = None
        if sources:
            with self._pending_lock:
                self._cleanup_telegram_sources = {key: value for key, value in self._cleanup_telegram_sources.items()
                                                 if key in self._pending}
                self._cleanup_telegram_sources[token] = set(sources)
                self._cleanup_command_origins = {key: value for key, value in self._cleanup_command_origins.items()
                                                 if key in self._pending}
                if command_origin:
                    self._cleanup_command_origins[token] = command_origin
            buttons = [[{"text": "✅ 确认清理", "callback_data": f"[PLUGIN]EmeTools|files:{token}:y"},
                        {"text": "取消", "callback_data": f"[PLUGIN]EmeTools|files:{token}:n"}]]
        try:
            notice = notices.file_cleanup_confirmation(preview, telegram=bool(buttons))
            if command_origin:
                self._media_bot_reply(*command_origin, *notice, buttons=buttons)
            else:
                self._send_tool_notice(*notice, buttons=buttons)
        except Exception as error:
            if command_origin:
                self._consume(token, "cleanup")
                self._set_cleanup_status(token, "failed", "确认通知发送失败，请重新发送 /cleanfiles")
                raise
            logger.warning("媒体增强 清理文件：确认通知发送失败：%s，保留网页确认", type(error).__name__)
        return {"ok": True, "pending": True, "token": token,
                "file_count": preview["file_count"], "dir_count": preview["dir_count"],
                "folders": preview["folders"], "total_bytes": preview.get("total_bytes", 0),
                "message": f"确认将 {preview['file_count']} 个文件及 {preview['dir_count']} 个文件夹移入回收站？"}

    def _cleanup_confirm(self, token: str):
        snapshot = self._consume(token, "cleanup")
        self._set_cleanup_status(token, "processing")
        logger.info("媒体增强 清理文件：开始复核并清理 %d 个目录", len(snapshot))

        def clean():
            if set(snapshot) != set(self._schedule["p115_cleanup"]["dir_ids"]):
                logger.warning("媒体增强 清理文件：目录配置已变化，终止清理")
                return {"ok": False, "message": "清理目录配置已变化，请重新预览"}
            deleted, deleted_dirs, errors, folders = 0, 0, [], []
            with self._client() as client:
                client.enable_read_recovery(self._p115_stop)
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
                            logger.warning("媒体增强 清理文件：目录 %s 内容变化，跳过", _log_label(detail["name"]))
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
                        detail["error"] = f"失败：{_log_label(error)}"
                        errors.append(f"目录 {cid} {detail['error']}")
                        if isinstance(error, P115DirectoryUnavailable):
                            logger.warning("媒体增强 清理文件：115 请求已暂停，保留剩余文件和目录")
                            break
                    logger.info("媒体增强 清理文件：目录 %s 完成，文件=%d，子目录=%d，结果=%s",
                                _log_label(detail["name"]), detail["files"], detail["dirs"],
                                "失败" if detail["error"] else "成功")
            logger.info("媒体增强 清理文件：执行完毕，文件=%d，子目录=%d，失败=%d",
                        deleted, deleted_dirs, len(errors))
            return {"ok": not errors, "deleted": deleted, "dir_count": deleted_dirs,
                    "errors": errors, "folders": folders,
                    "message": f"清理结束：文件 {deleted} 个，文件夹 {deleted_dirs} 个"}

        try:
            result = self._locked("p115_cleanup", clean)
        except Exception as error:
            self._set_cleanup_status(token, "failed", str(error))
            raise
        state = "completed" if result.get("ok") else "failed"
        notice = notices.file_cleanup(result, {"folders": []}) if "deleted" in result else None
        self._set_cleanup_status(token, state, notice[1] if notice else result.get("message", ""))
        return result

    _MOVE_COPY_RECORDS_KEY = "p115_move_copy_records"

    def _move_copy_record_key(self, source, destination):
        return f"copy:{source}:{destination}"

    def _load_move_copy_records(self):
        records = self.get_data(self._MOVE_COPY_RECORDS_KEY)
        if not isinstance(records, dict):
            return {}
        return {
            str(key): {str(item) for item in value if item is not None}
            for key, value in records.items()
            if isinstance(value, (list, tuple, set))
        }

    def _save_move_copy_records(self, records):
        self.save_data(self._MOVE_COPY_RECORDS_KEY, {
            str(key): sorted(str(item) for item in value if item is not None)
            for key, value in records.items()
            if value
        })

    def _reset_move_copy_records(self):
        records = self._load_move_copy_records()
        self.save_data(self._MOVE_COPY_RECORDS_KEY, {})
        logger.info("媒体增强 文件转存：已清理复制记录 %d 条规则", len(records))
        return {"ok": True, "cleared": len(records), "message": "复制记录已清理，下次将重新复制源目录中的全部项目"}

    def _move_info(self, *, snapshot=False):
        rules = copy.deepcopy(self._schedule["p115_move"]["rules"])
        copy_records = self._load_move_copy_records()
        pending = {}
        entries = {}
        if rules:
            with self._client() as client:
                for rule in rules:
                    try:
                        files, folders = client.list_children(rule["src_id"])
                        entries[rule["src_id"]] = self._move_fingerprint(files, folders)
                        identifiers = [str(item["fid"]) for item in files] + [str(item["cid"]) for item in folders]
                        if (rule.get("mode") or "move") == "copy":
                            copied = copy_records.get(self._move_copy_record_key(rule["src_id"], rule["dst_id"]), set())
                            count = sum(identifier not in copied for identifier in identifiers)
                        else:
                            count = len(identifiers)
                        pending[rule["src_id"]] = {"name": rule["src_name"] or rule["src_id"],
                                                    "count": count}
                    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
                        pending[rule["src_id"]] = {"name": rule["src_name"] or rule["src_id"],
                                                    "count": -1, "error": str(error)}
        logger.info("媒体增强 文件转存：待转存查询完成，规则=%d，待转存=%d，查询失败=%d",
                    len(rules), sum(max(0, item["count"]) for item in pending.values()),
                    sum(item["count"] < 0 for item in pending.values()))
        result = {"ok": True, "pending": pending, "rules": rules, "last_run": self._last_run.get("p115_move", "")}
        if snapshot:
            result["entries"] = entries
            result["copy_records"] = copy.deepcopy(copy_records)
        return result

    def _move_run(self, expected=None):
        rules = copy.deepcopy(self._schedule["p115_move"]["rules"])
        if not rules:
            logger.info("媒体增强 文件转存：未配置规则，跳过")
            return {"ok": False, "message": "请先保存转存规则"}
        logger.info("媒体增强 文件转存：开始检查 %d 条转存规则", len(rules))

        def move():
            if expected is not None and rules != expected["rules"]:
                return {"ok": False, "message": "转存规则已变化，请重新预览并确认"}
            if time.monotonic() < getattr(self, "_move_retry_after", 0):
                return {"ok": False, "deferred": True, "moved": 0, "errors": [], "details": [],
                        "message": "115 目录接口暂不可用，退避等待中；5 分钟后自动复查"}
            remaining = self._p115_cooldown_remaining()
            if remaining:
                message = self._p115_deferred_message(remaining)
                logger.info("媒体增强 文件转存：%s", message)
                return {"ok": False, "deferred": True, "moved": 0, "errors": [], "details": [],
                        "message": message}
            moved, errors, details, copy_records = 0, [], [], self._load_move_copy_records()
            if expected is not None and copy_records != expected["copy_records"]:
                return {"ok": False, "message": "增量复制记录已变化，请重新预览并确认"}
            with self._client() as client:
                if expected is not None:
                    for source, fingerprint in expected["entries"].items():
                        files, directories = client.list_children(source)
                        if self._move_fingerprint(files, directories) != fingerprint:
                            return {"ok": False, "message": "源目录内容已变化，请重新预览并确认"}
                for rule in rules:
                    source, destination = rule["src_id"], rule["dst_id"]
                    mode = rule.get("mode") or "move"
                    action = client.copy_files if mode == "copy" else client.move_files
                    detail = {"src_id": source, "dst_id": destination, "mode": mode,
                              "src_name": rule.get("src_name"), "dst_name": rule.get("dst_name"),
                              "status": "empty", "item_count": 0, "skipped_count": 0,
                              "file_count": 0, "total_bytes": 0}
                    details.append(detail)
                    stage = "读取源目录"
                    try:
                        files, directories = client.list_children(source)
                        if expected is not None and self._move_fingerprint(files, directories) != expected["entries"][source]:
                            raise ValueError("源目录内容已变化，已停止此规则，请重新预览")
                        identifiers = [item["fid"] for item in files] + [item["cid"] for item in directories]
                        logger.info("媒体增强 文件转存：%s %s → %s，源文件=%d，源目录=%d",
                                    "复制" if mode == "copy" else "移动",
                                    _log_label(detail["src_name"] or source),
                                    _log_label(detail["dst_name"] or destination), len(files), len(directories))
                        if not identifiers:
                            logger.info("媒体增强 文件转存：%s 源目录为空，跳过",
                                        _log_label(detail["src_name"] or source))
                            continue
                        if mode == "copy":
                            record_key = self._move_copy_record_key(source, destination)
                            copied_ids = copy_records.setdefault(record_key, set())
                            pending_identifiers = [identifier for identifier in identifiers
                                                   if str(identifier) not in copied_ids]
                            detail["skipped_count"] = len(identifiers) - len(pending_identifiers)
                            identifiers = pending_identifiers
                            if not identifiers:
                                detail["status"] = "unchanged"
                                logger.info("媒体增强 文件转存：复制规则 %s → %s 没有新增项目，跳过 %d 项",
                                            _log_label(detail["src_name"] or source),
                                            _log_label(detail["dst_name"] or destination),
                                            detail["skipped_count"])
                                continue
                        moved_ids = set()
                        for index in range(0, len(identifiers), 500):
                            batch = identifiers[index:index + 500]
                            stage = "复制提交" if mode == "copy" else "移动提交"
                            result = action(batch, destination)
                            if result.get("state"):
                                moved += len(batch)
                                moved_ids.update(batch)
                                detail["item_count"] = len(moved_ids)
                                if mode == "copy":
                                    copied_ids.update(str(identifier) for identifier in batch)
                                    self._save_move_copy_records(copy_records)
                            else:
                                errors.append(f"{source} → {destination}：{result.get('error') or result.get('msg')}")
                                detail["status"] = "failed"
                                logger.warning("媒体增强 文件转存：%s %s → %s 失败，批次=%d",
                                               "复制" if mode == "copy" else "移动",
                                               _log_label(detail["src_name"] or source),
                                               _log_label(detail["dst_name"] or destination), len(batch))
                                break
                        if moved_ids:
                            detail["item_count"] = len(moved_ids)
                            moved_files = [item for item in files if item["fid"] in moved_ids]
                            moved_dirs = [item for item in directories if item["cid"] in moved_ids]
                            count, size = self._move_tree_stats(client, moved_dirs)
                            detail["file_count"] = len(moved_files) + count
                            detail["total_bytes"] = sum(int(item.get("size") or 0) for item in moved_files) + size
                            if detail["status"] != "failed":
                                detail["status"] = "success"
                            logger.info("媒体增强 文件转存：%s %s → %s 已处理 %d 项（包含 %d 个文件），大小=%d 字节",
                                        "复制" if mode == "copy" else "移动",
                                        _log_label(detail["src_name"] or source),
                                        _log_label(detail["dst_name"] or destination), len(moved_ids),
                                        detail["file_count"], detail["total_bytes"])
                    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
                        reason = _log_label(error)
                        errors.append(f"{source} → {destination}：{stage}失败：{reason}")
                        detail["status"] = "failed"
                        detail["error"] = f"{stage}：{reason}"
                        logger.warning("媒体增强 文件转存：%s → %s %s异常：%s：%s",
                                       _log_label(detail["src_name"] or source),
                                       _log_label(detail["dst_name"] or destination), stage, type(error).__name__, reason)
                        if isinstance(error, P115DirectoryUnavailable):
                            self._move_retry_after = time.monotonic() + max(60, self._p115_cooldown_remaining())
                            break
            self._last_run["p115_move"] = datetime.now().isoformat()
            logger.info("媒体增强 文件转存：本次完成，已处理=%d，失败=%d，空目录=%d",
                        moved, len(errors), sum(item["status"] == "empty" for item in details))
            copied = sum(item.get("item_count", 0) for item in details
                         if item.get("mode") == "copy")
            moved_count = sum(item.get("item_count", 0) for item in details
                              if item.get("mode") != "copy")
            skipped = sum(item.get("skipped_count", 0) for item in details if item.get("mode") == "copy")
            return {"ok": not errors, "moved": moved, "moved_count": moved_count, "copied": copied,
                    "skipped": skipped,
                    "errors": errors, "details": details,
                    "message": f"转存完成：移动 {moved_count} 项，复制新增 {copied} 项，跳过已复制 {skipped} 项，失败 {len(errors)} 条"}

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
                if isinstance(error, P115DirectoryUnavailable):
                    raise
                logger.warning("媒体增强统计目录 %s 大小失败：%s：%s", cid, type(error).__name__, _log_label(error))
                size += known_size
        return count, size

    def _send_tool_notice(self, title: str, text: str, buttons=None) -> None:
        content = f"{title or ''}\n{text or ''}"
        category = "cleanup" if "清理文件" in content else \
            "move" if "转存" in content or "文件转存" in content else \
            "trash" if "回收站" in content else \
            "invalid" if "无效数据" in content else \
            "media" if "低质版本" in content or "去重" in content else \
            "missing" if "缺集" in content else \
            "subscription" if "订阅" in content else \
            "enrichment" if "补全" in content else "media"
        if not self._notifications.get("enabled", True) or not self._notifications.get(category, True):
            logger.info("媒体增强：通知已按设置跳过，类别=%s，标题=%s", category, _log_label(title or "无标题"))
            return
        has_failure = bool(re.search(r"失败(?!\s*0)", content)) or any(marker in content for marker in
            ("错误", "异常", "跳过", "未配置", "超时", "未完成"))
        if self._notifications.get("only_failures") and not has_failure and "待确认" not in content:
            logger.info("媒体增强：仅失败通知已启用，跳过成功通知，类别=%s", category)
            return
        if self._send_tracked_cleanup_confirmation(title, text, buttons):
            return
        # Skip _PluginBase.post_message's automatic "查看详情" plugin link: EME's
        # Telegram notification contains only its configured title and body.
        self.chain.post_message(Message(channel=NotificationChannel.Telegram, mtype=MessageType.Plugin,
                                        title=title or None, text=text, link=None, buttons=buttons,
                                        parse_mode="plain" if buttons else None))
        logger.info("媒体增强：已提交 MP 插件类型通知，标题=%s（由通知渠道决定实际接收 Bot）",
                    _log_label(title or "无标题"))

    @staticmethod
    def _telegram_confirmation_sources() -> dict:
        """Only bots with an explicit Plugin subscription and an admin may approve cleanup."""
        return {conf.name: conf.config for conf in get_service_configs(SystemConfigKey.Notifications, NotificationConf)
                if conf.enabled and str(conf.type).lower() == "telegram" and conf.name
                and MessageType.Plugin.value in (conf.switchs or []) and conf.config
                and (conf.config.get("TELEGRAM_ADMINS") or conf.config.get("TELEGRAM_CHAT_ID"))}

    def _send_tracked_cleanup_confirmation(self, title, text, buttons, *, source=None, user=None):
        callback = buttons[0][0].get("callback_data", "") if buttons and buttons[0] else ""
        match = re.fullmatch(r"\[PLUGIN\]EmeTools\|(?:files|trash|move|data|media):([a-f0-9]{32}):y", callback)
        if not match:
            return False
        token = match.group(1)
        try:
            sources = {source: True} if source else self._telegram_confirmation_sources()
            helper = NotificationHelper()
            clients = {name: helper.get_service(name=name) for name in sources}
            clients = {name: service.instance for name, service in clients.items()
                       if service and service.instance}
        except Exception as error:
            logger.warning("媒体增强 确认通知：无法读取通知实例（%s），使用原通知渠道", type(error).__name__)
            return False
        if not clients:
            return False
        with self._pending_lock:
            self._cleanup_message_refs = {key: refs for key, refs in self._cleanup_message_refs.items()
                                          if key in self._pending or time.monotonic() -
                                          self._cleanup_status.get(key, {}).get("updated", 0) < 3600}
        for name, client in clients.items():
            try:
                sent = client.send_msg(title=title, text=text, buttons=buttons, userid=user,
                                       parse_mode="plain", private_delivery=bool(user))
                if not isinstance(sent, dict) or not sent.get("success"):
                    raise ValueError("确认通知未确认发送成功")
                if sent.get("message_id") and sent.get("chat_id"):
                    with self._pending_lock:
                        self._cleanup_message_refs.setdefault(token, []).append({"source": name,
                            "userid": str(user or sent["chat_id"]), "original_chat_id": str(sent["chat_id"]),
                            "original_message_id": sent["message_id"]})
                else:
                    logger.warning("媒体增强 确认通知：通知渠道未返回消息ID，网页状态无法同步到原通知")
            except Exception as error:
                logger.warning("媒体增强 确认通知：发送未确认（%s），不重发，保留网页确认", type(error).__name__)
                if source:
                    raise RuntimeError("操作确认通知发送未确认") from None
        return True

    def _send_scheduled_telegram_confirmation(self, token: str, snapshot: dict) -> None:
        text = (f"扫描目录：{_log_label(snapshot['root'])}\n待清理：{len(snapshot['items'])} 项\n"
                "确认有效期：发送后 30 分钟；重启后失效。二次核验后移入隔离区，不会立即永久删除。")
        preview = "\n".join("• " + _log_label(os.path.basename(item["path"])) for item in snapshot["items"][:8])
        self._publish_tool_confirmation(token, "scheduled_tools", f"{text}\n{preview}")
        logger.info("媒体增强 清理数据：已提交 Telegram 按钮确认，候选=%d", len(snapshot["items"]))

    @eventmanager.register(EventType.MessageAction)
    def scheduled_confirmation_action(self, event: Event) -> None:
        data = event.event_data if event else None
        if not data or str(data.get("plugin_id") or "").lower() != "emetools":
            return
        shared_match = re.fullmatch(r"(?:trash|move|data):([a-f0-9]{32}):([ynp])", str(data.get("text") or ""))
        if shared_match:
            if shared_match.group(2) != "p":
                threading.Thread(target=self._handle_tool_confirmation,
                                 args=(shared_match.group(1), shared_match.group(2) == "y", data.copy()), daemon=True).start()
            return
        files_match = re.fullmatch(r"files:([a-f0-9]{32}):([ynp])", str(data.get("text") or ""))
        if files_match and self._enabled:
            if files_match.group(2) == "p":
                return
            threading.Thread(target=self._handle_file_cleanup_confirmation,
                             args=(files_match.group(1), files_match.group(2) == "y", data.copy()), daemon=True).start()
            return
        media_match = re.fullmatch(r"media:([a-f0-9]{32}):([ynp])", str(data.get("text") or ""))
        if media_match:
            if media_match.group(2) == "p":
                return
            threading.Thread(target=self._handle_media_bot_confirmation,
                             args=(media_match.group(1), media_match.group(2) == "y", data.copy()), daemon=True).start()
            return

    def _edit_file_cleanup_message(self, data: dict, token: str, text: str, processing: bool = False,
                                   completed: bool = False) -> bool:
        message_id = data.get("original_message_id")
        chat_id = data.get("original_chat_id")
        if not message_id or not chat_id:
            return False
        status_text = "🗑️清理完成" if completed else "⌛️清理中" if processing else "清理已结束"
        buttons = [[{"text": status_text, "callback_data": f"[PLUGIN]EmeTools|files:{token}:p"}]] if status_text else None
        try:
            self.chain.post_message(Message(channel=NotificationChannel.Telegram, source=data.get("source"),
                                        userid=str(data.get("userid") or ""), title="⚠️【清理文件】",
                                        text=text, buttons=buttons, parse_mode="plain" if buttons else None,
                                        original_message_id=message_id, original_chat_id=chat_id))
            return True
        except Exception as error:
            logger.warning("媒体增强 清理文件：更新原通知失败：%s，不影响清理状态", type(error).__name__)
            return False

    def _handle_file_cleanup_confirmation(self, token: str, action: str | bool, data: dict) -> None:
        if isinstance(action, bool):
            action = "y" if action else "n"
        source, user = str(data.get("source") or ""), str(data.get("userid") or "")
        if (not self._enabled or data.get("channel") not in {NotificationChannel.Telegram, NotificationChannel.Telegram.value}
                or not source or not user or str(data.get("original_chat_id") or "") != user
                or not self._file_cleanup_actor_allowed(token, source, user)):
            logger.warning("媒体增强 清理文件：Telegram确认被拒绝（需要已配置Bot的管理员私聊）")
            return
        try:
            with self._pending_lock:
                if self._cleanup_status.get(token, {}).get("state") in {"completed", "cancelled", "failed"}:
                    return
                pending = self._pending.get(token)
                if (not pending or pending[1] != "cleanup" or time.monotonic() - pending[0] >= 1800
                        or source not in self._cleanup_telegram_sources.get(token, set())):
                    raise ValueError("确认已过期、已处理或Bot不匹配，请重新预览")
            self._register_confirmation_message(token, data)
            if action == "p":
                return
            if action == "n":
                self._consume(token, "cleanup")
                self._set_cleanup_status(token, "cancelled", "已取消本次文件清理；网页中的同一确认也已失效。")
                return
            if not self._set_cleanup_status(token, "processing", "已提交清理确认，正在排队复核目录",
                                            expected_state="pending"):
                return

            def confirm():
                if not self._enabled:
                    return {"ok": False, "message": "插件已停用，未执行清理"}
                if not self._file_cleanup_actor_allowed(token, source, user):
                    return {"ok": False, "message": "权限配置已变化，未执行清理"}
                return self._cleanup_confirm(token)

            result = self._p115_tasks.run("cleanup_confirm:" + token, "Telegram确认清理文件", confirm, manual=True)
            if result.get("queued"):
                return
            notice = notices.file_cleanup(result, {"folders": []}) if "deleted" in result else None
            title, text = notice or ("媒体增强", result.get("message") or "清理已结束")
            self._finish_confirmation(token, "completed" if result.get("ok") else "failed", (title, text),
                                      command_origin=(source, user))
        except (HTTPException, RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
            logger.warning("媒体增强 清理文件：Telegram确认未完成：%s", type(error).__name__)
            self._finish_confirmation(token, "failed", ("媒体增强", "确认已过期、已处理或执行失败，请重新预览并检查日志；未自动重发删除请求。"),
                                      command_origin=(source, user))

    def _handle_scheduled_confirmation(self, token: str, approve: bool, data: dict) -> None:
        self._handle_tool_confirmation(token, approve, data)

    @staticmethod
    def _log_scan(snapshot: dict) -> None:
        items = snapshot.get("items") or []
        logger.info("媒体增强 清理无效数据：扫描完成，目录=%s，检查目录=%d，候选=%d，读取错误=%d",
                    _log_label(snapshot.get("root")), snapshot.get("total", 0), len(items),
                    len(snapshot.get("errors") or []))
        for item in items[:50]:
            logger.info("媒体增强 清理无效数据：候选 %s，类型=%s，包含文件=%d，原因=%s",
                        _log_label(item.get("path") or item.get("name")),
                        _log_label(item.get("kind")), item.get("files", 0), _log_label(item.get("reason")))
        if len(items) > 50:
            logger.info("媒体增强 清理无效数据：另有 %d 个候选未逐条显示", len(items) - 50)

    @staticmethod
    def _log_quarantine(result: dict) -> None:
        logger.info("媒体增强 清理无效数据：隔离结果，成功=%d，失败=%d，状态=%s",
                    len(result.get("deleted") or []), len(result.get("failed") or []), result.get("ok"))
        for item in (result.get("deleted") or [])[:50]:
            logger.info("媒体增强 清理无效数据：已隔离 %s", _log_label(item.get("path")))
        for item in (result.get("failed") or [])[:50]:
            logger.warning("媒体增强 清理无效数据：未清理 %s，原因=%s",
                           _log_label(item.get("path")), _log_label(item.get("message")))

    def _run_scheduled(self, section: str) -> None:
        if section == "tools":
            return self._run_scheduled_serial(section)
        return self._p115_tasks.run(section, f"定时{SECTION_NAMES.get(section, section)}",
                                    lambda: self._run_scheduled_serial(section))

    def _run_scheduled_serial(self, section: str) -> None:
        if not self._enabled or not self._schedule.get(section, {}).get("enabled"):
            return
        label = SECTION_NAMES.get(section, section)
        logger.info("媒体增强 %s：定时任务开始", label)
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
                            logger.warning("媒体增强 清理数据：Telegram 确认渠道已失效，保留待办并改发 MoviePilot 页面确认通知")
                            notification = notices.invalid_confirmation(snapshot)
                        else:
                            try:
                                self._send_scheduled_telegram_confirmation(token, snapshot)
                            except Exception as exc:
                                logger.warning("媒体增强 清理数据：Telegram 确认发送失败：%s，保留页面待办", type(exc).__name__)
                                notification = notices.invalid_confirmation(snapshot)
                    else:
                        logger.info("媒体增强 清理数据：%d 项等待插件页面二次确认", count)
                        notification = notices.invalid_confirmation(snapshot)
                else:
                    logger.info("媒体增强 清理无效数据：无项目或自动清理已关闭，未执行隔离")
            elif section == "p115_cleanup":
                if not self._source_cookie():
                    logger.warning("媒体增强 清理文件：跳过，未配置 115 Cookie")
                    notification = ("", "⏰ 115 定时清理跳过：未配置 115 Cookie")
                else:
                    preview = self._cleanup_preview()
                    if not preview.get("ok"):
                        if preview.get("deferred"):
                            logger.info("媒体增强 清理文件：115 正在退避，本轮不发送重复失败通知")
                        else:
                            logger.warning("媒体增强 清理文件：目录预览未完整通过，本轮未执行任何清理")
                            notification = notices.file_cleanup({}, preview)
                    elif preview.get("file_count") or preview.get("dir_count"):
                        result = self._cleanup_confirm(self._remember("cleanup", preview["snapshots"]))
                        if "deleted" in result:
                            notification = notices.file_cleanup(result, preview)
                    elif any(item.get("error") for item in preview.get("folders") or []):
                        notification = notices.file_cleanup({}, preview)
                    else:
                        logger.info("媒体增强 清理文件：所有目录为空，本次不清理")
            elif section == "p115_trash":
                if not self._source_cookie():
                    logger.warning("媒体增强 清空115回收站：跳过，未配置 115 Cookie")
                    notification = ("", "⏰ 115 回收站清空跳过：未配置 115 Cookie")
                else:
                    info = self._trash_info()
                    if info.get("count"):
                        result = self._trash_clear(info["token"])
                        if result.get("ok"):
                            notification = notices.empty_trash(info)
                        else:
                            logger.warning("媒体增强 清空115回收站：清空失败")
                            notification = ("", f"⏰ 115 回收站定时清空失败：{result.get('message') or '未知错误'}")
                    else:
                        logger.info("媒体增强 清空115回收站：回收站为空，本次不执行")
            else:
                if not self._source_cookie():
                    logger.warning("媒体增强 文件转存：跳过，未配置 115 Cookie")
                    notification = ("", "⏰ 115 监控转存跳过：未配置 115 Cookie")
                elif not self._schedule[section].get("rules"):
                    logger.warning("媒体增强 文件转存：跳过，未配置转存规则")
                    notification = ("", "⏰ 115 监控转存跳过：未配置源/目标文件夹")
                else:
                    result = self._move_run()
                    if "moved" in result and not result.get("deferred"):
                        notification = notices.file_move(result)
            self._last_run[section] = datetime.now().isoformat()
            if notification:
                self._send_tool_notice(*notification)
            logger.info("媒体增强 %s：定时任务结束，通知=%s", label, bool(notification))
        except Exception as error:
            logger.error("媒体增强 %s：定时任务异常：%s（敏感详情未记录）", label, type(error).__name__)

    def _pending_info(self):
        with self._pending_lock:
            now = time.monotonic()
            expired = [token for token, status in self._cleanup_status.items()
                       if status.get("kind", "cleanup") != "cleanup" and status["state"] == "pending"
                       and (token not in self._pending or now - self._pending[token][0] >= 1800)]
        for token in expired:
            self._set_cleanup_status(token, "expired", "确认已过期，请重新预览", expected_state="pending")
        with self._pending_lock:
            now = time.monotonic()
            confirmations = []
            for token, status in self._cleanup_status.items():
                kind = status.get("kind", "cleanup")
                if kind in {"cleanup", "media_bot"} or now - status["updated"] >= 3600:
                    continue
                saved = self._pending.get(token)
                state = status["state"]
                if state == "pending" and (not saved or now - saved[0] >= 1800):
                    state = "expired"
                item = {"token": token, "kind": kind, "state": state, "message": status.get("message", "")}
                if saved and state in {"pending", "processing"}:
                    snapshot = saved[2]
                    if kind == "trash":
                        item.update(count=snapshot["count"], ok=True)
                    elif kind == "move":
                        item.update(snapshot["preview"])
                    elif kind in {"manual_tools", "scheduled_tools"}:
                        item["count"] = len(snapshot.get("paths", snapshot.get("items", [])))
                confirmations.append(item)
            return {"ok": True, "confirmations": confirmations, "pending": [{"token": token, "kind": kind, "count": len(snapshot.get("items", []))}
                                           for token, (created, kind, snapshot) in self._pending.items()
                                           if kind == "scheduled_tools" and now - created < 1800
                                           and self._cleanup_status.get(token, {}).get("state") == "pending"]}

    def _confirm_scheduled_tools(self, token: str, notify: bool = True):
        snapshot = self._consume(token, "scheduled_tools")
        logger.info("媒体增强 清理无效数据：确认定时任务待办，候选=%d", len(snapshot["items"]))
        result = self._locked("tools", lambda: self._cleaner.quarantine(snapshot, [item["path"] for item in snapshot["items"]]))
        self._log_quarantine(result)
        if notify and result.get("ok") and (result.get("deleted") or result.get("failed")):
            title, text = notices.invalid_cleanup(result, snapshot["root"])
            self._send_tool_notice(title, text)
        return result

    async def action(self, action: ToolAction) -> dict:
        if action.operation == "p115_task_status":
            return self._p115_tasks.job_status(action.token)
        if action.operation == "p115_tasks":
            return {"tasks": self._p115_tasks.status()}
        confirmation_kinds = {"trash_clear": "trash", "move_confirm": "move",
                              "confirm_delete": "manual_tools", "confirm_scheduled": "scheduled_tools"}
        if action.operation in confirmation_kinds:
            await asyncio.to_thread(self._claim_tool_confirmation, action.token, confirmation_kinds[action.operation])
        labels = {"p115_dirs": "浏览115目录", "trash_info": "查询回收站", "trash_request": "预览回收站清空", "trash_clear": "清空回收站",
                  "cleanup_preview": "预览清理文件", "cleanup_request": "准备清理确认",
                  "cleanup_confirm": "确认清理文件", "move_info": "查询待转存", "move_run": "预览文件转存",
                  "move_request": "预览文件转存", "move_confirm": "确认文件转存",
                  "move_copy_reset": "清理复制记录"}
        if action.operation not in labels:
            return await self._action_serial(action)
        saved = action.model_copy(deep=True)
        key = action.operation + (":" + action.cid if action.operation == "p115_dirs" else "")
        if action.operation in {"trash_clear", "move_confirm"}:
            key = "confirm:" + action.token
        elif action.operation == "cleanup_confirm":
            key += ":" + action.token
        try:
            job = self._p115_tasks.submit(key, labels[action.operation], lambda: asyncio.run(self._action_serial(saved)))
        except Exception:
            if action.operation in confirmation_kinds:
                await asyncio.to_thread(self._set_cleanup_status, action.token, "failed", "任务队列未接受本次操作，请重新预览")
            raise
        if action.operation == "cleanup_confirm":
            await asyncio.to_thread(self._set_cleanup_status, action.token, "processing",
                                    "已提交清理确认，正在排队复核目录", expected_state="pending")
        return await self._p115_submission(job)

    async def _p115_submission(self, job: dict) -> dict:
        await asyncio.to_thread(job["done"].wait, 0.05)
        status = self._p115_tasks.job_status(job["id"])
        if status["state"] in {"completed", "failed"}:
            return status.get("result") or {"ok": False, "message": status["message"]}
        return {"ok": True, "queued": True, "job_id": job["id"], "message": status["message"]}

    async def _action_serial(self, action: ToolAction) -> dict:
        operation = action.operation
        if operation not in {"dirs", "root_dirs", "p115_dirs", "pending", "move_info", "move_copy_reset"}:
            logger.info("媒体增强：页面操作 %s 开始", _log_label(operation))
        try:
            confirmation_kinds = {"trash_clear": "trash", "move_confirm": "move",
                                  "confirm_delete": "manual_tools", "confirm_scheduled": "scheduled_tools"}
            if operation in confirmation_kinds:
                if self._get_cleanup_status(action.token)["state"] == "pending":
                    self._claim_tool_confirmation(action.token, confirmation_kinds[operation])
                return await asyncio.to_thread(self._execute_tool_confirmation, action.token, confirmation_kinds[operation])
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
                return await asyncio.to_thread(self._data_request, {"root": root, "scan_token": action.scan_token}, action.paths)
            if operation == "pending":
                return await asyncio.to_thread(self._pending_info)
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
            if operation == "trash_request":
                return await asyncio.to_thread(self._trash_request)
            if operation == "cleanup_preview":
                preview = await asyncio.to_thread(self._cleanup_preview)
                preview.pop("snapshots", None)
                return preview
            if operation in {"cleanup_status", "confirmation_status"}:
                return self._get_cleanup_status(action.token)
            if operation == "confirmation_cancel":
                status = self._get_cleanup_status(action.token)
                if status["state"] != "pending":
                    return {"ok": False, "message": "确认已处理或已过期"}
                if not self._set_cleanup_status(action.token, "cancelled", "本次操作已取消", expected_state="pending"):
                    return {"ok": False, "message": "操作已开始，不能取消"}
                with self._pending_lock:
                    self._pending.pop(action.token, None)
                    self._cleanup_telegram_sources.pop(action.token, None)
                    self._cleanup_command_origins.pop(action.token, None)
                return {"ok": True, "message": "本次操作已取消，TG 确认同步失效"}
            if operation == "cleanup_request":
                return await asyncio.to_thread(self._cleanup_request)
            if operation == "cleanup_confirm":
                self._set_cleanup_status(action.token, "processing", "正在复核目录并清理")
                result = await asyncio.to_thread(self._cleanup_confirm, action.token)
                notification = notices.file_cleanup(result, {"folders": []}) if "deleted" in result else None
                self._finish_confirmation(action.token, "completed" if result.get("ok") else "failed",
                    notification or ("媒体增强 · 清理文件", result.get("message") or "清理已结束"))
                return result
            if operation == "move_info":
                return await asyncio.to_thread(self._move_info)
            if operation == "move_copy_reset":
                return await asyncio.to_thread(self._locked, "p115_move", self._reset_move_copy_records)
            if operation in {"move_request", "move_run"}:
                return await asyncio.to_thread(self._move_request)
            raise HTTPException(status_code=400, detail="未知的工具操作")
        except HTTPException:
            if operation == "cleanup_confirm":
                self._finish_confirmation(action.token, "failed", ("媒体增强 · 清理文件", "确认已过期或已处理，请重新预览"))
            raise
        except (OSError, RuntimeError, ValueError, httpx.HTTPError) as error:
            if operation == "cleanup_confirm":
                self._finish_confirmation(action.token, "failed", ("媒体增强 · 清理文件", "清理未完成，请核实结果并检查日志；未自动重发删除请求。"))
            logger.warning("媒体增强：页面操作 %s 失败：%s（敏感详情未记录）",
                           _log_label(operation), type(error).__name__)
            return {"ok": False, "message": str(error)}

    async def enrichment_status(self) -> dict:
        return {**self._enrichment.status(), "preview_force": self._enrichment_config.get("preview_force", False)}

    async def enrichment_config(self) -> dict:
        return {"config": copy.deepcopy(self._enrichment_config),
                "ai_model": str(llm_setting("LLM_MODEL") or ""),
                "ai_available": llm_available()}

    async def save_enrichment_config(self, values: dict) -> dict:
        if not self._enabled:
            raise HTTPException(status_code=409, detail="请先启用插件")
        if self._enrichment.status()["running"]:
            raise HTTPException(status_code=409, detail="数据补全运行中，请完成后再保存设置")
        try:
            config = validate_enrich_config(values)
            config["preview_force"] = self._enrichment_config.get("preview_force", False)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if config["ai_enabled"] and not llm_available():
            raise HTTPException(status_code=400, detail="请先在 MoviePilot 配置 LLM 服务的认证与模型")
        self._enrichment_config = config
        self._persist()
        return {"config": copy.deepcopy(config)}

    async def enrichment_action(self, action: EnrichmentAction) -> dict:
        if not self._enabled:
            raise HTTPException(status_code=409, detail="请先启用插件")
        try:
            if action.operation == "metadata_source":
                if action.metadata_source not in ("tmdb", "douban"):
                    raise ValueError("元数据来源仅支持 TMDB 或豆瓣")
                if self._enrichment.status()["running"]:
                    raise ValueError("请等待当前数据补全任务结束再切换来源")
                previous = dict(self._enrichment_config)
                self._enrichment_config = {**previous, "metadata_source": action.metadata_source}
                try:
                    self._persist()
                except Exception:
                    self._enrichment_config = previous
                    raise
                return {"metadata_source": action.metadata_source}
            if action.operation == "preview_preferences":
                previous = dict(self._enrichment_config)
                self._enrichment_config = {**previous, "preview_force": action.force}
                try:
                    self._persist()
                except Exception:
                    self._enrichment_config = previous
                    raise
                return {"preview_force": action.force}
            if action.operation == "search":
                return {"items": await self._enrichment.search(action.keyword)}
            if action.operation == "series_list":
                return {"items": await self._enrichment.list_series()}
            if action.operation == "tv_libraries":
                return {"items": self._enrichment.tv_libraries()}
            if action.operation == "enrich":
                return self._enrichment.start_enrich(action.series_id, action.mode)
            if action.operation == "batch_enrich":
                return self._enrichment.start_library_enrich(action.library_ids)
            if action.operation == "overview_preview":
                return self._enrichment.start_overview_preview(action.series_id, action.library_ids)
            if action.operation == "repair_anomalies":
                return self._enrichment.start_repair_anomalies(action.series_id, action.allow_mediainfo)
            if action.operation == "mediainfo_check":
                return self._enrichment.start_mediainfo_check()
            if action.operation == "mediainfo_fill":
                return self._enrichment.start_mediainfo_fill()
            if action.operation == "preview_scan":
                if self._enrichment.status()["running"]:
                    raise ValueError("请等待当前补全任务结束后再扫描分集")
                return {"episodes": await self._enrichment.scan_preview(action.series_id)}
            if action.operation in {"preview_repair", "preview_size"}:
                return self._enrichment.start_preview_repair(
                    action.series_id, action.episode_ids, action.force,
                    dimensions_only=action.operation == "preview_size")
            if action.operation == "batch_preview":
                return self._enrichment.start_all_preview()
            raise HTTPException(status_code=400, detail="未知的数据补全操作")
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except Exception as error:
            logger.error("媒体增强 数据补全操作失败：%s：%s：%s",
                         action.operation, type(error).__name__, str(error)[:160])
            detail = str(error).strip()[:160]
            raise HTTPException(
                status_code=500,
                detail=f"数据补全操作失败：{type(error).__name__}{'：' + detail if detail else ''}",
            ) from error

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
            {"path": "/missing/fill/status", "endpoint": self.missing_fill_status, "methods": ["GET"], "auth": "bear", "summary": "TG 缺集补全任务"},
            {"path": "/missing/fill/action", "endpoint": self.missing_fill_action, "methods": ["POST"], "auth": "bear", "summary": "TG 缺集补全确认操作"},
            {"path": "/media-cleanup/status", "endpoint": self.media_status, "methods": ["GET"], "auth": "bear", "summary": "媒体清理状态"},
            {"path": "/media-cleanup/libraries", "endpoint": self.media_libraries, "methods": ["GET"], "auth": "bear", "summary": "媒体清理媒体库"},
            {"path": "/media-cleanup/action", "endpoint": self.media_action, "methods": ["POST"], "auth": "bear", "summary": "媒体清理操作"},
            {"path": "/enrichment/status", "endpoint": self.enrichment_status, "methods": ["GET"], "auth": "bear", "summary": "数据补全任务状态"},
            {"path": "/enrichment/config", "endpoint": self.enrichment_config, "methods": ["GET"], "auth": "bear", "summary": "补全设置"},
            {"path": "/enrichment/config", "endpoint": self.save_enrichment_config, "methods": ["POST"], "auth": "bear", "summary": "保存补全设置"},
            {"path": "/enrichment/action", "endpoint": self.enrichment_action, "methods": ["POST"], "auth": "bear", "summary": "数据补全操作"},
        ]
