"""Incidents use the same effective plan in chat, previews, diaries and canvas."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_schedule import FakeConfig, FakeContext, FakeEvent, PersonaSchedule, UMO, slots
from test_incidents import DAY, VALUE, FixedRandom, stamp
from astrbot_plugin_persona_schedule.adaptive import create_adjustment, target_activity
from astrbot_plugin_persona_schedule.core import digest, slot_at
from astrbot_plugin_persona_schedule.incidents import lottery_id


class IncidentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = stamp()
        holder = self

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(holder.now, tz)

        self.patches = [patch("astrbot_plugin_persona_schedule.main.datetime", Frozen),
                        patch("astrbot_plugin_persona_schedule.interaction.datetime", Frozen),
                        patch("astrbot_plugin_persona_schedule.main.time.time", side_effect=lambda: self.now)]
        for item in self.patches:
            item.start()
        self.context = FakeContext()
        generate = self.context.llm_generate

        async def model(**kwargs):
            if "突发生活插曲" in kwargs["system_prompt"]:
                self.context.calls.append(kwargs)
                return SimpleNamespace(completion_text=json.dumps(VALUE, ensure_ascii=False))
            return await generate(**kwargs)

        self.context.llm_generate = model
        config = FakeConfig({"enabled": True, "whitelist_enabled": True, "whitelist": [UMO],
                             "incident_enabled": True, "incident_probability": 1.0, "adaptive_enabled": False,
                             "transition_notice_enabled": False, "auto_schedule_enabled": False,
                             "diary_enabled": False, "canvas_integration_enabled": True})
        with patch("astrbot_plugin_persona_schedule.main.StarTools.get_data_dir", return_value=self.temp.name):
            self.plugin = PersonaSchedule(self.context, config)
        self.plugin._incident_now = lambda: self.now
        self.plugin.incident_rng = FixedRandom()
        self.persona = await self.plugin._persona("traveler")
        await self.plugin.ensure(self.persona, UMO, DAY)
        self.context.calls.clear()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @property
    def base(self):
        return self.plugin.store.data["personas"][self.persona["id"]]["days"][DAY]

    @property
    def entry(self):
        return self.plugin.store.data["incidents"].get(lottery_id(self.persona, DAY))

    async def incidents(self):
        self.plugin._schedule_incidents(self.now)
        await asyncio.gather(*list(self.plugin.tasks))

    async def activate(self):
        await self.incidents()
        self.now = self.entry["trigger_at"]
        await self.incidents()
        self.assertEqual(self.entry["status"], "active")
        self.now = self.entry["started_at"]

    async def test_busy_incident_intercepts_then_one_aggregate_reply_after_end(self):
        original = copy.deepcopy(self.base)
        await self.activate()
        for text in ("等你有空一起聊", "想问问今天的旅行"):
            event = FakeEvent(text)
            await self.plugin.intercept_message(event)
            self.assertTrue(event.stopped)
        self.assertEqual(len(self.plugin.store.data["queues"][UMO]["messages"]), 2)
        self.assertEqual(self.context.sent, [])
        self.now = self.entry["ends_at"]
        await self.incidents()
        session = self.plugin.store.data["sessions"][UMO]
        await self.plugin._session_tick(UMO, session)
        await self.plugin._session_tick(UMO, session)
        self.assertEqual(len(self.context.sent), 1)
        self.assertNotIn(UMO, self.plugin.store.data["queues"])
        self.assertEqual(self.base, original)

    async def test_canvas_refuses_busy_incident_and_changes_context_after_end(self):
        free = await self.plugin.get_proactive_context(UMO)
        self.assertTrue(free["allowed"])
        await self.activate()
        busy = await self.plugin.get_proactive_context(UMO)
        self.assertFalse(busy["allowed"])
        self.assertEqual(busy["status"], "busy")
        self.assertNotEqual(busy["context_key"], free["context_key"])
        self.assertIn(VALUE["title"], busy["prompt"])
        self.now = self.entry["ends_at"]
        await self.incidents()
        ended = await self.plugin.get_proactive_context(UMO)
        self.assertTrue(ended["allowed"])
        self.assertNotEqual(ended["context_key"], busy["context_key"])

    async def test_diary_archives_occurred_event_and_not_future_pending_draw(self):
        await self.incidents()
        self.assertNotIn("incident_history", self.plugin._diary_source(self.persona, DAY)["schedule"])
        self.now = self.entry["trigger_at"]
        await self.incidents()
        self.now = self.entry["ends_at"]
        await self.incidents()
        self.now = stamp("00:10:00", day="2026-10-31")
        entry = await self.plugin.ensure_diary(self.persona, DAY, UMO)
        history = entry["source"]["schedule"]["incident_history"]
        self.assertEqual(history[0]["title"], VALUE["title"])
        payload = json.loads(self.context.calls[-1]["prompt"])
        self.assertEqual(payload["saved_day"]["incident_history"], history)
        self.assertEqual(self.context.sent, [])

    async def test_commands_and_excluded_sessions_still_bypass_incident(self):
        await self.activate()
        command = FakeEvent("/reset")
        await self.plugin.intercept_message(command)
        self.assertFalse(command.stopped)
        self.plugin.store.settings["whitelist"] = []
        excluded = FakeEvent("普通留言")
        await self.plugin.intercept_message(excluded)
        self.assertFalse(excluded.stopped)
        self.assertFalse(self.plugin.store.data["queues"])

    async def test_stale_contact_exception_cannot_override_incident(self):
        await self.activate()
        self.plugin.store.settings["adaptive_enabled"] = True
        plan = self.plugin._cached_schedule(self.persona, DAY)
        local = datetime.fromtimestamp(self.now, ZoneInfo("Asia/Shanghai"))
        env = await self.plugin.environment(UMO, "test")
        target = target_activity(plan, local)
        decision = {"action": "brief_exception", "activity_kind": "personal", "importance": "urgent",
                    "delay_minutes": 5, "reason": "眼前急事", "tone": "concerned", "notice": ""}
        adjustment = create_adjustment(decision, target, local, umo=UMO, cid=env["cid"],
            persona_key=self.persona["key"], schedule_revision=plan["revision"], max_delay_minutes=30)
        adjustment["source_fingerprint"] = digest(self.base)
        self.plugin.adjustments[UMO] = adjustment
        slot, _ = slot_at(plan, local)
        self.assertEqual(self.plugin._effective_slot(env, plan, slot, local)["status"], "busy")
        adjustment["source_fingerprint"] = digest(plan)
        self.assertEqual(self.plugin._effective_slot(env, plan, slot, local)["status"], "free")
        # Canvas still cannot send a photo during an urgent reply-only exception.
        self.assertFalse((await self.plugin.get_proactive_context(UMO))["allowed"])

    async def test_web_preview_simulates_busy_without_consuming_lottery(self):
        before = copy.deepcopy(self.plugin.store.data)
        result = await self.plugin.web_preview({"persona_id": self.persona["id"], "date": DAY,
            "time": "10:00", "umo": UMO, "message": "在吗", "simulate_incident": {
                "title": "临时跑腿", "status": "busy", "duration_minutes": 20}})
        self.assertEqual(result["base_activity"]["status"], "free")
        self.assertEqual(result["effective_activity"]["status"], "busy")
        self.assertTrue(result["would_queue"])
        self.assertTrue(result["simulated_incident"]["id"].startswith("preview:"))
        self.assertEqual(self.plugin.store.data, before)
        self.assertEqual(self.context.calls, [])

    async def test_manual_save_ends_event_and_keeps_elapsed_history(self):
        await self.activate()
        self.now += 10 * 60
        await self.plugin.web_save({"persona_id": self.persona["id"], "date": DAY, "kind": "day",
            "value": {"slots": slots(), "summary": "手工安排"}})
        self.assertEqual(self.entry["status"], "cancelled")
        plan = self.plugin._cached_schedule(self.persona, DAY)
        local = datetime.fromtimestamp(self.now, ZoneInfo("Asia/Shanghai"))
        self.assertEqual(slot_at(plan, local)[0]["status"], "free")
        self.assertEqual(plan["incident_history"][0]["ends_at"], self.now)

    async def test_slow_incident_uses_slow_queue_and_can_deliver_during_event(self):
        original_generate = self.context.llm_generate

        async def slow_model(**kwargs):
            if "突发生活插曲" in kwargs["system_prompt"]:
                self.context.calls.append(kwargs)
                return SimpleNamespace(completion_text=json.dumps({**VALUE, "status": "slow"}, ensure_ascii=False))
            return await original_generate(**kwargs)

        self.context.llm_generate = slow_model
        self.plugin.store.settings.update(slow_min_seconds=5, slow_max_seconds=5)
        await self.activate()
        event = FakeEvent("路上慢慢回我也没关系")
        await self.plugin.intercept_message(event)
        self.assertTrue(event.stopped)
        queue = self.plugin.store.data["queues"][UMO]
        self.assertAlmostEqual(queue["due"], self.now + 5)
        self.now += 6
        await self.plugin._session_tick(UMO, self.plugin.store.data["sessions"][UMO])
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.entry["status"], "active")


if __name__ == "__main__":
    unittest.main()
