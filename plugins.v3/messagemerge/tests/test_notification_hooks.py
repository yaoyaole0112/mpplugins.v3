import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class NotificationHooksTests(unittest.TestCase):
    def setUp(self):
        class NotificationMixin:
            def post_message(self, **kwargs):
                return ("original", kwargs["message"])

            async def async_post_message(self, **kwargs):
                return ("async_original", kwargs["message"])

        self.mixin = NotificationMixin
        self.original_post = NotificationMixin.post_message
        self.original_async_post = NotificationMixin.async_post_message
        chain = types.ModuleType("app.chain")
        chain._messaging = types.SimpleNamespace(NotificationMixin=NotificationMixin)
        log = types.ModuleType("app.log")
        log.logger = Mock()
        self.modules = {"app.chain": chain, "app.log": log}
        path = Path(__file__).resolve().parents[1] / "notification_hooks.py"
        spec = importlib.util.spec_from_file_location("test_bundled_notification_hooks", path)
        self.hooks = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, self.modules):
            spec.loader.exec_module(self.hooks)

    def test_sync_priority_and_short_circuit(self):
        later = Mock(return_value=False)
        first = Mock(return_value=True)
        self.hooks.register("later", later, priority=100)
        self.hooks.register("first", first, priority=50)
        self.assertIsNone(self.mixin().post_message(message="message"))
        first.assert_called_once()
        later.assert_not_called()

    def test_original_delivery_and_unregister_restore(self):
        self.hooks.register("merge", Mock(return_value=True))
        self.assertEqual(self.hooks.deliver_original(self.mixin(), "message"), ("original", "message"))
        self.hooks.unregister("merge")
        self.assertIs(self.mixin.post_message, self.original_post)
        self.assertIs(self.mixin.async_post_message, self.original_async_post)

    def test_re_register_replaces_handler_without_double_wrapping(self):
        old = Mock(return_value=True)
        new = Mock(return_value=False)
        self.hooks.register("merge", old)
        self.hooks.register("merge", new)
        self.assertEqual(self.mixin().post_message(message="message"), ("original", "message"))
        old.assert_not_called()
        new.assert_called_once()

    def test_async_interception_and_fallthrough(self):
        async def intercept(*args):
            return True

        self.hooks.register("merge", intercept)
        self.assertIsNone(asyncio.run(self.mixin().async_post_message(message="message")))
        self.hooks.register("merge", lambda *args: False)
        self.assertEqual(asyncio.run(self.mixin().async_post_message(message="message")),
                         ("async_original", "message"))

    def test_plugin_loads_without_external_hookchain(self):
        plugins = types.ModuleType("app.plugins")
        plugins.__path__ = []
        plugins._PluginBase = type("PluginBase", (), {})
        message = types.ModuleType("app.schemas.message")
        message.Message = type("Message", (), {})
        types_module = types.ModuleType("app.schemas.types")
        types_module.MessageType = ()
        template = types.ModuleType("app.application.messaging.message")
        template.MessageTemplateHelper = Mock()
        path = Path(__file__).resolve().parents[1] / "__init__.py"
        spec = importlib.util.spec_from_file_location("test_standalone_messagemerge", path)
        plugin = importlib.util.module_from_spec(spec)
        modules = {**self.modules, "app.plugins": plugins, "app.schemas.message": message,
                   "app.schemas.types": types_module, "app.application.messaging.message": template,
                   spec.name: plugin}
        with patch.dict(sys.modules, modules):
            sys.modules.pop("app.plugins._hookchain", None)
            spec.loader.exec_module(plugin)
            self.assertTrue(plugin._hookchain.__file__.endswith("messagemerge/notification_hooks.py"))
            self.assertIs(sys.modules["app.plugins._hookchain"], plugin._hookchain)
            self.assertEqual(plugin.MessageMerge.plugin_version, "1.5.3")


if __name__ == "__main__":
    unittest.main()
