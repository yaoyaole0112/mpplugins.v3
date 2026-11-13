import copy
import importlib.util
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_ed2kfolderwatcher as existing


spec = importlib.util.spec_from_file_location("record_status_test", Path(__file__).resolve().parents[1] / "record_status.py")
status_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status_module)


class WorkflowSummaryTests(unittest.TestCase):
    def summary(self, *, normal="success", ordinary="success", enabled=True, problem="", failure=None, **record):
        row = {"upload_mode": "mp", "upload_verified": True, "upload_kind": "ordinary", **record}
        return status_module.workflow_summary(
            row, problem, {"state": normal, "text": normal}, {"state": ordinary, "text": ordinary}, enabled, failure,
        )

    def test_success_replaces_all_three_stale_notes(self):
        for note in ("等待115内容核验", "Telegram尚未全部投递成功；120秒后重试", "115云端大小与SHA1校验通过"):
            for state in ("queued", "verifying", "failed", "verified"):
                result = self.summary(upload_state=state, upload_note=note)
                self.assertEqual(result["state"], "success")
                self.assertEqual(result["label"], "已完成")
                self.assertEqual(result["detail"], "")
                self.assertNotIn(note, str(result))

    def test_normal_and_ordinary_deliveries_are_independent_requirements(self):
        for normal, ordinary in (("pending", "success"), ("success", "pending"), ("pending", "pending")):
            result = self.summary(normal=normal, ordinary=ordinary)
            self.assertEqual(result["state"], "pending")
            self.assertEqual(result["label"], "等待转发完成")

    def test_instant_upload_skips_ordinary_but_requires_normal_delivery(self):
        result = self.summary(upload_kind="instant", ordinary="pending")
        self.assertEqual(result["state"], "success")
        self.assertEqual(result["steps"][-1]["text"], "秒传跳过")
        self.assertEqual(self.summary(upload_kind="instant", normal="pending")["state"], "pending")

    def test_unknown_kind_is_not_silently_reported_as_completed(self):
        for kind in (None, "", "unknown"):
            result = self.summary(upload_kind=kind)
            self.assertEqual(result["state"], "pending")
            self.assertIn("上传方式", result["label"])
            self.assertEqual(result["steps"][-1]["text"], "上传方式未知，未执行")
            self.assertEqual(self.summary(upload_kind=kind, enabled=False)["state"], "success")

    def test_upload_not_verified_never_shows_completed(self):
        for state in ("computed", "upload_queued", "uploading", "verifying", "waiting_mp"):
            result = self.summary(upload_verified=False, upload_state=state)
            self.assertEqual(result["state"], "pending")
            self.assertEqual(result["steps"][1]["text"], "待完成")

    def test_actual_upload_failure_remains_visible_in_red(self):
        result = self.summary(upload_verified=False, upload_state="failed", upload_note="远端内容不一致")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["detail"], "远端内容不一致")

    def test_failed_postprocessing_is_red_only_while_delivery_is_unresolved(self):
        result = self.summary(normal="pending", upload_state="failed", upload_note="Telegram发送失败")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["detail"], "Telegram发送失败")
        self.assertEqual(self.summary(upload_state="failed", upload_note="Telegram发送失败")["state"], "success")

    def test_invalid_link_overrides_old_success_receipts(self):
        result = self.summary(problem="无效链接")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["steps"][0]["text"], "链接无效")
        self.assertEqual(result["steps"][1]["text"], "需重新校验")

    def test_disabled_channels_and_deletion_do_not_imply_missing_delivery(self):
        result = self.summary(normal="inactive", enabled=False, source_deleted=False)
        self.assertEqual(result["state"], "success")
        self.assertEqual(result["label"], "计算与校验已完成")
        self.assertEqual(result["steps"][-1]["text"], "未启用")

    def test_plain_monitored_files_do_not_claim_an_upload_or_cloud_verification(self):
        result = status_module.workflow_summary(
            {"source_type": "cloud"}, "", {"state": "success", "text": "已投递"}, {}, True,
        )
        self.assertEqual(result["state"], "success")
        self.assertEqual([step["key"] for step in result["steps"]], ["compute", "telegram"])

    def test_invalid_target_is_a_failure_not_success(self):
        self.assertEqual(self.summary(normal="failed")["state"], "failed")
        self.assertEqual(self.summary(ordinary="failed")["state"], "failed")

    def test_exhausted_unresolved_tasks_remain_failed_but_stale_failures_do_not_override_success(self):
        failure = {"exhausted": True, "error": "发送失败"}
        result = self.summary(normal="pending", failure=failure)
        self.assertEqual(result["state"], "failed")
        self.assertIn("停止自动重试", result["label"])
        self.assertEqual(result["detail"], "发送失败")
        self.assertEqual(self.summary(failure=failure)["state"], "success")


