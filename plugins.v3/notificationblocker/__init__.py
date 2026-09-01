"""
通知消息屏蔽插件 NotificationBlocker

按设置的正则关键词，对标题或正文命中的消息不进行渠道推送
（TG/微信等不发送），但消息中心历史仍保留，可在 Web 端查看。

实现方式：注册到共享通知拦截链（app.plugins._hookchain），
消息进入发送入口时先渲染出最终标题/正文，命中任一正则关键词则
写入消息历史后拦截（返回 True，不再发送）；未命中则放行，
交由后续处理器（如通知合并）或原始发送流程处理。

规则：
- 关键词为正则表达式，每行一个；标题或正文任一命中即屏蔽
- 无效的正则表达式会被忽略并在日志中提示
- 屏蔽的消息仍写入消息中心历史（与原始发送行为一致），仅渠道不推送
"""

import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase, _hookchain
from app.schemas.message import Message
from app.schemas.types import MessageType
from app.application.messaging.message import MessageTemplateHelper


_EPISODE_TITLE_RE = re.compile(
    r"^(?P<name>.+?)\s*\((?P<year>\d{4})\)\s*"
    r"S(?P<season>\d{1,2})\s*E(?P<episode>\d{1,4})"
    r"(?:\s+(?P<reason>.*?))?\s*$",
    re.IGNORECASE,
)


def _parse_episode_title(title: str) -> Optional[dict]:
    """解析常见的剧集标题，无法识别时返回 None。"""
    match = _EPISODE_TITLE_RE.match((title or "").strip())
    if not match:
        return None
    return {
        "name": match.group("name").strip(),
        "year": match.group("year"),
        "season": int(match.group("season")),
        "episode": int(match.group("episode")),
        "reason": (match.group("reason") or "").strip(),
    }


def _format_episode_ranges(episodes: List[int]) -> str:
    """把集数压缩为 E01-E04、E07 这样的连续区间。"""
    if not episodes:
        return ""
    values = sorted(set(episodes))
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


def _group_blocked_records(records: List[dict]) -> List[dict]:
    """按剧集和通知标题分组，同时保留每一条原始记录。"""
    groups = []
    indexes = {}
    for record in records:
        title = str(record.get("title") or "").strip()
        parsed = _parse_episode_title(title)
        if parsed:
            key = (
                "episode",
                parsed["name"],
                parsed["year"],
                parsed["season"],
                parsed["reason"],
            )
        else:
            key = ("title", title)

        if key not in indexes:
            indexes[key] = len(groups)
            groups.append({
                "kind": key[0],
                "key": key,
                "records": [],
                "parsed": parsed,
            })
        groups[indexes[key]]["records"].append(record)
    return groups


def _render_group(group: dict, number: int) -> List[str]:
    """渲染一组简洁摘要，不展开逐条通知的标题和正文。"""
    records = group["records"]
    if group["kind"] == "episode":
        parsed = group["parsed"]
        reason = f" {parsed['reason']}" if parsed["reason"] else ""
        episodes = [
            _parse_episode_title(record.get("title", ""))["episode"]
            for record in records
        ]
        episode_ranges = _format_episode_ranges(episodes)
        summary = (
            f"{number}. 【{parsed['name']} ({parsed['year']})】 "
            f"S{parsed['season']:02d} {episode_ranges}"
            f"{reason} ×{len(records)}"
        )
    else:
        title = str(records[0].get("title") or "（无标题）").strip()
        summary = f"{number}. {title}"
        if len(records) > 1:
            summary += f" ×{len(records)}"

    lines = [summary]
    sources = list(dict.fromkeys(
        str(record.get("source") or "").strip() for record in records
        if str(record.get("source") or "").strip()
    ))
    for source in sources:
        lines.append(f"   来源：{source}")
    return lines


def _chunk_lines(lines: List[str], max_length: int = 3500) -> List[str]:
    """按通知渠道常见长度限制拆分，不丢失任何行。"""
    chunks = []
    current = []
    current_length = 0
    for line in lines:
        # 极长的单行也要拆开，避免超过渠道限制。
        while len(line) > max_length:
            part, line = line[:max_length], line[max_length:]
            if current:
                chunks.append("\n".join(current))
                current = []
                current_length = 0
            chunks.append(part)
        line_length = len(line) + (1 if current else 0)
        if current and current_length + line_length > max_length:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks


