import re
import time

import httpx

from .cloud_storage import CloudError, CloudStorage, open_stream


SAMPLE_SIZE = 64 * 1024


def _cloud_download_request(storage, item, sha1):
    if item.get("storage") != "CloudDrive储存":
        return storage.download_request(item)
    response = storage.oper.client.get_download_url(item["path"], get_direct_url=True)
    if response and response.directUrl:
        headers = dict(getattr(response, "additionalHeaders", {}) or {})
        headers.update({"User-Agent": response.userAgent or "Mozilla/5.0", "Accept-Encoding": "identity"})
        return response.directUrl, headers
    try:
        native = CloudStorage("115网盘Plus")
    except CloudError:
        raise CloudError("CloudDrive未返回云端直链；请启用并登录相同115账号的115插件进行独立回读核验，暂不转发、不删除源文件") from None
    from app.plugins.p115disk.tools import get_ios_ua_app
    result = native.oper.client.fs_file_skim(
        str(item["fileid"]), **{**get_ios_ua_app(app=False), "timeout": 30},
    )
    rows = result.get("data") if isinstance(result, dict) and result.get("state") is True else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise CloudError("115独立回读查询未确认，请检查115插件账号是否与CloudDrive一致")
    remote = rows[0]
    if (
        str(remote.get("file_id")) != str(item["fileid"])
        or str(remote.get("file_size")) != str(item["size"])
        or str(remote.get("sha1") or "").lower() != sha1.lower()
        or not remote.get("pick_code")
    ):
        raise CloudError("115独立回读文件身份、大小或SHA1未匹配，暂不转发、不删除源文件")
    return native.download_request({**item, "storage": "115网盘Plus", "pickcode": remote["pick_code"]})


def verify_cloud_readback(storage, item, path, sha1, stopping):
    size = item["size"]
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise CloudError("云端回读文件大小无效")
    try:
        if stopping.is_set():
            raise InterruptedError("插件停止，取消云端回读核验")
        url, headers = _cloud_download_request(storage, item, sha1)
        headers = {key: value for key, value in headers.items() if key.lower() not in {"range", "if-range"}}
        headers["Accept-Encoding"] = "identity"
        offsets = sorted({0, max(0, (size - SAMPLE_SIZE) // 2), max(0, size - SAMPLE_SIZE)})
        deadline = time.monotonic() + 90
        with path.open("rb") as local, httpx.Client(
            timeout=httpx.Timeout(30, connect=15, read=30), trust_env=False,
        ) as client:
            for start in offsets:
                if stopping.is_set():
                    raise InterruptedError("插件停止，取消云端回读核验")
                end = min(size - 1, start + SAMPLE_SIZE - 1)
                response = open_stream(client, url, {**headers, "Range": f"bytes={start}-{end}"}, stopping)
                try:
                    expected = end - start + 1
                    content_range = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if (response.status_code != 206 or not content_range
                            or tuple(map(int, content_range.groups())) != (start, end, size)):
                        raise CloudError(f"云端抽样未返回正确字节范围（HTTP {response.status_code}），不回退完整下载")
                    length = response.headers.get("Content-Length")
                    if length is not None and (not length.isdigit() or int(length) != expected):
                        raise CloudError("云端抽样响应长度与请求不一致")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise CloudError("云端抽样返回压缩传输内容，无法按字节核验")
                    local.seek(start)
                    received = 0
                    for chunk in response.iter_bytes(8192):
                        if stopping.is_set():
                            raise InterruptedError("插件停止，取消云端回读核验")
                        if time.monotonic() > deadline:
                            raise CloudError("云端抽样读取超过90秒，稍后限次重试")
                        received += len(chunk)
                        if received > expected or chunk != local.read(len(chunk)):
                            raise CloudError("云端实际内容与本地抽样不一致，暂不转发、不删除源文件")
                    if received != expected:
                        raise CloudError("云端抽样读取不足，暂不转发、不删除源文件")
                finally:
                    response.close()
    except (InterruptedError, CloudError):
        raise
    except Exception as failure:
        raise CloudError(f"云端实际读取暂未确认（{type(failure).__name__}），仅重试核验，不重复上传、不转发、不删除源文件") from None
