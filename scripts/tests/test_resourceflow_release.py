import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_resourceflow_release import build_release, release_metadata


class ResourceFlowReleaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.plugin = self.root / "plugins.v3/resourceflow"
        self.plugin.mkdir(parents=True)
        (self.plugin / "__init__.py").write_text('class ResourceFlow:\n    plugin_version = "1.0.1"\n')
        (self.plugin / "package.json").write_text('{"version":"1.0.1"}')
        self.manifest = {"ResourceFlow": {
            "version": "1.0.1", "release": True, "history": {"v1.0.1": "Global resource policies"},
        }}
        self.save_manifest()
        for name in ("models.py", "channel_dedup.py", "active_dedup.py", "resource_dedup.py",
                     "shared_protocol.py", "shared_coordinator.py", "shared_plugin.py", "shared_models.py",
                     "telegram_rpc.py"):
            (self.plugin / name).touch()
        (self.plugin / "dist/assets").mkdir(parents=True)
        (self.plugin / "dist/assets/remoteEntry.js").write_text("// built")
        (self.plugin / "tests").mkdir()
        (self.plugin / "tests/private.json").write_text("{}")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)

    def save_manifest(self):
        (self.root / "package.v3.json").write_text(json.dumps(self.manifest))

    def test_asset_name_matches_moviepilot_contract(self):
        metadata = release_metadata(self.root)
        self.assertEqual(metadata["tag"], "ResourceFlow_v1.0.1")
        self.assertEqual(metadata["asset"], "resourceflow_v1.0.1.zip")

    def test_archive_contains_runtime_but_not_test_data(self):
        archive = build_release(self.root, self.root / "output")
        with zipfile.ZipFile(archive) as bundle:
            self.assertIn("resource_dedup.py", bundle.namelist())
            self.assertIn("dist/assets/remoteEntry.js", bundle.namelist())
            self.assertNotIn("tests/private.json", bundle.namelist())
            self.assertNotIn("plugins.v3/resourceflow/__init__.py", bundle.namelist())

    def test_version_mismatch_is_rejected(self):
        self.manifest["ResourceFlow"]["version"] = "6.4.2"
        self.save_manifest()
        with self.assertRaises(ValueError):
            release_metadata(self.root)

    def test_missing_notes_are_rejected(self):
        self.manifest["ResourceFlow"]["history"] = {}
        self.save_manifest()
        with self.assertRaises(ValueError):
            release_metadata(self.root)

    def test_missing_runtime_module_is_rejected(self):
        subprocess.run(["git", "rm", "--cached", "plugins.v3/resourceflow/resource_dedup.py"], cwd=self.root, check=True,
                       stdout=subprocess.DEVNULL)
        with self.assertRaises(ValueError):
            build_release(self.root, self.root / "output")

    def test_missing_release_flag_is_rejected(self):
        self.manifest["ResourceFlow"]["release"] = False
        self.save_manifest()
        with self.assertRaises(ValueError):
            release_metadata(self.root)

    def test_symlink_is_rejected(self):
        (self.plugin / "linked.py").symlink_to("models.py")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        with self.assertRaises(ValueError):
            build_release(self.root, self.root / "output")


if __name__ == "__main__":
    unittest.main()