class RecordStatusApiTests(unittest.TestCase):
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

    def record(self, *, normal=True, ordinary=True, **fields):
        path = self.create()
        self.plugin._process_file(path)
        self.plugin._telegram_enabled = True
        self.plugin._telegram_chat_id = "@normal"
        self.plugin._ordinary_upload_telegram_enabled = True
        self.plugin._ordinary_upload_telegram_targets = "@ordinary"
        records = self.records()
        row = records[str(path)]
        row.update(upload_mode="mp", upload_verified=True, upload_state="queued", upload_kind="ordinary",
                   upload_note="Telegram尚未全部投递成功；120秒后重试")
        row.update(fields)
        for channel, sent in (("telegram", normal), ("ordinary_upload_telegram", ordinary)):
            row[f"{channel}_sent"] = sent
            row[f"{channel}_target"] = (
                self.plugin._telegram_target_key() if channel == "telegram"
                else self.plugin._ordinary_upload_telegram_target_key()
            ) if sent else ""
        cache = self.root / "data" / "records.json"
        self.plugin._write_results(None, cache, records)
        return path, row, cache

    def test_current_receipts_produce_complete_summary_without_changing_records_or_ledger(self):
        _, row, cache = self.record()
        store = self.plugin._telegram_delivery_store
        store.checkpoint([row])
        ledger_before, records_before = store.path.read_bytes(), cache.read_bytes()
        memory_before = copy.deepcopy(store.links)
        with patch.object(store, "checkpoint", side_effect=AssertionError("display must not write")), \
                patch.object(self.plugin, "_send_telegram", side_effect=AssertionError("display must not send")):
            result = self.plugin.recent_records()
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"][0]["display_status"]["state"], "success")
        self.assertEqual(store.path.read_bytes(), ledger_before)
        self.assertEqual(cache.read_bytes(), records_before)
        self.assertEqual(store.links, memory_before)

    def test_durable_receipts_win_over_stale_false_record_flags(self):
        _, row, cache = self.record()
        self.plugin._telegram_delivery_store.checkpoint([row])
        records = self.records()
        for channel in ("telegram", "ordinary_upload_telegram"):
            records[next(iter(records))].update({f"{channel}_sent": False, f"{channel}_target": ""})
        self.plugin._write_results(None, cache, records)
        self.assertEqual(self.plugin.recent_records()["items"][0]["display_status"]["state"], "success")

    def test_new_target_account_and_topic_are_not_marked_delivered_by_old_receipts(self):
        self.record()
        self.plugin._telegram_chat_id = "@normal,@new_target"
        result = self.plugin.recent_records()["items"][0]["display_status"]
        self.assertEqual(result["state"], "pending")
        self.plugin._telegram_chat_id = "@normal"
        self.plugin._telegram_thread_id = "99"
        self.assertEqual(self.plugin.recent_records()["items"][0]["display_status"]["state"], "pending")
        self.plugin._telegram_thread_id = ""
        with patch.object(self.plugin, "_telegram_delivery_key", side_effect=lambda *target: f"another-account-{target}"):
            self.assertEqual(self.plugin.recent_records()["items"][0]["display_status"]["state"], "pending")

    def test_legacy_single_target_success_remains_visible_without_resending(self):
        _, _, cache = self.record(normal=False)
        records = self.records()
        row = records[next(iter(records))]
        row["telegram_sent"] = True
        row["telegram_target"] = self.plugin._telegram_delivery_key("@normal", None)
        self.plugin._write_results(None, cache, records)
        with patch.object(self.plugin, "_send_telegram", side_effect=AssertionError("must not resend")):
            self.assertEqual(self.plugin.recent_records()["items"][0]["display_status"]["state"], "success")
        self.plugin._telegram_chat_id = "@different"
        self.assertEqual(self.plugin.recent_records()["items"][0]["display_status"]["state"], "pending")

    def test_partial_target_receipts_show_counts_and_channels_remain_separate(self):
        _, _, cache = self.record(normal=False, ordinary=False)
        self.plugin._telegram_chat_id = "@normal,@second"
        records = self.records()
        records[next(iter(records))]["telegram_deliveries"] = {
            self.plugin._telegram_delivery_key("@normal", None): True,
        }
        self.plugin._write_results(None, cache, records)
        result = self.plugin.recent_records()["items"][0]["display_status"]
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["steps"][2]["text"], "部分投递 1/2")
        self.assertEqual(result["steps"][3]["text"], "待投递")

    def test_invalid_enabled_target_remains_visible_without_hiding_all_records(self):
        self.record()
        self.plugin._telegram_chat_id = ""
        result = self.plugin.recent_records()
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"][0]["display_status"]["state"], "failed")
        self.assertEqual(result["items"][0]["display_status"]["steps"][2]["text"], "目标设置无效")

    def test_missing_or_dirty_delivery_ledger_does_not_claim_success(self):
        self.record()
        self.plugin._telegram_delivery_store.dirty = True
        self.assertEqual(self.plugin.recent_records()["items"][0]["display_status"]["state"], "pending")
        self.plugin._telegram_delivery_store = None
        self.assertEqual(self.plugin.recent_records()["items"][0]["display_status"]["state"], "pending")

    def test_only_current_file_identity_can_supply_a_stopped_retry_reason(self):
        path, row, _ = self.record(normal=False)
        self.plugin._retry_failures[str(path)] = {
            "identity": [row["size"], row["mtime_ns"], row["inode"]],
            "exhausted": True, "error": "Telegram发送失败",
        }
        result = self.plugin.recent_records()["items"][0]["display_status"]
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["detail"], "Telegram发送失败")
        self.plugin._retry_failures[str(path)]["identity"] = [0, 0, 0]
        self.assertEqual(self.plugin.recent_records()["items"][0]["display_status"]["state"], "pending")

    def test_display_does_not_wait_for_network_sending_lock(self):
        self.record()
        ready, release = threading.Event(), threading.Event()
        def hold_send_lock():
            with self.plugin._telegram_delivery_store.lock:
                ready.set()
                release.wait(5)
        worker = threading.Thread(target=hold_send_lock)
        worker.start()
        try:
            self.assertTrue(ready.wait(1))
            self.assertTrue(self.plugin.recent_records()["ok"])
            self.assertTrue(worker.is_alive())
            self.assertFalse(release.is_set())
        finally:
            release.set()
            worker.join(5)


if __name__ == "__main__":
    unittest.main()
