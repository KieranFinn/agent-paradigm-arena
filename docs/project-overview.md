# 项目总文档：Agent Paradigm Arena

> 一个仓库，回答一个问题：**ReAct / Plan-and-Execute / Reflexion 三种 Agent 范式，在同一个任务上到底差在哪、差多少、代价是什么。**
> 这份文档记录了三件事：① 要做什么（需求）；② 任务与代码是怎么设计的（设计）；③ 实际做了哪些动作、跑出了什么（执行记录与已知问题）。

---

## 0. 一句话定位

**指标不是论文里的"效果更好"，而是可复现的 trace + 可对账的成本 + 能复现的失败。**

它是技术博客《ReAct / Plan-and-Execute / Reflexion 优缺点》的配套实验仓库，文章里的每个数字都能回溯到 `runs/<批次>/traces/` 下某一条具体的事件序列。

---

## 1. 需求设计

### 1.1 用户目标（原始诉求的演进）

| 阶段 | 用户诉求 | 落地产物 |
|---|---|---|
| 1 | 写一篇三种范式优劣的深度文章，给目录 | 六章目录（背景 → 三范式 → 对比矩阵 → 混合架构） |
| 2 | 不只讲原理，要用**真实代码原型**演示差异 | 三个最小原型 + 统一 mock 工具 + benchmark |
| 3 | 想"3 个任务 × 3 个框架 = 9 次实验" | 被否决，改为 **4×3 矩阵（含 CoT baseline）+ 2 难度档** |
| 4 | 用 DeepSeek API，代码放 `AI Agent` 目录 | 当前仓库 |

### 1.2 被否决的方案与理由（方法论要求）

这几条是"需求"的一部分，因为它们决定了后面所有设计的取舍：

| 原始想法 | 问题 | 采纳的方案 |
|---|---|---|
| 一个"查公司财报算增长"的 mock 任务跑三框架 | Plan 阶段信息需求已完全已知，**只能测出代码风格差异**，测不出范式能力差异 | 拆成三个**结构属性不同**的任务，见 §2 |
| 按"各范式擅长的任务"设计任务 | 同义反复，结论必然"各有主场" | 任务按**客观结构属性**分型（信息需求何时可知 / 有无硬判定器） |
| 3×3 里只跑对角线 | 缺 null baseline，无法回答"脚手架到底加了多少分" | **加 CoT 一行**，形成 4×3，正方形外还有对照组 |
| 用 LOC 行数比代码量 | 与范式优劣无关，只是工程风格 | 删除，改为**结构性指标** |
| "token 随轮数二次方增长" | 是 API 无状态（每轮重发全量）的特性，**不是 ReAct 专利** | 拆成「单步 context 长度」「累计 token」「峰值 prompt token」 |
| "TTFT 快慢" | 与真实用户感知脱节 | 改为「首个可观测副作用时刻」+ 模拟端到端耗时（P50/MAD） |
| 成功率报一个数 | 单次 run 的波动会被当成结论 | 每格 **N≥10**，报 **Wilson 95% 区间** |

### 1.3 功能需求

| 编号 | 需求 | 状态 |
|---|---|---|
| R1 | 三个范式的最小可实现原型（ReAct / Plan-and-Execute / Reflexion） | ✅ |
| R2 | 一个 CoT 单次调用作为 null baseline | ✅ |
| R3 | 三个结构属性互不相同的任务，每个任务 easy/hard 两档 | ✅ |
| R4 | 统一工具层、统一评估器、统一 trace 格式 | ✅ |
| R5 | 一次命令跑完整矩阵，产出 markdown 报表 + CSV | ✅ |
| R6 | 离线自检（不发请求、不花钱、可进 CI） | ✅ |
| R7 | 端到端冒烟（脚本化假 LLM，验证全部链路） | ✅ |
| R8 | 局部 vs 全量重规划开关（Plan 的核心实验） | ✅ |
| R9 | Evaluator 三档开关，含"偏心裁判"（Reflexion 的核心实验） | ✅ |
| R10 | 审计缓存，同一配置重跑可复现 | ✅ |
| R11 | 预算熔断（单次跑批上限） | ✅ |
| R12 | **用真实 DeepSeek API 跑通全矩阵** | ⏳ 待用户填密钥 |
| R13 | 基于实测数据写博客正文 | ⏳ 依赖 R12 |

