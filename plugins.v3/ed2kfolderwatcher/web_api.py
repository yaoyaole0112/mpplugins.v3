import asyncio
import json
import re

from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.dependencies.auth import get_current_active_superuser
from app.log import logger

from .user_account import UserAccountError
from .telegram_targets import telegram_targets
from .file_size import minimum_file_bytes, minimum_gb_value, minimum_gb_setting
from .local_upload import UPLOAD_FIELDS


CONFIG_FIELDS = ("enabled", "folder", "recursive", "monitor_enabled", "output_enabled", "output", "extensions",
                 "min_age", "min_file_size", "local_delete_source", "cloud_delete_source", "delete_small_files", "cloud_delete_enabled", "scan_enabled", "cron", "telegram_enabled", "telegram_mode",
                 "telegram_chat_id", "telegram_thread_id", "telegram_aggregate", "telegram_aggregate_window", "cloud_enabled", "cloud_scan_enabled", "cloud_storage", "cloud_folder", "cloud_recursive", "cloud_cron",
                 "cloud_read_timeout", "cloud_monitor_enabled", "cloud_life_source", "retry_limit", "movie_series_parallel") + UPLOAD_FIELDS
BOOLEAN_FIELDS = {"enabled", "recursive", "monitor_enabled", "output_enabled", "local_delete_source", "local_upload_delete_source", "local_upload_delete_cloud_copy", "cloud_delete_source", "delete_small_files", "cloud_delete_enabled", "scan_enabled", "telegram_enabled", "telegram_aggregate", "cloud_enabled", "cloud_scan_enabled", "cloud_recursive", "cloud_monitor_enabled", "movie_series_parallel", "local_upload_enabled"}


def settings_snapshot(plugin, config=None):
    source = config or {}
    result = {name: source.get(name, getattr(plugin, f"_{name}")) for name in CONFIG_FIELDS}
    result["telegram_mode"] = "user"
    result["min_file_size_gb"] = minimum_gb_value(result["min_file_size"])
    result["include_subdirectories"] = result["recursive"] if result["recursive"] == result["cloud_recursive"] else None
    return result


