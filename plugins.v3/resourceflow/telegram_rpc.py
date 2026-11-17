"""Private-chat RPC through the ED2K plugin's locked, authorized personal session."""

import asyncio
from importlib import import_module
from pathlib import Path
import time

from .shared_protocol import SharedError, decode, encode, parse_part


def ed2k_account(proxy=None):
    from app.runtime.extensions.plugin.manager import PluginManager
    ed2k = next((plugin for plugin in PluginManager().running_plugins.values()
                 if type(plugin).__name__ == "Ed2kFolderWatcher"), None)
    if not ed2k:
        raise SharedError("自动生成ED2K链接插件未运行，无法使用个人账号")
    module = import_module(f"{type(ed2k).__module__}.user_account")
    return module.TelegramUserAccount(Path(ed2k.get_data_path()), proxy=proxy)


class TelegramRPC:
    _last_send = {}

    def __init__(self, account, username, timeout=60):
        self.account, self.username, self.timeout = account, username, timeout

    def exchange(self, request_id, body):
        try:
            with self.account.exclusive(wait_seconds=5):
                credentials = self.account._credentials()
                return asyncio.run(asyncio.wait_for(
                    self._exchange(credentials, request_id, body), timeout=self.timeout))
        except SharedError:
            raise
        except Exception as error:
            if type(error).__name__ in {"FloodWaitError", "SlowModeWaitError", "FloodPremiumWaitError"}:
                wait = max(60, int(getattr(error, "seconds", 300))) + 1
                method = type(getattr(error, "request", None)).__name__
                method = method if method.endswith("Request") and method.isidentifier() else "审批请求"
                failure = SharedError(f"Telegram 要求限速等待，约 {wait} 秒后重试（{method}），本次暂缓下载")
                failure.retry_after = wait
                raise failure from None
            raise SharedError("Telegram 审批请求未确认，保留申请并暂缓下载") from None

    async def _exchange(self, credentials, request_id, body):
        client = None
        try:
            client = self.account._client(credentials)
            await client.connect()
            if not await client.is_user_authorized():
                raise SharedError("Telegram 个人账号未登录")
            me = await client.get_me()
            if not me or getattr(me, "bot", False):
                raise SharedError("共享审批必须使用个人账号")
            # Resolve cached input peers first; get_entity(username) always resolves the username.
            target = await client.get_input_entity(self.username)
            peer = await client.get_entity(target)
            if not getattr(peer, "bot", False) or str(getattr(peer, "username", "")).lower() != self.username.lstrip("@").lower():
                raise SharedError("协调目标不是指定的 Telegram Bot")
            first_id = None
            session = (str(self.account.session_path), peer.id)
            for text in encode(body, request_id):
                delay = self._last_send.get(session, 0) + 1.1 - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_send[session] = time.monotonic()
                message = await client.send_message(peer, text, parse_mode=None, link_preview=False)
                first_id = first_id or message.id
            parts, expected, cursor = {}, None, first_id
            while True:
                await asyncio.sleep(1.0)
                async for message in client.iter_messages(peer, min_id=cursor, reverse=True, limit=100):
                    cursor = max(cursor, message.id)
                    if getattr(message, "out", False) or getattr(message, "sender_id", None) != peer.id:
                        continue
                    try:
                        rid, index, total, part = parse_part(message.message or "", response=True)
                    except SharedError:
                        continue
                    if rid != request_id:
                        continue
                    if expected is not None and expected != total or index in parts and parts[index] != part:
                        raise SharedError("协调回复分片冲突")
                    expected, parts[index] = total, part
                    if len(parts) == expected:
                        response = decode([parts[number] for number in range(1, expected + 1)])
                        if response.get("ok") is not True:
                            failure = SharedError(str(response.get("error") or "协调端拒绝请求")[:200])
                            failure.definitive = True
                            raise failure
                        return response
        finally:
            if client is not None:
                await self.account._disconnect(client)
