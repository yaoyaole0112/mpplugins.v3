import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from emetools.rapid_upload import RapidUpload, normalize_config, validate_configs


class FakeOwner:
    def __init__(self, cookie="cookie"):
        self.cookie = cookie
        self.data = {}

    def _source_cookie(self):
        return self.cookie

    def get_data(self, key):
        return self.data.get(key)

    def save_data(self, key, value):
        self.data[key] = value


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def upload_init(self, payload=None, **kwargs):
        self.calls.append({**(payload or {}), **kwargs})
        return self.response


class ModernFakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def upload_file_init(self, filename, filesha1, filesize, dirname, read_range_bytes_or_hash, pid):
        self.calls.append({"filename": filename, "filesha1": filesha1, "filesize": filesize,
                           "dirname": dirname, "pid": pid, "read_range_bytes_or_hash": read_range_bytes_or_hash})
        return self.response


class RapidUploadTests(unittest.TestCase):
    def test_config_normalization_and_validation(self):
        config = normalize_config({"name": "电视剧", "media_extensions": ".MP4, mkv,mp4"})
        self.assertEqual(config["media_extensions"], "mp4,mkv")
        self.assertEqual(config["retry_limit"], 2)
        self.assertFalse(config["realtime_enabled"])
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
            self.assertEqual(client.calls[0]["target"], "U_1_123")
            self.assertEqual(rapid.status()["logs"][0]["status"], "succeeded")
            restored = RapidUpload(rapid._owner, {"configs": [config]})
            self.assertEqual(restored.status()["logs"][0]["status"], "succeeded")

    def test_modern_p115_upload_file_init_response_is_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "episode.mkv"
            media.write_bytes(b"modern-upload-api")
            config = normalize_config({"id": "rapid_1", "name": "电视剧", "source_path": str(root),
                                       "target_folder_id": "123", "media_extensions": "mkv"})
            client = ModernFakeClient({"state": True, "data": {"status": 2}})
            rapid = RapidUpload(FakeOwner(), {"configs": [config]})
            with patch.object(rapid, "_p115_client", return_value=client):
                result = rapid.run("rapid_1")
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["succeeded"]), 1)
            self.assertEqual(client.calls[0]["pid"], "U_1_123")

    def test_unmatched_file_is_recorded_without_moving_or_counting_as_failure(self):
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
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["unmatched"]), 1)
            self.assertEqual(result["unmatched"][0]["size"], media.stat().st_size)
            self.assertTrue(media.exists())
            self.assertFalse(failure.exists())
            self.assertEqual(rapid.status()["logs"][0]["status"], "unmatched")
            self.assertIn("未命中", rapid.status()["logs"][0]["message"])

    def test_unmatched_file_size_is_preserved_in_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "movie.mp4"
            media.write_bytes(b"not-found")
            config = normalize_config({"id": "rapid_1", "name": "电影", "source_path": str(root),
                                       "target_folder_id": "123", "media_extensions": "mp4"})
            client = FakeClient({"status": 1, "reuse": False})
            rapid = RapidUpload(FakeOwner(), {"configs": [config]})
            with patch.object(rapid, "_p115_client", return_value=client):
                result = rapid.run("rapid_1")
            self.assertTrue(result["ok"])
            self.assertTrue(media.exists())
            self.assertEqual(result["unmatched"][0]["size"], media.stat().st_size)
            self.assertEqual(rapid.status()["logs"][0]["size"], media.stat().st_size)

    def test_successful_files_are_not_uploaded_again_by_later_scans(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "movie.mp4"
            media.write_bytes(b"already uploaded")
            config = normalize_config({"id": "rapid_1", "name": "电影", "source_path": str(root),
                                       "target_folder_id": "123", "media_extensions": "mp4"})
            owner = FakeOwner()
            client = FakeClient({"status": 2, "reuse": True})
            rapid = RapidUpload(owner, {"configs": [config]})
            with patch.object(rapid, "_p115_client", return_value=client):
                first = rapid.run("rapid_1")
                second = rapid.run("rapid_1")
            self.assertEqual(len(first["succeeded"]), 1)
            self.assertEqual(second["skipped"][0]["path"], str(media))
            self.assertEqual(len(client.calls), 1)

    def test_realtime_status_explains_missing_source_directory(self):
        config = normalize_config({"id": "rapid_1", "name": "电视剧", "realtime_enabled": True})
        rapid = RapidUpload(FakeOwner(), {"configs": [config]})
        with patch("emetools.rapid_upload.Observer", object()), patch(
                "emetools.rapid_upload.FileSystemEventHandler", object()):
            rapid.refresh_realtime_watchers()
        state = rapid.status()
        self.assertEqual(state["realtime_watched_configs"], [])
        self.assertEqual(state["realtime_unwatched"][0]["reason"], "未设置本地源目录")

    def test_realtime_snapshot_detects_new_and_changed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.mkv"
            first.write_bytes(b"one")
            before = RapidUpload._directory_snapshot(root, "mkv")
            second = root / "second.mkv"
            second.write_bytes(b"two")
            first.write_bytes(b"changed")
            after = RapidUpload._directory_snapshot(root, "mkv")
            changed = {path for path, signature in after.items() if before.get(path) != signature}
            self.assertEqual(changed, {str(first), str(second)})

    def test_realtime_poll_queues_only_new_or_changed_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "existing.mkv"
            existing.write_bytes(b"old")
            config = normalize_config({"id": "rapid_1", "name": "电视剧", "source_path": str(root),
                                       "media_extensions": "mkv", "realtime_enabled": True})
            rapid = RapidUpload(FakeOwner(), {"configs": [config]})
            rapid._poll_snapshots["rapid_1"] = rapid._directory_snapshot(root, "mkv")
            new_file = root / "new.mkv"
            new_file.write_bytes(b"new")
            ignored = root / "notes.txt"
            ignored.write_text("ignore", encoding="utf-8")
            with patch.object(rapid, "_queue_realtime_event") as queue:
                rapid._poll_realtime_once()
            queue.assert_called_once_with("rapid_1", str(new_file))

    def test_realtime_paths_arriving_during_dispatch_are_processed_after_current_batch(self):
        config = normalize_config({"id": "rapid_1", "name": "电视剧", "realtime_enabled": True})
        rapid = RapidUpload(FakeOwner(), {"configs": [config]})
        first_started = threading.Event()
        release_first = threading.Event()
        second_processed = threading.Event()
        dispatched = []

        def callback(config_id, paths):
            dispatched.append((config_id, paths))
            if len(dispatched) == 1:
                first_started.set()
                release_first.wait(timeout=2)
            else:
                second_processed.set()
            return {"ok": True}

        rapid.set_realtime_callback(callback)
        rapid._queue_stable_realtime_paths("rapid_1", ["/source/first.mkv"])
        self.assertTrue(first_started.wait(timeout=2))
        rapid._queue_stable_realtime_paths("rapid_1", ["/source/second.mkv"])
        release_first.set()
        self.assertTrue(second_processed.wait(timeout=2))
        self.assertEqual(dispatched, [
            ("rapid_1", ["/source/first.mkv"]),
            ("rapid_1", ["/source/second.mkv"]),
        ])
        rapid.close()

    def test_realtime_queued_batch_is_retried_after_existing_job_finishes(self):
        config = normalize_config({"id": "rapid_1", "name": "电视剧", "realtime_enabled": True})
        rapid = RapidUpload(FakeOwner(), {"configs": [config]})
        attempts = []
        completed = threading.Event()

        def callback(config_id, paths):
            attempts.append(paths)
            if len(attempts) == 1:
                return {"ok": True, "queued": True}
            completed.set()
            return {"ok": True}

        rapid.set_realtime_callback(callback)
        rapid._queue_stable_realtime_paths("rapid_1", ["/source/queued.mkv"])
        self.assertTrue(completed.wait(timeout=4))
        self.assertEqual(attempts, [
            ["/source/queued.mkv"],
            ["/source/queued.mkv"],
        ])
        rapid.close()

    def test_realtime_batch_retries_when_manual_scan_is_still_running(self):
        config = normalize_config({"id": "rapid_1", "name": "电视剧", "realtime_enabled": True})
        rapid = RapidUpload(FakeOwner(), {"configs": [config]})
        attempts = []
        completed = threading.Event()

        def callback(config_id, paths):
            attempts.append(paths)
            if len(attempts) == 1:
                return {"ok": False, "message": "配置“电视剧”正在执行，请稍后再试"}
            completed.set()
            return {"ok": True}

        rapid.set_realtime_callback(callback)
        rapid._queue_stable_realtime_paths("rapid_1", ["/source/during-manual-scan.mkv"])
        self.assertTrue(completed.wait(timeout=4))
        self.assertEqual(attempts, [
            ["/source/during-manual-scan.mkv"],
            ["/source/during-manual-scan.mkv"],
        ])
        rapid.close()


if __name__ == "__main__":
    unittest.main()
