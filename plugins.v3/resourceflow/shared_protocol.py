"""Bounded Telegram envelopes for the ResourceFlow private-chat RPC protocol."""

import base64
import hashlib
import json
import math
import re
import zlib


VERSION = 1
MAX_BYTES = 192 * 1024
MAX_PARTS = 96
PART_SIZE = 2700
REQUEST_PREFIX = "RFQ1"
RESPONSE_PREFIX = "RFA1"
ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


class SharedError(Exception):
    """A sanitized, user-visible error without credentials or raw RPC bodies."""


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def encode(value, request_id, response=False):
    if not ID_PATTERN.fullmatch(request_id):
        raise SharedError("无效的请求编号")
    data = canonical(value).encode()
    if len(data) > MAX_BYTES:
        raise SharedError("申请批次过大，暂不添加下载")
    body = base64.urlsafe_b64encode(zlib.compress(data)).decode("ascii")
    parts = [body[index:index + PART_SIZE] for index in range(0, len(body), PART_SIZE)]
    if not parts or len(parts) > MAX_PARTS:
        raise SharedError("申请分片数量过多")
    prefix = RESPONSE_PREFIX if response else REQUEST_PREFIX
    return [f"{prefix} {request_id} {index + 1}/{len(parts)} {part}"
            for index, part in enumerate(parts)]


def parse_part(text, response=False):
    prefix = RESPONSE_PREFIX if response else REQUEST_PREFIX
    if not isinstance(text, str) or len(text) > 3000:
        raise SharedError("无效的协议消息")
    try:
        head, request_id, position, body = text.split(" ", 3)
        index, count = map(int, position.split("/"))
    except (ValueError, TypeError):
        raise SharedError("无效的协议分片") from None
    if (head != prefix or not ID_PATTERN.fullmatch(request_id) or not 1 <= index <= count <= MAX_PARTS
            or not 0 < len(body) <= PART_SIZE or not re.fullmatch(r"[A-Za-z0-9_=-]+", body)):
        raise SharedError("无效的协议分片")
    return request_id, index, count, body


def decode(parts):
    body = "".join(parts)
    if len(body) > PART_SIZE * MAX_PARTS:
        raise SharedError("协议消息过大")
    try:
        compressed = base64.b64decode(body.encode("ascii"), altchars=b"-_", validate=True)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or inflater.unconsumed_tail or not inflater.eof or inflater.unused_data:
            raise ValueError()
        def invalid_constant(value):
            raise ValueError("Non-finite JSON constant")

        value = json.loads(raw, parse_constant=invalid_constant)
        if not isinstance(value, dict):
            raise ValueError()
        stack = [(value, 0)]
        while stack:
            item, depth = stack.pop()
            if depth > 32 or isinstance(item, float) and not math.isfinite(item):
                raise ValueError()
            if isinstance(item, dict):
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)
        return value
    except (ValueError, TypeError, RecursionError, zlib.error):
        raise SharedError("无法解码协议消息") from None


def policy(attributes, tolerance):
    fields = sorted(set(attributes))
    if not fields or any(field not in {"resolution", "effects", "codec", "source", "fps", "size"}
                         for field in fields):
        raise SharedError("共享去重必须选择有效规格字段")
    tolerance = float(tolerance)
    if not 0 <= tolerance <= 20:
        raise SharedError("文件大小容差必须在 0 至 20 之间")
    value = {"version": VERSION, "attributes": fields, "size_tolerance_pct": tolerance}
    return {**value, "revision": digest(value)}
