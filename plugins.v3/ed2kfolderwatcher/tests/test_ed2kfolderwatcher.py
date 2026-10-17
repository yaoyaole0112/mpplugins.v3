import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class Ed2kTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        modules = {name: types.ModuleType(name) for name in ("app", "app.log", "app.plugins")}
        modules["app"].__path__ = []
        modules["app.log"].logger = Mock()
        modules["app.plugins"]._PluginBase = type("PluginBase", (), {})
        plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
        spec = importlib.util.spec_from_file_location("ed2k_test_plugin", plugin_path)
        cls.module = importlib.util.module_from_spec(spec)
        modules["ed2k_test_plugin"] = cls.module
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(cls.module)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "media"
        self.folder.mkdir()
        self.plugin = self.module.Ed2kFolderWatcher()
        self.plugin.get_data_path = lambda: self.root / "data"
        self.plugin.update_config = Mock()
        self.plugin.init_plugin({"folder": str(self.folder), "min_age": 0})
        self.addCleanup(self.plugin.stop_service)

    def create(self, name="test.bin", data=b"hello"):
        path = self.folder / name
        path.write_bytes(data)
        return path

    def records(self):
        return json.loads((self.root / "data" / "records.json").read_text())

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("worker timeout")

    def test_md4_vectors_and_fallback(self):
        for data, expected in [(b"", "31d6cfe0d16ae931b73c59d7e0c089c0"), (b"abc", "a448017aaf21d8525fc10ae87aa6729d")]:
            self.assertEqual(self.module._md4(data).hex(), expected)
            path = self.create(data=data)
            with patch.object(self.module, "_CryptoMD4", None):
                self.assertEqual(self.module.ed2k_hash(path), expected)

    def test_telegram_destination_formats(self):
        for value, expected in [(" 12345 ", "12345"), ("-10012345", "-10012345"), ("receiver_bot", "@receiver_bot"), ("@receiver_bot", "@receiver_bot"), ("https://t.me/receiver_bot", "@receiver_bot"), ("t.me/my_channel/", "@my_channel")]:
            with self.subTest(value=value):
                self.assertEqual(self.module.telegram_destination(value), expected)
        for value in ("https://t.me/+invite", "https://t.me/channel/123", "https://example.com/receiver", "", "two names"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.module.telegram_destination(value)

    def test_telegram_error_hints(self):
        error = RuntimeError("Bad Request: chat not found")
        hint = self.module.telegram_error_hint(error)
        self.assertIn("/start", hint)
        self.assertIn("@用户名", hint)
        self.assertIn("Bot-to-Bot", hint)
        error.error_code = 403
        error.description = "Forbidden"
        self.assertIn("频道发消息权限", self.module.telegram_error_hint(error))
        error.error_code = 401
        self.assertIn("Token 无效", self.module.telegram_error_hint(error))

    def test_telegram_chat_error_cooldown_and_token_redaction(self):
        self.plugin._telegram_enabled = True
        self.plugin._telegram_token = "private-token"
        self.plugin._telegram_chat_id = "12345"
        error = RuntimeError("chat not found private-token")
        error.error_code = 400
        client = Mock()
        client.send_message.side_effect = error
        settings = types.ModuleType("app.runtime.settings")
        settings.get_runtime_setting = Mock(return_value=None)
        with patch.object(self.module, "TelegramBotApi", return_value=client), patch.dict(sys.modules, {"app.runtime.settings": settings}):
            self.assertFalse(self.plugin._send_telegram(Path("test.mkv"), "link"))
            warning = self.module.logger.warning.call_args.args[0]
            self.assertNotIn("private-token", warning)
            self.assertIn("300秒", warning)
            self.assertFalse(self.plugin._send_telegram(Path("second.mkv"), "link"))
        client.send_message.assert_called_once()
        client.close.assert_called_once()

    def test_telegram_sends_to_normalized_username(self):
        self.plugin._telegram_enabled = True
        self.plugin._telegram_token = "private-token"
        self.plugin._telegram_chat_id = "https://t.me/receiver_bot"
        client = Mock()
        settings = types.ModuleType("app.runtime.settings")
        settings.get_runtime_setting = Mock(return_value=None)
        with patch.object(self.module, "TelegramBotApi", return_value=client), patch.dict(sys.modules, {"app.runtime.settings": settings}):
            self.assertTrue(self.plugin._send_telegram(Path("test.mkv"), "link"))
        self.assertEqual(client.send_message.call_args.args[0], "@receiver_bot")
        client.close.assert_called_once()

    def test_user_mode_no_bot_token_or_bot_fallback(self):
        self.plugin._telegram_enabled = True
        self.plugin._telegram_mode = "user"
        self.plugin._telegram_token = ""
        self.plugin._telegram_chat_id = "@receiver_bot"
        account = Mock()
        user_module = types.ModuleType("ed2k_test_plugin.user_account")
        user_module.TelegramUserAccount = Mock(return_value=account)
        user_module.UserAccountError = type("UserAccountError", (Exception,), {})
        settings = types.ModuleType("app.runtime.settings")
        settings.get_runtime_setting = Mock(return_value=None)
        with patch.dict(sys.modules, {"ed2k_test_plugin.user_account": user_module, "app.runtime.settings": settings}), patch.object(self.module, "TelegramBotApi", side_effect=AssertionError("bot fallback")):
            self.assertTrue(self.plugin._send_telegram(Path("中文.mkv"), "pure-link"))
            account.send.assert_called_once_with("@receiver_bot", "pure-link", thread_id=None)
            account.send.side_effect = user_module.UserAccountError("请先登录")
            self.assertFalse(self.plugin._send_telegram(Path("中文.mkv"), "pure-link"))
            self.assertFalse(self.plugin._send_telegram(Path("中文.mkv"), "pure-link"))
            self.assertEqual(account.send.call_count, 2)

    def test_user_mode_saved_without_auth_secrets(self):
        self.plugin._telegram_mode = "user"
        self.plugin._save_config()
        saved = self.plugin.update_config.call_args.args[0]
        self.assertEqual(saved["telegram_mode"], "user")
        self.assertNotIn("telegram_user_api_hash", saved)
        self.assertNotIn("telegram_user_session", saved)

    def test_user_target_dedup_separate_from_bot_and_account(self):
        self.plugin._telegram_token = "bot-token"
        self.plugin._telegram_chat_id = "@receiver_bot"
        bot_key = self.plugin._telegram_target_key()
        account = Mock()
        account.identity.return_value = "first-user"
        user_module = types.ModuleType("ed2k_test_plugin.user_account")
        user_module.TelegramUserAccount = Mock(return_value=account)
        self.plugin._telegram_mode = "user"
        with patch.dict(sys.modules, {"ed2k_test_plugin.user_account": user_module}):
            first_key = self.plugin._telegram_target_key()
            self.assertNotEqual(bot_key, first_key)
            self.plugin._telegram_token = "unrelated-token"
            self.assertEqual(first_key, self.plugin._telegram_target_key())
            account.identity.return_value = "second-user"
            self.assertNotEqual(first_key, self.plugin._telegram_target_key())

    def test_link_preserves_chinese_and_escapes_delimiters(self):
        path = self.create("大哥小助理.母带.100% |测试.mkv")
        link = self.module.ed2k_link(path, "a" * 32)
        self.assertEqual(link, f"ed2k://|file|大哥小助理.母带.100%25 %7C测试.mkv|5|{'a' * 32}|/")
        self.assertEqual(len(link.split("|")), 6)
        self.assertEqual(self.module.ed2k_filename("中文\n\u2028.mkv"), "中文%0A%E2%80%A8.mkv")

    def test_filename_preserves_spaces_and_punctuation(self):
        name = "花开锦绣 - S01E10 - 第 10 集 （国语）(2026) [2160p]【母带】，。！.mkv"
        self.assertEqual(self.module.ed2k_filename(name), name)
        self.assertEqual(self.module.ed2k_filename("分隔|百分%?#/\\\t.mkv"), "分隔%7C百分%25%3F%23%2F%5C%09.mkv")

    def test_upgrade_rewrites_escaped_spaces_and_parentheses(self):
        path = self.create("花开锦绣 - 第 10 集 (2026)（国语）.mkv")
        self.plugin._process_file(path)
        records = self.records()
        record = records[str(path)]
        record["link"] = f"ed2k://|file|{self.module.quote(path.name, safe='')}|5|{'a' * 32}|/"
        record["telegram_sent"] = True
        record["telegram_target"] = "previous-target"
        self.plugin._write_results(self.folder / "ed2k.txt", self.root / "data" / "records.json", records)
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("rehashed")), patch.object(self.plugin, "_notify_record", side_effect=AssertionError("resent")):
            self.plugin._refresh_link_names()
        updated = self.records()[str(path)]
        self.assertEqual(updated["link"], f"ed2k://|file|{path.name}|5|{'a' * 32}|/")
        self.assertTrue(updated["telegram_sent"])
        self.assertEqual(updated["telegram_target"], "previous-target")
        self.assertEqual(updated["link"], (self.folder / "ed2k.txt").read_text().strip())

    def test_upgrade_rewrites_old_links_without_hashing_or_sending(self):
        path = self.create("大哥小助理.%E4%B8%AD.mkv")
        self.plugin._process_file(path)
        path.unlink()
        records = self.records()
        record = records[str(path)]
        record["link"] = f"ed2k://|file|{self.module.quote(path.name, safe='')}|5|{'a' * 32}|/"
        record["telegram_sent"] = True
        record["telegram_target"] = "previous-target"
        self.plugin._write_results(self.folder / "ed2k.txt", self.root / "data" / "records.json", records)
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("rehashed")), patch.object(self.plugin, "_send_telegram", side_effect=AssertionError("resent")):
            self.plugin._refresh_link_names()
        updated = self.records()[str(path)]
        self.assertIn("大哥小助理.%25E4%25B8%25AD.mkv", updated["link"])
        self.assertTrue(updated["telegram_sent"])
        self.assertEqual(updated["telegram_target"], "previous-target")
        self.assertEqual(updated["link"], (self.folder / "ed2k.txt").read_text().strip())

    def test_upgrade_respects_txt_disabled(self):
        path = self.create("中文.mkv")
        self.plugin._process_file(path)
        records = self.records()
        records[str(path)]["link"] = f"ed2k://|file|%E4%B8%AD%E6%96%87.mkv|5|{'a' * 32}|/"
        output = self.folder / "ed2k.txt"
        cache = self.root / "data" / "records.json"
        self.plugin._write_results(output, cache, records)
        previous = output.read_text()
        self.plugin._output_enabled = False
        self.plugin._refresh_link_names()
        self.assertIn("中文.mkv", self.records()[str(path)]["link"])
        self.assertEqual(output.read_text(), previous)

    def test_boundary_hashes(self):
        from Crypto.Hash import MD4
        part_size = self.module.ED2K_PART_SIZE
        rhash_vectors = {
            0: "31d6cfe0d16ae931b73c59d7e0c089c0",
            1: "bde52cb31de33e46245e05fbdbd6fb24",
            9727999: "5461275e76837a313a0b2f67811c1023",
            9728000: "ee15063dd1e9c5bd5c0e4205c0b8e698",
            9728001: "748c0171a2d42d28afb644ef3e17f4e7",
            19456000: "fcca57f6ae31dcfa2ce0e41119738eb1",
        }
        for size in (0, 1, part_size - 1, part_size, part_size + 1, 2 * part_size):
            with self.subTest(size=size):
                data = b"a" * size
                if size < part_size:
                    expected = MD4.new(data).hexdigest()
                else:
                    digests = [MD4.new(data[offset:offset + part_size]).digest() for offset in range(0, size, part_size)]
                    if size % part_size == 0:
                        digests.append(MD4.new().digest())
                    expected = MD4.new(b"".join(digests)).hexdigest()
                self.assertEqual(self.module.ed2k_hash(self.create(data=data)), expected)
                self.assertEqual(expected, rhash_vectors[size])

    def test_incremental_and_history_after_removal(self):
        first = self.create()
        self.plugin._process_file(first)
        first.unlink()
        second = self.create("second.bin", b"new")
        self.plugin._process_file(second)
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("rehashed")):
            self.plugin._process_file(second)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(len((self.folder / "ed2k.txt").read_text().splitlines()), 2)

    def test_txt_disabled_and_legacy_migration(self):
        self.plugin._output_enabled = False
        path = self.create()
        legacy = self.folder / ".ed2k.txt.cache.json"
        legacy.write_text(json.dumps({"old": {"link": "old-link"}}))
        self.plugin._process_file(path)
        self.assertFalse((self.folder / "ed2k.txt").exists())
        self.assertIn("old", self.records())
        self.plugin._output = str(self.root / "different" / "links.txt")
        self.assertEqual(len(self.records()), 2)

    def test_self_outputs_and_symlinks_excluded(self):
        path = self.create()
        self.plugin._process_file(path)
        self.assertFalse(self.plugin._eligible(self.folder / "ed2k.txt"))
        self.assertFalse(self.plugin._eligible(self.create(".ed2k-write-test")))
        self.assertFalse(self.plugin._eligible(self.create("download.part")))
        symbolic = self.folder / "symbolic.bin"
        symbolic.symlink_to(path)
        self.assertFalse(self.plugin._eligible(symbolic))

    def test_safety_no_destination_no_delete(self):
        self.plugin._output_enabled = False
        self.plugin._delete_source = True
        path = self.create()
        self.plugin._process_file(path)
        self.assertTrue(path.exists())

    def test_delete_only_after_success(self):
        self.plugin._delete_source = True
        path = self.create()
        self.plugin._process_file(path)
        self.assertFalse(path.exists())
        self.assertEqual(len(self.records()), 1)
        self.assertIn("ed2k://", (self.folder / "ed2k.txt").read_text())

    def test_write_failure_preserves_source_and_cache(self):
        path = self.create()
        self.plugin._delete_source = True
        original = self.plugin._atomic_write

        def fail_txt(target, text):
            if target.suffix == ".txt":
                raise OSError("disk full")
            return original(target, text)

        with patch.object(self.plugin, "_atomic_write", side_effect=fail_txt):
            with self.assertRaises(OSError):
                self.plugin._process_file(path)
        self.assertTrue(path.exists())
        self.assertEqual(len(self.records()), 1)

    def test_telegram_failure_retries_without_rehash(self):
        path = self.create()
        self.plugin._telegram_enabled = True
        self.plugin._delete_source = True
        with patch.object(self.plugin, "_send_telegram", return_value=False):
            self.plugin._process_file(path)
        self.assertTrue(path.exists())
        self.assertIn(str(path), self.plugin._pending)
        with patch.object(self.module, "ed2k_hash", side_effect=AssertionError("rehashed")), patch.object(self.plugin, "_send_telegram", return_value=True):
            self.plugin._process_file(path)
        self.assertFalse(path.exists())

    def test_change_during_notification_prevents_delete(self):
        path = self.create()
        self.plugin._telegram_enabled = True
        self.plugin._delete_source = True

        def change(*args):
            path.write_bytes(b"replacement content")
            return True

        with patch.object(self.plugin, "_send_telegram", side_effect=change):
            self.plugin._process_file(path)
        self.assertTrue(path.exists())

    def test_change_during_hash_requeues(self):
        path = self.create()

        def change(*args):
            path.write_bytes(b"different")
            return "0" * 32

        with patch.object(self.module, "ed2k_hash", side_effect=change):
            self.plugin._process_file(path)
        self.assertIn(str(path), self.plugin._pending)
        self.assertFalse((self.folder / "ed2k.txt").exists())

    def test_corrupt_cache_not_silently_overwritten(self):
        path = self.create()
        data = self.root / "data"
        data.mkdir()
        (data / "records.json").write_text("broken")
        with self.assertRaises(ValueError):
            self.plugin._process_file(path)
        self.assertEqual((data / "records.json").read_text(), "broken")

    def test_realtime_create_and_move_directory(self):
        if self.module.Observer is None:
            self.skipTest("watchdog not installed")
        self.plugin.init_plugin({"enabled": True, "folder": str(self.folder), "min_age": 0})
        self.assertEqual(self.plugin.get_service(), [])
        path = self.create()
        self.wait_for(lambda: (self.root / "data" / "records.json").exists())
        self.assertIn(str(path), self.records())
        staged = self.root / "staged"
        staged.mkdir()
        (staged / "moved.bin").write_bytes(b"moved")
        staged.rename(self.folder / "arrived")
        self.wait_for(lambda: len(self.records()) == 2)

    def test_stop_cancels_pending_tasks(self):
        self.plugin.init_plugin({"enabled": True, "folder": str(self.folder), "min_age": 60})
        self.create()
        self.plugin.stop_service()
        self.assertFalse(self.plugin._worker)
        self.assertFalse((self.folder / "ed2k.txt").exists())

    def test_growing_file_event_is_delayed(self):
        self.plugin.init_plugin({"enabled": True, "monitor_enabled": False, "folder": str(self.folder), "min_age": 0})
        path = self.create()
        self.plugin._queue_event(path)
        time.sleep(0.5)
        path.write_bytes(b"still downloading")
        time.sleep(0.7)
        self.assertFalse((self.folder / "ed2k.txt").exists())
        self.wait_for(lambda: (self.folder / "ed2k.txt").exists())
        self.assertEqual(self.records()[str(path)]["size"], len(b"still downloading"))

    def test_atomic_replace_failure_preserves_old_txt(self):
        output = self.folder / "ed2k.txt"
        output.write_text("previous result")
        with patch.object(self.module.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.plugin._atomic_write(output, "new result")
        self.assertEqual(output.read_text(), "previous result")
        self.assertFalse(list(self.folder.glob(".ed2k-write-*")))

    def test_txt_disabled_does_not_modify_existing_file(self):
        output = self.folder / "ed2k.txt"
        output.write_text("keep unchanged")
        self.plugin._output_enabled = False
        self.plugin._process_file(self.create())
        self.assertEqual(output.read_text(), "keep unchanged")

    def test_invalid_extension_and_nonrecursive(self):
        path = self.create()
        self.plugin._extensions = "mkv,mp4"
        self.assertFalse(self.plugin._eligible(path))
        child = self.folder / "child"
        child.mkdir()
        nested = child / "test.mkv"
        nested.write_bytes(b"data")
        self.plugin._recursive = False
        self.assertFalse(self.plugin._eligible(nested))


if __name__ == "__main__":
    unittest.main()
