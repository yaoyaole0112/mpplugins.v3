import asyncio
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
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
        notification_helper = patch('emetools.NotificationHelper')
        self.notification_helper = notification_helper.start()
        self.notification_helper.return_value.get_service.return_value = None
        self.addCleanup(notification_helper.stop)
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

    def command(self, **overrides):
        data = {'channel': NotificationChannel.Telegram, 'source': '管理员Bot', 'user': '123'}
        data.update(overrides)
        self.plugin._run_tool_command_serial('emetools_cleanfiles', data)
        return data

    def test_command_replies_once_with_private_shared_confirmation(self):
        with patch.object(self.plugin, '_send_tool_notice') as broadcast:
            self.command()
        broadcast.assert_not_called()
        self.plugin.chain.post_message.assert_called_once()
        reply = self.plugin.chain.post_message.call_args.args[0]
        self.assertEqual((reply.source, reply.userid), ('管理员Bot', '123'))
        token = reply.buttons[0][0]['callback_data'].split(':')[1]
        self.assertEqual(self.plugin._get_cleanup_status(token)['state'], 'pending')
        self.assertIn('两处共用一次性确认', reply.text)
        self.client.delete_files.assert_not_called()
        self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.assertEqual(self.plugin._get_cleanup_status(token)['state'], 'completed')
        self.client.delete_files.assert_called_once_with(['222'])
        self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.client.delete_files.assert_called_once()
        with self.assertRaises(HTTPException) as repeated:
            asyncio.run(self.plugin._action_serial(ToolAction(operation='cleanup_confirm', token=token)))
        self.assertEqual(repeated.exception.status_code, 400)
        self.assertEqual(self.plugin._get_cleanup_status(token)['state'], 'completed')
        self.client.delete_files.assert_called_once()

    def test_command_works_when_routine_plugin_notifications_are_disabled(self):
        self.conf.switchs = []
        self.plugin._notifications['enabled'] = False
        self.command()
        reply = self.plugin.chain.post_message.call_args.args[0]
        token = reply.buttons[0][0]['callback_data'].split(':')[1]
        self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.client.delete_files.assert_called_once_with(['222'])

    def test_command_rejects_non_admin_before_preview(self):
        self.command(user='999')
        self.client.list_files_in_dir.assert_not_called()
        self.assertIn('没有清理权限', self.plugin.chain.post_message.call_args.args[0].text)
        self.assertFalse(self.plugin._pending)

    def test_command_confirmation_cannot_be_used_from_another_bot_or_user(self):
        self.command()
        token = next(iter(self.plugin._pending))
        with patch.object(self.plugin, '_telegram_command_sources', return_value={
                '其他Bot': {'TELEGRAM_ADMINS': '123'}, '管理员Bot': {'TELEGRAM_ADMINS': '456'}}):
            self.plugin._handle_file_cleanup_confirmation(token, True, {**self.data, 'source': '其他Bot'})
            self.plugin._handle_file_cleanup_confirmation(token, True, {
                **self.data, 'userid': '456', 'original_chat_id': '456'})
        self.assertIn(token, self.plugin._pending)
        self.client.delete_files.assert_not_called()

    def test_command_rechecks_admin_permission_after_queue_wait(self):
        self.command()
        token = next(iter(self.plugin._pending))
        def run(key, label, callback, manual=False):
            with patch.object(self.plugin, '_telegram_command_sources', return_value={}):
                return callback()
        with patch.object(self.plugin._p115_tasks, 'run', side_effect=run):
            self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.client.delete_files.assert_not_called()

    def test_command_empty_preview_sends_no_confirmation(self):
        self.client.list_files_in_dir.return_value = []
        self.command()
        reply = self.plugin.chain.post_message.call_args.args[0]
        self.assertIn('均为空', reply.text)
        self.assertIsNone(reply.buttons)
        self.assertFalse(self.plugin._pending)

    def test_command_cancel_invalidates_web_confirmation(self):
        self.command()
        token = next(iter(self.plugin._pending))
        self.plugin._handle_file_cleanup_confirmation(token, False, self.data)
        self.assertEqual(self.plugin._get_cleanup_status(token)['state'], 'cancelled')
        self.assertNotIn(token, self.plugin._pending)
        self.assertNotIn(token, self.plugin._cleanup_command_origins)
        self.client.delete_files.assert_not_called()

    def test_command_confirmation_in_group_cannot_delete(self):
        self.command()
        token = next(iter(self.plugin._pending))
        self.plugin._handle_file_cleanup_confirmation(token, True, {**self.data, 'original_chat_id': '-100123'})
        self.client.delete_files.assert_not_called()
        self.assertIn(token, self.plugin._pending)

    def test_command_failed_preview_sends_no_confirmation(self):
        with patch.object(self.plugin, '_cleanup_preview', return_value={'ok': False, 'message': '目录读取失败'}):
            self.command()
        reply = self.plugin.chain.post_message.call_args.args[0]
        self.assertEqual(reply.text, '目录读取失败')
        self.assertIsNone(reply.buttons)
        self.assertFalse(self.plugin._pending)

    def test_command_failed_confirmation_delivery_invalidates_token(self):
        self.plugin.chain.post_message.side_effect = [RuntimeError('delivery failed'), None]
        self.command()
        self.assertFalse(self.plugin._pending)
        self.assertFalse(self.plugin._cleanup_command_origins)
        self.client.delete_files.assert_not_called()

    def request(self):
        response = self.plugin._cleanup_request()
        self.assertTrue(response['pending'])
        return response['token']

    def test_web_confirmation_updates_original_telegram_message_processing_and_completed(self):
        sender = MagicMock()
        sender.send_msg.return_value = {'success': True, 'message_id': 789, 'chat_id': 123}
        self.notification_helper.return_value.get_service.return_value = SimpleNamespace(instance=sender)
        token = self.request()
        sender.send_msg.assert_called_once()
        self.plugin.chain.post_message.assert_not_called()
        result = asyncio.run(self.plugin._action_serial(ToolAction(operation='cleanup_confirm', token=token)))
        self.assertTrue(result['ok'])
        edits = [call.args[0] for call in self.plugin.chain.post_message.call_args_list
                 if call.args[0].original_message_id == 789]
        self.assertEqual([message.buttons[0][0]['text'] for message in edits], ['⌛️清理中', '🗑️清理完成'])
        self.assertTrue(all(message.source == '管理员Bot' and str(message.original_chat_id) == '123' for message in edits))
        self.client.delete_files.assert_called_once_with(['222'])
        self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.client.delete_files.assert_called_once()

    def test_web_failure_updates_original_notification_without_completed_state(self):
        sender = MagicMock()
        sender.send_msg.return_value = {'success': True, 'message_id': 790, 'chat_id': 123}
        self.notification_helper.return_value.get_service.return_value = SimpleNamespace(instance=sender)
        token = self.request()
        self.client.list_files_in_dir.return_value = [{'fid': '333', 'size': 1}]
        result = asyncio.run(self.plugin._action_serial(ToolAction(operation='cleanup_confirm', token=token)))
        self.assertFalse(result['ok'])
        edits = [call.args[0] for call in self.plugin.chain.post_message.call_args_list
                 if call.args[0].original_message_id == 790]
        self.assertEqual([message.buttons[0][0]['text'] for message in edits], ['⌛️清理中', '清理已结束'])
        self.client.delete_files.assert_not_called()

    def test_web_queue_updates_original_notification_before_cleanup_starts(self):
        sender = MagicMock()
        sender.send_msg.return_value = {'success': True, 'message_id': 793, 'chat_id': 123}
        self.notification_helper.return_value.get_service.return_value = SimpleNamespace(instance=sender)
        token = self.request()
        release = threading.Event()
        started = threading.Event()
        self.addCleanup(release.set)
        def hold():
            started.set()
            release.wait(5)
        self.plugin._p115_tasks.submit('hold', 'hold', hold)
        self.assertTrue(started.wait(2))
        response = asyncio.run(self.plugin.action(ToolAction(operation='cleanup_confirm', token=token)))
        self.assertTrue(response['queued'])
        self.assertEqual(self.plugin._get_cleanup_status(token)['state'], 'processing')
        self.client.delete_files.assert_not_called()
        message = self.plugin.chain.post_message.call_args.args[0]
        self.assertEqual(message.original_message_id, 793)
        self.assertEqual(message.buttons[0][0]['text'], '⌛️清理中')
        self.plugin._handle_file_cleanup_confirmation(token, True, {**self.data, 'original_message_id': 793})
        job = self.plugin._p115_tasks._jobs[response['job_id']]
        release.set()
        self.assertTrue(job['done'].wait(3))
        self.assertEqual(self.plugin._get_cleanup_status(token)['state'], 'completed')
        self.client.delete_files.assert_called_once_with(['222'])
        edits = [call.args[0] for call in self.plugin.chain.post_message.call_args_list
                 if call.args[0].original_message_id == 793]
        self.assertEqual([message.buttons[0][0]['text'] for message in edits], ['⌛️清理中', '🗑️清理完成'])

    def test_late_queue_update_cannot_overwrite_completed_status(self):
        token = self.request()
        self.plugin._set_cleanup_status(token, 'completed')
        self.assertFalse(self.plugin._set_cleanup_status(token, 'processing', expected_state='pending'))
        self.assertEqual(self.plugin._get_cleanup_status(token)['state'], 'completed')

    def test_command_confirmation_can_be_completed_from_web(self):
        sender = MagicMock()
        sender.send_msg.return_value = {'success': True, 'message_id': 795, 'chat_id': 123}
        self.notification_helper.return_value.get_service.return_value = SimpleNamespace(instance=sender)
        self.command()
        self.assertTrue(sender.send_msg.call_args.kwargs['private_delivery'])
        token = next(iter(self.plugin._pending))
        result = asyncio.run(self.plugin._action_serial(ToolAction(operation='cleanup_confirm', token=token)))
        self.assertTrue(result['ok'])
        edits = [call.args[0] for call in self.plugin.chain.post_message.call_args_list
                 if call.args[0].original_message_id == 795]
        self.assertEqual([message.buttons[0][0]['text'] for message in edits], ['⌛️清理中', '🗑️清理完成'])

    def test_new_preview_keeps_consumed_task_message_for_completion(self):
        sender = MagicMock()
        sender.send_msg.return_value = {'success': True, 'message_id': 796, 'chat_id': 123}
        self.notification_helper.return_value.get_service.return_value = SimpleNamespace(instance=sender)
        token = self.request()
        self.plugin._consume(token, 'cleanup')
        self.plugin._set_cleanup_status(token, 'processing')
        self.request()
        self.plugin.chain.post_message.reset_mock()
        self.plugin._set_cleanup_status(token, 'completed')
        message = self.plugin.chain.post_message.call_args.args[0]
        self.assertEqual(message.original_message_id, 796)
        self.assertEqual(message.buttons[0][0]['text'], '🗑️清理完成')

    def test_telegram_confirmation_updates_other_original_messages_once(self):
        sender = MagicMock()
        sender.send_msg.return_value = {'success': True, 'message_id': 101, 'chat_id': 123}
        self.notification_helper.return_value.get_service.return_value = SimpleNamespace(instance=sender)
        token = self.request()
        self.plugin._cleanup_message_refs[token].append({**self.data, 'original_message_id': 794})
        self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        for message_id in (101, 794):
            edits = [call.args[0] for call in self.plugin.chain.post_message.call_args_list
                     if call.args[0].original_message_id == message_id]
            self.assertEqual([message.buttons[0][0]['text'] for message in edits], ['⌛️清理中', '🗑️清理完成'])
        self.client.delete_files.assert_called_once_with(['222'])

    def test_edit_failure_does_not_repeat_or_prevent_cleanup(self):
        sender = MagicMock()
        sender.send_msg.return_value = {'success': True, 'message_id': 791, 'chat_id': 123}
        self.notification_helper.return_value.get_service.return_value = SimpleNamespace(instance=sender)
        token = self.request()
        self.plugin.chain.post_message.side_effect = RuntimeError('edit failed')
        self.assertTrue(self.plugin._cleanup_confirm(token)['ok'])
        self.client.delete_files.assert_called_once_with(['222'])
        self.assertEqual(self.plugin._get_cleanup_status(token)['state'], 'completed')

    def test_repeated_processing_status_does_not_repeat_edits(self):
        sender = MagicMock()
        sender.send_msg.return_value = {'success': True, 'message_id': 792, 'chat_id': 123}
        self.notification_helper.return_value.get_service.return_value = SimpleNamespace(instance=sender)
        token = self.request()
        self.plugin._set_cleanup_status(token, 'processing')
        self.plugin._set_cleanup_status(token, 'processing')
        self.plugin.chain.post_message.assert_called_once()

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
        message_count = self.plugin.chain.post_message.call_count
        self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.client.delete_files.assert_called_once()
        self.assertEqual(self.plugin.chain.post_message.call_count, message_count)
        self.assertEqual(self.plugin._get_cleanup_status(token)['state'], 'completed')
        self.assertNotIn(token, self.plugin._pending)
        self.assertNotIn(token, self.plugin._cleanup_telegram_sources)
        edited = next(call.args[0] for call in self.plugin.chain.post_message.call_args_list
                      if getattr(call.args[0], 'original_message_id', None) == 101
                      and getattr(call.args[0], 'buttons', None)
                      and call.args[0].buttons[0][0]['text'] == '🗑️清理完成')
        self.assertEqual(edited.original_message_id, 101)
        self.assertEqual(edited.original_chat_id, '123')
        self.assertEqual(edited.buttons, [[{
            'text': '🗑️清理完成', 'callback_data': f'[PLUGIN]EmeTools|files:{token}:p'}]])
        self.assertIn('已删除', edited.text)
        self.assertIn('📁111', edited.text)
        self.assertNotIn('清理文件', edited.text)

    def test_confirmation_merges_result_without_new_completion_message(self):
        token = self.request()
        self.plugin.chain.post_message.reset_mock()
        self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        messages = [call.args[0] for call in self.plugin.chain.post_message.call_args_list]
        new_messages = [message for message in messages if not message.original_message_id]
        self.assertEqual(len(new_messages), 0)
        self.assertEqual(sum('已删除' in message.text for message in messages), 1)
        self.assertFalse(any('详细结果见下方' in message.text or '另行通知' in message.text for message in messages))

    def test_failed_cleanup_does_not_show_completed_button(self):
        token = self.request()
        self.client.delete_files.return_value = {'state': False, 'msg': '删除失败'}
        self.plugin.chain.post_message.reset_mock()
        self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        messages = [call.args[0] for call in self.plugin.chain.post_message.call_args_list]
        self.assertFalse(any(message.buttons and message.buttons[0][0]['text'] == '🗑️清理完成'
                             for message in messages))
        self.assertEqual(self.plugin._get_cleanup_status(token)['state'], 'failed')

    def test_completed_status_button_is_inert(self):
        token = self.request()
        self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        self.plugin.chain.post_message.reset_mock()
        data = {**self.data, 'plugin_id': 'EmeTools', 'text': f'files:{token}:p'}
        with patch('emetools.threading.Thread') as thread:
            self.plugin.scheduled_confirmation_action(SimpleNamespace(event_data=data))
        thread.assert_not_called()
        self.plugin.chain.post_message.assert_not_called()
        self.client.delete_files.assert_called_once()

    def test_telegram_approval_immediately_replaces_choices_with_processing_button(self):
        token = self.request()
        self.plugin.chain.post_message.reset_mock()
        queued = {'queued': True, 'job_id': 'queued-job'}
        with patch.object(self.plugin._p115_tasks, 'run', return_value=queued):
            self.plugin._handle_file_cleanup_confirmation(token, True, self.data)
        edited = next(call.args[0] for call in self.plugin.chain.post_message.call_args_list
                      if getattr(call.args[0], 'original_message_id', None) == 101
                      and getattr(call.args[0], 'buttons', None))
        self.assertEqual(edited.buttons, [[{
            'text': '⌛️清理中', 'callback_data': f'[PLUGIN]EmeTools|files:{token}:p'}]])
        self.assertEqual(self.plugin._get_cleanup_status(token)['state'], 'processing')
        self.plugin.chain.post_message.assert_called_once()

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
