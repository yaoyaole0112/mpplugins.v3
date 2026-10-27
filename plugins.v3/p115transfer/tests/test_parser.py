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
    spec = importlib.util.spec_from_file_location("p115transfer_test_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TransferPluginTest(unittest.TestCase):
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
        plugin = self.module.P115Transfer()
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
        plugin = self.module.P115Transfer()
        plugin._target_cid = "12345"
        client = Mock()
        client.clouddownload_task_add_url.return_value = {"state": True}
        link = "ed2k://|file|demo|4|0123456789abcdef0123456789abcdef|/"
        plugin._offline(client, [link])
        client.clouddownload_task_add_url.assert_called_once_with({"url": link, "wp_path_id": 12345}, type="web")
        client.clouddownload_task_add_urls.assert_not_called()
        plugin._target_cid = "0"
        self.assertEqual(plugin._path_id(client), 0)
        plugin._target_cid = "invalid"
        with self.assertRaises(ValueError):
            plugin._path_id(client)

    def test_custom_bot_authorization(self):
        plugin = self.module.P115Transfer()
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
        plugin = self.module.P115Transfer()
        plugin._enabled = True
        plugin._handle = Mock()
        plugin.handle_message(types.SimpleNamespace(event_data={"text": "/ns https://115.com/s/abc"}))
        plugin._handle.assert_not_called()
        plugin._bot_token = "configured"
        plugin.handle_message(types.SimpleNamespace(event_data={"text": "https://115.com/s/abc"}))
        plugin._handle.assert_not_called()

    def test_settings_spacing_descriptions_and_stable_models(self):
        plugin = self.module.P115Transfer()
        form, defaults = plugin.get_form()
        self.assertEqual(plugin.plugin_name, "115转存/离线")
        self.assertEqual(plugin.plugin_config_prefix, "p115transfer_")
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
        plugin = self.module.P115Transfer()
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

    def test_bot_proxy_candidates_fail_over_and_deduplicate(self):
        plugin = self.module.P115Transfer()
        plugin._bot_proxy = "socks5://plugin-proxy:6153"
        runtime = types.ModuleType("app.runtime.settings")
        runtime.get_runtime_setting = Mock(return_value={"https": "http://mp-proxy:6152"})
        with patch.dict(sys.modules, {"app.runtime.settings": runtime}):
            self.assertEqual(
                plugin._resolve_bot_proxies(),
                [
                    ("插件配置", "socks5://plugin-proxy:6153"),
                    ("MoviePilot配置", "http://mp-proxy:6152"),
                    ("运行环境网络", None),
                ],
            )
            runtime.get_runtime_setting.return_value = {
                "https": "socks5://plugin-proxy:6153"
            }
            self.assertEqual(
                plugin._resolve_bot_proxies(),
                [
                    ("插件配置", "socks5://plugin-proxy:6153"),
                    ("运行环境网络", None),
                ],
            )
            plugin._bot_proxy = ""
            runtime.get_runtime_setting.return_value = None
            self.assertEqual(plugin._resolve_bot_proxies(), [("运行环境网络", None)])

    def test_bot_network_failure_switches_proxy_without_replaying_updates(self):
        plugin = self.module.P115Transfer()
        plugin._bot_proxy = "socks5://plugin-proxy:6153"
        plugin.get_data = Mock(return_value=0)
        runtime = types.ModuleType("app.runtime.settings")
        runtime.get_runtime_setting = Mock(return_value={"https": "http://mp-proxy:6152"})
        httpx = types.ModuleType("httpx")
        httpx.ConnectError = type("ConnectError", (Exception,), {})
        httpx.Timeout = Mock(return_value=object())
        httpx.Limits = Mock(return_value=object())
        httpx.Client = MagicMock()
        plugin._bot_call = Mock(side_effect=[httpx.ConnectError("secret-token"), {}, []])
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        with patch.dict(sys.modules, {"app.runtime.settings": runtime, "httpx": httpx}), \
             patch.object(self.module, "logger") as log:
            plugin._poll_bot(stop)
        self.assertEqual(httpx.Client.call_count, 2)
        self.assertEqual(httpx.Client.call_args_list[0].kwargs["proxy"], "socks5://plugin-proxy:6153")
        self.assertEqual(httpx.Client.call_args_list[1].kwargs["proxy"], "http://mp-proxy:6152")
        self.assertEqual(plugin._bot_proxy_source, "MoviePilot配置")
        self.assertEqual(log.warning.call_count, 1)
        self.assertNotIn("secret-token", plugin._bot_error)
        self.assertEqual(plugin._bot_error, "")

    def test_all_bot_paths_fail_then_backoff_before_retry(self):
        plugin = self.module.P115Transfer()
        plugin.get_data = Mock(return_value=0)
        plugin._resolve_bot_proxies = Mock(return_value=[
            ("插件配置", "socks5://plugin-proxy:6153"),
            ("MoviePilot配置", "http://mp-proxy:6152"),
            ("运行环境网络", None),
        ])
        httpx = types.ModuleType("httpx")
        httpx.ConnectError = type("ConnectError", (Exception,), {})
        httpx.Timeout = Mock(return_value=object())
        httpx.Limits = Mock(return_value=object())
        httpx.Client = MagicMock()
        plugin._bot_call = Mock(side_effect=[
            httpx.ConnectError("secret-token"),
            httpx.ConnectError("secret-token"),
            httpx.ConnectError("secret-token"),
            {}, [],
        ])
        stop = Mock()
        stop.is_set.side_effect = [False] * 4 + [True]
        with patch.dict(sys.modules, {"httpx": httpx}), patch.object(self.module, "logger") as log:
            plugin._poll_bot(stop)
        self.assertEqual(
            [call.kwargs["proxy"] for call in httpx.Client.call_args_list],
            ["socks5://plugin-proxy:6153", "http://mp-proxy:6152", None, "socks5://plugin-proxy:6153"],
        )
        stop.wait.assert_called_once_with(30)
        self.assertEqual(log.warning.call_count, 1)
        self.assertNotIn("secret-token", str(log.mock_calls))
        self.assertNotIn("plugin-proxy", str(log.mock_calls))
        self.assertNotIn("mp-proxy", str(log.mock_calls))
        self.assertEqual(plugin._bot_error, "")

    def test_startup_connect_error_retries_and_recovers(self):
        plugin = self.module.P115Transfer()
        plugin.get_data = Mock(return_value=0)
        plugin._resolve_bot_proxy = Mock(return_value=None)
        httpx = types.ModuleType("httpx")
        httpx.ConnectError = type("ConnectError", (Exception,), {})
        httpx.ReadTimeout = type("ReadTimeout", (Exception,), {})
        httpx.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
        httpx.Timeout = Mock(return_value=object())
        httpx.Limits = Mock(return_value=object())
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
        plugin = self.module.P115Transfer()
        plugin.get_data = Mock(return_value=0)
        plugin._resolve_bot_proxy = Mock(return_value=None)
        httpx = types.ModuleType("httpx")
        httpx.ConnectError = type("ConnectError", (Exception,), {})
        httpx.Timeout = Mock(return_value=object())
        httpx.Limits = Mock(return_value=object())
        httpx.Client = MagicMock()
        plugin._bot_call = Mock(side_effect=httpx.ConnectError("must-not-leak"))
        stop = Mock()
        stop.is_set.side_effect = [False] * 6 + [True]
        with patch.dict(sys.modules, {"httpx": httpx}):
            plugin._poll_bot(stop)
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [30, 60, 120, 240, 300, 300])
        self.assertNotIn("must-not-leak", plugin._bot_error)

    def test_bot_authentication_error_does_not_retry(self):
        plugin = self.module.P115Transfer()
        plugin.get_data = Mock(return_value=0)
        plugin._resolve_bot_proxy = Mock(return_value=None)
        httpx = types.ModuleType("httpx")
        httpx.Timeout = Mock(return_value=object())
        httpx.Limits = Mock(return_value=object())
        httpx.Client = MagicMock()
        plugin._bot_call = Mock(side_effect=self.module.BotConfigurationError())
        stop = Mock()
        stop.is_set.return_value = False
        with patch.dict(sys.modules, {"httpx": httpx}):
            plugin._poll_bot(stop)
        stop.wait.assert_not_called()
        self.assertIn("认证", plugin._bot_error)

    def test_reconnect_reuses_no_stale_http_connection(self):
        plugin = self.module.P115Transfer()
        plugin.get_data = Mock(return_value=0)
        plugin._resolve_bot_proxy = Mock(return_value="http://proxy:7890")
        httpx = types.ModuleType("httpx")
        httpx.ConnectError = type("ConnectError", (Exception,), {})
        httpx.Timeout = Mock(return_value=object())
        httpx.Limits = Mock(return_value=object())
        httpx.Client = MagicMock()
        plugin._bot_call = Mock(side_effect=[httpx.ConnectError(), {}, []])
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        with patch.dict(sys.modules, {"httpx": httpx}):
            plugin._poll_bot(stop)
        self.assertEqual(httpx.Client.call_count, 2)
        self.assertEqual(httpx.Client.call_args_list[0].kwargs["timeout"], httpx.Timeout.return_value)
        self.assertEqual(httpx.Client.call_args_list[0].kwargs["proxy"], "http://proxy:7890")
        self.assertTrue(httpx.Client.return_value.close.called)

    def test_read_timeout_warning_is_coalesced_and_recovery_clears_error(self):
        plugin = self.module.P115Transfer()
        plugin.get_data = Mock(return_value=0)
        plugin._resolve_bot_proxy = Mock(return_value=None)
        httpx = types.ModuleType("httpx")
        httpx.ConnectError = type("ConnectError", (Exception,), {})
        httpx.ReadTimeout = type("ReadTimeout", (Exception,), {})
        httpx.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
        httpx.Timeout = Mock(return_value=object())
        httpx.Limits = Mock(return_value=object())
        httpx.Client = MagicMock()
        plugin._bot_call = Mock(side_effect=[{}, httpx.ReadTimeout(), httpx.ReadTimeout(), []])
        stop = Mock()
        stop.is_set.side_effect = [False, False, False, True]
        with patch.dict(sys.modules, {"httpx": httpx}), patch.object(self.module, "logger") as log:
            plugin._poll_bot(stop)
        self.assertEqual(log.warning.call_count, 1)
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [30, 60])
        self.assertEqual(httpx.Client.call_count, 3)
        self.assertEqual(plugin._bot_error, "")
        httpx.Timeout.assert_called_once_with(connect=15, read=45, write=15, pool=15)

    def test_http_status_error_records_status_without_secret_url(self):
        plugin = self.module.P115Transfer()
        plugin.get_data = Mock(return_value=0)
        plugin._resolve_bot_proxy = Mock(return_value=None)
        httpx = types.ModuleType("httpx")
        httpx.ConnectError = type("ConnectError", (Exception,), {})
        httpx.ReadTimeout = type("ReadTimeout", (Exception,), {})
        httpx.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
        httpx.Timeout = Mock(return_value=object())
        httpx.Limits = Mock(return_value=object())
        httpx.Client = MagicMock()
        error = httpx.HTTPStatusError("secret-token-in-url")
        error.response = types.SimpleNamespace(status_code=502)
        plugin._bot_call = Mock(side_effect=error)
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch.dict(sys.modules, {"httpx": httpx}):
            plugin._poll_bot(stop)
        self.assertIn("HTTP 502", plugin._bot_error)
        self.assertNotIn("secret-token", plugin._bot_error)

    def test_ed2k_spaces_encoded_forward_and_order(self):
        from urllib.parse import quote

        link = "ed2k://|file|test movie.mkv|123|0123456789abcdef0123456789abcdef|/"
        links, _, _ = self.module.parse_input(quote(link, safe="") + "\nmagnet:?xt=urn:btih:ABC")
        self.assertEqual(links, [link, "magnet:?xt=urn:btih:ABC"])
        self.module.validate_offline_links(links)

    def test_invalid_ed2k_does_not_submit(self):
        plugin = self.module.P115Transfer()
        client = Mock()
        with self.assertRaises(self.module.P115OperationError):
            plugin._offline(client, ["ed2k://|file|test|1|INVALID|/"])
        client.clouddownload_task_add_url.assert_not_called()
        client.clouddownload_task_add_urls.assert_not_called()

    def test_offline_api_error_is_safe_and_actionable(self):
        plugin = self.module.P115Transfer()
        plugin._target_cid = "123"
        client = Mock()
        client.clouddownload_task_add_url.return_value = {
            "state": False, "errcode": 10004,
            "error_msg": "任务已存在 ed2k://secret UID=secret BOT_TOKEN=secret",
        }
        with self.assertRaises(self.module.P115OperationError) as error:
            plugin._offline(client, ["magnet:?xt=urn:btih:ABC"])
        self.assertIn("10004", str(error.exception))
        self.assertIn("任务已存在", str(error.exception))
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(client.clouddownload_task_add_url.call_count, 1)

    def test_offline_batch_partial_failure_count(self):
        plugin = self.module.P115Transfer()
        plugin._target_cid = "456"
        client = Mock()
        client.clouddownload_task_add_urls.return_value = {"state": True, "data": {"result": [
            {"state": True, "info_hash": "first"},
            {"state": 0, "errcode": 0, "errno": 99, "msg": "链接格式错误"},
        ]}}
        result = plugin._offline(client, ["magnet:?xt=urn:btih:ABC", "magnet:?xt=urn:btih:DEF"])
        self.assertIn("新增1条，已存在0条，失败1条", result)
        self.assertIn("99", result)
        self.assertEqual(client.clouddownload_task_add_urls.call_args.kwargs, {"type": "web"})

    def test_offline_network_failure_does_not_replay(self):
        plugin = self.module.P115Transfer()
        plugin._target_cid = "123"
        client = Mock()
        client.clouddownload_task_add_url.side_effect = TimeoutError("token must not leak")
        with self.assertRaises(self.module.P115OperationError) as error:
            plugin._offline(client, ["magnet:?xt=urn:btih:ABC"])
        self.assertIn("结果可能不确定", str(error.exception))
        self.assertNotIn("token must not leak", str(error.exception))
        client.clouddownload_task_add_urls.assert_not_called()
        self.assertEqual(client.clouddownload_task_add_url.call_count, 1)

    def test_empty_results_are_not_reported_as_success(self):
        plugin = self.module.P115Transfer()
        plugin._target_cid = "1"
        client = Mock()
        client.clouddownload_task_add_url.return_value = {"state": True, "data": {"result": []}}
        with self.assertRaises(self.module.P115OperationError):
            plugin._offline(client, ["magnet:?xt=urn:btih:ABC"])

    def test_user_facing_strings_and_commands(self):
        plugin = self.module.P115Transfer()
        form, _ = plugin.get_form()
        import json

        forbidden = "nan" + "share"
        self.assertNotIn(forbidden, json.dumps(form, ensure_ascii=False).lower())
        self.assertNotIn(forbidden, plugin._target_path.lower())
        self.assertTrue(all(forbidden not in command for command in plugin._commands))

    def test_existing_offline_task_is_not_an_error(self):
        plugin = self.module.P115Transfer()
        plugin._target_cid = "1"
        client = Mock()
        client.clouddownload_task_add_url.return_value = {"state": False, "errno": 10008}
        self.assertIn("无需重复提交", plugin._offline(client, ["magnet:?xt=urn:btih:ABC"]))
        self.assertEqual(client.clouddownload_task_add_url.call_count, 1)

    def test_batch_separates_existing_tasks(self):
        plugin = self.module.P115Transfer()
        plugin._target_cid = "1"
        client = Mock()
        client.clouddownload_task_add_urls.return_value = {"state": True, "data": {"result": [
            {"state": True}, {"state": False, "errcode": 10008},
        ]}}
        result = plugin._offline(client, ["magnet:?xt=urn:btih:ABC", "magnet:?xt=urn:btih:DEF"])
        self.assertIn("新增1条，已存在1条", result)
        self.assertNotIn("失败", result)

    def test_reply_timeout_does_not_fail_or_replay_task(self):
        plugin = self.module.P115Transfer()
        plugin._bot_call = Mock(side_effect=TimeoutError("secret"))
        plugin._reply({"bot_client": Mock(), "chat_id": 1}, "已提交")
        self.assertEqual(plugin._bot_call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
