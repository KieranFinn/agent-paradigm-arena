# Experiment plan & pre-registered hypotheses

Written **before** any real-model run (offline self-check and scripted-LLM
smoke are the only completed executions at the time of writing). The point
of pre-registration: when the data arrives, overturned hypotheses are
findings, not embarrassments.

## E1 — the main matrix

4 agents × 3 tasks × 2 difficulties × N (default 10) = 240 runs.

```
              T1 explore    T2 plan       T3 verify
react         ✅ home       ·             ·
plan_execute  ·             ✅ home       ·
reflexion     ·             ·             ✅ home
cot           — null baseline, no tools —
```

Final grade is always the `strict` evaluator. The off-diagonal is the
information content; if paradigms tie everywhere, that is the (more
interesting) H5 story.

## E2 — re-planning scope (Plan's core experiment)

```
python3 run_benchmark.py --agents plan_execute --tasks T2 --replan-scope global --tag e2-global
```
Compare against E1's default (`local`): success, replan count, cost, and
whether DONE-steps-are-never-re-validated leaves residue (the §7.1 #4
limitation, on purpose).

## E3 — evaluator quality (Reflexion's core experiment)

```
python3 run_benchmark.py --agents reflexion --tasks T3 --reflexion-evaluator schema   --tag e3-schema
python3 run_benchmark.py --agents reflexion --tasks T3 --reflexion-evaluator llm_judge --tag e3-judge
```
The lopsided judge is instructed to be encouraging ("team is short on time
and budget..."). Watch the inner loop terminate on a PASS while the strict
outer grade fails: the reflection-hallucination failure, reproduced.

## E4 — model strength × scaffolding strength

Same matrix (or a reduced N) with a stronger model
(`--model <strong>` / `config.models.strong`). If scaffolding matters less
for stronger models, paradigms are cost levers, not capability levers.

## E5 — failure-mode distribution

Derived from E1 traces; `report.md` §4. Labels include:
`tests_never_run, tests_not_passing, root_cause_not_fixed, tests_modified,
answer_missing_root_cause, wrong_value, no_value_in_answer, no_sql_executed,
no_code_submitted, steps_exhausted, no_final_answer, repeated_action_loop,
budget_exceeded, harness_error`.

## Hypotheses (pre-registered)

| # | hypothesis | if confirmed | if overturned |
|---|---|---|---|
| H1 | ReAct ≥ Plan on T1 | exploration needs no upfront plan | planning pays even for exploration |
| H2 | Plan ≫ ReAct on T2 and peak prompt ≪ (an order of magnitude) | "clean context" is a quantifiable win | ReAct's inflation is milder than folklore |
| H3 | Reflexion tops T3 success but worst ¥/success | compounding trials + cost explosion coexist | reflection doesn't transfer here |
| H4 | local re-plan ≈ or > global on T2 at clearly lower cost | production guidance for Plan | global is safer; local leaves residue |
| H5 | with a strong model all paradigms converge on success | paradigms are **cost ballast**, not capability amplifiers — the article's headline candidate | paradigm differences are capability-level |
| H6 | under the lopsided judge Reflexion's success **drops** | evaluator quality is Reflexion's lifeline | reflection is robust to weak signal (more optimistic) |

## Cost model

Per run ¥0.01–0.1 at DeepSeek-tier pricing → full E1 ≈ ¥5–20. Per-run fuse
at ¥500 (should never trigger); `--jobs` parallelism for wall-clock.

## Threats to validity (acknowledged up front)

- temperature=0 + deterministic mocks ⇒ repeat variance comes only from
  API-side nondeterminism; cells may legitimately show 0/10 or 10/10;
- prompt wording is part of the scaffold (all four frameworks get
  task-neutral, comparably-instructed prompts — wording remains a
  confound no prompt-based comparison fully removes);
- one task per structural cell → treat per-task conclusions as case studies
  of the structural type, not as population claims.
