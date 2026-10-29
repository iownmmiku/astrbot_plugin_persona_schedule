"""Behavior tests with AstrBot API doubles; calendars use the real holidays library."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
if (ROOT / ".qa" / "deps").is_dir():
    sys.path.insert(0, str(ROOT / ".qa" / "deps"))


class FakeConfig(dict):
    def save_config(self):
        pass


class FakeStar:
    def __init__(self, context):
        self.context = context


class FakeChain:
    def message(self, content):
        self.content = content
        return self


def decorator(*args, **kwargs):
    return lambda fn: fn


for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["astrbot.api"].AstrBotConfig = FakeConfig
sys.modules["astrbot.api"].logger = logging.getLogger("test")
sys.modules["astrbot.api.event"].AstrMessageEvent = object
sys.modules["astrbot.api.event"].MessageChain = FakeChain
sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(event_message_type=decorator,
    on_llm_request=decorator, on_llm_response=decorator, command=decorator, permission_type=decorator,
    EventMessageType=types.SimpleNamespace(ALL=1), PermissionType=types.SimpleNamespace(ADMIN=1))
sys.modules["astrbot.api.star"].Context = object
sys.modules["astrbot.api.star"].Star = FakeStar
sys.modules["astrbot.api.star"].StarTools = types.SimpleNamespace(get_data_dir=lambda name: "unused")
sys.modules["astrbot.api.star"].register = decorator

# Offline SDK double; real TextPart persistence is checked in smoke_astrbot.py.
class FakeTextPart:
    def __init__(self, text):
        self.text, self._no_save = text, False
    def mark_as_temp(self):
        self._no_save = True
        return self

sys.modules["astrbot.core.agent.message"] = types.ModuleType("astrbot.core.agent.message")
sys.modules["astrbot.core.agent.message"].TextPart = FakeTextPart

from astrbot_plugin_persona_schedule.core import (DEFAULTS, Store, active_events, allowed, digest,
    slot_at, validate_events, validate_profile, validate_settings, validate_slots)
from astrbot_plugin_persona_schedule.generator import Generator, parse_object, public_calendar
from astrbot_plugin_persona_schedule.main import NAME, PersonaSchedule

UMO = "test:FriendMessage:123"


def slots(status="free", title="休息"):
    return [{"start": "00:00", "end": "24:00", "title": title, "status": status, "note": "场景"}]


def profile(countries=None, world="real"):
    return {"world_type": world, "world_name": "测试世界", "countries": countries or [],
        "timezone": "Asia/Shanghai", "identity": "旅人", "lifestyle": "按人设生活", "calendar_notes": "",
        "weekly": [{**r, "days": list(range(1, 8))} for r in slots()]}


class FakePersonaManager:
    def __init__(self):
        self.item = {"persona_id": "traveler", "system_prompt": "自创世界的一位旅人，没有国籍，也不去学校。"}

    async def get_persona(self, persona_id):
        if persona_id != self.item["persona_id"]:
            raise ValueError("人设不存在")
        return self.item

    async def get_all_personas(self):
        return [self.item]

    async def resolve_selected_persona(self, **kwargs):
        return self.item["persona_id"], self.item, None, False


class FakeConversations:
    def __init__(self):
        self.cid = "conv-1"
        self.pairs = []

    async def get_curr_conversation_id(self, umo):
        return self.cid

    async def get_conversation(self, umo, cid):
        return types.SimpleNamespace(cid=cid, persona_id="traveler", history="[]")

    async def get_filtered_conversations(self, **kwargs):
        return [types.SimpleNamespace(user_id=UMO, title="测试对话", persona_id="traveler", cid=self.cid)], 1

    async def add_message_pair(self, *args):
        self.pairs.append(args)


class FakeContext:
    def __init__(self):
        self.persona_manager = FakePersonaManager()
        self.conversation_manager = FakeConversations()
        self.sent, self.routes, self.calls = [], [], []
        self.send_error = False
        self.send_result = True

    def register_web_api(self, *args):
        self.routes.append(args)

    def get_config(self, umo):
        return {}

    async def get_all_providers(self):
        return []

    async def get_current_chat_provider_id(self, umo):
        return "fake-provider"

    async def get_using_provider_async(self):
        return types.SimpleNamespace(meta=lambda: types.SimpleNamespace(id="fake-provider"))

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        instruction = kwargs["system_prompt"]
        if "分析人设并生成" in instruction:
            value = profile(world="original")
        elif "年度活动日历" in instruction:
            value = {"events": []}
        elif "生成给定日期" in instruction:
            value = {"summary": "旅人的一天", "slots": slots()}
        elif "第一人称生活日记" in instruction:
            value = {"title": "路上的一日", "mood": "平静", "content": "今天沿着熟悉的路慢慢走，回到住处时，我把沿途的风景记了下来。"}
        else:
            return types.SimpleNamespace(completion_text="刚才在路上，看到你说的了，等我回去一起聊。")
        return types.SimpleNamespace(completion_text=json.dumps(value, ensure_ascii=False))

    async def send_message(self, umo, chain):
        if self.send_error:
            raise OSError("network uncertain")
        self.sent.append((umo, chain.content))
        return self.send_result


class FakeEvent:
    unified_msg_origin = UMO
    is_at_or_wake_command = True

    def __init__(self, message="今天去哪里了？"):
        self.message_str = message
        self.extras = {}
        self.stopped = False

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def stop_event(self):
        self.stopped = True

    def get_platform_name(self):
        return "test"

    def get_sender_name(self):
        return "用户"

    def is_private_chat(self):
        return True


class ValidationTests(unittest.TestCase):
    def test_full_day_overlap_gap_and_midnight(self):
        self.assertEqual(validate_slots(slots())[0]["end"], "24:00")
        for bad in [slots() + slots(), [{**slots()[0], "start": "01:00"}],
                    [{**slots()[0], "start": "23:00", "end": "07:00"}],
                    [{**slots()[0], "end": "25:00"}], [{**slots()[0], "status": "unknown"}]]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_slots(bad)

    def test_week_needs_every_day(self):
        data = profile()
        data["weekly"][0]["days"] = [1, 2, 3, 4, 5]
        with self.assertRaises(ValueError):
            validate_profile(data)

    def test_settings_default_disabled_and_empty_whitelist(self):
        self.assertFalse(allowed(DEFAULTS, UMO))
        s = validate_settings({"enabled": True, "whitelist_enabled": True}, DEFAULTS)
        self.assertFalse(allowed(s, UMO))
        s["whitelist"] = [UMO]
        self.assertTrue(allowed(s, UMO))
        self.assertFalse(allowed(s, "test:FriendMessage:999"))

    def test_invalid_settings_rejected(self):
        for value in [{"enabled": "true"}, {"slow_min_seconds": 9999},
                      {"slow_min_seconds": 200, "slow_max_seconds": 100}, {"timezone": "Invalid/Timezone"}]:
            with self.subTest(value=value), self.assertRaises((ValueError, KeyError)):
                validate_settings(value, DEFAULTS)

    def test_events_cross_year_and_provenance(self):
        row = {"title": "冬日庆典", "start": "2025-12-28", "end": "2026-01-05", "kind": "festival",
               "day_off": True, "origin": "canon", "source_urls": ["https://fake.example/canon"]}
        clean = validate_events([row], 2026)[0]
        self.assertEqual(clean["origin"], "generated")
        self.assertEqual(clean["source_urls"], [])
        clean = validate_events([row], 2026, source_urls=row["source_urls"])[0]
        self.assertEqual(clean["origin"], "reference")
        self.assertEqual(len(active_events([clean], "2026-01-05")), 1)
        self.assertEqual(len(active_events([clean], "2026-01-06")), 0)

    def test_midnight_boundary(self):
        plan = {"slots": [{**slots("busy")[0], "end": "08:00"}, {**slots()[0], "start": "08:00"}]}
        self.assertEqual(slot_at(plan, datetime(2026,10,12,7,59,tzinfo=ZoneInfo("Asia/Shanghai")))[0]["status"], "busy")
        self.assertEqual(slot_at(plan, datetime(2026,10,12,8,0,tzinfo=ZoneInfo("Asia/Shanghai")))[0]["status"], "free")

    def test_json_model_contract(self):
        self.assertEqual(parse_object('```json\n{"events":[]}\n```'), {"events": []})
        for raw in ["not json", "[]", None]:
            with self.assertRaises((ValueError, TypeError)):
                parse_object(raw)

    def test_real_country_calendars(self):
        cn, _ = public_calendar(profile(["CN"]), 2026)
        jp, _ = public_calendar(profile(["JP"]), 2026)
        self.assertTrue(any("中秋" in r["title"] or "Mid-Autumn" in r["title"] for r in cn))
        self.assertFalse(any("中秋" in r["title"] or "Mid-Autumn" in r["title"] for r in jp))
        imaginary, _ = public_calendar(profile([], "original"), 2026)
        self.assertEqual(imaginary, [])

    def test_lunar_calendar_not_fixed_gregorian_date(self):
        dates = []
        for year in [2026, 2027]:
            rows, _ = public_calendar(profile(["CN"]), year)
            dates.append(next(r["start"][5:] for r in rows if "中秋" in r["title"] or "Mid-Autumn" in r["title"]))
        self.assertNotEqual(*dates)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_queue_merge_does_not_slide_delay(self):
        q = self.store.enqueue(UMO, "c", "p", "u", "first", 1, 100, "slow")
        self.store.enqueue(UMO, "c", "p", "u", "second", 2, 200, "slow")
        self.assertEqual(q["due"], 100)
        self.assertEqual(len(q["messages"]), 2)

    def test_bounded_queue_and_batch_commit(self):
        self.store.settings["queue_limit"] = 5
        for i in range(7):
            self.store.enqueue(UMO, "c", "p", "u", str(i), i, 100, "busy")
        q = self.store.data["queues"][UMO]
        self.assertEqual(q["dropped"], 2)
        batch = {r["id"] for r in q["messages"]}
        self.store.enqueue(UMO, "c", "p", "u", "new during generation", 8, 100, "slow")
        self.store.finish_batch(UMO, batch)
        self.assertEqual([r["text"] for r in q["messages"]], ["new during generation"])

    def test_restart_recovery_and_uncertain_delivery(self):
        q = self.store.enqueue(UMO, "c", "p", "u", "hello", 1, 2, "busy")
        q["delivery"] = "sending"
        self.store.save()
        restored = Store(self.temp.name)
        self.assertEqual(restored.data["queues"][UMO]["delivery"], "uncertain")

    def test_switch_drops_old_persona_messages(self):
        self.store.enqueue(UMO, "c1", "p1", "u", "old", 1, 2, "busy")
        q = self.store.enqueue(UMO, "c2", "p2", "u", "new", 2, 3, "busy")
        self.assertEqual([r["text"] for r in q["messages"]], ["new"])


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ctx = FakeContext()
        with patch('astrbot_plugin_persona_schedule.main.StarTools.get_data_dir',return_value=self.temp.name):
            self.plugin = PersonaSchedule(self.ctx, FakeConfig({"all_chats_enabled": True, "adaptive_enabled": False, "transition_notice_enabled": False}))
        self.plugin.store.settings["enabled"] = True
        self.persona = await self.plugin._persona("traveler")
        self.day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        await self.plugin.ensure(self.persona, UMO)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    def plan(self, status):
        record = self.plugin.store.data["personas"]["traveler"]
        record["days"][self.day]["slots"] = slots(status)
        return record["profile"], record["days"][self.day]

    async def tick(self):
        await self.plugin._session_tick(UMO,self.plugin.store.data["sessions"][UMO])

    async def test_free_passes_and_injects_context(self):
        event = FakeEvent()
        req = types.SimpleNamespace(system_prompt="原人设",conversation=None)
        await self.plugin.intercept_message(event)
        await self.plugin.inject_schedule(event,req)
        self.assertFalse(event.stopped)
        self.assertEqual(req.system_prompt, "原人设")
        self.assertIn("角色此刻", req.extra_user_content_parts[-1].text)
        self.assertTrue(req.extra_user_content_parts[-1]._no_save)
        self.assertEqual(self.ctx.sent,[])

    async def test_transient_context_preserves_other_parts_and_replaces_stale_block(self):
        event = FakeEvent()
        other = FakeTextPart("另一个插件的资料")
        history = [{"role": "user", "content": "原聊天"}]
        req = types.SimpleNamespace(system_prompt="固定人设", prompt="用户原话",
            contexts=history, conversation=None, extra_user_content_parts=[other])
        await self.plugin.inject_schedule(event, req)
        first = req.extra_user_content_parts[-1]
        self.plan("free")[1]["slots"][0]["title"] = "新的活动"
        await self.plugin.inject_schedule(event, req)
        self.assertEqual(req.system_prompt, "固定人设")
        self.assertEqual(req.prompt, "用户原话")
        self.assertIs(req.contexts, history)
        self.assertEqual(history, [{"role": "user", "content": "原聊天"}])
        self.assertEqual(len(req.extra_user_content_parts), 2)
        self.assertIs(req.extra_user_content_parts[0], other)
        self.assertIsNot(req.extra_user_content_parts[1], first)
        self.assertIn("新的活动", req.extra_user_content_parts[1].text)
        self.assertTrue(req.extra_user_content_parts[1]._no_save)
        self.plugin.store.settings["enabled"] = False
        await self.plugin.inject_schedule(event, req)
        self.assertEqual(req.extra_user_content_parts, [other])
        self.assertEqual(req.system_prompt, "固定人设")

    async def test_busy_request_does_not_receive_transient_context(self):
        self.plan("busy")
        event = FakeEvent()
        req = types.SimpleNamespace(system_prompt="固定人设", conversation=None, extra_user_content_parts=[])
        await self.plugin.inject_schedule(event, req)
        self.assertTrue(event.stopped)
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertEqual(req.system_prompt, "固定人设")

    async def test_busy_then_free_sends_single_combined_reply(self):
        self.plan("busy")
        for message in ["你在哪", "我今天有点难过"]:
            event = FakeEvent(message)
            await self.plugin.intercept_message(event)
            self.assertTrue(event.stopped)
        await self.tick()
        self.assertEqual(self.ctx.sent, [])
        self.plan("free")
        await self.tick()
        self.assertEqual(len(self.ctx.sent),1)
        self.assertNotIn(UMO,self.plugin.store.data["queues"])
        prompt = json.loads(self.ctx.calls[-1]["prompt"])
        self.assertEqual([r["text"] for r in prompt["queued_messages"]],["你在哪","我今天有点难过"])
        await self.tick()
        self.assertEqual(len(self.ctx.sent),1)

    async def test_slow_replies_after_due(self):
        self.plan("slow")
        event=FakeEvent()
        await self.plugin.intercept_message(event)
        self.assertTrue(event.stopped)
        await self.tick()
        self.assertEqual(self.ctx.sent,[])
        self.plugin.store.data["queues"][UMO]["due"]=time.time()-1
        await self.tick()
        self.assertEqual(len(self.ctx.sent),1)

    async def test_listener_and_hook_dont_duplicate_queue(self):
        self.plan("busy")
        event=FakeEvent()
        await self.plugin.intercept_message(event)
        await self.plugin.inject_schedule(event,types.SimpleNamespace(system_prompt="",conversation=None))
        self.assertEqual(len(self.plugin.store.data["queues"][UMO]["messages"]),1)

    async def test_disabled_and_whitelist_excluded_are_untouched(self):
        self.plan("busy")
        self.plugin.store.settings["whitelist_enabled"]=True
        event=FakeEvent()
        await self.plugin.intercept_message(event)
        self.assertFalse(event.stopped)
        self.assertEqual(self.plugin.store.data["queues"],{})
        self.plugin.store.settings.update(enabled=False,whitelist_enabled=False)
        await self.plugin.intercept_message(event)
        self.assertFalse(event.stopped)

    async def test_control_available_while_busy(self):
        self.plan("busy")
        event=FakeEvent("/日程")
        await self.plugin.intercept_message(event)
        self.assertFalse(event.stopped)

    async def test_cid_switch_pauses_queue(self):
        self.plan("busy")
        await self.plugin.intercept_message(FakeEvent())
        self.ctx.conversation_manager.cid="conv-2"
        self.plan("free")
        await self.tick()
        self.assertEqual(self.ctx.sent,[])
        self.assertEqual(self.plugin.store.data["queues"][UMO]["delivery"],"paused")

    async def test_disable_during_reply_generation(self):
        self.plan("busy")
        await self.plugin.intercept_message(FakeEvent())
        self.plan("free")
        async def reply(*args):
            self.plugin.store.settings["enabled"]=False
            return "不应该发送"
        self.plugin.generator.reply=reply
        await self.tick()
        self.assertEqual(self.ctx.sent,[])

    async def test_new_busy_period_during_reply_generation(self):
        self.plan("busy")
        await self.plugin.intercept_message(FakeEvent())
        self.plan("free")
        async def reply(*args):
            self.plan("busy")
            return "不应该发送"
        self.plugin.generator.reply=reply
        await self.tick()
        self.assertEqual(self.ctx.sent,[])

    async def test_new_messages_during_generation_are_next_batch(self):
        self.plan("busy")
        await self.plugin.intercept_message(FakeEvent("first"))
        self.plan("free")
        async def reply(*args):
            self.plugin.store.enqueue(UMO,"conv-1",self.persona["key"],"u","new",time.time(),time.time()-1,"slow")
            return "先回第一批"
        self.plugin.generator.reply=reply
        await self.tick()
        self.assertEqual(len(self.ctx.sent),1)
        self.assertEqual([r["text"] for r in self.plugin.store.data["queues"][UMO]["messages"]],["new"])

    async def test_uncertain_platform_send_no_automatic_resend(self):
        self.plan("busy")
        await self.plugin.intercept_message(FakeEvent())
        self.plan("free")
        self.ctx.send_error=True
        await self.tick()
        q=self.plugin.store.data["queues"][UMO]
        self.assertEqual(q["delivery"],"uncertain")
        self.ctx.send_error=False
        await self.tick()
        self.assertEqual(self.ctx.sent,[])

    async def test_history_failure_doesnt_resend(self):
        self.plan("busy")
        await self.plugin.intercept_message(FakeEvent())
        self.plan("free")
        self.ctx.conversation_manager.add_message_pair=AsyncMock(side_effect=RuntimeError("db failed"))
        await self.tick()
        await self.tick()
        self.assertEqual(len(self.ctx.sent),1)

    async def test_failed_rebuild_preserves_manual_data(self):
        before=copy.deepcopy(self.plugin.store.data["personas"]["traveler"])
        self.plugin.generator.profile=AsyncMock(side_effect=RuntimeError("model down"))
        with self.assertRaises(RuntimeError):
            await self.plugin.ensure(self.persona,UMO,force="profile")
        self.assertEqual(self.plugin.store.data["personas"]["traveler"],before)

    async def test_calendar_edit_invalidates_days(self):
        row={"title":"旅人集市","start":self.day,"end":self.day,"kind":"activity","day_off":True}
        await self.plugin.web_save({"persona_id":"traveler","kind":"calendar","date":self.day,"value":[row]})
        record=self.plugin.store.data["personas"]["traveler"]
        self.assertNotIn(self.day,record["days"])
        self.assertEqual(record["calendars"][self.day[:4]]["events"][0]["origin"],"manual")

    async def test_web_settings_keeps_and_redacts_secret(self):
        await self.plugin.web_settings({"tavily_api_key":"secret-test-only"})
        await self.plugin.web_settings({"slow_min_seconds":90})
        result=await self.plugin.web_state({})
        self.assertTrue(result["settings"]["has_tavily_api_key"])
        self.assertNotIn("secret-test-only",json.dumps(result))
        self.assertEqual(self.plugin.store.settings["tavily_api_key"],"secret-test-only")

    async def test_registered_web_routes_use_plugin_prefix(self):
        self.assertEqual({r[0].rsplit('/',1)[-1] for r in self.ctx.routes},
                         {'state','settings','detail','generate','job','save','queue','providers','diagnostics','diary',
                          'preview','history','restore','recurrences','recurrences-save'})
        self.assertTrue(all(r[0].startswith('/'+NAME+'/') for r in self.ctx.routes))

    async def test_cached_schedule_is_stable_and_no_extra_model_calls(self):
        before=len(self.ctx.calls)
        first=await self.plugin.ensure(self.persona,UMO)
        second=await self.plugin.ensure(self.persona,UMO)
        self.assertIs(first[2],second[2])
        self.assertEqual(len(self.ctx.calls),before)

    async def test_concurrent_first_generation_runs_once_per_persona(self):
        self.plugin.store.data["personas"]={}
        before=len(self.ctx.calls)
        await asyncio.gather(*[self.plugin.ensure(self.persona,UMO) for _ in range(4)])
        self.assertEqual(len(self.ctx.calls)-before,3)

    async def test_known_unsent_platform_failure_respects_backoff(self):
        self.plan("busy")
        await self.plugin.intercept_message(FakeEvent())
        self.plan("free")
        self.ctx.send_result=False
        await self.tick()
        self.assertEqual(len(self.ctx.sent),1)
        await self.tick()
        self.assertEqual(len(self.ctx.sent),1)
        self.assertGreater(self.plugin.store.data["queues"][UMO]["retry_after"],time.time())

    async def test_pending_message_records_busy_activity(self):
        self.plan("busy")
        await self.plugin.intercept_message(FakeEvent())
        row=self.plugin.store.data["queues"][UMO]["messages"][0]
        self.assertEqual(row["activity"],"休息")
        self.assertEqual(row["status"],"busy")

    async def test_expiration_continues_while_disabled(self):
        self.plan("busy")
        await self.plugin.intercept_message(FakeEvent())
        self.plugin.store.data["queues"][UMO]["created_at"]=time.time()-80*3600
        for row in self.plugin.store.data["queues"][UMO]["messages"]:
            row["at"]=self.plugin.store.data["queues"][UMO]["created_at"]
        self.plugin.store.settings["enabled"]=False
        await self.plugin.tick()
        self.assertEqual(self.plugin.store.data["queues"],{})

    async def test_settings_persistence_failure_does_not_enable_bot(self):
        self.plugin.store.settings["enabled"]=False
        self.plugin.config.save_config=lambda: (_ for _ in ()).throw(OSError("disk full"))
        with self.assertRaises(OSError):
            await self.plugin.web_settings({"enabled":True})
        self.assertFalse(self.plugin.store.settings["enabled"])

    async def test_day_generation_uses_persona_events(self):
        row={"title":"霜月祭","start":self.day,"end":self.day,"kind":"festival","day_off":True,"description":"世界观创作"}
        await self.plugin.web_save({"persona_id":"traveler","kind":"calendar","date":self.day,"value":[row]})
        await self.plugin.ensure(self.persona,UMO)
        payload=json.loads(self.ctx.calls[-1]["prompt"])
        self.assertEqual(payload["today_events"][0]["title"],"霜月祭")
        self.assertTrue(payload["today_events"][0]["day_off"])

    async def test_expired_queue_is_deleted_without_reply(self):
        self.plan("busy")
        await self.plugin.intercept_message(FakeEvent())
        self.plugin.store.data["queues"][UMO]["created_at"]=time.time()-80*3600
        for row in self.plugin.store.data["queues"][UMO]["messages"]:
            row["at"]=self.plugin.store.data["queues"][UMO]["created_at"]
        self.plan("free")
        await self.tick()
        self.assertNotIn(UMO,self.plugin.store.data["queues"])
        self.assertEqual(self.ctx.sent,[])


if __name__ == "__main__":
    unittest.main(verbosity=2)
