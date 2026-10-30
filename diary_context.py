"""Bounded, identity-scoped text materials for whitelist-only daily journals."""
from __future__ import annotations

from datetime import datetime
import time
from zoneinfo import ZoneInfo

from .core import digest

RETENTION_SECONDS = 8 * 86400
MAX_MESSAGES = 4000
MAX_PER_SESSION_DAY = 120
MAX_TEXT = 1000
PROMPT_MESSAGES = 160
PROMPT_CHARACTERS = 24000


def chat_enabled(settings, umo=None):
    enabled = bool(settings.get("enabled") and settings.get("diary_enabled")
                   and settings.get("diary_chat_enabled", True))
    return enabled and (umo is None or umo in settings.get("whitelist", []))


def prune_materials(store, now):
    """Revocation affects pending raw material; archived diary prose stays intact."""
    previous = store.data.get("diary_chat", {})
    rows = previous.get("messages", [])
    drops = previous.get("dropped", {})
    rows = [r for r in rows if r.get("at", 0) > now - RETENTION_SECONDS
            and chat_enabled(store.settings, r.get("umo"))]
    drops = {k: r for k, r in drops.items() if r.get("at", 0) > now - RETENTION_SECONDS
             and chat_enabled(store.settings, r.get("umo"))}
    result = {"messages": rows[-MAX_MESSAGES:], "dropped": dict(list(drops.items())[-MAX_MESSAGES:])}
    changed = previous != result
    if changed:
        store.data["diary_chat"] = result
    return changed


def _bucket(row):
    return digest([row["umo"], row["cid"], row["persona_id"], row["persona_key"], row["day"]])


def _identifier(env, role, token):
    return digest([env["umo"], env["cid"], env["persona"]["key"], role, token])


def _copy_materials(value):
    # Rows are immutable except for the explicitly replaced first-chat row.
    return {"messages": list(value.get("messages", [])),
            "dropped": {key: dict(row) for key, row in value.get("dropped", {}).items()}}


def _save_materials(store, materials, previous):
    store.data["diary_chat"] = materials
    try:
        store.save()
    except Exception:
        if previous is None:
            store.data.pop("diary_chat", None)
        else:
            store.data["diary_chat"] = previous
        raise


def _limit_materials(materials, row):
    rows = materials["messages"]
    bucket = _bucket(row)
    same = [r for r in rows if _bucket(r) == bucket]
    removed = same[:-MAX_PER_SESSION_DAY]
    removed_ids = {r["id"] for r in removed}
    rows[:] = [r for r in rows if r["id"] not in removed_ids]
    if len(rows) > MAX_MESSAGES:
        removed.extend(rows[:-MAX_MESSAGES])
        del rows[:-MAX_MESSAGES]
    for old in removed:
        key = _bucket(old)
        counter = materials["dropped"].setdefault(key, {k: old[k] for k in ("at", "umo", "persona_id", "persona_key", "day")})
        counter["count"] = counter.get("count", 0) + 1
    materials["dropped"] = dict(list(materials["dropped"].items())[-MAX_MESSAGES:])


def add_material(store, env, role, value, now, token):
    if not chat_enabled(store.settings, env["umo"]) or role not in {"user", "assistant"}:
        return False
    if not isinstance(value, str) or not value.strip():
        return False
    previous = store.data.get("diary_chat")
    prune_materials(store, now)
    materials = _copy_materials(store.data.get("diary_chat", {}))
    rows = materials["messages"]
    identifier = _identifier(env, role, token)
    if any(row["id"] == identifier for row in rows):
        return False
    record = store.data["personas"].get(env["persona"]["id"], {})
    profile = record.get("profile") if record.get("key") == env["persona"]["key"] else None
    tz = (profile or {}).get("timezone", store.settings["timezone"])
    local = datetime.fromtimestamp(now, ZoneInfo(tz))
    value = value.strip()
    row = {"id": identifier, "at": now, "umo": env["umo"], "cid": env["cid"],
           "persona_id": env["persona"]["id"], "persona_key": env["persona"]["key"],
           "day": local.date().isoformat(), "timezone": tz, "role": role,
           "text": value[:MAX_TEXT], "truncated": len(value) > MAX_TEXT}
    previous_sequence = store.data.get("diary_sequence", 0)
    row["sequence"] = previous_sequence + 1
    rows.append(row)
    _limit_materials(materials, row)
    store.data["diary_sequence"] = row["sequence"]
    try:
        _save_materials(store, materials, previous)
    except Exception:
        if previous_sequence:
            store.data["diary_sequence"] = previous_sequence
        else:
            store.data.pop("diary_sequence", None)
        raise
    return True


