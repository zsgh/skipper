"""Driver 层测试：接口统一性 + SelfLoopDriver（FakeLLM/FakeBerth）+ OpencodeDriver 命令构造。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from skipper.clients.berth import BerthError
from skipper.config import Settings
from skipper.drivers import AgentDriver, DriverRegistry, OpencodeDriver, SelfLoopDriver
from skipper.planner.schema import SubtaskResult

# ── 测试替身 ────────────────────────────────────────────────


class FakeLLM:
    """模拟 openai 的 chat.completions.create 接口。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    # llm.chat.completions.create(...)
    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeLLM 没有更多响应了")
        content = self._responses.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )


class FakeBerth:
    def __init__(self, results=None, error=None):
        self.calls = []
        self._results = list(results or [])
        self._error = error

    def run_script(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        if self._results:
            return self._results.pop(0)
        return {
            "stdout": "ok\n",
            "stderr": "",
            "exit_code": 0,
            "duration": 0.12,
            "artifacts": ["out.txt"],
            "task_id": kwargs.get("task_id"),
            "image": kwargs.get("image", "base"),
            "language": kwargs.get("language", "python"),
        }

    async def arun_script(self, **kwargs):
        return self.run_script(**kwargs)


def make_settings(**overrides) -> Settings:
    base = dict(
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
    )
    base.update(overrides)
    return Settings(**base)


# ── 接口统一性 ──────────────────────────────────────────────


def test_both_drivers_implement_interface():
    registry = DriverRegistry(FakeBerth(), FakeLLM([]), settings=make_settings())
    assert registry.names() == ["opencode", "self_loop"]
    for driver in [registry.get("self_loop"), registry.get("opencode")]:
        assert isinstance(driver, AgentDriver)
        info = driver.describe()
        assert info["name"] == driver.name()
        assert "description" in info


def test_registry_unknown_driver_falls_through():
    registry = DriverRegistry(FakeBerth(), None, settings=make_settings())
    with pytest.raises(KeyError):
        registry.get("nope")
    assert registry.default_name == "self_loop"


# ── SelfLoopDriver ──────────────────────────────────────────


async def test_self_loop_completes_task():
    llm = FakeLLM(
        [
            "先算一下：\n```python\nprint('step1')\n```",
            "搞定了，结果见上。\nTASK_DONE",
        ]
    )
    berth = FakeBerth()
    driver = SelfLoopDriver(berth, llm, settings=make_settings())
    result = await driver.run("测试任务", "task-1-s1", max_turns=5, timeout=60)

    parsed = SubtaskResult.model_validate(result)
    assert parsed.status == "completed"
    assert parsed.driver == "self_loop"
    assert parsed.sandbox_id == "task-1-s1"
    assert "搞定" in parsed.output
    assert parsed.artifacts == ["out.txt"]
    assert parsed.metadata["turns"] == 2
    assert parsed.metadata["llm_tokens"] == 30  # 2 次调用 × (10+5)

    # 发给 berth 的代码做了 chdir + exec 包装，且 task_id 正确
    assert len(berth.calls) == 1
    call = berth.calls[0]
    assert call["task_id"] == "task-1-s1"
    assert call["image"] == "base"
    assert '/workspace' in call["code"] and "chdir" in call["code"]
    assert "print('step1')" in call["code"]


async def test_self_loop_max_turns_exhausted():
    llm = FakeLLM(["```python\nprint(1)\n```", "```python\nprint(2)\n```"])
    berth = FakeBerth()
    driver = SelfLoopDriver(berth, llm, settings=make_settings())
    result = await driver.run("永远做不完", "task-1-s1", max_turns=2, timeout=60)
    parsed = SubtaskResult.model_validate(result)
    assert parsed.status == "failed"
    assert "max_turns" in parsed.error
    assert len(berth.calls) == 2


async def test_self_loop_berth_error_becomes_observation():
    llm = FakeLLM(
        [
            "```python\nprint(1)\n```",
            "```python\nprint(2)\n```",
        ]
    )
    berth = FakeBerth(error=BerthError("berth 不可达"))
    driver = SelfLoopDriver(berth, llm, settings=make_settings())
    result = await driver.run("任务", "task-1-s1", max_turns=2, timeout=60)
    parsed = SubtaskResult.model_validate(result)
    assert parsed.status == "failed"
    # 第二次 LLM 调用应该看到 berth 失败的 observation
    second_call_messages = llm.calls[1]["messages"]
    assert any("berth 调用失败" in m["content"] for m in second_call_messages if m["role"] == "user")


async def test_self_loop_llm_exception_is_structured_failure():
    class BrokenLLM:
        @property
        def chat(self):
            return self

        @property
        def completions(self):
            return self

        async def create(self, **kwargs):
            raise RuntimeError("upstream 500")

    driver = SelfLoopDriver(FakeBerth(), BrokenLLM(), settings=make_settings())
    result = await driver.run("任务", "task-1-s1", max_turns=2, timeout=60)
    parsed = SubtaskResult.model_validate(result)
    assert parsed.status == "failed"
    assert "RuntimeError" in parsed.error


# ── OpencodeDriver ──────────────────────────────────────────


async def test_opencode_builds_command_with_env_injection():
    berth = FakeBerth()
    driver = OpencodeDriver(berth, settings=make_settings())
    result = await driver.run("写一个 hello.txt", "task-9-s2", max_turns=1, timeout=300)

    parsed = SubtaskResult.model_validate(result)
    assert parsed.status == "completed"
    assert parsed.driver == "opencode"

    call = berth.calls[0]
    assert call["image"] == "opencode"
    assert call["task_id"] == "task-9-s2"
    code = call["code"]
    # 命令 / 配置 / 密钥注入都在包装器里，rootfs 不需要任何密钥
    assert "opencode" in code and "run" in code
    assert "OPENCODE_CONFIG_CONTENT" in code
    assert "SKIPPER_LLM_API_KEY" in code
    assert "test-key" in code
    assert "test-model" in code
    assert "http://llm.test/v1" in code
    assert "写一个 hello.txt" in code


async def test_opencode_timeout_mapping():
    berth = FakeBerth(
        results=[
            {
                "stdout": "partial",
                "stderr": "ERROR: opencode 执行超时",
                "exit_code": -1,
                "duration": 600,
                "artifacts": [],
            }
        ]
    )
    driver = OpencodeDriver(berth, settings=make_settings())
    result = await driver.run("超时任务", "task-1-s1", max_turns=1, timeout=600)
    parsed = SubtaskResult.model_validate(result)
    assert parsed.status == "timeout"


async def test_opencode_failure_mapping():
    berth = FakeBerth(
        results=[{"stdout": "", "stderr": "boom", "exit_code": 1, "duration": 1, "artifacts": []}]
    )
    driver = OpencodeDriver(berth, settings=make_settings())
    result = await driver.run("失败任务", "task-1-s1", max_turns=1, timeout=60)
    parsed = SubtaskResult.model_validate(result)
    assert parsed.status == "failed"
    assert "boom" in parsed.error


async def test_opencode_missing_image_gives_hint():
    berth = FakeBerth(error=BerthError("berth 返回 400: 未知镜像: 'opencode'"))
    driver = OpencodeDriver(berth, settings=make_settings())
    result = await driver.run("任务", "task-1-s1", max_turns=1, timeout=60)
    parsed = SubtaskResult.model_validate(result)
    assert parsed.status == "failed"
    assert "build_opencode_rootfs.sh" in parsed.error


async def test_opencode_requires_api_key():
    berth = FakeBerth()
    driver = OpencodeDriver(berth, settings=make_settings(llm_api_key=None))
    result = await driver.run("任务", "task-1-s1", max_turns=1, timeout=60)
    parsed = SubtaskResult.model_validate(result)
    assert parsed.status == "failed"
    assert "API key" in parsed.error
    assert berth.calls == []


def test_filter_task_artifacts_strips_runtime_noise():
    from skipper.artifacts import filter_task_artifacts

    artifacts = [
        ".npm/_cacache/content-v2/sha512/aa/bb",
        ".local/share/opencode/log/x.log",
        ".config/opencode/opencode.json",
        "opencode.db",
        "opencode.db-wal",
        "node_modules/foo/index.js",
        "notes.txt",
        "reports/summary.csv",
    ]
    assert filter_task_artifacts(artifacts) == ["notes.txt", "reports/summary.csv"]
