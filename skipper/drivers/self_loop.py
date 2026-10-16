"""SelfLoopDriver：自研 CodeAct loop（Day10-11 的 loop 包装成 driver）。

核心循环：
    1. 收到子任务描述（system prompt 说明沙箱规则）
    2. LLM 生成一段 python 代码（```python fenced block```）
    3. 通过 berth /scripts/run 在当前子任务的沙箱里执行
    4. 把 stdout / stderr / exit_code 回填给 LLM
    5. 循环，直到 LLM 输出 TASK_DONE 或达到 max_turns
    6. 返回 SubtaskResult

约定：
- 沙箱内 cwd = /workspace，产物写进 /workspace 才会被 berth 记为 artifacts。
- 所有代码都以 uid 1000 在 overlayfs 沙箱里跑，宿主无副作用。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import List, Optional, Set, Tuple

from skipper import tracing
from skipper.clients.berth import BerthClient, BerthError
from skipper.config import Settings, get_settings
from skipper.drivers.base import AgentDriver
from skipper.planner.schema import SubtaskResult

SELF_LOOP_SYSTEM_PROMPT = """\
你是 Skipper 的沙箱执行 agent。你通过"写代码 → 看结果 → 再写代码"的循环完成一个子任务。

规则：
1. 每轮回复输出**一段** python 代码，放在 ```python 代码块里。代码会在一个 Linux 沙箱里执行。
2. 沙箱当前目录是 /workspace；需要保留的产物（报告/数据/脚本）都写到 /workspace 下。
3. 可以 print 中间结果来观察；输出保持简短（避免打印超长内容）。
4. 看懂报错并修正（traceback 会在下一轮给你）。不要尝试安装系统包。
5. 任务完成后：回复里给出一段简短的最终总结（结论/产出文件路径），并在最后单独一行输出 TASK_DONE。
6. 不要重复已经做成功的步骤；每轮只做增量操作。
7. 代码必须自包含，能用标准库就用标准库。

示例回复（任务：计算 1..100 的和并保存到 sum.txt）：
```python
total = sum(range(1, 101))
with open("/workspace/sum.txt", "w") as f:
    f.write(str(total))
print("sum =", total)
```
TASK_DONE
"""

# 把 agent 生成的代码包一层：chdir 到 /workspace，并以 __main__ 语义执行
_EXEC_WRAPPER = '''\
import os as _os
_os.makedirs("/workspace", exist_ok=True)
_os.chdir("/workspace")
_code = {code_repr}
exec(compile(_code, "/workspace/agent_code.py", "exec"), {{"__name__": "__main__", "__file__": "/workspace/agent_code.py"}})
'''

_CODE_BLOCK_RE = re.compile(r"```(?:python|py)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)
_DONE_SENTINEL = "TASK_DONE"
MAX_OBSERVATION_CHARS = 4000
MAX_HISTORY_OBSERVATIONS = 6  # 只保留最近 N 轮观测，防止上下文爆炸


def extract_code(text: str) -> Optional[str]:
    """从 LLM 回复里取出最后一段代码块。"""
    blocks = _CODE_BLOCK_RE.findall(text or "")
    if not blocks:
        return None
    code = blocks[-1].strip("\n")
    return code.strip() or None


def clean_final_text(text: str) -> str:
    """去掉代码块与 sentinel，留下最终总结。"""
    text = _CODE_BLOCK_RE.sub("", text or "")
    text = text.replace(_DONE_SENTINEL, "")
    return text.strip()


def _last_observation_output(observation: str) -> str:
    """观测串里提取 stdout 部分（模型没写总结时兜底当产出）。"""
    out = []
    capture = False
    for line in (observation or "").splitlines():
        if line.startswith("stdout:"):
            capture = True
            continue
        if line.startswith("stderr:") or line.startswith("exit_code:"):
            capture = False
            continue
        if capture:
            out.append(line)
    text = "\n".join(out).strip()
    return "" if text == "(空)" else text[:2000]


