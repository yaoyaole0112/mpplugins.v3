"""配置界面及只读校验接口测试。"""

import importlib.util
import json
import sys
from pathlib import Path

from test_routing import defaults


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "directoryrename_ui_test",
    PLUGIN_ROOT / "__init__.py",
    submodule_search_locations=[str(PLUGIN_ROOT)],
)
plugin_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin_module
spec.loader.exec_module(plugin_module)


def test_validation_is_read_only():
    """校验既有规则不修改输入配置和插件开关。"""
    plugin = object.__new__(plugin_module.DirectoryRename)
    config = defaults.default_config()
    config["enabled"] = True
    snapshot = dict(config)
    plugin._enabled = False
    result = plugin.validate_config(config)
    assert result["success"]
    assert result["data"] == {"valid": True, "rule_count": 2}
    assert config == snapshot
    assert plugin._enabled is False


def test_validation_rejects_invalid_template():
    """损坏模板在保存前被拒绝。"""
    plugin = object.__new__(plugin_module.DirectoryRename)
    config = defaults.default_config()
    rules = json.loads(config["rules_json"])
    rules[0]["tv"] = "{{broken"
    config["rules_json"] = json.dumps(rules)
    result = plugin.validate_config(config)
    assert not result["success"]
    assert result["data"]["valid"] is False


def test_empty_enabled_config_is_rejected():
    """启用状态必须至少保留一条规则。"""
    plugin = object.__new__(plugin_module.DirectoryRename)
    assert not plugin.validate_config({"enabled": True, "rules_json": "[]"})["success"]
    assert plugin.validate_config({"enabled": False, "rules_json": "[]"})["success"]


def test_vue_form_retains_legacy_defaults():
    """Vue 表单沿用既有配置格式，不要求数据迁移。"""
    plugin = object.__new__(plugin_module.DirectoryRename)
    assert plugin.get_render_mode() == ("vue", "dist/assets")
    fields, model = plugin.get_form()
    assert fields == []
    assert model == defaults.default_config()
    routes = plugin.get_api()
    assert routes[0]["path"] == "/validate"
    assert routes[0]["dependencies"]
