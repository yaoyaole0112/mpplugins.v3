import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from emetools.subscription_monitor import SubscriptionMonitor, matches_keyword, matches_subscription, normalize_channel


class MatchingTests(unittest.TestCase):
    def test_poll_timeout_on_one_channel_does_not_block_others_or_advance_cursor(self):
        plugin = MagicMock()
        plugin.get_data.return_value = None
        plugin._monitor_config = {"sub": {"enabled": True, "channels": []},
                                  "kw": {"enabled": False, "channels": []}}
        monitor = SubscriptionMonitor(plugin)
        monitor.channel_ids["sub"] = {12345, 67890}
        monitor._last_refresh = float("inf")
        monitor._last_msg_ids = {"12345": 10, "67890": 20}
        client = MagicMock()

        async def get_messages(entity, **kwargs):
            if entity == 12345:
                raise TimeoutError()
            return [SimpleNamespace(id=21, raw_text="新消息", chat_id=-10067890)]

        client.get_messages = AsyncMock(side_effect=get_messages)
        monitor._authorized = AsyncMock(return_value=client)
        monitor._on_message = AsyncMock()

        async def stop_after_poll(_seconds):
            plugin._monitor_config["sub"]["enabled"] = False

        with patch("emetools.subscription_monitor.asyncio.sleep", side_effect=stop_after_poll):
            asyncio.run(monitor._poll_channels())
        self.assertEqual(monitor._last_msg_ids["12345"], 10)
        self.assertEqual(monitor._last_msg_ids["67890"], 21)
        self.assertEqual(monitor._on_message.await_count, 1)
        self.assertFalse(monitor.last_error)
        self.assertIn("频道 12345 读取超时", monitor._poll_errors[12345])

    def test_poll_warning_clears_after_recovery_and_is_scoped(self):
        plugin = MagicMock()
        plugin.get_data.return_value = None
        plugin._monitor_config = {"sub": {"enabled": True, "channels": []},
                                  "kw": {"enabled": True, "channels": []}}
        monitor = SubscriptionMonitor(plugin)
        monitor.channel_ids = {"sub": {12345}, "kw": {67890}}
        monitor._last_refresh = float("inf")
        monitor._last_msg_ids = {"12345": 10, "67890": 20}
        monitor.client = MagicMock()
        monitor.client.is_connected.return_value = True
        client = MagicMock()
        attempts = []

        async def get_messages(entity, **kwargs):
            attempts.append(entity)
            if entity == 12345 and attempts.count(12345) == 1:
                raise TimeoutError()
            return []

        client.get_messages = AsyncMock(side_effect=get_messages)
        monitor._authorized = AsyncMock(return_value=client)
        monitor._subscriptions = []

        async def stop_after_recovery(_seconds):
            if attempts.count(12345) == 2:
                plugin._monitor_config["sub"]["enabled"] = False
                plugin._monitor_config["kw"]["enabled"] = False
            else:
                warnings = (await monitor.status())["poll_warnings"]
                self.assertIn("频道 12345 读取超时", warnings["sub"])
                self.assertFalse(warnings["kw"])

        with patch("emetools.subscription_monitor.asyncio.sleep", side_effect=stop_after_recovery):
            asyncio.run(monitor._poll_channels())
        self.assertFalse(monitor._poll_errors)
        self.assertEqual(monitor._last_msg_ids, {"12345": 10, "67890": 20})

    def test_status_resolves_stopped_keyword_channel_title_and_caches_it(self):
        plugin = MagicMock()
        plugin.get_data.return_value = None
        plugin._tg_session = ""
        plugin._tg_api_id = "1"
        plugin._tg_api_hash = "hash"
        plugin._monitor_config = {"sub": {"enabled": False, "channels": []},
                                  "kw": {"enabled": False, "channels": ["samplechannel"]}}
        monitor = SubscriptionMonitor(plugin)
        monitor.client = MagicMock()
        monitor.client.is_connected.return_value = True
        monitor.client.is_user_authorized = AsyncMock(return_value=True)
        monitor.client.get_entity = AsyncMock(return_value=SimpleNamespace(title="示例频道"))
        status = asyncio.run(monitor.status())
        self.assertEqual(status["channel_titles"]["kw"]["samplechannel"], "示例频道")
        plugin.save_data.assert_called_with("monitor_channel_titles", monitor.channel_titles)
        asyncio.run(monitor.status())
        monitor.client.get_entity.assert_awaited_once()

    def test_connected_after_code_request_is_not_logged_in(self):
        plugin = MagicMock()
        plugin.get_data.return_value = None
        plugin._tg_session = ""
        plugin._tg_api_id = "1"
        plugin._tg_api_hash = "hash"
        plugin._monitor_config = {"sub": {"enabled": False, "channels": []},
                                  "kw": {"enabled": False, "channels": []}}
        monitor = SubscriptionMonitor(plugin)
        monitor.client = MagicMock()
        monitor.client.is_connected.return_value = True
        monitor.client.is_user_authorized = AsyncMock(return_value=False)
        monitor.code_hash = "pending-code-hash"
        self.assertFalse(asyncio.run(monitor.status())["logged_in"])
        self.assertFalse(monitor.last_error)
        monitor.client.is_user_authorized = AsyncMock(return_value=True)
        self.assertTrue(asyncio.run(monitor.status())["logged_in"])

    def test_revoked_session_does_not_count_as_login(self):
        plugin = MagicMock()
        plugin.get_data.return_value = None
        plugin._tg_session = "saved-session"
        plugin._tg_api_id = "1"
        plugin._tg_api_hash = "hash"
        plugin._monitor_config = {"sub": {"enabled": False, "channels": []},
                                  "kw": {"enabled": False, "channels": []}}
        monitor = SubscriptionMonitor(plugin)
        monitor.client = MagicMock()
        monitor.client.is_connected.return_value = True
        monitor.client.is_user_authorized = AsyncMock(return_value=False)
        self.assertFalse(asyncio.run(monitor.status())["logged_in"])
        self.assertIn("请先登录", monitor.last_error)

    def test_seen_keys_normalize_channel_ids_and_survive_monitor_restart(self):
        plugin = MagicMock()
        plugin.get_data.return_value = [[12345, 17]]
        first = SubscriptionMonitor(plugin)
        self.assertIn(first._message_key(-10012345, 17), first._seen)
        first._mark_seen(first._message_key(12345, 18))
        saved = plugin.save_data.call_args.args[1]
        plugin.get_data.return_value = saved
        restarted = SubscriptionMonitor(plugin)
        self.assertIn(restarted._message_key(-10012345, 18), restarted._seen)

    def test_seen_limit_evicts_oldest_without_clearing_all(self):
        plugin = MagicMock()
        monitor = SubscriptionMonitor(plugin)
        for message_id in range(3001):
            monitor._mark_seen((12345, message_id))
        self.assertNotIn((12345, 0), monitor._seen)
        self.assertIn((12345, 3000), monitor._seen)
        self.assertEqual(len(monitor._seen), 3000)

    def test_subscription_metadata_must_agree(self):
        sub = {"name": "星际旅程", "year": "2026", "type": "电视剧", "season": 2,
               "media_source": "tmdb", "media_id": "12345"}
        self.assertTrue(matches_subscription("星际旅程 S02 (2026) TMDB:12345", sub))
        self.assertFalse(matches_subscription("星际旅程 S01 (2026) TMDB:12345", sub))
        self.assertFalse(matches_subscription("星际旅程 S02 (2025) TMDB:12345", sub))
        self.assertFalse(matches_subscription("星际旅程 S02 (2026) TMDB:99999", sub))
        self.assertFalse(matches_subscription("星际旅程 电影 S02 (2026)", sub))

    def test_short_names_do_not_match_description(self):
        sub = {"name": "醒来", "type": "电视剧", "season": 1}
        self.assertFalse(matches_subscription("其他电影 S01\n\n简介：醒来之后…", sub))
        self.assertTrue(matches_subscription("《醒来》 S01E02", sub))

    def test_short_name_and_season_can_be_on_separate_labeled_lines(self):
        sub = {"name": "征途", "year": "2026", "type": "剧集", "season": 1}
        text = """✅少爷：请您检阅
文件数 28 ｜ 体积 129.9GB

🎬 影视：征途
📺 季集：S01E01-28
📅 年份：2026
🎭 类型：剧集

https://115.com/s/example?password=r3f3"""
        self.assertTrue(matches_subscription(text, sub))

    def test_tmdb_link_can_match_an_alternate_channel_title(self):
        sub = {"name": "长剧名", "media_source": "tmdb", "media_id": "12345",
               "type": "电视剧", "season": 2}
        self.assertTrue(matches_subscription("Alternative Name S02 https://www.themoviedb.org/tv/12345", sub))
        self.assertFalse(matches_subscription("Alternative Name S02 https://www.themoviedb.org/tv/99999", sub))

    def test_channel_validation_and_keyword_blacklist(self):
        self.assertEqual(normalize_channel("https://t.me/Test_Channel"), "test_channel")
        with self.assertRaises(ValueError):
            normalize_channel("https://example.com/channel")
        self.assertTrue(matches_keyword("星际 S02", ["星际"], ["枪版"]))
        self.assertFalse(matches_keyword("星际 S02 枪版", ["星际"], ["枪版"]))

    def test_channel_message_matches_mp_subscription_and_forwards_once(self):
        plugin = MagicMock()
        plugin._tg_forward_token = "fake-bot-token"
        plugin._monitor_config = {"sub": {"enabled": True, "channels": ["sample"]},
                                  "kw": {"enabled": False, "channels": []}}
        plugin._subscription_items = MagicMock(return_value=[{"name": "星际旅程", "season": 2,
                                                                "year": "2026", "type": "电视剧"}])
        monitor = SubscriptionMonitor(plugin)
        monitor.channel_ids["sub"] = {12345}
        monitor.client = MagicMock()
        monitor._channel_entities[12345] = SimpleNamespace(title="示例频道", username="sample")
        monitor.client.get_entity = AsyncMock(return_value="destination")
        monitor.client.forward_messages = AsyncMock()
        message = object()
        event = SimpleNamespace(chat_id=-10012345, id=17, raw_text="星际旅程 S02 (2026)", message=message)
        with patch("emetools.subscription_monitor.get_runtime_setting",
                   return_value={"https": "http://mp-proxy.invalid:7890"}) as proxy_config, patch("httpx.AsyncClient") as http_class:
            client = http_class.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=MagicMock(**{"json.return_value": {
                "ok": True, "result": {"username": "destination_bot"}}}))
            with patch("emetools.subscription_monitor.logger.info") as logged:
                asyncio.run(monitor._on_message(event))
                asyncio.run(monitor._on_message(event))
                asyncio.run(monitor._on_message(SimpleNamespace(
                    chat_id=-10012345, id=18, raw_text="星际旅程 S02 (2026)", message=message)))
        self.assertEqual(monitor.client.forward_messages.await_count, 2)
        monitor.client.forward_messages.assert_awaited_with("destination", message)
        plugin._subscription_items.assert_called_once()
        self.assertEqual(len(monitor.hits), 2)
        self.assertEqual(monitor.hits[0]["channel"], "示例频道")
        self.assertEqual(client.get.await_count, 1)
        proxy_config.assert_called_once_with("PROXY", None)
        self.assertEqual(http_class.call_args.kwargs["proxy"], "http://mp-proxy.invalid:7890")
        self.assertTrue(any("已转发" in str(call.args[0]) for call in logged.call_args_list))
        self.assertNotIn("fake-bot-token", str(logged.call_args_list))

    def test_failed_subscription_fetch_is_diagnosable_and_retryable(self):
        plugin = MagicMock()
        plugin._monitor_config = {"sub": {"enabled": True}, "kw": {"enabled": False}}
        plugin._subscription_items = MagicMock(side_effect=RuntimeError("hidden database detail"))
        monitor = SubscriptionMonitor(plugin)
        monitor.channel_ids["sub"] = {12345}
        event = SimpleNamespace(chat_id=-10012345, id=17, raw_text="星际旅程 S02")
        asyncio.run(monitor._on_message(event))
        self.assertIn("读取 MoviePilot 订阅失败", monitor.last_error)
        self.assertNotIn("hidden database detail", monitor.last_error)
        self.assertNotIn((event.chat_id, event.id), monitor._seen)


