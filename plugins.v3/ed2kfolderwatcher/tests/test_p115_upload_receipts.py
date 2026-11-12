import importlib.util
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


class TestP115UploadReceipts(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "sample.mkv"
        self.source.write_bytes(b"sample-data")
        self.store = self.root / "shared" / "receipts.json"

        config = types.ModuleType("app.sdk.config")
        config.settings = types.SimpleNamespace(PLUGIN_DATA_PATH=self.root / "shared")
        app_module = types.ModuleType("app")
        app_module.__path__ = []
        sdk_module = types.ModuleType("app.sdk")
        sdk_module.__path__ = []
        self.module_patch = patch.dict(
            sys.modules,
            {"app": app_module, "app.sdk": sdk_module, "app.sdk.config": config},
        )
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        module_key = "ed2k_test_p115_upload_receipts"
        spec = importlib.util.spec_from_file_location(
            module_key,
            Path(__file__).resolve().parents[2] / "p115disk" / "upload_receipts.py",
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.target = {
            "storage": "115网盘Plus",
            "fileid": "12345",
            "type": "file",
            "path": "/整理/样例.mkv",
            "name": "样例.mkv",
            "size": self.source.stat().st_size,
        }

    def test_receipt_matches_source_and_target_once(self):
        self.assertTrue(self.module.record_upload_result(self.source, self.target, "ordinary"))
        identity = self.module._identity(self.source)

        result = self.module.consume_upload_result(self.source, identity, self.target)

        self.assertEqual(result["upload_kind"], "ordinary")
        self.assertIsNone(self.module.consume_upload_result(self.source, identity, self.target))

    def test_receipt_does_not_match_changed_source_or_different_target(self):
        self.module.record_upload_result(self.source, self.target, "ordinary")
        identity = self.module._identity(self.source)
        changed_target = {**self.target, "fileid": "99999"}

        self.assertIsNone(self.module.consume_upload_result(self.source, identity, changed_target))
        self.source.write_bytes(b"changed-source")
        self.assertIsNone(
            self.module.consume_upload_result(
                self.source,
                [11, 1, 1, 1, 1],
                self.target,
            )
        )

    def test_invalid_upload_kind_is_rejected(self):
        self.assertFalse(self.module.record_upload_result(self.source, self.target, "unknown"))


if __name__ == "__main__":
    unittest.main()
