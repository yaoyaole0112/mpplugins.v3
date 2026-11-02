"""Helpers for bounded, persistent display history."""

import time


RECORD_TTL_SECONDS = 24 * 60 * 60


def recent_records(records, now=None, max_age=RECORD_TTL_SECONDS, limit=None, timestamp_key="timestamp"):
    now = float(now if now is not None else time.time())
    retained = []
    for item in records if isinstance(records, list) else []:
        if not isinstance(item, dict):
            continue
        record = dict(item)
        try:
            timestamp = float(record.get(timestamp_key) or 0)
        except (TypeError, ValueError):
            timestamp = 0
        if timestamp <= 0:
            timestamp = now
        if 0 <= now - timestamp <= max_age:
            record[timestamp_key] = timestamp
            retained.append(record)
    if limit is not None:
        limit = max(0, int(limit))
        newest_ids = {id(item) for item in sorted(
            retained, key=lambda item: item[timestamp_key], reverse=True)[:limit]}
        retained = [item for item in retained if id(item) in newest_ids]
    return retained
