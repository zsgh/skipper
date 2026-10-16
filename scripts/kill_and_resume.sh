#!/bin/bash
# 断点恢复演示：提交任务 → 杀掉 worker → 重启 → 任务从断点继续（已完成的 activity 不重跑）。
#
# 前置：
#   1) temporal server start-dev（或指向已有 server）
#   2) berth 在跑（BERTH_BASE_URL）
#   3) pip install -e . 完成，且配置了 LLM（SKIPPER_LLM_* 或 OPENAI_*）
#
# 用法：
#   scripts/kill_and_resume.sh ["任务描述"] [driver]
# 环境变量：
#   RESUME_TIMEOUT=1800   等待任务完成的上限（秒）
#   LOG_DIR=/tmp/skipper-demo
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
[ -x .venv/bin/python ] && PYTHON=".venv/bin/python"
TEMPORAL="${TEMPORAL:-$HOME/.temporalio/bin/temporal}"
command -v "$TEMPORAL" >/dev/null 2>&1 || TEMPORAL="temporal"

TASK="${1:-三步任务：第一步把 'A' 写入 /workspace/a.txt；第二步把 'B' 写入 /workspace/b.txt；第三步读取 a.txt 和 b.txt，把内容合并写入 /workspace/c.txt 并打印出来。}"
DRIVER="${2:-self_loop}"
RESUME_TIMEOUT="${RESUME_TIMEOUT:-1800}"
LOG_DIR="${LOG_DIR:-/tmp/skipper-demo}"
mkdir -p "$LOG_DIR"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

"$TEMPORAL" operator namespace list >/dev/null 2>&1 || {
    echo "Temporal 不可达：先跑 'temporal server start-dev'（或设置 TEMPORAL=...）" >&2
    exit 1
}

cleanup() {
    [ -n "${W1:-}" ] && kill "$W1" 2>/dev/null
    [ -n "${W2:-}" ] && kill "$W2" 2>/dev/null
    return 0
}
trap cleanup EXIT

say "启动 worker（第 1 次）"
"$PYTHON" -m skipper.temporal.worker > "$LOG_DIR/worker-1.log" 2>&1 &
W1=$!
sleep 4
kill -0 "$W1" 2>/dev/null || { echo "worker 启动失败，看 $LOG_DIR/worker-1.log"; exit 1; }

say "提交任务（后台）"
"$PYTHON" scripts/submit_task.py "$TASK" --driver "$DRIVER" > "$LOG_DIR/submit.log" 2>&1 &
TASK_ID=""
for _ in $(seq 1 90); do
    TASK_ID=$(sed -n 's/^Task ID: //p' "$LOG_DIR/submit.log" | head -1)
    [ -n "$TASK_ID" ] && break
    sleep 1
done
if [ -z "$TASK_ID" ]; then
    echo "提交失败："; cat "$LOG_DIR/submit.log"; exit 1
fi
echo "Task ID: $TASK_ID"

say "等第一个批次的子任务跑起来"
"$PYTHON" - "$TASK_ID" <<'PY'
import asyncio, sys
from skipper.temporal.client import query_state

async def main(task_id: str) -> None:
    for _ in range(120):
        try:
            state = await query_state(task_id)
        except Exception:
            await asyncio.sleep(2)
            continue
        done = [r for r in state.get("subtask_results", []) if r.get("status") == "completed"]
        if state.get("phase") == "planned" or done:
            print(f"  phase={state.get('phase')} 已完成子任务={[r['subtask_id'] for r in done]}")
            return
        if state.get("phase") == "done":
            print("  任务已经跑完了（太快了，建议换更长的任务）")
            return
        await asyncio.sleep(3)
    print("  等超时，直接继续")

asyncio.run(main(sys.argv[1]))
PY

say "记录杀之前的 event history"
"$TEMPORAL" workflow show -w "$TASK_ID" -o json > "$LOG_DIR/before.json" 2>/dev/null \
    || "$TEMPORAL" workflow show -w "$TASK_ID" > "$LOG_DIR/before.txt" 2>&1 || true

say "kill worker（模拟崩溃）"
pkill -f "skipper.temporal.worker" 2>/dev/null
sleep 3
if pgrep -f "skipper.temporal.worker" >/dev/null 2>&1; then
    echo "worker 还活着，强杀"; pkill -9 -f "skipper.temporal.worker" 2>/dev/null; sleep 1
fi
echo "worker 已死"

say "重启 worker（第 2 次）"
"$PYTHON" -m skipper.temporal.worker > "$LOG_DIR/worker-2.log" 2>&1 &
W2=$!

say "等待任务从断点继续并完成（上限 ${RESUME_TIMEOUT}s）"
"$PYTHON" - "$TASK_ID" "$RESUME_TIMEOUT" <<'PY'
import asyncio, sys, time
from skipper.temporal.client import get_result

async def main(task_id: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = await get_result(task_id, timeout=15)
            print(f"  完成：status={result['status']} 子任务={[r['subtask_id'] for r in result['subtask_results']]}")
            return
        except asyncio.TimeoutError:
            continue
    print("  超时未完成")

asyncio.run(main(sys.argv[1], float(sys.argv[2])))
PY

say "记录杀之后的 event history"
"$TEMPORAL" workflow show -w "$TASK_ID" -o json > "$LOG_DIR/after.json" 2>/dev/null \
    || "$TEMPORAL" workflow show -w "$TASK_ID" > "$LOG_DIR/after.txt" 2>&1 || true

say "对比：每个 activity 被调度了几次（>1 = 重跑）"
"$PYTHON" - "$LOG_DIR/before.json" "$LOG_DIR/after.json" <<'PY'
import collections, json, pathlib, sys

def scheduled_counts(path: str) -> collections.Counter:
    counter: collections.Counter = collections.Counter()
    p = pathlib.Path(path)
    if not p.exists():
        return counter
    try:
        data = json.loads(p.read_text())
    except (ValueError, OSError):
        return counter
    for ev in data.get("events", []):
        if ev.get("eventType") == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED":
            attrs = ev.get("activityTaskScheduledEventAttributes") or {}
            name = (attrs.get("activityType") or {}).get("name", "?")
            counter[name] += 1
    return counter

before, after = scheduled_counts(sys.argv[1]), scheduled_counts(sys.argv[2])
if not after:
    print("  （读不到 JSON history，看上面的文本版本）")
for name in sorted(set(before) | set(after)):
    print(f"  {name:<18} kill 前调度 {before[name]} 次 → 完成后共 {after[name]} 次")
print("  （值不变 = 已完成的 activity 没有重跑；resume 只补跑没做完的）")
PY

say "worker 日志片段（第 2 次）"
tail -5 "$LOG_DIR/worker-2.log" || true

echo
echo "演示结束。完整日志在 $LOG_DIR/，Temporal UI: http://localhost:8233"
