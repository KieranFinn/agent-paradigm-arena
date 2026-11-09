"""Task base class and the data contracts shared by tasks/frameworks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..llm import Trace
from ..tools import Toolbox


@dataclass
class TaskMeta:
    name: str            # "T1" | "T2" | "T3"
    title: str           # human label
    difficulty: str      # "easy" | "hard"
    structural_type: str  # "explore" | "plan" | "verify"


@dataclass
class EvalResult:
    ok: bool
    score: float
    checks: List[Dict[str, Any]] = field(default_factory=list)  # [{name, ok, detail}]
    detail: str = ""
    feedback: str = ""  # given back to Reflexion's reflect() on failure


@dataclass
class RunResult:
    """What a framework returns from one attempt at a task."""

    final_answer: str
    status: str = "completed"      # completed | steps_exhausted | budget_exceeded | error
    effects: List[Dict[str, Any]] = field(default_factory=list)
    info: Dict[str, Any] = field(default_factory=dict)  # replans/epochs/reflections/steps...
    error: str = ""


class TaskBase:
    """One task instance = one (task, difficulty) cell of one run.

    ``materialize()`` builds a FRESH environment (virtual repo / sqlite db /
    solution dir) and must be called before tool use; frameworks may call it
    once per attempt (Reflexion calls it once per epoch so every retry faces
    a pristine environment).
    """

    meta: TaskMeta

    @property
    def prompt(self) -> str:
        raise NotImplementedError

    def materialize(self, trace: "Trace") -> Toolbox:
        raise NotImplementedError

    def evaluate(self, answer: str, effects: List[Dict[str, Any]]) -> EvalResult:
        """strict: hard verifier. Only trusts effects, never self-report."""
        raise NotImplementedError

    def evaluate_schema(self, answer: str) -> EvalResult:
        """weak: shape-only check used to demo a weak verifier."""
        text = (answer or "").strip()
        ok = bool(text) and ("FINAL" in text or len(text) > 40)
        return EvalResult(
            ok=ok,
            score=1.0 if ok else 0.0,
            checks=[{"name": "answer_shape", "ok": ok, "detail": f"len={len(text)}"}],
            detail="schema check: answer has final-answer shape",
            feedback="The answer does not even look like a final answer.",
        )

    def evaluate_llm_judge(self, answer: str, llm: Any, step: int = 0) -> EvalResult:
        from ..evaluators import lopsided_judge

        return lopsided_judge(self.prompt, answer, llm, step=step)

    def cleanup(self) -> None:
        pass

    @staticmethod
    def _check(name: str, ok: bool, detail: str = "") -> Dict[str, Any]:
        return {"name": name, "ok": bool(ok), "detail": detail[:400]}
