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
from unittest.mock import patch
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from emetools.data_enrichment import DB_SCRIPT, DEFAULT_ENRICH_CONFIG, DataEnrichment, _docker_job, validate_enrich_config


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

    def test_tmdb_client_uses_moviepilot_domain_and_https_proxy(self):
        with patch('emetools.data_enrichment.settings.TMDB_API_DOMAIN', 'api.tmdb.org'), \
             patch('emetools.data_enrichment.get_runtime_setting', return_value={
                 'http': 'http://proxy.example:1080', 'https': 'http://proxy.example:1080'}), \
             patch('emetools.data_enrichment.httpx.AsyncClient') as client:
            self.enrichment._tmdb_client()
        self.assertEqual(client.call_args.kwargs['base_url'], 'https://api.tmdb.org/3/')
        self.assertEqual(client.call_args.kwargs['proxy'], 'http://proxy.example:1080')
        self.assertFalse(client.call_args.kwargs['trust_env'])

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
