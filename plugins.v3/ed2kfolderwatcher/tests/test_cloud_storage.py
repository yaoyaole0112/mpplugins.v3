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
    def test_transient_read_errors_are_not_identity_failures(self):
        from urllib3_future.exceptions import ReadTimeoutError, MaxRetryError
        timeout = ReadTimeoutError(None, "https://private.invalid", "timeout")
        for error in (timeout, MaxRetryError(None, "private", timeout), TimeoutError(), ConnectionError(), httpx.ReadTimeout("private")):
            self.assertTrue(cloud.transient_read_error(error))
        for error in (cloud.CloudError("SHA1 changed"), ValueError("bad metadata"), RuntimeError("unknown")):
            self.assertFalse(cloud.transient_read_error(error))

    def deletion_fixture(self):
        storage = object.__new__(cloud.CloudStorage)
        storage.storage = "115网盘Plus"
        storage.oper = Mock()
        storage.oper.client.user_id = 321
        item = {"storage": "115网盘Plus", "fileid": "123", "type": "file", "name": "a.mkv", "path": "/test/a.mkv", "size": 5, "modify_time": 1}
        storage.list = Mock(return_value=[item])
        storage.delete_path = Mock(return_value=item["path"])
        storage.oper.client.fs_file_skim.return_value = {"state": True, "data": [{"file_id": "123", "sha1": "A" * 40}]}
        tools = types.ModuleType("app.plugins.p115disk.tools")
        tools.get_ios_ua_app = lambda **kwargs: {}
        module_patch = patch.dict(sys.modules, {tools.__name__: tools})
        module_patch.start()
        self.addCleanup(module_patch.stop)
        return storage, item, storage.delete_scope("/test", True)

    def test_delete_scope_binds_account_directory_and_recursion(self):
        storage, item, scope = self.deletion_fixture()
        self.assertNotEqual(scope, storage.delete_scope("/test", False))
        self.assertNotEqual(scope, storage.delete_scope("/other", True))
        storage.oper.client.user_id = 654
        self.assertNotEqual(scope, storage.delete_scope("/test", True))
        for root in ("/", "relative", "/test/../other"):
            with self.subTest(root=root), self.assertRaises(cloud.CloudError):
                storage.delete_scope(root, True)
        storage.storage = "CloudDrive储存"
        with self.assertRaises(cloud.CloudError):
            storage.delete_scope("/test", True)

    def test_prepare_delete_requires_identity_and_verified_sha1(self):
        storage, item, scope = self.deletion_fixture()
        self.assertEqual(storage.prepare_delete(item, "a" * 40, "/test", True, scope), item)
        for changed in ({"size": 6}, {"type": "dir"}, {"path": "/other/a.mkv"}, {"fileid": "456"}):
            storage.list.return_value = [{**item, **changed}]
            with self.subTest(changed=changed), self.assertRaises(cloud.CloudError):
                storage.prepare_delete(item, "a" * 40, "/test", True, scope)
        storage.list.return_value = [item]
        for sha1 in (None, "wrong", "b" * 40):
            with self.subTest(sha1=sha1), self.assertRaises(cloud.CloudError):
                storage.prepare_delete(item, sha1, "/test", True, scope)
        storage.oper.client.fs_delete.assert_not_called()

    def test_delete_uses_refreshed_path_and_cancels_if_file_moved(self):
        storage, item, scope = self.deletion_fixture()
        storage.delete_path.return_value = "/outside/a.mkv"
        with self.assertRaises(cloud.CloudError):
            storage.prepare_delete(item, "a" * 40, "/test", True, scope)
        with self.assertRaises(cloud.CloudError):
            storage.delete_prepared(item, "/test", True, scope, threading.Event())
        storage.oper.client.fs_delete.assert_not_called()

    def test_delete_by_id_is_limited_strict_and_never_falls_back(self):
        storage, item, scope = self.deletion_fixture()
        storage.oper.client.fs_delete.return_value = {"state": True}
        self.assertTrue(storage.delete_prepared(item, "/test", True, scope, threading.Event()))
        storage.oper.client.fs_delete.assert_called_once_with("123", timeout=30)
        storage.oper._delete_rate_limiter.acquire.assert_called_once()
        storage.oper._id_cache.remove.assert_called_once_with(123)
        storage.oper.delete.assert_not_called()
        storage.oper.client.fs_delete.return_value = {"state": False}
        self.assertFalse(storage.delete_prepared(item, "/test", True, scope, threading.Event()))
        for result in (None, {}, {"state": 1}, {"state": "true"}):
            storage.oper.client.fs_delete.return_value = result
            with self.subTest(result=result), self.assertRaises(cloud.CloudError):
                storage.delete_prepared(item, "/test", True, scope, threading.Event())

    def test_delete_cancels_on_stop_account_change_and_outside_scope(self):
        storage, item, scope = self.deletion_fixture()
        stopping = threading.Event()
        stopping.set()
        with self.assertRaises(cloud.CloudError):
            storage.delete_prepared(item, "/test", True, scope, stopping)
        for changed in ({"path": "/other/a.mkv"}, {"type": "dir"}, {"fileid": "0"}):
            with self.subTest(changed=changed), self.assertRaises(cloud.CloudError):
                storage.delete_prepared({**item, **changed}, "/test", True, scope, threading.Event())
        storage.oper.client.user_id = 654
        with self.assertRaises(cloud.CloudError):
            storage.delete_prepared(item, "/test", True, scope, threading.Event())
        storage.oper.client.fs_delete.assert_not_called()

    def test_sha1_query_rejects_ambiguous_or_invalid_metadata(self):
        storage, item, scope = self.deletion_fixture()
        for result in ({"state": False}, {"state": True, "data": []}, {"state": True, "data": [{"file_id": "456", "sha1": "a" * 40}]}, {"state": True, "data": [{"file_id": "123", "sha1": "bad"}]}):
            storage.oper.client.fs_file_skim.return_value = result
            with self.subTest(result=result), self.assertRaises(cloud.CloudError):
                storage.file_sha1(item)

    @staticmethod
    def life_event(**values):
        event = {field: None for field in cloud.LIFE_EVENT_FIELDS}
        event.update(values)
        return event

    def retry_fixture(self, handler):
        storage, item = self.remote_fixture()
        stopping = Mock()
        stopping.is_set.return_value = False
        stopping.wait.return_value = False
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return storage, item, stopping, client

    def test_timeout_resumes_at_exact_offset_without_rehashing_prefix(self):
        requests = []
        class BrokenStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b"hell"
                raise httpx.ReadTimeout("private signed URL must not leak")
        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, headers={"ETag": '"same-file"', "Content-Length": "5"}, stream=BrokenStream())
            return httpx.Response(206, content=b"o", headers={"ETag": '"same-file"', "Content-Range": "bytes 4-4/5"})
        storage, item, stopping, client = self.retry_fixture(handler)
        callback = Mock()
        with patch.object(cloud, "READ_SIZE", 4), patch.object(cloud.httpx, "Client", return_value=client):
            digest = cloud.hash_remote(storage, item, stopping, md4, 3, on_retry=callback)
        self.assertEqual(digest, md4(md4(b"hel") + md4(b"lo")).hex())
        self.assertEqual(requests[1].headers["Range"], "bytes=4-")
        self.assertEqual(requests[1].headers["If-Range"], '"same-file"')
        self.assertEqual(storage.download_request.call_count, 2)
        callback.assert_called_once_with(1)

    def test_resume_rejects_ignored_range_wrong_offset_or_changed_etag(self):
        class BrokenStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b"hell"
                raise httpx.ReadTimeout("timeout")
        for status, headers in ((200, {}), (206, {"Content-Range": "bytes 0-0/5", "ETag": '"same"'}), (206, {"Content-Range": "bytes 4-4/5", "ETag": '"changed"'})):
            responses = [httpx.Response(200, headers={"ETag": '"same"'}, stream=BrokenStream()), httpx.Response(status, content=b"o", headers=headers)]
            storage, item, stopping, client = self.retry_fixture(lambda request: responses.pop(0))
            with self.subTest(status=status, headers=headers), patch.object(cloud, "READ_SIZE", 4), patch.object(cloud.httpx, "Client", return_value=client), self.assertRaises(cloud.CloudError):
                cloud.hash_remote(storage, item, stopping, md4, 3)

    def test_partial_timeout_without_validator_never_splices_unverified_data(self):
        class BrokenStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b"hell"
                raise httpx.ReadTimeout("timeout")
        storage, item, stopping, client = self.retry_fixture(lambda request: httpx.Response(200, stream=BrokenStream()))
        with patch.object(cloud, "READ_SIZE", 4), patch.object(cloud.httpx, "Client", return_value=client), self.assertRaises(cloud.CloudError) as failure:
            cloud.hash_remote(storage, item, stopping, md4, 3)
        self.assertIn("不能安全续读", str(failure.exception))
        storage.download_request.assert_called_once()

    def test_network_retries_are_bounded_and_use_configured_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout("private url")
        storage, item, stopping, client = self.retry_fixture(handler)
        with patch.object(cloud.httpx, "Client", return_value=client) as factory, self.assertRaises(cloud.CloudError):
            cloud.hash_remote(storage, item, stopping, md4, 3, read_timeout=600)
        self.assertEqual(storage.download_request.call_count, 3)
        self.assertEqual(factory.call_args.kwargs["timeout"].read, 600)
        self.assertEqual(stopping.wait.call_count, 2)

    def test_life_direct_request_is_bounded_and_sanitizes_failure(self):
        storage = object.__new__(cloud.CloudStorage)
        storage.storage = "115网盘Plus"
        storage.oper = Mock()
        storage.oper.client.life_behavior_detail.return_value = {"state": True, "data": {"list": [{"id": "42", "type": 1, "update_time": 123, "private": "secret"}]}}
        self.assertEqual(storage.life_events("direct"), [self.life_event(id="42", type=1, update_time=123)])
        storage.oper.client.life_behavior_detail.assert_called_once_with({"limit": 64, "offset": 0})
        storage.oper.client.life_behavior_detail.return_value = {"state": False, "error": "secret"}
        with self.assertRaises(cloud.CloudError) as failure:
            storage.life_events("direct")
        self.assertNotIn("secret", str(failure.exception))

    def test_shared_life_never_falls_back_to_network_when_unavailable(self):
        storage = object.__new__(cloud.CloudStorage)
        storage.storage = "115网盘Plus"
        storage.oper = Mock()
        with patch.dict(sys.modules, {"app.plugins.p115strmhelper.service": None}), self.assertRaises(cloud.CloudError):
            storage.life_events("shared")
        storage.oper.client.life_behavior_detail.assert_not_called()

    def test_life_event_resolves_only_current_file_path(self):
        storage = object.__new__(cloud.CloudStorage)
        storage.storage = "115网盘Plus"
        storage.oper = Mock()
        resolver = Mock(return_value="/监控/新文件.mkv")
        attr = types.ModuleType("p115client.tool.attr")
        attr.get_path = resolver
        modules = {
            "p115client": types.ModuleType("p115client"),
            "p115client.tool": types.ModuleType("p115client.tool"),
            "p115client.tool.attr": attr,
        }
        event = self.life_event(file_id="123", file_category=1, file_name="旧名称.mkv", file_size="5", pick_code="pick", sha1="ABC", update_time="456")
        with patch.dict(sys.modules, modules):
            item = storage.item_from_life_event(event)
        self.assertEqual(item, {"storage": "115网盘Plus", "fileid": "123", "type": "file", "path": "/监控/新文件.mkv", "name": "新文件.mkv", "size": 5, "modify_time": 456, "pickcode": "pick", "sha1": "ABC"})
        resolver.assert_called_once_with(client=storage.oper.client, attr="123", root_id=None, ensure_file=True, refresh=True, app="android")
        storage.oper.get_item_strict.assert_not_called()
        self.assertIsNone(storage.item_from_life_event(self.life_event(file_id="124", file_category=0)))

    def test_life_event_fetches_metadata_only_when_event_is_incomplete(self):
        storage = object.__new__(cloud.CloudStorage)
        storage.storage = "115网盘Plus"
        storage.oper = Mock()
        storage.oper.get_item_strict.return_value = {"storage": "u115", "fileid": "123", "type": "file", "path": "/监控/新文件.mkv", "name": "新文件.mkv", "size": 5, "modify_time": 456, "pickcode": "pick"}
        attr = types.ModuleType("p115client.tool.attr")
        attr.get_path = Mock(return_value="/监控/新文件.mkv")
        modules = {"p115client": types.ModuleType("p115client"), "p115client.tool": types.ModuleType("p115client.tool"), "p115client.tool.attr": attr}
        with patch.dict(sys.modules, modules):
            item = storage.item_from_life_event(self.life_event(file_id="123", file_category=1))
        self.assertEqual(item["size"], 5)
        self.assertEqual(item["pickcode"], "pick")
        storage.oper.get_item_strict.assert_called_once_with(Path("/监控/新文件.mkv"))

    def test_life_event_lookup_failures_are_sanitized_and_not_silently_discarded(self):
        storage = object.__new__(cloud.CloudStorage)
        storage.storage = "115网盘Plus"
        storage.oper = Mock()
        attr = types.ModuleType("p115client.tool.attr")
        attr.get_path = Mock(side_effect=TimeoutError("secret signed URL"))
        modules = {"p115client": types.ModuleType("p115client"), "p115client.tool": types.ModuleType("p115client.tool"), "p115client.tool.attr": attr}
        event = self.life_event(file_id="123", file_category=1)
        with patch.dict(sys.modules, modules):
            with self.assertRaises(cloud.CloudError) as failure:
                storage.item_from_life_event(event)
            self.assertNotIn("secret", str(failure.exception))
            self.assertIn("TimeoutError", str(failure.exception))
            attr.get_path.side_effect = FileNotFoundError("secret path")
            self.assertIsNone(storage.item_from_life_event(event))
            attr.get_path.side_effect = None
            attr.get_path.return_value = "/监控/新文件.mkv"
            storage.oper.get_item_strict.side_effect = RuntimeError("secret cookie")
            with self.assertRaises(cloud.CloudError) as failure:
                storage.item_from_life_event(event)
            self.assertNotIn("secret", str(failure.exception))
            self.assertIn("RuntimeError", str(failure.exception))

    def test_shared_life_reads_existing_database_without_calling_115(self):
        from sqlalchemy import create_engine, Integer
        from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
        class Base(DeclarativeBase):
            pass
        class LifeEvent(Base):
            __tablename__ = "life_event"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            type: Mapped[int] = mapped_column(Integer)
            update_time: Mapped[int] = mapped_column(Integer)
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(LifeEvent(id=7, type=1, update_time=123))
            session.commit()
        storage = object.__new__(cloud.CloudStorage)
        storage.storage = "115网盘Plus"
        storage.oper = Mock()
        storage.oper.client.user_id = 42
        worker = Mock()
        worker.client.user_id = 42
        database = Mock()
        database.ct_db_manager.get_database.return_value.session.side_effect = lambda: Session(engine)
        modules = {"app.plugins.p115strmhelper.service": types.SimpleNamespace(servicer=worker), "app.plugins.p115strmhelper.db_manager": database, "app.plugins.p115strmhelper.db_manager.models.life_event": types.SimpleNamespace(LifeEvent=LifeEvent)}
        with patch.dict(sys.modules, modules):
            self.assertEqual(storage.life_events("shared"), [self.life_event(id=7, type=1, update_time=123)])
            with Session(engine) as session:
                session.add_all([LifeEvent(id=index, type=1, update_time=200) for index in range(8, 138)])
                session.commit()
            first = storage.life_events("shared", after_id=7, since_time=150)
            second = storage.life_events("shared", after_id=first[-1]["id"], since_time=150)
            third = storage.life_events("shared", after_id=second[-1]["id"], since_time=150)
            self.assertEqual([row["id"] for row in first+second+third], list(range(8, 138)))
            self.assertEqual(storage.life_source_latest, 200)
            self.assertEqual(storage.life_events("shared", after_id=0, since_time=201), [])
            worker.client.user_id = 99
            with self.assertRaises(cloud.CloudError):
                storage.life_events("shared")
        storage.oper.client.life_behavior_detail.assert_not_called()

    def test_timeout_retry_stops_if_metadata_changes_or_cancelled(self):
        def handler(request):
            raise httpx.ReadTimeout("timeout")
        for cancelled in (False, True):
            storage, item, stopping, client = self.retry_fixture(handler)
            storage.fresh.return_value = {**item, "size": 6}
            stopping.wait.return_value = cancelled
            expected = InterruptedError if cancelled else cloud.CloudError
            with self.subTest(cancelled=cancelled), patch.object(cloud.httpx, "Client", return_value=client), self.assertRaises(expected):
                cloud.hash_remote(storage, item, stopping, md4, 3)
            storage.download_request.assert_called_once()

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
