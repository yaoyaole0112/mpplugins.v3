"""Bounded, cancellable reads and sequential OSS upload without rapid-upload waiting."""

import base64
import time


class TransferStopped(InterruptedError):
    pass


class UploadUncertain(RuntimeError):
    pass


class SourceChanged(RuntimeError):
    pass


def network_detail(error):
    names = []
    current = error
    for unused in range(5):
        if not isinstance(current, BaseException) or type(current).__name__ in names:
            break
        names.append(type(current).__name__)
        current = (getattr(current, "exception", None) or current.__cause__
                   or current.__context__ or getattr(current, "reason", None))
    status = getattr(error, "status", None)
    return "/".join(names) + (f"，HTTP {status}" if isinstance(status, int) and 100 <= status <= 599 else "")


class UploadTransportError(RuntimeError):
    def __init__(self, stage, error, retryable=False):
        self.retryable = retryable
        super().__init__(f"{stage}失败（{network_detail(error)}）")


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
    from oss2.exceptions import RequestError, ServerError
    from oss2.http import Session
    from oss2.models import PartInfo
    from oss2.utils import SizedFileAdapter

    if stop.is_set():
        raise TransferStopped("插件已停止，未请求上传凭据")
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    data = {**response, **data}
    callback = data.get("callback") or {}
    if not all(data.get(field) for field in ("bucket", "object")) or not all(
            callback.get(field) for field in ("callback", "callback_var")):
        raise ValueError("115 未返回完整上传凭据，保留源文件；请稍后重新扫描")

    session = None
    bucket = None
    upload_id = None
    completing = False

    def make_bucket():
        nonlocal session
        try:
            token = request(lambda: client.upload_gettoken(timeout=30))
        except TransferStopped:
            raise
        except Exception as error:
            raise UploadTransportError("获取上传凭据", error, isinstance(
                error, (UploadUncertain, ConnectionError, TimeoutError))) from error
        if isinstance(token.get("data"), dict):
            token = {**token, **token["data"]}
        if not all(token.get(field) for field in ("AccessKeyId", "AccessKeySecret", "SecurityToken")):
            raise ValueError("115 上传凭据获取失败，保留源文件")
        endpoint = token.get("endpoint") or "https://oss-cn-shenzhen.aliyuncs.com"
        if endpoint.startswith("http://"):
            endpoint = "https://" + endpoint[7:]
        if session is not None:
            session.session.close()
        session = Session()
        return Bucket(StsAuth(token["AccessKeyId"], token["AccessKeySecret"], token["SecurityToken"]),
                      endpoint, data["bucket"], session=session, connect_timeout=60)

    def retry_request(stage, action):
        nonlocal session
        for attempt in range(3):
            if stop.is_set():
                raise TransferStopped("插件已停止，未提交上传完成")
            try:
                return action()
            except TransferStopped:
                raise
            except Exception as error:
                retryable = isinstance(error, (RequestError, ConnectionError, TimeoutError)) or (
                    isinstance(error, ServerError) and (
                        error.status in (408, 429) or error.status >= 500))
                if not retryable or attempt == 2:
                    raise UploadTransportError(stage, error, retryable) from error
                if stop.wait(2 ** (attempt + 1)):
                    raise TransferStopped("插件已停止，保留源文件")
                session.session.close()
                session = Session()
                bucket.session = session

    try:
        bucket = make_bucket()
        refreshed = time.monotonic()
        object_name = data["object"]
        upload_id = retry_request("初始化分片", lambda: bucket.init_multipart_upload(object_name)).upload_id
        size = path.stat().st_size
        part_size = determine_part_size(size, preferred_size=10 * 1024 * 1024)
        parts = []
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

                def send_part():
                    stream.seek(offset)

                    def part_progress(consumed, part_total):
                        progress(offset + min(amount, max(0, consumed)), size)

                    return bucket.upload_part(object_name, upload_id, len(parts) + 1,
                                              SizedFileAdapter(reader, amount), progress_callback=part_progress)

                result = retry_request(f"上传第{len(parts) + 1}分片", send_part)
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
            raise UploadUncertain(
                f"提交上传完成结果待核实（{network_detail(error)}），未重复上传，保留源文件") from error
        if bucket is not None and upload_id is not None:
            try:
                bucket.abort_multipart_upload(object_name, upload_id)
            except Exception:
                pass
        raise
    finally:
        if session is not None:
            session.session.close()
