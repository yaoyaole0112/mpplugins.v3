import hashlib
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_ed2kfolderwatcher as existing


class LocalUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not hasattr(existing.Ed2kTests, "module"):
            existing.Ed2kTests.setUpClass()
        cls.module = existing.Ed2kTests.module
        cls.cloud_module = existing.Ed2kTests.cloud_module
        cls.upload_module = existing.Ed2kTests.upload_module
        cls.adapter_module = existing.Ed2kTests.adapter_module

    setUp = existing.Ed2kTests.setUp
    create = existing.Ed2kTests.create
    records = existing.Ed2kTests.records
    wait_for = existing.Ed2kTests.wait_for

    def configure(self, mode="direct", **settings):
        self.plugin.init_plugin({
            "folder": "", "local_upload_enabled": True, "local_upload_mode": mode,
            "local_upload_folder": str(self.folder), "local_upload_target": "/upload",
            "min_age": 0, "output_enabled": True, "output": str(self.root / "ed2k.txt"),
            **settings,
        })
        self.storage = Mock()
        self.storage.oper.client.user_id = 123
        self.storage.oper.get_item_strict.return_value = None
        self.storage.fresh.side_effect = lambda item: dict(item)
        self.storage.file_sha1.side_effect = lambda item: self.sha1
        self.remote = None
        schemas = types.ModuleType("app.schemas")
        schemas.FileItem = lambda **values: types.SimpleNamespace(**values)
        schema_patch = patch.dict(sys.modules, {"app.schemas": schemas})
        schema_patch.start()
        self.addCleanup(schema_patch.stop)
        storage_patch = patch.object(self.upload_module, "CloudStorage", return_value=self.storage)
        storage_patch.start()
        self.addCleanup(storage_patch.stop)
        self.storage.oper.upload.side_effect = self.upload
        def invoke(oper, target, path, *args, on_start=None):
            if on_start:
                on_start()
            return oper.upload(target, path, new_name=path.name)
        upload_patch = patch.object(self.upload_module, "upload_with_digest", side_effect=invoke)
        upload_patch.start()
        self.addCleanup(upload_patch.stop)

    def sample(self, name="movie(2026)(tmdbid=123).mkv"):
        path = self.create(name, b"test-upload-content")
        self.sha1 = hashlib.sha1(path.read_bytes()).hexdigest()
        self.remote = {
            "storage": "115网盘Plus", "fileid": "1234", "type": "file",
            "name": path.name, "path": f"/upload/{path.name}", "size": path.stat().st_size,
            "modify_time": 123, "sha1": self.sha1,
        }
        return path

    def upload(self, target, path, new_name):
        self.assertEqual(path.name, new_name)
        self.assertEqual(target.storage, "115网盘Plus")
        self.assertFalse(self.plugin._stopping.is_set())
        self.assertEqual((self.root / "ed2k.txt").read_text(), "")
        return dict(self.remote)

    def event(self, path, item=None):
        return types.SimpleNamespace(event_data={
            "fileitem": {"storage": "local", "path": str(path)},
            "transferinfo": {"success": True, "target_item": item or self.remote},
        })

    def test_direct_hashes_locally_verifies_and_uploads_once(self):
        self.configure()
        path = self.sample()
        with patch.object(self.cloud_module, "hash_remote", side_effect=AssertionError("remote read")), \
                patch.object(self.plugin, "_send_telegram", return_value=True):
            self.plugin._process_file(path)
            self.plugin._process_file(path)
        record = self.records()[str(path)]
        self.assertEqual(record["local_sha1"], self.sha1)
        self.assertEqual(record["link"].split("|")[4], self.module.ed2k_hash(path))
        self.assertTrue(record["upload_verified"])
        self.storage.oper.upload.assert_called_once()
        self.assertIn(record["link"], (self.root / "ed2k.txt").read_text())
        self.assertTrue(path.exists())
        self.assertFalse(self.plugin.recent_records()["items"][0]["upload_note"] == "")

    def test_upload_stages_reset_hash_percent_and_expose_real_bytes(self):
        self.configure()
        path = self.sample()
        self.plugin._local_active = {"path": str(path), "name": path.name, "size": 1000, "bytes_read": 1000}
        self.plugin._upload_stage(path, "等待上传")
        self.assertEqual(self.plugin._local_active["bytes_read"], 0)
        self.assertFalse(self.plugin._local_active["progress_known"])
        self.plugin._upload_progress(path, 25)
        row = self.plugin._progress_snapshot()["items"][0]
        self.assertEqual(row["phase"], "upload")
        self.assertEqual(row["bytes_read"], 250)
        self.assertTrue(row["progress_known"])
        self.plugin._upload_stage(path, "校验115文件大小与SHA1")
        self.assertFalse(self.plugin._local_active["progress_known"])
        self.assertEqual(self.plugin._local_active["bytes_read"], 0)

    def test_movie_upload_progress_overrides_completed_hash_progress(self):
        self.configure()
        path = self.sample()
        self.plugin._local_active = {"path": str(path), "name": path.name, "size": 1000}
        self.plugin._movie_hash_job = {"key": str(path), "done": True, "progress": {"bytes_read": 1000}}
        self.plugin._upload_stage(path, "准备115上传")
        rows = self.plugin._progress_snapshot()["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage"], "准备115上传")
        self.plugin._movie_hash_job = None

    def test_upload_checkpoint_does_not_overwrite_other_delivery_updates(self):
        self.configure()
        path = self.sample()
        def during_upload(target, source, new_name):
            records = self.records()
            records[str(path)]["checkpoint_preserved"] = True
            self.plugin._write_results(None, self.root / "data" / "records.json", records)
            self.assertTrue(self.plugin._cloud_request_lock.acquire(blocking=False))
            self.plugin._cloud_request_lock.release()
            return self.remote
        self.storage.oper.upload.side_effect = during_upload
        self.plugin._process_file(path)
        self.assertTrue(self.records()[str(path)]["checkpoint_preserved"])

    def test_upload_failure_keeps_local_and_does_not_repeat_ambiguous_upload(self):
        self.configure()
        path = self.sample()
        self.storage.oper.upload.side_effect = TimeoutError("not logged")
        for attempt in range(2):
            with self.assertRaises(self.cloud_module.CloudError):
                self.plugin._process_file(path)
        self.storage.oper.upload.assert_called_once()
        self.assertTrue(path.exists())
        self.assertEqual((self.root / "ed2k.txt").read_text(), "")
        self.storage.oper.get_item_strict.return_value = self.remote
        self.plugin._process_file(path)
        self.assertTrue(self.records()[str(path)]["upload_verified"])
        self.storage.oper.upload.assert_called_once()

    def test_preflight_rejection_does_not_poison_upload_attempt_or_hide_first_error(self):
        self.configure()
        path = self.sample()
        error = self.upload_module.UploadNotStarted("上传适配检查未通过，尚未发起上传")
        def reject(*args, **kwargs):
            raise error
        with patch.object(self.upload_module, "upload_with_digest", side_effect=reject) as attempt:
            for index in range(2):
                with self.assertRaises(self.upload_module.UploadNotStarted):
                    self.plugin._process_file(path)
                self.plugin._record_upload_error(path, error)
            self.assertEqual(attempt.call_count, 2)
        record = self.records()[str(path)]
        self.assertNotIn("upload_attempted", record)
        self.assertEqual(record["upload_error_code"], "not_started")
        self.assertEqual(record["upload_first_error"], str(error))
        self.assertTrue(path.exists())
        self.storage.oper.upload.assert_not_called()
        self.plugin._process_file(path)
        self.assertTrue(self.records()[str(path)]["upload_verified"])
        self.storage.oper.upload.assert_called_once()

    def test_uncertain_failure_retains_first_cause_on_later_retries(self):
        self.configure()
        path = self.sample()
        self.storage.oper.upload.side_effect = TimeoutError("sensitive details not exposed")
        with self.assertRaises(self.cloud_module.CloudError) as initial:
            self.plugin._process_file(path)
        self.plugin._record_upload_error(path, initial.exception)
        with self.assertRaisesRegex(self.cloud_module.CloudError, "上次上传结果未确认") as later:
            self.plugin._process_file(path)
        self.plugin._record_upload_error(path, later.exception)
        record = self.records()[str(path)]
        self.assertIn("TimeoutError", record["upload_first_error"])
        self.assertNotIn("sensitive details", record["upload_first_error"])
        self.assertTrue(record["upload_attempted"])
        self.assertIn("上次上传结果未确认", record["upload_note"])
        self.storage.oper.upload.assert_called_once()

    def test_manual_retry_resets_ambiguous_flag_without_clearing_hash(self):
        self.configure()
        path = self.sample()
        self.storage.oper.upload.side_effect = TimeoutError()
        with self.assertRaises(self.cloud_module.CloudError):
            self.plugin._process_file(path)
        previous = self.records()[str(path)]["link"]
        self.plugin.scan(retry_failed=True, upload_only=True)
        record = self.records()[str(path)]
        self.assertEqual(record["link"], previous)
        self.assertNotIn("upload_attempted", record)

    def test_same_name_different_content_is_not_overwritten_or_forwarded(self):
        self.configure(delete_source=True, telegram_enabled=True, telegram_chat_id="@receiver")
        path = self.sample()
        self.storage.oper.get_item_strict.return_value = self.remote
        self.storage.file_sha1.return_value = "b" * 40
        self.storage.file_sha1.side_effect = None
        with patch.object(self.plugin, "_send_telegram") as send:
            with self.assertRaisesRegex(self.cloud_module.CloudError, "SHA1"):
                self.plugin._process_file(path)
        self.storage.oper.upload.assert_not_called()
        send.assert_not_called()
        self.assertTrue(path.exists())
        self.assertEqual((self.root / "ed2k.txt").read_text(), "")
        self.assertFalse(self.plugin.recent_records()["items"][0]["can_delete"])

    def test_mp_waits_for_receipt_and_uses_cloud_name(self):
        self.configure(mode="mp", delete_source=True, telegram_enabled=True, telegram_chat_id="@receiver")
        path = self.sample()
        self.plugin._process_file(path)
        self.assertEqual(self.records()[str(path)]["upload_state"], "waiting_mp")
        self.assertTrue(path.exists())
        self.assertEqual((self.root / "ed2k.txt").read_text(), "")
        self.remote.update(name="整理后的电影.mkv", path="/电影/整理后的电影.mkv")
        self.plugin._enabled = True
        self.plugin._on_local_upload_transfer(self.event(path))
        with patch.object(self.plugin, "_send_telegram", return_value=True) as send:
            self.plugin._process_file(path)
        self.storage.oper.upload.assert_not_called()
        self.assertIn("整理后的电影.mkv", self.records()[str(path)]["link"])
        send.assert_called_once()
        self.assertFalse(path.exists())

    def test_mp_event_can_arrive_before_hashing_and_survive_reload(self):
        self.configure(mode="mp")
        path = self.sample()
        self.plugin._enabled = True
        self.plugin._on_local_upload_transfer(self.event(path))
        self.assertTrue((self.root / "data" / "local_upload_receipts.json").exists())
        self.configure(mode="mp")
        self.plugin._process_file(path)
        self.assertTrue(self.records()[str(path)]["upload_verified"])
        self.storage.oper.upload.assert_not_called()

    def test_unrelated_mp_events_are_ignored(self):
        self.configure(mode="mp")
        path = self.sample()
        self.plugin._enabled = True
        for change in ({"success": False}, {"target_item": {**self.remote, "storage": "local"}}):
            event = self.event(path)
            event.event_data["transferinfo"].update(change)
            self.plugin._on_local_upload_transfer(event)
        self.assertEqual(self.plugin._upload_receipts(), {})
        self.assertEqual(self.plugin._pending, {})

    def test_mp_receipt_for_replaced_local_file_is_not_reused(self):
        self.configure(mode="mp")
        path = self.sample()
        self.plugin._enabled = True
        self.plugin._on_local_upload_transfer(self.event(path))
        path.write_bytes(b"replaced local content")
        self.sha1 = hashlib.sha1(path.read_bytes()).hexdigest()
        self.plugin._process_file(path)
        self.assertEqual(self.records()[str(path)]["upload_state"], "waiting_mp")
        self.storage.file_sha1.assert_not_called()

    def test_uploaded_cloud_is_not_rehashed_or_deleted_by_cloud_monitor(self):
        self.configure()
        path = self.sample()
        self.plugin._process_file(path)
        records = self.records()
        self.assertTrue(self.plugin._skip_uploaded_cloud(self.storage, self.remote, records))
        self.plugin._delete_source = True
        self.plugin._cloud_delete_enabled = True
        self.plugin._maybe_delete_cloud_source(self.storage, self.remote, {}, None, self.root / "data" / "records.json", records, self.plugin._stopping)
        self.storage.delete_prepared.assert_not_called()
        self.plugin._cloud_enabled = True
        self.plugin._cloud_folder = "/upload"
        self.plugin._cloud_adapter = self.storage
        with patch.object(self.cloud_module, "hash_remote", side_effect=AssertionError("unexpected cloud read")):
            self.plugin._process_cloud_file(self.remote)
        self.storage.delete_prepared.assert_not_called()

    def test_telegram_failure_never_reuploads_and_keeps_source(self):
        self.configure(delete_source=True, telegram_enabled=True, telegram_chat_id="@receiver")
        path = self.sample()
        with patch.object(self.plugin, "_send_telegram", return_value=False):
            with self.assertRaises(self.cloud_module.CloudError):
                self.plugin._process_file(path)
        self.assertTrue(path.exists())
        with patch.object(self.plugin, "_send_telegram", return_value=True):
            self.plugin._process_file(path)
        self.storage.oper.upload.assert_called_once()
        self.assertFalse(path.exists())

    def test_source_change_during_upload_is_not_forwarded_or_deleted(self):
        self.configure(delete_source=True, telegram_enabled=True, telegram_chat_id="@receiver")
        path = self.sample()
        def changed(*args, **kwargs):
            path.write_bytes(b"changed after local hash")
            return self.remote
        self.storage.oper.upload.side_effect = changed
        with patch.object(self.plugin, "_send_telegram") as send, self.assertRaises(OSError):
            self.plugin._process_file(path)
        send.assert_not_called()
        self.assertTrue(path.exists())

    def test_nested_target_preserves_relative_path_and_multiple_roots(self):
        self.configure()
        subfolder = self.folder / "show" / "Season 1"
        subfolder.mkdir(parents=True)
        path = subfolder / "show.S01E01.mkv"
        path.write_bytes(b"episode")
        self.assertEqual(self.plugin._upload_destination(path), "/upload/show/Season 1/show.S01E01.mkv")
        self.assertEqual(self.plugin._local_roots(), [self.folder])
        self.assertEqual(self.plugin._local_roots(watching=True), [self.folder])

    def test_below_threshold_upload_source_is_never_deleted_by_small_file_switch(self):
        self.configure(min_file_size="100M", delete_small_files=True)
        path = self.sample()
        self.plugin._process_file(path)
        self.assertTrue(path.exists())
        self.storage.oper.upload.assert_not_called()

    def test_uploaded_local_delete_rechecks_cloud_and_local_identity(self):
        self.configure()
        path = self.sample()
        self.plugin._process_file(path)
        records = self.records()
        self.storage.file_sha1.side_effect = None
        self.storage.file_sha1.return_value = "b" * 40
        with self.assertRaises(self.cloud_module.CloudError):
            self.plugin._delete_uploaded_local(path, records[str(path)], None, self.root / "data" / "records.json", records)
        self.assertTrue(path.exists())

    def test_upload_only_scan_does_not_queue_regular_local_files(self):
        self.configure()
        regular = self.root / "regular"
        regular.mkdir()
        (regular / "normal.mkv").write_bytes(b"normal")
        self.plugin._folder = str(regular)
        path = self.sample()
        self.plugin.scan(upload_only=True)
        self.assertIn(str(path), self.plugin._pending)
        self.assertNotIn(str(regular / "normal.mkv"), self.plugin._pending)

    def test_cached_upload_is_not_sent_or_deleted_when_cloud_disappears(self):
        self.configure()
        path = self.sample()
        self.plugin._process_file(path)
        self.plugin._delete_source = True
        self.storage.fresh.side_effect = self.cloud_module.CloudError("cloud disappeared")
        with self.assertRaises(self.cloud_module.CloudError), patch.object(self.plugin, "_send_telegram") as send:
            self.plugin._process_file(path)
        self.assertTrue(path.exists())
        send.assert_not_called()
        self.storage.oper.upload.assert_called_once()

    def test_grouped_delete_failure_obeys_retry_limit(self):
        self.configure()
        path = self.sample()
        self.plugin._process_file(path)
        self.plugin._retry_limit = 1
        records = self.records()
        self.storage.file_sha1.side_effect = self.cloud_module.CloudError("query unavailable")
        cache = self.root / "data" / "records.json"
        for attempt in range(2):
            self.plugin._finish_uploaded_local_delete(path, records[str(path)], None, cache, records)
        self.assertEqual(self.plugin._retry_failures[str(path)]["attempts"], 2)
        self.assertTrue(path.exists())
        self.assertTrue(records[str(path)]["delete_failed"])

    def test_damaged_receipts_disable_plugin_instead_of_uploading_blindly(self):
        self.configure(mode="mp")
        (self.root / "data").mkdir(exist_ok=True)
        (self.root / "data" / "local_upload_receipts.json").write_text("broken")
        self.plugin.init_plugin({
            "enabled": True, "local_upload_enabled": True, "local_upload_mode": "mp",
            "local_upload_folder": str(self.folder), "folder": "", "min_age": 0,
        })
        self.assertFalse(self.plugin._enabled)
        self.assertIsNone(self.plugin._worker)

    def test_disable_upload_module_does_not_publish_unverified_local_record(self):
        self.configure(mode="mp")
        path = self.sample()
        self.plugin._process_file(path)
        self.plugin._local_upload_enabled = False
        self.plugin._folder = str(self.folder)
        self.plugin._delete_source = True
        self.plugin._telegram_enabled = True
        self.plugin._process_file(path)
        self.assertTrue(path.exists())
        self.assertEqual((self.root / "ed2k.txt").read_text(), "")

    def test_same_pass_checksum_matches_sha1_at_ed2k_boundary(self):
        path = self.create(data=b"a" * self.module.ED2K_PART_SIZE)
        checksum = hashlib.sha1()
        digest = self.module.ed2k_hash(path, checksum=checksum)
        self.assertEqual(digest, "ee15063dd1e9c5bd5c0e4205c0b8e698")
        self.assertEqual(checksum.hexdigest(), hashlib.sha1(path.read_bytes()).hexdigest())

    def test_movie_workflow_uses_existing_background_slot_and_finishes_alone(self):
        self.configure(telegram_aggregate=True, telegram_aggregate_window=0)
        path = self.sample()
        self.plugin._enabled = True
        self.plugin._worker = threading.Thread(target=self.plugin._work, daemon=True)
        self.plugin._worker.start()
        try:
            self.plugin._queue_event(path, delay=0)
            cache = self.root / "data" / "records.json"
            self.wait_for(lambda: cache.exists() and self.records().get(str(path), {}).get("upload_verified"))
            self.storage.oper.upload.assert_called_once()
            self.wait_for(lambda: self.plugin._movie_hash_job is None)
        finally:
            self.plugin.stop_service()
