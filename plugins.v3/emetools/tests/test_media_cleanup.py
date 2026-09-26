"""Safety regressions for the MoviePilot-owned media-version cleanup."""

import copy
import json
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

    def test_mediainfo_keeper_video_parameters_and_dovi_base_layer(self):
        sidecar = self.folder / "Movie.2160p-mediainfo.json"
        sidecar.write_text(json.dumps([{"MediaSourceInfo": {
            "Size": 2684354560, "Bitrate": 9000000, "MediaStreams": [{
                "Type": "Video", "Codec": "hevc", "Width": 1606, "Height": 3840,
                "VideoRange": "HDR10", "RealFrameRate": 25,
            }],
        }}]), encoding="utf-8")
        metadata = self.engine._metadata(str(self.folder), "Movie.2160p")
        self.assertEqual(metadata["resolution"], "4k")
        self.assertEqual(metadata["codec"], "hevc")
        self.assertEqual(metadata["fps"], "25")
        self.assertEqual(metadata["bitrate"], 9000000)
        self.assertEqual(metadata["size"], 2684354560)
        self.good.rename(self.folder / "Movie.2160p.DoVi.strm")
        (self.folder / "Movie.2160p.DoVi-mediainfo.json").write_text(sidecar.read_text(encoding="utf-8"), encoding="utf-8")
        result = self.engine.scan()
        self.assertEqual(result["results"][0]["versions"][0]["effect"], "dovi")

    def test_mediainfo_keeper_ffprobe_style(self):
        sidecar = self.folder / "Movie.720p-mediainfo.json"
        sidecar.write_text(json.dumps({"streams": [{"codec_type": "video", "codec_name": "h264",
            "width": 1280, "height": 720, "avg_frame_rate": "24000/1001"}],
            "format": {"size": "104857600", "bit_rate": "1500000"}}), encoding="utf-8")
        metadata = self.engine._metadata(str(self.folder), "Movie.720p")
        self.assertEqual(metadata["fps"], "24")
        self.assertEqual(metadata["codec"], "h264")
        self.assertEqual(metadata["size"], 104857600)

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

    def test_emby_virtual_folders_supply_scan_paths_when_user_views_have_no_path(self):
        del self.engine.libraries  # Exercise the real library discovery method.
        fake = SimpleNamespace(config=SimpleNamespace(name="Emby"), instance=SimpleNamespace(
            get_librarys=lambda **kwargs: [SimpleNamespace(id="1", name="影视", path=None)],
            get_emby_virtual_folders=lambda: [{"Id": "1", "Name": "影视", "Path": [str(self.root)]}]))
        with patch("emetools.media_cleanup.MediaServerHelper") as helper:
            helper.return_value.get_services.return_value = {"emby": fake}
            self.assertEqual(self.engine.libraries()[0]["paths"], [str(self.root)])
            self.assertEqual(self.engine.scan(["Emby::1"])["total_inferior"], 1)


if __name__ == "__main__":
    unittest.main()
