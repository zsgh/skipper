"""Temporal Workflow 层：AgentTaskWorkflow / HelloWorkflow。

确定性约束（Temporal 铁律）：
- workflow 里不调 LLM、不调 berth、不读时钟/文件/随机数；
- 所有副作用走 Activity；
- 拓扑排序是纯函数。
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import timedelta
from typing import Dict, List

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from skipper.planner.schema import Plan
    from skipper.temporal import activities


# ── 纯函数：拓扑分批 ─────────────────────────────────────────


def topological_batches(subtasks: List[dict]) -> List[List[dict]]:
    """把子任务 DAG 按依赖分层：同一批（list）内的子任务可并行。

    例: [A, B(dep A), C(dep A), D(dep B,C)] → [[A], [B, C], [D]]

    规则：
    - can_parallel=False 的子任务独占一批（强制串行）；
    - 输入顺序在批内保持稳定；
    - 重复 id / 未知依赖 / 环 → ValueError。

    纯函数：不读文件、不调网络、不用随机数。
    """
    by_id: Dict[str, dict] = {}
    for subtask in subtasks:
        sid = str(subtask["id"])
        if sid in by_id:
            raise ValueError(f"子任务 id 重复: {sid}")
        by_id[sid] = subtask

    indegree: Dict[str, int] = {}
    children: Dict[str, List[str]] = defaultdict(list)
    for subtask in subtasks:
        sid = str(subtask["id"])
        deps = list(dict.fromkeys(subtask.get("depends_on") or []))  # 去重且保序
        unknown = [d for d in deps if d not in by_id]
        if unknown:
            raise ValueError(f"子任务 {sid} 依赖了未知 id: {unknown}")
        indegree[sid] = len(deps)
        for dep in deps:
            children[dep].append(sid)

    ready: List[str] = [str(s["id"]) for s in subtasks if indegree[str(s["id"])] == 0]
    batches: List[List[dict]] = []

    while ready:
        parallel = [sid for sid in ready if by_id[sid].get("can_parallel", True)]
        serial = [sid for sid in ready if not by_id[sid].get("can_parallel", True)]

        if parallel:
            batch_ids = parallel
            next_ready = list(serial)
        else:
            # 只剩串行子任务：一批只放一个
            batch_ids = serial[:1]
            next_ready = serial[1:]

        batches.append([by_id[sid] for sid in batch_ids])

        for sid in batch_ids:
            for child in children[sid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    next_ready.append(child)
        ready = next_ready

    if sum(len(b) for b in batches) != len(subtasks):
        remaining = sorted(i for i, d in indegree.items() if d > 0)
        raise ValueError(f"依赖存在环: {remaining}")
    return batches


# ── Workflow ────────────────────────────────────────────────


@workflow.defn
class HelloWorkflow:
    """阶段一的最小 workflow：跑一段代码，验证 Temporal ↔ berth 链路。"""

    @workflow.run
    async def run(self, code: str = "print('hello')") -> dict:
        return await workflow.execute_activity(
            activities.run_hello,
            code,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )


@workflow.defn
class AgentTaskWorkflow:
    """用户任务 → planning → 分批并行执行 → 汇总。

    断点恢复：Temporal event history 保证 worker 崩溃后从断点继续，
    已完成的 activity 不会重跑。
    """

    def __init__(self) -> None:
        self._state: dict = {
            "phase": "pending",
            "plan": None,
            "subtask_results": [],
            "skipped": [],
            "result": None,
        }

    @workflow.query
    def get_state(self) -> dict:
        """进度查询（CLI / API 用来展示 Plan 与执行进度）。"""
        return {
            "phase": self._state["phase"],
            "plan": self._state["plan"],
            "subtask_results": self._state["subtask_results"],
            "skipped": self._state["skipped"],
            "result": self._state["result"],
        }

    @workflow.run
    async def run(self, task: str, driver_name: str = "self_loop") -> dict:
        task_id = workflow.info().workflow_id
        started_at = workflow.now()

        # 1. Planning（LLM 调用是副作用 → 必须在 Activity 里）
        plan_dict = await workflow.execute_activity(
            activities.plan_task,
            task,
            start_to_close_timeout=timedelta(seconds=180),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        plan = Plan.model_validate(plan_dict)
        self._state["plan"] = plan_dict
        self._state["phase"] = "planned"
        workflow.logger.info(
            "plan ready: %d subtasks, %d batches",
            len(plan.subtasks),
            len(topological_batches([s.model_dump() for s in plan.subtasks])),
        )

        # 2. 拓扑排序 → 分批并行执行
        batches = topological_batches([s.model_dump() for s in plan.subtasks])
        all_results: List[dict] = []
        executed: set = set()

        for batch in batches:
            results_by_id = {r["subtask_id"]: r for r in all_results}
            batch_calls = []
            for subtask in batch:
                # 依赖的产出（来自 event history，确定性）→ 传给下游做数据接力
                upstream = [
                    results_by_id[dep]
                    for dep in (subtask.get("depends_on") or [])
                    if dep in results_by_id
                ]
                batch_calls.append(
                    workflow.execute_activity(
                        activities.run_subtask,
                        args=[subtask, driver_name, task_id, upstream],
                        start_to_close_timeout=timedelta(minutes=30),
                        heartbeat_timeout=timedelta(minutes=2),
                        retry_policy=RetryPolicy(
                            maximum_attempts=2,
                            initial_interval=timedelta(seconds=5),
                            backoff_coefficient=2.0,
                        ),
                    )
                )
            self._state["phase"] = "running"
            batch_results = await asyncio.gather(*batch_calls)
            all_results.extend(batch_results)
            executed.update(r["subtask_id"] for r in batch_results)
            self._state["subtask_results"] = list(all_results)

            failed = [r["subtask_id"] for r in batch_results if r.get("status") != "completed"]
            if failed:
                workflow.logger.warning("batch 有子任务失败 %s，停止调度后续批次", failed)
                break

        # 记录被跳过的子任务（失败后未调度）
        self._state["skipped"] = [s.id for s in plan.subtasks if s.id not in executed]

        # 3. 汇总
        self._state["phase"] = "aggregating"
        final = await workflow.execute_activity(
            activities.aggregate_results,
            args=[task, plan.model_dump(), all_results],
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        final["task_id"] = task_id
        final["total_duration_ms"] = int((workflow.now() - started_at).total_seconds() * 1000)
        self._state["phase"] = "done"
        self._state["result"] = final
        return final
