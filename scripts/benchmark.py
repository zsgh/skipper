#!/usr/bin/env python3
"""评测集：10 个任务 × N 次，统计成功率 / 平均耗时 / 平均 token。

    python scripts/benchmark.py --runs 3 --driver self_loop
    python scripts/benchmark.py --runs 1 --driver both --json results/bm.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skipper.temporal.client import get_result, submit_task  # noqa: E402

# 10 个沙箱友好的任务（可验证、无真实账号依赖）
TASKS = [
    "计算 1 到 100 的和并打印结果，然后保存到 /workspace/sum.txt。",
    "生成 10 个 [0,100) 的随机整数，打印排序后的结果，并保存到 /workspace/random.txt。",
    "统计文本 'the quick brown fox jumps over the lazy dog the fox' 中每个单词出现的次数，打印出现次数最多的前 3 个。",
    "打印斐波那契数列的前 15 项，并保存到 /workspace/fib.txt。",
    "写 FizzBuzz（1 到 30），打印结果并保存到 /workspace/fizzbuzz.txt。",
    "生成一个 CSV 文件 /workspace/people.csv，包含 3 行人员数据（name,age,city），并打印文件内容。",
    "计算 2 的 100 次方，保存到 /workspace/power.txt，并打印它有多少位数字。",
    "实现一个质数判断函数，打印 100 以内的所有质数。",
    "把字符串 'hello world' 反转并打印结果，同时保存到 /workspace/reversed.txt。",
    "生成 /workspace/config.json，内容为 {\"name\": \"skipper\", \"version\": 1}，并打印文件内容。",
]


def total_tokens(result: dict) -> int:
    tokens = 0
    for r in result.get("subtask_results", []):
        try:
            tokens += int((r.get("metadata") or {}).get("llm_tokens", 0) or 0)
        except (TypeError, ValueError):
            pass
    return tokens


async def run_once(task: str, driver: str, timeout: float) -> dict:
    started = time.monotonic()
    info = await submit_task(task, driver, task_id=None)
    try:
        result = await get_result(info["task_id"], timeout=timeout)
        status = result.get("status", "failed")
        tokens = total_tokens(result)
        duration = result.get("total_duration_ms") or int((time.monotonic() - started) * 1000)
    except Exception as exc:
        status = "error"
        tokens = 0
        duration = int((time.monotonic() - started) * 1000)
        result = {"error": str(exc)}
    return {
        "task_id": info["task_id"],
        "task": task,
        "driver": driver,
        "status": status,
        "duration_ms": duration,
        "tokens": tokens,
        "result": result if isinstance(result, dict) else {},
    }


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if r["status"] in ("completed", "partial")]
    return {
        "runs": len(rows),
        "success_rate": round(len(ok) / len(rows), 3) if rows else 0.0,
        "avg_duration_ms": int(statistics.mean(r["duration_ms"] for r in rows)) if rows else 0,
        "avg_tokens": int(statistics.mean(r["tokens"] for r in rows)) if rows else 0,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Skipper benchmark")
    parser.add_argument("--runs", type=int, default=3, help="每个任务跑几次")
    parser.add_argument("--driver", default="self_loop", choices=["self_loop", "opencode", "both"])
    parser.add_argument("--timeout", type=float, default=900, help="单个任务等待上限（秒）")
    parser.add_argument("--tasks", type=int, default=len(TASKS), help="只跑前 N 个任务")
    parser.add_argument("--json", dest="json_path", default=None, help="结果写到哪里")
    args = parser.parse_args()

    drivers = ["self_loop", "opencode"] if args.driver == "both" else [args.driver]
    tasks = TASKS[: args.tasks]

    rows: list[dict] = []
    print(f"benchmark: {len(tasks)} tasks × {args.runs} runs × drivers={drivers}")
    for driver in drivers:
        for i, task in enumerate(tasks, 1):
            for run in range(1, args.runs + 1):
                row = await run_once(task, driver, args.timeout)
                rows.append(row)
                print(
                    f"[{driver}] task {i}/{len(tasks)} run {run}/{args.runs}: "
                    f"{row['status']:<10} {row['duration_ms']:>7} ms  {row['tokens']:>6} tokens"
                )

    print("\n== 汇总 ==")
    by_driver: dict[str, list[dict]] = {}
    for row in rows:
        by_driver.setdefault(row["driver"], []).append(row)
    print(f"{'driver':<10} {'runs':>5} {'success':>8} {'avg_dur':>10} {'avg_tokens':>11}")
    summary = {}
    for driver, driver_rows in by_driver.items():
        s = summarize(driver_rows)
        summary[driver] = s
        print(
            f"{driver:<10} {s['runs']:>5} {s['success_rate']:>8.2%} "
            f"{s['avg_duration_ms']:>8} ms {s['avg_tokens']:>11}"
        )

    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "runs_per_task": args.runs,
        "summary": summary,
        "rows": rows,
    }
    json_path = args.json_path or f"results/benchmark-{datetime.now():%Y%m%d-%H%M%S}.json"
    Path(json_path).parent.mkdir(parents=True, exist_ok=True)
    Path(json_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\n结果已写入 {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
