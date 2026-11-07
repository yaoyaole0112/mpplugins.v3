"""Download protected Discuz RSS attachments into a MoviePilot qBittorrent service."""

from __future__ import annotations

import hashlib
import html
import re
from threading import Thread
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.services import DownloaderHelper


ATTACHMENT_RE = re.compile(r"(?:^|[?&])mod=attachment(?:&|$)", re.I)
TORRENT_NAME_RE = re.compile(r"\.torrent(?:$|[?#])", re.I)
HTML_RE = re.compile(rb"<(?:!doctype|html|body)\b", re.I)


def _text(value: Optional[str]) -> str:
    return " ".join((value or "").split())


def parse_rss_items(xml_body: bytes | str, feed_url: str) -> List[Dict[str, str]]:
    """Return RSS items while tolerating namespaces and malformed descriptions."""
    root = ElementTree.fromstring(xml_body)
    items: List[Dict[str, str]] = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1].lower() != "item":
            continue
        values: Dict[str, str] = {}
        for child in item:
            key = child.tag.rsplit("}", 1)[-1].lower()
            values[key] = "".join(child.itertext()).strip()
        link = values.get("link", "").strip()
        if not link:
            continue
        items.append({
            "title": _text(values.get("title")) or link,
            "link": urljoin(feed_url, html.unescape(link)),
            "description": values.get("description", ""),
        })
    return items


