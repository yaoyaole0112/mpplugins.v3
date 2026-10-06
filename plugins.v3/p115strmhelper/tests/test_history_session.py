import ast
from inspect import signature
from pathlib import Path
from typing import List
from unittest import TestCase
from unittest.mock import Mock


def load_history_oper(base, history):
    source = Path(__file__).resolve().parents[1] / "db_manager/moviepilot_transfer.py"
    tree = ast.parse(source.read_text())
    definition = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    namespace = {
        "DbOper": base,
        "TransferHistory": history,
        "List": List,
        "signature": signature,
        "jieba_cut": lambda path, HMM: ["/媒体库/", "电影.mkv"],
    }
    module = ast.fix_missing_locations(ast.Module(body=[definition], type_ignores=[]))
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["TransferHBOper"]


class TestHistorySession(TestCase):
    def setUp(self):
        self.session = object()
        self.history = Mock()
        self.history.count_by_title = Mock(return_value=2)
        self.history.list_by_title = Mock(return_value=["first", "second"])

    def test_modern_host_opens_query_session(self):
        session = self.session

        class ModernBase:
            _db = None

            def _execute_sync_query(self, operation):
                return operation(session)

        oper = load_history_oper(ModernBase, self.history)()
        self.assertEqual(oper.get_transfer_his_by_path_title("path"), ["first", "second"])
        self.history.count_by_title.assert_called_once_with(session, title="/媒体库/%电影.mkv")
        self.history.list_by_title.assert_called_once_with(
            session, title="/媒体库/%电影.mkv", page=1, count=2
        )

    def test_legacy_host_reuses_existing_session(self):
        class LegacyBase:
            pass

        oper = load_history_oper(LegacyBase, self.history)()
        oper._db = self.session
        self.assertEqual(oper.get_transfer_his_by_path_title("path"), ["first", "second"])
        self.assertIs(self.history.count_by_title.call_args.args[0], self.session)

    def test_modern_host_preserves_explicit_session(self):
        class ModernBase:
            def _execute_sync_query(self, operation):
                return operation(self._db)

        oper = load_history_oper(ModernBase, self.history)()
        oper._db = self.session
        oper.get_transfer_his_by_path_title("path")
        self.assertIs(self.history.list_by_title.call_args.args[0], self.session)

    def test_modern_wildcard_api_receives_wildcard_flag(self):
        session = self.session

        class History:
            @staticmethod
            def count_by_title(db, title, wildcard=False):
                self.assertIs(db, session)
                self.assertTrue(wildcard)
                return 1

            @staticmethod
            def list_by_title(db, title, page, count, wildcard=False):
                self.assertIs(db, session)
                self.assertTrue(wildcard)
                self.assertEqual(count, 1)
                return ["match"]

        class LegacyBase:
            _db = session

        oper = load_history_oper(LegacyBase, History)()
        self.assertEqual(oper.get_transfer_his_by_path_title("path"), ["match"])

    def test_empty_results_skip_list_query(self):
        class LegacyBase:
            pass

        self.history.count_by_title.return_value = 0
        oper = load_history_oper(LegacyBase, self.history)()
        oper._db = self.session
        self.assertEqual(oper.get_transfer_his_by_path_title("path"), [])
        self.history.list_by_title.assert_not_called()

    def test_query_errors_are_not_swallowed(self):
        class ModernBase:
            def _execute_sync_query(self, operation):
                raise RuntimeError("database unavailable")

        oper = load_history_oper(ModernBase, self.history)()
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            oper.get_transfer_his_by_path_title("path")
