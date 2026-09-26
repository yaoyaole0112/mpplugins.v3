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
            subscription = None
            updates = []

            def get(self, subscribe_id):
                return self.subscription

            def update(self, subscribe_id, payload):
                self.updates.append((subscribe_id, payload))

        class UserOper:
            users = []

            def list(self):
                return self.users

        class EventType:
            SubscribeAdded = "subscribe.added"

        packages = {}
        for name in ("app", "app.db", "app.db.oper", "app.schemas", "app.sdk"):
            package = types.ModuleType(name)
            package.__path__ = []
            packages[name] = package
        modules = {
            **packages,
            "app.db.oper.subscribe": types.ModuleType("app.db.oper.subscribe"),
            "app.db.oper.user": types.ModuleType("app.db.oper.user"),
            "app.plugins": types.ModuleType("app.plugins"),
            "app.schemas.types": types.ModuleType("app.schemas.types"),
            "app.sdk.events": types.ModuleType("app.sdk.events"),
            "app.sdk.logging": types.ModuleType("app.sdk.logging"),
        }
        modules["app.db.oper.subscribe"].SubscribeOper = SubscribeOper
        modules["app.db.oper.user"].UserOper = UserOper
        modules["app.plugins"]._PluginBase = type("PluginBase", (), {})
        modules["app.schemas.types"].EventType = EventType
        modules["app.sdk.events"].Event = object
        modules["app.sdk.events"].eventmanager = types.SimpleNamespace(
            register=lambda event_type: lambda handler: handler
        )
        modules["app.sdk.logging"].logger = Mock()
        plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
        spec = importlib.util.spec_from_file_location("test_subscribeowner_plugin", plugin_path)
        plugin_module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(plugin_module)
        cls.plugin_class = plugin_module.SubscribeOwner
        cls.subscribe_oper = SubscribeOper
        cls.user_oper = UserOper

    def setUp(self):
        self.subscribe_oper.subscription = types.SimpleNamespace(
            id=12, name="测试剧集", type="电视剧", username="猫眼订阅"
        )
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

    def test_reassigns_plugin_owned_tv_subscription(self):
        self.send_event()
        self.assertEqual(self.subscribe_oper.updates, [(12, {"username": "admin"})])

    def test_preserves_real_user_and_non_tv_subscriptions(self):
        self.subscribe_oper.subscription.username = "admin"
        self.send_event()
        self.subscribe_oper.subscription.username = "猫眼订阅"
        self.subscribe_oper.subscription.type = "电影"
        self.send_event()
        self.assertEqual(self.subscribe_oper.updates, [])

    def test_requires_explicit_target_with_multiple_active_users(self):
        self.user_oper.users.append(
            types.SimpleNamespace(name="viewer", is_active=True)
        )
        self.send_event()
        self.assertEqual(self.subscribe_oper.updates, [])
        self.plugin.init_plugin({"enabled": True, "target_username": "viewer"})
        self.send_event()
        self.assertEqual(self.subscribe_oper.updates, [(12, {"username": "viewer"})])

    def test_does_not_override_another_real_user(self):
        self.user_oper.users.append(
            types.SimpleNamespace(name="viewer", is_active=True)
        )
        self.plugin.init_plugin({"enabled": True, "target_username": "admin"})
        self.subscribe_oper.subscription.username = "viewer"
        self.send_event()
        self.assertEqual(self.subscribe_oper.updates, [])

    def test_disabled_or_invalid_event_does_not_write(self):
        self.plugin.init_plugin({"enabled": False})
        self.send_event()
        self.plugin.init_plugin({"enabled": True})
        self.send_event("not-an-id")
        self.assertEqual(self.subscribe_oper.updates, [])


if __name__ == "__main__":
    unittest.main()
