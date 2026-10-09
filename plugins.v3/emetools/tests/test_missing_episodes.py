"""Regressions for completed-series subscription cleanup."""

import unittest
import copy
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.schemas.types import MediaSource, MediaType

from emetools.missing_episodes import DEFAULT_MISSING, MissingEpisodeDetector, normalize_cancel_config


class MissingFillInventoryTests(unittest.TestCase):
    def setUp(self):
        self.record = {"ServerName": "Emby", "LibraryName": "剧集", "TmdbId": "123", "SeriesId": "series",
                       "SeasonNum": 5, "MissingEpisodeNumbers": [12, 13], "MissingEpisodes": "12-13"}
        self.detector = object.__new__(MissingEpisodeDetector)
        self.detector._scan_lock = threading.Lock()
        service = SimpleNamespace(config=SimpleNamespace(name="Emby", config={"host": "http://emby.invalid", "apikey": "fake"}),
                                  instance=SimpleNamespace(get_user=lambda: "user"))
        self.detector._mediaserver_helper = SimpleNamespace(get_services=lambda **kwargs: {"emby": service})
        self.detector._results = [copy.deepcopy(self.record)]
        self.detector.save_data = MagicMock()
        self.views = {"Items": [{"Id": "library", "Name": "剧集"}]}
        self.series = {"Items": [{"Id": "series", "ProviderIds": {"Tmdb": "123"}}], "TotalRecordCount": 1}
        self.episodes = {"Items": [{"SeriesId": "series", "ParentIndexNumber": 5, "IndexNumber": 12, "IndexNumberEnd": 13}],
                         "TotalRecordCount": 1}
        self.detector._request_json = MagicMock(side_effect=[self.views, self.series, self.episodes])

    def test_complete_requires_real_matching_series_episodes(self):
        self.assertEqual(self.detector.verify_inventory(self.record), [])
        self.assertEqual(self.detector._results, [])
        self.detector.save_data.assert_called_once()

    def test_virtual_wrong_season_and_wrong_series_are_not_complete(self):
        for change in [{"LocationType": "Virtual"}, {"ParentIndexNumber": 4}, {"SeriesId": "other"}]:
            with self.subTest(change=change):
                self.episodes["Items"][0] = {"SeriesId": "series", "ParentIndexNumber": 5,
                                              "IndexNumber": 12, "IndexNumberEnd": 13, **change}
                self.detector._request_json.side_effect = [self.views, self.series, self.episodes]
                self.assertEqual(self.detector.verify_inventory(self.record), [12, 13])

    def test_partial_inventory_updates_missing_ranges_without_subscription_actions(self):
        self.episodes["Items"][0]["IndexNumberEnd"] = 12
        self.assertEqual(self.detector.verify_inventory(self.record), [13])
        self.assertEqual(self.detector._results[0]["MissingEpisodeNumbers"], [13])
        self.assertEqual(self.detector._results[0]["MissingEpisodes"], "13")

    def test_server_failure_absent_series_and_incomplete_payload_raise(self):
        for responses in [[None], [self.views, {"Items": [], "TotalRecordCount": 0}],
                          [self.views, self.series, None], [self.views, self.series, {"Items": []}]]:
            with self.subTest(responses=responses):
                self.detector._request_json.side_effect = responses
                with self.assertRaises(ValueError):
                    self.detector.verify_inventory(self.record)
                self.assertEqual(self.detector._results[0]["MissingEpisodeNumbers"], [12, 13])
        self.detector.save_data.assert_not_called()

    def test_pagination_is_required_even_when_first_page_contains_targets(self):
        self.episodes["TotalRecordCount"] = 2
        self.detector._request_json.side_effect = [self.views, self.series, self.episodes,
                                                  {"Items": [], "TotalRecordCount": 2}]
        with self.assertRaisesRegex(ValueError, "分页查询未完成"):
            self.detector.verify_inventory(self.record)
        self.detector.save_data.assert_not_called()

    def test_later_episode_page_is_read(self):
        self.episodes["Items"][0]["IndexNumberEnd"] = 12
        self.episodes["TotalRecordCount"] = 2
        later = {"Items": [{"SeriesId": "series", "ParentIndexNumber": 5, "IndexNumber": 13}], "TotalRecordCount": 2}
        self.detector._request_json.side_effect = [self.views, self.series, self.episodes, later]
        self.assertEqual(self.detector.verify_inventory(self.record), [])
        self.assertIn("StartIndex=1", self.detector._request_json.call_args.args[0])

    def test_concurrent_detector_scan_refuses_verification(self):
        self.detector._scan_lock.acquire()
        with self.assertRaisesRegex(ValueError, "正在扫描"):
            self.detector.verify_inventory(self.record)
        self.detector._request_json.assert_not_called()

    def test_newly_missing_episodes_are_preserved(self):
        self.detector._results[0]["MissingEpisodeNumbers"].append(14)
        self.assertEqual(self.detector.verify_inventory(self.record), [])
        self.assertEqual(self.detector._results[0]["MissingEpisodeNumbers"], [14])


