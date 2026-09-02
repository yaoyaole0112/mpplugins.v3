from pathlib import Path, PurePosixPath
from typing import List, Optional, Set, Tuple


def split_exts(value: Optional[str]) -> Set[str]:
    if not value:
        return set()
    return {
        f".{item.strip().lower().lstrip('.')}"
        for item in str(value).replace("，", ",").split(",")
        if item.strip()
    }


def posix(path: str) -> str:
    return str(path or "").replace("\\", "/").rstrip("/") or "/"


def has_prefix(full_path: str, prefix_path: str) -> bool:
    full = Path(posix(full_path)).parts
    prefix = Path(posix(prefix_path)).parts
    if len(prefix) > len(full):
        return False
    return full[: len(prefix)] == prefix


def parse_path_pairs(text: Optional[str]) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    if not text:
        return pairs
    for line in str(text).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "#" not in line:
            continue
        local_dir, pan_dir = line.split("#", 1)
        local_dir = posix(local_dir.strip())
        pan_dir = posix(pan_dir.strip())
        if local_dir and pan_dir:
            pairs.append((local_dir, pan_dir))
    return pairs


def match_media_path(
    paths: Optional[str], media_path: str
) -> Tuple[bool, Optional[str], Optional[str]]:
    media_path = posix(media_path)
    best: Optional[Tuple[str, str]] = None
    for local_dir, pan_dir in parse_path_pairs(paths):
        if has_prefix(media_path, pan_dir):
            if best is None or len(pan_dir) > len(best[1]):
                best = (local_dir, pan_dir)
    if not best:
        return False, None, None
    return True, best[0], best[1]


def local_mapped_path(local_dir: str, pan_dir: str, pan_path: str) -> Path:
    rel = PurePosixPath(posix(pan_path)).relative_to(PurePosixPath(posix(pan_dir)))
    return Path(local_dir) / Path(*rel.parts)


def local_strm_path(local_dir: str, pan_dir: str, pan_file_path: str) -> Path:
    mapped = local_mapped_path(local_dir, pan_dir, pan_file_path)
    return mapped.with_suffix(".strm")
