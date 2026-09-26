"""MoviePilot-owned media-version cleanup, ported from MediaEnhance's STRM workflow.

No MediaEnhance service, files or session are read at runtime. The media library
provides scope; the STRM tree provides versions, as in MediaEnhance/dedup.py.
"""

import copy
import functools
import json
import os
import re
import threading
from datetime import datetime

from app.sdk.logging import logger
from app.sdk.services import MediaServerHelper

from .p115 import P115Client


DEFAULT_RULES = [
    {"id": "effect", "name": "特效", "enabled": True, "type": "list", "order": ["dovi", "hdr10+", "hdr", "sdr"]},
    {"id": "resolution", "name": "分辨率", "enabled": True, "type": "list", "order": ["4k", "1080p", "720p", "480p"]},
    {"id": "quality", "name": "质量", "enabled": True, "type": "list", "order": ["remux", "blu-ray", "web-dl", "webrip", "hdtv"]},
    {"id": "fps", "name": "帧率", "enabled": True, "type": "list", "order": ["60", "50", "30", "25", "24"]},
    {"id": "bitrate", "name": "码率", "enabled": True, "type": "numeric", "direction": "desc", "tolerance": 1000000},
    {"id": "codec", "name": "编码", "enabled": True, "type": "list", "order": ["av1", "hevc", "h264", "vp9"]},
    {"id": "size", "name": "大小", "enabled": True, "type": "numeric", "direction": "desc", "tolerance": 0},
]
DEFAULT_CONFIG = {"enabled": False, "cron": "0 3 * * *", "library_ids": [], "rules": DEFAULT_RULES}
COMPANIONS = (".nfo", "-thumb.jpg", ".jpg", ".xml", ".png", ".sub", ".srt", ".ass")
SKIP_DIRS = {".mp-emetools-trash", ".eme-cleanup-trash", "@recycle", "#recycle"}


def validate_rules(rules):
    """Accept only a full permutation of EME's seven known rules."""
    if not isinstance(rules, list) or len(rules) != len(DEFAULT_RULES):
        raise ValueError("清理规则必须包含全部七项")
    known = {item["id"]: item for item in DEFAULT_RULES}
    if {item.get("id") for item in rules if isinstance(item, dict)} != set(known):
        raise ValueError("清理规则包含未知或重复的项目")
    result = []
    for item in rules:
        original = known[item["id"]]
        rule = copy.deepcopy(original)
        rule["enabled"] = bool(item.get("enabled", True))
        if original["type"] == "list":
            order = item.get("order")
            if not isinstance(order, list) or len(order) != len(original["order"]) or set(order) != set(original["order"]):
                raise ValueError("规则选项必须包含所有原始等级")
            rule["order"] = list(order)
        else:
            if item.get("direction", "desc") not in ("asc", "desc"):
                raise ValueError("排序方向无效")
            tolerance = item.get("tolerance", original["tolerance"])
            if type(tolerance) not in (int, float) or tolerance < 0:
                raise ValueError("规则容差必须为非负数")
            rule.update(direction=item.get("direction", "desc"), tolerance=tolerance)
        result.append(rule)
    return result


def parse_name(name):
    text = name.lower()
    fps = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    fps_value = float(fps.group(1)) if fps else 0
    return {
        "resolution": next((value for value, pattern in (("4k", r"2160p|4k|uhd"), ("1080p", r"1080p|fhd"),
                             ("720p", r"720p"), ("480p", r"480p|540p")) if re.search(pattern, text)), "unknown"),
        "effect": next((value for value, pattern in (("dovi", r"dovi|dolby[ _-]?vision|\.dv\."),
                         ("hdr10+", r"hdr10\+|hdr10plus"), ("hdr", r"hdr")) if re.search(pattern, text)), "sdr"),
        "codec": next((value for value, pattern in (("av1", r"av1"), ("hevc", r"hevc|h265|x265|h\.265"),
                        ("h264", r"h264|x264|avc|h\.264"), ("vp9", r"vp9")) if re.search(pattern, text)), "unknown"),
        "quality": next((value for value, pattern in (("remux", r"remux"), ("blu-ray", r"blu-?ray|bdrip|bdr"),
                          ("web-dl", r"web-?dl"), ("webrip", r"web-?rip"), ("hdtv", r"hdtv")) if re.search(pattern, text)), "unknown"),
        "fps": "60" if fps_value >= 55 else "50" if fps_value >= 45 else "30" if fps_value >= 27 else "25" if fps_value >= 24.5 else "24" if fps_value >= 20 else "",
    }


