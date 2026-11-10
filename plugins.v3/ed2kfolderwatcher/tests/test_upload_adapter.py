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
        bucket.upload_part("key", result.upload_id, 1, data=local_path)
        callback(50)
        if self.token_refresh:
            bucket = Bucket("auth-new", "endpoint", "bucket")
        bucket.upload_part("key", result.upload_id, 2, data=local_path)
        callback(100)
        bucket.complete_multipart_upload("key", result.upload_id, [])
        return {"path": "/upload/" + new_name}

    def bucket_observed(self, bucket):
        self.bucket = bucket


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
        def part(key, upload_id, part_number, data, progress_callback):
            if self.oper.stop_during_part:
                self.oper.stop_during_part()
            progress_callback(self.identity[0] // 4, self.identity[0] // 2)
            if self.oper.fail_part:
                raise TimeoutError()
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
        with self.assertRaisesRegex(self.adapter.CloudError, "安全进度"):
            self.call(oper)
        oper.upload.assert_not_called()

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
            "SizedFileAdapter": lambda stream, size: stream, "PartInfo": lambda *args: args,
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
