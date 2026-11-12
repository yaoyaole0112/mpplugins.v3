import json
import os
import re
import threading
import time
from pathlib import Path


RECEIPT_TTL = 30 * 60
_LOCK = threading.RLock()


def _store_path():
    from app.sdk.config import settings

    return Path(settings.PLUGIN_DATA_PATH) / "p115_upload_receipts.json"


def _identity(path):
    stat = Path(path).stat()
    return [stat.st_size, stat.st_mtime_ns, stat.st_ino, stat.st_dev, stat.st_ctime_ns]


def _saved_item(item):
    if hasattr(item, "model_dump"):
        item = item.model_dump(mode="json")
    if not isinstance(item, dict):
        return {}
    return {
        key: item.get(key)
        for key in ("storage", "fileid", "type", "path", "name", "size", "modify_time")
    }


def _read(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError("115 upload receipt store is malformed")
    cutoff = time.time() - RECEIPT_TTL
    return [
        item for item in data
        if isinstance(item.get("created_at"), (int, float))
        and item["created_at"] >= cutoff
    ]


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    os.replace(temp_path, path)


def record_upload_result(source_path, target_item, upload_kind):
    if upload_kind not in {"instant", "ordinary"}:
        return False
    source = Path(source_path).resolve()
    target = _saved_item(target_item)
    if (
        target.get("storage") != "115网盘Plus"
        or target.get("type") != "file"
        or not re.fullmatch(r"[1-9][0-9]{0,63}", str(target.get("fileid") or ""))
        or not isinstance(target.get("path"), str)
        or not target["path"].startswith("/")
        or target.get("size") != Path(source_path).stat().st_size
    ):
        return False
    try:
        identity = _identity(source)
    except OSError:
        return False
    path = _store_path()
    with _LOCK:
        records = _read(path)
        records.append({
            "source_path": str(source),
            "source_identity": identity,
            "target": target,
            "upload_kind": upload_kind,
            "created_at": time.time(),
        })
        _write(path, records[-5000:])
    return True


def consume_upload_result(source_path, source_identity, target_item):
    source = Path(source_path).resolve()
    target = _saved_item(target_item)
    if not target.get("fileid"):
        return None
    path = _store_path()
    with _LOCK:
        records = _read(path)
        match_index = next(
            (
                index for index in range(len(records) - 1, -1, -1)
                if records[index].get("source_path") == str(source)
                and records[index].get("source_identity") == list(source_identity)
                and str(records[index].get("target", {}).get("fileid")) == str(target.get("fileid"))
                and records[index].get("target", {}).get("path") == target.get("path")
                and records[index].get("target", {}).get("size") == target.get("size")
            ),
            None,
        )
        if match_index is None:
            _write(path, records)
            return None
        result = records.pop(match_index)
        _write(path, records)
        return result
