import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.sdk.media import MetaMusic, MusicInfo
from app.schemas.types import MUSIC_ENTITY_ALBUM, MUSIC_ENTITY_RECORDING


PACKAGE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cnmusic_under_test", PACKAGE / "__init__.py",
    submodule_search_locations=[str(PACKAGE)],
)
PLUGIN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PLUGIN
SPEC.loader.exec_module(PLUGIN)
PROVIDER = sys.modules["cnmusic_under_test.provider"]


class RecognitionTests(unittest.TestCase):
    def setUp(self):
        self.meta = MetaMusic.from_dict({
            "title": "少年中国说", "artists": ["张杰"],
            "album": "少年中国说", "year": 2021, "org_string": "少年中国说",
        })

    def test_parsed_tags_survive_normalization(self):
        result = PROVIDER.meta_from_any(self.meta)
        self.assertEqual(result.title, self.meta.title)
        self.assertEqual(result.artists, ["张杰"])
        self.assertEqual(result.album, self.meta.album)
        self.assertEqual(result.year, self.meta.year)
        result.artists.append("其他歌手")
        self.assertEqual(self.meta.artists, ["张杰"])

    def test_search_preserves_artist_in_query(self):
        provider = PROVIDER.CnMusicProvider()
        provider._search_recordings = Mock(return_value=[])
        provider.search(self.meta, media_source=PROVIDER.QQ_SOURCE,
                        music_types=[MUSIC_ENTITY_RECORDING])
        query = provider._search_recordings.call_args.args[1]
        self.assertEqual(query.artists, ["张杰"])

    def test_explicit_recording_does_not_match_album(self):
        provider = PROVIDER.CnMusicProvider()
        provider.search = Mock(return_value=[MusicInfo(
            title="少年中国说", artists=["张杰"], album="少年中国说",
            media_source=PROVIDER.QQ_SOURCE, media_id="album-id",
            music_type=MUSIC_ENTITY_ALBUM,
        )])
        result = provider.match(self.meta, music_type=MUSIC_ENTITY_RECORDING)
        self.assertIsNone(result)
        self.assertEqual(provider.search.call_args.kwargs["music_types"], [MUSIC_ENTITY_RECORDING])

    def test_manual_recognition_preserves_entity_type(self):
        plugin = object.__new__(PLUGIN.CnMusicSource)
        plugin._enabled = True
        plugin._provider = Mock()
        plugin._provider.match.return_value = None
        plugin.recognize_media(meta=self.meta, media_source=PROVIDER.QQ_SOURCE,
                               music_type=MUSIC_ENTITY_ALBUM)
        self.assertEqual(plugin._provider.match.call_args.kwargs["music_type"], MUSIC_ENTITY_ALBUM)

    def test_fallback_event_preserves_artist_and_type(self):
        plugin = object.__new__(PLUGIN.CnMusicSource)
        plugin._enabled = True
        plugin._fallback_on_auto = True
        plugin._provider = Mock()
        plugin._provider.match.return_value = None
        plugin.on_music_media_recognize(SimpleNamespace(event_data={
            "title": "少年中国说", "artists": ["张杰"], "music_type": MUSIC_ENTITY_RECORDING,
        }))
        call = plugin._provider.match.call_args
        self.assertEqual(call.args[0].artists, ["张杰"])
        self.assertEqual(call.kwargs["music_type"], MUSIC_ENTITY_RECORDING)

    def test_disabled_plugin_does_not_search(self):
        plugin = object.__new__(PLUGIN.CnMusicSource)
        plugin._enabled = False
        plugin._provider = Mock()
        self.assertIsNone(plugin.recognize_media(meta=self.meta, media_source=PROVIDER.QQ_SOURCE))
        plugin._provider.match.assert_not_called()


if __name__ == "__main__":
    unittest.main()
