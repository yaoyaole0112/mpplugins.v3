import unittest
from unittest.mock import patch

import httpx

from emetools.p115 import LIST_ENDPOINTS, P115Client, P115DirectoryUnavailable


class DirectoryFailureTests(unittest.TestCase):
    def make_client(self, handler):
        client = P115Client("test-cookie")
        client.client.close()
        client.client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.client.close)
        return client

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
        self.assertEqual(len(calls), len(LIST_ENDPOINTS))

    def test_first_endpoint_rejected_next_succeeds(self):
        def handler(request):
            if request.url.host == "115cdn.com":
                return httpx.Response(405)
            return httpx.Response(200, json={"state": True, "count": 0, "data": []})

        client = self.make_client(handler)
        self.assertEqual(client.list_children("123"), ([], []))

    def test_expired_session_does_not_replay_requests(self):
        client = self.make_client(lambda request: httpx.Response(
            200, json={"state": False, "errNo": 990001}))
        with self.assertRaisesRegex(P115DirectoryUnavailable, "登录状态失效"):
            client.list_children("123")
