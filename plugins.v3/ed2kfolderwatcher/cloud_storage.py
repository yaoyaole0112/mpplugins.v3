import hashlib
import posixpath
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit

import httpx


SUPPORTED_STORAGES = {"115网盘Plus", "CloudDrive储存"}
READ_SIZE = 256 * 1024


class CloudError(ValueError):
    pass


def cloud_path(value):
    value = str(value or "").strip()
    if not value.startswith("/") or "\x00" in value or "\\" in value or ".." in PurePosixPath(value).parts:
        raise CloudError("网盘目录必须是以 / 开头的绝对路径，不能包含 .. 或反斜杠")
    return posixpath.normpath("/" + value.lstrip("/"))


def fingerprint(item):
    return [item.get(field) for field in ("storage", "fileid", "path", "name", "size", "modify_time")]


def fingerprint_matches(saved, item):
    return isinstance(saved, (list, tuple)) and len(saved) in {6, 7} and list(saved[:6]) == fingerprint(item)


def record_key(item):
    identity = [item["storage"], item.get("fileid") or item["path"]]
    return "cloud:" + hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()


def allowed_item(item, storage, root):
    if item.get("storage") != storage or item.get("type") not in {"file", "dir"}:
        return False
    try:
        path = PurePosixPath(cloud_path(item.get("path")))
        return path.is_relative_to(PurePosixPath(root))
    except CloudError:
        return False


def available_storages():
    from app.application.storage import StorageHelper
    return [{"type": entry.type, "name": entry.name or entry.type}
            for entry in StorageHelper.get_storagies() if entry.type in SUPPORTED_STORAGES]


class CloudStorage:
    def __init__(self, storage):
        if storage not in SUPPORTED_STORAGES:
            raise CloudError("当前只支持已配置的 115网盘Plus 和 CloudDrive储存")
        self.storage = storage
        from app.runtime.extensions.plugin.manager import PluginManager
        attribute = "_p115_api" if storage == "115网盘Plus" else "_clouddrive_api"
        self.oper = next((getattr(plugin, attribute, None)
                          for plugin in PluginManager().running_plugins.values()
                          if getattr(plugin, "_disk_name", None) == storage and getattr(plugin, attribute, None) is not None), None)
        if self.oper is None:
            raise CloudError("对应存储插件未就绪，请先启用并登录存储插件")

    def list(self, path):
        from app.schemas import FileItem
        root = cloud_path(path)
        result = self.oper.list(FileItem(storage=self.storage, path=root.rstrip("/") + "/", type="dir"))
        if result is None:
            raise CloudError("网盘目录读取失败，请检查存储登录状态和目录")
        items = [entry.model_dump() if hasattr(entry, "model_dump") else dict(entry) for entry in result]
        return [entry for entry in items if allowed_item(entry, self.storage, root) and cloud_path(entry["path"]) != root]

    def fresh(self, item):
        parent = PurePosixPath(item["path"]).parent.as_posix()
        matches = [entry for entry in self.list(parent)
                   if entry.get("fileid") == item.get("fileid") and cloud_path(entry.get("path")) == cloud_path(item.get("path"))]
        if len(matches) != 1:
            raise CloudError("网盘文件已移动、删除或查询结果不唯一，保留记录等待重试")
        return matches[0]

    def walk(self, root, recursive, stopping):
        root = cloud_path(root)
        pending = [(root, 0)]
        visited = set()
        while pending:
            if stopping.is_set():
                raise InterruptedError("任务已停止")
            path, depth = pending.pop()
            if path in visited:
                continue
            visited.add(path)
            if depth > 64:
                raise CloudError("网盘目录层级超过 64 层，请缩小扫描范围")
            for item in self.list(path):
                if stopping.is_set():
                    raise InterruptedError("任务已停止")
                if not allowed_item(item, self.storage, root):
                    continue
                if item["type"] == "dir" and recursive:
                    pending.append((cloud_path(item["path"]), depth + 1))
                elif item["type"] == "file":
                    yield item
            yield None
            if stopping.wait(.1):
                raise InterruptedError("任务已停止")

    def download_request(self, item):
        from app.core.config import settings
        headers = {"User-Agent": settings.USER_AGENT, "Accept-Encoding": "identity"}
        if self.storage == "115网盘Plus":
            if not item.get("pickcode"):
                raise CloudError("115 文件缺少 pickcode，无法读取")
            result = self.oper.client.download_url(item["pickcode"], user_agent=settings.USER_AGENT)
            url = result.geturl() if result else None
            extra = getattr(result, "headers", None)
            if isinstance(extra, dict):
                headers.update(extra)
        else:
            result = self.oper.client.get_download_url(item["path"], get_direct_url=True)
            url = None
            if result and result.directUrl:
                url = result.directUrl
                headers["User-Agent"] = result.userAgent or settings.USER_AGENT
                headers.update({key: value for key, value in (getattr(result, "additionalHeaders", None) or {}).items() if key and value})
            elif result and result.downloadUrlPath:
                base = self.oper._download_base.rstrip("/")
                host = urlsplit(base).netloc
                suffix = result.downloadUrlPath.replace("{SCHEME}", urlsplit(base).scheme).replace("{HOST}", host).replace("{PREVIEW}", "false")
                url = base + suffix
        if not url or urlsplit(str(url)).scheme not in {"http", "https"} or not urlsplit(str(url)).hostname:
            raise CloudError("存储未返回可用 HTTP 下载地址，不会退回完整文件落盘下载")
        headers["Accept-Encoding"] = "identity"
        return str(url), headers


