from collections import Counter
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends
from sqlalchemy import select

from app.api.dependencies.auth import get_current_active_superuser
from app.api.dependencies.history import get_transfer_history_mutation_command
from app.db.models.transferhistory import TransferHistory
from app.plugins import _PluginBase
from app.runtime.errors import public_error_message
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.queries import list_transfer_history


class TransferFailureCleaner(_PluginBase):
    plugin_name = "失败整理记录清理"
    plugin_desc = "按失败提示定时清理整理记录，支持与 MP 删除弹窗一致的四种删除方式"
    plugin_icon = "Eraser.png"
    plugin_version = "1.1.2"
    plugin_author = "helios"
    author_url = "https://github.com/yaoyaole0112/mpplugins.v3"
    plugin_config_prefix = "transferfailurecleaner_"
    plugin_order = 30
    auth_level = 2
    _EMPTY_ERROR = "（无错误提示）"
    _TARGET_STATE_ERROR = "无法确认目标文件状态，已跳过整理以避免误覆盖"
    _TARGET_STATE_TYPE = "目标存储访问，无法确认目标文件状态，已跳过整理以避免误覆盖"
    _DELETE_MODES = {
        "record": ("仅删除转移记录", False, False),
        "source": ("删除转移记录和源文件", True, False),
        "destination": ("删除转移记录和媒体库文件", False, True),
        "all": ("删除转移记录、源文件和媒体库文件", True, True),
    }

    def __init__(self):
        super().__init__()
        self._run_lock = Lock()
        self.init_plugin()

    def init_plugin(self, config: Optional[dict] = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._cron = str(config.get("cron") or "0 3 * * *").strip()
        self._delete_mode = str(config.get("delete_mode") or "record")
        if self._delete_mode not in self._DELETE_MODES:
            self._delete_mode = "record"
        values = config.get("error_types", [])
        self._error_types = tuple(dict.fromkeys(
            self._error_type(value) for value in values if isinstance(value, str) and value
        )) if isinstance(values, list) else ()
        try:
            self._retain_hours = max(0, int(config.get("retain_hours", 0)))
        except (TypeError, ValueError, OverflowError):
            self._retain_hours = 0

    @classmethod
    def _error_type(cls, value: Any) -> str:
        if value is None or not str(value).strip():
            return cls._EMPTY_ERROR
        raw_message = str(value).strip()
        public_message = public_error_message(value, context="transfer") or cls._EMPTY_ERROR
        for message in (raw_message, public_message):
            for stage_prefix in ("失败阶段：目标存储访问", "失败阶段:目标存储访问"):
                if message.startswith(stage_prefix):
                    message = message[len(stage_prefix):].lstrip(" \t\r\n：:")
                    break
            if message == cls._TARGET_STATE_TYPE:
                return cls._TARGET_STATE_TYPE
            if message.startswith(cls._TARGET_STATE_ERROR):
                suffix = message[len(cls._TARGET_STATE_ERROR):]
                if not suffix or suffix[0] in "：: \t\r\n":
                    return cls._TARGET_STATE_TYPE
        return public_message

    def _failed_records(self) -> list:
        records = {}
        page_number = 1
        while True:
            page = list_transfer_history(
                {"status": False},
                {"page": page_number, "count": 200,
                 "sort": {"field": "id", "direction": "asc"}},
            )
            for record in page.items:
                if not record.status:
                    records[record.id] = record
            if not page.items or page_number * 200 >= page.total:
                break
            page_number += 1
        return list(records.values())

    def _matches(self, record: Any, selected: tuple, cutoff: Optional[datetime]) -> bool:
        if record.status or self._error_type(record.errmsg) not in selected:
            return False
        if cutoff is None:
            return True
        try:
            recorded_at = datetime.fromisoformat(record.date)
            if recorded_at.tzinfo is None:
                recorded_at = recorded_at.replace(tzinfo=cutoff.tzinfo)
            return recorded_at <= cutoff
        except (ValueError, TypeError):
            return False

    def _delete_record(self, history_id: int, selected: tuple, cutoff: Optional[datetime],
                       delete_mode: str) -> str:
        from app.factory import app

        runtime = getattr(app.state, "host_runtime", None)
        if runtime is None:
            raise RuntimeError("宿主运行时未就绪，停止清理")
        sessions = runtime.persistence.sync_session()
        try:
            session = next(sessions)
            statement = select(TransferHistory).where(TransferHistory.id == history_id)
            record = session.execute(statement).scalar_one_or_none()
            if record is not None and not record.transfer_task_id:
                record = session.execute(statement.with_for_update().execution_options(
                    populate_existing=True
                )).scalar_one_or_none()
                if record is not None and record.transfer_task_id:
                    session.rollback()
                    record = session.execute(statement).scalar_one_or_none()
            if record is None or not self._matches(record, selected, cutoff):
                return "skipped"
            command = get_transfer_history_mutation_command(db=session, runtime=runtime)
            mode_label, delete_source, delete_destination = self._DELETE_MODES[delete_mode]
            result = command.delete(history_id, delete_source=delete_source,
                                    delete_destination=delete_destination)
            if result.history == "deleted":
                return "deleted"
            logger.warning("失败整理记录清理：%s，保留记录 %s：%s", mode_label, history_id, result.message)
            return "retained"
        finally:
            sessions.close()

    def _run(self, preview: bool = False, manual: bool = False) -> Dict[str, Any]:
        if not manual and not self._enabled:
            return {"success": False, "message": "插件未启用"}
        selected = self._error_types
        delete_mode = self._delete_mode
        mode_label = self._DELETE_MODES[delete_mode][0]
        if not selected:
            return {"success": False, "message": "请先勾选错误提示类型并保存配置，不会清理任何记录"}
        if not self._run_lock.acquire(blocking=False):
            return {"success": False, "message": "已有任务正在执行，请稍后重试"}
        try:
            now = datetime.now().astimezone()
            cutoff = now - timedelta(hours=self._retain_hours) if self._retain_hours else None
            records = self._failed_records()
            candidates = [record for record in records if self._matches(record, selected, cutoff)]
            counts = Counter(self._error_type(record.errmsg) for record in candidates)
            summary = {"scanned": len(records), "matched": len(candidates),
                       "deleted": 0, "skipped": 0, "retained": 0, "failed": 0,
                       "types": dict(counts), "preview": preview,
                       "delete_mode": delete_mode, "delete_mode_label": mode_label}
            if not preview:
                for record in candidates:
                    try:
                        outcome = self._delete_record(record.id, selected, cutoff, delete_mode)
                        summary[outcome] += 1
                    except Exception as error:
                        summary["failed"] += 1
                        logger.error("失败整理记录清理：处理记录 %s 失败：%s", record.id, error)
            message = (
                f"预览：扫描 {len(records)} 条失败记录，匹配 {len(candidates)} 条，未执行删除"
                if preview else
                f"匹配 {len(candidates)} 条，删除 {summary['deleted']} 条，"
                f"跳过 {summary['skipped']} 条，宿主保留 {summary['retained']} 条，失败 {summary['failed']} 条"
            )
            message = f"{mode_label}；{message}"
            result = {"success": not (summary["failed"] or summary["retained"]),
                      "message": message, "data": summary}
            self.save_data("last_result", {"time": now.strftime("%Y-%m-%d %H:%M:%S"), **result})
            logger.info("失败整理记录清理：%s", message)
            return result
        except Exception as error:
            logger.error("失败整理记录清理：任务失败：%s", error)
            return {"success": False, "message": "执行失败，请查看插件日志"}
        finally:
            self._run_lock.release()

    def preview(self) -> Dict[str, Any]:
        return self._run(preview=True, manual=True)

    def run_now(self) -> Dict[str, Any]:
        return self._run(manual=True)

    def scheduled_cleanup(self) -> Dict[str, Any]:
        return self._run()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[dict]:
        return []

    def get_api(self) -> List[dict]:
        return [{"path": path, "endpoint": endpoint, "methods": ["POST"],
                 "auth": "bear", "dependencies": [Depends(get_current_active_superuser)],
                 "summary": summary}
                for path, endpoint, summary in (
                    ("/preview", self.preview, "预览失败整理记录清理"),
                    ("/run", self.run_now, "立即清理选中的失败整理记录"),
                )]

    def get_service(self) -> List[dict]:
        if not self._enabled or not self._error_types:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron, timezone=settings.TZ)
        except (ValueError, TypeError) as error:
            logger.error("失败整理记录清理：Cron 无效：%s", error)
            return []
        return [{"id": "TransferFailureCleanerCleanup", "name": "失败整理记录定时清理",
                 "trigger": trigger, "func": self.scheduled_cleanup, "kwargs": {}}]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        warning = ""
        try:
            counts = Counter(self._error_type(record.errmsg) for record in self._failed_records())
        except Exception as error:
            logger.error("失败整理记录清理：读取错误类型失败：%s", error)
            counts = Counter()
            warning = "暂时无法读取失败提示，请稍后重新打开配置页面。"
        options = [{"title": f"{error_type}（{counts[error_type]} 条）", "value": error_type}
                   for error_type in sorted(set(counts) | set(self._error_types))]
        return [{"component": "VForm", "props": {"class": "pa-2"}, "content": [
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal",
                "class": "mb-5", "style": "line-height: 1.7;",
                "text": "文件删除不可恢复，请先预览。删除源文件会联动删除对应下载任务。"}},
            {"component": "VCard", "props": {"variant": "outlined", "class": "mb-5"}, "content": [
                {"component": "VCardText", "props": {"class": "pa-4"}, "content": [
                    {"component": "div", "props": {"class": "text-subtitle-1 font-weight-medium mb-4"},
                        "text": "清理规则"},
                    {"component": "VSelect", "props": {"model": "delete_mode", "label": "删除方式",
                        "class": "mb-6", "density": "comfortable",
                        "items": [{"title": mode[0], "value": key} for key, mode in self._DELETE_MODES.items()],
                        "item-title": "title", "item-value": "value",
                        "hint": "与 MP 删除弹窗一致；默认仅删记录。", "persistent-hint": True}},
                    {"component": "VSelect", "props": {"model": "error_types", "label": "要清理的错误类型",
                        "class": "mb-6", "density": "comfortable",
                        "items": options, "item-title": "title", "item-value": "value", "multiple": True,
                        "chips": True, "closable-chips": True,
                        "hint": "按错误类型匹配，未勾选时不清理。", "persistent-hint": True}},
                    {"component": "VTextField", "props": {"model": "retain_hours", "label": "保留最近多少小时",
                        "density": "comfortable", "type": "number", "min": 0,
                        "hint": "0 不限制；设置保留时间后，时间无效的记录会跳过。", "persistent-hint": True}},
                ]},
            ]},
            {"component": "VCard", "props": {"variant": "outlined", "class": "mb-5"}, "content": [
                {"component": "VCardText", "props": {"class": "pa-4"}, "content": [
                    {"component": "div", "props": {"class": "text-subtitle-1 font-weight-medium mb-2"},
                        "text": "定时设置"},
                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用定时清理",
                        "class": "mb-4", "hide-details": True}},
                    {"component": "VTextField", "props": {"model": "cron", "label": "执行周期（Cron）",
                        "density": "comfortable", "hint": "默认每天 03:00，使用 MP 时区。", "persistent-hint": True}},
                ]},
            ]},
            {"component": "VAlert", "props": {"type": "warning" if warning else "info", "variant": "tonal",
                "class": "mb-4", "style": "line-height: 1.7;",
                "text": warning or "错误类型来自当前失败记录；无法确认目标文件状态的错误会合并，忽略剧名、集数和底层异常。重新打开设置可刷新选项。"}},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                "style": "line-height: 1.7;",
                "text": "保存后到“查看数据”预览或清理。手动清理无需开启定时开关，也使用已保存的删除方式。"}},
        ]}], {"enabled": False, "cron": "0 3 * * *", "error_types": [], "retain_hours": 0,
              "delete_mode": "record"}

    def get_page(self) -> List[dict]:
        last = self.get_data("last_result") or {}
        text = f"{last.get('time', '')} {last.get('message', '')}" if last else "尚无执行记录"
        content = [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": text}}]
        content.append({"component": "VAlert", "props": {"type": "warning", "variant": "tonal",
                        "text": f"当前已保存的删除方式：{self._DELETE_MODES[self._delete_mode][0]}。"
                                "涉及文件删除时不可恢复；请先预览，再立即清理。"}})
        for label, path, color in (("预览匹配记录（不删除）", "preview", "primary"),
                                   ("立即清理已选类型", "run", "warning")):
            content.append({"component": "VBtn", "props": {"color": color, "class": "ma-2"},
                            "text": label, "events": {"click": {
                                "api": f"plugin/TransferFailureCleaner/{path}", "method": "post"}}})
        for error_type, count in last.get("data", {}).get("types", {}).items():
            content.append({"component": "VAlert", "props": {"type": "info", "variant": "text",
                            "text": f"{error_type}：{count} 条"}})
        return [{"component": "VCard", "content": [{"component": "VCardText", "content": content}]}]

    def stop_service(self) -> None:
        pass
