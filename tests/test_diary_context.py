"""Whitelist journal materials use API doubles, never historical-chat scraping."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from test_schedule import FakeConfig, FakeContext, FakeEvent, NAME, PersonaSchedule, Store, UMO
from astrbot_plugin_persona_schedule.diary_context import add_material, bind_initial_material, prune_materials, select_materials


class DiaryContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ctx = FakeContext()
        config = FakeConfig({"enabled": True, "whitelist": [UMO], "whitelist_enabled": True,
                             "auto_schedule_enabled": False, "adaptive_enabled": False})
        with patch('astrbot_plugin_persona_schedule.main.StarTools.get_data_dir', return_value=self.temp.name):
            self.plugin = PersonaSchedule(self.ctx, config)
        self.persona = await self.plugin._persona('traveler')
        self.day = (datetime.now(ZoneInfo('Asia/Shanghai')).date() - timedelta(days=1)).isoformat()
        await self.plugin.ensure(self.persona, UMO, self.day)
        self.at = datetime.fromisoformat(self.day+'T20:00:00').replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.env = await self.plugin.environment(UMO, 'test')
        self.ctx.calls.clear()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def capture(self, value='聊了今天的小事', response=None, at=None):
        event = FakeEvent(value)
        with patch('astrbot_plugin_persona_schedule.main.time.time', return_value=self.at if at is None else at):
            await self.plugin.capture_diary_input(event)
            if response:
                await self.plugin.capture_diary_response(event, types.SimpleNamespace(completion_text=response))
        return event

    def material(self, **kwargs):
        return select_materials(self.plugin.store, kwargs.get('persona', self.persona), kwargs.get('day', self.day), kwargs.get('timezone', 'Asia/Shanghai'))

    async def test_received_and_generated_text_enter_diary_but_not_raw_archive(self):
        await self.capture('想和你一起看星星，私密原文', '等回家后看看夜空吧')
        entry = await self.plugin.ensure_diary(self.persona, self.day, UMO)
        payload = json.loads(self.ctx.calls[-1]['prompt'])
        material = payload['saved_day']['conversation_material']
        self.assertEqual([r['role'] for r in material['messages']], ['user', 'assistant'])
        self.assertIn('私密原文', material['messages'][0]['text'])
        self.assertEqual(entry['source']['chat_context']['message_count'], 2)
        self.assertNotIn('messages', entry['source']['chat_context'])
        self.assertNotIn('私密原文', json.dumps(entry['source'], ensure_ascii=False))
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(self.ctx.conversation_manager.pairs, [])

    async def test_empty_whitelist_never_collects_even_with_global_control(self):
        self.plugin.store.settings.update(whitelist=[], whitelist_enabled=False, all_chats_enabled=True)
        await self.capture(response='不会记录')
        self.assertEqual(self.material()['message_count'], 0)

    async def test_explicit_whitelist_materials_are_independent_of_reply_control_mode(self):
        self.plugin.store.settings.update(whitelist_enabled=False, all_chats_enabled=False)
        await self.capture()
        self.assertEqual(self.material()['message_count'], 1)
        self.assertFalse(self.plugin.store.data['queues'])

    async def test_disabled_capture_switch_plugin_or_journal_blocks_collection(self):
        for key in ('enabled', 'diary_enabled', 'diary_chat_enabled'):
            with self.subTest(key=key):
                self.plugin.store.settings[key] = False
                await self.capture()
                self.assertEqual(self.material()['message_count'], 0)
                self.plugin.store.settings[key] = True

    async def test_commands_and_unaddressed_group_text_are_excluded(self):
        await self.capture('/reset')
        event = FakeEvent('旁人的消息')
        event.is_private_chat = lambda: False
        event.is_at_or_wake_command = False
        await self.plugin.capture_diary_input(event)
        self.assertEqual(self.material()['message_count'], 0)

    async def test_same_event_and_same_platform_message_id_are_deduplicated(self):
        event = FakeEvent('同一条消息')
        event.message_obj = types.SimpleNamespace(message_id='platform-1')
        with patch('astrbot_plugin_persona_schedule.main.time.time', return_value=self.at):
            await self.plugin.capture_diary_input(event)
            await self.plugin.capture_diary_input(event)
            twin = FakeEvent('同一条消息')
            twin.message_obj = event.message_obj
            await self.plugin.capture_diary_input(twin)
            response = types.SimpleNamespace(completion_text='同一条回复')
            await self.plugin.capture_diary_response(event, response)
            await self.plugin.capture_diary_response(event, response)
        self.assertEqual(self.material()['message_count'], 2)

    async def test_response_after_conversation_reset_does_not_cross_identity(self):
        event = await self.capture()
        self.ctx.conversation_manager.cid = 'new-conversation'
        await self.plugin.capture_diary_response(event, types.SimpleNamespace(completion_text='新会话回复'))
        self.assertEqual(self.material()['message_count'], 1)

    async def test_tool_call_and_non_text_responses_are_excluded(self):
        event = await self.capture()
        await self.plugin.capture_diary_response(event, types.SimpleNamespace(completion_text='内部工具规划', tools_call_name=['search']))
        await self.plugin.capture_diary_response(event, types.SimpleNamespace(completion_text=None))
        await self.plugin.capture_diary_response(event, types.SimpleNamespace(completion_text='提供商错误', role='err'))
        self.assertEqual(self.material()['message_count'], 1)

    async def test_slow_environment_lookup_does_not_move_incoming_text_to_next_day(self):
        clock = [datetime.fromisoformat(self.day+'T23:59:59').replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()]
        original = self.plugin.environment
        async def late(*args, **kwargs):
            result = await original(*args, **kwargs)
            clock[0] += 5
            return result
        self.plugin.environment = late
        with patch('astrbot_plugin_persona_schedule.main.time.time', side_effect=lambda:clock[0]):
            await self.plugin.capture_diary_input(FakeEvent('午夜前收到'))
        self.assertEqual(self.material()['message_count'], 1)

    async def test_material_storage_failure_does_not_interrupt_already_sent_batch(self):
        await self.plugin.ensure(self.persona, UMO)
        now = datetime.now(ZoneInfo('Asia/Shanghai')).timestamp()
        self.plugin.store.enqueue(UMO, self.env['cid'], self.persona['key'], '用户', '待补回', now-5, now-1, 'slow')
        session = {'platform':'test', 'persona_id':'traveler', 'cid':self.env['cid']}
        with patch('astrbot_plugin_persona_schedule.main.add_material', side_effect=OSError('disk fixture')):
            await self.plugin._session_tick(UMO, session)
        self.assertEqual(len(self.ctx.sent), 1)
        self.assertNotIn(UMO, self.plugin.store.data['queues'])

    async def test_missing_profile_still_assigns_material_by_saved_schedule_timezone(self):
        record = self.plugin.store.data['personas']['traveler']
        saved_profile = record['profile']
        record['profile'] = None
        midnight = datetime.fromisoformat(self.day+'T23:30:00').replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        await self.capture(at=midnight)
        record['profile'] = saved_profile
        tokyo_day = (datetime.fromisoformat(self.day).date() + timedelta(days=1)).isoformat()
        self.assertEqual(self.material(day=tokyo_day, timezone='Asia/Tokyo')['message_count'], 1)
        self.assertEqual(self.material(day=self.day, timezone='Asia/Tokyo')['message_count'], 0)

    async def test_other_persona_and_other_day_do_not_enter_prompt(self):
        await self.capture()
        other = {**self.env, 'persona': {'id':'other', 'key':'other'}}
        add_material(self.plugin.store, other, 'user', '别人的秘密', self.at, 'other')
        add_material(self.plugin.store, self.env, 'user', '别天的话', self.at-86400, 'other-day')
        data = self.material()
        self.assertEqual(data['message_count'], 1)
        self.assertNotIn('秘密', json.dumps(data, ensure_ascii=False))

    async def test_multiple_conversations_remain_separate_anonymous_labels(self):
        await self.capture()
        other = {**self.env, 'cid':'conv-2'}
        add_material(self.plugin.store, other, 'user', '第二个会话', self.at+1, 'other')
        data = self.material()
        self.assertEqual(data['conversation_count'], 2)
        self.assertEqual(len({r['conversation'] for r in data['messages']}), 2)
        serialized = json.dumps(data, ensure_ascii=False)
        self.assertNotIn(UMO, serialized)
        self.assertNotIn('conv-1', serialized)

    async def test_setting_revocation_cleans_pending_text_and_keeps_existing_diary(self):
        await self.capture()
        archived = copy.deepcopy(await self.plugin.ensure_diary(self.persona, self.day, UMO))
        await self.plugin.web_settings({'whitelist': []})
        self.assertFalse(self.plugin.store.data['diary_chat']['messages'])
        self.assertEqual(self.plugin.store.data['diaries']['traveler'][self.day], archived)

    async def test_toggle_off_cleans_pending_text(self):
        await self.capture()
        await self.plugin.web_settings({'diary_chat_enabled': False})
        self.assertFalse(self.plugin.store.data['diary_chat']['messages'])

    async def test_whitelist_change_during_model_generation_discards_old_result(self):
        await self.capture()
        async def revoke(*args, **kwargs):
            self.plugin.store.settings['whitelist'] = []
            return {'title':'旧结果', 'content':'不应归档', 'mood':''}
        self.plugin.generator.diary = revoke
        with self.assertRaisesRegex(ValueError, '已变化'):
            await self.plugin.ensure_diary(self.persona, self.day, UMO)
        self.assertNotIn(self.day, self.plugin.store.data['diaries']['traveler'])

    async def test_retention_and_restart(self):
        await self.capture()
        reloaded = Store(self.temp.name)
        self.assertEqual(select_materials(reloaded, self.persona, self.day, 'Asia/Shanghai')['message_count'], 1)
        self.assertTrue(prune_materials(reloaded, self.at+9*86400))
        self.assertFalse(reloaded.data['diary_chat']['messages'])

    async def test_bounded_materials_and_prompt_report_omissions(self):
        with patch('astrbot_plugin_persona_schedule.diary_context.MAX_PER_SESSION_DAY', 4), patch('astrbot_plugin_persona_schedule.diary_context.PROMPT_MESSAGES', 2):
            for index in range(7):
                add_material(self.plugin.store, self.env, 'user', 'x'*1100, self.at+index, str(index))
            self.assertEqual(len(self.plugin.store.data['diary_chat']['messages']), 4)
            material = self.material()
            self.assertEqual(material['message_count'], 2)
            self.assertEqual(material['omitted_count'], 5)
            self.assertTrue(material['truncated'])
            self.assertTrue(all(len(r['text']) == 1000 for r in material['messages']))

    async def test_read_api_reports_material_counts_without_exposing_raw_text(self):
        await self.capture('不应出现在浏览器的原文')
        result = await self.plugin.web_diary({'persona_id':'traveler', 'date':self.day})
        self.assertEqual(result['chat_material']['message_count'], 1)
        self.assertEqual(result['chat_whitelist_count'], 1)
        self.assertNotIn('原文', json.dumps(result, ensure_ascii=False))

    async def test_diary_does_not_scrape_existing_conversation_history(self):
        self.ctx.conversation_manager.get_conversation = AsyncMock(side_effect=AssertionError('No historical scrape'))
        entry = await self.plugin.ensure_diary(self.persona, self.day, UMO)
        self.assertEqual(entry['source']['chat_context']['message_count'], 0)
        self.assertEqual(self.ctx.conversation_manager.get_conversation.await_count, 0)

    async def test_revocation_while_waiting_for_generation_slot_never_sends_old_text(self):
        await self.capture('撤销前的私密素材')
        self.plugin.generation_slots = asyncio.Semaphore(0)
        task = asyncio.create_task(self.plugin.ensure_diary(self.persona, self.day, UMO))
        await asyncio.sleep(0)
        self.assertTrue(self.plugin.generation_slots._waiters)
        await self.plugin.web_settings({'whitelist': []})
        self.plugin.generation_slots.release()
        with self.assertRaisesRegex(ValueError, '已变化'):
            await task
        self.assertEqual(self.ctx.calls, [])

    async def test_revocation_during_provider_lookup_prevents_model_call(self):
        await self.capture('撤销前的私密素材')
        async def provider(*args, **kwargs):
            await self.plugin.web_settings({'diary_chat_enabled': False})
            return 'fake-provider'
        self.plugin.generator.provider_id = provider
        with self.assertRaisesRegex(ValueError, '已变化'):
            await self.plugin.ensure_diary(self.persona, self.day, UMO)
        self.assertEqual(self.ctx.calls, [])

    async def test_revocation_after_invalid_response_prevents_format_retry(self):
        await self.capture('不能因格式重试再次发送')
        async def invalid(**kwargs):
            self.ctx.calls.append(kwargs)
            await self.plugin.web_settings({'whitelist': []})
            return types.SimpleNamespace(completion_text='invalid json')
        self.ctx.llm_generate = invalid
        with self.assertRaisesRegex(ValueError, '已变化'):
            await self.plugin.ensure_diary(self.persona, self.day, UMO)
        self.assertEqual(len(self.ctx.calls), 1)
        self.assertNotIn(self.day, self.plugin.store.data['diaries']['traveler'])

    async def test_sync_guard_failure_is_not_retried_or_added_to_format_prompt(self):
        checked = []
        def invalidated():
            checked.append(True)
            raise ValueError('private fixture guard detail')
        with self.assertRaisesRegex(ValueError, 'private fixture guard detail'):
            await self.plugin.generator.structured('fixture', {}, lambda x: x,
                                                   before_generate=invalidated)
        self.assertEqual(checked, [True])
        self.assertEqual(self.ctx.calls, [])

    async def test_first_conversation_input_and_reply_share_one_identity(self):
        self.plugin.store.settings.update(whitelist_enabled=False, all_chats_enabled=False)
        self.ctx.conversation_manager.cid = ''
        event = await self.capture('首次聊天')
        self.ctx.conversation_manager.cid = 'newly-created'
        conversation = await self.ctx.conversation_manager.get_conversation(UMO, 'newly-created')
        req = types.SimpleNamespace(conversation=conversation, extra_user_content_parts=[])
        await self.plugin.inject_schedule(event, req)
        with patch('astrbot_plugin_persona_schedule.main.time.time', return_value=self.at + 1):
            await self.plugin.capture_diary_response(event, types.SimpleNamespace(completion_text='首次回应'))
        material = self.material()
        self.assertEqual([r['role'] for r in material['messages']], ['user', 'assistant'])
        self.assertEqual(material['conversation_count'], 1)
        self.assertTrue(all(r['cid'] == 'newly-created' for r in self.plugin.store.data['diary_chat']['messages']))

    async def test_initial_bind_requires_request_and_current_conversation_match(self):
        self.plugin.store.settings.update(whitelist_enabled=False, all_chats_enabled=False)
        self.ctx.conversation_manager.cid = ''
        event = await self.capture()
        self.ctx.conversation_manager.cid = 'current-conversation'
        req = types.SimpleNamespace(conversation=types.SimpleNamespace(cid='different-request', persona_id='traveler'),
                                    extra_user_content_parts=[])
        await self.plugin.inject_schedule(event, req)
        with patch('astrbot_plugin_persona_schedule.main.time.time', return_value=self.at + 1):
            await self.plugin.capture_diary_response(event, types.SimpleNamespace(completion_text='不应跨会话绑定'))
        self.assertEqual(self.material()['message_count'], 1)
        self.assertEqual(self.plugin.store.data['diary_chat']['messages'][0]['cid'], '')

    async def test_binding_keeps_original_time_truncation_and_platform_dedup(self):
        initial = {**self.env, 'cid': ''}
        add_material(self.plugin.store, initial, 'user', 'x' * 1100, self.at, 'first-platform-id')
        before = copy.deepcopy(self.plugin.store.data['diary_chat']['messages'][0])
        self.assertTrue(bind_initial_material(self.plugin.store, initial, self.env, 'first-platform-id'))
        row = self.plugin.store.data['diary_chat']['messages'][0]
        self.assertEqual(row['at'], before['at'])
        self.assertEqual(row['day'], before['day'])
        self.assertEqual(row['text'], before['text'])
        self.assertTrue(row['truncated'])
        self.assertFalse(add_material(self.plugin.store, self.env, 'user', 'x' * 1100, self.at, 'first-platform-id'))
        self.assertEqual(self.material()['message_count'], 1)

    async def test_binding_never_moves_existing_conversation_or_changed_persona(self):
        initial = {**self.env, 'cid': ''}
        add_material(self.plugin.store, initial, 'user', 'first', self.at, 'first')
        saved = copy.deepcopy(self.plugin.store.data['diary_chat'])
        for original, current in [(self.env, {**self.env, 'cid': 'reset'}),
                                  (initial, {**self.env, 'persona': {**self.persona, 'key': 'changed'}}),
                                  (initial, {**self.env, 'umo': 'test:FriendMessage:other'})]:
            self.assertFalse(bind_initial_material(self.plugin.store, original, current, 'first'))
        self.assertEqual(self.plugin.store.data['diary_chat'], saved)

    async def test_failed_material_write_can_retry_without_duplicate_or_lost_marker(self):
        before = copy.deepcopy(self.plugin.store.data['diary_chat'])
        with patch.object(self.plugin.store, 'save', side_effect=OSError('disk fixture')):
            with self.assertRaises(OSError):
                add_material(self.plugin.store, self.env, 'user', '可重试文本', self.at, 'retry')
        self.assertEqual(self.plugin.store.data['diary_chat'], before)
        self.assertTrue(add_material(self.plugin.store, self.env, 'user', '可重试文本', self.at, 'retry'))
        self.assertEqual(self.material()['message_count'], 1)

    async def test_failed_binding_save_restores_original_identity(self):
        initial = {**self.env, 'cid': ''}
        add_material(self.plugin.store, initial, 'user', 'first', self.at, 'first')
        before = copy.deepcopy(self.plugin.store.data['diary_chat'])
        with patch.object(self.plugin.store, 'save', side_effect=OSError('disk fixture')):
            with self.assertRaises(OSError):
                bind_initial_material(self.plugin.store, initial, self.env, 'first')
        self.assertEqual(self.plugin.store.data['diary_chat'], before)
        self.assertTrue(bind_initial_material(self.plugin.store, initial, self.env, 'first'))

    async def test_binding_preserves_session_day_limit_and_omission_count(self):
        initial = {**self.env, 'cid': ''}
        with patch('astrbot_plugin_persona_schedule.diary_context.MAX_PER_SESSION_DAY', 2):
            add_material(self.plugin.store, initial, 'user', 'oldest', self.at, 'first')
            add_material(self.plugin.store, self.env, 'user', 'recent', self.at + 1, 'second')
            add_material(self.plugin.store, self.env, 'user', 'latest', self.at + 2, 'third')
            self.assertTrue(bind_initial_material(self.plugin.store, initial, self.env, 'first'))
        material = self.material()
        self.assertEqual([r['text'] for r in material['messages']], ['recent', 'latest'])
        self.assertEqual(material['omitted_count'], 1)

    async def test_diary_page_counts_use_saved_day_timezone_after_profile_change(self):
        record = self.plugin.store.data['personas'][self.persona['id']]
        record['profile']['timezone'] = 'Asia/Tokyo'
        late = datetime.fromisoformat(self.day + 'T23:30:00').replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        await self.capture('上海午夜前，东京已经次日', at=late)
        viewed = await self.plugin.web_diary({'persona_id': self.persona['id'], 'date': self.day})
        self.assertEqual(viewed['chat_material']['message_count'], 1)
        archived = await self.plugin.ensure_diary(self.persona, self.day, UMO)
        self.assertEqual(archived['source']['chat_context'], viewed['chat_material'])
        self.assertEqual(archived['timezone'], 'Asia/Shanghai')
        record['days'].pop(self.day)
        archived_view = await self.plugin.web_diary({'persona_id': self.persona['id'], 'date': self.day})
        self.assertEqual(archived_view['chat_material'], viewed['chat_material'])
