"""Workflow 层测试：拓扑排序纯函数 + Plan 解析校验 + WorkflowEnvironment 端到端（stub activities）。"""
from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from skipper.planner.parse import PlanParseError, parse_plan
from skipper.planner.schema import Plan
from skipper.temporal.workflows import AgentTaskWorkflow, topological_batches


# ── 拓扑分批（纯函数）────────────────────────────────────────


def _s(sid, deps=None, can_parallel=True):
    return {"id": sid, "description": f"task {sid}", "depends_on": deps or [], "can_parallel": can_parallel}


def test_batches_chain():
    assert [[s["id"] for s in b] for b in topological_batches([_s("a"), _s("b", ["a"]), _s("c", ["b"])])] == [
        ["a"],
        ["b"],
        ["c"],
    ]


def test_batches_fan_out():
    result = topological_batches([_s("a"), _s("b", ["a"]), _s("c", ["a"]), _s("d", ["a"])])
    assert [[s["id"] for s in b] for b in result] == [["a"], ["b", "c", "d"]]


def test_batches_diamond():
    result = topological_batches([_s("a"), _s("b", ["a"]), _s("c", ["a"]), _s("d", ["b", "c"])])
    assert [[s["id"] for s in b] for b in result] == [["a"], ["b", "c"], ["d"]]


def test_batches_serial_subtask_gets_own_batch():
    result = topological_batches([_s("a"), _s("b"), _s("c", can_parallel=False)])
    assert [[s["id"] for s in b] for b in result] == [["a", "b"], ["c"]]


def test_batches_single_and_empty():
    assert [[s["id"] for s in b] for b in topological_batches([_s("only")])] == [["only"]]
    assert topological_batches([]) == []


def test_batches_cycle_raises():
    with pytest.raises(ValueError, match="环"):
        topological_batches([_s("a", ["b"]), _s("b", ["a"])])


def test_batches_unknown_dep_raises():
    with pytest.raises(ValueError, match="未知 id"):
        topological_batches([_s("a", ["nope"])])


def test_batches_duplicate_id_raises():
    with pytest.raises(ValueError, match="重复"):
        topological_batches([_s("a"), _s("a")])


# ── Plan 解析 / 校验 ─────────────────────────────────────────


def test_parse_plan_fenced_json():
    raw = """好的，这是计划：
```json
{"subtasks": [{"id": "s1", "description": "分析"}, {"id": "s2", "description": "实现", "depends_on": ["s1"]}],
 "reasoning": "先分析再实现"}
```
"""
    plan = parse_plan(raw)
    assert isinstance(plan, Plan)
    assert [s.id for s in plan.subtasks] == ["s1", "s2"]
    assert plan.subtasks[1].depends_on == ["s1"]


def test_parse_plan_plain_json_with_chatter():
    raw = '前置说明。{"subtasks": [{"id": "s1", "description": "一步搞定"}], "reasoning": ""} 收尾。'
    plan = parse_plan(raw)
    assert len(plan.subtasks) == 1


def test_parse_plan_rejects_cycle():
    raw = '{"subtasks": [{"id": "a", "description": "x", "depends_on": ["b"]}, {"id": "b", "description": "y", "depends_on": ["a"]}]}'
    with pytest.raises(PlanParseError, match="环"):
        parse_plan(raw)


def test_parse_plan_rejects_unknown_dep():
    raw = '{"subtasks": [{"id": "a", "description": "x", "depends_on": ["ghost"]}]}'
    with pytest.raises(PlanParseError, match="未知 id"):
        parse_plan(raw)


def test_parse_plan_rejects_duplicate_ids():
    raw = '{"subtasks": [{"id": "a", "description": "x"}, {"id": "a", "description": "y"}]}'
    with pytest.raises(PlanParseError, match="重复"):
        parse_plan(raw)


def test_parse_plan_rejects_garbage():
    with pytest.raises(PlanParseError):
        parse_plan("我觉得这个任务很简单，不需要拆解。")


def test_parse_plan_rejects_empty():
    with pytest.raises(PlanParseError):
        parse_plan('{"subtasks": []}')


# ── 上游产物接力（unit：纯函数部分）──────────────────────────


def test_upstream_context_summarizes_deps():
    from skipper.temporal.activities import _build_upstream_context

    context = _build_upstream_context(
        [
            {
                "subtask_id": "s1",
                "driver": "self_loop",
                "output": "写好了 a.txt",
                "artifacts": ["a.txt"],
            }
        ]
    )
    assert "s1" in context
    assert "a.txt" in context
    assert "写好了 a.txt" in context


def test_safe_artifact_name_blocks_escaping_paths():
    from skipper.temporal.activities import _safe_artifact_name

    assert _safe_artifact_name("a.txt") == "a.txt"
    assert _safe_artifact_name("sub/b.txt") == "sub/b.txt"
    assert _safe_artifact_name("/etc/passwd") is None
    assert _safe_artifact_name("../../etc/passwd") is None
    assert _safe_artifact_name("a//b") is None


