import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from emetools.rapid_transfer import LimitedReader, TransferStopped, UploadUncertain, upload_oss


class RapidTransferTests(unittest.TestCase):
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

            def upload_part(_object, _upload, _number, data):
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

    def test_cancel_before_completion_aborts_parts_without_commit(self):
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
            bucket.complete_multipart_upload.assert_not_called()
            bucket.abort_multipart_upload.assert_called_once()
