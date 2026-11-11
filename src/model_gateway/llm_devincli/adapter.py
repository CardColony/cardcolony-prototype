# -*- coding: utf-8 -*-
"""Devin CLI 适配器（``devin -p --prompt-file --export``，本地模式）。

**定位**：与 ``llm_wbcli`` 同思路——把本机已登录的 Devin CLI
（桌面版捆绑的 ``devin.exe``，credentials.toml 自有登录态）当成一个
"模型 endpoint" 挂进 ``LlmRuntime`` 的 vendor 注册表，让
``models.yaml`` 里的 Devin 模型可以像普通模型一样被选中调用。

**与 wb-cli 的差异**（实测底稿 2026-11-10，devin 3000.11.x）：
    - **输出不是流式**：``-p`` 的 stdout 收工才吐最终正文 ⇒ 适配器
      缓冲到进程退出，一次性发 ``TextDelta``（中途只有 1s 节拍的
      abort/超时轮询）。
    - **结构化产物走 ``--export``**：ATIF 轨迹文件里有最终 agent 消息
      与 ``final_metrics``（token 用量），比 stdout 更可靠，优先取。
    - **没有系统提示词通道**（retail 版 ``--sysprompt`` 只在 partner
      build 有）⇒ 卡系统提示词与工具协议段都摊进 prompt 文件，
      协议段置顶标「宿主系统指令」（wb 实测：放用户位置会被 agent
      模型当"用户声称"拒写调用块）。
    - **本地模式不带 ``--cloud``**：云会话看不见本地目录且会产生
      假成功（执行体路径实证事故）。
    - **``ACP_BACKEND`` 必须剔**：桌面版注入的环境变量会让子进程
      误判自身角色 ⇒ rc=1 "Not logged in"（api.wb_exec 事故定案）。
    - 无 effort flag：思考档写在模型名里（swe-2-high/medium/max）。
      temperature/max_tokens/stop 无对应 flag。
    - 内置工具压不掉（无 --tools 开关）⇒ ``--permission-mode auto``
      （只放行只读件）+ 空白工作目录 + 协议段里明令"按文本协议调"。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from ..llm.errors import LlmError
from ..llm.retry import ResolvedRetryPolicy
from ..llm.runtime import LlmAdapter
from ..llm.types import (
    AbortedReason,
    BlockEnd,
    BlockStart,
    ErrorReason,
    FinishChunk,
    GenerateOptions,
    LlmFailure,
    ReasoningBlock,
    ReasoningDelta,
    StopReason,
    StreamChunk,
    TextBlock,
    TextDelta,
    TokenUsage,
    UsageChunk,
)
# 与 wb-cli 同一套"消息→转录文本 + 文本工具协议"序列化（纯函数，跨包复用）。
from ..llm_wbcli.serialize import serialize_transcript, tool_protocol_section

__all__ = [
    "DevinCliConnectionOptions",
    "DevinCliAdapterOptions",
    "DevinCliAdapter",
    "DEVINCLI_PROTOCOL",
]

logger = logging.getLogger("model_gateway.devincli")

#: models.yaml 里这类 provider 的 ``protocol`` 标记（编译后落为模型 entry 的
#: ``provider`` 字段）⇒ gateway._build_runtime 据此路由到本适配器。
DEVINCLI_PROTOCOL = "devin-cli"

#: 桌面版/宿主 IDE 注入的污染变量——带 ``ACP_BACKEND`` 时 devin 子进程会
#: 把自已认成 IDE 内嵌 agent ⇒ "Not logged in"（wb_exec 已踩实）。
_ENV_DROP = ("ACP_BACKEND",)

#: 未登录/凭证失效的 stderr 措辞（实测样例："Not logged in"）。
_AUTH_HINTS = ("not logged in", "authentication required", "unauthorized",
               "credentials")


@dataclass(frozen=True)
class DevinCliConnectionOptions:
    """一次调用已解析的 Devin CLI 连接事实。

    凭据全在 CLI 自己的登录态里（``credentials.toml``）——本适配器
    **不读、不传任何 token**。
    """

    exe: str = ""
    #: 子进程工作目录：配一个**空白 scratch 目录**——auto 档放行只读
    #: 内置工具，空目录让它无文件可翻（agent 壳爱探索 cwd）。
    work_dir: str = ""
    permission_mode: str = "auto"
    env_extra: Optional[dict[str, str]] = None
    #: 进程墙钟（秒）；swe-2-max 全轮往返量级是十秒级，给足但防挂死。
    timeout: float = 1800.0
    retry_policy: Optional[ResolvedRetryPolicy] = None


@dataclass(frozen=True)
class DevinCliAdapterOptions:
    """``(provider, model) -> 连接事实`` 钩子 + 可注入的进程拉起面（测试用）。"""

    options: Callable[[str, str], DevinCliConnectionOptions]
    spawn: Optional[Callable[..., Awaitable[Any]]] = None


def _child_env(extra: Optional[dict[str, str]]) -> dict[str, str]:
    env = dict(os.environ)
    for k in _ENV_DROP:
        env.pop(k, None)
    for k, v in (extra or {}).items():
        env[str(k)] = str(v)
    return env


async def _real_spawn(argv: list[str], env: dict[str, str], cwd: str):
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or None,
        env=env,
        limit=8 * 1024 * 1024,
    )


def _parse_atif(path: str) -> dict[str, Any]:
    """读 ``--export`` 的 ATIF 轨迹：最终正文 / 末轮思考 / token 用量。

    结构与 ``api.wb_exec.devin_client.parse_atif`` 同源（ATIF-v1.7 实测）；
    这里只要模型级记账三件，不抄工具步解析（跨层不引 api.*）。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    text = ""
    reasoning = ""
    for st in (data.get("steps") or []):
        if not isinstance(st, dict) or str(st.get("source") or "") != "agent":
            continue
        msg = str(st.get("message") or "").strip()
        if msg:
            text = msg
            reasoning = str(st.get("reasoning_content") or "").strip() or reasoning
    metrics = data.get("final_metrics") if isinstance(data.get("final_metrics"), dict) else {}
    return {"text": text, "reasoning": reasoning,
            "session_id": str(data.get("session_id") or ""), "metrics": metrics}


