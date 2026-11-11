import hashlib
import json
import re
import threading
import time
from pathlib import Path, PurePosixPath

from app.log import logger

from .cloud_storage import CloudError, CloudStorage, cloud_path, monitor_roots
from .upload_adapter import UploadNotStarted, upload_with_digest


UPLOAD_FIELDS = ("local_upload_enabled", "local_upload_mode", "local_upload_folder", "local_upload_target", "local_upload_rate")


def plain_item(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value) if isinstance(value, dict) else {}


def saved_item(value):
    item = plain_item(value)
    return {name: item.get(name) for name in (
        "storage", "fileid", "type", "path", "name", "size", "modify_time", "pickcode")}


def local_identity(stat):
    return [stat.st_size, stat.st_mtime_ns, stat.st_ino, stat.st_dev, stat.st_ctime_ns]


class LocalUploadMixin:
    _local_upload_enabled = False
    _local_upload_mode = "mp"
    _local_upload_folder = ""
    _local_upload_target = ""
    _local_upload_rate = 8

    def _init_local_upload(self, config):
        self._upload_receipt_lock = threading.Lock()
        self._local_upload_enabled = config.get("local_upload_enabled") is True
        self._local_upload_mode = str(config.get("local_upload_mode") or "mp")
        self._local_upload_folder = str(config.get("local_upload_folder") or "").strip()
        self._local_upload_target = str(config.get("local_upload_target") or "").strip()
        try:
            self._local_upload_rate = max(0, min(128, int(config.get("local_upload_rate", 8))))
        except (ValueError, TypeError):
            self._local_upload_rate = 8
        try:
            self._local_upload_folder = ",".join(monitor_roots(self._local_upload_folder))
            if self._local_upload_mode not in {"mp", "direct"}:
                raise ValueError("请选择MP整理上传或115插件直接上传")
            if self._local_upload_target:
                self._local_upload_target = cloud_path(self._local_upload_target)
            if self._local_upload_enabled and not self._local_upload_folder:
                raise ValueError("请指定本地计算上传目录")
            if self._local_upload_enabled and self._local_upload_mode == "direct" and not self._local_upload_target:
                raise ValueError("请指定115上传目标目录")
            if self._local_upload_enabled:
                self._upload_receipts()
        except ValueError as failure:
            self._enabled = False
            logger.error(f"自动生成ED2K链接：本地计算上传配置或回执无效，已停止插件：{failure}")

    def _upload_root_for(self, path):
        if not self._local_upload_enabled:
            return None
        resolved = path.resolve()
        roots = sorted(monitor_roots(self._local_upload_folder), key=len, reverse=True)
        for root in roots:
            folder = Path(root).resolve()
            if resolved.is_relative_to(folder) and (self._recursive or resolved.parent == folder):
                return Path(root)
        return None

    def _upload_receipts(self):
        path = Path(self.get_data_path()) / "local_upload_receipts.json"
        try:
            receipts = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(receipts, dict) or any(not isinstance(value, dict) for value in receipts.values()):
            raise ValueError("本地上传回执文件损坏，请检查local_upload_receipts.json")
        return receipts

    def _reset_upload_attempts(self, cache_path, records):
        for source, record in records.items():
            if record.get("upload_mode") and not record.get("upload_verified") and self._upload_root_for(Path(source)):
                record.pop("upload_attempted", None)
                record.pop("upload_first_error", None)
                record.pop("upload_first_error_at", None)
                record.pop("upload_error_code", None)
                record.update(upload_state="computed", upload_note="用户手动补扫：先核实目标，再决定是否重新上传")
        self._write_results(None, cache_path, records)

    def _record_upload_error(self, path, error):
        if self._upload_root_for(path) is None:
            return
        _, _, cache_path = self._paths()
        with self._lock:
            records = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
            record = records.get(str(path.resolve()))
            if record and record.get("upload_mode"):
                if not record.get("upload_first_error"):
                    record.update(upload_first_error=str(error), upload_first_error_at=time.time())
                if isinstance(error, UploadNotStarted) and not record.get("upload_attempted"):
                    record["upload_error_code"] = "not_started"
                record.update(upload_state="failed", upload_note=str(error))
                self._write_results(None, cache_path, records)

    def _on_local_upload_transfer(self, event):
        if not self._enabled or not self._local_upload_enabled or self._local_upload_mode != "mp" or self._stopping.is_set():
            return
        data = getattr(event, "event_data", None)
        if not isinstance(data, dict):
            return
        source = plain_item(data.get("fileitem"))
        transfer = plain_item(data.get("transferinfo"))
        target = saved_item(transfer.get("target_item"))
        if transfer.get("success") is not True or source.get("storage") != "local" or target.get("storage") != "115网盘Plus":
            return
        path = Path(source.get("path") or "")
        if not path.is_absolute() or self._upload_root_for(path) is None or target.get("type") != "file":
            return
        try:
            if not self._eligible(path):
                logger.warning(f"自动生成ED2K链接：MP上传完成但本地文件已移走或不符合过滤条件：{path.name}；此模式请使用复制上传并保留本地文件")
                return
            receipt = {"item": target, "identity": local_identity(path.stat()), "received_at": time.time()}
            with self._upload_receipt_lock:
                receipts = self._upload_receipts()
                receipts[str(path.resolve())] = receipt
                self._atomic_write(Path(self.get_data_path()) / "local_upload_receipts.json", json.dumps(receipts, ensure_ascii=False))
            self._queue_event(path)
        except (OSError, ValueError):
            logger.exception("自动生成ED2K链接：保存MP上传完成回执失败，保留本地文件")

    def _upload_scope(self, storage):
        account = getattr(storage.oper.client, "user_id", None)
        if not re.fullmatch(r"[1-9][0-9]*", str(account or "")):
            raise CloudError("无法确认115账号，保留本地文件")
        values = [str(account), self._local_upload_mode, self._local_upload_target, self._local_upload_folder]
        return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()

    def _upload_destination(self, path):
        root = self._upload_root_for(path)
        if root is None:
            raise CloudError("本地文件不在计算上传目录")
        relative = path.resolve().relative_to(root.resolve())
        destination = PurePosixPath(self._local_upload_target) / PurePosixPath(relative.as_posix())
        return cloud_path(destination.as_posix())

    def _verify_uploaded(self, storage, item, record):
        try:
            item = storage.fresh(item)
            if item.get("storage") != "115网盘Plus" or item.get("type") != "file" or item.get("size") != record["size"]:
                raise CloudError("上传后文件身份或大小不一致，保留本地文件，不转发")
            if storage.file_sha1(item) != record["local_sha1"]:
                raise CloudError("上传后SHA1与本地不一致，保留本地文件，不转发")
            return saved_item(item)
        except CloudError:
            raise
        except Exception as failure:
            raise CloudError(f"115上传内容核验暂时失败（{type(failure).__name__}），保留本地文件") from None

    def _prepare_local_upload(self, path, stat, records, key, output, cache_path):
        from . import ed2k_hash, ed2k_link, record_problem
        record = records.get(key)
        identity = local_identity(stat)
        valid = (record and not record_problem(record) and record.get("local_identity") == identity
                 and re.fullmatch(r"[0-9a-f]{40}", record.get("local_sha1", "")))
        if not valid:
            stopping = self._stopping
            def calculate(progress, on_retry):
                checksum = hashlib.sha1()
                digest = ed2k_hash(path, stopping, progress=progress, checksum=checksum)
                return digest, checksum.hexdigest()
            if self._movie_series_parallel and self._active_work_key == str(path):
                from .media_type import media_kind
                if media_kind(path.name, str(path)) == "movie":
                    digest, checksum = self._background_movie_hash(
                        str(path), tuple(identity), tuple(identity[:3]), path.name, stat.st_size, "本地",
                        calculate)
                else:
                    digest, checksum = calculate(lambda done, size: self._update_local_hash_progress(path, done, size), None)
            else:
                digest, checksum = calculate(lambda done, size: self._update_local_hash_progress(path, done, size), None)
            if stopping.is_set() or local_identity(path.stat()) != identity:
                raise OSError("本地文件在计算期间变化，取消上传")
            record = {
                "name": path.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                "inode": stat.st_ino, "device": stat.st_dev, "ctime_ns": stat.st_ctime_ns,
                "local_identity": identity, "local_sha1": checksum, "hash_version": 2,
                "link": ed2k_link(path.name, digest, stat.st_size), "generated_at": time.time(),
                "source_deleted": False, "telegram_sent": False, "telegram_target": "",
                "upload_mode": self._local_upload_mode, "upload_state": "computed", "upload_verified": False,
            }
            records = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
            records[key] = record
            self._write_results(output, cache_path, records)
            logger.info(f"自动生成ED2K链接：本地ED2K与SHA1计算完成，等待上传校验：{path.name}")
        return record, records

    def _process_local_upload(self, path, stat, output, cache_path):
        from . import ed2k_link
        key = str(path.resolve())
        records = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        record, records = self._prepare_local_upload(path, stat, records, key, output, cache_path)
        storage = CloudStorage("115网盘Plus")
        scope = self._upload_scope(storage)
        if record.get("upload_scope") != scope:
            record.update(upload_scope=scope, upload_mode=self._local_upload_mode, upload_state="computed", upload_verified=False)
            record.pop("upload_cloud_item", None)
            record.pop("upload_attempted", None)
            record.update(telegram_sent=False, telegram_target="", telegram_deliveries={})
        if not record.get("upload_verified"):
            if self._local_upload_mode == "mp":
                with self._upload_receipt_lock:
                    receipt = self._upload_receipts().get(key)
                if not receipt or receipt.get("identity") != local_identity(stat):
                    record.update(upload_state="waiting_mp", upload_note="等待MP复制上传完成；本插件不会主动发起整理")
                    self._write_results(output, cache_path, records)
                    self._clear_retry_failure(key)
                    return
                item = receipt["item"]
            else:
                destination = self._upload_destination(path)
                lookup = getattr(storage.oper, "get_item_strict", None)
                if lookup is None:
                    raise CloudError("115插件缺少严格查询接口，请更新115插件；未上传")
                self._upload_stage(path, "检查115上传目标")
                try:
                    with self._cloud_request_lock:
                        existing = lookup(Path(destination))
                except Exception as failure:
                    raise CloudError(f"115目标查询未确认（{type(failure).__name__}），未重复上传，保留本地文件") from None
                if existing:
                    item = saved_item(existing)
                else:
                    if record.get("upload_attempted"):
                        raise CloudError("上次上传结果未确认且目标未找到，保留本地文件；核实后可手动本地计算上传补扫重试")
                    if local_identity(path.stat()) != record["local_identity"] or self._stopping.is_set():
                        raise InterruptedError("本地文件变化或插件停止，取消上传")
                    record.update(upload_state="preparing", upload_note="检查115上传接口，尚未发起上传")
                    self._write_results(output, cache_path, records)
                    self._upload_stage(path, "准备115秒传／上传")
                    from app.schemas import FileItem
                    target = FileItem(storage="115网盘Plus", path=str(PurePosixPath(destination).parent), type="dir")
                    def mark_started():
                        nonlocal records, record
                        records = json.loads(cache_path.read_text(encoding="utf-8"))
                        record = records[key]
                        record.update(upload_attempted=True, upload_state="uploading",
                                      upload_note="调用115插件上传；不会覆盖同名异内容文件")
                        record.pop("upload_error_code", None)
                        self._write_results(output, cache_path, records)
                    try:
                        uploaded = upload_with_digest(
                            storage.oper, target, path, record["local_sha1"], record["local_identity"],
                            self._stopping, lambda percent: self._upload_progress(path, percent),
                            self._flush_due_groups, self._local_upload_rate, on_start=mark_started,
                        )
                    except CloudError:
                        raise
                    except Exception as failure:
                        raise CloudError(f"115上传调用异常（{type(failure).__name__}），结果待确认，保留本地文件") from None
                    records = json.loads(cache_path.read_text(encoding="utf-8"))
                    record = records[key]
                    if not uploaded:
                        raise CloudError("115上传未返回成功回执，保留本地文件，后续先查询目标，不自动重复上传")
                    item = saved_item(uploaded)
                if cloud_path(item.get("path")) != destination:
                    raise CloudError("115上传回执路径与目标不一致，保留本地文件")
            record.update(upload_state="verifying", upload_note="校验115文件大小与SHA1")
            self._write_results(output, cache_path, records)
            self._upload_stage(path, "校验115文件大小与SHA1")
            with self._cloud_request_lock:
                item = self._verify_uploaded(storage, item, record)
            if self._stopping.is_set() or local_identity(path.stat()) != record["local_identity"]:
                raise OSError("上传期间本地文件变化，不转发、不删除")
            digest = record["link"].split("|")[4]
            record.update(name=item["name"], link=ed2k_link(item["name"], digest, record["size"]),
                          upload_verified=True, upload_cloud_item=item, upload_state="verified",
                          upload_note="115大小与SHA1校验通过；使用本地计算结果，网盘副本不自动删除")
            record.pop("upload_error_code", None)
            self._write_results(output, cache_path, records)
            logger.info(f"自动生成ED2K链接：115上传内容校验通过，复用本地ED2K：{item['name']}")
        else:
            with self._cloud_request_lock:
                self._verify_uploaded(storage, record["upload_cloud_item"], record)
        if self._telegram_aggregate and self._active_batch_id in self._batches:
            with self._condition:
                batch = self._batches[self._active_batch_id]
                batch["contexts"][key] = {
                    "kind": "local", "path": path, "signature": tuple(record["local_identity"][:4]),
                    "output": output, "cache_path": cache_path, "batch_group": batch["groups"].get(key),
                }
            return
        if self._telegram_aggregate:
            candidates = self._aggregate_candidates(record, cache_path, records)
            if candidates:
                self._notify_records(candidates, checkpoint=lambda: self._write_results(output, cache_path, records))
        else:
            self._notify_record(Path(record["name"]), record, checkpoint=lambda: self._write_results(output, cache_path, records))
        self._write_results(output, cache_path, records)
        delivered = not self._telegram_enabled or record.get("telegram_sent")
        if not delivered:
            raise CloudError("上传已确认，Telegram尚未全部投递成功；重试仅补发，不重复上传")
        if self._delete_source and (output is not None or self._telegram_enabled):
            self._delete_uploaded_local(path, record, output, cache_path, records)
            self._clear_retry_failure(key)

    def _upload_stage(self, path, stage):
        active = self._local_active
        if active and active.get("path") == str(path):
            active.update(stage=stage, phase="upload", bytes_read=0, progress_known=False,
                          upload_speed=0, updated_at=time.time())

    def _upload_progress(self, path, percent):
        active = self._local_active
        if not active or active.get("path") != str(path):
            return
        if percent is None:
            self._upload_stage(path, "等待115秒传／上传初始化")
            return
        now = time.time()
        done = min(active["size"], int(active["size"] * percent / 100))
        elapsed = now - active.get("updated_at", now)
        speed = max(0, done - active.get("bytes_read", 0)) / elapsed if elapsed > 0 else 0
        active.update(stage="正在上传至115" if percent < 100 else "传输完成，等待115提交确认",
                      phase="upload", progress_known=True, bytes_read=done, upload_speed=speed,
                      updated_at=now)

    def _delete_uploaded_local(self, path, record, output, cache_path, records):
        if not record.get("upload_verified") or self._stopping.is_set():
            return
        storage = CloudStorage("115网盘Plus")
        if record.get("upload_scope") != self._upload_scope(storage):
            raise CloudError("上传账号或配置范围变化，保留本地文件")
        with self._cloud_request_lock:
            self._verify_uploaded(storage, record["upload_cloud_item"], record)
        stat = path.stat()
        if local_identity(stat) != record.get("local_identity"):
            raise OSError("本地文件身份变化，保留源文件")
        self._unlink_source(path, stat)
        record.update(source_deleted=True, deleted_at=time.time())
        record.pop("delete_failed", None)
        self._write_results(output, cache_path, records)

    def _finish_uploaded_local_delete(self, path, record, output, cache_path, records):
        try:
            self._delete_uploaded_local(path, record, output, cache_path, records)
            self._clear_retry_failure(str(path.resolve()))
        except (OSError, ValueError) as failure:
            record.update(delete_failed=True, upload_note=f"上传已确认，但本地删源校验失败：{failure}")
            self._write_results(output, cache_path, records)
            key = str(path.resolve())
            attempts, exhausted = self._retry_failure(
                key, "local", record.get("local_identity", [])[:3], path.name, record["upload_note"])
            if not exhausted:
                self._queue_event(path, delay=60)
            else:
                logger.error(f"自动生成ED2K链接：本地上传删源校验达到重试上限（{attempts}次），保留源文件：{path.name}")

    @staticmethod
    def _uploaded_record(item, records):
        return next((record for record in records.values()
                     if record.get("upload_verified")
                     and str(record.get("upload_cloud_item", {}).get("fileid") or "") == str(item.get("fileid") or "")
                     and record.get("upload_cloud_item", {}).get("storage") == item.get("storage")), None)

    def _skip_uploaded_cloud(self, storage, item, records):
        record = self._uploaded_record(item, records)
        if record:
            with self._cloud_request_lock:
                self._verify_uploaded(storage, item, record)
            return True
        if self._local_upload_enabled and self._local_upload_mode == "mp":
            with self._upload_receipt_lock:
                receipts = self._upload_receipts()
            for source, receipt in receipts.items():
                target = receipt.get("item", {})
                if target.get("storage") == item.get("storage") and str(target.get("fileid")) == str(item.get("fileid")):
                    path = Path(source)
                    if path.is_file() and self._upload_root_for(path) and local_identity(path.stat()) == receipt.get("identity"):
                        self._queue_event(path)
                        return True
        return False
