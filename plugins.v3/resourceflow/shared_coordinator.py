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

from .channel_dedup import specs_match
from .shared_protocol import (
    MAX_PARTS, SharedError, canonical, decode, digest, encode, parse_part, policy,
)


class CoordinatorStore:
    def __init__(self, path, group, attributes, tolerance):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.group = group
        self.policy = policy(attributes, tolerance)
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
                    title TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS claims_identity
                    ON claims(group_id, media_type, tmdb_id, season, state);
                CREATE TABLE IF NOT EXISTS fragments (
                    sender INTEGER NOT NULL, request_id TEXT NOT NULL, part INTEGER NOT NULL,
                    total INTEGER NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(sender, request_id, part));
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
        op = body.get("op")
        if op == "policy":
            return {"ok": True, "policy": self.policy, "user_id": sender}
        if op == "update":
            return self._update(db, sender, instance, body)
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
        approved, rejected, pending = [], [], []
        now = time.time()
        for item in validated:
            rows = db.execute(
                "SELECT * FROM claims WHERE group_id=? AND media_type=? AND tmdb_id=? "
                "AND season IS ? AND state != 'released'",
                (self.group, item["media_type"], item["tmdb_id"], item["season"]),
            ).fetchall()
            conflict = None
            for row in [dict(row) for row in rows] + pending:
                if (row["media_type"], row["tmdb_id"], row["season"]) != (
                        item["media_type"], item["tmdb_id"], item["season"]):
                    continue
                episodes = json.loads(row["episodes"])
                overlap = item["media_type"] == "movie" or bool(set(item["episodes"]) & set(episodes))
                if overlap and specs_match(item, {"specs": json.loads(row["specs"])},
                                           self.policy["attributes"], self.policy["size_tolerance_pct"]):
                    conflict = row
                    break
            if conflict:
                rejected.append({"id": item["id"], "reason": "同规格已占位或已完成",
                                 "owner": conflict["label"], "state": conflict["state"]})
                continue
            claim = uuid.uuid4().hex
            row = dict(
                id=claim, group_id=self.group, sender=sender, instance=instance,
                label=str(body.get("label") or instance[:8])[:80], request_id=request_id,
                file_id=item["id"], media_type=item["media_type"], tmdb_id=item["tmdb_id"],
                season=item["season"], episodes=canonical(item["episodes"]), specs=canonical(item["specs"]),
                title=item["title"], state="reserved", created=now, updated=now,
            )
            pending.append(row)
            approved.append({"id": item["id"], "claim": claim})
        if rejected and body["pack_policy"] == "exclude":
            return {"ok": True, "approved": [], "rejected": rejected,
                    "pack_rejected": True, "policy": self.policy}
        for row in pending:
            db.execute("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(row.values()))
        return {"ok": True, "approved": approved, "rejected": rejected,
                "pack_rejected": False, "policy": self.policy}

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
        return {"id": row["id"], "media_type": media_type, "tmdb_id": tmdb_id, "season": season,
                "episodes": episodes, "specs": specs, "title": str(row.get("title", ""))[:160]}

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
            changes.append((target, time.time(), row["id"]))
        db.executemany("UPDATE claims SET state=?, updated=? WHERE id=?", changes)
        return {"ok": True, "updated": len(changes)}

    def recent(self, limit=100):
        with self.connect() as db:
            rows = db.execute("SELECT id,label,title,media_type,tmdb_id,season,episodes,specs,state,updated "
                              "FROM claims WHERE group_id=? ORDER BY updated DESC LIMIT ?",
                              (self.group, min(int(limit), 100))).fetchall()
        return [{**dict(row), "episodes": json.loads(row["episodes"]), "specs": json.loads(row["specs"])}
                for row in rows]

    def has_unfinished(self):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM claims WHERE group_id=? AND state IN ('reserved','queued') LIMIT 1",
                                   (self.group,)).fetchone())


class BotWorker:
    def __init__(self, store, token, username, allowed_users, proxies=None, call=None):
        self.store, self.token, self.username = store, token, username.lstrip("@").lower()
        self.allowed_users = set(allowed_users)
        self.proxies, self.custom_call = proxies, call
        self.stop_event = threading.Event()
        self.thread = None
        self.last_error = ""
        self.last_ok = None
        self._sent_at = {}

    def call(self, method, payload):
        if self.custom_call:
            return self.custom_call(method, payload)
        import requests
        try:
            response = requests.post(f"https://api.telegram.org/bot{self.token}/{method}",
                                     json=payload, proxies=self.proxies, timeout=(10, 35))
            data = response.json()
        except Exception:
            raise SharedError("Telegram Bot 网络请求失败") from None
        if not data.get("ok"):
            retry = (data.get("parameters") or {}).get("retry_after")
            if retry:
                self.store.set_meta("retry_at", time.time() + min(max(int(retry), 1), 86400))
            raise SharedError(f"Telegram Bot 请求未确认（{int(data.get('error_code') or 0)}）")
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
                        self.stop_event.wait(min(retry_at - time.time(), 30))
                        continue
                    if not verified:
                        me = self.call("getMe", {})
                        if str(me.get("username", "")).lower() != self.username:
                            raise SharedError("Bot Token 与协调 Bot 用户名不一致")
                        if (self.call("getWebhookInfo", {}) or {}).get("url"):
                            raise SharedError("此 Bot 已设置 Webhook，请使用独立的协调 Bot")
                        verified = True
                    self.poll_once()
                    self.last_ok, self.last_error = time.time(), ""
                except Exception as error:
                    self.last_error = str(error) if isinstance(error, SharedError) else "协调处理失败，保留占位"
                    self.stop_event.wait(5)
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