class MissingEpisodeCompletionTests(unittest.TestCase):
    def test_auto_cancel_is_disabled_by_default(self):
        self.assertFalse(DEFAULT_MISSING["auto_cancel_enabled"])
        self.assertEqual(DEFAULT_MISSING["auto_cancel_mode"], "ended_or_aired")

    def test_legacy_cancel_modes_migrate_without_changing_behavior(self):
        for ended, aired, mode in [(False, False, "ended_or_aired"), (True, False, "ended"),
                                   (False, True, "aired"), (True, True, "ended_or_aired")]:
            with self.subTest(ended=ended, aired=aired):
                old = {"auto_cancel_completed": ended, "auto_cancel_aired_season": aired}
                migrated = normalize_cancel_config(old)
                self.assertEqual(migrated, {"auto_cancel_enabled": ended or aired, "auto_cancel_mode": mode})
                self.assertEqual(normalize_cancel_config(migrated), migrated)
                self.assertIn("auto_cancel_completed", old)
                self.detector.configure(migrated)
                self.assertEqual(self.detector._auto_cancel_completed, ended)
                self.assertEqual(self.detector._auto_cancel_aired_season, aired)

    def test_master_off_overrides_legacy_flags_and_keeps_mode(self):
        config = normalize_cancel_config({"auto_cancel_enabled": False, "auto_cancel_mode": "ended_or_aired",
                                          "auto_cancel_completed": True, "auto_cancel_aired_season": True})
        self.detector.configure(config)
        self.assertFalse(self.detector._auto_cancel_completed)
        self.assertFalse(self.detector._auto_cancel_aired_season)
        self.assertEqual(self.process()[1], set())
        config["auto_cancel_enabled"] = True
        self.detector.configure(config)
        self.assertTrue(self.detector._auto_cancel_completed)
        self.assertTrue(self.detector._auto_cancel_aired_season)
        self.assertTrue(self.process(status="Returning Series")[1])

    def test_aired_season_guard_boundaries(self):
        check = MissingEpisodeDetector._aired_season_reason
        episodes = [{"episode_number": 1, "air_date": "2026-09-24"}]
        self.assertEqual(check({}, 1, 1, episodes, "2026-10-01"), "")
        for air_date in (None, "invalid", "2026-02-30", "2026-09-25", "2026-10-01", "2026-10-02"):
            with self.subTest(air_date=air_date):
                self.assertTrue(check({}, 1, 1, [{"episode_number": 1, "air_date": air_date}], "2026-10-01"))
        self.assertTrue(check({}, 0, 1, episodes, "2026-10-01"))
        self.assertTrue(check({}, 1, 2, episodes, "2026-10-01"))
        self.assertTrue(check({"next_episode_to_air": {"season_number": 1}}, 1, 1, episodes, "2026-10-01"))
        self.assertTrue(check({"next_episode_to_air": {"episode_number": 2}}, 1, 1, episodes, "2026-10-01"))
        self.assertEqual(check({"next_episode_to_air": {"season_number": 2}}, 1, 1, episodes, "2026-10-01"), "")

    def test_season_mode_still_requires_local_complete_and_full_tmdb_response(self):
        self.detector._auto_cancel_completed = False
        self.detector._auto_cancel_aired_season = True
        self.assertEqual(self.process(status="Returning Series", local={1})[1], set())
        self.assertEqual(self.process(status="Returning Series", episode_count=3)[1], set())

    def setUp(self):
        self.detector = object.__new__(MissingEpisodeDetector)
        self.detector._ignore_season_zero = True
        self.detector._only_existing_seasons = True
        self.detector._ignore_future = True
        self.detector._auto_cancel_completed = True
        self.detector._auto_cancel_aired_season = False
        self.detector._skip_series_ids = set()

    def process(self, status="Ended", local=None, episode_count=2):
        details = {"status": status, "name": "完结剧", "seasons": [
            {"season_number": 1, "episode_count": episode_count},
        ]}
        season = {"episodes": [
            {"episode_number": 1, "air_date": "2026-01-01"},
            {"episode_number": 2, "air_date": "2026-01-02"},
        ]}
        inventory = {"series-1": {1: local if local is not None else {1, 2}}}
        with patch.object(self.detector, "_request_json", side_effect=[details, season]):
            return self.detector._process_series(
                {"Id": "series-1", "Name": "完结剧", "ProviderIds": {"Tmdb": "123"}},
                inventory, "key", "tmdb.example", "2026-10-01", "Q4", "电视剧",
            )

    def test_ended_complete_season_is_cancel_candidate(self):
        missing, completed = self.process()
        self.assertEqual(missing, [])
        self.assertEqual(completed, {("123", 1, "完结剧")})

    def test_running_or_incomplete_season_is_not_cancel_candidate(self):
        self.assertEqual(self.process(status="Returning Series")[1], set())
        missing, completed = self.process(local={1})
        self.assertEqual(completed, set())
        self.assertEqual(missing[0]["MissingEpisodeNumbers"], [2])

    def test_aired_complete_season_can_cancel_returning_series(self):
        self.detector._auto_cancel_completed = False
        self.detector._auto_cancel_aired_season = True
        missing, completed = self.process(status="Returning Series")
        self.assertEqual(missing, [])
        self.assertEqual(completed, {("123", 1, "完结剧")})

    def test_aired_season_waits_for_final_episode_and_complete_tmdb_data(self):
        self.detector._auto_cancel_completed = False
        self.detector._auto_cancel_aired_season = True
        details = {"status": "Returning Series", "seasons": [{"season_number": 1, "episode_count": 2}],
                   "next_episode_to_air": {"season_number": 1, "episode_number": 3}}
        season = {"episodes": [{"episode_number": 1, "air_date": "2026-01-01"},
                                {"episode_number": 2, "air_date": "2026-10-01"}]}
        with patch.object(self.detector, "_request_json", side_effect=[details, season]):
            _, completed = self.detector._process_series(
                {"Id": "series-1", "Name": "连载剧", "ProviderIds": {"Tmdb": "123"}},
                {"series-1": {1: {1, 2}}}, "key", "tmdb.example", "2026-10-01", "Q4", "电视剧",
            )
        self.assertEqual(completed, set())

    def test_aired_season_requires_contiguous_tmdb_episode_numbers(self):
        self.detector._auto_cancel_completed = False
        self.detector._auto_cancel_aired_season = True
        details = {"status": "Returning Series", "seasons": [{"season_number": 1, "episode_count": 2}]}
        season = {"episodes": [{"episode_number": 1, "air_date": "2026-01-01"},
                                {"episode_number": 1, "air_date": "2026-01-02"}]}
        with patch.object(self.detector, "_request_json", side_effect=[details, season]):
            _, completed = self.detector._process_series(
                {"Id": "series-1", "Name": "重复集号", "ProviderIds": {"Tmdb": "123"}},
                {"series-1": {1: {1, 2}}}, "key", "tmdb.example", "2026-10-01", "Q4", "电视剧",
            )
        self.assertEqual(completed, set())

    def test_partial_tmdb_response_is_not_cancel_candidate(self):
        self.assertEqual(self.process(episode_count=3)[1], set())

    def test_episode_override_limits_missing_and_subscription_total(self):
        self.detector._episode_overrides = {("123", 1): {"tmdb_id": "123", "season": 1, "total_episodes": 1}}
        missing, completed = self.process(local={1}, episode_count=2)
        self.assertEqual(missing, [])
        self.assertEqual(completed, {("123", 1, "完结剧")})

    def test_invalid_episode_number_does_not_abort_scan(self):
        self.detector._episode_overrides = {}
        details = {"status": "Ended", "name": "异常剧", "seasons": [{"season_number": 1, "episode_count": 2}]}
        season = {"episodes": [{"episode_number": "未知", "air_date": "2026-01-01"},
                                {"episode_number": 1, "air_date": "2026-01-01"},
                                {"episode_number": 2, "air_date": "2026-01-02"}]}
        with patch.object(self.detector, "_request_json", side_effect=[details, season]):
            missing, completed = self.detector._process_series(
                {"Id": "series-1", "Name": "异常剧", "ProviderIds": {"Tmdb": "123"}},
                {"series-1": {1: {1}}}, "key", "tmdb.example", "2026-10-01", "Q4", "电视剧",
            )
        self.assertEqual(missing[0]["MissingEpisodeNumbers"], [2])
        self.assertEqual(completed, set())

    def test_auto_episode_correction_uses_matching_douban_count(self):
        self.detector._auto_episode_correction = True
        self.detector._douban_episode_total = MagicMock(return_value=1)
        missing, completed = self.process(local={1}, episode_count=2)
        self.assertEqual(missing, [])
        self.assertEqual(completed, {("123", 1, "完结剧")})
        self.detector._douban_episode_total.assert_called_once()

    def test_auto_episode_correction_trusts_reliably_matched_lower_douban_count(self):
        self.detector._auto_episode_correction = True
        self.detector._douban_episode_total = MagicMock(return_value=1)
        missing, _ = self.process(local=set(), episode_count=2)
        self.assertEqual(missing[0]["TotalEpisodes"], 1)
        self.assertEqual(missing[0]["TotalEpisodesSource"], "豆瓣匹配（下修）")

    def test_auto_episode_correction_keeps_tmdb_when_sources_disagree(self):
        self.detector._auto_episode_correction = True
        self.detector._douban_episode_total = MagicMock(return_value=3)
        missing, completed = self.process(local={1}, episode_count=2)
        self.assertEqual(missing[0]["MissingEpisodeNumbers"], [2])
        self.assertEqual(missing[0]["TotalEpisodes"], 2)
        self.assertIn("未上修", missing[0]["TotalEpisodesSource"])
        self.assertEqual(completed, set())

    def test_douban_episode_count_uses_season_specific_match_and_explicit_count(self):
        detector = object.__new__(MissingEpisodeDetector)
        detector._request_json = MagicMock(side_effect=[
            [{"id": "5555", "title": "半熟恋人第五季", "year": "2025"}],
            {"r": 0, "subject": {"is_tv": True}},
            {"episodes_count_str": "28集"},
        ])
        count = detector._douban_episode_total(
            {"Name": "半熟恋人", "ProductionYear": "2021",
             "ProviderIds": {"Douban": "series-douban-id"}}, 5, "2025",
        )
        self.assertEqual(count, 28)
        calls = [call.args[0] for call in detector._request_json.call_args_list]
        self.assertIn("q=%E5%8D%8A%E7%86%9F%E6%81%8B%E4%BA%BA%20%E7%AC%AC5%E5%AD%A3", calls[0])
        self.assertIn("subject_abstract?subject_id=5555", calls[1])
        self.assertIn("/j/subject/5555", calls[2])

    def test_douban_episode_count_reads_page_total_from_subject_abstract(self):
        detector = object.__new__(MissingEpisodeDetector)
        detector._request_json = MagicMock(return_value={
            "r": 0, "subject": {"episodes_count": "24", "is_tv": True, "subtype": "TV"},
        })
        count = detector._douban_episode_total(
            {"Name": "喜剧之王", "ProductionYear": "2026", "ProviderIds": {"Douban": "36868927"}},
            1, "2026",
        )
        self.assertEqual(count, 24)
        self.assertEqual(detector._request_json.call_count, 1)
        self.assertIn("subject_abstract?subject_id=36868927", detector._request_json.call_args.args[0])

    def test_douban_episode_count_uses_suggest_when_detail_has_no_total(self):
        detector = object.__new__(MissingEpisodeDetector)
        detector._request_json = MagicMock(side_effect=[
            {"r": 0, "subject": {"is_tv": False, "subtype": "MOVIE", "episodes_count": ""}},
            {"sid": "1302425", "title": "喜剧之王"},
            None,
            [
                {"id": "36868927", "title": "喜剧之王", "year": "2026", "type": "movie", "episode": "24"},
                {"id": "1302425", "title": "喜剧之王", "year": "1999", "type": "movie", "episode": ""},
            ],
            {"r": 0, "subject": {"episodes_count": "24", "is_tv": True, "subtype": "TV"}},
        ])
        count = detector._douban_episode_total(
            {"Name": "喜剧之王", "ProductionYear": "1999", "ProviderIds": {"Douban": "1302425"}},
            1, "2026",
        )
        self.assertEqual(count, 24)

    def test_douban_episode_count_ignores_unknown_suggest_episode(self):
        detector = object.__new__(MissingEpisodeDetector)
        detector._request_json = MagicMock(side_effect=[
            [{"id": "38554758", "title": "密室大逃脱大神版 第八季", "year": "2026", "episode": "unknow"}],
            {"r": 0, "subject": {"is_tv": True, "episodes_count": ""}},
            {"title": "密室大逃脱大神版 第八季"},
            None,
        ])
        count = detector._douban_episode_total(
            {"Name": "密室大逃脱 大神版", "ProductionYear": "2026"}, 8, "2026",
        )
        self.assertIsNone(count)

    def test_broadcast_status_uses_season_air_dates(self):
        today = "2026-10-09"
        airing = [{"episode_number": 1, "air_date": "2026-10-01"},
                  {"episode_number": 2, "air_date": "2026-10-20"}]
        self.assertEqual(MissingEpisodeDetector._broadcast_status({}, 10, airing, today, 2), "在播")
        upcoming = [{"episode_number": 1, "air_date": "2026-11-01"}]
        self.assertEqual(MissingEpisodeDetector._broadcast_status({}, 1, upcoming, today, 1), "待播")
        ended = [{"episode_number": index, "air_date": "2026-01-01"} for index in range(1, 11)]
        self.assertEqual(
            MissingEpisodeDetector._broadcast_status({"status": "Returning Series"}, 1, ended, today, 10),
            "完结",
        )
        partial = [{"episode_number": 1, "air_date": "2026-01-01"}, {"episode_number": 2}]
        self.assertEqual(MissingEpisodeDetector._broadcast_status({}, 1, partial, today, 10), "在播")

    def test_broadcast_status_uses_douban_progress_without_air_dates(self):
        detector = object.__new__(MissingEpisodeDetector)
        detector.plugin_name = "ME工具 缺集检测"
        detector._request_json = MagicMock(return_value={
            "is_tv": True, "episodes_count": "24", "episodes_info": "更新至12集", "is_released": True,
        })
        status = detector._season_airing_status(
            {"Name": "喜剧之王", "ProviderIds": {"Douban": "36868927"}},
            1, {}, [{"episode_number": 1}], "2026-10-09", 24, "2026",
        )
        self.assertEqual(status, "在播")
        self.assertIn("/tv/36868927", detector._request_json.call_args.args[0])

    def test_backfill_airing_status_fills_old_results_once(self):
        detector = object.__new__(MissingEpisodeDetector)
        detector.plugin_name = "ME工具 缺集检测"
        detector._is_scanning = False
        detector._results = [{"SeriesName": "花儿与少年", "Year": "2014", "TmdbId": "121876",
                              "SeasonNum": 8, "TotalEpisodes": 20}]
        detector.save_data = MagicMock()
        detector._request_json = MagicMock(side_effect=[
            {"name": "花儿与少年", "status": "Returning Series",
             "seasons": [{"season_number": 8, "air_date": "2026-08-01", "episode_count": 20}],
             "next_episode_to_air": {"season_number": 8, "air_date": "2026-10-14"}},
            {"episodes": [
                {"episode_number": 1, "air_date": "2026-08-01"},
                {"episode_number": 2, "air_date": "2026-10-14"},
            ]},
        ])
        with patch("emetools.missing_episodes.settings") as plugin_settings:
            plugin_settings.TMDB_API_KEY = "key"
            plugin_settings.TMDB_API_DOMAIN = "api.themoviedb.org"
            plugin_settings.TZ = "Asia/Shanghai"
            detector.backfill_airing_status()
            detector.backfill_airing_status()
        self.assertEqual(detector._results[0]["AiringStatus"], "在播")
        self.assertEqual(detector._request_json.call_count, 2)
        detector.save_data.assert_called_once()

    def test_auto_episode_correction_skips_animation_genre(self):


        detector = object.__new__(MissingEpisodeDetector)
        detector._auto_episode_correction = True
        detector._douban_episode_total = MagicMock(return_value=12)
        total, source = detector._auto_episode_total(
            {"Name": "动画剧"}, 1, 10, details={"genres": [{"id": 16, "name": "Animation"}]}
        )
        self.assertEqual(total, 10)
        self.assertIn("动画类型", source)
        detector._douban_episode_total.assert_not_called()

    def test_disabled_or_unknown_status_does_not_cancel(self):
        for status in (None, "Canceled", "Returning Series"):
            self.assertEqual(self.process(status=status)[1], set())
        self.detector._auto_cancel_completed = False
        self.assertEqual(self.process()[1], set())

    def test_no_candidates_does_not_read_or_delete_subscriptions(self):
        self.detector._subscribe_chain = MagicMock()
        self.assertEqual(self.detector._cancel_completed_subscriptions(set()), [])
        self.detector._subscribe_chain.subscription_repository.list.assert_not_called()

    def test_failed_deletion_is_not_reported_as_cancelled(self):
        chain = MagicMock()
        chain.subscription_repository.list.return_value = [
            SimpleNamespace(id=7, type=MediaType.TV.value, media_source=MediaSource.TMDB.value,
                            media_id="123", season="1"),
            SimpleNamespace(id=8, type=MediaType.TV.value, media_source=MediaSource.TMDB,
                            media_id="123", season=None),
        ]
        chain._delete_subscription.return_value = False
        self.detector._subscribe_chain = chain
        self.assertEqual(self.detector._cancel_completed_subscriptions({("123", 1, "完结剧")}), [])
        chain._delete_subscription.assert_called_once_with(7)

    def test_cancellation_matches_tmdb_id_and_season_only(self):
        subscriptions = [
            SimpleNamespace(id=7, type=MediaType.TV.value, media_source=MediaSource.TMDB,
                            media_id="123", season=1),
            SimpleNamespace(id=8, type=MediaType.TV.value, media_source=MediaSource.TMDB,
                            media_id="123", season=2),
            SimpleNamespace(id=9, type=MediaType.TV.value, media_source="thetvdb",
                            media_id="123", season=1),
        ]
        chain = MagicMock()
        chain.subscription_repository.list.return_value = subscriptions
        chain._delete_subscription.return_value = True
        self.detector._subscribe_chain = chain

        cancelled = self.detector._cancel_completed_subscriptions({("123", 1, "完结剧")})

        chain._delete_subscription.assert_called_once_with(7)
        self.assertEqual(cancelled, [{"id": 7, "name": "完结剧", "tmdb_id": "123", "season": 1}])

    def test_library_scan_collects_completed_candidates(self):
        series_payload = {"Items": [
            {"Id": "series-1", "Name": "完结剧", "ProviderIds": {"Tmdb": "123"}},
        ]}
        episode_payload = {"Items": [
            {"SeriesId": "series-1", "ParentIndexNumber": 1, "IndexNumber": 1},
        ]}
        candidate = {("123", 1, "完结剧")}
        with patch.object(self.detector, "_request_json",
                          side_effect=[series_payload, episode_payload]), \
             patch.object(self.detector, "_process_series",
                          return_value=([], candidate)):
            results, completed = self.detector._scan_library(
                "http://emby", "emby-key", "user", "Q4",
                {"Id": "library", "Name": "电视剧"},
                "tmdb-key", "tmdb.example", "2026-10-01",
            )
        self.assertEqual(results, [])
        self.assertEqual(completed, candidate)


if __name__ == "__main__":
    unittest.main()
