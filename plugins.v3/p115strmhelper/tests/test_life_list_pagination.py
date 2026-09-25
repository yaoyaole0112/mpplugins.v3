import ast
from json import dumps
from pathlib import Path
from unittest import TestCase
from typing import Any, Dict, List
from unittest.mock import Mock


def load_life_puller():
    source = Path(__file__).resolve().parents[1] / "helper/life/client.py"
    tree = ast.parse(source.read_text())
    monitor = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MonitorLife")
    methods = [
        node for node in monitor.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_flatten_life_list", "_pull_life_list"}
    ]
    cls = ast.ClassDef(name="LifePuller", bases=[], keywords=[], body=methods, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    namespace = {
        "Any": Any, "Dict": Dict, "List": List, "dumps": dumps,
        "time": lambda: 2000, "check_response": lambda result: result,
        "BEHAVIOR_NAME_TO_TYPE": {"move_file": 6},
        "BEHAVIOR_TYPE_TO_NAME": {6: "move_file"},
    }
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["LifePuller"]


def response(ids, last_data=None):
    data = {"list": [{"behavior_type": "move_file", "items": [
        {"id": event_id, "update_time": 1900, "type": 6} for event_id in ids
    ]}]}
    if last_data is not None:
        data["last_data"] = last_data
    return {"data": data}


class TestLifeListPagination(TestCase):
    def setUp(self):
        self.puller = load_life_puller()()

    def test_reads_older_pages_before_advancing_checkpoint(self):
        self.puller._client = Mock()
        self.puller._client.life_list.side_effect = [
            response(range(1200, 200, -1), {"last_time": 1900, "last_count": 1000}),
            response(range(200, 100, -1)),
        ]
        events = self.puller._pull_life_list(1900, 150)
        self.assertEqual(len(events), 1050)
        self.assertEqual(events[0]["id"], 1200)
        self.assertEqual(events[-1]["id"], 151)
        self.assertEqual(self.puller._client.life_list.call_args_list[1].args[0]["last_data"],
                         dumps({"last_time": 1900, "last_count": 1000}))

    def test_offset_fallback_when_full_page_has_no_cursor(self):
        self.puller._client = Mock()
        self.puller._client.life_list.side_effect = [
            response(range(1200, 200, -1)), response(range(200, 100, -1)),
        ]
        self.assertEqual(len(self.puller._pull_life_list(1900, 150)), 1050)
        self.assertEqual(self.puller._client.life_list.call_args_list[1].args[0]["start"], 1000)

    def test_duplicate_page_fails_without_advancing_checkpoint(self):
        self.puller._client = Mock()
        self.puller._client.life_list.side_effect = [
            response(range(1200, 200, -1)), response(range(1200, 200, -1)),
        ]
        with self.assertRaises(RuntimeError):
            self.puller._pull_life_list(1900, 150)
