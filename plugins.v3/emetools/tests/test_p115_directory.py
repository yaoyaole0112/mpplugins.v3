import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx

from emetools.p115 import LIST_ENDPOINTS, P115Client, P115DirectoryUnavailable, _REQUEST_GATES


class DirectoryFailureTests(unittest.TestCase):
    def setUp(self):
        self.clock = 100.0
        self.registry = patch.dict(_REQUEST_GATES, clear=True)
        self.registry.start()
        self.addCleanup(self.registry.stop)
        self.monotonic = patch("emetools.p115.time.monotonic", side_effect=lambda: self.clock)
        self.monotonic.start()
        self.addCleanup(self.monotonic.stop)
        self.sleep = patch("emetools.p115.time.sleep", side_effect=self.advance)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def advance(self, seconds):
        self.clock += seconds

    def make_client(self, handler):
        client = P115Client("test-cookie")
        client.client.close()
        client.client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.client.close)
        return client

    def enable_recovery(self, client, handler):
        def reconnect():
            connection = httpx.Client(transport=httpx.MockTransport(handler))
            self.addCleanup(connection.close)
            return connection

        reset = patch.object(client, "_new_connection", side_effect=reconnect)
        reset.start()
        self.addCleanup(reset.stop)
        client.enable_read_recovery()

    def test_client_disables_idle_connection_reuse(self):
        with patch('emetools.p115.httpx.Client') as connection:
            client = P115Client('test-cookie')
        limits = connection.call_args.kwargs['limits']
        self.assertEqual(limits.max_connections, 1)
        self.assertEqual(limits.max_keepalive_connections, 0)
        client.client.close()

    def test_cleanup_read_uses_longer_timeout_without_changing_write_timeout(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={'state': True, 'count': 0, 'data': []})

        client = self.make_client(handler)
        client.enable_read_recovery()
        client.list_children('123')
        client.delete_files(['456'])
        self.assertEqual(requests[0].extensions['timeout']['read'], 45)
        self.assertEqual(requests[0].extensions['timeout']['connect'], 15)
        self.assertNotEqual(requests[1].extensions['timeout']['read'], 45)

    def test_all_405_stops_without_repeated_rounds(self):
        calls = []

        def handler(request):
            calls.append(request.url.host)
            return httpx.Response(405, text="blocked")

        client = self.make_client(handler)
        with patch("emetools.p115.time.sleep") as sleep:
            with self.assertRaisesRegex(P115DirectoryUnavailable, "HTTP 405"):
                client.list_children("123")
            sleep.assert_not_called()
        self.assertEqual(len(calls), 1)

    def test_malformed_directory_can_try_next_endpoint(self):
        def handler(request):
            if request.url.host == "115cdn.com":
                return httpx.Response(200, json={"state": True, "count": 1, "data": []})
            return httpx.Response(200, json={"state": True, "count": 0, "data": []})

        client = self.make_client(handler)
        self.assertEqual(client.list_children("123"), ([], []))

    def test_expired_session_does_not_replay_requests(self):
        client = self.make_client(lambda request: httpx.Response(
            200, json={"state": False, "errNo": 990001}))
        with self.assertRaisesRegex(P115DirectoryUnavailable, "登录状态失效"):
            client.list_children("123")

    def test_other_clients_and_operations_share_restriction(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(405, text="blocked")

        blocked = self.make_client(handler)
        other = self.make_client(handler)
        with self.assertRaisesRegex(P115DirectoryUnavailable, "不代表已确认 Cookie 失效"):
            blocked.list_children("123")
        operations = [lambda: other.list_children("456"), lambda: other.delete_files(["123"]),
                      lambda: other.copy_files(["123"], "456"), lambda: other.move_files(["123"], "456"),
                      other.rb_list, other.clear_recyclebin]
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(P115DirectoryUnavailable, "请求已暂停"):
                    operation()
        self.assertEqual(len(calls), 1)

    def test_cooldown_can_be_checked_without_network_request(self):
        client = self.make_client(lambda request: httpx.Response(405))
        with self.assertRaises(P115DirectoryUnavailable):
            client.list_children("123")
        self.assertGreater(P115Client.cooldown_remaining_for_cookie("test-cookie"), 0)
        self.assertGreater(client.cooldown_remaining(), 0)
        client.client.close()
        self.assertGreater(P115Client.cooldown_remaining_for_cookie("test-cookie"), 0)

    def test_http_auth_status_without_business_code_is_not_claimed_expired(self):
        for status in (401, 403):
            with self.subTest(status=status), patch.dict(_REQUEST_GATES, clear=True):
                client = self.make_client(lambda request: httpx.Response(status))
                with self.assertRaisesRegex(P115DirectoryUnavailable, "不代表已确认 Cookie 失效"):
                    client.rb_list()

    def test_different_cookies_do_not_inherit_each_others_cooldown(self):
        blocked = self.make_client(lambda request: httpx.Response(405))
        with self.assertRaises(P115DirectoryUnavailable):
            blocked.rb_list()
        with P115Client("other-cookie") as other:
            other.client.close()
            other.client = httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"state": True, "count": 0, "data": []})))
            self.assertEqual(other.list_children("123"), ([], []))

    def test_backoff_grows_and_success_resets_it(self):
        attempts = []

        def handler(request):
            attempts.append(self.clock)
            if len(attempts) < 3:
                return httpx.Response(405)
            return httpx.Response(200, json={"state": True, "count": 0, "data": []})

        client = self.make_client(handler)
        with self.assertRaisesRegex(P115DirectoryUnavailable, "暂停请求 300 秒"):
            client.list_children("123")
        self.advance(300)
        with self.assertRaisesRegex(P115DirectoryUnavailable, "暂停请求 600 秒"):
            client.list_children("123")
        self.advance(600)
        self.assertEqual(client.list_children("123"), ([], []))
        self.assertEqual(client._gate.failures, 0)

    def test_restricted_endpoint_only_changes_after_cooldown(self):
        calls = []

        def handler(request):
            calls.append(request.url.host)
            if request.url.host == "115cdn.com":
                return httpx.Response(405)
            return httpx.Response(200, json={"state": True, "count": 0, "data": []})

        client = self.make_client(handler)
        with self.assertRaises(P115DirectoryUnavailable):
            client.list_children("123")
        with self.assertRaises(P115DirectoryUnavailable):
            client.list_children("123")
        self.assertEqual(calls, ["115cdn.com"])
        self.advance(300)
        self.assertEqual(client.list_children("123"), ([], []))
        self.assertEqual(calls, ["115cdn.com", "proapi.115.com"])

    def test_retry_after_is_respected_and_bounded(self):
        client = self.make_client(lambda request: httpx.Response(429, headers={"Retry-After": "900"}))
        with self.assertRaisesRegex(P115DirectoryUnavailable, "暂停请求 900 秒"):
            client.rb_list()
        self.advance(900)
        client.client.close()
        client.client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "99999"})))
        self.addCleanup(client.client.close)
        with self.assertRaisesRegex(P115DirectoryUnavailable, "暂停请求 3600 秒"):
            client.rb_list()

    def test_network_timeout_does_not_replay_mutation_or_expose_url(self):
        calls = []

        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("https://example.com/?token=secret-cookie", request=request)

        client = self.make_client(handler)
        with patch("emetools.p115.logger") as logger:
            with self.assertRaisesRegex(P115DirectoryUnavailable, "结果可能不确定") as raised:
                client.copy_files(["123"], "456")
            with self.assertRaises(P115DirectoryUnavailable):
                client.copy_files(["123"], "456")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("secret-cookie", str(raised.exception))
        self.assertNotIn("secret-cookie", str(logger.mock_calls))

    def test_remote_disconnect_stops_reads_until_cooldown_then_recovers(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
            return httpx.Response(200, json={"state": True, "count": 0, "data": []})

        client = self.make_client(handler)
        with self.assertRaisesRegex(P115DirectoryUnavailable, "RemoteProtocolError"):
            client.list_children("123")
        with self.assertRaisesRegex(P115DirectoryUnavailable, "请求已暂停"):
            client.list_children("456")
        self.assertEqual(len(calls), 1)
        self.advance(60)
        self.assertEqual(client.list_children("123"), ([], []))
        self.assertEqual(client.cooldown_remaining(), 0)
        self.assertEqual(len(calls), 2)

    def test_read_recovery_reconnects_and_keeps_pagination_in_this_run(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx.RemoteProtocolError("disconnected", request=request)
            return httpx.Response(200, json={"state": True, "count": 0, "data": []})

        client = self.make_client(handler)
        self.enable_recovery(client, handler)
        self.assertEqual(client._page("123", 1000), ([], [], 0, 0))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].url.host, "proapi.115.com")
        self.assertEqual(calls[1].url.params["cid"], "123")
        self.assertEqual(calls[1].url.params["offset"], "1000")
        self.assertEqual(calls[1].url.params["app_ver"], "36.2.28")
        self.assertEqual(client._gate.list_endpoint, 1)
        self.assertGreaterEqual(self.clock, 160)

    def test_read_recovery_is_bounded_and_reports_exhaustion(self):
        calls = []

        def handler(request):
            calls.append(request)
            raise httpx.RemoteProtocolError("disconnected", request=request)

        client = self.make_client(handler)
        self.enable_recovery(client, handler)
        with self.assertRaisesRegex(P115DirectoryUnavailable, "已尝试 3 次") as raised:
            client.list_children("123")
        self.assertTrue(raised.exception.exhausted)
        self.assertEqual(len(calls), 3)
        self.assertEqual([request.url.host for request in calls], ["115cdn.com", "proapi.115.com", "aps.115.com"])

    def test_read_recovery_does_not_retry_access_restrictions_or_authentication(self):
        for status in (401, 403, 405, 429):
            with self.subTest(status=status), patch.dict(_REQUEST_GATES, clear=True):
                client = self.make_client(lambda request: httpx.Response(status))
                client.enable_read_recovery()
                with patch("emetools.p115.time.sleep") as sleep:
                    with self.assertRaises(P115DirectoryUnavailable):
                        client.list_children("123")
                    sleep.assert_not_called()

    def test_recovery_enabled_still_never_replays_a_delete(self):
        calls = []

        def handler(request):
            calls.append(request)
            raise httpx.RemoteProtocolError("disconnected", request=request)

        client = self.make_client(handler)
        self.enable_recovery(client, handler)
        with self.assertRaisesRegex(P115DirectoryUnavailable, "结果可能不确定"):
            client.delete_files(["123"])
        self.assertEqual(len(calls), 1)

    def test_stop_during_recovery_cancels_before_next_request(self):
        calls = []
        stop = threading.Event()

        def handler(request):
            calls.append(request)
            raise httpx.RemoteProtocolError("disconnected", request=request)

        client = self.make_client(handler)
        client.enable_read_recovery(stop)
        with patch("emetools.p115.time.sleep", side_effect=lambda delay: stop.set()):
            with self.assertRaisesRegex(P115DirectoryUnavailable, "清理任务已停止"):
                client.list_children("123")
        self.assertEqual(len(calls), 1)

    def test_preexisting_network_cooldown_is_waited_in_this_run(self):
        client = self.make_client(lambda request: httpx.Response(200, json={"state": True, "count": 0, "data": []}))
        self.enable_recovery(client, lambda request: httpx.Response(200, json={"state": True, "count": 0, "data": []}))
        client._gate.defer(minimum=60, retry_reads=True)
        self.assertTrue(P115Client.read_recovery_available_for_cookie("test-cookie"))
        self.assertEqual(client.list_children("123"), ([], []))
        self.assertGreaterEqual(self.clock, 160)

    def test_extended_shared_cooldown_has_a_bounded_wait(self):
        client = self.make_client(lambda request: httpx.Response(200))
        client.enable_read_recovery()
        client._gate.defer(minimum=3600, retry_reads=True)
        with self.assertRaisesRegex(P115DirectoryUnavailable, "已等待 240 秒") as raised:
            client.list_children("123")
        self.assertTrue(raised.exception.exhausted)
        self.assertEqual(self.clock, 340)

    def test_successful_requests_are_serialized_and_spaced_across_clients(self):
        active, peak, times = 0, 0, []
        tracking = threading.Lock()

        def handler(request):
            nonlocal active, peak
            with tracking:
                active += 1
                peak = max(peak, active)
                times.append(self.clock)
            threading.Event().wait(0.01)
            with tracking:
                active -= 1
            return httpx.Response(200, json={"state": True, "count": 0, "data": []})

        clients = [self.make_client(handler) for position in range(3)]
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda client: client.list_children("123"), clients))
        self.assertEqual(results, [([], [])] * 3)
        self.assertEqual(peak, 1)
        self.assertTrue(all(later - earlier >= 1 for earlier, later in zip(times, times[1:])))

    def test_business_error_attempts_are_bounded_and_not_logged_raw(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"state": False, "error": "cookie=private-token"})

        client = self.make_client(handler)
        with self.assertRaisesRegex(RuntimeError, "业务错误") as raised:
            client.list_children("123")
        self.assertEqual(len(calls), len(LIST_ENDPOINTS))
        self.assertNotIn("private-token", str(raised.exception))
