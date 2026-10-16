"""AgentDriver 抽象：所有 driver 的统一接口。

约束：
- 所有 driver 实现 `run()`，返回 SubtaskResult 的 dict 形式（schema 见 planner.schema）。
- workflow / activity 只依赖本接口，不感知 driver 内部实现。
- driver 内部负责「怎么干」；「在哪跑」统一通过 berth。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class AgentDriver(ABC):
    """可插拔的 Agent 执行器。"""

    @abstractmethod
    async def run(
        self,
        task: str,
        sandbox_id: str,
        max_turns: int = 20,
        timeout: int = 600,
    ) -> dict:
        """执行一个子任务，返回 SubtaskResult 的 dict 形式。

        Args:
            task: 子任务描述（planner 的输出，agent 可见的全部上下文）
            sandbox_id: 确定性沙箱标识（task_id + subtask_id）；同一 id 共享
                berth /workspace，重放时能复用产物
            max_turns: agent 循环的最大轮数
            timeout: 整体时间预算（秒）
        """

    @abstractmethod
    def name(self) -> str:
        """driver 名（注册表 key，workflow 通过它选择 driver）。"""

    @abstractmethod
    def describe(self) -> dict:
        """自我描述（给 registry.list() / CLI / API 用）。"""

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name()!r}>"
