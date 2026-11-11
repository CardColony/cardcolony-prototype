# -*- coding: utf-8 -*-
"""**Devin 执行体**的环境隔离（2026-11-04 新增）。

**为什么有这份文件**：生产服务是**从 Devin 桌面版（Windsurf IDE）的内置终端**起的
⇒ 那个环境里有 ``ACP_BACKEND=windsurf`` ⇒ 我们起的 ``devin.exe`` 子进程继承它
⇒ CLI 改走 IDE 凭据后端 ⇒ 每次 ``Not logged in`` ⇒ 每张卡被派给它就死一次。

⇒ 修法在**代码**里：起 Devin CLI 时把 ``ACP_BACKEND`` 剔掉
（不靠"人记得从干净 shell 启动服务"）。
"""
from __future__ import annotations

import asyncio
import os

import pytest

from src.api.wb_exec import devin_client


def test_child_env_drops_acp_backend(monkeypatch):
    """★ 起 Devin CLI 的环境里**不许有** ``ACP_BACKEND``（它会切凭据后端）。"""
    monkeypatch.setenv("ACP_BACKEND", "windsurf")
    monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")
    env = devin_client._child_env()
    assert "ACP_BACKEND" not in env, "ACP_BACKEND 会让 CLI 改走 IDE 凭据后端"
    assert env.get("SOME_OTHER_VAR") == "keep-me", "别的变量要原样继承"


def test_child_env_keeps_everything_else(monkeypatch):
    """其它环境变量**照常继承**（PATH/凭据等一个都不能少）。"""
    monkeypatch.setenv("KEEP_A", "1")
    monkeypatch.setenv("KEEP_B", "2")
    env = devin_client._child_env()
    assert env.get("KEEP_A") == "1"
    assert env.get("KEEP_B") == "2"
    # 真实环境变量也在（PATH 之类）
    assert env.get("PATH") == os.environ.get("PATH")


def test_devin_env_drop_list_contains_only_the_proven_offender():
    """**只剔实测证明有害的那一个**（其它 IDE 变量留着无害：实测去掉 ACP_BACKEND
    之后带全套 IDE 变量也能 rc=0 出活）。"""
    assert "ACP_BACKEND" in devin_client._DEVIN_ENV_DROP
    assert len(devin_client._DEVIN_ENV_DROP) == 1


