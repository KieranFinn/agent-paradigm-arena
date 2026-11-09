# Agent Paradigm Arena

> One repo, one question: **ReAct / Plan-and-Execute / Reflexion — on the same
> tasks, where exactly do they differ, by how much, and at what cost?**
> Not "which is better" — but a reproducible trace, an auditable cost ledger,
> and replayable failures for every claim.

Companion experiment repo for the blog post
《当模型足够强，范式还剩下什么？——ReAct / Plan-and-Execute / Reflexion 的 123 次真实运行对照实验》
(**[docs/blog.md](docs/blog.md)**, Chinese).
Every number in the article traces back to a concrete event sequence under
`runs/<batch>/traces/`; the merged evidence tables live in
[`runs/_merged/`](runs/_merged/report.md).

## What's inside

```
agent_arena/
├── llm.py            transport (SDK/urllib), Trace, simulated clock, pricing, audit cache
├── tools.py          tool registry, argument validation, error layering, effect recording
├── config.py         zero-dependency two-level YAML-subset loader
├── evaluators.py     strict / schema / llm_judge (the lopsided judge)
├── tasks/            T1 explore (repo debugging) · T2 plan (warehouse SQL) · T3 verify (hidden tests)
└── frameworks/       react · plan_execute · reflexion · cot   ← the ONLY variable
run_benchmark.py      matrix runner, statistics (Wilson CI, median±MAD), reports
smoke_test.py         offline end-to-end smoke (scripted fake LLM)
inspect_trace.py      render a run's trace as a timeline
```

**Design rule: the three paradigms share everything except control flow.**
Tools, tasks, evaluators, traces, pricing and cache all live outside
`frameworks/`, so changing a paradigm cannot silently change the exam room.

## Quick start

```bash
# 1. offline self-check - no key, no network, no cost
python3 run_benchmark.py --check

# 2. offline end-to-end smoke (scripted LLM drives all four frameworks)
python3 smoke_test.py

# 3. see the execution plan without spending anything
python3 run_benchmark.py --dry-run

# 4. real runs (needs a key)
cp .env.example .env          # fill DEEPSEEK_API_KEY (or any OpenAI-compatible provider)
python3 run_benchmark.py --agents react --tasks T1 --difficulty easy --repeats 1 --tag probe
python3 inspect_trace.py runs/<probe_batch>/traces --limit 1

# 5. full matrix + the specialty experiments
python3 run_benchmark.py --tag main --jobs 4
python3 run_benchmark.py --agents plan_execute --tasks T2 --replan-scope global --tag e2-global
python3 run_benchmark.py --agents reflexion --tasks T3 --reflexion-evaluator llm_judge --tag e3-judge
python3 run_benchmark.py --model <stronger-model> --tag e4-strong
```

## The matrix

4 agents × 3 tasks × 2 difficulties × N repeats (default 10 = 240 runs).
Tasks are chosen by **structural properties**, not by who is "good at them":

| task | type | information need | hard verifier | home turf |
|---|---|---|---|---|
| T1 legacy repo debugging | explore | discover-at-runtime | test suite | ReAct |
| T2 warehouse gross margin | plan | fully known upfront | SQL cross-check | Plan-and-Execute |
| T3 hidden-test implementation | verify | fully known upfront | hidden unit tests | Reflexion |

The diagonal is home turf; the off-diagonal cells are the new information.

## Outputs

```
runs/<batch>/
├── report.md      §1 per-agent · §2 4×3 matrix (✅ home turf) · §3 overall · §4 failure modes · §5 ¥/success
├── report.csv     flat per-run data (pareto-scatter ready)
├── raw.jsonl      one line per run (real wall-clock kept OUT of the main tables)
└── traces/        full event sequences - the blog's evidence
```

## Measurement rules (short version)

- success rates carry **Wilson 95% intervals**; latency/cost report **median ± MAD**;
- latency is a **simulated serving-time clock** (network jitter must not pollute
  the paradigm comparison); wall-clock is kept in `raw.jsonl` only;
- token costs are tiered (cached input / fresh input / output);
- "quadratic context growth" is split into **peak prompt tokens** (paradigm-
  attributable) vs **cumulative tokens** (API statelessness, every paradigm pays);
- evaluators only trust **recorded effects**, never the model's self-report;
- final grades always use the `strict` evaluator; `schema`/`llm_judge` exist to
  demo weak-verifier failures inside Reflexion's loop (E3).

See `docs/design.md` for the full contract and known deviations, and
`docs/experiments.md` for the pre-registered hypotheses (H1–H6).

## Requirements

Python ≥ 3.9, standard library only. The `openai` SDK is optional
(`pip install openai` if you want SDK transport; urllib fallback is automatic).
