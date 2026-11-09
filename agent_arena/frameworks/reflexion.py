"""Reflexion: outer epoch loop + distilled verbal memory pool.

Control flow (per epoch):
1. inner loop = run_react with the memory rendered as extra system context
   (or a single shot when the task has no tools);
2. evaluate the attempt with the configured evaluator gear
   (strict by default; E3 swaps it for schema / llm_judge);
3. on failure, reflect: condense THIS epoch's trajectory + the evaluator
   feedback into <= 80 words of concrete lessons, push to the memory pool.

Fixes applied relative to the original draft (project-overview.md §7.1):
- the evaluator now receives the REAL effects of the inner attempt
  (issue #1: it used to pass an empty list, distorting T1/T2 scores);
- _condense_trajectory actually honours its from-index so each reflection
  only sees the current epoch, not the whole history (issue #2).
"""

from __future__ import annotations

from typing import Dict, List

from ..evaluators import run_evaluator
from ..llm import BudgetExceeded, LLMClient, Trace
from ..tasks.base import RunResult, TaskBase
from .react import run_react

REFLECT_SYSTEM = """You are the REFLECTION module of a Reflexion agent. A previous
attempt at the task failed. You get (1) a condensed trajectory of the failed
attempt and (2) the evaluator's feedback.

Write at most 80 words of CONCRETE, actionable lessons for the next attempt:
what specifically to do differently (which files/tables to read first, which
formula to apply, which edge cases to handle, which errors to stop
repeating). No praise, no apologies, no restating the obvious."""

SINGLE_SHOT_SYSTEM = """You are a careful engineer. Solve the task by reasoning
step by step, then output the required deliverable. Start your final answer
with "FINAL:"."""


class _Memory:
    def __init__(self, size: int) -> None:
        self.size = size
        self.items: List[str] = []

    def add(self, lesson: str) -> None:
        self.items.append(lesson.strip())
        if len(self.items) > self.size:
            self.items = self.items[-self.size:]

    def render(self) -> str:
        if not self.items:
            return ""
        lines = "\n".join(f"{i}) {item}" for i, item in enumerate(self.items, 1))
        return (
            "\n\nLessons learned from your PREVIOUS failed attempts (oldest first):\n"
            f"{lines}\n"
            "Avoid these mistakes. Do not repeat actions that already failed."
        )


def _condense_trajectory(trace: Trace, from_idx: int, limit: int = 6000) -> str:
    """Readable one-line-per-event digest of THIS epoch's events, tail-cut."""
    lines: List[str] = []
    for e in trace.events[from_idx:]:
        if e.kind == "tool_call":
            args = ", ".join(f"{k}={str(v)[:80]}" for k, v in (e.payload.get("args") or {}).items())
            lines.append(f"TOOL {e.payload.get('tool')}({args[:200]})")
        elif e.kind == "tool_result":
            res = e.payload.get("result") or {}
            brief = res.get("summary") or res.get("error") or str(res)[:120]
            lines.append(f"  -> ok={res.get('ok')} {str(brief)[:160]}")
        elif e.kind == "guard":
            lines.append(f"GUARD {e.payload.get('tool')}: {e.payload.get('reason')}")
        elif e.kind == "plan":
            lines.append(f"PLAN {e.payload.get('trigger')}")
    text = "\n".join(lines)
    return text[-limit:] if len(text) > limit else text


def _extract_final(text: str) -> str:
    t = (text or "").strip()
    return t[6:].strip() if t.upper().startswith("FINAL:") else t


def _single_shot(task: TaskBase, llm: LLMClient, trace: Trace, extra: str) -> RunResult:
    answer = llm.text(SINGLE_SHOT_SYSTEM + extra, task.prompt, step=0, label="reflexion.shot")
    return RunResult(final_answer=_extract_final(answer), status="completed",
                     effects=[], info={"llm_calls": 1})


def run_reflexion(task: TaskBase, llm: LLMClient, trace: Trace, cfg: Dict) -> RunResult:
    max_epochs = int(cfg["reflexion"]["max_epochs"])
    memory_size = int(cfg["reflexion"]["memory_size"])
    evaluator_mode = str(cfg["reflexion"].get("evaluator", "strict"))
    memory = _Memory(memory_size)

    # does the task have tools at all? (all three do today; kept for safety)
    peek = task.materialize(trace)
    has_tools = bool(peek.specs)
    task.cleanup()

    reflections = 0
    inner: RunResult = RunResult(final_answer="", status="error")
    last_eval_ok = False

    try:
        for epoch in range(max_epochs):
            epoch_start = len(trace.events)
            if has_tools:
                inner = run_react(task, llm, trace, cfg, extra_context=memory.render())
            else:
                inner = _single_shot(task, llm, trace, memory.render())

            ev = run_evaluator(task, evaluator_mode, inner.final_answer,
                               effects=inner.effects, llm=llm, step=epoch)
            trace.add("eval", step=epoch, payload={
                "epoch": epoch, "mode": evaluator_mode, "ok": ev.ok,
                "checks": ev.checks, "detail": ev.detail[:400]})
            last_eval_ok = ev.ok
            if ev.ok:
                break
            if epoch < max_epochs - 1:
                condensed = _condense_trajectory(trace, epoch_start)
                lesson = llm.text(REFLECT_SYSTEM,
                                  f"== CONDENSED TRAJECTORY (attempt {epoch + 1}) ==\n{condensed}"
                                  f"\n\n== EVALUATOR FEEDBACK ==\n{ev.feedback}"
                                  "\n\nLessons for the next attempt:",
                                  step=epoch, label="reflexion.reflect",
                                  max_tokens=400)
                memory.add(lesson)
                reflections += 1
                trace.add("reflection", step=epoch, payload={
                    "epoch": epoch, "lesson": lesson[:800]})

        inner.info = dict(inner.info or {})
        inner.info.update({
            "epochs": trace.count("eval"),
            "reflections": reflections,
            "inner_evaluator": evaluator_mode,
            "last_eval_ok": last_eval_ok,
        })
        return inner
    except BudgetExceeded:
        inner.status = "budget_exceeded"
        inner.info = dict(inner.info or {})
        inner.info.update({"epochs": trace.count("eval"), "reflections": reflections})
        return inner
