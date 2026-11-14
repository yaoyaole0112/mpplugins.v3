"""Download admission and conservative recovery for cross-instance Telegram arbitration."""

import copy
import hashlib
from pathlib import Path
import re
import threading
import time
import uuid

from app import schemas
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.media import MetaInfo
from app.sdk.services import DownloaderHelper

from .active_dedup import download_files, paths_agree
from .channel_dedup import parse_candidate_file, parse_torrent_files
from .models import ChannelDedupSettingsPayload
from .shared_coordinator import BotWorker, CoordinatorStore
from .shared_models import SharedSettingsPayload
from .shared_protocol import SharedError, digest, policy
from .telegram_rpc import TelegramRPC, ed2k_account


class SharedResourceMixin:
    def update_channel_dedup_settings(self, payload: ChannelDedupSettingsPayload) -> schemas.Response:
        with getattr(self, "_shared_rpc_lock", threading.RLock()):
            store = getattr(self, "_shared_store", None)
            changed = (set(payload.attributes) != set(self._channel_dedup_attributes)
                       or payload.size_tolerance_pct != self._channel_dedup_size_tolerance_pct)
            try:
                if store and changed:
                    self._stop_shared()
                    self._shared_stopping = False
                    if store.has_unfinished():
                        return schemas.Response(success=False, message="协调端仍有未完成共享占位，暂不变更匹配字段或容差")
                return self._update_resource_dedup_settings(payload)
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
        self._shared_error = ""
        self._shared_last_ok = None
        raw = dict(config.get("shared") or {})
        instance_id = raw.pop("instance_id", None)
        self._shared_instance_id = instance_id if isinstance(instance_id, str) and len(instance_id) == 32 else uuid.uuid4().hex
        self._shared_settings = SharedSettingsPayload.model_validate(raw)
        self._shared_claims = self.get_data("shared_claims") or {}
        self._shared_retry_at = float(self.get_data("shared_retry_at") or 0)
        self._shared_probe = None
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
            return
        try:
            route = digest([config.bot_username, config.group])[:16]
            self._shared_store = CoordinatorStore(
                Path(self.get_data_path()) / f"coordinator-{route}.sqlite3", config.group,
                self._channel_dedup_attributes, self._channel_dedup_size_tolerance_pct,
            )
            self._shared_worker = BotWorker(
                self._shared_store, config.bot_token, config.bot_username, config.allowed_users,
                proxies=settings.PROXY,
            )
            self._shared_worker.start()
        except Exception:
            self._shared_error = "协调服务启动失败，请检查配置与数据库目录"

    def _stop_shared(self):
        self._shared_stopping = True
        worker = getattr(self, "_shared_worker", None)
        if worker:
            worker.stop()
            self._shared_worker = None
        self._shared_store = None

    def get_shared_status(self) -> schemas.Response:
        config = self._shared_config()
        token = config.pop("bot_token", None)
        worker = getattr(self, "_shared_worker", None)
        claims = list(getattr(self, "_shared_claims", {}).values())
        return schemas.Response(success=True, data={
            **config, "token_configured": bool(token),
            "coordinator_running": bool(worker and worker.thread and worker.thread.is_alive()),
            "last_error": self._shared_error or (worker.last_error if worker else ""),
            "last_ok": self._shared_last_ok, "coordinator_last_ok": worker.last_ok if worker else None,
            "retry_at": self._shared_retry_at,
            "policy": policy(self._channel_dedup_attributes, self._channel_dedup_size_tolerance_pct)
            if self._channel_dedup_attributes else None,
            "requests": [{"request_id": row["request_id"], "title": row["title"], "phase": row["phase"],
                          "task_id": row["task_id"], "updated": row.get("updated"), "error": row.get("error", ""),
                          "approved": len(row.get("approved") or []), "rejected": row.get("rejected") or []}
                         for row in sorted(claims, key=lambda row: row.get("updated", 0), reverse=True)[:100]],
            "records": self._shared_store.recent() if self._shared_store else [],
            "probe": self._shared_probe,
        })

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
                self._stop_shared()
                self._shared_stopping = False
                if (store and store.has_unfinished()
                        and (route_changed or not payload.host_coordinator)):
                    self._start_shared_worker()
                    raise SharedError("协调端仍有其他实例未完成的占位，不能更换连接或关闭协调端")
                self._shared_settings = payload
                self._shared_stopping = False
                self._shared_error = ""
                self._save_config()
                self._start_shared_worker()
                self._refresh_scheduler()
            return self.get_shared_status()
        except (SharedError, ValueError) as error:
            return schemas.Response(success=False, message=str(error))

    def test_shared_connection(self) -> schemas.Response:
        try:
            with self._shared_rpc_lock:
                response = self._shared_exchange(uuid.uuid4().hex, {"op": "policy"})
                self._shared_probe = response
            return self.get_shared_status()
        except SharedError as error:
            return schemas.Response(success=False, message=str(error))

    def _shared_exchange(self, request_id, value):
        if self._shared_stopping or time.time() < self._shared_retry_at:
            raise SharedError("共享审批已停止或正在退避等待")
        config = self._shared_settings
        body = {"v": 1, "group": config.group, "instance": self._shared_instance_id,
                "label": config.instance_label, **value}
        try:
            account = ed2k_account(settings.PROXY)
            result = TelegramRPC(account, config.bot_username).exchange(request_id, body)
            self._shared_last_ok, self._shared_error = time.time(), ""
            return result
        except SharedError as error:
            self._shared_error = str(error)
            self._shared_retry_at = time.time() + max(15, getattr(error, "retry_after", 15))
            self.save_data("shared_retry_at", self._shared_retry_at)
            raise

    def _save_shared(self):
        self.save_data("shared_claims", self._shared_claims)

    def _shared_phase(self, entry, phase, **changes):
        entry.update(phase=phase, updated=time.time(), **changes)
        self._save_shared()

    def _shared_participates(self, task):
        return bool(task.spec_dedup_enabled and getattr(self, "_shared_settings", SharedSettingsPayload()).enabled)

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
                entries = [entry for entry in self._shared_claims.values()
                           if entry["hash"] == torrent_hash and entry["downloader"] == task.downloader
                           and entry["phase"] not in {"completed", "released", "rejected"}]
                if not entries:
                    continue
                kind = "qbittorrent" if isinstance(torrent, dict) else "transmission"
                tags = (str(torrent.get("tags") or "").split(",") if kind == "qbittorrent"
                        else list(getattr(torrent, "labels", None) or []))
                try:
                    service = DownloaderHelper().get_service(name=task.downloader)
                    files = download_files(service.instance.get_files(torrent_hash), kind) if service else None
                    if not files:
                        continue
                    for entry in entries:
                        if self._shared_tag(entry) not in {str(tag).strip() for tag in tags}:
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

    def _shared_candidates(self, content, title, task, selection, spec_hint=""):
        paths = selection.get("accepted") if selection else None
        rows, rejected = [], []
        for item in parse_torrent_files(content):
            if not self._video_file(item["path"]) or paths is not None and item["path"] not in paths:
                continue
            candidate = parse_candidate_file(item["path"], item.get("size"), title, spec_hint, allow_movie=True)
            episode_group = getattr(MetaInfo(item["path"].rsplit("/", 1)[-1]), "episode_group", None)
            if (not candidate or episode_group or not self._verify_resource_identity(candidate, title, task)
                    or any(not candidate["specs"].get(field) for field in self._channel_dedup_attributes)):
                rejected.append(item["path"])
                if candidate:
                    self._pending_identity(candidate, title, task, "共享审批缺少核验身份或勾选规格")
                continue
            candidate["id"] = hashlib.sha256(item["path"].encode()).hexdigest()[:24]
            candidate["title"] = title[:160]
            rows.append(candidate)
        return rows, rejected

    def _add_prechecked_torrent(self, content, cookies, task, downloader, service, expected_hash, selection, title,
                               spec_hint=""):
        if not self._shared_participates(task):
            return self._add_local_prechecked_torrent(
                content, cookies, task, downloader, service, expected_hash, selection, title)
        with self._shared_rpc_lock, self._shared_lock:
            try:
                if not isinstance(content, bytes) or not self._channel_dedup_attributes:
                    raise SharedError("共享审批需要可核验的文件清单与至少一个规格字段")
                if any(row["hash"] == expected_hash and row["downloader"] == task.downloader
                       and row["phase"] not in {"released", "rejected"}
                       for row in self._shared_claims.values()):
                    raise SharedError("本种子已有共享申请，等待核验，不重复提交")
                candidates, rejected = self._shared_candidates(content, title, task, selection, spec_hint)
                if not candidates or rejected and self._channel_dedup_pack_policy == "exclude":
                    raise SharedError("共享审批候选身份或规格不完整，本次不添加下载")
                current_policy = policy(self._channel_dedup_attributes, self._channel_dedup_size_tolerance_pct)
                request_id = uuid.uuid4().hex
                body = {"op": "reserve", "revision": current_policy["revision"],
                        "pack_policy": self._channel_dedup_pack_policy,
                        "files": [{key: row.get(key) for key in
                                   ("id", "title", "media_type", "tmdb_id", "season", "episodes", "specs")}
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
                self._shared_phase(entry, "adding")
                paths = {row["path"] for row in entry["files"] if row["id"] in entry["approved"]}
                with self._inflight_lock:
                    local = self._inflight_entries.get(self._inflight_key(task.downloader, expected_hash))
                    if local:
                        local["records"] = [row for row in local.get("records", []) if row.get("path") in paths]
                        self._save_inflight_entries()
                result = self._add_local_prechecked_torrent(
                    content, cookies, task, downloader, service, expected_hash,
                    {"accepted": paths, "hold_paused": True, "shared_tag": self._shared_tag(entry)}, title)
                if not result:
                    self._shared_phase(entry, "uncertain", error="添加或选集未确认，保留占位等待核验")
                    return None
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
        self._save_shared()

    def _shared_task_ready(self, task, existing_hash=None):
        if not (not self._shared_stopping and self._enabled and task.enabled and task.spec_dedup_enabled
                and self._shared_settings.enabled and self._task_configs.get(task.id) is task):
            return False
        with self._task_scope(task.id):
            passed, _ = self._ResourceFlow__evaluate_pre_conditions_for_brush(exclude_hash=existing_hash)
            return passed and self._is_current_time_in_range(task)

    @staticmethod
    def _shared_tag(entry):
        return f"resourceflow-claim-{entry['request_id']}"

    def _start_shared_download(self, entry, task, downloader):
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
        if not downloader.start_torrents(entry["hash"]):
            raise SharedError("下载器启动未确认，保留占位")
        self._shared_phase(entry, "queued", error="")

    def reconcile_shared_claims(self):
        if self._shared_stopping or not self._shared_rpc_lock.acquire(blocking=False):
            return
        try:
            with self._shared_lock:
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
        finally:
            self._shared_rpc_lock.release()

    def _reconcile_shared_entry(self, entry):
        self._flush_shared_action(entry)
        if not entry.get("approved"):
            response = self._shared_exchange(entry["request_id"], entry["body"])
            self._accept_shared_reply(entry, response)
            if not entry["approved"]:
                self._shared_phase(entry, "rejected")
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
        tags = (str(torrents[0].get("tags") or "").split(",") if kind == "qbittorrent"
                else list(getattr(torrents[0], "labels", None) or []))
        if self._shared_tag(entry) not in {str(tag).strip() for tag in tags}:
            raise SharedError("任务缺少本申请归属标签，不自动操作或释放占位")
        approved = [row for row in entry["files"] if row["id"] in entry["approved"]]
        if entry["phase"] in {"adding", "uncertain", "reserved", "requesting"}:
            task = self._task_configs.get(entry["task_id"])
            if not task or not self._shared_task_ready(task, entry["hash"]):
                raise SharedError("原任务不可运行，保留暂停任务等待核验")
            if not self._select_recent_episode_files(downloader, entry["hash"],
                    {"accepted": {row["path"] for row in approved}, "hold_paused": True}, kind, task.name):
                raise SharedError("重启后选集未确认，保持暂停")
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
        elif entry["phase"] == "awaiting_start":
            task = self._task_configs.get(entry["task_id"])
            if not task:
                raise SharedError("原任务已不存在，保持暂停等待核验")
            self._start_shared_download(entry, task, downloader)
        else:
            self._shared_phase(entry, "queued", error="")
