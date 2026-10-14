"""Bounded, session-local contact adjustments; the saved day stays unchanged."""
from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta

from .core import digest, minute, text


KINDS = {"sleep", "rest", "personal", "fixed", "unknown"}
ACTIONS = {"keep", "defer", "brief_exception"}
IMPORTANCE = {"normal", "important", "urgent"}
TONES = {"plain", "tired", "concerned"}

# The model handles arbitrary worlds and languages. These are conservative
# guards for common fixed commitments, never the only source of flexibility.
FIXED = re.compile(r"上课|授课|讲课|考试|应试|执勤|值班|值守|站岗|巡逻|战斗|作战|驾驶|开车|"
                   r"手术|急救|开会|会议|上班|工作|仕事|出勤|勤務|授業|試験|戦闘|運転|"
                   r"\b(?:class|exam|lecture|meeting|on duty|work shift|driving|battle|surgery)\b", re.I)
SLEEP = re.compile(r"睡|就寝|安寝|补眠|補眠|眠る|ねる|\b(?:sleep|bedtime|nap)\b", re.I)
REST = re.compile(r"休息|休憩|歇息|歇会|歇一会|\b(?:rest|relax|break)\b", re.I)


def policy_kind(slot):
    title = slot.get("title", "")
    if slot.get("locked") or FIXED.search(title):
        return "fixed"
    if SLEEP.search(title):
        return "sleep"
    if REST.search(title):
        return "rest"
    return "unknown"


def _aware(now):
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("自适应日程需要带角色时区的时间")


def _slot_token(day, slot):
    return digest({"date": day, "slot": slot})


def target_activity(schedule, now, notice_minutes=10, *, next_schedule=None, previous_schedule=None):
    """Find a busy boundary from the given saved days; never generate a new day.

    If tomorrow's cached day is supplied, its early busy slots can be announced
    before midnight. `target.date` and `target.source_revision` always identify
    the saved day that actually owns the slot, including tomorrow's plan.
    """
    _aware(now)
    if schedule.get("date") != now.date().isoformat():
        return None
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = midnight - timedelta(days=1)
    days = []
    if isinstance(previous_schedule, dict) and previous_schedule.get("date") == yesterday.date().isoformat():
        days.append((previous_schedule, yesterday))
    days.append((schedule, midnight))
    tomorrow = midnight + timedelta(days=1)
    if isinstance(next_schedule, dict) and next_schedule.get("date") == tomorrow.date().isoformat():
        days.append((next_schedule, tomorrow))
    candidates = []
    for source, day_start in days:
        for slot in sorted(source.get("slots", []), key=lambda row: row["start"]):
            start = day_start + timedelta(minutes=minute(slot["start"]))
            end = day_start + timedelta(minutes=minute(slot["end"], end=True))
            candidates.append((source, slot, start, end))
    for index, (source, slot, start, end) in enumerate(candidates):
        if slot.get("status") != "busy":
            continue
        if start <= now < end or now < start <= now + timedelta(minutes=max(0, notice_minutes)):
            kind, left, right = policy_kind(slot), index, index

            def same_rest(other):
                other_slot = candidates[other][1]
                return (other_slot.get("status") == "busy" and not other_slot.get("locked")
                        and (kind in {"sleep", "rest"} and policy_kind(other_slot) == kind
                             or kind == "unknown" and policy_kind(other_slot) == "unknown"
                             and other_slot.get("title") == slot.get("title")))

            while left > 0 and candidates[left - 1][3] == candidates[left][2] and same_rest(left - 1):
                left -= 1
            while right + 1 < len(candidates) and candidates[right][3] == candidates[right + 1][2] and same_rest(right + 1):
                right += 1
            group_start, group_end = candidates[left][2].timestamp(), candidates[right][3].timestamp()
            return {"slot": copy.deepcopy(slot), "date": source["date"],
                    "source_revision": source.get("revision", 1),
                    "starts_at": start.timestamp(), "ends_at": end.timestamp(),
                    "active": start <= now, "minutes_until": max(0, (start - now).total_seconds() / 60),
                    "token": _slot_token(source["date"], slot), "policy_kind": kind,
                    "continuity_starts_at": group_start, "continuity_ends_at": group_end,
                    "continuity_token": digest({"start": group_start, "end": group_end, "kind": kind})}
    return None


