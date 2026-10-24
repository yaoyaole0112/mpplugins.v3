import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from emetools.data_enrichment import DataEnrichment, DEFAULT_ENRICH_CONFIG, validate_enrich_config
from emetools.overview_quality import (overview_issues, overview_length, overview_signature,
                                       duplicate_overviews)


class OverviewQualityTests(unittest.TestCase):
    def test_quality_reasons_and_visible_length(self):
        self.assertEqual(overview_length(' 中 文 \n'), 2)
        self.assertEqual(overview_issues('暂无简介', 0), ['缺失'])
        self.assertEqual(overview_issues('English plot', 40, True), ['非中文', '过短', '同季重复'])
        self.assertEqual(overview_issues('可靠中文剧情。', 0), [])

    def test_duplicate_detection_only_compares_within_same_season(self):
        entries = [{'ParentIndexNumber': season, 'Overview': text} for season, text in (
            (1, '这是一段完整的重复分集简介。'), (1, '这是 一段完整的重复分集简介。'),
            (2, '这是一段完整的重复分集简介。'), (1, '暂无简介'), (1, '暂无简介'))]
        self.assertEqual(duplicate_overviews(entries), {(1, overview_signature(entries[0]['Overview']))})

    def test_settings_validate_ranges_types_and_backward_defaults(self):
        values = validate_enrich_config({'overview_movie_min': 0, 'overview_episode_min': 2000})
        self.assertEqual(values['overview_movie_min'], 0)
        self.assertFalse(values['overview_lock'])
        self.assertTrue(values['overview_english_fallback'])
        for invalid in ({'overview_movie_min': -1}, {'overview_episode_min': 2001},
                        {'overview_movie_min': True}, {'overview_episode_min': '40'},
                        {'overview_lock': 1}, {'overview_english_fallback': 'false'}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_enrich_config(invalid)


class OverviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.storage = {}
        owner = SimpleNamespace(get_data=self.storage.get,
                                save_data=lambda key, value: self.storage.__setitem__(key, value))
        self.engine = DataEnrichment(owner)
        self.options = {**DEFAULT_ENRICH_CONFIG, 'ai_enabled': True, 'ai_overview': True}
        self.sleep_patch = patch('emetools.data_enrichment.asyncio.sleep', new_callable=AsyncMock)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def resolve(self, primary='', current='', **overrides):
        return asyncio.run(self.engine._resolve_overview(None, 'movie/123', 'secret', current, primary,
            {**self.options, **overrides}, minimum=40, source='TMDB 中文'))

    def test_english_fallback_uses_exact_movie_season_and_episode_id(self):
        for path in ('movie/123', 'tv/123', 'tv/123/season/2/episode/7'):
            with self.subTest(path=path), \
                    patch.object(self.engine, '_tmdb_json', new_callable=AsyncMock,
                                 return_value={'overview': 'An actual English synopsis.'}) as request, \
                    patch.object(self.engine, '_optional_ai_map', new_callable=AsyncMock,
                                 return_value={'Overview': '忠实翻译的中文剧情。'}) as translate:
                text, source = asyncio.run(self.engine._resolve_overview(None, path, 'secret', '', '',
                    self.options, minimum=40, source='TMDB 中文'))
                self.assertEqual(text, '忠实翻译的中文剧情。')
                self.assertEqual(source, 'TMDB 英文原文翻译')
                self.assertEqual(request.call_args.args[1], path)
                self.assertEqual(request.call_args.args[2]['language'], 'en-US')
                self.assertIn('不生成剧情', translate.call_args.args[1])

    def test_ai_and_scope_switches_prevent_fetching_and_translation(self):
        for values in ({'ai_enabled': False}, {'ai_overview': False}):
            with self.subTest(values=values), \
                    patch.object(self.engine, '_tmdb_json', new_callable=AsyncMock) as request, \
                    patch.object(self.engine, '_optional_ai_map', new_callable=AsyncMock) as translate:
                self.resolve(**values)
                request.assert_not_awaited()
                translate.assert_not_awaited()

    def test_disabled_english_fallback_still_translates_supplied_foreign_material(self):
        with patch.object(self.engine, '_tmdb_json', new_callable=AsyncMock) as request, \
                patch.object(self.engine, '_optional_ai_map', new_callable=AsyncMock,
                             return_value={'Overview': '原文中文翻译。'}) as translate:
            text, _ = self.resolve(primary='An actual English synopsis.', overview_english_fallback=False)
        self.assertEqual(text, '原文中文翻译。')
        request.assert_not_awaited()
        translate.assert_awaited_once()

    def test_good_chinese_overview_is_not_replaced_by_short_or_foreign_source(self):
        current = '已有可靠中文剧情。' * 10
        with patch.object(self.engine, '_tmdb_json', new_callable=AsyncMock) as request, \
                patch.object(self.engine, '_optional_ai_map', new_callable=AsyncMock) as translate:
            for primary in ('很短。', 'English synopsis only', '', '暂无简介', '暂无分集剧情'):
                self.assertEqual(self.resolve(primary=primary, current=current)[0], current)
        request.assert_not_awaited()
        translate.assert_not_awaited()

    def test_failed_or_unavailable_translation_does_not_invent_plot(self):
        for proposal in ('', 'Still English'):
            with patch.object(self.engine, '_tmdb_json', new_callable=AsyncMock,
                              return_value={'overview': 'Real source text'}), \
                    patch.object(self.engine, '_optional_ai_map', new_callable=AsyncMock,
                                 return_value={'Overview': proposal}):
                self.assertEqual(self.resolve(current='保留现有中文。')[0], '保留现有中文。')
        with patch.object(self.engine, '_tmdb_json', new_callable=AsyncMock, side_effect=ValueError('404')), \
                patch.object(self.engine, '_optional_ai_map', new_callable=AsyncMock) as translate:
            self.assertEqual(self.resolve(current='保留中文。')[0], '保留中文。')
            translate.assert_not_awaited()

    def test_write_phase_does_not_lock_fields_before_recheck(self):
        original = {'Overview': '原简介。', 'LockedFields': ['Name', 'Cast']}
        for enabled, text in ((False, '新中文简介。'), (True, '新中文简介。'),
                              (True, 'English only'), (True, '原简介。')):
            update = {**original, 'Overview': text}
            self.engine._lock_overview(original, update, {**self.options, 'overview_lock': enabled})
            self.assertEqual(update['LockedFields'], ['Name', 'Cast'])

    def test_readonly_preview_paginates_and_detects_same_season_duplicates_without_ai(self):
        entries = [
            {'Id': 'ep1', 'Name': '第一集', 'ParentIndexNumber': 1, 'IndexNumber': 1,
             'Overview': '同一季内重复使用的整剧简介内容。'},
            {'Id': 'ep2', 'Name': '第二集', 'ParentIndexNumber': 1, 'IndexNumber': 2,
             'Overview': '同一季内重复使用的整剧简介内容。'},
        ]
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.method, 'GET')
            if request.url.path.endswith('/Items/series'):
                return httpx.Response(200, json={'Id': 'series', 'Type': 'Series', 'Name': '测试剧'})
            offset = int(request.url.params.get('StartIndex', 0))
            return httpx.Response(200, json={'Items': entries[offset:offset + 1], 'TotalRecordCount': 2})

        client = httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler))
        with patch.object(self.engine, '_server', return_value=client), \
                patch.object(self.engine, '_user_id', new_callable=AsyncMock, return_value='admin'), \
                patch.object(self.engine, '_tmdb_json', new_callable=AsyncMock) as tmdb, \
                patch.object(self.engine, '_optional_ai_map', new_callable=AsyncMock) as ai:
            asyncio.run(self.engine._overview_preview('Q4::series', None, self.options))
        result = self.engine.status()['overview_preview']
        self.assertEqual(result['checked'], 3)
        self.assertEqual(len(requests), 3)
        self.assertIn('同季重复', result['items'][1]['reasons'])
        self.assertIn('同季重复', result['items'][2]['reasons'])
        tmdb.assert_not_awaited()
        ai.assert_not_awaited()
        self.assertFalse(self.storage)

    def test_preview_limits_display_but_counts_all_entries(self):
        async def read(*args):
            return {'Items': [{'Id': str(number), 'ParentIndexNumber': 1, 'IndexNumber': number,
                              'Overview': ''} for number in range(220)], 'TotalRecordCount': 220}
        client = httpx.AsyncClient(base_url='http://emby/')
        with patch.object(self.engine, '_server', return_value=client), \
                patch.object(self.engine, '_user_id', new_callable=AsyncMock, return_value='admin'), \
                patch.object(self.engine, '_item', new_callable=AsyncMock,
                             return_value={'Id': 'series', 'Type': 'Series', 'Name': '剧集'}), \
                patch.object(self.engine, '_json', side_effect=read):
            asyncio.run(self.engine._overview_preview('Q4::series', None, self.options))
        result = self.engine.status()['overview_preview']
        self.assertEqual(result['checked'], 221)
        self.assertEqual(len(result['items']), 200)
        self.assertTrue(result['truncated'])

    def test_preview_failure_is_not_reported_as_successful_empty_scan(self):
        client = httpx.AsyncClient(base_url='http://emby/')
        with patch.object(self.engine, '_server', return_value=client), \
                patch.object(self.engine, '_user_id', new_callable=AsyncMock, side_effect=ValueError('missing user')):
            asyncio.run(self.engine._overview_preview('Q4::series', None, self.options))
        result = self.engine.status()['overview_preview']
        self.assertEqual(result['checked'], 0)
        self.assertEqual(len(result['errors']), 1)

    def test_preview_starts_in_existing_exclusive_worker(self):
        with patch.object(self.engine, '_services', return_value={'Q4': object()}), \
                patch.object(self.engine, '_begin') as begin:
            self.engine.start_overview_preview('Q4::movie')
        self.assertEqual(begin.call_args.args[0], '媒体信息预检')
        with self.assertRaises(ValueError):
            self.engine.start_overview_preview('not-a-valid-key')

    def test_successful_http_response_is_read_back_and_user_data_is_not_written(self):
        saved = {}
        methods = []
        def handler(request):
            methods.append(request.method)
            if request.method == 'POST':
                saved.update(json.loads(request.content))
                return httpx.Response(204)
            return httpx.Response(200, json={**saved, 'People': [
                {**person, 'Id': 'generated', 'PrimaryImageTag': 'tag'} for person in saved['People']]})
        async def run():
            async with httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler)) as client:
                return await self.engine._save_item(client, 'ep1', {'Name': '分集', 'IndexNumber': 2}, {
                    'Name': '中文分集', 'IndexNumber': 2, 'Overview': '真实简介。',
                    'UserData': {'Played': True}, 'People': [{'Name': '演员', 'Type': 'Actor', 'Role': '饰 主角'}]},
                    user='admin', audit={'server': 'Q4', 'source': '电视猫'})
        self.assertTrue(asyncio.run(run()))
        self.assertEqual(methods, ['POST', 'GET'])
        self.assertNotIn('UserData', saved)
        record = self.engine.status()['records'][0]
        self.assertEqual(record['status'], 'updated')
        self.assertEqual(record['source'], '电视猫')
        self.assertEqual(record['server'], 'Q4')

    def test_http_success_with_unchanged_or_corrupt_fields_is_a_failure_without_replay(self):
        original = {'Name': '分集', 'Overview': '旧简介', 'IndexNumber': 2, 'ParentIndexNumber': 1,
                    'ProviderIds': {'Tmdb': '123'}}
        update = {**original, 'Overview': '新简介'}
        for field, wrong in (('Overview', '旧简介'), ('Name', ''), ('IndexNumber', 0),
                             ('ParentIndexNumber', 0), ('ProviderIds', {})):
            calls = []
            def handler(request):
                calls.append(request.method)
                return httpx.Response(204) if request.method == 'POST' else httpx.Response(200, json={**update, field: wrong})
            async def run():
                async with httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler)) as client:
                    await self.engine._save_item(client, '1', original, update, user='admin')
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, '回读不一致'):
                asyncio.run(run())
            self.assertEqual(calls, ['POST', 'GET', 'GET'])
            self.assertEqual(self.engine.status()['records'][0]['status'], 'failed')

    def test_delayed_consistency_retries_only_read(self):
        methods = []
        def handler(request):
            methods.append(request.method)
            if request.method == 'POST':
                return httpx.Response(204)
            return httpx.Response(200, json={'Name': '新标题' if len(methods) == 3 else '旧标题'})
        async def run():
            async with httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler)) as client:
                return await self.engine._save_item(client, '1', {'Name': '旧标题'}, {'Name': '新标题'}, user='admin')
        self.assertTrue(asyncio.run(run()))
        self.assertEqual(methods, ['POST', 'GET', 'GET'])

    def test_emby_studio_ids_order_and_rating_precision_do_not_cause_false_failures(self):
        methods = []
        update = {'Name': '剧集', 'Studios': [{'Name': '制作方'}, {'Name': '电视台'}],
                  'CommunityRating': 8.367, 'LockedFields': ['Cast', 'Overview'], 'Genres': ['剧情', '喜剧']}
        def handler(request):
            methods.append(request.method)
            if request.method == 'POST':
                self.assertEqual(json.loads(request.content)['Studios'], update['Studios'])
                return httpx.Response(204)
            return httpx.Response(200, json={**update, 'Studios': [
                {'Name': '电视台', 'Id': 101}, {'Name': '制作方', 'Id': 102}], 'CommunityRating': 8.4,
                'Genres': ['喜剧', '剧情'], 'LockedFields': ['Overview', 'Cast']})
        async def run():
            async with httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler)) as client:
                return await self.engine._save_item(client, '1', {'Name': '剧集'}, update, user='admin')
        self.assertTrue(asyncio.run(run()))
        self.assertEqual(methods, ['POST', 'GET'])
        self.assertEqual(self.engine.status()['records'][0]['status'], 'updated')

    def test_equivalent_studio_objects_and_precision_skip_repeated_write(self):
        original = {'Studios': [{'Name': 'Youku', 'Id': 44500}], 'CommunityRating': 8.4}
        update = {'Studios': [{'Name': 'Youku'}], 'CommunityRating': 8.367}
        client = SimpleNamespace(post=AsyncMock())
        self.assertFalse(asyncio.run(self.engine._save_item(client, '1', original, update)))
        client.post.assert_not_awaited()

    def test_visible_cast_subset_is_recorded_as_unverified_partial_without_aborting_or_replay(self):
        people = [{'Name': name, 'Type': 'Actor', 'Role': '饰 主角'} for name in ('甲', '乙', '丙')]
        methods = []
        def handler(request):
            methods.append(request.method)
            if request.method == 'POST':
                return httpx.Response(204)
            return httpx.Response(200, json={'Name': '剧集', 'People': [people[0], people[2]]})
        async def run():
            async with httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler)) as client:
                return await self.engine._save_item(client, '1', {'Name': '剧集'},
                    {'Name': '剧集', 'People': people}, user='admin')
        self.assertTrue(asyncio.run(run()))
        self.assertEqual(methods, ['POST', 'GET', 'GET'])
        record = self.engine.status()['records'][0]
        self.assertEqual(record['status'], 'partial')
        self.assertIn('尚未核实', record['message'])
        self.assertNotIn('回读验证通过', record['message'])

    def test_real_field_loss_or_cast_corruption_still_fails_without_replaying_write(self):
        people = [{'Name': name, 'Type': 'Actor', 'Role': '饰 主角'} for name in ('甲', '乙')]
        for wrong in ({'People': []}, {'People': [{**people[0], 'Role': '错误角色'}]},
                      {'People': list(reversed(people))}, {'Studios': [{'Name': '错误制作方'}]},
                      {'CommunityRating': 7.2}, {'LockedFields': []},
                      {'People': people[:1], 'Overview': '旧简介'}):
            with self.subTest(wrong=wrong):
                update = {'Name': '剧集', 'People': people, 'Studios': [{'Name': '制作方'}],
                          'CommunityRating': 8.367, 'LockedFields': ['Overview'], 'Overview': '新简介'}
                methods = []
                def handler(request):
                    methods.append(request.method)
                    return (httpx.Response(204) if request.method == 'POST' else
                            httpx.Response(200, json={**update, **wrong}))
                async def run():
                    async with httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler)) as client:
                        await self.engine._save_item(client, '1', {'Name': '剧集'}, update, user='admin')
                with self.assertRaisesRegex(ValueError, '不一致字段'):
                    asyncio.run(run())
                self.assertEqual(methods, ['POST', 'GET', 'GET'])
                self.assertEqual(self.engine.status()['records'][0]['status'], 'failed')

    def test_read_failure_reports_safe_reason_not_credentials_or_response_body(self):
        def handler(request):
            if request.method == 'POST':
                return httpx.Response(204)
            raise httpx.ReadTimeout('secret-token signed-url', request=request)
        async def run():
            async with httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler)) as client:
                await self.engine._save_item(client, '1', {'Name': '旧'}, {'Name': '新'}, user='admin')
        with self.assertRaisesRegex(ValueError, '回读异常：ReadTimeout'):
            asyncio.run(run())
        self.assertNotIn('secret-token', self.engine.status()['records'][0]['message'])

    def test_interrupted_write_with_only_partial_cast_readback_is_not_confirmed(self):
        people = [{'Name': name, 'Type': 'Actor', 'Role': '饰 主角'} for name in ('甲', '乙')]
        methods = []
        def handler(request):
            methods.append(request.method)
            if request.method == 'POST':
                raise httpx.ReadTimeout('secret-token', request=request)
            return httpx.Response(200, json={'Name': '剧集', 'People': people[:1]})
        async def run():
            async with httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler)) as client:
                await self.engine._save_item(client, '1', {'Name': '剧集'},
                    {'Name': '剧集', 'People': people}, user='admin')
        with self.assertRaisesRegex(ValueError, '写入响应中断'):
            asyncio.run(run())
        self.assertEqual(methods, ['POST', 'GET', 'GET'])
        self.assertEqual(self.engine.status()['records'][0]['status'], 'failed')

    def test_filtered_cast_does_not_abort_series_episode_enrichment(self):
        series = {'Id': 'series', 'Type': 'Series', 'Name': '测试剧', 'ProviderIds': {'Tmdb': '123'}}
        episode = {'Id': 'ep1', 'Type': 'Episode', 'Name': '第1集', 'Overview': '',
                   'ParentIndexNumber': 1, 'IndexNumber': 1}
        writes = []
        def emby_handler(request):
            if request.method == 'POST':
                payload = json.loads(request.content)
                writes.append(payload)
                if payload['Id'] == 'series':
                    series.update(payload)
                    series['People'] = series['People'][:1]
                else:
                    episode.update(payload)
                return httpx.Response(204)
            if request.url.path.endswith('/Items/series'):
                return httpx.Response(200, json=series)
            if request.url.path.endswith('/Items/ep1'):
                return httpx.Response(200, json=episode)
            return httpx.Response(200, json={'Items': [episode], 'TotalRecordCount': 1})
        def tmdb_handler(request):
            if '/season/' in request.url.path:
                return httpx.Response(200, json={'episodes': [
                    {'episode_number': 1, 'name': '第一集剧情标题', 'overview': '真实分集剧情。' * 10}]})
            return httpx.Response(200, json={'name': '测试剧', 'overview': '真实整剧剧情。' * 30,
                'production_companies': [{'name': '制作方'}], 'aggregate_credits': {'cast': [
                    {'name': name, 'profile_path': '/avatar.jpg', 'order': index, 'roles': [{'character': '主角'}]}
                    for index, name in enumerate(('演员甲', '演员乙'))]}})
        emby = httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://tmdb/3/', transport=httpx.MockTransport(tmdb_handler))
        with patch.object(self.engine, '_server', return_value=emby), \
                patch.object(self.engine, '_tmdb_client', return_value=tmdb), \
                patch.object(self.engine, '_user_id', new_callable=AsyncMock, return_value='admin'), \
                patch.object(self.engine, '_supplement_baike_cast', new_callable=AsyncMock,
                             side_effect=lambda cast, *args: (cast, [])), \
                patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'secret'):
            asyncio.run(self.engine._enrich('Q4', 'series', 'all', DEFAULT_ENRICH_CONFIG))
        self.assertEqual([entry['Id'] for entry in writes], ['series', 'ep1'])
        self.assertEqual(writes[0]['Studios'], [{'Name': '制作方'}])
        self.assertEqual(episode['Name'], '第一集剧情标题')
        records = self.engine.status()['records']
        self.assertEqual(records[0]['status'], 'updated')
        self.assertEqual(records[1]['status'], 'partial')

    def test_write_http_error_is_recorded_once_without_sensitive_url(self):
        methods = []
        def handler(request):
            methods.append(request.method)
            return httpx.Response(400)
        async def run():
            async with httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler)) as client:
                await self.engine._save_item(client, '1', {'Name': 'old'}, {'Name': '新'}, user='admin')
        with self.assertRaises(httpx.HTTPStatusError):
            asyncio.run(run())
        self.assertEqual(methods, ['POST'])
        record = self.engine.status()['records'][0]
        self.assertEqual(record['status'], 'failed')
        self.assertNotIn('http://', record['message'])

    def test_no_change_skips_write_and_records_unresolved_quality(self):
        client = SimpleNamespace(post=AsyncMock(), get=AsyncMock())
        item = {'Name': '剧集', 'Overview': ''}
        self.assertFalse(asyncio.run(self.engine._save_item(client, '1', item, item,
            audit={'issues': ['缺失']})))
        client.post.assert_not_awaited()
        client.get.assert_not_awaited()
        self.assertEqual(self.engine.status()['records'][0]['status'], 'partial')

    def test_records_are_bounded_persistent_and_reloaded(self):
        for number in range(205):
            self.engine._record_enrichment(str(number), 'skipped', {'Name': '标题'}, [], {}, '未变化')
        reloaded = DataEnrichment(self.engine.owner)
        self.assertEqual(len(reloaded.status()['records']), 200)
        self.assertEqual(reloaded.status()['records'][0]['id'], '204')
        self.assertEqual(reloaded.status()['records'][-1]['id'], '5')

    def test_movie_fallback_translation_defers_lock_until_recheck(self):
        stored = {'Id': 'movie', 'Type': 'Movie', 'Name': '测试电影', 'Overview': '',
                  'ProviderIds': {'Tmdb': '123'}, 'LockedFields': ['Name']}
        tmdb_requests = []
        methods = []
        def emby_handler(request):
            methods.append(request.method)
            if request.method == 'POST':
                stored.update(json.loads(request.content))
                return httpx.Response(204)
            return httpx.Response(200, json=stored)
        def tmdb_handler(request):
            tmdb_requests.append((request.url.path, request.url.params['language']))
            return httpx.Response(200, json={'title': '测试电影', 'overview':
                'Actual movie synopsis.' if request.url.params['language'] == 'en-US' else ''})
        emby = httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://tmdb/3/', transport=httpx.MockTransport(tmdb_handler))
        with patch.object(self.engine, '_server', return_value=emby), \
                patch.object(self.engine, '_tmdb_client', return_value=tmdb), \
                patch.object(self.engine, '_user_id', new_callable=AsyncMock, return_value='admin'), \
                patch.object(self.engine, '_optional_ai_map', new_callable=AsyncMock,
                             return_value={'Overview': '忠于原文的电影简介。'}), \
                patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'secret'):
            asyncio.run(self.engine._enrich('Q4', 'movie', 'metadata', {**self.options, 'overview_lock': True}))
        self.assertEqual(stored['Overview'], '忠于原文的电影简介。')
        self.assertEqual(stored['LockedFields'], ['Name'])
        self.assertEqual(methods, ['GET', 'POST', 'GET'])
        self.assertEqual(tmdb_requests, [('/3/movie/123', 'zh-CN'), ('/3/movie/123', 'en-US')])
        self.assertEqual(self.engine.status()['records'][0]['source'], 'TMDB 英文原文翻译')

    def test_episode_fallback_translates_only_that_episode_and_verifies_numbers(self):
        series = {'Id': 'series', 'Type': 'Series', 'Name': '测试剧', 'ProviderIds': {'Tmdb': '123'}}
        stored = {'Id': 'ep7', 'Type': 'Episode', 'Name': '中文标题', 'Overview': '',
                  'ParentIndexNumber': 2, 'IndexNumber': 7}
        tmdb_paths = []
        def emby_handler(request):
            if request.method == 'POST':
                stored.update(json.loads(request.content))
                return httpx.Response(204)
            if request.url.path.endswith('/Items/series'):
                return httpx.Response(200, json=series)
            if request.url.path.endswith('/Items/ep7'):
                return httpx.Response(200, json=stored)
            return httpx.Response(200, json={'Items': [stored], 'TotalRecordCount': 1})
        def tmdb_handler(request):
            tmdb_paths.append(request.url.path)
            if request.url.path.endswith('/episode/7'):
                self.assertEqual(request.url.params['language'], 'en-US')
                return httpx.Response(200, json={'overview': 'Actual episode seven plot.'})
            if '/season/' in request.url.path:
                return httpx.Response(200, json={'episodes': [{'episode_number': 7, 'name': '中文标题', 'overview': ''}]})
            return httpx.Response(200, json={'name': '测试剧'})
        emby = httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(emby_handler))
        tmdb = httpx.AsyncClient(base_url='https://tmdb/3/', transport=httpx.MockTransport(tmdb_handler))
        with patch.object(self.engine, '_server', return_value=emby), \
                patch.object(self.engine, '_tmdb_client', return_value=tmdb), \
                patch.object(self.engine, '_user_id', new_callable=AsyncMock, return_value='admin'), \
                patch.object(self.engine, '_web_episode_plots', new_callable=AsyncMock, return_value={}), \
                patch.object(self.engine, '_optional_ai_map', new_callable=AsyncMock,
                             return_value={'Overview': '第七集真实剧情的翻译。'}), \
                patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'secret'):
            asyncio.run(self.engine._enrich('Q4', 'series', 'episodes', {**self.options, 'overview_lock': True}))
        self.assertEqual(stored['Overview'], '第七集真实剧情的翻译。')
        self.assertEqual((stored['ParentIndexNumber'], stored['IndexNumber']), (2, 7))
        self.assertNotIn('LockedFields', stored)
        self.assertEqual(tmdb_paths.count('/3/tv/123/season/2/episode/7'), 1)

    def test_real_web_episode_plot_precedes_english_translation_and_replaces_placeholder(self):
        for initial in ('暂无分集剧情', '同一季重复使用的简介文本。'):
            with self.subTest(initial=initial):
                series = {'Id': 'series', 'Type': 'Series', 'Name': '测试剧', 'ProviderIds': {'Tmdb': '123'}}
                stored = {'Id': 'ep1', 'Type': 'Episode', 'Name': '中文标题', 'Overview': initial,
                          'ParentIndexNumber': 1, 'IndexNumber': 1}
                duplicate = {**stored, 'Id': 'ep2', 'IndexNumber': 2}
                def emby_handler(request):
                    if request.method == 'POST':
                        if request.url.path.endswith('/ep1'):
                            stored.update(json.loads(request.content))
                        else:
                            duplicate.update(json.loads(request.content))
                        return httpx.Response(204)
                    if request.url.path.endswith('/Items/series'):
                        return httpx.Response(200, json=series)
                    if request.url.path.endswith('/Items/ep1'):
                        return httpx.Response(200, json=stored)
                    if request.url.path.endswith('/Items/ep2'):
                        return httpx.Response(200, json=duplicate)
                    return httpx.Response(200, json={'Items': [dict(stored), dict(duplicate)], 'TotalRecordCount': 2})
                def tmdb_handler(request):
                    self.assertEqual(request.url.params['language'], 'zh-CN')
                    if '/season/' in request.url.path:
                        return httpx.Response(200, json={'episodes': [
                            {'episode_number': number, 'name': '中文标题', 'overview': ''} for number in (1, 2)]})
                    return httpx.Response(200, json={'name': '测试剧'})
                emby = httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(emby_handler))
                tmdb = httpx.AsyncClient(base_url='https://tmdb/3/', transport=httpx.MockTransport(tmdb_handler))
                with patch.object(self.engine, '_server', return_value=emby), \
                        patch.object(self.engine, '_tmdb_client', return_value=tmdb), \
                        patch.object(self.engine, '_user_id', new_callable=AsyncMock, return_value='admin'), \
                        patch.object(self.engine, '_web_episode_plots', new_callable=AsyncMock, return_value={
                            1: {'overview': '真实剧情。', 'source': '电视猫'},
                            2: {'overview': '另一集的真实剧情。', 'source': '百度百科'}}), \
                        patch.object(self.engine, '_optional_ai_map', new_callable=AsyncMock) as translate, \
                        patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'secret'):
                    asyncio.run(self.engine._enrich('Q4', 'series', 'episodes', self.options))
                self.assertEqual(stored['Overview'], '真实剧情。')
                translate.assert_not_awaited()
                self.assertEqual(self.engine.status()['records'][1]['source'], '电视猫')


if __name__ == '__main__':
    unittest.main()
