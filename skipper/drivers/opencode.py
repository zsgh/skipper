"""OpencodeDriver：在 berth 沙箱里跑 opencode（非交互模式）。

链路：
    Skipper → berth /scripts/run(image="opencode")
        → python 包装器（chdir /workspace + 注入环境变量）
        → opencode run --model ... "<子任务描述>"

密钥安全：
- opencode 的 API key / provider 配置**不写进 rootfs**；
- 通过 berth 执行时注入的环境变量传入（OPENCODE_CONFIG_CONTENT + SKIPPER_LLM_API_KEY），
  生命周期只在这一次沙箱执行内。

前置：`scripts/build_opencode_rootfs.sh` 构建 image 名为 "opencode" 的 rootfs。
"""
from __future__ import annotations

import json
import os
from typing import List, Optional, Tuple

from skipper import tracing
from skipper.artifacts import filter_task_artifacts
from skipper.clients.berth import BerthClient, BerthError
from skipper.config import Settings, get_settings
from skipper.drivers.base import AgentDriver
from skipper.planner.schema import SubtaskResult

# 包装器：设置 cwd / 环境变量 → 跑 opencode → 透传输出与退出码
#
# 两个 sandbox 适配点（berth 的 overlayfs + pivot_root 环境）：
# 1) opencode 靠 git 探测项目根；/proc/self/mountinfo 里的宿主路径会让它的
#    realPath 探测失败 → 先 git init 一个空仓库，让项目根 = /workspace；
# 2) 显式传 --dir /workspace，避免它从挂载点反推目录。
_SANDBOX_WRAPPER = '''\
import os, subprocess, sys

os.makedirs("/workspace", exist_ok=True)
os.chdir("/workspace")

if not os.path.isdir("/workspace/.git"):
    subprocess.run(["git", "init", "-q", "/workspace"], capture_output=True)

env = os.environ.copy()
env.update({env_repr})

try:
    proc = subprocess.run(
        {argv_repr},
        capture_output=True, text=True, timeout={subprocess_timeout},
        cwd="/workspace", env=env,
    )
except subprocess.TimeoutExpired as exc:
    out = exc.stdout or ""
    err = exc.stderr or ""
    sys.stdout.write(out if isinstance(out, str) else out.decode("utf-8", "replace"))
    sys.stderr.write(err if isinstance(err, str) else err.decode("utf-8", "replace"))
    sys.stderr.write("\\nERROR: opencode 执行超时（{subprocess_timeout}s）")
    raise SystemExit(124)

sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
raise SystemExit(proc.returncode)
'''
DEFAULT_PROVIDER_ID = "skipper"


def build_opencode_config(
    provider_id: str,
    model: str,
    base_url: Optional[str],
    *,
    api_key_env: str = "SKIPPER_LLM_API_KEY",
) -> dict:
    """生成 opencode 的 config JSON（apiKey 用 {env:...} 占位，不落盘）。"""
    provider: dict = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Skipper LLM",
        "options": {"apiKey": "{env:" + api_key_env + "}"},
        "models": {model: {"name": model}},
    }
    if base_url:
        provider["options"]["baseURL"] = base_url
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"{provider_id}/{model}",
        "small_model": f"{provider_id}/{model}",
        "autoupdate": False,
        "provider": {provider_id: provider},
    }


