"""Cache previews must not discard requested plans or user-maintained dates."""
from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_schedule import FakeConfig, FakeContext, PersonaSchedule, Store, UMO, slots


class Frozen(datetime):
    instant = datetime(2026, 10, 28, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)


class CacheRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = patch("astrbot_plugin_persona_schedule.main.datetime", Frozen)
        self.clock.start()
        self.context = FakeContext()
        with patch("astrbot_plugin_persona_schedule.main.StarTools.get_data_dir", return_value=self.temp.name):
            self.plugin = PersonaSchedule(self.context, FakeConfig())
        self.persona = await self.plugin._persona("traveler")
        self.today = Frozen.now(ZoneInfo("Asia/Shanghai")).date()
        self.day = self.today.isoformat()
        await self.plugin.ensure(self.persona, UMO, self.day)
        self.context.calls.clear()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.clock.stop()
        self.temp.cleanup()

    @property
    def record(self):
        return self.plugin.store.data["personas"][self.persona["id"]]

    def seed_generated_days(self, *, future=False):
        sample = copy.deepcopy(self.record["days"][self.day])
        self.record["days"] = {
            (self.today + timedelta(days=i if future else -i)).isoformat(): {
                **copy.deepcopy(sample),
                "date": (self.today + timedelta(days=i if future else -i)).isoformat(),
            }
            for i in range(45)
        }

    async def save_calendar(self, year, events):
        await self.plugin.web_save({"persona_id": self.persona["id"], "kind": "calendar",
                                    "date": f"{year}-01-01", "value": events})

    @staticmethod
    def event(year, **updates):
        return {"id": "anniversary", "title": "手动纪念日", "start": f"{year}-05-01",
                "end": f"{year}-05-01", "kind": "anniversary", "day_off": False,
                "description": "人工维护", "source_urls": [], **updates}

    async def test_full_cache_returns_newly_requested_older_day(self):
        self.seed_generated_days()
        requested = (self.today - timedelta(days=100)).isoformat()
        _, _, result = await self.plugin.ensure(self.persona, UMO, requested)
        self.assertEqual(result["date"], requested)
        self.assertIn(requested, self.record["days"])
        self.assertEqual(len(self.record["days"]), 45)
        self.assertEqual(len(self.context.calls), 1)
        await self.plugin.ensure(self.persona, UMO, requested)
        self.assertEqual(len(self.context.calls), 1)

    async def test_1970_preview_does_not_evict_today_when_future_cache_is_full(self):
        self.seed_generated_days(future=True)
        today_snapshot = copy.deepcopy(self.record["days"][self.day])
        _, _, result = await self.plugin.ensure(self.persona, UMO, "1970-01-01")
        self.assertEqual(result["date"], "1970-01-01")
        self.assertIn("1970-01-01", self.record["days"])
        self.assertEqual(self.record["days"][self.day], today_snapshot)
        calls = len(self.context.calls)
        await self.plugin.ensure(self.persona, UMO, self.day)
        self.assertEqual(len(self.context.calls), calls)
        self.assertEqual(len(self.record["days"]), 45)

    async def test_manual_days_are_durable_outside_generated_cache_limit(self):
        self.seed_generated_days(future=True)
        manual_dates = ["2020-01-01", "2020-01-02", "2020-01-03"]
        for day in manual_dates:
            await self.plugin.web_save({"persona_id": self.persona["id"], "kind": "day", "date": day,
                                        "value": {"summary": "手工安排", "slots": slots("busy", "特别安排")}})
        snapshots = {day: copy.deepcopy(self.record["days"][day]) for day in manual_dates}
        await self.plugin.ensure(self.persona, UMO, "1970-01-01")
        generated = [value for value in self.record["days"].values() if value.get("origin") != "manual"]
        self.assertEqual(len(generated), 45)
        self.assertEqual(len(self.record["days"]), 48)
        self.assertEqual({day: self.record["days"][day] for day in manual_dates}, snapshots)
        self.assertEqual({day: Store(self.temp.name).data["personas"][self.persona["id"]]["days"][day]
                          for day in manual_dates}, snapshots)

    async def test_generated_days_remain_bounded_across_many_past_requests(self):
        for offset in range(1, 65):
            requested = (self.today - timedelta(days=offset)).isoformat()
            _, _, result = await self.plugin.ensure(self.persona, UMO, requested)
            self.assertEqual(result["date"], requested)
            self.assertIn(self.day, self.record["days"])
            self.assertLessEqual(len(self.record["days"]), 45)

    async def test_far_year_preview_preserves_current_year_manual_calendar(self):
        await self.save_calendar(2026, [self.event(2026)])
        snapshot = copy.deepcopy(self.record["calendars"]["2026"])
        await self.plugin.ensure(self.persona, UMO, "2030-01-01")
        self.assertEqual(self.record["calendars"]["2026"], snapshot)

    async def test_manual_calendar_add_edit_delete_and_first_empty_survive_far_preview(self):
        baseline = copy.deepcopy(self.record["calendars"])
        for operation in ("add", "edit", "delete", "first_empty"):
            with self.subTest(operation=operation):
                self.record["calendars"] = copy.deepcopy(baseline)
                if operation in {"edit", "delete"}:
                    self.record["calendars"]["2020"] = {"year": 2020, "revision": 1,
                        "events": [self.event(2020, title="原来的活动", origin="generated")]}
                rows = [] if operation in {"delete", "first_empty"} else [self.event(2020)]
                await self.save_calendar(2020, rows)
                snapshot = copy.deepcopy(self.record["calendars"]["2020"])
                self.assertEqual(snapshot["origin"], "manual")
                if operation == "first_empty":
                    self.assertEqual(snapshot["revision"], 1)
                await self.plugin.ensure(self.persona, UMO, "2030-01-01", force="day")
                self.assertEqual(self.record["calendars"]["2020"], snapshot)
                persisted = Store(self.temp.name).data["personas"][self.persona["id"]]["calendars"]
                self.assertEqual(persisted["2020"], snapshot)

    async def test_legacy_revised_empty_calendar_is_preserved_without_origin(self):
        snapshot = {"year": 2020, "revision": 2, "events": []}
        self.record["calendars"]["2020"] = copy.deepcopy(snapshot)
        await self.plugin.ensure(self.persona, UMO, "2030-01-01")
        self.assertEqual(self.record["calendars"]["2020"], snapshot)

    async def test_legacy_manual_event_preserves_calendar_even_at_revision_one(self):
        snapshot = {"year": 2020, "revision": 1, "events": [self.event(2020, origin="manual")]}
        self.record["calendars"]["2020"] = copy.deepcopy(snapshot)
        await self.plugin.ensure(self.persona, UMO, "2030-01-01")
        self.assertEqual(self.record["calendars"]["2020"], snapshot)

    async def test_generated_calendars_protect_local_year_neighbors_and_request_only(self):
        for year in range(2020, 2030):
            self.record["calendars"][str(year)] = {"year": year, "revision": 1, "events": []}
        await self.plugin.ensure(self.persona, UMO, "2040-01-01")
        self.assertEqual(set(self.record["calendars"]), {"2025", "2026", "2027", "2040"})
        await self.plugin.ensure(self.persona, UMO, "2050-01-01")
        self.assertEqual(set(self.record["calendars"]), {"2025", "2026", "2027", "2050"})

    async def test_calendar_protection_uses_character_local_year_at_new_year(self):
        class NearNewYear(Frozen):
            instant = datetime(2026, 12, 31, 16, 30, tzinfo=ZoneInfo("UTC"))
        self.record["profile"]["timezone"] = "Asia/Tokyo"
        for year in range(2025, 2030):
            self.record["calendars"][str(year)] = {"year": year, "revision": 1, "events": []}
        with patch("astrbot_plugin_persona_schedule.main.datetime", NearNewYear):
            await self.plugin.ensure(self.persona, UMO, "2040-01-01")
        self.assertEqual(set(self.record["calendars"]), {"2026", "2027", "2028", "2040"})

    async def test_explicit_calendar_rebuild_can_replace_manual_year(self):
        await self.save_calendar(2020, [self.event(2020)])
        self.assertEqual(self.record["calendars"]["2020"]["origin"], "manual")
        await self.plugin.ensure(self.persona, UMO, "2020-01-01", force="calendar")
        calendar = self.record["calendars"]["2020"]
        self.assertNotEqual(calendar.get("origin"), "manual")
        self.assertEqual(calendar["events"], [])


if __name__ == "__main__":
    unittest.main()
