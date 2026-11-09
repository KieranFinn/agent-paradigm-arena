#!/usr/bin/env python3
"""Agent Paradigm Arena - matrix runner, statistics and reports.

Usage:
    python run_benchmark.py --check                 # offline self-check (no key, no cost)
    python run_benchmark.py --dry-run               # print the execution plan
    python run_benchmark.py --tag main              # run the full matrix (needs API key)
    python run_benchmark.py --agents react --tasks T1 --difficulty easy --repeats 1 --tag probe
    python run_benchmark.py --agents plan_execute --tasks T2 --replan-scope global --tag e2-global
    python run_benchmark.py --agents reflexion --tasks T3 --reflexion-evaluator llm_judge --tag e3-judge

Outputs (runs/<tag>_<ts>/):
    report.md   # §1 per-agent, §2 4x3 matrix, §3 overall, §4 failure modes
    report.csv  # flat per-run data (pareto scatter ready)
    raw.jsonl   # one line per run (includes real wall-clock)
    traces/     # full event sequences, one JSONL per run (blog evidence)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agent_arena.config import load_with_defaults  # noqa: E402
from agent_arena.evaluators import EVALUATOR_MODES, run_evaluator  # noqa: E402
from agent_arena.frameworks import AGENTS, FRAMEWORKS  # noqa: E402
from agent_arena.llm import (  # noqa: E402
    AuditCache,
    BudgetExceeded,
    LLMClient,
    Price,
    Trace,
    load_env_file,
    parse_json_loose,
    simulated_latency_ms,
)
from agent_arena.tasks import DIFFICULTIES, TASKS, make_task  # noqa: E402
from agent_arena.tools import ArgError, ToolError  # noqa: E402

HOME_TURF = {("react", "T1"), ("plan_execute", "T2"), ("reflexion", "T3")}
CHECK_TO_FAILURE = {
    "tests_run": "tests_never_run",
    "tests_pass": "tests_not_passing",
    "fixed_root_cause": "root_cause_not_fixed",
    "tests_untouched": "tests_modified",
    "answer_states_root_cause": "answer_missing_root_cause",
    "answer_has_value": "no_value_in_answer",
    "correct_value": "wrong_value",
    "sql_executed": "no_sql_executed",
    "solution_submitted": "no_code_submitted",
}

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------

def wilson_ci(k: int, n: int, z: float = 1.96) -> List[float]:
    """Wilson score interval for k successes out of n."""
    if n == 0:
        return [0.0, 1.0]
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [max(0.0, center - half), min(1.0, center + half)]


def median(xs: List[float]) -> float:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return 0.0
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def mad(xs: List[float]) -> float:
    xs = [x for x in xs if x is not None]
    if not xs:
        return 0.0
    med = median(xs)
    return median([abs(x - med) for x in xs])


def fmt_ci(k: int, n: int) -> str:
    lo, hi = wilson_ci(k, n)
    return f"{k}/{n} [{lo:.2f},{hi:.2f}]"


# ---------------------------------------------------------------------------
# run one execution
# ---------------------------------------------------------------------------

def classify_failure(run: Dict[str, Any], ev, trace: Trace) -> str:
    if run["status"] == "budget_exceeded":
        return "budget_exceeded"
    if run["status"] == "error":
        return "harness_error"
    if trace.count("guard") >= 3:
        return "repeated_action_loop"
    if not (run["final_answer"] or "").strip():
        return "no_final_answer"
    failed = [c["name"] for c in (ev.checks or []) if not c["ok"]]
    for name in failed:
        if name in CHECK_TO_FAILURE:
            return CHECK_TO_FAILURE[name]
    if run["status"] == "steps_exhausted":
        return "steps_exhausted"
    return failed[0] if failed else "unknown"


def run_one(
    agent: str,
    task_name: str,
    difficulty: str,
    repeat: int,
    cfg: Dict[str, Dict[str, Any]],
    api_key: str,
    cache: Optional[AuditCache],
) -> Dict[str, Any]:
    trace = Trace()
    task = make_task(task_name, difficulty)
    llm = LLMClient(
        api_key=api_key,
        base_url=str(cfg["llm"]["base_url"]),
        model=str(cfg["llm"]["model"]),
        temperature=float(cfg["llm"].get("temperature", 0)),
        max_tokens=int(cfg["llm"].get("max_tokens", 4096)),
        price=Price(
            input_cached=float(cfg["llm"].get("price_input_cached", 0.2)),
            input=float(cfg["llm"].get("price_input", 2.0)),
            output=float(cfg["llm"].get("price_output", 8.0)),
        ),
        trace=trace,
        cache=cache or AuditCache(os.devnull, enabled=False),
        budget_cny=float(cfg["budget"]["max_cny_per_run"]),
        request_timeout_s=float(cfg["llm"].get("request_timeout_s", 180)),
    )

    run: Dict[str, Any] = {
        "run_id": f"{agent}_{task_name}_{difficulty}_r{repeat}",
        "agent": agent, "task": task_name, "difficulty": difficulty,
        "repeat": repeat, "model": str(cfg["llm"]["model"]),
        "status": "completed", "final_answer": "", "error": "",
    }
    t0 = time.monotonic()
    try:
        outcome = FRAMEWORKS[agent](task, llm, trace, cfg)
        run["final_answer"] = (outcome.final_answer or "")[:4000]
        run["status"] = outcome.status
        effects, info = outcome.effects, (outcome.info or {})
    except BudgetExceeded as e:
        run["status"], run["error"] = "budget_exceeded", str(e)
        effects, info = [], {}
    except Exception as e:  # noqa: BLE001 - harness must survive model-side chaos
        run["status"] = "error"
        run["error"] = f"{type(e).__name__}: {e}"
        log(f"  !! {run['run_id']} raised: {run['error']}\n{traceback.format_exc(limit=4)}")
        effects, info = [], {}
    wall_ms = (time.monotonic() - t0) * 1000

    ev = run_evaluator(task, "strict", run["final_answer"], effects=effects)
    trace.add("final", payload={"answer": run["final_answer"][:2000], "ok": ev.ok,
                                "failure_mode": classify_failure(run, ev, trace) if not ev.ok else ""})

    tokens = trace.total_tokens()
    run.update({
        "ok": bool(ev.ok),
        "score": round(float(ev.score), 3),
        "checks": json.dumps(ev.checks, ensure_ascii=False),
        "detail": ev.detail,
        "failure_mode": "" if ev.ok else classify_failure(run, ev, trace),
        "steps": info.get("steps", info.get("llm_calls", trace.llm_calls())),
        "llm_calls": trace.llm_calls(),
        "prompt_tokens": tokens["prompt"],
        "completion_tokens": tokens["completion"],
        "cached_tokens": tokens["cached"],
        "peak_prompt_tokens": trace.peak_prompt_tokens(),
        "cost_cny": trace.total_cost_cny(),
        "first_action_ms": trace.to_first_action_ms(),
        "total_latency_ms": trace.total_latency_ms(),
        "wall_ms": round(wall_ms, 1),
        "replans": info.get("replans", 0),
        "epochs": info.get("epochs", 1),
        "reflections": info.get("reflections", 0),
        "guard_events": trace.count("guard"),
        "effects_count": len(effects),
        "trace_events": len(trace.events),
    })
    task.cleanup()
    return {"row": run, "trace": trace}


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------

def _cell_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    k = sum(1 for r in rows if r["ok"])
    costs = [r["cost_cny"] for r in rows]
    lat = [r["total_latency_ms"] for r in rows]
    peak = [r["peak_prompt_tokens"] for r in rows]
    ptoks = [r["prompt_tokens"] for r in rows]
    ctoks = [r["completion_tokens"] for r in rows]
    llm = [r["llm_calls"] for r in rows]
    cps = (sum(costs) / k) if k else float("nan")
    return {
        "n": n, "k": k, "ci": wilson_ci(k, n),
        "cost_med": median(costs), "cost_mad": mad(costs),
        "cost_total": sum(costs), "cost_per_success": cps,
        "lat_med": median(lat), "lat_mad": mad(lat),
        "peak_med": median(peak), "ptok_med": median(ptoks), "ctok_med": median(ctoks),
        "llc_med": median(llm),
    }


def write_reports(batch_dir: Path, rows: List[Dict[str, Any]], cfg, meta: Dict[str, Any]) -> None:
    (batch_dir / "traces").mkdir(parents=True, exist_ok=True)

    # report.csv
    csv_cols = [
        "run_id", "agent", "task", "difficulty", "repeat", "model", "ok", "score",
        "failure_mode", "status", "steps", "llm_calls", "prompt_tokens",
        "completion_tokens", "cached_tokens", "peak_prompt_tokens", "cost_cny",
        "first_action_ms", "total_latency_ms", "wall_ms", "replans", "epochs",
        "reflections", "guard_events", "effects_count", "detail", "error",
        "final_answer",
    ]
    with open(batch_dir / "report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=csv_cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in csv_cols})

    # group rows
    by_cell: Dict[tuple, List[Dict]] = {}
    for r in rows:
        by_cell.setdefault((r["agent"], r["task"], r["difficulty"]), []).append(r)

    stats = {key: _cell_stats(rs) for key, rs in by_cell.items()}

    L: List[str] = []
    L.append("# Agent Paradigm Arena - benchmark report\n")
    L.append(f"- batch: `{meta['batch']}`  \n- generated: {meta['generated']}")
    L.append(f"- model: `{meta['model']}`  repeats/cell: {meta['repeats']}  jobs: {meta['jobs']}")
    L.append(f"- plan.replan_scope: `{meta['replan_scope']}`  reflexion.evaluator: `{meta['evaluator']}`")
    L.append(f"- total runs: {len(rows)}  total cost: **{sum(r['cost_cny'] for r in rows):.4f} CNY**"
             f"  successes: {sum(1 for r in rows if r['ok'])}/{len(rows)}\n")

    # §1 per-agent vertical comparison
    L.append("## 1. Per-agent comparison (success [Wilson 95% CI], median cost / latency / peak prompt)\n")
    for agent in AGENTS:
        arows = [r for r in rows if r["agent"] == agent]
        if not arows:
            continue
        L.append(f"### {agent}\n")
        L.append("| task | diff | success | cost CNY (med±MAD) | latency s (med±MAD) | peak prompt (med) | llm calls (med) |")
        L.append("|---|---|---|---|---|---|---|")
        for (a, t, d) in sorted(k for k in stats if k[0] == agent):
            s = stats[(a, t, d)]
            L.append(
                f"| {t} | {d} | {fmt_ci(s['k'], s['n'])} | {s['cost_med']:.4f}±{s['cost_mad']:.4f} "
                f"| {s['lat_med']/1000:.1f}±{s['lat_mad']/1000:.1f} | {s['peak_med']:.0f} | {s['llc_med']:.0f} |"
            )
        L.append("")

    # §2 the 4x3 matrix per difficulty
    for diff in DIFFICULTIES:
        if not any(k[2] == diff for k in stats):
            continue
        L.append(f"## 2. Matrix - {diff}\n")
        L.append("| agent | T1 explore | T2 plan | T3 verify |")
        L.append("|---|---|---|---|")
        for agent in AGENTS:
            if not any(k[0] == agent and k[2] == diff for k in stats):
                continue
            cells = []
            for t in ("T1", "T2", "T3"):
                s = stats.get((agent, t, diff))
                if s is None:
                    cells.append("·")
                    continue
                mark = " ✅" if (agent, t) in HOME_TURF else ""
                cells.append(f"{fmt_ci(s['k'], s['n'])}{mark}")
            L.append(f"| {agent} | " + " | ".join(cells) + " |")
        L.append("\n(✅ = home turf per the structural-type design)\n")

    # §3 overall
    L.append("## 3. Overall\n")
    L.append("| agent | success | total CNY | CNY/success | med cost | med latency s | med peak prompt | med prompt tokens |")
    L.append("|---|---|---|---|---|---|---|---|")
    for agent in AGENTS:
        arows = [r for r in rows if r["agent"] == agent]
        if not arows:
            continue
        s = _cell_stats(arows)
        cps = f"{s['cost_per_success']:.4f}" if s["k"] else "n/a"
        L.append(
            f"| {agent} | {fmt_ci(s['k'], s['n'])} | {s['cost_total']:.4f} | {cps} "
            f"| {s['cost_med']:.4f} | {s['lat_med']/1000:.1f} | {s['peak_med']:.0f} | {s['ptok_med']:.0f} |"
        )
    L.append("")

    # §4 failure modes
    L.append("## 4. Failure modes (counts per agent x task, failures only)\n")
    fm: Dict[tuple, Dict[str, int]] = {}
    for r in rows:
        if r["ok"]:
            continue
        key = (r["agent"], r["task"], r["difficulty"])
        fm.setdefault(key, {})
        fm[key][r["failure_mode"] or "unknown"] = fm[key].get(r["failure_mode"] or "unknown", 0) + 1
    if not fm:
        L.append("_no failures - suspicious, double-check the evaluator_\n")
    for key in sorted(fm):
        modes = ", ".join(f"{m}×{c}" for m, c in sorted(fm[key].items(), key=lambda x: -x[1]))
        L.append(f"- `{key[0]}/{key[1]}/{key[2]}`: {modes}")
    L.append("")

    # §5 cost-effectiveness (pareto input)
    L.append("## 5. Cost-effectiveness (per cell: success rate vs CNY/success)\n")
    L.append("| agent | task | diff | success | CNY/success |")
    L.append("|---|---|---|---|---|")
    for key in sorted(stats):
        s = stats[key]
        cps = f"{s['cost_per_success']:.4f}" if s["k"] else "n/a"
        L.append(f"| {key[0]} | {key[1]} | {key[2]} | {fmt_ci(s['k'], s['n'])} | {cps} |")
    L.append("")

    (batch_dir / "report.md").write_text("\n".join(L), encoding="utf-8")


def write_trace(batch_dir: Path, run: Dict[str, Any], trace: Trace) -> None:
    path = batch_dir / "traces" / f"{run['run_id']}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ev in trace.as_dicts():
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# offline self-check (--check): no key, no network, no cost
# ---------------------------------------------------------------------------

def self_check() -> int:
    failures: List[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        status = "PASS" if cond else "FAIL"
        log(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    log("== config ==")
    cfg = load_with_defaults(ROOT / "config.example.yaml")
    check("config loads two-level YAML subset", isinstance(cfg.get("llm"), dict) and cfg["llm"]["price_input"] == 2.0)

    log("== statistics ==")
    lo, hi = wilson_ci(7, 10)
    check("wilson_ci(7,10) == [0.397, 0.892]", abs(lo - 0.397) < 5e-4 and abs(hi - 0.892) < 5e-4,
          f"got [{lo:.4f}, {hi:.4f}]")
    check("median/mad", median([1, 2, 3, 4]) == 2.5 and mad([1, 1, 2, 2]) == 0.5)

    log("== llm helpers ==")
    check("parse_json_loose: fenced", parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1})
    check("parse_json_loose: prose-wrapped", parse_json_loose('Sure! Here it is:\n{"steps": [{"id": 1}]} hope it helps')["steps"][0]["id"] == 1)
    try:
        parse_json_loose("no json here")
        check("parse_json_loose: raises on garbage", False)
    except ValueError:
        check("parse_json_loose: raises on garbage", True)
    p = Price()
    check("price tiers", abs(p.cost(1_000_000, 0, 0) - 2.0) < 1e-9
          and abs(p.cost(1_000_000, 0, 1_000_000) - 0.2) < 1e-9
          and abs(p.cost(0, 1_000_000, 0) - 8.0) < 1e-9)
    check("simulated latency: cached=40ms", simulated_latency_ms(1000, 10, 1000) == 40.0)
    check("simulated latency: formula", simulated_latency_ms(1000, 100, 0) == 450 + 1000 * 0.02 + 100 * 12.0)
    tr = Trace()
    tr.add("llm_call", prompt_tokens=100, completion_tokens=50, cost_cny=0.001, advance_ms=900)
    tr.add("plan", payload={})
    tr.add("tool_call", payload={}, advance_ms=5)
    tr.add("tool_result", payload={}, advance_ms=30)
    check("trace aggregations", tr.peak_prompt_tokens() == 100 and tr.total_latency_ms() == 935.0
          and tr.to_first_action_ms() == 900.0)

    log("== T1 explore ==")
    t1 = TASKS["T1"]("easy")
    box = t1.materialize(Trace())
    res = box.run("run_tests", {}, 0)
    check("T1 broken state: 2 passed, 1 failed, 1 skipped",
          res.get("passed") == 2 and res.get("failed") == 1 and res.get("skipped") == 1,
          res.get("summary", ""))
    r1 = box.run("read_file", {"path": "billing.py"}, 1)
    check("T1 read_file", r1.get("ok") and "verify_invoice" in r1.get("content", ""))
    r2 = box.run("apply_patch", {"path": "billing.py", "old": "NOT_PRESENT", "new": "x"}, 2)
    check("T1 apply_patch bad anchor -> kind=arg", r2.get("kind") == "arg")
    r3 = box.run("read_file", {"path": "../../etc/passwd"}, 3)
    check("T1 path escape rejected", r3.get("ok") is False)
    # the canonical fix
    box.run("apply_patch", {"path": "hashes.py", "old": 'EXPECTED_INVOICE_HASH = "a3f1c9e2b7d4"',
                            "new": 'EXPECTED_INVOICE_HASH = "67e52dd558aa"'}, 4)
    res2 = box.run("run_tests", {}, 5)
    check("T1 fixed state: 3 passed, 1 skipped",
          res2.get("passed") == 3 and res2.get("failed") == 0 and res2.get("skipped") == 1,
          res2.get("summary", ""))
    effects = [
        {"kind": "call", "tool": "run_tests", "args": {}, "step": 5},
        {"kind": "result", "tool": "run_tests", "ok": True},
    ]
    ev = t1.evaluate("FINAL: the root cause is in hashes.py - stale expected hash.", effects)
    check("T1 evaluate accepts correct fix", ev.ok, ev.detail)
    t1b = TASKS["T1"]("hard")
    boxb = t1b.materialize(Trace())
    check("T1 hard adds red herrings", "storage.py" in boxb.run("list_files", {}, 0).get("files", []))
    t1b.cleanup()

    log("== T2 plan ==")
    for diff in DIFFICULTIES:
        t2 = TASKS["T2"](diff)
        box2 = t2.materialize(Trace())
        tm = t2.true_margin()
        check(f"T2 {diff} true_margin == 0.55", abs(tm - 0.55) < 1e-9, f"got {tm}")
        r = box2.run("run_sql", {"sql": "SELECT COUNT(*) AS c, SUM(amount) AS s FROM sales"}, 0)
        expected_probe = [[5, 800.0]] if diff == "easy" else [[8, 1100.0]]
        check(f"T2 {diff} full-table probe {expected_probe[0]}",
              r.get("ok") and r["rows"] == expected_probe, str(r.get("rows")))
        r = box2.run("run_sql", {"sql": "SELECT * FROM sales WHERE year=2023"}, 1)
        check(f"T2 {diff} dup row visible in 2023 (3 rows)", r.get("ok") and r["row_count"] == 3,
              f"rows={r.get('row_count')}")
        r = box2.run("run_sql", {"sql": "INSERT INTO sales VALUES ('x', 1, 1)"}, 2)
        check(f"T2 {diff} INSERT rejected as arg error", r.get("kind") == "arg")
        r = box2.run("fetch_warehouse_export", {"year": 2023}, 3)
        check(f"T2 {diff} export API 403 (kind=tool)", r.get("kind") == "tool" and "403" in r.get("error", ""))
        r = box2.run("run_sql", {"sql": "SELECT a.region FROM sales a, sales b, sales c, cost d"}, 4)
        check(f"T2 {diff} >200 rows rejected", r.get("kind") == "tool" and "200" in r.get("error", ""))
        fx = [
            {"kind": "call", "tool": "run_sql", "args": {"sql": "SELECT DISTINCT region FROM sales"}, "step": 0},
            {"kind": "result", "tool": "run_sql", "ok": True},
        ]
        ev = t2.evaluate("FINAL: the 2023 overall gross margin is 0.55", fx)
        check(f"T2 {diff} evaluate accepts 0.55", ev.ok, ev.detail)
        ev = t2.evaluate("FINAL: it is 0.275", fx)
        check(f"T2 {diff} evaluate rejects wrong value", not ev.ok)
        ev = t2.evaluate("FINAL: 55%", fx)
        check(f"T2 {diff} percent parsing", ev.ok, ev.detail)
        t2.cleanup()

    log("== T3 verify ==")
    good_easy = (
        "import re\n\n"
        "def parse_duration(s):\n"
        "    if not isinstance(s, str):\n"
        "        raise ValueError('invalid duration')\n"
        "    m = re.fullmatch(r'(\\d+)(ms|s|m|h)', s)\n"
        "    if not m:\n"
        "        raise ValueError(f'invalid duration: {s!r}')\n"
        "    n, unit = int(m.group(1)), m.group(2)\n"
        "    return (n * {'ms': 1, 's': 1000, 'm': 60000, 'h': 3600000}[unit]) // 1000\n"
    )
    t3 = TASKS["T3"]("easy")
    box3 = t3.materialize(Trace())
    r = box3.run("write_solution", {"code": good_easy}, 0)
    check("T3 easy reference solution: 9 passed", r.get("passed") == 9 and r.get("failed") == 0, r.get("summary", ""))
    effects3 = [{"kind": "call", "tool": "write_solution", "args": {"code": good_easy}, "step": 0}]
    ev = t3.evaluate("FINAL: implemented parse_duration", effects3)
    check("T3 easy evaluate ok", ev.ok, ev.detail)
    r = box3.run("write_solution", {"code": "def parse_duration(s):\n    return int(s[:-2])\n"}, 1)
    check("T3 easy broken solution fails", r.get("failed", 0) > 0, r.get("summary", ""))
    cot_answer = "FINAL: done\n```python\n" + good_easy + "\n```"
    ev = t3.evaluate(cot_answer, [])
    check("T3 easy cot-style answer (code extracted from text)", ev.ok, ev.detail)
    t3.cleanup()

    good_hard = (
        "import re\n\n"
        "def slugify(s):\n"
        "    s = s.lower()\n"
        "    out = re.sub(r'[^a-z0-9\\u4e00-\\u9fff]+', '-', s)\n"
        "    out = out.strip('-')\n"
        "    return out or '-'\n\n"
        "def safe_int(value, default=None):\n"
        "    if isinstance(value, bool):\n"
        "        return value\n"
        "    if isinstance(value, int):\n"
        "        return value\n"
        "    if isinstance(value, str):\n"
        "        t = value.strip()\n"
        "        if re.fullmatch(r'-?\\d+(_\\d+)*', t):\n"
        "            return int(t.replace('_', ''))\n"
        "        return default\n"
        "    return default\n"
    )
    t3h = TASKS["T3"]("hard")
    box3h = t3h.materialize(Trace())
    r = box3h.run("write_solution", {"code": good_hard}, 0)
    check("T3 hard reference solution: 9 passed", r.get("passed") == 9 and r.get("failed") == 0, r.get("summary", ""))
    legacy_coerce = good_hard.replace(
        "if isinstance(value, bool):\n        return value\n    ",
        "if isinstance(value, bool):\n        return int(value)\n    ")
    r = box3h.run("write_solution", {"code": legacy_coerce}, 1)
    check("T3 hard legacy-coercion decoy is caught by hidden tests", r.get("failed", 0) > 0,
          r.get("summary", ""))
    t3h.cleanup()

    log("")
    if failures:
        log(f"SELF-CHECK FAILED: {len(failures)} failure(s): {failures}")
        return 1
    log("SELF-CHECK: all green.")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_matrix(args, cfg) -> List[Dict[str, str]]:
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    tasks = [t.strip().upper() for t in args.tasks.split(",") if t.strip()]
    diffs = [d.strip().lower() for d in args.difficulty.split(",") if d.strip()]
    for a in agents:
        if a not in AGENTS:
            raise SystemExit(f"unknown agent {a!r}; known: {list(AGENTS)}")
    for t in tasks:
        if t not in TASKS:
            raise SystemExit(f"unknown task {t!r}; known: {list(TASKS)}")
    for d in diffs:
        if d not in DIFFICULTIES:
            raise SystemExit(f"unknown difficulty {d!r}")
    repeats = args.repeats if args.repeats is not None else int(cfg["defaults"]["repeats"])
    cells = []
    for a in agents:
        for t in tasks:
            for d in diffs:
                for i in range(repeats):
                    cells.append({"agent": a, "task": t, "difficulty": d, "repeat": i})
    return cells


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="config yaml (default: config.yaml else config.example.yaml)")
    ap.add_argument("--agents", default=",".join(AGENTS))
    ap.add_argument("--tasks", default="T1,T2,T3")
    ap.add_argument("--difficulty", default="easy,hard")
    ap.add_argument("--repeats", type=int, default=None)
    ap.add_argument("--tag", default="batch")
    ap.add_argument("--model", default=None, help="override llm.model (E4)")
    ap.add_argument("--replan-scope", choices=["local", "global"], default=None, help="E2")
    ap.add_argument("--reflexion-evaluator", choices=list(EVALUATOR_MODES), default=None, help="E3")
    ap.add_argument("--jobs", type=int, default=1, help="parallel runs")
    ap.add_argument("--use-cache", action="store_true", help="enable the audit cache")
    ap.add_argument("--check", action="store_true", help="offline self-check, then exit")
    ap.add_argument("--dry-run", action="store_true", help="print the execution plan, then exit")
    args = ap.parse_args()

    if args.check:
        return self_check()

    cfg_path = args.config or (
        ROOT / "config.yaml" if (ROOT / "config.yaml").exists() else ROOT / "config.example.yaml"
    )
    cfg = load_with_defaults(cfg_path)
    if args.model:
        cfg["llm"]["model"] = args.model
        cfg["models"]["main"] = args.model
    if args.replan_scope:
        cfg["plan"]["replan_scope"] = args.replan_scope
    if args.reflexion_evaluator:
        cfg["reflexion"]["evaluator"] = args.reflexion_evaluator

    cells = build_matrix(args, cfg)
    repeats = int(cfg["defaults"]["repeats"]) if args.repeats is None else args.repeats

    if args.dry_run:
        print("DRY RUN - execution plan (no API calls, no key needed)")
        print(f"  config     : {cfg_path}")
        print(f"  model      : {cfg['llm']['model']} @ {cfg['llm']['base_url']}")
        print(f"  matrix     : {len(set((c['agent'], c['task'], c['difficulty']) for c in cells))} cells"
              f" x {repeats} repeats = {len(cells)} runs")
        print(f"  replan     : {cfg['plan']['replan_scope']}   reflexion evaluator: {cfg['reflexion']['evaluator']}")
        print(f"  jobs       : {args.jobs}   cache: {args.use_cache}")
        print(f"  est. cost  : {len(cells) * 0.01:.2f} - {len(cells) * 0.1:.2f} CNY"
              f"   budget/run: {cfg['budget']['max_cny_per_run']} CNY")
        for c in cells[:12]:
            print(f"    - {c['agent']}/{c['task']}/{c['difficulty']} r{c['repeat']}")
        if len(cells) > 12:
            print(f"    ... and {len(cells) - 12} more")
        return 0

    # real run: need a key
    load_env_file(ROOT / ".env")
    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key:
        print("ERROR: no API key. Set DEEPSEEK_API_KEY (or LLM_API_KEY) in the environment "
              "or in 'AI Agent/.env'. Copy .env.example to .env.")
        return 2

    batch = f"{args.tag}_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    batch_dir = ROOT / "runs" / batch
    (batch_dir / "traces").mkdir(parents=True, exist_ok=True)
    cache = AuditCache(str(ROOT / ".cache" / "llm_audit.jsonl"), enabled=args.use_cache)

    meta = {
        "batch": batch, "generated": datetime.now().isoformat(timespec="seconds"),
        "model": cfg["llm"]["model"], "repeats": repeats, "jobs": args.jobs,
        "replan_scope": cfg["plan"]["replan_scope"], "evaluator": cfg["reflexion"]["evaluator"],
    }
    log(f"== batch {batch}: {len(cells)} runs, model={cfg['llm']['model']}, jobs={args.jobs} ==")

    rows: List[Dict[str, Any]] = []
    raw_lock = threading.Lock()
    raw_path = batch_dir / "raw.jsonl"
    t_start = time.monotonic()

    def _done(fut):
        res = fut.result()
        run, trace = res["row"], res["trace"]
        write_trace(batch_dir, run, trace)
        with raw_lock:
            rows.append(run)
            with open(raw_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(
                    {**{k: run.get(k) for k in ("run_id", "agent", "task", "difficulty", "repeat",
                                                "model", "ok", "failure_mode", "status", "cost_cny",
                                                "wall_ms", "total_latency_ms", "llm_calls")},
                     "wall_clock_note": "real wall-clock; report tables use the simulated clock"},
                    ensure_ascii=False) + "\n")
        done_n = len(rows)
        log(f"[{done_n}/{len(cells)}] {run['run_id']}: ok={run['ok']} "
            f"cost={run['cost_cny']:.4f} lat={run['total_latency_ms']/1000:.1f}s "
            f"llm={run['llm_calls']} peak={run['peak_prompt_tokens']} "
            f"{'(' + run['failure_mode'] + ')' if run['failure_mode'] else ''}")

    try:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
            futures = [
                ex.submit(run_one, c["agent"], c["task"], c["difficulty"], c["repeat"], cfg, api_key, cache)
                for c in cells
            ]
            for fut in as_completed(futures):
                _done(fut)
    except KeyboardInterrupt:
        log("interrupted - writing partial reports...")
    finally:
        write_reports(batch_dir, rows, cfg, meta)
        elapsed = time.monotonic() - t_start
        total_cost = sum(r["cost_cny"] for r in rows)
        log(f"== done: {len(rows)}/{len(cells)} runs in {elapsed/60:.1f} min, "
            f"cost {total_cost:.4f} CNY, ok {sum(1 for r in rows if r['ok'])}/{len(rows)} ==")
        log(f"reports: {batch_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
