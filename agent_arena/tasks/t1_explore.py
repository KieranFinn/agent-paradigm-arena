"""T1 - Explore-type task: root-causing a failing test in a legacy repo.

Structural properties (why this task exists):
- WHICH file must be read is only knowable during execution (the error points
  at billing.py, the defect lives in hashes.py) -> rewards per-turn probing.
- Reading the "wrong" file does not immediately error -> ReAct's target-
  drift failure mode is observable.
- apply_patch returns ArgError when the anchor snippet is missing -> the
  model's ability to read errors and self-heal is observable.

Ground truth: hashes.py carries a stale ``EXPECTED_INVOICE_HASH``
("a3f1c9e2b7d4"); the correct value is compute_hash("billing-v2:in_7")
(= 67e52dd558aa).  Both the literal fix and the expression fix are accepted.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from ..llm import Trace
from ..tools import ArgError, ToolSpec, Toolbox
from .base import EvalResult, TaskBase, TaskMeta

_RUNNER = Path(__file__).with_name("_t1_runner.py")

_BILLING_PY = '''"""Invoice calculation for the billing service (v2)."""

from hashes import compute_hash, EXPECTED_INVOICE_HASH

RATE_TABLE = {"standard": 0.2, "premium": 0.35, "trial": 0.0}


def invoice_total(invoice):
    """Sum of qty*price over invoice lines, rounded to cents."""
    return round(sum(line["qty"] * line["price"] for line in invoice["lines"]), 2)


def rate_for(tier):
    return RATE_TABLE[tier]


def verify_invoice(invoice_id):
    """True when the stored expected hash matches the current signature."""
    return compute_hash(f"billing-v2:{invoice_id}") == EXPECTED_INVOICE_HASH
'''

_HASHES_PY = '''"""Hash helpers for invoice verification."""

import hashlib

# Expected hash for the current billing signature. Updated 2023-10-30 when
# the signature was bumped; see docs/CHANGELOG.md for details.
EXPECTED_INVOICE_HASH = "a3f1c9e2b7d4"


def compute_hash(signature):
    """First 12 hex chars of SHA-256 over the signature string."""
    return hashlib.sha256(signature.encode()).hexdigest()[:12]
'''

_TEST_BILLING = '''import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import billing


def test_invoice_total():
    inv = {"id": "in_7", "lines": [{"qty": 2, "price": 10.0}, {"qty": 1, "price": 5.0}]}
    assert billing.invoice_total(inv) == 25.0


def test_rate_for():
    assert billing.rate_for("standard") == 0.2
    assert billing.rate_for("premium") == 0.35


def test_verify_invoice_in7():
    assert billing.verify_invoice("in_7"), "invoice hash mismatch for in_7"
'''

_TEST_LEGACY = '''import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@unittest.skip("legacy path removed in v2")
def test_old_rates_endpoint():
    raise AssertionError("should be skipped")
'''

_API_LOG = """2023-10-31T08:12:04 INFO  GET /v1/health 200 3ms
2023-10-31T08:12:31 WARN  GET /v1/rates RETRY x2 502 upstream=pricing-svc
2023-10-31T08:12:33 INFO  GET /v1/rates 200 118ms
2023-10-31T08:14:02 INFO  POST /v1/invoices 201 45ms
2023-10-31T08:14:03 INFO  GET /v1/metrics 200 2ms
"""

_HTTP_CLIENT = '''"""Thin HTTP client used by the billing service."""

DEFAULT_TIMEOUT = None  # TODO: set after infra review (was 30s)


def request(url):
    raise NotImplementedError("handled by the shared gateway in prod")
'''

_STORAGE = '''"""Persistence helpers."""

_SCHEMA_VERSION = 2


def _normalize(record):
    # FIXME(2023-09): looks lossy but rates are re-derived upstream anyway
    return {k: v for k, v in record.items() if k not in ("_tmp", "_raw")}
'''

_METRICS = '''"""Metrics collection for the billing service."""

import math


def emit_histogram(name, value):
    if math.isnan(value):
        # WARN: nan values observed from the rates endpoint since 2023-10
        return None
    return {"name": name, "value": value}
'''

_CHANGELOG = """# Changelog

