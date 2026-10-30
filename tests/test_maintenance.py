"""Actual editor contracts, reviewed import and journal source lifecycle."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_schedule import ActualPersonaSchedule, FakeConfig, FakeContext, FakeEvent, Store, UMO, slots
from astrbot_plugin_persona_schedule.core import DEFAULTS, validate_settings
from astrbot_plugin_persona_schedule.diary_context import add_material, prune_materials
from astrbot_plugin_persona_schedule.maintenance import EditConflict


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ctx = FakeContext()
        with patch('astrbot_plugin_persona_schedule.main.StarTools.get_data_dir', return_value=self.temp.name):
            self.plugin = ActualPersonaSchedule(self.ctx, FakeConfig({'enabled': True, 'whitelist': [UMO],
                'whitelist_enabled': True, 'auto_schedule_enabled': False, 'backup_enabled': False}))
        self.persona = await self.plugin._persona('traveler')
        self.today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
        self.day = (self.today - timedelta(days=1)).isoformat()
        await self.plugin.ensure(self.persona, UMO, self.day)
        self.env = await self.plugin.environment(UMO)
        self.at = datetime.fromisoformat(self.day+'T20:00').replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    def token(self):
        return self.plugin._edit_token(self.persona)

    def edit(self, token=None, summary='updated'):
        value = {'persona_id': 'traveler', 'date': self.day, 'kind': 'day', 'value': {'slots': slots(), 'summary': summary}}
        if token is not None:
            value['expected_token'] = token
        return value

    async def test_missing_edit_token_is_rejected(self):
        before = copy.deepcopy(self.plugin.store.data)
        with self.assertRaises(EditConflict):
            await self.plugin.web_save(self.edit())
        self.assertEqual(self.plugin.store.data, before)

    async def test_two_editors_cannot_overwrite_newer_day(self):
        baseline = self.token()
        await self.plugin.web_save(self.edit(baseline, 'newer'))
        with self.assertRaises(EditConflict):
            await self.plugin.web_save(self.edit(baseline, 'old browser'))
        self.assertEqual(self.plugin.store.data['personas']['traveler']['days'][self.day]['summary'], 'newer')

    async def test_chat_and_runtime_statistics_do_not_invalidate_editor_token(self):
        baseline = self.token()
        add_material(self.plugin.store, self.env, 'user', 'a new text', self.at, 'one')
        self.plugin.store.data['model_usage'] = {'days': {}}
        self.plugin.store.log('ordinary log')
        await self.plugin.web_save(self.edit(baseline))

    async def test_rule_and_restore_requests_check_client_baseline(self):
        baseline = self.token()
        saved = await self.plugin.web_save(self.edit(baseline))
        for operation, payload in (
            (self.plugin.web_save_recurrences, {'rules': []}),
            (self.plugin.web_restore, {'id': saved['snapshot_id']})):
            with self.subTest(operation=operation.__name__), self.assertRaises(EditConflict):
                await operation({'persona_id': 'traveler', 'expected_token': baseline, **payload})

    async def test_backup_preview_and_export_do_not_modify_runtime(self):
        self.plugin.store.settings['tavily_api_key'] = 'private-key-fixture'
        add_material(self.plugin.store, self.env, 'user', 'private-chat-fixture', self.at, 'secret')
        backup = await self.plugin.web_backup_create({})
        before = copy.deepcopy(self.plugin.store.data)
        share = await self.plugin.web_export({'persona_id': 'traveler'})
        raw = json.dumps(share)
        self.assertNotIn('private-key-fixture', raw)
        self.assertNotIn('private-chat-fixture', raw)
        self.assertNotIn(UMO, raw)
        preview = await self.plugin.web_import_preview({'persona_id': 'traveler', 'backup_id': backup['backup']['id']})
        self.assertIn('preview_token', preview)
        self.assertEqual(self.plugin.store.data, before)
        self.assertFalse(self.ctx.sent)

    async def test_import_requires_review_token_and_rejects_changes_after_preview(self):
        share = await self.plugin.web_export({'persona_id': 'traveler'})
        preview = await self.plugin.web_import_preview({'persona_id': 'traveler', 'data': share})
        await self.plugin.web_save(self.edit(self.token()))
        with self.assertRaises(EditConflict):
            await self.plugin.web_import_apply({'persona_id': 'traveler', 'expected_token': self.token(),
                                                'preview_token': preview['preview_token']})
        with self.assertRaises(ValueError):
            await self.plugin.web_import_apply({'persona_id': 'traveler', 'expected_token': self.token(), 'preview_token': 'missing'})

    async def test_import_restores_only_selected_plan_never_queue_or_lottery(self):
        from astrbot_plugin_persona_schedule.backups import export_persona
        share = await self.plugin.web_export({'persona_id': 'traveler'})
        share['record']['days'][self.day]['summary'] = 'restored plan'
        share = export_persona(share['record'], 'traveler')
        self.plugin.store.enqueue(UMO, self.env['cid'], self.persona['key'], 'user', 'pending', time.time(), time.time()+300, 'busy')
        self.plugin.store.data['incidents'] = {'spent': {'id': 'spent', 'persona_id': 'traveler', 'persona_key': self.persona['key'],
                                                       'date': self.day, 'status': 'missed', 'started_at': None}}
        queue = copy.deepcopy(self.plugin.store.data['queues'])
        lottery = copy.deepcopy(self.plugin.store.data['incidents'])
        preview = await self.plugin.web_import_preview({'persona_id': 'traveler', 'data': share})
        result = await self.plugin.web_import_apply({'persona_id': 'traveler', 'preview_token': preview['preview_token'],
                                                     'expected_token': preview['expected_token']})
        self.assertTrue(result['saved'])
        self.assertEqual(self.plugin.store.data['personas']['traveler']['days'][self.day]['summary'], 'restored plan')
        self.assertEqual(self.plugin.store.data['queues'], queue)
        self.assertEqual(self.plugin.store.data['incidents'], lottery)
        self.assertFalse(self.ctx.sent)
        self.assertTrue(self.plugin.store.data['edit_history']['traveler'])
        with self.assertRaises(ValueError):
            await self.plugin.web_import_apply({'persona_id': 'traveler', 'preview_token': preview['preview_token'], 'expected_token': self.token()})

    async def test_import_expiry_and_invalid_data_leave_store_untouched(self):
        share = await self.plugin.web_export({'persona_id': 'traveler'})
        preview = await self.plugin.web_import_preview({'persona_id': 'traveler', 'data': share})
        self.plugin.import_previews[preview['preview_token']]['expires_at'] = 1
        before = copy.deepcopy(self.plugin.store.data)
        with self.assertRaises(ValueError):
            await self.plugin.web_import_apply({'persona_id': 'traveler', 'expected_token': self.token(), 'preview_token': preview['preview_token']})
        with self.assertRaises(ValueError):
            await self.plugin.web_import_preview({'persona_id': 'traveler', 'data': {'settings': {'enabled': True}}})
        self.assertEqual(self.plugin.store.data, before)

    async def test_source_update_flags_new_material_but_not_expiration_or_revocation(self):
        add_material(self.plugin.store, self.env, 'user', 'old text', self.at, 'one')
        await self.plugin.ensure_diary(self.persona, self.day, UMO)
        result = await self.plugin.web_diary({'persona_id': 'traveler', 'date': self.day})
        self.assertFalse(result['source_changed'])
        add_material(self.plugin.store, self.env, 'user', 'late input from that day', self.at+1, 'two')
        result = await self.plugin.web_diary({'persona_id': 'traveler', 'date': self.day})
        self.assertTrue(result['source_changed'])
        await self.plugin.ensure_diary(self.persona, self.day, UMO, force=True)
        self.plugin.store.settings['whitelist'] = []
        prune_materials(self.plugin.store, time.time())
        result = await self.plugin.web_diary({'persona_id': 'traveler', 'date': self.day})
        self.assertFalse(result['source_changed'])

    async def test_source_update_flags_edited_plan_without_auto_rewrite(self):
        entry = copy.deepcopy(await self.plugin.ensure_diary(self.persona, self.day, UMO))
        await self.plugin.web_save(self.edit(self.token(), 'new historical plan'))
        result = await self.plugin.web_diary({'persona_id': 'traveler', 'date': self.day})
        self.assertTrue(result['source_changed'])
        self.assertEqual(result['entry']['content'], entry['content'])
        self.plugin._schedule_diaries(time.time())
        self.assertFalse(self.plugin.diary_tasks)

    async def test_archive_buffer_is_applied_only_to_automatic_scheduler(self):
        self.assertEqual(self.plugin.store.settings['diary_archive_delay_minutes'], 5)
        midnight = datetime.combine(self.today, datetime.min.time(), ZoneInfo('Asia/Shanghai')).timestamp()
        self.plugin._schedule_diaries(midnight+299)
        self.assertFalse(self.plugin.diary_tasks)
        self.plugin._schedule_diaries(midnight+300)
        self.assertIn('traveler', self.plugin.diary_tasks)
        await asyncio.gather(*list(self.plugin.tasks))
        self.assertIn(self.day, self.plugin.store.data['diaries']['traveler'])

    async def test_diary_adjustment_facts_survive_runtime_log_rotation(self):
        change = {"persona_id": self.persona["id"], "persona_key": self.persona["key"], "date": self.day,
                  "action": "defer", "activity_kind": "sleep", "delay_minutes": 15, "tone": "tired",
                  "starts_at": self.at, "until": self.at + 900, "reason": "private conversation detail"}
        self.plugin.store.data["adaptation_log"] = [change]
        entry = await self.plugin.ensure_diary(self.persona, self.day, UMO)
        facts = copy.deepcopy(entry["source"]["schedule"]["contact_adjustments"])
        self.assertNotIn("private conversation detail", json.dumps(facts))
        self.plugin.store.data["adaptation_log"] = []
        current = await self.plugin.web_diary({"persona_id": self.persona["id"], "date": self.day})
        self.assertFalse(current["source_changed"])
        rewritten = await self.plugin.ensure_diary(self.persona, self.day, UMO, force=True)
        self.assertEqual(rewritten["source"]["schedule"]["contact_adjustments"], facts)

    async def test_new_settings_reject_invalid_probabilities_and_counts(self):
        self.assertFalse(DEFAULTS['incident_enabled'])
        self.assertEqual(DEFAULTS['incident_probability'], .2)
        for value in (True, '0.2', float('nan'), float('inf'), -.1, 1.1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_settings({'incident_probability': value}, DEFAULTS)
        for key, value in (('diary_archive_delay_minutes', 121), ('daily_background_call_limit', -1), ('backup_limit', 0)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_settings({key: value}, DEFAULTS)

    async def test_edit_cancels_only_incidents_from_changed_dates(self):
        events = {name: {"id": name, "persona_id": self.persona["id"], "persona_key": self.persona["key"],
                         "date": day, "status": "pending"}
                  for name, day in (("edited", self.day), ("unrelated", self.today.isoformat()))}
        self.plugin.store.data["incidents"] = events
        await self.plugin.web_save(self.edit(self.token(), "changed historical day"))
        self.assertEqual(self.plugin.store.data["incidents"]["edited"]["status"], "cancelled")
        self.assertEqual(self.plugin.store.data["incidents"]["unrelated"], events["unrelated"])


if __name__ == '__main__':
    unittest.main()
