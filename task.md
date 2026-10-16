# Skipper 任务书：Temporal 编排 + 可插拔 Driver + berth 执行

> **Skipper orchestrates, berth executes.**
>
> 交给 AI 编码实现。**依赖已有的 berth 服务**（https://github.com/zsgh/berth），不要重写沙箱。

---

## 一、项目定位

**Skipper** = 航海隐喻里的"船长"——不亲自开船（执行），负责规划航程（拆任务）、指挥船员（调度 driver）、处理突发（容错恢复）。

**berth** = 泊位——船停靠的地方，具体执行任务的沙箱。

两个项目一套：**Skipper 在上编排，berth 在下执行。**

---

## 二、项目目标

构建一个 **Agent Runtime**：

1. **用户提交任务** → Skipper 接收
2. **Skipper 做 planning** → 调 LLM 拆任务成 DAG
3. **子任务分发给可插拔 Driver** → 每个 driver 用自己的方式干活
4. **Driver 通过 berth 执行** → 沙箱里跑代码
5. **结果汇总** → 返回给用户
6. **全程可恢复** → Temporal 保证，worker 崩了任务不丢

**核心验证点**：kill worker → 重启 → 任务从断点继续，不重跑已完成的 activity。

---

## 三、架构

```
┌───────────────────────────────────────────────────────────────┐
│  用户任务（HTTP API 或 CLI）                                    │
└──────────────────────┬────────────────────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────────────────────┐
│  Skipper（Temporal 编排层）                                     │
│                                                                │
│  ┌──────────────────────────────────────────────────────┐    │
│  │  Workflow: AgentTaskWorkflow                          │    │
│  │                                                        │    │
│  │  1. Activity: plan_task                               │    │
│  │     输入: 用户任务描述                                  │    │
│  │     输出: Plan = [                                    │    │
│  │       { id, description, depends_on: [] },            │    │
│  │       { id, description, depends_on: [s1] }           │    │
│  │     ]                                                  │    │
│  │                                                        │    │
│  │  2. Workflow 内部: 拓扑排序 → 分批并行执行              │    │
│  │     for batch in topological_batches(plan):           │    │
│  │       results = await gather([                        │    │
│  │         Activity: run_subtask(s) for s in batch       │    │
│  │       ])                                               │    │
│  │                                                        │    │
│  │  3. Activity: aggregate_results                       │    │
│  │     输入: 所有子任务结果                                │    │
│  │     输出: 最终回复 + artifacts 列表                     │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────┬────────────────────────────────────────┘
                       │ Activity: run_subtask
                       ▼
┌───────────────────────────────────────────────────────────────┐
│  Driver 层（可插拔）                                            │
│                                                                │
│  ┌─────────────────────┐    ┌─────────────────────┐           │
│  │ SelfLoopDriver      │    │ OpencodeDriver      │           │
│  │ 自研 CodeAct loop    │    │ 在沙箱里跑 opencode  │           │
│  │ 调 berth /scripts/run│    │ 调 berth /scripts/run│           │
│  └──────────┬──────────┘    └──────────┬──────────┘           │
└─────────────┼────────────────────────────┼────────────────────┘
              │                            │
              └──────────────┬─────────────┘
                             │ HTTP
                             ▼
┌───────────────────────────────────────────────────────────────┐
│  berth（已有服务，:9000）                                       │
│  ├── /scripts/run     跑一次性脚本                              │
│  ├── /services        长驻服务（mysql/redis）                    │
│  └── overlayfs 沙箱 + systemd 托管 + host 网络                  │
└───────────────────────────────────────────────────────────────┘
```

---

## 四、关键技术决策（不要改）