### 1.4 非功能约束

- **零重型依赖**：不引入 LangChain / LlamaIndex，避免"黑盒框架差异"污染变量；`openai` SDK 是可选依赖，没装时自动退回标准库 `urllib`。
- **零配置依赖**：`config.py` 自带一个 YAML 子集解析器（只支持两级缩进映射），不依赖 PyYAML。
- **可复现**：`temperature=0`；时延用"按 token 数推算的模拟时钟"而非 wall-clock（消除网络抖动）；同一请求走审计缓存只真调一次。
- **可追溯**：每条 run 产出完整事件序列，失败 run 也保留。

---

## 2. 任务设计

### 2.1 分型原则

三个任务**不是**按"谁擅长"选的，而是按两个正交的结构属性选出来的：

```
             信息需求何时可知？
        执行时才可知          一开始完全已知
        ┌──────────────┬──────────────┐
 有 硬  │              │              │
 判 定  │    T1 探索型  │    T3 校验型  │
 器 ？  │  （排障）      │  （隐藏单测）  │
        ├──────────────┼──────────────┤
 无 硬  │              │              │
 判 定  │   （可探索+   │    T2 规划型  │
 器    │    无判定，    │  （数仓分析）  │
        │    难以评分）   │              │
        └──────────────┴──────────────┘
```

- **T1 探索型**：不知道要读哪个文件才会找到根因 → 适合逐轮试探（ReAct 主场）。
- **T2 规划型**：任务一开始就能拆成确定的 SQL 步骤，且互相依赖、可并行 → 适合先规划再执行（Plan 主场）。
- **T3 校验型**：有隐藏单测这一硬判定器，一次写对概率低，但错误可复利 → 适合反复试错+反思（Reflexion 主场）。

三者恰好构成一个**非对角线验证**：如果某范式在客场显著输给东道主，说明范式的影响是真实的；如果各范式全任务打平，结论就变成"范式只是成本层面的差异"——这本身是更有价值的结论（见 §6 H5）。

### 2.2 T1：遗留代码库排障（探索型）

| 项 | 内容 |
|---|---|
| 场景 | 一个计费服务单测挂了，报错文件是相对名 `billing.py`，需要在多文件仓库里定位真正根因并修复 |
| 工具（8 个） | `list_files`、`repo_map`、`read_file`、`write_file`、`apply_patch`、`grep`、`git_log`、`run_tests` |
| 根因 | `hashes.py` 里 `expected = "a3f1c9e2b7d4"` —— 截断的 hash 前缀；正确值 = `sha256("billing-v2:in_7").hexdigest()[:12]` |
| 红鲱鱼（easy） | `api.log` 里的 `/v1/rates RETRY ×2`；`http_client.py` 的 `DEFAULT_TIMEOUT` 留空 |
| 红鲱鱼（hard） | 追加一行 `metrics_collector` WARN；`storage.py` 多一个看起来可疑的 `_normalize()` |
| 诱饵 | `tests/test_legacy.py` 里有 `@skip` 的测试 |
| 判定（5 项检查） | `tests_run` / `tests_pass` / `fixed_root_cause`（改的是 hashes.py 且 billing.py 未被破坏）/ `tests_untouched`（不许改测试）/ `answer_states_root_cause` |
| 通过条件 | `tests_pass ∧ fixed_root_cause ∧ tests_untouched` |

**为什么这样设计**：信息需求执行时才可知，且"读错文件"不会立刻报错——这正是 ReAct 容易**目标漂移**的地方；同时 `apply_patch` 在 old 片段不存在时返回 `ArgError`，可以观察模型能否**读错误并自愈**；两次读到同一文件是合法的，读同一行重复工具调用则由护栏负责暴露。

### 2.3 T2：数仓多表分析（规划型）

