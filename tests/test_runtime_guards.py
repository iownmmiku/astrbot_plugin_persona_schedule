"""Optional generation rechecks revocation after shared model queue waits."""
from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from contextlib import AsyncExitStack
from datetime import date, timedelta
from unittest.mock import patch

from test_schedule import ActualPersonaSchedule, FakeConfig, FakeContext, UMO


class RuntimeGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ctx = FakeContext()
        with patch("astrbot_plugin_persona_schedule.main.StarTools.get_data_dir", return_value=self.temp.name):
            self.plugin = ActualPersonaSchedule(self.ctx, FakeConfig({"enabled": True,
                "auto_schedule_enabled": True, "backup_enabled": False}))
        self.persona = await self.plugin._persona("traveler")
        await self.plugin.ensure(self.persona, UMO)
        self.saved = copy.deepcopy(self.plugin.store.data["personas"])
        self.ctx.calls.clear()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def queued(self, scope, mutate):
        limiter = self.plugin.generator.runtime.limiter
        async with AsyncExitStack() as stack:
            for _ in range(limiter.background_capacity):
                await stack.enter_async_context(limiter.slot(background=True))
            operation = asyncio.create_task(self.plugin.ensure(self.persona, UMO, force=scope, automatic=True))
            for _ in range(100):
                if limiter.waiters:
                    break
                await asyncio.sleep(0)
            self.assertTrue(limiter.waiters)
            mutate()
        with self.assertRaises(ValueError):
            await operation
        self.assertFalse(self.ctx.calls)
        self.assertEqual(limiter.active, 0)

    async def test_each_generation_stage_checks_switch_after_queue_wait(self):
        for scope in ("profile", "calendar", "day"):
            with self.subTest(scope=scope):
                self.plugin.store.settings["auto_schedule_enabled"] = True
                usage = copy.deepcopy(self.plugin.store.data.get("model_usage"))
                await self.queued(scope, lambda: self.plugin.store.settings.update(auto_schedule_enabled=False))
                self.assertEqual(self.plugin.store.data["personas"], self.saved)
                self.assertEqual(self.plugin.store.data.get("model_usage"), usage)

    async def test_persona_change_while_waiting_does_not_call_old_model(self):
        await self.queued("profile", lambda: self.ctx.persona_manager.item.update(system_prompt="新角色"))
        self.assertEqual(self.plugin.store.data["personas"], self.saved)

    async def test_record_change_while_waiting_preserves_new_source(self):
        def replace_record():
            self.plugin.store.data["personas"]["traveler"]["profile"]["identity"] = "已编辑的身份"
        await self.queued("day", replace_record)
        self.assertEqual(self.plugin.store.data["personas"]["traveler"]["profile"]["identity"], "已编辑的身份")

    async def test_disable_during_provider_lookup_does_not_call_model(self):
        original = self.plugin.generator.provider_id
        async def lookup(*args, **kwargs):
            result = await original(*args, **kwargs)
            self.plugin.store.settings["enabled"] = False
            return result
        self.plugin.generator.provider_id = lookup
        with self.assertRaises(ValueError):
            await self.plugin.ensure(self.persona, UMO, force="day", automatic=True)
        self.assertFalse(self.ctx.calls)
        self.assertEqual(self.plugin.store.data["personas"], self.saved)

    async def test_manual_generation_remains_available_with_switch_off(self):
        self.plugin.store.settings.update(enabled=False, auto_schedule_enabled=False)
        result = await self.plugin.ensure(self.persona, UMO, force="day")
        self.assertTrue(result[2]["slots"])
        self.assertEqual(len(self.ctx.calls), 1)

    async def test_cached_foreground_plan_does_not_wait_for_background_generation(self):
        today = next(iter(self.saved["traveler"]["days"]))
        tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
        started, release = asyncio.Event(), asyncio.Event()
        original = self.ctx.llm_generate
        async def blocked(**kwargs):
            started.set()
            await release.wait()
            return await original(**kwargs)
        self.ctx.llm_generate = blocked
        background = asyncio.create_task(self.plugin.ensure(self.persona, UMO, tomorrow, automatic=True))
        try:
            await asyncio.wait_for(started.wait(), 1)
            before = copy.deepcopy(self.plugin.store.data)
            foreground = await asyncio.wait_for(self.plugin.ensure(self.persona, UMO, today), 1)
            self.assertEqual(foreground[2]["date"], today)
            self.assertEqual(self.plugin.store.data, before)
            self.assertFalse(background.done())
        finally:
            release.set()
            await background

    async def test_disabled_automatic_cached_read_still_returns_none(self):
        self.plugin.store.settings["auto_schedule_enabled"] = False
        self.assertIsNone(await self.plugin.ensure(self.persona, UMO, automatic=True))
        self.assertFalse(self.ctx.calls)

    async def test_cached_fast_path_refuses_stale_persona(self):
        self.ctx.persona_manager.item["system_prompt"] = "已更换的角色"
        with self.assertRaises(ValueError):
            await self.plugin.ensure(self.persona, UMO)
        self.assertFalse(self.ctx.calls)

    async def test_pending_recurrence_update_does_not_take_fast_path(self):
        from astrbot_plugin_persona_schedule.recurrence import validate_rules
        today = next(iter(self.saved["traveler"]["days"]))
        self.plugin.store.data["personas"]["traveler"]["recurrences"] = validate_rules([{
            "id": "annual", "title": "年年之约", "month": int(today[5:7]), "day": int(today[8:10]),
            "duration_days": 1, "calendar_system": "gregorian", "kind": "anniversary",
            "effect": "约定", "source": "manual", "enabled": True}])
        _, calendar, schedule = await self.plugin.ensure(self.persona, UMO, today)
        self.assertTrue(any(row["title"] == "年年之约" for row in calendar["events"]))
        self.assertTrue(any(row["title"] == "年年之约" for row in schedule["events"]))
        self.assertEqual(len(self.ctx.calls), 1)


if __name__ == "__main__":
    unittest.main()