class OpencodeDriver(AgentDriver):
    def __init__(
        self,
        berth_client: BerthClient,
        *,
        image: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        provider_id: Optional[str] = None,
        settings: Optional[Settings] = None,
        debug: bool = False,
    ):
        self.berth = berth_client
        self.settings = settings or get_settings()
        self.image = image or self.settings.opencode_image
        self.model = model or self.settings.llm_model
        self.base_url = base_url if base_url is not None else self.settings.llm_base_url
        self.api_key = api_key if api_key is not None else self.settings.llm_api_key
        self.provider_id = provider_id or self.settings.opencode_provider_id
        self.memory_mb = self.settings.opencode_memory_mb
        self.cpu_percent = self.settings.opencode_cpu_percent
        self.debug = debug

    def name(self) -> str:
        return "opencode"

    def describe(self) -> dict:
        return {
            "name": self.name(),
            "description": "在 berth 沙箱里跑 opencode（非交互 run），自带工具循环与文件编辑能力",
            "requires_llm": True,
            "model": self.model,
            "sandbox_image": self.image,
            "opencode_bin": "opencode",
        }

    # ── 主流程 ────────────────────────────────────────────

    async def run(
        self,
        task: str,
        sandbox_id: str,
        max_turns: int = 20,
        timeout: int = 600,
    ) -> dict:
        if not self.api_key:
            return self._failure(
                sandbox_id,
                "OpencodeDriver 缺少 LLM API key（配置 SKIPPER_LLM_API_KEY 或 OPENAI_API_KEY）",
            )

        # berth 单次执行上限 600s；opencode 一次 run 必须在这个窗口里完成
        berth_timeout = max(10, min(int(timeout), 600))
        subprocess_timeout = max(5, berth_timeout - 45)

        argv = [
            "opencode",
            "run",
            "--dir",
            "/workspace",
            "--model",
            f"{self.provider_id}/{self.model}",
            "--auto",
        ]
        # SKIPPER_OPENCODE_FORMAT=json 时输出 JSON 事件流（便于统计 token，输出更长）
        output_format = os.environ.get("SKIPPER_OPENCODE_FORMAT", "").strip().lower()
        if output_format in {"json", "default"}:
            argv += ["--format", output_format]
        if self.debug:
            argv.append("--print-logs")
        argv.append(task)

        config = build_opencode_config(self.provider_id, self.model, self.base_url)
        env = {
            "HOME": "/workspace",
            "OPENCODE_CONFIG_CONTENT": json.dumps(config, ensure_ascii=False),
            "SKIPPER_LLM_API_KEY": self.api_key,
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            # 沙箱里不需要每次拉 models.dev 目录（自定义 provider 已在 config 里）
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
            "NO_COLOR": "1",
            "TERM": "dumb",
        }
        wrapper = _SANDBOX_WRAPPER.format(
            env_repr=repr(env),
            argv_repr=repr(argv),
            subprocess_timeout=subprocess_timeout,
        )

        with tracing.span(
            "driver.opencode.sandbox_run",
            sandbox_id=sandbox_id,
            image=self.image,
            model=f"{self.provider_id}/{self.model}",
        ) as sp:
            try:
                result = await self.berth.arun_script(
                    code=wrapper,
                    task_id=sandbox_id,
                    timeout=berth_timeout,
                    language="python",
                    image=self.image,
                    memory_mb=self.memory_mb,
                    cpu_percent=self.cpu_percent,
                )
            except BerthError as exc:
                hint = ""
                if "未知镜像" in str(exc) or "image" in str(exc).lower():
                    hint = "（先跑 scripts/build_opencode_rootfs.sh 构建 image=opencode）"
                return self._failure(sandbox_id, f"berth 调用失败: {exc}{hint}")
            if sp is not None:
                try:
                    sp.set_attribute("exit_code", int(result.get("exit_code", 0)))
                except Exception:  # pragma: no cover
                    pass

        stdout = result.get("stdout", "") or ""
        stderr = result.get("stderr", "") or ""
        exit_code = int(result.get("exit_code", 1))
        artifacts = filter_task_artifacts(result.get("artifacts"))
        duration_ms = int(round(float(result.get("duration", 0)) * 1000))

        # 尝试从 json 事件流里提取更干净的最终回复（best-effort）
        final_text, tokens = _extract_json_events(stdout)
        output = final_text or stdout.strip()

        if exit_code == 0:
            status = "completed"
            error = None
        elif exit_code in (-1, 124):
            status = "timeout"
            error = f"opencode 在 {berth_timeout}s 内未完成（berth 单次执行上限 600s）"
        else:
            status = "failed"
            error = (stderr.strip().splitlines() or ["opencode 退出码 %s" % exit_code])[-1][:1000]

        subtask_result = SubtaskResult(
            subtask_id="",  # 由 run_subtask activity 回填
            status=status,
            driver=self.name(),
            sandbox_id=sandbox_id,
            output=output,
            artifacts=artifacts,
            duration_ms=duration_ms,
            error=error,
            trace_id=tracing.current_trace_id(),
            metadata={
                "exit_code": exit_code,
                "model": f"{self.provider_id}/{self.model}",
                "llm_tokens": tokens,
                "stderr_tail": stderr.strip()[-500:] if stderr else "",
            },
        )
        return subtask_result.model_dump()

    def _failure(self, sandbox_id: str, message: str) -> dict:
        return SubtaskResult(
            subtask_id="",
            status="failed",
            driver=self.name(),
            sandbox_id=sandbox_id,
            output=message,
            error=message,
            metadata={"model": f"{self.provider_id}/{self.model}"},
        ).model_dump()


# ── best-effort：解析 `--format json` 事件流 ─────────────────

_TEXT_KEYS = ("text", "content", "message")
_USAGE_KEYS = ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens")


def _extract_json_events(stdout: str) -> Tuple[Optional[str], int]:
    """从 opencode --format json 的事件流里提取最终文本与 token 用量。

    只做尽力而为：解析失败就返回 (None, 0)，调用方回退用原始 stdout。
    """
    if not stdout or not stdout.lstrip().startswith("{"):
        return None, 0

    texts: List[str] = []
    tokens = 0
    parsed_any = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed_any = True
        tokens += _find_tokens(event)
        candidate = _find_text(event)
        if candidate:
            texts.append(candidate)

    if not parsed_any:
        return None, 0
    final = "\n".join(texts).strip() if texts else None
    return final, tokens


def _find_tokens(obj) -> int:
    total = 0
    if isinstance(obj, dict):
        if any(k in obj for k in _USAGE_KEYS):
            try:
                total += int(obj.get("input_tokens", obj.get("prompt_tokens", 0)) or 0)
                total += int(obj.get("output_tokens", obj.get("completion_tokens", 0)) or 0)
                return total
            except (TypeError, ValueError):
                return 0
        for value in obj.values():
            total += _find_tokens(value)
    elif isinstance(obj, list):
        for value in obj:
            total += _find_tokens(value)
    return total


def _find_text(obj) -> Optional[str]:
    if isinstance(obj, dict):
        # opencode 事件大致形如 {"type":"text","part":{"text":"..."}}
        if obj.get("type") == "text" and isinstance(obj.get("part"), dict):
            text = obj["part"].get("text")
            if isinstance(text, str) and text.strip():
                return text
        for value in obj.values():
            found = _find_text(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_text(value)
            if found:
                return found
    return None
