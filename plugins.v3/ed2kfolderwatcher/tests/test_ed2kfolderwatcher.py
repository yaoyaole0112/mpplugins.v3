import importlib.util
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class Ed2kTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        modules = {name: types.ModuleType(name) for name in ("app", "app.log", "app.plugins")}
        modules["app"].__path__ = []
        modules["app.log"].logger = Mock()
        modules["app.plugins"]._PluginBase = type("PluginBase", (), {})
        plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
        spec = importlib.util.spec_from_file_location("ed2k_test_plugin", plugin_path)
        cls.module = importlib.util.module_from_spec(spec)
        modules["ed2k_test_plugin"] = cls.module
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(cls.module)
            cloud_spec = importlib.util.spec_from_file_location("ed2k_test_plugin.cloud_storage", plugin_path.with_name("cloud_storage.py"))
            cls.cloud_module = importlib.util.module_from_spec(cloud_spec)
            cloud_spec.loader.exec_module(cls.cloud_module)
            cls.module.cloud_storage = cls.cloud_module

    def setUp(self):
        module_patch = patch.dict(sys.modules, {"ed2k_test_plugin": self.module, "ed2k_test_plugin.cloud_storage": self.cloud_module})
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "media"
        self.folder.mkdir()
        self.plugin = self.module.Ed2kFolderWatcher()
        self.plugin.get_data_path = lambda: self.root / "data"
        self.plugin.update_config = Mock()
        self.plugin.init_plugin({"folder": str(self.folder), "min_age": 0})
        self.addCleanup(self.plugin.stop_service)

    def create(self, name="test.bin", data=b"hello"):
        path = self.folder / name
        path.write_bytes(data)
        return path

    def test_cloud_generation_is_incremental_and_never_deletes(self):
        self.plugin._cloud_enabled = True
        self.plugin._cloud_storage = "115网盘Plus"
        self.plugin._cloud_folder = "/cloud-test"
        self.plugin._delete_source = True
        self.plugin._output_enabled = False
        item = {"storage": "115网盘Plus", "fileid": "123", "type": "file", "name": "云端.mkv", "path": "/cloud-test/云端.mkv", "size": 5, "modify_time": 1, "pickcode": "pick"}
        from ed2k_test_plugin import cloud_storage
        storage = Mock()
        storage.fresh.return_value = item
        self.plugin._cloud_adapter = storage
        with patch.object(cloud_storage, "hash_remote", return_value=self.module._md4(b"hello").hex()) as hashing, patch.object(self.plugin, "_unlink_source") as unlink:
            self.plugin._process_cloud_file(item)
            self.plugin._process_cloud_file(item)
        hashing.assert_called_once()
        unlink.assert_not_called()
        records = self.records()
        self.assertEqual(len(records), 1)
        record = next(iter(records.values()))
        self.assertEqual(record["source_type"], "cloud")
        self.assertFalse(record["source_deleted"])
        visible = self.plugin.recent_records()["items"][0]
        self.assertFalse(visible["can_delete"])
        self.assertEqual(visible["storage"], "115网盘Plus")
        self.assertFalse(self.plugin.delete_source(visible["id"])["ok"])
        self.plugin._output_enabled = True
        with patch.object(cloud_storage, "hash_remote", side_effect=AssertionError("unnecessary download")):
            self.plugin._process_cloud_file(item)
        self.assertIn(record["link"], (self.folder / "ed2k.txt").read_text())

    def cloud_fixture(self):
        self.plugin._cloud_enabled = True
        self.plugin._cloud_storage = "115网盘Plus"
        self.plugin._cloud_folder = "/cloud-test"
        self.plugin._output_enabled = False
        item = {"storage": "115网盘Plus", "fileid": "123", "type": "file", "name": "云端.mkv", "path": "/cloud-test/云端.mkv", "size": 5, "modify_time": 1, "pickcode": "old"}
        storage = Mock()
        storage.fresh.return_value = item
        self.plugin._cloud_adapter = storage
        return item, storage

    def test_minimum_size_skips_local_hash_send_and_delete_including_existing_record(self):
        path = self.create("small.mkv", b"hello")
        self.plugin._process_file(path)
        before = self.records()
        self.plugin._min_file_size = "6B"
        self.plugin._telegram_enabled = True
        self.plugin._delete_source = True
        with patch.object(self.module, "ed2k_hash") as hashing, patch.object(self.plugin, "_notify_record") as notify, patch.object(self.plugin, "_unlink_source") as unlink:
            self.plugin._process_file(path)
        hashing.assert_not_called()
        notify.assert_not_called()
        unlink.assert_not_called()
        self.assertEqual(self.records(), before)
        self.assertTrue(path.exists())

    def test_minimum_size_boundary_and_growing_local_file(self):
        path = self.create("growing.mkv", b"hello")
        self.plugin._min_file_size = "6B"
        self.plugin._process_file(path)
        self.assertFalse((self.root / "data" / "records.json").exists())
        path.write_bytes(b"hello!")
        self.plugin._process_file(path)
        self.assertEqual(self.records()[str(path.resolve())]["size"], 6)

    def test_small_file_delete_without_hash_notification_or_output(self):
        path = self.create("small.mkv", b"hello")
        self.plugin._min_file_size = "6B"
        self.plugin._delete_small_files = True
        self.plugin._output_enabled = False
        self.plugin._telegram_enabled = True
        with patch.object(self.module, "ed2k_hash") as hashing, patch.object(self.plugin, "_notify_record") as notify:
            self.plugin._process_file(path)
        self.assertFalse(path.exists())
        hashing.assert_not_called()
        notify.assert_not_called()
        self.assertFalse((self.root / "data" / "records.json").exists())

    def test_small_file_delete_waits_for_stability_and_preserves_empty(self):
        path = self.create("small.mkv", b"hello")
        empty = self.create("empty.mkv", b"")
        self.plugin._min_file_size = "6B"
        self.plugin._delete_small_files = True
        self.plugin._min_age = 60
        with patch.object(self.plugin, "_queue_event") as queue:
            self.plugin._process_file(path)
            self.plugin._process_file(empty)
        self.assertTrue(path.exists())
        self.assertTrue(empty.exists())
        self.assertEqual(queue.call_count, 2)
        self.plugin._min_age = 0
        self.plugin._process_file(path)
        self.assertFalse(path.exists())

    def test_small_file_delete_respects_boundary_and_disabled_threshold(self):
        self.plugin._delete_small_files = True
        for threshold in ("", "0", "5B"):
            with self.subTest(threshold=threshold):
                self.plugin._min_file_size = threshold
                path = self.create("boundary.mkv", b"hello")
                self.plugin._process_file(path)
                self.assertTrue(path.exists())
                self.assertEqual(self.records()[str(path.resolve())]["size"], 5)

    def test_small_file_delete_rejects_changed_file_and_symlink(self):
        path = self.create("small.mkv", b"hello")
        expected = path.stat()
        path.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.plugin._unlink_source(path, expected)
        self.plugin._min_file_size = "100B"
        self.plugin._delete_small_files = True
        outside = self.root / "outside.mkv"
        outside.write_bytes(b"hello")
        symlink = self.folder / "link.mkv"
        symlink.symlink_to(outside)
        self.plugin._process_file(symlink)
        self.plugin._process_file(outside)
        self.assertTrue(outside.exists())

    def test_small_file_delete_configuration_round_trip(self):
        self.assertFalse(self.plugin._delete_small_files)
        self.plugin.init_plugin({"folder": str(self.folder), "delete_small_files": True, "min_file_size": "100M"})
        self.plugin._save_config()
        self.assertTrue(self.plugin.update_config.call_args.args[0]["delete_small_files"])
        self.assertFalse(self.plugin.get_form()[1]["delete_small_files"])

    def test_minimum_size_configuration_round_trip(self):
        self.plugin.init_plugin({"folder": str(self.folder), "min_file_size": "100M"})
        self.plugin._save_config()
        self.assertEqual(self.plugin.update_config.call_args.args[0]["min_file_size"], "100M")
        self.assertEqual(self.module.minimum_file_bytes(self.plugin._min_file_size), 100 * 1024 ** 2)
        self.assertEqual(self.plugin.get_form()[1]["min_file_size"], "")

    def test_minimum_size_skips_cloud_hash_lookup_notify_and_existing_link_resend(self):
        item, storage = self.cloud_fixture()
        with patch.object(self.cloud_module, "hash_remote", return_value="a" * 32):
            self.plugin._process_cloud_file(item)
        before = self.records()
        storage.reset_mock()
        self.plugin._min_file_size = "6B"
        self.plugin._telegram_enabled = True
        self.assertFalse(self.plugin._cloud_item_is_eligible(item))
        self.plugin._delete_small_files = True
        with patch.object(self.cloud_module, "hash_remote") as hashing, patch.object(self.plugin, "_notify_record") as notify:
            self.plugin._process_cloud_file(item)
        storage.fresh.assert_not_called()
        hashing.assert_not_called()
        notify.assert_not_called()
        self.assertEqual(self.records(), before)
        self.plugin._min_file_size = "5B"
        self.assertTrue(self.plugin._cloud_item_is_eligible(item))

    def test_life_events_only_queue_the_new_event_file(self):
        item, storage = self.cloud_fixture()
        self.plugin._enabled = True
        self.plugin._worker = threading.current_thread()
        self.plugin._cloud_monitor_enabled = True
        self.plugin._cloud_life_started = 10.5
        storage.life_events.return_value = [
            {"id": 3, "type": 1, "update_time": 11, "file_id": "123", "file_category": 1},
            {"id": 2, "type": 1, "update_time": 10, "file_id": "old", "file_category": 1},
            {"id": 1, "type": 3, "update_time": 11, "file_id": "ignored", "file_category": 1},
        ]
        storage.item_from_life_event.return_value = item
        with patch.object(self.plugin, "queue_cloud_scan", side_effect=AssertionError("life events must not scan the directory")):
            self.plugin.poll_cloud_events()
            self.plugin.poll_cloud_events()
        self.assertEqual(list(self.plugin._pending), [self.cloud_module.record_key(item)])
        self.assertEqual(self.plugin._cloud_status["life_events"], 1)
        self.assertEqual(self.plugin._cloud_life_cursor, 3)
        storage.item_from_life_event.assert_called_once_with(storage.life_events.return_value[0])
        self.assertEqual(self.plugin.get_service()[-1]["id"], "Ed2kFolderWatcherLife")

    def test_life_events_skip_directories_outside_files_and_filters(self):
        item, storage = self.cloud_fixture()
        self.plugin._enabled = True
        self.plugin._cloud_monitor_enabled = True
        self.plugin._cloud_life_started = 10
        events = [
            {"id": 6, "type": 2, "update_time": 11, "file_id": "126", "file_category": 0},
            {"id": 5, "type": 5, "update_time": 11, "file_id": "125", "file_category": 1},
            {"id": 4, "type": 1, "update_time": 11, "file_id": "124", "file_category": 1},
            {"id": 3, "type": 1, "update_time": 11, "file_id": "123", "file_category": 1},
        ]
        storage.life_events.return_value = events
        storage.item_from_life_event.side_effect = [
            {**item, "name": "upload.part", "path": "/cloud-test/upload.part"},
            {**item, "fileid": "124", "size": 0},
            {**item, "fileid": "125", "path": "/other/outside.mkv"},
        ]
        with patch.object(self.plugin, "queue_cloud_scan", side_effect=AssertionError("life events must not scan the directory")):
            self.plugin.poll_cloud_events()
        self.assertFalse(self.plugin._pending)
        self.assertEqual(self.plugin._cloud_status["life_events"], 0)

    def test_life_move_into_folder_queues_once_and_move_out_skips(self):
        item, storage = self.cloud_fixture()
        self.plugin._enabled = True
        self.plugin._cloud_monitor_enabled = True
        self.plugin._cloud_life_started = 10
        storage.life_events.return_value = [
            {"id": 4, "type": 6, "update_time": 11, "file_id": "123", "file_category": 1},
            {"id": 3, "type": 6, "update_time": 11, "file_id": "123", "file_category": 1},
            {"id": 2, "type": 6, "update_time": 11, "file_id": "124", "file_category": 1},
        ]
        storage.item_from_life_event.side_effect = [{**item, "fileid": "124", "path": "/other/云端.mkv"}, item]
        self.plugin.poll_cloud_events()
        self.assertEqual(list(self.plugin._pending), [self.cloud_module.record_key(item)])
        self.assertEqual(self.plugin._cloud_status["life_events"], 1)

    def life_fixture(self):
        item, storage = self.cloud_fixture()
        self.plugin._enabled = True
        self.plugin._cloud_monitor_enabled = True
        self.plugin._cloud_life_started = 10
        event = {"id": 1, "type": 14, "update_time": 11, "file_id": "123", "file_category": 1}
        storage.life_events.return_value = [event]
        return item, storage, event

    def test_life_file_outside_then_renamed_inside_is_not_lost(self):
        item, storage, event = self.life_fixture()
        storage.item_from_life_event.side_effect = [{**item, "path": "/资源池/a.mkv"}, item]
        with patch.object(self.plugin, "queue_cloud_scan", side_effect=AssertionError("must not scan")), patch.object(self.module.time, "monotonic", return_value=100):
            self.plugin.poll_cloud_events()
            self.assertFalse(self.plugin._pending)
            self.assertIn("123", self.plugin._cloud_life_candidates)
            storage.life_events.return_value = [{**event, "id": 2, "type": 24}]
            self.plugin.poll_cloud_events()
        self.assertIn(self.cloud_module.record_key(item), self.plugin._pending)
        self.assertFalse(self.plugin._cloud_life_candidates)

    def test_life_delayed_recheck_without_another_event(self):
        item, storage, event = self.life_fixture()
        storage.item_from_life_event.side_effect = [None, item]
        with patch.object(self.module.time, "monotonic", return_value=100):
            self.plugin.poll_cloud_events()
            self.plugin.poll_cloud_events()
        self.assertEqual(storage.item_from_life_event.call_count, 1)
        with patch.object(self.module.time, "monotonic", return_value=160):
            self.plugin.poll_cloud_events()
        self.assertIn(self.cloud_module.record_key(item), self.plugin._pending)

    def test_life_rename_only_never_processes_old_file(self):
        item, storage, event = self.life_fixture()
        storage.life_events.return_value = [{**event, "type": 24}]
        self.plugin.poll_cloud_events()
        storage.item_from_life_event.assert_not_called()
        self.assertFalse(self.plugin._pending)

    def test_life_late_old_event_is_not_replayed_after_cursor_advances(self):
        item, storage, event = self.life_fixture()
        storage.item_from_life_event.return_value = item
        self.plugin.poll_cloud_events()
        self.plugin._pending.clear()
        storage.life_events.return_value = [{**event, "id": 2, "update_time": 9}]
        self.plugin.poll_cloud_events()
        self.assertFalse(self.plugin._pending)
        storage.item_from_life_event.assert_called_once()

    def test_life_rechecks_expire_and_have_attempt_limit(self):
        item, storage, event = self.life_fixture()
        storage.item_from_life_event.return_value = None
        for attempt in range(11):
            with patch.object(self.module.time, "monotonic", return_value=100 + 60 * attempt):
                self.plugin.poll_cloud_events()
        self.assertEqual(storage.item_from_life_event.call_count, 10)
        self.assertFalse(self.plugin._cloud_life_candidates)
        self.plugin._cloud_life_cursor = 0
        with patch.object(self.module.time, "monotonic", return_value=1000):
            self.plugin.poll_cloud_events()
        with patch.object(self.module.time, "monotonic", return_value=1901):
            self.plugin.poll_cloud_events()
        self.assertFalse(self.plugin._cloud_life_candidates)
        self.assertEqual(storage.item_from_life_event.call_count, 11)

    def test_life_deleted_candidate_is_cancelled(self):
        item, storage, event = self.life_fixture()
        storage.item_from_life_event.return_value = None
        self.plugin.poll_cloud_events()
        storage.life_events.return_value = [{**event, "id": 2, "type": 22}]
        self.plugin.poll_cloud_events()
        self.assertFalse(self.plugin._cloud_life_candidates)
        self.assertEqual(storage.item_from_life_event.call_count, 1)

    def test_life_lookup_failure_retains_candidate_and_backs_off(self):
        item, storage, event = self.life_fixture()
        storage.item_from_life_event.side_effect = self.cloud_module.CloudError("safe failure")
        for now, delay in ((100, 300), (400, 600)):
            with patch.object(self.module.time, "monotonic", return_value=now):
                self.plugin.poll_cloud_events()
            self.assertEqual(self.plugin._cloud_life_retry_after, now + delay)
            self.assertIn("123", self.plugin._cloud_life_candidates)
        self.assertFalse(self.plugin._pending)

    def test_life_lookup_budget_and_candidate_capacity_are_bounded(self):
        item, storage, event = self.life_fixture()
        storage.life_events.return_value = [{**event, "id": index, "file_id": str(index)} for index in range(1, 600)]
        storage.item_from_life_event.return_value = None
        with patch.object(self.module.time, "monotonic", return_value=100):
            self.plugin.poll_cloud_events()
        self.assertEqual(len(self.plugin._cloud_life_candidates), 512)
        self.assertEqual(storage.item_from_life_event.call_count, 8)

    def test_life_disabled_or_failure_does_not_start_downloads(self):
        item, storage = self.cloud_fixture()
        self.plugin._enabled = True
        self.plugin.poll_cloud_events()
        storage.life_events.assert_not_called()
        self.plugin._cloud_monitor_enabled = True
        storage.life_events.side_effect = RuntimeError("secret url")
        self.plugin.poll_cloud_events()
        self.assertIn("RuntimeError", self.plugin._cloud_status["life_error"])
        self.assertNotIn("secret", self.plugin._cloud_status["life_error"])
        self.assertFalse(self.plugin._pending)
        self.plugin.poll_cloud_events()
        storage.life_events.assert_called_once()
        self.assertIsNotNone(self.plugin._cloud_status["life_retry_at"])

    def test_cloud_failures_back_off_and_success_clears_stale_error(self):
        item, storage = self.cloud_fixture()
        key = self.cloud_module.record_key(item)
        for delay in (300, 600, 1200, 2400, 3600):
            self.plugin._stopping.clear()
            self.plugin._pending = {key: (0, item)}
            with patch.object(self.plugin, "_process_cloud_file", side_effect=self.cloud_module.CloudError("ReadTimeout")), patch.object(self.plugin, "_queue_cloud_file", side_effect=lambda *args, **kwargs: self.plugin._stopping.set()) as retry:
                self.plugin._work()
            retry.assert_called_once_with(item, delay=delay)
            self.assertIsNotNone(self.plugin._cloud_status["retry_at"])
        self.plugin._cloud_succeeded(key)
        self.assertEqual(self.plugin._cloud_status["error"], "")
        self.assertIsNone(self.plugin._cloud_status["retry_at"])
        self.assertNotIn(key, self.plugin._cloud_failures)

    def test_cloud_legacy_record_survives_repeated_pickcode_changes_without_hash_or_send(self):
        item, storage = self.cloud_fixture()
        cloud_storage = self.cloud_module
        with patch.object(cloud_storage, "hash_remote", return_value=self.module._md4(b"hello").hex()):
            self.plugin._process_cloud_file(item)
        records = self.records()
        key = cloud_storage.record_key(item)
        record = records[key]
        record["cloud_fingerprint"] = cloud_storage.fingerprint(item) + ["legacy-pickcode"]
        self.plugin._telegram_enabled = True
        self.plugin._telegram_chat_id = "@receiver"
        record["telegram_sent"] = True
        record["telegram_target"] = self.plugin._telegram_target_key()
        record["telegram_sent_at"] = 123
        expected = {**record, "cloud_fingerprint": cloud_storage.fingerprint(item)}
        _, _, cache_path = self.plugin._paths()
        self.plugin._write_results(None, cache_path, records)
        with patch.object(cloud_storage, "hash_remote", side_effect=AssertionError("unnecessary download")) as hashing, patch.object(self.plugin, "_notify_record") as notify:
            for pickcode in ("new", "newer", "latest"):
                current = {**item, "pickcode": pickcode}
                storage.fresh.return_value = current
                self.plugin._process_cloud_file(current)
        hashing.assert_not_called()
        notify.assert_not_called()
        self.assertEqual(storage.fresh.call_count, 1)
        self.assertEqual(self.records()[key], expected)
        self.assertFalse(self.plugin._pending)

    def test_cloud_first_hash_uses_latest_pickcode_from_fresh_listing(self):
        item, storage = self.cloud_fixture()
        latest = {**item, "pickcode": "latest"}
        storage.fresh.return_value = latest
        with patch.object(self.cloud_module, "hash_remote", return_value=self.module._md4(b"hello").hex()) as hashing:
            self.plugin._process_cloud_file(item)
        hashing.assert_called_once()
        self.assertEqual(hashing.call_args.args[1]["pickcode"], "latest")
        self.assertEqual(len(self.records()), 1)
        self.assertFalse(self.plugin._pending)

    def test_cloud_changed_size_or_modify_time_still_rehashes(self):
        item, storage = self.cloud_fixture()
        with patch.object(self.cloud_module, "hash_remote", return_value=self.module._md4(b"hello").hex()):
            self.plugin._process_cloud_file(item)
        for field, value in (("size", 6), ("modify_time", 2)):
            with self.subTest(field=field):
                item = {**item, field: value, "pickcode": "new"}
                storage.fresh.return_value = item
                with patch.object(self.cloud_module, "hash_remote", return_value=self.module._md4(b"hello!").hex()) as hashing:
                    self.plugin._process_cloud_file(item)
                hashing.assert_called_once()
                record = self.records()[self.cloud_module.record_key(item)]
                self.assertEqual(record["cloud_fingerprint"], self.cloud_module.fingerprint(item))

    def test_cloud_failure_does_not_save_or_send_link(self):
        self.plugin._cloud_enabled = True
        self.plugin._cloud_storage = "115网盘Plus"
        self.plugin._cloud_folder = "/cloud-test"
        item = {"storage": "115网盘Plus", "fileid": "123", "type": "file", "name": "云端.mkv", "path": "/cloud-test/云端.mkv", "size": 5, "modify_time": 1, "pickcode": "pick"}
        from ed2k_test_plugin import cloud_storage
        storage = Mock()
        storage.fresh.return_value = item
        self.plugin._cloud_adapter = storage
        with patch.object(cloud_storage, "hash_remote", side_effect=cloud_storage.CloudError("short read")), patch.object(self.plugin, "_notify_record") as notify:
            with self.assertRaises(cloud_storage.CloudError):
                self.plugin._process_cloud_file(item)
        notify.assert_not_called()
        self.assertFalse((self.root / "data/records.json").exists())

    def test_cloud_scan_requires_enabled_and_has_own_schedule(self):
        self.assertFalse(self.plugin.queue_cloud_scan()["ok"])
        self.plugin._enabled = True
        self.plugin._cloud_enabled = True
        self.plugin._cloud_folder = "/cloud-test"
        self.plugin._worker = threading.current_thread()
        self.assertTrue(self.plugin.queue_cloud_scan()["ok"])
        self.assertTrue(self.plugin.queue_cloud_scan()["ok"])
        self.assertEqual(list(self.plugin._pending), ["__cloud_scan__"])
        self.assertEqual(self.plugin.get_service(), [])
        self.plugin._cloud_scan_enabled = True
        self.assertEqual(self.plugin.get_service()[0]["id"], "Ed2kFolderWatcherCloud")

    def test_cloud_schedule_switch_does_not_disable_manual_or_life_events(self):
        item, storage = self.cloud_fixture()
        self.plugin._enabled = True
        self.plugin._worker = threading.current_thread()
        self.plugin._cloud_scan_enabled = False
        self.plugin._cloud_monitor_enabled = True
        self.assertEqual([service["id"] for service in self.plugin.get_service()], ["Ed2kFolderWatcherLife"])
        self.assertTrue(self.plugin.queue_cloud_scan()["ok"])
        self.plugin._pending.clear()
        storage.life_events.return_value = [{"id": 99, "type": 1, "update_time": self.plugin._cloud_life_started + 1, "file_id": "123", "file_category": 1}]
        storage.item_from_life_event.return_value = item
        self.plugin.poll_cloud_events()
        self.assertIn(self.cloud_module.record_key(item), self.plugin._pending)
        self.assertNotIn("__cloud_scan__", self.plugin._pending)
        self.plugin._cloud_enabled = False
        self.assertEqual(self.plugin.get_service(), [])

    def test_cloud_schedule_defaults_and_legacy_upgrade(self):
        self.assertFalse(self.plugin._cloud_scan_enabled)
        with patch.object(self.plugin, "_refresh_link_names"):
            self.plugin.init_plugin({"cloud_enabled": True, "cloud_folder": "/cloud-test"})
        self.assertTrue(self.plugin._cloud_scan_enabled)
        self.plugin.init_plugin({"cloud_enabled": True, "cloud_folder": "/cloud-test", "cloud_scan_enabled": False})
        self.assertFalse(self.plugin._cloud_scan_enabled)
        self.plugin._save_config()
        self.assertIs(self.plugin.update_config.call_args.args[0]["cloud_scan_enabled"], False)

    def test_form_uses_updated_headings_and_cloud_schedule_switch(self):
        form, defaults = self.plugin.get_form()
        fields = form[0]["content"]
        self.assertEqual([field["text"] for field in fields if field["component"] == "VCardTitle"], ["通用设置", "本地监控", "网盘监控", "ED2K链接自动转发"])
        models = {field.get("props", {}).get("model"): field for field in fields}
        self.assertEqual(models["cloud_scan_enabled"]["props"]["label"], "网盘定时增量补扫")
        self.assertEqual(models["cloud_folder"]["props"]["label"], "网盘监控目录")
        self.assertEqual(models["cloud_cron"]["props"]["label"], "网盘补扫计划")
        self.assertIn("不扫描已有文件", models["cloud_monitor_enabled"]["props"]["hint"])
        self.assertFalse(defaults["cloud_scan_enabled"])

    def test_empty_file_never_hashes_sends_or_deletes(self):
        path = self.create(data=b"")
        self.plugin._delete_source = True
        self.plugin._telegram_enabled = True
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("hashed")), patch.object(self.plugin, "_send_telegram", side_effect=AssertionError("sent")):
            self.plugin._process_file(path)
        self.assertTrue(path.exists())
        self.assertFalse((self.root / "data/records.json").exists())
        self.assertIn(str(path), self.plugin._pending)

    def test_empty_file_later_filled_is_processed(self):
        path = self.create(data=b"")
        self.plugin._process_file(path)
        path.write_bytes(b"complete file")
        self.plugin._process_file(path)
        self.assertGreater(self.records()[str(path)]["size"], 0)

    def test_invalid_empty_hash_not_published_or_deleted(self):
        path = self.create()
        self.plugin._delete_source = True
        with patch.object(self.module, "ed2k_hash", return_value=self.module.EMPTY_MD4), patch.object(self.plugin, "_send_telegram", side_effect=AssertionError("sent")):
            self.plugin._process_file(path)
        self.assertTrue(path.exists())
        self.assertFalse((self.root / "data/records.json").exists())

    def test_hash_rejects_short_read(self):
        path = self.create()
        handle = Mock()
        with path.open("rb") as original:
            handle.fileno.return_value = original.fileno()
            handle.read.return_value = b""
            manager = Mock()
            manager.__enter__ = Mock(return_value=handle)
            manager.__exit__ = Mock(return_value=False)
            with patch.object(Path, "open", return_value=manager), self.assertRaises(OSError):
                self.module.ed2k_hash(path)

    def test_invalid_history_excluded_from_txt_and_marked(self):
        path = self.create()
        self.plugin._process_file(path)
        records = self.records()
        invalid = dict(records[str(path)])
        invalid.update(size=0, link=f"ed2k://|file|empty.mkv|0|{self.module.EMPTY_MD4}|/", source_deleted=True)
        records[str(self.folder / "empty.mkv")] = invalid
        self.plugin._write_results(self.folder / "ed2k.txt", self.root / "data/records.json", records)
        self.assertNotIn("|0|", (self.folder / "ed2k.txt").read_text())
        item = next(item for item in self.plugin.recent_records()["items"] if not item["link_valid"])
        self.assertIn("0", item["link_error"])
        self.assertFalse(item["can_delete"])
        self.assertIn(str(self.folder / "empty.mkv"), self.records())

    def test_invalid_history_regenerates_with_delivery_reset(self):
        path = self.create()
        self.plugin._process_file(path)
        records = self.records()
        records[str(path)].update(link=f"ed2k://|file|test.bin|0|{self.module.EMPTY_MD4}|/", telegram_sent=True)
        self.plugin._write_results(None, self.root / "data/records.json", records)
        self.plugin._process_file(path)
        self.assertFalse(self.module.record_problem(self.records()[str(path)]))
        self.assertFalse(self.records()[str(path)]["telegram_sent"])

    def test_manual_delete_preserves_link_record(self):
        path = self.create()
        self.plugin._process_file(path)
        item = self.plugin.recent_records()["items"][0]
        self.assertTrue(item["can_delete"])
        result = self.plugin.delete_source(item["id"])
        self.assertTrue(result["ok"])
        self.assertFalse(path.exists())
        record = self.records()[str(path)]
        self.assertEqual(record["link"], item["link"])
        self.assertEqual(record["delete_method"], "manual")
        self.assertFalse(self.plugin.delete_source(item["id"])["ok"])

    def test_manual_delete_modified_source_rejected(self):
        path = self.create()
        self.plugin._process_file(path)
        item = self.plugin.recent_records()["items"][0]
        path.write_bytes(b"new content")
        self.assertFalse(self.plugin.delete_source(item["id"])["ok"])
        self.assertTrue(path.exists())

    def test_manual_delete_legacy_same_metadata_wrong_content_rejected(self):
        import os
        path = self.create()
        self.plugin._process_file(path)
        records = self.records()
        record = records[str(path)]
        for name in ("inode", "device", "ctime_ns"):
            record.pop(name)
        self.plugin._write_results(None, self.root / "data/records.json", records)
        path.write_bytes(b"other")
        os.utime(path, ns=(record["mtime_ns"], record["mtime_ns"]))
        item = self.plugin.recent_records()["items"][0]
        self.assertFalse(self.plugin.delete_source(item["id"])["ok"])
        self.assertTrue(path.exists())

    def test_manual_delete_outside_monitor_or_symlink_rejected(self):
        path = self.create()
        self.plugin._process_file(path)
        item = self.plugin.recent_records()["items"][0]
        outside = self.root / "outside.bin"
        path.rename(outside)
        path.symlink_to(outside)
        self.assertFalse(self.plugin.delete_source(item["id"])["ok"])
        self.assertTrue(outside.exists())
        self.plugin._folder = str(self.root / "different")
        self.assertFalse(self.plugin.delete_source(item["id"])["ok"])

    def test_manual_delete_rechecks_after_hash(self):
        path = self.create()
        self.plugin._process_file(path)
        item = self.plugin.recent_records()["items"][0]
        digest = item["link"].split("|")[4]
        def change(*args):
            path.write_bytes(b"changed")
            return digest
        with patch.object(self.module, "ed2k_hash", side_effect=change):
            self.assertFalse(self.plugin.delete_source(item["id"])["ok"])
        self.assertTrue(path.exists())

    def test_manual_delete_storage_failure_does_not_delete(self):
        path = self.create()
        self.plugin._process_file(path)
        item = self.plugin.recent_records()["items"][0]
        with patch.object(self.plugin, "_write_results", side_effect=OSError("disk full")):
            self.assertFalse(self.plugin.delete_source(item["id"])["ok"])
        self.assertTrue(path.exists())

    def test_manual_delete_invalid_history_rejected(self):
        path = self.create(data=b"")
        record = {"name": path.name, "size": 0, "mtime_ns": path.stat().st_mtime_ns,
                  "link": f"ed2k://|file|test.bin|0|{self.module.EMPTY_MD4}|/", "source_deleted": False}
        self.plugin._write_results(None, self.root / "data/records.json", {str(path): record})
        item = self.plugin.recent_records()["items"][0]
        self.assertFalse(item["link_valid"])
        self.assertFalse(item["can_delete"])
        self.assertFalse(self.plugin.delete_source(item["id"])["ok"])
        self.assertTrue(path.exists())

    def test_manual_delete_directory_symlink_swap_rejected(self):
        folder = self.folder / "subdir"
        folder.mkdir()
        path = folder / "test.bin"
        path.write_bytes(b"hello")
        self.plugin._process_file(path)
        item = self.plugin.recent_records()["items"][0]
        outside = self.root / "outside"
        digest = item["link"].split("|")[4]
        def replace_parent(*args):
            folder.rename(outside)
            folder.symlink_to(outside, target_is_directory=True)
            return digest
        with patch.object(self.module, "ed2k_hash", side_effect=replace_parent):
            self.assertFalse(self.plugin.delete_source(item["id"])["ok"])
        self.assertTrue((outside / "test.bin").exists())

    def test_configured_monitor_symlink_still_deletes_correct_file(self):
        alias = self.root / "alias"
        alias.symlink_to(self.folder, target_is_directory=True)
        self.plugin._folder = str(alias)
        self.plugin._delete_source = True
        path = self.create()
        self.plugin._process_file(alias / path.name)
        self.assertFalse(path.exists())

    def test_multiple_targets_normalize_and_deduplicate(self):
        self.plugin._telegram_chat_id = "@Receiver\nhttps://t.me/receiver, -100123；@group|123;@other|"
        self.plugin._telegram_thread_id = "456"
        self.assertEqual(self.plugin._telegram_targets(), [("@Receiver", 456), ("-100123", 456), ("@group", 123), ("@other", None)])

    def test_multiple_targets_invalid_or_empty_rejected(self):
        for value in ("", "@valid\ninvalid target", "@group|0", "@group|12|34", ";\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.plugin._telegram_chat_id = value
                self.plugin._telegram_targets()

    def test_multiple_targets_limit(self):
        self.plugin._telegram_chat_id = "\n".join(f"@target{number}" for number in range(51))
        with self.assertRaises(ValueError):
            self.plugin._telegram_targets()

    def test_multi_target_partial_delivery_retry_and_checkpoint(self):
        self.plugin._telegram_enabled = True
        self.plugin._telegram_chat_id = "@first\n@second\n@third"
        record = {"link": "pure-link", "telegram_sent": False}
        checkpoint = Mock()
        with patch.object(self.plugin, "_send_telegram", side_effect=[True, False, True]) as sender:
            self.plugin._notify_record(Path("test.bin"), record, checkpoint)
        self.assertEqual(sender.call_count, 3)
        self.assertEqual(checkpoint.call_count, 2)
        self.assertFalse(record["telegram_sent"])
        record = json.loads(json.dumps(record))
        with patch.object(self.plugin, "_send_telegram", return_value=True) as sender:
            self.plugin._notify_record(Path("test.bin"), record)
        sender.assert_called_once_with(Path("test.bin"), "pure-link", target=("@second", None))
        self.assertTrue(record["telegram_sent"])

    def test_multi_target_reorder_and_add_only_new(self):
        self.plugin._telegram_enabled = True
        self.plugin._telegram_chat_id = "@first\n@second"
        record = {"link": "pure-link"}
        with patch.object(self.plugin, "_send_telegram", return_value=True):
            self.plugin._notify_record(Path("test.bin"), record)
        key = self.plugin._telegram_target_key()
        self.plugin._telegram_chat_id = "https://t.me/SECOND;first"
        self.assertEqual(key, self.plugin._telegram_target_key())
        self.plugin._telegram_chat_id += ";@third"
        with patch.object(self.plugin, "_send_telegram", return_value=True) as sender:
            self.plugin._notify_record(Path("test.bin"), record)
        sender.assert_called_once_with(Path("test.bin"), "pure-link", target=("@third", None))

    def test_multi_target_legacy_delivery_migrates(self):
        import hashlib
        from ed2k_test_plugin.user_account import TelegramUserAccount
        self.plugin._telegram_enabled = True
        identity = TelegramUserAccount(Path(self.plugin.get_data_path())).identity()
        legacy = hashlib.sha256(f"user|{identity}|@first|".encode()).hexdigest()
        record = {"link": "pure-link", "telegram_sent": True, "telegram_target": legacy}
        self.plugin._telegram_chat_id = "@first\n@second"
        with patch.object(self.plugin, "_send_telegram", return_value=True) as sender:
            self.plugin._notify_record(Path("test.bin"), record)
        sender.assert_called_once_with(Path("test.bin"), "pure-link", target=("@second", None))

    def test_multi_target_checkpoint_failure_stops_delivery(self):
        self.plugin._telegram_enabled = True
        self.plugin._telegram_chat_id = "@first\n@second"
        with patch.object(self.plugin, "_send_telegram", return_value=True) as sender:
            with self.assertRaises(OSError):
                self.plugin._notify_record(Path("test.bin"), {"link": "pure-link"}, Mock(side_effect=OSError("disk full")))
        self.assertEqual(sender.call_count, 1)

    def test_multi_target_delete_requires_all_and_persists_partial(self):
        path = self.create()
        self.plugin._telegram_enabled = True
        self.plugin._telegram_chat_id = "@first\n@second"
        self.plugin._delete_source = True
        with patch.object(self.plugin, "_send_telegram", side_effect=[True, False]):
            self.plugin._process_file(path)
        self.assertTrue(path.exists())
        record = self.records()[str(path.resolve())]
        self.assertEqual(len(record["telegram_deliveries"]), 1)
        self.assertFalse(record["telegram_sent"])
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("rehashed")), patch.object(self.plugin, "_send_telegram", return_value=True) as sender:
            self.plugin._process_file(path)
        self.assertEqual(sender.call_count, 1)
        self.assertFalse(path.exists())

    def test_multi_target_failure_cooldown_does_not_block_others(self):
        from ed2k_test_plugin.user_account import UserAccountError
        self.plugin._telegram_enabled = True
        self.plugin._telegram_chat_id = "@first\n@second"
        self.plugin._stopping = Mock()
        self.plugin._stopping.wait.return_value = False
        account = Mock()
        account.identity.return_value = "test-user"
        account.send.side_effect = [UserAccountError("目标无权发送"), None]
        runtime = types.ModuleType("app.runtime.settings")
        runtime.get_runtime_setting = Mock(return_value=None)
        with patch.dict(sys.modules, {"app.runtime.settings": runtime}), patch("ed2k_test_plugin.user_account.TelegramUserAccount", return_value=account):
            self.assertFalse(self.plugin._send_telegram_user(Path("test.bin"), "link", target=("@first", None)))
            self.assertTrue(self.plugin._send_telegram_user(Path("test.bin"), "link", target=("@second", None)))
            self.assertFalse(self.plugin._send_telegram_user(Path("test.bin"), "link", target=("@first", None)))
        self.assertEqual(account.send.call_count, 2)

    def test_multi_target_account_flood_wait_blocks_remaining(self):
        from ed2k_test_plugin.user_account import UserAccountError
        self.plugin._telegram_enabled = True
        account = Mock()
        account.send.side_effect = UserAccountError("限速", 123, account_wide=True)
        runtime = types.ModuleType("app.runtime.settings")
        runtime.get_runtime_setting = Mock(return_value=None)
        with patch.dict(sys.modules, {"app.runtime.settings": runtime}), patch("ed2k_test_plugin.user_account.TelegramUserAccount", return_value=account):
            self.assertFalse(self.plugin._send_telegram_user(Path("test.bin"), "link", target=("@first", None)))
            self.assertFalse(self.plugin._send_telegram_user(Path("test.bin"), "link", target=("@second", None)))
        self.assertEqual(account.send.call_count, 1)

    def records(self):
        return json.loads((self.root / "data" / "records.json").read_text())

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("worker timeout")

    def test_md4_vectors_and_fallback(self):
        for data, expected in [(b"", "31d6cfe0d16ae931b73c59d7e0c089c0"), (b"abc", "a448017aaf21d8525fc10ae87aa6729d")]:
            self.assertEqual(self.module._md4(data).hex(), expected)
            path = self.create(data=data)
            with patch.object(self.module, "_CryptoMD4", None):
                self.assertEqual(self.module.ed2k_hash(path), expected)

    def test_telegram_destination_formats(self):
        for value, expected in [(" 12345 ", "12345"), ("-10012345", "-10012345"), ("receiver_bot", "@receiver_bot"), ("@receiver_bot", "@receiver_bot"), ("https://t.me/receiver_bot", "@receiver_bot"), ("t.me/my_channel/", "@my_channel")]:
            with self.subTest(value=value):
                self.assertEqual(self.module.telegram_destination(value), expected)
        for value in ("https://t.me/+invite", "https://t.me/channel/123", "https://example.com/receiver", "", "two names"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.module.telegram_destination(value)

    def test_telegram_error_hints(self):
        error = RuntimeError("Bad Request: chat not found")
        hint = self.module.telegram_error_hint(error)
        self.assertIn("/start", hint)
        self.assertIn("@用户名", hint)
        self.assertIn("Bot-to-Bot", hint)
        error.error_code = 403
        error.description = "Forbidden"
        self.assertIn("频道发消息权限", self.module.telegram_error_hint(error))
        error.error_code = 401
        self.assertIn("Token 无效", self.module.telegram_error_hint(error))

    def test_legacy_bot_config_forces_user_mode(self):
        self.plugin.init_plugin({"folder": str(self.folder), "telegram_mode": "bot", "telegram_token": "private-token"})
        self.assertEqual(self.plugin._telegram_mode, "user")
        with patch.object(self.plugin, "_send_telegram_user", return_value=False) as sender, patch.object(self.module, "TelegramBotApi") as bot:
            self.plugin._telegram_enabled = True
            self.assertFalse(self.plugin._send_telegram(Path("test.mkv"), "link"))
            sender.assert_called_once()
            bot.assert_not_called()

    def test_disabled_telegram_does_not_send(self):
        with patch.object(self.plugin, "_send_telegram_user") as sender:
            self.assertTrue(self.plugin._send_telegram(Path("test.mkv"), "link"))
            sender.assert_not_called()
        form, defaults = self.plugin.get_form()
        self.assertEqual(defaults["telegram_mode"], "user")
        self.assertNotIn('"model": "telegram_token"', json.dumps(form))
        self.assertNotIn('"model": "telegram_mode"', json.dumps(form))

    def test_user_mode_no_bot_token_or_bot_fallback(self):
        self.plugin._telegram_enabled = True
        self.plugin._telegram_mode = "user"
        self.plugin._telegram_token = ""
        self.plugin._telegram_chat_id = "@receiver_bot"
        account = Mock()
        user_module = types.ModuleType("ed2k_test_plugin.user_account")
        user_module.TelegramUserAccount = Mock(return_value=account)
        user_module.UserAccountError = type("UserAccountError", (Exception,), {})
        settings = types.ModuleType("app.runtime.settings")
        settings.get_runtime_setting = Mock(return_value=None)
        with patch.dict(sys.modules, {"ed2k_test_plugin.user_account": user_module, "app.runtime.settings": settings}), patch.object(self.module, "TelegramBotApi", side_effect=AssertionError("bot fallback")):
            self.assertTrue(self.plugin._send_telegram(Path("中文.mkv"), "pure-link"))
            account.send.assert_called_once_with("@receiver_bot", "pure-link", thread_id=None)
            account.send.side_effect = user_module.UserAccountError("请先登录")
            self.assertFalse(self.plugin._send_telegram(Path("中文.mkv"), "pure-link"))
            self.assertFalse(self.plugin._send_telegram(Path("中文.mkv"), "pure-link"))
            self.assertEqual(account.send.call_count, 2)

    def test_user_mode_saved_without_auth_secrets(self):
        self.plugin._telegram_mode = "user"
        self.plugin._save_config()
        saved = self.plugin.update_config.call_args.args[0]
        self.assertEqual(saved["telegram_mode"], "user")
        self.assertNotIn("telegram_user_api_hash", saved)
        self.assertNotIn("telegram_user_session", saved)

    def test_user_target_dedup_ignores_bot_token_and_separates_accounts(self):
        self.plugin._telegram_token = "bot-token"
        self.plugin._telegram_chat_id = "@receiver_bot"
        account = Mock()
        account.identity.return_value = "first-user"
        user_module = types.ModuleType("ed2k_test_plugin.user_account")
        user_module.TelegramUserAccount = Mock(return_value=account)
        self.plugin._telegram_mode = "user"
        with patch.dict(sys.modules, {"ed2k_test_plugin.user_account": user_module}):
            first_key = self.plugin._telegram_target_key()
            self.plugin._telegram_mode = "bot"
            self.assertEqual(first_key, self.plugin._telegram_target_key())
            self.plugin._telegram_token = "unrelated-token"
            self.assertEqual(first_key, self.plugin._telegram_target_key())
            account.identity.return_value = "second-user"
            self.assertNotEqual(first_key, self.plugin._telegram_target_key())

    def test_link_preserves_chinese_and_escapes_delimiters(self):
        path = self.create("大哥小助理.母带.100% |测试.mkv")
        link = self.module.ed2k_link(path, "a" * 32)
        self.assertEqual(link, f"ed2k://|file|大哥小助理.母带.100%25 %7C测试.mkv|5|{'a' * 32}|/")
        self.assertEqual(len(link.split("|")), 6)
        self.assertEqual(self.module.ed2k_filename("中文\n\u2028.mkv"), "中文%0A%E2%80%A8.mkv")

    def test_filename_preserves_spaces_and_punctuation(self):
        name = "花开锦绣 - S01E10 - 第 10 集 （国语）(2026) [2160p]【母带】，。！.mkv"
        self.assertEqual(self.module.ed2k_filename(name), name)
        self.assertEqual(self.module.ed2k_filename("分隔|百分%?#/\\\t.mkv"), "分隔%7C百分%25%3F%23%2F%5C%09.mkv")

    def test_upgrade_rewrites_escaped_spaces_and_parentheses(self):
        path = self.create("花开锦绣 - 第 10 集 (2026)（国语）.mkv")
        self.plugin._process_file(path)
        records = self.records()
        record = records[str(path)]
        record["link"] = f"ed2k://|file|{self.module.quote(path.name, safe='')}|5|{'a' * 32}|/"
        record["telegram_sent"] = True
        record["telegram_target"] = "previous-target"
        self.plugin._write_results(self.folder / "ed2k.txt", self.root / "data" / "records.json", records)
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("rehashed")), patch.object(self.plugin, "_notify_record", side_effect=AssertionError("resent")):
            self.plugin._refresh_link_names()
        updated = self.records()[str(path)]
        self.assertEqual(updated["link"], f"ed2k://|file|{path.name}|5|{'a' * 32}|/")
        self.assertTrue(updated["telegram_sent"])
        self.assertEqual(updated["telegram_target"], "previous-target")
        self.assertEqual(updated["link"], (self.folder / "ed2k.txt").read_text().strip())

    def test_upgrade_rewrites_old_links_without_hashing_or_sending(self):
        path = self.create("大哥小助理.%E4%B8%AD.mkv")
        self.plugin._process_file(path)
        path.unlink()
        records = self.records()
        record = records[str(path)]
        record["link"] = f"ed2k://|file|{self.module.quote(path.name, safe='')}|5|{'a' * 32}|/"
        record["telegram_sent"] = True
        record["telegram_target"] = "previous-target"
        self.plugin._write_results(self.folder / "ed2k.txt", self.root / "data" / "records.json", records)
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("rehashed")), patch.object(self.plugin, "_send_telegram", side_effect=AssertionError("resent")):
            self.plugin._refresh_link_names()
        updated = self.records()[str(path)]
        self.assertIn("大哥小助理.%25E4%25B8%25AD.mkv", updated["link"])
        self.assertTrue(updated["telegram_sent"])
        self.assertEqual(updated["telegram_target"], "previous-target")
        self.assertEqual(updated["link"], (self.folder / "ed2k.txt").read_text().strip())

    def test_upgrade_respects_txt_disabled(self):
        path = self.create("中文.mkv")
        self.plugin._process_file(path)
        records = self.records()
        records[str(path)]["link"] = f"ed2k://|file|%E4%B8%AD%E6%96%87.mkv|5|{'a' * 32}|/"
        output = self.folder / "ed2k.txt"
        cache = self.root / "data" / "records.json"
        self.plugin._write_results(output, cache, records)
        previous = output.read_text()
        self.plugin._output_enabled = False
        self.plugin._refresh_link_names()
        self.assertIn("中文.mkv", self.records()[str(path)]["link"])
        self.assertEqual(output.read_text(), previous)

    def test_boundary_hashes(self):
        from Crypto.Hash import MD4
        part_size = self.module.ED2K_PART_SIZE
        rhash_vectors = {
            0: "31d6cfe0d16ae931b73c59d7e0c089c0",
            1: "bde52cb31de33e46245e05fbdbd6fb24",
            9727999: "5461275e76837a313a0b2f67811c1023",
            9728000: "ee15063dd1e9c5bd5c0e4205c0b8e698",
            9728001: "748c0171a2d42d28afb644ef3e17f4e7",
            19456000: "fcca57f6ae31dcfa2ce0e41119738eb1",
        }
        for size in (0, 1, part_size - 1, part_size, part_size + 1, 2 * part_size):
            with self.subTest(size=size):
                data = b"a" * size
                if size < part_size:
                    expected = MD4.new(data).hexdigest()
                else:
                    digests = [MD4.new(data[offset:offset + part_size]).digest() for offset in range(0, size, part_size)]
                    if size % part_size == 0:
                        digests.append(MD4.new().digest())
                    expected = MD4.new(b"".join(digests)).hexdigest()
                self.assertEqual(self.module.ed2k_hash(self.create(data=data)), expected)
                self.assertEqual(expected, rhash_vectors[size])

    def test_incremental_and_history_after_removal(self):
        first = self.create()
        self.plugin._process_file(first)
        first.unlink()
        second = self.create("second.bin", b"new")
        self.plugin._process_file(second)
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("rehashed")):
            self.plugin._process_file(second)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(len((self.folder / "ed2k.txt").read_text().splitlines()), 2)

    def test_txt_disabled_and_legacy_migration(self):
        self.plugin._output_enabled = False
        path = self.create()
        legacy = self.folder / ".ed2k.txt.cache.json"
        legacy.write_text(json.dumps({"old": {"link": "old-link"}}))
        self.plugin._process_file(path)
        self.assertFalse((self.folder / "ed2k.txt").exists())
        self.assertIn("old", self.records())
        self.plugin._output = str(self.root / "different" / "links.txt")
        self.assertEqual(len(self.records()), 2)

    def test_self_outputs_and_symlinks_excluded(self):
        path = self.create()
        self.plugin._process_file(path)
        self.assertFalse(self.plugin._eligible(self.folder / "ed2k.txt"))
        self.assertFalse(self.plugin._eligible(self.create(".ed2k-write-test")))
        self.assertFalse(self.plugin._eligible(self.create("download.part")))
        symbolic = self.folder / "symbolic.bin"
        symbolic.symlink_to(path)
        self.assertFalse(self.plugin._eligible(symbolic))

    def test_safety_no_destination_no_delete(self):
        self.plugin._output_enabled = False
        self.plugin._delete_source = True
        path = self.create()
        self.plugin._process_file(path)
        self.assertTrue(path.exists())

    def test_delete_only_after_success(self):
        self.plugin._delete_source = True
        path = self.create()
        self.plugin._process_file(path)
        self.assertFalse(path.exists())
        self.assertEqual(len(self.records()), 1)
        self.assertIn("ed2k://", (self.folder / "ed2k.txt").read_text())
        record = self.records()[str(path)]
        self.assertTrue(record["source_deleted"])
        self.assertGreaterEqual(record["deleted_at"], record["generated_at"])
        self.assertEqual(self.plugin.recent_records()["items"][0]["source_status"], "deleted")

    def test_recent_page_and_empty_records(self):
        self.assertTrue(self.plugin.get_page())
        self.assertEqual(self.plugin.recent_records()["items"], [])
        self.assertEqual(self.plugin.recent_records()["total"], 0)

    def test_recent_records_retained_and_timestamp(self):
        path = self.create()
        self.plugin._process_file(path)
        self.assertTrue(path.exists())
        record = self.plugin.recent_records()["items"][0]
        self.assertEqual(record["source_status"], "retained")
        self.assertEqual(record["name"], path.name)
        self.assertGreater(record["generated_at"], 0)
        self.assertNotIn("path", record)

    def test_recent_records_legacy_missing_file_not_inferred_deleted(self):
        path = self.create()
        self.plugin._process_file(path)
        records = self.records()
        records[str(path)].pop("source_deleted")
        records[str(path)].pop("generated_at")
        path.unlink()
        self.plugin._write_results(None, self.root / "data" / "records.json", records)
        record = self.plugin.recent_records()["items"][0]
        self.assertEqual(record["source_status"], "unknown")
        self.assertIsNone(record["generated_at"])

    def test_recent_records_sorted_limited_and_read_only(self):
        records = {str(self.folder / f"{index}.mkv"): {"name": f"{index}.mkv", "generated_at": index,
                   "link": f"link-{index}", "source_deleted": False} for index in range(105)}
        cache = self.root / "data" / "records.json"
        self.plugin._write_results(None, cache, records)
        before = cache.read_bytes()
        with patch.object(self.plugin, "scan", side_effect=AssertionError("scanned")):
            result = self.plugin.recent_records()
        self.assertEqual(result["total"], 105)
        self.assertEqual(len(result["items"]), 100)
        self.assertEqual(result["items"][0]["name"], "104.mkv")
        self.assertEqual(result["items"][-1]["name"], "5.mkv")
        self.assertEqual(cache.read_bytes(), before)

    def test_recent_records_bad_cache_does_not_overwrite(self):
        cache = self.root / "data" / "records.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("invalid json")
        self.assertFalse(self.plugin.recent_records()["ok"])
        self.assertEqual(cache.read_text(), "invalid json")

    def test_delete_failure_recorded_and_retry_success(self):
        self.plugin._delete_source = True
        path = self.create()
        with patch.object(self.plugin, "_unlink_source", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.plugin._process_file(path)
        self.assertTrue(path.exists())
        self.assertEqual(self.plugin.recent_records()["items"][0]["source_status"], "delete_failed")
        self.plugin._process_file(path)
        self.assertFalse(path.exists())
        self.assertEqual(self.plugin.recent_records()["items"][0]["source_status"], "deleted")

    def test_generation_time_preserved_on_unchanged_retry(self):
        path = self.create()
        self.plugin._process_file(path)
        generated = self.records()[str(path)]["generated_at"]
        self.plugin._delete_source = True
        self.plugin._process_file(path)
        self.assertEqual(self.records()[str(path)]["generated_at"], generated)

    def test_write_failure_preserves_source_and_cache(self):
        path = self.create()
        self.plugin._delete_source = True
        original = self.plugin._atomic_write

        def fail_txt(target, text):
            if target.suffix == ".txt":
                raise OSError("disk full")
            return original(target, text)

        with patch.object(self.plugin, "_atomic_write", side_effect=fail_txt):
            with self.assertRaises(OSError):
                self.plugin._process_file(path)
        self.assertTrue(path.exists())
        self.assertEqual(len(self.records()), 1)

    def test_telegram_failure_retries_without_rehash(self):
        path = self.create()
        self.plugin._telegram_enabled = True
        self.plugin._telegram_chat_id = "@receiver"
        self.plugin._delete_source = True
        with patch.object(self.plugin, "_send_telegram", return_value=False):
            self.plugin._process_file(path)
        self.assertTrue(path.exists())
        self.assertIn(str(path), self.plugin._pending)
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("rehashed")), patch.object(self.plugin, "_send_telegram", return_value=True):
            self.plugin._process_file(path)
        self.assertFalse(path.exists())

    def test_change_during_notification_prevents_delete(self):
        path = self.create()
        self.plugin._telegram_enabled = True
        self.plugin._telegram_chat_id = "@receiver"
        self.plugin._delete_source = True

        def change(*args):
            path.write_bytes(b"replacement content")
            return True

        with patch.object(self.plugin, "_send_telegram", side_effect=change):
            self.plugin._process_file(path)
        self.assertTrue(path.exists())

    def test_change_during_hash_requeues(self):
        path = self.create()

        def change(*args):
            path.write_bytes(b"different")
            return "0" * 32

        with patch.object(self.module, "ed2k_hash", side_effect=change):
            self.plugin._process_file(path)
        self.assertIn(str(path), self.plugin._pending)
        self.assertFalse((self.folder / "ed2k.txt").exists())

    def test_corrupt_cache_not_silently_overwritten(self):
        path = self.create()
        data = self.root / "data"
        data.mkdir()
        (data / "records.json").write_text("broken")
        with self.assertRaises(ValueError):
            self.plugin._process_file(path)
        self.assertEqual((data / "records.json").read_text(), "broken")

    def test_realtime_create_and_move_directory(self):
        if self.module.Observer is None:
            self.skipTest("watchdog not installed")
        self.plugin.init_plugin({"enabled": True, "folder": str(self.folder), "min_age": 0})
        self.assertEqual(self.plugin.get_service(), [])
        path = self.create()
        self.wait_for(lambda: (self.root / "data" / "records.json").exists())
        self.assertIn(str(path), self.records())
        staged = self.root / "staged"
        staged.mkdir()
        (staged / "moved.bin").write_bytes(b"moved")
        staged.rename(self.folder / "arrived")
        self.wait_for(lambda: len(self.records()) == 2)

    def test_stop_cancels_pending_tasks(self):
        self.plugin.init_plugin({"enabled": True, "folder": str(self.folder), "min_age": 60})
        self.create()
        self.plugin.stop_service()
        self.assertFalse(self.plugin._worker)
        self.assertFalse((self.folder / "ed2k.txt").exists())

    def test_growing_file_event_is_delayed(self):
        self.plugin.init_plugin({"enabled": True, "monitor_enabled": False, "folder": str(self.folder), "min_age": 0})
        path = self.create()
        self.plugin._queue_event(path)
        time.sleep(0.5)
        path.write_bytes(b"still downloading")
        time.sleep(0.7)
        self.assertFalse((self.folder / "ed2k.txt").exists())
        self.wait_for(lambda: (self.folder / "ed2k.txt").exists())
        self.assertEqual(self.records()[str(path)]["size"], len(b"still downloading"))

    def test_atomic_replace_failure_preserves_old_txt(self):
        output = self.folder / "ed2k.txt"
        output.write_text("previous result")
        with patch.object(self.module.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.plugin._atomic_write(output, "new result")
        self.assertEqual(output.read_text(), "previous result")
        self.assertFalse(list(self.folder.glob(".ed2k-write-*")))

    def test_txt_disabled_does_not_modify_existing_file(self):
        output = self.folder / "ed2k.txt"
        output.write_text("keep unchanged")
        self.plugin._output_enabled = False
        self.plugin._process_file(self.create())
        self.assertEqual(output.read_text(), "keep unchanged")

    def test_invalid_extension_and_nonrecursive(self):
        path = self.create()
        self.plugin._extensions = "mkv,mp4"
        self.assertFalse(self.plugin._eligible(path))
        child = self.folder / "child"
        child.mkdir()
        nested = child / "test.mkv"
        nested.write_bytes(b"data")
        self.plugin._recursive = False
        self.assertFalse(self.plugin._eligible(nested))


if __name__ == "__main__":
    unittest.main()
