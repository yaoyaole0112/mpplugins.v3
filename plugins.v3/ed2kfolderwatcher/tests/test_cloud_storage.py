import importlib.util
import threading
import unittest
import httpx
import sys
import types
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ed2k_cloud_storage_test", ROOT / "cloud_storage.py")
cloud = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cloud)


def md4(data):
    from Crypto.Hash import MD4
    return MD4.new(data).digest()


class CloudStorageTests(unittest.TestCase):
    def test_fingerprint_ignores_only_download_identifier(self):
        item = {"storage": "115网盘Plus", "fileid": "1", "path": "/test/a.mkv", "name": "a.mkv", "size": 5, "modify_time": 1, "pickcode": "old"}
        self.assertEqual(cloud.fingerprint(item), cloud.fingerprint({**item, "pickcode": "new"}))
        for field, value in (("storage", "CloudDrive储存"), ("fileid", "2"), ("path", "/test/b.mkv"), ("name", "b.mkv"), ("size", 6), ("modify_time", 2)):
            with self.subTest(field=field):
                self.assertNotEqual(cloud.fingerprint(item), cloud.fingerprint({**item, field: value}))

    def test_legacy_fingerprint_matches_without_download_identifier(self):
        item = {"storage": "115网盘Plus", "fileid": "1", "path": "/test/a.mkv", "name": "a.mkv", "size": 5, "modify_time": 1, "pickcode": "new"}
        current = cloud.fingerprint(item)
        self.assertTrue(cloud.fingerprint_matches(current, item))
        self.assertTrue(cloud.fingerprint_matches(current + ["old"], item))
        self.assertFalse(cloud.fingerprint_matches(current + ["old"], {**item, "size": 6}))
        for saved in (None, {}, "invalid", [], current[:-1], current + ["old", "extra"]):
            with self.subTest(saved=saved):
                self.assertFalse(cloud.fingerprint_matches(saved, item))

    def test_cloud_paths_reject_traversal_and_normalize(self):
        self.assertEqual(cloud.cloud_path("/media//shows/"), "/media/shows")
        for path in ("relative", "/../outside", "/media/../outside", "/media\\outside"):
            with self.subTest(path=path), self.assertRaises(cloud.CloudError):
                cloud.cloud_path(path)

    def test_item_must_remain_inside_selected_storage_folder(self):
        item = {"storage": "115网盘Plus", "type": "file", "path": "/media/shows/a.mkv"}
        self.assertTrue(cloud.allowed_item(item, "115网盘Plus", "/media"))
        self.assertFalse(cloud.allowed_item({**item, "path": "/media-old/a.mkv"}, "115网盘Plus", "/media"))
        self.assertFalse(cloud.allowed_item({**item, "storage": "CloudDrive储存"}, "115网盘Plus", "/media"))

    def test_stream_hash_matches_small_file_digest_across_transport_chunks(self):
        data = b"MoviePilot cloud stream" * 2000
        chunks = [data[index:index + 113] for index in range(0, len(data), 113)]
        result = cloud.hash_stream(iter(chunks), len(data), threading.Event(), md4, 1024)
        parts = [md4(data[index:index + 1024]) for index in range(0, len(data), 1024)]
        expected = md4(b"".join(parts)).hex()
        self.assertEqual(result, expected)

    def test_exact_part_boundary_matches_existing_ed2k_empty_terminal_part(self):
        data = b"a" * 4096
        result = cloud.hash_stream([data[:3000], data[3000:]], len(data), threading.Event(), md4, len(data))
        self.assertEqual(result, md4(md4(data) + md4(b"")).hex())

    def test_rejects_empty_short_long_and_cancelled_streams(self):
        for size, chunks in ((0, []), (5, [b"1234"]), (4, [b"12345"])):
            with self.subTest(size=size), self.assertRaises(cloud.CloudError):
                cloud.hash_stream(chunks, size, threading.Event(), md4, 3)
        stopping = threading.Event()
        stopping.set()
        with self.assertRaises(InterruptedError):
            cloud.hash_stream([b"1234"], 4, stopping, md4, 3)

    def test_rejects_sha1_mismatch(self):
        with self.assertRaises(cloud.CloudError):
            cloud.hash_stream([b"data"], 4, threading.Event(), md4, 8, sha1="0" * 40)

    def remote_fixture(self, data=b"hello"):
        item = {"storage": "115网盘Plus", "fileid": "1", "path": "/test/a.mkv", "name": "a.mkv", "size": len(data), "modify_time": 1, "pickcode": "pick"}
        storage = Mock()
        storage.download_request.return_value = ("https://origin.test/file?secret=hidden", {"Cookie": "credential", "Authorization": "credential", "User-Agent": "test"})
        storage.fresh.return_value = item
        return storage, item

    def test_remote_stream_uses_chunks_and_verifies_fresh_metadata(self):
        storage, item = self.remote_fixture()
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"hello", headers={"Content-Length": "5"})))
        with patch.object(cloud.httpx, "Client", return_value=client):
            result = cloud.hash_remote(storage, item, threading.Event(), md4, 1024)
        self.assertEqual(result, md4(b"hello").hex())
        storage.fresh.assert_called_once_with(item)

    def test_remote_rejects_changed_metadata(self):
        storage, item = self.remote_fixture()
        storage.fresh.return_value = {**item, "modify_time": 2}
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"hello")))
        with patch.object(cloud.httpx, "Client", return_value=client), self.assertRaises(cloud.CloudError):
            cloud.hash_remote(storage, item, threading.Event(), md4, 1024)

    def test_remote_accepts_download_identifier_refresh_after_hashing(self):
        storage, item = self.remote_fixture()
        storage.fresh.return_value = {**item, "pickcode": "refreshed"}
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"hello")))
        with patch.object(cloud.httpx, "Client", return_value=client):
            result = cloud.hash_remote(storage, item, threading.Event(), md4, 1024)
        self.assertEqual(result, md4(b"hello").hex())

    def test_remote_cancel_remains_interrupted_instead_of_network_failure(self):
        storage, item = self.remote_fixture()
        stopping = threading.Event()
        stopping.set()
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"hello")))
        with patch.object(cloud.httpx, "Client", return_value=client), self.assertRaises(InterruptedError):
            cloud.hash_remote(storage, item, stopping, md4, 1024)

    def test_remote_rejects_partial_html_and_wrong_size_responses(self):
        for status, headers in ((206, {}), (200, {"Content-Length": "6"}), (200, {"Content-Type": "text/html"})):
            storage, item = self.remote_fixture()
            client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, content=b"hello", headers=headers)))
            with self.subTest(status=status, headers=headers), patch.object(cloud.httpx, "Client", return_value=client), self.assertRaises(cloud.CloudError):
                cloud.hash_remote(storage, item, threading.Event(), md4, 1024)

    def test_redirect_does_not_leak_credentials_to_other_origin(self):
        storage, item = self.remote_fixture()
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.host == "origin.test":
                return httpx.Response(302, headers={"Location": "https://cdn.test/file"})
            return httpx.Response(200, content=b"hello")
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.object(cloud.httpx, "Client", return_value=client):
            cloud.hash_remote(storage, item, threading.Event(), md4, 1024)
        self.assertEqual(len(requests), 2)
        self.assertNotIn("cookie", requests[1].headers)
        self.assertNotIn("authorization", requests[1].headers)

    def test_network_error_does_not_expose_signed_url(self):
        storage, item = self.remote_fixture()
        def handler(request):
            raise httpx.ReadError("failed https://origin.test/file?secret=hidden")
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.object(cloud.httpx, "Client", return_value=client), self.assertRaises(cloud.CloudError) as failure:
            cloud.hash_remote(storage, item, threading.Event(), md4, 1024)
        self.assertNotIn("hidden", str(failure.exception))

    def test_storage_listing_keeps_required_directory_slash_and_bounds(self):
        storage = object.__new__(cloud.CloudStorage)
        storage.storage = "115网盘Plus"
        entry = {"storage": "115网盘Plus", "fileid": "1", "path": "/test/a.mkv", "type": "file", "name": "a.mkv", "size": 5}
        storage.oper = Mock()
        storage.oper.list.return_value = [entry, {**entry, "path": "/outside/a.mkv"}]
        schemas = types.ModuleType("app.schemas")
        schemas.FileItem = lambda **values: types.SimpleNamespace(**values)
        with patch.dict(sys.modules, {"app.schemas": schemas}):
            self.assertEqual(storage.list("/test"), [entry])
        self.assertEqual(storage.oper.list.call_args.args[0].path, "/test/")

    def test_download_requests_reuse_provider_headers_without_downloading(self):
        settings_module = types.ModuleType("app.core.config")
        settings_module.settings = types.SimpleNamespace(USER_AGENT="MoviePilot-test")
        storage = object.__new__(cloud.CloudStorage)
        storage.oper = Mock()
        storage.storage = "115网盘Plus"
        storage.oper.client.download_url.return_value = types.SimpleNamespace(geturl=lambda: "https://cdn.test/file", headers={"Cookie": "test-only"})
        with patch.dict(sys.modules, {"app.core.config": settings_module}):
            url, headers = storage.download_request({"pickcode": "test-code"})
        self.assertEqual(url, "https://cdn.test/file")
        self.assertEqual(headers["Cookie"], "test-only")
        storage.oper.download.assert_not_called()
        storage.storage = "CloudDrive储存"
        storage.oper._download_base = "http://clouddrive.test:19798"
        storage.oper.client.get_download_url.return_value = types.SimpleNamespace(directUrl="", downloadUrlPath="/static/file?preview={PREVIEW}")
        with patch.dict(sys.modules, {"app.core.config": settings_module}):
            url, headers = storage.download_request({"path": "/test/a.mkv"})
        self.assertEqual(url, "http://clouddrive.test:19798/static/file?preview=false")
        self.assertEqual(headers["Accept-Encoding"], "identity")
        storage.oper.download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
