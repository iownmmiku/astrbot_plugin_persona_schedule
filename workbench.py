"""Read-only scenario previews and bounded, persona-scoped edit recovery."""
from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .adaptive import create_adjustment, effective_activity, target_activity
from .commands import is_command_event
from .core import allowed, boolean, digest, minute, slow_bounds, slot_at, text


HISTORY_LIMIT = 20
HISTORY_BYTES = 8 * 1024 * 1024
SNAPSHOT_BYTES = 2 * 1024 * 1024
LABELS = {"profile": "编辑档案与周表前", "calendar": "编辑年度活动前", "day": "编辑当天日程前",
          "recurrences": "编辑重复活动前", "restore": "恢复快照前"}


def _record_for_edit(records, persona):
    record = records.get(persona["id"])
    if not record or record.get("key") != persona["key"]:
        record = {"key": persona["key"], "profile": None, "calendars": {}, "days": {}, "locked_days": {}}
    return copy.deepcopy(record)


def _changes(before, after):
    if after is None:
        return []
    result = []
    if before.get("profile") != after.get("profile"):
        result.append("档案或每周作息变化")
    for key, label in (("calendars", "年度活动"), ("days", "日期日程"), ("locked_days", "日期锁定")):
        left, right = before.get(key, {}), after.get(key, {})
        changed = [day for day in set(left) | set(right) if left.get(day) != right.get(day)]
        if changed:
            result.append(f"{label}变化：{len(changed)} 项")
    if before.get("recurrences", []) != after.get("recurrences", []):
        result.append("重复活动规则变化")
    return result


def _next_contact(schedule, now, effective, next_schedule=None):
    if effective["status"] == "free":
        return now.timestamp()
    if effective["status"] == "slow":
        return None  # The caller returns the configured interval separately.
    for source, day_start in ((schedule, now.replace(hour=0, minute=0, second=0, microsecond=0)),
                              (next_schedule, now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))):
        for row in (source or {}).get("slots", []):
            starts = day_start + timedelta(minutes=minute(row["start"]))
            if starts > now and row["status"] != "busy":
                return starts.timestamp()
    return None


