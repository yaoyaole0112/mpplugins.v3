"""Safety regressions for the MoviePilot-owned media-version cleanup."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from emetools.media_cleanup import DEFAULT_CONFIG, MediaCleanup, validate_rules
from emetools.p115 import P115DirectoryUnavailable


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

    def test_unavailable_mp_naming_protects_ties_without_hiding_lower_versions(self):
        self.good.unlink()
        self.bad.unlink()
        (self.folder / "Movie.2160p.DolbyVision.strm").write_text("best-a", encoding="utf-8")
        (self.folder / "Movie.2160p.DoVi.strm").write_text("best-b", encoding="utf-8")
        (self.folder / "Movie.1080p.HDR.strm").write_text("inferior-a", encoding="utf-8")
        (self.folder / "Movie.720p.SDR.strm").write_text("inferior", encoding="utf-8")

        with patch.object(self.engine, "_matches_mp_name", side_effect=ValueError("unavailable")):
            result = self.engine.scan(["emby::1"])

        self.assertEqual(result["duplicate_groups"], 1)
        self.assertEqual(result["total_inferior"], 2)
        versions = result["results"][0]["versions"]
        tied_best = [version for version in versions if version["is_best"]]
        inferior = [version for version in versions if not version["is_best"]]
        self.assertEqual(len(tied_best), 2)
        self.assertEqual(len(inferior), 2)
        with self.assertRaises(ValueError):
            self.engine.delete([tied_best[0]["file_path"]])

    def test_identical_rank_versions_are_reported_as_a_duplicate_group(self):
        self.good.unlink()
        self.bad.unlink()
        (self.folder / "Movie.2160p.HEVC.strm").write_text("first", encoding="utf-8")
        (self.folder / "Movie.2160p.H265.strm").write_text("second", encoding="utf-8")

        with patch.object(self.engine, "_matches_mp_name", side_effect=ValueError("unavailable")):
            result = self.engine.scan(["emby::1"])

        self.assertEqual(result["duplicate_groups"], 1)
        self.assertEqual(result["total_inferior"], 0)
        self.assertTrue(all(version["is_best"] for version in result["results"][0]["versions"]))

    def _tie_files(self):
        self.good.unlink()
        self.bad.unlink()
        canonical = self.folder / "Movie.2160p.H265.strm"
        alternative = self.folder / "Movie.2160p.HEVC.strm"
        canonical.write_text("https://example.com/redirect115/111", encoding="utf-8")
        alternative.write_text("https://example.com/redirect115/222", encoding="utf-8")
        return canonical, alternative

    def test_mp_naming_match_wins_over_newer_nonmatching_version(self):
        canonical, alternative = self._tie_files()
        os.utime(canonical, ns=(1000000000, 1000000000))
        os.utime(alternative, ns=(2000000000, 2000000000))
        with patch.object(self.engine, "_matches_mp_name", side_effect=lambda version: version["file_path"] == str(canonical)):
            result = self.engine.scan()
        self.assertEqual(result["results"][0]["versions"][0]["file_path"], str(canonical))
        self.assertEqual(result["total_inferior"], 1)

    def test_matching_versions_keep_only_latest_modification_time(self):
        canonical, alternative = self._tie_files()
        os.utime(canonical, ns=(1000000000, 1000000000))
        os.utime(alternative, ns=(2000000000, 2000000000))
        with patch.object(self.engine, "_matches_mp_name", return_value=True):
            result = self.engine.scan()
        versions = result["results"][0]["versions"]
        self.assertEqual(versions[0]["file_path"], str(alternative))
        self.assertEqual(sum(version["is_best"] for version in versions), 1)

    def test_nonmatching_versions_also_use_latest_date(self):
        canonical, alternative = self._tie_files()
        os.utime(canonical, ns=(2000000000, 2000000000))
        os.utime(alternative, ns=(1000000000, 1000000000))
        with patch.object(self.engine, "_matches_mp_name", return_value=False):
            result = self.engine.scan()
        self.assertEqual(result["results"][0]["versions"][0]["file_path"], str(canonical))
        self.assertEqual(result["total_inferior"], 1)

    def test_quality_wins_without_checking_naming(self):
        with patch.object(self.engine, "_matches_mp_name") as naming:
            result = self.engine.scan()
        naming.assert_not_called()
        self.assertEqual(result["results"][0]["versions"][0]["file_path"], str(self.good))

    def test_mp_naming_uses_local_series_and_episode_metadata(self):
        folder = self.root / "美人余 (2026)" / "Season 1"
        folder.mkdir(parents=True)
        filename = "美人余 - S01E10 - 第 10 集 - 2160p.DoVi.H265.DDP.strm"
        path = folder / filename
        path.write_text("link", encoding="utf-8")
        path.with_suffix(".nfo").write_text("<episodedetails><title>第 10 集</title><aired>2026-10-12</aired></episodedetails>", encoding="utf-8")
        (folder.parent / "tvshow.nfo").write_text("<tvshow><title>美人余</title><year>2026</year><uniqueid type='tmdb'>294446</uniqueid></tvshow>", encoding="utf-8")
        self.engine._scan_root = str(self.root)
        self.owner.chain = MagicMock()
        self.owner.chain.run_module_strict.return_value = f"美人余 (2026)/Season 1/{filename}"
        self.assertTrue(self.engine._matches_mp_name({"file_path": str(path)}))
        call = self.owner.chain.run_module_strict.call_args
        self.assertEqual(call.args, ("recommend_name",))
        self.assertEqual(call.kwargs["mediainfo"].title, "美人余")
        self.assertEqual(str(call.kwargs["mediainfo"].tmdb_id), "294446")
        self.assertEqual(call.kwargs["episodes_info"][0].episode_number, 10)
        self.assertNotIn("raise_exception", call.kwargs)
        self.owner.chain.run_module_strict.return_value = f"美人余 (2026)/Season 1/{filename.replace('DoVi', 'DOVI')}"
        self.assertFalse(self.engine._matches_mp_name({"file_path": str(path)}))

    def test_duplicate_strm_source_does_not_delete_retained_cloud_file(self):
        self.bad.write_text(self.good.read_text(encoding="utf-8"), encoding="utf-8")
        self.engine.scan()
        with patch("emetools.media_cleanup.P115Client") as client:
            result = self.engine.delete([str(self.bad)])
        client.assert_not_called()
        self.assertFalse(result["deleted"][0]["cloud"])
        self.assertTrue(self.good.exists())
        self.assertFalse(self.bad.exists())

    def test_movie_naming_passes_original_container_extension_to_mp(self):
        (self.folder / "movie.nfo").write_text(
            "<movie><title>电影名称</title><year>2025</year></movie>", encoding="utf-8")
        self.owner.chain = MagicMock()
        self.owner.chain.run_module_strict.return_value = "电影名称 (2025)/Movie.2160p.mkv"
        self.engine._scan_root = str(self.root)
        self.assertTrue(self.engine._matches_mp_name(
            {"file_path": str(self.good), "source_extension": ".mkv"}))
        arguments = self.owner.chain.run_module_strict.call_args.kwargs
        self.assertEqual(arguments["mediainfo"].title, "电影名称")
        self.assertTrue(arguments["meta"].title.endswith(".mkv"))
        self.assertEqual(arguments["episodes_info"], [])

    def test_three_matching_versions_and_equal_dates_choose_one_stable_keeper(self):
        canonical, alternative = self._tie_files()
        third = self.folder / "Movie.2160p.x265.strm"
        third.write_text("third", encoding="utf-8")
        for path in (canonical, alternative, third):
            os.utime(path, ns=(1000000000, 1000000000))
        with patch.object(self.engine, "_matches_mp_name", return_value=True):
            first = self.engine.scan()
            second = self.engine.scan()
        self.assertEqual(first["total_inferior"], 2)
        self.assertEqual(first["results"][0]["versions"][0]["file_path"],
                         second["results"][0]["versions"][0]["file_path"])

    def test_naming_change_after_scan_prevents_tied_version_deletion(self):
        canonical, alternative = self._tie_files()
        with patch.object(self.engine, "_matches_mp_name", side_effect=lambda version: version["file_path"] == str(canonical)):
            self.engine.scan()
        with patch.object(self.engine, "_matches_mp_name", side_effect=lambda version: version["file_path"] == str(alternative)), \
                patch("emetools.media_cleanup.P115Client") as client:
            with self.assertRaisesRegex(ValueError, "重新扫描"):
                self.engine.delete([str(alternative)])
        client.assert_not_called()
        self.assertTrue(canonical.exists())
        self.assertTrue(alternative.exists())

    def test_tie_loser_can_be_deleted_after_naming_revalidation(self):
        canonical, alternative = self._tie_files()
        with patch.object(self.engine, "_matches_mp_name", side_effect=lambda version: version["file_path"] == str(canonical)), \
                patch("emetools.media_cleanup.P115Client") as client:
            self.engine.scan()
            client.return_value.__enter__.return_value.delete_files.return_value = {"state": True}
            result = self.engine.delete([str(alternative)])
        self.assertTrue(result["ok"])
        self.assertTrue(canonical.exists())
        self.assertFalse(alternative.exists())

    def test_scan_sorts_chinese_series_by_pinyin_then_numeric_season_episode(self):
        episodes = [("云雀 (2026)", 1, 19), ("云雀 (2026)", 2, 1),
                    ("阿尔法 (2026)", 1, 10), ("云雀 (2026)", 1, 2),
                    ("阿尔法 (2026)", 1, 2)]
        for title, season, episode in episodes:
            folder = self.root / title
            folder.mkdir(exist_ok=True)
            for resolution in ("2160p", "720p"):
                (folder / f"{title} S{season:02}E{episode:02}.{resolution}.strm").write_text(
                    "https://example.com/redirect115/111", encoding="utf-8")
        result = self.engine.scan(["emby::1"])
        names = [item["name"] for item in result["results"] if " S" in item["name"]]
        self.assertEqual(names, ["阿尔法 (2026) S01E02", "阿尔法 (2026) S01E10",
                                 "云雀 (2026) S01E02", "云雀 (2026) S01E19", "云雀 (2026) S02E01"])

    def test_disabled_legacy_rules_are_compared_and_normalized(self):
        self.config["rules"] = [{**item, "enabled": False} for item in self.config["rules"]]
        self.assertTrue(all(rule["enabled"] for rule in validate_rules(self.config["rules"])))
        self.engine._scope_paths(str(self.root), ["emby::1"])
        self.assertIn("正在扫描 STRM 目录（", self.engine.progress)
        self.assertEqual(self.engine.scan()["total_inferior"], 1)

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

    def test_ungrouped_season_strm_does_not_invalidate_unchanged_inventory(self):
        season = self.folder / 'Season 2'
        season.mkdir()
        good = season / 'Show.S02E01.2160p.strm'
        bad = season / 'Show.S02E01.720p.strm'
        ignored = season / '.mkv(1).strm'
        good.write_text('https://example.com/redirect115/111', encoding='utf-8')
        bad.write_text('https://example.com/redirect115/222', encoding='utf-8')
        ignored.write_text('unrecognized', encoding='utf-8')
        self.engine.scan()
        self.assertIn(ignored.name, self.engine._scan_inventory[str(season)])
        self.assertNotIn(str(ignored), self.engine._snapshot)
        with patch('emetools.media_cleanup.P115Client') as client:
            client.return_value.__enter__.return_value.delete_files.return_value = {'state': True}
            result = self.engine.delete([str(bad)])
        self.assertEqual(len(result['deleted']), 1)
        self.assertFalse(result['failures'])
        self.assertTrue(ignored.exists())
        self.assertTrue(good.exists())

    def test_unchanged_excluded_symlink_is_not_a_directory_change(self):
        link = self.folder / 'link.strm'
        link.symlink_to(self.good)
        self.engine.scan()
        self.assertNotIn(str(link), self.engine._snapshot)
        with patch('emetools.media_cleanup.P115Client') as client:
            client.return_value.__enter__.return_value.delete_files.return_value = {'state': True}
            result = self.engine.delete([str(self.bad)])
        self.assertEqual(len(result['deleted']), 1)
        self.assertTrue(link.is_symlink())

    def test_replacing_candidate_with_symlink_still_prevents_cloud_deletion(self):
        self.engine.scan()
        self.bad.unlink()
        self.bad.symlink_to(self.good)
        with patch('emetools.media_cleanup.P115Client') as client:
            with self.assertRaises(ValueError):
                self.engine.delete([str(self.bad)])
        client.assert_not_called()

    def test_removing_strm_after_scan_still_prevents_cloud_deletion(self):
        other = self.folder / 'Movie.480p.strm'
        other.write_text('other', encoding='utf-8')
        self.engine.scan()
        other.unlink()
        with patch('emetools.media_cleanup.P115Client') as client:
            with self.assertRaisesRegex(ValueError, '版本已变化'):
                self.engine.delete([str(self.bad)])
        client.assert_not_called()

    def test_cloud_failure_keeps_local(self):
        self.engine.scan()
        with patch("emetools.media_cleanup.P115Client") as client:
            client.return_value.__enter__.return_value.delete_files.return_value = {"state": False}
            result = self.engine.delete([str(self.bad)])
        self.assertEqual(len(result["failures"]), 1)
        self.assertTrue(self.bad.exists())

    def test_access_restriction_stops_batch_and_keeps_remaining_files(self):
        another = self.folder / "Movie.480p.strm"
        another.write_text("https://example.com/redirect115/333", encoding="utf-8")
        self.engine.scan()
        with patch("emetools.media_cleanup.P115Client") as client:
            remote = client.return_value.__enter__.return_value
            remote.delete_files.side_effect = P115DirectoryUnavailable("HTTP 405；已暂停请求 300 秒")
            result = self.engine.delete([str(self.bad), str(another)])
        remote.delete_files.assert_called_once()
        self.assertFalse(result["deleted"])
        self.assertEqual(len(result["failures"]), 2)
        self.assertTrue(self.bad.exists())
        self.assertTrue(another.exists())

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
