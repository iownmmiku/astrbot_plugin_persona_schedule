"""Proactive schedules and the optional Persona Canvas integration contract."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from test_schedule import DEFAULTS, FakeConfig, FakeContext, PersonaSchedule, UMO, validate_settings


class AutomationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ctx = FakeContext()
        with patch('astrbot_plugin_persona_schedule.main.StarTools.get_data_dir', return_value=self.temp.name):
            self.plugin = PersonaSchedule(self.ctx, FakeConfig({"all_chats_enabled": True, "adaptive_enabled": False, "transition_notice_enabled": False}))
        self.persona = await self.plugin._persona('traveler')
        self.day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        self.plugin.store.settings.update(enabled=True, diary_enabled=False)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def drain(self):
        await asyncio.wait_for(asyncio.gather(*list(self.plugin.tasks)), timeout=4)

    async def prepare(self):
        await self.plugin.ensure(self.persona, UMO, self.day)
        self.ctx.calls.clear()
        return self.plugin.store.data['personas']['traveler']['days'][self.day]

    async def context(self):
        return await self.plugin.get_proactive_context(UMO)

    async def test_auto_generates_selected_unanalysed_persona_without_chat(self):
        self.plugin.store.settings['auto_schedule_personas'] = ['traveler']
        await self.plugin.tick()
        await self.drain()
        self.assertIn(self.day, self.plugin.store.data['personas']['traveler']['days'])
        self.assertEqual(len(self.ctx.calls), 3)
        self.assertFalse(self.ctx.sent)
        await self.plugin.tick()
        await self.drain()
        self.assertEqual(len(self.ctx.calls), 3)

    async def test_default_auto_scope_uses_prepared_personas(self):
        self.assertEqual(self.plugin.store.settings['auto_schedule_personas'], [])
        await self.plugin.tick()
        await self.drain()
        self.assertFalse(self.ctx.calls)
        yesterday = (datetime.now(ZoneInfo('Asia/Shanghai')).date()-timedelta(days=1)).isoformat()
        await self.plugin.ensure(self.persona, UMO, yesterday)
        self.ctx.calls.clear()
        await self.plugin.tick()
        await self.drain()
        self.assertEqual(len(self.ctx.calls), 1)
        self.assertIn(self.day, self.plugin.store.data['personas']['traveler']['days'])

    async def test_auto_off_does_not_generate_even_for_known_sessions(self):
        self.plugin.store.settings.update(auto_schedule_enabled=False, auto_schedule_personas=['traveler'])
        env = await self.plugin.environment(UMO, 'test')
        self.plugin._remember_session(env)
        await self.plugin.tick()
        await self.drain()
        self.assertFalse(self.ctx.calls)

    async def test_global_off_makes_no_background_model_calls(self):
        self.plugin.store.settings.update(enabled=False, auto_schedule_personas=['traveler'])
        await self.plugin.tick()
        await self.drain()
        self.assertFalse(self.ctx.calls)

    async def test_cached_manual_day_is_never_overwritten(self):
        plan = await self.prepare()
        plan.update(summary='手动安排', origin='manual')
        await self.plugin.tick()
        await self.drain()
        self.assertFalse(self.ctx.calls)
        self.assertEqual(plan['summary'], '手动安排')

    async def test_auto_time_respects_local_clock(self):
        await self.prepare()
        self.plugin.store.data['personas']['traveler']['days'].clear()
        self.plugin.store.settings['auto_schedule_time'] = '12:00'
        before = datetime(2026,10,13,11,59,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.plugin._schedule_plans(before)
        await self.drain()
        self.assertFalse(self.ctx.calls)
        after = datetime(2026,10,13,12,0,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.plugin._schedule_plans(after)
        await self.drain()
        self.assertEqual(len(self.ctx.calls), 1)

    async def test_parallel_ticks_do_not_duplicate_daily_generation(self):
        self.plugin.store.settings['auto_schedule_personas'] = ['traveler']
        await asyncio.gather(self.plugin.tick(), self.plugin.tick(), self.plugin.tick())
        await self.drain()
        self.assertEqual(len(self.ctx.calls), 3)

    async def test_auto_failure_has_backoff_and_no_secret_in_logs(self):
        self.plugin.store.settings['auto_schedule_personas'] = ['traveler']
        self.plugin.generator.profile = AsyncMock(side_effect=RuntimeError('fixture-secret'))
        await self.plugin.tick()
        await self.drain()
        await self.plugin.tick()
        await self.drain()
        self.assertEqual(self.plugin.generator.profile.await_count, 1)
        self.assertNotIn('fixture-secret', json.dumps(self.plugin.store.data['logs']))

    async def test_disabling_auto_during_generation_discards_uncommitted_plan(self):
        await self.prepare()
        record = self.plugin.store.data['personas']['traveler']
        record['days'].clear()
        original = self.plugin.generator.day
        async def disabled(*args):
            value = await original(*args)
            self.plugin.store.settings['auto_schedule_enabled'] = False
            return value
        self.plugin.generator.day = disabled
        await self.plugin.tick()
        await self.drain()
        self.assertFalse(record['days'])

    async def test_canvas_off_leaves_original_behavior_and_does_not_generate(self):
        result = await self.context()
        self.assertFalse(result['applicable'])
        self.assertTrue(result['allowed'])
        self.assertFalse(self.ctx.calls)

    async def test_canvas_busy_cancels_without_queueing_or_sending(self):
        plan = await self.prepare()
        plan['slots'][0]['status'] = 'busy'
        self.plugin.store.settings['canvas_integration_enabled'] = True
        result = await self.context()
        self.assertTrue(result['applicable'])
        self.assertFalse(result['allowed'])
        self.assertEqual(result['status'], 'busy')
        self.assertFalse(self.plugin.store.data['queues'])
        self.assertFalse(self.ctx.calls)
        self.assertFalse(self.ctx.sent)

    async def test_canvas_free_and_slow_have_stable_current_activity_prompt(self):
        plan = await self.prepare()
        self.plugin.store.settings['canvas_integration_enabled'] = True
        for status in ('free','slow'):
            plan['slots'][0].update(status=status, title='花园散步')
            result = await self.context()
            repeated = await self.context()
            self.assertTrue(result['allowed'])
            self.assertEqual(result['status'], status)
            self.assertIn('花园散步', result['prompt'])
            self.assertEqual(result['context_key'], repeated['context_key'])
            self.assertNotIn('queued_messages', result['prompt'])
            self.assertNotIn('history', result['prompt'])

    async def test_context_key_changes_with_activity_or_conversation(self):
        plan = await self.prepare()
        self.plugin.store.settings['canvas_integration_enabled'] = True
        first = await self.context()
        plan['slots'][0]['title'] = '新的活动'
        second = await self.context()
        self.ctx.conversation_manager.cid = 'conv-2'
        third = await self.context()
        self.assertNotEqual(first['context_key'], second['context_key'])
        self.assertNotEqual(second['context_key'], third['context_key'])

    async def test_canvas_respects_global_switch_and_session_whitelist(self):
        self.plugin.store.settings.update(canvas_integration_enabled=True, whitelist_enabled=True, whitelist=[])
        result = await self.context()
        self.assertFalse(result['applicable'])
        self.assertFalse(self.ctx.calls)
        self.plugin.store.settings.update(enabled=False, whitelist=[UMO])
        self.assertFalse((await self.context())['applicable'])

    async def test_context_resolves_platform_rules_without_prior_schedule_session(self):
        await self.prepare()
        self.plugin.store.settings['canvas_integration_enabled'] = True
        self.ctx.get_platform_inst = lambda pid: types.SimpleNamespace(meta=lambda: types.SimpleNamespace(name='test-platform'))
        self.ctx.persona_manager.resolve_selected_persona = AsyncMock(return_value=(
            'traveler', self.ctx.persona_manager.item, None, False))
        result = await self.context()
        self.assertTrue(result['allowed'])
        self.assertEqual(self.ctx.persona_manager.resolve_selected_persona.call_args.kwargs['platform_name'], 'test-platform')
        self.assertFalse(self.ctx.calls)

    async def test_missing_cached_plan_skips_without_extra_model_call_or_queue(self):
        self.plugin.store.settings['canvas_integration_enabled'] = True
        self.plugin.generator.profile = AsyncMock(side_effect=RuntimeError('fixture-secret'))
        result = await self.context()
        self.assertTrue(result['applicable'])
        self.assertFalse(result['allowed'])
        self.assertNotIn('fixture-secret', json.dumps(result))
        self.assertFalse(self.plugin.store.data['queues'])
        self.assertFalse(self.ctx.calls)
        self.plugin.generator.profile.assert_not_awaited()

    async def test_persona_switch_during_context_lookup_cancels_old_context(self):
        await self.prepare()
        self.plugin.store.settings['canvas_integration_enabled'] = True
        original = self.plugin.environment
        calls = 0
        async def switched(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.ctx.persona_manager.item['system_prompt'] = '已经切换的角色'
            return await original(*args)
        self.plugin.environment = switched
        result = await self.context()
        self.assertFalse(result['allowed'])
        self.assertIn('已切换', result['reason'])

    async def test_plan_replaced_during_context_lookup_uses_new_busy_state(self):
        plan = await self.prepare()
        self.plugin.store.settings['canvas_integration_enabled'] = True
        original = self.plugin.environment
        calls = 0
        async def edited(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                replacement = copy.deepcopy(plan)
                replacement['slots'][0]['status'] = 'busy'
                self.plugin.store.data['personas']['traveler']['days'][self.day] = replacement
            return await original(*args)
        self.plugin.environment = edited
        result = await self.context()
        self.assertFalse(result['allowed'])
        self.assertEqual(result['status'], 'busy')


class AutomationValidationTests(unittest.TestCase):
    def test_time_persona_and_switch_validation(self):
        self.assertFalse(DEFAULTS['canvas_integration_enabled'])
        self.assertTrue(DEFAULTS['auto_schedule_enabled'])
        for bad in ({'auto_schedule_time':'24:00'}, {'auto_schedule_time':'6:00'}, {'auto_schedule_time':1},
                    {'auto_schedule_personas':['']}, {'auto_schedule_personas':'traveler'},
                    {'auto_schedule_enabled':'true'}, {'canvas_integration_enabled':1}):
            with self.assertRaises(ValueError):
                validate_settings(bad, DEFAULTS)
        result = validate_settings({'auto_schedule_personas':[' traveler ', 'traveler'], 'auto_schedule_time':'06:30'}, DEFAULTS)
        self.assertEqual(result['auto_schedule_personas'], ['traveler'])
        self.assertEqual(result['auto_schedule_time'], '06:30')
