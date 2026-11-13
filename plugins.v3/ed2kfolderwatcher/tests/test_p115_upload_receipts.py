import hashlib
import importlib.util
import os
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

    def test_content_proof_recovers_receipt_after_metadata_change(self):
        checksum = hashlib.sha1(self.source.read_bytes()).hexdigest()
        self.module.record_upload_result(self.source, self.target, "instant", source_sha1=checksum)
        os.utime(self.source, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        identity = self.module._identity(self.source)
        self.assertIsNone(self.module.consume_upload_result(self.source, identity, self.target))
        result = self.module.consume_upload_result(self.source, identity, self.target, source_sha1=checksum)
        self.assertEqual(result["upload_kind"], "instant")

    def test_content_fallback_requires_matching_digest_and_live_source_identity(self):
        checksum = hashlib.sha1(self.source.read_bytes()).hexdigest()
        self.module.record_upload_result(self.source, self.target, "ordinary", source_sha1=checksum)
        os.utime(self.source, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        identity = self.module._identity(self.source)
        self.assertIsNone(self.module.consume_upload_result(self.source, identity, self.target, source_sha1="0" * 40))
        self.assertIsNone(self.module.consume_upload_result(self.source, [0] * 5, self.target, source_sha1=checksum))
        self.assertIsNone(self.module.consume_upload_result(
            self.source, identity, {**self.target, "fileid": "999"}, source_sha1=checksum))


if __name__ == "__main__":
    unittest.main()
