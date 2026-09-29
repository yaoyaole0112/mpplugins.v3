"""MoviePilot-owned Emby enrichment and STRM preview repair.

Based on MediaEnhance's enrichment, mediainfo and preview workflows. No EME
service, credentials, containers or runtime modules are used by this plugin.
"""

import asyncio
from io import BytesIO
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image
from pypinyin import lazy_pinyin

from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.services import MediaServerHelper
from app.schemas.types import MediaType
from app.runtime.settings import get_runtime_setting
from .tvmao_client import fetch_tvmao_cast


FRAME_FILTER = "libplacebo=tonemapping=bt.2390:color_primaries=bt709:color_trc=bt709:colorspace=bt709:format=yuv420p"
FRAME_IMAGE = "jellyfin/jellyfin:latest"
FRAME_EXECUTABLE = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
DOCKER_SOCKET = "/var/run/docker.sock"
SHENYI_EXTRACT = "f84e5d989aa8a8b6ab1a3a8c0848faab"
SHENYI_PERSIST = "f98bb72fe87c19265e4550abc2cad64f"
DEFAULT_ENRICH_CONFIG = {
    "metadata_source": "tmdb",
    "auto_on_import": True,
    "ai_enabled": False, "no_avatar": True, "episode_cast": False,
    "role_prefix": True, "ai_title": True, "ai_credits": True,
    "ai_overview": False, "resolve_role": True,
    "max_actors": 30, "cast_lock_min": 10,
}


def validate_enrich_config(values):
    if not isinstance(values, dict) or set(values) - DEFAULT_ENRICH_CONFIG.keys():
        raise ValueError("补全设置包含不支持的选项")
    result = dict(DEFAULT_ENRICH_CONFIG)
    for key, value in values.items():
        if key == "metadata_source":
            if value not in ("tmdb", "douban"):
                raise ValueError("元数据来源仅支持 TMDB 或豆瓣")
        elif key in ("max_actors", "cast_lock_min"):
            if type(value) is not int or not 1 <= value <= 200:
                raise ValueError("演员人数和锁定阈值必须是 1–200 的整数")
        elif type(value) is not bool:
            raise ValueError("补全设置开关必须为布尔值")
        result[key] = value
    return result


def _log_series(series_id):
    """Log only a validated, bounded Emby identifier, never an item path."""
    return str(series_id or "")[:90] if re.fullmatch(r"[\w-]{1,64}::[a-zA-Z0-9-]{1,64}",
                                                   str(series_id or "")) else "[剧集标识已隐藏]"


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
            # FFmpeg stderr may contain signed STRM URLs. Classify the error
            # without displaying raw logs, cookies or query-string tokens.
            stderr = _docker("GET", f"/containers/{container_id}/logs",
                             params={"stdout": "0", "stderr": "1", "tail": "20"}, timeout=20).content
            diagnostics = stderr.decode("utf-8", errors="replace")
            if "VK_ERROR" in diagnostics or "Vulkan device" in diagnostics:
                hint = "Vulkan 不可用，请检查宿主机 /dev/dri 设备映射"
            elif re.search(r"(?:HTTP|Server returned).*?(?:401|403)", diagnostics, re.I):
                hint = "远程视频链接拒绝访问（HTTP 401/403）"
            elif "No such filter" in diagnostics:
                hint = "截帧镜像缺少所需 FFmpeg 滤镜"
            else:
                hint = "请检查视频链接、设备及截帧容器日志"
            raise ValueError(f"独立截帧容器退出码 {status.get('StatusCode')}：{hint}")
        return stdout if binary else stdout.decode("utf-8").strip()
    finally:
        if container_id:
            try:
                _docker("DELETE", f"/containers/{container_id}", params={"force": "1"})
            except httpx.HTTPError:
                logger.warning("增强工具 数据补全：临时容器清理失败")


def _frame_host_root():
    """Resolve MoviePilot's /config bind for a short-lived FFmpeg output dir."""
    hostname = os.environ.get("HOSTNAME", "").strip()
    if not hostname:
        try:
            hostname = Path("/etc/hostname").read_text(encoding="utf-8").strip()
        except OSError:
            hostname = ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", hostname):
        raise ValueError("无法确认 MoviePilot 容器标识，已取消截帧")
    data = _docker("GET", f"/containers/{hostname}/json").json()
    mount = next((entry for entry in data.get("Mounts", [])
                  if entry.get("Destination") == "/config" and entry.get("Type") == "bind"), None)
    if not mount or not os.path.isabs(str(mount.get("Source") or "")):
        raise ValueError("MoviePilot 的 /config 未绑定宿主目录，无法安全读取截帧图片")
    return mount["Source"]


