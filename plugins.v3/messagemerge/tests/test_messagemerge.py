import importlib.util
import sys
import types
import unittest
import copy
from enum import Enum
from pathlib import Path
from unittest.mock import Mock, patch


class MessageMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Message:
            def __init__(self, **kwargs):
                for field in ("title", "text", "mtype", "channel", "source", "username", "targets", "action",
                              "ctype", "image", "link", "date", "notification_route_scope"):
                    setattr(self, field, None)
                self.__dict__.update(kwargs)

            def model_copy(self, deep=False, update=None):
                result = copy.deepcopy(self) if deep else copy.copy(self)
                result.__dict__.update(update or {})
                return result

            def model_dump(self):
                return vars(self).copy()

        class MessageType(Enum):
            Organize = "organize"

        exports = {
            "app.log": {"logger": Mock()},
            "app.plugins": {"_PluginBase": type("PluginBase", (), {}), "_hookchain": Mock()},
            "app.schemas.message": {"Message": Message},
            "app.schemas.types": {"MessageType": MessageType},
            "app.application.messaging.message": {
                "MessageTemplateHelper": types.SimpleNamespace(render=lambda **kwargs: kwargs["message"])
            },
        }
        modules = {}
        for name, attributes in exports.items():
            module = types.ModuleType(name)
            module.__dict__.update(attributes)
            modules[name] = module
        path = Path(__file__).resolve().parents[1] / "__init__.py"
        spec = importlib.util.spec_from_file_location("test_messagemerge_plugin", path)
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(cls.module)
        cls.Message = Message
        cls.MessageType = MessageType

    def make_message(self, episode, size, quality="WEB-DL HDR 2160p"):
        return self.Message(
            title=f"醉梦 (2026) S01E{episode:02d}",
            text=(
                f"✅ 已入库 · S01 E{episode:02d}\n━━━━━━━━━━━━━━━━━━\n"
                f"🗂 类型：国产剧\n📑 质量：{quality}\n💾 大小：{size}\n"
                "👤 主演：刘诗诗、 胡先煦、 李乃文、 李沁、 黄小蕾"
            ),
            mtype=self.MessageType.Organize,
            save_history=True,
        )

    def test_episode_groups_ignore_episode_number_but_keep_season(self):
        merger = self.module._Merger()
        first = self.make_message(1, "4.92G")
        second = self.make_message(2, "4.98G")
        other_season = self.Message(title="醉梦 (2026) S02E01", mtype=self.MessageType.Organize)
        self.assertEqual(merger._group_key(first), merger._group_key(second))
        self.assertNotEqual(merger._group_key(first), merger._group_key(other_season))

    def test_episode_summary_compresses_ranges_and_sums_size(self):
        messages = [
            self.make_message(2, "4.98G"),
            self.make_message(1, "4.92G"),
            self.make_message(3, "4.87G"),
            self.make_message(5, "4.82G"),
            self.make_message(6, "4.75G"),
        ]
        merged = self.module._Merger._compose(messages)
        self.assertEqual(merged.title, "🎬 醉梦 (2026)")
        self.assertIn("✅ 已入库 · S01 E01-03,E05-06", merged.text)
        self.assertIn("🗂 类型：国产剧", merged.text)
        self.assertIn("📑 质量：WEB-DL HDR 2160p", merged.text)
        self.assertIn("💾 大小：24.34G", merged.text)
        self.assertIn("👤 主演：刘诗诗、胡先煦、李乃文、李沁、黄小蕾", merged.text)
        self.assertFalse(merged.save_history)

    def test_non_episode_notifications_keep_generic_merge(self):
        messages = [
            self.Message(title="同标题", text="正文甲", mtype=self.MessageType.Organize),
            self.Message(title="同标题", text="正文乙", mtype=self.MessageType.Organize),
        ]
        merged = self.module._Merger._compose(messages)
        self.assertEqual(merged.title, "同标题（2条）")
        self.assertIn("1. 正文甲", merged.text)
        self.assertIn("2. 正文乙", merged.text)

    def test_rendered_screenshot_format_is_summarized(self):
        messages = [self.make_message(episode, "4G") for episode in (1, 2, 3, 5, 6)]
        for message in messages:
            message.title = "🎬 醉梦 (2026)"
        merged = self.module._Merger._compose(messages)
        self.assertEqual(merged.title, "🎬 醉梦 (2026)")
        self.assertIn("S01 E01-03,E05-06", merged.text)
        self.assertIn("💾 大小：20G", merged.text)
        self.assertNotIn("共 5 条", merged.text)

    def test_rendered_seasons_and_routes_are_isolated(self):
        first = self.make_message(1, "4G")
        first.title = "🎬 醉梦 (2026)"
        key = self.module._Merger._group_key(first)
        variants = [
            {"text": first.text.replace("S01", "S02")}, {"title": "🎬 另一剧 (2026)"},
            {"title": "🎬 醉梦 (2025)"}, {"targets": {"telegram": "other-user"}},
            {"channel": "telegram"}, {"source": "other-source"}, {"notification_route_scope": "admin"},
        ]
        for update in variants:
            with self.subTest(update=update):
                self.assertNotEqual(key, self.module._Merger._group_key(first.model_copy(update=update)))

    def test_mixed_units_and_quality_differences_are_preserved(self):
        messages = [self.make_message(1, "1024MiB"), self.make_message(2, "1GB", "WEB-DL 1080p")]
        merged = self.module._Merger._compose(messages)
        self.assertIn("💾 大小：2G", merged.text)
        self.assertIn("📑 质量：WEB-DL HDR 2160p / WEB-DL 1080p", merged.text)

    def test_episode_range_notifications_and_duplicates(self):
        first = self.make_message(1, "1G")
        first.title = "🎬 醉梦（2026）"
        first.text = first.text.replace("E01", "E01-E03")
        second = self.make_message(3, "2G")
        second.title = "🎬 醉梦 (2026)"
        merged = self.module._Merger._compose([first, second])
        self.assertIn("S01 E01-03", merged.text)
        self.assertIn("大小：3G", merged.text)

    def test_unsafe_or_incomplete_content_falls_back_without_information_loss(self):
        first = self.make_message(1, "4G")
        for text in (first.text.replace("4G", "未知"), first.text.replace("4G", "4"),
                     first.text.replace("💾 大小：4G\n", ""), first.text + "\n警告：有额外文件未处理"):
            second = self.make_message(2, "4G")
            second.text = text.replace("S01 E01", "S01 E02")
            with self.subTest(text=text):
                merged = self.module._Merger._compose([first, second])
                self.assertIn("共 2 条同类通知", merged.text)
                self.assertIn(second.text, merged.text)

    def test_failure_is_not_rewritten_as_success(self):
        first = self.make_message(1, "4G")
        first.text = "整理失败：权限不足"
        second = self.make_message(2, "4G")
        second.text = "整理失败：目标不可写"
        merged = self.module._Merger._compose([first, second])
        self.assertNotIn("✅ 已入库", merged.text)
        self.assertIn("权限不足", merged.text)
        self.assertIn("目标不可写", merged.text)

    def test_single_message_keeps_original_and_delivery_metadata(self):
        first = self.make_message(1, "4G")
        first.notification_route_scope = "user"
        first.disable_web_page_preview = True
        merged = self.module._Merger._compose([first])
        self.assertEqual(merged.text, first.text)
        self.assertEqual(merged.title, first.title)
        self.assertEqual(merged.notification_route_scope, "user")
        self.assertTrue(merged.disable_web_page_preview)

    def test_interactive_and_rich_messages_are_not_queued(self):
        for field, value in (("userid", "123"), ("buttons", [[{}]]), ("force_reply", True),
                             ("original_message_id", "123"), ("file_path", "/file"),
                             ("voice_path", "/voice"), ("rich_content", {"blocks": []}),
                             ("rich_message", "**rich**")):
            with self.subTest(field=field):
                message = self.make_message(1, "4G")
                setattr(message, field, value)
                self.assertFalse(self.module._Merger._should_merge(message))

    def test_rolling_window_ignores_cancelled_timer_and_writes_each_history(self):
        merger = self.module._Merger()
        merger.update(True)
        chain = Mock()
        messages = [self.make_message(1, "4G"), self.make_message(2, "4G")]
        for message in messages:
            message.title = "🎬 醉梦 (2026)"
        key = merger._group_key(messages[0])
        with patch.object(self.module.threading, "Timer") as timer, patch.object(merger, "_deliver") as deliver:
            self.assertTrue(merger.intercept(chain, messages[0], None, None, None, None, {}))
            stale_token = merger._groups[key]["token"]
            self.assertTrue(merger.intercept(chain, messages[1], None, None, None, None, {}))
            timer.return_value.cancel.assert_called_once()
            merger._flush(key, stale_token)
            deliver.assert_not_called()
            merger._flush(key, merger._groups[key]["token"])
            deliver.assert_called_once_with(messages, chain)
        self.assertEqual(chain.messageoper.add.call_count, 2)
        self.assertEqual(timer.call_args.args[0], 60)

    def test_configured_window_controls_timer_and_is_clamped(self):
        merger = self.module._Merger()
        merger.update(True, window_seconds=135)
        chain = Mock()
        message = self.make_message(1, "4G")
        with patch.object(self.module.threading, "Timer") as timer:
            merger.intercept(chain, message, None, None, None, None, {})
        self.assertEqual(timer.call_args.args[0], 135)
        merger.update(True, window_seconds=0)
        self.assertEqual(merger.window, 1)
        merger.update(True, window_seconds=99999)
        self.assertEqual(merger.window, 3600)
        merger.update(True, window_seconds="invalid")
        self.assertEqual(merger.window, 60)

    def test_plugin_config_and_form_expose_window_seconds(self):
        plugin = self.module.MessageMerge()
        plugin.init_plugin({"enabled": True, "window_seconds": "240"})
        self.assertEqual(plugin._window_seconds, 240)
        self.assertEqual(self.module._MERGER.window, 240)
        forms, defaults = plugin.get_form()
        self.assertEqual(defaults["window_seconds"], 60)
        self.assertTrue(any(
            component.get("component") == "VTextField"
            and component.get("props", {}).get("model") == "window_seconds"
            for component in self.iter_components(forms)
        ))

    @classmethod
    def iter_components(cls, components):
        for component in components:
            yield component
            yield from cls.iter_components(component.get("content", []))

    def test_configured_window_controls_timer_and_is_clamped(self):
        merger = self.module._Merger()
        merger.update(True, window_seconds=135)
        chain = Mock()
        message = self.make_message(1, "4G")
        with patch.object(self.module.threading, "Timer") as timer:
            merger.intercept(chain, message, None, None, None, None, {})
        self.assertEqual(timer.call_args.args[0], 135)
        merger.update(True, window_seconds=0)
        self.assertEqual(merger.window, 1)
        merger.update(True, window_seconds=99999)
        self.assertEqual(merger.window, 3600)
        merger.update(True, window_seconds="invalid")
        self.assertEqual(merger.window, 60)

    def test_plugin_config_and_form_expose_window_seconds(self):
        plugin = self.module.MessageMerge()
        plugin.init_plugin({"enabled": True, "window_seconds": "240"})
        self.assertEqual(plugin._window_seconds, 240)
        self.assertEqual(self.module._MERGER.window, 240)
        forms, defaults = plugin.get_form()
        self.assertEqual(defaults["window_seconds"], 60)
        self.assertTrue(any(
            component.get("component") == "VTextField"
            and component.get("props", {}).get("model") == "window_seconds"
            for component in self.iter_components(forms)
        ))

    @classmethod
    def iter_components(cls, components):
        for component in components:
            yield component
            yield from cls.iter_components(component.get("content", []))

    def test_flush_all_drains_pending_messages_once(self):
        merger = self.module._Merger()
        merger.update(True)
        chain = Mock()
        message = self.make_message(1, "4G")
        with patch.object(self.module.threading, "Timer"), patch.object(merger, "_deliver") as deliver:
            merger.intercept(chain, message, None, None, None, None, {})
            merger.flush_all()
            merger.flush_all()
            deliver.assert_called_once_with([message], chain)

    def test_disabled_merger_does_not_queue_or_write_history(self):
        merger = self.module._Merger()
        chain = Mock()
        with patch.object(self.module.threading, "Timer") as timer:
            self.assertFalse(merger.intercept(chain, self.make_message(1, "4G"), None, None, None, None, {}))
            timer.assert_not_called()
        chain.messageoper.add.assert_not_called()
        self.assertEqual(merger._groups, {})

    def test_failure_suffix_and_success_are_separate_groups(self):
        first = self.make_message(1, "4G")
        failure = self.make_message(2, "4G")
        failure.title += " 整理失败"
        failure.text = "权限不足"
        self.assertNotEqual(self.module._Merger._group_key(first), self.module._Merger._group_key(failure))


if __name__ == "__main__":
    unittest.main()
