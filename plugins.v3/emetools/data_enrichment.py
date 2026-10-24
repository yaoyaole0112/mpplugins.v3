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
from uuid import uuid4
from urllib.parse import quote
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps
from pypinyin import lazy_pinyin

from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.services import MediaServerHelper
from app.schemas.types import MediaType
from app.runtime.settings import get_runtime_setting
from .tvmao_client import fetch_tvmao_cast
from .baike_cast import fetch_baike_cast
from .episode_plots import fetch_episode_plots, missing_episode_title, missing_episode_overview
from .overview_quality import (overview_issues, overview_length, overview_signature,
                               duplicate_overviews)
from .media_precheck import media_checks
from .emby_guard import EmbyClient, ReadRetryClient, ENRICHMENT_SLOT, RESOLVE_SLOTS


FRAME_FILTER = ("libplacebo=w=1280:h=720:force_original_aspect_ratio=decrease:force_divisible_by=2:"
                "apply_dolbyvision=true:tonemapping=bt.2390:color_primaries=bt709:"
                "color_trc=bt709:colorspace=bt709:format=yuv420p")
FRAME_IMAGE = "jellyfin/jellyfin:latest"
FRAME_EXECUTABLE = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
DOCKER_SOCKET = "/var/run/docker.sock"
SHENYI_EXTRACT = "f84e5d989aa8a8b6ab1a3a8c0848faab"
SHENYI_PERSIST = "f98bb72fe87c19265e4550abc2cad64f"
DEFAULT_ENRICH_CONFIG = {
    "preview_force": False,
    "metadata_source": "tmdb",
    "auto_on_import": True,
    "ai_enabled": False, "no_avatar": True, "episode_cast": False,
    "role_prefix": True, "ai_title": True, "ai_credits": True,
    "ai_overview": False, "resolve_role": True,
    "max_actors": 30, "cast_lock_min": 10,
    "overview_movie_min": 120, "overview_episode_min": 40,
    "overview_english_fallback": True, "overview_lock": False,
}


