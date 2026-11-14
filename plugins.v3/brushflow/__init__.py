import base64
import copy
import json
import re
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Union
from unicodedata import normalize
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup
from fastapi import Query

from app import schemas
from app.application.torrent.download import TorrentHelper
from app.api.endpoints.plugin import register_plugin_api
from app.chain.download import DownloadChain
from app.chain.tmdb import TmdbChain
from app.chain.torrents import TorrentsChain
from app.sdk.cache import cached, fresh
from app.sdk.config import settings
from app.sdk.media import MediaInfo
from app.sdk.events import Event, eventmanager
from app.sdk.media import MetaInfo
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.site import SiteOper
from app.db.oper.subscribe import SubscribeOper
from app.sdk.services import DownloaderHelper
from app.sdk.network import SitesHelper
from app.runtime.thread import ThreadHelper
from app.sdk.logging import logger
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.plugins import _PluginBase
from app.scheduler import Scheduler
from app.schemas import MediaType, NotificationType, ServiceInfo, TorrentInfo
from app.schemas.query import DownloadHistoryFilter, QueryPageRequest
from app.schemas.types import EventType
from app.sdk.network import RequestUtils
from app.sdk.utilities import StringUtils

from .models import BrushFlowSettingsPayload, BrushTaskPayload, BrushTaskStatePayload
from .models import (
    ChannelDedupSettingsPayload, ResourceLedgerDeletePayload, migrated_identity_policy, task_spec_dedup_enabled,
)
from .resource_dedup import evidence_key, index_records, resource_slot
from .active_dedup import download_complete, download_files, download_state, paths_agree
from .channel_dedup import (
    filename_title_aliases, long_title_prefix_matches, normalize_title,
    parse_candidate_file, parse_channel_messages, parse_torrent_files,
    repair_record_frame_rates, same_release, specs_match,
)


TASK_CONFIG_FIELDS = (
    "enabled",
    "notify",
    "site_id",
    "downloader",
    "brush_interval",
    "check_interval",
    "cron",
    "active_time_range",
    "site_ratio_control",
    "site_ratio_target",
    "disksize",
    "maxupspeed",
    "maxdlspeed",
    "maxdlcount",
    "freeleech",
    "hr",
    "include",
    "exclude",
    "size",
    "seeder",
    "timezone_offset",
    "pubtime",
    "seed_time",
    "hr_seed_time",
    "seed_ratio",
    "seed_size",
    "download_time",
    "seed_avgspeed",
    "seed_inactivetime",
    "delete_size_range",
    "up_speed",
    "dl_speed",
    "auto_archive_days",
    "save_path",
    "delete_except_tags",
    "except_subscribe",
    "proxy_delete",
    "del_no_free",
    "qb_category",
    "site_hr_active",
    "recent_episodes_only",
    "spec_dedup_enabled",
    "blacklist_site_ids",
    "download_types",
    "site_skip_tips",
    "rss_support",
    "tag",
)

LEGACY_SITE_OVERRIDE_FIELDS = {
    "freeleech",
    "hr",
    "include",
    "exclude",
    "size",
    "seeder",
    "timezone_offset",
    "pubtime",
    "seed_time",
    "hr_seed_time",
    "seed_ratio",
    "seed_size",
    "download_time",
    "seed_avgspeed",
    "seed_inactivetime",
    "save_path",
    "proxy_delete",
    "qb_category",
    "site_hr_active",
    "recent_episodes_only",
    "blacklist_site_ids",
    "download_types",
    "site_skip_tips",
    "del_no_free",
    "rss_support",
}

GLOBAL_LIMIT_FIELDS = (
    "global_disksize",
    "global_maxdlcount",
    "global_maxupspeed",
    "global_maxdlspeed",
)

GLOBAL_DYNAMIC_DELETE_FIELDS = (
    "global_proxy_delete",
    "global_delete_size_range",
)


class BrushTaskConfig:
    """
    单个站点刷流任务的运行配置
    """

    def __init__(self, config: dict):
        """读取并标准化一项刷流任务配置"""
        self.id = str(config.get("id") or uuid.uuid4().hex)
        self.name = str(config.get("name") or "刷流任务").strip()
        self.enabled = bool(config.get("enabled", True))
        self.notify = bool(config.get("notify", True))
        self.site_id = int(config.get("site_id") or 0)
        self.downloader = str(config.get("downloader") or "").strip()
        self.brush_interval = max(int(self._parse_number(config.get("brush_interval")) or 10), 1)
        self.check_interval = max(int(self._parse_number(config.get("check_interval")) or 5), 1)
        self.cron = self._clean_text(config.get("cron"))
        self.active_time_range = self._clean_text(config.get("active_time_range"))
        self.site_ratio_control = bool(config.get("site_ratio_control", False))
        self.site_ratio_target = self._parse_number(config.get("site_ratio_target"))
        self.disksize = self._parse_number(config.get("disksize"))
        self.maxupspeed = self._parse_number(config.get("maxupspeed"))
        self.maxdlspeed = self._parse_number(config.get("maxdlspeed"))
        self.maxdlcount = self._parse_number(config.get("maxdlcount"))
        self.freeleech = config.get("freeleech", "free")
        self.hr = config.get("hr", "yes")
        self.include = self._clean_text(config.get("include"))
        self.exclude = self._clean_text(config.get("exclude"))
        self.size = self._clean_text(config.get("size"))
        self.seeder = self._clean_text(config.get("seeder"))
        self.timezone_offset = float(self._parse_number(config.get("timezone_offset")) or 0)
        self.pubtime = self._clean_text(config.get("pubtime"))
        self.seed_time = self._parse_number(config.get("seed_time"))
        self.hr_seed_time = self._parse_number(config.get("hr_seed_time"))
        self.seed_ratio = self._parse_number(config.get("seed_ratio"))
        self.seed_size = self._parse_number(config.get("seed_size"))
        self.download_time = self._parse_number(config.get("download_time"))
        self.seed_avgspeed = self._parse_number(config.get("seed_avgspeed"))
        self.seed_inactivetime = self._parse_number(config.get("seed_inactivetime"))
        self.delete_size_range = self._clean_text(config.get("delete_size_range"))
        self.up_speed = self._parse_number(config.get("up_speed"))
        self.dl_speed = self._parse_number(config.get("dl_speed"))
        self.auto_archive_days = self._parse_number(config.get("auto_archive_days"))
        self.save_path = self._clean_text(config.get("save_path"))
        self.delete_except_tags = self._clean_text(config.get("delete_except_tags"))
        self.except_subscribe = bool(config.get("except_subscribe", True))
        self.proxy_delete = bool(config.get("proxy_delete", False))
        self.del_no_free = bool(config.get("del_no_free", False)) if self.freeleech in {"free", "2xfree"} else False
        self.qb_category = self._clean_text(config.get("qb_category"))
        self.site_hr_active = bool(config.get("site_hr_active", False))
        self.recent_episodes_only = bool(config.get("recent_episodes_only", False))
        self.spec_dedup_enabled = bool(task_spec_dedup_enabled(config))
        self.blacklist_site_ids = list(dict.fromkeys(
            int(site_id) for site_id in config.get("blacklist_site_ids", [])
            if str(site_id).isdigit() and int(site_id) > 0
        ))
        self.download_types = list(dict.fromkeys(config.get("download_types") or []))
        self.site_skip_tips = bool(config.get("site_skip_tips", False))
        self.rss_support = bool(config.get("rss_support", False))
        self.tag = self._read_tag(config)

    @property
    def brush_tag(self) -> str:
        """返回当前任务在下载器中使用的标签（默认按站点名称生成，保证可读性）"""
        if self.tag:
            return self.tag
        site_name = BrushFlow._get_site_name(self.site_id) if self.site_id else None
        return f"刷流-{site_name}" if site_name else f"刷流-{self.id[:8]}"

    @staticmethod
    def _read_tag(config: dict) -> Optional[str]:
        """兼容读取 tag / downloader_tag，避免历史字段名导致启动时丢失下载器标签"""
        for key in ("tag", "downloader_tag"):
            value = BrushTaskConfig._clean_text(config.get(key))
            if value:
                return value
        return None

    @staticmethod
    def _clean_text(value: Any) -> Optional[str]:
        """把空白文本标准化为 None"""
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @staticmethod
    def _parse_number(value: Any) -> Optional[Union[int, float]]:
        """兼容解析历史配置中的整数、浮点数和空值"""
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return int(number) if number.is_integer() else number

    def to_dict(self) -> Dict[str, Any]:
        """返回可持久化和供前端编辑的任务配置"""
        data = {"id": self.id, "name": self.name}
        data.update({field: getattr(self, field) for field in TASK_CONFIG_FIELDS})
        return data


