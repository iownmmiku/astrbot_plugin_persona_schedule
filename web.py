"""WebUI routes and their JSON handlers for the schedule workbench."""
from __future__ import annotations

import asyncio
import copy
import sys
import time
import uuid
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from astrbot.api import logger

from .core import (NAME, active_events, allowed, field, maybe_await, minute, slot_at, slow_bounds, validate_events,
                   validate_profile, validate_settings, validate_slots)
from .diary_context import material_summary, prune_materials, select_materials
from .generator import is_chat_provider
from .maintenance import EditConflict
from .recurrence import apply_recurrences, expand_rules, validate_rules
from .workbench import _record_for_edit


class _MainDatetime:
    """Resolve ``datetime`` from main at call time, so clock patches apply."""

    def __getattr__(self, name):
        return getattr(sys.modules[__package__ + ".main"].datetime, name)


datetime = _MainDatetime()


class WebMixin:
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
