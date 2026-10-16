"""数据模型：Plan / Subtask / SubtaskResult / TaskResult。

统一定义在 planner.schema，供 drivers 与 temporal 复用（pydantic v2）。
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Complexity = Literal["simple", "medium", "complex"]


class Subtask(BaseModel):
    """规划器拆出来的单个子任务。"""

    id: str = Field(..., description='子任务 id，如 "s1" "s2"')
    description: str = Field(..., description="一段能独立执行的描述，交给一个 agent 完成")
    depends_on: List[str] = Field(default_factory=list, description="依赖的子任务 id 列表")
    preferred_driver: Optional[str] = Field(None, description="偏好使用的 driver（缺省用 workflow 指定的）")
    estimated_complexity: Complexity = "medium"
    can_parallel: bool = Field(True, description="False 时该子任务独占一批（强制串行）")
    metadata: Dict = Field(default_factory=dict)


class Plan(BaseModel):
    """planning 的产物：一个子任务 DAG。"""

    subtasks: List[Subtask]
    reasoning: str = ""
    total_estimated_steps: int = 0


class SubtaskResult(BaseModel):
    """单个子任务的执行结果（所有 driver 统一返回这个形状）。"""

    subtask_id: str
    status: Literal["completed", "failed", "timeout"]
    driver: str
    sandbox_id: str
    output: str
    artifacts: List[str] = Field(default_factory=list)
    duration_ms: int = 0
    error: Optional[str] = None
    trace_id: Optional[str] = None
    # 扩展信息（轮次 / token 用量 / 降级说明等），不参与编排逻辑
    metadata: Dict = Field(default_factory=dict)


class TaskResult(BaseModel):
    """整个任务的最终结果。"""

    task_id: str
    status: Literal["completed", "partial", "failed"]
    plan: Plan
    subtask_results: List[SubtaskResult]
    final_output: str
    total_duration_ms: int = 0
    artifacts: List[str] = Field(default_factory=list)