class _Blocker:
    """屏蔽器：线程安全的正则匹配 + 被屏蔽消息记录（模块级单例，与插件实例解耦）。"""

    def __init__(self) -> None:
        self.enabled: bool = False
        self.patterns: List[re.Pattern] = []
        self._lock = threading.Lock()
        self._save = None  # 持久化回调（插件注入 save_data）
        self._load = None  # 读取回调（插件注入 get_data）

    def bind(self, save_fn, load_fn) -> None:
        """绑定插件的数据持久化方法（save_data/get_data）。"""
        self._save = save_fn
        self._load = load_fn

    def update(self, enabled: bool, keywords: List[str]) -> None:
        """更新配置：编译关键词正则，无效的忽略。"""
        patterns: List[re.Pattern] = []
        for kw in keywords:
            kw = (kw or "").strip()
            if not kw:
                continue
            try:
                patterns.append(re.compile(kw))
            except re.error as err:
                logger.error(f"通知屏蔽: 关键词正则无效，已忽略 [{kw}]：{err}")
        with self._lock:
            self.enabled = enabled
            self.patterns = patterns

    def _record(self, msg: Message) -> None:
        """记录完整的被屏蔽消息（按天持久化，供通知汇总统计）。"""
        if not self._save or not self._load:
            return
        try:
            date_str = datetime.now(pytz.timezone(settings.TZ)).strftime("%Y-%m-%d")
            key = f"blocked_{date_str}"
            with self._lock:
                data = self._load(key) or {}
                types = dict(data.get("types") or {})
                mtype = getattr(msg.mtype, "value", None) or "未分类"
                types[mtype] = types.get(mtype, 0) + 1
                titles = list(data.get("titles") or [])
                title = (msg.title or "").strip()
                if title and title not in titles:
                    titles.append(title)
                records = list(data.get("records") or [])
                records.append({
                    "title": title,
                    "text": (msg.text or "").strip(),
                    "mtype": mtype,
                    "source": getattr(msg, "source", None),
                    "link": getattr(msg, "link", None),
                    "time": datetime.now(pytz.timezone(settings.TZ)).strftime(
                        "%H:%M:%S"
                    ),
                })
                self._save(key, {
                    "types": types,
                    "titles": titles,
                    "records": records,
                })
        except Exception as err:
            logger.error(f"通知屏蔽: 记录被屏蔽消息失败 {err}")

    def daily_blocked(self, date_str: str) -> dict:
        """读取指定日期的被屏蔽消息统计。"""
        if not self._load:
            return {"types": {}, "titles": [], "records": []}
        try:
            data = self._load(f"blocked_{date_str}") or {}
            return {
                "types": dict(data.get("types") or {}),
                "titles": list(data.get("titles") or []),
                "records": list(data.get("records") or []),
            }
        except Exception as err:
            logger.error(f"通知屏蔽: 读取屏蔽统计失败 {err}")
            return {"types": {}, "titles": [], "records": []}

    def intercept(self, chain, message, meta, mediainfo, torrentinfo, transferinfo, kwargs) -> bool:
        """拦截入口（由共享拦截链调用）：返回 True=已屏蔽（不推送），False=放行。"""
        if not self.enabled or not self.patterns:
            return False
        # 渲染得到最终标题/正文（与原始流程一致）
        try:
            rendered = MessageTemplateHelper.render(
                message=message, meta=meta, mediainfo=mediainfo,
                torrentinfo=torrentinfo, transferinfo=transferinfo, **kwargs)
        except Exception as err:
            logger.error(f"通知屏蔽: 渲染消息失败 {err}")
            rendered = message
        if rendered is None:
            return False
        title = rendered.title or ""
        text = rendered.text or ""
        for pattern in self.patterns:
            try:
                if pattern.search(title) or pattern.search(text):
                    # 仅不推送：写入消息中心历史后拦截发送
                    if rendered.save_history:
                        try:
                            chain.messageoper.add(**rendered.model_dump())
                        except Exception as err:
                            logger.error(f"通知屏蔽: 写入消息历史失败 {err}")
                    logger.info(f"通知屏蔽: 已屏蔽「{title}」（命中 {pattern.pattern}）")
                    # 记录本次屏蔽（供通知汇总统计）
                    self._record(rendered)
                    return True
            except Exception as err:
                logger.error(f"通知屏蔽: 正则执行错误 {err}")
        return False


_BLOCKER = _Blocker()


def _block_handler(chain, message, meta, mediainfo, torrentinfo, transferinfo, kwargs) -> bool:
    """屏蔽拦截处理器：返回 True=已屏蔽，False=放行。"""
    return _BLOCKER.intercept(chain, message, meta, mediainfo, torrentinfo, transferinfo, kwargs)


