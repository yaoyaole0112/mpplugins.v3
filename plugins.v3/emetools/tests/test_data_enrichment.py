import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from emetools.data_enrichment import (DB_SCRIPT, DEFAULT_ENRICH_CONFIG, DataEnrichment,
                                      PreviewFrameError, _docker_job, _frame_host_root, validate_enrich_config)


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.strm = root / 'S01E01.DoVi.strm'
        self.strm.write_text('https://example.org/video.mp4\n', encoding='utf8')
        self.storage = {}
        owner = SimpleNamespace(_strm_root=str(root), get_data=lambda key: self.storage.get(key),
                                save_data=lambda key, value: self.storage.__setitem__(key, value))
        self.enrichment = DataEnrichment(owner)
        verify_patch = patch.object(self.enrichment, '_verify_saved_item', new_callable=AsyncMock, return_value=True)
        verify_patch.start()
        self.addCleanup(verify_patch.stop)
        web_patch = patch.object(self.enrichment, '_web_episode_plots', new_callable=AsyncMock, return_value={})
        self.web_plots = web_patch.start()
        self.addCleanup(web_patch.stop)
        baike_patch = patch.object(self.enrichment, '_baike_cast', new_callable=AsyncMock, return_value={})
        self.baike_cast = baike_patch.start()
        self.addCleanup(baike_patch.stop)
        douban_patch = patch.object(self.enrichment, '_douban_data', new_callable=AsyncMock, return_value={})
        self.douban_data = douban_patch.start()
        self.addCleanup(douban_patch.stop)

    def real_douban_data(self, *args, **kwargs):
        return DataEnrichment._douban_data(self.enrichment, *args, **kwargs)

    def test_baike_supplements_chinese_cast_without_replacing_primary_ids(self):
        original = [{'id': 101, 'name': '演员甲', 'role': '主角', 'profile_path': '/primary.jpg'}]
        self.baike_cast.return_value = {'casts': [
            {'name': '演员甲', 'role': '主角', 'img': 'https://bkimg.cdn.bcebos.com/other.jpg'},
            *[{'name': f'演员{number}', 'role': '配角', 'img': ''} for number in range(1, 58)]],
            'directors': [{'name': '导演乙', 'role': '导演'}]}
        cast, directors = asyncio.run(self.enrichment._supplement_baike_cast(
            original, {'Name': '测试剧'}, DEFAULT_ENRICH_CONFIG))
        self.baike_cast.assert_awaited_once()
        self.assertEqual(len(cast), 58)
        self.assertEqual(cast[0]['id'], 101)
        self.assertEqual(cast[0]['profile_path'], '/primary.jpg')
        self.assertEqual(cast[-1]['name'], '演员57')
        self.assertEqual(directors[0]['name'], '导演乙')

    def test_baike_not_requested_when_actor_limit_already_localized(self):
        cast = [{'name': '演员甲', 'role': '主角', 'profile_path': '/primary.jpg'}]
        result, directors = asyncio.run(self.enrichment._supplement_baike_cast(
            cast, {'Name': '测试电影'}, {**DEFAULT_ENRICH_CONFIG, 'max_actors': 1}, 'movie'))
        self.baike_cast.assert_not_awaited()
        self.assertEqual(result, cast)
        self.assertEqual(directors, [])

    def test_baike_cast_written_for_movies_and_series_with_shared_settings(self):
        for media_type in ('Movie', 'Series'):
            with self.subTest(media_type=media_type):
                saved = []
                def emby_handler(request):
                    saved.append(json.loads(request.content))
                    return httpx.Response(204)
                emby = httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(emby_handler))
                tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/')
                item = {'Id': 'test1', 'Type': media_type, 'Name': '测试剧',
                        'ProductionYear': 2026, 'ProviderIds': {'Tmdb': '123'}}
                primary = [{'name': '演员甲', 'character': '主角', 'roles': [{'character': '主角'}],
                            'profile_path': '/primary.jpg'}]
                detail = {'credits': {'cast': primary}, 'aggregate_credits': {'cast': primary}}
                self.baike_cast.return_value = {'casts': [
                    {'name': '演员甲', 'role': '主角', 'img': 'https://bkimg.cdn.bcebos.com/new.jpg'},
                    {'name': '无头像演员', 'role': '护卫', 'img': ''},
                    *[{'name': f'演员{number}', 'role': '护卫',
                       'img': 'https://bkimg.cdn.bcebos.com/new.jpg'} for number in range(57)]],
                    'directors': [{'name': '导演乙', 'role': '导演'}]}
                with patch.object(self.enrichment, '_server', return_value=emby), \
                     patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
                     patch.object(self.enrichment, '_user_id', new_callable=AsyncMock, return_value='user'), \
                     patch.object(self.enrichment, '_item', new_callable=AsyncMock, return_value=item), \
                     patch.object(self.enrichment, '_tmdb_json', new_callable=AsyncMock, return_value=detail), \
                     patch.object(self.enrichment, '_douban_data', new_callable=AsyncMock, return_value={}), \
                     patch.object(self.enrichment, '_tvmao_cast', new_callable=AsyncMock, return_value=[]), \
                     patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
                    asyncio.run(self.enrichment._enrich('Q4', 'test1', 'credits',
                        {**DEFAULT_ENRICH_CONFIG, 'episode_cast': False, 'ai_enabled': False}))
                people = saved[0]['People']
                actors = [person for person in people if person['Type'] == 'Actor']
                self.assertEqual(len(actors), 30)
                self.assertEqual(actors[0]['Name'], '演员甲')
                self.assertNotIn('无头像演员', [person['Name'] for person in people])
                self.assertTrue(all(person['Role'].startswith('饰 ') for person in actors))
                self.assertEqual([person['Role'] for person in people if person['Type'] == 'Director'], ['导演'])
                self.baike_cast.assert_awaited_with(item, 'movie' if media_type == 'Movie' else 'tv')

    def test_movie_enrichment_writes_localized_overview_cast_and_director(self):
        saved = []
        def emby_handler(request):
            if request.method == 'POST':
                saved.append(json.loads(request.content))
                return httpx.Response(204)
            return httpx.Response(200, json={'Id': 'movie1', 'Type': 'Movie', 'Name': '旧电影名',
                'ProductionYear': 2026, 'ProviderIds': {'Tmdb': '12345'},
                'Overview': 'English description', 'LockedFields': ['Cast']})

        def tmdb_handler(request):
            self.assertIn('/movie/12345', request.url.path)
            return httpx.Response(200, json={'title': '中文电影名', 'overview': '中文剧情介绍',
                'credits': {'cast': [{'name': '演员甲', 'character': '主角', 'profile_path': '/a.jpg'}],
                            'crew': [{'name': '导演乙', 'job': 'Director'}]}})

        emby = httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/', transport=httpx.MockTransport(tmdb_handler))
        with patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
             patch.object(self.enrichment, '_user_id', new_callable=AsyncMock, return_value='user'), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
            asyncio.run(self.enrichment._enrich('Q4', 'movie1', 'all', DEFAULT_ENRICH_CONFIG))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['Name'], '中文电影名')
        self.assertEqual(saved[0]['Overview'], '中文剧情介绍')
        self.assertEqual([(person['Name'], person['Type']) for person in saved[0]['People']],
                         [('导演乙', 'Director'), ('演员甲', 'Actor')])
        self.assertNotIn('Cast', saved[0]['LockedFields'])

    def test_movie_preserves_chinese_overview_when_tmdb_only_has_english(self):
        saved = []
        def emby_handler(request):
            if request.method == 'POST':
                saved.append(json.loads(request.content))
                return httpx.Response(204)
            return httpx.Response(200, json={'Id': 'movie1', 'Type': 'Movie', 'Name': '电影名',
                'ProviderIds': {'Tmdb': '12345'}, 'Overview': '已有中文简介'})

        emby = httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'title': '电影名', 'overview': 'English only'})))
        with patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
             patch.object(self.enrichment, '_user_id', new_callable=AsyncMock, return_value='user'), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
            asyncio.run(self.enrichment._enrich('Q4', 'movie1', 'metadata', DEFAULT_ENRICH_CONFIG))
        self.assertEqual(saved, [])

    def test_movie_uses_tvmao_cast_and_prefixes_actor_roles(self):
        saved = []
        def emby_handler(request):
            if request.method == 'POST':
                saved.append(json.loads(request.content))
                return httpx.Response(204)
            return httpx.Response(200, json={'Id': 'movie1', 'Type': 'Movie', 'Name': '蜘蛛侠：崭新之日',
                'ProductionYear': 2026, 'ProviderIds': {'Tmdb': '12345'}})

        emby = httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'title': '蜘蛛侠：崭新之日', 'overview': '简介',
                'credits': {'cast': [{'name': 'Tom Holland', 'character': 'Peter Parker / Spider-Man',
                                      'profile_path': '/tom.jpg'}], 'crew': []}})))
        with patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
             patch.object(self.enrichment, '_user_id', new_callable=AsyncMock, return_value='user'), \
             patch.object(self.enrichment, '_douban_data', new_callable=AsyncMock,
                          return_value={'casts': [{'name': '汤姆·赫兰德', 'original_name': 'Tom Holland',
                                                  'role': '饰 彼得·帕克／蜘蛛侠 Peter Parker / Spider-Man',
                                                  'img': ''}]}), \
             patch.object(self.enrichment, '_tvmao_cast', new_callable=AsyncMock, return_value=[]), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
            asyncio.run(self.enrichment._enrich('Q4', 'movie1', 'credits', DEFAULT_ENRICH_CONFIG))
        self.assertEqual(saved[0]['People'][0]['Name'], '汤姆·赫兰德')
        self.assertEqual(saved[0]['People'][0]['Role'], '饰 彼得·帕克／蜘蛛侠')
        self.assertEqual(len(saved[0]['People']), 1)

    def test_tmdb_movie_keeps_primary_cast_when_secondary_translation_lacks_avatar(self):
        saved = []
        def emby_handler(request):
            if request.method == 'POST':
                saved.append(json.loads(request.content))
                return httpx.Response(204)
            return httpx.Response(200, json={'Id': 'movie1', 'Type': 'Movie',
                'Name': '蜘蛛侠：崭新之日', 'ProductionYear': 2026,
                'ProviderIds': {'Tmdb': '12345'}})
        emby = httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'title': '蜘蛛侠：崭新之日', 'overview': '简介',
                'credits': {'cast': [
                    {'name': 'Tom Holland', 'character': 'Peter Parker / Spider-Man', 'order': 0,
                     'profile_path': '/tom.jpg'},
                    {'name': 'Billy Clements', 'character': 'Additional Crew', 'order': 1},
                ], 'crew': []}})))
        with patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
             patch.object(self.enrichment, '_user_id', new_callable=AsyncMock, return_value='user'), \
             patch.object(self.enrichment, '_douban_data', new_callable=AsyncMock,
                          return_value={'casts': [{'name': '汤姆·赫兰德', 'original_name': 'Tom Holland',
                                                  'role': '饰 彼得·帕克／蜘蛛侠', 'img': ''}]}), \
             patch.object(self.enrichment, '_tvmao_cast', new_callable=AsyncMock, return_value=[]), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
            asyncio.run(self.enrichment._enrich('Q4', 'movie1', 'credits', DEFAULT_ENRICH_CONFIG))
        names = [person['Name'] for person in saved[0]['People']]
        self.assertIn('汤姆·赫兰德', names)
        self.assertNotIn('Billy Clements', names)
        tom = next(person for person in saved[0]['People'] if person['Name'] == '汤姆·赫兰德')
        self.assertEqual(tom['Role'], '饰 彼得·帕克／蜘蛛侠')

    def test_movie_secondary_translation_preserves_primary_order_and_excludes_other_actors(self):
        primary = [{'name': 'Tom Holland', 'original_name': 'Tom Holland', 'role': 'Peter Parker', 'id': 1},
                   {'name': 'Billy Clements', 'role': 'Officer', 'id': 2}]
        secondary = [{'name': '比利', 'original_name': 'Billy Clements', 'role': '警员'},
                     {'name': '汤姆·赫兰德', 'original_name': 'Tom Holland', 'role': '彼得·帕克'},
                     {'name': '其他演员', 'role': '其他角色'}]
        result = self.enrichment._localize_primary_cast(primary, secondary)
        self.assertEqual([actor['id'] for actor in result], [1, 2])
        self.assertEqual([actor['role'] for actor in result], ['彼得·帕克', '警员'])
        self.assertEqual(primary[0]['name'], 'Tom Holland')

    def test_movie_uses_tmdb_person_identity_avatar_filter_and_cast_lock(self):
        saved = []
        emby = httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(
            lambda request: (saved.append(json.loads(request.content)) or httpx.Response(204))))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/')
        detail = {'credits': {'cast': [
            {'id': 1136406, 'name': 'Tom Holland', 'original_name': 'Tom Holland',
             'character': '蜘蛛侠', 'profile_path': '/tom.jpg'},
            {'id': 2, 'name': '演员乙', 'character': '警员'},
            {'name': '无头像演员', 'character': '其他角色'}]}}
        async def fetch(client, endpoint, params):
            if endpoint.startswith('movie/'):
                return detail
            if endpoint == 'person/1136406':
                return {'name': '汤姆·赫兰德', 'profile_path': '/tom.jpg'}
            return {'name': '演员乙', 'profile_path': '/other.jpg'}
        with patch.object(self.enrichment, '_tmdb_json', side_effect=fetch), \
             patch.object(self.enrichment, '_douban_data', new_callable=AsyncMock,
                          return_value={'casts': [{'name': '汤姆·霍兰德', 'original_name': 'Tom Holland',
                                                   'role': '蜘蛛侠'}]}), \
             patch.object(self.enrichment, '_tvmao_cast', new_callable=AsyncMock, return_value=[]):
            asyncio.run(self.enrichment._enrich_movie(emby, tmdb, 'movie1',
                {'Type': 'Movie', 'ProviderIds': {'Tmdb': '969681'}}, 'credits',
                {**DEFAULT_ENRICH_CONFIG, 'max_actors': 3, 'cast_lock_min': 2}, 'key'))
            asyncio.run(self.enrichment._enrich_movie(emby, tmdb, 'movie1',
                {'Type': 'Movie', 'ProviderIds': {'Tmdb': '969681'}}, 'credits',
                {**DEFAULT_ENRICH_CONFIG, 'no_avatar': False, 'max_actors': 3, 'cast_lock_min': 2}, 'key'))
        self.assertEqual([person['Name'] for person in saved[0]['People']], ['汤姆·霍兰德', '演员乙'])
        self.assertIn('Cast', saved[0]['LockedFields'])
        self.assertEqual([person['Name'] for person in saved[1]['People']],
                         ['汤姆·霍兰德', '演员乙', '无头像演员'])
        asyncio.run(emby.aclose())
        asyncio.run(tmdb.aclose())

    def test_movie_ai_translates_roles_and_keeps_primary_cast(self):
        saved = []
        def handler(request):
            saved.append(json.loads(request.content))
            return httpx.Response(204)
        emby = httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/')
        detail = {'credits': {'cast': [
            {'name': '比利·克莱门茨', 'character': 'Officer', 'profile_path': '/b.jpg'},
            {'name': '扎布丽娜·格瓦拉', 'character': 'School Principal', 'profile_path': '/z.jpg'}]}}
        with patch.object(self.enrichment, '_tmdb_json', new_callable=AsyncMock, return_value=detail), \
             patch.object(self.enrichment, '_douban_data', new_callable=AsyncMock, return_value={}), \
             patch.object(self.enrichment, '_tvmao_cast', new_callable=AsyncMock, return_value=[]), \
             patch.object(self.enrichment, '_optional_ai_map', new_callable=AsyncMock,
                          return_value={'r0': '警员', 'r1': '校长'}):
            asyncio.run(self.enrichment._enrich_movie(emby, tmdb, 'movie1',
                {'Type': 'Movie', 'ProviderIds': {'Tmdb': '123'}}, 'credits',
                {**DEFAULT_ENRICH_CONFIG, 'ai_enabled': True}, 'key'))
        self.assertEqual([person['Role'] for person in saved[0]['People']], ['饰 警员', '饰 校长'])
        asyncio.run(emby.aclose())
        asyncio.run(tmdb.aclose())

    def test_movie_and_series_share_ai_retry_and_scope_settings(self):
        for media_type in ('电影', '剧集'):
            people = [{'Name': 'English Name', 'Type': 'Actor', 'Role': '饰 English Role'}]
            with patch.object(self.enrichment, '_optional_ai_map', new_callable=AsyncMock,
                              side_effect=[{'n0': '中文 / English', 'r0': '中文 / Role'},
                                           {'n0': '中文姓名', 'r0': '中文角色'}]) as translate:
                asyncio.run(self.enrichment._translate_people(people,
                    {**DEFAULT_ENRICH_CONFIG, 'ai_enabled': True}, '测试媒体', media_type))
                self.assertEqual(translate.await_count, 2)
                self.assertIn(media_type + '：测试媒体', translate.call_args.args[1])
            self.assertEqual(people[0]['Name'], '中文姓名')
            self.assertEqual(people[0]['Role'], '中文角色')
            with patch.object(self.enrichment, '_optional_ai_map', new_callable=AsyncMock) as translate:
                asyncio.run(self.enrichment._translate_people(people,
                    {**DEFAULT_ENRICH_CONFIG, 'ai_enabled': True, 'ai_credits': False}, '测试媒体', media_type))
                translate.assert_not_awaited()

    def test_douban_mobile_replaces_english_movie_cast(self):
        paths = []

        def handler(request):
            paths.append(request.url.path)
            if request.url.path.endswith('/j/subject/18'):
                return httpx.Response(200, json={'title': '蜘蛛侠：崭新之日',
                    'casts': [{'name': 'Tom Holland', 'role': 'Peter Parker', 'img': ''}]})
            if request.url.path.endswith('/celebrities'):
                return httpx.Response(200, json={'actors': [
                    {'name': '汤姆·赫兰德', 'latin_name': 'Tom Holland',
                     'character': '彼得·帕克／蜘蛛侠', 'avatar': {}}],
                    'directors': [{'name': '德斯汀·克里顿', 'latin_name': 'Destin Daniel Cretton',
                                   'avatar': {}}]})
            return httpx.Response(404)

        real_client = httpx.AsyncClient
        with patch('emetools.data_enrichment.httpx.AsyncClient',
                   side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)):
            result = asyncio.run(self.real_douban_data(
                {'Name': '蜘蛛侠：崭新之日', 'ProviderIds': {'Douban': '18'}}, include_cast=True))
        self.assertEqual(result['casts'][0]['name'], '汤姆·赫兰德')
        self.assertEqual(result['casts'][0]['original_name'], 'Tom Holland')
        self.assertEqual(result['casts'][0]['role'], '彼得·帕克／蜘蛛侠')
        self.assertEqual(result['directors'][0]['name'], '德斯汀·克里顿')
        self.assertEqual(paths, ['/j/subject/18', '/rexxar/api/v2/movie/18/celebrities'])

    def test_chinese_title_selection_rejects_foreign_scripts(self):
        for value in ('오징어 게임', '愛の不時着です', '中文한글', 'เด็กใหม่', 'Squid Game', ''):
            with self.subTest(title=value):
                self.assertFalse(DataEnrichment._is_chinese_title(value))
                self.assertEqual(DataEnrichment._select_chinese_title('原有中文剧名', value), '原有中文剧名')
        for value in ('鱿鱼游戏', '魷魚遊戲', '我的 Happy Ending'):
            self.assertTrue(DataEnrichment._is_chinese_title(value))
        self.assertEqual(DataEnrichment._select_chinese_title('오징어 게임', '鱿鱼游戏'), '鱿鱼游戏')

    def test_variety_season_suffix_is_removed_but_other_titles_are_unchanged(self):
        for title, expected in [('奔跑吧兄弟 第一季', '奔跑吧兄弟'),
                                ('半熟恋人 第 1 季', '半熟恋人'),
                                ('综艺节目 第二季', '综艺节目'),
                                ('脱口秀 2024', '脱口秀 2024')]:
            with self.subTest(title=title):
                self.assertEqual(DataEnrichment._enrichment_title(
                    {'Name': title, 'Genres': ['综艺']}), expected)
        self.assertEqual(DataEnrichment._enrichment_title(
            {'Name': '庆余年 第一季', 'Genres': ['剧情']}), '庆余年 第一季')
        self.assertEqual(DataEnrichment._enrichment_title(
            {'Name': '奔跑吧兄弟 第一季', 'Tags': ['Reality']}), '奔跑吧兄弟')

    def test_series_title_keeps_chinese_without_requesting_translations(self):
        with patch.object(self.enrichment, '_tmdb_json', new_callable=AsyncMock) as fetch:
            title = asyncio.run(self.enrichment._series_title(
                object(), '12345', 'test-key', '鱿鱼游戏', '오징어 게임', 'Squid Game'))
        self.assertEqual(title, '鱿鱼游戏')
        fetch.assert_not_awaited()

    def test_series_title_recovers_by_tmdb_id_and_prefers_simplified_chinese(self):
        translations = {'translations': [
            {'iso_639_1': 'ko', 'data': {'name': '오징어 게임'}},
            {'iso_639_1': 'zh', 'iso_3166_1': 'TW', 'data': {'name': '魷魚遊戲'}},
            {'iso_639_1': 'zh', 'iso_3166_1': 'CN', 'data': {'name': '鱿鱼游戏'}}]}
        with patch.object(self.enrichment, '_tmdb_json', new_callable=AsyncMock,
                          return_value=translations) as fetch:
            title = asyncio.run(self.enrichment._series_title(
                object(), '12345', 'test-key', '오징어 게임', None, '오징어 게임'))
        self.assertEqual(title, '鱿鱼游戏')
        self.assertEqual(fetch.call_args.args[1], 'tv/12345/translations')
        translations['translations'].pop()
        with patch.object(self.enrichment, '_tmdb_json', new_callable=AsyncMock, return_value=translations):
            title = asyncio.run(self.enrichment._series_title(
                object(), '12345', 'test-key', '오징어 게임', None, '오징어 게임'))
        self.assertEqual(title, '魷魚遊戲')

    def test_series_title_translation_failure_keeps_existing_title(self):
        with patch.object(self.enrichment, '_tmdb_json', new_callable=AsyncMock,
                          side_effect=ValueError('接口不可用')) as fetch:
            title = asyncio.run(self.enrichment._series_title(
                object(), '12345', 'test-key', 'Original title', None, 'Foreign title'))
        self.assertEqual(title, 'Original title')
        fetch.assert_awaited_once()

    def test_metadata_write_preserves_and_restores_chinese_series_title(self):
        cases = [
            ('鱿鱼游戏', '오징어 게임', '오징어 게임', None, False, '', '鱿鱼游戏'),
            ('鱿鱼游戏', '魷魚遊戲', '오징어 게임', None, False, '', '魷魚遊戲'),
            ('中文剧名', '日本語です', '中文剧名', None, False, '', '中文剧名'),
            ('오징어 게임', '', '오징어 게임', '鱿鱼游戏', False, '', '鱿鱼游戏'),
            ('오징어 게임', '', '오징어 게임', None, True, '鱿鱼游戏', '鱿鱼游戏'),
            ('愛の不時着です', '', '愛の不時着です', None, True, '中文剧名', '中文剧名'),
            ('เด็กใหม่', '', 'เด็กใหม่', None, True, '禁忌女孩', '禁忌女孩'),
            ('오징어 게임', '', '오징어 게임', None, True, '鱿鱼한글', '오징어 게임')]
        for current, douban, tmdb_name, localized, ai_enabled, ai_title, expected in cases:
            with self.subTest(current=current, douban=douban, ai=ai_enabled):
                written = []

                def emby_handler(request):
                    if request.method == 'POST':
                        written.append(json.loads(request.content))
                        return httpx.Response(204)
                    return httpx.Response(200, json={'Id': '73025', 'Type': 'Series', 'Name': current,
                        'ProviderIds': {'Tmdb': '12345'}})

                def tmdb_handler(request):
                    if request.url.path.endswith('/translations'):
                        return httpx.Response(200, json={'translations': [
                            {'iso_639_1': 'zh', 'iso_3166_1': 'CN', 'data': {'name': localized}}]})
                    return httpx.Response(200, json={'name': tmdb_name, 'overview': '中文简介'})

                emby = httpx.AsyncClient(base_url='http://emby/emby/', transport=httpx.MockTransport(emby_handler))
                tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/', transport=httpx.MockTransport(tmdb_handler))
                with patch.object(self.enrichment, '_user_id', new_callable=AsyncMock, return_value='user123'), \
                     patch.object(self.enrichment, '_server', return_value=emby), \
                     patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
                     patch.object(self.enrichment, '_douban_data', new_callable=AsyncMock, return_value={'name': douban}), \
                     patch.object(self.enrichment, '_ai_map', new_callable=AsyncMock,
                                  return_value={'Name': ai_title}) as translate, \
                     patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
                    asyncio.run(self.enrichment._enrich('Q4', '73025', 'metadata', {
                        **DEFAULT_ENRICH_CONFIG, 'metadata_source': 'douban', 'ai_enabled': ai_enabled}))
                self.assertEqual(written[0]['Name'], expected)
                if ai_enabled:
                    self.assertEqual(translate.call_args.args[0]['Name'], current)

    def test_episode_foreign_title_does_not_overwrite_existing_chinese_name(self):
        written = []

        def emby_handler(request):
            if request.method == 'POST':
                written.append(json.loads(request.content))
                return httpx.Response(204)
            if request.url.path.endswith('/Items/9'):
                return httpx.Response(200, json={'Id': '9', 'Name': '第一集：新的开始'})
            if request.url.path.endswith('/Items/73025'):
                return httpx.Response(200, json={'Id': '73025', 'Type': 'Series', 'Name': '中文剧名',
                    'ProviderIds': {'Tmdb': '12345'}})
            return httpx.Response(200, json={'Items': [{'Id': '9', 'ParentIndexNumber': 1, 'IndexNumber': 1}]})

        def tmdb_handler(request):
            if '/season/' in request.url.path:
                return httpx.Response(200, json={'episodes': [
                    {'episode_number': 1, 'name': '새로운 시작', 'overview': '中文剧情'}]})
            return httpx.Response(200, json={'name': '中文剧名'})

        emby = httpx.AsyncClient(base_url='http://emby/emby/', transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/', transport=httpx.MockTransport(tmdb_handler))
        with patch.object(self.enrichment, '_user_id', new_callable=AsyncMock, return_value='user123'), \
             patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
            asyncio.run(self.enrichment._enrich('Q4', '73025', 'episodes', DEFAULT_ENRICH_CONFIG))
        self.assertEqual(written[0]['Name'], '第一集：新的开始')
        self.assertEqual(written[0]['Overview'], '中文剧情')

    def test_episode_web_fallback_fills_only_missing_fields_even_without_tmdb_episodes(self):
        originals = {
            '23': {'Id': '23', 'Name': '第23集', 'Overview': ''},
            '24': {'Id': '24', 'Name': '原有有效标题', 'Overview': ''},
            '25': {'Id': '25', 'Name': 'Episode 25', 'Overview': '原有有效简介。'},
            '26': {'Id': '26', 'Name': '正常标题', 'Overview': '正常简介。'}}
        written = {}
        self.web_plots.return_value = {
            number: {'name': f'第{number}集：网页剧情标题', 'overview': '网页首句。', 'source': '电视猫'}
            for number in (23, 24, 25)}

        def emby_handler(request):
            if request.method == 'POST':
                written[request.url.path.rsplit('/', 1)[1]] = json.loads(request.content)
                return httpx.Response(204)
            if request.url.path.endswith('/Items/series'):
                return httpx.Response(200, json={'Id': 'series', 'Type': 'Series', 'Name': '测试剧',
                    'ProductionYear': 2026, 'ProviderIds': {'Tmdb': '12345'}})
            episode_id = request.url.path.rsplit('/', 1)[1]
            if episode_id in originals:
                return httpx.Response(200, json=originals[episode_id])
            self.assertEqual(request.url.params['Fields'], 'Overview')
            return httpx.Response(200, json={'Items': [
                {**entry, 'IndexNumber': int(key), 'ParentIndexNumber': 1}
                for key, entry in originals.items()]})

        def tmdb_handler(request):
            return httpx.Response(200, json={'episodes': [
                {'episode_number': 23, 'name': 'Episode 23', 'overview': ''},
                {'episode_number': 25, 'name': '새로운 시작', 'overview': ''}]}
                if '/season/' in request.url.path else {'name': '测试剧'})

        emby = httpx.AsyncClient(base_url='http://emby/emby/', transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/', transport=httpx.MockTransport(tmdb_handler))
        with patch.object(self.enrichment, '_user_id', new_callable=AsyncMock, return_value='user123'), \
             patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'), \
             patch('emetools.data_enrichment.asyncio.sleep', new_callable=AsyncMock):
            asyncio.run(self.enrichment._enrich('Q4', 'series', 'episodes', DEFAULT_ENRICH_CONFIG))
        self.web_plots.assert_awaited_once()
        self.assertEqual(self.web_plots.call_args.args[2], {23, 24, 25, 26})
        self.assertEqual(set(written), {'23', '24', '25'})
        self.assertEqual(written['23']['Name'], '第23集：网页剧情标题')
        self.assertEqual(written['23']['Overview'], '网页首句。')
        self.assertEqual(written['24']['Name'], originals['24']['Name'])
        self.assertEqual(written['24']['Overview'], '网页首句。')
        self.assertEqual(written['25']['Name'], '第25集：网页剧情标题')
        self.assertEqual(written['25']['Overview'], originals['25']['Overview'])

    def test_baike_overview_without_caption_is_written_and_existing_title_preserved(self):
        from emetools.episode_plots import parse_baike_episodes

        page_data = {'lemmaTitle': '测试剧', 'lemmaDesc': '2026年电视剧', 'structuredContent': [{
            'plotTitle': {'text': [{'tag': 'text', 'text': '第1集'}]},
            'plotDetail': {'text': [{'tag': 'text', 'text': '百科首句。后续剧情。'}]}}]}
        page = '<title>测试剧_百度百科</title><script id="__NEXT_DATA__">' + json.dumps(
            {'props': {'pageProps': {'pageData': page_data}}}) + '</script>'
        self.web_plots.return_value = {number: {**data, 'source': '百度百科'}
                                       for number, data in parse_baike_episodes(page, '测试剧', '2026').items()}
        written = []

        def handler(request):
            if request.method == 'POST':
                written.append(json.loads(request.content))
                return httpx.Response(204)
            if request.url.path.endswith('/Items/series'):
                return httpx.Response(200, json={'Id': 'series', 'Type': 'Series', 'Name': '测试剧',
                    'ProductionYear': 2026, 'ProviderIds': {'Tmdb': '123'}})
            episode = {'Id': 'episode', 'Name': '原有有效标题', 'Overview': '',
                       'IndexNumber': 1, 'ParentIndexNumber': 1}
            return httpx.Response(200, json=episode if request.url.path.endswith('/Items/episode')
                                  else {'Items': [episode]})

        emby = httpx.AsyncClient(base_url='http://emby/emby/', transport=httpx.MockTransport(handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'name': '测试剧', 'episodes': []})))
        with patch.object(self.enrichment, '_user_id', new_callable=AsyncMock, return_value='user'), \
                patch.object(self.enrichment, '_server', return_value=emby), \
                patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
                patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
            asyncio.run(self.enrichment._enrich('Q4', 'series', 'episodes', DEFAULT_ENRICH_CONFIG))
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]['Overview'], '百科首句。')
        self.assertEqual(written[0]['Name'], '原有有效标题')

    def test_missing_episode_title_is_reported_when_sources_cannot_supply_it(self):
        emby = httpx.AsyncClient(base_url='http://emby/emby/')
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/')
        with patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
             patch.object(self.enrichment, '_user_id', new_callable=AsyncMock, return_value='user'), \
             patch.object(self.enrichment, '_item', side_effect=[
                 {'Id': 'series', 'Name': '测试剧', 'Type': 'Series', 'ProviderIds': {'Tmdb': '12345'}},
                 {'Id': 'ep4', 'Name': '第 4 集', 'Overview': '已有简介。'}]), \
             patch.object(self.enrichment, '_json', new_callable=AsyncMock, return_value={
                 'Items': [{'Id': 'ep4', 'ParentIndexNumber': 1, 'IndexNumber': 4}]}), \
             patch.object(self.enrichment, '_tmdb_json', new_callable=AsyncMock,
                          return_value={'episodes': []}), \
             patch.object(self.enrichment, '_save_item', new_callable=AsyncMock, return_value=False), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
            asyncio.run(self.enrichment._enrich('Q4', 'series', 'episodes', DEFAULT_ENRICH_CONFIG))
        self.assertTrue(any('仍有 1 集缺少有效标题、0 集缺少简介' in line
                            for line in self.enrichment.status()['log']))

    def test_tmdb_client_uses_moviepilot_domain_and_https_proxy(self):
        with patch('emetools.data_enrichment.settings.TMDB_API_DOMAIN', 'api.tmdb.org'), \
             patch('emetools.data_enrichment.get_runtime_setting', return_value={
                 'http': 'http://proxy.example:1080', 'https': 'http://proxy.example:1080'}), \
             patch('emetools.data_enrichment.ReadRetryClient') as client:
            self.enrichment._tmdb_client()
        self.assertEqual(client.call_args.kwargs['base_url'], 'https://api.tmdb.org/3/')
        self.assertEqual(client.call_args.kwargs['proxy'], 'http://proxy.example:1080')
        self.assertFalse(client.call_args.kwargs['trust_env'])

    def test_tmdb_client_does_not_duplicate_api_path(self):
        with patch('emetools.data_enrichment.settings.TMDB_API_DOMAIN',
                   'https://mirror.example/tmdb/3/'), \
             patch('emetools.data_enrichment.get_runtime_setting', return_value=None), \
             patch('emetools.data_enrichment.ReadRetryClient') as client:
            self.enrichment._tmdb_client()
        self.assertEqual(client.call_args.kwargs['base_url'], 'https://mirror.example/tmdb/3/')

    def test_tmdb_not_found_error_points_to_id_or_domain(self):
        async def request():
            async with httpx.AsyncClient(base_url='https://api.tmdb.org/3/',
                                         transport=httpx.MockTransport(
                                             lambda _request: httpx.Response(404))) as client:
                with self.assertRaisesRegex(ValueError, 'TMDB 未找到对应数据') as error:
                    await self.enrichment._tmdb_json(client, 'tv/12345', {'api_key': 'secret-key'})
                self.assertIn('TMDB ID', str(error.exception))
                self.assertNotIn('secret-key', str(error.exception))

        asyncio.run(request())

    def test_metadata_source_validation_and_legacy_default(self):
        self.assertEqual(validate_enrich_config({})['metadata_source'], 'tmdb')
        self.assertTrue(validate_enrich_config({})['auto_on_import'])
        self.assertEqual(validate_enrich_config({'metadata_source': 'douban'})['metadata_source'], 'douban')
        for value in ('both', 'TMDB', None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_enrich_config({'metadata_source': value})

    def test_import_debounce_latest_event_wins_and_retries_busy_worker(self):
        self.enrichment.owner._enabled = True
        self.enrichment.owner._enrichment_config = dict(DEFAULT_ENRICH_CONFIG)
        timers = []

        class FakeTimer:
            def __init__(self, seconds, function, args):
                self.seconds, self.function, self.args = seconds, function, args
                self.cancelled = False
                timers.append(self)
            def start(self): pass
            def cancel(self): self.cancelled = True

        with patch.object(self.enrichment, '_services', return_value={'Q4': object()}), \
             patch('emetools.data_enrichment.threading.Timer', FakeTimer), \
             patch('emetools.data_enrichment.time.time', side_effect=[100, 100, 105, 105, 110, 110, 110, 111, 111]), \
             patch.object(self.enrichment, 'start_enrich') as start:
            self.enrichment.queue_import('Q4::123')
            self.enrichment.queue_import('Q4::123')
            self.assertTrue(timers[0].cancelled)
            self.assertEqual(timers[1].seconds, 300)
            self.assertEqual(self.storage['enrichment_import_pending']['Q4::123'], 405)
            timers[0].function(*timers[0].args)
            start.assert_not_called()
            self.enrichment.state['running'] = True
            timers[1].function(*timers[1].args)
            self.assertEqual(len(timers), 3)
            self.enrichment.state['running'] = False
            timers[2].function(*timers[2].args)
            start.assert_called_once_with('Q4::123', 'all')
            self.assertFalse(self.storage['enrichment_import_pending'])

    def test_import_retries_if_another_task_starts_after_busy_check(self):
        self.enrichment.owner._enabled = True
        self.enrichment.owner._enrichment_config = dict(DEFAULT_ENRICH_CONFIG)
        with patch('emetools.data_enrichment.threading.Timer') as timer, \
             patch('emetools.data_enrichment.time.time', return_value=100), \
             patch.object(self.enrichment, 'start_enrich', side_effect=ValueError(
                 '已有数据补全任务正在运行，请等待完成')):
            self.enrichment._queue_import('Q4::123', 400)
            self.enrichment._run_import('Q4::123', 400)
        self.assertEqual(self.storage['enrichment_import_pending'], {'Q4::123': 130})
        self.assertEqual(timer.call_count, 2)

    def test_recent_import_poll_queues_series_when_events_are_missing(self):
        self.enrichment.owner._enabled = True
        self.enrichment.owner._enrichment_config = dict(DEFAULT_ENRICH_CONFIG)
        self.storage['enrichment_import_poll_after'] = 800

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        response = {'Items': [
            {'Id': 'ep-new', 'SeriesId': 'series-new', 'DateCreated': '1970-01-01T00:15:00Z'},
            {'Id': 'movie-new', 'Type': 'Movie', 'DateCreated': '1970-01-01T00:15:00Z'},
            {'Id': 'ep-old', 'SeriesId': 'series-old', 'DateCreated': '1970-01-01T00:10:00Z'}]}
        with patch.object(self.enrichment, '_services', return_value={'Q4': object()}), \
             patch.object(self.enrichment, '_server', return_value=FakeClient()), \
             patch.object(self.enrichment, '_user_id', new=AsyncMock(return_value='user')), \
             patch.object(self.enrichment, '_json', new=AsyncMock(return_value=response)) as fetch, \
             patch.object(self.enrichment, 'queue_import') as queued, \
             patch('emetools.data_enrichment.time.time', return_value=1000):
            self.enrichment.poll_recent_imports()
        self.assertEqual({entry.args[0] for entry in queued.call_args_list},
                         {'Q4::series-new', 'Q4::movie-new'})
        self.assertEqual(self.storage['enrichment_import_poll_after'], 1000)
        self.assertEqual(fetch.call_args.args[1], 'Users/user/Items')
        self.assertEqual(fetch.call_args.args[2]['IncludeItemTypes'], 'Episode,Movie')

    def test_tmdb_import_resolution_filters_movie_namespace(self):
        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
        with patch.object(self.enrichment, '_services', return_value={'Q4': object()}), \
             patch.object(self.enrichment, '_server', return_value=FakeClient()), \
             patch.object(self.enrichment, '_user_id', new_callable=AsyncMock, return_value='user'), \
             patch.object(self.enrichment, '_json', new_callable=AsyncMock,
                          return_value={'Items': [{'Id': 'movie1'}]}) as fetch, \
             patch.object(self.enrichment, 'queue_import') as queued:
            self.enrichment._resolve_tmdb_import('969681', 'Movie')
        self.assertEqual(fetch.call_args.args[2]['IncludeItemTypes'], 'Movie')
        queued.assert_called_once_with('Q4::movie1')

    def test_batch_enrich_continues_after_failure(self):
        async def enrich(name, identifier, mode, options):
            if identifier == '2':
                raise ValueError('series failure')
        with patch.object(self.enrichment, '_enrich', side_effect=enrich):
            asyncio.run(self.enrichment._batch_enrich(['Q4::1', 'Q4::2', 'Q4::3'],
                                                     DEFAULT_ENRICH_CONFIG))
        self.assertEqual(self.enrichment.status()['batch_result'], {'ok': 2, 'fail': 1})

    def test_batch_enrich_logs_failure_detail(self):
        async def enrich(name, identifier, mode, options):
            raise ValueError('TMDB 连接失败：请检查代理设置')

        with patch.object(self.enrichment, '_enrich', side_effect=enrich):
            asyncio.run(self.enrichment._batch_enrich(['Q4::1'], DEFAULT_ENRICH_CONFIG))
        self.assertIn('ValueError：TMDB 连接失败：请检查代理设置', self.enrichment.state['log'][-2])

    def test_optional_ai_failure_keeps_enrichment_running(self):
        with patch.object(self.enrichment, '_ai_map', new_callable=AsyncMock,
                          side_effect=ValueError('AI 补齐已启用，请先在 MoviePilot 配置 LLM 服务')):
            result = asyncio.run(self.enrichment._optional_ai_map({'Name': '외국剧名'}, '剧集标题'))
        self.assertEqual(result, {})
        self.assertIn('保留原始资料继续补全', self.enrichment.state['log'][-1])

    def test_tv_libraries_exposes_series_and_movie_libraries(self):
        instance = SimpleNamespace(get_librarys=lambda hidden=False: [
            SimpleNamespace(id='11', name='电视剧', type='电视剧'),
            SimpleNamespace(id='12', name='电影', type='电影')])
        service = SimpleNamespace(config=SimpleNamespace(name='Q4'), instance=instance)
        with patch('emetools.data_enrichment.MediaServerHelper') as helper, \
             patch('emetools.data_enrichment.MediaType') as media_type:
            helper.return_value.get_services.return_value = {'Q4': service}
            media_type.TV.value = '电视剧'
            media_type.MOVIE.value = '电影'
            self.assertEqual(self.enrichment.tv_libraries(), [
                {'id': 'Q4::11', 'name': '电视剧', 'server': 'Q4', 'type': 'Series'},
                {'id': 'Q4::12', 'name': '电影', 'server': 'Q4', 'type': 'Movie'}])

    def test_batch_library_pages_all_series_without_first_page_limit(self):
        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        async def items(client, path, params):
            self.assertEqual(params['ParentId'], '11')
            self.assertEqual(params['IncludeItemTypes'], 'Series')
            start = params['StartIndex']
            page = range(start, min(start + 500, 1201))
            return {'Items': [{'Id': str(index)} for index in page], 'TotalRecordCount': 1201}

        with patch.object(self.enrichment, '_server', return_value=FakeClient()), \
             patch.object(self.enrichment, '_user_id', new=AsyncMock(return_value='user')), \
             patch.object(self.enrichment, '_json', side_effect=items):
            ids = asyncio.run(self.enrichment._library_series([
                {'id': 'Q4::11', 'server': 'Q4'}]))
        self.assertEqual(len(ids), 1201)
        self.assertEqual(ids[-1], 'Q4::1200')

    def test_movie_library_queries_movie_items_only(self):
        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        async def items(client, path, params):
            self.assertEqual(params['IncludeItemTypes'], 'Movie')
            return {'Items': [{'Id': 'movie1'}], 'TotalRecordCount': 1}

        with patch.object(self.enrichment, '_server', return_value=FakeClient()), \
             patch.object(self.enrichment, '_user_id', new=AsyncMock(return_value='user')), \
             patch.object(self.enrichment, '_json', side_effect=items):
            identifiers = asyncio.run(self.enrichment._library_series(
                [{'id': 'Q4::12', 'type': 'Movie'}]))
        self.assertEqual(identifiers, ['Q4::movie1'])

    def test_library_series_collects_titles_for_preview_logs(self):
        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        names = {}
        with patch.object(self.enrichment, '_server', return_value=FakeClient()), \
             patch.object(self.enrichment, '_user_id', new=AsyncMock(return_value='user')), \
             patch.object(self.enrichment, '_json', new=AsyncMock(return_value={
                 'Items': [{'Id': '14595', 'Name': '测试剧集'}], 'TotalRecordCount': 1})):
            ids = asyncio.run(self.enrichment._library_series([{'id': 'Q4::11'}], names))
        self.assertEqual(ids, ['Q4::14595'])
        self.assertEqual(names, {'Q4::14595': '测试剧集'})

    def test_library_batch_rejects_invalid_or_movie_library(self):
        with patch.object(self.enrichment, 'tv_libraries', return_value=[
            {'id': 'Q4::11', 'name': '电视剧', 'server': 'Q4'}]):
            for ids in ([], ['Q4::12'], ['Q4::11', 'Q4::11']):
                with self.subTest(ids=ids), self.assertRaises(ValueError):
                    self.enrichment._selected_tv_libraries(ids)

    def test_trigger_keeper_thumbnail_task(self):
        class Client:
            async def post(self, path):
                self.path = path
                return SimpleNamespace(raise_for_status=lambda: None)

        client = Client()
        responses = iter([
            [{"Id": "keeper-task", "Name": "MediaInfoKeeper - Refresh Recent Metadata", "State": "Idle"}],
            {"State": "Completed"},
        ])
        with patch.object(self.enrichment, '_json', side_effect=lambda *_args, **_kwargs: next(responses)), \
             patch('emetools.data_enrichment.asyncio.sleep', new=AsyncMock()):
            asyncio.run(self.enrichment._trigger_keeper_thumbnails(client))
        self.assertEqual(client.path, 'ScheduledTasks/Running/keeper-task')

    def test_batch_preview_unifies_missing_color_size_and_pending_repairs(self):
        async def scan(series_id):
            statuses = ['missing', 'candidate', 'size', 'pending', 'keep', 'fixed']
            self.enrichment._preview_selection = {
                f'{series_id}::ep{index}': {'path': str(self.strm), 'status': status}
                for index, status in enumerate(statuses, 1)}
            return [{'id': key, 'status': target['status']}
                    for key, target in self.enrichment._preview_selection.items()]

        async def repair(series_id, targets, force):
            self.assertEqual([key for key, _ in targets],
                             [f'{series_id}::ep{index}' for index in range(1, 5)])
            self.assertEqual([target['status'] for _, target in targets],
                             ['missing', 'candidate', 'size', 'pending'])
            self.assertFalse(force)
            self.enrichment.state['preview_result'] = {'ok': len(targets), 'fail': 0}

        with patch.object(self.enrichment, 'scan_preview', side_effect=scan), \
             patch.object(self.enrichment, '_repair_preview', side_effect=repair):
            asyncio.run(self.enrichment._batch_preview(['Q4::1', 'Q4::2']))
        self.assertEqual(self.enrichment.status()['batch_result'],
                         {'scanned': 12, 'ok': 8, 'fail': 0})
        self.assertTrue(any('Emby 剧集条目 ID 1' in line for line in self.enrichment.status()['log']))

    def test_all_mode_writes_series_ids_studios_and_episode_people(self):
        written = {}

        def emby_handler(request):
            if request.method == 'POST':
                written[request.url.path] = json.loads(request.content)
                return httpx.Response(204)
            if request.url.path.endswith('/Items/73025'):
                return httpx.Response(200, json={'Id': '73025', 'Type': 'Series',
                    'Name': '旧剧名', 'ProviderIds': {'Tmdb': '12345', 'Douban': '88'}})
            if request.url.path.endswith('/Items/ep1'):
                return httpx.Response(200, json={'Id': 'ep1', 'Name': '旧分集'})
            if request.url.path.endswith('/Items'):
                return httpx.Response(200, json={'Items': [{'Id': 'ep1', 'IndexNumber': 1,
                    'ParentIndexNumber': 1}]})
            return httpx.Response(404)

        def tmdb_handler(request):
            if '/season/' in request.url.path:
                return httpx.Response(200, json={'episodes': [{'episode_number': 1,
                    'name': '第一集', 'overview': '剧情简介'}]})
            return httpx.Response(200, json={'name': '新剧名', 'overview': '整剧简介',
                'vote_average': 8.2, 'genres': [{'name': '剧情'}],
                'networks': [{'name': '电视台'}],
                'production_companies': [{'name': '制作公司'}, {'name': '电视台'}],
                'external_ids': {'imdb_id': 'tt12345', 'tvdb_id': 987},
                'aggregate_credits': {'cast': [{'name': '演员甲', 'profile_path': '/a.jpg',
                    'roles': [{'character': '角色甲'}]}]}})

        emby = httpx.AsyncClient(base_url='http://emby/emby/',
                                 transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/',
                                 transport=httpx.MockTransport(tmdb_handler))
        with patch.object(self.enrichment, '_services', return_value={
                 'Q4': SimpleNamespace(get_user=lambda: 'user123')}), \
             patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
             patch.object(self.enrichment, '_douban_data', return_value={}), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
            asyncio.run(self.enrichment._enrich('Q4', '73025', 'all',
                {**DEFAULT_ENRICH_CONFIG, 'episode_cast': True}))
        series = written['/emby/Items/73025']
        episode = written['/emby/Items/ep1']
        self.assertEqual(series['Name'], '新剧名')
        self.assertEqual(series['Overview'], '整剧简介')
        self.assertEqual(series['CommunityRating'], 8.2)
        self.assertEqual(series['Genres'], ['剧情'])
        self.assertEqual(series['Studios'], [{'Name': '电视台'}, {'Name': '制作公司'}])
        self.assertEqual(series['ProviderIds'], {
            'Tmdb': '12345', 'Douban': '88', 'Imdb': 'tt12345', 'Tvdb': '987'})
        self.assertEqual(episode['People'], series['People'])
        self.assertEqual(episode['Name'], '旧分集')
        self.assertEqual(episode['Overview'], '剧情简介')

    def test_batch_enrichment_uses_overall_variety_title_and_actual_emby_season(self):
        written = {}
        search_queries = []
        season_paths = []

        def emby_handler(request):
            if request.method == 'POST':
                written[request.url.path] = json.loads(request.content)
                return httpx.Response(204)
            if request.url.path.endswith('/Items/variety1'):
                return httpx.Response(200, json={'Id': 'variety1', 'Type': 'Series',
                    'Name': '奔跑吧兄弟 第一季', 'Genres': ['综艺'], 'ProductionYear': 2014,
                    'ProviderIds': {}})
            if request.url.path.endswith('/Items'):
                return httpx.Response(200, json={'Items': [{'Id': 'ep1', 'IndexNumber': 1,
                    'ParentIndexNumber': 2}]})
            if request.url.path.endswith('/Items/ep1'):
                return httpx.Response(200, json={'Id': 'ep1', 'Name': '旧分集'})
            return httpx.Response(404)

        emby = httpx.AsyncClient(base_url='http://emby/emby/',
                                 transport=httpx.MockTransport(emby_handler))

        def tmdb_handler(request):
            if request.url.path.endswith('/search/tv'):
                search_queries.append(request.url.params['query'])
                return httpx.Response(200, json={'results': [
                    {'id': 222, 'name': '奔跑吧', 'first_air_date': '2014-01-01'}]})
            if '/season/' in request.url.path:
                season_paths.append(request.url.path)
                return httpx.Response(200, json={'episodes': [
                    {'episode_number': 1, 'name': '第二季第一集', 'overview': '本季内容'}]})
            return httpx.Response(200, json={'name': '奔跑吧', 'overview': '综艺简介',
                'genres': [{'name': '真人秀'}], 'aggregate_credits': {'cast': []},
                'external_ids': {}})

        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/',
                                 transport=httpx.MockTransport(tmdb_handler))
        with patch.object(self.enrichment, '_services', return_value={
                 'Q4': SimpleNamespace(get_user=lambda: 'user123')}), \
             patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
             patch.object(self.enrichment, '_douban_data', return_value={}), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
            asyncio.run(self.enrichment._enrich('Q4', 'variety1', 'all', DEFAULT_ENRICH_CONFIG))

        self.assertEqual(search_queries, ['奔跑吧兄弟'])
        self.assertEqual(season_paths, ['/3/tv/222/season/2'])
        self.assertEqual(written['/emby/Items/variety1']['Name'], '奔跑吧兄弟')
        self.assertEqual(written['/emby/Items/ep1']['Name'], '第二季第一集')
        self.assertEqual(written['/emby/Items/ep1']['Overview'], '本季内容')

    def test_douban_match_requires_unambiguous_title_season_and_year(self):
        candidate = {'id': '123', 'title': '庆余年 第二季', 'year': '2024'}
        match = self.enrichment._douban_match
        self.assertEqual(match([candidate], '庆余年', '2024', 2), candidate)
        self.assertIsNone(match([candidate], '庆余年', '2024', 1))
        self.assertIsNone(match([candidate], '庆余年', '2019', 2))
        self.assertIsNone(match([candidate], '另一个剧集', '2024', 2))
        self.assertIsNone(match([candidate, candidate], '庆余年', '2024', 2))

    def test_douban_data_uses_matched_subject_and_episode(self):
        paths = []

        def handler(request):
            paths.append(request.url.path)
            if request.url.path.endswith('subject_suggest'):
                return httpx.Response(200, json=[{'id': '18', 'title': '庆余年', 'year': '2019'}])
            if request.url.path.endswith('/j/subject/18'):
                return httpx.Response(200, json={'title': '庆余年', 'intro': '豆瓣简介'})
            return httpx.Response(200, json={'episodes': [{'episode': 1, 'title': '第一集', 'desc': '豆瓣分集'}]})

        real_client = httpx.AsyncClient
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch('emetools.data_enrichment.httpx.AsyncClient', side_effect=client):
            item = {'Name': '庆余年', 'ProductionYear': 2019}
            self.assertEqual(asyncio.run(self.real_douban_data(item))['overview'], '豆瓣简介')
            self.assertEqual(asyncio.run(self.real_douban_data(item, 1))[1]['name'], '第一集')
        self.assertIn('/j/subject/18', paths)
        self.assertIn('/j/tv/series/18', paths)

    def test_douban_credits_uses_emby_provider_id_and_mobile_fallback(self):
        paths = []

        def handler(request):
            paths.append(request.url.path)
            if request.url.path.endswith('/j/subject/18'):
                return httpx.Response(200, json={'title': '一瓯春', 'casts': []})
            if request.url.path.endswith('/celebrities'):
                return httpx.Response(200, json={'actors': [{'name': '许凯', 'character': '演员',
                    'avatar': {'normal': 'https://img.example/avatar.jpg'}}]})
            return httpx.Response(500)

        real_client = httpx.AsyncClient
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch('emetools.data_enrichment.httpx.AsyncClient', side_effect=client):
            result = asyncio.run(self.real_douban_data(
                {'Name': '一瓯春', 'ProviderIds': {'Douban': '18'}}, include_cast=True))
        self.assertEqual(result['casts'][0]['name'], '许凯')
        self.assertEqual(paths, ['/j/subject/18', '/rexxar/api/v2/movie/18/celebrities'])

    def test_douban_tmdb_cast_merge_prefers_chinese_and_real_role(self):
        douban = [{'name': '许凯', 'role': '演员', 'img': ''},
                  {'name': '周也', 'role': '饰 叶昭', 'img': 'https://img.example/zhou'}]
        tmdb = [{'name': 'Xu Kai', 'original_name': 'Xu Kai', 'role': 'Shen Run / Yan Rui',
                 'profile_path': '/xu.jpg', 'order': 0},
                {'name': 'Zhou Ye', 'role': 'Old English Role', 'profile_path': '/zhou.jpg', 'order': 1},
                {'name': 'Li Meiyan', 'role': 'Third', 'profile_path': '/li.jpg', 'order': 2}]
        merged = self.enrichment._merge_cast(douban, tmdb)
        self.assertEqual([person['name'] for person in merged], ['许凯', '周也', 'Li Meiyan'])
        self.assertEqual(merged[0]['role'], 'Shen Run / Yan Rui')
        self.assertEqual(merged[0]['profile_path'], '/xu.jpg')
        self.assertEqual(merged[1]['role'], '饰 叶昭')
        # Two Chinese actors with the same pinyin must not be guessed as the
        # same person: an ambiguous match is left intact.
        ambiguous = self.enrichment._merge_cast([
            {'name': '张伟'}, {'name': '章伟'}], [{'name': 'Zhang Wei', 'role': 'X'}])
        self.assertEqual(len(ambiguous), 3)

    def test_tvmao_fills_only_confident_actor_matches(self):
        current = [{'name': 'Li Jian', 'original_name': 'Li Jian',
                    'role': 'Temujin', 'profile_path': '/picture.jpg', 'order': 0},
                   {'name': 'Zhang Yao', 'role': '女侠', 'profile_path': '/zhang.jpg', 'order': 1}]
        web = [{'name': '李健', 'role': '铁木真', 'img': ''},
               {'name': '张瑶', 'role': '阿瑶', 'img': ''},
               {'name': '周也', 'role': '公主', 'img': '/zhou.jpg'}]
        merged, renamed, added = self.enrichment._merge_tvmao_cast(current, web)
        self.assertEqual((renamed, added), (2, 1))
        self.assertEqual([person['name'] for person in merged], ['李健', '张瑶', '周也'])
        self.assertEqual([person['role'] for person in merged], ['铁木真', '女侠', '公主'])
        self.assertEqual(merged[0]['profile_path'], '/picture.jpg')
        ambiguous = self.enrichment._merge_tvmao_cast([
            {'name': 'Zhang Wei', 'role': ''}, {'name': 'Zhang Wei', 'role': ''}],
            [{'name': '张伟', 'role': '甲'}])
        self.assertEqual(ambiguous[0][0]['name'], 'Zhang Wei')
        self.assertEqual(ambiguous[0][1]['name'], 'Zhang Wei')
        self.assertEqual(ambiguous[2], 0)
        oversized = [{'name': f'English Actor {i}', 'role': 'Unknown', 'profile_path': '/a.jpg',
                      'order': i} for i in range(50)]
        prioritized = self.enrichment._merge_tvmao_cast(
            oversized, [{'name': '李健', 'role': '铁木真', 'img': '/li.jpg'}])
        self.assertEqual(prioritized[0][0]['name'], '李健')
        self.assertEqual(prioritized[2], 1)

    def test_credits_uses_tvmao_only_when_both_sources_need_chinese(self):
        written, calls = [], []

        def emby_handler(request):
            if request.method == 'POST':
                written.append(json.loads(request.content))
                return httpx.Response(204)
            return httpx.Response(200, json={'Id': '73025', 'Type': 'Series', 'Name': '征途',
                                               'ProviderIds': {'Tmdb': '12345'}})

        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/',
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={
                'aggregate_credits': {'cast': [{'name': 'Li Jian', 'profile_path': '/li.jpg',
                                                'roles': [{'character': 'Temujin'}]}]}})))
        emby = httpx.AsyncClient(base_url='http://emby/emby/', transport=httpx.MockTransport(emby_handler))

        async def douban(_item, include_cast=False):
            calls.append('douban')
            return {'casts': [{'name': 'Li Jian', 'role': '', 'img': ''}]}

        async def tvmao(_item):
            calls.append('tvmao')
            return [{'name': '李健', 'role': '铁木真', 'img': ''}]

        with patch.object(self.enrichment, '_services', return_value={
                 'Q4': SimpleNamespace(get_user=lambda: 'user123')}), \
             patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
             patch.object(self.enrichment, '_douban_data', side_effect=douban), \
             patch.object(self.enrichment, '_tvmao_cast', side_effect=tvmao), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
            asyncio.run(self.enrichment._enrich('Q4', '73025', 'credits', DEFAULT_ENRICH_CONFIG))
        self.assertEqual(calls, ['douban', 'tvmao'])
        self.assertEqual(written[0]['People'][0]['Name'], '李健')
        self.assertEqual(written[0]['People'][0]['Role'], '饰 铁木真')

    def test_douban_credits_action_writes_chinese_names_and_ai_roles(self):
        saved = []

        def emby_handler(request):
            if request.method == 'POST':
                saved.append(json.loads(request.content))
                return httpx.Response(204)
            return httpx.Response(200, json={'Id': '73025', 'Type': 'Series', 'Name': '一瓯春',
                                               'ProviderIds': {'Tmdb': '12345', 'Douban': '18'}})

        def tmdb_handler(_request):
            return httpx.Response(200, json={'aggregate_credits': {'cast': [
                {'name': 'Xu Kai', 'profile_path': '/xu.jpg', 'order': 0,
                 'roles': [{'character': 'Shen Run / Yan Rui'}]}]}})

        async def douban_data(_item, include_cast=False):
            return {'casts': [{'name': '许凯', 'role': '演员', 'img': ''}]}

        async def ai_map(mapping, _context):
            return {key: '沈润／晏睿' for key in mapping}

        emby = httpx.AsyncClient(base_url='http://emby/emby/', transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/', transport=httpx.MockTransport(tmdb_handler))
        with patch.object(self.enrichment, '_services', return_value={
                 'Q4': SimpleNamespace(get_user=lambda: 'user123')}), \
             patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
             patch.object(self.enrichment, '_douban_data', side_effect=douban_data) as fetched, \
             patch.object(self.enrichment, '_tvmao_cast', return_value=[]), \
             patch.object(self.enrichment, '_ai_map', side_effect=ai_map) as translated, \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
            asyncio.run(self.enrichment._enrich('Q4', '73025', 'credits',
                {**DEFAULT_ENRICH_CONFIG, 'metadata_source': 'douban', 'ai_enabled': True}))
        fetched.assert_called_once()
        translated.assert_called_once()
        self.assertEqual(len(saved[0]['People']), 1)
        self.assertEqual(saved[0]['People'][0]['Name'], '许凯')
        self.assertEqual(saved[0]['People'][0]['Role'], '饰 沈润／晏睿')

    def test_ai_translates_english_role_even_when_douban_added_chinese_prefix(self):
        saved, ai_inputs = [], []

        def emby_handler(request):
            if request.method == 'POST':
                saved.append(json.loads(request.content))
                return httpx.Response(204)
            return httpx.Response(200, json={'Id': '73025', 'Type': 'Series', 'Name': '一瓯春',
                                               'ProviderIds': {'Tmdb': '12345'}})

        async def ai_map(mapping, _context):
            ai_inputs.append(mapping)
            return {'r0': '饰 沈润'}

        emby = httpx.AsyncClient(base_url='http://emby/emby/', transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/',
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={
                'aggregate_credits': {'cast': [{'name': '许凯', 'profile_path': '/pic.jpg',
                                                'roles': [{'character': '饰 Shen Run'}]}]}})))
        with patch.object(self.enrichment, '_services', return_value={
                 'Q4': SimpleNamespace(get_user=lambda: 'user123')}), \
             patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
             patch.object(self.enrichment, '_douban_data', return_value={}), \
             patch.object(self.enrichment, '_tvmao_cast', return_value=[]), \
             patch.object(self.enrichment, '_ai_map', side_effect=ai_map), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
            asyncio.run(self.enrichment._enrich('Q4', '73025', 'credits',
                {**DEFAULT_ENRICH_CONFIG, 'ai_enabled': True}))
        self.assertEqual(ai_inputs, [{'r0': '饰 Shen Run'}])
        self.assertEqual(saved[0]['People'][0]['Role'], '饰 沈润')

    def test_ai_retries_mixed_english_cast_and_preserves_untranslated_values(self):
        saved, requests = [], []

        def emby_handler(request):
            if request.method == 'POST':
                saved.append(json.loads(request.content))
                return httpx.Response(204)
            return httpx.Response(200, json={'Id': '73025', 'Type': 'Series',
                                               'Name': '测试美剧', 'ProviderIds': {'Tmdb': '12345'}})

        async def ai_map(mapping, context):
            requests.append(dict(mapping))
            self.assertIn('不得保留英文字母', context)
            if len(requests) == 1:
                return {'n0': '中文名 / English Name', 'r0': '中文角色 / English Role',
                        'n1': '约翰', 'r1': '主角'}
            return {'n0': '中文名', 'r0': '中文角色'}

        cast = [{'name': '中文名 / English Name', 'profile_path': '/one.jpg', 'order': 0,
                 'roles': [{'character': '饰 English Role'}]},
                {'name': 'John', 'profile_path': '/two.jpg', 'order': 1,
                 'roles': [{'character': 'English Role'}]}]
        emby = httpx.AsyncClient(base_url='http://emby/emby/',
                                 transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/', transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={'aggregate_credits': {'cast': cast}})))
        with patch.object(self.enrichment, '_services', return_value={
                 'Q4': SimpleNamespace(get_user=lambda: 'user123')}), \
             patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
             patch.object(self.enrichment, '_douban_data', return_value={}), \
             patch.object(self.enrichment, '_tvmao_cast', return_value=[]), \
             patch.object(self.enrichment, '_ai_map', side_effect=ai_map), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
            asyncio.run(self.enrichment._enrich('Q4', '73025', 'credits',
                                                {**DEFAULT_ENRICH_CONFIG, 'ai_enabled': True}))
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1], {'n0': '中文名 / English Name', 'r0': '饰 English Role'})
        self.assertEqual([person['Name'] for person in saved[0]['People']], ['中文名', '约翰'])
        self.assertEqual([person['Role'] for person in saved[0]['People']], ['饰 中文角色', '饰 主角'])

    def test_tmdb_connection_failure_is_actionable_and_hides_key(self):
        async def request():
            def fail(_request):
                raise httpx.ConnectError('no route to host')

            async with httpx.AsyncClient(base_url='https://api.tmdb.org/3/',
                                         transport=httpx.MockTransport(fail)) as client:
                with self.assertRaisesRegex(ValueError, 'TMDB 连接失败') as error:
                    await self.enrichment._tmdb_json(client, 'tv/12345', {'api_key': 'secret-key'})
                self.assertNotIn('secret-key', str(error.exception))

        asyncio.run(request())

    def test_settings_validate_boolean_and_actor_limits(self):
        config = validate_enrich_config({'no_avatar': False, 'max_actors': 12, 'cast_lock_min': 6})
        self.assertFalse(config['no_avatar'])
        self.assertEqual(config['max_actors'], 12)
        self.assertFalse(config['ai_enabled'])
        for invalid in ({'max_actors': 0}, {'cast_lock_min': '10'}, {'ai_enabled': 'false'},
                        {'unknown': True}, {'max_actors': True}):
            with self.assertRaises(ValueError):
                validate_enrich_config(invalid)

    def test_ai_translation_uses_moviepilot_llm_only_when_requested(self):
        with patch('emetools.data_enrichment.llm_available', return_value=True), \
             patch('emetools.data_enrichment.llm_setting', return_value='test-only'), \
             patch('emetools.data_enrichment.translate_with_moviepilot', new_callable=AsyncMock,
                   return_value='```json\n{"Name":"中文剧名"}\n```') as translate:
            response = asyncio.run(self.enrichment._ai_map({'Name': 'English name'}, '剧集标题'))
        self.assertEqual(response, {'Name': '中文剧名'})
        translate.assert_awaited_once()
        self.assertIn('English name', translate.call_args.args[0][1]['content'])

    def test_cast_settings_filter_limit_prefix_and_lock(self):
        saved = []

        def emby_handler(request):
            if request.url.path == '/emby/Users/user123/Items/73025':
                return httpx.Response(200, json={'Id': '73025', 'Type': 'Series', 'Name': '测试剧集',
                                                  'ProviderIds': {'Tmdb': '12345'}})
            if request.method == 'POST' and request.url.path == '/emby/Items/73025':
                saved.append(json.loads(request.content))
                return httpx.Response(204)
            return httpx.Response(404)

        def tmdb_handler(request):
            return httpx.Response(200, json={'name': '测试剧集', 'aggregate_credits': {'cast': [
                {'name': '甲演员', 'profile_path': '/pic.jpg', 'order': 1, 'roles': [{'character': '主角'}]},
                {'name': '乙演员', 'profile_path': None, 'order': 2, 'roles': [{'character': '配角'}]},
                {'name': '丙演员', 'profile_path': '/pic2.jpg', 'order': 3, 'roles': [{'character': '同事'}]}]}})

        emby = httpx.AsyncClient(base_url='http://emby/emby/', transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/', transport=httpx.MockTransport(tmdb_handler))
        options = {**DEFAULT_ENRICH_CONFIG, 'max_actors': 2, 'cast_lock_min': 2}
        with patch.object(self.enrichment, '_services', return_value={'Q4': SimpleNamespace(get_user=lambda: 'user123')}), \
             patch.object(self.enrichment, '_server', return_value=emby), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb):
            asyncio.run(self.enrichment._enrich('Q4', '73025', 'credits', options))
        self.assertEqual([person['Name'] for person in saved[0]['People']], ['甲演员', '丙演员'])
        self.assertEqual(saved[0]['People'][0]['Role'], '饰 主角')
        self.assertIn('Cast', saved[0]['LockedFields'])

    def test_credits_action_syncs_episode_people_when_enabled(self):
        saved, queried = {}, []

        def emby_handler(request):
            if request.method == 'POST':
                saved[request.url.path] = json.loads(request.content)
                return httpx.Response(204)
            if request.url.path == '/emby/Users/user123/Items/73025':
                return httpx.Response(200, json={'Id': '73025', 'Type': 'Series', 'Name': '一瓯春',
                                                 'ProviderIds': {'Tmdb': '12345'}})
            if request.url.path == '/emby/Users/user123/Items':
                queried.append(request.url.params.get('StartIndex'))
                offset = int(request.url.params['StartIndex'])
                pages = {0: [{'Id': 'ep1'}], 1: [{'Id': 'ep2'}]}
                return httpx.Response(200, json={'Items': pages.get(offset, []),
                                                 'TotalRecordCount': 2})
            if request.url.path.startswith('/emby/Users/user123/Items/ep'):
                return httpx.Response(200, json={'Id': request.url.path.rsplit('/', 1)[-1],
                                                 'Name': '原有标题', 'Overview': '原有简介',
                                                 'People': [{'Name': '旧演员'}]})
            return httpx.Response(404)

        emby = httpx.AsyncClient(base_url='http://emby/emby/',
                                 transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/',
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={
                'aggregate_credits': {'cast': [{'name': '许凯', 'profile_path': '/pic.jpg',
                                                'roles': [{'character': '谢府公子'}]}]}})))
        with patch.object(self.enrichment, '_services', return_value={
                 'Q4': SimpleNamespace(get_user=lambda: 'user123')}), \
             patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
             patch.object(self.enrichment, '_douban_data', return_value={}), \
             patch.object(self.enrichment, '_tvmao_cast', return_value=[]), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
            asyncio.run(self.enrichment._enrich('Q4', '73025', 'credits',
                {**DEFAULT_ENRICH_CONFIG, 'episode_cast': True}))
        self.assertEqual(queried, ['0', '1'])
        self.assertEqual(len(saved), 3)
        for episode_id in ('ep1', 'ep2'):
            episode = saved[f'/emby/Items/{episode_id}']
            self.assertEqual(episode['People'], saved['/emby/Items/73025']['People'])
            self.assertEqual(episode['Name'], '原有标题')
            self.assertEqual(episode['Overview'], '原有简介')
        self.assertIn('分集演职人员同步完成：2 集', self.enrichment.status()['log'])

    def test_credits_action_does_not_sync_episodes_when_disabled(self):
        paths = []

        def emby_handler(request):
            paths.append((request.method, request.url.path))
            if request.method == 'POST':
                return httpx.Response(204)
            if request.url.path.endswith('/Items/73025'):
                return httpx.Response(200, json={'Id': '73025', 'Type': 'Series', 'Name': '一瓯春',
                                                 'ProviderIds': {'Tmdb': '12345'}})
            return httpx.Response(500)

        emby = httpx.AsyncClient(base_url='http://emby/emby/',
                                 transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/',
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={
                'aggregate_credits': {'cast': [{'name': '许凯', 'profile_path': '/pic.jpg'}]}})))
        with patch.object(self.enrichment, '_services', return_value={
                 'Q4': SimpleNamespace(get_user=lambda: 'user123')}), \
             patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'):
            asyncio.run(self.enrichment._enrich('Q4', '73025', 'credits',
                                                {**DEFAULT_ENRICH_CONFIG, 'episode_cast': False}))
        self.assertEqual([path for method, path in paths if method == 'POST'],
                         ['/emby/Items/73025'])

    def test_douban_preferred_fields_and_tmdb_fallback_reach_emby(self):
        saved = []

        def emby_handler(request):
            if request.method == 'POST':
                saved.append(json.loads(request.content))
                return httpx.Response(204)
            if request.url.path.endswith('/Items/73025'):
                return httpx.Response(200, json={'Id': '73025', 'Type': 'Series', 'Name': '庆余年',
                                                  'ProviderIds': {'Tmdb': '12345'}})
            if request.url.path.endswith('/Items'):
                return httpx.Response(200, json={'Items': [{'Id': 'ep1', 'ParentIndexNumber': 1, 'IndexNumber': 1}]})
            if request.url.path.endswith('/Items/ep1'):
                return httpx.Response(200, json={'Id': 'ep1', 'Name': '旧分集'})
            return httpx.Response(404)

        def tmdb_handler(request):
            if '/season/' in request.url.path:
                return httpx.Response(200, json={'episodes': [{'episode_number': 1, 'name': 'TMDB 标题',
                                                               'overview': 'TMDB 分集简介'}]})
            return httpx.Response(200, json={'name': 'TMDB 标题', 'overview': 'TMDB 简介',
                                              'genres': [{'name': '剧情'}]})

        async def douban_data(_item, season=None, include_cast=False):
            return ({1: {'name': '豆瓣标题', 'overview': ''}} if season else
                    {'name': '豆瓣剧名', 'overview': '豆瓣简介'})

        emby = httpx.AsyncClient(base_url='http://emby/emby/', transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.tmdb.org/3/', transport=httpx.MockTransport(tmdb_handler))
        with patch.object(self.enrichment, '_services', return_value={'Q4': SimpleNamespace(get_user=lambda: 'user123')}), \
             patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_douban_data', side_effect=douban_data) as fetched, \
             patch.object(self.enrichment, '_tvmao_cast', return_value=[]), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb):
            asyncio.run(self.enrichment._enrich('Q4', '73025', 'all',
                                                {**DEFAULT_ENRICH_CONFIG, 'metadata_source': 'douban'}))
        self.assertEqual(fetched.call_count, 2)
        self.assertEqual(saved[0]['Name'], '豆瓣剧名')
        self.assertEqual(saved[0]['Overview'], '豆瓣简介')
        self.assertEqual(saved[0]['Genres'], ['剧情'])
        self.assertEqual(saved[1]['Name'], '豆瓣标题')
        self.assertEqual(saved[1]['Overview'], 'TMDB 分集简介')

    def test_preview_classification_and_cached_repair(self):
        key = 'Emby::123'
        thumb = str(self.strm)[:-5] + '-thumb.jpg'
        self.assertEqual(self.enrichment._preview_status(key, thumb), 'missing')
        from PIL import Image
        Image.new('RGB', (320, 180), (30, 205, 50)).save(thumb)
        self.assertEqual(self.enrichment._preview_status(key, thumb), 'candidate')
        stat = os.stat(thumb)
        self.storage['enrichment_preview_cache'] = {key: {'thumb': thumb, 'mtime': int(stat.st_mtime), 'size': stat.st_size}}
        self.assertEqual(self.enrichment._preview_status(key, thumb), 'fixed')

    def test_reject_strm_outside_root(self):
        self.assertEqual(self.enrichment._safe_strm(self.strm), str(self.strm))
        with self.assertRaises(ValueError):
            self.enrichment._safe_strm('/etc/passwd')

    def test_wrong_aspect_ratio_is_repairable_even_when_cached(self):
        from PIL import Image
        thumb = str(self.strm)[:-5] + '-thumb.jpg'
        Image.new('RGB', (1280, 576), (100, 100, 100)).save(thumb)
        stat = os.stat(thumb)
        self.storage['enrichment_preview_cache'] = {'Q4::ep1': {
            'thumb': thumb, 'mtime': int(stat.st_mtime), 'size': stat.st_size}}
        self.assertEqual(self.enrichment._preview_status('Q4::ep1', thumb), 'size')
        self.assertEqual(self.enrichment._preview_dimensions(thumb), (1280, 576, True))
        self.enrichment._preview_selection = {'Q4::ep1': {
            'path': str(self.strm), 'thumb': thumb, 'status': 'size', 'series_id': 'Q4::series'}}
        with patch.object(self.enrichment, '_begin', return_value={'started': True}) as begin:
            self.enrichment.start_preview_repair('Q4::series', ['Q4::ep1'], dimensions_only=True)
            begin.assert_called_once()

    def test_dovi_mixed_and_mild_casts_are_detected_without_a_dominant_channel(self):
        from PIL import Image
        thumb = str(self.strm)[:-5] + '-thumb.png'
        image = Image.new('RGB', (400, 240), (100, 100, 100))
        for top in range(0, 240, 60):
            for left in range(0, 400, 100):
                image.paste((80, 95, 80), (left, top, left + 25, top + 60))
                image.paste((110, 95, 105), (left + 25, top, left + 40, top + 60))
        image.save(thumb)
        self.assertEqual(self.enrichment._color_cast(thumb, dovi=True), 'mixed')
        self.assertEqual(self.enrichment._color_cast(thumb), '')
        Image.new('RGB', (400, 240), (80, 95, 80)).save(thumb)
        self.assertEqual(self.enrichment._color_cast(thumb, dovi=True), 'green')
        for color in [(100, 100, 100), (125, 115, 105), (60, 80, 110)]:
            Image.new('RGB', (400, 240), color).save(thumb)
            self.assertEqual(self.enrichment._color_cast(thumb, dovi=True), '')

    def test_dovi_faint_mixed_cast_requires_both_colors_and_coverage(self):
        from PIL import Image
        thumb = str(self.strm)[:-5] + '-thumb.png'
        image = Image.new('RGB', (800, 480), (150, 150, 145))
        for top in range(0, 480, 120):
            for left in range(0, 800, 200):
                image.paste((100, 108, 100), (left, top, left + 16, top + 120))
                image.paste((108, 100, 108), (left + 16, top, left + 32, top + 120))
        image.save(thumb)
        self.assertEqual(self.enrichment._color_cast(thumb, dovi=True), 'mixed')
        self.assertEqual(self.enrichment._color_cast(thumb), '')
        image = Image.new('RGB', (800, 480), (150, 150, 145))
        image.paste((100, 108, 100), (0, 0, 64, 480))
        image.save(thumb)
        self.assertEqual(self.enrichment._color_cast(thumb, dovi=True), '')
        image.paste((108, 100, 108), (64, 0, 80, 480))
        image.save(thumb)
        self.assertEqual(self.enrichment._color_cast(thumb, dovi=True), '')

    def test_local_foliage_and_clothes_are_not_global_color_casts(self):
        from PIL import Image
        thumb = str(self.strm)[:-5] + '-thumb.png'
        for color in ((30, 150, 40), (160, 40, 150)):
            image = Image.new('RGB', (800, 480), (155, 155, 150))
            image.paste(color, (0, 0, 800, 160))
            image.paste((190, 150, 125), (200, 160, 600, 480))
            image.save(thumb)
            for dovi in (True, False):
                self.assertEqual(self.enrichment._color_cast(thumb, dovi=dovi), '')

    def test_generation_retries_transient_failures_with_different_seek(self):
        attempt = AsyncMock(side_effect=[PreviewFrameError('视频读取中断', retryable=True), None])
        with patch.object(self.enrichment, '_generate_preview_attempt', attempt), \
                patch('emetools.data_enrichment.asyncio.sleep', new_callable=AsyncMock):
            asyncio.run(self.enrichment._generate_preview(str(self.strm), 'thumb'))
        self.assertEqual([call.args[2] for call in attempt.call_args_list], ['00:05:00', '00:01:00'])

    def test_generation_does_not_retry_device_errors_and_limits_network_retries(self):
        for retryable, count in ((False, 1), (True, 3)):
            attempt = AsyncMock(side_effect=PreviewFrameError('安全诊断', retryable=retryable))
            with patch.object(self.enrichment, '_generate_preview_attempt', attempt), \
                    patch('emetools.data_enrichment.asyncio.sleep', new_callable=AsyncMock):
                with self.assertRaises(PreviewFrameError):
                    asyncio.run(self.enrichment._generate_preview(str(self.strm), 'thumb'))
            self.assertEqual(attempt.call_count, count)

    def test_manual_force_allows_existing_dovi_and_missing_needs_no_force(self):
        from PIL import Image
        thumb = str(self.strm)[:-5] + '-thumb.jpg'
        Image.new('RGB', (320, 180), (100, 100, 100)).save(thumb)
        self.enrichment._preview_selection = {'Q4::ep1': {
            'path': str(self.strm), 'thumb': thumb, 'status': 'keep',
            'dovi': True, 'series_id': 'Q4::series'}}
        with patch.object(self.enrichment, '_begin', return_value={'started': True}) as begin:
            with self.assertRaisesRegex(ValueError, '没有检测到'):
                self.enrichment.start_preview_repair('Q4::series', ['Q4::ep1'])
            self.enrichment.start_preview_repair('Q4::series', ['Q4::ep1'], force=True)
            begin.assert_called_once()
            os.unlink(thumb)
            self.enrichment.start_preview_repair('Q4::series', ['Q4::ep1'], force=False)
            self.assertFalse(begin.call_args.args[-1])

    def test_missing_thumbnail_created_while_queued_is_not_overwritten(self):
        from PIL import Image
        thumb = self.strm.with_name(self.strm.stem + '-thumb.jpg')
        Image.new('RGB', (200, 200), (50, 180, 50)).save(thumb)
        original = thumb.read_bytes()
        with patch('emetools.data_enrichment._frame_host_root', return_value=self.temp.name), \
             patch('emetools.data_enrichment._docker', return_value=SimpleNamespace()), \
             patch('emetools.data_enrichment._docker_job') as job, \
             patch.object(self.enrichment, 'scan_preview', new_callable=AsyncMock):
            asyncio.run(self.enrichment._repair_preview('Q4::series', [
                ('Q4::ep1', {'path': str(self.strm), 'status': 'missing'})], force=True))
        job.assert_not_called()
        self.assertEqual(thumb.read_bytes(), original)

    def test_server_image_detection_is_not_discarded_by_local_thumbnail_recheck(self):
        from PIL import Image
        thumb = str(self.strm)[:-5] + '-thumb.jpg'
        Image.new('RGB', (200, 200), (100, 100, 100)).save(thumb)
        self.enrichment._preview_selection = {'Q4::ep1': {
            'path': str(self.strm), 'thumb': thumb, 'status': 'candidate',
            'dovi': True, 'series_id': 'Q4::series'}}
        with patch.object(self.enrichment, '_begin', return_value={'started': True}) as begin:
            self.enrichment.start_preview_repair('Q4::series', ['Q4::ep1'])
            begin.assert_called_once()
            self.assertEqual(begin.call_args.args[3][0][0], 'Q4::ep1')

    def test_probe_retries_timeout_and_reads_duration(self):
        item = {'path': str(self.strm), 'missing_size': True, 'missing_runtime': True}
        success = SimpleNamespace(returncode=0, stdout=b'{"format":{"size":"1024","duration":"12.5"}}')
        with patch('emetools.data_enrichment.subprocess.run',
                   side_effect=[subprocess.TimeoutExpired('ffprobe', 90), success]) as run, \
             patch('emetools.data_enrichment.asyncio.sleep', new_callable=AsyncMock):
            result = asyncio.run(self.enrichment._probe(item))
        self.assertEqual(result['Size'], 1024)
        self.assertEqual(result['RunTimeTicks'], 125000000)
        self.assertEqual([call.kwargs['timeout'] for call in run.call_args_list], [90, 150])
        self.assertIn('-rw_timeout', run.call_args.args[0])

    def test_probe_timeout_still_fetches_http_size_without_downloading_body(self):
        item = {'path': str(self.strm), 'missing_size': True, 'missing_runtime': True}
        factory = httpx.AsyncClient
        def client_factory(**kwargs):
            return factory(transport=httpx.MockTransport(
                lambda request: httpx.Response(206, headers={'Content-Range': 'bytes 0-0/123456'})))
        with patch('emetools.data_enrichment.subprocess.run', side_effect=subprocess.TimeoutExpired('ffprobe', 90)), \
             patch('emetools.data_enrichment.httpx.AsyncClient', side_effect=client_factory), \
             patch('emetools.data_enrichment.asyncio.sleep', new_callable=AsyncMock):
            result = asyncio.run(self.enrichment._probe(item))
        self.assertEqual(result, {'Size': 123456})

    def test_probe_reads_stream_duration_when_container_duration_missing(self):
        item = {'path': str(self.strm), 'missing_size': False, 'missing_runtime': True}
        process = SimpleNamespace(returncode=0, stdout=json.dumps({'streams': [
            {'codec_type': 'video', 'duration': '12.5'},
            {'codec_type': 'audio', 'duration': '13'},
            {'codec_type': 'subtitle', 'duration': '1000'}]}).encode())
        with patch('emetools.data_enrichment.subprocess.run', return_value=process):
            result = asyncio.run(self.enrichment._probe(item))
        self.assertEqual(result, {'RunTimeTicks': 130000000})

    def test_successful_mediainfo_write_is_not_failed_by_recheck_500(self):
        self.enrichment._mi_selection = [{'server': 'Q4', 'id': '1', 'path': str(self.strm),
                                         'missing_size': True, 'missing_runtime': False}]
        def handler(request):
            if request.url.path.endswith('System/Info/Public'):
                return httpx.Response(200, json={'Id': 'server'})
            return httpx.Response(200, json=[] if request.url.path.endswith(('Sessions', 'ScheduledTasks')) else {'Items': []})
        request = httpx.Request('GET', 'http://emby/Items')
        error = httpx.HTTPStatusError('500', request=request, response=httpx.Response(500, request=request))
        def server(name):
            return httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler))
        with patch.object(self.enrichment, '_server', side_effect=server), \
             patch.object(self.enrichment, '_existing_mediainfo', new_callable=AsyncMock, return_value={}), \
             patch.object(self.enrichment, '_probe', new_callable=AsyncMock, return_value={'Size': 100}), \
             patch.object(self.enrichment, '_verify_local_emby', new_callable=AsyncMock), \
             patch.object(self.enrichment, '_scheduled_task', new_callable=AsyncMock), \
             patch.object(self.enrichment, '_write_emby_database', return_value=(1, 'backup')), \
             patch.object(self.enrichment, '_check_mediainfo', new_callable=AsyncMock, side_effect=error), \
             patch('emetools.data_enrichment.asyncio.sleep', new_callable=AsyncMock):
            asyncio.run(self.enrichment._fill_mediainfo())
        self.assertTrue(self.enrichment.status()['mediainfo']['recheck_pending'])
        self.assertEqual(self.enrichment.status()['mediainfo']['updated_count'], 1)

    def test_mediainfo_waits_for_items_endpoint_without_repeating_database_write(self):
        self.enrichment._mi_selection = [{'server': 'Q4', 'id': '1', 'path': str(self.strm),
                                         'missing_size': True, 'missing_runtime': False}]
        items_requests = []
        def handler(request):
            if request.url.path.endswith('Items'):
                items_requests.append(request)
                return httpx.Response(500)
            if request.url.path.endswith('System/Info/Public'):
                return httpx.Response(200, json={'Id': 'server'})
            return httpx.Response(200, json=[])
        def server(name):
            return httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler))
        with patch.object(self.enrichment, '_server', side_effect=server), \
             patch.object(self.enrichment, '_existing_mediainfo', new_callable=AsyncMock, return_value={}), \
             patch.object(self.enrichment, '_probe', new_callable=AsyncMock, return_value={'Size': 100}), \
             patch.object(self.enrichment, '_verify_local_emby', new_callable=AsyncMock), \
             patch.object(self.enrichment, '_write_emby_database', return_value=(1, 'backup')) as write, \
             patch.object(self.enrichment, '_check_mediainfo', new_callable=AsyncMock) as check, \
             patch('emetools.data_enrichment.asyncio.sleep', new_callable=AsyncMock):
            asyncio.run(self.enrichment._fill_mediainfo())
        write.assert_called_once()
        check.assert_not_awaited()
        self.assertEqual(len(items_requests), 24)
        self.assertTrue(self.enrichment.status()['mediainfo']['recheck_pending'])

    def test_mediainfo_does_not_write_when_missing_fields_are_not_probed(self):
        self.enrichment._mi_selection = [{'server': 'Q4', 'id': '1', 'path': str(self.strm),
                                         'missing_size': True, 'missing_runtime': False}]
        with patch.object(self.enrichment, '_probe', new_callable=AsyncMock,
                          return_value={'Width': 1920, 'Container': 'mov'}), \
             patch.object(self.enrichment, '_existing_mediainfo', new_callable=AsyncMock, return_value={}), \
             patch.object(self.enrichment, '_write_emby_database') as write:
            asyncio.run(self.enrichment._fill_mediainfo())
        write.assert_not_called()

    def test_existing_mediainfo_uses_only_same_file_sources(self):
        item = {'server': 'Q4', 'id': '1', 'path': str(self.strm),
                'missing_size': True, 'missing_runtime': True}
        detail = {'Path': str(self.strm), 'MediaSources': [
            {'Path': '/other/video.mkv', 'Size': 999, 'RunTimeTicks': 999},
            {'Path': str(self.strm), 'Size': 100, 'RunTimeTicks': 125000000}]}
        emby = httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=detail)))
        with patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, '_user_id', new_callable=AsyncMock, return_value='admin'):
            result = asyncio.run(self.enrichment._existing_mediainfo(item))
        self.assertEqual(result, {'Size': 100, 'RunTimeTicks': 125000000})

    def test_failed_probe_diagnoses_archive_without_inventing_runtime(self):
        item = {'path': str(self.strm), 'missing_size': False, 'missing_runtime': True}
        process = SimpleNamespace(returncode=1, stdout=b'{}', stderr=b'EBML header parsing failed')
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(206, content=b"7z\xbc\xaf\x27\x1c" + bytes(250))))
        with patch('emetools.data_enrichment.subprocess.run', return_value=process), \
             patch('emetools.data_enrichment.httpx.AsyncClient', return_value=client), \
             patch('emetools.data_enrichment.asyncio.sleep', new_callable=AsyncMock):
            result = asyncio.run(self.enrichment._probe(item))
        self.assertEqual(result, {})
        self.assertTrue(any('7z 压缩包' in line for line in self.enrichment.status()['log']))

    def test_mediainfo_check_excludes_iso_with_only_missing_runtime(self):
        root = Path(self.temp.name)
        iso = root / 'movie.iso.strm'
        iso.write_text('https://example.org/movie.iso\n', encoding='utf8')
        missing_iso_size = root / 'missing.iso.strm'
        missing_iso_size.write_text('https://example.org/missing.iso\n', encoding='utf8')
        items = [
            {'Id': 'iso', 'Path': str(iso), 'Size': 100, 'RunTimeTicks': 0},
            {'Id': 'source-iso', 'MediaSources': [{'Path': str(iso), 'Size': 100}],
             'RunTimeTicks': 0},
            {'Id': 'iso-no-size', 'Path': str(missing_iso_size), 'Size': 0,
             'RunTimeTicks': 0},
            {'Id': 'episode', 'Path': str(self.strm), 'Size': 100, 'RunTimeTicks': 0},
            {'Id': 'complete', 'Path': str(self.strm), 'Size': 100, 'RunTimeTicks': 100},
        ]
        emby = httpx.AsyncClient(base_url='http://emby/emby/', transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={
                'Items': items, 'TotalRecordCount': len(items)})))
        with patch.object(self.enrichment, '_services', return_value={'Q4': object()}), \
             patch.object(self.enrichment, '_server', return_value=emby):
            asyncio.run(self.enrichment._check_mediainfo())
        result = self.enrichment.status()['mediainfo']
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['incomplete_count'], 2)
        self.assertEqual([item['id'] for item in self.enrichment._mi_selection],
                         ['iso-no-size', 'episode'])
        self.assertTrue(self.enrichment._skip_iso_runtime('/films/movie.ISO.STRM', 100, 0))
        self.assertFalse(self.enrichment._skip_iso_runtime('/films/movie.ISO.STRM', 0, 0))

    def test_database_script_writes_only_selected_fields_and_keeps_backup(self):
        db = Path(self.temp.name) / 'library.db'
        with sqlite3.connect(db) as connection:
            connection.execute('CREATE TABLE MediaItems (Id INTEGER PRIMARY KEY, Size INTEGER, RunTimeTicks INTEGER)')
            connection.execute('INSERT INTO MediaItems VALUES (1, 0, 0)')
        script = DB_SCRIPT.replace('/emby-config/data/library.db', str(db))
        environment = {**os.environ, 'ENRICH_UPDATES': json.dumps({'1': {'Size': 1024, 'RunTimeTicks': 20000000},
                                                                    '2': {'Size': 999}})}
        result = subprocess.run([sys.executable, '-c', script], env=environment,
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['updated'], 1)
        with sqlite3.connect(db) as connection:
            self.assertEqual(connection.execute('SELECT Size, RunTimeTicks FROM MediaItems').fetchone(), (1024, 20000000))
        with sqlite3.connect(json.loads(result.stdout)['backup']) as connection:
            self.assertEqual(connection.execute('SELECT Size FROM MediaItems').fetchone(), (0,))

    def test_sidecar_wait_error_still_removes_container(self):
        with patch('emetools.data_enrichment._docker') as docker:
            docker.side_effect = [SimpleNamespace(json=lambda: {'Id': 'abc'}),
                                  SimpleNamespace(), RuntimeError('wait timeout'), SimpleNamespace()]
            with self.assertRaises(RuntimeError):
                _docker_job('test-image', {})
            self.assertEqual(docker.call_args.args, ('DELETE', '/containers/abc'))

    def test_sidecar_start_timeout_recovers_already_running_container(self):
        responses = [SimpleNamespace(json=lambda: {'Id': 'abc'}), httpx.ReadTimeout('timed out'),
                     SimpleNamespace(json=lambda: {'State': {'Status': 'running'}}),
                     SimpleNamespace(json=lambda: {'StatusCode': 0}),
                     SimpleNamespace(content=b''), SimpleNamespace()]
        with patch('emetools.data_enrichment._docker', side_effect=responses) as docker:
            self.assertEqual(_docker_job('test-image', {}), '')
            self.assertEqual(docker.call_args_list[0].kwargs['timeout'], 120)
            self.assertEqual(docker.call_args_list[1].kwargs['timeout'], 120)
            self.assertEqual(sum(call.args[1].endswith('/start') for call in docker.call_args_list), 1)
            self.assertEqual(docker.call_args.args, ('DELETE', '/containers/abc'))

    def test_sidecar_create_timeout_cleans_up_by_unique_name(self):
        with patch('emetools.data_enrichment._docker', side_effect=[httpx.ReadTimeout('secret-url'),
                                                                  SimpleNamespace()]) as docker:
            with self.assertRaisesRegex(ValueError, '创建截帧容器超时') as error:
                _docker_job('test-image', {})
            self.assertNotIn('secret-url', str(error.exception))
            name = docker.call_args_list[0].kwargs['params']['name']
            self.assertEqual(docker.call_args.args, ('DELETE', '/containers/' + name))

    def test_sidecar_start_timeout_does_not_retry_unstarted_container(self):
        responses = [SimpleNamespace(json=lambda: {'Id': 'abc'}), httpx.ReadTimeout('timed out'),
                     SimpleNamespace(json=lambda: {'State': {'Status': 'created'}}), SimpleNamespace()]
        with patch('emetools.data_enrichment._docker', side_effect=responses) as docker:
            with self.assertRaisesRegex(ValueError, '启动截帧容器超时'):
                _docker_job('test-image', {})
            self.assertEqual(docker.call_args.args, ('DELETE', '/containers/abc'))

    def test_frame_mount_resolves_only_config_bind(self):
        details = {'Mounts': [{'Destination': '/config', 'Source': '/host/moviepilot-config',
                               'Type': 'bind'}]}
        with patch.dict(os.environ, {'HOSTNAME': 'a' * 12}), \
             patch('emetools.data_enrichment._docker', return_value=SimpleNamespace(json=lambda: details)):
            self.assertEqual(_frame_host_root(), '/host/moviepilot-config')
            details['Mounts'][0]['Destination'] = '/media'
            with self.assertRaisesRegex(ValueError, '/config'):
                _frame_host_root()

    def test_frame_mount_accepts_compose_container_name(self):
        details = {'Mounts': [{'Destination': '/config', 'Source': '/host/moviepilot-config',
                               'Type': 'bind'}]}
        with patch.dict(os.environ, {'HOSTNAME': 'moviepilot'}, clear=False), \
             patch('emetools.data_enrichment._docker', return_value=SimpleNamespace(json=lambda: details)) as docker:
            self.assertEqual(_frame_host_root(), '/host/moviepilot-config')
            self.assertEqual(docker.call_args.args, ('GET', '/containers/moviepilot/json'))

    def test_frame_failure_classifies_vulkan_without_leaking_strm_url(self):
        responses = [SimpleNamespace(json=lambda: {'Id': 'cid'}), SimpleNamespace(),
                     SimpleNamespace(json=lambda: {'StatusCode': 187}),
                     SimpleNamespace(content=b''),
                     SimpleNamespace(content=b'Failed creating Vulkan device https://video.example/?token=secret'),
                     SimpleNamespace()]
        with patch('emetools.data_enrichment._docker', side_effect=responses) as docker:
            with self.assertRaisesRegex(ValueError, 'Vulkan 不可用') as error:
                _docker_job('image', {})
        self.assertNotIn('secret', str(error.exception))
        self.assertEqual(docker.call_args.args, ('DELETE', '/containers/cid'))

    def test_frame_network_error_is_retryable_without_leaking_signed_url(self):
        responses = [SimpleNamespace(json=lambda: {'Id': 'cid'}), SimpleNamespace(),
                     SimpleNamespace(json=lambda: {'StatusCode': 1}),
                     SimpleNamespace(content=b''),
                     SimpleNamespace(content=b'I/O error https://video.example/?token=secret'),
                     SimpleNamespace()]
        with patch('emetools.data_enrichment._docker', side_effect=responses):
            with self.assertRaisesRegex(PreviewFrameError, '远程视频读取中断') as error:
                _docker_job('image', {})
        self.assertTrue(error.exception.retryable)
        self.assertNotIn('secret', str(error.exception))

    def test_generation_rereads_link_and_preserves_old_thumbnail_until_success(self):
        from PIL import Image
        real_tempdir = tempfile.TemporaryDirectory
        thumb = str(self.strm)[:-5] + '-thumb.jpg'
        Image.new('RGB', (1280, 720), (50, 60, 70)).save(thumb)
        original = Path(thumb).read_bytes()
        urls = []

        def job(_image, config, **kwargs):
            urls.append(config['Cmd'][config['Cmd'].index('-i') + 1])
            self.assertEqual(Path(thumb).read_bytes(), original)
            if len(urls) == 1:
                self.strm.write_text('https://example.org/refreshed.mp4', encoding='utf8')
                raise PreviewFrameError('远程视频读取中断', retryable=True)
            frame_dir = Path(self.temp.name) / config['HostConfig']['Binds'][0].split(':')[0].rsplit('/', 1)[-1]
            Image.new('RGB', (320, 180), (110, 120, 130)).save(frame_dir / 'frame.png')

        with patch('emetools.data_enrichment._frame_host_root', return_value=self.temp.name), \
                patch('emetools.data_enrichment._docker', return_value=SimpleNamespace()), \
                patch('emetools.data_enrichment._docker_job', side_effect=job), \
                patch('emetools.data_enrichment.tempfile.TemporaryDirectory',
                      side_effect=lambda **kwargs: real_tempdir(prefix=kwargs['prefix'], dir=self.temp.name)), \
                patch('emetools.data_enrichment.asyncio.sleep', new_callable=AsyncMock):
            asyncio.run(self.enrichment._generate_preview(str(self.strm), thumb))
        self.assertEqual(urls, ['https://example.org/video.mp4', 'https://example.org/refreshed.mp4'])
        self.assertNotEqual(Path(thumb).read_bytes(), original)

    def test_frame_137_checks_docker_oom_state_before_reporting_memory_failure(self):
        for oom in (True, False):
            responses = [SimpleNamespace(json=lambda: {'Id': 'cid'}), SimpleNamespace(),
                         SimpleNamespace(json=lambda: {'StatusCode': 137}),
                         SimpleNamespace(content=b''), SimpleNamespace(content=b''),
                         SimpleNamespace(json=lambda: {'State': {'OOMKilled': oom}}), SimpleNamespace()]
            with patch('emetools.data_enrichment._docker', side_effect=responses) as docker:
                with self.assertRaisesRegex(ValueError, '内存超限' if oom else '未确认 OOM'):
                    _docker_job('image', {})
            self.assertEqual(docker.call_args.args, ('DELETE', '/containers/cid'))

    def test_frame_187_without_diagnostics_does_not_guess_gpu_failure(self):
        responses = [SimpleNamespace(json=lambda: {'Id': 'cid'}), SimpleNamespace(),
                     SimpleNamespace(json=lambda: {'StatusCode': 187}),
                     SimpleNamespace(content=b''), SimpleNamespace(content=b''), SimpleNamespace()]
        with patch('emetools.data_enrichment._docker', side_effect=responses):
            with self.assertRaisesRegex(ValueError, '不能仅凭退出码认定'):
                _docker_job('image', {})

    def test_preview_repair_maps_gpu_and_reads_png_file_not_docker_logs(self):
        from PIL import Image
        real_tempdir = tempfile.TemporaryDirectory
        temporary = self.strm.with_name(self.strm.stem + '-thumb.jpg')
        saved = []

        def fake_job(_image, config, **_kwargs):
            saved.append(config)
            self.assertEqual(config['HostConfig']['Devices'][0]['PathOnHost'], '/dev/dri')
            self.assertEqual(config['HostConfig']['Binds'][0].split(':')[1], '/out')
            self.assertEqual(config['HostConfig']['Memory'], 1610612736)
            self.assertIn('vulkan=vk:0', config['Cmd'])
            self.assertEqual(config['Cmd'][config['Cmd'].index('-threads') + 1], '1')
            self.assertEqual(config['Cmd'][config['Cmd'].index('-filter_threads') + 1], '1')
            self.assertIn('apply_dolbyvision=true', config['Cmd'][config['Cmd'].index('-vf') + 1])
            self.assertIn('w=1280:h=720', config['Cmd'][config['Cmd'].index('-vf') + 1])
            Image.new('RGB', (200, 120), (50, 60, 70)).save(
                Path(self.temp.name) / config['HostConfig']['Binds'][0].split(':')[0].rsplit('/', 1)[-1] / 'frame.png')

        emby = httpx.AsyncClient(base_url='http://emby/emby/', transport=httpx.MockTransport(
            lambda request: httpx.Response(204) if request.method == 'POST' else httpx.Response(404)))
        with patch('emetools.data_enrichment._frame_host_root', return_value=self.temp.name), \
             patch('emetools.data_enrichment._docker', return_value=SimpleNamespace()), \
             patch('emetools.data_enrichment._docker_job', side_effect=fake_job), \
             patch('emetools.data_enrichment.tempfile.TemporaryDirectory',
                   side_effect=lambda **kw: real_tempdir(prefix=kw['prefix'], dir=self.temp.name)), \
             patch.object(self.enrichment, '_server', return_value=emby), \
             patch.object(self.enrichment, 'scan_preview', new_callable=AsyncMock), \
             patch('emetools.data_enrichment.asyncio.sleep', new_callable=AsyncMock) as sleep:
            asyncio.run(self.enrichment._repair_preview('Q4::series', [
                ('Q4::ep1', {'path': str(self.strm), 'status': 'missing'})], force=False))
            sleep.assert_not_awaited()
        self.assertTrue(saved)
        self.assertIn('Emby 分集 ID ep1', self.enrichment.status()['preview_repaired'][0])
        with Image.open(temporary) as image:
            self.assertEqual(image.size, (1280, 720))

    def test_preview_refresh_failure_retains_image_and_retry_only_syncs(self):
        from PIL import Image
        thumb = str(self.strm.with_name(self.strm.stem + "-thumb.jpg"))
        target = {"path": str(self.strm), "thumb": thumb, "status": "missing"}
        requests = []

        async def generate(path, output):
            self.assertEqual(path, str(self.strm))
            Image.new("RGB", (1280, 720), (50, 60, 70)).save(output)

        def handler(request):
            requests.append(request)
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/emby/Items/ep1/Refresh")
            self.assertEqual(request.url.params["MetadataRefreshMode"], "ValidationOnly")
            self.assertEqual(request.url.params["Recursive"], "false")
            self.assertEqual(request.url.params["ReplaceAllImages"], "false")
            return httpx.Response(400 if len(requests) == 1 else 204)

        def client(_name):
            return httpx.AsyncClient(base_url="http://emby/emby/", transport=httpx.MockTransport(handler))

        with patch.object(self.enrichment, "_generate_preview", side_effect=generate) as generate_mock, \
             patch.object(self.enrichment, "_server", side_effect=client), \
             patch.object(self.enrichment, "scan_preview", new_callable=AsyncMock), \
             patch("emetools.data_enrichment._docker") as docker:
            with self.assertRaisesRegex(ValueError, "1 集修复失败"):
                asyncio.run(self.enrichment._repair_preview("Q4::series", [("Q4::ep1", target)], False))
            self.assertEqual(self.enrichment._preview_status("Q4::ep1", thumb), "pending")
            self.assertFalse(self.storage["enrichment_preview_cache"]["Q4::ep1"]["synced"])
            before = Path(thumb).read_bytes()
            asyncio.run(self.enrichment._repair_preview("Q4::series", [("Q4::ep1", target)], False))
            generate_mock.assert_awaited_once()
            docker.assert_not_called()
            self.assertEqual(Path(thumb).read_bytes(), before)
        self.assertEqual(self.enrichment._preview_status("Q4::ep1", thumb), "fixed")
        self.assertEqual(self.enrichment.status()["preview_result"], {"ok": 1, "fail": 0})
        self.assertTrue(any("仅重试同步" in line for line in self.enrichment.status()["log"]))

    def test_pending_preview_is_accepted_without_force(self):
        from PIL import Image
        thumb = str(self.strm.with_name(self.strm.stem + "-thumb.jpg"))
        Image.new("RGB", (1280, 720), (50, 60, 70)).save(thumb)
        stat = os.stat(thumb)
        self.storage["enrichment_preview_cache"] = {"Q4::ep1": {
            "thumb": thumb, "mtime": int(stat.st_mtime), "size": stat.st_size, "synced": False}}
        self.enrichment._preview_selection = {"Q4::ep1": {
            "path": str(self.strm), "thumb": thumb, "status": "pending"}}
        with patch.object(self.enrichment, "_begin", return_value={"ok": True}) as begin:
            self.enrichment.start_preview_repair("Q4::series", ["Q4::ep1"])
        self.assertEqual(begin.call_args.args[3][0][0], "Q4::ep1")

    def test_enrich_reads_user_item_and_writes_admin_item(self):
        """Emby GET /Items/{id} gives 404; the detail GET must include UserId."""
        seen = []

        def emby_handler(request):
            seen.append((request.method, request.url.path))
            if request.url.path == '/emby/Users/user123/Items/73025':
                return httpx.Response(200, json={'Id': '73025', 'Type': 'Series', 'Name': '云雀叫天录',
                                                  'ProviderIds': {'Tmdb': '12345'}, 'People': []})
            if request.method == 'POST' and request.url.path == '/emby/Items/73025':
                return httpx.Response(204)
            return httpx.Response(404)

        def tmdb_handler(request):
            seen.append((request.method, request.url.path))
            if request.url.path == '/3/tv/12345':
                return httpx.Response(200, json={'name': '云雀叫天录', 'overview': '剧情介绍',
                                                  'aggregate_credits': {'cast': []}})
            return httpx.Response(404)

        emby = httpx.AsyncClient(base_url='http://emby/emby/', transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.themoviedb.org/3/',
                                 transport=httpx.MockTransport(tmdb_handler))
        server = SimpleNamespace(get_user=lambda: 'user123')
        with patch.object(self.enrichment, '_services', return_value={'Q4': server}), \
             patch.object(self.enrichment, '_server', return_value=emby), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb):
            asyncio.run(self.enrichment._enrich('Q4', '73025', 'metadata'))
        self.assertIn(('GET', '/emby/Users/user123/Items/73025'), seen)
        self.assertIn(('POST', '/emby/Items/73025'), seen)
        self.assertNotIn(('GET', '/emby/Items/73025'), seen)

    def test_episode_enrichment_uses_user_scoped_details(self):
        seen = []

        def emby_handler(request):
            seen.append((request.method, request.url.path))
            if request.url.path == '/emby/Users/user123/Items/73025':
                return httpx.Response(200, json={'Id': '73025', 'Type': 'Series', 'Name': '测试剧集',
                                                  'ProviderIds': {'Tmdb': '12345'}})
            if request.url.path == '/emby/Users/user123/Items':
                return httpx.Response(200, json={'Items': [{'Id': '9', 'ParentIndexNumber': 1,
                                                              'IndexNumber': 2}]})
            if request.url.path == '/emby/Users/user123/Items/9':
                return httpx.Response(200, json={'Id': '9', 'Name': '旧集名'})
            if request.method == 'POST' and request.url.path == '/emby/Items/9':
                return httpx.Response(204)
            return httpx.Response(404)

        def tmdb_handler(request):
            if request.url.path == '/3/tv/12345':
                return httpx.Response(200, json={'name': '测试剧集'})
            if request.url.path == '/3/tv/12345/season/1':
                return httpx.Response(200, json={'episodes': [{'episode_number': 2, 'name': '新集名'}]})
            return httpx.Response(404)

        emby = httpx.AsyncClient(base_url='http://emby/emby/', transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://api.themoviedb.org/3/',
                                 transport=httpx.MockTransport(tmdb_handler))
        with patch.object(self.enrichment, '_services', return_value={'Q4': SimpleNamespace(get_user=lambda: 'user123')}), \
             patch.object(self.enrichment, '_server', return_value=emby), \
             patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-key'), \
             patch.object(self.enrichment, '_tmdb_client', return_value=tmdb):
            asyncio.run(self.enrichment._enrich('Q4', '73025', 'episodes'))
        self.assertIn(('GET', '/emby/Users/user123/Items/9'), seen)
        self.assertIn(('POST', '/emby/Items/9'), seen)
        self.assertNotIn(('GET', '/emby/Items/9'), seen)


if __name__ == '__main__':
    unittest.main()
