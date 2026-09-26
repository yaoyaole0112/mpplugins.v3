from typing import Any, Dict, List, Optional, Tuple

from app.db.oper.subscribe import SubscribeOper
from app.db.oper.user import UserOper
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger


class SubscribeOwner(_PluginBase):
    plugin_name = "订阅用户修正"
    plugin_desc = "新建电视剧订阅时，将插件名称归属修正为指定的 MoviePilot 用户"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot/v3/docs/images/moviepilot.png"
    plugin_version = "1.0.0"
    plugin_author = "helios"
    author_url = "https://github.com/yaoyaole0112/mpplugins.v3"
    plugin_config_prefix = "subscribeowner_"
    plugin_order = 20
    auth_level = 2

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._target_username = str(config.get("target_username") or "").strip()

    @eventmanager.register(EventType.SubscribeAdded)
    def on_subscribe_added(self, event: Event) -> None:
        if not self._enabled or not event or not event.event_data:
            return
        subscribe_id = event.event_data.get("subscribe_id")
        if isinstance(subscribe_id, bool) or not str(subscribe_id).isdigit():
            return
        try:
            users = UserOper().list()
            active_users = [user for user in users if user.is_active]
            if self._target_username:
                target = next(
                    (user for user in active_users if user.name == self._target_username),
                    None,
                )
            else:
                target = active_users[0] if len(active_users) == 1 else None
            if target is None:
                logger.warning("订阅用户修正：目标用户不存在，或未指定且启用了多个用户")
                return

            subscription = SubscribeOper().get(int(subscribe_id))
            if not subscription or subscription.type != "电视剧":
                return
            if subscription.username == target.name or subscription.username in {
                user.name for user in users
            }:
                return

            SubscribeOper().update(subscription.id, {"username": target.name})
            logger.info(
                "订阅用户修正：%s（ID %s）归属从 %s 改为 %s",
                subscription.name,
                subscription.id,
                subscription.username,
                target.name,
            )
        except Exception as error:
            logger.error("订阅用户修正：处理订阅 ID %s 失败：%s", subscribe_id, error)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {"model": "enabled", "label": "自动修正新建电视剧订阅"},
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "target_username",
                            "label": "目标 MoviePilot 用户名",
                            "hint": "留空时仅在系统恰好只有一个启用用户时自动选择",
                            "persistent-hint": True,
                        },
                    },
                ],
            }
        ], {"enabled": False, "target_username": ""}

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self) -> None:
        return None
