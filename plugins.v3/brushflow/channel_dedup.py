"""Parse Telegram release posts and compare per-file release attributes."""

import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Iterable, Optional
from unicodedata import normalize
from urllib.parse import unquote


ATTRIBUTES = ("resolution", "effects", "codec", "source", "fps", "size")
VIDEO_EXTENSIONS = {"mkv", "mp4", "avi", "mov", "wmv", "m4v", "ts", "m2ts", "webm"}


def normalize_title(value: Any) -> str:
    return "".join(character.casefold() for character in normalize("NFKC", str(value or "")) if character.isalnum())


def filename_title_aliases(value: Any) -> set[str]:
    """Keep title evidence that generic filename parsers may discard as bracketed tags."""
    name = PurePosixPath(str(value or "").replace("\\", "/")).name
    head = re.split(
        r"(?i)(?:^|[ ._-])(?:S\s*\d{1,3}(?:\s*E\s*\d{1,4})?|E\d{1,4}|\d{1,3}x\d{1,4})(?!\d)",
        name, maxsplit=1,
    )[0]
    head = re.split(r"(?<!\d)(?:19|20)\d{2}(?!\d)", head, maxsplit=1)[0]
    aliases = set()
    for bracketed in re.findall(r"\[([^]]+)\]|【([^】]+)】|\(([^)]+)\)", head):
        text = next((part.strip() for part in bracketed if part.strip()), "")
        if (
            re.search(r"[\u3400-\u9fff]", text)
            and not re.search(
                r"字幕|中字|简繁|简体|繁体|国语|粤语|双语|内封|内嵌|高清|超清|"
                r"^(?:韩剧|日剧|美剧|日漫|国漫|动画|合集|完结|全\d+集)$",
                text,
            )
        ):
            aliases.add(text)
    plain = re.sub(r"\[[^]]*\]|【[^】]*】|\([^)]*\)", " ", head).strip(" ._-")
    if plain:
        aliases.add(plain)
    return aliases


def long_title_prefix_matches(name: str, aliases: Iterable[str]) -> bool:
    """Allow long ASCII truncations only at an official alias's complete-word boundary."""
    if not name.isascii():
        return False
    words = re.findall(r"[a-z0-9]+", name.casefold())
    key = "".join(words)
    if len(words) < 8 or len(key) < 40:
        return False
    for alias in aliases:
        if not str(alias).isascii():
            continue
        tokens = re.findall(r"[a-z0-9]+", str(alias).casefold())
        prefix = ""
        for index, token in enumerate(tokens):
            prefix += token
            if prefix == key and index < len(tokens) - 1:
                return True
            if len(prefix) >= len(key):
                break
    return False


def parse_episode(value: Any) -> tuple[Optional[int], list[int]]:
    text = str(value or "")
    match = re.search(r"(?i)(?:^|[^A-Z0-9])S\s*(\d{1,3})\s*E\s*(\d{1,4})(.*)", text)
    if match:
        season = int(match.group(1))
        first = int(match.group(2))
        tail = match.group(3)
        range_match = re.match(r"\s*[-~至]\s*(?:E\s*)?(\d{1,4})", tail, re.IGNORECASE)
        if range_match:
            last = int(range_match.group(1))
            return season, list(range(first, last + 1)) if first <= last <= first + 500 else []
        consecutive = re.match(r"(?i)(?:\s*E\s*\d{1,4})+", tail)
        if consecutive:
            return season, [first] + [int(number) for number in re.findall(r"(?i)E\s*(\d{1,4})", consecutive.group())]
        return season, [first]
    cross = re.search(r"(?i)(?:^|[^A-Z0-9])(\d{1,3})x(\d{1,4})(?!\d)", text)
    if cross:
        return int(cross.group(1)), [int(cross.group(2))]
    chinese = re.search(r"第\s*(\d{1,3})\s*季\s*第\s*(\d{1,4})\s*集", text)
    if chinese:
        return int(chinese.group(1)), [int(chinese.group(2))]
    return None, []


def _line_value(text: str, labels: Iterable[str]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?im)^[^\w\r\n]*(?:{label_pattern})\s*[:：]\s*([^\r\n]+)$", text)
    if not match:
        return ""
    value = re.sub(r"\[([^]]+)\]\(https?://[^)]+\)", r"\1", match.group(1))
    value = re.sub(r"\s*\(\s*https?://[^)]+\)", "", value).strip()
    return value


def _extract_tmdb_id(text: str) -> Optional[int]:
    match = re.search(r"(?i)tmdb\s*id\s*[:=：]?\s*(\d+)|themoviedb\.org/tv/(\d+)", text)
    if not match:
        return None
    try:
        return int(next(group for group in match.groups() if group))
    except (StopIteration, ValueError):
        return None


