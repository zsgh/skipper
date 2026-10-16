"""berth HTTP 客户端封装。

berth 是沙箱执行服务（https://github.com/zsgh/berth）：
- POST /scripts/run     跑一次性脚本（overlayfs 沙箱，用完即清理；同一 task_id 共享 /workspace）
- PUT  /services/{name} 起/复用长驻服务（redis/mysql，systemd 托管）
- GET  /health          健康检查

本模块只做「把 HTTP 调干净 + 错误信息清晰」，不做任何编排。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx

from skipper import tracing

DEFAULT_BASE_URL = "http://localhost:9000"
BERTH_MAX_TIMEOUT = 600  # berth 单次执行的 timeout 上限（秒）


class BerthError(RuntimeError):
    """berth 返回错误 / 不可达。"""

    def __init__(self, message: str, *, status_code: Optional[int] = None, response: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class BerthClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        # 注意：脚本执行本身可能很久，timeout 在每次请求上单独放宽
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    # ── 基础 ──────────────────────────────────────────────

    def health(self) -> dict:
        """GET /health"""
        return self._get("/health")

    def _get(self, path: str, **params) -> dict:
        try:
            resp = self._client.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.HTTPError as exc:
            raise BerthError(f"berth 不可达（{self.base_url}）: {exc}") from exc
        return self._handle(resp)

    def _post(self, path: str, json: Optional[dict] = None, *, timeout: Optional[float] = None) -> dict:
        try:
            resp = self._client.post(path, json=json, timeout=timeout)
        except httpx.HTTPError as exc:
            raise BerthError(f"berth 不可达（{self.base_url}）: {exc}") from exc
        return self._handle(resp)

    def _put(self, path: str, json: Optional[dict] = None, *, timeout: Optional[float] = None) -> dict:
        try:
            resp = self._client.put(path, json=json, timeout=timeout)
        except httpx.HTTPError as exc:
            raise BerthError(f"berth 不可达（{self.base_url}）: {exc}") from exc
        return self._handle(resp)

    def _delete(self, path: str, **params) -> dict:
        try:
            resp = self._client.delete(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.HTTPError as exc:
            raise BerthError(f"berth 不可达（{self.base_url}）: {exc}") from exc
        return self._handle(resp)

    @staticmethod
    def _handle(resp: httpx.Response) -> Any:
        try:
            data = resp.json()
        except ValueError:
            data = {"detail": resp.text[:500]}
        if resp.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else data
            raise BerthError(f"berth 返回 {resp.status_code}: {detail}", status_code=resp.status_code, response=data)
        return data

    # ── 一次性脚本 ────────────────────────────────────────

    def run_script(
        self,
        code: Optional[str] = None,
        task_id: Optional[str] = None,
        timeout: int = 60,
        language: str = "python",
        image: Optional[str] = None,
        *,
        cmd: Optional[List[str]] = None,
        memory_mb: int = 256,
        cpu_percent: int = 50,
    ) -> dict:
        """POST /scripts/run —— 在沙箱里跑一次性脚本/命令。

        同一 task_id 共享 /workspace（断点重放时能复用产物）。
        返回: {stdout, stderr, exit_code, duration, artifacts, task_id, image, language}
        """
        timeout = max(1, min(int(timeout), BERTH_MAX_TIMEOUT))
        payload: Dict[str, Any] = {
            "timeout": timeout,
            "image": image or "base",
            "memory_mb": memory_mb,
            "cpu_percent": cpu_percent,
        }
        if code is not None:
            payload["code"] = code
            if language:
                payload["language"] = language
        elif cmd is not None:
            payload["cmd"] = list(cmd)
        else:
            raise ValueError("run_script 需要 code 或 cmd 之一")
        if task_id:
            payload["task_id"] = task_id

        with tracing.span(
            "berth.run_script",
            kind="TOOL",
            input=code if code is not None else " ".join(cmd or []),
            task_id=task_id or "",
            image=payload["image"],
            timeout=timeout,
            language=language or ("cmd" if cmd else ""),
        ) as sp:
            # HTTP 超时给足余量：脚本 timeout + 沙箱准备/清理
            result = self._post("/scripts/run", json=payload, timeout=timeout + 60)
            output_text = f"exit_code={result.get('exit_code')}\n{result.get('stdout', '')}"
            if result.get("stderr"):
                output_text += f"\n[stderr]\n{result['stderr']}"
            tracing.set_span_output(sp, output_text)

        if isinstance(result, dict):
            result.setdefault("task_id", task_id)
        return result

    async def arun_script(self, **kwargs) -> dict:
        """异步包装（activity 里用，避免阻塞事件循环）。"""
        return await asyncio.to_thread(self.run_script, **kwargs)

    def list_scripts(self, limit: int = 100) -> List[dict]:
        """GET /scripts —— 最近执行列表。"""
        return self._get("/scripts", limit=limit)

    def get_script(self, task_id: str) -> dict:
        """GET /scripts/{task_id} —— 某次执行的历史记录。"""
        return self._get(f"/scripts/{task_id}")

    def clear_script(self, task_id: str) -> dict:
        """POST /scripts/{task_id}/clear —— 清空该任务的 /workspace。"""
        return self._post(f"/scripts/{task_id}/clear")

    # ── 长驻服务 ──────────────────────────────────────────

    def start_service(self, name: str, template: str = "redis") -> dict:
        """PUT /services/{name} —— ensure：没有就建，停了就起，跑着就复用。"""
        return self._put(f"/services/{name}", json={"template": template}, timeout=120)

    def start_existing_service(self, name: str) -> dict:
        """POST /services/{name}/start —— 把已停止的服务原样再起（复用数据卷）。"""
        return self._post(f"/services/{name}/start", timeout=120)

    def stop_service(self, name: str) -> dict:
        """POST /services/{name}/stop —— 停止（保留记录与数据卷）。"""
        return self._post(f"/services/{name}/stop", timeout=60)

    def delete_service(self, name: str, volumes: bool = False) -> dict:
        """DELETE /services/{name}?volumes=... —— 删记录（可选连卷一起删）。"""
        return self._delete(f"/services/{name}", volumes="true" if volumes else None)

    def list_services(self, all: bool = False, status: Optional[str] = None) -> List[dict]:
        """GET /services —— docker ps 语义。"""
        return self._get("/services", all="true" if all else None, status=status)

    def list_volumes(self) -> List[dict]:
        """GET /volumes —— 数据卷一览（含孤儿卷）。"""
        return self._get("/volumes")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BerthClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
