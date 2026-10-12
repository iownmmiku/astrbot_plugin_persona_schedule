"""Runtime extension regressions using explicit AstrBot API doubles.

Use smoke_astrbot.py separately for integration with a real installed SDK.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import types
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from test_schedule import FakeConfig, FakeContext, FakeEvent, PersonaSchedule, UMO, slots


class RuntimeExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ctx = FakeContext()
        self.ctx.get_all_providers = lambda: [self.provider(pid, pid + "-model") for pid in
                                               ("plan-provider", "reply-provider", "legacy-provider", "fake-provider")]
        with patch("astrbot_plugin_persona_schedule.main.StarTools.get_data_dir", return_value=self.temp.name):
            self.plugin = PersonaSchedule(self.ctx, FakeConfig())
        self.persona = await self.plugin._persona("traveler")
        self.day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    @staticmethod
    def provider(provider_id="plan-provider", model="plan-model", *, asynchronous=False):
        meta = types.SimpleNamespace(id=provider_id, model="stale-meta-model", type="openai",
                                     provider_type=types.SimpleNamespace(value="chat_completion"),
                                     api_key="fixture-provider-secret", base_url="https://private.example")
        obj = types.SimpleNamespace(provider_config={"key": ["fixture-provider-secret"]})
        if asynchronous:
            obj.meta = AsyncMock(return_value=meta)
            obj.get_model = AsyncMock(return_value=model)
        else:
            obj.meta = lambda: meta
            obj.get_model = lambda: model
        return obj

    async def test_sync_provider_list_uses_actual_model_and_redacts_provider_config(self):
        self.ctx.get_all_providers = lambda: [self.provider()]
        result = await self.plugin.web_providers({})
        self.assertEqual([row["id"] for row in result["providers"]], ["plan-provider"])
        row = result["providers"][0]
        self.assertEqual(row["model"], "plan-model")
        self.assertEqual(row["type"], "chat")
        self.assertGreater(result["checked_at"], 0)
        self.assertNotIn("fixture-provider-secret", json.dumps(result))
        self.assertNotIn("private.example", json.dumps(result))

    async def test_async_provider_list_meta_and_get_model_are_supported(self):
        provider = self.provider("async-provider", "async-model", asynchronous=True)
        self.ctx.get_all_providers = AsyncMock(return_value=[provider])
        result = await self.plugin.web_providers({})
        self.assertEqual(result["providers"][0]["model"], "async-model")
        provider.meta.assert_awaited()
        provider.get_model.assert_awaited()

    async def test_provider_without_get_model_uses_safe_metadata_fallback(self):
        provider = self.provider()
        del provider.get_model
        self.ctx.get_all_providers = lambda: [provider]
        result = await self.plugin.web_providers({})
        self.assertEqual(result["providers"][0]["model"], "stale-meta-model")
        self.assertNotIn("fixture-provider-secret", json.dumps(result))

    async def test_bad_provider_is_skipped_and_duplicate_ids_are_collapsed(self):
        broken = types.SimpleNamespace(meta=lambda: (_ for _ in ()).throw(RuntimeError("fixture-provider-secret")))
        self.ctx.get_all_providers = lambda: [broken, self.provider(), self.provider()]
        result = await self.plugin.web_providers({})
        self.assertEqual(len(result["providers"]), 1)
        self.assertNotIn("fixture-provider-secret", json.dumps(result))

    async def test_non_chat_provider_is_filtered_from_model_list(self):
        embedding = self.provider("embedding-provider", "embedding-model")
        metadata = embedding.meta()
        metadata.provider_type = types.SimpleNamespace(value="embedding")
        embedding.meta = lambda: metadata
        self.ctx.get_all_providers = lambda: [embedding, self.provider()]
        result = await self.plugin.web_providers({})
        self.assertEqual([row["id"] for row in result["providers"]], ["plan-provider"])

    async def test_explicit_schedule_and_reply_providers_are_independent(self):
        await self.plugin.web_settings({"schedule_provider_id": "plan-provider", "reply_provider_id": "reply-provider"})
        await self.plugin.ensure(self.persona, UMO, self.day)
        self.assertTrue(self.ctx.calls)
        self.assertEqual({call["chat_provider_id"] for call in self.ctx.calls}, {"plan-provider"})
        self.plugin.store.settings["enabled"] = True
        record = self.plugin.store.data["personas"]["traveler"]
        record["days"][self.day]["slots"] = slots("busy")
        await self.plugin.intercept_message(FakeEvent("先给你留言"))
        record["days"][self.day]["slots"] = slots("free")
        await self.plugin._session_tick(UMO, self.plugin.store.data["sessions"][UMO])
        self.assertEqual(self.ctx.calls[-1]["chat_provider_id"], "reply-provider")
        self.assertEqual(len(self.ctx.sent), 1)

    async def test_legacy_provider_is_fallback_without_changing_session_selection(self):
        await self.plugin.web_settings({"provider_id": "legacy-provider"})
        await self.plugin.ensure(self.persona, UMO, self.day)
        self.assertEqual({call["chat_provider_id"] for call in self.ctx.calls}, {"legacy-provider"})
        self.assertEqual(await self.ctx.get_current_chat_provider_id(UMO), "fake-provider")
        self.assertEqual(self.plugin.store.settings["schedule_provider_id"], "")
        self.assertEqual(self.plugin.store.settings["reply_provider_id"], "")

    async def test_diagnostics_is_read_only_and_does_not_expose_secrets(self):
        self.plugin.store.settings["tavily_api_key"] = "fixture-search-secret"
        self.ctx.get_all_providers = lambda: [self.provider()]
        before = (len(self.ctx.calls), len(self.ctx.sent))
        result = await self.plugin.web_diagnostics({"umo": UMO})
        self.assertEqual(before, (len(self.ctx.calls), len(self.ctx.sent)))
        raw = json.dumps(result)
        self.assertNotIn("fixture-search-secret", raw)
        self.assertNotIn("fixture-provider-secret", raw)
        self.assertNotIn("今天去哪里", raw)
        self.assertIn("worker", result)

    async def test_diagnostics_reports_actual_session_persona_and_selected_providers(self):
        await self.plugin.web_settings({"schedule_provider_id": "plan-provider", "reply_provider_id": "reply-provider"})
        env = await self.plugin.environment(UMO, "test")
        self.plugin._remember_session(env)
        result = await self.plugin.web_diagnostics({"umo": UMO})
        raw = json.dumps(result)
        self.assertIn("traveler", raw)
        self.assertIn("plan-provider", raw)
        self.assertIn("reply-provider", raw)
        self.assertIn("conv-1", raw)

    async def test_running_job_has_recoverable_progress_in_state(self):
        started, release = asyncio.Event(), asyncio.Event()
        original = self.plugin.generator.profile

        async def blocked_profile(*args, **kwargs):
            started.set()
            await release.wait()
            return await original(*args, **kwargs)

        self.plugin.generator.profile = blocked_profile
        job_id = (await self.plugin.web_generate({"persona_id": "traveler", "date": self.day, "scope": "profile"}))["job_id"]
        await asyncio.wait_for(started.wait(), timeout=3)
        try:
            state = await self.plugin.web_state({})
            restored = next(job for job in state["jobs"] if job["id"] == job_id)
            self.assertEqual(restored["status"], "running")
            self.assertEqual(restored["date"], self.day)
            self.assertEqual(restored["scope"], "profile")
            self.assertIsInstance(restored["stage"], str)
            self.assertTrue(restored["stage"])
            self.assertGreaterEqual(restored["progress"], 0)
            self.assertLess(restored["progress"], 100)
            self.assertEqual((await self.plugin.web_job({"id": job_id}))["id"], job_id)
        finally:
            release.set()
        await asyncio.wait_for(asyncio.gather(*list(self.plugin.tasks)), timeout=5)
        job = await self.plugin.web_job({"id": job_id})
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["progress"], 100)

    async def test_failed_job_sanitizes_provider_exception(self):
        self.plugin.generator.profile = AsyncMock(side_effect=RuntimeError("Authorization fixture-secret-bearer"))
        job_id = (await self.plugin.web_generate({"persona_id": "traveler", "date": self.day, "scope": "profile"}))["job_id"]
        await asyncio.wait_for(asyncio.gather(*list(self.plugin.tasks)), timeout=3)
        job = await self.plugin.web_job({"id": job_id})
        self.assertEqual(job["status"], "failed")
        self.assertIn("RuntimeError", job["message"])
        state = await self.plugin.web_state({})
        self.assertNotIn("fixture-secret-bearer", json.dumps(state))

    async def test_reloaded_plugin_exposes_interrupted_job_instead_of_stale_running(self):
        self.plugin.jobs["persisted-job"] = {"id": "persisted-job", "persona_id": "traveler", "date": self.day,
            "scope": "day", "status": "running", "stage": "day", "progress": 70, "message": "处理中"}
        self.plugin.store.save()
        with patch("astrbot_plugin_persona_schedule.main.StarTools.get_data_dir", return_value=self.temp.name):
            restarted = PersonaSchedule(self.ctx, FakeConfig())
        try:
            result = await restarted.web_job({"id": "persisted-job"})
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["stage"], "interrupted")
            self.assertIn("重载", result["message"])
            self.assertEqual(result["progress"], 70)
        finally:
            await restarted.terminate()

    async def test_worker_diagnostics_reflect_background_start_and_tick(self):
        before = await self.plugin.web_diagnostics({})
        self.assertEqual(before["worker"]["state"], "not_started")
        await self.plugin.initialize()
        await asyncio.sleep(0)
        after = await self.plugin.web_diagnostics({})
        self.assertEqual(after["worker"]["state"], "running")
        self.assertGreater(after["worker"]["last_tick"], 0)

    async def test_expired_old_message_does_not_drop_new_message_before_delivery(self):
        await self.plugin.ensure(self.persona, UMO, self.day)
        self.plugin.store.settings["enabled"] = True
        record = self.plugin.store.data["personas"]["traveler"]
        record["days"][self.day]["slots"] = slots("busy")
        env = await self.plugin.environment(UMO, "test")
        self.plugin._remember_session(env)
        old = time.time() - 73 * 3600
        self.plugin.store.enqueue(UMO, "conv-1", self.persona["key"], "u", "已过期旧消息", old, old, "busy")
        await self.plugin.intercept_message(FakeEvent("仍在期限内的新消息"))
        record["days"][self.day]["slots"] = slots("free")
        await self.plugin._session_tick(UMO, self.plugin.store.data["sessions"][UMO])
        payload = json.loads(self.ctx.calls[-1]["prompt"])
        self.assertEqual([message["text"] for message in payload["queued_messages"]], ["仍在期限内的新消息"])
        self.assertEqual(self.plugin.store.data["expired_messages"], 1)
        self.assertEqual(len(self.ctx.sent), 1)

    async def test_slot_delay_override_is_used_by_interception(self):
        await self.plugin.ensure(self.persona, UMO, self.day)
        self.plugin.store.settings["enabled"] = True
        record = self.plugin.store.data["personas"]["traveler"]
        record["days"][self.day]["slots"] = [{**slots("slow")[0], "slow_min_seconds": 20, "slow_max_seconds": 30}]
        with patch("astrbot_plugin_persona_schedule.main.random.uniform", return_value=22) as uniform:
            await self.plugin.intercept_message(FakeEvent())
        uniform.assert_called_once_with(20, 30)
        due = self.plugin.store.data["queues"][UMO]["due"]
        self.assertGreaterEqual(due - time.time(), 20)
        self.assertLessEqual(due - time.time(), 22)

    async def test_all_rebuild_scopes_preserve_manual_day_and_week_locks(self):
        await self.plugin.ensure(self.persona, UMO, self.day)
        record = self.plugin.store.data["personas"]["traveler"]
        record["profile"]["weekly"] = [
            {**slots("busy", "固定休息")[0], "end": "06:00", "days": list(range(1, 8)), "locked": True},
            {**slots()[0], "start": "06:00", "days": list(range(1, 8))}]
        manual_slots = [
            {**slots()[0], "end": "12:00"},
            {**slots("slow", "手动午后约会")[0], "start": "12:00", "end": "13:00", "locked": True,
             "slow_min_seconds": 20, "slow_max_seconds": 30},
            {**slots()[0], "start": "13:00"}]
        await self.plugin.web_save({"persona_id": "traveler", "kind": "profile", "value": record["profile"]})
        await self.plugin.web_save({"persona_id": "traveler", "kind": "day", "date": self.day,
                                    "value": {"summary": "手动日程", "slots": manual_slots}})
        for scope in ("day", "calendar", "profile"):
            with self.subTest(scope=scope):
                profile, _, day = await self.plugin.ensure(self.persona, UMO, self.day, force=scope)
                locked = next(slot for slot in day["slots"] if slot["title"] == "手动午后约会")
                self.assertEqual((locked["start"], locked["end"]), ("12:00", "13:00"))
                self.assertTrue(locked["locked"])
                self.assertEqual(locked["slow_min_seconds"], 20)
                self.assertTrue(any(slot["title"] == "固定休息" and slot["locked"] for slot in profile["weekly"]))

    async def test_edited_weekly_lock_replaces_generated_day_inheritance(self):
        profile, _, _ = await self.plugin.ensure(self.persona, UMO, self.day)
        profile["weekly"] = [
            {**slots("busy", "旧周表作息")[0], "end": "06:00", "days": list(range(1, 8)), "locked": True},
            {**slots()[0], "start": "06:00", "days": list(range(1, 8))}]
        await self.plugin.web_save({"persona_id": "traveler", "kind": "profile", "value": profile})
        _, _, generated = await self.plugin.ensure(self.persona, UMO, self.day)
        self.assertTrue(any(slot["title"] == "旧周表作息" for slot in generated["slots"]))
        profile["weekly"][0]["title"] = "新周表作息"
        await self.plugin.web_save({"persona_id": "traveler", "kind": "profile", "value": profile})
        _, _, regenerated = await self.plugin.ensure(self.persona, UMO, self.day)
        self.assertTrue(any(slot["title"] == "新周表作息" for slot in regenerated["slots"]))
        self.assertFalse(any(slot["title"] == "旧周表作息" for slot in regenerated["slots"]))

    async def test_expiry_during_generation_defers_and_rebuilds_from_recent_messages(self):
        await self.plugin.ensure(self.persona, UMO, self.day)
        self.plugin.store.settings["enabled"] = True
        record = self.plugin.store.data["personas"]["traveler"]
        record["days"][self.day]["slots"] = slots("busy")
        await self.plugin.intercept_message(FakeEvent("生成时过期的旧留言"))
        record["days"][self.day]["slots"] = slots("free")
        original = self.plugin.generator.reply

        async def expire_and_receive(*args, **kwargs):
            queue = self.plugin.store.data["queues"][UMO]
            queue["messages"][0]["at"] = time.time() - 73 * 3600
            self.plugin.store.enqueue(UMO, "conv-1", self.persona["key"], "u", "生成时刚收到的新留言",
                                      time.time(), time.time() - 1, "slow")
            return "含过期消息的回复不应发送"

        self.plugin.generator.reply = expire_and_receive
        await self.plugin._session_tick(UMO, self.plugin.store.data["sessions"][UMO])
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(self.plugin.store.data["delivery_health"][UMO]["status"], "deferred")
        self.plugin.generator.reply = original
        await self.plugin._session_tick(UMO, self.plugin.store.data["sessions"][UMO])
        self.assertEqual(len(self.ctx.sent), 1)
        payload = json.loads(self.ctx.calls[-1]["prompt"])
        self.assertEqual([message["text"] for message in payload["queued_messages"]], ["生成时刚收到的新留言"])

    async def test_delivery_health_survives_reload_without_resending(self):
        await self.plugin.ensure(self.persona, UMO, self.day)
        self.plugin.store.settings["enabled"] = True
        record = self.plugin.store.data["personas"]["traveler"]
        record["days"][self.day]["slots"] = slots("busy")
        await self.plugin.intercept_message(FakeEvent())
        record["days"][self.day]["slots"] = slots("free")
        await self.plugin._session_tick(UMO, self.plugin.store.data["sessions"][UMO])
        health = dict(self.plugin.store.data["delivery_health"][UMO])
        with patch("astrbot_plugin_persona_schedule.main.StarTools.get_data_dir", return_value=self.temp.name):
            restarted = PersonaSchedule(self.ctx, FakeConfig())
        try:
            self.assertEqual(restarted.store.data["delivery_health"][UMO], health)
            self.assertTrue(health["verified"])
            await restarted._session_tick(UMO, restarted.store.data["sessions"][UMO])
            self.assertEqual(len(self.ctx.sent), 1)
        finally:
            await restarted.terminate()

    async def test_successful_delivery_records_health_and_diagnostics_does_not_resend(self):
        await self.plugin.ensure(self.persona, UMO, self.day)
        self.plugin.store.settings["enabled"] = True
        record = self.plugin.store.data["personas"]["traveler"]
        record["days"][self.day]["slots"] = slots("busy")
        await self.plugin.intercept_message(FakeEvent())
        record["days"][self.day]["slots"] = slots("free")
        await self.plugin._session_tick(UMO, self.plugin.store.data["sessions"][UMO])
        health = self.plugin.store.data["delivery_health"][UMO]
        self.assertGreater(health["last_send_at"], 0)
        self.assertTrue(health["verified"])
        before = len(self.ctx.sent)
        await self.plugin.web_diagnostics({"umo": UMO})
        self.assertEqual(len(self.ctx.sent), before)

    async def test_uncertain_delivery_records_unverified_health(self):
        await self.plugin.ensure(self.persona, UMO, self.day)
        self.plugin.store.settings["enabled"] = True
        record = self.plugin.store.data["personas"]["traveler"]
        record["days"][self.day]["slots"] = slots("busy")
        await self.plugin.intercept_message(FakeEvent())
        record["days"][self.day]["slots"] = slots("free")
        self.ctx.send_error = True
        await self.plugin._session_tick(UMO, self.plugin.store.data["sessions"][UMO])
        health = self.plugin.store.data["delivery_health"][UMO]
        self.assertEqual(health["status"], "uncertain")
        self.assertFalse(health["verified"])
        self.ctx.send_error = False
        await self.plugin._session_tick(UMO, self.plugin.store.data["sessions"][UMO])
        self.assertEqual(self.ctx.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
