import hashlib
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

import test_ed2kfolderwatcher as existing


class UploadReadbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not hasattr(existing.Ed2kTests, "module"):
            existing.Ed2kTests.setUpClass()

    def setUp(self):
        self.namespace = existing.Ed2kTests.upload_module.verify_cloud_readback.__globals__
        self.verify = existing.Ed2kTests.upload_module.verify_cloud_readback
        self.error = existing.Ed2kTests.cloud_module.CloudError
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "a.mkv"
        self.data = bytes(range(251)) * 3000
        self.path.write_bytes(self.data)
        self.sha1 = hashlib.sha1(self.data).hexdigest()
        self.item = {"storage": "115网盘Plus", "fileid": "123", "path": "/upload/a.mkv", "size": len(self.data)}
        self.storage = Mock()
        self.storage.download_request.return_value = ("https://cloud.example/a", {})
        self.stopping = threading.Event()
        self.requests = []

    def response(self, request):
        self.requests.append(request)
        start, end = map(int, request.headers["range"].removeprefix("bytes=").split("-"))
        return httpx.Response(206, headers={"Content-Range": f"bytes {start}-{end}/{len(self.data)}"},
                              content=self.data[start:end + 1])

    def call(self, handler=None):
        actual_client = httpx.Client
        transport = httpx.MockTransport(handler or self.response)
        with patch.object(self.namespace["httpx"], "Client", side_effect=lambda **kwargs: actual_client(transport=transport, **kwargs)):
            self.verify(self.storage, self.item, self.path, self.sha1, self.stopping)

    def test_reads_only_three_small_ranges_and_compares_local_bytes(self):
        self.call()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.requests[-1].headers["range"], f"bytes={len(self.data)-65536}-{len(self.data)-1}")
        self.storage.download_request.assert_called_once()

    def test_rejects_no_range_wrong_size_short_body_and_wrong_bytes(self):
        handlers = (
            lambda request: httpx.Response(200, content=self.data),
            lambda request: httpx.Response(206, headers={"Content-Range": "bytes 0-65535/1"}, content=b"a"),
            lambda request: httpx.Response(206, headers={"Content-Range": f"bytes 0-65535/{len(self.data)}"}, content=self.data[:100]),
            lambda request: httpx.Response(206, headers={"Content-Range": f"bytes 0-65535/{len(self.data)}"}, content=b"x" * 65536),
        )
        for handler in handlers:
            with self.subTest(handler=handler), self.assertRaises(self.error):
                self.call(handler)

    def test_stop_does_not_request_cloud_bytes(self):
        self.stopping.set()
        with self.assertRaises(InterruptedError):
            self.call()
        self.storage.download_request.assert_not_called()

    def test_clouddrive_proxy_is_not_used_as_cloud_readback_evidence(self):
        self.item["storage"] = "CloudDrive储存"
        self.storage.oper.client.get_download_url.return_value = types.SimpleNamespace(directUrl="", downloadUrlPath="/cache/a")
        native = Mock()
        native.oper.client.fs_file_skim.return_value = {"state": True, "data": [{
            "file_id": "123", "file_size": str(len(self.data)), "sha1": self.sha1, "pick_code": "pick",
        }]}
        native.download_request.return_value = ("https://cloud.example/a", {})
        tools = types.ModuleType("app.plugins.p115disk.tools")
        tools.get_ios_ua_app = lambda **kwargs: {}
        with patch.dict(sys.modules, {"app.plugins.p115disk.tools": tools}), \
                patch.dict(self.namespace, {"CloudStorage": Mock(return_value=native)}):
            self.call()
        self.storage.download_request.assert_not_called()
        native.download_request.assert_called_once()

    def test_clouddrive_direct_url_is_read_without_proxy(self):
        self.item["storage"] = "CloudDrive储存"
        self.storage.oper.client.get_download_url.return_value = types.SimpleNamespace(
            directUrl="https://cloud.example/a", userAgent="test-agent", additionalHeaders={},
        )
        self.call()
        self.storage.download_request.assert_not_called()
        self.assertEqual(self.requests[0].headers["user-agent"], "test-agent")

    def test_missing_native_account_or_wrong_native_identity_blocks_readback(self):
        self.item["storage"] = "CloudDrive储存"
        self.storage.oper.client.get_download_url.return_value = types.SimpleNamespace(directUrl="")
        with patch.dict(self.namespace, {"CloudStorage": Mock(side_effect=self.error("not configured"))}):
            with self.assertRaisesRegex(self.error, "请启用"):
                self.call()
        native = Mock()
        native.oper.client.fs_file_skim.return_value = {"state": True, "data": [{
            "file_id": "another", "file_size": str(len(self.data)), "sha1": self.sha1, "pick_code": "pick",
        }]}
        tools = types.ModuleType("app.plugins.p115disk.tools")
        tools.get_ios_ua_app = lambda **kwargs: {}
        with patch.dict(sys.modules, {"app.plugins.p115disk.tools": tools}), \
                patch.dict(self.namespace, {"CloudStorage": Mock(return_value=native)}):
            with self.assertRaisesRegex(self.error, "未匹配"):
                self.call()
        native.download_request.assert_not_called()
        self.storage.download_request.assert_not_called()

    def test_small_file_uses_one_bounded_request(self):
        self.data = b"small"
        self.path.write_bytes(self.data)
        self.item["size"] = len(self.data)
        self.call()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].headers["range"], "bytes=0-4")