class DataEnrichment:
    def __init__(self, owner):
        self.owner = owner
        self.lock = threading.Lock()
        self.state = {"running": False, "task": "", "done": False, "error": "", "log": [],
                      "mediainfo": None, "preview": [], "preview_result": None,
                      "batch_result": None, "preview_repaired": []}
        self._preview_selection = {}
        self._mi_selection = []
        self._import_timers = {}
        self._import_pending = {}
        self._closed = False

    def resume_imports(self):
        pending = self.owner.get_data("enrichment_import_pending") or {}
        if not isinstance(pending, dict):
            return
        before = set(pending)
        for series_id, deadline in list(pending.items())[:200]:
            try:
                self._split(series_id)
                deadline = float(deadline)
                if 0 < deadline - time.time() < 24 * 3600:
                    self._queue_import(series_id, deadline)
            except (ValueError, TypeError, OSError):
                continue
        if before != set(self._import_pending):
            self.owner.save_data("enrichment_import_pending", dict(self._import_pending))

    def close(self):
        with self.lock:
            self._closed = True
            for timer in self._import_timers.values():
                timer.cancel()
            self._import_timers.clear()

    def _queue_import(self, series_id, deadline, expected=None):
        with self.lock:
            if self._closed or (expected is not None and
                                self._import_pending.get(series_id) != expected):
                return
            old = self._import_timers.pop(series_id, None)
            if old:
                old.cancel()
            self._import_pending[series_id] = deadline
            timer = threading.Timer(max(0.1, deadline - time.time()),
                                    self._run_import, (series_id, deadline))
            timer.daemon = True
            self._import_timers[series_id] = timer
            self.owner.save_data("enrichment_import_pending", dict(self._import_pending))
            timer.start()

    def queue_import(self, series_id):
        name, _ = self._split(series_id)
        if name not in self._services():
            return
        self._queue_import(series_id, time.time() + 300)
        logger.info("增强工具 数据补全：%s 新分集入库，5 分钟后合并补全", _log_series(series_id))

    def queue_import_by_tmdb(self, tmdb_id):
        """Fallback for successful MoviePilot transfers when Emby Webhook is unavailable."""
        value = str(tmdb_id or "").strip()
        if not value.isdigit():
            return False
        thread = threading.Thread(target=self._resolve_tmdb_import, args=(value,), daemon=True)
        thread.start()
        return True

    def _resolve_tmdb_import(self, tmdb_id):
        async def resolve():
            for server in self._services():
                try:
                    async with self._server(server) as client:
                        user = await self._user_id(server, client)
                        response = await self._json(client, f"Users/{user}/Items", {
                            "Recursive": "true", "IncludeItemTypes": "Series",
                            "AnyProviderIdEquals": f"tmdb.{tmdb_id}",
                            "Fields": "ProviderIds,ProductionYear", "Limit": 20})
                    items = response.get("Items") if isinstance(response, dict) else []
                    for item in items or []:
                        identifier = str(item.get("Id") or "")
                        if re.fullmatch(r"[a-zA-Z0-9-]{1,64}", identifier):
                            self.queue_import(f"{server}::{identifier}")
                            return
                except Exception as exc:
                    logger.info("增强工具 数据补全：整理完成后按 TMDB 查找剧集失败：%s", type(exc).__name__)
        try:
            asyncio.run(resolve())
        except Exception as exc:
            logger.info("增强工具 数据补全：整理完成后补全兜底失败：%s", type(exc).__name__)

    def _run_import(self, series_id, deadline):
        with self.lock:
            if self._closed or self._import_pending.get(series_id) != deadline:
                return
            if not self.owner._enabled or not self.options()["auto_on_import"]:
                self._import_timers.pop(series_id, None)
                self._import_pending.pop(series_id, None)
                self.owner.save_data("enrichment_import_pending", dict(self._import_pending))
                return
            if self.state["running"]:
                retry = True
            else:
                retry = False
        if retry:
            self._queue_import(series_id, time.time() + 30, expected=deadline)
            return
        try:
            self.start_enrich(series_id, "all")
            with self.lock:
                if self._import_pending.get(series_id) == deadline:
                    self._import_timers.pop(series_id, None)
                    self._import_pending.pop(series_id, None)
                    self.owner.save_data("enrichment_import_pending", dict(self._import_pending))
        except ValueError as exc:
            if "正在运行" in str(exc):
                self._queue_import(series_id, time.time() + 30, expected=deadline)
            else:
                self.log(f"入库自动补全未启动（{_log_series(series_id)}）：{type(exc).__name__}：{str(exc)[:120]}")
                logger.warning("增强工具 数据补全：入库自动补全未启动：%s：%s", type(exc).__name__, str(exc)[:160])
                with self.lock:
                    if self._import_pending.get(series_id) == deadline:
                        self._import_timers.pop(series_id, None)
                        self._import_pending.pop(series_id, None)
                        self.owner.save_data("enrichment_import_pending", dict(self._import_pending))

    def status(self):
        with self.lock:
            return {**self.state, "log": list(self.state["log"]),
                    "preview": list(self.state["preview"]),
                    "preview_repaired": list(self.state["preview_repaired"])}

    def options(self):
        return validate_enrich_config(getattr(self.owner, "_enrichment_config", {}) or {})

    @staticmethod
    def _needs_translation(text):
        return bool(text and not re.search(r"[\u3400-\u9fff]", str(text)))

    async def _ai_map(self, mapping, context):
        """Translate a bounded batch via MoviePilot's configured OpenAI-compatible LLM."""
        if not mapping:
            return {}
        key = str(getattr(settings, "LLM_API_KEY", "") or "")
        base = str(getattr(settings, "LLM_BASE_URL", "") or "").rstrip("/")
        model = str(getattr(settings, "LLM_MODEL", "") or "")
        if not (key and base and model):
            raise ValueError("AI 补齐已启用，请先在 MoviePilot 配置 LLM 服务")
        if str(getattr(settings, "LLM_API_PROTOCOL", "auto") or "auto").lower() == "anthropic":
            raise ValueError("AI 补齐需要 MoviePilot 中支持 OpenAI 兼容接口的 LLM 服务")
        proxies = get_runtime_setting("PROXY", None) if getattr(settings, "LLM_USE_PROXY", False) else None
        proxy = proxies.get("https") if isinstance(proxies, dict) else proxies
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        payload = {"model": model, "messages": [
            {"role": "system", "content": "你是影视元数据翻译助手。只翻译为简体中文，保留姓名和剧情事实；仅返回 JSON 对象，键必须与输入一致，值必须是字符串。"},
            {"role": "user", "content": context[:150] + "\n" + json.dumps(mapping, ensure_ascii=False)}],
                   "temperature": 0.1, "max_tokens": 2200}
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=90, trust_env=False) as client:
                response = await client.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload)
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I)
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("AI 未返回 JSON 对象")
            return {k: v.strip()[:1000] for k, v in result.items()
                    if k in mapping and isinstance(v, str) and v.strip()}
        except (httpx.HTTPError, KeyError, IndexError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            # Never log the LLM URL or headers: provider URLs may contain secrets.
            raise ValueError(f"MoviePilot LLM 请求或响应失败：{type(exc).__name__}") from None

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

    async def _user_id(self, name, client):
        """Use the same user-scoped item API as the Emby search/list client."""
        instance = self._services().get(name)
        if instance is None:
            raise ValueError("所选 Emby 服务器不可用，请重新搜索")
        user = str(instance.get_user() or "") if hasattr(instance, "get_user") else ""
        if not user:
            users = await self._json(client, "Users")
            user = str((users[0] if users else {}).get("Id") or "")
        if not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", user):
            raise ValueError("未获取到有效的 Emby 用户 ID")
        return user

    async def _item(self, client, user, item_id):
        if not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", str(item_id)):
            raise ValueError("Emby 条目 ID 无效")
        return await self._json(client, f"Users/{user}/Items/{item_id}",
                                {"Fields": "MediaSources,MediaStreams,Path,Size,ProviderIds"})

    @staticmethod
    def _tmdb_client():
        # Use MoviePilot's configured mirror and proxy, not a hard-coded TMDB
        # hostname that may be unreachable from the MoviePilot container.
        domain = str(getattr(settings, "TMDB_API_DOMAIN", "api.themoviedb.org")
                     or "api.themoviedb.org").strip().removeprefix("https://").removeprefix("http://").strip("/")
        proxies = get_runtime_setting("PROXY", None)
        proxy = proxies.get("https") if isinstance(proxies, dict) else proxies
        return httpx.AsyncClient(base_url=f"https://{domain}/3/", proxy=proxy,
                                 timeout=30, trust_env=False)

    async def _tmdb_json(self, client, path, params):
        try:
            return await self._json(client, path, params)
        except httpx.ConnectError:
            raise ValueError("TMDB 连接失败：请检查 MoviePilot 的 TMDB 域名和代理设置") from None
        except httpx.TimeoutException:
            raise ValueError("TMDB 请求超时：请检查 MoviePilot 的 TMDB 域名和代理设置") from None
        except httpx.HTTPStatusError as error:
            # Never include the request URL: the API key is in its query string.
            raise ValueError(f"TMDB 接口返回 HTTP {error.response.status_code}，请检查 MoviePilot 的 TMDB 配置") from None

    @staticmethod
    def _douban_match(results, name, year, season=1):
        """Never use the first search result without a reliable name/season/year match."""
        def normalize(value):
            value = re.sub(r"第\s*[一二三四五六七八九十百\d]+\s*季", "", str(value or ""))
            return re.sub(r"[^\w\u3400-\u9fff]", "", value).casefold()

        def season_number(value):
            match = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*季", str(value or ""))
            if not match:
                return 1
            raw = match.group(1)
            if raw.isdecimal():
                return int(raw)
            digits = {character: index + 1 for index, character in enumerate("一二三四五六七八九")}
            if "十" in raw:
                tens, ones = raw.split("十", 1)
                return (digits.get(tens, 1) if tens else 1) * 10 + digits.get(ones, 0)
            return digits.get(raw, 0)

        title = normalize(name)
        matches = [entry for entry in results if isinstance(entry, dict)
                   and entry.get("id") and normalize(entry.get("title")) == title
                   and season_number(entry.get("title")) == season
                   and (not year or not entry.get("year") or str(entry["year"])[:4] == year)]
        return matches[0] if len(matches) == 1 else None

    async def _douban_data(self, item, season=None, include_cast=False):
        """Read public Douban metadata directly; failures keep TMDB as fallback."""
        name = str(item.get("Name") or "")
        year = str(item.get("ProductionYear") or "")
        query = name if season in (None, 1) else f"{name} 第{season}季"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; MoviePilot/3.0)",
                   "Referer": "https://movie.douban.com/"}
        try:
            async with httpx.AsyncClient(headers=headers, timeout=12, follow_redirects=True,
                                         trust_env=False) as client:
                # Emby provider ID is more precise than a name search and avoids
                # Douban's occasionally rate-limited suggestion endpoint.
                provider = item.get("ProviderIds") or {}
                subject_id = str(provider.get("Douban") or provider.get("douban") or
                                 provider.get("DOUBAN") or "") if season is None else ""
                if not re.fullmatch(r"\d+", subject_id):
                    response = await client.get("https://movie.douban.com/j/subject_suggest", params={"q": query})
                    response.raise_for_status()
                    match = self._douban_match(response.json(), name, year if season in (None, 1) else "", season or 1)
                    if not match:
                        self.log(f"豆瓣未可靠匹配《{name}》第 {season or 1} 季，使用 TMDB")
                        return {}
                    subject_id = str(match["id"])
                if not re.fullmatch(r"\d+", subject_id):
                    return {}
                if season is not None:
                    response = await client.get(f"https://movie.douban.com/j/tv/series/{subject_id}")
                    response.raise_for_status()
                    return {int(ep["episode"]): {"name": str(ep.get("title") or ""),
                            "overview": str(ep.get("desc") or "")}
                            for ep in response.json().get("episodes", [])
                            if isinstance(ep, dict) and str(ep.get("episode") or "").isdecimal()
                            and int(ep["episode"]) > 0}
                response = await client.get(f"https://movie.douban.com/j/subject/{subject_id}")
                response.raise_for_status()
                data = response.json()
                casts = data.get("casts") or []
                if not casts and include_cast:
                    # j/subject may omit cast; mobile celebrities includes names
                    # and photos, but its generic '演员' is not a character name.
                    try:
                        mobile = await client.get(
                            f"https://m.douban.com/rexxar/api/v2/movie/{subject_id}/celebrities",
                            params={"start": 0, "count": 100},
                            headers={"Referer": "https://m.douban.com/"})
                        mobile.raise_for_status()
                        celebrities = mobile.json().get("actors") or []
                        casts = [{"name": actor.get("name"), "role": actor.get("character"),
                                  "img": (actor.get("avatar") or {}).get("large") or
                                         (actor.get("avatar") or {}).get("normal") or
                                         (actor.get("avatar") or {}).get("small")}
                                 for actor in celebrities if isinstance(actor, dict)]
                    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
                        self.log("豆瓣移动端演职人员不可用，使用 TMDB 补全演员")
                self.log(f"已匹配《{name}》· 豆瓣 {subject_id}（缺失字段回退 TMDB）")
                return {"name": str(data.get("title") or ""), "overview": str(data.get("intro") or ""),
                        "casts": casts if isinstance(casts, list) else []}
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as error:
            self.log(f"豆瓣数据不可用（{type(error).__name__}），使用 TMDB")
            return {}

    @staticmethod
    def _merge_cast(douban_cast, tmdb_cast):
        """Prefer Douban Chinese names, fill real roles/photos from matched TMDB actors."""
        def keys(value):
            names = [value.get("name"), value.get("original_name")]
            result = set()
            for name in names:
                if not isinstance(name, str) or not name.strip():
                    continue
                raw = re.sub(r"[^\w\u3400-\u9fff]", "", name).casefold()
                result.add(raw)
                if re.search(r"[\u3400-\u9fff]", name):
                    result.add(re.sub(r"[^a-z0-9]", "", "".join(lazy_pinyin(name)).casefold()))
            return result - {""}

        def is_placeholder(role):
            return str(role or "").strip().casefold() in {
                "", "演员", "饰", "配", "配音演员", "客串", "特别出演", "友情出演",
                "actor", "actress", "cast", "voice", "unknown", "self", "uncredited"}

        douban_cast = [actor for actor in douban_cast if isinstance(actor, dict) and actor.get("name")]
        tmdb_cast = [actor for actor in tmdb_cast if isinstance(actor, dict) and actor.get("name")]
        tmdb_index = {}
        for index, actor in enumerate(tmdb_cast):
            for key in keys(actor):
                tmdb_index.setdefault(key, set()).add(index)
        douban_index = {}
        for index, actor in enumerate(douban_cast):
            for key in keys(actor):
                douban_index.setdefault(key, set()).add(index)
        merged, used = [], set()
        for actor in douban_cast:
            if not isinstance(actor, dict) or not actor.get("name"):
                continue
            matches = {next(iter(tmdb_index[key])) for key in keys(actor)
                       if len(tmdb_index.get(key, ())) == 1 and len(douban_index.get(key, ())) == 1}
            counterpart = tmdb_cast[next(iter(matches))] if len(matches) == 1 else None
            if counterpart:
                used.update(matches)
            role = actor.get("role") or ""
            if is_placeholder(role) and counterpart and not is_placeholder(counterpart.get("role")):
                role = counterpart["role"]
            merged.append({"name": actor["name"], "role": "" if is_placeholder(role) else role,
                           "profile_path": actor.get("img") or (counterpart or {}).get("profile_path"),
                           "order": len(merged)})
        # The secondary source only supplies actors not confidently identified
        # in Douban; existing Chinese entries retain their order and names.
        merged.extend(actor for index, actor in enumerate(tmdb_cast) if index not in used)
        return merged

    @staticmethod
    def _needs_chinese_cast(cast, limit):
        if not cast:
            return True
        for actor in cast[:limit]:
            role = str(actor.get("role") or "")
            role = re.sub(r"^\s*[饰配]\s*", "", role)
            if (not re.search(r"[\u3400-\u9fff]", str(actor.get("name") or "")) or
                    not re.search(r"[\u3400-\u9fff]", role) or
                    role.strip() in {"演员", "配音演员", "客串", "特别出演", "友情出演"} or
                    re.search(r"[A-Za-z]", role)):
                return True
        return False

    @staticmethod
    def _merge_tvmao_cast(cast, web_cast):
        """Only join unique verified names; never assign a role to a guessed person."""
        def keys(name):
            name = str(name or "").strip()
            if not name:
                return set()
            result = {re.sub(r"[^\w\u3400-\u9fff]", "", name).casefold()}
            if re.search(r"[\u3400-\u9fff]", name):
                result.add(re.sub(r"[^a-z0-9]", "", "".join(lazy_pinyin(name)).casefold()))
            return result - {""}

        indexes = {}
        for index, actor in enumerate(cast):
            for key in keys(actor.get("name")) | keys(actor.get("original_name")):
                indexes.setdefault(key, set()).add(index)
        web_index = {}
        for index, actor in enumerate(web_cast):
            for key in keys(actor.get("name")):
                web_index.setdefault(key, set()).add(index)

        result = [dict(actor) for actor in cast]
        replaced = 0
        unmatched = []
        web_order = []
        matched_indices = set()
        for actor in web_cast:
            matches = {next(iter(indexes[key])) for key in keys(actor.get("name"))
                       if len(indexes.get(key, ())) == 1 and len(web_index.get(key, ())) == 1}
            if len(matches) != 1:
                unmatched.append(actor)
                continue
            match_index = next(iter(matches))
            if match_index in matched_indices:
                continue
            matched_indices.add(match_index)
            web_order.append(result[match_index])
            person = result[match_index]
            if not re.search(r"[\u3400-\u9fff]", str(person.get("name") or "")):
                person["name"] = actor["name"]
                replaced += 1
            role = re.sub(r"^\s*[饰配]\s*", "", str(person.get("role") or ""))
            if not re.search(r"[\u3400-\u9fff]", role) or re.search(r"[A-Za-z]", role) or role in {"演员", "配音演员"}:
                person["role"] = actor["role"]
            if not person.get("profile_path") and actor.get("img"):
                person["profile_path"] = actor["img"]

        # Only extend with unknown web actors when no reliable Chinese cast
        # existed; with a partial Chinese cast, unmatched names could duplicate
        # alternate stage names and should not be appended speculatively.
        if not any(re.search(r"[\u3400-\u9fff]", str(person.get("name") or "")) for person in cast):
            for actor in unmatched:
                if any(indexes.get(key) for key in keys(actor.get("name"))):
                    continue
                web_order.append({"name": actor["name"], "role": actor["role"],
                                  "profile_path": actor.get("img") or ""})
            # Give verified Chinese actors priority over a long English-only
            # TMDB list; otherwise max_actors could truncate every new actor.
            result = web_order + [person for index, person in enumerate(result)
                                  if index not in matched_indices]
            for index, person in enumerate(result):
                person["order"] = index
        return result, replaced, len(result) - len(cast)

    async def _tvmao_cast(self, item):
        try:
            providers = item.get("ProviderIds") or {}
            people = await fetch_tvmao_cast(str(item.get("Name") or ""),
                                            str(item.get("ProductionYear") or ""),
                                            str(providers.get("Tmdb") or providers.get("TMDB") or ""),
                                            status=self.log)
            self.log(f"电视猫演员表：找到 {len(people)} 人" if people else
                     "电视猫未找到可确认的中文演职人员，保持豆瓣/TMDB 结果")
            return people
        except (httpx.HTTPError, ValueError) as error:
            self.log(f"电视猫演员表不可用（{type(error).__name__}），保持豆瓣/TMDB 结果")
            return []

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
        try:
            services = self._services()
        except Exception as error:
            logger.error("增强工具 数据补全：读取 Emby 服务失败：%s：%s",
                         type(error).__name__, str(error)[:160])
            raise ValueError(f"读取 Emby 服务失败：{type(error).__name__}") from error
        for name, instance in services.items():
            try:
                async with self._server(name) as client:
                    user = await self._user_id(name, client)
                    data = await self._json(client, f"Users/{user}/Items", {"IncludeItemTypes": "Series",
                        "SearchTerm": keyword, "Recursive": "true", "Limit": 20})
                results.extend({"id": f"{name}::{item['Id']}", "name": item.get("Name", ""),
                    "year": item.get("ProductionYear") or "", "server": name}
                    for item in data.get("Items", []) if item.get("Id"))
            except Exception as error:
                logger.warning("增强工具 数据补全：搜索服务器 %s 失败：%s", name, type(error).__name__)
        return results[:40]

    async def list_series(self):
        """Bounded read-only catalog; the user must explicitly select batch targets."""
        results = []
        for name in self._services():
            async with self._server(name) as client:
                user = await self._user_id(name, client)
                offset = 0
                while offset < 2000:
                    data = await self._json(client, f"Users/{user}/Items", {
                        "IncludeItemTypes": "Series", "Recursive": "true",
                        "StartIndex": offset, "Limit": 500})
                    page = data.get("Items") or []
                    results.extend({"id": f"{name}::{item['Id']}",
                                    "name": item.get("Name") or "未命名",
                                    "year": item.get("ProductionYear") or "", "server": name}
                                   for item in page if item.get("Id"))
                    offset += len(page)
                    if not page or offset >= int(data.get("TotalRecordCount", offset)):
                        break
        return results

    def tv_libraries(self):
        """Only expose actual TV libraries, scoped by server and Emby view ID."""
        services = MediaServerHelper().get_services(type_filter="emby") or {}
        values = services.values() if isinstance(services, dict) else services
        result = []
        for service in values:
            if not getattr(service, "instance", None):
                continue
            server = str(service.config.name or "")
            for library in service.instance.get_librarys(hidden=False) or []:
                if getattr(library, "type", None) == MediaType.TV.value and getattr(library, "id", None):
                    result.append({"id": f"{server}::{library.id}",
                                   "name": str(library.name or "未命名"), "server": server})
        return result

    async def _library_series(self, libraries, titles=None):
        """Page through each selected TV view, without a first-N catalog cutoff."""
        seen = set()
        result = []
        for library in libraries:
            name, library_id = self._split(library["id"])
            async with self._server(name) as client:
                user = await self._user_id(name, client)
                offset = 0
                while True:
                    data = await self._json(client, f"Users/{user}/Items", {
                        "ParentId": library_id, "IncludeItemTypes": "Series",
                        "Recursive": "true", "StartIndex": offset, "Limit": 500})
                    page = data.get("Items") or []
                    for item in page:
                        identifier = str(item.get("Id") or "")
                        if re.fullmatch(r"[a-zA-Z0-9-]{1,64}", identifier):
                            key = f"{name}::{identifier}"
                            if key not in seen:
                                seen.add(key)
                                result.append(key)
                                if titles is not None:
                                    titles[key] = str(item.get("Name") or "未命名剧集").replace("\n", " ")[:90]
                    offset += len(page)
                    if not page or offset >= int(data.get("TotalRecordCount", offset)):
                        break
        return result

    def _selected_tv_libraries(self, library_ids):
        libraries = self.tv_libraries()
        known = {library["id"]: library for library in libraries}
        if library_ids is None:
            if not libraries:
                raise ValueError("没有可用的 Emby 电视剧媒体库")
            return libraries
        if (not isinstance(library_ids, list) or not library_ids or
                len(library_ids) > 200 or len(set(library_ids)) != len(library_ids) or
                any(not isinstance(value, str) or value not in known for value in library_ids)):
            raise ValueError("请选择有效的 Emby 电视剧媒体库并重试")
        return [known[value] for value in library_ids]

    def start_library_enrich(self, library_ids):
        libraries = self._selected_tv_libraries(library_ids)
        return self._begin("批量补全剧集数据", self._enrich_libraries, libraries, self.options())

    async def _enrich_libraries(self, libraries, options):
        series_ids = await self._library_series(libraries)
        self.log(f"已读取 {len(libraries)} 个电视剧媒体库，共 {len(series_ids)} 部剧集")
        await self._batch_enrich(series_ids, options)

    def start_all_preview(self):
        libraries = self._selected_tv_libraries(None)
        return self._begin("批量扫描与修复分集图片", self._preview_libraries, libraries)

    async def _preview_libraries(self, libraries):
        first = libraries[0] if libraries else None
        if first:
            name, _ = self._split(first["id"])
            async with self._server(name) as emby:
                await self._trigger_keeper_thumbnails(emby)
        titles = {}
        series_ids = await self._library_series(libraries, titles)
        self.log(f"已读取全部 {len(libraries)} 个电视剧媒体库，共 {len(series_ids)} 部剧集")
        await self._batch_preview(series_ids, titles)

    def start_batch_enrich(self, series_ids):
        if not isinstance(series_ids, list) or not 1 <= len(series_ids) <= 50:
            raise ValueError("每次请选择 1 至 50 部剧集补全")
        if len(set(series_ids)) != len(series_ids):
            raise ValueError("批量剧集不能重复")
        for series_id in series_ids:
            name, _ = self._split(series_id)
            if name not in self._services():
                raise ValueError("所选 Emby 服务器不可用，请刷新剧集列表")
        return self._begin("批量补全剧集数据", self._batch_enrich, list(series_ids), self.options())

    async def _batch_enrich(self, series_ids, options):
        ok = failed = 0
        for index, series_id in enumerate(series_ids, 1):
            name, identifier = self._split(series_id)
            self.log(f"[{index}/{len(series_ids)}] 开始补全 {_log_series(series_id)}")
            try:
                await self._enrich(name, identifier, "all", options)
                ok += 1
            except Exception as exc:
                failed += 1
                self.log(f"[{index}/{len(series_ids)}] 补全失败：{type(exc).__name__}（可单独重试）")
        with self.lock:
            self.state["batch_result"] = {"ok": ok, "fail": failed}
        self.log(f"批量补全结束：成功 {ok} 部，失败 {failed} 部")

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
            source = BytesIO(thumb) if isinstance(thumb, (bytes, bytearray)) else thumb
            with Image.open(source) as image:
                rgb = image.convert("RGB")
                width, height = rgb.size
                if width < 32 or height < 32:
                    return ""
                pixels = rgb.load()
                step = max(8, min(width, height) // 64)
                total = green = purple = 0
                green_strength = purple_strength = 0.0
                mean_r = mean_g = mean_b = 0.0
                sampled = 0
                for y in range(step // 2, height, step):
                    for x in range(step // 2, width, step):
                        red, g, blue = pixels[x, y]
                        mean_r += red
                        mean_g += g
                        mean_b += blue
                        sampled += 1
                        if max(red, g, blue) - min(red, g, blue) < 35:
                            continue
                        total += 1
                        # Emby 的 DoVi 截图不一定整张都呈现强烈色偏，
                        # 使用较低的像素差阈值捕获“明显发绿/发紫但画面仍有正常区域”的情况。
                        if g > red + 20 and g > blue + 20:
                            green += 1
                            green_strength += (g - max(red, blue)) / 255.0
                        elif red > g + 20 and blue > g + 15:
                            purple += 1
                            purple_strength += ((red + blue) / 2 - g) / 255.0
                if total < 80 or not sampled:
                    return ""
                mean_r /= sampled
                mean_g /= sampled
                mean_b /= sampled
                green_ratio = green / total
                purple_ratio = purple / total
                # 增加整图平均通道偏移作为补充，解决偏色集中在主体区域时漏检。
                green_mean_cast = mean_g - max(mean_r, mean_b)
                purple_mean_cast = (mean_r + mean_b) / 2 - mean_g
                if (green_ratio >= .18 and green_strength / max(green, 1) >= .04
                        and green >= purple + .04 * total
                        and (green_mean_cast >= 3 or green_ratio >= .28)):
                    return "green"
                if (purple_ratio >= .18 and purple_strength / max(purple, 1) >= .035
                        and purple >= green + .04 * total
                        and (purple_mean_cast >= 3 or purple_ratio >= .28)):
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
        results, selected = [], {}
        async with self._server(name) as client:
            user = await self._user_id(name, client)
            data = await self._json(client, f"Users/{user}/Items", {"ParentId": identifier,
                "IncludeItemTypes": "Episode", "Recursive": "true", "Fields": "Path",
                "Limit": 5000})
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
                # Emby may serve a cached Primary image that is not the STRM
                # sibling file.  Inspect the image Emby actually exposes too,
                # otherwise the UI can show a green frame while local scanning
                # reports it as normal.
                if status == "keep":
                    try:
                        image_response = await client.get(
                            f"Items/{item['Id']}/Images/Primary",
                            params={"quality": 100, "format": "jpg"},
                        )
                        content_type = str(image_response.headers.get("content-type") or "")
                        if image_response.is_success and content_type.lower().startswith("image/"):
                            if self._color_cast(image_response.content):
                                status = "candidate"
                    except (httpx.HTTPError, OSError, ValueError):
                        # Local STRM thumbnail detection remains the fallback.
                        pass
                result = {"id": key, "name": item.get("Name") or "",
                    "season": item.get("ParentIndexNumber") or 0,
                    "episode": item.get("IndexNumber") or 0,
                    "dovi": dovi, "has_thumb": os.path.isfile(thumb), "status": status}
                results.append(result)
                selected[result["id"]] = {"path": fs_path, "thumb": thumb,
                    "series": str(item.get("SeriesName") or "").replace("\n", " ")[:90],
                    "season": result["season"], "episode": result["episode"], "name": result["name"]}
        with self.lock:
            self._preview_selection = selected
            self.state["preview"] = results
        return results

    def _begin(self, task, target, *args):
        with self.lock:
            if self.state["running"]:
                raise ValueError("已有数据补全任务正在运行，请等待完成")
            self.state.update(running=True, done=False, task=task, error="", log=[])
            if task in ("分集图片修复", "批量扫描与修复分集图片"):
                self.state["preview_repaired"] = []

        def worker():
            try:
                asyncio.run(target(*args))
            except Exception as error:
                with self.lock:
                    self.state["error"] = f"{type(error).__name__}：{str(error)[:160]}"
                logger.error("增强工具 数据补全 %s 失败：%s：%s",
                             task, type(error).__name__, str(error)[:160])
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
        return self._begin("剧集补全", self._enrich, name, identifier, mode, self.options())

    async def _sync_episode_people(self, emby, user, series_id, people):
        """Propagate the freshly saved series cast without changing episode metadata."""
        if not people:
            self.log("剧集没有可写入的演职人员，跳过分集同步")
            return
        offset, changed = 0, 0
        while True:
            data = await self._json(emby, f"Users/{user}/Items", {
                "ParentId": series_id, "Recursive": "true",
                "IncludeItemTypes": "Episode", "StartIndex": offset, "Limit": 500})
            page = data.get("Items") or []
            for episode in page:
                episode_id = episode.get("Id")
                if not episode_id:
                    continue
                full = await self._item(emby, user, episode_id)
                full["People"] = people
                response = await emby.post(f"Items/{episode_id}", json={
                    key: value for key, value in full.items() if value is not None})
                response.raise_for_status()
                changed += 1
                if changed % 20 == 0:
                    self.log(f"已同步 {changed} 集演职人员")
            offset += len(page)
            if not page or offset >= int(data.get("TotalRecordCount", offset)):
                break
        self.log(f"分集演职人员同步完成：{changed} 集")

    async def _enrich(self, name, identifier, mode, options=None):
        options = validate_enrich_config(options or self.options())
        if mode == "all":
            # The single "补全剧集数据" action always includes episode cast
            # and, when an AI provider is enabled, every translation scope.
            options.update(episode_cast=True, ai_title=True, ai_credits=True,
                           ai_overview=True)
        api_key = str(getattr(settings, "TMDB_API_KEY", "") or "")
        if not api_key:
            raise ValueError("请先在 MoviePilot 配置 TMDB API Key")
        async with self._server(name) as emby, self._tmdb_client() as tmdb:
            user = await self._user_id(name, emby)
            item = await self._item(emby, user, identifier)
            if item.get("Type") != "Series":
                raise ValueError("选中项目不是 Emby 剧集")
            provider = item.get("ProviderIds") or {}
            tmdb_id = provider.get("Tmdb") or provider.get("TMDB")
            if not tmdb_id:
                search = await self._tmdb_json(tmdb, "search/tv", {"api_key": api_key,
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
            detail = await self._tmdb_json(tmdb, f"tv/{tmdb_id}", {"api_key": api_key,
                "language": "zh-CN", "append_to_response": "aggregate_credits,external_ids"})
            self.log(f"已匹配 {item.get('Name')} · TMDB {tmdb_id}")
            douban_detail = (await self._douban_data(item, include_cast=mode != "metadata")
                             if options["metadata_source"] == "douban"
                             and mode in ("all", "metadata", "credits") else {})
            if mode in ("all", "metadata", "credits"):
                update = dict(item)
                if mode != "credits":
                    if douban_detail.get("name") or detail.get("name"):
                        update["Name"] = douban_detail.get("name") or detail["name"]
                    if douban_detail.get("overview") or detail.get("overview"):
                        update["Overview"] = douban_detail.get("overview") or detail["overview"]
                    if detail.get("vote_average"): update["CommunityRating"] = detail["vote_average"]
                    if detail.get("genres"): update["Genres"] = [g["name"] for g in detail["genres"]]
                    studios = [entry.get("name") for entry in
                               (detail.get("networks") or []) + (detail.get("production_companies") or [])
                               if isinstance(entry, dict) and entry.get("name")]
                    if studios:
                        update["Studios"] = list(dict.fromkeys(studios))[:30]
                    providers = dict(item.get("ProviderIds") or {})
                    providers["Tmdb"] = str(tmdb_id)
                    external = detail.get("external_ids") or {}
                    if external.get("imdb_id"):
                        providers["Imdb"] = external["imdb_id"]
                    if external.get("tvdb_id"):
                        providers["Tvdb"] = str(external["tvdb_id"])
                    update["ProviderIds"] = providers
                    if options["ai_enabled"]:
                        translations = {}
                        if options["ai_title"] and re.search(r"[A-Za-z]", str(update.get("Name") or "")):
                            translations["Name"] = update["Name"][:180]
                        if options["ai_overview"] and re.search(r"[A-Za-z]", str(update.get("Overview") or "")):
                            translations["Overview"] = update["Overview"][:900]
                        if translations:
                            translated = await self._ai_map(translations, "剧集标题和剧情简介；将英文及中英混写内容译为简体中文，保留原有事实")
                            update.update({key: value for key, value in translated.items()
                                           if re.search(r"[\u3400-\u9fff]", value)
                                           and not re.search(r"[A-Za-z]", value)})
                if mode != "metadata":
                    tmdb_cast = (detail.get("aggregate_credits") or {}).get("cast") or []
                    cast = [{"name": p["name"], "original_name": p.get("original_name"),
                             "profile_path": p.get("profile_path"), "order": p.get("order"),
                             "role": (p.get("roles") or [{}])[0].get("character", "") or ""}
                            for p in tmdb_cast if p.get("name")]
                    douban_cast = douban_detail.get("casts") or []
                    if options["metadata_source"] == "douban" and douban_cast:
                        cast = self._merge_cast(douban_cast, cast)
                        self.log(f"演职人员来源：豆瓣优先（{len(douban_cast)} 人），TMDB 补缺（合并 {len(cast)} 人）")
                    else:
                        self.log(f"演职人员来源：TMDB（{len(cast)} 人）" +
                                 ("；豆瓣没有可用演员，已回退" if options["metadata_source"] == "douban" else ""))
                    if self._needs_chinese_cast(cast, options["max_actors"]):
                        if options["metadata_source"] != "douban":
                            douban_cast = (await self._douban_data(item, include_cast=True)).get("casts") or []
                            if douban_cast:
                                cast = self._merge_cast(douban_cast, cast)
                                self.log(f"TMDB 中文演职不足，豆瓣补缺（{len(douban_cast)} 人）")
                        if self._needs_chinese_cast(cast, options["max_actors"]):
                            web_cast = await self._tvmao_cast(item)
                            if web_cast:
                                cast, replaced, added = self._merge_tvmao_cast(cast, web_cast)
                                self.log(f"电视猫中文演职补缺：汉化姓名 {replaced} 人，新增 {added} 人")
                    cast = sorted((p for p in cast if p.get("name") and
                                   (not options["no_avatar"] or p.get("profile_path"))),
                                  key=lambda p: p.get("order") if p.get("order") is not None else 999)
                    people = [{"Name": p["name"], "Type": "Actor",
                               "Role": p.get("role") or ""}
                              for p in cast[:options["max_actors"]]]
                    if people and options["ai_enabled"] and options["ai_credits"]:
                        translations = {}
                        translated_names = translated_roles = 0
                        for index, person in enumerate(people):
                            if re.search(r"[A-Za-z]", person["Name"]):
                                translations[f"n{index}"] = person["Name"][:130]
                            # A Chinese prefix (e.g. "饰 Shen Run") must not
                            # suppress translation of the remaining English.
                            if re.search(r"[A-Za-z]", person["Role"]):
                                translations[f"r{index}"] = person["Role"][:130]
                            elif not person["Role"] and options["resolve_role"]:
                                translations[f"r{index}"] = f"{person['Name']} 的角色名（若无法确定保持空字符串）"
                        # A partially translated name (e.g. 中文 / English) is
                        # still English. Retry only untranslated fields once;
                        # never replace an actor with an invented or Latin name.
                        pending = translations
                        context = (f"剧集：{str(item.get('Name') or '')[:70]}。"
                                   "将英文演员姓名翻译或音译成简体中文，将英文角色名翻译或音译成简体中文；"
                                   "结果不得保留英文字母。保留已有中文及人物身份，不得编造角色或演员。"
                                   "不确定时返回原文。")
                        for _ in range(2):
                            if not pending:
                                break
                            unresolved = {}
                            for start in range(0, len(pending), 25):
                                chunk = dict(list(pending.items())[start:start + 25])
                                translated = await self._ai_map(chunk, context)
                                for key_name, original in chunk.items():
                                    value = translated.get(key_name, "")
                                    if (value and value != original and
                                            re.search(r"[\u3400-\u9fff]", value) and
                                            not re.search(r"[A-Za-z]", value)):
                                        person = people[int(key_name[1:])]
                                        person["Name" if key_name.startswith("n") else "Role"] = value
                                        if key_name.startswith("n"):
                                            translated_names += 1
                                        else:
                                            translated_roles += 1
                                    elif re.search(r"[A-Za-z]", original):
                                        unresolved[key_name] = original
                            pending = unresolved
                        remaining_names = sum(key.startswith("n") for key in pending)
                        remaining_roles = sum(key.startswith("r") for key in pending)
                        self.log(f"AI 演职人员汉化：姓名 {translated_names} 个、角色 {translated_roles} 个；"
                                 f"仍含英文姓名 {remaining_names} 个、角色 {remaining_roles} 个（未确认译名，保留原文）")
                    if people:
                        if options["role_prefix"]:
                            for person in people:
                                if person["Role"] and not person["Role"].startswith(("饰", "配")):
                                    prefix = "配 " if re.search(r"配音|voice", person["Role"], re.I) else "饰 "
                                    person["Role"] = prefix + person["Role"]
                        update["People"] = people
                        locked = [field for field in update.get("LockedFields") or [] if field != "Cast"]
                        if (len(people) >= options["cast_lock_min"] and
                                all(not self._needs_translation(person["Name"]) for person in people) and
                                (not options["role_prefix"] or all(
                                    person["Role"].startswith(("饰", "配")) for person in people))):
                            locked.append("Cast")
                        update["LockedFields"] = locked
                response = await emby.post(f"Items/{identifier}", json={k: v for k, v in update.items() if v is not None})
                response.raise_for_status()
                self.log({"all": "剧集元数据及演职人员已更新", "metadata": "剧集资料已更新",
                          "credits": "剧集演职人员已更新"}[mode])
                if mode == "credits" and options["episode_cast"]:
                    await self._sync_episode_people(emby, user, identifier, people)
            if mode in ("all", "episodes"):
                episodes = await self._json(emby, f"Users/{user}/Items", {"ParentId": identifier,
                    "Recursive": "true", "IncludeItemTypes": "Episode", "Limit": 5000})
                seasons = {}
                translated_seasons = {}
                series_people = (update.get("People") if mode == "all" else item.get("People")) or []
                changed = 0
                for episode in episodes.get("Items", []):
                    season, number = episode.get("ParentIndexNumber"), episode.get("IndexNumber")
                    if season is None or number is None: continue
                    if season not in seasons:
                        data = await self._tmdb_json(tmdb, f"tv/{tmdb_id}/season/{season}",
                            {"api_key": api_key, "language": "zh-CN"})
                        seasons[season] = {ep.get("episode_number"): ep for ep in data.get("episodes", [])}
                        if options["metadata_source"] == "douban":
                            for ep_number, db_ep in (await self._douban_data(item, season)).items():
                                fallback = seasons[season].get(ep_number) or {}
                                seasons[season][ep_number] = {
                                    "name": db_ep.get("name") or fallback.get("name"),
                                    "overview": db_ep.get("overview") or fallback.get("overview")}
                        if options["ai_enabled"] and (options["ai_title"] or options["ai_overview"]):
                            values = {}
                            emby_numbers = {entry.get("IndexNumber") for entry in episodes.get("Items", [])
                                            if entry.get("ParentIndexNumber") == season}
                            for number_key, entry in seasons[season].items():
                                if number_key not in emby_numbers or not isinstance(number_key, int):
                                    continue
                                if options["ai_title"] and re.search(r"[A-Za-z]", str(entry.get("name") or "")):
                                    values[f"n{number_key}"] = str(entry["name"])[:160]
                                if options["ai_overview"] and re.search(r"[A-Za-z]", str(entry.get("overview") or "")):
                                    values[f"o{number_key}"] = str(entry["overview"])[:700]
                            translated_seasons[season] = {}
                            pairs = list(values.items())
                            for offset in range(0, len(pairs), 16):
                                proposals = await self._ai_map(
                                    dict(pairs[offset:offset + 16]), f"剧集：{str(item.get('Name') or '')[:70]}；第 {season} 季分集标题及简介")
                                translated_seasons[season].update({key: value for key, value in proposals.items()
                                    if re.search(r"[\u3400-\u9fff]", value) and not re.search(r"[A-Za-z]", value)})
                    tmdb_episode = seasons[season].get(number) or {}
                    if not tmdb_episode.get("name") and not tmdb_episode.get("overview") and not (options["episode_cast"] and series_people):
                        continue
                    full = await self._item(emby, user, episode["Id"])
                    translated = translated_seasons.get(season, {})
                    if tmdb_episode.get("name"): full["Name"] = translated.get(f"n{number}", tmdb_episode["name"])
                    if tmdb_episode.get("overview"): full["Overview"] = translated.get(f"o{number}", tmdb_episode["overview"])
                    if options["episode_cast"] and series_people:
                        full["People"] = series_people
                    response = await emby.post(f"Items/{episode['Id']}", json={k: v for k, v in full.items() if v is not None})
                    response.raise_for_status()
                    changed += 1
                    if changed % 20 == 0: self.log(f"已补全 {changed} 集")
                self.log(f"分集信息补全完成：{changed} 集")

    def start_mediainfo_check(self):
        return self._begin("媒体信息检查", self._check_mediainfo)

    @staticmethod
    def _skip_iso_runtime(path, size, runtime):
        """Don't repeatedly flag ISO STRMs that only lack probeable runtime."""
        return (bool(size) and not runtime and
                str(path or "").lower().endswith(".iso.strm"))

    async def _check_mediainfo(self):
        results, total, skipped = [], 0, 0
        for name in self._services():
            async with self._server(name) as emby:
                offset = 0
                while True:
                    data = await self._json(emby, "Items", {"IncludeItemTypes": "Movie,Episode",
                        "Recursive": "true", "Fields": "Path,Size,MediaSources",
                        "StartIndex": offset, "Limit": 500})
                    page = data.get("Items") or []
                    for item in page:
                        source = (item.get("MediaSources") or [{}])[0]
                        size = item.get("Size") or source.get("Size") or 0
                        runtime = item.get("RunTimeTicks") or source.get("RunTimeTicks") or 0
                        path = item.get("Path") or source.get("Path")
                        if self._skip_iso_runtime(path, size, runtime):
                            skipped += 1
                            continue
                        total += 1
                        if not size or not runtime:
                            try: path = self._safe_strm(path)
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
        self.log(f"检查完成：共 {total} 项，需要补全 {len(results)} 项；跳过 {skipped} 项已知大小但缺时长的 ISO STRM")

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

    async def _trigger_keeper_thumbnails(self, emby):
        """Run MediaInfoKeeper's configured metadata/thumbnail task when present."""
        try:
            tasks = await self._json(emby, "ScheduledTasks")
            candidates = []
            for task in tasks if isinstance(tasks, list) else []:
                text = " ".join(str(task.get(key) or "") for key in ("Key", "Name", "Description"))
                normalized = re.sub(r"\s+", "", text).lower()
                if ("mediainfokeeper" in normalized and "refresh" in normalized and
                        ("recentmetadata" in normalized or "thumbnail" in normalized or "image" in normalized)):
                    candidates.append(task)
            if not candidates:
                self.log("未找到 MediaInfoKeeper 缩略图补全任务，跳过缺失图片补全")
                return
            task = candidates[0]
            task_id = str(task.get("Id") or "")
            if not task_id:
                self.log("MediaInfoKeeper 缩略图补全任务缺少任务 ID，跳过")
                return
            if str(task.get("State") or "").lower() != "running":
                response = await emby.post(f"ScheduledTasks/Running/{task_id}")
                response.raise_for_status()
                self.log("已触发 MediaInfoKeeper 缩略图补全任务")
            else:
                self.log("MediaInfoKeeper 缩略图补全任务已在运行")
            for _ in range(180):
                await asyncio.sleep(20)
                current = await self._json(emby, f"ScheduledTasks/{task_id}")
                if str(current.get("State") or "").lower() != "running":
                    self.log("MediaInfoKeeper 缩略图补全任务已完成")
                    return
            self.log("MediaInfoKeeper 缩略图补全任务等待超时，继续检测已有图片偏色")
        except (httpx.HTTPError, ValueError) as error:
            self.log(f"触发 MediaInfoKeeper 缩略图补全失败（{type(error).__name__}），跳过")

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
        # Missing thumbnails are handled by Emby/Shenyi; this tool only
        # repairs an existing thumbnail when its pixels show a color cast.
        targets = [(key, target) for key, target in targets
                   if self._preview_status(key, target.get("thumb", "")) == "candidate"]
        if not targets:
            raise ValueError("没有检测到发绿或发紫的已有分集图片")
        return self._begin("分集图片修复", self._repair_preview, series_id, targets, force)

    def start_batch_preview(self, series_ids):
        if not isinstance(series_ids, list) or not 1 <= len(series_ids) <= 20:
            raise ValueError("每次请选择 1 至 20 部剧集批量修复图片")
        if len(set(series_ids)) != len(series_ids):
            raise ValueError("批量剧集不能重复")
        for series_id in series_ids:
            name, _ = self._split(series_id)
            if name not in self._services():
                raise ValueError("所选 Emby 服务器不可用，请刷新剧集列表")
        return self._begin("批量扫描与修复分集图片", self._batch_preview, list(series_ids))

    async def _batch_preview(self, series_ids, titles=None):
        scanned = ok = failed = 0
        for index, series_id in enumerate(series_ids, 1):
            server, series_item_id = self._split(series_id)
            title = (titles or {}).get(series_id) or "未命名剧集"
            self.log(f"[{index}/{len(series_ids)}] 扫描《{title}》（服务器 {server}，Emby 剧集条目 ID {series_item_id}）")
            try:
                episodes = await self.scan_preview(series_id)
                for target in self._preview_selection.values():
                    target["series"] = title if title != "未命名剧集" else target.get("series") or title
                scanned += len(episodes)
                targets = [(entry["id"], self._preview_selection[entry["id"]])
                           for entry in episodes if entry["status"] == "candidate"]
                self.log(f"[{index}/{len(series_ids)}] 共 {len(episodes)} 集，检测到偏色图片 {len(targets)} 集（缺少缩略图跳过）")
                # Prevent a large library from silently launching hundreds of
                # network FFmpeg jobs; the user can rerun a narrower selection.
                if len(targets) > 300:
                    raise ValueError("该剧待修复超过 300 集，请单独选择分集")
                if targets:
                    with self.lock:
                        self.state["preview_result"] = None
                    try:
                        await self._repair_preview(series_id, targets, False)
                    except Exception as exc:
                        self.log(f"[{index}/{len(series_ids)}] 部分分集修复失败：{type(exc).__name__}")
                    finally:
                        summary = self.state.get("preview_result") or {}
                        ok += summary.get("ok", 0)
                        failed += summary.get("fail", 0) or (0 if summary else 1)
            except Exception as exc:
                failed += 1
                self.log(f"[{index}/{len(series_ids)}] 扫描或修复失败：{type(exc).__name__}（可单独重试）")
        with self.lock:
            self.state["batch_result"] = {"scanned": scanned, "ok": ok, "fail": failed}
            self.state["preview_result"] = {"ok": ok, "fail": failed}
        self.log(f"批量图片处理结束：扫描 {scanned} 集，修复 {ok} 集，失败 {failed} 项")
        if self.state["preview_repaired"]:
            self.log("本次已修复的剧集与分集见运行进度下方的修复清单")

    async def _repair_preview(self, series_id, targets, force):
        name, _ = self._split(series_id)
        host_root = await asyncio.to_thread(_frame_host_root)
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
                with tempfile.TemporaryDirectory(prefix="emetools-frame-", dir="/config") as frame_dir:
                    command = ["-v", "error", "-ss", "00:05:00", "-i", url,
                        "-frames:v", "1", "-vf", FRAME_FILTER, "-f", "image2", "-update", "1",
                        "-y", "/out/frame.png"]
                    config = {"Entrypoint": [FRAME_EXECUTABLE], "Cmd": command,
                              "HostConfig": {"NetworkMode": "bridge", "Memory": 536870912,
                                             "Binds": [f"{host_root}/{Path(frame_dir).name}:/out:rw"],
                                             "Devices": [{"PathOnHost": "/dev/dri",
                                                          "PathInContainer": "/dev/dri",
                                                          "CgroupPermissions": "rwm"}]}}
                    await asyncio.to_thread(_docker_job, FRAME_IMAGE, config, timeout=240)
                    frame = Path(frame_dir) / "frame.png"
                    if not frame.is_file() or frame.stat().st_size > 32 * 1024 * 1024:
                        raise ValueError("独立截帧容器未生成有效图片")
                    with Image.open(frame) as image:
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
                episode_id = key.split("::", 1)[1]
                series_title = str(target.get("series") or "未命名剧集").replace("\n", " ")[:90]
                episode_title = str(target.get("name") or "").replace("\n", " ")[:70]
                season = target.get("season")
                episode = target.get("episode")
                code = (f"S{int(season):02d}E{int(episode):02d}" if season is not None and episode is not None
                        else "季集号未知")
                detail = f"《{series_title}》 {code}{' · ' + episode_title if episode_title else ''}（服务器 {name}，Emby 分集 ID {episode_id}）"
                with self.lock:
                    self.state["preview_repaired"].append(detail)
                self.log(f"[{position}/{len(targets)}] 已修复并刷新 Emby：{detail}")
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