class BrushFlow(_PluginBase):
    """
    多站点独立任务刷流插件
    """

    plugin_name = "站点刷流"
    plugin_desc = "自动托管多个站点刷流任务，并独立调度、统计与诊断。"
    plugin_icon = "brush-flow.png"
    plugin_version = "6.4.4"
    plugin_author = "jxxghp,InfinityPacer,Seed680"
    author_url = "https://github.com/InfinityPacer"
    plugin_config_prefix = "brushflow_"
    plugin_order = 21
    auth_level = 2

    DATA_SCHEMA_VERSION = 2
    CONFIG_SCHEMA_VERSION = 6
    MAX_RUN_HISTORY = 50
    GLOBAL_BRUSH_TAG = "刷流"
    TASK_DATA_NAMES = ("torrents", "archived", "unmanaged", "statistic", "runs")
    CHANNEL_POLL_ACTIVE_SECONDS = 120
    CHANNEL_POLL_IDLE_SECONDS = 300
    CHANNEL_POLL_IDLE_THRESHOLD = 3
    CHANNEL_POLL_MANUAL_COOLDOWN = 30
    INFLIGHT_QUERY_SECONDS = 30
    INFLIGHT_ADD_GRACE_SECONDS = 300

    def init_plugin(self, config: dict = None) -> None:
        """初始化全局开关、任务配置、运行锁和历史数据迁移"""
        old_poll_lock = getattr(self, "_channel_dedup_lock", None)
        if old_poll_lock:
            self._channel_stopping = True
            with old_poll_lock:
                pass
        old_cancel_lock = getattr(self, "_channel_cancel_lock", None)
        if old_cancel_lock:
            self._channel_stopping = True
            with old_cancel_lock:
                pass
        raw_config = config or {}
        self._task_context = threading.local()
        self._task_locks: Dict[str, threading.Lock] = {}
        self._brush_lock = threading.Lock()
        self._global_delete_lock = threading.Lock()
        self._runtime_lock = threading.Lock()
        self._runtime: Dict[str, dict] = {}
        self._subscribe_infos: Dict[str, List[str]] = {}
        self._channel_dedup_lock = threading.Lock()
        self._channel_cancel_lock = threading.Lock()
        self._channel_cancel_offset = 0
        self._channel_state_lock = threading.RLock()
        self._channel_dedup_retry_after = 0
        self._channel_identity_cache = {}
        self._channel_generation = 0
        self._channel_stopping = False
        self._inflight_lock = getattr(self, "_inflight_lock", threading.RLock())
        with self._inflight_lock:
            inflight = self.get_data("channel_dedup_inflight") or {}
            self._inflight_entries = inflight if isinstance(inflight, dict) else {}
            self._inflight_checked_at = {}
            self._inflight_legacy_checked = {}
            self._inflight_task_snapshot = (0, [])
            self._inflight_alias_snapshot = None
            self._resource_ledger = self.get_data("resource_ledger") or {}
            self._identity_pending = self.get_data("resource_identity_pending") or {}
            self._resource_forgotten = self.get_data("resource_forgotten") or {}
        try:
            channel_config = ChannelDedupSettingsPayload.model_validate({
                "enabled": raw_config.get("channel_dedup_enabled", False),
                "cancel_downloading": raw_config.get("channel_dedup_cancel_downloading", False),
                "inflight_enabled": raw_config.get("channel_dedup_inflight_enabled", True),
                "pack_dedup_enabled": raw_config.get("channel_dedup_pack_dedup_enabled", True),
                "pack_policy": raw_config.get("channel_dedup_pack_policy", "partial"),
                "identity_policy": migrated_identity_policy(raw_config),
                "channels": raw_config.get("channel_dedup_channels", []),
                "attributes": raw_config.get(
                    "channel_dedup_attributes", ["resolution", "effects", "codec", "source", "fps"]
                ),
                "size_tolerance_pct": raw_config.get("channel_dedup_size_tolerance_pct", 5),
                "unknown_policy": raw_config.get("channel_dedup_unknown_policy", "exclude"),
                "identity_aliases": raw_config.get("channel_dedup_identity_aliases", {}),
                "movie_identity_aliases": raw_config.get("channel_dedup_movie_identity_aliases", {}),
            })
        except ValueError as err:
            channel_config = ChannelDedupSettingsPayload()
            logger.warning(f"频道去重配置无效，已使用默认值：{err}")
        self._channel_dedup_enabled = channel_config.enabled
        self._channel_dedup_cancel_downloading = channel_config.cancel_downloading
        self._channel_dedup_inflight_enabled = channel_config.inflight_enabled
        self._channel_dedup_pack_dedup_enabled = channel_config.pack_dedup_enabled
        self._channel_dedup_pack_policy = channel_config.pack_policy
        self._channel_dedup_identity_policy = channel_config.identity_policy
        self._channel_dedup_channels = channel_config.channels
        self._channel_dedup_attributes = channel_config.attributes
        self._channel_dedup_size_tolerance_pct = channel_config.size_tolerance_pct
        self._channel_dedup_unknown_policy = channel_config.unknown_policy
        self._channel_dedup_identity_aliases = channel_config.identity_aliases
        self._channel_dedup_movie_identity_aliases = channel_config.movie_identity_aliases
        self._channel_dedup_state = self.get_data("channel_dedup_state") or {
            "cursors": {}, "records": [], "last_poll_at": None, "last_error": "", "last_poll_ok": None,
        }
        self._prune_channel_dedup_records()
        repaired_frame_rates = repair_record_frame_rates(self._channel_dedup_state.get("records") or [])
        if repaired_frame_rates:
            self._save_channel_dedup_state()
            logger.info(f"频道资源记录已按原文件名修正 {repaired_frame_rates} 条帧率，消息游标保持不变")
        self._enabled = bool(raw_config.get("enabled", False))
        self._show_sidebar_nav = bool(raw_config.get("show_sidebar_nav", True))

        legacy_config = not isinstance(raw_config.get("tasks"), list) and bool(raw_config.get("brushsites"))
        for field in GLOBAL_LIMIT_FIELDS:
            value = raw_config.get(field)
            if value is None and legacy_config:
                value = raw_config.get(field.removeprefix("global_"))
            parsed_value = BrushTaskConfig._parse_number(value)
            setattr(self, f"_{field}", parsed_value if parsed_value and parsed_value > 0 else None)
        global_proxy_delete = raw_config.get(
            "global_proxy_delete",
            raw_config.get("proxy_delete", False) if legacy_config else False,
        )
        legacy_delete_range = raw_config.get("delete_size_range") if legacy_config else None
        self._global_proxy_delete, self._global_delete_size_range = self._validate_global_dynamic_delete_config(
            global_proxy_delete,
            raw_config.get("global_delete_size_range", legacy_delete_range),
        )

        task_rows = raw_config.get("tasks") if isinstance(raw_config.get("tasks"), list) else None
        migrated = task_rows is None and bool(raw_config.get("brushsites"))
        if migrated:
            task_rows = self._migrate_legacy_config(raw_config)
        task_rows = task_rows or []

        self._task_configs: Dict[str, BrushTaskConfig] = {}
        restored_tags = False
        for row in task_rows:
            if not isinstance(row, dict):
                continue
            if "spec_dedup_enabled" not in row:
                row = dict(row)
                row["spec_dedup_enabled"] = (
                    task_spec_dedup_enabled(row) if "purpose" in row or "resource_dedup_mode" in row
                    else "分享" in str(row.get("name", "")) or row.get("qb_category") == "分享"
                )
            task = BrushTaskConfig(row)
            if self._restore_task_tag(task, row):
                restored_tags = True
            if not self._validate_task_reference(task, notify=False):
                task.enabled = False
            self._task_configs[task.id] = task
            self._task_locks[task.id] = threading.Lock()
            self._runtime[task.id] = {"state": "idle", "operation": None, "last_error": None}

        normalized = self._current_config()
        if migrated or restored_tags or self._schema_needs_persist(raw_config):
            self.update_config(normalized)
        self._migrate_legacy_data()
        self._migrate_torrent_identity_data()
        with self._inflight_lock:
            for entry in self._inflight_entries.values():
                entry["spec_dedup_enabled"] = self._inflight_entry_enabled(entry)
                owners = [task for task in self._task_configs.values()
                          if task.name == entry.get("task_name") and task.downloader == entry.get("downloader")]
                if not entry.get("task_id") and len(owners) == 1:
                    entry["task_id"] = owners[0].id
                entry.pop("purpose", None)
            self._save_inflight_entries()
        self._remember_resources(self._channel_dedup_state.get("records") or [], "channel")

        # V5 为任务增加了唯一标签；启动时回收升级前已经失去任务配置的孤立标签。
        if self._enabled:
            try:
                ThreadHelper().submit(self._cleanup_unused_task_tags)
            except Exception as err:
                logger.warning(f"提交刷流标签清理任务失败：{str(err)}")

        if migrated and raw_config.get("onlyonce") and self._enabled:
            for task in self._task_configs.values():
                if task.enabled:
                    ThreadHelper().submit(self.brush, task.id)
                    ThreadHelper().submit(self.check, task.id)

    def get_state(self) -> bool:
        """返回插件全局启用状态"""
        return bool(getattr(self, "_enabled", False))

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """当前插件不注册远程命令"""
        return []

    @staticmethod
    def get_render_mode() -> Tuple[str, str]:
        """声明使用 Vue 联邦组件渲染插件界面"""
        return "vue", "dist/assets"

    def get_sidebar_nav(self) -> List[Dict[str, Any]]:
        """向主界面整理分组注册刷流任务入口"""
        if not self.get_state() or not getattr(self, "_show_sidebar_nav", True):
            return []
        return [
            {
                "nav_key": "main",
                "title": "站点刷流",
                "icon": "mdi-sync",
                "section": "organize",
                "permission": "manage",
                "order": 45,
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册 Vue 工作台使用的刷流任务 API"""
        return [
            {
                "path": "/status",
                "endpoint": self.get_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取刷流任务总览",
            },
            {
                "path": "/settings",
                "endpoint": self.update_settings,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "更新刷流插件设置",
            },
            {
                "path": "/channel-dedup",
                "endpoint": self.get_channel_dedup,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取频道监听与去重状态",
            },
            {
                "path": "/resource-ledger",
                "endpoint": self.get_resource_ledger,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "资源长期账本及待核验身份",
            },
            {
                "path": "/resource-ledger/forget",
                "endpoint": self.forget_resource,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "移除一条资源长期证据",
            },
            {
                "path": "/channel-dedup/settings",
                "endpoint": self.update_channel_dedup_settings,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "更新频道监听设置",
            },
            {
                "path": "/channel-dedup/poll",
                "endpoint": self.request_channel_dedup_poll,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即读取频道新消息",
            },
            {
                "path": "/tasks",
                "endpoint": self.create_task,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "创建刷流任务",
            },
            {
                "path": "/tasks/{task_id}",
                "endpoint": self.get_task_detail,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取刷流任务详情",
            },
            {
                "path": "/tasks/{task_id}",
                "endpoint": self.update_task,
                "methods": ["PUT"],
                "auth": "bear",
                "summary": "更新刷流任务",
            },
            {
                "path": "/tasks/{task_id}",
                "endpoint": self.delete_task,
                "methods": ["DELETE"],
                "auth": "bear",
                "summary": "删除刷流任务",
            },
            {
                "path": "/tasks/{task_id}/state",
                "endpoint": self.update_task_state,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "启用或暂停刷流任务",
            },
            {
                "path": "/tasks/{task_id}/run",
                "endpoint": self.run_task,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即执行刷流刷新",
            },
            {
                "path": "/tasks/{task_id}/check",
                "endpoint": self.check_task,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即检查刷流种子",
            },
            {
                "path": "/tasks/{task_id}/clear",
                "endpoint": self.clear_task_data,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清除单个刷流任务数据",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """Vue 配置组件只需要接收当前配置模型"""
        return [], self._current_config()

    def get_page(self) -> List[dict]:
        """Vue 详情组件自行通过插件 API 获取页面数据"""
        return []

    def get_dashboard(self, key: str, **kwargs) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], None]]:
        """保留原有仪表板入口并改由 Vue 组件渲染"""
        if not self.get_state():
            return None
        return (
            {"cols": 12, "sm": 6, "md": 6},
            {
                "title": "站点刷流",
                "subtitle": "多任务运行概览",
                "refresh": 30,
                "border": True,
            },
            None,
        )

    def get_service(self) -> List[Dict[str, Any]]:
        """为每个启用任务注册独立的刷流刷新和状态检查服务"""
        if not self.get_state():
            return []
        services: List[Dict[str, Any]] = []
        for task in self._task_configs.values():
            if not task.enabled:
                continue
            if task.cron:
                try:
                    brush_trigger: Union[str, CronTrigger] = CronTrigger.from_crontab(task.cron)
                    brush_kwargs: Dict[str, Any] = {}
                except ValueError as err:
                    logger.error(f"刷流任务 [{task.name}] CRON 表达式无效：{str(err)}")
                    brush_trigger = "interval"
                    brush_kwargs = {"minutes": task.brush_interval}
            else:
                brush_trigger = "interval"
                brush_kwargs = {"minutes": task.brush_interval}
            services.append(
                {
                    "id": f"Task_{task.id}_Brush",
                    "name": f"刷流刷新 - {task.name}",
                    "trigger": brush_trigger,
                    "func": self.brush,
                    "kwargs": brush_kwargs,
                    "func_kwargs": {"task_id": task.id},
                }
            )
            services.append(
                {
                    "id": f"Task_{task.id}_Check",
                    "name": f"刷流检查 - {task.name}",
                    "trigger": "interval",
                    "func": self.check,
                    "kwargs": {"minutes": task.check_interval},
                    "func_kwargs": {"task_id": task.id},
                }
            )
            promotion_expiry = self._next_promotion_expiry(task)
            if promotion_expiry:
                services.append(
                    {
                        "id": f"Task_{task.id}_PromotionExpiry",
                        "name": f"促销到期检查 - {task.name}",
                        "trigger": "date",
                        "func": self._check_promotion_expiry,
                        "kwargs": {"run_date": promotion_expiry},
                        "func_kwargs": {"task_id": task.id},
                    }
                )
        if getattr(self, "_channel_dedup_enabled", False) and self._channel_dedup_channels:
            services.append(
                {
                    "id": "ChannelDedupPoll",
                    "name": "频道资源记录同步",
                    "trigger": "interval",
                    "func": self.poll_channel_messages,
                    # Only check the persisted deadline here; Telegram reads run every 2 or 5 minutes.
                    "kwargs": {"minutes": 1},
                }
            )
            if getattr(self, "_channel_dedup_cancel_downloading", False):
                services.append({
                    "id": "ChannelDedupCancel", "name": "下载中频道重复检查",
                    "trigger": "interval", "func": self.cancel_channel_duplicates,
                    "kwargs": {"minutes": 1},
                })
        return services

    def stop_service(self) -> None:
        """插件不再维护私有调度器，公共服务由宿主统一停止"""
        self._channel_stopping = True
        poll_lock = getattr(self, "_channel_dedup_lock", None)
        if poll_lock:
            with poll_lock:
                pass
        cancel_lock = getattr(self, "_channel_cancel_lock", None)
        if cancel_lock:
            with cancel_lock:
                pass
        with getattr(self, "_runtime_lock", threading.Lock()):
            for runtime in getattr(self, "_runtime", {}).values():
                runtime.update({"state": "idle", "operation": None})

    @property
    def service_info(self) -> Optional[ServiceInfo]:
        """获取当前任务绑定的下载器服务"""
        task = self._get_task_config()
        if not task or not task.downloader:
            return None
        service = DownloaderHelper().get_service(name=task.downloader)
        if not service:
            self._log_and_notify_error(f"刷流任务 [{task.name}] 获取下载器实例失败，请检查配置")
            return None
        if service.instance.is_inactive():
            self._log_and_notify_error(f"刷流任务 [{task.name}] 下载器未连接")
            return None
        return service

    @property
    def downloader(self) -> Optional[Union[Qbittorrent, Transmission]]:
        """返回当前任务绑定的下载器实例"""
        service = self.service_info
        return service.instance if service else None

    def get_status(self) -> schemas.Response:
        """返回全局设置、任务摘要和前端可选项"""
        return schemas.Response(success=True, data=self._build_status_data())

    def get_channel_dedup(self) -> schemas.Response:
        return schemas.Response(success=True, data=self._channel_dedup_payload(include_records=True))

    def update_channel_dedup_settings(self, payload: ChannelDedupSettingsPayload) -> schemas.Response:
        with self._channel_state_lock:
            restart_polling = payload.channels != self._channel_dedup_channels or (
                payload.enabled and not self._channel_dedup_enabled
            )
            self._channel_dedup_enabled = payload.enabled
            self._channel_dedup_cancel_downloading = payload.cancel_downloading
            for field in ("inflight_enabled", "pack_dedup_enabled", "pack_policy", "identity_policy"):
                if field in payload.model_fields_set:
                    setattr(self, f"_channel_dedup_{field}", getattr(payload, field))
            self._channel_dedup_channels = payload.channels
            self._channel_dedup_attributes = payload.attributes
            self._channel_dedup_size_tolerance_pct = payload.size_tolerance_pct
            self._channel_dedup_unknown_policy = payload.unknown_policy
            self._channel_dedup_identity_aliases = payload.identity_aliases
            self._channel_dedup_movie_identity_aliases = payload.movie_identity_aliases
            self._channel_identity_cache.clear()
            self._inflight_task_snapshot = (0, [])
            self._channel_generation += 1
            if restart_polling:
                self._channel_dedup_state["idle_polls"] = 0
                self._channel_dedup_state["next_poll_at"] = 0
                self._save_channel_dedup_state()
            self._prune_channel_dedup_records()
        self._save_config()
        with self._inflight_lock:
            self._refresh_inflight_identity_bindings()
        self._refresh_scheduler()
        if payload.enabled and payload.channels and self.get_state():
            ThreadHelper().submit(self.poll_channel_messages)
        return schemas.Response(success=True, data=self._channel_dedup_payload(include_records=True))

    def request_channel_dedup_poll(self) -> schemas.Response:
        if not self._channel_dedup_enabled:
            return schemas.Response(success=False, message="请先开启频道监听")
        if not self._channel_dedup_channels:
            return schemas.Response(success=False, message="请至少添加一个 Telegram 频道")
        with self._channel_state_lock:
            state = self._channel_dedup_state
            if self._channel_dedup_lock.locked():
                return schemas.Response(success=True, message="频道消息正在同步")
            if time.monotonic() < self._channel_dedup_retry_after or time.time() < state.get("retry_at", 0):
                return schemas.Response(success=True, message="账号或网络退避等待中，暂不重复同步")
            if time.time() < (state.get("last_poll_at") or 0) + self.CHANNEL_POLL_MANUAL_COOLDOWN:
                return schemas.Response(success=True, message="刚完成同步，请稍后再试")
        try:
            ThreadHelper().submit(self.poll_channel_messages, True)
        except Exception as err:
            logger.error(f"提交频道消息同步任务失败：{type(err).__name__}")
            return schemas.Response(success=False, message="提交频道同步失败")
        return schemas.Response(success=True, message="已提交频道消息同步")

    def poll_channel_messages(self, force: bool = False) -> None:
        """通过 ED2K 插件的受锁 Telethon Session 增量读取频道消息。"""
        if getattr(self, "_channel_stopping", False) or not self.get_state():
            return
        if not getattr(self, "_channel_dedup_enabled", False) or not self._channel_dedup_channels:
            return
        if time.monotonic() < getattr(self, "_channel_dedup_retry_after", 0):
            return
        if not self._channel_dedup_lock.acquire(blocking=False):
            return
        try:
            with self._channel_state_lock:
                if self._channel_stopping or not self._channel_dedup_enabled or not self._channel_dedup_channels:
                    return
                poll_started = time.time()
                current_state = self._channel_dedup_state
                if poll_started < current_state.get("retry_at", 0):
                    return
                if force:
                    if poll_started < (current_state.get("last_poll_at") or 0) + self.CHANNEL_POLL_MANUAL_COOLDOWN:
                        return
                elif poll_started < current_state.get("next_poll_at", 0):
                    return
                self._prune_channel_dedup_records(save=False)
                state = copy.deepcopy(self._channel_dedup_state)
                channels = list(self._channel_dedup_channels)
                offset = int(state.get("channel_offset") or 0) % len(channels)
                channels = channels[offset:] + channels[:offset]
                generation = self._channel_generation
                previous_cursors = dict(state.get("cursors") or {})
            try:
                from app.runtime.extensions.plugin.manager import PluginManager

                ed2k = next(
                    (
                        plugin for plugin in PluginManager().running_plugins.values()
                        if type(plugin).__name__ == "Ed2kFolderWatcher"
                    ),
                    None,
                )
                if not ed2k or not callable(getattr(ed2k, "read_telegram_channel_messages", None)):
                    raise RuntimeError("自动生成ED2K链接插件未运行")
                result = ed2k.read_telegram_channel_messages(channels, dict(state.get("cursors") or {}))
                cutoff = time.time() - 72 * 60 * 60
                records = list(state.get("records") or [])
                known = {(item.get("channel"), item.get("message_id"), item.get("file_key")) for item in records}
                parsed_count = 0
                for message in result.get("messages") or []:
                    channel = str(message.get("channel") or "")
                    message_id = int(message.get("message_id") or 0)
                    if message_id:
                        state.setdefault("cursors", {})[channel] = max(
                            int(state.get("cursors", {}).get(channel) or 0), message_id
                        )
                    parsed_records = parse_channel_messages(
                        channel, message_id, message.get("published_at"), message.get("text")
                    )
                    for record in parsed_records:
                        key = (channel, message_id, record.get("file_key"))
                        if record["published_at"] >= cutoff and key not in known:
                            records.append(record)
                            known.add(key)
                            parsed_count += 1
                for channel, message_id in (result.get("cursors") or {}).items():
                    state.setdefault("cursors", {})[str(channel)] = max(
                        int(state.get("cursors", {}).get(str(channel)) or 0), int(message_id or 0)
                    )
                errors = result.get("errors") or []
                processed_channels = result.get("processed_channels", channels)
                processed_count = len(processed_channels)
                has_new_messages = any(
                    int(message_id or 0) > int(previous_cursors.get(channel) or 0)
                    for channel, message_id in state.get("cursors", {}).items()
                )
                message_counts = {}
                for message in result.get("messages") or []:
                    channel = message.get("channel")
                    message_counts[channel] = message_counts.get(channel, 0) + 1
                needs_catchup = set(processed_channels) != set(channels) or any(
                    count >= 200 for count in message_counts.values()
                )
                state["idle_polls"] = (
                    int(state.get("idle_polls") or 0) + 1
                    if not has_new_messages and not errors and not needs_catchup else 0
                )
                interval = (
                    self.CHANNEL_POLL_IDLE_SECONDS
                    if state["idle_polls"] >= self.CHANNEL_POLL_IDLE_THRESHOLD
                    else self.CHANNEL_POLL_ACTIVE_SECONDS
                )
                state["poll_interval_seconds"] = interval
                state["next_poll_at"] = poll_started + interval
                state["retry_at"] = 0
                state["channel_offset"] = (offset + processed_count) % len(channels)
                state["records"] = records
                state["last_poll_at"] = time.time()
                state["last_poll_ok"] = not errors
                state["last_error"] = "; ".join(
                    f"{item.get('channel')}: {item.get('error')}" for item in errors
                )
                with self._channel_state_lock:
                    if generation != self._channel_generation or self._channel_stopping:
                        return
                    self._channel_dedup_retry_after = 0
                    self._channel_dedup_state = state
                    self._prune_channel_dedup_records(save=False)
                    self._save_channel_dedup_state()
                if errors:
                    logger.warning(f"频道资源记录同步部分失败：{state['last_error']}")
                else:
                    logger.info(
                        f"频道资源记录同步完成：新增 {parsed_count} 条结构化记录，"
                        f"保留 {len(state['records'])} 条，下次约 {interval // 60} 分钟后同步"
                    )
                self._remember_resources(state["records"], "channel")
            except Exception as err:
                state["last_poll_at"] = time.time()
                state["last_poll_ok"] = False
                state["last_error"] = (
                    "请先更新并启用自动生成ED2K链接插件，完成 Telegram 个人账号授权"
                    if isinstance(err, RuntimeError) else type(err).__name__
                )
                with self._channel_state_lock:
                    retry_after = max(
                        self.CHANNEL_POLL_ACTIVE_SECONDS,
                        int(getattr(err, "retry_after", 120) or 120),
                    )
                    self._channel_dedup_retry_after = time.monotonic() + retry_after
                    state["retry_at"] = time.time() + retry_after
                    # Keep account backoff even if settings changed while the request was running.
                    if generation != self._channel_generation or self._channel_stopping:
                        self._channel_dedup_state.update({
                            "retry_at": state["retry_at"], "next_poll_at": state["retry_at"],
                        })
                        self._save_channel_dedup_state()
                        return
                    state["next_poll_at"] = state["retry_at"]
                    state["idle_polls"] = 0
                    state["poll_interval_seconds"] = retry_after
                    self._channel_dedup_state = state
                    self._save_channel_dedup_state()
                logger.warning(f"频道资源记录同步失败：{state['last_error']}")
        finally:
            self._channel_dedup_lock.release()

    def update_settings(self, payload: BrushFlowSettingsPayload) -> schemas.Response:
        """更新插件全局开关并刷新宿主任务调度"""
        global_dynamic_delete_was_enabled = self._global_dynamic_delete_enabled()
        self._enabled = payload.enabled
        self._show_sidebar_nav = payload.show_sidebar_nav
        for field in GLOBAL_LIMIT_FIELDS:
            setattr(self, f"_{field}", getattr(payload, field))
        for field in GLOBAL_DYNAMIC_DELETE_FIELDS:
            setattr(self, f"_{field}", getattr(payload, field))
        if global_dynamic_delete_was_enabled and not self._global_dynamic_delete_enabled():
            for task in self._task_configs.values():
                if task.proxy_delete and not task.delete_size_range:
                    task.proxy_delete = False
        self._save_config()
        self._refresh_scheduler()
        return schemas.Response(success=True, data=self._build_status_data())

    def create_task(self, payload: BrushTaskPayload) -> schemas.Response:
        """创建一个站点与下载器均独立的刷流任务"""
        task_data = payload.model_dump()
        task_data["id"] = uuid.uuid4().hex
        task = BrushTaskConfig(task_data)
        if not self._validate_task_reference(task):
            return schemas.Response(success=False, message="站点或下载器配置无效")
        self._task_configs[task.id] = task
        self._task_locks[task.id] = threading.Lock()
        self._runtime[task.id] = {"state": "idle", "operation": None, "last_error": None}
        self._save_config()
        self._refresh_scheduler()
        return schemas.Response(success=True, data=self._build_task_detail(task.id))

    def get_task_detail(
        self,
        task_id: str,
        state: str = Query("active", pattern="^(active|deleted|all)$"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=10, le=200),
    ) -> schemas.Response:
        """分页返回任务配置、统计、诊断记录和种子明细"""
        if task_id not in self._task_configs:
            return schemas.Response(success=False, message="刷流任务不存在")
        return schemas.Response(
            success=True,
            data=self._build_task_detail(task_id, state=state, page=page, page_size=page_size),
        )

    def update_task(self, task_id: str, payload: BrushTaskPayload) -> schemas.Response:
        """更新任务配置并保持原任务 ID 与历史数据关联"""
        if task_id not in self._task_configs:
            return schemas.Response(success=False, message="刷流任务不存在")
        if self._is_task_busy(task_id):
            return schemas.Response(success=False, message="任务正在执行，请稍后再修改")
        task_data = self._task_configs[task_id].to_dict()
        # 请求里省略的字段保持原值，避免旧前端把下载器标签等配置覆盖为空。
        task_data.update(payload.model_dump(exclude_unset=True))
        task_data["id"] = task_id
        task = BrushTaskConfig(task_data)
        if not self._validate_task_reference(task):
            return schemas.Response(success=False, message="站点或下载器配置无效")
        self._task_configs[task_id] = task
        with self._inflight_lock:
            self._inflight_task_snapshot = (0, [])
            self._inflight_legacy_checked = {}
        self._save_config()
        self._refresh_scheduler()
        return schemas.Response(success=True, data=self._build_task_detail(task_id))

    def delete_task(self, task_id: str) -> schemas.Response:
        """删除没有活跃种子的任务及其独立历史数据"""
        task = self._task_configs.get(task_id)
        if not task:
            return schemas.Response(success=False, message="刷流任务不存在")
        if self._is_task_busy(task_id):
            return schemas.Response(success=False, message="任务正在执行，请稍后再删除")
        torrents = self._get_task_data(task_id, "torrents") or {}
        active_count = sum(1 for item in torrents.values() if not item.get("deleted"))
        if active_count:
            return schemas.Response(success=False, message="任务仍有活跃种子，请先处理后再删除")
        self._task_configs.pop(task_id, None)
        self._task_locks.pop(task_id, None)
        self._runtime.pop(task_id, None)
        for data_name in self.TASK_DATA_NAMES:
            self.del_data(self._task_data_key(task_id, data_name))
        self._save_config()
        self._refresh_scheduler()
        try:
            # 删除接口不应被下载器网络请求阻塞，标签清理由后台线程完成。
            ThreadHelper().submit(self._cleanup_unused_task_tag, task)
        except Exception as err:
            logger.warning(f"提交刷流任务标签清理失败：{str(err)}")
        return schemas.Response(success=True, data=self._build_status_data())

    def update_task_state(self, task_id: str, payload: BrushTaskStatePayload) -> schemas.Response:
        """启用或暂停单个任务并更新对应宿主调度"""
        task = self._task_configs.get(task_id)
        if not task:
            return schemas.Response(success=False, message="刷流任务不存在")
        task.enabled = payload.enabled
        self._save_config()
        self._refresh_scheduler()
        return schemas.Response(success=True, data=self._build_task_detail(task_id))

    def run_task(self, task_id: str) -> schemas.Response:
        """异步提交单个任务的立即刷流刷新"""
        return self._submit_task_operation(task_id, "brush")

    def check_task(self, task_id: str) -> schemas.Response:
        """异步提交单个任务的立即状态检查"""
        return self._submit_task_operation(task_id, "check")

    def clear_task_data(self, task_id: str) -> schemas.Response:
        """清除单个任务的统计、历史与托管记录"""
        if task_id not in self._task_configs:
            return schemas.Response(success=False, message="刷流任务不存在")
        if self._is_task_busy(task_id):
            return schemas.Response(success=False, message="任务正在执行，请稍后再清除")
        for data_name in self.TASK_DATA_NAMES:
            self._save_task_data(task_id, data_name, {} if data_name != "runs" else [])
        return schemas.Response(success=True, data=self._build_task_detail(task_id))

    @eventmanager.register(EventType.PluginReload)
    def reload(self, event: Event) -> None:
        """插件重载后重新注册动态 API 和任务调度"""
        if event and event.event_data.get("plugin_id") == self.__class__.__name__:
            register_plugin_api(plugin_id=self.__class__.__name__)
            Scheduler().update_plugin_job(self.__class__.__name__)

    @staticmethod
    def _restore_task_tag(task: BrushTaskConfig, row: dict) -> bool:
        """原始配置里有标签、归一化后丢失时写回，返回是否发生了恢复"""
        raw_tag = BrushTaskConfig._read_tag(row)
        if raw_tag and not task.tag:
            task.tag = raw_tag
            return True
        return False

    def _schema_needs_persist(self, raw_config: dict) -> bool:
        """仅在 schema 版本变化时把规范化配置写回数据库"""
        return (raw_config.get("schema_version") or 0) != self.CONFIG_SCHEMA_VERSION

    def _current_config(self) -> Dict[str, Any]:
        """返回插件当前可持久化配置快照"""
        config = {
            "schema_version": self.CONFIG_SCHEMA_VERSION,
            "enabled": bool(getattr(self, "_enabled", False)),
            "show_sidebar_nav": bool(getattr(self, "_show_sidebar_nav", True)),
            "channel_dedup_enabled": bool(getattr(self, "_channel_dedup_enabled", False)),
            "channel_dedup_cancel_downloading": bool(getattr(self, "_channel_dedup_cancel_downloading", False)),
            "channel_dedup_inflight_enabled": bool(getattr(self, "_channel_dedup_inflight_enabled", True)),
            "channel_dedup_pack_dedup_enabled": bool(getattr(self, "_channel_dedup_pack_dedup_enabled", True)),
            "channel_dedup_pack_policy": getattr(self, "_channel_dedup_pack_policy", "partial"),
            "channel_dedup_identity_policy": getattr(self, "_channel_dedup_identity_policy", "defer"),
            "channel_dedup_channels": list(getattr(self, "_channel_dedup_channels", [])),
            "channel_dedup_attributes": list(getattr(self, "_channel_dedup_attributes", [])),
            "channel_dedup_size_tolerance_pct": getattr(self, "_channel_dedup_size_tolerance_pct", 5),
            "channel_dedup_unknown_policy": getattr(self, "_channel_dedup_unknown_policy", "exclude"),
            "channel_dedup_identity_aliases": dict(getattr(self, "_channel_dedup_identity_aliases", {})),
            "channel_dedup_movie_identity_aliases": dict(getattr(self, "_channel_dedup_movie_identity_aliases", {})),
            "tasks": [task.to_dict() for task in getattr(self, "_task_configs", {}).values()],
        }
        config.update({field: getattr(self, f"_{field}", None) for field in GLOBAL_LIMIT_FIELDS})
        config.update({field: getattr(self, f"_{field}", None) for field in GLOBAL_DYNAMIC_DELETE_FIELDS})
        return config

    def _save_channel_dedup_state(self) -> None:
        self.save_data("channel_dedup_state", self._channel_dedup_state)

    def _prune_channel_dedup_records(self, save: bool = True) -> None:
        with self._channel_state_lock:
            state = getattr(self, "_channel_dedup_state", None)
            if not isinstance(state, dict):
                self._channel_dedup_state = {
                    "cursors": {}, "records": [], "last_poll_at": None, "last_error": "", "last_poll_ok": None,
                }
                return
            cutoff = time.time() - 72 * 60 * 60
            records = state.get("records") if isinstance(state.get("records"), list) else []
            channels = set(self._channel_dedup_channels)
            kept_records = [
                row for row in records
                if isinstance(row, dict) and isinstance(row.get("published_at"), (int, float))
                and cutoff <= row["published_at"] <= time.time() + 300
                and row.get("channel") in channels
            ]
            cursors = state.get("cursors") if isinstance(state.get("cursors"), dict) else {}
            kept_cursors = {channel: value for channel, value in cursors.items() if channel in channels}
            changed = kept_records != state.get("records") or kept_cursors != state.get("cursors")
            state["records"] = kept_records
            state["cursors"] = kept_cursors
            if save and changed:
                self._save_channel_dedup_state()

    def _channel_dedup_payload(self, include_records: bool = False) -> dict:
        with self._channel_state_lock:
            self._prune_channel_dedup_records()
            state = self._channel_dedup_state
            payload = {
                "enabled": self._channel_dedup_enabled,
                "cancel_downloading": bool(getattr(self, "_channel_dedup_cancel_downloading", False)),
                "inflight_enabled": bool(getattr(self, "_channel_dedup_inflight_enabled", True)),
                "pack_dedup_enabled": bool(getattr(self, "_channel_dedup_pack_dedup_enabled", True)),
                "pack_policy": getattr(self, "_channel_dedup_pack_policy", "partial"),
                "identity_policy": getattr(self, "_channel_dedup_identity_policy", "defer"),
                "channels": list(self._channel_dedup_channels),
                "attributes": list(self._channel_dedup_attributes),
                "size_tolerance_pct": self._channel_dedup_size_tolerance_pct,
                "unknown_policy": getattr(self, "_channel_dedup_unknown_policy", "exclude"),
                "identity_aliases": dict(getattr(self, "_channel_dedup_identity_aliases", {})),
                "movie_identity_aliases": dict(getattr(self, "_channel_dedup_movie_identity_aliases", {})),
                "ledger_count": len(getattr(self, "_resource_ledger", {})),
                "pending_count": len(getattr(self, "_identity_pending", {})),
                "retention_hours": 72,
                "last_poll_at": state.get("last_poll_at"),
                "last_poll_ok": state.get("last_poll_ok"),
                "last_error": state.get("last_error") or "",
                "poll_interval_seconds": state.get("poll_interval_seconds", self.CHANNEL_POLL_ACTIVE_SECONDS),
                "next_poll_at": max(state.get("next_poll_at") or 0, state.get("retry_at") or 0) or None,
                "syncing": self._channel_dedup_lock.locked(),
                "record_count": len(state.get("records") or []),
            }
            if include_records:
                payload["records"] = sorted(
                    state.get("records") or [], key=lambda row: row["published_at"], reverse=True
                )[:100]
            return payload

    def _save_config(self) -> None:
        """保存全局设置和全部任务配置"""
        self.update_config(self._current_config())

    def _refresh_scheduler(self) -> None:
        """通知宿主按最新任务列表重建插件服务"""
        try:
            Scheduler().update_plugin_job(self.__class__.__name__)
        except Exception as err:
            logger.error(f"更新站点刷流调度失败：{str(err)}")

    def _global_dynamic_delete_enabled(self) -> bool:
        """返回全局动态删种开关与阈值是否同时有效"""
        return bool(
            getattr(self, "_global_proxy_delete", False)
            and getattr(self, "_global_delete_size_range", None)
        )

    @staticmethod
    def _validate_global_dynamic_delete_config(enabled: Any, size_range: Any) -> Tuple[bool, Optional[str]]:
        """复用设置模型校验持久化或迁移得到的全局动态删种配置。"""
        try:
            payload = BrushFlowSettingsPayload.model_validate(
                {
                    "global_proxy_delete": enabled,
                    "global_delete_size_range": size_range,
                }
            )
        except ValueError as err:
            logger.warning(f"全局动态删种配置无效，已自动关闭：{str(err)}")
            return False, None
        return payload.global_proxy_delete, payload.global_delete_size_range

    @staticmethod
    def _promotion_expiry_at(freedate_origin: Any, timezone_offset: float) -> Optional[datetime]:
        """把站点促销截止时间换算为宿主时区中的实际到期时刻"""
        if not freedate_origin:
            return None
        try:
            freedate_text = str(freedate_origin).strip().replace("T", " ").removesuffix("Z")
            site_expiry = datetime.strptime(freedate_text, "%Y-%m-%d %H:%M:%S")
            local_expiry = site_expiry + timedelta(hours=timezone_offset)
            return local_expiry.replace(tzinfo=ZoneInfo(settings.TZ))
        except (TypeError, ValueError) as err:
            logger.warning(f"解析促销截止时间失败：{str(err)}")
            return None

    def _next_promotion_expiry(self, task: BrushTaskConfig) -> Optional[datetime]:
        """返回任务中下一项未完成下载的促销截止时间"""
        if not task.del_no_free:
            return None
        now = datetime.now(ZoneInfo(settings.TZ))
        expiries: List[datetime] = []
        torrent_tasks = self._get_task_data(task.id, "torrents") or {}
        for torrent_task in torrent_tasks.values():
            if not isinstance(torrent_task, dict) or torrent_task.get("deleted"):
                continue
            if self._torrent_complete(torrent_task):
                continue
            expiry = self._promotion_expiry_at(torrent_task.get("freedate"), task.timezone_offset)
            if expiry and expiry > now:
                expiries.append(expiry)
        return min(expiries) if expiries else None

    def _check_promotion_expiry(self, task_id: str) -> None:
        """在最近促销截止时等待当前操作结束，检查后重排下一截止任务"""
        try:
            self.check(task_id, wait_for_lock=True)
        finally:
            self._refresh_scheduler()

    def _validate_task_reference(self, task: BrushTaskConfig, notify: bool = True) -> bool:
        """校验任务引用的私有站点和下载器是否仍然存在"""
        site = SiteOper().get(task.site_id)
        downloader_configs = DownloaderHelper().get_configs()
        valid = bool(
            site
            and not getattr(site, "public", False)
            and task.downloader
            and task.downloader in downloader_configs
        )
        if notify and not valid:
            self._log_and_notify_error(f"刷流任务 [{task.name}] 引用的站点或下载器不存在")
        return valid

    @staticmethod
    def _torrent_has_tag(torrent: Any, tag: str) -> bool:
        """判断 qBittorrent 种子是否仍绑定指定标签"""
        if not isinstance(torrent, dict):
            return False
        tags = {
            item.strip()
            for item in str(torrent.get("tags") or "").split(",")
            if item.strip()
        }
        return tag in tags

    @staticmethod
    def _delete_qbittorrent_tags(service: Any, tags: Union[str, List[str]]) -> bool:
        """删除 qBittorrent 全局标签定义，不调用需要种子 Hash 的 removeTags。"""
        client = getattr(getattr(service, "instance", None), "qbc", None)
        if not client or not tags:
            return False
        client.torrents_delete_tags(tags=tags)
        return True

    def _cleanup_unused_task_tag(
        self,
        task: BrushTaskConfig,
        torrents: Optional[List[Any]] = None,
    ) -> None:
        """仅删除不再被任何 qBittorrent 种子使用的任务唯一标签"""
        if not task or not task.downloader:
            return
        try:
            helper = DownloaderHelper()
            service = helper.get_service(name=task.downloader)
            if not service or not service.instance or not helper.is_downloader("qbittorrent", service=service):
                return
            if torrents is None:
                torrents, error = service.instance.get_torrents()
                if error:
                    logger.warning(f"清理刷流任务 [{task.name}] 标签时获取下载器种子失败")
                    return
            if any(self._torrent_has_tag(torrent, task.brush_tag) for torrent in torrents or []):
                return
            if self._delete_qbittorrent_tags(service, task.brush_tag):
                logger.info(f"清理刷流任务 [{task.name}] 未使用标签：{task.brush_tag}")
        except Exception as err:
            # 标签清理失败不应影响刷流检查或任务删除主流程。
            logger.warning(f"清理刷流任务 [{task.name}] 标签失败：{str(err)}")

    def _cleanup_unused_task_tags(self) -> None:
        """扫描全部 qBittorrent 下载器，清理历史遗留的刷流唯一标签"""
        try:
            helper = DownloaderHelper()
            downloader_names = set(helper.get_configs().keys())
            for downloader_name in downloader_names:
                service = helper.get_service(name=downloader_name)
                if not service or not service.instance or not helper.is_downloader("qbittorrent", service=service):
                    continue
                client = getattr(service.instance, "qbc", None)
                if not client:
                    continue
                all_tags = [str(tag).strip() for tag in client.torrents_tags() or [] if str(tag).strip()]
                task_tags = [tag for tag in all_tags if tag.startswith("刷流-")]
                if not task_tags:
                    continue
                torrents, error = service.instance.get_torrents()
                if error:
                    logger.warning(f"扫描下载器 [{downloader_name}] 刷流标签时获取种子失败")
                    continue
                used_tags = {
                    tag
                    for torrent in torrents or []
                    for tag in {
                        item.strip()
                        for item in str(torrent.get("tags") or "").split(",")
                        if item.strip()
                    }
                }
                unused_tags = [tag for tag in task_tags if tag not in used_tags]
                if unused_tags and self._delete_qbittorrent_tags(service, unused_tags):
                    logger.info(f"清理下载器 [{downloader_name}] 未使用刷流标签：{','.join(unused_tags)}")
        except Exception as err:
            logger.warning(f"扫描清理历史刷流标签失败：{str(err)}")

    def _migrate_legacy_config(self, config: dict) -> List[dict]:
        """把旧全局配置和站点覆盖 JSON 拆分为一站点一任务"""
        overrides = self._parse_legacy_site_overrides(config)
        tasks: List[dict] = []
        for site_id in config.get("brushsites") or []:
            site = SiteOper().get(site_id)
            if not site or getattr(site, "public", False):
                continue
            task_data = {field: config.get(field) for field in TASK_CONFIG_FIELDS if field in config}
            site_override = overrides.get(site.name, {})
            task_data.update(site_override)
            # 旧版会把全局时区小时数转换成分钟后再次持久化，站点覆盖 JSON 则始终保留小时数。
            if "timezone_offset" not in site_override:
                timezone_offset = BrushTaskConfig._parse_number(config.get("timezone_offset")) or 0
                task_data["timezone_offset"] = float(timezone_offset) / 60
            task_data.update(
                {
                    "id": uuid.uuid4().hex,
                    "name": site.name,
                    "site_id": site.id,
                    "enabled": True,
                    "brush_interval": 10,
                    "check_interval": 5,
                }
            )
            tasks.append(BrushTaskConfig(task_data).to_dict())
        return tasks

    @staticmethod
    def _parse_legacy_site_overrides(config: dict) -> Dict[str, dict]:
        """解析旧版允许注释的站点覆盖 JSON"""
        if not config.get("enable_site_config") or not config.get("site_config"):
            return {}
        try:
            content = re.sub(r"//.*?(?:\n|$)", "", str(config.get("site_config"))).strip()
            rows = json.loads(content)
        except (TypeError, ValueError) as err:
            logger.error(f"解析旧版站点独立配置失败：{str(err)}")
            return {}
        overrides: Dict[str, dict] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get("sitename"):
                continue
            overrides[str(row["sitename"])] = {
                key: row[key] for key in LEGACY_SITE_OVERRIDE_FIELDS if key in row
            }
        return overrides

    def _migrate_legacy_data(self) -> None:
        """按站点把旧全局种子、归档和未托管记录迁移到任务命名空间"""
        if (self.get_data("task_data_schema_version") or 0) >= self.DATA_SCHEMA_VERSION:
            return
        tasks = list(self._task_configs.values())
        by_site_id = {str(task.site_id): task for task in tasks}
        by_site_name = {
            self._get_site_name(task.site_id): task for task in tasks if self._get_site_name(task.site_id)
        }
        for data_name in ("torrents", "archived", "unmanaged"):
            legacy_rows = self.get_data(data_name) or {}
            buckets: Dict[str, dict] = {task.id: {} for task in tasks}
            for item_id, item in legacy_rows.items() if isinstance(legacy_rows, dict) else []:
                task = by_site_id.get(str(item.get("site"))) or by_site_name.get(item.get("site_name"))
                if not task and len(tasks) == 1:
                    task = tasks[0]
                if not task:
                    continue
                migrated_item = dict(item)
                migrated_item.update({"task_id": task.id, "task_name": task.name})
                buckets[task.id][item_id] = migrated_item
            for task_id, rows in buckets.items():
                current = self._get_task_data(task_id, data_name)
                if not current and rows:
                    self._save_task_data(task_id, data_name, rows)
        for task in tasks:
            self._recalculate_statistics(task.id)
        self.save_data("task_data_schema_version", self.DATA_SCHEMA_VERSION)

    def _migrate_torrent_identity_data(self) -> None:
        """移除任务记录中未参与业务判断的旧 IMDb ID 字段。"""
        for task in self._task_configs.values():
            for data_name in ("torrents", "archived", "unmanaged"):
                rows = self._get_task_data(task.id, data_name)
                if not isinstance(rows, dict):
                    continue
                changed = False
                for item in rows.values():
                    if isinstance(item, dict) and "imdbid" in item:
                        item.pop("imdbid", None)
                        changed = True
                if changed:
                    self._save_task_data(task.id, data_name, rows)

    @staticmethod
    def _get_site_name(site_id: int) -> Optional[str]:
        """按站点 ID 获取名称并兼容已删除站点"""
        site = SiteOper().get(site_id)
        return site.name if site else None

    @contextmanager
    def _task_scope(self, task_id: str) -> Iterator[BrushTaskConfig]:
        """在当前线程中绑定任务上下文，供深层核心逻辑读取"""
        previous = getattr(self._task_context, "task_id", None)
        self._task_context.task_id = task_id
        try:
            task = self._task_configs.get(task_id)
            if not task:
                raise KeyError(f"刷流任务不存在：{task_id}")
            yield task
        finally:
            if previous is None:
                if hasattr(self._task_context, "task_id"):
                    delattr(self._task_context, "task_id")
            else:
                self._task_context.task_id = previous

    @contextmanager
    def _all_task_locks_scope(self) -> Iterator[None]:
        """按任务 ID 顺序锁定全部任务，保护跨任务删种的数据一致性"""
        acquired_locks: List[threading.Lock] = []
        try:
            for task_id in sorted(self._task_configs):
                task_lock = self._task_locks.setdefault(task_id, threading.Lock())
                task_lock.acquire()
                acquired_locks.append(task_lock)
            yield
        finally:
            for task_lock in reversed(acquired_locks):
                task_lock.release()

    def _get_task_config(self, task_id: Optional[str] = None) -> Optional[BrushTaskConfig]:
        """获取显式任务或当前线程绑定的任务配置"""
        resolved_id = task_id or getattr(self._task_context, "task_id", None)
        return self._task_configs.get(resolved_id) if resolved_id else None

    def __get_brush_config(self, sitename: str = None) -> Optional[BrushTaskConfig]:
        """兼容核心逻辑获取当前任务配置"""
        task = self._get_task_config()
        if task or not sitename:
            return task
        return next(
            (item for item in self._task_configs.values() if self._get_site_name(item.site_id) == sitename),
            None,
        )

    @staticmethod
    def _task_data_key(task_id: str, data_name: str) -> str:
        """生成任务独立的插件数据键"""
        return f"task.{task_id}.{data_name}"

    def _get_task_data(self, task_id: str, data_name: str) -> Any:
        """读取指定任务的独立持久化数据"""
        return self.get_data(self._task_data_key(task_id, data_name))

    def _save_task_data(self, task_id: str, data_name: str, value: Any) -> None:
        """保存指定任务的独立持久化数据"""
        self.save_data(self._task_data_key(task_id, data_name), value)

    def _current_task_data(self, data_name: str, default: Any = None) -> Any:
        """读取当前线程任务的数据并提供缺省值"""
        task = self._get_task_config()
        if not task:
            return default
        value = self._get_task_data(task.id, data_name)
        return default if value is None else value

    def _save_current_task_data(self, data_name: str, value: Any) -> None:
        """保存当前线程任务的数据"""
        task = self._get_task_config()
        if task:
            self._save_task_data(task.id, data_name, value)

    def _submit_task_operation(self, task_id: str, operation: str) -> schemas.Response:
        """校验运行条件后把手动操作提交到宿主线程池"""
        task = self._task_configs.get(task_id)
        if not task:
            return schemas.Response(success=False, message="刷流任务不存在")
        if not self.get_state() or not task.enabled:
            return schemas.Response(success=False, message="插件或任务未启用")
        if not self._mark_task_queued(task_id, operation):
            return schemas.Response(success=False, message="任务已有操作正在执行")
        target = self.brush if operation == "brush" else self.check
        try:
            ThreadHelper().submit(target, task_id)
        except Exception as err:
            self._set_runtime(task_id, state="idle", operation=None, last_error=str(err))
            logger.error(f"提交刷流任务 [{task.name}] 失败：{str(err)}")
            return schemas.Response(success=False, message="任务提交失败")
        return schemas.Response(success=True, message="任务已提交", data=self._task_summary(task_id))

    def _is_task_busy(self, task_id: str) -> bool:
        """判断任务是否已经排队或正在执行，保护运行中的配置与数据"""
        task_lock = self._task_locks.get(task_id)
        with self._runtime_lock:
            runtime = self._runtime.get(task_id, {})
            return bool(
                runtime.get("state") in {"queued", "running"}
                or (task_lock and task_lock.locked())
            )

    def _mark_task_queued(self, task_id: str, operation: str) -> bool:
        """以原子方式把空闲任务标记为排队，避免重复提交手动操作"""
        with self._runtime_lock:
            runtime = self._runtime.setdefault(
                task_id,
                {"state": "idle", "operation": None, "last_error": None},
            )
            if runtime.get("state") in {"queued", "running"}:
                return False
            runtime.update({"state": "queued", "operation": operation, "last_error": None})
            return True

    def _set_runtime(self, task_id: str, **updates: Any) -> None:
        """线程安全地更新任务瞬时运行状态"""
        with self._runtime_lock:
            runtime = self._runtime.setdefault(task_id, {"state": "idle", "operation": None, "last_error": None})
            runtime.update(updates)

    def _append_run(self, task_id: str, report: dict) -> None:
        """保存最近的刷流或检查诊断记录"""
        history = self._get_task_data(task_id, "runs") or []
        stored_report = {
            **report,
            "reason_counts": dict(report.get("reason_counts") or {}),
        }
        history.insert(0, stored_report)
        self._save_task_data(task_id, "runs", history[: self.MAX_RUN_HISTORY])

    def _build_status_data(self) -> Dict[str, Any]:
        """组装工作台总览、任务摘要和可选站点下载器"""
        site_user_data = (
            self._latest_site_user_data_by_domain()
            if any(task.site_ratio_control for task in self._task_configs.values())
            else {}
        )
        task_rows = [
            self._task_summary(task_id, site_user_data_by_domain=site_user_data)
            for task_id in self._task_configs
        ]
        aggregate = {
            "task_count": len(task_rows),
            "enabled_count": sum(1 for row in task_rows if row.get("enabled")),
            "running_count": sum(
                1 for row in task_rows if row.get("state") in {"running", "brush", "check"}
            ),
            "active_count": sum(row.get("statistic", {}).get("active", 0) for row in task_rows),
            "uploaded": sum(row.get("statistic", {}).get("uploaded", 0) for row in task_rows),
            "downloaded": sum(row.get("statistic", {}).get("downloaded", 0) for row in task_rows),
            "seeding_size": sum(row.get("seeding_size", 0) for row in task_rows),
        }
        site_options = [
            {"title": site.get("name"), "value": site.get("id")}
            for site in SitesHelper().get_indexers()
            if not site.get("public")
        ]
        downloader_options = [
            {"title": item.name, "value": item.name}
            for item in DownloaderHelper().get_configs().values()
        ]
        return {
            "enabled": self.get_state(),
            "show_sidebar_nav": self._show_sidebar_nav,
            "channel_dedup": self._channel_dedup_payload(include_records=False),
            **{field: getattr(self, f"_{field}", None) for field in GLOBAL_LIMIT_FIELDS},
            **{field: getattr(self, f"_{field}", None) for field in GLOBAL_DYNAMIC_DELETE_FIELDS},
            "summary": aggregate,
            "tasks": task_rows,
            "options": {"sites": site_options, "downloaders": downloader_options},
        }

    def _task_summary(
        self,
        task_id: str,
        site_user_data_by_domain: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """组装单个任务在左侧任务列表和仪表板中的摘要"""
        task = self._task_configs.get(task_id)
        if not task:
            return {}
        statistic = self._get_statistic_info(task_id)
        torrents = self._get_task_data(task_id, "torrents") or {}
        history = self._get_task_data(task_id, "runs") or []
        runtime = dict(self._runtime.get(task_id, {}))
        site_ratio = self._build_site_ratio_status(task, site_user_data_by_domain)
        if not self.get_state():
            display_state = "disabled"
        elif not task.enabled:
            display_state = "paused"
        elif runtime.get("state") in {"queued", "running"}:
            display_state = runtime.get("operation") or "running"
        elif site_ratio["enabled"] and not site_ratio["available"]:
            display_state = "ratio_unavailable"
        elif site_ratio["reached"]:
            display_state = "waiting_ratio"
        elif not self._is_current_time_in_range(task):
            display_state = "waiting"
        elif runtime.get("last_error"):
            display_state = "error"
        else:
            display_state = "running"
        return {
            "id": task.id,
            "name": task.name,
            "enabled": task.enabled,
            "site_id": task.site_id,
            "site_name": self._get_site_name(task.site_id) or "站点已删除",
            "spec_dedup_enabled": task.spec_dedup_enabled,
            "downloader": task.downloader,
            "brush_interval": task.brush_interval,
            "check_interval": task.check_interval,
            "cron": task.cron,
            "active_time_range": task.active_time_range,
            "qb_category": task.qb_category,
            "tag": task.tag,
            "state": display_state,
            "operation": runtime.get("operation"),
            "last_error": runtime.get("last_error"),
            "next_run_at": self._next_run_at(task, history),
            "last_run": history[0] if history else None,
            "statistic": statistic,
            "seeding_size": self.__calculate_seeding_torrents_size(torrents),
            "site_ratio": site_ratio,
        }

    @staticmethod
    def _latest_site_user_data_by_domain() -> Dict[str, Any]:
        """按标准化域名索引各站点最新一条有效用户统计。"""
        result: Dict[str, Any] = {}
        for row in SiteOper().get_userdata_latest() or []:
            domain = StringUtils.get_url_domain(getattr(row, "domain", None))
            if domain and domain not in result:
                result[domain] = row
        return result

    def _build_site_ratio_status(
        self,
        task: BrushTaskConfig,
        site_user_data_by_domain: Optional[Dict[str, Any]] = None,
        site: Any = None,
    ) -> Dict[str, Any]:
        """组装任务绑定站点的当前分享率、目标值和控制状态。"""
        status = {
            "enabled": bool(task.site_ratio_control),
            "target": task.site_ratio_target,
            "current": None,
            "available": False,
            "unlimited": False,
            "reached": False,
            "updated_at": None,
        }
        if not task.site_ratio_control or not task.site_ratio_target:
            return status
        site = site or SiteOper().get(task.site_id)
        if not site:
            return status
        if site_user_data_by_domain is None:
            site_user_data_by_domain = self._latest_site_user_data_by_domain()
        domain = StringUtils.get_url_domain(getattr(site, "domain", None))
        user_data = site_user_data_by_domain.get(domain)
        if not user_data:
            return status
        if getattr(user_data, "err_msg", None):
            return status
        ratio = BrushTaskConfig._parse_number(getattr(user_data, "ratio", None))
        if ratio is None:
            return status
        upload = BrushTaskConfig._parse_number(getattr(user_data, "upload", None)) or 0
        download = BrushTaskConfig._parse_number(getattr(user_data, "download", None)) or 0
        unlimited = float(ratio) == 0 and float(upload) > 0 and float(download) <= 0
        # 站点响应不完整时解析器会保留三个字段的零值，不能把它当成真实低分享率。
        if float(ratio) == 0 and float(upload) <= 0 and float(download) <= 0:
            return status
        updated_day = getattr(user_data, "updated_day", None)
        updated_time = getattr(user_data, "updated_time", None)
        status.update(
            {
                "current": None if unlimited else float(ratio),
                "available": True,
                "unlimited": unlimited,
                "reached": unlimited or float(ratio) >= float(task.site_ratio_target),
                "updated_at": " ".join(value for value in (updated_day, updated_time) if value) or None,
            }
        )
        return status

    def _evaluate_site_ratio_control(
        self,
        task: BrushTaskConfig,
        site: Any = None,
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """判断站点分享率是否允许当前任务继续新增种子。"""
        status = self._build_site_ratio_status(task, site=site)
        if not status["enabled"]:
            return True, None, status
        if not status["available"]:
            return False, "暂无站点分享率统计，等待数据更新", status
        if status["reached"]:
            current = "无限" if status["unlimited"] else f"{status['current']:.2f}"
            return (
                False,
                f"站点分享率 {current}，已达到目标 {float(status['target']):.2f}",
                status,
            )
        return True, None, status

    def _build_task_detail(
        self,
        task_id: str,
        state: str = "active",
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """组装任务编辑、概览、诊断与分页种子数据"""
        task = self._task_configs[task_id]
        torrents = self._get_task_data(task_id, "torrents") or {}
        archived = self._get_task_data(task_id, "archived") or {}
        rows = list(torrents.values())
        if state == "active":
            rows = [row for row in rows if not row.get("deleted")]
        elif state == "deleted":
            rows = [row for row in rows if row.get("deleted")] + list(archived.values())
        else:
            rows.extend(archived.values())
        rows.sort(key=lambda item: item.get("time") or 0, reverse=True)
        total = len(rows)
        start = (page - 1) * page_size
        selected_rows = rows[start : start + page_size]
        return {
            "task": task.to_dict(),
            "summary": self._task_summary(task_id),
            "runs": (self._get_task_data(task_id, "runs") or [])[:20],
            "torrents": {
                "items": selected_rows,
                "total": total,
                "page": page,
                "page_size": page_size,
                "state": state,
            },
        }

    @staticmethod
    def _next_run_at(task: BrushTaskConfig, history: List[dict]) -> Optional[str]:
        """按 CRON 或固定间隔估算任务下一次刷新时间"""
        now = datetime.now().astimezone()
        try:
            if task.cron:
                next_time = CronTrigger.from_crontab(task.cron, timezone=settings.TZ).get_next_fire_time(None, now)
            else:
                last_brush = next((item for item in history if item.get("kind") == "brush"), None)
                if last_brush and last_brush.get("started_at"):
                    base = datetime.fromisoformat(last_brush["started_at"])
                    next_time = max(base + timedelta(minutes=task.brush_interval), now)
                else:
                    next_time = now + timedelta(minutes=task.brush_interval)
            return next_time.isoformat(timespec="minutes") if next_time else None
        except (TypeError, ValueError):
            return None

    def brush(self, task_id: Optional[str] = None) -> None:
        """执行单个任务的站点刷新、选种和下载流程"""
        task = self._get_task_config(task_id)
        if not task or not self.get_state() or not task.enabled:
            return
        task_lock = self._task_locks.setdefault(task.id, threading.Lock())
        if not task_lock.acquire(blocking=False):
            logger.info(f"刷流任务 [{task.name}] 已有操作执行中，本轮跳过")
            return
        report = self._new_run_report("brush")
        self._set_runtime(task.id, state="running", operation="brush", last_error=None)
        try:
            with self._brush_lock, self._task_scope(task.id):
                self._run_brush(task, report)
            report["success"] = report.get("result") not in {"downloader_unavailable", "site_missing"}
        except Exception as err:
            report.update({"success": False, "error": str(err)})
            self._set_runtime(task.id, last_error=str(err))
            logger.error(f"刷流任务 [{task.name}] 执行失败：{str(err)}")
        finally:
            report["finished_at"] = self._now_iso()
            self._append_run(task.id, report)
            self._set_runtime(task.id, state="idle", operation=None)
            task_lock.release()
            if report.get("added_count"):
                self._refresh_scheduler()

    def _run_brush(self, task: BrushTaskConfig, report: dict) -> None:
        """在已绑定任务上下文中执行刷流核心流程"""
        if not self._validate_task_reference(task) or not self.downloader:
            report["result"] = "downloader_unavailable"
            return
        if not self._is_current_time_in_range(task):
            report["result"] = "outside_active_time"
            report["reason_counts"]["不在开启时间段"] = 1
            return
        site = SiteOper().get(task.site_id)
        if not site:
            report["result"] = "site_missing"
            return
        ratio_passed, ratio_reason, ratio_status = self._evaluate_site_ratio_control(task, site=site)
        if ratio_status["enabled"]:
            report["site_ratio"] = ratio_status
        if not ratio_passed:
            report["result"] = "site_ratio_blocked"
            report["reason_counts"][ratio_reason] = 1
            return
        torrent_tasks: Dict[str, dict] = self._current_task_data("torrents", {})
        seeding_size = self.__calculate_seeding_torrents_size(torrent_tasks)
        global_seeding_size = self._calculate_global_seeding_size(task.id, torrent_tasks)
        passed, reason = self.__evaluate_size_condition_for_brush(
            seeding_size,
            global_torrents_size=global_seeding_size,
        )
        if not passed:
            report["result"] = "precondition_blocked"
            report["reason_counts"][reason] = 1
            return
        passed, reason = self.__evaluate_pre_conditions_for_brush()
        if not passed:
            report["result"] = "precondition_blocked"
            report["reason_counts"][reason] = 1
            return
        all_torrent_tasks = self._load_all_torrent_tasks()
        subscribe_titles = self.__get_subscribe_titles()
        self.__brush_site_torrents(
            site=site,
            torrent_tasks=torrent_tasks,
            all_torrent_tasks=all_torrent_tasks,
            subscribe_titles=subscribe_titles,
            report=report,
            global_seeding_size=global_seeding_size,
        )
        self._save_current_task_data("torrents", torrent_tasks)
        self._recalculate_statistics(task.id)

    def _load_all_torrent_tasks(self) -> Dict[str, dict]:
        """聚合所有任务的当前记录以保持跨站点重复种子保护"""
        rows: Dict[str, dict] = {}
        for task_id in self._task_configs:
            task_rows = self._get_task_data(task_id, "torrents") or {}
            rows.update(task_rows)
        return rows

    def _calculate_global_seeding_size(
        self,
        current_task_id: Optional[str] = None,
        current_torrent_tasks: Optional[Dict[str, dict]] = None,
    ) -> float:
        """汇总所有任务未删除种子的体积，并允许使用当前任务的内存快照。"""
        total_size = 0.0
        for task_id in self._task_configs:
            if task_id == current_task_id and current_torrent_tasks is not None:
                task_rows = current_torrent_tasks
            else:
                task_rows = self._get_task_data(task_id, "torrents") or {}
            total_size += self.__calculate_seeding_torrents_size(task_rows)
        return total_size

    def __brush_site_torrents(
        self,
        site: Any,
        torrent_tasks: Dict[str, dict],
        all_torrent_tasks: Dict[str, dict],
        subscribe_titles: Set[str],
        report: dict,
        global_seeding_size: float,
    ) -> None:
        """获取当前任务站点候选并逐项执行保留的选种规则"""
        task = self._get_task_config()
        logger.info(f"刷流任务 [{task.name}] 开始获取站点 {site.name} 的新种子")
        torrents = TorrentsChain().rss(domain=site.domain) if task.rss_support else TorrentsChain().browse(domain=site.domain)
        if not torrents:
            report["result"] = "no_candidates"
            return
        report["source_count"] = len(torrents)
        if task.except_subscribe:
            before_count = len(torrents)
            torrents = self.__filter_torrents_contains_subscribe(torrents, subscribe_titles)
            report["subscription_excluded"] = before_count - len(torrents)
            if report["subscription_excluded"]:
                report["reason_counts"]["命中订阅内容"] = report["subscription_excluded"]
        report["candidate_count"] = len(torrents)
        torrents.sort(key=lambda item: item.pubdate or "", reverse=True)
        seeding_size = self.__calculate_seeding_torrents_size(torrent_tasks)
        blacklist_site_names = self._blacklist_other_site_names(task)
        blacklist_history_cache = {}
        type_cache = {}
        for torrent in torrents:
            passed, reason = self.__evaluate_pre_conditions_for_brush(include_network_conditions=False)
            if not passed:
                report["reason_counts"][reason] += 1
                report["result"] = "precondition_blocked"
                break
            passed, reason = self.__evaluate_size_condition_for_brush(
                seeding_size,
                torrent.size,
                global_torrents_size=global_seeding_size,
            )
            if not passed:
                report["reason_counts"][reason] += 1
                continue
            passed, reason = self.__evaluate_conditions_for_brush(torrent, all_torrent_tasks)
            if not passed:
                report["reason_counts"][reason] += 1
                continue
            if blacklist_site_names is None:
                report["blacklist_excluded"] += 1
                report["reason_counts"]["种子黑名单站点信息读取失败"] += 1
                continue
            if blacklist_site_names:
                blocked, reason = self._is_blacklist_duplicate(
                    torrent.title,
                    blacklist_site_names,
                    blacklist_history_cache,
                )
                if blocked:
                    report["blacklist_excluded"] += 1
                    report["reason_counts"][reason] += 1
                    logger.info(f"刷流任务 [{task.name}] 排除种子 {torrent.title}：{reason}")
                    continue
            if task.download_types:
                media_type, source = self._download_type(torrent, type_cache)
                if media_type not in task.download_types:
                    reason = f"下载类型未选择：{media_type}" if media_type else "下载类型无法识别"
                    report["reason_counts"][reason] += 1
                    logger.info(f"刷流任务 [{task.name}] 排除种子 {torrent.title}：{reason}（{source}）")
                    continue
                logger.info(f"刷流任务 [{task.name}] 下载类型通过 {torrent.title}：{media_type}（{source}）")
            hash_string = self.__download(torrent)
            if not hash_string:
                report["reason_counts"]["下载预检未通过或下载器添加失败"] += 1
                continue
            torrent_task = self._torrent_to_task_record(torrent, site, task)
            torrent_tasks[hash_string] = torrent_task
            all_torrent_tasks[hash_string] = torrent_task
            seeding_size += torrent.size
            global_seeding_size += torrent.size
            report["added_count"] += 1
            report["added_titles"].append(torrent.title)
            self.eventmanager.send_event(
                etype=EventType.PluginTriggered,
                data={
                    "plugin_id": self.__class__.__name__,
                    "event_name": "brushflow_download_added",
                    "hash": hash_string,
                    "data": torrent_task,
                    "downloader": self.service_info.name,
                },
            )
            logger.info(f"刷流任务 [{task.name}] 新增种子：{torrent.title}|{torrent.description}")
            self.__send_add_message(torrent)
        report["filtered_count"] = max(report["candidate_count"] - report["added_count"], 0)
        report["result"] = "completed"

    @staticmethod
    def _explicit_download_type(values: list) -> Optional[str]:
        """只接受明确的分类或标签，避免将种子标题中的普通词误当类别。"""
        aliases = {
            "anime": {"动漫", "动画", "动画片", "番剧", "anime", "animation"},
            "variety": {"综艺", "真人秀", "综艺节目", "variety", "reality", "talk show"},
            "documentary": {"纪录", "纪录片", "记录片", "documentary"},
            "sports": {"体育", "运动", "体育赛事", "体育运动", "sports", "sport"},
            "mv": {"mv", "音乐视频", "音乐录影带", "music video"},
            "music": {"音乐", "music"},
            "movie": {"电影", "影片", "movie", "movies", "film"},
            "tv": {"剧集", "电视剧", "电视", "tv", "tv series", "series"},
        }
        labels = {str(value or "").strip().lower() for value in values}
        for kind, names in aliases.items():
            if labels & names:
                return kind
        return None

    def _download_type(self, torrent: TorrentInfo, cache: dict) -> Tuple[Optional[str], str]:
        """按明确站点分类及媒体题材分类；未识别时不允许通过筛选。"""
        category = self._explicit_download_type([torrent.category] + list(torrent.labels or []))
        if category in {"anime", "variety", "documentary", "sports", "mv", "music"}:
            return category, "站点分类或标签"
        key = (torrent.title or "", torrent.description or "", torrent.category or "")
        if key not in cache:
            try:
                cache[key] = self.chain.recognize_media(meta=MetaInfo(torrent.title or ""))
            except Exception as err:
                logger.warning(f"下载类型媒体识别失败 {torrent.title}：{type(err).__name__}")
                cache[key] = None
        media = cache[key]
        if media:
            ids = {str(value) for value in (getattr(media, "genre_ids", None) or [])}
            names = []
            for genre in getattr(media, "genres", None) or []:
                ids.add(str(genre.get("id") if isinstance(genre, dict) else getattr(genre, "id", "")))
                names.append(genre.get("name") if isinstance(genre, dict) else getattr(genre, "name", genre))
            explicit = self._explicit_download_type(names)
            for kind, genre_ids in (("anime", {"16"}), ("documentary", {"99"}),
                                    ("variety", {"10764", "10767"})):
                if ids & genre_ids or explicit == kind:
                    return kind, "媒体题材"
            media_type = getattr(media, "type", None)
            media_type = getattr(media_type, "value", media_type)
            basic = self._explicit_download_type([media_type])
            if basic:
                return basic, "媒体识别"
        if category:
            return category, "站点分类（媒体识别不可用）"
        return None, "站点分类及媒体识别均不可用"

    @staticmethod
    def _torrent_to_task_record(torrent: TorrentInfo, site: Any, task: BrushTaskConfig) -> dict:
        """把站点候选种子转换为可持久化的任务记录"""
        return {
            "task_id": task.id,
            "task_name": task.name,
            "spec_dedup_enabled": task.spec_dedup_enabled,
            "site": site.id,
            "site_name": site.name,
            "title": torrent.title,
            "size": torrent.size,
            "pubdate": torrent.pubdate,
            "description": torrent.description,
            "page_url": torrent.page_url,
            "date_elapsed": torrent.date_elapsed,
            "freedate": torrent.freedate,
            "uploadvolumefactor": torrent.uploadvolumefactor,
            "downloadvolumefactor": torrent.downloadvolumefactor,
            "hit_and_run": torrent.hit_and_run or task.site_hr_active,
            "volume_factor": torrent.volume_factor,
            "freedate_diff": torrent.freedate_diff,
            "ratio": 0,
            "downloaded": 0,
            "uploaded": 0,
            "seeding_time": 0,
            "deleted": False,
            "time": time.time(),
        }

    @staticmethod
    def _normalize_torrent_title(title: str) -> str:
        """按种子黑名单规则规范化完整标题，不忽略大小写。"""
        return normalize("NFC", str(title or "").strip())

    @staticmethod
    def _blacklist_other_site_names(task: BrushTaskConfig) -> Optional[Set[str]]:
        """取当前任务站点之外的已选站点名称。"""
        selected_ids = set(task.blacklist_site_ids or [])
        if task.site_id not in selected_ids:
            return set()
        names = set()
        for site_id in selected_ids - {task.site_id}:
            try:
                site = SiteOper().get(site_id)
                if not site or not site.name:
                    logger.error(f"种子黑名单站点 {site_id} 不存在或缺少名称")
                    return None
                names.add(normalize("NFC", str(site.name).strip()))
            except Exception as err:
                logger.error(f"读取种子黑名单站点 {site_id} 失败：{err}")
                return None
        return names

    @classmethod
    def _is_blacklist_duplicate(
        cls,
        title: str,
        other_site_names: Set[str],
        history_cache: Dict[str, Optional[Set[str]]],
    ) -> Tuple[bool, str]:
        """按站点名称与完整种子标题精确匹配其他限定站点的下载历史。"""
        normalized_title = cls._normalize_torrent_title(title)
        if not normalized_title:
            return True, "种子标题为空，无法验证种子黑名单"
        if normalized_title not in history_cache:
            sites = set()
            page_number = 1
            history_oper = DownloadHistoryOper()
            try:
                while True:
                    records, total = history_oper.query(
                        DownloadHistoryFilter(text=normalized_title),
                        QueryPageRequest(page=page_number, count=100),
                    )
                    sites.update(
                        normalize("NFC", str(record.torrent_site or "").strip())
                        for record in records
                        if cls._normalize_torrent_title(record.torrent_name) == normalized_title
                    )
                    if len(records) < 100 or page_number * 100 >= total:
                        break
                    page_number += 1
            except Exception as err:
                logger.error(f"种子黑名单下载历史查询失败，按排除处理：{err}")
                history_cache[normalized_title] = None
                return True, "种子黑名单历史查询失败"
            history_cache[normalized_title] = sites
        if history_cache[normalized_title] is None:
            return True, "种子黑名单历史查询失败"
        matched_site = next(
            (site_name for site_name in other_site_names if site_name in history_cache[normalized_title]),
            None,
        )
        if matched_site:
            return True, f"种子黑名单命中：已在限定站点 {matched_site} 下载过同名种子"
        return False, ""

    @staticmethod
    def _new_run_report(kind: str) -> dict:
        """创建一条结构稳定的运行诊断记录"""
        return {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "started_at": BrushFlow._now_iso(),
            "finished_at": None,
            "success": None,
            "result": None,
            "error": None,
            "source_count": 0,
            "subscription_excluded": 0,
            "blacklist_excluded": 0,
            "candidate_count": 0,
            "filtered_count": 0,
            "added_count": 0,
            "deleted_count": 0,
            "active_count": 0,
            "reason_counts": Counter(),
            "added_titles": [],
        }

    @staticmethod
    def _now_iso() -> str:
        """返回带本地时区且精确到秒的时间文本"""
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def __evaluate_size_condition_for_brush(
        self,
        torrents_size: float,
        add_torrent_size: float = 0.0,
        global_torrents_size: Optional[float] = None,
    ) -> Tuple[bool, Optional[str]]:
        """校验当前任务及所有任务新增种子后是否超过保种体积。"""
        task = self._get_task_config()
        if not task:
            return False, "任务配置不存在"
        estimated_size = torrents_size + (add_torrent_size or 0)
        if task.disksize:
            limit_size = float(task.disksize) * 1024 ** 3
            if estimated_size > limit_size:
                reason = (
                    f"预计做种体积 {self.__bytes_to_gb(estimated_size):.1f} GB，"
                    f"超过任务保种上限 {task.disksize} GB"
                )
                return False, reason
        global_disksize = getattr(self, "_global_disksize", None)
        if global_disksize:
            if global_torrents_size is None:
                global_torrents_size = self._calculate_global_seeding_size()
            estimated_global_size = global_torrents_size + (add_torrent_size or 0)
            if estimated_global_size > float(global_disksize) * 1024 ** 3:
                reason = (
                    f"预计全局做种体积 {self.__bytes_to_gb(estimated_global_size):.1f} GB，"
                    f"超过全局保种上限 {global_disksize} GB"
                )
                return False, reason
        return True, None

    def __evaluate_pre_conditions_for_brush(
        self,
        include_network_conditions: bool = True,
    ) -> Tuple[bool, Optional[str]]:
        """校验单任务与全局下载并发及上传下载带宽。"""
        task = self._get_task_config()
        if not task:
            return False, "任务配置不存在"
        global_maxdlcount = getattr(self, "_global_maxdlcount", None)
        if global_maxdlcount and self.__get_global_downloading_count() >= int(global_maxdlcount):
            return False, f"全局同时下载任务数达到上限 {global_maxdlcount}"
        if task.maxdlcount and self.__get_downloading_count() >= int(task.maxdlcount):
            return False, f"同时下载任务数达到上限 {task.maxdlcount}"
        if not include_network_conditions:
            return True, None
        avg_upload_speed, avg_download_speed = self.__get_average_bandwidth()
        if avg_upload_speed is None or avg_download_speed is None:
            return True, None
        global_maxupspeed = getattr(self, "_global_maxupspeed", None)
        global_maxdlspeed = getattr(self, "_global_maxdlspeed", None)
        if global_maxupspeed and avg_upload_speed >= float(global_maxupspeed) * 1024:
            return False, f"全局总上传带宽达到上限 {global_maxupspeed} KB/s"
        if global_maxdlspeed and avg_download_speed >= float(global_maxdlspeed) * 1024:
            return False, f"全局总下载带宽达到上限 {global_maxdlspeed} KB/s"
        if task.maxupspeed and avg_upload_speed >= float(task.maxupspeed) * 1024:
            return False, f"总上传带宽达到上限 {task.maxupspeed} KB/s"
        if task.maxdlspeed and avg_download_speed >= float(task.maxdlspeed) * 1024:
            return False, f"总下载带宽达到上限 {task.maxdlspeed} KB/s"
        return True, None

    def __evaluate_conditions_for_brush(
        self,
        torrent: TorrentInfo,
        torrent_tasks: Dict[str, dict],
    ) -> Tuple[bool, Optional[str]]:
        """按原有促销、H&R、规则、体积、人数和发布时间筛选候选"""
        task = self._get_task_config()
        if not task:
            return False, "任务配置不存在"
        task_key = f"{torrent.site_name}{torrent.title}"
        if any(task_key == f"{item.get('site_name')}{item.get('title')}" for item in torrent_tasks.values()):
            return False, "重复种子"
        if torrent.page_url:
            page_key = f"{torrent.site_name}{torrent.page_url}"
            if any(page_key == f"{item.get('site_name')}{item.get('page_url')}" for item in torrent_tasks.values()):
                return False, "重复种子"
        if torrent.title and any(
            torrent.site_name != item.get("site_name")
            and torrent.title == item.get("title")
            and not item.get("deleted")
            and not self._torrent_complete(item)
            for item in torrent_tasks.values()
        ):
            return False, "其他站点存在尚未下载完成的相同种子"
        if task.freeleech and torrent.downloadvolumefactor != 0:
            return False, "非免费种子"
        if task.freeleech == "2xfree" and torrent.uploadvolumefactor != 2:
            return False, "非双倍上传种子"
        if task.hr == "yes" and torrent.hit_and_run:
            return False, "存在 H&R"
        if task.include:
            include_match = bool(
                (torrent.title and re.search(task.include, torrent.title, re.I))
                or (torrent.description and re.search(task.include, torrent.description, re.I))
            )
            if not include_match:
                return False, "不符合包含规则"
        if task.exclude:
            exclude_match = bool(
                (torrent.title and re.search(task.exclude, torrent.title, re.I))
                or (torrent.description and re.search(task.exclude, torrent.description, re.I))
            )
            if exclude_match:
                return False, "符合排除规则"
        if task.size:
            size_range = [float(value) * 1024 ** 3 for value in task.size.split("-")]
            if len(size_range) == 1 and torrent.size < size_range[0]:
                return False, "种子大小低于下限"
            if len(size_range) > 1 and not size_range[0] <= torrent.size <= size_range[1]:
                return False, "种子大小不在范围内"
        if task.seeder:
            seeder_range = [float(value) for value in task.seeder.split("-")]
            seeders = torrent.seeders or 0
            if len(seeder_range) == 1 and seeders > seeder_range[0]:
                return False, "做种人数超过上限"
            if len(seeder_range) > 1 and not seeder_range[0] <= seeders <= seeder_range[1]:
                return False, "做种人数不在范围内"
        if task.pubtime:
            pubdate_minutes = self.__get_pubminutes(torrent.pubdate) - task.timezone_offset * 60
            pubtime_range = [float(value) for value in task.pubtime.split("-")]
            if len(pubtime_range) == 1 and pubdate_minutes > pubtime_range[0]:
                return False, "发布时间超过上限"
            if len(pubtime_range) > 1 and not pubtime_range[0] <= pubdate_minutes <= pubtime_range[1]:
                return False, "发布时间不在范围内"
        return True, None

    def check(self, task_id: Optional[str] = None, wait_for_lock: bool = False) -> None:
        """执行状态同步、删种和归档，到期检查可等待同任务的当前操作"""
        task = self._get_task_config(task_id)
        if not task or not self.get_state() or not task.enabled:
            return
        task_lock = self._task_locks.setdefault(task.id, threading.Lock())
        if not task_lock.acquire(blocking=wait_for_lock):
            logger.info(f"刷流任务 [{task.name}] 已有操作执行中，本轮检查跳过")
            return
        report = self._new_run_report("check")
        self._set_runtime(task.id, state="running", operation="check", last_error=None)
        try:
            with self._task_scope(task.id):
                self._run_check(task, report)
            report["success"] = report.get("result") not in {"downloader_unavailable", "downloader_error"}
        except Exception as err:
            report.update({"success": False, "error": str(err)})
            self._set_runtime(task.id, last_error=str(err))
            logger.error(f"刷流任务 [{task.name}] 检查失败：{str(err)}")
        finally:
            task_lock.release()
        if self._global_dynamic_delete_enabled():
            try:
                global_deleted_count = self._run_global_dynamic_delete()
                report["global_deleted_count"] = global_deleted_count
                report["deleted_count"] = report.get("deleted_count", 0) + global_deleted_count
            except Exception as err:
                report.update({"success": False, "error": str(err)})
                self._set_runtime(task.id, last_error=str(err))
                logger.error(f"全局动态删种失败：{str(err)}")
        report["finished_at"] = self._now_iso()
        self._append_run(task.id, report)
        self._set_runtime(task.id, state="idle", operation=None)

    def _run_check(self, task: BrushTaskConfig, report: dict) -> None:
        """在已绑定任务上下文中执行刷流种子检查"""
        if not self._validate_task_reference(task) or not self.downloader:
            report["result"] = "downloader_unavailable"
            return
        torrent_tasks: Dict[str, dict] = self._current_task_data("torrents", {})
        unmanaged_tasks: Dict[str, dict] = self._current_task_data("unmanaged", {})
        downloader = self.downloader
        seeding_torrents, error = downloader.get_torrents()
        if error:
            report["result"] = "downloader_error"
            raise RuntimeError("连接下载器出错")
        seeding_torrents_dict = {self.__get_hash(torrent): torrent for torrent in seeding_torrents}
        self.__update_seeding_tasks_based_on_tags(torrent_tasks, unmanaged_tasks, seeding_torrents_dict)
        check_hashes = list(torrent_tasks.keys())
        if not check_hashes:
            self._cleanup_unused_task_tag(task, torrents=seeding_torrents)
            report.update({"result": "no_managed_torrents", "active_count": 0})
            self._recalculate_statistics(task.id)
            return
        check_torrents = [seeding_torrents_dict[item] for item in check_hashes if item in seeding_torrents_dict]
        self.__update_torrent_tasks_state(check_torrents, torrent_tasks)
        self._record_completed_resources(task, check_torrents, torrent_tasks)
        self.__update_undeleted_torrents_missing_in_downloader(torrent_tasks, check_hashes, seeding_torrents)
        filtered_torrents = self.__filter_torrents_by_tag(check_torrents, task.delete_except_tags)
        if self._global_dynamic_delete_enabled():
            need_delete_hashes = []
        elif task.proxy_delete and task.delete_size_range:
            need_delete_hashes = self.__delete_torrent_for_proxy(filtered_torrents, torrent_tasks)
        else:
            need_delete_hashes = self.__delete_torrent_for_evaluate_conditions(filtered_torrents, torrent_tasks)
        need_delete_hashes = list(dict.fromkeys(need_delete_hashes or []))
        deleted_from_downloader = False
        if need_delete_hashes:
            self._record_completed_resources(task, [item for item in check_torrents
                if self.__get_hash(item) in need_delete_hashes], torrent_tasks, force=True)
            if DownloaderHelper().is_downloader("qbittorrent", service=self.service_info):
                self.__qb_torrents_reannounce(need_delete_hashes)
            if downloader.delete_torrents(ids=need_delete_hashes, delete_file=True):
                deleted_from_downloader = True
                for torrent_hash in need_delete_hashes:
                    if torrent_hash in torrent_tasks:
                        torrent_tasks[torrent_hash]["deleted"] = True
                        torrent_tasks[torrent_hash]["deleted_time"] = time.time()
        self.__auto_archive_tasks(torrent_tasks)
        self._cleanup_unused_task_tag(
            task,
            torrents=None if deleted_from_downloader else seeding_torrents,
        )
        self._save_current_task_data("torrents", torrent_tasks)
        self._recalculate_statistics(task.id)
        report.update(
            {
                "result": "completed",
                "deleted_count": len(need_delete_hashes),
                "active_count": sum(1 for item in torrent_tasks.values() if not item.get("deleted")),
            }
        )

    def __update_torrent_tasks_state(self, torrents: List[Any], torrent_tasks: Dict[str, dict]) -> None:
        """更新当前任务种子的上下传、分享率和做种时间"""
        for torrent in torrents:
            torrent_hash = self.__get_hash(torrent)
            torrent_task = torrent_tasks.get(torrent_hash)
            if not torrent_task:
                continue
            torrent_info = self.__get_torrent_info(torrent)
            torrent_task.update(
                {
                    "downloaded": torrent_info.get("downloaded"),
                    "progress": torrent_info.get("progress"),
                    "uploaded": torrent_info.get("uploaded"),
                    "ratio": torrent_info.get("ratio"),
                    "seeding_time": torrent_info.get("seeding_time"),
                }
            )

    def __update_seeding_tasks_based_on_tags(
        self,
        torrent_tasks: Dict[str, dict],
        unmanaged_tasks: Dict[str, dict],
        seeding_torrents_dict: Dict[str, Any],
    ) -> None:
        """按任务唯一标签同步 qBittorrent 中的纳管和移除状态"""
        task = self._get_task_config()
        if not task or not DownloaderHelper().is_downloader("qbittorrent", service=self.service_info):
            return
        added_tasks: List[dict] = []
        removed_tasks: List[dict] = []
        reset_tasks: List[dict] = []
        for torrent_hash, torrent in seeding_torrents_dict.items():
            tags = self.__get_label(torrent)
            has_unique_tag = task.brush_tag in tags
            has_global_tag = self.GLOBAL_BRUSH_TAG in tags
            existing = torrent_hash in torrent_tasks
            adopt_legacy = (
                has_global_tag
                and not existing
                and self._is_primary_task_for_torrent(task, torrent)
            )
            managed = has_unique_tag or (has_global_tag and existing) or adopt_legacy
            if managed:
                if not existing:
                    torrent_task = unmanaged_tasks.pop(torrent_hash, None) or self.__convert_torrent_info_to_task(torrent)
                    torrent_task.update({"task_id": task.id, "task_name": task.name})
                    torrent_tasks[torrent_hash] = torrent_task
                    added_tasks.append(torrent_task)
                elif torrent_tasks[torrent_hash].get("deleted"):
                    torrent_tasks[torrent_hash]["deleted"] = False
                    torrent_tasks[torrent_hash].pop("deleted_time", None)
                    reset_tasks.append(torrent_tasks[torrent_hash])
            elif existing:
                unmanaged_tasks[torrent_hash] = torrent_tasks.pop(torrent_hash)
                removed_tasks.append(unmanaged_tasks[torrent_hash])
        self._save_current_task_data("torrents", torrent_tasks)
        self._save_current_task_data("unmanaged", unmanaged_tasks)
        if added_tasks:
            self.__log_and_send_torrent_task_update_message(
                "【刷流任务种子加入】", "纳入刷流管理", "刷流任务标签匹配", added_tasks
            )
        if removed_tasks:
            self.__log_and_send_torrent_task_update_message(
                "【刷流任务种子移除】", "移除刷流管理", "刷流任务标签移除", removed_tasks
            )
        if reset_tasks:
            self.__log_and_send_torrent_task_update_message(
                "【刷流任务状态更新】", "恢复为正常", "下载器中仍存在对应种子", reset_tasks
            )

    def _is_primary_task_for_torrent(self, task: BrushTaskConfig, torrent: Any) -> bool:
        """仅让同站点第一项任务接管没有唯一标签的旧版刷流种子"""
        site_id, _ = self.__get_site_by_torrent(torrent)
        if site_id != task.site_id:
            return False
        site_tasks = [item for item in self._task_configs.values() if item.site_id == site_id]
        return bool(site_tasks and site_tasks[0].id == task.id)

    def __evaluate_conditions_for_delete(
        self,
        torrent_info: dict,
        torrent_task: dict,
    ) -> Tuple[bool, str]:
        """评估普通与 H&R 种子的原有删除条件"""
        task = self._get_task_config()
        if not task:
            return False, "任务配置不存在"
        hit_and_run = bool(torrent_task.get("hit_and_run"))
        if hit_and_run and (task.hr_seed_time or task.seed_ratio):
            if task.hr_seed_time and torrent_info.get("seeding_time", 0) >= float(task.hr_seed_time) * 3600:
                return True, f"H&R 做种时间达到 {task.hr_seed_time} 小时"
            if (
                task.seed_ratio
                and self._torrent_complete(torrent_info)
                and torrent_info.get("ratio", 0) >= float(task.seed_ratio)
            ):
                return True, f"H&R 分享率达到 {task.seed_ratio}"
            return False, "H&R 种子尚未满足删除条件"
        promotion_expired, promotion_reason = self.__promotion_expired(torrent_info, torrent_task)
        if promotion_expired:
            return True, promotion_reason
        if task.seed_time and torrent_info.get("seeding_time", 0) >= float(task.seed_time) * 3600:
            return True, f"做种时间达到 {task.seed_time} 小时"
        if (
            task.seed_ratio
            and self._torrent_complete(torrent_info)
            and torrent_info.get("ratio", 0) >= float(task.seed_ratio)
        ):
            return True, f"分享率达到 {task.seed_ratio}"
        if task.seed_size and torrent_info.get("uploaded", 0) >= float(task.seed_size) * 1024 ** 3:
            return True, f"上传量达到 {task.seed_size} GB"
        if (
            task.download_time
            and not self._torrent_complete(torrent_info)
            and torrent_info.get("dltime", 0) >= float(task.download_time) * 3600
        ):
            return True, f"下载耗时达到 {task.download_time} 小时"
        if (
            task.seed_avgspeed
            and torrent_info.get("avg_upspeed", 0) <= float(task.seed_avgspeed) * 1024
            and torrent_info.get("seeding_time", 0) >= 30 * 60
        ):
            return True, f"平均上传速度低于 {task.seed_avgspeed} KB/s"
        if task.seed_inactivetime and torrent_info.get("iatime", 0) >= float(task.seed_inactivetime) * 60:
            return True, f"未活动时间达到 {task.seed_inactivetime} 分钟"
        return False, "尚未满足删除条件"

    def __promotion_expired(self, torrent_info: dict, torrent_task: dict) -> Tuple[bool, str]:
        """判断免费促销是否结束且种子仍未完成下载"""
        task = self._get_task_config()
        if (
            not task
            or not task.del_no_free
            or self._torrent_complete(torrent_info)
        ):
            return False, ""
        expiry = self._promotion_expiry_at(torrent_task.get("freedate"), task.timezone_offset)
        if not expiry:
            return False, ""
        expired = datetime.now(expiry.tzinfo) >= expiry
        return expired, "促销已过期" if expired else ""

    def __delete_torrent_for_evaluate_conditions(
        self,
        torrents: List[Any],
        torrent_tasks: Dict[str, dict],
        dynamic: bool = False,
    ) -> List[str]:
        """找出满足用户删除条件的种子并发送对应通知"""
        delete_hashes: List[str] = []
        for torrent in torrents:
            torrent_hash = self.__get_hash(torrent)
            torrent_task = torrent_tasks.get(torrent_hash)
            if not torrent_task:
                continue
            torrent_info = self.__get_torrent_info(torrent)
            should_delete, reason = self.__evaluate_conditions_for_delete(torrent_info, torrent_task)
            if not should_delete:
                continue
            delete_hashes.append(torrent_hash)
            if dynamic:
                reason = f"触发动态删除阈值，{reason}"
            self.__send_delete_message(torrent_task, reason)
            logger.info(f"刷流任务删除种子：{torrent_task.get('title')}，原因：{reason}")
        return delete_hashes

    def __delete_torrent_for_evaluate_proxy_pre_conditions(
        self,
        torrents: List[Any],
        torrent_tasks: Dict[str, dict],
    ) -> List[str]:
        """动态删种前优先清理促销过期或下载超时的非 H&R 种子"""
        task = self._get_task_config()
        delete_hashes: List[str] = []
        for torrent in torrents:
            torrent_hash = self.__get_hash(torrent)
            torrent_task = torrent_tasks.get(torrent_hash)
            if not task or not torrent_task or torrent_task.get("hit_and_run"):
                continue
            torrent_info = self.__get_torrent_info(torrent)
            expired, reason = self.__promotion_expired(torrent_info, torrent_task)
            timed_out = bool(
                task.download_time
                and not self._torrent_complete(torrent_info)
                and torrent_info.get("dltime", 0) >= float(task.download_time) * 3600
            )
            if not expired and not timed_out:
                continue
            if timed_out and not reason:
                reason = f"下载耗时达到 {task.download_time} 小时"
            delete_hashes.append(torrent_hash)
            self.__send_delete_message(torrent_task, reason)
        return delete_hashes

    @staticmethod
    def _select_global_dynamic_deletions(
        candidates: List[dict],
        total_size: float,
        min_size: float,
        max_size: float,
    ) -> Tuple[List[dict], float, bool]:
        """按 V4 优先级从跨任务候选中生成全局动态删种计划"""
        selected: List[dict] = []
        selected_keys: Set[Tuple[str, str]] = set()
        remaining_size = total_size

        def select(
            candidate: dict,
            reason: str,
            reason_field: Optional[str] = None,
            dynamic_reason: bool = False,
        ) -> None:
            """把未选候选加入计划并扣减预计做种体积"""
            nonlocal remaining_size
            candidate_key = (
                candidate["downloader_name"],
                candidate["torrent_hash"],
            )
            if candidate_key in selected_keys:
                return
            selected_keys.add(candidate_key)
            task_delete_reasons: Dict[str, str] = {}
            for task, _ in candidate.get(
                "associated_records",
                [(candidate["task"], candidate.get("torrent_task"))],
            ):
                task_reason = (
                    candidate.get("task_condition_reasons", {}).get(task.id, {}).get(reason_field)
                    if reason_field
                    else None
                ) or reason
                if dynamic_reason:
                    task_reason = f"触发全局动态删除阈值，{task_reason}"
                task_delete_reasons[task.id] = task_reason
            selected.append(
                {
                    **candidate,
                    "delete_reason": reason,
                    "task_delete_reasons": task_delete_reasons,
                }
            )
            remaining_size = max(remaining_size - float(candidate.get("size") or 0), 0)

        for candidate in candidates:
            if candidate.get("pre_delete_reason"):
                select(
                    candidate,
                    candidate["pre_delete_reason"],
                    reason_field="pre_delete_reason",
                )

        threshold_triggered = remaining_size >= max_size
        if not threshold_triggered:
            return selected, remaining_size, False

        for candidate in candidates:
            if remaining_size <= min_size:
                break
            if not candidate.get("proxy_delete") and candidate.get("conditional_reason"):
                select(
                    candidate,
                    candidate["conditional_reason"],
                    reason_field="conditional_reason",
                )

        if remaining_size > min_size:
            for candidate in candidates:
                if remaining_size <= min_size:
                    break
                if candidate.get("proxy_delete") and candidate.get("conditional_reason"):
                    select(
                        candidate,
                        f"触发全局动态删除阈值，{candidate['conditional_reason']}",
                        reason_field="conditional_reason",
                        dynamic_reason=True,
                    )

        fallback_candidates = sorted(
            (
                candidate
                for candidate in candidates
                if candidate.get("proxy_delete")
                and candidate.get("completed")
                and not candidate.get("hit_and_run")
            ),
            key=lambda item: item.get("seeding_time", 0),
            reverse=True,
        )
        for candidate in fallback_candidates:
            if remaining_size <= min_size:
                break
            select(candidate, "触发全局动态删除阈值，系统按做种时间清理")

        return selected, remaining_size, True

    def _collect_global_dynamic_delete_candidates(
        self,
    ) -> Tuple[List[dict], float, Dict[str, Dict[str, dict]], Dict[str, ServiceInfo]]:
        """汇总启用任务的最新下载器状态、做种体积和全局删种候选"""
        candidate_rows: Dict[Tuple[str, str], List[dict]] = {}
        total_size = 0.0
        task_records: Dict[str, Dict[str, dict]] = {}
        services: Dict[str, ServiceInfo] = {}
        downloader_cache: Dict[str, Tuple[ServiceInfo, List[Any]]] = {}
        counted_torrents: Set[Tuple[str, str]] = set()
        associated_records: Dict[Tuple[str, str], List[Tuple[BrushTaskConfig, dict]]] = {}
        downloader_helper = DownloaderHelper()

        for task in self._task_configs.values():
            torrent_tasks: Dict[str, dict] = self._get_task_data(task.id, "torrents") or {}
            task_records[task.id] = torrent_tasks
            for torrent_hash, torrent_task in torrent_tasks.items():
                if torrent_task.get("deleted"):
                    continue
                torrent_key = (task.downloader, torrent_hash)
                associated_records.setdefault(torrent_key, []).append((task, torrent_task))

        for task in self._task_configs.values():
            if not task.enabled:
                continue
            torrent_tasks = task_records[task.id]
            if task.downloader not in downloader_cache:
                service = downloader_helper.get_service(name=task.downloader)
                if not service or not service.instance or service.instance.is_inactive():
                    raise RuntimeError(
                        f"全局动态删种无法获取下载器 [{task.downloader}] 实时状态，本轮已中止"
                    )
                torrents, error = service.instance.get_torrents()
                if error:
                    raise RuntimeError(
                        f"全局动态删种获取下载器 [{task.downloader}] 种子失败，本轮已中止"
                    )
                downloader_cache[task.downloader] = (service, torrents or [])
                services[task.downloader] = service

            service, downloader_torrents = downloader_cache[task.downloader]
            with self._task_scope(task.id):
                downloader_torrent_map: Dict[str, Any] = {}
                for torrent in downloader_torrents:
                    torrent_hash = self.__get_hash(torrent)
                    if torrent_hash:
                        downloader_torrent_map[torrent_hash] = torrent
                check_hashes = list(torrent_tasks)
                check_torrents = [
                    downloader_torrent_map[torrent_hash]
                    for torrent_hash in check_hashes
                    if torrent_hash in downloader_torrent_map
                ]
                self.__update_torrent_tasks_state(check_torrents, torrent_tasks)
                self._record_completed_resources(task, check_torrents, torrent_tasks)
                self.__update_undeleted_torrents_missing_in_downloader(
                    torrent_tasks,
                    check_hashes,
                    downloader_torrents,
                )
                self._save_task_data(task.id, "torrents", torrent_tasks)
                for torrent in check_torrents:
                    torrent_hash = self.__get_hash(torrent)
                    torrent_task = torrent_tasks.get(torrent_hash)
                    if not torrent_task or torrent_task.get("deleted"):
                        continue
                    torrent_key = (task.downloader, torrent_hash)
                    if torrent_key not in counted_torrents:
                        torrent_info = self.__get_torrent_info(torrent)
                        total_size += float(
                            torrent_info.get("total_size") or torrent_task.get("size") or 0
                        )
                        counted_torrents.add(torrent_key)

                filtered_torrents = self.__filter_torrents_by_tag(check_torrents, task.delete_except_tags)
                for torrent in filtered_torrents:
                    torrent_hash = self.__get_hash(torrent)
                    torrent_task = torrent_tasks.get(torrent_hash)
                    if not torrent_task or torrent_task.get("deleted"):
                        continue
                    torrent_key = (task.downloader, torrent_hash)
                    torrent_info = self.__get_torrent_info(torrent)
                    pre_delete_reason = ""
                    if not torrent_task.get("hit_and_run"):
                        expired, expired_reason = self.__promotion_expired(torrent_info, torrent_task)
                        timed_out = bool(
                            task.download_time
                            and not self._torrent_complete(torrent_info)
                            and torrent_info.get("dltime", 0) >= float(task.download_time) * 3600
                        )
                        if expired:
                            pre_delete_reason = expired_reason
                        elif timed_out:
                            pre_delete_reason = f"下载耗时达到 {task.download_time} 小时"
                    should_delete, conditional_reason = self.__evaluate_conditions_for_delete(
                        torrent_info,
                        torrent_task,
                    )
                    torrent_size = float(torrent_info.get("total_size") or torrent_task.get("size") or 0)
                    candidate_rows.setdefault(torrent_key, []).append(
                        {
                            "task": task,
                            "live_torrent": torrent,
                            "torrent_hash": torrent_hash,
                            "torrent_task": torrent_task,
                            "downloader_name": task.downloader,
                            "size": torrent_size,
                            "pre_delete_reason": pre_delete_reason,
                            "conditional_reason": conditional_reason if should_delete else "",
                            "proxy_delete": task.proxy_delete,
                            "completed": self._torrent_complete(torrent_info),
                            "hit_and_run": bool(torrent_task.get("hit_and_run")),
                            "seeding_time": torrent_info.get("seeding_time", 0),
                        }
                    )

        candidates: List[dict] = []
        for torrent_key, rows in candidate_rows.items():
            associations = associated_records.get(torrent_key, [])
            if len(rows) != len(associations):
                continue
            candidate = dict(rows[0])
            candidate.update(
                {
                    "associated_records": associations,
                    "proxy_delete": all(row["proxy_delete"] for row in rows),
                    "completed": all(row["completed"] for row in rows),
                    "hit_and_run": any(row["hit_and_run"] for row in rows),
                    "pre_delete_reason": (
                        rows[0]["pre_delete_reason"]
                        if all(row["pre_delete_reason"] for row in rows)
                        else ""
                    ),
                    "conditional_reason": (
                        rows[0]["conditional_reason"]
                        if all(row["conditional_reason"] for row in rows)
                        else ""
                    ),
                    "seeding_time": max(row["seeding_time"] for row in rows),
                    "task_condition_reasons": {
                        row["task"].id: {
                            "pre_delete_reason": row["pre_delete_reason"],
                            "conditional_reason": row["conditional_reason"],
                        }
                        for row in rows
                    },
                }
            )
            candidates.append(candidate)
        return candidates, total_size, task_records, services

    def _send_global_dynamic_delete_summary(
        self,
        deleted_entries: List[dict],
        remaining_size: float,
    ) -> None:
        """按受影响任务通知开关发送全局区间删种汇总"""
        notified_tasks = {
            task.id: task
            for entry in deleted_entries
            for task, _ in entry.get(
                "associated_records",
                [(entry["task"], entry.get("torrent_task"))],
            )
            if task.notify
        }
        if not notified_tasks:
            return
        task_names = "、".join(task.name for task in notified_tasks.values())
        self.post_message(
            mtype=NotificationType.SiteMessage,
            title="【刷流任务全局动态删除】",
            text=(
                f"任务：{task_names}\n"
                f"删除：{len(deleted_entries)} 个种子\n"
                f"当前做种：{self.__bytes_to_gb(remaining_size):.1f} GB"
            ),
        )

    def _run_global_dynamic_delete(self) -> int:
        """串行执行跨任务、跨下载器的全局动态删种并返回成功删除数"""
        if not self._global_dynamic_delete_enabled():
            return 0
        global_lock = getattr(self, "_global_delete_lock", None)
        if global_lock is None:
            self._global_delete_lock = threading.Lock()
            global_lock = self._global_delete_lock
        if not global_lock.acquire(blocking=False):
            logger.info("已有全局动态删种正在执行，本轮跳过")
            return 0

        try:
            with self._all_task_locks_scope():
                candidates, total_size, task_records, services = self._collect_global_dynamic_delete_candidates()
                limits = [
                    float(value) * 1024 ** 3
                    for value in str(self._global_delete_size_range).split("-")
                ]
                min_size = limits[0]
                max_size = limits[1] if len(limits) > 1 else limits[0]
                delete_plan, _, threshold_triggered = self._select_global_dynamic_deletions(
                    candidates,
                    total_size,
                    min_size,
                    max_size,
                )
                if not delete_plan:
                    if threshold_triggered:
                        logger.info(
                            f"全局做种体积 {self.__bytes_to_gb(total_size):.1f} GB 已达到动态删种上限，"
                            "但没有符合任务策略的可删除种子"
                        )
                    else:
                        logger.info(
                            f"全局做种体积 {self.__bytes_to_gb(total_size):.1f} GB，"
                            f"未达到动态删种上限 {self.__bytes_to_gb(max_size):.1f} GB"
                        )
                    return 0

                plan_by_downloader: Dict[str, List[dict]] = {}
                for entry in delete_plan:
                    plan_by_downloader.setdefault(entry["downloader_name"], []).append(entry)

                deleted_entries: List[dict] = []
                downloader_helper = DownloaderHelper()
                for downloader_name, entries in plan_by_downloader.items():
                    service = services.get(downloader_name)
                    if not service or not service.instance:
                        continue
                    torrent_hashes = list(dict.fromkeys(entry["torrent_hash"] for entry in entries))
                    for entry in entries:
                        for owner, row in entry.get("associated_records", []):
                            with self._task_scope(owner.id):
                                self._record_completed_resources(owner, [entry["live_torrent"]],
                                    {entry["torrent_hash"]: row}, force=True)
                    if downloader_helper.is_downloader("qbittorrent", service=service):
                        try:
                            if getattr(service.instance, "qbc", None):
                                service.instance.qbc.torrents_reannounce(torrent_hashes=torrent_hashes)
                        except Exception as err:
                            logger.warning(f"全局动态删种重新汇报下载器 [{downloader_name}] 失败：{str(err)}")
                    try:
                        if service.instance.delete_torrents(ids=torrent_hashes, delete_file=True):
                            deleted_entries.extend(entries)
                    except Exception as err:
                        logger.error(
                            f"全局动态删种调用下载器 [{downloader_name}] 删除失败：{str(err)}"
                        )

                deleted_at = time.time()
                affected_task_ids: Set[str] = set()
                recorded_entries: List[dict] = []
                notification_entries: List[Tuple[BrushTaskConfig, dict, str]] = []
                for entry in deleted_entries:
                    torrent_hash = entry["torrent_hash"]
                    entry_recorded = False
                    for task, _ in entry.get(
                        "associated_records",
                        [(entry["task"], entry.get("torrent_task"))],
                    ):
                        torrent_task = task_records.get(task.id, {}).get(torrent_hash)
                        if not torrent_task:
                            continue
                        delete_reason = entry.get("task_delete_reasons", {}).get(
                            task.id,
                            entry["delete_reason"],
                        )
                        torrent_task.update({"deleted": True, "deleted_time": deleted_at})
                        affected_task_ids.add(task.id)
                        notification_entries.append((task, torrent_task, delete_reason))
                        entry_recorded = True
                    if entry_recorded:
                        recorded_entries.append(entry)

                for affected_task_id in affected_task_ids:
                    self._save_task_data(
                        affected_task_id,
                        "torrents",
                        task_records[affected_task_id],
                    )
                    self._recalculate_statistics(affected_task_id)

                for task, torrent_task, delete_reason in notification_entries:
                    try:
                        with self._task_scope(task.id):
                            self.__send_delete_message(torrent_task, delete_reason)
                    except Exception as err:
                        logger.warning(f"全局动态删种发送任务 [{task.name}] 通知失败：{str(err)}")
                    logger.info(
                        f"全局动态删种删除任务 [{task.name}] 种子："
                        f"{torrent_task.get('title')}，原因：{delete_reason}"
                    )

                remaining_size = max(
                    total_size - sum(float(entry.get("size") or 0) for entry in deleted_entries),
                    0,
                )
                if threshold_triggered and len(limits) > 1 and recorded_entries:
                    try:
                        self._send_global_dynamic_delete_summary(recorded_entries, remaining_size)
                    except Exception as err:
                        logger.warning(f"全局动态删种发送汇总通知失败：{str(err)}")
                return len(recorded_entries)
        finally:
            global_lock.release()

    def __delete_torrent_for_proxy(
        self,
        torrents: List[Any],
        torrent_tasks: Dict[str, dict],
    ) -> List[str]:
        """按动态体积阈值执行前置、条件和兜底删种"""
        task = self._get_task_config()
        if not task or not task.proxy_delete or not task.delete_size_range:
            return []
        torrent_info_map = {
            self.__get_hash(torrent): self.__get_torrent_info(torrent) for torrent in torrents
        }
        total_size = self.__calculate_seeding_torrents_size(torrent_tasks)
        pre_delete_hashes = self.__delete_torrent_for_evaluate_proxy_pre_conditions(torrents, torrent_tasks)
        total_size -= sum(torrent_info_map[item].get("total_size", 0) for item in pre_delete_hashes if item in torrent_info_map)
        remaining_torrents = [torrent for torrent in torrents if self.__get_hash(torrent) not in pre_delete_hashes]
        limits = [float(value) * 1024 ** 3 for value in task.delete_size_range.split("-")]
        min_size = limits[0]
        max_size = limits[1] if len(limits) > 1 else limits[0]
        if total_size < max_size:
            return pre_delete_hashes
        delete_hashes = list(pre_delete_hashes)
        conditional_hashes = self.__delete_torrent_for_evaluate_conditions(
            remaining_torrents, torrent_tasks, dynamic=True
        )
        delete_hashes.extend(conditional_hashes)
        total_size -= sum(
            torrent_info_map[item].get("total_size", 0)
            for item in conditional_hashes
            if item in torrent_info_map
        )
        if total_size > min_size:
            remaining_hashes = [
                self.__get_hash(torrent)
                for torrent in remaining_torrents
                if self.__get_hash(torrent) not in delete_hashes
            ]
            completed = self.downloader.get_completed_torrents(ids=remaining_hashes)
            candidates = []
            for torrent in completed:
                torrent_hash = self.__get_hash(torrent)
                if torrent_tasks.get(torrent_hash, {}).get("hit_and_run"):
                    continue
                info = torrent_info_map.get(torrent_hash) or self.__get_torrent_info(torrent)
                candidates.append((torrent_hash, info))
            candidates.sort(key=lambda item: item[1].get("seeding_time", 0), reverse=True)
            for torrent_hash, torrent_info in candidates:
                if total_size <= min_size:
                    break
                delete_hashes.append(torrent_hash)
                total_size -= torrent_info.get("total_size", 0)
                torrent_task = torrent_tasks.get(torrent_hash, {})
                self.__send_delete_message(torrent_task, "触发动态删除阈值，系统按做种时间清理")
        if len(limits) > 1 and delete_hashes:
            self.__send_message(
                "【刷流任务动态删除】",
                f"任务：{task.name}\n删除：{len(delete_hashes)} 个种子\n当前做种：{self.__bytes_to_gb(total_size):.1f} GB",
            )
        return delete_hashes

    def __update_undeleted_torrents_missing_in_downloader(
        self,
        torrent_tasks: Dict[str, dict],
        torrent_check_hashes: List[str],
        torrents: List[Any],
    ) -> None:
        """把下载器中已不存在但仍标记正常的记录更新为已删除"""
        existing_hashes = set(self.__get_all_hashes(torrents))
        missing_hashes = [
            item for item in torrent_check_hashes
            if item not in existing_hashes and not torrent_tasks[item].get("deleted")
        ]
        deleted_tasks: List[dict] = []
        for torrent_hash in missing_hashes:
            torrent_task = torrent_tasks[torrent_hash]
            torrent_task.update({"deleted": True, "deleted_time": time.time()})
            deleted_tasks.append(torrent_task)
        if deleted_tasks:
            self.__log_and_send_torrent_task_update_message(
                "【刷流任务状态更新】", "更新为已删除", "下载器中找不到对应种子", deleted_tasks
            )

    def __convert_torrent_info_to_task(self, torrent: Any) -> dict:
        """把下载器种子转换为当前任务的托管记录"""
        torrent_info = self.__get_torrent_info(torrent)
        site_id, site_name = self.__get_site_by_torrent(torrent)
        task = self._get_task_config()
        return {
            "task_id": task.id if task else None,
            "task_name": task.name if task else None,
            "site": site_id,
            "site_name": site_name,
            "title": torrent_info.get("title", ""),
            "size": torrent_info.get("total_size", 0),
            "pubdate": None,
            "description": None,
            "page_url": None,
            "date_elapsed": None,
            "freedate": None,
            "uploadvolumefactor": None,
            "downloadvolumefactor": None,
            "hit_and_run": False,
            "volume_factor": None,
            "freedate_diff": None,
            "ratio": torrent_info.get("ratio", 0),
            "downloaded": torrent_info.get("downloaded", 0),
            "progress": torrent_info.get("progress"),
            "uploaded": torrent_info.get("uploaded", 0),
            "seeding_time": torrent_info.get("seeding_time", 0),
            "deleted": False,
            "time": torrent_info.get("add_on", time.time()),
        }

    @staticmethod
    def __get_redict_url(
        url: str,
        proxies: str = None,
        ua: str = None,
        cookie: str = None,
    ) -> Optional[str]:
        """解析带请求参数的跳转下载链接并返回真实地址"""
        match = re.search(r"\[(.*)](.*)", url)
        if not match:
            return None
        base64_str, request_url = match.group(1), match.group(2)
        if not base64_str:
            return request_url
        try:
            request_text = base64.b64decode(base64_str.encode("utf-8")).decode("utf-8")
            request_params: Dict[str, dict] = json.loads(request_text)
        except (ValueError, UnicodeDecodeError) as err:
            logger.error(f"解析种子跳转下载参数失败：{str(err)}")
            return None
        if not request_params.get("cookie"):
            cookie = None
        headers = request_params.get("header") or None
        request = RequestUtils(ua=ua, proxies=proxies, cookies=cookie, headers=headers)
        if request_params.get("method") == "get":
            response = request.get_res(request_url, params=request_params.get("params"))
        else:
            response = request.post_res(request_url, params=request_params.get("params"))
        if not response:
            return None
        result_path = request_params.get("result")
        if not result_path:
            return response.text
        data = response.json()
        success_key = request_params.get("success")
        if success_key and not data.get(success_key):
            return None
        for key in str(result_path).split("."):
            if not isinstance(data, dict):
                return None
            data = data.get(key)
            if data is None:
                return None
        result_url_path = request_params.get("result_path")
        result_query_param = request_params.get("result_query_param")
        if result_url_path and result_query_param:
            result_url = urljoin(
                f"{str(request_params.get('result_base_url')).rstrip('/')}/",
                str(result_url_path).lstrip("/"),
            )
            return f"{result_url}?{urlencode({result_query_param: data})}"
        return str(data)

    @staticmethod
    def __reset_download_url(torrent_url: str, site_id: int) -> str:
        """为支持的 NexusPHP 站点追加跳过下载提示参数"""
        try:
            if not torrent_url or torrent_url.startswith("magnet"):
                return torrent_url
            site = next(
                (item for item in SitesHelper().get_indexers() if item.get("id") == site_id),
                None,
            )
            if not site or site.get("name") in {"天空"} or not site.get("schema", "").startswith("Nexus"):
                return torrent_url
            parsed_url = urlparse(torrent_url)
            query_params = dict(parse_qsl(parsed_url.query))
            query_params["letdown"] = "1"
            return str(urlunparse(parsed_url._replace(query=urlencode(query_params))))
        except Exception as err:
            logger.error(f"处理种子下载提示地址失败：{str(err)}")
            return torrent_url

    def __download(self, torrent: TorrentInfo) -> Optional[str]:
        """按当前任务配置向 qBittorrent 或 Transmission 添加种子"""
        task = self._get_task_config()
        if not task or not torrent.enclosure:
            logger.error(f"获取种子下载链接失败：{torrent.title}")
            return None
        torrent_content: Union[str, bytes] = torrent.enclosure
        proxies = settings.PROXY if torrent.site_proxy else None
        cookies = torrent.site_cookie
        indirect_download = isinstance(torrent_content, str) and torrent_content.startswith("[")
        if indirect_download:
            torrent_content = self.__get_redict_url(
                torrent_content,
                proxies=proxies,
                ua=torrent.site_ua,
                cookie=cookies,
            )
            cookies = None
        if not torrent_content:
            return None
        if task.site_skip_tips and isinstance(torrent_content, str):
            torrent_content = self.__reset_download_url(torrent_content, torrent.site)
        if isinstance(torrent_content, str) and not torrent_content.startswith("magnet") and not indirect_download:
            # 站点可能返回登录页或首次下载确认页；交给主程序下载链校验并完成确认，避免把 HTML 传给下载器。
            download_torrent = copy.copy(torrent)
            download_torrent.enclosure = torrent_content
            download_torrent.site_cookie = cookies
            torrent_content, _, _ = DownloadChain().download_torrent(download_torrent)
            if not torrent_content:
                logger.error(f"刷流任务 [{task.name}] 获取有效种子文件失败：{torrent.title}")
                return None
        downloader = self.downloader
        service = self.service_info
        if not downloader or not service:
            return None
        if isinstance(torrent_content, str) and not torrent_content.startswith("magnet"):
            response = RequestUtils(cookies=cookies, proxies=proxies, ua=torrent.site_ua).get_res(
                url=torrent_content
            )
            if response is not None and response.ok:
                torrent_content = response.content
        expected_hash = TorrentHelper.get_torrent_hash(torrent_content)
        if not expected_hash:
            logger.warning(f"刷流任务 [{task.name}] 跳过种子 {torrent.title}：无法解析种子 Hash")
            return None
        if (self._resource_identity_deferred(task)
                and not parse_torrent_files(torrent_content)):
            self._pending_identity({"path": torrent.title, "specs": {}}, torrent.title, task,
                                   "未取得可核验的种子文件清单，不添加下载")
            logger.info(f"资源去重待核验 {torrent.title}：无法读取元数据，暂不添加下载")
            return None
        try:
            existing_torrents, query_error = downloader.get_torrents(ids=[expected_hash])
        except Exception as err:
            logger.warning(
                f"刷流任务 [{task.name}] 跳过种子 {torrent.title}：下载器 Hash 查询失败（{type(err).__name__}）"
            )
            return None
        if query_error:
            logger.warning(f"刷流任务 [{task.name}] 跳过种子 {torrent.title}：下载器 Hash 查询失败")
            return None
        if existing_torrents:
            logger.info(
                f"刷流任务 [{task.name}] 跳过重复种子 {torrent.title}："
                f"下载器已存在相同 Hash {expected_hash}，保留原任务"
            )
            return None
        recent_selection = None
        if task.recent_episodes_only:
            recent_selection = self._recent_episode_file_selection(torrent_content, torrent.title)
            if not recent_selection:
                return None
        if self._resource_mode(task) != "off":
            dedup_selection = self._channel_dedup_file_selection(
                torrent_content,
                torrent.title,
                accepted_paths=recent_selection.get("accepted") if recent_selection else None,
                spec_hint=" ".join([torrent.description or "", *(torrent.labels or [])]),
                task=task,
            )
            if dedup_selection and dedup_selection["excluded"]:
                if dedup_selection["reject_torrent"]:
                    logger.info(f"规格去重排除整包 {torrent.title}：存在不合格文件，按整包排除策略处理")
                    return None
                if recent_selection:
                    remaining = set(recent_selection.get("accepted") or []) - dedup_selection["excluded"]
                    if not remaining:
                        logger.info(f"频道同规格去重排除种子 {torrent.title}：{self._dedup_exclusion_summary(dedup_selection)}")
                        return None
                    recent_selection = {**recent_selection, "accepted": remaining}
                elif dedup_selection["video_count"] and len(dedup_selection["excluded"]) >= dedup_selection["video_count"]:
                    logger.info(f"频道同规格去重排除种子 {torrent.title}：{self._dedup_exclusion_summary(dedup_selection)}")
                    return None
                else:
                    recent_selection = {"accepted": dedup_selection["remaining_paths"]}
        with self._inflight_download_scope(
            torrent_content, torrent, task, expected_hash, recent_selection
        ) as reservation:
            if reservation["blocked"]:
                return None
            result = self._add_prechecked_torrent(
                torrent_content, cookies, task, downloader, service, expected_hash, reservation["selection"], torrent.title
            )
            reservation["added"] = bool(result)
            return result

    def _add_prechecked_torrent(
        self, torrent_content: Union[str, bytes], cookies: Optional[str], task: BrushTaskConfig,
        downloader: Any, service: ServiceInfo, expected_hash: str, recent_selection: Optional[dict], title: str,
    ) -> Optional[str]:
        """在去重及占位完成后保留原下载器添加、暂停选集和失败清理流程。"""
        up_speed = int(task.up_speed) if task.up_speed else None
        down_speed = int(task.dl_speed) if task.dl_speed else None
        downloader_helper = DownloaderHelper()
        if downloader_helper.is_downloader("qbittorrent", service=service):
            up_limit = up_speed * 1024 if up_speed else None
            down_limit = down_speed * 1024 if down_speed else None
            random_tag = StringUtils.generate_random_str(10)
            add_result = downloader.add_torrent(
                content=torrent_content,
                download_dir=task.save_path,
                cookie=cookies,
                category=task.qb_category,
                tag=[task.brush_tag, random_tag],
                upload_limit=up_limit,
                download_limit=down_limit,
                is_paused=bool(recent_selection),
            )
            added, added_hashes = self._qb_add_result(add_result)
            if not added:
                logger.warning(f"刷流任务 [{task.name}] 添加种子失败：{title}，不再等待 Hash")
                return None
            torrent_hash = (
                expected_hash if expected_hash in added_hashes
                else downloader.get_torrent_id_by_tag(tags=random_tag)
            )
            if not torrent_hash:
                logger.error(f"刷流任务 [{task.name}] 获取种子 Hash 失败")
                return torrent_hash
            if str(torrent_hash).lower() != expected_hash:
                logger.error(f"刷流任务 [{task.name}] 添加结果 Hash 与种子不一致，停止处理：{title}")
                return None
            if recent_selection and not self._select_recent_episode_files(
                downloader, torrent_hash, recent_selection, "qbittorrent", task.name
            ):
                try:
                    downloader.delete_torrents(ids=torrent_hash, delete_file=False)
                except Exception as err:
                    logger.error(f"移除选集失败的暂停任务失败：{err}")
                return None
            try:
                downloader.remove_torrents_tag(ids=torrent_hash, tag=random_tag)
            except Exception as err:
                logger.warning(f"刷流任务 [{task.name}] 移除随机标签失败：{str(err)}")
            return torrent_hash
        if downloader_helper.is_downloader("transmission", service=service):
            added_torrent = downloader.add_torrent(
                content=torrent_content,
                download_dir=task.save_path,
                cookie=cookies,
                labels=[task.brush_tag],
                is_paused=bool(recent_selection),
            )
            if not added_torrent:
                return None
            if recent_selection and not self._select_recent_episode_files(
                downloader, added_torrent.hashString, recent_selection, "transmission", task.name
            ):
                try:
                    downloader.delete_torrents(ids=added_torrent.hashString, delete_file=False)
                except Exception as err:
                    logger.error(f"移除选集失败的暂停任务失败：{err}")
                return None
            if task.up_speed or task.dl_speed:
                downloader.change_torrent(
                    hash_string=added_torrent.hashString,
                    upload_limit=up_speed,
                    download_limit=down_speed,
                )
            return added_torrent.hashString
        return None

    def _resource_mode(self, task=None) -> str:
        if task is None and hasattr(self, "_task_context"):
            task = self._get_task_config()
        if task and not task.spec_dedup_enabled:
            return "off"
        return "specs" if ((task or getattr(self, "_channel_dedup_enabled", False))
                           and getattr(self, "_channel_dedup_attributes", [])) else "off"

    def _resource_identity_deferred(self, task) -> bool:
        return (task is not None and self._resource_mode(task) != "off"
                and getattr(self, "_channel_dedup_identity_policy", "defer") == "defer")

    def _inflight_entry_enabled(self, entry) -> bool:
        if not getattr(self, "_channel_dedup_inflight_enabled", True):
            return False
        tasks = getattr(self, "_task_configs", {})
        task = tasks.get(entry.get("task_id"))
        if task is None:
            matches = [row for row in tasks.values() if row.name == entry.get("task_name")
                       and row.downloader == entry.get("downloader")]
            task = matches[0] if len(matches) == 1 else None
        return task.spec_dedup_enabled if task else bool(task_spec_dedup_enabled(entry))

    def _remember_resources(self, records, source: str) -> None:
        if not hasattr(self, "_resource_ledger"):
            return
        with self._inflight_lock:
            self._refresh_inflight_identity_bindings()
            forgotten = getattr(self, "_resource_forgotten", {})
            additions = {evidence_key(row): row for row in records if row and (row.get("tmdb_id") or row.get("title_key"))}
            additions = {key: row for key, row in additions.items()
                         if key not in self._resource_ledger and key not in forgotten}
            if not additions:
                return
            ledger = dict(self._resource_ledger)
            now, changed = time.time(), False
            for key, record in additions.items():
                ledger[key] = {**copy.deepcopy(record), "evidence_source": source,
                               "remembered_at": now, "channel": record.get("channel") or "已完成下载"}
                changed = True
            if changed:
                self.save_data("resource_ledger", ledger)
                self._resource_ledger = ledger

    def get_resource_ledger(self, page: int = 1, page_size: int = 50,
                            state: str = "ledger") -> schemas.Response:
        page, page_size = max(int(page), 1), min(max(int(page_size), 1), 100)
        with self._inflight_lock:
            rows = getattr(self, "_identity_pending" if state == "pending" else "_resource_ledger", {})
            items = [{"key": key, **value} for key, value in rows.items()]
            items.sort(key=lambda row: row.get("remembered_at", row.get("last_seen", 0)), reverse=True)
            return schemas.Response(success=True, data={"total": len(items), "page": page,
                "items": items[(page - 1) * page_size:page * page_size]})

    def forget_resource(self, payload: ResourceLedgerDeletePayload) -> schemas.Response:
        with self._inflight_lock:
            ledger = dict(getattr(self, "_resource_ledger", {}))
            ledger.pop(payload.key, None)
            forgotten = {**getattr(self, "_resource_forgotten", {}), payload.key: time.time()}
            self.save_data("resource_forgotten", forgotten)
            self.save_data("resource_ledger", ledger)
            self._resource_ledger, self._resource_forgotten = ledger, forgotten
        return schemas.Response(success=True, message="已移除长期证据；当前频道和在队任务仍参与去重")

    def _verify_resource_identity(self, candidate: dict, title: str, task=None) -> bool:
        if candidate.get("tmdb_id"):
            return True
        movie = candidate.get("media_type") == "movie"
        bindings = getattr(self, "_channel_dedup_movie_identity_aliases" if movie
                           else "_channel_dedup_identity_aliases", {})
        names = set(candidate.get("title_keys") or [])
        ids = {int(value) for name, value in bindings.items() if normalize_title(name) in names}
        identity = next(iter(ids)) if len(ids) == 1 else None
        if not ids and not movie:
            identity = self._channel_candidate_tmdb_identity(candidate["path"], title)
        elif not ids and movie:
            meta = MetaInfo(candidate["path"].rsplit("/", 1)[-1])
            key = ("movie", tuple(sorted(names)), candidate.get("year"))
            cached_identity = self._channel_identity_cache.get(key)
            if cached_identity and cached_identity[0] > time.monotonic():
                identity = cached_identity[1]
            else:
                try:
                    media = self.chain.recognize_media(meta=meta)
                    aliases = {normalize_title(getattr(media, field, "")) for field in
                               ("title", "en_title", "original_title", "original_name")}
                    aliases.update(normalize_title(name) for name in getattr(media, "names", []) or [])
                    cn = {name for name in names if re.search(r"[\u3400-\u9fff]", name)}
                    cn_aliases = {name for name in aliases if re.search(r"[\u3400-\u9fff]", name)}
                    if (self._is_movie(media) and names.intersection(aliases)
                            and candidate.get("year") == self._movie_year(media, None)
                            and candidate.get("year") is not None
                            and not (cn and cn_aliases and not cn.intersection(cn_aliases))):
                        identity = getattr(media, "tmdb_id", None)
                except Exception as err:
                    logger.info(f"资源身份核验失败 {candidate['path']}：{type(err).__name__}")
                self._channel_identity_cache[key] = (time.monotonic() + (600 if identity else 60), identity)
        if identity:
            candidate.update({"tmdb_id": int(identity), "identity_source": "alias" if ids else "cache",
                              "identity_evidence_names": sorted(names)})
            return True
        return False

    def _pending_identity(self, candidate: dict, title: str, task, reason: str) -> None:
        if not hasattr(self, "_identity_pending") or not self._resource_identity_deferred(task):
            return
        row = {**candidate, "task_name": task.name, "title": title, "reason": reason}
        key = evidence_key({**candidate, "title_key": candidate.get("title_key") or candidate.get("path")})
        with self._inflight_lock:
            pending = dict(self._identity_pending)
            if key in pending and time.time() - pending[key].get("last_seen", 0) < 300:
                return
            pending.pop(key, None)
            pending[key] = {**row, "last_seen": time.time()}
            pending = dict(list(pending.items())[-5000:])
            self.save_data("resource_identity_pending", pending)
            self._identity_pending = pending

    def _resolve_pending_identity(self, candidate: dict, task) -> None:
        if not getattr(self, "_identity_pending", None):
            return
        with self._inflight_lock:
            remaining = {key: row for key, row in self._identity_pending.items()
                         if not (row.get("path") == candidate["path"]
                                 and row.get("task_name") == task.name)}
            if len(remaining) != len(self._identity_pending):
                self.save_data("resource_identity_pending", remaining)
                self._identity_pending = remaining

    def _record_completed_resources(self, task, torrents, rows, force: bool = False) -> None:
        if not hasattr(self, "_resource_ledger") or not task.spec_dedup_enabled:
            return
        for torrent in torrents:
            torrent_hash = self.__get_hash(torrent)
            row = rows.get(torrent_hash) or {}
            if row.get("resource_ledger_recorded") or (not force and not self._torrent_complete(row)):
                continue
            try:
                kind = "qbittorrent" if isinstance(torrent, dict) else "transmission"
                files = download_files(self.downloader.get_files(torrent_hash), kind)
                if not files:
                    continue
                records = []
                wanted_videos = [item for item in files if item["wanted"] and self._video_file(item["path"])]
                for item in files:
                    if not item["wanted"] or not item["complete"]:
                        continue
                    candidate = parse_candidate_file(item["path"], item["size"], row.get("title", ""),
                                                     row.get("description", ""), allow_movie=True)
                    if candidate and not getattr(MetaInfo(item["path"].rsplit("/", 1)[-1]), "episode_group", None):
                        if self._verify_resource_identity(candidate, row.get("title", ""), task):
                            records.append(candidate)
                            self._resolve_pending_identity(candidate, task)
                        else:
                            self._pending_identity(candidate, row.get("title", ""), task, "已完成文件身份待核验，账本尚未记全")
                self._remember_resources(records, "completed")
                if self._torrent_complete(row) and records and len(records) == len(wanted_videos):
                    row["resource_ledger_recorded"] = True
            except Exception as err:
                logger.warning(f"资源账本读取完成文件失败，稍后重试：{torrent_hash} {type(err).__name__}")

    def _save_inflight_entries(self) -> None:
        self.save_data("channel_dedup_inflight", self._inflight_entries)

    def _channel_cancel_settings(self) -> str:
        with self._channel_state_lock:
            return json.dumps([
                self._channel_dedup_enabled, getattr(self, "_channel_dedup_cancel_downloading", False),
                self._channel_dedup_channels, self._channel_dedup_attributes,
                self._channel_dedup_size_tolerance_pct, self._channel_dedup_identity_aliases,
                getattr(self, "_channel_dedup_movie_identity_aliases", {}),
            ], sort_keys=True, ensure_ascii=False)

    def cancel_channel_duplicates(self) -> None:
        """Only mutate exclusively managed, unfinished downloads using published channel evidence."""
        if (not self.get_state() or getattr(self, "_channel_stopping", False)
                or not getattr(self, "_channel_dedup_enabled", False)
                or not getattr(self, "_channel_dedup_cancel_downloading", False)
                or not self._channel_dedup_channels):
            return
        if not self._channel_cancel_lock.acquire(blocking=False):
            return
        locks = []
        brush_locked = False
        try:
            # No waiting while holding brush/task locks: busy tasks are retried on the next tick.
            if not self._brush_lock.acquire(blocking=False):
                return
            brush_locked = True
            available = set()
            for task_id in sorted(self._task_configs):
                lock = self._task_locks.setdefault(task_id, threading.Lock())
                if not lock.acquire(blocking=False):
                    continue
                locks.append(lock)
                available.add(task_id)
            with self._channel_state_lock:
                self._prune_channel_dedup_records()
                records = copy.deepcopy(self._channel_dedup_state.get("records") or [])
                attributes = list(self._channel_dedup_attributes)
                tolerance = self._channel_dedup_size_tolerance_pct
                signature, generation = self._channel_cancel_settings(), self._channel_generation
            episodes = index_records(records)
            task_data, owners = {}, {}
            for task in self._task_configs.values():
                task_data[task.id] = self._get_task_data(task.id, "torrents") or {}
                for torrent_hash, row in task_data[task.id].items():
                    if not row.get("deleted"):
                        owners.setdefault((task.downloader, torrent_hash.lower()), []).append((task, row))
            eligible = [
                (name, torrent_hash, entries[0][0], entries[0][1])
                for (name, torrent_hash), entries in owners.items()
                if len(entries) == 1 and entries[0][0].enabled and entries[0][0].id in available
                and self._resource_mode(entries[0][0]) != "off"
                and entries[0][1].get("hit_and_run") is False and not entries[0][0].site_hr_active
                and (records or entries[0][1].get("channel_dedup_cancel_pending"))
                and (not download_complete(entries[0][1], "qbittorrent")
                     or entries[0][1].get("channel_dedup_cancel_pending"))
            ]
            if not eligible:
                return
            offset = self._channel_cancel_offset % len(eligible)
            batch = (eligible[offset:] + eligible[:offset])[:50]
            self._channel_cancel_offset = (offset + len(batch)) % len(eligible)
            grouped = {}
            for name, torrent_hash, task, row in batch:
                grouped.setdefault(name, []).append((torrent_hash, task, row))
            for name, entries in grouped.items():
                helper = DownloaderHelper()
                service = helper.get_service(name=name)
                if not service or not service.instance:
                    continue
                kind = next((value for value in ("qbittorrent", "transmission")
                             if helper.is_downloader(value, service=service)), None)
                if not kind:
                    continue
                downloader = service.instance
                try:
                    torrents, error = downloader.get_torrents(ids=[item[0] for item in entries])
                    if error:
                        continue
                    live = {self._channel_live_hash(item, kind): item for item in torrents or []}
                    for torrent_hash, task, row in entries:
                        if not self._channel_cancel_current(generation, signature):
                            return
                        pending = row.get("channel_dedup_cancel_pending") or {}
                        if torrent_hash not in live:
                            if pending.get("phase") == "removing":
                                self._finish_channel_cancel(task, torrent_hash, row, task_data[task.id], removed=True)
                            continue
                        torrent = live[torrent_hash]
                        if not self._channel_cancel_owned(torrent, kind, task):
                            continue
                        try:
                            self._cancel_published_files(
                                downloader, kind, torrent_hash, torrent, task, row, task_data[task.id],
                                episodes, attributes, tolerance, signature, generation,
                            )
                        except Exception as err:
                            if row.get("channel_dedup_cancel_pending"):
                                row["channel_dedup_cancel_error"] = type(err).__name__
                                self._save_task_data(task.id, "torrents", task_data[task.id])
                            logger.warning(f"下载中频道去重 [{task.name}] {torrent_hash} 处理失败，保留任务与文件：{type(err).__name__}")
                except Exception as err:
                    logger.warning(f"下载中频道去重核验下载器 [{name}] 失败：{type(err).__name__}")
        finally:
            for lock in reversed(locks):
                lock.release()
            if brush_locked:
                self._brush_lock.release()
            self._channel_cancel_lock.release()

    @staticmethod
    def _channel_live_hash(torrent: Any, kind: str) -> str:
        value = torrent.get("hash", "") if kind == "qbittorrent" else getattr(torrent, "hashString", "")
        return str(value).lower()

    def _channel_cancel_owned(self, torrent: Any, kind: str, task: BrushTaskConfig) -> bool:
        tags = (str(torrent.get("tags") or "").split(",") if kind == "qbittorrent"
                else getattr(torrent, "labels", None) or [])
        tags = {str(tag).strip() for tag in tags}
        excluded = {tag.strip() for tag in (task.delete_except_tags or "").split(",") if tag.strip()}
        owners = [other for other in self._task_configs.values()
                  if other.downloader == task.downloader and other.brush_tag in tags]
        return (task.enabled and task.spec_dedup_enabled and self._task_configs.get(task.id) is task
                and task.brush_tag in tags and not excluded.intersection(tags)
                and len(owners) == 1 and owners[0].id == task.id)

    def _channel_cancel_current(self, generation: int, signature: str) -> bool:
        return (not self._channel_stopping and self.get_state()
                and self._channel_generation == generation and self._channel_cancel_settings() == signature)

    def _published_file_matches(self, files: List[dict], task: BrushTaskConfig, torrent_hash: str,
                                row: dict, episodes: dict, attributes: List[str], tolerance: float) -> dict:
        with self._inflight_lock:
            self._refresh_inflight_identity_bindings()
            queued = copy.deepcopy(self._inflight_entries.get(
                self._inflight_key(task.downloader, torrent_hash), {}
            ).get("records") or [])
        matches = {}
        with self._channel_state_lock:
            bindings = {normalize_title(name): int(value)
                        for name, value in self._channel_dedup_identity_aliases.items()}
        for item in files:
            if not item["wanted"] or item["complete"]:
                continue
            candidate = parse_candidate_file(item["path"], item["size"], row.get("title") or "",
                                             spec_hint=row.get("description") or "", allow_movie=True)
            if not candidate or getattr(MetaInfo(item["path"].rsplit("/", 1)[-1]), "episode_group", None):
                continue
            evidence = [record for record in queued if paths_agree(record.get("path"), item["path"])
                        and record.get("season") == candidate["season"]
                        and record.get("episodes") == candidate["episodes"]
                        and (record.get("specs") or {}).get("size") == item["size"]]
            if not candidate.get("tmdb_id") and len(evidence) == 1:
                candidate["tmdb_id"] = evidence[0].get("tmdb_id")
            if candidate.get("media_type") == "movie":
                candidate_bindings = {normalize_title(name): int(value) for name, value in
                                     getattr(self, "_channel_dedup_movie_identity_aliases", {}).items()}
            else:
                candidate_bindings = bindings
            bound_ids = {candidate_bindings[name] for name in candidate.get("title_keys") or [] if name in candidate_bindings}
            if not candidate.get("tmdb_id") and bound_ids:
                if len(bound_ids) != 1:
                    continue
                candidate["tmdb_id"] = bound_ids.pop()
            if self._resource_identity_deferred(task) and not self._verify_resource_identity(candidate, row.get("title") or "", task):
                continue
            found = self._match_channel_candidate(candidate, episodes, attributes, tolerance, row.get("title") or "",
                                                   mode=self._resource_mode(task), task=task)
            if found and all(found):
                matches[item["path"]] = found
        return matches

    def _cancel_published_files(self, downloader: Any, kind: str, torrent_hash: str, torrent: Any,
                                task: BrushTaskConfig, row: dict, task_data: dict, episodes: dict,
                                attributes: List[str], tolerance: float, signature: str, generation: int) -> None:
        state = download_state(torrent, kind)
        pending = row.get("channel_dedup_cancel_pending") or {}
        if pending and pending.get("settings") != signature:
            logger.warning(f"下载中频道去重 [{task.name}] {torrent_hash} 上次处理后设置已改变，保留当前状态待人工检查")
            return
        completed = download_complete(torrent, kind)
        if not state and not (pending and completed):
            return
        files = download_files(downloader.get_files(torrent_hash), kind)
        if not files:
            return
        previous = set(pending.get("paths") or [])
        # Recover a verified selection after a process exit or an ambiguous API response.
        deselected = {item["path"] for item in files if not item["wanted"]}
        applied = previous & deselected if pending.get("phase") == "selecting" else set()
        if applied and (previous.issubset(applied) or completed):
            if not self._channel_cancel_current(generation, signature):
                return
            pending["paths"] = sorted(applied)
            self._save_task_data(task.id, "torrents", task_data)
            if pending.get("resume") and (state == "paused" or completed) and not downloader.start_torrents(torrent_hash):
                return
            self._finish_channel_cancel(task, torrent_hash, row, task_data, removed=False)
            return
        if completed:
            self._abandon_channel_cancel(downloader, torrent_hash, task, row, task_data, signature, generation)
            return
        matches = self._published_file_matches(files, task, torrent_hash, row, episodes, attributes, tolerance)
        if not self._channel_cancel_current(generation, signature):
            return
        if not matches:
            if applied:
                pending["paths"] = sorted(applied)
                self._save_task_data(task.id, "torrents", task_data)
                if pending.get("resume") and state == "paused" and not downloader.start_torrents(torrent_hash):
                    return
                self._finish_channel_cancel(task, torrent_hash, row, task_data, removed=False)
            elif pending:
                self._abandon_channel_cancel(downloader, torrent_hash, task, row, task_data, signature, generation)
            return
        row["channel_dedup_cancel_pending"] = pending = {
            "phase": "selecting" if applied else "prepared", "paths": sorted(applied | set(matches)), "settings": signature,
            "resume": pending.get("resume", state == "running"), "at": time.time(),
        }
        self._save_task_data(task.id, "torrents", task_data)
        if not self._channel_cancel_current(generation, signature):
            return
        if not downloader.stop_torrents(torrent_hash):
            return
        paused = None
        for attempt in range(3):
            current, error = downloader.get_torrents(ids=[torrent_hash])
            if error or len(current or []) != 1:
                return
            paused = current[0]
            if self._channel_live_hash(paused, kind) != torrent_hash:
                return
            if download_complete(paused, kind):
                self._abandon_channel_cancel(downloader, torrent_hash, task, row, task_data, signature, generation)
                return
            if download_state(paused, kind) == "paused":
                break
            if attempt < 2:
                time.sleep(0.15)
        else:
            logger.warning(f"下载中频道去重 [{task.name}] {torrent_hash} 未确认暂停或已完成，不执行取消")
            return
        if (not self._channel_cancel_owned(paused, kind, task)
                or not self._channel_cancel_current(generation, signature)):
            return
        paused_files = download_files(downloader.get_files(torrent_hash), kind)
        if not paused_files or {(item["id"], item["path"], item["size"]) for item in files} != {
            (item["id"], item["path"], item["size"]) for item in paused_files
        }:
            return
        matches = self._published_file_matches(paused_files, task, torrent_hash, row, episodes, attributes, tolerance)
        if not self._channel_cancel_current(generation, signature):
            return
        if not matches:
            if applied:
                pending["paths"] = sorted(applied)
                pending["phase"] = "selecting"
                self._save_task_data(task.id, "torrents", task_data)
                if pending.get("resume") and not downloader.start_torrents(torrent_hash):
                    return
                self._finish_channel_cancel(task, torrent_hash, row, task_data, removed=False)
            else:
                self._abandon_channel_cancel(downloader, torrent_hash, task, row, task_data, signature, generation)
            return
        targets = [item for item in paused_files if item["path"] in matches]
        remaining = [item for item in paused_files if item["wanted"] and self._video_file(item["path"])
                     and item["path"] not in matches]
        pending.update({"paths": sorted(applied | set(matches)), "phase": "selecting" if remaining else "removing"})
        self._save_task_data(task.id, "torrents", task_data)
        for item in targets:
            matched = matches[item["path"]]
            logger.info(
                f"下载中频道去重命中 [{task.name}] {item['path']}；频道消息 "
                f"{', '.join(sorted({str(record.get('channel')) + '/' + str(record.get('message_id')) for record in matched}))}；"
                f"匹配规格 {', '.join(attributes)}；仅取消未完成文件，保留本地数据"
            )
        if not self._channel_cancel_current(generation, signature) or not task.enabled:
            return
        if not remaining:
            if not downloader.delete_torrents(ids=torrent_hash, delete_file=False):
                return
            current, error = downloader.get_torrents(ids=[torrent_hash])
            if not error and not current:
                self._finish_channel_cancel(task, torrent_hash, row, task_data, removed=True)
            return
        ids = [item["id"] for item in targets]
        ok = (downloader.set_files(torrent_hash=torrent_hash, file_ids=ids, priority=0)
              if kind == "qbittorrent" else downloader.set_unwanted_files(torrent_hash, ids))
        if not ok:
            return
        verified = download_files(downloader.get_files(torrent_hash), kind)
        if not verified or {(item["id"], item["path"], item["size"], item["wanted"]) for item in verified} != {
            (item["id"], item["path"], item["size"], False if item["path"] in matches else item["wanted"])
            for item in paused_files
        }:
            logger.warning(f"下载中频道去重 [{task.name}] {torrent_hash} 未确认逐文件取消结果，保持暂停待核验")
            return
        if not self._channel_cancel_current(generation, signature):
            return
        if pending["resume"] and not downloader.start_torrents(torrent_hash):
            return
        self._finish_channel_cancel(task, torrent_hash, row, task_data, removed=False)

    def _abandon_channel_cancel(self, downloader: Any, torrent_hash: str, task: BrushTaskConfig,
                                row: dict, task_data: dict, signature: str, generation: int) -> None:
        if not self._channel_cancel_current(generation, signature):
            return
        pending = row.get("channel_dedup_cancel_pending") or {}
        if pending.get("resume") and not downloader.start_torrents(torrent_hash):
            return
        row.pop("channel_dedup_cancel_pending", None)
        self._save_task_data(task.id, "torrents", task_data)
        logger.info(f"下载中频道去重 [{task.name}] {torrent_hash} 暂停复核发现已完成或无可取消文件，保留任务")

    def _finish_channel_cancel(self, task: BrushTaskConfig, torrent_hash: str, row: dict,
                               task_data: dict, removed: bool) -> None:
        pending = row.pop("channel_dedup_cancel_pending", {})
        paths = pending.get("paths") or []
        row.pop("channel_dedup_cancel_error", None)
        history = list(row.get("channel_dedup_cancelled_files") or [])
        row["channel_dedup_cancelled_files"] = (history + [{"path": path, "at": time.time()} for path in paths])[-100:]
        if removed:
            row.update({"deleted": True, "deleted_time": time.time(), "delete_reason": "下载中频道同规格重复（保留文件）"})
        self._save_task_data(task.id, "torrents", task_data)
        with self._inflight_lock:
            key = self._inflight_key(task.downloader, torrent_hash)
            if removed:
                self._inflight_entries.pop(key, None)
            elif key in self._inflight_entries:
                records = self._inflight_entries[key].get("records") or []
                self._inflight_entries[key]["records"] = [
                    record for record in records if not any(paths_agree(record.get("path"), path) for path in paths)
                ]
            self._save_inflight_entries()
        self._recalculate_statistics(task.id)
        logger.info(f"下载中频道去重 [{task.name}] {torrent_hash}：{'已移除未完成任务' if removed else '已取消重复分集'} {len(paths)} 个文件，本地文件保留")

    def _cache_inflight_file_identity(self, candidate: dict, title: str) -> None:
        """Persist only explicit bindings or identities already verified in the local cache."""
        if candidate.get("tmdb_id"):
            return
        if candidate.get("media_type") == "movie":
            self._verify_resource_identity(candidate, title)
            return
        identity = self._channel_candidate_tmdb_identity(candidate["path"], title, cached_only=True)
        if not identity:
            return
        _, raw_names = self._channel_identity_names(candidate["path"], title)
        names = {normalize_title(name) for name in raw_names}
        with self._channel_state_lock:
            bound = any(
                normalize_title(name) in names and int(tmdb_id) == identity
                for name, tmdb_id in getattr(self, "_channel_dedup_identity_aliases", {}).items()
            )
        candidate.update({
            "tmdb_id": identity, "identity_source": "alias" if bound else "cache",
            "identity_evidence_names": sorted(names),
        })

    def _refresh_inflight_identity_bindings(self) -> None:
        """Rebind derived identities in queues and ledger; preserve file-embedded IDs."""
        with self._channel_state_lock:
            bindings = {
                normalize_title(name): int(tmdb_id)
                for name, tmdb_id in getattr(self, "_channel_dedup_identity_aliases", {}).items()
            }
            movie_bindings = {normalize_title(name): int(tmdb_id) for name, tmdb_id in
                              getattr(self, "_channel_dedup_movie_identity_aliases", {}).items()}
        signature = (tuple(sorted(bindings.items())), tuple(sorted(movie_bindings.items())))
        if getattr(self, "_inflight_alias_snapshot", None) == signature:
            return
        def rebind(record):
            if record.get("identity_source") not in {"alias", "cache"}:
                return False
            mapping = movie_bindings if record.get("media_type") == "movie" else bindings
            ids = {mapping[name] for name in record.get("identity_evidence_names") or [] if name in mapping}
            if not ids and record["identity_source"] != "alias":
                return False
            identity = next(iter(ids)) if len(ids) == 1 else None
            if record.get("tmdb_id") == identity and record.get("identity_invalidated", False) == (identity is None):
                return False
            record.update({"tmdb_id": identity, "identity_source": "alias",
                           "identity_invalidated": identity is None})
            return True

        entries = copy.deepcopy(self._inflight_entries)
        changed = False
        for entry in entries.values():
            for record in entry.get("records") or []:
                changed = rebind(record) or changed
        if changed:
            self.save_data("channel_dedup_inflight", entries)
            self._inflight_entries = entries
        ledger, ledger_changed = {}, False
        for key, old in getattr(self, "_resource_ledger", {}).items():
            record = copy.deepcopy(old)
            updated = rebind(record)
            ledger[evidence_key(record) if updated else key] = record
            ledger_changed = ledger_changed or updated
        if ledger_changed:
            self.save_data("resource_ledger", ledger)
            self._resource_ledger = ledger
        self._inflight_alias_snapshot = signature

    @staticmethod
    def _inflight_key(downloader_name: str, torrent_hash: str) -> str:
        return json.dumps([downloader_name, torrent_hash.lower()], ensure_ascii=False)

    @staticmethod
    def _inflight_title_keys(title: str) -> Set[str]:
        meta = MetaInfo(str(title).rsplit("/", 1)[-1])
        return {
            normalize_title(getattr(meta, field, None)) for field in ("cn_name", "en_name", "name")
        } - {""}

    def _reconcile_inflight_entries(self) -> None:
        """批量核验持久化占位；查询失败保留，确认不存在才释放。调用者持有入队锁。"""
        now = time.time()
        grouped: Dict[str, List[Tuple[str, dict]]] = {}
        for key, entry in self._inflight_entries.items():
            grouped.setdefault(entry["downloader"], []).append((key, entry))
        changed = False
        for name, entries in grouped.items():
            if now - self._inflight_checked_at.get(name, 0) < self.INFLIGHT_QUERY_SECONDS:
                continue
            self._inflight_checked_at[name] = now
            try:
                service = DownloaderHelper().get_service(name=name)
                if not service or not service.instance:
                    continue
                torrents, error = service.instance.get_torrents(ids=[entry["hash"] for _, entry in entries])
                if error:
                    continue
                hashes = {
                    str(item.get("hash") if isinstance(item, dict) else getattr(item, "hashString", "")).lower()
                    for item in torrents or []
                }
                for key, entry in entries:
                    if entry["hash"] in hashes:
                        if entry.get("status") != "queued" or not entry.get("confirmed_at"):
                            entry["status"] = "queued"
                            entry["confirmed_at"] = now
                            changed = True
                    elif (
                        entry.get("confirmed_at")
                        or now - entry.get("created_at", now) >= self.INFLIGHT_ADD_GRACE_SECONDS
                    ):
                        self._inflight_entries.pop(key, None)
                        changed = True
            except Exception as err:
                logger.warning(f"已入队同规格去重核验下载器 [{name}] 失败，保留占位：{type(err).__name__}")
        if changed:
            self._save_inflight_entries()

    def _backfill_inflight_entries(self, candidates: List[dict]) -> None:
        """只对同名的历史 BrushFlow 任务读取实际已选文件，不把整包标题当作已下载全集。"""
        now = time.time()
        snapshot_at, tasks = self._inflight_task_snapshot
        if now - snapshot_at >= self.INFLIGHT_QUERY_SECONDS:
            tasks = []
            for task in getattr(self, "_task_configs", {}).values():
                if self._resource_mode(task) == "off":
                    continue
                for torrent_hash, row in (self._get_task_data(task.id, "torrents") or {}).items():
                    if not row.get("deleted"):
                        row = {**row, "task_id": task.id, "task_name": task.name}
                        title = row.get("title") or ""
                        header = parse_candidate_file(title + ".mkv", row.get("size"))
                        tasks.append((
                            task.downloader, torrent_hash.lower(), row, self._inflight_title_keys(title),
                            (header.get("tmdb_id") if header else None)
                            or self._channel_candidate_tmdb_identity(title + ".mkv", title, cached_only=True),
                        ))
            self._inflight_task_snapshot = (now, tasks)
        candidate_names = set().union(*(self._inflight_title_keys(row["path"]) for row in candidates))
        candidate_ids = {row["tmdb_id"] for row in candidates if row.get("tmdb_id")}
        changed = False
        for name, torrent_hash, row, names, tmdb_id in tasks:
            if not self._inflight_entry_enabled({**row, "downloader": name}):
                continue
            key = self._inflight_key(name, torrent_hash)
            if key in self._inflight_entries or now - self._inflight_legacy_checked.get(key, 0) < 60:
                continue
            title = row.get("title") or ""
            if not (candidate_names & names or tmdb_id and tmdb_id in candidate_ids):
                continue
            self._inflight_legacy_checked[key] = now
            try:
                service = DownloaderHelper().get_service(name=name)
                if not service or not service.instance:
                    continue
                files = service.instance.get_files(torrent_hash)
                if not files:
                    continue
                records = []
                for item in files:
                    if isinstance(item, dict):
                        # Missing selection information must not imply that an entire pack was selected.
                        wanted = item.get("priority", 0) > 0
                        path, size = item.get("name"), item.get("size")
                    else:
                        wanted = getattr(item, "selected", False) is True
                        path, size = getattr(item, "name", ""), getattr(item, "size", None)
                    if not wanted:
                        continue
                    candidate = parse_candidate_file(path, size, title, spec_hint=row.get("description") or "", allow_movie=True)
                    if candidate and not getattr(MetaInfo(path.rsplit("/", 1)[-1]), "episode_group", None):
                        self._cache_inflight_file_identity(candidate, title)
                        records.append(candidate)
                if records:
                    self._inflight_entries[key] = {
                        "downloader": name, "hash": torrent_hash, "task_name": row.get("task_name") or "",
                        "created_at": now, "confirmed_at": now, "status": "queued", "records": records,
                        "task_id": row.get("task_id"), "spec_dedup_enabled": True,
                    }
                    changed = True
            except Exception as err:
                logger.warning(f"已入队同规格去重补查文件失败：{torrent_hash}（{type(err).__name__}）")
        if changed:
            self._save_inflight_entries()

    @contextmanager
    def _inflight_download_scope(
        self, content: Union[str, bytes], torrent: TorrentInfo, task: BrushTaskConfig,
        expected_hash: str, selection: Optional[dict],
    ) -> Iterator[dict]:
        """匹配和持久化占位原子化；添加及选集完成后提交，失败核实后回滚。"""
        outcome = {"blocked": False, "selection": selection, "added": False}
        if (self._resource_mode(task) == "off" or not getattr(self, "_channel_dedup_inflight_enabled", True)
                or not isinstance(content, bytes)):
            yield outcome
            return
        files = parse_torrent_files(content)
        if not files:
            yield outcome
            return
        spec_hint = " ".join([torrent.description or "", *(torrent.labels or [])])
        accepted = selection.get("accepted") if selection else None
        candidates = []
        for item in files:
            if accepted is not None and item["path"] not in accepted:
                continue
            candidate = parse_candidate_file(item["path"], item.get("size"), torrent.title,
                                             spec_hint=spec_hint, allow_movie=True)
            if candidate and not getattr(MetaInfo(item["path"].rsplit("/", 1)[-1]), "episode_group", None):
                if self._resource_identity_deferred(task):
                    self._verify_resource_identity(candidate, torrent.title, task)
                else:
                    self._cache_inflight_file_identity(candidate, torrent.title)
                candidates.append(candidate)
        if not candidates:
            yield outcome
            return
        key = self._inflight_key(task.downloader, expected_hash)
        token = uuid.uuid4().hex
        with self._inflight_lock:
            self._refresh_inflight_identity_bindings()
            self._reconcile_inflight_entries()
            self._backfill_inflight_entries(candidates)
            if key in self._inflight_entries:
                logger.info(f"已入队同规格去重排除种子 {torrent.title}：相同 Hash 已占位或已入队")
                outcome["blocked"] = True
            records = [
                {**record, "dedup_origin": "在队重复",
                 "channel": f"{entry.get('task_name') or entry['downloader']} / {entry['hash'][:12]}"}
                for entry in self._inflight_entries.values()
                if self._inflight_entry_enabled(entry)
                for record in entry.get("records") or []
            ]
            records += [{**row, "dedup_origin": "账本重复"}
                        for row in getattr(self, "_resource_ledger", {}).values()]
            with self._channel_state_lock:
                if getattr(self, "_channel_dedup_enabled", False):
                    self._prune_channel_dedup_records()
                    records += [{**row, "dedup_origin": "频道重复"}
                                for row in self._channel_dedup_state.get("records") or []]
            duplicate = self._channel_dedup_file_selection(
                content, torrent.title, accepted_paths=accepted, spec_hint=spec_hint,
                records_override=records, match_source="已入队同规格去重",
                task=task,
            ) if not outcome["blocked"] else None
            if duplicate:
                remaining = (set(accepted) - duplicate["excluded"]) if accepted is not None else duplicate["remaining_paths"]
                if (
                    duplicate["reject_torrent"]
                    or accepted is not None and not remaining
                    or len(duplicate["excluded"]) >= duplicate["video_count"]
                ):
                    logger.info(f"已入队同规格去重排除种子 {torrent.title}：{self._dedup_exclusion_summary(duplicate)}")
                    outcome["blocked"] = True
                else:
                    outcome["selection"] = {**(selection or {}), "accepted": remaining}
                candidates = [row for row in candidates if row["path"] not in duplicate["excluded"]]
            if not outcome["blocked"]:
                self._inflight_entries[key] = {
                    "downloader": task.downloader, "hash": expected_hash, "task_name": task.name,
                    "created_at": time.time(), "status": "reserved", "token": token, "records": candidates,
                    "task_id": task.id, "spec_dedup_enabled": task.spec_dedup_enabled,
                }
                self._save_inflight_entries()
        if outcome["blocked"]:
            yield outcome
            return
        try:
            yield outcome
        finally:
            with self._inflight_lock:
                entry = self._inflight_entries.get(key)
                if entry and entry.get("token") == token:
                    if outcome["added"]:
                        entry["status"] = "queued"
                        self._save_inflight_entries()
                    else:
                        # A timeout/ambiguous add may still have created a task. Never free it blindly.
                        try:
                            service = DownloaderHelper().get_service(name=task.downloader)
                            if service and service.instance:
                                existing, error = service.instance.get_torrents(ids=[expected_hash])
                                if not error:
                                    if existing:
                                        entry["status"] = "queued"
                                        entry["confirmed_at"] = time.time()
                                    else:
                                        self._inflight_entries.pop(key, None)
                                    self._save_inflight_entries()
                        except Exception as err:
                            logger.warning(f"已入队同规格去重添加结果待核验，暂留占位：{type(err).__name__}")

    @staticmethod
    def _qb_add_result(result: Any) -> Tuple[bool, List[str]]:
        """兼容宿主新版 (success, hashes) 与旧版布尔返回值。"""
        if isinstance(result, tuple) and len(result) == 2:
            success, hashes = result
            hashes = hashes if isinstance(hashes, (list, tuple)) else []
            return success is True, [str(value).lower() for value in hashes if value]
        return result is True, []

    @staticmethod
    def _video_file(path: str) -> bool:
        return normalize("NFKC", path).lower().rsplit(".", 1)[-1] in {
            "mkv", "mp4", "avi", "mov", "wmv", "m4v", "ts", "m2ts", "webm",
        }

    @staticmethod
    def _air_date_is_recent(air_date: Optional[str], today: date) -> bool:
        """三天前（含）至未来日期均属于可下载范围。"""
        if not air_date:
            return False
        try:
            return datetime.strptime(air_date, "%Y-%m-%d").date() >= today - timedelta(days=3)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _is_movie(media: Any) -> bool:
        media_type = getattr(media, "type", None)
        media_type = getattr(media_type, "value", media_type)
        return str(media_type or "").lower() in {"movie", "电影"}

    @staticmethod
    def _movie_year(media: Any, meta_year: Optional[str]) -> Optional[int]:
        release_date = getattr(media, "release_date", None)
        if release_date:
            match = re.match(r"^(\d{4})", str(release_date))
            if match:
                return int(match.group(1))
        year = getattr(media, "year", None) or meta_year
        match = re.search(r"\b(\d{4})\b", str(year or ""))
        return int(match.group(1)) if match else None

    def _recent_episode_file_selection(self, torrent_content: Union[str, bytes], title: str) -> Optional[dict]:
        """预检种子内视频，返回符合近期播出规则的文件路径及其名称。"""
        if not isinstance(torrent_content, bytes):
            return None
        _, paths = TorrentHelper().get_fileinfo_from_torrent_content(torrent_content)
        video_paths = [path for path in paths if self._video_file(path)]
        if not video_paths:
            logger.info(f"最近播出剧集规则排除 {title}：种子内没有可识别的视频文件")
            return None
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        accepted = []
        date_cache: Dict[Tuple[int, int, Optional[str]], Dict[int, Optional[str]]] = {}
        source_cache = {}
        douban_date_cache: Dict[Tuple[str, int], Dict[int, Optional[str]]] = {}
        for path in video_paths:
            try:
                meta = MetaInfo(path.rsplit("/", 1)[-1])
                episodes = list(meta.episode_list or [])
                season = meta.begin_season if meta.begin_season is not None else 1
                if not episodes and meta.begin_episode:
                    episodes = list(range(meta.begin_episode, (meta.end_episode or meta.begin_episode) + 1))
                media = self.chain.recognize_media(meta=meta)
                if self._is_movie(media):
                    movie_year = self._movie_year(media, meta.year)
                    if movie_year == today.year:
                        accepted.append(path)
                        logger.info(
                            f"最近播出剧集规则通过电影文件 {path}：上映年份 {movie_year}，当前年份 {today.year}"
                        )
                    else:
                        reason = f"无法识别上映年份（当前年份 {today.year}）" if movie_year is None else (
                            f"上映年份 {movie_year}，不属于当前年份 {today.year}"
                        )
                        logger.info(f"最近播出剧集规则排除电影文件 {path}：{reason}")
                    continue
                tmdb_id = getattr(media, "tmdb_id", None)
                dates, sources = {}, {}
                if tmdb_id:
                    key = (int(tmdb_id), int(season), meta.episode_group)
                    if key not in date_cache:
                        date_cache[key], source_cache[key] = {}, {}
                    dates, sources = date_cache[key], source_cache[key]
                    try:
                        if not dates:
                            dates.update({
                                int(ep.episode_number): ep.air_date
                                for ep in TmdbChain().tmdb_episodes(
                                    tmdbid=key[0], season=key[1], episode_group=meta.episode_group
                                )
                                if ep.episode_number is not None
                            })
                            sources.update({number: "TMDB整季API" for number in dates})
                    except Exception as err:
                        logger.info(f"TMDB 整季日期查询失败，继续核验：{path}：{type(err).__name__}")
                    # 剧集组编号不一定等于默认季集号，不能用默认单集或网页数据填补。
                    if not meta.episode_group:
                        missing = [number for number in episodes if not dates.get(number)]
                        # 多集缺失时先读一次整季网页，避免整季包逐集发起网络请求。
                        if len(missing) == 1:
                            number = missing[0]
                            value = self._tmdb_episode_air_date(key[0], key[1], int(number))
                            if value:
                                dates[number], sources[number] = value, "TMDB单集API"
                        if any(not dates.get(number) for number in missing):
                            web_dates = self._tmdb_web_episode_air_dates(key[0], key[1])
                            for number, value in web_dates.items():
                                if not dates.get(number) and value:
                                    dates[number], sources[number] = value, "TMDB网页"
                        for number in missing:
                            if not dates.get(number) and len(missing) > 1:
                                value = self._tmdb_episode_air_date(key[0], key[1], int(number))
                                if value:
                                    dates[number], sources[number] = value, "TMDB单集API"
                if any(not dates.get(number) for number in episodes):
                    if meta.episode_group:
                        raise ValueError("剧集组日期缺失，不能用默认季集号或豆瓣季集号代替")
                    douban_id = str(getattr(media, "douban_id", "") or "")
                    if not douban_id and str(getattr(media, "media_source", "")).lower().endswith("douban"):
                        douban_id = str(getattr(media, "media_id", "") or "")
                    if not douban_id:
                        name = str(getattr(media, "title", "") or meta.cn_name or meta.en_name or "")
                        year = str(meta.year or (getattr(media, "year", "") if season == 1 else "") or "")
                        douban_id = self._recent_episode_douban_id(name, year, season)
                    if not douban_id:
                        raise ValueError("TMDB逐集日期缺失，豆瓣未精确匹配到剧名/年份/季号")
                    douban_key = (douban_id, int(season))
                    if douban_key not in douban_date_cache:
                        douban_date_cache[douban_key] = self._douban_episode_air_dates(douban_id, season)
                    for number in episodes:
                        if not dates.get(number) and douban_date_cache[douban_key].get(number):
                            dates[number], sources[number] = douban_date_cache[douban_key][number], "豆瓣"
                air_dates = [dates.get(number) for number in episodes]
                if not episodes or any(not value for value in air_dates):
                    raise ValueError("季集号或 TMDB/豆瓣逐集播出日期缺失")
                detail = ", ".join(
                    f"S{season:02d}E{number:02d}={dates[number]}({sources[number]})" for number in episodes
                )
                if all(self._air_date_is_recent(value, today) for value in air_dates):
                    accepted.append(path)
                    logger.info(f"最近播出剧集规则通过文件 {path}：{detail}，日期下限 {today - timedelta(days=3)}")
                else:
                    logger.info(f"最近播出剧集规则排除文件 {path}：{detail}，早于日期下限 {today - timedelta(days=3)}")
            except Exception as err:
                logger.info(f"最近播出剧集规则排除文件 {path}：{err}")
        if not accepted:
            logger.info(f"最近播出剧集规则排除 {title}：没有符合日期范围的剧集")
            return None
        return {"accepted": set(accepted), "videos": set(video_paths)}

    def _channel_dedup_file_selection(
        self,
        torrent_content: Union[str, bytes],
        title: str,
        accepted_paths: Optional[Set[str]] = None,
        spec_hint: str = "",
        records_override: Optional[List[dict]] = None,
        match_source: str = "频道同规格去重",
        task: Optional[BrushTaskConfig] = None,
    ) -> Optional[dict]:
        """按任务开关与勾选字段匹配频道、账本及同包证据，不以未知值认定重复。"""
        if task is None and hasattr(self, "_task_context"):
            task = self._get_task_config()
        mode = self._resource_mode(task)
        if not isinstance(torrent_content, bytes) or mode == "off":
            return None
        with self._channel_state_lock:
            if records_override is None:
                self._prune_channel_dedup_records()
            records = list(
                self._channel_dedup_state.get("records") or []
            ) if records_override is None and getattr(self, "_channel_dedup_enabled", False) else (records_override or [])
            attributes = list(self._channel_dedup_attributes)
            tolerance = self._channel_dedup_size_tolerance_pct
            unknown_policy = getattr(self, "_channel_dedup_unknown_policy", "exclude")
            defer_identity = self._resource_identity_deferred(task)
            pack_dedup = task is not None and getattr(self, "_channel_dedup_pack_dedup_enabled", True)
            pack_policy = getattr(self, "_channel_dedup_pack_policy", "partial")
            bindings = {
                normalize_title(name): int(tmdb_id)
                for name, tmdb_id in getattr(self, "_channel_dedup_identity_aliases", {}).items()
            }
        if records_override is None:
            with self._inflight_lock:
                self._refresh_inflight_identity_bindings()
                records = [{**row, "dedup_origin": "频道重复"} for row in records]
                records += [{**row, "dedup_origin": "账本重复"}
                            for row in getattr(self, "_resource_ledger", {}).values()]
        files = parse_torrent_files(torrent_content)
        if not files:
            logger.info(f"{match_source}跳过 {title}：无法读取种子内逐文件大小与路径，按未命中放行")
            return None
        selected_keys = {
            str(path).replace("\\", "/").casefold() for path in accepted_paths or []
        } if accepted_paths is not None else None
        file_paths = [item["path"] for item in files]
        considered = [
            item for item in files
            if self._video_file(item["path"])
            and (selected_keys is None or item["path"].replace("\\", "/").casefold() in selected_keys)
        ]
        excluded: Set[str] = set()
        matched_count = 0
        missing_count = 0
        identity_count = 0
        episode_records = index_records(records)
        pack_records = {}
        for item in considered:
            candidate = parse_candidate_file(item["path"], item.get("size"), title,
                                             spec_hint=spec_hint, allow_movie=task is not None)
            if not candidate:
                if defer_identity:
                    excluded.add(item["path"])
                    identity_count += 1
                    self._pending_identity({"path": item["path"], "specs": {}}, title, task, "无法解析资源")
                continue
            if getattr(MetaInfo(item["path"].rsplit("/", 1)[-1]), "episode_group", None):
                if defer_identity:
                    excluded.add(item["path"])
                    identity_count += 1
                    self._pending_identity(candidate, title, task, "剧集组需核验标准季集号")
                continue
            missing_attributes = [
                key for key in attributes if candidate["specs"].get(key) is None
            ]
            if missing_attributes:
                if unknown_policy == "exclude":
                    excluded.add(item["path"])
                    missing_count += 1
                    logger.info(
                        f"{match_source}排除文件 {item['path']}：候选缺少勾选规格 "
                        f"{', '.join(missing_attributes)}，按缺规格排除策略处理（不是已发布重复）"
                    )
                    continue
                else:
                    logger.info(
                        f"{match_source}放行 {item['path']}：候选缺少勾选规格 "
                        f"{', '.join(missing_attributes)}，按缺规格放行策略处理"
                    )
            candidate_bindings = bindings if candidate.get("media_type") != "movie" else {
                normalize_title(name): int(value) for name, value in
                getattr(self, "_channel_dedup_movie_identity_aliases", {}).items()}
            bound_ids = {candidate_bindings[name] for name in candidate.get("title_keys") or [] if name in candidate_bindings}
            if not candidate.get("tmdb_id") and bound_ids:
                if len(bound_ids) != 1:
                    logger.info(f"{match_source}无法确认剧集身份 {item['path']}：文件名命中互相冲突的别名绑定")
                    if defer_identity:
                        excluded.add(item["path"])
                        identity_count += 1
                        self._pending_identity(candidate, title, task, "别名绑定冲突")
                    continue
                candidate.update({"tmdb_id": bound_ids.pop(), "identity_source": "alias",
                                  "identity_evidence_names": candidate.get("title_keys") or []})
            if defer_identity:
                if not self._verify_resource_identity(candidate, title, task):
                    excluded.add(item["path"])
                    identity_count += 1
                    self._pending_identity(candidate, title, task, "身份未确认，等待别名绑定或识别重试")
                    logger.info(f"资源去重待核验 {item['path']}：不添加下载，非重复命中")
                    continue
                self._resolve_pending_identity(candidate, task)
            if "effects" in attributes and "effects" in candidate["inferred_attributes"]:
                logger.info(
                    f"{match_source}规格推断 {item['path']}：未标动态范围的常规 "
                    f"{candidate['specs']['resolution']} H.264 WEB 资源按 SDR 命名约定比较，非媒体实测"
                )
            matched_records = self._match_channel_candidate(candidate, episode_records, attributes, tolerance, title,
                                                            mode=mode, task=task)
            pack_match = self._match_channel_candidate(candidate, pack_records, attributes, tolerance, title,
                                                       mode=mode, task=task) if pack_dedup else []
            if pack_match:
                matched_records = [external or packed for external, packed in zip(matched_records, pack_match)]
            if matched_records and all(matched_records):
                excluded.add(item["path"])
                matched_count += 1
                matched_from = sorted({record.get("channel", "") for record in matched_records if record})
                origins = sorted({record.get("dedup_origin", "频道重复") for record in matched_records if record})
                episode_text = (f"S{candidate['season']:02d}" +
                    ",".join(f"E{number:02d}" for number in candidate["episodes"])) if candidate["episodes"] else "电影"
                logger.info(
                    f"{match_source}命中 {item['path']}：{episode_text}；"
                    f"{'频道' if records_override is None else '已有任务'} {', '.join(matched_from)}；"
                    f"判定来源 {', '.join(origins)}；匹配规格 "
                    f"{', '.join(f'{key}={candidate['specs'].get(key)}' for key in attributes)}"
                )
            elif pack_dedup:
                for slot, rows in index_records([{
                    **candidate, "channel": "同包已保留文件", "dedup_origin": "包内重复",
                }]).items():
                    pack_records.setdefault(slot, []).extend(rows)
        if not excluded:
            return None
        excluded_keys = {path.replace("\\", "/").casefold() for path in excluded}
        return {
            "excluded": excluded,
            "remaining_paths": {
                path for path in file_paths if path.replace("\\", "/").casefold() not in excluded_keys
            },
            "video_count": len(considered),
            "matched_video_count": matched_count,
            "missing_spec_count": missing_count,
            "identity_pending_count": identity_count,
            "reject_torrent": pack_policy == "exclude",
        }

    def _match_channel_candidate(self, candidate: dict, episode_records: dict,
                                 attributes: List[str], tolerance: float, title: str, mode: str = "specs",
                                 task: Optional[BrushTaskConfig] = None) -> List[Optional[dict]]:
        """One identity/spec contract for pre-add filtering and unfinished-file cancellation."""
        matches = []
        numbers = candidate["episodes"] or ([None] if candidate.get("media_type") == "movie" else [])
        for episode in numbers:
            one_episode = {**candidate, "episodes": [episode] if episode is not None else []}
            plausible = [
                record for record in episode_records.get(resource_slot(candidate, episode),
                    episode_records.get((candidate["season"], episode), []))
                if (mode == "episode" or specs_match(one_episode, record, attributes, tolerance))
                and (mode == "episode" or "size" not in attributes
                     or set(candidate["episodes"]) == set(record.get("episodes") or []))
            ]
            match = next((row for row in plausible if same_release(one_episode, row, attributes, tolerance, mode)), None)
            if not match and any(row.get("tmdb_id") for row in plausible) and not candidate.get("tmdb_id"):
                identity = (self._verify_resource_identity(candidate, title, task) and candidate.get("tmdb_id")
                            if candidate.get("media_type") == "movie"
                            else self._channel_candidate_tmdb_identity(candidate["path"], title))
                if identity:
                    candidate["tmdb_id"] = one_episode["tmdb_id"] = identity
                    match = next((row for row in plausible if same_release(one_episode, row, attributes, tolerance, mode)), None)
            matches.append(match)
        return matches

    @staticmethod
    def _dedup_exclusion_summary(selection: dict) -> str:
        return (
            f"{'整包排除（存在不合格文件）' if selection.get('reject_torrent') else '没有可下载视频'}："
            f"同规格重复 {selection.get('matched_video_count', 0)} 个，"
            f"缺少勾选规格 {selection.get('missing_spec_count', 0)} 个"
            f"，身份待核验 {selection.get('identity_pending_count', 0)} 个"
        )

    @staticmethod
    def _channel_identity_names(path: str, title: str) -> Tuple[Any, Set[str]]:
        meta = MetaInfo(path.rsplit("/", 1)[-1])
        raw_names = {
            str(getattr(meta, field, "") or "").strip() for field in ("cn_name", "en_name", "name")
        } | filename_title_aliases(path)
        raw_names.discard("")
        if not raw_names:
            meta = MetaInfo(title)
            raw_names = {
                str(getattr(meta, field, "") or "").strip() for field in ("cn_name", "en_name", "name")
            } | filename_title_aliases(title)
            raw_names.discard("")
        return meta, raw_names

    def _channel_candidate_tmdb_identity(self, path: str, title: str, cached_only: bool = False) -> Optional[int]:
        """只对已有同季集/规格候选查询身份，并拒绝模糊剧名误识别。"""
        meta, raw_names = self._channel_identity_names(path, title)
        if getattr(meta, "episode_group", None):
            return None
        names = {normalize_title(name) for name in raw_names} - {""}
        if not names:
            return None
        key = (
            tuple(sorted(names)), str(getattr(meta, "year", "") or ""),
            getattr(meta, "begin_season", None) or 1,
        )
        with self._channel_state_lock:
            bound_ids = {
                int(tmdb_id) for name, tmdb_id in getattr(self, "_channel_dedup_identity_aliases", {}).items()
                if normalize_title(name) in names
            }
            if bound_ids:
                if len(bound_ids) == 1:
                    return bound_ids.pop()
                logger.info(f"频道同规格去重无法确认剧集身份 {path}：文件名命中互相冲突的别名绑定")
                return None
            cached_identity = self._channel_identity_cache.get(key)
            if cached_identity and cached_identity[0] > time.monotonic():
                return cached_identity[1]
        if cached_only:
            return None
        identity = None
        try:
            media = self.chain.recognize_media(meta=meta)
            evidence_names = set(raw_names)
            # A bracketed season label is not part of the series title, but its season must agree.
            for name in raw_names:
                suffix = re.search(r"[ ._-]*第\s*[0-9零〇一二两三四五六七八九十百]+\s*季$", name)
                if not suffix or not getattr(meta, "begin_season", None):
                    continue
                season_meta = MetaInfo(name)
                base = name[:suffix.start()].strip()
                if (base and season_meta.begin_season == meta.begin_season
                        and not season_meta.end_season):
                    evidence_names.remove(name)
                    evidence_names.add(base)
            evidence_keys = {normalize_title(name) for name in evidence_names} - {""}
            raw_aliases = {
                str(getattr(media, field, "") or "")
                for field in ("title", "en_title", "original_title", "original_name", "hk_title", "tw_title", "sg_title")
            } | {str(name) for name in getattr(media, "names", []) or []}
            aliases = {normalize_title(name) for name in raw_aliases} - {""}
            chinese_names = {normalize_title(name) for name in evidence_names if re.search(r"[\u3400-\u9fff]", name)}
            chinese_aliases = {normalize_title(name) for name in raw_aliases if re.search(r"[\u3400-\u9fff]", name)}
            year, media_year = str(meta.year or ""), str(getattr(media, "year", "") or "")
            first_season = getattr(meta, "begin_season", None) in (None, 1)
            year_conflict = first_season and year and media_year and year != media_year
            chinese_conflict = chinese_names and chinese_aliases and not chinese_names.intersection(chinese_aliases)
            exact = bool(evidence_keys.intersection(aliases))
            truncated = bool(
                not exact and year and year == media_year
                and any(long_title_prefix_matches(name, raw_aliases) for name in raw_names)
            )
            if not self._is_movie(media) and not year_conflict and not chinese_conflict and (exact or truncated):
                raw_id = getattr(media, "tmdb_id", None)
                identity = int(raw_id) if raw_id else None
                if identity and truncated:
                    logger.info(f"频道同规格去重确认长标题截断别名 {path}：完整词边界及年份一致，TMDB {identity}")
            if not identity:
                reason = (
                    "识别为电影" if self._is_movie(media) else
                    "首季年份不一致" if year_conflict else
                    "中文剧名证据不一致" if chinese_conflict else
                    "剧名与官方别名不匹配" if not (exact or truncated) else
                    "识别结果缺少TMDB ID"
                )
                logger.info(f"频道同规格去重无法确认剧集身份 {path}：{reason}，按未命中放行")
        except Exception as err:
            logger.info(f"频道同规格去重无法确认剧集身份 {path}：{type(err).__name__}，按未命中放行")
        with self._channel_state_lock:
            if len(self._channel_identity_cache) >= 1024:
                self._channel_identity_cache.clear()
            self._channel_identity_cache[key] = (time.monotonic() + (600 if identity else 60), identity)
        return identity

    @staticmethod
    @cached(maxsize=512, ttl=300, skip_none=False, empty_ttl=60, local_only=True)
    def _tmdb_episode_air_date(tmdb_id: int, season: int, episode: int) -> Optional[str]:
        """整季缓存可能落后于单集，缺失时跳过宿主旧缓存核验一次。"""
        try:
            with fresh():
                item = TmdbApi().get_tv_episode_detail(tmdb_id, season, episode)
            if (item and item.get("season_number") == season
                    and item.get("episode_number") == episode):
                return item.get("air_date")
        except Exception as err:
            logger.info(f"TMDB单集日期查询失败 {tmdb_id} S{season}E{episode}：{type(err).__name__}")
        return None

    @staticmethod
    @cached(maxsize=256, ttl=300, empty_ttl=60, local_only=True)
    def _tmdb_web_episode_air_dates(tmdb_id: int, season: int) -> Dict[int, str]:
        """API 尚未同步时，只读官方网页内精确对应季集的显式日期。"""
        try:
            response = RequestUtils(
                ua=settings.NORMAL_USER_AGENT, proxies=settings.PROXY, timeout=12
            ).get_res(url=f"https://www.themoviedb.org/tv/{tmdb_id}/season/{season}?language=en-US")
            if response is None or not response.ok:
                return {}
            return BrushFlow._parse_tmdb_web_episode_air_dates(response.text, tmdb_id, season)
        except Exception as err:
            logger.info(f"TMDB网页日期查询失败 {tmdb_id} S{season}：{type(err).__name__}")
            return {}

    @staticmethod
    def _parse_tmdb_web_episode_air_dates(html: str, tmdb_id: int, season: int) -> Dict[int, str]:
        soup = BeautifulSoup(html, "html.parser")
        dates = {}
        for row in soup.select("div.episode"):
            link = row.select_one("a[data-season-number][data-episode-number]")
            if not link or link.get("data-season-number") != str(season):
                continue
            number = link.get("data-episode-number", "")
            if not number.isdecimal():
                continue
            expected = f"/tv/{tmdb_id}/season/{season}/episode/{int(number)}"
            if urlparse(link.get("href", "")).path != expected:
                continue
            date_node = row.select_one("span.date")
            if not date_node:
                continue
            raw_date = date_node.get_text(" ", strip=True)
            try:
                dates[int(number)] = datetime.strptime(raw_date, "%B %d, %Y").date().isoformat()
            except ValueError:
                continue
        return dates

    @staticmethod
    @cached(maxsize=256, ttl=300, empty_ttl=60, local_only=True)
    def _recent_episode_douban_id(name: str, year: str, season: int) -> str:
        if not name:
            return ""
        try:
            query = name if season == 1 else f"{name} 第{season}季"
            response = RequestUtils(
                ua=settings.NORMAL_USER_AGENT, referer="https://movie.douban.com/", timeout=12
            ).get_res(url="https://movie.douban.com/j/subject_suggest", params={"q": query})
            if response is None or not response.ok:
                return ""
            results = response.json()
            if not isinstance(results, list):
                return ""
            normalize = lambda text: re.sub(r"[^\w]", "", str(text)).casefold()
            matches = set()
            for item in results:
                if not isinstance(item, dict) or not str(item.get("id", "")).isdecimal():
                    continue
                candidate = MetaInfo(str(item.get("title") or ""))
                candidate_name = candidate.cn_name or candidate.en_name or ""
                candidate_season = candidate.begin_season if candidate.begin_season is not None else 1
                if (normalize(candidate_name) == normalize(name) and candidate_season == season
                        and (not year or str(item.get("year") or "") == year)):
                    matches.add(str(item["id"]))
            return matches.pop() if len(matches) == 1 else ""
        except Exception as err:
            logger.info(f"豆瓣剧集匹配失败：{name} S{season}：{type(err).__name__}")
            return ""

    @staticmethod
    @cached(maxsize=256, ttl=300, empty_ttl=60, local_only=True)
    def _douban_episode_air_dates(douban_id: str, season: int) -> Dict[int, Optional[str]]:
        """只使用豆瓣逐集明确提供的日期；不从聚合更新进度推测日期。"""
        if not douban_id.isdecimal():
            return {}
        response = RequestUtils(ua=settings.NORMAL_USER_AGENT, timeout=12).get_res(
            url=f"https://movie.douban.com/j/tv/series/{douban_id}"
        )
        if not response or not response.ok:
            return {}
        try:
            payload = response.json()
        except Exception:
            return {}
        episodes = payload.get("episodes") if isinstance(payload, dict) else None
        date_map: Dict[int, Optional[str]] = {}
        for item in episodes or []:
            if not isinstance(item, dict) or not str(item.get("episode") or "").isdecimal():
                continue
            item_season = item.get("season") or item.get("season_number")
            if item_season is None and season != 1:
                continue
            if item_season is not None and str(item_season) != str(season):
                continue
            raw_date = item.get("air_date")
            if raw_date:
                try:
                    date_map[int(item["episode"])] = datetime.strptime(
                        str(raw_date)[:10], "%Y-%m-%d"
                    ).date().isoformat()
                except ValueError:
                    date_map[int(item["episode"])] = None
        return date_map

    @staticmethod
    def _select_recent_episode_files(downloader: Any, torrent_hash: str, selection: dict,
                                     downloader_type: str, task_name: str) -> bool:
        """暂停状态下只选择通过日期核验的视频；操作失败时保持暂停。"""
        try:
            if not downloader.stop_torrents(torrent_hash):
                logger.error(f"刷流任务 [{task_name}] 无法确认暂停，未执行选集")
                return False
            files = downloader.get_files(torrent_hash)
            if not files:
                logger.error(f"刷流任务 [{task_name}] 获取种子文件清单失败，任务保持暂停")
                return False
            wanted, unwanted = [], []
            for index, item in enumerate(files):
                path = item.get("name") if isinstance(item, dict) else getattr(item, "name", "")
                file_id = item.get("id", index) if isinstance(item, dict) else getattr(item, "id", index)
                normalized_path = str(path or "").replace("\\", "/").lstrip("./")
                selected = any(
                    normalized_path == accepted
                    or normalized_path.endswith("/" + accepted)
                    or accepted.endswith("/" + normalized_path)
                    for accepted in selection["accepted"]
                )
                (wanted if selected else unwanted).append(file_id)
            if not wanted:
                return False
            if downloader_type == "qbittorrent":
                ok = (not unwanted or downloader.set_files(torrent_hash=torrent_hash, file_ids=unwanted, priority=0))
                ok = ok and downloader.set_files(torrent_hash=torrent_hash, file_ids=wanted, priority=1)
            else:
                ok = downloader.set_files(torrent_hash, wanted)
                ok = ok and downloader.set_unwanted_files(torrent_hash, unwanted)
            if not ok or not downloader.start_torrents(torrent_hash):
                downloader.stop_torrents(torrent_hash)
                logger.error(f"刷流任务 [{task_name}] 选集或启动未确认，任务保持暂停")
                return False
            return True
        except Exception as err:
            try:
                downloader.stop_torrents(torrent_hash)
            except Exception:
                pass
            logger.error(f"刷流任务 [{task_name}] 设置近期播出剧集失败，任务保持暂停：{err}")
            return False

    def __qb_torrents_reannounce(self, torrent_hashes: List[str]) -> None:
        """删除 qBittorrent 种子前强制重新汇报 Tracker"""
        downloader = self.downloader
        if not downloader or not getattr(downloader, "qbc", None) or not torrent_hashes:
            return
        try:
            downloader.qbc.torrents_reannounce(torrent_hashes=torrent_hashes)
        except Exception as err:
            logger.error(f"强制重新汇报 Tracker 失败：{str(err)}")

    def __get_hash(self, torrent: Any) -> str:
        """兼容获取 qBittorrent 与 Transmission 种子 Hash"""
        try:
            service = self.service_info
            if service and DownloaderHelper().is_downloader("qbittorrent", service=service):
                return torrent.get("hash") or ""
            return getattr(torrent, "hashString", "") or ""
        except Exception as err:
            logger.error(f"获取种子 Hash 失败：{str(err)}")
            return ""

    def __get_all_hashes(self, torrents: List[Any]) -> List[str]:
        """提取下载器种子列表中的全部有效 Hash"""
        return [torrent_hash for torrent in torrents if (torrent_hash := self.__get_hash(torrent))]

    def __get_label(self, torrent: Any) -> List[str]:
        """兼容获取 qBittorrent 标签和 Transmission Labels"""
        try:
            service = self.service_info
            if service and DownloaderHelper().is_downloader("qbittorrent", service=service):
                return [item.strip() for item in str(torrent.get("tags") or "").split(",") if item.strip()]
            return [str(item).strip() for item in getattr(torrent, "labels", None) or [] if str(item).strip()]
        except Exception as err:
            logger.error(f"获取种子标签失败：{str(err)}")
            return []

    @staticmethod
    def _torrent_complete(torrent_info: dict) -> bool:
        """按下载器实际完成度判断，旧记录缺少进度时兼容原下载量判断。"""
        try:
            progress = torrent_info.get("progress")
            if progress is not None:
                return float(progress) >= 1
            size = float(torrent_info.get("total_size") or torrent_info.get("size") or 0)
            downloaded = float(torrent_info.get("downloaded") or 0)
            return size > 0 and downloaded >= size
        except (TypeError, ValueError):
            return False

    def __get_torrent_info(self, torrent: Any) -> dict:
        """统一提取 qBittorrent 与 transmission-rpc v7 种子状态"""
        now_timestamp = int(time.time())
        service = self.service_info
        if service and DownloaderHelper().is_downloader("qbittorrent", service=service):
            torrent_id = torrent.get("hash")
            title = torrent.get("name")
            added_on = torrent.get("added_on") or 0
            completion_on = torrent.get("completion_on") or 0
            last_activity = torrent.get("last_activity") or 0
            dltime = now_timestamp - added_on if added_on > 0 else 0
            seeding_time = now_timestamp - completion_on if completion_on > 0 else 0
            iatime = now_timestamp - last_activity if last_activity > 0 else 0
            ratio = torrent.get("ratio") or 0
            uploaded = torrent.get("uploaded") or 0
            progress = torrent.get("progress")
            downloaded = torrent.get("downloaded") or 0
            total_size = torrent.get("total_size") or 0
            tags = torrent.get("tags") or ""
            tracker = torrent.get("tracker") or ""
        else:
            torrent_id = getattr(torrent, "hashString", "")
            title = getattr(torrent, "name", "")
            done_date = getattr(torrent, "done_date", None) or getattr(torrent, "date_done", None)
            added_date = getattr(torrent, "added_date", None) or getattr(torrent, "date_added", None)
            activity_date = getattr(torrent, "activity_date", None) or getattr(torrent, "date_active", None)
            done_timestamp = int(done_date.timestamp()) if done_date and done_date.timestamp() > 0 else 0
            added_on = int(added_date.timestamp()) if added_date and added_date.timestamp() > 0 else 0
            activity_timestamp = int(activity_date.timestamp()) if activity_date and activity_date.timestamp() > 0 else 0
            seeding_time = now_timestamp - done_timestamp if done_timestamp else 0
            dltime = now_timestamp - added_on if added_on else 0
            iatime = now_timestamp - activity_timestamp if activity_timestamp else 0
            total_size = getattr(torrent, "total_size", 0) or 0
            progress = getattr(torrent, "progress", 0) or 0
            progress = progress / 100
            downloaded = getattr(torrent, "downloaded_ever", None)
            if downloaded is None:
                downloaded = int(total_size * progress)
            ratio = getattr(torrent, "ratio", 0) or 0
            uploaded = getattr(torrent, "uploaded_ever", None)
            if uploaded is None:
                uploaded = int(downloaded * ratio)
            tags = getattr(torrent, "labels", None) or ""
            tracker_list = getattr(torrent, "tracker_list", None)
            tracker = tracker_list[0] if tracker_list else ""
        avg_upspeed = int(uploaded / dltime) if dltime else uploaded
        return {
            "hash": torrent_id,
            "title": title,
            "seeding_time": seeding_time,
            "ratio": ratio,
            "uploaded": uploaded,
            "downloaded": downloaded,
            "progress": progress,
            "avg_upspeed": avg_upspeed,
            "iatime": iatime,
            "dltime": dltime,
            "total_size": total_size,
            "add_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(added_on)),
            "add_on": added_on,
            "tags": tags,
            "tracker": tracker,
        }

    def __get_average_bandwidth(
        self,
        sample_count: int = 5,
        interval: float = 3.0,
    ) -> Tuple[Optional[float], Optional[float]]:
        """多次采样所有下载器带宽并返回平均值"""
        upload_speeds: List[float] = []
        download_speeds: List[float] = []
        for index in range(sample_count):
            downloader_info = self.__get_downloader_info()
            if downloader_info:
                upload_speeds.append(downloader_info.upload_speed or 0)
                download_speeds.append(downloader_info.download_speed or 0)
            if index < sample_count - 1:
                time.sleep(interval)
        if not upload_speeds or not download_speeds:
            return None, None
        return sum(upload_speeds) / len(upload_speeds), sum(download_speeds) / len(download_speeds)

    def __get_downloader_info(self) -> schemas.DownloaderInfo:
        """通过插件链汇总当前所有下载器的实时传输信息"""
        result = schemas.DownloaderInfo()
        transfer_infos = self.chain.run_module("downloader_info")
        for transfer_info in transfer_infos or []:
            result.download_speed += transfer_info.download_speed
            result.upload_speed += transfer_info.upload_speed
            result.download_size += transfer_info.download_size
            result.upload_size += transfer_info.upload_size
        return result

    def __get_downloading_count(self) -> int:
        """获取带当前任务唯一标签的下载中种子数量"""
        task = self._get_task_config()
        downloader = self.downloader
        if not task or not downloader:
            return 0
        try:
            torrents = downloader.get_downloading_torrents(tags=task.brush_tag)
            return len(torrents or [])
        except Exception as err:
            logger.error(f"获取任务 [{task.name}] 下载数量失败：{str(err)}")
            return 0

    def __get_global_downloading_count(self) -> int:
        """按下载器去重汇总带全局刷流标签或任务标签的下载中种子数量。"""
        total_count = 0
        downloader_names = {task.downloader for task in self._task_configs.values() if task.downloader}
        downloader_helper = DownloaderHelper()
        for downloader_name in downloader_names:
            try:
                service = downloader_helper.get_service(name=downloader_name)
                if not service or not service.instance:
                    continue
                task_tags = {
                    task.brush_tag for task in self._task_configs.values() if task.downloader == downloader_name
                } | {self.GLOBAL_BRUSH_TAG}
                torrents = service.instance.get_downloading_torrents()
                total_count += sum(
                    1 for torrent in torrents or [] if task_tags.intersection(self.__get_label(torrent))
                )
            except Exception as err:
                logger.error(f"获取下载器 [{downloader_name}] 全局刷流下载数量失败：{str(err)}")
        return total_count

    @staticmethod
    def __get_pubminutes(pubdate: str) -> float:
        """计算站点发布时间距当前时间的分钟数"""
        if not pubdate:
            return 0
        try:
            publish_time = datetime.strptime(pubdate.replace("T", " ").replace("Z", ""), "%Y-%m-%d %H:%M:%S")
            return (datetime.now() - publish_time).total_seconds() / 60
        except (TypeError, ValueError) as err:
            logger.error(f"解析发布时间 {pubdate} 失败：{str(err)}")
            return 0

    def __filter_torrents_by_tag(self, torrents: List[Any], exclude_tag: Optional[str]) -> List[Any]:
        """过滤包含任一删除排除标签的种子"""
        if not exclude_tag:
            return torrents
        excluded_tags = {item.strip() for item in exclude_tag.split(",") if item.strip()}
        return [
            torrent for torrent in torrents
            if not excluded_tags.intersection(self.__get_label(torrent))
        ]

    def __get_subscribe_titles(self) -> Set[str]:
        """识别并缓存当前订阅可用于排除匹配的标题集合"""
        task = self._get_task_config()
        if not task or not task.except_subscribe:
            return set()
        subscribes = SubscribeOper().list() or []
        for subscribe in subscribes:
            cache_key = f"{subscribe.id}_{subscribe.name}"
            if cache_key in self._subscribe_infos:
                continue
            titles = [subscribe.name]
            try:
                meta = MetaInfo(subscribe.name)
                meta.year = subscribe.year
                meta.begin_season = subscribe.season or None
                meta.type = MediaType(subscribe.type)
                mediainfo: MediaInfo = self.chain.recognize_media(
                    meta=meta,
                    mtype=meta.type,
                    media_source=subscribe.media_source,
                    media_id=subscribe.media_id,
                    cache=True,
                )
                if mediainfo:
                    titles.extend(mediainfo.names)
            except Exception as err:
                logger.error(f"识别订阅 {subscribe.name} 失败：{str(err)}")
            self._subscribe_infos[cache_key] = [item.strip() for item in titles if item and item.strip()]
        current_keys = {f"{subscribe.id}_{subscribe.name}" for subscribe in subscribes}
        for cache_key in set(self._subscribe_infos) - current_keys:
            self._subscribe_infos.pop(cache_key, None)
        return {title for titles in self._subscribe_infos.values() for title in titles}

    @staticmethod
    def __filter_torrents_contains_subscribe(
        torrents: List[TorrentInfo],
        subscribe_titles: Set[str],
    ) -> List[TorrentInfo]:
        """排除标题或描述命中任一订阅名称的候选种子"""
        if not subscribe_titles:
            return torrents
        included: List[TorrentInfo] = []
        for torrent in torrents:
            title = torrent.title or ""
            description = torrent.description or ""
            if any(item in title or item in description for item in subscribe_titles):
                logger.info(f"命中订阅内容，排除种子：{title}|{description}")
                continue
            included.append(torrent)
        return included

    @staticmethod
    def __bytes_to_gb(size_in_bytes: float) -> float:
        """把字节数转换为 GB"""
        return float(size_in_bytes or 0) / 1024 ** 3

    @staticmethod
    def __calculate_seeding_torrents_size(torrent_tasks: Dict[str, dict]) -> float:
        """计算未删除托管种子的总做种体积"""
        return sum(
            item.get("size", 0) or 0
            for item in torrent_tasks.values()
            if not item.get("deleted")
        )

    def __auto_archive_tasks(self, torrent_tasks: Dict[str, dict]) -> None:
        """按当前任务保留天数归档已删除种子记录"""
        task = self._get_task_config()
        if not task or not task.auto_archive_days or task.auto_archive_days <= 0:
            return
        archived_tasks: Dict[str, dict] = self._current_task_data("archived", {})
        threshold = float(task.auto_archive_days) * 86400
        now_timestamp = time.time()
        archive_hashes = []
        for torrent_hash, item in torrent_tasks.items():
            if not item.get("deleted"):
                continue
            deleted_time = item.get("deleted_time")
            if deleted_time is None or (
                isinstance(deleted_time, (int, float))
                and now_timestamp - deleted_time > threshold
            ):
                archive_hashes.append(torrent_hash)
        for torrent_hash in archive_hashes:
            archived_tasks[torrent_hash] = torrent_tasks.pop(torrent_hash)
        self._save_current_task_data("archived", archived_tasks)

    def _recalculate_statistics(self, task_id: str) -> Dict[str, int]:
        """从当前和归档记录重新计算单任务统计"""
        torrents = self._get_task_data(task_id, "torrents") or {}
        archived = self._get_task_data(task_id, "archived") or {}
        combined = {**archived, **torrents}
        statistic = {
            "count": len(combined),
            "deleted": sum(1 for item in combined.values() if item.get("deleted")),
            "uploaded": sum(item.get("uploaded", 0) or 0 for item in combined.values()),
            "downloaded": sum(item.get("downloaded", 0) or 0 for item in combined.values()),
            "unarchived": sum(1 for item in torrents.values() if item.get("deleted")),
            "active": sum(1 for item in torrents.values() if not item.get("deleted")),
            "active_uploaded": sum(
                item.get("uploaded", 0) or 0 for item in torrents.values() if not item.get("deleted")
            ),
            "active_downloaded": sum(
                item.get("downloaded", 0) or 0 for item in torrents.values() if not item.get("deleted")
            ),
        }
        self._save_task_data(task_id, "statistic", statistic)
        return statistic

    def _get_statistic_info(self, task_id: str) -> Dict[str, int]:
        """读取单任务统计并为历史空数据补齐字段"""
        defaults = {
            "count": 0,
            "deleted": 0,
            "uploaded": 0,
            "downloaded": 0,
            "unarchived": 0,
            "active": 0,
            "active_uploaded": 0,
            "active_downloaded": 0,
        }
        statistic = self._get_task_data(task_id, "statistic") or {}
        return {**defaults, **statistic}

    @staticmethod
    def _is_valid_time_range(time_range: Optional[str]) -> bool:
        """校验 HH:MM-HH:MM 格式的每日时间段"""
        if not time_range or not re.fullmatch(r"\d{2}:\d{2}-\d{2}:\d{2}", time_range):
            return False
        try:
            start, end = time_range.split("-", 1)
            datetime.strptime(start, "%H:%M")
            datetime.strptime(end, "%H:%M")
            return True
        except ValueError:
            return False

    def _is_current_time_in_range(self, task: Optional[BrushTaskConfig] = None) -> bool:
        """判断当前时间是否处于任务允许的每日开启区间"""
        task = task or self._get_task_config()
        if not task or not self._is_valid_time_range(task.active_time_range):
            return True
        start_text, end_text = task.active_time_range.split("-", 1)
        start_time = datetime.strptime(start_text, "%H:%M").time()
        end_time = datetime.strptime(end_text, "%H:%M").time()
        current_time = datetime.now().time()
        if start_time <= end_time:
            return start_time <= current_time <= end_time
        return current_time >= start_time or current_time <= end_time

    @staticmethod
    def __get_site_by_torrent(torrent: Any) -> Tuple[int, str]:
        """根据 Tracker 或磁力链接识别种子所属站点"""
        trackers: List[str] = []
        last_domain = "未知"
        tracker_url = torrent.get("tracker") if isinstance(torrent, dict) else None
        if not tracker_url:
            tracker_list = getattr(torrent, "tracker_list", None)
            tracker_url = tracker_list[0] if tracker_list else None
        if tracker_url:
            trackers.append(tracker_url)
        magnet_link = torrent.get("magnet_uri") if isinstance(torrent, dict) else getattr(torrent, "magnet_link", None)
        if magnet_link:
            trackers.extend(unquote(item) for item in parse_qs(urlparse(magnet_link).query).get("tr", []))
        tracker_mappings = {
            "chdbits.xyz": "ptchdbits.co",
            "agsvpt.trackers.work": "agsvpt.com",
            "tracker.cinefiles.info": "audiences.me",
        }
        for tracker in trackers:
            if not tracker:
                continue
            domain = next(
                (mapped for keyword, mapped in tracker_mappings.items() if keyword in tracker),
                StringUtils.get_url_domain(tracker),
            )
            last_domain = domain or last_domain
            site = SitesHelper().get_indexer(domain)
            if site:
                return site.get("id"), site.get("name")
        return 0, last_domain

    def _log_and_notify_error(self, message: str) -> None:
        """记录错误并写入系统消息中心"""
        logger.error(message)
        self.systemmessage.put(message, title="站点刷流")

    def __send_delete_message(self, torrent_task: dict, reason: str) -> None:
        """发送包含任务、站点、种子和原因的删种通知"""
        task = self._get_task_config()
        if not task or not task.notify:
            return
        text = (
            f"任务：{task.name}\n"
            f"站点：{torrent_task.get('site_name') or '未知'}\n"
            f"标题：{torrent_task.get('title') or '未知'}\n"
            f"原因：{reason}"
        )
        self.post_message(mtype=NotificationType.SiteMessage, title="【刷流任务种子删除】", text=text)

    @staticmethod
    def __build_add_message_text(torrent: Union[TorrentInfo, dict], task_name: str) -> str:
        """兼容候选对象和任务字典构建新增通知文本"""
        def read_value(key: str, default: Any = None) -> Any:
            """统一读取候选对象或字典字段"""
            return torrent.get(key, default) if isinstance(torrent, dict) else getattr(torrent, key, default)

        lines = [f"任务：{task_name}"]
        labels = {
            "site_name": "站点",
            "title": "标题",
            "description": "内容",
            "size": "大小",
            "pubdate": "发布时间",
            "seeders": "做种数",
            "volume_factor": "促销",
            "hit_and_run": "Hit&Run",
        }
        for key, label in labels.items():
            value = read_value(key)
            if key == "size" and value:
                value = StringUtils.str_filesize(value)
            if value not in (None, "", False):
                lines.append(f"{label}：{'是' if key == 'hit_and_run' else value}")
        return "\n".join(lines)

    def __send_add_message(self, torrent: Union[TorrentInfo, dict]) -> None:
        """发送当前任务新增刷流种子的通知"""
        task = self._get_task_config()
        if not task or not task.notify:
            return
        self.post_message(
            mtype=NotificationType.SiteMessage,
            title="【刷流任务种子下载】",
            text=self.__build_add_message_text(torrent, task.name),
        )

    def __send_message(self, title: str, text: str) -> None:
        """按当前任务通知开关发送通用站点消息"""
        task = self._get_task_config()
        if task and task.notify:
            self.post_message(mtype=NotificationType.SiteMessage, title=title, text=text)

    def __log_and_send_torrent_task_update_message(
        self,
        title: str,
        status: str,
        reason: str,
        torrent_tasks: List[dict],
    ) -> None:
        """记录并汇总发送标签同步导致的任务状态变更"""
        if not torrent_tasks:
            return
        task = self._get_task_config()
        first_title = torrent_tasks[0].get("title") or "未知种子"
        text = (
            f"任务：{task.name if task else '未知'}\n"
            f"内容：{first_title} 等 {len(torrent_tasks)} 个种子已{status}\n"
            f"原因：{reason}"
        )
        logger.info(f"{title}，{text}")
        self.__send_message(title, text)