def parse_size(value: Any) -> Optional[int]:
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    match = re.search(r"(?i)(\d+(?:\.\d+)?)\s*(B|KB|KIB|MB|MIB|GB|GIB|TB|TIB)\b", text)
    if not match:
        return None
    units = {"B": 0, "KB": 1, "KIB": 1, "MB": 2, "MIB": 2, "GB": 3, "GIB": 3, "TB": 4, "TIB": 4}
    return int(float(match.group(1)) * (1024 ** units[match.group(2).upper()]))


def parse_specs(text: Any, size: Any = None) -> dict:
    value = str(text or "").upper().replace("Ｈ", "H").replace("．", ".")
    resolution_match = re.search(r"(?<!\d)(2160|1440|1080|720|576|540|480)\s*([PI])", value)
    effects = []
    for pattern, label in (
        (r"HDR10\s*\+", "HDR10+"),
        (r"DOLBY[ ._-]*VISION|DOVI|(?:^|[^A-Z0-9])DV(?:[^A-Z0-9]|$)", "DV"),
        (r"HDR10(?!\s*\+)", "HDR10"),
        (r"(?<![A-Z0-9])HDR(?![A-Z0-9])", "HDR"),
        (r"(?<![A-Z0-9])HLG(?![A-Z0-9])", "HLG"),
        (r"(?<![A-Z0-9])SDR(?![A-Z0-9])", "SDR"),
    ):
        if re.search(pattern, value):
            effects.append(label)
    codec = None
    for pattern, label in (
        (r"(?:H\.?\s*265|HEVC|X265|H265)", "H.265"),
        (r"(?:H\.?\s*264|AVC|X264|H264)", "H.264"),
        (r"(?:AV1|AOM AV1)", "AV1"),
        (r"VC[- ]?1", "VC-1"),
        (r"MPEG[- ]?2", "MPEG-2"),
    ):
        if re.search(pattern, value):
            codec = label
            break
    source = None
    for pattern, label in (
        (r"REMUX", "REMUX"),
        (r"BLU[ ._-]?RAY|BLURAY|BDRIP|BDMV", "BluRay"),
        (r"WEB[ ._-]?DL|WEBDL", "WEB-DL"),
        (r"WEB[ ._-]?RIP|WEBRIP", "WEBRip"),
        (r"HDTV", "HDTV"),
        (r"DVD(?:RIP)?", "DVD"),
        (r"(?<![A-Z0-9])WEB(?![A-Z0-9])", "WEB"),
    ):
        if re.search(pattern, value):
            source = label
            break
    # Dots also delimit filename fields: AAC.2.0.25FPS means 25 FPS, not 0.25.
    fps_value = re.sub(
        r"(?<![A-Z0-9])(?:AAC|DDP|DD\+?|(?:E[ ._-]*)?AC[ ._-]*3|"
        r"DTS(?:[ ._-]*HD(?:[ ._-]*MA)?)?|TRUE[ ._-]*HD|L?PCM|FLAC|OPUS)"
        r"[ ._-]*(?:(?:DOLBY|ATMOS)[ ._-]*)*\d\.\d(?!\d)",
        " ",
        value,
    )
    fps_match = re.search(r"(?<!\d)(\d{1,2}(?:\.\d{1,3})?)\s*(?:FPS|帧|FRAME(?:S)?\b)", fps_value)
    fps = None
    if fps_match:
        try:
            fps = round(float(fps_match.group(1)), 3)
        except ValueError:
            pass
    return {
        "resolution": f"{resolution_match.group(1)}{resolution_match.group(2).lower()}" if resolution_match else None,
        "effects": "+".join(sorted(set(effects))) if effects else None,
        "codec": codec,
        "source": source,
        "fps": fps,
        "size": parse_size(size),
    }


def repair_record_frame_rates(records: Iterable[dict]) -> int:
    """Repair persisted FPS from retained filenames without rereading Telegram."""
    repaired = 0
    for record in records:
        if not isinstance(record, dict) or not record.get("file_key"):
            continue
        specs = record.get("specs")
        if not isinstance(specs, dict):
            continue
        fps = parse_specs(record["file_key"])["fps"]
        if fps is not None and fps != specs.get("fps"):
            record["specs"] = {**specs, "fps": fps}
            repaired += 1
    return repaired


def merge_specs(primary: dict, fallback: dict) -> dict:
    return {key: primary.get(key) if primary.get(key) is not None else fallback.get(key) for key in ATTRIBUTES}


