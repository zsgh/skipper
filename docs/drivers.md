# Driver：怎么新增一个可插拔 Agent 执行器

## 接口

所有 driver 实现 `skipper.drivers.base.AgentDriver`（`skipper/drivers/base.py`）：

```python
class AgentDriver(ABC):
    @abstractmethod
    async def run(self, task: str, sandbox_id: str,
                  max_turns: int = 20, timeout: int = 600) -> dict:
        """执行一个子任务，返回 SubtaskResult 的 dict 形式。"""

    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def describe(self) -> dict: ...
```

约定：

- **返回格式统一**：必须能通过 `SubtaskResult` 的校验（`status ∈ completed/failed/timeout`、
  `output`、`artifacts`、`duration_ms`…）。
- `subtask_id` 由 `run_subtask` activity 回填，driver 填空字符串即可。
- **不许把内部实现暴露给 workflow**：workflow 只知道 driver 的名字。
- 沙箱执行统一走 berth（`skipper.clients.berth.BerthClient`），不自己开沙箱。
- 异常不要抛给上层：driver 自己兜底成 `status="failed"` 的结果（activity 还有一层兜底）。

## 内置的两个 driver

### SelfLoopDriver（`skipper/drivers/self_loop.py`）

自研 CodeAct loop，最小依赖：

```
LLM 生成 python 代码 → berth /scripts/run 执行 → stdout/stderr 回填 → 循环
         ↑                                                    │
         └────────── 直到输出 TASK_DONE 或 max_turns ──────────┘
```

- 沙箱 `image="base"`；agent 代码被包一层 `chdir("/workspace") + exec`，所以相对路径、子进程都在
  `/workspace` 下工作，产物天然被 berth 记成 artifacts。
- 每个子任务一次 `run_subtask`，内部最多 `max_turns` 轮（默认 20）；token 用量记在
  `metadata.llm_tokens`，轮数记在 `metadata.turns`。
- 模型没写总结时，用最后一轮的 stdout 兜底当产出。

### OpencodeDriver（`skipper/drivers/opencode.py`）

把 [opencode](https://opencode.ai) 塞进沙箱跑非交互模式：

```
Skipper → berth /scripts/run(image="opencode")
    → python 包装器（git init + chdir /workspace + 注入环境变量）
    → opencode run --dir /workspace --model skipper/<model> --auto "<子任务>"
```

- **前置**：`scripts/build_opencode_rootfs.sh` 在 berth 宿主机上构建 image 名为 `opencode`
  的 rootfs（Debian bookworm + Node 20 + `npm i -g opencode-ai`）。
- **密钥不落盘**：provider 配置通过 `OPENCODE_CONFIG_CONTENT`（内联 JSON）注入，
  API key 用 `{env:SKIPPER_LLM_API_KEY}` 占位，真实值随这一次沙箱执行的环境变量传入。
- provider 用 `@ai-sdk/openai-compatible` 自定义（任何 OpenAI 兼容端点都能接）。
- 两个沙箱适配点（见代码注释）：
  1. opencode 靠 git 探测项目根，而 berth 的 `/proc/self/mountinfo` 会暴露宿主路径 →
     driver 先 `git init /workspace`；
  2. 显式 `--dir /workspace`。
- 资源：`SKIPPER_OPENCODE_MEMORY_MB`（默认 1024）——Bun 运行时比普通脚本吃内存，
  berth 默认 256MB 会直接 OOM。
- opencode 自己的运行时文件（`.git/`、`.npm/`、`.local/`、`opencode.db*`…）会被
  `skipper.artifacts.filter_task_artifacts` 过滤掉，不算任务产物。

## 新增一个 driver（三步）

```python
# 1) skipper/drivers/my_driver.py
from skipper.drivers.base import AgentDriver
from skipper.planner.schema import SubtaskResult

class MyDriver(AgentDriver):
    def __init__(self, berth_client, *, settings=None):
        self.berth = berth_client

    def name(self) -> str:
        return "my_driver"

    def describe(self) -> dict:
        return {"name": self.name(), "description": "……", "requires_llm": False}

    async def run(self, task, sandbox_id, max_turns=20, timeout=600) -> dict:
        result = await self.berth.arun_script(
            code=f"print({task!r})", task_id=sandbox_id, timeout=min(timeout, 600),
        )
        return SubtaskResult(
            subtask_id="", status="completed" if result["exit_code"] == 0 else "failed",
            driver=self.name(), sandbox_id=sandbox_id,
            output=result["stdout"], artifacts=result.get("artifacts", []),
            duration_ms=int(result["duration"] * 1000),
        ).model_dump()
```

```python
# 2) 注册（skipper/drivers/registry.py 的 __init__ 里 register 一下，
#    或者外部通过 DriverRegistry(..., drivers=[MyDriver(berth)]) 注入）
registry.register(MyDriver(berth))
```

```python
# 3) 用起来
from skipper.temporal.client import submit_task
await submit_task("你的任务", driver="my_driver")
```

给子任务单独指定 driver：planning 输出里的 `preferred_driver` 字段
（不存在时自动回退到 workflow 指定的 driver，并在结果 metadata 里说明）。

## 测试

- 单元测试见 `tests/test_drivers.py`：用 FakeLLM / FakeBerth 断言 driver 的
  「命令构造、状态映射、错误兜底、schema 合规」。
- 集成测试见 `tests/test_integration.py`（真实 berth / Temporal / LLM）。
