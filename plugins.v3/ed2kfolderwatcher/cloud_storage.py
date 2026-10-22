import hashlib
import posixpath
import re
import sys
from contextlib import closing
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

import httpx


SUPPORTED_STORAGES = {"115网盘Plus", "CloudDrive储存"}
READ_SIZE = 256 * 1024
LIFE_EVENT_FIELDS = (
    "id", "type", "file_id", "parent_id", "file_name", "file_category",
    "file_type", "file_size", "sha1", "pick_code", "update_time", "create_time",
)


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

    def life_events(self, source):
        if self.storage != "115网盘Plus":
            raise CloudError("115生活事件仅支持115网盘Plus；CloudDrive请使用定时补扫")
        if source == "shared":
            service = sys.modules.get("app.plugins.p115strmhelper.service")
            database = sys.modules.get("app.plugins.p115strmhelper.db_manager")
            models = sys.modules.get("app.plugins.p115strmhelper.db_manager.models.life_event")
            worker = getattr(service, "servicer", None)
            thread = getattr(worker, "monitor_life_thread", None)
            if not thread or not thread.is_alive() or not database or not models:
                raise CloudError("未检测到运行中的115插件生活事件监听；请先开启该监听，或手动选择独立轮询")
            shared_user = getattr(getattr(worker, "client", None), "user_id", None)
            own_user = getattr(self.oper.client, "user_id", None)
            if not shared_user or not own_user or str(shared_user) != str(own_user):
                raise CloudError("115生活事件监听与当前存储账号不一致，不能复用")
            from sqlalchemy import select
            model = models.LifeEvent
            with database.ct_db_manager.get_database().session() as session:
                return [{field: getattr(row, field, None) for field in LIFE_EVENT_FIELDS}
                        for row in session.scalars(select(model).order_by(model.id.desc()).limit(64))]
        if source != "direct":
            raise CloudError("生活事件来源无效")
        result = self.oper.client.life_behavior_detail({"limit": 64, "offset": 0})
        if not isinstance(result, dict) or result.get("state") is False or not isinstance(result.get("data"), dict):
            raise CloudError("115生活事件读取失败，请检查登录状态或稍后重试")
        events = result["data"].get("list")
        if not isinstance(events, list) or any(not isinstance(entry, dict) for entry in events):
            raise CloudError("115生活事件响应格式无效")
        return [{key: entry.get(key) for key in LIFE_EVENT_FIELDS} for entry in events[:64]]

    def item_from_life_event(self, event):
        if self.storage != "115网盘Plus":
            raise CloudError("115生活事件仅支持115网盘Plus")
        try:
            if int(event.get("file_category")) == 0:
                return None
            file_id = str(int(event["file_id"]))
        except (KeyError, TypeError, ValueError):
            return None
        try:
            from p115client.tool.attr import get_path
            path = cloud_path(get_path(
                client=self.oper.client,
                attr=file_id,
                root_id=None,
                ensure_file=True,
                refresh=True,
                app="android",
            ))
        except FileNotFoundError:
            return None
        except Exception as failure:
            raise CloudError(f"生活事件文件路径查询失败（{type(failure).__name__}），保留候选等待退避复查") from None
        try:
            size = int(event.get("file_size") or 0)
            modify_time = int(event.get("update_time") or event.get("create_time") or 0)
        except (TypeError, ValueError):
            size = modify_time = 0
        item = {
            "storage": self.storage,
            "fileid": file_id,
            "type": "file",
            "path": path,
            "name": PurePosixPath(path).name,
            "size": size,
            "modify_time": modify_time,
            "pickcode": str(event.get("pick_code") or ""),
            "sha1": str(event.get("sha1") or ""),
        }
        if item["size"] > 0 and item["pickcode"]:
            return item
        try:
            lookup = getattr(self.oper, "get_item_strict", None) or self.oper.get_item
            current = lookup(Path(path))
            current = current.model_dump() if hasattr(current, "model_dump") else dict(current) if current else None
        except Exception as failure:
            raise CloudError(f"生活事件文件详情查询失败（{type(failure).__name__}），保留候选等待退避复查") from None
        if not current or current.get("type") != "file" or str(current.get("fileid") or "") != file_id:
            return None
        current.update(storage=self.storage, fileid=file_id, type="file", path=path,
                       name=current.get("name") or item["name"])
        if item["sha1"]:
            current["sha1"] = item["sha1"]
        return current

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


