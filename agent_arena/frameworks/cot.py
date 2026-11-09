"""CoT: a single tool-less call. The null baseline.

Its only job is to establish the floor: what "no scaffolding" buys on each
task family.  On T1/T2 (environment interaction needed) it is expected to
fail; on T3 (pure generation) it can legitimately succeed.
"""

from __future__ import annotations

from typing import Dict

from ..llm import LLMClient, Trace
from ..tasks.base import RunResult, TaskBase

SYSTEM = """You have NO tools. Solve the task by reasoning step by step.
When you are done, output your final answer starting with "FINAL:" on its
own first line, followed by the answer in the format the task requires."""


def run_cot(task: TaskBase, llm: LLMClient, trace: Trace, cfg: Dict) -> RunResult:
    answer = llm.text(SYSTEM, task.prompt, step=0, label="cot")
    text = (answer or "").strip()
    final = text[6:].strip() if text.upper().startswith("FINAL:") else text
    return RunResult(final_answer=final, status="completed", effects=[],
                     info={"llm_calls": 1})
