"""Activities：workflow 里所有副作用都在这里。

- plan_task        LLM 拆任务 → 校验 → Plan
- run_subtask      选择 driver → 在确定性沙箱里执行子任务
- aggregate_results 汇总所有子任务结果 → TaskResult
- run_hello        最小冒烟 activity（阶段一）
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from skipper import tracing
from skipper.artifacts import filter_task_artifacts
from skipper.config import Settings, get_settings
from skipper.planner import parse as plan_parse
from skipper.planner import prompts
from skipper.planner.schema import Plan, SubtaskResult, TaskResult


# ── Activity 运行上下文（worker 启动时注入）──────────────────


@dataclass
class ActivityContext:
    berth: object = None
    registry: object = None
    llm: object = None
    settings: Settings = None  # type: ignore[assignment]


_ctx = ActivityContext()


def configure(
    *,
    berth=None,
    registry=None,
    llm=None,
    settings: Optional[Settings] = None,
) -> None:
    """worker（或测试）启动时注入依赖。"""
    global _ctx
    _ctx = ActivityContext(
        berth=berth,
        registry=registry,
        llm=llm,
        settings=settings or get_settings(),
    )


def get_context() -> ActivityContext:
    return _ctx


# ── plan_task ───────────────────────────────────────────────

PLAN_MAX_ATTEMPTS = 3


@activity.defn
async def plan_task(task: str) -> dict:
    """用户任务描述 → Plan（LLM 输出 + schema 校验 + DAG 校验）。"""
    ctx = get_context()
    if ctx.llm is None:
        raise ApplicationError(
            "plan_task 需要 LLM 客户端：请配置 SKIPPER_LLM_* 或 OPENAI_* 环境变量",
            non_retryable=True,
        )

    model = ctx.settings.llm_model
    messages: List[dict] = [
        {"role": "system", "content": prompts.PLANNING_SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    last_error: Optional[Exception] = None
    for attempt in range(1, PLAN_MAX_ATTEMPTS + 1):
        with tracing.span("plan_task.llm_call", attempt=attempt, model=model, task=task[:200]):
            resp = await _chat_with_json_fallback(ctx, model=model, messages=messages)
        raw = _message_text(resp)

        try:
            plan = plan_parse.parse_plan(raw)
            plan = plan_parse.fill_defaults(plan)
            activity.logger.info(
                "planning ok: %d subtasks (%s)",
                len(plan.subtasks),
                ", ".join(s.id for s in plan.subtasks),
            )
            return plan.model_dump()
        except plan_parse.PlanParseError as exc:
            last_error = exc
            activity.logger.warning("planning 第 %d 次解析失败: %s", attempt, exc)
            messages.append({"role": "assistant", "content": raw[:4000]})
            messages.append(
                {
                    "role": "user",
                    "content": prompts.PLAN_REPAIR_PROMPT.format(error=str(exc)),
                }
            )

    raise ApplicationError(
        f"planning 连续 {PLAN_MAX_ATTEMPTS} 次失败: {last_error}",
        non_retryable=True,
    )


# ── run_subtask ─────────────────────────────────────────────


@activity.defn
async def run_subtask(
    subtask: dict,
    driver_name: str,
    task_id: str,
    upstream_results: Optional[list] = None,
) -> dict:
    """在确定性沙箱里执行一个子任务（重放时 sandbox_id 不变，berth 复用 workspace）。

    upstream_results: 直接依赖（depends_on）的子任务结果列表。子任务各自独立沙箱，
    依赖产生的文本产物会自动接力到本沙箱的 /workspace（有大小上限），
    上游输出摘要会拼进子任务描述，保证「串行才能共享文件」的语义成立。
    """
    ctx = get_context()
    if ctx.registry is None:
        raise ApplicationError("run_subtask 缺少 driver registry（worker 未正确初始化）", non_retryable=True)

    subtask_id = str(subtask["id"])
    sandbox_id = f"{task_id}-{subtask_id}"
    upstream_results = [r for r in (upstream_results or []) if r.get("status") == "completed"]

    requested = subtask.get("preferred_driver") or driver_name or ctx.registry.default_name
    fallback_from = None
    if not ctx.registry.has(requested):
        fallback_from = requested
        requested = ctx.registry.default_name
        activity.logger.warning(
            "子任务 %s 请求的 driver %r 不存在，回退到 %r", subtask_id, fallback_from, requested
        )

    driver = ctx.registry.get(requested)
    activity.logger.info(
        "run_subtask %s → driver=%s sandbox=%s", subtask_id, requested, sandbox_id
    )
    activity.heartbeat()

    # 上游产物接力：文件内容 → 本沙箱 /workspace；输出摘要 → 任务描述
    task_description = subtask["description"]
    if upstream_results:
        try:
            seeded = await _handoff_upstream(ctx.berth, upstream_results, sandbox_id)
            if seeded:
                activity.logger.info("子任务 %s 接力上游产物: %s", subtask_id, seeded)
        except Exception as exc:  # 接力失败不阻塞执行
            activity.logger.warning("上游产物接力失败（忽略）: %s", exc)
        task_description = (
            subtask["description"]
            + "\n\n"
            + _build_upstream_context(upstream_results)
        )

    with tracing.span(
        "run_subtask",
        subtask_id=subtask_id,
        driver=requested,
        sandbox_id=sandbox_id,
    ):
        try:
            result = await _run_with_heartbeats(
                driver.run(
                    task=task_description,
                    sandbox_id=sandbox_id,
                    max_turns=ctx.settings.self_loop_max_turns,
                    timeout=1800,
                )
            )
        except Exception as exc:  # driver 不允许把异常抛给 workflow
            result = SubtaskResult(
                subtask_id=subtask_id,
                status="failed",
                driver=requested,
                sandbox_id=sandbox_id,
                output="",
                error=f"{type(exc).__name__}: {exc}",
            ).model_dump()

    result["subtask_id"] = subtask_id
    result["sandbox_id"] = sandbox_id
    result.setdefault("driver", requested)
    metadata = result.setdefault("metadata", {}) or {}
    if fallback_from:
        metadata["driver_fallback_from"] = fallback_from
    result["metadata"] = metadata
    activity.heartbeat()
    return result


async def _run_with_heartbeats(coro):
    """driver 执行期间定期 heartbeat（heartbeat_timeout=2min 不会误杀长任务）。"""

    async def beat() -> None:
        while True:
            await asyncio.sleep(30)
            try:
                activity.heartbeat()
            except Exception:  # pragma: no cover - 非 activity 上下文（测试）时忽略
                return

    hb = asyncio.create_task(beat())
    try:
        return await coro
    finally:
        hb.cancel()


# ── 上游产物接力（子任务沙箱隔离 → 依赖数据流）────────────────

HANDOFF_MAX_FILES_PER_DEP = 4
HANDOFF_MAX_FILE_CHARS = 8000      # berth stdout 上限 10000，留余量
HANDOFF_MAX_TOTAL_CHARS = 24000


def _build_upstream_context(upstream_results: List[dict]) -> str:
    """把上游子任务的输出摘要拼成给下游 agent 的上下文。"""
    lines = ["## 上游子任务产出（依赖的产出文件已放入当前沙箱 /workspace）"]
    for r in upstream_results:
        output = (r.get("output") or "").strip()
        if len(output) > 1500:
            output = output[:1500] + "...[截断]"
        artifacts = ", ".join(r.get("artifacts") or []) or "(无)"
        lines.append(f"### {r['subtask_id']}（{r.get('driver', '?')}）")
        lines.append(f"产出文件: {artifacts}")
        if output:
            lines.append(f"输出: {output}")
    return "\n".join(lines)


def _safe_artifact_name(name: str) -> Optional[str]:
    """只允许 workspace 内的相对路径。"""
    if not name or name.startswith("/") or "\x00" in name:
        return None
    parts = name.split("/")
    if any(p in ("..", "") for p in parts):
        return None
    return name


async def _handoff_upstream(berth, upstream_results: List[dict], sandbox_id: str) -> List[str]:
    """读取上游 artifacts 的文本内容，写入当前子任务的沙箱 /workspace。

    只接力文本文件（有大小上限）；读不到/二进制直接跳过。
    """
    import json

    files: dict = {}
    total = 0
    for r in upstream_results:
        dep_sandbox = r.get("sandbox_id")
        if not dep_sandbox:
            continue
        candidates = filter_task_artifacts(r.get("artifacts") or [])
        for name in candidates[:HANDOFF_MAX_FILES_PER_DEP]:
            safe = _safe_artifact_name(name)
            if not safe or total >= HANDOFF_MAX_TOTAL_CHARS:
                continue
            read_code = (
                "import sys\n"
                f"sys.stdout.write(open('/workspace/' + {safe!r}).read({HANDOFF_MAX_FILE_CHARS}))\n"
            )
            try:
                out = await berth.arun_script(
                    code=read_code, task_id=dep_sandbox, timeout=30, image="base"
                )
            except Exception:
                continue
            if out.get("exit_code") != 0:
                continue
            content = out.get("stdout") or ""
            if not content or "\x00" in content:
                continue
            content = content[:HANDOFF_MAX_FILE_CHARS]
            files[safe] = content
            total += len(content)

    if not files:
        return []

    write_code = (
        "import json, os\n"
        f"files = json.loads({json.dumps(files)!r})\n"
        "for name, content in files.items():\n"
        "    path = os.path.join('/workspace', name)\n"
        "    os.makedirs(os.path.dirname(path), exist_ok=True)\n"
        "    with open(path, 'w') as f:\n"
        "        f.write(content)\n"
        "print('seeded:', sorted(files))\n"
    )
    result = await berth.arun_script(code=write_code, task_id=sandbox_id, timeout=30, image="base")
    if result.get("exit_code") != 0:
        raise RuntimeError(f"写入上游产物失败: {(result.get('stderr') or '')[:300]}")
    return sorted(files)


# ── aggregate_results ───────────────────────────────────────


@activity.defn
async def aggregate_results(task: str, plan: dict, results: list) -> dict:
    """汇总子任务结果 → TaskResult（LLM 写总结；LLM 不可用时退化为拼接）。"""
    ctx = get_context()
    plan_obj = Plan.model_validate(plan)
    subtask_results = [SubtaskResult.model_validate(r) for r in results]

    if not subtask_results:
        status = "failed"
    elif all(r.status == "completed" for r in subtask_results):
        status = "completed"
    elif all(r.status != "completed" for r in subtask_results):
        status = "failed"
    else:
        status = "partial"

    fallback_output = _concat_outputs(subtask_results)
    final_output = fallback_output
    aggregation = "concat"
    if ctx.llm is not None and subtask_results:
        try:
            with tracing.span("aggregate_results.llm_call", subtask_count=len(subtask_results)):
                resp = await _chat_with_json_fallback(
                    ctx,
                    model=ctx.settings.llm_model,
                    messages=[
                        {"role": "system", "content": prompts.AGGREGATE_SYSTEM_PROMPT},
                        {"role": "user", "content": _aggregation_input(task, subtask_results)},
                    ],
                    json_mode=False,
                )
            text = _message_text(resp).strip()
            if text:
                final_output = text
                aggregation = "llm"
        except Exception as exc:
            activity.logger.warning("汇总 LLM 调用失败，退化为拼接: %s", exc)

    artifacts = sorted({a for r in subtask_results for a in r.artifacts})
    result = TaskResult(
        task_id="",  # workflow 回填
        status=status,
        plan=plan_obj,
        subtask_results=subtask_results,
        final_output=final_output,
        artifacts=artifacts,
    )
    payload = result.model_dump()
    payload["metadata"] = {"aggregation": aggregation}
    return payload


def _concat_outputs(results: List[SubtaskResult]) -> str:
    parts = []
    for r in results:
        head = f"## {r.subtask_id} [{r.status}] driver={r.driver}"
        body = r.output.strip() or (r.error or "")
        parts.append(f"{head}\n{body}")
    return "\n\n".join(parts)


def _aggregation_input(task: str, results: List[SubtaskResult]) -> str:
    lines = [f"用户任务：{task}", "", "子任务结果："]
    for r in results:
        output = r.output.strip()
        if len(output) > 2000:
            output = output[:2000] + "...[截断]"
        lines.append(
            f"- {r.subtask_id}（{r.status}，driver={r.driver}）: {output}"
            + (f"\n  错误: {r.error}" if r.error else "")
            + (f"\n  产物: {', '.join(r.artifacts)}" if r.artifacts else "")
        )
    return "\n".join(lines)


# ── 最小冒烟 activity（阶段一）───────────────────────────────


@activity.defn
async def run_hello(code: str = "print('hello')") -> dict:
    """在 berth 沙箱里跑一段代码，验证 Temporal → berth 链路。"""
    ctx = get_context()
    if ctx.berth is None:
        raise ApplicationError("run_hello 缺少 berth 客户端", non_retryable=True)
    task_id = f"hello-{activity.info().workflow_id}"
    result = await ctx.berth.arun_script(code=code, task_id=task_id, timeout=60)
    if result.get("exit_code") != 0:
        raise ApplicationError(
            f"berth 执行失败（exit={result.get('exit_code')}）: {result.get('stderr', '')[:500]}"
        )
    return result


# ── LLM 小工具 ──────────────────────────────────────────────


async def _chat_with_json_fallback(ctx: ActivityContext, *, model: str, messages: list, json_mode: bool = True):
    """先试 response_format=json_object；供应商不支持时回退普通调用。"""
    if json_mode:
        try:
            return await ctx.llm.chat.completions.create(
                model=model, messages=messages, temperature=0.2,
                response_format={"type": "json_object"},
            )
        except Exception:
            pass  # 不支持 json_object 的端点，回退
    return await ctx.llm.chat.completions.create(model=model, messages=messages, temperature=0.2)


def _message_text(resp) -> str:
    try:
        return resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return ""
