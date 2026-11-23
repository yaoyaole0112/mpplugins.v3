import re
import threading
import time
import unittest
from datetime import date, timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from resourceflow import ResourceFlow, BrushTaskConfig, TorrentHelper, TorrentInfo
from resourceflow.models import ResourcePendingApprovalPayload
from resourceflow.shared_plugin import SharedSettingsPayload
from test_channel_dedup import bencode


TORRENT_CONTENT = (
    b"d4:infod6:lengthi1e4:name8:test.mkv12:piece lengthi16384e"
    b"6:pieces20:aaaaaaaaaaaaaaaaaaaaee"
)


class DownloadDedupTest(unittest.TestCase):
    def setUp(self):
        self.plugin = ResourceFlow.__new__(ResourceFlow)
        self.plugin._mp_transfer_exclude_patterns = Mock(return_value=())
        self.task = BrushTaskConfig({
            "name": "test", "downloader": "QB", "tag": "resourceflow-test",
            "spec_dedup_enabled": False,
        })
        self.plugin._get_task_config = Mock(return_value=self.task)
        self.plugin._recent_episode_file_selection = Mock(return_value={"accepted": {"test.mkv"}})
        self.plugin._select_recent_episode_files = Mock(return_value=True)
        self.downloader = Mock()
        self.downloader.get_torrents.return_value = ([], False)
        self.expected_hash = TorrentHelper.get_torrent_hash(TORRENT_CONTENT)
        self.assertIsNotNone(self.expected_hash)
        self.downloader.get_torrent_id_by_tag.return_value = self.expected_hash
        self.downloader.add_torrent.return_value = (True, [self.expected_hash])
        self.service_type = "qbittorrent"
        self.torrent = TorrentInfo(
            title="Site.Title.S01E09", enclosure="https://example.org/download.php?id=1",
        )
        for name, value in (("downloader", self.downloader), ("service_info", SimpleNamespace(name="QB"))):
            patcher = patch.object(ResourceFlow, name, new_callable=PropertyMock, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        chain = patch("resourceflow.DownloadChain")
        self.download_chain = chain.start()
        self.addCleanup(chain.stop)
        self.download_chain.return_value.download_torrent.return_value = (TORRENT_CONTENT, None, None)
        helper = patch("resourceflow.DownloaderHelper")
        self.downloader_helper = helper.start()
        self.addCleanup(helper.stop)
        self.downloader_helper.return_value.is_downloader.side_effect = (
            lambda kind, **kwargs: kind == self.service_type
        )

    def download(self):
        return self.plugin._ResourceFlow__download(self.torrent)

    def assert_existing_task_untouched(self):
        for method in ("add_torrent", "delete_torrents", "remove_torrents_tag", "get_torrent_id_by_tag",
                       "start_torrents", "change_torrent"):
            getattr(self.downloader, method).assert_not_called()
        self.plugin._select_recent_episode_files.assert_not_called()

    def test_real_torrent_and_magnet_use_the_same_hash(self):
        magnet = f"magnet:?xt=urn:btih:{self.expected_hash.upper()}"
        self.assertEqual(TorrentHelper.get_torrent_hash(magnet), self.expected_hash)

    def test_cached_torrent_bytes_are_passed_separately_from_string_enclosure(self):
        self.torrent.enclosure = None
        result = self.plugin._ResourceFlow__download(
            self.torrent, torrent_content_override=TORRENT_CONTENT,
        )
        self.assertEqual(result, self.expected_hash)
        self.assertEqual(self.downloader.add_torrent.call_args.kwargs["content"], TORRENT_CONTENT)

    def prepare_pending_download(self, *, recent_screened=True):
        p, task = self.plugin, self.task
        task.enabled = task.spec_dedup_enabled = task.recent_episodes_only = True
        task.site_id = 23
        task.pubtime = "0-120"
        self.torrent.site = 23
        self.torrent.site_name = "Fixture"
        self.torrent.pubdate = "2020-01-01 04:00:29"
        self.torrent.size = 17
        self.torrent.downloadvolumefactor = 0
        self.path, self.other = "Show.S01E09.mkv", "Show.S01E10.mkv"
        self.content = bencode({"info": {
            "name": "Show", "piece length": 16384, "pieces": b"a" * 20,
            "files": [{"path": [self.path], "length": 7}, {"path": [self.other], "length": 10}],
        }})
        self.expected_hash = TorrentHelper.get_torrent_hash(self.content)
        self.downloader.add_torrent.return_value = (True, [self.expected_hash])
        self.downloader.get_files.return_value = [
            {"index": 0, "name": self.path, "size": 7, "progress": 0, "priority": 1},
            {"index": 1, "name": self.other, "size": 10, "progress": 0, "priority": 0},
        ]
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p.get_data_path = Mock(return_value=temp.name)
        p.get_state = Mock(return_value=True)
        p._task_configs = {task.id: task}
        p._task_locks, p._task_context = {}, threading.local()
        p._inflight_lock = threading.RLock()
        p._inflight_entries = {}
        p._identity_pending_source_lock = threading.RLock()
        p._identity_pending, p._identity_pending_sources = {}, {}
        p._shared_settings = SharedSettingsPayload(enabled=True, bot_username="@testbot")
        p._shared_stopping = False
        p._shared_rpc_lock, p._shared_lock = threading.Lock(), threading.RLock()
        p._shared_claims = {}
        p._sync_shared_history = Mock()
        p._shared_task_ready = Mock(return_value=True)
        p._channel_dedup_attributes = ["resolution"]
        p._channel_dedup_size_tolerance_pct = 5
        p._channel_dedup_pack_policy = "partial"
        p.save_data = Mock()
        p._validate_task_reference = Mock(return_value=True)
        p._get_task_data = Mock(return_value={})
        p._save_task_data = Mock()
        p._evaluate_site_ratio_control = Mock(return_value=(True, "", {}))
        p._ResourceFlow__evaluate_size_condition_for_brush = Mock(return_value=(True, ""))
        p._ResourceFlow__evaluate_pre_conditions_for_brush = Mock(return_value=(True, ""))
        p._ResourceFlow__send_add_message = Mock()
        site_patch = patch("resourceflow.SiteOper")
        site_patch.start().return_value.get.return_value = SimpleNamespace(id=23, name="Fixture")
        self.addCleanup(site_patch.stop)
        downloader_patch = patch("resourceflow.shared_plugin.DownloaderHelper", return_value=SimpleNamespace(
            get_service=Mock(return_value=SimpleNamespace(instance=self.downloader)),
            is_downloader=Mock(return_value=True),
        ))
        downloader_patch.start()
        self.addCleanup(downloader_patch.stop)
        event_patch = patch.object(ResourceFlow, "eventmanager", create=True)
        event_patch.start()
        self.addCleanup(event_patch.stop)
        self.steps = []
        self.approved = True

        def exchange(request_id, body):
            self.steps.append(body["op"])
            if body["op"] == "reserve":
                file_id, = [row["id"] for row in body["files"]]
                return {
                    "ok": True, "policy": p._local_shared_policy(),
                    "approved": [{"id": file_id, "claim": "b" * 32}] if self.approved else [],
                    "rejected": [] if self.approved else [{"id": file_id, "reason": "duplicate"}],
                }
            return {"ok": True, "updated": len(body["updates"])}

        p._shared_exchange = Mock(side_effect=exchange)
        self.downloader.add_torrent.side_effect = lambda **kw: (
            self.steps.append("add") or (True, [self.expected_hash])
        )
        self.downloader.start_torrents.side_effect = lambda _: self.steps.append("start") or True
        source = {"content": self.content, "task_id": task.id, "hash": self.expected_hash,
                  "title": self.torrent.title, "torrent": p._pending_torrent_snapshot(self.torrent)}
        p._pending_identity(
            {"path": self.path, "media_type": "tv", "tmdb_id": 123, "season": 1, "episodes": [9],
             "specs": {}}, self.torrent.title, task, "缺少勾选规格：resolution", source,
            recent_screened=recent_screened,
        )
        self.key, = p._identity_pending
        self.source_id = p._identity_pending[self.key]["source_id"]

    def submit_pending_download(self, *, tmdb_id=123):
        p = self.plugin
        payload = ResourcePendingApprovalPayload(
            key=self.key, tmdb_id=tmdb_id, media_title="Show", media_type="tv",
            season=1, episodes=[9], specs={"resolution": "1080p"}, confirmed=True,
        )
        with patch("resourceflow.ThreadHelper") as thread:
            thread.return_value.submit.side_effect = lambda fn, *args: fn(*args)
            return p.approve_pending_resource(payload)

    def test_manual_resume_reuses_cached_torrent_after_site_publish_window(self):
        self.prepare_pending_download()
        p = self.plugin
        self.assertEqual(p._ResourceFlow__evaluate_conditions_for_brush(self.torrent, {}),
                         (False, "发布时间不在范围内"))
        with patch.object(p, "_ResourceFlow__evaluate_conditions_for_brush") as rules:
            self.assertTrue(self.submit_pending_download().success)
            rules.assert_not_called()
        self.assertEqual(self.steps, ["reserve", "add", "update", "start"])
        self.assertNotIn(self.key, p._identity_pending)
        self.assertEqual(self.downloader.add_torrent.call_args.kwargs["content"], self.content)
        self.assertTrue(self.downloader.add_torrent.call_args.kwargs["is_paused"])
        self.assertEqual(p._select_recent_episode_files.call_args.args[2]["accepted"], {self.path})
        p._recent_episode_file_selection.assert_not_called()
        self.download_chain.return_value.download_torrent.assert_not_called()

    def test_only_file_that_passed_recent_rule_gets_persisted_proof(self):
        self.prepare_pending_download(recent_screened=False)
        p = self.plugin
        self.download_chain.return_value.download_torrent.return_value = (self.content, None, None)
        p._mp_resource_candidate = Mock(return_value=(
            {"path": self.path, "media_type": "tv", "tmdb_id": 123, "season": 1, "episodes": [9],
             "specs": {}}, None, "",
        ))
        p._recent_episode_file_selection.return_value = {"accepted": {self.path}}
        self.assertIsNone(self.download())
        self.assertEqual(p._mp_resource_candidate.call_count, 1)
        self.assertEqual(p._mp_resource_candidate.call_args.args[0], self.path)
        self.assertTrue(p._identity_pending[self.key]["recent_screened"])
        self.assertEqual(p._identity_pending[self.key]["source_id"], self.source_id)
        self.assertEqual(self.steps, [])
        self.assert_existing_task_untouched()

    def test_manual_resume_coordinator_duplicate_never_reaches_downloader(self):
        self.prepare_pending_download()
        self.approved = False
        self.assertTrue(self.submit_pending_download().success)
        self.assertEqual(self.steps, ["reserve"])
        self.assert_existing_task_untouched()
        row = self.plugin._identity_pending[self.key]
        self.assertEqual(row["manual_state"], "failed")
        self.assertFalse(self.plugin._identity_pending_sources[self.source_id].get("consumed_hash"))

    def test_manual_resume_only_checks_unverified_recent_date_for_selected_file(self):
        self.prepare_pending_download(recent_screened=False)
        p = self.plugin
        p._recent_episode_file_selection = ResourceFlow._recent_episode_file_selection.__get__(p)
        p._mp_file_recognition = Mock(side_effect=AssertionError("unselected file was examined"))
        old_date = (date.today() - timedelta(days=30)).isoformat()
        with patch("resourceflow.TmdbChain") as tmdb:
            tmdb.return_value.tmdb_episodes.return_value = [
                SimpleNamespace(episode_number=9, air_date=old_date),
            ]
            self.assertTrue(self.submit_pending_download().success)
            tmdb.return_value.tmdb_episodes.assert_called_once_with(tmdbid=123, season=1, episode_group=None)
        p._mp_file_recognition.assert_not_called()
        self.assertEqual(self.steps, [])
        self.assertIn("最近播出日期尚未通过", p._identity_pending[self.key]["manual_error"])
        self.assert_existing_task_untouched()

    def test_manual_resume_rechecks_recent_date_if_verified_identity_changes(self):
        self.prepare_pending_download()
        self.plugin._recent_episode_file_selection.return_value = None
        self.assertTrue(self.submit_pending_download(tmdb_id=456).success)
        self.assertEqual(self.plugin._recent_episode_file_selection.call_args.kwargs["only_path"], self.path)
        self.assertEqual(self.steps, [])
        self.assert_existing_task_untouched()

    def test_manual_resume_refuses_expired_torrent_before_approval(self):
        self.prepare_pending_download()
        self.plugin._identity_pending_sources[self.source_id]["expires_at"] = time.time() - 1
        result = self.submit_pending_download()
        self.assertFalse(result.success)
        self.assertIn("过期", result.message)
        self.assertEqual(self.steps, [])
        self.downloader.get_torrents.assert_not_called()
        self.assert_existing_task_untouched()

    def test_existing_hash_is_skipped_before_episode_lookup_for_both_downloaders(self):
        self.task.recent_episodes_only = True
        self.downloader.get_torrents.return_value = ([{"name": "Different.Root.Title.S01"}], False)
        for kind in ("qbittorrent", "transmission"):
            with self.subTest(kind=kind):
                self.service_type = kind
                self.assertIsNone(self.download())
                self.downloader.get_torrents.assert_called_with(ids=[self.expected_hash])
                self.plugin._recent_episode_file_selection.assert_not_called()
                self.assert_existing_task_untouched()

    def test_existing_hash_does_not_parse_files_or_create_pending_identity(self):
        self.plugin._resource_identity_deferred = Mock(return_value=True)
        self.plugin._pending_identity = Mock()
        self.plugin._mp_transfer_exclude_patterns.return_value = (re.compile("Preview", re.I),)
        self.downloader.get_torrents.return_value = ([{"hash": self.expected_hash}], False)
        with patch("resourceflow.parse_torrent_files") as parse, patch("resourceflow.logger") as logger:
            self.assertIsNone(self.download())
        parse.assert_not_called()
        self.plugin._pending_identity.assert_not_called()
        self.assertIn("原任务的添加来源未核实", str(logger.info.call_args_list))
        self.assert_existing_task_untouched()

    def test_transfer_screen_stops_title_before_fetching_torrent(self):
        self.plugin._mp_transfer_exclude_patterns.return_value = (re.compile("加更篇|PREVIEW", re.I),)
        self.torrent.title = "Natural High S04E29 Preview"
        self.assertIsNone(self.download())
        self.download_chain.return_value.download_torrent.assert_not_called()
        self.downloader.get_torrents.assert_not_called()
        self.assert_existing_task_untouched()

    def test_transfer_screen_checks_torrent_folders_and_filenames_including_manual_entry(self):
        self.task.recent_episodes_only = True
        for root, filename in (("Bonus.Pack", "Show.S04E29.mkv"), ("Show", "Show.S04E29.加更篇.mkv")):
            content = bencode({"info": {
                "name": root, "piece length": 16384, "pieces": b"a" * 20,
                "files": [{"path": [filename], "length": 7}, {"path": ["Show.S04E28.mkv"], "length": 10}],
            }})
            self.plugin._mp_transfer_exclude_patterns.return_value = (re.compile("加更篇|bonus", re.I),)
            for manual in (None, "Show.S04E28.mkv"):
                with self.subTest(root=root, manual=manual):
                    self.assertIsNone(self.plugin._ResourceFlow__download(
                        self.torrent, manual_path=manual, torrent_content_override=content,
                    ))
                    self.plugin._recent_episode_file_selection.assert_not_called()
                    self.assert_existing_task_untouched()

    def test_transfer_screen_leaves_clean_torrent_eligible(self):
        self.plugin._mp_transfer_exclude_patterns.return_value = (re.compile("加更篇|preview", re.I),)
        with patch("resourceflow.logger") as logger:
            self.assertEqual(self.download(), self.expected_hash)
        self.assertIn("入口 自动刷流", str(logger.info.call_args_list))
        self.assertIn(f"Hash {self.expected_hash}；共享审批 未启用", str(logger.info.call_args_list))

    def test_magnet_is_held_if_transfer_screen_cannot_check_paths(self):
        self.torrent.enclosure = f"magnet:?xt=urn:btih:{self.expected_hash}"
        self.plugin._mp_transfer_exclude_patterns.return_value = (re.compile("preview", re.I),)
        self.assertIsNone(self.download())
        self.downloader.get_torrents.assert_called_once_with(ids=[self.expected_hash])
        self.assert_existing_task_untouched()

    def test_reads_current_mp_transfer_words_as_case_insensitive_regex(self):
        with patch("resourceflow.get_configured_system_config") as config:
            config.return_value.get.return_value = ["", r"加更篇|PREVIEW\b"]
            patterns = ResourceFlow._mp_transfer_exclude_patterns()
            config.return_value.get.assert_called_once()
            self.assertEqual(config.return_value.get.call_args.args[0].value, "TransferExcludeWords")
            self.assertEqual(len(patterns), 1)
            self.assertTrue(patterns[0].search("a.preview.mkv"))
            config.return_value.get.return_value = ["["]
            self.assertIsNone(ResourceFlow._mp_transfer_exclude_patterns())

    def test_query_failure_prevents_add(self):
        for result in (([], True), ([{"hash": self.expected_hash}], True)):
            with self.subTest(result=result):
                self.downloader.get_torrents.return_value = result
                self.assertIsNone(self.download())
                self.assert_existing_task_untouched()
        self.downloader.get_torrents.side_effect = RuntimeError("unavailable")
        self.assertIsNone(self.download())
        self.assert_existing_task_untouched()

    def test_invalid_torrent_does_not_query_or_add(self):
        self.download_chain.return_value.download_torrent.return_value = (b"not a torrent", None, None)
        self.assertIsNone(self.download())
        self.downloader.get_torrents.assert_not_called()
        self.assert_existing_task_untouched()

    def test_magnet_duplicate_is_skipped_without_downloading_metadata(self):
        self.torrent.enclosure = f"magnet:?xt=urn:btih:{self.expected_hash}"
        self.downloader.get_torrents.return_value = ([{"hash": self.expected_hash}], False)
        self.assertIsNone(self.download())
        self.download_chain.return_value.download_torrent.assert_not_called()
        self.assert_existing_task_untouched()

    def test_failed_add_does_not_wait_for_hash_or_select_files(self):
        self.task.recent_episodes_only = True
        for result in ((False, []), (False, [self.expected_hash]), False, None, ("false", [])):
            with self.subTest(result=result):
                self.downloader.add_torrent.return_value = result
                self.assertIsNone(self.download())
                self.downloader.get_torrent_id_by_tag.assert_not_called()
                self.plugin._select_recent_episode_files.assert_not_called()
                self.downloader.delete_torrents.assert_not_called()

    def test_successful_new_api_uses_returned_hash_and_selects_paused_files(self):
        self.task.recent_episodes_only = True
        self.downloader.add_torrent.return_value = (True, [self.expected_hash.upper()])
        self.assertEqual(self.download(), self.expected_hash)
        self.assertTrue(self.downloader.add_torrent.call_args.kwargs["is_paused"])
        self.downloader.get_torrent_id_by_tag.assert_not_called()
        self.plugin._select_recent_episode_files.assert_called_once_with(
            self.downloader, self.expected_hash,
            self.plugin._recent_episode_file_selection.return_value, "qbittorrent", "test",
        )
        self.downloader.remove_torrents_tag.assert_called_once()

    def test_success_without_returned_hash_uses_legacy_tag_lookup(self):
        for result in (True, (True, [])):
            with self.subTest(result=result):
                self.downloader.add_torrent.return_value = result
                self.downloader.get_torrent_id_by_tag.reset_mock()
                self.assertEqual(self.download(), self.expected_hash)
                self.downloader.get_torrent_id_by_tag.assert_called_once()
                self.assertFalse(self.downloader.add_torrent.call_args.kwargs["is_paused"])

    def test_failed_selection_only_removes_newly_added_paused_task(self):
        self.task.recent_episodes_only = True
        self.plugin._select_recent_episode_files.return_value = False
        self.assertIsNone(self.download())
        self.downloader.delete_torrents.assert_called_once_with(ids=self.expected_hash, delete_file=False)

    def test_shared_selection_failure_keeps_paused_torrent_for_reconciliation(self):
        self.task.recent_episodes_only = True
        selection = {"accepted": {"test.mkv"}, "shared_request_id": "request-test"}
        self.plugin._recent_episode_file_selection.return_value = selection
        self.plugin._select_recent_episode_files.return_value = False

        self.assertEqual(self.download(), self.expected_hash)
        self.assertTrue(selection["selection_pending"])
        self.downloader.delete_torrents.assert_not_called()
        self.downloader.start_torrents.assert_not_called()

    def test_mismatched_tag_hash_is_not_modified(self):
        self.downloader.add_torrent.return_value = True
        self.downloader.get_torrent_id_by_tag.return_value = "f" * 40
        self.assertIsNone(self.download())
        self.plugin._select_recent_episode_files.assert_not_called()
        self.downloader.delete_torrents.assert_not_called()
        self.downloader.remove_torrents_tag.assert_not_called()

    def test_transmission_new_torrent_keeps_selection_workflow(self):
        self.service_type = "transmission"
        self.task.recent_episodes_only = True
        self.downloader.add_torrent.return_value = SimpleNamespace(hashString=self.expected_hash)
        self.assertEqual(self.download(), self.expected_hash)
        self.assertTrue(self.downloader.add_torrent.call_args.kwargs["is_paused"])
        self.plugin._select_recent_episode_files.assert_called_once_with(
            self.downloader, self.expected_hash,
            self.plugin._recent_episode_file_selection.return_value, "transmission", "test",
        )


if __name__ == "__main__":
    unittest.main()
