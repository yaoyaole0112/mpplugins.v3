import ast
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import ModuleType
from unittest import TestCase
from unittest.mock import MagicMock, call, patch

from httpx import (
    HTTPStatusError,
    LocalProtocolError,
    RemoteProtocolError,
    Request,
    Response,
)
from urllib3_future.exceptions import (
    MaxRetryError,
    ProtocolError,
    ReadTimeoutError,
    ResponseError,
)


def load_read_retry():
    path = Path(__file__).resolve().parents[1] / "utils/read_retry.py"
    module = ModuleType("_p115_read_retry_test")
    module.logger = MagicMock()
    tree = ast.parse(path.read_text())
    tree.body = [
        node for node in tree.body
        if not (isinstance(node, ast.ImportFrom) and node.module == "app.sdk.logging")
    ]
    exec(compile(tree, str(path), "exec"), module.__dict__)
    return module


class TestFullReadRetry(TestCase):
    def setUp(self):
        self.module = load_read_retry()
        self.pause = patch.object(self.module, "sleep")
        self.sleep = self.pause.start()
        self.addCleanup(self.pause.stop)

    def test_truncated_read_retries_current_request_with_backoff(self):
        error = ProtocolError("incomplete chunked read")
        response = {"state": True, "data": [{"id": 2}]}
        params = {"page": 2}
        parse = object()
        with patch.object(
            self.module, "request", side_effect=[error, error, response]
        ) as request:
            result = self.module.retry_read_request(
                "https://example.invalid/list", params=params, parse=parse, timeout=30,
            )
        self.assertIs(result, response)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])
        for invocation in request.call_args_list:
            self.assertIs(invocation.kwargs["params"], params)
            self.assertIs(invocation.kwargs["parse"], parse)
            self.assertEqual(invocation.kwargs["timeout"], 30)

    def test_persistent_failure_is_bounded_and_preserves_exception(self):
        error = RemoteProtocolError("incomplete chunked read")
        with patch.object(self.module, "request", side_effect=error) as request:
            with self.assertRaises(RemoteProtocolError) as raised:
                self.module.retry_read_request("https://example.invalid/list")
        self.assertIs(raised.exception, error)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_business_status_parse_errors_and_writes_are_not_retried(self):
        status = HTTPStatusError(
            "denied", request=Request("GET", "https://example.invalid"),
            response=Response(403),
        )
        cases = [
            ("GET", ValueError("invalid response")),
            ("GET", PermissionError("expired credentials")),
            ("GET", status),
            ("GET", LocalProtocolError("invalid request")),
            ("POST", ProtocolError("disconnected")),
            ("PUT", ProtocolError("disconnected")),
            ("DELETE", ProtocolError("disconnected")),
            ("GET", MaxRetryError(None, "/list", ResponseError("too many 503 responses"))),
        ]
        for method, error in cases:
            with self.subTest(method=method, error=type(error).__name__):
                with patch.object(
                    self.module, "request", side_effect=error
                ) as request:
                    with self.assertRaises(type(error)):
                        self.module.retry_read_request(
                            "https://example.invalid/list", method
                        )
                request.assert_called_once()
        self.sleep.assert_not_called()

    def test_wrapped_transport_error_is_retried(self):
        error = MaxRetryError(None, "/list", ProtocolError("disconnected"))
        with patch.object(
            self.module, "request", side_effect=[error, {"state": True}]
        ) as request:
            self.assertEqual(
                self.module.retry_read_request("https://example.invalid/list"),
                {"state": True},
            )
        self.assertEqual(request.call_count, 2)

    def test_read_timeouts_and_connection_resets_are_retried(self):
        errors = [
            ReadTimeoutError(None, "/list", "read timed out"),
            ConnectionResetError("connection reset"),
            TimeoutError("timed out"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with patch.object(
                    self.module, "request", side_effect=[error, {}]
                ) as request:
                    self.assertEqual(
                        self.module.retry_read_request("https://example.invalid/list"), {}
                    )
                self.assertEqual(request.call_count, 2)

    def test_read_only_metadata_post_timeout_is_retried(self):
        error = ReadTimeoutError(None, "/files/file", "Read timed out")
        response = {"state": True, "data": [{"file_name": "a.mkv"}]}
        data = {"file_id": "1,2"}
        with patch.object(self.module, "request", side_effect=[error, response]) as request:
            result = self.module.retry_read_request(
                "https://webapi.115.com/files/file", "POST", data=data
            )
        self.assertIs(result, response)
        self.assertEqual(request.call_count, 2)
        for invocation in request.call_args_list:
            self.assertIs(invocation.kwargs["data"], data)
        self.sleep.assert_called_once_with(1)

    def test_post_allowlist_never_retries_mutations_or_streams(self):
        from io import BytesIO

        cases = [
            ("https://webapi.115.com/files/move", {"file_id": "1"}),
            ("https://webapi.115.com/files/delete", {"file_id": "1"}),
            ("https://webapi.115.com/files/file/", {"file_id": "1"}),
            ("https://example.invalid/files/file", {"file_id": "1"}),
            ("http://webapi.115.com/files/file", {"file_id": "1"}),
            ("https://webapi.115.com:444/files/file", {"file_id": "1"}),
            ("https://webapi.115.com/files/file?op=delete", {"file_id": "1"}),
            ("https://webapi.115.com/files/file", {"file_id": "1", "op": "delete"}),
            ("https://webapi.115.com/files/file", BytesIO(b"file_id=1")),
        ]
        for url, data in cases:
            with self.subTest(url=url, data=type(data).__name__):
                with patch.object(self.module, "request", side_effect=ProtocolError("closed")) as request:
                    with self.assertRaises(ProtocolError):
                        self.module.retry_read_request(url, "POST", data=data)
                request.assert_called_once()
        self.sleep.assert_not_called()

    def test_business_response_is_returned_unchanged_without_retry(self):
        response = {"state": False, "errno": 405}
        with patch.object(self.module, "request", return_value=response) as request:
            self.assertIs(
                self.module.retry_read_request("https://example.invalid/list"), response
            )
        request.assert_called_once()
        self.sleep.assert_not_called()

    def test_async_request_is_passed_through_without_sync_retry(self):
        response = object()
        with patch.object(self.module, "request", return_value=response) as request:
            self.assertIs(
                self.module.retry_read_request("https://example.invalid", async_=True),
                response,
            )
        request.assert_called_once_with(
            url="https://example.invalid", method="GET", async_=True
        )
        self.sleep.assert_not_called()

    def test_timeout_backend_detection_remains_compatible(self):
        self.assertEqual(
            self.module.retry_read_request.__module__, "urllib3_future_request"
        )

    def test_successful_page_is_not_replayed(self):
        pages = []
        failures = []

        def request(**kwargs):
            page = kwargs["params"]["page"]
            pages.append(page)
            if page == 2 and not failures:
                failures.append(page)
                raise ProtocolError("incomplete chunked read")
            return [{"id": page}]

        with patch.object(self.module, "request", side_effect=request):
            items = []
            for page in (1, 2, 3):
                items.extend(
                    self.module.retry_read_request(
                        "https://example.invalid/list", params={"page": page}
                    )
                )
        self.assertEqual(pages, [1, 2, 2, 3])
        self.assertEqual(items, [{"id": 1}, {"id": 2}, {"id": 3}])

    def test_real_truncated_http_response_recovers_with_original_parser(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                requests.append(self.path)
                if len(requests) == 1:
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b"20\r\n{\"state\":")
                    self.wfile.flush()
                    self.close_connection = True
                else:
                    body = b'{"state":true,"data":[{"id":7}]}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            response = self.module.retry_read_request(
                f"http://127.0.0.1:{server.server_port}/list?page=2",
                parse=lambda response, body: json.loads(body), retries=0, timeout=3,
            )
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
        self.assertEqual(response, {"state": True, "data": [{"id": 7}]})
        self.assertEqual(requests, ["/list?page=2", "/list?page=2"])
        self.sleep.assert_called_once_with(1)
