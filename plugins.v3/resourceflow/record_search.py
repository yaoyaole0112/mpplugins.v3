"""Search displayable approval records without inspecting private download payloads."""

import re
import unicodedata

from .channel_dedup import filename_title_aliases


STATE_LABELS = {
    "requesting": "等待审批", "reserved": "已获准 已批准", "adding": "暂停添加中",
    "awaiting_start": "等待入队确认", "awaiting_qb_files": "等待 QB 文件清单", "timeout_cancel_pending": "超时取消中",
    "queued": "已入队", "completed": "已完成 已下载", "released": "已释放",
    "rejected": "已过滤 已排除", "uncertain": "待核验",
}
TEXT_FIELDS = (
    "key", "id", "request_id", "title", "media_title", "path", "file_key", "title_key",
    "task_id", "task_name", "site_name", "downloader", "label", "owner", "download_owners", "reason", "error",
    "channel", "evidence_source",
    "media_source", "media_id", "douban_id",
)
SPEC_FIELDS = ("resolution", "effects", "codec", "source", "fps", "size")
CHINESE_TITLE = re.compile(r"[\u3400-\u9fff]")
CHINESE_TITLE_PREFIX = re.compile(r"^[\u3400-\u9fff][\u3400-\u9fff0-9 、，！：·？！《》「」“”',:!?—-]*")


def record_title_key(row):
    if row.get("media_type") not in {"tv", "movie"}:
        return None
    source = str(row.get("media_source") or "").lower()
    if source.endswith("douban"):
        identity = str(row.get("media_id") or row.get("douban_id") or "").strip()
        return (row["media_type"], "douban", identity) if identity.isdecimal() and int(identity) > 0 else None
    try:
        tmdb_id = int(row.get("media_id") or row.get("tmdb_id"))
    except (TypeError, ValueError):
        return None
    return (row["media_type"], tmdb_id) if tmdb_id > 0 else None


def localized_record_titles(rows):
    titles = {}
    for row in rows:
        title = row.get("media_title")
        key = record_title_key(row)
        if key and isinstance(title, str) and CHINESE_TITLE.search(title):
            titles[key] = title.strip()[:120]
    return titles


def filename_display_title(*values):
    """Extract a readable Chinese title without binding or changing the media identity."""
    for value in values:
        if not isinstance(value, str) or not CHINESE_TITLE.search(value):
            continue
        name = unicodedata.normalize("NFKC", value.replace("\\", "/").rsplit("/", 1)[-1])
        choices = []
        for alias in filename_title_aliases(name):
            match = CHINESE_TITLE_PREFIX.match(alias)
            if match:
                title = match[0].rstrip(" -·")
                choices.append((name.find(title), -len(title), title))
        if choices:
            return min(choices)[2][:120]
    return ""


def record_display_title(row, titles, torrent_title=""):
    value = row.get("media_title")
    title = value.strip() if isinstance(value, str) else ""
    if CHINESE_TITLE.search(title):
        return title
    return (titles.get(record_title_key(row))
            or filename_display_title(row.get("path"), row.get("title"), torrent_title)
            or title)


def _normalize(value):
    return unicodedata.normalize("NFKC", str(value)).casefold()


def _terms(row):
    values = [str(row[key]) for key in TEXT_FIELDS if row.get(key) is not None]
    phase = row.get("phase") or row.get("state") or ""
    values.extend((phase, STATE_LABELS.get(phase, "")))
    if row.get("media_type") in {"movie", "tv"}:
        values.append("电影 movie" if row["media_type"] == "movie" else "剧集 tv")
    if row.get("reason") == "同规格已占位或已完成":
        values.append("资源下载中" if row.get("state") in {"queued", "reserved"} else "资源已下载")
        values.append("资源已完成/已下载")
    if row.get("tmdb_id"):
        values.append(f"TMDB {row['tmdb_id']}")
    elif str(row.get("media_source") or "").lower().endswith("douban") and row.get("media_id"):
        values.append(f"豆瓣 {row['media_id']}")
    season = row.get("season")
    if season is not None:
        try:
            prefix = f"S{int(season):02d}"
            values.append(prefix)
            for episode in row.get("episodes") or []:
                values.append(f"{prefix}E{int(episode):02d}")
        except (TypeError, ValueError):
            pass
    specs = row.get("specs") or {}
    if isinstance(specs, dict):
        values.extend(str(specs[key]) for key in SPEC_FIELDS if specs.get(key) is not None)
    for key in ("files", "rejected"):
        for item in row.get(key) or []:
            if isinstance(item, dict):
                values.extend(_terms(item))
    return values


def matches_record(row, query=""):
    tokens = _normalize(query or "")[:200].split()
    if not tokens:
        return True
    text = _normalize(" ".join(_terms(row)))
    return all(token in text for token in tokens)


def record_owners(row):
    """Return downloader labels visible on a record and its file decisions."""
    owners = set()
    for value in row.get("download_owners") or []:
        if isinstance(value, str) and value.strip():
            owners.add(value.strip())
    for key in ("label", "owner"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            owners.add(value.strip())
    for key in ("files", "rejected"):
        for item in row.get(key) or []:
            if not isinstance(item, dict):
                continue
            for owner_key in ("label", "owner"):
                value = item.get(owner_key)
                if isinstance(value, str) and value.strip():
                    owners.add(value.strip())
            for decision in item.get("rejected") or []:
                if isinstance(decision, dict) and isinstance(decision.get("owner"), str):
                    value = decision["owner"].strip()
                    if value:
                        owners.add(value)
    return owners


def matches_owner(row, owner=""):
    selected = str(owner or "").strip()
    if not selected:
        return True
    target = _normalize(selected)
    return any(_normalize(value) == target for value in record_owners(row))


def matches_media_type(row, media_type=""):
    selected = str(media_type or "").strip()
    if not selected:
        return True
    if row.get("media_type") == selected:
        return True
    return any(isinstance(item, dict) and item.get("media_type") == selected
               for item in row.get("files") or [])


def record_page(items, page=1, page_size=20):
    page, page_size = max(int(page), 1), min(max(int(page_size), 1), 100)
    total = len(items)
    page = min(page, max(1, (total + page_size - 1) // page_size))
    return {"total": total, "page": page, "page_size": page_size,
            "items": items[(page - 1) * page_size:page * page_size]}
