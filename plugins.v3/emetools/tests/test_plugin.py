import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from emetools import EmeTools, ScheduleChange, ToolAction, _log_label
from emetools.invalid_data import InvalidDataCleaner, QUARANTINE
from emetools.p115 import P115Client
from emetools.subscription_monitor import matches_subscription, matches_keyword, normalize_channel
from app.schemas.types import EventType, NotificationChannel, MessageType
from app.schemas.system import NotificationConf


class InvalidDataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cleaner = InvalidDataCleaner(str(self.root))

    def test_retain_referenced_and_shared_metadata(self):
        (self.root / "Movie.strm").write_text("https://example.com", encoding="utf-8")
        (self.root / "Movie-poster.jpg").write_bytes(b"image")
        (self.root / "Movie-mediainfo.json").write_text("{}", encoding="utf-8")
        (self.root / "movie.nfo").write_text("metadata", encoding="utf-8")
        (self.root / "orphan.jpg").write_bytes(b"orphan")
        report = self.cleaner.start_scan(str(self.root))
        self.assertEqual([item["name"] for item in report["items"]], ["orphan.jpg"])

    def test_modified_file_is_not_deleted(self):
        orphan = self.root / "orphan.nfo"
        orphan.write_text("first", encoding="utf-8")
        report = self.cleaner.start_scan(str(self.root))
        orphan.write_text("changed", encoding="utf-8")
        result = self.cleaner.delete(report["scan_token"], [str(orphan)])
        self.assertTrue(orphan.exists())
        self.assertFalse(result["deleted"])
        self.assertEqual(len(result["failed"]), 1)

    def test_quarantine_is_recoverable_and_excluded(self):
        orphan = self.root / "orphan.nfo"
        orphan.write_text("first", encoding="utf-8")
        report = self.cleaner.start_scan(str(self.root))
        result = self.cleaner.delete(report["scan_token"], [str(orphan)])
        self.assertFalse(orphan.exists())
        self.assertEqual(len(result["deleted"]), 1)
        stored = Path(result["deleted"][0]["quarantine"])
        self.assertTrue(stored.is_file())
        self.assertTrue((stored.parent.parent / "manifest.json").exists())
        self.assertEqual(self.cleaner.start_scan(str(self.root))["count"], 0)
        self.assertTrue((self.root / QUARANTINE).exists())

    def test_rejects_outside_and_symlink_directory(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (self.root / "link").symlink_to(outside.name)
        with self.assertRaises(ValueError):
            self.cleaner.scan(str(self.root / "link"))
        with self.assertRaises(ValueError):
            self.cleaner.scan(outside.name)

    def test_empty_subtree_is_one_quarantine_item(self):
        target = self.root / "gone" / "deep"
        target.mkdir(parents=True)
        (target / "poster.jpg").write_bytes(b"image")
        (target / "old.json").write_text("{}", encoding="utf-8")
        (self.root / "keep.strm").write_text("https://example.com", encoding="utf-8")
        items = self.cleaner.start_scan(str(self.root))["items"]
        self.assertEqual([(item["name"], item["kind"], item["files"]) for item in items],
                         [("gone", "directory", 2)])

    def test_new_nested_strm_prevents_folder_quarantine(self):
        target = self.root / "gone"
        target.mkdir()
        (target / "old.nfo").write_text("obsolete", encoding="utf-8")
        report = self.cleaner.start_scan(str(self.root))
        (target / "new.strm").write_text("https://example.com", encoding="utf-8")
        result = self.cleaner.delete(report["scan_token"], [str(target)])
        self.assertEqual(len(result["failed"]), 1)
        self.assertTrue((target / "new.strm").exists())

    def test_scan_token_cannot_be_reused_for_new_files(self):
        old = self.root / "old.nfo"
        old.write_text("old", encoding="utf-8")
        report = self.cleaner.start_scan(str(self.root))
        later = self.root / "later.nfo"
        later.write_text("later", encoding="utf-8")
        result = self.cleaner.delete(report["scan_token"], [str(later)])
        self.assertFalse(result["deleted"])
        self.assertTrue(later.exists())


class P115ParseTests(unittest.TestCase):
    def test_directory_and_file_fields(self):
        result = P115Client._parse_entries({"state": True, "count": 2, "data": [
            {"fid": "123", "fn": "影片", "fc": "0"},
            {"fid": "456", "fn": "video.mkv", "fc": "1", "fs": "99"},
        ]})
        self.assertEqual(result[0], [{"fid": "456", "name": "video.mkv", "size": 99}])
        self.assertEqual(result[1][0]["cid"], "123")


class PluginTests(unittest.TestCase):
    def test_sidebar_uses_background_free_sparkles_icon(self):
        self.assertEqual(self.plugin.get_sidebar_nav()[0]["icon"], "mdi-creation-outline")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.plugin = object.__new__(EmeTools)
        self.plugin.get_config = MagicMock(return_value={})
        self.plugin.get_data = MagicMock(return_value=None)
        self.plugin.save_data = MagicMock()
        with patch('emetools.missing_episodes.SubscribeChain', return_value=MagicMock()), patch('emetools.missing_episodes.MediaServerHelper', return_value=MagicMock()):
            self.plugin.init_plugin({"strm_root": self.directory.name})
        self.plugin.update_config = MagicMock()
        self.plugin.get_config = MagicMock(return_value={})
        self.plugin.chain = MagicMock()

    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def test_media_scan_libraries_are_independent_of_scheduled_libraries(self):
        self.plugin._media_config["library_ids"] = ["scheduled::1"]
        self.plugin._media_scan_library_ids = []
        initial = self.run_async(self.plugin.media_status())
        self.assertEqual(initial["scan_library_ids"], [])
        self.assertEqual(initial["config"]["library_ids"], ["scheduled::1"])
        with patch("emetools.threading.Thread") as thread:
            self.run_async(self.plugin.media_action({"operation": "scan", "library_ids": ["manual::2"]}))
            thread.return_value.start.assert_called_once()
        self.assertEqual(self.plugin._media_scan_library_ids, ["manual::2"])
        self.assertEqual(self.plugin._media_config["library_ids"], ["scheduled::1"])
        self.assertEqual(self.plugin.update_config.call_args.args[0]["media_scan_library_ids"], ["manual::2"])
        self.assertEqual(self.plugin.update_config.call_args.args[0]["media_cleanup"]["library_ids"], ["scheduled::1"])
        self.plugin._media.running = False
        with patch("emetools.Scheduler"):
            self.run_async(self.plugin.media_action({"operation": "save", "config": {"library_ids": ["scheduled::3"]}}))
        self.assertEqual(self.plugin._media_scan_library_ids, ["manual::2"])
        self.assertEqual(self.run_async(self.plugin.media_status())["config"]["library_ids"], ["scheduled::3"])

    def test_missing_migration_defaults_off_and_copies_legacy_results(self):
        from emetools.missing_episodes import MissingAction
        plugin = object.__new__(EmeTools)
        plugin.get_config = MagicMock(return_value={
            "enabled": True, "missing_action": MissingAction.ADD_SUBSCRIBE.value,
            "skip_series_ids": ["123"], "library_names": ["电视剧"]})
        plugin.get_data = MagicMock(side_effect=lambda key, plugin_id=None:
                                    [{"SeriesName": "测试剧"}] if plugin_id else None)
        plugin.save_data = MagicMock()
        with patch('emetools.missing_episodes.SubscribeChain', return_value=MagicMock()):
            plugin.init_plugin({"strm_root": self.directory.name})
        self.assertFalse(plugin._missing_config["enabled"])
        self.assertEqual(plugin._missing_config["missing_action"], MissingAction.ADD_SUBSCRIBE.value)
        plugin.save_data.assert_any_call("missing_episodes", [{"SeriesName": "测试剧"}])
        self.assertEqual(plugin._missing._subscribe_chain._delete_subscription.call_count, 0)

    def test_missing_refuses_duplicate_schedule_and_mutating_scan(self):
        from emetools.missing_episodes import MissingAction
        self.plugin.get_config.return_value = {"enabled": True}
        self.plugin._missing_config.update({"enabled": True, "missing_action": MissingAction.ADD_SUBSCRIBE.value})
        self.assertFalse(any(job["id"] == "EmeTools_missing" for job in self.plugin.get_service()))
        with self.assertRaises(HTTPException) as raised:
            self.run_async(self.plugin.missing_action({"operation": "scan"}))
        self.assertEqual(raised.exception.status_code, 409)
        self.plugin._missing.scan_missing_episodes = MagicMock()
        self.plugin._run_missing_scheduled()
        self.plugin._missing.scan_missing_episodes.assert_not_called()

    def test_missing_schedule_requires_old_plugin_off(self):
        self.plugin.get_config.return_value = {"enabled": True}
        with self.assertRaises(HTTPException) as raised:
            self.run_async(self.plugin.missing_action({"operation": "save", "config": {"enabled": True}}))
        self.assertEqual(raised.exception.status_code, 409)

    def test_bot_commands_require_explicit_user_and_never_delete_directly(self):
        commands = {item["cmd"]: item for item in self.plugin.get_command()}
        self.assertEqual(set(commands), {"/cleanup", "/cleanfiles", "/cleartrash", "/ememove", "/cleandupes"})
        self.assertTrue(all(item["event"] == EventType.PluginAction for item in commands.values()))
        self.plugin._trash_clear = MagicMock()
        self.plugin._cleanup_confirm = MagicMock()
        self.plugin.tool_command(MagicMock(event_data={"action": "emetools_cleartrash", "channel": NotificationChannel.Telegram}))
        self.plugin._trash_clear.assert_not_called()
        self.plugin._cleanup_confirm.assert_not_called()
        self.plugin.chain.post_message.assert_not_called()

    def test_media_bot_confirmation_is_scoped_to_origin_and_single_use(self):
        from emetools import DEFAULT_MEDIA_CLEANUP
        self.plugin._media.scan = MagicMock(return_value={"results": [{"versions": [
            {"is_best": False, "file_path": "/strm/low.strm", "size": 42}]}]})
        self.plugin._media.last_scan = "original-scan"
        self.plugin._media.delete = MagicMock(return_value={"deleted": [{"file_path": "/strm/low.strm", "size": 42}], "failures": []})
        self.plugin._media.refresh_emby = MagicMock()
        self.plugin._media_config = {**DEFAULT_MEDIA_CLEANUP, "library_ids": ["emby::1"]}
        admin = {"origin": {"TELEGRAM_ADMINS": "123"}, "other": {"TELEGRAM_ADMINS": "123"}}
        with patch.object(self.plugin, "_telegram_command_sources", return_value=admin), \
             patch("emetools.matches_channel_admin", side_effect=lambda _, cfg, uid: bool(cfg) and cfg.get("TELEGRAM_ADMINS") == uid):
            self.plugin._request_media_bot_cleanup({"source": "origin", "user": "123", "channel": NotificationChannel.Telegram})
            self.plugin._media.scan.assert_called_once_with(["emby::1"])
            confirmation = self.plugin.chain.post_message.call_args.args[0]
            self.assertEqual((confirmation.source, confirmation.userid), ("origin", "123"))
            callback = confirmation.buttons[0][0]["callback_data"]
            token = callback.split("|")[1].split(":")[1]
            other = {"channel": NotificationChannel.Telegram, "source": "other", "userid": "123", "original_chat_id": "123"}
            self.plugin._handle_media_bot_confirmation(token, True, other)
            self.plugin._media.delete.assert_not_called()
            self.plugin._handle_media_bot_confirmation(token, True, {**other, "source": "origin", "original_chat_id": "456"})
            self.plugin._media.delete.assert_not_called()
            self.plugin._handle_media_bot_confirmation(token, True, {**other, "source": "origin"})
            self.plugin._media.delete.assert_called_once_with(["/strm/low.strm"])
            self.assertEqual(self.plugin.chain.post_message.call_args.args[0].source, "origin")
            self.plugin._handle_media_bot_confirmation(token, True, {**other, "source": "origin"})
            self.plugin._media.delete.assert_called_once()

    def test_command_reply_uses_only_originating_bot_source(self):
        from app.schemas.types import NotificationChannel
        self.plugin._cleaner.start_scan = MagicMock(return_value={"count": 0})
        self.plugin._run_tool_command("emetools_cleanup", {
            "channel": NotificationChannel.Telegram, "source": "通知 Bot", "user": "123"})
        message = self.plugin.chain.post_message.call_args.args[0]
        self.assertEqual(message.channel, NotificationChannel.Telegram)
        self.assertEqual(message.source, "通知 Bot")
        self.assertEqual(message.userid, "123")
        self.assertEqual(self.plugin.chain.post_message.call_count, 1)

    def test_command_move_does_not_send_global_tool_notice(self):
        self.plugin._move_run = MagicMock(return_value={"ok": True, "moved": 1,
                                                        "errors": [], "details": []})
        self.plugin._send_tool_notice = MagicMock()
        self.plugin._run_tool_command("emetools_move", {
            "channel": NotificationChannel.Telegram, "source": "入库 Bot", "user": "123"})
        self.plugin._send_tool_notice.assert_not_called()
        message = self.plugin.chain.post_message.call_args.args[0]
        self.assertEqual(message.source, "入库 Bot")
        self.assertEqual(self.plugin.chain.post_message.call_count, 1)

    def test_command_failure_also_replies_only_to_originating_bot(self):
        self.plugin._cleaner.start_scan = MagicMock(side_effect=RuntimeError("secret=hidden"))
        self.plugin._run_tool_command("emetools_cleanup", {
            "channel": NotificationChannel.Telegram, "source": "通知 Bot", "user": "123"})
        message = self.plugin.chain.post_message.call_args.args[0]
        self.assertEqual(message.source, "通知 Bot")
        self.assertNotIn("secret=hidden", message.text)
        self.assertEqual(self.plugin.chain.post_message.call_count, 1)

    def test_scan_and_cleanup_logging_includes_item_and_result_without_token(self):
        target = Path(self.directory.name) / "orphan.nfo"
        target.write_text("orphan", encoding="utf-8")
        with patch("emetools.logger.info") as logged:
            scan = self.run_async(self.plugin.action(ToolAction(operation="scan", path=self.directory.name)))
            result = self.run_async(self.plugin.action(ToolAction(
                operation="delete", path=self.directory.name, scan_token=scan["scan_token"],
                paths=[str(target)])))
        self.assertTrue(result["ok"])
        templates = " ".join(str(call.args[0]) for call in logged.call_args_list)
        self.assertIn("扫描完成", templates)
        self.assertIn("候选", templates)
        self.assertIn("隔离结果", templates)
        self.assertNotIn(scan["scan_token"], str(logged.call_args_list))

    def test_log_labels_hide_links_and_credentials(self):
        label = _log_label("movie\n token=123:secret https://example.invalid/path?cookie=secret")
        self.assertNotIn("123:secret", label)
        self.assertNotIn("example.invalid", label)
        self.assertNotIn("\n", label)

    def test_icon_is_shipped_with_plugin(self):
        from emetools import ICON_URL
        self.assertTrue(ICON_URL.endswith("/plugins.v3/emetools/icon.jpeg"))
        self.assertEqual((Path(__file__).resolve().parents[1] / "icon.jpeg").read_bytes()[:3], b"\xff\xd8\xff")

    def test_old_eme_address_is_ignored(self):
        with patch('emetools.missing_episodes.SubscribeChain', return_value=MagicMock()):
            self.plugin.init_plugin({"eme_url": "http://nonexistent:7077", "strm_root": self.directory.name})
        self.assertNotIn("eme_url", str(self.run_async(self.plugin.status())))
        self.assertEqual(self.plugin.get_service(), [])

    def test_monitor_defaults_off_and_secrets_are_not_exposed(self):
        with patch('emetools.missing_episodes.SubscribeChain', return_value=MagicMock()):
            self.plugin.init_plugin({"strm_root": self.directory.name, "tg_api_id": "1234",
                                     "tg_api_hash": "a" * 32, "tg_forward_token": "123:" + "z" * 35,
                                     "tg_session": "secret-session"})
        self.plugin.get_config = MagicMock(return_value={})
        status = self.run_async(self.plugin.status())
        self.assertEqual(self.plugin._monitor_config["sub"]["enabled"], False)
        self.assertEqual(self.plugin._monitor_config["kw"]["enabled"], False)
        for secret in ("a" * 32, "secret-session", "123:" + "z" * 35):
            self.assertNotIn(secret, str(status))

    def test_subscription_list_not_published_but_monitor_still_reads_mp(self):
        from emetools.subscription_monitor import SubscriptionMonitor
        paths = {route["path"] for route in self.plugin.get_api()}
        self.assertNotIn("/subscriptions", paths)
        self.assertIn("/monitor/status", paths)
        self.assertTrue(callable(self.plugin._subscription_items))
        self.assertTrue(callable(SubscriptionMonitor._on_message))

    def test_monitor_save_and_validation(self):
        from emetools import MonitorChange
        self.run_async(self.plugin.monitor_action(MonitorChange(
            operation="save", scope="kw", channels=["@channelname", "https://t.me/channelname"],
            keywords=["Movie.*2026"], blacklist=["camrip"])))
        self.assertEqual(self.plugin._monitor_config["kw"]["channels"], ["channelname"])
        with self.assertRaises(HTTPException):
            self.run_async(self.plugin.monitor_action(MonitorChange(
                operation="save", scope="sub", channels=["https://other.host/bad"])))
        with self.assertRaises(HTTPException):
            self.run_async(self.plugin.monitor_action(MonitorChange(
                operation="save", scope="kw", keywords=["["])))

    def test_credentials_change_requires_logout_and_unknown_settings_rejected(self):
        self.plugin._tg_session = "logged-in-session"
        self.plugin._tg_api_id = "1234"
        with self.assertRaises(HTTPException):
            self.run_async(self.plugin.save_settings({"tg_api_id": "5678"}))
        with self.assertRaises(HTTPException):
            self.run_async(self.plugin.save_settings({"tg_session": "inject"}))

    def test_basic_and_telegram_settings_can_be_saved_independently(self):
        self.plugin._tg_api_id = "1234"
        self.plugin._tg_api_hash = "a" * 32
        self.plugin._tg_forward_token = "123:" + "z" * 35
        self.run_async(self.plugin.save_settings({"show_sidebar_nav": False}))
        self.assertEqual(self.plugin._tg_api_id, "1234")
        self.assertEqual(self.plugin._tg_api_hash, "a" * 32)
        self.assertEqual(self.plugin._tg_forward_token, "123:" + "z" * 35)
        self.run_async(self.plugin.save_settings({"tg_forward_token": "321:" + "x" * 35}))
        self.assertFalse(self.plugin._show_sidebar_nav)
        self.assertEqual(self.plugin._strm_root, self.directory.name)
        self.assertEqual(self.plugin._tg_forward_token, "321:" + "x" * 35)

    def test_rejects_unknown_schedule_and_root_cleanup(self):
        with self.assertRaises(HTTPException):
            self.run_async(self.plugin.save_schedule(ScheduleChange(
                section="p115_trash", settings={"password": "overwrite"})))
        with self.assertRaises(HTTPException):
            self.run_async(self.plugin.save_schedule(ScheduleChange(
                section="p115_cleanup", settings={"dir_ids": ["0"]})))

    def test_requires_cookie_before_enabling_115_schedule(self):
        with self.assertRaises(HTTPException):
            self.run_async(self.plugin.save_schedule(ScheduleChange(
                section="p115_trash", settings={"enabled": True})))

    def test_helper_cookie_is_read_fresh_for_each_operation(self):
        self.plugin.get_config.side_effect = [{"cookies": "first"}, {"cookies": "second"}]
        with patch("emetools.P115Client") as client:
            self.plugin._client()
            self.plugin._client()
        self.assertEqual(client.call_args_list[0].args, ("first",))
        self.assertEqual(client.call_args_list[1].args, ("second",))
        self.plugin.get_config.assert_called_with("P115StrmHelper")

    def test_cookie_and_proxy_are_not_saved_locally(self):
        with patch('emetools.missing_episodes.SubscribeChain', return_value=MagicMock()):
            self.plugin.init_plugin({"strm_root": self.directory.name, "cookie": "legacy", "proxy": "http://old"})
        migrated = self.plugin.update_config.call_args.args[0]
        self.assertNotIn("cookie", migrated)
        self.assertNotIn("proxy", migrated)
        self.plugin.get_config.return_value = {"cookies": "helper"}
        result = self.run_async(self.plugin.save_settings({"strm_root": self.directory.name}))
        self.assertTrue(result["settings"]["cookie_configured"])
        saved = self.plugin.update_config.call_args.args[0]
        self.assertNotIn("cookie", saved)
        self.assertNotIn("proxy", saved)
        with self.assertRaises(HTTPException):
            self.run_async(self.plugin.save_settings({"proxy": "http://old"}))

    def test_root_browser_allows_navigation_but_rejects_symlinks(self):
        (Path(self.directory.name) / "child").mkdir()
        (Path(self.directory.name) / "linked").symlink_to("child", target_is_directory=True)
        response = self.run_async(self.plugin.action(ToolAction(operation="root_dirs", path=self.directory.name)))
        self.assertEqual(response["dirs"], ["child"])
        response = self.run_async(self.plugin.action(ToolAction(operation="root_dirs", path=str(Path(self.directory.name) / "linked"))))
        self.assertFalse(response["ok"])

    def test_new_root_updates_unconfigured_scan_path(self):
        new_root = Path(self.directory.name) / "new"
        new_root.mkdir()
        self.run_async(self.plugin.save_settings({"strm_root": str(new_root)}))
        self.assertEqual(self.plugin._schedule["tools"]["path"], str(new_root))

    def test_new_root_keeps_active_cleanup_from_changing_target(self):
        new_root = Path(self.directory.name) / "new"
        new_root.mkdir()
        self.plugin._schedule["tools"].update({"path": self.directory.name, "enabled": True})
        with self.assertRaises(HTTPException):
            self.run_async(self.plugin.save_settings({"strm_root": str(new_root)}))
        self.assertEqual(self.plugin._strm_root, self.directory.name)

    def test_scan_token_required_to_delete(self):
        with self.assertRaises(HTTPException):
            self.run_async(self.plugin.action(ToolAction(operation="delete", paths=["/strm/a"])))

    def test_cookie_never_exposed_in_status(self):
        self.plugin.get_config.return_value = {"cookies": "sensitive"}
        result = self.run_async(self.plugin.status())
        self.assertNotIn("sensitive", str(result))
        self.assertTrue(result["settings"]["cookie_configured"])

    def test_scheduled_scan_requires_page_confirmation(self):
        orphan = Path(self.directory.name) / "orphan.nfo"
        orphan.write_text("metadata", encoding="utf-8")
        self.plugin._schedule["tools"].update({"enabled": True, "path": self.directory.name,
                                                "auto_delete": True, "confirm_cleanup": True,
                                                "confirm_mode": "moviepilot"})
        self.plugin.chain = MagicMock()
        self.plugin._run_scheduled("tools")
        self.assertTrue(orphan.exists())
        pending = self.run_async(self.plugin.action(ToolAction(operation="pending")))["pending"]
        self.assertEqual(len(pending), 1)
        confirmed = self.run_async(self.plugin.action(ToolAction(
            operation="confirm_scheduled", token=pending[0]["token"])))
        self.assertEqual(len(confirmed["deleted"]), 1)
        self.assertFalse(orphan.exists())
        self.assertEqual(self.plugin.chain.post_message.call_count, 2)

    def test_disabling_auto_cleanup_does_not_create_pending(self):
        orphan = Path(self.directory.name) / "orphan.nfo"
        orphan.write_text("metadata", encoding="utf-8")
        self.plugin._schedule["tools"].update({"enabled": True, "path": self.directory.name,
                                                "auto_delete": False, "confirm_cleanup": True,
                                                "confirm_mode": "moviepilot"})
        self.plugin.chain = MagicMock()
        self.plugin._run_scheduled("tools")
        self.assertTrue(orphan.exists())
        self.assertEqual(self.run_async(self.plugin.action(ToolAction(operation="pending")))["pending"], [])

    @staticmethod
    def telegram_conf():
        return NotificationConf(name="安全 Bot", type="telegram", enabled=True,
                                switchs=[MessageType.Plugin.value], config={"TELEGRAM_ADMINS": "123"})

    def test_telegram_confirmation_sends_buttons_without_deleting(self):
        orphan = Path(self.directory.name) / "orphan.nfo"
        orphan.write_text("metadata", encoding="utf-8")
        self.plugin._schedule["tools"].update({"enabled": True, "path": self.directory.name,
                                                "auto_delete": True, "confirm_mode": "telegram"})
        with patch("emetools.get_service_configs", return_value=[self.telegram_conf()]):
            self.plugin._run_scheduled("tools")
        self.assertTrue(orphan.exists())
        message = self.plugin.chain.post_message.call_args.args[0]
        self.assertEqual(message.channel, NotificationChannel.Telegram)
        self.assertEqual(message.mtype, MessageType.Plugin)
        self.assertEqual(self.plugin.chain.post_message.call_count, 1)
        self.assertIn("[PLUGIN]EmeTools|data:", message.buttons[0][0]["callback_data"])
        token = self.run_async(self.plugin.action(ToolAction(operation="pending")))["pending"][0]["token"]
        self.assertIn(token, message.buttons[0][0]["callback_data"])
        data = {"channel": NotificationChannel.Telegram, "source": "安全 Bot", "userid": "123",
                "original_chat_id": "123"}
        with patch.object(self.plugin, "_telegram_confirmation_sources", return_value={"安全 Bot": {"TELEGRAM_ADMINS": "123"}}), \
             patch("emetools.matches_channel_admin", side_effect=lambda _, cfg, uid: bool(cfg) and cfg.get("TELEGRAM_ADMINS") == uid):
            self.plugin._handle_scheduled_confirmation(token, True, {**data, "original_chat_id": "456"})
            self.plugin._handle_scheduled_confirmation(token, True, {**data, "source": "其他 Bot"})
            self.assertTrue(orphan.exists())
            self.assertEqual(self.plugin.chain.post_message.call_count, 1)
            self.plugin._handle_scheduled_confirmation(token, True, data)
            self.assertFalse(orphan.exists())
            reply = self.plugin.chain.post_message.call_args.args[0]
            self.assertEqual(reply.source, "安全 Bot")
            self.assertEqual(reply.userid, "123")
            self.assertEqual(self.plugin.chain.post_message.call_count, 2)
            self.plugin._handle_scheduled_confirmation(token, True, data)
            self.assertEqual(self.plugin.chain.post_message.call_count, 3)

    def test_telegram_cancel_consumes_token_and_keeps_file(self):
        orphan = Path(self.directory.name) / "orphan.nfo"
        orphan.write_text("metadata", encoding="utf-8")
        self.plugin._schedule["tools"]["confirm_mode"] = "telegram"
        token = self.plugin._remember("scheduled_tools", self.plugin._cleaner.scan(self.directory.name))
        with patch.object(self.plugin, "_telegram_confirmation_sources", return_value={"安全 Bot": {"TELEGRAM_ADMINS": "123"}}), \
             patch("emetools.matches_channel_admin", return_value=True):
            self.plugin._handle_scheduled_confirmation(token, False, {"channel": NotificationChannel.Telegram,
                "source": "安全 Bot", "userid": "123", "original_chat_id": "123"})
        self.assertTrue(orphan.exists())
        self.assertEqual(self.run_async(self.plugin.action(ToolAction(operation="pending")))["pending"], [])

    def test_telegram_channel_disappears_keeps_page_confirmation(self):
        orphan = Path(self.directory.name) / "orphan.nfo"
        orphan.write_text("metadata", encoding="utf-8")
        self.plugin._schedule["tools"].update({"enabled": True, "path": self.directory.name,
                                                "auto_delete": True, "confirm_mode": "telegram"})
        with patch.object(self.plugin, "_telegram_confirmation_sources", return_value={}):
            self.plugin._run_scheduled("tools")
        self.assertTrue(orphan.exists())
        self.assertEqual(len(self.run_async(self.plugin.action(ToolAction(operation="pending")))["pending"]), 1)
        self.assertIsNone(self.plugin.chain.post_message.call_args.args[0].buttons)

    def test_telegram_send_failure_keeps_page_confirmation(self):
        orphan = Path(self.directory.name) / "orphan.nfo"
        orphan.write_text("metadata", encoding="utf-8")
        self.plugin._schedule["tools"].update({"enabled": True, "path": self.directory.name,
                                                "auto_delete": True, "confirm_mode": "telegram"})
        with patch.object(self.plugin, "_telegram_confirmation_sources", return_value={"安全 Bot": {}}), \
             patch.object(self.plugin, "_send_scheduled_telegram_confirmation", side_effect=RuntimeError("send failed")):
            self.plugin._run_scheduled("tools")
        self.assertTrue(orphan.exists())
        self.assertEqual(len(self.run_async(self.plugin.action(ToolAction(operation="pending")))["pending"]), 1)
        self.assertEqual(self.plugin.chain.post_message.call_count, 1)

    def test_telegram_mode_requires_admin_plugin_bot(self):
        with patch("emetools.get_service_configs", return_value=[]), self.assertRaises(HTTPException):
            self.run_async(self.plugin.save_schedule(ScheduleChange(section="tools", settings={
                "enabled": True, "path": self.directory.name, "auto_delete": True, "confirm_mode": "telegram"})))

    def test_legacy_confirmation_setting_preserves_moviepilot_mode(self):
        legacy = object.__new__(EmeTools)
        legacy.get_config = MagicMock(return_value={})
        legacy.get_data = MagicMock(return_value=None)
        legacy.save_data = MagicMock()
        with patch('emetools.missing_episodes.SubscribeChain', return_value=MagicMock()), patch('emetools.missing_episodes.MediaServerHelper', return_value=MagicMock()):
            legacy.init_plugin({"strm_root": self.directory.name, "schedule": {"tools": {"confirm_cleanup": True}}})
        self.assertEqual(legacy._schedule["tools"]["confirm_mode"], "moviepilot")

    def test_cleanup_aborts_if_remote_listing_changes(self):
        self.plugin.get_config.return_value = {"cookies": "test"}
        self.plugin._schedule["p115_cleanup"]["dir_ids"] = ["123"]
        client = MagicMock()
        client.__enter__.return_value = client
        client.list_files_in_dir.return_value = [{"fid": "old", "size": 4}]
        client.list_children.return_value = ([], [])
        self.plugin._client = lambda: client
        requested = self.run_async(self.plugin.action(ToolAction(operation="cleanup_request")))
        self.assertTrue(requested["pending"])
        client.list_files_in_dir.return_value = [{"fid": "new", "size": 4}]
        confirmed = self.run_async(self.plugin.action(ToolAction(
            operation="cleanup_confirm", token=requested["token"])))
        self.assertFalse(confirmed["ok"])
        client.delete_files.assert_not_called()

    def test_trash_requires_matching_preview_token(self):
        self.plugin.get_config.return_value = {"cookies": "test"}
        client = MagicMock()
        client.__enter__.return_value = client
        client.rb_list.side_effect = [{"count": 1, "items": []}, {"count": 2, "items": []}]
        self.plugin._client = lambda: client
        info = self.run_async(self.plugin.action(ToolAction(operation="trash_info")))
        result = self.run_async(self.plugin.action(ToolAction(operation="trash_clear", token=info["token"])))
        self.assertFalse(result["ok"])
        client.clear_recyclebin.assert_not_called()


if __name__ == "__main__":
    unittest.main()
