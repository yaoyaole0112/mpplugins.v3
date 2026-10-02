from copy import deepcopy
from inspect import Parameter, getattr_static, signature
from pathlib import Path
from threading import RLock
from typing import Optional

from app.application.history import DownloadHistorySnapshot
from app.chain.media import MediaChain
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.schemas.types import MUSIC_ENTITY_ALBUM, MUSIC_ENTITY_RECORDING, MediaType
from app.sdk.logging import logger


def _restore_music_download_context(cls, download_history: Optional[DownloadHistorySnapshot], file_path: Path, discard_recording_identity: bool=False, discard_saved_identity: bool=False, *, storage: Optional[str]='local', batch_mtype: Optional[MediaType]=None) -> tuple[Optional[MetaMusic], Optional[MusicInfo]]:
    """从下载历史恢复音乐上下文，并用当前音频标签覆盖曲目级字段。

        种子未提供的语义字段由历史中已选媒体补缺；实体类型和来源身份始终
        沿用已选媒体，不根据专辑名或文件曲名在单曲与专辑之间转换。
        多音轨批次误带单曲身份时只保留文件自身标签，避免把同一 recording
        身份传播到整张专辑；调用方随后可使用目录级证据重新匹配专辑。
        手动选中多条历史且未要求复用历史身份时，专辑身份也必须丢弃，确保
        目录级识别能够重新补齐发行版、分类和规范名称。
        """
    if batch_mtype in (MediaType.MOVIE, MediaType.TV) or not MediaChain.is_audio_path(file_path):
        return (None, None)
    if getattr(download_history, 'type', None) in (MediaType.MOVIE.value, MediaType.TV.value):
        return (None, None)
    root_text = getattr(download_history, 'path', None)
    if root_text and Path(root_text) != file_path and (not file_path.is_relative_to(Path(root_text))):
        return (None, None)
    note = getattr(download_history, 'note', None)
    music_note = note.get('music') if isinstance(note, dict) else None
    if not isinstance(music_note, dict) or music_note.get('version') != 1:
        return (None, None)
    try:
        saved_meta = MetaMusic.from_dict(music_note.get('meta') or {})
        saved_info = MusicInfo.from_dict(music_note.get('media') or {})
    except (TypeError, ValueError):
        return (None, None)
    file_tags = MediaChain.read_path_meta(file_path, storage=storage)
    should_discard_identity = discard_saved_identity or (discard_recording_identity and saved_info.music_type == MUSIC_ENTITY_RECORDING)
    if should_discard_identity:
        file_meta = deepcopy(file_tags)
        file_meta.org_string = file_path.name
        file_meta.title = file_meta.title or file_path.stem
        return (file_meta, None)
    file_meta = deepcopy(saved_meta)
    for field_name in ('artists', 'album', 'album_artist', 'year', 'disc_number', 'track_number', 'total_tracks', 'version', 'isrc'):
        saved_value = getattr(saved_info, field_name, None)
        if getattr(file_meta, field_name, None) in (None, '', []) and saved_value not in (None, '', []):
            setattr(file_meta, field_name, deepcopy(saved_value))
    file_meta.org_string = file_path.name
    if file_tags.title:
        file_meta.title = file_tags.title
    is_album_context = saved_info.music_type == MUSIC_ENTITY_ALBUM
    for field_name in ('artists', 'disc_number', 'track_number', 'total_discs', 'version', 'isrc'):
        if getattr(file_tags, field_name, None):
            setattr(file_meta, field_name, deepcopy(getattr(file_tags, field_name)))
    for field_name in ('album', 'album_artist', 'year', 'total_tracks'):
        file_value = getattr(file_tags, field_name, None)
        if file_value and (not is_album_context or not getattr(file_meta, field_name, None)):
            setattr(file_meta, field_name, deepcopy(file_value))
    for field_name in ('audio_format', 'bit_depth', 'sample_rate', 'bitrate', 'duration'):
        if getattr(file_tags, field_name, None):
            setattr(file_meta, field_name, getattr(file_tags, field_name))
    file_meta.media_source = saved_info.media_source or saved_meta.media_source
    file_meta.media_id = saved_info.media_id or saved_meta.media_id
    file_info = cls._music_info_from_meta(file_meta)
    file_info.media_source = saved_info.media_source
    file_info.media_id = saved_info.media_id
    file_info.music_type = saved_info.music_type
    file_info.artist_ids = list(saved_info.artist_ids)
    file_info.album_id = saved_info.album_id
    file_info.album_type = saved_info.album_type
    file_info.release_date = saved_info.release_date
    file_info.cover_url = saved_info.cover_url
    file_info.lyrics = saved_info.lyrics
    file_info.category = saved_info.category
    file_info.genres = list(saved_info.genres)
    file_info.detail_link = saved_info.detail_link
    file_info.listen_count = saved_info.listen_count
    return (file_meta, file_info)


class MusicContextPatcher:
    """仅为缺少存储参数的旧宿主恢复音乐上下文接口。"""

    _lock = RLock()
    _owner = None
    _original = None
    _replacement = None

    @classmethod
    def enable(cls):
        with cls._lock:
            if cls._owner is not None:
                return
            from app.chain.transfer.filter import FileFilterMixin

            parameters = signature(FileFilterMixin._restore_music_download_context).parameters
            if any(parameter.kind == Parameter.VAR_KEYWORD for parameter in parameters.values()):
                return
            if {"storage", "batch_mtype"}.issubset(parameters):
                return
            cls._original = getattr_static(FileFilterMixin, "_restore_music_download_context")
            cls._replacement = classmethod(_restore_music_download_context)
            FileFilterMixin._restore_music_download_context = cls.__dict__["_replacement"]
            cls._owner = FileFilterMixin
            logger.info("【音乐上下文】旧宿主 storage/batch_mtype 兼容补丁已启用")

    @classmethod
    def disable(cls):
        with cls._lock:
            if cls._owner is None:
                return
            if getattr_static(cls._owner, "_restore_music_download_context") is cls.__dict__["_replacement"]:
                cls._owner._restore_music_download_context = cls.__dict__["_original"]
            cls._owner = None
            cls._original = None
            cls._replacement = None