| 项 | 内容 |
|---|---|
| 场景 | 从 `sales` / `cost` / `targets` 三表计算某年的整体毛利率 |
| 工具（5 个） | `list_tables`、`describe_table`、`read_data_dictionary`、`run_sql`（只允许 SELECT/WITH、单语句、>200 行报错）、`fetch_warehouse_export`（**永远 403**） |
| 埋的坑 1 | `sales` 有重复行 → 必须 `DISTINCT` |
| 埋的坑 2 | `cost` 里有 `south 2022 = 999.0` 的 legacy 行 → 必须排除 |
| 埋的坑 3 | `year < 2018` 不在可查范围 |
| 埋的坑 4 | `targets` 缺失某 region → 必须用数据字典里的 `DEFAULT_TARGET=400` |
| 埋的坑 5 | `fetch_warehouse_export` 永远 403 → 逼迫放弃"一次性导出"的捷径，转而用 SQL 逐步算 |
| 真值 | **0.55**（north 150−100 + south 250−80，即 220/400） |
| 真值来源 | `true_margin()` 用 ground-truth SQL 交叉校验，不写死常量 |
| 判定（5 项） | `answer_has_value` / `correct_value`（|Δ|<5e-3）/ `sql_executed` / `used_join` / `deduplicated` |
| 通过条件 | `correct_value ∧ sql_executed` |

**为什么要故意让 `run_sql` 报错**：Plan 的经典失效是"计划前提在 Step 2 被证伪（数据不存在/权限不足），但 Step 3 已经按原计划排好"，能直接观察到级联失效与重规划代价。

> 实测确认（本次 probe）：`SELECT COUNT(*) AS c, SUM(amount) AS s FROM sales` 返回 `(5, 800.0)`，`SELECT * FROM sales WHERE year=2023` 只返回 north 150 / south 250 两行——**重复行和 legacy 成本行只在数据字典文字里说明，工具层不会主动提示**。这是有意的：考察模型是否读文档、而不是靠试探发现。

### 2.4 T3：隐藏单测实现（校验型）

| 项 | 内容 |
|---|---|
| 场景 | 按规格文档实现函数，提交后跑隐藏单测 |
| 工具 | 只能写 `solution.py`，无其他工具 |
| easy | `parse_duration`：`500ms→0`、`1200ms→1`、大小写敏感、`"1h 30m"` 非法 |
| hard | `slugify`：CJK 保留、空串→`"-"`、`"C++ & Rust"→"c-rust"`；另含 `safe_int`：bool 保留、`"3.9"`→default、`"1_000"`→1000 |
| 诱饵 | 规格里有一处矛盾描述，需要模型自行取舍 |
| 判定 | `_run_hidden_tests` 用 `types.ModuleType` + `sys.modules` 临时注入一个最小 pytest 替身，执行 `test_` 函数；`_all_green()` 要求输出匹配 `\b\d+ passed\b` 且无 `failed`/`error` |

**为什么这样设计**：T3 的判定器是**硬的、可复利的**——每条失败信息都指向具体边界用例，这正是 Reflexion "反思→下次避免"能发挥作用的前提；反过来，一旦换成"偏心裁判"（§5.3），就能复现 Reflexion 的经典失效。

---

## 3. 代码文件设计

### 3.1 分层

```
            ┌─────────────────────────────────────────────┐
            │  run_benchmark.py   矩阵跑批 / 统计 / 报表      │
            │  smoke_test.py      离线端到端冒烟（假 LLM）    │
            │  inspect_trace.py   把 trace 渲染成时间线      │
            └───────────────────┬─────────────────────────┘
                                │
            ┌───────────────────▼─────────────────────────┐
            │  agent_arena/frameworks/   ← 唯一的变量在这里    │
            │   react  plan_execute  reflexion  cot       │
            └──────┬─────────────────────────┬────────────┘
                   │                         │
   ┌───────────────▼──────────┐   ┌──────────▼──────────────┐
   │ agent_arena/tasks/       │   │ agent_arena/llm.py      │
   │ T1 / T2 / T3 + base      │   │ LLMClient + Trace + 定价 │
   └───────────────┬──────────┘   └──────────┬──────────────┘
                   │                         │
        ┌──────────▼──────────┐   ┌──────────▼──────────────┐
        │ agent_arena/tools.py│   │ agent_arena/evaluators  │
        │ 工具抽象 + 错误分层   │   │ strict/schema/llm_judge │
        └─────────────────────┘   └─────────────────────────┘
```

