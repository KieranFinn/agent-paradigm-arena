#!/usr/bin/env python3
"""Merge batch directories into one analysis set and produce the blog-facing
statistics: the 4x3x2 matrix (all collected data, N per cell reported
honestly), a symmetric N=3 subsample cross-check, hypothesis checks (H1-H6
where data exists), and per-agent structural metrics.

Usage:
    python3 analyze_merged.py runs/main_* runs/m2-* --out runs/_merged
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agent_arena.frameworks import AGENTS  # noqa: E402
import run_benchmark as rb  # noqa: E402

TASK_ORDER = ("T1", "T2", "T3")
DIFF_ORDER = ("easy", "hard")
HOME_TURF = {("react", "T1"), ("plan_execute", "T2"), ("reflexion", "T3")}


def load_rows(batch_dirs: List[Path]) -> list:
    rows = []
    for d in batch_dirs:
        csv_path = d / "report.csv"
        if not csv_path.exists():
            continue
        with open(csv_path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                r["source_batch"] = d.name
                for k in ("ok",):
                    r[k] = r[k] == "True"
                for k in ("repeat", "steps", "llm_calls", "prompt_tokens", "completion_tokens",
                          "cached_tokens", "peak_prompt_tokens", "replans", "epochs",
                          "reflections", "guard_events", "effects_count"):
                    r[k] = int(r[k] or 0)
                for k in ("score", "cost_cny", "first_action_ms", "total_latency_ms", "wall_ms"):
                    r[k] = float(r[k] or 0)
                rows.append(r)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("batches", nargs="+")
    ap.add_argument("--out", default="runs/_merged")
    args = ap.parse_args()

    batch_dirs = []
    for b in args.batches:
        p = Path(b)
        if p.is_dir():
            batch_dirs.append(p)
        else:
            for q in sorted(ROOT.glob(b)):
                if q.is_dir():
                    batch_dirs.append(q)
    rows = load_rows(batch_dirs)
    if not rows:
        print("no rows found")
        return 1

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # subsample symmetric N=3 view
    by_cell: dict = {}
    for r in rows:
        by_cell.setdefault((r["agent"], r["task"], r["difficulty"]), []).append(r)

    # ---- merged report ----------------------------------------------------
    L = []
    L.append("# Merged analysis (all batches, honest N per cell)\n")
    L.append(f"- batches: {', '.join(d.name for d in batch_dirs)}")
    L.append(f"- runs: {len(rows)}  cost: {sum(r['cost_cny'] for r in rows):.4f} CNY  "
             f"successes: {sum(1 for r in rows if r['ok'])}/{len(rows)}\n")

    L.append("## Matrix (full data)\n")
    for diff in DIFF_ORDER:
        L.append(f"### {diff}\n")
        L.append("| agent | T1 explore | T2 plan | T3 verify |")
        L.append("|---|---|---|---|")
        for agent in AGENTS:
            cells = []
            for t in TASK_ORDER:
                rs = by_cell.get((agent, t, diff), [])
                if not rs:
                    cells.append("—")
                    continue
                mark = " ✅" if (agent, t) in HOME_TURF else ""
                cells.append(f"{rb.fmt_ci(sum(1 for r in rs if r['ok']), len(rs))}{mark}"
                             f" (n={len(rs)})")
            L.append(f"| {agent} | " + " | ".join(cells) + " |")
        L.append("")

    L.append("## Matrix (symmetric subsample, first 3 per cell)\n")
    for diff in DIFF_ORDER:
        L.append(f"### {diff}\n")
        L.append("| agent | T1 explore | T2 plan | T3 verify |")
        L.append("|---|---|---|---|")
        for agent in AGENTS:
            cells = []
            for t in TASK_ORDER:
                rs = sorted(by_cell.get((agent, t, diff), []), key=lambda r: r["run_id"])[:3]
                if not rs:
                    cells.append("—")
                    continue
                mark = " ✅" if (agent, t) in HOME_TURF else ""
                cells.append(f"{rb.fmt_ci(sum(1 for r in rs if r['ok']), len(rs))}{mark}")
            L.append(f"| {agent} | " + " | ".join(cells) + " |")
        L.append("")

    # ---- structural metrics per agent ---------------------------------------
    L.append("## Structural metrics per agent (medians over all runs)\n")
    L.append("| agent | n | cost CNY | peak prompt | cum prompt | cum/peak | completion | llm calls | first action s | sim latency s | real wall s |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for agent in AGENTS:
        rs = [r for r in rows if r["agent"] == agent]
        if not rs:
            continue
        peak = rb.median([r["peak_prompt_tokens"] for r in rs])
        cum = rb.median([r["prompt_tokens"] for r in rs])
        comp = rb.median([r["completion_tokens"] for r in rs])
        ratio = (cum / peak) if peak else 0
        fa = [r["first_action_ms"] for r in rs if r["first_action_ms"]]
        L.append(
            f"| {agent} | {len(rs)} | {rb.median([r['cost_cny'] for r in rs]):.4f} "
            f"| {peak:.0f} | {cum:.0f} | {ratio:.1f}x | {comp:.0f} "
            f"| {rb.median([r['llm_calls'] for r in rs]):.0f} "
            f"| {rb.median(fa)/1000 if fa else float('nan'):.1f} "
            f"| {rb.median([r['total_latency_ms'] for r in rs])/1000:.1f} "
            f"| {rb.median([r['wall_ms'] for r in rs])/1000:.1f} |"
        )
    L.append("")

    # per task x agent detail (cost/latency/peak)
    L.append("## Per-cell detail (median cost CNY / sim latency s / peak prompt)\n")
    L.append("| agent | task | diff | n | ok | cost | lat | peak | cum | llm | replans | epochs | refl |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for agent in AGENTS:
        for t in TASK_ORDER:
            for diff in DIFF_ORDER:
                rs = by_cell.get((agent, t, diff), [])
                if not rs:
                    continue
                L.append(
                    f"| {agent} | {t} | {diff} | {len(rs)} | {sum(1 for r in rs if r['ok'])} "
                    f"| {rb.median([r['cost_cny'] for r in rs]):.4f} "
                    f"| {rb.median([r['total_latency_ms'] for r in rs])/1000:.1f} "
                    f"| {rb.median([r['peak_prompt_tokens'] for r in rs]):.0f} "
                    f"| {rb.median([r['prompt_tokens'] for r in rs]):.0f} "
                    f"| {rb.median([r['llm_calls'] for r in rs]):.0f} "
                    f"| {rb.median([r['replans'] for r in rs]):.1f} "
                    f"| {rb.median([r['epochs'] for r in rs]):.1f} "
                    f"| {rb.median([r['reflections'] for r in rs]):.1f} |"
                )
    L.append("")

    # failure modes
    L.append("## Failure modes\n")
    fm: dict = {}
    for r in rows:
        if r["ok"]:
            continue
        key = (r["agent"], r["task"], r["difficulty"])
        fm.setdefault(key, {})
        m = r["failure_mode"] or "unknown"
        fm[key][m] = fm[key].get(m, 0) + 1
    if not fm:
        L.append("_no failures in the merged set_")
    for key in sorted(fm):
        modes = ", ".join(f"{m}×{c}" for m, c in sorted(fm[key].items(), key=lambda x: -x[1]))
        L.append(f"- `{key[0]}/{key[1]}/{key[2]}`: {modes}")
    L.append("")

    # hypothesis checks (only where data exists)
    L.append("## Hypothesis checks (against available data)\n")

    def cell_ok(agent, t, diff="easy"):
        rs = by_cell.get((agent, t, diff), [])
        return sum(1 for r in rs if r["ok"]), len(rs)

    def cell_med(agent, t, diff, field):
        rs = by_cell.get((agent, t, diff), [])
        return rb.median([r[field] for r in rs]) if rs else None

    for diff in DIFF_ORDER:
        k_r, n_r = cell_ok("react", "T1", diff)
        k_p, n_p = cell_ok("plan_execute", "T1", diff)
        if n_r and n_p:
            L.append(f"- H1 ({diff}): ReAct {k_r}/{n_r} vs Plan {k_p}/{n_p} on T1")
        pk_r = cell_med("react", "T2", diff, "peak_prompt_tokens")
        pk_p = cell_med("plan_execute", "T2", diff, "peak_prompt_tokens")
        if pk_r is not None and pk_p is not None:
            L.append(f"- H2 ({diff}): T2 peak prompt ReAct {pk_r:.0f} vs Plan {pk_p:.0f} "
                     f"(ratio {pk_r/pk_p:.1f}x)" if pk_p else f"- H2 ({diff}): Plan peak=0")
        k_x, n_x = cell_ok("reflexion", "T3", diff)
        k_r3, n_r3 = cell_ok("react", "T3", diff)
        if n_x and n_r3:
            cx = cell_med("reflexion", "T3", diff, "cost_cny")
            cr3 = cell_med("react", "T3", diff, "cost_cny")
            L.append(f"- H3 ({diff}): T3 Reflexion {k_x}/{n_x} vs ReAct {k_r3}/{n_r3}; "
                     f"median cost {cx:.4f} vs {cr3:.4f} CNY")
    all_ok = {a: (sum(1 for r in rows if r["agent"] == a and r["ok"]),
                  sum(1 for r in rows if r["agent"] == a)) for a in AGENTS}
    L.append(f"- H5 (aggregate): " + "; ".join(f"{a} {k}/{n}" for a, (k, n) in all_ok.items() if n))
    L.append("")

    (out / "report.md").write_text("\n".join(L), encoding="utf-8")

    # merged csv
    cols = ["source_batch"] + list(rows[0].keys())
    cols = [c for c in dict.fromkeys(cols) if c != "source_batch"]
    with open(out / "report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"merged {len(rows)} runs from {len(batch_dirs)} batches -> {out}")
    print("\n".join(L[:34]))
    return 0


if __name__ == "__main__":
    from typing import List  # noqa: E402
    sys.exit(main())