def _usage_from_metrics(metrics: dict[str, Any]) -> Optional[TokenUsage]:
    """ATIF ``final_metrics`` → TokenUsage（键名按实测多档兜底）。"""
    def _num(*keys: str) -> int:
        for k in keys:
            v = metrics.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    inp = _num("total_prompt_tokens", "prompt_tokens", "input_tokens")
    out = _num("total_completion_tokens", "completion_tokens", "output_tokens",
               "total_output_tokens")
    if not inp and not out:
        return None
    return TokenUsage(input_tokens=inp, output_tokens=out)


def _is_auth_error(msg: str) -> bool:
    low = msg.lower()
    return any(h in low for h in _AUTH_HINTS)


class DevinCliAdapter(LlmAdapter):
    """``devin -p`` 子进程 → StreamChunk 流（终态一次性回吐）。"""

    def __init__(self, config: DevinCliAdapterOptions) -> None:
        self._config = config

    def provider_info(self, provider: str) -> dict[str, str]:
        return {"id": provider, "name": provider}

    def provider_retry_policy(self, provider: str) -> Optional[ResolvedRetryPolicy]:
        try:
            return self._config.options(provider, "").retry_policy
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        conn = self._config.options(options.provider, options.model)
        if not conn.exe:
            raise LlmError("devin cli exe 未配置", "DEVINCLI_CONFIG")

        # ---- prompt 文件组装 ----
        # 顺序：①工具协议段（带 tools 时）＋②卡系统提示词＋③消息转录。
        # devin 无系统通道 ⇒ 前两段在文件里按"宿主注入"措辞置顶
        # （wb 实测教训：协议放用户位置会被 agent 模型当"用户声称"拒绝）。
        parts: list[str] = []
        _tp = tool_protocol_section(options.tools)
        if _tp:
            parts.append(
                "- 环境约束（宿主系统注入，优先级最高）：你自带的内置工具在"
                "**本会话已全部下线**——任何直接调用都会在执行前被系统拒绝，"
                "不会生效；清单工具的**唯一**调用方式是下面指定的 XML 文本块，"
                "宿主解析后真实执行并回执。也不要探索文件系统。\n\n" + _tp)
        if options.system:
            parts.append(f"【系统·宿主注入】\n{options.system}")
        transcript = serialize_transcript(options.messages)
        parts.append(transcript if transcript.strip() else "（空输入）")
        prompt_text = "\n\n".join(parts)

        tmpdir = tempfile.mkdtemp(prefix="devincli-")
        prompt_file = os.path.join(tmpdir, "prompt.md")
        export_file = os.path.join(tmpdir, "session.json")
        proc = None
        stdout_task: Optional[asyncio.Task] = None
        stderr_task: Optional[asyncio.Task] = None
        aborted = False
        timed_out = False
        try:
            with open(prompt_file, "w", encoding="utf-8") as fh:
                fh.write(prompt_text)
            argv = [
                conn.exe, "-p",
                "--prompt-file", prompt_file,
                "--export", export_file,
                "--permission-mode", conn.permission_mode or "auto",
                "--model", str(options.model),
                "--respect-workspace-trust", "false",
            ]
            env = _child_env(conn.env_extra)
            cwd = conn.work_dir or tempfile.gettempdir()
            spawn = self._config.spawn or _real_spawn
            logger.info("[devincli] 起进程 model=%s cwd=%s", options.model, cwd)

            proc = await spawn(argv, env, cwd)
            # stdout/stderr 先挂 drain——poll wait 期间缓冲不会堵死。
            stdout_task = asyncio.create_task(proc.stdout.read())
            stderr_task = asyncio.create_task(proc.stderr.read())

            deadline = (time.monotonic() + float(conn.timeout)
                        if conn.timeout and conn.timeout > 0 else None)
            while proc.returncode is None:
                if getattr(options.signal, "aborted", False):
                    aborted = True
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                    break
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

            stdout = (await stdout_task if stdout_task else b"") or b""
            stderr = (await stderr_task if stderr_task else b"") or b""
            out_text = stdout.decode("utf-8", "replace").strip()
            err_tail = stderr.decode("utf-8", "replace").strip()[-600:]
            atif = _parse_atif(export_file) if os.path.exists(export_file) else {}
            text = str(atif.get("text") or "") or out_text

            if aborted:
                yield FinishChunk(reason=AbortedReason(
                    failure=LlmFailure("aborted by caller", "ABORTED")))
                return
            if timed_out:
                yield FinishChunk(reason=ErrorReason(failure=LlmFailure(
                    f"devin cli 超时（{conn.timeout}s）", "DEVINCLI_TIMEOUT")))
                return
            if proc.returncode not in (0, None) or not text:
                detail = err_tail or out_text or "devin cli 无正文输出"
                if _is_auth_error(detail):
                    yield FinishChunk(reason=ErrorReason(failure=LlmFailure(
                        f"devin 未登录/凭证失效：{detail}", "DEVINCLI_AUTH")))
                else:
                    yield FinishChunk(reason=ErrorReason(failure=LlmFailure(
                        f"devin exited {proc.returncode}: {detail}",
                        "DEVINCLI_EXIT", status=proc.returncode)))
                return

            # ---- 成功：末轮思考（若有）→ 正文块 → usage → finish ----
            if atif.get("reasoning"):
                yield ReasoningDelta(index=0, text=str(atif["reasoning"]))
                yield BlockEnd(index=0, block=ReasoningBlock(
                    text=str(atif["reasoning"])))
            yield BlockStart(index=1, block_type="text")
            yield TextDelta(index=1, text=text)
            yield BlockEnd(index=1, block=TextBlock(text=text))
            usage = _usage_from_metrics(atif.get("metrics") or {})
            if usage is not None:
                yield UsageChunk(usage=usage)
            yield FinishChunk(reason=StopReason())
        except LlmError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LlmError(f"devin cli transport failed: {exc}",
                           "TRANSPORT", cause=exc) from exc
        finally:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await proc.wait()
                except Exception:  # noqa: BLE001
                    pass
            for task in (stdout_task, stderr_task):
                if task is not None:
                    try:
                        if not task.done():
                            task.cancel()
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
            for name in (prompt_file, export_file):
                try:
                    if os.path.exists(name):
                        os.unlink(name)
                except OSError:
                    pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass
