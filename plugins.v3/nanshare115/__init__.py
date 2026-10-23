"""NanShare 风格的 Telegram 115 转存/离线下载插件。"""

from __future__ import annotations

import re
from threading import Event as StopEvent, Lock, Thread
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
    plugin_name = "115转存/离线"
    plugin_desc = "通过 MoviePilot Telegram Bot 接收 115分享、magnet、ed2k 或 torrent 链接并转存到指定115目录。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot/v3/docs/images/moviepilot.png"
    plugin_version = "1.1.1"
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

    def __init__(self):
        super().__init__()
        self._stop = StopEvent()
        self._worker = None
        self._bot_token = ""
        self._allowed_users = set()
        self._target_cid = ""
        self._bot_proxy = ""

    def _refresh_cookie(self):
        config = self.get_config("P115StrmHelper") or {}
        cookie = str(config.get("cookies") or "").strip()
        if cookie != self._cookie or self._client is None:
            self._cookie = cookie
            self._client = P115Client(cookie) if cookie else None

    def init_plugin(self, config: dict = None) -> None:
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._target_path = str(config.get("target_path") or "/NanShare").strip() or "/NanShare"
        self._target_cid = str(config.get("target_cid") or "").strip()
        self._bot_token = str(config.get("bot_token") or "").strip()
        self._bot_proxy = str(config.get("bot_proxy") or "").strip()
        self._allowed_users = {item for item in re.split(r"[,，\s]+", str(config.get("allowed_users") or "")) if item.isdecimal()}
        if self._enabled:
            try:
                self._refresh_cookie()
            except Exception as exc:
                sdk_logger.error(f"NanShare 115：初始化客户端失败：{type(exc).__name__}")
        if self._enabled and self._bot_token and self._allowed_users:
            self._stop = StopEvent()
            self._worker = Thread(target=self._poll_bot, args=(self._stop,), daemon=True, name="nanshare115-bot")
            self._worker.start()

    def get_state(self) -> bool:
        return self._enabled and bool(self._cookie)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{"cmd": command, "event": EventType.PluginAction, "desc": "115转存/离线：接收分享及离线链接", "category": "", "data": {"action": "nanshare115"}} for command in NanShare115._commands]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{"path": "/status", "endpoint": self.status, "methods": ["GET"], "summary": "查看115转存/离线状态"}]

    def status(self) -> Dict[str, Any]:
        return {"enabled": self._enabled, "configured": bool(self._cookie), "target_path": self._target_path,
                "target_cid": self._target_cid, "bot_running": bool(self._worker and self._worker.is_alive())}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        def field(model, title, description, label, **props):
            return {"component": "div", "props": {"class": "mb-6"}, "content": [
                {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mb-2"}, "text": title},
                {"component": "div", "props": {"class": "text-body-2 text-medium-emphasis mb-4", "style": "line-height: 1.7; overflow-wrap: anywhere;"}, "text": description},
                {"component": "VTextField", "props": {
                    "model": model, "label": label, "variant": "outlined",
                    "density": "comfortable", "hide-details": "auto", **props,
                }},
            ]}

        return [{"component": "VForm", "props": {"class": "pa-4"}, "content": [
            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用115转存/离线", "class": "mb-4"}},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-6", "text": "自动读取115网盘STRM助手的Cookie，无需重复填写。独立Bot必须配置允许的用户ID，且不要与其他程序共用接收Bot。"}},
            field("bot_token", "接收链接的 Bot", "填写你自己的 Bot Token，让该 Bot 独立接收链接；留空则继续使用 MoviePilot 已配置的 Bot。", "Bot Token（可选）", type="password"),
            field("allowed_users", "允许使用 Bot 的用户", "使用独立 Bot 时必填。只接受这些 Telegram 用户的私聊消息；填写数字用户ID，多个ID用逗号分隔，不是 @用户名。", "Telegram 用户ID", placeholder="123456789,987654321"),
            field("bot_proxy", "Bot 网络代理", "仅用于独立 Bot 访问 Telegram API。网络能够直连时留空；需要代理时填写完整的代理地址和端口。", "代理地址（可选）", placeholder="http://127.0.0.1:7890"),
            field("target_path", "网盘保存目录", "115分享转存和离线任务都保存到此目录。填写从网盘根目录开始的路径，目录不存在时自动创建；填写下方CID后以CID为准。", "115目录路径", placeholder="/NanShare"),
            field("target_cid", "精确指定保存目录", "可填写已有115文件夹的数字CID，填写后优先于上方目录路径；0表示网盘根目录。不清楚CID时留空，使用目录路径即可。", "目录CID（可选）", placeholder="例如：123456789"),
        ]}], {"enabled": self._enabled, "target_path": self._target_path, "target_cid": self._target_cid,
               "bot_token": self._bot_token, "allowed_users": ",".join(sorted(self._allowed_users)), "bot_proxy": self._bot_proxy}

    def get_page(self) -> Optional[List[dict]]:
        return None

    def _reply(self, data: Dict[str, Any], text: str) -> None:
        if data.get("bot_client"):
            self._bot_call(data["bot_client"], "sendMessage", {"chat_id": data["chat_id"], "text": text})
            return
        try:
            channel = data.get("channel")
            source = data.get("source")
            if channel and source:
                self.chain.post_message(Message(channel=channel, source=source, title=self.plugin_name, text=text, userid=_event_userid(data)))
                return
        except Exception:
            pass
        logger.info("NanShare 115：回复渠道不可用，未广播消息")

    def _path_id(self, client: P115Client) -> int:
        if self._target_cid:
            if not self._target_cid.isdecimal():
                raise ValueError("目标目录CID必须为非负整数")
            return int(self._target_cid)
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
        return f"115分享已转存到 {self._target_cid or self._target_path}"

    def _offline(self, client: P115Client, links: List[str]) -> str:
        payload = {f"url[{index}]": link for index, link in enumerate(links)}
        payload["wp_path_id"] = self._path_id(client)
        response = client.clouddownload_task_add_urls(payload)
        if not response.get("state"):
            raise RuntimeError(str(response.get("error") or "115 离线接口返回失败"))
        result = (response.get("data") or {}).get("result")
        count = len(result) if isinstance(result, list) else len(links)
        return f"已提交 {count} 条离线任务，目标目录：{self._target_cid or self._target_path}（尚未确认下载完成）"

    def _handle(self, data: Dict[str, Any], text: str) -> None:
        offline, _, share = parse_input(text)
        if not offline and not share:
            self._reply(data, "未识别链接。支持：115分享链接、magnet、ed2k、HTTP(S) .torrent。")
            return
        try:
            with self._client_lock:
                self._refresh_cookie()
                client = self._client
        except Exception as exc:
            self._reply(data, f"读取STRM助手登录状态失败：{type(exc).__name__}")
            return
        if not client:
            self._reply(data, "请先在115网盘STRM助手中登录或配置Cookie。")
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
            self._reply(data, f"处理失败：{type(exc).__name__}；请核实网盘状态后再试，插件不会自动重放提交。")

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event) -> None:
        data = event.event_data or {}
        if data.get("action") != "nanshare115" or not self._enabled or self._bot_token:
            return
        self._handle(data, str(data.get("arg_str") or ""))

    @eventmanager.register(EventType.UserMessage)
    def handle_message(self, event: Event) -> None:
        if not self._enabled or self._bot_token:
            return
        data = event.event_data or {}
        text = str(data.get("text") or "").strip()
        if text.startswith("/"):
            return
        if U115_RE.search(text) or MAGNET_RE.search(text) or ED2K_RE.search(text) or TORRENT_RE.search(text):
            self._handle(data, text)

    def stop_service(self) -> None:
        if hasattr(self, "_stop"):
            self._stop.set()
        if getattr(self, "_worker", None) and self._worker.is_alive():
            self._worker.join(timeout=35)
            if self._worker.is_alive():
                raise RuntimeError("旧Bot任务尚未停止，请稍后重试")
        self._worker = None
        self._client = None

    def _bot_call(self, client, method, payload):
        response = client.post(f"https://api.telegram.org/bot{self._bot_token}/{method}", json=payload)
        if response.status_code in {401, 404, 409}:
            raise ValueError("Bot认证失败或接收冲突")
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError("Telegram请求失败")
        return result.get("result")

    def _bot_message(self, client, message):
        sender = str((message.get("from") or {}).get("id") or "")
        chat = message.get("chat") or {}
        if sender not in self._allowed_users or chat.get("type") != "private":
            return
        text = str(message.get("text") or message.get("caption") or "")
        for entity in message.get("entities") or message.get("caption_entities") or []:
            if entity.get("type") == "text_link" and entity.get("url"):
                text += "\n" + entity["url"]
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            if parts[0].split("@")[0] not in self._commands:
                return
            text = parts[1] if len(parts) > 1 else ""
        if self._stop.is_set():
            return
        self._handle({"bot_client": client, "chat_id": chat["id"], "user": sender}, text)

    def _poll_bot(self, stop):
        import hashlib
        import httpx

        offset_key = "bot_offset_" + hashlib.sha256(self._bot_token.encode()).hexdigest()[:16]
        offset = int(self.get_data(offset_key) or 0)
        try:
            with httpx.Client(timeout=25, proxy=self._bot_proxy or None) as client:
                info = self._bot_call(client, "getWebhookInfo", {})
                if info.get("url"):
                    logger.warning("NanShare 115：Bot已配置webhook，停止接收；不会自动删除webhook")
                    return
                while not stop.is_set():
                    try:
                        updates = self._bot_call(client, "getUpdates", {"offset": offset, "timeout": 10, "allowed_updates": ["message"]})
                        for update in updates:
                            if stop.is_set():
                                return
                            offset = int(update["update_id"]) + 1
                            self.save_data(offset_key, offset)
                            self._bot_message(client, update.get("message") or {})
                    except ValueError:
                        logger.warning("NanShare 115：Bot认证或接收冲突，停止监听")
                        return
                    except Exception as exc:
                        logger.warning("NanShare 115：Bot请求异常：%s，30秒后继续接收，不重放转存", type(exc).__name__)
                        stop.wait(30)
        except Exception as exc:
            logger.warning("NanShare 115：Bot停止：%s", type(exc).__name__)
