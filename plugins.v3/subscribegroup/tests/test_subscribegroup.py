import copy
import importlib.util
import json
import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import Mock, patch


class SubscribeGroupPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        modules = {
            name: types.ModuleType(name)
            for name in (
                "app", "app.api", "app.api.dependencies", "app.api.dependencies.auth",
                "fastapi", "app.db", "app.db.oper", "app.db.oper.downloadhistory",
                "app.db.oper.site", "app.db.oper.subscribe", "app.plugins",
                "app.schemas", "app.schemas.types", "app.sdk", "app.sdk.events",
                "app.sdk.logging", "app.sdk.media",
            )
        }
        modules["app.db.oper.downloadhistory"].DownloadHistoryOper = Mock
        modules["app.db.oper.site"].SiteOper = Mock
        modules["app.db.oper.subscribe"].SubscribeOper = Mock
        modules["fastapi"].BackgroundTasks = object
        modules["fastapi"].Depends = lambda dependency: dependency
        modules["app.api.dependencies.auth"].get_current_active_superuser = Mock()
        modules["app.plugins"]._PluginBase = object
        modules["app.schemas.types"].EventType = types.SimpleNamespace(
            SubscribeAdded="subscribe.added", DownloadAdded="download.added"
        )
        class MediaType(Enum):
            TV = "电视剧"
            MOVIE = "电影"
            MUSIC = "音乐"

        modules["app.schemas.types"].MediaType = MediaType
        modules["app.schemas.types"].SystemConfigKey = Mock()
        modules["app.sdk.events"].Event = object
        modules["app.sdk.events"].eventmanager = types.SimpleNamespace(
            register=lambda event_type: lambda handler: handler
        )
        modules["app.sdk.logging"].logger = Mock()
        modules["app.sdk.media"].build_media_key = Mock()
        modules["app.sdk.media"].resolve_media_identity = lambda media: (
            getattr(media, "media_source", None), getattr(media, "media_id", None)
        )
        plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
        spec = importlib.util.spec_from_file_location("test_subscribegroup_plugin", plugin_path)
        plugin_module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(plugin_module)
        cls.plugin_class = plugin_module.SubscribeGroup
        cls.plugin_module = plugin_module

    def setUp(self):
        self.plugin = self.plugin_class()
        self.plugin.get_data = Mock()

    def render_rows(self, history):
        self.plugin.get_data.return_value = history
        page = self.plugin.get_page()
        json.dumps({"render_mode": "vuetify", "page": page}, ensure_ascii=False).encode("utf-8")
        history_section = next(item for item in page if item["component"] == "VRow")
        table = history_section["content"][0]["content"][0]
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
                self.assertTrue(any(item.get("text") == "暂无数据" for item in page))
                self.assertEqual(page[0]["component"], "VCard")
                json.dumps(page, ensure_ascii=False).encode("utf-8")


class BackgroundQueue:
    def __init__(self):
        self.tasks = []

    def add_task(self, handler, *args):
        self.tasks.append((handler, args))

    def run(self):
        for handler, args in self.tasks:
            handler(*args)
        self.tasks.clear()


class ExistingSubscriptionFillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SubscribeGroupPageTests.setUpClass()
        cls.plugin_class = SubscribeGroupPageTests.plugin_class
        cls.plugin_module = SubscribeGroupPageTests.plugin_module

    def setUp(self):
        self.plugin = self.plugin_class()
        self.data = {}
        self.plugin.get_data = Mock(side_effect=lambda key: self.data.get(key))
        self.plugin.save_data = Mock(side_effect=lambda key, value: self.data.update({key: copy.deepcopy(value)}))
        self.plugin.update_config = Mock()
        self.plugin.init_plugin({})
        self.plugin._subscribe_confs = {"国产剧": {"filter_groups": ["📺剧集规则"]}, "华语电影": {"filter_groups": ["🎬电影规则"]}}
        self.plugin._subscribeoper = Mock()
        self.plugin.chain = Mock()
        self.queue = BackgroundQueue()
        self.subscriptions = []
        self.plugin._subscribeoper.list.side_effect = lambda: self.subscriptions
        self.plugin._subscribeoper.get.side_effect = lambda subscribe_id: next(
            (subscribe for subscribe in self.subscriptions if subscribe.id == subscribe_id), None
        )

        def update(subscribe_id, payload):
            subscribe = self.plugin._subscribeoper.get(subscribe_id)
            if subscribe is not None:
                for field, value in payload.items():
                    setattr(subscribe, field, value)
            return subscribe

        self.plugin._subscribeoper.update.side_effect = update

    def subscription(self, subscribe_id=1, **fields):
        values = {
            "id": subscribe_id, "name": f"测试订阅{subscribe_id}", "year": "2026",
            "type": "电视剧", "media_category": "国产剧", "media_source": "themoviedb",
            "media_id": str(subscribe_id), "episode_group": None,
            "include": "", "exclude": None, "filter_groups": [], "sites": [],
            "resolution": None, "quality": None, "effect": None, "save_path": "",
        }
        values.update(fields)
        subscribe = types.SimpleNamespace(**values)
        self.subscriptions.append(subscribe)
        return subscribe

    def run_fill(self, preview=False):
        response = self.plugin.preview_existing_fill(self.queue) if preview else self.plugin.start_existing_fill(self.queue)
        self.assertTrue(response["success"])
        self.queue.run()
        return self.plugin.get_existing_fill_status()["data"]

    def assert_response_envelope(self, response):
        self.assertEqual(set(response), {"success", "message", "data"})
        self.assertIsInstance(response["success"], bool)
        self.assertIsInstance(response["message"], str)

    def test_all_button_responses_match_strict_frontend_envelope(self):
        self.subscription()
        self.assert_response_envelope(self.plugin.get_existing_fill_status())
        self.assert_response_envelope(self.plugin.preview_existing_fill(self.queue))
        self.assert_response_envelope(self.plugin.start_existing_fill(self.queue))
        self.queue.run()
        response = self.plugin.get_existing_fill_status()
        self.assert_response_envelope(response)
        self.assertEqual(response["data"]["status"], "completed")
        self.assertEqual(response["message"], response["data"]["message"])
        self.assert_response_envelope(self.plugin.start_existing_fill(self.queue))
        self.queue.run()
        self.plugin._subscribe_confs = {}
        response = self.plugin.start_existing_fill(self.queue)
        self.assert_response_envelope(response)
        self.assertFalse(response["success"])

    def test_status_payload_is_nested_and_not_aliased(self):
        self.subscription()
        self.run_fill(preview=True)
        response = self.plugin.get_existing_fill_status()
        self.assert_response_envelope(response)
        self.assertNotIn("status", response)
        self.assertNotIn("updated", response)
        response["data"]["status"] = "changed-by-caller"
        self.assertEqual(self.data["last_fill_result"]["status"], "completed")

    def test_fills_tv_and_movie_but_not_music(self):
        television = self.subscription()
        movie = self.subscription(2, type="电影", media_category="华语电影")
        music = self.subscription(3, type="音乐", media_category="国产剧")
        result = self.run_fill()
        self.assertEqual((result["total"], result["updated"], result["failed"]), (2, 2, 0))
        self.assertEqual(television.filter_groups, ["📺剧集规则"])
        self.assertEqual(movie.filter_groups, ["🎬电影规则"])
        self.assertEqual(music.filter_groups, [])
        self.assertEqual(len(self.data["history"]), 2)

    def test_default_preserves_existing_fields_and_fills_missing_fields(self):
        self.plugin._subscribe_confs["国产剧"].update({"include": "新的规则", "exclude": "广告", "sites": [1]})
        subscribe = self.subscription(include="手动设置", sites=[9])
        self.run_fill()
        self.assertEqual(subscribe.include, "手动设置")
        self.assertEqual(subscribe.sites, [9])
        self.assertEqual(subscribe.exclude, "广告")
        payload = self.plugin._subscribeoper.update.call_args.args[1]
        self.assertNotIn("include", payload)
        self.assertNotIn("sites", payload)

    def test_overwrite_changes_only_configured_fields(self):
        self.plugin._overwrite_existing = True
        subscribe = self.subscription(filter_groups=["旧规则"], include="保留", save_path="/手动路径")
        self.run_fill()
        self.assertEqual(subscribe.filter_groups, ["📺剧集规则"])
        self.assertEqual(subscribe.include, "保留")
        self.assertEqual(subscribe.save_path, "/手动路径")

    def test_reuses_resource_parsers_and_name_variable(self):
        self.plugin._subscribe_confs["国产剧"].update({
            "resolution": "2160p", "quality": "WEB-DL", "effect": "HDR",
            "savepath": "/media/{name}", "include": r"\bWEB\d+", "exclude": "预告", "sites": [1, 2],
        })
        subscribe = self.subscription(name="测试剧集")
        self.run_fill()
        self.assertEqual(subscribe.resolution, "4K|2160p|x2160")
        self.assertEqual(subscribe.quality, "WEB-?DL|WEB-?RIP")
        self.assertEqual(subscribe.save_path, "/media/测试剧集 (2026)")
        self.assertEqual(subscribe.include, r"\bWEB\d+")
        self.assertEqual(subscribe.sites, [1, 2])

    def test_preview_does_not_write_subscriptions_or_history(self):
        subscribe = self.subscription()
        original = copy.deepcopy(vars(subscribe))
        result = self.run_fill(preview=True)
        self.assertEqual(result["updated"], 1)
        self.assertTrue(result["preview"])
        self.assertIn("可补填 1 条", result["message"])
        self.assertEqual(vars(subscribe), original)
        self.plugin._subscribeoper.update.assert_not_called()
        self.assertNotIn("history", self.data)

    def test_missing_category_is_recognized_by_media_identity(self):
        self.subscription(media_category=None, episode_group="group-1")
        self.plugin.chain.recognize_media.return_value = types.SimpleNamespace(category="国产剧")
        result = self.run_fill()
        self.assertEqual(result["updated"], 1)
        self.plugin.chain.recognize_media.assert_called_once_with(
            mtype=self.plugin_module.MediaType.TV, media_source="themoviedb", media_id="1", episode_group="group-1"
        )

    def test_recognition_category_is_cached_across_seasons(self):
        self.subscription(media_category=None)
        self.subscription(2, media_category=None, media_id="1")
        self.plugin.chain.recognize_media.return_value = types.SimpleNamespace(category="国产剧")
        self.assertEqual(self.run_fill()["updated"], 2)
        self.plugin.chain.recognize_media.assert_called_once()

    def test_unknown_category_and_missing_identity_are_skipped(self):
        self.subscription(media_category="未配置分类")
        self.subscription(2, media_category=None, media_id=None)
        self.subscription(3, media_category=None)
        self.plugin.chain.recognize_media.return_value = None
        result = self.run_fill()
        self.assertEqual((result["skipped"], result["failed"], result["updated"]), (3, 0, 0))
        self.plugin._subscribeoper.update.assert_not_called()

    def test_one_recognition_failure_does_not_stop_remaining_subscriptions(self):
        self.subscription(media_category=None)
        self.subscription(2)
        self.plugin.chain.recognize_media.side_effect = TimeoutError("识别超时")
        result = self.run_fill()
        self.assertEqual((result["status"], result["failed"], result["updated"]), ("completed", 1, 1))

    def test_deleted_subscriptions_are_skipped(self):
        self.subscription()
        self.plugin._subscribeoper.get.side_effect = None
        self.plugin._subscribeoper.get.return_value = None
        self.assertEqual(self.run_fill()["skipped"], 1)
        self.plugin._subscribeoper.update.assert_not_called()

    def test_deletion_during_update_is_not_recorded_as_success(self):
        self.subscription()
        self.plugin._subscribeoper.update.side_effect = None
        self.plugin._subscribeoper.update.return_value = None
        result = self.run_fill()
        self.assertEqual((result["updated"], result["skipped"]), (0, 1))
        self.assertNotIn("history", self.data)

    def test_rechecks_fields_after_recognition_to_preserve_concurrent_manual_edit(self):
        original = self.subscription(media_category=None)
        latest = copy.copy(original)
        latest.filter_groups = ["用户刚刚填写"]
        self.plugin._subscribeoper.get.side_effect = [original, latest]
        self.plugin.chain.recognize_media.return_value = types.SimpleNamespace(category="国产剧")
        self.assertEqual(self.run_fill()["updated"], 0)
        self.plugin._subscribeoper.update.assert_not_called()

    def test_repeated_runs_are_idempotent_even_in_overwrite_mode(self):
        self.plugin._overwrite_existing = True
        self.subscription()
        self.assertEqual(self.run_fill()["updated"], 1)
        self.assertEqual(self.run_fill()["updated"], 0)
        self.assertEqual(len(self.data["history"]), 1)

    def test_changed_media_identity_during_recognition_is_skipped(self):
        original = self.subscription(media_category=None)
        latest = copy.copy(original)
        latest.media_id = "changed-identity"
        self.plugin._subscribeoper.get.side_effect = [original, latest]
        self.plugin.chain.recognize_media.return_value = types.SimpleNamespace(category="国产剧")
        self.assertEqual(self.run_fill()["skipped"], 1)
        self.plugin._subscribeoper.update.assert_not_called()

    def test_type_changed_to_music_before_scan_is_skipped(self):
        original = self.subscription()
        latest = copy.copy(original)
        latest.type = "音乐"
        self.plugin._subscribeoper.get.side_effect = None
        self.plugin._subscribeoper.get.return_value = latest
        self.assertEqual(self.run_fill()["skipped"], 1)
        self.plugin._subscribeoper.update.assert_not_called()

    def test_duplicate_start_is_rejected_until_task_completes(self):
        self.subscription()
        self.assertTrue(self.plugin.start_existing_fill(self.queue)["success"])
        self.assertFalse(self.plugin.start_existing_fill(self.queue)["success"])
        self.assertEqual(len(self.queue.tasks), 1)
        self.queue.run()
        self.assertTrue(self.plugin.start_existing_fill(self.queue)["success"])
        self.queue.run()

    def test_stop_cancels_queued_job_without_writing(self):
        self.subscription()
        self.plugin.start_existing_fill(self.queue)
        self.plugin.stop_service()
        self.queue.run()
        self.assertEqual(self.data["last_fill_result"]["status"], "cancelled")
        self.plugin._subscribeoper.update.assert_not_called()

    def test_stop_during_recognition_prevents_write(self):
        self.subscription(media_category=None)

        def recognize(**kwargs):
            self.plugin.stop_service()
            return types.SimpleNamespace(category="国产剧")

        self.plugin.chain.recognize_media.side_effect = recognize
        self.assertEqual(self.run_fill()["status"], "cancelled")
        self.plugin._subscribeoper.update.assert_not_called()

    def test_configuration_is_snapshotted_when_job_is_started(self):
        subscribe = self.subscription()
        self.plugin.start_existing_fill(self.queue)
        self.plugin._subscribe_confs["国产剧"]["filter_groups"] = ["之后保存的新规则"]
        self.queue.run()
        self.assertEqual(subscribe.filter_groups, ["📺剧集规则"])

    def test_no_rules_rejects_start(self):
        self.plugin._subscribe_confs = {}
        self.assertFalse(self.plugin.start_existing_fill(self.queue)["success"])
        self.assertEqual(self.queue.tasks, [])

    def test_list_failure_releases_lock_and_reports_failure(self):
        self.plugin._subscribeoper.list.side_effect = RuntimeError("数据库不可用")
        self.assertEqual(self.run_fill()["status"], "failed")
        self.assertFalse(self.plugin._fill_lock.locked())

    def test_manual_fill_is_independent_of_automatic_event_switches(self):
        self.subscription()
        self.assertFalse(self.plugin.get_state())
        self.assertEqual(self.run_fill()["updated"], 1)

    def test_new_subscription_event_reuses_same_rule_builder(self):
        self.plugin._category = True
        subscribe = self.subscription(filter_groups=["原有规则"])
        event = types.SimpleNamespace(event_data={"subscribe_id": subscribe.id, "mediainfo": {"category": "国产剧"}})
        self.plugin.subscribe_notice(event)
        self.assertEqual(subscribe.filter_groups, ["📺剧集规则"])
        self.assertEqual(len(self.data["history"]), 1)

    def test_api_endpoints_are_admin_only_and_mutations_use_post(self):
        endpoints = self.plugin.get_api()
        self.assertEqual(len(endpoints), 3)
        self.assertTrue(all(item["dependencies"] and item["auth"] == "bear" for item in endpoints))
        self.assertEqual({item["path"]: item["methods"] for item in endpoints}, {
            "/fill-existing": ["POST"], "/preview-existing": ["POST"], "/fill-existing/status": ["GET"]
        })

    def test_overwrite_setting_is_default_off_and_persists(self):
        form, defaults = self.plugin.get_form()
        self.assertFalse(defaults["overwrite_existing"])
        self.assertIn("overwrite_existing", json.dumps(form))
        self.plugin.init_plugin({"overwrite_existing": True})
        self.plugin._SubscribeGroup__update_config()
        self.assertTrue(self.plugin.update_config.call_args.args[0]["overwrite_existing"])

    def test_empty_subscriptions_complete_without_changes(self):
        result = self.run_fill()
        self.assertEqual((result["status"], result["total"], result["updated"]), ("completed", 0, 0))


if __name__ == "__main__":
    unittest.main()