| 决策 | 选择 | 原因 |
|---|---|---|
| 编排层 | **Temporal** | durable execution 的正解 |
| Planning 位置 | **Temporal Workflow 内部调 Activity** | workflow 必须确定性，LLM 调用是副作用 |
| Driver 抽象 | **可插拔**，接口统一 | 验证 runtime 的通用性 |
| 沙箱 | **复用 berth**，不重写 | berth 已稳定 |
| Planning 输出 | **DAG 格式**（含 depends_on） | 让 Temporal 做并行调度 |
| 子任务沙箱分配 | **同一批并行的子任务，各自独立沙箱** | 隔离，且都能用 berth |
| 断点恢复 | **Temporal event history** | 不手写 SQLite 快照 |

---

## 五、目录结构

```
~/stage3/skipper/
├── README.md                    # "Skipper orchestrates, berth executes."
├── requirements.txt
├── pyproject.toml
├── .env.example
│
├── skipper/                     # Python 包
│   ├── __init__.py
│   │
│   ├── temporal/
│   │   ├── worker.py            # worker 启动入口
│   │   ├── client.py            # 提交任务的客户端
│   │   ├── workflows.py         # AgentTaskWorkflow
│   │   └── activities.py        # plan_task / run_subtask / aggregate
│   │
│   ├── drivers/
│   │   ├── base.py              # AgentDriver 抽象
│   │   ├── self_loop.py         # 自研 CodeAct
│   │   ├── opencode.py          # opencode 驱动
│   │   └── registry.py          # driver 注册表
│   │
│   ├── clients/
│   │   └── berth.py             # berth HTTP 客户端封装
│   │
│   ├── planner/
│   │   ├── schema.py            # Plan / Subtask
│   │   └── prompts.py           # planning system prompt
│   │
│   └── api/
│       └── server.py            # 可选：FastAPI 包装（提交/查询任务）
│
├── tests/
│   ├── test_workflow.py
│   ├── test_drivers.py
│   └── test_integration.py
│
└── scripts/
    ├── submit_task.py           # CLI：提交任务
    ├── kill_and_resume.sh       # 演示断点恢复
    ├── build_opencode_rootfs.sh # 构建带 opencode 的 rootfs
    └── benchmark.py             # 评测集
```

**Python 包名**：`skipper`。用户能这样用：

```python
from skipper.temporal.client import submit_task
from skipper.drivers import DriverRegistry
from skipper.clients.berth import BerthClient
```

---

## 六、数据模型

### 6.1 Plan

```python
# skipper/planner/schema.py
from pydantic import BaseModel
from typing import List, Optional

class Subtask(BaseModel):
    id: str                          # "s1", "s2"
    description: str
    depends_on: List[str] = []
    preferred_driver: Optional[str] = None
    estimated_complexity: str = "medium"  # simple | medium | complex
    can_parallel: bool = True
    metadata: dict = {}

class Plan(BaseModel):
    subtasks: List[Subtask]
    reasoning: str
    total_estimated_steps: int
```

### 6.2 SubtaskResult

```python
class SubtaskResult(BaseModel):
    subtask_id: str
    status: str                      # completed | failed | timeout
    driver: str
    sandbox_id: str
    output: str
    artifacts: List[str] = []
    duration_ms: int
    error: Optional[str] = None
    trace_id: Optional[str] = None
```

### 6.3 TaskResult

```python
class TaskResult(BaseModel):
    task_id: str
    status: str                      # completed | partial | failed
    plan: Plan
    subtask_results: List[SubtaskResult]
    final_output: str
    total_duration_ms: int
```

---

## 七、阶段任务

### 阶段一：基础设施 + Temporal 骨架

#### 任务 1.1 项目初始化

```bash
mkdir -p ~/stage3/skipper
cd ~/stage3/skipper
python3 -m venv .venv && source .venv/bin/activate
pip install temporalio httpx pydantic openai
pip install opentelemetry-api opentelemetry-sdk \
    opentelemetry-exporter-otlp \
    openinference-instrumentation-openai
pip install arize-phoenix arize-phoenix-otel

# 建目录结构
mkdir -p skipper/{temporal,drivers,clients,planner,api}
mkdir -p tests scripts
touch skipper/__init__.py
touch skipper/temporal/__init__.py
touch skipper/drivers/__init__.py
touch skipper/clients/__init__.py
touch skipper/planner/__init__.py
```

