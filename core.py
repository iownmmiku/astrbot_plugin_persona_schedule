from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULTS = {
    "enabled": False,
    "whitelist_enabled": False,
    "whitelist": [],
    "all_chats_enabled": False,
    "adaptive_enabled": True,
    "transition_notice_enabled": True,
    "transition_notice_minutes": 10,
    "adaptive_max_delay_minutes": 30,
    "adaptive_review_interval_seconds": 180,
    "active_conversation_minutes": 10,
    "provider_id": "",
    "schedule_provider_id": "",
    "reply_provider_id": "",
    "diary_provider_id": "",
    "diary_enabled": True,
    "diary_limit": 180,
    "auto_schedule_enabled": True,
    "auto_schedule_time": "00:00",
    "auto_schedule_personas": [],
    "canvas_integration_enabled": False,
    "timezone": "Asia/Shanghai",
    "slow_min_seconds": 60,
    "slow_max_seconds": 180,
    "tick_seconds": 15,
    "timeout_seconds": 90,
    "queue_limit": 60,
    "queue_expire_hours": 72,
    "search_enabled": False,
    "tavily_api_key": "",
}
STATES = {"free", "slow", "busy"}
LABELS = {"free": "空闲", "slow": "半忙", "busy": "忙碌"}
KINDS = {"festival", "vacation", "activity", "anniversary"}


def field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def text(value, limit=1000) -> str:
    if not isinstance(value, str):
        raise ValueError("文字字段必须是字符串")
    return value.strip()[:limit]


def boolean(value) -> bool:
    if not isinstance(value, bool):
        raise ValueError("开关必须为 true 或 false")
    return value


def minute(value: str, *, end=False) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError("时间格式应为 HH:MM")
    hour, part = map(int, value.split(":"))
    if end and (hour, part) == (24, 0):
        return 1440
    if not (0 <= hour < 24 and 0 <= part < 60):
        raise ValueError("时间超出范围")
    return hour * 60 + part


def _normalise_slots(rows, *, weekly=False, allow_empty=False):
    if not isinstance(rows, list) or not (0 if allow_empty else 1) <= len(rows) <= 120:
        raise ValueError("日程应包含 1～120 个时间段")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("时间段必须是对象")
        start, end = minute(row.get("start")), minute(row.get("end"), end=True)
        if start >= end:
            raise ValueError("结束时间必须晚于开始时间，跨午夜请拆成两段")
        state = row.get("status")
        if state not in STATES:
            raise ValueError("状态只能为 free / slow / busy")
        title = text(row.get("title", ""), 120)
        if not title:
            raise ValueError("请填写日程名称")
        clean = {"start": row["start"], "end": row["end"], "status": state,
                 "title": title, "note": text(row.get("note", ""), 800),
                 "locked": boolean(row.get("locked", False))}
        delay_keys = ("slow_min_seconds", "slow_max_seconds")
        if any(key in row for key in delay_keys):
            if not all(key in row for key in delay_keys):
                raise ValueError("时段最短和最长延迟必须一起设置")
            low, high = (row[key] for key in delay_keys)
            if any(type(value) is not int or not 5 <= value <= 7200 for value in (low, high)):
                raise ValueError("时段延迟应为 5～7200 秒的整数")
            if low > high:
                raise ValueError("时段最短延迟不能超过最长延迟")
            clean.update(zip(delay_keys, (low, high)))
        if weekly:
            days = row.get("days")
            if not isinstance(days, list) or not days or any(type(d) is not int or d not in range(1, 8) for d in days):
                raise ValueError("days 应为周一到周日的数字数组（1～7）")
            clean["days"] = sorted(set(days))
        result.append(clean)
    return result


def validate_slots(rows, *, weekly=False):
    result = _normalise_slots(rows, weekly=weekly)
    for day in range(1, 8) if weekly else [None]:
        selected = sorted((r for r in result if day is None or day in r["days"]), key=lambda r: r["start"])
        cursor = 0
        for row in selected:
            start = minute(row["start"])
            if start != cursor:
                raise ValueError(f"{'周' + str(day) if day else '当天'}日程有重叠或空缺，请完整覆盖 00:00～24:00")
            cursor = minute(row["end"], end=True)
        if cursor != 1440:
            raise ValueError("日程必须完整覆盖 00:00～24:00")
    return sorted(result, key=lambda r: (r["start"], r.get("days", [0])))