def _media_value(key, raw):
    """Normalize sidecar values into the same ranks used by MediaEnhance."""
    text = str(raw or "").lower().strip()
    if not text:
        return ""
    if key == "resolution":
        match = re.search(r"(\d{3,5})\s*[x×]\s*(\d{3,5})", text)
        if match:
            width = max(int(match.group(1)), int(match.group(2)))
            return "4k" if width >= 3200 else "1080p" if width >= 1800 else "720p" if width >= 1200 else "480p"
        return parse_name(text)[key]
    if key == "fps":
        try:
            rate = text.split("/")
            fps = float(rate[0]) / float(rate[1]) if len(rate) == 2 else float(text)
        except (ValueError, ZeroDivisionError):
            return ""
        return "60" if fps >= 55 else "50" if fps >= 45 else "30" if fps >= 27 else "25" if fps >= 24.5 else "24" if fps >= 20 else ""
    if key == "effect":
        if re.search(r"dovi|dolby.?vision|\bdv\b", text):
            return "dovi"
        if re.search(r"hdr10\+|hdr10plus", text):
            return "hdr10+"
        if re.search(r"hdr|hlg|smpte2084|arib-std-b67|\bpq\b", text):
            return "hdr"
        return "sdr" if re.search(r"sdr|bt709|bt601|bt470", text) else ""
    value = parse_name(text).get(key, "")
    return "" if value == "unknown" else value


def compare_versions(first, second, rules):
    """Use the same first-decisive-rule comparison as MediaEnhance."""
    for rule in rules:
        if not rule["enabled"]:
            continue
        key = rule["id"]
        if rule["type"] == "list":
            order = rule["order"]
            def rank(value):
                return next((len(order) - idx for idx, item in enumerate(order)
                             if item in str(value or "").lower()), 0)
            diff = rank(first.get(key)) - rank(second.get(key))
        else:
            diff = (first.get(key) or 0) - (second.get(key) or 0)
            if abs(diff) <= rule["tolerance"]:
                diff = 0
            if rule["direction"] == "asc":
                diff = -diff
        if diff:
            return 1 if diff > 0 else -1
    return 0


def _group_key(root, folder, name):
    parts = os.path.relpath(folder, root).split(os.sep)
    season_part = next((i for i, part in enumerate(parts) if re.fullmatch(r"season\s*\d+|第\s*\d+\s*季", part, re.I)), None)
    episode = re.search(r"[Ss](\d+)[Ee](\d+)", name)
    if season_part is not None:
        if not episode or not season_part:
            return None
        return f"{parts[season_part - 1]} S{int(episode.group(1)):02}E{int(episode.group(2)):02}"
    if episode:
        return f"{os.path.basename(folder)} S{int(episode.group(1)):02}E{int(episode.group(2)):02}"
    # EME groups movie versions by their enclosing directory, not filename.
    return name if os.path.realpath(folder) == os.path.realpath(root) else os.path.basename(folder)


