import hashlib
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_local_upload as existing_upload


global_vars = types.SimpleNamespace(is_transfer_stopped=lambda source: False)
transfer_process = lambda source: lambda percent: None
sleep = lambda seconds: None


class FakeUploader:
    def __init__(self):
        self.client = Mock()
        self.get_item_strict = Mock(return_value=None)

    def upload(self, target, path, new_name=None):
        if global_vars.is_transfer_stopped(str(path)):
            return None
        transfer_process(str(path))(50)
        sleep(0)
        self.client.write_to_file(1, 0, path.read_bytes())
        return {"storage": "CloudDrive储存", "path": f"{target.path}/{new_name}"}


class CloudDriveAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        existing_upload.LocalUploadTests.setUpClass()
        cls.adapter = existing_upload.LocalUploadTests.upload_module.clouddrive_upload

    def setUp(self):
        module_patch = patch.dict(sys.modules, {
            "ed2k_test_plugin": existing_upload.LocalUploadTests.module,
            "ed2k_test_plugin.local_upload": existing_upload.LocalUploadTests.upload_module,
        })
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "a.mkv"
        self.path.write_bytes(b"cloud-upload")
        self.identity = existing_upload.LocalUploadTests.upload_module.local_identity(self.path.stat())
        self.target = types.SimpleNamespace(path="/115/upload")
        self.remote = types.SimpleNamespace(
            id="123", fullPathName="/115/upload/a.mkv", size=self.path.stat().st_size,
            isLocal=False, isCloudFile=True, fileEncryptionType=0,
            CloudAPI=types.SimpleNamespace(name="115", userName="account"),
            fileHashes={2: hashlib.sha1(self.path.read_bytes()).hexdigest()},
        )
        self.oper = Mock()
        self.oper._download_base = "http://cd2:19798"
        self.oper.client.find_file_by_path.return_value = self.remote
        self.oper.client.get_sub_files.return_value = [self.remote]
        self.oper.client.get_upload_file_list.return_value = types.SimpleNamespace(uploadFiles=[])
        self.oper.client.upload_terminal_enums.return_value = ({2, 3}, 2)
        schemas = types.ModuleType("app.schemas")
        schemas.FileItem = lambda **values: types.SimpleNamespace(**values)
        schema_patch = patch.dict(sys.modules, {"app.schemas": schemas})
        schema_patch.start()
        self.addCleanup(schema_patch.stop)
        self.item = {"fileid": "123", "path": self.remote.fullPathName, "size": self.remote.size}

    def test_account_scope_binds_endpoint_mount_account_and_target(self):
        scope = self.adapter.upload_scope(self.oper, "/115/upload", "/local")
        self.assertEqual(scope, self.adapter.upload_scope(self.oper, "/115/upload", "/local"))
        self.assertNotEqual(scope, self.adapter.upload_scope(self.oper, "/115/other", "/local"))
        self.remote.CloudAPI.userName = "another"
        self.assertNotEqual(scope, self.adapter.upload_scope(self.oper, "/115/upload", "/local"))
        for changed in ({"isLocal": True}, {"fileEncryptionType": 2},
                        {"CloudAPI": types.SimpleNamespace(name="Other", userName="a")}):
            with self.subTest(changed=changed), patch.multiple(self.remote, **changed):
                with self.assertRaises(self.adapter.CloudError):
                    self.adapter.upload_scope(self.oper, "/115/upload", "/local")

    def test_remote_sha1_requires_cloud_file_identity_and_finished_upload(self):
        self.assertEqual(self.adapter.uploaded_sha1(self.oper, self.item), self.remote.fileHashes[2])
        task = types.SimpleNamespace(destPath=self.remote.fullPathName, statusEnum=1)
        self.oper.client.get_upload_file_list.return_value = types.SimpleNamespace(uploadFiles=[task])
        with self.assertRaises(self.adapter.CloudError):
            self.adapter.uploaded_sha1(self.oper, self.item)
        task.statusEnum = 3
        with self.assertRaises(self.adapter.CloudError):
            self.adapter.uploaded_sha1(self.oper, self.item)
        task.statusEnum = 2
        self.assertEqual(self.adapter.uploaded_sha1(self.oper, self.item), self.remote.fileHashes[2])

    def test_missing_sha1_or_wrong_identity_never_uses_local_hash_as_remote_evidence(self):
        for changed in ({"fileHashes": {}}, {"id": "different"},
                        {"isCloudFile": False}, {"size": 1}, {"fileEncryptionType": 2}):
            with self.subTest(changed=changed), patch.multiple(self.remote, **changed):
                with self.assertRaises(self.adapter.CloudError):
                    self.adapter.uploaded_sha1(self.oper, self.item)

    def test_adapter_is_scoped_serialized_and_does_not_patch_running_plugin(self):
        oper = FakeUploader()
        progress = Mock()
        on_start = Mock()
        original_upload = oper.upload
        result = self.adapter.upload_file(
            oper, self.target, self.path, self.identity, threading.Event(),
            progress, Mock(), 0, on_start=on_start,
        )
        self.assertEqual(result["path"], "/115/upload/a.mkv")
        self.assertEqual(oper.upload, original_upload)
        self.assertIs(global_vars, original_upload.__func__.__globals__["global_vars"])
        self.assertNotIn("upload", oper.__dict__)
        self.assertFalse(self.adapter.UPLOAD_GATE.locked())
        oper.client.write_to_file.assert_called_once_with(1, 0, b"cloud-upload")
        on_start.assert_called_once()
        progress.assert_called_once_with(50)

    def test_existing_target_stop_and_changed_source_do_not_upload(self):
        for state in ("exists", "stopped", "changed"):
            oper = FakeUploader()
            stopping = threading.Event()
            identity = list(self.identity)
            on_start = Mock()
            if state == "exists":
                oper.get_item_strict.return_value = self.item
                self.assertEqual(self.adapter.upload_file(
                    oper, self.target, self.path, identity, stopping, Mock(), Mock(), 0,
                    on_start=on_start,
                ), self.item)
            else:
                if state == "stopped":
                    stopping.set()
                else:
                    identity[0] += 1
                with self.assertRaises((InterruptedError, OSError)):
                    self.adapter.upload_file(
                        oper, self.target, self.path, identity, stopping, Mock(), Mock(), 0,
                        on_start=on_start,
                    )
            oper.client.write_to_file.assert_not_called()
            on_start.assert_not_called()
            self.assertFalse(self.adapter.UPLOAD_GATE.locked())

    def test_unrecognized_upload_interface_fails_before_start(self):
        on_start = Mock()
        with self.assertRaises(self.adapter.UploadNotStarted):
            self.adapter.upload_file(
                self.oper, self.target, self.path, self.identity, threading.Event(),
                Mock(), Mock(), 0, on_start=on_start,
            )
        on_start.assert_not_called()

    def test_deadline_client_does_not_change_running_clients_stub(self):
        client = types.SimpleNamespace(stub=types.SimpleNamespace(Query=Mock(return_value="ok")))
        clone = self.adapter.bounded_client(client)
        self.assertIsNot(clone.stub, client.stub)
        self.assertEqual(clone.stub.Query("path"), "ok")
        client.stub.Query.assert_called_once_with("path", timeout=60)

    def test_upload_gate_cancels_waiting_request_without_starting(self):
        oper = FakeUploader()
        stopping = threading.Event()
        on_start = Mock()
        timer = threading.Timer(0.05, stopping.set)
        self.adapter.UPLOAD_GATE.acquire()
        timer.start()
        try:
            with self.assertRaises(InterruptedError):
                self.adapter.upload_file(
                    oper, self.target, self.path, self.identity, stopping,
                    Mock(), Mock(), 0, on_start=on_start,
                )
        finally:
            timer.cancel()
            self.adapter.UPLOAD_GATE.release()
        on_start.assert_not_called()
        oper.client.write_to_file.assert_not_called()


class CloudDriveWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        existing_upload.LocalUploadTests.setUpClass()
        for name in ("module", "cloud_module", "upload_module", "adapter_module"):
            setattr(cls, name, getattr(existing_upload.LocalUploadTests, name))

    setUp = existing_upload.LocalUploadTests.setUp
    configure = existing_upload.LocalUploadTests.configure
    create = existing_upload.LocalUploadTests.create
    sample = existing_upload.LocalUploadTests.sample
    upload = existing_upload.LocalUploadTests.upload
    records = existing_upload.LocalUploadTests.records

    def configure_cloud(self, **settings):
        self.configure(mode="clouddrive", **settings)
        path = self.sample()
        self.remote["storage"] = "CloudDrive储存"
        adapter = self.upload_module.clouddrive_upload
        for name, options in (
            ("upload_scope", {"return_value": "cloud-scope"}),
            ("uploaded_sha1", {"side_effect": lambda *args: self.sha1}),
            ("upload_file", {"side_effect": self.cloud_upload}),
        ):
            patcher = patch.object(adapter, name, **options)
            patcher.start()
            self.addCleanup(patcher.stop)
        return path

    def cloud_upload(self, oper, target, path, *args, on_start=None, **kwargs):
        self.assertEqual(target.storage, "CloudDrive储存")
        if on_start:
            on_start()
        return dict(self.remote)

    def test_cloud_upload_uses_local_digest_and_verifies_before_output(self):
        path = self.configure_cloud()
        adapter = self.upload_module.clouddrive_upload
        self.plugin._process_file(path)
        record = self.records()[str(path)]
        self.assertTrue(record["upload_verified"])
        self.assertEqual(record["upload_kind"], "unknown")
        self.assertIn(record["link"], (self.root / "ed2k.txt").read_text())
        self.assertEqual(record["link"].split("|")[4], self.module.ed2k_hash(path))
        adapter.upload_file.assert_called_once()
        self.storage.oper.upload.assert_not_called()
        self.upload_module.upload_with_digest.assert_not_called()
        self.plugin._process_file(path)
        adapter.upload_file.assert_called_once()

    def test_missing_remote_hash_retains_source_and_retries_only_verification(self):
        path = self.configure_cloud(local_upload_delete_source=True)
        adapter = self.upload_module.clouddrive_upload
        adapter.uploaded_sha1.side_effect = self.upload_module.CloudError("SHA1尚不可用")
        with self.assertRaisesRegex(self.upload_module.CloudError, "SHA1尚不可用"):
            self.plugin._process_file(path)
        self.assertTrue(path.exists())
        self.assertEqual((self.root / "ed2k.txt").read_text(), "")
        self.assertFalse(self.records()[str(path)]["upload_verified"])
        self.storage.oper.get_item_strict.return_value = self.remote
        adapter.uploaded_sha1.side_effect = lambda *args: self.sha1
        self.plugin._process_local_upload(path, path.stat(), *self.plugin._paths()[1:], upload_worker=True)
        adapter.upload_file.assert_called_once()
        self.assertTrue(self.records()[str(path)]["upload_verified"])
        self.assertFalse(path.exists())

    def test_completed_cloud_uploads_are_filtered_with_one_scope_lookup_per_scan(self):
        path = self.configure_cloud()
        adapter = self.upload_module.clouddrive_upload
        self.plugin._process_file(path)
        records = self.records()
        other = self.sample("another.mkv")
        records[str(other)] = {**records[str(path)], "local_identity": self.upload_module.local_identity(other.stat())}
        self.plugin._write_results(None, self.plugin._paths()[2], records)
        adapter.upload_scope.reset_mock()
        adapter.uploaded_sha1.reset_mock()
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("completed")):
            self.plugin.scan(upload_only=True)
        self.assertEqual(self.plugin._pending, {})
        adapter.upload_scope.assert_called_once()
        adapter.uploaded_sha1.assert_not_called()
        adapter.upload_file.assert_called_once()

    def test_ambiguous_upload_never_restarts_when_target_is_missing(self):
        path = self.configure_cloud()
        adapter = self.upload_module.clouddrive_upload
        adapter.upload_file.side_effect = lambda *args, on_start=None, **kwargs: on_start()
        with self.assertRaisesRegex(self.upload_module.CloudError, "未返回上传成功回执"):
            self.plugin._process_file(path)
        self.assertTrue(self.records()[str(path)]["upload_attempted"])
        with self.assertRaisesRegex(self.upload_module.CloudError, "上次上传结果未确认"):
            self.plugin._process_local_upload(path, path.stat(), *self.plugin._paths()[1:], upload_worker=True)
        adapter.upload_file.assert_called_once()
        self.assertTrue(path.exists())

    def test_hash_mismatch_blocks_output_forward_and_source_deletion(self):
        path = self.configure_cloud(local_upload_delete_source=True, telegram_enabled=True,
                                    telegram_chat_id="@target")
        adapter = self.upload_module.clouddrive_upload
        adapter.uploaded_sha1.side_effect = lambda *args: "0" * 40
        with patch.object(self.plugin, "_send_telegram_text") as send:
            with self.assertRaisesRegex(self.upload_module.CloudError, "SHA1与本地不一致"):
                self.plugin._process_file(path)
        self.assertTrue(path.exists())
        self.assertEqual((self.root / "ed2k.txt").read_text(), "")
        send.assert_not_called()

    def test_unknown_upload_kind_never_triggers_ordinary_upload_forward(self):
        path = self.configure_cloud(ordinary_upload_telegram_enabled=True,
                                    ordinary_upload_telegram_targets="@separate")
        with patch.object(self.plugin, "_notify_ordinary_upload_record") as send:
            self.plugin._process_file(path)
        self.assertEqual(self.records()[str(path)]["upload_kind"], "unknown")
        send.assert_not_called()

    def test_upload_preserves_other_records_created_while_waiting_for_cloud(self):
        path = self.configure_cloud()
        adapter = self.upload_module.clouddrive_upload
        def concurrent_upload(*args, on_start=None, **kwargs):
            if on_start:
                on_start()
            output, cache_path = self.plugin._paths()[1:]
            records = self.records()
            records["other"] = {"name": "another.mkv", "size": 1, "link": "other"}
            self.plugin._write_results(None, cache_path, records)
            return dict(self.remote)
        adapter.upload_file.side_effect = concurrent_upload
        self.plugin._process_file(path)
        self.assertIn("other", self.records())
        self.assertTrue(self.records()[str(path)]["upload_verified"])
