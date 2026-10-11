import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest import TestCase
from unittest.mock import Mock


class TestRenameCenterFallback(TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / "__init__.py"
        tree = ast.parse(source.read_text())
        method = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "resolve_media_info"
        )
        self.logger = Mock()
        self.client = Mock()
        self.utils = Mock()
        self.utils.emby_mediainfo_to_rename_fields.return_value = {}
        self.utils.ffprobe_get_media_info.return_value = ({"videoCodec": "H265"}, "")
        self.item = SimpleNamespace(storage="115网盘Plus", fileid="42")
        self.cache = {"42": {"sha1": "abc", "size": 123}}
        namespace = {
            "Dict": Dict, "Any": Any, "FileItem": object,
            "P115Center": Mock(return_value=self.client),
            "RenameDictUtils": self.utils,
            "pantransfercacher": SimpleNamespace(file_item_dict=self.cache),
            "settings": SimpleNamespace(PORT=3001),
            "share_strm_center": Mock(), "logger": self.logger,
        }
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        exec(compile(module, str(source), "exec"), namespace)
        self.resolve = namespace["resolve_media_info"]

    def test_empty_center_falls_back_without_warning(self):
        self.client.download_emby_mediainfo_data.return_value = {"ABC": None}
        self.assertEqual(self.resolve("/movie.mkv", self.item), {"videoCodec": "H265"})
        self.utils.ffprobe_get_media_info.assert_called_once()
        self.logger.warning.assert_not_called()
        self.logger.error.assert_not_called()
        self.assertNotIn("42", self.cache)

    def test_missing_sha1_is_normal_miss(self):
        self.client.download_emby_mediainfo_data.return_value = {}
        self.assertTrue(self.resolve("/movie.mkv", self.item))
        self.utils.emby_mediainfo_to_rename_fields.assert_called_once_with(None)
        self.logger.warning.assert_not_called()

    def test_center_hit_skips_probe(self):
        self.client.download_emby_mediainfo_data.return_value = {"ABC": {}}
        self.utils.emby_mediainfo_to_rename_fields.return_value = {"effect": "HDR"}
        self.assertEqual(self.resolve("/movie.mkv", self.item), {"effect": "HDR"})
        self.utils.ffprobe_get_media_info.assert_not_called()

    def test_center_exception_keeps_warning_and_fallback(self):
        self.client.download_emby_mediainfo_data.side_effect = TimeoutError("center timeout")
        self.assertTrue(self.resolve("/movie.mkv", self.item))
        self.logger.warning.assert_called_once()
        self.utils.ffprobe_get_media_info.assert_called_once()

    def test_probe_timeout_remains_error(self):
        self.client.download_emby_mediainfo_data.return_value = {}
        self.utils.ffprobe_get_media_info.return_value = (None, "ffprobe 超时(60s)")
        self.assertEqual(self.resolve("/movie.mkv", self.item), {})
        self.logger.error.assert_called_once()
        message = self.logger.error.call_args.args[0]
        self.assertIn("/movie.mkv", message)
        self.assertIn("超时", message)
        self.assertIn("保留原有命名字段", message)

    def test_local_file_does_not_query_center(self):
        self.item.storage = "local"
        self.assertTrue(self.resolve("/movie.mkv", self.item))
        self.client.download_emby_mediainfo_data.assert_not_called()
        self.assertEqual(
            self.utils.ffprobe_get_media_info.call_args.kwargs["source_path"],
            "/movie.mkv",
        )
