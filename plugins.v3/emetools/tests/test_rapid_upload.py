import tempfile
import threading
import time
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
        self.assertFalse(config["upload_on_unmatched"])
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

    def test_status_exposes_waiting_files_and_manual_scan_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "episode.mkv"
            media.write_bytes(b"queued")
            config = normalize_config({"id": "rapid_1", "name": "电视剧", "source_path": str(root),
                                       "target_folder_id": "123", "media_extensions": "mkv"})
            rapid = RapidUpload(FakeOwner(), {"configs": [config]})
            rapid.queue_scan("rapid_1", [str(media)])
            state = rapid.status()
            self.assertEqual(state["activity_total"], 1)
            self.assertEqual(state["activity"][0]["status"], "queued")
            self.assertEqual(state["activity"][0]["name"], media.name)
            self.assertEqual(state["activity"][0]["size"], media.stat().st_size)
            rapid.clear_scan_queue("rapid_1", [str(media)])
            rapid.queue_scan("rapid_1")
            self.assertEqual(rapid.status()["activity"][0]["status"], "queued")
            self.assertIn("扫描任务", rapid.status()["activity"][0]["name"])

    def test_status_shows_current_file_while_hash_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "episode.mkv"
            media.write_bytes(b"processing")
            config = normalize_config({"id": "rapid_1", "name": "电视剧", "source_path": str(root),
                                       "target_folder_id": "123", "media_extensions": "mkv"})
            rapid = RapidUpload(FakeOwner(), {"configs": [config]})
            hash_started = threading.Event()
            release_hash = threading.Event()
            finished = threading.Event()
            results = []

            def slow_hash(_path):
                hash_started.set()
                release_hash.wait(timeout=2)
                return "ABC123", media.stat().st_size

            client = FakeClient({"status": 2, "reuse": True})

            def run():
                with patch.object(rapid, "_p115_client", return_value=client), \
                        patch.object(RapidUpload, "_sha1", side_effect=slow_hash):
                    results.append(rapid.run("rapid_1"))
                finished.set()

            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(hash_started.wait(timeout=1))
            state = rapid.status()
            self.assertEqual(state["activity"][0]["status"], "processing")
            self.assertEqual(state["activity"][0]["path"], str(media))
            release_hash.set()
            self.assertTrue(finished.wait(timeout=2))
            worker.join(timeout=1)
            self.assertTrue(results[0]["ok"])
            self.assertEqual(rapid.status()["activity"], [])

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

    def test_unmatched_file_can_fall_back_to_normal_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "movie.mp4"
            media.write_bytes(b"not-found")
            config = normalize_config({"id": "rapid_1", "name": "电影", "source_path": str(root),
                                       "target_folder_id": "123", "target_folder_path": "/电影",
                                       "media_extensions": "mp4", "upload_on_unmatched": True})
            client = FakeClient({"status": 1, "reuse": False})
            rapid = RapidUpload(FakeOwner(), {"configs": [config]})
            with patch.object(rapid, "_p115_client", return_value=client), \
                    patch.object(rapid, "_upload_original", return_value=object()) as upload:
                result = rapid.run("rapid_1")
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["unmatched"]), 0)
            self.assertEqual(len(result["uploaded"]), 1)
            upload.assert_called_once_with(media, config)
            self.assertEqual(rapid.status()["logs"][0]["status"], "uploaded")
            self.assertIn("普通上传成功", rapid.status()["logs"][0]["message"])
            self.assertTrue(media.exists())

    def test_unmatched_normal_upload_failure_uses_failure_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failure = root.parent / f"{root.name}-failure"
            media = root / "movie.mp4"
            media.write_bytes(b"not-found")
            config = normalize_config({"id": "rapid_1", "name": "电影", "source_path": str(root),
                                       "target_folder_id": "123", "target_folder_path": "/电影",
                                       "failure_directory": str(failure), "media_extensions": "mp4",
                                       "upload_on_unmatched": True})
            client = FakeClient({"status": 1, "reuse": False})
            rapid = RapidUpload(FakeOwner(), {"configs": [config]})
            with patch.object(rapid, "_p115_client", return_value=client), \
                    patch.object(rapid, "_upload_original", side_effect=RuntimeError("upload failed")):
                result = rapid.run("rapid_1")
            self.assertFalse(result["ok"])
            self.assertEqual(len(result["failed"]), 1)
            self.assertFalse(media.exists())
            self.assertTrue((failure / media.name).exists())
            self.assertEqual(rapid.status()["logs"][0]["status"], "failed")

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

    def test_realtime_initial_snapshot_does_not_block_plugin_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = normalize_config({"id": "rapid_1", "name": "电视剧", "source_path": str(root),
                                       "media_extensions": "mkv", "realtime_enabled": True})
            rapid = RapidUpload(FakeOwner(), {"configs": [config]})
            snapshot_started = threading.Event()
            release_snapshot = threading.Event()
            snapshot = {str(root / "existing.mkv"): (3, 1)}

            def slow_snapshot(*_args, **_kwargs):
                snapshot_started.set()
                release_snapshot.wait(timeout=2)
                return snapshot

            with patch("emetools.rapid_upload.Observer", None), \
                    patch("emetools.rapid_upload.FileSystemEventHandler", None), \
                    patch.object(RapidUpload, "_directory_snapshot", side_effect=slow_snapshot):
                started = time.monotonic()
                rapid.refresh_realtime_watchers()
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 0.5)
                self.assertTrue(snapshot_started.wait(timeout=1))
                self.assertEqual(rapid._poll_snapshots["rapid_1"], {})
                release_snapshot.set()
                deadline = time.monotonic() + 2
                while rapid._poll_snapshots["rapid_1"] != snapshot and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(rapid._poll_snapshots["rapid_1"], snapshot)
            rapid.close()

    def test_initial_snapshot_queues_files_created_while_baseline_is_scanning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = normalize_config({"id": "rapid_1", "name": "电视剧", "source_path": str(root),
                                       "media_extensions": "mkv", "realtime_enabled": True})
            rapid = RapidUpload(FakeOwner(), {"configs": [config]})
            stop_event = threading.Event()
            new_file = str(root / "created-during-scan.mkv")
            signature = (10, time.time_ns())
            rapid._poll_snapshots["rapid_1"] = {}
            with patch.object(RapidUpload, "_directory_snapshot", return_value={new_file: signature}), \
                    patch.object(rapid, "_queue_realtime_event",
                                 side_effect=lambda *_args: stop_event.set()) as queue:
                rapid._poll_realtime_directories(
                    [(config, root)], stop_event, baseline_started_ns=signature[1] - 1
                )
            queue.assert_called_once_with("rapid_1", new_file)
            rapid.close()

    def test_realtime_dispatch_preserves_paths_after_callback_exception(self):
        config = normalize_config({"id": "rapid_1", "name": "电视剧", "realtime_enabled": True})
        rapid = RapidUpload(FakeOwner(), {"configs": [config]})
        first_attempt = threading.Event()
        second_attempt = threading.Event()
        attempts = []

        def callback(config_id, paths):
            attempts.append((config_id, paths))
            if len(attempts) == 1:
                first_attempt.set()
                raise RuntimeError("temporary failure")
            second_attempt.set()
            return {"ok": True}

        rapid._realtime_callback = callback
        rapid._realtime_dispatch_paths["rapid_1"] = {"/source/new.mkv"}
        rapid._realtime_dispatching.add("rapid_1")
        rapid._drain_realtime_dispatch("rapid_1", rapid._poll_stop)
        self.assertTrue(first_attempt.is_set())
        self.assertTrue(second_attempt.is_set())
        self.assertEqual(attempts, [
            ("rapid_1", ["/source/new.mkv"]),
            ("rapid_1", ["/source/new.mkv"]),
        ])
        rapid.close()

    def test_realtime_specific_path_run_does_not_walk_entire_source_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "episode.mkv"
            media.write_bytes(b"rapid-upload-test")
            config = normalize_config({"id": "rapid_1", "name": "电视剧", "source_path": str(root),
                                       "target_folder_id": "123", "media_extensions": "mkv"})
            rapid = RapidUpload(FakeOwner(), {"configs": [config]})
            client = FakeClient({"status": 2, "reuse": True})
            with patch.object(rapid, "_p115_client", return_value=client), \
                    patch.object(RapidUpload, "_iter_files",
                                 side_effect=AssertionError("realtime path scan walked the whole tree")):
                result = rapid.run("rapid_1", [str(media)])
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["succeeded"][0]["path"], str(media))

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
