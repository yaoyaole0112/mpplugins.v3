import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterator
from unittest import TestCase
from unittest.mock import MagicMock, Mock


class FakeFileTooBig(Exception):
    pass


def load_export_methods():
    source = Path(__file__).resolve().parents[1] / "helper/strm/increment.py"
    tree = ast.parse(source.read_text())
    helper_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "IncrementSyncStrmHelper"
    )
    methods = [
        node for node in helper_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"__wait_export_dir", "__iter_export_dir_paths"}
    ]
    module = ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[]))
    logger = Mock()
    status = Mock()
    namespace = {
        "Callable": Callable,
        "Iterator": Iterator,
        "P115FileTooBig": FakeFileTooBig,
        "configer": SimpleNamespace(
            get_ios_ua_app=lambda app=False: {},
            increment_sync_itertree_timeout_seconds=60,
        ),
        "export_dir_iter_line": lambda file: iter(file),
        "export_dir_parse_iter_path": lambda lines, escape: (
            escape(path) for path in lines
        ),
        "export_dir_status": status,
        "logger": logger,
        "perf_counter": lambda: 0,
        "sleep": Mock(),
    }
    exec(compile(module, str(source), "exec"), namespace)
    return namespace, logger, status


class TestIncrementExportDir(TestCase):
    def setUp(self):
        self.namespace, self.logger, self.status = load_export_methods()
        self.export_file = MagicMock()
        self.export_file.__iter__.return_value = iter(["/root", "/root/movie.mkv"])
        self.client = Mock()
        self.client.download_url.return_value = "https://example.com/export.txt"
        self.client.open.return_value = self.export_file
        self.helper = SimpleNamespace(client=self.client, api_count=0)
        setattr(self.helper, "__wait_export_dir", Mock(return_value=42))
        self.iter_paths = self.namespace["__iter_export_dir_paths"]

    def test_wait_returns_exported_file_id(self):
        self.status.return_value = {"file_id": "42"}
        self.helper._make_throttled_export_dir_wait_logger = Mock(return_value=Mock())

        self.assertEqual(self.namespace["__wait_export_dir"](self.helper, 7), 42)
        self.assertEqual(self.helper.api_count, 1)

    def test_parses_and_cleans_up_export_file(self):
        paths = list(self.iter_paths(self.helper, 7, str.upper))

        self.assertEqual(paths, ["/ROOT", "/ROOT/MOVIE.MKV"])
        self.client.download_url.assert_called_once_with(42, app="web")
        self.export_file.close.assert_called_once()
        self.client.fs_delete.assert_called_once_with(42)
        self.assertEqual(self.helper.api_count, 2)

    def test_read_error_survives_cleanup_failures(self):
        def interrupted():
            yield "/root"
            raise OSError("download interrupted")

        self.export_file.__iter__.return_value = interrupted()
        self.export_file.close.side_effect = OSError("close failed")
        self.client.fs_delete.side_effect = OSError("delete failed")

        with self.assertRaisesRegex(OSError, "download interrupted"):
            list(self.iter_paths(self.helper, 7, str))

        self.export_file.close.assert_called_once()
        self.client.fs_delete.assert_called_once_with(42)
        self.assertEqual(self.logger.warning.call_count, 2)

    def test_close_partial_iterator_releases_export(self):
        iterator = self.iter_paths(self.helper, 7, str)
        self.assertEqual(next(iterator), "/root")

        iterator.close()

        self.export_file.close.assert_called_once()
        self.client.fs_delete.assert_called_once_with(42)

    def test_download_error_still_deletes_export(self):
        self.client.download_url.side_effect = OSError("download unavailable")

        with self.assertRaisesRegex(OSError, "download unavailable"):
            list(self.iter_paths(self.helper, 7, str))

        self.client.fs_delete.assert_called_once_with(42)
        self.export_file.close.assert_not_called()

    def test_large_export_retries_with_android(self):
        self.client.download_url.side_effect = [
            FakeFileTooBig(), "https://example.com/large-export.txt"
        ]

        self.assertEqual(len(list(self.iter_paths(self.helper, 7, str))), 2)
        self.assertEqual(
            self.client.download_url.call_args_list[1].kwargs, {"app": "android"}
        )
        self.client.fs_delete.assert_called_once_with(42)

    def test_wait_error_does_not_delete_unknown_file(self):
        getattr(self.helper, "__wait_export_dir").side_effect = TimeoutError("wait")

        with self.assertRaises(TimeoutError):
            list(self.iter_paths(self.helper, 7, str))

        self.client.fs_delete.assert_not_called()
