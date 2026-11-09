"""T3 - Verify-type task: implement functions against a spec, graded by
hidden unit tests.

Structural properties (why this task exists):
- A HARD verifier exists (the hidden test suite): every failure message
  points at a concrete boundary case -> failed attempts are capital the
  next attempt can invest.  This is the precondition for Reflexion to
  shine, and (when the evaluator is swapped for the lopsided judge, E3)
  the precondition for reproducing the reflection-hallucination failure.
- First-shot correctness is genuinely hard (boundary cases, a spec
  contradiction decoy), so multi-attempt frameworks have room to differ.

The candidate solution runs inside an isolated subprocess
(``_t3_runner.py``) with a minimal pytest stand-in (``raises`` helper,
``test_*`` discovery), never in-process.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from ..llm import Trace
from ..tools import ToolSpec, Toolbox
from .base import EvalResult, TaskBase, TaskMeta

_RUNNER = Path(__file__).with_name("_t3_runner.py")

_SPEC_EASY = """SPEC: implement the function `parse_duration(s: str) -> int)` in solution.py.

Semantics:
- Input is a single token: a non-negative integer immediately followed by
  exactly one unit suffix, one of: "ms", "s", "m", "h".
- Returns the duration in whole seconds, TRUNCATED toward zero
  (e.g. "500ms" -> 0, "1200ms" -> 1).
- Unit suffixes are case-SENSITIVE: only lowercase forms are valid
  (e.g. "500MS" is invalid).
- Compound or multi-token inputs are invalid (e.g. "1h 30m").
- Negative numbers are invalid.
- Anything invalid must raise ValueError.
- No other functions, imports or globals are required; keep the module
  side-effect free.

Submit by calling the write_solution tool with the full module source.
Each submission is executed against a hidden test suite; its output is
returned to you.
"""

_SPEC_HARD = """SPEC: implement BOTH functions in solution.py.

FUNCTION 1: `slugify(s: str) -> str`
- lowercase all ASCII letters
- keep CJK (Chinese/Japanese/Korean) characters as-is
- replace every run of consecutive characters that are NOT [a-z0-9 or CJK]
  with a single "-"
- strip leading and trailing "-"
- if the result is empty, return "-"

FUNCTION 2: `safe_int(value, default=None)`
- booleans are returned UNCHANGED (do not coerce them to 0/1)
- ints are returned as-is
- strings are stripped of surrounding whitespace; a string representing a
  plain integer (optional leading '-', decimal digits, underscores allowed
  as digit separators, e.g. "1_000") is converted with int(); otherwise
  return `default`
- anything else returns `default`

LEGACY BEHAVIOUR (kept for reference; older integrations depended on it):
in legacy versions slugify("") returned the empty string, and safe_int
coerced booleans to 0/1. The function reference above describes the
current intended surface for new integrations.

Submit by calling the write_solution tool with the full module source.
Each submission is executed against a hidden test suite; its output is
returned to you.
"""

# Hidden tests are NEVER shown to the agent. One module per function.
_TESTS_EASY = [
    '''from solution import parse_duration
from pytest_shim import raises


def test_ms_truncates_to_zero():
    assert parse_duration("500ms") == 0


def test_ms_truncates_to_one():
    assert parse_duration("1200ms") == 1


def test_seconds():
    assert parse_duration("90s") == 90


def test_minutes():
    assert parse_duration("2m") == 120


def test_hours():
    assert parse_duration("1h") == 3600


def test_unit_is_case_sensitive():
    with raises(ValueError):
        parse_duration("500MS")


def test_compound_is_invalid():
    with raises(ValueError):
        parse_duration("1h 30m")


def test_negative_is_invalid():
    with raises(ValueError):
        parse_duration("-5s")


def test_garbage_is_invalid():
    with raises(ValueError):
        parse_duration("abc")
''',
]

_TESTS_HARD = [
    '''from solution import slugify


def test_slug_lowercases_and_keeps_cjk():
    assert slugify("Hello 世界") == "hello-世界"


def test_slug_empty_returns_dash():
    assert slugify("") == "-"


def test_slug_symbols_collapse():
    assert slugify("C++ & Rust") == "c-rust"


def test_slug_run_collapse():
    assert slugify("a---b") == "a-b"


def test_slug_strips_edges():
    assert slugify("--hi--") == "hi"
''',
    '''from solution import safe_int


def test_safe_bool_unchanged():
    assert safe_int(True, None) is True
    assert safe_int(False, None) is False


def test_safe_float_string_returns_default():
    assert safe_int("3.9", 7) == 7


def test_safe_underscore_digits():
    assert safe_int("1_000", 0) == 1000


def test_safe_plain_int_passthrough():
    assert safe_int(42, 0) == 42
''',
]

_PROMPT_TMPL = """Implement the following spec exactly in a module named solution.py.

{spec}

