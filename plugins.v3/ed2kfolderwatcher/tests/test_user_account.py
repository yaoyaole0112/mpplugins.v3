import asyncio
import importlib.util
import json
import stat
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock


spec = importlib.util.spec_from_file_location("ed2k_user_account_test", Path(__file__).resolve().parents[1] / "user_account.py")
account_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(account_module)


class TelegramUserAccountTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.client = types.SimpleNamespace(
            connect=AsyncMock(), disconnect=AsyncMock(),
            is_user_authorized=AsyncMock(return_value=True),
            get_me=AsyncMock(return_value=types.SimpleNamespace(id=1234, bot=False)),
            send_message=AsyncMock(), start=AsyncMock())
        self.factory = Mock(return_value=self.client)
        self.account = account_module.TelegramUserAccount(self.data, client_factory=self.factory)
        self.credentials = {"api_id": 123, "api_hash": "private-api-hash", "user_id": 1234}
        self.account.save_credentials(self.credentials)

    def test_send_only_link_and_disconnect(self):
        result = self.account.send("@receiver_bot", "ed2k://|file|中文.mkv|1|hash|/")
        self.assertTrue(result["authorized"])
        self.client.send_message.assert_awaited_once_with("@receiver_bot", "ed2k://|file|中文.mkv|1|hash|/", parse_mode=None, link_preview=False, reply_to=None)
        self.client.disconnect.assert_awaited_once()

    def test_numeric_destination_and_topic(self):
        self.account.send("-100123", "link", thread_id=78)
        self.client.send_message.assert_awaited_once_with(-100123, "link", parse_mode=None, link_preview=False, reply_to=78)

    def test_unauthorized_session_never_sends(self):
        self.client.is_user_authorized.return_value = False
        with self.assertRaisesRegex(account_module.UserAccountError, "未登录或已失效"):
            self.account.send("@receiver_bot", "link")
        self.client.send_message.assert_not_awaited()
        self.client.disconnect.assert_awaited_once()

    def test_bot_session_rejected(self):
        self.client.get_me.return_value.bot = True
        with self.assertRaisesRegex(account_module.UserAccountError, "不能使用机器人会话"):
            self.account.send("@receiver_bot", "link")
        self.client.send_message.assert_not_awaited()

    def test_credentials_and_session_permissions(self):
        self.account.send("@receiver_bot", "link")
        self.assertEqual(stat.S_IMODE(self.account.directory.stat().st_mode), 0o700)
        for path in self.account.directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.account.identity(), "1234")

    def test_absent_credentials_no_client_or_login_prompt(self):
        self.account.credentials_path.unlink()
        with self.assertRaisesRegex(account_module.UserAccountError, "终端"):
            self.account.send("@receiver_bot", "link")
        self.factory.assert_not_called()

    def test_same_session_concurrency_rejected(self):
        another = account_module.TelegramUserAccount(self.data, client_factory=self.factory)
        with self.account.exclusive():
            with self.assertRaisesRegex(account_module.UserAccountError, "不要并行"):
                another.check()
        self.factory.assert_not_called()

    def test_reads_new_channel_messages_with_shared_session_lock(self):
        self.client.get_entity = AsyncMock(return_value=types.SimpleNamespace(id=123))
        message = types.SimpleNamespace(
            id=12,
            date=datetime(2026, 11, 10, 8, 56, 36, tzinfo=timezone.utc),
            message="new release post",
        )

        def iter_messages(entity, min_id, reverse, limit):
            self.assertEqual(entity.id, 123)
            self.assertEqual((min_id, reverse, limit), (10, True, 200))

            async def rows():
                yield message

            return rows()

        self.client.iter_messages = Mock(side_effect=iter_messages)
        result = self.account.read_channel_messages(["@channel"], {"@channel": 10})
        self.assertEqual(result["cursors"], {"@channel": 12})
        self.assertEqual(result["messages"][0]["text"], "new release post")
        self.assertEqual(result["messages"][0]["channel"], "@channel")
        self.client.disconnect.assert_awaited_once()

    def test_channel_read_error_is_scoped_to_channel(self):
        self.client.get_entity = AsyncMock(side_effect=RuntimeError("private details"))
        result = self.account.read_channel_messages(["@bad"], {})
        self.assertEqual(result["errors"], [{"channel": "@bad", "error": "RuntimeError"}])
        self.assertNotIn("private details", str(result))
        self.client.disconnect.assert_awaited_once()

    def test_initial_read_starts_at_72_hour_boundary_and_uses_numeric_channel_id(self):
        self.client.get_entity = AsyncMock(return_value=types.SimpleNamespace(id=123))

        def iter_messages(entity, **kwargs):
            self.assertEqual(kwargs["min_id"], 0)
            self.assertTrue(kwargs["reverse"])
            age = datetime.now(timezone.utc) - kwargs["offset_date"]
            self.assertAlmostEqual(age.total_seconds(), 72 * 3600, delta=5)

            async def rows():
                if False:
                    yield None

            return rows()

        self.client.iter_messages = Mock(side_effect=iter_messages)
        result = self.account.read_channel_messages(["-1001234567890"], {})
        self.client.get_entity.assert_awaited_once_with(-1001234567890)
        self.assertEqual(result["processed_channels"], ["-1001234567890"])

    def test_partial_read_failure_preserves_fetched_messages_for_cursor_progress(self):
        self.client.get_entity = AsyncMock(return_value=types.SimpleNamespace(id=123))

        def iter_messages(entity, **kwargs):
            async def rows():
                yield types.SimpleNamespace(id=12, date=datetime.now(timezone.utc), message="release")
                raise TimeoutError("private details")
            return rows()

        self.client.iter_messages = Mock(side_effect=iter_messages)
        result = self.account.read_channel_messages(["@channel"], {"@channel": 10})
        self.assertEqual(result["messages"][0]["message_id"], 12)
        self.assertEqual(result["errors"], [{"channel": "@channel", "error": "TimeoutError"}])

    def test_private_exception_text_never_propagated(self):
        self.client.connect.side_effect = RuntimeError("secret-phone secret-password private-api-hash")
        with self.assertRaises(account_module.UserAccountError) as captured:
            self.account.send("@receiver_bot", "link")
        self.assertNotIn("secret", str(captured.exception))
        self.assertNotIn("private-api-hash", str(captured.exception))
        self.client.disconnect.assert_awaited_once()

    def test_flood_wait_preserved(self):
        error = type("FloodWaitError", (Exception,), {})("private details")
        error.seconds = 73
        self.client.send_message.side_effect = error
        with self.assertRaises(account_module.UserAccountError) as captured:
            self.account.send("@receiver_bot", "link")
        self.assertEqual(captured.exception.retry_after, 74)
        self.assertNotIn("private details", str(captured.exception))

    def test_login_saves_only_api_credentials_and_account_id(self):
        with self.account.exclusive():
            result = asyncio.run(self.account.login(self.credentials, phone=Mock(), code=Mock(), password=Mock()))
        self.assertEqual(result, 1234)
        saved = json.loads(self.account.credentials_path.read_text())
        self.assertEqual(set(saved), {"api_id", "api_hash", "user_id"})
        self.client.start.assert_awaited_once()
        self.client.disconnect.assert_awaited_once()

    def test_login_failure_does_not_save_new_credentials(self):
        self.account.credentials_path.unlink()
        self.client.start.side_effect = RuntimeError("bad code")
        with self.account.exclusive():
            with self.assertRaises(RuntimeError):
                asyncio.run(self.account.login(self.credentials, phone=Mock(), code=Mock(), password=Mock()))
        self.assertFalse(self.account.credentials_path.exists())
        self.client.disconnect.assert_awaited_once()

    def test_proxy_conversion(self):
        proxy = account_module.mtproto_proxy({"https": "socks5h://name:p%40ss@localhost:1080"})
        self.assertEqual(proxy["proxy_type"], "socks5")
        self.assertEqual(proxy["password"], "p@ss")
        self.assertEqual(proxy["port"], 1080)
        self.assertIsNone(account_module.mtproto_proxy(None))
        with self.assertRaises(account_module.UserAccountError):
            account_module.mtproto_proxy("https://localhost:8080")


if __name__ == "__main__":
    unittest.main()