## 2023-10-30
- Invoice signature bumped to `billing-v2:in_7`. The expected hash in
  `hashes.py` must equal `compute_hash("billing-v2:in_7")` (hex, first 12
  chars). The previous signature's value was `a3f1c9e2b7d4`.

## 2023-09-12
- storage: `_normalize()` now drops `_tmp`/`_raw` keys (rates are
  re-derived upstream, no data loss).

## 2023-08-01
- Initial import of the billing service.
"""

_README = """# billing-service

Small invoicing demo service.

Run the test suite:
    python -m pytest tests/ -q
"""

_GIT_LOG = """* 8f2a11c 2023-10-30 Bump invoice signature to billing-v2 for in_7
* 5c1de90 2023-10-12 Expose rates retry counter in metrics_collector
* 3a9f4b2 2023-08-01 Initial import of the billing service
"""

_BASE_FILES = {
    "billing.py": _BILLING_PY,
    "hashes.py": _HASHES_PY,
    "http_client.py": _HTTP_CLIENT,
    "api.log": _API_LOG,
    "README.md": _README,
    "docs/CHANGELOG.md": _CHANGELOG,
    "tests/test_billing.py": _TEST_BILLING,
    "tests/test_legacy.py": _TEST_LEGACY,
}

_HARD_EXTRA = {
    "storage.py": _STORAGE,
    "metrics_collector.py": _METRICS,
}

PROMPT = """You are debugging a legacy billing-service repository. CI failed with:

  $ python -m pytest tests/ -q
  FAILED tests/test_billing.py::test_verify_invoice_in7 - AssertionError: invoice hash mismatch for in_7
  2 passed, 1 failed, 1 skipped

Task: locate the TRUE root cause of the failure (the repository contains
red herrings that are NOT the cause), fix it with a minimal change, and
verify the fix by running the tests.

Rules:
- Do not modify anything under tests/ .
- Do not silence, skip or delete the failing test.
- Your final answer must start with "FINAL:" and must name the root-cause
  file and describe the defect in one or two sentences.
