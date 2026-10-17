import re
from urllib.parse import quote, urlsplit

import httpx

from .episode_plots import _baike_candidates, _baike_data, _baike_text
from .tvmao_client import _USER_AGENT


def parse_baike_cast(page, title, year='', media_type='tv'):
    data = _baike_data(page)
    if not data or data.get('lemmaTitle') != title:
        return {}
    description = str(data.get('lemmaDesc') or '')
    category = r'电影|影片' if media_type == 'movie' else r'电视剧|剧集|短剧|玄幻剧|仙侠剧|悬疑剧|爱情剧|古装剧'
    if not re.search(category, description):
        return {}
    years = re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', description)
    if year and years and str(year)[:4] not in years:
        return {}
    for field in (data.get('card') or {}).get('content') or []:
        if field.get('title') in {'首播时间', '首播日期', '上映时间', '发行时间'}:
            dates = re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', _baike_text(field.get('data')))
            if year and dates and str(year)[:4] not in dates:
                return {}
    cast, directors, seen = [], [], {}
    row_count = 0

    def collect(value):
        nonlocal row_count
        if isinstance(value, list):
            for child in value:
                collect(child)
        elif isinstance(value, dict):
            if 'actor' in value and 'actorRole' in value:
                row_count += 1
                name = _baike_text(value['actor']).strip()
                roles = [_baike_text(role).strip() for role in value.get('actorRole') or []]
                role = ' / '.join(dict.fromkeys(role for role in roles if role and role != '-'))
                image = (value.get('actorImg') or {}).get('value') or {}
                image_url = str(image.get('url') or '')
                parsed = urlsplit(image_url)
                if parsed.scheme != 'https' or parsed.hostname not in {
                        'bkimg.cdn.bcebos.com', 'bkimg.bj.bcebos.com', 'bkimg.bdimg.com'}:
                    image_url = ''
                key = re.sub(r'\s+', '', name)
                if key and key not in seen:
                    seen[key] = len(cast)
                    cast.append({'name': name, 'role': role, 'img': image_url, 'order': len(cast)})
                elif key:
                    existing = cast[seen[key]]
                    existing['role'] = ' / '.join(dict.fromkeys(
                        part for part in (existing['role'], role) if part))
                    existing['img'] = existing['img'] or image_url
            elif 'staffDirector' in value:
                for person in value.get('staffDirector') or []:
                    name = _baike_text(person).strip()
                    if name and name not in {entry['name'] for entry in directors}:
                        directors.append({'name': name, 'role': '导演'})
            else:
                for child in value.values():
                    collect(child)

    collect(data.get('structuredContent') or [])
    return {'casts': cast, 'directors': directors, 'cast_rows': row_count} if cast or directors else {}


async def fetch_baike_cast(title, year='', media_type='tv', status=None, active=None):
    if not title or len(title) > 80:
        return {}

    def report(message):
        if status:
            status(message)

    async with httpx.AsyncClient(headers={'User-Agent': _USER_AGENT},
                                 timeout=httpx.Timeout(12, connect=6),
                                 follow_redirects=True, trust_env=False) as client:
        for host in ('baike.baidu.com', 'wapbaike.baidu.com'):
            try:
                if active:
                    active()
                response = await client.get('https://' + host + '/item/' + quote(title, safe=''))
                if active:
                    active()
                response.raise_for_status()
                result = parse_baike_cast(response.text, title, year, media_type)
                if not result:
                    candidates = _baike_candidates(response.text, title, year, media_type)
                    if candidates:
                        if active:
                            active()
                        response = await client.get('https://' + host + candidates[0])
                        if active:
                            active()
                        response.raise_for_status()
                        result = parse_baike_cast(response.text, title, year, media_type)
                if result:
                    report(f"百度百科演员表：完整读取 {result['cast_rows']} 条，去重后 {len(result.get('casts') or [])} 人，"
                           f"导演 {len(result.get('directors') or [])} 人（包含全部分页内嵌数据，写入仍按演职上限及头像设置）")
                    return result
            except httpx.HTTPStatusError as error:
                report(f'百度百科演职 {host} 返回 HTTP {error.response.status_code}，尝试其他公开入口')
            except (httpx.HTTPError, ValueError, TypeError, AttributeError) as error:
                report(f'百度百科演职 {host} 读取失败：{type(error).__name__}，保留其他来源结果')
    report('百度百科未找到可唯一确认的完整演职数据，保留其他来源结果')
    return {}
