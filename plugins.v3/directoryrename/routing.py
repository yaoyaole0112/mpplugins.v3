"""Select templates within native planning and library lookup scopes."""

import json
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from inspect import signature
from pathlib import PurePosixPath
from threading import RLock
from types import SimpleNamespace

from jinja2 import Environment


_STATE_ATTRIBUTE = "_directory_rename_routing_state"
_INSTALL_LOCK = RLock()


def normalize_path(value) -> PurePosixPath:
    if not isinstance(value, (str, PurePosixPath)):
        raise ValueError("目标路径必须是绝对路径")
    text = str(value)
    path = PurePosixPath(text)
    if not text.startswith("/") or "\\" in text or ".." in path.parts:
        raise ValueError("目标路径必须是绝对路径，且不能包含反斜杠或上级目录")
    return path


@dataclass(frozen=True)
class Rule:
    name: str
    storage: str
    path: PurePosixPath
    movie: str
    tv: str


def parse_rules(raw: str) -> tuple[Rule, ...]:
    entries = json.loads(raw)
    if not isinstance(entries, list):
        raise ValueError("重命名规则必须是 JSON 数组")
    rules = []
    seen = set()
    environment = Environment()
    for index, item in enumerate(entries, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 条规则必须是对象")
        storage = item.get("storage")
        if not isinstance(storage, str) or not storage.strip():
            raise ValueError(f"第 {index} 条规则缺少目标存储")
        path = normalize_path(item.get("path"))
        key = (storage, path)
        if key in seen:
            raise ValueError(f"第 {index} 条规则重复指定同一目标存储和目录")
        seen.add(key)
        templates = {}
        for media_type in ("movie", "tv"):
            template = item.get(media_type, "")
            if not isinstance(template, str):
                raise ValueError(f"第 {index} 条规则的 {media_type} 模板必须是文本")
            if template:
                environment.parse(template)
                if template.startswith("/") or "\\" in template:
                    raise ValueError(f"第 {index} 条规则的模板必须生成相对路径")
            templates[media_type] = template
        if not any(templates.values()):
            raise ValueError(f"第 {index} 条规则至少需要一个影视模板")
        rules.append(
            Rule(str(item.get("name") or index), storage, path, **templates)
        )
    return tuple(sorted(rules, key=lambda rule: len(rule.path.parts), reverse=True))


def select_template(rules, storage, path, media_type):
    kind = {"电影": "movie", "电视剧": "tv"}.get(
        getattr(media_type, "value", media_type)
    )
    if not kind or not path:
        return None
    try:
        target = normalize_path(path)
    except ValueError:
        return None
    for rule in rules:
        if storage == rule.storage and (
            target == rule.path or rule.path in target.parents
        ):
            template = getattr(rule, kind)
            if template:
                return template
    return None


def install_hooks(handler_module, filemanager_module):
    """Keep one shared state across hot reloads and third-party wrapper chains."""
    with _INSTALL_LOCK:
        existing = getattr(handler_module, _STATE_ATTRIBUTE, None)
        if existing is not None:
            return existing

        handler = handler_module.TransHandler
        manager = filemanager_module.FileManagerModule
        # Use the outer module boundary, not the 115 plugin's batch-plan hook.
        original_plan = manager.plan_transfer
        original_lookup = manager.media_files
        original_destination = handler.get_dest_dir
        original_explicit_destination = handler.get_dest_path
        plan_signature = signature(original_plan)
        destination_signature = signature(original_destination)
        if not {"fileitem", "target_directory", "target_storage", "target_path"} <= set(
            plan_signature.parameters
        ):
            raise RuntimeError("当前 MoviePilot 整理计划接口不兼容，未安装命名钩子")

        state = SimpleNamespace(
            rules=(),
            owner=None,
            target=ContextVar("directory_rename_target", default=None),
            lookup=ContextVar("directory_rename_lookup", default=None),
        )

        def scoped_getter(original_getter):
            @wraps(original_getter)
            def get_setting(name, *args, **kwargs):
                value = original_getter(name, *args, **kwargs)
                target = state.target.get()
                if name != "RENAME_FORMAT" or target is None:
                    return value
                storage, path, rules = target

                def resolve(media_type):
                    return (
                        select_template(rules, storage, path, media_type)
                        or value(media_type)
                    )

                return resolve

            return get_setting

        @wraps(original_plan)
        def plan_transfer(*args, **kwargs):
            rules = state.rules
            if not rules:
                return original_plan(*args, **kwargs)
            bound = plan_signature.bind(*args, **kwargs)
            directory = bound.arguments.get("target_directory")
            source = bound.arguments["fileitem"]
            storage = (
                bound.arguments.get("target_storage")
                or getattr(directory, "library_storage", None)
                or source.storage
                or "local"
            )
            path = (
                getattr(directory, "library_path", None)
                if directory is not None
                else bound.arguments.get("target_path")
            )
            token = state.target.set(
                (storage, path, rules)
            )
            lookup_token = state.lookup.set(None)
            try:
                return original_plan(*args, **kwargs)
            finally:
                state.lookup.reset(lookup_token)
                state.target.reset(token)

        @wraps(original_lookup)
        def media_files(*args, **kwargs):
            rules = state.rules
            if not rules:
                return original_lookup(*args, **kwargs)
            lookup_token = state.lookup.set(rules)
            target_token = state.target.set(None)
            try:
                return original_lookup(*args, **kwargs)
            finally:
                state.target.reset(target_token)
                state.lookup.reset(lookup_token)

        @wraps(original_destination)
        def get_dest_dir(*args, **kwargs):
            result = original_destination(*args, **kwargs)
            rules = state.lookup.get()
            if rules is not None:
                directory = destination_signature.bind(
                    *args, **kwargs
                ).arguments["target_dir"]
                state.target.set((directory.library_storage, result, rules))
            elif state.target.get() is not None:
                storage, _, rules = state.target.get()
                state.target.set((storage, result, rules))
            return result

        @wraps(original_explicit_destination)
        def get_dest_path(*args, **kwargs):
            result = original_explicit_destination(*args, **kwargs)
            if state.target.get() is not None:
                storage, _, rules = state.target.get()
                state.target.set((storage, result, rules))
            return result

        handler_module.get_runtime_setting = scoped_getter(
            handler_module.get_runtime_setting
        )
        filemanager_module.get_runtime_setting = scoped_getter(
            filemanager_module.get_runtime_setting
        )
        manager.plan_transfer = plan_transfer
        handler.get_dest_dir = staticmethod(get_dest_dir)
        handler.get_dest_path = staticmethod(get_dest_path)
        manager.media_files = media_files
        setattr(handler_module, _STATE_ATTRIBUTE, state)
        return state
