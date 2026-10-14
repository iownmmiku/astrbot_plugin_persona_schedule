"""Offline integration against an installed, real AstrBot SDK.

This script never imports test_schedule, starts adapters, reads an existing bot
configuration, calls a remote model, or sends a message to a real account.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--astrbot-path", required=True,
                        help="AstrBot source root containing the astrbot package")
    parser.add_argument("--site-packages", action="append", default=[],
                        help="Optional dependency directory; repeat if needed")
    return parser.parse_args()


async def run_smoke(isolation: Path):
    # Imports are deliberately delayed until ASTRBOT_ROOT has been isolated.
    import astrbot
    from astrbot.api import AstrBotConfig
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.star import Context
    from astrbot.api.web import PluginRequest, bind_request_context
    from astrbot.core import astrbot_config, db_helper, sp
    from astrbot.core.astrbot_config_mgr import AstrBotConfigManager
    from astrbot.core.conversation_mgr import ConversationManager
    from astrbot.core.message.components import Plain
    from astrbot.core.persona_mgr import PersonaManager
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.manager import PlatformManager
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform import Platform
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.provider.entities import LLMResponse, ProviderMeta, ProviderRequest
    from astrbot.core.provider.manager import ProviderManager
    from astrbot.core.provider.provider import Provider
    from astrbot.core.umop_config_router import UmopConfigRouter
    from astrbot.core.utils.astrbot_path import get_astrbot_root
    from astrbot.core.star.star_manager import PluginManager
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import star_handlers_registry
    from astrbot.core.pipeline.context import PipelineContext
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
    from astrbot.core.pipeline.process_stage.stage import StarRequestSubStage
    from astrbot.builtin_stars.builtin_commands.main import Main as BuiltinCommands
    from astrbot.dashboard.plugin_page_auth import PluginPageAuth
    from astrbot.dashboard.services.plugin_page_service import PluginPageService, PluginPageServiceError
    from astrbot.dashboard.services.plugin_service import PluginService
    import jwt
    from starlette.requests import Request

    from astrbot_plugin_persona_schedule.main import NAME, PersonaSchedule
    from astrbot_plugin_persona_schedule.generator import ADAPTIVE

    assert Path(get_astrbot_root()).resolve() == isolation.resolve(), "AstrBot data is not isolated"
    assert ".qa" in isolation.parts, "Smoke data must stay in the workspace QA directory"
    checks = []

    class OfflineProvider(Provider):
        """A local SDK provider fixture, with no network implementation."""

        def __init__(self, provider_id):
            super().__init__({"id": provider_id, "type": "offline_smoke", "key": ["fixture-secret"]}, {})
            self.model_name = "offline-default"
            self.calls = []

        def meta(self):
            return ProviderMeta(id=self.provider_config["id"], model=self.get_model(), type="offline_smoke")

        def get_current_key(self):
            return "fixture-secret"

        def set_key(self, key):
            raise AssertionError("The plugin must not change provider credentials")

        async def get_models(self):
            return ["offline-default", "offline-plan", "offline-reply"]

        async def text_chat(self, prompt=None, system_prompt=None, model=None, **kwargs):
            self.calls.append({"model": model, "system_prompt": system_prompt, "prompt": prompt})
            slot = {"start": "00:00", "end": "24:00", "title": "旅人的休息时间", "status": "free", "note": "离线联调"}
            if "分析人设并生成" in (system_prompt or ""):
                value = {"world_type": "original", "world_name": "离线测试世界", "countries": [],
                         "timezone": "Asia/Shanghai", "identity": "旅人", "lifestyle": "按人设生活",
                         "calendar_notes": "离线测试，无现实节日", "weekly": [{**slot, "days": list(range(1, 8))}]}
            elif "年度活动日历" in (system_prompt or ""):
                value = {"events": []}
            elif "生成给定日期" in (system_prompt or ""):
                value = {"summary": "离线测试的一天", "slots": [slot]}
            elif "第一人称生活日记" in (system_prompt or ""):
                value = {"title": "旅途的一页", "mood": "平静",
                         "content": "今天在旅途中停下脚步，在住处慢慢休息。我把平静的一天记在这里。"}
            elif ADAPTIVE in (system_prompt or ""):
                value = {"action": "defer", "activity_kind": "sleep", "importance": "normal",
                         "delay_minutes": 30, "reason": "还在延续刚才的话题，稍后再休息。",
                         "tone": "tired", "notice": "有点困了，再陪你聊一会儿就去睡。"}
            else:
                return LLMResponse(role="assistant", completion_text="刚才在路上，看到你留下的两条消息了，回来一起聊。")
            return LLMResponse(role="assistant", completion_text=json.dumps(value, ensure_ascii=False))

    class RecordingPlatform(Platform):
        """SDK adapter fixture that records messages in memory only."""

        def __init__(self, event_queue):
            super().__init__({"id": "offline_smoke"}, event_queue)
            self.sent = []

        def meta(self):
            return PlatformMetadata(name="offline_smoke", id="offline_smoke", description="In-memory smoke adapter")

        async def run(self):
            raise AssertionError("A smoke adapter must never be started")

        async def send_by_session(self, session, message_chain):
            self.sent.append((str(session), message_chain))

    await db_helper.initialize()
    router = UmopConfigRouter(sp)
    await router.initialize()
    acm = AstrBotConfigManager(astrbot_config, router, sp)
    await acm.initialize()
    personas = PersonaManager(db_helper, acm)
    await personas.initialize()
    await personas.create_persona("offline-traveler", "原创世界的旅人，不上学，没有现实国籍。", tools=[])
    conversations = ConversationManager(db_helper)
    providers = ProviderManager(acm, db_helper, personas)
    schedule_provider, reply_provider, diary_provider = (
        OfflineProvider(name) for name in ("offline-schedule", "offline-reply", "offline-diary"))
    providers.provider_insts.extend([schedule_provider, reply_provider, diary_provider])
    providers.inst_map.update({p.meta().id: p for p in providers.provider_insts})
    event_queue = asyncio.Queue()
    platforms = PlatformManager(astrbot_config, event_queue)
    recording = RecordingPlatform(event_queue)
    platforms.platform_insts.append(recording)
    context = Context(event_queue, astrbot_config, db_helper, providers, platforms,
                      conversations, None, personas, acm, None, None)
    config = AstrBotConfig(str(isolation / "plugin-config.json"), default_config={})
    context.registered_web_apis = []
    plugin = PersonaSchedule(context, config)

    async def request_api(name, method="GET", payload=None):
        payload = payload or {}
        route = next(r for r in context.registered_web_apis if r[0] == f"/{NAME}/{name}" and method in r[2])
        body = json.dumps(payload).encode()
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        scope = {"type": "http", "http_version": "1.1", "method": method,
                 "scheme": "http", "server": ("127.0.0.1", 0), "client": ("127.0.0.1", 0),
                 "path": route[0], "query_string": urlencode(payload).encode() if method == "GET" else b"",
                 "headers": [(b"content-type", b"application/json")]}
        with bind_request_context(PluginRequest(Request(scope, receive), plugin_name=NAME, username="offline-smoke")):
            response = await route[1]()
        assert response.status_code == 200, f"{name} returned HTTP {response.status_code}"
        return json.loads(response.body)

    try:
        metadata = PluginManager._load_plugin_metadata(str(ROOT))
        assert metadata and metadata.name == NAME
        metadata.root_dir_name = ROOT.name
        page_manager = SimpleNamespace(
            context=SimpleNamespace(get_all_stars=lambda: [metadata]),
            plugin_store_path=str(ROOT.parent), reserved_plugin_path=str(isolation / "unused-reserved"))
        page_service = PluginPageService(page_manager)
        discovered = await page_service.discover_plugin_pages(metadata)
        assert [page.name for page in discovered] == ["schedule"]
        assert await page_service.resolve_plugin_pages_root(metadata) == ROOT / "pages"
        serialized = await page_service.serialize_plugin_pages(metadata)
        assert serialized[0]["i18n_key"] == "pages.schedule"
        components = await PluginService(None, page_manager).get_plugin_page_components(
            metadata, page_service.serialize_plugin_pages)
        assert components[0]["type"] == "page" and components[0]["page_name"] == "schedule"
        assert metadata.i18n["zh-CN"]["pages"]["schedule"]["title"] == "角色日程"
        checks.append("real 4.28 PluginManager metadata + pages/schedule discovery + official page component + i18n")

        # Exercise official asset serving and token scope helpers, without a
        # dashboard server, browser login, or an existing JWT configuration.
        secret = "offline-smoke-signing-secret-only-never-a-real-dashboard-key"
        entry = await page_service.get_plugin_page_entry_config(
            plugin_name=NAME, page_name="schedule", jwt_secret=secret,
            username="offline-smoke", locale="zh-CN")
        entry_url = urlsplit(entry["content_path"])
        token = parse_qs(entry_url.query)["asset_token"][0]
        claims = jwt.decode(token, secret, algorithms=["HS256"])
        assert claims["plugin_name"] == NAME and claims["page_name"] == "schedule"
        assert claims["exp"] - claims["iat"] == 60
        assert PluginPageAuth.is_asset_token(claims)
        assert PluginPageAuth.is_scope_valid(claims, entry_url.path)
        assert not PluginPageAuth.is_scope_valid(claims, entry_url.path.replace("/schedule/", "/other/"))
        assert not PluginPageAuth.is_scope_valid(claims, entry_url.path.replace(NAME, "other-plugin"))
        assert not PluginPageAuth.is_scope_valid(claims, f"/api/plugin/{NAME}/state")
        for invalid_claims in ({**claims, "exp": 1},):
            expired = jwt.encode(invalid_claims, secret, algorithm="HS256")
            try:
                jwt.decode(expired, secret, algorithms=["HS256"])
            except jwt.ExpiredSignatureError:
                pass
            else:
                raise AssertionError("Expired page token was accepted")
        assert page_service.build_initial_context(
            asset_token=token, jwt_secret="a-different-offline-signing-secret-only", locale="zh-CN", theme="dark") is None

        async def page_asset(path, theme="dark"):
            return await page_service.serve_page_content(
                plugin_name=NAME, page_name="schedule", asset_path=path,
                asset_token=token, jwt_secret=secret, username="offline-smoke", locale="zh-CN", theme=theme)

        class Assets(HTMLParser):
            def __init__(self):
                super().__init__()
                self.urls = []
            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                for key in ("src", "href"):
                    if key in attributes:
                        self.urls.append(attributes[key])

        html = await page_asset("")
        assert html.content_type.startswith("text/html")
        assert 'data-theme="dark"' in html.content and 'name="color-scheme" content="dark"' in html.content
        assets = Assets()
        assets.feed(html.content)
        prefix = f"/api/plugin/page/content/{NAME}/schedule/"
        assert any(urlsplit(url).path == prefix + "style.css" for url in assets.urls)
        assert any(urlsplit(url).path == prefix + "app.js" for url in assets.urls)
        assert any(urlsplit(url).path == "/api/plugin/page/bridge-sdk.js" for url in assets.urls)
        for url in assets.urls:
            parsed = urlsplit(url)
            if parsed.path.startswith(prefix) or parsed.path == "/api/plugin/page/bridge-sdk.js":
                assert parse_qs(parsed.query) == {"asset_token": [token], "theme": ["dark"]}
                assert PluginPageAuth.is_scope_valid(claims, parsed.path)
        light = await page_asset("index.html", theme="light")
        assert 'data-theme="light"' in light.content
        css = await page_asset("style.css")
        assert css.content_type.startswith("text/css") and "data-theme" in css.content
        app = await page_asset("app.js")
        assert app.content_type.startswith("application/javascript")
        imports = re.findall(r"\bfrom\s+['\"]([^'\"]+)['\"]", app.content)
        vendor_url = next(url for url in imports if "vendor/rhinelab/content-transition.js" in url)
        assert urlsplit(vendor_url).path == prefix + "vendor/rhinelab/content-transition.js"
        assert parse_qs(urlsplit(vendor_url).query) == {"asset_token": [token], "theme": ["dark"]}
        vendor = await page_asset("vendor/rhinelab/content-transition.js")
        assert vendor.content_type.startswith("application/javascript") and "ContentTransition" in vendor.content
        # The current CSS uses no file-backed url(). This fixture exercises the
        # SDK's CSS rewrite path without introducing an artificial UI asset.
        css_fixture = page_service.rewrite_plugin_page_css(
            'a{background:url("./vendor/rhinelab/LICENSE?download=1#license")}',
            NAME, "schedule", "style.css", {"asset_token": token, "theme": "dark"})
        fixture_url = re.search(r'url\("(.*?)"\)', css_fixture).group(1)
        assert urlsplit(fixture_url).path == prefix + "vendor/rhinelab/LICENSE"
        assert parse_qs(urlsplit(fixture_url).query) == {
            "download": ["1"], "asset_token": [token], "theme": ["dark"]}
        assert urlsplit(fixture_url).fragment == "license"
        bridge = await page_service.serve_bridge_sdk(
            asset_token=token, jwt_secret=secret, locale="en-US", theme="dark")
        assert bridge.content_type.startswith("application/javascript")
        assert "AstrBotPluginPage" in bridge.content and "apiGet" in bridge.content
        initial = re.search(r"__setInitialContext\((\{.*\})\);", bridge.content)
        initial = json.loads(initial.group(1))
        assert initial["pageTitle"] == "角色日程" and initial["locale"] == "zh-CN" and initial["isDark"]
        assert initial["i18n"]["zh-CN"]["pages"]["schedule"]["title"] == "角色日程"
        for forbidden in ("../metadata.yaml", "../../main.py"):
            try:
                await page_asset(forbidden)
            except PluginPageServiceError as exc:
                assert exc.status_code == 404
            else:
                raise AssertionError("Page asset escaped its directory")
        assert page_service.build_security_headers()["Cache-Control"] == "no-store"
        checks.append("real page HTML/CSS/JS/vendor serving + scoped 60s token + theme + bridge initial context + asset rewriting")

        state = await request_api("state")
        assert "offline-traveler" in {p["id"] for p in state["personas"]}
        assert not state["settings"]["enabled"] and not state["settings"]["whitelist_enabled"]
        assert "fixture-secret" not in json.dumps(state)
        checks.append("real Context registration + native PluginRequest/JSONResponse + safe defaults")
        settings = {"enabled": True, "all_chats_enabled": True, "adaptive_enabled": False, "transition_notice_enabled": False, "schedule_provider_id": "offline-schedule", "reply_provider_id": "offline-reply",
                    "diary_provider_id": "offline-diary", "canvas_integration_enabled": True}
        await request_api("settings", "POST", settings)
        provider_data = await request_api("providers")
        assert "fixture-secret" not in json.dumps(provider_data)
        checks.append("provider enumeration without credential exposure")

        umo = "offline_smoke:FriendMessage:smoke-user"
        cid = await conversations.new_conversation(umo, title="Offline integration", persona_id="offline-traveler")
        persona = await plugin._persona("offline-traveler")
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        _, _, plan = await plugin.ensure(persona, umo, day)
        assert schedule_provider.calls and not reply_provider.calls
        checks.append("real PersonaManager + SQLite conversations + schedule provider selection")
        plan["slots"][0]["status"] = "busy"
        blocked_proactive = await plugin.get_proactive_context(umo)
        assert blocked_proactive["applicable"] and not blocked_proactive["allowed"]
        assert blocked_proactive["status"] == "busy" and blocked_proactive["cid"] == cid
        assert not recording.sent and not plugin.store.data["queues"]

        def event(text):
            message = AstrBotMessage()
            message.type = MessageType.FRIEND_MESSAGE
            message.self_id, message.session_id, message.message_id = "smoke-bot", "smoke-user", str(time.time_ns())
            message.sender = MessageMember(user_id="smoke-user", nickname="Offline user")
            message.message, message.message_str, message.raw_message = [Plain(text)], text, {}
            return AstrMessageEvent(text, message, recording.meta(), "smoke-user")

        # Resolve and execute the real built-in reset handler using the SDK's
        # waking + plugin-dispatch stages. All writes stay in isolated SQLite;
        # no result-sending stage, external adapter, or remote model is run.
        builtin = BuiltinCommands(context)
        for instance in (builtin, plugin):
            module = type(instance).__module__
            star_map[module].star_cls = instance
            star_map[module].activated = True
            if instance is builtin:
                star_map[module].name = "builtin_commands"
                star_map[module].reserved = True
            for handler in star_handlers_registry.get_handlers_by_module_name(module):
                handler.handler = getattr(instance, handler.handler_name)
        command_config = copy.deepcopy(dict(astrbot_config))
        command_config["wake_prefix"] = ["/"]
        pipeline = PipelineContext(command_config, None, "offline-command-smoke", db_helper=None)
        waking, dispatch = WakingCheckStage(), StarRequestSubStage()
        await waking.initialize(pipeline)
        await dispatch.initialize(pipeline)
        reset = event("/reset")
        before_command = (len(schedule_provider.calls), len(reply_provider.calls), len(recording.sent))
        await waking.process(reset)
        assert reset.message_str == "reset" and not reset.is_stopped()
        activated = reset.get_extra("activated_handlers")
        reset_handler = next(handler for handler in activated
                             if handler.handler_module_path == type(builtin).__module__
                             and handler.handler_name == "reset")
        schedule_handler = next(handler for handler in activated
                                if handler.handler_module_path == type(plugin).__module__
                                and handler.handler_name == "intercept_message")
        assert reset_handler.extras_configs["priority"] == 0
        assert schedule_handler.extras_configs["priority"] == -100
        assert activated.index(reset_handler) < activated.index(schedule_handler)
        assert reset_handler.handler_full_name in reset.get_extra("handlers_parsed_params")
        results = []
        async for _ in dispatch.process(reset):
            if reset.get_result():
                results.append(reset.get_result().get_plain_text())
        new_cid = await conversations.get_curr_conversation_id(umo)
        assert new_cid and new_cid != cid and any("new conversation" in result for result in results)
        assert (await conversations.get_conversation(umo, new_cid)).persona_id == "offline-traveler"
        assert reset.get_extra("_clean_group_context_session") and not reset.is_stopped()
        assert not plugin.store.data["queues"] and not reset.get_extra(NAME + ":queued", False)
        assert before_command == (len(schedule_provider.calls), len(reply_provider.calls), len(recording.sent))
        cid = new_cid
        checks.append("real 4.28 waking + priority dispatch + built-in reset updates isolated SQLite while busy; no queue/model/platform call")

        for text in ["刚刚在做什么？", "等你有空再一起聊。"]:
            incoming = event(text)
            await plugin.intercept_message(incoming)
            assert incoming.is_stopped(), "Busy event was not stopped by the real SDK event"
        session = plugin.store.data["sessions"][umo]
        await plugin._session_tick(umo, session)
        assert not recording.sent and not reply_provider.calls
        plan["slots"][0]["status"] = "free"
        permitted_proactive = await plugin.get_proactive_context(umo)
        assert permitted_proactive["applicable"] and permitted_proactive["allowed"]
        assert permitted_proactive["status"] == "free" and plan["summary"] in permitted_proactive["prompt"]
        assert permitted_proactive["context_key"] != blocked_proactive["context_key"]
        assert not recording.sent
        checks.append("real SDK persona/session proactive context + busy cancellation + schedule prompt without sending")
        await plugin._session_tick(umo, session)
        await plugin._session_tick(umo, session)
        assert len(recording.sent) == 1 and len(reply_provider.calls) == 1
        assert umo not in plugin.store.data["queues"]
        history = await conversations.get_conversation(umo, cid)
        assert "看到你留下的两条消息" in json.dumps(json.loads(history.history), ensure_ascii=False)
        checks.append("real AstrMessageEvent stop + busy silence + single aggregate local delivery + SQLite history")

        # Use the actual SDK request/event types to continue a recent chat at
        # bedtime. Only a session-local contact window may change; the saved
        # day, SQLite history and recording adapter must remain untouched.
        saved_plan = copy.deepcopy(plan)
        saved_settings = copy.deepcopy(plugin.store.settings)
        saved_dialogue = copy.deepcopy(plugin.dialogue.get(umo))
        saved_adjustment = copy.deepcopy(plugin.adjustments.get(umo))
        saved_adaptations = copy.deepcopy(plugin.store.data["adaptation_log"])
        before_adaptive = (len(schedule_provider.calls), len(reply_provider.calls),
                           len(diary_provider.calls), len(recording.sent), history.history)
        try:
            local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
            start = local_now.replace(second=0, microsecond=0) - timedelta(minutes=1)
            if start.date() != local_now.date():
                start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_clock = start.strftime("%H:%M")
            slots = [] if start_clock == "00:00" else [{"start":"00:00", "end":start_clock,
                "title":"自己的时间", "status":"free", "note":"离线弹性联调"}]
            slots.append({"start":start_clock, "end":"24:00", "title":"睡觉",
                          "status":"busy", "note":"准备休息，手机静音。", "locked":False})
            plan["slots"] = copy.deepcopy(slots)
            plugin.store.settings.update(adaptive_enabled=True, adaptive_max_delay_minutes=30)
            plugin.dialogue[umo] = {"cid":cid, "persona_key":persona["key"],
                "last_inbound":local_now.timestamp()-60, "reviews":{}, "notices":{}}
            plugin.adjustments.pop(umo, None)
            plugin.store.save()
            continuing = event("刚才说到的那件事，我们再聊一会儿吧。")
            await plugin.intercept_message(continuing)
            assert not continuing.is_stopped() and umo not in plugin.store.data["queues"]
            adjustment = plugin.store.data["contact_adjustments"][umo]
            assert adjustment["action"] == "defer" and adjustment["tone"] == "tired"
            assert adjustment["cid"] == cid and adjustment["persona_key"] == persona["key"]
            assert adjustment["starts_at"] == start.timestamp()
            assert time.time() < adjustment["until"] <= start.timestamp()+30*60
            assert len(schedule_provider.calls) == before_adaptive[0]+1
            adaptive_payload = json.loads(schedule_provider.calls[-1]["prompt"])
            assert adaptive_payload["active_conversation"] and adaptive_payload["target_activity"]["active"]
            assert adaptive_payload["target_activity"]["slot"]["status"] == "busy"
            request = ProviderRequest(prompt=continuing.message_str, system_prompt="原有角色提示。",
                conversation=await conversations.get_conversation(umo, cid))
            await plugin.inject_schedule(continuing, request)
            assert not continuing.is_stopped() and umo not in plugin.store.data["queues"]
            assert request.system_prompt.startswith("原有角色提示。") and "倦意" in request.system_prompt
            assert "有限的联系调整" in request.system_prompt
            assert plugin.adjustments[umo]["until"] == adjustment["until"]
            assert plan["slots"] == slots and plan["slots"][-1]["status"] == "busy"
            assert before_adaptive == (len(schedule_provider.calls)-1, len(reply_provider.calls),
                len(diary_provider.calls), len(recording.sent),
                (await conversations.get_conversation(umo, cid)).history)
            checks.append("real SDK ongoing bedtime chat + schedule-model adaptive decision + finite session exception + tired LLM request; unchanged saved day/history/platform")
        finally:
            plan.clear()
            plan.update(saved_plan)
            plugin.store.settings.clear()
            plugin.store.settings.update(saved_settings)
            if saved_dialogue is None:
                plugin.dialogue.pop(umo, None)
            else:
                plugin.dialogue[umo] = saved_dialogue
            if saved_adjustment is None:
                plugin.adjustments.pop(umo, None)
            else:
                plugin.adjustments[umo] = saved_adjustment
            plugin.store.data["adaptation_log"] = saved_adaptations
            plugin.store.save()

        # Save a completed local day, then exercise the actual worker path. A
        # diary archives role life only; it must neither send nor append chat.
        yesterday = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat()
        record = plugin.store.data["personas"][persona["id"]]
        record["days"][yesterday] = {**copy.deepcopy(plan), "date": yesterday}
        before = (len(schedule_provider.calls), len(reply_provider.calls), len(recording.sent), history.history)
        await plugin.tick()
        await asyncio.wait_for(asyncio.gather(*list(plugin.tasks)), timeout=5)
        archived = plugin.store.data["diaries"][persona["id"]][yesterday]
        assert archived["revision"] == 1 and len(diary_provider.calls) == 1
        assert archived["source"]["schedule"]["date"] == yesterday
        await plugin.tick()
        await asyncio.wait_for(asyncio.gather(*list(plugin.tasks)), timeout=5)
        assert len(diary_provider.calls) == 1
        archive = await request_api("diary", payload={"persona_id": persona["id"], "date": yesterday})
        assert archive["entries"][0]["date"] == yesterday and archive["entry"]["content"] == archived["content"]
        assert "persona_key" not in archive["entry"] and archive["can_generate"]
        current = await request_api("diary", payload={"persona_id": persona["id"], "date": day})
        assert not current["can_generate"] and current["entry"] is None
        assert before == (len(schedule_provider.calls), len(reply_provider.calls), len(recording.sent),
                          (await conversations.get_conversation(umo, cid)).history)
        assert "fixture-secret" not in json.dumps(archive)
        checks.append("real worker completed-day diary once + independent provider + read-only archive API + unchanged chat history")

        job = await request_api("generate", "POST", {"persona_id": persona["id"], "date": yesterday, "scope": "diary"})
        await asyncio.wait_for(asyncio.gather(*list(plugin.tasks)), timeout=5)
        complete = await request_api("job", payload={"id": job["job_id"]})
        assert complete["status"] == "done" and complete["progress"] == 100
        assert plugin.store.data["diaries"][persona["id"]][yesterday]["revision"] == 2
        assert len(diary_provider.calls) == 2 and len(recording.sent) == before[2]
        checks.append("native diary regeneration job + atomic revisioned archive without platform delivery")

        await plugin.initialize()
        await asyncio.sleep(0.05)
        before = (len(schedule_provider.calls), len(reply_provider.calls), len(diary_provider.calls), len(recording.sent))
        diagnostics = await request_api("diagnostics", payload={"umo": umo})
        assert before == (len(schedule_provider.calls), len(reply_provider.calls), len(diary_provider.calls), len(recording.sent))
        assert "fixture-secret" not in json.dumps(diagnostics)
        assert diagnostics["worker"]["state"] == "running" and diagnostics["worker"]["last_tick"]
        checks.append("real worker lifecycle + read-only diagnostics without model invocation or extra delivery")
    finally:
        await plugin.terminate()
        await db_helper.engine.dispose()
    print(json.dumps({"ok": True, "astrbot_version": astrbot.__version__, "checks": checks,
                      "mode": "offline-real-sdk", "external_model_verified": False,
                      "external_platform_delivery_verified": False}, ensure_ascii=False, indent=2))


def main():
    args = parse_args()
    source = Path(args.astrbot_path).expanduser().resolve()
    if not (source / "astrbot" / "__init__.py").is_file():
        raise SystemExit("--astrbot-path must contain the real astrbot package")
    sys.dont_write_bytecode = True
    for directory in args.site_packages:
        # Prefer dependencies shipped with the selected Python runtime. Extra
        # directories supply missing packages rather than replacing SDK pins.
        sys.path.append(str(Path(directory).expanduser().resolve()))
    sys.path.insert(0, str(source))
    sys.path.insert(0, str(ROOT.parent))
    qa = (ROOT / ".qa").resolve()
    qa.mkdir(exist_ok=True)
    previous = Path.cwd()
    # TemporaryDirectory deletes only this newly-created, checked QA directory.
    with tempfile.TemporaryDirectory(prefix="astrbot-smoke-", dir=qa) as temporary:
        isolation = Path(temporary).resolve()
        if isolation.parent != qa:
            raise SystemExit("Refusing a smoke data directory outside .qa")
        os.environ["ASTRBOT_ROOT"] = str(isolation)
        os.chdir(isolation)
        try:
            asyncio.run(run_smoke(isolation))
        finally:
            os.chdir(previous)


if __name__ == "__main__":
    main()
