"""T2 - Plan-type task: multi-table warehouse analysis.

Structural properties (why this task exists):
- The full information need is knowable up front (the metric formula is in
  the prompt) and decomposes into ordered, partially parallel SQL steps
  -> rewards plan-first execution.
- Deliberate landmines force mid-plan invalidation: a 403 export API (the
  "one-shot shortcut" does not exist), duplicate sales rows, a legacy
  sentinel cost row, and a missing target region -> the plan's premises can
  be falsified at step k while step k+1 was already scheduled.

Ground truth is computed by ``true_margin()`` via a ground-truth SQL
cross-check against the same database (never a hard-coded constant).

NOTE (deviation from project-overview.md §2.3, see docs/design.md): the
duplicate sales row lives IN fiscal year 2023 so that forgetting DISTINCT
actually changes the answer.  The overview's probe note ("WHERE year=2023
returns two rows") described the duplicate as invisible; that would make
trap 1 un-failable, so it is corrected here.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple

from ..llm import Trace
from ..tools import ArgError, ToolSpec, Toolbox
from .base import EvalResult, TaskBase, TaskMeta

_GROUND_TRUTH_SQL = """
WITH rev AS (SELECT DISTINCT region, year, amount FROM sales WHERE year = 2023),
     cost23 AS (SELECT region, amount FROM cost WHERE year = 2023 AND amount <> 999.0),
     tgt AS (SELECT region, target FROM targets)
SELECT ROUND(SUM((rev.amount - COALESCE(cost23.amount, 0.0)) / COALESCE(tgt.target, 400.0)), 6)
FROM rev
LEFT JOIN cost23 ON cost23.region = rev.region
LEFT JOIN tgt ON tgt.region = rev.region
"""

_DICT_EASY = """WAREHOUSE DATA DICTIONARY (v3, analytics platform team)

Table sales (region, year, amount) - per-region yearly revenue.
  NOTE: known duplicate rows exist (ETL re-runs); DEDUPLICATE on
  (region, year, amount) before aggregating.

Table cost (region, year, amount) - per-region yearly cost.
  NOTE: rows with amount = 999.0 are legacy sentinel rows from the 2022
  migration dry-run; they MUST be excluded from aggregates.

Table targets (region, target) - per-region annual sales target.
  NOTE: regions missing from this table use the company standard
  DEFAULT_TARGET = 400.0.

Coverage: fiscal years 2018-2023 only. Earlier years are outside warehouse
coverage and do not exist.

The bulk warehouse export API (fetch_warehouse_export) is permanently
denied (403) for this service account; use SQL instead.
"""

_DICT_HARD = """WAREHOUSE DATA DICTIONARY
Revision 7.2 - analytics platform team - reviewed quarterly

SECTION 1 - TABLES

sales (region TEXT, year INT, amount REAL)
  Row-level lineage is NOT guaranteed for this table: the ingestion job was
  re-run several times during the 2022 platform migration and row-level
  duplicates were not always collapsed. Downstream consumers are expected
  to deduplicate before aggregation. Partitions: fy2018-fy2023.

cost (region TEXT, year INT, amount REAL)
  Ledger extracts. Older partitions may contain sentinel rows emitted by
  the 2022 migration dry-run harness (recognizable by the exact amount
  999.0); these rows were never real spend and must not be included in
  financial aggregates. 'east' was a pilot region that never launched; its
  rows are kept for audit purposes only.

targets (region TEXT, target REAL)
  Annual sales targets by region. Regions absent from this table fall back
  to the company standard default target of 400.0 (approved in the FY23
  planning cycle, minutes 2023-02-14).

SECTION 2 - ACCESS NOTES

- Warehouse coverage starts with fiscal year 2018; earlier years were
  archived to cold storage and are not queryable.
- The bulk warehouse export API (fetch_warehouse_export) returns 403 for
  this service account; the security team has refused all exceptions.
- Scans returning more than 200 rows are rejected by the gateway.

SECTION 3 - METRICS

Overall gross margin (per the task statement) is computed region by region
and summed: sum over regions of (revenue - cost) / annual_target, using
each region's own target (the documented default when missing).
"""

PROMPT = """You are analyzing a company data warehouse (SQLite) to compute one business metric.

METRIC - overall gross margin for fiscal year 2023:

    overall_gross_margin = sum over regions of (revenue_region - cost_region) / annual_target_region

