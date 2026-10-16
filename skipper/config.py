"""集中配置：全部来自环境变量（.env 可选），不在代码里写死任何密钥。

命名约定：SKIPPER_* 优先，回退到通用的 OPENAI_* / 默认值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

try:  # .env 可选
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv 不可用时安静降级
    pass


def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    """运行期设置。密钥字段 repr=False，避免日志/异常里意外泄漏。"""

    # Temporal
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "skipper-task-queue"
    temporal_ui_base: str = "http://localhost:8233"

    # berth
    berth_base_url: str = "http://localhost:9000"

    # LLM（OpenAI 兼容）
    llm_base_url: Optional[str] = None
    llm_api_key: Optional[str] = field(default=None, repr=False)
    llm_model: str = "gpt-4o-mini"

    # Driver
    default_driver: str = "self_loop"
    self_loop_max_turns: int = 20
    opencode_image: str = "opencode"
    opencode_provider_id: str = "skipper"
    opencode_memory_mb: int = 1024   # opencode(Bun) 比普通脚本吃内存，berth 默认 256 不够
    opencode_cpu_percent: int = 200

    # 可观测性
    tracing_enabled: bool = True
    phoenix_endpoint: str = "http://localhost:6006/v1/traces"
    phoenix_project: str = "skipper"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            temporal_address=_env("TEMPORAL_ADDRESS", default="localhost:7233"),
            temporal_namespace=_env("TEMPORAL_NAMESPACE", default="default"),
            temporal_task_queue=_env("TEMPORAL_TASK_QUEUE", default="skipper-task-queue"),
            temporal_ui_base=_env("TEMPORAL_UI_BASE", default="http://localhost:8233"),
            berth_base_url=_env("BERTH_BASE_URL", "BERTH_URL", default="http://localhost:9000"),
            llm_base_url=_env("SKIPPER_LLM_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE"),
            llm_api_key=_env("SKIPPER_LLM_API_KEY", "OPENAI_API_KEY"),
            llm_model=_env("SKIPPER_LLM_MODEL", "OPENAI_MODEL", default="gpt-4o-mini"),
            default_driver=_env("SKIPPER_DEFAULT_DRIVER", default="self_loop"),
            self_loop_max_turns=_env_int("SKIPPER_SELF_LOOP_MAX_TURNS", 20),
            opencode_image=_env("SKIPPER_OPENCODE_IMAGE", default="opencode"),
            opencode_provider_id=_env("SKIPPER_OPENCODE_PROVIDER_ID", default="skipper"),
            opencode_memory_mb=_env_int("SKIPPER_OPENCODE_MEMORY_MB", 1024),
            opencode_cpu_percent=_env_int("SKIPPER_OPENCODE_CPU_PERCENT", 200),
            tracing_enabled=_env_bool("SKIPPER_TRACING", True),
            phoenix_endpoint=_env("PHOENIX_ENDPOINT", "PHOENIX_COLLECTOR_ENDPOINT", default="http://localhost:6006/v1/traces"),
            phoenix_project=_env("PHOENIX_PROJECT", default="skipper"),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def reset_settings() -> None:
    """测试用：清掉缓存，让 env 改动重新生效。"""
    get_settings.cache_clear()