"""


class T1ExploreTask(TaskBase):
    def __init__(self, difficulty: str = "easy") -> None:
        self.difficulty = difficulty
        self.meta = TaskMeta("T1", "legacy repo debugging", difficulty, "explore")
        self.files: Dict[str, str] = dict(_BASE_FILES)
        if difficulty == "hard":
            self.files.update(_HARD_EXTRA)
        self._tmpdir: Optional[str] = None
        self._orig: Dict[str, str] = dict(self.files)

    @property
    def prompt(self) -> str:
        return PROMPT

    # -- environment ---------------------------------------------------------
    def materialize(self, trace: "Trace") -> Toolbox:
        self.cleanup()
        self._tmpdir = tempfile.mkdtemp(prefix="arena_t1_")
        for rel, content in self.files.items():
            path = os.path.join(self._tmpdir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        return Toolbox(self._specs(), trace)

    def cleanup(self) -> None:
        if self._tmpdir and os.path.isdir(self._tmpdir):
            shutil.rmtree(self._tmpdir, ignore_errors=True)
        self._tmpdir = None

    # -- helpers ---------------------------------------------------------------
    def _listing(self) -> List[str]:
        """Walk the *materialized* repo so post-write state is visible."""
        if not self._tmpdir:
            return sorted(self.files.keys())
        out = []
        for root, _dirs, names in os.walk(self._tmpdir):
            for n in names:
                full = os.path.join(root, n)
                out.append(os.path.relpath(full, self._tmpdir))
        return sorted(out)

    def _resolve(self, rel: str) -> str:
        if not isinstance(rel, str) or not rel.strip():
            raise ArgError("path must be a non-empty string")
        root = os.path.abspath(self._tmpdir)
        target = os.path.abspath(os.path.join(root, rel))
        if not (target == root or target.startswith(root + os.sep)):
            raise ArgError(f"path escapes the repository: {rel!r}")
        return target

    def _read(self, rel: str) -> str:
        target = self._resolve(rel)
        if not os.path.isfile(target):
            from ..tools import ToolError

            raise ToolError(f"file not found: {rel}")
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def _write(self, rel: str, content: str) -> None:
        target = self._resolve(rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)

    def _run_runner(self, *extra: str) -> Optional[Dict]:
        if not self._tmpdir:
            return None
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "LANG": "C"}
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-B", str(_RUNNER), self._tmpdir, *extra],
                capture_output=True, text=True, timeout=25, cwd=self._tmpdir, env=env,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "test runner timed out after 25s"}
        return {"ok": proc.returncode == 0 or True, "stdout": proc.stdout, "stderr": proc.stderr}

    # -- tools -------------------------------------------------------------------
    def _specs(self) -> List[ToolSpec]:
        return [
            ToolSpec(
                "list_files", "List all files in the repository (relative paths).",
                {}, [], lambda: {"ok": True, "files": self._listing()},
            ),
            ToolSpec(
                "repo_map",
                "Show the repository tree with a one-line summary per file.",
                {}, [],
                self._tool_repo_map,
            ),
            ToolSpec(
                "read_file", "Read a file's full content.",
                {"path": "string"}, ["path"],
                lambda path: {"ok": True, "content": self._read(path)},
            ),
            ToolSpec(
                "write_file", "Create or overwrite a file with the given content.",
                {"path": "string", "content": "string"}, ["path", "content"],
                self._tool_write_file,
            ),
            ToolSpec(
                "apply_patch",
                "Replace the FIRST occurrence of `old` with `new` inside a file. "
                "Fails with kind=arg when `old` is not found.",
                {"path": "string", "old": "string", "new": "string"},
                ["path", "old", "new"],
                self._tool_apply_patch,
            ),
            ToolSpec(
                "grep", "Search a regex pattern across repository files; returns file:line:text matches.",
                {"pattern": "string"}, ["pattern"],
                self._tool_grep,
            ),
            ToolSpec(
                "git_log", "Show the recent commit history.",
                {}, [], lambda: {"ok": True, "log": _GIT_LOG},
            ),
            ToolSpec(
                "run_tests", "Run the test suite and return the summary.",
                {}, [], self._tool_run_tests,
            ),
        ]

    def _tool_repo_map(self) -> Dict:
        lines = ["."]
        entries = self._listing()
        for i, rel in enumerate(entries):
            branch = "`-- " if i == len(entries) - 1 else "|-- "
            desc = ""
            if rel.endswith(".py"):
                try:
                    m = re.search(r'^"""(.*)"""', self._read(rel), re.M)
                    if m:
                        desc = "  # " + m.group(1).strip()
                except Exception:
                    pass
            lines.append(branch + rel + desc)
        return {"ok": True, "map": "\n".join(lines)}

    def _tool_write_file(self, path: str, content: str) -> Dict:
        existed = path in self.files or os.path.isfile(self._resolve(path))
        self._write(path, content)
        return {"ok": True, "path": path, "written_bytes": len(content), "existed": existed}

    def _tool_apply_patch(self, path: str, old: str, new: str) -> Dict:
        content = self._read(path)
        if old not in content:
            raise ArgError(
                f"old snippet not found in {path!r}. Read the file and use an exact, "
                f"unique snippet (got {len(old)} chars)."
            )
        patched = content.replace(old, new, 1)
        self._write(path, patched)
        return {"ok": True, "path": path, "replaced": 1}

    def _tool_grep(self, pattern: str) -> Dict:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ArgError(f"invalid regex: {e}")
        matches = []
        for rel in self._listing():
            try:
                for lineno, line in enumerate(self._read(rel).splitlines(), 1):
                    if rx.search(line):
                        matches.append(f"{rel}:{lineno}:{line.strip()[:160]}")
                        if len(matches) >= 50:
                            return {"ok": True, "matches": matches, "truncated": True}
            except Exception:
                continue
        return {"ok": True, "matches": matches}

    def _tool_run_tests(self) -> Dict:
        out = self._run_runner()
        if out is None:
            from ..tools import ToolError

            raise ToolError("environment not materialized")
        if not out.get("ok") and "error" in out:
            from ..tools import ToolError

            raise ToolError(out["error"])
        m = re.search(r"SUMMARY (\d+) passed, (\d+) failed, (\d+) skipped", out["stdout"])
        if not m:
            from ..tools import ToolError

            raise ToolError(
                "test runner produced no summary; tail: "
                + (out["stdout"] + out["stderr"])[-400:]
            )
        passed, failed, skipped = map(int, m.groups())
        return {
            "ok": True,
            "summary": f"{passed} passed, {failed} failed, {skipped} skipped",
            "passed": passed, "failed": failed, "skipped": skipped,
            "output": out["stdout"][-3000:],
        }

    # -- evaluation ------------------------------------------------------------
    def _hash_ok(self) -> bool:
        out = self._run_runner("--check-hash")
        if not out or "stdout" not in out:
            return False
        return "HASH_OK" in out["stdout"]

    def _current(self, rel: str) -> str:
        try:
            return self._read(rel)
        except Exception:
            return ""

    def evaluate(self, answer: str, effects: List[Dict]) -> EvalResult:
        calls = [e for e in effects if e.get("kind") == "call" and e.get("tool") == "run_tests"]
        tests_run = len(calls) > 0

        fresh = None
        if self._tmpdir:
            out = self._run_runner()
            if out and "stdout" in out:
                m = re.search(r"SUMMARY (\d+) passed, (\d+) failed", out["stdout"])
                if m:
                    fresh = {"passed": int(m.group(1)), "failed": int(m.group(2))}
        tests_pass = bool(fresh and fresh["failed"] == 0 and fresh["passed"] >= 3) and tests_run

        hashes_changed = self._current("hashes.py") != self._orig["hashes.py"]
        billing_unchanged = self._current("billing.py") == self._orig["billing.py"]
        hash_ok = self._hash_ok() if (self._tmpdir and hashes_changed) else False
        fixed_root_cause = hashes_changed and hash_ok and billing_unchanged

        tests_untouched = (
            self._current("tests/test_billing.py") == self._orig["tests/test_billing.py"]
            and self._current("tests/test_legacy.py") == self._orig["tests/test_legacy.py"]
        )
        answer_states_root_cause = bool(re.search(r"hashes\.py", answer or "", re.I))

        checks = [
            self._check("tests_run", tests_run, f"run_tests called {len(calls)} time(s)"),
            self._check(
                "tests_pass", tests_pass,
                f"fresh run: {fresh} ; model ran tests: {tests_run}",
            ),
            self._check(
                "fixed_root_cause", fixed_root_cause,
                f"hashes.py changed={hashes_changed}, hash_ok={hash_ok}, billing.py unchanged={billing_unchanged}",
            ),
            self._check("tests_untouched", tests_untouched, "tests/ must stay byte-identical"),
            self._check(
                "answer_states_root_cause", answer_states_root_cause,
                "final answer must name hashes.py",
            ),
        ]
        ok = tests_pass and fixed_root_cause and tests_untouched
        failed_names = [c["name"] for c in checks if not c["ok"]]
        return EvalResult(
            ok=ok,
            score=sum(1 for c in checks if c["ok"]) / len(checks),
            checks=checks,
            detail="; ".join(f"{c['name']}={c['ok']}" for c in checks),
            feedback=(
                "All checks passed."
                if ok
                else f"Failed checks: {', '.join(failed_names)}. "
                + (f"Last test summary observed: {fresh}. " if fresh else "Tests were never run successfully. ")
                + "Remember: the grader re-runs the test suite on the final repository state, "
                "does not accept modifications under tests/, and requires the final answer to name the root-cause file."
            ),
        )
