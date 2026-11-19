"""Single-authority, durable resource arbitration. No downloader or public HTTP listener."""

import json
import math
import os
from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from app.sdk.logging import logger

from .channel_dedup import normalize_specs
from .record_search import (
    localized_record_titles, matches_owner, matches_record, record_display_title, record_page,
)
from .shared_cache import (
    RECORD_COLUMNS, SYNC_BATCH, SYNC_MAX_PARTS, SYNC_VERSION, display_record, filter_records,
    has_2160p_conflict, same_resource,
)
from .shared_history import AUTHORITY_VERSION
from .shared_protocol import (
    MAX_PARTS, SharedError, canonical, decode, digest, encode, parse_part, policy,
)


class CoordinatorStore:
    def __init__(self, path, group, attributes, tolerance, rules=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.group = group
        self.policy = policy(attributes, tolerance, rules)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS rpc (
                    sender INTEGER NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    response TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(sender, request_id));
                CREATE TABLE IF NOT EXISTS owners (
                    instance TEXT PRIMARY KEY, sender INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS claims (
                    id TEXT PRIMARY KEY, group_id TEXT NOT NULL, sender INTEGER NOT NULL,
                    instance TEXT NOT NULL, label TEXT NOT NULL, request_id TEXT NOT NULL,
                    file_id TEXT NOT NULL, media_type TEXT NOT NULL, tmdb_id INTEGER NOT NULL,
                    season INTEGER, episodes TEXT NOT NULL, specs TEXT NOT NULL,
                    title TEXT NOT NULL, media_title TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS claims_identity
                    ON claims(group_id, media_type, tmdb_id, season, state);
                CREATE TABLE IF NOT EXISTS fragments (
                    sender INTEGER NOT NULL, request_id TEXT NOT NULL, part INTEGER NOT NULL,
                    total INTEGER NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(sender, request_id, part));
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS completion_imports (
                    group_id TEXT NOT NULL, instance TEXT NOT NULL, evidence_id TEXT NOT NULL,
                    revision TEXT NOT NULL, claim_id TEXT NOT NULL,
                    PRIMARY KEY(group_id, instance, evidence_id, revision));
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(claims)")}
            if "media_title" not in columns:
                db.execute("ALTER TABLE claims ADD COLUMN media_title TEXT NOT NULL DEFAULT ''")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('authority_id', ?)", (canonical(uuid.uuid4().hex),))
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS claim_changes (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, claim_id TEXT NOT NULL,
                    UNIQUE(group_id, claim_id));
                CREATE INDEX IF NOT EXISTS claim_changes_cursor ON claim_changes(group_id, seq);
                CREATE TRIGGER IF NOT EXISTS claim_changes_insert AFTER INSERT ON claims BEGIN
                    INSERT OR REPLACE INTO claim_changes(group_id,claim_id) VALUES (NEW.group_id,NEW.id);
                END;
                CREATE TRIGGER IF NOT EXISTS claim_changes_update AFTER UPDATE OF
                    id,group_id,label,title,media_title,media_type,tmdb_id,season,episodes,specs,
                    state,created,updated,request_id,file_id ON claims BEGIN
                    INSERT OR REPLACE INTO claim_changes(group_id,claim_id)
                        SELECT OLD.group_id,OLD.id WHERE OLD.group_id!=NEW.group_id OR OLD.id!=NEW.id;
                    INSERT OR REPLACE INTO claim_changes(group_id,claim_id) VALUES (NEW.group_id,NEW.id);
                END;
                CREATE TRIGGER IF NOT EXISTS claim_changes_delete AFTER DELETE ON claims BEGIN
                    INSERT OR REPLACE INTO claim_changes(group_id,claim_id) VALUES (OLD.group_id,OLD.id);
                END;
                INSERT INTO claim_changes(group_id,claim_id)
                    SELECT claims.group_id,claims.id FROM claims WHERE NOT EXISTS (
                        SELECT 1 FROM claim_changes WHERE group_id=claims.group_id AND claim_id=claims.id);
                COMMIT;
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def meta(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, canonical(value)))

    def accept_part(self, sender, text):
        request_id, index, count, body = parse_part(text)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM fragments WHERE created < ?", (time.time() - 3600,))
            rows = db.execute("SELECT * FROM fragments WHERE sender=? AND request_id=?",
                              (sender, request_id)).fetchall()
            if any(row["total"] != count or row["part"] == index and row["body"] != body for row in rows):
                raise SharedError("请求分片冲突")
            pending = db.execute("SELECT COUNT(*) FROM fragments WHERE sender=?", (sender,)).fetchone()[0]
            if pending >= MAX_PARTS * 8 and not any(row["part"] == index for row in rows):
                raise SharedError("未完成分片过多，请等待重试")
            db.execute("INSERT OR IGNORE INTO fragments VALUES (?,?,?,?,?,?)",
                       (sender, request_id, index, count, body, time.time()))
            rows = db.execute("SELECT * FROM fragments WHERE sender=? AND request_id=? ORDER BY part",
                              (sender, request_id)).fetchall()
            if len(rows) != count:
                return None
            value = decode([row["body"] for row in rows])
        response = self.handle(sender, request_id, value)
        return encode(response, request_id, response=True)

    def handle(self, sender, request_id, body):
        fingerprint = digest(body)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM rpc WHERE sender=? AND request_id=?",
                             (sender, request_id)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise SharedError("同一请求编号不能更换申请内容")
                return json.loads(old["response"])
            db.execute("SAVEPOINT application")
            try:
                response = self._handle(db, int(sender), request_id, body)
            except (SharedError, ValueError, TypeError, KeyError) as error:
                db.execute("ROLLBACK TO application")
                response = {"ok": False, "error": str(error) if isinstance(error, SharedError)
                            else "申请字段无效", "policy": self.policy}
            db.execute("RELEASE application")
            db.execute("INSERT INTO rpc VALUES (?,?,?,?,?)",
                       (sender, request_id, fingerprint, canonical(response), time.time()))
            return response

    def _handle(self, db, sender, request_id, body):
        if body.get("v") != 1 or body.get("group") != self.group:
            raise SharedError("协议版本或共享组不匹配")
        instance = str(body.get("instance", ""))
        if not re.fullmatch(r"[a-f0-9]{32}", instance):
            raise SharedError("实例标识无效")
        owner = db.execute("SELECT sender FROM owners WHERE instance=?", (instance,)).fetchone()
        if owner and owner["sender"] != sender:
            raise SharedError("实例已经绑定其他 Telegram 账号")
        db.execute("INSERT OR IGNORE INTO owners VALUES (?,?)", (instance, sender))
        db.execute("UPDATE claims SET sender=? WHERE group_id=? AND instance=? AND sender=0",
                   (sender, self.group, instance))
        op = body.get("op")
        if op == "policy":
            return {"ok": True, "policy": self.policy, "user_id": sender, **self.authority()}
        if op == "sync_records":
            return self._sync_records(db, request_id, body)
        if op == "records":
            return {"ok": True, **self.authority(), "records": self.search_records(
                str(body.get("query", ""))[:200], str(body.get("status", "")),
                body.get("page", 1), min(int(body.get("page_size", 20)), 20),
                str(body.get("media_type", "")), str(body.get("owner", ""))[:80])}
        if op == "inspect":
            return self._inspect(db, sender, instance, body)
        if op == "update":
            return self._update(db, sender, instance, body)
        if op == "import_completed":
            return self._import_completed(db, sender, instance, request_id, body)
        if op != "reserve" or body.get("revision") != self.policy["revision"]:
            raise SharedError("共享规则版本不一致，请同步协调端规格设置")
        files = body.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= 500:
            raise SharedError("每批申请须包含 1 至 500 个视频文件")
        validated = [self._file(row) for row in files]
        ids = [row["id"] for row in validated]
        if len(set(ids)) != len(ids):
            raise SharedError("申请中的文件编号重复")
        if body.get("pack_policy") not in {"partial", "exclude"}:
            raise SharedError("整包处理策略无效")
        if self.policy.get("rules") and body["pack_policy"] != self.policy["rules"]["pack_policy"]:
            raise SharedError("整包处理策略与协调端不一致")
        approved, rejected, pending = [], [], []
        now = time.time()
        for item in validated:
            rows = db.execute(
                "SELECT * FROM claims WHERE group_id=? AND media_type=? AND tmdb_id=? "
                "AND season IS ? AND state != 'released'",
                (self.group, item["media_type"], item["tmdb_id"], item["season"]),
            ).fetchall()
            conflict = None
            existing_rows = [dict(row) for row in rows]
            for row in existing_rows + pending:
                high_resolution_row = row
                if isinstance(row.get("episodes"), str) and isinstance(row.get("specs"), str):
                    high_resolution_row = {**row, "episodes": json.loads(row["episodes"]),
                                           "specs": json.loads(row["specs"])}
                if (self.policy.get("rules", {}).get("prefer_2160p", True)
                        and has_2160p_conflict(item, high_resolution_row)):
                    conflict = {**row, "_match_reason": "已有2160p/4K高分辨率资源"}
                    break
                if self._same_identity_file(item, row, overlap=True):
                    conflict = row
                    break
            if conflict:
                rejected.append({"id": item["id"], "reason": conflict.get("_match_reason", "同规格已占位或已完成"),
                                 "owner": conflict["label"], "state": conflict["state"],
                                 "claim": conflict["id"], "created": conflict["created"],
                                 "updated": conflict["updated"]})
                continue
            claim = uuid.uuid4().hex
            row = dict(
                id=claim, group_id=self.group, sender=sender, instance=instance,
                label=str(body.get("label") or instance[:8])[:80], request_id=request_id,
                file_id=item["id"], media_type=item["media_type"], tmdb_id=item["tmdb_id"],
                season=item["season"], episodes=canonical(item["episodes"]), specs=canonical(item["specs"]),
                title=item["title"], media_title=item["media_title"],
                state="reserved", created=now, updated=now,
            )
            pending.append(row)
            approved.append({"id": item["id"], "claim": claim})
        if rejected and body["pack_policy"] == "exclude":
            return {"ok": True, "approved": [], "rejected": rejected,
                    "pack_rejected": True, "policy": self.policy, **self.authority()}
        for row in pending:
            db.execute(
                "INSERT INTO claims "
                "(id,group_id,sender,instance,label,request_id,file_id,media_type,tmdb_id,season,episodes,specs,"
                "title,media_title,state,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row.values()),
            )
        return {"ok": True, "approved": approved, "rejected": rejected,
                "pack_rejected": False, "policy": self.policy, **self.authority()}

    def authority(self):
        return {"authority_version": AUTHORITY_VERSION, "authority_id": self.meta("authority_id"),
                "record_sync_version": SYNC_VERSION}

    def _sync_records(self, db, request_id, body):
        cursor = body.get("cursor")
        if type(cursor) is not int or not 0 <= cursor < 2**63:
            raise SharedError("共享记录同步游标无效")
        authority = self.authority()
        head = db.execute("SELECT COALESCE(MAX(seq),0) FROM claim_changes WHERE group_id=?",
                          (self.group,)).fetchone()[0]
        reset = (body.get("authority_id") != authority["authority_id"]
                 or body.get("revision") != self.policy["revision"] or cursor > head)
        if reset:
            cursor = 0
        columns = ",".join(f"claims.{name}" for name in RECORD_COLUMNS)
        rows = db.execute(
            f"SELECT claim_changes.seq,claim_changes.claim_id,{columns} FROM claim_changes "
            "LEFT JOIN claims ON claims.id=claim_changes.claim_id AND claims.group_id=claim_changes.group_id "
            "WHERE claim_changes.group_id=? AND claim_changes.seq>? ORDER BY claim_changes.seq LIMIT ?",
            (self.group, cursor, SYNC_BATCH + 1),
        ).fetchall()
        changes = [{"seq": row["seq"], "id": row["claim_id"],
                    "record": display_record({key: row[key] for key in RECORD_COLUMNS}) if row["id"] else None}
                   for row in rows[:SYNC_BATCH]]
        while True:
            response = {**authority, "ok": True, "policy": self.policy, "changes": changes, "reset": reset,
                        "cursor": changes[-1]["seq"] if changes else cursor, "more": len(rows) > len(changes)}
            try:
                if len(encode(response, request_id, response=True)) <= SYNC_MAX_PARTS:
                    return response
            except SharedError:
                if len(changes) <= 1:
                    raise
            if len(changes) <= 1:
                raise SharedError("单条共享记录超过同步大小限制")
            changes = changes[:len(changes) // 2]

    def filter_duplicates(self, candidates):
        with self.connect() as db:
            db.execute("BEGIN")
            return filter_records(db, candidates, self.policy, group=self.group)

    def seed_completed(self, instance, label, files):
        """Import the coordinator's own verified legacy evidence before accepting requests."""
        body = {"files": files, "label": label, "revision": self.policy["revision"]}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute("SELECT sender FROM owners WHERE instance=?", (instance,)).fetchone()
            return self._import_completed(db, owner["sender"] if owner else 0, instance,
                                          uuid.uuid4().hex, body)

    def _import_completed(self, db, sender, instance, request_id, body):
        if body.get("revision") != self.policy["revision"]:
            raise SharedError("历史完成证据规则已变更，请重新同步")
        files = body.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= 100:
            raise SharedError("每批历史完成证据须包含 1 至 100 个文件")
        imported = []
        for source in files:
            item = self._file(source)
            completed_at = source.get("completed_at")
            if (type(completed_at) not in {int, float} or not math.isfinite(completed_at)
                    or not 0 < completed_at <= time.time() + 300
                    or item["media_type"] == "tv" and len(item["episodes"]) != 1
                    and not source.get("original_claim")):
                raise SharedError("历史完成证据的时间或逐集信息无效")
            old = db.execute(
                "SELECT claim_id FROM completion_imports WHERE group_id=? AND instance=? AND evidence_id=? AND revision=?",
                (self.group, instance, item["id"], self.policy["revision"])).fetchone()
            original = source.get("original_claim")
            linked = db.execute("SELECT * FROM claims WHERE id=?", (original,)).fetchone() if original else None
            linked_resolution = (normalize_specs(json.loads(linked["specs"])).get("resolution")
                                 if linked and isinstance(linked["specs"], str) else None)
            item_resolution = item["specs"].get("resolution")
            if linked and (linked["group_id"] != self.group or linked["instance"] != instance
                           or linked["request_id"] != source.get("original_request")
                           or linked["file_id"] != source.get("original_file")
                           or linked["state"] == "released"
                           or not self._same_identity_file(item, linked)
                           or self.policy.get("rules", {}).get("prefer_2160p", True)
                           and linked_resolution and item_resolution and linked_resolution != item_resolution
                           or set(item["episodes"]) != set(json.loads(linked["episodes"]))):
                raise SharedError("历史完成证据与原占位身份或归属不一致")
            if linked:
                claim = linked["id"]
                db.execute("UPDATE claims SET state='completed',specs=?,updated=? WHERE id=?",
                           (canonical(item["specs"]), time.time(), claim))
            elif old and db.execute("SELECT 1 FROM claims WHERE id=? AND state='completed'",
                                    (old["claim_id"],)).fetchone():
                claim = old["claim_id"]
            else:
                matches = db.execute(
                    "SELECT * FROM claims WHERE group_id=? AND media_type=? AND tmdb_id=? "
                    "AND season IS ? AND state='completed'",
                    (self.group, item["media_type"], item["tmdb_id"], item["season"])).fetchall()
                match = next((row for row in matches if self._same_completed_evidence(item, row)), None)
                # An original request must retain its original claim link, even after DB recovery.
                claim = original or (match["id"] if match else uuid.uuid4().hex)
                if original or not match:
                    if original and (not re.fullmatch(r"[a-f0-9]{32}", str(original))
                                     or not re.fullmatch(r"[a-f0-9]{32}", str(source.get("original_request", "")))
                                     or not re.fullmatch(r"[a-f0-9]{24}", str(source.get("original_file", "")))):
                        raise SharedError("原占位关联字段无效")
                    db.execute(
                        "INSERT INTO claims "
                        "(id,group_id,sender,instance,label,request_id,file_id,media_type,tmdb_id,season,episodes,specs,"
                        "title,media_title,state,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (claim, self.group, sender, instance, str(body.get("label") or instance[:8])[:80],
                         source.get("original_request") or request_id, source.get("original_file") or item["id"],
                         item["media_type"], item["tmdb_id"], item["season"], canonical(item["episodes"]),
                         canonical(item["specs"]), item["title"], item["media_title"], "completed",
                         completed_at, time.time()))
            db.execute("INSERT OR REPLACE INTO completion_imports VALUES (?,?,?,?,?)",
                       (self.group, instance, item["id"], self.policy["revision"], claim))
            imported.append({"id": item["id"], "claim": claim})
        return {"ok": True, "imported": imported, **self.authority()}

    def _same_identity_file(self, item, row, overlap=False):
        return same_resource(item, {**dict(row), "episodes": json.loads(row["episodes"]),
                                   "specs": json.loads(row["specs"])}, self.policy["attributes"],
                             self.policy["size_tolerance_pct"], overlap=overlap)

    def _same_completed_evidence(self, item, row):
        if not self._same_identity_file(item, row):
            return False
        if not self.policy.get("rules", {}).get("prefer_2160p", True):
            return True
        stored = normalize_specs(json.loads(row["specs"])).get("resolution")
        incoming = item["specs"].get("resolution")
        return stored == incoming

    def _inspect(self, db, sender, instance, body):
        requested = body.get("claims")
        if not isinstance(requested, list) or not 1 <= len(requested) <= 100:
            raise SharedError("无效的占位核验清单")
        results = []
        for item in requested:
            if not isinstance(item, dict):
                raise SharedError("无效的占位核验项")
            row = db.execute(
                "SELECT * FROM claims WHERE id=? AND group_id=? AND sender=? AND instance=? "
                "AND request_id=? AND file_id=?",
                (item.get("claim"), self.group, sender, instance,
                 item.get("request_id"), item.get("file_id"))).fetchone()
            results.append({"claim": item.get("claim"), "state": row["state"] if row else "missing"})
        return {"ok": True, "claims": results, **self.authority()}

    def _file(self, row):
        if not isinstance(row, dict) or not re.fullmatch(r"[a-f0-9]{24}", str(row.get("id", ""))):
            raise SharedError("文件编号无效")
        media_type = row.get("media_type")
        tmdb_id = row.get("tmdb_id")
        if media_type not in {"tv", "movie"} or type(tmdb_id) is not int or not 0 < tmdb_id < 2**53:
            raise SharedError("未核验的媒体身份不能申请")
        season, episodes = row.get("season"), row.get("episodes")
        if media_type == "tv":
            if (type(season) is not int or not 0 <= season <= 1000 or not isinstance(episodes, list)
                    or not 1 <= len(episodes) <= 500 or any(type(ep) is not int or not 0 <= ep <= 100000
                                                          for ep in episodes)):
                raise SharedError("季集号无效")
            episodes = sorted(set(episodes))
        else:
            season, episodes = None, []
        specs = row.get("specs")
        if not isinstance(specs, dict):
            raise SharedError("规格字段无效")
        # Unknown, unselected parser fields must not block an otherwise valid claim.
        specs = {key: specs.get(key) for key in self.policy["attributes"]}
        if any(value is None or value == "" for value in specs.values()):
            raise SharedError("缺少共享规则勾选的规格")
        for key, value in specs.items():
            if key in {"fps", "size"}:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise SharedError("规格数值无效")
            elif not isinstance(value, str) or not 0 < len(value) <= 80:
                raise SharedError("规格文本无效")
        specs = normalize_specs(specs)
        resolution = row.get("specs", {}).get("resolution")
        if resolution is not None:
            if not isinstance(resolution, str) or not 0 < len(resolution) <= 80:
                raise SharedError("分辨率规格无效")
            specs["resolution"] = normalize_specs({"resolution": resolution})["resolution"]
        if any(value is None for value in specs.values()):
            raise SharedError("缺少共享规则勾选的规格")
        media_title = row.get("media_title", "")
        if not isinstance(media_title, str):
            raise SharedError("媒体显示名称无效")
        return {"id": row["id"], "media_type": media_type, "tmdb_id": tmdb_id, "season": season,
                "episodes": episodes, "specs": specs, "title": str(row.get("title", ""))[:160],
                "media_title": media_title[:120]}

    def _update(self, db, sender, instance, body):
        updates = body.get("updates")
        if not isinstance(updates, list) or not 1 <= len(updates) <= 500:
            raise SharedError("无效的状态回报")
        changes = []
        for item in updates:
            if not isinstance(item, dict):
                raise SharedError("无效的状态回报")
            row = db.execute("SELECT * FROM claims WHERE id=? AND sender=? AND instance=? AND group_id=?",
                             (item.get("claim"), sender, instance, self.group)).fetchone()
            target = item.get("state")
            if not row or target not in {"queued", "completed", "released"}:
                raise SharedError("无权更新占位")
            current = row["state"]
            if target == "released" and body.get("confirmed_absent") is not True:
                raise SharedError("释放占位必须确认下载器不存在任务")
            if current == "completed" and target != "completed" or current == "released" and target != "released":
                raise SharedError("不能回退已完成或已释放的占位")
            if target == "queued":
                completed = db.execute(
                    "SELECT * FROM claims WHERE group_id=? AND media_type=? AND tmdb_id=? "
                    "AND season IS ? AND state='completed' AND id!=?",
                    (self.group, row["media_type"], row["tmdb_id"], row["season"], row["id"])).fetchall()
                candidate = {**dict(row), "episodes": json.loads(row["episodes"]), "specs": json.loads(row["specs"])}
                if any(self._same_identity_file(candidate, other, overlap=True) for other in completed):
                    raise SharedError("启动前核验发现同规格资源已完成，保持暂停，不重复下载")
            changes.append((target, time.time(), row["id"]))
        db.executemany("UPDATE claims SET state=?, updated=? WHERE id=?", changes)
        return {"ok": True, "updated": len(changes), **self.authority()}

    def recent(self, limit=100):
        with self.connect() as db:
            rows = db.execute("SELECT id,label,title,media_title,media_type,tmdb_id,season,episodes,specs,state,created,updated "
                              "FROM claims WHERE group_id=? ORDER BY updated DESC LIMIT ?",
                              (self.group, min(int(limit), 100))).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["episodes"], item["specs"] = json.loads(row["episodes"]), normalize_specs(json.loads(row["specs"]))
            result.append(item)
        return result

    def has_unfinished(self):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM claims WHERE group_id=? AND state IN ('reserved','queued') LIMIT 1",
                                   (self.group,)).fetchone())

    def search_records(self, query="", status="", page=1, page_size=20, media_type="", owner=""):
        with self.connect() as db:
            db.execute("BEGIN")
            titles = self._media_titles(db)
            owners = {row[0] for row in db.execute(
                "SELECT DISTINCT label FROM claims WHERE group_id=? AND label<>''", (self.group,))}
            rows = db.execute(
                f"SELECT {','.join(RECORD_COLUMNS)} FROM claims "
                "WHERE group_id=? AND (?='' OR state=?) ORDER BY created DESC, id",
                (self.group, status, status))
            items = []
            for row in rows:
                if media_type and row["media_type"] != media_type:
                    continue
                item = display_record(row)
                item["media_title"] = record_display_title(item, titles)
                if matches_owner(item, owner) and matches_record(item, query):
                    items.append(item)
        return {**record_page(items, page, page_size), "owners": sorted(owners, key=str.casefold)}

    def media_titles(self):
        with self.connect() as db:
            return self._media_titles(db)

    def _media_titles(self, db):
        rows = db.execute(
            "SELECT media_type,tmdb_id,media_title FROM claims "
            "WHERE group_id=? AND media_title<>'' ORDER BY updated, id", (self.group,))
        return localized_record_titles(map(dict, rows))


