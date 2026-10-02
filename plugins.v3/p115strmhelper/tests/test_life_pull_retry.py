import ast
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock


def load_once_pull():
    source = Path(__file__).resolve().parents[1] / "helper/life/client.py"
    tree = ast.parse(source.read_text())
    monitor = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MonitorLife"
    )
    method = next(
        node for node in monitor.body
        if isinstance(node, ast.FunctionDef) and node.name == "once_pull"
    )
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    logger = Mock()
    namespace = {"logger": logger}
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["once_pull"], logger


class TestLifePullRetry(TestCase):
    def setUp(self):
        self.once_pull, self.logger = load_once_pull()
        self.monitor = Mock()
        self.monitor._life_pull_failures = 0
        self.monitor._wait_for_transfer_complete.return_value = False
        self.monitor._is_405_error.return_value = False
        self.monitor.stop_event.wait.return_value = True

    def test_timeout_retries_with_backoff_without_advancing_cursor(self):
        self.monitor._pull_life_list.side_effect = TimeoutError("life unavailable")
        for failure_count in range(1, 9):
            self.assertEqual(self.once_pull(self.monitor, 123, 456), (123, 456))
            self.assertEqual(
                self.monitor._wait_or_stop.call_args.args,
                (min(2 ** failure_count, 60),),
            )
        self.assertEqual(self.monitor._life_pull_failures, 8)

    def test_recovery_resets_backoff_even_when_no_events(self):
        self.monitor._life_pull_failures = 3
        self.monitor._pull_life_list.return_value = []
        self.assertEqual(self.once_pull(self.monitor, 123, 456), (123, 456))
        self.assertEqual(self.monitor._life_pull_failures, 0)
        self.logger.info.assert_called_once()

    def test_405_preserves_separate_cooldown(self):
        self.monitor._pull_life_list.side_effect = ValueError("405")
        self.monitor._is_405_error.return_value = True
        self.monitor.LIFE_405_COOLDOWN = 15
        self.assertEqual(self.once_pull(self.monitor, 123, 456), (123, 456))
        self.monitor._wait_or_stop.assert_called_once_with(15)
        self.assertEqual(self.monitor._life_pull_failures, 0)
