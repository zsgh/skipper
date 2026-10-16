"""`skipper` 命令行入口。

    skipper submit "复杂任务..." --driver self_loop
    skipper hello "print('hello')"
    skipper status agent-task-xxxx
    skipper result agent-task-xxxx
    skipper drivers
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from skipper.config import get_settings


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


async def _cmd_submit(args) -> int:
    from skipper.temporal.client import submit_task

    info = await submit_task(args.task, args.driver, task_id=args.id)
    print(f"Task ID: {info['task_id']}")
    print(f"Driver:  {info['driver']}")
    print(f"Tracking: {info['tracking_url']}")
    if args.wait:
        from skipper.temporal.client import get_result

        result = await get_result(info["task_id"])
        print(f"\n最终输出:\n{result.get('final_output', '')}")
    return 0


async def _cmd_hello(args) -> int:
    from skipper.temporal.client import submit_hello

    info = await submit_hello(args.code)
    print(f"Task ID: {info['task_id']}")
    result = await info["handle"].result()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


async def _cmd_status(args) -> int:
    from skipper.temporal.client import query_state

    state = await query_state(args.task_id)
    print(f"phase: {state.get('phase')}")
    if state.get("plan"):
        print("plan:")
        for s in state["plan"].get("subtasks", []):
            deps = ",".join(s.get("depends_on") or []) or "-"
            print(f"  {s['id']}: {s['description']} (depends_on: [{deps}])")
    for r in state.get("subtask_results", []):
        print(f"  [{r['status']}] {r['subtask_id']} (driver={r['driver']}, {r.get('duration_ms', 0)}ms)")
    if state.get("skipped"):
        print(f"skipped: {state['skipped']}")
    if state.get("result"):
        print("result:")
        _print_json(state["result"].get("final_output"))
    return 0


async def _cmd_result(args) -> int:
    from skipper.temporal.client import get_result

    result = await get_result(args.task_id, timeout=args.timeout)
    _print_json(result)
    return 0


def _cmd_drivers(args) -> int:
    from skipper.clients.berth import BerthClient
    from skipper.drivers import DriverRegistry

    settings = get_settings()
    registry = DriverRegistry(BerthClient(settings.berth_base_url), None, settings=settings)
    _print_json(registry.list())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skipper", description="Skipper orchestrates, berth executes.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit", help="提交任务到 Temporal")
    p_submit.add_argument("task")
    p_submit.add_argument("--driver", default="self_loop", choices=["self_loop", "opencode"])
    p_submit.add_argument("--id", default=None)
    p_submit.add_argument("--wait", action="store_true")
    p_submit.set_defaults(func=lambda args: asyncio.run(_cmd_submit(args)))

    p_hello = sub.add_parser("hello", help="最小冒烟（HelloWorkflow）")
    p_hello.add_argument("code", nargs="?", default="print('hello')")
    p_hello.set_defaults(func=lambda args: asyncio.run(_cmd_hello(args)))

    p_status = sub.add_parser("status", help="查询任务进度")
    p_status.add_argument("task_id")
    p_status.set_defaults(func=lambda args: asyncio.run(_cmd_status(args)))

    p_result = sub.add_parser("result", help="等待并打印任务结果")
    p_result.add_argument("task_id")
    p_result.add_argument("--timeout", type=float, default=None, help="等待秒数（默认无限）")
    p_result.set_defaults(func=lambda args: asyncio.run(_cmd_result(args)))

    p_drivers = sub.add_parser("drivers", help="列出可用 driver")
    p_drivers.set_defaults(func=_cmd_drivers)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
