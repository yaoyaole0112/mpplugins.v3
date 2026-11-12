import copy
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_channel_integration as integration
from brushflow import BrushTaskConfig
from brushflow.active_dedup import download_files, download_state
from brushflow.channel_dedup import parse_candidate_file
from brushflow.models import ChannelDedupSettingsPayload


class FakeDownloader:
    def __init__(self, kind, tag):
        self.kind, self.tag = kind, tag
        self.progress, self.paused, self.exists = 0.25, False, True
        self.files = [{"index": 0, "name": "Show.S01E01.1080p.WEB-DL.H264.mkv",
                       "size": 1000, "priority": 1, "progress": 0.25}]
        self.get_torrents = Mock(side_effect=self.torrents)
        self.get_files = Mock(side_effect=self.file_snapshot)
        self.stop_torrents = Mock(side_effect=self.stop)
        self.start_torrents = Mock(side_effect=self.start)
        self.delete_torrents = Mock(side_effect=self.delete)
        self.set_files = Mock(side_effect=self.select)
        self.set_unwanted_files = Mock(side_effect=lambda tid, ids: self.select(file_ids=ids))

    def snapshot(self):
        if self.kind == "qbittorrent":
            return {"hash": "a" * 40, "tags": self.tag, "progress": self.progress,
                    "state": ("pausedUP" if self.progress == 1 else "pausedDL") if self.paused
                    else "downloading"}
        return SimpleNamespace(hashString="a" * 40, labels=[self.tag], percent_done=self.progress,
                               status="stopped" if self.paused else "downloading")

    def torrents(self, **kwargs):
        return ([self.snapshot()] if self.exists else []), False

    def file_snapshot(self, *args):
        if self.kind == "qbittorrent":
            return copy.deepcopy(self.files)
        return [SimpleNamespace(id=row["index"], name=row["name"], size=row["size"],
                                selected=row["priority"] > 0, completed=int(row["size"] * row["progress"]))
                for row in self.files]

    def stop(self, *args):
        self.paused = True
        return True

    def start(self, *args):
        self.paused = False
        return True

    def delete(self, **kwargs):
        assert kwargs["delete_file"] is False
        self.exists = False
        return True

    def select(self, **kwargs):
        for item in self.files:
            if item["index"] in kwargs["file_ids"]:
                item["priority"] = 0
        return True


