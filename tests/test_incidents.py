"""Deterministic clocks and draws exercise shared incidents without real chat."""
from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_schedule import Store, profile, slots
from astrbot_plugin_persona_schedule.core import digest, slot_at
from astrbot_plugin_persona_schedule.incidents import (
    IncidentMixin, _fitting_end, eligible_slots, lottery_id, probability, validate_incident,
)


DAY = "2026-10-30"
PERSONA = {"id": "traveler", "key": "traveler-key", "prompt": "幻想世界的旅人"}
VALUE = {"suitable": True, "title": "帮邻居搬箱子", "note": "邻居的货物送到了，临时帮忙搬进屋内。",
         "status": "busy", "duration_minutes": 30, "category": "help", "reason": "旅人目前正在休息，有空帮忙"}


def stamp(clock="10:00:00", day=DAY, zone="Asia/Shanghai"):
    return datetime.fromisoformat(day + "T" + clock).replace(tzinfo=ZoneInfo(zone)).timestamp()


class FixedRandom:
    def __init__(self, value=0.1, index=0):
        self.value, self.index, self.draws, self.choices = value, index, 0, 0

    def random(self):
        self.draws += 1
        return self.value

    def randrange(self, size):
        self.choices += 1
        return self.index % size


class Harness(IncidentMixin):
    def __init__(self, root):
        self.store = Store(root)
        self.store.settings.update(enabled=True, incident_enabled=True, incident_probability=0.2)
        self.persona = dict(PERSONA)
        self.now = stamp()
        self.persona_locks, self.tasks = {}, set()
        self.calls, self.scopes, self.failures = [], [], {}
        self.value = copy.deepcopy(VALUE)
        self.generator = SimpleNamespace(structured=self.generate, task_scope=self.scope)
        self.store.data["personas"] = {PERSONA["id"]: {"key": PERSONA["key"], "profile": profile(world="original"),
            "days": {DAY: {"date": DAY, "timezone": "Asia/Shanghai", "slots": slots(), "events": [],
                          "revision": 1, "summary": "旅人的一天"}}, "calendars": {}, "locked_days": {}}}
        self.init_incidents(self.now)
        self.incident_rng = FixedRandom()

    def _incident_now(self):
        return self.now

    async def _persona(self, pid):
        if pid != self.persona["id"]:
            raise ValueError("missing")
        return dict(self.persona)

    async def _require_current_persona(self, persona):
        if persona != self.persona:
            raise ValueError("人设已变化")

    def _require_edit_token(self, persona, payload):
        if payload.get("expected_token") != "current":
            raise ValueError("保存冲突")

    def _edit_token(self, persona):
        return digest(self.store.data.get("incidents", {}))

    @staticmethod
    def _year(day):
        return datetime.fromisoformat(day).year

    @staticmethod
    def _lock(mapping, key):
        return mapping.setdefault(key, asyncio.Lock())

    def _spawn(self, coroutine, name):
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def scope(self, purpose, **kwargs):
        self.scopes.append((purpose, kwargs))
        return nullcontext()

    async def generate(self, instruction, payload, validator, **kwargs):
        await kwargs["before_generate"]()
        self.calls.append((instruction, copy.deepcopy(payload), kwargs["purpose"]))
        return validator(copy.deepcopy(self.value))

    @property
    def base(self):
        return self.store.data["personas"][PERSONA["id"]]["days"][DAY]

    @property
    def entry(self):
        return self.store.data["incidents"].get(lottery_id(self.persona, DAY))

    async def tick(self, when=None):
        if when is not None:
            self.now = when
        self._schedule_incidents(self.now)
        await asyncio.gather(*list(self.tasks))

    async def activate(self):
        await self.tick()
        await self.tick(self.entry["trigger_at"])
        self.now = self.entry["started_at"]
        return self.entry


