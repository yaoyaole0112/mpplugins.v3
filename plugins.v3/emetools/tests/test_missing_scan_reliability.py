import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from emetools.missing_episodes import DEFAULT_MISSING, MissingEpisodeDetector


class ScanReliabilityTests(unittest.TestCase):
    def test_tmdb_request_uses_proxy_and_retries_transient_failure(self):
        response = SimpleNamespace(status_code=200, json=lambda: {'seasons': []})
        with patch('emetools.missing_episodes.RequestUtils') as client, \
             patch('emetools.missing_episodes.time.sleep'), \
             patch('emetools.missing_episodes.settings', SimpleNamespace(
                 PROXY={'https': 'http://proxy'}, TMDB_API_DOMAIN='api.themoviedb.org')):
            client.return_value.get_res.side_effect = [None, response]
            result = self.detector._request_json('https://api.themoviedb.org/3/tv/1?api_key=test')
            self.assertEqual(result, {'seasons': []})
            self.assertEqual(client.call_count, 2)
            self.assertEqual(client.call_args.kwargs['proxies'], {'https': 'http://proxy'})

    def test_emby_request_does_not_use_tmdb_proxy(self):
        response = SimpleNamespace(status_code=200, json=lambda: {'Items': []})
        with patch('emetools.missing_episodes.RequestUtils') as client:
            client.return_value.get_res.return_value = response
            self.detector._request_json('http://emby/emby/Items')
            self.assertIsNone(client.call_args.kwargs['proxies'])

    def test_non_contiguous_tmdb_episode_numbers_are_compared_without_aborting(self):
        payloads = [{'seasons': [{'season_number': 1, 'episode_count': 2}]},
                    {'episodes': [{'episode_number': 1, 'air_date': '2026-01-01'},
                                  {'episode_number': 3, 'air_date': '2026-01-01'}]}]
        with patch.object(self.detector, '_request_json', side_effect=payloads):
            results, completed = self.detector._process_series(self.series[0], {'failed': {1: {1}}},
                'test', 'tmdb', '2026-10-13', 'Q4', '剧集')
        self.assertEqual(results[0]['MissingEpisodeNumbers'], [3])
        self.assertEqual(completed, set())

    def setUp(self):
        self.storage = {}
        self.detector = object.__new__(MissingEpisodeDetector)
        self.detector.owner = SimpleNamespace(get_data=self.storage.get,
            save_data=lambda key, value: self.storage.update({key: copy.deepcopy(value)}))
        self.detector._scan_lock = threading.Lock()
        self.detector._is_scanning = False
        self.detector._scan_status = {}
        self.detector._cancelled_subscriptions = []
        self.detector._last_scan_time = '上次成功扫描'
        self.detector.configure(DEFAULT_MISSING)
        self.records = [{'ServerName': 'Q4', 'LibraryName': '剧集', 'SeriesId': name,
            'SeriesName': name, 'TmdbId': number, 'SeasonNum': 1, 'SeasonFormatted': 'S1',
            'MissingEpisodeNumbers': [2], 'MissingEpisodes': '2'}
            for name, number in (('failed', '1'), ('complete', '2'))]
        self.detector._results = copy.deepcopy(self.records)
        self.service = SimpleNamespace(config=SimpleNamespace(name='Q4',
            config={'host': 'http://emby', 'apikey': 'test'}),
            instance=SimpleNamespace(get_user=lambda: 'user'))
        self.detector._mediaserver_helper = SimpleNamespace(get_services=lambda **kwargs: {'Q4': self.service})
        self.series = [{'Id': row['SeriesId'], 'Name': row['SeriesName'],
                        'ProviderIds': {'Tmdb': row['TmdbId']}} for row in self.records]

    def test_failed_series_retains_previous_while_successful_complete_series_is_removed(self):
        def scan(*args):
            if args[3]['Id'] == 'failed':
                raise ValueError('TMDB S01 查询失败')
            return [], set()
        with patch.object(self.detector, '_selected_libraries', return_value=[{'Id': 'lib', 'Name': '剧集'}]), \
             patch.object(self.detector, '_emby_items', return_value=self.series), \
             patch.object(self.detector, '_scan_series', side_effect=scan), \
             patch.object(self.detector, '_handle_missing') as handle, \
             patch('emetools.missing_episodes.settings.TMDB_API_KEY', 'test'):
            self.detector.scan_missing_episodes()
        self.assertEqual(len(self.detector._results), 1)
        self.assertEqual(self.detector._results[0]['SeriesId'], 'failed')
        self.assertTrue(self.detector._results[0]['PendingReview'])
        self.assertEqual(self.detector._results[0]['MissingEpisodeNumbers'], [2])
        self.assertFalse(self.detector._scan_status['complete'])
        self.assertEqual(self.detector._scan_status['failed_count'], 1)
        handle.assert_not_called()
        self.detector._load_saved_data()
        self.assertTrue(self.detector._results[0]['PendingReview'])
        self.assertFalse(self.detector._scan_status['complete'])

    def test_failed_library_listing_does_not_erase_results(self):
        with patch.object(self.detector, '_selected_libraries', side_effect=ValueError('Emby 查询失败')), \
             patch('emetools.missing_episodes.settings.TMDB_API_KEY', 'test'):
            self.detector.scan_missing_episodes()
        self.assertEqual(len(self.detector._results), 2)
        self.assertTrue(all(row['PendingReview'] for row in self.detector._results))
        self.assertEqual(self.detector._last_scan_time, '上次成功扫描')
        self.assertFalse(self.detector._scan_status['complete'])

    def test_partial_scan_updates_successful_series_without_acting_on_stale_rows(self):
        fresh = {**self.records[1], 'MissingEpisodes': '3', 'MissingEpisodeNumbers': [3]}
        def scan(*args):
            if args[3]['Id'] == 'failed':
                raise ValueError('TMDB 查询失败')
            return [fresh], set()
        with patch.object(self.detector, '_selected_libraries', return_value=[{'Id': 'lib', 'Name': '剧集'}]), \
             patch.object(self.detector, '_emby_items', return_value=self.series), \
             patch.object(self.detector, '_scan_series', side_effect=scan), \
             patch.object(self.detector, '_handle_missing') as handle, \
             patch('emetools.missing_episodes.settings.TMDB_API_KEY', 'test'):
            self.detector.scan_missing_episodes()
        self.assertEqual(len(self.detector._results), 2)
        handle.assert_called_once_with(fresh)
        self.assertEqual(self.detector._scan_status['retained_count'], 1)

    def test_empty_library_response_is_not_confirmation_of_no_missing_items(self):
        with patch.object(self.detector, '_selected_libraries', return_value=[]), \
             patch('emetools.missing_episodes.settings.TMDB_API_KEY', 'test'):
            self.detector.scan_missing_episodes()
        self.assertEqual(len(self.detector._results), 2)
        self.assertFalse(self.detector._scan_status['complete'])

    def test_successful_rescan_removes_stale_results_and_clears_pending_status(self):
        self.detector._results[0]['PendingReview'] = True
        with patch.object(self.detector, '_selected_libraries', return_value=[{'Id': 'lib', 'Name': '剧集'}]), \
             patch.object(self.detector, '_emby_items', return_value=self.series), \
             patch.object(self.detector, '_scan_series', return_value=([], set())), \
             patch('emetools.missing_episodes.settings.TMDB_API_KEY', 'test'):
            self.detector.scan_missing_episodes()
        self.assertEqual(self.detector._results, [])
        self.assertTrue(self.detector._scan_status['complete'])
        self.assertEqual(self.detector._scan_status['retained_count'], 0)

    def test_tmdb_failure_is_not_interpreted_as_no_missing_episodes(self):
        for payloads in ([None], [{'seasons': [{'season_number': 1, 'episode_count': 2}]}, None],
                         [{'seasons': [{'season_number': 1, 'episode_count': 2}]}, {'episodes': []}],
                         [{'seasons': []}],
                         [{'seasons': [{'season_number': 1, 'episode_count': 2}]}, {'episodes': []}]):
            with self.subTest(payloads=payloads), patch.object(self.detector, '_request_json', side_effect=payloads):
                with self.assertRaises(ValueError):
                    self.detector._process_series(self.series[0], {'failed': {1: {1}}},
                        'test', 'tmdb', '2026-10-13', 'Q4', '剧集')

    def test_first_scan_failure_is_visible_even_without_previous_record(self):
        self.detector._results = []
        self.detector._scan_issues = []
        results = []
        self.detector._retain_failed_series(results, 'Q4', '剧集', self.series[0], ValueError('查询失败'))
        self.assertEqual(results, [])
        self.assertEqual(self.detector._scan_issues[0]['series'], 'failed')

    def test_explicit_skip_does_not_resurrect_old_results(self):
        self.detector._skip_series_ids = {'1'}
        results = []
        self.detector._retain_failed_series(results, 'Q4', '剧集', self.series[0], ValueError('查询失败'))
        self.assertEqual(results, [])
