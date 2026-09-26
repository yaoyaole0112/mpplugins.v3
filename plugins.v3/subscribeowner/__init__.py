from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends

from app.api.dependencies.auth import get_current_active_superuser
from app.db.oper.subscribe import SubscribeOper
from app.db.oper.user import UserOper
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger


class SubscribeOwner(_PluginBase):
    plugin_name = "订阅用户修正"
    plugin_desc = "定时检查全部订阅，并在新建订阅时修正归属用户"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot/v3/docs/images/moviepilot.png"
    plugin_version = "1.2.1"
    plugin_author = "helios"
    author_url = "https://github.com/yaoyaole0112/mpplugins.v3"
    plugin_config_prefix = "subscribeowner_"
    plugin_order = 20
    auth_level = 2

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._target_username = str(config.get("target_username") or "").strip()
        self._cron = str(config.get("cron") or "*/30 * * * *").strip()

    def _resolve_target(self) -> Optional[str]:
        active_users = [user for user in UserOper().list() if user.is_active]
        if self._target_username:
            target = next(
                (user for user in active_users if user.name == self._target_username),
                None,
            )
        else:
            target = active_users[0] if len(active_users) == 1 else None
        if target is None:
            logger.warning("订阅用户修正：目标用户不存在，或未指定且启用了多个用户")
            return None
        return target.name

    @staticmethod
    def _update_owner(subscription: Any, target_username: str) -> bool:
        if subscription.username == target_username:
            return False
        return SubscribeOper().update(
            subscription.id, {"username": target_username}
        ) is not None

    @eventmanager.register(EventType.SubscribeAdded)
    def on_subscribe_added(self, event: Event) -> None:
        if not self._enabled or not event or not event.event_data:
            return
        subscribe_id = event.event_data.get("subscribe_id")
        if isinstance(subscribe_id, bool) or not str(subscribe_id).isdigit():
            return
        logger.info("订阅用户修正：收到新增订阅 ID %s", subscribe_id)
        try:
            target_username = self._resolve_target()
            if target_username is None:
                return
            subscription = SubscribeOper().get(int(subscribe_id))
            if not subscription:
                logger.warning("订阅用户修正：未找到新增订阅 ID %s", subscribe_id)
                return
            previous_username = subscription.username
            if self._update_owner(subscription, target_username):
                logger.info(
                    "订阅用户修正：%s（ID %s）归属从 %s 改为 %s",
                    subscription.name,
                    subscription.id,
                    previous_username,
                    target_username,
                )
            else:
                logger.info(
                    "订阅用户修正：新增订阅 ID %s 已归属 %s，无需修改",
                    subscribe_id, target_username,
                )
        except Exception as error:
            logger.error("订阅用户修正：处理订阅 ID %s 失败：%s", subscribe_id, error)

    def sync_subscriptions(self, manual: bool = False) -> Dict[str, Any]:
        if not self._enabled and not manual:
            return {"success": False, "message": "插件未启用"}
        logger.info("订阅用户修正：开始%s检查全部订阅", "手动" if manual else "定时")
        try:
            target_username = self._resolve_target()
            if target_username is None:
                return {"success": False, "message": "请配置有效的目标用户"}
            subscriptions = SubscribeOper().list()
        except Exception as error:
            logger.error("订阅用户修正：读取订阅失败：%s", error)
            return {"success": False, "message": "读取订阅失败，请查看插件日志"}

        updated = 0
        failed = 0
        for subscription in subscriptions:
            try:
                updated += self._update_owner(subscription, target_username)
            except Exception as error:
                failed += 1
                logger.error("订阅用户修正：订阅 ID %s 更新失败：%s", subscription.id, error)
        logger.info(
            "订阅用户修正：检查 %s 条，修正 %s 条，失败 %s 条，目标用户 %s",
            len(subscriptions), updated, failed, target_username,
        )
        result = {
            "success": failed == 0,
            "message": f"检查 {len(subscriptions)} 条，修正 {updated} 条，失败 {failed} 条",
            "data": {"total": len(subscriptions), "updated": updated, "failed": failed},
        }
        try:
            self.save_data("last_result", {
                "time": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                "mode": "手动" if manual else "定时",
                "message": result["message"],
            })
        except Exception as error:
            logger.warning("订阅用户修正：保存执行摘要失败：%s", error)
        return result

    def run_now(self) -> Dict[str, Any]:
        return self.sync_subscriptions(manual=True)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run",
            "endpoint": self.run_now,
            "methods": ["POST"],
            "auth": "bear",
            "dependencies": [Depends(get_current_active_superuser)],
            "summary": "立即修正全部订阅用户",
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except ValueError as error:
            logger.error("订阅用户修正：执行周期无效：%s", error)
            return []
        return [{
            "id": self.__class__.__name__,
            "name": self.plugin_name,
            "trigger": trigger,
            "func": self.sync_subscriptions,
            "kwargs": {},
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "enabled", "label": "自动修正全部订阅",
                            "class": "mb-4",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "target_username",
                            "label": "目标 MoviePilot 用户名",
                            "hint": "留空时仅在系统恰好只有一个启用用户时自动选择",
                            "persistent-hint": True,
                            "class": "mb-6",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "cron",
                            "label": "定时执行周期（Cron）",
                            "hint": "默认每 30 分钟检查全部已有订阅",
                            "persistent-hint": True,
                            "class": "mt-2 mb-4",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "class": "mt-4",
                            "text": "立即运行请点击下方“查看数据”，在插件页面使用按钮；执行记录可在插件日志查看。",
                        },
                    },
                ],
            }
        ], {"enabled": False, "target_username": "", "cron": "*/30 * * * *"}

    def get_page(self) -> List[dict]:
        last_result = self.get_data("last_result") or {}
        status = (
            f"上次{last_result.get('mode', '')}执行：{last_result.get('time', '')}，"
            f"{last_result.get('message', '')}"
            if isinstance(last_result, dict) and last_result.get("time")
            else "尚无执行记录"
        )
        return [
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-6"},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "variant": "tonal",
                                    "prepend-icon": "mdi-play",
                                },
                                "text": "立即运行",
                                "events": {
                                    "click": {
                                        "api": "plugin/SubscribeOwner/run",
                                        "method": "post",
                                    },
                                },
                            },
                            {
                                "component": "div",
                                "props": {"class": "text-body-2 mt-4"},
                                "text": status,
                            },
                        ],
                    },
                ],
            },
        ]

    def stop_service(self) -> None:
        return None
