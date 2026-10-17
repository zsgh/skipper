#!/bin/bash
# 一键拉起本地开发依赖（macOS 本机侧）：
#   - Temporal dev server（:7233，UI :8233，数据持久化在 .temporal/skipper.db）
#   - Phoenix（:6006）
#   - Skipper worker（Python 进程）
#   - FastAPI 包装（:8500）
# 已在跑的会跳过。berth 在 Lima VM 里，本脚本只做健康检查 + 提示。
#
# 用法：
#   bash scripts/start_dev.sh
#   # 只检查状态：
#   bash scripts/start_dev.sh --status
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
TEMPORAL="${TEMPORAL:-$HOME/.temporalio/bin/temporal}"
command -v "$TEMPORAL" >/dev/null 2>&1 || TEMPORAL="temporal"
LOG_DIR="${LOG_DIR:-/tmp/skipper-demo}"
STATUS_ONLY=0
[ "${1:-}" = "--status" ] && STATUS_ONLY=1

mkdir -p "$LOG_DIR" "$ROOT/.temporal"

tcp_up() { nc -z 127.0.0.1 "$1" >/dev/null 2>&1; }
http_up() { curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$1$2" 2>/dev/null; }
proc_up() { pgrep -f "$1" >/dev/null 2>&1; }

start_temporal() {
    if tcp_up 7233; then echo "Temporal:   已运行  http://localhost:8233"; return 0; fi
    if [ "$STATUS_ONLY" = 1 ]; then echo "Temporal:   未运行"; return 1; fi
    echo "Temporal:   启动中 ..."
    nohup "$TEMPORAL" server start-dev \
        --db-filename "$ROOT/.temporal/skipper.db" \
        --ui-port 8233 --port 7233 > "$LOG_DIR/temporal.log" 2>&1 &
    for _ in $(seq 1 30); do tcp_up 7233 && break; sleep 1; done
    if tcp_up 7233; then
        echo "Temporal:   ok      http://localhost:8233"
    else
        echo "Temporal:   启动失败，看 $LOG_DIR/temporal.log"
        return 1
    fi
}

start_phoenix() {
    if tcp_up 6006; then echo "Phoenix:    已运行  http://localhost:6006"; return 0; fi
    if [ "$STATUS_ONLY" = 1 ]; then echo "Phoenix:    未运行"; return 1; fi
    echo "Phoenix:    启动中 ..."
    if [ -x "$ROOT/.venv/bin/phoenix" ]; then
        nohup "$ROOT/.venv/bin/phoenix" serve --port 6006 > "$LOG_DIR/phoenix.log" 2>&1 &
    else
        nohup "$PY" -m phoenix.server.main serve --port 6006 > "$LOG_DIR/phoenix.log" 2>&1 &
    fi
    for _ in $(seq 1 40); do tcp_up 6006 && break; sleep 1; done
    if tcp_up 6006; then
        echo "Phoenix:    ok      http://localhost:6006"
    else
        echo "Phoenix:    启动失败，看 $LOG_DIR/phoenix.log"
        return 1
    fi
}

start_worker() {
    if proc_up "skipper.temporal.worker"; then echo "Worker:     已运行"; return 0; fi
    if [ "$STATUS_ONLY" = 1 ]; then echo "Worker:     未运行"; return 1; fi
    echo "Worker:     启动中 ..."
    nohup "$PY" -m skipper.temporal.worker > "$LOG_DIR/worker-main.log" 2>&1 &
    sleep 5
    if proc_up "skipper.temporal.worker"; then
        echo "Worker:     ok      task_queue=skipper-task-queue"
    else
        echo "Worker:     启动失败，看 $LOG_DIR/worker-main.log"
        return 1
    fi
}

start_api() {
    if tcp_up 8500; then echo "API:        已运行  http://localhost:8500/docs"; return 0; fi
    if [ "$STATUS_ONLY" = 1 ]; then echo "API:        未运行"; return 1; fi
    echo "API:        启动中 ..."
    nohup "$PY" -m skipper.api.server > "$LOG_DIR/api.log" 2>&1 &
    for _ in $(seq 1 15); do http_up 8500 /health && break; sleep 1; done
    if http_up 8500 /health; then
        echo "API:        ok      http://localhost:8500/docs"
    else
        echo "API:        启动失败，看 $LOG_DIR/api.log"
        return 1
    fi
}

check_berth() {
    local health
    health=$(curl -s --max-time 3 http://127.0.0.1:9000/health 2>/dev/null)
    if echo "$health" | grep -q '"ok"'; then
        echo "berth:      已运行  http://localhost:9000（沙箱执行正常）"
    else
        echo "berth:      不可达！Skipper 无法执行任务。"
        echo "            如果在 Lima VM 里：limactl start debian12"
        echo "            然后进 VM 拉起 berth："
        echo "              limactl shell debian12 -- bash -lc 'cd ~/stage3/berth && setsid nohup .venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 9000 > /tmp/berth.log 2>&1 < /dev/null &'"
        echo "            注意：VM 重启后 mysql/redis 记录会变 crashed，用以下命令恢复："
        echo "              curl -X PUT localhost:9000/services/mysql  -H 'Content-Type: application/json' -d '{\"template\":\"mysql\"}'"
        echo "              curl -X PUT localhost:9000/services/my-redis -H 'Content-Type: application/json' -d '{\"template\":\"redis\"}'"
    fi
}

echo "== Skipper 本地依赖 =="
start_temporal
start_phoenix
start_worker
start_api
check_berth
echo
echo "日志目录: $LOG_DIR"
