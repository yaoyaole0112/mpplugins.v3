"""华语音乐元数据源：QQ 音乐 / 网易云，用于整理识别回退。"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.core.event import Event, eventmanager
from app.plugins import _PluginBase
from app.sdk.logging import logger
from app.sdk.media import MetaMusic, MusicInfo, normalize_media_source
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    ChainEventType,
    MediaSource,
    MediaType,
)

from .provider import (
    NETEASE_SOURCE,
    PLUGIN_SOURCES,
    QQ_SOURCE,
    CnMusicProvider,
)


class CnMusicSource(_PluginBase):
    plugin_name = "华语音乐识别"
    plugin_desc = "接入 QQ 音乐 / 网易云元数据。自动整理在 MusicBrainz 失败时回退匹配华语歌曲。"
    plugin_icon = "music.png"
    plugin_version = "1.0.1"
    plugin_author = "helios"
    author_url = "https://github.com/yaoyaole0112/mpplugins.v3"
    plugin_config_prefix = "cnmusicsource_"
    plugin_order = 23
    auth_level = 1

    _enabled = True
    _fallback_on_auto = True
    _prefer_qq = True
    _min_score = 16
    _provider: Optional[CnMusicProvider] = None

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", True))
        self._fallback_on_auto = bool(config.get("fallback_on_auto", True))
        self._prefer_qq = bool(config.get("prefer_qq", True))
        try:
            self._min_score = max(8, int(config.get("min_score") or 16))
        except (TypeError, ValueError):
            self._min_score = 16
        self._provider = CnMusicProvider(prefer_qq=self._prefer_qq) if self._enabled else None
        logger.info(
            f"华语音乐识别插件已{'启用' if self._enabled else '停用'}，"
            f"自动回退={'开' if self._fallback_on_auto else '关'}，最低匹配分={self._min_score}"
        )

    def get_state(self) -> bool:
        return bool(self._enabled)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "fallback_on_auto",
                                            "label": "MusicBrainz 失败时自动回退",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "prefer_qq",
                                            "label": "优先 QQ 音乐",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_score",
                                            "label": "自动匹配最低分",
                                            "placeholder": "16",
                                            "type": "number",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "V3 自动整理默认只查 MusicBrainz。开启回退后，"
                                                "MusicBrainz/TheAudioDB/豆瓣音乐未命中时会用 QQ 音乐、网易云补识别。"
                                                "也可在手动整理里把数据源改成 QQ音乐 / 网易云音乐。"
                                                "华语新歌、游戏 OST 在 QQ 音乐通常更全。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": True,
            "fallback_on_auto": True,
            "prefer_qq": True,
            "min_score": 16,
        }

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self):
        self._provider = None

    def get_media_source(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "QQ音乐",
                "media_source": QQ_SOURCE,
                "media_types": [MediaType.MUSIC],
            },
            {
                "name": "网易云音乐",
                "media_source": NETEASE_SOURCE,
                "media_types": [MediaType.MUSIC],
            },
        ]

    def get_module(self) -> Dict[str, Any]:
        return {
            "search_music": self.search_music,
            "recognize_media": self.recognize_media,
            "music_album": self.music_album,
        }

    def search_music(
        self,
        meta: MetaMusic = None,
        limit: int = 20,
        media_source: MediaSource = None,
        music_types: Optional[Iterable[str]] = None,
        **kwargs,
    ) -> Optional[List[MusicInfo]]:
        if not self._enabled or not self._provider:
            return None
        source = normalize_media_source(media_source)
        if source not in PLUGIN_SOURCES:
            return None
        return self._provider.search(
            meta,
            limit=limit,
            media_source=source,
            music_types=music_types,
        )

    def recognize_media(
        self,
        meta: MetaMusic = None,
        mtype: MediaType = None,
        media_source: MediaSource = None,
        media_id: str = None,
        music_type: str = None,
        **kwargs,
    ) -> Optional[MusicInfo]:
        if not self._enabled or not self._provider:
            return None
        if mtype and mtype != MediaType.MUSIC and not isinstance(meta, MetaMusic):
            return None
        source = normalize_media_source(media_source)
        if source not in PLUGIN_SOURCES:
            return None
        return self._provider.recognize(
            media_source=source,
            media_id=str(media_id or ""),
            music_type=music_type,
            meta=meta if isinstance(meta, MetaMusic) else None,
        )

    def music_album(
        self,
        media_source: MediaSource = None,
        media_id: str = None,
        **kwargs,
    ):
        if not self._enabled or not self._provider:
            return None
        source = normalize_media_source(media_source)
        if source not in PLUGIN_SOURCES or not media_id:
            return None
        return self._provider.get_album(source, str(media_id))

    @eventmanager.register(ChainEventType.MusicMediaRecognize)
    def on_music_media_recognize(self, event: Event):
        """MusicBrainz 等内置源未给出身份时，用 QQ/网易云补识别。"""
        if not self._enabled or not self._fallback_on_auto or not self._provider:
            return
        if not event or event.event_data is None:
            return
        data = event.event_data
        if self._event_get(data, "mediainfo"):
            return
        source = normalize_media_source(self._event_get(data, "media_source"))
        if source in PLUGIN_SOURCES:
            return
        if source and source not in (
            MediaSource.MusicBrainz,
            MediaSource.TheAudioDB,
            MediaSource.DoubanMusic,
        ):
            return
        title = self._event_get(data, "title")
        artists = self._event_get(data, "artists") or []
        if isinstance(artists, str):
            artists = [artists]
        album = self._event_get(data, "album")
        year = self._event_get(data, "year")
        music_type = self._event_get(data, "music_type") or MUSIC_ENTITY_RECORDING
        if not title:
            return
        meta = MetaMusic.from_dict(
            {
                "title": title,
                "artists": list(artists),
                "album": album,
                "year": year,
                "org_string": title,
            }
        )
        matched = self._provider.match(
            meta,
            music_type=music_type if music_type in (MUSIC_ENTITY_RECORDING, MUSIC_ENTITY_ALBUM) else None,
            extra_title=title,
            min_score=self._min_score,
        )
        if not matched:
            logger.info(f"华语音乐识别未命中：{title} / {artists}")
            return
        payload = matched.to_dict()
        if hasattr(data, "mediainfo"):
            data.mediainfo = payload
        elif isinstance(data, dict):
            data["mediainfo"] = payload
        logger.info(
            f"华语音乐识别命中：{matched.title} - {matched.artist} "
            f"({matched.media_source}:{matched.media_id})"
        )

    @staticmethod
    def _event_get(data: Any, key: str, default=None):
        if data is None:
            return default
        if isinstance(data, dict):
            return data.get(key, default)
        return getattr(data, key, default)
