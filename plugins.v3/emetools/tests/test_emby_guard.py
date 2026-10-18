import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from emetools.data_enrichment import DataEnrichment
from emetools.emby_guard import ENRICHMENT_SLOT, EmbyClient, ReadRetryClient, REQUEST_SLOTS, reserve_delay
from emetools.media_cleanup import DEFAULT_CONFIG, MediaCleanup, REFRESH_PENDING


class EmbyLoadTests(unittest.TestCase):
    def setUp(self):
        self.enrichment = DataEnrichment(SimpleNamespace())

    def test_unchanged_people_with_emby_generated_fields_do_not_write(self):
        original = {"Name": "剧集", "People": [{"Name": "演员", "Type": "Actor",
                    "Role": "饰 主角", "Id": "123", "PrimaryImageTag": "tag"}]}
        update = {"Name": "剧集", "People": [{"Name": "演员", "Type": "Actor", "Role": "饰 主角"}]}
        client = SimpleNamespace(post=AsyncMock())
        self.assertFalse(asyncio.run(self.enrichment._save_item(client, "1", original, update)))
        client.post.assert_not_called()

    def test_changed_item_writes_once_and_paces(self):
        response = Mock()
        client = SimpleNamespace(post=AsyncMock(return_value=response))
        with patch('emetools.data_enrichment.asyncio.sleep', new_callable=AsyncMock) as sleep:
            self.assertTrue(asyncio.run(self.enrichment._save_item(client, "1",
                             {"Name": "old"}, {"Name": "新标题"})))
        client.post.assert_awaited_once()
        sleep.assert_awaited_once_with(0.5)

    def test_read_disconnect_recovers_and_releases_slots_between_attempts(self):
        requests = []

        def handler(request):
            requests.append(request)
            if len(requests) < 3:
                raise httpx.RemoteProtocolError('signed-secret', request=request)
            return httpx.Response(200, json={'Name': '剧集'})

        async def check_backoff(seconds):
            if seconds in (1, 2):
                self.assertTrue(REQUEST_SLOTS.acquire(blocking=False))
                self.assertTrue(REQUEST_SLOTS.acquire(blocking=False))
                REQUEST_SLOTS.release()
                REQUEST_SLOTS.release()

        async def run():
            async with EmbyClient(active=self.enrichment._ensure_active, base_url='http://emby/',
                                  transport=httpx.MockTransport(handler)) as client:
                self.assertEqual((await client.get('Items/1')).json(), {'Name': '剧集'})

        with patch('emetools.emby_guard.reserve_delay', return_value=0), \
                patch('emetools.emby_guard.asyncio.sleep', side_effect=check_backoff):
            asyncio.run(run())
        self.assertEqual(len(requests), 3)

    def test_read_retries_are_bounded_and_errors_do_not_leak_secrets(self):
        requests = []

        def handler(request):
            requests.append(request)
            raise httpx.RemoteProtocolError('https://secret/?api_key=secret', request=request)

        async def run():
            async with ReadRetryClient(service_name='TMDB', base_url='https://tmdb/',
                                       transport=httpx.MockTransport(handler)) as client:
                with self.assertRaisesRegex(httpx.RemoteProtocolError, 'TMDB GET') as error:
                    await client.get('tv/1', params={'api_key': 'secret'})
                self.assertNotIn('secret', str(error.exception))

        with patch('emetools.emby_guard.asyncio.sleep', new_callable=AsyncMock):
            asyncio.run(run())
        self.assertEqual(len(requests), 3)

    def test_http_status_errors_are_not_retried(self):
        handler = Mock(return_value=httpx.Response(403))

        async def run():
            async with ReadRetryClient(transport=httpx.MockTransport(handler)) as client:
                self.assertEqual((await client.get('https://emby/Items')).status_code, 403)

        asyncio.run(run())
        handler.assert_called_once()

    def test_write_disconnect_is_not_replayed_and_saved_data_is_verified(self):
        methods = []

        def handler(request):
            methods.append(request.method)
            if request.method == 'POST':
                raise httpx.RemoteProtocolError('secret', request=request)
            self.assertEqual(request.url.path, '/Users/user123/Items/1')
            return httpx.Response(200, json={'Name': '新标题', 'People': [
                {'Id': '1', 'Name': '演员', 'Type': 'Actor', 'Role': '饰 主角'}]})

        async def run():
            async with EmbyClient(active=self.enrichment._ensure_active, base_url='http://emby/',
                                  transport=httpx.MockTransport(handler)) as client:
                self.assertTrue(await self.enrichment._save_item(client, '1',
                    {'Name': '旧标题', 'UserData': {'Played': False}},
                    {'Name': '新标题', 'UserData': {'Played': False},
                     'People': [{'Name': '演员', 'Type': 'Actor', 'Role': '饰 主角'}]}, user='user123'))

        with patch('emetools.emby_guard.asyncio.sleep', new_callable=AsyncMock), \
                patch('emetools.emby_guard.reserve_delay', return_value=0):
            asyncio.run(run())
        self.assertEqual(methods, ['POST', 'GET'])

    def test_unconfirmed_write_preserves_error_without_resubmitting(self):
        methods = []

        def handler(request):
            methods.append(request.method)
            if request.method == 'POST':
                raise httpx.ReadTimeout('secret', request=request)
            return httpx.Response(200, json={'Name': '旧标题'})

        async def run():
            async with EmbyClient(active=self.enrichment._ensure_active, base_url='http://emby/',
                                  transport=httpx.MockTransport(handler)) as client:
                with self.assertRaisesRegex(ValueError, '未能确认保存结果'):
                    await self.enrichment._save_item(client, '1', {'Name': '旧标题'}, {'Name': '新标题'}, user='user123')

        with patch('emetools.emby_guard.asyncio.sleep', new_callable=AsyncMock), \
                patch('emetools.emby_guard.reserve_delay', return_value=0):
            asyncio.run(run())
        self.assertEqual(methods, ['POST', 'GET'])

    def test_retry_stops_when_plugin_closes(self):
        def disconnect(request):
            raise httpx.RemoteProtocolError('disconnected', request=request)

        handler = Mock(side_effect=disconnect)

        async def close(seconds):
            if seconds == 1:
                self.enrichment.close()

        async def run():
            async with EmbyClient(active=self.enrichment._ensure_active,
                                  transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(asyncio.CancelledError):
                    await client.get('https://emby/Items')

        with patch('emetools.emby_guard.asyncio.sleep', side_effect=close), \
                patch('emetools.emby_guard.reserve_delay', return_value=0):
            asyncio.run(run())
        handler.assert_called_once()

    def test_closed_instance_cannot_write(self):
        self.enrichment.close()
        client = SimpleNamespace(post=AsyncMock())
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(self.enrichment._save_item(client, "1", {}, {"Name": "新"}))
        client.post.assert_not_called()

    def test_worker_lock_survives_instance_replacement(self):
        ENRICHMENT_SLOT.acquire()
        try:
            with self.assertRaisesRegex(ValueError, '正在运行'):
                self.enrichment._begin('测试', AsyncMock())
            self.assertFalse(self.enrichment.state['running'])
        finally:
            ENRICHMENT_SLOT.release()

    def test_transfer_burst_creates_only_one_resolver(self):
        with patch('emetools.data_enrichment.threading.Thread') as thread:
            self.assertTrue(self.enrichment.queue_import_by_tmdb('123'))
            self.assertFalse(self.enrichment.queue_import_by_tmdb('123'))
            thread.assert_called_once()
            self.enrichment._resolve_tmdb_import = Mock()
            thread.call_args.kwargs['target']()

    def test_request_pacing_separates_reads_and_writes(self):
        with patch('emetools.emby_guard.NEXT_REQUEST', 0), \
             patch('emetools.emby_guard.time.monotonic', return_value=100):
            self.assertEqual(reserve_delay(), 0)
            self.assertAlmostEqual(reserve_delay(write=True), 0.15)
            self.assertAlmostEqual(reserve_delay(), 0.65)

    def test_closed_client_releases_request_slots(self):
        async def run():
            client = EmbyClient(active=self.enrichment._ensure_active,
                                transport=httpx.MockTransport(lambda request: httpx.Response(200)))
            async with client:
                self.enrichment.close()
                with self.assertRaises(asyncio.CancelledError):
                    await client.get('https://example.org/emby/Items')
        asyncio.run(run())
        self.assertTrue(REQUEST_SLOTS.acquire(blocking=False))
        REQUEST_SLOTS.release()

    def test_cancellation_after_slot_acquired_releases_slot(self):
        checks = Mock(side_effect=[None, asyncio.CancelledError()])
        async def run():
            async with EmbyClient(active=checks, transport=httpx.MockTransport(
                    lambda request: httpx.Response(200))) as client:
                with self.assertRaises(asyncio.CancelledError):
                    await client.get('https://example.org/emby/Items')
        with patch('emetools.emby_guard.reserve_delay', return_value=0):
            asyncio.run(run())
        self.assertTrue(REQUEST_SLOTS.acquire(blocking=False))
        self.assertTrue(REQUEST_SLOTS.acquire(blocking=False))
        REQUEST_SLOTS.release()
        REQUEST_SLOTS.release()

    def test_cleanup_burst_requests_single_deferred_refresh(self):
        service = SimpleNamespace(instance=Mock())
        cleanup = MediaCleanup(SimpleNamespace(), DEFAULT_CONFIG)
        with patch('emetools.media_cleanup.MediaServerHelper') as helper, \
             patch('emetools.media_cleanup.threading.Timer') as timer:
            helper.return_value.get_services.return_value = {'audit-server': service}
            try:
                cleanup.refresh_emby()
                cleanup.refresh_emby()
                timer.assert_called_once()
                service.instance.refresh_root_library.assert_not_called()
                with patch('emetools.emby_guard.time.sleep'):
                    cleanup._refresh_server('audit-server', service.instance)
                service.instance.refresh_root_library.assert_called_once()
            finally:
                REFRESH_PENDING.pop('audit-server', None)
