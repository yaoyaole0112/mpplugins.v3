"""MoviePilot plugin for destination-specific renaming."""

from fastapi import Depends
from jinja2 import TemplateError

from app.api.dependencies.auth import get_current_active_superuser
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .defaults import default_config
from .routing import install_hooks, parse_rules


class DirectoryRename(_PluginBase):
    plugin_name = "目标目录重命名"
    plugin_desc = "按目标存储和目录为电影、电视剧选择独立重命名模板。"
    plugin_version = "1.1.1"
    plugin_author = "Local"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot/v3/docs/images/moviepilot.png"
    plugin_config_prefix = "directoryrename_"
    plugin_order = 99
    auth_level = 1

    def init_plugin(self, config=None):
        self.stop_service()
        config = config or {}
        if not config.get("enabled"):
            return
        if self.is_clone:
            raise ValueError("目标目录重命名请在本体配置多条规则，不支持独立分身")
        rules = parse_rules(config.get("rules_json", "[]"))
        if not rules:
            raise ValueError("启用目标目录重命名时至少需要一条规则")

        from app.modules.filemanager import module, transhandler

        self._routing_state = install_hooks(transhandler, module)
        self._routing_state.owner = self
        self._routing_state.rules = rules
        self._enabled = True
        logger.info(f"目标目录重命名已启用：{len(rules)} 条目录规则")

    def get_state(self):
        return bool(getattr(self, "_enabled", False))

    def stop_service(self):
        self._enabled = False
        state = getattr(self, "_routing_state", None)
        if state is not None and state.owner is self:
            # Existing scopes retain their snapshot; new calls fall back immediately.
            state.rules = ()
            state.owner = None

    def get_api(self):
        return [{
            "path": "/validate",
            "endpoint": self.validate_config,
            "methods": ["POST"],
            "auth": "bear",
            "dependencies": [Depends(get_current_active_superuser)],
            "summary": "校验目标目录命名规则（不保存配置）",
        }]

    def validate_config(self, config: dict) -> dict:
        """
        校验表单生成的目录规则，不修改配置或运行状态。

        :param config: 包含启用状态和 rules_json 的待保存配置
        :return: 规则校验结果及数量
        """
        try:
            rules = parse_rules(config.get("rules_json", "[]"))
            if config.get("enabled") and not rules:
                raise ValueError("启用时至少需要一条目标目录规则")
        except (ValueError, TypeError, TemplateError) as error:
            return {
                "success": False,
                "message": f"规则校验失败：{error}",
                "data": {"valid": False},
            }
        return {
            "success": True,
            "message": "",
            "data": {"valid": True, "rule_count": len(rules)},
        }

    @staticmethod
    def get_render_mode():
        return "vue", "dist/assets"

    def get_page(self):
        return []

    def get_form(self):
        return [], default_config()
