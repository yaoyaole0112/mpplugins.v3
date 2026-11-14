import copy
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

import test_channel_integration as integration
from brushflow import BrushFlow, BrushTaskConfig, TorrentInfo
from brushflow.channel_dedup import parse_candidate_file, parse_channel_message, parse_specs, same_release
from brushflow.models import (
    BrushTaskPayload, ChannelDedupSettingsPayload, ResourceLedgerDeletePayload, migrated_identity_policy,
)
from brushflow.resource_dedup import evidence_key, index_records


class ResourceDedupTests(unittest.TestCase):
    def setUp(self):
        integration.ChannelIntegrationTests.setUp(self)
        self.plugin._resource_ledger = {}
        self.plugin._resource_forgotten = {}
        self.plugin._identity_pending = {}
        self.plugin._channel_dedup_movie_identity_aliases = {}
        self.plugin._channel_dedup_state['records'] = []
        self.plugin._channel_dedup_attributes = ['resolution']
        self.plugin._channel_dedup_unknown_policy = 'exclude'
        self.plugin._channel_dedup_identity_policy = 'defer'
        self.task = BrushTaskConfig({
            'name': 'Test', 'downloader': 'QB', 'spec_dedup_enabled': True,
        })
        self.tv = 'Show.2026.(tmdbid=123).S01E01.1080p.H264.mkv'
        self.movie = 'Film.2026.(tmdbid=123).2160p.H265.mkv'

    def candidate(self, path=None, size=1000):
        return parse_candidate_file(path or self.tv, size, allow_movie=True)

    def select(self, paths=None, records=None):
        return self.plugin._channel_dedup_file_selection(
            integration.make_torrent(paths or [(self.tv, 1000)]), 'Show',
            task=self.task, records_override=records,
        )

    def test_unselected_fields_do_not_block_new_resolution(self):
        self.plugin._remember_resources([self.candidate()], 'completed')
        path = self.tv.replace('1080p.H264', '4K.H265.HDR10.60FPS')
        self.assertIsNone(self.select([(path, 5000)]))

    def test_global_pack_switch_only_disables_internal_duplicates(self):
        second = self.tv.replace('H264', 'H265')
        paths = [(self.tv, 1000), (second, 2000)]
        self.assertEqual(self.select(paths)['excluded'], {second})
        self.plugin._channel_dedup_pack_dedup_enabled = False
        self.assertIsNone(self.select(paths))
        self.plugin._remember_resources([self.candidate()], 'completed')
        self.assertEqual(self.select(paths)['excluded'], {self.tv, second})

    def test_global_inflight_off_neither_checks_nor_reserves_or_backfills(self):
        self.plugin._channel_dedup_inflight_enabled = False
        self.plugin._inflight_entries['existing'] = {
            'task_id': self.task.id, 'records': [self.candidate()], 'spec_dedup_enabled': True,
        }
        self.plugin._reconcile_inflight_entries = Mock()
        self.plugin._backfill_inflight_entries = Mock()
        content = integration.make_torrent([(self.tv, 1000)])
        with self.plugin._inflight_download_scope(
            content, TorrentInfo(title='Show'), self.task, 'a' * 40, None,
        ) as outcome:
            self.assertFalse(outcome['blocked'])
            outcome['added'] = True
        self.assertEqual(set(self.plugin._inflight_entries), {'existing'})
        self.assertFalse(self.plugin._inflight_entry_enabled(self.plugin._inflight_entries['existing']))
        self.plugin._reconcile_inflight_entries.assert_not_called()
        self.plugin._backfill_inflight_entries.assert_not_called()
        self.plugin._remember_resources([self.candidate()], 'completed')
        self.assertIsNotNone(self.select())

    def test_global_identity_policy_applies_to_all_tasks_not_legacy_task_values(self):
        path = 'Unknown.2026.S01E01.1080p.mkv'
        self.task.identity_policy = 'allow'
        self.assertEqual(self.select([(path, 1000)])['identity_pending_count'], 1)
        self.plugin._channel_dedup_identity_policy = 'allow'
        self.task.identity_policy = 'defer'
        self.assertIsNone(self.select([(path, 1000)]))
        self.task.spec_dedup_enabled = False
        self.plugin._channel_dedup_identity_policy = 'defer'
        self.assertFalse(self.plugin._resource_identity_deferred(self.task))

    def test_pack_policy_excludes_whole_torrent_only_for_resource_exclusions(self):
        self.plugin._channel_dedup_pack_policy = 'exclude'
        self.plugin._remember_resources([self.candidate()], 'completed')
        second = self.tv.replace('E01', 'E02')
        result = self.select([(self.tv, 1000), (second, 1000)])
        self.assertTrue(result['reject_torrent'])
        self.assertEqual(result['excluded'], {self.tv})
        self.assertIsNone(self.select([(second, 1000)]))
        self.assertIsNone(self.plugin._channel_dedup_file_selection(
            integration.make_torrent([(self.tv, 1000), (second, 1000)]),
            'Show', accepted_paths={second}, task=self.task,
        ))
        self.task.spec_dedup_enabled = False
        self.assertIsNone(self.select([(self.tv, 1000), (second, 1000)]))

    def test_pack_policy_blocks_add_and_inflight_reservation(self):
        downloader, _ = integration.ChannelIntegrationTests.prepare_download(self)
        self.plugin._channel_dedup_identity_policy = 'defer'
        self.plugin._channel_dedup_pack_policy = 'exclude'
        self.plugin._get_task_config = Mock(return_value=self.task)
        second = self.tv.replace('E01', 'E02')
        content = integration.make_torrent([(self.tv, 1000), (second, 1000)])
        self.plugin._remember_resources([self.candidate()], 'completed')
        with patch('brushflow.DownloadChain') as download:
            download.return_value.download_torrent.return_value = (content, None, None)
            self.assertIsNone(self.plugin._BrushFlow__download(self.torrent))
        downloader.add_torrent.assert_not_called()
        self.assertFalse(self.plugin._inflight_entries)
        with self.plugin._inflight_download_scope(
            content, self.torrent, self.task, 'b' * 40, None,
        ) as outcome:
            self.assertTrue(outcome['blocked'])
        self.assertFalse(self.plugin._inflight_entries)

    def test_partial_pack_policy_and_no_queue_still_select_only_remaining_files(self):
        downloader, selected = integration.ChannelIntegrationTests.prepare_download(self)
        self.plugin._channel_dedup_identity_policy = 'defer'
        self.plugin._channel_dedup_inflight_enabled = False
        self.plugin._get_task_config = Mock(return_value=self.task)
        second = self.tv.replace('E01', 'E02')
        content = integration.make_torrent([(self.tv, 1000), (second, 1000)])
        self.plugin._remember_resources([self.candidate()], 'completed')
        with patch('brushflow.DownloadChain') as download:
            download.return_value.download_torrent.return_value = (content, None, None)
            self.assertEqual(self.plugin._BrushFlow__download(self.torrent), 'a' * 40)
        downloader.add_torrent.assert_called_once()
        self.assertEqual(selected.call_args.args[2]['accepted'], {second})
        self.assertFalse(self.plugin._inflight_entries)

    def test_empty_attributes_disable_matching_without_discarding_ledger(self):
        self.plugin._channel_dedup_enabled = False
        self.plugin._channel_dedup_attributes = []
        self.plugin._remember_resources([self.candidate()], 'completed')
        self.assertIsNone(self.select())
        self.assertEqual(len(self.plugin._resource_ledger), 1)

    def test_specs_mode_keeps_different_resolution(self):
        self.task.spec_dedup_enabled = True
        self.plugin._remember_resources([self.candidate()], 'completed')
        self.assertIsNone(self.select([(self.tv.replace('1080p', '2160p'), 1000)]))
        self.assertIsNotNone(self.select())

    def test_specs_mode_uses_ledger_without_listener(self):
        self.task.spec_dedup_enabled = True
        self.plugin._channel_dedup_enabled = False
        self.plugin._remember_resources([self.candidate()], 'completed')
        self.assertIsNotNone(self.select())

    def complete(self, path):
        downloader = Mock()
        downloader.get_files.return_value = [
            dict(index=0, name=path, size=1000, priority=1, progress=1),
        ]
        torrent_hash = 'a' * 40
        rows = {torrent_hash: {'progress': 1, 'title': 'Show'}}
        with patch.object(BrushFlow, 'downloader', new_callable=PropertyMock, return_value=downloader), \
             patch.object(self.plugin, '_BrushFlow__get_hash', return_value=torrent_hash):
            self.plugin._record_completed_resources(self.task, [{'progress': 1}], rows, force=True)
        self.assertTrue(rows[torrent_hash]['resource_ledger_recorded'])

    def test_resolution_only_completed_version_sequence_for_tv_and_movie(self):
        self.task.spec_dedup_enabled = True
        self.plugin._channel_dedup_enabled = False
        for prefix in ('Show.2026.(tmdbid=123).S01E01', 'Film.2026.(tmdbid=123)'):
            with self.subTest(prefix=prefix):
                self.plugin._resource_ledger.clear()
                first = prefix + '.1080p.HDR10.H265.WEB-DL.mkv'
                second = prefix + '.1080p.SDR.H264.BluRay.60FPS.mkv'
                third = prefix + '.4K.SDR.H264.WEB-DL.mkv'
                fourth = prefix + '.UHD.HDR10.H265.BluRay.24FPS.mkv'
                self.assertIsNone(self.select([(first, 1000)]))
                self.complete(first)
                self.assertEqual(self.select([(second, 8000)])['excluded'], {second})
                self.assertIsNone(self.select([(third, 1000)]))
                self.complete(third)
                self.assertEqual(self.select([(fourth, 9000)])['excluded'], {fourth})
                self.assertEqual(len(self.plugin._resource_ledger), 2)
                if 'S01E01' in prefix:
                    self.assertIsNone(self.select([(second.replace('E01', 'E02'), 1000)]))

    def test_every_selected_field_ignores_all_unselected_fields(self):
        self.task.spec_dedup_enabled = True
        first = self.tv.replace('1080p.H264', '1080p.HDR10.H264.WEB-DL.24FPS')
        variants = {
            'resolution': ('1080p.SDR.H265.BluRay.60FPS', '2160p.SDR.H265.BluRay.60FPS'),
            'effects': ('2160p.HDR10.H265.BluRay.60FPS', '2160p.SDR.H265.BluRay.60FPS'),
            'codec': ('2160p.SDR.H264.BluRay.60FPS', '2160p.SDR.H265.BluRay.60FPS'),
            'source': ('2160p.SDR.H265.WEB-DL.60FPS', '2160p.SDR.H265.BluRay.60FPS'),
            'fps': ('2160p.SDR.H265.BluRay.24FPS', '2160p.SDR.H265.BluRay.60FPS'),
            'size': ('2160p.SDR.H265.BluRay.60FPS', '2160p.SDR.H265.BluRay.60FPS'),
        }
        for attribute, (same, changed) in variants.items():
            with self.subTest(attribute=attribute):
                self.plugin._resource_ledger.clear()
                self.plugin._channel_dedup_attributes = [attribute]
                self.plugin._remember_resources([self.candidate(first)], 'completed')
                same_path = self.tv.replace('1080p.H264', same)
                self.assertEqual(self.select([(same_path, 1000)])['excluded'], {same_path})
                changed_path = self.tv.replace('1080p.H264', changed)
                self.assertIsNone(self.select([(changed_path, 2000)]))

    def test_multiple_selected_fields_compare_the_combination_not_any_field(self):
        self.task.spec_dedup_enabled = True
        self.plugin._channel_dedup_attributes = ['resolution', 'effects']
        first = self.tv.replace('1080p.H264', '1080p.HDR10.H265')
        self.plugin._remember_resources([self.candidate(first)], 'completed')
        same = first.replace('H265', 'H264.BluRay.60FPS')
        self.assertEqual(self.select([(same, 7000)])['excluded'], {same})
        for different in (first.replace('HDR10', 'SDR'), first.replace('1080p', '2160p')):
            self.assertIsNone(self.select([(different, 1000)]))

    def test_resolution_only_pack_filters_known_bucket_and_retains_new_bucket_per_episode(self):
        self.task.spec_dedup_enabled = True
        self.plugin._remember_resources([self.candidate()], 'completed')
        paths = [
            self.tv.replace('H264', 'HDR10.H265'),
            self.tv.replace('1080p.H264', '4K.HDR10.H265'),
            self.tv.replace('1080p.H264', 'UHD.SDR.H264'),
            self.tv.replace('E01', 'E02'),
            self.tv.replace('E01', 'E02').replace('H264', 'HDR10.H265'),
        ]
        result = self.select([(path, 1000) for path in paths])
        self.assertEqual(result['excluded'], {paths[0], paths[2], paths[4]})
        self.assertEqual(result['remaining_paths'], {paths[1], paths[3]})

    def test_resolution_only_atomic_reservations_across_sites_and_downloaders(self):
        self.task.spec_dedup_enabled = True
        self.plugin._reconcile_inflight_entries = Mock()
        self.plugin._backfill_inflight_entries = Mock()
        self.plugin._refresh_inflight_identity_bindings = Mock()
        other = BrushTaskConfig({**self.task.to_dict(), 'name': 'Other Site', 'downloader': 'TR'})
        first = self.tv.replace('H264', 'HDR10.H265')
        for number, (path, blocked, task) in enumerate((
            (first, False, self.task),
            (self.tv.replace('H264', 'SDR.H264'), True, other),
            (self.tv.replace('1080p', '4K'), False, other),
            (self.tv.replace('1080p.H264', 'UHD.HDR10.H265'), True, self.task),
        ), 1):
            with self.plugin._inflight_download_scope(integration.make_torrent([(path, 1000)]),
                 TorrentInfo(title='Show'), task, f'{number:040x}', None) as outcome:
                self.assertEqual(outcome['blocked'], blocked)
                outcome['added'] = not blocked
        self.assertEqual(len(self.plugin._inflight_entries), 2)
        self.assertFalse(self.plugin._resource_ledger)

    def test_switch_off_never_filters_or_records_pending(self):
        self.plugin._remember_resources([self.candidate()], 'completed')
        self.task.spec_dedup_enabled = False
        self.assertIsNone(self.select())
        self.assertIsNone(self.select([('Unknown.S01E01.mkv', 1000)]))
        self.assertFalse(self.plugin._identity_pending)

    def test_switch_off_bypasses_channel_ledger_and_package_matching(self):
        self.plugin._channel_dedup_state['records'] = [self.candidate()]
        self.plugin._remember_resources([self.candidate()], 'completed')
        self.task.spec_dedup_enabled = False
        second = self.tv.replace('H264', 'HDR10.H265')
        self.assertIsNone(self.select([(self.tv, 1000), (second, 2000)]))
        with self.plugin._inflight_download_scope(integration.make_torrent([(self.tv, 1000)]),
             TorrentInfo(title='Show'), self.task, 'b' * 40, None) as outcome:
            self.assertFalse(outcome['blocked'])
            outcome['added'] = True
        self.assertFalse(self.plugin._inflight_entries)
        self.assertEqual(len(self.plugin._resource_ledger), 1)

    def test_switch_changes_queue_participation_by_task_id_even_after_rename(self):
        self.plugin._task_configs = {self.task.id: self.task}
        entry = {'task_id': self.task.id, 'task_name': 'Old Name', 'downloader': 'QB',
                 'spec_dedup_enabled': True}
        self.assertTrue(self.plugin._inflight_entry_enabled(entry))
        self.task.spec_dedup_enabled = False
        self.assertFalse(self.plugin._inflight_entry_enabled(entry))
        self.task.spec_dedup_enabled = True
        entry['spec_dedup_enabled'] = False
        self.assertTrue(self.plugin._inflight_entry_enabled(entry))

    def test_switch_off_legacy_queue_follows_current_task_setting(self):
        self.task.spec_dedup_enabled = False
        self.plugin._task_configs = {self.task.id: self.task}
        self.assertFalse(self.plugin._inflight_entry_enabled({
            'task_name': self.task.name, 'downloader': 'QB', 'purpose': 'share',
        }))

    def test_other_task_ignores_disabled_queue_then_respects_it_when_reenabled(self):
        self.task.spec_dedup_enabled = False
        other = BrushTaskConfig({'name': 'Other', 'downloader': 'TR', 'spec_dedup_enabled': True})
        self.plugin._task_configs = {self.task.id: self.task, other.id: other}
        self.plugin._reconcile_inflight_entries = Mock()
        self.plugin._backfill_inflight_entries = Mock()
        self.plugin._inflight_entries['old'] = {
            'task_id': self.task.id, 'task_name': self.task.name, 'downloader': 'QB',
            'spec_dedup_enabled': True, 'hash': 'c' * 40, 'records': [self.candidate()],
        }
        with patch('brushflow.DownloaderHelper') as helper:
            helper.return_value.get_service.return_value = SimpleNamespace(
                instance=SimpleNamespace(get_torrents=Mock(return_value=([], False))))
            for enabled in (False, True):
                self.task.spec_dedup_enabled = enabled
                with self.plugin._inflight_download_scope(integration.make_torrent([(self.tv, 1000)]),
                     TorrentInfo(title='Show'), other, 'a' * 40, None) as outcome:
                    self.assertEqual(outcome['blocked'], enabled)
        self.assertEqual(len(self.plugin._inflight_entries), 1)
        self.assertFalse(self.plugin._resource_ledger)

    def test_switch_off_does_not_bypass_recent_episode_selection(self):
        downloader, _ = integration.ChannelIntegrationTests.prepare_download(self)
        self.task.spec_dedup_enabled = False
        self.task.recent_episodes_only = True
        self.plugin._get_task_config = Mock(return_value=self.task)
        self.plugin._recent_episode_file_selection = Mock(return_value=None)
        self.assertIsNone(self.plugin._BrushFlow__download(self.torrent))
        self.plugin._recent_episode_file_selection.assert_called_once()
        downloader.add_torrent.assert_not_called()

    def test_switch_off_download_bypasses_strict_pack_and_queue_rules(self):
        downloader, selected = integration.ChannelIntegrationTests.prepare_download(self)
        self.task.spec_dedup_enabled = False
        self.plugin._channel_dedup_identity_policy = 'defer'
        self.plugin._channel_dedup_pack_policy = 'exclude'
        self.plugin._channel_dedup_unknown_policy = 'exclude'
        self.plugin._get_task_config = Mock(return_value=self.task)
        self.plugin._remember_resources([self.candidate()], 'completed')
        self.assertEqual(self.plugin._BrushFlow__download(self.torrent), 'a' * 40)
        downloader.add_torrent.assert_called_once()
        selected.assert_not_called()
        self.assertFalse(self.plugin._inflight_entries)
        self.assertFalse(self.plugin._identity_pending)
        self.assertEqual(len(self.plugin._resource_ledger), 1)

    def test_allow_identity_never_creates_new_pending_even_on_completed_backfill(self):
        self.plugin._channel_dedup_identity_policy = 'allow'
        unknown = self.candidate('Unknown.2026.S01E01.1080p.mkv')
        self.plugin._pending_identity(unknown, 'Unknown', self.task, 'unverified')
        self.assertFalse(self.plugin._identity_pending)
        self.plugin._channel_dedup_identity_policy = 'defer'
        self.task.spec_dedup_enabled = False
        self.plugin._pending_identity(unknown, 'Unknown', self.task, 'unverified')
        self.assertFalse(self.plugin._identity_pending)

    def test_task_update_preserves_switch_when_omitted_and_other_selection_rules(self):
        self.task.spec_dedup_enabled = False
        self.task.recent_episodes_only = True
        self.task.include = 'KEEP'
        self.task.qb_category = '分享'
        self.plugin._task_configs = {self.task.id: self.task}
        self.plugin._is_task_busy = Mock(return_value=False)
        self.plugin._validate_task_reference = Mock(return_value=True)
        self.plugin._save_config = Mock()
        self.plugin._refresh_scheduler = Mock()
        self.plugin._build_task_detail = Mock(return_value={})
        for switch in (None, True, False):
            extra = {} if switch is None else {'spec_dedup_enabled': switch}
            result = self.plugin.update_task(self.task.id, BrushTaskPayload(
                name='Renamed', site_id=1, downloader='QB', **extra))
            self.assertTrue(result.success)
            saved = self.plugin._task_configs[self.task.id]
            self.assertEqual(saved.spec_dedup_enabled, switch is True)
            self.assertTrue(saved.recent_episodes_only)
            self.assertEqual(saved.include, 'KEEP')
            self.assertEqual(saved.qb_category, '分享')

    def test_different_season_episode_and_explicit_id_not_merged(self):
        self.plugin._remember_resources([self.candidate()], 'completed')
        for path in (self.tv.replace('S01', 'S02'), self.tv.replace('E01', 'E02'),
                     self.tv.replace('id=123', 'id=999')):
            with self.subTest(path=path):
                self.assertIsNone(self.select([(path, 1000)]))

    def test_movie_and_tv_id_namespaces_are_separate(self):
        self.plugin._remember_resources([self.candidate()], 'completed')
        self.assertIsNone(self.select([(self.movie, 1000)]))

    def test_movie_different_resolution_is_retained(self):
        self.plugin._remember_resources([self.candidate(self.movie)], 'completed')
        path = self.movie.replace('2160p', '1080p')
        self.assertIsNone(self.select([(path, 1000)]))

    def test_pack_keeps_distinct_resolutions(self):
        second = self.tv.replace('1080p', '2160p')
        self.assertIsNone(self.select([(self.tv, 1000), (second, 2000)]))

    def test_pack_specs_mode_keeps_two_resolutions_but_not_same_specs(self):
        self.task.spec_dedup_enabled = True
        second = self.tv.replace('H264', 'H265')
        third = self.tv.replace('1080p', '2160p')
        result = self.select([(self.tv, 1000), (second, 2000), (third, 3000)])
        self.assertEqual(result['excluded'], {second})
        self.assertIn(third, result['remaining_paths'])

    def test_movie_pack_keeps_distinct_resolutions(self):
        second = self.movie.replace('2160p', '1080p')
        self.assertIsNone(self.select([(self.movie, 1000), (second, 2000)]))

    def test_pack_duplicate_in_another_show_does_not_match(self):
        other = self.tv.replace('id=123', 'id=124')
        self.assertIsNone(self.select([(self.tv, 1000), (other, 1000)]))

    def test_ledger_survives_channel_expiry_and_queue_removal(self):
        record = self.candidate()
        record['published_at'] = time.time() - 100 * 3600
        self.plugin._remember_resources([record], 'channel')
        self.plugin._inflight_entries.clear()
        self.plugin._channel_dedup_state['records'] = []
        self.assertIsNotNone(self.select())
        self.assertEqual(len(self.plugin._resource_ledger), 1)

    def test_ledger_is_idempotent_and_persisted(self):
        self.plugin._remember_resources([self.candidate()], 'completed')
        writes = self.plugin.save_data.call_count
        self.plugin._remember_resources([self.candidate()], 'completed')
        self.assertEqual(self.plugin.save_data.call_count, writes)
        saved = copy.deepcopy(self.plugin._resource_ledger)
        self.plugin._resource_ledger = saved
        self.assertIsNotNone(self.select())

    def test_ledger_write_failure_does_not_publish_memory_evidence(self):
        self.plugin.save_data.side_effect = RuntimeError('disk')
        with self.assertRaises(RuntimeError):
            self.plugin._remember_resources([self.candidate()], 'completed')
        self.assertFalse(self.plugin._resource_ledger)

    def test_forget_does_not_reimport_same_evidence(self):
        record = self.candidate()
        self.plugin._remember_resources([record], 'completed')
        key = next(iter(self.plugin._resource_ledger))
        self.plugin.forget_resource(ResourceLedgerDeletePayload(key=key))
        self.plugin._remember_resources([record], 'channel')
        self.assertFalse(self.plugin._resource_ledger)

    def test_ledger_endpoint_is_bounded_and_supports_pending(self):
        self.plugin._remember_resources([self.candidate()], 'completed')
        self.assertEqual(self.plugin.get_resource_ledger().data['total'], 1)
        self.assertEqual(self.plugin.get_resource_ledger(page=2).data['items'], [])
        self.assertEqual(self.plugin.get_resource_ledger(state='pending').data['total'], 0)

    def test_unknown_identity_deferred_without_channel_match(self):
        path = 'Unknown.2026.S01E01.1080p.mkv'
        result = self.select([(path, 1000)])
        self.assertEqual(result['identity_pending_count'], 1)
        self.assertEqual(result['matched_video_count'], 0)
        self.assertEqual(len(self.plugin._identity_pending), 1)

    def test_video_missing_selected_fields_is_excluded_without_claiming_duplicate(self):
        result = self.select([('unrecognizable.mkv', 1000)])
        self.assertEqual(result['missing_spec_count'], 1)
        self.assertEqual(result['matched_video_count'], 0)

    def test_strict_share_does_not_add_magnet_without_file_metadata(self):
        downloader, _ = integration.ChannelIntegrationTests.prepare_download(self)
        self.plugin._get_task_config = Mock(return_value=self.task)
        self.torrent.enclosure = 'magnet:?xt=urn:btih:' + 'a' * 40
        self.assertIsNone(self.plugin._BrushFlow__download(self.torrent))
        downloader.add_torrent.assert_not_called()
        self.assertEqual(len(self.plugin._identity_pending), 1)

    def test_legacy_allow_does_not_defer(self):
        self.plugin._channel_dedup_identity_policy = 'allow'
        self.assertIsNone(self.select([('Unknown.2026.S01E01.1080p.mkv', 1000)]))

    def test_binding_resolves_pending_and_clears_it(self):
        path = 'Short.2026.S01E01.1080p.mkv'
        self.select([(path, 1000)])
        self.plugin._channel_dedup_identity_aliases = {'Short': 123}
        self.assertIsNone(self.select([(path, 1000)]))
        self.assertFalse(self.plugin._identity_pending)

    def test_movie_binding_does_not_use_tv_binding(self):
        path = 'Film.2026.1080p.mkv'
        self.plugin._channel_dedup_identity_aliases = {'Film': 123}
        self.assertEqual(self.select([(path, 1000)])['identity_pending_count'], 1)
        self.plugin._channel_dedup_movie_identity_aliases = {'Film': 999}
        self.assertIsNone(self.select([(path, 1000)]))

    def test_movie_recognition_requires_exact_title_type_and_year(self):
        candidate = self.candidate('Film.2026.1080p.mkv')
        for media in (SimpleNamespace(title='Other', type='电影', tmdb_id=123, year=2026),
                      SimpleNamespace(title='Film', type='电视剧', tmdb_id=123, year=2026),
                      SimpleNamespace(title='Film', type='电影', tmdb_id=123, year=2025)):
            self.plugin._channel_identity_cache.clear()
            self.plugin.chain.recognize_media.return_value = media
            self.assertFalse(self.plugin._verify_resource_identity(copy.deepcopy(candidate), '', self.task))
        self.plugin._channel_identity_cache.clear()
        self.plugin.chain.recognize_media.return_value = SimpleNamespace(title='Film', type='电影', tmdb_id=123, year=2026)
        self.assertTrue(self.plugin._verify_resource_identity(candidate, '', self.task))

    def test_chinese_conflict_not_overridden_by_english_movie_alias(self):
        candidate = self.candidate('[错误中文].Film.2026.1080p.mkv')
        self.plugin.chain.recognize_media.return_value = SimpleNamespace(
            title='正确中文', en_title='Film', type='电影', tmdb_id=123, year=2026)
        self.assertFalse(self.plugin._verify_resource_identity(candidate, '', self.task))

    def test_multiepisode_file_requires_all_episodes(self):
        self.plugin._remember_resources([self.candidate()], 'completed')
        path = self.tv.replace('E01', 'E01-E02')
        self.assertIsNone(self.select([(path, 1000)]))
        self.plugin._remember_resources([self.candidate(self.tv.replace('E01', 'E02'))], 'completed')
        self.assertIsNotNone(self.select([(path, 1000)]))

    def test_multiepisode_combines_external_and_package_evidence(self):
        self.plugin._remember_resources([self.candidate()], 'completed')
        second = self.tv.replace('E01', 'E02')
        combined = self.tv.replace('E01', 'E01-E02')
        result = self.select([(second, 1000), (combined, 2000)])
        self.assertEqual(result['excluded'], {combined})

    def test_unknown_specs_allow_does_not_bypass_strict_identity(self):
        self.task.spec_dedup_enabled = True
        self.plugin._channel_dedup_attributes = ['effects']
        self.plugin._channel_dedup_unknown_policy = 'allow'
        path = 'Unknown.2026.S01E01.1080p.H265.mkv'
        self.assertEqual(self.select([(path, 1000)])['identity_pending_count'], 1)

    def test_episodic_file_without_episode_not_misclassified_as_movie(self):
        self.assertIsNone(parse_candidate_file('01.mkv', 1000, 'Show.2026.S01.(tmdbid=123)', allow_movie=True))

    def test_ledger_alias_changes_rebind_and_removal_invalidates_evidence(self):
        self.plugin._channel_dedup_identity_aliases = {'Short': 123}
        record = self.candidate('Short.2026.S01E01.1080p.mkv')
        self.assertTrue(self.plugin._verify_resource_identity(record, '', self.task))
        self.plugin._remember_resources([record], 'completed')
        self.plugin._channel_dedup_identity_aliases = {'Short': 999}
        self.plugin._refresh_inflight_identity_bindings()
        saved = next(iter(self.plugin._resource_ledger.values()))
        self.assertEqual(saved['tmdb_id'], 999)
        self.assertEqual(next(iter(self.plugin._resource_ledger)), evidence_key(saved))
        self.plugin._channel_dedup_identity_aliases = {}
        self.plugin._refresh_inflight_identity_bindings()
        saved = next(iter(self.plugin._resource_ledger.values()))
        self.assertTrue(saved['identity_invalidated'])
        self.assertFalse(same_release(record, saved, [], mode='episode'))
        self.plugin._channel_dedup_identity_aliases = {'Short': 123}
        self.plugin._refresh_inflight_identity_bindings()
        self.assertFalse(next(iter(self.plugin._resource_ledger.values()))['identity_invalidated'])

    def test_ledger_explicit_identity_is_not_rewritten_by_alias(self):
        self.plugin._remember_resources([self.candidate()], 'completed')
        self.plugin._channel_dedup_identity_aliases = {'Show': 999}
        self.plugin._refresh_inflight_identity_bindings()
        self.assertEqual(next(iter(self.plugin._resource_ledger.values()))['tmdb_id'], 123)

    def test_channel_movie_message_parses_without_episode(self):
        record = parse_channel_message('@test', 1, time.time(),
            '影视：Film\n类型：电影\n年份：2026\nID：123\n质量：4K H265')
        self.assertEqual((record['media_type'], record['tmdb_id']), ('movie', 123))
        self.assertEqual(record['specs']['resolution'], '2160p')
        self.assertTrue(same_release(self.candidate(self.movie), record, [], mode='episode'))

    def test_resolution_nfkc_and_codec_normalization(self):
        self.assertEqual(parse_specs('４Ｋ HEVC')['resolution'], '2160p')
        self.assertEqual(parse_specs('UHD H.265')['codec'], 'H.265')
        self.assertEqual(parse_specs('FHD AVC')['resolution'], '1080p')
        self.assertEqual(parse_specs('HDR Vivid')['effects'], 'HDR Vivid')
        record = self.candidate('Ｓｈｏｗ．２０２６．(tmdbid=123)．Ｓ０１Ｅ０１．４Ｋ．ｍｋｖ')
        self.assertEqual((record['season'], record['episodes'], record['tmdb_id']), (1, [1], 123))

    def test_legacy_movie_queue_identity_uses_only_movie_bindings(self):
        self.plugin._channel_dedup_identity_aliases = {'Film': 123}
        candidate = self.candidate('Film.2026.1080p.mkv')
        self.plugin._cache_inflight_file_identity(candidate, '')
        self.assertIsNone(candidate.get('tmdb_id'))
        self.plugin._channel_dedup_movie_identity_aliases = {'Film': 999}
        self.plugin._cache_inflight_file_identity(candidate, '')
        self.assertEqual(candidate['tmdb_id'], 999)

    def test_completed_files_only_are_recorded(self):
        downloader = Mock()
        downloader.get_files.return_value = [
            dict(index=0, name=self.tv, size=1000, priority=1, progress=1),
            dict(index=1, name=self.tv.replace('E01', 'E02'), size=1000, priority=1, progress=0.5),
            dict(index=2, name=self.tv.replace('E01', 'E03'), size=1000, priority=0, progress=1),
        ]
        torrent = {'hash': 'a' * 40, 'progress': 0.5}
        rows = {'a' * 40: {'progress': 0.5, 'title': 'Show'}}
        with patch.object(BrushFlow, 'downloader', new_callable=PropertyMock, return_value=downloader), \
             patch.object(self.plugin, '_BrushFlow__get_hash', return_value='a' * 40):
            self.plugin._record_completed_resources(self.task, [torrent], rows)
            self.assertFalse(self.plugin._resource_ledger)
            self.plugin._record_completed_resources(self.task, [torrent], rows, force=True)
        records = list(self.plugin._resource_ledger.values())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['episodes'], [1])

    def test_switch_off_completion_does_not_enter_ledger(self):
        self.task.spec_dedup_enabled = False
        self.plugin._record_completed_resources(self.task, [{'hash': 'a'}], {}, force=True)
        self.assertFalse(self.plugin._resource_ledger)

    def test_completed_package_retries_until_every_wanted_video_is_verified(self):
        unknown = 'Short.2026.S01E02.1080p.mkv'
        downloader = Mock()
        downloader.get_files.return_value = [
            dict(index=0, name=self.tv, size=1000, priority=1, progress=1),
            dict(index=1, name=unknown, size=1000, priority=1, progress=1),
        ]
        torrent = {'hash': 'a' * 40, 'progress': 1}
        rows = {'a' * 40: {'progress': 1, 'title': 'Show'}}
        with patch.object(BrushFlow, 'downloader', new_callable=PropertyMock, return_value=downloader), \
             patch.object(self.plugin, '_BrushFlow__get_hash', return_value='a' * 40):
            self.plugin._record_completed_resources(self.task, [torrent], rows)
            self.assertFalse(rows['a' * 40].get('resource_ledger_recorded'))
            self.assertEqual(len(self.plugin._resource_ledger), 1)
            self.assertEqual(len(self.plugin._identity_pending), 1)
            self.plugin._channel_dedup_identity_aliases = {'Short': 123}
            self.plugin._record_completed_resources(self.task, [torrent], rows)
        self.assertTrue(rows['a' * 40]['resource_ledger_recorded'])
        self.assertEqual(len(self.plugin._resource_ledger), 2)
        self.assertFalse(self.plugin._identity_pending)

    def test_task_payload_roundtrip_and_invalid_switch(self):
        default = BrushTaskPayload(name='Share', site_id=1, downloader='QB')
        self.assertTrue(default.spec_dedup_enabled)
        payload = BrushTaskPayload(name='Share', site_id=1, downloader='QB', spec_dedup_enabled=False)
        task = BrushTaskConfig(payload.model_dump())
        self.assertNotIn('identity_policy', task.to_dict())
        self.assertFalse(task.spec_dedup_enabled)
        self.assertNotIn('purpose', task.to_dict())
        self.assertNotIn('resource_dedup_mode', task.to_dict())
        with self.assertRaises(ValueError):
            BrushTaskPayload(name='Share', site_id=1, downloader='QB', spec_dedup_enabled='invalid')
        payload = ChannelDedupSettingsPayload(movie_identity_aliases='Film = 123')
        self.assertEqual(payload.movie_identity_aliases, {'Film': 123})

    def test_atomic_spec_reservation_across_tasks(self):
        self.plugin._reconcile_inflight_entries = Mock()
        self.plugin._backfill_inflight_entries = Mock()
        self.plugin._refresh_inflight_identity_bindings = Mock()
        torrent = TorrentInfo(title='Show', description='')
        content = integration.make_torrent([(self.tv, 1000)])
        with self.plugin._inflight_download_scope(content, torrent, self.task, 'a' * 40, None) as first:
            self.assertFalse(first['blocked'])
            with self.plugin._inflight_download_scope(content, torrent, self.task, 'b' * 40, None) as second:
                self.assertTrue(second['blocked'])
            first['added'] = True
        self.assertFalse(self.plugin._resource_ledger)

    def test_legacy_traffic_entries_do_not_block_enabled_task(self):
        self.plugin._reconcile_inflight_entries = Mock()
        self.plugin._backfill_inflight_entries = Mock()
        self.plugin._refresh_inflight_identity_bindings = Mock()
        self.plugin._inflight_entries['old'] = {'purpose': 'traffic', 'downloader': 'QB', 'hash': 'c' * 40,
                                                'records': [self.candidate()]}
        with self.plugin._inflight_download_scope(integration.make_torrent([(self.tv, 1000)]),
             TorrentInfo(title='Show'), self.task, 'a' * 40, None) as outcome:
            self.assertFalse(outcome['blocked'])
            outcome['added'] = True

    def test_atomic_check_combines_ledger_and_queued_episode_coverage(self):
        self.plugin._reconcile_inflight_entries = Mock()
        self.plugin._backfill_inflight_entries = Mock()
        self.plugin._remember_resources([self.candidate()], 'completed')
        second = self.candidate(self.tv.replace('E01', 'E02'))
        self.plugin._inflight_entries['old'] = {'purpose': 'share', 'downloader': 'QB', 'hash': 'c' * 40,
                                                'records': [second]}
        combined = self.tv.replace('E01', 'E01-E02')
        with self.plugin._inflight_download_scope(integration.make_torrent([(combined, 2000)]),
             TorrentInfo(title='Show'), self.task, 'a' * 40, None) as outcome:
            self.assertTrue(outcome['blocked'])


