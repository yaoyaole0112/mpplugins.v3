"""MoviePilot search adapter and evidence checks for episode air dates."""

import asyncio
import json
import re
from datetime import date
from unicodedata import normalize
from urllib.parse import urlsplit


# Only editorial/platform hosts, not general social or user publishing sites.
OFFICIAL_SCHEDULE_HOSTS = {
    "v.qq.com", "wetv.vip", "iqiyi.com", "www.iqiyi.com", "m.iqiyi.com",
    "mgtv.com", "www.mgtv.com", "w.mgtv.com", "youku.com", "www.youku.com",
    "v.youku.com", "tv.cctv.com", "www.cctv.com", "www.yangshipin.cn",
    "www.netflix.com", "www.disneyplus.com", "www.crunchyroll.com",
}


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
    if not re.search(r"播出|上线|更新|开播|\b(?:airs?|aired|air date|premieres?|streams?|releases?)\b", quote):
        return False
    dated = list(re.finditer(
        r"(?<!\d)(?:(\d{4})\s*[-/.年]\s*)?(\d{1,2})\s*[-/.月]\s*(\d{1,2})(?:\s*日)?(?!\d)",
        quote,
    ))
    if len(dated) != 1:
        return False
    year, month, day = dated[0].groups()
    # A yearless excerpt is ambiguous for back catalogues and annual shows.
    if not year or (int(year), int(month), int(day)) != (value.year, value.month, value.day):
        return False
    episode_tags = re.findall(
        r"(?:s\d+\s*e(\d+)(?!\d)|第\s*(\d+)\s*[集话]|episode\s+(\d+)(?!\d))", quote
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
        if not explicit_seasons and season != 1:
            continue
        if title_matches(row.get("title", "")):
            return True
    return False


async def search_episode_evidence(prompt):
    """Run the same bounded, proxy-aware local search tool as the MoviePilot agent."""
    from app.agent.tools.impl.search_web import SearchWebTool

    tool = SearchWebTool(session_id="resourceflow-air-dates", user_id="resourceflow")
    episodes = " ".join(f"第{number}集" for number in prompt["episodes"][:4])
    query = f'{prompt["title"]} 第{prompt["season"]}季 官方 追剧日历 播出时间'
    raw = await asyncio.wait_for(tool.run(
        query=f"{query} {episodes}", max_results=10,
    ), timeout=25)
    try:
        results = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(results, list):
        return []
    return [{
        key: str(row.get(key) or "")[:limit]
        for key, limit in (("url", 800), ("title", 250), ("snippet", 850))
    } for row in results[:10] if isinstance(row, dict) and _source_key(row.get("url"))]


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