**验证**：

```bash
python3 -c "import temporalio; print(temporalio.__version__)"
```

#### 任务 1.2 berth 客户端封装

**`skipper/clients/berth.py`**：

```python
class BerthClient:
    def __init__(self, base_url: str = "http://localhost:9000"):
        self.base_url = base_url

    def run_script(
        self,
        code: str,
        task_id: str,
        timeout: int = 60,
        language: str = "python",
        image: str = None,
    ) -> dict:
        ...

    def start_service(self, name: str, template: str) -> dict: ...
    def stop_service(self, name: str) -> dict: ...
    def health(self) -> dict: ...
```

**验证**：

```python
from skipper.clients.berth import BerthClient
c = BerthClient()
assert c.health()["status"] == "ok"
r = c.run_script('print("hello")', "test-1")
assert "hello" in r["stdout"]
```

#### 任务 1.3 最小 Workflow

- `skipper/temporal/worker.py`
- `skipper/temporal/workflows.py`：一个最简单的 workflow，调一个 activity 跑 `print("hello")`
- `skipper/temporal/client.py`：提交任务

**验证**：

```bash
# 终端 1
temporal server start-dev

# 终端 2
python -m skipper.temporal.worker

# 终端 3
python -m skipper.temporal.client "print('hello')"

# Temporal UI (localhost:8233) 里能看到 workflow 完成
```

---

### 阶段二：Driver 抽象层

#### 任务 2.1 Driver 接口

**`skipper/drivers/base.py`**：

```python
from abc import ABC, abstractmethod

class AgentDriver(ABC):
    @abstractmethod
    async def run(
        self,
        task: str,
        sandbox_id: str,
        max_turns: int = 20,
        timeout: int = 600,
    ) -> dict:
        """返回 SubtaskResult 的 dict 形式。"""

    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def describe(self) -> dict: ...
```

#### 任务 2.2 SelfLoopDriver

**`skipper/drivers/self_loop.py`**：把之前 Day10-11 的 CodeAct loop 包成 driver。

**核心逻辑**：

```
1. 收到 task 描述
2. LLM 生成代码
3. 调 berth /scripts/run 执行
4. 结果回填给 LLM
5. 循环，直到 TASK_DONE 或 max_turns
6. 返回 SubtaskResult
```

#### 任务 2.3 OpencodeDriver

**前置**：构建带 opencode 的 rootfs。

**`scripts/build_opencode_rootfs.sh`**：

```bash
#!/bin/bash
set -euo pipefail
ROOTFS="${1:-/opt/sandbox-rootfs-opencode}"

sudo debootstrap --variant=minbase bookworm "$ROOTFS" http://deb.debian.org/debian
sudo chroot "$ROOTFS" /bin/bash -c "
    apt update
    apt install -y python3 python3-pip curl git nodejs npm ca-certificates
    npm install -g @opencode/cli
"
sudo chroot "$ROOTFS" opencode --version
```

**`skipper/drivers/opencode.py`**：

```python
class OpencodeDriver(AgentDriver):
    def name(self): return "opencode"

    async def run(self, task, sandbox_id, max_turns=20, timeout=600):
        code = f"""
import subprocess
result = subprocess.run(
    ["opencode", "run", "--non-interactive", "--task", {repr(task)}],
    capture_output=True, text=True, timeout={timeout - 30},
    cwd="/workspace",
)
print(result.stdout)
if result.stderr:
    print("---STDERR---")
    print(result.stderr)
"""
        berth_result = self.berth.run_script(
            code=code, task_id=sandbox_id,
            timeout=timeout, image="opencode",
        )
        ...
```

**注意**：opencode 的 API key 通过 **berth 的环境变量注入机制**传入，不写死 rootfs。

#### 任务 2.4 Driver Registry

**`skipper/drivers/registry.py`**：