class _AttachmentParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._anchor_href: Optional[str] = None
        self._anchor_text: List[str] = []
        self.links: List[Tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._anchor_href = urljoin(self.base_url, html.unescape(href))
            self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._anchor_href:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._anchor_href:
            return
        text = _text("".join(self._anchor_text))
        self.links.append((self._anchor_href, text))
        self._anchor_href = None
        self._anchor_text = []


def extract_attachment_urls(post_body: bytes | str, post_url: str) -> List[str]:
    """Extract Discuz attachment URLs, preferring links that look like torrent files."""
    parser = _AttachmentParser(post_url)
    parser.feed(post_body.decode("utf-8", errors="ignore") if isinstance(post_body, bytes) else post_body)
    candidates: List[str] = []
    for href, text in parser.links:
        parsed = urlparse(href)
        haystack = f"{href} {text}"
        if ATTACHMENT_RE.search(parsed.query) or TORRENT_NAME_RE.search(haystack):
            if href not in candidates:
                candidates.append(href)
    return candidates


def is_torrent_payload(content: bytes) -> bool:
    """Reject login/error HTML before handing bytes to qBittorrent."""
    if not content or HTML_RE.search(content[:4096]):
        return False
    stripped = content.lstrip()
    return stripped.startswith(b"d") and b"announce" in content[:16384] or (
        len(content) >= 20 and not stripped.startswith(b"<")
    )


class ForumRssQb(_PluginBase):
    plugin_name = "论坛 RSS 种子下载"
    plugin_desc = "读取论坛 RSS，登录帖子提取 .torrent 附件并交给 qBittorrent。"
    plugin_icon = "https://raw.githubusercontent.com/MoviePilot/v3/docs/images/moviepilot.png"
    plugin_version = "1.0.3"
    plugin_author = "helios"
    author_url = ""
    plugin_config_prefix = "forumrssqb_"
    plugin_order = 50
    auth_level = 2

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._rss_url = str(config.get("rss_url") or "").strip()
        self._site_url = str(config.get("site_url") or "").strip()
        self._cookie = str(config.get("cookie") or "").strip()
        self._downloader_name = str(config.get("downloader_name") or "").strip()
        self._save_path = str(config.get("save_path") or "").strip() or None
        self._category = str(config.get("category") or "").strip() or None
        self._tag = str(config.get("tag") or "ForumRSS").strip() or None
        self._cron = str(config.get("cron") or "*/10 * * * *").strip()
        self._only_once = bool(config.get("only_once", False))
        self._max_items = max(1, min(int(config.get("max_items") or 20), 100))
        self._user_agent = str(
            config.get("user_agent")
            or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        ).strip()
        if self._only_once:
            self._only_once = False
            self._save_config()
            Thread(
                target=self.run,
                name="forum-rss-qb-once",
                daemon=True,
            ).start()

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        """The host owns the public scheduler; there is no private worker to stop."""
        return None

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def _save_config(self) -> None:
        self.update_config({
            "enabled": self._enabled,
            "rss_url": self._rss_url,
            "site_url": self._site_url,
            "cookie": self._cookie,
            "downloader_name": self._downloader_name,
            "save_path": self._save_path or "",
            "category": self._category or "",
            "tag": self._tag or "",
            "cron": self._cron,
            "only_once": self._only_once,
            "max_items": self._max_items,
            "user_agent": self._user_agent,
        })

    def _request(self, url: str):
        return RequestUtils(cookies=self._cookie, ua=self._user_agent).get_res(url=url)

    def _site_normalize(self, url: str) -> str:
        """Use the configured login host when RSS contains a mirror hostname."""
        if not self._site_url:
            return url
        configured = urlparse(self._site_url)
        current = urlparse(url)
        if not configured.netloc or not current.netloc:
            return url
        return urlunparse(current._replace(
            scheme=configured.scheme or current.scheme,
            netloc=configured.netloc,
        ))

    def _get_downloader(self):
        if not self._downloader_name:
            return None
        service = DownloaderHelper().get_service(name=self._downloader_name)
        helper = DownloaderHelper()
        if not service or not service.instance or not helper.is_downloader("qbittorrent", service=service):
            return None
        if service.instance.is_inactive():
            return None
        return service.instance

    def _history(self) -> Dict[str, Dict[str, Any]]:
        history = self.get_data("history") or {}
        return history if isinstance(history, dict) else {}

    def run(self) -> Dict[str, Any]:
        if not self._rss_url:
            return {"success": False, "message": "未配置 RSS 地址"}
        downloader = self._get_downloader()
        if not downloader:
            return {"success": False, "message": "未找到可用的 qBittorrent 下载器"}
        rss_response = self._request(self._rss_url)
        if not rss_response or not rss_response.ok:
            return {"success": False, "message": "RSS 请求失败"}
        try:
            items = parse_rss_items(rss_response.content, self._rss_url)[: self._max_items]
        except Exception as error:
            logger.error("论坛 RSS 种子下载：解析 RSS 失败：%s", error)
            return {"success": False, "message": "RSS XML 解析失败"}
        history = self._history()
        added = skipped = failed = 0
        for item in items:
            post_url = self._site_normalize(item["link"])
            post_response = self._request(post_url)
            if not post_response or not post_response.ok:
                failed += 1
                continue
            attachments = extract_attachment_urls(post_response.content, post_url)
            # Some RSS feeds already include the attachment URL in the description.
            if not attachments:
                attachments = extract_attachment_urls(item.get("description", ""), post_url)
            for attachment_url in attachments:
                attachment_url = self._site_normalize(attachment_url)
                if attachment_url in history and history[attachment_url].get("status") == "added":
                    skipped += 1
                    continue
                attachment_response = self._request(attachment_url)
                payload = attachment_response.content if attachment_response and attachment_response.ok else b""
                if not is_torrent_payload(payload):
                    history[attachment_url] = {"status": "failed", "title": item["title"]}
                    failed += 1
                    continue
                digest = hashlib.sha256(payload).hexdigest()
                try:
                    ok = downloader.add_torrent(
                        content=payload,
                        download_dir=self._save_path,
                        category=self._category,
                        tag=[self._tag] if self._tag else None,
                    )
                except Exception as error:
                    logger.error("论坛 RSS 种子下载：提交 qB 失败：%s", error)
                    ok = False
                history[attachment_url] = {
                    "status": "added" if ok else "failed",
                    "title": item["title"],
                    "sha256": digest,
                    "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
                added += bool(ok)
                failed += not bool(ok)
        self.save_data("history", history)
        result = {"success": failed == 0, "message": f"扫描 {len(items)} 条，新增 {added}，跳过 {skipped}，失败 {failed}"}
        self.save_data("last_result", result)
        logger.info("论坛 RSS 种子下载：%s", result["message"])
        return result

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run",
            "endpoint": self.run,
            "methods": ["POST"],
            "summary": "立即扫描 RSS 并下载种子",
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except ValueError:
            logger.error("论坛 RSS 种子下载：Cron 无效：%s", self._cron)
            return []
        return [{
            "id": self.__class__.__name__,
            "name": self.plugin_name,
            "trigger": trigger,
            "func": self.run,
            "kwargs": {},
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        try:
            downloaders = [
                {"title": conf.name, "value": conf.name}
                for conf in DownloaderHelper().get_configs().values()
                if conf.type == "qbittorrent"
            ]
        except Exception:
            downloaders = []
        return [{
            "component": "VForm",
            "content": [
                {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}},
                {"component": "VTextField", "props": {"model": "rss_url", "label": "RSS 地址", "required": True}},
                {"component": "VTextField", "props": {"model": "site_url", "label": "站点首页（可选）"}},
                {"component": "VTextarea", "props": {"model": "cookie", "label": "论坛登录 Cookie", "hint": "只保存在 MoviePilot 配置中，不要填账号密码。", "persistentHint": True}},
                {"component": "VSelect", "props": {"model": "downloader_name", "label": "qBittorrent 下载器", "items": downloaders, "required": True}},
                {"component": "VTextField", "props": {"model": "save_path", "label": "保存目录（可选）"}},
                {"component": "VTextField", "props": {"model": "category", "label": "qB 分类（可选）"}},
                {"component": "VTextField", "props": {"model": "tag", "label": "qB 标签", "hint": "默认 ForumRSS"}},
                {"component": "VTextField", "props": {"model": "cron", "label": "执行周期（Cron）", "hint": "默认每 10 分钟"}},
                {"component": "VTextField", "props": {"model": "max_items", "label": "每次最多处理 RSS 条目数", "type": "number"}},
                {"component": "VSwitch", "props": {"model": "only_once", "label": "保存后立即运行一次"}},
            ],
        }], {
            "enabled": False,
            "rss_url": "",
            "site_url": "",
            "cookie": "",
            "downloader_name": "",
            "save_path": "",
            "category": "",
            "tag": "ForumRSS",
            "cron": "*/10 * * * *",
            "max_items": 20,
            "only_once": False,
            "user_agent": "",
        }

    def get_page(self) -> Optional[List[dict]]:
        result = self.get_data("last_result") or {}
        return [{
            "component": "VAlert",
            "props": {
                "type": "info",
                "variant": "tonal",
                "text": result.get("message", "尚无执行记录") if isinstance(result, dict) else "尚无执行记录",
            },
        }]
