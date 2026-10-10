# -*- coding: utf-8 -*-
"""[主界面优化 E2/V6] 沙箱端到端：/api/nav 增列 branch_active_at ＋ 导航零写 ＋ 会话计数。

经 httpx.ASGITransport 走真实 HTTP 层（conftest 沙箱，不碰真实 works）。
"""
from __future__ import annotations

import hashlib

import httpx


def _works_hash(works_dir) -> str:
    h = hashlib.sha256()
    for p in sorted(works_dir.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(works_dir)).encode("utf-8"))
            h.update(p.read_bytes())
    return h.hexdigest()


async def test_nav_has_branch_active_and_zero_write(sandbox, work, monkeypatch):
    from src.api.routes import router
    from src.storage import activity
    from fastapi import FastAPI

    # ★ activity.py 自带 WORKS_DIR 常量（T17 沙箱盲区）：不补丁则 nav 现算读真实 works，
    # 零写哈希证明与断言口径全部失真。conftest 一字不动，本测试内补齐铁律一。
    monkeypatch.setattr(activity, "WORKS_DIR", sandbox.works)
    app = FastAPI()
    app.include_router(router)
    # nav 不列根卡：先建一张子卡再验（own/branch 键均在场）
    from src.storage import card_store
    cr = await card_store.create_card(work["work_id"], "导航验收子卡", "用户直接创建",
                                      parent=work["work_card_id"])
    assert "error" not in cr, cr
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        before = _works_hash(sandbox.works)
        r1 = await client.get("/api/nav")
        assert r1.status_code == 200
        data = r1.json()
        r2 = await client.get("/api/nav")
        after = _works_hash(sandbox.works)
        assert before == after          # V6：导航请求 works 全量哈希前后一致（零写）

        proj = next(p for p in data["projects"] if p["work_id"] == work["work_id"])
        assert proj["cards"], "项目应有非根卡"
        for c in proj["cards"]:
            assert "branch_active_at" in c          # E2 增列在场
            assert "last_active_at" in c            # 卡级活跃照发
        # 分支语义：深层卡活跃会顶起祖先链（沙箱内所有卡都无会话 → 全空串仍须键在）


async def test_message_roundtrip_session_count(sandbox, work, monkeypatch):
    """真实调用链（mock 模型）：发一条消息 → 会话恰好 +2（用户＋助手），
    与 send() 增量上屏口径一致（V3 的沙箱实证腿）。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    from src.api import model_service
    from src.api.routes import router
    from src.storage import activity
    from fastapi import FastAPI

    # ★ activity.WORKS_DIR 沙箱补丁（同上）＋ mock 网关（离线真链路）
    monkeypatch.setattr(activity, "WORKS_DIR", sandbox.works)
    model_service._gateway = None
    app = FastAPI()
    app.include_router(router)
    wid, cid = work["work_id"], work["work_card_id"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        r0 = await client.get(f"/api/works/{wid}/cards/{cid}/session")
        # [迭代二 S4 配套] 对话块口径（环节留痕块 behavior 非空不计）
        def _dialog(blks):
            return [m for m in blks if not m.get("behavior")]
        base = len(_dialog(r0.json())) if isinstance(r0.json(), list) else 0
        r = await client.post(f"/api/works/{wid}/cards/{cid}/messages",
                              json={"content": "验收消息"})
        assert r.status_code == 200, r.text
        r1 = await client.get(f"/api/works/{wid}/cards/{cid}/session")
        msgs = _dialog(r1.json())
        assert isinstance(msgs, list)
        assert len(msgs) == base + 2                       # 恰好多一条用户＋一条助手
        assert msgs[-2]["role"] == "user" and msgs[-1]["role"] == "assistant"
