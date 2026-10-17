#!/bin/bash
# 停止本机侧开发进程：benchmark / worker / API / Phoenix / Temporal。
# 不动 berth（它在 Lima VM 里，需要时用 `limactl stop debian12`；注意会一并停掉 VM 里的 mysql/redis）。
#
# 用法：
#   bash scripts/stop_dev.sh            # 停 worker/API/Phoenix/Temporal + 终止运行中的 workflow
#   bash scripts/stop_dev.sh --tasks    # 只停评测 + 终止运行中的 workflow，保留服务
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
TEMPORAL="${TEMPORAL:-$HOME/.temporalio/bin/temporal}"
command -v "$TEMPORAL" >/dev/null 2>&1 || TEMPORAL="temporal"
ONLY_TASKS=0
[ "${1:-}" = "--tasks" ] && ONLY_TASKS=1

stop_proc() {
    local pattern="$1" label="$2"
    if pgrep -f "$pattern" >/dev/null 2>&1; then
        pkill -f "$pattern" 2>/dev/null
        sleep 1
        pgrep -f "$pattern" >/dev/null 2>&1 && { pkill -9 -f "$pattern" 2>/dev/null; sleep 1; }
        echo "$label: 已停止"
    else
        echo "$label: 未运行"
    fi
}

echo "== 停止评测/任务 =="
stop_proc "scripts/benchmark.py" "benchmark"

# 终止所有运行中的 workflow（否则 worker 停了它们会一直等着）
if "$TEMPORAL" operator namespace list >/dev/null 2>&1; then
    "$PY" - <<'EOF'
import asyncio
from skipper.temporal.client import get_client

async def main() -> None:
    client = await get_client()
    ids = []
    async for wf in client.list_workflows("ExecutionStatus='Running'", limit=100):
        ids.append(wf.id)
    for tid in ids:
        try:
            await client.get_workflow_handle(tid).terminate(reason="stop_dev.sh")
            print(f"workflow:  terminated {tid}")
        except Exception as exc:
            print(f"workflow:  terminate failed {tid}: {exc}")
    if not ids:
        print("workflow:  没有运行中的任务")
asyncio.run(main())
EOF
fi

if [ "$ONLY_TASKS" = 1 ]; then
    echo
    echo "只停了任务；Temporal/Phoenix/worker/API 保持运行。"
    exit 0
fi

echo "== 停止本机服务 =="
stop_proc "skipper.temporal.worker" "worker"
stop_proc "skipper.api.server" "API"
stop_proc "phoenix serve" "Phoenix"
stop_proc "temporal server start-dev" "Temporal"
echo
echo "berth 仍在 Lima VM 里运行（需要停整个 VM：limactl stop debian12）"
