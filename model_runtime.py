"""Fair model admission, background call budgets and bounded usage summaries."""
from __future__ import annotations

import asyncio
import copy
import inspect
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime
from zoneinfo import ZoneInfo


class BudgetExceeded(RuntimeError):
    """An optional background request exhausted the configured daily budget."""


class PriorityLimiter:
    """One slot is reserved for foreground work; waiting requests age fairly."""

    def __init__(self, capacity=3, *, aging_seconds=5, clock=time.monotonic):
        self.capacity = max(1, capacity)
        self.background_capacity = max(1, self.capacity - 1)
        self.aging_seconds = max(0.01, aging_seconds)
        self.clock = clock
        self.active = self.background_active = self.sequence = 0
        self.waiters = []

    def _drain(self):
        self.waiters[:] = [row for row in self.waiters if not row["future"].cancelled()]
        while self.active < self.capacity:
            eligible = [row for row in self.waiters
                        if not row["background"] or self.background_active < self.background_capacity]
            if not eligible:
                break
            now = self.clock()
            row = min(eligible, key=lambda item: (
                item["priority"] - int((now - item["at"]) / self.aging_seconds), item["sequence"]))
            self.waiters.remove(row)
            self.active += 1
            self.background_active += int(row["background"])
            row["granted"] = True
            row["future"].set_result(max(0.0, now - row["at"]))

    @asynccontextmanager
    async def slot(self, *, priority=5, background=False):
        self.sequence += 1
        row = {"future": asyncio.get_running_loop().create_future(), "priority": priority,
               "background": background, "at": self.clock(), "sequence": self.sequence, "granted": False}
        self.waiters.append(row)
        self._drain()
        try:
            waited = await row["future"]
            yield waited
        finally:
            if row["granted"]:
                self.active -= 1
                self.background_active -= int(background)
            elif row in self.waiters:
                self.waiters.remove(row)
            self._drain()


class ModelRuntime:
    PURPOSES = {"schedule", "reply", "diary", "adaptive", "incident", "notice", "preview"}

    def __init__(self, store, *, capacity=3):
        self.store = store
        self.limiter = PriorityLimiter(capacity)
        self._scope = ContextVar("persona_schedule_model_task", default=None)
        self.statistics_error = ""

    @contextmanager
    def task_scope(self, purpose, *, background=False):
        if purpose not in self.PURPOSES:
            raise ValueError("模型任务用途无效")
        token = self._scope.set((purpose, bool(background)))
        try:
            yield
        finally:
            self._scope.reset(token)

    def classify(self, purpose):
        scope = self._scope.get()
        purpose, background = scope if scope else (purpose, purpose in {"incident", "notice"})
        if purpose not in self.PURPOSES:
            raise ValueError("模型任务用途无效")
        if purpose in {"reply", "adaptive"}:
            background = False
        return purpose, background

    def _date(self):
        return datetime.now(ZoneInfo(self.store.settings.get("timezone", "Asia/Shanghai"))).date().isoformat()

    def _row(self, day, purpose):
        days = self.store.data.setdefault("model_usage", {}).setdefault("days", {})
        while len(days) > 29 and day not in days:
            del days[min(days)]
        daily = days.setdefault(day, {"background_calls": 0, "purposes": {}})
        return daily, daily["purposes"].setdefault(purpose, {"calls": 0, "success": 0, "failed": 0,
            "cancelled": 0, "retries": 0, "budget_skipped": 0, "wait_seconds": 0.0,
            "duration_seconds": 0.0, "usage_samples": 0, "input_tokens": 0, "output_tokens": 0})

    def _save_metrics(self):
        try:
            self.store.save()
            self.statistics_error = ""
        except Exception as exc:
            # A statistics failure must not turn an accepted model result into
            # another model/platform send. The next ordinary save may persist it.
            self.statistics_error = type(exc).__name__

    @staticmethod
    async def guard(callback):
        if callback:
            result = callback()
            if inspect.isawaitable(result):
                await result

    @staticmethod
    def usage(response):
        value = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
        if value is None:
            return None
        def get(name):
            result = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
            return result if type(result) is int and result >= 0 else None
        incoming, outgoing = get("input"), get("output")
        if incoming is None:
            incoming = get("prompt_tokens")
        if incoming is None and get("input_other") is not None:
            incoming = get("input_other") + (get("input_cached") or 0)
        if outgoing is None:
            outgoing = get("completion_tokens")
        return (incoming, outgoing) if incoming is not None and outgoing is not None else None

    async def invoke(self, purpose, provider_lookup, invoke, *, guard=None, retry=False):
        purpose, background = self.classify(purpose)
        priority = 0 if purpose in {"reply", "adaptive"} else 10 if background else 5
        async with self.limiter.slot(priority=priority, background=background) as waited:
            await self.guard(guard)
            provider_id = await provider_lookup(purpose)
            await self.guard(guard)
            day = self._date()
            usage_existed = "model_usage" in self.store.data
            usage_before = self.store.data.get("model_usage", {})
            days_before = dict(usage_before.get("days", {}))
            previous_daily = days_before.get(day)
            purpose_existed = previous_daily is not None and purpose in previous_daily["purposes"]
            previous_row = copy.deepcopy(previous_daily["purposes"][purpose]) if purpose_existed else None
            previous_background = previous_daily["background_calls"] if previous_daily is not None else 0
            daily, row = self._row(day, purpose)
            limit = self.store.settings.get("daily_background_call_limit", 0)
            if background and limit and daily["background_calls"] >= limit:
                row["budget_skipped"] += 1
                self._save_metrics()
                raise BudgetExceeded("今日后台模型调用已达到预算，保留现有安排")
            row["calls"] += 1
            row["retries"] += int(retry)
            row["wait_seconds"] += waited
            if background:
                daily["background_calls"] += 1
            try:
                # Reserve budget durably before entering the provider. A crash
                # may leave an unfinished count, but cannot reset spent quota.
                self.store.save()
            except Exception:
                # Preserve other in-flight requests' dictionary identities.
                if purpose_existed:
                    row.clear()
                    row.update(previous_row)
                else:
                    daily["purposes"].pop(purpose, None)
                daily["background_calls"] = previous_background
                days = self.store.data["model_usage"]["days"]
                days.clear()
                days.update(days_before)
                if not usage_existed:
                    self.store.data.pop("model_usage", None)
                raise
            start = time.monotonic()
            try:
                response = await invoke(provider_id)
            except asyncio.CancelledError:
                row["cancelled"] += 1
                raise
            except Exception:
                row["failed"] += 1
                raise
            else:
                row["success"] += 1
                usage = self.usage(response)
                if usage is not None:
                    row["usage_samples"] += 1
                    row["input_tokens"] += usage[0]
                    row["output_tokens"] += usage[1]
                return response
            finally:
                row["duration_seconds"] += max(0, time.monotonic() - start)
                self._save_metrics()

    def snapshot(self):
        day = self._date()
        days = self.store.data.get("model_usage", {}).get("days", {})
        today = copy.deepcopy(days.get(day, {"background_calls": 0, "purposes": {}}))
        return {"date": day, "timezone": self.store.settings.get("timezone", "Asia/Shanghai"),
                "active": self.limiter.active, "queued": len(self.limiter.waiters),
                "capacity": self.limiter.capacity, "background_capacity": self.limiter.background_capacity,
                "background_limit": self.store.settings.get("daily_background_call_limit", 0),
                "background_used": today["background_calls"], "today": today,
                "days": [{"date": key, **copy.deepcopy(value)} for key, value in sorted(days.items(), reverse=True)[:30]],
                "statistics_error": self.statistics_error}
