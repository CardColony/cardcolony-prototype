# -*- coding: utf-8 -*-
"""[UI 卡 2026-11-02] create_ui_card + ui 卡型测试。

钉的事：
T1  注册：create_ui_card 在 TOOL_REGISTRY / TOOL_SWITCH(create_child) / ToolDoc
T2  建卡：card_type=ui、使命块落任务、goal=task、parent=当前卡、建卡即首跑
T3  关口：空 title/task 拒；父卡不存在拒；越权挂外树拒
T4  能力：ui 卡 abilities 有 browser/run_code/report_up_chain，无 report_done
T5  工具面：ui 卡面见 browser+report_up+写文件件；不见 report_done/direct/stop_card
T6  路由：ui 不在 wb_exec 委派白名单 ⇒ 走系统内模型（内循环）
"""
from __future__ import annotations

from src.api import card_tools, function_room, routes
from src.api.wb_exec import bridge as wb_bridge
from src.api.wb_exec import config as wb_config
from src.storage import card_store, work_store

REPO_CATALOG = function_room._DEFAULT_PATH


def _use_repo_catalog(monkeypatch) -> None:
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH, str(REPO_CATALOG))
    function_room.reset_cache()


def _stub_wake(monkeypatch) -> None:
    """建卡即首跑会真起模型回合——打桩只验证建卡本身。"""
    async def _fake(*a, **kw):
        return {"ok": True, "stub": True}
    monkeypatch.setattr(routes, "_run_message", _fake)


async def _mk_work_and_card(monkeypatch,
                            title: str = "UI 卡测试") -> tuple[str, dict, str]:
    _use_repo_catalog(monkeypatch)
    wdata = await work_store.create_work(title)
    wid = wdata["work_id"]
    await function_room.seed_work(wid)          # 职能目录启用 ⇒ report_up 才会下发
    r = await card_store.create_card(wid, "父卡", "模型生成",
                                     card_type="sub_project",
                                     skip_kick=True)
    cid = str(r["meta"]["card_id"])
    return wid, card_store.iter_card_metas(wid), cid


# ---------------------------------------------------------------- T1 注册面

def test_ui_card_registered():
    assert "create_ui_card" in card_tools.TOOL_REGISTRY
    assert card_tools.TOOL_SWITCH.get("create_ui_card") == "create_child"
    doc = getattr(card_tools.TOOL_REGISTRY["create_ui_card"], "doc", None)
    assert doc is not None and getattr(doc, "group", "")


# ---------------------------------------------------------------- T2 建卡

async def test_create_ui_card_happy(sandbox, monkeypatch):
    wid, cards, cid = await _mk_work_and_card(monkeypatch)
    _stub_wake(monkeypatch)

    out = await card_tools._h_create_ui_card(
        wid, cid, {"title": "卡面详情页原型",
                   "task": "写一个卡片详情页 HTML 并在浏览器里验证渲染"}, cards)
    assert out.get("ok"), out
    assert out["card_type"] == "ui"
    assert out["parent"] == cid and out["started"] is True

    metas = card_store.iter_card_metas(wid)
    nid = out["card_id"]
    assert nid in metas
    assert str(metas[nid].get("card_type") or "") == "ui"
    assert str(metas[nid].get("parent") or "") == cid

    card = await card_store.read_card(wid, nid)
    mission = str((card.get("body") or {}).get("mission") or "")
    assert "卡片详情页" in mission
    assert str((card.get("meta") or {}).get("goal") or "") == \
        "写一个卡片详情页 HTML 并在浏览器里验证渲染"


# ---------------------------------------------------------------- T3 关口

async def test_create_ui_card_gates(sandbox, monkeypatch):
    wid, cards, cid = await _mk_work_and_card(monkeypatch)
    _stub_wake(monkeypatch)

    out = await card_tools._h_create_ui_card(
        wid, cid, {"title": "", "task": "干活"}, cards)
    assert out["error"]["code"] == "validation"
    out = await card_tools._h_create_ui_card(
        wid, cid, {"title": "x", "task": ""}, cards)
    assert out["error"]["code"] == "validation"
    out = await card_tools._h_create_ui_card(
        wid, cid, {"title": "x", "task": "干活", "parent": "card_zzz"}, cards)
    assert out["error"]["code"] == "not_found"


# ---------------------------------------------------------------- T4 能力挂载

async def test_ui_card_abilities(sandbox, monkeypatch):
    wid, cards, cid = await _mk_work_and_card(monkeypatch)
    _stub_wake(monkeypatch)
    out = await card_tools._h_create_ui_card(
        wid, cid, {"title": "t", "task": "做界面"}, cards)
    assert out.get("ok"), out
    meta = (await card_store.read_card(wid, out["card_id"])).get("meta") or {}
    abilities = set(meta.get("abilities") or [])
    # 浏览器＋读写文件＋跑命令＋汇报——内循环执行卡的配置
    assert {"browser", "read_files", "write_files", "run_code",
            "report_up_chain"} <= abilities, abilities
    # 不给 report_done（那是 coder 的收口件）
    assert "report_done" not in abilities


# ---------------------------------------------------------------- T5 工具面

async def test_ui_card_toolface(sandbox, monkeypatch):
    wid, cards, cid = await _mk_work_and_card(monkeypatch)
    _stub_wake(monkeypatch)
    out = await card_tools._h_create_ui_card(
        wid, cid, {"title": "t", "task": "做界面"}, cards)
    assert out.get("ok"), out
    nid = out["card_id"]
    cards = card_store.iter_card_metas(wid)

    names = {t.get("name") for t in card_tools.build_tool_schemas(
        card_id=nid, work_id=wid, cards=cards)}
    assert "browser" in names, names          # 眼睛
    assert "report_up" in names, names        # 收口通道（作业卡口径）
    assert "write_file" in names, names       # 写界面代码
    assert "bash" in names, names             # 起服务/跑构建
    assert "sync" in names, names
    # 不给的东西
    assert "report_done" not in names         # coder 专属
    assert "direct" not in names              # 无子卡，没有指令对象
    assert "stop_card" not in names           # 父权治理件


# ---------------------------------------------------------------- T6 路由：不委派

async def test_ui_card_not_delegable(sandbox, monkeypatch):
    """ui 不在 wb_exec 白名单 ⇒ is_delegable_card 拒 ⇒ 内循环跑系统内模型。"""
    cfg = wb_bridge.load_config()
    assert "ui" not in (cfg.delegable_card_types or ())
    wid, cards, cid = await _mk_work_and_card(monkeypatch)
    _stub_wake(monkeypatch)
    out = await card_tools._h_create_ui_card(
        wid, cid, {"title": "t", "task": "做界面"}, cards)
    assert out.get("ok"), out
    assert await wb_bridge.is_delegable_card(wid, out["card_id"]) is False
