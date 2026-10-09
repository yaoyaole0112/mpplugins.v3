"""Read public TVMao cast pages as a conservative Chinese-credits fallback.

This module makes no request to MediaEnhance. A search result is accepted only
when its work title matches uniquely; parsed actors are checked again against
the title of the actor page before they can be written to Emby.
"""

import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx


_WORK_PATH = re.compile(r"^/(?:kanju|drama)/([A-Za-z0-9_-]{3,40})(?:/actors)?/?$")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_CHINESE = re.compile(r"[\u3400-\u9fff]")
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36")
# User-provided actors page for the Emby/TMDB entry "征途" (336207). The
# search endpoint can omit the series despite its actors page being available.
# Never apply this direct link to another show sharing the same Chinese title.
_KNOWN_WORKS = {("征途", "336207"): "/kanju/YXAhXWhl"}


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs = dict(attrs)
            self.current = [attrs.get("href", ""), attrs.get("title", ""), ""]

    def handle_data(self, data):
        if self.current is not None:
            self.current[2] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current:
            self.links.append(self.current)
            self.current = None


def _work_title_matches(candidate, title, year=""):
    candidate = html.unescape(str(candidate or "")).strip()
    title = str(title or "").strip()
    if not candidate or not title:
        return False
    mentioned_years = _YEAR.findall(candidate)
    if year and mentioned_years and str(year)[:4] not in mentioned_years:
        return False
    candidate = _YEAR.sub("", candidate).strip(" \t_()（）-·")
    candidate = re.sub(r"^电视剧(?=《?" + re.escape(title) + r")", "", candidate)
    candidate = re.sub(r"^《(?=" + re.escape(title) + r")", "", candidate)
    if candidate.startswith(title + "》"):
        candidate = title + candidate[len(title) + 1:]
    if candidate == title:
        return True
    if not candidate.startswith(title):
        return False
    suffix = candidate[len(title):].lstrip(" \t_()（）-·：:｜|")
    return bool(re.match(r"^(?:电视剧|演员表|剧情介绍|分集剧情|人物介绍)(?:[\W_]|$)", suffix))


def _search_path(page, title, year=""):
    parser = _Links()
    parser.feed(page)
    matches = set()
    for path, label, text in parser.links:
        match = _WORK_PATH.fullmatch(path)
        if match and (_work_title_matches(label, title, year) if label.strip() else
                      _work_title_matches(text, title, year)):
            matches.add(path.rstrip("/").removesuffix("/actors"))
    return next(iter(matches)) if len(matches) == 1 else ""


def _clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


def parse_actors(page):
    """Parse the two TVMao actor layouts used by MediaEnhance."""
    actors = []
    seen = set()

    def add(name, role, image=""):
        name, role = _clean(name), _clean(role)
        if (not name or not role or name in seen or not _CHINESE.search(name)
                or not _CHINESE.search(role) or len(name) > 70 or len(role) > 100):
            return
        seen.add(name)
        actors.append({"name": name, "role": role,
                       "img": urljoin("https://www.tvmao.com/", image) if image else ""})

    # TVMao card alt text: 角色名（演员名饰演）.
    for image in re.finditer(r"<img\b[^>]*>", page, flags=re.I | re.S):
        tag = image.group(0)
        alt = re.search(r"\balt\s*=\s*(['\"])(.*?)\1", tag, flags=re.I | re.S)
        if not alt:
            continue
        match = re.fullmatch(r"\s*(.+?)\s*[（(]\s*(.+?)\s*饰演\s*[）)]\s*", html.unescape(alt.group(2)))
        if match:
            src = re.search(r"\b(?:data-original|data-src|src)\s*=\s*(['\"])(.*?)\1", tag, flags=re.I | re.S)
            add(match.group(2), match.group(1), src.group(2) if src else "")

    # Older /drama and /kanju pages: <li><... class="c-name">role
    # <a class="black_link">actor</a></li>.
    for block in re.findall(r"<li\b[^>]*>(.*?)</li>", page, flags=re.I | re.S):
        role = re.search(r'class\s*=\s*["\'][^"\']*\bc-name\b[^"\']*["\'][^>]*>(.*?)</', block, re.I | re.S)
        actor = re.search(r'class\s*=\s*["\'][^"\']*\bblack_link\b[^"\']*["\'][^>]*>(.*?)</a>', block, re.I | re.S)
        if role and actor:
            add(actor.group(1), role.group(1))
    return actors


async def fetch_tvmao_cast(title, year="", tmdb_id="", status=None):
    """Return verified Chinese actors, logging the stage when no match exists."""
    if not title or len(str(title)) > 80:
        return []
    direct = _KNOWN_WORKS.get((str(title).strip(), str(tmdb_id or "")))
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT,
                                          "Referer": "https://www.tvmao.com/"},
                                 timeout=httpx.Timeout(12, connect=6),
                                 follow_redirects=True, trust_env=False) as client:
        path = ""
        try:
            search = await client.get("https://www.tvmao.com/query.jsp", params={"keys": title})
            search.raise_for_status()
            path = _search_path(search.text, title, year)
        except httpx.HTTPError:
            if not direct:
                raise
            if status:
                status("电视猫站内搜索不可用，尝试用户提供的演员页")
        if not path and direct:
            path = direct
            if status:
                status("电视猫站内搜索未匹配，尝试用户提供的《征途》演员页")
        if not path:
            if status:
                status("电视猫站内搜索未唯一匹配剧名，未读取其他剧集演员")
            return []
        page = await client.get("https://www.tvmao.com" + path + "/actors")
        page.raise_for_status()
        # Never apply the cast of an unrelated TV show even if search changed.
        headings = re.findall(r"<title\b[^>]*>(.*?)</title>|<h1\b[^>]*>(.*?)</h1>",
                              page.text, re.I | re.S)
        if not any(_work_title_matches(_clean(value), title, year)
                   for group in headings for value in group if value):
            if status:
                status("电视猫演员页标题与剧名不符，已拒绝写入")
            return []
        actors = parse_actors(page.text)
        if not actors and status:
            status("电视猫演员页可访问，但未解析到中文演员及角色")
        return actors
