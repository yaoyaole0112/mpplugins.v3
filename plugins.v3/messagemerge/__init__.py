"""
通知合并插件 MessageMerge

将短时间窗口内同一剧集的多条入库通知智能聚合为一条摘要推送，
减少 TG/微信等渠道的消息刷屏（如批量删除源文件、站点消息、STRM 生成等）。

实现方式：接管 MoviePilot 通知发送统一入口
(NotificationMixin.post_message / async_post_message)，
收到第一条消息后等待 _WINDOW_SECONDS（60 秒），期间有新的同类消息
到达则重新计时；连续 60 秒没有同类消息后，自动合并成一条发送。

规则：
- 剧集通知按剧名、年份、季合并，不同剧集或季不会混合；其他通知按完整标题合并
- 可配置各消息类型的合并通知发送范围（管理员/用户/全部），
  参考 MP 通知隔离（场景开关）；未配置的类型跟随系统通知隔离
- 带交互元素的消息不合并，直接发送：有 userid（定向/回复）、有按钮、
  强制回复、消息编辑、文件/语音消息、WebAgent 渠道
- 已拦截的原始消息仍会写入消息中心历史（每条保留各自的查看详情链接），
  渠道推送（TG/微信等）只收到合并后的摘要
- 插件停用/重载时会立即清空缓存的消息并发送，避免消息丢失
"""

import threading
import time
import re
import json
import sys
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger
from app.plugins import _PluginBase

try:
    from app.plugins import _hookchain
except ImportError:
    from . import notification_hooks as _hookchain

    _hookchain = sys.modules.setdefault("app.plugins._hookchain", _hookchain)
from app.schemas.message import Message
from app.schemas.types import MessageType
from app.application.messaging.message import MessageTemplateHelper

# 滚动等待时长（秒）：收到第一条消息后开始计时，期间有新同类消息则重新计时
_WINDOW_SECONDS = 60
_SERIES_RE = re.compile(r"^(?:🎬\s*|📺\s*)?(?P<name>.+?)\s*[（(](?P<year>\d{4})[）)](?P<tail>.*)$")
_EPISODES_RE = re.compile(r"S(?P<season>\d{1,2})\s*(?P<episodes>E\d{1,4}(?:\s*[-~～]\s*E?\d{1,4}|\s*[,、，]\s*E?\d{1,4}|\s*E\d{1,4})*)", re.IGNORECASE)
_EPISODE_RANGE_RE = re.compile(r"E?(\d{1,4})(?:\s*[-~～]\s*E?(\d{1,4}))?", re.IGNORECASE)
_FIELD_RE = re.compile(r"[^\w\s]*\s*(类型|分类|质量|画质|大小|主演|演员)\s*[:：]\s*(.+)")
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(B|[KMGT](?:I?B)?)", re.IGNORECASE)

# 发送范围选项（对齐 MP 通知隔离的场景开关）
_RANGE_OPTIONS = [
    {"title": "跟随系统", "value": ""},
    {"title": "管理员", "value": "admin"},
    {"title": "用户", "value": "user"},
    {"title": "全部", "value": "all"},
]


