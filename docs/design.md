# Design notes & deliberate deviations

This file records the implementation contract of `agent_arena`, and every
place where this implementation **deviates** from (or hardens)
`project-overview.md`, with the reason. Read this before trusting any table.

## 1. The one rule

Three paradigms share everything except control flow. Tools, tasks,
evaluators, traces, pricing, cache all live outside `frameworks/`.

## 2. Data contracts

**TraceEvent** (one JSONL line per event):
`step, kind, t_ms, latency_ms, model, prompt_tokens, completion_tokens,
cached_tokens, cost_cny, payload` with
`kind ∈ {llm_call, tool_call, tool_result, plan, eval, reflection, guard, final}`.

Trace aggregations map 1:1 to blog table columns:

| metric | trace method |
|---|---|
| first observable side effect | `to_first_action_ms()` (first `tool_call` or `plan`) |
| cumulative tokens | `total_tokens()` → prompt/completion/cached |
| peak prompt (ReAct inflation evidence) | `peak_prompt_tokens()` |
| cost | `total_cost_cny()` (tiered pricing) |
| simulated elapsed | `total_latency_ms()` |

**Effects**: `Toolbox.run` records `{"kind": "call", "tool", "args", "step"}`
and `{"kind": "result", "tool", "ok", "summary"}` into a run-scoped sink
(contextvar in the main thread; plan-executor worker threads attach the same
sink explicitly). Evaluators trust effects only, never self-reports.

**Error layering**: `ArgError` → `{"ok": false, "kind": "arg"}` (model can
self-heal); `ToolError` → `{"ok": false, "kind": "tool"}` (environment limit;
retrying is useless). The blog's "self-healing" analysis keys off reactions
to `kind=arg`.

## 3. Simulated clock & pricing

- audit-cache hit → 40 ms; otherwise `450 + fresh_prompt × 0.02 + completion × 12.0` ms
  (fresh = prompt − cached; cached tokens are served cheap, not free);
- tool call ticks 5 ms, tool result 30 ms;
- prices (CNY / 1M tokens): cached input 0.2, fresh input 2.0, output 8.0 —
  DeepSeek-tier defaults, configurable per provider in `config.yaml`;
- real wall-clock is measured per run and stored in `raw.jsonl`, but **never**
  enters the report's main tables.

Caveat that goes into the blog: with a high real-world KV-cache hit rate,
ReAct's *actual* bill is lower than this model suggests — the simulation
prices the API as it reports cache hits, and DeepSeek reports them
conservatively. Relative comparisons remain valid; absolute CNY must be read
as "this serving model", not "your invoice".

## 4. Deviations from project-overview.md (and why)

1. **Audit cache is opt-in (`--use-cache`), off by default.**
   The overview says "同一请求走审计缓存只真调一次". Implemented literally with
   temperature=0 and deterministic mock tools, repeats 2..N of a cell would
   be byte-identical cache hits and the Wilson intervals would degenerate.
   The cache now serves development iterations / resuming; within-batch
   repeats always hit the real API (that IS the measured variance).
2. **T2's duplicate sales row lives in fiscal 2023.**
   The overview's probe note ("`WHERE year=2023` returns two rows") described
   a duplicate that cannot change the answer — the `deduplicated` check would
   be checking a non-issue. The duplicate is now `('north', 2023, 150.0)`
   (full-table probe stays `(5, 800.0)` as documented); forgetting DISTINCT
   yields 0.7 instead of 0.55. The trap is real.
3. **T1/T3 hidden-test execution moved into isolated subprocesses**
   (`_t1_runner.py`, `_t3_runner.py`, `python -I -B`) instead of in-process
   `exec`. Two reasons: (a) an LLM-written infinite loop must not hang the
   benchmark; (b) CPython validates `.pyc` by (mtime-in-seconds, size) — a
   patch that preserves length and lands within the same second would load a
   **stale `__pycache__`** and report pre-patch test results (this bug was
   actually observed during bring-up and fixed with `-B`). The doc's
   `types.ModuleType + sys.modules` injection lives on *inside* the runner.
4. **Local re-plan keeps PENDING siblings.** The overview's `kept` filter
   (`status == "DONE"`) silently dropped never-started sibling steps; we keep
   every step that is not (failed ∪ its downstream), which is the meaningful
   reading of "local". The §7.1 #4 limitation stands on purpose: DONE steps
   are never re-validated after a local re-plan — that residual-inconsistency
   risk is exactly what E2 measures.
5. **Plan-executor tool calls carry the real step id** (overview §7.1 #3
   said "记账，不改"; a fresh implementation has no reason to keep the wart).
6. **`--jobs N` run-level parallelism** was added (the matrix is 240+ real
   API runs; sequential would take hours). Each run gets its own task
   instance / trace / tmpdir; the audit cache is thread-safe; T2's sqlite
   connection is created with `check_same_thread=False` and a lock.
7. **Both Reflexion fixes from §7.1 are applied by construction**:
   the inner evaluator receives the real effects (#1), and
   `_condense_trajectory` honours its epoch start index (#2).

## 5. Known limitations (goes into the blog as-is)

- Mock tools are idealized: no timeouts, rate limits, or format drift. Read
  the results as **relative** differences; do not extrapolate absolutes.
- One task family per structural cell, easy/hard only, N=10 per cell:
  trend-level evidence, not significance tests.
- Reflexion's memory pool keeps the last 3 lessons; long-horizon experience
  accumulation is out of scope.
- T2 easy and hard share the ground truth 0.55 (the overview's §7.1 #7
  suggestion of a different-valued hard variant was consciously skipped;
  hard adds noise rows/regions/decoys instead. Framed as a limitation in the
  blog, not hidden).
- The Reflexion inner loop is a full ReAct (the task's tools stay available).
  So "Reflexion vs ReAct on T3" measures *cross-attempt verbal memory on top
  of in-trajectory tool feedback*, not Reflexion in its paper-pure form.
  That is the honest modern question; the blog says so explicitly.

## 6. Guardrails

- repeated-action guard: same (tool, canonical args) in 3 consecutive turns →
  `guard` event + `{"ok": false, "guard": "repeated_action"}` fed back;
- per-step turn budget (`plan.max_step_turns=8`), per-run step budget
  (`defaults.max_steps=40`), per-run LLM-call budget (`max_llm_calls=60`),
  per-run cost fuse (`budget.max_cny_per_run=500`);
- path-traversal protection on T1 file tools; SQL is single-statement,
  SELECT/WITH-only, result-set-capped at 200 rows.
