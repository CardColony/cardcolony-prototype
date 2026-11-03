# -*- coding: utf-8 -*-
"""★ **会话清理**的安全护栏单测（2026-11-04）。

用户红线：「**千万别删除对话**」「**千万别删错对话**」
⇒ 这些用例把"**只删我们自己那一条**"钉死：

- 白名单之外的工作目录 ⇒ **不算我们的**（用户的 Devin Desktop / opencode
  会话目录是仓库根 ⇒ 落选 ⇒ 不在删除范围）；
- id 对不上 / 工作目录对不上 ⇒ **跳过**（宁可留垃圾，也不删错）；
- 桌面端执行体（WorkBuddy）⇒ **一律不删**；
- opencode 侧**只删给定的那个 id**。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.api.wb_exec import cleanup as cl


# ===================================================================== 白名单
def test_is_ours_only_under_whitelist_roots():
    roots = [r"D:\AI_station\推演agent\works"]
    # ★ 我们卡片的工作目录 ⇒ 是我们的
    assert cl._is_ours(r"D:\AI_station\推演agent\works\work_027", roots) is True
    # ★ 用户自己在 Devin Desktop 里的会话（目录是仓库根）⇒ **不是我们的**
    assert cl._is_ours(r"D:\AI_station\推演agent", roots) is False
    assert cl._is_ours("C:\\", roots) is False
    assert cl._is_ours(r"D:\Temp\wb_oc_sandbox\works\work_005", roots) is False
    # 空目录 ⇒ 落选
    assert cl._is_ours("", roots) is False


def test_whitelist_default_root_is_repo_works():
    """>> 白名单默认根 = 仓库的 works/（每张 coder 卡的工作目录都在这儿）。"""
    roots = [str(p) for p in cl.DEFAULT_WORK_ROOTS]
    assert roots and roots[0].lower().endswith("works"), roots


def test_session_id_must_look_like_an_id():
    assert cl._looks_like_session_id("admitted-hare") is True
    assert cl._looks_like_session_id("ses_abc123") is True
    # ★ 乱七八糟的东西 ⇒ 不许当 id 传进去
    assert cl._looks_like_session_id("") is False
    assert cl._looks_like_session_id("rm -rf /") is False
    assert cl._looks_like_session_id("a" * 200) is False


# ===================================================================== Devin
@pytest.mark.asyncio
async def test_devin_cleanup_skips_when_id_not_in_that_dir(tmp_path, monkeypatch):
    """★ **id 不在这个目录的会话列表里 ⇒ 跳过**（绝不按 id 盲删）。"""
    work = tmp_path / "work_001"
    work.mkdir()
    cli = tmp_path / "devin.exe"
    cli.write_bytes(b"stub")
    calls: list[list[str]] = []

    async def fake_run(argv, cwd, timeout):  # noqa: ANN001
        calls.append(list(argv))
        if argv[1] == "list":
            return {"ok": True,
                    "stdout": json.dumps([{"id": "someone-else",
                                           "working_directory": str(work),
                                           "title": "用户的"}]),
                    "stderr": "", "rc": 0}
        return {"ok": True, "stdout": "", "stderr": "", "rc": 0}

    monkeypatch.setattr(cl, "_run_cli", fake_run)
    res = await cl.cleanup_devin(str(cli), "my-session", str(work))
    assert res["deleted"] is False
    assert "跳过" in str(res.get("skipped"))
    # ★ **绝不能发出 rm**（只发过 list）
    assert all("rm" not in c for c in calls), calls


@pytest.mark.asyncio
async def test_devin_cleanup_skips_when_workdir_mismatch(tmp_path, monkeypatch):
    """★ **工作目录对不上 ⇒ 跳过**（哪怕 id 出现在列表里）。"""
    work = tmp_path / "work_001"
    work.mkdir()
    cli = tmp_path / "devin.exe"
    cli.write_bytes(b"stub")
    calls: list[list[str]] = []

    async def fake_run(argv, cwd, timeout):  # noqa: ANN001
        calls.append(list(argv))
        if argv[1] == "list":
            return {"ok": True,
                    "stdout": json.dumps([{"id": "my-session",
                                           "working_directory": r"D:\别的\目录",
                                           "title": "别的目录的会话"}]),
                    "stderr": "", "rc": 0}
        return {"ok": True, "stdout": "", "stderr": "", "rc": 0}

    monkeypatch.setattr(cl, "_run_cli", fake_run)
    res = await cl.cleanup_devin(str(cli), "my-session", str(work))
    assert res["deleted"] is False
    assert all("rm" not in c for c in calls), calls


@pytest.mark.asyncio
async def test_devin_cleanup_deletes_only_verified_session(tmp_path, monkeypatch):
    """★ 身份对得上 ⇒ 才发 ``rm <完整 id> --force``。"""
    work = tmp_path / "work_001"
    work.mkdir()
    cli = tmp_path / "devin.exe"
    cli.write_bytes(b"stub")
    calls: list[list[str]] = []

    async def fake_run(argv, cwd, timeout):  # noqa: ANN001
        calls.append(list(argv))
        if argv[1] == "list":
            return {"ok": True,
                    "stdout": json.dumps([{"id": "my-session",
                                           "working_directory": str(work),
                                           "title": "我们自己的"}]),
                    "stderr": "", "rc": 0}
        return {"ok": True, "stdout": "deleted", "stderr": "", "rc": 0}

    monkeypatch.setattr(cl, "_run_cli", fake_run)
    res = await cl.cleanup_devin(str(cli), "my-session", str(work))
    assert res["deleted"] is True
    rm_calls = [c for c in calls if "rm" in c]
    assert len(rm_calls) == 1
    assert rm_calls[0][2] == "my-session"       # ★ 完整 id
    assert "--force" in rm_calls[0]            # ★ 非交互


# ===================================================================== opencode
@pytest.mark.asyncio
async def test_opencode_cleanup_deletes_only_the_given_id():
    """★ **只删给定的那个 id**（用户的会话 id 根本不会传进来）。"""
    seen: list[str] = []

    class _Client:
        async def delete_session(self, sid):  # noqa: ANN001
            seen.append(sid)
            return 200, True

    res = await cl.cleanup_opencode(_Client(), "ses_ours")
    assert res["deleted"] is True
    assert seen == ["ses_ours"]


@pytest.mark.asyncio
async def test_opencode_cleanup_skips_without_client():
    res = await cl.cleanup_opencode(None, "ses_ours")
    assert res["deleted"] is False
    assert "跳过" in str(res.get("skipped"))


# ===================================================================== 入口
@pytest.mark.asyncio
async def test_cleanup_never_touches_desktop_executors():
    """★★ **桌面端执行体一律不删**（用户的对话，一个都不能碰）。"""
    called: list[str] = []

    async def fake_devin(*a, **k):  # noqa: ANN001
        called.append("devin")
        return {"deleted": True}

    async def fake_oc(*a, **k):  # noqa: ANN001
        called.append("oc")
        return {"deleted": True}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(cl, "cleanup_devin", fake_devin)
    monkey.setattr(cl, "cleanup_opencode", fake_oc)
    try:
        for ex in ("wb-cn", "wb-intl", "wb-desktop"):
            res = await cl.cleanup_after_run(ex, session_id="x",
                                             work_dir=r"D:\x", client=object())
            assert res["deleted"] is False
            assert "不删" in str(res.get("skipped"))
        assert called == [], called
    finally:
        monkey.undo()


@pytest.mark.asyncio
async def test_cleanup_after_run_survives_exceptions(monkeypatch):
    """★ 清理出问题**绝不能影响卡片结果**（返回报告，不抛）。"""

    async def boom(*a, **k):  # noqa: ANN001
        raise RuntimeError("炸了")

    monkeypatch.setattr(cl, "cleanup_devin", boom)
    res = await cl.cleanup_after_run("wb-devin", session_id="x",
                                     work_dir=r"D:\x")
    assert res["deleted"] is False
    assert "异常" in str(res.get("skipped"))


# ===================================================================== 轨迹留存
def test_save_atif_keeps_trajectory(tmp_path, monkeypatch):
    """★ **删客户端会话前先把 ATIF 存进我们自己的日志**（可查性不丢）。"""
    monkeypatch.setattr(cl, "ATIF_DIR", tmp_path / "atif")
    path = cl.save_atif("card_006", "wb-devin",
                        {"raw": {"schema_version": "ATIF-v1.7", "steps": []}})
    assert path and Path(path).exists()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["schema_version"] == "ATIF-v1.7"
