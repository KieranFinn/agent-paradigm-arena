"""ReAct: single reasoning-acting loop over a grow-only message list.

The structural facts this implementation makes observable (and the blog
relies on):
- ``messages`` never shrinks: every turn re-sends the full history
  -> peak prompt tokens grow roughly linearly with turns (the API's
  statelessness, made visible).
- A repeated-action guard fires when the same (tool, normalized args)
  appears in 3 consecutive turns, replacing the result with
  ``{"ok": false, "guard": "repeated_action"}``.
- Tool results longer than 4000 chars are truncated before re-entering
  the context (only per-result; long files still inflate the context -
  by design, ReAct's inflation must be observed, not hidden).
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from ..llm import BudgetExceeded, LLMClient, Msg, Trace
from ..tasks.base import RunResult, TaskBase
from ..tools import Toolbox, effects_scope

SYSTEM_TMPL = """You are an autonomous agent using the ReAct (Reason + Act) loop.

At every turn:
1. Reason briefly about what you know and what you need next.
2. Then call the tool(s) that make progress. Only call listed tools.
   Multiple independent tool calls in one turn are allowed.
3. Study each tool result before deciding the next step. Results with
   "ok": false are errors: "kind": "arg" means YOUR call was malformed,
   "kind": "tool" means an environment limit - react accordingly.
4. Never fabricate tool results or file/data contents you have not read.

When - and only when - the task is fully solved and verified, reply
WITHOUT tool calls, starting your message with "FINAL:" followed by the
answer required by the task. You have at most {max_steps} tool turns;
wasted turns reduce your score.
{extra}"""

_NUDGE = (
    "You replied without tool calls and without a final answer. Either call "
    "a tool to make progress, or reply with a message starting with 'FINAL:'."
)


def _shrink(result: Dict, limit: int = 4000) -> Dict:
    """Truncate an over-long tool result before it re-enters the context."""
    s = json.dumps(result, ensure_ascii=False)
    if len(s) <= limit:
        return result
    out = json.loads(json.dumps(result, ensure_ascii=False))  # deep copy
    for key in ("content", "output", "map", "dictionary", "log", "result"):
        if isinstance(out.get(key), str) and len(out[key]) > limit - 500:
            out[key] = out[key][: limit - 500] + "\n...[truncated]"
    s2 = json.dumps(out, ensure_ascii=False)
    if len(s2) > limit + 800:  # last resort: hard cut
        out = {"ok": out.get("ok"), "note": "result truncated", "preview": s2[: limit - 100]}
    return out


def _extract_final(content: str) -> str:
    text = (content or "").strip()
    if text.upper().startswith("FINAL:"):
        return text[6:].strip()
    return text


def run_react(
    task: TaskBase,
    llm: LLMClient,
    trace: Trace,
    cfg: Dict,
    extra_context: str = "",
) -> RunResult:
    max_steps = int(cfg["defaults"]["max_steps"])
    max_calls = int(cfg["defaults"]["max_llm_calls"])
    system = SYSTEM_TMPL.format(max_steps=max_steps, extra=extra_context)

    with effects_scope() as sink:
        toolbox: Optional[Toolbox] = task.materialize(trace)
        messages: List = [
            {"role": "system", "content": system},
            {"role": "user", "content": task.prompt},
        ]
        streak: Dict[str, int] = {}
        steps_used = 0
        guard_fired = 0
        last_content = ""

        try:
            for step in range(max_steps):
                if trace.llm_calls() >= max_calls:
                    break
                msg: Msg = llm.chat(messages, tools=toolbox.schemas(), step=step, label="react")
                messages.append(msg.to_api())
                last_content = msg.content or ""

                if not msg.tool_calls:
                    if (msg.content or "").strip().upper().startswith("FINAL:"):
                        return RunResult(
                            final_answer=_extract_final(msg.content),
                            status="completed",
                            effects=sink.snapshot(),
                            info={"steps": steps_used, "llm_calls": trace.llm_calls(),
                                  "guard_fired": guard_fired},
                        )
                    messages.append({"role": "user", "content": _NUDGE})
                    continue

                steps_used += 1
                current_keys = []
                for tc in msg.tool_calls:
                    key = f"{tc.name}|{json.dumps(tc.args, sort_keys=True, ensure_ascii=False, default=str)}"
                    current_keys.append(key)
                    streak[key] = streak.get(key, 0) + 1
                    result = toolbox.run(tc.name, tc.args, step=step)
                    if streak[key] >= 3:
                        guard_fired += 1
                        trace.add("guard", step=step, payload={
                            "tool": tc.name, "reason": "repeated_action", "count": streak[key]})
                        result = {"ok": False, "kind": "arg", "guard": "repeated_action",
                                  "error": "the same tool call with identical arguments was "
                                           "repeated 3 times in a row; change your approach"}
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(_shrink(result), ensure_ascii=False),
                    })
                for k in list(streak):
                    if k not in current_keys:
                        del streak[k]
        except BudgetExceeded:
            return RunResult(final_answer="", status="budget_exceeded",
                             effects=sink.snapshot(),
                             info={"steps": steps_used, "llm_calls": trace.llm_calls(),
                                   "guard_fired": guard_fired})

        return RunResult(
            final_answer=_extract_final(last_content),
            status="steps_exhausted",
            effects=sink.snapshot(),
            info={"steps": steps_used, "llm_calls": trace.llm_calls(),
                  "guard_fired": guard_fired},
        )
