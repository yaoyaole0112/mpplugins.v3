import asyncio
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse


class UserAccountError(Exception):
    def __init__(self, message: str, retry_after: int = 300):
        super().__init__(message)
        self.retry_after = retry_after


def mtproto_proxy(proxy):
    if not proxy:
        return None
    address = proxy if isinstance(proxy, str) else proxy.get("https") or proxy.get("http") or proxy.get("all")
    if not address:
        return None
    parsed = urlparse(address)
    scheme = {"socks5h": "socks5", "socks4a": "socks4"}.get(parsed.scheme, parsed.scheme)
    if scheme not in {"http", "socks5", "socks4"} or not parsed.hostname or not parsed.port:
        raise UserAccountError("个人账号代理必须为带端口的 http、socks5 或 socks4 地址；不支持 HTTPS 代理隧道")
    return {"proxy_type": scheme, "addr": parsed.hostname, "port": parsed.port,
            "rdns": True, "username": unquote(parsed.username) if parsed.username else None,
            "password": unquote(parsed.password) if parsed.password else None}


class TelegramUserAccount:
    def __init__(self, data_path: Path, proxy=None, client_factory=None):
        self.directory = Path(data_path) / "telegram-user"
        self.credentials_path = self.directory / "credentials.json"
        self.session_path = self.directory / "account.session"
        self.proxy = proxy
        self.client_factory = client_factory

    def configured(self) -> bool:
        return self.credentials_path.is_file() and self.session_path.is_file()

    def identity(self) -> str:
        try:
            return str(self._credentials().get("user_id", "unverified"))
        except UserAccountError:
            return "unauthorized"

    def _credentials(self) -> dict:
        try:
            credentials = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            if int(credentials["api_id"]) <= 0 or not credentials["api_hash"]:
                raise ValueError()
            return credentials
        except (OSError, ValueError, KeyError, TypeError):
            raise UserAccountError("个人账号尚未授权或本地凭证无效；请在服务器终端运行 telegram_user_login.py 完成登录，勿把验证码发到聊天中") from None

    def _permissions(self) -> None:
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.directory.chmod(0o700)
        for path in self.directory.iterdir():
            if path.is_file():
                path.chmod(0o600)

    @contextmanager
    def exclusive(self):
        self._permissions()
        descriptor = os.open(self.directory / "account.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UserAccountError("个人账号正在登录或发送；请等待当前任务完成，不要并行操作同一 Session", 60) from None
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _client(self, credentials):
        factory = self.client_factory
        if factory is None:
            try:
                from telethon import TelegramClient
            except ImportError:
                raise UserAccountError("未安装 Telethon，请安装插件 requirements.txt 中的依赖") from None
            factory = TelegramClient
        descriptor = os.open(self.session_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.session_path.chmod(0o600)
        return factory(str(self.session_path), int(credentials["api_id"]), credentials["api_hash"],
                       proxy=mtproto_proxy(self.proxy), timeout=15, connection_retries=1,
                       request_retries=1, retry_delay=1, flood_sleep_threshold=0,
                       device_model="MoviePilot ED2K", app_version="1.4.0")

    @staticmethod
    async def _disconnect(client):
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _operation(self, credentials, destination=None, link=None, thread_id=None):
        client = None
        try:
            client = self._client(credentials)
            await client.connect()
            if not await client.is_user_authorized():
                raise UserAccountError("个人账号会话未登录或已失效，请重新运行本地登录工具")
            me = await client.get_me()
            if not me or getattr(me, "bot", False):
                raise UserAccountError("必须授权 Telegram 个人账号，不能使用机器人会话")
            if destination is not None:
                recipient = int(destination) if destination.lstrip("-").isdigit() else destination
                await client.send_message(recipient, link, parse_mode=None, link_preview=False, reply_to=thread_id)
            return {"user_id": me.id, "authorized": True}
        finally:
            await self._disconnect(client)

    def _execute(self, destination=None, link=None, thread_id=None):
        try:
            credentials = self._credentials()
            with self.exclusive():
                result = asyncio.run(asyncio.wait_for(self._operation(credentials, destination, link, thread_id), timeout=60))
                self._permissions()
                return result
        except UserAccountError:
            raise
        except Exception as error:
            name = type(error).__name__
            if name in {"FloodWaitError", "SlowModeWaitError", "FloodPremiumWaitError"}:
                seconds = max(60, int(getattr(error, "seconds", 300)))
                raise UserAccountError(f"Telegram 要求限速等待 {seconds} 秒；请勿重复尝试", seconds + 1) from None
            if name in {"AuthKeyUnregisteredError", "SessionRevokedError", "UnauthorizedError", "UserDeactivatedError"}:
                raise UserAccountError("个人账号会话已失效，请在终端重新登录") from None
            if name in {"UserIsBlockedError", "ChatWriteForbiddenError", "UserBannedInChannelError", "PeerIdInvalidError", "UsernameInvalidError", "UsernameNotOccupiedError"}:
                raise UserAccountError("接收目标不可用或个人账号无权发送；请核对 @用户名、账号状态及接收机器人的使用要求") from None
            raise UserAccountError(f"个人账号连接或发送失败（{name}）；请检查代理与网络，不会退回 Bot 身份发送", 60) from None

    def check(self):
        return self._execute()

    def send(self, destination: str, link: str, thread_id=None):
        return self._execute(destination, link, thread_id)

    def save_credentials(self, credentials):
        self._permissions()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, delete=False) as handle:
                temporary = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                json.dump(credentials, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.credentials_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def login(self, credentials, phone, code, password):
        client = None
        try:
            client = self._client(credentials)
            await client.start(phone=phone, code_callback=code, password=password)
            me = await client.get_me()
            if not me or getattr(me, "bot", False):
                raise UserAccountError("登录的必须是个人账号，不能使用 Bot Token")
            self.save_credentials({"api_id": int(credentials["api_id"]), "api_hash": credentials["api_hash"], "user_id": me.id})
            return me.id
        finally:
            await self._disconnect(client)
            self._permissions()