def validate_decision(obj, target, max_delay_minutes=30, remaining_minutes=None, *, active_conversation=True):
    """Reject malformed or policy-breaking model output instead of trusting it."""
    if not isinstance(obj, dict):
        raise ValueError("自适应决策必须是 JSON 对象")
    action, kind = obj.get("action"), obj.get("activity_kind")
    importance, tone = obj.get("importance"), obj.get("tone", "plain")
    delay = obj.get("delay_minutes", 0)
    if action not in ACTIONS or kind not in KINDS or importance not in IMPORTANCE or tone not in TONES:
        raise ValueError("自适应决策枚举不正确")
    if type(delay) is not int or delay < 0:
        raise ValueError("延后分钟数必须是非负整数")
    reason, notice = text(obj.get("reason", ""), 500), text(obj.get("notice", ""), 240)
    if not reason:
        raise ValueError("自适应决策需要简短原因")
    guard = target.get("policy_kind") or policy_kind(target["slot"])
    if guard != "unknown":
        kind = guard
    if action != "keep" and target["slot"].get("locked"):
        raise ValueError("锁定时段必须保持原安排")
    limit = min(120, max(0, int(max_delay_minutes)))
    if remaining_minutes is not None:
        limit = min(limit, max(0, int(remaining_minutes)))
    if action == "keep":
        if delay != 0:
            raise ValueError("保持安排时延后分钟数必须为 0")
    elif action == "defer":
        if kind not in {"sleep", "rest", "personal"}:
            raise ValueError("固定或未知活动不能延后")
        if not active_conversation and importance == "normal":
            raise ValueError("普通新消息不能自动延后活动，需有持续对话或重要事情")
        if not 1 <= delay <= limit:
            raise ValueError(f"延后分钟数必须在剩余预算 1～{limit} 内")
    else:
        if importance != "urgent":
            raise ValueError("短暂例外仅用于紧急而需要立即回应的事情")
        if not 1 <= delay <= min(5, limit):
            raise ValueError("紧急短暂回复例外最多 5 分钟，且不能超过剩余预算")
    if kind in {"sleep", "rest"} and action == "defer":
        tone = "tired"
    return {"action": action, "activity_kind": kind, "importance": importance,
            "delay_minutes": delay, "reason": reason, "tone": tone, "notice": notice}


def create_adjustment(decision, target, now, *, umo, cid, persona_key,
                      schedule_revision, max_delay_minutes=30, previous=None):
    """Create one finite contact window per slot, never renew it per message.

    Keep expired records until the source activity ends so the same conversation
    cannot repeatedly obtain a new exception. A sleep/rest window may cross
    midnight; all other windows stop at the original slot's end.
    """
    _aware(now)
    clean = validate_decision(decision, target, max_delay_minutes)
    if clean["action"] == "keep" or not umo or not cid or not persona_key:
        return None
    if previous and previous.get("target_token") == target["token"]:
        if all(previous.get(k) == v for k, v in (("umo", umo), ("cid", cid), ("persona_key", persona_key),
                                                ("source_revision", schedule_revision))):
            return copy.deepcopy(previous) if previous.get("until", 0) > now.timestamp() else None
    if (previous and clean["action"] == "defer" and previous.get("action") == "defer"
            and clean["activity_kind"] in {"sleep", "rest"}
            and previous.get("activity_kind") == clean["activity_kind"]
            and (previous.get("continuity_token") and previous.get("continuity_token") == target.get("continuity_token")
                 or previous.get("continuity_starts_at", previous["starts_at"]) <= target["starts_at"]
                 <= previous.get("continuity_ends_at", previous.get("source_ends_at", 0)))
            and all(previous.get(k) == v for k, v in (("umo", umo), ("cid", cid), ("persona_key", persona_key)))):
        # Midnight (or an arbitrary split in the saved day) does not create a
        # fresh budget for the very same continuous period of rest.
        return copy.deepcopy(previous) if previous.get("until", 0) > now.timestamp() else None
    # Deferral is measured from the planned start, never from the latest message.
    planned_start = (target.get("continuity_starts_at", target["starts_at"])
                     if clean["activity_kind"] in {"sleep", "rest"} else target["starts_at"])
    start = planned_start if clean["action"] == "defer" else max(now.timestamp(), target["starts_at"])
    cap = (planned_start + min(120, max(0, max_delay_minutes)) * 60
           if clean["action"] == "defer" else start + min(5, max_delay_minutes) * 60)
    until = min(start + clean["delay_minutes"] * 60, cap)
    if clean["activity_kind"] not in {"sleep", "rest"} or clean["action"] != "defer":
        until = min(until, target["ends_at"])
    if until <= max(now.timestamp(), start):
        return None
    return {"umo": umo, "cid": cid, "persona_key": persona_key, "date": target["date"],
            "source_revision": schedule_revision, "source_slot": copy.deepcopy(target["slot"]),
            "target_token": target["token"], "starts_at": start, "until": until,
            "continuity_starts_at": target.get("continuity_starts_at", target["starts_at"]),
            "continuity_ends_at": target.get("continuity_ends_at", target["ends_at"]),
            "continuity_token": target.get("continuity_token", target["token"]),
            "source_ends_at": target["ends_at"], "created_at": now.timestamp(), **clean}


