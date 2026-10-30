"""Local checksummed state backups and narrowly scoped schedule interchange."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MAX_BACKUP_BYTES = 64 * 1024 * 1024
MAX_IMPORT_BYTES = 4 * 1024 * 1024
BACKUP_FORMAT = "persona-schedule-backup"
EXPORT_FORMAT = "persona-schedule-export"


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _checksum(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _folder(store):
    root = Path(store.root).resolve()
    folder = (root / "backups").resolve()
    if folder.parent != root:
        raise ValueError("备份目录必须位于插件数据目录内")
    folder.mkdir(exist_ok=True)
    return folder


def _path(store, identity):
    if not isinstance(identity, str) or not re.fullmatch(r"backup-[0-9T]+-[a-f0-9]{12}\.json", identity):
        raise ValueError("备份标识无效")
    folder = _folder(store)
    candidate = folder / identity
    path = candidate.resolve()
    if path.parent != folder or candidate.is_symlink():
        raise ValueError("备份路径无效")
    return path


def _validate_state(data):
    if not isinstance(data, dict):
        raise ValueError("备份数据不是对象")
    for key in ("settings", "personas", "sessions", "queues"):
        if not isinstance(data.get(key), dict):
            raise ValueError("备份缺少有效的状态字段")
    for key in ("personas", "sessions", "queues"):
        if any(not isinstance(value, dict) for value in data[key].values()):
            raise ValueError("备份记录格式无效")
    for queue in data["queues"].values():
        if not isinstance(queue.get("messages"), list) or any(not isinstance(row, dict) for row in queue["messages"]):
            raise ValueError("备份留言格式无效")
    if not isinstance(data.get("logs", []), list) or not isinstance(data.get("diaries", {}), dict):
        raise ValueError("备份归档格式无效")
    return data


def read_backup(store, identity):
    path = _path(store, identity)
    if path.stat().st_size > MAX_BACKUP_BYTES:
        raise ValueError("备份超过 64 MiB 读取上限")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("format") != BACKUP_FORMAT or value.get("version") != 1:
        raise ValueError("不支持的备份格式或版本")
    if value.get("sha256") != _checksum(value.get("data")):
        raise ValueError("备份完整性校验失败")
    return _validate_state(value["data"])


def list_backups(store):
    result = []
    for path in sorted(_folder(store).glob("backup-*.json"), reverse=True):
        if not re.fullmatch(r"backup-[0-9T]+-[a-f0-9]{12}\.json", path.name):
            continue
        row = {"id": path.name, "at": path.stat().st_mtime, "size": path.stat().st_size,
               "_modified_ns": path.stat().st_mtime_ns,
               "valid": False, "checksum": "", "persona_count": 0}
        try:
            data = read_backup(store, path.name)
            value = json.loads(path.read_text(encoding="utf-8"))
            if type(value.get("created_at")) not in (int, float) or not math.isfinite(value["created_at"]):
                raise ValueError("备份时间无效")
            row.update(at=value["created_at"], valid=True, checksum=value["sha256"],
                       persona_count=len(data["personas"]))
        except (ValueError, TypeError, KeyError, OSError):
            row["error"] = "备份损坏、格式不支持或无法读取"
        result.append(row)
    result.sort(key=lambda row: (row["at"], row["_modified_ns"], row["id"]), reverse=True)
    for row in result:
        row.pop("_modified_ns")
    return result


def create_backup(store, *, now=None):
    now = time.time() if now is None else now
    data = copy.deepcopy(_validate_state(store.data))
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%S")
    identity = f"backup-{stamp}-{uuid.uuid4().hex[:12]}.json"
    path = _path(store, identity)
    value = {"format": BACKUP_FORMAT, "version": 1, "created_at": now,
             "sha256": _checksum(data), "data": data}
    encoded = _encoded(value)
    if len(encoded) > MAX_BACKUP_BYTES:
        raise ValueError("完整备份超过 64 MiB，请先精简归档")
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)
    # Retain bounded local backups. Every candidate's absolute path is checked.
    limit = store.settings.get("backup_limit", 5)
    for old in list_backups(store)[max(1, int(limit)):]:
        _path(store, old["id"]).unlink()
    store._last_backup_at = now
    return {"id": identity, "at": now, "size": len(encoded), "valid": True,
            "checksum": value["sha256"], "persona_count": len(data["personas"])}


def maybe_backup(store, *, now=None):
    now = time.time() if now is None else now
    if not store.settings.get("backup_enabled", True):
        return
    last = getattr(store, "_last_backup_at", None)
    if last is None:
        last = max((row["at"] for row in list_backups(store) if row["valid"]), default=0)
        store._last_backup_at = last
    if now - last >= store.settings.get("backup_interval_minutes", 60) * 60:
        create_backup(store, now=now)


def recover_latest(store):
    """Return a validated local state; caller preserves corrupt input first."""
    for row in list_backups(store):
        if row["valid"]:
            data = copy.deepcopy(read_backup(store, row["id"]))
            from .core import DEFAULTS, validate_settings
            try:
                validate_settings(data.get("settings", {}), DEFAULTS)
            except (ValueError, TypeError, KeyError):
                continue
            # Recovery is explicit in the UI and never automatically sends old
            # queued text or re-enables timers from an old private snapshot.
            data["settings"]["enabled"] = False
            for queue in data["queues"].values():
                if isinstance(queue, dict):
                    queue.update(delivery="paused", reason="从备份恢复，请核对后手工处理旧留言")
            data["recovery_info"] = {"at": time.time(), "backup_id": row["id"],
                                     "requires_review": True}
            return data
    raise ValueError("状态文件损坏且没有可用备份，请恢复有效 state.json")


def _day(value):
    if not isinstance(value, str):
        raise ValueError("导入日期必须是字符串")
    parsed = date.fromisoformat(value)
    if not 1970 <= parsed.year <= 2100 or parsed.isoformat() != value:
        raise ValueError("导入日期应为 1970～2100 年内的 YYYY-MM-DD")
    return parsed


def clean_record(record):
    """Allowlist schedule fields; never import queues, identities or credentials."""
    from .core import (_normalise_slots, active_events, text, validate_events,
                       validate_profile, validate_slots)
    from .recurrence import apply_recurrences, validate_rules
    if not isinstance(record, dict):
        raise ValueError("导入角色资料必须是对象")
    profile = validate_profile(record["profile"]) if record.get("profile") is not None else None
    result = {"profile": profile, "calendars": {}, "days": {}, "locked_days": {},
              "recurrences": validate_rules(record.get("recurrences", []))}
    for key, maximum in (("calendars", 131), ("days", 2000), ("locked_days", 2000)):
        if not isinstance(record.get(key, {}), dict) or len(record.get(key, {})) > maximum:
            raise ValueError("导入日程数量或格式无效")
    for year, calendar in record.get("calendars", {}).items():
        if not isinstance(year, str) or not re.fullmatch(r"\d{4}", year) or not 1970 <= int(year) <= 2100:
            raise ValueError("导入年度无效")
        if not isinstance(calendar, dict):
            raise ValueError("导入年度活动必须是对象")
        events = validate_events(calendar.get("events", []), int(year), manual=True)
        result["calendars"][year] = apply_recurrences({"year": int(year), "events": events,
            "revision": 1, "origin": "manual"}, result["recurrences"], int(year))
    for day, schedule in record.get("days", {}).items():
        parsed = _day(day)
        if not isinstance(schedule, dict):
            raise ValueError("导入当天日程必须是对象")
        zone = text(schedule.get("timezone") or (profile or {}).get("timezone", "Asia/Shanghai"), 80)
        ZoneInfo(zone)
        calendar = result["calendars"].get(str(parsed.year))
        events = active_events(calendar["events"], day) if calendar else validate_events(schedule.get("events", []), parsed.year, manual=True)
        result["days"][day] = {"date": day, "timezone": zone, "summary": text(schedule.get("summary", ""), 1000),
            "slots": validate_slots(schedule.get("slots")), "events": events, "origin": "manual", "revision": 1}
    for day, locks in record.get("locked_days", {}).items():
        _day(day)
        rows = _normalise_slots(locks, allow_empty=True)
        rows.sort(key=lambda row: row["start"])
        if any(not row["locked"] for row in rows) or any(left["end"] > right["start"] for left, right in zip(rows, rows[1:])):
            raise ValueError("导入锁定安排重叠或未锁定")
        result["locked_days"][day] = rows
    return result


def export_persona(record, persona_id):
    clean = clean_record(record)
    value = {"format": EXPORT_FORMAT, "version": 1, "persona_id": str(persona_id)[:200],
             "created_at": time.time(), "record": clean, "sha256": _checksum(clean)}
    if len(_encoded(value)) > MAX_IMPORT_BYTES:
        raise ValueError("角色导出超过 4 MiB，请先精简手工日程")
    return value


def _read_import(payload):
    if isinstance(payload, str):
        if len(payload.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise ValueError("导入文件不能超过 4 MiB")
        payload = json.loads(payload)
    if not isinstance(payload, dict) or len(_encoded(payload)) > MAX_IMPORT_BYTES:
        raise ValueError("导入文件格式或大小无效")
    if payload.get("format") != EXPORT_FORMAT or payload.get("version") != 1:
        raise ValueError("仅支持版本 1 的角色日程导出文件")
    if payload.get("sha256") != _checksum(payload.get("record")):
        raise ValueError("导入资料完整性校验失败")
    return payload


def parse_import(payload):
    return clean_record(_read_import(payload).get("record"))


def preview_import(payload, current_record):
    from .workbench import _changes
    value = _read_import(payload)
    record = clean_record(value["record"])
    return {"record": record, "changes": _changes(current_record, record),
            "persona_id": str(value.get("persona_id", ""))[:200],
            "summary": {"days": len(record["days"]), "calendars": len(record["calendars"]),
                        "recurrences": len(record["recurrences"]), "has_profile": bool(record["profile"])}}
