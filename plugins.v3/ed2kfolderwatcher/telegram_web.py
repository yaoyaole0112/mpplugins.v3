import asyncio
import re
import threading
import time

from .user_account import TelegramUserAccount, UserAccountError


class TelegramWebLogin:
    def __init__(self, account: TelegramUserAccount, clock=time.monotonic):
        self.account = account
        self.clock = clock
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="ed2k-telegram-login", daemon=True)
        self.client = None
        self.guard = None
        self.phone = ""
        self.code_hash = ""
        self.password_required = False
        self.expires_at = 0
        self.next_code_at = 0
        self.expiry = None
        self.command_lock = None
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.run_until_complete(self._release())
        self.loop.close()

    async def call(self, operation: str, values=None):
        if self.loop.is_closed() or not self.thread.is_alive():
            raise UserAccountError("插件配置已更新，请刷新页面后重试")
        future = asyncio.run_coroutine_threadsafe(self._dispatch(operation, values or {}), self.loop)
        return await asyncio.wrap_future(future)

    def snapshot(self):
        try:
            credentials = self.account._credentials()
        except UserAccountError:
            credentials = {}
        return {"api_id": credentials.get("api_id", ""), "api_hash_configured": bool(credentials.get("api_hash")),
                "session_present": bool(credentials.get("user_id") and self.account.session_path.exists()),
                "pending": bool(self.client and self.phone), "password_required": self.password_required,
                "retry_in": max(0, int(self.next_code_at - self.clock())),
                "expires_in": max(0, int(self.expires_at - self.clock())) if self.client else 0}

    async def _dispatch(self, operation, values):
        if self.command_lock is None:
            self.command_lock = asyncio.Lock()
        async with self.command_lock:
            try:
                if self.client and self.clock() >= self.expires_at:
                    await self._release()
                handler = {"status": self._status, "save": self._save, "send_code": self._send_code,
                           "sign_in": self._sign_in, "logout": self._logout, "cancel": self._cancel}.get(operation)
                if handler is None:
                    raise UserAccountError("未知账号操作")
                result = await asyncio.wait_for(handler(values), timeout=40)
                return {**self.snapshot(), "ok": True, **result}
            except UserAccountError:
                if not self.phone:
                    await self._release()
                raise
            except Exception as error:
                name = type(error).__name__
                if name == "SessionPasswordNeededError":
                    self.password_required = True
                    return {**self.snapshot(), "ok": True, "logged_in": False, "message": "验证码已通过，请输入两步验证密码"}
                if name in {"PhoneCodeInvalidError", "PasswordHashInvalidError"}:
                    raise UserAccountError("验证码或两步验证密码不正确，请重新输入") from None
                if name == "PhoneCodeExpiredError":
                    await self._release()
                    raise UserAccountError("验证码已过期，请重新发送验证码") from None
                seconds = max(60, int(getattr(error, "seconds", 60)))
                if name in {"FloodWaitError", "PhoneNumberFloodError", "FloodPremiumWaitError"}:
                    self.next_code_at = max(self.next_code_at, self.clock() + seconds)
                    await self._release()
                    raise UserAccountError(f"Telegram 要求等待 {seconds} 秒后重试，请勿重复请求验证码", seconds) from None
                await self._release()
                raise UserAccountError(f"Telegram 授权操作失败（{name}），请检查网络或稍后重试；敏感详情不输出", 60) from None

    async def _release(self):
        if self.expiry is not None:
            self.expiry.cancel()
            self.expiry = None
        client, self.client = self.client, None
        self.phone = self.code_hash = ""
        self.password_required = False
        self.expires_at = 0
        try:
            if client is not None:
                await asyncio.wait_for(self.account._disconnect(client), timeout=5)
        finally:
            if self.guard is not None:
                guard, self.guard = self.guard, None
                guard.__exit__(None, None, None)

    async def _expire(self):
        if self.command_lock is None:
            return
        async with self.command_lock:
            if self.client and self.clock() >= self.expires_at:
                await self._release()

    async def _status(self, values):
        if self.client:
            return {"logged_in": False, "message": "等待两步验证密码" if self.password_required else "等待输入验证码"}
        if not self.snapshot()["session_present"]:
            return {"logged_in": False, "message": "请保存 API 设置并发送验证码登录"}
        try:
            result = await asyncio.to_thread(self.account.check)
            return {"logged_in": result["authorized"], "message": "Telegram 个人账号已登录"}
        except UserAccountError as error:
            return {"logged_in": False, "message": str(error)}

    async def _save(self, values):
        api_id = str(values.get("api_id", "")).strip()
        api_hash = str(values.get("api_hash", "")).strip()
        if not api_id.isdigit() or int(api_id) <= 0 or len(api_id) > 12:
            raise UserAccountError("API ID 必须为正整数")
        try:
            previous = self.account._credentials()
        except UserAccountError:
            previous = {}
        api_hash = api_hash or previous.get("api_hash", "")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            raise UserAccountError("API Hash 必须为 32 位十六进制字符串；已保存时可留空保持不变")
        changed = int(api_id) != previous.get("api_id") or api_hash != previous.get("api_hash")
        if changed and previous.get("user_id"):
            raise UserAccountError("已有登录账号，请先退出登录再更改 API ID/API Hash")
        if changed:
            await self._release()
        elif self.client is not None:
            return {"message": "API 设置未变，当前验证码登录流程保留"}
        credentials = {**previous, "api_id": int(api_id), "api_hash": api_hash}
        with self.account.exclusive():
            self.account.save_credentials(credentials)
        return {"message": "API 设置已保存在本地；API Hash 不会回显"}

    async def _open(self):
        credentials = self.account._credentials()
        guard = self.account.exclusive()
        guard.__enter__()
        self.guard = guard
        self.client = self.account._client(credentials)
        self.expires_at = self.clock() + 600
        self.expiry = self.loop.call_later(600, lambda: self.loop.create_task(self._expire()))
        await self.client.connect()
        return self.client

    async def _send_code(self, values):
        phone = str(values.get("phone", "")).strip()
        if not re.fullmatch(r"\+\d{7,15}", phone):
            raise UserAccountError("请输入国际格式手机号，例如 +8613800000000")
        remaining = int(self.next_code_at - self.clock())
        if remaining > 0:
            raise UserAccountError(f"请等待 {remaining + 1} 秒再请求验证码", remaining + 1)
        await self._release()
        self.next_code_at = self.clock() + 60
        client = await self._open()
        if await client.is_user_authorized():
            return await self._complete()
        result = await client.send_code_request(phone)
        code_hash = getattr(result, "phone_code_hash", None)
        if not code_hash:
            await self._release()
            raise UserAccountError("Telegram 未返回验证码流程，请检查账号状态后稍后重试")
        self.phone, self.code_hash = phone, code_hash
        return {"logged_in": False, "message": "验证码已请求，请查看 Telegram 官方服务消息；本次登录流程最多保留 10 分钟"}

    async def _sign_in(self, values):
        if self.client is None or not self.phone or not self.code_hash:
            raise UserAccountError("登录流程不存在或已过期，请先发送验证码")
        if self.password_required:
            password = str(values.get("password", ""))
            if not password:
                raise UserAccountError("请输入两步验证密码")
            await self.client.sign_in(password=password)
        else:
            code = str(values.get("code", "")).replace(" ", "")
            if not re.fullmatch(r"\d{4,8}", code):
                raise UserAccountError("请输入 Telegram 登录验证码")
            await self.client.sign_in(phone=self.phone, code=code, phone_code_hash=self.code_hash)
        return await self._complete()

    async def _complete(self):
        me = await self.client.get_me()
        if not me or getattr(me, "bot", False):
            await self._release()
            raise UserAccountError("必须使用 Telegram 个人账号，不能授权机器人会话")
        credentials = self.account._credentials()
        credentials["user_id"] = me.id
        self.account.save_credentials(credentials)
        await self._release()
        return {"logged_in": True, "message": "Telegram 登录成功，现在可补发已生成的链接"}

    async def _cancel(self, values):
        await self._release()
        return {"logged_in": False, "message": "已取消本次验证码登录流程"}

    async def _logout(self, values):
        await self._release()
        client = await self._open()
        if await client.is_user_authorized():
            if not await client.log_out():
                raise UserAccountError("Telegram 未确认退出，会话暂未清除，请稍后再试")
        await self._release()
        credentials = self.account._credentials()
        credentials.pop("user_id", None)
        with self.account.exclusive():
            for suffix in ("", "-journal", "-wal", "-shm"):
                self.account.session_path.with_name(self.account.session_path.name + suffix).unlink(missing_ok=True)
            self.account.save_credentials(credentials)
        return {"logged_in": False, "message": "已退出本插件的 Telegram 会话，API 设置保留"}

    def close(self):
        if self.loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
        try:
            future.result(timeout=10)
        except Exception:
            future.cancel()
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=10)

    async def _shutdown(self):
        if self.command_lock is None:
            await self._release()
        else:
            async with self.command_lock:
                await self._release()
