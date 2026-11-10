import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from emetools.rapid_transfer import (
    LimitedReader, TransferStopped, UploadTransportError, UploadUncertain, upload_oss,
)


class RapidTransferTests(unittest.TestCase):
    def upload_fixture(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "episode.mkv"
        path.write_bytes(b"video-content")
        bucket = Mock()
        bucket.init_multipart_upload.return_value.upload_id = "upload"
        bucket.upload_part.return_value.etag = "etag"
        bucket.complete_multipart_upload.return_value.status = 200
        client = Mock()
        client.upload_gettoken.return_value = {
            "AccessKeyId": "id", "AccessKeySecret": "secret", "SecurityToken": "token"}
        response = {"bucket": "bucket", "object": "object",
                    "callback": {"callback": "callback", "callback_var": "var"}}
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        return path, bucket, client, response, stop

    def test_limited_reader_returns_full_requested_bytes_and_applies_rate(self):
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        content = b"x" * (2 * 1024 * 1024)
        with patch("emetools.rapid_transfer.time.monotonic", return_value=100):
            reader = LimitedReader(io.BytesIO(content), stop, 1)
            self.assertEqual(reader.read(len(content)), content)
        stop.wait.assert_called_once_with(2.0)

    def test_cancel_interrupts_throttled_read(self):
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        with patch("emetools.rapid_transfer.time.monotonic", return_value=100):
            reader = LimitedReader(io.BytesIO(b"x" * 1024), stop, 1)
            with self.assertRaises(TransferStopped):
                reader.read(1024)

    def test_sequential_upload_consumes_every_byte_and_completes_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.mkv"
            payload = b"x" * (11 * 1024 * 1024 + 17)
            path.write_bytes(payload)
            bucket = Mock()
            bucket.init_multipart_upload.return_value.upload_id = "upload"
            consumed = []

            def upload_part(_object, _upload, _number, data, progress_callback=None):
                chunks = []
                while chunk := data.read(65536):
                    chunks.append(chunk)
                consumed.append(b"".join(chunks))
                return SimpleNamespace(etag="etag")

            bucket.upload_part.side_effect = upload_part
            bucket.complete_multipart_upload.return_value.status = 200
            client = Mock()
            client.upload_gettoken.return_value = {
                "AccessKeyId": "id", "AccessKeySecret": "secret", "SecurityToken": "token"}
            response = {"data": {"bucket": "bucket", "object": "object",
                                "callback": {"callback": "callback", "callback_var": "var"}}}
            before_complete = Mock()
            progress = Mock()
            with patch("oss2.Bucket", return_value=bucket):
                upload_oss(client, path, response, threading.Event(), 0, lambda callback: callback(),
                           progress, before_complete)
            self.assertEqual(b"".join(consumed), payload)
            self.assertEqual(len(consumed), 2)
            before_complete.assert_called_once()
            bucket.complete_multipart_upload.assert_called_once()
            bucket.abort_multipart_upload.assert_not_called()
            progress.assert_called_with(len(payload), len(payload))

    def test_completion_timeout_is_uncertain_and_is_never_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.mkv"
            path.write_bytes(b"video")
            bucket = Mock()
            bucket.init_multipart_upload.return_value.upload_id = "upload"
            bucket.upload_part.return_value.etag = "etag"
            bucket.complete_multipart_upload.side_effect = TimeoutError("signed-url-secret")
            client = Mock()
            client.upload_gettoken.return_value = {
                "AccessKeyId": "id", "AccessKeySecret": "secret", "SecurityToken": "token"}
            response = {"bucket": "bucket", "object": "object",
                        "callback": {"callback": "callback", "callback_var": "var"}}
            with patch("oss2.Bucket", return_value=bucket), self.assertRaises(UploadUncertain) as raised:
                upload_oss(client, path, response, threading.Event(), 0, lambda callback: callback(),
                           Mock(), Mock())
            self.assertNotIn("secret", str(raised.exception))
            bucket.complete_multipart_upload.assert_called_once()
            bucket.abort_multipart_upload.assert_not_called()
            self.assertTrue(path.exists())

    def test_cancel_before_initialization_sends_no_requests_or_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.mkv"
            path.write_bytes(b"video")
            bucket = Mock()
            bucket.init_multipart_upload.return_value.upload_id = "upload"
            client = Mock()
            client.upload_gettoken.return_value = {
                "AccessKeyId": "id", "AccessKeySecret": "secret", "SecurityToken": "token"}
            response = {"bucket": "bucket", "object": "object",
                        "callback": {"callback": "callback", "callback_var": "var"}}
            stop = threading.Event()
            stop.set()
            with patch("oss2.Bucket", return_value=bucket), self.assertRaises(TransferStopped):
                upload_oss(client, path, response, stop, 0, lambda callback: callback(), Mock(), Mock())
            client.upload_gettoken.assert_not_called()
            bucket.init_multipart_upload.assert_not_called()
            bucket.complete_multipart_upload.assert_not_called()
            bucket.abort_multipart_upload.assert_not_called()

    def test_partial_read_network_failure_rewinds_same_part_and_replaces_connection(self):
        from oss2.exceptions import RequestError

        path, bucket, client, response, stop = self.upload_fixture()
        reads = []

        def send_part(_object, _upload, number, data, progress_callback=None):
            self.assertEqual(number, 1)
            if not reads:
                reads.append(data.read(3))
                raise RequestError(ConnectionError("signed-url-secret"))
            reads.append(data.read())
            return SimpleNamespace(etag="etag")

        bucket.upload_part.side_effect = send_part
        with patch("oss2.Bucket", return_value=bucket), patch("oss2.http.Session") as sessions:
            upload_oss(client, path, response, stop, 0, lambda callback: callback(), Mock(), Mock())
        self.assertEqual(reads, [b"vid", b"video-content"])
        self.assertEqual(sessions.call_count, 2)
        stop.wait.assert_called_once_with(2)
        bucket.complete_multipart_upload.assert_called_once()
        bucket.abort_multipart_upload.assert_not_called()

    def test_network_retries_are_bounded_and_failure_has_safe_stage_and_cause(self):
        from oss2.exceptions import RequestError

        path, bucket, client, response, stop = self.upload_fixture()
        bucket.upload_part.side_effect = RequestError(TimeoutError("signed-url-secret"))
        with patch("oss2.Bucket", return_value=bucket), self.assertRaises(UploadTransportError) as raised:
            upload_oss(client, path, response, stop, 0, lambda callback: callback(), Mock(), Mock())
        self.assertEqual(bucket.upload_part.call_count, 3)
        self.assertTrue(raised.exception.retryable)
        self.assertIn("上传第1分片", str(raised.exception))
        self.assertIn("RequestError/TimeoutError", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))
        bucket.abort_multipart_upload.assert_called_once()
        bucket.complete_multipart_upload.assert_not_called()
        self.assertTrue(path.exists())

    def test_retry_does_not_rewind_an_already_successful_part(self):
        from oss2.exceptions import RequestError

        path, bucket, client, response, stop = self.upload_fixture()
        path.write_bytes(b"abcDEFghi")
        attempts = []

        def send_part(_object, _upload, number, data, progress_callback=None):
            payload = data.read()
            attempts.append((number, payload))
            if len(attempts) == 2:
                raise RequestError(ConnectionError("secret"))
            return SimpleNamespace(etag=f"etag-{number}")

        bucket.upload_part.side_effect = send_part
        with patch("oss2.Bucket", return_value=bucket), patch("oss2.determine_part_size", return_value=3):
            upload_oss(client, path, response, stop, 0, lambda callback: callback(), Mock(), Mock())
        self.assertEqual(attempts, [(1, b"abc"), (2, b"DEF"), (2, b"DEF"), (3, b"ghi")])

    def test_permanent_server_error_is_not_retried(self):
        from oss2.exceptions import ServerError

        path, bucket, client, response, stop = self.upload_fixture()
        bucket.upload_part.side_effect = ServerError(403, {}, b"", {})
        with patch("oss2.Bucket", return_value=bucket), self.assertRaises(UploadTransportError) as raised:
            upload_oss(client, path, response, stop, 0, lambda callback: callback(), Mock(), Mock())
        self.assertFalse(raised.exception.retryable)
        bucket.upload_part.assert_called_once()
        stop.wait.assert_not_called()
        bucket.complete_multipart_upload.assert_not_called()

    def test_initialization_network_error_retries_but_completion_does_not(self):
        from oss2.exceptions import RequestError

        path, bucket, client, response, stop = self.upload_fixture()
        bucket.init_multipart_upload.side_effect = [
            RequestError(ConnectionError("secret")), SimpleNamespace(upload_id="upload")]
        bucket.complete_multipart_upload.side_effect = RequestError(TimeoutError("secret"))
        with patch("oss2.Bucket", return_value=bucket), self.assertRaises(UploadUncertain) as raised:
            upload_oss(client, path, response, stop, 0, lambda callback: callback(), Mock(), Mock())
        self.assertEqual(bucket.init_multipart_upload.call_count, 2)
        bucket.complete_multipart_upload.assert_called_once()
        bucket.abort_multipart_upload.assert_not_called()
        self.assertIn("RequestError/TimeoutError", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_stop_during_retry_wait_does_not_commit_or_retry(self):
        from oss2.exceptions import RequestError

        path, bucket, client, response, stop = self.upload_fixture()
        bucket.upload_part.side_effect = RequestError(ConnectionError("secret"))
        stop.wait.return_value = True
        with patch("oss2.Bucket", return_value=bucket), self.assertRaises(TransferStopped):
            upload_oss(client, path, response, stop, 0, lambda callback: callback(), Mock(), Mock())
        bucket.upload_part.assert_called_once()
        bucket.complete_multipart_upload.assert_not_called()
        bucket.abort_multipart_upload.assert_called_once()

    def test_read_only_token_failure_is_retryable_not_uncertain_completion(self):
        path, bucket, client, response, stop = self.upload_fixture()
        with patch("oss2.Bucket", return_value=bucket), self.assertRaises(UploadTransportError) as raised:
            upload_oss(client, path, response, stop, 0,
                       Mock(side_effect=UploadUncertain("signed-url-secret")), Mock(), Mock())
        self.assertTrue(raised.exception.retryable)
        self.assertIn("获取上传凭据", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))
        bucket.init_multipart_upload.assert_not_called()

    def test_transient_server_failure_retries_and_http_endpoint_is_upgraded(self):
        from oss2.exceptions import ServerError

        path, bucket, client, response, stop = self.upload_fixture()
        client.upload_gettoken.return_value["endpoint"] = "http://oss-cn-shenzhen.aliyuncs.com"
        bucket.upload_part.side_effect = [ServerError(503, {}, b"", {}), SimpleNamespace(etag="etag")]
        with patch("oss2.Bucket", return_value=bucket) as factory:
            upload_oss(client, path, response, stop, 0, lambda callback: callback(), Mock(), Mock())
        self.assertEqual(factory.call_args.args[1], "https://oss-cn-shenzhen.aliyuncs.com")
        self.assertEqual(bucket.upload_part.call_count, 2)
        bucket.complete_multipart_upload.assert_called_once()

    def test_reports_in_part_bytes_and_retries_do_not_count_resent_bytes_twice(self):
        from oss2.exceptions import RequestError

        path, bucket, client, response, stop = self.upload_fixture()
        path.write_bytes(b"abcdef")
        attempts = []
        progress = Mock()

        def send_part(_object, _upload, number, data, progress_callback=None):
            attempts.append(number)
            progress_callback(1, 3)
            if len(attempts) == 2:
                raise RequestError(ConnectionError("secret"))
            progress_callback(len(data.read()), 3)
            return SimpleNamespace(etag="etag")

        bucket.upload_part.side_effect = send_part
        with patch("oss2.Bucket", return_value=bucket), patch("oss2.determine_part_size", return_value=3):
            upload_oss(client, path, response, stop, 0, lambda callback: callback(), progress, Mock())
        self.assertEqual(attempts, [1, 2, 2])
        reported = [item.args for item in progress.call_args_list]
        self.assertIn((1, 6), reported)
        self.assertIn((4, 6), reported)
        self.assertEqual(reported[-1], (6, 6))
        self.assertTrue(all(completed <= total == 6 for completed, total in reported))