**设计原则：三个范式共享除控制流以外的一切。** 工具、任务、评估器、trace、定价、缓存全部在 `frameworks/` 之外，改范式不可能无意中改到"考场"。

### 3.2 文件职责与规模

| 文件 | 行数 | 职责 | 关键接口 |
|---|---:|---|---|
| `agent_arena/llm.py` | 388 | 传输层（SDK/urllib）、Trace、模拟时钟、定价、审计缓存 | `LLMClient.chat(model, messages, tools, step, label)`、`text()`、`json_call()`、`parse_json_loose()` |
| `agent_arena/tools.py` | 104 | 工具注册、参数校验、错误分层、副作用记录 | `ToolSpec(name, description, params, required, fn)`、`Toolbox.schemas()/run(name, args, step)`、`record_effect(kind, **data)` |
| `agent_arena/config.py` | 61 | 零依赖 YAML 子集解析 | `load(path)` |
| `agent_arena/evaluators.py` | 54 | 三档评估器分派 | `EVALUATOR_MODES=("strict","schema","llm_judge")`、`run_evaluator(...)`、`lopsided_judge` |
| `agent_arena/tasks/base.py` | 51 | 任务基类与数据契约 | `TaskMeta`、`EvalResult`、`RunResult`、`TaskBase.evaluate/evaluate_schema/evaluate_llm_judge` |
| `agent_arena/tasks/t1_explore.py` | 317 | T1 虚拟仓库 + 8 工具 + 判定 | `T1ExploreTask.materialize/apply_patch/run_tests` |
| `agent_arena/tasks/t2_plan.py` | 211 | T2 sqlite 数仓 + 5 工具 + 判定 | `T2PlanTask.true_margin()` |
| `agent_arena/tasks/t3_verify.py` | 293 | T3 规格 + 隐藏测试替身 + 判定 | `T3VerifyTask._run_hidden_tests` |
| `agent_arena/frameworks/react.py` | 141 | ReAct 单循环 + 护栏 | `run_react(task, llm, trace, cfg, extra_context)` |
| `agent_arena/frameworks/plan_execute.py` | 287 | Planner / Executor / Re-planner | `run_plan_execute(task, llm, trace, cfg)` |
| `agent_arena/frameworks/reflexion.py` | 136 | 外环 epoch + 记忆池 | `run_reflexion(task, llm, trace, cfg)` |
| `agent_arena/frameworks/cot.py` | 23 | 单次调用 baseline | `run_cot(task, llm, trace, model)` |
| `run_benchmark.py` | 609 | 自检、跑批、统计、报表 | `run_one(...)`、`wilson_ci/median/mad/summarize`、`write_reports(...)` |
| `smoke_test.py` | 188 | 离线冒烟（脚本化 LLM） | `ScriptedTransport` |
| `inspect_trace.py` | 82 | trace → 时间线 | CLI |

### 3.3 核心数据契约

**TraceEvent**（每个事件都写一行 JSONL）：

```
TraceEvent(step, kind, t_ms, latency_ms, model,
           prompt_tokens, completion_tokens, cached_tokens,
           cost_cny, payload)
kind ∈ {llm_call, tool_call, tool_result, plan, eval, reflection, guard, final}
```

`Trace` 上的聚合方法就是文章表格里的每一列：

| 指标 | 来源 |
|---|---|
| 首个可观测副作用时刻 | `to_first_action_ms()`（首个 `tool_call` 或 `plan` 事件） |
| 累计 token | `total_tokens()` → `{prompt, completion, cached}` |
| 峰值 prompt | `peak_prompt_tokens()`（ReAct 上下文膨胀的直接证据） |
| 成本 | `total_cost_cny()`（按 `Price` 分 cached/fresh/output 三档计价） |
| 模拟耗时 | `total_latency_ms()` |

**EvalResult**：

```python
EvalResult(ok: bool, score: float, checks: list[dict], detail: str, feedback: str)
```

