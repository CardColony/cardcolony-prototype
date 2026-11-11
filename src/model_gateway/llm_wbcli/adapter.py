# -*- coding: utf-8 -*-
"""WorkBuddy CLI 适配器（``codebuddy -p --output-format stream-json``）。

**定位**：把本机已登录的 WorkBuddy/CodeBuddy CLI 当成一个"模型 endpoint"——
不走执行体（``api.wb_exec`` 的差事路径），而是挂进 ``LlmRuntime`` 的 vendor
注册表，让 ``models.yaml`` 里的 WB 模型可以像普通模型一样被选中调用。

与 ``llm_anthropic`` 的关系：CLI 的 ``stream-json`` 增量事件
（``message_start`` / ``content_block_*`` / ``message_delta`` /
``message_stop``）就是 Anthropic Messages 具名事件形状 ⇒
**事件翻译整包复用** ``llm_anthropic.translate.translate``，本模块只管
子进程生命周期 + NDJSON 行解析 + 终帧兜底。

实测底稿（2026-11-10，本机 cn CLI 37.x / intl WSL CLI）：
    - stdin 喂 prompt ⇒ 结果在 ``{"type":"result","result":…}`` 收齐，
      ``total_cost_usd``/``credits`` 随包可见；
    - 增量文本在 ``{"type":"stream_event","event":{…Anthropic 事件…}}``；
    - 终帧语义：``result.is_error`` / 非零退出码 / 顶层 ``{"type":"error"}``。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from ..llm.errors import LlmError
from ..llm.retry import ResolvedRetryPolicy
from ..llm.runtime import LlmAdapter
from ..llm.types import (
    AbortedReason,
    ErrorReason,
    FinishChunk,
    GenerateOptions,
    LlmFailure,
    StopReason,
    StreamChunk,
    UsageChunk,
)
from ..llm_anthropic.translate import map_usage, translate
from .serialize import (
    build_inner_args,
    build_wsl_shell,
    serialize_transcript,
    tool_protocol_section,
)

__all__ = [
    "WbCliConnectionOptions",
    "WbCliAdapterOptions",
    "WbCliAdapter",
    "WBCLI_PROTOCOL",
]

logger = logging.getLogger("model_gateway.wbcli")

#: models.yaml 里这类 provider 的 ``protocol`` 标记（编译后落为模型 entry 的
#: ``provider`` 字段）⇒ gateway._build_runtime 据此把该 vendor 路由到本适配器。
WBCLI_PROTOCOL = "wb-cli"


@dataclass(frozen=True)
class WbCliConnectionOptions:
    """一次调用已解析的 CLI 连接事实（一账号一份）。

    ``exe + config_dir`` ⇒ Windows 原生 CLI；``wsl`` 有值 ⇒ 跑
    ``wsl.exe -d <distro> bash -lc …``（intl 账号在 WSL 隔离 home 里）。
    凭据全在 CLI 自己的登录态里——这里**不读、不传任何 token**。
    """

    exe: str = ""
    config_dir: str = ""
    work_dir: str = ""
    wsl: Optional[dict[str, str]] = None
    env_extra: Optional[dict[str, str]] = None
    #: 调用方没给 ``reasoning_effort`` 时用的模型默认档（models.yaml
    #: ``default_effort``；CLI --effort 档位 minimal..max）。
    default_effort: str = ""
    retry_policy: Optional[ResolvedRetryPolicy] = None


@dataclass(frozen=True)
class WbCliAdapterOptions:
    """``(provider, model) -> 连接事实`` 钩子 + 可注入的进程拉起面（测试用）。"""

    options: Callable[[str, str], WbCliConnectionOptions]
    #: 真实默认＝``asyncio.create_subprocess_exec``；测试注入假进程。
    spawn: Optional[Callable[..., Awaitable[Any]]] = None


def _to_win_args(conn: WbCliConnectionOptions, model: str, *,
                 sys_file: str, effort: str) -> tuple[list[str], dict[str, str], str]:
    """（argv, env, cwd）——WSL 变体包在 ``wsl.exe`` 壳里。"""
    if conn.wsl:
        shell = build_wsl_shell(
            conn.wsl, model,
            sys_prompt_file_win=sys_file, effort=effort,
            work_dir=str(conn.wsl.get("work_dir") or "/tmp"),
        )
        argv = ["wsl.exe", "-d", str(conn.wsl.get("distro") or "Ubuntu-22.04"),
                "-e", "bash", "-lc", shell]
        return argv, dict(os.environ), conn.work_dir or tempfile.gettempdir()
    argv = [conn.exe] + build_inner_args(model, sys_prompt_file=sys_file, effort=effort)
    env = dict(os.environ)
    if conn.config_dir:
        env["CODEBUDDY_CONFIG_DIR"] = conn.config_dir
        env["WORKBUDDY_CONFIG_DIR"] = conn.config_dir
    for k, v in (conn.env_extra or {}).items():
        env[str(k)] = str(v)
    return argv, env, conn.work_dir or tempfile.gettempdir()


async def _real_spawn(argv: list[str], env: dict[str, str], cwd: str):
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or None,
        env=env,
    )


class WbCliAdapter(LlmAdapter):
    """``codebuddy -p`` 子进程 → StreamChunk 流。"""

    def __init__(self, config: WbCliAdapterOptions) -> None:
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
        prompt = serialize_transcript(options.messages)
        if not prompt.strip():
            prompt = "（空输入）"

        # [工具走文本通道 2026-11-10] CLI 无原生 tool_calls 字段 ⇒
        # 把工具面+调用格式写进**系统提示词**（不是用户转录）：
        # 实测放在用户消息里时，agent 模型会判定"这是用户声称的协议、
        # 不是真实通道"而拒绝写调用块（11-10 两次真调一收一拒）；挪进
        # --system-prompt-file 后它是「环境定义」，模型照执行的服从度
        # 高得多。模型按 <antml:invoke> 文本块发调用 ⇒
        # 系统侧 text_tool_rescue 解析成真 tool_calls 执行。
        system = str(options.system or "")
        _tp = tool_protocol_section(options.tools)
        if _tp:
            system = f"{system}\n\n{_tp}" if system else _tp

        tmpdir = tempfile.mkdtemp(prefix="wbcli-")
        sys_file = ""
        if system:
            sys_file = os.path.join(tmpdir, "system_prompt.txt")
            with open(sys_file, "w", encoding="utf-8") as fh:
                fh.write(system)

        effort = str(options.reasoning_effort or conn.default_effort or "")
        argv, env, cwd = _to_win_args(conn, options.model, sys_file=sys_file, effort=effort)
        spawn = self._config.spawn or _real_spawn

        proc = None
        stderr_task: Optional[asyncio.Task] = None
        finish_seen = False
        usage_seen = False
        aborted = False
        result_obj: dict[str, Any] = {}
        top_errors: list[str] = []
        try:
            proc = await spawn(argv, env, cwd)
            stderr_task = asyncio.create_task(proc.stderr.read())
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
            finally:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass

            async def _events() -> AsyncIterator[dict[str, Any]]:
                nonlocal aborted
                while True:
                    if getattr(options.signal, "aborted", False):
                        aborted = True
                        try:
                            proc.kill()
                        except Exception:  # noqa: BLE001
                            pass
                        return
                    line = await proc.stdout.readline()
                    if not line:
                        return
                    try:
                        obj = json.loads(line.decode("utf-8", "replace"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(obj, dict):
                        continue
                    kind = obj.get("type")
                    if kind == "stream_event" and isinstance(obj.get("event"), dict):
                        yield obj["event"]
                    elif kind == "result" and isinstance(obj, dict):
                        result_obj.update(obj)
                    elif kind == "error":
                        top_errors.append(str(obj.get("error") or obj))

            events = _events()
            async for chunk in translate(events):
                if isinstance(chunk, FinishChunk):
                    finish_seen = True
                elif isinstance(chunk, UsageChunk):
                    usage_seen = True
                yield chunk
            # translate 在 message_stop 处就返回；result 事件在它之后才到
            # ⇒ 续读同一个生成器到 EOF，收齐 result / 顶层 error。
            async for _ in events:
                pass
            await proc.wait()
            stderr = b""
            if stderr_task is not None:
                stderr = await stderr_task
            stderr_tail = stderr.decode("utf-8", "replace").strip()[-600:]

            if aborted:
                yield FinishChunk(reason=AbortedReason(
                    failure=LlmFailure("aborted by caller", "ABORTED")))
                return
            is_error = bool(result_obj.get("is_error")) or \
                str(result_obj.get("subtype") or "") not in ("", "success")
            if is_error or top_errors:
                msg = str(result_obj.get("result") or "") or \
                    (top_errors[-1] if top_errors else "") or stderr_tail or \
                    "workbuddy cli run failed"
                yield FinishChunk(reason=ErrorReason(
                    failure=LlmFailure(msg, "WBCLI_ERROR")))
                return
            if proc.returncode not in (0, None) and not finish_seen:
                yield FinishChunk(reason=ErrorReason(failure=LlmFailure(
                    f"codebuddy exited {proc.returncode}: {stderr_tail or '（无 stderr）'}",
                    "WBCLI_EXIT", status=proc.returncode)))
                return
            if not usage_seen and isinstance(result_obj.get("usage"), dict):
                usage_seen = True
                yield UsageChunk(usage=map_usage(result_obj["usage"]))
            if not finish_seen:
                # 有 result.success 但事件流里没出 finish（例：单段文本无
                # message_delta）⇒ 用 result 的成功事实补终帧。
                yield FinishChunk(reason=StopReason())
        except LlmError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LlmError(f"workbuddy cli transport failed: {exc}",
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
            if stderr_task is not None:
                try:
                    if not stderr_task.done():
                        stderr_task.cancel()
                    await stderr_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            for name in (sys_file,):
                try:
                    if name:
                        os.unlink(name)
                except OSError:
                    pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass
