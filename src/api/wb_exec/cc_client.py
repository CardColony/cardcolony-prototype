# -*- coding: utf-8 -*-
"""**Command Code 执行体**（2026-11-04 加）——第五路候选。

**它是什么**：``command-code``（npm）＝"Best coding agent for open models"的
CLI 代理（Claude / GPT / Kimi / DeepSeek / GLM / Qwen / MiniMax 都通吃），
自带账号态（``COMMAND_CODE_API_KEY``）＋ **NDJSON 事件流**。

**形态**（实测 2026-11-04）::

    node <dist/index.mjs> -p "<任务正文>" -m <模型>
         --yolo --trust --skip-onboarding --no-session
         --tools-all --output-format json --max-turns N

★ **三个实测出来的坑（都写在这里，别再踩）**：
1. **必须用 ``--yolo`` 旗标**，``--permission-mode yolo`` **不够** ——
   对照实验（同一模型同一任务）：
   ``--yolo`` 14.6s 零拦截／``--dangerously-skip-permissions`` 14.8s 零拦截／
   ``--permission-mode yolo`` 30.6s（**第一次工具调用被拦一次**，
   agent 得重试或换路，慢一倍且可能半途放弃）／**不给 ⇒ 完全写不了**。
   上游原话：``Use --yolo ... to enable file writes and shell commands in print mode``。
2. **``--tools-all``**：print 模式会**扣掉一部分工具** ⇒ 卡要写文件/跑命令 ⇒ 必须给。
3. **目录里的 FREE 标签会过期**：``poolside/laguna-s-2.1-free`` 目录还标 FREE，
   实调上游说「**free tier has ended**」⇒ **标签不等于能用，必须真调**。
   ⇒ 本次实测**真能用的免费档**（真调出活、文件真落地）：
   ``inclusionai/ling-3.1-flash-1019:free``（20.2s）、
   ``reflection/beam:free``（15.8s）、
   ``stealth/glyph-cluster:free``（23.3s）。

**会话不落盘**：``--no-session`` ⇒ 只在内存里 ⇒ **客户端里不留对话**
⇒ 红线「千万别删除对话」天然满足（**没有需要清理的东西**）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("api.wb_exec.cc")

#: 默认入口（npm 全局安装）。
DEFAULT_ENTRY = (Path(os.environ.get("APPDATA", "") or "") / "npm" / "node_modules"
                 / "command-code" / "dist" / "index.mjs")
#: 默认模型（免费档，实测能出活）。
DEFAULT_MODEL = "inclusionai/ling-3.1-flash-1019:free"
#: **一轮最多几个 turn**（＝ command-code CLI 自己的默认值 100）。
#: ★ 2026-11-04：曾设 8 ⇒ 复杂开发任务**只够"摸清代码"** ⇒ 撞上限 rc=8
#:   ⇒ 被判"失败·无正文" ⇒ 白跑一轮。⇒ 用户令改回 **100**。
#: **0 ＝ 不传 `--max-turns` ⇒ CLI 不设轮数上限**（2026-11-04）。
#: ★ 教训：`--max-turns` 是**我们自己传进去的**（CLI 默认 100）⇒ 我们自己把活儿
#:   卡在 100 轮上 ⇒ 复杂开发任务"根本完不成"（rc=8）。
#:   实测 CLI 的实现 `runTurn({...void 0===a?{}:{maxTurns:a}})` ⇒ **不传就不限**。
DEFAULT_MAX_TURNS = 0
#: Windows 命令行上限 32767 ⇒ 给 prompt 留余量（**尾部**的回显指令不能丢）。
MAX_PROMPT_CHARS = 28000

#: ★ command-code 自己的退出码表（实测自它的 bundle 里的 `Qx`）。
#: ⇒ **按码分类处理**，别把所有非 0 都当"传输失败"（那会让同一路反复白跑）。
CC_EXIT_KINDS: dict[int, str] = {
    0: "ok",
    1: "error",
    3: "auth",                   # AUTH_ERROR
    4: "permission",             # PERMISSION_DENIED
    5: "rate_limit",             # RATE_LIMITED（429）
    6: "connection",             # CONNECTION_ERROR
    7: "server",                 # SERVER_ERROR
    8: "max_turns",              # MAX_TURNS_REACHED（**干了活但没收尾**）
    9: "no_response",            # NO_RESPONSE（真没正文）
    10: "insufficient_credits",  # INSUFFICIENT_CREDITS
    130: "interrupted",
}


def exit_kind(rc: int) -> str:
    """退出码 ⇒ 语义分类（未知码按 ``error``）。"""
    return CC_EXIT_KINDS.get(int(rc), "error")


def _clip_prompt(text: str) -> str:
    """prompt 超长 ⇒ **掐中间、保头尾**（尾部有回显指令，掐了就废）。"""
    s = str(text or "")
    if len(s) <= MAX_PROMPT_CHARS:
        return s
    head = s[:9000]
    tail = s[-16000:]
    return (head + "\n\n……（中间已截断，完整内容见工作目录里的卡面事实）……\n\n"
            + tail)


class CcError(RuntimeError):
    """Command Code 调用失败。"""


class CcRunner:
    """起一个 ``command-code`` 进程跑一轮（**一个进程 = 一个槽**）。"""

    def __init__(self, entry: str, *, node_exe: str = "node",
                 api_key_env: str = "COMMAND_CODE_API_KEY",
                 model: str = DEFAULT_MODEL,
                 max_turns: int = DEFAULT_MAX_TURNS,
                 timeout: float = 3600.0) -> None:
        self.entry = str(entry or "")
        self.node_exe = str(node_exe or "node")
        self.api_key_env = str(api_key_env or "COMMAND_CODE_API_KEY")
        self.model = str(model or DEFAULT_MODEL)
        # ★ 0 ＝ **不限轮数**（原来 `or 8` 会把 0 变成 8 ⇒ 表达不了"不限"）
        self.max_turns = int(max_turns or 0)
        self.timeout = float(timeout)

    # ---------------------------------------------------------------- 命令
    def build_argv(self, prompt: str, model: str = "",
                   max_turns: int = 0) -> list[str]:
        budget = int(max_turns or self.max_turns or 0)
        return [
            self.node_exe, self.entry,
            "-p", _clip_prompt(prompt),
            "-m", str(model or self.model),
            # ★ 必须给 --yolo（旗标），--permission-mode yolo 不够（见文件头实测）
            "--yolo",
            "--trust",              # 免掉项目信任交互（print 模式不能问）
            "--skip-onboarding",    # 免掉 taste onboarding
            "--no-session",         # ★ 不落盘 ⇒ 客户端里不留对话（红线）
            "--tools-all",          # ★ print 模式默认扣工具 ⇒ 卡要写文件/跑命令
            "--output-format", "json",
            # ★ **不传 `--max-turns` ⇒ CLI 不设轮数上限**
            #   （实测其实现：`runTurn({...void 0===a?{}:{maxTurns:a}})`）
            #   ⇒ 轮数不再是我们自己给自己设的假瓶颈；真边界＝**我们的超时**。
            *([] if budget <= 0 else ["--max-turns", str(budget)]),
        ]

    # ---------------------------------------------------------------- 跑
    async def run(self, prompt: str, work_dir: str, model: str = "",
                  on_event: Optional[Callable[[str, dict], None]] = None,
                  max_turns: int = 0) -> dict[str, Any]:
        """跑一轮 ⇒ ``{ok, text, tools, session_id, usage, errors, rc, …}``。

        ``on_event(kind, payload)``：**边跑边回调**（工具调用/正文/思考）
        ⇒ 卡面上能**实时**看到它在干什么（比 Devin 的 ATIF 事后解析强）。

        **不抛异常**：失败也返回结构化结果（上层据此换候选/如实报错）。
        """
        work_dir = str(work_dir)
        os.makedirs(work_dir, exist_ok=True)
        key = os.environ.get(self.api_key_env, "")
        if not key:
            return {"ok": False, "text": "", "tools": [], "errors": [
                f"没有 {self.api_key_env}（.env 或环境变量都没有）"],
                "rc": -1, "session_id": "", "usage": {}}
        argv = self.build_argv(prompt, model, max_turns)
        env = dict(os.environ)
        env[self.api_key_env] = key
        env["NO_COLOR"] = "1"
        logger.info("[cc] 起进程 model=%s cwd=%s", model or self.model, work_dir)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=work_dir, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=8 * 1024 * 1024)   # ★ 长行不许把读取打挂
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": "", "tools": [], "errors": [
                f"起进程失败：{type(exc).__name__}: {exc}"],
                "rc": -1, "session_id": "", "usage": {}}

        tools: list[dict[str, Any]] = []
        errors: list[str] = []
        texts: list[str] = []
        final_text = ""
        session_id = ""
        usage: dict[str, Any] = {}
        done = asyncio.Event()

        async def _drain_stderr() -> None:
            """★ stderr **必须一直抽干**（否则子进程写日志时阻塞 ⇒ 回包发不出来）。"""
            assert proc.stderr is not None
            try:
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        break
                    s = line.decode("utf-8", "replace").strip()
                    if s and ("error" in s.lower() or "warn" in s.lower()):
                        logger.debug("[cc] stderr: %s", s[:200])
            except Exception:  # noqa: BLE001
                pass

        async def _read_stdout() -> None:
            assert proc.stdout is not None
            state: dict[str, Any] = {}
            try:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    s = line.decode("utf-8", "replace").strip().lstrip("\ufeff")
                    if not s:
                        continue
                    try:
                        obj = json.loads(s)
                    except Exception:
                        continue
                    await _handle_event(obj, tools, errors, texts, on_event, state)
                    if state.get("final_text"):
                        final_text = str(state["final_text"])
                    if state.get("session_id"):
                        session_id = str(state["session_id"])
                    if state.get("usage"):
                        usage = dict(state["usage"])
            except Exception as exc:  # noqa: BLE001
                errors.append(f"事件流异常：{type(exc).__name__}: {exc}")
            finally:
                done.set()

        stderr_task = asyncio.create_task(_drain_stderr())
        stdout_task = asyncio.create_task(_read_stdout())
        try:
            await asyncio.wait_for(done.wait(), timeout=self.timeout)
            rc = await proc.wait()
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            errors.append(f"超时（{int(self.timeout)}s）")
            rc = -1
        finally:
            stdout_task.cancel()
            stderr_task.cancel()

        text_all = final_text or "".join(texts)
        kind = exit_kind(rc)
        # ★ **rc≠0 时 stdout 仍可能带着正文**（rc=8＝轮数用尽，agent 干了活
        #   但没收尾）⇒ **不许因为 rc≠0 就说"无正文"**。
        ok = (rc == 0) and bool(text_all) and not errors
        return {"ok": ok, "text": text_all, "tools": tools,
                "errors": errors, "rc": rc, "kind": kind,
                "session_id": session_id, "usage": usage,
                # ★ 把 stdout/stderr 片段带回去 ⇒ 出错时能一眼看出原因
                "stdout_tail": (stdout or "")[-4000:],
                "stderr_tail": (stderr or "")[-2000:]}


# ===================================================================== 事件解析
async def _handle_event(obj: dict, tools: list, errors: list, texts: list,
                         on_event: Optional[Callable[[str, dict], None]],
                         state: dict) -> None:
    """把一条 NDJSON 事件翻译成我们要的（kind, payload）。

    **事件类型全集**（实测 2026-11-04 成功跑）：
    ``run_start → turn_start → message_start → model_request_start →
    model_trace → thinking_start → thinking_delta → thinking_end →
    message_update → model_request_end → message_end → tool_queued →
    tool_running → tool_completed → turn_end → text_delta → run_end → result``
    """
    etype = str(obj.get("type") or "")
    ev = obj.get("event") if isinstance(obj.get("event"), dict) else {}
    sub = str((ev or {}).get("type") or "")

    if etype == "result":
        state["final_text"] = str(obj.get("finalText") or "")
        state["session_id"] = str(obj.get("sessionId") or "")
        state["usage"] = obj.get("usage") or {}
        if str(obj.get("subtype") or "") == "error":
            errors.append(str(obj.get("error") or "command-code 报错")[:300])
        _fire(on_event, "final", {"text": state["final_text"],
                                  "session_id": state["session_id"]})
        return

    if sub == "run_start":
        state["session_id"] = str(ev.get("sessionId") or "")
        return
    if sub == "tool_queued":
        rec = {"id": str(ev.get("toolCallId") or ""),
               "name": str(ev.get("toolName") or ""),
               "input": ev.get("input") or {},
               "status": "running", "output": ""}
        tools.append(rec)
        _fire(on_event, "tool_call", rec)
        return
    if sub == "tool_running":
        for rec in tools:
            if rec["id"] and rec["id"] == str(ev.get("toolCallId") or ""):
                rec["status"] = "running"
        return
    if sub == "tool_completed":
        cid = str(ev.get("toolCallId") or "")
        out = ""
        for part in (ev.get("result") or []):
            if isinstance(part, dict) and str(part.get("type")) == "text":
                out += str(part.get("text") or "")
        rec = None
        for r in tools:
            if r["id"] and r["id"] == cid:
                rec = r
        if rec is None:
            rec = {"id": cid, "name": str(ev.get("toolName") or ""),
                   "input": {}, "status": "completed", "output": ""}
            tools.append(rec)
        rec["status"] = "completed"
        rec["output"] = out[:2000]
        _fire(on_event, "tool_result", rec)
        return
    if sub == "tool_hook_blocked":
        errors.append(str(ev.get("hookOutput") or "工具被拦截")[:300])
        return
    if sub == "run_error":
        err = ev.get("error") or {}
        errors.append(str(err.get("message") or ev.get("type") or "run_error")[:300])
        return
    if sub == "text_delta":
        d = str(ev.get("delta") or "")
        if d:
            texts.append(d)
            _fire(on_event, "text_delta", {"text": d})
        return
    if sub == "thinking_end":
        t = str(ev.get("text") or "")
        if t:
            _fire(on_event, "thinking", {"text": t[:2000]})
        return
    if sub == "run_end":
        res = ev.get("result") or {}
        state["final_text"] = str(res.get("finalText") or "")
        state["usage"] = res.get("usage") or {}
        nxt = res.get("nextState") or {}
        if isinstance(nxt, dict) and nxt.get("sessionId"):
            state["session_id"] = str(nxt["sessionId"])
        return


def _fire(on_event: Optional[Callable[[str, dict], None]],
          kind: str, payload: dict) -> None:
    if on_event is None:
        return
    try:
        on_event(kind, payload)
    except Exception as exc:  # noqa: BLE001 回调失败不能拖垮主流程
        logger.debug("[cc] 事件回调失败 %s: %s", kind, exc)