def slow_bounds(slot, settings):
    """Use a validated slot's delay override, otherwise the global interval."""
    return (slot.get("slow_min_seconds", settings["slow_min_seconds"]),
            slot.get("slow_max_seconds", settings["slow_max_seconds"]))


def merge_locked_slots(generated, previous, *, weekly=False):
    """Overlay prior locked slots on a complete candidate without moving locks.

    `previous` may be a complete schedule or a list of locked fragments. All
    returned rows are independently validated. Weekly schedules are merged per
    weekday, then identical rows are grouped back into a shared `days` list.
    """
    candidates = validate_slots(generated, weekly=weekly)
    locks = [row for row in _normalise_slots([] if previous is None else previous,
                                           weekly=weekly, allow_empty=True)
             if row["locked"]]

    def clock(value):
        return f"{value // 60:02d}:{value % 60:02d}"

    def merge_day(day_rows, day_locks):
        day_locks = sorted(day_locks, key=lambda row: row["start"])
        cursor = 0
        for row in day_locks:
            if minute(row["start"]) < cursor:
                raise ValueError("锁定时段不能重叠")
            cursor = minute(row["end"], end=True)
        result = [copy.deepcopy(row) for row in day_locks]
        for row in day_rows:
            start, end = minute(row["start"]), minute(row["end"], end=True)
            cursor = start
            for lock in day_locks:
                left, right = minute(lock["start"]), minute(lock["end"], end=True)
                if right <= cursor:
                    continue
                if left >= end:
                    break
                if cursor < left:
                    result.append({**row, "start": clock(cursor), "end": clock(left)})
                cursor = max(cursor, right)
                if cursor >= end:
                    break
            if cursor < end:
                result.append({**row, "start": clock(cursor), "end": clock(end)})
        return sorted(result, key=lambda row: row["start"])

    if not weekly:
        return validate_slots(merge_day(candidates, locks))
    grouped = {}
    for weekday in range(1, 8):
        select = lambda rows: [{k: copy.deepcopy(v) for k, v in row.items() if k != "days"}
                               for row in rows if weekday in row["days"]]
        for row in merge_day(select(candidates), select(locks)):
            key = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if key not in grouped:
                grouped[key] = {**row, "days": []}
            grouped[key]["days"].append(weekday)
    return validate_slots(list(grouped.values()), weekly=True)


def validate_profile(obj):
    if not isinstance(obj, dict):
        raise ValueError("角色档案必须为对象")
    world = obj.get("world_type")
    if world not in {"real", "fanfiction", "original"}:
        raise ValueError("world_type 应为 real / fanfiction / original")
    countries = obj.get("countries", [])
    if not isinstance(countries, list) or len(countries) > 8 or any(not isinstance(c, str) or not re.fullmatch(r"[A-Z]{2}", c) for c in countries):
        raise ValueError("countries 应为国家/地区代码数组，如 CN、JP；无关联时留空")
    tz = text(obj.get("timezone", "Asia/Shanghai"), 80)
    ZoneInfo(tz)
    return {"world_type": world, "world_name": text(obj.get("world_name", ""), 160),
            "countries": sorted(set(countries)), "timezone": tz,
            "identity": text(obj.get("identity", ""), 300),
            "lifestyle": text(obj.get("lifestyle", ""), 2500),
            "calendar_notes": text(obj.get("calendar_notes", ""), 2500),
            "weekly": validate_slots(obj.get("weekly"), weekly=True)}


