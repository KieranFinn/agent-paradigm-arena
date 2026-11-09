#!/usr/bin/env python3
"""Rebuild report.csv for a batch whose runner was killed before reports were
written. All metrics live in the per-run trace JSONL files (written
incrementally); raw.jsonl supplies wall-clock. Loses only score/checks detail
(kept blank) - every table column used by the analysis is recovered exactly.

Usage: python3 reconstruct_rows.py runs/main_2026xxxx
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


def rebuild(trace_path: Path, wall_ms: float) -> dict:
    events = [json.loads(l) for l in open(trace_path, encoding="utf-8") if l.strip()]
    llm = [e for e in events if e["kind"] == "llm_call"]
    final = next((e for e in reversed(events) if e["kind"] == "final"), None)
    name = trace_path.stem  # agent_task_difficulty_rN
    agent, task, diff, rep = name.rsplit("_", 3)[0], *name.rsplit("_", 3)[1:]
    rep = int(rep[1:])
    peak = max((e["prompt_tokens"] for e in llm), default=0)
    prompt = sum(e["prompt_tokens"] for e in llm)
    completion = sum(e["completion_tokens"] for e in llm)
    cached = sum(e["cached_tokens"] for e in llm)
    cost = sum(e["cost_cny"] for e in llm)
    first_action = next((e["t_ms"] for e in events if e["kind"] in ("tool_call", "plan")), None)
    total_lat = events[-1]["t_ms"] if events else 0
    ok = bool(final and final["payload"].get("ok"))
    return {
        "run_id": name,
        "agent": agent, "task": task, "difficulty": diff, "repeat": rep,
        "model": llm[0]["model"] if llm else "",
        "ok": ok,
        "score": 1.0 if ok else 0.0,
        "checks": "", "detail": "",
        "failure_mode": (final["payload"].get("failure_mode") if final else "") or "",
        "status": "completed" if ok else "reconstructed_unknown",
        "final_answer": (final["payload"].get("answer") if final else "") or "",
        "steps": sum(1 for e in llm if e["payload"].get("tool_calls")),
        "llm_calls": len(llm),
        "prompt_tokens": prompt, "completion_tokens": completion, "cached_tokens": cached,
        "peak_prompt_tokens": peak, "cost_cny": round(cost, 6),
        "first_action_ms": first_action, "total_latency_ms": total_lat,
        "wall_ms": round(wall_ms, 1),
        "replans": sum(1 for e in events if e["kind"] == "plan"
                       and "replan" in str(e["payload"].get("trigger", ""))),
        "epochs": sum(1 for e in events if e["kind"] == "eval") or 1,
        "reflections": sum(1 for e in events if e["kind"] == "reflection"),
        "guard_events": sum(1 for e in events if e["kind"] == "guard"),
        "effects_count": sum(1 for e in events if e["kind"] == "tool_call"),
        "error": "",
    }


def main() -> int:
    batch = Path(sys.argv[1])
    walls = {}
    raw = batch / "raw.jsonl"
    if raw.exists():
        for line in open(raw, encoding="utf-8"):
            d = json.loads(line)
            walls[d["run_id"]] = d.get("wall_ms", 0.0)
    rows = []
    for tp in sorted((batch / "traces").glob("*.jsonl")):
        if tp.stem not in walls and not raw.exists():
            continue
        rows.append(rebuild(tp, walls.get(tp.stem, 0.0)))
    if not rows:
        print("nothing to rebuild")
        return 1
    out = batch / "report.csv"
    cols = list(rows[0].keys())
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    ok = sum(1 for r in rows if r["ok"])
    print(f"rebuilt {len(rows)} rows -> {out} (ok {ok}/{len(rows)}, "
          f"cost {sum(r['cost_cny'] for r in rows):.4f} CNY)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
