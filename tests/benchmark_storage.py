"""Measure isolated synthetic JSON writes; never open an installed bot's state.

Run: python tests/benchmark_storage.py --iterations 15 --output .qa/storage-benchmark.json
The event-loop delay is a callback queued immediately before a synchronous save,
not an estimate of external platform latency or model performance.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import platform
import statistics
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
from astrbot_plugin_persona_schedule.core import Store


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def seed(store, personas, days, messages):
    slots = [{"start": f"{number * 3:02}:00", "end": f"{(number + 1) * 3:02}:00",
              "title": "合成测试活动", "status": "free", "note": "不含真实人设或用户信息" * 8,
              "locked": False} for number in range(8)]
    for index in range(personas):
        store.data["personas"][f"fixture-{index}"] = {"key": f"fixture-key-{index}", "profile": None,
            "calendars": {}, "locked_days": {}, "recurrences": [], "days": {}}
        for offset in range(days):
            day = (date(2026, 10, 30) - timedelta(days=offset)).isoformat()
            store.data["personas"][f"fixture-{index}"]["days"][day] = {
                "date": day, "timezone": "Asia/Shanghai", "slots": copy.deepcopy(slots),
                "events": [], "summary": "合成的一天" * 30, "origin": "generated", "revision": 1}
    store.data["diary_chat"] = {"messages": [{"id": f"fixture-{number}", "umo": "fixture:FriendMessage:1",
        "cid": "fixture", "persona_id": "fixture-0", "persona_key": "fixture-key-0", "at": 1800000000,
        "day": "2026-10-30", "timezone": "Asia/Shanghai", "role": "user", "text": "合成文字" * 75,
        "truncated": False} for number in range(messages)], "dropped": {}}


async def measure(name, personas, days, messages, iterations):
    with tempfile.TemporaryDirectory(prefix="persona-schedule-benchmark-") as directory:
        store = Store(directory)
        store.settings["backup_enabled"] = False
        seed(store, personas, days, messages)
        store.save()  # Warm filesystem and JSON encoder before sampling.
        durations, delays = [], []
        loop = asyncio.get_running_loop()
        for index in range(iterations):
            completion = loop.create_future()
            start = time.perf_counter()
            loop.call_soon(lambda future=completion, at=start: future.set_result((time.perf_counter() - at) * 1000))
            store.data["expired_messages"] = index
            store.save()
            durations.append(store.storage_metrics()["last_ms"])
            delays.append(await completion)
        return {"name": name, "personas": personas, "days_per_persona": days, "material_messages": messages,
                "iterations": iterations, "bytes": store.path.stat().st_size,
                "save_p50_ms": statistics.median(durations), "save_p95_ms": percentile(durations, .95),
                "save_max_ms": max(durations), "queued_callback_p95_ms": percentile(delays, .95),
                "backups_enabled_during_samples": False}


async def run(iterations):
    rows = []
    for args in (("small", 1, 7, 120), ("medium", 5, 30, 1000), ("large", 20, 45, 4000)):
        rows.append(await measure(*args, iterations))
    return {"mode": "isolated-synthetic", "python": platform.python_version(), "platform": platform.system(),
            "measured_at": time.time(), "cases": rows,
            "scope": "JSON serialization, synchronous file write/replace and queued callback delay; excludes real bot/model/platform traffic"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=15)
    parser.add_argument("--output")
    options = parser.parse_args()
    if not 3 <= options.iterations <= 100:
        parser.error("--iterations must be between 3 and 100")
    report = json.dumps(asyncio.run(run(options.iterations)), ensure_ascii=False, indent=2)
    if options.output:
        target = Path(options.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report + "\n", encoding="utf-8")
    print(report)