def validate_events(rows, year, *, manual=False, source_urls=()):
    if not isinstance(rows, list) or len(rows) > 250:
        raise ValueError("活动应为数组，最多 250 项")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("活动必须为对象")
        start, end = date.fromisoformat(row.get("start", "")), date.fromisoformat(row.get("end", ""))
        if start > end or start.year > year or end.year < year or (end - start).days > 366:
            raise ValueError("活动日期应与选中年份相交，且不超过 366 天")
        kind = row.get("kind", "activity")
        if kind not in KINDS:
            raise ValueError("活动种类无效")
        title = text(row.get("title", ""), 160)
        if not title:
            raise ValueError("活动名称不能为空")
        urls = row.get("source_urls", [])
        if not isinstance(urls, list):
            raise ValueError("source_urls 必须是数组")
        urls = [u for u in urls if isinstance(u, str) and u.startswith("https://") and (manual or u in source_urls)][:8]
        origin = "manual" if manual else "reference" if urls else "generated"
        result.append({"id": text(row.get("id", uuid.uuid4().hex), 80), "title": title,
                       "start": start.isoformat(), "end": end.isoformat(), "kind": kind,
                       "day_off": boolean(row.get("day_off", False)),
                       "description": text(row.get("description", ""), 2000),
                       "origin": origin, "source_urls": urls})
    return sorted(result, key=lambda r: (r["start"], r["title"]))


def active_events(events, day):
    return [r for r in events if r["start"] <= day <= r["end"]]


def weekly_for_day(profile, day):
    weekday = date.fromisoformat(day).isoweekday()
    return [{k: v for k, v in row.items() if k != "days"}
            for row in profile["weekly"] if weekday in row["days"]]


def slot_at(schedule, now):
    at = now.hour * 60 + now.minute
    row = next(r for r in schedule["slots"] if minute(r["start"]) <= at < minute(r["end"], end=True))
    end = datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo) + timedelta(minutes=minute(row["end"], end=True))
    return row, end.timestamp()


def allowed(settings, umo):
    if not settings["enabled"]:
        return False
    if settings["whitelist_enabled"]:
        return umo in settings["whitelist"]
    return bool(settings.get("all_chats_enabled", False))


