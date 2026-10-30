from __future__ import annotations

import asyncio
import copy
import inspect
import json
import random
import time
import uuid
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .core import (DEFAULTS, LABELS, Store, active_events, allowed, digest, field,
                   merge_locked_slots, minute, slow_bounds, slot_at, validate_events,
                   validate_profile, validate_settings, validate_slots, weekly_for_day)
from .generator import Generator, is_chat_provider
from .commands import is_command_event
from .adaptive import adjustment_summary
from .interaction import InteractionMixin
from .recurrence import apply_recurrences, expand_rules, validate_rules
from .diary_context import add_material, bind_initial_material, chat_enabled, material_summary, prune_materials, select_materials
from .workbench import WorkbenchMixin, _record_for_edit
from .maintenance import MaintenanceMixin, EditConflict
from .incidents import IncidentMixin
from .model_runtime import BudgetExceeded

NAME = "astrbot_plugin_persona_schedule"


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


@register(NAME, "iownmmiku", "按人设生成生活日程、活动、日记和合并补回消息", "0.3.7")
class PersonaSchedule(MaintenanceMixin, IncidentMixin, WorkbenchMixin, InteractionMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context, self.config = context, config
        self.store = Store(StarTools.get_data_dir(NAME))
        self.store.data["settings"] = validate_settings({k: v for k, v in config.items() if k in DEFAULTS}, self.store.settings)
        if self.store.data.get("recovery_info", {}).get("requires_review"):
            self.store.settings["enabled"] = False
        prune_materials(self.store, time.time())
        self.store.save()
        self.generator = Generator(context, self.store)
        self.persona_locks, self.session_locks = {}, {}
        self.diary_locks, self.diary_tasks = {}, {}
        self.plan_tasks = {}
        self.interaction_locks = {}
        self.dialogue = self.store.data.setdefault("dialogue_state", {})
        self.adjustments = self.store.data.setdefault("contact_adjustments", {})
        self.store.data.setdefault("adaptation_log", [])
        for meta in self.dialogue.values():
            for notice in meta.get("notices", {}).values():
                if notice.get("status") == "sending":
                    notice["status"] = "uncertain"
                elif notice.get("status") == "generating":
                    notice.update(status="failed", retry_at=time.time() + 60)
        self.tasks = set()
        self.jobs = self.store.data.setdefault("jobs", {})
        for job in self.jobs.values():
            if job.get("status") == "running":
                job.update(status="failed", stage="interrupted", updated_at=time.time(),
                           message="插件已重载，生成任务中断；原日程仍保留，请重新生成")
        self.health = self.store.data.setdefault("delivery_health", {})
        for umo, health in self.health.items():
            if health.get("status") == "sending":
                health.update(status="uncertain", verified=False, error="上次发送期间进程退出，请核对平台")
            elif health.get("status") == "generating":
                health.update(status="deferred", verified=False, error="上次回复生成已中断，等待重新检查日程")
        # Provider calls share the purpose-aware limiter in Generator.runtime.
        # This optional outer gate also supports isolated cancellation tests.
        self.generation_slots = nullcontext()
        self.failures = {}
        self.closing = False
        self.worker = None
        self.last_tick, self.worker_error = 0, ""
        self.import_previews = {}
        self.init_incidents()
        self.store.save()
        self._migrate_queued_commands()
        self._register_web()

    async def initialize(self):
        self.worker = self._spawn(self._run(), "persona-schedule-worker")

    def _spawn(self, coroutine, name):
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def terminate(self):
        self.closing = True
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        self.store.save()

    def _lock(self, mapping, key):
        return mapping.setdefault(key, asyncio.Lock())

    def _interaction_allowed(self, umo):
        return allowed(self.store.settings, umo)

    def _migrate_queued_commands(self):
        if self.store.data.get("commands_migrated"):
            return
        removed = 0
        for umo, queue in list(self.store.data["queues"].items()):
            try:
                config = self.context.get_config(umo)
            except Exception:
                config = {}
            messages = queue["messages"]
            queue["messages"] = [r for r in messages if not is_command_event(SimpleNamespace(message_str=r.get("text", "")), config)]
            removed += len(messages) - len(queue["messages"])
            if not queue["messages"]:
                self.store.data["queues"].pop(umo, None)
        self.store.data["commands_migrated"] = True
        if removed:
            self.store.log(f"升级时移除了 {removed} 条旧版误暂存的命令，其余留言保留")
        self.store.save()

    async def _persona(self, persona_id):
        item = await maybe_await(self.context.persona_manager.get_persona(persona_id))
        return self._persona_data(item, persona_id)

    async def _require_current_persona(self, persona):
        current = await self._persona(persona["id"])
        if (current["id"], current["key"]) != (persona["id"], persona["key"]):
            raise ValueError("人设已变化，已取消旧操作，请刷新后重试")

    def _persona_data(self, item, persona_id=""):
        if not item:
            raise ValueError("当前没有可用的人设")
        pid = str(field(item, "persona_id") or field(item, "name") or persona_id)
        prompt = str(field(item, "system_prompt") or field(item, "prompt") or "")
        return {"id": pid, "prompt": prompt, "key": digest({"id": pid, "prompt": prompt})}

    async def environment(self, umo, platform_name="", conversation=None):
        if not platform_name:
            getter = getattr(self.context, "get_platform_inst", None)
            platform = await maybe_await(getter(umo.split(":", 1)[0])) if callable(getter) else None
            if platform:
                platform_name = str(field(await maybe_await(platform.meta()), "name", "") or "")
        manager = self.context.conversation_manager
        cid = field(conversation, "cid")
        if conversation is None:
            cid = await maybe_await(manager.get_curr_conversation_id(umo))
            if cid:
                conversation = await maybe_await(manager.get_conversation(umo, cid))
        manager_persona = self.context.persona_manager
        persona_id = field(conversation, "persona_id")
        resolver = getattr(manager_persona, "resolve_selected_persona", None)
        if resolver:
            pid, item, _, _ = await maybe_await(resolver(
                umo=umo, conversation_persona_id=persona_id, platform_name=platform_name,
                provider_settings=self.context.get_config(umo).get("provider_settings", {})))
            if pid == "[%None]" or item is None:
                return None
            persona = self._persona_data(item, pid)
        else:
            # Per-session overrides take precedence over conversation/default.
            from astrbot.api import sp
            service = await sp.get_async(scope="umo", scope_id=umo, key="session_service_config", default={})
            persona_id = (service or {}).get("persona_id") or persona_id
            if persona_id == "[%None]":
                return None
            if persona_id is not None:
                persona = await self._persona(persona_id)
            else:
                persona = self._persona_data(await maybe_await(manager_persona.get_default_persona_v3(umo)))
        return {"umo": umo, "cid": cid or "", "persona": persona,
                "conversation": conversation, "platform": platform_name}

    def _record(self, persona):
        records = self.store.data["personas"]
        record = records.get(persona["id"])
        if not record or record["key"] != persona["key"]:
            record = {"key": persona["key"], "profile": None, "calendars": {}, "days": {}, "locked_days": {}}
            records[persona["id"]] = record
        record.setdefault("locked_days", {})
        return record

    def _preserve_locks(self, record, days):
        archive = record.setdefault("locked_days", {})
        for day, schedule in days.items():
            # Generated rows may inherit weekly locks. Only explicitly saved
            # dates should become independent date-specific overrides.
            if schedule.get("origin") != "manual":
                continue
            locks = [copy.deepcopy(row) for row in schedule["slots"] if row.get("locked")]
            if locks:
                archive[day] = locks
            else:
                archive.pop(day, None)
        for old in sorted(archive)[:-90]:
            archive.pop(old)

    async def ensure(self, persona, umo="", day="", *, force="", progress=None, automatic=False):
        def inactive():
            return automatic and not (self.store.settings["enabled"] and self.store.settings["auto_schedule_enabled"])
        def stage(name, percent, message):
            if progress:
                progress(name, percent, message)
        stage("waiting", 5, "等待角色档案生成锁")
        with self.generator.task_scope("schedule", background=automatic):
            return await self._ensure_plan(persona, umo, day, force=force, progress=progress, automatic=automatic)

    async def _ensure_plan(self, persona, umo="", day="", *, force="", progress=None, automatic=False):
        def inactive():
            return automatic and not (self.store.settings["enabled"] and self.store.settings["auto_schedule_enabled"])
        def stage(name, percent, message):
            if progress:
                progress(name, percent, message)
        if not force:
            if inactive():
                return None
            await self._require_current_persona(persona)
            if inactive():
                return None
            # Complete plans are immutable while a background rebuild waits on
            # the model. Chats can use that saved snapshot without waiting for
            # tomorrow's optional generation to release the persona write lock.
            cached = self.store.data["personas"].get(persona["id"], {})
            profile = cached.get("profile")
            if cached.get("key") == persona["key"] and profile:
                requested_day = day or datetime.now(ZoneInfo(profile["timezone"])).date().isoformat()
                year = str(date.fromisoformat(requested_day).year)
                calendar = cached.get("calendars", {}).get(year)
                schedule = cached.get("days", {}).get(requested_day)
                if (calendar is not None and schedule is not None
                        and apply_recurrences(calendar, cached.get("recurrences", []), int(year)) == calendar):
                    return profile, calendar, self.effective_schedule(persona, schedule)
        async with self._lock(self.persona_locks, persona["id"]):
            if inactive():
                return None
            await self._require_current_persona(persona)
            records = self.store.data["personas"]
            previous = records.get(persona["id"])
            fingerprint = digest(previous)
            async def check_generation():
                await self._require_current_persona(persona)
                if inactive():
                    raise ValueError("日程自动生成已关闭，已取消本次模型调用")
                if digest(records.get(persona["id"])) != fingerprint:
                    raise ValueError("生成期间日程已变化，已取消旧模型调用，请重试")

            original = previous if previous and previous.get("key") == persona["key"] else {}
            record = _record_for_edit(records, persona)
            record.setdefault("locked_days", {})
            changed = bool(force)
            if force == "profile":
                self._preserve_locks(record, record["days"])
                record.update(profile=None, calendars={}, days={})
            if record["profile"] is None:
                stage("profile", 15, "分析人设与每周生活节奏")
                async with self.generation_slots:
                    if inactive():
                        return None
                    record["profile"] = await self.generator.profile(persona, umo, before_generate=check_generation)
                old_weekly = (original.get("profile") or {}).get("weekly", [])
                record["profile"]["weekly"] = merge_locked_slots(record["profile"]["weekly"], old_weekly, weekly=True)
                changed = True
            profile = record["profile"]
            now = datetime.now(ZoneInfo(profile["timezone"]))
            day = day or now.date().isoformat()
            year = str(date.fromisoformat(day).year)
            if force == "calendar":
                self._preserve_locks(record, {k: v for k, v in record["days"].items() if k.startswith(year)})
                record["calendars"].pop(year, None)
                record["days"] = {k: v for k, v in record["days"].items() if not k.startswith(year)}
            if year not in record["calendars"]:
                stage("calendar", 40, "整理节庆、休假与世界观活动；已启用时检索参考资料")
                async with self.generation_slots:
                    if inactive():
                        return None
                    record["calendars"][year] = await self.generator.calendar(persona, profile, int(year), umo,
                                                                           before_generate=check_generation)
                changed = True
                # A preview must not evict the current calendar or hand edits.
                # Legacy saves may only identify manual ownership by revision
                # or event provenance, including a deliberately emptied year.
                protected_years = {str(now.year + offset) for offset in (-1, 0, 1)} | {year}
                for old in list(record["calendars"]):
                    calendar = record["calendars"][old]
                    manual = (calendar.get("origin") == "manual" or calendar.get("revision", 1) > 1
                              or any(row.get("origin") == "manual" for row in calendar.get("events", [])))
                    if old not in protected_years and not manual:
                        del record["calendars"][old]
            recurring_calendar = apply_recurrences(record["calendars"][year], record.get("recurrences", []), int(year))
            if recurring_calendar != record["calendars"][year]:
                recurring_calendar["revision"] = record["calendars"][year].get("revision", 0) + 1
                record["calendars"][year] = recurring_calendar
                cached = record["days"].get(day)
                if cached:
                    current_events = active_events(recurring_calendar["events"], day)
                    if cached.get("events", []) != current_events:
                        if cached.get("origin") == "manual":
                            cached.update(events=current_events, revision=cached.get("revision", 0) + 1)
                        else:
                            record["days"].pop(day)
                changed = True
            if force == "day":
                self._preserve_locks(record, {day: record["days"][day]} if day in record["days"] else {})
                record["days"].pop(day, None)
            if day not in record["days"]:
                stage("day", 70, "编排当天日程并保留锁定时段")
                recent = [{"date": k, "summary": v["summary"]} for k, v in sorted(record["days"].items()) if k < day][-3:]
                async with self.generation_slots:
                    if inactive():
                        return None
                    record["days"][day] = await self.generator.day(persona, profile, record["calendars"][year], day, recent, umo,
                                                                 before_generate=check_generation)
                record["days"][day]["revision"] = original.get("days", {}).get(day, {}).get("revision", 0) + 1
                # A date-specific lock takes precedence over the weekly lock.
                base = merge_locked_slots(weekly_for_day(profile, day), record["locked_days"].get(day, []))
                record["days"][day]["slots"] = merge_locked_slots(record["days"][day]["slots"], base)
                changed = True
                # Generated plans are a bounded cache. User-saved dates are
                # durable, and neither today's plan nor this request may be
                # discarded before the caller can use the returned result.
                generated = sorted(k for k, value in record["days"].items() if value.get("origin") != "manual")
                excess = max(0, len(generated) - 45)
                protected_days = {day, now.date().isoformat()}
                for old in generated:
                    if not excess:
                        break
                    if old not in protected_days:
                        del record["days"][old]
                        excess -= 1
            if changed:
                await self._require_current_persona(persona)
                if inactive():
                    return None
                if digest(records.get(persona["id"])) != fingerprint:
                    raise ValueError("生成期间日程已变化，已保留最新安排，请重试")
                # Commit all generated pieces together. A failed rebuild leaves
                # previously edited profiles/calendars/schedules intact.
                stage("saving", 95, "校验并保存完整日程")
                previous_incidents = copy.deepcopy(self.store.data.get("incidents", {}))
                previous_adjustments = copy.deepcopy(self.adjustments)
                if force:
                    self._cancel_persona_incidents(persona, "日程已重新生成", persist=False)
                    for origin, adjustment in list(self.adjustments.items()):
                        if adjustment.get("persona_key") == persona["key"]:
                            self.adjustments.pop(origin)
                records[persona["id"]] = record
                try:
                    self.store.save()
                except Exception:
                    if previous is None:
                        records.pop(persona["id"], None)
                    else:
                        records[persona["id"]] = previous
                    self.store.data["incidents"] = previous_incidents
                    self.adjustments.clear()
                    self.adjustments.update(previous_adjustments)
                    raise
            else:
                # Retain the stored object identity used by final delivery checks.
                record = previous
            return record["profile"], record["calendars"][year], self.effective_schedule(persona, record["days"][day])

    def _cached_schedule(self, persona, day, now=None):
        record = self.store.data["personas"].get(persona["id"], {})
        if record.get("key") != persona["key"]:
            return None
        base = record.get("days", {}).get(day)
        return self.effective_schedule(persona, base, now) if base else None

    def _schedule_plans(self, now):
        settings = self.store.settings
        if not settings["auto_schedule_enabled"]:
            return
        selected = settings["auto_schedule_personas"] or [pid for pid, record in self.store.data["personas"].items() if record.get("profile")]
        for pid in selected:
            if pid in self.plan_tasks and not self.plan_tasks[pid].done():
                continue
            if self.failures.get("plan:" + pid, 0) > now or any(j["persona_id"] == pid and j["status"] == "running" for j in self.jobs.values()):
                continue
            record = self.store.data["personas"].get(pid, {})
            tz = (record.get("profile") or {}).get("timezone", settings["timezone"])
            local = datetime.fromtimestamp(now, ZoneInfo(tz))
            if local.strftime("%H:%M") < settings["auto_schedule_time"] or local.date().isoformat() in record.get("days", {}):
                continue
            self.plan_tasks[pid] = self._spawn(self._automatic_plan(pid), "schedule-plan-" + digest(pid))

    async def _automatic_plan(self, pid):
        try:
            persona = await self._persona(pid)
            session = next((s for s in self.store.data["sessions"].values() if s["persona_id"] == pid), {})
            result = await self.ensure(persona, session.get("umo", ""), automatic=True)
            if result:
                self.store.log(f"已主动生成 {pid} 的 {result[2]['date']} 日程")
        except Exception as exc:
            self._report_failure("plan:" + pid, exc)

    async def get_proactive_context(self, umo):
        """Optional public integration for Persona Canvas; never queues a push.

        Read cached plans before its decision and immediately before send.
        The caller must cancel when allowed=False or context_key has changed.
        """
        settings = self.store.settings
        inactive = {"enabled": settings["canvas_integration_enabled"], "applicable": False,
                    "allowed": True, "status": "unavailable", "prompt": "", "reason": "联动未启用或会话不在日程控制范围", "context_key": ""}
        if not settings["canvas_integration_enabled"] or not allowed(settings, umo):
            return inactive
        unavailable = {**inactive, "applicable": True, "allowed": False, "reason": "当前日程不可用，已跳过这次主动联系"}
        try:
            session = self.store.data["sessions"].get(umo, {})
            env = await self.environment(umo, session.get("platform", ""))
            if not env:
                return {**inactive, "reason": "当前会话没有生效人设"}
            record = self.store.data["personas"].get(env["persona"]["id"], {})
            if record.get("key") != env["persona"]["key"] or not record.get("profile"):
                return unavailable
            profile = record["profile"]
            day = datetime.now(ZoneInfo(profile["timezone"])).date().isoformat()
            schedule = self._cached_schedule(env["persona"], day)
            if not schedule:
                return unavailable
            current = await self.environment(umo, session.get("platform", ""))
            if not current or current["persona"]["key"] != env["persona"]["key"] or current["cid"] != env["cid"]:
                return {**unavailable, "reason": "人设或对话已切换，已取消旧主动内容"}
            if not self.store.settings["canvas_integration_enabled"] or not allowed(self.store.settings, umo):
                return {**inactive, "reason": "联动设置已变化"}
            # environment() awaits the database. WebUI edits may have replaced
            # the plan in that gap, so read the latest snapshot after all awaits.
            record = self.store.data["personas"].get(env["persona"]["id"], {})
            if record.get("key") != env["persona"]["key"] or not record.get("profile"):
                return unavailable
            profile = record["profile"]
            now = datetime.now(ZoneInfo(profile["timezone"]))
            schedule = self._cached_schedule(env["persona"], now.date().isoformat(), now)
            if not schedule:
                return unavailable
            if schedule["date"] != now.date().isoformat():
                return unavailable
            slot, end = slot_at(schedule, now)
            effective = self._effective_slot(env, schedule, slot, now)
            if effective.get("adaptive", {}).get("action") == "defer":
                slot = effective
                # The drawing plugin needs the contact state, not a reason that
                # could paraphrase private details from the user's conversation.
                slot["adaptive"].pop("reason", None)
            data = {"persona_id": env["persona"]["id"], "persona_key": env["persona"]["key"], "cid": env["cid"],
                    "date": schedule["date"], "timezone": profile["timezone"], "summary": schedule["summary"],
                    "activity": slot, "events": schedule["events"]}
            return {**unavailable, "allowed": slot["status"] != "busy", "status": slot["status"],
                    "persona_id": env["persona"]["id"], "persona_key": env["persona"]["key"], "cid": env["cid"],
                    "date": schedule["date"], "context_key": digest(data), "activity_end": end,
                    "reason": "角色忙碌，取消本次主动联系" if slot["status"] == "busy" else "当前可主动联系",
                    "prompt": "\n[角色日程资料，仅作生活背景]\n" + json.dumps(data, ensure_ascii=False) +
                              "\n主动回复及照片的场景、服装、动作应符合当前活动和当天事件，不得声称正在做相冲突的事情。半忙时简短自然，不必逐条报告日程。"}
        except Exception as exc:
            self._report_failure("canvas:" + digest(umo), exc)
            return unavailable

    def _diary_source(self, persona, day):
        record = self.store.data["personas"].get(persona["id"], {})
        if record.get("key") != persona["key"]:
            raise ValueError("人设已变化，请先生成新日程；历史日记仍可查看")
        profile, schedule = record.get("profile"), self._cached_schedule(persona, day)
        if not profile or not schedule:
            archived = self.store.data["diaries"].get(persona["id"], {}).get(day, {})
            if archived.get("persona_key") == persona["key"]:
                source = archived.get("source", {})
                profile, schedule = source.get("profile"), source.get("schedule")
        if not profile or not schedule:
            raise ValueError("这一天没有已保存的日程，不能凭空补写日记")
        profile = {k: v for k, v in profile.items() if k != "weekly"}
        schedule = copy.deepcopy(schedule)
        archived = self.store.data["diaries"].get(persona["id"], {}).get(day, {})
        past = (archived.get("source", {}).get("schedule", {}).get("contact_adjustments", [])
                if archived.get("persona_key") == persona["key"] else [])
        # The runtime log rotates globally. Already archived, anonymous facts
        # must not disappear or mark a diary stale just because that log aged out.
        adjustments = {digest(item): item for item in past}
        for row in self.store.data["adaptation_log"]:
            if row.get("persona_id") == persona["id"] and row.get("date") == day and row.get("persona_key") == persona["key"]:
                item = adjustment_summary(row)
                adjustments[digest(item)] = item
        if adjustments:
            schedule["contact_adjustments"] = list(adjustments.values())[-20:]
        tz = schedule.get("timezone") or profile["timezone"]
        return copy.deepcopy({"profile": profile, "schedule": schedule,
                              "chat_context": select_materials(self.store, persona, day, tz)})

    def _check_diary_date(self, persona, day):
        self._year(day)
        source = self._diary_source(persona, day)
        tz = source["schedule"].get("timezone") or source["profile"]["timezone"]
        if day >= datetime.now(ZoneInfo(tz)).date().isoformat():
            raise ValueError("这一天尚未结束，请等角色当地日期结束后写日记")
        return source, tz

    async def ensure_diary(self, persona, day, umo="", *, force=False, automatic=False, progress=None):
        with self.generator.task_scope("diary", background=automatic):
            return await self._ensure_diary(persona, day, umo, force=force, automatic=automatic, progress=progress)

    async def _ensure_diary(self, persona, day, umo="", *, force=False, automatic=False, progress=None):
        # A separate lock avoids duplicate automatic/manual writes. Snapshot and
        # commit hold the persona lock briefly; a diary never blocks chat LLMs.
        async with self._lock(self.diary_locks, persona["id"]):
            if automatic and not (self.store.settings["enabled"] and self.store.settings["diary_enabled"]):
                return None
            entries = self.store.data["diaries"].setdefault(persona["id"], {})
            previous = entries.get(day)
            if previous and not force:
                return previous
            async with self._lock(self.persona_locks, persona["id"]):
                source, tz = self._check_diary_date(persona, day)
            if progress:
                progress("diary", 40, "回顾已结束的日程与白名单对话，按角色语气写日记")
            async def check_source():
                await self._require_current_persona(persona)
                if automatic and not (self.store.settings["enabled"] and self.store.settings["diary_enabled"]):
                    raise ValueError("日记自动生成已关闭，已取消本次模型调用")
                if digest(self._diary_source(persona, day)) != digest(source):
                    raise ValueError("生成期间日程或白名单聊天素材已变化，请重试")

            recent = [{"date": d, "title": e["title"], "content": e["content"][:1200]}
                      for d, e in sorted(entries.items()) if d < day and e.get("persona_key") == persona["key"]][-2:]
            async with self.generation_slots:
                if automatic and not (self.store.settings["enabled"] and self.store.settings["diary_enabled"]):
                    return None
                await check_source()
                diary_day = {**source["schedule"], "conversation_material": source["chat_context"]}
                value = await self.generator.diary(persona, source["profile"], diary_day, recent, umo,
                                                   before_generate=check_source)
            async with self._lock(self.persona_locks, persona["id"]):
                if automatic and not (self.store.settings["enabled"] and self.store.settings["diary_enabled"]):
                    return None
                current_persona = await self._persona(persona["id"])
                if current_persona["key"] != persona["key"] or digest(self._diary_source(persona, day)) != digest(source):
                    raise ValueError("生成期间人设或当天日程已变化，已保留旧日记，请重试")
                if progress:
                    progress("saving", 95, "归档日记与当天日程快照")
                now = time.time()
                archived_source = copy.deepcopy(source)
                # Raw conversations have a short, bounded retention. The archive
                # records counts and schedule evidence, never the sampled text.
                archived_source["chat_context"] = material_summary(source["chat_context"])
                entry = {**value, "date": day, "timezone": tz, "persona_key": persona["key"],
                         "created_at": previous["created_at"] if previous else now, "updated_at": now,
                         "revision": previous.get("revision", 0) + 1 if previous else 1, "source": archived_source}
                entry["source_tracking"] = self._diary_tracking(persona, day, source)
                saved_entries, saved_logs = dict(entries), list(self.store.data["logs"])
                try:
                    entries[day] = entry
                    for old in sorted(entries)[:-self.store.settings["diary_limit"]]:
                        del entries[old]
                    self.store.log(f"已归档 {persona['id']} 的 {day} 日记")
                except Exception:
                    entries.clear()
                    entries.update(saved_entries)
                    self.store.data["logs"] = saved_logs
                    raise
                return entry

    def _schedule_diaries(self, now):
        if not self.store.settings["diary_enabled"]:
            return
        for pid, record in list(self.store.data["personas"].items()):
            if pid in self.diary_tasks and not self.diary_tasks[pid].done():
                continue
            if not record.get("profile") or any(j["persona_id"] == pid and j["status"] == "running" for j in self.jobs.values()):
                continue
            entries = self.store.data["diaries"].get(pid, {})
            # Catch up recent saved days after downtime, one per persona/tick.
            # Missing plans are never generated retrospectively for this task.
            pending = []
            for d, schedule in sorted(record["days"].items(), reverse=True):
                tz = schedule.get("timezone") or record["profile"]["timezone"]
                today = datetime.fromtimestamp(now, ZoneInfo(tz)).date()
                ended_at = datetime.combine(date.fromisoformat(d) + timedelta(days=1), datetime.min.time(), ZoneInfo(tz)).timestamp()
                if ((today - timedelta(days=7)).isoformat() <= d < today.isoformat()
                        and now >= ended_at + self.store.settings["diary_archive_delay_minutes"] * 60
                        and d not in entries and self.failures.get(f"diary:{pid}:{d}", 0) <= now):
                    pending.append(d)
            if pending:
                self.diary_tasks[pid] = self._spawn(self._automatic_diary(pid, pending[0]), "schedule-diary-" + digest(pid))

    async def _automatic_diary(self, pid, day):
        try:
            persona = await self._persona(pid)
            session = next((s for s in self.store.data["sessions"].values() if s["persona_id"] == pid), {})
            await self.ensure_diary(persona, day, session.get("umo", ""), automatic=True)
        except Exception as exc:
            self._report_failure(f"diary:{pid}:{day}", exc)

    def _remember_session(self, env, title=""):
        sessions = self.store.data["sessions"]
        previous = sessions.get(env["umo"], {})
        sessions[env["umo"]] = {"umo": env["umo"], "cid": env["cid"], "persona_id": env["persona"]["id"],
            "platform": env["platform"], "title": title or previous.get("title", env["umo"]), "last_seen": time.time()}
        # Message materials for diaries are stored separately with short bounds.
        if len(sessions) > 2000:
            oldest = min(sessions, key=lambda u: sessions[u]["last_seen"])
            sessions.pop(oldest)
        self.store.save()

    def _control(self, event):
        try:
            config = self.context.get_config(event.unified_msg_origin)
        except Exception:
            config = {}
        return is_command_event(event, config)

    def _report_failure(self, key, exc):
        # Provider exceptions may contain credentials; expose only the type.
        now = time.time()
        if self.failures.get(key, 0) < now:
            self.failures[key] = now + 300
            operation = "日记归档" if key.startswith("diary:") else "主动日程生成" if key.startswith("plan:") else "日程处理"
            if isinstance(exc, BudgetExceeded):
                local = datetime.fromtimestamp(now, ZoneInfo(self.store.settings["timezone"]))
                self.failures[key] = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                message = f"{operation}已跳过：今日后台模型预算已用完，保留现有安排，次日再检查"
            else:
                message = f"{operation}失败（{type(exc).__name__}），5 分钟后重试；请检查模型和配置"
            self.store.log(message)
            logger.warning("[角色日程] %s", message)

    async def _intercept(self, event, conversation=None):
        umo = event.unified_msg_origin
        event.set_extra(NAME + ":checked_context", None)
        if not allowed(self.store.settings, umo) or self._control(event):
            return None
        result = getattr(event, "get_result", None)
        if getattr(event, "_has_send_oper", False) or callable(result) and result() is not None:
            return None
        if event.get_extra(NAME + ":queued", False):
            event.stop_event()
            return None
        if self.failures.get(umo, 0) > time.time():
            return None
        try:
            env = await self.environment(umo, event.get_platform_name(), conversation)
            if not env:
                return None
            self._remember_session(env, event.get_sender_name())
            profile, _, schedule = await self.ensure(env["persona"], umo)
            if not allowed(self.store.settings, umo):
                return None
            now = datetime.now(ZoneInfo(profile["timezone"]))
            await self._adaptive_interaction(event, env, profile, schedule, now)
            record = self.store.data["personas"].get(env["persona"]["id"], {})
            if record.get("key") != env["persona"]["key"] or not allowed(self.store.settings, umo):
                return None
            profile = record.get("profile") or profile
            now = datetime.now(ZoneInfo(profile["timezone"]))
            schedule = self._cached_schedule(env["persona"], now.date().isoformat(), now)
            if schedule is None:
                return None
            slot, end = slot_at(schedule, now)
            slot = self._effective_slot(env, schedule, slot, now)
            queue = self.store.data["queues"].get(umo)
            if slot["status"] == "free" and queue is None:
                event.set_extra(NAME + ":checked_context", {"umo": umo, "cid": env["cid"],
                    "persona_id": env["persona"]["id"], "persona_key": env["persona"]["key"]})
                return schedule, slot
            self._queue_schedule_message(event, env, slot, end)
            return None
        except Exception as exc:
            self._report_failure(umo, exc)
            # A missing model/bad profile must not accidentally silence a bot.
            return None

    def _queue_schedule_message(self, event, env, slot, end):
        if slot["status"] == "busy":
            due = end
        elif slot["status"] == "slow":
            due = time.time() + random.uniform(*slow_bounds(slot, self.store.settings))
        else:
            due = time.time() + 2
        message = event.message_str.strip() or "[非文本消息，附件内容未解析]"
        self.store.enqueue(env["umo"], env["cid"], env["persona"]["key"], event.get_sender_name(),
                           message, time.time(), due, slot["status"], slot["title"])
        event.set_extra(NAME + ":queued", True)
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-100)
    async def intercept_message(self, event: AstrMessageEvent):
        if not (event.is_private_chat() or event.is_at_or_wake_command):
            return
        await self._intercept(event)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-90)
    async def capture_diary_input(self, event: AstrMessageEvent):
        """Record only text addressed to the bot in an explicit diary whitelist."""
        if (not chat_enabled(self.store.settings, event.unified_msg_origin)
                or event.get_extra(NAME + ":diary_capture", None) or self._control(event)
                or not (event.is_private_chat() or event.is_at_or_wake_command)):
            return
        value = getattr(event, "message_str", "")
        if not isinstance(value, str) or not value.strip():
            return
        received_at = time.time()
        try:
            env = await self.environment(event.unified_msg_origin, event.get_platform_name())
            if not env:
                return
            message_id = field(getattr(event, "message_obj", None), "message_id", "")
            token = str(message_id) if message_id else uuid.uuid4().hex
            identity = {"umo": env["umo"], "cid": env["cid"], "persona": {k: env["persona"][k] for k in ("id", "key")}}
            if add_material(self.store, identity, "user", value, received_at, token):
                event.set_extra(NAME + ":diary_capture", {"env": identity, "token": token})
        except Exception as exc:
            logger.warning("[角色日程] 日记文本采集失败（%s），聊天继续", type(exc).__name__)

    @filter.on_llm_response(priority=-100)
    async def capture_diary_response(self, event: AstrMessageEvent, response):
        marker = event.get_extra(NAME + ":diary_capture", None)
        value = field(response, "completion_text", "")
        if (not marker or not chat_enabled(self.store.settings, event.unified_msg_origin)
                or field(response, "role", "assistant") != "assistant"
                or field(response, "tools_call_name", None) or field(response, "tool_calls", None)):
            return
        if not isinstance(value, str) or not value.strip():
            return
        generated_at = time.time()
        try:
            env = await self.environment(event.unified_msg_origin, event.get_platform_name())
            original = marker["env"]
            if (not env or (env["umo"], env["cid"], env["persona"]["key"], env["persona"]["id"])
                    != (original["umo"], original["cid"], original["persona"]["key"], original["persona"]["id"])):
                return
            add_material(self.store, original, "assistant", value, generated_at, marker["token"] + ":" + digest(value))
        except Exception as exc:
            logger.warning("[角色日程] 日记回复采集失败（%s），聊天继续", type(exc).__name__)

    async def _bind_diary_request(self, event, req):
        marker = event.get_extra(NAME + ":diary_capture", None)
        conversation = getattr(req, "conversation", None)
        if (not marker or marker["env"]["cid"] or not field(conversation, "cid")
                or not chat_enabled(self.store.settings, event.unified_msg_origin)):
            return
        try:
            requested = await self.environment(event.unified_msg_origin, event.get_platform_name(), conversation)
            current = await self.environment(event.unified_msg_origin, event.get_platform_name())
            if (not requested or not current
                    or (requested["cid"], requested["persona"]["key"])
                    != (current["cid"], current["persona"]["key"])):
                return
            if bind_initial_material(self.store, marker["env"], current, marker["token"]):
                marker["env"] = {"umo": current["umo"], "cid": current["cid"],
                                 "persona": {k: current["persona"][k] for k in ("id", "key")}}
        except Exception as exc:
            logger.warning("[角色日程] 日记首次会话绑定失败（%s），聊天继续", type(exc).__name__)

    @filter.on_llm_request(priority=-100)
    async def inject_schedule(self, event: AstrMessageEvent, req):
        await self.capture_diary_input(event)
        await self._bind_diary_request(event, req)
        # Only replace our own transient block if the same request is checked
        # again. Preserve the user's text, history and other plugins' parts.
        previous = getattr(req, "_persona_schedule_content_part", None)
        if previous is not None:
            req.extra_user_content_parts = [part for part in (getattr(req, "extra_user_content_parts", None) or []) if part is not previous]
            req._persona_schedule_content_part = None
        reservation = getattr(req, "_persona_schedule_notice_reservation", None)

        def clear_request():
            self._release_notice_reservation(reservation)
            req._persona_schedule_notice_reservation = None
            event.set_extra(NAME + ":checked_context", None)

        # Recheck at the actual LLM boundary, including plugins which wake later.
        info = await self._intercept(event, getattr(req, "conversation", None))
        checked = event.get_extra(NAME + ":checked_context", None)
        if not info or not checked:
            clear_request()
            return
        # Re-resolve the current session, then use only its latest cached plan.
        # No await follows the final snapshot read and the busy/slow decision.
        env = await self.environment(event.unified_msg_origin, event.get_platform_name())
        identity = (event.unified_msg_origin, checked["cid"], checked["persona_key"])
        if (not env or (env["umo"], env["cid"], env["persona"]["key"]) != identity
                or env["persona"]["id"] != checked["persona_id"]):
            clear_request()
            event.stop_event()  # Never hand an old request to a new identity.
            return
        if not allowed(self.store.settings, env["umo"]) or self._control(event):
            clear_request()
            return
        record = self.store.data["personas"].get(env["persona"]["id"], {})
        profile = record.get("profile")
        if record.get("key") != checked["persona_key"] or not profile:
            clear_request()
            event.stop_event()
            return
        now = datetime.now(ZoneInfo(profile["timezone"]))
        schedule = self._cached_schedule(env["persona"], now.date().isoformat(), now)
        if schedule is None:
            clear_request()
            event.stop_event()
            return
        slot, end = slot_at(schedule, now)
        slot = self._effective_slot(env, schedule, slot, now)
        if slot["status"] != "free" or self.store.data["queues"].get(env["umo"]):
            clear_request()
            self._queue_schedule_message(event, env, slot, end)
            return
        if not isinstance(reservation, dict) or reservation.get("identity") != identity:
            self._release_notice_reservation(reservation)
            reservation = {"identity": identity, "owner": uuid.uuid4().hex, "tokens": set()}
        req._persona_schedule_notice_reservation = reservation
        from astrbot.core.agent.message import TextPart
        part = TextPart(text=self._life_context(env, schedule, slot, now,
            notice_reservation=reservation)).mark_as_temp()
        if getattr(req, "extra_user_content_parts", None) is None:
            req.extra_user_content_parts = []
        req.extra_user_content_parts.append(part)
        req._persona_schedule_content_part = part

    async def _run(self):
        while not self.closing:
            try:
                await self.tick()
                self.worker_error = ""
            except Exception as exc:
                self.worker_error = type(exc).__name__
                self._report_failure("worker", exc)
            await asyncio.sleep(self.store.settings["tick_seconds"])

    async def tick(self):
        # Work per session concurrently so one slow model does not stall everyone.
        now = time.time()
        self.last_tick = now
        if prune_materials(self.store, now):
            self.store.save()
        if self.store.prune_expired(now):
            self.store.log("已逐条清理超过保留期限的留言，保留期限内的新留言")
        self._schedule_incidents(now)
        if not self.store.settings["enabled"]:
            return
        self._schedule_plans(now)
        self._schedule_diaries(now)
        for umo, session in list(self.store.data["sessions"].items()):
            if not allowed(self.store.settings, umo) or self.failures.get(umo, 0) > now:
                continue
            lock = self._lock(self.session_locks, umo)
            if not lock.locked():
                # Lock is acquired by this coroutine before the next tick.
                self._spawn(self._session_tick(umo, session), "schedule-session-" + digest(umo))

    async def _session_tick(self, umo, session):
        async with self._lock(self.session_locks, umo):
            if not allowed(self.store.settings, umo):
                return
            try:
                self.store.prune_expired(time.time(), umo)
                queue = self.store.data["queues"].get(umo)
                env = await self.environment(umo, session["platform"])
                if not env:
                    if queue:
                        queue.update(delivery="paused", reason="当前未启用人设")
                        self.store.save()
                    return
                # Persist the resolved current persona so a switch takes effect.
                session["persona_id"] = env["persona"]["id"]
                session["cid"] = env["cid"]
                if queue and (queue["persona_key"] != env["persona"]["key"] or queue["cid"] != env["cid"]):
                    queue.update(delivery="paused", reason="对话或人设已切换，请清除旧队列")
                    self.store.save()
                    return
                if not queue:
                    record = self.store.data["personas"].get(env["persona"]["id"], {})
                    profile = record.get("profile")
                    if profile and record.get("key") == env["persona"]["key"]:
                        day = datetime.now(ZoneInfo(profile["timezone"])).date().isoformat()
                        schedule = self._cached_schedule(env["persona"], day)
                        if schedule:
                            await self._maybe_transition_notice(env, profile, schedule)
                    return
                if queue["delivery"] in {"uncertain", "paused", "sending"}:
                    return
                profile, _, schedule = await self.ensure(env["persona"], umo)
                if queue.get("retry_after", 0) > time.time():
                    return
                now = datetime.now(ZoneInfo(profile["timezone"]))
                slot, end = slot_at(schedule, now)
                slot = self._effective_slot(env, schedule, slot, now)
                if slot["status"] == "busy":
                    queue["due"] = end
                    self.store.save()
                    return
                if slot["status"] == "free":
                    queue["due"] = min(queue["due"], time.time())
                if queue["due"] > time.time():
                    return
                await self._deliver(env, profile, schedule, slot, queue)
            except Exception as exc:
                if self.health.get(umo, {}).get("status") == "generating":
                    self._send_health(umo, "failed", error=type(exc).__name__, verified=False)
                self._report_failure(umo, exc)

    async def _deliver(self, env, profile, schedule, slot, queue):
        umo = env["umo"]
        messages = copy.deepcopy(queue["messages"])
        ids = {r["id"] for r in messages}
        history = field(env["conversation"], "history", "[]") or []
        if isinstance(history, str):
            history = json.loads(history)
        contexts = history[-12:] if isinstance(history, list) else []
        self._send_health(umo, "generating", verified=False)
        try:
            async with self.generation_slots:
                reply = (await self.generator.reply(env["persona"], messages, schedule, slot, umo, contexts)).strip()
        except Exception as exc:
            self._send_health(umo, "failed", error=type(exc).__name__, verified=False)
            raise
        if not reply:
            self._send_health(umo, "failed", error="模型未生成有效补回消息", verified=False)
            raise ValueError("模型未生成补回消息")
        reply = reply[:1200]
        # Recheck permissions, persona, conversation and time *after* model wait.
        self.store.prune_expired(time.time(), umo)
        current = await self.environment(umo, env["platform"])
        if not allowed(self.store.settings, umo) or self.store.data["queues"].get(umo) is not queue:
            self._send_health(umo, "deferred")
            return
        if not ids.issubset({row["id"] for row in queue["messages"]}):
            self._send_health(umo, "deferred")
            return
        if not current or current["cid"] != env["cid"] or current["persona"]["key"] != env["persona"]["key"]:
            queue.update(delivery="paused", reason="生成回复期间切换了对话或人设")
            self._send_health(umo, "paused")
            self.store.save()
            return
        record = self._record(current["persona"])
        current_profile = record["profile"]
        if current_profile is None:
            self._send_health(umo, "deferred")
            return
        now = datetime.now(ZoneInfo(current_profile["timezone"]))
        current_schedule = self._cached_schedule(current["persona"], now.date().isoformat(), now)
        if current_schedule is None:
            self._send_health(umo, "deferred")
            return
        current_slot, end = slot_at(current_schedule, now)
        current_slot = self._effective_slot(current, current_schedule, current_slot, now)
        if current_slot["status"] == "busy":
            queue["due"] = end
            self._send_health(umo, "deferred")
            self.store.save()
            return
        # Edits or a boundary crossing can invalidate the reply's context.
        if digest(current_schedule) != digest(schedule) or current_slot != slot or queue["due"] > time.time():
            self._send_health(umo, "deferred")
            return
        queue["delivery"] = "sending"
        self._send_health(umo, "sending")
        self.store.save()
        try:
            sent = await self.context.send_message(umo, MessageChain().message(reply))
        except Exception as exc:
            queue.update(delivery="uncertain", reason="平台发送发生异常，请核对是否送达；不会自动重复发送")
            self._send_health(umo, "uncertain", error=type(exc).__name__, verified=False)
            self.store.save()
            return
        if sent is False:
            queue.update(delivery="pending", due=time.time() + 300, retry_after=time.time() + 300,
                         reason="平台未匹配，5 分钟后重试")
            self._send_health(umo, "unmatched", error="未匹配可主动发送的平台", verified=False)
            self.store.save()
            return
        self._send_health(umo, "accepted", verified=sent is True, sent_at=time.time())
        try:
            add_material(self.store, env, "assistant", reply, time.time(), "aggregate:" + digest(sorted(ids)))
        except Exception as exc:
            logger.warning("[角色日程] 补回日记采集失败（%s），继续完成已发送批次", type(exc).__name__)
        self.store.finish_batch(umo, ids)
        # A history write failure must never trigger another platform send.
        try:
            manager = self.context.conversation_manager
            if env["cid"]:
                combined = "\n".join(f'{r["sender"]}: {r["text"]}' for r in messages)
                await maybe_await(manager.add_message_pair(env["cid"],
                    {"role": "user", "content": combined}, {"role": "assistant", "content": reply}))
        except Exception:
            self.store.log("补回消息已发送，但未能写入 AstrBot 对话历史")
        self.store.log("已合并补回一条消息")

    def _send_health(self, umo, status, *, error="", verified=None, sent_at=None):
        health = self.health.setdefault(umo, {"last_send_at": None, "verified": False})
        health.update(status=status, error=error)
        if verified is not None:
            health["verified"] = verified
        if sent_at is not None:
            health["last_send_at"] = sent_at
        # Bound diagnostics to the same session scale; no message content here.
        if len(self.health) > 2000:
            self.health.pop(next(iter(self.health)))
        self.store.save()

    @filter.command("日程")
    async def schedule_command(self, event: AstrMessageEvent):
        """查看当前角色的日程；插件关闭时也可预览。"""
        try:
            env = await self.environment(event.unified_msg_origin, event.get_platform_name())
            if not env:
                yield event.plain_result("当前对话没有启用人设，请先选择人设。")
                return
            self._remember_session(env, event.get_sender_name())
            profile, _, schedule = await self.ensure(env["persona"], env["umo"])
            slot, _ = slot_at(schedule, datetime.now(ZoneInfo(profile["timezone"])))
            lines = [f'{env["persona"]["id"]} · {schedule["date"]} ({profile["timezone"]})',
                     f'现在：{slot["title"]} · {LABELS[slot["status"]]}', schedule["summary"]]
            lines += [f'{r["start"]}–{r["end"]} {LABELS[r["status"]]} · {r["title"]}' for r in schedule["slots"]]
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            self._report_failure(event.unified_msg_origin, exc)
            yield event.plain_result("日程生成失败，请在插件 WebUI 查看设置或重试。")
        finally:
            event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("日程重建")
    async def rebuild_command(self, event: AstrMessageEvent):
        """管理员：重建当前角色今天的日程。"""
        try:
            env = await self.environment(event.unified_msg_origin, event.get_platform_name())
            if env:
                await self.ensure(env["persona"], env["umo"], force="day")
                yield event.plain_result("已重建今日日程，发送 /日程 查看。")
        except Exception as exc:
            self._report_failure(event.unified_msg_origin, exc)
            yield event.plain_result("重建失败，请检查模型配置。")
        finally:
            event.stop_event()

    @filter.command("日程会话")
    async def session_command(self, event: AstrMessageEvent):
        """查看当前会话来源 ID，供白名单选择。"""
        yield event.plain_result("会话来源 ID：" + event.unified_msg_origin + "\n可在插件 WebUI 的对话白名单中选择。")
        event.stop_event()

    def _register_web(self):
        self.web_available = False
        if not hasattr(self.context, "register_web_api"):
            logger.warning("[角色日程] 当前 AstrBot 无插件视图 API，请更新 AstrBot 与管理面板")
            return
        routes = {"state": ("GET", self.web_state), "settings": ("POST", self.web_settings),
                  "detail": ("GET", self.web_detail), "generate": ("POST", self.web_generate),
                  "job": ("GET", self.web_job), "save": ("POST", self.web_save),
                  "queue": ("POST", self.web_queue), "providers": ("GET", self.web_providers),
                  "diagnostics": ("GET", self.web_diagnostics), "diary": ("GET", self.web_diary),
                  "preview": ("POST", self.web_preview), "history": ("GET", self.web_history),
                  "restore": ("POST", self.web_restore), "recurrences": ("GET", self.web_recurrences),
                  "recurrences-save": ("POST", self.web_save_recurrences),
                  "incidents": ("GET", self.web_incidents), "incident-end": ("POST", self.web_incident_end),
                  "backups": ("GET", self.web_backups), "backup-create": ("POST", self.web_backup_create),
                  "export": ("GET", self.web_export), "import-preview": ("POST", self.web_import_preview),
                  "import-apply": ("POST", self.web_import_apply)}
        for path, (method, handler) in routes.items():
            self.context.register_web_api(f"/{NAME}/{path}", self._web_handler(handler, method), [method], "角色日程 · " + path)
        self.web_available = True

    def _web_handler(self, handler, method):
        async def route(**_kwargs):
            try:
                from astrbot.api.web import error_response, json_response, request
                payload = await request.json(default={}) if method == "POST" else dict(request.query)
            except ImportError:
                from quart import jsonify as json_response, request
                error_response = lambda message, status_code=400: (json_response({"status": "error", "message": message}), status_code)
                payload = await request.get_json(silent=True) if method == "POST" else dict(request.args)
            try:
                if not isinstance(payload, dict):
                    raise ValueError("请求应为 JSON 对象")
                return json_response(await handler(payload))
            except EditConflict as exc:
                return error_response(str(exc), status_code=409)
            except (ValueError, KeyError, TypeError) as exc:
                # These validators only contain local diagnostic text.
                return error_response(str(exc)[:500], status_code=400)
            except Exception as exc:
                self._report_failure("web", exc)
                return error_response("操作失败，请检查配置和 AstrBot 日志", status_code=500)
        route.__name__ = handler.__name__ + "_route"
        return route

    async def _providers(self):
        getter = getattr(self.context, "get_all_providers", None)
        if not getter:
            return []
        providers, seen = [], set()
        for provider in await maybe_await(getter()) or []:
            try:
                if not await is_chat_provider(provider):
                    continue
                meta = await maybe_await(provider.meta())
                pid = str(field(meta, "id", "") or "")
                if not pid or pid in seen:
                    continue
                model_getter = getattr(provider, "get_model", None)
                model = await maybe_await(model_getter()) if model_getter else field(meta, "model", "")
                model = str(model or "")[:300]
                providers.append({"id": pid, "name": f"{pid} · {model}" if model and model != pid else pid,
                                  "model": model, "type": "chat"})
                seen.add(pid)
            except Exception:
                # Never return provider config, endpoints, credentials or errors.
                continue
        return providers

    async def web_providers(self, payload):
        return {"providers": await self._providers(), "checked_at": time.time()}

    def _worker_status(self):
        state = "stopped" if self.closing else "running" if self.worker and not self.worker.done() else "not_started"
        return {"state": state, "last_tick": self.last_tick or None, "last_error": self.worker_error}

    def _persona_runtime(self, persona_id, persona_key=None):
        record = self.store.data["personas"].get(persona_id, {})
        if persona_key is not None and record.get("key") != persona_key:
            record = {}
        profile = record.get("profile")
        result = {"next_contact_at": None, "current_status": "unknown", "current_activity": "尚未生成当天日程",
                  "queue_count": sum(len(q["messages"]) for q in self.store.data["queues"].values()
                      if record.get("key") == q["persona_key"]),
                  "slow_min_seconds": self.store.settings["slow_min_seconds"],
                  "slow_max_seconds": self.store.settings["slow_max_seconds"]}
        if not profile:
            return result
        now = datetime.now(ZoneInfo(profile["timezone"]))
        persona = {"id": persona_id, "key": record.get("key")}
        schedule = self._cached_schedule(persona, now.date().isoformat(), now)
        if not schedule:
            return result
        slot, _ = slot_at(schedule, now)
        low, high = slow_bounds(slot, self.store.settings)
        result.update(current_status=slot["status"], current_activity=slot["title"],
                      slow_min_seconds=low, slow_max_seconds=high)
        if slot["status"] != "busy":
            result["next_contact_at"] = now.timestamp()
            return result
        # Scan consecutive busy slots and cached following days. Do not pretend
        # a future weekly template is the actual, possibly holiday-specific day.
        for offset in range(46):
            day = now.date() + timedelta(days=offset)
            cached = self._cached_schedule(persona, day.isoformat(), now)
            if not cached:
                break
            for row in cached["slots"]:
                if offset == 0 and minute(row["end"], end=True) <= now.hour * 60 + now.minute:
                    continue
                if row["status"] != "busy":
                    start = datetime.combine(day, datetime.min.time(), tzinfo=now.tzinfo) + timedelta(minutes=minute(row["start"]))
                    result["next_contact_at"] = max(start.timestamp(), now.timestamp())
                    return result
        return result

    async def web_diagnostics(self, payload):
        target = str(payload.get("umo", ""))
        if target and (len(target) > 300 or target.count(":") < 2):
            raise ValueError("请选择有效的会话来源 ID")
        sessions = list(self.store.data["sessions"].values())
        if target:
            sessions = [self.store.data["sessions"].get(target, {"umo": target, "platform": "", "title": target})]
        rows = []
        for session in sessions[:50]:
            umo = session["umo"]
            health = self.health.get(umo, {})
            row = {"umo": umo, "title": session.get("title") or umo, "platform": session.get("platform", ""),
                   "allowed": allowed(self.store.settings, umo), "persona_id": "", "cid": "",
                   "schedule_provider_id": "", "reply_provider_id": "", "send_status": health.get("status", "unverified"),
                   "last_send_at": health.get("last_send_at"), "last_send_error": health.get("error", ""),
                   "active_send_verified": bool(health.get("verified"))}
            try:
                env = await self.environment(umo, row["platform"])
                if env:
                    row.update(persona_id=env["persona"]["id"], cid=env["cid"])
                    row.update(self._persona_runtime(env["persona"]["id"], env["persona"]["key"]))
                else:
                    row["error"] = "当前会话没有启用人设"
                for purpose in ("schedule", "reply"):
                    try:
                        row[purpose + "_provider_id"] = await self.generator.provider_id(umo, purpose=purpose)
                    except Exception as exc:
                        row[purpose + "_model_error"] = type(exc).__name__
            except Exception as exc:
                row["error"] = "会话解析失败（" + type(exc).__name__ + "）"
            rows.append(row)
        settings = self.store.settings
        return {"worker": self._worker_status(), "web_available": self.web_available,
                "model_runtime": self.generator.runtime.snapshot(), "storage_metrics": self.store.storage_metrics(),
                "models": {key: settings[key] or (settings["schedule_provider_id"] if key == "diary_provider_id" else "") or settings["provider_id"]
                           for key in ("schedule_provider_id", "reply_provider_id", "diary_provider_id")},
                "sessions": rows, "truncated": len(sessions) > 50, "checked_at": time.time(),
                "integration_mode": "live" if type(self.context).__module__.startswith("astrbot.") else "unknown"}

    async def web_state(self, payload):
        personas = await maybe_await(self.context.persona_manager.get_all_personas())
        personas = [self._persona_data(p) for p in personas]
        options = []
        for persona in personas:
            record = self.store.data["personas"].get(persona["id"], {})
            options.append({"id": persona["id"], "ready": record.get("key") == persona["key"] and bool(record.get("profile"))})
        sessions = dict(self.store.data["sessions"])
        manager = self.context.conversation_manager
        getter = getattr(manager, "get_filtered_conversations", None)
        if getter:
            convs, _ = await maybe_await(getter(page=1, page_size=500, include_history=False))
        else:
            convs = await maybe_await(manager.get_conversations())
        for conv in convs:
            umo = str(field(conv, "user_id", ""))
            if umo.count(":") >= 2 and umo not in sessions:
                sessions[umo] = {"umo": umo, "title": field(conv, "title") or umo, "persona_id": field(conv, "persona_id") or "", "cid": field(conv, "cid"), "platform": ""}
        settings = copy.deepcopy(self.store.settings)
        settings["has_tavily_api_key"] = bool(settings.pop("tavily_api_key"))
        queues = [{"umo": u, "count": len(q["messages"]), "dropped": q["dropped"], "expired": q.get("expired", 0), "due": q["due"],
                   "persona_id": next((pid for pid, record in self.store.data["personas"].items() if record["key"] == q["persona_key"]), ""),
                   "send_status": self.health.get(u, {}).get("status", "unverified"),
                   "delivery": q["delivery"], "reason": q["reason"]} for u, q in self.store.data["queues"].items()]
        return {"settings": settings, "personas": options, "sessions": list(sessions.values()),
                "model_runtime": self.generator.runtime.snapshot(), "storage_metrics": self.store.storage_metrics(),
                "adaptation_log": [{k: v for k, v in row.items() if k not in {"persona_key", "source_slot"}}
                                   for row in self.store.data["adaptation_log"][-60:]],
                "providers": await self._providers(), "queues": queues, "logs": self.store.data["logs"][-30:],
                "expired_messages": self.store.data.get("expired_messages", 0), "jobs": list(self.jobs.values()),
                "runtime": {"worker": self._worker_status(), "checked_at": time.time(),
                            "personas": {item["id"]: self._persona_runtime(item["id"], item["key"]) for item in personas}}}

    async def web_settings(self, payload):
        if "schedule_provider_id" in payload or "reply_provider_id" in payload:
            # Clearing a new dropdown means follow the session, including after
            # upgrading an old shared-model configuration.
            payload = {**payload, "provider_id": ""}
        settings = validate_settings(payload, self.store.settings)
        previous = copy.deepcopy(dict(self.config))
        self.config.update(settings)
        try:
            await maybe_await(self.config.save_config())
        except Exception:
            self.config.clear()
            self.config.update(previous)
            raise
        self.store.data["settings"] = settings
        if payload.get("enabled") is True and self.store.data.get("recovery_info"):
            self.store.data["recovery_info"]["requires_review"] = False
        prune_materials(self.store, time.time())
        if not settings["adaptive_enabled"]:
            self.adjustments.clear()
        if not settings["enabled"] or not settings["incident_enabled"]:
            for pid, record in self.store.data["personas"].items():
                self._cancel_persona_incidents({"id": pid, "key": record["key"]}, "突发功能已关闭", persist=False)
        self.store.save()
        self.failures.clear()
        self.store.log("已保存插件设置")
        return {"saved": True}

    async def web_detail(self, payload):
        persona = await self._persona(str(payload.get("persona_id", "")))
        day = str(payload.get("date", date.today().isoformat()))
        year = self._year(day)
        record = _record_for_edit(self.store.data["personas"], persona)
        return {"persona_id": persona["id"], "profile": record["profile"],
                "calendar": record["calendars"].get(str(year)), "day": self._cached_schedule(persona, day),
                "base_day": record["days"].get(day), "edit_token": self._edit_token(persona)}

    async def web_diary(self, payload):
        persona = await self._persona(str(payload.get("persona_id", "")))
        day = str(payload.get("date", ""))
        if day:
            self._year(day)
        record = self.store.data["personas"].get(persona["id"], {})
        tz = (record.get("profile") or {}).get("timezone", self.store.settings["timezone"])
        today = datetime.now(ZoneInfo(tz)).date().isoformat()
        entries = self.store.data["diaries"].get(persona["id"], {})
        entry = copy.deepcopy(entries.get(day))
        if entry:
            entry["persona_changed"] = entry.get("persona_key") != persona["key"]
            entry.pop("persona_key", None)
        can_generate, reason = False, ""
        material_tz = tz
        if day:
            try:
                _, material_tz = self._check_diary_date(persona, day)
                can_generate = True
            except ValueError as exc:
                reason = str(exc)
                # Today's plan is not eligible for a diary yet, but its saved
                # timezone still determines the pending material count.
                if record.get("key") == persona["key"]:
                    material_tz = record.get("days", {}).get(day, {}).get("timezone") or tz
        return {"persona_id": persona["id"], "date": day, "today": today, "timezone": tz,
                **self._diary_change_flags(persona, day, entries.get(day)),
                "archive_delay_minutes": self.store.settings["diary_archive_delay_minutes"],
                "automatic": self.store.settings["enabled"] and self.store.settings["diary_enabled"],
                "chat_material": material_summary(select_materials(self.store, persona, day, material_tz)),
                "chat_whitelist_count": len(self.store.settings["whitelist"]),
                "can_generate": can_generate, "reason": reason, "entry": entry,
                "entries": [{"date": d, "title": e["title"], "mood": e.get("mood", ""),
                             "updated_at": e["updated_at"]} for d, e in sorted(entries.items(), reverse=True)]}

    def _year(self, day):
        year = date.fromisoformat(day).year
        if not 1970 <= year <= 2100:
            raise ValueError("日期应在 1970～2100 年之间")
        return year

    async def web_generate(self, payload):
        persona = await self._persona(str(payload.get("persona_id", "")))
        day = str(payload.get("date", ""))
        if day:
            self._year(day)
        force = str(payload.get("scope", "day"))
        if force not in {"profile", "calendar", "day", "diary"}:
            raise ValueError("生成范围无效")
        if force == "diary":
            self._check_diary_date(persona, day)
        for job in self.jobs.values():
            if job["persona_id"] == persona["id"] and job["status"] == "running":
                return {"job_id": job["id"]}
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "persona_id": persona["id"], "date": day, "scope": force,
               "status": "running", "stage": "waiting", "progress": 0, "message": "等待开始",
               "created_at": time.time(), "updated_at": time.time()}
        self.jobs[job_id] = job
        # A real UMO allows using the persona's current session model selection.
        session = next((s for s in self.store.data["sessions"].values() if s["persona_id"] == persona["id"]), {})
        async def generate_job():
            try:
                progress = lambda stage, percent, message: self._update_job(job, stage, percent, message)
                if force == "diary":
                    await self.ensure_diary(persona, day, session.get("umo", ""), force=True, progress=progress)
                else:
                    await self.ensure(persona, session.get("umo", ""), day, force=force, progress=progress)
                self._update_job(job, "done", 100, "生成完成", status="done")
                self.failures.clear()
            except asyncio.CancelledError:
                self._update_job(job, "interrupted", job["progress"], "生成任务已中断；原日程仍保留，请重新生成", status="failed")
                raise
            except Exception as exc:
                phase = {"profile": "人设与周表", "calendar": "年度活动", "day": "当天日程", "diary": "当天日记", "saving": "保存校验"}.get(job["stage"], "生成")
                detail = "模型调用超时，可调整超时或更换模型" if isinstance(exc, TimeoutError) else "请检查所选聊天模型、输出格式和配置"
                self._update_job(job, job["stage"], job["progress"], f"{phase}失败（{type(exc).__name__}）；{detail}", status="failed")
                self._report_failure("job", exc)
        self._spawn(generate_job(), "schedule-generation-" + job_id)
        if len(self.jobs) > 50:
            for old in list(self.jobs):
                if len(self.jobs) <= 50:
                    break
                if self.jobs[old]["status"] != "running":
                    del self.jobs[old]
        self.store.save()
        return {"job_id": job_id}

    def _update_job(self, job, stage, progress, message, *, status=None):
        job.update(stage=stage, progress=progress, message=message, updated_at=time.time())
        if status:
            job["status"] = status
        self.store.save()

    async def web_job(self, payload):
        job = self.jobs.get(str(payload.get("id", "")))
        if job is None:
            raise ValueError("任务不存在，可能插件已经重载")
        return job

    async def web_save(self, payload):
        persona = await self._persona(str(payload.get("persona_id", "")))
        kind = payload.get("kind")
        async with self._lock(self.persona_locks, persona["id"]):
            await self._require_current_persona(persona)
            self._require_edit_token(persona, payload)
            record = _record_for_edit(self.store.data["personas"], persona)
            original = copy.deepcopy(record)
            if kind == "profile":
                profile = validate_profile(payload.get("value"))
                self._preserve_locks(record, record["days"])
                record["profile"] = profile
                record["calendars"], record["days"] = {}, {}
            elif kind == "calendar":
                year = self._year(str(payload.get("date", "")))
                existing = record["calendars"].get(str(year), {})
                # Preserve untouched provenance; edited rows become manual.
                value = payload.get("value")
                if not isinstance(value, list):
                    raise ValueError("活动应为数组")
                rows = validate_events([row for row in value if not isinstance(row, dict)
                    or not (str(row.get("id", "")).startswith("recurrence:") or row.get("origin") == "recurring")], year, manual=True)
                for row in rows:
                    old = next((r for r in existing.get("events", []) if r["id"] == row["id"]), None)
                    if old and all(row.get(k) == old.get(k) for k in row if k != "origin"):
                        row["origin"] = old["origin"]
                self._preserve_locks(record, {k: v for k, v in record["days"].items() if k.startswith(str(year))})
                from .recurrence import apply_recurrences
                record["calendars"][str(year)] = apply_recurrences({**existing, "year": year, "events": rows,
                    "revision": existing.get("revision", 0) + 1, "origin": "manual"}, record.get("recurrences", []), year)
                record["days"] = {k: v for k, v in record["days"].items() if not k.startswith(str(year))}
            elif kind == "day":
                day = str(payload.get("date", ""))
                year = self._year(day)
                value = payload.get("value")
                if not isinstance(value, dict):
                    raise ValueError("日程必须是对象")
                slots = validate_slots(value.get("slots"))
                old = record["days"].get(day, {})
                record["days"][day] = {"date": day, "slots": slots,
                    "timezone": (record.get("profile") or {}).get("timezone", self.store.settings["timezone"]),
                    "summary": str(value.get("summary", "手动日程"))[:1000], "origin": "manual",
                    "revision": old.get("revision", 0) + 1,
                    "events": active_events(record["calendars"].get(str(year), {}).get("events", []), day)}
                self._preserve_locks(record, {day: record["days"][day]})
            else:
                raise ValueError("保存类型无效")
            snapshot_id = self._commit_edit(persona, original, record, kind, str(payload.get("date", "")))
        self.failures.clear()
        return {"saved": True, "snapshot_id": snapshot_id, "edit_token": self._edit_token(persona)}

    async def web_recurrences(self, payload):
        persona = await self._persona(str(payload.get("persona_id", "")))
        record = self.store.data["personas"].get(persona["id"], {})
        if record.get("key") != persona["key"]:
            record = {}
        if payload.get("date"):
            year = self._year(str(payload["date"]))
        elif payload.get("year") is not None:
            try:
                year = int(payload["year"])
            except (TypeError, ValueError):
                raise ValueError("年份无效") from None
            self._year(f"{year:04d}-01-01")
        else:
            tz = (record.get("profile") or {}).get("timezone", self.store.settings["timezone"])
            year = datetime.now(ZoneInfo(tz)).year
        rules = copy.deepcopy(record.get("recurrences", []))
        return {"persona_id": persona["id"], "rules": rules, "year": year,
                "edit_token": self._edit_token(persona),
                "events": expand_rules(rules, year), "calendar_systems": ["gregorian", "fictional"]}

    async def web_save_recurrences(self, payload):
        persona = await self._persona(str(payload.get("persona_id", "")))
        rules = validate_rules(payload.get("rules"))
        async with self._lock(self.persona_locks, persona["id"]):
            await self._require_current_persona(persona)
            self._require_edit_token(persona, payload)
            previous = _record_for_edit(self.store.data["personas"], persona)
            if previous.get("recurrences", []) == rules:
                return {"saved": True, "count": len(rules), "edit_token": self._edit_token(persona)}
            record = copy.deepcopy(previous)
            old_rules = record.get("recurrences", [])
            record["recurrences"] = rules
            for year, calendar in list(record["calendars"].items()):
                updated = apply_recurrences(calendar, rules, int(year))
                if updated != calendar:
                    updated["revision"] = calendar.get("revision", 0) + 1
                    record["calendars"][year] = updated
            before_years, after_years = {}, {}
            for day, schedule in list(record["days"].items()):
                year = self._year(day)
                if year not in before_years:
                    before_years[year], after_years[year] = expand_rules(old_rules, year), expand_rules(rules, year)
                before = active_events(before_years[year], day)
                after = active_events(after_years[year], day)
                if before == after:
                    continue
                if schedule.get("origin") == "manual":
                    calendar = record["calendars"].get(str(year), {"events": schedule.get("events", [])})
                    events = active_events(apply_recurrences(calendar, rules, year)["events"], day)
                    schedule.update(events=events, revision=schedule.get("revision", 0) + 1)
                else:
                    record["days"].pop(day)
            # An invalidated generated day may later restart its revision at
            # one. Never allow an old exception to survive a changed rule set.
            snapshot_id = self._commit_edit(persona, previous, record, "recurrences", clear_adjustments=True)
        self.failures.clear()
        return {"saved": True, "count": len(rules), "snapshot_id": snapshot_id, "edit_token": self._edit_token(persona)}

    async def web_queue(self, payload):
        umo, action = str(payload.get("umo", "")), payload.get("action")
        if action == "clear":
            self.store.data["queues"].pop(umo, None)
        elif action == "retry":
            queue = self.store.data["queues"].get(umo)
            if queue is None:
                raise ValueError("队列不存在")
            queue.update(delivery="pending", due=time.time(), retry_after=0, reason="")
            self.failures.pop(umo, None)
        else:
            raise ValueError("队列操作无效")
        self.store.save()
        return {"saved": True}
