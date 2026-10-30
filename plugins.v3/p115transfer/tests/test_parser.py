import importlib.util
import json
import sys
import types
import unittest
from unittest.mock import MagicMock, Mock, call, patch
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

    def test_telegraph_paths_are_allowlisted_and_deduplicated(self):
        text = " ".join([
            "https://telegra.ph/Movie-10-27?x=1",
            "https://graph.org/Movie-10-27#section",
            "https://www.telegra.ph/Other-10-27",
            "https://telegra.ph.evil.test/Nope",
            "https://telegra.ph@127.0.0.1/Nope",
            "https://user:password@telegra.ph/Nope",
            "https://telegra.ph:8080/Nope",
            "https://telegra.ph/encoded%2fpath",
            "https://telegra.ph/..",
            "http://127.0.0.1/Nope",
        ])
        self.assertEqual(self.module.telegraph_paths(text), ["Movie-10-27", "Other-10-27"])

    def test_forwarded_photo_caption_url_buttons_are_received(self):
        plugin = self.module.P115Transfer()
        plugin._allowed_users = {"123"}
        plugin._handle = Mock()
        message = {
            "from": {"id": 123}, "chat": {"id": 123, "type": "private"},
            "forward_origin": {"type": "channel", "chat": {"id": -100}},
            "photo": [{"file_id": "poster"}], "caption": "电影介绍",
            "caption_entities": [{"type": "text_link", "url": "https://telegra.ph/Caption-10-27"}],
            "reply_markup": {"inline_keyboard": [[
                {"text": "查看资源", "url": "https://telegra.ph/Movie-10-27"},
                {"text": "广告", "url": "https://advert.example/"},
                {"text": "回调按钮", "callback_data": "secret"},
            ]]},
        }
        plugin._bot_message(Mock(), message)
        received = plugin._handle.call_args.args[1]
        self.assertEqual(self.module.telegraph_paths(received), ["Caption-10-27", "Movie-10-27"])
        self.assertNotIn("secret", received)
        message["from"]["id"] = 999
        plugin._handle.reset_mock()
        plugin._bot_message(Mock(), message)
        plugin._handle.assert_not_called()

    def test_mp_user_message_accepts_telegraph_links(self):
        plugin = self.module.P115Transfer()
        plugin._enabled = True
        plugin._handle = Mock()
        plugin.handle_message(types.SimpleNamespace(event_data={"text": "https://telegra.ph/Movie-10-27"}))
        plugin._handle.assert_called_once()

    def test_telegraph_code_and_anchor_resources_keep_filename_spaces(self):
        link = "ed2k://|file|哆啦A梦 movie.mkv|123|0123456789abcdef0123456789abcdef|/"
        content = [
            {"tag": "pre", "children": [{"tag": "code", "children": [link[:30], link[30:]]}]},
            {"tag": "p", "children": [
                {"tag": "a", "attrs": {"href": "https://115.com/s/Ab12?password=abcd"}, "children": ["网盘链接"]},
            ]},
        ]
        text = self.module.telegraph_text(content)
        self.assertEqual(self.module.parse_input(text)[0], [link])
        self.assertEqual(self.module.parse_share_links(text), [("Ab12", "abcd")])

    def test_multiple_share_links_keep_independent_passwords(self):
        self.assertEqual(self.module.parse_share_links(
            "https://115.com/s/First 提取码：abcd\n"
            "https://115cdn.com/s/Second?password=1234\n"
            "https://115share.com/s/Third 密码:efgh"
        ), [("First", "abcd"), ("Second", "1234"), ("Third", "efgh")])

    def test_telegraph_and_direct_resources_deduplicate_before_submit(self):
        plugin = self.module.P115Transfer()
        link = "ed2k://|file|movie.mkv|123|0123456789abcdef0123456789abcdef|/"
        plugin._fetch_telegraph = Mock(return_value=link + "\nhttps://115.com/s/First?password=abcd\nhttps://115.com/s/Second 密码1234")
        plugin._refresh_cookie = Mock()
        plugin._client = Mock()
        plugin._offline = Mock(return_value="离线已提交")
        plugin._receive_share = Mock(return_value="分享已转存")
        plugin._reply = Mock()
        plugin._handle({}, link + "\nhttps://telegra.ph/Movie-10-27\nhttps://graph.org/Movie-10-27")
        plugin._fetch_telegraph.assert_called_once_with("Movie-10-27")
        plugin._offline.assert_called_once_with(plugin._client, [link])
        self.assertEqual(plugin._receive_share.call_args_list, [
            call(plugin._client, "First", "abcd"), call(plugin._client, "Second", "1234"),
        ])

    def test_telegraph_failure_prevents_even_direct_resource_submission(self):
        plugin = self.module.P115Transfer()
        plugin._fetch_telegraph = Mock(side_effect=self.module.P115OperationError("Telegraph读取失败，未提交任务"))
        plugin._refresh_cookie = Mock()
        plugin._offline = Mock()
        plugin._reply = Mock()
        plugin._handle({}, "magnet:?xt=urn:btih:ABC\nhttps://telegra.ph/Movie-10-27")
        plugin._refresh_cookie.assert_not_called()
        plugin._offline.assert_not_called()
        self.assertIn("未提交任务", plugin._reply.call_args.args[1])

    def test_telegraph_page_limit_checked_before_fetch(self):
        plugin = self.module.P115Transfer()
        plugin._fetch_telegraph = Mock()
        with self.assertRaises(self.module.P115OperationError):
            plugin._resolve_resources("\n".join(f"https://telegra.ph/Page-{index}" for index in range(4)))
        plugin._fetch_telegraph.assert_not_called()

    def test_stop_during_telegraph_fetch_does_not_submit(self):
        plugin = self.module.P115Transfer()
        plugin._reply = Mock()
        plugin._refresh_cookie = Mock()
        def fetch_page(path):
            plugin._stop.set()
            return "magnet:?xt=urn:btih:ABC"
        plugin._fetch_telegraph = fetch_page
        plugin._handle({"bot_client": Mock()}, "https://telegra.ph/Movie-10-27")
        plugin._refresh_cookie.assert_not_called()
        plugin._reply.assert_not_called()

    def test_unavailable_forwarded_buttons_have_actionable_reply(self):
        plugin = self.module.P115Transfer()
        plugin._reply = Mock()
        plugin._refresh_cookie = Mock()
        plugin._handle({}, "只保留海报及影片介绍")
        self.assertIn("页面链接", plugin._reply.call_args.args[1])
        plugin._refresh_cookie.assert_not_called()

    def _telegraph_httpx(self, payload):
        httpx = types.ModuleType("httpx")
        httpx.Timeout = Mock(return_value=object())
        httpx.Client = MagicMock()
        response = httpx.Client.return_value.__enter__.return_value.stream.return_value.__enter__.return_value
        response.iter_bytes.return_value = [json.dumps(payload).encode()]
        return httpx, response

    def test_fetch_telegraph_uses_fixed_api_without_redirects_or_credentials(self):
        plugin = self.module.P115Transfer()
        plugin._bot_token = "private-bot-token"
        plugin._cookie = "private-cookie"
        plugin._resolve_bot_proxies = Mock(return_value=[("运行环境网络", None)])
        httpx, _ = self._telegraph_httpx({"ok": True, "result": {"content": [{"tag": "p", "children": ["magnet:?xt=urn:btih:ABC"]}]}})
        with patch.dict(sys.modules, {"httpx": httpx}):
            self.assertIn("magnet:", plugin._fetch_telegraph("Movie-10-27"))
        httpx.Client.assert_called_once_with(timeout=httpx.Timeout.return_value, proxy=None, trust_env=False, follow_redirects=False)
        httpx.Client.return_value.__enter__.return_value.stream.assert_called_once_with(
            "GET", "https://api.telegra.ph/getPage/Movie-10-27", params={"return_content": "true"},
        )
        self.assertNotIn("private-", str(httpx.Client.mock_calls))

    def test_fetch_telegraph_network_failure_tries_other_proxies(self):
        plugin = self.module.P115Transfer()
        plugin._resolve_bot_proxies = Mock(return_value=[("插件配置", "http://first-proxy"), ("运行环境网络", None)])
        httpx, response = self._telegraph_httpx({"ok": True, "result": {"content": ["页面正文"]}})
        response.raise_for_status.side_effect = [ConnectionError("private-url"), None]
        with patch.dict(sys.modules, {"httpx": httpx}):
            self.assertEqual(plugin._fetch_telegraph("Movie-10-27"), "页面正文")
        self.assertEqual([entry.kwargs["proxy"] for entry in httpx.Client.call_args_list], ["http://first-proxy", None])

    def test_telegraph_response_size_and_nested_structure_are_bounded(self):
        plugin = self.module.P115Transfer()
        plugin._resolve_bot_proxies = Mock(return_value=[("运行环境网络", None)])
        httpx, response = self._telegraph_httpx({})
        response.iter_bytes.return_value = [b"x" * (self.module.MAX_TELEGRAPH_BYTES + 1)]
        with patch.dict(sys.modules, {"httpx": httpx}), self.assertRaises(self.module.P115OperationError):
            plugin._fetch_telegraph("Movie-10-27")
        content = "content"
        for _ in range(66):
            content = {"tag": "p", "children": [content]}
        with self.assertRaises(self.module.P115OperationError):
            self.module.telegraph_text([content])

    def test_telegraph_errors_do_not_leak_server_responses(self):
        plugin = self.module.P115Transfer()
        plugin._resolve_bot_proxies = Mock(return_value=[("运行环境网络", None)])
        httpx, _ = self._telegraph_httpx({"ok": False, "error": "private-response-content"})
        with patch.dict(sys.modules, {"httpx": httpx}), self.assertRaises(self.module.P115OperationError) as error:
            plugin._fetch_telegraph("Movie-10-27")
        self.assertNotIn("private-", str(error.exception))

    def test_telegraph_all_network_failures_are_safe_and_not_replayed(self):
        plugin = self.module.P115Transfer()
        plugin._resolve_bot_proxies = Mock(return_value=[("插件配置", "http://private-proxy"), ("运行环境网络", None)])
        httpx, response = self._telegraph_httpx({})
        response.raise_for_status.side_effect = TimeoutError("private-url-with-token")
        with patch.dict(sys.modules, {"httpx": httpx}), self.assertRaises(self.module.P115OperationError) as error:
            plugin._fetch_telegraph("Movie-10-27")
        self.assertEqual(httpx.Client.call_count, 2)
        self.assertNotIn("private-", str(error.exception))
        self.assertIn("未提交任务", str(error.exception))

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
        log.warning.assert_not_called()
        self.assertTrue(any("Bot连接已恢复" in entry.args[0] for entry in log.info.call_args_list))
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

    def test_proxy_failover_wraps_after_recovery_without_warning_or_delay(self):
        plugin = self.module.P115Transfer()
        plugin.get_data = Mock(return_value=0)
        plugin._resolve_bot_proxies = Mock(return_value=[
            ("插件配置", "http://first-proxy"),
            ("MoviePilot配置", "http://second-proxy"),
            ("运行环境网络", None),
        ])
        httpx = types.ModuleType("httpx")
        httpx.ConnectError = type("ConnectError", (Exception,), {})
        httpx.ReadTimeout = type("ReadTimeout", (Exception,), {})
        httpx.Timeout = Mock()
        httpx.Limits = Mock()
        httpx.Client = MagicMock()
        plugin._bot_call = Mock(side_effect=[
            httpx.ConnectError(), {}, [],
            httpx.ReadTimeout(), httpx.ConnectError(), {}, [],
        ])
        stop = Mock()
        stop.is_set.side_effect = [False] * 5 + [True]
        with patch.dict(sys.modules, {"httpx": httpx}), patch.object(self.module, "logger") as log:
            plugin._poll_bot(stop)
        self.assertEqual([entry.kwargs["proxy"] for entry in httpx.Client.call_args_list], [
            "http://first-proxy", "http://second-proxy", None, "http://first-proxy",
        ])
        stop.wait.assert_not_called()
        log.warning.assert_not_called()
        self.assertEqual(plugin._bot_proxy_source, "插件配置")
        self.assertEqual(plugin._bot_error, "")
        self.assertEqual(sum("Bot连接已恢复" in entry.args[0] for entry in log.info.call_args_list), 2)

    def test_repeated_failed_cycles_warn_once_only_after_all_paths_fail(self):
        plugin = self.module.P115Transfer()
        plugin.get_data = Mock(return_value=0)
        plugin._resolve_bot_proxies = Mock(return_value=[
            ("插件配置", "http://private-first"),
            ("MoviePilot配置", "http://private-second"),
            ("运行环境网络", None),
        ])
        httpx = types.ModuleType("httpx")
        httpx.ConnectError = type("ConnectError", (Exception,), {})
        httpx.Timeout = Mock()
        httpx.Limits = Mock()
        httpx.Client = MagicMock()
        stop = Mock()
        stop.is_set.side_effect = [False] * 7 + [True]
        warnings_before_calls = []
        with patch.dict(sys.modules, {"httpx": httpx}), patch.object(self.module, "logger") as log:
            def bot_call(client, method, payload):
                warnings_before_calls.append(log.warning.call_count)
                if len(warnings_before_calls) <= 6:
                    raise httpx.ConnectError("private-token-in-url")
                return {} if method == "getWebhookInfo" else []
            plugin._bot_call = Mock(side_effect=bot_call)
            plugin._poll_bot(stop)
        self.assertEqual(warnings_before_calls[:4], [0, 0, 0, 1])
        self.assertEqual(log.warning.call_count, 1)
        self.assertIn("本轮全部连接方式均失败", str(log.warning.call_args))
        self.assertNotIn("private-", str(log.mock_calls))
        self.assertEqual(stop.wait.call_args_list, [call(30), call(60)])
        self.assertEqual(plugin._bot_error, "")

    def test_proxy_switch_preserves_update_offset_and_does_not_replay(self):
        plugin = self.module.P115Transfer()
        plugin.get_data = Mock(return_value=10)
        plugin.save_data = Mock()
        plugin._bot_message = Mock()
        plugin._resolve_bot_proxies = Mock(return_value=[("插件配置", "http://proxy"), ("运行环境网络", None)])
        httpx = types.ModuleType("httpx")
        httpx.ConnectError = type("ConnectError", (Exception,), {})
        httpx.Timeout = Mock()
        httpx.Limits = Mock()
        httpx.Client = MagicMock()
        plugin._bot_call = Mock(side_effect=[{}, [{"update_id": 10, "message": {}}], httpx.ConnectError(), {}, []])
        stop = Mock()
        stop.is_set.side_effect = [False] * 4 + [True]
        with patch.dict(sys.modules, {"httpx": httpx}), patch.object(self.module, "logger") as log:
            plugin._poll_bot(stop)
        plugin._bot_message.assert_called_once()
        self.assertEqual(plugin.save_data.call_count, 1)
        self.assertEqual(plugin.save_data.call_args.args[1], 11)
        self.assertEqual([entry.args[2]["offset"] for entry in plugin._bot_call.call_args_list if entry.args[1] == "getUpdates"], [10, 11, 11])
        log.warning.assert_not_called()

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
