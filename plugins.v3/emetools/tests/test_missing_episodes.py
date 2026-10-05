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
