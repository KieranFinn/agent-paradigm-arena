# Merged analysis (all batches, honest N per cell)

- batches: main_20261109-135454, m2-plan_20261109-140959, m2-plan_20261109-141722, m2-refl_20261109-142417, m2-cot_20261109-143143
- runs: 123  cost: 7.0106 CNY  successes: 111/123

## Matrix (full data)

### easy

| agent | T1 explore | T2 plan | T3 verify |
|---|---|---|---|
| react | 10/10 [0.72,1.00] ✅ (n=10) | 10/10 [0.72,1.00] (n=10) | 10/10 [0.72,1.00] (n=10) |
| plan_execute | 10/10 [0.72,1.00] (n=10) | 3/3 [0.44,1.00] ✅ (n=3) | 3/3 [0.44,1.00] (n=3) |
| reflexion | 3/3 [0.44,1.00] (n=3) | 3/3 [0.44,1.00] (n=3) | 3/3 [0.44,1.00] ✅ (n=3) |
| cot | 0/3 [0.00,0.56] (n=3) | 0/3 [0.00,0.56] (n=3) | 3/3 [0.44,1.00] (n=3) |

### hard

| agent | T1 explore | T2 plan | T3 verify |
|---|---|---|---|
| react | 10/10 [0.72,1.00] ✅ (n=10) | 10/10 [0.72,1.00] (n=10) | 9/9 [0.70,1.00] (n=9) |
| plan_execute | 6/6 [0.61,1.00] (n=6) | 3/3 [0.44,1.00] ✅ (n=3) | 3/3 [0.44,1.00] (n=3) |
| reflexion | 3/3 [0.44,1.00] (n=3) | 3/3 [0.44,1.00] (n=3) | 3/3 [0.44,1.00] ✅ (n=3) |
| cot | 0/3 [0.00,0.56] (n=3) | 0/3 [0.00,0.56] (n=3) | 3/3 [0.44,1.00] (n=3) |

## Matrix (symmetric subsample, first 3 per cell)

### easy

| agent | T1 explore | T2 plan | T3 verify |
|---|---|---|---|
| react | 3/3 [0.44,1.00] ✅ | 3/3 [0.44,1.00] | 3/3 [0.44,1.00] |
| plan_execute | 3/3 [0.44,1.00] | 3/3 [0.44,1.00] ✅ | 3/3 [0.44,1.00] |
| reflexion | 3/3 [0.44,1.00] | 3/3 [0.44,1.00] | 3/3 [0.44,1.00] ✅ |
| cot | 0/3 [0.00,0.56] | 0/3 [0.00,0.56] | 3/3 [0.44,1.00] |

### hard

| agent | T1 explore | T2 plan | T3 verify |
|---|---|---|---|
| react | 3/3 [0.44,1.00] ✅ | 3/3 [0.44,1.00] | 3/3 [0.44,1.00] |
| plan_execute | 3/3 [0.44,1.00] | 3/3 [0.44,1.00] ✅ | 3/3 [0.44,1.00] |
| reflexion | 3/3 [0.44,1.00] | 3/3 [0.44,1.00] | 3/3 [0.44,1.00] ✅ |
| cot | 0/3 [0.00,0.56] | 0/3 [0.00,0.56] | 3/3 [0.44,1.00] |

## Structural metrics per agent (medians over all runs)

| agent | n | cost CNY | peak prompt | cum prompt | cum/peak | completion | llm calls | first action s | sim latency s | real wall s |
|---|---|---|---|---|---|---|---|---|---|---|
| react | 59 | 0.0311 | 3937 | 16783 | 4.3x | 2500 | 7 | 1.0 | 34.8 | 18.6 |
| plan_execute | 28 | 0.0972 | 4836 | 62740 | 13.0x | 8372 | 26 | 11.0 | 112.7 | 58.4 |
| reflexion | 18 | 0.0312 | 4458 | 17006 | 3.8x | 2560 | 7 | 1.0 | 35.6 | 21.1 |
| cot | 18 | 0.0646 | 269 | 269 | 1.0x | 8012 | 1 | nan | 96.6 | 59.7 |

## Per-cell detail (median cost CNY / sim latency s / peak prompt)

