import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from emetools.subscription_monitor import SubscriptionMonitor, matches_keyword, matches_subscription, normalize_channel


class MatchingTests(unittest.TestCase):
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
        monitor.client.get_entity = AsyncMock(return_value="destination")
        monitor.client.forward_messages = AsyncMock()
        message = object()
        event = SimpleNamespace(chat_id=-10012345, id=17, raw_text="星际旅程 S02 (2026)", message=message)
        with patch("httpx.AsyncClient") as http_class:
            client = http_class.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=MagicMock(**{"json.return_value": {
                "ok": True, "result": {"username": "destination_bot"}}}))
            with patch("emetools.subscription_monitor.logger.info") as logged:
                asyncio.run(monitor._on_message(event))
                asyncio.run(monitor._on_message(event))
        monitor.client.forward_messages.assert_awaited_once_with("destination", message)
        plugin._subscription_items.assert_called_once()
        self.assertEqual(len(monitor.hits), 1)
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


if __name__ == "__main__":
    unittest.main()