class _Merger:
    """合并器：线程安全的消息缓冲与滚动窗口调度（模块级单例，与插件实例解耦）。"""

    def __init__(self) -> None:
        self.enabled: bool = False
        self.window: int = _WINDOW_SECONDS
        self.ranges: Dict[str, str] = {}  # 消息类型name -> admin/user/all；空 = 跟随系统
        self._lock = threading.Lock()
        self._groups: Dict[Tuple, Dict[str, Any]] = {}
        self._flushing_ids = set()

    def update(self, enabled: bool, ranges: Optional[Dict[str, str]] = None) -> None:
        with self._lock:
            self.enabled = enabled
            self.window = _WINDOW_SECONDS
            self.ranges = dict(ranges or {})

    def _apply_send_scope(self, msg: Message) -> None:
        """按插件配置的发送范围设置合并消息目标；未配置/全部/用户则跟随系统通知隔离。

        - admin：目标设为管理员的渠道 ID（与系统通知隔离的 admin 行为一致）
        - user：广播消息无操作人，交由系统通知隔离处理（不干预）
        - all / 未配置：不干预，系统按消息类型正常发送
        """
        if not self.ranges:
            return
        mtype = getattr(msg.mtype, "name", None)
        action = self.ranges.get(mtype) if mtype else None
        if not action or action in ("user", "all"):
            return
        try:
            from app.runtime.config import settings
            from app.application.chain.data import UserPortProxy as UserOper

            targets = UserOper().get_settings(settings.SUPERUSER)
            if targets:
                msg.targets = targets
                logger.info(f"通知合并: 「{msg.title}」按配置发送给管理员")
        except Exception as err:
            logger.error(f"通知合并: 设置发送范围失败 {err}")

    # ------------------------------------------------------------------
    @staticmethod
    def _should_merge(msg: Message) -> bool:
        """判断消息是否适合合并。带交互元素的消息一律不合并。"""
        if msg is None:
            return False
        if getattr(msg, "userid", None):
            return False  # 定向/交互消息
        if getattr(msg, "buttons", None):
            return False  # 带按钮的交互消息
        if getattr(msg, "force_reply", False):
            return False  # 强制回复
        if getattr(msg, "original_message_id", None) or getattr(msg, "original_chat_id", None):
            return False  # 编辑类消息
        if getattr(msg, "file_path", None) or getattr(msg, "voice_path", None):
            return False  # 文件/语音消息
        if getattr(msg, "channel", None) and getattr(msg.channel, "name", "") == "WebAgent":
            return False  # WebAgent 交互会话
        if getattr(msg, "rich_message", None) or getattr(msg, "rich_content", None):
            return False
        return bool((msg.title or "").strip())

    @staticmethod
    def _group_key(msg: Message) -> Optional[Tuple]:
        """分组键：消息类型 + 剧名/季；非剧集通知仍按完整标题合并。"""
        title = (msg.title or "").strip()
        if not title:
            return None
        mtype = getattr(msg.mtype, "value", None) or (msg.mtype if msg.mtype else None)
        route = (
            getattr(getattr(msg, "channel", None), "value", getattr(msg, "channel", None)),
            getattr(msg, "source", None), getattr(msg, "username", None),
            json.dumps(getattr(msg, "targets", None), sort_keys=True, ensure_ascii=False),
            getattr(msg, "action", None), getattr(msg, "notification_route_scope", None),
            getattr(msg, "private_delivery", False), getattr(msg, "parse_mode", None),
        )
        episode = _episode_info(msg)
        if episode:
            return (mtype, route, "episode", episode["series"], episode["season"], episode["suffix"])
        return (mtype, route, "title", title)

    # ------------------------------------------------------------------
    def intercept(self, chain, message, meta, mediainfo, torrentinfo, transferinfo, kwargs) -> bool:
        """拦截入口（由共享拦截链调用）：返回 True=已拦截进入合并队列，False=放行。"""
        with self._lock:
            if id(message) in self._flushing_ids:
                return False
        if not self.enabled:
            return False
        # 渲染得到最终标题/文本（与原始流程一致）
        try:
            rendered = MessageTemplateHelper.render(
                message=message, meta=meta, mediainfo=mediainfo,
                torrentinfo=torrentinfo, transferinfo=transferinfo, **kwargs)
        except Exception as err:
            logger.error(f"通知合并: 渲染消息失败 {err}")
            rendered = message
        if not self._should_merge(rendered):
            return False
        key = self._group_key(rendered)
        if key is None:
            return False
        with self._lock:
            group = self._groups.get(key)
            if group is None:
                group = {"messages": [], "chain": chain, "timer": None, "token": None}
                self._groups[key] = group
            group["messages"].append(rendered)
            # 滚动窗口：有新的同类消息到达则取消旧计时器，重新计时
            old = group["timer"]
            if old:
                try:
                    old.cancel()
                except Exception:
                    pass
            token = object()
            group["token"] = token
            timer = threading.Timer(self.window, self._flush, args=(key, token))
            timer.daemon = True
            timer.start()
            group["timer"] = timer
        # 原始消息写入消息中心历史（与原始发送行为一致，保留每条详情与链接）
        if rendered.save_history:
            try:
                chain.messageoper.add(**rendered.model_dump())
            except Exception as err:
                logger.error(f"通知合并: 写入消息历史失败 {err}")
        logger.info(f"通知合并: 已拦截「{rendered.title}」加入合并队列（{self.window}s 内无同类消息即发送）")
        return True

    # ------------------------------------------------------------------
    def _flush(self, key: Tuple, token: object = None) -> None:
        """滚动窗口到期时发送合并消息。

        仅当分组当前的计时标记与触发本次回调的 token 一致时才发送，
        避免已被新消息重置的旧计时器误发缓存。
        """
        with self._lock:
            group = self._groups.get(key)
            if group is None or not group["messages"]:
                return
            if group.get("token") is not token:
                return  # 计时已被更新的同类消息重置，本次回调失效
            self._groups.pop(key, None)
            msgs = group["messages"]
            chain = group["chain"]
            timer = group.get("timer")
            if timer:
                try:
                    timer.cancel()
                except Exception:
                    pass
        self._deliver(msgs, chain)

    def flush_all(self) -> None:
        """插件停止时清空所有缓存分组并发送（防止消息丢失）。"""
        with self._lock:
            keys = list(self._groups.keys())
            groups = []
            for k in keys:
                g = self._groups.pop(k, None)
                if g and g["messages"]:
                    if g.get("timer"):
                        try:
                            g["timer"].cancel()
                        except Exception:
                            pass
                    groups.append(g)
        for g in groups:
            self._deliver(g["messages"], g["chain"])

    def _deliver(self, msgs: List[Message], chain) -> None:
        """通过原始发送方法投递合并消息（带防递归标记）。"""
        merged = self._compose(msgs)
        # 按插件配置的发送范围设置目标（未配置的跟随系统通知隔离）
        self._apply_send_scope(merged)
        mid = id(merged)
        with self._lock:
            self._flushing_ids.add(mid)
        try:
            _hookchain.deliver_original(chain, message=merged)
            logger.info(f"通知合并: 已发送合并消息「{merged.title}」")
        except Exception as err:
            logger.error(f"通知合并: 发送合并消息失败 {err}")
        finally:
            with self._lock:
                self._flushing_ids.discard(mid)

    @staticmethod
    def _compose(msgs: List[Message]) -> Message:
        """按剧集信息生成摘要，其他通知保留通用合并格式。"""
        first = msgs[0]
        n = len(msgs)
        summary = _compose_episode_summary(msgs) if n > 1 else None
        if summary:
            title, body = summary
            return first.model_copy(deep=True, update={"title": title, "text": body, "ctype": None, "save_history": False})
        texts, seen = [], set()
        for m in msgs:
            t = (m.text or "").strip()
            if t and t not in seen:
                seen.add(t)
                texts.append(t)
        if len(texts) == 1:
            body = texts[0]
        else:
            lines = [f"共 {n} 条同类通知："]
            lines.extend(f"{i}. {t}" for i, t in enumerate(texts, 1))
            body = "\n".join(lines)
        title = (first.title or "").strip()
        if n > 1:
            title = f"{title}（{n}条）" if title else f"通知（{n}条）"
        return first.model_copy(deep=True, update={"title": title, "text": body, "ctype": None, "save_history": False})


