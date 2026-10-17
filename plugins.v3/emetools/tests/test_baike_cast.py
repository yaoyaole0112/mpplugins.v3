import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from emetools.baike_cast import fetch_baike_cast, parse_baike_cast


def actor(name, role='主角', image='https://bkimg.cdn.bcebos.com/pic/test.jpg'):
    return {'actor': {'text': [{'tag': 'text', 'text': name}]},
            'actorRole': [{'text': [{'tag': 'text', 'text': role}]}],
            'actorImg': {'value': {'url': image}}}


def page(actors=(), description='2026年电视剧', title='测试剧', lemmas=(), extra=()):
    data = {'lemmaTitle': title, 'lemmaDesc': description,
            'navigation': {'lemmas': list(lemmas)},
            'structuredContent': [[{'content': list(actors)}, *extra]]}
    return '<div>当前仅显示第一页10人</div><script id="__NEXT_DATA__" type="application/json">' + \
        json.dumps({'props': {'pageProps': {'pageData': data}}}, ensure_ascii=False) + '</script>'


class BaikeCastTests(unittest.TestCase):
    def test_all_pages_embedded_cast_and_directors_are_read(self):
        result = parse_baike_cast(page([actor(f'演员{number}') for number in range(58)], extra=[
            {'content': {'staffDirector': [{'text': [{'tag': 'text', 'text': '导演甲'}]}],
                         'staffActionDirector': [{'text': [{'tag': 'text', 'text': '动作导演'}]}]}},
            {'roleActor': [{'text': [{'tag': 'text', 'text': '角色介绍不算演员'}]}]}]), '测试剧', '2026')
        self.assertEqual(len(result['casts']), 58)
        self.assertEqual(result['cast_rows'], 58)
        self.assertEqual(result['casts'][57]['name'], '演员57')
        self.assertEqual(result['directors'], [{'name': '导演甲', 'role': '导演'}])

    def test_deduplicate_roles_and_do_not_invent_missing_avatar(self):
        result = parse_baike_cast(page([actor('演员甲', '主角', ''), actor('演员甲', '分身', ''),
            actor('演员乙', image='https://untrusted.example/photo.jpg')]), '测试剧')
        self.assertEqual(len(result['casts']), 2)
        self.assertEqual(result['cast_rows'], 3)
        self.assertEqual(result['casts'][0]['role'], '主角 / 分身')
        self.assertTrue(all(not entry['img'] for entry in result['casts']))

    def test_reject_wrong_title_year_and_media_type(self):
        self.assertEqual(parse_baike_cast(page([actor('演员甲')]), '别的剧'), {})
        self.assertEqual(parse_baike_cast(page([actor('演员甲')]), '测试剧', '2025'), {})
        self.assertEqual(parse_baike_cast(page([actor('演员甲')]), '测试剧', media_type='movie'), {})
        self.assertTrue(parse_baike_cast(page([actor('演员甲')], description='2026年电影'),
                                        '测试剧', '2026', 'movie'))

    def test_public_mobile_fallback_reads_complete_cast_in_three_requests(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.host == 'baike.baidu.com':
                return httpx.Response(403)
            if request.url.path.endswith('/123'):
                return httpx.Response(200, text=page([actor(f'演员{number}') for number in range(58)]))
            return httpx.Response(200, text=page(description='网络小说', lemmas=[
                {'lemmaTitle': '测试剧', 'lemmaDesc': '2026年电视剧', 'lemmaId': 123}]))
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('emetools.baike_cast.httpx.AsyncClient', return_value=client):
            result = asyncio.run(fetch_baike_cast('测试剧', '2026'))
        self.assertEqual(len(requests), 3)
        self.assertEqual(len(result['casts']), 58)

    def test_ambiguous_navigation_is_not_followed(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, text=page(description='网络小说', lemmas=[
                {'lemmaTitle': '测试剧', 'lemmaDesc': '2026年电视剧', 'lemmaId': identifier}
                for identifier in [123, 456]]))
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('emetools.baike_cast.httpx.AsyncClient', return_value=client):
            self.assertEqual(asyncio.run(fetch_baike_cast('测试剧', '2026')), {})
        self.assertEqual(len(requests), 2)

    def test_network_error_preserves_other_sources_and_cancellation_propagates(self):
        def handler(request):
            raise httpx.ReadTimeout('timeout', request=request)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('emetools.baike_cast.httpx.AsyncClient', return_value=client):
            self.assertEqual(asyncio.run(fetch_baike_cast('测试剧')), {})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('emetools.baike_cast.httpx.AsyncClient', return_value=client):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(fetch_baike_cast('测试剧', active=Mock(side_effect=asyncio.CancelledError)))
