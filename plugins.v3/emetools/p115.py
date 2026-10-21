"""115 web API operations used by this plugin, independent of EME."""

import hashlib
import math
import threading
import time

import httpx

from app.sdk.logging import logger


API = "https://webapi.115.com"


class P115DirectoryUnavailable(RuntimeError):
    def __init__(self, message, retryable=False, exhausted=False):
        super().__init__(message)
        self.retryable = retryable
        self.exhausted = exhausted


class P115RequestGate:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_finished = None
        self.retry_at = 0
        self.failures = 0
        self.list_endpoint = 0
        self.retry_reads = False

    def check(self):
        remaining = self.remaining()
        if remaining > 0:
            raise P115DirectoryUnavailable(f"115 请求已暂停，约 {remaining} 秒后复查；本轮不再提交请求",
                                          retryable=self.retry_reads)

    def remaining(self):
        return max(0, math.ceil(self.retry_at - time.monotonic()))

    def defer(self, minimum=300, retry_after=0, retry_reads=False):
        self.failures += 1
        delay = min(3600, max(minimum * 2 ** min(self.failures - 1, 4), retry_after))
        self.retry_at = time.monotonic() + delay
        self.retry_reads = retry_reads
        return delay


_REQUEST_GATES = {}
_REQUEST_GATES_LOCK = threading.Lock()


def _request_gate(cookie):
    account = hashlib.sha256(cookie.strip().encode()).digest()
    with _REQUEST_GATES_LOCK:
        return _REQUEST_GATES.setdefault(account, P115RequestGate())


LIST_ENDPOINTS = (
    ("https://115cdn.com/webapi/files", {"aid": 1, "o": "user_ptime", "asc": 0, "show_dir": 1,
                                         "fc_mix": 0, "format": "json", "v": "4.6", "natsort": 1}),
    ("https://proapi.115.com/android/2.0/ufile/files", {"aid": 1, "count_folders": 1,
                                                          "record_open_time": 1, "show_dir": 1,
                                                          "format": "json", "app_ver": "36.2.28"}),
    ("https://aps.115.com/natsort/files.php", {"aid": 1, "o": "user_ptime", "asc": 0,
                                              "show_dir": 1, "fc_mix": 0, "format": "json", "natsort": 1}),
)


