"""Exercise real preview/save/restore handlers without models or platform sends."""
from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from test_schedule import FakeConfig, FakeContext, PersonaSchedule, Store, UMO, profile, slots
from astrbot_plugin_persona_schedule.adaptive import create_adjustment, target_activity
from astrbot_plugin_persona_schedule.core import digest


DAY = "2026-10-29"


class WorkbenchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.context = FakeContext()
        self.config = FakeConfig({"enabled": True, "all_chats_enabled": True,
                                  "auto_schedule_enabled": False, "diary_enabled": False})
        with patch("astrbot_plugin_persona_schedule.main.StarTools.get_data_dir", return_value=self.temp.name):
            self.plugin = PersonaSchedule(self.context, self.config)
        self.persona = await self.plugin._persona("traveler")
        await self.plugin.ensure(self.persona, UMO, DAY)
        self.record = self.plugin.store.data["personas"]["traveler"]
        self.record["days"][DAY]["slots"] = [
            {"start": "00:00", "end": "21:00", "title": "街边闲逛", "status": "slow", "note": "在外面",
             "slow_min_seconds": 20, "slow_max_seconds": 40},
            {"start": "21:00", "end": "22:00", "title": "聊天", "status": "free", "note": ""},
            {"start": "22:00", "end": "24:00", "title": "睡觉", "status": "busy", "note": ""}]
        self.plugin.store.save()
        self.context.calls.clear()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def preview(self, **kwargs):
        return await self.plugin.web_preview({"persona_id": "traveler", "date": DAY,
            "time": "22:05", "message": "想再聊一会儿", "umo": UMO, **kwargs})

    async def save_day(self, title="编辑后"):
        return await self.plugin.web_save({"persona_id": "traveler", "kind": "day", "date": DAY,
            "value": {"slots": slots(title=title), "summary": title}})

    async def history(self):
        return await self.plugin.web_history({"persona_id": "traveler"})

    async def test_default_preview_is_read_only_and_never_calls_model_or_send(self):
        before, disk = copy.deepcopy(self.plugin.store.data), self.plugin.store.path.read_bytes()
        result = await self.preview()
        self.assertEqual(result["status"], "busy")
        self.assertTrue(result["would_queue"])
        self.assertTrue(result["read_only"])
        self.assertFalse(result["model_evaluated"])
        self.assertEqual(self.context.calls, [])
        self.assertEqual(self.context.sent, [])
        self.assertEqual(self.plugin.store.data, before)
        self.assertEqual(self.plugin.store.path.read_bytes(), disk)

    async def test_missing_cache_does_not_create_or_generate_record(self):
        self.plugin.store.data["personas"].clear()
        before = copy.deepcopy(self.plugin.store.data)
        with self.assertRaisesRegex(ValueError, "先生成或保存"):
            await self.preview()
        self.assertEqual(self.plugin.store.data, before)
        self.assertEqual(self.context.calls, [])

    async def test_preview_command_bypasses_busy_and_model(self):
        result = await self.preview(message="/reset", evaluate=True)
        self.assertTrue(result["scope"]["command_bypass"])
        self.assertFalse(result["would_queue"])
        self.assertFalse(result["model_evaluated"])
        self.assertEqual(self.context.calls, [])

    async def test_preview_uses_registered_command_prefix_config(self):
        self.context.get_config = lambda _umo: {"wake_prefix": ["!"]}
        result = await self.preview(message="!reset")
        self.assertTrue(result["scope"]["command_bypass"])

    async def test_empty_whitelist_is_not_global_even_in_preview(self):
        self.plugin.store.settings.update(whitelist_enabled=True, whitelist=[])
        result = await self.preview()
        self.assertEqual(result["status"], "busy")
        self.assertFalse(result["scope"]["allowed"])
        self.assertFalse(result["would_queue"])

    async def test_preview_without_session_still_shows_role_schedule(self):
        result = await self.preview(umo="")
        self.assertEqual(result["base_activity"]["title"], "睡觉")
        self.assertFalse(result["scope"]["allowed"])
        self.assertFalse(result["would_queue"])

    async def test_session_with_other_persona_does_not_inherit_adjustments(self):
        self.plugin.environment = AsyncMock(return_value={"umo": UMO, "cid": "conv-1",
            "persona": {"id": "someone", "key": "other-key"}})
        result = await self.preview()
        self.assertFalse(result["scope"]["allowed"])
        self.assertFalse(result["would_queue"])
        self.assertIn("人设", result["scope"]["reason"])

    async def test_slow_preview_reports_slot_delay_override(self):
        result = await self.preview(time="12:00")
        self.assertEqual(result["status"], "slow")
        self.assertEqual(result["delay_seconds"], [20, 40])
        self.assertTrue(result["would_queue"])

    async def test_free_preview_preserves_existing_queue_order_without_mutation(self):
        result = await self.preview(time="21:30")
        self.assertFalse(result["would_queue"])
        self.plugin.store.enqueue(UMO, "conv-1", self.persona["key"], "test", "older", 1, 2, "slow")
        before = copy.deepcopy(self.plugin.store.data)
        result = await self.preview(time="21:30")
        self.assertTrue(result["would_queue"])
        self.assertEqual(result["status"], "free")
        self.assertEqual(self.plugin.store.data, before)

    async def test_explicit_model_evaluation_uses_real_bounded_policy_without_committing(self):
        decision = {"action": "defer", "activity_kind": "sleep", "importance": "normal", "delay_minutes": 20,
                    "reason": "持续聊天，有限延后休息", "tone": "tired", "notice": "稍后就睡"}
        self.plugin.generator.adaptive = AsyncMock(return_value=decision)
        before = copy.deepcopy(self.plugin.store.data)
        result = await self.preview(evaluate=True, active_conversation=True)
        self.assertTrue(result["model_evaluated"])
        self.assertEqual(result["base_activity"]["status"], "busy")
        self.assertEqual(result["status"], "free")
        self.assertFalse(result["would_queue"])
        self.assertEqual(result["effective_activity"]["adaptive"]["tone"], "tired")
        self.assertEqual(self.plugin.store.data, before)
        args, kwargs = self.plugin.generator.adaptive.call_args
        self.assertEqual(args[5], [])
        self.assertTrue(kwargs["active_conversation"])

    async def test_model_cannot_defer_fixed_duties(self):
        self.record["days"][DAY]["slots"][-1]["title"] = "执勤"
        self.plugin.generator.adaptive = AsyncMock(return_value={"action": "defer", "activity_kind": "personal",
            "importance": "important", "delay_minutes": 20, "reason": "想聊", "tone": "plain", "notice": ""})
        result = await self.preview(evaluate=True)
        self.assertFalse(result["model_evaluated"])
        self.assertEqual(result["status"], "busy")
        self.assertIsNone(result["decision"])
        self.assertIn("保留", result["reason"])

    async def test_simulated_deferral_is_anchored_to_original_start(self):
        self.plugin.generator.adaptive = AsyncMock(return_value={"action": "defer", "activity_kind": "sleep",
            "importance": "important", "delay_minutes": 20, "reason": "再聊片刻", "tone": "tired", "notice": ""})
        result = await self.preview(time="22:25", evaluate=True, active_conversation=True)
        self.assertTrue(result["model_evaluated"])
        self.assertEqual(result["status"], "busy")
        self.assertIn("按原计划开始计时", result["reason"])

    async def test_locked_slot_skips_explicit_model(self):
        self.record["days"][DAY]["slots"][-1]["locked"] = True
        self.plugin.generator.adaptive = AsyncMock()
        result = await self.preview(evaluate=True)
        self.assertFalse(result["model_evaluated"])
        self.plugin.generator.adaptive.assert_not_called()

    async def test_model_failure_keeps_saved_busy_plan(self):
        self.plugin.generator.adaptive = AsyncMock(side_effect=OSError("secret endpoint"))
        before = copy.deepcopy(self.plugin.store.data)
        result = await self.preview(evaluate=True)
        self.assertEqual(result["status"], "busy")
        self.assertNotIn("secret", result["reason"])
        self.assertEqual(self.plugin.store.data, before)

    async def test_model_preview_cannot_apply_results_after_plan_changes(self):
        async def changed(*args, **kwargs):
            self.record["days"][DAY]["summary"] = "new plan"
            return {"action": "keep", "activity_kind": "sleep", "importance": "normal", "delay_minutes": 0,
                    "reason": "按计划", "tone": "plain", "notice": ""}
        self.plugin.generator.adaptive = AsyncMock(side_effect=changed)
        with self.assertRaisesRegex(ValueError, "预演期间"):
            await self.preview(evaluate=True)
        self.assertEqual(self.plugin.adjustments, {})

    async def test_preview_reuses_active_and_expired_budget_instead_of_asking_for_new_window(self):
        now = datetime.fromisoformat(DAY + "T22:05").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        plan = self.record["days"][DAY]
        target = target_activity(plan, now)
        decision = {"action": "defer", "activity_kind": "sleep", "importance": "normal", "delay_minutes": 20,
                    "reason": "再聊片刻", "tone": "tired", "notice": ""}
        adjustment = create_adjustment(decision, target, now, umo=UMO, cid="conv-1", persona_key=self.persona["key"],
            schedule_revision=plan["revision"])
        adjustment["source_fingerprint"] = digest(plan)
        self.plugin.adjustments[UMO] = adjustment
        self.plugin.generator.adaptive = AsyncMock()
        before = copy.deepcopy(self.plugin.store.data)
        for clock, expected in (("22:05", "free"), ("22:25", "busy")):
            result = await self.preview(time=clock, evaluate=True, active_conversation=True)
            self.assertEqual(result["status"], expected)
            self.assertFalse(result["model_evaluated"])
            self.assertIn("不申请新的预算", result["reason"])
        self.plugin.generator.adaptive.assert_not_called()
        self.assertEqual(self.plugin.store.data, before)

    async def test_expired_sleep_budget_cannot_restart_after_midnight(self):
        now = datetime.fromisoformat(DAY + "T22:05").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        plan = self.record["days"][DAY]
        next_day = "2026-10-30"
        self.record["days"][next_day] = {"date": next_day, "timezone": "Asia/Shanghai", "summary": "继续休息",
            "revision": 1, "slots": slots("busy", "睡觉"), "events": []}
        target = target_activity(plan, now, next_schedule=self.record["days"][next_day])
        decision = {"action": "defer", "activity_kind": "sleep", "importance": "normal", "delay_minutes": 20,
                    "reason": "再聊片刻", "tone": "tired", "notice": ""}
        self.plugin.adjustments[UMO] = create_adjustment(decision, target, now, umo=UMO, cid="conv-1",
            persona_key=self.persona["key"], schedule_revision=plan["revision"])
        self.plugin.generator.adaptive = AsyncMock()
        result = await self.preview(date=next_day, time="00:05", evaluate=True)
        self.assertEqual(result["status"], "busy")
        self.plugin.generator.adaptive.assert_not_called()

    async def test_model_preview_cannot_return_old_session_after_reset(self):
        async def reset(*args, **kwargs):
            self.context.conversation_manager.cid = "new-conversation"
            return {"action": "keep", "activity_kind": "sleep", "importance": "normal", "delay_minutes": 0,
                    "reason": "按计划", "tone": "plain", "notice": ""}
        self.plugin.generator.adaptive = AsyncMock(side_effect=reset)
        with self.assertRaisesRegex(ValueError, "会话或人设已变化"):
            await self.preview(evaluate=True)

    async def test_configuration_change_during_model_preview_is_stale(self):
        async def disabled(*args, **kwargs):
            self.plugin.store.settings["enabled"] = False
            return {"action": "keep", "activity_kind": "sleep", "importance": "normal", "delay_minutes": 0,
                    "reason": "按计划", "tone": "plain", "notice": ""}
        self.plugin.generator.adaptive = AsyncMock(side_effect=disabled)
        with self.assertRaisesRegex(ValueError, "配置或联系调整已变化"):
            await self.preview(evaluate=True)

    async def test_preview_rejects_invalid_time_and_non_boolean_switches(self):
        for value in ({"time": "24:00"}, {"evaluate": "true"}, {"active_conversation": 1}, {"umo": "oops"}):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                await self.preview(**value)

    async def test_day_edit_creates_pre_edit_snapshot_and_restore_revises_source(self):
        old = copy.deepcopy(self.record)
        result = await self.save_day()
        history = await self.history()
        self.assertEqual(history["entries"][0]["id"], result["snapshot_id"])
        self.assertTrue(history["entries"][0]["can_restore"])
        self.assertNotIn("record", history["entries"][0])
        self.assertIn("日期日程", history["entries"][0]["changes"][0])
        current_rev = self.plugin.store.data["personas"]["traveler"]["days"][DAY]["revision"]
        restored = await self.plugin.web_restore({"persona_id": "traveler", "id": result["snapshot_id"]})
        current = self.plugin.store.data["personas"]["traveler"]
        self.assertEqual(current["days"][DAY]["slots"], old["days"][DAY]["slots"])
        self.assertGreater(current["days"][DAY]["revision"], current_rev)
        self.assertTrue(restored["snapshot_id"])

    async def test_invalid_save_preserves_everything_without_history(self):
        for kind, value in (("day", {"slots": []}), ("calendar", [{"title": "bad"}]), ("profile", {}), ("wrong", {})):
            before = copy.deepcopy(self.plugin.store.data)
            with self.subTest(kind=kind), self.assertRaises((ValueError, TypeError)):
                await self.plugin.web_save({"persona_id": "traveler", "kind": kind, "date": DAY, "value": value})
            self.assertEqual(self.plugin.store.data, before)

    async def test_invalid_first_save_does_not_create_persona_record(self):
        self.plugin.store.data["personas"].clear()
        before = copy.deepcopy(self.plugin.store.data)
        with self.assertRaises(ValueError):
            await self.plugin.web_save({"persona_id": "traveler", "kind": "day", "date": DAY, "value": {"slots": []}})
        self.assertEqual(self.plugin.store.data, before)

    async def test_failed_disk_write_rolls_back_record_and_new_history(self):
        before = copy.deepcopy(self.plugin.store.data)
        disk = self.plugin.store.path.read_bytes()
        with patch.object(self.plugin.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.save_day()
        self.assertEqual(self.plugin.store.data, before)
        self.assertEqual(self.plugin.store.path.read_bytes(), disk)

    async def test_failed_first_save_does_not_create_record_or_history(self):
        self.plugin.store.data["personas"].clear()
        before = copy.deepcopy(self.plugin.store.data)
        with patch.object(self.plugin.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.save_day()
        self.assertEqual(self.plugin.store.data, before)

    async def test_failed_restore_preserves_existing_history_and_adjustments(self):
        saved = await self.save_day()
        self.plugin.adjustments[UMO] = {"persona_key": self.persona["key"], "until": 100}
        before = copy.deepcopy(self.plugin.store.data)
        disk = self.plugin.store.path.read_bytes()
        with patch.object(self.plugin.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.plugin.web_restore({"persona_id": "traveler", "id": saved["snapshot_id"]})
        self.assertEqual(self.plugin.store.data, before)
        self.assertEqual(self.plugin.store.path.read_bytes(), disk)
        self.assertEqual(self.plugin.adjustments[UMO]["until"], 100)

    async def test_restore_can_be_undone_and_does_not_restore_queues_or_diaries(self):
        saved = await self.save_day("first edit")
        self.plugin.store.data["diaries"]["traveler"] = {DAY: {"title": "keep journal"}}
        self.plugin.store.enqueue(UMO, "conv-1", self.persona["key"], "test", "latest message", 1, 2, "slow")
        queue, diaries = copy.deepcopy(self.plugin.store.data["queues"]), copy.deepcopy(self.plugin.store.data["diaries"])
        restored = await self.plugin.web_restore({"persona_id": "traveler", "id": saved["snapshot_id"]})
        await self.plugin.web_restore({"persona_id": "traveler", "id": restored["snapshot_id"]})
        self.assertEqual(self.plugin.store.data["personas"]["traveler"]["days"][DAY]["summary"], "first edit")
        self.assertEqual(self.plugin.store.data["queues"], queue)
        self.assertEqual(self.plugin.store.data["diaries"], diaries)

    async def test_restore_waiting_for_lock_rechecks_official_persona(self):
        saved = await self.save_day("keep this edit")
        lock = self.plugin._lock(self.plugin.persona_locks, self.persona["id"])
        await lock.acquire()
        looked_up = asyncio.Event()
        original_persona = self.plugin._persona

        async def observed_persona(pid):
            value = await original_persona(pid)
            looked_up.set()
            return value

        with patch.object(self.plugin, "_persona", side_effect=observed_persona):
            pending = asyncio.create_task(self.plugin.web_restore(
                {"persona_id": "traveler", "id": saved["snapshot_id"]}))
            try:
                await asyncio.wait_for(looked_up.wait(), 1)
                self.context.persona_manager.item["system_prompt"] = "新的角色身份"
                before, disk = copy.deepcopy(self.plugin.store.data), self.plugin.store.path.read_bytes()
            finally:
                lock.release()
            with self.assertRaisesRegex(ValueError, "人设已变化"):
                await pending
        self.assertEqual(self.plugin.store.data, before)
        self.assertEqual(self.plugin.store.path.read_bytes(), disk)

    async def test_restore_waiting_for_lock_does_not_overwrite_new_identity_record(self):
        saved = await self.save_day()
        lock = self.plugin._lock(self.plugin.persona_locks, self.persona["id"])
        await lock.acquire()
        looked_up = asyncio.Event()
        original_persona = self.plugin._persona

        async def observed_persona(pid):
            value = await original_persona(pid)
            looked_up.set()
            return value

        with patch.object(self.plugin, "_persona", side_effect=observed_persona):
            pending = asyncio.create_task(self.plugin.web_restore(
                {"persona_id": "traveler", "id": saved["snapshot_id"]}))
            try:
                await asyncio.wait_for(looked_up.wait(), 1)
                self.context.persona_manager.item["system_prompt"] = "新的角色身份"
                changed_persona = await original_persona("traveler")
                self.plugin.store.data["personas"]["traveler"] = {
                    **copy.deepcopy(self.record), "key": changed_persona["key"], "recurrences": []}
                self.plugin.store.save()
                before, disk = copy.deepcopy(self.plugin.store.data), self.plugin.store.path.read_bytes()
            finally:
                lock.release()
            with self.assertRaisesRegex(ValueError, "人设已变化"):
                await pending
        self.assertEqual(self.plugin.store.data, before)
        self.assertEqual(self.plugin.store.path.read_bytes(), disk)

    async def test_snapshot_survives_restart_and_is_bounded(self):
        for index in range(23):
            await self.save_day(str(index))
        history = await self.history()
        self.assertEqual(len(history["entries"]), 20)
        reloaded = Store(self.temp.name)
        self.assertEqual(len(reloaded.data["edit_history"]["traveler"]), 20)

    async def test_old_persona_snapshot_is_visible_but_cannot_restore(self):
        result = await self.save_day()
        self.context.persona_manager.item["system_prompt"] = "changed"
        history = await self.history()
        self.assertFalse(history["entries"][0]["can_restore"])
        before = copy.deepcopy(self.plugin.store.data)
        with self.assertRaisesRegex(ValueError, "人设已变化"):
            await self.plugin.web_restore({"persona_id": "traveler", "id": result["snapshot_id"]})
        self.assertEqual(self.plugin.store.data, before)

    async def test_unknown_or_corrupt_snapshot_cannot_change_current_plan(self):
        saved = await self.save_day()
        with self.assertRaisesRegex(ValueError, "不存在"):
            await self.plugin.web_restore({"persona_id": "traveler", "id": "missing"})
        snapshot = self.plugin.store.data["edit_history"]["traveler"][0]
        snapshot["record"]["days"][DAY]["summary"] = "tampered"
        before = copy.deepcopy(self.plugin.store.data)
        with self.assertRaisesRegex(ValueError, "校验失败"):
            await self.plugin.web_restore({"persona_id": "traveler", "id": saved["snapshot_id"]})
        self.assertEqual(self.plugin.store.data, before)

    async def test_profile_and_calendar_dependency_invalidations_can_be_undone(self):
        old = copy.deepcopy(self.record)
        for kind, value in (("profile", profile(world="original")), ("calendar", [])):
            saved = await self.plugin.web_save({"persona_id": "traveler", "kind": kind, "date": DAY, "value": value})
            self.assertNotIn(DAY, self.plugin.store.data["personas"]["traveler"]["days"])
            await self.plugin.web_restore({"persona_id": "traveler", "id": saved["snapshot_id"]})
            self.assertEqual(self.plugin.store.data["personas"]["traveler"]["days"][DAY]["slots"], old["days"][DAY]["slots"])

    async def test_restore_invalidates_only_same_persona_active_adjustments(self):
        saved = await self.save_day()
        self.plugin.adjustments[UMO] = {"persona_key": self.persona["key"]}
        self.plugin.adjustments["other"] = {"persona_key": "someone-else"}
        await self.plugin.web_restore({"persona_id": "traveler", "id": saved["snapshot_id"]})
        self.assertNotIn(UMO, self.plugin.adjustments)
        self.assertIn("other", self.plugin.adjustments)