def bind_initial_material(store, original, current, token):
    """Bind one initial input after AstrBot creates its first conversation.

    The caller must verify the request's conversation against the current SDK
    environment. Existing conversation IDs can never be rebound after reset.
    """
    if (original["cid"] or not current["cid"]
            or not chat_enabled(store.settings, current["umo"])
            or (original["umo"], original["persona"]["id"], original["persona"]["key"])
            != (current["umo"], current["persona"]["id"], current["persona"]["key"])):
        return False
    previous = store.data.get("diary_chat", {})
    old_id = _identifier(original, "user", token)
    new_id = _identifier(current, "user", token)
    rows = previous.get("messages", [])
    source = next((row for row in rows if row["id"] == old_id), None)
    if (source is None or source["at"] <= time.time() - RETENTION_SECONDS
            or any(row["id"] == new_id for row in rows)):
        return False
    materials = _copy_materials(previous)
    rebound = {**source, "id": new_id, "cid": current["cid"]}
    materials["messages"] = [rebound if row["id"] == old_id else row for row in rows]
    _limit_materials(materials, rebound)
    _save_materials(store, materials, previous)
    return True


def select_materials(store, persona, day, timezone):
    enabled = chat_enabled(store.settings)
    cutoff = time.time() - RETENTION_SECONDS
    zone = ZoneInfo(timezone)
    rows = [r for r in store.data.get("diary_chat", {}).get("messages", [])
            if enabled and r.get("umo") in store.settings["whitelist"]
            and r.get("persona_id") == persona["id"] and r.get("persona_key") == persona["key"]
            and r.get("at", 0) > cutoff and datetime.fromtimestamp(r["at"], zone).date().isoformat() == day]
    # Stable order preserves user/assistant sequencing at equal timestamps.
    rows.sort(key=lambda r: r["at"])
    chosen, characters = [], 0
    for row in reversed(rows):
        if len(chosen) >= PROMPT_MESSAGES or characters + len(row["text"]) > PROMPT_CHARACTERS:
            break
        chosen.append(row)
        characters += len(row["text"])
    chosen.reverse()
    identities = sorted({(r["umo"], r["cid"]) for r in chosen})
    aliases = {identity: f"对话 {index + 1}" for index, identity in enumerate(identities)}
    dropped = sum(r.get("count", 0) for r in store.data.get("diary_chat", {}).get("dropped", {}).values()
                  if enabled and r.get("umo") in store.settings["whitelist"]
                  and r.get("persona_id") == persona["id"] and r.get("persona_key") == persona["key"]
                  and r.get("at", 0) > cutoff and datetime.fromtimestamp(r["at"], zone).date().isoformat() == day)
    return {"enabled": enabled, "message_count": len(chosen), "conversation_count": len(identities),
            "omitted_count": len(rows) - len(chosen) + dropped,
            "truncated": bool(dropped or len(chosen) < len(rows) or any(r.get("truncated") for r in chosen)),
            "messages": [{"conversation": aliases[(r["umo"], r["cid"])], "role": r["role"],
                          "time": datetime.fromtimestamp(r["at"], ZoneInfo(timezone)).strftime("%H:%M:%S"),
                          "text": r["text"]} for r in chosen]}


def material_summary(context):
    return {key: context[key] for key in ("enabled", "message_count", "conversation_count", "omitted_count", "truncated")}
