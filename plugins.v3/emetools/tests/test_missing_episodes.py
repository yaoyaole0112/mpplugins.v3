"""Regressions for completed-series subscription cleanup."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.schemas.types import MediaSource, MediaType

from emetools.missing_episodes import DEFAULT_MISSING, MissingEpisodeDetector


class MissingEpisodeCompletionTests(unittest.TestCase):
    def test_auto_cancel_is_disabled_by_default(self):
        self.assertFalse(DEFAULT_MISSING["auto_cancel_completed"])

    def setUp(self):
        self.detector = object.__new__(MissingEpisodeDetector)
        self.detector._ignore_season_zero = True
        self.detector._only_existing_seasons = True
        self.detector._ignore_future = True
        self.detector._auto_cancel_completed = True
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
