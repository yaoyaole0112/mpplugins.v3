"""Offline routing tests; no file transfer or cloud requests."""

import importlib.util
import json
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

from jinja2 import Template


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


routing = load_module("directoryrename_test_routing", "routing.py")
defaults = load_module("directoryrename_test_defaults", "defaults.py")
STORAGE = "115网盘Plus"
SHARE = Path("/影视资源/分享池/已整理")
LIBRARY = Path("/影视资源/已整理/电视剧")


class MediaType(Enum):
    MOVIE = "电影"
    TV = "电视剧"
    MUSIC = "音乐"


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.rules = routing.parse_rules(defaults.default_config()["rules_json"])
        self.variables = {
            "title": "测试剧",
            "year": 2026,
            "tmdbid": 12345,
            "season": 1,
            "episode": 2,
            "season_episode": "S01E02",
            "resourceType": "WEB-DL",
            "videoFormat": "2160p",
            "effect": "HDR",
            "videoCodec": "H265",
            "videoBit": "10bit",
            "audioCodec": "DDP",
            "fps": "25",
            "fileExt": ".mkv",
        }

    def render(self, path, kind, variables=None):
        template = routing.select_template(self.rules, STORAGE, path, kind)
        self.assertIsNotNone(template)
        return Template(template).render(
            self.variables if variables is None else variables
        )

    def test_share_tv_exact_template(self):
        self.assertEqual(
            self.render(SHARE, MediaType.TV),
            "测试剧 (2026)/测试剧(2026)(tmdbid=12345).S01E02.WEB-DL."
            "2160p.HDR.H265.10bit.DDP.25帧-RMGroup.mkv",
        )

    def test_share_movie_does_not_include_episode(self):
        self.assertEqual(
            self.render(SHARE, MediaType.MOVIE),
            "测试剧 (2026)/测试剧(2026)(tmdbid=12345).WEB-DL."
            "2160p.HDR.H265.10bit.DDP.25帧-RMGroup.mkv",
        )

    def test_library_tv_exact_template(self):
        self.assertEqual(
            self.render(LIBRARY, MediaType.TV),
            "测试剧 (2026)/Season 1/测试剧 - S01E02 - 第 2 集 - "
            "2160p.HDR.H265.DDP.mkv",
        )

    def test_library_movie_exact_template(self):
        self.assertEqual(
            self.render(LIBRARY, MediaType.MOVIE),
            "测试剧 (2026)/测试剧 - 2160p.HDR.H265.DDP.mkv",
        )

    def test_category_subdirectory_matches(self):
        self.assertEqual(
            routing.select_template(
                self.rules, STORAGE, LIBRARY / "国产剧", MediaType.TV
            ),
            defaults.LIBRARY_TV_TEMPLATE,
        )

    def test_wrong_storage_never_matches(self):
        for storage in ("local", "115网盘", "115plus", None):
            with self.subTest(storage=storage):
                self.assertIsNone(
                    routing.select_template(self.rules, storage, SHARE, MediaType.TV)
                )

    def test_other_paths_never_match(self):
        for path in (
            "/影视资源/已整理/电影",
            "/影视资源/已整理/电视剧备份",
            "/影视资源/分享池/已整理备份",
            "/影视资源/已整理",
            "/other",
            "/影视资源/已整理/电视剧/../电影",
            None,
        ):
            with self.subTest(path=path):
                self.assertIsNone(
                    routing.select_template(self.rules, STORAGE, path, MediaType.TV)
                )

    def test_music_and_unknown_media_are_unchanged(self):
        for kind in (MediaType.MUSIC, None, "other"):
            self.assertIsNone(
                routing.select_template(self.rules, STORAGE, LIBRARY, kind)
            )

    def test_missing_optional_fields(self):
        variables = {"title": "测试剧", "fileExt": ".mp4"}
        self.assertEqual(
            self.render(SHARE, MediaType.TV, variables),
            "测试剧/测试剧-RMGroup.mp4",
        )
        self.assertEqual(
            self.render(LIBRARY, MediaType.MOVIE, variables),
            "测试剧/测试剧.mp4",
        )

    def test_trailing_slash_and_duplicate_separators(self):
        self.assertEqual(
            routing.select_template(
                self.rules, STORAGE, "/影视资源//已整理/电视剧/", "电视剧"
            ),
            defaults.LIBRARY_TV_TEMPLATE,
        )

    def test_longest_directory_wins(self):
        raw = json.loads(defaults.default_config()["rules_json"])
        raw.append(
            {
                "storage": STORAGE,
                "path": str(LIBRARY / "特别"),
                "tv": "special/{{title}}{{fileExt}}",
            }
        )
        rules = routing.parse_rules(json.dumps(raw))
        self.assertEqual(
            routing.select_template(
                rules, STORAGE, LIBRARY / "特别" / "国产", MediaType.TV
            ),
            "special/{{title}}{{fileExt}}",
        )

    def test_invalid_configuration_is_rejected(self):
        for raw in (
            "{}",
            "[1]",
            '[{"path": "/test", "tv": "test"}]',
            '[{"storage": "local", "path": "relative", "tv": "test"}]',
            '[{"storage": "local", "path": "/a/../b", "tv": "test"}]',
            '[{"storage": "local", "path": "/a", "tv": "{{broken"}]',
            '[{"storage": "local", "path": "/a", "tv": "/absolute"}]',
            '[{"storage": "local", "path": "/a", "tv": 5}]',
            '[{"storage": "local", "path": "/a"}]',
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(Exception):
                    routing.parse_rules(raw)

    def test_duplicate_path_is_rejected(self):
        rules = json.loads(defaults.default_config()["rules_json"])
        rules.append(dict(rules[0], path=str(SHARE)))
        with self.assertRaises(ValueError):
            routing.parse_rules(json.dumps(rules))


class HookTests(unittest.TestCase):
    def setUp(self):
        self.handler_module = SimpleNamespace()
        self.manager_module = SimpleNamespace()
        handler_module = self.handler_module
        manager_module = self.manager_module

        def native_setting(name, *args, **kwargs):
            if name == "RENAME_FORMAT":
                return lambda kind: f"native:{getattr(kind, 'value', kind)}"
            return "unrelated-setting"

        handler_module.get_runtime_setting = native_setting
        manager_module.get_runtime_setting = native_setting

        class Handler:
            def plan_transfer(
                self, planning_input, *, target_storage, target_path, media_type,
                barrier=None, fail=False, **kwargs,
            ):
                if barrier:
                    barrier.wait(timeout=5)
                if fail:
                    raise RuntimeError("intentional planning failure")
                return handler_module.get_runtime_setting("RENAME_FORMAT")(media_type)

            @staticmethod
            def get_dest_dir(mediainfo, target_dir, **kwargs):
                result = Path(target_dir.library_path)
                if kwargs.get("need_type_folder"):
                    result /= mediainfo.type.value
                return result

            @staticmethod
            def get_dest_path(mediainfo, target_path, **kwargs):
                result = Path(target_path)
                if kwargs.get("need_type_folder"):
                    result /= mediainfo.type.value
                return result

        class Manager:
            def plan_transfer(
                self, fileitem, media_type, target_directory=None,
                target_storage=None, target_path=None, library_type_folder=False,
                **kwargs,
            ):
                storage = (
                    target_storage
                    or getattr(target_directory, "library_storage", None)
                    or fileitem.storage
                    or "local"
                )
                media = SimpleNamespace(type=media_type)
                if target_directory is not None:
                    path = Handler.get_dest_dir(
                        media, target_directory, need_type_folder=library_type_folder
                    )
                else:
                    path = Handler.get_dest_path(
                        media, target_path, need_type_folder=library_type_folder
                    )
                return Handler().plan_transfer(
                    None, target_storage=storage, target_path=path,
                    media_type=media_type, **kwargs,
                )

            def media_files(self, mediainfo):
                results = []
                for directory in self.directories:
                    Handler.get_dest_dir(mediainfo, directory)
                    results.append(
                        manager_module.get_runtime_setting("RENAME_FORMAT")(
                            mediainfo.type
                        )
                    )
                return results

        handler_module.TransHandler = Handler
        manager_module.FileManagerModule = Manager
        self.state = routing.install_hooks(handler_module, manager_module)
        self.state.rules = routing.parse_rules(
            defaults.default_config()["rules_json"]
        )
        self.manager = Manager()

    def plan(self, storage=STORAGE, path=LIBRARY, kind=MediaType.TV, **kwargs):
        return self.manager.plan_transfer(
            SimpleNamespace(storage="local"),
            target_storage=storage, target_path=path, media_type=kind, **kwargs
        )

    def test_planning_receives_selected_template(self):
        self.assertEqual(self.plan(), defaults.LIBRARY_TV_TEMPLATE)

    def test_nonmatching_plan_preserves_original(self):
        self.assertEqual(self.plan(storage="local"), "native:电视剧")
        self.assertEqual(self.plan(path="/影视资源/已整理/电影"), "native:电视剧")
        self.assertEqual(self.plan(kind=MediaType.MUSIC), "native:音乐")

    def test_scope_does_not_leak_to_unspecified_previews(self):
        self.plan()
        self.assertIsNone(self.state.target.get())
        self.assertEqual(
            self.handler_module.get_runtime_setting("RENAME_FORMAT")(MediaType.TV),
            "native:电视剧",
        )

    def test_scope_resets_after_failure(self):
        with self.assertRaises(RuntimeError):
            self.plan(fail=True)
        self.assertIsNone(self.state.target.get())

    def test_parallel_destinations_are_isolated(self):
        barrier = Barrier(3)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(self.plan, path=SHARE, barrier=barrier),
                pool.submit(self.plan, path=LIBRARY, barrier=barrier),
                pool.submit(self.plan, storage="local", barrier=barrier),
            ]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(
            results,
            [defaults.SHARE_TEMPLATE, defaults.LIBRARY_TV_TEMPLATE, "native:电视剧"],
        )
        self.assertIsNone(self.state.target.get())

    def test_library_lookup_switches_per_destination(self):
        manager = self.manager_module.FileManagerModule()
        manager.directories = [
            SimpleNamespace(library_storage=STORAGE, library_path=str(SHARE)),
            SimpleNamespace(library_storage=STORAGE, library_path=str(LIBRARY)),
            SimpleNamespace(library_storage="local", library_path=str(LIBRARY)),
            SimpleNamespace(
                library_storage=STORAGE, library_path="/影视资源/已整理/电影"
            ),
        ]
        self.assertEqual(
            manager.media_files(SimpleNamespace(type=MediaType.TV)),
            [
                defaults.SHARE_TEMPLATE,
                defaults.LIBRARY_TV_TEMPLATE,
                "native:电视剧",
                "native:电视剧",
            ],
        )
        self.assertIsNone(self.state.target.get())
        self.assertIsNone(self.state.lookup.get())

    def test_hot_reload_does_not_stack_wrappers(self):
        plan = self.manager_module.FileManagerModule.plan_transfer
        getter = self.handler_module.get_runtime_setting
        state = routing.install_hooks(self.handler_module, self.manager_module)
        self.assertIs(state, self.state)
        self.assertIs(plan, self.manager_module.FileManagerModule.plan_transfer)
        self.assertIs(getter, self.handler_module.get_runtime_setting)

    def test_disabling_restores_native_behavior(self):
        self.state.rules = ()
        self.assertEqual(self.plan(), "native:电视剧")

    def test_other_settings_are_unchanged(self):
        self.assertEqual(
            self.handler_module.get_runtime_setting("RMT_SUBEXT"),
            "unrelated-setting",
        )

    def test_later_third_party_wrapper_is_preserved(self):
        original = self.handler_module.TransHandler.plan_transfer
        calls = []

        def other_wrapper(*args, **kwargs):
            calls.append(True)
            return original(*args, **kwargs)

        self.handler_module.TransHandler.plan_transfer = other_wrapper
        routing.install_hooks(self.handler_module, self.manager_module)
        self.assertEqual(self.plan(), defaults.LIBRARY_TV_TEMPLATE)
        self.assertEqual(calls, [True])
        self.state.rules = ()
        self.assertEqual(self.plan(), "native:电视剧")

    def test_directory_selected_storage_and_path(self):
        directory = SimpleNamespace(
            library_storage=STORAGE, library_path=str(LIBRARY)
        )
        self.assertEqual(
            self.plan(storage=None, path="/ignored", target_directory=directory),
            defaults.LIBRARY_TV_TEMPLATE,
        )
        self.assertEqual(
            self.plan(storage="local", target_directory=directory),
            "native:电视剧",
        )

    def test_type_subdirectory_resolved_before_template_selection(self):
        self.assertEqual(
            self.plan(
                path="/影视资源/已整理", library_type_folder=True
            ),
            defaults.LIBRARY_TV_TEMPLATE,
        )
        directory = SimpleNamespace(
            library_storage=STORAGE, library_path="/影视资源/已整理"
        )
        self.assertEqual(
            self.plan(
                storage=None, target_directory=directory, library_type_folder=True
            ),
            defaults.LIBRARY_TV_TEMPLATE,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