| agent | task | diff | n | ok | cost | lat | peak | cum | llm | replans | epochs | refl |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| react | T1 | easy | 10 | 10 | 0.0409 | 43.5 | 6176 | 42841 | 11 | 0.0 | 1.0 | 0.0 |
| react | T1 | hard | 10 | 10 | 0.0419 | 43.3 | 6648 | 45954 | 11 | 0.0 | 1.0 | 0.0 |
| react | T2 | easy | 10 | 10 | 0.0221 | 23.8 | 3110 | 14372 | 7 | 0.0 | 1.0 | 0.0 |
| react | T2 | hard | 10 | 10 | 0.0301 | 31.8 | 3810 | 17938 | 7 | 0.0 | 1.0 | 0.0 |
| react | T3 | easy | 10 | 10 | 0.0111 | 15.6 | 1974 | 2720 | 2 | 0.0 | 1.0 | 0.0 |
| react | T3 | hard | 9 | 9 | 0.0482 | 70.9 | 3942 | 5507 | 3 | 0.0 | 1.0 | 0.0 |
| plan_execute | T1 | easy | 10 | 10 | 0.1380 | 152.7 | 5457 | 101644 | 36 | 1.0 | 1.0 | 0.0 |
| plan_execute | T1 | hard | 6 | 6 | 0.1208 | 131.9 | 5081 | 81898 | 30 | 0.0 | 1.0 | 0.0 |
| plan_execute | T2 | easy | 3 | 3 | 0.0617 | 69.9 | 2480 | 28396 | 18 | 0.0 | 1.0 | 0.0 |
| plan_execute | T2 | hard | 3 | 3 | 0.0705 | 82.8 | 2728 | 36152 | 20 | 0.0 | 1.0 | 0.0 |
| plan_execute | T3 | easy | 3 | 3 | 0.0451 | 58.3 | 2031 | 14038 | 11 | 0.0 | 1.0 | 0.0 |
| plan_execute | T3 | hard | 3 | 3 | 0.0748 | 103.2 | 3312 | 13699 | 9 | 0.0 | 1.0 | 0.0 |
| reflexion | T1 | easy | 3 | 3 | 0.0507 | 45.8 | 6882 | 58367 | 13 | 0.0 | 1.0 | 0.0 |
| reflexion | T1 | hard | 3 | 3 | 0.0516 | 56.1 | 7533 | 54795 | 12 | 0.0 | 1.0 | 0.0 |
| reflexion | T2 | easy | 3 | 3 | 0.0218 | 24.7 | 3101 | 11546 | 6 | 0.0 | 1.0 | 0.0 |
| reflexion | T2 | hard | 3 | 3 | 0.0287 | 30.6 | 3803 | 17825 | 7 | 0.0 | 1.0 | 0.0 |
| reflexion | T3 | easy | 3 | 3 | 0.0095 | 13.2 | 1762 | 2507 | 2 | 0.0 | 1.0 | 0.0 |
| reflexion | T3 | hard | 3 | 3 | 0.0344 | 49.6 | 4864 | 5688 | 2 | 0.0 | 1.0 | 0.0 |
| cot | T1 | easy | 3 | 0 | 0.0798 | 119.4 | 236 | 236 | 1 | 0.0 | 1.0 | 0.0 |
| cot | T1 | hard | 3 | 0 | 0.0721 | 107.9 | 236 | 236 | 1 | 0.0 | 1.0 | 0.0 |
| cot | T2 | easy | 3 | 0 | 0.0585 | 87.8 | 269 | 269 | 1 | 0.0 | 1.0 | 0.0 |
| cot | T2 | hard | 3 | 0 | 0.0195 | 29.3 | 269 | 269 | 1 | 0.0 | 1.0 | 0.0 |
| cot | T3 | easy | 3 | 3 | 0.0179 | 26.6 | 328 | 328 | 1 | 0.0 | 1.0 | 0.0 |
| cot | T3 | hard | 3 | 3 | 0.1014 | 152.1 | 407 | 407 | 1 | 0.0 | 1.0 | 0.0 |

## Failure modes

- `cot/T1/easy`: tests_never_run×3
- `cot/T1/hard`: tests_never_run×3
- `cot/T2/easy`: no_value_in_answer×2, no_sql_executed×1
- `cot/T2/hard`: no_value_in_answer×3

## Hypothesis checks (against available data)

- H1 (easy): ReAct 10/10 vs Plan 10/10 on T1
- H2 (easy): T2 peak prompt ReAct 3110 vs Plan 2480 (ratio 1.3x)
- H3 (easy): T3 Reflexion 3/3 vs ReAct 10/10; median cost 0.0095 vs 0.0111 CNY
- H1 (hard): ReAct 10/10 vs Plan 6/6 on T1
- H2 (hard): T2 peak prompt ReAct 3810 vs Plan 2728 (ratio 1.4x)
- H3 (hard): T3 Reflexion 3/3 vs ReAct 9/9; median cost 0.0344 vs 0.0482 CNY
- H5 (aggregate): react 59/59; plan_execute 28/28; reflexion 18/18; cot 6/18