class IncidentPureTests(unittest.TestCase):
    def test_probability_rejects_non_numbers_booleans_and_nonfinite(self):
        self.assertEqual(probability(0), 0)
        self.assertEqual(probability(1), 1)
        self.assertEqual(probability(0.2), 0.2)
        for value in (-0.01, 1.01, True, "0.2", None, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                probability(value)

    def test_model_contract_rejects_invalid_values_and_duplicates(self):
        self.assertEqual(validate_incident(VALUE)["duration_minutes"], 30)
        for change in ({"suitable": "yes"}, {"status": "free"}, {"duration_minutes": True},
                       {"duration_minutes": 61}, {"category": "unknown"}, {"title": ""}, {"note": ""}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_incident({**VALUE, **change})
        with self.assertRaisesRegex(ValueError, "重复"):
            validate_incident(VALUE, [VALUE])
        self.assertEqual(validate_incident({"suitable": False, "reason": "不适合"}),
                         {"suitable": False, "reason": "不适合"})

    def test_locked_fixed_sleep_and_busy_rows_have_no_choices(self):
        for row in ({"locked": True}, {"title": "开会"}, {"title": "睡觉"}, {"status": "busy"}):
            schedule = {"date": DAY, "timezone": "Asia/Shanghai", "slots": [{**slots()[0], **row}]}
            with self.subTest(row=row):
                self.assertEqual(eligible_slots(schedule, stamp()), [])

    def test_choices_are_future_and_leave_room_within_local_midnight(self):
        schedule = {"date": DAY, "timezone": "Asia/Tokyo", "slots": slots()}
        now = stamp("23:42:59", zone="Asia/Tokyo")
        choices = eligible_slots(schedule, now)
        self.assertTrue(choices)
        for trigger, boundary, _ in choices:
            self.assertGreater(trigger, now)
            self.assertGreaterEqual(boundary - trigger, 16 * 60)
            self.assertEqual(datetime.fromtimestamp(boundary, ZoneInfo("Asia/Tokyo")).hour, 0)
        self.assertFalse(eligible_slots(schedule, stamp("23:44:00", zone="Asia/Tokyo")))

    def test_other_day_and_dst_imaginary_minutes_are_not_selected(self):
        schedule = {"date": DAY, "timezone": "Asia/Shanghai", "slots": slots()}
        self.assertFalse(eligible_slots(schedule, stamp(day="2026-10-31")))
        schedule.update(date="2026-03-08", timezone="America/New_York")
        choices = eligible_slots(schedule, stamp("01:59:00", day="2026-03-08", zone="America/New_York"))
        self.assertTrue(choices)
        self.assertNotIn(2, {datetime.fromtimestamp(row[0], ZoneInfo("America/New_York")).hour for row in choices})

    def test_duration_does_not_cross_repeated_or_missing_clock_hour(self):
        zone = ZoneInfo("America/New_York")
        for day in ("2026-03-08", "2026-11-01"):
            boundary = stamp("04:00:00", day=day, zone="America/New_York")
            end = _fitting_end(day, 100, 60, boundary, zone)
            self.assertEqual(datetime.fromtimestamp(end, zone).strftime("%H:%M"), "01:59")
            self.assertIsNone(_fitting_end(day, 115, 30, boundary, zone))


class IncidentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = Harness(self.temp.name)

    async def asyncTearDown(self):
        for task in list(self.plugin.tasks):
            task.cancel()
        await asyncio.gather(*list(self.plugin.tasks), return_exceptions=True)
        self.temp.cleanup()

    async def test_disabled_has_no_lottery_no_model_and_unchanged_plan(self):
        self.plugin.store.settings["incident_enabled"] = False
        before = copy.deepcopy(self.plugin.base)
        await self.plugin.tick()
        self.assertIsNone(self.plugin.entry)
        self.assertEqual(self.plugin.base, before)
        self.assertFalse(self.plugin.calls)

    async def test_zero_probability_draw_is_persisted_without_model(self):
        self.plugin.store.settings["incident_probability"] = 0
        await self.plugin.tick()
        self.assertEqual(self.plugin.entry["status"], "missed")
        self.assertEqual(self.plugin.incident_rng.draws, 0)
        self.assertFalse(self.plugin.calls)
        self.plugin.store.settings["incident_probability"] = 1
        await self.plugin.tick(stamp("11:00:00"))
        self.assertEqual(self.plugin.entry["probability"], 0)
        self.assertFalse(self.plugin.calls)

    async def test_probability_draw_once_and_current_setting_does_not_reroll(self):
        self.plugin.incident_rng = FixedRandom(0.3)
        await self.plugin.tick()
        for _ in range(5):
            await self.plugin.tick()
        self.assertEqual(self.plugin.incident_rng.draws, 1)
        self.assertEqual(self.plugin.entry["status"], "missed")
        self.plugin.store.settings["incident_probability"] = 1
        await self.plugin.tick()
        self.assertEqual(self.plugin.entry["status"], "missed")

    async def test_no_candidate_is_recorded_without_random_or_model(self):
        self.plugin.base["slots"] = slots("busy", "上课")
        await self.plugin.tick()
        self.assertEqual(self.plugin.entry["status"], "skipped")
        self.assertIn("没有", self.plugin.entry["reason"])
        self.assertEqual(self.plugin.incident_rng.draws, 0)

    async def test_hit_waits_until_trigger_and_does_not_leak_into_effective_plan(self):
        await self.plugin.tick()
        self.assertEqual(self.plugin.entry["status"], "pending")
        self.assertFalse(self.plugin.calls)
        effective = self.plugin.effective_schedule(self.plugin.persona, self.plugin.base)
        self.assertIs(effective, self.plugin.base)
        before = copy.deepcopy(self.plugin.store.data)
        await self.plugin.web_incidents({"persona_id": "traveler", "date": DAY})
        self.assertEqual(self.plugin.store.data, before)

    async def test_live_event_changes_shared_plan_and_preserves_source(self):
        original = copy.deepcopy(self.plugin.base)
        entry = await self.plugin.activate()
        result = self.plugin.effective_schedule(self.plugin.persona, self.plugin.base)
        self.assertEqual(self.plugin.base, original)
        local = datetime.fromtimestamp(self.plugin.now, ZoneInfo("Asia/Shanghai"))
        self.assertEqual(slot_at(result, local)[0]["status"], "busy")
        self.assertEqual(result["incident_history"][0]["id"], entry["id"])
        self.assertEqual(result, self.plugin.effective_schedule(self.plugin.persona, self.plugin.base))
        self.assertEqual(len(self.plugin.calls), 1)
        self.assertEqual(self.plugin.calls[0][2], "incident")
        self.assertEqual(self.plugin.scopes, [("incident", {"background": True})])
        self.assertNotIn("chat", self.plugin.calls[0][1])

    async def test_model_completion_never_backdates_current_minute(self):
        await self.plugin.tick()
        self.plugin.now = self.plugin.entry["trigger_at"] + 42
        await self.plugin.tick()
        start = self.plugin.entry["started_at"]
        self.assertGreater(start, self.plugin.now)
        self.assertIs(self.plugin.effective_schedule(self.plugin.persona, self.plugin.base), self.plugin.base)

    async def test_event_ends_at_boundaries_and_history_remains(self):
        entry = await self.plugin.activate()
        await self.plugin.tick(entry["ends_at"])
        self.assertEqual(self.plugin.entry["status"], "ended")
        effective = self.plugin.effective_schedule(self.plugin.persona, self.plugin.base)
        local = datetime.fromtimestamp(self.plugin.now, ZoneInfo("Asia/Shanghai"))
        self.assertEqual(slot_at(effective, local)[0]["status"], "free")
        self.assertEqual(len(effective["incident_history"]), 1)
        self.assertEqual(len(self.plugin.calls), 1)

    async def test_closing_switch_ends_now_keeps_history_and_does_not_reroll(self):
        entry = await self.plugin.activate()
        self.plugin.now = entry["started_at"] + 10 * 60 + 20
        self.plugin.store.settings["incident_enabled"] = False
        await self.plugin.tick()
        self.assertEqual(self.plugin.entry["actual_end"], self.plugin.now)
        effective = self.plugin.effective_schedule(self.plugin.persona, self.plugin.base)
        local = datetime.fromtimestamp(self.plugin.now, ZoneInfo("Asia/Shanghai"))
        self.assertEqual(slot_at(effective, local)[0]["status"], "free")
        self.assertEqual(effective["incident_history"][0]["ends_at"], self.plugin.now)
        self.plugin.store.settings["incident_enabled"] = True
        await self.plugin.tick()
        self.assertEqual(self.plugin.entry["status"], "cancelled")
        self.assertEqual(len(self.plugin.calls), 1)

    async def test_restart_resumes_live_event_and_skips_missed_pending(self):
        await self.plugin.tick()
        self.plugin.now = self.plugin.entry["trigger_at"] + 1
        self.plugin.init_incidents(self.plugin.now)
        self.assertEqual(self.plugin.entry["status"], "skipped")
        self.assertFalse(self.plugin.calls)
        self.plugin.store.data["incidents"].clear()
        self.plugin.incident_rng = FixedRandom()
        entry = await self.plugin.activate()
        self.plugin.now = entry["started_at"] + 60
        self.plugin.init_incidents(self.plugin.now)
        self.assertEqual(self.plugin.entry["status"], "active")

    async def test_interrupted_generation_does_not_rerun_after_restart(self):
        await self.plugin.tick()
        self.plugin.entry["status"] = "generating"
        self.plugin.init_incidents(self.plugin.now)
        await self.plugin.tick()
        self.assertEqual(self.plugin.entry["status"], "skipped")
        self.assertFalse(self.plugin.calls)

    async def test_backup_recovery_skips_new_draws_on_recovery_local_date(self):
        self.plugin.store.data["recovery_info"] = {"at": self.plugin.now, "requires_review": True}
        await self.plugin.tick()
        self.assertEqual(self.plugin.entry["status"], "skipped")
        self.assertIn("恢复", self.plugin.entry["reason"])
        self.assertEqual(self.plugin.incident_rng.draws, 0)

    async def test_reviewed_backup_still_skips_recovery_day_but_allows_next_day(self):
        # Explicitly re-enabling the plugin acknowledges recovery, but cannot
        # restore lottery opportunities consumed after the old backup snapshot.
        self.plugin.store.data["recovery_info"] = {"at": self.plugin.now, "requires_review": False}
        await self.plugin.tick()
        self.assertEqual(self.plugin.entry["status"], "skipped")
        self.assertIn("恢复", self.plugin.entry["reason"])
        self.assertEqual(self.plugin.incident_rng.draws, 0)
        tomorrow = "2026-10-31"
        self.plugin.store.data["personas"][PERSONA["id"]]["days"][tomorrow] = {
            **copy.deepcopy(self.plugin.base), "date": tomorrow}
        await self.plugin.tick(stamp(day=tomorrow))
        next_entry = self.plugin.store.data["incidents"][lottery_id(self.plugin.persona, tomorrow)]
        self.assertEqual(next_entry["status"], "pending")
        self.assertEqual(self.plugin.incident_rng.draws, 1)

    async def test_recovered_active_event_is_not_replayed(self):
        await self.plugin.activate()
        self.plugin.store.data["recovery_info"] = {"at": self.plugin.now, "requires_review": True}
        self.plugin.init_incidents(self.plugin.now)
        self.assertEqual(self.plugin.entry["status"], "cancelled")

    async def test_model_declines_or_fails_preserves_base_schedule(self):
        await self.plugin.tick()
        self.plugin.value = {"suitable": False, "reason": "没有合适的情境"}
        await self.plugin.tick(self.plugin.entry["trigger_at"])
        self.assertEqual(self.plugin.entry["status"], "skipped")
        self.assertIs(self.plugin.effective_schedule(self.plugin.persona, self.plugin.base), self.plugin.base)

    async def test_late_model_response_does_not_fit_and_is_skipped(self):
        await self.plugin.tick()
        generate = self.plugin.generate

        async def late(*args, **kwargs):
            result = await generate(*args, **kwargs)
            self.plugin.now = self.plugin.entry["window_end"] - 10 * 60
            return result

        self.plugin.generator.structured = late
        await self.plugin.tick(self.plugin.entry["trigger_at"])
        self.assertEqual(self.plugin.entry["status"], "skipped")
        self.assertNotIn("started_at", self.plugin.entry)

    async def test_config_or_source_change_during_model_wait_discards_result(self):
        for mode in ("disabled", "source", "persona"):
            with self.subTest(mode=mode):
                self.plugin = Harness(self.temp.name)
                self.plugin.store.data["incidents"].clear()
                await self.plugin.tick()
                generate = self.plugin.generate

                async def changed(*args, **kwargs):
                    value = await generate(*args, **kwargs)
                    if mode == "disabled":
                        self.plugin.store.settings["incident_enabled"] = False
                    elif mode == "source":
                        self.plugin.base["summary"] = "新安排"
                    else:
                        self.plugin.persona["key"] = "changed-key"
                    return value

                self.plugin.generator.structured = changed
                identifier = self.plugin.entry["id"]
                await self.plugin.tick(self.plugin.entry["trigger_at"])
                self.assertEqual(self.plugin.store.data["incidents"][identifier]["status"], "skipped")
                self.assertNotIn("started_at", self.plugin.store.data["incidents"][identifier])

    async def test_manual_edit_cancels_pending_without_model(self):
        await self.plugin.tick()
        trigger = self.plugin.entry["trigger_at"]
        self.plugin.base["revision"] += 1
        await self.plugin.tick(trigger)
        self.assertEqual(self.plugin.entry["status"], "cancelled")
        self.assertFalse(self.plugin.calls)

    async def test_failed_ledger_write_rolls_back_and_cancellation_can_be_atomic_with_edit(self):
        await self.plugin.activate()
        before = copy.deepcopy(self.plugin.store.data["incidents"])
        with patch.object(self.plugin.store, "save", side_effect=OSError("fixture disk full")):
            with self.assertRaises(OSError):
                self.plugin._cancel_persona_incidents(self.plugin.persona, "edit")
        self.assertEqual(self.plugin.store.data["incidents"], before)
        with patch.object(self.plugin.store, "save", side_effect=AssertionError("must be caller's transaction")):
            self.assertTrue(self.plugin._cancel_persona_incidents(self.plugin.persona, "edit", persist=False))
        self.assertEqual(self.plugin.entry["status"], "cancelled")

    async def test_date_scoped_cancellation_preserves_unrelated_pending_and_history(self):
        await self.plugin.activate()
        entries = self.plugin.store.data["incidents"]
        tomorrow = "2026-10-31"
        tomorrow_id = lottery_id(self.plugin.persona, tomorrow)
        future = {**copy.deepcopy(self.plugin.entry), "id": tomorrow_id, "date": tomorrow,
                  "status": "pending", "trigger_at": stamp(day=tomorrow)}
        future.pop("started_at")
        future.pop("ends_at")
        entries[tomorrow_id] = future
        before = copy.deepcopy(entries)
        self.assertFalse(self.plugin._cancel_persona_incidents(self.plugin.persona, "no affected date", affected_dates=[]))
        self.assertEqual(self.plugin.store.data["incidents"], before)
        self.plugin.now += 10 * 60
        self.assertTrue(self.plugin._cancel_persona_incidents(self.plugin.persona, "today edited", affected_dates={DAY}))
        self.assertEqual(self.plugin.entry["status"], "cancelled")
        self.assertEqual(self.plugin.entry["actual_end"], self.plugin.now)
        self.assertEqual(self.plugin.store.data["incidents"][tomorrow_id], future)
        self.assertEqual(self.plugin.effective_schedule(self.plugin.persona, self.plugin.base)["incident_history"][0]["ends_at"], self.plugin.now)

    async def test_diagnostics_are_read_only_and_end_requires_current_token(self):
        await self.plugin.activate()
        # Older/restored diagnostic rows may lack their creation timestamp;
        # sorting must not change whether an incident actually occurred.
        self.plugin.entry.pop("created_at")
        before = copy.deepcopy(self.plugin.store.data)
        status = await self.plugin.web_incidents({"persona_id": "traveler", "date": DAY})
        self.assertEqual(status["entry"], status["today"])
        self.assertEqual(status["history"], status["entries"])
        self.assertNotIn("persona_key", status["entry"])
        self.assertEqual(self.plugin.store.data, before)
        with self.assertRaisesRegex(ValueError, "冲突"):
            await self.plugin.web_incident_end({"persona_id": "traveler", "id": self.plugin.entry["id"]})
        await self.plugin.web_incident_end({"persona_id": "traveler", "id": self.plugin.entry["id"], "expected_token": "current"})
        self.assertEqual(self.plugin.entry["status"], "cancelled")

    async def test_simulation_is_read_only_and_does_not_consume_draw(self):
        before = copy.deepcopy(self.plugin.store.data)
        local = datetime.fromtimestamp(self.plugin.now, ZoneInfo("Asia/Shanghai"))
        simulated, event = self.plugin.preview_incident(self.plugin.persona, self.plugin.base, local,
            {"simulate_incident": {"title": "模拟帮忙", "status": "slow", "duration_minutes": 20}})
        self.assertEqual(slot_at(simulated, local)[0]["status"], "slow")
        self.assertTrue(event["id"].startswith("preview:"))
        self.assertEqual(self.plugin.store.data, before)
        self.assertEqual(self.plugin.incident_rng.draws, 0)
        self.assertFalse(self.plugin.calls)

    async def test_preview_cannot_insert_into_locked_or_short_slot(self):
        local = datetime.fromtimestamp(self.plugin.now, ZoneInfo("Asia/Shanghai"))
        self.plugin.base["slots"][0]["locked"] = True
        with self.assertRaises(ValueError):
            self.plugin.preview_incident(self.plugin.persona, self.plugin.base, local, {"simulate_incident": {}})
        self.plugin.base["slots"][0]["locked"] = False
        local = datetime.fromtimestamp(stamp("23:50:00"), ZoneInfo("Asia/Shanghai"))
        with self.assertRaises(ValueError):
            self.plugin.preview_incident(self.plugin.persona, self.plugin.base, local, {"simulate_incident": {}})

    async def test_old_persona_events_do_not_enter_new_identity_plan(self):
        await self.plugin.activate()
        changed = {**self.plugin.persona, "key": "new-persona"}
        self.assertIs(self.plugin.effective_schedule(changed, self.plugin.base), self.plugin.base)

    async def test_recent_events_are_context_only_and_exact_title_repetition_is_rejected(self):
        old = copy.deepcopy(VALUE)
        self.plugin.store.data["incidents"]["yesterday"] = {"id": "yesterday", "persona_id": "traveler",
            "persona_key": PERSONA["key"], "date": "2026-10-29", "timezone": "Asia/Shanghai", "status": "ended",
            "created_at": stamp(day="2026-10-29"), "started_at": stamp(day="2026-10-29"), "incident": old}
        await self.plugin.tick()
        await self.plugin.tick(self.plugin.entry["trigger_at"])
        self.assertEqual(self.plugin.entry["status"], "skipped")
        self.assertEqual(self.plugin.calls[0][1]["recent_incidents"][0]["title"], VALUE["title"])


if __name__ == "__main__":
    unittest.main()
