"""LLM 客户端工厂：任何 OpenAI 兼容接口（OpenAI / GLM / MiMo / vLLM ...）。"""
from __future__ import annotations

from typing import Optional

from skipper.config import Settings, get_settings


def create_llm_client(settings: Optional[Settings] = None):
    """按配置创建 AsyncOpenAI 客户端；没配 key 返回 None。"""
    s = settings or get_settings()
    if not s.llm_api_key:
        return None

    from openai import AsyncOpenAI

    kwargs = {"api_key": s.llm_api_key, "timeout": 120.0, "max_retries": 2}
    if s.llm_base_url:
        kwargs["base_url"] = s.llm_base_url
    return AsyncOpenAI(**kwargs)