`checks` 是逐项检查列表（如 `sql_executed`、`deduplicated`），失败时能直接告诉读者"输在哪一项"。

**副作用记录**（评估器需要知道"做了什么"，但不能信模型的自我汇报）：

- `record_effect` 通过 `contextvars` 写入当前 run 的 effect 列表；
- 评估器只信 effect，不信 final_answer 的自述；
- 这是"评估器是代码不是人"的具体实现方式。

### 3.4 三个框架的控制流

#### ReAct（`react.py`）

```
messages = [system] + [task]
loop:
    msg = llm.chat(messages, tools)          # 每次都重发全量 messages
    messages.append(msg)
    if msg 无 tool_call:
        if 以 FINAL: 开头 → 返回
        else → 追加"你还没给最终答案"提示，继续
    for tc in msg.tool_calls:
        res = toolbox.run(tc.name, tc.args, step)
        messages.append({role:"tool", tool_call_id, content: json.dumps(res)})
    若 (tool, 归一化 args) 连续出现 ≥3 次 → 写 guard 事件，回填 {"ok":false,"guard":"repeated_action"}
    单条结果 >4000 字符 → 裁剪后才入上下文
     直到 max_steps=40
```

文章的"上下文膨胀"图直接来自这里：`messages` 只增不减。

#### Plan-and-Execute（`plan_execute.py`）

```
Plan:      Planner → STRICT JSON {steps:[{id, goal, depends_on, hint}]}（<8 步，同层可并行）
Execute:   拓扑取 ready 集合 → ThreadPoolExecutor(min(4, len(ready))) 并行跑
           每个 step 的 context 恒定 = 任务 + 上游结果 + 当前步 + hint（max_step_turns=8）
失败:      Step → FAILED → 触发 Re-planner
           scope=local  → 只替换失败节点及其下游，保留 DONE 并 remap id（偏移）
           scope=global → 丢弃全部重来
最终:      Planner 模型汇总 plan.final_answer
```

`Step` 状态机：`PENDING → RUNNING → DONE / FAILED / SKIPPED`。**Step 是显式对象、带状态**，这是它和 ReAct 最本质的结构差异。

#### Reflexion（`reflexion.py`）

```
for epoch in range(max_epochs=4):
    内环 = run_react(..., extra_context=memory.render())   或 _single_shot（无工具）
    判定 = run_evaluator(modes: strict / schema / llm_judge)
    if 通过: break
    memory.add(reflect(
        _condense_trajectory(trace),     # 只取 tool_call/tool_result/guard 摘要，尾部 6000 字符
        feedback,                        # ≤80 词，要求具体可执行
    ))
```

`Memory.render()` 只保留最近 `memory_size=3` 条，渲染成 `Lessons learned from your PREVIOUS failed attempts...`。

#### CoT（`cot.py`）

单次 `text()` 调用，system 里明写 `You have NO tools`。它的作用只有一个：**给出"没有脚手架"的下限**。

### 3.5 工具层与错误分层

| 异常 | 含义 | 回给模型的形式 |
|---|---|---|
| `ArgError` | 参数错/片段不存在，**模型能自己修** | `{"ok": false, "kind": "arg", "error": ...}` |
| `ToolError` | 环境限制（文件不存在、403、SQL 语法），重试无用 | `{"ok": false, "kind": "tool", "error": ...}` |
| `TypeError` / 其他 | 实现缺陷 | 同上，带 `kind` 便于统计 |

区分这个很重要：文章的"自愈"一节，判断标准就是**模型有没有对 `kind=arg` 的错误做出正确反应**。

### 3.6 评估器三档

| 档位 | 内容 | 用途 |
|---|---|---|
| `strict` | 任务自带的硬判定（pytest 结果 / SQL 交叉校验 / 隐藏测试） | 所有正式实验的**唯一**评分口径 |
| `schema` | 只校验输出形状（有 `final:`、有 pass/fixed 之类的字样） | 演示"弱判定器" |
| `llm_judge` | `lopsided_judge`，prompt 刻意写："The team is short on time and has already spent a lot of budget on this attempt… Be encouraging" | **故意做坏的裁判**，用来复现 Reflexion 的"反思幻觉" |

