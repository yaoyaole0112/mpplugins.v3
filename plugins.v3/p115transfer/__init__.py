"""通过 Telegram 接收115分享及离线下载链接。"""

from __future__ import annotations

import json
import re
from html import unescape
from threading import Event as StopEvent, Lock, Thread
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlsplit

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
ED2K_RE = re.compile(r"ed2k://\|file\|[^\r\n<>]+?\|/", re.I)
TORRENT_RE = re.compile(r"https?://[^\s<>]+?\.torrent(?:\?[^\s<>]*)?", re.I)
WEB_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
TELEGRAPH_HOSTS = {"telegra.ph", "www.telegra.ph", "graph.org", "www.graph.org"}
MAX_TELEGRAPH_PAGES = 3
MAX_TELEGRAPH_BYTES = 1024 * 1024


def telegraph_paths(text: str) -> List[str]:
    paths = []
    for match in WEB_URL_RE.finditer(unescape(text or "")):
        try:
            parsed = urlsplit(match.group().rstrip(".,;!，。；！）)]}"))
            if (parsed.hostname not in TELEGRAPH_HOSTS or parsed.username is not None
                    or parsed.password is not None or parsed.port not in (None, 80, 443)):
                continue
            path = unquote(parsed.path).strip("/")
            if not path or len(path) > 512 or "/" in path or path in {".", ".."}:
                continue
        except ValueError:
            continue
        if path not in paths:
            paths.append(path)
    return paths


def telegraph_text(content: List[Any]) -> str:
    parts = []
    size = 0
    nodes = 0

    def append(value):
        nonlocal size
        size += len(value)
        if size > MAX_TELEGRAPH_BYTES:
            raise P115OperationError("Telegraph页面内容过大，未提交任务；请单独发送资源链接")
        parts.append(value)

    def visit(node, depth):
        nonlocal nodes
        nodes += 1
        if depth > 64 or nodes > 20000:
            raise P115OperationError("Telegraph页面结构过于复杂，未提交任务；请单独发送资源链接")
        if isinstance(node, str):
            append(node)
        elif isinstance(node, dict):
            tag = node.get("tag")
            block = tag in {"p", "pre", "blockquote", "li", "h3", "h4", "br", "hr", "figure"}
            if block:
                append("\n")
            href = (node.get("attrs") or {}).get("href")
            if isinstance(href, str):
                append(href + " ")
            for child in node.get("children") or []:
                visit(child, depth + 1)
            if block:
                append("\n")

    for node in content:
        visit(node, 0)
    return "".join(parts)


def parse_share_links(text: str) -> List[Tuple[str, str]]:
    value = unescape(text or "")
    matches = list(U115_RE.finditer(value))
    shares = []
    for index, match in enumerate(matches):
        link = urlsplit(match.group())
        password = next(iter(parse_qs(link.query).get("password", [])), "")
        if not re.fullmatch(r"[A-Za-z0-9]{4}", password):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
            context = value[match.end():end]
            password_match = U115_PASSWORD_RE.search(context)
            if password_match is None and len(matches) == 1:
                password_match = U115_PASSWORD_RE.search(value)
            password = password_match.group("password") if password_match else ""
        share = (match.group("code"), password)
        if share not in shares:
            shares.append(share)
    return shares


def telegram_message_text(message: Dict[str, Any]) -> str:
    parts = [str(message.get("text") or message.get("caption") or "")]
    for field in ("entities", "caption_entities"):
        for entity in message.get(field) or []:
            if entity.get("type") == "text_link" and isinstance(entity.get("url"), str):
                parts.append(entity["url"])
    for row in (message.get("reply_markup") or {}).get("inline_keyboard") or []:
        for button in row:
            if isinstance(button.get("url"), str):
                parts.append(button["url"])
    return "\n".join(parts)


