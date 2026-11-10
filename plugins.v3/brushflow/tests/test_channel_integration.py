import copy
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from brushflow import BrushFlow, BrushTaskConfig, TorrentInfo
from brushflow.channel_dedup import parse_channel_message
from brushflow.models import ChannelDedupSettingsPayload
from test_channel_dedup import bencode


def make_torrent(paths):
    return bencode({"info": {
        "name": "Show",
        "files": [{"length": size, "path": [path]} for path, size in paths],
        "piece length": 16384,
        "pieces": b"a" * 20,
    }})


class ChannelIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.plugin = BrushFlow.__new__(BrushFlow)
        self.plugin._enabled = True
        self.plugin._channel_stopping = False
        self.plugin._channel_generation = 0
        self.plugin._channel_dedup_lock = threading.Lock()
        self.plugin._channel_state_lock = threading.RLock()
        self.plugin._channel_dedup_retry_after = 0
        self.plugin._channel_identity_cache = {}
        self.plugin._channel_dedup_enabled = True
        self.plugin._channel_dedup_channels = ["@channel"]
        self.plugin._channel_dedup_attributes = ["resolution", "codec"]
        self.plugin._channel_dedup_size_tolerance_pct = 5
        self.record = parse_channel_message("@channel", 1, time.time(), "影视：Show\n季集：S01E01\n质量：1080p H264")
        self.plugin._channel_dedup_state = {
            "cursors": {}, "records": [self.record], "last_poll_at": None, "last_error": "",
        }
        self.plugin.save_data = Mock()
        self.plugin.chain = SimpleNamespace(recognize_media=Mock(return_value=None))
        self.paths = [
            ("Show.S01E01.1080p.WEB-DL.H264.mkv", 1000),
            ("Show.S01E02.1080p.WEB-DL.H264.mkv", 2000),
            ("Show.S01E01.srt", 100),
        ]
        self.content = make_torrent(self.paths)

    def select(self, accepted=None):
        return self.plugin._channel_dedup_file_selection(self.content, "Show.S01.1080p.H264", accepted)

    def test_season_pack_excludes_only_matching_file_and_keeps_unmatched_episode(self):
        result = self.select()
        self.assertEqual(result["excluded"], {self.paths[0][0]})
        self.assertEqual(result["remaining_paths"], {self.paths[1][0], self.paths[2][0]})
        self.plugin.chain.recognize_media.assert_not_called()

    def test_recent_rule_file_selection_is_applied_before_channel_matching(self):
        self.assertIsNone(self.select({self.paths[1][0]}))
        result = self.select({self.paths[0][0]})
        self.assertEqual(result["excluded"], {self.paths[0][0]})

    def test_size_uses_video_length_not_pack_total(self):
        self.plugin._channel_dedup_attributes = ["size"]
        self.record["specs"]["size"] = 1000
        result = self.select()
        self.assertEqual(result["excluded"], {self.paths[0][0]})

    def test_unknown_selected_fps_allows_download_without_identity_lookup(self):
        self.plugin._channel_dedup_attributes = ["resolution", "fps"]
        self.assertIsNone(self.select())
        self.plugin.chain.recognize_media.assert_not_called()

    def test_tmdb_identity_resolves_english_chinese_names_and_is_cached_per_show(self):
        self.record["title_key"] = "中文剧名"
        self.record["tmdb_id"] = 123
        self.plugin.chain.recognize_media.return_value = SimpleNamespace(
            title="中文剧名", en_title="Show", tmdb_id=123, type="电视剧",
        )
        self.assertIsNotNone(self.select())
        self.assertIsNotNone(self.select())
        self.plugin.chain.recognize_media.assert_called_once()

    def test_fuzzy_misrecognition_does_not_exclude_a_different_title(self):
        self.record["title_key"] = "other"
        self.record["tmdb_id"] = 123
        self.plugin.chain.recognize_media.return_value = SimpleNamespace(
            title="Other Show", tmdb_id=123, type="电视剧",
        )
        self.assertIsNone(self.select())

    def prepare_phony_record(self):
        self.plugin._channel_dedup_attributes = ["resolution", "effects"]
        self.record = parse_channel_message(
            "@channel", 9997, time.time(),
            "影视：换母疑云\n季集：S01E05\n质量：1080p SDR H264\n"
            "ed2k://|file|换母疑云(2026)(tmdbid=275139).S01E05.WEB-DL.1080p."
            f"SDR.H264.8bit.DDP.5.1.24帧-RMGroup.mkv|1109637998|{'a' * 32}|/",
        )
        self.plugin._channel_dedup_state["records"] = [self.record]
        self.plugin.chain.recognize_media.return_value = SimpleNamespace(
            title="换母疑云", en_title="Phony", tmdb_id=275139, type="电视剧",
        )
        self.phony_path = "Phony.S01E05.2026.1080p.DSNP.WEB-DL.H.264.DDP5.1-HHWEB.mkv"
        self.content = make_torrent([(self.phony_path, 1109643658)])

    def test_unlabelled_sdr_resolves_identity_and_excludes_before_download(self):
        self.prepare_phony_record()
        downloader, selection = self.prepare_download()
        self.torrent.title = "Phony S01 2026 1080p DSNP WEB-DL H.264 DDP5.1-HHWEB"
        with patch("brushflow.logger") as log:
            self.assertIsNone(self.plugin._BrushFlow__download(self.torrent))
        downloader.add_torrent.assert_not_called()
        selection.assert_not_called()
        self.plugin.chain.recognize_media.assert_called_once()
        self.assertTrue(any("SDR" in call.args[0] and "推断" in call.args[0] for call in log.info.call_args_list))

    def test_site_description_and_labels_reach_candidate_comparison(self):
        for field in ("description", "labels"):
            with self.subTest(field=field):
                self.prepare_phony_record()
                self.content = make_torrent([(self.phony_path.replace("H.264", "H.265"), 1109643658)])
                downloader, _ = self.prepare_download()
                self.torrent.title = "Phony S01 2026 1080p WEB-DL H.265"
                setattr(self.torrent, field, ["SDR"] if field == "labels" else "1080p SDR")
                self.assertIsNone(self.plugin._BrushFlow__download(self.torrent))
                downloader.add_torrent.assert_not_called()

    def test_sdr_rule_keeps_different_effect_resolution_episode_or_identity(self):
        self.prepare_phony_record()
        for path in (
            self.phony_path.replace("1080p", "720p"),
            self.phony_path.replace("1080p", "1080p.HDR10"),
            self.phony_path.replace("S01E05", "S01E06"),
            self.phony_path.replace("Phony.", "Phony.(tmdbid=999)."),
        ):
            with self.subTest(path=path):
                self.content = make_torrent([(path, 1109643658)])
                self.assertIsNone(self.plugin._channel_dedup_file_selection(self.content, "Phony S01"))

    def test_inferred_sdr_does_not_bypass_identity_validation(self):
        self.prepare_phony_record()
        for media in (
            None,
            SimpleNamespace(title="Different Show", tmdb_id=275139, type="电视剧"),
        ):
            with self.subTest(media=media):
                self.plugin._channel_identity_cache.clear()
                self.plugin.chain.recognize_media.return_value = media
                self.assertIsNone(self.plugin._channel_dedup_file_selection(self.content, "Phony S01"))

    def test_inferred_sdr_pack_excludes_only_published_episode(self):
        self.prepare_phony_record()
        next_episode = self.phony_path.replace("S01E05", "S01E06")
        self.content = make_torrent([(self.phony_path, 1109643658), (next_episode, 1200000000)])
        downloader, selection = self.prepare_download()
        self.torrent.title = "Phony S01 2026 1080p DSNP WEB-DL H.264 DDP5.1-HHWEB"
        self.assertEqual(self.plugin._BrushFlow__download(self.torrent), "a" * 40)
        self.assertTrue(downloader.add_torrent.call_args.kwargs["is_paused"])
        self.assertEqual(selection.call_args.args[2]["accepted"], {next_episode})

    def test_missing_selected_attribute_logs_reason_and_does_not_guess(self):
        self.plugin._channel_dedup_attributes = ["resolution", "fps"]
        with patch("brushflow.logger") as log:
            self.assertIsNone(self.select())
        self.assertTrue(any("fps" in call.args[0] and "缺少" in call.args[0] for call in log.info.call_args_list))
        self.plugin.chain.recognize_media.assert_not_called()

    def test_combined_episode_file_requires_all_episodes_to_match(self):
        self.content = make_torrent([("Show.S01E01-E02.1080p.H264.mkv", 1000)])
        self.assertIsNone(self.select())
        second = copy.deepcopy(self.record)
        second["episodes"] = [2]
        second["message_id"] = 2
        self.plugin._channel_dedup_state["records"].append(second)
        self.assertIsNotNone(self.select())
        self.plugin._channel_dedup_attributes = ["size"]
        self.record["specs"]["size"] = second["specs"]["size"] = 1000
        self.assertIsNone(self.select())

    def test_expired_and_removed_channel_records_are_pruned_without_rewriting_on_every_check(self):
        stale = {**self.record, "published_at": time.time() - 72 * 3600 - 10}
        removed = {**self.record, "channel": "@removed"}
        self.plugin._channel_dedup_state["records"].extend([stale, removed])
        self.plugin._prune_channel_dedup_records()
        self.assertEqual(self.plugin._channel_dedup_state["records"], [self.record])
        self.plugin._prune_channel_dedup_records()
        self.plugin.save_data.assert_called_once()

    def test_poll_persists_structured_records_cursors_and_no_message_body(self):
        message = {"channel": "@channel", "message_id": 12, "published_at": time.time(),
                   "text": "影视：Show\n季集：S01E03\n质量：1080p H264"}
        bridge = type("Ed2kFolderWatcher", (), {})()
        bridge.read_telegram_channel_messages = Mock(return_value={
            "messages": [message], "cursors": {"@channel": 12}, "errors": [],
        })
        with patch("app.runtime.extensions.plugin.manager.PluginManager") as manager:
            manager.return_value.running_plugins = {"Ed2kFolderWatcher": bridge}
            self.plugin.poll_channel_messages()
            self.plugin.poll_channel_messages()
        records = self.plugin._channel_dedup_state["records"]
        self.assertEqual(len(records), 2)
        self.assertEqual(self.plugin._channel_dedup_state["cursors"]["@channel"], 12)
        self.assertNotIn("text", records[1])
        self.assertTrue(self.plugin._channel_dedup_state["last_poll_ok"])
        self.assertFalse(self.plugin._channel_dedup_lock.locked())
        bridge.read_telegram_channel_messages.assert_called_once()

    def install_bridge(self, result=None, side_effect=None):
        bridge = type("Ed2kFolderWatcher", (), {})()
        bridge.read_telegram_channel_messages = Mock(return_value=result, side_effect=side_effect)
        patcher = patch("app.runtime.extensions.plugin.manager.PluginManager")
        patcher.start().return_value.running_plugins = {"Ed2kFolderWatcher": bridge}
        self.addCleanup(patcher.stop)
        return bridge

    def test_active_idle_and_resumed_activity_intervals(self):
        now = time.time()
        empty = {"messages": [], "cursors": {}, "errors": [], "processed_channels": ["@channel"]}
        activity = {**empty, "cursors": {"@channel": 12}}
        resumed = {**empty, "cursors": {"@channel": 13}}
        bridge = self.install_bridge(side_effect=[activity, empty, empty, empty, resumed])
        with patch("brushflow.time.time") as clock:
            for elapsed in (0, 120, 240, 360):
                clock.return_value = now + elapsed
                self.plugin.poll_channel_messages()
            state = self.plugin._channel_dedup_state
            self.assertEqual(state["idle_polls"], 3)
            self.assertEqual(state["poll_interval_seconds"], 300)
            self.assertEqual(state["next_poll_at"], now + 660)
            clock.return_value = now + 659
            self.plugin.poll_channel_messages()
            self.assertEqual(bridge.read_telegram_channel_messages.call_count, 4)
            clock.return_value = now + 660
            self.plugin.poll_channel_messages()
        state = self.plugin._channel_dedup_state
        self.assertEqual(state["cursors"]["@channel"], 13)
        self.assertEqual(state["idle_polls"], 0)
        self.assertEqual(state["poll_interval_seconds"], 120)
        self.assertEqual(bridge.read_telegram_channel_messages.call_count, 5)

    def test_no_network_or_database_write_before_persisted_deadline(self):
        self.plugin._channel_dedup_state["next_poll_at"] = time.time() + 300
        with patch("app.runtime.extensions.plugin.manager.PluginManager") as manager:
            self.plugin.poll_channel_messages()
        manager.assert_not_called()
        self.plugin.save_data.assert_not_called()
        self.assertFalse(self.plugin._channel_dedup_lock.locked())

    def test_manual_sync_can_bypass_idle_deadline_but_not_thirty_second_cooldown(self):
        now = time.time()
        self.plugin._channel_dedup_state.update({"last_poll_at": now, "next_poll_at": now + 300})
        bridge = self.install_bridge({"messages": [], "cursors": {}, "errors": []})
        with patch("brushflow.time.time", return_value=now + 29):
            self.plugin.poll_channel_messages(force=True)
        bridge.read_telegram_channel_messages.assert_not_called()
        with patch("brushflow.time.time", return_value=now + 30):
            self.plugin.poll_channel_messages(force=True)
            self.plugin.poll_channel_messages(force=True)
        bridge.read_telegram_channel_messages.assert_called_once()

    def test_manual_api_submits_forced_poll_and_reports_cooldown(self):
        with patch("brushflow.ThreadHelper") as helper:
            response = self.plugin.request_channel_dedup_poll()
            self.assertTrue(response.success)
            helper.return_value.submit.assert_called_once_with(self.plugin.poll_channel_messages, True)
            helper.return_value.submit.reset_mock()
            self.plugin._channel_dedup_state["last_poll_at"] = time.time()
            response = self.plugin.request_channel_dedup_poll()
            self.assertIn("刚完成", response.message)
            helper.return_value.submit.assert_not_called()

    def test_pending_channel_rotation_does_not_enter_idle_mode(self):
        now = time.time()
        self.plugin._channel_dedup_channels = ["@channel", "@second"]
        self.plugin._channel_dedup_state["idle_polls"] = 2
        bridge = self.install_bridge({
            "messages": [], "cursors": {}, "errors": [], "processed_channels": ["@channel"],
        })
        with patch("brushflow.time.time", return_value=now):
            self.plugin.poll_channel_messages()
        self.assertEqual(self.plugin._channel_dedup_state["channel_offset"], 1)
        self.assertEqual(self.plugin._channel_dedup_state["idle_polls"], 0)
        self.assertEqual(self.plugin._channel_dedup_state["next_poll_at"], now + 120)
        with patch("brushflow.time.time", return_value=now + 120):
            self.plugin.poll_channel_messages()
        self.assertEqual(bridge.read_telegram_channel_messages.call_args.args[0], ["@second", "@channel"])

    def test_partial_failure_or_full_message_page_does_not_enter_idle_mode(self):
        for result in (
            {"messages": [], "errors": [{"channel": "@channel", "error": "TimeoutError"}]},
            {"messages": [{"channel": "@channel"}] * 200, "errors": []},
            {"messages": [], "errors": [], "processed_channels": []},
        ):
            with self.subTest(result_keys=list(result)):
                self.plugin._channel_dedup_state.update({"idle_polls": 2, "next_poll_at": 0})
                self.install_bridge(result)
                self.plugin.poll_channel_messages()
                self.assertEqual(self.plugin._channel_dedup_state["idle_polls"], 0)
                self.assertEqual(self.plugin._channel_dedup_state["poll_interval_seconds"], 120)

    def test_persisted_account_backoff_blocks_automatic_and_manual_sync_after_reload(self):
        self.plugin._channel_dedup_retry_after = 0
        self.plugin._channel_dedup_state["retry_at"] = time.time() + 600
        with patch("app.runtime.extensions.plugin.manager.PluginManager") as manager:
            self.plugin.poll_channel_messages()
            self.plugin.poll_channel_messages(force=True)
        manager.assert_not_called()
        with patch("brushflow.ThreadHelper") as helper:
            response = self.plugin.request_channel_dedup_poll()
        self.assertIn("退避", response.message)
        helper.return_value.submit.assert_not_called()

    def test_settings_changes_preserve_account_backoff_and_existing_cursors(self):
        deadline = time.time() + 600
        self.plugin._channel_dedup_retry_after = time.monotonic() + 600
        self.plugin._channel_dedup_state.update({"retry_at": deadline, "cursors": {"@channel": 12}})
        self.plugin._save_config = Mock()
        self.plugin._refresh_scheduler = Mock()
        with patch("brushflow.ThreadHelper"):
            self.plugin.update_channel_dedup_settings(ChannelDedupSettingsPayload(
                enabled=True, channels=["@channel", "@second"],
            ))
        state = self.plugin._channel_dedup_state
        self.assertEqual(state["retry_at"], deadline)
        self.assertGreater(self.plugin._channel_dedup_retry_after, time.monotonic())
        self.assertEqual(state["cursors"], {"@channel": 12})

    def test_inflight_settings_change_still_persists_telegram_backoff(self):
        error = ValueError("private details")
        error.retry_after = 900

        def fail_after_settings_change(*args):
            self.plugin._channel_generation += 1
            self.plugin._channel_dedup_state["channel_offset"] = 7
            raise error

        self.install_bridge(side_effect=fail_after_settings_change)
        self.plugin.poll_channel_messages()
        state = self.plugin._channel_dedup_state
        self.assertGreaterEqual(state["retry_at"], time.time() + 899)
        self.assertEqual(state["channel_offset"], 7)
        self.assertEqual(state["records"], [self.record])
        self.plugin.save_data.assert_called_once()

    def test_channel_status_exposes_interval_and_next_deadline(self):
        deadline = time.time() + 300
        self.plugin._channel_dedup_state.update({
            "poll_interval_seconds": 300, "next_poll_at": deadline, "retry_at": deadline + 100,
        })
        status = self.plugin._channel_dedup_payload()
        self.assertEqual(status["poll_interval_seconds"], 300)
        self.assertEqual(status["next_poll_at"], deadline + 100)

    def test_failed_poll_preserves_previous_records_and_retries_with_backoff(self):
        bridge = type("Ed2kFolderWatcher", (), {})()
        bridge.read_telegram_channel_messages = Mock(side_effect=ValueError("private details"))
        with patch("app.runtime.extensions.plugin.manager.PluginManager") as manager:
            manager.return_value.running_plugins = {"Ed2kFolderWatcher": bridge}
            self.plugin.poll_channel_messages()
            self.plugin.poll_channel_messages()
        bridge.read_telegram_channel_messages.assert_called_once()
        self.assertEqual(self.plugin._channel_dedup_state["records"], [self.record])
        self.assertEqual(self.plugin._channel_dedup_state["last_error"], "ValueError")

    def test_all_matched_torrent_is_rejected_before_adding(self):
        self.content = make_torrent([self.paths[0]])
        downloader, selection = self.prepare_download()
        self.assertIsNone(self.plugin._BrushFlow__download(self.torrent))
        downloader.add_torrent.assert_not_called()
        selection.assert_not_called()

    def test_partial_pack_is_added_paused_and_only_remaining_files_are_selected(self):
        downloader, selection = self.prepare_download()
        self.assertEqual(self.plugin._BrushFlow__download(self.torrent), "a" * 40)
        self.assertTrue(downloader.add_torrent.call_args.kwargs["is_paused"])
        self.assertEqual(selection.call_args.args[2]["accepted"], {self.paths[1][0], self.paths[2][0]})

    def test_channel_rule_and_recent_rule_intersect_before_download(self):
        downloader, selection = self.prepare_download(recent=True, accepted={self.paths[0][0], self.paths[1][0]})
        self.assertEqual(self.plugin._BrushFlow__download(self.torrent), "a" * 40)
        self.assertEqual(selection.call_args.args[2]["accepted"], {self.paths[1][0]})
        self.assertTrue(downloader.add_torrent.call_args.kwargs["is_paused"])

    def test_recent_selection_all_matched_is_not_added(self):
        downloader, selection = self.prepare_download(recent=True, accepted={self.paths[0][0]})
        self.assertIsNone(self.plugin._BrushFlow__download(self.torrent))
        downloader.add_torrent.assert_not_called()

    def prepare_download(self, recent=False, accepted=None):
        task = BrushTaskConfig({"name": "test", "downloader": "QB", "recent_episodes_only": recent})
        self.plugin._get_task_config = Mock(return_value=task)
        self.plugin._recent_episode_file_selection = Mock(return_value={"accepted": accepted})
        self.plugin._select_recent_episode_files = Mock(return_value=True)
        downloader = Mock()
        downloader.get_torrents.return_value = ([], False)
        downloader.add_torrent.return_value = (True, ["a" * 40])
        self.torrent = TorrentInfo(title="Show.S01", enclosure="https://example.test/download")
        for name, value in (("downloader", downloader), ("service_info", SimpleNamespace(name="QB"))):
            patcher = patch.object(BrushFlow, name, new_callable=PropertyMock, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("brushflow.DownloadChain")
        patcher.start().return_value.download_torrent.return_value = (self.content, None, None)
        self.addCleanup(patcher.stop)
        patcher = patch("brushflow.TorrentHelper.get_torrent_hash", return_value="a" * 40)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("brushflow.DownloaderHelper")
        patcher.start().return_value.is_downloader.side_effect = lambda kind, **kwargs: kind == "qbittorrent"
        self.addCleanup(patcher.stop)
        return downloader, self.plugin._select_recent_episode_files


class ChannelSettingsTests(unittest.TestCase):
    def test_channel_usernames_urls_numeric_ids_are_normalized(self):
        settings = ChannelDedupSettingsPayload(channels="https://t.me/example\n@example\n-1001234567890")
        self.assertEqual(settings.channels, ["@example", "-1001234567890"])

    def test_invalid_attributes_and_invite_links_are_rejected(self):
        with self.assertRaises(ValueError):
            ChannelDedupSettingsPayload(attributes=["unsupported"])
        with self.assertRaises(ValueError):
            ChannelDedupSettingsPayload(channels=["https://t.me/+private"])


if __name__ == "__main__":
    unittest.main()