def _truncate(text: str, limit: int = MAX_OBSERVATION_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + f"\n... [截断 {len(text) - limit + 20} 字符]"


def _format_observation(result: dict) -> str:
    stdout = _truncate(result.get("stdout", "")) or "(空)"
    stderr = _truncate(result.get("stderr", "")) or "(空)"
    lines = [
        "Observation:",
        f"exit_code: {result.get('exit_code')}",
        "stdout:",
        stdout,
    ]
    if result.get("stderr"):
        lines += ["stderr:", stderr]
    return "\n".join(lines)


class SelfLoopDriver(AgentDriver):
    def __init__(
        self,
        berth_client: BerthClient,
        llm_client=None,
        *,
        model: Optional[str] = None,
        system_prompt: str = SELF_LOOP_SYSTEM_PROMPT,
        settings: Optional[Settings] = None,
        temperature: float = 0.2,
    ):
        self.berth = berth_client
        self.llm = llm_client
        self.settings = settings or get_settings()
        self.model = model or self.settings.llm_model
        self.system_prompt = system_prompt
        self.temperature = temperature

    def name(self) -> str:
        return "self_loop"

    def describe(self) -> dict:
        return {
            "name": self.name(),
            "description": "自研 CodeAct loop：LLM 生成代码 → berth 沙箱执行 → 结果回填 → 直至 TASK_DONE",
            "requires_llm": True,
            "model": self.model,
            "sandbox_image": "base",
            "max_turns_default": self.settings.self_loop_max_turns,
        }

    # ── 核心循环 ──────────────────────────────────────────

    async def run(
        self,
        task: str,
        sandbox_id: str,
        max_turns: int = 20,
        timeout: int = 600,
    ) -> dict:
        started = time.monotonic()
        deadline = started + max(30, timeout)
        max_turns = max(1, max_turns)

        turns = 0
        llm_tokens = 0
        artifacts: Set[str] = set()
        observations: List[str] = []
        status = "failed"
        error: Optional[str] = None
        final_output = ""
        last_assistant_text = ""

        try:
            if self.llm is None:
                raise RuntimeError("SelfLoopDriver 需要 LLM 客户端（配置 SKIPPER_LLM_* 或 OPENAI_*）")

            messages: List[dict] = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": task},
            ]

            while turns < max_turns:
                if time.monotonic() >= deadline:
                    status = "timeout"
                    error = f"达到整体时间预算 {timeout}s，强制结束"
                    break
                turns += 1

                with tracing.span("driver.self_loop.llm_turn", turn=turns, model=self.model) as sp:
                    resp = await self._chat(messages)
                    if sp is not None and getattr(resp, "usage", None) is not None:
                        usage = resp.usage
                        try:
                            sp.set_attribute(
                                "llm.tokens.total",
                                (usage.prompt_tokens or 0) + (usage.completion_tokens or 0),
                            )
                        except Exception:  # pragma: no cover
                            pass
                llm_tokens += self._extract_tokens(resp)

                text = self._message_text(resp)
                last_assistant_text = text or last_assistant_text
                code = extract_code(text)
                done = _DONE_SENTINEL in (text or "")

                if code:
                    remaining = max(5, int(deadline - time.monotonic()))
                    observation = await self._run_in_sandbox(code, sandbox_id, remaining, artifacts)
                    messages.append({"role": "assistant", "content": text})
                    observations.append(observation)
                    messages.append({"role": "user", "content": observation})
                    self._prune_history(messages, observations)
                elif not done:
                    # 既没有代码也没有 TASK_DONE：提醒一下
                    nudge = (
                        "请输出一段 ```python 代码块来推进任务；"
                        "如果任务已经完成，请在最后单独一行输出 TASK_DONE。"
                    )
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": nudge})

                if done:
                    status = "completed"
                    final_output = clean_final_text(text)
                    break
            else:
                status = "failed"
                error = f"达到 max_turns={max_turns} 仍未完成"

            if status == "completed" and not final_output:
                final_output = (
                    clean_final_text(last_assistant_text)
                    or (observations and _last_observation_output(observations[-1]))
                    or "子任务完成（无文字总结）"
                )
            if status == "timeout" and not final_output:
                final_output = self._partial_output(observations)

        except BerthError as exc:
            status = "failed"
            error = f"berth 不可用: {exc}"
        except Exception as exc:  # LLM / 未知异常：driver 统一兜底成结构化失败
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"

        duration_ms = int((time.monotonic() - started) * 1000)
        result = SubtaskResult(
            subtask_id="",  # 由 run_subtask activity 回填
            status=status,
            driver=self.name(),
            sandbox_id=sandbox_id,
            output=final_output or (error or ""),
            artifacts=sorted(artifacts),
            duration_ms=duration_ms,
            error=error,
            trace_id=tracing.current_trace_id(),
            metadata={"turns": turns, "llm_tokens": llm_tokens, "model": self.model},
        )
        return result.model_dump()

    # ── 内部工具 ──────────────────────────────────────────

    async def _chat(self, messages: List[dict]):
        """一次 LLM 调用（带 1 次重试）。"""
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                return await self.llm.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                )
            except Exception as exc:  # 网络抖动 / 限流
                last_exc = exc
                if attempt == 0:
                    await asyncio.sleep(2)
        raise last_exc  # type: ignore[misc]

    async def _run_in_sandbox(
        self,
        code: str,
        sandbox_id: str,
        remaining_seconds: int,
        artifacts: Set[str],
    ) -> str:
        """在 berth 里执行 agent 代码，返回给 LLM 的 observation。"""
        wrapped = _EXEC_WRAPPER.format(code_repr=repr(code))
        berth_timeout = max(5, min(remaining_seconds, 600))

        with tracing.span("driver.self_loop.sandbox_exec", sandbox_id=sandbox_id) as sp:
            try:
                result = await asyncio.to_thread(
                    self.berth.run_script,
                    code=wrapped,
                    task_id=sandbox_id,
                    timeout=berth_timeout,
                    language="python",
                    image="base",
                )
            except BerthError as exc:
                return f"Observation:\nberth 调用失败: {exc}"

            if sp is not None:
                try:
                    sp.set_attribute("exit_code", int(result.get("exit_code", 0)))
                except Exception:  # pragma: no cover
                    pass

        for artifact in result.get("artifacts") or []:
            artifacts.add(artifact)
        return _format_observation(result)

    @staticmethod
    def _message_text(resp) -> str:
        try:
            content = resp.choices[0].message.content
            return content or ""
        except (AttributeError, IndexError):
            return ""

    @staticmethod
    def _extract_tokens(resp) -> int:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return 0
        try:
            return int((usage.prompt_tokens or 0) + (usage.completion_tokens or 0))
        except Exception:
            return 0

    @staticmethod
    def _prune_history(messages: List[dict], observations: List[str]) -> None:
        """历史只保留最近 MAX_HISTORY_OBSERVATIONS 轮观测。"""
        if len(observations) <= MAX_HISTORY_OBSERVATIONS:
            return
        keep = MAX_HISTORY_OBSERVATIONS
        # messages = [system, task, (assistant, user-observation)*]
        head = messages[:2]
        tail = messages[-keep * 2 :]
        messages[:] = head + tail

    @staticmethod
    def _partial_output(observations: List[str]) -> str:
        if not observations:
            return "（超时，无产出）"
        return "（超时前最后一轮观测）\n" + observations[-1]
