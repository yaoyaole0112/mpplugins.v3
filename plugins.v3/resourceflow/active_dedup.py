"""Strict downloader snapshots for cancelling unfinished published duplicates."""

import math
from typing import Optional


def path_key(path: str) -> str:
    return str(path or "").replace("\\", "/").lstrip("./")


def paths_agree(left: str, right: str) -> bool:
    left, right = path_key(left), path_key(right)
    return bool(left and right) and (
        left == right or left.endswith("/" + right) or right.endswith("/" + left)
    )


def _number(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def download_state(torrent, kind: str) -> Optional[str]:
    """Return running/paused only for positively unfinished, stable download states."""
    if kind == "qbittorrent":
        if not isinstance(torrent, dict):
            return None
        progress = _number(torrent.get("progress"))
        state = str(torrent.get("state") or "").casefold()
        paused = {"pauseddl", "stoppeddl"}
        running = {"downloading", "stalleddl", "queueddl", "forceddl"}
    else:
        progress = _number(getattr(torrent, "percent_done", None))
        state = str(getattr(torrent, "status", "")).casefold()
        paused, running = {"stopped"}, {"downloading", "download pending"}
    if progress is None or not 0 <= progress < 1:
        return None
    return "paused" if state in paused else "running" if state in running else None


def download_complete(torrent, kind: str) -> bool:
    progress = (_number(torrent.get("progress")) if kind == "qbittorrent" and isinstance(torrent, dict)
                else _number(getattr(torrent, "percent_done", None)))
    return progress == 1


def download_files(files, kind: str) -> Optional[list[dict]]:
    """Unknown selection/progress or ambiguous file IDs must never authorize mutation."""
    if not files:
        return None
    result, ids, paths = [], set(), set()
    for item in files:
        if kind == "qbittorrent":
            if not isinstance(item, dict):
                return None
            index, name, size = item.get("index", item.get("id")), item.get("name"), item.get("size")
            priority, progress = _number(item.get("priority")), _number(item.get("progress"))
            if priority is None or priority < 0 or progress is None or not 0 <= progress <= 1:
                return None
            wanted, complete = priority > 0, progress == 1
        else:
            index, name, size = getattr(item, "id", None), getattr(item, "name", None), getattr(item, "size", None)
            wanted, completed = getattr(item, "selected", None), _number(getattr(item, "completed", None))
            if not isinstance(wanted, bool) or completed is None or _number(size) is None or not 0 <= completed <= size:
                return None
            complete = completed == size
        if (
            isinstance(index, bool) or not isinstance(index, int) or index < 0
            or not isinstance(name, str) or not path_key(name)
            or _number(size) is None or size < 0
            or index in ids or path_key(name) in paths
        ):
            return None
        ids.add(index)
        paths.add(path_key(name))
        result.append({"id": index, "path": path_key(name), "size": size,
                       "wanted": wanted, "complete": complete})
    return result
