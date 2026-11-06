# -*- coding: utf-8 -*-
"""Devin 执行体（**走 Devin CLI**，2026-11-03）。

**为什么走 CLI 而不是 REST API**（实测）：
- REST ``api.devin.ai/v3`` 要 **服务用户 key（``cog_`` 开头）**；
  用户手里的是 **webapp 会话令牌（``apk_user_…``）** ⇒ ``/v3/*`` 全 403。
- 而 **Devin CLI 自带账号态**（``devin auth login`` 一次就够）⇒
  **直接用 CLI 吃账号额度**，不必再折腾 key。

**CLI 形态（官方文档 + 本地 ``share/devin/docs``）**：
- ``devin -p --prompt-file <f> --export <out.json>`` ⇒
  **非交互**（跑完打印结果就退出）＋ **导出 ATIF 轨迹**（工具调用在里头）
- ★ **默认走「本地 agent」**（**不加** ``--cloud``）——
  2026-11-04 实测：**云端会话跑在 Devin 的 VM 里 ⇒ 看不到本机的工作目录**
  ⇒ agent 只会回「这是你本机的路径，请批准本地访问」⇒ **0 次工具调用**
  ⇒ **卡片假装成功**（现场 card_010）。本地 agent 跑在**本机** ⇒ 能直接读写
  卡的工作目录（实测 ATIF 11 步、文件真写出）。
- ``--permission-mode bypass``（别名 ``dangerous``/``yolo``）⇒ 等价于我们的
  ``bypassPermissions``（否则它会停下来等审批 ⇒ **卡死**）。
- ``--respect-workspace-trust false`` ⇒ **非交互模式在未信任目录会直接失败**
  ⇒ 脚本里必须带上。
- ``--model swe-2-max`` ⇒ **精确的模型 id**。
  ★ **别写家族名**：``--model swe-2`` 会落到家族默认档（实测解析成
  ``swe-2-high``）；要哪个档就写哪个 id（``swe-2-high|medium|max``，
  价目表上这三个都标 **Free**，``-lightning`` 变体才收费）。

**ATIF** = Agent Trajectory Interchange Format（``--export`` 的输出）⇒
工具调用与最终回复都在里面 ⇒ 映射成我们已有的 ``wb_exec_*`` 事件。
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

logger = logging.getLogger("api.wb_exec.devin")

#: 默认模型（**用户套餐里免费**；价目表标 Free）。
DEFAULT_MODEL = "swe-2-max"
#: 权限模式：bypass（=dangerous/yolo）——否则 agent 会停下来等审批 ⇒ 卡死。
DEFAULT_PERMISSION_MODE = "bypass"


class DevinError(RuntimeError):
    """Devin CLI 调用失败。"""


#: ★★ **起 Devin CLI 时必须从环境里剔掉的变量**（2026-11-04 实测定案）。
#:
#: **现场**：推演agent 的服务是**从 Devin 桌面版（Windsurf IDE）的内置终端**起的
#:   （Git Bash ``nohup.exe ./.venv/Scripts/python.exe main.py``）⇒ 那个终端的环境里
#:   带着一整套 IDE 变量，其中最要命的是：:
#:
#:       ACP_BACKEND = windsurf
#:
#:   ⇒ 我们起的 ``devin.exe`` 子进程**继承了它** ⇒ CLI **改走 IDE 的凭据后端**
#:   ⇒ 那条路没有效会话 ⇒ 每次都回：:
#:
#:       Error: Not logged in. Run `devin.exe auth login` to authenticate.
#:
#:   ⇒ 表现：Devin 优先级最高 ⇒ **每张卡都被派给它** ⇒ 每张卡 7 秒后失败
#:   ⇒ 旧代码把"鉴权失败"当成整卡失败 ⇒ **卡全死**（现场 18:00-18:06 连死 28 次）。
#:
#: ★ **A/B 实测矩阵**（2026-11-04，用生产进程环境的真实值）：
#:   - 干净环境 ⇒ **rc=0**（出活）；
#:   - 只加 ``ACP_BACKEND=windsurf`` ⇒ **rc=1 Not logged in**；
#:   - 全套 IDE 变量（含 ACP_BACKEND）⇒ rc=1；
#:   - 全套 IDE 变量**但去掉 ACP_BACKEND** ⇒ **rc=0**（出活）。
#:   ⇒ **就这一个变量的事** ⇒ 其余 IDE 变量（WINDSURF_*/VSCODE_*）留着无害。
#:
#: ⇒ 所以：**这里把 ``ACP_BACKEND`` 剔掉**，不论服务是从哪个终端起的都能干活
#:   （而不是去要求"必须从干净的 shell 启动服务"——那种要求靠人记��住）。
_DEVIN_ENV_DROP = ("ACP_BACKEND",)


def _child_env() -> dict:
    """起 Devin CLI 用的环境：**继承父进程，但剔掉 IDE 污染变量**。"""
    env = dict(os.environ)
    for k in _DEVIN_ENV_DROP:
        env.pop(k, None)
    return env


class DevinRunner:
    """起一个 ``devin --cloud -p`` 进程跑一轮（**一个进程 = 一个槽**）。"""

    def __init__(self, cli_exe: str, *, model: str = DEFAULT_MODEL,
                 permission_mode: str = DEFAULT_PERMISSION_MODE,
                 cloud: bool = True,
                 timeout: float = 3600.0,
                 verbosity: int = 1) -> None:
        self.cli_exe = str(cli_exe)
        self.model = str(model or DEFAULT_MODEL)
        self.permission_mode = str(permission_mode or DEFAULT_PERMISSION_MODE)
        self.cloud = bool(cloud)
        self.timeout = float(timeout)
        self.verbosity = int(verbosity)

    # ---------------------------------------------------------------- 命令
    def build_argv(self, prompt_file: str, export_file: str,
                   model: str = "") -> list[str]:
        argv = [self.cli_exe]
        if self.cloud:
            argv.append("--cloud")
        argv += ["-p", "--prompt-file", str(prompt_file),
                 "--export", str(export_file),
                 "--permission-mode", self.permission_mode,
                 "--model", str(model or self.model),
                 "--respect-workspace-trust", "false",
                 "--verbosity", str(self.verbosity)]
        return argv

    async def run(self, prompt: str, work_dir: str, model: str = "",
                  marker: str = "",
                  on_event: Optional[Callable[[str, dict], None]] = None
                  ) -> dict[str, Any]:
        """跑一轮 ⇒ ``{ok, text, export_path, stdout, stderr, rc, session_id}``。

        ``model`` 覆盖默认模型（**候选表里写什么就用什么** —— 候选表是唯一事实源；
        注意**别写家族名**：``swe-2`` 会落到家族默认档，实测解析成 ``swe-2-high``）。

        ★ **实时事件**（2026-11-04 加）：`-p` 的工具事件**要等进程退出**才能从
        ATIF 里解析出来 ⇒ 卡面在跑的这几分钟**一个字都没有**（看着像"没回应"）
        ⇒ 这里**并行轮询 Devin 自己的会话库**
        （``%APPDATA%/devin/cli/sessions.db`` 的 ``message_nodes``）
        ⇒ **边跑边**把 ``assistant`` / ``tool`` 消息映射成
        工具调用/工具结果/正文/思考事件 ⇒ 卡面实时有回应。

        ★ **怎么认"自己的那条会话"**：用**分段标记**（``【推演agent 任务卡 …】``）
        在 ``role=user`` 消息里定位 ⇒ **同 work 目录里别的会话（用户的）不会被算进来**。

        **不抛异常**：失败也返回结构化结果（上层据此换候选/如实报错）。
        """
        work_dir = str(work_dir)
        os.makedirs(work_dir, exist_ok=True)
        tmpdir = tempfile.mkdtemp(prefix="devin_exec_")
        prompt_file = os.path.join(tmpdir, "prompt.md")
        export_file = os.path.join(tmpdir, "session.json")
        try:
            with open(prompt_file, "w", encoding="utf-8") as fh:
                fh.write(str(prompt))
            argv = self.build_argv(prompt_file, export_file, model)
            logger.info("[devin] 起进程 model=%s cloud=%s cwd=%s",
                        model or self.model, self.cloud, work_dir)
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=work_dir, env=_child_env(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=8 * 1024 * 1024)
            poller = (asyncio.create_task(_poll_live_events(marker, on_event))
                      if (marker and on_event) else None)
            # ★ **日志尾随**（永远可用的实时源）：``-p`` 不落库、stdout 收工才吐
            #   ⇒ 只剩 CLI 自己的日志里有逐次工具调用 ⇒ 按 pid 精确认领。
            tailer = (asyncio.create_task(_tail_devin_log(proc.pid, on_event))
                      if on_event else None)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), self.timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                for _tg in (poller, tailer):
                    if _tg is not None:
                        _tg.cancel()
                return {"ok": False, "text": "", "export_path": "",
                        "stdout": "", "stderr": f"超时（{self.timeout}s）", "rc": -1,
                        "session_id": ""}
            finally:
                for _tg in (poller, tailer):
                    if _tg is not None:
                        _tg.cancel()
            stdout = (out or b"").decode("utf-8", "replace")
            stderr = (err or b"").decode("utf-8", "replace")
            rc = proc.returncode
            parsed = parse_atif(export_file) if os.path.exists(export_file) else {}
            text = parsed.get("text") or stdout.strip()
            ok = rc == 0 and bool(text)
            if not ok:
                logger.warning("[devin] 进程 rc=%s 无正文；stderr=%s",
                               rc, stderr.strip())
            return {"ok": ok, "text": text, "export_path": export_file,
                    "stdout": stdout, "stderr": stderr, "rc": rc,
                    "atif": parsed,
                    # ★ **会话 id**（ATIF 里带的）⇒ 卡片做完后**精确**删掉
                    #   **我们自己这一条**会话（见 cleanup.py 的红线纪律）。
                    "session_id": str(parsed.get("session_id") or "")}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------- ATIF 解析
def parse_atif(path: str) -> dict[str, Any]:
    """解析 ``--export`` 出来的 **ATIF 轨迹**（实测 schema ``ATIF-v1.7``）。

    **真实结构**（2026-11-01 实测）：
    ```json
    {"schema_version":"ATIF-v1.7","session_id":"…",
     "agent":{"name":"devin","model_name":"SWE-2 High","tool_definitions":[…]},
     "steps":[{"step_id":10,"timestamp":"…","source":"agent",
               "message":"…", "reasoning_content":"…",
               "tool_calls":[{"tool_call_id":"exec:0#…",
                              "function_name":"exec",
                              "arguments":{"command":"…"}}],
               "observation":{"results":[{"source_call_id":"exec:0#…",
                                         "content":"Output from command…"}]},
               "metrics":{…}}],
     "final_metrics":{"total_prompt_tokens":…,"total_steps":…}}
    ```
    ⇒ **工具调用在 step 里**（不是独立数组），**工具结果在同 step 的 observation**，
    **最终正文 = 最后一条 ``source=="agent"`` 且 ``message`` 非空的 step**。

    ⇒ 返回 ``{"text": str, "tools": [{"name","status","input","output"}],
    "session_id": str, "raw": dict}``
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[devin] ATIF 解析失败 %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    steps = data.get("steps") or []
    tools: list[dict[str, Any]] = []
    text = ""
    for st in steps:
        if not isinstance(st, dict):
            continue
        # 工具结果先按 source_call_id 归拢（observation 在同一步的 tool_calls 之后）
        results: dict[str, str] = {}
        obs = st.get("observation") or {}
        for r in (obs.get("results") or []):
            if isinstance(r, dict):
                results[str(r.get("source_call_id") or "")] = str(r.get("content") or "")
        for tc in (st.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            name = str(tc.get("function_name") or tc.get("name") or "")
            if not name:
                continue
            cid = str(tc.get("tool_call_id") or tc.get("id") or "")
            tools.append({
                "name": name,
                "status": "completed" if cid in results else "running",
                "input": tc.get("arguments") or tc.get("input") or {},
                "output": results.get(cid, ""),
            })
        msg = str(st.get("message") or "").strip()
        if msg and str(st.get("source") or "") == "agent":
            text = msg
    if not text:
        for st in reversed(steps):
            if isinstance(st, dict):
                m = str(st.get("message") or "").strip()
                if m:
                    text = m
                    break
    return {"text": text, "tools": tools,
            # ★ **会话 id**（顶层字段）⇒ 用来精确删掉"我们自己这一条"会话。
            "session_id": str(data.get("session_id") or ""),
            "raw": data}


# ===================================================================== 实时事件
def _sessions_db_path() -> str:
    """Devin CLI 的会话库（**只读**打开）。"""
    return str(Path(os.environ.get("APPDATA", "")) / "devin" / "cli" / "sessions.db")


async def _poll_live_events(marker: str,
                            on_event: Callable[[str, dict], None],
                            interval: float = 1.5) -> int:
    """**轮询 Devin 会话库 ⇒ 边跑边出事件**（卡面实时有回应）。

    ★ **只处理"我们自己那条会话"**：``role=user`` 消息里带**分段标记**的那条
    ⇒ 同目录（甚至同 work）的别的会话不会被算进来。

    ⇒ 返回发了多少条事件（诊断用）。
    """
    import sqlite3

    db = _sessions_db_path()
    if not os.path.exists(db):
        return 0
    con = None
    last_row = 0
    locked_sid = ""
    emitted = 0
    try:
        while True:
            try:
                if con is None:
                    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
                cur = con.cursor()
                # ★ **增量**：只取新追加的行（row_id 是主键 ⇒ 极便宜）
                rows = cur.execute(
                    "SELECT row_id, session_id, chat_message FROM message_nodes "
                    "WHERE row_id > ? ORDER BY row_id LIMIT 500",
                    (last_row,)).fetchall()
                for row_id, sid, raw in rows:
                    last_row = max(last_row, int(row_id))
                    try:
                        msg = json.loads(raw or "{}")
                    except Exception:  # noqa: BLE001
                        continue
                    if not isinstance(msg, dict):
                        continue
                    role = str(msg.get("role") or "")
                    content = str(msg.get("content") or "")
                    if role == "user":
                        # ★ **靠分段标记认领自己的会话**
                        if marker and marker in content:
                            locked_sid = str(sid)
                            logger.info("[devin] 认领会话 %s（实时事件已接上）", sid)
                        continue
                    if not locked_sid or str(sid) != locked_sid:
                        continue
                    if role == "assistant":
                        th = msg.get("thinking") or {}
                        if isinstance(th, dict) and th.get("thinking"):
                            _fire(on_event, "thinking",
                                  {"text": str(th["thinking"])})
                        if content.strip():
                            _fire(on_event, "text", {"text": content})
                        for tc in (msg.get("tool_calls") or []):
                            if not isinstance(tc, dict):
                                continue
                            _fire(on_event, "tool_call", {
                                "id": str(tc.get("id") or ""),
                                "name": str(tc.get("name") or ""),
                                "input": tc.get("arguments") or {},
                            })
                            emitted += 1
                    elif role == "tool":
                        _fire(on_event, "tool_result", {
                            "id": str(msg.get("tool_call_id") or ""),
                            "name": "",
                            "ok": True,
                            "summary": content,
                        })
                        emitted += 1
            except Exception as exc:  # noqa: BLE001 轮询失败不许拖垮执行
                logger.debug("[devin] 实时轮询异常：%s", exc)
                try:
                    if con is not None:
                        con.close()
                except Exception:  # noqa: BLE001
                    pass
                con = None
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        return emitted
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:  # noqa: BLE001
            pass


def _fire(on_event: Optional[Callable[[str, dict], None]],
          kind: str, payload: dict) -> None:
    if on_event is None:
        return
    try:
        on_event(kind, payload)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[devin] 事件回调失败 %s: %s", kind, exc)


def _find_acp_child(pid: int) -> int:
    """找 ``devin -p`` 派生的 **``devin acp`` 子进程** pid（找不到 ⇒ 0）。

    ★ 只按**父子关系**认领 ⇒ 不会认到用户 Devin Desktop 的进程。
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001 没装 psutil ⇒ 用不上这条路
        return 0
    try:
        parent = psutil.Process(int(pid))
    except Exception:  # noqa: BLE001 进程已退出
        return 0
    try:
        for child in parent.children(recursive=True):
            try:
                cmd = child.cmdline() or []
            except Exception:  # noqa: BLE001
                continue
            if any(str(x).strip() == "acp" for x in cmd):
                return int(child.pid)
    except Exception:  # noqa: BLE001
        return 0
    return 0


async def _tail_devin_log(pid: int,
                          on_event: Callable[[str, dict], None],
                          interval: float = 0.5) -> int:
    """**tail 我们自己那个 Devin CLI 进程的日志** ⇒ 实时给出"它正在干什么"。

    ★ **只 tail 自己**：``-p`` 会派生一个 ``devin acp`` 子进程，**工具调用的日志在
      子进程那份里** ⇒ 用 psutil 按父子关系找到它 ⇒ 按 pid 精确认领日志文件
      ⇒ **绝不碰用户 Devin Desktop 的日志**。
    ★ 日志格式变了就**跳过**（解析失败不影响主流程）。
    """
    import re
    import time

    log_dir = Path(os.environ.get("APPDATA", "")) / "devin" / "cli" / "logs"
    # ① 等 ACP 子进程出生（`-p` 起来后很快就有）
    target_pid = 0
    for _ in range(25):
        target_pid = _find_acp_child(pid)
        if target_pid:
            break
        await asyncio.sleep(0.4)
    if not target_pid:
        target_pid = int(pid)
    # ② 找它的日志文件（文件名带 pid ⇒ 精确认领）
    path = None
    for _ in range(20):
        try:
            cands = sorted(log_dir.glob(f"devin_*_{target_pid}.log"),
                           key=lambda q: q.stat().st_mtime, reverse=True)
        except Exception:  # noqa: BLE001
            cands = []
        if cands:
            path = cands[0]
            break
        await asyncio.sleep(0.3)
    if path is None:
        logger.info("[devin] 没找到 pid=%s 的日志 ⇒ 不做实时心跳", target_pid)
        return 0

    # ③ 各类工具的行（**按出现顺序**，谁先来先发）
    pat_exec = re.compile(r'toolbox::tools::exec: shell=NonInteractive \{ '
                          r'shell: (\w+), command: "(.*?)", login:')
    pat_read = re.compile(r"toolbox::tools::read: Reading file: (.+?)\s*$")
    pat_write = re.compile(r"toolbox::tools::write: Writing to file: (.+?)\s*$")
    pat_edit = re.compile(r"toolbox::tools::edit: Editing file: (.+?)\s*$")
    pat_grep = re.compile(r"toolbox::tools::grep: Searching for pattern: "
                          r"'(.*?)' in: (.+?)\s*$")
    pat_tool = re.compile(r"Permission decision for tool ([A-Za-z0-9_.-]+)")

    emitted = 0
    pending: tuple[str, float] = ("", 0.0)

    def _flush_pending(now: float) -> None:
        """裸工具名等 1.5 秒还没等到具体行 ⇒ 也发出去（别丢事件）。"""
        nonlocal pending, emitted
        name, ts = pending
        if not name:
            return
        if now - ts < 1.5:
            return
        pending = ("", 0.0)
        _fire(on_event, "tool_call",
              {"id": "", "name": name, "input": {}, "live": "log"})
        emitted += 1

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            # ★ **从头读**（不是从末尾）：工具调用往往发生在**前几秒**，
            #   从末尾 seek 会全部错过 ⇒ 卡面又变成空白。
            #   文件名带 pid ⇒ 一定是本次进程的日志 ⇒ 不会重放别人的。
            fh.seek(0)
            while True:
                line = fh.readline()
                if not line:
                    _flush_pending(time.time())
                    await asyncio.sleep(interval)
                    continue
                now = time.time()
                m = pat_exec.search(line)
                if m:
                    pending = ("", 0.0)
                    _fire(on_event, "tool_call", {
                        "id": "", "name": "exec", "live": "log",
                        "input": {"shell": m.group(1), "command": m.group(2)},
                    })
                    emitted += 1
                    continue
                m = pat_read.search(line)
                if m:
                    pending = ("", 0.0)
                    _fire(on_event, "tool_call", {
                        "id": "", "name": "read", "live": "log",
                        "input": {"path": m.group(1)},
                    })
                    emitted += 1
                    continue
                m = pat_write.search(line)
                if m:
                    pending = ("", 0.0)
                    _fire(on_event, "tool_call", {
                        "id": "", "name": "write", "live": "log",
                        "input": {"path": m.group(1)},
                    })
                    emitted += 1
                    continue
                m = pat_edit.search(line)
                if m:
                    pending = ("", 0.0)
                    _fire(on_event, "tool_call", {
                        "id": "", "name": "edit", "live": "log",
                        "input": {"path": m.group(1)},
                    })
                    emitted += 1
                    continue
                m = pat_grep.search(line)
                if m:
                    pending = ("", 0.0)
                    _fire(on_event, "tool_call", {
                        "id": "", "name": "grep", "live": "log",
                        "input": {"pattern": m.group(1), "path": m.group(2)},
                    })
                    emitted += 1
                    continue
                m = pat_tool.search(line)
                if m:
                    # 裸工具名 ⇒ **先攒着**（具体行通常在几十毫秒后）
                    if not pending[0]:
                        pending = (m.group(1), now)
                    _flush_pending(now)
    except asyncio.CancelledError:
        return emitted
    except Exception as exc:  # noqa: BLE001 日志读不了 ⇒ 不影响执行
        logger.debug("[devin] 日志尾随异常：%s", exc)
        return emitted


