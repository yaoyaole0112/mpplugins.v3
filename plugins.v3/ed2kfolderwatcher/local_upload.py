import hashlib
import json
import re
import threading
import time
from pathlib import Path, PurePosixPath

from app.log import logger

from .cloud_storage import CloudError, CloudStorage, cloud_path, monitor_roots
from .upload_adapter import UploadNotStarted, instant_upload_check, upload_with_digest


UPLOAD_FIELDS = (
    "local_upload_enabled", "local_upload_mode", "local_upload_folder",
    "local_upload_target", "local_upload_rate", "local_upload_delete_source",
    "local_upload_delete_cloud_copy",
)


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
    _local_upload_delete_source = False
    _local_upload_delete_cloud_copy = False

    def _init_local_upload(self, config):
        self._upload_receipt_lock = threading.Lock()
        self._local_upload_enabled = config.get("local_upload_enabled") is True
        self._local_upload_mode = str(config.get("local_upload_mode") or "mp")
        self._local_upload_folder = str(config.get("local_upload_folder") or "").strip()
        self._local_upload_target = str(config.get("local_upload_target") or "").strip()
        self._local_upload_delete_source = config.get("local_upload_delete_source") is True
        self._local_upload_delete_cloud_copy = config.get("local_upload_delete_cloud_copy") is True
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
                self._load_local_upload_queue()
                self._start_local_upload_worker()
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

    def _mp_upload_receipt(self, path, stat):
        with self._upload_receipt_lock:
            receipt = self._upload_receipts().get(str(path.resolve()))
        if not receipt or receipt.get("identity") != local_identity(stat):
            return None
        item = receipt.get("item")
        if not isinstance(item, dict) or item.get("storage") != "115网盘Plus" or item.get("type") != "file":
            return None
        if not re.fullmatch(r"[1-9][0-9]{0,63}", str(item.get("fileid") or "")) or item.get("size") != stat.st_size:
            return None
        try:
            cloud_path(item.get("path"))
        except ValueError:
            return None
        return receipt

    def _local_upload_queue_path(self):
        return Path(self.get_data_path()) / "local_upload_queue.json"

    def _load_local_upload_queue(self):
        try:
            queue = json.loads(self._local_upload_queue_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            queue = {}
        except (OSError, ValueError):
            raise ValueError("本地115上传队列文件损坏，请检查local_upload_queue.json")
        if not isinstance(queue, dict) or any(
            not isinstance(item, dict) or not isinstance(item.get("identity"), list)
            or not isinstance(item.get("path"), str) for item in queue.values()
        ):
            raise ValueError("本地115上传队列格式错误，请检查local_upload_queue.json")
        with self._local_upload_queue_lock:
            self._local_upload_queue = queue

    def _save_local_upload_queue(self):
        self._atomic_write(
            self._local_upload_queue_path(),
            json.dumps(self._local_upload_queue, ensure_ascii=False),
        )

    def _start_local_upload_worker(self):
        if not self._enabled or not self._local_upload_enabled or self._stopping.is_set():
            return
        with self._local_upload_queue_lock:
            self._local_upload_queue_wake.set()
            if self._local_upload_worker and self._local_upload_worker.is_alive():
                return
            self._local_upload_worker = threading.Thread(
                target=self._drain_local_upload_queue,
                name="ed2k-normal-upload",
                daemon=True,
            )
            self._local_upload_worker.start()

    def _queue_local_upload(self, path, identity):
        key = str(path.resolve())
        with self._local_upload_queue_lock:
            current = self._local_upload_queue.get(key)
            if current and current.get("identity") == list(identity):
                return
            self._local_upload_queue[key] = {
                "path": key, "identity": list(identity), "queued_at": time.time(),
                "retry_at": 0, "attempts": 0,
            }
            try:
                self._save_local_upload_queue()
            except Exception:
                self._local_upload_queue.pop(key, None)
                raise
            self._local_upload_queue_wake.set()
        self._start_local_upload_worker()

    def _remove_local_upload_queue(self, key):
        with self._local_upload_queue_lock:
            self._local_upload_queue.pop(key, None)
            self._save_local_upload_queue()

    def _drain_local_upload_queue(self):
        while not self._stopping.is_set():
            now = time.time()
            with self._local_upload_queue_lock:
                candidate = next(
                    ((key, item) for key, item in self._local_upload_queue.items()
                     if item.get("retry_at", 0) <= now),
                    None,
                )
                next_retry = min(
                    (item.get("retry_at", 0) for item in self._local_upload_queue.values()
                     if item.get("retry_at", 0) > now),
                    default=now + 5,
                )
            if candidate is None:
                self._local_upload_queue_wake.wait(max(0.2, min(5, next_retry - now)))
                self._local_upload_queue_wake.clear()
                continue
            key, item = candidate
            path = Path(item["path"])
            try:
                stat = path.stat()
                if local_identity(stat) != item["identity"] or not self._eligible(path):
                    self._remove_local_upload_queue(key)
                    continue
                self._upload_active = {
                    "path": str(path), "name": path.name, "size": stat.st_size,
                    "bytes_read": 0, "phase": "upload", "progress_known": False,
                    "stage": "MP上传已完成，等待内容核验" if self._local_upload_mode == "mp" else "等待115秒传／普通上传通道", "started_at": time.time(),
                }
                output, cache_path = self._paths()[1:]
                output = output if self._output_enabled else None
                self._process_local_upload(path, stat, output, cache_path, upload_worker=True)
                self._remove_local_upload_queue(key)
            except InterruptedError:
                if self._stopping.is_set():
                    break
                self._remove_local_upload_queue(key)
            except Exception as failure:
                try:
                    self._record_upload_error(path, failure)
                    with self._local_upload_queue_lock:
                        current = self._local_upload_queue.get(key)
                        if current:
                            attempts = int(current.get("attempts", 0)) + 1
                            if attempts <= self._retry_limit:
                                delay = min(3600, 60 * 2 ** min(attempts - 1, 5))
                                current.update(attempts=attempts, retry_at=time.time() + delay)
                                records_path = self._paths()[2]
                                records = json.loads(records_path.read_text(encoding="utf-8")) if records_path.exists() else {}
                                record = records.get(key)
                                if record:
                                    retry_note = (
                                        f"{failure}；{delay}秒后重试Telegram投递，不会重新上传"
                                        if self._local_upload_mode == "mp"
                                        else f"{failure}；{delay}秒后在独立上传队列重试"
                                    )
                                    record.update(upload_state="queued", upload_note=retry_note)
                                    self._write_results(None, records_path, records)
                                self._save_local_upload_queue()
                                self._local_upload_queue_wake.set()
                            else:
                                self._local_upload_queue.pop(key, None)
                                self._save_local_upload_queue()
                except Exception:
                    logger.exception("自动生成ED2K链接：更新独立115上传队列失败")
            finally:
                self._upload_active = None

    def _reset_upload_attempts(self, cache_path, records):
        for source, record in records.items():
            if record.get("upload_mode") and not record.get("upload_verified") and self._upload_root_for(Path(source)):
                record.pop("upload_attempted", None)
                record.pop("upload_first_error", None)
                record.pop("upload_first_error_at", None)
                record.pop("upload_error_code", None)
                record.update(upload_state="computed", upload_note=(
                    "用户手动补扫：仅处理已有MP上传成功回执的文件"
                    if self._local_upload_mode == "mp"
                    else "用户手动补扫：先核实目标，再决定是否重新上传"
                ))
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
            identity = local_identity(path.stat())
            upload_kind = None
            try:
                from app.plugins.p115disk.upload_receipts import consume_upload_result

                result = consume_upload_result(path, identity, target)
                if result:
                    upload_kind = result.get("upload_kind")
            except Exception as failure:
                logger.warning(
                    f"自动生成ED2K链接：无法读取115上传类型回执；FUCK孤本转发将跳过："
                    f"{path.name}（{type(failure).__name__}）"
                )
            receipt = {
                "item": target,
                "identity": identity,
                "upload_kind": upload_kind if upload_kind in {"instant", "ordinary"} else "unknown",
                "received_at": time.time(),
            }
            with self._upload_receipt_lock:
                receipts = self._upload_receipts()
                receipt_key = str(path.resolve())
                previous = receipts.get(receipt_key)
                if (
                    previous
                    and previous.get("identity") == identity
                    and saved_item(previous.get("item")) == target
                    and previous.get("upload_kind") in {"instant", "ordinary"}
                    and receipt["upload_kind"] == "unknown"
                ):
                    receipt["upload_kind"] = previous["upload_kind"]
                receipts[receipt_key] = receipt
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

    def _process_local_upload(self, path, stat, output, cache_path, upload_worker=False):
        from . import ed2k_link
        key = str(path.resolve())
        receipt = None
        if self._local_upload_mode == "mp":
            receipt = self._mp_upload_receipt(path, stat)
            if receipt is None:
                return
        records = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        record, records = self._prepare_local_upload(path, stat, records, key, output, cache_path)
        if self._local_upload_mode == "mp" and receipt:
            record["upload_kind"] = (
                receipt.get("upload_kind")
                if receipt.get("upload_kind") in {"instant", "ordinary"}
                else "unknown"
            )
            self._write_results(output, cache_path, records)
        if not upload_worker:
            if self._local_upload_mode != "mp" and not record.get("upload_verified") and not record.get("upload_attempted"):
                self._try_local_instant_upload(path, record, output, cache_path, records)
                records = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else records
                record = records.get(key, record)
            record.update(
                upload_state="verifying" if self._local_upload_mode == "mp" else "upload_queued",
                upload_note=(
                    "MP复制上传已完成，本地ED2K已计算；等待115内容核验"
                    if self._local_upload_mode == "mp"
                    else record.get("upload_note") or "ED2K已计算；等待独立115上传通道（先检查秒传，未命中再普通上传）"
                ),
            )
            self._write_results(output, cache_path, records)
            self._queue_local_upload(path, record["local_identity"])
            return
        storage = CloudStorage("115网盘Plus")
        scope = self._upload_scope(storage)
        if record.get("upload_scope") != scope:
            record.update(upload_scope=scope, upload_mode=self._local_upload_mode, upload_state="computed", upload_verified=False)
            record.pop("upload_cloud_item", None)
            record.pop("upload_attempted", None)
            record.pop("upload_kind", None)
            record.update(telegram_sent=False, telegram_target="", telegram_deliveries={})
            record.update(ordinary_upload_telegram_sent=False, ordinary_upload_telegram_target="",
                          ordinary_upload_telegram_deliveries={})
        if not record.get("upload_verified"):
            if self._local_upload_mode == "mp":
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
                    instant_reused = [False]

                    def recheck_instant_upload():
                        self._upload_stage(path, "上传通道空闲，重新检查115秒传")
                        try:
                            with self._cloud_request_lock:
                                folder_item = lookup(Path(PurePosixPath(destination).parent.as_posix()))
                            folder_item = saved_item(folder_item)
                            folder_id = str(folder_item.get("fileid") or "")
                            if folder_item.get("type") != "dir" or not re.fullmatch(r"[1-9][0-9]{0,63}", folder_id):
                                record.update(rapid_check="unavailable",
                                              upload_note="上传前无法确认目标目录；继续按普通上传检查")
                                self._write_results(output, cache_path, records)
                                return None
                            record.update(rapid_check_started=True, upload_state="checking_instant",
                                          upload_note="已取得115上传通道，最后复查秒传")
                            self._write_results(output, cache_path, records)
                            with self._cloud_request_lock:
                                hit = instant_upload_check(
                                    storage.oper, path, record["local_sha1"], record["size"], folder_id,
                                )
                            record.update(rapid_check="hit" if hit is True else "miss" if hit is False else "unavailable")
                            record.pop("rapid_check_started", None)
                            self._write_results(output, cache_path, records)
                            if hit is not True:
                                return None
                            with self._cloud_request_lock:
                                instant_item = lookup(Path(destination))
                            if not instant_item:
                                raise CloudError("上传前复查已命中115秒传，但目标文件暂不可见；等待重试核验，不启动普通上传")
                            instant_reused[0] = True
                            return instant_item
                        except CloudError:
                            raise
                        except Exception as failure:
                            record.update(rapid_check="unavailable", upload_note=f"上传前秒传复查不可用（{type(failure).__name__}）；继续普通上传")
                            record.pop("rapid_check_started", None)
                            self._write_results(output, cache_path, records)
                            logger.warning(f"自动生成ED2K链接：上传前秒传复查不可用，继续普通上传：{path.name}（{type(failure).__name__}）")
                            return None

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
                            before_upload=recheck_instant_upload,
                        )
                    except CloudError:
                        raise
                    except Exception as failure:
                        raise CloudError(f"115上传调用异常（{type(failure).__name__}），结果待确认，保留本地文件") from None
                    records = json.loads(cache_path.read_text(encoding="utf-8"))
                    record = records[key]
                    if not uploaded:
                        raise CloudError("115上传未返回成功回执，保留本地文件，后续先查询目标，不自动重复上传")
                    record["upload_kind"] = "instant" if instant_reused[0] else "ordinary"
                    item = saved_item(uploaded)
                    try:
                        from app.plugins.p115disk.upload_receipts import consume_upload_result

                        consume_upload_result(path, record["local_identity"], item)
                    except Exception as failure:
                        logger.debug(
                            f"自动生成ED2K链接：清理直传类型回执失败：{path.name} "
                            f"({type(failure).__name__})"
                        )
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
                          upload_note="115大小与SHA1校验通过；链接复用本地计算结果")
            if (self._local_upload_delete_cloud_copy and self._cloud_delete_enabled
                    and self._local_upload_cloud_delete_scope):
                record["upload_cloud_delete_scope"] = self._local_upload_cloud_delete_scope
            record.pop("upload_error_code", None)
            self._write_results(output, cache_path, records)
            logger.info(f"自动生成ED2K链接：115上传内容校验通过，复用本地ED2K：{item['name']}")
        else:
            with self._cloud_request_lock:
                self._verify_uploaded(storage, record["upload_cloud_item"], record)
        ordinary_forward_required = (
            self._ordinary_upload_telegram_enabled and record.get("upload_kind") == "ordinary"
        )
        if ordinary_forward_required:
            self._notify_ordinary_upload_record(
                record, checkpoint=lambda: self._write_results(output, cache_path, records),
            )
            self._write_results(output, cache_path, records)
            if not record.get("ordinary_upload_telegram_sent"):
                raise CloudError("115普通上传及内容校验已确认，独立普通上传Telegram目标尚未全部投递成功；重试仅补发，不重新上传")
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
        delivered = (
            (not self._telegram_enabled or record.get("telegram_sent"))
            and (not ordinary_forward_required or record.get("ordinary_upload_telegram_sent"))
        )
        if not delivered:
            if ordinary_forward_required and not record.get("ordinary_upload_telegram_sent"):
                raise CloudError("115普通上传及内容校验已确认，独立普通上传Telegram目标尚未全部投递成功；重试仅补发，不重新上传")
            if self._local_upload_mode == "mp":
                raise CloudError("MP整理上传及网盘内容校验已确认，Telegram尚未全部投递成功；重试仅补发，不重新上传")
            raise CloudError("115上传已确认，Telegram尚未全部投递成功；重试仅补发，不重复上传")
        if self._local_upload_delete_cloud_copy and not record.get("upload_cloud_copy_deleted"):
            self._delete_uploaded_cloud_copy(record, output, cache_path, records)
        if self._local_upload_delete_source and (
                output is not None or self._telegram_enabled
                or (ordinary_forward_required and record.get("ordinary_upload_telegram_sent"))):
            self._delete_uploaded_local(path, record, output, cache_path, records)
            self._clear_retry_failure(key)

    def _delete_uploaded_cloud_copy(self, record, output, cache_path, records):
        if record.get("upload_cloud_delete_state") == "uncertain":
            raise CloudError("115网盘副本删除结果未确认；不会自动重发删除请求，请先到网盘核实")
        if not self._cloud_delete_enabled or not self._local_upload_cloud_delete_scope:
            raise CloudError("未授权删除115网盘副本，已保留")
        scope = record.get("upload_cloud_delete_scope")
        if not scope or scope != self._local_upload_cloud_delete_scope:
            raise CloudError("上传时未记录匹配的网盘删源授权；保留115副本")
        storage = CloudStorage("115网盘Plus")
        if storage.delete_scope(self._local_upload_target, True) != scope:
            raise CloudError("115账号或上传目标目录授权已变化，保留网盘副本")
        item = record.get("upload_cloud_item") or {}
        with self._cloud_request_lock:
            current = storage.prepare_delete(
                item, record.get("local_sha1"), self._local_upload_target, True, scope,
            )
        record.update(upload_cloud_delete_state="inflight",
                      upload_cloud_delete_note="已复核上传副本身份与SHA1，等待115删除确认")
        self._write_results(output, cache_path, records)
        try:
            with self._cloud_request_lock:
                deleted = storage.delete_prepared(
                    current, self._local_upload_target, True, scope, self._stopping,
                )
        except Exception:
            record.update(upload_cloud_delete_state="uncertain",
                          upload_cloud_delete_note="115副本删除结果未确认；不会自动重发删除请求")
            self._write_results(output, cache_path, records)
            raise CloudError("115网盘副本删除结果未确认；请先到网盘核实，未自动重试") from None
        if deleted is not True:
            record.update(upload_cloud_delete_state="uncertain",
                          upload_cloud_delete_note="115未确认副本删除成功；不会自动重发删除请求")
            self._write_results(output, cache_path, records)
            raise CloudError("115网盘副本删除未确认，已停止自动重试")
        record.update(upload_cloud_copy_deleted=True, upload_cloud_delete_state="deleted",
                      upload_cloud_delete_note="115已确认上传副本删除")
        record.pop("delete_failed", None)
        self._write_results(output, cache_path, records)

    def _try_local_instant_upload(self, path, record, output, cache_path, records):
        key = str(path.resolve())
        try:
            destination = self._upload_destination(path)
            storage = CloudStorage("115网盘Plus")
            lookup = getattr(storage.oper, "get_item_strict", None)
            if lookup is None:
                return
            parent = PurePosixPath(destination).parent.as_posix()
            with self._cloud_request_lock:
                folder = lookup(Path(parent))
            folder = saved_item(folder)
            if not folder or folder.get("type") != "dir":
                return
            folder_id = str(folder.get("fileid") or "")
            if not re.fullmatch(r"[1-9][0-9]{0,63}", folder_id):
                return
            record.update(rapid_check_started=True, upload_state="checking_instant",
                          upload_note="快速检查115秒传；未命中将转入独立普通上传队列")
            self._write_results(output, cache_path, records)
            self._upload_stage(path, "快速检查115秒传")
            with self._cloud_request_lock:
                hit = instant_upload_check(
                    storage.oper, path, record["local_sha1"],
                    record["size"], folder_id,
                )
            record.update(rapid_check="hit" if hit is True else "miss" if hit is False else "unavailable")
            record.pop("rapid_check_started", None)
            record["upload_note"] = (
                "115快速秒传命中；等待独立核验与链接输出" if hit is True
                else "115未命中秒传；已转入独立普通上传队列" if hit is False
                else "秒传接口不可用；转入独立上传通道检查"
            )
            self._write_results(output, cache_path, records)
            logger.info(f"自动生成ED2K链接：本地文件秒传预检查完成（{'命中' if hit else '未命中' if hit is False else '不可用'}）：{path.name}")
        except Exception as failure:
            record.update(rapid_check="uncertain", upload_note=f"秒传预检查未确认（{type(failure).__name__}）；转入独立上传通道只读核验")
            record.pop("rapid_check_started", None)
            self._write_results(output, cache_path, records)
            logger.warning(f"自动生成ED2K链接：秒传预检查未确认，转入上传通道核验：{path.name}（{type(failure).__name__}）")

    def _upload_stage(self, path, stage):
        active = next((item for item in (self._upload_active, self._local_active)
                       if item and item.get("path") == str(path)), None)
        if active and active.get("path") == str(path):
            active.update(stage=stage, phase="upload", bytes_read=0, progress_known=False,
                          upload_speed=0, updated_at=time.time())

    def _upload_progress(self, path, percent):
        active = next((item for item in (self._upload_active, self._local_active)
                       if item and item.get("path") == str(path)), None)
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