def parse_input(text: str) -> Tuple[List[str], List[str], Optional[Tuple[str, str]]]:
    """提取离线链接和 115 分享码；结果保持输入顺序并去重。"""
    value = unescape(text or "")
    value = re.sub(r"ed2k%3a%2f%2f[^\s<>]+", lambda match: unquote(match.group()), value, flags=re.I)
    offline: List[str] = []
    matches = sorted((match for pattern in (MAGNET_RE, ED2K_RE, TORRENT_RE) for match in pattern.finditer(value)), key=lambda match: match.start())
    for match in matches:
        link = match.group()
        if link not in offline:
            offline.append(link)
    share = U115_RE.search(value)
    if not share:
        return offline, [], None
    password_match = U115_PASSWORD_RE.search(value)
    password = password_match.group("password") if password_match else ""
    return offline, [share.group(0)], (share.group("code"), password)


def _event_userid(data: Dict[str, Any]) -> Optional[str]:
    value = data.get("userid") or data.get("user")
    return str(value) if value is not None else None


class BotConfigurationError(ValueError):
    pass


class P115OperationError(RuntimeError):
    pass


def is_duplicate_task(response):
    if not isinstance(response, dict):
        return False
    return any(str(response.get(key)) == "10008" for key in ("errcode", "errno", "errNo", "code"))


def response_error(stage, response):
    if not isinstance(response, dict):
        return P115OperationError(f"{stage}：115返回格式异常，请核实网盘状态")
    code = next((str(response[key]) for key in ("errcode", "errno", "errNo", "code")
                 if key in response and str(response[key]) != "0" and re.fullmatch(r"-?\d{1,12}", str(response[key]))), "未知")
    message = " ".join(str(response.get(key) or "") for key in ("error", "error_msg", "err_msg", "message", "msg"))
    reason = "115拒绝请求，请在115客户端核实账户及任务状态"
    for pattern, hint in (
        (r"验证码|验证|captcha|verify", "115要求安全验证，请在115客户端完成验证后再操作"),
        (r"登录|cookie|login|认证", "115登录状态异常，请在STRM助手中重新登录"),
        (r"重复|已存在|已添加|exist|duplicate", "任务已存在，请勿重复提交"),
        (r"配额|次数|quota|limit|上限", "离线次数或配额受限，请在115客户端核实"),
        (r"空间|容量|space", "网盘空间不足，请在115客户端核实"),
        (r"链接|格式|url|link", "115不接受此链接，请检查完整性并在115客户端核实"),
        (r"目录|文件夹|directory|folder", "目标目录不可用，请核实目录路径或CID"),
    ):
        if re.search(pattern, message, re.I):
            reason = hint
            break
    return P115OperationError(f"{stage}：{reason}（115错误码：{code}）")


def validate_offline_links(links):
    for index, link in enumerate(links, 1):
        if not link.lower().startswith("ed2k:"):
            continue
        fields = link.split("|")
        if (len(fields) < 6 or fields[1].lower() != "file" or not fields[2]
                or not fields[3].isdecimal() or int(fields[3]) <= 0
                or not re.fullmatch(r"[a-fA-F0-9]{32}", fields[4]) or fields[-1] != "/"):
            raise P115OperationError(f"第{index}条ed2k链接格式错误：需包含文件名、正整数大小、32位哈希和结尾 |/；未提交任务")