class BotWorker:
    RETRY_BASE_SECONDS = 5
    RETRY_MAX_SECONDS = 300
    ERROR_LOG_INTERVAL_SECONDS = 300

    def __init__(self, store, token, username, allowed_users, proxies=None, call=None):
        self.store, self.token, self.username = store, token, username.lstrip("@").lower()
        self.allowed_users = set(allowed_users)
        self.proxies, self.custom_call = proxies, call
        self.stop_event = threading.Event()
        self.thread = None
        self.last_error = ""
        self.last_ok = None
        self.ready_event = threading.Event()
        self._sent_at = {}
        self._consecutive_failures = 0
        self._last_logged_error = ""
        self._last_error_logged_at = 0

    @staticmethod
    def _network_error(method, error, requests):
        prefix = f"Telegram Bot {method}"
        exceptions = requests.exceptions
        if isinstance(error, exceptions.ProxyError):
            return f"{prefix} 代理连接失败（ProxyError），请检查 MoviePilot 代理配置和代理连通性"
        if isinstance(error, exceptions.SSLError):
            return f"{prefix} TLS 握手失败（SSLError），请检查代理证书及 NAS 系统时间"
        if isinstance(error, exceptions.ConnectTimeout):
            return f"{prefix} 连接超时（ConnectTimeout），请检查 NAS 出站网络、DNS 和代理"
        if isinstance(error, exceptions.ReadTimeout):
            return f"{prefix} 响应超时（ReadTimeout），请检查代理是否支持 Telegram 长轮询"
        if isinstance(error, exceptions.Timeout):
            return f"{prefix} 请求超时（{type(error).__name__}），请检查 NAS 网络和代理"
        if isinstance(error, exceptions.ConnectionError):
            return f"{prefix} DNS/网络连接失败（{type(error).__name__}），请检查 api.telegram.org 解析及代理"
        if isinstance(error, exceptions.RequestException):
            return f"{prefix} 请求失败（{type(error).__name__}），请检查 NAS 网络和代理"
        return f"{prefix} 网络请求失败（{type(error).__name__}）"

    def _record_failure(self, error):
        self._consecutive_failures = min(self._consecutive_failures + 1, 16)
        retry_after = getattr(error, "retry_after", 0)
        delay = (min(max(int(retry_after), 1), 86400) if retry_after else
                 min(self.RETRY_BASE_SECONDS * 2 ** (self._consecutive_failures - 1), self.RETRY_MAX_SECONDS))
        summary = (str(error) if isinstance(error, SharedError)
                   else f"协调处理异常（{type(error).__name__}），暂缓请求")
        self.last_error = f"{summary}；连续失败 {self._consecutive_failures} 次，{delay} 秒后重试"

        now = time.monotonic()
        if (summary != self._last_logged_error
                or now - self._last_error_logged_at >= self.ERROR_LOG_INTERVAL_SECONDS):
            logger.warning(f"资源刷流协调 Bot 请求失败：{summary}；连续失败 {self._consecutive_failures} 次，"
                           f"{delay} 秒后重试")
            self._last_logged_error = summary
            self._last_error_logged_at = now
        return delay

    def _record_success(self):
        if self._consecutive_failures:
            logger.info(f"资源刷流协调 Bot 连接恢复；此前连续失败 {self._consecutive_failures} 次")
        self._consecutive_failures = 0
        self._last_logged_error = ""
        self._last_error_logged_at = 0
        self.last_error = ""

    def _wait_retry(self, seconds):
        deadline = time.monotonic() + max(0, seconds)
        while not self.stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.stop_event.wait(min(remaining, 30))

    def call(self, method, payload):
        if self.custom_call:
            return self.custom_call(method, payload)
        try:
            import requests
        except ImportError:
            raise SharedError("Telegram Bot 请求依赖缺失（requests），请重新安装资源刷流插件依赖") from None
        try:
            response = requests.post(f"https://api.telegram.org/bot{self.token}/{method}",
                                     json=payload, proxies=self.proxies, timeout=(10, 35))
        except requests.exceptions.RequestException as error:
            raise SharedError(self._network_error(method, error, requests)) from None
        except Exception as error:
            raise SharedError(f"Telegram Bot {method} 请求准备失败（{type(error).__name__}）") from None
        try:
            data = response.json()
        except ValueError:
            status = getattr(response, "status_code", 0)
            raise SharedError(f"Telegram Bot {method} 返回无法解析的响应（HTTP {status}），"
                              "可能是代理或网关返回了非 JSON 页面") from None
        except Exception as error:
            status = getattr(response, "status_code", 0)
            raise SharedError(f"Telegram Bot {method} 响应解析失败（HTTP {status}，{type(error).__name__}）") from None
        if not isinstance(data, dict):
            status = getattr(response, "status_code", 0)
            raise SharedError(f"Telegram Bot {method} 响应格式无效（HTTP {status}）")
        if not data.get("ok"):
            parameters = data.get("parameters")
            retry = parameters.get("retry_after") if isinstance(parameters, dict) else None
            try:
                retry_seconds = min(max(int(retry), 1), 86400) if retry else 0
            except (TypeError, ValueError):
                retry_seconds = 0
            if retry_seconds:
                self.store.set_meta("retry_at", time.time() + retry_seconds)
            try:
                error_code = int(data.get("error_code") or getattr(response, "status_code", 0) or 0)
            except (TypeError, ValueError):
                error_code = 0
            if error_code == 401:
                reason = "Bot Token 无效或已撤销"
            elif error_code == 409:
                reason = "同一 Bot 可能已有其他 getUpdates 接收器，或 Webhook 尚未关闭"
            elif error_code == 429:
                reason = f"Telegram 限流{f'，约 {retry_seconds} 秒后重试' if retry_seconds else ''}"
            else:
                reason = "Telegram API 拒绝请求"
            failure = SharedError(f"Telegram Bot {method} 请求未确认（{error_code}）：{reason}")
            if retry_seconds:
                failure.retry_after = retry_seconds
            raise failure
        return data.get("result")

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name="ResourceFlowCoordinator")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=50)
            if self.thread.is_alive():
                raise SharedError("协调线程尚未退出，暂不重启另一个接收器")

    def run(self):
        import fcntl
        lock_path = self.store.path.with_suffix(".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.last_error = "同一数据库已有协调接收器运行"
                return
            verified = False
            while not self.stop_event.is_set():
                try:
                    retry_at = self.store.meta("retry_at", 0)
                    if retry_at > time.time():
                        self._wait_retry(min(retry_at - time.time(), 30))
                        continue
                    if not verified:
                        me = self.call("getMe", {})
                        if str(me.get("username", "")).lower() != self.username:
                            raise SharedError("Bot Token 与协调 Bot 用户名不一致")
                        if (self.call("getWebhookInfo", {}) or {}).get("url"):
                            raise SharedError("此 Bot 已设置 Webhook，请使用独立的协调 Bot")
                        verified = True
                        self.ready_event.set()
                    self.poll_once()
                    self.last_ok = time.time()
                    self._record_success()
                except Exception as error:
                    self._wait_retry(self._record_failure(error))
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def poll_once(self):
        updates = self.call("getUpdates", {
            "offset": int(self.store.meta("offset", 0)), "timeout": 25, "limit": 50,
            "allowed_updates": ["message"],
        }) or []
        for update in updates:
            if self.stop_event.is_set():
                return
            message = update.get("message") or {}
            sender = (message.get("from") or {}).get("id")
            chat = message.get("chat") or {}
            if sender in self.allowed_users and chat.get("type") == "private" and chat.get("id") == sender:
                text = message.get("text", "")
                if text.startswith("RFQ1 "):
                    try:
                        response = self.store.accept_part(sender, text)
                    except SharedError:
                        response = None
                    for part in response or []:
                        wait = self._sent_at.get(sender, 0) + 1.1 - time.monotonic()
                        if wait > 0 and self.stop_event.wait(wait):
                            return
                        self.call("sendMessage", {"chat_id": sender, "text": part,
                                                  "disable_web_page_preview": True})
                        self._sent_at[sender] = time.monotonic()
            # Failed sends never advance this cursor; replay is idempotent after restart.
            self.store.set_meta("offset", int(update["update_id"]) + 1)
