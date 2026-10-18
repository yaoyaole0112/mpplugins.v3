import re
from urllib.parse import urlparse


def telegram_destination(value: str) -> str:
    value = value.strip()
    if value.startswith(("https://", "http://", "t.me/")):
        parsed = urlparse(value if "://" in value else f"https://{value}")
        username = parsed.path.strip("/")
        if parsed.netloc.lower() not in {"t.me", "telegram.me", "www.t.me"} or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", username):
            raise ValueError("请填写目标 @用户名 或数字 Chat ID，不要填写消息链接、私有邀请链接")
        value = f"@{username}"
    if re.fullmatch(r"-?\d+", value):
        return str(int(value))
    if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]*", value):
        return f"@{value.lstrip('@')}"
    raise ValueError("接收目标格式错误：请填写数字 Chat ID 或 @用户名")


def telegram_targets(value: str, default_thread: str = "") -> list[tuple[str, int | None]]:
    default_thread = default_thread.strip()
    if default_thread and (not default_thread.isdigit() or int(default_thread) <= 0):
        raise ValueError("话题线程 ID 必须为正整数或留空")
    targets = []
    seen = set()
    for entry in re.split(r"[\r\n,，;；]+", value):
        if not entry.strip():
            continue
        parts = entry.strip().split("|")
        if len(parts) > 2:
            raise ValueError("接收目标请使用 @用户名|话题ID，每行一个")
        destination = telegram_destination(parts[0])
        thread = parts[1].strip() if len(parts) == 2 else default_thread
        if thread and (not thread.isdigit() or int(thread) <= 0):
            raise ValueError("话题线程 ID 必须为正整数或留空")
        target = (destination, int(thread) if thread else None)
        key = (destination.lower(), target[1])
        if key not in seen:
            seen.add(key)
            targets.append(target)
    if not targets:
        raise ValueError("请填写至少一个 Telegram 接收目标")
    if len(targets) > 50:
        raise ValueError("Telegram 接收目标最多支持 50 个")
    return targets
