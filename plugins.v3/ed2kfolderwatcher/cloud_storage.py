import hashlib
import importlib
import json
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


def transient_read_error(error):
    classes = [TimeoutError, ConnectionError, httpx.TransportError]
    for module, names in (
        ("urllib3.exceptions", ("TimeoutError", "ProtocolError", "NewConnectionError")),
        ("urllib3_future.exceptions", ("TimeoutError", "ProtocolError", "NewConnectionError")),
        ("requests.exceptions", ("Timeout", "ConnectionError")),
    ):
        try:
            imported = importlib.import_module(module)
        except ImportError:
            continue
        classes.extend(getattr(imported, name) for name in names)
    seen = set()
    while isinstance(error, BaseException) and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, CloudError):
            return False
        if isinstance(error, tuple(classes)):
            return True
        error = getattr(error, "reason", None) or error.__cause__ or error.__context__
    return False


def cloud_path(value):
    value = str(value or "").strip()
    if not value.startswith("/") or "\x00" in value or "\\" in value or ".." in PurePosixPath(value).parts:
        raise CloudError("网盘目录必须是以 / 开头的绝对路径，不能包含 .. 或反斜杠")
    return posixpath.normpath("/" + value.lstrip("/"))


def monitor_roots(value, *, cloud=False, recursive=False):
    lines = str(value or "").splitlines()
    roots = []
    if len(str(value or "")) > 32768:
        raise ValueError("监控目录配置过长")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if cloud:
            root = cloud_path(line)
            if root == "/":
                raise CloudError("请指定网盘子目录，不允许监控整个网盘根目录")
        else:
            path = Path(line).expanduser()
            if not path.is_absolute() or "\x00" in line or ".." in path.parts:
                raise ValueError("本地监控目录必须是绝对路径，不能包含 .. 或空字符")
            root = str(path)
        if root not in roots:
            roots.append(root)
    if len(roots) > 32:
        raise ValueError("本地和网盘分别最多支持32个监控目录")
    if recursive:
        roots = [root for root in roots if not any(
            root != parent and PurePosixPath(root).is_relative_to(PurePosixPath(parent))
            for parent in roots)]
    return roots


def allowed_roots(item, storage, roots, recursive=True):
    return any(allowed_item(item, storage, root) and (
        recursive or PurePosixPath(cloud_path(item.get("path"))).parent.as_posix() == root
    ) for root in monitor_roots(roots, cloud=True))


def fingerprint(item):
    result = [item.get(field) for field in ("storage", "fileid", "path", "name", "size", "modify_time")]
    if result[1] is not None:
        result[1] = str(result[1])
    return result


def fingerprint_matches(saved, item):
    if not isinstance(saved, (list, tuple)) or len(saved) not in {6, 7}:
        return False
    previous = list(saved[:6])
    if previous[1] is not None:
        previous[1] = str(previous[1])
    return previous == fingerprint(item)


def record_key(item):
    identity = [item["storage"], str(item["fileid"]) if item.get("fileid") else item["path"]]
    return "cloud:" + hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()