def adjustment_valid(adjustment, slot, now, *, umo, cid, persona_key,
                     schedule_revision, schedule_date=None, source_revision=None):
    """Check identity, source revision and activity before exposing an exception."""
    _aware(now)
    if not isinstance(adjustment, dict) or not all(adjustment.get(k) == value for k, value in
            (("umo", umo), ("cid", cid), ("persona_key", persona_key))):
        return False
    if not adjustment.get("starts_at", float("inf")) <= now.timestamp() < adjustment.get("until", 0):
        return False
    if slot.get("locked") or slot.get("status") != "busy":
        return False
    if source_revision is not None and source_revision != adjustment.get("source_revision"):
        return False
    day = schedule_date or now.date().isoformat()
    if day == adjustment.get("date"):
        if schedule_revision != adjustment.get("source_revision"):
            return False
        if _slot_token(day, slot) == adjustment.get("target_token"):
            return True
        if all(slot.get(key) == adjustment.get("source_slot", {}).get(key) for key in ("start", "end")):
            return False
        if adjustment.get("action") != "defer" or adjustment.get("activity_kind") not in {"sleep", "rest"}:
            return False
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        slot_start = (midnight + timedelta(minutes=minute(slot["start"]))).timestamp()
        slot_end = (midnight + timedelta(minutes=minute(slot["end"], end=True))).timestamp()
        same_kind = (policy_kind(slot) == adjustment["activity_kind"] or policy_kind(slot) == "unknown"
                     and slot.get("title") == adjustment.get("source_slot", {}).get("title"))
        return (same_kind and adjustment.get("continuity_starts_at", float("inf")) <= slot_start
                and slot_end <= adjustment.get("continuity_ends_at", 0))
    if adjustment.get("action") != "defer" or adjustment.get("activity_kind") not in {"sleep", "rest"}:
        return False
    if source_revision is None:
        return False
    try:
        source_day = datetime.fromisoformat(adjustment["date"]).date()
        current_day = datetime.fromisoformat(day).date()
    except (ValueError, TypeError, KeyError):
        return False
    if current_day != source_day + timedelta(days=1) or policy_kind(slot) == "fixed":
        return False
    # The next day's unrelated revision is not comparable with the source day.
    # Caller should pass source_revision from the original saved day when crossing.
    kind = policy_kind(slot)
    return (kind == adjustment["activity_kind"] or
            kind == "unknown" and slot.get("title") == adjustment.get("source_slot", {}).get("title"))


def effective_activity(slot, now, adjustment=None, *, umo, cid, persona_key,
                       schedule_revision, schedule_date=None, source_revision=None):
    result = copy.deepcopy(slot)
    if adjustment_valid(adjustment, slot, now, umo=umo, cid=cid, persona_key=persona_key,
                        schedule_revision=schedule_revision, schedule_date=schedule_date,
                        source_revision=source_revision):
        # A short contact window is immediately reachable. The finite deadline
        # prevents slow-mode queue delays from silently extending the exception.
        result["status"] = "free"
        result["adaptive"] = {k: adjustment[k] for k in
                              ("action", "reason", "tone", "until", "activity_kind", "delay_minutes")}
    return result


def adjustment_summary(adjustment):
    """Facts for diaries, excluding identifiers and model-paraphrased user details.

    `reason` is deliberately omitted: a model might quote private user content
    despite the prompt. Keep that session-local detail out of persona-wide prose.
    """
    return {key: copy.deepcopy(adjustment[key]) for key in
            ("date", "action", "activity_kind", "delay_minutes", "tone", "starts_at", "until")
            if key in adjustment}
