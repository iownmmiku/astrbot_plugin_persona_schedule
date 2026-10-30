"""Admission and quota regressions with controlled clocks and model doubles."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_schedule import FakeContext, Generator, Store, UMO
from astrbot_plugin_persona_schedule.model_runtime import BudgetExceeded, ModelRuntime, PriorityLimiter


class LimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_overtakes_queued_background_work(self):
        limiter, order = PriorityLimiter(1), []
        async def enter(name, priority):
            async with limiter.slot(priority=priority, background=priority > 0):
                order.append(name)
        async with limiter.slot(priority=0):
            background = asyncio.create_task(enter("background", 10))
            reply = asyncio.create_task(enter("reply", 0))
            await asyncio.sleep(0)
        await asyncio.gather(background, reply)
        self.assertEqual(order, ["reply", "background"])
        self.assertEqual(limiter.active, 0)

    async def test_waiting_background_ages_ahead_of_new_foreground(self):
        clock, order = [0.0], []
        limiter = PriorityLimiter(1, aging_seconds=1, clock=lambda: clock[0])
        async def enter(name, priority):
            async with limiter.slot(priority=priority, background=priority > 0):
                order.append(name)
        async with limiter.slot(priority=0):
            old = asyncio.create_task(enter("old", 10))
            await asyncio.sleep(0)
            clock[0] = 20
            fresh = asyncio.create_task(enter("fresh", 0))
            await asyncio.sleep(0)
        await asyncio.gather(old, fresh)
        self.assertEqual(order, ["old", "fresh"])

    async def test_background_cannot_occupy_reserved_foreground_slot(self):
        limiter, entered, release = PriorityLimiter(3), [], asyncio.Event()
        async def background(number):
            async with limiter.slot(priority=10, background=True):
                entered.append(number)
                await release.wait()
        tasks = [asyncio.create_task(background(i)) for i in range(3)]
        await asyncio.sleep(0)
        self.assertEqual(len(entered), 2)
        async with limiter.slot(priority=0):
            self.assertEqual(limiter.active, 3)
        release.set()
        await asyncio.gather(*tasks)

    async def test_cancellation_releases_waiting_and_granted_capacity(self):
        limiter = PriorityLimiter(1)
        entered = asyncio.Event()
        async def hold():
            async with limiter.slot():
                entered.set()
                await asyncio.Event().wait()
        running = asyncio.create_task(hold())
        await entered.wait()
        waiting = asyncio.create_task(hold())
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual((limiter.active, limiter.waiters), (0, []))


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.ctx = FakeContext()
        self.generator = Generator(self.ctx, self.store)
        self.runtime = self.generator.runtime

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def call(self, purpose="schedule", guard=None):
        return await self.generator.generate("fixture", {}, UMO, purpose=purpose, before_generate=guard)

    def row(self, purpose):
        return self.runtime.snapshot()["today"]["purposes"][purpose]

    async def test_background_budget_does_not_block_reply_or_immediate_schedule(self):
        self.store.settings["daily_background_call_limit"] = 1
        with self.generator.task_scope("schedule", background=True):
            await self.call()
            with self.assertRaises(BudgetExceeded):
                await self.call()
        await self.call("reply")
        await self.call("schedule")
        self.assertEqual(len(self.ctx.calls), 3)
        self.assertEqual(self.runtime.snapshot()["background_used"], 1)
        self.assertEqual(self.row("schedule")["budget_skipped"], 1)

    async def test_zero_budget_is_unlimited_and_scope_does_not_leak(self):
        self.store.settings["daily_background_call_limit"] = 0
        with self.generator.task_scope("diary", background=True):
            await self.call()
            await self.call()
        await self.call()
        self.assertEqual(self.row("diary")["calls"], 2)
        self.assertEqual(self.row("schedule")["calls"], 1)

    async def test_daily_budget_persists_and_resets_on_next_configured_date(self):
        self.store.settings["daily_background_call_limit"] = 1
        with patch.object(self.runtime, "_date", return_value="2026-10-29"):
            await self.call("incident")
        reloaded = Store(self.temp.name)
        runtime = Generator(self.ctx, reloaded).runtime
        with patch.object(runtime, "_date", return_value="2026-10-29"):
            self.assertEqual(runtime.snapshot()["background_used"], 1)
        with patch.object(self.runtime, "_date", return_value="2026-10-30"):
            await self.call("incident")
            self.assertEqual(self.runtime.snapshot()["background_used"], 1)

    async def test_real_usage_only_is_counted_and_raw_response_is_not_stored(self):
        self.ctx.llm_generate = AsyncMock(return_value=types.SimpleNamespace(completion_text="private",
            usage=types.SimpleNamespace(input_other=2, input_cached=4, output=3)))
        await self.call()
        row = self.row("schedule")
        self.assertEqual((row["input_tokens"], row["output_tokens"], row["usage_samples"]), (6, 3, 1))
        self.assertNotIn("private", json.dumps(self.store.data.get("model_usage")))
        self.ctx.llm_generate.return_value = types.SimpleNamespace(completion_text="no usage")
        await self.call()
        self.assertEqual(self.row("schedule")["usage_samples"], 1)

    async def test_failures_are_counted_without_saving_exception_credentials(self):
        self.ctx.llm_generate = AsyncMock(side_effect=RuntimeError("credential-fixture"))
        with self.assertRaises(RuntimeError):
            await self.call("reply")
        self.assertEqual(self.row("reply")["failed"], 1)
        self.assertNotIn("credential-fixture", json.dumps(self.runtime.snapshot()))
        self.assertEqual(self.runtime.limiter.active, 0)

    async def test_cancelled_provider_counts_once_and_releases_slot(self):
        started = asyncio.Event()
        async def blocked(**kwargs):
            started.set()
            await asyncio.Event().wait()
        self.ctx.llm_generate = blocked
        task = asyncio.create_task(self.call("reply"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.row("reply")["cancelled"], 1)
        self.assertEqual(self.runtime.limiter.active, 0)

    async def test_revocation_after_wait_and_provider_lookup_prevents_call(self):
        valid = [True]
        def guard():
            if not valid[0]:
                raise ValueError("revoked")
        async def provider(*args, **kwargs):
            valid[0] = False
            return "provider"
        self.generator.provider_id = provider
        with self.assertRaisesRegex(ValueError, "revoked"):
            await self.call(guard=guard)
        self.assertEqual(self.ctx.calls, [])
        self.assertNotIn("model_usage", self.store.data)

    async def test_structured_retries_count_actual_provider_calls(self):
        self.ctx.llm_generate = AsyncMock(side_effect=[types.SimpleNamespace(completion_text="not json"),
            types.SimpleNamespace(completion_text='{"ok": 1}')])
        result = await self.generator.structured("fixture", {}, lambda value: value["ok"], UMO)
        self.assertEqual(result, 1)
        self.assertEqual((self.row("schedule")["calls"], self.row("schedule")["retries"]), (2, 1))

    async def test_budget_applies_to_format_retry_without_new_call(self):
        self.store.settings["daily_background_call_limit"] = 1
        self.ctx.llm_generate = AsyncMock(return_value=types.SimpleNamespace(completion_text="not json"))
        with self.assertRaises(BudgetExceeded):
            await self.generator.structured("fixture", {}, lambda value: value, UMO, purpose="incident")
        self.assertEqual(self.ctx.llm_generate.await_count, 1)

    async def test_failed_budget_reservation_does_not_leave_phantom_usage(self):
        before = copy.deepcopy(self.store.data)
        with patch.object(self.store, "save", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                await self.call("incident")
        self.assertEqual(self.store.data, before)
        self.assertEqual(self.ctx.calls, [])

    async def test_failed_completion_statistics_do_not_repeat_model_result(self):
        with patch.object(self.store, "save", side_effect=[None, OSError("full")]):
            result = await self.call("reply")
        self.assertTrue(result)
        self.assertEqual(len(self.ctx.calls), 1)
        self.assertEqual(self.runtime.snapshot()["statistics_error"], "OSError")

    async def test_statistics_keep_at_most_thirty_dates(self):
        for number in range(1, 32):
            day = f"2026-10-{number:02}"
            with patch.object(self.runtime, "_date", return_value=day):
                await self.call("schedule")
        self.assertEqual(len(self.store.data["model_usage"]["days"]), 30)
        self.assertNotIn("2026-10-01", self.store.data["model_usage"]["days"])

    async def test_incident_adaptive_and_preview_use_schedule_model_route(self):
        calls = []
        async def current(umo):
            calls.append(umo)
            return "shared"
        self.ctx.get_current_chat_provider_id = current
        for purpose in ("incident", "adaptive", "preview", "notice"):
            self.assertEqual(await self.generator.provider_id(UMO, purpose=purpose), "shared")
        self.assertEqual(calls, [UMO] * 4)


if __name__ == "__main__":
    unittest.main()
