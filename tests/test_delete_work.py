# -*- coding: utf-8 -*-
"""[删项目 2026-10-21 · 用户令] 项目必须能删。

原来 work 层写着「不可删除（不提供删除函数）」，根卡又拒删 ⇒ 用户建错的项目
永远删不掉（质问：「那不就是项目不能删吗？有病吗？」）。
删法是把整个目录搬进 works/_trash/（可恢复）；确认只在前端一个弹窗，后端不设门槛。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.storage import work_store


async def test_delete_work_moves_directory_to_trash(work):
    """两种模式都算成功：
      · moved —— 目录搬进 works/_trash/（理想路径）
      · marked —— 目录被占用（Windows 的 -wal/-shm）搬不动 ⇒ 标记删除，立刻从列表消失
    共同判据：**列表里不能再出现**。
    """
    wid = work["work_id"]
    wd = Path(work_store.WORKS_DIR) / wid
    assert wd.exists()

    r = work_store.delete_work(wid)
    assert r.get("ok") is True, r
    assert r.get("mode") in ("moved", "marked"), r
    assert not any(w["work_id"] == wid for w in work_store.list_works()), "列表里不该再有它"
    if r.get("mode") == "moved":
        moved = Path(r["moved_to"])
        assert moved.exists() and moved.parent.name == "_trash"
        assert wid in moved.name
        assert (moved / "work.json").exists(), "内容还在（不是抹掉）"


async def test_delete_work_missing_returns_error(work):
    r = work_store.delete_work("work_999")
    assert "error" in r


async def test_delete_work_api_deletes_without_extra_confirm(work):
    """[用户令 2026-10-21] 一个「是否删除」弹窗就够，后端不再要求输入项目名。"""
    import httpx

    from main import app

    wid = work["work_id"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        r = await client.delete(f"/api/works/{wid}")
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True
        assert not any(w["work_id"] == wid for w in work_store.list_works())
