# -*- coding: utf-8 -*-
"""换父（reparent）· 回归网

[换父 2026-10-20 · 5718] 钉住四件事：

1. **存储层是唯一裁决点**：人（PATCH）与模型（reparent_card）走同一道校验——
   判据只有一份，不会出现「人拖得动、模型拖不动」或反过来。
2. **结构判据逐条拒**：成环、父卡不存在、父卡类型不能接子卡、根卡不可改。
   ★ 其中「父卡不存在」原实现是**静默回退根卡**（写错 id 也返回成功，卡被悄悄
   挂到根卡下）——本文件把它钉成报错。
3. **子树整体迁移**：改的是边，不是节点。被移动卡的孙子仍是它的孙子。
4. **模型工具的作用域＝只在自己子树内**（用户 2026-10-20 裁定）：不改自己的
   归属、不把卡挪出子树。

机制层铁律同 conftest：不 import main、不触真实数据（沙箱由 autouse fixture 提供）。
"""
from __future__ import annotations

import pytest

from src.api import card_tools
from src.permissions import policy
from src.storage import card_store, work_store

CANVAS_JS = "static/canvas.js"


# ---------------------------------------------------------------- 夹具

@pytest.fixture
async def typed_work(typed_registry):
    """带真实分类词表的空剧本（根卡＝project/rd_project，子卡 sub_project）。

    ``typed_registry`` 必须**先于**建 work 生效：根卡的 card_type 与 perms 都是
    建卡那一刻按注册表算死的，注册表后挂就没意义。
    """
    wdata = await work_store.create_work("换父回归剧本")
    assert "error" not in wdata, wdata
    return wdata


async def _mk(work_id: str, title: str, parent: str,
              card_type: str = "sub_project", **kw) -> str:
    r = await card_store.create_card(
        work_id, title, "模型生成", card_type=card_type,
        parent=parent, skip_kick=True, **kw)
    assert "error" not in r, r
    return r["meta"]["card_id"]


# ---------------------------------------------------------------- 存储层判据

async def test_reparent_moves_whole_subtree(typed_work):
    """改的是边：被移动卡的整棵子树跟着走，孙子还是孙子。"""
    wid = typed_work["work_id"]
    root = typed_work["work_card_id"]
    a = await _mk(wid, "A", root)
    b = await _mk(wid, "B", a)
    c = await _mk(wid, "C", root)

    r = await card_store.update_card(wid, a, {"parent": c}, "测试：A 挂到 C 下")
    assert "error" not in r, r

    metas = card_store.iter_card_metas(wid)
    assert metas[a]["parent"] == c, "被移动卡的父没变"
    assert metas[b]["parent"] == a, "孙子被改掉了（应该原地不动）"
    assert policy._is_in_subtree(c, b, metas), "孙子的祖先链没跟着走"


async def test_reparent_rejects_cycle(typed_work):
    """不能挂到自己的后代下（成环）。"""
    wid = typed_work["work_id"]
    root = typed_work["work_card_id"]
    a = await _mk(wid, "A", root)
    b = await _mk(wid, "B", a)

    r = await card_store.update_card(wid, a, {"parent": b}, "测试：成环")
    assert "error" in r
    assert "自身或后代" in r["error"]
    assert card_store.iter_card_metas(wid)[a]["parent"] == root, "被拒的写入不许改卡"


async def test_reparent_rejects_missing_parent(typed_work):
    """★ 父卡不存在 ⇒ 报错，**不再静默回退根卡**（原实现会悄悄改结构）。"""
    wid = typed_work["work_id"]
    root = typed_work["work_card_id"]
    a = await _mk(wid, "A", root)

    r = await card_store.update_card(wid, a, {"parent": "card_999"}, "测试：父卡不存在")
    assert "error" in r
    assert "不存在" in r["error"]
    assert card_store.iter_card_metas(wid)[a]["parent"] == root


async def test_reparent_rejects_tool_as_parent(typed_work):
    """工具卡 can_have_children=false ⇒ 不能当父卡（读注册表，不硬编码类型名）。"""
    wid = typed_work["work_id"]
    root = typed_work["work_card_id"]
    a = await _mk(wid, "A", root)
    tool = await _mk(wid, "工具卡", root, card_type="tool", owner_card_id=a)

    r = await card_store.update_card(wid, a, {"parent": tool}, "测试：挂到工具卡下")
    assert "error" in r
    assert "不可有子卡" in r["error"]


