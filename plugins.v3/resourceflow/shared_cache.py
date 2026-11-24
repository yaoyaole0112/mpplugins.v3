"""Local mirror of the coordinator's download records; never grants admission."""

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .channel_dedup import filename_title_aliases, normalize_specs, normalize_title, specs_match
from .record_search import (
    localized_record_titles, matches_owner, matches_record, record_display_title, record_page,
)
from .shared_protocol import ID_PATTERN, SharedError, canonical


SYNC_VERSION = 2
SYNC_BATCH = 100
SYNC_MAX_PARTS = 4
ACTIVE_MAX_AGE = 120
COMPLETED_MAX_AGE = 600
HIGH_RESOLUTION_WAIT = "同集2160p/4K已占位或正在下载，低分辨率暂缓"
HIGH_RESOLUTION_DONE = "已有2160p/4K高分辨率资源"
HIGH_RESOLUTION_PACK_WAIT = "整包暂缓：等待包内其他文件的2160p/4K占位结果"
RECORD_COLUMNS = (
    "id", "label", "title", "media_title", "site_name", "media_type", "tmdb_id",
    "media_source", "media_id", "season", "episodes", "specs", "state", "created",
    "updated", "request_id", "file_id",
)


def display_record(row):
    item = dict(row)
    for field, length in (("request_id", 32), ("file_id", 24)):
        if not re.fullmatch(rf"[a-f0-9]{{{length}}}", str(item.get(field, ""))):
            item.pop(field, None)
    item["episodes"], item["specs"] = json.loads(item["episodes"]), normalize_specs(json.loads(item["specs"]))
    if item.get("media_source") not in {"themoviedb", "douban"}:
        item["media_source"] = "themoviedb"
    if item.get("media_id") in (None, ""):
        item["media_id"] = str(item.get("tmdb_id") or "")
    if item["media_source"] == "douban" and item.get("tmdb_id") == 0:
        item["tmdb_id"] = None
    return item


def _identity_alias_index(rules, media_type):
    aliases = (rules or {}).get("movie_identity_aliases" if media_type == "movie" else "identity_aliases") or {}
    tmdb_aliases, douban_aliases, title_aliases = {}, {}, {}
    for name, target in aliases.items():
        match = re.fullmatch(r"(?i)(TMDB|THEMOVIEDB|DOUBAN)\s*[:#]?\s*([1-9]\d*)", str(name).strip())
        if match:
            source, raw_id = match.groups()
            if source == "DOUBAN":
                douban_aliases[raw_id] = int(target)
            else:
                tmdb_aliases[int(raw_id)] = int(target)
        else:
            title_aliases.setdefault(normalize_title(name), set()).add(int(target))
    return tmdb_aliases, douban_aliases, title_aliases


def _raw_media_identity(record):
    source = str(record.get("media_source") or "").strip().lower()
    if source.endswith("douban"):
        identity = record.get("media_id") or record.get("douban_id")
        return ("douban", str(identity)) if str(identity or "").isdecimal() and int(identity) > 0 else None
    if source.endswith(("tmdb", "themoviedb")) or not source:
        identity = record.get("media_id") or record.get("tmdb_id")
        try:
            value = int(identity)
        except (TypeError, ValueError):
            return None
        return ("themoviedb", str(value)) if 0 < value < 2**53 else None
    return None


def _canonical_media_identity(record, rules, alias_index=None):
    raw = _raw_media_identity(record)
    if not raw:
        return None
    media_type = record.get("media_type")
    tmdb_aliases, douban_aliases, title_aliases = alias_index or _identity_alias_index(rules, media_type)
    names = {normalize_title(record.get("media_title", ""))}
    names.update(normalize_title(name) for name in filename_title_aliases(record.get("title", "")))
    names.discard("")
    matched_titles = {
        target
        for name in names
        for target in title_aliases.get(name, ())
    }
    if len(matched_titles) == 1:
        raw = ("themoviedb", str(matched_titles.pop()))
    elif raw[0] == "douban" and raw[1] in douban_aliases:
        raw = ("themoviedb", str(douban_aliases[raw[1]]))
    if raw[0] == "themoviedb":
        tmdb_id = int(raw[1])
        visited = set()
        while tmdb_id in tmdb_aliases and tmdb_id not in visited:
            visited.add(tmdb_id)
            tmdb_id = tmdb_aliases[tmdb_id]
        raw = ("themoviedb", str(tmdb_id))
    return raw


def _identity_query_keys(candidate, rules, alias_index=None):
    alias_index = alias_index or _identity_alias_index(rules, candidate.get("media_type"))
    tmdb_aliases, douban_aliases, title_aliases = alias_index
    canonical_identity = _canonical_media_identity(candidate, rules, alias_index)
    if not canonical_identity:
        return None
    if canonical_identity[0] == "douban":
        return [canonical_identity]
    canonical_id = int(canonical_identity[1])
    if any(target == canonical_id for targets in title_aliases.values() for target in targets):
        return None
    possible_tmdb_ids = {canonical_id, *tmdb_aliases, *tmdb_aliases.values()}
    keys = [
        ("themoviedb", str(raw_id)) for raw_id in possible_tmdb_ids
        if _resolve_tmdb_id(raw_id, tmdb_aliases) == canonical_id
    ]
    keys.extend(
        ("douban", raw_id) for raw_id, target in douban_aliases.items()
        if _resolve_tmdb_id(target, tmdb_aliases) == canonical_id
    )
    return sorted(set(keys))


