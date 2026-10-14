"""Daily archive regressions; models and platforms are explicit API doubles."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from test_schedule import DEFAULTS, FakeConfig, FakeContext, PersonaSchedule, Store, UMO, slots, validate_settings
from astrbot_plugin_persona_schedule.generator import validate_diary


class DiaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ctx = FakeContext()
        with patch('astrbot_plugin_persona_schedule.main.StarTools.get_data_dir', return_value=self.temp.name):
            self.plugin = PersonaSchedule(self.ctx, FakeConfig({"all_chats_enabled": True, "adaptive_enabled": False, "transition_notice_enabled": False}))
        # Daily planning has separate regressions. Keep these tests specific
        # to diary scheduling and provider invocation counts.
        self.plugin.store.settings['auto_schedule_enabled'] = False
        self.persona = await self.plugin._persona('traveler')
        self.today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
        self.day = (self.today - timedelta(days=1)).isoformat()
        await self.plugin.ensure(self.persona, UMO, self.day)
        self.ctx.calls.clear()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def write(self, **kwargs):
        return await self.plugin.ensure_diary(self.persona, self.day, UMO, **kwargs)

    async def drain(self):
        await asyncio.wait_for(asyncio.gather(*list(self.plugin.tasks)), timeout=4)

    async def test_writes_first_person_archive_without_sending_or_reading_chat(self):
        self.ctx.conversation_manager.get_conversation = AsyncMock(side_effect=AssertionError('Do not read chat'))
        entry = await self.write()
        self.assertEqual(entry['date'], self.day)
        self.assertEqual(entry['revision'], 1)
        self.assertEqual(entry['source']['schedule']['date'], self.day)
        self.assertNotIn('weekly', entry['source']['profile'])
        self.assertEqual(len(self.ctx.calls), 1)
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(self.ctx.conversation_manager.pairs, [])
        self.assertNotIn('queued_messages', self.ctx.calls[0]['prompt'])

    async def test_cached_and_concurrent_requests_only_write_once(self):
        result = await asyncio.gather(self.write(), self.write(), self.write())
        self.assertEqual(len(self.ctx.calls), 1)
        self.assertEqual(result[0], result[2])
        await self.write()
        self.assertEqual(len(self.ctx.calls), 1)

    async def test_force_increments_revision_and_preserves_created_time(self):
        first = copy.deepcopy(await self.write())
        second = await self.write(force=True)
        self.assertEqual(second['revision'], 2)
        self.assertEqual(first['created_at'], second['created_at'])
        self.assertGreaterEqual(second['updated_at'], first['updated_at'])

    async def test_failure_preserves_existing_diary(self):
        entry = copy.deepcopy(await self.write())
        self.plugin.generator.diary = AsyncMock(side_effect=RuntimeError('fixture-secret'))
        with self.assertRaises(RuntimeError):
            await self.write(force=True)
        self.assertEqual(self.plugin.store.data['diaries']['traveler'][self.day], entry)

    async def test_cancellation_preserves_existing_diary(self):
        entry = copy.deepcopy(await self.write())
        started = asyncio.Event()
        async def blocked(*args):
            started.set()
            await asyncio.Event().wait()
        self.plugin.generator.diary = blocked
        task = asyncio.create_task(self.write(force=True))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.plugin.store.data['diaries']['traveler'][self.day], entry)

    async def test_today_and_future_are_rejected_without_llm(self):
        for offset in (0, 1):
            day = (self.today + timedelta(days=offset)).isoformat()
            await self.plugin.ensure(self.persona, UMO, day)
            self.ctx.calls.clear()
            with self.assertRaisesRegex(ValueError, '尚未结束'):
                await self.plugin.ensure_diary(self.persona, day, UMO)
            self.assertFalse(self.ctx.calls)

    async def test_missing_plan_is_not_invented_retrospectively(self):
        with self.assertRaisesRegex(ValueError, '没有已保存'):
            await self.plugin.ensure_diary(self.persona, '2020-01-01', UMO)
        self.assertFalse(self.ctx.calls)

    async def test_midnight_uses_character_timezone(self):
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 11, 15, 1, tzinfo=timezone.utc).astimezone(tz)
        record = self.plugin.store.data['personas']['traveler']
        saved = copy.deepcopy(record['days'][self.day])
        saved.update(date='2026-10-11', timezone='Asia/Tokyo')
        record['days']['2026-10-11'] = saved
        with patch('astrbot_plugin_persona_schedule.main.datetime', Frozen):
            entry = await self.plugin.ensure_diary(self.persona, '2026-10-11', UMO)
            self.assertEqual(entry['timezone'], 'Asia/Tokyo')
            saved['timezone'] = 'Asia/Shanghai'
            with self.assertRaisesRegex(ValueError, '尚未结束'):
                await self.plugin.ensure_diary(self.persona, '2026-10-11', UMO, force=True)

    async def test_read_api_lists_archive_and_never_calls_model(self):
        await self.write()
        self.ctx.calls.clear()
        data = await self.plugin.web_diary({'persona_id':'traveler', 'date':self.day})
        self.assertEqual(data['entries'][0]['date'], self.day)
        self.assertTrue(data['can_generate'])
        self.assertNotIn('persona_key', data['entry'])
        self.assertFalse(self.ctx.calls)
        data = await self.plugin.web_diary({'persona_id':'traveler', 'date':self.today.isoformat()})
        self.assertFalse(data['can_generate'])
        self.assertIsNone(data['entry'])

    async def test_archive_survives_schedule_and_profile_regeneration(self):
        entry = copy.deepcopy(await self.write())
        await self.plugin.ensure(self.persona, UMO, self.today.isoformat(), force='profile')
        self.assertEqual(self.plugin.store.data['diaries']['traveler'][self.day], entry)
        # The snapshot permits rewriting after the daily cache is invalidated.
        rewritten = await self.write(force=True)
        self.assertEqual(rewritten['revision'], 2)

    async def test_archive_survives_restart(self):
        entry = await self.write()
        reloaded = Store(self.temp.name)
        self.assertEqual(reloaded.data['diaries']['traveler'][self.day], entry)

    async def test_persona_change_preserves_history_and_marks_old_identity(self):
        await self.write()
        self.ctx.persona_manager.item['system_prompt'] = '人设已改变'
        data = await self.plugin.web_diary({'persona_id':'traveler','date':self.day})
        self.assertTrue(data['entry']['persona_changed'])
        self.assertFalse(data['can_generate'])
        self.assertIn(self.day, self.plugin.store.data['diaries']['traveler'])

    async def test_source_change_during_generation_discards_stale_result(self):
        async def changed(*args):
            self.plugin.store.data['personas']['traveler']['days'][self.day]['summary'] = '新安排'
            return {'title':'old', 'content':'stale', 'mood':''}
        self.plugin.generator.diary = changed
        with self.assertRaisesRegex(ValueError, '已变化'):
            await self.write()
        self.assertNotIn(self.day, self.plugin.store.data['diaries']['traveler'])

    async def test_prompt_change_during_generation_discards_result(self):
        async def changed(*args):
            self.ctx.persona_manager.item['system_prompt'] = '新的角色'
            return {'title':'old','content':'stale','mood':''}
        self.plugin.generator.diary = changed
        with self.assertRaisesRegex(ValueError, '已变化'):
            await self.write()
        self.assertNotIn(self.day, self.plugin.store.data['diaries']['traveler'])

    async def test_recent_diaries_exclude_other_identity_and_future(self):
        entry = copy.deepcopy(await self.write())
        entries = self.plugin.store.data['diaries']['traveler']
        entries['2020-01-01'] = {**entry, 'persona_key':'other', 'content':'private-old-persona'}
        entries['2099-01-01'] = {**entry, 'content':'future'}
        await self.write(force=True)
        recent = json.loads(self.ctx.calls[-1]['prompt'])['recent_diaries']
        self.assertEqual(recent, [])

    async def test_retention_keeps_latest_dates(self):
        entry = copy.deepcopy(await self.write())
        entries = self.plugin.store.data['diaries']['traveler']
        for day in range(1, 10):
            entries[f'2020-01-{day:02}'] = {**entry, 'date':f'2020-01-{day:02}'}
        self.plugin.store.settings['diary_limit'] = 7
        await self.write(force=True)
        self.assertEqual(len(entries), 7)
        self.assertIn(self.day, entries)
        self.assertNotIn('2020-01-01', entries)

    async def test_automatic_tick_writes_saved_yesterday_once_without_sessions(self):
        self.plugin.store.settings['enabled'] = True
        await self.plugin.tick()
        await self.drain()
        self.assertIn(self.day, self.plugin.store.data['diaries']['traveler'])
        await self.plugin.tick()
        await self.drain()
        self.assertEqual(len(self.ctx.calls), 1)
        self.assertFalse(self.ctx.sent)

    async def test_automatic_tick_crosses_local_midnight_once(self):
        record = self.plugin.store.data['personas']['traveler']
        saved = copy.deepcopy(record['days'][self.day])
        saved.update(date='2026-10-11', timezone='Asia/Tokyo')
        record['days'] = {'2026-10-11': saved}
        record['profile']['timezone'] = 'Asia/Tokyo'
        self.plugin.store.settings['enabled'] = True
        clock = datetime(2026, 10, 11, 14, 59, 59, tzinfo=timezone.utc)
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.astimezone(tz)
        with patch('astrbot_plugin_persona_schedule.main.datetime', Frozen), \
             patch('astrbot_plugin_persona_schedule.main.time.time', side_effect=lambda: clock.timestamp()):
            await self.plugin.tick()
            await self.drain()
            self.assertFalse(self.ctx.calls)
            clock += timedelta(seconds=2)
            await self.plugin.tick()
            await self.drain()
            self.assertIn('2026-10-11', self.plugin.store.data['diaries']['traveler'])
            await self.plugin.tick()
            await self.drain()
            self.assertEqual(len(self.ctx.calls), 1)
        self.assertFalse(self.ctx.sent)

    async def test_global_off_or_diary_off_makes_no_automatic_calls(self):
        for enabled, diary in ((False,True),(True,False)):
            self.plugin.store.settings.update(enabled=enabled, diary_enabled=diary)
            await self.plugin.tick()
            await self.drain()
            self.assertFalse(self.ctx.calls)

    async def test_scheduler_uses_saved_day_timezone_after_profile_timezone_change(self):
        # At this instant yesterday has ended in Tokyo but not Shanghai.
        # A later profile edit must not move the saved day's archive boundary.
        clock = datetime(2026, 10, 11, 15, 1, tzinfo=timezone.utc)
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.astimezone(tz)
        record = self.plugin.store.data['personas']['traveler']
        saved = copy.deepcopy(record['days'][self.day])
        self.plugin.store.settings['enabled'] = True
        with patch('astrbot_plugin_persona_schedule.main.datetime', Frozen), \
             patch('astrbot_plugin_persona_schedule.main.time.time', return_value=clock.timestamp()):
            for saved_tz, profile_tz, expected in (
                ('Asia/Tokyo', 'Asia/Shanghai', True),
                ('Asia/Shanghai', 'Asia/Tokyo', False),
            ):
                with self.subTest(saved_timezone=saved_tz, profile_timezone=profile_tz):
                    record['profile']['timezone'] = profile_tz
                    record['days'] = {'2026-10-11': {**saved, 'date':'2026-10-11', 'timezone':saved_tz}}
                    self.plugin.store.data['diaries']['traveler'] = {}
                    self.ctx.calls.clear()
                    await self.plugin.tick()
                    await self.drain()
                    self.assertEqual('2026-10-11' in self.plugin.store.data['diaries']['traveler'], expected)
                    self.assertEqual(len(self.ctx.calls), int(expected))
        self.assertFalse(self.ctx.sent)

    async def test_whitelist_does_not_restrict_personal_archive(self):
        self.plugin.store.settings.update(enabled=True, whitelist_enabled=True, whitelist=[])
        await self.plugin.tick()
        await self.drain()
        self.assertIn(self.day, self.plugin.store.data['diaries']['traveler'])
        self.assertFalse(self.ctx.sent)

    async def test_auto_catches_up_only_recent_saved_days_one_per_tick(self):
        record = self.plugin.store.data['personas']['traveler']
        sample = record['days'][self.day]
        dates = [(self.today-timedelta(days=n)).isoformat() for n in (2, 7, 8)]
        for day in dates:
            record['days'][day] = {**sample,'date':day}
        self.plugin.store.settings['enabled'] = True
        await self.plugin.tick()
        await self.drain()
        self.assertEqual(list(self.plugin.store.data['diaries']['traveler']), [self.day])
        for _ in range(3):
            await self.plugin.tick()
            await self.drain()
        self.assertEqual(set(self.plugin.store.data['diaries']['traveler']), {self.day,*dates[:2]})
        self.assertEqual(len(self.ctx.calls), 3)

    async def test_auto_failure_is_backed_off_and_secrets_are_not_logged(self):
        self.plugin.store.settings['enabled'] = True
        self.plugin.generator.diary = AsyncMock(side_effect=RuntimeError('fixture-secret'))
        await self.plugin.tick()
        await self.drain()
        await self.plugin.tick()
        await self.drain()
        self.assertEqual(self.plugin.generator.diary.await_count, 1)
        self.assertNotIn('fixture-secret', json.dumps(self.plugin.store.data['logs']))

    async def test_disabling_during_auto_generation_does_not_commit(self):
        self.plugin.store.settings['enabled'] = True
        async def disabled(*args):
            self.plugin.store.settings['diary_enabled'] = False
            return {'title':'no', 'content':'no', 'mood':''}
        self.plugin.generator.diary = disabled
        self.assertIsNone(await self.write(automatic=True))
        self.assertNotIn(self.day, self.plugin.store.data['diaries']['traveler'])

    async def test_manual_generation_job_works_even_when_auto_disabled(self):
        self.plugin.store.settings['diary_enabled'] = False
        job_id = (await self.plugin.web_generate({'persona_id':'traveler','date':self.day,'scope':'diary'}))['job_id']
        await self.drain()
        job = await self.plugin.web_job({'id':job_id})
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job['progress'], 100)
        self.assertIn(self.day, self.plugin.store.data['diaries']['traveler'])

    async def test_invalid_manual_date_rejected_before_job_creation(self):
        with self.assertRaises(ValueError):
            await self.plugin.web_generate({'persona_id':'traveler','date':'','scope':'diary'})
        self.assertFalse(self.plugin.jobs)

    async def test_diary_provider_can_follow_plan_or_be_independent(self):
        # Existing provider selection unit tests validate the provider lookup;
        # this fixture supplies the selected IDs without external providers.
        self.ctx.get_all_providers = None
        self.plugin.store.settings.update(schedule_provider_id='plan', reply_provider_id='reply')
        await self.write()
        self.assertEqual(self.ctx.calls[-1]['chat_provider_id'], 'plan')
        self.plugin.store.settings['diary_provider_id'] = 'diary'
        await self.write(force=True)
        self.assertEqual(self.ctx.calls[-1]['chat_provider_id'], 'diary')
        self.assertEqual(self.plugin.store.settings['reply_provider_id'], 'reply')


class DiaryValidationTests(unittest.TestCase):
    def test_diary_schema_requires_nonempty_plain_text(self):
        for bad in ({}, {'title':'a','content':''}, {'title':2,'content':'a'}, {'title':'a','content':[]}):
            with self.assertRaises(ValueError):
                validate_diary(bad)
        value = validate_diary({'title':'  标题  ','content':'正文\n第二段'})
        self.assertEqual(value, {'title':'标题','content':'正文\n第二段','mood':''})

    def test_diary_settings_bounds_and_defaults(self):
        self.assertTrue(DEFAULTS['diary_enabled'])
        self.assertFalse(DEFAULTS['enabled'])
        for bad in ({'diary_enabled':'true'}, {'diary_limit':6}, {'diary_limit':1096}, {'diary_limit':True}, {'diary_provider_id':1}):
            with self.assertRaises(ValueError):
                validate_settings(bad, DEFAULTS)