class ResourceMigrationTests(unittest.TestCase):
    def test_global_policy_migration_prefers_explicit_then_unanimous_then_strict(self):
        self.assertEqual(migrated_identity_policy({}), 'defer')
        for policies, expected in ((['allow'], 'allow'), (['defer'], 'defer'),
                                   (['allow', 'defer'], 'defer')):
            tasks = [{'spec_dedup_enabled': True, 'identity_policy': policy} for policy in policies]
            tasks.append({'spec_dedup_enabled': False, 'identity_policy': 'defer'})
            self.assertEqual(migrated_identity_policy({'tasks': tasks}), expected)
            self.assertEqual(migrated_identity_policy({
                'tasks': tasks, 'channel_dedup_identity_policy': 'allow',
            }), 'allow')

    def test_new_global_settings_validate_and_old_task_field_is_not_exported(self):
        settings = ChannelDedupSettingsPayload(
            inflight_enabled=False, pack_dedup_enabled=False, pack_policy='exclude', identity_policy='allow',
        )
        self.assertFalse(settings.inflight_enabled)
        self.assertFalse(settings.pack_dedup_enabled)
        self.assertEqual(settings.pack_policy, 'exclude')
        for field in ('pack_policy', 'identity_policy'):
            with self.assertRaises(ValueError):
                ChannelDedupSettingsPayload(**{field: 'invalid'})
        task = BrushTaskPayload(name='Test', site_id=1, downloader='QB', identity_policy='allow')
        self.assertNotIn('identity_policy', task.model_dump())

    def test_global_settings_roundtrip_preserves_tasks_and_ledger(self):
        plugin = BrushFlow.__new__(BrushFlow)
        plugin.get_data = Mock(return_value=None)
        plugin.save_data = Mock()
        plugin.update_config = Mock()
        plugin.init_plugin({'enabled': False, 'tasks': [], 'channel_dedup_identity_policy': 'allow'})
        plugin._save_config = Mock()
        plugin._refresh_scheduler = Mock()
        before = copy.deepcopy(plugin._resource_ledger)
        for enabled in (False, True):
            payload = ChannelDedupSettingsPayload(
                inflight_enabled=enabled, pack_dedup_enabled=enabled, pack_policy='exclude',
                identity_policy='defer', attributes=['resolution'],
            )
            result = plugin.update_channel_dedup_settings(payload)
            self.assertTrue(result.success)
            config = plugin._current_config()
            self.assertEqual(config['channel_dedup_inflight_enabled'], enabled)
            self.assertEqual(result.data['pack_dedup_enabled'], enabled)
            self.assertEqual(result.data['pack_policy'], 'exclude')
            self.assertEqual(config['channel_dedup_identity_policy'], 'defer')
            self.assertEqual(plugin._resource_ledger, before)
        plugin.update_channel_dedup_settings(ChannelDedupSettingsPayload(attributes=['resolution']))
        self.assertEqual(plugin._channel_dedup_pack_policy, 'exclude')
        self.assertEqual(plugin._channel_dedup_identity_policy, 'defer')

    def test_upgrade_creates_switch_and_keeps_old_limits(self):
        plugin = BrushFlow.__new__(BrushFlow)
        data = {'task_data_schema_version': 2}
        plugin.get_data = Mock(side_effect=lambda key: copy.deepcopy(data.get(key)))
        plugin.save_data = Mock()
        plugin.update_config = Mock()
        plugin._validate_task_reference = Mock(return_value=True)
        plugin._migrate_torrent_identity_data = Mock()
        config = {'schema_version': 2, 'enabled': False, 'channel_dedup_channels': [], 'tasks': [
            {'id': 'share', 'name': '【分享】A', 'downloader': 'QB', 'tag': 'A', 'maxdlcount': 17},
            {'id': 'traffic', 'name': 'B', 'downloader': 'QB', 'tag': 'B'},
            {'id': 'category', 'name': 'C', 'qb_category': '分享', 'downloader': 'QB', 'tag': 'C'},
        ]}
        original = copy.deepcopy(config)
        plugin.init_plugin(config)
        saved = plugin.update_config.call_args.args[0]
        self.assertEqual(config, original)
        self.assertEqual(saved['schema_version'], 6)
        tasks = {row['id']: row for row in saved['tasks']}
        self.assertTrue(tasks['share']['spec_dedup_enabled'])
        self.assertEqual(saved['channel_dedup_identity_policy'], 'defer')
        self.assertNotIn('identity_policy', tasks['share'])
        self.assertEqual(tasks['share']['maxdlcount'], 17)
        self.assertFalse(tasks['traffic']['spec_dedup_enabled'])
        self.assertTrue(tasks['category']['spec_dedup_enabled'])
        self.assertNotIn('purpose', tasks['share'])
        self.assertNotIn('resource_dedup_mode', tasks['share'])

    def test_explicit_switch_is_preserved_on_reload_and_beats_legacy_fields(self):
        task = BrushTaskConfig({'name': 'Share', 'spec_dedup_enabled': False, 'purpose': 'share',
                                'resource_dedup_mode': 'episode', 'identity_policy': 'allow'})
        self.assertFalse(BrushTaskConfig(task.to_dict()).spec_dedup_enabled)
        self.assertNotIn('identity_policy', task.to_dict())

    def test_legacy_api_fields_migrate_without_exporting_modes(self):
        for old, expected in (({'purpose': 'traffic'}, False),
                              ({'purpose': 'share', 'resource_dedup_mode': 'off'}, False),
                              ({'purpose': 'share', 'resource_dedup_mode': 'episode'}, True),
                              ({'purpose': 'traffic', 'spec_dedup_enabled': True}, True)):
            with self.subTest(old=old):
                payload = BrushTaskPayload(name='Test', site_id=1, downloader='QB', **old)
                self.assertEqual(payload.spec_dedup_enabled, expected)
                self.assertNotIn('purpose', payload.model_dump())
                self.assertNotIn('resource_dedup_mode', payload.model_dump())

    def test_old_modes_migrate_to_switch_and_explicit_settings_beat_name(self):
        for schema in (3, 4, 5):
            with self.subTest(schema=schema):
                plugin = BrushFlow.__new__(BrushFlow)
                plugin.get_data = Mock(side_effect=lambda key: 2 if key == 'task_data_schema_version' else None)
                plugin.save_data = Mock()
                plugin.update_config = Mock()
                plugin._validate_task_reference = Mock(return_value=True)
                plugin._migrate_torrent_identity_data = Mock()
                config = {'schema_version': schema, 'enabled': False, 'tasks': [
                    {'id': 'share', 'name': 'Pure Traffic Name', 'purpose': 'share',
                     'resource_dedup_mode': 'episode', 'tag': 'A', 'downloader': 'QB'},
                    {'id': 'traffic', 'name': '【分享】Name', 'purpose': 'traffic',
                     'resource_dedup_mode': 'off', 'tag': 'B', 'downloader': 'QB'},
                    {'id': 'off', 'name': 'Off', 'purpose': 'share',
                     'resource_dedup_mode': 'off', 'tag': 'C', 'downloader': 'QB'},
                    {'id': 'explicit', 'name': '【分享】Explicit Off', 'purpose': 'share',
                     'spec_dedup_enabled': False, 'resource_dedup_mode': 'episode', 'tag': 'D', 'downloader': 'QB'},
                ]}
                original = copy.deepcopy(config)
                plugin.init_plugin(config)
                self.assertEqual(config, original)
                self.assertTrue(plugin._task_configs['share'].spec_dedup_enabled)
                self.assertFalse(plugin._task_configs['traffic'].spec_dedup_enabled)
                self.assertFalse(plugin._task_configs['off'].spec_dedup_enabled)
                self.assertFalse(plugin._task_configs['explicit'].spec_dedup_enabled)
