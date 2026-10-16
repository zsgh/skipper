# Planning：拆任务的设计与约束

## 目标

把用户的一句话任务，拆成 **3-8 个能独立交给一个 agent 的子任务**，用 `depends_on` 组成 DAG，
让 Temporal 按批并行调度。

## 输出格式（schema）

```json
{
  "subtasks": [
    {
      "id": "s1",
      "description": "分析项目结构，列出 5 个模块路径，输出到 /workspace/modules.txt",
      "depends_on": [],
      "estimated_complexity": "simple | medium | complex",
      "can_parallel": true
    }
  ],
  "reasoning": "为什么这样拆（简短）"
}
```

- `depends_on`：依赖的上游子任务 id。`can_parallel=false` 表示该子任务独占一批（强制串行）。
- 校验（`skipper/planner/parse.py`）：id 唯一、依赖存在、无环（Kahn 检测）、子任务数 1-16。
- LLM 输出容错：先整体 `json.loads`，再抠 ```json 代码块，再扫描第一个平衡的 `{}`。

## Prompt 的几条硬约束（`skipper/planner/prompts.py`）

| # | 约束 | 为什么 |
|---|---|---|
| 1 | 每个子任务「一段能独立执行的描述」 | agent 看不到别的子任务上下文 |
| 2 | 有依赖用 `depends_on`，无依赖可并行 | 并行度交给编排层 |
| 3 | 粒度 3-8 个；不要太底层/太笼统 | 太细 token 爆炸，太粗没法并行 |
| 4 | 并行子任务不能共享文件；需要共享必须串行 | 每个子任务独立沙箱 |
| 5 | 描述里写清产出（文件名、打印什么） | 便于汇总与接力 |
| 6 | 需要真实账号/交互的步骤不拆 | 沙箱里做不了 |
| 7 | 只输出 JSON | 流水线解析 |

Prompt 里带一个 few-shot 示例（5 个模块生成测试），示例演示了「先分析（s1）→
后并行（s2…s6 都依赖 s1）」的常见形状。

## 失败重试（plan_task activity）

LLM 输出不合规是常态，处理方式是把错误喂回去重试：

```
第 1 次：system prompt + 任务描述
    ↓ 解析/校验失败（PlanParseError）
第 2 次：追加 assistant 原始输出 + 「上次哪里错了，重新输出完整 JSON」
    ↓ 再失败
第 3 次：再来一遍
    ↓ 再失败 → ApplicationError(non_retryable)，workflow 失败并给出原因
```

配套机制：

- 优先用 `response_format={"type": "json_object"}`（供应商支持时更稳），
  400/不支持时自动回退普通调用（`_chat_with_json_fallback`）。
- 解析分层：`extract_json`（宽容抠 JSON）→ `parse_plan`（pydantic 校验）→
  `validate_plan`（DAG 合法性），错误信息具体到「哪个 id 依赖了谁」。

## 依赖产出的接力

规划层只需要记住一条：**依赖 = 数据流**。

`run_subtask` 执行下游客子任务前会：

1. 读取直接依赖（`depends_on`）的**文本产物**，写入下游沙箱的 `/workspace`；
2. 把上游子任务的输出摘要拼进子任务描述。

所以「s3: 读取 a.txt 和 b.txt 合并」这种描述能成立，即使 s3 和 s1/s2 不在同一个沙箱。

## 调优建议

- 任务太扁（10+ 个并行子任务）：调 prompt 的粒度约束，或在 `run_subtask` 里限制并发。
- 子任务老是失败：看 Phoenix 里 `run_subtask → driver.*` 的 span，是 LLM 没懂（prompt 问题）
  还是沙箱问题（berth span 报错）。
- 想换更强的模型：`SKIPPER_LLM_MODEL` 指到任何 OpenAI 兼容模型即可，planning 和 driver 共用。
