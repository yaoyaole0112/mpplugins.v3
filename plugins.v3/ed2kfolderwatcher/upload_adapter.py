import hashlib
import math
import threading
import time
import types
import re
import hashlib
from pathlib import Path

from .cloud_storage import CloudError


UPLOAD_GATE = threading.Lock()


class UploadNotStarted(CloudError):
    pass


def instant_upload_check(oper, path, digest, size, target_id):
    if not re.fullmatch(r"[1-9][0-9]{0,63}", str(target_id or "")):
        return None
    client = oper.client
    upload_file_init = getattr(client, "upload_file_init", None)

    def read_range(value):
        match = re.search(r"(?<!\d)(\d+)\s*[-:]\s*(\d+)(?!\d)", str(value or ""))
        if match:
            start, end = map(int, match.groups())
        else:
            start, end = 0, min(size, 1024 * 1024) - 1
        if not 0 <= start <= end < size or end - start > 1024 * 1024:
            raise CloudError("115秒传抽样校验范围超出文件")
        checksum = hashlib.sha1()
        remaining = end - start + 1
        with Path(path).open("rb") as source:
            source.seek(start)
            while remaining:
                chunk = source.read(min(256 * 1024, remaining))
                if not chunk:
                    raise OSError("115秒传抽样校验时本地文件不完整")
                checksum.update(chunk)
                remaining -= len(chunk)
        return checksum.hexdigest().upper()

    if callable(upload_file_init):
        response = upload_file_init(
            Path(path).name, digest, size, "", read_range, f"U_1_{target_id}", timeout=30
        )
    else:
        upload_init = getattr(client, "upload_init", None)
        if not callable(upload_init):
            return None
        response = upload_init(
            {"filename": Path(path).name, "filesize": size, "fileid": digest,
             "target": f"U_1_{target_id}"},
            read_range_bytes_or_hash=read_range, timeout=30,
        )
    if not isinstance(response, dict):
        raise CloudError("115秒传接口返回了无效响应")
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    status = response.get("status", data.get("status"))
    try:
        status = int(status or 0)
    except (TypeError, ValueError):
        status = 0
    if response.get("reuse") or data.get("reuse") or status == 2:
        return True
    if status == 1:
        return False
    raise CloudError(f"115秒传检查未完成（返回状态 {status}），保留本地文件")


def resolve_uploader(oper):
    method = oper.upload
    function = getattr(method, "__func__", method)
    required = {"transfer_process", "global_vars", "Bucket"}
    if isinstance(function, types.FunctionType):
        if required <= function.__globals__.keys():
            return function, None
        patcher = function.__globals__.get("P115DiskPatcher")
        helper = function.__globals__.get("P115DiskCore")
        patched_class = getattr(patcher, "_patched_class", None)
        if (function.__name__ == "_patch_upload" and isinstance(patcher, type)
                and isinstance(helper, type) and isinstance(patched_class, type)
                and isinstance(oper, patched_class) and getattr(patcher, "_active", False) is True
                and getattr(patcher, "_patch_upload", None) is function):
            enhanced = getattr(helper, "upload", None)
            if isinstance(enhanced, types.FunctionType) and required <= enhanced.__globals__.keys():
                return enhanced, helper
    raise UploadNotStarted("115上传接口适配检查未通过，尚未发起上传；请更新自动生成ED2K链接插件")


