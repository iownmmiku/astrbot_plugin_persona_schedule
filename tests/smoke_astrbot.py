"""Offline integration against an installed, real AstrBot SDK.

This script never imports test_schedule, starts adapters, reads an existing bot
configuration, calls a remote model, or sends a message to a real account.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
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
    from astrbot.core.provider.entities import LLMResponse, ProviderMeta
    from astrbot.core.provider.manager import ProviderManager
    from astrbot.core.provider.provider import Provider
    from astrbot.core.umop_config_router import UmopConfigRouter
    from astrbot.core.utils.astrbot_path import get_astrbot_root
    from starlette.requests import Request

    from astrbot_plugin_persona_schedule.main import NAME, PersonaSchedule

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
    schedule_provider, reply_provider = OfflineProvider("offline-schedule"), OfflineProvider("offline-reply")
    providers.provider_insts.extend([schedule_provider, reply_provider])
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
        state = await request_api("state")
        assert "offline-traveler" in {p["id"] for p in state["personas"]}
        assert not state["settings"]["enabled"] and not state["settings"]["whitelist_enabled"]
        assert "fixture-secret" not in json.dumps(state)
        checks.append("real Context registration + native PluginRequest/JSONResponse + safe defaults")
        settings = {"enabled": True, "schedule_provider_id": "offline-schedule", "reply_provider_id": "offline-reply"}
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

        def event(text):
            message = AstrBotMessage()
            message.type = MessageType.FRIEND_MESSAGE
            message.self_id, message.session_id, message.message_id = "smoke-bot", "smoke-user", str(time.time_ns())
            message.sender = MessageMember(user_id="smoke-user", nickname="Offline user")
            message.message, message.message_str, message.raw_message = [Plain(text)], text, {}
            return AstrMessageEvent(text, message, recording.meta(), "smoke-user")

        for text in ["刚刚在做什么？", "等你有空再一起聊。"]:
            incoming = event(text)
            await plugin.intercept_message(incoming)
            assert incoming.is_stopped(), "Busy event was not stopped by the real SDK event"
        session = plugin.store.data["sessions"][umo]
        await plugin._session_tick(umo, session)
        assert not recording.sent and not reply_provider.calls
        plan["slots"][0]["status"] = "free"
        await plugin._session_tick(umo, session)
        await plugin._session_tick(umo, session)
        assert len(recording.sent) == 1 and len(reply_provider.calls) == 1
        assert umo not in plugin.store.data["queues"]
        history = await conversations.get_conversation(umo, cid)
        assert "看到你留下的两条消息" in json.dumps(json.loads(history.history), ensure_ascii=False)
        checks.append("real AstrMessageEvent stop + busy silence + single aggregate local delivery + SQLite history")

        await plugin.initialize()
        await asyncio.sleep(0.05)
        before = (len(schedule_provider.calls), len(reply_provider.calls), len(recording.sent))
        diagnostics = await request_api("diagnostics", payload={"umo": umo})
        assert before == (len(schedule_provider.calls), len(reply_provider.calls), len(recording.sent))
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
