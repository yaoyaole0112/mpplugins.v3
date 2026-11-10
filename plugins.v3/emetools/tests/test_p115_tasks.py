import asyncio
import threading
import time
import unittest
from types import SimpleNamespace

from emetools.p115_tasks import P115CoordinatorStopped, P115TaskCoordinator
from emetools import EmeTools, RapidAction, ToolAction


class P115TaskTests(unittest.TestCase):
    def setUp(self):
        self.queue = P115TaskCoordinator()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.addCleanup(self.queue.close)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("task state did not arrive")
            time.sleep(0.005)

    def hold(self):
        job = self.queue.submit("active", "active", lambda: self.release.wait(2))
        self.wait_for(lambda: job["state"] == "running")
        return job

    def test_manual_priority_deduplication_and_single_execution(self):
        active = self.hold()
        order = []
        background = threading.Thread(target=lambda: self.queue.run("scheduled", "scheduled", lambda: order.append("scheduled")))
        background.start()
        self.wait_for(lambda: len(self.queue.status()) == 2)
        manual = self.queue.submit("manual", "manual", lambda: order.append("manual"))
        duplicate = self.queue.submit("manual", "manual", lambda: order.append("duplicate"))
        self.assertIs(manual, duplicate)
        self.assertEqual([job["label"] for job in self.queue.status()], ["active", "manual", "scheduled"])
        self.release.set()
        self.assertTrue(active["done"].wait(2))
        background.join(2)
        self.assertFalse(background.is_alive())
        self.assertEqual(order, ["manual", "scheduled"])

    def test_close_cancels_waiting_jobs_without_submitting_requests(self):
        self.hold()
        executed = []
        job = self.queue.submit("pending", "pending", lambda: executed.append(True))
        self.queue.close()
        self.assertTrue(job["done"].wait(1))
        self.assertEqual(self.queue.job_status(job["id"])["state"], "cancelled")
        self.assertEqual(executed, [])
        with self.assertRaises(ValueError):
            self.queue.submit("closed", "closed", lambda: None)

    def test_stale_page_during_plugin_reload_gets_retryable_directory_error(self):
        plugin = object.__new__(EmeTools)
        plugin._p115_tasks = self.queue
        self.queue.close()
        result = asyncio.run(plugin.action(ToolAction(operation="p115_dirs", cid="0")))
        self.assertFalse(result["ok"])
        self.assertIn("插件正在更新或重载", result["message"])
        self.assertIn("未发送115请求", result["message"])

    def test_unexpected_directory_error_is_returned_as_actionable_message(self):
        plugin = object.__new__(EmeTools)
        plugin._p115_tasks = self.queue

        def fail(_cid):
            raise TypeError("internal detail")

        plugin._p115_dirs = fail
        result = asyncio.run(plugin.action(ToolAction(operation="p115_dirs", cid="0")))
        self.assertFalse(result["ok"])
        self.assertIn("读取115目录失败（TypeError）", result["message"])
        self.assertNotIn("internal detail", result["message"])

    def test_unexpected_rapid_submission_error_is_not_reported_as_unknown(self):
        class BrokenQueue:
            def submit(self, *args, **kwargs):
                raise TypeError("internal detail")

        plugin = object.__new__(EmeTools)
        plugin._p115_tasks = BrokenQueue()
        plugin._rapid = SimpleNamespace(queue_scan=lambda *_args: None, clear_scan_queue=lambda *_args: None)
        result = asyncio.run(plugin.rapid_action(RapidAction(operation="run", config_id="rapid_1")))
        self.assertFalse(result["ok"])
        self.assertIn("本地秒传任务提交失败（TypeError）", result["message"])
        self.assertNotIn("internal detail", result["message"])

    def test_rapid_action_submits_without_unsupported_manual_keyword(self):
        class Queue:
            def submit(self, key, label, callback):
                self.arguments = (key, label, callback)
                return {"id": "rapid-job", "done": threading.Event()}

        plugin = object.__new__(EmeTools)
        plugin._p115_tasks = Queue()
        plugin._rapid = SimpleNamespace(queue_scan=lambda *_args: None, clear_scan_queue=lambda *_args: None)

        async def submission(job):
            return {"ok": True, "queued": True, "job_id": job["id"]}

        plugin._p115_submission = submission
        result = asyncio.run(plugin.rapid_action(RapidAction(operation="run", config_id="rapid_1")))
        self.assertTrue(result["ok"])
        self.assertEqual(result["job_id"], "rapid-job")

    def test_failure_releases_queue_without_replaying_callback(self):
        called = []
        def fail():
            called.append(True)
            raise RuntimeError("private-signed-url")
        job = self.queue.submit("failure", "failure", fail)
        self.assertTrue(job["done"].wait(2))
        status = self.queue.job_status(job["id"])
        self.assertEqual(status["state"], "failed")
        self.assertNotIn("private", status["message"])
        self.assertEqual(called, [True])
        self.assertEqual(self.queue.run("next", "next", lambda: 42), 42)

    def test_results_are_copied_and_queue_is_bounded(self):
        job = self.queue.submit("result", "result", lambda: {"token": "confirmation", "ok": True})
        self.assertTrue(job["done"].wait(2))
        status = self.queue.job_status(job["id"])
        status["result"]["token"] = "changed"
        self.assertEqual(self.queue.job_status(job["id"])["result"]["token"], "confirmation")
        self.hold()
        for index in range(32):
            self.queue.submit(str(index), str(index), lambda: None)
        with self.assertRaises(ValueError):
            self.queue.submit("overflow", "overflow", lambda: None)

    def test_nested_coordination_does_not_deadlock(self):
        self.assertEqual(self.queue.run("outer", "outer", lambda: self.queue.run("inner", "inner", lambda: 42)), 42)

    def test_rapid_scan_does_not_hold_control_lane_and_control_calls_are_serial(self):
        scan_started = threading.Event()
        request_started = threading.Event()
        scan_release = threading.Event()
        self.addCleanup(scan_release.set)
        self.hold()

        def scan():
            scan_started.set()
            self.queue.run("rapid_api:test", "rapid request", lambda: request_started.set())
            scan_release.wait(2)
            return {"ok": True}

        job = self.queue.submit("rapid_upload:one", "rapid scan", scan)
        self.assertTrue(scan_started.wait(1))
        self.assertFalse(request_started.is_set())
        self.release.set()
        self.assertTrue(request_started.wait(1))
        control = self.queue.submit("clean", "clean", lambda: {"ok": True})
        self.assertTrue(control["done"].wait(1))
        self.assertEqual(control["state"], "completed")
        self.assertEqual(job["state"], "running")
        scan_release.set()
        self.assertTrue(job["done"].wait(1))

    def test_two_rapid_scans_are_serial_and_share_duplicate_protection(self):
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        first = self.queue.submit("rapid_upload:one", "first", lambda: (started.set(), release.wait(2)))
        self.assertTrue(started.wait(1))
        second = self.queue.submit("rapid_upload:two", "second", lambda: 42)
        duplicate = self.queue.submit("rapid_upload:two", "second duplicate", lambda: 99)
        self.assertIs(second, duplicate)
        self.assertEqual(second["state"], "queued")
        release.set()
        self.assertTrue(first["done"].wait(1))
        self.assertTrue(second["done"].wait(1))
        self.assertEqual(second["result"], 42)

    def test_manual_api_returns_promptly_and_delivers_original_result_once(self):
        self.hold()
        plugin = object.__new__(EmeTools)
        plugin._p115_tasks = self.queue
        calls = []
        async def action_serial(action):
            calls.append(action.operation)
            return {"ok": True, "token": "confirmed-preview", "file_count": 3}
        plugin._action_serial = action_serial
        async def request():
            first = await asyncio.wait_for(plugin.action(ToolAction(operation="cleanup_preview")), 1)
            second = await plugin.action(ToolAction(operation="cleanup_preview"))
            self.assertEqual(first["job_id"], second["job_id"])
            state = await plugin.action(ToolAction(operation="p115_task_status", token=first["job_id"]))
            self.assertEqual(state["state"], "queued")
            return first["job_id"]
        token = asyncio.run(request())
        self.assertEqual(calls, [])
        self.release.set()
        self.wait_for(lambda: self.queue.job_status(token)["state"] == "completed")
        self.assertEqual(calls, ["cleanup_preview"])
        self.assertEqual(self.queue.job_status(token)["result"]["token"], "confirmed-preview")

    def test_confirmation_tokens_are_not_coalesced_with_other_confirmations(self):
        self.hold()
        plugin = object.__new__(EmeTools)
        plugin._p115_tasks = self.queue
        plugin._set_cleanup_status = lambda *args, **kwargs: True
        async def action_serial(action):
            return {"ok": True}
        plugin._action_serial = action_serial
        first = asyncio.run(plugin.action(ToolAction(operation="cleanup_confirm", token="first")))
        second = asyncio.run(plugin.action(ToolAction(operation="cleanup_confirm", token="second")))
        self.assertNotEqual(first["job_id"], second["job_id"])

    def test_confirmation_expiry_is_checked_after_waiting(self):
        self.hold()
        plugin = object.__new__(EmeTools)
        plugin._p115_tasks = self.queue
        plugin._set_cleanup_status = lambda *args, **kwargs: True
        plugin._pending_lock = threading.Lock()
        plugin._pending = {"confirmation": (time.monotonic(), "cleanup", {})}
        async def action_serial(action):
            plugin._consume(action.token, "cleanup")
            return {"ok": True}
        plugin._action_serial = action_serial
        response = asyncio.run(plugin.action(ToolAction(operation="cleanup_confirm", token="confirmation")))
        self.assertIn("confirmation", plugin._pending)
        plugin._pending["confirmation"] = (time.monotonic() - 1801, "cleanup", {})
        self.release.set()
        self.wait_for(lambda: self.queue.job_status(response["job_id"])["state"] == "failed")

    def test_stopping_preserves_running_operation_and_cancels_waiters_on_reopen(self):
        active = self.hold()
        pending = self.queue.submit("pending", "pending", lambda: self.fail("cancelled task ran"))
        self.queue.close()
        self.queue.reopen()
        executed = []
        newer = self.queue.submit("new", "new", lambda: executed.append(True))
        self.assertEqual(active["state"], "running")
        self.assertEqual(pending["state"], "cancelled")
        self.assertEqual(newer["state"], "queued")
        self.release.set()
        self.assertTrue(newer["done"].wait(2))
        self.assertEqual(executed, [True])

    def test_stale_coordinator_submission_is_handed_to_replacement_once(self):
        plugin = object.__new__(EmeTools)
        replacement = P115TaskCoordinator()
        class StaleQueue:
            def submit(self, *args, **kwargs):
                plugin._p115_tasks = replacement
                raise P115CoordinatorStopped("stopped")
        plugin._p115_tasks = StaleQueue()
        executed = []

        job = plugin._submit_p115_task("move", "文件转存", lambda: executed.append(True))
        self.assertTrue(job["done"].wait(2))
        self.assertEqual(job["state"], "completed")
        self.assertEqual(executed, [True])
        replacement.close()

    def test_scheduled_task_does_not_submit_during_plugin_shutdown(self):
        plugin = object.__new__(EmeTools)
        plugin._p115_stop = threading.Event()
        plugin._p115_stop.set()
        class BrokenQueue:
            def run(self, *args, **kwargs):
                raise AssertionError("shutdown task must not be submitted")
        plugin._p115_tasks = BrokenQueue()

        self.assertIsNone(plugin._run_scheduled("p115_move"))

    def test_duplicate_scheduled_ticks_do_not_accumulate(self):
        active = self.hold()
        executed = []
        background = threading.Thread(target=lambda: self.queue.run("scheduled", "scheduled", lambda: executed.append(True)))
        background.start()
        self.wait_for(lambda: len(self.queue.status()) == 2)
        response = self.queue.run("scheduled", "scheduled", lambda: self.fail("duplicate tick ran"))
        self.assertTrue(response["queued"])
        self.assertEqual(len(self.queue.status()), 2)
        self.release.set()
        background.join(2)
        self.assertTrue(active["done"].wait(2))
        self.assertEqual(executed, [True])

    def test_media_cleanup_api_queues_without_consuming_confirmation_early(self):
        self.hold()
        plugin = object.__new__(EmeTools)
        plugin._p115_tasks = self.queue
        received = []
        async def media_serial(action):
            received.append(action["token"])
            return {"ok": True, "deleted": [], "failures": []}
        plugin._media_action_serial = media_serial
        response = asyncio.run(plugin.media_action({"operation": "confirm_delete", "token": "confirmation"}))
        self.assertTrue(response["queued"])
        self.assertEqual(received, [])
        self.release.set()
        self.wait_for(lambda: self.queue.job_status(response["job_id"])["state"] == "completed")
        self.assertEqual(received, ["confirmation"])


if __name__ == "__main__":
    unittest.main()