def upload_with_digest(oper, target, path, digest, identity, stopping, progress, checkpoint, rate_limit=8, on_start=None,
                      before_upload=None, on_result=None):
    from .local_upload import local_identity
    last_check = [0.0]

    def check(force=False):
        if stopping.is_set():
            raise InterruptedError("插件停止，取消115上传")
        now = time.monotonic()
        if force or now - last_check[0] >= 0.5:
            last_check[0] = now
            if local_identity(path.stat()) != identity:
                raise OSError("上传期间本地文件变化，保留源文件")

    while not UPLOAD_GATE.acquire(timeout=0.5):
        check()
    pending = {}
    confirmed = [0]
    upload_kind = ["unknown"]
    next_write = [None]
    try:
        check(force=True)
        if before_upload is not None:
            existing = before_upload()
            if existing is not None:
                return existing
        function, helper = resolve_uploader(oper)
        bindings = dict(function.__globals__)
        original_stop = bindings["global_vars"]
        original_bucket = bindings["Bucket"]
        last_checkpoint = [0.0]
        last_progress = [0.0]
        started = [False]

        def begin_upload():
            check(force=True)
            if not started[0]:
                if on_start is not None:
                    on_start()
                started[0] = True

        def wait(seconds):
            deadline = time.monotonic() + max(0, seconds)
            while True:
                check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                if stopping.wait(min(1, remaining)):
                    raise InterruptedError("插件停止，取消115上传等待")
                checkpoint()

        def report(percent, flush=True):
            check()
            if not isinstance(percent, (int, float)) or not math.isfinite(percent):
                return
            now = time.monotonic()
            if flush or now - last_progress[0] >= 0.25:
                progress(max(0, min(100, percent)))
                last_progress[0] = now
            if flush and now - last_checkpoint[0] >= 1:
                last_checkpoint[0] = now
                checkpoint()

        def transfer_process(source):
            if Path(source).resolve() != path.resolve():
                raise CloudError("115上传进度来源不一致，停止上传")
            progress(None)
            def update(percent):
                if isinstance(percent, (int, float)) and math.isfinite(percent):
                    confirmed[0] = int(identity[0] * max(0, min(100, percent)) / 100)
                report(percent)
            return update

        class StopProxy:
            def is_transfer_stopped(self, source):
                if Path(source).resolve() == path.resolve():
                    check()
                return original_stop.is_transfer_stopped(source)

            def __getattr__(self, name):
                return getattr(original_stop, name)

        class ClientProxy:
            def __getattr__(self, name):
                value = getattr(oper.client, name)
                if name not in {"upload_file_init", "upload_gettoken"}:
                    return value

                def call(*args, **kwargs):
                    check()
                    kwargs.setdefault("timeout", 30)
                    if name == "upload_file_init":
                        begin_upload()
                    if name == "upload_file_init" and "read_range_bytes_or_hash" in kwargs:
                        kwargs["read_range_bytes_or_hash"] = range_hash
                    response = value(*args, **kwargs)
                    if name == "upload_file_init":
                        upload_kind[0] = (
                            "instant" if isinstance(response, dict)
                            and response.get("state") is not False and response.get("reuse") is True
                            else "unknown"
                        )
                    return response
                return call

        def range_hash(value):
            check(force=True)
            start, end = map(int, value.split("-"))
            if not 0 <= start <= end < identity[0]:
                raise CloudError("115上传校验范围无效，保留本地文件")
            checksum = hashlib.sha1()
            remaining = end - start + 1
            with path.open("rb") as source:
                source.seek(start)
                while remaining:
                    check()
                    chunk = source.read(min(256 * 1024, remaining))
                    if not chunk:
                        raise OSError("115上传校验时本地文件不完整")
                    checksum.update(chunk)
                    remaining -= len(chunk)
            check(force=True)
            return checksum.hexdigest().upper()

        class OperProxy:
            client = ClientProxy()

            def _calc_sha1(self, source, size=None):
                check(force=True)
                if Path(source).resolve() != path.resolve() or size is not None:
                    raise CloudError("115上传请求的SHA1范围不一致，停止上传")
                return digest

            def __getattr__(self, name):
                value = getattr(oper, name)
                if name == "_get_oss_token" and isinstance(getattr(value, "__func__", None), types.FunctionType):
                    return types.MethodType(value.__func__, self)
                return value

        class BucketProxy:
            def __init__(self, *args, **kwargs):
                kwargs["connect_timeout"] = 60
                self.bucket = original_bucket(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self.bucket, name)

            def init_multipart_upload(self, key, *args, **kwargs):
                check()
                begin_upload()
                result = self.bucket.init_multipart_upload(key, *args, **kwargs)
                pending[(key, result.upload_id)] = self
                return result

            def upload_part(self, key, upload_id, part_number, **kwargs):
                check()
                last_size = [0]

                def on_bytes(consumed, total):
                    delta = max(0, consumed - last_size[0])
                    last_size[0] = consumed
                    if rate_limit:
                        now = time.monotonic()
                        next_write[0] = max(next_write[0] or now, now) + delta / (rate_limit * 1024 ** 2)
                        delay = next_write[0] - now
                        if delay > 0 and stopping.wait(delay):
                            raise InterruptedError("插件停止，取消115上传")
                    report((confirmed[0] + consumed) * 100 / identity[0], flush=False)

                pending[(key, upload_id)] = self
                result = self.bucket.upload_part(key, upload_id, part_number, **kwargs, progress_callback=on_bytes)
                confirmed[0] += last_size[0]
                return result

            def complete_multipart_upload(self, key, upload_id, *args, **kwargs):
                check(force=True)
                result = self.bucket.complete_multipart_upload(key, upload_id, *args, **kwargs)
                if result.status == 200:
                    pending.pop((key, upload_id), None)
                    upload_kind[0] = "ordinary"
                return result

            def abort_multipart_upload(self, key, upload_id):
                result = self.bucket.abort_multipart_upload(key, upload_id)
                pending.pop((key, upload_id), None)
                return result

        bindings.update(transfer_process=transfer_process, global_vars=StopProxy(), Bucket=BucketProxy, sleep=wait)
        scoped = types.FunctionType(function.__code__, bindings, function.__name__, function.__defaults__, function.__closure__)
        scoped.__kwdefaults__ = function.__kwdefaults__
        proxy = OperProxy()
        receiver = helper(p115_api=proxy) if helper is not None else proxy
        uploaded = scoped(receiver, target, path, new_name=path.name)
        if uploaded and on_result is not None:
            on_result(upload_kind[0])
        return uploaded
    finally:
        for (key, upload_id), bucket in tuple(pending.items()):
            try:
                bucket.abort_multipart_upload(key, upload_id)
            except Exception:
                pass
        UPLOAD_GATE.release()
