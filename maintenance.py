"""Versioned editor writes and reviewed, persona-only data recovery."""
from __future__ import annotations

import copy
import json
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .core import digest
from .workbench import _changes, _record_for_edit


class EditConflict(ValueError):
    pass


class MaintenanceMixin:
    def _diary_tracking(self, persona, day, source):
        from .diary_context import chat_enabled, RETENTION_SECONDS
        zone = ZoneInfo(source["schedule"].get("timezone") or source["profile"]["timezone"])
        cursor = max((row.get("sequence", 0) for row in self.store.data.get("diary_chat", {}).get("messages", [])
                      if row.get("persona_id") == persona["id"] and row.get("persona_key") == persona["key"]
                      and chat_enabled(self.store.settings, row["umo"]) and row.get("at", 0) > time.time() - RETENTION_SECONDS
                      and datetime.fromtimestamp(row["at"], zone).date().isoformat() == day), default=0)
        return {"plan": digest({"profile": source["profile"], "schedule": source["schedule"]}), "chat_cursor": cursor}

    def _diary_change_flags(self, persona, day, entry):
        reasons = []
        if entry and entry.get("persona_key") == persona["key"] and entry.get("source_tracking"):
            try:
                current = self._diary_tracking(persona, day, self._diary_source(persona, day))
                previous = entry["source_tracking"]
                if current["plan"] != previous["plan"]:
                    reasons.append("归档后日程或角色生活资料已更新")
                if current["chat_cursor"] > previous.get("chat_cursor", 0):
                    reasons.append("归档后新增了属于这一天的白名单素材")
            except (ValueError, KeyError):
                pass
        return {"source_changed": bool(reasons), "source_change_reasons": reasons}

    def _edit_token(self, persona):
        events = [{k: row.get(k) for k in ("id", "status", "started_at", "ends_at", "actual_end", "incident")}
                  for row in self.store.data.get("incidents", {}).values()
                  if row.get("persona_key") == persona["key"] and row.get("started_at")]
        return digest({"persona": persona["key"], "record": _record_for_edit(self.store.data["personas"], persona),
                       "incidents": sorted(events, key=lambda row: row.get("id") or "")})

    def _require_edit_token(self, persona, payload):
        expected = payload.get("expected_token")
        if not isinstance(expected, str) or not expected:
            raise EditConflict("编辑冲突：缺少读取版本，请刷新页面后重试；当前草稿仍保留")
        if expected != self._edit_token(persona):
            raise EditConflict("编辑冲突：资料已更新，请比较最新安排后重新保存；当前草稿仍保留")

    async def web_backups(self, payload):
        from .backups import list_backups
        return {"backups": list_backups(self.store), "storage_metrics": self.store.storage_metrics(),
                "settings": {k: self.store.settings[k] for k in ("backup_enabled", "backup_limit", "backup_interval_minutes")}}

    async def web_backup_create(self, payload):
        from .backups import create_backup
        return {"saved": True, "backup": create_backup(self.store)}

    async def web_export(self, payload):
        from .backups import export_persona
        persona = await self._persona(str(payload.get("persona_id", "")))
        return export_persona(_record_for_edit(self.store.data["personas"], persona), persona["id"])

    async def web_import_preview(self, payload):
        from .backups import export_persona, parse_import, read_backup
        persona = await self._persona(str(payload.get("persona_id", "")))
        data = payload.get("data")
        if payload.get("backup_id"):
            saved = read_backup(self.store, str(payload["backup_id"]))
            record = saved.get("personas", {}).get(persona["id"])
            if not record:
                raise ValueError("备份中没有这个人设的安排")
            if record.get("key") != persona["key"]:
                raise ValueError("备份属于旧版人设，不能恢复到当前身份")
            data = export_persona(record, persona["id"])
        if isinstance(data, str):
            if len(data.encode("utf-8")) > 2 * 1024 * 1024:
                raise ValueError("导入文件超过 2 MiB")
            data = json.loads(data)
        if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("导入文件超过 2 MiB")
        imported = parse_import(data)
        imported["key"] = persona["key"]
        async with self._lock(self.persona_locks, persona["id"]):
            await self._require_current_persona(persona)
            current = _record_for_edit(self.store.data["personas"], persona)
            baseline = self._edit_token(persona)
            changes = _changes(current, imported)
            previews = getattr(self, "import_previews", {})
            previews = {key: item for key, item in previews.items() if item["expires_at"] > time.time()}
            while len(previews) >= 20:
                previews.pop(next(iter(previews)))
            token = uuid.uuid4().hex
            previews[token] = {"persona": persona, "record": imported, "baseline": baseline,
                               "expires_at": time.time() + 600}
            self.import_previews = previews
        summary = {"days": len(imported.get("days", {})), "calendars": len(imported.get("calendars", {})),
                   "recurrences": len(imported.get("recurrences", []))}
        return {"preview_token": token, "expected_token": baseline, "changes": changes, "summary": summary,
                "persona_id": persona["id"], "expires_in": 600}

    async def web_import_apply(self, payload):
        persona = await self._persona(str(payload.get("persona_id", "")))
        token = str(payload.get("preview_token", ""))
        async with self._lock(self.persona_locks, persona["id"]):
            await self._require_current_persona(persona)
            self._require_edit_token(persona, payload)
            preview = getattr(self, "import_previews", {}).get(token)
            if not preview or preview["expires_at"] <= time.time():
                raise ValueError("恢复预览已过期，请重新预览")
            if preview["persona"]["key"] != persona["key"] or preview["persona"]["id"] != persona["id"]:
                raise ValueError("恢复预览不属于当前人设")
            if preview["baseline"] != self._edit_token(persona):
                raise EditConflict("编辑冲突：预览后安排已更新，请重新预览；尚未恢复")
            current = _record_for_edit(self.store.data["personas"], persona)
            restored = copy.deepcopy(preview["record"])
            for kind in ("days", "calendars"):
                for key, row in restored.get(kind, {}).items():
                    row["revision"] = max(row.get("revision", 0), current.get(kind, {}).get(key, {}).get("revision", 0)) + 1
            snapshot_id = self._commit_edit(persona, current, restored, "import", clear_adjustments=True)
            self.import_previews.pop(token, None)
        self.failures.clear()
        return {"saved": True, "snapshot_id": snapshot_id, "edit_token": self._edit_token(persona)}