@pytest.mark.asyncio
async def test_devin_runner_spawns_with_cleaned_env(monkeypatch, tmp_path):
    """★ 真正起进程时用的就是**剔过 ACP_BACKEND 的环境**（不是只在工具函数里）。"""
    seen: dict = {}

    class _Proc:
        pid = 4242
        returncode = 0

        async def communicate(self):
            return ("", "")

        async def wait(self):
            return self.returncode

        def kill(self):
            return None

    async def fake_exec(*argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setenv("ACP_BACKEND", "windsurf")
    monkeypatch.setattr(devin_client.asyncio, "create_subprocess_exec", fake_exec)

    r = devin_client.DevinRunner(
        devin_client.__file__ and r"C:\devin\devin.exe", model="swe-2-max",
        cloud=False, timeout=30.0)
    await r.run("干点活", str(tmp_path), model="swe-2-max")

    assert seen.get("env") is not None, "起进程必须显式给 env"
    assert "ACP_BACKEND" not in seen["env"]


# ---------- 收尾挂住兜底（2026-11-10 · work_027 card_637 实证） ----------
# 现场：执行体 22:41 出最终正文，之后 27 分钟零事件、进程不退——
# communicate() 等「进程退＋管道EOF」两件，子孙占住管道就永远吊住，
# 卡面一直"运行中"直到用户手停。run() 改成四路竞速后这些形态都得收口。


class _HungProc:
    """communicate/wait 都永不返回——模拟「正文出了但进程僵死」。"""

    def __init__(self):
        self.pid = 9999
        self.returncode = None
        self.killed = False
        self._dead = None  # asyncio.Event 在事件循环里建

    async def communicate(self):
        await asyncio.Event().wait()  # 永不返回：管道被子孙占住
        return (b"", b"")

    async def wait(self):
        if self._dead is None:
            self._dead = asyncio.Event()
        await self._dead.wait()
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        if self._dead is not None:
            self._dead.set()


def _fake_feed(events):
    """造一个受控实时事件源：喂完给定事件就睡死（等取消）。"""
    async def _feed(_marker, on_event):
        for kind, payload in events:
            on_event(kind, payload)
        await asyncio.Event().wait()
    return _feed


async def _noop_tailer(_pid, _on_event):
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_post_text_idle_closes_hung_run(monkeypatch, tmp_path):
    """正文已出＋全静默 ≥阈值 ⇒ 杀进程、按已捕正文成功收口（不重做）。"""
    monkeypatch.setattr(devin_client, "_POST_TEXT_IDLE_S", 0.2)
    monkeypatch.setattr(devin_client, "_PIPE_DRAIN_GRACE_S", 0.05)
    proc = _HungProc()

    async def fake_exec(*argv, **kwargs):
        return proc

    monkeypatch.setattr(devin_client.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(devin_client, "_poll_live_events",
                        _fake_feed([("text", {"content": "干完了，报告在 x.md"})]))
    monkeypatch.setattr(devin_client, "_tail_devin_log", _noop_tailer)

    r = devin_client.DevinRunner(r"C:\devin\devin.exe", model="swe-2-max",
                                 cloud=False, timeout=60.0)
    res = await r.run("干活", str(tmp_path), model="swe-2-max",
                      marker="M", on_event=lambda k, p: None)

    assert proc.killed, "收尾挂住必须杀进程"
    assert res["ok"] is True, "有已捕正文＝活干完了，判失败会让上层重做"
    assert res["text"] == "干完了，报告在 x.md"


@pytest.mark.asyncio
async def test_pending_tool_blocks_idle_close(monkeypatch, tmp_path):
    """工具在途（pending>0）⇒ 静默看门不早杀，落总超时闸。"""
    monkeypatch.setattr(devin_client, "_POST_TEXT_IDLE_S", 0.1)
    monkeypatch.setattr(devin_client, "_PIPE_DRAIN_GRACE_S", 0.05)
    proc = _HungProc()

    async def fake_exec(*argv, **kwargs):
        return proc

    monkeypatch.setattr(devin_client.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        devin_client, "_poll_live_events",
        _fake_feed([("tool_call", {"tool": "bash"}),
                    ("text", {"content": "中间话"})]))  # tool_result 永远不来
    monkeypatch.setattr(devin_client, "_tail_devin_log", _noop_tailer)

    r = devin_client.DevinRunner(r"C:\devin\devin.exe", model="swe-2-max",
                                 cloud=False, timeout=0.8)
    res = await r.run("干活", str(tmp_path), model="swe-2-max",
                      marker="M", on_event=lambda k, p: None)

    assert res["rc"] == -1 and res["ok"] is False, "在途工具期静默该走总超时"


@pytest.mark.asyncio
async def test_pipe_held_after_exit_recovers(monkeypatch, tmp_path):
    """进程退了但管道被占 ⇒ 排空宽限后照样收口，不无限吊。"""
    monkeypatch.setattr(devin_client, "_PIPE_DRAIN_GRACE_S", 0.1)

    class _ExitedProc:
        pid = 8888
        returncode = 0

        async def communicate(self):
            await asyncio.Event().wait()  # 管道被子孙占住

        async def wait(self):
            return 0  # 进程本体已退

        def kill(self):
            pass

    async def fake_exec(*argv, **kwargs):
        return _ExitedProc()

    monkeypatch.setattr(devin_client.asyncio, "create_subprocess_exec", fake_exec)

    r = devin_client.DevinRunner(r"C:\devin\devin.exe", model="swe-2-max",
                                 cloud=False, timeout=60.0)
    res = await r.run("干活", str(tmp_path), model="swe-2-max")
    assert res["rc"] == 0, "进程真退出码要保留"
    assert res["ok"] is False  # 0 正文 ⇒ 上层判失败（空结果守卫）
