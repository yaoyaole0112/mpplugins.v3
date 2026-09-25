import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from emetools.subscription_monitor import matches_keyword, matches_subscription, normalize_channel


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

    def test_channel_validation_and_keyword_blacklist(self):
        self.assertEqual(normalize_channel("https://t.me/Test_Channel"), "test_channel")
        with self.assertRaises(ValueError):
            normalize_channel("https://example.com/channel")
        self.assertTrue(matches_keyword("星际 S02", ["星际"], ["枪版"]))
        self.assertFalse(matches_keyword("星际 S02 枪版", ["星际"], ["枪版"]))


if __name__ == "__main__":
    unittest.main()
