"""Persistent evidence keys, shared by movie and episode resource guards."""

import hashlib
import json

from .channel_dedup import normalize_title


def resource_slot(record: dict, episode=None) -> tuple:
    kind = record.get("media_type", "tv")
    return (kind, record.get("season") if kind == "tv" else None, episode if kind == "tv" else None)


def evidence_key(record: dict) -> str:
    identity = (
        ["tmdb", int(record["tmdb_id"])] if record.get("tmdb_id")
        else ["title", normalize_title(record.get("title_key") or record.get("title")), record.get("year")]
    )
    payload = [
        record.get("media_type", "tv"), identity, record.get("season"), record.get("episodes"),
        record.get("specs") or {},
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def index_records(records) -> dict:
    result = {}
    for record in records:
        numbers = record.get("episodes") or ([None] if record.get("media_type") == "movie" else [])
        for number in numbers:
            result.setdefault(resource_slot(record, number), []).append(record)
    return result
