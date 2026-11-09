import unittest
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from brushflow import BrushFlow, BrushTaskConfig, TorrentHelper, TorrentInfo


TORRENT_CONTENT = (
    b"d4:infod6:lengthi1e4:name8:test.mkv12:piece lengthi16384e"
    b"6:pieces20:aaaaaaaaaaaaaaaaaaaaee"
)


class DownloadDedupTest(unittest.TestCase):
    def setUp(self):
        self.plugin = BrushFlow.__new__(BrushFlow)
        self.task = BrushTaskConfig({
            "name": "test", "downloader": "QB", "tag": "brushflow-test",
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
            patcher = patch.object(BrushFlow, name, new_callable=PropertyMock, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        chain = patch("brushflow.DownloadChain")
        self.download_chain = chain.start()
        self.addCleanup(chain.stop)
        self.download_chain.return_value.download_torrent.return_value = (TORRENT_CONTENT, None, None)
        helper = patch("brushflow.DownloaderHelper")
        self.downloader_helper = helper.start()
        self.addCleanup(helper.stop)
        self.downloader_helper.return_value.is_downloader.side_effect = (
            lambda kind, **kwargs: kind == self.service_type
        )

    def download(self):
        return self.plugin._BrushFlow__download(self.torrent)

    def assert_existing_task_untouched(self):
        for method in ("add_torrent", "delete_torrents", "remove_torrents_tag", "get_torrent_id_by_tag",
                       "start_torrents", "change_torrent"):
            getattr(self.downloader, method).assert_not_called()
        self.plugin._select_recent_episode_files.assert_not_called()

    def test_real_torrent_and_magnet_use_the_same_hash(self):
        magnet = f"magnet:?xt=urn:btih:{self.expected_hash.upper()}"
        self.assertEqual(TorrentHelper.get_torrent_hash(magnet), self.expected_hash)

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
