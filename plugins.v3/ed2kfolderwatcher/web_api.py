import asyncio
import json
import re

from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.dependencies.auth import get_current_active_superuser

from .user_account import UserAccountError
from .telegram_targets import telegram_targets


CONFIG_FIELDS = ("enabled", "folder", "recursive", "monitor_enabled", "output_enabled", "output", "extensions",
                 "min_age", "delete_source", "scan_enabled", "cron", "telegram_enabled", "telegram_mode",
                 "telegram_chat_id", "telegram_thread_id")
BOOLEAN_FIELDS = {"enabled", "recursive", "monitor_enabled", "output_enabled", "delete_source", "scan_enabled", "telegram_enabled"}


def settings_snapshot(plugin):
    result = {name: getattr(plugin, f"_{name}") for name in CONFIG_FIELDS}
    result["telegram_mode"] = "user"
    return result


def validate_settings(plugin, values):
    if set(values) - set(CONFIG_FIELDS) - {"telegram_token"}:
        raise UserAccountError("包含未知配置项，请刷新页面后重新保存")
    result = {name: getattr(plugin, f"_{name}") for name in CONFIG_FIELDS}
    for name, value in values.items():
        if name in BOOLEAN_FIELDS:
            if not isinstance(value, bool):
                raise UserAccountError("开关配置格式不正确")
        elif name == "min_age":
            if isinstance(value, bool) or not str(value).isdigit() or not 0 <= int(value) <= 86400:
                raise UserAccountError("文件稳定时间必须为 0 到 86400 的整数秒")
            value = int(value)
        else:
            if not isinstance(value, str) or len(value) > 4096:
                raise UserAccountError("配置文本格式不正确或过长")
            value = value.strip()
        result[name] = value
    result["telegram_mode"] = "user"
    try:
        CronTrigger.from_crontab(result["cron"])
    except Exception:
        raise UserAccountError("定时补扫计划格式无效") from None
    if result["telegram_thread_id"] and (not result["telegram_thread_id"].isdigit() or int(result["telegram_thread_id"]) <= 0):
        raise UserAccountError("话题线程 ID 必须为正整数或留空")
    result["telegram_token"] = plugin._telegram_token
    if result["telegram_enabled"] or result["telegram_chat_id"]:
        try:
            telegram_targets(result["telegram_chat_id"], result["telegram_thread_id"])
        except ValueError as error:
            raise UserAccountError(str(error)) from None
    result["onlyonce"] = False
    return result


async def read_body(request):
    body = await request.body()
    if len(body) > 16384:
        raise HTTPException(status_code=400, detail="请求内容过长")
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=400, detail="请求 JSON 无效") from None
    if not isinstance(result, dict):
        raise HTTPException(status_code=400, detail="请求必须为 JSON 对象")
    return result


def response(content):
    return JSONResponse(content, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def build_routes(plugin):
    async def recent(principal=Depends(get_current_active_superuser)):
        return response(await asyncio.to_thread(plugin.recent_records))

    async def delete_source(request: Request, principal=Depends(get_current_active_superuser)):
        values = await read_body(request)
        if set(values) != {"id", "confirm"} or values.get("confirm") is not True or not isinstance(values.get("id"), str) or not re.fullmatch(r"[0-9a-f]{64}", values["id"]):
            raise HTTPException(status_code=400, detail="请确认删除并提供有效的记录 ID")
        return response(await asyncio.to_thread(plugin.delete_source, values["id"]))

    async def status(principal=Depends(get_current_active_superuser)):
        return response({"ok": True, "settings": settings_snapshot(plugin)})

    async def save(request: Request, principal=Depends(get_current_active_superuser)):
        try:
            values = await read_body(request)
            config = validate_settings(plugin, values)
            plugin.update_config(config)
            await asyncio.to_thread(plugin.init_plugin, config)
            from app.scheduler import Scheduler
            Scheduler().update_plugin_job(plugin.__class__.__name__)
            return response({"ok": True, "settings": settings_snapshot(plugin), "message": "插件设置已保存"})
        except UserAccountError as error:
            return response({"ok": False, "message": str(error)})

    async def telegram_status(principal=Depends(get_current_active_superuser)):
        try:
            return response(await plugin.telegram_account_status())
        except UserAccountError as error:
            return response({"ok": False, "message": str(error)})

    async def telegram_action(request: Request, principal=Depends(get_current_active_superuser)):
        values = await read_body(request)
        operation = values.get("operation")
        payload = values.get("values") or {}
        if not isinstance(operation, str) or not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="账号操作格式无效")
        allowed = {"save": {"api_id", "api_hash"}, "send_code": {"phone"}, "sign_in": {"code", "password"},
                   "logout": set(), "status": set(), "cancel": set()}
        if operation not in allowed or set(payload) - allowed[operation] or any(not isinstance(value, (str, int)) or isinstance(value, bool) or len(str(value)) > 512 for value in payload.values()):
            raise HTTPException(status_code=400, detail="账号操作字段无效")
        try:
            result = await plugin.telegram_account_action(values)
            if result.get("logged_in") or operation == "save":
                plugin._telegram_retry_after = 0
                plugin._telegram_target_retry = {}
            return response(result)
        except UserAccountError as error:
            manager = plugin._get_web_login()
            return response({**manager.snapshot(), "ok": False, "message": str(error)})

    async def run(principal=Depends(get_current_active_superuser)):
        if not plugin._enabled:
            return response({"ok": False, "message": "请先启用并保存插件设置"})
        await asyncio.to_thread(plugin.scan)
        return response({"ok": True, "message": "增量补扫已加入队列；已有链接不重复计算"})

    return [{"path": path, "endpoint": endpoint, "methods": [method], "auth": "bear", "summary": summary}
            for path, endpoint, method, summary in (
                ("/records", recent, "GET", "最近生成链接与删源状态"),
                ("/records/delete-source", delete_source, "POST", "确认后手动删除记录对应的源文件"),
                ("/settings", status, "GET", "读取安全配置"), ("/settings", save, "POST", "保存插件配置"),
                ("/telegram/status", telegram_status, "GET", "个人账号登录状态"),
                ("/telegram/action", telegram_action, "POST", "个人账号网页授权"), ("/run", run, "POST", "增量补扫与补发"))]