class ForwardingTests(unittest.TestCase):
    def setUp(self):
        self.plugin = MagicMock()
        self.plugin.get_data.return_value = None
        self.plugin._monitor_config = {
            "sub": {"enabled": False, "channels": []},
            "kw": {"enabled": True, "channels": ["sample"], "keywords": ["资源"], "blacklist": []},
        }
        self.monitor = SubscriptionMonitor(self.plugin)
        self.monitor.channel_ids["kw"] = {12345}
        self.monitor.resolve_forward_bot = AsyncMock(return_value="destination")
        self.monitor.client = MagicMock()
        self.monitor.client.forward_messages = AsyncMock()
        self.monitor.client.send_message = AsyncMock()
        self.event = SimpleNamespace(chat_id=-10012345, id=17, raw_text="资源 🎬 下载链接 提取码：1234",
                                     message=SimpleNamespace(entities=[object()], noforwards=False))

    def test_restricted_forward_falls_back_to_text_with_original_entities_once(self):
        from telethon.errors import ChatForwardsRestrictedError
        self.monitor.client.forward_messages.side_effect = ChatForwardsRestrictedError(request=None)
        self.monitor.last_error = "转发失败：ChatForwardsRestrictedError"
        asyncio.run(self.monitor._on_message(self.event))
        asyncio.run(self.monitor._on_message(self.event))
        self.monitor.client.forward_messages.assert_awaited_once()
        self.monitor.client.send_message.assert_awaited_once_with(
            "destination", self.event.raw_text, formatting_entities=self.event.message.entities,
            parse_mode=None, link_preview=False)
        self.assertEqual(self.monitor.last_error, "")
        self.assertIn("已发送正文与链接", self.monitor.hits[0]["delivery"])
        self.assertEqual(self.monitor._last_msg_ids["12345"], 17)

    def test_known_protection_uses_text_without_failed_forward_rpc(self):
        self.monitor._channel_entities[12345] = SimpleNamespace(noforwards=True)
        asyncio.run(self.monitor._on_message(self.event))
        self.monitor.client.forward_messages.assert_not_awaited()
        self.monitor.client.send_message.assert_awaited_once()

    def test_unrelated_failure_does_not_copy_or_advance_checkpoint(self):
        self.monitor.client.forward_messages.side_effect = RuntimeError("private details")
        self.assertIs(asyncio.run(self.monitor._on_message(self.event)), False)
        self.monitor.client.send_message.assert_not_awaited()
        self.assertNotIn((12345, 17), self.monitor._seen)
        self.assertNotIn("12345", self.monitor._last_msg_ids)
        self.assertNotIn("private details", self.monitor.last_error)
        self.assertFalse(self.monitor._forwarding)

    def test_failed_text_delivery_remains_retryable_without_false_success(self):
        self.event.message.noforwards = True
        self.monitor.client.send_message.side_effect = [RuntimeError(), None]
        self.assertIs(asyncio.run(self.monitor._on_message(self.event)), False)
        self.assertFalse(self.monitor.hits)
        self.assertNotIn((12345, 17), self.monitor._seen)
        self.assertNotIn("12345", self.monitor._last_msg_ids)
        asyncio.run(self.monitor._on_message(self.event))
        self.assertEqual(len(self.monitor.hits), 1)
        self.assertEqual(self.monitor.last_error, "")

    def test_concurrent_live_and_poll_delivery_only_send_once(self):
        async def run():
            started = asyncio.Event()
            release = asyncio.Event()

            async def forward(*args):
                started.set()
                await release.wait()

            self.monitor.client.forward_messages.side_effect = forward
            first = asyncio.create_task(self.monitor._on_message(self.event))
            await started.wait()
            self.assertIs(await self.monitor._on_message(self.event), False)
            release.set()
            await first

        asyncio.run(run())
        self.monitor.client.forward_messages.assert_awaited_once()
        self.assertEqual(len(self.monitor.hits), 1)
        self.assertFalse(self.monitor._forwarding)


if __name__ == "__main__":
    unittest.main()