def open_stream(client, url, headers, stopping):
    request_headers = dict(headers)
    for _ in range(6):
        if stopping.is_set():
            raise InterruptedError("任务已停止")
        response = client.send(client.build_request("GET", url, headers=request_headers), stream=True)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
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
    raise CloudError("网盘下载重定向次数过多")


def remote_chunks(storage, item, stopping, read_timeout, on_retry):
    offset = 0
    validator = None
    with httpx.Client(timeout=httpx.Timeout(30, connect=15, read=read_timeout), trust_env=False) as client:
        for attempt in range(3):
            if attempt:
                if on_retry:
                    on_retry(attempt)
                if stopping.wait(5 * attempt):
                    raise InterruptedError("任务已停止")
                fresh = storage.fresh(item)
                if fingerprint(fresh) != fingerprint(item):
                    raise CloudError("断线后网盘文件状态变化，拒绝续读")
                item = {**fresh, "sha1": item.get("sha1")}
            url, headers = storage.download_request(item)
            headers = {key: value for key, value in headers.items() if key.lower() not in {"range", "if-range"}}
            if offset:
                headers["Range"] = f"bytes={offset}-"
                if validator:
                    headers["If-Range"] = validator
            try:
                response = open_stream(client, url, headers, stopping)
                try:
                    response.raise_for_status()
                    if offset:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                        if response.status_code != 206 or not match or tuple(map(int, match.groups())) != (offset, item["size"] - 1, item["size"]):
                            raise CloudError("下载服务不支持正确的断点续读，已停止，绝不拼接重复数据")
                        if validator and response.headers.get("ETag") != validator:
                            raise CloudError("断点续读文件校验标识变化，拒绝生成 ED2K")
                    elif response.status_code != 200 or response.headers.get("Content-Range"):
                        raise CloudError("存储未返回完整文件响应，拒绝生成 ED2K")
                    length = response.headers.get("Content-Length")
                    if length is not None and (not length.isdigit() or int(length) != item["size"] - offset):
                        raise CloudError("下载响应大小与网盘记录不一致，拒绝生成 ED2K")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise CloudError("下载返回压缩传输内容，拒绝按字节偏移续读")
                    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                    if content_type in {"text/html", "application/json", "application/xml", "text/xml"} and PurePosixPath(item["path"]).suffix.lower() not in {".html", ".htm", ".json", ".xml"}:
                        raise CloudError("存储返回登录页或错误内容，不是原文件")
                    if not offset:
                        candidate = response.headers.get("ETag", "")
                        validator = candidate if candidate.startswith('"') and candidate.endswith('"') else None
                    for chunk in response.iter_bytes(READ_SIZE):
                        offset += len(chunk)
                        yield chunk
                    return
                finally:
                    response.close()
            except (httpx.TransportError, OSError) as failure:
                if attempt == 2 or (offset and not validator and not item.get("sha1")):
                    reason = "；服务未提供强校验标识，不能安全续读" if offset and not validator and not item.get("sha1") else "；已达到本次重连上限"
                    raise CloudError(f"网盘读取失败（{type(failure).__name__}）{reason}，保留记录并退避重试") from None
            except httpx.HTTPStatusError as failure:
                raise CloudError(f"网盘下载返回 HTTP {failure.response.status_code}，请检查登录状态、权限或服务限流") from None


def hash_remote(storage, item, stopping, md4, part_size, progress=None, read_timeout=300, on_retry=None):
    with closing(remote_chunks(storage, item, stopping, read_timeout, on_retry)) as chunks:
        digest = hash_stream(chunks, item["size"], stopping, md4, part_size, progress, item.get("sha1"))
    if fingerprint(storage.fresh(item)) != fingerprint(item):
        raise CloudError("计算期间网盘文件状态变化，拒绝保存或发送链接")
    return digest
