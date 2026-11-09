import unittest
from datetime import date
from unittest.mock import patch

from app.sdk.media import MetaInfo
from brushflow import BrushFlow, RequestUtils


class RecentEpisodeRuleTest(unittest.TestCase):
    def test_includes_cutoff_today_and_future_dates(self):
        today = date(2026, 11, 9)
        self.assertTrue(BrushFlow._air_date_is_recent("2026-11-06", today))
        self.assertTrue(BrushFlow._air_date_is_recent("2026-11-09", today))
        self.assertTrue(BrushFlow._air_date_is_recent("2026-11-15", today))

    def test_excludes_dates_older_than_cutoff_and_invalid_dates(self):
        today = date(2026, 11, 9)
        self.assertFalse(BrushFlow._air_date_is_recent("2026-11-05", today))
        self.assertFalse(BrushFlow._air_date_is_recent(None, today))
        self.assertFalse(BrushFlow._air_date_is_recent("2026-02-30", today))

    def test_video_filter_ignores_non_video_files(self):
        self.assertTrue(BrushFlow._video_file("Season 2/E02.mkv"))
        self.assertFalse(BrushFlow._video_file("Season 2/E02.srt"))

    def test_metainfo_extracts_single_and_multi_episode_ranges(self):
        single = MetaInfo("Example.Series.S02E03.mkv")
        multiple = MetaInfo("Example.Series.S02E03-E04.mkv")
        unknown = MetaInfo("Example.Series.2026.1080p.mkv")
        self.assertEqual((single.begin_season, single.episode_list), (2, [3]))
        self.assertEqual((multiple.begin_season, multiple.episode_list), (2, [3, 4]))
        self.assertEqual(unknown.episode_list, [])

    @patch.object(RequestUtils, "get_res")
    def test_douban_fallback_requires_explicit_episode_air_dates(self, get_res):
        class Response:
            ok = True

            @staticmethod
            def json():
                return {"episodes": [
                    {"episode": 1, "title": "第一集"},
                    {"episode": 2, "title": "第二集", "air_date": "2026-11-12"},
                ]}

        get_res.return_value = Response()
        dates = BrushFlow._douban_episode_air_dates("12345", 1)
        self.assertIsNone(dates.get(1))
        self.assertEqual(dates.get(2), "2026-11-12")

    def test_selection_excludes_nonmatching_files_before_starting(self):
        class Downloader:
            def __init__(self):
                self.calls = []

            def stop_torrents(self, torrent_hash):
                self.calls.append(("pause", torrent_hash))
                return True

            def get_files(self, torrent_hash):
                return [
                    {"id": 0, "name": "Show/Show.S01E01.mkv"},
                    {"id": 1, "name": "Show.S01E02.mkv"},
                    {"id": 2, "name": "Show.S01E03.srt"},
                ]

            def set_files(self, **kwargs):
                self.calls.append(("priority", kwargs["file_ids"], kwargs["priority"]))
                return True

            def start_torrents(self, torrent_hash):
                self.calls.append(("start", torrent_hash))
                return True

        downloader = Downloader()
        selected = BrushFlow._select_recent_episode_files(
            downloader, "hash", {"accepted": {"Show.S01E01.mkv"}}, "qbittorrent", "test"
        )
        self.assertTrue(selected)
        self.assertEqual(downloader.calls, [
            ("pause", "hash"),
            ("priority", [1, 2], 0),
            ("priority", [0], 1),
            ("start", "hash"),
        ])


if __name__ == "__main__":
    unittest.main()
