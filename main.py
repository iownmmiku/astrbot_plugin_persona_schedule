from __future__ import annotations

import asyncio
import copy
import inspect
import json
import random
import time
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .core import (DEFAULTS, LABELS, Store, active_events, allowed, digest, field,
                   slot_at, validate_events, validate_profile, validate_settings, validate_slots)
from .generator import Generator

NAME = "astrbot_plugin_persona_schedule"
CONTROL = {"日程", "日程重建", "日程会话"}


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


@register(NAME, "iownmmiku", "按人设生成生活日程、活动和合并补回消息", "0.1.0")
class PersonaSchedule(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context, self.config = context, config
        self.store = Store(StarTools.get_data_dir(NAME))
        self.store.data["settings"] = validate_settings({k: v for k, v in config.items() if k in DEFAULTS}, self.store.settings)
        self.store.save()
        self.generator = Generator(context, self.store)
        self.persona_locks, self.session_locks = {}, {}
        self.tasks, self.jobs = set(), {}
        self.generation_slots = asyncio.Semaphore(3)
        self.failures = {}
        self.closing = False
        self.worker = None
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

    async def _persona(self, persona_id):
        item = await maybe_await(self.context.persona_manager.get_persona(persona_id))
        return self._persona_data(item, persona_id)

    def _persona_data(self, item, persona_id=""):
        if not item:
            raise ValueError("当前没有可用的人设")
        pid = str(field(item, "persona_id") or field(item, "name") or persona_id)
        prompt = str(field(item, "system_prompt") or field(item, "prompt") or "")
        return {"id": pid, "prompt": prompt, "key": digest({"id": pid, "prompt": prompt})}

    async def environment(self, umo, platform_name="", conversation=None):
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
            record = {"key": persona["key"], "profile": None, "calendars": {}, "days": {}}
            records[persona["id"]] = record
        return record

    async def ensure(self, persona, umo="", day="", *, force=""):
        async with self._lock(self.persona_locks, persona["id"]):
            original = self._record(persona)
            record = copy.deepcopy(original)
            changed = bool(force)
            if force == "profile":
                record.update(profile=None, calendars={}, days={})
            if record["profile"] is None:
                async with self.generation_slots:
                    record["profile"] = await self.generator.profile(persona, umo)
                changed = True
            profile = record["profile"]
            now = datetime.now(ZoneInfo(profile["timezone"]))
            day = day or now.date().isoformat()
            year = str(date.fromisoformat(day).year)
            if force == "calendar":
                record["calendars"].pop(year, None)
                record["days"] = {k: v for k, v in record["days"].items() if not k.startswith(year)}
            if year not in record["calendars"]:
                async with self.generation_slots:
                    record["calendars"][year] = await self.generator.calendar(persona, profile, int(year), umo)
                changed = True
                # Keep the previous/current/next annual calendars.
                for old in list(record["calendars"]):
                    if abs(int(old) - int(year)) > 1:
                        del record["calendars"][old]
            if force == "day":
                record["days"].pop(day, None)
            if day not in record["days"]:
                recent = [{"date": k, "summary": v["summary"]} for k, v in sorted(record["days"].items()) if k < day][-3:]
                async with self.generation_slots:
                    record["days"][day] = await self.generator.day(persona, profile, record["calendars"][year], day, recent, umo)
                changed = True
                for old in sorted(record["days"])[:-45]:
                    del record["days"][old]
            if changed:
                # Commit all generated pieces together. A failed rebuild leaves
                # previously edited profiles/calendars/schedules intact.
                self.store.data["personas"][persona["id"]] = record
                self.store.save()
            else:
                record = original
            return record["profile"], record["calendars"][year], record["days"][day]

    def _remember_session(self, env, title=""):
        sessions = self.store.data["sessions"]
        previous = sessions.get(env["umo"], {})
        sessions[env["umo"]] = {"umo": env["umo"], "cid": env["cid"], "persona_id": env["persona"]["id"],
            "platform": env["platform"], "title": title or previous.get("title", env["umo"]), "last_seen": time.time()}
        # Metadata only; message text is kept exclusively in bounded queues.
        if len(sessions) > 2000:
            oldest = min(sessions, key=lambda u: sessions[u]["last_seen"])
            sessions.pop(oldest)
        self.store.save()

    def _control(self, event):
        word = event.message_str.strip().lstrip("/").split(maxsplit=1)
        return bool(word and word[0] in CONTROL)

    def _report_failure(self, key, exc):
        # Provider exceptions may contain credentials; expose only the type.
        now = time.time()
        if self.failures.get(key, 0) < now:
            self.failures[key] = now + 300
            message = f"日程处理失败（{type(exc).__name__}），5 分钟后重试；请检查模型和配置"
            self.store.log(message)
            logger.warning("[角色日程] %s", message)

    async def _intercept(self, event, conversation=None):
        umo = event.unified_msg_origin
        if not allowed(self.store.settings, umo) or self._control(event):
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
            slot, end = slot_at(schedule, now)
            queue = self.store.data["queues"].get(umo)
            if slot["status"] == "free" and queue is None:
                return schedule, slot
            if slot["status"] == "busy":
                due = end
            elif slot["status"] == "slow":
                due = time.time() + random.uniform(self.store.settings["slow_min_seconds"], self.store.settings["slow_max_seconds"])
            else:
                due = time.time() + 2
            # This listener also runs before commands/plugins; no LLM call or
            # typing/send operation occurs after stop_event for queued messages.
            message = event.message_str.strip() or "[非文本消息，附件内容未解析]"
            self.store.enqueue(umo, env["cid"], env["persona"]["key"], event.get_sender_name(),
                               message, time.time(), due, slot["status"], slot["title"])
            event.set_extra(NAME + ":queued", True)
            event.stop_event()
            return None
        except Exception as exc:
            self._report_failure(umo, exc)
            # A missing model/bad profile must not accidentally silence a bot.
            return None

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def intercept_message(self, event: AstrMessageEvent):
        if not (event.is_private_chat() or event.is_at_or_wake_command):
            return
        await self._intercept(event)

    @filter.on_llm_request(priority=10000)
    async def inject_schedule(self, event: AstrMessageEvent, req):
        # Recheck at the actual LLM boundary, including plugins which wake later.
        info = await self._intercept(event, getattr(req, "conversation", None))
        if info:
            schedule, slot = info
            req.system_prompt = (req.system_prompt or "") + "\n[角色此刻的生活状态]\n" + json.dumps(
                {"date": schedule["date"], "summary": schedule["summary"], "activity": slot,
                 "events": schedule["events"]}, ensure_ascii=False) + "\n保持人设，当前安排可自然影响聊天内容，不必每条消息都报告日程。"

    async def _run(self):
        while not self.closing:
            try:
                await self.tick()
            except Exception as exc:
                self._report_failure("worker", exc)
            await asyncio.sleep(self.store.settings["tick_seconds"])

    async def tick(self):
        # Work per session concurrently so one slow model does not stall everyone.
        now = time.time()
        expired = [u for u, q in self.store.data["queues"].items()
                   if now - q["created_at"] > self.store.settings["queue_expire_hours"] * 3600
                   and q["delivery"] != "sending"]
        for umo in expired:
            self.store.data["queues"].pop(umo, None)
        if expired:
            self.store.log("已清理超过保留期限的待回复消息")
        if not self.store.settings["enabled"]:
            return
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
                queue = self.store.data["queues"].get(umo)
                if queue and time.time() - queue["created_at"] > self.store.settings["queue_expire_hours"] * 3600:
                    self.store.data["queues"].pop(umo, None)
                    self.store.log("已清理超过保留期限的待回复消息")
                    queue = None
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
                profile, _, schedule = await self.ensure(env["persona"], umo)
                if not queue or queue["delivery"] in {"uncertain", "paused", "sending"}:
                    return
                if queue.get("retry_after", 0) > time.time():
                    return
                now = datetime.now(ZoneInfo(profile["timezone"]))
                slot, end = slot_at(schedule, now)
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
                self._report_failure(umo, exc)

    async def _deliver(self, env, profile, schedule, slot, queue):
        umo = env["umo"]
        messages = copy.deepcopy(queue["messages"])
        ids = {r["id"] for r in messages}
        history = field(env["conversation"], "history", "[]") or []
        if isinstance(history, str):
            history = json.loads(history)
        contexts = history[-12:] if isinstance(history, list) else []
        async with self.generation_slots:
            reply = (await self.generator.reply(env["persona"], messages, schedule, slot, umo, contexts)).strip()
        if not reply:
            raise ValueError("模型未生成补回消息")
        reply = reply[:1200]
        # Recheck permissions, persona, conversation and time *after* model wait.
        current = await self.environment(umo, env["platform"])
        if not allowed(self.store.settings, umo) or self.store.data["queues"].get(umo) is not queue:
            return
        if not current or current["cid"] != env["cid"] or current["persona"]["key"] != env["persona"]["key"]:
            queue.update(delivery="paused", reason="生成回复期间切换了对话或人设")
            self.store.save()
            return
        record = self._record(current["persona"])
        current_profile = record["profile"]
        if current_profile is None:
            return
        now = datetime.now(ZoneInfo(current_profile["timezone"]))
        current_schedule = record["days"].get(now.date().isoformat())
        if current_schedule is None:
            return
        current_slot, end = slot_at(current_schedule, now)
        if current_slot["status"] == "busy":
            queue["due"] = end
            self.store.save()
            return
        # Edits or a boundary crossing can invalidate the reply's context.
        if current_schedule is not schedule or current_slot != slot or queue["due"] > time.time():
            return
        queue["delivery"] = "sending"
        self.store.save()
        try:
            sent = await self.context.send_message(umo, MessageChain().message(reply))
        except Exception:
            queue.update(delivery="uncertain", reason="平台发送发生异常，请核对是否送达；不会自动重复发送")
            self.store.save()
            return
        if sent is False:
            queue.update(delivery="pending", due=time.time() + 300, retry_after=time.time() + 300,
                         reason="平台未匹配，5 分钟后重试")
            self.store.save()
            return
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
                  "queue": ("POST", self.web_queue)}
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
            except (ValueError, KeyError, TypeError) as exc:
                # These validators only contain local diagnostic text.
                return error_response(str(exc)[:500], status_code=400)
            except Exception as exc:
                self._report_failure("web", exc)
                return error_response("操作失败，请检查配置和 AstrBot 日志", status_code=500)
        route.__name__ = handler.__name__ + "_route"
        return route

    async def web_state(self, payload):
        personas = await maybe_await(self.context.persona_manager.get_all_personas())
        options = [{"id": self._persona_data(p)["id"], "ready": bool(self.store.data["personas"].get(self._persona_data(p)["id"], {}).get("profile"))} for p in personas]
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
        queues = [{"umo": u, "count": len(q["messages"]), "dropped": q["dropped"], "due": q["due"],
                   "delivery": q["delivery"], "reason": q["reason"]} for u, q in self.store.data["queues"].items()]
        providers = []
        getter = getattr(self.context, "get_all_providers", None)
        if getter:
            for provider in await maybe_await(getter()):
                meta = provider.meta()
                providers.append({"id": meta.id, "name": str(field(meta, "model", "") or meta.id)})
        return {"settings": settings, "personas": options, "sessions": list(sessions.values()),
                "providers": providers, "queues": queues, "logs": self.store.data["logs"][-30:]}

    async def web_settings(self, payload):
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
        self.store.save()
        self.failures.clear()
        self.store.log("已保存插件设置")
        return {"saved": True}

    async def web_detail(self, payload):
        persona = await self._persona(str(payload.get("persona_id", "")))
        day = str(payload.get("date", date.today().isoformat()))
        year = self._year(day)
        record = self._record(persona)
        return {"persona_id": persona["id"], "profile": record["profile"],
                "calendar": record["calendars"].get(str(year)), "day": record["days"].get(day)}

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
        if force not in {"profile", "calendar", "day"}:
            raise ValueError("生成范围无效")
        for job in self.jobs.values():
            if job["persona_id"] == persona["id"] and job["status"] == "running":
                return {"job_id": job["id"]}
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "persona_id": persona["id"], "status": "running", "message": "正在生成"}
        self.jobs[job_id] = job
        # A real UMO allows using the persona's current session model selection.
        session = next((s for s in self.store.data["sessions"].values() if s["persona_id"] == persona["id"]), {})
        async def generate_job():
            try:
                await self.ensure(persona, session.get("umo", ""), day, force=force)
                job.update(status="done", message="生成完成")
                self.failures.clear()
            except Exception as exc:
                job.update(status="failed", message=f"生成失败（{type(exc).__name__}），请检查模型和设置")
                self._report_failure("job", exc)
        self._spawn(generate_job(), "schedule-generation-" + job_id)
        if len(self.jobs) > 50:
            for old in list(self.jobs):
                if len(self.jobs) <= 50:
                    break
                if self.jobs[old]["status"] != "running":
                    del self.jobs[old]
        return {"job_id": job_id}

    async def web_job(self, payload):
        job = self.jobs.get(str(payload.get("id", "")))
        if job is None:
            raise ValueError("任务不存在，可能插件已经重载")
        return job

    async def web_save(self, payload):
        persona = await self._persona(str(payload.get("persona_id", "")))
        kind = payload.get("kind")
        async with self._lock(self.persona_locks, persona["id"]):
            record = self._record(persona)
            if kind == "profile":
                record["profile"] = validate_profile(payload.get("value"))
                record["calendars"], record["days"] = {}, {}
            elif kind == "calendar":
                year = self._year(str(payload.get("date", "")))
                existing = record["calendars"].get(str(year), {})
                # Preserve untouched provenance; edited rows become manual.
                rows = validate_events(payload.get("value"), year, manual=True)
                for row in rows:
                    old = next((r for r in existing.get("events", []) if r["id"] == row["id"]), None)
                    if old and all(row.get(k) == old.get(k) for k in row if k != "origin"):
                        row["origin"] = old["origin"]
                record["calendars"][str(year)] = {**existing, "year": year, "events": rows,
                    "revision": existing.get("revision", 0) + 1}
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
                    "summary": str(value.get("summary", "手动日程"))[:1000], "origin": "manual",
                    "revision": old.get("revision", 0) + 1,
                    "events": active_events(record["calendars"].get(str(year), {}).get("events", []), day)}
            else:
                raise ValueError("保存类型无效")
            self.store.save()
        self.failures.clear()
        return {"saved": True}

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