class ActiveDedupTests(unittest.TestCase):
    def setUp(self):
        integration.ChannelIntegrationTests.setUp(self)
        self.plugin._channel_cancel_lock = threading.Lock()
        self.plugin._brush_lock = threading.Lock()
        self.plugin._channel_cancel_offset = 0
        self.plugin._channel_dedup_cancel_downloading = True
        self.plugin._channel_dedup_attributes = ["resolution"]
        self.plugin._channel_dedup_unknown_policy = "exclude"
        self.task = BrushTaskConfig({"id": "task-one", "name": "test", "enabled": True,
                                     "downloader": "QB", "downloader_tags": "BrushTest"})
        self.plugin._task_configs = {self.task.id: self.task}
        self.plugin._task_locks = {self.task.id: threading.Lock()}
        self.row = {"title": "Show S01 1080p", "deleted": False, "hit_and_run": False}
        self.rows = {self.task.id: {"a" * 40: self.row}}
        self.persisted = {}
        self.plugin._get_task_data = Mock(side_effect=lambda task_id, key: self.rows[task_id])
        self.plugin._save_task_data = Mock(side_effect=lambda task_id, key, data:
                                           self.persisted.update({task_id: copy.deepcopy(data)}))
        self.plugin._recalculate_statistics = Mock()
        self.plugin.get_state = Mock(return_value=True)
        self.downloader = FakeDownloader("qbittorrent", self.task.brush_tag)
        self.service = SimpleNamespace(instance=self.downloader)
        self.helper = Mock()
        self.helper.get_service.return_value = self.service
        self.helper.is_downloader.side_effect = lambda value, **kwargs: value == self.downloader.kind
        self.patcher = patch("brushflow.DownloaderHelper", return_value=self.helper)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.record["specs"] = {"resolution": "1080p", "effects": None, "codec": "H.265",
                                "source": "BluRay", "fps": 60, "size": 99999}
        self.queue()

    def queue(self):
        self.plugin._inflight_entries[self.plugin._inflight_key("QB", "a" * 40)] = {
            "downloader": "QB", "hash": "a" * 40, "status": "queued",
            "records": [parse_candidate_file(row["name"], row["size"]) for row in self.downloader.files],
        }

    def run_check(self):
        with patch("brushflow.logger"), patch("brushflow.time.sleep"):
            self.plugin.cancel_channel_duplicates()

    def add_file(self, episode=2, progress=0.1, priority=7):
        self.downloader.files.append({"index": len(self.downloader.files),
                                      "name": f"Show.S01E{episode:02d}.1080p.WEB-DL.H264.mkv",
                                      "size": 2000, "priority": priority, "progress": progress})
        self.queue()

    def assert_untouched(self):
        self.downloader.stop_torrents.assert_not_called()
        self.downloader.set_files.assert_not_called()
        self.downloader.set_unwanted_files.assert_not_called()
        self.downloader.delete_torrents.assert_not_called()

    def test_whole_duplicate_is_removed_without_deleting_data_or_using_unselected_specs(self):
        self.run_check()
        self.downloader.delete_torrents.assert_called_once_with(ids="a" * 40, delete_file=False)
        self.assertTrue(self.row["deleted"])
        self.assertEqual(self.plugin._inflight_entries, {})
        self.assertEqual(len(self.row["channel_dedup_cancelled_files"]), 1)
        self.assertNotIn("channel_dedup_cancel_pending", self.row)

    def test_pack_only_deselects_unfinished_duplicate_and_preserves_other_priorities(self):
        self.add_file()
        self.run_check()
        self.downloader.delete_torrents.assert_not_called()
        self.downloader.set_files.assert_called_once_with(torrent_hash="a" * 40, file_ids=[0], priority=0)
        self.assertEqual([row["priority"] for row in self.downloader.files], [0, 7])
        self.downloader.start_torrents.assert_called_once()
        self.assertFalse(self.row["deleted"])
        records = next(iter(self.plugin._inflight_entries.values()))["records"]
        self.assertEqual([record["episodes"] for record in records], [[2]])
        self.run_check()
        self.downloader.set_files.assert_called_once()

    def test_originally_paused_pack_is_not_resumed(self):
        self.add_file()
        self.downloader.paused = True
        self.run_check()
        self.downloader.start_torrents.assert_not_called()
        self.assertTrue(self.downloader.paused)

    def test_completed_files_remain_selected_and_completed_torrents_are_not_touched(self):
        self.add_file(progress=1)
        self.run_check()
        self.assertEqual(self.downloader.files[1]["priority"], 7)
        self.downloader.delete_torrents.assert_not_called()
        self.downloader.stop_torrents.reset_mock()
        self.downloader.progress = 1
        self.run_check()
        self.downloader.stop_torrents.assert_not_called()

    def test_completed_duplicate_in_an_unfinished_pack_is_not_touched(self):
        self.add_file()
        self.downloader.files[0]["progress"] = 1
        self.run_check()
        self.assert_untouched()

    def test_known_completed_history_does_not_consume_unfinished_check_batch(self):
        completed = {f"{number:040x}": {"hit_and_run": False, "deleted": False, "progress": 1}
                     for number in range(80)}
        self.rows[self.task.id] = {**completed, "a" * 40: self.row}
        self.run_check()
        self.assertTrue(self.row["deleted"])
        self.assertEqual(self.downloader.get_torrents.call_args_list[0].kwargs["ids"], ["a" * 40])

    def test_unwanted_duplicate_is_not_touched(self):
        self.add_file()
        self.downloader.files[0]["priority"] = 0
        self.run_check()
        self.assert_untouched()

    def test_unidentified_or_unknown_selected_spec_is_not_cancellation_evidence(self):
        self.plugin._channel_dedup_attributes = ["resolution", "effects"]
        self.run_check()
        self.assert_untouched()
        self.plugin._channel_dedup_attributes = ["resolution"]
        self.record["title_key"] = "different"
        self.run_check()
        self.assert_untouched()

    def test_only_selected_specs_need_to_match(self):
        self.plugin._channel_dedup_attributes = ["resolution", "fps"]
        self.run_check()
        self.assert_untouched()
        self.plugin._channel_dedup_attributes = ["resolution"]
        self.run_check()
        self.assertTrue(self.row["deleted"])

    def test_different_resolution_season_episode_and_explicit_id_never_cancel(self):
        for field, value in (("season", 2), ("episodes", [2]), ("title_key", "different")):
            original = self.record[field]
            self.record[field] = value
            self.run_check()
            self.assert_untouched()
            self.record[field] = original
        self.record["specs"]["resolution"] = "2160p"
        self.run_check()
        self.assert_untouched()
        self.record["specs"]["resolution"] = "1080p"
        self.record["tmdb_id"] = 123
        self.downloader.files[0]["name"] = "Show.(tmdbid=999).S01E01.1080p.mkv"
        self.run_check()
        self.assert_untouched()

    def test_combined_episode_file_requires_all_episodes_to_be_published(self):
        self.downloader.files[0]["name"] = "Show.S01E01-E02.1080p.mkv"
        self.run_check()
        self.assert_untouched()
        second = copy.deepcopy(self.record)
        second["episodes"] = [2]
        self.plugin._channel_dedup_state["records"].append(second)
        self.run_check()
        self.assertTrue(self.row["deleted"])

    def test_hr_disabled_task_missing_owner_tag_and_excluded_tags_are_protected(self):
        for field, value in (("hit_and_run", True), ("deleted", True)):
            self.row[field] = value
            self.run_check()
            self.assert_untouched()
            self.row[field] = False
        self.task.site_hr_active = True
        self.run_check()
        self.assert_untouched()
        self.task.site_hr_active = False
        self.task.enabled = False
        self.run_check()
        self.assert_untouched()
        self.task.enabled = True
        self.downloader.tag = "manual"
        self.run_check()
        self.assert_untouched()
        self.task.delete_except_tags = "keep"
        self.downloader.tag = self.task.brush_tag + ",keep"
        self.run_check()
        self.assert_untouched()

    def test_unknown_hr_status_is_not_authorization_to_cancel(self):
        self.row.pop("hit_and_run")
        self.run_check()
        self.assert_untouched()

    def test_shared_hash_or_multiple_managed_tags_are_protected(self):
        second = BrushTaskConfig({"id": "other", "name": "other", "downloader": "QB", "enabled": False})
        self.plugin._task_configs[second.id] = second
        self.rows[second.id] = {"a" * 40: copy.deepcopy(self.row)}
        self.run_check()
        self.assert_untouched()
        self.rows[second.id] = {}
        self.downloader.tag = self.task.brush_tag + "," + second.brush_tag
        self.run_check()
        self.assert_untouched()

    def test_shared_managed_tag_is_not_exclusive_ownership(self):
        second = BrushTaskConfig({"id": "other", "downloader": "QB", "tag": self.task.brush_tag})
        self.plugin._task_configs[second.id] = second
        self.rows[second.id] = {}
        self.run_check()
        self.assert_untouched()

    def test_message_published_after_queueing_cancels_on_next_check(self):
        self.plugin._channel_dedup_state["records"] = []
        self.run_check()
        self.assert_untouched()
        self.plugin._channel_dedup_state["records"] = [self.record]
        self.run_check()
        self.assertTrue(self.row["deleted"])

    def test_empress_pack_queued_before_publication_only_cancels_s03e04(self):
        filename = ("[茜茜皇后 第三季].The.Empress.2026.S03E04.1080p.NF.WEB-DL."
                    "H264.DDP5.1.Atmos-UBWEB.mkv")
        self.downloader.files[0].update({"name": filename, "size": 2242142161})
        self.add_file()
        self.downloader.files[1]["name"] = filename.replace("E04", "E05")
        self.row["title"] = "The Empress 2026 S03 Complete 1080p"
        self.queue()
        self.plugin._channel_dedup_state["records"] = []
        self.run_check()
        self.assert_untouched()
        self.record.update({
            "title_key": "茜茜皇后", "tmdb_id": 131488, "year": 2022,
            "season": 3, "episodes": [4], "specs": {"resolution": "1080p"},
        })
        self.plugin.chain.recognize_media.return_value = SimpleNamespace(
            type="电视剧", title="茜茜皇后", en_title="The Empress", original_title="Die Kaiserin",
            names=["茜茜皇后", "The Empress"], year="2022", tmdb_id=131488)
        self.plugin._channel_dedup_state["records"] = [self.record]
        self.run_check()
        self.downloader.set_files.assert_called_once_with(torrent_hash="a" * 40, file_ids=[0], priority=0)
        self.assertEqual([item["priority"] for item in self.downloader.files], [0, 7])
        self.assertFalse(self.downloader.paused)
        self.downloader.delete_torrents.assert_not_called()

    def test_manual_binding_conflict_and_embedded_identity_follow_preadd_contract(self):
        self.record["tmdb_id"] = 123
        self.plugin._channel_dedup_identity_aliases = {"Show": 999}
        self.run_check()
        self.assert_untouched()
        self.plugin._channel_dedup_identity_aliases = {"Show": 123}
        self.run_check()
        self.assertTrue(self.row["deleted"])

    def test_cross_language_identity_uses_verified_official_aliases(self):
        self.record.update({"title_key": "中文剧名", "tmdb_id": 123})
        self.plugin.chain.recognize_media.return_value = SimpleNamespace(
            title="中文剧名", en_title="Show", tmdb_id=123, type="电视剧")
        self.run_check()
        self.assertTrue(self.row["deleted"])

    def test_switch_round_trip_and_scheduler_do_not_change_telegram_polling(self):
        self.plugin._save_config = Mock()
        self.plugin._refresh_scheduler = Mock()
        with patch("brushflow.ThreadHelper"):
            response = self.plugin.update_channel_dedup_settings(ChannelDedupSettingsPayload(
                enabled=True, channels=["@channel"], attributes=["resolution"], cancel_downloading=True))
        self.assertTrue(response.success)
        self.assertTrue(self.plugin._channel_dedup_payload()["cancel_downloading"])
        self.assertTrue(self.plugin._current_config()["channel_dedup_cancel_downloading"])
        self.task.enabled = False
        services = {item["id"]: item for item in self.plugin.get_service()}
        self.assertEqual(services["ChannelDedupCancel"]["kwargs"], {"minutes": 1})
        self.assertEqual(services["ChannelDedupPoll"]["kwargs"], {"minutes": 1})
        self.plugin._channel_dedup_cancel_downloading = False
        self.assertNotIn("ChannelDedupCancel", {item["id"] for item in self.plugin.get_service()})

    def test_default_and_disabled_conditions_do_not_query_or_mutate_downloaders(self):
        self.assertFalse(ChannelDedupSettingsPayload().cancel_downloading)
        for attribute, value in (("_channel_dedup_cancel_downloading", False),
                                 ("_channel_dedup_enabled", False), ("_channel_stopping", True),
                                 ("_channel_dedup_attributes", []), ("_channel_dedup_channels", [])):
            original = getattr(self.plugin, attribute)
            setattr(self.plugin, attribute, value)
            self.run_check()
            self.downloader.get_torrents.assert_not_called()
            setattr(self.plugin, attribute, original)

    def test_expired_or_removed_channel_records_cannot_cancel(self):
        self.record["published_at"] = time.time() - 73 * 3600
        self.run_check()
        self.assert_untouched()
        self.record["published_at"] = time.time()
        self.record["channel"] = "@removed"
        self.plugin._channel_dedup_state["records"] = [self.record]
        self.run_check()
        self.assert_untouched()

    def test_downloader_error_unknown_progress_or_metadata_state_never_mutates(self):
        self.downloader.get_torrents.side_effect = lambda **kwargs: ([], True)
        self.run_check()
        self.assert_untouched()
        for torrent in ({"hash": "a" * 40, "tags": self.task.brush_tag},
                        {"hash": "a" * 40, "tags": self.task.brush_tag, "progress": .2, "state": "checkingDL"}):
            self.downloader.get_torrents.side_effect = lambda **kwargs: ([torrent], False)
            self.run_check()
            self.assert_untouched()

    def test_unknown_or_ambiguous_file_snapshot_never_mutates(self):
        for files in (None, [{"name": "Show.S01E01.1080p.mkv", "size": 1000}],
                      [self.downloader.files[0], self.downloader.files[0]]):
            self.downloader.get_files.side_effect = None
            self.downloader.get_files.return_value = files
            self.run_check()
            self.assert_untouched()

    def test_pause_failure_or_pause_not_confirmed_never_deletes_or_deselects(self):
        for stop in (lambda *args: False, lambda *args: True):
            self.downloader.stop_torrents.side_effect = stop
            self.run_check()
            self.downloader.delete_torrents.assert_not_called()
            self.downloader.set_files.assert_not_called()

    def test_completion_racing_with_pause_is_retained_and_resumed(self):
        def complete(*args):
            self.downloader.paused = True
            self.downloader.progress = 1
            return True
        self.downloader.stop_torrents.side_effect = complete
        self.run_check()
        self.downloader.delete_torrents.assert_not_called()
        self.downloader.start_torrents.assert_called_once()
        self.assertNotIn("channel_dedup_cancel_pending", self.row)

    def test_individual_completion_while_pausing_is_not_deselected(self):
        self.add_file()
        def complete(*args):
            self.downloader.paused = True
            self.downloader.files[0]["progress"] = 1
            return True
        self.downloader.stop_torrents.side_effect = complete
        self.run_check()
        self.downloader.delete_torrents.assert_not_called()
        self.downloader.set_files.assert_not_called()
        self.downloader.start_torrents.assert_called_once()

    def test_changed_settings_after_pause_prevent_delete_selection_or_resume(self):
        def stop(*args):
            self.downloader.paused = True
            self.plugin._channel_generation += 1
            self.plugin._channel_dedup_cancel_downloading = False
            return True
        self.downloader.stop_torrents.side_effect = stop
        self.run_check()
        self.downloader.delete_torrents.assert_not_called()
        self.downloader.set_files.assert_not_called()
        self.downloader.start_torrents.assert_not_called()

    def test_disabled_task_while_pausing_is_not_cancelled(self):
        def stop(*args):
            self.downloader.paused = True
            self.task.enabled = False
            return True
        self.downloader.stop_torrents.side_effect = stop
        self.run_check()
        self.downloader.delete_torrents.assert_not_called()
        self.downloader.set_files.assert_not_called()

    def test_delete_failure_retains_task_queue_and_pending_for_retry(self):
        self.downloader.delete_torrents.side_effect = None
        self.downloader.delete_torrents.return_value = False
        self.run_check()
        self.assertFalse(self.row["deleted"])
        self.assertTrue(self.plugin._inflight_entries)
        self.assertEqual(self.persisted[self.task.id]["a" * 40]["channel_dedup_cancel_pending"]["phase"], "removing")
        self.downloader.delete_torrents.side_effect = self.downloader.delete
        self.run_check()
        self.assertTrue(self.row["deleted"])

    def test_ambiguous_delete_is_only_finalized_when_absence_is_confirmed(self):
        calls = 0
        def torrents(**kwargs):
            nonlocal calls
            calls += 1
            return ([], True) if calls == 3 else self.downloader.torrents(**kwargs)
        self.downloader.get_torrents.side_effect = torrents
        self.run_check()
        self.assertFalse(self.row["deleted"])
        self.assertTrue(self.plugin._inflight_entries)
        self.downloader.get_torrents.side_effect = self.downloader.torrents
        self.run_check()
        self.assertTrue(self.row["deleted"])

    def test_failed_selection_keeps_paused_and_retries_without_changing_other_files(self):
        self.add_file()
        self.downloader.set_files.side_effect = None
        self.downloader.set_files.return_value = False
        self.run_check()
        self.assertTrue(self.downloader.paused)
        self.downloader.start_torrents.assert_not_called()
        self.assertTrue(self.plugin._inflight_entries)
        self.downloader.set_files.side_effect = self.downloader.select
        self.run_check()
        self.assertFalse(self.downloader.paused)
        self.assertNotIn("channel_dedup_cancel_pending", self.row)

    def test_unverified_selection_recovers_from_persisted_intent_and_resumes_once(self):
        self.add_file()
        calls = 0
        def files(*args):
            nonlocal calls
            calls += 1
            return None if calls == 3 else self.downloader.file_snapshot()
        self.downloader.get_files.side_effect = files
        self.run_check()
        self.assertTrue(self.downloader.paused)
        self.downloader.start_torrents.assert_not_called()
        self.assertIn("channel_dedup_cancel_pending", self.row)
        self.downloader.get_files.side_effect = self.downloader.file_snapshot
        self.run_check()
        self.downloader.start_torrents.assert_called_once()
        self.assertNotIn("channel_dedup_cancel_pending", self.row)

    def test_resume_failure_is_retried_without_reselecting(self):
        self.add_file()
        self.downloader.start_torrents.side_effect = None
        self.downloader.start_torrents.return_value = False
        self.run_check()
        self.assertTrue(self.downloader.paused)
        self.assertIn("channel_dedup_cancel_pending", self.row)
        self.downloader.start_torrents.side_effect = self.downloader.start
        self.run_check()
        self.downloader.set_files.assert_called_once()
        self.assertNotIn("channel_dedup_cancel_pending", self.row)

    def test_expired_evidence_restores_previously_running_task_without_cancelling(self):
        self.downloader.delete_torrents.side_effect = None
        self.downloader.delete_torrents.return_value = False
        self.run_check()
        self.assertTrue(self.downloader.paused)
        self.record["published_at"] = time.time() - 73 * 3600
        self.run_check()
        self.assertFalse(self.downloader.paused)
        self.assertFalse(self.row["deleted"])
        self.assertNotIn("channel_dedup_cancel_pending", self.row)
        self.assertTrue(self.plugin._inflight_entries)

    def test_completed_during_retry_is_restored_and_never_deleted(self):
        self.downloader.delete_torrents.side_effect = None
        self.downloader.delete_torrents.return_value = False
        self.run_check()
        self.downloader.progress = 1
        self.run_check()
        self.assertFalse(self.downloader.paused)
        self.downloader.delete_torrents.assert_called_once()
        self.assertNotIn("channel_dedup_cancel_pending", self.row)

    def test_completed_after_selection_keeps_files_and_releases_cancelled_reservation(self):
        self.add_file()
        self.downloader.start_torrents.side_effect = None
        self.downloader.start_torrents.return_value = False
        self.run_check()
        self.downloader.progress = 1
        self.downloader.start_torrents.side_effect = self.downloader.start
        self.run_check()
        self.assertFalse(self.downloader.paused)
        self.downloader.delete_torrents.assert_not_called()
        records = next(iter(self.plugin._inflight_entries.values()))["records"]
        self.assertEqual([item["episodes"] for item in records], [[2]])
        self.assertNotIn("channel_dedup_cancel_pending", self.row)

    def test_partly_applied_selection_releases_only_verified_cancelled_reservations(self):
        self.add_file(episode=2)
        self.add_file(episode=3)
        second = copy.deepcopy(self.record)
        second["episodes"] = [2]
        self.plugin._channel_dedup_state["records"].append(second)
        def partially_select(**kwargs):
            self.downloader.files[0]["priority"] = 0
            return False
        self.downloader.set_files.side_effect = partially_select
        self.run_check()
        self.assertTrue(self.downloader.paused)
        self.plugin._channel_dedup_state["records"] = []
        self.run_check()
        self.assertFalse(self.downloader.paused)
        self.assertEqual(len(self.row["channel_dedup_cancelled_files"]), 1)
        records = next(iter(self.plugin._inflight_entries.values()))["records"]
        self.assertEqual([item["episodes"] for item in records], [[2], [3]])
        self.assertNotIn("channel_dedup_cancel_pending", self.row)

    def test_recovery_with_changed_settings_does_not_resume_or_remove(self):
        self.row["channel_dedup_cancel_pending"] = {"settings": "old", "paths": ["some"], "resume": True}
        self.run_check()
        self.assert_untouched()
        self.downloader.start_torrents.assert_not_called()

    def test_busy_task_or_parallel_cancellation_never_waits_or_mutates(self):
        for lock in (self.plugin._task_locks[self.task.id], self.plugin._brush_lock, self.plugin._channel_cancel_lock):
            with lock:
                self.run_check()
            self.assert_untouched()
        self.run_check()
        self.assertTrue(self.row["deleted"])

    def test_transmission_whole_task_and_partial_pack_use_wanted_file_api(self):
        self.downloader.kind = "transmission"
        self.add_file()
        self.run_check()
        self.downloader.set_files.assert_not_called()
        self.downloader.set_unwanted_files.assert_called_once_with("a" * 40, [0])
        self.downloader.start_torrents.assert_called_once()
        second = copy.deepcopy(self.record)
        second["episodes"] = [2]
        self.plugin._channel_dedup_state["records"].append(second)
        self.run_check()
        self.downloader.delete_torrents.assert_called_once_with(ids="a" * 40, delete_file=False)


class SnapshotTests(unittest.TestCase):
    def test_invalid_completion_or_selection_never_authorizes_changes(self):
        for progress in (None, True, -1, 1, float("nan"), float("inf")):
            self.assertIsNone(download_state({"state": "downloading", "progress": progress}, "qbittorrent"))
        self.assertIsNone(download_files([{"name": "file", "size": 1, "progress": .1,
                                          "priority": 1}], "qbittorrent"))
        self.assertIsNone(download_files([SimpleNamespace(id=0, name="file", size=100, selected=True)], "transmission"))
        self.assertIsNone(download_files([SimpleNamespace(id=0, name="file", size=100, selected=None, completed=1)], "transmission"))


if __name__ == "__main__":
    unittest.main()
