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
                                      _docker_job, _frame_host_root, validate_enrich_config)


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

    def test_chinese_title_selection_rejects_foreign_scripts(self):
        for value in ('오징어 게임', '愛の不時着です', '中文한글', 'เด็กใหม่', 'Squid Game', ''):
            with self.subTest(title=value):
                self.assertFalse(DataEnrichment._is_chinese_title(value))
                self.assertEqual(DataEnrichment._select_chinese_title('原有中文剧名', value), '原有中文剧名')
        for value in ('鱿鱼游戏', '魷魚遊戲', '我的 Happy Ending'):
            self.assertTrue(DataEnrichment._is_chinese_title(value))
        self.assertEqual(DataEnrichment._select_chinese_title('오징어 게임', '鱿鱼游戏'), '鱿鱼游戏')

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

    def test_tmdb_client_uses_moviepilot_domain_and_https_proxy(self):
        with patch('emetools.data_enrichment.settings.TMDB_API_DOMAIN', 'api.tmdb.org'), \
             patch('emetools.data_enrichment.get_runtime_setting', return_value={
                 'http': 'http://proxy.example:1080', 'https': 'http://proxy.example:1080'}), \
             patch('emetools.data_enrichment.httpx.AsyncClient') as client:
            self.enrichment._tmdb_client()
        self.assertEqual(client.call_args.kwargs['base_url'], 'https://api.tmdb.org/3/')
        self.assertEqual(client.call_args.kwargs['proxy'], 'http://proxy.example:1080')
        self.assertFalse(client.call_args.kwargs['trust_env'])

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

    def test_batch_enrich_continues_after_failure(self):
        async def enrich(name, identifier, mode, options):
            if identifier == '2':
                raise ValueError('series failure')
        with patch.object(self.enrichment, '_enrich', side_effect=enrich):
            asyncio.run(self.enrichment._batch_enrich(['Q4::1', 'Q4::2', 'Q4::3'],
                                                     DEFAULT_ENRICH_CONFIG))
        self.assertEqual(self.enrichment.status()['batch_result'], {'ok': 2, 'fail': 1})

    def test_tv_libraries_only_exposes_series_libraries(self):
        instance = SimpleNamespace(get_librarys=lambda hidden=False: [
            SimpleNamespace(id='11', name='电视剧', type='电视剧'),
            SimpleNamespace(id='12', name='电影', type='电影')])
        service = SimpleNamespace(config=SimpleNamespace(name='Q4'), instance=instance)
        with patch('emetools.data_enrichment.MediaServerHelper') as helper, \
             patch('emetools.data_enrichment.MediaType') as media_type:
            helper.return_value.get_services.return_value = {'Q4': service}
            media_type.TV.value = '电视剧'
            self.assertEqual(self.enrichment.tv_libraries(), [
                {'id': 'Q4::11', 'name': '电视剧', 'server': 'Q4'}])

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

    def test_batch_preview_only_repairs_existing_suspected_images(self):
        async def scan(series_id):
            self.enrichment._preview_selection = {
                f'{series_id}::ep1': {'path': str(self.strm)},
                f'{series_id}::ep2': {'path': str(self.strm)}}
            return [{'id': f'{series_id}::ep1', 'status': 'missing'},
                    {'id': f'{series_id}::ep2', 'status': 'candidate'}]

        async def repair(series_id, targets, force):
            self.assertEqual([key for key, _ in targets], [f'{series_id}::ep2'])
            self.assertFalse(force)
            self.enrichment.state['preview_result'] = {'ok': 1, 'fail': 0}

        with patch.object(self.enrichment, 'scan_preview', side_effect=scan), \
             patch.object(self.enrichment, '_repair_preview', side_effect=repair):
            asyncio.run(self.enrichment._batch_preview(['Q4::1', 'Q4::2']))
        self.assertEqual(self.enrichment.status()['batch_result'],
                         {'scanned': 4, 'ok': 2, 'fail': 0})
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
            asyncio.run(self.enrichment._enrich('Q4', '73025', 'all', DEFAULT_ENRICH_CONFIG))
        series = written['/emby/Items/73025']
        episode = written['/emby/Items/ep1']
        self.assertEqual(series['Name'], '新剧名')
        self.assertEqual(series['Overview'], '整剧简介')
        self.assertEqual(series['CommunityRating'], 8.2)
        self.assertEqual(series['Genres'], ['剧情'])
        self.assertEqual(series['Studios'], ['电视台', '制作公司'])
        self.assertEqual(series['ProviderIds'], {
            'Tmdb': '12345', 'Douban': '88', 'Imdb': 'tt12345', 'Tvdb': '987'})
        self.assertEqual(episode['People'], series['People'])
        self.assertEqual(episode['Name'], '第一集')
        self.assertEqual(episode['Overview'], '剧情简介')

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
            self.assertEqual(asyncio.run(self.enrichment._douban_data(item))['overview'], '豆瓣简介')
            self.assertEqual(asyncio.run(self.enrichment._douban_data(item, 1))[1]['name'], '第一集')
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
            result = asyncio.run(self.enrichment._douban_data(
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
        seen = []

        def handler(request):
            seen.append((request.url.path, json.loads(request.content)['model']))
            return httpx.Response(200, json={'choices': [{'message': {
                'content': '{"Name":"中文剧名"}'}}]})

        llm = httpx.AsyncClient(base_url='https://llm.example/v1/', transport=httpx.MockTransport(handler))
        with patch('emetools.data_enrichment.settings.LLM_API_KEY', 'test-only'), \
             patch('emetools.data_enrichment.settings.LLM_BASE_URL', 'https://llm.example/v1'), \
             patch('emetools.data_enrichment.settings.LLM_MODEL', 'test-model'), \
             patch('emetools.data_enrichment.settings.LLM_API_PROTOCOL', 'auto'), \
             patch('emetools.data_enrichment.settings.LLM_USE_PROXY', False), \
             patch('emetools.data_enrichment.httpx.AsyncClient', return_value=llm):
            response = asyncio.run(self.enrichment._ai_map({'Name': 'English name'}, '剧集标题'))
        self.assertEqual(response, {'Name': '中文剧名'})
        self.assertEqual(seen, [('/v1/chat/completions', 'test-model')])

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
             patch('emetools.data_enrichment.httpx.AsyncClient', return_value=tmdb):
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
        Image.new('RGB', (200, 200), (30, 205, 50)).save(thumb)
        self.assertEqual(self.enrichment._preview_status(key, thumb), 'candidate')
        stat = os.stat(thumb)
        self.storage['enrichment_preview_cache'] = {key: {'thumb': thumb, 'mtime': int(stat.st_mtime), 'size': stat.st_size}}
        self.assertEqual(self.enrichment._preview_status(key, thumb), 'fixed')

    def test_reject_strm_outside_root(self):
        self.assertEqual(self.enrichment._safe_strm(self.strm), str(self.strm))
        with self.assertRaises(ValueError):
            self.enrichment._safe_strm('/etc/passwd')

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

    def test_preview_repair_maps_gpu_and_reads_png_file_not_docker_logs(self):
        from PIL import Image
        real_tempdir = tempfile.TemporaryDirectory
        temporary = self.strm.with_name(self.strm.stem + '-thumb.jpg')
        saved = []

        def fake_job(_image, config, **_kwargs):
            saved.append(config)
            self.assertEqual(config['HostConfig']['Devices'][0]['PathOnHost'], '/dev/dri')
            self.assertEqual(config['HostConfig']['Binds'][0].split(':')[1], '/out')
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
             patch('emetools.data_enrichment.asyncio.sleep', new_callable=AsyncMock):
            asyncio.run(self.enrichment._repair_preview('Q4::series', [
                ('Q4::ep1', {'path': str(self.strm)})], force=True))
        self.assertTrue(saved)
        self.assertIn('Emby 分集 ID ep1', self.enrichment.status()['preview_repaired'][0])
        with Image.open(temporary) as image:
            self.assertEqual(image.size, (200, 96))

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
             patch('emetools.data_enrichment.httpx.AsyncClient', return_value=tmdb):
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
             patch('emetools.data_enrichment.httpx.AsyncClient', return_value=tmdb):
            asyncio.run(self.enrichment._enrich('Q4', '73025', 'episodes'))
        self.assertIn(('GET', '/emby/Users/user123/Items/9'), seen)
        self.assertIn(('POST', '/emby/Items/9'), seen)
        self.assertNotIn(('GET', '/emby/Items/9'), seen)


if __name__ == '__main__':
    unittest.main()
