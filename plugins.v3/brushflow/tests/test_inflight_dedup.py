import copy
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_channel_integration as channel_tests
from brushflow import BrushTaskConfig


class InflightDedupTests(unittest.TestCase):
    def setUp(self):
        channel_tests.ChannelIntegrationTests.setUp(self)
        self.plugin._channel_dedup_state["records"] = []
        self.plugin._channel_dedup_attributes = ["resolution", "effects"]
        self.path = "Show.S01E01.2026.1080p.SDR.WEB-DL.H264.mkv"
        self.content = channel_tests.make_torrent([(self.path, 1000)])
        self.downloader, self.selection = channel_tests.ChannelIntegrationTests.prepare_download(self)
        self.task = self.plugin._get_task_config()
        self.plugin.save_data.side_effect = self.save
        self.persisted = {}

    def save(self, key, value):
        self.persisted[key] = copy.deepcopy(value)

    def scope(self, torrent_hash="a" * 40, path=None, selection=None, task=None):
        content = channel_tests.make_torrent([(path or self.path, 1000)])
        return self.plugin._inflight_download_scope(
            content, self.torrent, task or self.task, torrent_hash, selection
        )

    def enqueue(self, torrent_hash="a" * 40, path=None, selection=None):
        with self.scope(torrent_hash, path, selection) as result:
            if not result["blocked"]:
                result["added"] = True
        return result

    def test_downloaded_task_blocks_another_hash_before_channel_publication(self):
        self.assertEqual(self.plugin._BrushFlow__download(self.torrent), "a" * 40)
        with patch("brushflow.TorrentHelper.get_torrent_hash", return_value="b" * 40):
            self.assertIsNone(self.plugin._BrushFlow__download(self.torrent))
        self.downloader.add_torrent.assert_called_once()
        self.assertEqual(len(self.persisted["channel_dedup_inflight"]), 1)

    def test_selected_specs_all_match_and_unselected_codec_source_size_do_not_matter(self):
        self.enqueue()
        duplicate = self.enqueue("b" * 40, self.path.replace("H264", "H265").replace("WEB-DL", "BluRay"))
        self.assertTrue(duplicate["blocked"])
        for path in (
            self.path.replace("1080p", "2160p"),
            self.path.replace("SDR", "HDR10"),
            self.path.replace("S01E01", "S01E02"),
            self.path.replace("Show.", "Different."),
            self.path.replace("2026", "2025"),
        ):
            with self.subTest(path=path):
                self.assertFalse(self.enqueue(str(len(self.plugin._inflight_entries)) * 40, path)["blocked"])

    def test_real_chitose_cr_then_baha_is_blocked_with_resolution_and_effects_selected(self):
        cr = "Chitose.kun.wa.Ramune.Bin.no.Naka.S01E18.2026.1080p.CR.WEB-DL.H264.AAC-ADWeb.mkv"
        baha = "Chitose.kun.wa.Ramune.Bin.no.Naka.S01E18.2026.1080p.Baha.WEB-DL.x264.AAC-ADWeb.mp4"
        self.assertFalse(self.enqueue(path=cr)["blocked"])
        self.assertTrue(self.enqueue("b" * 40, baha)["blocked"])

    def test_unknown_selected_effects_are_not_silently_treated_as_sdr(self):
        self.enqueue()
        path = self.path.replace("SDR.", "").replace("H264", "H265")
        self.assertFalse(self.enqueue("b" * 40, path)["blocked"])

    def test_failed_add_releases_reservation_only_after_confirmed_absence(self):
        self.downloader.add_torrent.return_value = (False, [])
        self.assertIsNone(self.plugin._BrushFlow__download(self.torrent))
        self.assertEqual(self.plugin._inflight_entries, {})
        self.downloader.add_torrent.return_value = (True, ["a" * 40])
        self.assertEqual(self.plugin._BrushFlow__download(self.torrent), "a" * 40)

    def test_ambiguous_add_preserves_reservation_when_hash_exists(self):
        self.downloader.add_torrent.side_effect = RuntimeError("timeout")
        self.downloader.get_torrents.side_effect = [([], False), ([{"hash": "a" * 40}], False)]
        with self.assertRaises(RuntimeError):
            self.plugin._BrushFlow__download(self.torrent)
        entry = next(iter(self.plugin._inflight_entries.values()))
        self.assertEqual(entry["status"], "queued")
        self.assertTrue(self.enqueue("b" * 40)["blocked"])

    def test_failed_add_and_downloader_outage_keep_reservation_until_rechecked(self):
        self.downloader.add_torrent.return_value = (False, [])
        self.downloader.get_torrents.side_effect = [([], False), ([], True)]
        self.assertIsNone(self.plugin._BrushFlow__download(self.torrent))
        self.assertEqual(len(self.plugin._inflight_entries), 1)
        self.downloader.get_torrents.side_effect = None
        self.downloader.get_torrents.return_value = ([], True)
        for entry in self.plugin._inflight_entries.values():
            entry["created_at"] -= 600
        self.assertTrue(self.enqueue("b" * 40)["blocked"])

    def test_confirmed_deleted_task_is_released_but_fresh_add_is_given_grace(self):
        self.enqueue()
        self.assertTrue(self.enqueue("b" * 40)["blocked"])
        for entry in self.plugin._inflight_entries.values():
            entry["created_at"] -= 600
        self.plugin._inflight_checked_at = {}
        self.assertFalse(self.enqueue("c" * 40)["blocked"])
        self.assertEqual(len(self.plugin._inflight_entries), 1)

    def test_once_seen_task_is_released_after_confirmed_deletion_without_add_grace(self):
        self.enqueue()
        self.downloader.get_torrents.return_value = ([{"hash": "a" * 40}], False)
        self.plugin._inflight_checked_at = {}
        self.assertTrue(self.enqueue("b" * 40)["blocked"])
        self.downloader.get_torrents.return_value = ([], False)
        self.plugin._inflight_checked_at = {}
        self.assertFalse(self.enqueue("c" * 40)["blocked"])

    def test_restart_preserves_queued_records_and_active_tasks_do_not_expire_at_72_hours(self):
        self.enqueue()
        self.plugin._inflight_entries = copy.deepcopy(self.persisted["channel_dedup_inflight"])
        self.plugin._inflight_checked_at = {}
        for entry in self.plugin._inflight_entries.values():
            entry["created_at"] -= 4 * 86400
        self.downloader.get_torrents.return_value = ([{"hash": "a" * 40}], False)
        self.assertTrue(self.enqueue("b" * 40)["blocked"])

    def test_atomic_reservation_blocks_parallel_enqueue_across_tasks_and_downloaders(self):
        reserved = threading.Event()
        checked = threading.Event()
        other_task = BrushTaskConfig({"name": "another site", "downloader": "TR"})

        def first():
            with self.scope() as result:
                reserved.set()
                if not checked.wait(5):
                    raise AssertionError("Concurrent check did not finish")
                result["added"] = True
                return result["blocked"]

        def second():
            if not reserved.wait(5):
                raise AssertionError("First reservation was not created")
            try:
                with self.scope("b" * 40, task=other_task) as result:
                    return result["blocked"]
            finally:
                checked.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_result, second_result = pool.submit(first), pool.submit(second)
            self.assertFalse(first_result.result(timeout=10))
            self.assertTrue(second_result.result(timeout=10))
        self.assertEqual(len(self.plugin._inflight_entries), 1)

    def test_pack_selects_only_unqueued_episode_and_reserves_only_selected_files(self):
        self.enqueue()
        episode2 = self.path.replace("S01E01", "S01E02")
        old_episode = self.path.replace("S01E01", "S01E03")
        content = channel_tests.make_torrent([(self.path, 1000), (episode2, 2000), (old_episode, 3000)])
        with self.plugin._inflight_download_scope(
            content, self.torrent, self.task, "b" * 40, {"accepted": {self.path, episode2}}
        ) as result:
            self.assertFalse(result["blocked"])
            self.assertEqual(result["selection"]["accepted"], {episode2})
            result["added"] = True
        records = self.plugin._inflight_entries[self.plugin._inflight_key("QB", "b" * 40)]["records"]
        self.assertEqual([row["episodes"] for row in records], [[2]])

    def test_combined_episode_file_needs_all_episodes_but_size_cannot_match_separate_files(self):
        self.enqueue()
        combined = self.path.replace("S01E01", "S01E01-E02")
        self.assertFalse(self.enqueue("b" * 40, combined)["blocked"])
        self.plugin._inflight_entries.pop(self.plugin._inflight_key("QB", "b" * 40))
        self.enqueue("c" * 40, self.path.replace("S01E01", "S01E02"))
        self.assertTrue(self.enqueue("d" * 40, combined)["blocked"])
        self.plugin._channel_dedup_attributes = ["size"]
        self.assertFalse(self.enqueue("e" * 40, combined)["blocked"])

    def test_channel_and_queue_exclusions_intersect_for_paused_pack_selection(self):
        self.plugin._channel_dedup_attributes = ["resolution", "codec"]
        self.enqueue()
        record = copy.deepcopy(self.record)
        record["episodes"] = [2]
        self.plugin._channel_dedup_state["records"] = [record]
        episode2, episode3 = (self.path.replace("S01E01", f"S01E{number:02d}") for number in (2, 3))
        content = channel_tests.make_torrent([(self.path, 1000), (episode2, 2000), (episode3, 3000)])
        with patch("brushflow.DownloadChain") as chain, patch(
            "brushflow.TorrentHelper.get_torrent_hash", return_value="b" * 40
        ):
            chain.return_value.download_torrent.return_value = (content, None, None)
            self.downloader.add_torrent.return_value = (True, ["b" * 40])
            self.assertEqual(self.plugin._BrushFlow__download(self.torrent), "b" * 40)
        self.assertEqual(self.selection.call_args.args[2]["accepted"], {episode3})
        self.assertTrue(self.downloader.add_torrent.call_args.kwargs["is_paused"])

    def test_disabled_empty_attributes_and_magnet_do_not_reserve(self):
        for enabled, attributes, content in (
            (False, ["resolution"], self.content), (True, [], self.content),
            (True, ["resolution"], "magnet:?xt=urn:btih:" + "a" * 40),
        ):
            self.plugin._channel_dedup_enabled = enabled
            self.plugin._channel_dedup_attributes = attributes
            with self.plugin._inflight_download_scope(content, self.torrent, self.task, "a" * 40, None) as result:
                self.assertFalse(result["blocked"])
                result["added"] = True
        self.assertEqual(self.plugin._inflight_entries, {})

    def test_transmission_uses_same_queue_guard_and_paused_pack_selection(self):
        self.enqueue()
        episode2 = self.path.replace("S01E01", "S01E02")
        content = channel_tests.make_torrent([(self.path, 1000), (episode2, 2000)])
        with patch("brushflow.DownloaderHelper") as helper, patch("brushflow.DownloadChain") as chain, patch(
            "brushflow.TorrentHelper.get_torrent_hash", return_value="b" * 40
        ):
            helper.return_value.get_service.return_value = SimpleNamespace(instance=self.downloader)
            helper.return_value.is_downloader.side_effect = lambda kind, **kwargs: kind == "transmission"
            chain.return_value.download_torrent.return_value = (content, None, None)
            self.downloader.add_torrent.return_value = SimpleNamespace(hashString="b" * 40)
            self.assertEqual(self.plugin._BrushFlow__download(self.torrent), "b" * 40)
        self.assertEqual(self.selection.call_args.args[3], "transmission")
        self.assertEqual(self.selection.call_args.args[2]["accepted"], {episode2})

    def test_backfill_uses_actual_wanted_files_only(self):
        legacy_task = BrushTaskConfig({"id": "old", "name": "legacy", "downloader": "QB"})
        self.plugin._task_configs = {legacy_task.id: legacy_task}
        self.plugin._get_task_data = Mock(return_value={
            "c" * 40: {"title": "Show S01 2026 1080p SDR WEB-DL H264", "task_name": "legacy"},
        })
        episode2 = self.path.replace("S01E01", "S01E02")
        self.downloader.get_files.return_value = [
            {"name": self.path, "size": 1000, "priority": 1},
            {"name": episode2, "size": 2000, "priority": 0},
        ]
        self.assertTrue(self.enqueue()["blocked"])
        self.assertFalse(self.enqueue("b" * 40, episode2)["blocked"])
        self.downloader.get_files.assert_called_once_with("c" * 40)

    def test_backfill_transmission_selected_flags_and_missing_flags_are_not_assumed(self):
        self.plugin._task_configs = {"legacy": BrushTaskConfig({"downloader": "TR"})}
        self.plugin._get_task_data = Mock(return_value={
            "c" * 40: {"title": "Show S01 2026 1080p SDR WEB-DL H264"},
        })
        self.downloader.get_files.return_value = [
            SimpleNamespace(name=self.path, size=1000, selected=True),
            SimpleNamespace(name=self.path.replace("S01E01", "S01E02"), size=1000, selected=False),
            {"name": self.path.replace("S01E01", "S01E03"), "size": 1000},
        ]
        self.assertTrue(self.enqueue()["blocked"])
        self.assertFalse(self.enqueue("b" * 40, self.path.replace("S01E01", "S01E02"))["blocked"])
        self.assertFalse(self.enqueue("d" * 40, self.path.replace("S01E01", "S01E03"))["blocked"])

    def test_conflicting_explicit_tmdb_ids_never_match_just_by_title(self):
        self.enqueue(path=self.path.replace("Show.", "Show.(tmdbid=100)."))
        self.assertFalse(self.enqueue("b" * 40, self.path.replace("Show.", "Show.(tmdbid=200)."))["blocked"])

    def test_selected_size_tolerance_and_spec_changes_use_current_settings(self):
        self.enqueue()
        self.plugin._channel_dedup_attributes = ["size"]
        for expected, torrent_hash, size in ((True, "b" * 40, 1040), (False, "c" * 40, 1100)):
            content = channel_tests.make_torrent([(self.path, size)])
            with self.plugin._inflight_download_scope(
                content, self.torrent, self.task, torrent_hash, None
            ) as result:
                self.assertEqual(result["blocked"], expected)
                if not expected:
                    result["added"] = True

    def test_persistence_failure_happens_before_adding_to_downloader(self):
        self.plugin.save_data.side_effect = RuntimeError("db unavailable")
        with self.assertRaises(RuntimeError):
            self.plugin._BrushFlow__download(self.torrent)
        self.downloader.add_torrent.assert_not_called()

    def test_failed_partial_file_selection_removes_task_and_releases_reservation(self):
        self.enqueue()
        episode2 = self.path.replace("S01E01", "S01E02")
        content = channel_tests.make_torrent([(self.path, 1000), (episode2, 2000)])
        self.selection.return_value = False
        with patch("brushflow.DownloadChain") as chain, patch(
            "brushflow.TorrentHelper.get_torrent_hash", return_value="b" * 40
        ):
            chain.return_value.download_torrent.return_value = (content, None, None)
            self.downloader.add_torrent.return_value = (True, ["b" * 40])
            self.assertIsNone(self.plugin._BrushFlow__download(self.torrent))
        self.assertNotIn(self.plugin._inflight_key("QB", "b" * 40), self.plugin._inflight_entries)
        self.downloader.delete_torrents.assert_called_once_with(ids="b" * 40, delete_file=False)


if __name__ == "__main__":
    unittest.main()