class PreviewFrameError(ValueError):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def validate_enrich_config(values):
    if not isinstance(values, dict) or set(values) - DEFAULT_ENRICH_CONFIG.keys():
        raise ValueError("补全设置包含不支持的选项")
    result = dict(DEFAULT_ENRICH_CONFIG)
    for key, value in values.items():
        if key == "metadata_source":
            if value not in ("tmdb", "douban"):
                raise ValueError("元数据来源仅支持 TMDB 或豆瓣")
        elif key in ("overview_movie_min", "overview_episode_min"):
            if type(value) is not int or not 0 <= value <= 2000:
                raise ValueError("简介最低字数必须是 0–2000 的整数")
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
    container_name = "emetools-job-" + uuid4().hex
    phase = "创建截帧容器"
    try:
        result = _docker("POST", "/containers/create", payload={"Image": image, **config},
                         params={"name": container_name}, timeout=120)
        container_id = result.json()["Id"]
        phase = "启动截帧容器"
        try:
            _docker("POST", f"/containers/{container_id}/start", timeout=120)
        except httpx.ReadTimeout:
            details = _docker("GET", f"/containers/{container_id}/json", timeout=30).json()
            if details.get("State", {}).get("Status") not in ("running", "exited"):
                raise
        phase = "等待视频截帧完成"
        status = _docker("POST", f"/containers/{container_id}/wait",
                         params={"condition": "not-running"}, timeout=timeout).json()
        phase = "读取截帧容器日志"
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
            oom_killed = False
            if status.get("StatusCode") == 137:
                try:
                    details = _docker("GET", f"/containers/{container_id}/json").json()
                    oom_killed = details.get("State", {}).get("OOMKilled") is True
                except (httpx.HTTPError, ValueError, TypeError):
                    pass
            retryable = False
            if oom_killed:
                hint = "容器内存超限（OOM），请释放宿主机内存后重试"
            elif "VK_ERROR" in diagnostics or "Vulkan device" in diagnostics:
                hint = "Vulkan 不可用，请检查宿主机 /dev/dri 设备映射"
            elif re.search(r"(?:HTTP|Server returned).*?(?:401|403)", diagnostics, re.I):
                hint = "远程视频链接拒绝访问（HTTP 401/403）"
                retryable = True
            elif re.search(r"timed? out|connection (?:reset|refused)|end of file|HTTP.*?5\d\d|Server returned.*?5\d\d|I/O error", diagnostics, re.I):
                hint = "远程视频读取中断或超时"
                retryable = True
            elif "No such filter" in diagnostics:
                hint = "截帧镜像缺少所需 FFmpeg 滤镜"
            elif re.search(r"Invalid (?:color|data)|Error (?:while|during) decoding|corrupt", diagnostics, re.I):
                hint = "视频解码或色彩信息异常，请检查视频源是否完整"
                retryable = True
            elif re.search(r"Cannot allocate memory|out of memory", diagnostics, re.I):
                hint = "解码或滤镜内存分配失败，请释放宿主机内存后重试"
            elif status.get("StatusCode") == 137:
                hint = "截帧进程被强制终止，未确认 OOM；请检查宿主机资源与容器状态"
            elif status.get("StatusCode") == 187:
                hint = "解码或滤镜处理失败，不能仅凭退出码认定为显卡故障"
                retryable = True
            else:
                hint = "请检查视频链接、设备及截帧容器日志"
            raise PreviewFrameError(f"独立截帧容器退出码 {status.get('StatusCode')}：{hint}", retryable=retryable)
        return stdout if binary else stdout.decode("utf-8").strip()
    except httpx.TimeoutException as error:
        raise PreviewFrameError(f"{phase}超时；请检查 Docker 响应、宿主机负载及远程视频连接",
                                retryable=phase == "等待视频截帧完成") from error
    finally:
        try:
            _docker("DELETE", f"/containers/{container_id or container_name}", params={"force": "1"})
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                logger.warning("增强工具 数据补全：临时容器清理失败")
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
        self._verification_incomplete = False
        self._verification_detail = ""
        self._resolve_recent = {}
        records = owner.get_data("enrichment_records") if hasattr(owner, "get_data") else []
        self._records = [entry for entry in records if isinstance(entry, dict)][-200:] if isinstance(records, list) else []
        self.state.update(overview_preview=None, records=list(reversed(self._records)))

    def _ensure_active(self):
        if self._closed:
            raise asyncio.CancelledError("插件已停止，取消旧补全任务")

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

    def queue_import(self, series_id, delay=300):
        name, _ = self._split(series_id)
        if name not in self._services():
            return
        delay = max(30, min(int(delay), 300))
        self._queue_import(series_id, time.time() + delay)
        logger.info("增强工具 数据补全：%s 新分集入库，%d 秒后合并补全", _log_series(series_id), delay)

    def poll_recent_imports(self):
        """Fallback for missed Emby webhooks and missing MoviePilot transfer events."""
        if self._closed or not self.owner._enabled or not self.options()["auto_on_import"]:
            return
        previous = self.owner.get_data("enrichment_import_poll_after")
        try:
            after = float(previous)
        except (TypeError, ValueError):
            after = time.time() - 900
        checked_at = time.time()

        async def scan():
            queued = set()
            for server in self._services():
                async with self._server(server) as client:
                    user = await self._user_id(server, client)
                    response = await self._json(client, f"Users/{user}/Items", {
                        "Recursive": "true", "IncludeItemTypes": "Episode,Movie",
                        "SortBy": "DateCreated", "SortOrder": "Descending",
                        "Fields": "SeriesId,DateCreated", "Limit": 100})
                for episode in response.get("Items") or []:
                    created = str(episode.get("DateCreated") or "").strip()
                    try:
                        created_at = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        continue
                    if created_at <= after:
                        continue
                    identifier = str(episode.get("Id") if episode.get("Type") == "Movie" else episode.get("SeriesId") or "")
                    if re.fullmatch(r"[a-zA-Z0-9-]{1,64}", identifier):
                        queued.add(f"{server}::{identifier}")
            for series_id in queued:
                self.queue_import(series_id, delay=30)
            return len(queued)

        try:
            count = asyncio.run(scan())
            self.owner.save_data("enrichment_import_poll_after", checked_at)
            if count:
                logger.info("增强工具 数据补全：最近入库轮询发现 %d 部电影／剧集，已安排兜底补全", count)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.info("增强工具 数据补全：最近入库轮询失败：%s", type(exc).__name__)

    def queue_import_by_tmdb(self, tmdb_id, media_type="Series"):
        """Fallback for successful MoviePilot transfers when Emby Webhook is unavailable."""
        value = str(tmdb_id or "").strip()
        if not value.isdigit() or media_type not in ("Movie", "Series"):
            return False
        with self.lock:
            now = time.monotonic()
            key = (value, media_type)
            if self._closed or now - self._resolve_recent.get(key, -60) < 60:
                return False
            if not RESOLVE_SLOTS.acquire(blocking=False):
                return False
            self._resolve_recent = {entry: stamp for entry, stamp in self._resolve_recent.items()
                                    if now - stamp < 60}
            self._resolve_recent[key] = now

        def resolve():
            try:
                self._resolve_tmdb_import(value, media_type)
            except asyncio.CancelledError:
                return
            finally:
                RESOLVE_SLOTS.release()

        try:
            threading.Thread(target=resolve, daemon=True).start()
        except BaseException:
            RESOLVE_SLOTS.release()
            raise
        return True

    def _resolve_tmdb_import(self, tmdb_id, media_type="Series"):
        async def resolve():
            for server in self._services():
                try:
                    async with self._server(server) as client:
                        user = await self._user_id(server, client)
                        response = await self._json(client, f"Users/{user}/Items", {
                            "Recursive": "true", "IncludeItemTypes": media_type,
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
                    "preview_repaired": list(self.state["preview_repaired"]),
                    "records": list(reversed(self._records))}

    def options(self):
        return validate_enrich_config(getattr(self.owner, "_enrichment_config", {}) or {})

    @staticmethod
    def _needs_translation(text):
        return bool(text and not re.search(r"[\u3400-\u9fff]", str(text)))

    @staticmethod
    def _is_chinese_title(text):
        value = str(text or "").strip()
        return bool(re.search(r"[\u3400-\u9fff]", value) and not re.search(
            r"[\u3040-\u30ff\u31f0-\u31ff\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7ff\u0e00-\u0e7f]", value))

    @classmethod
    def _select_chinese_title(cls, current, *candidates):
        return next((str(title).strip() for title in candidates if cls._is_chinese_title(title)), current)

    async def _series_title(self, tmdb, tmdb_id, api_key, current, *candidates, media_type="tv"):
        title = self._select_chinese_title(current, *candidates)
        if self._is_chinese_title(title):
            return title
        try:
            result = await self._tmdb_json(tmdb, f"{media_type}/{tmdb_id}/translations", {"api_key": api_key})
            translations = [entry for entry in result.get("translations", [])
                            if entry.get("iso_639_1") == "zh"]
            translations.sort(key=lambda entry: entry.get("iso_3166_1") not in ("CN", "SG"))
            title = self._select_chinese_title(current, *[
                (entry.get("data") or {}).get("title" if media_type == "movie" else "name") for entry in translations])
        except ValueError:
            self.log("TMDB 中文剧名翻译读取失败，保留原有剧名")
        if not self._is_chinese_title(title):
            self.log("未找到可用中文剧名，保留 Emby 原有剧名，避免写入外文标题")
        return title

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

    async def _optional_ai_map(self, mapping, context):
        try:
            return await self._ai_map(mapping, context)
        except ValueError as error:
            self.log(f"AI 翻译不可用（{error}），保留原始资料继续补全")
            return {}

    async def _translate_people(self, people, options, title, media_type):
        if not people or not options["ai_enabled"] or not options["ai_credits"]:
            return
        translations = {}
        translated_names = translated_roles = 0
        for index, person in enumerate(people):
            if re.search(r"[A-Za-z]", person["Name"]):
                translations[f"n{index}"] = person["Name"][:130]
            if re.search(r"[A-Za-z]", person["Role"]):
                translations[f"r{index}"] = person["Role"][:130]
            elif not person["Role"] and options["resolve_role"] and person["Type"] == "Actor":
                translations[f"r{index}"] = f"{person['Name']} 的角色名（若无法确定保持空字符串）"
        pending = translations
        context = (f"{media_type}：{str(title or '')[:70]}。"
                   "将英文演员姓名翻译或音译成简体中文，将英文角色名翻译或音译成简体中文；"
                   "结果不得保留英文字母。保留已有中文及人物身份，不得编造角色或演员。"
                   "不确定时返回原文。")
        for _ in range(2):
            if not pending:
                break
            unresolved = {}
            for start in range(0, len(pending), 25):
                chunk = dict(list(pending.items())[start:start + 25])
                translated = await self._optional_ai_map(chunk, context)
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
        return EmbyClient(base_url=url + "/", headers={"X-Emby-Token": key}, active=self._ensure_active,
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
                                {"Fields": "MediaSources,MediaStreams,Path,Size,ProviderIds,Genres,Tags"})

    @staticmethod
    def _enrichment_title(item):
        """Use the overall title for variety series stored with a season suffix."""
        title = str(item.get("Name") or "").strip()
        values = []
        for field in ("Genres", "Tags"):
            value = item.get(field) or []
            values.extend([value] if isinstance(value, str) else value)
        categories = " ".join(str(value) for value in values)
        if not re.search(r"综艺|真人秀|脱口秀|variety|reality|talk\s*show", categories, re.I):
            return title
        normalized = re.sub(
            r"\s*[-_：:·]?\s*(?:第\s*)?[零〇一二两三四五六七八九十百\d]+\s*季\s*$", "", title)
        return normalized.strip() or title

    @staticmethod
    def _tmdb_client():
        # Use MoviePilot's configured mirror and proxy, not a hard-coded TMDB
        # hostname that may be unreachable from the MoviePilot container.
        configured = str(getattr(settings, "TMDB_API_DOMAIN", "api.themoviedb.org")
                         or "api.themoviedb.org").strip()
        parsed = urlsplit(configured if "://" in configured else f"https://{configured}")
        domain = parsed.netloc or parsed.path.split("/", 1)[0]
        base_path = parsed.path if parsed.netloc else ""
        base_path = re.sub(r"/3/?$", "", base_path.rstrip("/"))
        base_path = "/" + base_path.strip("/") if base_path.strip("/") else ""
        proxies = get_runtime_setting("PROXY", None)
        proxy = proxies.get("https") if isinstance(proxies, dict) else proxies
        return ReadRetryClient(base_url=f"https://{domain}{base_path}/3/", proxy=proxy,
                               service_name="TMDB", timeout=30, trust_env=False)

    async def _tmdb_json(self, client, path, params):
        try:
            return await self._json(client, path, params)
        except httpx.ConnectError:
            raise ValueError("TMDB 连接失败：请检查 MoviePilot 的 TMDB 域名和代理设置") from None
        except httpx.TimeoutException:
            raise ValueError("TMDB 请求超时：请检查 MoviePilot 的 TMDB 域名和代理设置") from None
        except httpx.RequestError as error:
            raise ValueError(f"TMDB 读取失败（{type(error).__name__}），有限重试后仍未恢复；已完成的补全保留，请稍后重试") from None
        except httpx.HTTPStatusError as error:
            # Never include the request URL: the API key is in its query string.
            if error.response.status_code == 404:
                raise ValueError("TMDB 未找到对应数据（HTTP 404），请检查媒体的 TMDB ID 或 MoviePilot 的 TMDB API 地址") from None
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
                directors = []
                cast_needs_mobile = include_cast and (
                    not casts or any(
                        not re.search(r"[\u3400-\u9fff]", str(actor.get("name") or "")) or
                        re.search(r"[A-Za-z]", str(actor.get("role") or ""))
                        for actor in casts if isinstance(actor, dict)))
                if cast_needs_mobile:
                    # j/subject may omit cast; mobile celebrities includes names
                    # and photos, but its generic '演员' is not a character name.
                    try:
                        mobile = await client.get(
                            f"https://m.douban.com/rexxar/api/v2/movie/{subject_id}/celebrities",
                            params={"start": 0, "count": 100},
                            headers={"Referer": "https://m.douban.com/"})
                        mobile.raise_for_status()
                        mobile_data = mobile.json()
                        celebrities = mobile_data.get("actors") or []
                        mobile_cast = [{"name": actor.get("name"), "role": actor.get("character"),
                                        "original_name": actor.get("latin_name"),
                                        "img": (actor.get("avatar") or {}).get("large") or
                                               (actor.get("avatar") or {}).get("normal") or
                                               (actor.get("avatar") or {}).get("small")}
                                       for actor in celebrities if isinstance(actor, dict)]
                        if mobile_cast:
                            casts = mobile_cast
                        directors = [{"name": person.get("name"), "role": "导演",
                                      "original_name": person.get("latin_name"),
                                      "img": (person.get("avatar") or {}).get("large") or
                                             (person.get("avatar") or {}).get("normal")}
                                     for person in mobile_data.get("directors") or []
                                     if isinstance(person, dict) and person.get("name")]
                    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
                        self.log("豆瓣移动端演职人员不可用，使用 TMDB 补全演员")
                self.log(f"已匹配《{name}》· 豆瓣 {subject_id}（缺失字段回退 TMDB）")
                return {"name": str(data.get("title") or ""), "overview": str(data.get("intro") or ""),
                        "casts": casts if isinstance(casts, list) else [],
                        "directors": directors if cast_needs_mobile else []}
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
    def _localize_primary_cast(cast, secondary):
        result = []
        for actor in cast:
            matches = []
            for candidate in secondary:
                merged = DataEnrichment._merge_cast([candidate], [actor])
                if len(merged) == 1:
                    matches.append(merged[0])
            localized = dict(actor)
            if len(matches) == 1:
                matched = matches[0]
                if (not DataEnrichment._is_chinese_title(actor.get("name")) and
                        re.search(r"[\u3400-\u9fff]", matched.get("name") or "")):
                    localized["name"] = matched["name"]
                role = matched.get("role") or ""
                if re.search(r"[\u3400-\u9fff]", role):
                    localized["role"] = role
                if not localized.get("profile_path") and matched.get("profile_path"):
                    localized["profile_path"] = matched["profile_path"]
            result.append(localized)
        return result

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
    def _localized_role(role):
        value = str(role or "").strip()
        prefix = "配" if value.startswith("配") else "饰" if value.startswith("饰") else ""
        value = re.sub(r"^\s*[饰配]\s*", "", value)
        if re.search(r"[\u3400-\u9fff]", value) and re.search(r"[A-Za-z]", value):
            value = re.sub(r"[A-Za-z][A-Za-z0-9 .,'\"-]*(?:\s*/\s*[A-Za-z][A-Za-z0-9 .,'\"-]*)*\s*$", "", value)
            value = value.rstrip(" /｜|,，;；-").strip()
        return f"{prefix} {value}".strip() if prefix and value else value

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

    async def _baike_cast(self, item, media_type='tv'):
        self._ensure_active()
        return await fetch_baike_cast(str(item.get('Name') or ''),
                                     str(item.get('ProductionYear') or ''), media_type,
                                     status=self.log, active=self._ensure_active)

    @staticmethod
    def _merge_baike_cast(cast, secondary):
        result = DataEnrichment._localize_primary_cast(cast, secondary)
        added = 0
        for candidate in secondary:
            if not candidate.get('name'):
                continue
            matches = [actor for actor in cast
                       if len(DataEnrichment._merge_cast([candidate], [actor])) == 1]
            if matches:
                continue
            if any(re.sub(r'\s+', '', str(actor.get('name') or '')).casefold() ==
                   re.sub(r'\s+', '', candidate['name']).casefold() for actor in result):
                continue
            result.append({'name': candidate['name'], 'role': candidate.get('role') or '',
                           'profile_path': candidate.get('img') or candidate.get('profile_path') or ''})
            added += 1
        for position, actor in enumerate(result):
            actor['order'] = position
        return result, added

    async def _supplement_baike_cast(self, cast, item, options, media_type='tv'):
        usable = [actor for actor in cast if not options['no_avatar'] or actor.get('profile_path')]
        if len(usable) >= options['max_actors'] and not self._needs_chinese_cast(cast, options['max_actors']):
            return cast, []
        detail = await self._baike_cast(item, media_type)
        secondary = detail.get('casts') or []
        if secondary:
            cast, added = self._merge_baike_cast(cast, secondary)
            self.log(f'百度百科中文演职补缺：获取完整演员表 {len(secondary)} 人，新增 {added} 人；'
                     '保留主数据源演员及顺序，按设置筛选后写入')
        return cast, detail.get('directors') or []

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
                    data = await self._json(client, f"Users/{user}/Items", {"IncludeItemTypes": "Series,Movie",
                        "SearchTerm": keyword, "Recursive": "true", "Limit": 20})
                results.extend({"id": f"{name}::{item['Id']}", "name": item.get("Name", ""),
                    "year": item.get("ProductionYear") or "", "server": name,
                    "type": item.get("Type") or "Series"}
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
                        "IncludeItemTypes": "Series,Movie", "Recursive": "true",
                        "StartIndex": offset, "Limit": 500})
                    page = data.get("Items") or []
                    results.extend({"id": f"{name}::{item['Id']}",
                                    "name": item.get("Name") or "未命名",
                                    "year": item.get("ProductionYear") or "", "server": name,
                                    "type": item.get("Type") or "Series"}
                                   for item in page if item.get("Id"))
                    offset += len(page)
                    if not page or offset >= int(data.get("TotalRecordCount", offset)):
                        break
        return results

    def tv_libraries(self):
        """Expose TV and movie libraries, scoped by server and Emby view ID."""
        services = MediaServerHelper().get_services(type_filter="emby") or {}
        values = services.values() if isinstance(services, dict) else services
        result = []
        for service in values:
            if not getattr(service, "instance", None):
                continue
            server = str(service.config.name or "")
            for library in service.instance.get_librarys(hidden=False) or []:
                if getattr(library, "type", None) in (MediaType.TV.value, MediaType.MOVIE.value) and getattr(library, "id", None):
                    result.append({"id": f"{server}::{library.id}",
                                   "name": str(library.name or "未命名"), "server": server,
                                   "type": "Movie" if library.type == MediaType.MOVIE.value else "Series"})
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
                        "ParentId": library_id, "IncludeItemTypes": library.get("type", "Series"),
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

    def _selected_tv_libraries(self, library_ids, tv_only=False):
        libraries = [library for library in self.tv_libraries()
                     if not tv_only or library.get("type", "Series") == "Series"]
        known = {library["id"]: library for library in libraries}
        if library_ids is None:
            if not libraries:
                raise ValueError("没有可用的 Emby 电视剧媒体库" if tv_only else "没有可用的 Emby 媒体库")
            return libraries
        if (not isinstance(library_ids, list) or not library_ids or
                len(library_ids) > 200 or len(set(library_ids)) != len(library_ids) or
                any(not isinstance(value, str) or value not in known for value in library_ids)):
            raise ValueError("请选择有效的 Emby 媒体库并重试")
        return [known[value] for value in library_ids]

    def start_library_enrich(self, library_ids):
        libraries = self._selected_tv_libraries(library_ids)
        return self._begin("批量补全媒体数据", self._enrich_libraries, libraries, self.options())

    async def _enrich_libraries(self, libraries, options):
        series_ids = await self._library_series(libraries)
        self.log(f"已读取 {len(libraries)} 个媒体库，共 {len(series_ids)} 部电影或剧集")
        await self._batch_enrich(series_ids, options)

    def start_all_preview(self):
        libraries = self._selected_tv_libraries(None, tv_only=True)
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
            self._ensure_active()
            name, identifier = self._split(series_id)
            self.log(f"[{index}/{len(series_ids)}] 开始补全 {_log_series(series_id)}")
            try:
                await self._enrich(name, identifier, "all", options)
                ok += 1
            except Exception as exc:
                failed += 1
                detail = str(exc).strip()[:160]
                self.log(f"[{index}/{len(series_ids)}] 补全失败：{type(exc).__name__}：{detail or '未提供错误详情'}（可单独重试）")
        with self.lock:
            self.state["batch_result"] = {"ok": ok, "fail": failed}
        self.log(f"批量补全结束：成功 {ok} 部，失败 {failed} 部")

    def start_overview_preview(self, series_id="", library_ids=None):
        if series_id:
            name, _ = self._split(series_id)
            if name not in self._services():
                raise ValueError("所选 Emby 服务器不可用")
            libraries = None
        else:
            libraries = self._selected_tv_libraries(library_ids)
        return self._begin("媒体信息预检", self._overview_preview, series_id, libraries, self.options())

    def start_repair_anomalies(self, series_id, allow_mediainfo=False):
        name, _ = self._split(series_id)
        if name not in self._services():
            raise ValueError("所选 Emby 服务器不可用")
        return self._begin("补全异常信息", self._repair_anomalies, series_id, self.options(), allow_mediainfo)

    async def _repair_anomalies(self, series_id, options, allow_mediainfo=False):
        collected = {}
        with self.lock:
            self.state["repair_result"] = None
            self.state["preview_repaired"] = []
        await self._overview_preview(series_id, None, options, collected)
        if self.state["overview_preview"]["errors"]:
            raise ValueError("所选媒体预检未完成，未执行修复，请检查预检读取错误后重试")
        if series_id not in collected:
            raise ValueError("所选媒体已失效或不再是电影/剧集，未执行修复，请重新搜索选择")
        field_map = {"标题": "Name", "分集标题": "Name", "简介/概要": "Overview", "分集简介": "Overview",
                     "类型": "Genres", "工作室/播出平台": "Studios", "演职员": "People", "分集演职员": "People"}
        scope, images, media = {}, [], []
        for key, (entry, checks) in collected.items():
            problems = {check["field"]: check for check in checks if check["status"] in {"缺失", "疑似异常"}}
            fields = {field_map[field] for field in problems if field in field_map}
            if fields:
                scope[key] = fields
            if "分集图片" in problems:
                detail = problems["分集图片"]
                images.append((key, {"path": entry.get("Path"), "status": "missing" if detail["status"] == "缺失" else "candidate",
                    "series": entry.get("SeriesName"), "name": entry.get("Name"),
                    "season": entry.get("ParentIndexNumber"), "episode": entry.get("IndexNumber")}))
            if "媒体信息" in problems:
                sources = [source for source in entry.get("MediaSources") or [] if isinstance(source, dict)] or [{}]
                source = next((value for value in sources if value.get("Type") in (None, "Default")), sources[0])
                detail = problems["媒体信息"]["detail"]
                media.append({"id": str(entry["Id"]), "server": key.split("::", 1)[0], "name": entry.get("Name"),
                    "path": entry.get("Path") or source.get("Path"), "missing_size": "大小" in detail,
                    "missing_runtime": "时长" in detail})
        failures = []
        try:
            self._repair_scope = scope
            self._repair_failures = failures
            if scope:
                self.log("[1/3] 修复异常资料与分集字段；正常字段不覆盖，遵循补全设置的 AI 范围")
                repair_options = {**options, "episode_cast": options["episode_cast"] or any(
                    "People" in fields and key != series_id for key, fields in scope.items())}
                active_fields = set().union(*scope.values())
                for option, field in (("ai_title", "Name"), ("ai_overview", "Overview"), ("ai_credits", "People")):
                    repair_options[option] = options[option] and field in active_fields
                name, identifier = self._split(series_id)
                parent_fields = scope.get(series_id, set())
                episode_fields = [fields for key, fields in scope.items() if key != series_id]
                parent = collected.get(series_id, ({}, []))[0]
                needs_cast = "People" in parent_fields or any("People" in fields for fields in episode_fields)
                if episode_fields:
                    mode = "episodes" if not parent_fields and (not needs_cast or parent.get("People")) else "all"
                else:
                    mode = "credits" if parent_fields == {"People"} else "all" if needs_cast else "metadata"
                try:
                    await self._enrich(name, identifier, mode, repair_options)
                except Exception as error:
                    failures.append(f"资料修复：{type(error).__name__}")
                    self.log("资料修复未全部完成，继续检查其他类别；详情见逐项补全记录")
            if images:
                self.log(f"[2/3] 串行修复 {len(images)} 集异常图片，正常图片不覆盖")
                image_result = {"ok": 0, "fail": 0}
                for target in images:
                    with self.lock:
                        self.state["preview_result"] = None
                    try:
                        await self._repair_preview(series_id, [target], False, rescan=False)
                    except Exception as error:
                        failures.append(f"图片 {target[0]}：{type(error).__name__}")
                        if not self.state.get("preview_result"):
                            image_result["fail"] += 1
                    current = self.state.get("preview_result") or {}
                    for field in image_result:
                        image_result[field] += current.get(field, 0)
                with self.lock:
                    self.state["preview_result"] = image_result
            if media:
                if allow_mediainfo:
                    self.log(f"[3/3] 仅探测所选媒体的 {len(media)} 项大小/时长；不触发全库 Extract/Persist")
                    safe_media = []
                    for entry in media:
                        try:
                            entry["path"] = self._safe_strm(entry["path"])
                            safe_media.append(entry)
                        except (ValueError, TypeError):
                            failures.append(f"大小/时长 {entry['id']}：非可安全读取的本地 STRM")
                            self.log(f"条目 {entry['id']} 不满足本地 STRM 安全读取条件，保留原值，继续其余条目")
                    try:
                        if safe_media:
                            await self._fill_mediainfo(selection=safe_media)
                    except Exception as error:
                        failures.append(f"大小/时长修复：{type(error).__name__}")
                else:
                    self.log("[3/3] 未授权短暂停止 Emby 写库，跳过大小/时长；其余修复继续")
        finally:
            self._repair_scope = None
            self._repair_failures = None
        self.log("正在重新预检所选媒体，核实修复结果；无可靠来源的内容不编造")
        await self._overview_preview(series_id, None, options)
        result = self.state["overview_preview"]
        with self.lock:
            self.state["repair_result"] = {"remaining": result["issues"], "unknown": result["unknown"],
                "failures": failures, "errors": list(result["errors"]), "media_skipped": len(media) if not allow_mediainfo else 0}
        self.log(f"异常修复流程结束：仍有 {result['issues']} 项疑似问题、{result['unknown']} 项待核实；失败步骤 {len(failures)} 个、复查读取失败 {len(result['errors'])} 处。请查看最新预检结果，不代表全部补齐")

    async def _precheck_image(self, emby, entry):
        if "ImageTags" not in entry:
            return {"field": "分集图片", "status": "待核实", "detail": "接口未返回图片标记，无法判定缺图"}
        if not (entry.get("ImageTags") or {}).get("Primary"):
            return {"field": "分集图片", "status": "缺失", "detail": "Emby 未提供分集主图，可通过修复图片补全"}
        identifier = str(entry.get("Id") or "")
        if not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", identifier):
            return {"field": "分集图片", "status": "待核实", "detail": "分集 ID 无效，未读取图片"}
        self._ensure_active()
        try:
            response = await emby.get(f"Items/{identifier}/Images/Primary", params={
                "maxWidth": 640, "maxHeight": 360, "quality": 85, "format": "jpg"})
            if response.status_code == 404:
                return {"field": "分集图片", "status": "缺失", "detail": "图片标记存在，但图片接口返回 404"}
            response.raise_for_status()
            content_type = str(response.headers.get("content-type") or "").lower()
            if not content_type.startswith("image/") or len(response.content) > 8 * 1024 * 1024:
                return {"field": "分集图片", "status": "待核实", "detail": "图片响应格式异常或超过大小限制"}
            width, height, size_issue = self._preview_dimensions(response.content)
            if not width or not height:
                return {"field": "分集图片", "status": "疑似异常", "detail": "图片无法解码"}
            dovi = bool(re.search(r"dovi|dolby[ -]?vision|\.dv[.\-_ ]", str(entry.get("Path") or ""), re.I))
            cast = self._color_cast(response.content, dovi=dovi)
            issues = (["尺寸比例异常"] if size_issue else []) + (["疑似偏色"] if cast else [])
            return {"field": "分集图片", "status": "疑似异常" if issues else "正常",
                    "detail": "、".join(issues) if issues else "已读取缩略图，未发现明显偏色或比例异常"}
        except (httpx.HTTPError, OSError, ValueError, Image.DecompressionBombError) as error:
            return {"field": "分集图片", "status": "待核实", "detail": f"图片读取失败（{type(error).__name__}），不判定为正常"}

    async def _overview_preview(self, series_id, libraries, options, collected=None):
        result = {"items": [], "checked": 0, "issues": 0, "unknown": 0, "errors": [], "truncated": False,
                  "category_counts": {}}
        self.log("媒体信息预检串行读取 Emby 资料与分集缩略图；不写库、不探测视频、不截帧、不调用 AI，大媒体库建议分批检查")
        with self.lock:
            self.state["overview_preview"] = None
        identifiers = [series_id] if series_id else await self._library_series(libraries)
        for key in identifiers:
            self._ensure_active()
            name, identifier = self._split(key)
            try:
                async with self._server(name) as emby:
                    user = await self._user_id(name, emby)
                    item = await self._item(emby, user, identifier)
                    if item.get("Type") not in {"Movie", "Series"}:
                        continue
                    entries = [item]
                    if item.get("Type") == "Series":
                        offset = 0
                        while True:
                            self._ensure_active()
                            page = await self._json(emby, f"Users/{user}/Items", {
                                "ParentId": identifier, "Recursive": "true", "IncludeItemTypes": "Episode",
                                "Fields": "Overview,People,Path,MediaSources,MediaStreams,Size", "Limit": 100, "StartIndex": offset})
                            episodes = page.get("Items") or []
                            if not episodes and offset < int(page.get("TotalRecordCount", offset)):
                                raise ValueError("分集分页提前返回空列表，预检未完成")
                            entries.extend(episodes)
                            offset += len(episodes)
                            if not episodes or offset >= int(page.get("TotalRecordCount", offset)):
                                break
                    duplicates = duplicate_overviews(entries[1:])
                    for entry in entries:
                        self._ensure_active()
                        episode = entry is not item
                        minimum = options["overview_episode_min" if episode else "overview_movie_min"]
                        duplicated = episode and (entry.get("ParentIndexNumber"),
                                                   overview_signature(entry.get("Overview"))) in duplicates
                        reasons = overview_issues(entry.get("Overview"), minimum, duplicated)
                        checks = media_checks(entry, options, episode=episode, duplicated=duplicated)
                        if episode:
                            checks.append(await self._precheck_image(emby, entry))
                        if collected is not None:
                            collected[f"{name}::{entry.get('Id')}"] = (entry, checks)
                        problems = [check for check in checks if check["status"] in {"缺失", "疑似异常"}]
                        uncertain = [check for check in checks if check["status"] == "待核实"]
                        for check in problems:
                            result["category_counts"][check["field"]] = result["category_counts"].get(check["field"], 0) + 1
                        reasons += [f"{check['field']}：{check['status']}" for check in problems
                                    if check["field"] not in {"简介/概要", "分集简介"}]
                        result["checked"] += 1
                        result["issues"] += bool(problems)
                        result["unknown"] += bool(uncertain)
                        if len(result["items"]) < 200:
                            result["items"].append({"id": f"{name}::{entry.get('Id')}", "server": name,
                                "title": str(item.get("Name") or "未命名媒体")[:100],
                                "episode": f"S{entry.get('ParentIndexNumber')}E{entry.get('IndexNumber')}" if episode else "",
                                "type": "分集" if episode else "电影" if item.get("Type") == "Movie" else "剧集",
                                "length": overview_length(entry.get("Overview")), "reasons": reasons, "checks": checks,
                                "uncertain": bool(uncertain),
                                "plan": "资料问题使用数据补全；图片问题使用修复图片；大小/时长问题使用媒体信息补全。预检不执行修复。"
                                    if problems else "部分项目尚待核实" if uncertain else "已检查项目正常"})
                        else:
                            result["truncated"] = True
                        if result["checked"] % 20 == 0:
                            with self.lock:
                                self.state["overview_preview"] = {**result, "items": list(result["items"]),
                                    "errors": list(result["errors"]), "category_counts": dict(result["category_counts"])}
            except (ValueError, httpx.HTTPError) as error:
                result["errors"].append(f"{_log_series(key)}：读取失败（{type(error).__name__}）")
            with self.lock:
                self.state["overview_preview"] = {**result, "items": list(result["items"]), "errors": list(result["errors"]),
                                                  "category_counts": dict(result["category_counts"])}
        with self.lock:
            self.state["overview_preview"] = result
        self.log(f"媒体信息预检完成：检查 {result['checked']} 项，疑似问题 {result['issues']} 项，待核实 {result['unknown']} 项；仅诊断，不写库、不请求 AI")

    async def _resolve_overview(self, tmdb, path, api_key, current, primary, options, *, minimum,
                                source, duplicated=False):
        current_issues = overview_issues(current, minimum, duplicated)
        primary_issues = overview_issues(primary, minimum)
        text, used_source = str(primary or "").strip(), source
        if (not text or "缺失" in primary_issues or "非中文" in primary_issues or
                ("过短" in primary_issues and not current_issues)):
            text, used_source = str(current or "").strip(), "Emby 原值"
        if (options["ai_enabled"] and options["ai_overview"]
                and overview_issues(text, minimum, duplicated and overview_signature(text) == overview_signature(current))):
            english = primary if "非中文" in primary_issues else ""
            if not english and options["overview_english_fallback"]:
                try:
                    english = (await self._tmdb_json(tmdb, path, {"api_key": api_key, "language": "en-US"})).get("overview") or ""
                except (ValueError, httpx.HTTPError) as error:
                    self.log(f"英文简介读取失败（{type(error).__name__}），保留可用原值")
            if english and self._needs_translation(english):
                translated = await self._optional_ai_map({"Overview": str(english)[:6000]},
                    "仅将提供的简介原文忠实翻译为简体中文；不扩写、不生成剧情、不补充原文没有的事实")
                proposal = str(translated.get("Overview") or "").strip()
                if proposal and self._is_chinese_title(proposal) and (
                        overview_length(proposal) >= overview_length(text) or "非中文" in overview_issues(text, minimum)
                        or duplicated):
                    text, used_source = proposal, "TMDB 英文原文翻译" if not primary or source.startswith("TMDB") else f"{source}原文翻译"
        if not text and primary and "缺失" not in primary_issues:
            text, used_source = str(primary).strip(), source
        if "非中文" in overview_issues(text, 0) and used_source == "TMDB 中文":
            used_source = "TMDB 原文（未汉化）"
        return text, used_source

    @staticmethod
    def _lock_overview(original, update, options):
        if not options["overview_lock"] or not update.get("Overview"):
            return
        if (update.get("Overview") == original.get("Overview") or
                overview_issues(update["Overview"], 0)):
            return
        locked = list(update.get("LockedFields") or [])
        if "Overview" not in locked:
            locked.append("Overview")
        update["LockedFields"] = locked

    def _record_enrichment(self, identifier, status, update, fields, audit, message=""):
        audit = audit or {}
        entry = {"time": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "id": str(identifier)[:64], "server": str(audit.get("server") or "")[:80],
                 "title": str(update.get("Name") or audit.get("title") or identifier)[:120],
                 "type": str(update.get("Type") or ""), "status": status,
                 "source": str(audit.get("source") or "元数据来源")[:80],
                 "fields": list(fields), "message": message[:200]}
        with self.lock:
            self._records.append(entry)
            self._records = self._records[-200:]
            if hasattr(self.owner, "save_data"):
                try:
                    self.owner.save_data("enrichment_records", list(self._records))
                except (OSError, RuntimeError, ValueError):
                    logger.warning("媒体增强：补全结果记录持久化失败，当前进程内记录仍可查看")

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
    def _color_cast(thumb, *, dovi=False):
        """Detect dominant casts and mixed green/magenta casts in DoVi frames."""
        try:
            source = BytesIO(thumb) if isinstance(thumb, (bytes, bytearray)) else thumb
            with Image.open(source) as image:
                rgb = image.convert("RGB")
                width, height = rgb.size
                if width < 32 or height < 32:
                    return ""
                pixels = rgb.load()
                step = max(8, min(width, height) // 64)
                total = green = purple = green_tint = purple_tint = 0
                faint_green = faint_purple = 0
                green_strength = purple_strength = 0.0
                mean_r = mean_g = mean_b = 0.0
                sampled = 0
                tiles = [[0, 0, 0] for _ in range(16)]
                for y in range(step // 2, height, step):
                    for x in range(step // 2, width, step):
                        red, g, blue = pixels[x, y]
                        tile = tiles[min(3, y * 4 // height) * 4 + min(3, x * 4 // width)]
                        tile[0] += 1
                        if g > red + 6 and g > blue + 6:
                            tile[1] += 1
                        elif red > g + 6 and blue > g + 6:
                            tile[2] += 1
                        mean_r += red
                        mean_g += g
                        mean_b += blue
                        sampled += 1
                        if g > red + 6 and g > blue + 6:
                            faint_green += 1
                        elif red > g + 6 and blue > g + 6:
                            faint_purple += 1
                        if g > red + 10 and g > blue + 10:
                            green_tint += 1
                        elif red > g + 10 and blue > g + 8:
                            purple_tint += 1
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
                if sampled < 80:
                    return ""
                active_tiles = [tile for tile in tiles if tile[0]]
                green_spread = sum(tile[1] / tile[0] >= .18 for tile in active_tiles) / len(active_tiles)
                purple_spread = sum(tile[2] / tile[0] >= .18 for tile in active_tiles) / len(active_tiles)
                mixed_spread = sum((tile[1] + tile[2]) / tile[0] >= .12 for tile in active_tiles) / len(active_tiles)
                mean_r /= sampled
                mean_g /= sampled
                mean_b /= sampled
                green_ratio = green / max(total, 1)
                purple_ratio = purple / max(total, 1)
                # 增加整图平均通道偏移作为补充，解决偏色集中在主体区域时漏检。
                green_mean_cast = mean_g - max(mean_r, mean_b)
                purple_mean_cast = (mean_r + mean_b) / 2 - mean_g
                green_tint_ratio = green_tint / max(sampled, 1)
                purple_tint_ratio = purple_tint / max(sampled, 1)
                if (green_spread >= .65 and total >= 80 and green_ratio >= .18 and green_strength / max(green, 1) >= .04
                        and green >= purple + .04 * total
                        and (green_mean_cast >= 3 or green_ratio >= .28)):
                    return "green"
                if (dovi and green_spread >= .65 and green_tint_ratio >= .34 and green_mean_cast >= 2.0 and
                        green_tint_ratio >= purple_tint_ratio + .08):
                    return "green"
                if (purple_spread >= .65 and total >= 80 and purple_ratio >= .18 and purple_strength / max(purple, 1) >= .035
                        and purple >= green + .04 * total
                        and (purple_mean_cast >= 3 or purple_ratio >= .28)):
                    return "purple"
                if (dovi and purple_spread >= .65 and purple_tint_ratio >= .34 and purple_mean_cast >= 2.0 and
                        purple_tint_ratio >= green_tint_ratio + .08):
                    return "purple"
                if (dovi and mixed_spread >= .65 and green_tint_ratio >= .15 and purple_tint_ratio >= .05 and
                        green_tint_ratio + purple_tint_ratio >= .23):
                    return "mixed"
                if (dovi and mixed_spread >= .65 and faint_green / sampled >= .06 and faint_purple / sampled >= .045
                        and (faint_green + faint_purple) / sampled >= .12):
                    return "mixed"
        except (OSError, ValueError):
            pass
        return ""

    @staticmethod
    def _preview_dimensions(thumb):
        try:
            source = BytesIO(thumb) if isinstance(thumb, (bytes, bytearray)) else thumb
            with Image.open(source) as image:
                width, height = image.size
            return width, height, abs(width / max(height, 1) - 16 / 9) > .04
        except (OSError, ValueError):
            return 0, 0, False

    def _preview_status(self, item_id, thumb):
        if not os.path.isfile(thumb):
            return "missing"
        try:
            stat = os.stat(thumb)
            cached = self._preview_cache().get(item_id) or {}
            if cached.get("thumb") == thumb and cached.get("mtime") == int(stat.st_mtime) and cached.get("size") == stat.st_size:
                return "size" if self._preview_dimensions(thumb)[2] else ("pending" if cached.get("synced") is False else "fixed")
        except OSError:
            return "missing"
        dovi = bool(re.search(r"dovi|dolby[ -]?vision|\.dv[.\-_ ]", thumb, re.I))
        if self._color_cast(thumb, dovi=dovi):
            return "candidate"
        return "size" if self._preview_dimensions(thumb)[2] else "keep"

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
                width, height, size_issue = self._preview_dimensions(thumb)
                if status in {"keep", "fixed"}:
                    try:
                        image_response = await client.get(
                            f"Items/{item['Id']}/Images/Primary",
                            params={"quality": 100, "format": "jpg"},
                        )
                        content_type = str(image_response.headers.get("content-type") or "")
                        if image_response.is_success and content_type.lower().startswith("image/"):
                            remote_width, remote_height, remote_size_issue = self._preview_dimensions(image_response.content)
                            if remote_size_issue:
                                width, height, size_issue = remote_width, remote_height, True
                                status = "size"
                            if self._color_cast(image_response.content, dovi=dovi):
                                status = "candidate"
                    except (httpx.HTTPError, OSError, ValueError):
                        # Local STRM thumbnail detection remains the fallback.
                        pass
                result = {"id": key, "name": item.get("Name") or "",
                    "season": item.get("ParentIndexNumber") or 0,
                    "episode": item.get("IndexNumber") or 0,
                    "dovi": dovi, "has_thumb": os.path.isfile(thumb), "status": status,
                    "width": width, "height": height, "size_issue": size_issue}
                results.append(result)
                selected[result["id"]] = {"path": fs_path, "thumb": thumb,
                    "status": status, "size_issue": size_issue, "dovi": dovi, "series_id": series_id,
                    "series": str(item.get("SeriesName") or "").replace("\n", " ")[:90],
                    "season": result["season"], "episode": result["episode"], "name": result["name"]}
        with self.lock:
            self._preview_selection = selected
            self.state["preview"] = results
        return results

    def _begin(self, task, target, *args):
        with self.lock:
            if self._closed:
                raise ValueError("插件已停止，请刷新页面")
            if self.state["running"] or not ENRICHMENT_SLOT.acquire(blocking=False):
                raise ValueError("已有数据补全任务正在运行，请等待完成")
            self.state.update(running=True, done=False, task=task, error="", log=[], repair_result=None)
            if task in ("分集图片修复", "批量扫描与修复分集图片"):
                self.state["preview_repaired"] = []

        def worker():
            try:
                self._ensure_active()
                asyncio.run(target(*args))
            except asyncio.CancelledError:
                self.log("插件停止或重载，旧任务已取消；已完成的写入保留")
            except Exception as error:
                with self.lock:
                    self.state["error"] = f"{type(error).__name__}：{str(error)[:160]}"
                logger.error("增强工具 数据补全 %s 失败：%s：%s",
                             task, type(error).__name__, str(error)[:160])
            finally:
                with self.lock:
                    self.state["running"] = False
                    self.state["done"] = True
                ENRICHMENT_SLOT.release()

        try:
            threading.Thread(target=worker, name=f"eme-enrich-{task}", daemon=True).start()
        except BaseException:
            with self.lock:
                self.state["running"] = False
            ENRICHMENT_SLOT.release()
            raise
        return {"started": True, "message": f"{task}已开始，可在页面查看进度"}

    def start_enrich(self, series_id, mode="all"):
        if mode not in {"all", "metadata", "credits", "episodes"}:
            raise ValueError("未知补全操作")
        name, identifier = self._split(series_id)
        if name not in self._services():
            raise ValueError("所选 Emby 服务器不可用")
        return self._begin("媒体补全", self._enrich, name, identifier, mode, self.options())

    async def _enrich_movie(self, emby, tmdb, identifier, item, mode, options, api_key, user=None, server=""):
        self.log(f"电影主数据源：{'TMDB' if options['metadata_source'] == 'tmdb' else '豆瓣优先'}；演职上限 {options['max_actors']} 人")
        provider = item.get("ProviderIds") or {}
        tmdb_id = provider.get("Tmdb") or provider.get("TMDB")
        if not tmdb_id:
            search = await self._tmdb_json(tmdb, "search/movie", {"api_key": api_key,
                "query": item.get("Name") or "", "language": "zh-CN", "page": 1})
            candidates = search.get("results") or []
            year = str(item.get("ProductionYear") or "")
            matched = [entry for entry in candidates if entry.get("release_date", "")[:4] == year] if year else candidates
            if len(matched) != 1:
                raise ValueError("TMDB 电影匹配不唯一或年份不符，请先在 Emby 设置正确的 TMDB ID")
            tmdb_id = matched[0].get("id")
        if not tmdb_id:
            raise ValueError("未找到对应的 TMDB 电影，不写入 Emby")
        detail = await self._tmdb_json(tmdb, f"movie/{tmdb_id}", {"api_key": api_key,
            "language": "zh-CN", "append_to_response": "credits,external_ids"})
        douban = (await self._douban_data(item, include_cast=mode != "metadata")
                  if options["metadata_source"] == "douban" and mode in ("all", "metadata", "credits") else {})
        update = dict(item)
        overview_source = "Emby 原值"
        if mode != "credits":
            update["Name"] = await self._series_title(
                tmdb, tmdb_id, api_key, item.get("Name"), douban.get("name"), detail.get("title"),
                media_type="movie")
            overview, overview_source = await self._resolve_overview(
                tmdb, f"movie/{tmdb_id}", api_key, item.get("Overview"),
                douban.get("overview") or detail.get("overview"), options,
                minimum=options["overview_movie_min"], source="豆瓣" if douban.get("overview") else "TMDB 中文")
            if overview:
                update["Overview"] = overview
            if detail.get("vote_average"):
                update["CommunityRating"] = detail["vote_average"]
            if detail.get("genres"):
                update["Genres"] = [genre["name"] for genre in detail["genres"] if genre.get("name")]
            studios = [company.get("name") for company in detail.get("production_companies") or []
                       if isinstance(company, dict) and company.get("name")]
            if studios:
                update["Studios"] = [{"Name": name} for name in dict.fromkeys(studios)][:30]
            providers = dict(provider)
            providers["Tmdb"] = str(tmdb_id)
            imdb_id = (detail.get("external_ids") or {}).get("imdb_id") or detail.get("imdb_id")
            if imdb_id:
                providers["Imdb"] = imdb_id
            update["ProviderIds"] = providers
            if options["ai_enabled"]:
                translations = {}
                if options["ai_title"] and update.get("Name") and not self._is_chinese_title(update["Name"]):
                    translations["Name"] = str(update.get("Name") or "")[:180]
                translated = await self._optional_ai_map(translations, "电影标题及剧情简介，译为简体中文，不编造事实")
                update.update({key: value for key, value in translated.items()
                               if self._is_chinese_title(value) and not re.search(r"[A-Za-z]", value)})
        if mode != "metadata":
            credits = detail.get("credits") or {}
            cast = [{"name": actor["name"], "original_name": actor.get("original_name"),
                     "id": actor.get("id"),
                     "role": actor.get("character") or "", "profile_path": actor.get("profile_path"),
                     "order": actor.get("order")}
                    for actor in credits.get("cast") or [] if actor.get("name")]
            douban_cast = douban.get("casts") or []
            if self._needs_chinese_cast(cast, options["max_actors"]) and not douban_cast:
                secondary = await self._douban_data(item, include_cast=True)
                douban_cast = secondary.get("casts") or []
                if douban_cast and options["metadata_source"] == "tmdb":
                    self.log("TMDB 作为电影主数据源，豆瓣仅补充中文演职与角色")
            if douban_cast:
                cast = (self._localize_primary_cast(cast, douban_cast)
                        if options["metadata_source"] == "tmdb" else self._merge_cast(douban_cast, cast))
            if self._needs_chinese_cast(cast, options["max_actors"]):
                tvmao_cast = await self._tvmao_cast(item)
                if tvmao_cast:
                    if options["metadata_source"] == "tmdb":
                        cast = self._localize_primary_cast(cast, tvmao_cast)
                    else:
                        cast, replaced, added = self._merge_tvmao_cast(cast, tvmao_cast)
                        self.log(f"电影电视猫中文演职补缺：汉化姓名 {replaced} 人，新增 {added} 人")
            cast, baike_directors = await self._supplement_baike_cast(cast, item, options, 'movie')
            for actor in cast[:options["max_actors"]]:
                if actor.get("id"):
                    try:
                        person = await self._tmdb_json(tmdb, f"person/{actor['id']}",
                            {"api_key": api_key, "language": "zh-CN"})
                        names = [person.get("name"), *(person.get("also_known_as") or [])]
                        chinese = next((value for value in names if self._is_chinese_title(value)
                                        and not re.search(r"[A-Za-z]", value)), None)
                        if chinese:
                            actor["name"] = chinese
                        if not actor.get("profile_path") and person.get("profile_path"):
                            actor["profile_path"] = person["profile_path"]
                    except ValueError:
                        self.log("TMDB 人物中文别名读取失败，保留姓名继续补全")
            cast = [actor for actor in cast if actor.get("name") and
                    (not options["no_avatar"] or actor.get("profile_path"))]
            people = [{"Name": actor["name"], "Type": "Actor", "Role": actor.get("role") or ""}
                      for actor in cast if actor.get("name")][:options["max_actors"]]
            tmdb_directors = [{"name": crew["name"], "original_name": crew.get("original_name"),
                               "role": "导演", "profile_path": crew.get("profile_path")}
                              for crew in credits.get("crew") or []
                              if crew.get("job") == "Director" and crew.get("name")]
            douban_directors = douban.get("directors") or []
            if douban_directors:
                tmdb_directors = self._merge_cast(douban_directors, tmdb_directors)
            if baike_directors:
                tmdb_directors, added = self._merge_baike_cast(tmdb_directors, baike_directors)
            directors = [{"Name": person["name"], "Type": "Director", "Role": "导演"}
                         for person in tmdb_directors if person.get("name")]
            people = directors[:10] + people
            await self._translate_people(people, options, item.get("Name"), "电影")
            if options["role_prefix"]:
                for person in people:
                    if person["Type"] == "Actor" and person["Role"]:
                        person["Role"] = self._localized_role(person["Role"])
                        if not person["Role"].startswith(("饰", "配")):
                            person["Role"] = "饰 " + person["Role"]
            if people:
                update["People"] = people
                locked = [field for field in update.get("LockedFields") or [] if field != "Cast"]
                actors = [person for person in people if person["Type"] == "Actor"]
                if (len(actors) >= options["cast_lock_min"] and
                        all(not self._needs_translation(person["Name"]) for person in actors) and
                        (not options["role_prefix"] or all(
                            person["Role"].startswith(("饰", "配")) for person in actors))):
                    locked.append("Cast")
                update["LockedFields"] = locked
                unresolved = [person for person in people if re.search(r"[A-Za-z]", person["Name"] + " " + person["Role"])]
                if unresolved:
                    if options["ai_enabled"] and options["ai_credits"]:
                        hint = "AI 演职翻译已开启并执行；部分字段未取得可用中文结果，请检查上方 AI 请求或翻译日志。保留原文，不代表完整汉化"
                    elif options["ai_enabled"]:
                        hint = "AI 已开启，但演职翻译范围未开启"
                    else:
                        hint = "AI 智能补齐未开启，可靠中文资料不足时保留原文"
                    self.log(f"仍有 {len(unresolved)} 位演职的姓名或角色含外文；{hint}")
        if mode != "credits":
            self._lock_overview(item, update, options)
        issues = overview_issues(update.get("Overview"), options["overview_movie_min"]) if mode != "credits" else []
        if not await self._save_item(emby, identifier, item, update, user=user,
                                     audit={"server": server, "source": overview_source, "issues": issues}):
            self.log("电影资料未确认修复，保留失败记录，继续其他类别" if self._verification_detail else "电影资料未变化，跳过重复写入")
            return
        if self._verification_incomplete:
            self.log("电影资料已提交；演职回读不完整，请查看逐项补全记录，未确认全部人员已保存")
        else:
            self.log("电影资料及演职人员已更新" if mode == "all" else "电影资料已更新" if mode == "metadata" else "电影演职人员已更新")

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
                changed += await self._save_item(emby, episode_id, full, {**full, "People": people}, user=user)
                if changed % 20 == 0:
                    self.log(f"已同步 {changed} 集演职人员")
            offset += len(page)
            if not page or offset >= int(data.get("TotalRecordCount", offset)):
                break
        self.log(f"分集演职人员同步完成：{changed} 集")

    @staticmethod
    def _metadata_value(field, value):
        if field == "People":
            return [{key: person.get(key) or "" for key in ("Name", "Type", "Role")}
                    for person in value or []]
        if field == "Studios":
            return sorted({entry.get("Name", "") if isinstance(entry, dict) else str(entry)
                           for entry in value or []})
        if field in {"LockedFields", "Genres"}:
            return sorted(set(value or []))
        if field == "CommunityRating" and isinstance(value, (int, float)):
            return round(value, 1)
        return value

    async def _verify_saved_item(self, emby, identifier, original, payload, changed_fields, user):
        self._verification_detail = ""
        self._verification_incomplete = False
        expected = {field: payload[field] for field in changed_fields if field in payload}
        for field in ("Name", "IndexNumber", "ParentIndexNumber", "ProviderIds"):
            if field in original and field not in expected:
                expected[field] = payload.get(field, original[field])
        for attempt in range(2):
            self._ensure_active()
            current = (await self._item(emby, user, identifier) if user else
                       await self._json(emby, f"Items/{identifier}"))
            mismatches = [field for field, value in expected.items()
                          if not isinstance(current, dict) or
                          self._metadata_value(field, current.get(field)) != self._metadata_value(field, value)]
            if not mismatches:
                return True
            if attempt == 0:
                await asyncio.sleep(0.5)
        if mismatches == ["People"] and isinstance(current, dict):
            visible = self._metadata_value("People", current.get("People"))
            submitted = self._metadata_value("People", payload.get("People"))
            remaining = iter(submitted)
            if visible and len(visible) < len(submitted) and all(
                    any(person == candidate for candidate in remaining) for person in visible):
                self._verification_incomplete = True
                self._verification_detail = (f"演职回读 {len(visible)}/{len(submitted)} 人，"
                    "可能受服务器隐藏人物等规则影响；完整演员表尚未核实，不代表全部已补齐")
                self.log(f"Emby 条目 {identifier} {self._verification_detail}；不重放写入，继续后续补全")
                return True
        self._verification_detail = "不一致字段：" + "、".join(mismatches)
        self.log(f"Emby 条目 {identifier} 回读核验未通过，{self._verification_detail}；不重放写入")
        return False

    @staticmethod
    def _scoped_update(original, update, fields):
        scoped = dict(original)
        for field in fields:
            if update.get(field) is not None:
                scoped[field] = update[field]
        locks = set(original.get("LockedFields") or [])
        proposed = set(update.get("LockedFields") or [])
        for field, lock in (("Overview", "Overview"), ("People", "Cast")):
            if field in fields:
                locks.discard(lock)
                if lock in proposed:
                    locks.add(lock)
        if locks != set(original.get("LockedFields") or []):
            scoped["LockedFields"] = sorted(locks)
        return scoped

    async def _save_item(self, emby, identifier, original, update, *, user=None, audit=None):
        try:
            return await self._save_item_impl(emby, identifier, original, update, user=user, audit=audit)
        except (ValueError, httpx.HTTPError) as error:
            failures = getattr(self, "_repair_failures", None)
            if failures is None:
                raise
            failures.append(f"资料 {identifier}：{type(error).__name__}")
            self._verification_detail = f"写入/核验失败：{type(error).__name__}"
            self.log(f"条目 {identifier} 写入/核验失败，未重放写入，继续其余异常条目；详情见逐项记录")
            return False

    async def _save_item_impl(self, emby, identifier, original, update, *, user=None, audit=None):
        self._ensure_active()
        self._verification_detail = ""
        self._verification_incomplete = False
        scope = getattr(self, "_repair_scope", None)
        if scope is not None:
            fields = scope.get(f"{(audit or {}).get('server')}::{identifier}", set())
            if not fields:
                return False
            update = self._scoped_update(original, update, fields)
        changed_fields = [field for field, value in update.items() if field != "UserData" and value is not None and
                          self._metadata_value(field, original.get(field)) != self._metadata_value(field, value)]
        if not changed_fields:
            issues = (audit or {}).get("issues") or []
            self._record_enrichment(identifier, "partial" if issues else "skipped", update, [], audit,
                                    "字段未变化，未重复写入" + (f"；简介仍有：{'、'.join(issues)}" if issues else ""))
            return False
        payload = {key: value for key, value in update.items() if value is not None}
        payload.pop("UserData", None)
        interrupted = False
        try:
            response = await emby.post(f"Items/{identifier}", json=payload)
            response.raise_for_status()
        except (httpx.RemoteProtocolError, httpx.NetworkError, httpx.TimeoutException):
            interrupted = True
            self.log(f"Emby 条目 {identifier} 写入响应中断，正在只读核验是否已保存；不重放写入")
        except httpx.HTTPError as error:
            self._record_enrichment(identifier, "failed", update, changed_fields, audit,
                                    f"写入失败（{type(error).__name__}），未自动重试")
            raise
        try:
            confirmed = await self._verify_saved_item(emby, identifier, original, payload, changed_fields, user)
            if interrupted and self._verification_incomplete:
                confirmed = False
                self._verification_detail = "写入响应中断；" + self._verification_detail
        except (httpx.HTTPError, ValueError) as error:
            confirmed = False
            self._verification_detail = f"回读异常：{type(error).__name__}"
        if not confirmed:
            message = "写入后回读不一致或读取失败，未能确认保存结果；不重放写入，请稍后核对"
            if self._verification_detail:
                message += f"（{self._verification_detail}）"
            self._record_enrichment(identifier, "failed", update, changed_fields, audit, message)
            raise ValueError(f"Emby 条目 {identifier} {message}")
        if interrupted and not self._verification_incomplete:
            self.log(f"Emby 条目 {identifier} 已核验保存成功，继续补全")
        issues = (audit or {}).get("issues") or []
        message = (self._verification_detail if self._verification_incomplete else "回读验证通过")
        self._record_enrichment(identifier, "partial" if issues or self._verification_incomplete else "updated",
                                update, changed_fields, audit,
                                message + (f"；简介仍有：{'、'.join(issues)}" if issues else ""))
        await asyncio.sleep(0.5)
        return True

    async def _enrich(self, name, identifier, mode, options=None):
        before = self._records[-1] if self._records else None
        try:
            return await self._enrich_impl(name, identifier, mode, options)
        except (ValueError, httpx.HTTPError) as error:
            latest = self._records[-1] if self._records else None
            if latest is before or latest.get("status") != "failed":
                self._record_enrichment(identifier, "failed", {}, [], {"server": name},
                                        f"补全未完成（{type(error).__name__}），请查看运行进度")
            raise

    async def _enrich_impl(self, name, identifier, mode, options=None):
        options = validate_enrich_config(options or self.options())
        if mode == "all" and getattr(self, "_repair_scope", None) is None:
            options.update(episode_cast=True, ai_title=True, ai_credits=True)
        api_key = str(getattr(settings, "TMDB_API_KEY", "") or "")
        if not api_key:
            raise ValueError("请先在 MoviePilot 配置 TMDB API Key")
        async with self._server(name) as emby, self._tmdb_client() as tmdb:
            user = await self._user_id(name, emby)
            item = await self._item(emby, user, identifier)
            original_item = dict(item)
            if item.get("Type") == "Movie":
                if mode == "episodes":
                    raise ValueError("电影没有分集信息")
                await self._enrich_movie(emby, tmdb, identifier, item, mode, options, api_key, user=user, server=name)
                return
            if item.get("Type") != "Series":
                raise ValueError("选中项目不是 Emby 电影或剧集")
            original_title = str(item.get("Name") or "").strip()
            enrichment_title = self._enrichment_title(item)
            if enrichment_title != item.get("Name"):
                self.log(f"综艺按整体名称《{enrichment_title}》补全，不使用季标题")
                item = {**item, "Name": enrichment_title}
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
                    matched_title = await self._series_title(
                        tmdb, tmdb_id, api_key, item.get("Name"),
                        douban_detail.get("name"), detail.get("name"))
                    update["Name"] = enrichment_title if enrichment_title != original_title else matched_title
                    overview, overview_source = await self._resolve_overview(
                        tmdb, f"tv/{tmdb_id}", api_key, item.get("Overview"),
                        douban_detail.get("overview") or detail.get("overview"), options,
                        minimum=options["overview_movie_min"], source="豆瓣" if douban_detail.get("overview") else "TMDB 中文")
                    if overview:
                        update["Overview"] = overview
                    if detail.get("vote_average"): update["CommunityRating"] = detail["vote_average"]
                    if detail.get("genres"): update["Genres"] = [g["name"] for g in detail["genres"]]
                    studios = [entry.get("name") for entry in
                               (detail.get("networks") or []) + (detail.get("production_companies") or [])
                               if isinstance(entry, dict) and entry.get("name")]
                    if studios:
                        update["Studios"] = [{"Name": name} for name in dict.fromkeys(studios)][:30]
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
                        if options["ai_title"] and update.get("Name") and not self._is_chinese_title(update["Name"]):
                            translations["Name"] = update["Name"][:180]
                        if translations:
                            translated = await self._optional_ai_map(translations, "剧集标题和剧情简介；将韩文、日文、泰文、英文等外文译为简体中文，保留原有事实，不编造剧名")
                            update.update({key: value for key, value in translated.items()
                                           if self._is_chinese_title(value)
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
                    cast, baike_directors = await self._supplement_baike_cast(cast, item, options)
                    cast = sorted((p for p in cast if p.get("name") and
                                   (not options["no_avatar"] or p.get("profile_path"))),
                                  key=lambda p: p.get("order") if p.get("order") is not None else 999)
                    people = [{"Name": p["name"], "Type": "Actor",
                               "Role": p.get("role") or ""}
                              for p in cast[:options["max_actors"]]]
                    people.extend({'Name': person['name'], 'Type': 'Director', 'Role': '导演'}
                                  for person in baike_directors[:10] if person.get('name'))
                    await self._translate_people(people, options, item.get("Name"), "剧集")
                    if people:
                        if options["role_prefix"]:
                            for person in people:
                                if person['Type'] == 'Actor' and person["Role"] and not person["Role"].startswith(("饰", "配")):
                                    prefix = "配 " if re.search(r"配音|voice", person["Role"], re.I) else "饰 "
                                    person["Role"] = prefix + person["Role"]
                        update["People"] = people
                        locked = [field for field in update.get("LockedFields") or [] if field != "Cast"]
                        if (sum(person['Type'] == 'Actor' for person in people) >= options["cast_lock_min"] and
                                all(not self._needs_translation(person["Name"]) for person in people) and
                                (not options["role_prefix"] or all(
                                    person['Type'] != 'Actor' or person["Role"].startswith(("饰", "配")) for person in people))):
                            locked.append("Cast")
                        update["LockedFields"] = locked
                if mode != "credits":
                    self._lock_overview(original_item, update, options)
                saved = await self._save_item(emby, identifier, original_item, update, user=user,
                    audit={"server": name, "source": overview_source if mode != "credits" else "演职来源",
                           "issues": overview_issues(update.get("Overview"), options["overview_movie_min"]) if mode != "credits" else []})
                if saved and self._verification_incomplete:
                    self.log("剧集资料已提交；演职回读不完整，请查看逐项补全记录，继续分集补全")
                else:
                    self.log({"all": "剧集元数据及演职人员已更新", "metadata": "剧集资料已更新",
                              "credits": "剧集演职人员已更新"}[mode] if saved else
                             "剧集资料未确认修复，保留失败记录，继续分集与其他类别" if self._verification_detail else
                             "剧集资料未变化，跳过重复写入")
                if mode == "credits" and options["episode_cast"] and getattr(self, "_repair_scope", None) is None:
                    await self._sync_episode_people(emby, user, identifier, people)
            if mode in ("all", "episodes"):
                episodes = {"Items": []}
                offset = 0
                while True:
                    page = await self._json(emby, f"Users/{user}/Items", {"ParentId": identifier,
                        "Recursive": "true", "IncludeItemTypes": "Episode", "Limit": 300,
                        "StartIndex": offset, "Fields": "Overview"})
                    items = page.get("Items") or []
                    episodes["Items"].extend(items)
                    offset += len(items)
                    if not items or offset >= int(page.get("TotalRecordCount", offset)):
                        break
                seasons = {}
                translated_seasons = {}
                web_seasons = {}
                series_people = (update.get("People") if mode == "all" else item.get("People")) or []
                duplicates = duplicate_overviews(episodes.get("Items", []))
                changed = 0
                missing_titles = missing_overviews = 0
                for episode in episodes.get("Items", []):
                    scope = getattr(self, "_repair_scope", None)
                    if scope is not None and f"{name}::{episode['Id']}" not in scope:
                        continue
                    season, number = episode.get("ParentIndexNumber"), episode.get("IndexNumber")
                    if season is None or number is None: continue
                    if scope is not None and scope.get(f"{name}::{episode['Id']}") == {"People"}:
                        if series_people:
                            full = await self._item(emby, user, episode["Id"])
                            changed += await self._save_item(emby, episode["Id"], full,
                                {**full, "People": series_people}, user=user,
                                audit={"server": name, "source": "整剧演职资料"})
                        else:
                            self.log(f"S{season:02d}E{number:02d} 暂无可靠整剧演职资料，保留原值")
                        continue
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
                                            if entry.get("ParentIndexNumber") == season and (scope is None or
                                                "Name" in scope.get(f"{name}::{entry['Id']}", set()))}
                            for number_key, entry in seasons[season].items():
                                if number_key not in emby_numbers or not isinstance(number_key, int):
                                    continue
                                if options["ai_title"] and entry.get("name") and not self._is_chinese_title(entry["name"]):
                                    values[f"n{number_key}"] = str(entry["name"])[:160]
                            translated_seasons[season] = {}
                            pairs = list(values.items())
                            for offset in range(0, len(pairs), 16):
                                proposals = await self._optional_ai_map(
                                    dict(pairs[offset:offset + 16]), f"剧集：{str(item.get('Name') or '')[:70]}；第 {season} 季分集标题及简介")
                                translated_seasons[season].update({key: value for key, value in proposals.items()
                                    if self._is_chinese_title(value) and not re.search(r"[A-Za-z]", value)})
                    tmdb_episode = seasons[season].get(number) or {}
                    duplicated = (season, overview_signature(episode.get("Overview"))) in duplicates
                    if (missing_episode_title(tmdb_episode.get("name")) and missing_episode_overview(tmdb_episode.get("overview"))
                            and not (options["episode_cast"] and series_people)
                            and not overview_issues(episode.get("Overview"), options["overview_episode_min"], duplicated)
                            and not missing_episode_title(episode.get("Name"))
                            and not missing_episode_overview(episode.get("Overview"))):
                        continue
                    full = await self._item(emby, user, episode["Id"])
                    original_episode = dict(full)
                    translated = translated_seasons.get(season, {})
                    overview_source = "Emby 原值"
                    if not missing_episode_title(tmdb_episode.get("name")):
                        full["Name"] = self._select_chinese_title(
                            full.get("Name"), translated.get(f"n{number}"), tmdb_episode["name"])
                    if not missing_episode_overview(tmdb_episode.get("overview")):
                        proposal = tmdb_episode["overview"]
                        if (not self._needs_translation(proposal) or not full.get("Overview")) and (
                                not overview_issues(proposal, options["overview_episode_min"])
                                or overview_issues(full.get("Overview"), options["overview_episode_min"], duplicated)):
                            full["Overview"] = proposal
                            overview_source = "豆瓣/TMDB 分集" if options["metadata_source"] == "douban" else "TMDB 中文"
                    remaining_duplicate = duplicated and overview_signature(full.get("Overview")) == overview_signature(episode.get("Overview"))
                    if missing_episode_title(full.get("Name")) or overview_issues(full.get("Overview"), options["overview_episode_min"], remaining_duplicate):
                        if season not in web_seasons:
                            numbers = {number}
                            for entry in episodes.get("Items", []):
                                if scope is not None and not ({"Name", "Overview"} & scope.get(f"{name}::{entry['Id']}", set())):
                                    continue
                                episode_number = entry.get("IndexNumber")
                                if entry.get("ParentIndexNumber") != season or type(episode_number) is not int:
                                    continue
                                primary = seasons[season].get(episode_number) or {}
                                resolved_title = entry.get("Name")
                                if not missing_episode_title(primary.get("name")):
                                    resolved_title = self._select_chinese_title(resolved_title,
                                        translated.get(f"n{episode_number}"), primary["name"])
                                if (missing_episode_title(resolved_title) or (
                                        overview_issues(entry.get("Overview"), options["overview_episode_min"],
                                            (season, overview_signature(entry.get("Overview"))) in duplicates)
                                        and overview_issues(primary.get("overview"), options["overview_episode_min"]))):
                                    numbers.add(episode_number)
                            plot_item = {**item, "Name": self._select_chinese_title(
                                item.get("Name"), item.get("Name"), detail.get("name"))}
                            web_seasons[season] = await self._web_episode_plots(plot_item, season, numbers)
                        plot = web_seasons[season].get(number) or {}
                        fields = []
                        if missing_episode_title(full.get("Name")) and not missing_episode_title(plot.get("name")):
                            full["Name"] = plot["name"]
                            fields.append("标题")
                        if (plot.get("overview") and overview_issues(full.get("Overview"), options["overview_episode_min"], remaining_duplicate)
                                and (overview_length(plot["overview"]) > overview_length(full.get("Overview"))
                                     or missing_episode_overview(full.get("Overview")) or remaining_duplicate)):
                            full["Overview"] = plot["overview"]
                            overview_source = plot.get("source") or "剧情网页"
                            fields.append("简介")
                        if fields:
                            self.log(f"S{season:02d}E{number:02d} 已从{plot.get('source') or '剧情网页'}补充{'、'.join(fields)}")
                    remaining_duplicate = duplicated and overview_signature(full.get("Overview")) == overview_signature(episode.get("Overview"))
                    web_overview = overview_source not in {"Emby 原值", "TMDB 中文", "豆瓣/TMDB 分集"}
                    preferred_overview = full.get("Overview") if web_overview else tmdb_episode.get("overview")
                    overview_options = ({**options, "overview_english_fallback": False}
                                        if web_overview else options)
                    resolved, resolved_source = await self._resolve_overview(
                        tmdb, f"tv/{tmdb_id}/season/{season}/episode/{number}", api_key,
                        full.get("Overview"), preferred_overview, overview_options,
                        minimum=options["overview_episode_min"], source=overview_source,
                        duplicated=remaining_duplicate)
                    if resolved and ("原文翻译" in resolved_source or
                                     missing_episode_overview(full.get("Overview"))):
                        full["Overview"] = resolved
                        overview_source = resolved_source
                    if options["episode_cast"] and series_people:
                        full["People"] = series_people
                    self._lock_overview(original_episode, full, options)
                    missing_titles += int(missing_episode_title(full.get("Name")))
                    missing_overviews += int(missing_episode_overview(full.get("Overview")))
                    issues = overview_issues(full.get("Overview"), options["overview_episode_min"],
                        duplicated and overview_signature(full.get("Overview")) == overview_signature(episode.get("Overview")))
                    saved = await self._save_item(emby, episode["Id"], original_episode, full, user=user,
                        audit={"server": name, "source": overview_source, "issues": issues})
                    changed += saved
                    if saved and changed % 20 == 0: self.log(f"已补全 {changed} 集")
                self.log(f"分集信息补全完成：{changed} 集")
                if missing_titles or missing_overviews:
                    self.log(f"仍有 {missing_titles} 集缺少有效标题、{missing_overviews} 集缺少简介；来源未覆盖或未可靠匹配，保留原值，不代表已补齐")

    async def _web_episode_plots(self, item, season, numbers):
        self.log(f"正在补充第 {season} 季缺失的分集信息：电视猫优先，百度百科兜底（{len(numbers)} 集）")
        return await fetch_episode_plots(str(item.get("Name") or ""),
                                        str(item.get("ProductionYear") or ""), season, numbers,
                                        self.log, self._ensure_active)

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
        process = None
        for attempt, timeout in enumerate((90, 150), 1):
            try:
                process = await asyncio.to_thread(subprocess.run,
                    ["/usr/local/bin/ffprobe", "-v", "error", "-rw_timeout", "30000000",
                     "-show_entries", "format=duration,size,bit_rate,format_name:stream=codec_type,width,height,duration",
                     "-of", "json", url], capture_output=True, timeout=timeout)
                if process.returncode == 0:
                    break
                error = (process.stderr or b"").decode("utf-8", errors="replace").lower()
                reason = "视频格式或文件头无效" if any(text in error for text in
                    ("invalid data found", "ebml header parsing failed")) else "探测程序返回错误"
                if "403" in error or "401" in error:
                    reason = "视频链接鉴权失败"
                elif "404" in error:
                    reason = "视频源不存在"
                self.log(f"视频探测第 {attempt} 次未成功：{reason}（退出码 {process.returncode}）")
            except subprocess.TimeoutExpired:
                self.log(f"视频探测第 {attempt} 次超时（{timeout} 秒）")
            if attempt == 1:
                self.log("重新读取 STRM 链接后重试视频探测")
                url = self._video_url(item["path"])
                await asyncio.sleep(2)
        if process is not None and process.returncode == 0:
            try:
                data = json.loads(process.stdout or b"{}")
                if not isinstance(data, dict):
                    data = {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.log("视频探测返回无效 JSON，继续尝试 HTTP 文件大小")
                data = {}
            info = data.get("format") or {}
            if item["missing_size"] and str(info.get("size") or "").isdigit():
                fields["Size"] = int(info["size"])
            if item["missing_runtime"]:
                try: fields["RunTimeTicks"] = int(float(info.get("duration") or 0) * 10_000_000)
                except (TypeError, ValueError, OverflowError): pass
                if not fields.get("RunTimeTicks"):
                    durations = []
                    for stream in data.get("streams") or []:
                        if stream.get("codec_type") not in ("video", "audio"):
                            continue
                        try:
                            ticks = int(float(stream.get("duration") or 0) * 10_000_000)
                            if ticks > 0:
                                durations.append(ticks)
                        except (TypeError, ValueError, OverflowError):
                            continue
                    if durations:
                        fields["RunTimeTicks"] = max(durations)
                        self.log("容器未返回总时长，使用视频／音频流中的实际时长")
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
        if item["missing_runtime"] and not fields.get("RunTimeTicks"):
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                    async with client.stream("GET", url, headers={"Range": "bytes=0-255"}) as response:
                        response.raise_for_status()
                        header = b""
                        async for block in response.aiter_bytes(256):
                            header = block
                            break
                        if header.startswith(b"7z\xbc\xaf\x27\x1c"):
                            self.log("源文件实际为 7z 压缩包，非可直接播放视频；请解压真实视频或更换资源后重建 STRM")
                        elif len(header) >= 32 and not any(header):
                            self.log("源文件开头为全零数据，疑似损坏或错误文件；请核对网盘原文件并更换可播放资源")
                        elif header.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                            self.log("视频链接返回 HTML 页面而非视频；请检查链接鉴权或重定向配置")
            except httpx.HTTPError as error:
                self.log(f"读取视频文件头失败：{type(error).__name__}")
        return {key: value for key, value in fields.items() if isinstance(value, str) or value > 0}

    async def _existing_mediainfo(self, item):
        fields = {}
        try:
            async with self._server(item["server"]) as emby:
                user = await self._user_id(item["server"], emby)
                detail = await self._item(emby, user, item["id"])
            sources = [source for source in detail.get("MediaSources") or []
                       if source.get("Path") == item["path"]]
            if detail.get("Path") == item["path"]:
                sources.insert(0, detail)
            for key, missing in (("Size", "missing_size"), ("RunTimeTicks", "missing_runtime")):
                if item[missing]:
                    value = next((source.get(key) for source in sources
                                  if type(source.get(key)) is int and source[key] > 0), None)
                    if value:
                        fields[key] = value
            if fields:
                self.log("从同一文件的 Emby／神医已提取信息取得缺失字段")
        except httpx.HTTPError as error:
            self.log(f"读取 Emby 已有媒体信息失败：{type(error).__name__}，继续视频探测")
        return fields

    async def _fill_mediainfo(self, selection=None):
        # Collect probe data before interrupting Emby. Never guess a database path.
        updates = {}
        scoped = selection is not None
        selection = list(selection if scoped else self._mi_selection)
        servers = {item["server"] for item in selection}
        if len(servers) != 1:
            raise ValueError("仅支持单台 Emby 服务器的安全写库，未修改数据库")
        name = next(iter(servers))
        if not scoped and any(item["missing_size"] and item["missing_runtime"] for item in selection):
            async with self._server(name) as emby:
                await self._scheduled_task(emby, SHENYI_EXTRACT, "Extract")
        for index, item in enumerate(selection, 1):
            self._ensure_active()
            try:
                self.log(f"[{index}/{len(selection)}] 正在读取 {os.path.basename(item['path'])[:120]}；缺失：" +
                         "、".join(label for key, label in (("missing_size", "大小"), ("missing_runtime", "时长"))
                                   if item[key]))
                fields = await self._existing_mediainfo(item)
                pending = {**item, "missing_size": item["missing_size"] and not fields.get("Size"),
                           "missing_runtime": item["missing_runtime"] and not fields.get("RunTimeTicks")}
                if pending["missing_size"] or pending["missing_runtime"]:
                    fields.update(await self._probe(pending))
                if scoped:
                    fields = {key: fields[key] for key, missing in (("Size", "missing_size"), ("RunTimeTicks", "missing_runtime"))
                              if item[missing] and type(fields.get(key)) is int and fields[key] > 0}
                if (item["missing_size"] and fields.get("Size") or
                        item["missing_runtime"] and fields.get("RunTimeTicks")):
                    updates.setdefault(item["server"], {})[item["id"]] = fields
                    remaining = []
                    if item["missing_size"] and not fields.get("Size"):
                        remaining.append("大小")
                    if item["missing_runtime"] and not fields.get("RunTimeTicks"):
                        remaining.append("时长")
                    self.log(f"[{index}/{len(selection)}] 探测成功" +
                             (f"，仍缺{'、'.join(remaining)}" if remaining else ""))
                else:
                    self.log(f"[{index}/{len(selection)}] 未获取到缺失的大小或时长，跳过写库")
            except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
                self.log(f"[{index}/{len(selection)}] 探测失败：{type(error).__name__}")
            if index % 20 == 0: self.log(f"已探测 {index}/{len(selection)} 项")
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
        self._ensure_active()
        self.log(f"已探测 {sum(map(len, updates.values()))} 项；即将暂时停止 Emby、备份写库并重启，此期间网页无法访问")
        changed, backup = await asyncio.to_thread(self._write_emby_database, updates[name])
        self.log(f"数据库已备份：{backup}；已更新 {changed} 项")
        ready = False
        for _ in range(24):
            await asyncio.sleep(5)
            try:
                async with self._server(name) as emby:
                    await self._json(emby, "System/Info/Public")
                    await self._json(emby, "Items", {"IncludeItemTypes": "Movie,Episode",
                        "Recursive": "true", "Fields": "Path,Size,MediaSources", "Limit": 1})
                    if not scoped:
                        await self._scheduled_task(emby, SHENYI_PERSIST, "Persist")
                    ready = True
                    break
            except httpx.HTTPError:
                continue
        if not ready:
            self.log(f"已写入 {changed} 项；Emby 重启后媒体查询尚未恢复，未执行完整复查。请稍后点击开始检查，不要重复写库")
            with self.lock:
                self.state["mediainfo"] = {**(self.state.get("mediainfo") or {}),
                    "updated_count": changed, "recheck_pending": True}
            return
        if scoped:
            return
        try:
            await self._check_mediainfo()
        except httpx.HTTPError:
            self.log(f"已写入 {changed} 项；写库后复查未成功，可能是 Emby 初始化或媒体查询异常。请稍后点击开始检查")
            with self.lock:
                self.state["mediainfo"] = {**(self.state.get("mediainfo") or {}),
                    "updated_count": changed, "recheck_pending": True}

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

    def start_preview_repair(self, series_id, episode_ids, force=False, dimensions_only=False):
        name, _ = self._split(series_id)
        if not isinstance(episode_ids, list) or not episode_ids or len(episode_ids) > 300:
            raise ValueError("请选择 1 至 300 集预览图")
        with self.lock:
            targets = [(key, self._preview_selection[key]) for key in episode_ids
                       if key.startswith(name + "::") and key in self._preview_selection]
        if len(targets) != len(episode_ids) or len(set(episode_ids)) != len(episode_ids):
            raise ValueError("所选分集已失效，请重新扫描")
        if dimensions_only:
            targets = [(key, target) for key, target in targets
                       if target.get("size_issue") or target.get("status") == "size"]
        targets = [(key, target) for key, target in targets
                   if target.get("series_id", series_id) == series_id
                   and (self._preview_status(key, target.get("thumb", "")) == "missing" or
                        target.get("status") in {"candidate", "size", "pending"} or
                        self._preview_status(key, target.get("thumb", "")) in {"candidate", "size", "pending"} or
                        force and target.get("dovi"))]
        if not targets:
            raise ValueError("没有检测到缺失、疑似偏色或尺寸异常的分集图片")
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
                           for entry in episodes if entry["status"] in ("candidate", "missing", "size", "pending")]
                self.log(f"[{index}/{len(series_ids)}] 共 {len(episodes)} 集，统一批量处理：缺图、偏色、尺寸异常或待同步图片 {len(targets)} 集；正常图片不覆盖")
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

    async def _generate_preview(self, path, thumb):
        for attempt, seek in enumerate(("00:05:00", "00:01:00", "00:00:10"), 1):
            self._ensure_active()
            try:
                await self._generate_preview_attempt(path, thumb, seek)
                return
            except PreviewFrameError as error:
                if not error.retryable or attempt == 3:
                    raise
                self.log(f"截帧第 {attempt} 次未成功：{error}；重新读取 STRM，并更换截帧位置后重试")
                await asyncio.sleep(2)

    async def _generate_preview_attempt(self, path, thumb, seek):
        host_root = await asyncio.to_thread(_frame_host_root)
        try:
            await asyncio.to_thread(_docker, "GET", "/images/%s/json" % quote(FRAME_IMAGE, safe=""))
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
            self.log("首次使用正在获取独立截帧镜像，可能需要较长时间")
            await asyncio.to_thread(_docker, "POST", "/images/create",
                                    params={"fromImage": "jellyfin/jellyfin", "tag": "latest"}, timeout=900)
        url = self._video_url(path)
        with tempfile.TemporaryDirectory(prefix="emetools-frame-", dir="/config") as frame_dir:
            output_bind = f"{host_root}/{Path(frame_dir).name}:/out:rw"
            command = ["-v", "error", "-init_hw_device", "vulkan=vk:0", "-filter_hw_device", "vk",
                "-threads", "1", "-filter_threads", "1", "-rw_timeout", "20000000",
                "-ss", seek, "-i", url, "-an", "-sn", "-frames:v", "1",
                "-vf", FRAME_FILTER, "-threads", "1", "-f", "image2", "-update", "1",
                "-y", "/out/frame.png"]
            config = {"Entrypoint": [FRAME_EXECUTABLE], "Cmd": command,
                      "HostConfig": {"NetworkMode": "bridge", "Memory": 1610612736,
                                     "CpuPeriod": 100000, "CpuQuota": 100000,
                                     "Binds": [output_bind],
                                     "Devices": [{"PathOnHost": "/dev/dri",
                                                  "PathInContainer": "/dev/dri",
                                                  "CgroupPermissions": "rwm"}]}}
            await asyncio.to_thread(_docker_job, FRAME_IMAGE, config, timeout=240)
            frame = Path(frame_dir) / "frame.png"
            if not frame.is_file() or frame.stat().st_size > 32 * 1024 * 1024:
                raise PreviewFrameError("独立截帧容器未生成有效图片", retryable=True)
            try:
                with Image.open(frame) as image:
                    rgb = image.convert("RGB")
                    rgb = ImageOps.fit(rgb, (1280, 720), method=Image.Resampling.LANCZOS)
            except (OSError, ValueError):
                raise PreviewFrameError("截帧图片不完整或无法解码", retryable=True) from None
            temporary = thumb + ".emetools-tmp"
            try:
                rgb.save(temporary, format="JPEG", quality=92)
                os.replace(temporary, thumb)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)

    async def _repair_preview(self, series_id, targets, force, rescan=True):
        name, _ = self._split(series_id)
        completed, failed = 0, 0
        for position, (key, target) in enumerate(targets, 1):
            self._ensure_active()
            path = self._safe_strm(target["path"])
            thumb = path[:-5] + "-thumb.jpg"
            status = self._preview_status(key, thumb)
            if not rescan:
                if status in {"keep", "fixed"}:
                    status = "pending"
                elif target.get("status") == "missing" and status in {"candidate", "size"}:
                    target = {**target, "status": status}
            if target.get("status") == "missing" and status not in {"missing", "pending"}:
                self.log(f"[{position}/{len(targets)}] 缩略图已由其他任务生成，跳过覆盖")
                continue
            if not force and status in {"fixed", "keep"} and target.get("status") not in {"candidate", "size"}:
                self.log(f"[{position}/{len(targets)}] 已修复或无需修复，跳过")
                continue
            try:
                phase = "截帧并生成缩略图"
                started = time.monotonic()
                self.log(f"[{position}/{len(targets)}] 正在同步已生成图片" if status == "pending" else f"[{position}/{len(targets)}] 正在截帧并转换杜比色彩")
                if status != "pending":
                    await self._generate_preview(path, thumb)
                    cache = self._preview_cache()
                    stat = os.stat(thumb)
                    cache[key] = {"thumb": thumb, "mtime": int(stat.st_mtime), "size": stat.st_size, "synced": False}
                    self.owner.save_data("enrichment_preview_cache", cache)
                phase = "刷新 Emby 分集"
                self.log(f"[{position}/{len(targets)}] 缩略图已生成，正在刷新 Emby")
                async with self._server(name) as emby:
                    response = await emby.post(f"Items/{key.split('::', 1)[1]}/Refresh",
                                               params={"replaceAllMetadata": "false", "Recursive": "false",
                                                       "MetadataRefreshMode": "ValidationOnly", "ImageRefreshMode": "Default",
                                                       "ReplaceAllImages": "false"})
                    response.raise_for_status()
                phase = "保存图片修复记录"
                cache = self._preview_cache()
                stat = os.stat(thumb)
                cache[key] = {"thumb": thumb, "mtime": int(stat.st_mtime), "size": stat.st_size, "synced": True}
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
                self.log(f"[{position}/{len(targets)}] 已修复并刷新 Emby：{detail}，耗时 {time.monotonic() - started:.1f} 秒")
            except Exception as error:
                failed += 1
                reason = (f"HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError)
                          else str(error) if isinstance(error, PreviewFrameError) else type(error).__name__)
                retry = "；图片已生成，再次点击修复图片仅重试同步，不重新截帧" if phase == "刷新 Emby 分集" else ""
                self.log(f"[{position}/{len(targets)}] Emby 分集 ID {key.split('::', 1)[1]}，"
                         f"S{int(target.get('season') or 0):02d}E{int(target.get('episode') or 0):02d} {phase}失败：{reason}{retry}")
        with self.lock:
            self.state["preview_result"] = {"ok": completed, "fail": failed}
        self.log(f"预览图修复完成：成功 {completed} 集，失败 {failed} 集")
        if rescan:
            try:
                await self.scan_preview(series_id)
            except httpx.HTTPError:
                self.log("刷新分集状态失败，请点击扫描分集重试")
        if failed:
            raise ValueError(f"{failed} 集修复失败，请查看运行进度")
