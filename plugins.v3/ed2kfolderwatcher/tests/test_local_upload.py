import hashlib
import json
import os
import sys
import threading
import time
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
        readback_patch = patch.object(self.upload_module, "verify_cloud_readback")
        self.readback = readback_patch.start()
        self.addCleanup(readback_patch.stop)
        self.storage.oper.upload.side_effect = self.upload
        self.actual_upload_kind = "ordinary"
        def invoke(oper, target, path, *args, on_start=None, before_upload=None, on_result=None):
            if before_upload:
                instant_item = before_upload()
                if instant_item is not None:
                    return instant_item
            if on_start:
                on_start()
            result = oper.upload(target, path, new_name=path.name)
            if result and on_result:
                on_result(self.actual_upload_kind)
            return result
        upload_patch = patch.object(self.upload_module, "upload_with_digest", side_effect=invoke)
        upload_patch.start()
        self.addCleanup(upload_patch.stop)
        def run_upload_inline(path, identity):
            stat = path.stat()
            output, cache_path = self.plugin._paths()[1:]
            output = output if self.plugin._output_enabled else None
            return self.plugin._process_local_upload(
                path, stat, output, cache_path, upload_worker=True,
            )
        self.real_upload_queue = self.plugin._queue_local_upload
        self.plugin._queue_local_upload = run_upload_inline

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

    def test_unreadable_cloud_blocks_all_forwarding_output_and_deletion(self):
        self.configure(telegram_enabled=True, telegram_chat_id="@target", local_upload_delete_source=True,
                       ordinary_upload_telegram_enabled=True, ordinary_upload_telegram_targets="@ordinary")
        path = self.sample()
        self.readback.side_effect = self.cloud_module.CloudError("cloud not ready")
        with patch.object(self.plugin, "_send_telegram") as send, \
                patch.object(self.plugin, "_send_telegram_user") as ordinary:
            with self.assertRaisesRegex(self.cloud_module.CloudError, "cloud not ready"):
                self.plugin._process_file(path)
            send.assert_not_called()
            ordinary.assert_not_called()
        record = self.records()[str(path)]
        self.assertFalse(record["upload_verified"])
        self.assertNotIn("upload_readback_verified", record)
        self.assertEqual((self.root / "ed2k.txt").read_text(), "")
        self.assertTrue(path.exists())
        self.storage.oper.get_item_strict.return_value = self.remote
        self.readback.side_effect = None
        with patch.object(self.plugin, "_send_telegram", return_value=True), \
                patch.object(self.plugin, "_send_telegram_user", return_value=True):
            self.plugin._process_local_upload(path, path.stat(), *self.plugin._paths()[1:], upload_worker=True)
        self.storage.oper.upload.assert_called_once()
        self.assertTrue(self.records()[str(path)]["upload_readback_verified"])
        self.assertTrue(self.records()[str(path)]["upload_verified_at"])
        self.assertFalse(path.exists())

    def test_mp_receipt_path_does_not_add_direct_upload_readback(self):
        self.configure(mode="mp")
        path = self.sample()
        self.plugin._enabled = True
        self.plugin._on_local_upload_transfer(self.event(path))
        self.plugin._process_file(path)
        self.readback.assert_not_called()
        self.assertTrue(self.records()[str(path)]["upload_verified"])

    def test_completed_upload_is_not_queued_by_events_or_repeated_scans(self):
        self.configure(telegram_enabled=True, telegram_chat_id="@target", telegram_aggregate=False)
        path = self.sample()
        with patch.object(self.plugin, "_send_telegram", return_value=True) as send:
            self.plugin._process_file(path)
            with patch.object(self.plugin, "_verify_uploaded", side_effect=AssertionError("already completed")), \
                    patch.object(self.module, "ed2k_hash", side_effect=AssertionError("already computed")):
                self.plugin._queue_event(path)
                self.plugin.scan(upload_only=True)
                self.plugin.scan(upload_only=True)
                self.plugin._process_file(path)
                self.plugin._process_local_upload(path, path.stat(), *self.plugin._paths()[1:], upload_worker=True)
        self.assertEqual(self.plugin._pending, {})
        self.assertEqual(self.plugin._local_upload_queue, {})
        self.assertEqual(self.records()[str(path)]["upload_state"], "verified")
        send.assert_called_once()
        self.storage.oper.upload.assert_called_once()

    def test_saving_unchanged_settings_does_not_restore_completed_upload(self):
        self.configure()
        path = self.sample()
        self.plugin._process_file(path)
        self.plugin._local_upload_queue[str(path)] = {
            "path": str(path), "identity": self.records()[str(path)]["local_identity"],
            "queued_at": time.time(), "retry_at": 0, "attempts": 0,
        }
        self.plugin._save_local_upload_queue()
        config = {
            "enabled": True, "folder": "", "local_upload_enabled": True,
            "local_upload_mode": "direct", "local_upload_folder": str(self.folder),
            "local_upload_target": "/upload", "output": str(self.root / "ed2k.txt"),
            "output_enabled": True, "min_age": 0,
        }
        with patch.object(self.module.threading, "Thread") as worker, \
                patch.object(self.plugin, "_start_monitor"), \
                patch.object(self.plugin, "_start_local_upload_worker"), \
                patch.object(self.plugin, "_verify_uploaded", side_effect=AssertionError("already verified")):
            worker.return_value.is_alive.return_value = False
            self.plugin.init_plugin(config)
            self.plugin.init_plugin(config)
        self.assertEqual(self.plugin._pending, {})
        self.assertEqual(self.plugin._local_upload_queue, {})
        self.assertEqual(json.loads(self.plugin._local_upload_queue_path().read_text()), {})
        self.storage.oper.upload.assert_called_once()

    def test_restore_prunes_completed_uploads_and_keeps_unverified_work(self):
        self.configure()
        completed = self.sample()
        self.plugin._process_file(completed)
        pending = self.sample("pending.mkv")
        records = self.records()
        records[str(completed)]["upload_state"] = "upload_queued"
        self.plugin._write_results(None, self.plugin._paths()[2], records)
        self.plugin._local_upload_queue = {
            str(path): {
                "path": str(path), "identity": self.upload_module.local_identity(path.stat()),
                "queued_at": time.time(), "retry_at": 0, "attempts": 0,
            } for path in (completed, pending)
        }
        self.plugin._save_local_upload_queue()
        self.plugin._load_local_upload_queue()
        self.assertEqual(set(self.plugin._local_upload_queue), {str(pending)})
        self.assertEqual(self.records()[str(completed)]["upload_state"], "verified")
        self.assertEqual(set(json.loads(self.plugin._local_upload_queue_path().read_text())), {str(pending)})

    def test_missing_upload_source_is_removed_without_retry_or_cloud_calls(self):
        self.configure()
        path = self.sample()
        self.plugin._process_file(path)
        entry = {
            "path": str(path), "identity": self.upload_module.local_identity(path.stat()),
            "queued_at": time.time(), "retry_at": 0, "attempts": 0,
        }
        path.unlink()
        self.plugin._local_upload_queue = {str(path): entry}
        self.plugin._save_local_upload_queue()
        with patch.object(self.plugin, "_upload_storage", side_effect=AssertionError("missing source")), \
                patch.object(self.plugin, "_record_upload_error", side_effect=AssertionError("no retry")):
            self.plugin._load_local_upload_queue()
            self.assertEqual(self.plugin._local_upload_queue, {})
            self.plugin._local_upload_queue = {str(path): entry}
            self.plugin._local_upload_queue_wake.wait = lambda timeout: self.plugin._stopping.set()
            self.plugin._drain_local_upload_queue()
        self.assertEqual(self.plugin._local_upload_queue, {})
        self.assertEqual(self.records()[str(path)]["upload_state"], "verified")
        self.assertTrue(self.records()[str(path)]["upload_verified"])
        self.assertIn("本地源文件已不存在", self.records()[str(path)]["upload_note"])

    def test_changed_file_is_still_queued_after_previous_upload_completed(self):
        self.configure()
        path = self.sample()
        self.plugin._process_file(path)
        path.write_bytes(b"changed upload content")
        self.plugin.scan(upload_only=True)
        self.assertIn(str(path), self.plugin._pending)

    def test_new_destination_account_or_mode_still_queues_completed_source(self):
        self.configure()
        path = self.sample()
        self.plugin._process_file(path)
        self.plugin._local_upload_target = "/another"
        self.plugin._queue_event(path)
        self.assertIn(str(path), self.plugin._pending)
        self.plugin._pending.clear()
        self.plugin._local_upload_target = "/upload"
        self.storage.oper.client.user_id = 456
        self.plugin._queue_event(path)
        self.assertIn(str(path), self.plugin._pending)
        self.plugin._pending.clear()
        self.storage.oper.client.user_id = 123
        self.plugin._local_upload_mode = "clouddrive"
        with patch.object(self.plugin, "_upload_scope", return_value=self.records()[str(path)]["upload_scope"]):
            self.plugin._queue_event(path)
        self.assertIn(str(path), self.plugin._pending)

    def test_unavailable_account_does_not_drop_pending_upload(self):
        self.configure()
        path = self.sample()
        self.plugin._process_file(path)
        with patch.object(self.plugin, "_upload_scope", side_effect=self.cloud_module.CloudError("account unavailable")):
            self.plugin._queue_event(path)
        self.assertIn(str(path), self.plugin._pending)

    def test_new_telegram_target_keeps_completed_upload_eligible_for_delivery(self):
        self.configure(telegram_enabled=True, telegram_chat_id="@first")
        path = self.sample()
        with patch.object(self.plugin, "_send_telegram", return_value=True) as send:
            self.plugin._process_file(path)
            self.plugin._telegram_chat_id = "@first,@second"
            self.plugin.scan(upload_only=True)
            self.assertIn(str(path), self.plugin._pending)
            self.plugin._process_file(path)
        self.assertEqual(send.call_count, 2)
        self.storage.oper.upload.assert_called_once()
        self.assertTrue(self.records()[str(path)]["telegram_sent"])

    def test_partial_telegram_delivery_is_not_filtered_out(self):
        self.configure(telegram_enabled=True, telegram_chat_id="@first")
        path = self.sample()
        with patch.object(self.plugin, "_send_telegram", return_value=False):
            with self.assertRaises(self.cloud_module.CloudError):
                self.plugin._process_file(path)
        self.plugin.scan(upload_only=True)
        self.assertIn(str(path), self.plugin._pending)
        self.assertTrue(self.records()[str(path)]["upload_verified"])

    def test_new_ordinary_forward_target_is_not_filtered_out(self):
        self.configure(ordinary_upload_telegram_enabled=True, ordinary_upload_telegram_targets="@first")
        path = self.sample()
        with patch.object(self.plugin, "_send_telegram_user", return_value=True) as send:
            self.plugin._process_file(path)
            self.plugin._queue_event(path)
            self.assertEqual(self.plugin._pending, {})
            self.plugin._ordinary_upload_telegram_targets = "@first,@second"
            self.plugin._queue_event(path)
            self.assertIn(str(path), self.plugin._pending)
            self.plugin._process_file(path)
        self.assertEqual(send.call_count, 2)
        self.storage.oper.upload.assert_called_once()

    def test_new_local_or_cloud_delete_setting_is_not_filtered_out(self):
        self.configure()
        path = self.sample()
        self.plugin._process_file(path)
        self.plugin._local_upload_delete_source = True
        self.plugin._queue_event(path)
        self.assertIn(str(path), self.plugin._pending)
        self.plugin._pending.clear()
        self.plugin._local_upload_delete_source = False
        self.plugin._local_upload_delete_cloud_copy = True
        self.plugin._queue_event(path)
        self.assertIn(str(path), self.plugin._pending)

    def test_mp_completed_receipt_is_not_replayed_but_changed_target_is_processed(self):
        self.configure(mode="mp")
        path = self.sample()
        self.plugin._enabled = True
        self.plugin._on_local_upload_transfer(self.event(path))
        self.plugin._pending.clear()
        self.plugin._process_file(path)
        self.plugin.scan(upload_only=True)
        self.assertEqual(self.plugin._pending, {})
        changed = {**self.remote, "fileid": "5678", "name": "renamed.mkv", "path": "/upload/renamed.mkv"}
        self.plugin._on_local_upload_transfer(self.event(path, changed))
        self.assertIn(str(path), self.plugin._pending)
        self.storage.oper.upload.assert_not_called()

    def test_direct_upload_waits_for_recent_change_even_with_preserved_old_mtime(self):
        self.configure()
        path = self.sample()
        now = time.time()
        os.utime(path, (now - 3600, now - 3600))
        self.plugin._min_age = 15
        with patch.object(self.module, "ed2k_hash", wraps=self.module.ed2k_hash) as hashing:
            self.plugin._process_file(path)
            hashing.assert_not_called()
            self.storage.oper.upload.assert_not_called()
            self.assertFalse((self.root / "data" / "records.json").exists())
            self.assertIn(str(path), self.plugin._pending)
            with patch.object(self.module.time, "time", return_value=now + 30):
                self.plugin._process_file(path)
            hashing.assert_called_once()
        self.assertTrue(self.records()[str(path)]["upload_verified"])

    def test_mp_success_receipt_does_not_add_direct_upload_change_wait(self):
        self.configure(mode="mp")
        path = self.sample()
        now = time.time()
        os.utime(path, (now - 3600, now - 3600))
        self.plugin._min_age = 15
        self.plugin._enabled = True
        self.plugin._on_local_upload_transfer(self.event(path))
        self.plugin._process_file(path)
        self.assertTrue(self.records()[str(path)]["upload_verified"])
        self.storage.oper.upload.assert_not_called()

    def test_upload_channel_rechecks_instant_upload_before_ordinary_transfer(self):
        self.configure()
        path = self.sample()
        folder = {
            "storage": "115网盘Plus", "fileid": "555", "type": "dir",
            "name": "upload", "path": "/upload",
        }
        visible = [False]
        self.storage.oper.get_item_strict.side_effect = lambda target: (
            folder if str(target) == "/upload" else dict(self.remote) if visible[0] else None
        )
        def hit(*args):
            visible[0] = True
            return True
        with patch.object(self.upload_module, "instant_upload_check", side_effect=hit) as check, \
                patch.object(self.plugin, "_send_telegram", return_value=True):
            self.plugin._process_file(path)
        record = self.records()[str(path.resolve())]
        self.assertTrue(record["upload_verified"])
        self.assertEqual(record["upload_kind"], "instant")
        self.storage.oper.upload.assert_not_called()
        self.assertGreaterEqual(check.call_count, 1)

    def test_successful_fast_check_is_not_repeated_while_waiting_for_verification(self):
        self.configure()
        path = self.sample()
        folder = {"storage": "115网盘Plus", "fileid": "555", "type": "dir", "path": "/upload", "name": "upload"}
        self.storage.oper.get_item_strict.side_effect = lambda target: folder if str(target) == "/upload" else None
        with patch.object(self.plugin, "_queue_local_upload"), \
                patch.object(self.upload_module, "instant_upload_check", return_value=True) as check:
            self.plugin._process_file(path)
            self.plugin._process_file(path)
        record = self.records()[str(path)]
        self.assertEqual(record["upload_kind"], "instant")
        self.assertTrue(record["upload_attempted"])
        self.assertFalse(record["upload_verified"])
        check.assert_called_once()
        self.assertEqual((self.root / "ed2k.txt").read_text(), "")
        self.storage.oper.upload.assert_not_called()
        self.storage.oper.get_item_strict.side_effect = lambda target: dict(self.remote)
        output, cache = self.plugin._paths()[1:]
        self.plugin._process_local_upload(path, path.stat(), output, cache, upload_worker=True)
        self.assertTrue(self.records()[str(path)]["upload_verified"])
        self.assertEqual(self.records()[str(path)]["upload_kind"], "instant")
        self.storage.oper.upload.assert_not_called()

    def test_fast_check_timeout_preserves_marker_and_never_starts_ordinary_upload(self):
        self.configure()
        path = self.sample()
        folder = {"storage": "115网盘Plus", "fileid": "555", "type": "dir", "path": "/upload", "name": "upload"}
        self.storage.oper.get_item_strict.side_effect = lambda target: folder if str(target) == "/upload" else None
        with patch.object(self.plugin, "_queue_local_upload"), \
                patch.object(self.upload_module, "instant_upload_check", side_effect=TimeoutError()) as check:
            self.plugin._process_file(path)
            self.plugin._process_file(path)
        record = self.records()[str(path)]
        self.assertTrue(record["upload_attempted"])
        self.assertEqual(record["rapid_check"], "uncertain")
        check.assert_called_once()
        output, cache = self.plugin._paths()[1:]
        with self.assertRaisesRegex(self.cloud_module.CloudError, "上次上传结果未确认"):
            self.plugin._process_local_upload(path, path.stat(), output, cache, upload_worker=True)
        self.storage.oper.upload.assert_not_called()
        self.assertTrue(path.exists())
        self.assertEqual((self.root / "ed2k.txt").read_text(), "")

    def test_legacy_fast_hit_recovers_marker_without_creating_another_file(self):
        self.configure()
        path = self.sample()
        output, cache = self.plugin._paths()[1:]
        record, records = self.plugin._prepare_local_upload(path, path.stat(), {}, str(path), output, cache)
        record.update(upload_scope=self.plugin._upload_scope(self.storage), rapid_check="hit")
        self.plugin._write_results(output, cache, records)
        with patch.object(self.plugin, "_queue_local_upload"), \
                patch.object(self.upload_module, "instant_upload_check") as check:
            self.plugin._process_file(path)
        check.assert_not_called()
        record = self.records()[str(path)]
        self.assertTrue(record["upload_attempted"])
        self.assertEqual(record["upload_kind"], "instant")
        self.storage.oper.upload.assert_not_called()

    def test_manual_retry_explicitly_clears_fast_check_uncertainty_markers(self):
        self.configure()
        path = self.sample()
        output, cache = self.plugin._paths()[1:]
        record, records = self.plugin._prepare_local_upload(path, path.stat(), {}, str(path), output, cache)
        record.update(upload_attempted=True, rapid_check="hit", rapid_check_started=True)
        self.plugin._reset_upload_attempts(cache, records)
        record = self.records()[str(path)]
        self.assertNotIn("upload_attempted", record)
        self.assertNotIn("rapid_check", record)
        self.assertNotIn("rapid_check_started", record)
        self.storage.oper.upload.assert_not_called()

    def test_upload_lane_timeout_does_not_fall_through_to_ordinary_upload(self):
        self.configure()
        path = self.sample()
        folder = {"storage": "115网盘Plus", "fileid": "555", "type": "dir", "path": "/upload", "name": "upload"}
        self.storage.oper.get_item_strict.side_effect = lambda target: folder if str(target) == "/upload" else None
        output, cache = self.plugin._paths()[1:]
        with patch.object(self.plugin, "_try_local_instant_upload"), \
                patch.object(self.plugin, "_queue_local_upload"):
            self.plugin._process_file(path)
        with patch.object(self.upload_module, "instant_upload_check", side_effect=TimeoutError()), \
                self.assertRaisesRegex(self.cloud_module.CloudError, "秒传复查结果未确认"):
            self.plugin._process_local_upload(path, path.stat(), output, cache, upload_worker=True)
        self.assertTrue(self.records()[str(path)]["upload_attempted"])
        self.storage.oper.upload.assert_not_called()
        self.assertTrue(path.exists())

    def test_existing_destination_does_not_create_another_instant_upload(self):
        self.configure()
        path = self.sample()
        self.storage.oper.get_item_strict.return_value = dict(self.remote)
        with patch.object(self.upload_module, "instant_upload_check") as check:
            self.plugin._process_file(path)
        check.assert_not_called()
        self.storage.oper.upload.assert_not_called()
        self.assertTrue(self.records()[str(path)]["upload_verified"])

    def test_ordinary_upload_uses_its_separate_telegram_destinations(self):
        self.configure(
            ordinary_upload_telegram_enabled=True,
            ordinary_upload_telegram_targets="@private_receiver",
        )
        path = self.sample()
        with patch.object(self.plugin, "_send_telegram_user", return_value=True) as send:
            self.plugin._process_file(path)
        record = self.records()[str(path.resolve())]
        self.assertEqual(record["upload_kind"], "ordinary")
        self.assertTrue(record["ordinary_upload_telegram_sent"])
        self.assertEqual(send.call_args.kwargs["target"], ("@private_receiver", None))

    def test_final_instant_result_overrides_precheck_miss_without_ordinary_forward(self):
        self.configure(ordinary_upload_telegram_enabled=True, ordinary_upload_telegram_targets="@private_receiver")
        path = self.sample()
        self.actual_upload_kind = "instant"
        folder = {"storage": "115网盘Plus", "fileid": "555", "type": "dir", "path": "/upload", "name": "upload"}
        self.storage.oper.get_item_strict.side_effect = lambda target: folder if str(target) == "/upload" else None
        with patch.object(self.upload_module, "instant_upload_check", return_value=False), \
                patch.object(self.plugin, "_send_telegram_user", return_value=True) as send:
            self.plugin._process_file(path)
        record = self.records()[str(path)]
        self.assertEqual(record["rapid_check"], "miss")
        self.assertEqual(record["upload_kind"], "instant")
        self.assertTrue(record["upload_verified"])
        send.assert_not_called()

    def test_unknown_upload_result_does_not_guess_ordinary(self):
        self.configure(ordinary_upload_telegram_enabled=True, ordinary_upload_telegram_targets="@private_receiver")
        path = self.sample()
        self.actual_upload_kind = "unknown"
        with patch.object(self.plugin, "_send_telegram_user", return_value=True) as send:
            self.plugin._process_file(path)
        record = self.records()[str(path)]
        self.assertEqual(record["upload_kind"], "unknown")
        self.assertTrue(record["upload_verified"])
        send.assert_not_called()

    def test_mp_ordinary_receipt_survives_initial_scope_binding(self):
        self.configure(mode="mp", ordinary_upload_telegram_enabled=True,
                       ordinary_upload_telegram_targets="@private_receiver")
        path = self.sample()
        output, cache = self.plugin._paths()[1:]
        receipt = {"item": self.remote, "upload_kind": "ordinary"}
        with patch.object(self.plugin, "_mp_upload_receipt", return_value=receipt), \
                patch.object(self.plugin, "_send_telegram_user", return_value=True) as send:
            self.plugin._process_local_upload(path, path.stat(), output, cache, upload_worker=True)
        record = self.records()[str(path)]
        self.assertEqual(record["upload_kind"], "ordinary")
        self.assertTrue(record["ordinary_upload_telegram_sent"])
        send.assert_called_once()
        self.storage.oper.upload.assert_not_called()

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

    def test_slow_upload_does_not_block_hashing_and_queueing_next_file(self):
        self.configure()
        self.plugin._enabled = True
        first = self.sample("first.mkv")
        second = self.sample("second.mkv")
        upload_started = threading.Event()
        release_upload = threading.Event()

        def slow_upload(*args, on_start=None, **kwargs):
            if on_start:
                on_start()
            upload_started.set()
            release_upload.wait(5)
            return dict(self.remote)

        self.plugin._queue_local_upload = self.real_upload_queue
        with patch.object(self.upload_module, "upload_with_digest", side_effect=slow_upload), \
                patch.object(self.plugin, "_try_local_instant_upload", return_value=None):
            try:
                self.plugin._process_file(first)
                self.assertTrue(upload_started.wait(3))
                self.plugin._process_file(second)
                records = self.records()
                self.assertIn(str(second.resolve()), records)
                self.assertEqual(records[str(second.resolve())]["upload_state"], "upload_queued")
                self.assertIn(str(second.resolve()), self.plugin._local_upload_queue)
            finally:
                self.plugin._stopping.set()
                self.plugin._local_upload_queue_wake.set()
                release_upload.set()
                if self.plugin._local_upload_worker:
                    self.plugin._local_upload_worker.join(5)

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
        self.configure(local_upload_delete_source=True, telegram_enabled=True, telegram_chat_id="@receiver")
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
        self.configure(mode="mp", local_upload_delete_source=True, telegram_enabled=True, telegram_chat_id="@receiver")
        path = self.sample()
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("must wait for MP upload")):
            self.plugin._process_file(path)
        self.assertFalse((self.root / "data" / "records.json").exists())
        self.assertTrue(path.exists())
        self.assertFalse((self.root / "ed2k.txt").exists())
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

    def test_mp_computed_record_waits_for_verification_not_another_upload(self):
        self.configure(mode="mp")
        path = self.sample()
        self.plugin._enabled = True
        self.plugin._on_local_upload_transfer(self.event(path))
        with patch.object(self.plugin, "_queue_local_upload") as queue:
            self.plugin._process_file(path)
        record = self.records()[str(path)]
        self.assertEqual(record["upload_state"], "verifying")
        self.assertIn("MP复制上传已完成", record["upload_note"])
        self.assertIn("等待115内容核验", record["upload_note"])
        self.assertFalse(record.get("upload_verified"))
        queue.assert_called_once_with(path, record["local_identity"])
        self.storage.oper.upload.assert_not_called()
        self.storage.file_sha1.assert_not_called()
        self.assertEqual((self.root / "ed2k.txt").read_text(), "")

    def test_mp_telegram_failure_is_reported_as_rebroadcast_not_upload_retry(self):
        self.configure(mode="mp", telegram_enabled=True, telegram_chat_id="@receiver")
        path = self.sample()
        self.plugin._enabled = True
        self.plugin._on_local_upload_transfer(self.event(path))
        output, cache = self.plugin._paths()[1:]
        with patch.object(self.plugin, "_queue_local_upload"), patch.object(self.plugin, "_send_telegram", return_value=False):
            self.plugin._process_local_upload(path, path.stat(), output, cache)
            with self.assertRaisesRegex(self.cloud_module.CloudError, "MP整理上传及网盘内容校验已确认，Telegram尚未全部投递成功；重试仅补发，不重新上传"):
                self.plugin._process_local_upload(path, path.stat(), output, cache, upload_worker=True)
        self.storage.oper.upload.assert_not_called()
        self.assertTrue(self.records()[str(path)]["upload_verified"])

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
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("stale MP receipt")):
            self.plugin._process_file(path)
            self.plugin._pending.clear()
            self.plugin._queue_event(path, delay=0)
        self.assertFalse(self.plugin._pending)
        self.assertFalse((self.root / "data" / "records.json").exists())
        self.storage.file_sha1.assert_not_called()

    def test_mp_mode_does_not_watch_upload_source_directories(self):
        self.configure(mode="mp")
        self.assertEqual(self.plugin._local_roots(watching=True), [])
        with patch.object(self.module, "Observer") as observer:
            self.plugin._start_monitor()
        observer.assert_not_called()

    def test_mp_startup_does_not_watch_scan_or_hash_unuploaded_files(self):
        self.create("not-uploaded.mkv", b"not uploaded")
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("no upload receipt")), \
                patch.object(self.plugin, "_iter_files", side_effect=AssertionError("must not scan source")), \
                patch.object(self.plugin, "_start_monitor") as monitor:
            self.configure(mode="mp", enabled=True)
        monitor.assert_not_called()
        self.assertFalse(self.plugin._pending)
        self.assertEqual(self.records(), {})

    def test_mp_scan_only_recovers_success_receipts_without_walking_source(self):
        self.configure(mode="mp")
        path = self.sample()
        ignored = self.create("not-uploaded.mkv", b"not uploaded")
        with patch.object(self.plugin, "_iter_files", side_effect=AssertionError("must not walk MP source")):
            self.plugin.scan(retry_failed=True, upload_only=True)
        self.assertFalse(self.plugin._pending)
        self.plugin._enabled = True
        self.plugin._on_local_upload_transfer(self.event(path))
        self.plugin._pending.clear()
        with patch.object(self.plugin, "_iter_files", side_effect=AssertionError("must only use MP receipts")):
            self.plugin.scan(upload_only=True)
        self.assertIn(str(path), self.plugin._pending)
        self.assertNotIn(str(ignored), self.plugin._pending)

    def test_mp_upload_scope_overrides_overlapping_local_monitor_without_receipt(self):
        self.configure(mode="mp")
        path = self.sample()
        self.plugin._folder = str(self.folder)
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("unuploaded file")):
            self.plugin._queue_event(path, delay=0)
            self.plugin.scan()
            self.plugin._process_file(path)
        self.assertFalse(self.plugin._pending)
        self.assertEqual(self.records(), {})

    def test_mp_mode_does_not_block_other_local_monitor_directories(self):
        self.configure(mode="mp")
        self.sample()
        regular = self.root / "regular"
        regular.mkdir()
        path = regular / "normal.mkv"
        path.write_bytes(b"normal")
        self.plugin._folder = str(regular)
        self.plugin._queue_event(path, delay=0)
        self.assertIn(str(path), self.plugin._pending)
        self.assertEqual(self.plugin._local_roots(watching=True), [regular])

    def test_mp_upload_worker_rejects_missing_or_invalid_receipts_before_hashing(self):
        self.configure(mode="mp")
        path = self.sample()
        output, cache = self.plugin._paths()[1:]
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("must not hash before upload")):
            self.plugin._process_local_upload(path, path.stat(), output, cache, upload_worker=True)
            self.plugin._enabled = True
            for changes in ({"size": 1}, {"fileid": "0"}, {"path": "relative"}, {"storage": "local"}):
                self.plugin._on_local_upload_transfer(self.event(path, {**self.remote, **changes}))
                self.plugin._process_local_upload(path, path.stat(), output, cache, upload_worker=True)
        self.assertFalse(self.plugin._pending)
        self.assertFalse(cache.exists())
        self.storage.oper.upload.assert_not_called()
        self.storage.file_sha1.assert_not_called()

    def test_uploaded_cloud_is_not_rehashed_or_deleted_by_cloud_monitor(self):
        self.configure()
        path = self.sample()
        self.plugin._process_file(path)
        records = self.records()
        self.assertTrue(self.plugin._skip_uploaded_cloud(self.storage, self.remote, records))
        self.plugin._local_upload_delete_source = True
        self.plugin._cloud_delete_enabled = True
        self.plugin._maybe_delete_cloud_source(self.storage, self.remote, {}, None, self.root / "data" / "records.json", records, self.plugin._stopping)
        self.storage.delete_prepared.assert_not_called()
        self.plugin._cloud_enabled = True
        self.plugin._cloud_folder = "/upload"
        self.plugin._cloud_adapter = self.storage
        with patch.object(self.cloud_module, "hash_remote", side_effect=AssertionError("unexpected cloud read")):
            self.plugin._process_cloud_file(self.remote)
        self.storage.delete_prepared.assert_not_called()

    def test_uploaded_cloud_copy_delete_requires_scope_and_marks_confirmed_result(self):
        self.configure()
        self.sample()
        self.plugin._local_upload_delete_cloud_copy = True
        self.plugin._cloud_delete_enabled = True
        self.plugin._local_upload_cloud_delete_scope = "bound-upload-scope"
        self.plugin._local_upload_target = "/upload"
        self.storage.delete_scope.return_value = "bound-upload-scope"
        self.storage.prepare_delete.return_value = self.remote
        self.storage.delete_prepared.return_value = True
        record = {
            "upload_cloud_item": dict(self.remote),
            "local_sha1": self.sha1,
            "upload_cloud_delete_scope": "bound-upload-scope",
        }
        cache = self.root / "data" / "records.json"
        self.plugin._delete_uploaded_cloud_copy(record, None, cache, {str(self.root): record})
        self.assertTrue(record["upload_cloud_copy_deleted"])
        self.assertEqual(record["upload_cloud_delete_state"], "deleted")
        self.storage.prepare_delete.assert_called_once()
        self.storage.delete_prepared.assert_called_once()

    def test_uploaded_cloud_copy_delete_refuses_scope_mismatch_without_request(self):
        self.configure()
        self.sample()
        self.plugin._local_upload_delete_cloud_copy = True
        self.plugin._cloud_delete_enabled = True
        self.plugin._local_upload_cloud_delete_scope = "current-scope"
        self.plugin._local_upload_target = "/upload"
        self.storage.delete_scope.return_value = "changed-scope"
        record = {
            "upload_cloud_item": dict(self.remote),
            "local_sha1": self.sha1,
            "upload_cloud_delete_scope": "current-scope",
        }
        with self.assertRaisesRegex(self.cloud_module.CloudError, "授权已变化"):
            self.plugin._delete_uploaded_cloud_copy(record, None, self.root / "data" / "records.json", {str(self.root): record})
        self.storage.prepare_delete.assert_not_called()
        self.storage.delete_prepared.assert_not_called()

    def test_telegram_failure_never_reuploads_and_keeps_source(self):
        self.configure(local_upload_delete_source=True, telegram_enabled=True, telegram_chat_id="@receiver")
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
        self.configure(local_upload_delete_source=True, telegram_enabled=True, telegram_chat_id="@receiver")
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
        self.plugin._local_upload_delete_source = True
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
        output, cache = self.plugin._paths()[1:]
        self.plugin._prepare_local_upload(path, path.stat(), {}, str(path), output, cache)
        self.plugin._local_upload_enabled = False
        self.plugin._folder = str(self.folder)
        self.plugin._local_upload_delete_source = True
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
