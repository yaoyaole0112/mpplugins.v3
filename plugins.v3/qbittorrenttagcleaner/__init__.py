import re
from threading import Thread
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase


DEFAULT_PATTERN = r"^[A-Za-z0-9]{8,12}$"
DEFAULT_KEEP_TAGS = "MusicPilot"


def find_removable_tags(
    torrents: List[Any], pattern: re.Pattern, keep_tags: set[str]
) -> Dict[str, List[str]]:
    """Return tag -> torrent hash mapping for tags matching the configured pattern."""
    result: Dict[str, List[str]] = {}
    for torrent in torrents or []:
        if hasattr(torrent, "get"):
            torrent_hash = torrent.get("hash")
            raw_tags = torrent.get("tags")
        else:
            torrent_hash = getattr(torrent, "hash", None)
            raw_tags = getattr(torrent, "tags", None)
        if not torrent_hash:
            continue
        tags = raw_tags.split(",") if isinstance(raw_tags, str) else (raw_tags or [])
        for tag in {str(item).strip() for item in tags if str(item).strip()}:
            if tag not in keep_tags and pattern.fullmatch(tag):
                result.setdefault(tag, []).append(str(torrent_hash))
    return result


class QbittorrentTagCleaner(_PluginBase):
    """Remove random temporary tags from qBittorrent without changing torrents."""

    plugin_name = "qBittorrent随机标签清理"
    plugin_desc = "仅删除 qBittorrent 种子上的随机标签，不删除或修改种子。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot/v3/docs/images/moviepilot.png"
    plugin_version = "1.0.4"
    plugin_author = "helios"
    plugin_order = 99
    plugin_config_prefix = "qbittorrenttagcleaner_"
    auth_level = 1

    _enabled = False
    _downloader_name = ""
    _pattern_text = DEFAULT_PATTERN
    _keep_tags_text = DEFAULT_KEEP_TAGS
    _keep_tags: set[str] = set(DEFAULT_KEEP_TAGS.split(","))
    _cron = "*/10 * * * *"
    _onlyonce = False
    _pattern: Optional[re.Pattern] = None

    def init_plugin(self, config: dict = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._downloader_name = str(config.get("downloader_name") or "").strip()
        self._pattern_text = str(config.get("pattern") or DEFAULT_PATTERN).strip()
        self._keep_tags_text = str(
            config.get("keep_tags") or DEFAULT_KEEP_TAGS
        ).strip()
        self._keep_tags = {
            tag.strip()
            for tag in self._keep_tags_text.split(",")
            if tag.strip()
        }
        self._cron = str(config.get("cron") or "*/10 * * * *").strip()
        self._onlyonce = bool(config.get("onlyonce", False))
        try:
            self._pattern = re.compile(self._pattern_text)
            CronTrigger.from_crontab(self._cron)
        except Exception as err:
            self._pattern = None
            logger.error(f"qBittorrent随机标签清理: 配置无效：{err}")
        if self._onlyonce and self._pattern:
            self._onlyonce = False
            self._save_config()
            Thread(
                target=self.clean,
                name="qbittorrent-tag-cleaner-once",
                daemon=True,
            ).start()
            logger.info("qBittorrent随机标签清理: 已启动立即执行任务")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def _get_downloader(self):
        if not self._downloader_name or not self._pattern:
            return None
        service = DownloaderHelper().get_service(name=self._downloader_name)
        if not service or service.type != "qbittorrent":
            return None
        return service.instance

    def _run_clean(self, source: str) -> None:
        logger.info(f"qBittorrent随机标签清理: 开始执行（{source}）")
        downloader = self._get_downloader()
        if not downloader:
            logger.warning("qBittorrent随机标签清理: 未找到可用的 qBittorrent 下载器")
            return
        try:
            torrents, error = downloader.get_torrents()
            if error or not torrents:
                logger.info(f"qBittorrent随机标签清理: 扫描完成（{source}），无可处理种子")
                return
            removable = find_removable_tags(torrents, self._pattern, self._keep_tags)
            removed = 0
            for tag, torrent_hashes in removable.items():
                if downloader.delete_torrents_tag(ids=torrent_hashes, tag=tag):
                    removed += len(torrent_hashes)
            logger.info(
                f"qBittorrent随机标签清理: 扫描完成（{source}），"
                f"命中 {len(removable)} 个标签，处理 {removed} 个种子"
            )
        except Exception as err:
            logger.error(f"qBittorrent随机标签清理失败：{err}", exc_info=True)
        else:
            logger.info(f"qBittorrent随机标签清理: 执行结束（{source}）")

    def clean(self) -> None:
        self._run_clean("定时/手动")

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._pattern:
            return []
        return [{
            "id": "QbittorrentTagCleaner",
            "name": "qBittorrent随机标签清理",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self.clean,
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run",
            "endpoint": self.clean,
            "methods": ["GET"],
            "summary": "立即清理随机标签",
            "description": "仅删除匹配规则的 qBittorrent 标签，不修改种子。",
        }]

    def get_page(self) -> Optional[List[dict]]:
        return None

    def stop_service(self) -> None:
        return None

    def _save_config(self) -> None:
        self.update_config({
            "enabled": self._enabled,
            "downloader_name": self._downloader_name,
            "pattern": self._pattern_text,
            "keep_tags": self._keep_tags_text,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
        })

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        try:
            options = [
                {"title": conf.name, "value": conf.name}
                for conf in DownloaderHelper().get_configs().values()
                if conf.type == "qbittorrent"
            ]
        except Exception:
            options = []
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "downloader_name",
                                            "label": "qBittorrent 下载器",
                                            "items": options,
                                            "clearable": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "pattern",
                                            "label": "随机标签正则",
                                            "hint": "默认删除 8-12 位纯字母数字标签，例如 0G7NpbzEzF。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "保存后立即运行一次",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "keep_tags",
                                            "label": "保留标签",
                                            "hint": "多个标签用英文逗号分隔，默认保留 MusicPilot。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "hint": "Cron，默认每 10 分钟执行一次。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": self._enabled,
            "downloader_name": self._downloader_name,
            "pattern": self._pattern_text,
            "keep_tags": self._keep_tags_text,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
        }
