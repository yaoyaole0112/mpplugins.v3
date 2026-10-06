import ast
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Dict
from unittest import TestCase
from unittest.mock import Mock


def load_remove(namespace):
    source = Path(__file__).resolve().parents[1] / "helper/life/client.py"
    tree = ast.parse(source.read_text())
    monitor = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MonitorLife"
    )
    method = next(
        node for node in monitor.body
        if isinstance(node, ast.FunctionDef) and node.name == "remove"
    )
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    namespace.update({"Any": Any, "Dict": Dict, "Path": Path})
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["remove"]


class TestLifeDeleteSafety(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.local_path = Path(self.directory.name) / "movie.strm"
        self.local_path.write_text("http://example.invalid/video")
        self.database = Mock()
        self.database.get_by_id.return_value = {"path": "/media/movie.mkv"}
        self.chain = SimpleNamespace(
            get_file_item_strict=Mock(return_value=None),
            get_file_item=Mock(return_value=None),
        )
        self.logger = Mock()
        path_utils = Mock()
        path_utils.get_media_path.return_value = (True, self.directory.name, "/media")
        path_utils.sanitize_path_parts.side_effect = lambda path: path
        config = SimpleNamespace(
            get_config=lambda key: False,
            pan_transfer_unrecognized_path=None,
            storage_module="115网盘",
            monitor_life_remove_mp_history=False,
        )
        self.history_helper = Mock()
        self.rmtree = Mock()
        namespace = {
            "FileDbHelper": Mock(return_value=self.database),
            "StorageChain": Mock(return_value=self.chain),
            "configer": config,
            "PathUtils": path_utils,
            "StrmGenerater": SimpleNamespace(get_strm_filename=lambda path: "movie.strm"),
            "PathRemoveUtils": Mock(),
            "MediaSyncDelHelper": self.history_helper,
            "logger": self.logger,
            "rmtree": self.rmtree,
        }
        self.remove = load_remove(namespace)
        self.monitor = SimpleNamespace(rmt_mediaext_set={".mkv"})
        self.event = {"file_id": 1, "file_category": 1, "file_name": "movie.mkv"}

    def assert_preserved(self):
        self.assertTrue(self.local_path.exists())
        self.database.remove_by_path_batch.assert_not_called()
        self.history_helper.assert_not_called()
        self.rmtree.assert_not_called()
        self.chain.get_file_item.assert_not_called()

    def test_query_errors_preserve_local_file_and_history(self):
        for error in (RuntimeError("HTTP 405"), RuntimeError("HTTP 401"), TimeoutError()):
            with self.subTest(error=error):
                self.chain.get_file_item_strict.side_effect = error
                self.remove(self.monitor, self.event)
                self.assert_preserved()

    def test_folder_query_failure_does_not_remove_tree(self):
        self.event["file_category"] = 0
        self.chain.get_file_item_strict.side_effect = RuntimeError("HTTP 405")
        self.remove(self.monitor, self.event)
        self.assert_preserved()

    def test_old_host_without_strict_query_preserves_local_file(self):
        del self.chain.get_file_item_strict
        self.remove(self.monitor, self.event)
        self.assert_preserved()
        self.logger.warning.assert_called_once()

    def test_non_callable_strict_query_preserves_local_file(self):
        self.chain.get_file_item_strict = None
        self.remove(self.monitor, self.event)
        self.assert_preserved()

    def test_existing_remote_item_preserves_local_file(self):
        remote = object()
        self.chain.get_file_item_strict.return_value = remote
        self.remove(self.monitor, self.event)
        self.assert_preserved()
        self.database.upsert_batch.assert_called_once()

    def test_confirmed_absence_allows_local_deletion(self):
        self.remove(self.monitor, self.event)
        self.assertFalse(self.local_path.exists())
        self.chain.get_file_item_strict.assert_called_once_with(
            storage="115网盘", path=Path("/media/movie.mkv")
        )
        self.database.remove_by_path_batch.assert_called_once_with(
            path="/media/movie.mkv", only_file=False
        )
        self.chain.get_file_item.assert_not_called()
