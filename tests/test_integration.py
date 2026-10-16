"""集成测试：打真实 berth / Temporal / LLM（探测不到就 skip）。

    pytest tests/test_integration.py -v
    SKIPPER_E2E=1 pytest tests/test_integration.py -v   # 额外跑完整 AgentTaskWorkflow（慢）
"""
from __future__ import annotations

import os
import time
import uuid

import pytest

from skipper.clients.berth import BerthClient, BerthError
from skipper.config import Settings, get_settings
from skipper.llm import create_llm_client
from skipper.planner.schema import TaskResult


def _berth_reachable(url: str) -> bool:
    try:
        return BerthClient(url, timeout=3).health().get("status") == "ok"
    except Exception:
        return False


async def _temporal_reachable(settings: Settings) -> bool:
    from temporalio.client import Client

    try:
        await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="module")
def berth(settings) -> BerthClient:
    if not _berth_reachable(settings.berth_base_url):
        pytest.skip(f"berth 不可达: {settings.berth_base_url}")
    client = BerthClient(settings.berth_base_url)
    yield client
    client.close()


# ── berth ───────────────────────────────────────────────────


def test_berth_health(berth):
    assert berth.health()["status"] == "ok"


def test_berth_run_script_hello(berth):
    result = berth.run_script('print("hello")', f"itest-{uuid.uuid4().hex[:8]}")
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


def test_berth_workspace_shared_within_task_id(berth):
    task_id = f"itest-ws-{uuid.uuid4().hex[:8]}"
    try:
        first = berth.run_script('open("/workspace/state.txt","w").write("v1")', task_id)
        assert first["exit_code"] == 0

        second = berth.run_script('print(open("/workspace/state.txt").read())', task_id)
        assert second["exit_code"] == 0
        assert "v1" in second["stdout"]

        # 非确定性 id 之间不共享 workspace：新沙箱里文件不存在
        isolated = berth.run_script('import os; print(os.path.exists("/workspace/state.txt"))', f"{task_id}-other")
        assert "False" in isolated["stdout"]
    finally:
        berth.clear_script(task_id)


def test_berth_artifacts_and_timeout(berth):
    task_id = f"itest-art-{uuid.uuid4().hex[:8]}"
    try:
        result = berth.run_script('open("/workspace/out.txt","w").write("x")', task_id)
        assert "out.txt" in result["artifacts"]

        timed_out = berth.run_script("import time; time.sleep(10)", task_id, timeout=2)
        assert timed_out["exit_code"] == -1  # berth 超时强杀
    finally:
        berth.clear_script(task_id)


def _opencode_version(berth: BerthClient) -> dict | None:
    """探测 berth 上有没有 opencode 镜像；没有返回 None。"""
    try:
        result = berth.run_script(
            cmd=["/usr/bin/env", "HOME=/tmp", "/usr/bin/opencode", "--version"],
            task_id=f"itest-oc-probe-{uuid.uuid4().hex[:8]}",
            timeout=60,
            image="opencode",
        )
    except BerthError:
        return None
    if result.get("exit_code") != 0:
        return None
    return result


def test_berth_lists_services(berth):
    services = berth.list_services(all=True)
    assert isinstance(services, list)


def test_berth_opencode_image_smoke(berth):
    """opencode 镜像可用性（先生成 rootfs：scripts/build_opencode_rootfs.sh）。"""
    result = _opencode_version(berth)
    if result is None:
        pytest.skip("berth 上没有可用的 opencode 镜像（先跑 scripts/build_opencode_rootfs.sh）")
    assert result["stdout"].strip(), result


# ── LLM planning（真实 LLM）──────────────────────────────────


@pytest.fixture(scope="module")
def llm(settings):
    client = create_llm_client(settings)
    if client is None:
        pytest.skip("未配置 LLM（SKIPPER_LLM_API_KEY / OPENAI_API_KEY）")
    return client


async def test_plan_task_with_real_llm(llm, settings):
    from temporalio.testing import ActivityEnvironment

    from skipper.temporal import activities

    activities.configure(berth=None, registry=None, llm=llm, settings=settings)
    env = ActivityEnvironment()
    plan = await env.run(
        activities.plan_task,
        "计算 1 到 100 的和，保存到 /workspace/sum.txt 并打印结果。",
    )
    assert 1 <= len(plan["subtasks"]) <= 16
    ids = [s["id"] for s in plan["subtasks"]]
    assert len(ids) == len(set(ids))
    for subtask in plan["subtasks"]:
        assert all(dep in ids for dep in subtask["depends_on"])


# ── 端到端（Temporal 真实运行）───────────────────────────────


