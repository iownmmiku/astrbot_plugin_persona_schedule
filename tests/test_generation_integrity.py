"""Queued edits and generated plans must not replace a newer persona's data."""
from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from test_schedule import FakeConfig, FakeContext, PersonaSchedule, Store, UMO, slots


class GenerationIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ctx = FakeContext()
        with patch("astrbot_plugin_persona_schedule.main.StarTools.get_data_dir", return_value=self.temp.name):
            self.plugin = PersonaSchedule(self.ctx, FakeConfig())
        self.persona = await self.plugin._persona("traveler")
        self.day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        await self.plugin.ensure(self.persona, UMO, self.day)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    @property
    def records(self):
        return self.plugin.store.data["personas"]

    async def test_prompt_change_during_generation_preserves_saved_plan(self):
        before = copy.deepcopy(self.records)
        generate = self.plugin.generator.day

        async def changed(*args, **kwargs):
            result = await generate(*args, **kwargs)
            self.ctx.persona_manager.item["system_prompt"] = "新角色设定"
            return result

        self.plugin.generator.day = changed
        with self.assertRaisesRegex(ValueError, "人设已变化"):
            await self.plugin.ensure(self.persona, UMO, self.day, force="day")
        self.assertEqual(self.records, before)
        self.assertEqual(Store(self.temp.name).data["personas"], before)

    async def test_failed_generation_for_changed_persona_preserves_old_record(self):
        before = copy.deepcopy(self.records)
        self.ctx.persona_manager.item["system_prompt"] = "新角色设定"
        persona = await self.plugin._persona("traveler")
        self.plugin.generator.profile = AsyncMock(side_effect=RuntimeError("offline"))
        with self.assertRaises(RuntimeError):
            await self.plugin.ensure(persona, UMO, self.day)
        self.assertEqual(self.records, before)
        self.plugin.store.save()  # A later unrelated save must not persist an empty record.
        self.assertEqual(Store(self.temp.name).data["personas"], before)

    async def test_viewing_changed_persona_hides_old_plan_without_erasing_it(self):
        before = copy.deepcopy(self.records)
        self.ctx.persona_manager.item["system_prompt"] = "新角色设定"
        detail = await self.plugin.web_detail({"persona_id": "traveler", "date": self.day})
        self.assertIsNone(detail["profile"])
        self.assertIsNone(detail["day"])
        state = await self.plugin.web_state({})
        self.assertFalse(state["personas"][0]["ready"])
        self.assertEqual(state["runtime"]["personas"]["traveler"]["current_status"], "unknown")
        self.assertEqual(self.records, before)
        self.plugin.store.save()
        self.assertEqual(Store(self.temp.name).data["personas"], before)

    async def test_plan_write_failure_rolls_memory_back(self):
        before = copy.deepcopy(self.records)
        with patch.object(self.plugin.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.plugin.ensure(self.persona, UMO, self.day, force="day")
        self.assertEqual(self.records, before)
        self.assertEqual(Store(self.temp.name).data["personas"], before)

    async def test_first_plan_write_failure_does_not_leave_partial_record(self):
        self.records.clear()
        self.plugin.store.save()
        with patch.object(self.plugin.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.plugin.ensure(self.persona, UMO, self.day)
        self.assertEqual(self.records, {})

    async def test_missing_legacy_lock_archive_is_initialized_on_generation(self):
        self.records["traveler"].pop("locked_days")
        self.records["traveler"]["days"].clear()
        _, _, day = await self.plugin.ensure(self.persona, UMO, self.day)
        self.assertEqual(day["date"], self.day)
        self.assertEqual(self.records["traveler"]["locked_days"], {})

    async def test_record_replaced_during_generation_is_not_overwritten(self):
        generate = self.plugin.generator.day
        replacement = copy.deepcopy(self.records["traveler"])
        replacement["days"][self.day]["summary"] = "已保存的更新安排"

        async def replaced(*args, **kwargs):
            result = await generate(*args, **kwargs)
            self.records["traveler"] = replacement
            self.plugin.store.save()
            return result

        self.plugin.generator.day = replaced
        with self.assertRaisesRegex(ValueError, "日程已变化"):
            await self.plugin.ensure(self.persona, UMO, self.day, force="day")
        self.assertIs(self.records["traveler"], replacement)
        self.assertEqual(Store(self.temp.name).data["personas"]["traveler"], replacement)

    async def test_waiting_operations_recheck_persona_after_lock(self):
        for action in ("generate", "day", "profile", "calendar", "recurrences"):
            with self.subTest(action=action):
                self.ctx.persona_manager.item["system_prompt"] = self.persona["prompt"]
                lock = self.plugin._lock(self.plugin.persona_locks, "traveler")
                await lock.acquire()
                if action == "generate":
                    operation = self.plugin.ensure(self.persona, UMO, self.day, force="day")
                elif action == "recurrences":
                    operation = self.plugin.web_save_recurrences({"persona_id": "traveler", "rules": []})
                else:
                    value = {"slots": slots("busy"), "summary": "旧编辑"} if action == "day" else (
                        copy.deepcopy(self.records["traveler"]["profile"]) if action == "profile" else [])
                    operation = self.plugin.web_save({"persona_id": "traveler", "date": self.day,
                                                      "kind": action, "value": value})
                task = asyncio.create_task(operation)
                await asyncio.sleep(0)
                self.ctx.persona_manager.item["system_prompt"] = "新角色 " + action
                current = await self.plugin._persona("traveler")
                replacement = copy.deepcopy(self.records["traveler"])
                replacement["key"] = current["key"]
                self.records["traveler"] = replacement
                before = copy.deepcopy(self.plugin.store.data)
                lock.release()
                with self.assertRaisesRegex(ValueError, "人设已变化"):
                    await task
                self.assertEqual(self.plugin.store.data, before)


if __name__ == "__main__":
    unittest.main()
