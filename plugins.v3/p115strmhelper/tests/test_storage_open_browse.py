import ast
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional
from unittest import TestCase
from unittest.mock import Mock


class TestStorageOpenBrowse(TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[2] / "p115disk/p115_api.py"
        method = next(node for node in ast.walk(ast.parse(source.read_text()))
                      if isinstance(node, ast.FunctionDef) and node.name == "list")
        self.provider = Mock()
        namespace = {"FileItem": object, "List": List, "Optional": Optional,
                     "U115Pan": lambda: self.provider, "logger": Mock()}
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        exec(compile(module, str(source), "exec"), namespace)
        self.browse = namespace["list"]
        self.api = SimpleNamespace(_list_rate_limiter=Mock(), _disk_name="115网盘Plus",
                                   detail=Mock(return_value="cookie-file"))
        self.fileitem = Mock(type="dir", path="/")

    def test_open_results_preserve_plus_storage(self):
        child = Mock()
        child.model_copy.return_value = "plus-child"
        self.provider.list.return_value = [child]
        self.assertEqual(self.browse(self.api, self.fileitem), ["plus-child"])
        self.fileitem.model_copy.assert_called_once_with(update={"storage": "u115"})
        child.model_copy.assert_called_once_with(update={"storage": "115网盘Plus"})

    def test_empty_open_directory_is_not_requeried(self):
        self.provider.list.return_value = []
        self.assertEqual(self.browse(self.api, self.fileitem), [])
        self.api.detail.assert_not_called()

    def test_open_failure_falls_back_to_cookie(self):
        self.fileitem.type = "file"
        self.provider.list.side_effect = TimeoutError("network")
        self.assertEqual(self.browse(self.api, self.fileitem), ["cookie-file"])
        self.api.detail.assert_called_once_with(self.fileitem)
