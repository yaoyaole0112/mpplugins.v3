import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from emetools import tool_notifications as notices
from tests import test_shared_confirmation


class ConfirmationResultTests(unittest.TestCase):
    setUp = test_shared_confirmation.SharedConfirmationTests.setUp
    api = test_shared_confirmation.SharedConfirmationTests.api
    edits = test_shared_confirmation.SharedConfirmationTests.edits
    command_tool = test_shared_confirmation.SharedConfirmationTests.command_tool

    def assert_merged(self, expected, token):
        messages = [call.args[0] for call in self.plugin.chain.post_message.call_args_list]
        self.assertTrue(messages)
        self.assertTrue(all(message.original_message_id for message in messages))
        self.assertEqual(self.edits()[-1].text, expected[1])
        self.assertNotIn(expected[0], self.edits()[-1].text)
        self.assertFalse(any('详细结果见下方' in message.text or '另行通知' in message.text for message in messages))
        self.assertEqual(len(self.edits()[-1].buttons[0]), 1)
        self.assertTrue(self.edits()[-1].buttons[0][0]['callback_data'].endswith(':p'))
        self.assertNotIn('notice_sent', self.api('confirmation_status', token=token))

    def test_web_cleanup_result_replaces_confirmation_body(self):
        token = self.api('cleanup_request')['token']
        result = self.api('cleanup_confirm', token=token)
        self.assert_merged(notices.file_cleanup(result, {'folders': []}), token)
        self.assertIn('1 个文件', self.edits()[-1].text)
        self.assertIn('1.00 KB', self.edits()[-1].text)

    def test_trash_result_replaces_confirmation_body(self):
        token = self.api('trash_request')['token']
        result = self.api('trash_clear', token=token)
        self.assert_merged(notices.empty_trash(result), token)
        self.assertIn('不可恢复', self.edits()[-1].text)

    def test_move_result_replaces_confirmation_body(self):
        token = self.api('move_request')['token']
        result = self.api('move_confirm', token=token)
        self.assert_merged(notices.file_move(result), token)
        self.client.copy_files.assert_called_once()

    def test_manual_invalid_data_result_replaces_confirmation_body(self):
        scan = {'root': self.plugin._strm_root, 'scan_token': 'scan'}
        token = self.plugin._data_request(scan, ['mock-path'])['token']
        result = {'ok': True, 'deleted': [{'path': 'mock-path'}], 'failed': []}
        self.plugin._cleaner.delete = MagicMock(return_value=result)
        self.plugin._handle_tool_confirmation(token, True, {**self.data, 'original_message_id': 789})
        self.assert_merged(notices.invalid_cleanup(result, scan['root']), token)

    def test_scheduled_data_result_replaces_confirmation_body(self):
        snapshot = {'root': self.plugin._strm_root, 'items': [{'path': 'mock-path'}]}
        token = self.plugin._remember('scheduled_tools', snapshot)
        self.plugin._send_scheduled_telegram_confirmation(token, snapshot)
        result = {'ok': True, 'deleted': [{'path': 'mock-path'}], 'failed': []}
        self.plugin._cleaner.quarantine = MagicMock(return_value=result)
        self.api('confirm_scheduled', token=token)
        self.assert_merged(notices.invalid_cleanup(result, snapshot['root']), token)

    def test_tg_error_is_in_original_message_without_second_reply(self):
        token = self.command_tool('emetools_cleartrash')
        self.client.clear_recyclebin.side_effect = RuntimeError('request failed')
        self.plugin._handle_tool_confirmation(token, True, {**self.data, 'original_message_id': 789})
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'failed')
        self.assertTrue(all(call.args[0].original_message_id for call in self.plugin.chain.post_message.call_args_list))
        self.assertIn('未自动重发', self.edits()[-1].text)
        self.client.clear_recyclebin.assert_called_once()

    def test_partial_file_cleanup_keeps_counts_and_failure_details(self):
        token = self.api('cleanup_request')['token']
        self.client.delete_files.return_value = {'state': False, 'msg': '删除失败'}
        result = self.api('cleanup_confirm', token=token)
        self.assert_merged(notices.file_cleanup(result, {'folders': []}), token)
        self.assertIn('删除失败', self.edits()[-1].text)
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'failed')

    def test_same_terminal_state_can_receive_detailed_body_without_new_message(self):
        token = self.api('trash_request')['token']
        self.plugin._set_cleanup_status(token, 'completed', '旧的简要状态')
        expected = ('清空回收站', '已清空 2 项；完整结果')
        self.plugin._finish_confirmation(token, 'completed', expected)
        self.assert_merged(expected, token)
        count = self.plugin.chain.post_message.call_count
        self.plugin._finish_confirmation(token, 'completed', expected)
        self.assertEqual(self.plugin.chain.post_message.call_count, count)

    def test_untracked_web_confirmation_keeps_one_standalone_result(self):
        self.notification_helper.return_value.get_service.return_value = None
        token = self.api('trash_request')['token']
        self.plugin.chain.post_message.reset_mock()
        result = self.api('trash_clear', token=token)
        messages = [call.args[0] for call in self.plugin.chain.post_message.call_args_list]
        self.assertEqual(len(messages), 1)
        self.assertFalse(messages[0].original_message_id)
        self.assertEqual(messages[0].text, notices.empty_trash(result)[1])

    def test_failed_edit_falls_back_only_once_without_repeating_operation(self):
        token = self.api('trash_request')['token']
        with patch.object(self.plugin, '_edit_tool_confirmation_message', return_value=False):
            result = self.api('trash_clear', token=token)
        messages = [call.args[0] for call in self.plugin.chain.post_message.call_args_list]
        self.assertEqual(len(messages), 1)
        self.assertFalse(messages[0].original_message_id)
        self.assertEqual(messages[0].text, notices.empty_trash(result)[1])
        self.client.clear_recyclebin.assert_called_once()
        self.plugin._finish_confirmation(token, 'completed', notices.empty_trash(result))
        self.assertEqual(self.plugin.chain.post_message.call_count, 1)

    def test_one_bot_edit_failure_does_not_duplicate_result_to_other_bots(self):
        token = self.api('trash_request')['token']
        self.plugin._cleanup_message_refs[token].append({
            'source': '第二Bot', 'userid': '456', 'original_chat_id': '456', 'original_message_id': 790})
        edit = self.plugin._edit_tool_confirmation_message
        with patch.object(self.plugin, '_edit_tool_confirmation_message', side_effect=lambda data, *args:
                          False if data['source'] == '第二Bot' else edit(data, *args)):
            self.api('trash_clear', token=token)
        fallback = [call.args[0] for call in self.plugin.chain.post_message.call_args_list
                    if not call.args[0].original_message_id]
        self.assertEqual([(message.source, message.userid) for message in fallback], [('第二Bot', '456')])
        self.assertEqual(len(self.edits()), 2)

    def test_notification_failure_does_not_turn_success_into_failed_operation(self):
        token = self.api('trash_request')['token']
        with patch.object(self.plugin, '_edit_tool_confirmation_message', return_value=False), \
                patch.object(self.plugin, '_media_bot_reply', side_effect=RuntimeError('offline')):
            self.assertTrue(self.api('trash_clear', token=token)['ok'])
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'completed')
        self.client.clear_recyclebin.assert_called_once()

    def media_confirmation(self):
        self.plugin._media.scan = MagicMock(return_value={'results': [{'versions': [
            {'is_best': False, 'file_path': '/strm/low.strm', 'size': 1024}]}]})
        self.plugin._media.delete = MagicMock(return_value={'deleted': [
            {'file_path': '/strm/low.strm', 'size': 1024}], 'failures': []})
        self.plugin._media.refresh_emby = MagicMock()
        self.plugin._request_media_bot_cleanup({'source': '管理员Bot', 'user': '123', 'channel': self.data['channel']})
        token = next(iter(self.plugin._pending))
        self.plugin.chain.post_message.reset_mock()
        return token

    def test_media_cleanup_updates_tracked_confirmation_and_does_not_repeat_title(self):
        token = self.media_confirmation()
        data = {**self.data, 'original_message_id': 789}
        self.plugin._handle_media_bot_confirmation(token, True, data)
        self.assert_merged(notices.media_cleanup(self.plugin._media.delete.return_value), token)
        count = self.plugin.chain.post_message.call_count
        self.plugin._handle_media_bot_confirmation(token, True, data)
        self.assertEqual(self.plugin.chain.post_message.call_count, count)
        self.plugin._media.delete.assert_called_once_with(['/strm/low.strm'])

    def test_media_failure_updates_original_without_new_reply(self):
        token = self.media_confirmation()
        self.plugin._media.delete.side_effect = ValueError('扫描结果已变化')
        self.plugin._handle_media_bot_confirmation(token, True, {**self.data, 'original_message_id': 789})
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'failed')
        self.assertTrue(all(call.args[0].original_message_id for call in self.plugin.chain.post_message.call_args_list))
        self.assertIn('扫描结果已变化', self.edits()[-1].text)

    def test_media_cancel_updates_original_without_new_reply(self):
        token = self.media_confirmation()
        self.plugin._handle_media_bot_confirmation(token, False, {**self.data, 'original_message_id': 789})
        self.assertEqual(self.api('confirmation_status', token=token)['state'], 'cancelled')
        self.plugin._media.delete.assert_not_called()
        self.assertEqual(self.plugin.chain.post_message.call_count, 1)
        self.assertTrue(self.edits()[-1].original_message_id)

    def test_media_status_button_is_inert_and_not_exported_as_web_action(self):
        token = self.media_confirmation()
        self.plugin.scheduled_confirmation_action(SimpleNamespace(event_data={
            **self.data, 'plugin_id': 'EmeTools', 'text': f'media:{token}:p'}))
        self.plugin._media.delete.assert_not_called()
        self.assertEqual(self.api('pending')['confirmations'], [])
