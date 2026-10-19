import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "ed2k_api_test_package"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
web_api = None


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global web_api
        spec = importlib.util.spec_from_file_location(f"{PACKAGE}.web_api", ROOT / "web_api.py")
        web_api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(web_api)

    def setUp(self):
        self.plugin = Mock()
        for name in web_api.CONFIG_FIELDS:
            setattr(self.plugin, f"_{name}", False if name in web_api.BOOLEAN_FIELDS else "")
        self.plugin._telegram_mode = "user"
        self.plugin._min_age = 60
        self.plugin._cron = "*/10 * * * *"
        self.plugin._telegram_token = "secret-bot-token"
        self.plugin.telegram_account_action = AsyncMock(return_value={"ok": True, "logged_in": True})
        self.plugin.telegram_account_status = AsyncMock(return_value={"ok": True, "logged_in": False})
        self.app = FastAPI()
        self.routes = web_api.build_routes(self.plugin)
        for route in self.routes:
            self.app.add_api_route(route["path"], route["endpoint"], methods=route["methods"])
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def authorize(self):
        self.app.dependency_overrides[web_api.get_current_active_superuser] = lambda: types.SimpleNamespace(is_superuser=True)

    def test_multi_target_settings_validation(self):
        result = web_api.validate_settings(self.plugin, {"telegram_enabled": True, "telegram_chat_id": "@first\n@second|123"})
        self.assertEqual(result["telegram_chat_id"], "@first\n@second|123")
        for value in ("", "@valid\ninvalid target", "@group|0"):
            with self.subTest(value=value), self.assertRaises(web_api.UserAccountError):
                web_api.validate_settings(self.plugin, {"telegram_enabled": True, "telegram_chat_id": value})

    def test_manual_delete_requires_confirmation_and_record_id(self):
        self.authorize()
        for payload in ({"id": "a" * 64}, {"id": "a" * 64, "confirm": False}, {"id": "../file", "confirm": True}, {"id": "a" * 64, "confirm": True, "path": "/etc/passwd"}):
            self.assertEqual(self.client.post("/records/delete-source", json=payload).status_code, 400)
        self.plugin.delete_source.assert_not_called()
        self.plugin.delete_source.return_value = {"ok": True, "message": "deleted"}
        result = self.client.post("/records/delete-source", json={"id": "a" * 64, "confirm": True})
        self.assertTrue(result.json()["ok"])
        self.plugin.delete_source.assert_called_once_with("a" * 64)

    def test_manual_delete_requires_authentication(self):
        from app.api.dependencies.auth import get_current_active_user
        def unauthenticated():
            raise HTTPException(status_code=401, detail="Not authenticated")
        self.app.dependency_overrides[get_current_active_user] = unauthenticated
        result = self.client.post("/records/delete-source", json={"id": "a" * 64, "confirm": True})
        self.assertIn(result.status_code, (401, 403))
        self.plugin.delete_source.assert_not_called()

    def test_every_route_declares_bearer_admin_auth(self):
        for route in self.routes:
            self.assertEqual(route["auth"], "bear")
            dependency = inspect.signature(route["endpoint"]).parameters["principal"].default
            self.assertIs(dependency.dependency, web_api.get_current_active_superuser)

    def test_non_admin_forbidden(self):
        from app.api.dependencies.auth import get_current_active_user
        self.app.dependency_overrides[get_current_active_user] = lambda: types.SimpleNamespace(is_superuser=False)
        for route in self.routes:
            response = self.client.request(route["methods"][0], route["path"])
            self.assertEqual(response.status_code, 403, response.text)

    def test_settings_hide_token_and_no_cache(self):
        self.authorize()
        response = self.client.get("/settings")
        self.assertNotIn("telegram_token_configured", response.json()["settings"])
        self.assertNotIn("telegram_token", response.json()["settings"])
        self.assertEqual(response.json()["settings"]["telegram_mode"], "user")
        self.assertNotIn("secret-bot-token", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_recent_records_read_only_admin_endpoint(self):
        self.authorize()
        self.plugin.recent_records.return_value = {"ok": True, "items": [], "total": 0}
        response = self.client.get("/records")
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.plugin.recent_records.assert_called_once_with()
        self.plugin.scan.assert_not_called()
        self.plugin.init_plugin.assert_not_called()
        self.plugin.update_config.assert_not_called()

    def test_strict_payload_validation(self):
        self.authorize()
        for payload in ([1], {"operation": "sign_in", "values": {"session": "secret"}},
                        {"operation": "save", "values": {"api_id": True}},
                        {"operation": "send_code", "values": {"phone": "x" * 513}}):
            self.assertEqual(self.client.post("/telegram/action", json=payload).status_code, 400)
        self.assertEqual(self.client.post("/telegram/action", content="x" * 16385).status_code, 400)
        self.plugin.telegram_account_action.assert_not_awaited()

    def test_success_resets_retry(self):
        self.authorize()
        self.plugin._telegram_retry_after = 999
        response = self.client.post("/telegram/action", json={"operation": "sign_in", "values": {"code": "12345"}})
        self.assertTrue(response.json()["logged_in"])
        self.assertEqual(self.plugin._telegram_retry_after, 0)

    def test_validation_preserves_blank_bot_token(self):
        result = web_api.validate_settings(self.plugin, {"telegram_token": "ignored", "telegram_mode": "bot", "min_age": "12"})
        self.assertEqual(result["telegram_token"], "secret-bot-token")
        self.assertEqual(result["telegram_mode"], "user")
        self.assertEqual(result["min_age"], 12)
        for values in ({"enabled": "false"}, {"min_age": -1}, {"telegram_thread_id": "-3"}, {"api_hash": "secret"}):
            with self.assertRaises(web_api.UserAccountError):
                web_api.validate_settings(self.plugin, values)

    def test_disabled_scan_rejected(self):
        self.authorize()
        self.assertFalse(self.client.post("/run").json()["ok"])
        self.plugin.scan.assert_not_called()