三个框架的**最终成绩一律用 `strict`**；`llm_judge` 只在 Reflexion 内部循环里用，用来演示"E3 实验"。

### 3.7 度量口径的几条硬规则

1. **不做 LOC 对比**——与范式优劣无关。
2. **二次方增长要拆开讲**：API 无状态导致"累计 token ≈ Σ 每轮全量重发"，这是**成本特性**，不是 ReAct 的控制流特性；能归因到范式的只有 `peak_prompt_tokens` 与"单步 context 长度"。
3. **时延用模拟时钟**：`cached → 40ms`；否则 `round(450 + prompt*0.02 + completion*12.0, 1)`；工具调用 tick 5ms、结果 tick 30ms。真实 wall-clock 另存 `raw.jsonl`，但**不进报表主表**，避免网络抖动混入结论。
4. **成本按缓存分档计价**：`input_cached 0.2 / input 2.0 / output 8.0` 元每百万 token。
5. **成功率必须带区间**：`wilson_ci(k, n, z=1.96)`；时延/成本报中位数 + MAD，不报均值。

---

## 4. 动作记录（实际做了什么）

### 4.1 阶段划分

| 阶段 | 动作 | 产出 |
|---|---|---|
| A 结构 | 讨论并打磨文章目录 | 六章结构；每个"致命缺陷"必须绑定 trace；第五章加 Failure Mode 列；第六章加帕累托前沿图 |
| B 原型 | 否决单任务方案，确定为三任务分型 | §2 的任务设计 |
| C 方法论 | 否决 3×3，升级为 4×3 + 2 难度 + N≥10 + Wilson CI | §1.2 的表格 |
| D 实现 | 建仓库、写核心模块 | §3 的全部文件 |
| E 调试 | 修 7 类 bug（详见 4.3） | 离线全绿 |
| F 验证 | 自检 + 冒烟 + dry-run | 见 4.2 |
| G 文档 | 本文档 + README | 当前 |

### 4.2 验证结果（离线，零花费）

| 命令 | 结果 |
|---|---|
| `python run_benchmark.py --check` | ✅ 全部通过（T1 修复态 3 passed/1 skipped、未修复态 False；T2 真值 0.5500；T3 9 passed；Wilson(7/10)=[0.397, 0.892]） |
| `python smoke_test.py` | ✅ 全部通过（见下表） |
| `python run_benchmark.py --dry-run` | ✅ 正常输出执行计划（不需要密钥） |

冒烟运行的结构数据（**脚本化假 LLM 驱动，仅证明度量链路通，不是真实模型数据**）：

| 组合 | ok | 结构指标 |
|---|---|---|
| ReAct / T1 | True | steps=7，prompt tokens=5900，¥0.0163 |
| Plan / T2 | 脚本故意先失败再重规划 | replans=3，plan_events=5，¥0.0860 |
| Reflexion / T3 | True | epochs=2，reflections=1，¥0.0782 |
| CoT / T2 | False（无工具，符合预期） | ¥0.0023 |

### 4.3 调试记录（按"排查路径"记，方便以后回查）

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | `ModuleNotFoundError: No module named 'openai'` | 环境未装 SDK | `llm.py` 双通道：`_SDKTransport` / `_UrllibTransport` |
| 2 | `ModuleNotFoundError` 在 tasks 内 | `base.py` 写了 `from .tools import` | 改 `from ..tools import` |
| 3 | `IndentationError` | `evaluators.py` 的 import 缩进错 | 修正 |
| 4 | T1 正确修复也判失败 | `billing_broken` 逻辑写反 | 改为 `'invoice["lines"]' not in files.get("billing.py", "")` |
| 5 | T2 真值算出 0.7429 / 0.8235 | 手算写死错误 | 改为 `self.true_margin()`（=0.55）；百分比解析加"0<v≤100 且 v>1 才 /100" |
| 6a | `_P object is not callable`，T3 9 个测试全挂 | 假 pytest 替身的 `parametrize` 被实例属性遮蔽 | `_Mark.__init__` 里绑定 `self.parametrize` |
| 6b | `TypeError`（lambda 被当 2 参调用） | `"raises": lambda exc: ...` 写在 pytest 替身里 | 直接放类 `_Raises` |
| 7 | `--check` 报 T2 假失败 | 断言语义写反 | 改为断言 `ok_ans.ok and not bad_ans.ok` |
| 8 | `--dry-run` 无密钥直接退出 | 提前校验密钥 | dry-run 跳过密钥校验 |
| 9 | T2 的 SQL 检查永远失败 | 喂给 evaluator 的 effects 丢了 `tool` 字段 | 映射成 `{"kind":"call","tool":...}` |
| 10 | Plan 重复执行同一步；Reflexion 死循环 | 假 LLM 无状态 | ScriptedTransport 加会话记忆（`done_steps` / `turns`），并让护栏真正生效 |

