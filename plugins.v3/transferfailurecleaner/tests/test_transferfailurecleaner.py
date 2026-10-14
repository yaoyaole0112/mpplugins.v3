import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch


class TransferFailureCleanerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class PluginBase:
            def __init__(self):
                self.data = {}

            def save_data(self, key, value):
                self.data[key] = value

            def get_data(self, key):
                return self.data.get(key)

        class CronTrigger:
            @staticmethod
            def from_crontab(value, timezone=None):
                if value == "invalid":
                    raise ValueError("invalid cron")
                return (value, timezone)

        exports = {
            "app.plugins": {"_PluginBase": PluginBase},
            "app.api.dependencies.auth": {"get_current_active_superuser": Mock()},
            "app.api.dependencies.history": {"get_transfer_history_mutation_command": Mock()},
            "app.db.models.transferhistory": {"TransferHistory": types.SimpleNamespace(id=1)},
            "app.runtime.errors": {"public_error_message": lambda value, **kwargs: str(value).strip()},
            "app.sdk.config": {"settings": types.SimpleNamespace(TZ="Asia/Shanghai")},
            "app.sdk.logging": {"logger": Mock()},
            "app.sdk.queries": {"list_transfer_history": Mock()},
            "apscheduler.triggers.cron": {"CronTrigger": CronTrigger},
            "fastapi": {"Depends": lambda value: value},
            "sqlalchemy": {"select": Mock()},
        }
        modules = {}
        for name, attributes in exports.items():
            module = types.ModuleType(name)
            module.__dict__.update(attributes)
            modules[name] = module
        path = Path(__file__).resolve().parents[1] / "__init__.py"
        spec = importlib.util.spec_from_file_location("test_transferfailurecleaner_plugin", path)
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(cls.module)

    def setUp(self):
        self.plugin = self.module.TransferFailureCleaner()
        self.plugin.init_plugin({"enabled": True, "error_types": ["质量更好"]})
        self.module.list_transfer_history.reset_mock()
        self.module.get_transfer_history_mutation_command.reset_mock()

    @staticmethod
    def record(record_id=1, error="质量更好", status=False, date=None, task_id=None):
        return types.SimpleNamespace(id=record_id, errmsg=error, status=status,
                                     date=date, transfer_task_id=task_id)

    @classmethod
    def form_components(cls, components):
        for component in components:
            yield component
            yield from cls.form_components(component.get("content", []))

    def test_empty_selection_never_deletes(self):
        self.plugin.init_plugin({"enabled": True})
        with patch.object(self.plugin, "_delete_record") as delete:
            self.assertFalse(self.plugin.run_now()["success"])
            delete.assert_not_called()
        self.module.list_transfer_history.assert_not_called()

    def test_preview_only_matches_exact_failed_errors(self):
        records = [self.record(), self.record(2, status=True),
                   self.record(3, error="质量更好：不同提示"), self.record(4, error=None)]
        with patch.object(self.plugin, "_failed_records", return_value=records), \
                patch.object(self.plugin, "_delete_record") as delete:
            result = self.plugin.preview()
        self.assertEqual(result["data"]["matched"], 1)
        self.assertEqual(result["data"]["deleted"], 0)
        delete.assert_not_called()

    def test_retention_skips_recent_missing_and_invalid_dates(self):
        cutoff = datetime.now(timezone.utc)
        self.assertTrue(self.plugin._matches(self.record(date=(cutoff - timedelta(hours=1)).isoformat()),
                                            ("质量更好",), cutoff))
        for date in (None, "bad", (cutoff + timedelta(hours=1)).isoformat()):
            self.assertFalse(self.plugin._matches(self.record(date=date), ("质量更好",), cutoff))

    def test_null_error_requires_explicit_selection(self):
        record = self.record(error=None)
        self.assertFalse(self.plugin._matches(record, ("质量更好",), None))
        self.assertTrue(self.plugin._matches(record, (self.plugin._EMPTY_ERROR,), None))

    def test_run_reports_host_rejections_and_continues_after_errors(self):
        records = [self.record(record_id=record_id) for record_id in range(1, 5)]
        with patch.object(self.plugin, "_failed_records", return_value=records), \
                patch.object(self.plugin, "_delete_record", side_effect=[
                    "deleted", "retained", RuntimeError("db error"), "skipped"
                ]):
            result = self.plugin.run_now()
        self.assertFalse(result["success"])
        for key in ("deleted", "retained", "failed", "skipped"):
            self.assertEqual(result["data"][key], 1)
        self.assertFalse(self.plugin._run_lock.locked())

    def test_disabled_schedule_does_not_query(self):
        self.plugin.init_plugin({"error_types": ["质量更好"]})
        self.assertFalse(self.plugin.scheduled_cleanup()["success"])
        self.module.list_transfer_history.assert_not_called()

    def test_pagination_reads_all_pages_before_deletion(self):
        self.module.list_transfer_history.side_effect = [
            types.SimpleNamespace(items=[self.record()], total=201),
            types.SimpleNamespace(items=[self.record(2)], total=201),
        ]
        self.assertEqual(len(self.plugin._failed_records()), 2)
        self.assertEqual(self.module.list_transfer_history.call_args.args[1]["page"], 2)
        self.module.list_transfer_history.side_effect = None

    def test_busy_run_does_not_release_other_run_lock(self):
        self.plugin._run_lock.acquire()
        self.assertFalse(self.plugin.run_now()["success"])
        self.assertTrue(self.plugin._run_lock.locked())
        self.plugin._run_lock.release()

    def test_scan_failure_releases_lock(self):
        with patch.object(self.plugin, "_failed_records", side_effect=RuntimeError("unavailable")):
            self.assertFalse(self.plugin.run_now()["success"])
        self.assertFalse(self.plugin._run_lock.locked())

    def test_services_validate_cron_and_timezone(self):
        self.assertEqual(self.plugin.get_service()[0]["trigger"], ("0 3 * * *", "Asia/Shanghai"))
        self.plugin.init_plugin({"enabled": True, "error_types": ["质量更好"], "cron": "invalid"})
        self.assertEqual(self.plugin.get_service(), [])

    def test_api_endpoints_require_superuser(self):
        for api in self.plugin.get_api():
            self.assertEqual(api["methods"], ["POST"])
            self.assertEqual(api["dependencies"], [self.module.get_current_active_superuser])

    def test_form_keeps_saved_types_without_current_records(self):
        with patch.object(self.plugin, "_failed_records", return_value=[]):
            form, defaults = self.plugin.get_form()
        select = next(item for item in self.form_components(form) if item.get("props", {}).get("model") == "errors_record")
        self.assertEqual(select["props"]["items"][0]["value"], "质量更好")
        self.assertEqual(defaults["errors_record"], ["质量更好"])

    def test_delete_uses_host_command_without_locking_durable_receipt_or_deleting_files(self):
        session = Mock()
        session.execute.return_value.scalar_one_or_none.return_value = self.record(task_id="task")
        sessions = Mock()
        sessions.__next__ = Mock(return_value=session)
        runtime = types.SimpleNamespace(persistence=types.SimpleNamespace(sync_session=lambda: sessions))
        factory = types.ModuleType("app.factory")
        factory.app = types.SimpleNamespace(state=types.SimpleNamespace(host_runtime=runtime))
        command = self.module.get_transfer_history_mutation_command.return_value
        command.delete.return_value = types.SimpleNamespace(history="deleted")
        self.module.select.reset_mock()
        with patch.dict(sys.modules, {"app.factory": factory}):
            self.assertEqual(self.plugin._delete_record(1, ("质量更好",), None, "record"), "deleted")
        self.module.select.return_value.where.return_value.with_for_update.assert_not_called()
        command.delete.assert_called_with(1, delete_source=False, delete_destination=False)
        sessions.close.assert_called_once()

    def test_changed_success_record_is_not_deleted(self):
        session = Mock()
        session.execute.return_value.scalar_one_or_none.return_value = self.record(status=True, task_id="task")
        sessions = Mock()
        sessions.__next__ = Mock(return_value=session)
        runtime = types.SimpleNamespace(persistence=types.SimpleNamespace(sync_session=lambda: sessions))
        factory = types.ModuleType("app.factory")
        factory.app = types.SimpleNamespace(state=types.SimpleNamespace(host_runtime=runtime))
        with patch.dict(sys.modules, {"app.factory": factory}):
            self.assertEqual(self.plugin._delete_record(1, ("质量更好",), None, "record"), "skipped")
        self.module.get_transfer_history_mutation_command.assert_not_called()
        sessions.close.assert_called_once()

    def test_four_modes_pass_exact_source_and_destination_flags(self):
        session = Mock()
        session.execute.return_value.scalar_one_or_none.return_value = self.record(task_id="task")
        runtime = types.SimpleNamespace(persistence=types.SimpleNamespace(
            sync_session=lambda: iter([session])
        ))
        def session_scope():
            yield session
        runtime.persistence.sync_session = session_scope
        factory = types.ModuleType("app.factory")
        factory.app = types.SimpleNamespace(state=types.SimpleNamespace(host_runtime=runtime))
        command = self.module.get_transfer_history_mutation_command.return_value
        command.delete.return_value = types.SimpleNamespace(history="deleted")
        for mode, source, destination in (("record", False, False), ("source", True, False),
                                           ("destination", False, True), ("all", True, True)):
            with self.subTest(mode=mode), patch.dict(sys.modules, {"app.factory": factory}):
                self.assertEqual(self.plugin._delete_record(1, ("质量更好",), None, mode), "deleted")
                command.delete.assert_called_with(1, delete_source=source, delete_destination=destination)

    def test_unknown_delete_mode_falls_back_to_record_only(self):
        self.plugin.init_plugin({"delete_mode": "invalid"})
        self.assertEqual(self.plugin._delete_mode, "record")

    def test_preview_exposes_destructive_mode_without_deletion(self):
        self.plugin.init_plugin({"error_types": ["质量更好"], "delete_mode": "all"})
        with patch.object(self.plugin, "_failed_records", return_value=[self.record()]), \
                patch.object(self.plugin, "_delete_record") as delete:
            result = self.plugin.preview()
        self.assertEqual(result["data"]["delete_mode"], "all")
        self.assertIn("源文件和媒体库文件", result["message"])
        delete.assert_not_called()

    def test_run_uses_saved_delete_mode(self):
        self.plugin.init_plugin({"error_types": ["质量更好"], "delete_mode": "source"})
        with patch.object(self.plugin, "_failed_records", return_value=[self.record()]), \
                patch.object(self.plugin, "_delete_record", return_value="deleted") as delete:
            self.assertTrue(self.plugin.run_now()["success"])
        delete.assert_called_once_with(1, ("质量更好",), None, "source")

    def test_form_groups_fields_with_spacing_and_preserves_config_models(self):
        with patch.object(self.plugin, "_failed_records", return_value=[]):
            form, defaults = self.plugin.get_form()
        components = list(self.form_components(form))
        fields = {item["props"]["model"]: item for item in components if "model" in item.get("props", {})}
        self.assertEqual(set(fields), set(defaults))
        self.assertEqual(sum(item["component"] == "VCard" for item in components), 5)
        for mode in self.plugin._DELETE_MODES:
            self.assertTrue(fields[f"errors_{mode}"]["props"]["multiple"])
            self.assertEqual(fields[f"errors_{mode}"]["props"]["density"], "comfortable")
        self.assertNotIn("delete_mode", fields)
        self.assertNotIn("error_types", fields)

    def test_form_displays_read_failure_separately(self):
        with patch.object(self.plugin, "_failed_records", side_effect=RuntimeError("unavailable")):
            form, _defaults = self.plugin.get_form()
        components = list(self.form_components(form))
        self.assertTrue(any(item["component"] == "VAlert"
                            and "暂时无法读取" in item["props"].get("text", "") for item in components))

    def test_target_state_errors_group_across_paths_and_rpc_details(self):
        core = self.plugin._TARGET_STATE_ERROR
        examples = [
            f"失败阶段：目标存储访问 {core}：/115open/喜剧之王/Season 1/S01E01.mkv - "
            "【CloudDrive】查询文件信息失败: <_InactiveRpcError status = StatusCode.DEADLINE_EXCEEDED> "
            "下一步：检查目标存储连接 已尝试 1/3 次",
            f"{core}：/115open/另一部剧/S02E08.mkv - Permission denied",
            f"失败阶段:目标存储访问\n{core}: /media/movie.mkv - timeout",
            core,
        ]
        for message in examples:
            with self.subTest(message=message):
                self.assertEqual(self.plugin._error_type(message), self.plugin._TARGET_STATE_TYPE)

    def test_target_group_does_not_merge_different_failures(self):
        for message in ("失败阶段：目标存储访问 目标存储空间不足", "查询文件信息失败：/media/test.mkv",
                        "源文件异常：" + self.plugin._TARGET_STATE_ERROR,
                        self.plugin._TARGET_STATE_ERROR + "之外的错误"):
            with self.subTest(message=message):
                self.assertEqual(self.plugin._error_type(message), message)

    def test_legacy_selection_normalizes_and_matches_another_episode(self):
        core = self.plugin._TARGET_STATE_ERROR
        self.plugin.init_plugin({"error_types": [f"{core}：/show/S01E01.mkv", f"{core}：/show/S01E02.mkv"]})
        self.assertEqual(self.plugin._error_types, (self.plugin._TARGET_STATE_TYPE,))
        self.assertTrue(self.plugin._matches(self.record(error=f"{core}：/other/S03E12.mkv"),
                                             self.plugin._error_types, None))

    def test_grouped_form_counts_and_preview_use_the_same_category(self):
        core = self.plugin._TARGET_STATE_ERROR
        records = [self.record(1, error=f"{core}：/show/S01E01.mkv"),
                   self.record(2, error=f"{core}：/other/S02E02.mkv")]
        self.plugin.init_plugin({"error_types": [self.plugin._TARGET_STATE_TYPE]})
        with patch.object(self.plugin, "_failed_records", return_value=records), \
                patch.object(self.plugin, "_delete_record") as delete:
            form, _defaults = self.plugin.get_form()
            result = self.plugin.preview()
        field = next(item for item in self.form_components(form)
                     if item.get("props", {}).get("model") == "errors_record")
        self.assertEqual(field["props"]["items"], [
            {"title": f"{self.plugin._TARGET_STATE_TYPE}（2 条）", "value": self.plugin._TARGET_STATE_TYPE}
        ])
        self.assertEqual(result["data"]["types"], {self.plugin._TARGET_STATE_TYPE: 2})
        self.assertEqual(result["data"]["matched"], 2)
        delete.assert_not_called()

    def test_raw_target_reason_survives_public_message_sanitizing(self):
        with patch.object(self.module, "public_error_message", return_value="存储请求超时"):
            self.assertEqual(self.plugin._error_type(self.plugin._TARGET_STATE_ERROR + "：/show/E01.mkv"),
                             self.plugin._TARGET_STATE_TYPE)

    def test_multiple_rules_dispatch_each_type_to_its_own_delete_mode(self):
        self.plugin.init_plugin({"errors_record": ["存储访问失败"], "errors_source": ["质量更好"],
                                 "errors_destination": ["目标错误"], "errors_all": ["全部错误"]})
        records = [self.record(1, error="存储访问失败"), self.record(2),
                   self.record(3, error="目标错误"), self.record(4, error="全部错误"),
                   self.record(5, error="未选择"), self.record(6, status=True)]
        with patch.object(self.plugin, "_failed_records", return_value=records), \
                patch.object(self.plugin, "_delete_record", return_value="deleted") as delete:
            result = self.plugin.run_now()
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["matched"], 4)
        self.assertEqual(result["data"]["deleted"], 4)
        for record_id, mode, error_type in ((1, "record", "存储访问失败"), (2, "source", "质量更好"),
                                          (3, "destination", "目标错误"), (4, "all", "全部错误")):
            delete.assert_any_call(record_id, (error_type,), None, mode)
            self.assertEqual(result["data"]["rules"][mode]["deleted"], 1)
        self.assertEqual(delete.call_count, 4)

    def test_multi_rule_preview_reports_modes_without_deleting(self):
        self.plugin.init_plugin({"errors_record": ["存储访问失败"], "errors_source": ["质量更好"]})
        records = [self.record(1, error="存储访问失败"), self.record(2), self.record(3)]
        with patch.object(self.plugin, "_failed_records", return_value=records), \
                patch.object(self.plugin, "_delete_record") as delete:
            result = self.plugin.preview()
        self.assertEqual(result["data"]["delete_mode"], "multiple")
        self.assertEqual(result["data"]["rules"]["record"]["matched"], 1)
        self.assertEqual(result["data"]["rules"]["source"]["matched"], 2)
        self.assertEqual(result["data"]["rules"]["source"]["deleted"], 0)
        delete.assert_not_called()

    def test_conflicting_rules_block_manual_preview_and_schedule(self):
        self.plugin.init_plugin({"enabled": True, "errors_record": ["质量更好"], "errors_source": ["质量更好"]})
        with patch.object(self.plugin, "_delete_record") as delete:
            for run in (self.plugin.preview, self.plugin.run_now, self.plugin.scheduled_cleanup):
                result = run()
                self.assertFalse(result["success"])
                self.assertIn("不能分配到多个", result["message"])
            self.assertEqual(self.plugin.get_service(), [])
            delete.assert_not_called()
        self.module.list_transfer_history.assert_not_called()

    def test_normalized_target_errors_conflict_across_rules(self):
        core = self.plugin._TARGET_STATE_ERROR
        self.plugin.init_plugin({"errors_record": [core + "：/show/E01.mkv"],
                                 "errors_all": [core + "：/other/E02.mkv"]})
        self.assertEqual(self.plugin._rule_conflicts(self.plugin._rules), [self.plugin._TARGET_STATE_TYPE])

    def test_old_configuration_maps_into_same_mode_without_changing_schedule(self):
        for mode in self.plugin._DELETE_MODES:
            with self.subTest(mode=mode):
                self.plugin.init_plugin({"enabled": True, "delete_mode": mode, "error_types": ["质量更好"],
                                         "cron": "20 */2 * * *", "retain_hours": 6})
                self.assertEqual(self.plugin._rules[mode], ("质量更好",))
                self.assertEqual(sum(bool(selected) for selected in self.plugin._rules.values()), 1)
                self.assertEqual(self.plugin._cron, "20 */2 * * *")
                self.assertEqual(self.plugin._retain_hours, 6)

    def test_cleared_rules_never_resurrect_legacy_configuration(self):
        self.plugin.init_plugin({"error_types": ["质量更好"], "delete_mode": "all", "errors_record": []})
        self.assertEqual(self.plugin._error_types, ())
        self.assertFalse(self.plugin.run_now()["success"])

    def test_form_migrates_legacy_selection_to_matching_rule_defaults(self):
        self.plugin.init_plugin({"delete_mode": "source", "error_types": ["质量更好"]})
        with patch.object(self.plugin, "_failed_records", return_value=[]):
            _form, defaults = self.plugin.get_form()
        self.assertEqual(defaults["errors_source"], ["质量更好"])
        for mode in ("record", "destination", "all"):
            self.assertEqual(defaults[f"errors_{mode}"], [])

    def test_changed_error_is_rechecked_only_against_its_original_rule(self):
        session = Mock()
        session.execute.return_value.scalar_one_or_none.return_value = self.record(error="存储访问失败", task_id="task")
        def session_scope():
            yield session
        runtime = types.SimpleNamespace(persistence=types.SimpleNamespace(sync_session=session_scope))
        factory = types.ModuleType("app.factory")
        factory.app = types.SimpleNamespace(state=types.SimpleNamespace(host_runtime=runtime))
        with patch.dict(sys.modules, {"app.factory": factory}):
            self.assertEqual(self.plugin._delete_record(1, ("质量更好",), None, "source"), "skipped")
        self.module.get_transfer_history_mutation_command.assert_not_called()

    def test_rule_outcomes_are_counted_separately(self):
        self.plugin.init_plugin({"errors_record": ["存储访问失败"], "errors_source": ["质量更好"]})
        records = [self.record(1, error="存储访问失败"), self.record(2)]
        with patch.object(self.plugin, "_failed_records", return_value=records), \
                patch.object(self.plugin, "_delete_record", side_effect=["deleted", "retained"]):
            result = self.plugin.run_now()
        self.assertEqual(result["data"]["rules"]["record"]["deleted"], 1)
        self.assertEqual(result["data"]["rules"]["source"]["retained"], 1)
        self.assertFalse(result["success"])

    def test_multi_rule_results_display_separate_actions_and_counts(self):
        self.plugin.init_plugin({"errors_record": ["存储访问失败"], "errors_source": ["质量更好"]})
        records = [self.record(1, error="存储访问失败"), self.record(2)]
        with patch.object(self.plugin, "_failed_records", return_value=records):
            self.plugin.preview()
        texts = [item.get("props", {}).get("text", "") for item in self.form_components(self.plugin.get_page())]
        self.assertTrue(any("仅删除转移记录：匹配 1 条" in text for text in texts))
        self.assertTrue(any("删除转移记录和源文件：匹配 1 条" in text for text in texts))

    def test_global_retention_applies_to_every_rule(self):
        now = datetime.now(timezone.utc)
        self.plugin.init_plugin({"errors_record": ["存储访问失败"], "errors_source": ["质量更好"],
                                 "retain_hours": 6})
        records = [self.record(1, error="存储访问失败", date=(now - timedelta(hours=12)).isoformat()),
                   self.record(2, date=(now - timedelta(hours=12)).isoformat()),
                   self.record(3, error="存储访问失败", date=now.isoformat()),
                   self.record(4, date=None)]
        with patch.object(self.plugin, "_failed_records", return_value=records):
            result = self.plugin.preview()
        self.assertEqual(result["data"]["matched"], 2)
        self.assertEqual(result["data"]["rules"]["record"]["matched"], 1)
        self.assertEqual(result["data"]["rules"]["source"]["matched"], 1)


if __name__ == "__main__":
    unittest.main()