async def test_reparent_rejects_root(typed_work):
    """根卡无父：改它一律拒（结构性防护）。"""
    wid = typed_work["work_id"]
    root = typed_work["work_card_id"]
    a = await _mk(wid, "A", root)

    r = await card_store.update_card(wid, root, {"parent": a}, "测试：改根卡父")
    assert "error" in r
    assert "根卡" in r["error"]


async def test_reparent_empty_parent_normalizes_to_root(typed_work):
    """空串＝挂回顶层（归一成项目根卡），不落成「parent 键存在但为空」的悬空节点。"""
    wid = typed_work["work_id"]
    root = typed_work["work_card_id"]
    a = await _mk(wid, "A", root)
    b = await _mk(wid, "B", a)

    r = await card_store.update_card(wid, b, {"parent": ""}, "测试：挂回顶层")
    assert "error" not in r, r
    assert card_store.iter_card_metas(wid)[b]["parent"] == root


def test_can_have_children_reads_registry():
    """can_have_children 是**注册表字段**，不是第二份硬编码类型表。

    [换父 2026-10-20] 这个字段此前代码零消费（工具卡/角色卡各自硬编码）——
    本用例钉住「读注册表」这条口径：真注册表里 tool/role/coder/rule 为 false，
    sub_project/topic/project 为 true，查不到的类型放行（降级不阻断写路径）。
    """
    assert card_store._type_can_have_children("tool") is False
    assert card_store._type_can_have_children("role") is False
    assert card_store._type_can_have_children("coder") is False
    assert card_store._type_can_have_children("rule") is False
    assert card_store._type_can_have_children("sub_project") is True
    assert card_store._type_can_have_children("topic") is True
    assert card_store._type_can_have_children("project") is True
    assert card_store._type_can_have_children("查不到的类型") is True


# ---------------------------------------------------------------- 模型工具

async def _call(work_id: str, actor: str, card_id: str, new_parent: str) -> dict:
    cards = card_store.iter_card_metas(work_id)
    return await card_tools.execute_tool(
        work_id, actor, "reparent_card",
        {"card_id": card_id, "new_parent_id": new_parent}, cards)


async def test_tool_moves_card_within_subtree(typed_work):
    """子树内调整：根卡把 B 从 A 挪到 C。"""
    wid = typed_work["work_id"]
    root = typed_work["work_card_id"]
    a = await _mk(wid, "A", root)
    b = await _mk(wid, "B", a)
    c = await _mk(wid, "C", root)

    env = await _call(wid, root, b, c)
    assert env["ok"] is True, env
    assert card_store.iter_card_metas(wid)[b]["parent"] == c


async def test_tool_denies_move_out_of_subtree(typed_work):
    """★ 用户裁定：只能在自己子树内调整——不能把卡挪出子树。"""
    wid = typed_work["work_id"]
    root = typed_work["work_card_id"]
    a = await _mk(wid, "A", root)
    b = await _mk(wid, "B", a)

    env = await _call(wid, a, b, root)     # A 想把 B 交给根卡（挪出自己子树）
    assert env["ok"] is False, env
    assert env["error"]["code"] == "denied"
    assert card_store.iter_card_metas(wid)[b]["parent"] == a


async def test_tool_denies_own_reparent(typed_work):
    """不能改自己的归属（自己的归属归上级管）。"""
    wid = typed_work["work_id"]
    root = typed_work["work_card_id"]
    a = await _mk(wid, "A", root)

    env = await _call(wid, a, a, root)
    assert env["ok"] is False, env
    assert env["error"]["code"] == "denied"


async def test_tool_denies_card_outside_subtree(typed_work):
    """动不了别人的卡：C 与 A 是兄弟，A 不能动 C。"""
    wid = typed_work["work_id"]
    root = typed_work["work_card_id"]
    a = await _mk(wid, "A", root)
    c = await _mk(wid, "C", root)

    env = await _call(wid, a, c, a)
    assert env["ok"] is False, env
    assert env["error"]["code"] == "denied"


async def test_tool_denies_cycle(typed_work):
    """把 A 挂到 A 的子卡下 ⇒ 成环，拒。"""
    wid = typed_work["work_id"]
    root = typed_work["work_card_id"]
    a = await _mk(wid, "A", root)
    b = await _mk(wid, "B", a)

    env = await _call(wid, root, a, b)
    assert env["ok"] is False, env
    assert "成环" in env["error"]["message"] or "后代" in env["error"]["message"]


async def test_tool_idempotent(typed_work):
    """已经是该父卡 ⇒ 幂等返回，不重复写（不留无谓历史）。"""
    wid = typed_work["work_id"]
    root = typed_work["work_card_id"]
    a = await _mk(wid, "A", root)
    b = await _mk(wid, "B", a)

    env = await _call(wid, root, b, a)
    assert env["ok"] is True, env
    assert env["data"].get("existed") is True


