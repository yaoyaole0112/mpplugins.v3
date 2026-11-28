"""MoviePilot search adapter and evidence checks for episode air dates."""

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import json
import re
from datetime import date
from datetime import timedelta
from unicodedata import normalize
from urllib.parse import urlsplit


# Only editorial/platform hosts, not general social or user publishing sites.
OFFICIAL_SCHEDULE_HOSTS = {
    "v.qq.com", "wetv.vip", "iqiyi.com", "www.iqiyi.com", "m.iqiyi.com",
    "mgtv.com", "www.mgtv.com", "w.mgtv.com", "youku.com", "www.youku.com",
    "v.youku.com", "tv.cctv.com", "www.cctv.com", "www.yangshipin.cn",
    "www.netflix.com", "www.disneyplus.com", "www.crunchyroll.com",
    "tvn.cjenm.com",
}


def run_on_moviepilot_loop(coroutine):
    """Run host tools on MoviePilot's loop so their loop-bound resources stay valid."""
    try:
        from app.runtime.loop import main_loop_registry

        loop = main_loop_registry.current
    except (ImportError, AttributeError):
        loop = None

    if loop is None:
        return asyncio.run(coroutine)
    if loop.is_closed() or not loop.is_running():
        coroutine.close()
        raise RuntimeError("MoviePilot 主事件循环不可用")
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if current_loop is loop:
        coroutine.close()
        raise RuntimeError("不能在 MoviePilot 主事件循环线程内同步等待 AI 核验")

    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    try:
        return future.result(timeout=180)
    except FutureTimeoutError:
        future.cancel()
        raise


def _source_key(url):
    try:
        parsed = urlsplit(str(url or "").strip())
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username:
            return None
        path = parsed.path.rstrip("/")
        if not path and not parsed.query:
            return None
        return parsed.hostname.lower(), path, parsed.query
    except ValueError:
        return None


def _evidence_text(text):
    return " ".join(normalize("NFKC", str(text or "")).casefold().split())


def supports_official_date(item, rows, value, episode, season, title_matches):
    """Accept only an actual official search excerpt tying one episode to one date."""
    source = _source_key(item.get("source"))
    quote = _evidence_text(item.get("quote"))
    if not source or source[0] not in OFFICIAL_SCHEDULE_HOSTS or not (8 <= len(quote) <= 300):
        return False
    if not re.search(
        r"播出|上线|更新|开播|방송|방영|공개|\b(?:airs?|aired|air date|premieres?|streams?|releases?)\b",
        quote,
    ):
        return False
    dated = list(re.finditer(
        r"(?<!\d)(?:(\d{4})\s*[-/.年년]\s*)?(\d{1,2})\s*[-/.月월]\s*"
        r"(\d{1,2})(?:\s*[日일])?(?!\d)",
        quote,
    ))
    if len(dated) != 1:
        return False
    year, month, day = dated[0].groups()
    # A yearless excerpt is ambiguous for back catalogues and annual shows.
    if not year or (int(year), int(month), int(day)) != (value.year, value.month, value.day):
        return False
    episode_tags = re.findall(
        r"(?:s\d+\s*e(\d+)(?!\d)|第\s*(\d+)\s*[集话]|episode\s+(\d+)(?!\d)|"
        r"제?\s*(\d+)\s*회)", quote
    )
    if [int(next(tag for tag in tags if tag)) for tags in episode_tags] != [episode]:
        return False
    for row in rows:
        if _source_key(row.get("url")) != source or quote not in _evidence_text(row.get("snippet")):
            continue
        context = _evidence_text(f"{row.get('title', '')} {row.get('snippet', '')}")
        explicit_seasons = {
            int(next(tag for tag in tags if tag)) for tags in re.findall(
                r"(?:\bs(\d+)\s*e\d+|\bseason\s*(\d+)|第\s*(\d+)\s*季)", context
            )
        }
        if explicit_seasons and explicit_seasons != {season}:
            continue
        if not explicit_seasons and season != 1 and source[0] != "tvn.cjenm.com":
            continue
        # Korean official episode pages commonly use the broadcaster's Korean
        # program title, which may not be present in MoviePilot's title aliases.
        # The candidate itself is checked against the recognized identity before
        # this function is called; here, verify its quoted episode/date in-page.
        if title_matches(row.get("title", "")) or (
            source[0] == "tvn.cjenm.com"
            and str(row.get("title", "")).strip()
            and episode_tags
        ):
            return True
    return False


