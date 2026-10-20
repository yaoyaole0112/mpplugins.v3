"""Cleanup naming regressions through the installed MoviePilot dispatcher."""

import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.chain.base import ChainBase
from app.modules.filemanager import FileManagerModule
from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher

from emetools.media_cleanup import DEFAULT_CONFIG, MediaCleanup


class MediaCleanupDispatcherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "美人余 (2026)" / "Season 1"
        self.folder.mkdir(parents=True)
        (self.folder.parent / "tvshow.nfo").write_text(
            "<tvshow><title>美人余</title><year>2026</year></tvshow>", encoding="utf-8")
        self.canonical = self.folder / "美人余 - S01E10 - 第 10 集 - 2160p.DoVi.H265.DDP.strm"
        self.alternative = self.folder / "美人余 - S01E10 - 第 10 集 - 2160p.DOVI.H265.EAC.strm"
        self.canonical.write_text("first", encoding="utf-8")
        self.alternative.write_text("second", encoding="utf-8")
        os.utime(self.canonical, ns=(1000000000, 1000000000))
        os.utime(self.alternative, ns=(2000000000, 2000000000))
        self.errors = []
        self.owner = SimpleNamespace(_strm_root=str(self.root))
        self.engine = MediaCleanup(self.owner, copy.deepcopy(DEFAULT_CONFIG))
        self.engine.libraries = lambda: [{"id": "emby::1", "paths": [str(self.root)]}]
        template = "{{title}} - {{season_episode}} - 第 {{episode}} 集 - {{videoFormat}}.{{effect}}.{{videoCodec}}{% if audioCodec %}.{{audioCodec}}{% endif %}{{fileExt}}"
        self.naming_setting = patch("app.modules.filemanager.module.get_runtime_setting",
                                    return_value=lambda media_type: template)
        self.naming_setting.start()
        self.addCleanup(self.naming_setting.stop)

    def _bind_chain(self, provider, plugin=False):
        module = SimpleNamespace(get_priority=lambda: 1, get_name=lambda: "FileManager",
                                 recommend_name=provider)
        chain = object.__new__(ChainBase)

        def handle_error(error, *args, **kwargs):
            self.errors.append(error)

        chain._module_dispatcher = ModuleInvocationDispatcher(
            module_catalog=SimpleNamespace(get_running_modules=lambda method: [] if plugin else [module]),
            plugin_catalog=SimpleNamespace(get_plugin_modules=lambda: {
                ("NamingProvider", "NamingProvider"): {"recommend_name": provider}} if plugin else {}),
            plugin_error_handler=handle_error, system_error_handler=handle_error,
            rate_limit_handler=handle_error)
        self.owner.chain = chain

    def _assert_single_keeper(self, expected):
        result = self.engine.scan()
        self.assertEqual(result["duplicate_groups"], 1)
        self.assertEqual(result["total_inferior"], 1)
        versions = result["results"][0]["versions"]
        self.assertEqual([version["file_path"] for version in versions if version["is_best"]], [str(expected)])
        self.assertEqual(self.errors, [])

    def test_real_naming_module_through_host_dispatcher_prefers_matching_name(self):
        self._bind_chain(FileManagerModule.recommend_name)
        self._assert_single_keeper(self.canonical)

    def test_real_naming_module_through_plugin_dispatcher_prefers_matching_name(self):
        self._bind_chain(FileManagerModule.recommend_name, plugin=True)
        self._assert_single_keeper(self.canonical)

    def test_real_dispatcher_uses_latest_date_when_no_names_match(self):
        def recommend_name(meta, mediainfo, episodes_info=None):
            return "different-name.mkv"

        self._bind_chain(recommend_name)
        self._assert_single_keeper(self.alternative)

    def test_real_dispatcher_uses_latest_date_when_all_names_match(self):
        def recommend_name(meta, mediainfo, episodes_info=None):
            return meta.title

        self._bind_chain(recommend_name)
        self._assert_single_keeper(self.alternative)

    def test_real_provider_error_still_protects_ties_and_reports_lower_version(self):
        def recommend_name(meta, mediainfo, episodes_info=None):
            raise ValueError("provider unavailable")

        for plugin in (False, True):
            with self.subTest(plugin=plugin):
                self.errors.clear()
                self._bind_chain(recommend_name, plugin=plugin)
                inferior = self.folder / "美人余 - S01E10 - 720p.SDR.H264.strm"
                inferior.write_text("third", encoding="utf-8")
                result = self.engine.scan()
                self.assertEqual(result["total_inferior"], 1)
                versions = result["results"][0]["versions"]
                self.assertEqual(sum(version["is_best"] for version in versions), 2)
                self.assertEqual(len(self.errors), 1)
                self.assertIsInstance(self.errors[0], ValueError)


if __name__ == "__main__":
    unittest.main()
