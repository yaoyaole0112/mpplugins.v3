"""115 web API operations used by this plugin, independent of EME."""

import time

import httpx


API = "https://webapi.115.com"
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
        self.client = httpx.Client(
            headers={"Cookie": cookie.strip(), "Referer": "https://115.com/",
                     "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125.0 Safari/537.36"},
            timeout=20, follow_redirects=True,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.client.close()

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
        for attempt in range(3):
            for url, defaults in LIST_ENDPOINTS:
                try:
                    params = {**defaults, "cid": cid, "limit": 1000, "offset": offset}
                    response = self.client.get(url, params=params)
                    response.raise_for_status()
                    data = response.json()
                    if not data.get("state"):
                        last_error = str(data.get("error") or data.get("msg") or last_error)
                        continue
                    files, directories, total, length = self._parse_entries(data)
                    if not files and not directories and (total is None or total != 0):
                        continue
                    if total is None and length >= 1000:
                        continue
                    return files, directories, total, length
                except (httpx.HTTPError, ValueError, TypeError) as error:
                    last_error = str(error)
            if attempt < 2:
                time.sleep(3)
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
        response = self.client.post(f"{API}/rb/delete", data={f"fid[{index}]": str(identifier)
                                                                  for index, identifier in enumerate(identifiers)})
        response.raise_for_status()
        return response.json()

    def move_files(self, identifiers: list, destination: str) -> dict:
        if not identifiers:
            return {"state": False, "msg": "无文件 ID"}
        fields = {"pid": str(destination)}
        fields.update({f"fid[{index}]": str(identifier) for index, identifier in enumerate(identifiers)})
        response = self.client.post(f"{API}/files/move", data=fields, timeout=30)
        response.raise_for_status()
        return response.json()

    def rb_list(self, limit: int = 1000) -> dict:
        response = self.client.get(f"{API}/rb", params={"aid": 7, "cid": 0, "limit": limit, "offset": 0})
        response.raise_for_status()
        result = response.json()
        if not result.get("state"):
            raise RuntimeError(str(result.get("error") or result.get("msg") or "115 回收站查询失败"))
        return {"count": int(result.get("count") or 0), "items": result.get("data") or []}

    def clear_recyclebin(self, password: str = "000000") -> dict:
        response = self.client.post(f"{API}/rb/secret_del", data={"password": str(password or "000000").zfill(6)[:6]})
        response.raise_for_status()
        return response.json()
