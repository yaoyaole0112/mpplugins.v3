import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from emetools.tvmao_client import _search_path, fetch_tvmao_cast, parse_actors


class TvmaoTests(unittest.TestCase):
    def test_search_needs_exact_work_and_no_ambiguity(self):
        search = ('<a href="/kanju/OTHER999" title="征途第二季">第二季</a>'
                  '<a href="/kanju/YXAhXWhl" title="征途演员表">征途</a>')
        self.assertEqual(_search_path(search, '征途'), '/kanju/YXAhXWhl')
        self.assertEqual(_search_path('<a href="/kanju/YXAhXWhl" title="征途 (2024) 演员表">征途</a>',
                                      '征途', '2026'), '')
        self.assertEqual(_search_path(search.replace('OTHER999', 'ANOTHER').replace('征途第二季', '征途'),
                                      '征途'), '')
        self.assertEqual(_search_path('<a href="https://evil.example/kanju/YXAhXWhl" title="征途">征途</a>',
                                      '征途'), '')

    def test_actor_card_and_old_li_layout(self):
        page = ('<img src="/photos/a.jpg" alt="沈润（许凯饰演）">'
                '<img data-src="/photos/b.jpg" alt="阿遥（周也饰演）">'
                '<li><span class="c-name">叶昭</span><a class="black_link">张瑶</a></li>'
                '<img src="/photos/third.jpg" alt="沈润（许凯饰演）">')
        cast = parse_actors(page)
        self.assertEqual([entry['name'] for entry in cast], ['许凯', '周也', '张瑶'])
        self.assertEqual(cast[0]['role'], '沈润')
        self.assertEqual(cast[0]['img'], 'https://www.tvmao.com/photos/a.jpg')

    def test_fetch_rejects_wrong_page_title_and_uses_only_tvmao(self):
        requested = []

        def handler(request):
            requested.append(str(request.url))
            if request.url.path == '/query.jsp':
                return httpx.Response(200, text='<a href="/kanju/YXAhXWhl" title="征途演员表">征途</a>')
            return httpx.Response(200, text='<title>征途演员表 - 电视猫</title>'
                       '<img src="/actors/p.jpg" alt="铁木真（李健饰演）">')

        original = httpx.AsyncClient
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), **kwargs)

        with patch('emetools.tvmao_client.httpx.AsyncClient', side_effect=client):
            cast = asyncio.run(fetch_tvmao_cast('征途'))
        self.assertEqual(cast[0]['name'], '李健')
        self.assertEqual([httpx.URL(url).host for url in requested], ['www.tvmao.com'] * 2)
        self.assertEqual(httpx.URL(requested[1]).path, '/kanju/YXAhXWhl/actors')

        def wrong_page(request):
            if request.url.path == '/query.jsp':
                return httpx.Response(200, text='<a href="/kanju/YXAhXWhl" title="征途演员表">征途</a>')
            return httpx.Response(200, text='<title>另一部电视剧演员表</title>'
                       '<img src="/actors/p.jpg" alt="铁木真（李健饰演）">')

        with patch('emetools.tvmao_client.httpx.AsyncClient',
                   side_effect=lambda **kwargs: original(transport=httpx.MockTransport(wrong_page), **kwargs)):
            self.assertEqual(asyncio.run(fetch_tvmao_cast('征途')), [])

    def test_user_provided_link_bypasses_search_only_for_confirmed_tmdb_id(self):
        requested, stages = [], []

        def handler(request):
            requested.append(request.url.path)
            if request.url.path == '/query.jsp':
                return httpx.Response(200, text='<a href="/kanju/OTHER" title="征途第二季">另一剧</a>')
            return httpx.Response(200, text='<title>电视剧《征途》演员表_电视猫</title>'
                                  '<img src="/photos/li.jpg" alt="朱德（李健饰演）">')

        original = httpx.AsyncClient
        with patch('emetools.tvmao_client.httpx.AsyncClient',
                   side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)):
            cast = asyncio.run(fetch_tvmao_cast('征途', '2026', '336207', stages.append))
            unrelated = asyncio.run(fetch_tvmao_cast('征途', '2026', 'not-same-id', stages.append))
        self.assertEqual(cast[0]['name'], '李健')
        self.assertEqual(cast[0]['role'], '朱德')
        self.assertIn('/kanju/YXAhXWhl/actors', requested)
        self.assertEqual(unrelated, [])
        self.assertTrue(any('用户提供' in stage for stage in stages))

    def test_direct_link_rejects_wrong_page_title_and_reports_parse_failure(self):
        original = httpx.AsyncClient
        statuses = []

        def handler(request):
            if request.url.path == '/query.jsp':
                return httpx.Response(200, text='搜索结果为空')
            return httpx.Response(200, text='<title>另一部电视剧演员表</title>'
                                              '<img alt="朱德（李健饰演）">')

        with patch('emetools.tvmao_client.httpx.AsyncClient',
                   side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)):
            self.assertEqual(asyncio.run(fetch_tvmao_cast('征途', '2026', '336207', statuses.append)), [])
        self.assertTrue(any('标题与剧名不符' in stage for stage in statuses))
