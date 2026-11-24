"""Bounded Telegram envelopes for the ResourceFlow private-chat RPC protocol."""

import base64
import hashlib
import json
import math
import re
import unicodedata
import zlib


VERSION = 1
POLICY_VERSION = 3
MAX_BYTES = 192 * 1024
MAX_PARTS = 96
PART_SIZE = 2700
REQUEST_PREFIX = "RFQ1"
RESPONSE_PREFIX = "RFA1"
ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
RULE_DEFAULTS = {
    "unknown_policy": "exclude", "inflight_enabled": True, "pack_dedup_enabled": True,
    "pack_policy": "partial", "identity_policy": "defer", "prefer_2160p": True,
}


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
        return decode_json(raw)
    except (ValueError, TypeError, RecursionError, zlib.error):
        raise SharedError("无法解码协议消息") from None


def decode_json(raw):
    def invalid_constant(_):
        raise ValueError()

    try:
        if len(raw) > MAX_BYTES:
            raise ValueError()
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
    except (ValueError, TypeError, RecursionError):
        raise SharedError("无法解码协议消息") from None


def _identity_bindings(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or len(value) > 200:
        raise SharedError("媒体身份绑定无效")
    result, normalized = {}, {}
    for raw_name, raw_id in value.items():
        if not isinstance(raw_name, str):
            raise SharedError("媒体身份绑定格式无效")
        name = str(raw_name).strip()
        key = "".join(
            character.casefold()
            for character in unicodedata.normalize("NFKC", name)
            if character.isalnum()
        )
        if (not key or len(name) > 200 or isinstance(raw_id, bool)
                or not re.fullmatch(r"[1-9]\d*", str(raw_id))):
            raise SharedError("媒体身份绑定格式无效")
        tmdb_id = int(raw_id)
        if tmdb_id >= 2**53:
            raise SharedError("媒体身份绑定 TMDB ID 超出有效范围")
        if key in normalized and normalized[key] != tmdb_id:
            raise SharedError("同一身份绑定不能指向不同 TMDB ID")
        normalized[key] = tmdb_id
        result[name] = tmdb_id

    id_aliases = {}
    for name, tmdb_id in result.items():
        match = re.fullmatch(r"(?i)(TMDB|DOUBAN)\s*[:#]?\s*([1-9]\d*)", name)
        if match:
            source, raw_source_id = match.groups()
            source_id = int(raw_source_id)
            if (len(raw_source_id) > 32 or source == "TMDB"
                    and (source_id >= 2**53 or source_id == tmdb_id)):
                raise SharedError("媒体身份映射的来源或目标 ID 无效")
            if source == "TMDB":
                id_aliases[source_id] = tmdb_id
    for source_id in id_aliases:
        visited = set()
        current = source_id
        while current in id_aliases:
            if current in visited:
                raise SharedError("TMDB 身份映射不能形成循环")
            visited.add(current)
            current = id_aliases[current]
    return result


def policy(attributes, tolerance, rules=None, identity_aliases=None, movie_identity_aliases=None):
    fields = sorted(set(attributes))
    if not fields or any(field not in {"resolution", "effects", "codec", "source", "fps", "size"}
                         for field in fields):
        raise SharedError("共享去重必须选择有效规格字段")
    tolerance = float(tolerance)
    if not 0 <= tolerance <= 20:
        raise SharedError("文件大小容差必须在 0 至 20 之间")
    value = {
        "version": POLICY_VERSION, "attributes": fields, "size_tolerance_pct": tolerance,
        "identity_aliases": _identity_bindings(identity_aliases),
        "movie_identity_aliases": _identity_bindings(movie_identity_aliases),
    }
    if rules is not None:
        if not isinstance(rules, dict) or set(rules) != set(RULE_DEFAULTS):
            raise SharedError("协调端资源规则字段不完整，请更新双方插件")
        for field in ("inflight_enabled", "pack_dedup_enabled", "prefer_2160p"):
            if type(rules[field]) is not bool:
                raise SharedError("协调端资源规则无效")
        for field, choices in {
            "unknown_policy": ("exclude", "allow"), "pack_policy": ("partial", "exclude"),
            "identity_policy": ("defer", "allow"),
        }.items():
            if rules[field] not in choices:
                raise SharedError("协调端资源规则无效")
        value["rules"] = dict(rules)
    return {**value, "revision": digest(value)}
