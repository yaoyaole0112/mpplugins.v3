"""Isolated STRM metadata cleanup with snapshot verification and quarantine."""

import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from datetime import datetime


EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tbn", ".nfo", ".json", ".xml"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tbn"}
ART = r"(?:poster|folder|cover|default|movie|fanart|backdrop|background|banner|landscape|thumb|logo|clearlogo|clearart|disc|cdart|art)"
QUARANTINE = ".mp-emetools-trash"
EXCLUDED = {QUARANTINE, ".eme-cleanup-trash", ".actors", "@eaDir", "#recycle", ".recycle", ".snapshot", "lost+found"}


class InvalidDataCleaner:
    def __init__(self, strm_root: str):
        self.root = os.path.abspath(strm_root)
        self._lock = threading.Lock()
        self._scans = {}

    def resolve(self, path: str) -> str:
        if self.root == os.sep or os.path.islink(self.root):
            raise ValueError("STRM 根目录不能是系统根目录或符号链接")
        root = os.path.abspath((path or "").strip() or self.root)
        if os.path.commonpath([self.root, root]) != self.root or os.path.realpath(root) != root:
            raise ValueError("仅允许扫描 STRM 根目录内的真实目录")
        if any(part in EXCLUDED or part.casefold() in {"extrafanart", "extrathumbs"}
               for part in os.path.relpath(root, self.root).split(os.sep)):
            raise ValueError("不能扫描隔离区或受保护目录")
        if not os.path.isdir(root):
            raise ValueError("扫描目录不存在：" + root)
        current = root
        while current != self.root:
            if os.path.ismount(current):
                raise ValueError("不能扫描嵌套挂载点")
            current = os.path.dirname(current)
        return root

    @staticmethod
    def _signature(path: str):
        digest = hashlib.sha256()
        count = 0

        def record(target):
            info = os.lstat(target)
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValueError("含符号链接或特殊文件，已跳过")
            digest.update(repr((os.path.relpath(target, path), info.st_dev, info.st_ino,
                                info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)).encode())
            if stat.S_ISREG(info.st_mode) and os.path.splitext(target)[1].casefold() in EXTENSIONS:
                descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)

        def fail(error):
            raise error

        record(path)
        if os.path.isdir(path):
            for folder, dirs, files in os.walk(path, onerror=fail, followlinks=False):
                for name in sorted(dirs + files):
                    target = os.path.join(folder, name)
                    if name in EXCLUDED or os.path.ismount(target):
                        raise ValueError("含受保护目录或挂载点，已跳过")
                    record(target)
                count += len(files)
        else:
            count = 1
        return digest.hexdigest(), count

    @staticmethod
    def _valid(name: str, stems: set, subtree_has_strm: bool) -> bool:
        stem, extension = os.path.splitext(name.casefold())
        if stem in stems:
            return True
        for source in stems:
            if not stem.startswith(source):
                continue
            suffix = stem[len(source):]
            if extension == ".json" and suffix == "-mediainfo":
                return True
            if extension in IMAGE_EXTENSIONS and re.fullmatch(r"[-.]" + ART + r"(?:[- ]?\d+)?", suffix):
                return True
        if not subtree_has_strm:
            return False
        if name.casefold() in {"tvshow.nfo", "season.nfo", "movie.nfo", "series.xml", "season.xml", "movie.xml"}:
            return True
        return extension in IMAGE_EXTENSIONS and bool(re.fullmatch(
            ART + r"(?:[- ]?\d+)?|season(?:\d+|[- ]?all|[- ]?specials)[-.]" + ART, stem))

    def scan(self, path: str, include_metadata: bool = True) -> dict:
        root = self.resolve(path)
        walk, blocked, errors = [], set(), []

        def fail(error):
            blocked.add(os.path.abspath(error.filename or root))
            errors.append({"path": error.filename or root, "message": "目录读取失败，已保护：" + str(error)})

        for folder, dirs, files in os.walk(root, onerror=fail, followlinks=False):
            safe_dirs = []
            for name in dirs:
                target = os.path.join(folder, name)
                if os.path.islink(target) or os.path.ismount(target):
                    blocked.add(folder)
                    errors.append({"path": target, "message": "符号链接或嵌套挂载点，已保护"})
                elif name not in EXCLUDED:
                    safe_dirs.append(name)
            dirs[:] = safe_dirs
            if any(os.path.islink(os.path.join(folder, name)) for name in files):
                blocked.add(folder)
            walk.append((folder, list(dirs), files))
        has_strm, unsafe = {}, {}
        for folder, dirs, files in reversed(walk):
            has_strm[folder] = any(name.casefold().endswith(".strm") for name in files) or any(
                has_strm.get(os.path.join(folder, name), False) for name in dirs)
            unsafe[folder] = folder in blocked or any(unsafe.get(os.path.join(folder, name), True) for name in dirs)
        covered, items = set(), []
        for folder, dirs, files in walk:
            if os.path.dirname(folder) in covered:
                covered.add(folder)
                continue
            if os.path.basename(folder).casefold() in {"extrafanart", "extrathumbs"} and has_strm.get(os.path.dirname(folder)):
                covered.add(folder)
                continue
            if unsafe.get(folder):
                continue
            if folder != root and not has_strm[folder]:
                candidates = [(folder, "directory", "整棵子树无 STRM")]
                covered.add(folder)
            elif include_metadata:
                stems = {os.path.splitext(name)[0].casefold() for name in files if name.casefold().endswith(".strm")}
                candidates = [(os.path.join(folder, name), "metadata", "无对应 STRM 且不是受保护的共享资料")
                              for name in files if os.path.splitext(name)[1].casefold() in EXTENSIONS
                              and not name.startswith("._") and not self._valid(name, stems, has_strm[folder])]
            else:
                candidates = []
            for target, kind, reason in candidates:
                try:
                    signature, count = self._signature(target)
                    items.append({"name": os.path.basename(target), "path": target, "kind": kind,
                                  "reason": reason, "files": count, "signature": signature})
                except (OSError, ValueError) as error:
                    errors.append({"path": target, "message": str(error)})
        return {"root": root, "items": items, "total": max(0, len(walk) - 1), "errors": errors,
                "include_metadata": include_metadata, "root_inode": os.stat(root).st_ino}

    def start_scan(self, path: str) -> dict:
        with self._lock:
            snapshot = self.scan(path)
            token = uuid.uuid4().hex
            now = time.monotonic()
            self._scans = {key: value for key, value in self._scans.items() if now - value[0] <= 1800}
            while len(self._scans) >= 8:
                self._scans.pop(next(iter(self._scans)))
            self._scans[token] = (now, snapshot)
            items = [{key: value for key, value in item.items() if key != "signature"} for item in snapshot["items"]]
            return {"ok": True, "scan_token": token, "root": snapshot["root"], "total": snapshot["total"],
                    "count": len(items), "items": items, "errors": snapshot["errors"],
                    "metadata_count": sum(item["kind"] == "metadata" for item in items),
                    "directory_count": sum(item["kind"] == "directory" for item in items)}

    def quarantine(self, snapshot: dict, paths: list) -> dict:
        root = self.resolve(snapshot["root"])
        if os.stat(root).st_ino != snapshot["root_inode"]:
            raise ValueError("扫描目录已被替换，请重新扫描")
        originals = {item["path"]: item for item in snapshot["items"]}
        current = {item["path"]: item for item in self.scan(root, snapshot["include_metadata"])["items"]}
        success, failed = [], []
        for target in dict.fromkeys(paths):
            previous, latest = originals.get(target), current.get(target)
            if not previous or not latest or previous["signature"] != latest["signature"]:
                failed.append({"path": target, "message": "文件已变化或不在扫描结果中，请重新扫描"})
                continue
            descriptor = None
            try:
                parts = os.path.relpath(target, root).split(os.sep)
                if parts == ["."] or ".." in parts or any(part in EXCLUDED for part in parts):
                    raise ValueError("不允许清理扫描根或受保护路径")
                descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                for component in parts[:-1]:
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                pinned = f"/proc/self/fd/{descriptor}/{parts[-1]}"
                if previous["kind"] == "metadata":
                    names = os.listdir(descriptor)
                    stems = {os.path.splitext(name)[0].casefold() for name in names if name.casefold().endswith(".strm")}
                    has_strm = bool(stems)
                    if not has_strm and self._valid(parts[-1], set(), True):
                        for folder, dirs, files in os.walk(f"/proc/self/fd/{descriptor}", followlinks=False):
                            if any(name.casefold().endswith(".strm") for name in files):
                                has_strm = True
                                break
                    if self._valid(parts[-1], stems, has_strm):
                        raise ValueError("对应 STRM 已出现，已保护")
                signature, count = self._signature(pinned)
                if signature != previous["signature"]:
                    raise ValueError("清理前文件发生变化，请重新扫描")
                trash = os.path.join(root, QUARANTINE)
                if os.path.lexists(trash) and (os.path.islink(trash) or not os.path.isdir(trash) or os.path.ismount(trash)):
                    raise ValueError("隔离目录异常")
                os.makedirs(trash, mode=0o700, exist_ok=True)
                batch = os.path.join(trash, uuid.uuid4().hex)
                os.mkdir(batch, mode=0o700)
                payload = os.path.join(batch, "payload")
                os.mkdir(payload, mode=0o700)
                destination = os.path.join(payload, parts[-1])
                with open(os.path.join(batch, "manifest.json"), "x", encoding="utf-8") as stream:
                    json.dump({"original": target, "stored": destination, "kind": previous["kind"],
                               "files": count, "time": datetime.now().isoformat()}, stream, ensure_ascii=False, indent=2)
                os.rename(parts[-1], destination, src_dir_fd=descriptor)
                success.append({"path": target, "kind": previous["kind"], "quarantine": destination})
            except (OSError, ValueError) as error:
                failed.append({"path": target, "message": "已跳过：" + str(error)})
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        return {"ok": True, "deleted": success, "failed": failed}

    def delete(self, token: str, paths: list) -> dict:
        with self._lock:
            saved = self._scans.get(token)
            if not saved or time.monotonic() - saved[0] > 1800:
                raise ValueError("扫描已失效，请重新扫描")
            return self.quarantine(saved[1], paths)