async def test_e2e_hello_workflow(settings, berth):
    if not await _temporal_reachable(settings):
        pytest.skip(f"Temporal 不可达: {settings.temporal_address}")

    from temporalio.client import Client
    from temporalio.worker import Worker

    from skipper.drivers import DriverRegistry
    from skipper.temporal import activities
    from skipper.temporal.workflows import HelloWorkflow

    registry = DriverRegistry(berth, None, settings=settings)
    activities.configure(berth=berth, registry=registry, llm=None, settings=settings)

    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    queue = f"itest-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[HelloWorkflow],
        activities=[activities.run_hello],
    ):
        result = await client.execute_workflow(
            HelloWorkflow.run,
            'print("hello from temporal")',
            id=f"itest-hello-{uuid.uuid4().hex[:8]}",
            task_queue=queue,
        )
    assert result["exit_code"] == 0
    assert "hello from temporal" in result["stdout"]


@pytest.mark.skipif(os.environ.get("SKIPPER_E2E") != "1", reason="设置 SKIPPER_E2E=1 才跑完整链路")
async def test_e2e_agent_workflow_with_self_loop(settings, berth, llm):
    if not await _temporal_reachable(settings):
        pytest.skip(f"Temporal 不可达: {settings.temporal_address}")

    from temporalio.client import Client
    from temporalio.worker import Worker

    from skipper.drivers import DriverRegistry
    from skipper.temporal import activities
    from skipper.temporal.workflows import AgentTaskWorkflow

    registry = DriverRegistry(berth, llm, settings=settings)
    activities.configure(berth=berth, registry=registry, llm=llm, settings=settings)

    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    queue = f"itest-{uuid.uuid4().hex[:8]}"
    started = time.time()
    async with Worker(
        client,
        task_queue=queue,
        workflows=[AgentTaskWorkflow],
        activities=[activities.plan_task, activities.run_subtask, activities.aggregate_results],
    ):
        result = await client.execute_workflow(
            AgentTaskWorkflow.run,
            args=["计算 1 到 50 的和并保存到 /workspace/sum50.txt，然后打印这个和。", "self_loop"],
            id=f"itest-agent-{uuid.uuid4().hex[:8]}",
            task_queue=queue,
        )
    parsed = TaskResult.model_validate(result)
    assert parsed.status in ("completed", "partial")
    assert parsed.subtask_results
    assert all(r.status == "completed" for r in parsed.subtask_results), [
        (r.subtask_id, r.error) for r in parsed.subtask_results
    ]
    print(f"e2e(self_loop) 耗时 {time.time() - started:.1f}s, final_output={parsed.final_output[:200]!r}")


@pytest.mark.skipif(os.environ.get("SKIPPER_E2E") != "1", reason="设置 SKIPPER_E2E=1 才跑完整链路")
async def test_e2e_agent_workflow_with_opencode(settings, berth, llm):
    """opencode 驱动的完整链路：写文件 → 依赖接力 → 读取打印。"""
    if not await _temporal_reachable(settings):
        pytest.skip(f"Temporal 不可达: {settings.temporal_address}")
    if _opencode_version(berth) is None:
        pytest.skip("berth 上没有可用的 opencode 镜像（先跑 scripts/build_opencode_rootfs.sh）")

    from temporalio.client import Client
    from temporalio.worker import Worker

    from skipper.drivers import DriverRegistry
    from skipper.temporal import activities
    from skipper.temporal.workflows import AgentTaskWorkflow

    registry = DriverRegistry(berth, llm, settings=settings)
    activities.configure(berth=berth, registry=registry, llm=llm, settings=settings)

    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    queue = f"itest-{uuid.uuid4().hex[:8]}"
    started = time.time()
    async with Worker(
        client,
        task_queue=queue,
        workflows=[AgentTaskWorkflow],
        activities=[activities.plan_task, activities.run_subtask, activities.aggregate_results],
    ):
        result = await client.execute_workflow(
            AgentTaskWorkflow.run,
            args=[
                "在 /workspace 写一个文件 oc_notes.txt，内容为 'opencode in berth'，然后打印文件内容。",
                "opencode",
            ],
            id=f"itest-oc-agent-{uuid.uuid4().hex[:8]}",
            task_queue=queue,
        )
    parsed = TaskResult.model_validate(result)
    assert parsed.status == "completed", [
        (r.subtask_id, r.status, r.error) for r in parsed.subtask_results
    ]
    assert all(r.driver == "opencode" for r in parsed.subtask_results)
    artifacts = {a for r in parsed.subtask_results for a in r.artifacts}
    assert "oc_notes.txt" in artifacts, artifacts
    print(f"e2e(opencode) 耗时 {time.time() - started:.1f}s, final_output={parsed.final_output[:200]!r}")
