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
        select = next(item for item in form[0]["content"] if item.get("props", {}).get("model") == "error_types")
        self.assertEqual(select["props"]["items"][0]["value"], "质量更好")
        self.assertEqual(defaults["error_types"], [])

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


if __name__ == "__main__":
    unittest.main()