def parse_channel_message(
    channel: str, message_id: int, message_date: Any, text: str, filename_match=None,
) -> Optional[dict]:
    if not isinstance(text, str) or not text.strip():
        return None
    title = _line_value(text, ("影视", "剧名", "片名", "名称"))
    season, episodes = parse_episode(_line_value(text, ("季集", "剧集", "集数")))
    filename_match = filename_match or re.search(r"ed2k://\|file\|([^|]+)\|([0-9]+)\|[0-9a-fA-F]{32}\|/", text)
    filename = unquote(filename_match.group(1)) if filename_match else ""
    file_season, file_episodes = parse_episode(filename)
    if file_season is not None:
        season, episodes = file_season, file_episodes
    elif season is None:
        season, episodes = parse_episode(title)
    if not title and filename:
        title = re.split(r"(?i)[ ._-]S\d{1,3}E\d{1,4}", filename, maxsplit=1)[0]
        title = re.sub(r"\((?:19|20)\d{2}\)|\(tmdbid=\d+\)", "", title, flags=re.I)
    if not title or season is None or not episodes:
        return None
    if filename:
        title = re.sub(r"\s*\([^)]*https?://[^)]*\)", "", title).strip()
    date_value = message_date
    if isinstance(date_value, datetime):
        if date_value.tzinfo is None:
            date_value = date_value.replace(tzinfo=timezone.utc)
        date_value = date_value.timestamp()
    else:
        try:
            date_value = float(date_value)
        except (TypeError, ValueError):
            date_value = 0
    quality = _line_value(text, ("质量", "画质", "规格"))
    fps = _line_value(text, ("帧率", "帧频"))
    source = _line_value(text, ("来源",))
    size_value = _line_value(text, ("大小", "体积"))
    specs = merge_specs(
        parse_specs(filename, filename_match.group(2) if filename_match else None),
        parse_specs(" ".join((quality, fps, source)), size_value),
    )
    tmdb_id = _extract_tmdb_id(filename)
    if not tmdb_id:
        tmdb_id = _extract_tmdb_id(text)
    if not tmdb_id and re.search(r"类型\s*[:：]\s*剧集", text):
        id_match = re.search(r"(?:^|[|｜\s])ID\s*[:：]\s*(\d+)", text)
        tmdb_id = int(id_match.group(1)) if id_match else None
    year_match = re.search(r"(?:19|20)\d{2}", _line_value(text, ("年份",)))
    return {
        "channel": str(channel),
        "message_id": int(message_id),
        "published_at": float(date_value),
        "title": title,
        "title_key": normalize_title(title),
        "file_key": filename,
        "tmdb_id": tmdb_id,
        "year": int(year_match.group()) if year_match else None,
        "season": season,
        "episodes": sorted(set(episodes)),
        "specs": specs,
    }


def parse_channel_messages(channel: str, message_id: int, message_date: Any, text: str) -> list[dict]:
    matches = list(re.finditer(r"ed2k://\|file\|([^|]+)\|([0-9]+)\|[0-9a-fA-F]{32}\|/", text or ""))
    if not matches:
        record = parse_channel_message(channel, message_id, message_date, text)
        return [record] if record else []
    records = [parse_channel_message(channel, message_id, message_date, text, match) for match in matches]
    return [record for record in records if record]


def parse_torrent_files(content: bytes) -> list[dict]:
    """Use MoviePilot's existing torrentool parser and root-relative file convention."""
    if not isinstance(content, bytes) or not content or len(content) > 16 * 1024 * 1024:
        return []
    try:
        from torrentool.api import Torrent

        torrent = Torrent.from_string(content)
        files = torrent.files
        single_file = len(files) == 1 and files[0].name == torrent.name
        result = []
        for entry in files:
            path = PurePosixPath(entry.name)
            if not single_file and path.parts and path.parts[0] == torrent.name:
                path = path.relative_to(torrent.name)
            result.append({"path": str(path), "size": entry.length})
        return result
    except Exception:
        return []