class MediaCleanup:
    def __init__(self, owner, config):
        self.owner = owner
        self.config = config
        self.lock = threading.Lock()
        self.running = False
        self.progress = ""
        self.last_scan = ""
        self.last_error = ""
        self.result = None
        self._snapshot = {}
        self._scan_rules = None
        self._scan_root = None
        self._scan_inventory = {}

    def libraries(self):
        helper = MediaServerHelper()
        services = helper.get_services(type_filter="emby") or {}
        values = services.values() if isinstance(services, dict) else services
        result = []
        for service in values:
            server = str(service.config.name or "")
            # get_librarys() reads Emby Users/{user}/Views; its Path is often
            # empty. VirtualFolders/Query returns the actual LibraryOptions
            # PathInfos needed to constrain a filesystem scan.
            for library in service.instance.get_emby_virtual_folders() or []:
                paths = library.get("Path") or []
                if isinstance(paths, str):
                    paths = [paths]
                result.append({"id": f"{server}::{library['Id']}", "name": str(library["Name"]),
                               "server": server, "paths": [str(p) for p in paths if p]})
        return result

    def _scope_paths(self, root, selected):
        libraries = self.libraries()
        if not libraries:
            raise ValueError("未获取到 Emby 媒体库路径，请检查 Emby 连接及媒体库配置")
        selected = set(selected or [])
        known = {lib["id"] for lib in libraries}
        if selected - known:
            raise ValueError("所选 Emby 媒体库已不可用，请重新选择")
        paths = []
        for lib in libraries:
            if selected and lib["id"] not in selected:
                continue
            for path in lib["paths"]:
                normalized = os.path.realpath(path)
                if os.path.commonpath([root, normalized]) == root and os.path.isdir(normalized):
                    paths.append(normalized)
        if not paths:
            raise ValueError("所选 Emby 媒体库路径不在 MoviePilot 的 STRM 根目录下；请核对两端挂载路径，未执行扫描")
        names = [lib.get("name") or os.path.basename(str(lib["paths"][0]).rstrip(os.sep)) or lib["id"]
                 for lib in libraries if not selected or lib["id"] in selected]
        label = "、".join(names[:3]) + (f"等 {len(names)} 个" if len(names) > 3 else "")
        self.progress = f"正在扫描 STRM 目录：{label}媒体库"
        return sorted(set(paths))

    @staticmethod
    def _metadata(folder, stem):
        # The same sidecar preference as EME: filename -> MediaInfo JSON ->
        # ShenYi JSON. Only read bounded JSON in the STRM directory.
        version = {}
        for suffix in ("-mediainfo.json", ".mediainfo.json", ".json"):
            path = os.path.join(folder, stem + suffix)
            try:
                if os.path.getsize(path) > 1024 * 1024 or os.path.islink(path):
                    continue
                with open(path, encoding="utf-8") as stream:
                    data = json.load(stream)
                # Emby MediaInfoKeeper exports [{MediaSourceInfo:{MediaStreams:[]}}].
                if isinstance(data, list):
                    first = data[0] if data and isinstance(data[0], dict) else {}
                    data = first.get("MediaSourceInfo") or {}
                if not isinstance(data, dict):
                    continue
                streams = data.get("MediaStreams") or data.get("streams") or data.get("videoStreams") or []
                video = next((item for item in streams if isinstance(item, dict) and
                              str(item.get("Type") or item.get("codec_type") or "video").lower() == "video"), {})
                if not video and isinstance(data.get("video"), dict):
                    video = data["video"]
                fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
                numbers = {
                    "size": data.get("Size") or data.get("size") or fmt.get("size"),
                    "bitrate": data.get("Bitrate") or data.get("BitRate") or data.get("bitrate") or
                               fmt.get("bit_rate") or video.get("BitRate") or video.get("bit_rate"),
                }
                for key, raw in numbers.items():
                    try:
                        number = int(float(raw or 0))
                        if number > 0:
                            version[key] = number
                    except (ValueError, TypeError, OverflowError):
                        continue
                dimensions = (video.get("Width") or video.get("width"), video.get("Height") or video.get("height"))
                raw_values = {
                    "resolution": data.get("resolution") or
                                  (f"{dimensions[0]}x{dimensions[1]}" if all(dimensions) else ""),
                    "effect": data.get("effect") or video.get("VideoRange") or video.get("ColorTransfer") or
                              video.get("hdr_format") or video.get("color_transfer"),
                    "codec": data.get("codec") or video.get("Codec") or video.get("codec_name"),
                    "quality": data.get("quality"),
                    "fps": data.get("fps") or video.get("RealFrameRate") or video.get("VideoFrameRate") or
                           video.get("avg_frame_rate"),
                }
                for key, raw in raw_values.items():
                    normalized = _media_value(key, raw)
                    if normalized:
                        version[key] = normalized
            except (OSError, ValueError, TypeError):
                continue
        return version

    def scan(self, library_ids=None):
        if not self.lock.acquire(blocking=False):
            raise ValueError("媒体清理正在扫描或清理中")
        self.running = True
        self.result = None
        self._snapshot = {}
        self._scan_rules = None
        self._scan_inventory = {}
        self.last_error = ""
        try:
            root = os.path.realpath(self.owner._strm_root)
            if not os.path.isdir(root):
                raise ValueError("STRM 根目录不存在")
            paths = self._scope_paths(root, library_ids)
            grouped = {}
            seen_folders = set()
            for scope in paths:
                for folder, dirs, files in os.walk(scope, followlinks=False):
                    dirs[:] = [name for name in dirs if name not in SKIP_DIRS and not os.path.islink(os.path.join(folder, name))]
                    if folder in seen_folders:
                        continue
                    seen_folders.add(folder)
                    for name in files:
                        if not name.lower().endswith(".strm"):
                            continue
                        path = os.path.join(folder, name)
                        if os.path.islink(path):
                            continue
                        # A library root may contain unrelated loose films: do not
                        # treat every STRM there as a version of the same title.
                        key = name if folder in paths and not re.search(r"[Ss]\d+[Ee]\d+", name) else _group_key(root, folder, name)
                        if not key:
                            continue
                        info = parse_name(name)
                        extra = self._metadata(folder, name[:-5])
                        # File name explicitly marked DoVi must not be downgraded
                        # by its HDR10-compatible base layer in a sidecar.
                        if info["effect"] == "dovi" and extra.get("effect") != "dovi":
                            extra.pop("effect", None)
                        info.update(extra)
                        stat = os.stat(path, follow_symlinks=False)
                        grouped.setdefault((folder, key), []).append({"file_path": path, "file_name": name,
                            "size": 0, "bitrate": 0, "item_id": "", **info,
                            "signature": (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)})
            results, snapshot = [], {}
            rules = validate_rules(self.config["rules"])
            for (_, name), versions in grouped.items():
                if len(versions) < 2:
                    continue
                ordered = sorted(versions, key=functools.cmp_to_key(lambda a, b: compare_versions(a, b, rules)), reverse=True)
                # Only a strictly better best version can justify deletion.
                if compare_versions(ordered[0], ordered[1], rules) <= 0:
                    continue
                for idx, version in enumerate(ordered):
                    version["is_best"] = idx == 0
                    version["best_path"] = ordered[0]["file_path"]
                    snapshot[version["file_path"]] = version
                results.append({"name": name, "versions": [{k: v for k, v in item.items() if k != "signature"}
                                  for item in ordered], "inferior_count": len(ordered) - 1,
                                "total_space_savings": sum(v["size"] for v in ordered[1:])})
            results.sort(key=lambda item: item["total_space_savings"], reverse=True)
            self._snapshot = snapshot
            self._scan_inventory = {folder: {item["file_name"]: item["signature"] for versions in grouped.values()
                                             for item in versions if os.path.dirname(item["file_path"]) == folder}
                                    for folder, _ in grouped}
            self._scan_rules = copy.deepcopy(rules)
            self._scan_root = root
            self.result = {"total_scanned": sum(map(len, grouped.values())), "duplicate_groups": len(results),
                           "total_inferior": sum(item["inferior_count"] for item in results),
                           "total_space_savings": sum(item["total_space_savings"] for item in results), "results": results}
            self.last_scan = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info("增强工具 媒体清理：扫描 %d 个 STRM，重复组=%d，可清理版本=%d", self.result["total_scanned"], len(results), self.result["total_inferior"])
            return self.result
        finally:
            self.running = False
            self.progress = ""
            self.lock.release()

    @staticmethod
    def _file_id(path):
        with open(path, encoding="utf-8", errors="ignore") as stream:
            content = stream.read(1024)
        match = re.search(r"/redirect115/(\d+)(?:/|[?\s\"']|$)", content, re.I)
        if match:
            return match.group(1)
        pick = re.search(r"(?:pickcode|pick_code)=([^&\s\"']+)", content, re.I)
        if pick:
            try:
                from p115pickcode import pickcode_to_id
                return str(pickcode_to_id(pick.group(1)) or "")
            except (ImportError, ValueError):
                return ""
        return ""

    def delete(self, paths):
        """Delete only inferior versions from the current scan, checking file identity."""
        if not self.lock.acquire(blocking=False):
            raise ValueError("媒体清理正在扫描或清理中")
        try:
            root = os.path.realpath(self.owner._strm_root)
            if not self.result or not self._snapshot or not isinstance(paths, list) or not paths:
                raise ValueError("请先扫描并选择低质版本")
            if len(paths) > 1000 or len(paths) != len(set(paths)):
                raise ValueError("清理列表不合法")
            selected = []
            for path in paths:
                item = self._snapshot.get(path)
                if not item or item["is_best"] or os.path.commonpath([root, os.path.realpath(path)]) != root:
                    raise ValueError("仅可清理本次扫描确认的低质版本")
                if validate_rules(self.config["rules"]) != self._scan_rules or root != self._scan_root:
                    raise ValueError("规则或根目录已改变，请重新扫描")
                folder = os.path.dirname(path)
                inventory = self._scan_inventory.get(folder, {})
                current = {name for name in os.listdir(folder) if name.lower().endswith(".strm")}
                if current != set(inventory):
                    raise ValueError("媒体目录中的版本已变化，请重新扫描")
                for name, signature in inventory.items():
                    member = os.path.join(folder, name)
                    st = os.stat(member, follow_symlinks=False)
                    if os.path.islink(member) or (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != signature:
                        raise ValueError("媒体版本已变化，请重新扫描")
                best = self._snapshot.get(item["best_path"])
                if not best or os.path.islink(best["file_path"]) or os.path.commonpath(
                        [root, os.path.realpath(best["file_path"])]) != root:
                    raise ValueError("最佳版本已发生变化，请重新扫描")
                best_stat = os.stat(best["file_path"], follow_symlinks=False)
                if (best_stat.st_dev, best_stat.st_ino, best_stat.st_size, best_stat.st_mtime_ns) != best["signature"]:
                    raise ValueError("最佳版本已发生变化，请重新扫描")
                stat = os.stat(path, follow_symlinks=False)
                if os.path.islink(path) or (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != item["signature"]:
                    raise ValueError("文件已变化，请重新扫描")
                selected.append(item)
            deleted, failures = [], []
            cookie = self.owner._source_cookie()
            # The same cloud-to-local order as MediaEnhance, but never remove
            # the STRM if the 115 API explicitly reports a deletion failure.
            for item in selected:
                path = item["file_path"]
                try:
                    fid = self._file_id(path)
                    if not cookie or not fid:
                        raise ValueError("缺少 115 Cookie 或 STRM 文件 ID，已保留本地文件")
                    if fid and cookie:
                        with P115Client(cookie) as client:
                            response = client.delete_files([fid])
                        if not response.get("state"):
                            raise RuntimeError("115 文件删除失败，已保留 STRM")
                    if not os.path.isfile(path) or os.path.islink(path):
                        raise ValueError("STRM 文件发生变化")
                    base = path[:-5]
                    for target in (path, *(base + suffix for suffix in COMPANIONS)):
                        if os.path.isfile(target) and not os.path.islink(target):
                            os.remove(target)
                    deleted.append({"file_path": path, "size": item["size"], "cloud": bool(fid and cookie)})
                    self._snapshot.pop(path, None)
                    self._scan_inventory.get(os.path.dirname(path), {}).pop(os.path.basename(path), None)
                    logger.info("增强工具 媒体清理：已清理低质版本 %s（115=%s）", os.path.basename(path), bool(fid and cookie))
                except (OSError, RuntimeError, ValueError) as error:
                    failures.append({"file_path": path, "error": str(error)[:160]})
                    logger.warning("增强工具 媒体清理：跳过 %s：%s", os.path.basename(path), type(error).__name__)
            # Removed files must not reappear in the cached scan results.
            if deleted:
                removed = {item["file_path"] for item in deleted}
                for group in self.result["results"]:
                    group["versions"] = [v for v in group["versions"] if v["file_path"] not in removed]
                    group["inferior_count"] = sum(not v["is_best"] for v in group["versions"])
                    group["total_space_savings"] = sum(v["size"] for v in group["versions"] if not v["is_best"])
                self.result["results"] = [g for g in self.result["results"] if g["inferior_count"]]
                self.result["duplicate_groups"] = len(self.result["results"])
                self.result["total_inferior"] = sum(g["inferior_count"] for g in self.result["results"])
                self.result["total_space_savings"] = sum(g["total_space_savings"] for g in self.result["results"])
            return {"ok": not failures, "deleted": deleted, "failures": failures}
        finally:
            self.lock.release()

    def refresh_emby(self):
        """Ask Emby to refresh the library after a successful local deletion."""
        try:
            helper = MediaServerHelper()
            services = helper.get_services(type_filter="emby") or {}
            values = services.values() if isinstance(services, dict) else services
            for service in values:
                if not service.instance.refresh_root_library():
                    logger.warning("增强工具 媒体清理：Emby 媒体库刷新未成功")
        except Exception as error:
            logger.warning("增强工具 媒体清理：Emby 媒体库刷新失败：%s", type(error).__name__)
