"""Local mirror of the coordinator's download records; never grants admission."""

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .channel_dedup import normalize_effects, specs_match
from .record_search import matches_record, record_page
from .shared_protocol import ID_PATTERN, SharedError, canonical


SYNC_VERSION = 1
SYNC_BATCH = 100
SYNC_MAX_PARTS = 4
ACTIVE_MAX_AGE = 120
COMPLETED_MAX_AGE = 600
RECORD_COLUMNS = ("id", "label", "title", "media_title", "media_type", "tmdb_id", "season", "episodes", "specs",
                  "state", "created", "updated", "request_id", "file_id")


def display_record(row):
    item = dict(row)
    for field, length in (("request_id", 32), ("file_id", 24)):
        if not re.fullmatch(rf"[a-f0-9]{{{length}}}", str(item.get(field, ""))):
            item.pop(field, None)
    item["episodes"], item["specs"] = json.loads(item["episodes"]), json.loads(item["specs"])
    if item["specs"].get("effects"):
        item["specs"]["effects"] = normalize_effects(item["specs"]["effects"])
    return item


def same_resource(candidate, record, attributes, tolerance, *, overlap=True):
    identity = ("media_type", "tmdb_id", "season")
    if any(candidate.get(field) != record.get(field) for field in identity):
        return False
    if candidate["media_type"] != "movie":
        episodes = set(candidate.get("episodes") or [])
        other = record.get("episodes") or []
        if not (episodes.intersection(other) if overlap else episodes.issubset(other)):
            return False
    return specs_match(candidate, record, attributes, tolerance)


class SharedRecordCache:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mirror (
                    id TEXT PRIMARY KEY, media_type TEXT NOT NULL, tmdb_id INTEGER NOT NULL,
                    season INTEGER, state TEXT NOT NULL, updated REAL NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS mirror_identity ON mirror(media_type, tmdb_id, season, state);
                CREATE TABLE IF NOT EXISTS progress (
                    singleton INTEGER PRIMARY KEY CHECK (singleton=1), scope TEXT NOT NULL,
                    cursor INTEGER NOT NULL, more INTEGER NOT NULL, checked_at REAL NOT NULL);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def progress(self, scope, db=None):
        if db is None:
            with self.connect() as connection:
                return self.progress(scope, connection)
        row = db.execute("SELECT cursor,more,checked_at FROM progress WHERE scope=?", (scope,)).fetchone()
        return dict(row) if row else {"cursor": 0, "more": True, "checked_at": 0}

    def apply(self, scope, cursor, result, checked_at):
        records, more, reset = result.get("changes"), result.get("more"), result.get("reset")
        next_cursor = result.get("cursor")
        previous = 0 if reset is True else cursor
        if (not isinstance(records, list) or len(records) > SYNC_BATCH or type(more) is not bool
                or type(reset) is not bool or type(next_cursor) is not int or next_cursor < previous
                or (more and not records)):
            raise SharedError("共享记录增量响应无效")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            stored = db.execute("SELECT scope,cursor FROM progress").fetchone()
            if not reset and ((stored["cursor"] if stored and stored["scope"] == scope else 0) != cursor):
                raise SharedError("共享记录同步游标不一致")
            if reset or not stored or stored["scope"] != scope:
                db.execute("DELETE FROM mirror")
            for change in records:
                seq, claim, row = change["seq"], change["id"], change["record"]
                if (type(seq) is not int or not previous < seq <= next_cursor
                        or not isinstance(claim, str) or not ID_PATTERN.fullmatch(claim)):
                    raise SharedError("共享记录增量顺序无效")
                if row is None:
                    db.execute("DELETE FROM mirror WHERE id=?", (claim,))
                else:
                    if (not isinstance(row, dict) or row.get("id") != claim
                            or row.get("state") not in {"reserved", "queued", "completed", "released"}
                            or row.get("media_type") not in {"tv", "movie"}
                            or type(row.get("tmdb_id")) is not int or not isinstance(row.get("episodes"), list)
                            or not isinstance(row.get("specs"), dict) or "season" not in row):
                        raise SharedError("共享记录数据无效")
                    db.execute("INSERT OR REPLACE INTO mirror VALUES (?,?,?,?,?,?,?)", (
                        claim, row["media_type"], row["tmdb_id"], row["season"], row["state"], row["updated"],
                        canonical(row)))
                previous = seq
            if previous != next_cursor:
                raise SharedError("共享记录增量游标无效")
            db.execute("INSERT OR REPLACE INTO progress VALUES (1,?,?,?,?)",
                       (scope, next_cursor, int(more), checked_at))

    def filter(self, scope, candidates, rules, now):
        with self.connect() as db:
            db.execute("BEGIN")
            progress = self.progress(scope, db)
            age = now - progress["checked_at"]
            if progress["more"] or not 0 <= age < COMPLETED_MAX_AGE:
                return candidates, []
            return filter_records(db, candidates, rules, mirror=True, completed_only=age >= ACTIVE_MAX_AGE)

    def search(self, scope, query, status, page, page_size, media_type):
        with self.connect() as db:
            db.execute("BEGIN")
            if self.progress(scope, db)["more"]:
                return None
            rows = db.execute("SELECT data FROM mirror WHERE (?='' OR state=?) AND (?='' OR media_type=?) "
                              "ORDER BY updated DESC, id", (status, status, media_type, media_type))
            items = (json.loads(row["data"]) for row in rows)
            return record_page([item for item in items if matches_record(item, query)], page, page_size)


def filter_records(db, candidates, rules, *, mirror=False, group=None, completed_only=False):
    available, blocked, identities = [], [], {}
    for candidate in candidates:
        identity = tuple(candidate[field] for field in ("media_type", "tmdb_id", "season"))
        if identity not in identities:
            state = "state='completed'" if completed_only else "state!='released'"
            match = f"media_type=? AND tmdb_id=? AND season IS ? AND {state}"
            if mirror:
                rows = db.execute(f"SELECT data FROM mirror WHERE {match}", identity)
                identities[identity] = [json.loads(row["data"]) for row in rows]
            else:
                rows = db.execute(f"SELECT {','.join(RECORD_COLUMNS)} FROM claims WHERE group_id=? AND {match}",
                                  (group, *identity))
                identities[identity] = [display_record(row) for row in rows]
        hit = next((row for row in identities[identity]
                    if same_resource(candidate, row, rules["attributes"], rules["size_tolerance_pct"])), None)
        if hit:
            blocked.append((candidate, hit))
        else:
            available.append(candidate)
    return available, blocked
