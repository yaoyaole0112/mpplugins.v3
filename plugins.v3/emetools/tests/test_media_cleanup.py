"""Safety regressions for the MoviePilot-owned media-version cleanup."""

import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from emetools.media_cleanup import DEFAULT_CONFIG, MediaCleanup, validate_rules


class MediaCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "Movie (2025)"
        self.folder.mkdir()
        self.good = self.folder / "Movie.2160p.strm"
        self.bad = self.folder / "Movie.720p.strm"
        self.good.write_text("https://example.com/redirect115/111", encoding="utf-8")
        self.bad.write_text("https://example.com/redirect115/222", encoding="utf-8")
        self.owner = SimpleNamespace(_strm_root=str(self.root), _source_cookie=lambda: "cookie")
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.engine = MediaCleanup(self.owner, self.config)
        self.engine.libraries = lambda: [{"id": "emby::1", "paths": [str(self.root)]}]

    def test_scan_only_marks_inferior_version(self):
        result = self.engine.scan(["emby::1"])
        self.assertEqual(result["total_inferior"], 1)
        self.assertEqual(result["results"][0]["versions"][0]["file_path"], str(self.good))
        with self.assertRaises(ValueError):
            self.engine.delete([str(self.good)])

    def test_tie_never_deletes(self):
        self.config["rules"] = [{**item, "enabled": False} for item in self.config["rules"]]
        self.assertEqual(self.engine.scan()["total_inferior"], 0)

    def test_changed_best_file_prevents_deletion(self):
        self.engine.scan()
        self.good.write_text("changed", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.engine.delete([str(self.bad)])
        self.assertTrue(self.bad.exists())

    def test_new_version_after_scan_prevents_deletion(self):
        self.engine.scan()
        (self.folder / "Movie.4320p.strm").write_text("new", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.engine.delete([str(self.bad)])
        self.assertTrue(self.bad.exists())

    def test_cloud_failure_keeps_local(self):
        self.engine.scan()
        with patch("emetools.media_cleanup.P115Client") as client:
            client.return_value.__enter__.return_value.delete_files.return_value = {"state": False}
            result = self.engine.delete([str(self.bad)])
        self.assertEqual(len(result["failures"]), 1)
        self.assertTrue(self.bad.exists())

    def test_rejects_outside_library_and_invalid_rule(self):
        self.engine.libraries = lambda: [{"id": "emby::1", "paths": ["/not/mounted"]}]
        with self.assertRaises(ValueError):
            self.engine.scan()
        with self.assertRaises(ValueError):
            validate_rules(self.config["rules"][:-1])


if __name__ == "__main__":
    unittest.main()
