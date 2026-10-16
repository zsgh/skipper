# 架构：Skipper 是怎么编排的

> **Skipper orchestrates, berth executes.**

## 一张图

```
┌───────────────────────────────────────────────────────────────┐
│  用户任务（HTTP API / CLI）                                    │
└──────────────────────┬────────────────────────────────────────┘
                       │  start_workflow(AgentTaskWorkflow)
                       ▼
┌───────────────────────────────────────────────────────────────┐
│  Skipper（Temporal 编排层）                                     │
│                                                                │
│  Workflow: AgentTaskWorkflow                                   │
│   1. Activity: plan_task        ← LLM 拆任务成 DAG              │
│   2. 纯函数: topological_batches ← 分批（批内并行）             │
│   3. Activity: run_subtask ×N   ← 每批 gather，失败即停         │
│        └─ 依赖产出接力：上游 artifacts → 下游沙箱 /workspace    │
│   4. Activity: aggregate_results ← 汇总成最终回复               │
└──────────────────────┬────────────────────────────────────────┘
                       │ Activity: run_subtask
                       ▼
┌───────────────────────────────────────────────────────────────┐
│  Driver 层（可插拔）                                            │
│   ┌─────────────────────┐   ┌─────────────────────┐            │
│   │ SelfLoopDriver      │   │ OpencodeDriver      │            │
│   │ 自研 CodeAct loop    │   │ 沙箱里跑 opencode    │            │
│   └──────────┬──────────┘   └──────────┬──────────┘            │
└──────────────┼─────────────────────────┼───────────────────────┘
               │           HTTP          │
               ▼                         ▼
┌───────────────────────────────────────────────────────────────┐
│  berth（沙箱执行服务，:9000）                                    │
│  POST /scripts/run  overlayfs 沙箱 + 用完即走 + /workspace 复用  │
└───────────────────────────────────────────────────────────────┘
```

## 各层职责

| 层 | 职责 | 不做什么 |
|---|---|---|
| API / CLI | 收任务、回结果 | 不拆任务 |
| Workflow | 拆解调度、并行、容错、断点恢复 | 不调 LLM / 不调 berth |
| Activity | 所有副作用（LLM、沙箱执行、汇总） | 不做流程决策 |
| Driver | 「怎么把子任务做完」 | 不感知 DAG / 其它子任务 |
| berth | 「给我代码，我跑给你看」 | 不做任何编排 |

## 可靠性设计

### 1. Temporal 确定性

- Workflow 里只有：调 activity、`asyncio.gather`、`topological_batches`（纯函数）、`workflow.now()`。
- 时钟、随机数、网络、文件系统一律不走 workflow。
- 所以 worker 崩了以后，重放 event history 得到的状态和崩溃前完全一致。

### 2. Activity 幂等

- `sandbox_id = f"{task_id}-{subtask_id}"` 是确定性的；berth 按 task_id 复用 `/workspace`。
- 重试 = 在同一个沙箱里再跑一次，已写出的文件还在，不会污染别的任务。
- workflow 里 activity 的重试策略：`run_subtask` 最多 2 次（间隔 5s、指数退避）；
  `plan_task` 最多 3 次（LLM 抖动兜底）。

### 3. 断点恢复

- 每个 activity 的完成结果都进 Temporal event history。
- worker 被杀 → 正在跑的 activity 靠 **heartbeat_timeout(2min)** 判定失联后重试；
  已完成的 activity 直接复用 history，不重跑。
- `scripts/kill_and_resume.sh` 演示并核对这一点（对比 ActivityTaskScheduled 次数）。

### 4. 失败传播

- 某批里有子任务没完成 → 停止调度后续批次（后续子任务记为 skipped）。
- `aggregate_results` 按「全部完成 / 部分完成 / 全失败」标 `completed / partial / failed`，
  汇总时明确说明哪一步失败。

### 5. 沙箱隔离与数据接力

- 并行的子任务各自独立沙箱（不会互相踩文件）；沙箱内以 uid 1000 运行，capability 清空。
- 依赖关系 = 数据流：`run_subtask` 会把**直接依赖的文本产物**（≤ 8KB/文件、有总量上限）
  从上游沙箱接力到下游沙箱的 `/workspace`，并把上游输出摘要拼进子任务描述。
- 二进制/超大文件不接力（只跳过，不报错）。

## 可观测性

- Phoenix（OTLP）接收 trace；Temporal 的 `OpenTelemetryPlugin` 负责跨 workflow/activity 传递上下文。
- 一棵完整 trace 树：

```
RunWorkflow:AgentTaskWorkflow
├── StartActivity:plan_task → RunActivity:plan_task → plan_task.llm_call → ChatCompletion
├── StartActivity:run_subtask → RunActivity:run_subtask → run_subtask
│     ├── driver.self_loop.llm_turn → ChatCompletion
│     └── driver.self_loop.sandbox_exec → berth.run_script
└── StartActivity:aggregate_results → ... → ChatCompletion
```

- 每个 span 带 `subtask_id` / `driver` / `sandbox_id` 属性；activity 结果里有 `trace_id` 可以互查。

## 配置

全部走环境变量（见 `.env.example`）：Temporal 地址 / berth 地址 / LLM（OpenAI 兼容）/
driver 默认值与资源上限 / Phoenix 端点。密钥只放环境变量，不落代码不落 rootfs。
