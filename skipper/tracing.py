"""Phoenix / OpenTelemetry 接入（可选，装不上就安静降级）。

- worker 启动时调用 setup_tracing()
- 业务代码用 skipper.tracing.span() 埋点，没开 tracing 时是 no-op

span 内容遵循 OpenInference 语义约定（Phoenix UI 的 kind / input / output 列
就是读这些属性）：
    openinference.span.kind = CHAIN | TOOL | LLM | ...
    input.value / input.mime_type
    output.value / output.mime_type
    session.id                （workflow_id：Phoenix 的 Sessions 视图按它聚合）
"""
from __future__ import annotations

import contextlib
import logging
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

MAX_SPAN_TEXT = 4000  # 单个 input/output 的截断长度

_tracer_provider = None
_enabled = False


def setup_tracing(
    project_name: str = "skipper",
    endpoint: str = "http://localhost:6006/v1/traces",
    enabled: bool = True,
) -> bool:
    """初始化 Phoenix OTLP trace。返回是否成功启用。"""
    global _tracer_provider, _enabled

    if _enabled:
        return True  # 幂等：client / worker 可能都调一次
    if not enabled:
        logger.info("tracing disabled (SKIPPER_TRACING=0)")
        return False

    try:
        from phoenix.otel import register  # noqa: F401  （依赖检查）
    except Exception as exc:  # pragma: no cover - 依赖缺失时
        logger.warning("arize-phoenix-otel 不可用，跳过 tracing: %s", exc)
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # Temporal 的 OpenTelemetryPlugin 要求全局 provider 是 ReplaySafeTracerProvider，
        # 所以这里手动建（必须在使用任何 trace API 之前 set_global）
        resource = Resource.create(
            {
                "service.name": project_name,
                "openinference.project.name": project_name,  # Phoenix 的项目分组
            }
        )
        try:
            from temporalio.contrib.opentelemetry import create_tracer_provider

            provider = create_tracer_provider(resource=resource)
        except Exception as exc:  # pragma: no cover - 没有 temporalio contrib 时降级
            logger.debug("create_tracer_provider 不可用，使用普通 provider: %s", exc)
            from opentelemetry.sdk.trace import TracerProvider

            provider = TracerProvider(resource=resource)

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        _tracer_provider = provider

        try:
            from openinference.instrumentation.openai import OpenAIInstrumentor

            OpenAIInstrumentor().instrument(tracer_provider=provider)
        except Exception as exc:  # pragma: no cover
            logger.warning("openinference OpenAI 埋点不可用: %s", exc)
        _enabled = True
        logger.info("tracing → Phoenix (%s, project=%s)", endpoint, project_name)
        return True
    except Exception as exc:  # pragma: no cover - 端点不可达等
        logger.warning("tracing 初始化失败（继续运行）: %s", exc)
        return False


def tracer():
    """拿到 tracer；没启用时返回 None。"""
    if not _enabled:
        return None
    from opentelemetry import trace

    return trace.get_tracer("skipper")


# ── span 内容写入（OpenInference 语义）──────────────────────


def _truncate(text: str, limit: int = MAX_SPAN_TEXT) -> str:
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 20] + f"...[截断 {len(text) - limit + 20} 字符]"


def _set_attr(sp, key: str, value) -> None:
    if sp is None or value is None:
        return
    try:
        sp.set_attribute(key, value if isinstance(value, (str, int, float, bool)) else str(value))
    except Exception:  # pragma: no cover
        pass


def set_span_kind(sp, kind: str) -> None:
    """CHAIN（编排）/ TOOL（执行）/ LLM / AGENT …，Phoenix 的 kind 列。"""
    _set_attr(sp, "openinference.span.kind", kind)


def set_span_input(sp, text: str, mime_type: str = "text/plain") -> None:
    _set_attr(sp, "input.value", _truncate(text))
    _set_attr(sp, "input.mime_type", mime_type)


def set_span_output(sp, text: str, mime_type: str = "text/plain") -> None:
    _set_attr(sp, "output.value", _truncate(text))
    _set_attr(sp, "output.mime_type", mime_type)


def set_span_session(sp, session_id: str) -> None:
    """session.id（= workflow_id）：Phoenix Sessions 视图按任务聚合。"""
    _set_attr(sp, "session.id", session_id)


def mark_error(sp, exc: BaseException) -> None:
    """span 标红：record_exception + status=ERROR。"""
    if sp is None:
        return
    try:
        sp.record_exception(exc)
        from opentelemetry.trace import Status, StatusCode

        sp.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
    except Exception:  # pragma: no cover
        pass


@contextlib.contextmanager
def span(
    name: str,
    *,
    kind: Optional[str] = None,
    input: Optional[str] = None,  # noqa: A002 - 对齐 OpenInference 的 input.value
    output: Optional[str] = None,
    session_id: Optional[str] = None,
    **attributes,
) -> Iterator[object]:
    """统一的埋点入口。无 tracing 时是 no-op 上下文。

    input/output 还没确定时（比如要等调用结果出来），先传 None，
    拿到结果后在 with 块内调用 set_span_input / set_span_output。
    """
    t = tracer()
    if t is None:
        yield None
        return
    with t.start_as_current_span(name) as sp:
        if kind:
            set_span_kind(sp, kind)
        if input is not None:
            set_span_input(sp, input)
        if output is not None:
            set_span_output(sp, output)
        if session_id:
            set_span_session(sp, session_id)
        for key, value in attributes.items():
            _set_attr(sp, key, value)
        yield sp


def current_trace_id() -> Optional[str]:
    """当前 span 的 trace id（hex），便于把执行结果和 trace 关联起来。"""
    if not _enabled:
        return None
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.trace_id:
            return format(ctx.trace_id, "032x")
    except Exception:  # pragma: no cover
        pass
    return None


def temporal_plugins() -> list:
    """Temporal 的 OpenTelemetry 插件：workflow → activity 同一棵 trace 树。

    用官方 OpenTelemetryPlugin（自带 sandbox passthrough + 上下文传播）；
    add_temporal_spans=True 会生成 RunWorkflow / RunActivity 这类 span。
    """
    if not _enabled:
        return []
    try:
        from temporalio.contrib.opentelemetry import OpenTelemetryPlugin

        return [OpenTelemetryPlugin(add_temporal_spans=True)]
    except Exception as exc:  # pragma: no cover - 依赖缺失
        logger.warning("Temporal OpenTelemetryPlugin 不可用: %s", exc)
        return []