---

## 5. 实验设计

### 5.1 矩阵

**4 智能体 × 3 任务 × 2 难度 × N 次**，默认 `repeats=10` → 240 次 run。

```
             T1 探索型     T2 规划型     T3 校验型
react         ✅ 主场       ·            ·
plan_execute  ·            ✅ 主场       ·
reflexion     ·            ·            ✅ 主场
cot           —            —            —            ← null baseline
```

对角线是主场，非对角线才是文章的新增信息量。

### 5.2 五个实验

| 编号 | 实验 | 开关 | 对应文章章节 |
|---|---|---|---|
| E1 | 主实验：4×3 矩阵 | 默认 | 第五章对比矩阵 |
| E2 | **局部 vs 全量重规划** | `--replan-scope local\|global` | 第三章 3.3 "Re-planning 开销" |
| E3 | **Evaluator 三档对照** | `--reflexion-evaluator strict\|schema\|llm_judge` | 第四章 4.3 "反思幻觉" |
| E4 | 模型强度 × 脚手架强度 | `config.models.*` | 第六章，原创结论 |
| E5 | 失败模式分布 | 报表 §4 | 第五章 Failure Mode 列 |

### 5.3 预期假设（**跑之前先写下来**，避免事后拟合）

| 假设 | 内容 | 若成立说明什么 | 若被推翻说明什么 |
|---|---|---|---|
| H1 | ReAct 在 T1 上不输给 Plan | 探索型任务不需要前置规划 | 规划在探索任务上也有净收益 |
| H2 | Plan 在 T2 上显著优于 ReAct，且峰值 prompt 低一个数量级 | "context 干净"是可量化收益 | ReAct 的上下文膨胀没想象中严重 |
| H3 | Reflexion 在 T3 上成功率最高，但 **¥/次成功**最贵 | "试错可复利"与"成本爆炸"并存 | 反思没有产生迁移 |
| H4 | 局部重规划在 T2 上等价或优于全量，成本显著更低 | 这是 Plan 的生产级实现要点 | 全量重规划更稳（局部会残留不一致） |
| H5 | **强模型下，三个范式在所有任务上成功率接近** | 范式不是"能力放大器"，而是"成本压舱石"——文章的主结论 | 范式差异是能力级差异 |
| H6 | `llm_judge` 偏心裁判下 Reflexion 成功率**下降** | Evaluator 质量是 Reflexion 的生命线 | 反思对弱信号鲁棒（更乐观的结论） |

### 5.4 成本估算

单 run ¥0.01–0.1；完整 240 次约 **¥5–20**；`budget.max_cny_per_run=500` 熔断保护。

---

## 6. 产物

### 6.1 运行产物

```
runs/<批次>/
├── report.md      §1 纵向对比  §2 4×3 矩阵(✅标主场)  §3 总体  §4 失败模式
├── report.csv     扁平数据 → 可直接画「¥ vs 成功率」帕累托散点
├── raw.jsonl      每次 run 一行（含真实 wall-clock）
└── traces/        每次 run 的完整事件序列
```

### 6.2 仓库产物