class NotificationBlocker(_PluginBase):
    """通知消息屏蔽插件"""

    # 插件元信息
    plugin_name = "通知消息屏蔽"
    plugin_desc = "按正则关键词屏蔽消息通知：标题或正文命中即不推送（历史保留）。"
    plugin_version = "1.3.2"
    plugin_author = "helios"
    plugin_order = 51
    plugin_config_prefix = "notificationblocker_"
    auth_level = 1

    # 默认配置
    _enabled = False
    _keywords: List[str] = []
    _summary_enabled = False
    _summary_cron = "0 20 * * *"
    _cron_valid = True

    def init_plugin(self, config: dict = None):
        """插件启用 / 配置保存时调用"""
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        raw = config.get("keywords") or ""
        self._keywords = [
            x.strip() for x in str(raw).replace("\r", "").split("\n") if x.strip()
        ]
        self._summary_enabled = bool(config.get("summary_enabled", False))
        self._summary_cron = str(config.get("summary_cron") or "0 20 * * *").strip()
        # 校验汇总定时表达式
        try:
            CronTrigger.from_crontab(self._summary_cron)
            self._cron_valid = True
        except Exception as err:
            self._cron_valid = False
            logger.error(f"通知屏蔽: 通知汇总定时表达式无效 [{self._summary_cron}]：{err}")
        _BLOCKER.update(self._enabled, self._keywords)
        _BLOCKER.bind(self.save_data, self.get_data)
        # 优先级 50：先于通知合并（100），命中的消息直接屏蔽不进入合并队列
        _hookchain.register("notificationblocker", _block_handler, priority=50)
        logger.info(
            f"通知屏蔽: 初始化完成，状态={'启用' if self._enabled else '停用'}，"
            f"关键词数={len(self._keywords)}，汇总={'开' if self._summary_enabled and self._cron_valid else '关'}"
        )

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/send_summary",
            "endpoint": self.send_daily_summary,
            "methods": ["GET"],
            "summary": "发送今日通知汇总",
            "description": "手动触发发送今天各类型通知条数汇总",
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        """注册通知汇总定时服务"""
        if not (self._enabled and self._summary_enabled and self._cron_valid):
            return []
        return [{
            "id": "NotificationBlocker_daily_summary",
            "name": "通知汇总",
            "trigger": CronTrigger.from_crontab(self._summary_cron),
            "func": self.send_daily_summary,
            "kwargs": {},
        }]

    def send_daily_summary(self):
        """发送今日被屏蔽通知汇总（分类合并，不展开原文和正文）。"""
        try:
            now = datetime.now(pytz.timezone(settings.TZ))
            date_str = now.strftime("%Y-%m-%d")
            blocked = _BLOCKER.daily_blocked(date_str)
            types = blocked.get("types") or {}
            records = blocked.get("records") or []
            # 兼容 1.2.0 以前的数据：旧版本没有保存正文，只能还原去重标题。
            legacy = not records and blocked.get("titles")
            if legacy:
                records = [{"title": title, "text": ""} for title in blocked["titles"]]
            total = sum(types.values())
            lines = [f"统计时间：{date_str}", f"累计屏蔽：{total} 条"]
            if types:
                lines.append("")
                lines.append("📊 类型统计")
                for mtype, cnt in sorted(types.items(), key=lambda x: -x[1]):
                    lines.append(f"{mtype}：{cnt} 条")
            if records:
                lines.extend(["", "📦 分类明细"])
                for index, group in enumerate(_group_blocked_records(records), 1):
                    lines.extend(_render_group(group, index))
            if legacy:
                lines.extend([
                    "",
                    "注：这是旧版本数据，仅保存了去重标题，重复记录无法恢复。",
                ])
            else:
                if not types:
                    lines.append("")
                    lines.append("今日暂无被屏蔽的通知")
            chunks = _chunk_lines(lines)
            for index, text in enumerate(chunks, 1):
                suffix = f"，第 {index}/{len(chunks)} 部分" if len(chunks) > 1 else ""
                # 绕过拦截链直发，避免汇总消息被屏蔽/合并处理。
                _hookchain.deliver_original(
                    self.chain,
                    message=Message(
                        mtype=MessageType.Other,
                        title=f"今日屏蔽通知汇总（{total} 条{suffix}）",
                        text=text,
                    ),
                )
            logger.info(
                f"通知屏蔽: 已发送今日屏蔽通知汇总（共 {total} 条，{len(chunks)} 部分）"
            )
        except Exception as err:
            logger.error(f"通知屏蔽: 发送通知汇总失败 {err}")

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """插件配置表单 (Vuetify JSON)"""
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
                                        "props": {"model": "enabled", "label": "启用通知屏蔽"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "summary_enabled", "label": "通知汇总"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "summary_cron",
                                            "label": "汇总定时",
                                            "placeholder": "0 20 * * *",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "keywords",
                                            "label": "屏蔽关键词",
                                            "rows": 6,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            },
        ], {
            "enabled": False,
            "keywords": "",
            "summary_enabled": False,
            "summary_cron": "0 20 * * *",
        }

    def get_page(self) -> Optional[List[dict]]:
        return None

    def stop_service(self):
        """插件停止：注销拦截处理器"""
        _BLOCKER.enabled = False
        _hookchain.unregister("notificationblocker")
        logger.info("通知屏蔽: 插件已停止")