def validate_settings(payload, previous):
    if not isinstance(payload, dict):
        raise ValueError("设置必须为对象")
    result = copy.deepcopy(previous)
    for key in ("enabled", "whitelist_enabled", "all_chats_enabled", "search_enabled", "diary_enabled",
                "auto_schedule_enabled", "canvas_integration_enabled", "adaptive_enabled", "transition_notice_enabled"):
        if key in payload:
            result[key] = boolean(payload[key])
    for key in ("provider_id", "schedule_provider_id", "reply_provider_id", "diary_provider_id", "timezone", "tavily_api_key"):
        if key in payload:
            result[key] = text(payload[key], 500)
    ZoneInfo(result["timezone"])
    if "auto_schedule_time" in payload:
        minute(payload["auto_schedule_time"])
        result["auto_schedule_time"] = payload["auto_schedule_time"]
    if "auto_schedule_personas" in payload:
        rows = payload["auto_schedule_personas"]
        if not isinstance(rows, list) or len(rows) > 2000 or any(not isinstance(p, str) or not p.strip() or len(p) > 300 for p in rows):
            raise ValueError("主动日程人设应为有效的人设 ID 列表")
        result["auto_schedule_personas"] = sorted(set(p.strip() for p in rows))
    if "whitelist" in payload:
        rows = payload["whitelist"]
        if not isinstance(rows, list) or len(rows) > 2000 or any(not isinstance(u, str) or len(u) > 300 or u.count(":") < 2 for u in rows):
            raise ValueError("白名单应包含有效的 AstrBot 会话来源 ID")
        result["whitelist"] = sorted(set(rows))
    bounds = {"slow_min_seconds": (5, 3600), "slow_max_seconds": (5, 7200),
              "tick_seconds": (5, 60), "timeout_seconds": (10, 300),
              "queue_limit": (5, 200), "queue_expire_hours": (1, 168), "diary_limit": (7, 1095),
              "transition_notice_minutes": (1, 60), "adaptive_max_delay_minutes": (1, 120),
              "adaptive_review_interval_seconds": (30, 1800), "active_conversation_minutes": (1, 60)}
    for key, (low, high) in bounds.items():
        if key in payload:
            value = payload[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{key} 应为 {low}～{high} 的整数")
            result[key] = value
    if result["slow_min_seconds"] > result["slow_max_seconds"]:
        raise ValueError("最短延迟不能超过最长延迟")
    return result


class Store:
    """Small atomic snapshots; all callers mutate on AstrBot's event loop."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.json"
        self.data = {"settings": copy.deepcopy(DEFAULTS), "personas": {}, "sessions": {}, "queues": {}, "logs": [],
                     "expired_messages": 0, "diaries": {}}
        if self.path.exists():
            # Do not overwrite a damaged file with empty data.
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("角色日程数据格式损坏，请恢复 state.json 备份")
            self.data.update(loaded)
            self.data["settings"] = validate_settings(loaded.get("settings", {}), DEFAULTS)
        for queue in self.data["queues"].values():
            queue.setdefault("expired", 0)
            if queue.get("delivery") == "sending":
                queue["delivery"] = "uncertain"
                queue["reason"] = "上次发送期间进程退出，需人工核对是否已送达"
        self.save()

    @property
    def settings(self):
        return self.data["settings"]

    def save(self):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def log(self, message):
        self.data["logs"].append({"at": datetime.now(timezone.utc).isoformat(), "message": message})
        self.data["logs"] = self.data["logs"][-150:]
        self.save()

    def enqueue(self, umo, cid, persona_key, title, message, now, due, state, activity=""):
        queue = self.data["queues"].get(umo)
        if queue and (queue["cid"] != cid or queue["persona_key"] != persona_key):
            self.log("会话切换后清除了原角色的待回复消息")
            queue = None
        if queue is None:
            queue = {"cid": cid, "persona_key": persona_key, "created_at": now,
                     "due": due, "messages": [], "dropped": 0, "expired": 0,
                     "delivery": "pending", "reason": ""}
            self.data["queues"][umo] = queue
        # Never postpone a slow reply just because another message arrives.
        if state == "busy":
            queue["due"] = max(queue["due"], due)
        elif state == "free":
            queue["due"] = min(queue["due"], due)
        queue["messages"].append({"id": uuid.uuid4().hex, "at": now, "sender": title[:100],
                                  "text": message[:4000], "activity": activity[:200], "status": state})
        excess = max(0, len(queue["messages"]) - self.settings["queue_limit"])
        if excess:
            queue["messages"] = queue["messages"][excess:]
            queue["dropped"] += excess
        queue["created_at"] = min(row["at"] for row in queue["messages"])
        self.save()
        return queue

    def prune_expired(self, now, umo=None):
        """Expire individual messages, keeping recent arrivals and active sends.

        Returns the number removed in this call and persists any change. The
        queue counter survives partial cleanup; the global counter also records
        messages from queues which become empty and are removed.
        """
        cutoff = now - self.settings["queue_expire_hours"] * 3600
        targets = list(self.data["queues"]) if umo is None else [umo]
        total, changed = 0, False
        for origin in targets:
            queue = self.data["queues"].get(origin)
            if queue is None or queue["delivery"] == "sending":
                continue
            messages = queue["messages"]
            remaining = [row for row in messages if row["at"] > cutoff]
            expired = len(messages) - len(remaining)
            total += expired
            if expired:
                queue["expired"] = queue.get("expired", 0) + expired
                queue["messages"] = remaining
                changed = True
            if not remaining:
                self.data["queues"].pop(origin, None)
                changed = True
                continue
            created_at = min(row["at"] for row in remaining)
            if queue["created_at"] != created_at:
                queue["created_at"] = created_at
                changed = True
        if total:
            self.data["expired_messages"] = self.data.get("expired_messages", 0) + total
        if changed:
            self.save()
        return total

    def finish_batch(self, umo, ids):
        queue = self.data["queues"].get(umo)
        if not queue:
            return
        queue["messages"] = [r for r in queue["messages"] if r["id"] not in ids]
        if queue["messages"]:
            queue["delivery"] = "pending"
            queue["reason"] = ""
            queue["created_at"] = min(row["at"] for row in queue["messages"])
        else:
            self.data["queues"].pop(umo, None)
        self.save()
