"""提交任务的客户端（HTTP API / CLI 都走这里）。

    python -m skipper.temporal.client "给 /workspace 写一个 hello.txt"
    python -m skipper.temporal.client --hello "print('hello')"
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import timedelta
from typing import Optional

from temporalio.client import Client, WorkflowHandle

from skipper.config import Settings, get_settings
from skipper.temporal.workflows import AgentTaskWorkflow, HelloWorkflow

DEFAULT_EXECUTION_TIMEOUT = timedelta(hours=3)


async def get_client(settings: Optional[Settings] = None) -> Client:
    settings = settings or get_settings()
    return await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)


def new_task_id() -> str:
    return f"agent-task-{uuid.uuid4().hex[:12]}"


async def submit_task(
    task: str,
    driver: str = "self_loop",
    *,
    task_id: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> dict:
    """提交一个用户任务，返回 {task_id, run_id, tracking_url}。"""
    settings = settings or get_settings()
    client = await get_client(settings)
    task_id = task_id or new_task_id()
    handle = await client.start_workflow(
        AgentTaskWorkflow.run,
        args=[task, driver],
        id=task_id,
        task_queue=settings.temporal_task_queue,
        execution_timeout=DEFAULT_EXECUTION_TIMEOUT,
    )
    return {
        "task_id": task_id,
        "run_id": handle.result_run_id,
        "driver": driver,
        "tracking_url": f"{settings.temporal_ui_base}/namespaces/{settings.temporal_namespace}/workflows/{task_id}",
    }


async def submit_hello(code: str = "print('hello')", *, settings: Optional[Settings] = None) -> dict:
    """最小冒烟：HelloWorkflow 在 berth 里跑一段代码。"""
    settings = settings or get_settings()
    client = await get_client(settings)
    task_id = f"hello-{uuid.uuid4().hex[:12]}"
    handle = await client.start_workflow(
        HelloWorkflow.run,
        code,
        id=task_id,
        task_queue=settings.temporal_task_queue,
        execution_timeout=timedelta(minutes=10),
    )
    return {"task_id": task_id, "run_id": handle.result_run_id, "handle": handle}


async def _get_handle(task_id: str, settings: Optional[Settings] = None) -> WorkflowHandle:
    settings = settings or get_settings()
    client = await get_client(settings)
    return client.get_workflow_handle(task_id)


async def query_state(task_id: str, *, settings: Optional[Settings] = None) -> dict:
    """查询 workflow 进度（plan / subtask 结果）。"""
    handle = await _get_handle(task_id, settings)
    return await handle.query(AgentTaskWorkflow.get_state)


async def get_result(task_id: str, *, timeout: Optional[float] = None, settings: Optional[Settings] = None) -> dict:
    """等待并取回最终结果（timeout 秒后抛 asyncio.TimeoutError）。"""
    handle = await _get_handle(task_id, settings)
    if timeout is None:
        return await handle.result()
    return await asyncio.wait_for(handle.result(), timeout=timeout)


# ── CLI ─────────────────────────────────────────────────────


def _print_plan(plan: dict) -> None:
    if not plan:
        print("(plan 还没生成)")
        return
    for s in plan.get("subtasks", []):
        deps = ",".join(s.get("depends_on") or []) or "-"
        driver = s.get("preferred_driver") or ""
        print(f"  {s['id']}: {s['description']}  (depends_on: [{deps}]{' driver=' + driver if driver else ''})")


async def _cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Skipper: submit a task to Temporal")
    parser.add_argument("task", nargs="?", help="任务描述（--hello 时为要跑的代码）")
    parser.add_argument("--driver", default="self_loop", help="self_loop | opencode")
    parser.add_argument("--id", dest="task_id", default=None, help="自定义 workflow id")
    parser.add_argument("--wait", action="store_true", help="等待任务完成并打印结果")
    parser.add_argument("--hello", action="store_true", help="跑最小 HelloWorkflow（阶段一冒烟）")
    args = parser.parse_args(argv)

    settings = get_settings()

    if args.hello:
        code = args.task or "print('hello')"
        info = await submit_hello(code, settings=settings)
        handle = info["handle"]
        result = await handle.result()
        print(f"Task ID: {info['task_id']}")
        print(f"HelloWorkflow 完成: exit_code={result.get('exit_code')} stdout={result.get('stdout', '').strip()!r}")
        return 0 if result.get("exit_code") == 0 else 1

    if not args.task:
        parser.error("需要任务描述（或 --hello）")

    info = await submit_task(args.task, args.driver, task_id=args.task_id, settings=settings)
    print(f"Task ID: {info['task_id']}")
    print(f"Driver:  {info['driver']}")
    print(f"Tracking: {info['tracking_url']}")

    if args.wait:
        result = await get_result(info["task_id"], settings=settings)
        print(f"Plan:")
        _print_plan(result.get("plan", {}))
        print(f"\nStatus: {result['status']}  (总耗时 {result.get('total_duration_ms', 0)} ms)")
        print(f"\n最终输出:\n{result.get('final_output', '')}")
        return 0 if result["status"] in ("completed", "partial") else 1

    # 默认：等几秒展示 Plan（不阻塞到任务结束）
    handle = await _get_handle(info["task_id"], settings)
    plan = None
    for _ in range(30):
        state = await handle.query(AgentTaskWorkflow.get_state)
        if state.get("plan"):
            plan = state["plan"]
            break
        await asyncio.sleep(1)
    print("Plan:")
    _print_plan(plan)
    if plan is None:
        print("  (plan 仍在生成中，去 Temporal UI 看进度)")
    return 0


def main(argv=None) -> int:
    try:
        return asyncio.run(_cli(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