def _resolve_tmdb_id(tmdb_id, id_aliases):
    visited = set()
    while tmdb_id in id_aliases and tmdb_id not in visited:
        visited.add(tmdb_id)
        tmdb_id = id_aliases[tmdb_id]
    return tmdb_id


def same_resource(candidate, record, attributes, tolerance, *, overlap=True, identity_rules=None,
                  identity_index=None, candidate_identity=None):
    if any(candidate.get(field) != record.get(field) for field in ("media_type", "season")):
        return False
    identity_index = identity_index or _identity_alias_index(identity_rules, candidate.get("media_type"))
    candidate_identity = (candidate_identity if candidate_identity is not None else
                          _canonical_media_identity(candidate, identity_rules, identity_index))
    if not candidate_identity or candidate_identity != _canonical_media_identity(record, identity_rules, identity_index):
        return False
    if candidate["media_type"] != "movie":
        episodes = set(candidate.get("episodes") or [])
        other = record.get("episodes") or []
        if not (episodes.intersection(other) if overlap else episodes.issubset(other)):
            return False
    return specs_match(candidate, record, attributes, tolerance)


def is_2160p_downgrade(candidate, record, identity_rules=None, identity_index=None,
                        candidate_identity=None):
    identity_index = identity_index or _identity_alias_index(identity_rules, candidate.get("media_type"))
    candidate_identity = (candidate_identity if candidate_identity is not None else
                          _canonical_media_identity(candidate, identity_rules, identity_index))
    if (candidate.get("media_type") != "tv" or record.get("media_type") != "tv"
            or not candidate_identity
            or candidate_identity != _canonical_media_identity(record, identity_rules, identity_index)
            or candidate.get("season") != record.get("season")):
        return False
    if not set(candidate.get("episodes") or []).intersection(record.get("episodes") or []):
        return False
    candidate_resolution = normalize_specs(candidate.get("specs") or {}).get("resolution")
    record_resolution = normalize_specs(record.get("specs") or {}).get("resolution")
    return bool(record_resolution == "2160p"
                and re.fullmatch(r"(?:1080|720|576|540|480|360|240|144)[pi]", candidate_resolution or "", re.I))


def find_resource_conflict(candidate, records, rules):
    """Prefer confirmed evidence over a temporary high-resolution hold."""
    prefer_2160p = rules.get("rules", {}).get("prefer_2160p", True)
    identity_index = _identity_alias_index(rules, candidate.get("media_type"))
    candidate_identity = _canonical_media_identity(candidate, rules, identity_index)
    temporary = ordinary = None
    for row in records:
        state = row["state"]
        if state not in {"reserved", "queued", "completed"}:
            continue
        if (prefer_2160p and is_2160p_downgrade(
                candidate, row, rules, identity_index, candidate_identity)):
            if state == "completed":
                return {**row, "_match_reason": HIGH_RESOLUTION_DONE}
            if temporary is None:
                temporary = {**row, "_match_reason": HIGH_RESOLUTION_WAIT}
            continue
        if same_resource(candidate, row, rules["attributes"], rules["size_tolerance_pct"],
                         identity_rules=rules, identity_index=identity_index,
                         candidate_identity=candidate_identity):
            if state == "completed":
                return row
            if ordinary is None:
                ordinary = row
    return temporary or ordinary


