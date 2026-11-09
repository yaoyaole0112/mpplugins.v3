import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.sdk.media import MetaInfo
from brushflow import BrushFlow, RequestUtils
from brushflow.models import BrushTaskPayload


class RecentEpisodeRuleTest(unittest.TestCase):
    def setUp(self):
        for name in ("_tmdb_episode_air_date", "_tmdb_web_episode_air_dates",
                     "_recent_episode_douban_id", "_douban_episode_air_dates"):
            getattr(BrushFlow, name).cache_clear()

    def select_files(self, paths, media, season_dates=()):
        plugin = BrushFlow.__new__(BrushFlow)
        plugin.chain = SimpleNamespace(recognize_media=Mock(return_value=media))
        with patch("brushflow.TorrentHelper") as torrent_helper, patch("brushflow.TmdbChain") as tmdb:
            torrent_helper.return_value.get_fileinfo_from_torrent_content.return_value = (None, paths)
            tmdb.return_value.tmdb_episodes.return_value = [
                SimpleNamespace(episode_number=number, air_date=value) for number, value in season_dates
            ]
            return plugin._recent_episode_file_selection(b"torrent", "test")

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

    def test_movie_year_uses_tmdb_release_date_then_recognized_or_filename_year(self):
        self.assertEqual(
            BrushFlow._movie_year(SimpleNamespace(release_date="2026-12-01", year="2025"), "2024"),
            2026,
        )
        self.assertEqual(BrushFlow._movie_year(SimpleNamespace(year="2026"), None), 2026)
        self.assertEqual(BrushFlow._movie_year(SimpleNamespace(), "2026"), 2026)
        self.assertIsNone(BrushFlow._movie_year(SimpleNamespace(), None))

    def test_recent_rule_accepts_current_year_movie_and_rejects_old_or_unknown_year(self):
        cases = [
            ("Current.Movie.2026.mkv", SimpleNamespace(type="电影", release_date="2026-05-01"), True),
            ("Old.Movie.2025.mkv", SimpleNamespace(type="movie", release_date="2025-12-31"), False),
            ("Unknown.Movie.mkv", SimpleNamespace(type="movie"), False),
        ]
        for path, media, expected in cases:
            with self.subTest(path=path):
                result = self.select_files([path], media)
                if expected:
                    self.assertEqual(result["accepted"], {path})
                else:
                    self.assertIsNone(result)

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

    @patch.object(BrushFlow, "_recent_episode_douban_id")
    @patch.object(BrushFlow, "_tmdb_web_episode_air_dates")
    @patch.object(BrushFlow, "_tmdb_episode_air_date", return_value="2099-11-09")
    def test_season_list_missing_episode_uses_single_episode_api(self, single, web, douban):
        path = "有你的恋歌.Sing.for.You.S03E10.2026.2160p.MGTV.WEB-DL.H264.AAC-ADWeb.mkv"
        result = self.select_files([path], SimpleNamespace(tmdb_id=281511), [(8, "2026-11-02")])
        self.assertEqual(result["accepted"], {path})
        single.assert_called_once_with(281511, 3, 10)
        web.assert_not_called()
        douban.assert_not_called()

    @patch.object(BrushFlow, "_recent_episode_douban_id")
    @patch.object(BrushFlow, "_tmdb_web_episode_air_dates", return_value={35: "2099-11-09"})
    @patch.object(BrushFlow, "_tmdb_episode_air_date", return_value=None)
    def test_null_api_date_uses_web_and_reuses_for_duplicate_file(self, single, web, douban):
        paths = [
            "Notes.of.DNA.Appraiser.S01E35.2160p.YOUKU.WEB-DL.AAC2.0.H.265-MWeb.mkv",
            "Notes.of.DNA.Appraiser.S01E35.1080p.YOUKU.WEB-DL.AAC2.0.H.264-MWeb.mkv",
        ]
        result = self.select_files(paths, SimpleNamespace(tmdb_id=259785), [(35, None)])
        self.assertEqual(result["accepted"], set(paths))
        single.assert_called_once_with(259785, 1, 35)
        web.assert_called_once_with(259785, 1)
        douban.assert_not_called()

    @patch.object(BrushFlow, "_tmdb_web_episode_air_dates", return_value={1: "2099-11-09", 2: "2099-11-09"})
    @patch.object(BrushFlow, "_tmdb_episode_air_date", return_value=None)
    def test_does_not_replace_known_old_dates_when_filling_missing_episodes(self, single, web):
        paths = ["Show.S01E01.mkv", "Show.S01E02.mkv", "Show.S01E01-E02.mkv"]
        result = self.select_files(paths, SimpleNamespace(tmdb_id=123), [(1, "2000-01-01"), (2, None)])
        self.assertEqual(result["accepted"], {paths[1]})

    @patch.object(BrushFlow, "_tmdb_web_episode_air_dates", return_value={1: "2099-11-09", 2: "2099-11-09"})
    @patch.object(BrushFlow, "_tmdb_episode_air_date")
    def test_multi_episode_file_uses_one_season_web_request(self, single, web):
        path = "Show.S01E01-E02.mkv"
        result = self.select_files([path], SimpleNamespace(tmdb_id=123))
        self.assertEqual(result["accepted"], {path})
        web.assert_called_once_with(123, 1)
        single.assert_not_called()

    @patch.object(BrushFlow, "_recent_episode_douban_id", return_value="12345")
    @patch.object(BrushFlow, "_douban_episode_air_dates", return_value={2: "2099-11-09"})
    @patch.object(BrushFlow, "_tmdb_web_episode_air_dates", return_value={})
    @patch.object(BrushFlow, "_tmdb_episode_air_date", return_value=None)
    def test_douban_fills_only_missing_dates_after_exact_name_lookup(self, single, web, douban, lookup):
        path = "Example.Series.2026.S01E01-E02.mkv"
        media = SimpleNamespace(tmdb_id=123, title="Example Series", year="2026")
        result = self.select_files([path], media, [(1, "2099-11-08")])
        self.assertEqual(result["accepted"], {path})
        lookup.assert_called_once_with("Example Series", "2026", 1)

    @patch.object(BrushFlow, "_recent_episode_douban_id", return_value="")
    @patch.object(BrushFlow, "_tmdb_web_episode_air_dates", return_value={})
    @patch.object(BrushFlow, "_tmdb_episode_air_date", return_value=None)
    def test_all_sources_unavailable_still_excludes_file(self, single, web, douban):
        self.assertIsNone(self.select_files(["Show.S01E35.mkv"], SimpleNamespace(tmdb_id=123)))

    @patch.object(BrushFlow, "_recent_episode_douban_id")
    @patch.object(BrushFlow, "_tmdb_web_episode_air_dates")
    @patch.object(BrushFlow, "_tmdb_episode_air_date")
    def test_episode_group_never_falls_back_to_default_numbering(self, single, web, douban):
        meta = SimpleNamespace(
            episode_list=[10], begin_season=3, begin_episode=10, end_episode=None, episode_group="group"
        )
        with patch("brushflow.MetaInfo", return_value=meta):
            self.assertIsNone(self.select_files(["Show.S03E10.mkv"], SimpleNamespace(tmdb_id=123)))
        single.assert_not_called()
        web.assert_not_called()
        douban.assert_not_called()

    @patch.object(BrushFlow, "_tmdb_episode_air_date", return_value="2099-11-09")
    def test_special_season_zero_is_not_changed_to_season_one(self, single):
        path = "Show.S00E01.mkv"
        self.assertEqual(self.select_files([path], SimpleNamespace(tmdb_id=123))["accepted"], {path})
        single.assert_called_once_with(123, 0, 1)

    def test_web_dates_are_scoped_to_exact_tv_season_and_episode(self):
        def row(tmdb_id, season, episode, raw_date):
            return f"""<div class="episode">
                <a data-season-number="{season}" data-episode-number="{episode}"
                   href="/tv/{tmdb_id}/season/{season}/episode/{episode}?language=en-US">Episode</a>
                <span class="date">{raw_date}</span></div>"""

        html = '<span class="date">November 1, 2026</span>'
        html += row(259785, 1, 35, "November  9, 2026")
        html += row(259785, 1, 36, "November 10, 2026")
        html += row(259785, 1, 37, "")
        html += row(259785, 2, 38, "November 9, 2026")
        html += row(123, 1, 39, "November 9, 2026")
        html += row(259785, 1, 40, "Not available")
        self.assertEqual(BrushFlow._parse_tmdb_web_episode_air_dates(html, 259785, 1), {
            35: "2026-11-09", 36: "2026-11-10",
        })

    @patch("brushflow.TmdbApi")
    def test_single_episode_response_must_have_matching_season_and_episode(self, api):
        api.return_value.get_tv_episode_detail.return_value = {
            "season_number": 2, "episode_number": 10, "air_date": "2026-11-09"
        }
        self.assertIsNone(BrushFlow._tmdb_episode_air_date(281511, 3, 10))

    @patch.object(RequestUtils, "get_res")
    def test_douban_lookup_requires_unambiguous_name_year_and_season(self, get_res):
        response = get_res.return_value
        response.ok = True
        response.json.return_value = [
            {"id": "1", "title": "有你的恋歌 第三季", "year": "2026"},
            {"id": "2", "title": "有你的恋歌 第二季", "year": "2026"},
            {"id": "3", "title": "其他恋歌 第三季", "year": "2026"},
            {"id": "4", "title": "有你的恋歌 第三季", "year": "2025"},
        ]
        self.assertEqual(BrushFlow._recent_episode_douban_id("有你的恋歌", "2026", 3), "1")
        BrushFlow._recent_episode_douban_id.cache_clear()
        response.json.return_value.append({"id": "5", "title": "有你的恋歌 第三季", "year": "2026"})
        self.assertEqual(BrushFlow._recent_episode_douban_id("有你的恋歌", "2026", 3), "")

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

    def test_blacklist_requires_exact_title_and_other_selected_site(self):
        history = [
            SimpleNamespace(torrent_name="Show.S01E01.mkv", torrent_site="站点乙"),
            SimpleNamespace(torrent_name="Show.S01E01.mkv extra", torrent_site="站点乙"),
            SimpleNamespace(torrent_name="Show.S01E01.mkv", torrent_site="站点丙"),
        ]
        cache = {}
        with patch("brushflow.DownloadHistoryOper") as history_oper:
            history_oper.return_value.query.return_value = (history, len(history))
            blocked, reason = BrushFlow._is_blacklist_duplicate(
                "Show.S01E01.mkv", {"站点乙"}, cache
            )
        self.assertTrue(blocked)
        self.assertIn("站点乙", reason)
        self.assertEqual(history_oper.return_value.query.call_count, 1)

    def test_blacklist_normalizes_nfc_and_does_not_block_unselected_sites(self):
        history = [
            SimpleNamespace(torrent_name="Café.S01E01.mkv", torrent_site="当前站点"),
            SimpleNamespace(torrent_name="Café.S01E01.mkv", torrent_site="未选择站点"),
        ]
        with patch("brushflow.DownloadHistoryOper") as history_oper:
            history_oper.return_value.query.return_value = (history, len(history))
            blocked, reason = BrushFlow._is_blacklist_duplicate(
                "Cafe\u0301.S01E01.mkv", {"别的站点"}, {}
            )
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_blacklist_query_failure_is_cached_and_excluded(self):
        cache = {}
        with patch("brushflow.DownloadHistoryOper") as history_oper:
            history_oper.return_value.query.side_effect = RuntimeError("db unavailable")
            first = BrushFlow._is_blacklist_duplicate("Show.mkv", {"站点乙"}, cache)
            second = BrushFlow._is_blacklist_duplicate("Show.mkv", {"站点乙"}, cache)
        self.assertTrue(first[0])
        self.assertTrue(second[0])
        self.assertEqual(history_oper.return_value.query.call_count, 1)

    def test_blacklist_site_ids_are_deduplicated_and_positive(self):
        payload = BrushTaskPayload(
            name="task", site_id=1, downloader="qb", blacklist_site_ids=[2, 2, 3]
        )
        self.assertEqual(payload.blacklist_site_ids, [2, 3])
        with self.assertRaises(ValueError):
            BrushTaskPayload(name="task", site_id=1, downloader="qb", blacklist_site_ids=[0])


if __name__ == "__main__":
    unittest.main()
