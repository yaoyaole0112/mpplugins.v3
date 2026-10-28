import copy
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class SubscribeGroupPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        modules = {
            name: types.ModuleType(name)
            for name in (
                "app", "app.db", "app.db.oper", "app.db.oper.downloadhistory",
                "app.db.oper.site", "app.db.oper.subscribe", "app.plugins",
                "app.schemas", "app.schemas.types", "app.sdk", "app.sdk.events",
                "app.sdk.logging", "app.sdk.media",
            )
        }
        modules["app.db.oper.downloadhistory"].DownloadHistoryOper = Mock
        modules["app.db.oper.site"].SiteOper = Mock
        modules["app.db.oper.subscribe"].SubscribeOper = Mock
        modules["app.plugins"]._PluginBase = object
        modules["app.schemas.types"].EventType = types.SimpleNamespace(
            SubscribeAdded="subscribe.added", DownloadAdded="download.added"
        )
        modules["app.schemas.types"].MediaType = Mock()
        modules["app.schemas.types"].SystemConfigKey = Mock()
        modules["app.sdk.events"].Event = object
        modules["app.sdk.events"].eventmanager = types.SimpleNamespace(
            register=lambda event_type: lambda handler: handler
        )
        modules["app.sdk.logging"].logger = Mock()
        modules["app.sdk.media"].build_media_key = Mock()
        modules["app.sdk.media"].resolve_media_identity = Mock()
        plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
        spec = importlib.util.spec_from_file_location("test_subscribegroup_plugin", plugin_path)
        plugin_module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(plugin_module)
        cls.plugin_class = plugin_module.SubscribeGroup

    def setUp(self):
        self.plugin = self.plugin_class()
        self.plugin.get_data = Mock()

    def render_rows(self, history):
        self.plugin.get_data.return_value = history
        page = self.plugin.get_page()
        json.dumps({"render_mode": "vuetify", "page": page}, ensure_ascii=False).encode("utf-8")
        table = page[0]["content"][0]["content"][0]
        return table["content"][1]["content"]

    def render_content(self, content):
        history = [{
            "time": "2026-10-28 12:00:00",
            "name": "测试订阅",
            "type": "二级分类自定义配置",
            "content": content,
        }]
        return self.render_rows(history)[0]["content"][3]["text"]

    def test_escaped_emoji_json_is_valid_utf8(self):
        payload = {"filter_groups": ["🆙前置过滤", "✅促销免费", "📺剧集规则", "🕹️综艺规则"]}
        content = self.render_content(json.dumps(payload))
        self.assertEqual(json.loads(content), payload)
        self.assertIn("📺剧集规则", content)

    def test_unescaped_chinese_and_emoji_are_not_corrupted(self):
        payload = {"include": "国粤双语", "filter_groups": ["📺欧美剧订阅"]}
        self.assertEqual(json.loads(self.render_content(json.dumps(payload, ensure_ascii=False))), payload)

    def test_regex_backslashes_keep_json_semantics(self):
        payload = {"include": r"\bWEB\d+\b", "exclude": r"\u[0-9a-f]{4}", "savepath": r"C:\new\test"}
        self.assertEqual(json.loads(self.render_content(json.dumps(payload))), payload)

    def test_invalid_json_is_preserved_as_plain_text(self):
        content = "原始规则 📺 " + r"\u[0-9a-f]{4}\b\d+"
        self.assertEqual(self.render_content(content), content)

    def test_plain_chinese_is_preserved(self):
        self.assertEqual(self.render_content("中文历史内容"), "中文历史内容")

    def test_structured_content_is_supported(self):
        for payload in ({"include": "中文📺"}, ["🆙前置过滤", "📺剧集规则"]):
            with self.subTest(payload=payload):
                self.assertEqual(json.loads(self.render_content(payload)), payload)

    def test_empty_and_missing_content(self):
        for content in (None, ""):
            with self.subTest(content=content):
                self.assertEqual(self.render_content(content), "")
        row = self.render_rows([{"time": "2026-10-28 12:00:00", "name": "测试"}])[0]
        self.assertEqual(row["content"][3]["text"], "")

    def test_json_string_content_is_readable(self):
        self.assertEqual(self.render_content(json.dumps("中文📺")), "中文📺")

    def test_single_legacy_history_object(self):
        history = {"time": "2026-10-28 12:00:00", "name": "测试", "content": json.dumps({"include": "📺"})}
        self.assertEqual(len(self.render_rows(history)), 1)

    def test_history_is_sorted_without_modifying_persisted_data(self):
        history = [
            {"time": "2026-10-27 12:00:00", "name": "旧记录", "content": json.dumps({"include": "📺"})},
            {"time": "2026-10-28 12:00:00", "name": "新记录", "content": json.dumps({"include": "中文"})},
        ]
        original = copy.deepcopy(history)
        rows = self.render_rows(history)
        self.assertEqual([row["content"][1]["text"] for row in rows], ["新记录", "旧记录"])
        self.assertEqual(history, original)

    def test_empty_history_page(self):
        for history in (None, []):
            with self.subTest(history=history):
                self.plugin.get_data.return_value = history
                page = self.plugin.get_page()
                self.assertEqual(page[0]["text"], "暂无数据")
                json.dumps(page, ensure_ascii=False).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
