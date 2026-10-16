import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from emetools.episode_plots import (fetch_episode_plots, first_sentence, missing_episode_title,
                                   parse_baike_episodes, parse_tvmao_episode, _episode_links)


def tvmao_page(number=23, title='测试剧', caption='剧情标题', plot='这是第一句话。这是第二句话。'):
    return (f'<title>{title}第{number}集分集剧情_电视剧_电视猫</title>'
            f'<p class="epi_t">第{number}集：{caption}</p>'
            f'<div class="e_a_t">电视猫 时间：2026-09-24</div>'
            f'<article class="clear epi_c"><p>{plot}</p><script>广告。</script>'
            '<div class="epipd">图片版权。</div></article>')


class EpisodePlotsTests(unittest.TestCase):
    def test_tvmao_keeps_caption_and_only_first_sentence(self):
        data = parse_tvmao_episode(tvmao_page(caption='沈玉儿送钱给陈盛元'), '测试剧', 23)
        self.assertEqual(data, {'name': '第23集：沈玉儿送钱给陈盛元', 'overview': '这是第一句话。'})

    def test_tvmao_rejects_wrong_work_episode_and_year(self):
        self.assertEqual(parse_tvmao_episode(tvmao_page(), '其他剧', 23), {})
        self.assertEqual(parse_tvmao_episode(tvmao_page(), '测试剧', 24), {})
        page = tvmao_page().replace('测试剧第23集', '测试剧（2024）第23集')
        self.assertEqual(parse_tvmao_episode(page, '测试剧', 23, '2026'), {})
        self.assertEqual(parse_tvmao_episode(tvmao_page() +
            '<div class="obj-summary-info"><p class="obj-info">中国内地 | 2024年 | 电视剧</p></div>',
            '测试剧', 23, '2026'), {})

    def test_tvmao_mobile_layout_and_drama_path_alias(self):
        page = ('<title>测试剧第23集分集剧情_电视剧_电视猫</title>'
                '<p class="d_e_t">第23集：手机版标题</p>'
                '<div class="epicol"><div class="episode"><p>手机版首句。后文。</p></div></div>'
                '<a href="/drama/TEST123/episode/7-23">23</a>')
        self.assertEqual(parse_tvmao_episode(page, '测试剧', 23), {
            'name': '第23集：手机版标题', 'overview': '手机版首句。'})
        self.assertEqual(_episode_links(page, '/kanju/TEST123'), {23: '/drama/TEST123/episode/7-23'})

    def test_first_sentence_uses_chinese_period_not_comma_or_question_mark(self):
        self.assertEqual(first_sentence('  谁来了？甲解释，乙作答。后续剧情。'), '谁来了？甲解释，乙作答。')
        self.assertEqual(first_sentence('没有句号的简介'), '没有句号的简介')
        self.assertEqual(first_sentence('暂无简介'), '')

    def test_generic_episode_titles_are_missing(self):
        for value in ('', '第23集', '第 二十三 集', 'Episode 23', 'S01E23', '未命名'):
            self.assertTrue(missing_episode_title(value), value)
        self.assertFalse(missing_episode_title('第23集：剧情标题'))
        self.assertFalse(missing_episode_title('原有中文标题'))

    def test_baike_reads_multiple_episodes_without_borrowing_next_episode(self):
        page = ('<title>测试剧_百度百科</title><h1>测试剧</h1><h2>分集剧情</h2>'
                '<div>第23集：百科标题</div><div>百科第一句。百科第二句。</div>'
                '<div>第24集</div><div>下一集第一句。<sup>[12]</sup>下一集第二句。</div>'
                '<h2>演职员表</h2><p>演员资料。</p>')
        self.assertEqual(parse_baike_episodes(page, '测试剧'), {
            23: {'name': '第23集：百科标题', 'overview': '百科第一句。'},
            24: {'name': '', 'overview': '下一集第一句。'}})
        self.assertEqual(parse_baike_episodes(page, '同名另一剧'), {})
        self.assertEqual(parse_baike_episodes('<title>百度安全验证</title>', '测试剧'), {})
        self.assertEqual(parse_baike_episodes(page + '<dl><dt>首播时间</dt><dd>2024年</dd></dl>',
                                              '测试剧', '2026'), {})

    def test_transport_failure_uses_public_mobile_layout(self):
        def handler(request):
            if request.url.host == 'www.tvmao.com':
                raise httpx.RemoteProtocolError('connection closed', request=request)
            self.assertEqual(request.url.host, 'm.tvmao.com')
            if request.url.path == '/query.jsp':
                return httpx.Response(200, text='<a href="/kanju/TEST123" title="测试剧">测试剧</a>')
            return httpx.Response(200, text=tvmao_page(1) + '<a href="/drama/TEST123/episode">1</a>')
        self.assertEqual(self.run_fetch(handler, numbers=(1,))[1]['source'], '电视猫')

    def test_episode_links_only_use_verified_work_and_tvmao_host(self):
        page = ('<a href="/kanju/TEST123/episode">1</a>'
                '<a href="/kanju/TEST123/episode/7-23">23</a>'
                '<a href="https://evil.example/kanju/TEST123/episode/7-24">24</a>'
                '<a href="/kanju/OTHER123/episode/7-25">25</a>')
        self.assertEqual(_episode_links(page, '/kanju/TEST123'), {
            1: '/kanju/TEST123/episode', 23: '/kanju/TEST123/episode/7-23'})

    def run_fetch(self, handler, season=1, title='测试剧', numbers=(23, 24)):
        original = httpx.AsyncClient
        with patch('emetools.episode_plots.httpx.AsyncClient', side_effect=lambda **kwargs:
                   original(transport=httpx.MockTransport(handler), **kwargs)), \
             patch('emetools.episode_plots.asyncio.sleep'):
            return asyncio.run(fetch_episode_plots(title, '2026', season, numbers))

    def test_tvmao_first_baike_only_fills_missing_episode(self):
        requested = []

        def handler(request):
            requested.append(str(request.url))
            if request.url.path == '/query.jsp':
                return httpx.Response(200, text='<a href="/kanju/TEST123" title="测试剧">测试剧</a>')
            if request.url.path == '/kanju/TEST123/episode':
                return httpx.Response(200, text='<title>测试剧分集剧情_电视猫</title>'
                    '<a href="/kanju/TEST123/episode/7-23">23</a>')
            if request.url.path.endswith('/7-23'):
                return httpx.Response(200, text=tvmao_page())
            if request.url.host == 'baike.baidu.com':
                return httpx.Response(200, text='<title>测试剧_百度百科</title><h1>测试剧</h1>'
                    '<h2>分集剧情</h2><h3>第23集：不应覆盖电视猫</h3><p>百科甲。</p>'
                    '<h3>第24集：百科乙标题</h3><p>百科乙。第二句。</p>')
            return httpx.Response(404)

        result = self.run_fetch(handler)
        self.assertEqual(result[23], {'name': '第23集：剧情标题', 'overview': '这是第一句话。', 'source': '电视猫'})
        self.assertEqual(result[24], {'name': '第24集：百科乙标题', 'overview': '百科乙。', 'source': '百度百科'})
        self.assertEqual(len(requested), 5)

    def test_public_suggestions_find_all_missing_stars_titles_without_known_work_mapping(self):
        requested = []
        title = '此处通往繁星'

        def handler(request):
            requested.append(request.url.path)
            self.assertEqual(request.url.host, 'www.tvmao.com')
            if request.url.path == '/servlet/queryobject':
                self.assertEqual(request.url.params['type'], 'all')
                self.assertEqual(request.url.params['term'], title)
                return httpx.Response(200, json=[{'name': title, 'year': 2026, 'typeId': 11,
                                                   'url': '/kanju/ZmdlcSFc'}])
            if request.url.path == '/kanju/ZmdlcSFc/episode':
                return httpx.Response(200, text=f'<title>{title}分集剧情_电视猫</title>' + ''.join(
                    f'<a href="/kanju/ZmdlcSFc/episode/{(number - 1) // 3}-{number}">{number}</a>'
                    for number in range(4, 10)))
            number = int(request.url.path.rsplit('-', 1)[1])
            return httpx.Response(200, text=tvmao_page(number, title))

        result = self.run_fetch(handler, title=title, numbers=range(4, 10))
        self.assertEqual(set(result), set(range(4, 10)))
        self.assertEqual(len(requested), 8)
        self.assertNotIn('/query.jsp', requested)
        for number in range(4, 10):
            self.assertEqual(result[number]['name'], f'第{number}集：剧情标题')
            self.assertEqual(result[number]['source'], '电视猫')

    def test_complete_tvmao_data_does_not_request_baike(self):
        def handler(request):
            self.assertNotEqual(request.url.host, 'baike.baidu.com')
            if request.url.path == '/query.jsp':
                return httpx.Response(200, text='<a href="/kanju/TEST123" title="测试剧">测试剧</a>')
            if request.url.path.endswith('/episode'):
                return httpx.Response(200, text=tvmao_page(1) + '<a href="/kanju/TEST123/episode">1</a>')
            return httpx.Response(404)
        result = self.run_fetch(handler, numbers=(1,))
        self.assertEqual(result[1]['source'], '电视猫')

    def test_network_failures_and_verification_page_do_not_invent_data(self):
        def handler(request):
            if request.url.host in {'www.tvmao.com', 'm.tvmao.com'}:
                raise httpx.ReadTimeout('timeout', request=request)
            return httpx.Response(200, text='<title>百度安全验证</title>')
        self.assertEqual(self.run_fetch(handler), {})

    def test_season_two_never_uses_unqualified_season_one_result(self):
        def handler(request):
            if request.url.path == '/query.jsp':
                self.assertIn(request.url.params['keys'], ('测试剧第二季', '测试剧第2季'))
                return httpx.Response(200, text='<a href="/kanju/TEST123" title="测试剧">测试剧</a>')
            return httpx.Response(200, text='<title>测试剧_百度百科</title><h1>测试剧</h1>'
                '<h2>分集剧情</h2><h3>第23集</h3><p>第一季不应误用。</p>')
        self.assertEqual(self.run_fetch(handler, season=2), {})

    def test_cancellation_is_checked_before_reading_source(self):
        active = Mock(side_effect=RuntimeError('已取消'))
        with self.assertRaisesRegex(RuntimeError, '已取消'):
            asyncio.run(fetch_episode_plots('测试剧', '2026', 1, [23], active=active))


if __name__ == '__main__':
    unittest.main()