def test_tool_is_registered_with_edit_switch():
    """工具面与执行层同源：在册、挂 EDIT 开关、非只读。"""
    assert "reparent_card" in card_tools.TOOL_REGISTRY
    assert card_tools.TOOL_SWITCH["reparent_card"] == policy.SWITCH_EDIT
    assert card_tools.TOOL_REGISTRY["reparent_card"].readonly is False
    assert "reparent_card" not in card_tools.AUTO_ONLY_TOOLS
    assert "reparent_card" not in card_tools.ROOT_ONLY_TOOLS
    # 无活板时不给改结构（与建卡/连线同档）
    assert "reparent_card" in card_tools._PLAN_GATE_BLOCKED


def test_toolface_only_for_types_that_can_have_children(typed_registry):
    """不能有子卡的卡看不见换父工具（它没有子树可调，给了只会白调一次被拒）。"""
    def _names(card_type: str) -> set[str]:
        cards = {"c": {"card_id": "c", "card_type": card_type,
                       "perms": policy.perms_for("rd_project", card_type)}}
        return {t["name"] for t in card_tools.build_tool_schemas("c", cards=cards)}

    assert "reparent_card" in _names("sub_project")
    assert "reparent_card" not in _names("tool")


# ---------------------------------------------------------------- 人面（PATCH）

async def test_patch_reparent_ok(client, typed_work):
    """人的通路：画布拖拽走 PATCH，与模型工具落到同一道存储校验。"""
    wid = typed_work["work_id"]
    root = typed_work["work_card_id"]
    a = await _mk(wid, "A", root)
    c = await _mk(wid, "C", root)

    r = await client.patch(f"/api/works/{wid}/cards/{a}",
                           json={"updates": {"parent": c}, "history_note": "拖拽改归属"})
    assert r.status_code == 200, r.text
    assert card_store.iter_card_metas(wid)[a]["parent"] == c


async def test_patch_reparent_rejects_tool_parent(client, typed_work):
    """人的通路同样被拒：400 + 具体原因（前端拿它当回弹提示）。"""
    wid = typed_work["work_id"]
    root = typed_work["work_card_id"]
    a = await _mk(wid, "A", root)
    tool = await _mk(wid, "工具卡", root, card_type="tool", owner_card_id=a)

    r = await client.patch(f"/api/works/{wid}/cards/{a}",
                           json={"updates": {"parent": tool}, "history_note": "拖到工具卡上"})
    assert r.status_code == 400, r.text
    assert "不可有子卡" in r.json()["error"]
    assert card_store.iter_card_metas(wid)[a]["parent"] == root


# ---------------------------------------------------------------- 前端（源码级）

def _js() -> str:
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    return (root / CANVAS_JS).read_text(encoding="utf-8")


def test_drag_no_longer_persists_position():
    """★ 用户裁定「空白处＝回弹，什么都不做」：拖拽路径不再写坐标。

    防回退：一旦有人把 persistCardPos 加回 onDragUp，拖一张卡到空白处就会
    改同层顺序——那正是本次被否掉的旧语义。
    """
    js = _js()
    start = js.index("function onDragUp(e)")
    end = js.index("// ── [换父 2026-10-20]", start)
    body = js[start:end]
    assert "persistCardPos(" not in body, "onDragUp 又写坐标了（空白回弹被破坏）"
    assert "normalizeSiblingOrder(" not in body, "onDragUp 又调同层归一了"
    assert "_revertDragVisual()" in body, "缺回弹"
    assert "_applyReparent(" in body, "缺改父调用"


def test_drop_verdict_rules_present():
    """前端只判结构事实：自己 / 自己的后代 / 已经是子卡；空白＝静默回弹。"""
    js = _js()
    assert "function _dropVerdict(" in js
    assert "function _descendantIds(" in js
    assert "不能挂到自己身上" in js
    assert "会成环" in js
    assert "if (!targetId) return { ok: false, reason: \"\" }" in js, \
        "空白落点必须是静默回弹（不带提示）"


def test_drop_highlight_classes_exist():
    """落点高亮：可挂（drop-ok）/ 不可挂（drop-bad），样式在 canvas.css 里。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    js = _js()
    css = (root / "static" / "canvas.css").read_text(encoding="utf-8")
    assert '"drop-ok"' in js and '"drop-bad"' in js
    assert ".card-node.drop-ok" in css
    assert ".card-node.drop-bad" in css