class WorkbenchMixin:
    def _archive_edit(self, persona, record_before, kind, day="", after=None):
        """Append a pre-edit snapshot; callers commit and save atomically after validation."""
        content = copy.deepcopy(record_before)
        if content.get("key") != persona["key"]:
            raise ValueError("人设已变化，不能为其他人设保存编辑快照")
        size = len(json.dumps(content, ensure_ascii=False).encode("utf-8"))
        if size > SNAPSHOT_BYTES:
            raise ValueError("角色日程资料超过 2 MiB，无法安全保存编辑快照，请先备份并精简历史手工日程")
        entry = {"id": uuid.uuid4().hex, "at": time.time(), "kind": kind, "date": day,
                 "label": LABELS.get(kind, "编辑安排前") + (" · " + day if day else ""),
                 "persona_key": persona["key"], "record": content, "fingerprint": digest(content),
                 "changes": _changes(content, after)}
        existing = self.store.data.setdefault("edit_history", {}).get(persona["id"], [])
        entries = [*existing, entry][-HISTORY_LIMIT:]
        while len(entries) > 1 and len(json.dumps(entries, ensure_ascii=False).encode("utf-8")) > HISTORY_BYTES:
            entries.pop(0)
        self.store.data["edit_history"][persona["id"]] = entries
        return entry["id"]

    def _commit_edit(self, persona, record_before, record_after, kind, day="", *, clear_adjustments=False):
        """Commit record/history together; a failed atomic file write rolls memory back."""
        pid, records = persona["id"], self.store.data["personas"]
        had_record, previous_record = pid in records, records.get(pid)
        had_root = "edit_history" in self.store.data
        histories = self.store.data.get("edit_history", {})
        had_history, previous_history = pid in histories, histories.get(pid)
        removed = {}
        try:
            snapshot_id = self._archive_edit(persona, record_before, kind, day, record_after)
            records[pid] = record_after
            if clear_adjustments:
                for umo, adjustment in list(self.adjustments.items()):
                    if adjustment.get("persona_key") == persona["key"]:
                        removed[umo] = self.adjustments.pop(umo)
            self.store.save()
            return snapshot_id
        except Exception:
            if had_record:
                records[pid] = previous_record
            else:
                records.pop(pid, None)
            if had_history:
                self.store.data["edit_history"][pid] = previous_history
            elif "edit_history" in self.store.data:
                self.store.data["edit_history"].pop(pid, None)
            if not had_root and not self.store.data.get("edit_history"):
                self.store.data.pop("edit_history", None)
            self.adjustments.update(removed)
            raise

    async def web_history(self, payload):
        persona = await self._persona(str(payload.get("persona_id", "")))
        entries = self.store.data.get("edit_history", {}).get(persona["id"], [])
        return {"persona_id": persona["id"], "limit": HISTORY_LIMIT,
                "entries": [{**{key: entry[key] for key in ("id", "at", "kind", "date", "label", "changes")},
                             "can_restore": entry.get("persona_key") == persona["key"]}
                            for entry in reversed(entries)]}

    async def web_restore(self, payload):
        persona = await self._persona(str(payload.get("persona_id", "")))
        snapshot_id = str(payload.get("id", ""))
        async with self._lock(self.persona_locks, persona["id"]):
            # A generation job may hold the lock while the user edits the
            # official persona. Never restore an old identity after that wait.
            await self._require_current_persona(persona)
            entry = next((entry for entry in self.store.data.get("edit_history", {}).get(persona["id"], [])
                          if entry["id"] == snapshot_id), None)
            if entry is None:
                raise ValueError("快照不存在，可能已被新编辑记录替换")
            saved = entry.get("record")
            if entry.get("persona_key") != persona["key"] or not saved or saved.get("key") != persona["key"]:
                raise ValueError("人设已变化，不能将旧版人设快照恢复到当前人设")
            if digest(saved) != entry.get("fingerprint"):
                raise ValueError("快照内容校验失败，未修改当前日程")
            current = _record_for_edit(self.store.data["personas"], persona)
            restored = copy.deepcopy(saved)
            for key in ("days", "calendars"):
                for day, item in restored.get(key, {}).items():
                    item["revision"] = max(item.get("revision", 0), current.get(key, {}).get(day, {}).get("revision", 0)) + 1
            # Restored source data must not inherit an exception from the edit
            # which was just undone, including a restored matching revision.
            undo_id = self._commit_edit(persona, current, restored, "restore", entry.get("date", ""), clear_adjustments=True)
        self.failures.clear()
        return {"saved": True, "restored_id": snapshot_id, "snapshot_id": undo_id}

    async def web_preview(self, payload):
        persona = await self._persona(str(payload.get("persona_id", "")))
        day, clock = str(payload.get("date", "")), str(payload.get("time", ""))
        self._year(day)
        minute(clock)
        evaluate = boolean(payload.get("evaluate", False))
        active = boolean(payload.get("active_conversation", False))
        message, umo = text(payload.get("message", ""), 4000), text(payload.get("umo", ""), 300)
        if umo and umo.count(":") < 2:
            raise ValueError("预演对话应为完整的 AstrBot 会话来源 ID")
        record = copy.deepcopy(self.store.data["personas"].get(persona["id"], {}))
        settings_fingerprint = digest(self.store.settings)
        adjustment_snapshot = copy.deepcopy(self.adjustments.get(umo)) if umo else None
        profile, schedule = record.get("profile"), record.get("days", {}).get(day)
        if record.get("key") != persona["key"] or not profile or not schedule:
            raise ValueError("所选日期没有当前人设已保存的日程，请先生成或保存，再运行只读预演")
        tz = ZoneInfo(schedule.get("timezone") or profile["timezone"])
        now = datetime.fromisoformat(day + "T" + clock).replace(tzinfo=tz)
        if now.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != now.replace(tzinfo=None):
            raise ValueError("这个当地时间处于夏令时跳时区间，请选择有效时间")
        base, end = slot_at(schedule, now)
        previous_day, next_day = ((now.date() + timedelta(days=offset)).isoformat() for offset in (-1, 1))
        target = target_activity(schedule, now, self.store.settings["transition_notice_minutes"],
                                 previous_schedule=record.get("days", {}).get(previous_day),
                                 next_schedule=record.get("days", {}).get(next_day))
        env = await self.environment(umo) if umo else None
        matched = bool(env and env["persona"]["key"] == persona["key"] and env["persona"]["id"] == persona["id"])
        try:
            config = self.context.get_config(umo) if umo else {}
        except Exception:
            config = {}
        command = is_command_event(SimpleNamespace(message_str=message), config)
        controlled = bool(umo and matched and allowed(self.store.settings, umo) and not command)
        if command:
            scope_reason = "已识别命令，绕过日程拦截"
        elif not umo:
            scope_reason = "未选择对话，仅预览角色安排"
        elif not matched:
            scope_reason = "所选对话当前人设与预演角色不同，实际不按这个角色拦截"
        elif not allowed(self.store.settings, umo):
            scope_reason = "所选对话未启用日程控制"
        else:
            scope_reason = "所选对话在日程控制范围内"
        effective = copy.deepcopy(base)
        previous = copy.deepcopy(adjustment_snapshot) if matched else None
        if previous and (previous.get("cid") != env["cid"] or previous.get("persona_key") != persona["key"]):
            previous = None
        source = record.get("days", {}).get((previous or {}).get("date", ""))
        if (self.store.settings["adaptive_enabled"] and matched
                and (not previous or not previous.get("source_fingerprint") or source and digest(source) == previous["source_fingerprint"])):
            effective = effective_activity(base, now, previous, umo=umo, cid=env["cid"], persona_key=persona["key"],
                schedule_revision=schedule.get("revision", 1), schedule_date=day,
                source_revision=source.get("revision", 1) if source else None)
        spent = bool(target and previous and (previous.get("target_token") == target["token"]
                     or previous.get("action") == "defer" and previous.get("activity_kind") == target.get("policy_kind")
                     and previous.get("continuity_starts_at") == target.get("continuity_starts_at")))
        if not spent and target and previous and matched:
            spent = bool(self._effective_slot(env, schedule, target["slot"], now).get("adaptive"))
        decision, model_evaluated, model_error = None, False, ""
        if evaluate:
            if not message:
                raise ValueError("模型情景判断需要填写一条模拟消息")
            if not self.store.settings["adaptive_enabled"]:
                model_error = "弹性联系调整已关闭，保持基础安排"
            elif command:
                model_error = "命令绕过日程，不调用判断模型"
            elif spent:
                model_error = "该会话本段联系调整已判断，沿用现有截止时间，不申请新的预算"
            elif not target or target["slot"].get("locked"):
                model_error = "附近没有可判断的忙碌边界，保持现有安排"
            else:
                try:
                    async with self.generation_slots:
                        decision = await asyncio.wait_for(self.generator.adaptive(persona, profile, schedule, target,
                            message, [], umo if matched else "", active_conversation=active),
                            min(25, self.store.settings["timeout_seconds"]))
                    simulated_umo, simulated_cid = (umo, env["cid"]) if matched else ("preview:FriendMessage:sandbox", "preview")
                    adjustment = create_adjustment(decision, target, now, umo=simulated_umo, cid=simulated_cid,
                        persona_key=persona["key"], schedule_revision=record["days"][target["date"]].get("revision", 1),
                        max_delay_minutes=self.store.settings["adaptive_max_delay_minutes"], previous=previous)
                    if decision["action"] != "keep" and not adjustment:
                        model_error = "模型建议的联系窗口按原计划开始计时，现已结束，保持原安排"
                    effective = effective_activity(base, now, adjustment, umo=simulated_umo, cid=simulated_cid,
                        persona_key=persona["key"], schedule_revision=schedule.get("revision", 1), schedule_date=day,
                        source_revision=record["days"][target["date"]].get("revision", 1)) if adjustment else effective
                    model_evaluated = True
                except Exception as exc:
                    decision = None
                    model_error = f"模型判断失败（{type(exc).__name__}），保留已有安排"
        current_persona = await self._persona(persona["id"])
        current_env = await self.environment(umo) if umo else None
        def identity(value):
            return (value["cid"], value["persona"]["key"], value["persona"]["id"]) if value else None
        if identity(current_env) != identity(env):
            raise ValueError("预演期间所选会话或人设已变化，请重新预演；未修改实际日程")
        if digest(self.store.settings) != settings_fingerprint or umo and digest(self.adjustments.get(umo)) != digest(adjustment_snapshot):
            raise ValueError("预演期间配置或联系调整已变化，请重新预演；未修改实际日程")
        if current_persona["key"] != persona["key"] or digest(self.store.data["personas"].get(persona["id"])) != digest(record):
            raise ValueError("预演期间人设或日程已变化，请重新预演；未修改实际日程")
        has_queue = bool(matched and self.store.data["queues"].get(umo))
        would_queue = controlled and (effective["status"] != "free" or has_queue)
        reason = ("忙碌时静默，恢复联系后合并补回" if effective["status"] == "busy" else
                  "半忙时延迟合并简短回复" if effective["status"] == "slow" else
                  "已有待回留言，本条加入队列保持顺序" if has_queue else "当前可正常回复")
        if not controlled:
            reason += "；" + scope_reason
        if model_error:
            reason += "；" + model_error
        return {"persona_id": persona["id"], "date": day, "time": clock, "timezone": str(tz), "cached": True,
                "read_only": True, "base_activity": copy.deepcopy(base), "effective_activity": effective,
                "status": effective["status"], "scope": {"enabled": self.store.settings["enabled"],
                    "allowed": controlled, "command_bypass": command, "reason": scope_reason},
                "would_queue": would_queue, "delay_seconds": list(slow_bounds(effective, self.store.settings)) if effective["status"] == "slow" else None,
                "upcoming_activity": target, "next_contact_at": _next_contact(schedule, now, effective, record.get("days", {}).get(next_day)),
                "decision": decision, "model_evaluated": model_evaluated, "reason": reason}
