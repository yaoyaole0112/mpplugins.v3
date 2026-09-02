import json
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from apscheduler.triggers.cron import CronTrigger
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
import pytz

from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import Event, eventmanager
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfoPath
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import FileItem, NotificationType, RefreshMediaItem, TransferInfo
from app.schemas.types import EventType, MediaType

from .helper.path import match_media_path, split_exts
from .helper.strm import StrmSyncHelper
from .helper.transfer import PanTransferHelper
from .version import VERSION


class CloudPanStrmHelper(_PluginBase):
    """
    通用网盘 STRM 同步与整理插件
    """

    plugin_name = "网盘STRM助手"
    plugin_desc = "仿 115 STRM 助手：全量/增量 STRM 同步、整理监控、网盘目录整理。"
    plugin_icon = (
        "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/"
        "refs/heads/v2/src/assets/images/misc/u115.png"
    )
    plugin_version = VERSION
    plugin_author = "helios"
    author_url = "https://github.com/yaoyaole0112/mpplugins.v3"
    plugin_config_prefix = "cloudpanstrmhelper_"
    plugin_order = 98
    auth_level = 1

    _sync_lock = Lock()
    _transfer_lock = Lock()

    def __init__(self):
        super().__init__()
        self._scheduler = None
        self._enabled = False
        self._notify = False
        self._storage = "115网盘Plus"
        self._moviepilot_address = ""
        self._user_rmt_mediaext = "mp4,mkv,ts,iso,rmvb,avi,wmv,m2ts,mpg,flv,rm,mov"
        self._user_download_mediaext = "srt,ass,ssa,sup,idx,sub,nfo,jpg,png,webp"
        self._strm_url_mode = "plugin"
        self._strm_url_template = ""
        self._once_full_sync_strm = False
        self._timing_full_sync_strm = False
        self._cron_full_sync_strm = "0 */7 * * *"
        self._full_sync_strm_paths = ""
        self._full_sync_overwrite_mode = "never"
        self._full_sync_auto_download_mediainfo_enabled = False
        self._full_sync_remove_unless_strm = False
        self._increment_sync_enabled = False
        self._increment_sync_cron = "*/30 * * * *"
        self._increment_sync_paths = ""
        self._once_increment_sync = False
        self._transfer_monitor_enabled = False
        self._transfer_monitor_paths = ""
        self._transfer_monitor_scrape_metadata_enabled = False
        self._transfer_mp_mediaserver_paths = ""
        self._transfer_monitor_media_server_refresh_enabled = False
        self._transfer_monitor_mediaservers: List[str] = []
        self._pan_transfer_enabled = False
        self._pan_transfer_paths = ""
        self._pan_transfer_cron = "*/10 * * * *"
        self._pan_transfer_min_filesize = 0
        self._once_pan_transfer = False
        self._last_result = ""

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify"))
            self._storage = (config.get("storage") or "115网盘Plus").strip()
            self._moviepilot_address = (
                config.get("moviepilot_address") or ""
            ).strip().rstrip("/")
            self._user_rmt_mediaext = config.get("user_rmt_mediaext") or self._user_rmt_mediaext
            self._user_download_mediaext = (
                config.get("user_download_mediaext") or self._user_download_mediaext
            )
            self._strm_url_mode = config.get("strm_url_mode") or "plugin"
            self._strm_url_template = config.get("strm_url_template") or ""
            self._once_full_sync_strm = bool(config.get("once_full_sync_strm"))
            self._timing_full_sync_strm = bool(config.get("timing_full_sync_strm"))
            self._cron_full_sync_strm = config.get("cron_full_sync_strm") or "0 */7 * * *"
            self._full_sync_strm_paths = config.get("full_sync_strm_paths") or ""
            self._full_sync_overwrite_mode = config.get("full_sync_overwrite_mode") or "never"
            self._full_sync_auto_download_mediainfo_enabled = bool(
                config.get("full_sync_auto_download_mediainfo_enabled")
            )
            self._full_sync_remove_unless_strm = bool(
                config.get("full_sync_remove_unless_strm")
            )
            self._increment_sync_enabled = bool(config.get("increment_sync_enabled"))
            self._increment_sync_cron = config.get("increment_sync_cron") or "*/30 * * * *"
            self._increment_sync_paths = config.get("increment_sync_paths") or ""
            self._once_increment_sync = bool(config.get("once_increment_sync"))
            self._transfer_monitor_enabled = bool(config.get("transfer_monitor_enabled"))
            self._transfer_monitor_paths = config.get("transfer_monitor_paths") or ""
            self._transfer_monitor_scrape_metadata_enabled = bool(
                config.get("transfer_monitor_scrape_metadata_enabled")
            )
            self._transfer_mp_mediaserver_paths = (
                config.get("transfer_mp_mediaserver_paths") or ""
            )
            self._transfer_monitor_media_server_refresh_enabled = bool(
                config.get("transfer_monitor_media_server_refresh_enabled")
            )
            self._transfer_monitor_mediaservers = (
                config.get("transfer_monitor_mediaservers") or []
            )
            self._pan_transfer_enabled = bool(config.get("pan_transfer_enabled"))
            self._pan_transfer_paths = config.get("pan_transfer_paths") or ""
            self._pan_transfer_cron = config.get("pan_transfer_cron") or "*/10 * * * *"
            self._pan_transfer_min_filesize = int(
                config.get("pan_transfer_min_filesize") or 0
            )
            self._once_pan_transfer = bool(config.get("once_pan_transfer"))
            self._last_result = config.get("last_result") or ""

        self._data_path().mkdir(parents=True, exist_ok=True)

        changed = False
        delayed = []
        if self._once_full_sync_strm:
            self._once_full_sync_strm = False
            changed = True
            delayed.append((self.full_sync_strm_files, "网盘STRM全量同步"))
        if self._once_increment_sync:
            self._once_increment_sync = False
            changed = True
            delayed.append((self.increment_sync_strm_files, "网盘STRM增量同步"))
        if self._once_pan_transfer:
            self._once_pan_transfer = False
            changed = True
            delayed.append((self.pan_transfer, "网盘整理"))
        if changed:
            self.__update_config()
        if self._enabled and delayed:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            run_date = datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3)
            for func, name in delayed:
                self._scheduler.add_job(func, "date", run_date=run_date, name=name)
            self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def _data_path(self) -> Path:
        return Path(settings.PLUGIN_DATA_PATH) / "cloudpanstrmhelper"

    def _snapshot_path(self) -> Path:
        return self._data_path() / "increment_snapshot.json"

    def _load_snapshot(self) -> dict:
        path = self._snapshot_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_snapshot(self, data: dict) -> None:
        self._snapshot_path().write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _helper(self, overwrite: Optional[str] = None) -> StrmSyncHelper:
        return StrmSyncHelper(
            storage=self._storage,
            moviepilot_address=self._moviepilot_address,
            mediaext=self._user_rmt_mediaext,
            sidecar_ext=self._user_download_mediaext,
            overwrite_mode=overwrite or self._full_sync_overwrite_mode,
            auto_download_sidecar=self._full_sync_auto_download_mediainfo_enabled,
            strm_url_mode=self._strm_url_mode,
            strm_url_template=self._strm_url_template,
        )

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "storage": self._storage,
                "moviepilot_address": self._moviepilot_address,
                "user_rmt_mediaext": self._user_rmt_mediaext,
                "user_download_mediaext": self._user_download_mediaext,
                "strm_url_mode": self._strm_url_mode,
                "strm_url_template": self._strm_url_template,
                "once_full_sync_strm": self._once_full_sync_strm,
                "timing_full_sync_strm": self._timing_full_sync_strm,
                "cron_full_sync_strm": self._cron_full_sync_strm,
                "full_sync_strm_paths": self._full_sync_strm_paths,
                "full_sync_overwrite_mode": self._full_sync_overwrite_mode,
                "full_sync_auto_download_mediainfo_enabled": self._full_sync_auto_download_mediainfo_enabled,
                "full_sync_remove_unless_strm": self._full_sync_remove_unless_strm,
                "increment_sync_enabled": self._increment_sync_enabled,
                "increment_sync_cron": self._increment_sync_cron,
                "increment_sync_paths": self._increment_sync_paths,
                "once_increment_sync": self._once_increment_sync,
                "transfer_monitor_enabled": self._transfer_monitor_enabled,
                "transfer_monitor_paths": self._transfer_monitor_paths,
                "transfer_monitor_scrape_metadata_enabled": self._transfer_monitor_scrape_metadata_enabled,
                "transfer_mp_mediaserver_paths": self._transfer_mp_mediaserver_paths,
                "transfer_monitor_media_server_refresh_enabled": self._transfer_monitor_media_server_refresh_enabled,
                "transfer_monitor_mediaservers": self._transfer_monitor_mediaservers,
                "pan_transfer_enabled": self._pan_transfer_enabled,
                "pan_transfer_paths": self._pan_transfer_paths,
                "pan_transfer_cron": self._pan_transfer_cron,
                "pan_transfer_min_filesize": self._pan_transfer_min_filesize,
                "once_pan_transfer": self._once_pan_transfer,
                "last_result": self._last_result,
            }
        )

    def _notify_result(self, title: str, text: str) -> None:
        self._last_result = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {title}\n{text}"
        self.__update_config()
        logger.info("%s\n%s", title, text)
        if self._notify:
            self.post_message(title=title, text=text, mtype=NotificationType.Manual)

    def full_sync_strm_files(self):
        if not self._enabled or not self._full_sync_strm_paths:
            return
        if not self._sync_lock.acquire(blocking=False):
            logger.warning("【STRM同步】已有任务在运行，跳过全量同步")
            return
        try:
            helper = self._helper()
            result = helper.sync_paths(
                self._full_sync_strm_paths,
                incremental=False,
                remove_unless=self._full_sync_remove_unless_strm,
            )
            if result.get("snapshot"):
                self._save_snapshot(result["snapshot"])
            self._notify_result(
                "全量 STRM 同步完成",
                (
                    f"生成 {result['strm_count']}，跳过 {result['strm_skip_count']}，"
                    f"失败 {result['strm_fail_count']}，附属 {result['sidecar_count']}，"
                    f"清理 {result['remove_count']}"
                ),
            )
        except Exception as err:
            logger.error("【STRM同步】全量同步失败: %s", err)
            self._notify_result("全量 STRM 同步失败", str(err))
        finally:
            self._sync_lock.release()

    def increment_sync_strm_files(self):
        if not self._enabled:
            return
        paths = self._increment_sync_paths or self._full_sync_strm_paths
        if not paths:
            return
        if not self._sync_lock.acquire(blocking=False):
            logger.warning("【STRM同步】已有任务在运行，跳过增量同步")
            return
        try:
            helper = self._helper(overwrite="always")
            result = helper.sync_paths(
                paths,
                snapshot=self._load_snapshot(),
                incremental=True,
                remove_unless=self._full_sync_remove_unless_strm,
            )
            if result.get("snapshot"):
                self._save_snapshot(result["snapshot"])
            self._notify_result(
                "增量 STRM 同步完成",
                (
                    f"生成 {result['strm_count']}，跳过 {result['strm_skip_count']}，"
                    f"失败 {result['strm_fail_count']}，附属 {result['sidecar_count']}，"
                    f"清理 {result['remove_count']}"
                ),
            )
        except Exception as err:
            logger.error("【STRM同步】增量同步失败: %s", err)
            self._notify_result("增量 STRM 同步失败", str(err))
        finally:
            self._sync_lock.release()

    def pan_transfer(self):
        if not self._enabled or not self._pan_transfer_enabled or not self._pan_transfer_paths:
            return
        if not self._transfer_lock.acquire(blocking=False):
            logger.warning("【网盘整理】已有任务在运行，跳过")
            return
        try:
            helper = PanTransferHelper(
                storage=self._storage,
                mediaext=self._user_rmt_mediaext,
                min_filesize=self._pan_transfer_min_filesize,
            )
            result = helper.scan_and_transfer(self._pan_transfer_paths)
            self._notify_result(
                "网盘整理提交完成",
                (
                    f"入队 {len(result['queued'])}，跳过 {len(result['skipped'])}，"
                    f"失败 {len(result['failed'])}"
                ),
            )
        except Exception as err:
            logger.error("【网盘整理】运行失败: %s", err)
            self._notify_result("网盘整理失败", str(err))
        finally:
            self._transfer_lock.release()

    @eventmanager.register(EventType.TransferComplete)
    def generate_strm(self, event: Event):
        if (
            not self._enabled
            or not self._transfer_monitor_enabled
            or not self._transfer_monitor_paths
            or not self._moviepilot_address
        ):
            return
        data = event.event_data or {}
        item_transfer: TransferInfo = data.get("transferinfo")
        mediainfo: MediaInfo = data.get("mediainfo")
        meta: MetaBase = data.get("meta")
        if not item_transfer or not getattr(item_transfer, "target_item", None):
            return
        target_item: FileItem = item_transfer.target_item
        if target_item.storage != self._storage:
            return
        dest_dir = getattr(item_transfer, "target_diritem", None)
        dest_dir_path = dest_dir.path if dest_dir else str(Path(target_item.path).parent)
        matched, local_dir, pan_dir = match_media_path(
            self._transfer_monitor_paths, dest_dir_path
        )
        if not matched:
            return
        helper = self._helper(overwrite="always")
        suffix = Path(target_item.name or "").suffix.lower()
        if suffix not in split_exts(self._user_rmt_mediaext):
            return
        strm_path = helper.write_strm(local_dir, pan_dir, target_item, overwrite=True)
        if not strm_path:
            return

        subtitle_list = getattr(item_transfer, "subtitle_list_new", []) or []
        audio_list = getattr(item_transfer, "audio_list_new", []) or []
        chain = StorageChain()
        for extra in list(subtitle_list) + list(audio_list):
            extra_item = chain.get_file_item(storage=self._storage, path=Path(extra))
            if extra_item:
                helper.auto_download_sidecar = True
                helper.download_sidecar(local_dir, pan_dir, extra_item)

        if self._transfer_monitor_scrape_metadata_enabled:
            self._scrape(strm_path, target_item.name, mediainfo, meta)
        if self._transfer_monitor_media_server_refresh_enabled and mediainfo:
            self._refresh_mediaserver(strm_path, mediainfo)

    def _scrape(
        self,
        path: str,
        item_name: str,
        mediainfo: Optional[MediaInfo],
        meta: Optional[MetaBase],
    ) -> None:
        try:
            mediachain = MediaChain()
            file_path = Path(path)
            if mediainfo and mediainfo.type == MediaType.MOVIE:
                dir_path = file_path.parent
                fileitem = FileItem(
                    storage="local",
                    type="dir",
                    path=str(dir_path),
                    name=dir_path.name,
                    basename=dir_path.stem,
                    modify_time=dir_path.stat().st_mtime,
                )
            else:
                fileitem = FileItem(
                    storage="local",
                    type="file",
                    path=str(file_path).replace("\\", "/"),
                    name=file_path.name,
                    basename=file_path.stem,
                    extension=file_path.suffix[1:],
                    size=file_path.stat().st_size if file_path.exists() else 0,
                    modify_time=file_path.stat().st_mtime if file_path.exists() else 0,
                )
            if not mediainfo:
                meta = meta or MetaInfoPath(file_path)
                mediainfo = mediachain.recognize_by_meta(meta)
            mediachain.scrape_metadata(fileitem=fileitem, meta=meta, mediainfo=mediainfo)
            logger.info("【媒体刮削】%s 完成", item_name)
        except Exception as err:
            logger.warning("【媒体刮削】%s 失败: %s", item_name, err)

    def _refresh_mediaserver(self, strm_path: str, mediainfo: MediaInfo) -> None:
        refresh_path = strm_path
        if self._transfer_mp_mediaserver_paths:
            matched, mediaserver_path, moviepilot_path = match_media_path(
                self._transfer_mp_mediaserver_paths, strm_path
            )
            if matched and mediaserver_path and moviepilot_path:
                refresh_path = strm_path.replace(moviepilot_path, mediaserver_path)
        items = [
            RefreshMediaItem(
                title=mediainfo.title,
                year=mediainfo.year,
                type=mediainfo.type,
                category=mediainfo.category,
                target_path=Path(refresh_path),
            )
        ]
        services = MediaServerHelper().get_services(
            name_filters=self._transfer_monitor_mediaservers or None
        )
        for name, service in (services or {}).items():
            instance = getattr(service, "instance", None)
            if instance and hasattr(instance, "refresh_library_by_items"):
                instance.refresh_library_by_items(items)
            elif instance and hasattr(instance, "refresh_root_library"):
                instance.refresh_root_library()
            else:
                logger.warning("【媒体服务器刷新】%s 不支持刷新", name)

    def redirect_url(self, request: Request, storage: str = "", path: str = ""):
        storage = unquote(storage or self._storage)
        path = unquote(path or "")
        if not path:
            return JSONResponse({"success": False, "message": "缺少 path"}, 400)
        fileitem = StorageChain().get_file_item(storage=storage, path=Path(path))
        if not fileitem:
            return JSONResponse({"success": False, "message": "文件不存在"}, 404)
        url = getattr(fileitem, "download_url", None)
        if url:
            return RedirectResponse(str(url), 302)
        pickcode = getattr(fileitem, "pickcode", None)
        if pickcode:
            target = (
                f"{self._moviepilot_address or str(request.base_url).rstrip('/')}"
                f"/api/v1/plugin/P115StrmHelper/redirect_url"
                f"?apikey={settings.API_TOKEN}&pickcode={pickcode}"
            )
            if fileitem.name:
                from urllib.parse import quote

                target += f"&file_name={quote(fileitem.name)}"
            return RedirectResponse(target, 302)
        return JSONResponse(
            {
                "success": False,
                "message": "当前存储未提供 download_url，请改用 URL 模式 115 或自定义模板",
            },
            500,
        )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/cloudpan_full_sync",
                "event": EventType.PluginAction,
                "desc": "网盘STRM全量同步",
                "data": {"action": "cloudpan_full_sync"},
            },
            {
                "cmd": "/cloudpan_inc_sync",
                "event": EventType.PluginAction,
                "desc": "网盘STRM增量同步",
                "data": {"action": "cloudpan_inc_sync"},
            },
            {
                "cmd": "/cloudpan_transfer",
                "event": EventType.PluginAction,
                "desc": "网盘整理",
                "data": {"action": "cloudpan_transfer"},
            },
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event):
        data = (event.event_data or {}) if event else {}
        action = data.get("action")
        if action == "cloudpan_full_sync":
            self.full_sync_strm_files()
        elif action == "cloudpan_inc_sync":
            self.increment_sync_strm_files()
        elif action == "cloudpan_transfer":
            self.pan_transfer()

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/redirect_url",
                "endpoint": self.redirect_url,
                "methods": ["GET", "POST", "HEAD"],
                "summary": "STRM 302 跳转",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        jobs: List[Dict[str, Any]] = []
        if not self._enabled:
            return jobs
        if self._timing_full_sync_strm and self._full_sync_strm_paths:
            jobs.append(
                {
                    "id": "CloudPanStrmHelper_full_sync",
                    "name": "网盘STRM全量同步",
                    "trigger": CronTrigger.from_crontab(self._cron_full_sync_strm),
                    "func": self.full_sync_strm_files,
                    "kwargs": {},
                }
            )
        if self._increment_sync_enabled and (
            self._increment_sync_paths or self._full_sync_strm_paths
        ):
            jobs.append(
                {
                    "id": "CloudPanStrmHelper_increment_sync",
                    "name": "网盘STRM增量同步",
                    "trigger": CronTrigger.from_crontab(self._increment_sync_cron),
                    "func": self.increment_sync_strm_files,
                    "kwargs": {},
                }
            )
        if self._pan_transfer_enabled and self._pan_transfer_paths:
            jobs.append(
                {
                    "id": "CloudPanStrmHelper_pan_transfer",
                    "name": "网盘整理",
                    "trigger": CronTrigger.from_crontab(self._pan_transfer_cron),
                    "func": self.pan_transfer,
                    "kwargs": {},
                }
            )
        return jobs

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        mediaserver_helper = MediaServerHelper()

        def switch(model: str, label: str, md: int = 3) -> dict:
            return {
                "component": "VCol",
                "props": {"cols": 12, "md": md},
                "content": [
                    {"component": "VSwitch", "props": {"model": model, "label": label}}
                ],
            }

        def text(model: str, label: str, hint: str = "", md: int = 4) -> dict:
            props = {"model": model, "label": label}
            if hint:
                props["hint"] = hint
                props["persistent-hint"] = True
            return {
                "component": "VCol",
                "props": {"cols": 12, "md": md},
                "content": [{"component": "VTextField", "props": props}],
            }

        def area(model: str, label: str, placeholder: str, hint: str = "") -> dict:
            props = {
                "model": model,
                "label": label,
                "rows": 5,
                "placeholder": placeholder,
            }
            if hint:
                props["hint"] = hint
                props["persistent-hint"] = True
            return {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [{"component": "VTextarea", "props": props}],
                    }
                ],
            }

        basic = [
            {
                "component": "VRow",
                "content": [
                    switch("enabled", "启用插件"),
                    switch("notify", "发送通知"),
                    text(
                        "storage",
                        "存储名称",
                        "需与 MoviePilot 存储模块名称一致，如 115网盘Plus / CloudDrive储存 / 123云盘",
                        6,
                    ),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    text(
                        "moviepilot_address",
                        "MoviePilot 外网地址",
                        "用于写入 STRM 的 302 地址，例如 http://192.168.1.10:3000",
                        12,
                    )
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSelect",
                                "props": {
                                    "model": "strm_url_mode",
                                    "label": "STRM URL 模式",
                                    "items": [
                                        {"title": "插件302", "value": "plugin"},
                                        {"title": "转交115助手", "value": "115"},
                                        {"title": "直链download_url", "value": "download"},
                                        {"title": "自定义模板", "value": "template"},
                                    ],
                                },
                            }
                        ],
                    },
                    text("user_rmt_mediaext", "媒体扩展名", "", 4),
                    text("user_download_mediaext", "附属文件扩展名", "", 4),
                ],
            },
            area(
                "strm_url_template",
                "自定义 STRM URL 模板",
                "{address}/d/{path_encoded}",
                "可用变量: address storage path path_encoded name fileid pickcode download_url",
            ),
        ]
        full_sync = [
            {
                "component": "VRow",
                "content": [
                    switch("once_full_sync_strm", "立即全量同步一次"),
                    switch("timing_full_sync_strm", "定时全量同步"),
                    switch("full_sync_auto_download_mediainfo_enabled", "下载字幕/NFO"),
                    switch("full_sync_remove_unless_strm", "清理失效STRM"),
                ],
            },
            {
                "component": "VRow",
                "content": [
                    text("cron_full_sync_strm", "全量同步周期", "Cron", 4),
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSelect",
                                "props": {
                                    "model": "full_sync_overwrite_mode",
                                    "label": "覆盖模式",
                                    "items": [
                                        {"title": "已存在则跳过", "value": "never"},
                                        {"title": "始终覆盖", "value": "always"},
                                    ],
                                },
                            }
                        ],
                    },
                ],
            },
            area(
                "full_sync_strm_paths",
                "全量同步目录",
                "/strm/电影#/媒体库/电影\n/strm/剧集#/媒体库/剧集",
                "一行一个：本地STRM目录#网盘媒体目录",
            ),
        ]
        increment = [
            {
                "component": "VRow",
                "content": [
                    switch("increment_sync_enabled", "启用增量同步"),
                    switch("once_increment_sync", "立即增量同步一次"),
                    text("increment_sync_cron", "增量同步周期", "Cron", 6),
                ],
            },
            area(
                "increment_sync_paths",
                "增量同步目录",
                "留空则复用全量同步目录",
                "一行一个：本地STRM目录#网盘媒体目录",
            ),
        ]
        monitor = [
            {
                "component": "VRow",
                "content": [
                    switch("transfer_monitor_enabled", "整理事件监控"),
                    switch("transfer_monitor_scrape_metadata_enabled", "STRM自动刮削"),
                    switch("transfer_monitor_media_server_refresh_enabled", "刷新媒体服务器"),
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VSelect",
                                "props": {
                                    "multiple": True,
                                    "chips": True,
                                    "clearable": True,
                                    "model": "transfer_monitor_mediaservers",
                                    "label": "媒体服务器",
                                    "items": [
                                        {"title": config.name, "value": config.name}
                                        for config in mediaserver_helper.get_configs().values()
                                    ],
                                },
                            }
                        ],
                    },
                ],
            },
            area(
                "transfer_monitor_paths",
                "整理监控目录",
                "/strm/电影#/媒体库/电影",
                "监控 MoviePilot 整理完成事件，按网盘目标路径生成 STRM",
            ),
            area(
                "transfer_mp_mediaserver_paths",
                "媒体服务器路径替换",
                "/media#/strm",
                "媒体库路径#MoviePilot路径",
            ),
        ]
        pan = [
            {
                "component": "VRow",
                "content": [
                    switch("pan_transfer_enabled", "启用网盘整理"),
                    switch("once_pan_transfer", "立即整理一次"),
                    text("pan_transfer_cron", "整理扫描周期", "Cron", 4),
                    text("pan_transfer_min_filesize", "最小文件(MB)", "0 表示不限制", 2),
                ],
            },
            area(
                "pan_transfer_paths",
                "网盘整理目录",
                "/下载/电影\n/离线下载",
                "一行一个网盘目录。扫描后把媒体文件交给 MoviePilot 目录整理入库",
            ),
        ]

        def tab(value: str, icon: str, text: str) -> dict:
            return {
                "component": "VTab",
                "props": {"value": value},
                "content": [
                    {"component": "VIcon", "props": {"icon": icon, "start": True}},
                    {"component": "span", "text": text},
                ],
            }

        def window(value: str, content: list) -> dict:
            return {
                "component": "VWindowItem",
                "props": {"value": value},
                "content": [{"component": "VCardText", "content": content}],
            }

        return [
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {
                        "component": "VTabs",
                        "props": {"model": "tab", "grow": True, "color": "primary"},
                        "content": [
                            tab("basic", "mdi-cog", "基础"),
                            tab("full", "mdi-sync", "全量STRM"),
                            tab("inc", "mdi-delta", "增量STRM"),
                            tab("monitor", "mdi-eye", "整理监控"),
                            tab("pan", "mdi-folder-move", "网盘整理"),
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VWindow",
                        "props": {"model": "tab"},
                        "content": [
                            window("basic", basic),
                            window("full", full_sync),
                            window("inc", increment),
                            window("monitor", monitor),
                            window("pan", pan),
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "storage": "115网盘Plus",
            "moviepilot_address": "",
            "user_rmt_mediaext": "mp4,mkv,ts,iso,rmvb,avi,wmv,m2ts,mpg,flv,rm,mov",
            "user_download_mediaext": "srt,ass,ssa,sup,idx,sub,nfo,jpg,png,webp",
            "strm_url_mode": "plugin",
            "strm_url_template": "",
            "once_full_sync_strm": False,
            "timing_full_sync_strm": False,
            "cron_full_sync_strm": "0 */7 * * *",
            "full_sync_strm_paths": "",
            "full_sync_overwrite_mode": "never",
            "full_sync_auto_download_mediainfo_enabled": False,
            "full_sync_remove_unless_strm": False,
            "increment_sync_enabled": False,
            "increment_sync_cron": "*/30 * * * *",
            "increment_sync_paths": "",
            "once_increment_sync": False,
            "transfer_monitor_enabled": False,
            "transfer_monitor_paths": "",
            "transfer_monitor_scrape_metadata_enabled": False,
            "transfer_mp_mediaserver_paths": "",
            "transfer_monitor_media_server_refresh_enabled": False,
            "transfer_monitor_mediaservers": [],
            "pan_transfer_enabled": False,
            "pan_transfer_paths": "",
            "pan_transfer_cron": "*/10 * * * *",
            "pan_transfer_min_filesize": 0,
            "once_pan_transfer": False,
            "tab": "basic",
        }

    def get_page(self) -> List[dict]:
        text = self._last_result or "还没有运行记录。"
        return [
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal"},
                "text": text,
            }
        ]

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as err:
            logger.error("退出插件失败：%s", err)
