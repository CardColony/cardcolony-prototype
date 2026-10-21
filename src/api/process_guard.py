# -*- coding: utf-8 -*-
"""进程护栏：不许 agent 按**镜像名**通杀解释器进程。

## 为什么必须有

[2026-10-21 实测事故 · 用户令] `work_018 / card_031`（coder 卡）在 5713 worktree
里干活，为了清理测试残留进程执行了：

    taskkill /F /IM python.exe

`/IM` 是**按镜像名**杀——不分青红皂白，凡是叫 `python.exe` 的全杀。而 5700 生产
服务**也是** `python.exe`。结果：23:41:26 这条命令执行，5700 当场消失，没有异常、
没有 traceback、没有事件日志——从外部看就是「服务被随意杀了」。

根因不是 Python，也不是操作系统：**工具层没有「不许杀宿主」这条护栏**。
按名字杀进程这个动作本身就不该出现在自主 agent 手里——它无法区分
「我起的测试进程」和「宿主服务」，而两者同名。

## 护栏口径（只拦「通杀」，不拦「精确杀」）

拒：
  · `taskkill ... /IM <python|pythonw|py|cmd>`（按镜像名）
  · `taskkill ... /IM *`（全杀）
  · `Stop-Process -Name python*` / `Get-Process ... | Stop-Process`
  · `pkill python` / `killall python`
  · `wmic process ... delete`
  · 按 PID 杀**宿主进程**（本进程 / 父进程 / `.server.pid` 里记的那个）

放行（给出路）：
  · `tasklist` / `Get-Process` 只查不杀；
  · `taskkill /PID <具体 pid> /F`，且该 pid 不在保护名单。
  ⇒ 要清理自己起的测试进程，先查出它的 pid 再精确杀。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

#: 通杀类特征：(正则, 说明)。全部按小写匹配。
_KILL_ALL_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"taskkill[^\n]*?/im\s+[\"']?(python|pythonw|python3|py)(\.exe)?\b",
     "按镜像名杀 python（宿主服务同名，会一起死）"),
    (r"taskkill[^\n]*?/im\s+[\"']?\*", "按镜像名杀所有进程"),
    (r"taskkill[^\n]*?/im\s+[\"']?(cmd|powershell|pwsh)(\.exe)?\b",
     "按镜像名杀 shell 进程"),
    (r"stop-process[^\n]*?-name\s+[\"']?(python|pythonw|py)", "Stop-Process -Name python"),
    (r"get-process[^\n]*?\|\s*stop-process", "Get-Process | Stop-Process（通杀）"),
    (r"\bpkill[^\n]*?\bpython", "pkill python"),
    (r"\bkillall[^\n]*?\bpython", "killall python"),
    (r"wmic\s+process[^\n]*?\bdelete\b", "wmic process delete（通杀）"),
)

#: Python 代码里的列表/关键字写法：taskkill 与 /IM 与 python 同时出现即判通杀。
_PY_LIST_FORM = re.compile(
    r"taskkill[\s\S]{0,80}?(/im|['\"]/im['\"])[\s\S]{0,40}?(python|pythonw)", re.I)

#: 按 PID 杀的写法（用来判「是不是杀宿主」）。
_KILL_BY_PID = re.compile(
    r"(?:taskkill[^\n]*?/pid\s+|stop-process[^\n]*?-id\s+|os\.kill\(\s*)(\d+)", re.I)


def _protected_pids() -> set[int]:
    """宿主进程集合：本进程、父进程、`.server.pid` 里记的那个。"""
    pids: set[int] = set()
    try:
        pids.add(int(os.getpid()))
    except Exception:  # noqa: BLE001
        pass
    try:
        pids.add(int(os.getppid()))
    except Exception:  # noqa: BLE001
        pass
    try:
        raw = Path(".server.pid").read_text(encoding="utf-8").strip()
        if raw.isdigit():
            pids.add(int(raw))
    except Exception:  # noqa: BLE001 读不到就不算
        pass
    return pids


def find_process_kill(text: str) -> list[str]:
    """返回命中的通杀特征（空 = 放行）。``text`` 可以是 shell 命令或 python 代码。"""
    s = str(text or "")
    low = s.lower()
    hits: list[str] = []
    for pat, label in _KILL_ALL_PATTERNS:
        if re.search(pat, low, re.I):
            hits.append(label)
    if not hits and _PY_LIST_FORM.search(s):
        # subprocess.run(["taskkill", "/F", "/IM", "python.exe"]) 这种参数分开写的
        hits.append("按镜像名杀 python（宿主服务同名，会一起死）")
    if not hits:
        protected = _protected_pids()
        for m in _KILL_BY_PID.finditer(s):
            try:
                pid = int(m.group(1))
            except Exception:  # noqa: BLE001
                continue
            if pid in protected:
                hits.append(f"按 PID 杀宿主进程（pid={pid} 是本服务自己）")
                break
    return hits


def deny_message(hits: list[str], tool: str) -> str:
    """拒绝文案：说清为什么 + 给出路（照凭证闸的写法）。"""
    why = "；".join(dict.fromkeys(hits))
    return (
        f"拒绝执行：{why}。\n"
        "宿主服务与测试进程**同名**（都是 python.exe），按名字杀会把服务一起杀掉——"
        "实测已发生：一条 `taskkill /F /IM python.exe` 把正在跑的服务直接干掉了，"
        "没有异常、没有日志。\n"
        "出路：先 `tasklist | findstr python`（或 `Get-Process python`）查出你要清理的"
        "那个进程的 **PID**，再用 `taskkill /PID <pid> /F` 精确杀。\n"
        f"（{tool} 工具已拦截；这是护栏，不是失败重试能绕过的。）"
    )


def log_deny(work_id: str, card_id: str, hits: list[str], payload: Any) -> None:
    """留痕（与凭证闸同通道：不抛、不阻断主链、不新增事件类型）。"""
    try:
        from . import prompt_log

        prompt_log.append_perm_log({
            "fn": "exec_process_guard",
            "action": "deny",
            "card_id": card_id,
            "hits": list(dict.fromkeys(hits)),
            "cmd_head": str(payload)[:200],
        })
    except Exception:  # noqa: BLE001 留痕失败不影响拒绝本身
        pass
