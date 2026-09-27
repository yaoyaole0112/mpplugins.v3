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
_USER_AGENT = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
               "AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1")


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


async def fetch_tvmao_cast(title, year=""):
    """Return Chinese actors, or [] when search/page validation/network fails."""
    if not title or len(str(title)) > 80:
        return []
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT},
                                 timeout=httpx.Timeout(12, connect=6),
                                 follow_redirects=True, trust_env=False) as client:
        search = await client.get("https://www.tvmao.com/query.jsp", params={"keys": title})
        search.raise_for_status()
        path = _search_path(search.text, title, year)
        if not path:
            return []
        page = await client.get("https://www.tvmao.com" + path + "/actors")
        page.raise_for_status()
        # Never apply the cast of an unrelated TV show even if search changed.
        heading = re.search(r"<title\b[^>]*>(.*?)</title>", page.text, re.I | re.S)
        if not heading or not _work_title_matches(_clean(heading.group(1)), title, year):
            return []
        return parse_actors(page.text)
