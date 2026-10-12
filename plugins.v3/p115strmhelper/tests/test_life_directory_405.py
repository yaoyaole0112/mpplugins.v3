import ast
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from urllib.error import HTTPError


class TestLifeDirectory405(TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / "helper/life/client.py"
        method = next(
            node for node in ast.walk(ast.parse(source.read_text()))
            if isinstance(node, ast.FunctionDef) and node.name == "_iter_transfer_files"
        )
        self.iterator = Mock()
        namespace = {
            "iter_files_with_path": self.iterator,
            "configer": SimpleNamespace(get_ios_ua_app=lambda: {"app": "ios"}),
            "logger": Mock(),
        }
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        exec(compile(module, str(source), "exec"), namespace)
        self.iter_files = namespace["_iter_transfer_files"]
        self.monitor = SimpleNamespace(
            _client=Mock(), _wait_or_stop=Mock(return_value=False),
            _is_405_error=lambda error: getattr(error, "code", None) == 405,
        )

    def test_android_success_does_not_retry(self):
        self.iterator.return_value = iter([{"id": 1}])
        self.assertEqual(list(self.iter_files(self.monitor, 42)), [{"id": 1}])
        self.assertEqual(self.iterator.call_args.kwargs["app"], "android")
        self.assertEqual(self.iterator.call_args.kwargs["max_workers"], 0)
        self.monitor._wait_or_stop.assert_not_called()

    def test_partial_failure_deduplicates_fallback(self):
        def interrupted():
            yield {"id": 1}
            raise HTTPError("https://example.com", 405, "blocked", {}, None)
        self.iterator.side_effect = [interrupted(), iter([{"id": "1"}, {"id": 2}])]
        self.assertEqual(list(self.iter_files(self.monitor, 42)), [{"id": 1}, {"id": 2}])
        self.assertEqual(self.iterator.call_args.kwargs["app"], "ios")
        self.monitor._wait_or_stop.assert_called_once_with(5)

    def test_all_channels_fail_propagates_error(self):
        self.iterator.side_effect = HTTPError("https://example.com", 405, "blocked", {}, None)
        with self.assertRaises(HTTPError):
            list(self.iter_files(self.monitor, 42))
        self.assertEqual(self.iterator.call_count, 2)

    def test_other_error_does_not_switch(self):
        self.iterator.side_effect = TimeoutError("timeout")
        with self.assertRaises(TimeoutError):
            list(self.iter_files(self.monitor, 42))
        self.assertEqual(self.iterator.call_count, 1)

    def test_stop_does_not_treat_partial_result_as_complete(self):
        self.iterator.side_effect = HTTPError("https://example.com", 405, "blocked", {}, None)
        self.monitor._wait_or_stop.return_value = True
        with self.assertRaises(HTTPError):
            list(self.iter_files(self.monitor, 42))
        self.assertEqual(self.iterator.call_count, 1)

    def test_increment_preserves_original_exception_with_host_logger(self):
        source = Path(__file__).resolve().parents[1] / "helper/strm/increment.py"
        method = next(
            node for node in ast.walk(ast.parse(source.read_text()))
            if isinstance(node, ast.FunctionDef) and node.name == "__generate_pan_tree"
        )
        logger = SimpleNamespace(info=Mock(), warning=Mock(), error=Mock())
        class TreeError(Exception):
            pass
        namespace = {
            "logger": logger, "sentry_manager": Mock(), "format_exc": lambda: "traceback",
            "ItertreeInternalError": TreeError,
        }
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        exec(compile(module, str(source), "exec"), namespace)
        helper = Mock()
        original = HTTPError("https://example.com", 405, "blocked", {}, None)
        setattr(helper, "__itertree", Mock(side_effect=original))
        with self.assertRaises(TreeError) as caught:
            namespace["__generate_pan_tree"](helper, "/cloud", "/local")
        self.assertIs(caught.exception.__cause__, original)
        logger.error.assert_called_once()

    def test_worker_backoff_preserves_cursor_and_saves_on_stop(self):
        source = Path(__file__).resolve().parents[1] / "service/life/__init__.py"
        method = next(
            node for node in ast.parse(source.read_text()).body
            if isinstance(node, ast.FunctionDef) and node.name == "monitor_life_thread_worker"
        )
        method.decorator_list = []
        config = Mock(monitor_life_first_pull_mode="last", notify=False)
        config.get_plugin_data.return_value = {"from_time": 123, "from_id": 456}
        monitor = Mock()
        monitor.check_status.return_value = True
        monitor.once_pull.side_effect = HTTPError("https://example.com", 405, "blocked", {}, None)
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.side_effect = [False, False, False, False, True]
        namespace = {
            "MonitorLife": object, "Event": object, "configer": config,
            "logger": Mock(), "time": lambda: 999, "format_exc": lambda: "traceback",
        }
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        exec(compile(module, str(source), "exec"), namespace)
        namespace["monitor_life_thread_worker"](monitor, stop)
        self.assertEqual(
            [call.kwargs["timeout"] for call in stop.wait.call_args_list],
            [30, 60, 120, 240, 300],
        )
        for call in monitor.once_pull.call_args_list:
            self.assertEqual(call.kwargs, {"from_time": 123, "from_id": 456})
        config.save_plugin_data.assert_called_with(
            "monitor_life_strm_files", {"from_time": 123, "from_id": 456}
        )
