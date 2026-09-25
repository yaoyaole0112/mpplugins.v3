import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from emetools import EmeTools, ToolAction
from emetools import tool_notifications as notices
from app.schemas.types import MessageType, NotificationChannel


class NotificationTemplateTests(unittest.TestCase):
    def test_invalid_cleanup_and_confirmation_match_eme_except_real_quarantine_path(self):
        title, text = notices.invalid_cleanup({"deleted": [1, 2], "failed": [1]}, "/strm")
        self.assertEqual(title, "📃 清理无效数据")
        self.assertEqual(text, "━━━━━━━━━━━━━━━\n✅ 已清理 2 项无效媒体数据，跳过 1 项。\n📁 隔离目录：/strm/.mp-emetools-trash")
        title, text = notices.invalid_confirmation({"root": "/strm", "items": [{"path": "/strm/old.nfo"}]})
        self.assertEqual(title, "⚠️【清理无效数据】待确认")
        self.assertIn("待清理：1 项", text)
        self.assertIn("• old.nfo", text)

    def test_file_cleanup_empty_success_failure_and_details(self):
        self.assertIsNone(notices.file_cleanup({"deleted": 0, "dir_count": 0}, {"folders": [{"name": "空目录", "error": ""}]}))
        title, text = notices.file_cleanup(
            {"deleted": 2, "dir_count": 1, "folders": [{"name": "电影", "files": 2, "dirs": 1, "size": 1024, "error": ""}]},
            {"folders": [{"name": "电影", "error": ""}]})
        self.assertEqual(title, "🗑 清理文件")
        self.assertEqual(text, "━━━━━━━━━━━━━━━\n✅已删除：2 个文件 + 1 个文件夹（1.0 KB）\n\n📁电影：2 个文件 + 1 个文件夹（1.0 KB）")
        self.assertIn("📁坏目录：读取失败", notices.file_cleanup({}, {"folders": [{"name": "坏目录", "error": "读取失败"}]})[1])

    def test_trash_and_move_templates(self):
        self.assertIsNone(notices.empty_trash({"count": 0}))
        self.assertEqual(notices.empty_trash({"count": 3, "size_bytes": 2048}),
                         ("🧹 清空115 回收站", "━━━━━━━━━━━━━━━\n📁 已清空 3 个文件 · 💾 已释放 2.0 KB\n⚠️ 彻底删除不可恢复！"))
        self.assertIsNone(notices.file_move({"moved": 0, "errors": []}))
        title, text = notices.file_move({"moved": 1, "errors": ["失败"], "details": [
            {"status": "success", "src_name": "源目录", "dst_name": "目标目录", "file_count": 3, "total_bytes": 512}]})
        self.assertEqual(title, "📁 文件转存")
        self.assertEqual(text, "━━━━━━━━━━━━━━━\n🎯[源] 源目录 → [目标] 目标目录\n📚数量：3 个 · 💾大小：512.0 B\n\n⚠️ 转存失败：1 个目录，请查看日志")


class ScheduledNotificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.plugin = object.__new__(EmeTools)
        self.plugin.get_config = MagicMock(return_value={})
        self.plugin.get_data = MagicMock(return_value=None)
        self.plugin.save_data = MagicMock()
        with patch('emetools.missing_episodes.SubscribeChain', return_value=MagicMock()), patch('emetools.missing_episodes.MediaServerHelper', return_value=MagicMock()):
            self.plugin.init_plugin({"strm_root": directory.name})
        self.plugin.get_config = MagicMock(return_value={"cookies": "test"})
        self.plugin.chain = MagicMock()

    @property
    def sent(self):
        return self.plugin.chain.post_message

    @property
    def last_message(self):
        return self.sent.call_args.args[0]

    def run_job(self, section):
        self.plugin._schedule[section]["enabled"] = True
        self.plugin._run_scheduled(section)

    def test_idle_move_never_notifies_but_success_and_failure_do(self):
        self.plugin._schedule["p115_move"]["rules"] = [{"src_id": "1", "dst_id": "2"}]
        self.plugin._move_run = MagicMock(return_value={"ok": True, "moved": 0, "errors": [], "details": []})
        self.run_job("p115_move")
        self.run_job("p115_move")
        self.sent.assert_not_called()
        self.plugin._move_run.return_value = {"ok": True, "moved": 1, "errors": [], "details": [
            {"status": "success", "src_name": "待转存", "dst_name": "媒体库", "file_count": 1, "total_bytes": 1024}]}
        self.run_job("p115_move")
        self.sent.assert_called_once()
        self.assertEqual(self.last_message.title, "📁 文件转存")
        self.assertEqual(self.last_message.channel, NotificationChannel.Telegram)
        self.assertEqual(self.last_message.mtype, MessageType.Plugin)
        self.assertIsNone(self.last_message.link)
        self.plugin._move_run.return_value = {"ok": False, "moved": 0, "errors": ["读取失败"], "details": []}
        self.run_job("p115_move")
        self.assertIn("⚠️ 转存失败：1 个目录", self.last_message.text)

    def test_empty_cleanup_and_trash_are_silent(self):
        self.plugin._cleanup_preview = MagicMock(return_value={"ok": True, "folders": [],
                                                        "file_count": 0, "dir_count": 0})
        self.plugin._cleanup_confirm = MagicMock()
        self.run_job("p115_cleanup")
        self.plugin._cleanup_confirm.assert_not_called()
        self.plugin._trash_info = MagicMock(return_value={"count": 0, "token": "unused", "size_bytes": 0})
        self.plugin._trash_clear = MagicMock()
        self.run_job("p115_trash")
        self.plugin._trash_clear.assert_not_called()
        self.sent.assert_not_called()

    def test_partial_cleanup_error_and_trash_result_are_reported(self):
        self.plugin._cleanup_preview = MagicMock(return_value={"ok": True, "folders": [
            {"name": "失败目录", "error": "读取失败", "files": 0, "dirs": 0}], "file_count": 0, "dir_count": 0})
        self.run_job("p115_cleanup")
        self.assertEqual(self.last_message.title, "🗑 清理文件")
        self.plugin._trash_info = MagicMock(return_value={"count": 2, "size_bytes": 512, "token": "token"})
        self.plugin._trash_clear = MagicMock(return_value={"ok": True, "count": 2})
        self.run_job("p115_trash")
        self.assertEqual(self.last_message.title, "🧹 清空115 回收站")
        self.plugin._trash_clear.return_value = {"ok": False, "message": "密码错误"}
        self.run_job("p115_trash")
        self.assertEqual(self.last_message.text, "⏰ 115 回收站定时清空失败：密码错误")

    def test_invalid_scan_only_notifies_after_cleanup_or_confirmation(self):
        (self.root / "orphan.nfo").write_text("old", encoding="utf-8")
        schedule = self.plugin._schedule["tools"]
        schedule.update(path=str(self.root), auto_delete=False)
        self.run_job("tools")
        self.sent.assert_not_called()
        schedule["auto_delete"] = True
        schedule["confirm_cleanup"] = True
        schedule["confirm_mode"] = "moviepilot"
        self.run_job("tools")
        self.assertEqual(self.last_message.title, "⚠️【清理无效数据】待确认")
        token = self.plugin._pending_info()["pending"][0]["token"]
        self.plugin._confirm_scheduled_tools(token)
        self.assertEqual(self.last_message.title, "📃 清理无效数据")
        self.assertEqual(self.plugin._pending_info()["pending"], [])
        self.run_job("tools")
        self.assertEqual(self.sent.call_count, 2)

    def test_move_nested_file_counts_follow_eme(self):
        self.plugin._schedule["p115_move"]["rules"] = [{"src_id": "1", "src_name": "源", "dst_id": "2", "dst_name": "目标"}]
        client = MagicMock()
        client.list_children.side_effect = [
            ([{"fid": "11", "size": 100}], [{"cid": "12", "size": 0}]),
            ([{"fid": "13", "size": 200}], [{"cid": "14", "size": 0}]),
            ([{"fid": "15", "size": 300}], []),
        ]
        client.move_files.return_value = {"state": True}
        context = MagicMock()
        context.__enter__.return_value = client
        self.plugin._client = MagicMock(return_value=context)
        result = self.plugin._move_run()
        self.assertEqual(result["moved"], 2)
        self.assertEqual(result["details"][0]["file_count"], 3)
        self.assertEqual(result["details"][0]["total_bytes"], 600)

    def test_manual_file_cleanup_confirmation_and_result(self):
        preview = {"ok": True, "folders": [{"name": "旧文件", "files": 1, "dirs": 0,
                                               "size": 1024, "error": ""}],
                   "file_count": 1, "dir_count": 0, "total_bytes": 1024,
                   "snapshots": {"111": {"files": ["123"], "dirs": [], "name": "旧文件"}}}
        self.plugin._schedule["p115_cleanup"]["dir_ids"] = ["111"]
        self.plugin._cleanup_preview = MagicMock(return_value=preview)
        requested = self.plugin._cleanup_request()
        self.assertTrue(requested["pending"])
        self.assertEqual(self.last_message.title, "⚠️【清理文件】待确认")
        self.plugin._cleanup_confirm = MagicMock(return_value={"ok": True, "deleted": 1,
            "dir_count": 0, "folders": [{"name": "旧文件", "files": 1, "dirs": 0, "size": 1024, "error": ""}]})
        result = asyncio.run(self.plugin.action(ToolAction(operation="cleanup_confirm", token=requested["token"])))
        self.assertTrue(result["ok"])
        self.assertEqual(self.last_message.title, "🗑 清理文件")

    def test_manual_trash_and_invalid_confirmation_follow_same_templates(self):
        self.plugin._trash_clear = MagicMock(return_value={"ok": True, "count": 3, "size_bytes": 1024})
        asyncio.run(self.plugin.action(ToolAction(operation="trash_clear", token="test")))
        self.assertEqual(self.last_message.title, "🧹 清空115 回收站")
        self.plugin._trash_clear.return_value = {"ok": True, "message": "回收站为空"}
        asyncio.run(self.plugin.action(ToolAction(operation="trash_clear", token="test")))
        self.assertEqual(self.sent.call_count, 1)
        (self.root / "orphan.nfo").write_text("old", encoding="utf-8")
        snapshot = self.plugin._cleaner.start_scan(str(self.root))
        request = asyncio.run(self.plugin.action(ToolAction(
            operation="request_delete", scan_token=snapshot["scan_token"],
            paths=[str(self.root / "orphan.nfo")], path=str(self.root))))
        self.assertEqual(self.last_message.title, "⚠️【清理无效数据】待确认")
        asyncio.run(self.plugin.action(ToolAction(operation="confirm_delete", token=request["token"])))
        self.assertEqual(self.last_message.title, "📃 清理无效数据")


if __name__ == "__main__":
    unittest.main()
