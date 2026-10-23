"""NanShare 风格的 Telegram 115 转存/离线下载插件。"""

from __future__ import annotations

import re
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.message import Message
from app.schemas.types import EventType
from p115client import P115Client

try:
    from app.sdk.logging import logger as sdk_logger
except ImportError:
    sdk_logger = logger

U115_RE = re.compile(r"https?://(?:115\.com|115cdn\.com|115share\.com)/s/(?P<code>[A-Za-z0-9]+)(?:\?[^\s]*)?", re.I)
U115_PASSWORD_RE = re.compile(r"(?:password|提取码|访问码|密码)\s*[=:：]?\s*(?P<password>[A-Za-z0-9]{4})", re.I)
MAGNET_RE = re.compile(r"magnet:\?[^\s<>]+", re.I)
ED2K_RE = re.compile(r"ed2k://\|file\|[^\s<>]+?\|/", re.I)
TORRENT_RE = re.compile(r"https?://[^\s<>]+?\.torrent(?:\?[^\s<>]*)?", re.I)


def parse_input(text: str) -> Tuple[List[str], List[str], Optional[Tuple[str, str]]]:
    """提取离线链接和 115 分享码；结果保持输入顺序并去重。"""
    value = text or ""
    offline: List[str] = []
    for pattern in (MAGNET_RE, ED2K_RE, TORRENT_RE):
        for match in pattern.findall(value):
            if match not in offline:
                offline.append(match)
    share = U115_RE.search(value)
    if not share:
        return offline, [], None
    password_match = U115_PASSWORD_RE.search(value)
    password = password_match.group("password") if password_match else ""
    return offline, [share.group(0)], (share.group("code"), password)


def _event_userid(data: Dict[str, Any]) -> Optional[str]:
    value = data.get("userid") or data.get("user")
    return str(value) if value is not None else None


