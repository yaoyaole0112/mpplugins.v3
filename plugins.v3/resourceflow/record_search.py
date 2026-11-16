"""Search displayable approval records without inspecting private download payloads."""

import unicodedata


STATE_LABELS = {
    "requesting": "等待审批", "reserved": "已获准 已批准", "adding": "暂停添加中",
    "awaiting_start": "等待入队确认", "timeout_cancel_pending": "超时取消中",
    "queued": "已入队", "completed": "已完成 已下载", "released": "已释放",
    "rejected": "已排除", "uncertain": "待核验",
}
TEXT_FIELDS = (
    "key", "id", "request_id", "title", "media_title", "path", "file_key", "title_key",
    "task_id", "task_name", "downloader", "label", "owner", "reason", "error",
    "channel", "evidence_source",
)
SPEC_FIELDS = ("resolution", "effects", "codec", "source", "fps", "size")


def _normalize(value):
    return unicodedata.normalize("NFKC", str(value)).casefold()


def _terms(row):
    values = [str(row[key]) for key in TEXT_FIELDS if row.get(key) is not None]
    phase = row.get("phase") or row.get("state") or ""
    values.extend((phase, STATE_LABELS.get(phase, "")))
    if row.get("media_type") in {"movie", "tv"}:
        values.append("电影 movie" if row["media_type"] == "movie" else "剧集 tv")
    if row.get("reason") == "同规格已占位或已完成":
        values.append("资源已完成/已下载")
    if row.get("tmdb_id"):
        values.append(f"TMDB {row['tmdb_id']}")
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


def record_page(items, page=1, page_size=20):
    page, page_size = max(int(page), 1), min(max(int(page_size), 1), 100)
    total = len(items)
    page = min(page, max(1, (total + page_size - 1) // page_size))
    return {"total": total, "page": page, "page_size": page_size,
            "items": items[(page - 1) * page_size:page * page_size]}
