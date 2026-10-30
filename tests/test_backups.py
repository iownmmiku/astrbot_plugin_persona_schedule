"""Backups are local, bounded, verified and never replay pending messages."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_schedule import Store, UMO, profile, slots
from astrbot_plugin_persona_schedule.backups import (MAX_IMPORT_BYTES, _checksum, create_backup,
    export_persona, list_backups, parse_import, preview_import, read_backup)


def record():
    return {"key": "private-persona-key", "profile": profile(), "calendars": {}, "locked_days": {},
            "recurrences": [], "days": {"2026-10-30": {"date": "2026-10-30", "timezone": "Asia/Shanghai",
                "summary": "原安排", "slots": slots(), "events": [], "revision": 2}}}


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.settings.update(backup_enabled=False, backup_limit=3)
        self.store.data["personas"]["traveler"] = record()
        self.store.save()

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_backup_is_checksummed_and_contains_private_full_state(self):
        self.store.settings["tavily_api_key"] = "private-key"
        self.store.data["diary_chat"] = {"messages": [{"text": "private-chat"}]}
        row = create_backup(self.store)
        restored = read_backup(self.store, row["id"])
        self.assertEqual(restored, self.store.data)
        self.assertTrue(row["valid"])
        self.assertEqual(row["persona_count"], 1)
        self.assertNotIn("private-key", json.dumps(list_backups(self.store)))

    def test_backup_rotation_never_exceeds_configured_limit(self):
        for value in range(7):
            self.store.data["expired_messages"] = value
            create_backup(self.store, now=1800000000 + value)
        rows = list_backups(self.store)
        self.assertEqual(len(rows), 3)
        self.assertEqual([read_backup(self.store, row["id"])["expired_messages"] for row in rows], [6, 5, 4])

    def test_interval_does_not_write_a_backup_for_every_message(self):
        self.store.settings.update(backup_enabled=True, backup_interval_minutes=60, backup_limit=5)
        self.store._last_backup_at = 1800000000
        count = len(list_backups(self.store))
        with patch("astrbot_plugin_persona_schedule.backups.time.time", return_value=1800000001):
            for _ in range(4):
                self.store.save()
        self.assertEqual(len(list_backups(self.store)), count)
        with patch("astrbot_plugin_persona_schedule.backups.time.time", return_value=1800003601):
            self.store.save()
        self.assertEqual(len(list_backups(self.store)), count + 1)

    def test_checksum_tampering_is_reported_and_never_imported(self):
        row = create_backup(self.store)
        path = Path(self.temp.name) / "backups" / row["id"]
        value = json.loads(path.read_text(encoding="utf-8"))
        value["data"]["expired_messages"] = 99
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "校验"):
            read_backup(self.store, row["id"])
        self.assertFalse(next(item for item in list_backups(self.store) if item["id"] == row["id"])["valid"])

    def test_corrupt_main_recovers_verified_backup_and_preserves_corrupt_file(self):
        self.store.settings["enabled"] = True
        self.store.enqueue(UMO, "cid", "persona", "name", "old pending", 10, 20, "busy")
        row = create_backup(self.store)
        self.store.path.write_text("{broken", encoding="utf-8")
        recovered = Store(self.temp.name)
        self.assertEqual(recovered.data["personas"], self.store.data["personas"])
        self.assertFalse(recovered.settings["enabled"])
        self.assertEqual(recovered.data["queues"][UMO]["delivery"], "paused")
        self.assertEqual(recovered.data["recovery_info"]["backup_id"], row["id"])
        damaged = Path(self.temp.name) / recovered.data["recovery_info"]["preserved_file"]
        self.assertEqual(damaged.read_text(encoding="utf-8"), "{broken")

    def test_unrecoverable_main_remains_untouched(self):
        root = Path(self.temp.name) / "without-backups"
        root.mkdir()
        path = root / "state.json"
        path.write_text("invalid-state", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "没有可用备份"):
            Store(root)
        self.assertEqual(path.read_text(encoding="utf-8"), "invalid-state")

    def test_deleted_main_recovers_existing_backup_without_replaying_queue(self):
        self.store.settings["enabled"] = True
        self.store.enqueue(UMO, "cid", "persona", "name", "old pending", 10, 20, "busy")
        row = create_backup(self.store)
        self.store.path.unlink()
        recovered = Store(self.temp.name)
        self.assertEqual(recovered.data["personas"], self.store.data["personas"])
        self.assertFalse(recovered.settings["enabled"])
        self.assertEqual(recovered.data["queues"][UMO]["delivery"], "paused")
        self.assertEqual(recovered.data["recovery_info"]["backup_id"], row["id"])
        self.assertNotIn("preserved_file", recovered.data["recovery_info"])

    def test_deleted_main_with_only_bad_backups_does_not_create_empty_state(self):
        create_backup(self.store)
        for path in (Path(self.temp.name) / "backups").glob("backup-*.json"):
            path.write_text("broken", encoding="utf-8")
        self.store.path.unlink()
        with self.assertRaisesRegex(ValueError, "没有可用备份"):
            Store(self.temp.name)
        self.assertFalse(self.store.path.exists())
        self.assertTrue(list((Path(self.temp.name) / "backups").glob("backup-*.json")))

    def test_invalid_latest_backup_falls_back_to_older_valid_one(self):
        older = create_backup(self.store, now=1800000000)
        latest = create_backup(self.store, now=1800000010)
        (Path(self.temp.name) / "backups" / latest["id"]).write_text("bad", encoding="utf-8")
        self.store.path.write_text("bad-main", encoding="utf-8")
        recovered = Store(self.temp.name)
        self.assertEqual(recovered.data["recovery_info"]["backup_id"], older["id"])

    def test_backup_error_does_not_invalidate_already_saved_state(self):
        self.store.data["expired_messages"] = 77
        with patch("astrbot_plugin_persona_schedule.backups.maybe_backup", side_effect=OSError("full")):
            self.store.save()
        self.assertEqual(json.loads(self.store.path.read_text(encoding="utf-8"))["expired_messages"], 77)
        self.assertEqual(self.store.storage_metrics()["backup_error"], "OSError")

    def test_backup_identifiers_cannot_escape_directory(self):
        for identity in ("../state.json", "D:/state.json", "backup-../../state.json", "state.json"):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                read_backup(self.store, identity)

    def test_share_export_excludes_secrets_chat_session_and_internal_identity(self):
        source = record()
        source.update(settings={"key": "secret"}, queues={UMO: "private"}, diary_chat={"messages": []},
                      system_prompt="private-persona-prompt", incidents={"ledger": "private"})
        value = export_persona(source, "traveler")
        raw = json.dumps(value)
        for forbidden in ("private-persona-key", "secret", UMO, "private-persona-prompt", "ledger", "diary_chat"):
            self.assertNotIn(forbidden, raw)
        clean = parse_import(value)
        self.assertEqual(clean["days"]["2026-10-30"]["slots"][0]["status"], "free")
        self.assertNotIn("key", clean)

    def test_import_preview_is_read_only_and_reports_changes(self):
        before = copy.deepcopy(self.store.data)
        source = record()
        source["days"]["2026-10-30"]["summary"] = "新安排"
        result = preview_import(export_persona(source, "traveler"), self.store.data["personas"]["traveler"])
        self.assertTrue(result["changes"])
        self.assertEqual(result["summary"]["days"], 1)
        self.assertEqual(self.store.data, before)
        self.assertTrue(preview_import(export_persona(source, "traveler"), {})["changes"])

    def test_import_validation_rejects_bad_format_size_checksum_and_overlap(self):
        value = export_persona(record(), "traveler")
        for modified in ({**value, "version": 99}, {**value, "sha256": "wrong"}, {"format": "other"}):
            with self.subTest(modified=modified), self.assertRaises(ValueError):
                parse_import(modified)
        with self.assertRaises(ValueError):
            parse_import(" " * (MAX_IMPORT_BYTES + 1))
        value["record"]["days"]["2026-10-30"]["slots"] *= 2
        value["sha256"] = _checksum(value["record"])
        with self.assertRaises(ValueError):
            parse_import(value)

    def test_import_revalidates_even_checksummed_untrusted_fields(self):
        value = export_persona(record(), "traveler")
        value["record"]["locked_days"] = {"2026-10-30": slots()}
        value["sha256"] = _checksum(value["record"])
        with self.assertRaisesRegex(ValueError, "锁定"):
            parse_import(value)

    def test_storage_metrics_report_real_bytes_and_bounded_timings(self):
        for _ in range(210):
            self.store.save()
        metrics = self.store.storage_metrics()
        self.assertEqual(metrics["bytes"], self.store.path.stat().st_size)
        self.assertEqual(metrics["sample_count"], 200)
        self.assertGreater(metrics["save_count"], 210)
        self.assertGreaterEqual(metrics["p95_ms"], metrics["p50_ms"])
        self.assertGreaterEqual(metrics["max_ms"], metrics["p95_ms"])
        before = metrics["failed_count"]
        with patch.object(Path, "write_bytes", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                self.store.save()
        self.assertEqual(self.store.storage_metrics()["failed_count"], before + 1)


if __name__ == "__main__":
    unittest.main()