class SharedRecordCache:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mirror (
                    id TEXT PRIMARY KEY, media_type TEXT NOT NULL, tmdb_id INTEGER NOT NULL,
                    media_source TEXT NOT NULL DEFAULT 'themoviedb',
                    media_id TEXT NOT NULL DEFAULT '',
                    season INTEGER, state TEXT NOT NULL, updated REAL NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS mirror_scope ON mirror(media_type, season, state);
                CREATE TABLE IF NOT EXISTS progress (
                    singleton INTEGER PRIMARY KEY CHECK (singleton=1), scope TEXT NOT NULL,
                    cursor INTEGER NOT NULL, more INTEGER NOT NULL, checked_at REAL NOT NULL);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(mirror)")}
            if "media_source" not in columns:
                db.execute("ALTER TABLE mirror ADD COLUMN media_source TEXT NOT NULL DEFAULT 'themoviedb'")
            if "media_id" not in columns:
                db.execute("ALTER TABLE mirror ADD COLUMN media_id TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE mirror SET media_id=CAST(tmdb_id AS TEXT) WHERE media_id=''")
            db.execute("DROP INDEX IF EXISTS mirror_identity")
            db.execute("CREATE INDEX mirror_identity ON mirror(media_type,media_source,media_id,season,state)")
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
                    if not isinstance(row, dict):
                        raise SharedError("共享记录数据无效")
                    media_source = str(row.get("media_source") or "themoviedb").lower()
                    if media_source in {"tmdb", "themoviedb"}:
                        media_source = "themoviedb"
                    tmdb_id = row.get("tmdb_id")
                    media_id = str(row.get("media_id") or (tmdb_id if tmdb_id else "")).strip()
                    if (not isinstance(row, dict) or row.get("id") != claim
                            or row.get("state") not in {"reserved", "queued", "completed", "released"}
                            or row.get("media_type") not in {"tv", "movie"}
                            or media_source not in {"themoviedb", "douban"}
                            or not media_id.isdecimal() or int(media_id) <= 0 or len(media_id) > 32
                            or media_source == "themoviedb" and (
                                type(tmdb_id) is not int or not 0 < tmdb_id < 2**53
                                or str(tmdb_id) != media_id)
                            or media_source == "douban" and tmdb_id not in (None, 0)
                            or not isinstance(row.get("episodes"), list)
                            or not isinstance(row.get("specs"), dict) or "season" not in row
                            or "site_name" in row and (not isinstance(row.get("site_name"), str)
                                                       or len(row["site_name"]) > 80)):
                        raise SharedError("共享记录数据无效")
                    stored = {**row, "media_source": media_source, "media_id": media_id,
                              "tmdb_id": tmdb_id if media_source == "themoviedb" else None}
                    db.execute("INSERT OR REPLACE INTO mirror "
                               "(id,media_type,tmdb_id,media_source,media_id,season,state,updated,data) "
                               "VALUES (?,?,?,?,?,?,?,?,?)", (
                        claim, row["media_type"], tmdb_id if media_source == "themoviedb" else 0,
                        media_source, media_id, row["season"], row["state"], row["updated"],
                        canonical(stored)))
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

    def search(self, scope, query, status, page, page_size, media_type, owner=""):
        with self.connect() as db:
            db.execute("BEGIN")
            if self.progress(scope, db)["more"]:
                return None
            titles = self._media_titles(db)
            owners = {row[0] for row in db.execute(
                "SELECT DISTINCT json_extract(data, '$.label') FROM mirror "
                "WHERE COALESCE(json_extract(data, '$.label'), '') <> ''")}
            rows = db.execute("SELECT data FROM mirror WHERE (?='' OR state=?) AND (?='' OR media_type=?) "
                              "ORDER BY json_extract(data, '$.created') DESC, id", (status, status, media_type, media_type))
            items = []
            for row in rows:
                item = json.loads(row["data"])
                item["specs"] = normalize_specs(item["specs"])
                item["media_title"] = record_display_title(item, titles)
                if matches_owner(item, owner) and matches_record(item, query):
                    items.append(item)
            return {**record_page(items, page, page_size), "owners": sorted(owners, key=str.casefold)}

    def media_titles(self, scope):
        with self.connect() as db:
            db.execute("BEGIN")
            return {} if self.progress(scope, db)["more"] else self._media_titles(db)

    @staticmethod
    def _media_titles(db):
        rows = db.execute("SELECT media_type, tmdb_id, media_source, media_id, "
                          "json_extract(data, '$.media_title') AS media_title "
                          "FROM mirror WHERE media_title <> '' ORDER BY updated, id")
        return localized_record_titles(map(dict, rows))


def filter_records(db, candidates, rules, *, mirror=False, group=None, completed_only=False):
    available, blocked, identities = [], [], {}
    for candidate in candidates:
        scope = (candidate["media_type"], candidate.get("season"))
        query_keys = _identity_query_keys(candidate, rules)
        query_scope = (*scope, tuple(query_keys) if query_keys is not None else None)
        if query_scope not in identities:
            state = "state='completed'" if completed_only else "state!='released'"
            identity_filter = ""
            params = []
            if query_keys is not None:
                identity_filter = " AND (" + " OR ".join(
                    "(media_source=? AND media_id=?)" for _ in query_keys) + ")"
                params = [value for key in query_keys for value in key]
            if mirror:
                rows = db.execute(
                    f"SELECT data FROM mirror WHERE media_type=?{identity_filter} "
                    f"AND season IS ? AND {state}",
                    (scope[0], *params, scope[1]),
                )
                identities[query_scope] = [json.loads(row["data"]) for row in rows]
            else:
                rows = db.execute(
                    f"SELECT {','.join(RECORD_COLUMNS)} FROM claims "
                    f"WHERE group_id=? AND media_type=?{identity_filter} AND season IS ? AND {state}",
                    (group, scope[0], *params, scope[1]),
                )
                identities[query_scope] = [display_record(row) for row in rows]
        hit = find_resource_conflict(candidate, identities[query_scope], rules)
        if hit:
            blocked.append((candidate, hit))
        else:
            available.append(candidate)
    return available, blocked
