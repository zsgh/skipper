# Skipper

**Skipper orchestrates, berth executes.**

Skipper 是一个 Agent Runtime：负责任务规划、并行调度、容错恢复。它不亲自执行任务，
而是把子任务分发给可插拔的 agent driver，driver 通过
[berth](https://github.com/zsgh/berth) 在沙箱里执行。

```
用户任务 → Temporal（planning → DAG 并行调度 → 汇总）→ Driver（self_loop / opencode）→ berth 沙箱
```

## 架构

```
┌───────────────────────────────────────────────────────────────┐
│  用户任务（HTTP API / CLI）                                    │
└──────────────────────┬────────────────────────────────────────┘
                       ▼
┌───────────────────────────────────────────────────────────────┐
│  Skipper（Temporal 编排层）                                     │
│                                                                │
│  Workflow: AgentTaskWorkflow                                   │
│   1. plan_task         LLM 把任务拆成 DAG（含 depends_on）      │
│   2. topological_batches 纯函数分批：批内并行、批间串行          │
│   3. run_subtask ×N    每批并行执行；失败即停；上游产物接力下游   │
│   4. aggregate_results 汇总成最终回复 + artifacts               │
└──────────────────────┬────────────────────────────────────────┘
                       ▼
┌───────────────────────────────────────────────────────────────┐
│  Driver 层（可插拔）                                            │
│   ┌─────────────────────┐   ┌─────────────────────┐            │
│   │ SelfLoopDriver      │   │ OpencodeDriver      │            │
│   │ 自研 CodeAct loop    │   │ 沙箱内跑 opencode    │            │
│   └──────────┬──────────┘   └──────────┬──────────┘            │
└──────────────┼─────────────────────────┼───────────────────────┘
               ▼            HTTP         ▼
┌───────────────────────────────────────────────────────────────┐
│  berth（沙箱执行服务 :9000）                                    │
│  POST /scripts/run  overlayfs 沙箱 · 用完即走 · /workspace 复用  │
└───────────────────────────────────────────────────────────────┘
```

## 核心能力

- **Task Planning**：LLM 拆任务成 DAG（带 schema 校验 + 无环检测 + 失败重试）
- **Parallel Execution**：无依赖的子任务并行，同批各用独立沙箱
- **Pluggable Drivers**：`self_loop`（自研 CodeAct loop）、`opencode`（沙箱里跑 opencode），接口统一
- **Durable Execution**：Temporal 保证，kill worker → 重启 → 从断点继续，已完成的 activity 不重跑
- **Full Observability**：Phoenix trace 覆盖 workflow → activity → driver → berth
- **依赖数据接力**：上游子任务的文本产物自动进入下游沙箱，DAG 不只是顺序

## 快速开始

前置：

- Python 3.10+
- [Temporal CLI](https://temporal.download)（`temporal server start-dev`）
- berth 在跑（`curl localhost:9000/health` 返回 `{"status":"ok"}`）
- 一个 OpenAI 兼容 LLM 端点（OpenAI / GLM / MiMo / vLLM…）

```bash
# 1) 安装
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2) 配置（.env 会被自动加载；也可以直接 export）
cp .env.example .env
#   SKIPPER_LLM_BASE_URL=https://api.openai.com/v1
#   SKIPPER_LLM_API_KEY=sk-...
#   SKIPPER_LLM_MODEL=gpt-4o-mini

# 3) 一键拉起本地依赖（Temporal / Phoenix / worker / FastAPI；已在跑的会跳过）
bash scripts/start_dev.sh

#    等价的手动方式：
#    temporal server start-dev --ui-port 8233 --port 7233
#    phoenix serve --port 6006
#    python -m skipper.temporal.worker

#    重启机器后先跑这个检查状态（Temporal 数据持久化在 .temporal/skipper.db）：
#    bash scripts/start_dev.sh --status

# 4) 提交任务
python scripts/submit_task.py "写三个文件 a.txt/b.txt/c.txt 分别放 A/B/C，再把三个文件合并成 merged.txt" --driver self_loop

# 或:
skipper submit "..." --driver opencode
```

## 演示

### demo 1：最小链路（Temporal → berth）

```bash
python scripts/submit_task.py --hello "print('hello from berth')"
# HelloWorkflow: exit_code=0 stdout='hello from berth'
```

### demo 2：复杂任务（多子任务 + 并行 + 接力）

```bash
python scripts/submit_task.py "写三个文件：/workspace/a.txt 内容为 A、/workspace/b.txt 内容为 B、\
/workspace/c.txt 内容为 C；然后读取这三个文件，按 a、b、c 的顺序拼接，写入 /workspace/merged.txt 并打印。" \
--driver self_loop --wait
```

规划结果（实测）：s1/s2/s3 并行写三个文件 → s4 依赖它们，把三个文件合并成 `merged.txt`。
s4 的沙箱里会自动出现接力过来的 `a.txt`/`b.txt`/`c.txt`。

### demo 3：kill-and-resume

```bash
bash scripts/kill_and_resume.sh
```

流程：提交任务（3 个子任务：s1/s2 并行写文件，s3 依赖它们合并）→ 杀掉 worker（SIGTERM 模拟崩溃）
→ 重启 worker → 任务从断点继续。实测输出：

```
plan_task          kill 前调度 1 次 → 完成后共 1 次
run_subtask        kill 前调度 3 次 → 完成后共 3 次
aggregate_results  kill 前调度 0 次 → 完成后共 1 次
（值不变 = 已完成的 activity 没有重跑；resume 只补跑没做完的）
```

s1/s2 在 kill 前已完成，重启后被**直接复用**（没有重新调度）；s3 被 kill 打断，重启后
attempt=2 继续，并且拿到了接力过来的 a.txt/b.txt。

### demo 4：双驱动对比

同一个任务：**「计算 1 到 100 的和并打印，保存到 `/workspace/sum.txt`」**，
两个 driver 各跑一遍（`scripts/benchmark.py --runs 1 --tasks 4 --driver both`）：

| 驱动 | 耗时 | 子任务数 | LLM token | 产物 | 结果 |
|---|---|---|---|---|---|
| self_loop | 19.9s | 1 | 495 | `sum.txt` | ✅ completed |
| opencode | 29.2s | 1 | n/a* | `sum.txt` | ✅ completed |

4 个任务 × 各 1 次汇总：**self_loop 成功率 100% / 平均 22.6s / 平均 749 tokens**；
**opencode 成功率 100% / 平均 32.8s**（token 数需要 `SKIPPER_OPENCODE_FORMAT=json`，
让 opencode 输出 JSON 事件流后才能统计）。

> 数据由双驱动评测产生（`results/dual-driver.json`）。opencode 需要在宿主机上构建
> `opencode` 镜像：`sudo scripts/build_opencode_rootfs.sh`。

### 评测集

```bash
python scripts/benchmark.py --runs 3 --driver both        # 10 任务 × 3 次 × 2 驱动
python scripts/benchmark.py --runs 1 --tasks 4 --driver both --json results/bench.json
```

实测（10 个任务 × 1 次，self_loop）：**成功率 100%，平均 35.9s，平均 2117 tokens**
（`results/benchmark-self-loop-10.json`）。统计成功率 / 平均耗时 / 平均 token，结果写入 `results/`。

## 目录结构

```
skipper/
├── skipper/
│   ├── temporal/      # workflows / activities / worker / client
│   ├── drivers/       # base / self_loop / opencode / registry
│   ├── planner/       # schema / prompts / parse（校验 DAG）
│   ├── clients/       # berth HTTP 客户端
│   ├── api/           # 可选 FastAPI 包装
│   ├── artifacts.py   # 产物噪音过滤
│   ├── config.py      # 环境变量配置
│   ├── tracing.py     # Phoenix / OTel 接入
│   └── cli.py         # `skipper` 命令
├── tests/             # unit（stub）+ integration（真实服务）
├── scripts/           # submit / kill_and_resume / benchmark / build_opencode_rootfs
└── docs/              # architecture / drivers / planning
```

## 测试

```bash
pytest                                  # 单元测试（不依赖外部服务）
pytest tests/test_integration.py        # 集成测试（berth / Temporal / LLM 探测不到就 skip；
                                        #  opencode 镜像不存在时对应用例自动 skip）
SKIPPER_E2E=1 pytest tests/test_integration.py   # 加跑完整 AgentTaskWorkflow（self_loop + opencode）
```

## 实现注记（踩过的坑）

| 坑 | 现象 | 处理 |
|---|---|---|
| httpx 走 socks 代理 | `ImportError: socksio` | 依赖里带 `httpx[socks]`；localhost 走 `NO_PROXY` |
| berth 的 /proc 泄漏宿主路径 | opencode 探测项目根失败（realPath ENOENT） | driver 先 `git init /workspace` + 显式 `--dir` |
| opencode 内存 | Bun 运行时 > 256MB 被 cgroup/全局 OOM 杀 | `SKIPPER_OPENCODE_MEMORY_MB`（默认 1024），berth 宿主机建议 4GB+ |
| npm 在 chroot 误判 libc | 装了 musl 版 opencode | 构建 rootfs 时挂载 `/proc`（`build_opencode_rootfs.sh` 已处理） |
| opencode 事件流太长 | stdout 被 berth 截断（10k） | 沙箱内落盘解析，只回传 `{final_text, tokens, steps}` 摘要 |
| Phoenix + Temporal 插件 | 全局 provider 必须是 ReplaySafe；Simple exporter 在 sandbox 里报错 | `create_tracer_provider()` + `BatchSpanProcessor` |
| Phoenix 里 kind/input/output 空白 | 普通 attribute 不显示 | `tracing.span(kind=..., input=..., output=...)` 统一写 OpenInference 语义属性 |
| 子任务沙箱隔离 | 下游读不到上游文件 | 依赖产物接力（文本 ≤8KB/文件）+ 上游输出摘要拼进描述 |

## 验收状态

- [x] berth 客户端健康检查 / 跑脚本 / workspace 复用
- [x] 最小 workflow（HelloWorkflow）在 Temporal UI 完成
- [x] `SelfLoopDriver` / `OpencodeDriver` 各自跑通任务，registry 列出两个 driver
- [x] `plan_task` 拆 DAG、无环、依赖完整（含失败重试）
- [x] `AgentTaskWorkflow` 完整跑通（并行批次 + 失败即停 + 汇总）
- [x] Phoenix 完整 trace 树（workflow → plan → subtask → driver → berth）
- [x] CLI 提交并跟踪；kill-and-resume 演示通过
- [x] 双驱动对比有数据；评测集可跑
