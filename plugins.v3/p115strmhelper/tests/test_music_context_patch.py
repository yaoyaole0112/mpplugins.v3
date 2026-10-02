import importlib.util
from inspect import getattr_static
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.chain.media import MediaChain
from app.chain.transfer.filter import FileFilterMixin
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.schemas.types import MediaType


spec = importlib.util.spec_from_file_location(
    "p115_music_context_test", Path(__file__).resolve().parents[1] / "patch/music_context.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TestMusicContextPatch(TestCase):
    def tearDown(self):
        module.MusicContextPatcher.disable()

    def test_modern_host_is_unchanged(self):
        def modern(cls, history, path, *, storage="local", batch_mtype=None):
            return history, path

        with patch.object(FileFilterMixin, "_restore_music_download_context", classmethod(modern)):
            original = getattr_static(FileFilterMixin, "_restore_music_download_context")
            module.MusicContextPatcher.enable()
            self.assertIs(getattr_static(FileFilterMixin, "_restore_music_download_context"), original)

    def test_legacy_host_patch_is_idempotent_and_restored(self):
        def legacy(cls, download_history, file_path):
            return download_history, file_path

        original = classmethod(legacy)
        with patch.object(FileFilterMixin, "_restore_music_download_context", original):
            module.MusicContextPatcher.enable()
            replacement = getattr_static(FileFilterMixin, "_restore_music_download_context")
            self.assertIs(FileFilterMixin._restore_music_download_context.__self__, FileFilterMixin)
            module.MusicContextPatcher.enable()
            self.assertIs(getattr_static(FileFilterMixin, "_restore_music_download_context"), replacement)
            self.assertEqual(
                FileFilterMixin._restore_music_download_context(
                    None, Path("/cloud/episode.mkv"), storage="115网盘", batch_mtype=MediaType.TV
                ),
                (None, None),
            )
            module.MusicContextPatcher.disable()
            self.assertIs(getattr_static(FileFilterMixin, "_restore_music_download_context"), original)

    def test_disable_does_not_overwrite_another_patch(self):
        def legacy(cls, download_history, file_path):
            return None, None

        with patch.object(FileFilterMixin, "_restore_music_download_context", classmethod(legacy)):
            module.MusicContextPatcher.enable()
            another = classmethod(legacy)
            FileFilterMixin._restore_music_download_context = another
            module.MusicContextPatcher.disable()
            self.assertIs(getattr_static(FileFilterMixin, "_restore_music_download_context"), another)

    def test_cloud_audio_preserves_storage(self):
        history = SimpleNamespace(
            type=MediaType.MUSIC.value, path="/cloud",
            note={"music": {"version": 1, "meta": {}, "media": {}}},
        )
        with patch.object(MediaChain, "is_audio_path", return_value=True), \
                patch.object(MediaChain, "read_path_meta", return_value=SimpleNamespace(title="Track")) as reader, \
                patch.object(MetaMusic, "from_dict", return_value=SimpleNamespace()), \
                patch.object(MusicInfo, "from_dict", return_value=SimpleNamespace()):
            metadata, identity = module._restore_music_download_context(
                FileFilterMixin, history, Path("/cloud/track.flac"),
                storage="115网盘", batch_mtype=MediaType.MUSIC, discard_saved_identity=True,
            )
        reader.assert_called_once_with(Path("/cloud/track.flac"), storage="115网盘")
        self.assertEqual(metadata.title, "Track")
        self.assertIsNone(identity)