Your final answer must start with "FINAL:" and briefly state what you
implemented. The hidden test suite is the only grading authority.
"""


class T3VerifyTask(TaskBase):
    def __init__(self, difficulty: str = "easy") -> None:
        self.difficulty = difficulty
        self.meta = TaskMeta("T3", "hidden-test implementation", difficulty, "verify")
        self._tmpdir: Optional[str] = None
        self.last_code: Optional[str] = None

    @property
    def prompt(self) -> str:
        spec = _SPEC_HARD if self.difficulty == "hard" else _SPEC_EASY
        return _PROMPT_TMPL.format(spec=spec)

    @property
    def _tests(self) -> List[str]:
        return _TESTS_HARD if self.difficulty == "hard" else _TESTS_EASY

    @property
    def _total_tests(self) -> int:
        return sum(src.count("def test_") for src in self._tests)

    # -- environment ----------------------------------------------------------
    def materialize(self, trace: "Trace") -> Toolbox:
        self.cleanup()
        self._tmpdir = tempfile.mkdtemp(prefix="arena_t3_")
        self.last_code = None
        return Toolbox(self._specs(), trace)

    def cleanup(self) -> None:
        if self._tmpdir and os.path.isdir(self._tmpdir):
            shutil.rmtree(self._tmpdir, ignore_errors=True)
        self._tmpdir = None

    # -- tools -----------------------------------------------------------------
    def _specs(self) -> List[ToolSpec]:
        return [
            ToolSpec(
                "write_solution",
                "Submit the full source code of solution.py. The hidden test "
                "suite runs on every submission and its output (including "
                "failure details) is returned.",
                {"code": "string"}, ["code"],
                self._tool_write_solution,
            ),
        ]

    def _run_hidden_tests(self, code: str) -> Dict:
        """Run the candidate against the hidden suite in a subprocess."""
        if not self._tmpdir:
            self.materialize(Trace())
        with open(os.path.join(self._tmpdir, "solution.py"), "w", encoding="utf-8") as f:
            f.write(code)
        tests_json = os.path.join(self._tmpdir, "tests.json")
        with open(tests_json, "w", encoding="utf-8") as f:
            json.dump(self._tests, f)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "LANG": "C"}
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-B", str(_RUNNER), self._tmpdir, tests_json],
                capture_output=True, text=True, timeout=20, cwd=self._tmpdir, env=env,
            )
        except subprocess.TimeoutExpired:
            from ..tools import ToolError

            raise ToolError("hidden test runner timed out after 20s (infinite loop?)")
        out = proc.stdout
        total = self._total_tests
        if "SOLUTION_IMPORT_ERROR" in out:
            passed, failed = 0, total
        else:
            m = re.search(r"SUMMARY (\d+) passed, (\d+) failed", out)
            passed, failed = (int(m.group(1)), int(m.group(2))) if m else (0, total)
        return {
            "ok": True,
            "summary": f"{passed} passed, {failed} failed",
            "passed": passed,
            "failed": failed,
            "output": out[-3000:],
        }

    def _tool_write_solution(self, code: str) -> Dict:
        self.last_code = code
        result = self._run_hidden_tests(code)
        result["note"] = "hidden tests executed; failures are listed above"
        return result

    # -- evaluation ---------------------------------------------------------------
    @staticmethod
    def _extract_code_from_answer(answer: str) -> Optional[str]:
        """For tool-less baselines (CoT): pull the code out of the answer."""
        if not answer:
            return None
        fenced = re.findall(r"```(?:python|py)?\s*\n(.*?)```", answer, re.S)
        if fenced:
            return max(fenced, key=len)
        m = re.search(r"^(?:def |import |from |class ).*$", answer, re.M)
        if m:
            return answer[m.start():]
        return None

    def _all_green(self, run: Optional[Dict]) -> bool:
        if not run:
            return False
        return run.get("failed", 1) == 0 and run.get("passed", 0) >= self._total_tests

    def evaluate(self, answer: str, effects: List[Dict]) -> EvalResult:
        calls = [e for e in effects if e.get("kind") == "call" and e.get("tool") == "write_solution"]
        submitted = bool(calls) and self.last_code is not None
        code = self.last_code
        source = "write_solution tool"
        if not submitted:
            code = self._extract_code_from_answer(answer or "")
            source = "code block in final answer"
        code_present = code is not None

        run: Optional[Dict] = None
        if code_present:
            try:
                run = self._run_hidden_tests(code)
            except Exception as e:  # runner-level failure (timeout etc.)
                run = {"summary": f"runner error: {e}", "passed": 0, "failed": 1, "output": str(e)}
        tests_pass = self._all_green(run)

        checks = [
            self._check("solution_submitted", submitted or code_present,
                        f"source: {source}" if code_present else "no code found"),
            self._check("tests_pass", tests_pass,
                        (run or {}).get("summary", "no run") if run else "no run"),
        ]
        ok = tests_pass
        failed_names = [c["name"] for c in checks if not c["ok"]]
        tail = (run or {}).get("output", "")[-600:]
        return EvalResult(
            ok=ok,
            score=sum(1 for c in checks if c["ok"]) / len(checks),
            checks=checks,
            detail="; ".join(f"{c['name']}={c['ok']}" for c in checks),
            feedback=(
                "All hidden tests passed."
                if ok
                else f"Failed checks: {', '.join(failed_names)}. Hidden test run: "
                f"{(run or {}).get('summary', 'not run')}. Recent failures:\n{tail}"
            ),
        )
