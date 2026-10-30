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
            elif "突发生活插曲" in (system_prompt or ""):
                value = {"suitable": True, "title": "帮邻居搬行李", "note": "在旅馆临时帮邻居搬行李。",
                         "status": "busy", "duration_minutes": 30, "category": "help",
                         "reason": "旅人原本在休息，临时伸出援手符合人设。"}
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

    async def request_api(name, method="GET", payload=None, *, expected_status=200):
        payload = dict(payload or {})
        if name in {"save", "recurrences-save", "restore", "incident-end", "import-apply"} and "expected_token" not in payload:
            selected = await plugin._persona(str(payload.get("persona_id", "")))
            payload["expected_token"] = plugin._edit_token(selected)
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
        assert response.status_code == expected_status, f"{name} returned HTTP {response.status_code}: {response.body!r}"
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
        plugin_version = re.search(r"(?m)^version:\s*(\S+)$", (ROOT / "metadata.yaml").read_text(encoding="utf-8")).group(1)
        prefix = f"/api/plugin/page/content/{NAME}/schedule/"
        assert any(urlsplit(url).path == prefix + "style.css" for url in assets.urls)
        assert any(urlsplit(url).path == prefix + "app.js" for url in assets.urls)
        assert any(urlsplit(url).path == "/api/plugin/page/bridge-sdk.js" for url in assets.urls)
        for url in assets.urls:
            parsed = urlsplit(url)
            if parsed.path.startswith(prefix) or parsed.path == "/api/plugin/page/bridge-sdk.js":
                query = parse_qs(parsed.query)
                if urlsplit(url).path.endswith(("/style.css", "/app.js")):
                    assert query.pop("v", None) == [plugin_version]
                assert query == {"asset_token": [token], "theme": ["dark"]}
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

        def event(text, session_id="smoke-user"):
            message = AstrBotMessage()
            message.type = MessageType.FRIEND_MESSAGE
            message.self_id, message.session_id, message.message_id = "smoke-bot", session_id, str(time.time_ns())
            message.sender = MessageMember(user_id=session_id, nickname="Offline user")
            message.message, message.message_str, message.raw_message = [Plain(text)], text, {}
            return AstrMessageEvent(text, message, recording.meta(), session_id)

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
            assert request.system_prompt == "原有角色提示。"
            part = request.extra_user_content_parts[-1]
            assert "倦意" in part.text and "有限的联系调整" in part.text
            assert part.model_dump_for_context()["_no_save"] is True
            assembled = await request.assemble_context()
            assert assembled["role"] == "user" and assembled["content"][-1]["_no_save"] is True
            from astrbot.core.agent.message import UserMessageSegment
            transient = UserMessageSegment.model_validate(assembled)
            assert "角色此刻" in str(transient.model_dump())
            from astrbot.core.agent.message import dump_messages_with_checkpoints
            persisted = dump_messages_with_checkpoints([transient])
            assert "角色此刻" not in str(persisted)
            assert continuing.message_str in str(persisted)
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

        # New tools exercise real registered endpoints and request objects.
        before_preview = json.dumps(plugin.store.data, ensure_ascii=False, sort_keys=True)
        forecast = await request_api("preview", "POST", {"persona_id":persona["id"], "date":day,
            "time":"12:00", "message":"今晚有空吗？", "umo":umo})
        assert forecast["read_only"] and not forecast["model_evaluated"]
        assert before_preview == json.dumps(plugin.store.data, ensure_ascii=False, sort_keys=True)
        checks.append("real read-only scenario endpoint leaves schedules, queues and dialogue state unchanged")

        current_plan = plugin.store.data["personas"][persona["id"]]["days"][day]
        old_summary = current_plan["summary"]
        changed_day = {**copy.deepcopy(current_plan), "summary":"Edited only in isolated SDK"}
        saved = await request_api("save", "POST", {"persona_id":persona["id"], "date":day, "kind":"day", "value":changed_day})
        snapshots = await request_api("history", payload={"persona_id":persona["id"]})
        assert snapshots["entries"][0]["id"] == saved["snapshot_id"]
        await request_api("restore", "POST", {"persona_id":persona["id"], "id":saved["snapshot_id"]})
        assert plugin.store.data["personas"][persona["id"]]["days"][day]["summary"] == old_summary
        assert len((await request_api("history", payload={"persona_id":persona["id"]}))["entries"]) >= 2
        checks.append("real snapshot save/history/restore endpoints preserve schedule and retain undo of restore")

        # Use the official PersonaManager/SQLite edit API while both operations
        # wait for the same generation lock. Only the timing signal is wrapped;
        # persona reads and writes use the actual SDK throughout this check.
        from unittest.mock import patch
        persona_lock = plugin._lock(plugin.persona_locks, persona["id"])
        await persona_lock.acquire()
        original_lookup = plugin._persona
        looked_up, lookup_count = asyncio.Event(), 0

        async def observed_lookup(pid):
            nonlocal lookup_count
            result = await original_lookup(pid)
            lookup_count += 1
            if lookup_count == 2:
                looked_up.set()
            return result

        before_identity_edit = copy.deepcopy(plugin.store.data)
        disk_before_identity_edit = plugin.store.path.read_bytes()
        tasks = []
        try:
            with patch.object(plugin, "_persona", side_effect=observed_lookup):
                tasks = [asyncio.create_task(plugin.web_save({"persona_id":persona["id"],
                    "date":day, "kind":"day", "value":changed_day})),
                    asyncio.create_task(plugin.web_restore({"persona_id":persona["id"], "id":saved["snapshot_id"]}))]
                await asyncio.wait_for(looked_up.wait(), 5)
                await personas.update_persona(persona["id"], system_prompt="官方编辑后的新旅人身份。")
                assert (await original_lookup(persona["id"]))["key"] != persona["key"]
                persona_lock.release()
                outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
                assert all(isinstance(outcome, ValueError) and "人设已变化" in str(outcome) for outcome in outcomes)
            assert plugin.store.data == before_identity_edit
            assert plugin.store.path.read_bytes() == disk_before_identity_edit
        finally:
            if persona_lock.locked():
                persona_lock.release()
            for pending in tasks:
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await personas.update_persona(persona["id"], system_prompt=persona["prompt"])
        checks.append("real PersonaManager edit while save/restore wait for persona lock rejects stale identity and preserves disk/state")

        rule = {"title":"旅人的跨年纪念", "calendar":"fictional", "mapping":"设定霜月末日映射公历12月31日",
            "month":12, "day":31, "duration_days":3, "kind":"anniversary"}
        await request_api("recurrences-save", "POST", {"persona_id":persona["id"], "rules":[rule]})
        recurring = await request_api("recurrences", payload={"persona_id":persona["id"], "year":"2028"})
        assert any(e["start"] == "2027-12-31" and e["end"] == "2028-01-02" for e in recurring["events"])
        checks.append("real recurring-rule endpoints retain fictional mapping and cross-year occurrence dates")

        await request_api("settings", "POST", {"whitelist":[umo], "diary_chat_enabled":True})
        material_at = datetime.fromisoformat(yesterday+"T20:00:00").replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        from unittest.mock import patch
        chat_event = event("白名单素材：今天聊到一起看星星。")
        history_before_materials = (await conversations.get_conversation(umo, await conversations.get_curr_conversation_id(umo))).history
        with patch("astrbot_plugin_persona_schedule.main.time.time", return_value=material_at):
            await plugin.capture_diary_input(chat_event)
            await plugin.capture_diary_response(chat_event, LLMResponse(role="assistant", completion_text="可以留意今晚的夜空。"))
        await plugin.ensure_diary(persona, yesterday, umo, force=True)
        material_payload = json.loads(diary_provider.calls[-1]["prompt"])["saved_day"]["conversation_material"]
        assert material_payload["message_count"] == 2 and material_payload["conversation_count"] == 1
        assert [row["role"] for row in material_payload["messages"]] == ["user", "assistant"]
        material_archive = plugin.store.data["diaries"][persona["id"]][yesterday]
        assert "messages" not in material_archive["source"]["chat_context"]
        assert history_before_materials == (await conversations.get_conversation(umo, await conversations.get_curr_conversation_id(umo))).history
        await request_api("settings", "POST", {"whitelist":[]})
        assert not plugin.store.data["diary_chat"]["messages"] and material_archive["source"]["chat_context"]["message_count"] == 2
        checks.append("real event and LLMResponse whitelist journal materials, bounded metadata-only archive and revocation cleanup")

        # AstrBot creates the first SQLite conversation after message capture.
        # The real ProviderRequest is the authority for binding that one input;
        # a later reset must not attach an old model response to the new chat.
        from astrbot_plugin_persona_schedule.diary_context import select_materials
        first_session = "smoke-first-contact"
        first_umo = f"offline_smoke:FriendMessage:{first_session}"
        await sp.put_async(scope="umo", scope_id=first_umo, key="session_service_config",
                           value={"persona_id":persona["id"]})
        await request_api("settings", "POST", {"whitelist":[first_umo], "diary_chat_enabled":True,
                                                "whitelist_enabled":False, "all_chats_enabled":False})
        assert not await conversations.get_curr_conversation_id(first_umo)
        first_event = event("初次见面，我们来聊聊今天的旅行。", first_session)
        await plugin.capture_diary_input(first_event)
        marker = first_event.get_extra(NAME + ":diary_capture")
        assert marker and not marker["env"]["cid"]
        first_cid = await conversations.new_conversation(first_umo, title="Initial diary binding", persona_id=persona["id"])
        first_conversation = await conversations.get_conversation(first_umo, first_cid)
        first_request = ProviderRequest(prompt=first_event.message_str, conversation=first_conversation)
        await plugin.inject_schedule(first_event, first_request)
        assert not first_event.is_stopped()
        await plugin.capture_diary_response(first_event, LLMResponse(role="assistant", completion_text="今天的旅途很平静。"))
        material_rows = plugin.store.data["diary_chat"]["messages"]
        assert len(material_rows) == 2 and {row["cid"] for row in material_rows} == {first_cid}
        assert [row["role"] for row in material_rows] == ["user", "assistant"]
        first_day = datetime.fromtimestamp(material_rows[0]["at"], ZoneInfo("Asia/Shanghai")).date().isoformat()
        bound_material = select_materials(plugin.store, persona, first_day, "Asia/Shanghai")
        assert bound_material["message_count"] == 2 and bound_material["conversation_count"] == 1
        before_late_reply = copy.deepcopy(plugin.store.data["diary_chat"])
        first_reset = event("/reset", first_session)
        await waking.process(first_reset)
        async for _ in dispatch.process(first_reset):
            pass
        assert await conversations.get_curr_conversation_id(first_umo) != first_cid
        await plugin.capture_diary_response(first_event, LLMResponse(role="assistant", completion_text="这是重置前迟到的旧回复。"))
        assert plugin.store.data["diary_chat"] == before_late_reply
        assert (await conversations.get_conversation(first_umo, first_cid)).history == first_conversation.history
        await request_api("settings", "POST", {"whitelist":[], "all_chats_enabled":True})
        checks.append("real initial empty-CID event binds through ProviderRequest conversation; user/assistant share one journal chat and reset excludes late reply")

        # Two editors read one revision through the native endpoint. Only the
        # first may commit; HTTP 409 must preserve its data and the stale draft.
        editor = await request_api("detail", payload={"persona_id": persona["id"], "date": day})
        token = editor["edit_token"]
        original = copy.deepcopy(plugin.store.data["personas"][persona["id"]]["days"][day])
        draft = {**copy.deepcopy(original), "summary": "First SDK editor saved this revision"}
        committed = await request_api("save", "POST", {"persona_id": persona["id"], "date": day,
            "kind": "day", "value": draft, "expected_token": token})
        assert committed["edit_token"] != token
        after_commit = copy.deepcopy(plugin.store.data)
        after_commit_disk = plugin.store.path.read_bytes()
        conflict = await request_api("save", "POST", {"persona_id": persona["id"], "date": day,
            "kind": "day", "value": original, "expected_token": token}, expected_status=409)
        assert "编辑冲突" in json.dumps(conflict, ensure_ascii=False)
        assert plugin.store.data == after_commit and plugin.store.path.read_bytes() == after_commit_disk
        await request_api("restore", "POST", {"persona_id": persona["id"], "id": committed["snapshot_id"]})
        checks.append("native SDK edit token + stale second editor HTTP 409 + atomic disk/state preservation")

        # A deterministic clock controls only the local scenario. Actual SDK
        # events, providers, SQLite conversation and native routes remain real.
        from astrbot_plugin_persona_schedule.incidents import lottery_id
        incident_clock = datetime.fromisoformat(day + "T10:00:00").replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

        class FrozenIncidentTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(incident_clock, tz)

        async def incident_tick():
            plugin._schedule_incidents(incident_clock)
            await asyncio.wait_for(asyncio.gather(*list(plugin.tasks)), 5)

        before_incident = (len(schedule_provider.calls), len(reply_provider.calls), len(recording.sent))
        with patch("astrbot_plugin_persona_schedule.main.datetime", FrozenIncidentTime), \
                patch("astrbot_plugin_persona_schedule.interaction.datetime", FrozenIncidentTime), \
                patch("astrbot_plugin_persona_schedule.main.time.time", side_effect=lambda: incident_clock), \
                patch.object(plugin, "_incident_now", side_effect=lambda: incident_clock), \
                patch.object(plugin, "incident_rng", SimpleNamespace(random=lambda: 0.1, randrange=lambda size: 0)):
            await request_api("settings", "POST", {"incident_enabled": True, "incident_probability": 1.0})
            untouched = copy.deepcopy(plugin.store.data)
            preview = await request_api("preview", "POST", {"persona_id": persona["id"], "date": day,
                "time": "10:00", "umo": umo, "message": "等忙完再聊", "simulate_incident": {
                    "title": "模拟临时事项", "status": "busy", "duration_minutes": 20}})
            assert preview["read_only"] and preview["would_queue"] and not preview["model_evaluated"]
            assert preview["effective_activity"]["status"] == "busy" and preview["simulated_incident"]
            assert plugin.store.data == untouched
            assert before_incident == (len(schedule_provider.calls), len(reply_provider.calls), len(recording.sent))
            free_context = await plugin.get_proactive_context(umo)
            await incident_tick()
            identity = lottery_id(persona, day)
            entry = plugin.store.data["incidents"][identity]
            assert entry["status"] == "pending" and len(schedule_provider.calls) == before_incident[0]
            incident_clock = entry["trigger_at"]
            await incident_tick()
            entry = plugin.store.data["incidents"][identity]
            assert entry["status"] == "active" and len(schedule_provider.calls) == before_incident[0] + 1
            assert "conversation_material" not in json.loads(schedule_provider.calls[-1]["prompt"])
            incident_clock = entry["started_at"]
            busy_context = await plugin.get_proactive_context(umo)
            assert free_context["allowed"] and not busy_context["allowed"] and busy_context["status"] == "busy"
            assert free_context["context_key"] != busy_context["context_key"]
            for message in ("忙完再回复就好。", "回来后一起聊旅途吧。"):
                incoming = event(message)
                await plugin.intercept_message(incoming)
                assert incoming.is_stopped()
            assert len(plugin.store.data["queues"][umo]["messages"]) == 2
            await plugin._session_tick(umo, plugin.store.data["sessions"][umo])
            assert len(recording.sent) == before_incident[2] and len(reply_provider.calls) == before_incident[1]
            incident_clock = entry["ends_at"]
            await incident_tick()
            for _ in range(2):
                await plugin._session_tick(umo, plugin.store.data["sessions"][umo])
            assert len(recording.sent) == before_incident[2] + 1 and len(reply_provider.calls) == before_incident[1] + 1
            assert umo not in plugin.store.data["queues"]
            resumed = await plugin.get_proactive_context(umo)
            assert resumed["allowed"] and resumed["context_key"] != busy_context["context_key"]
            actual = await request_api("incidents", payload={"persona_id": persona["id"]})
            assert actual["entry"]["status"] == "ended" and actual["history"]
            assert "persona_key" not in json.dumps(actual) and "source_fingerprint" not in json.dumps(actual)
        await request_api("settings", "POST", {"incident_enabled": False})
        checks.append("real SDK incident draw/generation + busy silence + one post-incident aggregate + Canvas boundary + read-only native simulation")

        # A reviewed backup restores only the selected role's schedule. Private
        # queues, completed incident decisions and unrelated roles stay intact.
        await personas.create_persona("offline-neighbor", "原创世界的邻居，有自己的生活安排。", tools=[])
        neighbor = await plugin._persona("offline-neighbor")
        await plugin.ensure(neighbor, day=day)
        current_plan = plugin.store.data["personas"][persona["id"]]["days"][day]
        current_plan["slots"][0]["status"] = "busy"
        retained_message = event("这条离线留言应保留在原队列。")
        await plugin.intercept_message(retained_message)
        assert retained_message.is_stopped()
        current_plan["slots"][0]["status"] = "free"
        plugin.store.save()
        preserved = {key: copy.deepcopy(plugin.store.data[key]) for key in ("queues", "incidents", "diaries")}
        neighbor_before = copy.deepcopy(plugin.store.data["personas"][neighbor["id"]])
        backup = (await request_api("backup-create", "POST"))["backup"]
        listing = await request_api("backups")
        assert any(row["id"] == backup["id"] and row["valid"] for row in listing["backups"])
        exported = await request_api("export", payload={"persona_id": persona["id"]})
        export_text = json.dumps(exported, ensure_ascii=False)
        assert exported["format"] == "persona-schedule-export" and exported["record"]["days"]
        assert all(value not in export_text for value in ('"key"', '"persona_key"', '"messages"', '"queues"', '"diaries"', "fixture-secret", retained_message.message_str))
        await request_api("save", "POST", {"persona_id": persona["id"], "date": day, "kind": "day",
            "value": {**copy.deepcopy(current_plan), "summary": "Changed after local backup"}})
        before_import = copy.deepcopy(plugin.store.data)
        import_preview = await request_api("import-preview", "POST", {"persona_id": persona["id"], "backup_id": backup["id"]})
        assert import_preview["changes"] and plugin.store.data == before_import
        applied = await request_api("import-apply", "POST", {"persona_id": persona["id"],
            "preview_token": import_preview["preview_token"], "expected_token": import_preview["expected_token"]})
        assert applied["saved"] and applied["edit_token"]
        assert plugin.store.data["personas"][persona["id"]]["days"][day]["summary"] == current_plan["summary"]
        assert plugin.store.data["personas"][neighbor["id"]] == neighbor_before
        assert all(plugin.store.data[key] == value for key, value in preserved.items())
        await request_api("queue", "POST", {"umo": umo, "action": "clear"})
        checks.append("native checksummed backup create/list + redacted share export + reviewed persona-only restore preserves queues, incident ledger and other role")

        await plugin.ensure_diary(persona, yesterday, umo, force=True)
        clean_diary = await request_api("diary", payload={"persona_id": persona["id"], "date": yesterday})
        assert not clean_diary["source_changed"]
        before_diary_change = len(diary_provider.calls)
        edited_yesterday = copy.deepcopy(plugin.store.data["personas"][persona["id"]]["days"][yesterday])
        edited_yesterday["summary"] = "归档后补充的角色生活安排"
        await request_api("save", "POST", {"persona_id": persona["id"], "date": yesterday,
            "kind": "day", "value": edited_yesterday})
        changed_diary = await request_api("diary", payload={"persona_id": persona["id"], "date": yesterday})
        assert changed_diary["source_changed"] and changed_diary["source_change_reasons"]
        assert changed_diary["entry"]["content"] == clean_diary["entry"]["content"]
        assert len(diary_provider.calls) == before_diary_change
        await request_api("settings", "POST", {"diary_archive_delay_minutes": 12})
        assert (await request_api("state"))["settings"]["diary_archive_delay_minutes"] == 12
        await request_api("settings", "POST", {"diary_archive_delay_minutes": 121}, expected_status=400)
        assert plugin.store.settings["diary_archive_delay_minutes"] == 12
        checks.append("native diary source_changed notice preserves archive until regeneration + validated end-of-day archive buffer setting")

        await request_api("settings", "POST", {"daily_background_call_limit": 1})
        before_metrics = (len(schedule_provider.calls), len(reply_provider.calls), len(diary_provider.calls), len(recording.sent))
        runtime_state = await request_api("state")
        runtime_diagnostics = await request_api("diagnostics", payload={"umo": umo})
        for response in (runtime_state, runtime_diagnostics):
            runtime = response["model_runtime"]
            assert runtime["background_limit"] == 1 and runtime["background_used"] >= 1
            assert runtime["active"] == runtime["queued"] == 0
            assert runtime["today"]["purposes"]["incident"]["success"] == 1
            assert runtime["today"]["purposes"]["reply"]["success"] >= 2
            assert "fixture-secret" not in json.dumps(response)
        assert before_metrics == (len(schedule_provider.calls), len(reply_provider.calls), len(diary_provider.calls), len(recording.sent))
        checks.append("native model-runtime purpose statistics + background budget configuration readable without model calls or delivery")

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
