import asyncio
import json
import re

from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.dependencies.auth import get_current_active_superuser

from .user_account import UserAccountError
from .telegram_targets import telegram_targets
from .file_size import minimum_file_bytes, minimum_gb_value, minimum_gb_setting


CONFIG_FIELDS = ("enabled", "folder", "recursive", "monitor_enabled", "output_enabled", "output", "extensions",
                 "min_age", "min_file_size", "delete_source", "delete_small_files", "scan_enabled", "cron", "telegram_enabled", "telegram_mode",
                 "telegram_chat_id", "telegram_thread_id", "cloud_enabled", "cloud_scan_enabled", "cloud_storage", "cloud_folder", "cloud_recursive", "cloud_cron",
                 "cloud_read_timeout", "cloud_monitor_enabled", "cloud_life_source")
BOOLEAN_FIELDS = {"enabled", "recursive", "monitor_enabled", "output_enabled", "delete_source", "delete_small_files", "scan_enabled", "telegram_enabled", "cloud_enabled", "cloud_scan_enabled", "cloud_recursive", "cloud_monitor_enabled"}


def settings_snapshot(plugin):
    result = {name: getattr(plugin, f"_{name}") for name in CONFIG_FIELDS}
    result["telegram_mode"] = "user"
    result["min_file_size_gb"] = minimum_gb_value(plugin._min_file_size)
    result["include_subdirectories"] = plugin._recursive if plugin._recursive == plugin._cloud_recursive else None
    return result


def validate_settings(plugin, values):
    values = dict(values)
    if "include_subdirectories" in values:
        shared_recursive = values.pop("include_subdirectories")
        if not isinstance(shared_recursive, bool):
            raise UserAccountError("请明确选择是否包含子目录")
        if {"recursive", "cloud_recursive"} & set(values):
            raise UserAccountError("子目录配置重复，请刷新页面后重新保存")
        values.update(recursive=shared_recursive, cloud_recursive=shared_recursive)
    if "min_file_size_gb" in values:
        if "min_file_size" in values:
            raise UserAccountError("大小阈值配置重复，请刷新页面重试")
        try:
            values["min_file_size"] = minimum_gb_setting(values.pop("min_file_size_gb"))
        except ValueError as failure:
            raise UserAccountError(str(failure)) from None
    if set(values) - set(CONFIG_FIELDS) - {"telegram_token"}:
        raise UserAccountError("包含未知配置项，请刷新页面后重新保存")
    result = {name: getattr(plugin, f"_{name}") for name in CONFIG_FIELDS}
    for name, value in values.items():
        if name in BOOLEAN_FIELDS:
            if not isinstance(value, bool):
                raise UserAccountError("开关配置格式不正确")
        elif name in {"min_age", "cloud_read_timeout"}:
            lower, upper = (0, 86400) if name == "min_age" else (30, 1800)
            if isinstance(value, bool) or not str(value).isdigit() or not lower <= int(value) <= upper:
                raise UserAccountError(f"{'文件稳定时间' if name == 'min_age' else '网盘读取超时'}必须为 {lower} 到 {upper} 的整数秒")
            value = int(value)
        else:
            if not isinstance(value, str) or len(value) > 4096:
                raise UserAccountError("配置文本格式不正确或过长")
            value = value.strip()
        result[name] = value
    result["telegram_mode"] = "user"
    try:
        minimum_file_bytes(result["min_file_size"])
    except ValueError as failure:
        raise UserAccountError(str(failure)) from None
    if result["cloud_life_source"] not in {"shared", "direct"}:
        raise UserAccountError("生活事件来源无效")
    if result["cloud_monitor_enabled"] and result["cloud_storage"] != "115网盘Plus":
        raise UserAccountError("115生活事件仅支持115网盘Plus")
    try:
        CronTrigger.from_crontab(result["cron"])
    except Exception:
        raise UserAccountError("定时补扫计划格式无效") from None
    if result["telegram_thread_id"] and (not result["telegram_thread_id"].isdigit() or int(result["telegram_thread_id"]) <= 0):
        raise UserAccountError("话题线程 ID 必须为正整数或留空")
    result["telegram_token"] = plugin._telegram_token
    if result["cloud_enabled"]:
        from .cloud_storage import SUPPORTED_STORAGES, available_storages, cloud_path, CloudError
        try:
            if result["cloud_storage"] not in SUPPORTED_STORAGES:
                raise CloudError("请选择 115网盘Plus 或 CloudDrive储存")
            result["cloud_folder"] = cloud_path(result["cloud_folder"])
            if result["cloud_folder"] == "/":
                raise CloudError("请指定具体的网盘子目录，不允许扫描整个网盘根目录")
            if result["cloud_storage"] not in {item["type"] for item in available_storages()}:
                raise CloudError("所选网盘存储未配置，请先启用并登录对应插件")
            CronTrigger.from_crontab(result["cloud_cron"])
        except (CloudError, ValueError, TypeError) as error:
            raise UserAccountError(f"网盘配置无效：{error}") from None
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

    async def cloud_status(principal=Depends(get_current_active_superuser)):
        return response(await asyncio.to_thread(plugin.cloud_status))

    async def cloud_run(principal=Depends(get_current_active_superuser)):
        return response(plugin.queue_cloud_scan())

    async def cloud_browse(request: Request, principal=Depends(get_current_active_superuser)):
        from .cloud_storage import CloudStorage, CloudError, cloud_path
        values = await read_body(request)
        if set(values) != {"storage", "path"} or any(not isinstance(value, str) for value in values.values()):
            raise HTTPException(status_code=400, detail="请提供存储类型和目录")
        try:
            path = cloud_path(values["path"])
            def browse():
                storage = CloudStorage(values["storage"])
                return [{"name": entry["name"], "path": entry["path"]} for entry in storage.list(path) if entry["type"] == "dir"]
            return response({"ok": True, "path": path, "folders": await asyncio.to_thread(browse)})
        except Exception as error:
            detail = str(error) if isinstance(error, CloudError) else f"读取网盘目录失败（{type(error).__name__}），请检查存储插件登录状态"
            return response({"ok": False, "message": detail})

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
                ("/cloud/status", cloud_status, "GET", "只读网盘来源及生成进度"),
                ("/cloud/run", cloud_run, "POST", "网盘增量扫描与补发"),
                ("/cloud/browse", cloud_browse, "POST", "浏览网盘子目录"),
                ("/settings", status, "GET", "读取安全配置"), ("/settings", save, "POST", "保存插件配置"),
                ("/telegram/status", telegram_status, "GET", "个人账号登录状态"),
                ("/telegram/action", telegram_action, "POST", "个人账号网页授权"), ("/run", run, "POST", "增量补扫与补发"))]
