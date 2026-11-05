# -*- coding: utf-8 -*-
"""[连线不看对方 2026-10-22 · 用户令] 建线／解线只判**发起方**，不判目标卡。

用户原话：「为什么要他妈的对方同意，对方他妈的怎么同意？做的什么垃圾的他妈的机制？」

改前：``policy.check(..., SCOPE_ANY)`` 的第一级是「**目标卡** perms 须授 link」，
理由是「线是双向的，被连方也要能回话」。这个「同意」是伪命题：
  · 目标卡的 perms 是建卡时从类型模板抄下来的静态值，没有任何「同意」动作；
  · 用户也没有任何地方能表示同意／拒绝；
  · 实际效果只是「role / worker / tool / rule 四类永远连不上」（配置里它们没有 link）。
实测：work_022 的根卡连自己的角色卡 card_003/card_004，四次全 denied。

线的记录写在**发起方**卡的 ``links`` / ``relations`` 字段上，被连方不需要同意；
真正需要双方的是**发信**，那一关由 ``check_linked``（两卡之间已有线）把着，
与 perms 无关——本文件同时钉住这一点，防止「放开建线」被误读成「放开通信」。
"""
from __future__ import annotations

import pytest

from src.api import card_tools
from src.storage import card_store, work_store


async def _work_with_pair(title: str) -> tuple[str, dict]:
    """一个项目 ＋ 根卡(card_001) ＋ 一张子卡(card_002)。"""
    wid = (await work_store.create_work(title))["work_id"]
    await card_store.create_card(wid, "对方卡", "用户直接创建",
                                 card_type="sub_project", parent="card_001")
    return wid, card_store.iter_card_metas(wid)


def _strip(cards: dict, cid: str, *switches: str) -> dict:
    """**只在内存里**把某张卡的指定开关抹掉（不动库）——模拟「该类型没配这个开关」。

    [能力底座 2026-11-01] 判权已迁到 ``abilities``（perms 只是回退）：抹开关
    要连对应**能力码**一起抹。能力码与开关名多数字面相同（link/unlink/
    read_linked/link_rel…），按 impl.switch 反查一遍更稳——找不到就按同名抹。
    """
    meta = dict(cards.get(cid) or {})
    meta["perms"] = [s for s in (meta.get("perms") or []) if s not in switches]
    try:
        from src.infra import capability_registry as _cap
        codes = {str(c.get("code")) for c in _cap.load().get("capabilities", [])
                 if str((c.get("impl") or {}).get("switch") or "") in switches}
    except Exception:  # noqa: BLE001 清单读不出 ⇒ 按同名抹
        codes = set()
    codes |= set(switches)
    meta["abilities"] = [a for a in (meta.get("abilities") or [])
                         if a not in codes]
    cards[cid] = meta
    return cards


async def test_link_works_when_target_has_no_link_switch(sandbox):
    """目标卡 perms 里没有 link ⇒ 发起方照样能建线（改前这里 denied）。"""
    wid, cards = await _work_with_pair("连线不看对方·建线")
    _strip(cards, "card_002", "link", "unlink")

    r = await card_tools._h_link_cards(
        wid, "card_001", {"target_id": "card_002", "type": "关联"}, cards)
    assert r.get("ok") is True, r
    assert r["target"] == "card_002"

    saved = (await card_store.read_card(wid, "card_001"))["meta"].get("links") or []
    assert any(x.get("target") == "card_002" for x in saved), "线必须落到发起方卡上"


async def test_link_rel_works_when_target_has_no_link_switch(sandbox):
    """依赖关联线同款：只看发起方。"""
    wid, cards = await _work_with_pair("连线不看对方·依赖线")
    _strip(cards, "card_002", "link", "unlink")

    r = await card_tools._h_link_rel(
        wid, "card_001",
        {"target_id": "card_002", "kind": "依赖", "detail": "测试依赖"}, cards)
    assert r.get("ok") is True, r


async def test_unlink_works_when_target_has_no_unlink_switch(sandbox):
    """解线同理：目标卡没有 unlink 也能解（线记在发起方卡上）。"""
    wid, cards = await _work_with_pair("连线不看对方·解线")
    ok = await card_tools._h_link_cards(
        wid, "card_001", {"target_id": "card_002", "type": "先后"}, cards)
    assert ok.get("ok") is True, ok

    cards = card_store.iter_card_metas(wid)
    _strip(cards, "card_002", "link", "unlink")
    r = await card_tools._h_unlink_cards(
        wid, "card_001", {"target_id": "card_002"}, cards)
    assert r.get("ok") is True, r
    assert r["removed"] == 1


