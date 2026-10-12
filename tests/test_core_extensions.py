"""Regression tests for message expiry, locked schedules and model routing."""
from __future__ import annotations

import copy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_schedule as fixtures  # Reuse its AstrBot API doubles and dependency setup.

from astrbot_plugin_persona_schedule.core import (
    DEFAULTS, Store, merge_locked_slots, slow_bounds, validate_settings,
    validate_slots, weekly_for_day,
)
from astrbot_plugin_persona_schedule.generator import Generator, is_chat_provider


def slot(start="00:00", end="24:00", title="自由活动", status="free", **extra):
    return {"start": start, "end": end, "title": title, "status": status, "note": "", **extra}


class SlotExtensionTests(unittest.TestCase):
    def test_legacy_slots_have_unlocked_default(self):
        self.assertIs(validate_slots([slot()])[0]["locked"], False)

    def test_slot_delay_requires_valid_pair(self):
        for extra in ({"slow_min_seconds": 5}, {"slow_max_seconds": 5},
                      {"slow_min_seconds": 4, "slow_max_seconds": 20},
                      {"slow_min_seconds": 5, "slow_max_seconds": 7201},
                      {"slow_min_seconds": True, "slow_max_seconds": 20},
                      {"slow_min_seconds": 30, "slow_max_seconds": 20},
                      {"locked": "true"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_slots([slot(**extra)])
        row = validate_slots([slot(slow_min_seconds=7200, slow_max_seconds=7200)])[0]
        self.assertEqual(slow_bounds(row, DEFAULTS), (7200, 7200))
        self.assertEqual(slow_bounds(slot(), DEFAULTS), (60, 180))

    def test_weekly_day_keeps_lock_and_delay(self):
        row = slot(days=list(range(1, 8)), locked=True,
                   slow_min_seconds=25, slow_max_seconds=35)
        weekly = validate_slots([row], weekly=True)
        daily = weekly_for_day({"weekly": weekly}, "2026-10-12")
        self.assertTrue(daily[0]["locked"])
        self.assertEqual(slow_bounds(daily[0], DEFAULTS), (25, 35))

    def test_daily_lock_crosses_generated_boundaries_without_moving(self):
        generated = [slot("00:00", "08:00", "睡觉", "busy"),
                     slot("08:00", "12:00", "工作", "busy"),
                     slot("12:00", "24:00", "休息")]
        previous = [slot("07:30", "09:00", "固定通勤", "slow", locked=True,
                         slow_min_seconds=120, slow_max_seconds=240)]
        originals = copy.deepcopy((generated, previous))
        merged = merge_locked_slots(generated, previous)
        self.assertEqual([(row["start"], row["end"]) for row in merged],
                         [("00:00", "07:30"), ("07:30", "09:00"),
                          ("09:00", "12:00"), ("12:00", "24:00")])
        self.assertEqual(merged[1], validate_slots(
            [slot("00:00", "07:30"), previous[0], slot("09:00", "24:00")])[1])
        self.assertEqual((generated, previous), originals)

    def test_full_day_lock_and_unlocked_previous(self):
        fixed = slot(title="不可变日程", locked=True)
        self.assertEqual(merge_locked_slots([slot(title="新安排")], [fixed]),
                         validate_slots([fixed]))
        self.assertEqual(merge_locked_slots([slot(title="新安排")], [slot(title="旧安排")]),
                         validate_slots([slot(title="新安排")]))

    def test_multiple_locks_preserve_delay_on_clipped_generated_rows(self):
        generated = [slot(status="slow", slow_min_seconds=50, slow_max_seconds=70)]
        locks = [slot("06:00", "07:00", "晨练", locked=True),
                 slot("18:00", "19:00", "晚餐", locked=True)]
        merged = merge_locked_slots(generated, locks)
        self.assertEqual(len(merged), 5)
        self.assertTrue(all(slow_bounds(row, DEFAULTS) == (50, 70)
                            for row in merged if not row["locked"]))
        self.assertEqual(merge_locked_slots(merged, locks), merged)

    def test_overlapping_locks_are_rejected(self):
        locks = [slot("08:00", "10:00", locked=True),
                 slot("09:00", "11:00", locked=True)]
        with self.assertRaisesRegex(ValueError, "锁定时段"):
            merge_locked_slots([slot()], locks)
        with self.assertRaises(ValueError):
            merge_locked_slots([slot()], False)

    def test_weekly_locks_only_affect_assigned_days(self):
        generated = [slot(days=list(range(1, 8)))]
        previous = [slot("09:00", "10:00", "固定课程", "busy", days=[1, 3], locked=True,
                         slow_min_seconds=80, slow_max_seconds=90)]
        merged = merge_locked_slots(generated, previous, weekly=True)
        fixed = next(row for row in merged if row["locked"])
        self.assertEqual(fixed["days"], [1, 3])
        self.assertEqual((fixed["start"], fixed["end"]), ("09:00", "10:00"))
        self.assertEqual(slow_bounds(fixed, DEFAULTS), (80, 90))
        monday = weekly_for_day({"weekly": merged}, "2026-10-12")
        tuesday = weekly_for_day({"weekly": merged}, "2026-10-13")
        self.assertEqual(len(monday), 3)
        self.assertEqual(len(tuesday), 1)
        self.assertFalse(tuesday[0]["locked"])

    def test_locks_at_same_time_on_different_weekdays_do_not_overlap(self):
        locks = [slot("08:00", "10:00", "周一课", days=[1], locked=True),
                 slot("09:00", "11:00", "周二课", days=[2], locked=True)]
        merged = merge_locked_slots([slot(days=list(range(1, 8)))], locks, weekly=True)
        self.assertEqual(len([row for row in merged if row["locked"]]), 2)

    def test_merged_schedule_limit_is_validated(self):
        def clock(value):
            return f"{value // 60:02d}:{value % 60:02d}"
        rows = [slot(clock(i * 12), clock((i + 1) * 12), str(i)) for i in range(120)]
        with self.assertRaisesRegex(ValueError, "120"):
            merge_locked_slots(rows, [slot("00:01", "00:02", locked=True)])


class MessageExpiryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.settings["queue_expire_hours"] = 1
        self.now = 100_000

    def tearDown(self):
        self.temp.cleanup()

    def enqueue(self, at, text="留言", umo=fixtures.UMO):
        return self.store.enqueue(umo, "conv", "persona", "用户", text, at, self.now + 100, "busy")

    def test_old_message_expires_without_deleting_recent_arrival(self):
        queue = self.enqueue(self.now - 4000, "旧留言")
        self.enqueue(self.now - 10, "刚发的留言")
        self.assertEqual(self.store.prune_expired(self.now), 1)
        self.assertEqual([row["text"] for row in queue["messages"]], ["刚发的留言"])
        self.assertEqual(queue["created_at"], self.now - 10)
        self.assertEqual(queue["expired"], 1)
        restored = Store(self.temp.name)
        self.assertEqual(restored.data["queues"][fixtures.UMO]["expired"], 1)
        self.assertEqual(restored.data["expired_messages"], 1)

    def test_expiry_counts_accumulate_without_double_counting(self):
        queue = self.enqueue(self.now - 4000)
        self.enqueue(self.now - 5)
        self.assertEqual(self.store.prune_expired(self.now), 1)
        self.assertEqual(self.store.prune_expired(self.now), 0)
        self.enqueue(self.now - 5000)
        self.assertEqual(self.store.prune_expired(self.now), 1)
        self.assertEqual(queue["expired"], 2)
        self.assertEqual(self.store.data["expired_messages"], 2)

    def test_fully_expired_queue_deleted_and_global_counter_survives(self):
        self.enqueue(self.now - 3600)
        self.assertEqual(self.store.prune_expired(self.now), 1)
        self.assertNotIn(fixtures.UMO, self.store.data["queues"])
        self.assertEqual(Store(self.temp.name).data["expired_messages"], 1)

    def test_sending_queue_untouched_until_send_finishes(self):
        queue = self.enqueue(self.now - 4000)
        queue["delivery"] = "sending"
        self.assertEqual(self.store.prune_expired(self.now), 0)
        self.assertEqual(len(queue["messages"]), 1)
        queue["delivery"] = "uncertain"
        self.assertEqual(self.store.prune_expired(self.now), 1)

    def test_optional_origin_only_cleans_selected_queue(self):
        self.enqueue(self.now - 4000)
        self.enqueue(self.now - 4000, umo="other:FriendMessage:1")
        self.assertEqual(self.store.prune_expired(self.now, fixtures.UMO), 1)
        self.assertIn("other:FriendMessage:1", self.store.data["queues"])

    def test_finish_batch_restarts_age_from_remaining_message(self):
        queue = self.enqueue(self.now - 3599, "正在生成")
        ids = {row["id"] for row in queue["messages"]}
        self.enqueue(self.now - 2, "生成期间的新消息")
        self.store.finish_batch(fixtures.UMO, ids)
        self.assertEqual(queue["created_at"], self.now - 2)
        self.assertEqual(self.store.prune_expired(self.now + 5), 0)

    def test_bounded_queue_age_tracks_oldest_retained_message(self):
        self.store.settings["queue_limit"] = 5
        for i in range(7):
            queue = self.enqueue(self.now - 10 + i)
        self.assertEqual(queue["dropped"], 2)
        self.assertEqual(queue["created_at"], self.now - 8)


class ModelRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.context = fixtures.FakeContext()
        self.context.get_provider_by_id = lambda selected: types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(id=selected, type="chat_completion")) if selected in {"schedule", "reply", "legacy"} else None
        self.generator = Generator(self.context, self.store)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_independent_schedule_and_reply_models(self):
        self.store.settings.update(schedule_provider_id="schedule", reply_provider_id="reply", provider_id="legacy")
        await self.generator.generate("测试", {}, fixtures.UMO)
        await self.generator.generate("测试", {}, fixtures.UMO, purpose="reply")
        self.assertEqual([call["chat_provider_id"] for call in self.context.calls], ["schedule", "reply"])

    async def test_legacy_model_then_current_conversation_fallback(self):
        self.store.settings["provider_id"] = "legacy"
        for purpose in ("schedule", "reply"):
            self.assertEqual(await self.generator.provider_id(fixtures.UMO, purpose), "legacy")
        self.store.settings["provider_id"] = ""
        self.assertEqual(await self.generator.provider_id(fixtures.UMO), "fake-provider")
        self.context.get_current_chat_provider_id = AsyncMock(return_value=None)
        self.assertEqual(await self.generator.provider_id(fixtures.UMO), "fake-provider")

    async def test_reply_method_routes_to_reply_model(self):
        self.store.settings.update(schedule_provider_id="schedule", reply_provider_id="reply")
        await self.generator.reply({"id": "traveler", "prompt": "旅人"}, [],
                                   {"summary": "休息"}, slot(), fixtures.UMO, [])
        self.assertEqual(self.context.calls[-1]["chat_provider_id"], "reply")

    async def test_unavailable_explicit_model_rejected_without_secret_details(self):
        self.store.settings["schedule_provider_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "所选聊天模型不可用"):
            await self.generator.provider_id()
        self.context.get_provider_by_id = AsyncMock(side_effect=RuntimeError("key=secret-provider-config"))
        with self.assertRaises(ValueError) as caught:
            await self.generator.provider_id()
        self.assertNotIn("secret-provider-config", str(caught.exception))

    async def test_provider_listing_used_when_direct_lookup_missing(self):
        del self.context.get_provider_by_id
        self.context.get_all_providers = AsyncMock(return_value=[
            types.SimpleNamespace(meta=AsyncMock(side_effect=RuntimeError("unrelated adapter"))),
            types.SimpleNamespace(meta=AsyncMock(return_value=types.SimpleNamespace(
                id="schedule", type="chat_completion")))])
        self.store.settings["schedule_provider_id"] = "schedule"
        self.assertEqual(await self.generator.provider_id(), "schedule")
        self.store.settings["schedule_provider_id"] = "missing"
        with self.assertRaises(ValueError):
            await self.generator.provider_id()

    async def test_explicit_non_chat_model_is_rejected(self):
        self.store.settings["reply_provider_id"] = "embedding"
        self.context.get_provider_by_id = AsyncMock(return_value=types.SimpleNamespace(
            meta=AsyncMock(return_value=types.SimpleNamespace(id="embedding", type="text_embedding")),
            text_chat=AsyncMock()))
        with self.assertRaisesRegex(ValueError, "不支持向量"):
            await self.generator.provider_id(purpose="reply")
        self.assertEqual(self.context.calls, [])

    async def test_chat_capability_and_enum_metadata_are_supported(self):
        from enum import Enum
        class ProviderType(Enum):
            CHAT_COMPLETION = "chat_completion"
            TEXT_TO_SPEECH = "text_to_speech"
        for meta in ({"type": ProviderType.CHAT_COMPLETION}, {"type": "openai_chat_completion"}):
            with self.subTest(meta=meta):
                self.assertTrue(await is_chat_provider(types.SimpleNamespace(meta=lambda: meta)))
        self.assertTrue(await is_chat_provider(types.SimpleNamespace(text_chat=AsyncMock())))
        self.assertFalse(await is_chat_provider(types.SimpleNamespace(
            meta=lambda: {"type": ProviderType.TEXT_TO_SPEECH})))
        self.assertFalse(await is_chat_provider(object()))

    async def test_new_settings_preserve_legacy_and_validate_strings(self):
        settings = validate_settings({"provider_id": "legacy", "schedule_provider_id": "schedule",
                                      "reply_provider_id": "reply"}, DEFAULTS)
        self.assertEqual(settings["provider_id"], "legacy")
        self.assertEqual(settings["reply_provider_id"], "reply")
        with self.assertRaises(ValueError):
            validate_settings({"schedule_provider_id": 1}, DEFAULTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
