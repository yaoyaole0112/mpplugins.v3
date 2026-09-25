import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from emetools import EmeTools, ScheduleChange, ToolAction
from emetools.invalid_data import InvalidDataCleaner, QUARANTINE
from emetools.p115 import P115Client
from emetools.subscription_monitor import matches_subscription, matches_keyword, normalize_channel
from app.schemas.types import EventType, NotificationChannel


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
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.plugin = object.__new__(EmeTools)
        self.plugin.init_plugin({"strm_root": self.directory.name})
        self.plugin.update_config = MagicMock()
        self.plugin.get_config = MagicMock(return_value={})
        self.plugin.chain = MagicMock()

    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def test_bot_commands_require_explicit_user_and_never_delete_directly(self):
        commands = {item["cmd"]: item for item in self.plugin.get_command()}
        self.assertEqual(set(commands), {"/cleanup", "/cleanfiles", "/cleartrash", "/ememove"})
        self.assertTrue(all(item["event"] == EventType.PluginAction for item in commands.values()))
        self.plugin._trash_clear = MagicMock()
        self.plugin._cleanup_confirm = MagicMock()
        self.plugin.tool_command(MagicMock(event_data={"action": "emetools_cleartrash", "channel": NotificationChannel.Telegram}))
        self.plugin._trash_clear.assert_not_called()
        self.plugin._cleanup_confirm.assert_not_called()
        self.plugin.chain.post_message.assert_not_called()

    def test_old_eme_address_is_ignored(self):
        self.plugin.init_plugin({"eme_url": "http://nonexistent:7077", "strm_root": self.directory.name})
        self.assertNotIn("eme_url", str(self.run_async(self.plugin.status())))
        self.assertEqual(self.plugin.get_service(), [])

    def test_monitor_defaults_off_and_secrets_are_not_exposed(self):
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
                                                "auto_delete": True, "confirm_cleanup": True})
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
                                                "auto_delete": False, "confirm_cleanup": True})
        self.plugin.chain = MagicMock()
        self.plugin._run_scheduled("tools")
        self.assertTrue(orphan.exists())
        self.assertEqual(self.run_async(self.plugin.action(ToolAction(operation="pending")))["pending"], [])

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