```python
class DriverRegistry:
    def __init__(self, berth_client, llm_client):
        self.drivers = {
            "self_loop": SelfLoopDriver(berth_client, llm_client),
            "opencode": OpencodeDriver(berth_client),
        }

    def get(self, name: str) -> AgentDriver: ...
    def list(self) -> list[dict]: ...
```

**验证**：

```python
from skipper.drivers import DriverRegistry
r = DriverRegistry(berth, llm)
assert "self_loop" in [d["name"] for d in r.list()]
assert "opencode" in [d["name"] for d in r.list()]
```

---

### 阶段三：Planning Activity

#### 任务 3.1 Plan Schema + Prompt

- `skipper/planner/schema.py`（见数据模型）
- `skipper/planner/prompts.py`：planning system prompt

**Planning prompt 关键约束**：

```
你是任务规划器。把用户的任务拆成子任务 DAG。

要求：
1. 每个子任务必须是"一段能独立执行的描述"，能交给一个 agent 完成
2. 有依赖的子任务用 depends_on 声明
3. 无依赖的子任务可以并行
4. 拆分粒度：3-8 个子任务
5. 子任务不要太底层
6. 考虑沙箱隔离：两个子任务需要共享文件 → 必须串行
7. 输出必须是合法 JSON

示例：
输入: "给项目里 5 个模块各生成测试"
输出: {
  "subtasks": [
    {"id": "s1", "description": "给 module_a 生成测试", "depends_on": []},
    ...
  ],
  "reasoning": "..."
}
```

#### 任务 3.2 plan_task Activity

**`skipper/temporal/activities.py`**：

```python
@activity.defn
async def plan_task(task: str) -> dict:
    # 1. 调 LLM
    # 2. 解析 JSON
    # 3. Pydantic 校验
    # 4. 检查依赖完整性（无环、无未知依赖）
    # 5. 返回 Plan dict
```

---

### 阶段四：完整 Workflow

#### 任务 4.1 AgentTaskWorkflow

**`skipper/temporal/workflows.py`**：

```python
@workflow.defn
class AgentTaskWorkflow:
    @workflow.run
    async def run(self, task: str, driver_name: str = "self_loop") -> dict:
        task_id = workflow.info().workflow_id

        # 1. Planning
        plan_dict = await workflow.execute_activity(
            plan_task, task,
            start_to_close_timeout=timedelta(seconds=120),
        )
        plan = Plan(**plan_dict)

        # 2. 拓扑排序，分批并行
        batches = topological_batches(plan.subtasks)
        all_results = []
        for batch in batches:
            batch_tasks = [
                workflow.execute_activity(
                    run_subtask,
                    args=[subtask.model_dump(), driver_name, task_id],
                    start_to_close_timeout=timedelta(minutes=30),
                    heartbeat_timeout=timedelta(minutes=2),
                )
                for subtask in batch
            ]
            batch_results = await asyncio.gather(*batch_tasks)
            all_results.extend(batch_results)
            if any(r["status"] == "failed" for r in batch_results):
                break

        # 3. 汇总
        return await workflow.execute_activity(
            aggregate_results,
            args=[task, plan.model_dump(), all_results],
            start_to_close_timeout=timedelta(seconds=60),
        )
```

#### 任务 4.2 run_subtask Activity

```python
@activity.defn
async def run_subtask(subtask: dict, driver_name: str, task_id: str) -> dict:
    # 确定性 sandbox_id
    sandbox_id = f"{task_id}-{subtask['id']}"
    driver = registry.get(driver_name)
    return await driver.run(
        task=subtask["description"],
        sandbox_id=sandbox_id,
        timeout=1800,
    )
```

#### 任务 4.3 aggregate_results Activity

```python
@activity.defn
async def aggregate_results(task: str, plan: dict, results: list) -> dict:
    # 简单版：拼接 output
    # LLM 版：调 LLM 写总结
    ...
```

#### 任务 4.4 拓扑排序

**`skipper/temporal/workflows.py`** 里加纯函数：

