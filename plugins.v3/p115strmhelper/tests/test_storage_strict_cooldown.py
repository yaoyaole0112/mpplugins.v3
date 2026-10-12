import ast
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from typing import Optional
from unittest import TestCase
from unittest.mock import Mock


class StorageQueryError(Exception):
    pass


class TestStorageStrictCooldown(TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[2] / "p115disk/p115_api.py"
        methods = [
            node for node in ast.walk(ast.parse(source.read_text()))
            if isinstance(node, ast.FunctionDef)
            and node.name in ("get_item_strict", "_get_item_strict_once")
        ]
        self.now = 100.0
        self.sleep = Mock(side_effect=self.advance)
        namespace = {
            "Path": Path, "Optional": Optional, "FileItem": object,
            "StorageQueryError": StorageQueryError, "logger": Mock(),
            "monotonic": lambda: self.now, "sleep": self.sleep,
        }
        module = ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[]))
        exec(compile(module, str(source), "exec"), namespace)
        self.strict = namespace["get_item_strict"]
        self.once = namespace["_get_item_strict_once"]
        self.api = SimpleNamespace(
            _get_cached_item=Mock(return_value=None),
            _strict_query_lock=RLock(), _all_channel_cooldown_until=0,
            _get_item_strict_once=Mock(return_value="target"),
            _is_405=lambda error: "405" in str(error),
            _set_all_channel_cooldown=Mock(side_effect=self.cooldown),
            _get_item_rate_limiter=Mock(), _query_item=Mock(),
            _note_405=Mock(), _fs_search_fallback=Mock(return_value=None),
            _get_u115_item=Mock(side_effect=StorageQueryError("Open unavailable")),
        )
        self.path = Path("/target/episode.mp4")

    def advance(self, seconds):
        self.now += seconds

    def cooldown(self):
        self.api._all_channel_cooldown_until = self.now + 60

    def test_cached_target_does_not_wait(self):
        self.cooldown()
        self.api._get_cached_item.return_value = "cached"
        self.assertEqual(self.strict(self.api, self.path), "cached")
        self.sleep.assert_not_called()

    def test_cooldown_waits_before_query(self):
        self.cooldown()
        self.assertEqual(self.strict(self.api, self.path), "target")
        self.sleep.assert_called_once_with(60)
        self.api._get_item_strict_once.assert_called_once_with(self.path)

    def test_405_retries_once_after_cooldown(self):
        self.api._get_item_strict_once.side_effect = [StorageQueryError("405"), "target"]
        self.assertEqual(self.strict(self.api, self.path), "target")
        self.sleep.assert_called_once_with(60)
        self.assertEqual(self.api._get_item_strict_once.call_count, 2)

    def test_persistent_405_remains_unknown(self):
        self.api._get_item_strict_once.side_effect = StorageQueryError("405")
        with self.assertRaises(StorageQueryError):
            self.strict(self.api, self.path)
        self.assertEqual(self.api._get_item_strict_once.call_count, 2)
        self.sleep.assert_called_once_with(60)

    def test_long_cooldown_does_not_wait_indefinitely(self):
        self.api._all_channel_cooldown_until = self.now + 120
        with self.assertRaises(StorageQueryError):
            self.strict(self.api, self.path)
        self.api._get_item_strict_once.assert_not_called()
        self.sleep.assert_not_called()

    def test_renewed_cooldown_respects_total_budget(self):
        self.cooldown()
        def fail_and_renew(path):
            self.cooldown()
            raise StorageQueryError("cooling")
        self.api._get_item_strict_once.side_effect = fail_and_renew
        with self.assertRaises(StorageQueryError):
            self.strict(self.api, self.path)
        self.api._get_item_strict_once.assert_called_once()
        self.sleep.assert_called_once_with(60)

    def test_non_405_failure_is_not_retried(self):
        self.api._get_item_strict_once.side_effect = StorageQueryError("timeout")
        with self.assertRaises(StorageQueryError):
            self.strict(self.api, self.path)
        self.api._get_item_strict_once.assert_called_once()
        self.sleep.assert_not_called()

    def test_confirmed_absence_returns_none(self):
        self.api._query_item.side_effect = FileNotFoundError(self.path)
        self.assertIsNone(self.once(self.api, self.path))

    def test_empty_fallback_is_not_proof_of_absence(self):
        self.api._query_item.side_effect = TimeoutError("timeout")
        with self.assertRaises(StorageQueryError):
            self.once(self.api, self.path)

    def test_lock_timeout_does_not_query(self):
        self.api._strict_query_lock = Mock()
        self.api._strict_query_lock.acquire.return_value = False
        with self.assertRaises(StorageQueryError):
            self.strict(self.api, self.path)
        self.api._get_item_strict_once.assert_not_called()
        self.api._strict_query_lock.release.assert_not_called()

    def test_lock_released_after_failure(self):
        self.api._strict_query_lock = Mock()
        self.api._get_item_strict_once.side_effect = StorageQueryError("timeout")
        with self.assertRaises(StorageQueryError):
            self.strict(self.api, self.path)
        self.api._strict_query_lock.release.assert_called_once()

    def test_open_success_bypasses_cookie_cooldown(self):
        self.cooldown()
        self.api._get_u115_item.side_effect = None
        self.api._get_u115_item.return_value = "open-target"
        self.assertEqual(self.strict(self.api, self.path), "open-target")
        self.api._get_u115_item.assert_called_once_with(self.path, strict=True)
        self.sleep.assert_not_called()
        self.api._get_item_strict_once.assert_not_called()

    def test_open_confirmed_absence_bypasses_cookie_cooldown(self):
        self.cooldown()
        self.api._get_u115_item.side_effect = FileNotFoundError(self.path)
        self.assertIsNone(self.strict(self.api, self.path))
        self.sleep.assert_not_called()
        self.api._get_item_strict_once.assert_not_called()

    def test_actual_strict_helper_uses_provider_strict_contract(self):
        source = Path(__file__).resolve().parents[2] / "p115disk/p115_api.py"
        method = next(node for node in ast.walk(ast.parse(source.read_text()))
                      if isinstance(node, ast.FunctionDef) and node.name == "_get_u115_item")
        provider = Mock()
        namespace = {"Path": Path, "Optional": Optional, "FileItem": object,
                     "U115Pan": lambda: provider, "StorageChain": Mock()}
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        exec(compile(module, str(source), "exec"), namespace)
        helper = namespace["_get_u115_item"]
        provider.get_item_strict.return_value = None
        with self.assertRaises(FileNotFoundError):
            helper(self.api, self.path, strict=True)
        provider.get_item_strict.assert_called_once_with(self.path)
        namespace["StorageChain"].assert_not_called()
        provider.get_item_strict.side_effect = StorageQueryError("network")
        with self.assertRaises(StorageQueryError):
            helper(self.api, self.path, strict=True)
