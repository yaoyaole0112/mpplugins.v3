import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from emetools import ToolAction
from tests import test_cleanup_telegram


class SharedConfirmationTests(unittest.TestCase):
    def setUp(self):
        test_cleanup_telegram.FileCleanupTelegramTests.setUp(self)
        self.sender = MagicMock()
        self.sender.send_msg.return_value = {'success': True, 'message_id': 789, 'chat_id': 123}
        self.notification_helper.return_value.get_service.return_value = SimpleNamespace(instance=self.sender)
        self.client.rb_list.return_value = {'count': 2, 'items': [
            {'id': 'one', 'file_size': '1024'}, {'id': 'two', 'file_size': '2048'}]}
        self.client.clear_recyclebin.return_value = {'state': True}
        self.plugin._schedule['p115_move']['rules'] = [
            {'src_id': '111', 'dst_id': '333', 'src_name': '源', 'dst_name': '目标', 'mode': 'copy'}]
        self.client.list_children.return_value = ([{'fid': '222', 'name': 'test.mkv', 'size': 1024}], [])
        self.client.copy_files.return_value = {'state': True}
        self.client.move_files.return_value = {'state': True}

    def api(self, operation, **kwargs):
        return asyncio.run(self.plugin.action(ToolAction(operation=operation, **kwargs)))

    def edits(self):
        return [call.args[0] for call in self.plugin.chain.post_message.call_args_list
                if call.args[0].original_message_id == 789]

    def command_tool(self, name):
        self.plugin._run_tool_command_serial(name, {
            'source': '管理员Bot', 'user': '123', 'channel': self.data['channel']})
        return next(iter(self.plugin._pending))

    def test_trash_web_confirm_updates_original_tg_and_cannot_repeat(self):
        result = self.api('trash_request')
        token = result['token']
        self.client.clear_recyclebin.assert_not_called()
        self.assertIn('不可恢复', self.sender.send_msg.call_args.kwargs['text'])
        self.assertTrue(self.api('trash_clear', token=token)['ok'])
        self.assertEqual([message.buttons[0][0]['text'] for message in self.edits()], ['⌛️清空中', '✅清空完成'])
        self.assertTrue(all(len(message.buttons[0]) == 1 for message in self.edits()))
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'completed')
        with self.assertRaises(HTTPException):
            self.api('trash_clear', token=token)
        self.client.clear_recyclebin.assert_called_once()

    def test_trash_tg_command_creates_private_confirmation_and_web_can_confirm(self):
        token = self.command_tool('emetools_cleartrash')
        self.client.clear_recyclebin.assert_not_called()
        self.assertTrue(self.sender.send_msg.call_args.kwargs['private_delivery'])
        self.assertEqual(self.sender.send_msg.call_args.kwargs['userid'], '123')
        self.assertEqual(self.api('pending')['confirmations'][0]['token'], token)
        self.plugin._handle_tool_confirmation(token, True, self.data)
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'completed')
        self.client.clear_recyclebin.assert_called_once()

    def test_trash_same_count_changed_identity_is_rejected(self):
        token = self.api('trash_request')['token']
        self.client.rb_list.return_value = {'count': 2, 'items': [{'id': 'new'}, {'id': 'other'}]}
        self.assertFalse(self.api('trash_clear', token=token)['ok'])
        self.client.clear_recyclebin.assert_not_called()
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'failed')

    def test_move_command_does_not_submit_until_confirmed(self):
        token = self.command_tool('emetools_move')
        self.client.copy_files.assert_not_called()
        self.client.move_files.assert_not_called()
        result = self.api('move_confirm', token=token)
        self.assertEqual(result['copied'], 1)
        self.client.copy_files.assert_called_once_with(['222'], '333')
        self.assertEqual([message.buttons[0][0]['text'] for message in self.edits()], ['⌛️转存中', '✅转存完成'])

    def test_changed_move_rules_cannot_use_previous_authorization(self):
        token = self.api('move_request')['token']
        self.plugin._schedule['p115_move']['rules'][0]['dst_id'] = '444'
        self.assertFalse(self.api('move_confirm', token=token)['ok'])
        self.client.copy_files.assert_not_called()
        self.client.move_files.assert_not_called()

    def test_changed_move_source_blocks_submit(self):
        token = self.api('move_request')['token']
        self.client.list_children.return_value = ([{'fid': '555', 'size': 1}], [])
        self.assertFalse(self.api('move_confirm', token=token)['ok'])
        self.client.copy_files.assert_not_called()

    def test_changed_copy_records_cannot_expand_authorized_transfer(self):
        token = self.api('move_request')['token']
        with patch.object(self.plugin, '_load_move_copy_records', return_value={'new': {'other'}}):
            self.assertFalse(self.api('move_confirm', token=token)['ok'])
        self.client.copy_files.assert_not_called()

    def test_manual_invalid_data_tg_confirmation_is_web_shared(self):
        scan = {'root': self.plugin._strm_root, 'scan_token': 'mock-scan'}
        self.plugin._cleaner.delete = MagicMock(return_value={'ok': True, 'deleted': [], 'failed': []})
        token = self.plugin._data_request(scan, ['mock-path'])['token']
        self.plugin._handle_tool_confirmation(token, True, self.data)
        self.plugin._cleaner.delete.assert_called_once_with('mock-scan', ['mock-path'])
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'completed')

    def test_cancel_web_updates_tg_and_tg_cannot_execute(self):
        token = self.api('trash_request')['token']
        self.assertTrue(self.api('confirmation_cancel', token=token)['ok'])
        self.assertEqual(self.edits()[-1].buttons[0][0]['text'], '已取消')
        self.plugin._handle_tool_confirmation(token, True, self.data)
        self.client.clear_recyclebin.assert_not_called()

    def test_tg_group_wrong_user_or_bot_cannot_confirm(self):
        token = self.api('trash_request')['token']
        for data in ({**self.data, 'original_chat_id': '-100'}, {**self.data, 'userid': '456'},
                     {**self.data, 'source': 'other'}):
            self.plugin._handle_tool_confirmation(token, True, data)
        self.client.clear_recyclebin.assert_not_called()
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'pending')

    def test_processing_cannot_be_cancelled_or_double_submitted(self):
        token = self.api('trash_request')['token']
        self.plugin._claim_tool_confirmation(token, 'trash')
        self.plugin._handle_tool_confirmation(token, False, self.data)
        self.assertFalse(self.api('confirmation_cancel', token=token)['ok'])
        with self.assertRaises(HTTPException):
            self.api('trash_clear', token=token)
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'processing')

    def test_wrong_kind_does_not_consume_another_confirmation(self):
        token = self.api('trash_request')['token']
        with self.assertRaises(HTTPException):
            self.plugin._consume(token, 'move')
        self.assertIn(token, self.plugin._pending)

    def test_expired_confirmation_never_submits(self):
        token = self.api('trash_request')['token']
        created, kind, snapshot = self.plugin._pending[token]
        self.plugin._pending[token] = (time.monotonic() - 1801, kind, snapshot)
        with self.assertRaises(HTTPException):
            self.api('trash_clear', token=token)
        self.plugin._handle_tool_confirmation(token, True, self.data)
        self.client.clear_recyclebin.assert_not_called()
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'expired')
        self.assertEqual(self.edits()[-1].buttons[0][0]['text'], '已过期')

    def test_empty_or_failed_previews_create_no_confirmation(self):
        self.client.rb_list.return_value = {'count': 0, 'items': []}
        self.assertTrue(self.api('trash_request')['empty'])
        self.sender.send_msg.assert_not_called()
        self.client.list_children.side_effect = RuntimeError('read failed')
        self.assertFalse(self.api('move_request')['ok'])
        self.sender.send_msg.assert_not_called()

    def test_submission_failure_updates_original_message_without_replay(self):
        token = self.api('trash_request')['token']
        self.client.clear_recyclebin.side_effect = RuntimeError('uncertain result')
        self.assertFalse(self.api('trash_clear', token=token)['ok'])
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'failed')
        self.assertEqual(self.edits()[-1].buttons[0][0]['text'], '⚠️清空未完成')
        self.client.clear_recyclebin.assert_called_once()

    def test_scheduled_monitor_still_executes_without_manual_confirmation(self):
        self.plugin._schedule['p115_move']['enabled'] = True
        with patch.object(self.plugin, '_source_cookie', return_value='mock'), \
                patch.object(self.plugin, '_move_run', return_value={'moved': 0, 'errors': []}) as move:
            self.plugin._run_scheduled_serial('p115_move')
        move.assert_called_once_with()

    def test_move_web_preview_tg_confirm_updates_state_and_blocks_web_repeat(self):
        token = self.api('move_request')['token']
        self.plugin._handle_tool_confirmation(token, True, self.data)
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'completed')
        with self.assertRaises(HTTPException):
            self.api('move_confirm', token=token)
        self.client.copy_files.assert_called_once()

    def test_cancel_tg_removes_web_pending_action(self):
        token = self.api('trash_request')['token']
        self.plugin._handle_tool_confirmation(token, False, self.data)
        self.assertEqual(self.api('pending')['confirmations'][0]['state'], 'cancelled')
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'cancelled')
        self.client.clear_recyclebin.assert_not_called()

    def test_numeric_callback_chat_id_does_not_duplicate_original_message_edit(self):
        token = self.api('trash_request')['token']
        self.plugin._handle_tool_confirmation(token, True, {
            **self.data, 'original_chat_id': 123, 'original_message_id': 789})
        self.assertEqual([message.buttons[0][0]['text'] for message in self.edits()], ['⌛️清空中', '✅清空完成'])

    def test_queue_updates_tg_immediately_and_rechecks_authority_on_execution(self):
        token = self.command_tool('emetools_cleartrash')
        callbacks = []
        def wait_in_queue(key, label, callback, manual=False):
            callbacks.append(callback)
            return {'ok': True, 'queued': True}
        with patch.object(self.plugin._p115_tasks, 'run', side_effect=wait_in_queue):
            self.plugin._handle_tool_confirmation(token, True, self.data)
        self.assertEqual(self.edits()[-1].buttons[0][0]['text'], '⌛️清空中')
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'processing')
        with patch.object(self.plugin, '_telegram_command_sources', return_value={}):
            with self.assertRaises(ValueError):
                callbacks[0]()
        self.client.clear_recyclebin.assert_not_called()
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'failed')

    def test_scheduled_data_web_confirmation_updates_original_tg_message(self):
        snapshot = {'root': self.plugin._strm_root, 'items': [{'path': 'mock-path'}]}
        token = self.plugin._remember('scheduled_tools', snapshot)
        self.plugin._send_scheduled_telegram_confirmation(token, snapshot)
        self.plugin._cleaner.quarantine = MagicMock(return_value={'ok': True, 'deleted': [], 'failed': []})
        self.api('confirm_scheduled', token=token)
        self.plugin._cleaner.quarantine.assert_called_once_with(snapshot, ['mock-path'])
        self.assertEqual(self.api('pending')['pending'], [])
        self.assertEqual([message.buttons[0][0]['text'] for message in self.edits()], ['⌛️清理中', '✅清理完成'])

    def test_command_confirmation_ignores_routine_notice_switch(self):
        self.plugin._notifications['enabled'] = False
        self.conf.switchs = []
        token = self.command_tool('emetools_cleartrash')
        self.sender.send_msg.assert_called_once()
        self.plugin._handle_tool_confirmation(token, True, self.data)
        self.client.clear_recyclebin.assert_called_once()

    def test_manual_move_run_compatibility_only_previews(self):
        self.assertTrue(self.api('move_run')['token'])
        self.client.copy_files.assert_not_called()
        self.client.move_files.assert_not_called()

    def test_new_preview_token_does_not_override_old_terminal_state(self):
        token = self.api('trash_request')['token']
        self.api('confirmation_cancel', token=token)
        self.plugin._set_cleanup_status(token, 'processing')
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'cancelled')