class NanShare115(_PluginBase):
    plugin_name = "NanShare 115转存"
    plugin_desc = "通过 MoviePilot Telegram Bot 接收 115分享、magnet、ed2k 或 torrent 链接并转存到指定115目录。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot/v3/docs/images/moviepilot.png"
    plugin_version = "1.0.0"
    plugin_author = "helios"
    author_url = "https://github.com/yaoyaole0112/mpplugins.v3"
    plugin_config_prefix = "nanshare115_"
    plugin_order = 24
    auth_level = 1

    _enabled = False
    _cookie = ""
    _target_path = "/NanShare"
    _commands = ("/nanshare", "/ns")
    _client: Optional[P115Client] = None
    _client_lock = Lock()

    def init_plugin(self, config: dict = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._cookie = str(config.get("cookie") or "").strip()
        self._target_path = str(config.get("target_path") or "/NanShare").strip() or "/NanShare"
        self._client = None
        if self._enabled and self._cookie:
            try:
                self._client = P115Client(self._cookie)
            except Exception as exc:
                sdk_logger.error(f"NanShare 115：初始化客户端失败：{type(exc).__name__}")

    def get_state(self) -> bool:
        return self._enabled and bool(self._cookie)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{"cmd": command, "event": EventType.PluginAction, "desc": "NanShare接收链接并转存到115", "category": "", "data": {"action": "nanshare115"}} for command in NanShare115._commands]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{"path": "/status", "endpoint": self.status, "methods": ["GET"], "summary": "查看 NanShare 115 状态"}]

    def status(self) -> Dict[str, Any]:
        return {"enabled": self._enabled, "configured": bool(self._cookie), "target_path": self._target_path}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [{"component": "VForm", "content": [
            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}},
            {"component": "VTextField", "props": {"model": "cookie", "label": "115 Cookie", "type": "password", "hint": "仅保存在 MoviePilot 插件配置中", "persistentHint": True}},
            {"component": "VTextField", "props": {"model": "target_path", "label": "115目标目录", "placeholder": "/NanShare", "hint": "目录不存在时自动创建；TG 消息支持 /nanshare 链接"}},
        ]}], {"enabled": False, "cookie": self._cookie, "target_path": self._target_path}

    def get_page(self) -> Optional[List[dict]]:
        return None

    def _reply(self, data: Dict[str, Any], text: str) -> None:
        try:
            channel = data.get("channel")
            source = data.get("source")
            if channel and source:
                self.chain.post_message(Message(channel=channel, source=source, title="NanShare 115", text=text, userid=_event_userid(data)))
                return
        except Exception:
            pass
        logger.info(f"NanShare 115 回复：{text}")

    def _path_id(self, client: P115Client) -> int:
        from p115client.tool.iterdir import iterdir

        parent_id = 0
        for name in (part for part in self._target_path.split("/") if part):
            matched = None
            for item in iterdir(client, cid=parent_id, ensure_file=False, app="web"):
                item_name = str(item.get("name") or item.get("file_name") or item.get("n") or "")
                if item_name == name and bool(item.get("is_dir", item.get("is_directory", True))):
                    matched = item
                    break
            if matched:
                parent_id = int(matched.get("id") or matched.get("cid") or matched.get("fid"))
                continue
            response = client.fs_mkdir(name, pid=parent_id)
            if not response.get("state"):
                raise RuntimeError(str(response.get("error") or response.get("message") or "创建115目录失败"))
            data = response.get("data") or {}
            parent_id = int(data.get("cid") or data.get("id") or response.get("cid") or 0)
            if not parent_id:
                raise RuntimeError("创建115目录后未返回目录ID")
        return parent_id

    def _receive_share(self, client: P115Client, code: str, password: str) -> str:
        from p115client.tool.iterdir import share_iterdir
        share_files = list(share_iterdir(client, receive_code=password, share_code=code, cid=0, app="web", order="user_ptime"))
        if not share_files:
            raise ValueError("分享链接为空或提取码错误")
        payload = {"share_code": code, "receive_code": password, "file_id": ",".join(str(item["id"]) for item in share_files), "cid": self._path_id(client), "is_check": 0}
        response = client.share_receive(payload)
        if not response.get("state"):
            raise RuntimeError(str(response.get("error") or "115 转存接口返回失败"))
        return f"115分享已转存到 {self._target_path}"

    def _offline(self, client: P115Client, links: List[str]) -> str:
        payload = {f"url[{index}]": link for index, link in enumerate(links)}
        payload["wp_path_id"] = self._path_id(client)
        response = client.clouddownload_task_add_urls(payload)
        if not response.get("state"):
            raise RuntimeError(str(response.get("error") or "115 离线接口返回失败"))
        result = (response.get("data") or {}).get("result")
        count = len(result) if isinstance(result, list) else len(links)
        return f"已提交 {count} 条离线任务，目标目录：{self._target_path}"

    def _handle(self, data: Dict[str, Any], text: str) -> None:
        offline, _, share = parse_input(text)
        if not offline and not share:
            self._reply(data, "未识别链接。支持：115分享链接、magnet、ed2k、HTTP(S) .torrent。")
            return
        client = self._client
        if not client:
            self._reply(data, "请先在插件设置中启用并填写115 Cookie。")
            return
        try:
            results = []
            with self._client_lock:
                if share:
                    results.append(self._receive_share(client, share[0], share[1]))
                if offline:
                    results.append(self._offline(client, offline))
            self._reply(data, "\n".join(results))
        except Exception as exc:
            sdk_logger.error(f"NanShare 115 处理失败：{type(exc).__name__}")
            self._reply(data, f"处理失败：{str(exc)[:200]}")

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event) -> None:
        data = event.event_data or {}
        if data.get("action") != "nanshare115" or not self._enabled:
            return
        self._handle(data, str(data.get("arg_str") or ""))

    @eventmanager.register(EventType.UserMessage)
    def handle_message(self, event: Event) -> None:
        if not self.get_state():
            return
        data = event.event_data or {}
        text = str(data.get("text") or "").strip()
        if text.startswith(self._commands):
            parts = text.split(maxsplit=1)
            self._handle(data, parts[1] if len(parts) == 2 else "")
            return
        if U115_RE.search(text) or MAGNET_RE.search(text) or ED2K_RE.search(text) or TORRENT_RE.search(text):
            self._handle(data, text)

    def stop_service(self) -> None:
        self._client = None
