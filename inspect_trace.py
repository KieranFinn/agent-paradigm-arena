#!/usr/bin/env python3
"""Render a run's trace JSONL as a human-readable timeline.

Usage:
    python inspect_trace.py runs/<batch>/traces [--limit 3] [--grep failed]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def render(path: Path) -> str:
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    out = [f"== {path.name} ({len(events)} events) =="]
    peak = cost = ptok = ctok = 0
    first_action = None
    for e in events:
        kind = e["kind"]
        t = e["t_ms"]
        step = e["step"]
        if kind == "llm_call":
            p = e["payload"]
            label = p.get("label", "")
            tcs = ", ".join(tc["name"] for tc in p.get("tool_calls", []))
            out.append(f"[{t:>9.1f}ms] step={step:<3} LLM {label:<22} "
                       f"prompt={e['prompt_tokens']} completion={e['completion_tokens']} "
                       f"cached={e['cached_tokens']} cost={e['cost_cny']:.5f}"
                       + (f" tools=[{tcs}]" if tcs else ""))
            peak = max(peak, e["prompt_tokens"])
            cost += e["cost_cny"]
            ptok += e["prompt_tokens"]
            ctok += e["completion_tokens"]
        elif kind == "tool_call":
            if first_action is None:
                first_action = t
            out.append(f"[{t:>9.1f}ms] step={step:<3} TOOL_CALL {e['payload']['tool']}")
        elif kind == "tool_result":
            res = e["payload"]["result"]
            brief = res.get("summary") or res.get("error") or ("ok" if res.get("ok") else "failed")
            out.append(f"[{t:>9.1f}ms] step={step:<3}   -> {str(brief)[:120]}")
        elif kind == "plan":
            steps = e["payload"].get("steps", [])
            ids = ", ".join(f"{s['id']}:{s['status'][:4]}" for s in steps)
            out.append(f"[{t:>9.1f}ms] PLAN {e['payload'].get('trigger', '')} [{ids}]")
        elif kind == "eval":
            p = e["payload"]
            out.append(f"[{t:>9.1f}ms] EVAL epoch={p.get('epoch')} mode={p.get('mode')} ok={p.get('ok')}")
        elif kind == "reflection":
            out.append(f"[{t:>9.1f}ms] REFLECTION epoch={e['payload'].get('epoch')} "
                       f"{e['payload'].get('lesson', '')[:100]}")
        elif kind == "guard":
            out.append(f"[{t:>9.1f}ms] GUARD {e['payload'].get('tool')} {e['payload'].get('reason')}")
        elif kind == "final":
            p = e["payload"]
            out.append(f"[{t:>9.1f}ms] FINAL ok={p.get('ok')} "
                       f"failure_mode={p.get('failure_mode') or '-'}")

    out.append(f"-- peak_prompt={peak}  prompt_tokens={ptok}  completion_tokens={ctok}  "
               f"cost={cost:.4f} CNY  first_action={first_action if first_action is not None else 'n/a'}  "
               f"total_latency={events[-1]['t_ms'] if events else 0}ms")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help="trace .jsonl files or directories")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--grep", default=None, help="only files whose name contains this")
    args = ap.parse_args()

    files = []
    for p in map(Path, args.paths):
        if p.is_dir():
            files.extend(sorted(p.glob("*.jsonl")))
        else:
            files.append(p)
    if args.grep:
        files = [f for f in files if args.grep.lower() in f.name.lower()]
    files = files[: args.limit]

    if not files:
        print("no trace files found")
        return 1
    for f in files:
        print(render(f))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
