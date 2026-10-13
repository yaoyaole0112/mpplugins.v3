import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from emetools.data_enrichment import DataEnrichment
from emetools.emby_guard import ENRICHMENT_SLOT, EmbyClient, REQUEST_SLOTS, reserve_delay
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
