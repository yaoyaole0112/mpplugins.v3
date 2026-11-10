"""Bounded, cancellable reads and sequential OSS upload without rapid-upload waiting."""

import base64
import time


class TransferStopped(InterruptedError):
    pass


class UploadUncertain(RuntimeError):
    pass


class LimitedReader:
    def __init__(self, stream, stop, limit_mib=0):
        self.stream = stream
        self.stop = stop
        self.rate = float(limit_mib) * 1024 * 1024
        self.started = time.monotonic()
        self.consumed = 0

    def read(self, size=-1):
        if self.stop.is_set():
            raise TransferStopped("插件已停止，保留源文件和待处理队列")
        data = self.stream.read(size if size >= 0 else 1024 * 1024)
        self.consumed += len(data)
        if self.rate:
            delay = self.consumed / self.rate - (time.monotonic() - self.started)
            if delay > 0 and self.stop.wait(delay):
                raise TransferStopped("插件已停止，保留源文件和待处理队列")
        return data


def upload_oss(client, path, response, stop, limit_mib, request, progress, before_complete):
    from oss2 import Bucket, StsAuth, determine_part_size
    from oss2.models import PartInfo
    from oss2.utils import SizedFileAdapter

    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    data = {**response, **data}
    callback = data.get("callback") or {}
    if not all(data.get(field) for field in ("bucket", "object")) or not all(
            callback.get(field) for field in ("callback", "callback_var")):
        raise ValueError("115 未返回完整上传凭据，保留源文件；请稍后重新扫描")

    def make_bucket():
        token = request(lambda: client.upload_gettoken(timeout=30))
        if isinstance(token.get("data"), dict):
            token = {**token, **token["data"]}
        if not all(token.get(field) for field in ("AccessKeyId", "AccessKeySecret", "SecurityToken")):
            raise ValueError("115 上传凭据获取失败，保留源文件")
        endpoint = token.get("endpoint") or "https://oss-cn-shenzhen.aliyuncs.com"
        return Bucket(StsAuth(token["AccessKeyId"], token["AccessKeySecret"], token["SecurityToken"]),
                      endpoint, data["bucket"], connect_timeout=60)

    bucket = make_bucket()
    refreshed = time.monotonic()
    object_name = data["object"]
    upload_id = bucket.init_multipart_upload(object_name).upload_id
    size = path.stat().st_size
    part_size = determine_part_size(size, preferred_size=10 * 1024 * 1024)
    parts = []
    completing = False
    try:
        with path.open("rb") as stream:
            reader = LimitedReader(stream, stop, limit_mib)
            offset = 0
            while offset < size:
                if stop.is_set():
                    raise TransferStopped("插件已停止，保留源文件")
                if time.monotonic() - refreshed > 600:
                    bucket = make_bucket()
                    refreshed = time.monotonic()
                amount = min(part_size, size - offset)
                result = bucket.upload_part(object_name, upload_id, len(parts) + 1,
                                            SizedFileAdapter(reader, amount))
                parts.append(PartInfo(len(parts) + 1, result.etag))
                offset += amount
                progress(offset, size)
        if stop.is_set():
            raise TransferStopped("插件已停止，未提交上传完成")
        before_complete()
        headers = {
            "x-oss-callback": base64.b64encode(callback["callback"].encode()).decode(),
            "x-oss-callback-var": base64.b64encode(callback["callback_var"].encode()).decode(),
        }
        completing = True
        result = bucket.complete_multipart_upload(object_name, upload_id, parts, headers=headers)
        if result.status != 200:
            raise UploadUncertain("上传完成回调未确认成功，待核实网盘结果，未重复上传")
        return result
    except Exception as error:
        if completing:
            raise UploadUncertain("上传完成结果待核实，未重复上传，保留源文件") from error
        try:
            bucket.abort_multipart_upload(object_name, upload_id)
        except Exception:
            pass
        raise
