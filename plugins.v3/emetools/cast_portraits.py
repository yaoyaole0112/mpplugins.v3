import asyncio
import base64
from io import BytesIO
import re
import time
import unicodedata
from urllib.parse import quote, urlsplit

import httpx
from PIL import Image, ImageOps
from .media_precheck import foreign_text


def portrait_url(value):
    value = str(value or '').strip()
    if re.fullmatch(r'/[\w.-]+\.(?:jpg|jpeg|png|webp)', value, re.I):
        return 'https://image.tmdb.org/t/p/w185' + value
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return ''
    host = (parsed.hostname or '').lower()
    allowed = (host == 'image.tmdb.org' or host.endswith('.doubanio.com') or
               host in {'bkimg.cdn.bcebos.com', 'bkimg.bj.bcebos.com', 'bkimg.bdimg.com',
                        'img.tvmao.com', 'pic.tvmao.com', 'image.tvmao.com'})
    if (parsed.scheme == 'https' and allowed and not parsed.username and not parsed.password and
            port in (None, 443) and not parsed.fragment and
            not re.search(r'celebrity-default|personage-default|default[-_]avatar', parsed.path, re.I)):
        return value
    return ''


def normalized_name(value):
    return ''.join(character for character in unicodedata.normalize('NFKD', str(value or '')).casefold()
                   if character.isalnum())


