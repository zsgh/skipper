"""Plan 解析与校验：LLM 输出的 JSON → Plan（含 DAG 合法性检查）。

纯函数，不依赖网络/文件（plan_task activity 里调用）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from skipper.planner.schema import Plan, Subtask


class PlanParseError(ValueError):
    """LLM 输出无法解析为合法 Plan。"""


def extract_json(text: str) -> Dict[str, Any]:
    """从 LLM 输出里抠出第一个 JSON 对象。

    依次尝试：整体 json.loads → ```json 代码块 → 首个平衡的 {...}。
    """
    text = (text or "").strip()
    if not text:
        raise PlanParseError("LLM 输出为空")

    # 1) 整体就是 JSON
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # 2) 提取 ```json ... ``` 代码块
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        try:
            obj = json.loads(fence.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # 3) 扫描第一个平衡的 {...}（处理前后有解释文字的情况）
    start = text.find("{")
    if start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break

    raise PlanParseError(f"无法从输出中提取 JSON：{text[:200]!r}")


def parse_plan(raw: str) -> Plan:
    """LLM 原始输出 → 校验过的 Plan。"""
    obj = extract_json(raw)

    # 宽容处理：允许 {"plan": {...}} 或直接 [...] 等轻微变形
    if "subtasks" not in obj:
        for value in obj.values():
            if isinstance(value, dict) and "subtasks" in value:
                obj = value
                break

    try:
        plan = Plan.model_validate(obj)
    except Exception as exc:  # pydantic ValidationError 等
        raise PlanParseError(f"Plan schema 校验失败: {exc}") from exc

    validate_plan(plan)
    return plan


def validate_plan(plan: Plan) -> None:
    """DAG 合法性：id 唯一、依赖存在、无环、粒度合理。"""
    subtasks: List[Subtask] = plan.subtasks
    if not subtasks:
        raise PlanParseError("Plan 里没有任何子任务")
    if len(subtasks) > 16:
        raise PlanParseError(f"子任务过多（{len(subtasks)} > 16），请合并粒度")

    ids = [s.id for s in subtasks]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise PlanParseError(f"子任务 id 重复: {dupes}")

    known = set(ids)
    for s in subtasks:
        unknown = [d for d in s.depends_on if d not in known]
        if unknown:
            raise PlanParseError(f"子任务 {s.id} 依赖了未知 id: {unknown}")
        if s.id in s.depends_on:
            raise PlanParseError(f"子任务 {s.id} 依赖了自己")

    # 环检测（Kahn）
    indegree = {s.id: len(s.depends_on) for s in subtasks}
    children: Dict[str, List[str]] = {s.id: [] for s in subtasks}
    for s in subtasks:
        for dep in s.depends_on:
            children[dep].append(s.id)
    queue = [i for i, d in indegree.items() if d == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for child in children[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(subtasks):
        cyclic = sorted(i for i, d in indegree.items() if d > 0)
        raise PlanParseError(f"依赖存在环: {cyclic}")


def fill_defaults(plan: Plan) -> Plan:
    """补 planning 输出里可省略的字段（total_estimated_steps 等）。"""
    if not plan.total_estimated_steps:
        plan.total_estimated_steps = len(plan.subtasks)
    return plan
