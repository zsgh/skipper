#!/usr/bin/env python3
"""CLI：提交任务并跟踪。

    python scripts/submit_task.py "给项目里的 5 个模块各生成一个 pytest 测试" --driver self_loop
    python scripts/submit_task.py "..." --wait          # 等任务跑完再退出
    python scripts/submit_task.py --hello "print('hello')"  # 阶段一冒烟
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 免安装也能跑

from skipper.temporal.client import (  # noqa: E402
    get_result,
    query_state,
    submit_hello,
    submit_task,
)
from skipper.temporal.workflows import AgentTaskWorkflow  # noqa: E402


def print_plan(plan: dict) -> None:
    if not plan:
        print("  (plan 仍在生成中……)")
        return
    for s in plan.get("subtasks", []):
        deps = ", ".join(s.get("depends_on") or []) or "-"
        driver = f" driver={s['preferred_driver']}" if s.get("preferred_driver") else ""
        print(f"  {s['id']}: {s['description']} (depends_on: [{deps}]{driver})")


async def run(args: argparse.Namespace) -> int:
    if args.hello:
        info = await submit_hello(args.task or "print('hello')")
        print(f"Task ID: {info['task_id']}")
        result = await info["handle"].result()
        ok = result.get("exit_code") == 0
        print(f"HelloWorkflow: exit_code={result.get('exit_code')} stdout={result.get('stdout', '').strip()!r}")
        return 0 if ok else 1

    if not args.task:
        print("需要任务描述（或 --hello）", file=sys.stderr)
        return 2

    info = await submit_task(args.task, args.driver, task_id=args.task_id)
    print(f"Task ID: {info['task_id']}")
    print(f"Driver:  {info['driver']}")
    print(f"Tracking: {info['tracking_url']}")

    if args.wait:
        result = await get_result(info["task_id"])
        print("\nPlan:")
        print_plan(result.get("plan", {}))
        print(f"\nStatus: {result['status']}（总耗时 {result.get('total_duration_ms', 0)} ms）")
        print(f"\n最终输出:\n{result.get('final_output', '')}")
        return 0 if result["status"] in ("completed", "partial") else 1

    # 等 planning 完成，把 DAG 打出来（最多 ~60s）
    from skipper.temporal.client import _get_handle  # 内部工具，CLI 自用

    handle = await _get_handle(info["task_id"])
    plan = None
    for _ in range(60):
        state = await handle.query(AgentTaskWorkflow.get_state)
        plan = state.get("plan")
        if plan:
            break
        await asyncio.sleep(1)
    print("\nPlan:")
    print_plan(plan or {})
    print(f"\n（任务继续在后台执行，用 temporal UI 或 `skipper status {info['task_id']}` 跟踪）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task", nargs="?", help="任务描述（--hello 时为代码）")
    parser.add_argument("--driver", default="self_loop", choices=["self_loop", "opencode"])
    parser.add_argument("--id", dest="task_id", default=None, help="自定义 workflow id")
    parser.add_argument("--wait", action="store_true", help="等待完成并打印最终结果")
    parser.add_argument("--hello", action="store_true", help="跑 HelloWorkflow 冒烟")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