def _format_episode_ranges(episodes: List[int]) -> str:
    values = sorted(set(episodes))
    if not values:
        return ""
    ranges = []
    start = previous = values[0]
    for episode in values[1:]:
        if episode == previous + 1:
            previous = episode
            continue
        ranges.append((start, previous))
        start = previous = episode
    ranges.append((start, previous))
    return ",".join(
        f"E{start:02d}" if start == end else f"E{start:02d}-{end:02d}"
        for start, end in ranges
    )


def _episode_info(msg: Message) -> Optional[dict]:
    match = _SERIES_RE.fullmatch((msg.title or "").strip())
    if not match:
        return None
    tail = match.group("tail").strip()
    success_lines = [line.strip() for line in (msg.text or "").splitlines()
                     if re.match(r"^✅\s*已入库\s*[·:：]?\s*S", line.strip(), re.IGNORECASE)]
    if len(success_lines) > 1:
        return None
    status_line = success_lines[0] if success_lines else ""
    status_episodes = re.sub(r"^✅\s*已入库\s*[·:：]?\s*", "", status_line)
    episode_match = _EPISODES_RE.match(tail) if tail else _EPISODES_RE.fullmatch(status_episodes)
    if not episode_match:
        return None
    suffix = tail[episode_match.end():].strip() if tail else ""
    if status_line and (not _EPISODES_RE.fullmatch(status_episodes)
                        or status_episodes.replace(" ", "").upper() != episode_match.group().replace(" ", "").upper()):
        return None
    episodes = []
    for start, end in _EPISODE_RANGE_RE.findall(episode_match.group("episodes")):
        first, last = int(start), int(end or start)
        if first > last:
            return None
        episodes.extend(range(first, last + 1))
    return {
        "series": f"{match.group('name').strip()} ({match.group('year')})",
        "season": int(episode_match.group("season")),
        "episodes": episodes,
        "suffix": suffix,
        "status_line": status_line,
    }


