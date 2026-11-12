import ast
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def test_marketplace_python_and_frontend_versions_match(self):
        manifest = json.loads((ROOT.parents[1] / "package.v3.json").read_text(encoding="utf-8"))
        release = manifest["Ed2kFolderWatcher"]
        version = release["version"]
        module = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
        plugin = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "Ed2kFolderWatcher")
        assignment = next(
            node for node in plugin.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "plugin_version" for target in node.targets)
        )
        self.assertEqual(ast.literal_eval(assignment.value), version)
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(package["version"], version)
        self.assertEqual(lock["version"], version)
        self.assertEqual(lock["packages"][""]["version"], version)
        self.assertIn(f"v{version}", release["history"])