def infer_weekly_episode_dates(dates, episodes, today, window_days=30):
    """Project only a demonstrable weekly run from dates in the recent month."""
    known = []
    for number, value in dates.items():
        aired = release_date(value)
        if aired and today - timedelta(days=window_days) <= aired <= today:
            known.append((int(number), aired))
    known.sort()
    if len(known) < 2:
        return {}

    # Use the consecutive weekly streak ending at the latest dated episode.
    streak = [known[-1]]
    for previous, current in zip(reversed(known[:-1]), reversed(known[1:])):
        episode_gap = current[0] - previous[0]
        day_gap = (current[1] - previous[1]).days
        if episode_gap != 1 or not 6 <= day_gap <= 8:
            break
        streak.append(previous)
    if len(streak) < 2:
        return {}

    latest_number, latest_date = known[-1]
    return {
        int(number): (latest_date + timedelta(days=7 * (int(number) - latest_number))).isoformat()
        for number in episodes
        if int(number) > latest_number
    }


async def search_episode_evidence(prompt):
    """Run a few bounded query variants through MoviePilot's configured web search."""
    from app.agent.tools.impl.search_web import SearchWebTool
    from app.runtime.log import logger

    tool = SearchWebTool(session_id="resourceflow-air-dates", user_id="resourceflow")
    title = str(prompt.get("title") or "").strip()
    aliases = [
        str(value).strip() for value in prompt.get("aliases", [])
        if str(value).strip() and str(value).strip().casefold() != title.casefold()
    ]
    if len(aliases) > 1:
        aliases = aliases[:5]
    episodes = [int(number) for number in prompt.get("episodes", [])[:4]]
    if not title or not episodes:
        return []

    tags = " ".join(f"第{number}集" for number in episodes)
    periods = " ".join(f"第{number}期" for number in episodes)
    season_tags = " ".join(f"S{int(prompt['season']):02d}E{number:02d}" for number in episodes)
    year = str(prompt.get("year") or "").strip()
    year_part = f" {year}" if year else ""
    # Search both localized wording and native season/episode markers. A
    # media database year can describe the series premiere rather than the
    # season currently being released.
    queries = [
        f"{title} {season_tags} air date release date{year_part}",
        f"{title} {season_tags} episode {episodes[0]} aired{year_part}",
        f"{title} {tags} 播出日期{year_part}",
        f"{title} {periods} 更新 播出时间{year_part}",
    ]
    for alias in aliases[:2]:
        queries.append(f"{alias} {season_tags} air date{year_part}")
        queries.append(f"{alias} {tags} 播出日期{year_part}")

    async def search(query):
        try:
            raw = await asyncio.wait_for(tool.run(query=query, max_results=8), timeout=20)
        except Exception as err:
            logger.info(f"最近播出日期搜索失败，查询“{query}”：{type(err).__name__}")
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            status = str(raw or "").replace("\n", " ")[:120]
            logger.info(f"最近播出日期搜索无结构化结果，查询“{query}”：{status or '空响应'}")
            return []
        results = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(results, list):
            logger.info(f"最近播出日期搜索返回格式异常，查询“{query}”：{type(payload).__name__}")
            return []
        rows = [{
            key: str(row.get(key) or "")[:limit]
            for key, limit in (("url", 800), ("title", 250), ("snippet", 850))
        } for row in results[:8] if isinstance(row, dict) and _source_key(row.get("url"))]
        logger.info(f"最近播出日期搜索查询“{query}”：返回 {len(rows)} 条")
        return rows

    batches = await asyncio.gather(*(search(query) for query in queries))
    merged, seen = [], set()
    for rows in batches:
        for row in rows:
            key = (_source_key(row.get("url")), _evidence_text(row.get("title")), _evidence_text(row.get("snippet")))
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
    return merged[:20]


def cited_source_keys(response):
    """Read provider-issued citations, not URLs from the model's JSON."""
    keys = set()

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") in {"url_citation", "web_search_result_location", "citation"}:
                if key := _source_key(node.get("url")):
                    keys.add(key)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for field in ("content", "additional_kwargs", "response_metadata"):
        walk(getattr(response, field, None))
    return keys


def has_retrieved_source(source, rows, citations=()):
    key = _source_key(source)
    return bool(key and (key in citations or any(_source_key(row.get("url")) == key for row in rows)))


def release_date(value):
    try:
        result = date.fromisoformat(value)
        return result if result.isoformat() == value else None
    except (ValueError, TypeError):
        return None
