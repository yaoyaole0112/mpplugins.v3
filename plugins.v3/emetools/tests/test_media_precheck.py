import asyncio
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from emetools.data_enrichment import DataEnrichment, DEFAULT_ENRICH_CONFIG
from emetools.media_precheck import media_checks


class MediaPrecheckTests(unittest.TestCase):
    def setUp(self):
        self.options = dict(DEFAULT_ENRICH_CONFIG)
        self.engine = DataEnrichment(SimpleNamespace())

    def checks(self, item, **kwargs):
        return {entry['field']: entry for entry in media_checks(item, self.options, **kwargs)}

    def test_series_checks_metadata_without_requiring_aggregate_file_information(self):
        checks = self.checks({'Type': 'Series', 'Name': '测试剧集', 'Overview': '中文剧情。' * 40,
            'Genres': ['剧情'], 'Studios': [{'Name': '优酷', 'Id': 123}],
            'People': [{'Name': '演员', 'Type': 'Actor', 'Role': '饰 主角'}]})
        for field in ('标题', '简介/概要', '类型', '工作室/播出平台', '演职员'):
            self.assertEqual(checks[field]['status'], '正常')
        self.assertEqual(checks['媒体信息']['status'], '不适用')

    def test_english_genres_and_studios_are_abnormal(self):
        checks = self.checks({'Type': 'Series', 'Name': '中文剧', 'Overview': '中文剧情。' * 40,
            'Genres': ['Drama'], 'Studios': [{'Name': 'Mango TV'}],
            'People': [{'Name': '演员', 'Type': 'Actor', 'Role': '饰 主角'}]})
        self.assertEqual(checks['类型']['status'], '疑似异常')
        self.assertEqual(checks['工作室/播出平台']['status'], '疑似异常')
        self.assertIn('含外文 1 项', checks['工作室/播出平台']['detail'])

    def test_known_english_metadata_labels_have_safe_chinese_mappings(self):
        async def run():
            return (await self.engine._localize_labels(['Drama', 'Mango TV'], self.options, '测试'))
        self.assertEqual(asyncio.run(run()), ['剧情', '芒果TV'])

    def test_movie_uses_source_size_runtime_and_stream_metadata(self):
        checks = self.checks({'Type': 'Movie', 'Name': '电影', 'Studios': ['电影公司'],
            'MediaSources': [{'Type': 'Default', 'Size': 100, 'RunTimeTicks': 100000,
                'MediaStreams': [{'Type': 'Video', 'Codec': 'hevc', 'Width': 1920, 'Height': 1080},
                                 {'Type': 'Audio', 'Codec': 'aac'}]}]})
        self.assertEqual(checks['媒体信息']['status'], '正常')
        self.assertEqual(checks['音视频流']['status'], '正常')
        self.assertEqual(checks['工作室/播出平台']['status'], '正常')
        self.assertNotIn('分集标题', checks)

    def test_episode_checks_generic_title_duplicate_overview_cast_and_size(self):
        checks = self.checks({'Name': '第4集', 'Overview': '同季重复的简介文本。', 'People': [],
                             'Size': -1, 'RunTimeTicks': 'invalid'}, episode=True, duplicated=True)
        self.assertEqual(checks['分集标题']['status'], '缺失')
        self.assertIn('同季重复', checks['分集简介']['detail'])
        self.assertIn('隐藏', checks['分集演职员']['detail'])
        self.assertIn('分集演职写入未开启', checks['分集演职员']['detail'])
        self.assertEqual(checks['媒体信息']['status'], '缺失')
        self.assertNotIn('工作室/播出平台', checks)

    def test_foreign_cast_and_missing_roles_are_diagnostic_not_invented(self):
        checks = self.checks({'People': [{'Name': 'English Actor', 'Type': 'Actor', 'Role': ''}]}, episode=True)
        self.assertEqual(checks['分集演职员']['status'], '疑似异常')
        self.assertIn('含外文 1 人', checks['分集演职员']['detail'])
        self.assertIn('前缀待补 1 人', checks['分集演职员']['detail'])
        self.assertEqual(self.checks({}, episode=True)['分集演职员']['status'], '待核实')

    def test_stream_runtime_fallback_and_incomplete_codec_information(self):
        checks = self.checks({'Type': 'Movie', 'Size': 100, 'MediaStreams': [
            {'Type': 'Video', 'RunTimeTicks': 10000, 'Width': 1920}, {'Type': 'Audio'}]})
        self.assertEqual(checks['媒体信息']['status'], '正常')
        self.assertEqual(checks['音视频流']['status'], '缺失')
        self.assertIn('分辨率', checks['音视频流']['detail'])
        self.assertIn('音频编码', checks['音视频流']['detail'])

    def test_image_absence_and_missing_image_tags_are_distinct_and_do_not_fetch(self):
        client = SimpleNamespace(get=AsyncMock())
        missing = asyncio.run(self.engine._precheck_image(client, {'Id': '1', 'ImageTags': {}}))
        unknown = asyncio.run(self.engine._precheck_image(client, {'Id': '1'}))
        self.assertEqual(missing['status'], '缺失')
        self.assertEqual(unknown['status'], '待核实')
        client.get.assert_not_awaited()

    def image(self, size=(640, 360)):
        output = BytesIO()
        Image.new('RGB', size, (100, 100, 100)).save(output, 'JPEG')
        return output.getvalue()

    def check_image(self, response=None, error=None):
        def handler(request):
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.params['maxWidth'], '640')
            if error:
                raise error('sensitive signed-url', request=request)
            return response
        async def run():
            async with httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler)) as client:
                return await self.engine._precheck_image(client, {'Id': 'ep1', 'ImageTags': {'Primary': 'tag'}})
        return asyncio.run(run())

    def test_image_dimensions_and_color_checks_are_readonly_and_bounded(self):
        result = self.check_image(httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=self.image()))
        self.assertEqual(result['status'], '正常')
        result = self.check_image(httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=self.image((300, 300))))
        self.assertIn('比例异常', result['detail'])
        with patch.object(self.engine, '_color_cast', return_value='green'):
            result = self.check_image(httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=self.image()))
        self.assertIn('疑似偏色', result['detail'])

    def test_image_network_failures_and_html_are_not_normal_or_leaked(self):
        for result in (self.check_image(error=httpx.ReadTimeout),
                       self.check_image(httpx.Response(200, headers={'content-type': 'text/html'}, content=b'error')),
                       self.check_image(httpx.Response(403))):
            self.assertEqual(result['status'], '待核实')
            self.assertNotIn('sensitive', result['detail'])
        result = self.check_image(httpx.Response(404))
        self.assertEqual(result['status'], '缺失')
        result = self.check_image(httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=b'invalid'))
        self.assertEqual(result['status'], '疑似异常')

    def test_precheck_reports_each_category_counts_unknowns_and_never_writes(self):
        requests = []
        def handler(request):
            requests.append(request.method)
            if request.url.path.endswith('/Items/series'):
                return httpx.Response(200, json={'Id': 'series', 'Type': 'Series', 'Name': '测试剧集',
                    'Genres': [], 'Studios': [], 'People': []})
            return httpx.Response(200, json={'Items': [{'Id': 'ep1', 'Name': '第1集',
                'ParentIndexNumber': 1, 'IndexNumber': 1, 'ImageTags': {}, 'People': []}], 'TotalRecordCount': 1})
        client = httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler))
        with patch.object(self.engine, '_server', return_value=client), \
                patch.object(self.engine, '_user_id', new_callable=AsyncMock, return_value='admin'), \
                patch.object(self.engine, '_tmdb_json', new_callable=AsyncMock) as tmdb, \
                patch.object(self.engine, '_optional_ai_map', new_callable=AsyncMock) as ai:
            asyncio.run(self.engine._overview_preview('Q4::series', None, self.options))
        result = self.engine.status()['overview_preview']
        self.assertEqual(result['checked'], 2)
        self.assertEqual(result['issues'], 2)
        self.assertEqual(result['unknown'], 1)
        self.assertEqual(result['category_counts']['分集图片'], 1)
        self.assertEqual(result['category_counts']['工作室/播出平台'], 1)
        self.assertTrue(result['items'][1]['checks'])
        self.assertEqual(requests, ['GET', 'GET'])
        tmdb.assert_not_awaited()
        ai.assert_not_awaited()

    def test_incomplete_episode_pagination_is_reported_as_error(self):
        client = httpx.AsyncClient(base_url='http://emby/')
        with patch.object(self.engine, '_server', return_value=client), \
                patch.object(self.engine, '_user_id', new_callable=AsyncMock, return_value='admin'), \
                patch.object(self.engine, '_item', new_callable=AsyncMock, return_value={'Type': 'Series'}), \
                patch.object(self.engine, '_json', new_callable=AsyncMock, return_value={'Items': [], 'TotalRecordCount': 1}):
            asyncio.run(self.engine._overview_preview('Q4::series', None, self.options))
        result = self.engine.status()['overview_preview']
        self.assertTrue(result['errors'])
        self.assertEqual(result['checked'], 0)


if __name__ == '__main__':
    unittest.main()
