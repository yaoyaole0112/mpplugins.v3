"""Download admission and conservative recovery for cross-instance Telegram arbitration."""

import copy
import hashlib
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from app import schemas
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.media import MetaInfo
from app.sdk.services import DownloaderHelper
from app.modules.themoviedb.tmdbapi import TmdbApi

from .active_dedup import download_files, paths_agree
from .channel_dedup import normalize_specs, parse_candidate_file, parse_torrent_files
from .models import ChannelDedupSettingsPayload
from .record_search import (
    matches_media_type, matches_owner, matches_record, record_display_title, record_owners, record_page,
    record_title_key,
)
from .shared_cache import SYNC_VERSION, SharedRecordCache, same_resource
from .shared_coordinator import BotWorker, CoordinatorStore
from .shared_history import AUTHORITY_VERSION, completion_rows
from .shared_models import SharedSettingsPayload
from .shared_protocol import RULE_DEFAULTS, SharedError, digest, policy
from .telegram_rpc import TelegramRPC, ed2k_account


class SharedResourceMixin:
    _shared_cache = None
    _shared_cache_activity = 0
    _shared_cache_next_sync = 0
    _shared_cache_sync_failed = False

    def _shared_rules(self):
        return {key: getattr(self, f"_channel_dedup_{key}", default) for key, default in RULE_DEFAULTS.items()}

    def _local_shared_policy(self):
        return policy(self._channel_dedup_attributes, self._channel_dedup_size_tolerance_pct, self._shared_rules())

    def _apply_shared_policy(self, value):
        if not isinstance(value, dict) or "rules" not in value:
            raise SharedError("协调端尚不支持统一资源规则，请将协调端更新至 1.0.3 或以上")
        expected = policy(value.get("attributes", []), value.get("size_tolerance_pct", 5), value["rules"])
        if expected != value:
            raise SharedError("协调端规则版本或内容无效")
        current = self._local_shared_policy() if self._channel_dedup_attributes else None
        if self._shared_settings.host_coordinator:
            if expected != current:
                raise SharedError("协调端规则与本机设置不一致")
            return
        if expected == current:
            return
        with self._channel_state_lock:
            self._channel_dedup_attributes = list(expected["attributes"])
            self._channel_dedup_size_tolerance_pct = expected["size_tolerance_pct"]
            for key, setting in expected["rules"].items():
                setattr(self, f"_channel_dedup_{key}", setting)
            self._channel_identity_cache.clear()
            self._inflight_task_snapshot = (0, [])
            self._channel_generation += 1
        self._save_config()

    def _sync_shared_policy(self):
        with self._shared_rpc_lock:
            self._check_shared_available()
            if self._shared_settings.host_coordinator:
                self._ensure_shared_worker()
                self._apply_shared_policy(self._shared_store.policy)
                response = {"policy": self._shared_store.policy, **self._shared_store.authority()}
            else:
                response = getattr(self, "_shared_probe", None)
                if not response or time.time() - getattr(self, "_shared_policy_checked_at", 0) >= 600:
                    response = self._shared_exchange(uuid.uuid4().hex, {"op": "policy"})
                    self._shared_policy_checked_at = time.time()
                self._apply_shared_policy(response.get("policy"))
            self._shared_probe = response
            if self._shared_settings.host_coordinator:
                self._shared_policy_checked_at = time.time()
            self._require_shared_authority(response)
            self._sync_shared_history(store=self._shared_store if self._shared_settings.host_coordinator else None)
            self._audit_shared_links()

    @staticmethod
    def _require_shared_authority(response):
        if (response.get("authority_version", 0) < AUTHORITY_VERSION
                or not re.fullmatch(r"[a-f0-9]{32}", str(response.get("authority_id", "")))):
            raise SharedError("请先将协调端更新至 1.0.17 或以上，再启用统一共享资源记录")

    def _shared_cache_scope(self, response):
        return digest([self._shared_settings.bot_username.lower(), self._shared_settings.group,
                       self._shared_instance_id, response["authority_id"], response["policy"]["revision"], SYNC_VERSION])

    def _shared_cache_probe(self):
        probe = getattr(self, "_shared_probe", None) or {}
        if self._shared_cache and probe.get("record_sync_version") == SYNC_VERSION:
            return probe
        return None

    def _sync_shared_record_cache(self):
        probe = self._shared_cache_probe()
        if not probe or self._shared_settings.host_coordinator:
            return
        now = time.time()
        if now < self._shared_cache_next_sync or now < getattr(self, "_shared_retry_at", 0):
            return
        self._shared_cache_next_sync = now + 60
        try:
            progress = self._shared_cache.progress(self._shared_cache_scope(probe))
            active = now - self._shared_cache_activity < 180
            if not active and not self._shared_cache_sync_failed and not progress["more"] and now - progress["checked_at"] < 540:
                return
            result = self._shared_exchange(uuid.uuid4().hex, {
                "op": "sync_records", "authority_id": probe["authority_id"],
                "revision": probe["policy"]["revision"], "cursor": progress["cursor"],
            })
            self._require_shared_authority(result)
            if result.get("record_sync_version") != SYNC_VERSION:
                raise SharedError("协调端共享记录同步协议不匹配")
            self._apply_shared_policy(result.get("policy"))
            self._shared_probe = {**probe, "policy": result["policy"], "authority_id": result["authority_id"]}
            self._shared_policy_checked_at = now
            self._shared_cache.apply(self._shared_cache_scope(self._shared_probe), progress["cursor"], result, now)
            self._shared_cache_sync_failed = False
        except (SharedError, sqlite3.Error, KeyError, TypeError, ValueError) as error:
            self._shared_cache_sync_failed = True
            self._shared_cache_next_sync = now + 120
            logger.warning(f"共享下载记录同步暂缓：{error}；未确认资源仍须协调端审批")

    def _filter_shared_record_cache(self, candidates, title):
        if not candidates:
            return candidates, []
        self._shared_cache_activity = time.time()
        store = getattr(self, "_shared_store", None)
        probe = self._shared_cache_probe()
        try:
            if self._shared_settings.host_coordinator and store:
                available, blocked = store.filter_duplicates(candidates)
            elif (probe and not self._shared_cache_sync_failed
                  and probe["policy"]["revision"] == self._local_shared_policy()["revision"]):
                available, blocked = self._shared_cache.filter(
                    self._shared_cache_scope(probe), candidates, probe["policy"], self._shared_cache_activity)
            else:
                return candidates, []
        except (sqlite3.Error, KeyError, TypeError, ValueError) as error:
            logger.warning(f"共享下载记录本地对比不可用：{error}；转协调端审批")
            return candidates, []
        for candidate, row in blocked:
            episodes = ",".join(f"{episode:02d}" for episode in candidate["episodes"])
            scope = f"S{candidate['season']:02d}E{episodes}" if candidate["media_type"] == "tv" else "电影"
            reason = row.get("_match_reason", "同规格已占位或已完成")
            logger.info(f"共享下载记录本地命中 {title}：TMDB {candidate['tmdb_id']} · {scope}；"
                        f"原因：{reason}；下载方：{row['label']}；状态 {row['state']}；"
                        f"占位 {row['id']}，不发送占位申请")
        return available, blocked

    def _shared_history_rows(self):
        records = [*getattr(self, "_resource_ledger", {}).values(),
                   *getattr(self, "_shared_completed_outbox", {}).values()]
        changed = False
        with self._shared_lock:
            for entry in getattr(self, "_shared_claims", {}).values():
                completed = set(entry.get("done", [])) | set(entry.get("observed_completed", []))
                for row in entry.get("files", []):
                    if row.get("id") not in completed or row.get("id") not in entry.get("approved", {}):
                        continue
                    times = entry.setdefault("file_completed_at", {})
                    if row["id"] not in times:
                        times[row["id"]] = entry.get("completed_at") or entry.get("updated") or entry.get("created")
                        changed = True
                    records.append({
                        **row, "evidence_source": "completed", "remembered_at": times[row["id"]],
                        "original_claim": entry["approved"][row["id"]],
                        "original_request": entry["request_id"], "original_file": row["id"],
                    })
            if changed:
                self._save_shared()
        return completion_rows(records, self._channel_dedup_attributes)

    def _sync_shared_history(self, store=None):
        """Acknowledged local history is an archive, never a second admission authority."""
        response = store.authority() if store else getattr(self, "_shared_probe", None) or {}
        self._require_shared_authority(response)
        scope = digest([self._shared_settings.bot_username.lower(), self._shared_settings.group,
                        response["authority_id"], self._local_shared_policy()["revision"],
                        self._shared_instance_id])
        progress = getattr(self, "_shared_history", {})
        previous = digest({key: value for key, value in progress.items() if key != "last_sync_at"})
        if progress.get("scope") != scope:
            progress = self._shared_history = {"scope": scope, "acknowledged": {},
                                               "repair": progress.get("repair", "")}
        rows, skipped = self._shared_history_rows()
        progress["unverified"] = skipped
        waiting = [row for row in rows if row["id"] not in progress["acknowledged"]]
        progress["pending"] = len(waiting)
        for start in range(0, len(waiting), 100):
            batch = waiting[start:start + 100]
            body = {"op": "import_completed", "revision": self._local_shared_policy()["revision"],
                    "files": batch, "repair": progress.get("repair", "")}
            if store:
                result = store.seed_completed(self._shared_instance_id,
                                              self._shared_settings.instance_label, batch)
            else:
                result = self._shared_exchange(digest([scope, body])[:32], body)
            imported = result.get("imported")
            if (not isinstance(imported, list) or len(imported) != len(batch)
                    or {row.get("id") for row in imported} != {row["id"] for row in batch}
                    or any(not re.fullmatch(r"[a-f0-9]{32}", str(row.get("claim", ""))) for row in imported)):
                raise SharedError("历史完成证据回执不完整，暂缓新增下载")
            progress["acknowledged"].update({row["id"]: row["claim"] for row in imported})
            progress["pending"] -= len(batch)
            self.save_data("shared_history", progress)
        progress["last_sync_at"] = time.time()
        self._shared_history = progress
        if waiting or previous != digest({key: value for key, value in progress.items() if key != "last_sync_at"}):
            self.save_data("shared_history", progress)

    def _capture_shared_completed(self, task, torrent_hash, records):
        """Keep a durable outbox only for verified completed files lacking an approval."""
        with self._shared_lock:
            approved_paths = {item["path"] for entry in self._shared_claims.values()
                              if entry["hash"] == torrent_hash and entry["downloader"] == task.downloader
                              for item in entry.get("files", []) if item["id"] in entry.get("approved", {})}
            outbox = dict(getattr(self, "_shared_completed_outbox", {}))
            for row in records:
                if any(paths_agree(row["path"], path) for path in approved_paths):
                    continue
                value = {**copy.deepcopy(row), "evidence_source": "completed"}
                key = digest(value)
                if key not in outbox:
                    outbox[key] = {**value, "remembered_at": time.time()}
            self._shared_completed_outbox = outbox
            self.save_data("shared_completed_outbox", outbox)

    def _audit_shared_links(self, force=False):
        if not force and time.time() - getattr(self, "_shared_audit_at", 0) < 600:
            return
        requested, owners = [], {}
        for entry in self._shared_claims.values():
            for file_id, claim in entry.get("approved", {}).items():
                requested.append({"claim": claim, "request_id": entry["request_id"], "file_id": file_id})
                owners[claim] = entry
        problems = []
        for start in range(0, len(requested), 100):
            batch = requested[start:start + 100]
            response = self._shared_exchange(uuid.uuid4().hex, {"op": "inspect", "claims": batch})
            states = response.get("claims")
            if (not isinstance(states, list) or len(states) != len(batch)
                    or {item.get("claim") for item in states} != {item["claim"] for item in batch}):
                raise SharedError("共享占位核验回复不完整，暂缓新增下载")
            for item in states:
                entry = owners[item["claim"]]
                file_id = next(key for key, claim in entry["approved"].items() if claim == item["claim"])
                expected = ("completed" if file_id in entry.get("done", []) else
                            "released" if entry["phase"] == "released" else None)
                if (item["state"] == "missing" or expected and item["state"] != expected
                        or item["state"] == "released" and entry["phase"] != "released"):
                    problems.append(item["claim"])
                    entry["integrity_error"] = "共享占位缺失或状态不一致，等待核验；不会重新申请"
                elif item["state"] not in {"reserved", "queued", "completed", "released"}:
                    raise SharedError("共享占位状态无效")
                elif item["state"] == "completed":
                    entry["done"] = sorted(set(entry.get("done", [])) | {file_id})
        self._shared_integrity_error = f"共享占位关联异常 {len(problems)} 个，暂缓新增下载" if problems else ""
        if not problems:
            for entry in self._shared_claims.values():
                entry.pop("integrity_error", None)
        self._save_shared()
        if problems:
            # Force a replay of confirmed completion evidence on the next attempt.
            self._shared_history = {"repair": uuid.uuid4().hex}
            self.save_data("shared_history", self._shared_history)
            raise SharedError(self._shared_integrity_error)
        self._shared_audit_at = time.time()

    def update_channel_dedup_settings(self, payload: ChannelDedupSettingsPayload) -> schemas.Response:
        with getattr(self, "_shared_rpc_lock", threading.RLock()):
            store = getattr(self, "_shared_store", None)
            fields = {"attributes": list(self._channel_dedup_attributes),
                      "size_tolerance_pct": self._channel_dedup_size_tolerance_pct, **self._shared_rules()}
            changed = any(getattr(payload, key) != value for key, value in fields.items()
                          if key in payload.model_fields_set)
            config = getattr(self, "_shared_settings", None)
            if config and not config.host_coordinator:
                if changed:
                    return schemas.Response(success=False, message="全局去重字段和资源规则仅可在协调端设置")
            payload = payload.model_copy(update={
                key: getattr(payload, key) if key in payload.model_fields_set else value
                for key, value in fields.items()
            })
            try:
                if changed and any(row["phase"] not in {"released", "completed", "rejected"}
                                   for row in getattr(self, "_shared_claims", {}).values()):
                    return schemas.Response(success=False, message="仍有未完成共享申请，暂不变更协调端规则")
                if store and changed:
                    self._stop_shared()
                    self._shared_stopping = False
                    if store.has_unfinished():
                        return schemas.Response(success=False, message="协调端仍有未完成共享占位，暂不变更匹配字段或容差")
                response = self._update_resource_dedup_settings(payload)
                if response.success and changed and config and config.host_coordinator:
                    self._shared_policy_changed()
                return response
            finally:
                if store and changed and not self._shared_worker:
                    self._shared_stopping = False
                    self._start_shared_worker()

    def poll_channel_messages(self, force=False):
        return None

    def request_channel_dedup_poll(self) -> schemas.Response:
        return schemas.Response(success=False, message="资源刷流不读取频道，请使用 Telegram 共享审批")

    def request_shared_reconcile(self) -> schemas.Response:
        from app.runtime.thread import ThreadHelper
        ThreadHelper().submit(self.reconcile_shared_claims)
        return schemas.Response(success=True, message="已提交共享申请核验")

    def _shared_policy_changed(self):
        if not getattr(self, "_shared_settings", None) or not self._shared_settings.host_coordinator:
            return
        with self._shared_rpc_lock:
            self._stop_shared()
            self._shared_stopping = False
            self._start_shared_worker()

    def _init_shared(self, config):
        self._shared_lock = getattr(self, "_shared_lock", threading.RLock())
        self._shared_rpc_lock = getattr(self, "_shared_rpc_lock", threading.RLock())
        self._shared_worker = None
        self._shared_store = None
        self._shared_stopping = False
        self._shared_transport_lock = threading.Lock()
        self._shared_worker_retry_at = 0
        self._shared_error = self.get_data("shared_retry_error") or ""
        self._shared_last_ok = None
        raw = dict(config.get("shared") or {})
        instance_id = raw.pop("instance_id", None)
        self._shared_instance_id = instance_id if isinstance(instance_id, str) and len(instance_id) == 32 else uuid.uuid4().hex
        self._shared_settings = SharedSettingsPayload.model_validate(raw)
        self._shared_claims = self.get_data("shared_claims") or {}
        self._shared_history = self.get_data("shared_history") or {}
        self._shared_completed_outbox = self.get_data("shared_completed_outbox") or {}
        self._shared_integrity_error = ""
        self._shared_audit_at = 0
        self._shared_retry_at = float(self.get_data("shared_retry_at") or 0)
        self._shared_probe = None
        self._shared_cache = None
        self._shared_cache_next_sync = 0
        self._shared_cache_activity = time.time()
        self._shared_cache_sync_failed = False
        try:
            self._shared_cache = SharedRecordCache(Path(self.get_data_path()) / "shared-record-cache.sqlite3")
        except (OSError, sqlite3.Error) as error:
            logger.warning(f"无法打开共享下载记录本地缓存：{error}；继续使用协调端审批")
        self._start_shared_worker()

    def _shared_config(self):
        value = getattr(self, "_shared_settings", SharedSettingsPayload()).model_dump()
        return {**value, "instance_id": getattr(self, "_shared_instance_id", "")}

    def _start_shared_worker(self):
        config = self._shared_settings
        if not config.host_coordinator:
            return
        if not config.bot_token:
            self._shared_error = "协调端尚未配置 Bot Token"
            self._shared_worker_retry_at = time.time() + 30
            return
        try:
            route = digest([config.bot_username, config.group])[:16]
            self._shared_store = CoordinatorStore(
                Path(self.get_data_path()) / f"coordinator-{route}.sqlite3", config.group,
                self._channel_dedup_attributes, self._channel_dedup_size_tolerance_pct,
                self._shared_rules(),
            )
            self._sync_shared_history(store=self._shared_store)
            self._shared_worker = BotWorker(
                self._shared_store, config.bot_token, config.bot_username, config.allowed_users,
                proxies=settings.PROXY,
            )
            self._shared_worker.start()
            self._shared_error = ""
            self._shared_worker_retry_at = 0
        except Exception as error:
            self._shared_store = None
            self._shared_worker = None
            self._shared_worker_retry_at = time.time() + 30
            detail = str(error).strip().replace("\n", " ")[:160]
            cause = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
            self._shared_error = f"协调服务启动失败（{cause}），30 秒后自动重试"

    def _ensure_shared_worker(self):
        if self._shared_stopping:
            raise SharedError("本机协调端正在停止或重启，暂缓新增下载")
        if not self._shared_settings.host_coordinator:
            raise SharedError("当前实例未启用本机协调端")
        worker = self._shared_worker
        if worker and worker.thread and worker.thread.is_alive() and self._shared_store:
            if worker.ready_event.is_set():
                return
            reason = worker.last_error or "协调 Bot 正在连接 Telegram"
            raise SharedError(f"{reason}，暂缓新增下载")
        if worker and worker.thread and not worker.thread.is_alive() and not self._shared_worker_retry_at:
            self._shared_error = worker.last_error or "协调 Bot 接收线程已停止"
            self._shared_worker_retry_at = time.time() + 30
        if time.time() < self._shared_worker_retry_at:
            reason = self._shared_error or "协调服务启动失败"
            raise SharedError(f"{reason}，暂缓新增下载")
        self._shared_worker = None
        self._shared_store = None
        self._start_shared_worker()
        worker = self._shared_worker
        if not self._shared_store or not worker or not worker.thread or not worker.thread.is_alive():
            raise SharedError(f"{self._shared_error or '本机协调端启动失败'}，暂缓新增下载")
        if not worker.ready_event.is_set():
            reason = worker.last_error or "协调 Bot 正在连接 Telegram"
            raise SharedError(f"{reason}，暂缓新增下载")

    def _stop_shared(self):
        self._shared_stopping = True
        worker = getattr(self, "_shared_worker", None)
        if worker:
            worker.stop()
            self._shared_worker = None
        self._shared_store = None

    def _shared_request_status(self, row, titles=None):
        files = row.get("files") or []
        approved = row.get("approved") or {}
        rejected = row.get("rejected") or []
        decisions = {item["id"]: item for item in rejected if isinstance(item, dict) and "id" in item}
        task = getattr(self, "_task_configs", {}).get(row.get("task_id"))
        return {
            "request_id": row["request_id"], "title": row["title"], "phase": row["phase"],
            "task_id": row["task_id"], "task_name": getattr(task, "name", None),
            "download_owners": sorted({
                *([getattr(getattr(self, "_shared_settings", None), "instance_label", "")]
                  if approved else []),
                *(item.get("owner", "") for item in rejected if isinstance(item, dict) and item.get("owner")),
            }),
            "downloader": row.get("downloader"), "created": row.get("created"), "updated": row.get("updated"),
            "download_started_at": row.get("download_started_at"),
            "timeout_at": row.get("timeout_at"), "timeout_reason": row.get("timeout_reason", ""),
            "error": "" if row["phase"] == "rejected" and any(item.get("reason") for item in decisions.values())
            else row.get("error", ""),
            "approved": len(approved), "rejected": rejected,
            "integrity_error": row.get("integrity_error", ""),
            "file_count": len(files), "completed": len(row.get("done") or []),
            "files": [{**{key: item.get(key) for key in
                         ("id", "path", "media_type", "tmdb_id", "season", "episodes")},
                       "media_title": record_display_title(item, titles or {}),
                       "specs": normalize_specs(item.get("specs") or {}),
                       "decision": "approved" if item.get("id") in approved else
                       "rejected" if item.get("id") in decisions else
                       "not_approved" if row["phase"] == "rejected" else "pending",
                       "reason": decisions.get(item.get("id"), {}).get("reason", ""),
                       "owner": decisions.get(item.get("id"), {}).get("owner", "")}
                      for item in files],
            "claim_links": list(approved.values()),
        }

    def get_shared_status(self) -> schemas.Response:
        config = self._shared_config()
        token = config.pop("bot_token", None)
        worker = getattr(self, "_shared_worker", None)
        claims = list(getattr(self, "_shared_claims", {}).values())
        return schemas.Response(success=True, data={
            **config, "token_configured": bool(token),
            "coordinator_running": bool(worker and worker.thread and worker.thread.is_alive()
                                         and getattr(worker, "ready_event", None)
                                         and worker.ready_event.is_set()),
            "last_error": self._shared_error or (worker.last_error if worker else ""),
            "last_ok": self._shared_last_ok, "coordinator_last_ok": worker.last_ok if worker else None,
            "retry_at": self._shared_retry_at,
            "policy": self._local_shared_policy()
            if self._channel_dedup_attributes else None,
            "requests": [self._shared_request_status(row)
                         for row in sorted(claims, key=lambda row: row.get("updated", 0), reverse=True)[:100]],
            "records": self._shared_store.recent() if self._shared_store else [],
            "probe": self._shared_probe,
            "history_sync": {key: value for key, value in getattr(self, "_shared_history", {}).items()
                             if key != "acknowledged"},
            "integrity_error": getattr(self, "_shared_integrity_error", ""),
        })

    def get_shared_records(self, kind: str = "requests", query: str = "", status: str = "",
                           page: int = 1, page_size: int = 20, media_type: str = "",
                           owner: str = "") -> schemas.Response:
        if kind not in {"requests", "records"}:
            return schemas.Response(success=False, message="无效的记录类别")
        if kind == "records":
            store = getattr(self, "_shared_store", None)
            if store:
                data = store.search_records(query, status, page, page_size, media_type, owner)
            elif getattr(self, "_shared_settings", None) and self._shared_settings.enabled:
                self._shared_cache_activity = time.time()
                probe = self._shared_cache_probe()
                if probe:
                    try:
                        data = self._shared_cache.search(self._shared_cache_scope(probe), query, status, page,
                                                         page_size, media_type, owner)
                    except (sqlite3.Error, KeyError, TypeError, ValueError):
                        data = None
                    if data is not None:
                        return schemas.Response(success=True, data=self._shared_record_display_page(data, kind))
                try:
                    with self._shared_rpc_lock:
                        response = self._shared_exchange(uuid.uuid4().hex, {
                            "op": "records", "query": query[:200], "status": status,
                            "page": page, "page_size": min(page_size, 20), "media_type": media_type,
                            "owner": owner[:80]})
                    self._require_shared_authority(response)
                    data = response["records"]
                    if owner and "owners" not in data:
                        raise SharedError("协调端版本较旧，暂不支持下载方筛选；请先更新协调端")
                except SharedError as error:
                    return schemas.Response(success=False, message=str(error))
            else:
                data = record_page([], page, page_size)
        else:
            titles = self._resource_display_titles()
            with self._shared_lock:
                rows = [self._shared_request_status(row, titles) for row in self._shared_claims.values()]
            owners = set()
            filtered = []
            for row in rows:
                owners.update(record_owners(row))
                phase = row.get("phase")
                matches_status = (not status or phase == status
                                  or status == "queued" and phase == "awaiting_start")
                if (matches_status and matches_media_type(row, media_type)
                        and matches_owner(row, owner) and matches_record(row, query)):
                    filtered.append(row)
            rows = filtered
            rows.sort(key=lambda row: (-(row.get("created") or 0), row["request_id"]))
            data = {**record_page(rows, page, page_size), "owners": sorted(owners, key=str.casefold)}
        return schemas.Response(success=True, data=self._shared_record_display_page(data, kind))

    @staticmethod
    def _shared_record_display_page(data, kind):
        def display_title(row, torrent_title):
            if row.get("media_title"):
                return row
            # Parse only the visible page, without performing TMDB lookups while listing history.
            for value in (row.get("path"), torrent_title):
                if (isinstance(value, str) and value and (name := MetaInfo(value.rsplit("/", 1)[-1]).name)
                        and any(char.isalpha() for char in name)):
                    return {**row, "media_title": name[:120]}
            return row

        if kind == "requests":
            return {**data, "items": [{**row, "files": [display_title(item, row["title"]) for item in row["files"]]}
                                      for row in data["items"]]}
        return {**data, "items": [display_title(row, row.get("title")) for row in data["items"]]}

    def update_shared_settings(self, payload: SharedSettingsPayload) -> schemas.Response:
        try:
            with self._shared_rpc_lock, self._shared_lock:
                previous = self._shared_settings
                if payload.bot_token is None:
                    payload = payload.model_copy(update={"bot_token": previous.bot_token})
                if payload.host_coordinator and not payload.bot_token:
                    raise SharedError("运行协调端必须填写独立 Bot Token")
                unfinished = any(row["phase"] not in {"released", "completed", "rejected"}
                                 for row in self._shared_claims.values())
                route_changed = (payload.bot_username, payload.group, payload.bot_token) != (
                    previous.bot_username, previous.group, previous.bot_token)
                if unfinished and route_changed:
                    raise SharedError("仍有未完成共享申请，不能更换 Bot、Token 或共享组")
                store = self._shared_store
                restart = (route_changed or payload.host_coordinator != previous.host_coordinator
                           or payload.allowed_users != previous.allowed_users)
                if restart:
                    self._stop_shared()
                    self._shared_stopping = False
                    if (store and store.has_unfinished()
                            and (route_changed or not payload.host_coordinator)):
                        self._start_shared_worker()
                        raise SharedError("协调端仍有其他实例未完成的占位，不能更换连接或关闭协调端")
                self._shared_settings = payload
                if route_changed or payload.host_coordinator != previous.host_coordinator:
                    self._shared_probe = None
                    self._shared_policy_checked_at = 0
                    self._shared_cache_next_sync = 0
                    self._shared_cache_activity = time.time()
                    self._shared_cache_sync_failed = False
                self._shared_stopping = False
                self._shared_error = ""
                self._shared_worker_retry_at = 0
                self._save_config()
                if restart:
                    self._start_shared_worker()
                self._refresh_scheduler()
            return self.get_shared_status()
        except (SharedError, ValueError) as error:
            return schemas.Response(success=False, message=str(error))

    def test_shared_connection(self) -> schemas.Response:
        try:
            with self._shared_rpc_lock:
                response = self._shared_exchange(uuid.uuid4().hex, {"op": "policy"})
                self._apply_shared_policy(response.get("policy"))
                self._require_shared_authority(response)
                self._shared_probe = response
            return self.get_shared_status()
        except SharedError as error:
            return schemas.Response(success=False, message=str(error))

    def _check_shared_available(self):
        if self._shared_stopping:
            raise SharedError("共享审批已停止")
        retry_at = getattr(self, "_shared_retry_at", 0)
        if time.time() < retry_at:
            remaining = max(1, int(retry_at - time.time()) + 1)
            reason = getattr(self, "_shared_error", "") or "上次连接失败"
            raise SharedError(f"共享审批退避等待，约 {remaining} 秒后重试；上次原因：{reason}")

    def _shared_exchange(self, request_id, value):
        # Several site tasks can enter the same refresh cycle together. Recheck
        # the shared backoff after waiting for the transport lock so queued
        # tasks do not send more Telegram messages after the first FloodWait.
        with self._shared_transport_lock:
            self._check_shared_available()
            config = self._shared_settings
            body = {"v": 1, "group": config.group, "instance": self._shared_instance_id,
                    "label": config.instance_label, **value}
            try:
                account = ed2k_account(settings.PROXY)
                rpc = (TelegramRPC(account, config.bot_username, timeout=20) if value.get("op") == "sync_records"
                       else TelegramRPC(account, config.bot_username))
                result = rpc.exchange(request_id, body)
                if value.get("op") != "policy":
                    self._require_shared_authority(result)
                    previous = (getattr(self, "_shared_probe", None) or {}).get("authority_id")
                    if previous and previous != result["authority_id"]:
                        self._shared_probe = None
                        self._shared_policy_checked_at = 0
                        self._shared_integrity_error = "协调端资源库已变更，等待历史同步及关联核验"
                        raise SharedError(self._shared_integrity_error)
                self._shared_last_ok, self._shared_error = time.time(), ""
                if self._shared_retry_at:
                    self._shared_retry_at = 0
                    self.save_data("shared_retry_at", 0)
                    self.save_data("shared_retry_error", "")
                return result
            except SharedError as error:
                self._shared_error = str(error)
                if getattr(error, "definitive", False):
                    self._shared_policy_checked_at = 0
                elif value.get("op") != "sync_records" or getattr(error, "retry_after", 0):
                    self._shared_retry_at = time.time() + max(15, getattr(error, "retry_after", 15))
                    self.save_data("shared_retry_at", self._shared_retry_at)
                    self.save_data("shared_retry_error", self._shared_error)
                raise

    def _save_shared(self):
        self.save_data("shared_claims", self._shared_claims)

    def _shared_phase(self, entry, phase, **changes):
        if phase == "completed":
            entry.setdefault("completed_at", time.time())
        entry.update(phase=phase, updated=time.time(), **changes)
        self._save_shared()

    def _shared_participates(self, task):
        return bool(task.spec_dedup_enabled and getattr(self, "_shared_settings", SharedSettingsPayload()).enabled)

    def _shared_download_gate(self, task):
        """任务要求共享审批时，全局未启用不能静默降级为本地下载。"""
        if not task.spec_dedup_enabled:
            return True
        config = getattr(self, "_shared_settings", None)
        if config and config.enabled:
            return True
        logger.warning(
            f"共享审批门禁 [{task.name}]：任务已开启共享下载审批，但全局审批未启用或配置缺失，"
            "暂缓新增下载；不会退回本地下载"
        )
        return False

    def _shared_add_authorized(self, task, torrent_hash, selection):
        """下载器添加入口只接受本任务、本种子与精确获批文件的暂停清单。"""
        if not task.spec_dedup_enabled:
            return True
        if not self._shared_download_gate(task) or not isinstance(selection, dict):
            return False
        if selection.get("hold_paused") is not True:
            return False
        request_id = selection.get("shared_request_id")
        if not isinstance(request_id, str) or not request_id:
            return False
        entry = getattr(self, "_shared_claims", {}).get(request_id)
        if (not entry or entry.get("phase") != "adding" or not entry.get("approved")
                or entry.get("task_id") != task.id or entry.get("downloader") != task.downloader
                or entry.get("hash") != torrent_hash or entry.get("request_id") != request_id):
            return False
        approved_paths = {row["path"] for row in entry.get("files", [])
                          if row["id"] in entry["approved"]}
        return bool(approved_paths and approved_paths == set(selection.get("accepted") or []))

    def _log_shared_decision(self, entry):
        """记录实际申请、媒体身份、勾选规格及冲突下载方，避免只凭频道发帖时间猜测。"""
        rejected = {row["id"]: row for row in entry.get("rejected", [])}
        fields = entry["body"]["files"]
        for row in fields:
            conflict = rejected.get(row["id"])
            decision = "批准" if row["id"] in entry.get("approved", {}) else "排除"
            identity = (f"S{row['season']:02d}" + ",".join(f"E{ep:02d}" for ep in row["episodes"])
                        if row["media_type"] == "tv" else "电影")
            specs = ", ".join(f"{key}={value}" for key, value in sorted(row["specs"].items()))
            reason = (f"；原因 {conflict.get('reason', '')}；下载方 {conflict.get('owner', '')}"
                      f"；占位状态 {conflict.get('state', '')}" if conflict else "")
            if decision == "排除" and not conflict:
                reason = "；原因 整包审批未获准，不添加该文件"
            logger.info(
                f"共享审批文件{decision} {entry['title']}：TMDB {row['tmdb_id']} · {identity}；"
                f"匹配规格 {specs}；任务 {entry['task_id']}；下载器 {entry['downloader']}；"
                f"申请 {entry['request_id']}；Hash {entry['hash']}{reason}"
            )

    def _shared_update(self, entry, updates, absent=False):
        body = {"op": "update", "updates": updates, "confirmed_absent": bool(absent)}
        action = {"id": digest([entry["request_id"], body])[:32], "body": body}
        entry["action"] = action
        self._save_shared()
        self._flush_shared_action(entry)

    def _flush_shared_action(self, entry):
        action = entry.get("action")
        if not action:
            return
        updates = action["body"]["updates"]
        response = self._shared_exchange(action["id"], action["body"])
        if response.get("updated") != len(updates):
            raise SharedError("协调端状态回报数量不一致")
        completed = {row["claim"] for row in updates if row["state"] == "completed"}
        entry["done"] = sorted(set(entry.get("done", [])) | {
            file_id for file_id, claim in entry.get("approved", {}).items() if claim in completed})
        entry.pop("action", None)
        self._save_shared()

    def _shared_observe_torrents(self, task, torrents):
        if not getattr(self, "_shared_claims", None):
            return
        with self._shared_lock:
            for torrent in torrents:
                torrent_hash = self._ResourceFlow__get_hash(torrent)
                matching_entries = [entry for entry in self._shared_claims.values()
                                    if entry["hash"] == torrent_hash
                                    and entry["downloader"] == task.downloader
                                    and entry.get("task_id") == task.id]
                if not matching_entries:
                    continue
                kind = "qbittorrent" if isinstance(torrent, dict) else "transmission"
                try:
                    service = DownloaderHelper().get_service(name=task.downloader)
                    if not service or not service.instance:
                        continue
                    for entry in matching_entries:
                        self._remove_legacy_shared_tag(entry, task, service.instance, kind, torrent)
                    entries = [entry for entry in matching_entries
                               if entry["phase"] not in {"completed", "released", "rejected"}]
                    if not entries:
                        continue
                    files = download_files(service.instance.get_files(torrent_hash), kind) if service else None
                    if not files:
                        continue
                    for entry in entries:
                        if not self._shared_torrent_owned(entry, task, torrent, kind):
                            continue
                        observed = set(entry.get("observed_completed", []))
                        for row in entry["files"]:
                            matches = [item for item in files if paths_agree(item["path"], row["path"])]
                            if (row["id"] in entry.get("approved", {}) and len(matches) == 1
                                    and matches[0]["wanted"] and matches[0]["complete"]):
                                observed.add(row["id"])
                        entry["observed_completed"] = sorted(observed)
                    self._save_shared()
                except Exception:
                    logger.warning("共享完成证据读取未确认，保留原占位")

    def _shared_candidates(self, content, title, task, selection, spec_hint="",
                           pending_source=None, candidate_overrides=None):
        paths = selection.get("accepted") if selection else None
        rows, rejected = [], []
        display_titles = getattr(self, "_shared_media_title_cache", None)
        if not isinstance(display_titles, dict):
            display_titles = self._shared_media_title_cache = {}
        for item in parse_torrent_files(content):
            if not self._video_file(item["path"]) or paths is not None and item["path"] not in paths:
                continue
            candidate = parse_candidate_file(item["path"], item.get("size"), title, spec_hint, allow_movie=True)
            candidate = self._apply_pending_candidate_override(
                candidate, item["path"], item.get("size"), candidate_overrides
            )
            episode_group = getattr(MetaInfo(item["path"].rsplit("/", 1)[-1]), "episode_group", None)
            reason = ""
            if not candidate:
                reason = "无法解析媒体或季集身份"
            elif episode_group and not candidate.get("manual_identity"):
                reason = "剧集组需核验标准季集号"
            elif not self._verify_resource_identity(candidate, title, task):
                reason = "媒体身份未确认（TMDB ID 或名称别名未匹配）"
            else:
                missing_specs = [
                    field for field in self._channel_dedup_attributes if not candidate["specs"].get(field)
                ]
                if missing_specs:
                    reason = f"缺少勾选规格：{', '.join(missing_specs)}"
            if reason:
                rejected.append(item["path"])
                if candidate:
                    self._pending_identity(candidate, title, task, reason, pending_source)
                logger.info(f"资源刷流共享审批候选暂缓 {item['path']}：{reason}")
                continue
            candidate["id"] = hashlib.sha256(item["path"].encode()).hexdigest()[:24]
            candidate["title"] = title[:160]
            candidate["media_title"] = self._shared_media_title(candidate, display_titles)
            rows.append(candidate)
        return rows, rejected

    @staticmethod
    def _shared_active_phases():
        """Local claims that still represent an active or unresolved download."""
        return {
            "requesting", "reserved", "adding", "awaiting_start", "queued",
            "uncertain", "starting",
        }

    def _shared_local_file_is_blocking(self, entry, file_id):
        """Keep unresolved same-instance work from being sent twice."""
        phase = entry.get("phase")
        if phase == "requesting":
            return file_id in {row.get("id") for row in entry.get("files", [])}
        if phase in self._shared_active_phases():
            return file_id in entry.get("approved", {})
        return False

    def _shared_local_duplicate(self, candidate):
        """Find an existing same-instance resource with matching selected specs.

        This mirrors the coordinator's identity and episode-overlap semantics.
        It is only a local optimization; every non-hit still goes through the
        coordinator for the cross-device atomic decision.
        """
        attributes = getattr(self, "_channel_dedup_attributes", []) or []
        tolerance = getattr(self, "_channel_dedup_size_tolerance_pct", 5)
        for entry in getattr(self, "_shared_claims", {}).values():
            if entry.get("phase") not in self._shared_active_phases():
                continue
            for row in entry.get("files", []) or []:
                if (self._shared_local_file_is_blocking(entry, row.get("id"))
                        and same_resource(candidate, row, attributes, tolerance)):
                    return entry, row
        return None

    def _shared_filter_local_duplicates(self, candidates, title):
        """Remove same-instance duplicates before creating a Telegram RFQ."""
        available, blocked = [], []
        for candidate in candidates:
            hit = self._shared_local_duplicate(candidate)
            if hit:
                blocked.append((candidate, hit))
            else:
                available.append(candidate)
        for candidate, (entry, _previous) in blocked:
            if candidate.get("media_type") == "tv":
                identity = (
                    f"TMDB {candidate.get('tmdb_id')} · "
                    f"S{candidate.get('season'):02d}E"
                    + ",".join(f"{episode:02d}" for episode in candidate.get("episodes") or [])
                )
            else:
                identity = f"TMDB {candidate.get('tmdb_id')} · 电影"
            specs = ", ".join(
                f"{key}={candidate.get('specs', {}).get(key)}"
                for key in getattr(self, "_channel_dedup_attributes", [])
            )
            logger.info(
                f"共享审批本地缓存命中 {title}：{identity}；规格 {specs}；"
                f"复用申请 {entry.get('request_id')}，不再发送重复审批"
            )
        return available, blocked

    def _shared_media_title(self, candidate, cache):
        tmdb_id = candidate.get("tmdb_id")
        if not tmdb_id:
            return ""
        cache_key = (candidate.get("media_type"), int(tmdb_id))
        if cache_key in cache:
            return cache[cache_key]
        path = candidate.get("path") or ""
        meta = MetaInfo(path.rsplit("/", 1)[-1])
        media_title = ""
        try:
            media = self.chain.recognize_media(meta=meta)
            recognized_id = getattr(media, "tmdb_id", None)
            if recognized_id and int(recognized_id) != int(tmdb_id):
                return ""
            names = [
                getattr(media, field, "")
                for field in ("title", "cn_name", "hk_title", "tw_title", "sg_title")
            ]
            names.extend(getattr(media, "names", []) or [])
            media_title = next(
                (str(name).strip() for name in names
                 if isinstance(name, str) and re.search(r"[\u3400-\u9fff]", name)),
                "",
            )
        except Exception:
            pass
        if not media_title:
            try:
                media_type = {
                    "tv": schemas.MediaType.TV,
                    "movie": schemas.MediaType.MOVIE,
                }.get(candidate.get("media_type"))
                localized = TmdbApi(language="zh-CN").get_info(
                    mtype=media_type, tmdbid=int(tmdb_id)
                ) if media_type else None
                localized_names = [
                    localized.get("name") or localized.get("title") if isinstance(localized, dict) else "",
                ]
                if isinstance(localized, dict):
                    localized_names.extend(localized.get("names") or [])
                media_title = next(
                    (str(name).strip() for name in localized_names
                     if isinstance(name, str) and re.search(r"[\u3400-\u9fff]", name)),
                    "",
                )
            except Exception:
                pass
        if not media_title:
            media_title = next(
                (str(getattr(meta, field, "") or "").strip()
                 for field in ("cn_name", "name")
                 if re.search(r"[\u3400-\u9fff]", str(getattr(meta, field, "") or ""))),
                "",
            )
        if media_title:
            if len(cache) >= 1000:
                cache.pop(next(iter(cache)))
            cache[cache_key] = media_title[:120]
        return media_title[:120]

    def _resource_display_titles(self):
        """Reuse verified local titles; listing records must not trigger recognition/network calls."""
        titles = {}
        store_titles = getattr(getattr(self, "_shared_store", None), "media_titles", None)
        if callable(store_titles):
            try:
                titles.update(store_titles())
            except Exception:
                pass
        elif (getattr(self, "_shared_settings", None) and self._shared_settings.enabled
              and (probe := self._shared_cache_probe())):
            try:
                titles.update(self._shared_cache.media_titles(self._shared_cache_scope(probe)))
            except (sqlite3.Error, KeyError, TypeError, ValueError):
                pass
        titles.update(getattr(self, "_shared_media_title_cache", {}) or {})
        lock = getattr(self, "_shared_lock", None)
        if lock:
            with lock:
                files = [dict(item) for row in getattr(self, "_shared_claims", {}).values()
                         for item in row.get("files") or []]
        else:
            files = []
        for row in files + list((getattr(self, "_channel_dedup_state", {}) or {}).get("records") or []):
            title = row.get("media_title")
            if not title and row.get("channel"):
                title = row.get("title")
            key = self._resource_title_key(row)
            if key and isinstance(title, str) and re.search(r"[\u3400-\u9fff]", title):
                titles[key] = title.strip()[:120]
        return titles

    @staticmethod
    def _resource_title_key(row):
        return record_title_key(row)

    def _resource_record_title(self, row, titles):
        title = row.get("media_title")
        if isinstance(title, str) and re.search(r"[\u3400-\u9fff]", title):
            return title.strip()[:120]
        title = titles.get(self._resource_title_key(row))
        if isinstance(title, str) and re.search(r"[\u3400-\u9fff]", title):
            return title.strip()[:120]
        # Pending identities may only have a Chinese filename. This is a display hint, not a binding.
        for value in (row.get("path"), row.get("title")):
            if not isinstance(value, str) or not re.search(r"[\u3400-\u9fff]", value):
                continue
            name = getattr(MetaInfo(value.rsplit("/", 1)[-1]), "cn_name", "")
            if isinstance(name, str) and re.search(r"[\u3400-\u9fff]", name):
                return name.strip()[:120]
        return ""

    def _add_prechecked_torrent(self, content, cookies, task, downloader, service, expected_hash, selection, title,
                               spec_hint="", pending_source=None, candidate_overrides=None):
        if not task.spec_dedup_enabled:
            logger.info(
                f"共享审批旁路 [{task.name}] {title}：任务开关关闭，仅执行本地规则，"
                f"不参与跨设备去重；下载器 {task.downloader}；Hash {expected_hash}"
            )
            return self._add_local_prechecked_torrent(
                content, cookies, task, downloader, service, expected_hash, selection, title)
        if not self._shared_download_gate(task):
            return None
        with self._shared_rpc_lock, self._shared_lock:
            try:
                if not self._shared_download_gate(task):
                    return None
                self._check_shared_available()
                self._sync_shared_history(store=self._shared_store if self._shared_settings.host_coordinator else None)
                if getattr(self, "_shared_integrity_error", ""):
                    self._audit_shared_links(force=True)
                if not isinstance(content, bytes) or not self._channel_dedup_attributes:
                    raise SharedError("共享审批需要可核验的文件清单与至少一个规格字段")
                if any(row["hash"] == expected_hash and row["downloader"] == task.downloader
                       and row["phase"] not in {"released", "rejected"}
                       for row in self._shared_claims.values()):
                    raise SharedError("本种子已有共享申请，等待核验，不重复提交")
                candidates, rejected = self._shared_candidates(
                    content, title, task, selection, spec_hint, pending_source, candidate_overrides
                )
                candidates, local_blocked = self._shared_filter_local_duplicates(candidates, title)
                candidates, shared_blocked = self._filter_shared_record_cache(candidates, title)
                blocked_count = len(local_blocked) + len(shared_blocked)
                if blocked_count and self._channel_dedup_pack_policy == "exclude":
                    logger.info(
                        f"共享审批本地缓存排除整包 {title}：命中 {blocked_count} 个重复文件"
                    )
                    return None
                if blocked_count and not candidates:
                    logger.info(f"共享审批本地缓存跳过 {title}：候选均已命中共享下载记录或本机在途申请")
                    return None
                if not candidates or rejected and self._channel_dedup_pack_policy == "exclude":
                    raise SharedError("共享审批候选身份或规格不完整，本次不添加下载")
                current_policy = self._local_shared_policy()
                request_id = uuid.uuid4().hex
                body = {"op": "reserve", "revision": current_policy["revision"],
                        "pack_policy": self._channel_dedup_pack_policy,
                        "files": [{**{key: row.get(key) for key in
                                   ("id", "title", "media_type", "tmdb_id", "media_title",
                                    "season", "episodes")},
                                   "specs": {
                                       **{key: row["specs"].get(key) for key in current_policy["attributes"]},
                                       "resolution": row["specs"].get("resolution"),
                                   }}
                                  for row in candidates]}
                entry = {"request_id": request_id, "body": body, "title": title[:160],
                         "task_id": task.id, "downloader": task.downloader, "hash": expected_hash,
                         "files": candidates, "phase": "requesting", "updated": time.time(),
                         "created": time.time(), "attempted_add": False}
                self._shared_claims[request_id] = entry
                self._save_shared()
                reply = self._shared_exchange(request_id, body)
                self._accept_shared_reply(entry, reply)
                if not entry["approved"]:
                    self._shared_phase(entry, "rejected")
                    logger.info(f"共享审批排除 {title}：{len(entry.get('rejected') or [])} 个重复文件")
                    return None
                self._shared_phase(entry, "reserved")
                if not self._shared_task_ready(task):
                    raise SharedError("任务或共享设置已变更，暂不添加下载")
                entry["attempted_add"] = True
                entry["task_tag"] = task.brush_tag
                self._shared_phase(entry, "adding")
                paths = {row["path"] for row in entry["files"] if row["id"] in entry["approved"]}
                with self._inflight_lock:
                    local = self._inflight_entries.get(self._inflight_key(task.downloader, expected_hash))
                    if local:
                        local["records"] = [row for row in local.get("records", []) if row.get("path") in paths]
                        self._save_inflight_entries()
                result = self._add_local_prechecked_torrent(
                    content, cookies, task, downloader, service, expected_hash,
                    selection := {"accepted": paths, "hold_paused": True,
                                  "shared_request_id": request_id}, title)
                if not result:
                    self._shared_phase(entry, "uncertain", error="添加或选集未确认，保留占位等待核验")
                    return None
                entry["selection_pending"] = bool(selection.get("selection_pending"))
                self._shared_phase(entry, "awaiting_start")
                self._start_shared_download(entry, task, downloader)
                logger.info(f"共享审批通过 {title}：批准 {len(paths)} 个文件，下载器已确认")
                return result
            except SharedError as error:
                if "entry" in locals():
                    if getattr(error, "definitive", False) and entry["phase"] == "requesting":
                        entry["phase"] = "rejected"
                    self._shared_phase(entry, entry["phase"], error=str(error))
                logger.warning(f"资源刷流共享审批暂缓 {title}：{error}")
                if "result" in locals() and result:
                    # The paused task exists: let the caller register its ownership and lifecycle.
                    return result
                return None
            except Exception:
                if "entry" in locals():
                    self._shared_phase(entry, "uncertain", error="操作异常，保留占位等待核验")
                logger.warning(f"资源刷流共享审批暂缓 {title}：操作未确认，保留占位")
                return result if "result" in locals() and result else None

    def _accept_shared_reply(self, entry, reply):
        approved = reply.get("approved")
        if not isinstance(approved, list):
            raise SharedError("协调端批准清单无效")
        known = {row["id"] for row in entry["files"]}
        pairs = {}
        for row in approved:
            if (not isinstance(row, dict) or row.get("id") not in known or row["id"] in pairs
                    or not isinstance(row.get("claim"), str) or not re.fullmatch(r"[a-f0-9]{32}", row["claim"])):
                raise SharedError("协调端批准文件与申请不一致")
            pairs[row["id"]] = row["claim"]
        rejected = reply.get("rejected")
        if not isinstance(rejected, list) or any(not isinstance(row, dict) or row.get("id") not in known
                                                  for row in rejected):
            raise SharedError("协调端排除清单无效")
        rejected_ids = [row["id"] for row in rejected]
        pack_rejected = reply.get("pack_rejected")
        if (len(set(pairs.values())) != len(pairs) or len(set(rejected_ids)) != len(rejected_ids)
                or set(pairs) & set(rejected_ids)
                or (pack_rejected is not True and set(pairs) | set(rejected_ids) != known)
                or (pack_rejected is True and (pairs or entry["body"]["pack_policy"] != "exclude"))):
            raise SharedError("协调端文件清单不完整或整包策略不一致")
        if reply.get("policy", {}).get("revision") != entry["body"]["revision"]:
            raise SharedError("协调端回复规则版本不一致")
        entry["approved"] = pairs
        entry["rejected"] = rejected
        entry["done"] = []
        entry["error"] = ""
        self._save_shared()
        self._log_shared_decision(entry)

    def _shared_task_ready(self, task, existing_hash=None):
        if not (not self._shared_stopping and self._enabled and task.enabled and task.spec_dedup_enabled
                and self._shared_settings.enabled and self._task_configs.get(task.id) is task):
            return False
        with self._task_scope(task.id):
            passed, _ = self._ResourceFlow__evaluate_pre_conditions_for_brush(exclude_hash=existing_hash)
            return passed and self._is_current_time_in_range(task)

    @staticmethod
    def _shared_tag(entry):
        """Legacy visible marker used only to clean up tasks from older versions."""
        return f"resourceflow-claim-{entry['request_id']}"

    @staticmethod
    def _torrent_tags(torrent, kind):
        if kind == "qbittorrent":
            raw = torrent.get("tags") or ""
            return {str(tag).strip() for tag in raw.split(",") if str(tag).strip()}
        return {str(tag).strip() for tag in (getattr(torrent, "labels", None) or []) if str(tag).strip()}

    def _shared_torrent_owned(self, entry, task, torrent, kind):
        tags = self._torrent_tags(torrent, kind)
        if self._shared_tag(entry) in tags:
            return True
        return self._shared_task_tag_matches(entry, task, tags)

    @staticmethod
    def _shared_task_tag_matches(entry, task, tags):
        return bool(entry.get("attempted_add") and (
            entry.get("task_tag") in tags or (
                task and task.id == entry.get("task_id") and task.downloader == entry.get("downloader")
                and task.brush_tag in tags)))

    def _remove_legacy_shared_tag(self, entry, task, downloader, kind, torrent):
        """Remove the old visible marker after the normal task label confirms ownership."""
        legacy_tag = self._shared_tag(entry)
        current_tags = self._torrent_tags(torrent, kind)
        if legacy_tag not in current_tags or not task:
            return
        if (entry["phase"] not in {"completed", "released", "rejected"}
                and not self._shared_task_tag_matches(entry, task, current_tags)):
            # An in-flight legacy task may still rely on this marker when its task tag has changed.
            return
        try:
            if kind == "qbittorrent":
                removed = downloader.remove_torrents_tag(ids=entry["hash"], tag=legacy_tag)
            else:
                current = list(downloader.get_torrent_tags(ids=entry["hash"]) or [])
                remaining = [tag for tag in current if tag != legacy_tag]
                if remaining and legacy_tag in current:
                    removed = downloader.set_torrent_tag(ids=entry["hash"], tags=remaining, org_tags=[])
                else:
                    removed = legacy_tag not in current
            if removed is True:
                logger.info(f"清理旧共享归属标签 {entry.get('title', '')}：{legacy_tag}")
        except Exception as error:
            logger.warning(f"清理旧共享归属标签失败 {entry.get('title', '')}：{error}")

    def _start_shared_download(self, entry, task, downloader):
        if not entry.get("approved") or entry.get("phase") not in {"awaiting_start", "queued"}:
            raise SharedError("共享申请未处于可启动阶段，禁止启动下载")
        if entry.get("selection_pending"):
            raise SharedError("文件清单暂不可用，等待重试选集后再启动")
        if not self._shared_task_ready(task, entry["hash"]):
            raise SharedError("任务已停用或关闭共享去重，保持暂停")
        service = DownloaderHelper().get_service(name=entry["downloader"])
        if not service or not service.instance:
            raise SharedError("下载器不可用，保持暂停")
        kind = "qbittorrent" if DownloaderHelper().is_downloader("qbittorrent", service=service) else "transmission"
        snapshot = download_files(downloader.get_files(entry["hash"]), kind)
        approved = [row for row in entry["files"] if row["id"] in entry["approved"]]
        if not snapshot or any(item["wanted"] and not any(paths_agree(item["path"], row["path"])
                                                         for row in approved) for item in snapshot):
            raise SharedError("实际选集尚未确认或存在未经批准的文件，保持暂停")
        for row in approved:
            matches = [item for item in snapshot if paths_agree(item["path"], row["path"])]
            if len(matches) != 1 or not matches[0]["wanted"]:
                raise SharedError("实际选集与批准清单不一致，保持暂停")
        self._shared_update(entry, [{"claim": claim, "state": "queued"}
                                    for file_id, claim in entry["approved"].items()
                                    if file_id not in entry.get("done", [])])
        if not self._shared_task_ready(task, entry["hash"]):
            raise SharedError("确认期间设置已变更，保持暂停")
        entry.setdefault("start_attempt_at", time.time())
        self._save_shared()
        if not downloader.start_torrents(entry["hash"]):
            raise SharedError("下载器启动未确认，保留占位")
        entry.setdefault("download_started_at", entry.get("start_attempt_at") or time.time())
        entry.pop("start_attempt_at", None)
        self._shared_phase(entry, "queued", error="")
        logger.info(
            f"共享审批启动确认 [{task.name}] {entry['title']}：申请 {entry['request_id']}；"
            f"下载器 {entry['downloader']}；Hash {entry['hash']}"
        )

    def reconcile_shared_claims(self):
        if self._shared_stopping or not self._shared_rpc_lock.acquire(blocking=False):
            return
        try:
            with self._shared_lock:
                if self._shared_settings.enabled:
                    try:
                        response = getattr(self, "_shared_probe", None)
                        if not response or time.time() - getattr(self, "_shared_policy_checked_at", 0) >= 600:
                            response = self._shared_exchange(uuid.uuid4().hex, {"op": "policy"})
                            self._shared_policy_checked_at = time.time()
                        self._apply_shared_policy(response.get("policy"))
                        self._require_shared_authority(response)
                        self._shared_probe = response
                        self._sync_shared_history(store=self._shared_store
                                                  if self._shared_settings.host_coordinator else None)
                        self._audit_shared_links()
                    except SharedError as error:
                        self._shared_error = str(error)
                        return
                pending = [row for row in self._shared_claims.values()
                           if row["phase"] not in {"released", "completed", "rejected"}
                           and time.time() - row.get("checked_at", 0) >= 30]
                pending.sort(key=lambda row: row.get("checked_at", 0))
                for entry in pending[:10]:
                    entry["checked_at"] = time.time()
                    try:
                        self._reconcile_shared_entry(entry)
                    except SharedError as error:
                        if getattr(error, "definitive", False) and entry["phase"] == "requesting":
                            entry["phase"] = "rejected"
                        self._shared_phase(entry, entry["phase"], error=str(error))
                    except Exception:
                        self._shared_phase(entry, entry["phase"], error="下载器或共享状态待核验")
                if self._shared_settings.enabled:
                    self._sync_shared_record_cache()
        finally:
            self._shared_rpc_lock.release()

    def _reconcile_shared_entry(self, entry):
        self._flush_shared_action(entry)
        if not entry.get("approved"):
            response = self._shared_exchange(entry["request_id"], entry["body"])
            self._accept_shared_reply(entry, response)
            if not entry["approved"]:
                self._shared_phase(entry, "rejected", error="")
                return
        observed = [file_id for file_id in entry.get("observed_completed", [])
                    if file_id in entry["approved"] and file_id not in entry.get("done", [])]
        if observed:
            self._shared_update(entry, [{"claim": entry["approved"][file_id], "state": "completed"}
                                       for file_id in observed])
        if len(entry.get("done", [])) == len(entry["approved"]):
            self._shared_phase(entry, "completed", error="")
            return
        service = DownloaderHelper().get_service(name=entry["downloader"])
        if not service or not service.instance:
            raise SharedError("下载器不可用，保留共享占位")
        downloader = service.instance
        torrents, error = downloader.get_torrents(ids=[entry["hash"]])
        if error:
            raise SharedError("下载器查询失败，保留共享占位")
        if not torrents:
            if entry.get("timeout_cancel_pending"):
                self._release_timed_out_claims(entry)
                return
            if entry.get("attempted_add") and time.time() - entry["created"] < 300:
                return
            entry["absent_checks"] = entry.get("absent_checks", 0) + 1
            if entry.get("attempted_add") and entry["absent_checks"] < 2:
                self._save_shared()
                return
            remaining = [claim for file_id, claim in entry["approved"].items() if file_id not in entry.get("done", [])]
            if remaining:
                self._shared_update(entry, [{"claim": claim, "state": "released"} for claim in remaining], absent=True)
            self._shared_phase(entry, "released", error="")
            return
        entry["absent_checks"] = 0
        kind = "qbittorrent" if DownloaderHelper().is_downloader("qbittorrent", service=service) else "transmission"
        if len(torrents) != 1:
            raise SharedError("下载器任务不唯一，保留占位等待核验")
        task = self._task_configs.get(entry["task_id"])
        if not self._shared_torrent_owned(entry, task, torrents[0], kind):
            raise SharedError("下载器任务缺少本任务标签，无法确认共享申请归属；保留占位等待核验")
        self._remove_legacy_shared_tag(entry, task, downloader, kind, torrents[0])
        approved = [row for row in entry["files"] if row["id"] in entry["approved"]]
        if entry.get("timeout_cancel_pending"):
            self._cancel_timed_out_download(entry, downloader)
            return
        if entry["phase"] in {"adding", "uncertain", "reserved", "requesting"} or entry.get("selection_pending"):
            if not task or not self._shared_task_ready(task, entry["hash"]):
                raise SharedError("原任务不可运行，保留暂停任务等待核验")
            if not self._select_recent_episode_files(downloader, entry["hash"],
                    {"accepted": {row["path"] for row in approved}, "hold_paused": True}, kind, task.name):
                raise SharedError("重启后选集未确认，保持暂停")
            entry["selection_pending"] = False
            self._shared_phase(entry, "awaiting_start")
        snapshot = download_files(downloader.get_files(entry["hash"]), kind)
        if not snapshot:
            raise SharedError("无法核验实际文件选择，保留共享占位")
        extra = [item for item in snapshot if item["wanted"]
                 and not any(paths_agree(item["path"], row["path"]) for row in approved)]
        if extra:
            raise SharedError("存在未经批准的已选文件，保留占位等待核验")
        completed = []
        for row in approved:
            matches = [item for item in snapshot if paths_agree(item["path"], row["path"])]
            if len(matches) != 1 or not matches[0]["wanted"]:
                raise SharedError("实际选集与批准清单不一致，需人工核验")
            if matches[0]["complete"] and row["id"] not in entry.get("done", []):
                completed.append(row["id"])
        if completed:
            self._shared_update(entry, [{"claim": entry["approved"][file_id], "state": "completed"}
                                        for file_id in completed])
            entry["done"] = list(set(entry.get("done", [])) | set(completed))
            self._save_shared()
        if len(entry.get("done", [])) == len(approved):
            self._shared_phase(entry, "completed", error="")
        else:
            if entry["phase"] == "queued" and not entry.get("download_started_at"):
                # Existing queued claims get a fresh, durable observation baseline after upgrade.
                entry["download_started_at"] = time.time()
                self._save_shared()
            if entry.get("download_started_at") and time.time() - entry["download_started_at"] >= 2 * 60 * 60:
                entry["timeout_cancel_pending"] = True
                entry["timeout_at"] = time.time()
                entry["timeout_reason"] = "下载超过 2 小时，按获批文件判断后取消未完成部分"
                self._shared_phase(entry, "timeout_cancel_pending",
                                   error="超时取消进行中；确认下载器任务停止前不会释放占位")
                self._cancel_timed_out_download(entry, downloader)
            elif entry["phase"] == "awaiting_start":
                task = self._task_configs.get(entry["task_id"])
                if not task:
                    raise SharedError("原任务已不存在，保持暂停等待核验")
                self._start_shared_download(entry, task, downloader)
            else:
                self._shared_phase(entry, "queued", error="")

    def _cancel_timed_out_download(self, entry, downloader):
        """Stop and remove the owned torrent without data before releasing unfinished claims."""
        if not downloader.stop_torrents(entry["hash"]):
            raise SharedError("下载超过 2 小时，但下载器未确认停止；继续保留占位")
        if not downloader.delete_torrents(ids=entry["hash"], delete_file=False):
            raise SharedError("下载超过 2 小时，但下载器未确认移除任务；继续保留占位")
        torrents, error = downloader.get_torrents(ids=[entry["hash"]])
        if error or torrents:
            raise SharedError("超时任务移除状态无法确认；继续保留占位")
        self._release_timed_out_claims(entry)

    def _release_timed_out_claims(self, entry):
        remaining = [claim for file_id, claim in entry.get("approved", {}).items()
                     if file_id not in entry.get("done", [])]
        if remaining:
            self._shared_update(entry, [{"claim": claim, "state": "released"} for claim in remaining],
                                absent=True)
        self._shared_phase(entry, "released",
                           error="超过 2 小时仍有获批集未完成；下载任务已移除但数据保留，未完成集占位已释放")
