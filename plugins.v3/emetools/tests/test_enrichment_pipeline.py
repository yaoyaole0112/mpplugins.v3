import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from emetools.data_enrichment import DataEnrichment, DEFAULT_ENRICH_CONFIG
from emetools.enrichment_llm import translate_with_moviepilot
from emetools.media_precheck import foreign_text


class EnrichmentPipelineTests(unittest.TestCase):
    def setUp(self):
        self.engine = DataEnrichment(SimpleNamespace())
        portrait_patch = patch.object(self.engine, '_sync_cast_portraits', new_callable=AsyncMock)
        portrait_patch.start()
        self.addCleanup(portrait_patch.stop)
        portrait_ai_patch = patch('emetools.data_enrichment.search_portraits_with_moviepilot',
                                  new_callable=AsyncMock, return_value={})
        portrait_ai_patch.start()
        self.addCleanup(portrait_ai_patch.stop)
        self.options = {**DEFAULT_ENRICH_CONFIG, 'ai_enabled': True}

    def test_primary_cast_retains_identity_order_and_existing_chinese_role(self):
        primary = [{'id': 101, 'name': '演员甲', 'original_name': 'Actor One',
                    'role': '原有中文角色', 'order': 0, 'profile_path': '/primary.jpg'}]
        secondary = [{'name': '演员甲', 'original_name': 'Actor One', 'role': '另一中文角色'},
                     {'name': '演员乙', 'role': '配角', 'img': '/secondary.jpg'}]
        result = self.engine._complete_primary_cast(primary, secondary)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], primary[0])
        self.assertEqual(result[1]['name'], '演员乙')
        self.assertEqual(result[1]['profile_path'], '/secondary.jpg')
        self.assertGreater(result[1]['order'], result[0]['order'])
        self.assertEqual(len(primary), 1)

    def test_actor_selection_explains_filters_and_preserves_explicit_limit(self):
        cast = [{'name': '演员甲', 'profile_path': '/a.jpg', 'order': 0},
                {'name': '演员乙', 'profile_path': '', 'order': 1},
                {'name': '演员丙', 'profile_path': '/c.jpg', 'order': 2}]
        result = self.engine._select_actors(cast, {**self.options, 'max_actors': 1})
        self.assertEqual([actor['name'] for actor in result], ['演员甲'])
        self.assertIn('无头像过滤 1 人', self.engine.status()['log'][-1])
        result = self.engine._select_actors(cast, {**self.options, 'no_avatar': False})
        self.assertEqual(len(result), 3)

    def test_scoped_actor_repair_defers_new_cast_lock_until_readback_checks(self):
        item = {'Id': 'movie', 'Type': 'Movie', 'Name': '测试电影', 'ProviderIds': {'Tmdb': '123'},
                'LockedFields': ['Name']}
        self.engine._repair_scope = {'Q4::movie': {'People'}}
        detail = {'credits': {'cast': [{'name': '中文演员', 'character': '中文角色',
                                        'profile_path': '/a.jpg'}]}}
        with patch.object(self.engine, '_tmdb_json', new_callable=AsyncMock, return_value=detail), \
                patch.object(self.engine, '_douban_data', new_callable=AsyncMock, return_value={}), \
                patch.object(self.engine, '_tvmao_cast', new_callable=AsyncMock, return_value=[]), \
                patch.object(self.engine, '_supplement_baike_cast', new_callable=AsyncMock,
                             return_value=([{'name': '中文演员', 'role': '中文角色', 'profile_path': '/a.jpg'}], [])), \
                patch.object(self.engine, '_save_item', new_callable=AsyncMock, return_value=True) as save:
            asyncio.run(self.engine._enrich_movie(None, None, 'movie', item, 'credits',
                {**self.options, 'cast_lock_min': 1}, 'test-only', server='Q4'))
        proposal = save.call_args.args[3]
        self.assertEqual(proposal['LockedFields'], ['Name'])
        self.assertEqual(len(proposal['People']), 1)

    def test_douban_fetches_full_pages_even_when_summary_cast_is_already_chinese(self):
        offsets = []
        def handler(request):
            if '/j/subject/' in request.url.path:
                return httpx.Response(200, json={'title': '测试剧', 'casts': [
                    {'name': '原中文演员', 'role': '原中文角色'}]})
            offset = int(request.url.params['start'])
            offsets.append(offset)
            actors = [{'id': str(index), 'name': f'演员{index}', 'character': '角色',
                       'avatar': {'normal': f'https://images.invalid/{index}.jpg'}}
                      for index in range(offset, min(offset + 100, 103))]
            return httpx.Response(200, json={'actors': actors})
        real_client = httpx.AsyncClient
        with patch('emetools.data_enrichment.httpx.AsyncClient', side_effect=lambda **kwargs:
                   real_client(transport=httpx.MockTransport(handler), **kwargs)):
            result = asyncio.run(self.engine._douban_data(
                {'Name': '测试剧', 'ProviderIds': {'Douban': '123'}}, include_cast=True))
        self.assertEqual(offsets, [0, 100])
        self.assertEqual(len(result['casts']), 104)
        self.assertEqual(result['casts'][102]['name'], '演员102')
        self.assertEqual(result['casts'][-1]['name'], '原中文演员')

    def test_complete_chinese_names_still_trigger_source_count_check(self):
        @asynccontextmanager
        async def client():
            yield SimpleNamespace()

        for media_type in ('Series', 'Movie'):
            for episode_cast in (False, True):
                with self.subTest(media_type=media_type, episode_cast=episode_cast):
                    parent = {'Id': 'series', 'Type': media_type, 'Name': '测试媒体',
                              'ProviderIds': {'Tmdb': '123'},
                              'People': [{'Name': '演员甲', 'Type': 'Actor', 'Role': '饰 主角'}]}
                    episode = {'Id': 'ep', 'Type': 'Episode', 'People': parent['People']}
                    collected = {'Q4::series': (parent, []), 'Q4::ep': (episode, [])}
                    source_cast = [{'name': '演员甲', 'profile_path': '/a.jpg',
                                    'roles': [{'character': '主角'}], 'character': '主角'},
                                   {'name': '演员乙', 'profile_path': '/b.jpg',
                                    'roles': [{'character': '配角'}], 'character': '配角'}]
                    detail = {'credits': {'cast': source_cast}, 'aggregate_credits': {'cast': source_cast}}
                    with patch.object(self.engine, '_tmdb_client', side_effect=client), \
                            patch.object(self.engine, '_tmdb_json', new_callable=AsyncMock,
                                         return_value=detail), \
                            patch.object(self.engine, '_douban_data', new_callable=AsyncMock,
                                         return_value={}), \
                            patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-only'):
                        result = asyncio.run(self.engine._check_cast_completeness(
                            'Q4::series', collected, {**self.options, 'episode_cast': episode_cast}))
                    self.assertIn('Q4::series', result)
                    self.assertEqual('Q4::ep' in result, episode_cast)

    def test_missing_confirmed_provider_does_not_guess_cast_completeness(self):
        collected = {'Q4::series': ({'Type': 'Series', 'People': []}, [])}
        with patch.object(self.engine, '_douban_data', new_callable=AsyncMock) as source:
            result = asyncio.run(self.engine._check_cast_completeness('Q4::series', collected, self.options))
        self.assertEqual(result, set())
        source.assert_not_awaited()

    def test_cast_readback_shortfall_stays_abnormal_and_prevents_clean_locking(self):
        self.engine._cast_expectations = {'Q4::series': 18}
        checks = [{'field': '演职员', 'status': '正常', 'detail': '可见 4 人，含外文 0 人'}]
        collected = {'Q4::series': ({'Id': 'series', 'People': [
            {'Name': f'演员{index}', 'Type': 'Actor', 'Role': '饰 配角'} for index in range(4)]}, checks)}
        self.engine.state['overview_preview'] = {'items': [{'id': 'Q4::series', 'checks': checks,
            'reasons': [], 'plan': '已检查项目正常'}], 'issues': 0, 'unknown': 0}
        self.engine._verify_cast_completeness(collected)
        result = self.engine.state['overview_preview']
        self.assertEqual(checks[0]['status'], '疑似异常')
        self.assertIn('至少 18 位演员，回读可见 4 位', checks[0]['detail'])
        self.assertEqual(result['issues'], 1)
        self.assertEqual(result['category_counts']['演职员'], 1)
        self.assertIn('演职员：疑似异常', result['items'][0]['reasons'])
        self.assertNotEqual(result['items'][0]['plan'], '已检查项目正常')

    def test_source_complete_but_avatar_filtered_cast_is_not_falsely_short(self):
        @asynccontextmanager
        async def client():
            yield SimpleNamespace()
        parent = {'Id': 'series', 'Type': 'Series', 'Name': '测试', 'ProviderIds': {'Tmdb': '123'},
                  'People': [{'Name': '演员甲', 'Type': 'Actor', 'Role': '饰 主角'}]}
        source_cast = [{'name': '演员甲', 'profile_path': '/a.jpg'},
                       {'name': '演员乙', 'profile_path': ''}]
        with patch.object(self.engine, '_tmdb_client', side_effect=client), \
                patch.object(self.engine, '_tmdb_json', new_callable=AsyncMock,
                             return_value={'aggregate_credits': {'cast': source_cast}}), \
                patch.object(self.engine, '_douban_data', new_callable=AsyncMock, return_value={}), \
                patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test-only'):
            result = asyncio.run(self.engine._check_cast_completeness(
                'Q4::series', {'Q4::series': (parent, [])}, self.options))
        self.assertEqual(result, set())
        self.assertEqual(self.engine._cast_expectations, {'Q4::series': 1})

    def test_ai_failure_is_sanitized_and_does_not_retry_another_paid_protocol(self):
        with patch('emetools.data_enrichment.llm_available', return_value=True), \
                patch('emetools.data_enrichment.llm_setting', return_value='test-only'), \
                patch('emetools.data_enrichment.translate_with_moviepilot', new_callable=AsyncMock,
                      side_effect=RuntimeError('https://private.invalid/secret-token')) as translate, \
                patch('emetools.data_enrichment.httpx.AsyncClient') as fallback:
            result = asyncio.run(self.engine._optional_ai_map({'Name': 'Foreign'}, '测试'))
        self.assertEqual(result, {})
        translate.assert_awaited_once()
        fallback.assert_not_called()
        self.assertNotIn('secret-token', ' '.join(self.engine.status()['log']))

    def test_only_foreign_people_fields_are_translated_without_inventing_roles(self):
        people = [{'Name': '中文演员', 'Role': 'Foreign Role', 'Type': 'Actor'},
                  {'Name': 'Foreign Actor', 'Role': '中文角色', 'Type': 'Actor'},
                  {'Name': '另一演员', 'Role': '', 'Type': 'Actor'}]
        translate = AsyncMock(return_value={'r0': '中文角色甲', 'n1': '演员乙'})
        with patch.object(self.engine, '_optional_ai_map', translate):
            asyncio.run(self.engine._translate_people(people, self.options, '测试剧', '剧集'))
        self.assertEqual(translate.call_args.args[0], {'r0': 'Foreign Role', 'n1': 'Foreign Actor'})
        self.assertEqual(people[0]['Name'], '中文演员')
        self.assertEqual(people[0]['Role'], '中文角色甲')
        self.assertEqual(people[1]['Name'], '演员乙')
        self.assertEqual(people[1]['Role'], '中文角色')
        self.assertEqual(people[2]['Role'], '')

    def test_studio_ai_fallback_respects_metadata_translation_switch(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                translate = AsyncMock(return_value={'0': '中文平台', '1': '中文制作公司'})
                with patch.object(self.engine, '_optional_ai_map', translate):
                    result = asyncio.run(self.engine._localize_labels(
                        ['Hulu', 'Goddard Textiles', '二十世纪电视'],
                        {**self.options, 'ai_metadata': enabled}, '工作室汉化'))
                self.assertEqual(result[-1], '二十世纪电视')
                if enabled:
                    self.assertEqual(result[:2], ['中文平台', '中文制作公司'])
                    translate.assert_awaited_once()
                else:
                    self.assertEqual(result[:2], ['Hulu', 'Goddard Textiles'])
                    translate.assert_not_awaited()

    def test_foreign_scripts_and_mixed_labels_are_detected_without_rejecting_known_acronyms(self):
        for value in ('Foreign Actor', '中文 English', '한국 배우', 'タイ語', 'ภาษาไทย'):
            with self.subTest(value=value):
                self.assertTrue(foreign_text(value))
        for value in ('中文演员', 'HBO电视台', '芒果TV', 'BBC电视台', ''):
            with self.subTest(value=value):
                self.assertFalse(foreign_text(value))

    def test_official_llm_routing_closes_model_after_success_or_failure(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                invoke = AsyncMock(side_effect=RuntimeError('test-only') if fails else None,
                    return_value=SimpleNamespace(content=[{'type': 'text', 'text': '{"a":"中文"}'}]))
                model = SimpleNamespace(ainvoke=invoke)
                with patch('app.agent.llm.helper.LLMHelper.get_llm', new_callable=AsyncMock,
                           return_value=model) as build, \
                        patch('app.agent.llm.helper.LLMHelper.close_llm', new_callable=AsyncMock) as close:
                    messages = [{'content': '只翻译已有字段'}, {'content': '{"a":"foreign"}'}]
                    if fails:
                        with self.assertRaises(RuntimeError):
                            asyncio.run(translate_with_moviepilot(messages))
                    else:
                        self.assertEqual(asyncio.run(translate_with_moviepilot(messages)), '{"a":"中文"}')
                    self.assertEqual(build.call_args.kwargs['web_search_mode'], 'disabled')
                    close.assert_awaited_once_with(model)