# ── WorkflowEnvironment（时间跳跃 + stub activities）─────────

PLAN_DICT = {
    "subtasks": [
        {"id": "s1", "description": "先做一步", "depends_on": []},
        {"id": "s2", "description": "并行 A", "depends_on": ["s1"]},
        {"id": "s3", "description": "并行 B", "depends_on": ["s1"]},
        {"id": "s4", "description": "最后汇总", "depends_on": ["s2", "s3"]},
    ],
    "reasoning": "s2/s3 可并行，s4 收尾",
    "total_estimated_steps": 4,
}

STATE: dict = {}


@activity.defn(name="plan_task")
async def stub_plan_task(task: str) -> dict:
    STATE["planned_for"] = task
    return {
        "subtasks": [dict(s) for s in PLAN_DICT["subtasks"]],
        "reasoning": PLAN_DICT["reasoning"],
        "total_estimated_steps": 4,
    }


@activity.defn(name="run_subtask")
async def stub_run_subtask(subtask: dict, driver_name: str, task_id: str, upstream_results: list = None) -> dict:
    STATE.setdefault("executed", []).append(subtask["id"])
    STATE.setdefault("upstream_seen", {})[subtask["id"]] = [
        r["subtask_id"] for r in (upstream_results or [])
    ]
    status = "failed" if subtask["id"] in STATE.get("fail_ids", []) else "completed"
    return {
        "subtask_id": subtask["id"],
        "status": status,
        "driver": driver_name,
        "sandbox_id": f"{task_id}-{subtask['id']}",
        "output": f"done {subtask['id']}",
        "artifacts": [f"{subtask['id']}.txt"],
        "duration_ms": 5,
        "error": None if status == "completed" else "boom",
        "metadata": {},
    }


@activity.defn(name="aggregate_results")
async def stub_aggregate(task: str, plan: dict, results: list) -> dict:
    STATE["aggregated_with"] = [r["subtask_id"] for r in results]
    all_ok = all(r["status"] == "completed" for r in results) and bool(results)
    return {
        "task_id": "",
        "status": "completed" if all_ok else "partial",
        "plan": plan,
        "subtask_results": results,
        "final_output": "汇总完成",
        "total_duration_ms": 0,
        "artifacts": sorted({a for r in results for a in r["artifacts"]}),
    }


async def _run_workflow(task="测试任务", driver="self_loop"):
    env = await WorkflowEnvironment.start_time_skipping()
    async with env:
        queue = f"test-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskWorkflow],
            activities=[stub_plan_task, stub_run_subtask, stub_aggregate],
        ):
            return await env.client.execute_workflow(
                AgentTaskWorkflow.run,
                args=[task, driver],
                id=f"wf-{uuid.uuid4().hex[:12]}",
                task_queue=queue,
            )


async def test_workflow_parallel_batches_and_aggregation():
    STATE.clear()
    result = await _run_workflow()
    assert result["status"] == "completed"
    assert result["task_id"]  # workflow 回填
    # s2/s3 同一批并行，执行顺序不保证；但 s1 必须最早、s4 必须最后
    executed = STATE["executed"]
    assert set(executed) == {"s1", "s2", "s3", "s4"}
    assert executed[0] == "s1" and executed[-1] == "s4"
    assert set(STATE["aggregated_with"]) == {"s1", "s2", "s3", "s4"}
    # 依赖的上游结果被传给了下游 activity
    assert STATE["upstream_seen"]["s2"] == ["s1"]
    assert set(STATE["upstream_seen"]["s4"]) == {"s2", "s3"}
    # 汇总结果里带上每个子任务的 artifacts
    assert set(result["artifacts"]) == {"s1.txt", "s2.txt", "s3.txt", "s4.txt"}


async def test_workflow_stops_after_failed_batch():
    STATE.clear()
    STATE["fail_ids"] = ["s2"]  # s2 失败 → s4 不应该被调度
    result = await _run_workflow()
    assert result["status"] == "partial"
    assert set(STATE["executed"]) == {"s1", "s2", "s3"}
    assert set(STATE["aggregated_with"]) == {"s1", "s2", "s3"}


async def test_workflow_query_reports_plan():
    STATE.clear()
    env = await WorkflowEnvironment.start_time_skipping()
    async with env:
        queue = f"test-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskWorkflow],
            activities=[stub_plan_task, stub_run_subtask, stub_aggregate],
        ):
            handle = await env.client.start_workflow(
                AgentTaskWorkflow.run,
                args=["查询测试", "self_loop"],
                id=f"wf-{uuid.uuid4().hex[:12]}",
                task_queue=queue,
            )
            await handle.result()
            state = await handle.query(AgentTaskWorkflow.get_state)
            assert state["phase"] == "done"
            assert state["plan"]["subtasks"][0]["id"] == "s1"
            assert len(state["subtask_results"]) == 4
            assert state["skipped"] == []
            assert state["result"]["final_output"] == "汇总完成"
