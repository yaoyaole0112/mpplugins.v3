import hashlib
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

import test_ed2kfolderwatcher as existing


global_vars = Mock()
transfer_process = Mock()
Bucket = Mock()


class PartData:
    def __init__(self, source, size):
        self.source = source
        self.len = size
        self.remaining = size

    def read(self, amount=None):
        if amount is None or amount < 0:
            amount = self.remaining
        chunk = self.source.read(min(amount, self.remaining))
        self.remaining -= len(chunk)
        return chunk


class Fake115:
    def __init__(self):
        self.client = Mock()
        self.sha1_called = False
        self.bucket = None
        self.fail_part = False
        self.stop_during_part = None
        self.token_refresh = False
        self.file_size = 0
        self.started = None
        self.release = None

    def _calc_sha1(self, path):
        self.sha1_called = True
        raise AssertionError("must not reread SHA1")

    def _get_oss_token(self):
        return self.client.upload_gettoken()

    def upload(self, target, local_path, new_name=None):
        if global_vars.is_transfer_stopped(str(local_path)):
            return None
        self.client.upload_file_init(filesha1=self._calc_sha1(local_path))
        self._get_oss_token()
        callback = transfer_process(str(local_path))
        bucket = Bucket("auth", "endpoint", "bucket")
        self.bucket_observed(bucket)
        result = bucket.init_multipart_upload("key")
        if self.started is not None:
            self.started.set()
            self.release.wait(3)
        with local_path.open("rb") as source:
            bucket.upload_part("key", result.upload_id, 1, data=PartData(source, self.file_size // 2))
            callback(50)
            if self.token_refresh:
                bucket = Bucket("auth-new", "endpoint", "bucket")
            bucket.upload_part("key", result.upload_id, 2, data=PartData(source, self.file_size - self.file_size // 2))
        callback(100)
        bucket.complete_multipart_upload("key", result.upload_id, [])
        return {"path": "/upload/" + new_name}

    def bucket_observed(self, bucket):
        self.bucket = bucket


class Instant115(Fake115):
    def upload(self, target, local_path, new_name=None):
        response = self.client.upload_file_init(filesha1=self._calc_sha1(local_path))
        if not response.get("reuse"):
            response = self.client.upload_file_init(filesha1=self._calc_sha1(local_path))
        return {"path": "/upload/" + new_name} if response.get("reuse") else None


class Retry115(Fake115):
    def upload(self, target, local_path, new_name=None):
        self.client.upload_file_init(filesha1=self._calc_sha1(local_path))
        bucket = Bucket("auth", "endpoint", "bucket")
        result = bucket.init_multipart_upload("key")
        with local_path.open("rb") as source:
            try:
                bucket.upload_part("key", result.upload_id, 1, data=PartData(source, self.file_size))
            except TimeoutError:
                source.seek(0)
                bucket.upload_part("key", result.upload_id, 1, data=PartData(source, self.file_size))
        bucket.complete_multipart_upload("key", result.upload_id, [])
        return {"path": "/upload/" + new_name}


class UploadAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not hasattr(existing.Ed2kTests, "module"):
            existing.Ed2kTests.setUpClass()
        cls.upload = existing.Ed2kTests.upload_module
        cls.adapter = existing.Ed2kTests.adapter_module

    def setUp(self):
        import sys
        from unittest.mock import patch
        modules = patch.dict(sys.modules, {"ed2k_test_plugin.local_upload": self.upload})
        modules.start()
        self.addCleanup(modules.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "test.mkv"
        self.path.write_bytes(b"test-content" * 1024)
        self.identity = self.upload.local_identity(self.path.stat())
        self.digest = hashlib.sha1(self.path.read_bytes()).hexdigest()
        self.oper = Fake115()
        self.oper.file_size = self.identity[0]
        self.stopping = threading.Event()
        self.reported = []
        self.checkpoint = Mock()
        self.raw_bucket = Mock()
        self.raw_bucket.init_multipart_upload.return_value = types.SimpleNamespace(upload_id="upload-id")
        self.raw_bucket.complete_multipart_upload.return_value = types.SimpleNamespace(status=200)
        self.sent_parts = []
        def part(key, upload_id, part_number, data, progress_callback):
            if self.oper.stop_during_part:
                self.oper.stop_during_part()
            progress_callback(self.identity[0] // 4, self.identity[0] // 2)
            if self.oper.fail_part:
                raise TimeoutError()
            received = bytearray()
            while chunk := data.read(1024):
                received.extend(chunk)
            self.sent_parts.append(bytes(received))
            progress_callback(self.identity[0] // 2, self.identity[0] // 2)
            return types.SimpleNamespace(etag="etag")
        self.raw_bucket.upload_part.side_effect = part
        global_vars.reset_mock()
        global_vars.is_transfer_stopped.return_value = False
        transfer_process.reset_mock()
        Bucket.reset_mock()
        Bucket.return_value = self.raw_bucket

    def call(self, oper=None, rate=0):
        return self.adapter.upload_with_digest(
            oper or self.oper, None, self.path, self.digest, self.identity,
            self.stopping, self.reported.append, self.checkpoint, rate,
        )

    def test_instant_upload_check_uses_cached_sha1_and_validates_sample_range(self):
        self.oper.client.upload_file_init.return_value = {"data": {"status": 2}}
        result = self.adapter.instant_upload_check(
            self.oper, self.path, self.digest, self.identity[0], "123",
        )
        self.assertTrue(result)
        arguments = self.oper.client.upload_file_init.call_args.args
        self.assertEqual(arguments[:3], (self.path.name, self.digest, self.identity[0]))
        self.assertEqual(arguments[5], "U_1_123")
        self.assertEqual(arguments[4]("2-8"), hashlib.sha1(self.path.read_bytes()[2:9]).hexdigest().upper())
        for invalid in ("bad", "-1-8", "10-2", "bytes=2-8junk", "2-8,10-12"):
            with self.subTest(invalid=invalid), self.assertRaises(self.adapter.CloudError):
                arguments[4](invalid)

    def test_actual_uploaded_bytes_match_cached_checksum_without_rereading(self):
        self.call()
        self.assertEqual(b"".join(self.sent_parts), self.path.read_bytes())
        self.raw_bucket.complete_multipart_upload.assert_called_once()
        self.assertFalse(self.oper.sha1_called)

    def test_oss_sdk_progress_adapter_preserves_body_length_and_checksum(self):
        try:
            from oss2.utils import make_progress_adapter
        except ImportError:
            self.skipTest("OSS SDK is not available")
        sent = bytearray()
        def part(key, upload_id, part_number, data, progress_callback):
            adapted = make_progress_adapter(data, progress_callback)
            while chunk := adapted.read(1024):
                sent.extend(chunk)
            return types.SimpleNamespace(etag="etag")
        self.raw_bucket.upload_part.side_effect = part
        self.assertTrue(self.call())
        self.assertEqual(bytes(sent), self.path.read_bytes())
        self.raw_bucket.complete_multipart_upload.assert_called_once()

    def test_wrong_cached_sha1_blocks_server_commit_and_aborts(self):
        self.digest = "0" * 40
        with self.assertRaisesRegex(self.adapter.CloudError, "实际上传字节数或SHA1"):
            self.call()
        self.raw_bucket.complete_multipart_upload.assert_not_called()
        self.raw_bucket.abort_multipart_upload.assert_called_once()
        self.assertTrue(self.path.exists())

    def test_incomplete_part_read_blocks_commit_and_aborts(self):
        def short_read(key, upload_id, part_number, data, progress_callback):
            data.read(2)
            return types.SimpleNamespace(etag="etag")
        self.raw_bucket.upload_part.side_effect = short_read
        with self.assertRaisesRegex(self.adapter.CloudError, "分片读取不足"):
            self.call()
        self.raw_bucket.complete_multipart_upload.assert_not_called()
        self.raw_bucket.abort_multipart_upload.assert_called_once()

    def test_retried_part_does_not_double_count_failed_attempt_bytes(self):
        oper = Retry115()
        oper.file_size = self.identity[0]
        attempts = [0]
        def part(key, upload_id, part_number, data, progress_callback):
            attempts[0] += 1
            if attempts[0] == 1:
                data.read(10)
                raise TimeoutError()
            while data.read(1024):
                pass
            return types.SimpleNamespace(etag="etag")
        self.raw_bucket.upload_part.side_effect = part
        self.assertTrue(self.call(oper))
        self.assertEqual(attempts[0], 2)
        self.raw_bucket.complete_multipart_upload.assert_called_once()
        self.raw_bucket.abort_multipart_upload.assert_not_called()

    def test_instant_upload_check_reports_normal_miss_for_independent_queue(self):
        self.oper.client.upload_file_init.return_value = {"status": 1}
        self.assertFalse(self.adapter.instant_upload_check(
            self.oper, self.path, self.digest, self.identity[0], "123",
        ))

    def test_cached_digest_real_progress_and_no_global_mutation(self):
        result = self.call()
        self.assertTrue(result)
        self.assertFalse(self.oper.sha1_called)
        self.oper.client.upload_file_init.assert_called_once_with(filesha1=self.digest, timeout=30)
        self.oper.client.upload_gettoken.assert_called_once_with(timeout=30)
        self.assertEqual(Bucket.call_args.kwargs["connect_timeout"], 60)
        self.assertEqual(self.reported[0], None)
        self.assertIn(25, self.reported)
        self.assertIn(100, self.reported)
        self.checkpoint.assert_called()
        transfer_process.assert_not_called()
        self.assertIs(Fake115.upload.__globals__["global_vars"], global_vars)
        self.assertIs(Fake115.upload.__globals__["Bucket"], Bucket)
        self.raw_bucket.abort_multipart_upload.assert_not_called()

    def test_actual_instant_hit_after_initial_miss_is_not_ordinary(self):
        oper = Instant115()
        oper.client.upload_file_init.side_effect = [
            {"state": True, "reuse": False}, {"state": True, "reuse": True},
        ]
        result_kind = Mock()
        result = self.adapter.upload_with_digest(
            oper, None, self.path, self.digest, self.identity, self.stopping,
            self.reported.append, self.checkpoint, 0, on_result=result_kind,
        )
        self.assertTrue(result)
        result_kind.assert_called_once_with("instant")
        self.assertEqual(oper.client.upload_file_init.call_count, 2)
        self.raw_bucket.init_multipart_upload.assert_not_called()

    def test_ordinary_kind_requires_successful_multipart_completion(self):
        result_kind = Mock()
        self.adapter.upload_with_digest(
            self.oper, None, self.path, self.digest, self.identity, self.stopping,
            self.reported.append, self.checkpoint, 0, on_result=result_kind,
        )
        result_kind.assert_called_once_with("ordinary")
        self.oper.fail_part = True
        result_kind.reset_mock()
        with self.assertRaises(TimeoutError):
            self.adapter.upload_with_digest(
                self.oper, None, self.path, self.digest, self.identity, self.stopping,
                self.reported.append, self.checkpoint, 0, on_result=result_kind,
            )
        result_kind.assert_not_called()

    def test_missing_successful_upload_evidence_stays_unknown(self):
        self.raw_bucket.complete_multipart_upload.return_value = types.SimpleNamespace(status=500)
        result_kind = Mock()
        self.adapter.upload_with_digest(
            self.oper, None, self.path, self.digest, self.identity, self.stopping,
            self.reported.append, self.checkpoint, 0, on_result=result_kind,
        )
        result_kind.assert_called_once_with("unknown")

    def test_missing_successful_file_receipt_does_not_report_kind(self):
        oper = Instant115()
        oper.client.upload_file_init.return_value = {"state": True, "reuse": False}
        result_kind = Mock()
        result = self.adapter.upload_with_digest(
            oper, None, self.path, self.digest, self.identity, self.stopping,
            self.reported.append, self.checkpoint, 0, on_result=result_kind,
        )
        self.assertIsNone(result)
        result_kind.assert_not_called()

    def test_failure_aborts_only_incomplete_multipart_and_releases_gate(self):
        self.oper.fail_part = True
        with self.assertRaises(TimeoutError):
            self.call()
        self.raw_bucket.abort_multipart_upload.assert_called_once_with("key", "upload-id")
        self.assertFalse(self.adapter.UPLOAD_GATE.locked())
        self.assertTrue(self.path.exists())

    def test_stop_during_network_progress_aborts_without_global_stop(self):
        self.oper.stop_during_part = self.stopping.set
        with self.assertRaises(InterruptedError):
            self.call()
        self.raw_bucket.abort_multipart_upload.assert_called_once()
        global_vars.stop_transfer.assert_not_called()
        self.assertFalse(self.adapter.UPLOAD_GATE.locked())

    def test_changed_source_blocks_cached_digest_before_network(self):
        self.path.write_bytes(b"changed")
        with self.assertRaises(OSError):
            self.call()
        self.oper.client.upload_file_init.assert_not_called()

    def test_token_refresh_preserves_progress_and_does_not_abort_completed_upload(self):
        self.oper.token_refresh = True
        self.call()
        self.assertEqual(self.reported[-1], 100)
        self.raw_bucket.abort_multipart_upload.assert_not_called()
        self.assertEqual(Bucket.call_count, 2)

    def test_unsupported_uploader_fails_closed_without_global_patch(self):
        oper = types.SimpleNamespace(upload=Mock())
        with self.assertRaisesRegex(self.adapter.UploadNotStarted, "尚未发起上传"):
            self.call(oper)
        oper.upload.assert_not_called()

    def test_preflight_failure_never_marks_upload_started(self):
        started = Mock()
        with self.assertRaises(self.adapter.UploadNotStarted):
            self.adapter.upload_with_digest(
                types.SimpleNamespace(upload=Mock()), None, self.path, self.digest,
                self.identity, self.stopping, Mock(), Mock(), 0, on_start=started,
            )
        started.assert_not_called()

    def test_upload_marker_is_written_before_first_initialization_request(self):
        started = Mock()
        self.oper.client.upload_file_init.side_effect = lambda **kwargs: self.assertTrue(started.called)
        self.adapter.upload_with_digest(
            self.oper, None, self.path, self.digest, self.identity, self.stopping,
            self.reported.append, self.checkpoint, 0, on_start=started,
        )
        started.assert_called_once()

    def test_rate_limit_wait_is_interruptible(self):
        self.stopping = Mock()
        self.stopping.is_set.return_value = False
        self.stopping.wait.return_value = True
        with self.assertRaises(InterruptedError):
            self.call(rate=1)
        self.stopping.wait.assert_called()
        self.assertGreater(self.stopping.wait.call_args.args[0], 0)

    def test_two_callers_are_serial_and_waiting_upload_can_stop(self):
        self.oper.started = threading.Event()
        self.oper.release = threading.Event()
        outcomes = []
        def first():
            try:
                outcomes.append(self.call())
            except Exception as failure:
                outcomes.append(failure)
        worker = threading.Thread(target=first)
        worker.start()
        self.addCleanup(worker.join, 4)
        self.addCleanup(self.oper.release.set)
        self.assertTrue(self.oper.started.wait(2))
        stopped = threading.Event()
        stopped.set()
        with self.assertRaises(InterruptedError):
            self.adapter.upload_with_digest(
                Fake115(), None, self.path, self.digest, self.identity,
                stopped, Mock(), Mock(), 0,
            )
        self.oper.release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(outcomes[0], dict)

    def test_installed_115_upload_method_offline_contract(self):
        import ast
        import __future__
        from datetime import datetime, timezone
        source = Path("/app/app/plugins/p115disk/p115_api.py")
        if not source.exists():
            self.skipTest("Installed 115 uploader is not available")
        tree = ast.parse(source.read_text())
        uploader = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "P115Api")
        method = next(node for node in uploader.body if isinstance(node, ast.FunctionDef) and node.name == "upload")
        scope = {
            "Path": Path, "global_vars": global_vars, "Bucket": Bucket,
            "transfer_process": transfer_process, "logger": Mock(), "check_response": Mock(),
            "StsAuth": Mock(), "determine_part_size": lambda *args, **kwargs: 10 * 1024 ** 2,
            "SizedFileAdapter": PartData, "PartInfo": lambda *args: args,
            "b64encode_as_string": lambda value: value, "ServerError": RuntimeError,
            "datetime": datetime, "timezone": timezone,
        }
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec",
                     flags=__future__.annotations.compiler_flag), scope)
        oper = types.SimpleNamespace(
            client=Mock(), get_folder=Mock(return_value=types.SimpleNamespace(fileid="7")),
            _id_cache=Mock(), get_item=Mock(return_value={"name": self.path.name}),
            _calc_sha1=Mock(side_effect=AssertionError("extra full read")),
            _get_oss_token=Mock(return_value=("endpoint", "key", "secret", "token", datetime(2035, 1, 1, tzinfo=timezone.utc))),
            _is_token_expiring=Mock(return_value=False),
        )
        oper._id_cache.get_id_by_dir.return_value = None
        oper.client.upload_file_init.return_value = {
            "state": True, "reuse": False, "bucket": "bucket", "object": "key",
            "callback": {"callback": "value", "callback_var": "variables"},
        }
        oper.upload = types.MethodType(scope["upload"], oper)
        target = types.SimpleNamespace(path="/upload")
        result = self.adapter.upload_with_digest(
            oper, target, self.path, self.digest, self.identity, self.stopping,
            self.reported.append, self.checkpoint, 0,
        )
        self.assertEqual(result["name"], self.path.name)
        oper._calc_sha1.assert_not_called()
        self.assertEqual(oper.client.upload_file_init.call_args.kwargs["filesha1"], self.digest)
        challenge = oper.client.upload_file_init.call_args.kwargs["read_range_bytes_or_hash"]
        self.assertEqual(challenge("10-999"), hashlib.sha1(self.path.read_bytes()[10:1000]).hexdigest().upper())
        for invalid in ("-1-999", "0-99999999", "100-99", "bad"):
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, OSError)):
                challenge(invalid)
        self.assertEqual(self.reported[-1], 100)
        self.raw_bucket.complete_multipart_upload.assert_called_once()
        self.raw_bucket.abort_multipart_upload.assert_not_called()

    def test_installed_strm_static_upload_patch_offline_contract(self):
        import ast
        import __future__
        from datetime import datetime, timezone
        from time import perf_counter
        root = Path("/app/app/plugins/p115strmhelper")
        if not root.exists():
            self.skipTest("Installed STRM upload patch is not available")
        scope = {
            "Path": Path, "global_vars": global_vars, "Bucket": Bucket,
            "transfer_process": transfer_process, "logger": Mock(), "check_response": Mock(),
            "StsAuth": Mock(), "determine_part_size": lambda *args, **kwargs: 10 * 1024 ** 2,
            "SizedFileAdapter": PartData, "PartInfo": lambda *args: args,
            "b64encode_as_string": lambda value: value, "ServerError": RuntimeError,
            "datetime": datetime, "timezone": timezone, "perf_counter": perf_counter,
            "configer": types.SimpleNamespace(
                machine_id="offline-test", notify=False, upload_module_skip_slow_upload=False,
                get_config=lambda key: -1 if key == "upload_module_wait_timeout" else 0,
            ),
            "upload_notifier": Mock(), "P115Center": Mock(), "UploadInfo": Mock(),
        }
        source = root / "core/p115disk.py"
        tree = ast.parse(source.read_text())
        helper = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "P115DiskCore")
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(source), "exec",
                     flags=__future__.annotations.compiler_flag), scope)
        wrapped_class = type("Wrapped115", (Fake115,), {})
        patch_source = root / "patch/p115disk_upload.py"
        tree = ast.parse(patch_source.read_text())
        patcher = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "P115DiskPatcher")
        globals_patch = {"P115DiskCore": scope["P115DiskCore"], "logger": Mock(), "P115API_AVAILABLE": True,
                         "P115Api": wrapped_class, "configer": types.SimpleNamespace(upload_module_enhancement=True)}
        exec(compile(ast.Module(body=[patcher], type_ignores=[]), str(patch_source), "exec",
                     flags=__future__.annotations.compiler_flag), globals_patch)
        patch_type = globals_patch["P115DiskPatcher"]
        patch_type.enable()
        self.addCleanup(patch_type.disable)
        oper = wrapped_class()
        oper.get_folder = Mock(return_value=types.SimpleNamespace(fileid="7"))
        oper._id_cache = Mock()
        oper._id_cache.get_id_by_dir.return_value = None
        oper.get_item = Mock(return_value={"name": self.path.name})
        oper._get_oss_token = Mock(return_value=("endpoint", "key", "secret", "token", datetime(2035, 1, 1, tzinfo=timezone.utc)))
        oper._is_token_expiring = Mock(return_value=False)
        oper.client.upload_file_init.return_value = {
            "state": True, "reuse": False, "bucket": "bucket", "object": "key",
            "callback": {"callback": "value", "callback_var": "variables"},
        }
        patched_method = wrapped_class.upload
        result = self.adapter.upload_with_digest(
            oper, types.SimpleNamespace(path="/upload"), self.path, self.digest,
            self.identity, self.stopping, self.reported.append, self.checkpoint, 0,
        )
        self.assertEqual(result["name"], self.path.name)
        self.assertFalse(oper.sha1_called)
        self.assertEqual(oper.client.upload_file_init.call_args.kwargs["filesha1"], self.digest)
        self.assertEqual(self.reported[-1], 100)
        self.assertIs(wrapped_class.upload, patched_method)
        self.assertTrue(patch_type._active)
        self.raw_bucket.complete_multipart_upload.assert_called_once()
        self.raw_bucket.abort_multipart_upload.assert_not_called()
        scope["configer"].upload_module_skip_slow_upload = True
        self.raw_bucket.reset_mock()
        result = self.adapter.upload_with_digest(
            oper, types.SimpleNamespace(path="/upload"), self.path, self.digest,
            self.identity, self.stopping, self.reported.append, self.checkpoint, 0,
        )
        self.assertIsNone(result)
        self.raw_bucket.init_multipart_upload.assert_not_called()
        self.assertIs(wrapped_class.upload, patched_method)
