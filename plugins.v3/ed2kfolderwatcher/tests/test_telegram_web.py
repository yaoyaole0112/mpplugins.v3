import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "ed2k_web_test_package"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package


def load_module(name):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


account_module = load_module("user_account")
web_module = load_module("telegram_web")


class TelegramWebTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.client = types.SimpleNamespace(
            connect=AsyncMock(), disconnect=AsyncMock(),
            is_user_authorized=AsyncMock(return_value=False),
            send_code_request=AsyncMock(return_value=types.SimpleNamespace(phone_code_hash="secret-code-hash")),
            sign_in=AsyncMock(), log_out=AsyncMock(return_value=True),
            get_me=AsyncMock(return_value=types.SimpleNamespace(id=123, bot=False)))
        self.account = account_module.TelegramUserAccount(Path(temporary.name), client_factory=Mock(return_value=self.client))
        self.credentials = {"api_id": 12345, "api_hash": "a" * 32}
        self.account.save_credentials(self.credentials)
        self.now = 1000
        self.manager = web_module.TelegramWebLogin(self.account, clock=lambda: self.now)
        self.addCleanup(self.manager.close)

    def call(self, operation, values=None):
        return asyncio.run(self.manager.call(operation, values))

    def start(self):
        return self.call("send_code", {"phone": "+8613800000000"})

    def assert_released(self):
        self.assertIsNone(self.manager.client)
        self.assertIsNone(self.manager.guard)
        with self.account.exclusive():
            pass

    def test_code_login_and_no_secrets_returned(self):
        result = self.start()
        self.assertTrue(result["pending"])
        with self.assertRaises(account_module.UserAccountError):
            with self.account.exclusive():
                pass
        result = self.call("sign_in", {"code": "12345"})
        self.assertTrue(result["logged_in"])
        self.assertEqual(self.account._credentials()["user_id"], 123)
        self.assertEqual(set(self.account._credentials()), {"api_id", "api_hash", "user_id"})
        for secret in ("a" * 32, "+8613800000000", "secret-code-hash", '"code"', '"password"'):
            self.assertNotIn(secret, json.dumps(result))
        self.assert_released()

    def test_two_factor_transition_and_retry(self):
        self.start()
        self.client.sign_in.side_effect = type("SessionPasswordNeededError", (Exception,), {})("secret")
        result = self.call("sign_in", {"code": "12345"})
        self.assertTrue(result["password_required"])
        self.assertTrue(result["pending"])
        self.client.sign_in.side_effect = type("PasswordHashInvalidError", (Exception,), {})("private password")
        with self.assertRaisesRegex(account_module.UserAccountError, "不正确"):
            self.call("sign_in", {"password": "private password"})
        self.assertTrue(self.manager.snapshot()["pending"])
        self.client.sign_in.side_effect = None
        self.assertTrue(self.call("sign_in", {"password": "private password"})["logged_in"])
        self.assert_released()

    def test_invalid_code_retains_pending_and_expired_releases(self):
        self.start()
        self.client.sign_in.side_effect = type("PhoneCodeInvalidError", (Exception,), {})("secret")
        with self.assertRaisesRegex(account_module.UserAccountError, "不正确"):
            self.call("sign_in", {"code": "12345"})
        self.assertTrue(self.manager.snapshot()["pending"])
        self.client.sign_in.side_effect = type("PhoneCodeExpiredError", (Exception,), {})("secret")
        with self.assertRaisesRegex(account_module.UserAccountError, "过期"):
            self.call("sign_in", {"code": "12345"})
        self.assert_released()

    def test_expiry_and_cancel_release(self):
        self.start()
        self.now += 601
        self.assertFalse(self.call("status")["pending"])
        self.assert_released()
        self.start()
        self.call("cancel")
        self.assert_released()

    def test_resend_cooldown_and_close_release(self):
        self.start()
        with self.assertRaisesRegex(account_module.UserAccountError, "等待"):
            self.start()
        self.client.send_code_request.assert_awaited_once()
        self.manager.close()
        self.assert_released()

    def test_save_blank_hash_and_reject_logged_in_changes(self):
        result = self.call("save", {"api_id": 12345, "api_hash": ""})
        self.assertTrue(result["api_hash_configured"])
        self.assertEqual(self.account._credentials()["api_hash"], "a" * 32)
        self.account.save_credentials({**self.credentials, "user_id": 123})
        with self.assertRaisesRegex(account_module.UserAccountError, "先退出"):
            self.call("save", {"api_id": 678, "api_hash": "b" * 32})

    def test_save_unchanged_during_login_preserves_flow(self):
        self.start()
        self.assertTrue(self.call("save", {"api_id": 12345})["pending"])

    def test_logout_revokes_and_retains_api_only(self):
        self.account.session_path.touch()
        self.account.save_credentials({**self.credentials, "user_id": 123})
        self.client.is_user_authorized.return_value = True
        result = self.call("logout")
        self.assertFalse(result["logged_in"])
        self.client.log_out.assert_awaited_once()
        self.assertFalse(self.account.session_path.exists())
        self.assertEqual(self.account._credentials(), self.credentials)
        self.assert_released()

    def test_flood_wait_and_error_sanitization(self):
        error = type("FloodWaitError", (Exception,), {})("secret phone password")
        error.seconds = 125
        self.client.send_code_request.side_effect = error
        with self.assertRaises(account_module.UserAccountError) as captured:
            self.start()
        self.assertEqual(captured.exception.retry_after, 125)
        self.assertNotIn("secret", str(captured.exception))
        self.assertEqual(self.manager.snapshot()["retry_in"], 125)
        self.assert_released()

    def test_network_error_cleans_lock_and_hides_details(self):
        self.client.connect.side_effect = RuntimeError("secret phone password")
        with self.assertRaises(account_module.UserAccountError) as captured:
            self.start()
        self.assertNotIn("secret", str(captured.exception))
        self.assert_released()

    def test_bot_session_refused(self):
        self.start()
        self.client.get_me.return_value.bot = True
        with self.assertRaisesRegex(account_module.UserAccountError, "个人账号"):
            self.call("sign_in", {"code": "12345"})
        self.assertNotIn("user_id", self.account._credentials())
        self.assert_released()
