"""QQ 音乐 / 网易云音乐元数据检索与匹配。"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional
from urllib.parse import quote

from app.sdk.logging import logger
from app.sdk.media import MetaMusic, MusicInfo, normalize_media_source
from app.sdk.network import RequestUtils
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    MediaSource,
)
from app.domain.context import MusicAlbumInfo


QQ_SOURCE = MediaSource("qqmusic")
NETEASE_SOURCE = MediaSource("netease")
PLUGIN_SOURCES = (QQ_SOURCE, NETEASE_SOURCE)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[\s\-–—−－_/|,，、]+")
_ARTIST_SEP_RE = re.compile(
    r"\s*[;/|｜,，、&＆＋+]\s*|\s*(?:feat\.?|ft\.?|featuring|with|vs\.?)\s+",
    re.I,
)
_ARTIST_PAREN_RE = re.compile(r"^(?P<main>.+?)\s*[\(（](?P<alias>[^\)）]+)[\)）]\s*$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_PAREN_CHUNK_RE = re.compile(r"[\(（][^\)）]*[\)）]")
_COVER_COVER_HINTS = ("cover", "piano", "伴奏", "翻唱", "live", "现场", "beat")
_ARTIST_NOISE = {"feat", "ft", "featuring", "with", "cover", "vs", "and", "x"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _compact(value: Any) -> str:
    return _SPACE_RE.sub("", _text(value)).casefold()


def _tokens(value: Any) -> list[str]:
    return [part for part in _TOKEN_RE.split(_text(value)) if part]


def _core_title(value: Any) -> str:
    """去掉括号版本信息，便于「我的楼兰 (烟嗓版)」匹配「我的楼兰」。"""
    text = _PAREN_CHUNK_RE.sub("", _text(value))
    return _compact(text)


def _looks_cjk_artist_list(value: str) -> bool:
    parts = [part for part in value.split() if part]
    if len(parts) < 2:
        return False
    return all(_CJK_RE.search(part) and not _LATIN_RE.search(part) for part in parts)


def _split_artist_names(names: Iterable[str] | None) -> list[str]:
    """拆开合奏标签：薛之谦 韩红、古巨基;刘涛、Jay Chou (周杰倫)。"""
    result: list[str] = []
    seen: set[str] = set()

    def add(part: Any) -> None:
        name = _text(part)
        key = _compact(name)
        if not key or key in seen or key in _ARTIST_NOISE:
            return
        seen.add(key)
        result.append(name)

    for raw in names or []:
        text_value = _text(raw)
        if not text_value:
            continue
        chunks = [_text(part) for part in _ARTIST_SEP_RE.split(text_value) if _text(part)]
        if len(chunks) <= 1 and _looks_cjk_artist_list(text_value):
            chunks = [_text(part) for part in text_value.split() if _text(part)]
        if not chunks:
            chunks = [text_value]
        for chunk in chunks:
            matched = _ARTIST_PAREN_RE.match(chunk)
            if matched:
                add(matched.group("main"))
                add(matched.group("alias"))
            else:
                add(chunk)
    return result


def _year_of(value: Any) -> Optional[int]:
    text = _text(value)
    if not text:
        return None
    if text.isdigit() and len(text) >= 4:
        year = int(text[:4])
        return year if 1900 <= year <= 2100 else None
    match = re.search(r"(19|20)\d{2}", text)
    if not match:
        return None
    year = int(match.group(0))
    return year if 1900 <= year <= 2100 else None


def _millis_to_seconds(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return round(number / 1000) if number > 10000 else number


def _qq_cover(album_mid: str, size: int = 800) -> Optional[str]:
    if not album_mid:
        return None
    return f"https://y.gtimg.cn/music/photo_new/T002R{size}x{size}M000{album_mid}.jpg"


def _album_type(track_count: Optional[int], genres: Iterable[str] = ()) -> tuple[Optional[str], list[str]]:
    genre_text = " ".join(genres)
    secondary: list[str] = []
    if re.search(r"soundtrack|ost|原声", genre_text, re.I):
        secondary.append("Soundtrack")
    if re.search(r"live|现场", genre_text, re.I):
        secondary.append("Live")
    if track_count == 1 or re.search(r"single|单曲", genre_text, re.I):
        return "Single", secondary
    if re.search(r"\bep\b|迷你", genre_text, re.I):
        return "EP", secondary
    return "Album", secondary


@dataclass
class SearchQuery:
    title: str
    artists: list[str] = field(default_factory=list)
    album: Optional[str] = None
    year: Optional[int] = None
    hints: list[str] = field(default_factory=list)
    original: Optional[str] = None

    @classmethod
    def from_meta(cls, meta: Optional[MetaMusic], extra_title: Optional[str] = None) -> "SearchQuery":
        title = _text(getattr(meta, "title", None) or extra_title)
        artists = _split_artist_names(getattr(meta, "artists", None) or [])
        album = _text(getattr(meta, "album", None)) or None
        original = _text(getattr(meta, "org_string", None) or extra_title or title)
        hints: list[str] = []
        known = {_compact(title), *(_compact(name) for name in artists), _compact(album)}
        for token in _tokens(original):
            compact = _compact(token)
            if compact and compact not in known and compact not in {_compact(item) for item in hints}:
                hints.append(token)
        year = getattr(meta, "year", None)
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        return cls(
            title=title,
            artists=artists,
            album=album,
            year=year,
            hints=hints,
            original=original or None,
        )

    def keywords(self) -> list[str]:
        queries: list[str] = []
        if self.title and self.artists:
            queries.append(f"{self.title} {self.artists[0]}")
        if self.title and self.album:
            queries.append(f"{self.title} {self.album}")
        if self.title:
            queries.append(self.title)
        if self.original and self.original not in queries:
            queries.append(self.original)
        unique: list[str] = []
        seen: set[str] = set()
        for query in queries:
            key = _compact(query)
            if key and key not in seen:
                unique.append(query)
                seen.add(key)
        return unique


class CnMusicProvider:
    """请求 QQ 音乐和网易云公开搜索接口，并按曲名/艺人打分。"""

    def __init__(self, prefer_qq: bool = True) -> None:
        self._request = RequestUtils(timeout=15, ua=_UA)
        self._last_request_at = 0.0
        self._min_interval = 0.25
        self._source_order = [QQ_SOURCE, NETEASE_SOURCE] if prefer_qq else [NETEASE_SOURCE, QQ_SOURCE]

    def _get_json(self, url: str, referer: str) -> Optional[dict[str, Any]]:
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        try:
            response = self._request.get_res(
                url,
                headers={"Referer": referer, "User-Agent": _UA},
            )
            self._last_request_at = time.monotonic()
            if not response or response.status_code != 200:
                logger.warning(f"华语音乐源请求失败：{url} -> {getattr(response, 'status_code', None)}")
                return None
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except Exception as error:
            logger.warning(f"华语音乐源请求异常：{url} -> {error}")
            return None

    def search(
        self,
        meta: Optional[MetaMusic],
        limit: int = 20,
        media_source: Optional[MediaSource] = None,
        music_types: Optional[Iterable[str]] = None,
        extra_title: Optional[str] = None,
    ) -> list[MusicInfo]:
        query = SearchQuery.from_meta(meta, extra_title=extra_title)
        if not query.title and not query.original:
            return []
        wanted = {str(item) for item in (music_types or []) if item}
        sources = self._selected_sources(media_source)
        results: list[MusicInfo] = []
        for source in sources:
            if not wanted or MUSIC_ENTITY_RECORDING in wanted:
                results.extend(self._search_recordings(source, query, limit))
            if not wanted or MUSIC_ENTITY_ALBUM in wanted:
                results.extend(self._search_albums(source, query, limit))
        ranked = sorted(
            results,
            key=lambda info: self.score(info, query),
            reverse=True,
        )
        unique: list[MusicInfo] = []
        seen: set[tuple[str, str, str]] = set()
        for info in ranked:
            identity = (
                str(info.media_source or ""),
                str(info.music_type or ""),
                str(info.media_id or ""),
            )
            if identity in seen:
                continue
            seen.add(identity)
            unique.append(info)
            if len(unique) >= max(1, limit):
                break
        return unique

    def match(
        self,
        meta: Optional[MetaMusic],
        media_source: Optional[MediaSource] = None,
        music_type: Optional[str] = None,
        extra_title: Optional[str] = None,
        min_score: int = 16,
    ) -> Optional[MusicInfo]:
        query = SearchQuery.from_meta(meta, extra_title=extra_title)
        types = [music_type] if music_type else [MUSIC_ENTITY_RECORDING, MUSIC_ENTITY_ALBUM]
        candidates = self.search(
            meta,
            limit=10,
            media_source=media_source,
            music_types=types,
            extra_title=extra_title,
        )
        best: Optional[MusicInfo] = None
        best_score = -1
        for info in candidates:
            score = self.score(info, query)
            if score > best_score:
                best = info
                best_score = score
        if not best or best_score < min_score:
            return None
        if query.artists and not self._artist_hit(best, query.artists):
            return None
        if query.title and not self._title_hit(best, query.title):
            return None
        return best

    def recognize(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
        meta: Optional[MetaMusic] = None,
    ) -> Optional[MusicInfo]:
        source = normalize_media_source(media_source)
        if source not in PLUGIN_SOURCES:
            if meta:
                return self.match(meta, media_source=source, music_type=music_type)
            return None
        if not media_id:
            return self.match(meta, media_source=source, music_type=music_type) if meta else None
        if music_type == MUSIC_ENTITY_ALBUM:
            album = self.get_album(source, media_id)
            return album.to_music_info() if album else None
        if source == QQ_SOURCE:
            info = self._qq_song(media_id)
        else:
            info = self._netease_song(media_id)
        if info:
            return info
        if music_type in (None, MUSIC_ENTITY_ALBUM):
            album = self.get_album(source, media_id)
            return album.to_music_info() if album else None
        return None

    def get_album(self, media_source: MediaSource, media_id: str) -> Optional[MusicAlbumInfo]:
        source = normalize_media_source(media_source)
        if source == QQ_SOURCE:
            return self._qq_album(media_id)
        if source == NETEASE_SOURCE:
            return self._netease_album(media_id)
        return None

    def _selected_sources(self, media_source: Optional[MediaSource]) -> list[MediaSource]:
        source = normalize_media_source(media_source)
        if source in PLUGIN_SOURCES:
            return [source]
        if source in (None, MediaSource.MusicBrainz, MediaSource.TheAudioDB, MediaSource.DoubanMusic):
            return list(self._source_order)
        return []

    def _search_recordings(self, source: MediaSource, query: SearchQuery, limit: int) -> list[MusicInfo]:
        items: list[MusicInfo] = []
        for keyword in query.keywords()[:3]:
            if source == QQ_SOURCE:
                items.extend(self._qq_search_songs(keyword, limit))
            else:
                items.extend(self._netease_search_songs(keyword, limit))
            if any(self.score(info, query) >= 16 and self._artist_hit(info, query.artists) for info in items):
                break
        return items

    def _search_albums(self, source: MediaSource, query: SearchQuery, limit: int) -> list[MusicInfo]:
        items: list[MusicInfo] = []
        keyword = query.album or query.title
        if not keyword:
            return []
        if source == QQ_SOURCE:
            items.extend(self._qq_search_albums(keyword, limit))
        else:
            items.extend(self._netease_search_albums(keyword, limit))
        return items

    def _qq_search_songs(self, keyword: str, limit: int) -> list[MusicInfo]:
        url = (
            "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
            f"?p=1&n={max(1, min(limit, 20))}&w={quote(keyword)}&format=json&t=0"
        )
        payload = self._get_json(url, "https://y.qq.com/")
        songs = (((payload or {}).get("data") or {}).get("song") or {}).get("list") or []
        return [info for item in songs if (info := self._qq_song_from_search(item))]

    def _qq_search_albums(self, keyword: str, limit: int) -> list[MusicInfo]:
        url = (
            "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
            f"?p=1&n={max(1, min(limit, 20))}&w={quote(keyword)}&format=json&t=8"
        )
        payload = self._get_json(url, "https://y.qq.com/")
        albums = (((payload or {}).get("data") or {}).get("album") or {}).get("list") or []
        results: list[MusicInfo] = []
        for item in albums:
            album_mid = _text(item.get("albumMID") or item.get("albumMid"))
            if not album_mid:
                continue
            artists = self._qq_album_artists(item)
            year = _year_of(item.get("publicTime"))
            album_type, secondary = _album_type(None, [_text(item.get("albumName"))])
            results.append(
                MusicInfo(
                    media_source=QQ_SOURCE,
                    media_id=album_mid,
                    music_type=MUSIC_ENTITY_ALBUM,
                    title=_text(item.get("albumName")),
                    artists=artists,
                    album=_text(item.get("albumName")),
                    album_artist=artists[0] if artists else None,
                    year=year,
                    release_date=_text(item.get("publicTime")) or None,
                    cover_url=_text(item.get("albumPic")) or _qq_cover(album_mid),
                    album_type=album_type,
                    secondary_types=secondary,
                    detail_link=f"https://y.qq.com/n/ryqq/albumDetail/{album_mid}",
                    names=[_text(item.get("albumName"))],
                    raw_data={"provider": "qqmusic", "kind": "album", "payload": item},
                )
            )
        return results

    def _qq_song(self, song_mid: str) -> Optional[MusicInfo]:
        url = (
            "https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg"
            f"?songmid={quote(str(song_mid))}&format=json"
        )
        payload = self._get_json(url, "https://y.qq.com/")
        items = (payload or {}).get("data") or []
        if not items:
            return None
        return self._qq_song_from_detail(items[0])

    def _qq_album(self, album_mid: str) -> Optional[MusicAlbumInfo]:
        url = (
            "https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg"
            f"?albummid={quote(str(album_mid))}&format=json"
        )
        payload = self._get_json(url, "https://y.qq.com/")
        data = (payload or {}).get("data") or {}
        if not data:
            return None
        artists = _split_artist_names([_text(data.get("singername"))] if data.get("singername") else [])
        tracks = [
            info for item in (data.get("list") or [])
            if (info := self._qq_song_from_album_track(item, data))
        ]
        album_type, secondary = _album_type(
            _safe_int(data.get("total") or data.get("total_song_num") or len(tracks)),
            [_text(data.get("genre"))],
        )
        return MusicAlbumInfo(
            media_source=QQ_SOURCE,
            media_id=_text(data.get("mid") or album_mid),
            title=_text(data.get("name")),
            artists=artists,
            album_type=album_type,
            secondary_types=secondary,
            release_date=_text(data.get("aDate")) or None,
            cover_url=_qq_cover(_text(data.get("mid") or album_mid)),
            genres=[_text(data.get("genre"))] if data.get("genre") else [],
            detail_link=f"https://y.qq.com/n/ryqq/albumDetail/{_text(data.get('mid') or album_mid)}",
            tracks=tracks,
            raw_data={"provider": "qqmusic", "kind": "album-detail", "payload": data},
        )

    def _qq_song_from_search(self, item: dict[str, Any]) -> Optional[MusicInfo]:
        song_mid = _text(item.get("songmid") or item.get("strMediaMid"))
        if not song_mid:
            return None
        singers = item.get("singer") or []
        artists = _split_artist_names(
            [_text(singer.get("name")) for singer in singers if _text(singer.get("name"))]
        )
        artist_ids = [_text(singer.get("mid") or singer.get("id")) for singer in singers]
        album_mid = _text(item.get("albummid"))
        album_name = _text(item.get("albumname"))
        lyric = _text(item.get("lyric"))
        pubtime = item.get("pubtime")
        release_date = None
        year = None
        if isinstance(pubtime, int) and pubtime > 0:
            try:
                release_date = datetime.fromtimestamp(pubtime).strftime("%Y-%m-%d")
                year = int(release_date[:4])
            except (OSError, ValueError, OverflowError):
                year = None
        tags = [part for part in lyric.split("|") if part]
        album_type, secondary = _album_type(None, tags)
        return MusicInfo(
            media_source=QQ_SOURCE,
            media_id=song_mid,
            music_type=MUSIC_ENTITY_RECORDING,
            title=_text(item.get("songname")),
            artists=artists,
            artist_ids=artist_ids,
            album=album_name or None,
            album_artist=artists[0] if artists else None,
            album_id=album_mid or None,
            album_type=album_type,
            secondary_types=secondary,
            year=year,
            release_date=release_date,
            duration=_safe_int(item.get("interval")),
            cover_url=_qq_cover(album_mid),
            tags=tags,
            title_aliases=[lyric] if lyric else [],
            detail_link=f"https://y.qq.com/n/ryqq/songDetail/{song_mid}",
            names=[_text(item.get("songname")), album_name],
            raw_data={"provider": "qqmusic", "kind": "song-search", "payload": item},
        )

    def _qq_song_from_detail(self, item: dict[str, Any]) -> Optional[MusicInfo]:
        song_mid = _text((item.get("mid") or (item.get("file") or {}).get("media_mid")))
        if not song_mid:
            return None
        singers = item.get("singer") or []
        artists = _split_artist_names(
            [_text(singer.get("name")) for singer in singers if _text(singer.get("name"))]
        )
        album = item.get("album") or {}
        album_mid = _text(album.get("mid"))
        album_name = _text(album.get("name") or album.get("title"))
        subtitle = _text(album.get("subtitle"))
        release_date = _text(album.get("time_public")) or None
        return MusicInfo(
            media_source=QQ_SOURCE,
            media_id=song_mid,
            music_type=MUSIC_ENTITY_RECORDING,
            title=_text(item.get("name") or item.get("title")),
            artists=artists,
            artist_ids=[_text(singer.get("mid")) for singer in singers],
            album=album_name or None,
            album_artist=artists[0] if artists else None,
            album_id=album_mid or None,
            year=_year_of(release_date),
            release_date=release_date,
            duration=_safe_int(item.get("interval")),
            cover_url=_qq_cover(album_mid),
            title_aliases=[subtitle] if subtitle else [],
            tags=[subtitle] if subtitle else [],
            detail_link=f"https://y.qq.com/n/ryqq/songDetail/{song_mid}",
            names=[_text(item.get("name") or item.get("title")), album_name, subtitle],
            raw_data={"provider": "qqmusic", "kind": "song-detail", "payload": item},
        )

    def _qq_song_from_album_track(self, item: dict[str, Any], album: dict[str, Any]) -> Optional[MusicInfo]:
        song_mid = _text(item.get("songmid") or item.get("strMediaMid"))
        if not song_mid:
            return None
        singers = item.get("singer") or []
        artists = _split_artist_names(
            [_text(singer.get("name")) for singer in singers if _text(singer.get("name"))]
        )
        album_mid = _text(album.get("mid"))
        return MusicInfo(
            media_source=QQ_SOURCE,
            media_id=song_mid,
            music_type=MUSIC_ENTITY_RECORDING,
            title=_text(item.get("songname")),
            artists=artists,
            album=_text(album.get("name")) or None,
            album_id=album_mid or None,
            track_number=_safe_int(item.get("belongCD") or item.get("cdIdx")),
            duration=_safe_int(item.get("interval")),
            cover_url=_qq_cover(album_mid),
            detail_link=f"https://y.qq.com/n/ryqq/songDetail/{song_mid}",
            raw_data={"provider": "qqmusic", "kind": "album-track", "payload": item},
        )

    @staticmethod
    def _qq_album_artists(item: dict[str, Any]) -> list[str]:
        singers = item.get("singer_list") or []
        names = _split_artist_names(
            [_text(singer.get("name")) for singer in singers if _text(singer.get("name"))]
        )
        if names:
            return names
        name = _text(item.get("singerName"))
        return _split_artist_names([name] if name else [])

    def _netease_search_songs(self, keyword: str, limit: int) -> list[MusicInfo]:
        url = (
            "https://music.163.com/api/cloudsearch/pc"
            f"?s={quote(keyword)}&type=1&offset=0&limit={max(1, min(limit, 20))}"
        )
        payload = self._get_json(url, "https://music.163.com/")
        songs = ((payload or {}).get("result") or {}).get("songs") or []
        if not songs:
            fallback = (
                "https://music.163.com/api/search/get/web"
                f"?s={quote(keyword)}&type=1&offset=0&limit={max(1, min(limit, 20))}"
            )
            payload = self._get_json(fallback, "https://music.163.com/")
            songs = ((payload or {}).get("result") or {}).get("songs") or []
        return [info for item in songs if (info := self._netease_song_from_search(item))]

    def _netease_search_albums(self, keyword: str, limit: int) -> list[MusicInfo]:
        url = (
            "https://music.163.com/api/cloudsearch/pc"
            f"?s={quote(keyword)}&type=10&offset=0&limit={max(1, min(limit, 20))}"
        )
        payload = self._get_json(url, "https://music.163.com/")
        albums = ((payload or {}).get("result") or {}).get("albums") or []
        results: list[MusicInfo] = []
        for item in albums:
            album_id = _text(item.get("id"))
            if not album_id:
                continue
            artist = item.get("artist") or {}
            artists = _split_artist_names([_text(artist.get("name"))] if artist.get("name") else [])
            pic = _text((item.get("picUrl") or item.get("blurPicUrl")))
            year = _year_of(item.get("publishTime"))
            if isinstance(item.get("publishTime"), int) and item.get("publishTime") > 10_000_000_000:
                year = _year_of(datetime.fromtimestamp(item["publishTime"] / 1000).strftime("%Y-%m-%d"))
            results.append(
                MusicInfo(
                    media_source=NETEASE_SOURCE,
                    media_id=album_id,
                    music_type=MUSIC_ENTITY_ALBUM,
                    title=_text(item.get("name")),
                    artists=artists,
                    album=_text(item.get("name")),
                    album_artist=artists[0] if artists else None,
                    year=year,
                    cover_url=pic or None,
                    detail_link=f"https://music.163.com/#/album?id={album_id}",
                    names=[_text(item.get("name"))],
                    raw_data={"provider": "netease", "kind": "album-search", "payload": item},
                )
            )
        return results

    def _netease_song(self, song_id: str) -> Optional[MusicInfo]:
        url = f"https://music.163.com/api/song/detail?ids=[{quote(str(song_id))}]"
        payload = self._get_json(url, "https://music.163.com/")
        songs = (payload or {}).get("songs") or []
        if not songs:
            return None
        return self._netease_song_from_search(songs[0])

    def _netease_album(self, album_id: str) -> Optional[MusicAlbumInfo]:
        url = f"https://music.163.com/api/album/{quote(str(album_id))}"
        payload = self._get_json(url, "https://music.163.com/")
        album = (payload or {}).get("album") or payload or {}
        if not album.get("id") and not album.get("name"):
            return None
        artists = _split_artist_names([_text((album.get("artist") or {}).get("name"))])
        songs = album.get("songs") or []
        tracks = [info for item in songs if (info := self._netease_song_from_search(item))]
        pic = _text(album.get("picUrl") or album.get("blurPicUrl"))
        publish = album.get("publishTime")
        release_date = None
        if isinstance(publish, int) and publish > 0:
            try:
                release_date = datetime.fromtimestamp(publish / 1000).strftime("%Y-%m-%d")
            except (OSError, ValueError, OverflowError):
                release_date = None
        return MusicAlbumInfo(
            media_source=NETEASE_SOURCE,
            media_id=_text(album.get("id") or album_id),
            title=_text(album.get("name")),
            artists=artists,
            release_date=release_date,
            cover_url=pic or None,
            detail_link=f"https://music.163.com/#/album?id={_text(album.get('id') or album_id)}",
            tracks=tracks,
            raw_data={"provider": "netease", "kind": "album-detail", "payload": album},
        )

    def _netease_song_from_search(self, item: dict[str, Any]) -> Optional[MusicInfo]:
        song_id = _text(item.get("id"))
        if not song_id:
            return None
        artists_payload = item.get("ar") or item.get("artists") or []
        artists = _split_artist_names(
            [_text(artist.get("name")) for artist in artists_payload if _text(artist.get("name"))]
        )
        artist_ids = [_text(artist.get("id")) for artist in artists_payload]
        album = item.get("al") or item.get("album") or {}
        album_name = _text(album.get("name"))
        aliases = [_text(alias) for alias in (item.get("alia") or item.get("alias") or []) if _text(alias)]
        publish = album.get("publishTime") or item.get("publishTime")
        release_date = None
        year = None
        if isinstance(publish, int) and publish > 0:
            stamp = publish / 1000 if publish > 10_000_000_000 else publish
            try:
                release_date = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
                year = int(release_date[:4])
            except (OSError, ValueError, OverflowError):
                year = None
        duration = _millis_to_seconds(item.get("dt") or item.get("duration"))
        return MusicInfo(
            media_source=NETEASE_SOURCE,
            media_id=song_id,
            music_type=MUSIC_ENTITY_RECORDING,
            title=_text(item.get("name")),
            artists=artists,
            artist_ids=artist_ids,
            album=album_name or None,
            album_artist=artists[0] if artists else None,
            album_id=_text(album.get("id")) or None,
            year=year,
            release_date=release_date,
            duration=duration,
            cover_url=_text(album.get("picUrl")) or None,
            title_aliases=aliases,
            detail_link=f"https://music.163.com/#/song?id={song_id}",
            names=[_text(item.get("name")), album_name, *aliases],
            raw_data={"provider": "netease", "kind": "song-search", "payload": item},
        )

    def score(self, info: MusicInfo, query: SearchQuery) -> int:
        score = 0
        title = _compact(info.title)
        expected = _compact(query.title)
        core_title = _core_title(info.title)
        core_expected = _core_title(query.title)
        if expected and title == expected:
            score += 10
        elif core_expected and core_title == core_expected:
            score += 8
        elif expected and expected in title:
            score += 4
        elif expected and title in expected and len(title) >= 2:
            score += 3
        if self._artist_hit(info, query.artists):
            score += 8
        if query.album and _compact(query.album) and _compact(query.album) in _compact(info.album or info.title):
            score += 3
        blob = _compact(
            " ".join(
                [
                    info.title or "",
                    info.album or "",
                    " ".join(info.artists or []),
                    " ".join(info.tags or []),
                    " ".join(info.title_aliases or []),
                ]
            )
        )
        for hint in query.hints:
            if _compact(hint) and _compact(hint) in blob:
                score += 3
        lowered = (info.title or "").casefold()
        if any(hint in lowered for hint in _COVER_COVER_HINTS) and query.artists and self._artist_hit(info, query.artists) is False:
            score -= 6
        if query.year and info.year and int(query.year) == int(info.year):
            score += 1
        return score

    @staticmethod
    def _artist_hit(info: MusicInfo, artists: Iterable[str]) -> bool:
        expected = {_compact(name) for name in _split_artist_names(artists)}
        if not expected:
            return True
        actual = {
            _compact(name)
            for name in _split_artist_names(list(info.artists or []) + list(info.artist_aliases or []))
        }
        if expected & actual:
            return True
        # 允许「周杰倫」命中「Jay Chou (周杰倫)」，但避免「周杰」误伤「周杰伦」
        for name in expected:
            if len(name) < 3:
                continue
            if any(name in item or item in name for item in actual if len(item) >= 3):
                return True
        return False

    @staticmethod
    def _title_hit(info: MusicInfo, title: str) -> bool:
        expected = _compact(title)
        if not expected:
            return True
        core_expected = _core_title(title)
        names = [info.title, *(info.title_aliases or []), *(info.names or [])]
        for name in names:
            if not name:
                continue
            compact = _compact(name)
            if expected == compact or expected in compact or compact in expected:
                return True
            if core_expected and core_expected == _core_title(name):
                return True
        return False


def _safe_int(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number != 0 else None
