# -*- coding: utf-8 -*-
"""[强制停止 2026-10-24 · 用户令] 停止必须是强制性的;根卡/父卡有打断/停止权。

钉六件事:
T1 halt 标记:置位/查询/解除;halt 内含 request_stop 的轮杀
T2 入口闸:halted 卡拒绝内部唤醒(_run_message source_kind=internal ⇒ error:halted,
   不起回合);人类消息(source_kind=human)解除并继续
T3 stop_card 工具·halt 档:goal 级联暂停 + 标记置位 + 留痕
T4 stop_card 工具·interrupt 档:只杀轮,不动 goal、不置标记
T5 权限:非祖先拒;总裁(根卡)可停全树;根卡自己不可被停;coder 卡无此工具
T6 HTTP stop:置标记(强制),用户再发消息可解除
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import card_tools, control, routes as api_routes
from src.storage import card_store, function_store, work_store


@pytest.fixture(autouse=True)
def _clear_control_state():
    """control 的停止状态是模块级(sandbox 只搬数据根)——跨用例必须清,
    否则上一用例的 halt 标记/停止置位污染下一用例(实测批量挂、单跑过)。"""
    control._HALTED.clear()
    control._stop_requests.clear()
    control._stop_events.clear()
    yield
    control._HALTED.clear()
    control._stop_requests.clear()
    control._stop_events.clear()


async def _tree():
    w = await work_store.create_work("强制停止")
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    mid = (await card_store.create_card(wid, "父卡", "模型生成",
                                        parent=root))["meta"]["card_id"]
    leaf = (await card_store.create_card(wid, "子卡", "模型生成",
                                         parent=mid))["meta"]["card_id"]
    # 叶子=legacy 自动卡(有 goal、goal_state=active):stop_auto 的 goal 暂停
    # 对这类卡走「投影列回退」分支,对 goal 域卡走正规 pause——两态都覆盖。
    await card_store.update_card(wid, leaf,
                                 {"auto_mode": 1, "goal": "子目标"}, "t")
    await card_store.update_card(wid, leaf, {"goal_state": "active"}, "t")
    return wid, root, mid, leaf


def _cards(wid):
    return card_tools._collect_card_meta(wid)


# ---------------------------------------------------------------- T1

async def test_halt_marker_lifecycle(sandbox):
    wid, root, mid, leaf = await _tree()
    assert control.is_halted(wid, leaf) is None
    control.halt(wid, leaf, reason="测试停", by="card_x")
    h = control.is_halted(wid, leaf)
    assert h and h["reason"] == "测试停" and h["by"] == "card_x"
    assert control.unhalt(wid, leaf) is True
    assert control.is_halted(wid, leaf) is None
    assert control.unhalt(wid, leaf) is False       # 幂等


# ---------------------------------------------------------------- T2

async def test_gate_rejects_internal_and_human_clears(sandbox):
    wid, root, mid, leaf = await _tree()
    control.halt(wid, leaf, reason="跑偏", by=mid)
    # 内部唤醒(goal 续轮/收件箱)⇒ 拒,不起回合
    r = await api_routes._run_message(wid, leaf, "内部激励文", None,
                                      append_user=False)
    assert isinstance(r, dict) and r.get("error", {}).get("code") == "halted"
    assert control.is_halted(wid, leaf) is not None   # 标记未被内部路径清
    # 人类直接发话 ⇒ 解除并继续(内容是普通消息,MOCK_LLM 未设时可能真调模型——
    # 这里只验证「标记被清」这一步:进链路前先解,故用 monkeypatch 拦截下层)
    import asyncio

    async def _fake_inner(*a, **k):
        return {"ok": True}
    # _run_message 是一个薄壳:真正工作在壳内——直接验证 human 分支清标记:
    # 模拟:置 human 标志调用会先 unhalt 再继续;此处用直接调用来证明顺序:
    # (不真跑模型:monkeypatch session_facade 读历史会 IO,但 gate 在最前)
    control.halt(wid, leaf, reason="再停一次", by=mid)
    assert control.is_halted(wid, leaf) is not None


# ---------------------------------------------------------------- T2b

async def test_gate_human_clears_marker(sandbox, monkeypatch):
    """人类直接发话 ⇒ gate 先解除标记再继续(下层换桩,不真调模型)。"""
    wid, root, mid, leaf = await _tree()
    control.halt(wid, leaf, reason="用户说话前", by=mid)

    async def _fake_inner(*a, **k):
        return {"ok": True, "stub": True}

    monkeypatch.setattr(api_routes, "_run_message_inner", _fake_inner)
    r = await api_routes._run_message(wid, leaf, "用户直接说话", None,
                                      source_kind="human")
    assert isinstance(r, dict) and r.get("stub") is True   # 真的继续走了
    assert control.is_halted(wid, leaf) is None            # 标记已解除


# ---------------------------------------------------------------- T3/T5

async def test_stop_card_halt_by_parent(sandbox):
    wid, root, mid, leaf = await _tree()
    cards = _cards(wid)
    out = await card_tools._h_stop_card(
        wid, mid, {"card_id": leaf, "mode": "halt", "reason": "输出污染仓库"},
        cards)
    assert out.get("mode") == "halt"
    assert control.is_halted(wid, leaf) is not None
    m = card_store._meta_of(wid, leaf) or {}
    assert str(m.get("goal_state") or "") == "paused"   # goal 正规暂停
    # 权限:兄弟不可停兄弟
    other = (await card_store.create_card(wid, "兄弟", "模型生成",
                                          parent=root))["meta"]["card_id"]
    cards = _cards(wid)
    denied = await card_tools._h_stop_card(
        wid, other, {"card_id": leaf, "reason": "越权尝试"}, cards)
    assert denied.get("ok") is False
    # 总裁(根卡)可停全树任意卡
    ok_root = await card_tools._h_stop_card(
        wid, root, {"card_id": other, "reason": "总裁令"}, cards)
    assert ok_root.get("mode") == "halt"
    # 根卡自己不可被停
    denied_root = await card_tools._h_stop_card(
        wid, mid, {"card_id": root, "reason": "下克上"}, cards)
    assert denied_root.get("ok") is False


async def test_stop_card_interrupt_only_kills_round(sandbox):
    wid, root, mid, leaf = await _tree()
    cards = _cards(wid)
    out = await card_tools._h_stop_card(
        wid, mid, {"card_id": leaf, "mode": "interrupt", "reason": "这轮跑偏"},
        cards)
    assert out.get("mode") == "interrupt"
    assert control.is_halted(wid, leaf) is None         # 不置标记
    m = card_store._meta_of(wid, leaf) or {}
    assert str(m.get("goal_state") or "") == "active"   # goal 不动


async def test_stop_card_toolface_for_parents_not_coder(sandbox):
    wid, root, mid, leaf = await _tree()
    coder = (await card_store.create_card(wid, "编程卡", "模型生成",
                                          parent=mid,
                                          card_type="coder"))["meta"]["card_id"]
    cards = _cards(wid)
    for cid, expect in ((root, True), (mid, True), (leaf, True),
                        (coder, False)):
        tools = card_tools.build_tool_schemas(card_id=cid, work_id=wid,
                                              cards=cards)
        names = {str(t.get("name")) for t in tools}
        assert ("stop_card" in names) is expect, (cid, expect, names)


# ---------------------------------------------------------------- T6

async def test_http_stop_is_forced(sandbox):
    wid, root, mid, leaf = await _tree()
    app = FastAPI()
    app.include_router(api_routes.router)
    with TestClient(app) as client:
        resp = client.post(f"/api/works/{wid}/cards/{leaf}/stop")
        assert resp.status_code == 200
    assert control.is_halted(wid, leaf) is not None       # 强制标记
    m = card_store._meta_of(wid, leaf) or {}
    assert str(m.get("goal_state") or "") == "paused"
    control.unhalt(wid, leaf)                             # 清理,人类可再启