```python
def topological_batches(subtasks: list[dict]) -> list[list[dict]]:
    """
    例: [A, B(dep A), C(dep A), D(dep B,C)]
    输出: [[A], [B, C], [D]]
    """
```

**必须是纯函数**（不读文件、不调网络、不用随机数）。

---

### 阶段五：可观测性

#### 任务 5.1 Phoenix trace

**`skipper/temporal/worker.py`** 里初始化：

```python
from phoenix.otel import register
from openinference.instrumentation.openai import OpenAIInstrumentor

tracer_provider = register(
    project_name="skipper",
    endpoint="http://localhost:6006/v1/traces",
    batch=False,
)
OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)
```

**埋点**：

- `plan_task` 一个 span
- 每个 `run_subtask` 一个 span
- driver 内每次 LLM 调用和 berth 调用是子 span
- 每个 span 带 `subtask_id`、`driver`、`sandbox_id` 属性

**验证**：Phoenix UI 里能看到完整 trace 树：workflow → plan → subtasks → driver → berth。

---

### 阶段六：CLI + 演示

#### 任务 6.1 提交任务的 CLI

**`scripts/submit_task.py`**：

```bash
python scripts/submit_task.py \
  "给项目里的 5 个模块各生成一个 pytest 测试" \
  --driver self_loop
```

输出：

```
Task ID: agent-task-abc123
Plan:
  s1: 分析项目结构 (depends_on: [])
  s2: 给 module_a 生成测试 (depends_on: [s1])
  ...
Tracking: http://localhost:8233/namespaces/default/workflows/agent-task-abc123
```

#### 任务 6.2 kill-and-resume 演示脚本

**`scripts/kill_and_resume.sh`**：

```bash
#!/bin/bash
python scripts/submit_task.py "复杂任务..." --driver self_loop &
TASK_PID=$!
sleep 10
pkill -f "skipper.temporal.worker"
sleep 5
python -m skipper.temporal.worker &
wait
```

**验证**：

- 日志显示 worker 重启后 workflow 从断点继续
- 已完成的 activity 没有重新执行
- Temporal UI event history 完整

#### 任务 6.3 双驱动对比

同一个任务，两个 driver 各跑一次：

| 驱动 | 耗时 | 生成文件数 | LLM token | 成功率 |
|---|---|---|---|---|
| self_loop | ? | ? | ? | ? |
| opencode | ? | ? | ? | ? |

写入 README。

#### 任务 6.4 评测集

**`scripts/benchmark.py`**：10 个任务 × 3 次，统计成功率、平均耗时、平均 token。

---

## 八、验收标准

### 阶段一
- [ ] `pip install` 全成功
- [ ] berth 客户端健康检查通过
- [ ] 最小 workflow 在 Temporal UI 里完成

### 阶段二
- [ ] `SelfLoopDriver` 跑通一个简单任务
- [ ] `OpencodeDriver` 跑通一个简单任务
- [ ] registry 列出两个 driver

### 阶段三
- [ ] `plan_task` 把任务拆成 DAG
- [ ] 子任务无环、依赖完整
- [ ] 拆解结果 3-8 个，粒度合适

### 阶段四
- [ ] `AgentTaskWorkflow` 跑完整流程
- [ ] 并行批次正确执行
- [ ] 汇总结果合理

### 阶段五
- [ ] Phoenix UI 里能看到完整 trace
- [ ] 每个 subtask 有独立 span

### 阶段六
- [ ] CLI 提交任务并跟踪
- [ ] kill-and-resume 演示通过
- [ ] 双驱动对比有数据
- [ ] 评测集能跑

---

## 九、关键约束（不要违反）

1. **Temporal Workflow 必须确定性**
   - 不调 LLM（走 Activity）
   - 不调 berth（走 Activity）
   - 不读时钟、不用随机数、不读文件
   - 只有纯计算逻辑

2. **Activity 必须幂等**
   - `run_subtask` 的 sandbox_id 是确定性的（task_id + subtask_id）
   - 重放时如果用同一个 sandbox_id，berth 会复用 workspace

