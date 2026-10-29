"""Explicit annual dates, including fictional dates mapped to Gregorian days.

Rules belong to one persona record. They are independent of the generated
annual calendar, so rebuilding a year cannot remove a user's anniversaries.
"""
from __future__ import annotations

import copy
import re
import uuid
from datetime import date, timedelta

from .core import KINDS, boolean, text


def validate_rules(rows):
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("重复活动应为数组，最多 100 项")
    result, ids = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("重复规则必须是对象")
        identity = row.get("id")
        if identity is None or identity == "":
            identity = uuid.uuid4().hex
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity):
            raise ValueError("重复规则 ID 无效")
        if identity in ids:
            raise ValueError("重复规则 ID 不能重复")
        ids.add(identity)
        title = text(row.get("title", ""), 160)
        if not title:
            raise ValueError("请填写重复活动名称")
        kind = row.get("kind", "anniversary")
        if kind not in KINDS:
            raise ValueError("重复活动种类无效")
        calendar = row.get("calendar", "gregorian")
        if calendar not in {"gregorian", "fictional"}:
            raise ValueError("重复规则支持公历或明确映射到公历的虚构日期；不支持固定公历冒充农历")
        month, day, duration = row.get("month"), row.get("day"), row.get("duration_days", 1)
        if type(month) is not int or type(day) is not int:
            raise ValueError("月份和日期必须是整数")
        try:
            date(2000, month, day)  # A leap year admits an explicit February 29.
        except (ValueError, TypeError):
            raise ValueError("月份或日期无效") from None
        if type(duration) is not int or not 1 <= duration <= 366:
            raise ValueError("活动持续时间应为 1～366 天")
        leap = row.get("leap_policy", "skip")
        if leap not in {"skip", "feb28"}:
            raise ValueError("闰日策略应为 skip 或 feb28")
        mapping = text(row.get("mapping", ""), 1500)
        if calendar == "fictional" and not mapping:
            raise ValueError("虚构日期需要说明原历法与公历日期的映射依据")
        bounds = {}
        for key in ("start_year", "end_year"):
            value = row.get(key)
            if value == "":
                value = None
            if value is not None and (type(value) is not int or not 1970 <= value <= 2100):
                raise ValueError("规则起止年份应为 1970～2100 的整数或留空")
            bounds[key] = value
        if (bounds["start_year"] is not None and bounds["end_year"] is not None
                and bounds["start_year"] > bounds["end_year"]):
            raise ValueError("规则结束年份不能早于开始年份")
        result.append({"id": identity, "title": title, "kind": kind, "calendar": calendar,
                       "month": month, "day": day, "duration_days": duration,
                       "leap_policy": leap, "mapping": mapping,
                       "description": text(row.get("description", ""), 2000),
                       "day_off": boolean(row.get("day_off", False)),
                       "enabled": boolean(row.get("enabled", True)), **bounds})
    return result


def expand_rules(rules, year):
    """Materialize intersecting occurrences without consulting a model or clock."""
    if type(year) is not int or not 1970 <= year <= 2100:
        raise ValueError("年份应在 1970～2100")
    rules = validate_rules(rules)
    events = []
    for rule in rules:
        if not rule["enabled"]:
            continue
        # Duration <= 366: only this year and the previous year can intersect.
        for anchor in (year - 1, year):
            if not (rule["start_year"] or 1970) <= anchor <= (rule["end_year"] or 2100):
                continue
            try:
                start = date(anchor, rule["month"], rule["day"])
            except ValueError:
                if (rule["month"], rule["day"], rule["leap_policy"]) != (2, 29, "feb28"):
                    continue
                start = date(anchor, 2, 28)
            end = start + timedelta(days=rule["duration_days"] - 1)
            if start.year > year or end.year < year:
                continue
            description = rule["description"]
            if rule["calendar"] == "fictional":
                description += ("\n" if description else "") + "虚构历法的公历映射：" + rule["mapping"]
            events.append({"id": f"recurrence:{rule['id']}:{anchor}",
                           "recurrence_id": rule["id"], "title": rule["title"],
                           "start": start.isoformat(), "end": end.isoformat(),
                           "kind": rule["kind"], "day_off": rule["day_off"],
                           "description": description, "origin": "recurring", "source_urls": []})
    return sorted(events, key=lambda row: (row["start"], row["title"], row["id"]))


def is_recurring_event(row):
    return (isinstance(row, dict) and (row.get("origin") == "recurring"
            or str(row.get("id", "")).startswith("recurrence:")))


def apply_recurrences(calendar, rules, year):
    """Overlay explicit rules, replacing stale derived rows and model duplicates."""
    result = copy.deepcopy(calendar)
    recurring = expand_rules(rules, year)
    keys = {(row["title"], row["start"], row["end"]) for row in recurring}
    base = []
    for row in result.get("events", []):
        if is_recurring_event(row):
            continue
        # Explicit annual edits are retained. Only inferred/model duplicates
        # are replaced by the deterministic recurring occurrence.
        if row.get("origin") != "manual" and (row["title"], row["start"], row["end"]) in keys:
            continue
        base.append(row)
    # Preserve ordinary calendars byte-for-byte when no rules affect them.
    if not recurring and base == result.get("events", []):
        return result
    result["events"] = sorted(base + recurring, key=lambda row: (row["start"], row["title"], row.get("id", "")))
    return result
