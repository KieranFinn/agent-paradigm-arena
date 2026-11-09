"""Plan-and-Execute: explicit plan DAG -> parallel step executors -> re-planner.

Structural facts this implementation makes observable:
- A Step is an EXPLICIT object with a status (PENDING/RUNNING/DONE/FAILED/
  SKIPPED); ReAct has no such object.  This is the essential difference.
- Each executor's context is CONSTANT per step: task + upstream results +
  this step + hint.  It never accumulates other steps' trajectories, so
  peak prompt stays flat - the "clean context" hypothesis (H2).
- Steps whose dependencies are all DONE run in parallel
  (ThreadPoolExecutor, min(4, len(ready)) workers).
- A FAILED step triggers the re-planner.  scope=local replaces the failed
  node and its transitive downstream, keeping DONE steps and untouched
  PENDING siblings (design decision, docs/design.md: the doc's original
  code also dropped pending siblings; we keep them and document it).
  scope=global discards the whole plan.

Known limitation kept ON PURPOSE (project-overview.md §7.1 #4): steps that
are already DONE are never re-validated after a local re-plan; if the new
sub-plan invalidates their premises, the inconsistency survives.  This is
exactly what experiment E2 (local vs global) is designed to expose.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from ..llm import BudgetExceeded, LLMClient, Msg, Trace
from ..tasks.base import RunResult, TaskBase
from ..tools import Toolbox, attach_sink_to_thread, effects_scope

PLANNER_SYSTEM = """You are the PLANNER of a plan-and-execute agent. Given a task
and the available tools, produce a short execution plan as strict JSON:

{"steps": [{"id": 1, "goal": "...", "depends_on": [], "hint": "..."}, ...]}

Rules:
- At most {max_steps} steps; keep plans minimal (3-6 steps typically).
- Each step must be completable by one executor agent using the tools.
- "depends_on" lists the ids of steps that must be DONE before this step.
- Steps without pending dependencies run in PARALLEL: split independent
  work into sibling steps instead of one big step.
- "hint" (<= 30 words) tells the executor how to do the step (which tools
  to use, what to watch out for).
- The final step must gather whatever the task's final answer needs.
Reply with ONLY the JSON object, no prose.
{extra}"""

REPLAN_LOCAL_SYSTEM = """You are the RE-PLANNER of a plan-and-execute agent. A step FAILED.
Produce a REPLACEMENT plan fragment as strict JSON:

{"steps": [{"id": N, "goal": "...", "depends_on": [], "hint": "..."}, ...]}

Rules:
- Replace ONLY the failed step's work and its downstream effects.
- Kept (DONE) steps are listed with their results; you may reference their
  ids in depends_on. New steps MUST use ids >= {next_id}.
- Keep the fragment minimal. Reply with ONLY the JSON object.
- Learn from the failure reason; do not repeat the failed approach."""

REPLAN_GLOBAL_SYSTEM = """You are the RE-PLANNER of a plan-and-execute agent. The previous plan
failed and is being discarded. Produce a BRAND-NEW full plan as strict JSON
(same schema: {"steps": [{"id": 1, "goal": "...", "depends_on": [], "hint": "..."}, ...]}).
Learn from the failure; keep it minimal. Reply with ONLY the JSON object."""

EXECUTOR_SYSTEM = """You are the EXECUTOR of ONE step of a larger plan. You see the overall
task, the results of upstream steps, and YOUR step. Complete ONLY your step
with the tools. Do not do other steps' work.

When your step is done, reply WITHOUT tool calls, starting with
"FINAL: <concise result of this step, include concrete values>".
If after honest attempts your step cannot be done, reply
"FAILED: <one-line reason>".

