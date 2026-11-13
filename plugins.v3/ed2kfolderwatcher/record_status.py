def delivery_status(record, channel, context, store, legacy_delivered):
    if not context.get("enabled"):
        return {"state": "inactive", "text": "未启用"}
    if context.get("error"):
        return {"state": "failed", "text": "目标设置无效"}
    if store is None or store.dirty:
        return {"state": "pending", "text": "投递凭据待确认"}
    entry = store.links.get(store.digest(record), {}).get(channel, {})
    deliveries = dict(record.get(f"{channel}_deliveries") or {})
    deliveries.update(entry.get("deliveries", {}))
    completed = dict(entry.get("targets", {}))
    if record.get(f"{channel}_sent") is True and record.get(f"{channel}_target"):
        completed[record[f"{channel}_target"]] = True
    targets = context["targets"]
    sent = sum(
        deliveries.get(key) is True or context["target_key"] in completed
        or legacy_delivered(destination, thread_id, completed, len(targets), channel)
        for (destination, thread_id), key in targets
    )
    total = len(targets)
    if sent == total and total:
        return {"state": "success", "text": "已投递" if total == 1 else f"已投递 {sent}/{total}"}
    return {"state": "pending", "text": f"部分投递 {sent}/{total}" if sent else "待投递"}


def workflow_summary(record, problem, telegram, ordinary, ordinary_enabled, failure=None):
    uploaded = bool(record.get("upload_mode") or record.get("upload_state") or record.get("upload_kind"))
    verified = record.get("upload_verified") is True
    steps = [{"key": "compute", "label": "计算", "text": "链接无效" if problem else "已完成",
              "state": "failed" if problem else "success"}]
    if uploaded:
        verify_failed = record.get("upload_state") == "failed" and not verified
        steps.append({"key": "verify", "label": "网盘校验",
                      "text": "需重新校验" if problem else "已通过" if verified else "失败" if verify_failed else "待完成",
                      "state": "failed" if problem or verify_failed else "success" if verified else "pending"})
    steps.append({"key": "telegram", "label": "常规TG", **telegram})
    if uploaded:
        if not ordinary_enabled:
            orphan = {"state": "inactive", "text": "未启用"}
        elif record.get("upload_kind") == "instant":
            orphan = {"state": "inactive", "text": "秒传跳过"}
        elif record.get("upload_kind") != "ordinary":
            orphan = {"state": "pending", "text": "上传方式未知，未执行"}
        else:
            orphan = ordinary
        steps.append({"key": "ordinary", "label": "孤本", **orphan})
    state, label, detail = "pending", "处理中", ""
    if problem:
        state, label = "failed", "链接无效，需重新计算"
    elif uploaded and not verified:
        if record.get("upload_state") == "failed":
            state, label, detail = "failed", "上传或网盘校验失败", record.get("upload_note", "")
        else:
            label = {
                "waiting_mp": "等待MP上传成功回执",
                "upload_queued": "等待上传网盘",
                "queued": "等待上传或校验",
                "uploading": "正在上传网盘",
                "verifying": "等待网盘校验",
            }.get(record.get("upload_state"), "等待上传或网盘校验")
    elif any(step["state"] == "failed" for step in steps):
        state, label = "failed", "转发设置需要修正"
    elif any(step["state"] == "pending" for step in steps):
        label = "等待转发完成"
        if uploaded and ordinary_enabled and record.get("upload_kind") not in {"instant", "ordinary"}:
            label = "待确认上传方式，孤本未执行"
        if record.get("upload_state") == "failed" and (
            telegram["state"] == "pending"
            or (ordinary_enabled and record.get("upload_kind") == "ordinary" and ordinary["state"] == "pending")
        ):
            state, label, detail = "failed", "转发尚未完成，请检查失败原因", record.get("upload_note", "")
    else:
        state = "success"
        label = "已完成" if any(step["key"] in {"telegram", "ordinary"} and step["state"] == "success"
                               for step in steps) else "计算与校验已完成" if uploaded else "计算已完成"
    if state != "success" and not problem and failure and failure.get("exhausted"):
        state, label, detail = "failed", "未完成，已停止自动重试", failure.get("error", "")
    return {"state": state, "label": label, "steps": steps, "detail": detail}
