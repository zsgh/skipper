"""Worker 启动入口。

    python -m skipper.temporal.worker
    python -m skipper.temporal.worker --task-queue skipper-dev --no-phoenix
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from temporalio.client import Client
from temporalio.worker import Worker

from skipper import tracing
from skipper.clients.berth import BerthClient, BerthError
from skipper.config import Settings
from skipper.drivers import DriverRegistry
from skipper.llm import create_llm_client
from skipper.temporal import activities
from skipper.temporal.workflows import AgentTaskWorkflow, HelloWorkflow

logger = logging.getLogger("skipper.worker")

WORKFLOWS = [AgentTaskWorkflow, HelloWorkflow]
ACTIVITIES = [
    activities.plan_task,
    activities.run_subtask,
    activities.aggregate_results,
    activities.run_hello,
]


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Skipper Temporal worker")
    parser.add_argument("--temporal-address", default=None, help="Temporal 地址（默认读 env）")
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--task-queue", default=None)
    parser.add_argument("--berth-url", default=None)
    parser.add_argument("--model", default=None, help="覆盖 SKIPPER_LLM_MODEL")
    parser.add_argument("--no-phoenix", action="store_true", help="关闭 Phoenix tracing")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def build_settings(args: argparse.Namespace) -> Settings:
    base = Settings.from_env()
    return Settings(
        temporal_address=args.temporal_address or base.temporal_address,
        temporal_namespace=args.namespace or base.temporal_namespace,
        temporal_task_queue=args.task_queue or base.temporal_task_queue,
        temporal_ui_base=base.temporal_ui_base,
        berth_base_url=args.berth_url or base.berth_base_url,
        llm_base_url=base.llm_base_url,
        llm_api_key=base.llm_api_key,
        llm_model=args.model or base.llm_model,
        default_driver=base.default_driver,
        self_loop_max_turns=base.self_loop_max_turns,
        opencode_image=base.opencode_image,
        opencode_provider_id=base.opencode_provider_id,
        tracing_enabled=base.tracing_enabled and not args.no_phoenix,
        phoenix_endpoint=base.phoenix_endpoint,
        phoenix_project=base.phoenix_project,
    )


async def main(argv=None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = build_settings(args)

    # 可观测性（阶段五）：Phoenix trace
    tracing.setup_tracing(
        project_name=settings.phoenix_project,
        endpoint=settings.phoenix_endpoint,
        enabled=settings.tracing_enabled,
    )

    # berth 客户端（沙箱执行入口）
    berth = BerthClient(settings.berth_base_url)
    try:
        health = berth.health()
        logger.info("berth 健康检查 ok（%s: %s）", settings.berth_base_url, health.get("status"))
    except BerthError as exc:
        logger.warning("berth 不可达（%s）：%s —— worker 照常启动，任务执行时会报错", settings.berth_base_url, exc)

    llm = create_llm_client(settings)
    if llm is None:
        logger.warning("未配置 LLM key（SKIPPER_LLM_API_KEY / OPENAI_API_KEY）——planning 与 self_loop 会在运行时失败")
    else:
        logger.info("LLM: model=%s base_url=%s", settings.llm_model, settings.llm_base_url or "(openai 默认)")

    registry = DriverRegistry(berth, llm, settings=settings)
    activities.configure(berth=berth, registry=registry, llm=llm, settings=settings)

    # OTel 插件：让 workflow 与 activity 落在同一棵 Phoenix trace 树里
    # （worker 会自动继承 client 的 plugins，不用重复传）
    plugins = tracing.temporal_plugins()
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        plugins=plugins,
    )
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
    )
    logger.info(
        "worker 启动：task_queue=%s drivers=%s temporal=%s",
        settings.temporal_task_queue,
        registry.names(),
        settings.temporal_address,
    )
    await worker.run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