class P115Transfer(_PluginBase):
    plugin_name = "115转存/离线"
    plugin_desc = "通过Telegram Bot接收115分享及离线链接，支持Telegraph资源页面和消息URL按钮，保存到指定115目录。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot/v3/docs/images/moviepilot.png"
    plugin_version = "1.3.1"
    plugin_author = "helios"
    author_url = "https://github.com/yaoyaole0112/mpplugins.v3"
    plugin_config_prefix = "p115transfer_"
    plugin_order = 24
    auth_level = 1

    _enabled = False
    _cookie = ""
    _target_path = "/115离线"
    _commands = ("/p115", "/offline", "/ns")
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
        self._bot_error = ""
        self._bot_proxy_source = "未连接"

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
        self._target_path = str(config.get("target_path") or "/115离线").strip() or "/115离线"
        self._target_cid = str(config.get("target_cid") or "").strip()
        self._bot_token = str(config.get("bot_token") or "").strip()
        self._bot_proxy = str(config.get("bot_proxy") or "").strip()
        self._bot_error = ""
        self._allowed_users = {item for item in re.split(r"[,，\s]+", str(config.get("allowed_users") or "")) if item.isdecimal()}
        if self._enabled:
            try:
                self._refresh_cookie()
            except Exception as exc:
                sdk_logger.error(f"115转存/离线：初始化客户端失败：{type(exc).__name__}")
        if self._enabled and self._bot_token and self._allowed_users:
            self._stop = StopEvent()
            self._worker = Thread(target=self._poll_bot, args=(self._stop,), daemon=True, name="p115-transfer-bot")
            self._worker.start()

    def get_state(self) -> bool:
        return self._enabled and bool(self._cookie)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{"cmd": command, "event": EventType.PluginAction, "desc": "115转存/离线：接收分享及离线链接", "category": "", "data": {"action": "p115transfer"}} for command in P115Transfer._commands]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{"path": "/status", "endpoint": self.status, "methods": ["GET"], "summary": "查看115转存/离线状态"}]

    def status(self) -> Dict[str, Any]:
        return {"enabled": self._enabled, "configured": bool(self._cookie), "target_path": self._target_path,
                "target_cid": self._target_cid, "bot_running": bool(self._worker and self._worker.is_alive()),
                "bot_error": self._bot_error, "bot_proxy_source": self._bot_proxy_source}

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
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-6", "text": "自动读取115网盘STRM助手的Cookie，无需重复填写。独立Bot必须配置允许的用户ID，且不要与其他程序共用接收Bot。支持转发含Telegraph链接的消息及URL按钮；若转发后按钮丢失，请直接发送页面链接。"}},
            field("bot_token", "接收链接的 Bot", "填写你自己的 Bot Token，让该 Bot 独立接收链接；留空则继续使用 MoviePilot 已配置的 Bot。", "Bot Token（可选）", type="password"),
            field("allowed_users", "允许使用 Bot 的用户", "使用独立 Bot 时必填。只接受这些 Telegram 用户的私聊消息；填写数字用户ID，多个ID用逗号分隔，不是 @用户名。", "Telegram 用户ID", placeholder="123456789,987654321"),
            field("bot_proxy", "Bot 网络代理", "仅用于独立 Bot 访问 Telegram API。填写后优先使用此代理；留空自动使用 MoviePilot 的网络代理，MP未配置时使用运行环境网络。地址必须能从MP容器访问，127.0.0.1指容器自身。", "代理地址（可选）", placeholder="http://代理服务器地址:7890"),
            field("target_path", "网盘保存目录", "115分享转存和离线任务都保存到此目录。填写从网盘根目录开始的路径，目录不存在时自动创建；填写下方CID后以CID为准。", "115目录路径", placeholder="/115离线"),
            field("target_cid", "精确指定保存目录", "可填写已有115文件夹的数字CID，填写后优先于上方目录路径；0表示网盘根目录。不清楚CID时留空，使用目录路径即可。", "目录CID（可选）", placeholder="例如：123456789"),
        ]}], {"enabled": self._enabled, "target_path": self._target_path, "target_cid": self._target_cid,
               "bot_token": self._bot_token, "allowed_users": ",".join(sorted(self._allowed_users)), "bot_proxy": self._bot_proxy}

    def get_page(self) -> Optional[List[dict]]:
        return None

    def _reply(self, data: Dict[str, Any], text: str) -> None:
        if data.get("bot_client"):
            try:
                self._bot_call(data["bot_client"], "sendMessage", {"chat_id": data["chat_id"], "text": text})
            except Exception as exc:
                logger.warning("115转存/离线：任务处理后的Bot回复未送达（%s），不代表任务提交失败；请核实网盘状态，不自动重放", type(exc).__name__)
            return
        try:
            channel = data.get("channel")
            source = data.get("source")
            if channel and source:
                self.chain.post_message(Message(channel=channel, source=source, title=self.plugin_name, text=text, userid=_event_userid(data)))
                return
        except Exception:
            pass
        logger.info("115转存/离线：回复渠道不可用，未广播消息")

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
                raise response_error("创建目标目录", response)
            data = response.get("data") or {}
            parent_id = int(data.get("cid") or data.get("id") or response.get("cid") or 0)
            if not parent_id:
                raise P115OperationError("创建目标目录：115未返回目录ID，已停止提交")
        return parent_id

    def _receive_share(self, client: P115Client, code: str, password: str) -> str:
        from p115client.tool.iterdir import share_iterdir
        share_files = list(share_iterdir(client, receive_code=password, share_code=code, cid=0, app="web", order="user_ptime"))
        if not share_files:
            raise ValueError("分享链接为空或提取码错误")
        payload = {"share_code": code, "receive_code": password, "file_id": ",".join(str(item["id"]) for item in share_files), "cid": self._path_id(client), "is_check": 0}
        response = client.share_receive(payload)
        if not response.get("state"):
            raise response_error("分享转存", response)
        return f"115分享已转存到 {self._target_cid or self._target_path}"

    def _offline(self, client: P115Client, links: List[str]) -> str:
        validate_offline_links(links)
        try:
            destination = self._path_id(client)
        except P115OperationError:
            raise
        except Exception as exc:
            raise P115OperationError(f"读取目标目录失败：{type(exc).__name__}；未提交离线任务") from None
        payload = {"wp_path_id": destination}
        try:
            if len(links) == 1:
                payload["url"] = links[0]
                response = client.clouddownload_task_add_url(payload, type="web")
            else:
                payload.update({f"url[{index}]": link for index, link in enumerate(links)})
                response = client.clouddownload_task_add_urls(payload, type="web")
        except Exception as exc:
            raise P115OperationError(f"提交离线任务异常：{type(exc).__name__}；结果可能不确定，请核实115任务列表，不自动重试") from None
        if not isinstance(response, dict) or not response.get("state"):
            if is_duplicate_task(response):
                logger.info("115转存/离线：离线任务已存在，未重复添加；请在115任务列表查看，不自动移动已有任务")
                return "该离线任务已在115中存在，无需重复提交。请查看115任务列表；已有任务的保存目录不会被本次设置改变。"
            raise response_error("提交离线任务", response)
        data = response.get("data")
        if isinstance(data, dict) and "state" in data and not data["state"]:
            if is_duplicate_task(data):
                logger.info("115转存/离线：离线任务已存在，未重复添加")
                return "该离线任务已在115中存在，无需重复提交，请查看115任务列表。"
            raise response_error("提交离线任务", data)
        result = data.get("result") if isinstance(data, dict) else data if isinstance(data, list) else None
        if result is None:
            result = response.get("result")
        if isinstance(result, list):
            existing = [item for item in result if is_duplicate_task(item)]
            failures = [item for item in result if not is_duplicate_task(item) and (not isinstance(item, dict) or ("state" in item and not item["state"])
                        or any(str(item.get(key, 0)) not in {"0", "", "None"} for key in ("errcode", "errno")))]
            count = len(result) - len(failures) - len(existing)
            if failures:
                detail = str(response_error("失败项", failures[0]))
                sdk_logger.warning("115转存/离线：离线提交成功%d条，失败%d条；%s", count, len(failures), detail)
                return f"离线提交结果：新增{count}条，已存在{len(existing)}条，失败{len(failures)}条；{detail}。请核实115任务列表，不自动重试。"
            if existing:
                logger.info("115转存/离线：新增%d条离线任务，已存在%d条，未重放", count, len(existing))
                return f"离线提交结果：新增{count}条，已存在{len(existing)}条，无需重复提交；请查看115任务列表。"
            if not result:
                raise P115OperationError("提交离线任务：115返回空结果，未确认成功，请核实任务列表")
        else:
            count = len(links)
        return f"已提交 {count} 条离线任务，目标目录：{self._target_cid or self._target_path}（尚未确认下载完成）"

    def _fetch_telegraph(self, path: str) -> str:
        import httpx

        url = "https://api.telegra.ph/getPage/" + quote(path, safe="")
        for _, proxy in self._resolve_bot_proxies():
            try:
                with httpx.Client(timeout=httpx.Timeout(12, connect=8), proxy=proxy,
                                  trust_env=False, follow_redirects=False) as client:
                    with client.stream("GET", url, params={"return_content": "true"}) as response:
                        response.raise_for_status()
                        body = bytearray()
                        for chunk in response.iter_bytes(chunk_size=65536):
                            body.extend(chunk)
                            if len(body) > MAX_TELEGRAPH_BYTES:
                                raise P115OperationError("Telegraph页面内容过大，未提交任务；请单独发送资源链接")
                payload = json.loads(body)
                if not isinstance(payload, dict) or not payload.get("ok"):
                    raise P115OperationError("Telegraph页面不存在或不可读取，未提交任务；请检查页面链接")
                content = (payload.get("result") or {}).get("content")
                if not isinstance(content, list):
                    raise P115OperationError("Telegraph未返回页面正文，未提交任务；请直接发送资源链接")
                return telegraph_text(content)
            except P115OperationError:
                raise
            except Exception:
                continue
        raise P115OperationError("Telegraph页面读取失败，已尝试可用网络方式；未提交任务，请稍后重新发送")

    def _resolve_resources(self, text: str) -> Tuple[List[str], List[Tuple[str, str]]]:
        paths = telegraph_paths(text)
        if len(paths) > MAX_TELEGRAPH_PAGES:
            raise P115OperationError(f"每条消息最多解析{MAX_TELEGRAPH_PAGES}个Telegraph页面，未提交任务；请分开发送")
        sources = [text] + [self._fetch_telegraph(path) for path in paths]
        offline = []
        shares = []
        for source in sources:
            for link in parse_input(source)[0]:
                if link not in offline:
                    offline.append(link)
            for share in parse_share_links(source):
                if share not in shares:
                    shares.append(share)
        validate_offline_links(offline)
        return offline, shares

    def _handle(self, data: Dict[str, Any], text: str) -> None:
        try:
            offline, shares = self._resolve_resources(text)
        except P115OperationError as exc:
            logger.warning("115转存/离线：%s", str(exc))
            self._reply(data, str(exc))
            return
        if data.get("bot_client") and self._stop.is_set():
            return
        if not offline and not shares:
            self._reply(data, "未识别到可转存的资源链接。支持115分享、magnet、ed2k、HTTP(S) .torrent及Telegraph页面。转发消息若未保留“查看资源”按钮，请复制按钮对应的页面链接发送给我。")
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
                for code, password in shares:
                    results.append(self._receive_share(client, code, password))
                if offline:
                    results.append(self._offline(client, offline))
            self._reply(data, "\n".join(results))
        except Exception as exc:
            detail = str(exc) if isinstance(exc, P115OperationError) else f"{type(exc).__name__}；请核实目标目录及115任务状态"
            sdk_logger.error("115转存/离线：%s", detail)
            self._reply(data, f"处理失败：{detail}；插件不会自动重放提交。")

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event) -> None:
        data = event.event_data or {}
        if data.get("action") != "p115transfer" or not self._enabled or self._bot_token:
            return
        self._handle(data, str(data.get("arg_str") or ""))

    @eventmanager.register(EventType.UserMessage)
    def handle_message(self, event: Event) -> None:
        if not self._enabled or self._bot_token:
            return
        data = event.event_data or {}
        text = telegram_message_text(data).strip()
        if text.startswith("/"):
            return
        if U115_RE.search(text) or MAGNET_RE.search(text) or ED2K_RE.search(text) or TORRENT_RE.search(text) or telegraph_paths(text):
            self._handle(data, text)

    def stop_service(self) -> None:
        if hasattr(self, "_stop"):
            self._stop.set()
        if getattr(self, "_worker", None) and self._worker.is_alive():
            self._worker.join(timeout=55)
            if self._worker.is_alive():
                raise RuntimeError("旧Bot任务尚未停止，请稍后重试")
        self._worker = None
        self._client = None

    def _bot_call(self, client, method, payload):
        response = client.post(f"https://api.telegram.org/bot{self._bot_token}/{method}", json=payload)
        if response.status_code in {401, 404, 409}:
            raise BotConfigurationError("Bot认证失败或接收冲突")
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError("Telegram请求失败")
        return result.get("result")

    @staticmethod
    def _normalise_runtime_proxy(proxy):
        if isinstance(proxy, dict):
            proxy = (proxy.get("https") or proxy.get("https://") or proxy.get("http")
                     or proxy.get("http://") or proxy.get("all") or proxy.get("all://"))
        return (str(proxy).strip() or None) if proxy else None

    def _runtime_bot_proxy(self):
        from app.runtime.settings import get_runtime_setting

        return self._normalise_runtime_proxy(get_runtime_setting("PROXY", None))

    def _resolve_bot_proxy(self):
        if self._bot_proxy:
            self._bot_proxy_source = "插件配置"
            return self._bot_proxy
        proxy = self._runtime_bot_proxy()
        self._bot_proxy_source = "MoviePilot配置" if proxy else "运行环境网络"
        return proxy

    def _resolve_bot_proxies(self):
        """返回按优先级排列的代理候选，最后始终保留直连。"""
        candidates = []

        def add(source, proxy):
            proxy = self._normalise_runtime_proxy(proxy)
            if proxy is None and source != "运行环境网络":
                return
            key = proxy or "__direct__"
            if any(existing_key == key for _, _, existing_key in candidates):
                return
            candidates.append((source, proxy, key))

        if self._bot_proxy:
            add("插件配置", self._bot_proxy)
            try:
                add("MoviePilot配置", self._runtime_bot_proxy())
            except (ImportError, AttributeError):
                pass
        else:
            try:
                add("MoviePilot配置", self._runtime_bot_proxy())
            except (ImportError, AttributeError):
                try:
                    add("MoviePilot配置", self._resolve_bot_proxy())
                except Exception:
                    pass
        add("运行环境网络", None)
        return [(source, proxy) for source, proxy, _ in candidates]

    @staticmethod
    def _is_proxy_failover_error(exc, httpx):
        error_types = tuple(
            error_type
            for name in (
                "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteError",
                "ReadError", "RemoteProtocolError", "PoolTimeout",
            )
            if isinstance(error_type := getattr(httpx, name, None), type)
        )
        if error_types and isinstance(exc, error_types):
            return True
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return isinstance(status_code, int) and status_code >= 500

    @staticmethod
    def _bot_error_reason(exc, httpx):
        if isinstance(exc, tuple(error_type for name in ("ConnectError", "ConnectTimeout")
                           if isinstance(error_type := getattr(httpx, name, None), type))):
            return "连接失败，请检查容器到Telegram的DNS、代理及网络连通性"
        if isinstance(exc, tuple(error_type for name in ("ReadTimeout", "PoolTimeout")
                           if isinstance(error_type := getattr(httpx, name, None), type))):
            return "Telegram长轮询超时，已重建连接；请检查代理空闲连接超时"
        status_code = getattr(getattr(exc, "response", None), "status_code", "未知")
        if status_code != "未知":
            return f"Telegram返回HTTP {status_code}，已重建连接；请检查代理及Telegram连通性"
        return "请求未完成，请检查网络或代理"

    def _bot_message(self, client, message):
        sender = str((message.get("from") or {}).get("id") or "")
        chat = message.get("chat") or {}
        if sender not in self._allowed_users or chat.get("type") != "private":
            return
        text = telegram_message_text(message)
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
        client = None
        try:
            proxies = self._resolve_bot_proxies()
            proxy_index = 0
            proxy_source, proxy = proxies[proxy_index]
            self._bot_proxy_source = proxy_source
            logger.info("115转存/离线：Bot连接方式：%s", proxy_source)
            timeout = httpx.Timeout(connect=15, read=45, write=15, pool=15)
            limits = httpx.Limits(max_keepalive_connections=0, keepalive_expiry=0)
            webhook_checked = False
            failures = 0
            failed_paths = 0
            outage_reported = False
            while not stop.is_set():
                if client is None:
                    client = httpx.Client(
                        timeout=timeout,
                        limits=limits,
                        proxy=proxy,
                        trust_env=False,
                    )
                try:
                    if not webhook_checked:
                        info = self._bot_call(client, "getWebhookInfo", {})
                        if info.get("url"):
                            self._bot_error = "Bot已配置webhook，请使用独立Bot；不会自动删除webhook"
                            logger.warning("115转存/离线：%s", self._bot_error)
                            return
                        webhook_checked = True
                    updates = self._bot_call(
                        client,
                        "getUpdates",
                        {"offset": offset, "timeout": 25, "allowed_updates": ["message"]},
                    )
                    if self._bot_error:
                        logger.info("115转存/离线：Bot连接已恢复，当前连接方式：%s，继续监听", proxy_source)
                    failures = 0
                    failed_paths = 0
                    outage_reported = False
                    self._bot_error = ""
                    for update in updates:
                        if stop.is_set():
                            return
                        offset = int(update["update_id"]) + 1
                        self.save_data(offset_key, offset)
                        self._bot_message(client, update.get("message") or {})
                except BotConfigurationError:
                    self._bot_error = "Bot认证失败或接收冲突，请检查Token及其他接收程序"
                    logger.warning("115转存/离线：%s", self._bot_error)
                    return
                except Exception as exc:
                    network_error = self._is_proxy_failover_error(exc, httpx)
                    failed_source = proxy_source
                    if network_error:
                        failed_paths += 1
                        if client is not None:
                            client.close()
                            client = None
                        proxy_index = (proxy_index + 1) % len(proxies)
                        proxy_source, proxy = proxies[proxy_index]
                        if len(proxies) > 1:
                            webhook_checked = False
                    if network_error and failed_paths < len(proxies):
                        reason = self._bot_error_reason(exc, httpx)
                        self._bot_error = f"{type(exc).__name__}：{reason}；正在切换连接方式"
                        logger.info(
                            "115转存/离线：Bot连接暂时异常（%s，%s），自动切换到%s；不重放转存",
                            type(exc).__name__, failed_source, proxy_source,
                        )
                        self._bot_proxy_source = proxy_source
                        continue
                    failures += 1
                    delay = min(300, 30 * 2 ** min(failures - 1, 4))
                    reason = self._bot_error_reason(exc, httpx)
                    self._bot_error = f"{type(exc).__name__}：{reason}；{delay}秒后重试"
                    if not outage_reported:
                        scope = "本轮全部连接方式均失败" if network_error else "请求异常"
                        logger.warning("115转存/离线：%s；%s（%s）；不重放转存", scope, self._bot_error, failed_source)
                        outage_reported = True
                    else:
                        logger.info("115转存/离线：Bot仍在重连（第%d次，%d秒后重试）", failures, delay)
                    if client is not None:
                        client.close()
                        client = None
                    failed_paths = 0
                    self._bot_proxy_source = proxy_source
                    stop.wait(delay)
        except Exception as exc:
            self._bot_error = f"{type(exc).__name__}：Bot初始化失败，请检查代理配置并重新保存"
            logger.warning("115转存/离线：%s", self._bot_error)
        finally:
            if client is not None:
                client.close()
