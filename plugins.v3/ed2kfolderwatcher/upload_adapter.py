import hashlib
import math
import threading
import time
import types
from pathlib import Path

from .cloud_storage import CloudError


UPLOAD_GATE = threading.Lock()


def upload_with_digest(oper, target, path, digest, identity, stopping, progress, checkpoint, rate_limit=8):
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
    next_write = [None]
    try:
        check(force=True)
        method = oper.upload
        function = getattr(method, "__func__", None)
        required = {"transfer_process", "global_vars", "Bucket"}
        if not isinstance(function, types.FunctionType) or not required <= function.__globals__.keys():
            raise CloudError("当前115上传接口不支持安全进度适配，请更新115网盘Plus；未发起上传")
        bindings = dict(function.__globals__)
        original_stop = bindings["global_vars"]
        original_bucket = bindings["Bucket"]
        last_checkpoint = [0.0]
        last_progress = [0.0]

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
                    if name == "upload_file_init" and "read_range_bytes_or_hash" in kwargs:
                        kwargs["read_range_bytes_or_hash"] = range_hash
                    return value(*args, **kwargs)
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
                return result

            def abort_multipart_upload(self, key, upload_id):
                result = self.bucket.abort_multipart_upload(key, upload_id)
                pending.pop((key, upload_id), None)
                return result

        bindings.update(transfer_process=transfer_process, global_vars=StopProxy(), Bucket=BucketProxy)
        scoped = types.FunctionType(function.__code__, bindings, function.__name__, function.__defaults__, function.__closure__)
        scoped.__kwdefaults__ = function.__kwdefaults__
        return scoped(OperProxy(), target, path, new_name=path.name)
    finally:
        for (key, upload_id), bucket in tuple(pending.items()):
            try:
                bucket.abort_multipart_upload(key, upload_id)
            except Exception:
                pass
        UPLOAD_GATE.release()
