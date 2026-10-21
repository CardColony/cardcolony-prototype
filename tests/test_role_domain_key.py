# -*- coding: utf-8 -*-
"""[排他键保全 2026-10-20 · card_004] 改档不得静默丢掉 exclusive_key。

**这条是真机实测挖出来的，不是我猜的**
175 区（第二层·职能角色）首次真机验收（`evidence_cabinet_region.json`，work_001）：
根卡做了**正确**的调阁动作——职责书 v1→v2、关注集合扩到新卡 `card_006`、
调用 `role_watch_cards` ＋ `update_role_profile`。但改完之后角色的档案变成：

    {"domain": "interface_contract", "statement": "本项目对外接口(card_002/003/004/006)…"}
                                                              ↑ exclusive_key 不见了

根因：`role_room.update_profile` 里 `changed["jurisdiction"] = patch["jurisdiction"]`
是**整字典覆盖**。模型只想改职责范围，自然只写 domain+statement，排他键就被擦掉。

为什么这是缺陷而不是小事：排他键是「一域一角色」的地基。虽然 `_domain_conflict`
有 `exclusive_key or domain` 兜底（唯一性暂未破），但**身份标识丢了** ⇒ 175 区渲染出
「域=?」、根卡失去"这域归谁"的凭据，多改几次档就再也追不回来。

判据全部机械：改档后直接读 profile 的 `jurisdiction.exclusive_key`，以及"同域能否再组一个"。
"""
from __future__ import annotations

import pytest

from src.api import card_tools, role_room
from src.storage import card_store


@pytest.fixture(autouse=True)
def _reset_role_module_state():
    role_room._LAST_SPAWN.clear()
    role_room._ACTIVE.clear()
    role_room._LOCKS.clear()
    yield
    role_room._LAST_SPAWN.clear()
    role_room._ACTIVE.clear()
    role_room._LOCKS.clear()


async def _mk_child(wid: str, root: str, title: str) -> str:
    r = await card_store.create_card(
        wid, title=title, source="用户直接创建", card_type="sub_project",
        parent=root, charter=f"## 使命\n{title}")
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def _mk_role(wid: str, root: str, watch: list[str], *,
                   title: str = "契约观察员", key: str = "interface_contract") -> str:
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools._h_create_role(wid, root, {
        "title": title,
        "charter": "盯接口契约一致性；互斥才报警，否则沉默。",
        "jurisdiction": {"domain": key, "statement": "接口口径一致性",
                         "exclusive_key": key},
        "watch_card_ids": watch,
        "signals": ["status_changed"],
    }, cards)
    assert "error" not in r, r
    return r["role_card_id"]


def _key_of(wid: str, rid: str) -> str:
    prof = role_room.role_store.get_profile(wid, rid) or {}
    return str(((prof.get("jurisdiction") or {}).get("exclusive_key")) or "")


async def test_edit_statement_only_keeps_exclusive_key(work):
    """真机复现的那个调用形状：只写 domain+statement ⇒ 键必须继承。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "接口甲")
    rid = await _mk_role(wid, root, [c])
    assert _key_of(wid, rid) == "interface_contract"

    res = await role_room.update_profile(wid, rid, {
        "jurisdiction": {"domain": "interface_contract",
                         "statement": "范围从一张接口扩到两张接口"},
    })
    assert res.get("error") is None, res
    assert _key_of(wid, rid) == "interface_contract"      # ← 修复前这里是 ""（None）
    # domain 也必须在（只改 statement 不该把 domain 弄丢）
    prof = role_room.role_store.get_profile(wid, rid) or {}
    assert (prof.get("jurisdiction") or {}).get("domain") == "interface_contract"


async def test_statement_only_edit_keeps_uniqueness_enforced(work):
    """改档后同域仍只能有一个角色——唯一性不能因为键丢了而失效。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "接口甲")
    rid = await _mk_role(wid, root, [c])
    await role_room.update_profile(wid, rid, {
        "jurisdiction": {"domain": "interface_contract", "statement": "改一次范围"}})

    cards = card_tools._collect_card_meta(wid)
    dup = await card_tools._h_create_role(wid, root, {
        "title": "撞域角色", "charter": "抢同一个域",
        "jurisdiction": {"domain": "interface_contract", "statement": "撞域",
                         "exclusive_key": "interface_contract"},
        "watch_card_ids": [c]}, cards)
    assert dup["ok"] is False
    assert "责任域" in str(dup["error"]["message"])


async def test_explicit_new_key_is_respected(work):
    """显式传新键 ⇒ 听传的（不能被"继承旧键"霸占）。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "接口甲")
    rid = await _mk_role(wid, root, [c])
    await role_room.update_profile(wid, rid, {
        "jurisdiction": {"domain": "interface_contract",
                         "statement": "改名", "exclusive_key": "contract_renamed"}})
    assert _key_of(wid, rid) == "contract_renamed"


async def test_domain_move_without_key_tracks_new_domain(work):
    """有意换域（domain 变了）而不带键 ⇒ 键跟新 domain 走，不继承旧键。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "接口甲")
    rid = await _mk_role(wid, root, [c])
    await role_room.update_profile(wid, rid, {
        "jurisdiction": {"domain": "cost_budget", "statement": "改管成本"}})
    assert _key_of(wid, rid) == "cost_budget"