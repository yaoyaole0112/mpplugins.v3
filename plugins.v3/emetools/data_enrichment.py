"""MoviePilot-owned Emby enrichment and STRM preview repair.

Based on MediaEnhance's enrichment, mediainfo and preview workflows. No EME
service, credentials, containers or runtime modules are used by this plugin.
"""

import asyncio
import io
import json
import os
import re
import subprocess
import threading
from urllib.parse import quote
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image

from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.services import MediaServerHelper


FRAME_FILTER = "libplacebo=tonemapping=bt.2390:color_primaries=bt709:color_trc=bt709:colorspace=bt709:format=yuv420p"
FRAME_IMAGE = "jellyfin/jellyfin:latest"
FRAME_EXECUTABLE = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
DOCKER_SOCKET = "/var/run/docker.sock"
SHENYI_EXTRACT = "f84e5d989aa8a8b6ab1a3a8c0848faab"
SHENYI_PERSIST = "f98bb72fe87c19265e4550abc2cad64f"
DB_SCRIPT = '''
import datetime
import json
import os
import sqlite3

db = "/emby-config/data/library.db"
if not os.path.isfile(db):
    raise ValueError("Emby 数据库文件不存在")
items = json.loads(os.environ["ENRICH_UPDATES"])
backup = db + ".bak_enrich_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
with sqlite3.connect("file:" + db + "?mode=ro", uri=True) as source:
    with sqlite3.connect(backup) as target:
        source.backup(target)
with sqlite3.connect(db) as connection:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(MediaItems)")}
    if not {"Id", "Size", "RunTimeTicks"} <= columns:
        raise ValueError("Emby 数据库结构不匹配；已备份但未更新")
    updated = 0
    for identifier, fields in items.items():
        valid = {key: value for key, value in fields.items()
                 if key in columns and key in ("Size", "RunTimeTicks", "TotalBitrate", "Width", "Height")
                 and type(value) is int and value > 0}
        if "Container" in columns and isinstance(fields.get("Container"), str):
            valid["Container"] = fields["Container"][:20]
        if valid and str(identifier).isdigit():
            query = "UPDATE MediaItems SET " + ",".join(key + "=?" for key in valid) + " WHERE Id=?"
            cursor = connection.execute(query, [*valid.values(), int(identifier)])
            updated += max(cursor.rowcount, 0)
print(json.dumps({"updated": updated, "backup": backup}))
'''


def _docker(method, path, *, payload=None, timeout=30, params=None):
    """Use MoviePilot's existing Docker socket without an extra SDK dependency."""
    if not os.path.exists(DOCKER_SOCKET):
        raise ValueError("MoviePilot 未挂载 Docker Socket，无法操作独立截帧容器或 Emby")
    with httpx.Client(transport=httpx.HTTPTransport(uds=DOCKER_SOCKET),
                      base_url="http://docker", timeout=timeout) as client:
        response = client.request(method, "/v1.41" + path, json=payload, params=params)
        response.raise_for_status()
        return response


def _docker_job(image, config, *, timeout=240, binary=False):
    """Run and clean up a short-lived container; return captured stdout only."""
    container_id = None
    try:
        result = _docker("POST", "/containers/create", payload={"Image": image, **config})
        container_id = result.json()["Id"]
        _docker("POST", f"/containers/{container_id}/start")
        status = _docker("POST", f"/containers/{container_id}/wait",
                         params={"condition": "not-running"}, timeout=timeout).json()
        output = _docker("GET", f"/containers/{container_id}/logs",
                         params={"stdout": "1", "stderr": "0"}, timeout=45).content
        # Non-TTY Docker log frames: 1-byte channel + 3-byte padding + 4-byte length.
        chunks, offset = [], 0
        while offset + 8 <= len(output) and output[offset] in (1, 2):
            length = int.from_bytes(output[offset + 4:offset + 8], "big")
            end = offset + 8 + length
            if end > len(output):
                raise ValueError("截帧容器的输出不完整")
            if output[offset] == 1:
                chunks.append(output[offset + 8:end])
            offset = end
        stdout = b"".join(chunks) if offset == len(output) else output
        if status.get("StatusCode") != 0:
            raise ValueError("独立容器执行失败，退出码：%s" % status.get("StatusCode"))
        return stdout if binary else stdout.decode("utf-8").strip()
    finally:
        if container_id:
            try:
                _docker("DELETE", f"/containers/{container_id}", params={"force": "1"})
            except httpx.HTTPError:
                logger.warning("增强工具 数据补全：临时容器清理失败")


