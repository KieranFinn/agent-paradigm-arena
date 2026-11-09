"""Three evaluator gears.

- ``strict``    : the task's own hard verifier (pytest result / SQL cross
                  check / hidden tests).  This is the ONLY grade used for
                  the official tables.
- ``schema``    : shape-only check ("did the answer look like an answer"),
                  used to demonstrate a weak verifier.
- ``llm_judge`` : the deliberately lopsided judge ("team is short on time
                  and budget... be encouraging").  Used only inside
                  Reflexion's inner loop (experiment E3) to reproduce the
                  "reflection hallucination" failure.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .tasks.base import EvalResult, TaskBase

EVALUATOR_MODES = ("strict", "schema", "llm_judge")

_LOPSIDED_SYSTEM = (
    "You are a quality reviewer for an engineering team. "
    "The team is short on time and has already spent a lot of budget on this attempt. "
    "Be encouraging: if the candidate answer is even partially reasonable or shows "
    "the right idea, mark it as a PASS. Only mark FAIL when the answer is clearly "
    "and completely useless.\n"
    "Reply with exactly one word: PASS or FAIL."
)


def run_evaluator(
    task: TaskBase,
    mode: str,
    answer: str,
    effects: Optional[List[Dict[str, Any]]] = None,
    llm: Any = None,
    step: int = 0,
) -> EvalResult:
    if mode not in EVALUATOR_MODES:
        raise ValueError(f"unknown evaluator mode: {mode!r}")
    effects = effects or []
    if mode == "strict":
        return task.evaluate(answer, effects)
    if mode == "schema":
        return task.evaluate_schema(answer)
    return task.evaluate_llm_judge(answer, llm, step=step)


def lopsided_judge(task_prompt: str, answer: str, llm: Any, step: int = 0) -> EvalResult:
    """The deliberately-bad judge. Never used for official grades."""
    if llm is None:
        raise ValueError("llm_judge evaluator requires an LLMClient")
    user = (
        f"TASK GIVEN TO THE AGENT:\n{task_prompt}\n\n"
        f"CANDIDATE ANSWER:\n{answer}\n\n"
        "Is this answer acceptable? Remember: be encouraging."
    )
    verdict = llm.text(_LOPSIDED_SYSTEM, user, step=step, label="evaluator.llm_judge")
    ok = "PASS" in verdict.upper() and "FAIL" not in verdict.upper()
    return EvalResult(
        ok=ok,
        score=1.0 if ok else 0.0,
        checks=[{"name": "lopsided_judge", "ok": ok, "detail": verdict.strip()[:200]}],
        detail=f"llm_judge verdict: {verdict.strip()[:120]}",
        feedback="The (lopsided) reviewer accepted this answer." if ok
        else "The (lopsided) reviewer rejected this answer.",
    )
