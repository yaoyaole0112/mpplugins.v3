"""共享审批门禁与跨设备同集资源的端到端回归。"""

import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from resourceflow import ResourceFlow
from resourceflow.shared_coordinator import CoordinatorStore
from resourceflow.shared_models import SharedSettingsPayload
from resourceflow.shared_protocol import SharedError
import test_shared_arbitration as arbitration


class SharedDownloadGateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = arbitration.AdmissionTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.plugin = self.fixture.plugin
        self.task = self.fixture.task

    def test_task_on_with_global_off_never_falls_back_to_local_download(self):
        self.plugin._shared_settings = SharedSettingsPayload(enabled=False)
        self.assertIsNone(self.fixture.add())
        self.plugin._shared_exchange.assert_not_called()
        self.plugin._add_local_prechecked_torrent.assert_not_called()
        self.fixture.downloader.start_torrents.assert_not_called()

    def test_task_on_with_missing_global_config_never_falls_back(self):
        del self.plugin._shared_settings
        self.assertIsNone(self.fixture.add())
        self.plugin._add_local_prechecked_torrent.assert_not_called()

    def test_disabled_task_still_explicitly_allows_local_download(self):
        self.task.spec_dedup_enabled = False
        self.plugin._shared_settings = SharedSettingsPayload(enabled=False)
        with patch("resourceflow.shared_plugin.logger") as logger:
            self.assertEqual(self.fixture.add(), "a" * 40)
        self.plugin._shared_exchange.assert_not_called()
        self.assertIn("任务开关关闭", str(logger.info.call_args_list))

    def test_download_entry_rejects_disabled_global_before_contacting_site(self):
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._shared_settings = SharedSettingsPayload(enabled=False)
        plugin._get_task_config = Mock(return_value=self.task)
        torrent = SimpleNamespace(title="Show", enclosure="https://example.org/download")
        with patch("resourceflow.DownloadChain") as chain:
            self.assertIsNone(plugin._ResourceFlow__download(torrent))
        chain.assert_not_called()

    def test_brush_batch_reports_disabled_global_without_reading_site(self):
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._shared_settings = SharedSettingsPayload(enabled=False)
        plugin._validate_task_reference = Mock()
        report = plugin._new_run_report("brush")
        plugin._run_brush(self.task, report)
        self.assertEqual(report["result"], "shared_approval_disabled")
        self.assertEqual(report["reason_counts"]["任务要求共享审批，但全局审批未启用"], 1)
        plugin._validate_task_reference.assert_not_called()

    def test_downloader_add_boundary_requires_exact_approved_selection(self):
        self.fixture.add()
        entry = self.fixture.entry()
        entry["phase"] = "adding"
        selection = self.plugin._add_local_prechecked_torrent.call_args.args[6]
        self.assertTrue(self.plugin._shared_add_authorized(self.task, "a" * 40, selection))
        variants = [
            None,
            {**selection, "hold_paused": False},
            {**selection, "shared_tag": "resourceflow-claim-" + "f" * 32},
            {**selection, "accepted": {"Show.S01E02.mkv"}},
            {**selection, "accepted": set()},
        ]
        for candidate in variants:
            with self.subTest(selection=candidate):
                self.assertFalse(self.plugin._shared_add_authorized(self.task, "a" * 40, candidate))
        for field, value in (("phase", "rejected"), ("hash", "b" * 40),
                             ("task_id", "other"), ("downloader", "other"),
                             ("approved", {})):
            original = entry[field]
            with self.subTest(field=field):
                entry[field] = value
                self.assertFalse(self.plugin._shared_add_authorized(self.task, "a" * 40, selection))
            entry[field] = original

    def test_direct_downloader_helper_cannot_bypass_shared_gate(self):
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._shared_settings = self.plugin._shared_settings
        plugin._shared_claims = {}
        downloader = Mock()
        self.assertIsNone(plugin._add_local_prechecked_torrent(
            b"torrent", None, self.task, downloader, Mock(), "a" * 40, None, "Show"))
        downloader.add_torrent.assert_not_called()

    def test_valid_approval_reaches_real_add_helper_paused_then_starts(self):
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin.__dict__.update(self.plugin.__dict__)
        del plugin._add_local_prechecked_torrent
        plugin._select_recent_episode_files = Mock(return_value=True)
        self.task.up_speed = self.task.dl_speed = None
        self.task.save_path = self.task.qb_category = None
        self.task.brush_tag = "resourceflow-test"
        downloader = self.fixture.downloader
        downloader.add_torrent.return_value = (True, ["a" * 40])
        helper = Mock()
        helper.is_downloader.side_effect = lambda kind, **kwargs: kind == "qbittorrent"
        with patch("resourceflow.DownloaderHelper", return_value=helper):
            result = plugin._add_prechecked_torrent(
                b"torrent", None, self.task, downloader, Mock(), "a" * 40, None, "Show")
        self.assertEqual(result, "a" * 40)
        self.assertTrue(downloader.add_torrent.call_args.kwargs["is_paused"])
        entry = next(iter(plugin._shared_claims.values()))
        self.assertIn(plugin._shared_tag(entry), downloader.add_torrent.call_args.kwargs["tag"])
        downloader.start_torrents.assert_called_once_with("a" * 40)

    def test_rejected_or_released_entry_cannot_be_started(self):
        self.fixture.add()
        entry = self.fixture.entry()
        self.fixture.downloader.start_torrents.reset_mock()
        self.plugin._shared_exchange.reset_mock()
        for phase in ("rejected", "released", "completed"):
            entry["phase"] = phase
            with self.subTest(phase=phase), self.assertRaises(SharedError):
                self.plugin._start_shared_download(entry, self.task, self.fixture.downloader)
        self.fixture.downloader.start_torrents.assert_not_called()
        self.plugin._shared_exchange.assert_not_called()

    def test_real_coordinator_denial_never_adds_or_starts_peer_resource(self):
        """重放截图同集同规格、不同命名及大小，保留真实事务和客户端门禁。"""
        with TemporaryDirectory() as directory:
            store = CoordinatorStore(
                Path(directory) / "claims.sqlite3", "resources", ["resolution", "effects"], 5,
                self.plugin._shared_rules())
            self.plugin._channel_dedup_attributes = ["resolution", "effects"]
            self.plugin._shared_candidates.return_value = ([{
                **arbitration.file_row(), "tmdb_id": 305227, "episodes": [44],
                "media_title": "丰臣兄弟！",
                "path": "Brothers.in.Arms.S01E44.1080p.LINETV.WEB-DL.AAC2.0.H.264-MWeb.mkv",
                "specs": {"resolution": "1080p", "effects": "SDR", "size": 1025402607},
            }], [])
            self.fixture.downloader.get_files.return_value = [{
                "index": 0, "name": self.plugin._shared_candidates.return_value[0][0]["path"],
                "size": 1025402607, "progress": 0, "priority": 1,
            }]
            self.plugin._shared_instance_id = "a" * 32
            sender = [11]

            def exchange(request_id, body):
                return store.handle(sender[0], request_id, {
                    "v": 1, "group": "resources", "instance": self.plugin._shared_instance_id,
                    "label": "First" if sender[0] == 11 else "Peer", **body,
                })

            self.plugin._shared_exchange.side_effect = exchange
            self.assertEqual(self.fixture.add(), "a" * 40)
            entry = self.fixture.entry()
            self.plugin._shared_update(entry, [
                {"claim": claim, "state": "completed"} for claim in entry["approved"].values()
            ])
            original = copy.deepcopy(entry)
            sender[0] = 22
            self.plugin._shared_instance_id = "b" * 32
            self.plugin._shared_claims = {}
            candidate = self.plugin._shared_candidates.return_value[0][0]
            candidate.update(
                id="2" * 24,
                path="[丰臣兄弟！].Brothers.in.Arms.2026.S01E44.1080p.LINETV.WEB-DL.H264.AAC-UBWEB.mkv")
            candidate["specs"]["size"] = 1026658099
            self.plugin._add_local_prechecked_torrent.reset_mock()
            self.fixture.downloader.start_torrents.reset_mock()
            with patch("resourceflow.shared_plugin.logger") as logger:
                self.assertIsNone(self.plugin._add_prechecked_torrent(
                    b"torrent", None, self.task, self.fixture.downloader, Mock(),
                    "b" * 40, None, "Brothers in Arms S01E44 1080p UBWEB"))
            self.plugin._add_local_prechecked_torrent.assert_not_called()
            self.fixture.downloader.start_torrents.assert_not_called()
            self.assertEqual(self.fixture.entry()["phase"], "rejected")
            self.assertEqual(len(store.recent()), 1)
            self.assertEqual(store.recent()[0]["state"], "completed")
            output = str(logger.info.call_args_list)
            self.assertIn("TMDB 305227", output)
            self.assertIn("S01E44", output)
            self.assertIn("First", output)
            self.assertIn(self.fixture.entry()["request_id"], output)
            self.assertNotEqual(original["request_id"], self.fixture.entry()["request_id"])
