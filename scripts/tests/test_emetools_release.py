import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile


SPEC = importlib.util.spec_from_file_location("emetools_release", Path(__file__).parents[1] / "build_emetools_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class EmeToolsReleaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.plugin = self.root / "plugins.v3/emetools"
        (self.plugin / "dist/assets").mkdir(parents=True)
        (self.plugin / "tests").mkdir()
        (self.plugin / "src").mkdir()
        (self.plugin / "__init__.py").write_text('class EmeTools:\n    plugin_version = "2.10.43"\n')
        (self.plugin / "package.json").write_text('{"version":"2.10.43"}')
        self.manifest = {"EmeTools": {"version": "2.10.43", "release": True,
                                     "history": {"v2.10.43": "官方更新发布包"}}}
        self.save_manifest()
        for filename in ("requirements.txt", "data_enrichment.py", "dist/assets/remoteEntry.js",
                         "dist/assets/Page.js", "dist/assets/Page.css", "tests/test_plugin.py",
                         "src/Page.vue", "private.session", ".env"):
            (self.plugin / filename).write_text("")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)

    def save_manifest(self):
        (self.root / "package.v3.json").write_text(json.dumps(self.manifest))

    def test_asset_matches_official_case_sensitive_tag_and_lowercase_zip(self):
        metadata = release.release_metadata(self.root)
        self.assertEqual(metadata["tag"], "EmeTools_v2.10.43")
        self.assertEqual(metadata["asset"], "emetools_v2.10.43.zip")

    def test_zip_installs_from_root_without_development_or_private_files(self):
        (self.plugin / "untracked.py").write_text("private_data = True")
        archive = release.build_release(self.root, self.root / "output")
        with zipfile.ZipFile(archive) as bundle:
            self.assertIsNone(bundle.testzip())
            self.assertEqual(set(bundle.namelist()), {"__init__.py", "data_enrichment.py", "package.json",
                "requirements.txt", "dist/assets/remoteEntry.js", "dist/assets/Page.js", "dist/assets/Page.css"})

    def test_inconsistent_versions_cannot_be_published(self):
        self.manifest["EmeTools"]["version"] = "2.10.42"
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "版本号不一致"):
            release.release_metadata(self.root)

    def test_release_declaration_and_notes_are_required(self):
        self.manifest["EmeTools"]["release"] = False
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "release=true"):
            release.release_metadata(self.root)
        self.manifest["EmeTools"]["release"] = True
        self.manifest["EmeTools"]["history"] = {}
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "更新说明"):
            release.release_metadata(self.root)

    def test_version_segments_above_99_cannot_be_published(self):
        (self.plugin / "__init__.py").write_text('class EmeTools:\n    plugin_version = "2.10.100"\n')
        with self.assertRaisesRegex(ValueError, "不超过99"):
            release.release_metadata(self.root)

    def test_missing_federation_entry_stops_release(self):
        (self.plugin / "dist/assets/remoteEntry.js").unlink()
        subprocess.run(["git", "add", "-u"], cwd=self.root, check=True)
        with self.assertRaisesRegex(ValueError, "远程组件入口"):
            release.build_release(self.root, self.root / "output")

    def test_symlinks_cannot_be_packaged(self):
        (self.plugin / "data_enrichment.py").unlink()
        (self.plugin / "data_enrichment.py").symlink_to("__init__.py")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            release.build_release(self.root, self.root / "output")


if __name__ == "__main__":
    unittest.main()