class DataEnrichment:
    def __init__(self, owner):
        self.owner = owner
        self.lock = threading.Lock()
        self.state = {"running": False, "task": "", "done": False, "error": "", "log": [],
                      "mediainfo": None, "preview": [], "preview_result": None}
        self._preview_selection = {}
        self._mi_selection = []

    def status(self):
        with self.lock:
            return {**self.state, "log": list(self.state["log"]),
                    "preview": list(self.state["preview"])}

    def log(self, message):
        text = str(message).replace("\n", " ")[:250]
        with self.lock:
            self.state["log"] = [*self.state["log"][-79:], text]
        logger.info("增强工具 数据补全：%s", text)

    def _services(self):
        services = MediaServerHelper().get_services(type_filter="emby") or {}
        values = services.values() if isinstance(services, dict) else services
        return {str(service.config.name): service.instance for service in values
                if getattr(service, "instance", None) and getattr(service.config, "name", None)}

    def _server(self, name):
        services = MediaServerHelper().get_services(type_filter="emby") or {}
        values = services.values() if isinstance(services, dict) else services
        service = next((entry for entry in values if str(entry.config.name) == name), None)
        if not services:
            raise ValueError("MoviePilot 尚未连接 Emby 服务器")
        if not service:
            raise ValueError("所选 Emby 服务器不可用，请重新搜索")
        config = service.config.config or {}
        host = str(config.get("host") or "").rstrip("/")
        key = str(config.get("apikey") or "")
        if not host or not key:
            raise ValueError("Emby 连接信息不完整")
        url = host if host.lower().endswith("/emby") else host + "/emby"
        return httpx.AsyncClient(base_url=url + "/", headers={"X-Emby-Token": key},
                                 timeout=httpx.Timeout(90, connect=10), follow_redirects=True)

    @staticmethod
    async def _json(client, path, params=None):
        response = await client.get(path.lstrip("/"), params=params)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _split(item_id):
        if "::" not in item_id:
            raise ValueError("请先选择搜索到的 Emby 剧集")
        name, identifier = item_id.split("::", 1)
        if not name or not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", identifier):
            raise ValueError("Emby 剧集标识无效")
        return name, identifier

    async def search(self, keyword):
        keyword = str(keyword or "").strip()[:80]
        if len(keyword) < 2:
            return []
        results = []
        for name, instance in self._services().items():
            try:
                async with self._server(name) as client:
                    user = str(instance.get_user() or "") if hasattr(instance, "get_user") else ""
                    path = f"Users/{user}/Items" if user else "Items"
                    data = await self._json(client, path, {"IncludeItemTypes": "Series",
                        "SearchTerm": keyword, "Recursive": "true", "Limit": 20})
                results.extend({"id": f"{name}::{item['Id']}", "name": item.get("Name", ""),
                    "year": item.get("ProductionYear") or "", "server": name}
                    for item in data.get("Items", []) if item.get("Id"))
            except (httpx.HTTPError, ValueError) as error:
                logger.warning("增强工具 数据补全：搜索服务器 %s 失败：%s", name, type(error).__name__)
        return results[:40]

    def _safe_strm(self, path):
        root = os.path.realpath(self.owner._strm_root)
        actual = os.path.realpath(str(path or ""))
        if not os.path.isdir(root) or not os.path.isfile(actual) or not actual.lower().endswith(".strm"):
            raise ValueError("分集 STRM 文件不存在或不在配置的目录下")
        if os.path.commonpath((root, actual)) != root:
            raise ValueError("分集 STRM 文件不在配置的根目录下")
        return actual

    def _preview_cache(self):
        try:
            cache = self.owner.get_data("enrichment_preview_cache") or {}
            return cache if isinstance(cache, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _color_cast(thumb):
        """Use MediaEnhance's conservative green/purple threshold."""
        try:
            with Image.open(thumb) as image:
                rgb = image.convert("RGB")
                width, height = rgb.size
                if width < 32 or height < 32:
                    return ""
                pixels = rgb.load()
                step = max(8, min(width, height) // 64)
                total = green = purple = 0
                green_strength = purple_strength = 0.0
                for y in range(step // 2, height, step):
                    for x in range(step // 2, width, step):
                        red, g, blue = pixels[x, y]
                        if max(red, g, blue) - min(red, g, blue) < 35:
                            continue
                        total += 1
                        if g > red + 35 and g > blue + 35:
                            green += 1
                            green_strength += (g - max(red, blue)) / 255.0
                        elif red > g + 30 and blue > g + 20:
                            purple += 1
                            purple_strength += ((red + blue) / 2 - g) / 255.0
                if total < 120:
                    return ""
                if green / total >= .42 and green_strength / max(green, 1) >= .12 and green >= purple + .12 * total:
                    return "green"
                if purple / total >= .42 and purple_strength / max(purple, 1) >= .10 and purple >= green + .12 * total:
                    return "purple"
        except (OSError, ValueError):
            pass
        return ""

    def _preview_status(self, item_id, thumb):
        if not os.path.isfile(thumb):
            return "missing"
        try:
            stat = os.stat(thumb)
            cached = self._preview_cache().get(item_id) or {}
            if cached.get("thumb") == thumb and cached.get("mtime") == int(stat.st_mtime) and cached.get("size") == stat.st_size:
                return "fixed"
        except OSError:
            return "missing"
        return "candidate" if self._color_cast(thumb) else "keep"

    @staticmethod
    def _video_url(path):
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                url = line.strip()
                parsed = urlsplit(url)
                if parsed.scheme in ("http", "https") and parsed.hostname:
                    return url
        raise ValueError("STRM 文件缺少有效的 HTTP 视频链接")

    async def scan_preview(self, series_id):
        name, identifier = self._split(series_id)
        async with self._server(name) as client:
            data = await self._json(client, "Items", {"ParentId": identifier,
                "IncludeItemTypes": "Episode", "Recursive": "true", "Fields": "Path",
                "Limit": 5000})
        results, selected = [], {}
        for item in data.get("Items", []):
            path = item.get("Path") or ""
            try:
                fs_path = self._safe_strm(path)
            except ValueError:
                continue
            thumb = fs_path[:-5] + "-thumb.jpg"
            dovi = bool(re.search(r"dovi|dolby[ -]?vision|\.dv[.\-_ ]", path, re.I))
            key = f"{name}::{item['Id']}"
            status = self._preview_status(key, thumb)
            result = {"id": key, "name": item.get("Name") or "",
                "season": item.get("ParentIndexNumber") or 0,
                "episode": item.get("IndexNumber") or 0,
                "dovi": dovi, "has_thumb": os.path.isfile(thumb), "status": status}
            results.append(result)
            selected[result["id"]] = {"path": fs_path, "thumb": thumb}
        with self.lock:
            self._preview_selection = selected
            self.state["preview"] = results
        return results

    def _begin(self, task, target, *args):
        with self.lock:
            if self.state["running"]:
                raise ValueError("已有数据补全任务正在运行，请等待完成")
            self.state.update(running=True, done=False, task=task, error="", log=[])

        def worker():
            try:
                asyncio.run(target(*args))
            except Exception as error:
                with self.lock:
                    self.state["error"] = f"{type(error).__name__}：{str(error)[:160]}"
                logger.exception("增强工具 数据补全 %s 失败", task)
            finally:
                with self.lock:
                    self.state["running"] = False
                    self.state["done"] = True

        threading.Thread(target=worker, name=f"eme-enrich-{task}", daemon=True).start()
        return {"started": True, "message": f"{task}已开始，可在页面查看进度"}

    def start_enrich(self, series_id, mode="all"):
        if mode not in {"all", "metadata", "credits", "episodes"}:
            raise ValueError("未知补全操作")
        name, identifier = self._split(series_id)
        if name not in self._services():
            raise ValueError("所选 Emby 服务器不可用")
        return self._begin("剧集补全", self._enrich, name, identifier, mode)

    async def _enrich(self, name, identifier, mode):
        api_key = str(getattr(settings, "TMDB_API_KEY", "") or "")
        if not api_key:
            raise ValueError("请先在 MoviePilot 配置 TMDB API Key")
        async with self._server(name) as emby, httpx.AsyncClient(
                base_url="https://api.themoviedb.org/3/", timeout=30) as tmdb:
            item = await self._json(emby, f"Items/{identifier}")
            if item.get("Type") != "Series":
                raise ValueError("选中项目不是 Emby 剧集")
            provider = item.get("ProviderIds") or {}
            tmdb_id = provider.get("Tmdb") or provider.get("TMDB")
            if not tmdb_id:
                search = await self._json(tmdb, "search/tv", {"api_key": api_key,
                    "query": item.get("Name", ""), "language": "zh-CN", "page": 1})
                candidates = search.get("results") or []
                year = str(item.get("ProductionYear") or "")
                matched = next((r for r in candidates if r.get("first_air_date", "")[:4] == year), None)
                # A wrong first-result match would overwrite the user's Emby metadata.
                if not matched and year:
                    raise ValueError("TMDB 未找到同年份剧集，请先在 Emby 设置正确的 TMDB ID")
                if not matched and len(candidates) != 1:
                    raise ValueError("TMDB 匹配不唯一，请先在 Emby 设置正确的 TMDB ID")
                tmdb_id = (matched or candidates[0] if candidates else {}).get("id")
            if not tmdb_id:
                raise ValueError("未找到对应的 TMDB 剧集，不写入 Emby")
            detail = await self._json(tmdb, f"tv/{tmdb_id}", {"api_key": api_key,
                "language": "zh-CN", "append_to_response": "aggregate_credits"})
            self.log(f"已匹配 {item.get('Name')} · TMDB {tmdb_id}")
            if mode in ("all", "metadata", "credits"):
                update = dict(item)
                if mode != "credits":
                    if detail.get("name"): update["Name"] = detail["name"]
                    if detail.get("overview"): update["Overview"] = detail["overview"]
                    if detail.get("vote_average"): update["CommunityRating"] = detail["vote_average"]
                    if detail.get("genres"): update["Genres"] = [g["name"] for g in detail["genres"]]
                if mode != "metadata":
                    cast = (detail.get("aggregate_credits") or {}).get("cast") or []
                    people = [{"Name": p["name"], "Type": "Actor", "Role": (p.get("roles") or [{}])[0].get("character", "")}
                              for p in cast[:50] if p.get("name")]
                    if people: update["People"] = people
                response = await emby.post(f"Items/{identifier}", json=update)
                response.raise_for_status()
                self.log("剧集元数据及演职人员已更新" if mode == "all" else "剧集资料已更新")
            if mode in ("all", "episodes"):
                episodes = await self._json(emby, "Items", {"ParentId": identifier,
                    "Recursive": "true", "IncludeItemTypes": "Episode", "Limit": 5000})
                seasons = {}
                changed = 0
                for episode in episodes.get("Items", []):
                    season, number = episode.get("ParentIndexNumber"), episode.get("IndexNumber")
                    if not season or number is None: continue
                    if season not in seasons:
                        data = await self._json(tmdb, f"tv/{tmdb_id}/season/{season}",
                            {"api_key": api_key, "language": "zh-CN"})
                        seasons[season] = {ep.get("episode_number"): ep for ep in data.get("episodes", [])}
                    tmdb_episode = seasons[season].get(number) or {}
                    if not tmdb_episode.get("name") and not tmdb_episode.get("overview"): continue
                    full = await self._json(emby, f"Items/{episode['Id']}")
                    if tmdb_episode.get("name"): full["Name"] = tmdb_episode["name"]
                    if tmdb_episode.get("overview"): full["Overview"] = tmdb_episode["overview"]
                    response = await emby.post(f"Items/{episode['Id']}", json=full)
                    response.raise_for_status()
                    changed += 1
                    if changed % 20 == 0: self.log(f"已补全 {changed} 集")
                self.log(f"分集信息补全完成：{changed} 集")

    def start_mediainfo_check(self):
        return self._begin("媒体信息检查", self._check_mediainfo)

    async def _check_mediainfo(self):
        results, total = [], 0
        for name in self._services():
            async with self._server(name) as emby:
                offset = 0
                while True:
                    data = await self._json(emby, "Items", {"IncludeItemTypes": "Movie,Episode",
                        "Recursive": "true", "Fields": "Path,Size,MediaSources",
                        "StartIndex": offset, "Limit": 500})
                    page = data.get("Items") or []
                    for item in page:
                        total += 1
                        source = (item.get("MediaSources") or [{}])[0]
                        size = item.get("Size") or source.get("Size") or 0
                        runtime = item.get("RunTimeTicks") or source.get("RunTimeTicks") or 0
                        if not size or not runtime:
                            try: path = self._safe_strm(item.get("Path") or source.get("Path"))
                            except ValueError: continue
                            results.append({"id": item["Id"], "server": name,
                                "name": item.get("Name", ""), "path": path,
                                "missing_size": not bool(size), "missing_runtime": not bool(runtime)})
                    offset += len(page)
                    if not page or offset >= int(data.get("TotalRecordCount", offset)): break
                    self.log(f"{name}：已检查 {offset} 个媒体项")
        with self.lock:
            self._mi_selection = results
            self.state["mediainfo"] = {"total": total, "incomplete_count": len(results),
                "items": [{k: item[k] for k in ("id", "server", "name", "missing_size", "missing_runtime")}
                          for item in results[:100]]}
        self.log(f"检查完成：共 {total} 项，需要补全 {len(results)} 项")

    def start_mediainfo_fill(self):
        with self.lock:
            if not self.state["mediainfo"] or not self._mi_selection:
                raise ValueError("请先检查并确认有待补全项目")
        return self._begin("媒体信息补全", self._fill_mediainfo)

    async def _scheduled_task(self, emby, task_id, label):
        """The Emby 神医 tasks are optional; never fail a local repair when absent."""
        try:
            task = await self._json(emby, f"ScheduledTasks/{task_id}")
            if str(task.get("State") or "").lower() != "running":
                response = await emby.post(f"ScheduledTasks/Running/{task_id}")
                response.raise_for_status()
            self.log(f"等待神医 {label} 完成")
            for _ in range(180):
                await asyncio.sleep(20)
                task = await self._json(emby, f"ScheduledTasks/{task_id}")
                if str(task.get("State") or "").lower() != "running":
                    self.log(f"神医 {label} 已完成")
                    return
            self.log(f"神医 {label} 等待超时，继续下一步")
        except httpx.HTTPError as error:
            self.log(f"神医 {label} 不可用（{type(error).__name__}），跳过")

    async def _probe(self, item):
        url = self._video_url(item["path"])
        fields = {}
        process = await asyncio.to_thread(subprocess.run,
            ["/usr/local/bin/ffprobe", "-v", "error", "-show_entries",
             "format=duration,size,bit_rate,format_name:stream=codec_type,width,height",
             "-of", "json", url], capture_output=True, timeout=90)
        if process.returncode == 0:
            data = json.loads(process.stdout or b"{}"); info = data.get("format") or {}
            if item["missing_size"] and str(info.get("size") or "").isdigit():
                fields["Size"] = int(info["size"])
            if item["missing_runtime"]:
                try: fields["RunTimeTicks"] = int(float(info.get("duration") or 0) * 10_000_000)
                except (TypeError, ValueError): pass
            if info.get("bit_rate") and str(info["bit_rate"]).isdigit():
                fields["TotalBitrate"] = int(info["bit_rate"])
            container = str(info.get("format_name") or "").split(",")[0]
            if container: fields["Container"] = container
            video = next((stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"), {})
            for field, key in (("Width", "width"), ("Height", "height")):
                if isinstance(video.get(key), int) and video[key] > 0:
                    fields[field] = video[key]
        # ffprobe often cannot read Size from STRM redirects; as in EME, use
        # Content-Length/Content-Range from the video URL as an independent source.
        if item["missing_size"] and not fields.get("Size"):
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                    async with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
                        response.raise_for_status()
                        length = (response.headers.get("Content-Range") or "").rsplit("/", 1)[-1]
                        if not length.isdigit():
                            length = response.headers.get("Content-Length") if response.status_code == 200 else ""
                        if str(length).isdigit(): fields["Size"] = int(length)
            except httpx.HTTPError:
                pass
        return {key: value for key, value in fields.items() if isinstance(value, str) or value > 0}

    async def _fill_mediainfo(self):
        # Collect probe data before interrupting Emby. Never guess a database path.
        updates = {}
        servers = {item["server"] for item in self._mi_selection}
        if len(servers) != 1:
            raise ValueError("仅支持单台 Emby 服务器的安全写库，未修改数据库")
        name = next(iter(servers))
        if any(item["missing_size"] and item["missing_runtime"] for item in self._mi_selection):
            async with self._server(name) as emby:
                await self._scheduled_task(emby, SHENYI_EXTRACT, "Extract")
        for index, item in enumerate(self._mi_selection, 1):
            try:
                fields = await self._probe(item)
                if fields:
                    updates.setdefault(item["server"], {})[item["id"]] = fields
            except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
                self.log(f"[{index}/{len(self._mi_selection)}] 探测失败：{type(error).__name__}")
            if index % 20 == 0: self.log(f"已探测 {index}/{len(self._mi_selection)} 项")
        if not updates:
            self.log("没有探测到可安全写入的文件大小或时长")
            return
        name = next(iter(updates))
        async with self._server(name) as emby:
            await self._verify_local_emby(emby)
            sessions = await self._json(emby, "Sessions")
            if any(session.get("NowPlayingItem") for session in sessions):
                raise ValueError("Emby 当前有用户正在播放，已取消停机写库")
            tasks = await self._json(emby, "ScheduledTasks")
            if any(task.get("State") == "Running" for task in tasks):
                raise ValueError("Emby 当前有计划任务运行，已取消停机写库")
        self.log(f"已探测 {sum(map(len, updates.values()))} 项；检查 Emby 空闲，准备备份并写库")
        changed, backup = await asyncio.to_thread(self._write_emby_database, updates[name])
        self.log(f"数据库已备份：{backup}；已更新 {changed} 项")
        # Give Emby time to become responsive after restart before Persist/check.
        for _ in range(12):
            await asyncio.sleep(5)
            try:
                async with self._server(name) as emby:
                    await self._json(emby, "System/Info/Public")
                    await self._scheduled_task(emby, SHENYI_PERSIST, "Persist")
                    break
            except httpx.HTTPError:
                continue
        await self._check_mediainfo()

    async def _verify_local_emby(self, emby):
        """Never write the local 'emby' DB for a different connected server."""
        expected = str((await self._json(emby, "System/Info/Public")).get("Id") or "")
        image = (await asyncio.to_thread(_docker, "GET", "/containers/moviepilot/json")).json()["Config"]["Image"]
        script = ('import json,urllib.request; '
                  'print(json.load(urllib.request.urlopen('
                  '"http://127.0.0.1:8096/emby/System/Info/Public",timeout=8)).get("Id",""))')
        config = {"Entrypoint": ["/opt/venv/bin/python"], "Cmd": ["-c", script],
                  "HostConfig": {"NetworkMode": "container:emby", "Memory": 134217728}}
        actual = await asyncio.to_thread(_docker_job, image, config, timeout=20)
        if expected and actual.strip() == expected:
            return
        raise ValueError("无法确认当前 Emby 与本机 emby 容器为同一实例；为保护数据库已取消写入")

    @staticmethod
    def _write_emby_database(updates):
        container = _docker("GET", "/containers/emby/json").json()
        mounts = [mount for mount in container.get("Mounts", []) if mount.get("Destination") == "/config"
                  and mount.get("Type") == "bind"]
        if len(mounts) != 1:
            raise ValueError("无法确定 Emby 数据库的宿主机挂载，未停止服务")
        host_config = mounts[0]["Source"]
        image = _docker("GET", "/containers/moviepilot/json").json()["Config"]["Image"]
        _docker("GET", "/images/%s/json" % quote(image, safe=""))
        config = {"Entrypoint": ["/opt/venv/bin/python"], "Cmd": ["-c", DB_SCRIPT],
                  "Env": ["ENRICH_UPDATES=" + json.dumps(updates)],
                  "HostConfig": {"Binds": [host_config + ":/emby-config:rw"],
                                 "NetworkMode": "none", "Memory": 268435456}}
        stopped = False
        try:
            _docker("POST", "/containers/emby/stop", params={"t": "30"}, timeout=60)
            stopped = True
            response = json.loads(_docker_job(image, config, timeout=300).splitlines()[-1])
            return response["updated"], response["backup"]
        finally:
            if stopped:
                _docker("POST", "/containers/emby/start", timeout=60)

    def start_preview_repair(self, series_id, episode_ids, force=False):
        name, _ = self._split(series_id)
        if not isinstance(episode_ids, list) or not episode_ids or len(episode_ids) > 300:
            raise ValueError("请选择 1 至 300 集预览图")
        with self.lock:
            targets = [(key, self._preview_selection[key]) for key in episode_ids
                       if key.startswith(name + "::") and key in self._preview_selection]
        if len(targets) != len(episode_ids) or len(set(episode_ids)) != len(episode_ids):
            raise ValueError("所选分集已失效，请重新扫描")
        return self._begin("分集预览图修复", self._repair_preview, series_id, targets, force)

    async def _repair_preview(self, series_id, targets, force):
        name, _ = self._split(series_id)
        try:
            await asyncio.to_thread(_docker, "GET", "/images/%s/json" % quote(FRAME_IMAGE, safe=""))
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
            self.log("首次使用正在获取独立截帧镜像，可能需要较长时间")
            await asyncio.to_thread(_docker, "POST", "/images/create",
                                    params={"fromImage": "jellyfin/jellyfin", "tag": "latest"}, timeout=900)
        completed, failed = 0, 0
        for position, (key, target) in enumerate(targets, 1):
            path = self._safe_strm(target["path"])
            thumb = path[:-5] + "-thumb.jpg"
            if self._preview_status(key, thumb) in ("fixed", "keep") and not force:
                self.log(f"[{position}/{len(targets)}] 已修复或无需修复，跳过")
                continue
            try:
                url = self._video_url(path)
                command = ["-v", "error", "-ss", "00:05:00", "-i", url,
                    "-frames:v", "1", "-vf", FRAME_FILTER, "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
                config = {"Entrypoint": [FRAME_EXECUTABLE], "Cmd": command,
                          "HostConfig": {"NetworkMode": "bridge", "Memory": 536870912}}
                png = await asyncio.to_thread(_docker_job, FRAME_IMAGE, config, timeout=240, binary=True)
                with Image.open(io.BytesIO(png)) as image:
                    rgb = image.convert("RGB")
                    rgb = rgb.crop((0, 0, rgb.width, max(1, int(rgb.height * .8))))
                    temporary = thumb + ".emetools-tmp"
                    try:
                        rgb.save(temporary, format="JPEG", quality=92)
                        os.replace(temporary, thumb)
                    finally:
                        if os.path.exists(temporary): os.unlink(temporary)
                async with self._server(name) as emby:
                    response = await emby.post(f"Items/{key.split('::', 1)[1]}/Refresh",
                                               params={"replaceAllMetadata": "false"})
                    response.raise_for_status()
                cache = self._preview_cache()
                stat = os.stat(thumb)
                cache[key] = {"thumb": thumb, "mtime": int(stat.st_mtime), "size": stat.st_size}
                self.owner.save_data("enrichment_preview_cache", cache)
                completed += 1
                self.log(f"[{position}/{len(targets)}] 分集 {key.split('::', 1)[1]} 已重截并刷新 Emby")
            except Exception as error:
                failed += 1
                self.log(f"[{position}/{len(targets)}] 修复失败：{type(error).__name__}：{str(error)[:100]}")
            if position % 3 == 0: await asyncio.sleep(8)
            else: await asyncio.sleep(2)
        with self.lock:
            self.state["preview_result"] = {"ok": completed, "fail": failed}
        self.log(f"预览图修复完成：成功 {completed} 集，失败 {failed} 集")
        try:
            await self.scan_preview(series_id)
        except httpx.HTTPError:
            self.log("刷新分集状态失败，请点击扫描分集重试")
        if failed:
            raise ValueError(f"{failed} 集修复失败，请查看运行进度")
