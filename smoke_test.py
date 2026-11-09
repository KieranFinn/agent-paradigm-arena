#!/usr/bin/env python3
"""Offline end-to-end smoke test: a scripted fake LLM drives ALL frameworks
through all links (framework -> tools -> effects -> evaluator -> trace).

No network, no key, no cost.  The structural numbers produced here prove the
measurement chain works; they are NOT model data (that's what runs/ is for).

Scenarios:
  S1  ReAct / T1   : happy path incl. one ArgError self-heal
  S2  ReAct / T1   : repeated-action guard fires
  S3  Plan  / T2   : step fails -> local re-plan -> success
  S4  Reflexion/T3 : epoch 1 fails, reflection, epoch 2 passes
  S5  CoT   / T2   : tool-less baseline fails the strict evaluator
  S6  effects recorded from worker threads (covered by S3's sql_executed)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import agent_arena.llm as llmmod  # noqa: E402
from agent_arena.config import load_with_defaults  # noqa: E402
from agent_arena.frameworks import FRAMEWORKS  # noqa: E402
from agent_arena.llm import LLMClient, Trace  # noqa: E402
from agent_arena.tasks import make_task  # noqa: E402

FAILURES: list = []


def expect(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""), flush=True)
    if not cond:
        FAILURES.append(name)


class ScriptedTransport:
    """Replaces the HTTP transport; routes by system-prompt content."""

    def __init__(self, router):
        self.router = router
        self.calls = []

    def request(self, base_url, api_key, payload, timeout):
        self.calls.append(payload)
        return self.router(payload)


def _usage(payload, content: str) -> dict:
    chars = sum(len(str(m.get("content") or "")) for m in payload["messages"])
    return {
        "prompt_tokens": max(10, chars // 4),
        "completion_tokens": max(5, len(content) // 4),
        "prompt_cache_hit_tokens": max(0, chars // 8),
    }


def _text_resp(payload, content: str) -> dict:
    return {"choices": [{"message": {"content": content, "tool_calls": None},
                         "finish_reason": "stop"}],
            "usage": _usage(payload, content)}


def _tool_resp(payload, calls: list) -> dict:
    content = " ".join(f"{c[0]}({json.dumps(c[1])})" for c in calls)
    return {"choices": [{"message": {
        "content": None,
        "tool_calls": [{"id": f"c{i}", "type": "function",
                        "function": {"name": name,
                                     "arguments": json.dumps(args, ensure_ascii=False)}}
                       for i, (name, args) in enumerate(calls)]},
        "finish_reason": "tool_calls"}],
        "usage": _usage(payload, content)}


def _system(payload) -> str:
    return payload["messages"][0].get("content", "") if payload["messages"] else ""


def _last_tool_content(payload) -> str:
    for m in reversed(payload["messages"]):
        if m.get("role") == "tool":
            return m.get("content", "")
    return ""


def install(router) -> ScriptedTransport:
    t = ScriptedTransport(router)
    llmmod._TRANSPORT = t
    return t


def make_llm() -> LLMClient:
    return LLMClient(api_key="dummy", base_url="http://scripted", model="scripted-1",
                     trace=Trace(), budget_cny=500.0)


GOOD_HASH_FIX = {"path": "hashes.py",
                 "old": 'EXPECTED_INVOICE_HASH = "a3f1c9e2b7d4"',
                 "new": 'EXPECTED_INVOICE_HASH = "67e52dd558aa"'}
BAD_HASH_FIX = {"path": "hashes.py",
                "old": "EXPECTED_INVOICE_HASH = 'a3f1c9e2b7d4'",
                "new": "EXPECTED_INVOICE_HASH = 'x'"}

CODE_BAD = "def parse_duration(s):\n    return int(s[:-2])\n"
CODE_GOOD = (
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


def s1_react_t1(payload):
    sysmsg = _system(payload)
    if "ReAct" not in sysmsg:
        raise AssertionError("S1: expected react system prompt")
    msgs = payload["messages"]
    n_assistant_tc = sum(1 for m in msgs if m.get("role") == "assistant" and m.get("tool_calls"))
    if n_assistant_tc == 0:
        return _tool_resp(payload, [("read_file", {"path": "billing.py"})])
    if n_assistant_tc == 1:
        return _tool_resp(payload, [("read_file", {"path": "hashes.py"})])
    if n_assistant_tc == 2:
        return _tool_resp(payload, [("apply_patch", BAD_HASH_FIX)])  # wrong anchor -> ArgError
    if n_assistant_tc == 3:
        if '"kind": "arg"' not in _last_tool_content(payload):
            raise AssertionError("S1: expected an arg error to be fed back")
        return _tool_resp(payload, [("apply_patch", GOOD_HASH_FIX)])
    if n_assistant_tc == 4:
        return _tool_resp(payload, [("run_tests", {})])
    return _text_resp(payload, "FINAL: The root cause is in hashes.py - "
                               "EXPECTED_INVOICE_HASH was stale after the signature bump.")


def s2_react_guard(payload):
    sysmsg = _system(payload)
    if "ReAct" not in sysmsg:
        raise AssertionError("S2: expected react system prompt")
    return _tool_resp(payload, [("read_file", {"path": "api.log"})])


def s3_plan_t2(payload):
    sysmsg = _system(payload)
    msgs = payload["messages"]
    if "RE-PLANNER" in sysmsg:  # must precede the PLANNER check ("RE-PLANNER" contains it)
        return _text_resp(payload, json.dumps({"steps": [
            {"id": 3, "goal": "Read the data dictionary and table schemas",
             "depends_on": [], "hint": "read_data_dictionary first"},
            {"id": 4, "goal": "Compute the 2023 overall gross margin",
             "depends_on": [3], "hint": "deduplicate sales; exclude 999.0 sentinel rows; "
                                        "DEFAULT_TARGET=400 for missing regions"},
        ]}))
    if "PLANNER" in sysmsg:
        return _text_resp(payload, json.dumps({"steps": [
            {"id": 1, "goal": "Inspect the warehouse schema and data dictionary",
             "depends_on": [], "hint": "list_tables, describe_table, read_data_dictionary"},
            {"id": 2, "goal": "Compute the 2023 overall gross margin",
             "depends_on": [1], "hint": "run_sql with DISTINCT and joins"},
        ]}))
    if "EXECUTOR" in sysmsg:
        step_id = -1
        for m in reversed(msgs):
            if m.get("role") == "user" and "== YOUR STEP ==" in (m.get("content") or ""):
                mm = re.search(r"id: (\d+)", m["content"])
                step_id = int(mm.group(1)) if mm else -1
                break
        n_tc = sum(1 for x in msgs if x.get("role") == "assistant" and x.get("tool_calls"))
        if step_id == 1:
            return _text_resp(payload, "FAILED: dictionary tool unexpectedly unavailable")
        if step_id == 3:
            if n_tc == 0:
                return _tool_resp(payload, [("read_data_dictionary", {})])
            return _text_resp(payload, "FINAL: dictionary read; duplicate rows, 999.0 sentinel "
                                       "rows and DEFAULT_TARGET=400 documented.")
        if step_id == 4:
            if n_tc == 0:
                return _tool_resp(payload, [("run_sql", {
                    "sql": "SELECT DISTINCT region, year, amount FROM sales WHERE year=2023"})])
            return _text_resp(payload, "FINAL: computed overall gross margin = 0.55")
        raise AssertionError(f"S3: unexpected step id {step_id}")
    if "REPORTER" in sysmsg:
        return _text_resp(payload, "FINAL: the 2023 overall gross margin is 0.55")
    raise AssertionError("S3: unexpected call:\n" + sysmsg[:200])


def s4_reflexion_t3(payload):
    sysmsg = _system(payload)
    if "REFLECTION" in sysmsg:
        return _text_resp(payload, "Reject compound tokens early with a strict regex fullmatch; "
                                   "truncate milliseconds toward zero; raise ValueError on any "
                                   "non-lowercase unit.")
    if "ReAct" in sysmsg:
        msgs = payload["messages"]
        has_memory = "PREVIOUS failed attempts" in sysmsg
        n_tc = sum(1 for m in msgs if m.get("role") == "assistant" and m.get("tool_calls"))
        if n_tc == 0:
            code = CODE_GOOD if has_memory else CODE_BAD
            return _tool_resp(payload, [("write_solution", {"code": code})])
        return _text_resp(payload, "FINAL: implemented parse_duration per spec.")
    raise AssertionError("S4: unexpected call")


def s5_cot(payload):
    return _text_resp(payload, "FINAL: the gross margin is probably around 0.4")


def main() -> int:
    cfg = load_with_defaults(None)

    print("== S1: ReAct / T1 happy path (with ArgError self-heal) ==")
    install(s1_react_t1)
    task = make_task("T1", "easy")
    llm = make_llm()
    out = FRAMEWORKS["react"](task, llm, llm.trace, cfg)
    ev = task.evaluate(out.final_answer, out.effects)
    expect("S1 strict ok", ev.ok, ev.detail)
    expect("S1 status completed", out.status == "completed")
    expect("S1 guard never fired", llm.trace.count("guard") == 0)
    expect("S1 metrics present", llm.trace.llm_calls() >= 6
           and llm.trace.peak_prompt_tokens() > 0
           and llm.trace.total_cost_cny() > 0
           and llm.trace.to_first_action_ms() is not None,
           f"llm={llm.trace.llm_calls()} peak={llm.trace.peak_prompt_tokens()} "
           f"cost={llm.trace.total_cost_cny():.4f}")
    task.cleanup()

    print("== S2: ReAct repeated-action guard ==")
    install(s2_react_guard)
    cfg_guard = load_with_defaults(None)
    cfg_guard["defaults"]["max_steps"] = 6
    task = make_task("T1", "easy")
    llm = make_llm()
    out = FRAMEWORKS["react"](task, llm, llm.trace, cfg_guard)
    expect("S2 guard fired >= 1", llm.trace.count("guard") >= 1)
    guard_payload = next((e.payload for e in llm.trace.events if e.kind == "guard"), None)
    expect("S2 guard reason repeated_action", guard_payload is not None
           and guard_payload.get("reason") == "repeated_action")
    ev = task.evaluate(out.final_answer, out.effects)
    expect("S2 strict fails (nothing fixed)", not ev.ok)
    task.cleanup()

    print("== S3: Plan-and-Execute / T2 with local re-plan ==")
    install(s3_plan_t2)
    task = make_task("T2", "easy")
    llm = make_llm()
    out = FRAMEWORKS["plan_execute"](task, llm, llm.trace, cfg)
    ev = task.evaluate(out.final_answer, out.effects)
    expect("S3 strict ok", ev.ok, ev.detail)
    expect("S3 one replan", out.info.get("replans") == 1, str(out.info))
    expect("S3 plan events >= 3 (initial+replan+final)", llm.trace.count("plan") >= 3)
    expect("S3 effects include worker-thread run_sql",
           any(e.get("tool") == "run_sql" for e in out.effects if e.get("kind") == "call"))
    expect("S3 final answer carries the value", "0.55" in out.final_answer, out.final_answer[:80])
    task.cleanup()

    print("== S4: Reflexion / T3 (fail -> reflect -> succeed) ==")
    install(s4_reflexion_t3)
    task = make_task("T3", "easy")
    llm = make_llm()
    out = FRAMEWORKS["reflexion"](task, llm, llm.trace, cfg)
    ev = task.evaluate(out.final_answer, out.effects)
    expect("S4 strict ok", ev.ok, ev.detail)
    expect("S4 two epochs", out.info.get("epochs") == 2, str(out.info))
    expect("S4 one reflection", out.info.get("reflections") == 1)
    expect("S4 eval events == 2", llm.trace.count("eval") == 2)
    expect("S4 reflection event present", llm.trace.count("reflection") == 1)
    task.cleanup()

    print("== S5: CoT / T2 (tool-less baseline) ==")
    install(s5_cot)
    task = make_task("T2", "easy")
    llm = make_llm()
    out = FRAMEWORKS["cot"](task, llm, llm.trace, cfg)
    ev = task.evaluate(out.final_answer, out.effects)
    expect("S5 strict fails as designed", not ev.ok, ev.detail)
    expect("S5 exactly one llm call", llm.trace.llm_calls() == 1)
    task.cleanup()

    print()
    if FAILURES:
        print(f"SMOKE TEST FAILED: {len(FAILURES)}: {FAILURES}")
        return 1
    print("SMOKE TEST: all green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
