import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _load_module():
    app_event = types.ModuleType("app.core.event")
    app_event.Event = object
    app_event.eventmanager = types.SimpleNamespace(register=lambda *_a, **_k: (lambda fn: fn))
    app_log = types.ModuleType("app.log")
    app_log.logger = types.SimpleNamespace(error=lambda *_a, **_k: None, info=lambda *_a, **_k: None)
    app_plugins = types.ModuleType("app.plugins")
    app_plugins._PluginBase = object
    app_message = types.ModuleType("app.schemas.message")
    app_message.Message = object
    app_types = types.ModuleType("app.schemas.types")
    app_types.EventType = types.SimpleNamespace(PluginAction="PluginAction", UserMessage="UserMessage")
    p115client = types.ModuleType("p115client")
    p115client.P115Client = object
    sys.modules.update({
        "app": types.ModuleType("app"),
        "app.core": types.ModuleType("app.core"),
        "app.core.event": app_event,
        "app.log": app_log,
        "app.plugins": app_plugins,
        "app.schemas": types.ModuleType("app.schemas"),
        "app.schemas.message": app_message,
        "app.schemas.types": app_types,
        "p115client": p115client,
    })
    path = Path(__file__).parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("nanshare115_test_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def test_parse_mixed_links(self):
        offline, shares, parsed_share = self.module.parse_input(
            "magnet:?xt=urn:btih:ABC https://115.com/s/Ab12?password=1234"
        )
        self.assertEqual(len(offline), 1)
        self.assertEqual(len(shares), 1)
        self.assertEqual(parsed_share, ("Ab12", "1234"))

    def test_parse_ed2k_and_torrent(self):
        text = "ed2k://|file|demo.mkv|4|0123456789abcdef0123456789abcdef|/ https://example.test/a.torrent"
        offline, _, share = self.module.parse_input(text)
        self.assertEqual(len(offline), 2)
        self.assertIsNone(share)


if __name__ == "__main__":
    unittest.main()
