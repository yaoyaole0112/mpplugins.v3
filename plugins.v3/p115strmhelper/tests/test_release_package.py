import ast
import json
from pathlib import Path
from unittest import TestCase


class TestReleasePackage(TestCase):
    def test_package_initializers_are_not_empty(self) -> None:
        root = Path(__file__).resolve().parents[1]
        initializers = sorted(root.rglob("__init__.py"))
        self.assertTrue(initializers)
        for path in initializers:
            with self.subTest(path=path.relative_to(root)):
                self.assertTrue(
                    path.read_bytes().strip(),
                    "Empty package files can fail to download through the deployment network",
                )

    def test_market_version_matches_plugin(self) -> None:
        root = Path(__file__).resolve().parents[1]
        namespace = {}
        exec(compile((root / "version.py").read_text(encoding="utf-8"), "version.py", "exec"), namespace)
        manifest = json.loads((root.parents[1] / "package.v3.json").read_text(encoding="utf-8"))
        plugin = manifest["P115StrmHelper"]
        self.assertEqual(namespace["VERSION"], plugin["version"])
        self.assertIn("v" + plugin["version"], plugin["history"])

    def test_initializer_syntax_is_valid(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for path in root.rglob("__init__.py"):
            with self.subTest(path=path.relative_to(root)):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
