import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from emetools import EmeTools, ToolAction
from app.schemas.system import NotificationConf
from app.schemas.types import MessageType, NotificationChannel


class FileCleanupTelegramTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.plugin = object.__new__(EmeTools)
        self.plugin.get_config = MagicMock(return_value={})
        self.plugin.get_data = MagicMock(return_value=None)
        self.plugin.save_data = MagicMock()
        with patch('emetools.missing_episodes.SubscribeChain', return_value=MagicMock()), \
                patch('emetools.missing_episodes.MediaServerHelper', return_value=MagicMock()):
            self.plugin.init_plugin({'strm_root': directory.name})
        self.plugin.chain = MagicMock()
        self.plugin._schedule['p115_cleanup']['dir_ids'] = ['111']
        self.client = MagicMock()
        self.client.__enter__.return_value = self.client
        self.client.list_files_in_dir.return_value = [{'fid': '222', 'size': 1024}]
        self.client.list_children.return_value = ([], [])
        self.client.delete_files.return_value = {'state': True}
        self.plugin._client = lambda: self.client
        self.conf = NotificationConf(name='管理员Bot', type='telegram', enabled=True,
                                     switchs=[MessageType.Plugin.value], config={'TELEGRAM_ADMINS': '123'})
        sources = patch('emetools.get_service_configs', return_value=[self.conf])
        sources.start()
        self.addCleanup(sources.stop)
        admin = patch('emetools.matches_channel_admin', side_effect=lambda channel, config, user:
                      bool(config) and config.get('TELEGRAM_ADMINS') == user)
        admin.start()
        self.addCleanup(admin.stop)
        self.data = {'channel': NotificationChannel.Telegram, 'source': '管理员Bot',
                     'userid': '123', 'original_chat_id': '123', 'original_message_id': 101}

    def request(self):
        response = self.plugin._cleanup_request()
        self.assertTrue(response['pending'])
        return response['token']

    def test_notification_has_buttons_and_shared_web_token(self):
        token = self.request()
        message = self.plugin.chain.post_message.call_args.args[0]
        self.assertIn('两处共用一次性确认', message.text)
        self.assertEqual(message.parse_mode, 'plain')
        self.assertEqual(message.buttons[0][0]['callback_data'], f'[PLUGIN]EmeTools|files:{token}:y')
        self.assertEqual(message.buttons[0][1]['callback_data'], f'[PLUGIN]EmeTools|files:{token}:n')
        self.assertLessEqual(len(message.buttons[0][0]['callback_data'].encode()), 64)
        self.assertIn(token, self.plugin._pending)
        self.client.delete_files.assert_not_called()

    def test_telegram_confirm_rechecks_and_deletes_once_with_private_result(self):
        token = self.request()
        self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.client.delete_files.assert_called_once_with(['222'])
        self.assertEqual(self.client.list_files_in_dir.call_count, 2)
        reply = self.plugin.chain.post_message.call_args.args[0]
        self.assertEqual((reply.source, reply.userid), ('管理员Bot', '123'))
        self.assertIn('1 个文件', reply.text)
        self.assertEqual(self.plugin._get_cleanup_status(token)['state'], 'completed')
        self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.client.delete_files.assert_called_once()
        self.assertNotIn(token, self.plugin._pending)
        self.assertNotIn(token, self.plugin._cleanup_telegram_sources)
        edited = next(call.args[0] for call in self.plugin.chain.post_message.call_args_list
                      if getattr(call.args[0], 'original_message_id', None) == 101
                      and getattr(call.args[0], 'buttons', None) is None)
        self.assertEqual(edited.original_message_id, 101)
        self.assertEqual(edited.original_chat_id, '123')
        self.assertIsNone(edited.buttons)

    def test_telegram_approval_immediately_replaces_choices_with_processing_button(self):
        token = self.request()
        queued = {'queued': True, 'job_id': 'queued-job'}
        with patch.object(self.plugin._p115_tasks, 'run', return_value=queued):
            self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        edited = next(call.args[0] for call in self.plugin.chain.post_message.call_args_list
                      if getattr(call.args[0], 'original_message_id', None) == 101
                      and getattr(call.args[0], 'buttons', None))
        self.assertEqual(edited.buttons, [[{
            'text': '⌛️清理中', 'callback_data': f'[PLUGIN]EmeTools|files:{token}:p'}]])
        self.assertEqual(self.plugin._get_cleanup_status(token)['state'], 'processing')

    def test_cancel_invalidates_web_confirmation_without_deletion(self):
        token = self.request()
        self.plugin._handle_file_cleanup_confirmation(token, False, self.data)
        self.assertEqual(self.plugin._get_cleanup_status(token)['state'], 'cancelled')
        self.client.delete_files.assert_not_called()
        response = asyncio.run(self.plugin.action(ToolAction(operation='cleanup_confirm', token=token)))
        self.assertFalse(response['ok'])
        self.client.delete_files.assert_not_called()

    def test_web_confirmation_invalidates_telegram_button(self):
        token = self.request()
        response = asyncio.run(self.plugin.action(ToolAction(operation='cleanup_confirm', token=token)))
        self.assertTrue(response['ok'])
        self.assertEqual(self.plugin._get_cleanup_status(token)['state'], 'completed')
        self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.client.delete_files.assert_called_once()

    def test_invalid_actor_chat_channel_or_bot_cannot_consume_token(self):
        token = self.request()
        variants = [{'userid': '456'}, {'original_chat_id': '-100123'}, {'source': '其他Bot'},
                    {'channel': 'wechat'}]
        for change in variants:
            with self.subTest(change=change):
                self.plugin._handle_file_cleanup_confirmation(token, True, {**self.data, **change})
                self.plugin._handle_file_cleanup_confirmation(token, False, {**self.data, **change})
                self.assertIn(token, self.plugin._pending)
        self.client.delete_files.assert_not_called()

    def test_bot_added_after_request_cannot_confirm_existing_token(self):
        token = self.request()
        with patch.object(self.plugin, '_telegram_confirmation_sources', return_value={
                '新Bot': {'TELEGRAM_ADMINS': '123'}}):
            self.plugin._handle_file_cleanup_confirmation(token, True, {**self.data, 'source': '新Bot'})
        self.assertIn(token, self.plugin._pending)
        self.client.delete_files.assert_not_called()

    def test_expired_or_disabled_confirmation_cannot_delete(self):
        token = self.request()
        created, kind, snapshot = self.plugin._pending[token]
        self.plugin._pending[token] = (created - 1801, kind, snapshot)
        self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.client.delete_files.assert_not_called()
        self.plugin._enabled = False
        self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.client.delete_files.assert_not_called()

    def test_directory_change_still_blocks_telegram_deletion(self):
        token = self.request()
        self.client.list_files_in_dir.return_value = [{'fid': '333', 'size': 2048}]
        self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.client.delete_files.assert_not_called()
        self.assertIn('内容已变化', self.plugin.chain.post_message.call_args.args[0].text)

    def test_no_admin_bot_keeps_page_only_confirmation(self):
        with patch('emetools.get_service_configs', return_value=[]):
            token = self.request()
        message = self.plugin.chain.post_message.call_args.args[0]
        self.assertIsNone(message.buttons)
        self.assertIn('需要配置', message.text)
        self.assertNotIn(token, self.plugin._cleanup_telegram_sources)
        self.assertTrue(self.plugin._cleanup_confirm(token)['ok'])

    def test_send_failure_keeps_usable_page_token(self):
        with patch.object(self.plugin, '_send_tool_notice', side_effect=RuntimeError('send failed')):
            token = self.request()
        self.assertIn(token, self.plugin._pending)
        self.assertTrue(self.plugin._cleanup_confirm(token)['ok'])

    def test_channel_configuration_failure_keeps_page_confirmation(self):
        with patch.object(self.plugin, '_telegram_confirmation_sources', side_effect=RuntimeError('not ready')):
            token = self.request()
        self.assertIsNone(self.plugin.chain.post_message.call_args.args[0].buttons)
        self.assertTrue(self.plugin._cleanup_confirm(token)['ok'])

    def test_cleanup_notification_uses_cleanup_switch_not_trash_switch(self):
        self.plugin._notifications['cleanup'] = False
        self.request()
        self.plugin.chain.post_message.assert_not_called()
        self.plugin._notifications['cleanup'] = True
        self.plugin._notifications['trash'] = False
        self.request()
        self.plugin.chain.post_message.assert_called_once()

    def test_file_callback_is_routed_to_background_worker(self):
        token = self.request()
        data = {**self.data, 'plugin_id': 'EmeTools', 'text': f'files:{token}:y'}
        with patch('emetools.threading.Thread') as thread:
            self.plugin.scheduled_confirmation_action(SimpleNamespace(event_data=data))
        self.assertEqual(thread.call_args.kwargs['target'], self.plugin._handle_file_cleanup_confirmation)
        self.assertEqual(thread.call_args.kwargs['args'], (token, True, data))
        thread.return_value.start.assert_called_once()

    def test_confirmation_uses_same_queue_key_as_web(self):
        token = self.request()
        with patch.object(self.plugin._p115_tasks, 'run', return_value={'ok': True, 'queued': True}) as queued:
            self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.assertEqual(queued.call_args.args[0], 'cleanup_confirm:' + token)
        self.assertTrue(queued.call_args.kwargs['manual'])
        self.client.delete_files.assert_not_called()

    def test_permissions_are_checked_again_after_queue_wait(self):
        token = self.request()

        def run(key, label, callback, manual=False):
            with patch.object(self.plugin, '_telegram_confirmation_sources', return_value={}):
                return callback()

        with patch.object(self.plugin._p115_tasks, 'run', side_effect=run):
            self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.assertIn(token, self.plugin._pending)
        self.client.delete_files.assert_not_called()

    def test_expiry_during_queue_wait_blocks_deletion(self):
        token = self.request()

        def run(key, label, callback, manual=False):
            created, kind, snapshot = self.plugin._pending[token]
            self.plugin._pending[token] = (created - 1801, kind, snapshot)
            return callback()

        with patch.object(self.plugin._p115_tasks, 'run', side_effect=run):
            self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.client.delete_files.assert_not_called()

    def test_cancellation_while_approval_is_queued_blocks_deletion(self):
        token = self.request()

        def run(key, label, callback, manual=False):
            self.plugin._handle_file_cleanup_confirmation(token, False, self.data)
            return callback()

        with patch.object(self.plugin._p115_tasks, 'run', side_effect=run):
            self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.client.delete_files.assert_not_called()
        self.assertNotIn(token, self.plugin._pending)