class CastPortraits:
    def __init__(self, tmdb_json, search, log):
        self.tmdb_json, self.search, self.log = tmdb_json, search, log
        self.cache = {}
        self.uploaded = {}

    async def _read(self, client, path, api_key, **params):
        key = (str(getattr(client, 'base_url', '')), path, tuple(sorted(params.items())))
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        result = await self.tmdb_json(client, path, {'api_key': api_key, **params})
        if not isinstance(result, dict):
            raise ValueError('人物来源返回格式无效')
        if len(self.cache) >= 256:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = time.monotonic(), result
        return result

    @staticmethod
    def _matches(person, actor, media_id, media_type):
        expected = {normalized_name(actor.get(key)) for key in ('name', 'original_name')} - {''}
        names = {normalized_name(value) for value in
                 [person.get('name'), *(person.get('also_known_as') or [])]} - {''}
        credits = (person.get('combined_credits') or {}).get('cast') or []
        return bool(expected & names) and any(
            str(credit.get('id')) == str(media_id) and credit.get('media_type') == media_type
            for credit in credits if isinstance(credit, dict))

    async def complete(self, cast, client, api_key, item, options, *, allow_ai=True):
        cast = [dict(actor) for actor in cast]
        media_id = (item.get('ProviderIds') or {}).get('Tmdb') or (item.get('ProviderIds') or {}).get('TMDB')
        media_type = 'movie' if item.get('Type') == 'Movie' else 'tv'
        attempted, recovered, eligible, unresolved = 0, 0, 0, []
        deadline, reads = time.monotonic() + 60, 0

        async def read(path, **params):
            nonlocal reads
            remaining = deadline - time.monotonic()
            if reads >= 60 or remaining <= 0:
                raise ValueError('本轮头像来源检索达到预算')
            reads += 1
            return await asyncio.wait_for(self._read(client, path, api_key, **params), timeout=min(10, remaining))

        for actor in sorted(cast, key=lambda entry: entry.get('order') if entry.get('order') is not None else 999):
            if eligible >= options['max_actors']:
                break
            existing_image = portrait_url(actor.get('profile_path'))
            if existing_image or not options.get('no_avatar'):
                eligible += 1
            if existing_image and not (actor.get('id') and foreign_text(actor.get('name'))):
                continue
            actor['profile_path'] = actor.get('profile_path') if existing_image else ''
            if attempted >= 40 or reads >= 60 or time.monotonic() >= deadline:
                continue
            attempted += 1
            identifier = str(actor.get('id') or '')
            try:
                if identifier.isdecimal():
                    person = await read(f'person/{identifier}', language='zh-CN', append_to_response='images')
                    if str(person.get('id')) != identifier:
                        continue
                    if foreign_text(actor.get('name')):
                        chinese = next((value for value in [person.get('name'), *(person.get('also_known_as') or [])]
                                        if value and re.search(r'[\u3400-\u9fff]', value) and not foreign_text(value)), '')
                        if chinese:
                            actor.setdefault('original_name', actor['name'])
                            actor['name'] = chinese
                elif str(media_id or '').isdecimal():
                    query = actor.get('original_name') or actor.get('name')
                    candidates = await read('search/person', query=str(query)[:130], language='zh-CN', page=1)
                    matches = []
                    expected = {normalized_name(actor.get(key)) for key in ('name', 'original_name')} - {''}
                    for candidate in (candidates.get('results') or [])[:5]:
                        if normalized_name(candidate.get('name')) not in expected or not candidate.get('id'):
                            continue
                        detail = await read(f"person/{candidate['id']}", language='zh-CN',
                                            append_to_response='combined_credits,images')
                        if self._matches(detail, actor, media_id, media_type):
                            matches.append(detail)
                    if len(matches) != 1:
                        unresolved.append(actor)
                        continue
                    person = matches[0]
                    actor['id'] = person['id']
                else:
                    continue
                image = portrait_url(person.get('profile_path'))
                if not image:
                    image = next((portrait_url(entry.get('file_path')) for entry in
                                  (person.get('images') or {}).get('profiles') or []
                                  if portrait_url(entry.get('file_path'))), '')
                if image and not existing_image:
                    actor['profile_path'] = image
                    recovered += 1
                    if options.get('no_avatar'):
                        eligible += 1
                elif not existing_image:
                    unresolved.append(actor)
            except (ValueError, httpx.HTTPError, KeyError, TypeError, TimeoutError):
                unresolved.append(actor)
        if (allow_ai and unresolved and options.get('ai_enabled') and options.get('ai_portraits') and
                str(media_id or '').isdecimal()):
            targets = unresolved[:10]
            try:
                result = await self.search(item, targets)
                for index, actor in enumerate(targets):
                    candidate = (result or {}).get(str(index))
                    if not isinstance(candidate, dict) or actor.get('profile_path'):
                        continue
                    identifier = str(candidate.get('tmdb_id') or '')
                    source = str(candidate.get('source') or '')
                    if not identifier.isdecimal() or not re.fullmatch(
                            rf'https://(?:www\.)?themoviedb\.org/person/{identifier}(?:-[\w-]+)?/?', source):
                        continue
                    if actor.get('id') and str(actor['id']) != identifier:
                        continue
                    person = await asyncio.wait_for(self._read(client, f'person/{identifier}', api_key,
                        language='zh-CN', append_to_response='combined_credits,images'), timeout=10)
                    if str(person.get('id')) != identifier or not self._matches(person, actor, media_id, media_type):
                        continue
                    image = portrait_url(person.get('profile_path')) or next((
                        portrait_url(entry.get('file_path')) for entry in
                        (person.get('images') or {}).get('profiles') or []
                        if portrait_url(entry.get('file_path'))), '')
                    if image:
                        actor.update(id=person['id'], profile_path=image)
                        recovered += 1
            except (ValueError, httpx.HTTPError, TypeError, KeyError, TimeoutError) as error:
                reason = str(error) if re.fullmatch(r'宿主联网头像检索不可用：[A-Za-z0-9_]+', str(error)) else type(error).__name__
                self.log(f'AI 头像检索不可用（{reason}），需宿主模型支持原生联网；保留可靠来源结果，不重试付费请求')
        if attempted:
            self.log(f'演员头像：先补取再过滤；检查 {attempted} 人，补得 {recovered} 人；未核实头像不采用')
        return cast

    async def _download(self, client, url):
        for redirect in range(4):
            if not portrait_url(url):
                raise ValueError('头像地址不属于可信来源')
            async with client.stream('GET', url, headers={'Referer': 'https://movie.douban.com/'}) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers.get('location', '')))
                    continue
                response.raise_for_status()
                if not response.headers.get('content-type', '').lower().startswith('image/'):
                    raise ValueError('头像响应不是图片')
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 5 * 1024 * 1024:
                        raise ValueError('头像图片过大')
            with Image.open(BytesIO(data)) as image:
                if min(image.size) < 32 or image.width * image.height > 16000000:
                    raise ValueError('头像尺寸异常')
                image = ImageOps.exif_transpose(image).convert('RGB')
                image.thumbnail((600, 900))
                output = BytesIO()
                image.save(output, 'JPEG', quality=90)
                return output.getvalue()
        raise ValueError('头像重定向过多')

    async def sync(self, emby, client, pairs, active):
        completed, failed = 0, 0
        for actor, person in pairs:
            url = portrait_url(actor.get('profile_path'))
            if not url:
                continue
            active()
            try:
                response = await emby.get('Persons/' + quote(person['Name'], safe=''))
                response.raise_for_status()
                target = response.json()
                identifier = str(target.get('Id') or '')
                if (target.get('Type') != 'Person' or not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', identifier) or
                        normalized_name(target.get('Name')) != normalized_name(person['Name'])):
                    failed += 1
                    continue
                cache_key = (str(emby.base_url), identifier)
                recently_uploaded = time.monotonic() - self.uploaded.get(cache_key, float('-inf')) < 60
                if recently_uploaded or (target.get('ImageTags') or {}).get('Primary'):
                    continue
                providers = target.get('ProviderIds') or {}
                source_id = str(providers.get('Tmdb') or providers.get('TMDB') or '')
                if source_id and source_id != str(actor.get('id') or ''):
                    failed += 1
                    continue
                data = await self._download(client, url)
                active()
                interrupted = False
                try:
                    upload = await emby.post(f'Items/{identifier}/Images/Primary',
                                             content=base64.b64encode(data), headers={'Content-Type': 'image/jpeg'})
                    upload.raise_for_status()
                except (httpx.RemoteProtocolError, httpx.NetworkError, httpx.TimeoutException):
                    interrupted = True
                verify = await emby.get(f'Items/{identifier}/Images')
                verify.raise_for_status()
                if not any(entry.get('ImageType') == 'Primary' for entry in verify.json()):
                    raise ValueError('人物头像未通过回读核验')
                if len(self.uploaded) >= 512:
                    self.uploaded.clear()
                self.uploaded[cache_key] = time.monotonic()
                completed += 1
                if interrupted:
                    self.log('演员头像上传响应中断，但回读已确认保存；未重放上传')
            except (httpx.HTTPError, ValueError, OSError, Image.DecompressionBombError, TypeError, KeyError) as error:
                failed += 1
                self.log(f'演员头像补充未确认（{type(error).__name__}），不重放上传、不影响其他演员')
        self.log(f'Emby 演员头像：新增 {completed} 人，未确认 {failed} 人；已有头像不覆盖')
        return completed
