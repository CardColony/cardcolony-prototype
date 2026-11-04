# -*- coding: utf-8 -*-
"""**Devin 执行体**的环境隔离（2026-11-04 新增）。

**为什么有这份文件**：生产服务是**从 Devin 桌面版（Windsurf IDE）的内置终端**起的
⇒ 那个环境里有 ``ACP_BACKEND=windsurf`` ⇒ 我们起的 ``devin.exe`` 子进程继承它
⇒ CLI 改走 IDE 凭据后端 ⇒ 每次 ``Not logged in`` ⇒ 每张卡被派给它就死一次。

⇒ 修法在**代码**里：起 Devin CLI 时把 ``ACP_BACKEND`` 剔掉
（不靠"人记得从干净 shell 启动服务"）。
"""
from __future__ import annotations

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