def validate_settings(plugin, values):
    values = dict(values)
    confirm_cloud_delete = values.pop("confirm_cloud_delete", False)
    if not isinstance(confirm_cloud_delete, bool):
        raise UserAccountError("网盘删源确认格式不正确")
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
            values["min_file_size"] = minimum_gb_setting(values.pop("min_file_size_gb"), previous=plugin._min_file_size)
        except ValueError as failure:
            raise UserAccountError(str(failure)) from None
    if set(values) - set(CONFIG_FIELDS) - {"telegram_token"}:
        raise UserAccountError("包含未知配置项，请刷新页面后重新保存")
    result = {name: getattr(plugin, f"_{name}") for name in CONFIG_FIELDS}
    for name, value in values.items():
        if name in BOOLEAN_FIELDS:
            if not isinstance(value, bool):
                raise UserAccountError("开关配置格式不正确")
        elif name in {"min_age", "cloud_read_timeout", "telegram_aggregate_window", "retry_limit", "local_upload_rate"}:
            lower, upper = (0, 128) if name == "local_upload_rate" else ((0, 86400) if name == "min_age" else ((30, 1800) if name == "cloud_read_timeout" else ((0, 10) if name == "retry_limit" else (0, 3600))))
            if isinstance(value, bool) or not str(value).isdigit() or not lower <= int(value) <= upper:
                label = {"min_age": "文件稳定时间", "cloud_read_timeout": "网盘读取超时", "retry_limit": "自动重试次数", "telegram_aggregate_window": "聚合转发等待时间", "local_upload_rate": "直传速度上限"}[name]
                raise UserAccountError(f"{label}必须为 {lower} 到 {upper} 的整数")
            value = int(value)
        else:
            if not isinstance(value, str) or len(value) > (32768 if name in {"folder", "cloud_folder", "local_upload_folder"} else 4096):
                raise UserAccountError("配置文本格式不正确或过长")
            value = value.strip()
        result[name] = value
    result["telegram_mode"] = "user"
    from .cloud_storage import monitor_roots
    try:
        result["folder"] = ",".join(monitor_roots(result["folder"]))
        result["cloud_folder"] = ",".join(monitor_roots(result["cloud_folder"], cloud=True))
        result["local_upload_folder"] = ",".join(monitor_roots(result["local_upload_folder"]))
        from .cloud_storage import cloud_path
        if result["local_upload_target"]:
            result["local_upload_target"] = cloud_path(result["local_upload_target"])
    except ValueError as failure:
        raise UserAccountError(str(failure)) from None
    try:
        minimum_file_bytes(result["min_file_size"])
    except ValueError as failure:
        raise UserAccountError(str(failure)) from None
    if result["cloud_life_source"] not in {"shared", "direct"}:
        raise UserAccountError("生活事件来源无效")
    result["local_upload_mode"] = result["local_upload_mode"] or "mp"
    if result["local_upload_mode"] not in {"mp", "direct"}:
        raise UserAccountError("请选择有效的本地计算上传方式")
    if result["local_upload_enabled"]:
        if not result["local_upload_folder"]:
            raise UserAccountError("请填写本地计算上传目录")
        if result["local_upload_mode"] == "direct" and not result["local_upload_target"]:
            raise UserAccountError("直传模式请填写115上传目标目录")
        from .cloud_storage import CloudStorage, CloudError
        try:
            storage = CloudStorage("115网盘Plus")
            if result["local_upload_mode"] == "direct" and not callable(getattr(storage.oper, "get_item_strict", None)):
                raise CloudError("请更新115插件以提供严格目标查询")
        except CloudError as failure:
            raise UserAccountError(str(failure)) from None
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
        from .cloud_storage import SUPPORTED_STORAGES, available_storages, CloudError
        try:
            if result["cloud_storage"] not in SUPPORTED_STORAGES:
                raise CloudError("请选择 115网盘Plus 或 CloudDrive储存")
            if not result["cloud_folder"]:
                raise CloudError("请至少指定一个网盘监控目录")
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
    result["cloud_delete_scope"] = ""
    result["local_upload_cloud_delete_scope"] = ""
    if not result["cloud_enabled"]:
        result["cloud_delete_source"] = False
    if result["cloud_storage"] != "115网盘Plus":
        result["cloud_delete_source"] = False
        if result["local_upload_delete_cloud_copy"]:
            raise UserAccountError("网盘副本删除仅支持115网盘")
    result["cloud_delete_enabled"] = bool(
        result["cloud_delete_source"] or result["local_upload_delete_cloud_copy"]
    )
    if result["cloud_delete_enabled"]:
        from .cloud_storage import CloudStorage, CloudError
        try:
            if not result["output_enabled"] and not result["telegram_enabled"]:
                raise CloudError("请至少开启TXT或Telegram一种输出后再启用网盘删源")
            if result["cloud_enabled"] and result["cloud_folder"]:
                result["cloud_delete_scope"] = CloudStorage(result["cloud_storage"]).delete_scope(result["cloud_folder"], result["cloud_recursive"])
            if result["local_upload_delete_cloud_copy"]:
                if not result["local_upload_enabled"] or result["local_upload_mode"] != "direct" or not result["local_upload_target"]:
                    raise CloudError("删除上传副本仅支持已配置目标目录的直接上传模式")
                result["local_upload_cloud_delete_scope"] = CloudStorage(result["cloud_storage"]).delete_scope(result["local_upload_target"], True)
            if result["cloud_delete_source"] and not result["cloud_delete_scope"]:
                raise CloudError("启用网盘监控删源前，请先启用网盘监控并填写目录")
            if not result["cloud_delete_scope"] and not result["local_upload_cloud_delete_scope"]:
                raise CloudError("请先配置网盘监控目录或本地上传目标目录")
        except CloudError as failure:
            raise UserAccountError(str(failure)) from None
        except Exception as failure:
            raise UserAccountError(f"115网盘删源范围校验失败（{type(failure).__name__}），未启用删除") from None
    return result


