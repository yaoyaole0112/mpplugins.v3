import importlib.util
import sys
import types
import unittest
from unittest.mock import Mock, patch
from pathlib import Path


def _load_module():
    app_event = types.ModuleType("app.core.event")
    app_event.Event = object
    app_event.eventmanager = types.SimpleNamespace(register=lambda *_a, **_k: (lambda fn: fn))
    app_log = types.ModuleType("app.log")
    app_log.logger = types.SimpleNamespace(error=lambda *_a, **_k: None, info=lambda *_a, **_k: None)
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


if __name__ == "__main__":
    unittest.main()