3. **Planning 输出必须校验**
   - Pydantic schema
   - DAG 无环检测
   - 未知依赖检测

4. **Driver 接口必须统一**
   - 所有 driver 实现 `AgentDriver`
   - 返回格式一致
   - 不暴露 driver 内部实现给 workflow

5. **berth 只做执行，不做编排**
   - 不要在 berth 里加 plan / DAG 逻辑
   - berth 只负责"给我代码，我跑给你看"

---

## 十、踩坑提醒

| 坑 | 现象 | 修法 |
|---|---|---|
| Workflow 里调 LLM | 重放时结果不一致，workflow 卡死 | 所有 LLM 调用走 Activity |
| 用了 `time.time()` | 重放时值不同 | 用 `workflow.now()` |
| Activity 不幂等 | 重放时重复执行副作用 | 确定性 ID + 状态检查 |
| Planning JSON 解析失败 | LLM 输出格式不对 | few-shot + 重试 |
| 循环依赖 | 拓扑排序失败 | 校验阶段报错 |
| opencode 装不上 | rootfs 里 node 版本不够 | 用 node 20+ |
| API key 泄漏 | rootfs 里写死 | 环境变量注入 |
| 沙箱清理失败 | berth 里 workspace 残留 | 定期扫描 berth 的 `/volumes` |
| Temporal worker 崩了 | 任务卡住 | 检查 heartbeat 配置 |
| activity 超时 | 长任务中断 | heartbeat + 合理 timeout |

---

## 十一、交付物清单

**代码**：

- [ ] `~/stage3/skipper/` 完整项目
- [ ] Python 包名 `skipper`
- [ ] 支持两个 driver 可切换
- [ ] `README.md` 含架构图、快速开始、示例

**功能**：

- [ ] 用户提交任务 → Temporal → Planning → 并行执行 → 汇总
- [ ] kill worker 后从断点恢复
- [ ] Phoenix 全链路 trace

**演示**：

- [ ] demo 1：简单任务（单子任务）跑通
- [ ] demo 2：复杂任务（多子任务并行）跑通
- [ ] demo 3：kill-and-resume
- [ ] demo 4：双驱动对比

**文档**：

- [ ] `docs/architecture.md`
- [ ] `docs/drivers.md`（如何新增 driver）
- [ ] `docs/planning.md`（planning prompt 设计）

---

## 十二、命名约定

**项目名**：`skipper`
**Python 包名**：`skipper`
**对应项目**：`berth`（沙箱执行服务）
**一句话介绍**：Skipper orchestrates, berth executes.

**命名风格**：

- 目录/模块：小写下划线（`self_loop.py`、`run_subtask`）
- 类名：驼峰（`AgentTaskWorkflow`、`SelfLoopDriver`）
- CLI 命令：`skipper submit "..."`

**未来扩展时，航海隐喻统一**：

- 更上层如果做"多 crew 协作" → `Crew`（船员群）
- 更上层如果做"航线定义" → `Route`
- 不用联想不相关的隐喻

---

## 十三、README 开头模板

```markdown
# Skipper

**Skipper orchestrates, berth executes.**

Skipper 是一个 Agent Runtime，负责任务规划、并行调度、容错恢复。
它不亲自执行任务，而是把子任务分发给可插拔的 agent driver，
driver 通过 [berth](https://github.com/zsgh/berth) 在沙箱里执行。

## 架构

[架构图]

## 核心能力

- **Task Planning**：LLM 拆任务成 DAG
- **Parallel Execution**：无依赖的子任务并行
- **Pluggable Drivers**：支持自研 CodeAct loop、opencode 等
- **Durable Execution**：Temporal 保证，崩了从断点恢复
- **Full Observability**：Phoenix trace 覆盖 planning 到执行

## 快速开始

[install / run]

## 演示

[gif 或视频链接]
```

---

**开始编码。每完成一个阶段，跑一遍验收标准。卡住就把错误贴出来。**