def _message_fields(msg: Message, status_line: str) -> Optional[Dict[str, str]]:
    fields = {}
    for line in (msg.text or "").splitlines():
        line = line.strip()
        if not line or line == status_line or re.fullmatch(r"[━─—_\-]{3,}", line):
            continue
        match = _FIELD_RE.fullmatch(line)
        if not match:
            return None
        label, value = match.groups()
        normalized = {"分类": "类型", "画质": "质量", "演员": "主演"}.get(label, label)
        if normalized in fields:
            return None
        fields[normalized] = value.strip()
    return fields


def _size_bytes(value: str) -> Optional[Decimal]:
    match = _SIZE_RE.fullmatch(value.strip())
    if not match:
        return None
    unit = match.group(2).upper()
    power = 0 if unit == "B" else "KMGT".index(unit[0]) + 1
    return Decimal(match.group(1)) * (1024 ** power)


def _format_size(total_bytes: Decimal) -> str:
    for power, unit in ((4, "T"), (3, "G"), (2, "M"), (1, "K"), (0, "B")):
        if total_bytes >= 1024 ** power or power == 0:
            value = total_bytes / (1024 ** power)
            return f"{value:.2f}".rstrip("0").rstrip(".") + unit


def _compose_episode_summary(messages: List[Message]) -> Optional[Tuple[str, str]]:
    records = [_episode_info(msg) for msg in messages]
    if any(not record or not record["status_line"] or record["suffix"] for record in records):
        return None
    first = records[0]
    if any((record["series"], record["season"]) != (first["series"], first["season"]) for record in records):
        return None
    fields = [_message_fields(msg, record["status_line"]) for msg, record in zip(messages, records)]
    if any(item is None for item in fields):
        return None
    sizes = [item.get("大小") for item in fields]
    total_size = None
    if any(size is not None for size in sizes):
        if any(size is None or _size_bytes(size) is None for size in sizes):
            return None
        total_size = _format_size(sum((_size_bytes(size) for size in sizes), Decimal(0)))
    episodes = [episode for record in records for episode in record["episodes"]]
    lines = [f"✅ 已入库 · S{first['season']:02d} {_format_episode_ranges(episodes)}", "━━━━━━━━━━━━━━━━━━"]
    for key, label in (("类型", "🗂 类型"), ("质量", "📑 质量"), ("大小", "💾 大小"), ("主演", "👤 主演")):
        if key == "大小":
            value = total_size
        else:
            values = [item[key] for item in fields if item.get(key)]
            if key == "主演":
                values = [actor.strip() for value in values for actor in re.split(r"[、,，]", value) if actor.strip()]
            value = ("、" if key == "主演" else " / ").join(dict.fromkeys(values))
        if value:
            lines.append(f"{label}：{value}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    return f"🎬 {first['series']}", "\n".join(lines)


# ----------------------------------------------------------------------
# 模块级单例与补丁包装
# ----------------------------------------------------------------------
_MERGER = _Merger()


def _merge_handler(chain, message, meta, mediainfo, torrentinfo, transferinfo, kwargs) -> bool:
    """合并拦截处理器：返回 True=已拦截（进入合并队列），False=放行。"""
    return _MERGER.intercept(chain, message, meta, mediainfo, torrentinfo, transferinfo, kwargs)


class MessageMerge(_PluginBase):
    """通知合并插件"""

    # 插件元信息
    plugin_name = "通知合并"
    plugin_desc = "同剧同季入库通知智能聚合：合并集数、汇总大小和媒体信息，其他通知按标题合并。"
    plugin_version = "1.5.2"
    plugin_author = "helios"
    plugin_order = 50
    plugin_config_prefix = "messagemerge_"
    auth_level = 1

    # 默认配置
    _enabled = False
    _ranges: Dict[str, str] = {}  # 消息类型name -> admin/user/all；空 = 跟随系统通知隔离

    def init_plugin(self, config: dict = None):
        """插件启用 / 配置保存时调用"""
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        # 表单配置为扁平结构 range_<类型name>=发送范围，还原为 {类型name: 范围}
        ranges: Dict[str, str] = {}
        raw_ranges = config.get("ranges")
        if isinstance(raw_ranges, dict):
            for k, v in raw_ranges.items():
                if str(v) in ("admin", "user", "all"):
                    ranges[str(k)] = str(v)
        else:
            for item in MessageType:
                action = str(config.get(f"range_{item.name}") or "").strip()
                if action in ("admin", "user", "all"):
                    ranges[item.name] = action
        self._ranges = ranges
        _MERGER.update(self._enabled, self._ranges)
        _hookchain.register("messagemerge", _merge_handler, priority=100)
        logger.info(
            f"通知合并: 初始化完成，状态={'启用' if self._enabled else '停用'}，"
            f"等待={_WINDOW_SECONDS}s，发送范围={self._ranges or '跟随系统'}"
        )

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/send_test",
            "endpoint": self.send_test,
            "methods": ["GET"],
            "summary": "发送测试消息",
            "description": "连续发送 count 条标题相同的消息，用于验证通知合并效果",
        }]

    def send_test(self, count: int = 3, title: str = "通知合并测试", msg_type: str = ""):
        """发送 N 条标题相同的测试消息（走插件发送通道，会被合并器拦截）。"""
        try:
            n = max(2, min(int(count), 10))
        except (TypeError, ValueError):
            n = 3
        mtype = None
        if msg_type:
            try:
                mtype = MessageType[msg_type]
            except KeyError:
                return {"success": False, "message": f"无效的消息类型: {msg_type}"}
        for i in range(1, n + 1):
            self.chain.post_message(Message(
                mtype=mtype,
                title=title,
                text=f"测试消息第 {i} 条（{time.strftime('%H:%M:%S')}）。\n用于验证通知合并插件效果。",
            ))
        return {"success": True, "message": f"已发送 {n} 条标题相同的测试消息，60 秒内无同类消息后会自动合并发送"}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """插件配置表单 (Vuetify JSON)：按消息类型设置合并通知发送范围"""
        form_rows = [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {"label": item.value, "disabled": True, "hideDetails": True},
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VSelect",
                                "props": {
                                    "model": f"range_{item.name}",
                                    "label": "发送范围",
                                    "items": _RANGE_OPTIONS,
                                },
                            }
                        ],
                    },
                ],
            }
            for item in MessageType
        ]
        defaults = {f"range_{item.name}": "" for item in MessageType}
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用通知合并"},
                                    }
                                ],
                            },
                        ],
                    },
                    *form_rows,
                ],
            },
        ], {
            "enabled": False,
            **defaults,
        }

    def get_page(self) -> Optional[List[dict]]:
        return None

    def stop_service(self):
        """插件停止：清空缓存并注销拦截处理器"""
        _MERGER.flush_all()
        _MERGER.enabled = False
        _hookchain.unregister("messagemerge")
        logger.info("通知合并: 插件已停止，缓存消息已清空")