async def read_body(request, limit=16384):
    body = await request.body()
    if len(body) > limit:
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

    async def cancel_task(request: Request, principal=Depends(get_current_active_superuser)):
        values = await read_body(request)
        if set(values) != {"id"} or not isinstance(values.get("id"), str) or not re.fullmatch(r"[0-9a-f]{64}", values["id"]):
            raise HTTPException(status_code=400, detail="请提供有效的等待任务 ID")
        return response(await asyncio.to_thread(plugin.cancel_task, values["id"]))

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
        return response(plugin.queue_cloud_scan(retry_failed=True))

    async def local_run(principal=Depends(get_current_active_superuser)):
        if not plugin._enabled:
            return response({"ok": False, "message": "请先启用并保存插件设置"})
        if not plugin._folder:
            return response({"ok": False, "message": "请先配置并保存本地监控目录"})
        if plugin._worker is None or not plugin._worker.is_alive():
            return response({"ok": False, "message": "本地扫描工作线程未运行，请重新保存插件设置后重试"})
        await asyncio.to_thread(plugin.scan, True)
        return response({"ok": True, "message": "本地增量扫描已加入后台队列；已有链接不重复计算"})

    async def upload_run(principal=Depends(get_current_active_superuser)):
        if not plugin._enabled or not plugin._local_upload_enabled:
            return response({"ok": False, "message": "请先启用并保存本地计算上传设置"})
        if plugin._worker is None or not plugin._worker.is_alive():
            return response({"ok": False, "message": "工作线程未运行，请重新保存设置"})
        await asyncio.to_thread(plugin.scan, True, True)
        if plugin._local_upload_mode == "mp":
            return response({"ok": True, "message": "MP上传成功回执补扫已入队；未上传文件不计算，不会主动发起整理"})
        return response({"ok": True, "message": "本地计算上传增量补扫已入队；先核对115目标，未确认的上传允许再次尝试"})

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
            values = await read_body(request, limit=262144)
            config = validate_settings(plugin, values)
            plugin.update_config(config)
            def update_schedule():
                from app.scheduler import Scheduler
                Scheduler().update_plugin_job(plugin.__class__.__name__)
            reloaded_now = plugin.schedule_reload(config, callback=update_schedule)
            snapshot = settings_snapshot(plugin, config)
            if not reloaded_now:
                message = "插件设置已保存，当前配置应用完成后将继续应用本次修改"
            else:
                message = "插件设置已保存，正在应用监控配置"
            return response({"ok": True, "settings": snapshot, "message": message, "applying": True})
        except UserAccountError as error:
            return response({"ok": False, "message": str(error)})
        except Exception as error:
            logger.exception("自动生成ED2K链接：保存插件设置失败")
            return response({"ok": False, "message": f"保存插件设置失败（{type(error).__name__}），请查看插件日志"})

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

    return [{"path": path, "endpoint": endpoint, "methods": [method], "auth": "bear", "summary": summary}
            for path, endpoint, method, summary in (
                ("/records", recent, "GET", "最近生成链接与删源状态"),
                ("/tasks/cancel", cancel_task, "POST", "取消尚未开始的文件任务"),
                ("/records/delete-source", delete_source, "POST", "确认后手动删除记录对应的源文件"),
                ("/cloud/status", cloud_status, "GET", "网盘来源及生成进度"),
                ("/cloud/run", cloud_run, "POST", "网盘增量扫描与补发"),
                ("/cloud/browse", cloud_browse, "POST", "浏览网盘子目录"),
                ("/settings", status, "GET", "读取安全配置"), ("/settings", save, "POST", "保存插件配置"),
                ("/telegram/status", telegram_status, "GET", "个人账号登录状态"),
                ("/telegram/action", telegram_action, "POST", "个人账号网页授权"),
                ("/local-upload/run", upload_run, "POST", "本地计算上传115增量补扫与补发"),
                ("/local/run", local_run, "POST", "本地增量扫描与补发"))]
