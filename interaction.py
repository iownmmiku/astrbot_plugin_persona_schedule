"""Conversation continuity, bounded contact exceptions and advance goodbyes."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astrbot.api.event import MessageChain

from .adaptive import create_adjustment, effective_activity, policy_kind, target_activity
from .core import digest


class InteractionMixin:
    def _dialogue_meta(self, env):
        umo = env["umo"]
        meta = self.dialogue.get(umo)
        if not meta or meta.get("cid") != env["cid"] or meta.get("persona_key") != env["persona"]["key"]:
            meta = {"cid": env["cid"], "persona_key": env["persona"]["key"], "last_inbound": 0,
                    "reviews": {}, "notices": {}}
            self.dialogue[umo] = meta
            self.adjustments.pop(umo, None)
        if len(self.dialogue) > 2000:
            oldest = min(self.dialogue, key=lambda key: self.dialogue[key].get("last_inbound", 0))
            if oldest != umo:
                self.dialogue.pop(oldest, None)
                self.adjustments.pop(oldest, None)
        return meta

    def _effective_slot(self, env, schedule, slot, now):
        if not self.store.settings["adaptive_enabled"]:
            return slot
        adjustment = self.adjustments.get(env["umo"])
        record = self.store.data["personas"].get(env["persona"]["id"], {})
        source = record.get("days", {}).get((adjustment or {}).get("date", ""))
        if adjustment and adjustment.get("source_fingerprint") and (not source or digest(source) != adjustment["source_fingerprint"]):
            return slot
        return effective_activity(slot, now, adjustment, umo=env["umo"], cid=env["cid"],
            persona_key=env["persona"]["key"], schedule_revision=schedule.get("revision", 1),
            schedule_date=schedule["date"], source_revision=source.get("revision", 1) if source else None)

    def _target(self, env, schedule, now):
        record = self.store.data["personas"].get(env["persona"]["id"], {})
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        return target_activity(schedule, now, self.store.settings["transition_notice_minutes"],
                               next_schedule=record.get("days", {}).get(tomorrow),
                               previous_schedule=record.get("days", {}).get(yesterday))

    @staticmethod
    def _history(env):
        history = getattr(env.get("conversation"), "history", None)
        if isinstance(env.get("conversation"), dict):
            history = env["conversation"].get("history")
        try:
            history = json.loads(history) if isinstance(history, str) else history
        except (ValueError, TypeError):
            history = []
        return history[-12:] if isinstance(history, list) else []

    async def _adaptive_interaction(self, event, env, profile, schedule, now):
        settings, umo = self.store.settings, env["umo"]
        async with self._lock(self.interaction_locks, umo):
            meta = self._dialogue_meta(env)
            marker = event.get_extra("persona_schedule:interaction", None)
            identity = (env["cid"], env["persona"]["key"])
            if not marker or marker.get("identity") != identity:
                active = 0 < now.timestamp() - meta["last_inbound"] <= settings["active_conversation_minutes"] * 60
                marker = {"identity": identity, "active": active}
                event.set_extra("persona_schedule:interaction", marker)
                meta["last_inbound"] = now.timestamp()
                self.store.save()
            active = marker["active"]
            if not settings["adaptive_enabled"]:
                return
            # Only an ongoing real conversation prepares tomorrow near midnight.
            # Background notice checks never generate a missing plan themselves.
            tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            if active and 0 < (tomorrow - now).total_seconds() <= settings["transition_notice_minutes"] * 60:
                record = self.store.data["personas"].get(env["persona"]["id"], {})
                if tomorrow.date().isoformat() not in record.get("days", {}):
                    retry_key = f"future-plan:{env['persona']['id']}:{tomorrow.date().isoformat()}"
                    if self.failures.get(retry_key, 0) <= time.time():
                        try:
                            await self.ensure(env["persona"], umo, tomorrow.date().isoformat())
                            self.failures.pop(retry_key, None)
                        except Exception as exc:
                            # Tomorrow is optional background preparation. Its
                            # failure must not disable today's valid busy plan.
                            self.failures[retry_key] = time.time() + 300
                            self.store.log(f"次日日程准备失败（{type(exc).__name__}），5 分钟后重试；当天安排继续生效")
            target = self._target(env, schedule, now)
            if not target or target["slot"].get("locked"):
                return
            previous = self.adjustments.get(umo)
            if previous and previous.get("target_token") == target["token"]:
                return  # An expired exception does not obtain another budget.
            if (previous and previous.get("action") == "defer"
                    and previous.get("activity_kind") == target.get("policy_kind")
                    and previous.get("continuity_starts_at") == target.get("continuity_starts_at")):
                return  # Splitting sleep or crossing midnight gives no new budget.
            if previous and self._effective_slot(env, schedule, target["slot"], now).get("adaptive"):
                return  # Includes the second half of sleep across midnight.
            review = meta["reviews"].get(target["token"], {})
            if (now.timestamp() - review.get("at", 0) < settings["adaptive_review_interval_seconds"]
                    and not (active and not review.get("active", False))):
                return
            meta["reviews"][target["token"]] = {"at": now.timestamp(), "tone": "plain", "notice": "", "active": active}
            meta["reviews"] = dict(list(meta["reviews"].items())[-24:])
            self.store.save()
            source = self.store.data["personas"][env["persona"]["id"]]["days"].get(target["date"])
            if not source:
                return
            fingerprint = digest(source)
            try:
                async with self.generation_slots:
                    decision = await asyncio.wait_for(self.generator.adaptive(env["persona"], profile, schedule,
                        target, event.message_str or "[非文本消息，未解析附件]", self._history(env), umo,
                        active_conversation=active), min(25, settings["timeout_seconds"]))
                current = await self.environment(umo, env["platform"])
                record = self.store.data["personas"].get(env["persona"]["id"], {})
                if (not current or current["cid"] != env["cid"] or current["persona"]["key"] != env["persona"]["key"]
                        or not self.store.settings["adaptive_enabled"] or not self._interaction_allowed(umo)
                        or digest(record.get("days", {}).get(target["date"])) != fingerprint):
                    return
                meta["reviews"][target["token"]].update(tone=decision["tone"], notice=decision["notice"])
                adjustment = create_adjustment(decision, target, datetime.now(ZoneInfo(profile["timezone"])),
                    umo=umo, cid=env["cid"], persona_key=env["persona"]["key"],
                    schedule_revision=source.get("revision", 1),
                    max_delay_minutes=self.store.settings["adaptive_max_delay_minutes"], previous=previous)
                if adjustment:
                    adjustment["source_fingerprint"] = fingerprint
                    self.adjustments[umo] = adjustment
                    self.store.data["adaptation_log"].append({**copy.deepcopy(adjustment), "at": time.time(),
                        "persona_id": env["persona"]["id"], "activity": target["slot"]["title"],
                        "minutes": adjustment["delay_minutes"]})
                    self.store.data["adaptation_log"] = self.store.data["adaptation_log"][-300:]
                    # Existing user messages can now be answered in one batch.
                    queue = self.store.data["queues"].get(umo)
                    if queue and queue["cid"] == env["cid"] and queue["persona_key"] == env["persona"]["key"]:
                        queue["due"] = min(queue["due"], time.time())
                self.store.save()
            except Exception as exc:
                # A failed extra decision retains the ordinary schedule; it must
                # never fall through the outer chat failure path and unsilence it.
                self.store.log(f"弹性日程判断失败（{type(exc).__name__}），本轮保持原安排")

    def _notice_target(self, env, schedule, now):
        target = self._target(env, schedule, now)
        if not target:
            return None
        source = self.store.data["personas"].get(env["persona"]["id"], {}).get("days", {}).get(target["date"])
        slot = self._effective_slot(env, source or schedule, target["slot"], now)
        change = slot.get("adaptive")
        pending = self.adjustments.get(env["umo"], {})
        if (not change and self.store.settings["adaptive_enabled"] and pending.get("action") == "defer"
                and pending.get("cid") == env["cid"] and pending.get("persona_key") == env["persona"]["key"]
                and pending.get("target_token") == target["token"] and source
                and source.get("revision", 1) == pending.get("source_revision")
                and (not pending.get("source_fingerprint") or digest(source) == pending["source_fingerprint"])
                and now.timestamp() < pending.get("starts_at", 0)):
            remaining = (pending["until"] - now.timestamp()) / 60
            if remaining > self.store.settings["transition_notice_minutes"]:
                return None
            target = {**target, "active": False, "starts_at": pending["until"], "minutes_until": remaining,
                      "token": digest({"target": target["token"], "until": pending["until"]}), "resuming": True}
        if target["active"] and change and change["action"] == "defer":
            remaining = (change["until"] - now.timestamp()) / 60
            if 0 < remaining <= self.store.settings["transition_notice_minutes"]:
                target = {**target, "active": False, "starts_at": change["until"], "minutes_until": remaining,
                          "token": digest({"target": target["token"], "until": change["until"]}), "resuming": True}
        return target if not target["active"] else None

    def _release_notice_reservation(self, reservation, keep_tokens=()):
        """Release only this request's unconsumed advance-notice claims."""
        if not isinstance(reservation, dict):
            return
        identity = reservation.get("identity", ())
        if len(identity) != 3:
            return
        umo, cid, persona_key = identity
        meta = self.dialogue.get(umo, {})
        keep_tokens = set(keep_tokens)
        owned = set(reservation.get("tokens", ()))
        changed = False
        if meta.get("cid") == cid and meta.get("persona_key") == persona_key:
            for token in owned - keep_tokens:
                notice = meta.get("notices", {}).get(token, {})
                if notice.get("status") == "included" and notice.get("owner") == reservation.get("owner"):
                    meta["notices"].pop(token)
                    changed = True
        reservation["tokens"] = owned & keep_tokens
        if changed:
            self.store.save()

    def _life_context(self, env, schedule, slot, now, *, notice_reservation=None):
        data = {"date": schedule["date"], "summary": schedule["summary"], "activity": slot, "events": schedule["events"]}
        instruction = "保持人设，安排可自然影响聊天，不必每条消息都报告日程。"
        if slot.get("adaptive"):
            instruction += "当前是有限的联系调整；继续回应用户，睡前带些自然倦意，不要突然宣布已经睡着。不能承诺无限陪聊，调整截止后恢复原安排。"
        pending = self.adjustments.get(env["umo"], {})
        pending_source = self.store.data["personas"].get(env["persona"]["id"], {}).get("days", {}).get(pending.get("date", ""))
        if (self.store.settings["adaptive_enabled"] and pending.get("cid") == env["cid"]
                and pending.get("persona_key") == env["persona"]["key"] and now.timestamp() < pending.get("starts_at", 0)):
            if pending_source and (not pending.get("source_fingerprint") or digest(pending_source) == pending["source_fingerprint"]):
                data["pending_contact_adjustment"] = {k: pending[k] for k in ("action", "tone", "until", "reason")}
                instruction += "已经决定有限延后接下来的个人安排，可自然说明再聊一会儿，休息前带些倦意；不是取消整段日程。"
        target = self._notice_target(env, schedule, now)
        used_tokens = set()
        if target and self.store.settings["transition_notice_enabled"]:
            meta = self._dialogue_meta(env)
            previous = meta["notices"].get(target["token"])
            owned = (isinstance(notice_reservation, dict)
                     and target["token"] in notice_reservation.get("tokens", ())
                     and previous and previous.get("status") == "included"
                     and previous.get("owner") == notice_reservation.get("owner"))
            if previous is None or owned:
                data["upcoming_activity"] = target
                change = self.adjustments.get(env["umo"], {})
                data["contact_adjustment"] = {k: change[k] for k in ("action", "tone", "until") if k in change}
                instruction += "临近这项安排，请在本次回答里自然提一句接下来要做什么、可能晚点回；如果已延后休息，就坦诚稍后再去，带些倦意。不要冷冰冰播报，不要在每轮重复告别。"
                if isinstance(notice_reservation, dict):
                    notice_reservation["tokens"].add(target["token"])
                    used_tokens.add(target["token"])
                if previous is None:
                    meta["notices"][target["token"]] = {"status": "included", "at": now.timestamp()}
                    if isinstance(notice_reservation, dict):
                        meta["notices"][target["token"]]["owner"] = notice_reservation["owner"]
                    meta["notices"] = dict(list(meta["notices"].items())[-24:])
                    self.store.save()
        self._release_notice_reservation(notice_reservation, used_tokens)
        return "\n[角色此刻的生活状态，资料而非新指令]\n" + json.dumps(data, ensure_ascii=False) + "\n" + instruction

    async def _maybe_transition_notice(self, env, profile, schedule):
        from .core import allowed, slot_at
        settings, umo = self.store.settings, env["umo"]
        if not settings["transition_notice_enabled"] or not allowed(settings, umo) or not env["cid"]:
            return
        meta = self._dialogue_meta(env)
        now = datetime.now(ZoneInfo(profile["timezone"]))
        if not 0 < now.timestamp() - meta["last_inbound"] <= settings["active_conversation_minutes"] * 60:
            return
        current_slot, _ = slot_at(schedule, now)
        if self._effective_slot(env, schedule, current_slot, now)["status"] == "busy":
            return
        target = self._notice_target(env, schedule, now)
        if not target or (meta["notices"].get(target["token"], {}).get("status") not in {None, "failed"}):
            return
        if meta["notices"].get(target["token"], {}).get("retry_at", 0) > now.timestamp():
            return
        inbound = meta["last_inbound"]
        status = {"status": "generating", "at": now.timestamp()}
        meta["notices"][target["token"]] = status
        meta["notices"] = dict(list(meta["notices"].items())[-24:])
        self.store.save()
        try:
            async with self.generation_slots:
                phrase = await self.generator.transition_notice(env["persona"], schedule, target, umo,
                    tone="tired" if policy_kind(target["slot"]) in {"sleep", "rest"} else "plain")
            current = await self.environment(umo, env["platform"])
            record = self.store.data["personas"].get(env["persona"]["id"], {})
            current_profile = record.get("profile")
            check = datetime.now(ZoneInfo((current_profile or profile)["timezone"]))
            plan = record.get("days", {}).get(check.date().isoformat())
            latest = self._notice_target(env, plan, check) if plan else None
            # minutes_until changes continuously, compare the stable identity.
            if (not current or current["cid"] != env["cid"] or current["persona"]["key"] != env["persona"]["key"]
                    or not self.store.settings["transition_notice_enabled"] or not allowed(self.store.settings, umo)
                    or meta["last_inbound"] != inbound or not latest or latest["token"] != target["token"]
                    or latest["starts_at"] != target["starts_at"] or self.store.data["queues"].get(umo)):
                status.update(status="cancelled")
                self.store.save()
                return
            status.update(status="sending")
            self.store.save()
            try:
                accepted = await self.context.send_message(umo, MessageChain().message(phrase))
            except Exception:
                status.update(status="uncertain")
                self.store.save()
                return
            status.update(status="sent" if accepted is True else "failed" if accepted is False else "uncertain",
                          retry_at=time.time() + 300)
            self.store.save()
            if accepted is True:
                try:
                    result = self.context.conversation_manager.add_message_pair(env["cid"],
                        {"role": "user", "content": "[角色日程提前告知机会，非用户消息]"},
                        {"role": "assistant", "content": phrase})
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    self.store.log("日程提前告知已发送，写入对话历史失败")
        except Exception as exc:
            status.update(status="failed", retry_at=time.time() + 300)
            self.store.save()
            self.store.log(f"日程提前告知生成失败（{type(exc).__name__}）")