def hash_stream(chunks, size, stopping, md4, part_size, progress=None, sha1=None):
    if not isinstance(size, int) or size <= 0:
        raise CloudError("空文件或未知文件大小，拒绝生成 ED2K")
    buffer = bytearray()
    parts = []
    total = 0
    checksum = hashlib.sha1()
    for chunk in chunks:
        if stopping.is_set():
            raise InterruptedError("任务已停止")
        if total + len(chunk) > size:
            raise CloudError("网盘读取超出文件大小，拒绝生成 ED2K")
        total += len(chunk)
        checksum.update(chunk)
        offset = 0
        while offset < len(chunk):
            count = min(part_size - len(buffer), len(chunk) - offset)
            buffer.extend(chunk[offset:offset + count])
            offset += count
            if len(buffer) == part_size:
                parts.append(md4(bytes(buffer)))
                buffer.clear()
        if progress:
            progress(total, size)
    if stopping.is_set():
        raise InterruptedError("任务已停止")
    if total != size:
        raise CloudError("网盘读取字节数不足，拒绝生成 ED2K")
    if sha1 and checksum.hexdigest().lower() != sha1.lower():
        raise CloudError("网盘 SHA1 校验不一致，拒绝生成 ED2K")
    parts.append(md4(bytes(buffer)))
    digest = parts[0] if len(parts) == 1 else md4(b"".join(parts))
    return digest.hex()


def hash_remote(storage, item, stopping, md4, part_size, progress=None):
    url, headers = storage.download_request(item)
    try:
        with httpx.Client(timeout=httpx.Timeout(30, connect=15, read=120), trust_env=False) as client:
            request_headers = dict(headers)
            for _ in range(6):
                if stopping.is_set():
                    raise InterruptedError("任务已停止")
                response = client.send(client.build_request("GET", url, headers=request_headers), stream=True)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise CloudError("网盘下载重定向缺少地址")
                next_url = urljoin(url, location)
                current_origin = urlsplit(url)
                next_origin = urlsplit(next_url)
                if next_origin.scheme not in {"http", "https"} or not next_origin.hostname:
                    raise CloudError("网盘下载地址重定向到不安全地址")
                if current_origin.scheme == "https" and next_origin.scheme != "https":
                    raise CloudError("网盘下载地址不允许从 HTTPS 降级到 HTTP")
                if (current_origin.scheme, current_origin.hostname, current_origin.port) != (next_origin.scheme, next_origin.hostname, next_origin.port):
                    request_headers = {key: value for key, value in request_headers.items() if key.lower() not in {"authorization", "cookie", "proxy-authorization"}}
                url = next_url
            else:
                raise CloudError("网盘下载重定向次数过多")
            try:
                response.raise_for_status()
                if response.status_code != 200 or response.headers.get("Content-Range"):
                    raise CloudError("存储未返回完整文件响应，拒绝生成 ED2K")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) != item["size"]):
                    raise CloudError("下载响应大小与网盘记录不一致，拒绝生成 ED2K")
                content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if content_type in {"text/html", "application/json", "application/xml", "text/xml"} and PurePosixPath(item["path"]).suffix.lower() not in {".html", ".htm", ".json", ".xml"}:
                    raise CloudError("存储返回登录页或错误内容，不是原文件")
                digest = hash_stream(response.iter_bytes(READ_SIZE), item["size"], stopping, md4, part_size, progress, item.get("sha1"))
            finally:
                response.close()
    except InterruptedError:
        raise
    except (httpx.HTTPError, OSError) as failure:
        raise CloudError(f"网盘读取失败（{type(failure).__name__}），不保存临时下载文件，稍后重试") from None
    if fingerprint(storage.fresh(item)) != fingerprint(item):
        raise CloudError("计算期间网盘文件状态变化，拒绝保存或发送链接")
    return digest
