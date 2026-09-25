"""Independent Telegram user-account monitor for MoviePilot subscriptions."""

import asyncio
import re
import threading
import time
from collections import deque
from datetime import datetime
from types import SimpleNamespace

from app.sdk.logging import logger

try:
    from telethon import TelegramClient, events
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession
except ImportError:
    TelegramClient = events = StringSession = SessionPasswordNeededError = None


def normalize_channel(value):
    value = str(value or "").strip()
    value = re.sub(r"^https?://(?:www\.)?t\.me/", "", value, flags=re.I).strip("/@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value):
        raise ValueError("请输入公开频道的 @用户名或 t.me/频道链接")
    return value.lower()


def matches_subscription(text, sub):
    """Conservative match: title or explicit TMDB ID, then reject conflicting metadata."""
    text = text or ""
    title = re.split(r"\n\s*\n", text.strip(), maxsplit=1)[0][:120]
    name = (sub.get("name") or "").strip()
    media_id = str(sub.get("media_id") or "") if str(sub.get("media_source") or "").lower().endswith(("tmdb", "themoviedb")) else ""
    named = bool(name and re.search(re.escape(name), title if len(name) <= 3 else text, re.I))
    if named and len(name) <= 3:
        named = bool(re.search(rf"《\s*{re.escape(name)}\s*》|(?<![\w\u3400-\u9fff]){re.escape(name)}(?![\w\u3400-\u9fff]).{{0,30}}(?:[Ss]\d+|第\s*\d+\s*[季集])", title, re.I))
    id_hit = bool(media_id and re.search(
        rf"(?:(?:tmdb|themoviedb)\s*(?:id)?\s*[:：#]?\s*|themoviedb\.org/(?:tv|movie)/){re.escape(media_id)}\b",
        text, re.I))
    if not (named or id_hit):
        return False
    declared_id = re.search(r"(?:tmdb|themoviedb)\s*[:：]?\s*(\d+)", text, re.I)
    if declared_id and media_id and declared_id.group(1) != media_id:
        return False
    year = str(sub.get("year") or "")
    declared_year = re.search(r"\b(?:19|20)\d{2}\b", title)
    if year and declared_year and declared_year.group() != year:
        return False
    kind = str(sub.get("type") or "").lower()
    if ("电影" in kind or "movie" in kind) and re.search(r"电视剧|剧集|\btv\b|\bseries\b", title, re.I):
        return False
    if ("电视剧" in kind or "剧集" in kind or kind == "tv") and re.search(r"电影|影片|\bmovie\b|\bfilm\b", title, re.I):
        return False
    season = sub.get("season")
    if season is not None and ("电视剧" in kind or "剧集" in kind or kind == "tv"):
        try:
            number = int(season)
        except (TypeError, ValueError):
            return False
        check = title if len(name) <= 3 else text
        if not re.search(rf"\b[Ss]0*{number}(?!\d)|第\s*{number}\s*季", check):
            return False
    return True


def matches_keyword(text, keywords, blacklist):
    try:
        if any(re.search(pattern, text or "", re.I) for pattern in blacklist):
            return False
        return any(re.search(pattern, text or "", re.I) for pattern in keywords)
    except re.error:
        return False


class SubscriptionMonitor:
    """Telethon client belongs to one dedicated event loop; no cross-loop client access."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.loop = None
        self.thread = None
        self.client = None
        self.code_hash = None
        self.phone = None
        self.channel_ids = {"sub": set(), "kw": set()}
        self.hits = deque(maxlen=40)
        self._seen = set()
        self._subscriptions = []
        self._last_refresh = 0
        self._watchdog = None
        self.last_error = ""
        self.last_event = ""
        self.last_poll = ""

    def _ensure_loop(self):
        if self.thread and self.thread.is_alive():
            return
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            ready.set()
            self.loop.run_forever()
            self.loop.close()

        self.thread = threading.Thread(target=run, name="EmeToolsTelegram", daemon=True)
        self.thread.start()
        ready.wait(5)

    async def call(self, coro):
        self._ensure_loop()
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self.loop))

    async def _connect(self):
        if TelegramClient is None:
            raise ValueError("未安装 telethon，请重新安装插件依赖并重启 MoviePilot")
        if not self.plugin._tg_api_id or not self.plugin._tg_api_hash:
            raise ValueError("请先在设置中保存 Telegram API ID 和 API Hash")
        if self.client is None:
            self.client = TelegramClient(StringSession(self.plugin._tg_session), int(self.plugin._tg_api_id), self.plugin._tg_api_hash)
            self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def _authorized(self):
        client = await self._connect()
        if not await client.is_user_authorized():
            raise ValueError("请先登录 Telegram 账号")
        return client

    async def send_code(self, phone):
        if not re.fullmatch(r"\+\d{7,15}", phone or ""):
            raise ValueError("手机号需为国际格式，例如 +8613800000000")
        client = await self._connect()
        result = await client.send_code_request(phone)
        self.phone, self.code_hash = phone, result.phone_code_hash
        return {"ok": True, "message": "验证码已发送，请在 Telegram 中查看"}

    async def sign_in(self, code="", password=""):
        if not self.phone or not self.code_hash:
            raise ValueError("请先发送验证码")
        client = await self._connect()
        try:
            if password:
                await client.sign_in(password=password)
            else:
                if not re.fullmatch(r"\d{4,8}", code):
                    raise ValueError("请输入 Telegram 验证码")
                await client.sign_in(phone=self.phone, code=code, phone_code_hash=self.code_hash)
        except SessionPasswordNeededError:
            return {"ok": False, "password_required": True, "message": "请输入二步验证密码"}
        self.plugin._tg_session = client.session.save()
        self.plugin._persist()
        self.code_hash = None
        return {"ok": True, "message": "Telegram 登录成功"}

    async def logout(self):
        for scope in ("sub", "kw"):
            self.plugin._monitor_config[scope]["enabled"] = False
            self.channel_ids[scope].clear()
        if self.client:
            await self.client.log_out()
            self.client = None
        self.plugin._tg_session = ""
        self.plugin._persist()
        return {"ok": True}

    async def start(self, scope):
        client = await self._authorized()
        config = self.plugin._monitor_config[scope]
        if not config["channels"]:
            raise ValueError("请先配置监控频道")
        if scope == "kw" and not config["keywords"]:
            raise ValueError("请先配置关键词")
        if not self.plugin._tg_forward_token:
            raise ValueError("请先在设置中配置转发 Bot Token")
        ids = set()
        for channel in config["channels"]:
            entity = await client.get_entity(channel)
            ids.add(int(entity.id))
        self.channel_ids[scope] = ids
        self.plugin._monitor_config[scope]["enabled"] = True
        self.plugin._persist()
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._poll_channels())
        return {"ok": True}

    async def stop(self, scope):
        self.channel_ids[scope].clear()
        self.plugin._monitor_config[scope]["enabled"] = False
        self.plugin._persist()
        return {"ok": True}

    async def resume(self):
        if not self.plugin._enabled or not self.plugin._tg_session:
            return
        for scope in ("sub", "kw"):
            if self.plugin._monitor_config[scope]["enabled"]:
                try:
                    await self.start(scope)
                except Exception as exc:
                    self.last_error = f"{scope} 监控恢复失败：{type(exc).__name__}"
                    logger.warning("订阅清理转存 %s 监控恢复失败: %s", scope, type(exc).__name__)
        if any(self.plugin._monitor_config[scope]["enabled"] for scope in ("sub", "kw")):
            if self._watchdog is None or self._watchdog.done():
                self._watchdog = asyncio.create_task(self._poll_channels())

    async def shutdown(self):
        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None
        if self.client:
            await self.client.disconnect()
            self.client = None
        self.channel_ids = {"sub": set(), "kw": set()}

    async def status(self):
        logged = bool(self.client and self.client.is_connected())
        if not logged and self.plugin._tg_session and self.plugin._tg_api_id:
            try:
                await asyncio.wait_for(self._authorized(), timeout=8)
                logged = True
            except Exception as exc:
                self.last_error = f"Telegram 状态检查失败：{type(exc).__name__}"
        return {"configured": bool(self.plugin._tg_api_id and self.plugin._tg_api_hash), "logged_in": logged,
                "dependency_ready": TelegramClient is not None, "hits": list(self.hits),
                "last_error": self.last_error, "last_event": self.last_event,
                "last_poll": self.last_poll,
                "listening_channels": {scope: len(self.channel_ids[scope]) for scope in ("sub", "kw")},
                "subscription_count": len(self._subscriptions),
                "sub": dict(self.plugin._monitor_config["sub"]), "kw": dict(self.plugin._monitor_config["kw"])}

    async def _poll_channels(self):
        """Backfill recent channel posts, including messages missed during a disconnect."""
        while any(self.plugin._monitor_config[scope]["enabled"] for scope in ("sub", "kw")):
            try:
                if self.plugin._monitor_config["sub"]["enabled"] and time.monotonic() - self._last_refresh > 300:
                    await self._refresh_subscriptions()
                for scope in ("sub", "kw"):
                    if self.plugin._monitor_config[scope]["enabled"] and not self.channel_ids[scope]:
                        await self.start(scope)
                client = await asyncio.wait_for(self._authorized(), timeout=12)
                for peer_id in self.channel_ids["sub"] | self.channel_ids["kw"]:
                    messages = await asyncio.wait_for(client.get_messages(peer_id, limit=30), timeout=12)
                    for message in reversed(messages):
                        if not message.raw_text or not message.date:
                            continue
                        # Never bulk-forward historical messages on first start.
                        if time.time() - message.date.timestamp() > 900:
                            continue
                        await self._on_message(SimpleNamespace(chat_id=message.chat_id or -int(f"100{peer_id}"),
                                                               id=message.id, raw_text=message.raw_text, message=message))
                self.last_poll = datetime.now().strftime("%m-%d %H:%M:%S")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"频道补漏失败：{type(exc).__name__}"
                logger.warning("订阅清理转存频道补漏失败: %s", type(exc).__name__)
            await asyncio.sleep(60)

    async def _refresh_subscriptions(self):
        try:
            self._subscriptions = await asyncio.wait_for(
                asyncio.to_thread(self.plugin._subscription_items), timeout=12)
            self._last_refresh = time.monotonic()
        except Exception as exc:
            self.last_error = f"读取 MoviePilot 订阅失败：{type(exc).__name__}"
            logger.warning("订阅清理转存读取订阅失败: %s", type(exc).__name__)

    async def _on_message(self, event):
        peer_id = int(str(event.chat_id)[4:]) if str(event.chat_id).startswith("-100") else abs(int(event.chat_id or 0))
        scopes = [scope for scope in ("sub", "kw") if self.plugin._monitor_config[scope]["enabled"] and peer_id in self.channel_ids[scope]]
        if not scopes or not event.raw_text:
            return
        self.last_event = datetime.now().strftime("%m-%d %H:%M:%S")
        key = (event.chat_id, event.id)
        if key in self._seen:
            return
        text = event.raw_text
        names = []
        if "sub" in scopes:
            if time.monotonic() - self._last_refresh > 300:
                await self._refresh_subscriptions()
                if not self._last_refresh:
                    return
            names.extend(sub["name"] for sub in self._subscriptions if matches_subscription(text, sub))
        if "kw" in scopes and matches_keyword(text, self.plugin._monitor_config["kw"]["keywords"], self.plugin._monitor_config["kw"]["blacklist"]):
            names.append("关键词匹配")
        if not names:
            self._seen.add(key)
            return
        try:
            import httpx
            token = self.plugin._tg_forward_token
            async with httpx.AsyncClient(timeout=15) as http:
                result = (await http.get(f"https://api.telegram.org/bot{token}/getMe")).json()
            if not result.get("ok") or not result.get("result", {}).get("username"):
                raise ValueError("转发 Bot Token 无效")
            bot = await self.client.get_entity(result["result"]["username"])
            await self.client.forward_messages(bot, event.message)
            self._seen.add(key)
            self.hits.appendleft({"time": datetime.now().strftime("%m-%d %H:%M:%S"), "channel": str(event.chat_id), "matches": names})
            self.last_error = ""
        except Exception as exc:
            self.last_error = f"转发失败：{type(exc).__name__}"
            logger.warning("订阅清理转存 Telegram 转发失败: %s", type(exc).__name__)
        if len(self._seen) > 2000:
            self._seen.clear()
