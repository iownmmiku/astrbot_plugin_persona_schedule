"""Annual explicit rules are deterministic and independent of model calendars."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_schedule import FakeConfig, FakeContext, PersonaSchedule, UMO, slots
from astrbot_plugin_persona_schedule.core import Store
from astrbot_plugin_persona_schedule.recurrence import apply_recurrences, expand_rules, validate_rules


def rule(**changes):
    return {"id": "birthday", "title": "生日", "month": 10, "day": 29,
            "kind": "anniversary", "calendar": "gregorian", **changes}


class RuleTests(unittest.TestCase):
    def test_leap_day_skips_non_leap_year_by_default(self):
        value = rule(month=2, day=29)
        self.assertEqual(expand_rules([value], 2027), [])
        self.assertEqual(expand_rules([value], 2028)[0]["start"], "2028-02-29")

    def test_leap_day_can_explicitly_use_february_28(self):
        value = rule(month=2, day=29, leap_policy="feb28")
        self.assertEqual(expand_rules([value], 2027)[0]["start"], "2027-02-28")
        self.assertEqual(expand_rules([value], 2028)[0]["start"], "2028-02-29")

    def test_cross_year_span_is_visible_from_both_years(self):
        value = rule(month=12, day=30, duration_days=5)
        earlier, later = expand_rules([value], 2026), expand_rules([value], 2027)
        end = next(row for row in earlier if row["start"] == "2026-12-30")
        start = next(row for row in later if row["start"] == "2026-12-30")
        self.assertEqual(end, start)
        self.assertEqual(end["end"], "2027-01-03")

    def test_bounds_apply_to_occurrence_start_year_not_overlap_year(self):
        value = rule(month=12, day=31, duration_days=3, start_year=2026, end_year=2026)
        self.assertEqual(expand_rules([value], 2025), [])
        self.assertEqual(len(expand_rules([value], 2026)), 1)
        self.assertEqual(expand_rules([value], 2027)[0]["end"], "2027-01-02")
        self.assertEqual(expand_rules([value], 2028), [])

    def test_disabled_rule_is_kept_but_not_expanded(self):
        self.assertFalse(validate_rules([rule(enabled=False)])[0]["enabled"])
        self.assertEqual(expand_rules([rule(enabled=False)], 2026), [])

    def test_fictional_calendar_requires_explicit_mapping(self):
        with self.assertRaisesRegex(ValueError, "映射"):
            validate_rules([rule(calendar="fictional")])
        values = expand_rules([rule(calendar="fictional", mapping="银湾霜月祭映射公历 10 月 29 日")], 2026)
        self.assertIn("虚构历法的公历映射", values[0]["description"])
        self.assertIn("银湾霜月祭", values[0]["description"])

    def test_lunar_calendar_is_not_silently_treated_as_fixed_gregorian(self):
        with self.assertRaisesRegex(ValueError, "农历"):
            validate_rules([rule(calendar="lunar")])

    def test_invalid_rules_fail_without_mutating_input(self):
        cases = [rule(month=0), rule(month=4, day=31), rule(month=True),
                 rule(day="29"), rule(duration_days=False), rule(duration_days=367),
                 rule(leap_policy="mar1"), rule(start_year=2101), rule(start_year=2028, end_year=2027),
                 rule(enabled=1), rule(day_off="yes"), rule(title=" "), rule(id="../bad"), rule(id=0)]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                before = copy.deepcopy(candidate)
                with self.assertRaises(ValueError):
                    validate_rules([candidate])
                self.assertEqual(candidate, before)

    def test_rule_limit_and_duplicate_ids_are_checked(self):
        with self.assertRaises(ValueError):
            validate_rules([rule(), rule()])
        with self.assertRaises(ValueError):
            validate_rules([rule(id=str(i)) for i in range(101)])
        self.assertEqual(len(validate_rules([rule(id=str(i)) for i in range(100)])), 100)

    def test_new_rule_gets_stable_id_after_validation(self):
        value = rule()
        value.pop("id")
        saved = validate_rules([value])
        self.assertTrue(saved[0]["id"])
        self.assertEqual(expand_rules(saved, 2026), expand_rules(saved, 2026))

    def test_overlay_replaces_model_duplicate_but_keeps_explicit_annual_edits(self):
        occurrence = expand_rules([rule()], 2026)[0]
        model = {**occurrence, "id": "inferred", "origin": "generated"}
        model.pop("recurrence_id")
        manual = {**model, "id": "manual-note", "origin": "manual", "description": "另一个明确保存的安排"}
        source = {"year": 2026, "events": [model, manual], "revision": 2}
        before = copy.deepcopy(source)
        result = apply_recurrences(source, [rule()], 2026)
        self.assertEqual({r["id"] for r in result["events"]}, {occurrence["id"], "manual-note"})
        self.assertEqual(source, before)
        self.assertEqual(result["revision"], 2)

    def test_removing_rules_removes_only_derived_events(self):
        manual = {"id": "annual", "title": "一次活动", "start": "2026-02-01", "end": "2026-02-01", "origin": "manual"}
        source = {"events": [manual, *expand_rules([rule()], 2026)]}
        self.assertEqual(apply_recurrences(source, [], 2026)["events"], [manual])

    def test_empty_overlay_preserves_calendar_shape(self):
        source = {"year": 2026, "events": [], "notes": ["备注"]}
        self.assertEqual(apply_recurrences(source, [], 2026), source)


class Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = cls(2026, 10, 29, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


class RecurrenceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = patch("astrbot_plugin_persona_schedule.main.datetime", Frozen)
        self.clock.start()
        self.context = FakeContext()
        with patch("astrbot_plugin_persona_schedule.main.StarTools.get_data_dir", return_value=self.temp.name):
            self.plugin = PersonaSchedule(self.context, FakeConfig())
        self.persona = await self.plugin._persona("traveler")
        await self.plugin.ensure(self.persona, UMO, "2026-10-29")
        self.context.calls.clear()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.clock.stop()
        self.temp.cleanup()

    @property
    def record(self):
        return self.plugin.store.data["personas"]["traveler"]

    async def save(self, rules):
        return await self.plugin.web_save_recurrences({"persona_id": "traveler", "rules": rules})

    async def test_rules_repeat_next_year_and_reach_day_model_context(self):
        await self.save([rule(day_off=True)])
        self.assertNotIn("2026-10-29", self.record["days"])
        _, calendar, plan = await self.plugin.ensure(self.persona, UMO, "2027-10-29")
        event = next(r for r in calendar["events"] if r.get("recurrence_id") == "birthday")
        self.assertEqual(event["start"], "2027-10-29")
        self.assertTrue(event["day_off"])
        self.assertIn(event, plan["events"])
        payload = json.loads(self.context.calls[-1]["prompt"])
        self.assertIn(event, payload["today_events"])

    async def test_rebuild_calendar_and_profile_do_not_erase_rules(self):
        await self.save([rule()])
        rules = copy.deepcopy(self.record["recurrences"])
        for force in ("calendar", "profile"):
            with self.subTest(force=force):
                await self.plugin.ensure(self.persona, UMO, "2026-10-29", force=force)
                self.assertEqual(self.record["recurrences"], rules)
                self.assertEqual(len([r for r in self.record["calendars"]["2026"]["events"] if r.get("recurrence_id")]), 1)

    async def test_rule_change_invalidates_only_affected_generated_dates(self):
        await self.plugin.ensure(self.persona, UMO, "2026-10-30")
        unaffected = copy.deepcopy(self.record["days"]["2026-10-30"])
        await self.save([rule()])
        self.assertNotIn("2026-10-29", self.record["days"])
        self.assertEqual(self.record["days"]["2026-10-30"], unaffected)
        self.assertFalse(self.context.calls[-1].get("unexpected", False))

    async def test_manual_slots_are_preserved_and_event_metadata_is_refreshed(self):
        await self.plugin.web_save({"persona_id": "traveler", "kind": "day", "date": "2026-10-29",
                                    "value": {"summary": "人工安排", "slots": [{**r, "locked": True} for r in slots("busy", "不可挪动")]}})
        previous = copy.deepcopy(self.record["days"]["2026-10-29"])
        await self.save([rule()])
        plan = self.record["days"]["2026-10-29"]
        self.assertEqual(plan["slots"], previous["slots"])
        self.assertEqual(plan["summary"], previous["summary"])
        self.assertGreater(plan["revision"], previous["revision"])
        self.assertEqual(plan["events"][0]["recurrence_id"], "birthday")
        await self.save([])
        self.assertEqual(self.record["days"]["2026-10-29"]["slots"], previous["slots"])
        self.assertEqual(self.record["days"]["2026-10-29"]["events"], [])

    async def test_invalid_save_is_transactional(self):
        before = copy.deepcopy(self.plugin.store.data)
        with self.assertRaises(ValueError):
            await self.save([rule(month=13)])
        self.assertEqual(self.plugin.store.data, before)

    async def test_read_and_expand_do_not_create_or_mutate_records(self):
        self.plugin.store.data["personas"].clear()
        before = copy.deepcopy(self.plugin.store.data)
        data = await self.plugin.web_recurrences({"persona_id": "traveler", "year": "2027"})
        self.assertEqual(data["rules"], [])
        self.assertEqual(self.plugin.store.data, before)

    async def test_rules_are_durable_and_stale_persona_rules_are_not_exposed(self):
        await self.save([rule()])
        stored = Store(self.temp.name).data["personas"]["traveler"]["recurrences"]
        self.assertEqual(stored, self.record["recurrences"])
        self.context.persona_manager.item["system_prompt"] = "完全不同的人设"
        data = await self.plugin.web_recurrences({"persona_id": "traveler", "date": "2027-01-01"})
        self.assertEqual(data["rules"], [])

    async def test_annual_manual_save_keeps_rules_in_control_of_derived_events(self):
        await self.save([rule()])
        calendar = copy.deepcopy(self.record["calendars"]["2026"])
        await self.plugin.web_save({"persona_id": "traveler", "kind": "calendar", "date": "2026-10-29",
                                    "value": calendar["events"]})
        _, calendar, _ = await self.plugin.ensure(self.persona, UMO, "2026-10-29")
        self.assertEqual(len([r for r in calendar["events"] if r.get("recurrence_id") == "birthday"]), 1)

    async def test_rule_save_snapshot_can_restore_without_touching_diaries_or_queues(self):
        before = copy.deepcopy(self.record)
        self.plugin.store.enqueue(UMO, "conv-1", self.persona["key"], "测试用户", "待回复", 10, 20, "busy", "安排")
        self.plugin.store.data["diaries"]["traveler"] = {"2026-10-28": {"title": "留存的日记"}}
        queues, diaries = copy.deepcopy(self.plugin.store.data["queues"]), copy.deepcopy(self.plugin.store.data["diaries"])
        result = await self.save([rule()])
        await self.plugin.ensure(self.persona, UMO, "2026-10-29")
        await self.plugin.web_restore({"persona_id": "traveler", "id": result["snapshot_id"]})
        self.assertEqual(self.record.get("recurrences", []), [])
        self.assertEqual(self.record["days"]["2026-10-29"]["events"], before["days"]["2026-10-29"]["events"])
        self.assertEqual(self.plugin.store.data["queues"], queues)
        self.assertEqual(self.plugin.store.data["diaries"], diaries)

    async def test_snapshot_failure_leaves_rules_and_calendars_untouched(self):
        before = copy.deepcopy(self.plugin.store.data)
        with patch.object(self.plugin, "_archive_edit", side_effect=ValueError("快照过大")):
            with self.assertRaisesRegex(ValueError, "快照"):
                await self.save([rule()])
        self.assertEqual(self.plugin.store.data, before)

    async def test_rule_changes_clear_only_this_personas_contact_exceptions(self):
        self.plugin.adjustments.update({"this": {"persona_key": self.persona["key"]},
                                        "other": {"persona_key": "different-persona"}})
        await self.save([rule()])
        self.assertNotIn("this", self.plugin.adjustments)
        self.assertIn("other", self.plugin.adjustments)

    async def test_disk_failure_rolls_back_rules_history_and_contact_exceptions(self):
        self.plugin.adjustments["this"] = {"persona_key": self.persona["key"]}
        before = copy.deepcopy(self.plugin.store.data)
        with patch.object(self.plugin.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.save([rule()])
        self.assertEqual(self.plugin.store.data, before)

    async def test_saving_identical_rules_does_not_create_duplicate_history(self):
        await self.save([rule()])
        count = len(self.plugin.store.data["edit_history"]["traveler"])
        await self.save([rule()])
        self.assertEqual(len(self.plugin.store.data["edit_history"]["traveler"]), count)


if __name__ == "__main__":
    unittest.main()
