import re

from .episode_plots import missing_episode_title
from .overview_quality import overview_issues, overview_length


def positive_number(value):
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def foreign_text(value):
    """Detect untranslated labels, including mixed Chinese and foreign scripts."""
    text = str(value or "").strip()
    if re.search(r"[\u3040-\u30ff\u1100-\u11ff\u3130-\u318f\uac00-\ud7ff\u0e00-\u0e7f]", text):
        return True
    latin = re.findall(r"[A-Za-z]+", text)
    if not latin:
        return False
    if not re.search(r"[\u3400-\u9fff]", text):
        return True
    return any(token.upper() not in {"TV", "HBO", "CCTV", "BBC", "IMAX", "AI"} for token in latin)


def media_checks(item, options, *, episode=False, duplicated=False):
    checks = []

    def add(field, status, detail):
        checks.append({"field": field, "status": status, "detail": detail})

    title = str(item.get("Name") or "").strip()
    title_field = "分集标题" if episode else "标题"
    if not title or (episode and missing_episode_title(title)):
        add(title_field, "缺失", "未填写或仅有集号占位标题")
    elif re.search(r"[A-Za-z]", title) and not re.search(r"[\u3400-\u9fff]", title):
        add(title_field, "疑似异常", "标题尚未汉化；无可靠译名时应保留原文")
    else:
        add(title_field, "正常", title[:120])
    overview = item.get("Overview")
    issues = overview_issues(overview, options["overview_episode_min" if episode else "overview_movie_min"], duplicated)
    add("分集简介" if episode else "简介/概要", "缺失" if "缺失" in issues else "疑似异常" if issues else "正常",
        f"{overview_length(overview)} 字" + ("；" + "、".join(issues) if issues else ""))
    if not episode:
        genres = item.get("Genres") or []
        genre_values = [str(value).strip() for value in genres if str(value).strip()]
        foreign_genres = sum(foreign_text(value) for value in genre_values)
        add("类型", "缺失" if not genre_values else "疑似异常" if foreign_genres else "正常",
            ("、".join(genre_values) or "未填写媒体类型") +
            (f"；含外文 {foreign_genres} 项" if foreign_genres else ""))
        studios = [str(entry.get("Name") or "") if isinstance(entry, dict) else str(entry)
                   for entry in item.get("Studios") or []]
        studios = [name for name in studios if name.strip()]
        foreign_studios = sum(foreign_text(name) for name in studios)
        add("工作室/播出平台", "缺失" if not studios else "疑似异常" if foreign_studios else "正常",
            ("、".join(studios) or "未填写制作方或播出平台") +
            (f"；含外文 {foreign_studios} 项" if foreign_studios else ""))
    people = [person for person in item.get("People") or [] if isinstance(person, dict) and person.get("Name")]
    field = "分集演职员" if episode else "演职员"
    if "People" not in item:
        add(field, "待核实", "接口未返回演职资料；服务器可能隐藏人物，不据此判定为空")
    elif not people:
        add(field, "疑似异常", "未返回有效演职员；可能缺失或被服务器隐藏" +
            ("；分集演职写入未开启" if episode and not options["episode_cast"] else ""))
    else:
        foreign = sum(foreign_text(person.get("Name")) or foreign_text(person.get("Role")) for person in people)
        roles = sum(person.get("Type") == "Actor" and (not str(person.get("Role") or "").strip() or
                    (options["role_prefix"] and not str(person.get("Role") or "").startswith(("饰", "配")))) for person in people)
        add(field, "疑似异常" if foreign or roles else "正常",
            f"可见 {len(people)} 人，含外文 {foreign} 人，角色缺失/前缀待补 {roles} 人；不代表完整演员表")
    if episode or item.get("Type") == "Movie":
        sources = [source for source in item.get("MediaSources") or [] if isinstance(source, dict)]
        source = next((source for source in sources if source.get("Type") in (None, "Default")), sources[0] if sources else {})
        size = item.get("Size") if positive_number(item.get("Size")) else source.get("Size")
        runtime = item.get("RunTimeTicks") if positive_number(item.get("RunTimeTicks")) else source.get("RunTimeTicks")
        if not positive_number(runtime):
            streams = source.get("MediaStreams") or item.get("MediaStreams") or []
            runtime = next((stream.get("RunTimeTicks") for stream in streams if isinstance(stream, dict)
                            and positive_number(stream.get("RunTimeTicks"))), None)
        missing = [field for field, value in (("大小", size), ("时长", runtime)) if not positive_number(value)]
        add("媒体信息", "缺失" if missing else "正常", "缺少" + "、".join(missing) if missing else "已有文件大小与时长")
        streams = [stream for stream in source.get("MediaStreams") or item.get("MediaStreams") or [] if isinstance(stream, dict)]
        videos = [stream for stream in streams if stream.get("Type") == "Video"]
        if not videos:
            add("音视频流", "待核实", "未返回视频流，编码与分辨率尚无法核实")
        else:
            video = videos[0]
            missing = (["视频编码"] if not video.get("Codec") else [])
            if not positive_number(video.get("Width")) or not positive_number(video.get("Height")):
                missing.append("分辨率")
            audio = [stream for stream in streams if stream.get("Type") == "Audio"]
            if any(not stream.get("Codec") for stream in audio):
                missing.append("音频编码")
            add("音视频流", "缺失" if missing else "正常", "缺少" + "、".join(missing) if missing else
                "已有视频编码与分辨率" + ("及音频编码" if audio else "；未返回音频流，不据此判断静音视频异常"))
    else:
        add("媒体信息", "不适用", "整剧无独立媒体文件，大小与时长逐分集检查")
    return checks