def parse_candidate_file(
    path: str, size: Optional[int], title_hint: str = "", spec_hint: str = "",
) -> Optional[dict]:
    filename = PurePosixPath(str(path or "")).name
    if "." not in filename or filename.rsplit(".", 1)[-1].lower() not in VIDEO_EXTENSIONS:
        return None
    season, episodes = parse_episode(filename)
    if season is None:
        episode_match = re.search(r"(?i)(?:^|[ ._-])E(\d{1,4})(?!\d)", filename)
        season_match = re.search(r"(?i)(?:^|[^A-Z0-9])S(?:EASON[ ._-]*)?(\d{1,3})(?!\d)", f"{path} {title_hint}")
        if episode_match and season_match:
            season, episodes = int(season_match.group(1)), [int(episode_match.group(1))]
    if season is None or not episodes:
        return None
    title_part = re.split(r"(?i)(?:^|[ ._-])(?:S\s*\d{1,3}\s*E\s*\d{1,4}|E\d{1,4}|\d{1,3}x\d{1,4})", filename, maxsplit=1)[0]
    title_part = re.sub(r"(?i)\b(?:19|20)\d{2}\b|\(?tmdbid\s*=\s*\d+\)?", " ", title_part)
    title_key = normalize_title(title_part)
    if not title_key:
        hinted_title = re.split(r"(?i)(?:^|[ ._-])S\s*\d{1,3}\s*E\s*\d{1,4}", str(title_hint), maxsplit=1)[0]
        hinted_title = re.sub(r"(?i)\b(?:19|20)\d{2}\b|\(?tmdbid\s*=\s*\d+\)?", " ", hinted_title)
        title_key = normalize_title(hinted_title)
    tmdb_id = _extract_tmdb_id(filename) or _extract_tmdb_id(title_hint)
    year_match = re.search(r"\b(?:19|20)\d{2}\b", filename)
    specs = merge_specs(parse_specs(filename, size), merge_specs(parse_specs(title_hint), parse_specs(spec_hint)))
    inferred_attributes = []
    # Naming convention only, not a probe: limit implicit SDR to ordinary AVC WEB releases.
    if (
        specs["effects"] is None
        and specs["resolution"] in {"1080p", "1080i", "720p", "720i", "576p", "576i", "540p", "540i", "480p", "480i"}
        and specs["codec"] == "H.264"
        and specs["source"] in {"WEB-DL", "WEBRip", "WEB"}
        and not re.search(
            r"HDR|HLG|DOVI|DOLBY[ ._-]*VISION|EDR|MAXPLUS|"
            r"(?:^|[^A-Z0-9])(?:DV|PQ)(?:[^A-Z0-9]|$)|"
            r"(?:10|12)[ ._-]*BIT|HI10P|HIGH[ ._-]*10|YUV\d+P(?:10|12)|"
            r"(?:BT|REC)[ ._-]*2020|(?:SMPTE[ ._-]*)?(?:ST[ ._-]*)?2084",
            f"{path} {title_hint} {spec_hint}", re.IGNORECASE,
        )
    ):
        specs["effects"] = "SDR"
        inferred_attributes.append("effects")
    return {
        "path": str(path),
        "title_key": title_key,
        "title_keys": sorted(({title_key} | {normalize_title(name) for name in filename_title_aliases(filename)}) - {""}),
        "tmdb_id": tmdb_id,
        "year": int(year_match.group()) if year_match else None,
        "season": season,
        "episodes": sorted(set(episodes)),
        "specs": specs,
        "inferred_attributes": inferred_attributes,
    }


def _same_size(left: Any, right: Any, tolerance_pct: float) -> bool:
    try:
        left_size, right_size = int(left), int(right)
    except (TypeError, ValueError):
        return False
    if left_size <= 0 or right_size <= 0:
        return False
    return abs(left_size - right_size) * 100 <= max(left_size, right_size) * tolerance_pct


def specs_match(candidate: dict, record: dict, attributes: Iterable[str], size_tolerance_pct: float = 5) -> bool:
    selected = list(dict.fromkeys(attribute for attribute in attributes if attribute in ATTRIBUTES))
    if not selected:
        return False
    candidate_specs = candidate.get("specs") or {}
    record_specs = record.get("specs") or {}
    for attribute in selected:
        left, right = candidate_specs.get(attribute), record_specs.get(attribute)
        if attribute == "size":
            if not _same_size(left, right, size_tolerance_pct):
                return False
        elif left is None or right is None or left != right:
            return False
    return True


def same_release(candidate: dict, record: dict, attributes: Iterable[str], size_tolerance_pct: float = 5) -> bool:
    if not candidate or not record:
        return False
    if candidate.get("season") != record.get("season"):
        return False
    candidate_episodes = set(candidate.get("episodes") or [])
    record_episodes = set(record.get("episodes") or [])
    if not candidate_episodes or not candidate_episodes.issubset(record_episodes):
        return False
    candidate_id, record_id = candidate.get("tmdb_id"), record.get("tmdb_id")
    if candidate_id and record_id:
        if int(candidate_id) != int(record_id):
            return False
    else:
        candidate_names = set(candidate.get("title_keys") or []) | {candidate.get("title_key")}
        record_names = set(record.get("title_keys") or []) | {record.get("title_key")}
        if not (candidate_names & record_names) - {None, ""}:
            return False
        if candidate.get("year") and record.get("year") and candidate["year"] != record["year"]:
            return False
    return specs_match(candidate, record, attributes, size_tolerance_pct)
