import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from emetools.data_enrichment import DataEnrichment, DEFAULT_ENRICH_CONFIG


class AnomalyRepairTests(unittest.TestCase):
    def setUp(self):
        self.storage = {}
        self.engine = DataEnrichment(SimpleNamespace(get_data=self.storage.get,
            save_data=lambda key, value: self.storage.__setitem__(key, value)))
        self.options = dict(DEFAULT_ENRICH_CONFIG)
        self.parent = {'Id': 'series', 'Type': 'Series', 'Name': '测试剧',
                       'People': [{'Name': '演员', 'Type': 'Actor', 'Role': '饰 角色'}]}

    def check(self, field, status='缺失', detail=''):
        return {'field': field, 'status': status, 'detail': detail}

    def preview(self, collected):
        async def run(series_id, libraries, options, plan=None):
            self.assertEqual(series_id, 'Q4::series')
            self.assertIsNone(libraries)
            if plan is not None:
                plan.update(collected)
            self.engine.state['overview_preview'] = {'errors': [], 'issues': 2, 'unknown': 1}
        return run

    def test_field_whitelist_preserves_normal_metadata_providers_and_locks(self):
        original = {'Name': '正常名称', 'Overview': '原简介', 'People': [{'Name': '原演员'}],
                    'Genres': ['剧情'], 'ProviderIds': {'Tmdb': '123'}, 'LockedFields': ['Name', 'Cast']}
        proposal = {'Name': '错误新名称', 'Overview': '补全简介', 'People': [], 'Genres': [],
                    'ProviderIds': {}, 'LockedFields': ['Overview']}
        update = self.engine._scoped_update(original, proposal, {'Overview'})
        self.assertEqual(update, {**original, 'Overview': '补全简介', 'LockedFields': ['Cast', 'Name', 'Overview']})
        self.assertEqual(original['Overview'], '原简介')
        self.assertEqual(self.engine._scoped_update(original, proposal, {'People'})['LockedFields'], ['Name'])

    def test_item_outside_scope_never_posts(self):
        self.engine._repair_scope = {'Q4::other': {'Overview'}}
        client = SimpleNamespace(post=AsyncMock())
        saved = asyncio.run(self.engine._save_item(client, 'series', {'Name': '原名'}, {'Name': '新名'},
                                                    audit={'server': 'Q4'}))
        self.assertFalse(saved)
        client.post.assert_not_awaited()

    def test_scope_filters_real_post_and_readback(self):
        original = {'Id': 'ep', 'Name': '正常标题', 'Overview': '', 'People': [{'Name': '原演员'}]}
        proposal = {**original, 'Name': '不应覆盖', 'Overview': '可靠中文简介', 'People': []}
        self.engine._repair_scope = {'Q4::ep': {'Overview'}}
        writes = []

        def handler(request):
            if request.method == 'POST':
                import json
                writes.append(json.loads(request.content))
                return httpx.Response(204)
            return httpx.Response(200, json={**original, 'Overview': '可靠中文简介'})

        async def run():
            async with httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler)) as client:
                return await self.engine._save_item(client, 'ep', original, proposal, user='admin', audit={'server': 'Q4'})
        self.assertTrue(asyncio.run(run()))
        self.assertEqual(writes, [{**original, 'Overview': '可靠中文简介'}])

    def test_fresh_scope_overrides_stale_precheck_and_does_not_change_ai_settings(self):
        self.engine.state['overview_preview'] = {'items': [{'id': 'OTHER::wrong'}]}
        collected = {'Q4::series': (self.parent, []), 'Q4::ep': ({'Id': 'ep'}, [self.check('分集标题')])}

        async def enrich(name, identifier, mode, options):
            self.assertEqual((name, identifier, mode), ('Q4', 'series', 'episodes'))
            self.assertEqual(self.engine._repair_scope, {'Q4::ep': {'Name'}})
            self.assertEqual(options['ai_title'], self.options['ai_title'])

        with patch.object(self.engine, '_overview_preview', side_effect=self.preview(collected)) as preview, \
                patch.object(self.engine, '_enrich', side_effect=enrich):
            asyncio.run(self.engine._repair_anomalies('Q4::series', self.options))
        self.assertEqual(preview.await_count, 2)
        self.assertIsNone(self.engine._repair_scope)
        self.assertIsNone(self.engine._repair_failures)
        self.assertEqual(self.engine.state['repair_result']['remaining'], 2)

    def test_images_only_do_not_need_tmdb_and_failed_image_does_not_stop_next(self):
        collected = {'Q4::series': (self.parent, []), **{
            f'Q4::ep{number}': ({'Id': f'ep{number}', 'Path': f'/media/ep{number}.strm'}, [self.check('分集图片')])
            for number in range(2)}}
        with patch.object(self.engine, '_overview_preview', side_effect=self.preview(collected)), \
                patch.object(self.engine, '_enrich', new_callable=AsyncMock) as enrich, \
                patch.object(self.engine, '_repair_preview', new_callable=AsyncMock,
                             side_effect=[ValueError('failed'), None]) as images:
            asyncio.run(self.engine._repair_anomalies('Q4::series', self.options))
        enrich.assert_not_awaited()
        self.assertEqual(images.await_count, 2)
        self.assertEqual(images.await_args.kwargs, {'rescan': False})
        self.assertEqual(len(self.engine.state['repair_result']['failures']), 1)

    def test_remote_missing_image_syncs_existing_normal_thumb_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'episode.strm')
            Path(path[:-5] + '-thumb.jpg').write_bytes(b'existing normal image')
            requests = []
            def handler(request):
                requests.append((request.method, request.url.path))
                return httpx.Response(204)
            client = httpx.AsyncClient(base_url='http://emby/', transport=httpx.MockTransport(handler))
            with patch.object(self.engine, '_server', return_value=client), \
                    patch.object(self.engine, '_safe_strm', return_value=path), \
                    patch.object(self.engine, '_preview_status', return_value='keep'), \
                    patch.object(self.engine, '_generate_preview', new_callable=AsyncMock) as generate, \
                    patch.object(self.engine, 'scan_preview', new_callable=AsyncMock) as scan:
                asyncio.run(self.engine._repair_preview('Q4::series', [('Q4::ep', {'path': path, 'status': 'missing'})],
                                                       False, rescan=False))
            generate.assert_not_awaited()
            scan.assert_not_awaited()
            self.assertEqual(requests, [('POST', '/Items/ep/Refresh')])
            self.assertEqual(self.engine.state['preview_result'], {'ok': 1, 'fail': 0})

    def test_media_requires_explicit_authorization_and_does_not_mutate_global_selection(self):
        collected = {'Q4::series': ({**self.parent, 'Path': '/media/movie.strm'},
                                  [self.check('媒体信息', detail='缺少大小、时长')])}
        sentinel = [{'id': 'unrelated'}]
        self.engine._mi_selection = sentinel
        for allowed in (False, True):
            with patch.object(self.engine, '_overview_preview', side_effect=self.preview(collected)), \
                    patch.object(self.engine, '_safe_strm', return_value='/media/movie.strm'), \
                    patch.object(self.engine, '_fill_mediainfo', new_callable=AsyncMock) as fill:
                asyncio.run(self.engine._repair_anomalies('Q4::series', self.options, allowed))
            if allowed:
                self.assertEqual(fill.await_args.kwargs['selection'][0]['id'], 'series')
            else:
                fill.assert_not_awaited()
            self.assertIs(self.engine._mi_selection, sentinel)

    def test_precheck_errors_block_all_writes(self):
        async def incomplete(*args):
            self.engine.state['overview_preview'] = {'errors': ['读取失败']}
        with patch.object(self.engine, '_overview_preview', side_effect=incomplete), \
                patch.object(self.engine, '_enrich', new_callable=AsyncMock) as enrich:
            with self.assertRaisesRegex(ValueError, '预检未完成'):
                asyncio.run(self.engine._repair_anomalies('Q4::series', self.options))
        enrich.assert_not_awaited()

    def test_removed_selected_media_does_not_report_empty_success(self):
        with patch.object(self.engine, '_overview_preview', side_effect=self.preview({})), \
                patch.object(self.engine, '_enrich', new_callable=AsyncMock) as enrich:
            with self.assertRaisesRegex(ValueError, '所选媒体已失效'):
                asyncio.run(self.engine._repair_anomalies('Q4::series', self.options))
        enrich.assert_not_awaited()

    def test_unknown_checks_are_not_blindly_repaired(self):
        collected = {'Q4::series': (self.parent, [self.check('演职员', '待核实'), self.check('音视频流', '待核实')])}
        with patch.object(self.engine, '_overview_preview', side_effect=self.preview(collected)), \
                patch.object(self.engine, '_enrich', new_callable=AsyncMock) as enrich:
            asyncio.run(self.engine._repair_anomalies('Q4::series', self.options))
        enrich.assert_not_awaited()

    def test_save_failure_is_isolated_only_during_anomaly_repair(self):
        self.engine._repair_failures = []
        with patch.object(self.engine, '_save_item_impl', new_callable=AsyncMock, side_effect=ValueError('failed')):
            saved = asyncio.run(self.engine._save_item(None, 'ep', {}, {}))
            self.assertFalse(saved)
            self.assertEqual(len(self.engine._repair_failures), 1)
            self.engine._repair_failures = None
            with self.assertRaises(ValueError):
                asyncio.run(self.engine._save_item(None, 'ep', {}, {}))

    def test_movie_repairs_do_not_propagate_to_episodes_or_enable_ai(self):
        movie = {**self.parent, 'Type': 'Movie'}
        collected = {'Q4::series': (movie, [self.check('演职员')])}
        async def enrich(name, identifier, mode, options):
            self.assertEqual(mode, 'credits')
            self.assertFalse(options['ai_enabled'])
            self.assertFalse(options['ai_title'])
            self.assertEqual(self.engine._repair_scope, {'Q4::series': {'People'}})
        with patch.object(self.engine, '_overview_preview', side_effect=self.preview(collected)), \
                patch.object(self.engine, '_enrich', side_effect=enrich):
            asyncio.run(self.engine._repair_anomalies('Q4::series', self.options))

    def test_real_episode_enrichment_skips_healthy_episodes_and_normal_fields(self):
        parent = {**self.parent, 'ProviderIds': {'Tmdb': '123'}}
        episodes = [{'Id': f'ep{number}', 'ParentIndexNumber': 1, 'IndexNumber': number,
                     'Name': '第1集' if number == 1 else '健康标题', 'Overview': '正常简介。'} for number in (1, 2)]
        self.engine._repair_scope = {'Q4::ep1': {'Name'}}
        read_items, written = [], []
        async def item(client, user, identifier):
            read_items.append(identifier)
            return dict(parent if identifier == 'series' else next(entry for entry in episodes if entry['Id'] == identifier))
        async def tmdb(client, path, params):
            if '/season/' in path:
                return {'episodes': [{'episode_number': number, 'name': '可靠分集标题', 'overview': '来源简介。'} for number in (1, 2)]}
            return {'name': '测试剧'}
        async def save(client, identifier, original, update, **kwargs):
            written.append((identifier, self.engine._scoped_update(original, update, self.engine._repair_scope[f'Q4::{identifier}'])))
            return True
        @asynccontextmanager
        async def client(*args):
            yield SimpleNamespace()
        with patch.object(self.engine, '_server', side_effect=client), \
                patch.object(self.engine, '_tmdb_client', side_effect=client), \
                patch.object(self.engine, '_user_id', new_callable=AsyncMock, return_value='admin'), \
                patch.object(self.engine, '_item', side_effect=item), \
                patch.object(self.engine, '_json', new_callable=AsyncMock, return_value={'Items': episodes, 'TotalRecordCount': 2}), \
                patch.object(self.engine, '_tmdb_json', side_effect=tmdb), \
                patch.object(self.engine, '_save_item', side_effect=save), \
                patch.object(self.engine, '_optional_ai_map', new_callable=AsyncMock) as ai, \
                patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test'):
            asyncio.run(self.engine._enrich_impl('Q4', 'series', 'episodes', {**self.options, 'overview_episode_min': 0}))
        self.assertEqual(read_items, ['series', 'ep1'])
        self.assertEqual(written, [('ep1', {**episodes[0], 'Name': '可靠分集标题'})])
        ai.assert_not_awaited()

    def test_failed_metadata_still_runs_image_repairs_and_final_precheck(self):
        collected = {'Q4::series': (self.parent, [self.check('类型')]),
                     'Q4::ep': ({'Id': 'ep', 'Path': '/media/ep.strm'}, [self.check('分集图片')])}
        with patch.object(self.engine, '_overview_preview', side_effect=self.preview(collected)) as preview, \
                patch.object(self.engine, '_enrich', new_callable=AsyncMock, side_effect=ValueError('failed')), \
                patch.object(self.engine, '_repair_preview', new_callable=AsyncMock) as image:
            asyncio.run(self.engine._repair_anomalies('Q4::series', self.options))
        image.assert_awaited_once()
        self.assertEqual(preview.await_count, 2)
        self.assertEqual(len(self.engine.state['repair_result']['failures']), 1)

    def test_scoped_probe_does_not_trigger_global_tasks_or_scan(self):
        @asynccontextmanager
        async def server(name):
            yield SimpleNamespace()
        selection = [{'id': 'ep', 'server': 'Q4', 'path': '/media/ep.strm', 'missing_size': True, 'missing_runtime': True}]
        with patch.object(self.engine, '_server', side_effect=server), \
                patch.object(self.engine, '_existing_mediainfo', new_callable=AsyncMock, return_value={}), \
                patch.object(self.engine, '_probe', new_callable=AsyncMock, return_value={'Size': 100, 'RunTimeTicks': 10000}), \
                patch.object(self.engine, '_verify_local_emby', new_callable=AsyncMock), \
                patch.object(self.engine, '_json', new_callable=AsyncMock, return_value=[]), \
                patch.object(self.engine, '_write_emby_database', return_value=(1, '/backup')) as write, \
                patch.object(self.engine, '_scheduled_task', new_callable=AsyncMock) as task, \
                patch.object(self.engine, '_check_mediainfo', new_callable=AsyncMock) as scan, \
                patch('emetools.data_enrichment.asyncio.sleep', new_callable=AsyncMock):
            asyncio.run(self.engine._fill_mediainfo(selection=selection))
        write.assert_called_once_with({'ep': {'Size': 100, 'RunTimeTicks': 10000}})
        task.assert_not_awaited()
        scan.assert_not_awaited()

    def test_episode_cast_only_does_not_fetch_seasons_or_episode_plots(self):
        parent = {**self.parent, 'ProviderIds': {'Tmdb': '123'}}
        episode = {'Id': 'ep', 'ParentIndexNumber': 1, 'IndexNumber': 1, 'Name': '正常标题', 'Overview': '正常简介'}
        self.engine._repair_scope = {'Q4::ep': {'People'}}
        @asynccontextmanager
        async def client(*args):
            yield SimpleNamespace()
        with patch.object(self.engine, '_server', side_effect=client), \
                patch.object(self.engine, '_tmdb_client', side_effect=client), \
                patch.object(self.engine, '_user_id', new_callable=AsyncMock, return_value='admin'), \
                patch.object(self.engine, '_item', new_callable=AsyncMock, side_effect=[parent, episode]), \
                patch.object(self.engine, '_json', new_callable=AsyncMock, return_value={'Items': [episode], 'TotalRecordCount': 1}), \
                patch.object(self.engine, '_tmdb_json', new_callable=AsyncMock, return_value={'name': '测试剧'}) as tmdb, \
                patch.object(self.engine, '_web_episode_plots', new_callable=AsyncMock) as plots, \
                patch.object(self.engine, '_save_item', new_callable=AsyncMock, return_value=True) as save, \
                patch('emetools.data_enrichment.settings.TMDB_API_KEY', 'test'):
            asyncio.run(self.engine._enrich_impl('Q4', 'series', 'episodes', {**self.options, 'episode_cast': True}))
        self.assertEqual(tmdb.await_count, 1)
        plots.assert_not_awaited()
        self.assertEqual(save.await_args.args[3], {**episode, 'People': parent['People']})

    def test_scoped_probe_never_overwrites_known_fields_or_stream_information(self):
        @asynccontextmanager
        async def server(name):
            yield SimpleNamespace()
        selection = [{'id': 'ep', 'server': 'Q4', 'path': '/media/ep.strm', 'missing_size': True, 'missing_runtime': False}]
        with patch.object(self.engine, '_server', side_effect=server), \
                patch.object(self.engine, '_existing_mediainfo', new_callable=AsyncMock, return_value={}), \
                patch.object(self.engine, '_probe', new_callable=AsyncMock, return_value={
                    'Size': 100, 'RunTimeTicks': 99999, 'Container': 'mp4', 'Width': 800}), \
                patch.object(self.engine, '_verify_local_emby', new_callable=AsyncMock), \
                patch.object(self.engine, '_json', new_callable=AsyncMock, return_value=[]), \
                patch.object(self.engine, '_write_emby_database', return_value=(1, '/backup')) as write, \
                patch('emetools.data_enrichment.asyncio.sleep', new_callable=AsyncMock):
            asyncio.run(self.engine._fill_mediainfo(selection=selection))
        write.assert_called_once_with({'ep': {'Size': 100}})

    def test_internal_plan_is_not_limited_by_200_visible_rows(self):
        episodes = [{'Id': f'ep{number}', 'ParentIndexNumber': 1, 'IndexNumber': number,
                     'Name': '第1集', 'ImageTags': {}} for number in range(1, 206)]
        @asynccontextmanager
        async def server(name):
            yield SimpleNamespace()
        async def page(client, path, params):
            offset = params['StartIndex']
            return {'Items': episodes[offset:offset + 100], 'TotalRecordCount': len(episodes)}
        plan = {}
        with patch.object(self.engine, '_server', side_effect=server), \
                patch.object(self.engine, '_user_id', new_callable=AsyncMock, return_value='admin'), \
                patch.object(self.engine, '_item', new_callable=AsyncMock, return_value=self.parent), \
                patch.object(self.engine, '_json', side_effect=page):
            asyncio.run(self.engine._overview_preview('Q4::series', None, self.options, plan))
        self.assertEqual(len(plan), 206)
        self.assertEqual(len(self.engine.state['overview_preview']['items']), 200)
        self.assertIn('Q4::ep205', plan)
        self.assertTrue(self.engine.state['overview_preview']['truncated'])


if __name__ == '__main__':
    unittest.main()
