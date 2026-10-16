"""产物路径小工具：识别/过滤「运行时噪音」，只保留真正的任务产物。

opencode、git、npm/bun 都会在 HOME=/workspace 下写自己的运行时文件，
这些不应该被当作任务产物（artifacts）接力给下游子任务。
"""
from __future__ import annotations

from typing import Iterable, List

NOISE_PREFIXES = (
    ".git/",
    ".local/",
    ".config/",
    ".cache/",
    ".opencode/",
    ".npm/",
    ".bun/",
    "node_modules/",
)
NOISE_NAMES = {
    "opencode.db",
    "opencode.db-shm",
    "opencode.db-wal",
    ".DS_Store",
}


def is_noise_artifact(name: str) -> bool:
    if not name:
        return True
    if name.startswith(NOISE_PREFIXES):
        return True
    if name in NOISE_NAMES or name.startswith("opencode.db-"):
        return True
    return False


def filter_task_artifacts(artifacts: Iterable[str]) -> List[str]:
    """过滤运行时噪音，保留任务产物（保持原顺序）。"""
    return [name for name in (artifacts or []) if not is_noise_artifact(name)]
