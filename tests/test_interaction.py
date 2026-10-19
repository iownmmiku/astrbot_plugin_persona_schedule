"""Message-flow regressions for finite contact windows and advance goodbyes.

Models and platform sends are explicit doubles. The saved day remains the
source of truth; a contact exception belongs to one conversation only.
"""
from __future__ import annotations

import copy
import json
import tempfile
import types
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from test_schedule import FakeConfig, FakeContext, FakeEvent, PersonaSchedule, UMO
from astrbot_plugin_persona_schedule.adaptive import validate_decision


DAY = "2026-10-14"
TZ = ZoneInfo("Asia/Shanghai")


class Frozen(datetime):
    current = datetime(2026, 10, 14, 21, 49, tzinfo=TZ)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz is not None else cls.current.replace(tzinfo=None)


class InteractionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock("21:49")
        self.temp = tempfile.TemporaryDirectory()
        self.patchers = [
            patch("astrbot_plugin_persona_schedule.main.datetime", Frozen),
            patch("astrbot_plugin_persona_schedule.interaction.datetime", Frozen),
            patch("time.time", side_effect=lambda: Frozen.current.timestamp()),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.ctx = FakeContext()
        self.config = FakeConfig({"enabled": True, "all_chats_enabled": True,
            "adaptive_enabled": True, "transition_notice_enabled": True,
            "auto_schedule_enabled": False, "diary_enabled": False})
        with patch("astrbot_plugin_persona_schedule.main.StarTools.get_data_dir", return_value=self.temp.name):
            self.plugin = PersonaSchedule(self.ctx, self.config)
        self.persona = await self.plugin._persona("traveler")
        await self.plugin.ensure(self.persona, UMO, DAY)
        self.set_plan()
        self.decision = {"action": "keep", "activity_kind": "sleep", "importance": "normal",
            "delay_minutes": 0, "reason": "保持原本安排", "tone": "plain", "notice": "我一会儿要休息了。"}
        self.plugin.generator.adaptive = AsyncMock(side_effect=self.decide)
        self.plugin.generator.transition_notice = AsyncMock(return_value="我一会儿该休息了，晚点再聊。")
        self.plugin.generator.reply = AsyncMock(return_value="刚才的留言我看到了，我们接着聊。")
        self.ctx.calls.clear()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    @staticmethod
    def clock(clock, day=DAY):
        Frozen.current = datetime.fromisoformat(day + "T" + clock).replace(tzinfo=TZ)

    @property
    def record(self):
        return self.plugin.store.data["personas"]["traveler"]

    @property
    def plan(self):
        return self.record["days"][DAY]

    def set_plan(self, title="睡觉", *, locked=False):
        self.plan["slots"] = [
            {"start": "00:00", "end": "22:00", "title": "自由聊天", "status": "free", "note": "", "locked": False},
            {"start": "22:00", "end": "24:00", "title": title, "status": "busy", "note": "", "locked": locked},
        ]

    async def decide(self, persona, profile, schedule, target, message, history, umo, **kwargs):
        return validate_decision(copy.deepcopy(self.decision), target,
            self.plugin.store.settings["adaptive_max_delay_minutes"],
            active_conversation=kwargs.get("active_conversation", False))

    async def message(self, text="我们再聊一会儿吧", umo=UMO, *, inject=False):
        event = FakeEvent(text)
        event.unified_msg_origin = umo
        request = types.SimpleNamespace(system_prompt="角色人设", conversation=None)
        await self.plugin.intercept_message(event)
        if inject:
            await self.plugin.inject_schedule(event, request)
        return event, request

    async def activate(self):
        self.clock("21:49")
        event, _ = await self.message("今天一路顺利吗？")
        self.assertFalse(event.stopped)
        self.clock("21:55")
        return await self.plugin.environment(UMO, "test")

    async def session_tick(self):
        return await self.plugin._session_tick(UMO, self.plugin.store.data["sessions"][UMO])

    def notice_status(self):
        return next(iter(self.plugin.dialogue[UMO]["notices"].values()))["status"]

    def defer(self, minutes=20, *, reason="还在持续聊天，短暂延后休息"):
        self.decision.update(action="defer", activity_kind="sleep", delay_minutes=minutes,
            reason=reason, importance="normal")

    async def test_empty_scope_does_not_intercept_any_chat(self):
        self.plugin.store.settings.update(all_chats_enabled=False, whitelist_enabled=False, whitelist=[])
        self.clock("22:01")
        event, _ = await self.message()
        self.assertFalse(event.stopped)
        self.assertFalse(self.plugin.store.data["queues"])
        self.assertFalse(self.plugin.store.data["sessions"])
        self.plugin.generator.adaptive.assert_not_awaited()

    async def test_explicit_empty_whitelist_takes_precedence_over_all_chats(self):
        self.plugin.store.settings.update(all_chats_enabled=True, whitelist_enabled=True, whitelist=[])
        self.clock("22:01")
        event, _ = await self.message()
        self.assertFalse(event.stopped)
        self.assertFalse(self.plugin.store.data["queues"])

    async def test_only_listed_session_is_intercepted(self):
        self.plugin.store.settings.update(all_chats_enabled=False, whitelist_enabled=True, whitelist=[UMO], adaptive_enabled=False)
        self.clock("22:01")
        included, _ = await self.message()
        excluded, _ = await self.message(umo="test:FriendMessage:other")
        self.assertTrue(included.stopped)
        self.assertFalse(excluded.stopped)
        self.assertEqual(set(self.plugin.store.data["queues"]), {UMO})

    async def test_explicit_all_chats_allows_busy_interception(self):
        self.plugin.store.settings["adaptive_enabled"] = False
        self.clock("22:01")
        event, _ = await self.message()
        self.assertTrue(event.stopped)
        self.assertIn(UMO, self.plugin.store.data["queues"])

    async def test_reset_and_already_replied_events_are_untouched(self):
        self.clock("22:01")
        for kind in ("reset", "sent", "result"):
            event = FakeEvent("/reset" if kind == "reset" else "还有件事")
            if kind == "sent":
                event._has_send_oper = True
            if kind == "result":
                event.get_result = lambda: object()
            await self.plugin.intercept_message(event)
            await self.plugin.inject_schedule(event, types.SimpleNamespace(system_prompt="原文", conversation=None))
            self.assertFalse(event.stopped, kind)
        self.assertFalse(self.plugin.store.data["queues"])
        self.plugin.generator.adaptive.assert_not_awaited()

    async def test_continuing_chat_defers_sleep_with_fatigue_without_editing_plan(self):
        before = copy.deepcopy(self.plan)
        await self.activate()
        self.defer()
        await self.message("这个故事还没讲完呢")
        until = self.plugin.adjustments[UMO]["until"]
        self.clock("22:01")
        event, request = await self.message("然后发生什么了？", inject=True)
        self.assertFalse(event.stopped)
        self.assertIn("倦意", request.extra_user_content_parts[-1].text)
        self.assertIn('"tone": "tired"', request.extra_user_content_parts[-1].text)
        self.assertEqual(self.plugin.adjustments[UMO]["until"], until)
        self.assertEqual(self.plan, before)
        self.assertEqual(self.plugin.generator.adaptive.await_count, 1)
        self.assertFalse(self.plugin.store.data["queues"])

    async def test_first_keep_can_be_reviewed_once_when_chat_becomes_continuous(self):
        self.clock("21:55")
        first, _ = await self.message("今晚有什么安排？")
        self.assertFalse(first.stopped)
        self.assertFalse(self.plugin.adjustments)
        self.assertFalse(self.plugin.generator.adaptive.await_args.kwargs["active_conversation"])
        self.defer()
        self.clock("21:55:30")
        second, _ = await self.message("我还想接着刚才的话题")
        self.assertFalse(second.stopped)
        self.assertEqual(self.plugin.generator.adaptive.await_count, 2)
        self.assertTrue(self.plugin.generator.adaptive.await_args.kwargs["active_conversation"])
        until = self.plugin.adjustments[UMO]["until"]
        self.clock("21:56")
        await self.message("然后呢？")
        self.assertEqual(self.plugin.generator.adaptive.await_count, 2)
        self.assertEqual(self.plugin.adjustments[UMO]["until"], until)

    async def test_first_normal_message_cannot_automatically_defer_sleep(self):
        self.clock("22:01")
        self.defer()
        event, _ = await self.message("你还醒着吗")
        self.assertTrue(event.stopped)
        self.assertFalse(self.plugin.adjustments)

    async def test_important_first_message_can_use_remaining_finite_sleep_window(self):
        self.clock("22:05")
        self.defer()
        self.decision["importance"] = "important"
        event, _ = await self.message("现在有件重要的事想说", inject=True)
        self.assertFalse(event.stopped)
        until = datetime.fromtimestamp(self.plugin.adjustments[UMO]["until"], TZ)
        self.assertEqual(until.strftime("%H:%M"), "22:20")

    async def test_long_inactive_gap_does_not_count_as_continuing_chat(self):
        await self.activate()
        self.plugin.store.settings["active_conversation_minutes"] = 1
        self.defer()
        self.clock("22:01")
        event, _ = await self.message()
        self.assertTrue(event.stopped)
        self.assertFalse(self.plugin.adjustments)

    async def test_sleep_budget_ends_and_cannot_renew_with_more_messages(self):
        await self.activate()
        self.defer()
        await self.message()
        until = self.plugin.adjustments[UMO]["until"]
        self.clock("22:20")
        expired, _ = await self.message("再等等")
        self.clock("22:40")
        again, _ = await self.message("我还想继续聊")
        self.assertTrue(expired.stopped)
        self.assertTrue(again.stopped)
        self.assertEqual(self.plugin.adjustments[UMO]["until"], until)
        self.assertEqual(self.plugin.generator.adaptive.await_count, 1)
        self.assertEqual(len(self.plugin.store.data["queues"][UMO]["messages"]), 2)

    async def test_urgent_fixed_activity_has_only_short_contact_exception(self):
        self.set_plan("夜间值班")
        before = copy.deepcopy(self.plan)
        self.clock("22:01")
        self.decision.update(action="brief_exception", activity_kind="fixed", importance="urgent", delay_minutes=3,
            reason="用户有紧急事项，需要短暂回应")
        event, request = await self.message("现在有很紧急的事情", inject=True)
        self.assertFalse(event.stopped)
        adjustment = self.plugin.adjustments[UMO]
        self.assertEqual(adjustment["action"], "brief_exception")
        self.assertLessEqual(adjustment["until"] - Frozen.current.timestamp(), 300)
        self.assertIn("有限的联系调整", request.extra_user_content_parts[-1].text)
        self.clock("22:05")
        expired, _ = await self.message()
        self.assertTrue(expired.stopped)
        self.assertEqual(self.plan, before)

    async def test_locked_activity_does_not_ask_model_for_an_exception(self):
        self.set_plan("睡觉", locked=True)
        self.clock("22:01")
        event, _ = await self.message("非常紧急")
        self.assertTrue(event.stopped)
        self.assertFalse(self.plugin.adjustments)
        self.plugin.generator.adaptive.assert_not_awaited()

    async def test_failed_adaptive_decision_keeps_busy_policy(self):
        self.clock("22:01")
        self.plugin.generator.adaptive = AsyncMock(side_effect=RuntimeError("offline model unavailable"))
        event, _ = await self.message()
        self.assertTrue(event.stopped)
        self.assertFalse(self.plugin.adjustments)
        self.assertIn(UMO, self.plugin.store.data["queues"])
        self.assertNotIn(UMO, self.plugin.failures)

    async def test_fixed_duty_cannot_be_deferred_by_wrong_model_classification(self):
        self.set_plan("上课")
        await self.activate()
        self.defer()
        self.clock("22:01")
        event, _ = await self.message()
        self.assertTrue(event.stopped)
        self.assertFalse(self.plugin.adjustments)

    async def test_contact_exception_is_private_to_one_session(self):
        await self.activate()
        self.defer()
        await self.message()
        self.clock("22:01")
        self.decision.update(action="keep", delay_minutes=0)
        other, _ = await self.message(umo="test:FriendMessage:other")
        original, _ = await self.message()
        self.assertTrue(other.stopped)
        self.assertFalse(original.stopped)
        self.assertNotIn("test:FriendMessage:other", self.plugin.adjustments)

    async def test_conversation_switch_does_not_reuse_contact_exception(self):
        await self.activate()
        self.defer()
        await self.message()
        self.ctx.conversation_manager.cid = "conv-2"
        self.clock("22:01")
        self.decision.update(action="keep", delay_minutes=0)
        event, _ = await self.message()
        self.assertTrue(event.stopped)
        self.assertNotIn(UMO, self.plugin.adjustments)
        self.assertEqual(self.plugin.store.data["queues"][UMO]["cid"], "conv-2")

    async def test_disabling_adaptive_ignores_existing_contact_exception(self):
        await self.activate()
        self.defer()
        await self.message()
        self.plugin.store.settings["adaptive_enabled"] = False
        self.clock("22:01")
        event, _ = await self.message()
        self.assertTrue(event.stopped)

    async def test_plan_revision_change_invalidates_existing_contact_window(self):
        await self.activate()
        self.defer()
        await self.message()
        self.plan["revision"] += 1
        self.clock("22:01")
        event, _ = await self.message()
        self.assertTrue(event.stopped)
        self.assertEqual(self.plugin.generator.adaptive.await_count, 1)

    async def test_source_edit_without_revision_cannot_reuse_old_window(self):
        await self.activate()
        self.defer()
        await self.message()
        self.plan["summary"] = "当天安排已被更改"
        self.clock("22:01")
        event, _ = await self.message()
        self.assertTrue(event.stopped)

    async def test_canvas_context_does_not_receive_private_adjustment_reason(self):
        await self.activate()
        self.defer(reason="PRIVATE_TOPIC 不应进入绘图资料")
        await self.message()
        self.clock("22:01")
        self.plugin.store.settings["canvas_integration_enabled"] = True
        state = await self.plugin.get_proactive_context(UMO)
        self.assertTrue(state["allowed"])
        self.assertEqual(state["status"], "free")
        self.assertNotIn("PRIVATE_TOPIC", json.dumps(state))

    async def test_rebuilding_day_increments_source_revision(self):
        previous = self.plan["revision"]
        await self.plugin.ensure(self.persona, UMO, DAY, force="day")
        self.assertEqual(self.plan["revision"], previous + 1)

    async def test_upgrade_cleans_old_queued_commands_and_preserves_real_messages(self):
        for message in ("/reset", "resetting my plans tomorrow"):
            self.plugin.store.enqueue(UMO, "conv-1", self.persona["key"], "用户", message,
                Frozen.current.timestamp(), Frozen.current.timestamp() + 100, "busy", "睡觉")
        self.plugin.store.data.pop("commands_migrated", None)
        self.plugin._migrate_queued_commands()
        self.assertEqual([r["text"] for r in self.plugin.store.data["queues"][UMO]["messages"]],
                         ["resetting my plans tomorrow"])

    async def test_legacy_white_off_config_does_not_migrate_to_all_chats(self):
        self.plugin.store.settings.pop("all_chats_enabled", None)
        self.plugin.store.settings["whitelist_enabled"] = False
        await self.plugin.terminate()
        with patch("astrbot_plugin_persona_schedule.main.StarTools.get_data_dir", return_value=self.temp.name):
            self.plugin = PersonaSchedule(self.ctx, FakeConfig({"enabled": True, "whitelist_enabled": False}))
        event, _ = await self.message()
        self.assertFalse(event.stopped)
        self.assertFalse(self.plugin.store.settings["all_chats_enabled"])
        self.assertFalse(self.plugin.store.data["queues"])

    async def test_contact_window_opens_existing_busy_backlog_for_one_combined_reply(self):
        self.clock("22:01")
        first, _ = await self.message("还在忙吗")
        self.assertTrue(first.stopped)
        self.defer()
        self.clock("22:02")
        second, _ = await self.message("这个话题我还想接着说")
        self.assertTrue(second.stopped)  # Joins the existing batch, never duplicates it.
        await self.session_tick()
        self.assertEqual(len(self.ctx.sent), 1)
        self.assertNotIn(UMO, self.plugin.store.data["queues"])
        self.assertEqual(len(self.ctx.conversation_manager.pairs), 1)
        self.assertEqual(len(self.plugin.generator.reply.await_args.args[1]), 2)

    async def test_disable_during_decision_does_not_create_adjustment_or_queue(self):
        await self.activate()
        self.defer()
        async def decision(*args, **kwargs):
            result = await self.decide(*args, **kwargs)
            self.plugin.store.settings["enabled"] = False
            return result
        self.plugin.generator.adaptive = AsyncMock(side_effect=decision)
        event, _ = await self.message()
        self.assertFalse(event.stopped)
        self.assertFalse(self.plugin.adjustments)
        self.assertFalse(self.plugin.store.data["queues"])

    async def test_active_controlled_chat_receives_one_advance_goodbye(self):
        await self.activate()
        await self.session_tick()
        await self.session_tick()
        self.assertEqual(len(self.ctx.sent), 1)
        self.assertEqual(len(self.ctx.conversation_manager.pairs), 1)
        self.assertEqual(self.plugin.generator.transition_notice.await_count, 1)
        self.assertEqual(self.notice_status(), "sent")

    async def test_notice_has_no_recipient_without_existing_conversation(self):
        await self.activate()
        self.ctx.conversation_manager.cid = None
        await self.session_tick()
        self.assertFalse(self.ctx.sent)
        self.plugin.generator.transition_notice.assert_not_awaited()

    async def test_notice_requires_recent_real_inbound_activity(self):
        env = await self.plugin.environment(UMO, "test")
        self.plugin._remember_session(env)
        self.clock("21:55")
        await self.session_tick()
        self.assertFalse(self.ctx.sent)
        self.plugin.generator.transition_notice.assert_not_awaited()
        await self.activate()
        self.plugin.store.settings["active_conversation_minutes"] = 1
        await self.session_tick()
        self.assertFalse(self.ctx.sent)

    async def test_notice_respects_scope_and_busy_status(self):
        await self.activate()
        self.plugin.store.settings["all_chats_enabled"] = False
        await self.session_tick()
        self.plugin.store.settings["all_chats_enabled"] = True
        self.clock("22:01")
        await self.session_tick()
        self.assertFalse(self.ctx.sent)
        self.plugin.generator.transition_notice.assert_not_awaited()

    async def test_notice_already_included_in_response_is_not_sent_separately(self):
        await self.activate()
        _, request = await self.message(inject=True)
        self.assertIn("upcoming_activity", request.extra_user_content_parts[-1].text)
        await self.session_tick()
        self.assertFalse(self.ctx.sent)
        self.plugin.generator.transition_notice.assert_not_awaited()
        self.assertEqual(self.notice_status(), "included")

    async def cancellation(self, mutate):
        await self.activate()
        async def phrase(*args, **kwargs):
            result = mutate()
            if hasattr(result, "__await__"):
                await result
            return "我一会儿去休息。"
        self.plugin.generator.transition_notice = AsyncMock(side_effect=phrase)
        await self.session_tick()
        self.assertFalse(self.ctx.sent)
        self.assertFalse(self.ctx.conversation_manager.pairs)
        self.assertEqual(self.notice_status(), "cancelled")

    async def test_notice_cancelled_when_generation_crosses_busy_boundary(self):
        await self.cancellation(lambda: self.clock("22:00"))

    async def test_notice_cancelled_when_conversation_changes_during_generation(self):
        await self.cancellation(lambda: setattr(self.ctx.conversation_manager, "cid", "conv-2"))

    async def test_notice_cancelled_when_new_message_arrives_during_generation(self):
        async def new_message():
            self.clock("21:55:01")
            await self.message("还有一件事")
        await self.cancellation(new_message)

    async def test_notice_cancelled_when_switch_is_disabled_during_generation(self):
        await self.cancellation(lambda: self.plugin.store.settings.update(transition_notice_enabled=False))

    async def test_notice_cancelled_when_session_is_removed_from_scope(self):
        await self.cancellation(lambda: self.plugin.store.settings.update(all_chats_enabled=False))

    async def test_notice_cancelled_when_plan_changes_during_generation(self):
        await self.cancellation(lambda: self.plan["slots"][1].update(title="有其他事情要忙"))

    async def test_notice_cancelled_when_current_activity_becomes_busy_during_generation(self):
        await self.cancellation(lambda: self.plan["slots"][0].update(status="busy", title="临时会议"))

    async def test_notice_cancelled_when_a_waiting_batch_appears(self):
        def queued():
            self.plugin.store.enqueue(UMO, "conv-1", self.persona["key"], "用户", "积压消息",
                Frozen.current.timestamp(), Frozen.current.timestamp() + 60, "slow")
        await self.cancellation(queued)

    async def test_uncertain_notice_is_not_automatically_repeated(self):
        await self.activate()
        self.ctx.send_error = True
        await self.session_tick()
        self.ctx.send_error = False
        await self.session_tick()
        self.assertEqual(self.notice_status(), "uncertain")
        self.assertEqual(self.plugin.generator.transition_notice.await_count, 1)
        self.assertFalse(self.ctx.sent)

    async def test_notice_generation_failure_backs_off_without_sending(self):
        await self.activate()
        self.plugin.generator.transition_notice = AsyncMock(side_effect=RuntimeError("offline model down"))
        await self.session_tick()
        await self.session_tick()
        self.assertEqual(self.notice_status(), "failed")
        self.assertEqual(self.plugin.generator.transition_notice.await_count, 1)
        self.assertFalse(self.ctx.sent)

    async def test_restart_during_notice_send_keeps_uncertain_without_resending(self):
        await self.activate()
        await self.session_tick()
        entry = next(iter(self.plugin.dialogue[UMO]["notices"].values()))
        entry["status"] = "sending"
        self.plugin.store.save()
        await self.plugin.terminate()
        self.ctx.sent.clear()
        with patch("astrbot_plugin_persona_schedule.main.StarTools.get_data_dir", return_value=self.temp.name):
            self.plugin = PersonaSchedule(self.ctx, self.config)
        self.plugin.generator.transition_notice = AsyncMock(return_value="不能重发")
        await self.session_tick()
        self.assertEqual(self.notice_status(), "uncertain")
        self.assertFalse(self.ctx.sent)
        self.plugin.generator.transition_notice.assert_not_awaited()

    async def test_diary_contains_adjustment_facts_without_user_reason_or_identifiers(self):
        await self.activate()
        self.defer(reason="PRIVATE_USER_DETAIL 不该进入角色共用日记")
        await self.message("PRIVATE_ORIGINAL_MESSAGE")
        self.clock("00:01", "2026-10-15")
        entry = await self.plugin.ensure_diary(self.persona, DAY, UMO)
        adjustments = entry["source"]["schedule"]["contact_adjustments"]
        self.assertEqual(adjustments[0]["action"], "defer")
        self.assertEqual(adjustments[0]["delay_minutes"], 20)
        for key in ("reason", "umo", "cid", "persona_key", "source_slot"):
            self.assertNotIn(key, adjustments[0])
        diary_prompt = self.ctx.calls[-1]["prompt"]
        self.assertNotIn("PRIVATE_USER_DETAIL", diary_prompt)
        self.assertNotIn("PRIVATE_ORIGINAL_MESSAGE", diary_prompt)
        self.assertFalse(self.ctx.sent)


if __name__ == "__main__":
    unittest.main()
