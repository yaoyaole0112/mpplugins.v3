import asyncio
import re
from urllib.parse import quote, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .tvmao_client import _search_path, _USER_AGENT, _work_title_matches


_EPISODE = re.compile(r'^第\s*(\d+)\s*集\s*(?:[：:]\s*(.*))?$')
_TVMAO_WORK = re.compile(r'^/(?:kanju|drama)/[A-Za-z0-9_-]{3,40}$')
_KNOWN = {('云雀叫天录', '2026', 1): ('/kanju/XmJjaW8k', '/item/云雀叫天录/63227441')}


def missing_episode_title(value):
    text = str(value or '').strip()
    return (not text or bool(re.fullmatch(
        r'(?:第\s*[\d一二三四五六七八九十百零〇两]+\s*[集期]|(?:episode|ep|e)\s*\d+|S\d+E\d+|\d+|未命名|暂无标题)',
        text, re.I)))


def missing_episode_overview(value):
    return str(value or '').strip() in {'', '暂无简介', '暂无剧情', '暂无剧情简介', '暂无分集剧情'}


def first_sentence(value):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if missing_episode_overview(text):
        return ''
    position = text.find('。')
    return text[:position + 1] if position >= 0 else text


def _document(page):
    soup = BeautifulSoup(page, 'html.parser')
    for element in soup.select('script,style,nav,aside,sup,.e_a_t,.epipd,.copyright'):
        element.decompose()
    return soup


def _verified(soup, title, year):
    info = soup.select_one('.obj-summary-info .obj-info')
    years = re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', info.get_text(' ', strip=True)) if info else []
    if year and years and str(year)[:4] not in years:
        return False
    headings = [node.get_text(' ', strip=True) for node in soup.select('h1,title')]
    return any(_work_title_matches(re.sub(r'第\s*\d+\s*集(?=分集剧情)', '', text)
                                   .replace('分集剧情介绍', '分集剧情'), title, year) for text in headings)


def parse_tvmao_episode(page, title, number, year=''):
    soup = _document(page)
    if not _verified(soup, title, year):
        return {}
    heading = soup.select_one('.epi_t, .d_e_t')
    content = soup.select_one('article.epi_c, .epi_c, .epicol .episode')
    if not heading or not content:
        return {}
    text = heading.get_text(' ', strip=True)
    match = _EPISODE.fullmatch(text)
    if not match or int(match[1]) != number:
        return {}
    overview = first_sentence(content.get_text('', strip=True))
    return {'name': text if match[2] else '', 'overview': overview}


def parse_baike_episodes(page, title, year=''):
    soup = _document(page)
    heading = soup.find('h1')
    if not heading or heading.get_text('', strip=True) != title:
        return {}
    if year:
        for label in soup.find_all('dt'):
            if re.sub(r'\s+', '', label.get_text()) in {'首播时间', '首播日期', '上映时间', '发行时间'}:
                value = label.find_next_sibling('dd')
                years = re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', value.get_text()) if value else []
                if years and str(year)[:4] not in years:
                    return {}
    title_node = soup.find('title')
    if title_node and not _work_title_matches(title_node.get_text(' ', strip=True).replace('_百度百科', ''), title, year):
        return {}
    text = soup.get_text('\n', strip=True)
    section = text.find('分集剧情')
    if section < 0:
        return {}
    result = {}
    current = None
    paragraphs = []
    subtitle = ''

    def save():
        if current is not None and paragraphs:
            result[current] = {'name': subtitle, 'overview': first_sentence(''.join(paragraphs))}

    for line in text[section:].splitlines():
        line = line.strip()
        match = _EPISODE.fullmatch(line)
        if match:
            save()
            current = int(match[1])
            subtitle = line if match[2] else ''
            paragraphs = []
        elif current is not None:
            if line in {'演职员表', '演员表', '职员表', '角色介绍', '音乐原声', '幕后制作', '参考资料'}:
                break
            if line and not re.fullmatch(r'\[?\d+\]?', line):
                paragraphs.append(line)
    save()
    return result