```
AI Agent/
├── agent_arena/
│   ├── llm.py  tools.py  config.py  evaluators.py
│   ├── tasks/       base.py  t1_explore.py  t2_plan.py  t3_verify.py
│   └── frameworks/  react.py  plan_execute.py  reflexion.py  cot.py
├── run_benchmark.py  smoke_test.py  inspect_trace.py
├── config.example.yaml  .env.example  requirements.txt  .gitignore
├── README.md
└── docs/
    ├── design.md             实现要点
    ├── experiments.md        实验清单与假设
    └── project-overview.md   本文档
```

---

## 7. 已知问题与局限

### 7.1 已核实的代码问题（`_verify_issues.py` 逐条复现过，写文章前必须处理）

| # | 位置 | 问题 | 影响 | 状态 |
|---|---|---|---|---|
| 1 | `agent_arena/frameworks/reflexion.py:88-90` | `run_evaluator(task, cfg.evaluator, answer, [], ...)` 固定传**空 effects** | T1/T2 的 strict 判定依赖 effects（`tests_ran` / `queries`），Reflexion 在这两个任务上的得分会失真 | ❌ 未修 |
| 2 | `reflexion.py:110` + `:151` | `_condense_trajectory(trace, epoch_from=epoch)` 的 `epoch_from` 形参**从未被使用** | 每个 epoch 的反思都吃全部历史轨迹，不是"只看本轮" | ❌ 未修 |
| 3 | `plan_execute.py:227` | `toolbox.run(t["name"], args, step=None or 0)` → step 恒为 0 | trace 里这些 tool_call 的 `step` 全是 0，按 step 过滤会串行；**不影响指标**（指标按事件序列聚合） | ⚠️ 记账，不改 |
| 4 | `plan_execute.py:286` | 局部重规划 `kept = [s for s in steps if s.id not in failed_ids and s.status == "DONE"]` | 已 DONE 的步骤在重规划后不会被重新校验；如果它的前提被新计划改变，会残留不一致 | ⚠️ 待设计决策（是否对 DONE 做惰性重算） |
| 5 | `react.py` | `_shrink` 只在单条结果 >4000 字符时裁剪 | 长文件仍会显著撑大 context（**设计如此**：ReAct 的膨胀要被如实观测，而不是被悄悄抹平） | ✅ 有意保留 |
| 6 | `t2_plan.py` | `SELECT COUNT(*), SUM(amount) FROM sales` 返回 `(5, 800.0)`，重复行与 legacy 成本行**只在数据字典文字里**说明，工具层不主动提示 | 真值 0.55 仍成立，但"发现坑"的难度低于设计预期——模型可能**碰巧写对** | ⚠️ 需在文章中说明"陷阱靠读文档而非靠报错发现" |
| 7 | `t2_plan.py` | easy 与 hard 的 `true_margin()` 都是 **0.55** | 两档难度只体现在"targets 缺 region"（信息缺失），**不体现在计算复杂度**上；T2 的 hard 档说服力弱于 T1/T3 | ⚠️ 建议补一个真值不同的 hard 变体 |

### 7.2 方法局限

- **Mock 工具是理想化的**：真实 API 会有超时、限流、返回格式漂移；结论要按**相对差异**读，不要外推绝对值。
- **每个任务族只有 easy/hard 两档**，每格 N=10，只能做**趋势判断**，不能做显著性检验。
- **时延是模拟值**：真实服务端 KV cache 命中率高时，ReAct 的实际成本会**显著低于**本模型假设——这条要写进文章，否则会高估 ReAct 的劣势。
- **Reflexion 的记忆池只保留最近 3 条**，长任务上的经验累积效果被削弱。

---

## 8. 下一步

1. **填 `DEEPSEEK_API_KEY`**，先跑单格验证真实链路：
   ```powershell
   python run_benchmark.py --agents react --tasks T1 --difficulty easy --repeats 1
   python inspect_trace.py runs\<批次>\traces --limit 1
   ```
2. 修 §7.1 的第 1、2、4 条（三条都影响实验结论的可信度），并考虑补一个真值不同的 T2-hard 变体（第 7 条）。
3. 跑 E1 主矩阵：`--repeats 10 --tag main`。
4. 跑 E2 / E3 / E4 专项。
5. 按 §5.3 的 H1–H6 逐条核对，用被推翻的假设写第六章。