async def test_actor_without_link_switch_is_still_denied(sandbox):
    """不放开过头：**发起方**没有 link ⇒ 照样拒（这是「我能不能建线」，不是「对方同不同意」）。"""
    wid, cards = await _work_with_pair("连线不看对方·发起方无权")
    _strip(cards, "card_001", "link")

    r = await card_tools._h_link_cards(
        wid, "card_001", {"target_id": "card_002", "type": "关联"}, cards)
    assert r.get("ok") is False, r
    assert r["error"]["code"] == "denied"
    assert "link" in r["error"]["message"], "拒因要说清缺哪个开关"


async def test_wire_does_not_grant_messaging(sandbox):
    """放开建线 ≠ 放开通信：没有线的两张卡，发信仍被 ``check_linked`` 拒。

    这条是「不放开过头」的边界：线的存在与否由 ``_linked_between`` 判，
    与 perms 无关——所以「目标卡没 link 也能被连」不会变成「谁都能给谁发信」。
    """
    from src.permissions import policy

    wid, cards = await _work_with_pair("连线不看对方·通信边界")
    # [2026-11-01] 「无线」前提改用**两张兄弟卡**：父子现在自带默认通信线
    # （child→parent），check_linked 任一侧有边即成立 ⇒ 001↔002 恒有线。
    # 兄弟卡各自只有指向父卡的默认线、彼此无边 ⇒ check_linked 仍拒。
    r0 = await card_store.create_card(wid, "兄弟卡", "用户直接创建",
                                      card_type="sub_project", parent="card_001")
    sib = r0["meta"]["card_id"]
    cards = card_store.iter_card_metas(wid)
    with pytest.raises(PermissionError):
        policy.check_linked("card_002", sib, cards)

    await card_tools._h_link_cards(
        wid, "card_001", {"target_id": "card_002", "type": "关联"}, cards)
    cards = card_store.iter_card_metas(wid)
    assert policy.check_linked("card_001", "card_002", cards) is True


async def test_send_message_does_not_check_target_read(sandbox):
    """发信不查目标卡的 `read`（改前查，只是「统一关口」的副作用）。

    所有类型都有 read，所以那道检查从来没拦过任何人——它唯一的作用是让报错
    说不清到底卡在哪。能不能发＝发起方有 send_card_message；能不能发给这个对象
    ＝两卡之间有线。
    """
    wid, cards = await _work_with_pair("连线不看对方·发信")
    await card_tools._h_link_cards(
        wid, "card_001", {"target_id": "card_002", "type": "关联"}, cards)
    cards = card_store.iter_card_metas(wid)
    _strip(cards, "card_002", "read")          # 目标卡连 read 都没有

    # [能力底座 2026-11-01] send_card_message 已退役出模型工具面，能力清单里
    # 没有 switch=send_card_message 的能力码 ⇒ 模型路径的发信开关关恒拒。
    # 本用例钉的是「发信不查**目标卡** read」⇒ 保留通信线闸、跳过发起方
    # 开关关（与系统薄壳同形），语义不减。
    r = await card_tools._h_send_card_message(
        wid, "card_001",
        {"target_card_id": "card_002", "msg_type": "msg_notify", "content": "测试"},
        cards, require_switch=False)
    assert r.get("msg_id"), r          # 成功返回 msg_id（没有 ok 字段）


async def test_read_linked_does_not_check_target_read_linked(sandbox):
    """沿线读不查目标卡的 `read_linked`——线本身就是凭据，不需要第二道许可。

    改前查目标卡 ⇒ worker / tool / rule 三类（没有 read_linked）永远读不了。
    """
    wid, cards = await _work_with_pair("连线不看对方·沿线读")
    await card_tools._h_link_cards(
        wid, "card_001", {"target_id": "card_002", "type": "关联"}, cards)
    cards = card_store.iter_card_metas(wid)
    _strip(cards, "card_002", "read_linked")

    r = await card_tools._h_read_linked(
        wid, "card_001", {"target_id": "card_002", "part": "正文"}, cards)
    assert r.get("ok") is not False or r.get("error", {}).get("code") != "denied", r