You have at most {max_turns} tool turns for this step. Results with
"ok": false are errors ("arg" = your call was malformed, "tool" =
environment limit); react to them instead of repeating them."""

FINALIZER_SYSTEM = """You are the REPORTER of a plan-and-execute agent. Given the task and
the completed plan results, write the final answer. It must start with
"FINAL:" and follow the task's answer-format requirements exactly (values,
file names, descriptions - whatever the task asked for). If the plan
failed to produce the needed information, say so honestly in the answer."""

_NUDGE = (
    "Reply either with tool calls, or with 'FINAL: <result>' if your step "
    "is done, or with 'FAILED: <reason>' if it cannot be done."
)


@dataclass
class PStep:
    id: int
    goal: str
    hint: str = ""
    depends_on: List[int] = field(default_factory=list)
    status: str = "PENDING"  # PENDING | RUNNING | DONE | FAILED | SKIPPED
    result: str = ""
    failure: str = ""
    turns: int = 0

    def brief(self) -> Dict:
        return {"id": self.id, "goal": self.goal, "hint": self.hint,
                "depends_on": self.depends_on, "status": self.status}


def _extract_final(text: str) -> str:
    t = (text or "").strip()
    return t[6:].strip() if t.upper().startswith("FINAL:") else t


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " ...[truncated]"


def _fmt(tmpl: str, **kw) -> str:
    """str.replace-based templating: prompts contain literal JSON with braces,
    so str.format() would misparse them."""
    out = tmpl
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _validate_steps(raw, valid_ids: Optional[Set[int]], max_steps: int) -> List[PStep]:
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        raise ValueError("plan JSON must be {\"steps\": [...]}")
    steps: List[PStep] = []
    seen: Set[int] = set()
    candidates: List[tuple] = []
    for item in raw["steps"][:max_steps]:
        if not isinstance(item, dict):
            continue
        try:
            sid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if sid in seen:
            continue
        goal = str(item.get("goal") or "").strip()
        if not goal:
            continue
        deps_raw = item.get("depends_on") or []
        deps = []
        for d in deps_raw if isinstance(deps_raw, list) else []:
            try:
                deps.append(int(d))
            except (TypeError, ValueError):
                continue
        candidates.append((sid, goal, str(item.get("hint") or "")[:400], deps))
        seen.add(sid)
    if not candidates:
        raise ValueError("plan contains no usable steps")
    # keep only deps that point at real steps (new siblings or kept ids);
    # new steps may reference each other in any order
    all_ids = seen | (valid_ids or set())
    steps = [
        PStep(id=sid, goal=goal, hint=hint,
              depends_on=[d for d in deps if d != sid and d in all_ids])
        for sid, goal, hint, deps in candidates
    ]
    # cycle check
    by_id = {s.id: s for s in steps}
    for s in steps:
        stack, seen_walk = [s.id], set()
        while stack:
            cur = stack.pop()
            if cur in seen_walk:
                raise ValueError(f"dependency cycle at step {cur}")
            seen_walk.add(cur)
            stack.extend(d for d in by_id.get(cur, PStep(0, "")).depends_on if d in by_id)
    return steps


def _downstream(steps: List[PStep], ids: Set[int]) -> Set[int]:
    """Transitive downstream of the given step ids (including themselves)."""
    out = set(ids)
    changed = True
    while changed:
        changed = False
        for s in steps:
            if s.id not in out and any(d in out for d in s.depends_on):
                out.add(s.id)
                changed = True
    return out


def _ancestors(steps: List[PStep], step: PStep) -> List[PStep]:
    by_id = {s.id: s for s in steps}
    out: Dict[int, PStep] = {}
    stack = list(step.depends_on)
    while stack:
        d = stack.pop()
        if d in out or d not in by_id:
            continue
        out[d] = by_id[d]
        stack.extend(by_id[d].depends_on)
    return [out[k] for k in sorted(out)]


def _tools_brief(toolbox: Toolbox) -> str:
    return "\n".join(
        f"- {s['function']['name']}: {s['function']['description']}"
        for s in toolbox.schemas()
    )


def _planner_json(llm: LLMClient, trace: Trace, system: str, user: str, label: str,
                  valid_ids: Optional[Set[int]], max_steps: int) -> List[PStep]:
    raw = llm.json_call(system, user, step=0, label=label)
    return _validate_steps(raw, valid_ids, max_steps)


def _run_step(step: PStep, task: TaskBase, toolbox: Toolbox, llm: LLMClient,
              trace: Trace, cfg: Dict, upstream_text: str, sink) -> None:
    """Execute one step with a CONSTANT context (mini ReAct bounded loop)."""
    attach_sink_to_thread(sink)
    max_turns = int(cfg["plan"]["max_step_turns"])
    system = _fmt(EXECUTOR_SYSTEM, max_turns=max_turns)
    user = (
        f"== TASK ==\n{task.prompt}\n\n"
        f"== UPSTREAM STEP RESULTS ==\n{upstream_text}\n\n"
        f"== YOUR STEP ==\nid: {step.id}\ngoal: {step.goal}\nhint: {step.hint}\n\n"
        "Complete ONLY this step."
    )
    messages: List = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    streak: Dict[str, int] = {}
    step.status = "RUNNING"

    for turn in range(max_turns):
        msg: Msg = llm.chat(messages, tools=toolbox.schemas(), step=step.id,
                            label=f"plan.step{step.id}")
        messages.append(msg.to_api())
        if not msg.tool_calls:
            text = (msg.content or "").strip()
            up = text.upper()
            if up.startswith("FINAL:"):
                step.status, step.result, step.turns = "DONE", _extract_final(text), turn + 1
                return
            if up.startswith("FAILED:"):
                step.status, step.failure, step.turns = "FAILED", text[7:].strip(), turn + 1
                return
            messages.append({"role": "user", "content": _NUDGE})
            continue

        current_keys = []
        for tc in msg.tool_calls:
            key = f"{tc.name}|{json.dumps(tc.args, sort_keys=True, ensure_ascii=False, default=str)}"
            current_keys.append(key)
            streak[key] = streak.get(key, 0) + 1
            result = toolbox.run(tc.name, tc.args, step=step.id)
            if streak[key] >= 3:
                trace.add("guard", step=step.id, payload={
                    "tool": tc.name, "reason": "repeated_action", "count": streak[key]})
                result = {"ok": False, "kind": "arg", "guard": "repeated_action",
                          "error": "identical tool call repeated 3 times; change approach"}
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(_shrink(result, 1500), ensure_ascii=False),
            })
        for k in list(streak):
            if k not in current_keys:
                del streak[k]

    step.status, step.failure, step.turns = "FAILED", "step turn budget exhausted", max_turns


def _shrink(result: Dict, limit: int) -> Dict:
    s = json.dumps(result, ensure_ascii=False)
    if len(s) <= limit:
        return result
    out = dict(result)
    for key in ("content", "output", "map", "dictionary", "log", "result"):
        if isinstance(out.get(key), str) and len(out[key]) > limit - 300:
            out[key] = out[key][: limit - 300] + "\n...[truncated]"
    return out


def run_plan_execute(task: TaskBase, llm: LLMClient, trace: Trace, cfg: Dict) -> RunResult:
    max_plan_steps = int(cfg["plan"]["max_steps"])
    max_replans = int(cfg["plan"]["max_replans"])
    scope = str(cfg["plan"].get("replan_scope", "local"))

    with effects_scope() as sink:
        toolbox = task.materialize(trace)
        tools_brief = _tools_brief(toolbox)
        replans = 0

        try:
            try:
                steps = _planner_json(
                    llm, trace,
                    _fmt(PLANNER_SYSTEM, max_steps=max_plan_steps, extra=""),
                    f"== TASK ==\n{task.prompt}\n\n== TOOLS ==\n{tools_brief}",
                    "plan.initial", None, max_plan_steps)
            except ValueError as e:
                return RunResult(final_answer="", status="error", effects=sink.snapshot(),
                                 info={"replans": 0, "error": f"planner failed: {e}"})
            trace.add("plan", payload={"trigger": "initial", "steps": [s.brief() for s in steps]})

            for _iteration in range(60):
                pending = [s for s in steps if s.status == "PENDING"]
                if not pending:
                    break
                by_id = {s.id: s for s in steps}
                ready = [s for s in pending
                         if all(by_id.get(d) is not None and by_id[d].status == "DONE"
                                for d in s.depends_on)]
                if not ready:
                    failed_any = any(s.status == "FAILED" for s in steps)
                    if failed_any and replans < max_replans:
                        steps, replans = _replan(task, llm, trace, cfg, steps, tools_brief, scope, replans)
                        continue
                    for s in pending:
                        s.status = "SKIPPED"
                        s.failure = "blocked (upstream failed)"
                    break

                upstream_cache: Dict[int, str] = {}
                for s in ready:
                    anc = _ancestors(steps, s)
                    upstream_cache[s.id] = "\n\n".join(
                        f"[step {a.id} - DONE] goal: {a.goal}\nresult: {_truncate(a.result, 1200)}"
                        for a in anc
                    ) or "(no upstream results - your step has no dependencies)"

                workers = min(4, len(ready))
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futures = [ex.submit(_run_step, s, task, toolbox, llm, trace, cfg,
                                         upstream_cache[s.id], sink) for s in ready]
                    for f in futures:
                        f.result()

                if any(s.status == "FAILED" for s in steps):
                    if replans < max_replans:
                        steps, replans = _replan(task, llm, trace, cfg, steps, tools_brief, scope, replans)
                        continue
                    for s in steps:
                        if s.status == "PENDING":
                            s.status = "SKIPPED"
                            s.failure = "skipped after final failure"
                    break

            trace.add("plan", payload={"trigger": "final", "steps": [s.brief() for s in steps]})

            done_text = "\n\n".join(
                f"[step {s.id} - {s.status}] goal: {s.goal}"
                + (f"\nresult: {_truncate(s.result, 1200)}" if s.result else "")
                + (f"\nfailure: {_truncate(s.failure, 300)}" if s.failure else "")
                for s in sorted(steps, key=lambda x: x.id)
            )
            answer = llm.text(
                FINALIZER_SYSTEM,
                f"== TASK ==\n{task.prompt}\n\n== PLAN RESULTS ==\n{done_text}",
                step=0, label="plan.final_answer")
            return RunResult(
                final_answer=_extract_final(answer),
                status="completed",
                effects=sink.snapshot(),
                info={
                    "replans": replans,
                    "plan_events": trace.count("plan"),
                    "steps": len(steps),
                    "failed_steps": sum(1 for s in steps if s.status == "FAILED"),
                    "skipped_steps": sum(1 for s in steps if s.status == "SKIPPED"),
                },
            )
        except BudgetExceeded:
            return RunResult(final_answer="", status="budget_exceeded", effects=sink.snapshot(),
                             info={"replans": replans})


def _replan(task: TaskBase, llm: LLMClient, trace: Trace, cfg: Dict,
            steps: List[PStep], tools_brief: str, scope: str, replans: int):
    failed = [s for s in steps if s.status == "FAILED"]
    failed_ids = _downstream(steps, {s.id for s in failed})

    if scope == "local":
        kept = [s for s in steps if s.id not in failed_ids]
        next_id = max((s.id for s in steps), default=0) + 1
        kept_brief = "\n".join(
            f"- [step {s.id} - {s.status}] goal: {s.goal}"
            + (f" | result: {_truncate(s.result, 600)}" if s.result else "")
            for s in kept) or "(none)"
        fail_brief = "\n".join(
            f"- step {s.id} FAILED: goal '{s.goal}' reason: {_truncate(s.failure, 300)}"
            for s in failed)
        user = (
            f"== TASK ==\n{task.prompt}\n\n== TOOLS ==\n{tools_brief}\n\n"
            f"== KEPT STEPS (do not redo) ==\n{kept_brief}\n\n"
            f"== FAILED STEPS ==\n{fail_brief}\n\n"
            f"New step ids must be >= {next_id}."
        )
        new_steps = _planner_json(llm, trace, _fmt(REPLAN_LOCAL_SYSTEM, next_id=next_id),
                                  user, f"plan.replan{replans + 1}",
                                  {s.id for s in kept} | {s.id for s in steps if s.id in failed_ids},
                                  int(cfg["plan"]["max_steps"]))
        merged = kept + new_steps
        trace.add("plan", payload={"trigger": f"replan:local#{replans + 1}",
                                   "steps": [s.brief() for s in merged],
                                   "replaced_ids": sorted(failed_ids)})
        return merged, replans + 1

    statuses = "\n".join(
        f"- [step {s.id} - {s.status}] goal: {s.goal}"
        + (f" | failure: {_truncate(s.failure, 200)}" if s.failure else "")
        + (f" | result: {_truncate(s.result, 400)}" if s.result else "")
        for s in steps)
    user = (
        f"== TASK ==\n{task.prompt}\n\n== TOOLS ==\n{tools_brief}\n\n"
        f"== FAILED PLAN (discarded) ==\n{statuses}"
    )
    new_steps = _planner_json(llm, trace, REPLAN_GLOBAL_SYSTEM, user,
                              f"plan.replan{replans + 1}", None, int(cfg["plan"]["max_steps"]))
    trace.add("plan", payload={"trigger": f"replan:global#{replans + 1}",
                               "steps": [s.brief() for s in new_steps]})
    return new_steps, replans + 1