def _episode_links(page, path):
    soup = BeautifulSoup(page, 'html.parser')
    result = {}
    identifier = path.rsplit('/', 1)[1]
    work_path = r'/(?:kanju|drama)/' + re.escape(identifier)
    for link in soup.find_all('a', href=True):
        parsed = urlsplit(urljoin('https://www.tvmao.com' + path + '/episode', link['href']))
        match = re.fullmatch(work_path + r'/episode/(\d+)-(\d+)/?', parsed.path)
        if parsed.hostname not in {'www.tvmao.com', 'm.tvmao.com'}:
            continue
        if match:
            result[int(match[2])] = parsed.path
        elif re.fullmatch(work_path + r'/episode/?', parsed.path):
            result[1] = parsed.path
    return result


def _season_titles(title, season):
    if season == 1:
        return [title]
    digits = '零一二三四五六七八九'
    if not 1 < season < 100:
        return []
    tens, ones = divmod(season, 10)
    chinese = ((digits[tens] if tens > 1 else '') + '十' if tens else '') + (digits[ones] if ones else '')
    return [f'{title}第{chinese}季', f'{title}第{season}季']


async def fetch_episode_plots(title, year, season, numbers, status=None, active=None):
    numbers = sorted({number for number in numbers if type(number) is int and 1 <= number <= 9999})[:300]
    if not title or len(title) > 80 or not numbers or season < 1:
        return {}
    queries = _season_titles(title, season)
    if not queries:
        return {}
    direct_tvmao, direct_baike = _KNOWN.get((title, str(year), season), ('', ''))
    result = {}

    def report(message):
        if status:
            status(message)

    async with httpx.AsyncClient(headers={'User-Agent': _USER_AGENT},
                                 timeout=httpx.Timeout(12, connect=6),
                                 follow_redirects=True, trust_env=False) as client:
        async def read(url):
            if active:
                active()
            try:
                response = await client.get(url)
            except httpx.TransportError:
                if urlsplit(url).hostname != 'www.tvmao.com':
                    raise
                if active:
                    active()
                response = await client.get(url.replace('https://www.tvmao.com/', 'https://m.tvmao.com/', 1))
            response.raise_for_status()
            return response.text

        path = ''
        work_title = queries[0]
        try:
            for query in queries:
                if active:
                    active()
                search = await read('https://www.tvmao.com/query.jsp?' + urlencode({'keys': query}))
                path = _search_path(search, query, str(year) if season == 1 else '')
                if path:
                    work_title = query
                    break
        except httpx.HTTPError:
            report('电视猫分集搜索不可用，尝试已确认的剧集地址或百度百科')
        path = path or direct_tvmao
        if path and _TVMAO_WORK.fullmatch(path):
            try:
                page = await read('https://www.tvmao.com' + path + '/episode')
                soup = _document(page)
                if _verified(soup, work_title, str(year) if season == 1 else ''):
                    links = _episode_links(page, path)
                    for index, number in enumerate(numbers):
                        if number not in links:
                            continue
                        if index:
                            await asyncio.sleep(.2)
                        try:
                            episode_page = page if number == 1 else await read('https://www.tvmao.com' + links[number])
                            data = parse_tvmao_episode(episode_page, work_title, number, str(year) if season == 1 else '')
                            if data:
                                result[number] = {**data, 'source': '电视猫'}
                            else:
                                report(f'电视猫第 {number} 集未解析到与剧名、集号一致的剧情，尝试百度百科')
                        except httpx.HTTPError:
                            report(f'电视猫第 {number} 集读取失败，尝试百度百科')
                else:
                    report('电视猫剧情页剧名或年份不匹配，未使用该页内容')
            except httpx.HTTPError:
                report('电视猫分集剧情不可用，尝试百度百科')
        remaining = [number for number in numbers if not result.get(number, {}).get('name')
                     or not result.get(number, {}).get('overview')]
        if remaining:
            try:
                fallback = {}
                for query in dict.fromkeys([work_title, *queries]):
                    page = await read('https://baike.baidu.com' + (direct_baike or '/item/' + quote(query, safe='')))
                    fallback = parse_baike_episodes(page, query, str(year) if season == 1 else '')
                    if fallback:
                        break
                if not fallback:
                    report('百度百科未获取到可确认的分集剧情（可能需验证或条目不匹配），不猜填')
                for number in remaining:
                    data = fallback.get(number) or {}
                    if not data:
                        continue
                    current = result.setdefault(number, {})
                    for field in ('name', 'overview'):
                        if not current.get(field) and data.get(field):
                            current[field] = data[field]
                            current['source'] = '电视猫/百度百科' if current.get('source') == '电视猫' else '百度百科'
            except httpx.HTTPError:
                report('百度百科分集剧情读取失败，保留原有信息')
    return result