def legacy_record_key(item):
    identity = str(item.get("fileid") or "")
    if not identity.isascii() or not identity.isdigit() or len(identity) > 64 or str(int(identity)) != identity:
        return None
    return "cloud:" + hashlib.sha256(repr([item["storage"], int(identity)]).encode("utf-8")).hexdigest()


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

    def delete_scope(self, root, recursive):
        roots = sorted(monitor_roots(root, cloud=True))
        if self.storage != "115网盘Plus" or not roots:
            raise CloudError("自动删源仅支持115网盘Plus的指定子目录，CloudDrive仍只读")
        account = getattr(self.oper.client, "user_id", None)
        if not isinstance(account, (str, int)) or isinstance(account, bool) or not re.fullmatch(r"[1-9][0-9]*", str(account)):
            raise CloudError("无法确认115存储账号，拒绝授权或删除")
        payload = [self.storage, str(account), roots[0] if len(roots) == 1 else roots, bool(recursive)]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()

    def prepare_delete(self, item, sha1, root, recursive, scope):
        if not scope or self.delete_scope(root, recursive) != scope:
            raise CloudError("网盘删源授权与当前账号或目录不一致，请重新授权")
        if not re.fullmatch(r"[1-9][0-9]{0,63}", str(item.get("fileid") or "")) or item.get("type") != "file":
            raise CloudError("仅允许按有效文件ID删除单个115文件")
        if not isinstance(sha1, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha1):
            raise CloudError("缺少已验证SHA1，保留网盘源文件")
        current = self.fresh(item)
        if not allowed_roots(current, self.storage, root) or current.get("type") != "file" or not fingerprint_matches(fingerprint(item), current):
            raise CloudError("网盘文件身份、大小或路径变化，拒绝删除")
        if not allowed_roots(current, self.storage, root, recursive):
            raise CloudError("网盘文件位于未授权的子目录，拒绝删除")
        if self.file_sha1(current) != sha1.lower():
            raise CloudError("网盘文件SHA1变化或缺失，拒绝删除")
        if self.delete_path(current) != cloud_path(item["path"]):
            raise CloudError("115文件当前路径已变化，拒绝删除")
        return current

    def delete_path(self, item):
        from p115client.tool.attr import get_path
        return cloud_path(get_path(client=self.oper.client, attr=str(item["fileid"]), root_id=None, ensure_file=True, refresh=True, app="android", timeout=30))

    def file_sha1(self, item):
        if self.storage != "115网盘Plus" or item.get("type") != "file" or not re.fullmatch(r"[1-9][0-9]{0,63}", str(item.get("fileid") or "")):
            raise CloudError("只能校验有效115文件的SHA1")
        from app.plugins.p115disk.tools import get_ios_ua_app
        response = self.oper.client.fs_file_skim(str(item["fileid"]), **{**get_ios_ua_app(app=False), "timeout": 30})
        if not isinstance(response, dict) or response.get("state") is not True:
            raise CloudError("115文件SHA1查询未确认成功")
        rows = response.get("data")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or str(rows[0].get("file_id")) != str(item["fileid"]):
            raise CloudError("115文件SHA1查询身份不一致")
        digest = rows[0].get("sha1")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", digest):
            raise CloudError("115文件缺少有效SHA1")
        return digest.lower()

    def delete_prepared(self, item, root, recursive, scope, stopping):
        if stopping.is_set() or self.delete_scope(root, recursive) != scope:
            raise CloudError("插件停止或授权变化，取消网盘删除")
        if item.get("type") != "file" or not allowed_roots(item, self.storage, root) or not re.fullmatch(r"[1-9][0-9]{0,63}", str(item.get("fileid") or "")):
            raise CloudError("删除对象不是已授权的单个文件")
        if not allowed_roots(item, self.storage, root, recursive):
            raise CloudError("删除对象位于未授权的子目录")
        from app.plugins.p115disk.tools import get_ios_ua_app
        limiter = getattr(self.oper, "_delete_rate_limiter", None)
        if limiter is None:
            raise CloudError("115存储缺少删除限速器，拒绝发送删除请求")
        limiter.acquire()
        if stopping.is_set() or self.delete_scope(root, recursive) != scope:
            raise CloudError("等待限速期间停止或授权变化，取消删除")
        if self.delete_path(item) != cloud_path(item["path"]):
            raise CloudError("限速等待后文件已移动，取消删除")
        if stopping.is_set():
            raise CloudError("插件停止，取消删除")
        result = self.oper.client.fs_delete(str(item["fileid"]), **{**get_ios_ua_app(app=False), "timeout": 30})
        if not isinstance(result, dict) or not isinstance(result.get("state"), bool):
            raise CloudError("115删除响应状态未知，不自动重试")
        if result["state"] is False:
            return False
        for name in ("_id_cache", "_id_item_cache"):
            cache = getattr(self.oper, name, None)
            if cache is not None:
                try:
                    cache.remove(int(item["fileid"]))
                except Exception:
                    pass
        return True

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
                   if str(entry.get("fileid")) == str(item.get("fileid")) and item.get("fileid") not in (None, "") and cloud_path(entry.get("path")) == cloud_path(item.get("path"))]
        if len(matches) != 1:
            raise CloudError("网盘文件已移动、删除或查询结果不唯一，保留记录等待重试")
        return matches[0]

    def walk(self, root, recursive, stopping):
        from .file_order import filename_order
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
            children = []
            for item in sorted(self.list(path), key=lambda item: filename_order(item.get("name") or item["path"])):
                if stopping.is_set():
                    raise InterruptedError("任务已停止")
                if not allowed_item(item, self.storage, root):
                    continue
                if item["type"] == "dir" and recursive:
                    children.append((cloud_path(item["path"]), depth + 1))
                elif item["type"] == "file":
                    yield item
            pending.extend(reversed(children))
            yield None
            if stopping.wait(.1):
                raise InterruptedError("任务已停止")

    def life_events(self, source, after_id=None, since_time=None):
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
                latest = session.scalar(select(model.update_time).order_by(model.id.desc()).limit(1))
                self.life_source_latest = float(latest) if latest else None
                query = select(model)
                if after_id is not None:
                    query = query.where(model.id > after_id)
                if since_time is not None:
                    query = query.where(model.update_time >= since_time)
                query = query.order_by(model.id.asc() if after_id is not None else model.id.desc()).limit(64)
                return [{field: getattr(row, field, None) for field in LIFE_EVENT_FIELDS}
                        for row in session.scalars(query)]
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
