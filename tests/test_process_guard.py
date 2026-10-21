# -*- coding: utf-8 -*-
"""进程护栏：不许 agent 按镜像名通杀解释器进程。

[2026-10-21 实测事故 · 用户令] `work_018 / card_031` 在 5713 worktree 里清理测试
残留进程，执行了：

    taskkill /F /IM python.exe

`/IM` 按**镜像名**杀 ⇒ 5700 生产服务同名 ⇒ 23:41:26 当场消失，无异常、无 traceback、
无事件日志。用户原话：「服务是被 agent 通杀 python 杀掉的」。

护栏只拦「通杀」，不拦「精确杀」：先查 PID 再 `taskkill /PID` 仍然允许。
"""
from __future__ import annotations

import os

import pytest

from src.api import process_guard


# ---------------------------------------------------------------- 必须拒


@pytest.mark.parametrize("cmd", [
    'taskkill /F /IM python.exe',
    'taskkill /F /IM python.exe 2>&1 | findstr /C:"SUCCESS"',
    'taskkill /f /im pythonw.exe',
    'taskkill /IM python3',
    'taskkill /F /IM *',
    'Stop-Process -Name python -Force',
    'Get-Process python | Stop-Process',
    'pkill -9 python',
    'killall python3',
    'wmic process where "name=\'python.exe\'" delete',
])
def test_kill_by_image_name_is_denied(cmd):
    hits = process_guard.find_process_kill(cmd)
    assert hits, f"必须拦住：{cmd}"


def test_python_subprocess_list_form_is_denied():
    """python 工具里的写法：参数分开放在列表里，也要拦住。"""
    code = (
        'import subprocess\n'
        'subprocess.run(["taskkill", "/F", "/IM", "python.exe"])\n'
    )
    assert process_guard.find_process_kill(code)


def test_killing_host_pid_is_denied():
    """按 PID 杀也拦——只要那个 PID 是本服务自己。"""
    cmd = f"taskkill /PID {os.getpid()} /F"
    hits = process_guard.find_process_kill(cmd)
    assert hits and "宿主" in hits[0]


# ---------------------------------------------------------------- 必须放行


@pytest.mark.parametrize("cmd", [
    'tasklist /FO CSV | findstr python',
    'Get-Process python',
    'taskkill /PID 987654 /F',            # 精确 PID，不是宿主
    'Stop-Process -Id 987654 -Force',
    'python -m pytest tests -q',
    'git status --short',
])
def test_precise_and_readonly_commands_pass(cmd):
    assert process_guard.find_process_kill(cmd) == [], f"不该拦：{cmd}"


# ---------------------------------------------------------------- 拒绝文案


def test_deny_message_gives_a_way_out():
    msg = process_guard.deny_message(["按镜像名杀 python"], "bash")
    assert "PID" in msg and "taskkill /PID" in msg
    assert "同名" in msg, "要说清为什么：宿主服务与测试进程同名"


# ---------------------------------------------------------------- 接线


async def test_bash_tool_denies_kill_all(work, monkeypatch):
    """走真实工具面：这条命令必须被拒，且**没有真的执行**。"""
    from src.api import card_tools
    from src.storage import card_store

    wid, root = work["work_id"], work["work_card_id"]
    r = await card_store.create_card(wid, "卡", "模型生成", parent=root)
    cid = r["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)

    called = {"n": 0}

    async def _fake_run(**kw):
        called["n"] += 1
        return {"ok": True, "data": {"exit_code": 0, "stdout": "", "stderr": ""}}

    monkeypatch.setattr(card_tools, "_run_subprocess", _fake_run)

    out = await card_tools.execute_tool(
        wid, cid, "bash", {"command": "taskkill /F /IM python.exe"}, cards)
    assert out["ok"] is False
    assert out["error"]["code"] == "denied"
    assert "PID" in out["error"]["message"]
    assert called["n"] == 0, "被拒的命令绝不许真的执行"