where:
  revenue_region       = 2023 revenue of the region (table `sales`)
  cost_region          = 2023 cost of the region (table `cost`)
  annual_target_region = the region's annual target (table `targets`;
                         regions missing from `targets` use the DEFAULT_TARGET
                         documented in the data dictionary)

The data has known quality issues; they are documented in the data
dictionary (read_data_dictionary). Report the final value in your answer,
e.g. "FINAL: the 2023 overall gross margin is 0.55" (plain fraction or
percent both accepted).
"""


class T2PlanTask(TaskBase):
    def __init__(self, difficulty: str = "easy") -> None:
        self.difficulty = difficulty
        self.meta = TaskMeta("T2", "warehouse gross-margin analysis", difficulty, "plan")
        self._tmpdir: Optional[str] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    @property
    def prompt(self) -> str:
        return PROMPT

    @property
    def dictionary(self) -> str:
        return _DICT_HARD if self.difficulty == "hard" else _DICT_EASY

    # -- environment --------------------------------------------------------
    def materialize(self, trace: "Trace") -> Toolbox:
        self.cleanup()
        self._tmpdir = tempfile.mkdtemp(prefix="arena_t2_")
        db_path = os.path.join(self._tmpdir, "warehouse.db")
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._seed()
        return Toolbox(self._specs(), trace)

    def cleanup(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
        if self._tmpdir and os.path.isdir(self._tmpdir):
            shutil.rmtree(self._tmpdir, ignore_errors=True)
        self._tmpdir = None

    def _seed(self) -> None:
        c = self._conn
        c.executescript(
            """
            CREATE TABLE sales (region TEXT, year INTEGER, amount REAL);
            CREATE TABLE cost (region TEXT, year INTEGER, amount REAL);
            CREATE TABLE targets (region TEXT, target REAL);
            """
        )
        # sales: 5 rows, SUM(amount)=800.0, and the 2023 partition contains a
        # real duplicate (north, 2023, 150.0) that changes the answer if not
        # deduplicated.
        c.executemany(
            "INSERT INTO sales VALUES (?,?,?)",
            [
                ("north", 2023, 150.0),
                ("south", 2023, 250.0),
                ("north", 2023, 150.0),  # duplicate row (ETL re-run)
                ("north", 2022, 100.0),
                ("south", 2022, 150.0),
            ],
        )
        cost_rows = [
            ("north", 2023, 100.0),
            ("south", 2023, 80.0),
            ("south", 2022, 999.0),  # legacy sentinel row
        ]
        targets_rows = [
            ("north", 400.0),
            # south intentionally missing -> DEFAULT_TARGET = 400.0
        ]
        if self.difficulty == "hard":
            cost_rows += [
                ("east", 2023, 70.0),   # pilot region, never launched
                ("east", 2022, 999.0),  # second sentinel, older partition
                ("north", 2021, 60.0),
                ("south", 2021, 75.0),
            ]
            targets_rows += [("east", 250.0)]  # decoy target for dead region
            c.executemany(
                "INSERT INTO sales VALUES (?,?,?)",
                [
                    ("north", 2021, 90.0),
                    ("south", 2021, 110.0),
                    ("north", 2022, 100.0),  # another duplicate, older year
                ],
            )
        c.executemany("INSERT INTO cost VALUES (?,?,?)", cost_rows)
        c.executemany("INSERT INTO targets VALUES (?,?)", targets_rows)
        c.commit()

    def true_margin(self) -> float:
        """Ground truth via SQL cross-check; works even if the task was never
        materialized (e.g. the CoT baseline never touches tools)."""
        if self._conn is None:
            from ..llm import Trace

            self.materialize(Trace())
            try:
                return self._query_margin()
            finally:
                self.cleanup()
        return self._query_margin()

    def _query_margin(self) -> float:
        with self._lock:
            row = self._conn.execute(_GROUND_TRUTH_SQL).fetchone()
        return float(row[0])

    # -- tools ----------------------------------------------------------------
    def _specs(self) -> List[ToolSpec]:
        return [
            ToolSpec("list_tables", "List all tables in the warehouse.", {}, [], self._tool_list_tables),
            ToolSpec(
                "describe_table", "Show columns and types of one table.",
                {"table": "string"}, ["table"], self._tool_describe,
            ),
            ToolSpec(
                "read_data_dictionary",
                "Read the warehouse data dictionary (data quality notes, defaults, coverage).",
                {}, [], lambda: {"ok": True, "dictionary": self.dictionary},
            ),
            ToolSpec(
                "run_sql",
                "Execute ONE read-only SQL statement (SELECT or WITH only; single "
                "statement; result sets larger than 200 rows are rejected).",
                {"sql": "string"}, ["sql"], self._tool_run_sql,
            ),
            ToolSpec(
                "fetch_warehouse_export",
                "Bulk-export a year of warehouse data as a file.",
                {"year": "integer"}, ["year"], self._tool_fetch_export,
            ),
        ]

    def _tool_list_tables(self) -> Dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        return {"ok": True, "tables": [r[0] for r in rows]}

    def _tool_describe(self, table: str) -> Dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchall()
            if not rows:
                raise ArgError(f"unknown table: {table!r}")
            cols = self._conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return {
            "ok": True,
            "table": table,
            "columns": [{"name": c[1], "type": c[2]} for c in cols],
            "row_count_note": "use run_sql for contents",
        }

    def _tool_run_sql(self, sql: str) -> Dict:
        from ..tools import ToolError

        text = (sql or "").strip()
        if text.endswith(";"):
            text = text[:-1].strip()
        if not text:
            raise ArgError("empty SQL")
        if ";" in text:
            raise ArgError("only a single SQL statement is allowed (found ';')")
        first = text.split(None, 1)[0].upper()
        if first not in ("SELECT", "WITH"):
            raise ArgError(
                f"only SELECT / WITH statements are allowed (got {first!r})"
            )
        if re.search(r"\b(PRAGMA|ATTACH|DETACH|CREATE|INSERT|UPDATE|DELETE|DROP)\b", text, re.I):
            raise ArgError("read-only queries only")
        try:
            with self._lock:
                cur = self._conn.execute(text)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(201)
        except sqlite3.Error as e:
            raise ToolError(f"SQL error: {e}")
        if len(rows) > 200:
            raise ToolError(
                "result set larger than 200 rows; add filters (gateway policy)"
            )
        return {
            "ok": True,
            "columns": cols,
            "rows": [list(r) for r in rows],
            "row_count": len(rows),
            "summary": f"{len(rows)} row(s)",
        }

    @staticmethod
    def _tool_fetch_export(year: int) -> Dict:
        from ..tools import ToolError

        raise ToolError(
            "HTTP 403 Forbidden: the warehouse export API is denied for this "
            "service account (permanent; security team refused exceptions)"
        )

    # -- evaluation -------------------------------------------------------------
    @staticmethod
    def _extract_value(answer: str) -> Optional[float]:
        """Last-line-first numeric extraction with percent normalization."""
        if not answer:
            return None
        lines = [ln for ln in (answer or "").splitlines() if ln.strip()]
        candidates: List[float] = []

        def _nums(text: str) -> List[float]:
            return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text)]

        for line in reversed(lines):
            for v in _nums(line):
                candidates.append(v)
            if candidates:
                break
        candidates += [v for v in _nums(answer) if v not in candidates]

        for v in candidates:
            if 1.0 < v <= 100.0:
                yield_check = v / 100.0
                if 0.0 < yield_check <= 1.5:
                    return yield_check
            if 0.0 < v <= 1.5:
                return v
            if v > 1.5:  # e.g. raw sum like 220 - keep scanning
                continue
        return None

    def evaluate(self, answer: str, effects: List[Dict]) -> EvalResult:
        calls = [e for e in effects if e.get("kind") == "call"]
        sqls = [e["args"].get("sql", "") for e in calls if e.get("tool") == "run_sql"]
        sql_ok = [e for e in effects if e.get("kind") == "result" and e.get("tool") == "run_sql" and e.get("ok")]

        truth = self.true_margin()
        value = self._extract_value(answer or "")
        correct = value is not None and abs(value - truth) < 5e-3

        checks = [
            self._check("answer_has_value", value is not None, f"extracted={value}"),
            self._check("correct_value", correct, f"|{value} - {truth:.4f}| < 5e-3" if value is not None else "no value"),
            self._check("sql_executed", bool(sql_ok), f"{len(sql_ok)} successful run_sql call(s)"),
            self._check("used_join", any(re.search(r"\bJOIN\b", s, re.I) for s in sqls), ""),
            self._check("deduplicated", any(re.search(r"\bDISTINCT\b", s, re.I) for s in sqls), ""),
        ]
        ok = correct and bool(sql_ok)
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
                "The reported value does not match the warehouse ground truth. "
                "Re-derive it from the tables and re-read the data dictionary's data-quality "
                "notes (row duplicates, sentinel rows, missing regions' default target). "
                "Do not trust un-deduplicated aggregates."
            ),
        )
