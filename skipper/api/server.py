"""可选：FastAPI 包装（提交 / 查询任务）。

    uvicorn skipper.api.server:app --port 8500
    # 或
    python -m skipper.api.server
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from skipper.config import get_settings
from skipper.temporal.client import get_result, query_state, submit_task

app = FastAPI(title="skipper", version="0.1.0", description="Skipper orchestrates, berth executes.")


class SubmitRequest(BaseModel):
    task: str
    driver: str = "self_loop"
    task_id: Optional[str] = None


@app.get("/health")
async def health() -> dict:
    settings = get_settings()
    return {"status": "ok", "task_queue": settings.temporal_task_queue}


@app.post("/tasks")
async def create_task(req: SubmitRequest) -> dict:
    try:
        return await submit_task(req.task, req.driver, task_id=req.task_id)
    except Exception as exc:  # Temporal 不可达等
        raise HTTPException(status_code=502, detail=f"提交失败: {exc}") from exc


@app.get("/tasks/{task_id}")
async def task_status(task_id: str) -> dict:
    try:
        state = await query_state(task_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"任务不存在或查询失败: {exc}") from exc
    return {
        "task_id": task_id,
        "phase": state.get("phase"),
        "plan": state.get("plan"),
        "subtask_results": state.get("subtask_results", []),
        "skipped": state.get("skipped", []),
        "result": state.get("result"),
    }


@app.get("/tasks/{task_id}/result")
async def task_result(task_id: str) -> dict:
    try:
        return await get_result(task_id, timeout=1)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=202, detail="任务仍在执行")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"任务不存在: {exc}") from exc


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host="0.0.0.0", port=8500, log_level="info")


if __name__ == "__main__":
    main()
