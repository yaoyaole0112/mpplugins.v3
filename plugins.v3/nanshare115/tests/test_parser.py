import importlib.util
import sys
import types
import unittest
from unittest.mock import MagicMock, Mock, patch
from pathlib import Path


def _load_module():
    app_event = types.ModuleType("app.core.event")
    app_event.Event = object
    app_event.eventmanager = types.SimpleNamespace(register=lambda *_a, **_k: (lambda fn: fn))
    app_log = types.ModuleType("app.log")
    app_log.logger = Mock()
    app_plugins = types.ModuleType("app.plugins")
    app_plugins._PluginBase = object
    app_message = types.ModuleType("app.schemas.message")
    app_message.Message = object
    app_types = types.ModuleType("app.schemas.types")
    app_types.EventType = types.SimpleNamespace(PluginAction="PluginAction", UserMessage="UserMessage")
    p115client = types.ModuleType("p115client")
    p115client.P115Client = object
    sys.modules.update({
        "app": types.ModuleType("app"),
        "app.core": types.ModuleType("app.core"),
        "app.core.event": app_event,
        "app.log": app_log,
        "app.plugins": app_plugins,
        "app.schemas": types.ModuleType("app.schemas"),
        "app.schemas.message": app_message,
        "app.schemas.types": app_types,
        "p115client": p115client,
    })
    path = Path(__file__).parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("nanshare115_test_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def test_parse_mixed_links(self):
        offline, shares, parsed_share = self.module.parse_input(
            "magnet:?xt=urn:btih:ABC https://115.com/s/Ab12?password=1234"
        )
        self.assertEqual(len(offline), 1)
        self.assertEqual(len(shares), 1)
        self.assertEqual(parsed_share, ("Ab12", "1234"))

    def test_parse_ed2k_and_torrent(self):
        text = "ed2k://|file|demo.mkv|4|0123456789abcdef0123456789abcdef|/ https://example.test/a.torrent"
        offline, _, share = self.module.parse_input(text)
        self.assertEqual(len(offline), 2)
        self.assertIsNone(share)

    def test_cookie_from_helper_and_refresh(self):
        plugin = self.module.NanShare115()
        plugin.get_config = Mock(return_value={"cookies": "first-cookie"})
        with patch.object(self.module, "P115Client") as factory:
            plugin._refresh_cookie()
            factory.assert_called_once_with("first-cookie")
            plugin._refresh_cookie()
            self.assertEqual(factory.call_count, 1)
            plugin.get_config.return_value = {"cookies": "new-cookie"}
            plugin._refresh_cookie()
            factory.assert_called_with("new-cookie")
            plugin.get_config.return_value = {}
            plugin._refresh_cookie()
            self.assertIsNone(plugin._client)

    def test_target_cid_and_offline_payload(self):
        plugin = self.module.NanShare115()
        plugin._target_cid = "12345"
        client = Mock()
        client.clouddownload_task_add_urls.return_value = {"state": True, "data": {"result": [{}]}}
        plugin._offline(client, ["ed2k://|file|demo|4|ABC|/"])
        self.assertEqual(client.clouddownload_task_add_urls.call_args.args[0]["wp_path_id"], 12345)
        plugin._target_cid = "0"
        self.assertEqual(plugin._path_id(client), 0)
        plugin._target_cid = "invalid"
        with self.assertRaises(ValueError):
            plugin._path_id(client)

    def test_custom_bot_authorization(self):
        plugin = self.module.NanShare115()
        plugin._allowed_users = {"123"}
        plugin._handle = Mock()
        client = Mock()
        message = {"from": {"id": 999}, "chat": {"id": 123, "type": "private"}, "text": "/ns ed2k://|file|a|1|ABC|/"}
        plugin._bot_message(client, message)
        plugin._handle.assert_not_called()
        message["from"]["id"] = 123
        message["chat"]["type"] = "group"
        plugin._bot_message(client, message)
        plugin._handle.assert_not_called()
        message["chat"]["type"] = "private"
        plugin._bot_message(client, message)
        self.assertEqual(plugin._handle.call_count, 1)

    def test_no_duplicate_mp_command_or_custom_bot_consumption(self):
        plugin = self.module.NanShare115()
        plugin._enabled = True
        plugin._handle = Mock()
        plugin.handle_message(types.SimpleNamespace(event_data={"text": "/ns https://115.com/s/abc"}))
        plugin._handle.assert_not_called()
        plugin._bot_token = "configured"
        plugin.handle_message(types.SimpleNamespace(event_data={"text": "https://115.com/s/abc"}))
        plugin._handle.assert_not_called()

    def test_settings_spacing_descriptions_and_stable_models(self):
        plugin = self.module.NanShare115()
        form, defaults = plugin.get_form()
        self.assertEqual(plugin.plugin_name, "115转存/离线")
        self.assertEqual(plugin.plugin_config_prefix, "nanshare115_")
        self.assertEqual(form[0]["props"]["class"], "pa-4")
        groups = form[0]["content"][2:]
        self.assertEqual(len(groups), 5)
        models = []
        for group in groups:
            self.assertEqual(group["props"]["class"], "mb-6")
            title, description, control = group["content"]
            self.assertTrue(title["text"])
            self.assertTrue(description["text"])
            self.assertEqual(description["props"]["class"], "text-body-2 text-medium-emphasis mb-4")
            self.assertEqual(control["component"], "VTextField")
            self.assertEqual(control["props"]["variant"], "outlined")
            self.assertEqual(control["props"]["density"], "comfortable")
            models.append(control["props"]["model"])
        self.assertEqual(models, ["bot_token", "allowed_users", "bot_proxy", "target_path", "target_cid"])
        self.assertNotIn("cookie", defaults)

    def test_bot_proxy_prefers_explicit_then_mp(self):
        plugin = self.module.NanShare115()
        runtime = types.ModuleType("app.runtime.settings")
        runtime.get_runtime_setting = Mock(return_value={"https": "http://mp-proxy:7890"})
        with patch.dict(sys.modules, {"app.runtime.settings": runtime}):
            self.assertEqual(plugin._resolve_bot_proxy(), "http://mp-proxy:7890")
            self.assertEqual(plugin._bot_proxy_source, "MoviePilot配置")
            plugin._bot_proxy = "http://explicit-proxy:7890"
            self.assertEqual(plugin._resolve_bot_proxy(), plugin._bot_proxy)
            plugin._bot_proxy = ""
            runtime.get_runtime_setting.return_value = None
            self.assertIsNone(plugin._resolve_bot_proxy())

    def test_startup_connect_error_retries_and_recovers(self):
        plugin = self.module.NanShare115()
        plugin.get_data = Mock(return_value=0)
        plugin._resolve_bot_proxy = Mock(return_value=None)
        httpx = types.ModuleType("httpx")
        httpx.ConnectError = type("ConnectError", (Exception,), {})
        httpx.Client = MagicMock()
        plugin._bot_call = Mock(side_effect=[httpx.ConnectError("secret-token"), {}, []])
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        with patch.dict(sys.modules, {"httpx": httpx}):
            plugin._poll_bot(stop)
        stop.wait.assert_called_once_with(30)
        self.assertEqual([call.args[1] for call in plugin._bot_call.call_args_list], ["getWebhookInfo", "getWebhookInfo", "getUpdates"])
        self.assertEqual(plugin._bot_error, "")

    def test_startup_backoff_is_bounded_and_stoppable(self):
        plugin = self.module.NanShare115()
        plugin.get_data = Mock(return_value=0)
        plugin._resolve_bot_proxy = Mock(return_value=None)
        httpx = types.ModuleType("httpx")
        httpx.ConnectError = type("ConnectError", (Exception,), {})
        httpx.Client = MagicMock()
        plugin._bot_call = Mock(side_effect=httpx.ConnectError("must-not-leak"))
        stop = Mock()
        stop.is_set.side_effect = [False] * 6 + [True]
        with patch.dict(sys.modules, {"httpx": httpx}):
            plugin._poll_bot(stop)
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [30, 60, 120, 240, 300, 300])
        self.assertNotIn("must-not-leak", plugin._bot_error)

    def test_bot_authentication_error_does_not_retry(self):
        plugin = self.module.NanShare115()
        plugin.get_data = Mock(return_value=0)
        plugin._resolve_bot_proxy = Mock(return_value=None)
        httpx = types.ModuleType("httpx")
        httpx.Client = MagicMock()
        plugin._bot_call = Mock(side_effect=self.module.BotConfigurationError())
        stop = Mock()
        stop.is_set.return_value = False
        with patch.dict(sys.modules, {"httpx": httpx}):
            plugin._poll_bot(stop)
        stop.wait.assert_not_called()
        self.assertIn("认证", plugin._bot_error)


if __name__ == "__main__":
    unittest.main()
