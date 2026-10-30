import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emetools.rapid_upload import RapidUpload, normalize_config, validate_configs


class FakeOwner:
    def __init__(self, cookie="cookie"):
        self.cookie = cookie

    def _source_cookie(self):
        return self.cookie


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def upload_init(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class RapidUploadTests(unittest.TestCase):
    def test_config_normalization_and_validation(self):
        config = normalize_config({"name": "电视剧", "media_extensions": ".MP4, mkv,mp4"})
        self.assertEqual(config["media_extensions"], "mp4,mkv")
        self.assertEqual(config["retry_limit"], 2)
        self.assertEqual(validate_configs([config])[0]["id"], config["id"])

    def test_run_hashes_only_matching_files_and_reports_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "show" / "episode.mkv"
            media.parent.mkdir()
            media.write_bytes(b"rapid-upload-test")
            (root / "show" / "episode.nfo").write_text("ignored", encoding="utf-8")
            config = normalize_config({"id": "rapid_1", "name": "电视剧", "source_path": str(root),
                                       "target_folder_id": "123", "media_extensions": "mkv"})
            client = FakeClient({"status": 2, "reuse": True})
            rapid = RapidUpload(FakeOwner(), {"configs": [config]})
            with patch.object(rapid, "_p115_client", return_value=client):
                result = rapid.run("rapid_1")
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["succeeded"]), 1)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(client.calls[0]["pid"], "U_1_123")
            self.assertEqual(rapid.status()["logs"][0]["status"], "succeeded")

    def test_unmatched_file_can_move_to_failure_directory_without_uploading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failure = root.parent / f"{root.name}-failure"
            media = root / "movie.mp4"
            media.write_bytes(b"not-found")
            config = normalize_config({"id": "rapid_1", "name": "电影", "source_path": str(root),
                                       "target_folder_id": "123", "failure_directory": str(failure),
                                       "media_extensions": "mp4"})
            client = FakeClient({"status": 1, "reuse": False})
            rapid = RapidUpload(FakeOwner(), {"configs": [config]})
            with patch.object(rapid, "_p115_client", return_value=client):
                result = rapid.run("rapid_1")
            self.assertFalse(result["ok"])
            self.assertEqual(len(result["failed"]), 1)
            self.assertFalse(media.exists())
            self.assertTrue((failure / "movie.mp4").exists())
            self.assertEqual(rapid.status()["logs"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
