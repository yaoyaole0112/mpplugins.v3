"""Exercise real MoviePilot planning with deterministic metadata and no cloud I/O."""

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_routing import (
    LIBRARY, SHARE, STORAGE, defaults, routing,
)

from app.application.directory import DirectoryHelper
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.modules.filemanager import module, transhandler
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.types import MediaType


class NativePlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = routing.install_hooks(transhandler, module)

    def setUp(self):
        self.state.rules = routing.parse_rules(
            defaults.default_config()["rules_json"]
        )
        self.manager = object.__new__(module.FileManagerModule)
        self.values = {
            "title": "测试剧", "year": 2026, "tmdbid": 12345,
            "season": 1, "episode": 2, "season_episode": "S01E02",
            "resourceType": "WEB-DL", "videoFormat": "2160p",
            "effect": "HDR", "videoCodec": "H265", "videoBit": "10bit",
            "audioCodec": "DDP", "fps": "25", "fileExt": ".mkv",
        }
        self.naming_patch = patch.object(
            transhandler.TransHandler, "get_naming_dict",
            return_value=self.values,
        )
        self.events_patch = patch.object(
            transhandler.eventmanager, "send_event", return_value=None
        )
        self.naming_patch.start()
        self.events_patch.start()
        self.addCleanup(self.naming_patch.stop)
        self.addCleanup(self.events_patch.stop)
        self.source_oper = Mock()

    def plan(self, path, media_type=MediaType.TV, storage=STORAGE,
             directory=None, source_type="file", extension="mkv"):
        name = "Test.S01E02.2160p.WEB-DL.mkv"
        item = FileItem(
            storage=STORAGE, type=source_type,
            path=f"/offline-source/{name}", name=name, extension=extension,
        )
        self.values["fileExt"] = f".{extension}"
        return self.manager.plan_transfer(
            fileitem=item,
            meta=MetaInfo(name),
            mediainfo=MediaInfo(
                type=media_type, title="测试剧", year="2026", tmdb_id=12345
            ),
            target_directory=directory,
            target_storage=storage,
            target_path=Path(path),
            transfer_type="copy",
            source_oper=self.source_oper,
            preview=True,
        )

    def test_share_and_library_tv_paths(self):
        share = self.plan(SHARE)
        library = self.plan(LIBRARY)
        self.assertEqual(
            share.final_target_path,
            f"{SHARE}/测试剧 (2026)/测试剧(2026)(tmdbid=12345).S01E02."
            "WEB-DL.2160p.HDR.H265.10bit.DDP.25帧-RMGroup.mkv",
        )
        self.assertEqual(
            library.final_target_path,
            f"{LIBRARY}/测试剧 (2026)/Season 1/测试剧 - S01E02 - 第 2 集"
            " - 2160p.HDR.H265.DDP.mkv",
        )
        self.assertTrue(share.preview)
        self.assertTrue(library.preview)
        self.assertEqual(self.source_oper.mock_calls, [])

    def test_movie_uses_movie_template(self):
        checkpoint = self.plan(LIBRARY, MediaType.MOVIE)
        self.assertEqual(
            checkpoint.final_target_path,
            f"{LIBRARY}/测试剧 (2026)/测试剧 - 2160p.HDR.H265.DDP.mkv",
        )

    def test_native_root_inference_uses_selected_template(self):
        original = DirectoryHelper.get_media_root_path
        with patch.object(
            DirectoryHelper, "get_media_root_path", wraps=original
        ) as root_spy:
            checkpoint = self.plan(LIBRARY)
        args, kwargs = root_spy.call_args
        self.assertEqual(args[0], defaults.LIBRARY_TV_TEMPLATE)
        self.assertEqual(
            original(*args, **kwargs), LIBRARY / "测试剧 (2026)"
        )
        self.assertIn("/Season 1/", checkpoint.final_target_path)

    def test_native_type_directory_resolution(self):
        directory = TransferDirectoryConf(
            library_path="/影视资源/已整理",
            library_storage=STORAGE,
            library_type_folder=True,
            library_category_folder=False,
            renaming=True,
            transfer_type="copy",
        )
        checkpoint = self.plan(
            "/ignored", storage=None, directory=directory
        )
        self.assertEqual(checkpoint.root_target_path, str(LIBRARY))
        self.assertIn("/Season 1/", checkpoint.final_target_path)

    def test_renaming_disabled_keeps_original_name(self):
        directory = TransferDirectoryConf(
            library_path=str(LIBRARY), library_storage=STORAGE,
            renaming=False, transfer_type="copy",
        )
        checkpoint = self.plan(LIBRARY, directory=directory)
        self.assertEqual(
            checkpoint.final_target_path,
            f"{LIBRARY}/Test.S01E02.2160p.WEB-DL.mkv",
        )

    def test_unmatched_storage_and_movie_directory_keep_native_template(self):
        for path, storage in (
            (LIBRARY, "local"),
            (Path("/影视资源/已整理/电影"), STORAGE),
        ):
            with self.subTest(path=path, storage=storage):
                self.state.rules = ()
                baseline = self.plan(path, storage=storage).final_target_path
                self.state.rules = routing.parse_rules(
                    defaults.default_config()["rules_json"]
                )
                actual = self.plan(path, storage=storage).final_target_path
                self.assertEqual(actual, baseline)

    def test_subtitle_stays_with_new_season_layout(self):
        checkpoint = self.plan(LIBRARY, extension="srt")
        self.assertIn("/测试剧 (2026)/Season 1/", checkpoint.final_target_path)
        self.assertTrue(checkpoint.final_target_path.endswith(".srt"))
        self.assertEqual(self.source_oper.mock_calls, [])

    def test_directory_preview_does_not_list_source(self):
        checkpoint = self.plan(SHARE, MediaType.MOVIE, source_type="dir")
        self.assertEqual(
            checkpoint.final_target_path, f"{SHARE}/测试剧 (2026)"
        )
        self.assertEqual(checkpoint.items, ())
        self.assertEqual(self.source_oper.mock_calls, [])

    def test_native_library_lookup_scans_series_root_not_season(self):
        directories = [
            TransferDirectoryConf(
                library_path=str(path), library_storage=STORAGE,
                library_category_folder=False, library_type_folder=False,
            )
            for path in (SHARE, LIBRARY)
        ]
        storage = Mock()
        storage.get_item.return_value = None
        with patch.object(
            module.FileManagerModule,
            "_FileManagerModule__get_storage_oper",
            return_value=storage,
        ), patch.object(
            DirectoryHelper, "get_library_dirs", return_value=directories,
        ):
            result = self.manager.media_files(
                MediaInfo(type=MediaType.TV, title="测试剧", year="2026")
            )
        self.assertEqual(result, [])
        self.assertEqual(
            [call.args[0] for call in storage.get_item.call_args_list],
            [SHARE / "测试剧 (2026)", LIBRARY / "测试剧 (2026)"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
