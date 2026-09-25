"""Scheduled-tool notification conditions and copy matching MediaEnhance."""

import os

from .invalid_data import QUARANTINE


DIVIDER = "━━━━━━━━━━━━━━━"


def format_bytes(value):
    size = float(value or 0)
    if size <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def invalid_cleanup(result, root):
    count = len(result.get("deleted") or [])
    skipped = len(result.get("failed") or [])
    return "📃 清理无效数据", "\n".join([
        DIVIDER,
        f"✅ 已清理 {count} 项无效媒体数据，跳过 {skipped} 项。",
        f"📁 隔离目录：{os.path.join(root, QUARANTINE)}",
    ])


def invalid_confirmation(snapshot):
    items = snapshot.get("items") or []
    root = snapshot["root"]
    preview = "\n".join("• " + os.path.basename(item["path"]) for item in items[:8])
    if len(items) > 8:
        preview += f"\n…等共 {len(items)} 项"
    return "⚠️【清理无效数据】待确认", (
        f"扫描目录：{root}\n待清理：{len(items)} 项\n"
        "确认有效期：发送后 30 分钟；服务重启后失效，过期请重新扫描。\n"
        "这些项目将在二次核验后移入隔离区，不会立即永久删除。\n\n"
        f"{preview}\n\n请在MediaEnhance工具的待办中确认或取消。"
    )


def _count_text(files, directories, size):
    parts = []
    if files:
        parts.append(f"{files} 个文件")
    if directories:
        parts.append(f"{directories} 个文件夹")
    return f"{' + '.join(parts) if parts else '0 项'}（{format_bytes(size)}）"


def file_cleanup(result, preview):
    folders = result.get("folders") or []
    failures = [item for item in (preview.get("folders") or []) if item.get("error")]
    failures.extend(item for item in folders if item.get("error"))
    deleted = int(result.get("deleted") or 0)
    directories = int(result.get("dir_count") or 0)
    if not (deleted or directories or failures):
        return None
    size = sum(int(item.get("size") or 0) for item in folders)
    lines = [DIVIDER, f"✅已删除：{_count_text(deleted, directories, size)}"]
    details = []
    for item in preview.get("folders") or []:
        if item.get("error"):
            details.append(f"📁{item['name']}：{item['error']}")
    for item in folders:
        if item.get("error"):
            details.append(f"📁{item['name']}：{item['error']}")
        elif item.get("files") or item.get("dirs"):
            details.append(f"📁{item['name']}：{_count_text(item.get('files', 0), item.get('dirs', 0), item.get('size', 0))}")
    if details:
        lines.extend(["", *details[:10]])
        if len(details) > 10:
            lines.append(f"…等共 {len(details)} 个目录")
    return "🗑 清理文件", "\n".join(lines).rstrip()


def file_cleanup_confirmation(preview):
    folders = preview.get("folders") or []
    lines = [
        f"待清理：{_count_text(preview.get('file_count', 0), preview.get('dir_count', 0), preview.get('total_bytes', 0))}",
        "确认后会把这些目录中的文件和文件夹移入 115 回收站。",
        "确认有效期：30 分钟；服务重启后失效。", "",
    ]
    for folder in folders[:8]:
        name = folder.get("name") or folder.get("cid")
        lines.append(f"📁{name}：{folder['error'] if folder.get('error') else _count_text(folder.get('files', 0), folder.get('dirs', 0), folder.get('size', 0))}")
    if len(folders) > 8:
        lines.append(f"…等共 {len(folders)} 个目录")
    lines.extend(["", "请在MediaEnhance工具页面确认或取消。"])
    return "⚠️【清理文件】待确认", "\n".join(lines)


def empty_trash(info):
    if not info.get("count"):
        return None
    return "🧹 清空115 回收站", "\n".join([
        DIVIDER,
        f"📁 已清空 {info['count']} 个文件 · 💾 已释放 {format_bytes(info.get('size_bytes'))}",
        "⚠️ 彻底删除不可恢复！",
    ])


def file_move(result):
    if not result.get("moved") and not result.get("errors"):
        return None
    lines = [DIVIDER]
    for detail in result.get("details") or []:
        if detail.get("status") != "success":
            continue
        lines.extend([
            f"🎯[源] {detail.get('src_name') or detail.get('src_id')} → [目标] {detail.get('dst_name') or detail.get('dst_id')}",
            f"📚数量：{detail.get('file_count', 0)} 个 · 💾大小：{format_bytes(detail.get('total_bytes', 0))}",
            "",
        ])
    if result.get("errors"):
        lines.append(f"⚠️ 转存失败：{len(result['errors'])} 个目录，请查看日志")
    return "📁 文件转存", "\n".join(lines).rstrip()
