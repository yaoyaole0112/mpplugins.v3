"""Build bounded, verified completion evidence without reading Telegram or media APIs."""

import math
import re

from .channel_dedup import normalize_specs
from .shared_protocol import digest


AUTHORITY_VERSION = 3
SITE_METADATA_VERSION = 1


def site_label(value):
    if isinstance(value, str) and not any(mark in value for mark in (":", "/", "\\", "@", "?", "=")):
        return " ".join(value.split())[:80]
    return ""


def completion_rows(records, attributes):
    rows, skipped = {}, 0
    for record in records:
        if not isinstance(record, dict) or record.get("evidence_source") != "completed":
            continue
        kind, identity = record.get("media_type", "tv"), record.get("tmdb_id")
        media_source = str(record.get("media_source") or "").strip().lower()
        if media_source.endswith("douban"):
            media_source = "douban"
            media_id = str(record.get("media_id") or record.get("douban_id") or "").strip()
            valid_identity = media_id.isdecimal() and int(media_id) > 0 and len(media_id) <= 32
            identity = int(media_id) if valid_identity else None
        else:
            media_source = "themoviedb"
            media_id = str(record.get("media_id") or identity or "").strip()
            valid_identity = (media_id.isdecimal() and int(media_id) == identity
                              and type(identity) is int and 0 < identity < 2**53)
        season, episodes = record.get("season"), record.get("episodes")
        specs = record.get("specs") or {}
        if (record.get("identity_invalidated") or kind not in {"tv", "movie"}
                or not valid_identity
                or not isinstance(specs, dict)
                or any(specs.get(key) is None or specs.get(key) == "" for key in attributes)
                or any(key in {"size", "fps"} and (
                    type(specs.get(key)) not in {int, float}
                    or not math.isfinite(specs[key]) or specs[key] <= 0) for key in attributes)
                or any(key not in {"size", "fps"} and (
                    not isinstance(specs.get(key), str) or not 0 < len(specs[key]) <= 80)
                       for key in attributes)
                or kind == "tv" and (type(season) is not int or not 0 <= season <= 1000
                    or not isinstance(episodes, list) or not episodes or len(episodes) > 500
                    or any(type(ep) is not int or not 0 <= ep <= 100000 for ep in episodes))):
            skipped += 1
            continue
        timestamp = record.get("remembered_at", record.get("updated", 0))
        if type(timestamp) not in {int, float} or not math.isfinite(timestamp) or timestamp <= 0:
            skipped += 1
            continue
        links = {key: record[key] for key, length in
                 (("original_claim", 32), ("original_request", 32), ("original_file", 24))
                 if re.fullmatch(rf"[a-f0-9]{{{length}}}", str(record.get(key, "")))}
        if len(links) != 3:
            links = {}
        # An approved physical multi-episode file must retain its complete original link.
        episode_groups = ([sorted(set(episodes))] if links else
                          [[episode] for episode in sorted(set(episodes))]) if kind == "tv" else [[]]
        for group in episode_groups:
            stored_specs = {key: specs[key] for key in attributes}
            resolution = specs.get("resolution")
            if isinstance(resolution, str) and 0 < len(resolution) <= 80:
                stored_specs["resolution"] = normalize_specs({"resolution": resolution})["resolution"]
            row = {
                "media_type": kind,
                "tmdb_id": identity if media_source == "themoviedb" else None,
                "media_source": media_source, "media_id": media_id,
                "season": season if kind == "tv" else None,
                "episodes": group,
                "specs": stored_specs,
                "title": str(record.get("title") or record.get("path") or record.get("file_key") or "")[-160:],
                "media_title": str(record.get("media_title") or "")[:120],
                "completed_at": timestamp,
            }
            # Preserve original approval links when repairing a confirmed completed request.
            row.update(links)
            row["id"] = digest(row)[:24]
            name = site_label(record.get("site_name"))
            if name:
                row["site_name"] = name
            if name or row["id"] not in rows:
                rows[row["id"]] = row
    return list(rows.values()), skipped
