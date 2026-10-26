import asyncio
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from emetools.cast_portraits import CastPortraits, portrait_url
from emetools.data_enrichment import DEFAULT_ENRICH_CONFIG
from emetools.enrichment_llm import search_portraits_with_moviepilot


class CastPortraitTests(unittest.TestCase):
    def setUp(self):
        self.read = AsyncMock()
        self.search = AsyncMock(return_value={})
        self.logs = []
        self.resolver = CastPortraits(self.read, self.search, self.logs.append)
        self.client = SimpleNamespace(base_url='https://tmdb.test/3/')
        self.item = {'Type': 'Series', 'Name': '测试剧', 'ProviderIds': {'Tmdb': '123'}}
        self.options = {**DEFAULT_ENRICH_CONFIG, 'ai_enabled': True}

    def complete(self, cast, **options):
        return asyncio.run(self.resolver.complete(cast, self.client, 'secret', self.item,
                                                  {**self.options, **options}))

    def test_existing_source_portraits_need_no_search(self):
        cast = [{'name': '演员甲', 'profile_path': '/actor.jpg'},
                {'name': '演员乙', 'profile_path': 'https://img1.doubanio.com/view/personage/x.jpg'}]
        self.assertEqual(self.complete(cast), cast)
        self.read.assert_not_awaited()
        self.search.assert_not_awaited()

    def test_movie_and_series_recover_the_same_source_portrait(self):
        self.read.return_value = {'id': 8, 'profile_path': '/real.jpg'}
        for media_type in ('Movie', 'Series'):
            self.item['Type'] = media_type
            result = self.complete([{'name': '演员甲', 'id': 8}])
            self.assertTrue(result[0]['profile_path'].endswith('/real.jpg'))

    def test_known_source_chinese_alias_precedes_ai_and_preserves_existing_portrait(self):
        self.read.return_value = {'id': 8, 'name': 'Actor One', 'also_known_as': ['演员甲'],
                                  'profile_path': '/other.jpg'}
        result = self.complete([{'name': 'Actor One', 'id': 8, 'profile_path': '/original.jpg'}])
        self.assertEqual(result[0]['name'], '演员甲')
        self.assertEqual(result[0]['profile_path'], '/original.jpg')
        self.search.assert_not_awaited()

    def test_inspection_never_invokes_ai(self):
        self.read.return_value = {'results': []}
        asyncio.run(self.resolver.complete([{'name': '演员甲'}], self.client, 'key',
                                           self.item, self.options, allow_ai=False))
        self.search.assert_not_awaited()

    def test_person_details_and_images_are_read_before_filtering_and_cached(self):
        self.read.return_value = {'id': 8, 'images': {'profiles': [{'file_path': '/real.jpg'}]}}
        cast = [{'id': 8, 'name': '演员甲', 'profile_path': ''}]
        for unused in range(2):
            result = self.complete(cast)
            self.assertTrue(result[0]['profile_path'].endswith('/real.jpg'))
        self.assertEqual(self.read.await_count, 1)
        self.assertEqual(cast[0]['profile_path'], '')

    def test_name_search_requires_same_media_and_unique_identity(self):
        self.read.side_effect = [
            {'results': [{'id': 8, 'name': '演员甲'}, {'id': 9, 'name': '演员甲'}]},
            {'id': 8, 'name': '演员甲', 'profile_path': '/wrong.jpg',
             'combined_credits': {'cast': [{'id': 999, 'media_type': 'tv'}]}},
            {'id': 9, 'name': '演员甲', 'profile_path': '/right.jpg',
             'combined_credits': {'cast': [{'id': 123, 'media_type': 'tv'}]}}]
        result = self.complete([{'name': '演员甲'}])
        self.assertEqual(result[0]['id'], 9)
        self.assertTrue(result[0]['profile_path'].endswith('/right.jpg'))

    def test_ambiguous_same_name_people_not_adopted(self):
        self.read.side_effect = [
            {'results': [{'id': 8, 'name': '演员甲'}, {'id': 9, 'name': '演员甲'}]},
            *[{'id': identifier, 'name': '演员甲', 'profile_path': '/a.jpg',
               'combined_credits': {'cast': [{'id': 123, 'media_type': 'tv'}]}}
              for identifier in (8, 9)]]
        result = self.complete([{'name': '演员甲'}], ai_portraits=False)
        self.assertFalse(result[0]['profile_path'])

    def test_ai_candidate_must_have_source_and_verified_filmography(self):
        self.read.side_effect = [
            {'results': []},
            {'id': 8, 'name': 'Actor One', 'also_known_as': ['演员甲'], 'profile_path': '/real.jpg',
             'combined_credits': {'cast': [{'id': 123, 'media_type': 'tv'}]}}]
        self.search.return_value = {'0': {'tmdb_id': 8, 'source': 'https://www.themoviedb.org/person/8'}}
        result = self.complete([{'name': '演员甲'}])
        self.assertTrue(result[0]['profile_path'].endswith('/real.jpg'))
        self.search.assert_awaited_once()

    def test_ai_cannot_supply_arbitrary_image_urls_or_change_existing_id(self):
        self.read.return_value = {'id': 8}
        self.search.return_value = {'0': {'tmdb_id': 9, 'source': 'https://www.themoviedb.org/person/9',
                                         'image': 'http://127.0.0.1/secret'}}
        result = self.complete([{'id': 8, 'name': '演员甲'}])
        self.assertFalse(result[0]['profile_path'])
        self.assertEqual(self.read.await_count, 1)

    def test_failed_ai_not_replayed_and_disabled_never_called(self):
        self.read.return_value = {'results': []}
        self.search.side_effect = ValueError('unavailable')
        self.complete([{'name': '演员甲'}])
        self.search.assert_awaited_once()
        self.search.reset_mock()
        self.complete([{'name': '演员甲'}], ai_enabled=False)
        self.search.assert_not_awaited()

    def test_source_query_budget_is_bounded(self):
        self.read.return_value = {'results': []}
        result = self.complete([{'name': f'演员{index}'} for index in range(80)], ai_portraits=False)
        self.assertEqual(len(result), 80)
        self.assertEqual(self.read.await_count, 40)

    def test_ai_search_uses_native_web_capability_and_closes_model(self):
        for fails in (False, True):
            model = SimpleNamespace(ainvoke=AsyncMock(
                side_effect=RuntimeError('secret-provider-output') if fails else None,
                return_value=SimpleNamespace(content='{"0":{"tmdb_id":8,"source":"https://www.themoviedb.org/person/8"}}')))
            with patch('app.agent.llm.helper.LLMHelper.get_llm', new_callable=AsyncMock,
                       return_value=model) as build, \
                    patch('app.agent.llm.helper.LLMHelper.close_llm', new_callable=AsyncMock) as close:
                if fails:
                    with self.assertRaises(ValueError) as caught:
                        asyncio.run(search_portraits_with_moviepilot(self.item, [{'name': '演员甲'}]))
                    self.assertNotIn('secret-provider-output', str(caught.exception))
                else:
                    result = asyncio.run(search_portraits_with_moviepilot(self.item, [{'name': '演员甲'}]))
                    self.assertEqual(result['0']['tmdb_id'], 8)
                self.assertEqual(build.call_args.kwargs['web_search_mode'], 'builtin')
                self.assertEqual(model.ainvoke.await_count, 1)
                close.assert_awaited_once_with(model)

    def test_unsupported_native_search_does_not_invoke_an_unsearched_model(self):
        with patch('app.agent.llm.helper.LLMHelper.get_llm', new_callable=AsyncMock,
                   side_effect=RuntimeError('unsupported')) as build:
            with self.assertRaises(ValueError):
                asyncio.run(search_portraits_with_moviepilot(self.item, [{'name': '演员甲'}]))
            build.assert_awaited_once()

    def test_image_url_whitelist(self):
        for value in ('http://image.tmdb.org/x.jpg', 'https://image.tmdb.org.evil.test/x.jpg',
                      'https://127.0.0.1/x.jpg', 'https://image.tmdb.org:bad/a.jpg',
                      'https://user:pass@image.tmdb.org/a.jpg', '//evil.test/a.jpg'):
            self.assertFalse(portrait_url(value))
        self.assertTrue(portrait_url('/actual.jpg'))
        for host in ('bkimg.cdn.bcebos.com', 'bkimg.bj.bcebos.com', 'bkimg.bdimg.com', 'image.tvmao.com'):
            self.assertTrue(portrait_url(f'https://{host}/actual.jpg'))

    def test_upload_is_verified_cached_and_does_not_overwrite(self):
        output = BytesIO()
        Image.new('RGB', (120, 180), 'red').save(output, 'JPEG')
        requests = []
        state = {'uploaded': False}

        def handle(request):
            requests.append(request)
            if request.url.host == 'image.tmdb.org':
                return httpx.Response(200, content=output.getvalue(), headers={'Content-Type': 'image/jpeg'})
            if request.url.path.startswith('/Persons/'):
                return httpx.Response(200, json={'Id': '42', 'Type': 'Person', 'Name': '演员甲'})
            if request.method == 'POST':
                state['uploaded'] = True
                raise httpx.ReadTimeout('interrupted', request=request)
            return httpx.Response(200, json=[{'ImageType': 'Primary'}] if state['uploaded'] else [])

        async def run():
            async with httpx.AsyncClient(base_url='https://emby.test/', transport=httpx.MockTransport(handle)) as emby, \
                    httpx.AsyncClient(transport=httpx.MockTransport(handle)) as images:
                pairs = [({'id': 8, 'profile_path': '/real.jpg'}, {'Name': '演员甲'})]
                self.assertEqual(await self.resolver.sync(emby, images, pairs, lambda: None), 1)
                self.assertEqual(await self.resolver.sync(emby, images, pairs, lambda: None), 0)
        asyncio.run(run())
        self.assertEqual(sum(request.method == 'POST' for request in requests), 1)
        self.assertTrue(any('未重放上传' in line for line in self.logs))

    def test_redirect_to_untrusted_host_rejected_without_request(self):
        requested = []

        def handle(request):
            requested.append(request)
            return httpx.Response(302, headers={'Location': 'http://127.0.0.1/secret'})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                with self.assertRaises(ValueError):
                    await self.resolver._download(client, 'https://image.tmdb.org/a.jpg')
        asyncio.run(run())
        self.assertEqual(len(requested), 1)

    def test_existing_portrait_or_conflicting_identity_never_uploaded(self):
        for extra in ({'ImageTags': {'Primary': 'existing'}}, {'ProviderIds': {'Tmdb': '999'}}):
            requests = []

            def handle(request):
                requests.append(request)
                return httpx.Response(200, json={'Id': '42', 'Type': 'Person', 'Name': '演员甲', **extra})

            async def run():
                async with httpx.AsyncClient(base_url='https://emby.test/', transport=httpx.MockTransport(handle)) as emby:
                    self.assertEqual(await self.resolver.sync(emby, None,
                        [({'id': 8, 'profile_path': '/real.jpg'}, {'Name': '演员甲'})], lambda: None), 0)
            asyncio.run(run())
            self.assertEqual(len(requests), 1)