class P115Client:
    def __init__(self, cookie: str):
        if not cookie.strip():
            raise ValueError("请先在 115 网盘 STRM 助手中配置 Cookie")
        self._gate = _request_gate(cookie)
        self._recover_reads = False
        self._stop_event = None
        self._headers = {"Cookie": cookie.strip(), "Referer": "https://115.com/",
                         "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125.0 Safari/537.36"}
        self.client = self._new_connection()

    def _new_connection(self):
        return httpx.Client(
            headers=self._headers,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            timeout=20, follow_redirects=True,
        )

    def enable_read_recovery(self, stop_event=None):
        self._recover_reads = True
        self._stop_event = stop_event

    @staticmethod
    def read_recovery_available_for_cookie(cookie):
        return bool(str(cookie or "").strip()) and _request_gate(cookie).retry_reads

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.client.close()

    @staticmethod
    def cooldown_remaining_for_cookie(cookie: str) -> int:
        """Return the shared cooldown without opening a network connection."""
        if not str(cookie or "").strip():
            return 0
        return _request_gate(cookie).remaining()

    def cooldown_remaining(self) -> int:
        return self._gate.remaining()

    def _request(self, method, url, **kwargs):
        attempts = 3 if self._recover_reads and method.upper() in {"GET", "HEAD"} else 1
        if attempts > 1:
            kwargs.setdefault("timeout", httpx.Timeout(45, connect=15, write=20, pool=20))
        deadline = time.monotonic() + 240
        for attempt in range(attempts):
            if self._stop_event is not None and self._stop_event.is_set():
                raise P115DirectoryUnavailable("清理任务已停止，未继续读取或提交清理")
            try:
                return self._request_once(method, url, **kwargs)
            except P115DirectoryUnavailable as error:
                if attempts == 1 or not error.retryable:
                    raise
                if attempt == attempts - 1:
                    raise P115DirectoryUnavailable("115 本轮读取恢复已尝试 3 次，仍未恢复；未重放任何写入请求",
                                                  retryable=True, exhausted=True) from None
                logger.info("媒体增强 115：本轮只读恢复 %d/2，等待共享退避 %d 秒后重新连接；不重放写入",
                            attempt + 1, self._gate.remaining())
                while self._gate.remaining():
                    if self._stop_event is not None and self._stop_event.is_set():
                        raise P115DirectoryUnavailable("清理任务已停止，未继续读取或提交清理")
                    if not self._gate.retry_reads:
                        self._gate.check()
                    if time.monotonic() >= deadline:
                        raise P115DirectoryUnavailable("115 本轮只读恢复已等待 240 秒，仍未恢复；未重放任何写入请求",
                                                      retryable=True, exhausted=True)
                    time.sleep(min(1, self._gate.remaining()))
                self.client.close()
                self.client = self._new_connection()
                for position, (list_url, defaults) in enumerate(LIST_ENDPOINTS):
                    if url == list_url:
                        url, defaults = LIST_ENDPOINTS[(position + 1) % len(LIST_ENDPOINTS)]
                        original = kwargs.get("params") or {}
                        kwargs = {**kwargs, "params": {**defaults, **{key: original[key] for key in
                                  ("cid", "limit", "offset") if key in original}}}
                        break

    def _request_once(self, method, url, **kwargs):
        endpoint = httpx.URL(url)
        params = kwargs.get("params") or {}
        cid = str(params.get("cid") or "")
        safe_cid = cid if cid.isdecimal() else "-"
        with self._gate.lock:
            self._gate.check()
            if self._gate.last_finished is not None:
                delay = 1 - (time.monotonic() - self._gate.last_finished)
                if delay > 0:
                    time.sleep(delay)
            if self._stop_event is not None and self._stop_event.is_set():
                raise P115DirectoryUnavailable("清理任务已停止，未继续读取或提交清理")
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.RequestError as error:
                read_only = method.upper() in {"GET", "HEAD"}
                delay = self._gate.defer(minimum=60, retry_reads=read_only)
                logger.warning("媒体增强 115：%s %s%s CID=%s 网络异常=%s，共享退避 %d 秒；不重放提交",
                               method, endpoint.host, endpoint.path, safe_cid, type(error).__name__, delay)
                reason = "目录或信息读取失败，未提交修改" if read_only else "提交结果可能不确定，请先核实，本轮不自动重试"
                raise P115DirectoryUnavailable(
                    f"115 网络请求失败（{type(error).__name__}），已暂停请求 {delay} 秒；{reason}",
                    retryable=read_only) from None
            finally:
                self._gate.last_finished = time.monotonic()
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            code = str(payload.get("errNo") or payload.get("errno") or payload.get("code") or "") \
                if isinstance(payload, dict) else ""
            expired = isinstance(payload, dict) and not payload.get("state") and code in {"990001", "99", "40101032"}
            if expired or response.status_code in (401, 403, 405, 429):
                retry_after = response.headers.get("Retry-After", "")
                delay = self._gate.defer(retry_after=int(retry_after) if retry_after.isdecimal() else 0)
                if not expired:
                    for position, (list_url, defaults) in enumerate(LIST_ENDPOINTS):
                        if url == list_url:
                            self._gate.list_endpoint = (position + 1) % len(LIST_ENDPOINTS)
                            break
                reason = "登录状态失效（115 明确返回认证错误码）" if expired else \
                    f"接口访问受限（HTTP {response.status_code}），不代表已确认 Cookie 失效"
                logger.warning("媒体增强 115：%s %s%s CID=%s HTTP=%d；%s；共享退避 %d 秒，停止切换接口",
                               method, endpoint.host, endpoint.path, safe_cid, response.status_code, reason, delay)
                raise P115DirectoryUnavailable(f"115 {reason}；已暂停请求 {delay} 秒，本轮停止")
            if response.is_error:
                logger.warning("媒体增强 115：%s %s%s CID=%s HTTP=%d",
                               method, endpoint.host, endpoint.path, safe_cid, response.status_code)
                raise RuntimeError(f"115 接口请求失败：HTTP {response.status_code}，未自动重放提交")
            if self._gate.failures:
                logger.info("媒体增强 115：退避后请求恢复，%s %s%s HTTP=%d",
                            method, endpoint.host, endpoint.path, response.status_code)
            self._gate.failures = 0
            self._gate.retry_at = 0
            self._gate.retry_reads = False
            for position, (list_url, defaults) in enumerate(LIST_ENDPOINTS):
                if url == list_url:
                    self._gate.list_endpoint = position
                    break
            logger.debug("媒体增强 115：%s %s%s CID=%s HTTP=%d",
                         method, endpoint.host, endpoint.path, safe_cid, response.status_code)
            return response

    @staticmethod
    def _parse_entries(response: dict):
        data = response.get("data") or []
        if isinstance(data, dict):
            data = data.get("data") or data.get("list") or []
        if not isinstance(data, list):
            raise ValueError("115 列表返回格式异常")
        count = response.get("count")
        total = int(count) if count not in (None, "") else None
        files, directories = [], []
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("115 列表包含无法识别的条目")
            name = item.get("name") or item.get("file_name") or item.get("fn") or item.get("n") or ""
            fid = str(item.get("fid") or "")
            cid = str(item.get("cid") or item.get("id") or "")
            category = str(item.get("fc") or "")
            if category:
                is_file = category != "0"
            else:
                kind = str(item.get("category") or item.get("file_category") or "").lower()
                is_file = kind in ("file", "1") or (fid and (not cid or cid == "0"))
            size = int(item.get("fs") or item.get("s") or item.get("size") or item.get("file_size") or 0)
            if is_file and fid:
                files.append({"fid": fid, "name": name, "size": size})
            elif not is_file:
                directory_id = cid if cid and cid != "0" else fid
                if directory_id and directory_id != "0":
                    directories.append({"cid": directory_id, "name": name, "size": size})
                else:
                    raise ValueError("115 文件夹缺少 CID")
            else:
                raise ValueError("115 文件缺少 FID")
        return files, directories, total, len(data)

    def _page(self, cid: str, offset: int):
        last_error = "目录接口异常或账号被风控"
        with self._gate.lock:
            if not (self._recover_reads and self._gate.retry_reads):
                self._gate.check()
            start = self._gate.list_endpoint
        endpoints = LIST_ENDPOINTS[start:] + LIST_ENDPOINTS[:start]
        for url, defaults in endpoints:
            try:
                params = {**defaults, "cid": cid, "limit": 1000, "offset": offset}
                response = self._request("GET", url, params=params)
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("115 目录接口未返回对象")
                if not data.get("state"):
                    last_error = "目录接口返回业务错误"
                    continue
                files, directories, total, length = self._parse_entries(data)
                if not files and not directories and (total is None or total != 0):
                    last_error = "目录接口返回不完整的空列表"
                    continue
                if total is None and length >= 1000:
                    last_error = "目录接口缺少分页总数"
                    continue
                return files, directories, total, length
            except (ValueError, TypeError) as error:
                last_error = type(error).__name__
        raise RuntimeError("115 目录读取失败，未对内容作任何改动：" + last_error[:160])

    def list_children(self, cid: str = "0"):
        files, directories, offset = [], [], 0
        while True:
            batch_files, batch_dirs, total, length = self._page(str(cid or "0"), offset)
            files.extend(batch_files)
            directories.extend(batch_dirs)
            offset += length
            if total is None or offset >= total:
                break
            if not length:
                raise RuntimeError("115 目录分页不完整，已停止操作")
        return files, directories

    def list_files_in_dir(self, cid: str):
        queue = [str(cid)]
        visited = set()
        files = []
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            batch, children = self.list_children(current)
            files.extend(batch)
            queue.extend(item["cid"] for item in children)
        return files

    def directories(self, cid: str = "0"):
        _, directories = self.list_children(cid)
        return directories

    def delete_files(self, identifiers: list) -> dict:
        if not identifiers:
            return {"state": False, "msg": "无文件 ID"}
        response = self._request("POST", f"{API}/rb/delete", data={f"fid[{index}]": str(identifier)
                                                                  for index, identifier in enumerate(identifiers)})
        return response.json()

    def move_files(self, identifiers: list, destination: str) -> dict:
        if not identifiers:
            return {"state": False, "msg": "无文件 ID"}
        fields = {"pid": str(destination)}
        fields.update({f"fid[{index}]": str(identifier) for index, identifier in enumerate(identifiers)})
        response = self._request("POST", f"{API}/files/move", data=fields, timeout=30)
        return response.json()

    def copy_files(self, identifiers: list, destination: str) -> dict:
        if not identifiers:
            return {"state": False, "msg": "无文件 ID"}
        fields = {"pid": str(destination)}
        fields.update({f"fid[{index}]": str(identifier) for index, identifier in enumerate(identifiers)})
        response = self._request("POST", f"{API}/files/copy", data=fields, timeout=30)
        return response.json()

    def rb_list(self, limit: int = 1000) -> dict:
        response = self._request("GET", f"{API}/rb", params={"aid": 7, "cid": 0, "limit": limit, "offset": 0})
        result = response.json()
        if not result.get("state"):
            raise RuntimeError(str(result.get("error") or result.get("msg") or "115 回收站查询失败"))
        return {"count": int(result.get("count") or 0), "items": result.get("data") or []}

    def clear_recyclebin(self, password: str = "000000") -> dict:
        response = self._request("POST", f"{API}/rb/secret_del", data={"password": str(password or "000000").zfill(6)[:6]})
        return response.json()
