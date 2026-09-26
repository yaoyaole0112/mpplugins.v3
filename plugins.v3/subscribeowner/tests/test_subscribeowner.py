import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class SubscribeOwnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class SubscribeOper:
            subscriptions = []
            updates = []

            def get(self, subscribe_id):
                return next(
                    (item for item in self.subscriptions if item.id == subscribe_id),
                    None,
                )

            def list(self):
                return self.subscriptions

            def update(self, subscribe_id, payload):
                self.updates.append((subscribe_id, payload))
                return self.get(subscribe_id)

        class UserOper:
            users = []

            def list(self):
                return self.users

        class EventType:
            SubscribeAdded = "subscribe.added"

        class CronTrigger:
            @staticmethod
            def from_crontab(value):
                if value == "invalid":
                    raise ValueError("invalid cron")
                return value

        packages = {}
        for name in (
            "app", "app.api", "app.api.dependencies", "app.db", "app.db.oper",
            "app.schemas", "app.sdk",
            "apscheduler", "apscheduler.triggers",
        ):
            package = types.ModuleType(name)
            package.__path__ = []
            packages[name] = package
        modules = {
            **packages,
            "app.db.oper.subscribe": types.ModuleType("app.db.oper.subscribe"),
            "app.db.oper.user": types.ModuleType("app.db.oper.user"),
            "app.api.dependencies.auth": types.ModuleType("app.api.dependencies.auth"),
            "app.plugins": types.ModuleType("app.plugins"),
            "app.schemas.types": types.ModuleType("app.schemas.types"),
            "app.sdk.events": types.ModuleType("app.sdk.events"),
            "app.sdk.logging": types.ModuleType("app.sdk.logging"),
            "apscheduler.triggers.cron": types.ModuleType("apscheduler.triggers.cron"),
            "fastapi": types.ModuleType("fastapi"),
        }
        modules["app.db.oper.subscribe"].SubscribeOper = SubscribeOper
        modules["app.db.oper.user"].UserOper = UserOper
        modules["app.api.dependencies.auth"].get_current_active_superuser = Mock()
        class PluginBase:
            def __init__(self):
                self.data = {}

            def save_data(self, key, value):
                self.data[key] = value

            def get_data(self, key):
                return self.data.get(key)

        modules["app.plugins"]._PluginBase = PluginBase
        modules["app.schemas.types"].EventType = EventType
        modules["app.sdk.events"].Event = object
        modules["app.sdk.events"].eventmanager = types.SimpleNamespace(
            register=lambda event_type: lambda handler: handler
        )
        modules["app.sdk.logging"].logger = Mock()
        cls.logger = modules["app.sdk.logging"].logger
        modules["apscheduler.triggers.cron"].CronTrigger = CronTrigger
        modules["fastapi"].Depends = lambda dependency: dependency
        plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
        spec = importlib.util.spec_from_file_location("test_subscribeowner_plugin", plugin_path)
        plugin_module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(plugin_module)
        cls.plugin_class = plugin_module.SubscribeOwner
        cls.subscribe_oper = SubscribeOper
        cls.user_oper = UserOper

    def setUp(self):
        self.logger.reset_mock()
        self.subscribe_oper.subscriptions = [
            types.SimpleNamespace(
                id=12, name="测试剧集", type="电视剧", username="猫眼订阅"
            )
        ]
        self.subscribe_oper.updates = []
        self.user_oper.users = [
            types.SimpleNamespace(name="admin", is_active=True)
        ]
        self.plugin = self.plugin_class()
        self.plugin.init_plugin({"enabled": True})

    def send_event(self, subscribe_id=12):
        self.plugin.on_subscribe_added(
            types.SimpleNamespace(event_data={"subscribe_id": subscribe_id})
        )

    def test_reassigns_new_subscription(self):
        self.send_event()
        self.assertEqual(self.subscribe_oper.updates, [(12, {"username": "admin"})])

    def test_skips_already_owned_subscription(self):
        self.subscribe_oper.subscriptions[0].username = "admin"
        self.send_event()
        self.assertEqual(self.subscribe_oper.updates, [])
        self.logger.info.assert_any_call(
            "订阅用户修正：新增订阅 ID %s 已归属 %s，无需修改",
            12, "admin",
        )

    def test_periodic_sweep_updates_all_types_and_real_users(self):
        self.subscribe_oper.subscriptions.extend([
            types.SimpleNamespace(id=13, name="测试电影", type="电影", username="viewer"),
            types.SimpleNamespace(id=14, name="测试音乐", type="音乐", username="admin"),
        ])
        result = self.plugin.sync_subscriptions()
        self.assertEqual(result["data"], {"total": 3, "updated": 2, "failed": 0})
        self.assertEqual(self.subscribe_oper.updates, [
            (12, {"username": "admin"}),
            (13, {"username": "admin"}),
        ])

    def test_schedules_cron_when_enabled(self):
        service = self.plugin.get_service()
        self.assertEqual(service[0]["trigger"], "*/30 * * * *")
        self.assertEqual(service[0]["func"], self.plugin.sync_subscriptions)
        self.plugin.init_plugin({"enabled": True, "cron": "invalid"})
        self.assertEqual(self.plugin.get_service(), [])

    def test_requires_explicit_target_with_multiple_active_users(self):
        self.user_oper.users.append(
            types.SimpleNamespace(name="viewer", is_active=True)
        )
        self.send_event()
        self.assertEqual(self.subscribe_oper.updates, [])
        self.plugin.init_plugin({"enabled": True, "target_username": "viewer"})
        self.send_event()
        self.assertEqual(self.subscribe_oper.updates, [(12, {"username": "viewer"})])

    def test_reassigns_new_subscription_from_another_real_user(self):
        self.user_oper.users.append(
            types.SimpleNamespace(name="viewer", is_active=True)
        )
        self.plugin.init_plugin({"enabled": True, "target_username": "admin"})
        self.subscribe_oper.subscriptions[0].username = "viewer"
        self.send_event()
        self.assertEqual(self.subscribe_oper.updates, [(12, {"username": "admin"})])

    def test_disabled_or_invalid_event_does_not_write(self):
        self.plugin.init_plugin({"enabled": False})
        self.send_event()
        self.plugin.sync_subscriptions()
        self.assertEqual(self.plugin.get_service(), [])
        self.plugin.init_plugin({"enabled": True})
        self.send_event("not-an-id")
        self.assertEqual(self.subscribe_oper.updates, [])

    def test_run_button_works_while_periodic_job_disabled(self):
        self.plugin.init_plugin({"enabled": False, "target_username": "admin"})
        result = self.plugin.run_now()
        self.assertTrue(result["success"])
        self.assertEqual(result["data"], {"total": 1, "updated": 1, "failed": 0})
        self.assertIn("上次手动执行", str(self.plugin.get_page()))
        self.assertEqual(self.plugin.get_service(), [])
        api = self.plugin.get_api()[0]
        self.assertEqual((api["path"], api["methods"], api["auth"]), (
            "/run", ["POST"], "bear"
        ))
        self.assertTrue(api["dependencies"])

    def test_form_links_to_page_and_page_has_run_button(self):
        fields = self.plugin.get_form()[0][0]["content"]
        self.assertEqual(fields[1]["props"]["class"], "mb-6")
        self.assertEqual(fields[2]["props"]["class"], "mt-2 mb-4")
        self.assertIn("查看数据", fields[3]["props"]["text"])
        button = self.plugin.get_page()[0]["content"][0]["content"][0]
        self.assertEqual(button["text"], "立即运行")
        self.assertEqual(button["events"]["click"], {
            "api": "plugin/SubscribeOwner/run", "method": "post"
        })


if __name__ == "__main__":
    unittest.main()
