import ast
import json
from pathlib import Path
from threading import current_thread
from types import ModuleType
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import urlsplit

from p115client import P115Client
from p115pickcode import to_pickcode
from urllib3_future.exceptions import ReadTimeoutError

from test_full_read_retry import load_read_retry


def load_serial_iterator():
    path = Path(__file__).resolve().parents[1] / "helper/strm/full/iterator.py"
    module = ModuleType("_p115_skim_serial_test")
    exec(compile(ast.parse(path.read_text()), str(path), "exec"), module.__dict__)
    return module


class TestFullSkimSerial(TestCase):
    def setUp(self):
        self.iterator = load_serial_iterator()
        self.retry = load_read_retry()
        self.client = P115Client("")
        self.client.__dict__["pickcode_stable_point"] = "0000"
        self.requests = []
        self.metadata_attempts = 0
        self.fail_persistently = False

    def transport(self, **kwargs):
        self.requests.append((urlsplit(kwargs["url"]).path, kwargs, current_thread()))
        path = self.requests[-1][0]
        if path == "/files/downfiles":
            self.assertEqual(kwargs["params"]["per_page"], 500)
            response = {
                "state": True,
                "data": {
                    "list": [{"pc": to_pickcode(2), "pid": "1", "fs": 1024, "sha1": "ABC"}],
                    "has_next_page": False,
                },
            }
        elif path == "/files/file":
            self.assertEqual(kwargs["method"], "POST")
            self.assertEqual(kwargs["data"], {"file_id": "2"})
            self.metadata_attempts += 1
            if self.metadata_attempts == 1 or self.fail_persistently:
                raise ReadTimeoutError(None, path, "Read timed out")
            response = {
                "state": True,
                "data": [{"pick_code": to_pickcode(2), "file_name": "a.mkv"}],
            }
        elif path == "/files/medialist":
            self.assertNotIn("page_size", kwargs)
            self.assertNotIn("max_workers", kwargs)
            response = {
                "state": True,
                "path": [
                    {"cid": "0", "pid": "0", "name": ""},
                    {"cid": "1", "pid": "0", "name": "media"},
                ],
            }
        else:
            self.fail(f"意外的请求路径: {path}")
        return kwargs["parse"](None, json.dumps(response).encode())

    def scan(self, id_to_dirnode=None):
        if id_to_dirnode is None:
            id_to_dirnode = {1: ("media", 0)}
        return self.iterator.iter_files_skim_serial(
            self.client,
            1,
            with_path=True,
            with_ancestors=True,
            id_to_dirnode=id_to_dirnode,
            request=self.retry.retry_read_request,
        )

    def test_real_client_post_metadata_timeout_recovers_without_prefetch(self):
        with patch.object(self.retry, "request", side_effect=self.transport):
            with patch.object(self.retry, "sleep") as sleep:
                items = list(self.scan())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], 2)
        self.assertEqual(items[0]["path"], "/media/a.mkv")
        self.assertEqual(
            [entry["id"] for entry in items[0]["ancestors"]], [0, 1, 2]
        )
        self.assertEqual(
            [entry[0] for entry in self.requests],
            ["/files/downfiles", "/files/file", "/files/file"],
        )
        self.assertTrue(all(entry[2] is current_thread() for entry in self.requests))
        sleep.assert_called_once_with(1)

    def test_persistent_post_failure_stops_without_late_background_requests(self):
        self.fail_persistently = True
        with patch.object(self.retry, "request", side_effect=self.transport):
            with patch.object(self.retry, "sleep") as sleep:
                with self.assertRaises(ReadTimeoutError):
                    list(self.scan())
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.metadata_attempts, 3)
        self.assertTrue(all(entry[2] is current_thread() for entry in self.requests))
        self.assertEqual(sleep.call_count, 2)

    def test_missing_ancestors_are_loaded_on_demand_in_same_thread(self):
        with patch.object(self.retry, "request", side_effect=self.transport):
            with patch.object(self.retry, "sleep"):
                items = list(self.scan(id_to_dirnode={}))
        self.assertEqual(items[0]["path"], "/media/a.mkv")
        self.assertEqual(self.requests[-1][0], "/files/medialist")
        self.assertTrue(all(entry[2] is current_thread() for entry in self.requests))

    def test_page_controls_do_not_leak_into_ancestor_http_requests(self):
        attrs = iter([{"id": 2}])
        with patch.object(self.iterator, "iter_download_nodes", return_value=attrs) as nodes:
            with patch.object(self.iterator, "ensure_path", return_value=attrs) as paths:
                self.assertEqual(list(self.scan()), [{"id": 2}])
        self.assertEqual(nodes.call_args.kwargs["max_workers"], 0)
        self.assertEqual(nodes.call_args.kwargs["page_size"], 500)
        self.assertTrue(paths.call_args.kwargs["path_already"])
        self.assertNotIn("page_size", paths.call_args.kwargs)
        self.assertNotIn("max_workers", paths.call_args.kwargs)
