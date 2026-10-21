# -*- coding: utf-8 -*-
"""[第二层区 2026-10-20 · card_004] 根卡上下文里的「职能角色现状＋变化信号」区。

**为什么需要这个区（不是设计偏好，是实测结论）**
card_004 的组阁判断力实验（`docs/5713-组阁判断力实验-报告-20261020.md`）真机实测：
  · 零干预下根卡 45 次工具调用里 `create_role` 零出现，尽管工具在手、四个跨卡问题全发现；
  · goal 末尾加一句「该不该有人专门盯着」，它立刻组出高质量角色；
  · 局面变了（新增一张冲突卡）后，它 `list_roles` → `read_card` **读过角色卡**，
    仍然不改档、不撤岗、不扩关注范围 ⇒ **会设，不会调**。
所以调阁触发必须外置成机制：每轮把现状与变化摊到根卡眼前。

**本文件的判据全部是结构事实**：给定 `ctx.role_cabinet` / `ctx.cards`，断言渲染文本里
出现/不出现**具体卡片 id、角色 id、数字**——不猜模型措辞，也不检查"建议"语气。
"""
from __future__ import annotations

from src.api.context_regions import RegionCtx, render_role_cabinet_region


def _card(cid: str, title: str, *, parent: str = "card_001",
          ctype: str = "sub_project", created: str = "2026-10-20T10:00:00") -> dict:
    return {"card_id": cid, "title": title, "parent": parent,
            "card_type": ctype, "created_at": created, "relations": []}


def _role(cid: str, *, key: str = "timeline_contract", watch=None, duty="active",
          version=1, created="2026-10-20T10:00:00", recent=None) -> dict:
    return {"role_id": cid, "exclusive_key": key, "watch": list(watch or []),
            "duty": duty, "charter_version": version, "created_at": created,
            "recent": list(recent or [])}


def _ctx(cards, roles, work_card_id="card_001") -> RegionCtx:
    return RegionCtx(work_id="work_001", card_id=work_card_id, cards=cards,
                     work_card_id=work_card_id, role_cabinet={"roles": roles})


def test_other_cards_get_no_region():
    """非根卡：取数层已过滤成 {} ⇒ 渲染必须返回空串（别把阁员状态摊给作业卡）。"""
    ctx = _ctx([_card("card_002", "甲"), _card("card_003", "乙")], [])
    ctx.card_id = "card_002"
    ctx.role_cabinet = {}
    assert render_role_cabinet_region(ctx) == ""


def test_single_work_card_without_roles_does_not_nag():
    """只有 1 张工作卡且无角色 ⇒ 不出现（还没到"有跨卡面"的程度）。"""
    ctx = _ctx([_card("card_002", "甲")], [])
    assert render_role_cabinet_region(ctx) == ""


def test_no_roles_but_multiple_cards_shows_founding_point():
    """≥2 张工作卡且无角色 ⇒ 出区，并给出**事实**：这些卡没有角色在关注。"""
    ctx = _ctx([_card("card_002", "甲"), _card("card_003", "乙"), _card("card_004", "丙")], [])
    text = render_role_cabinet_region(ctx)
    assert "第二层" in text
    assert "3 张工作卡" in text
    assert "没有任何角色在关注" in text
    # 中性边界：不得出现"你必须/应该组阁"这类指令
    assert "应该组阁" not in text and "必须组阁" not in text
    assert "由你按局面判断" in text


def test_new_card_after_founding_is_flagged_as_change():
    """核心信号：组阁之后新增、且没有角色在看的卡 → 明确点名（D07 实测的失效点）。"""
    cards = [
        _card("card_002", "订单结算接口", created="2026-10-20T09:00:00"),
        _card("card_003", "库存对账接口", created="2026-10-20T09:00:00"),
        _card("card_006", "支付回调接口", created="2026-10-20T21:59:00"),   # 组阁后新增
    ]
    roles = [_role("card_005", watch=["card_002", "card_003"],
                   created="2026-10-20T21:44:00")]
    text = render_role_cabinet_region(_ctx(cards, roles))
    assert "组阁之后新增的卡" in text
    assert "card_006" in text and "支付回调接口" in text
    # 已被关注的卡不该被当成盲区
    assert "没有角色在看：card_002" not in text
    # 阁员现状要摊开（省掉根卡自己查）
    assert "card_005" in text and "timeline_contract" in text


def test_all_cards_watched_yields_no_signal_block():
    """所有卡都在关注里、也无异常 ⇒ 只出阁员现状，不制造噪音信号。"""
    cards = [_card("card_002", "甲"), _card("card_003", "乙")]
    roles = [_role("card_005", watch=["card_002", "card_003"])]
    text = render_role_cabinet_region(_ctx(cards, roles))
    assert "card_005" in text
    assert "机械检测到的局面变化" not in text


def test_silent_streak_and_empty_watch_are_flagged():
    """角色连续 3 轮 silent / 关注集合为空 ⇒ 都是机械可检的"该复盘"事实。"""
    cards = [_card("card_002", "甲"), _card("card_003", "乙")]
    silent = [{"decision": "silent", "ts": "2026-10-20T21:00:00"},
              {"decision": "silent", "ts": "2026-10-20T21:05:00"},
              {"decision": "silent", "ts": "2026-10-20T21:10:00"}]
    roles = [_role("card_005", watch=["card_002", "card_003"], recent=silent),
             _role("card_007", key="cost_budget", watch=[], recent=[])]
    text = render_role_cabinet_region(_ctx(cards, roles))
    assert "3 轮判词全为 silent" in text
    assert "关注集合为空" in text
    assert "card_007" in text


def test_archived_role_is_counted_not_listed_as_active():
    """撤岗角色不进在值名单（但要点出"已撤岗、台账保留"）。"""
    cards = [_card("card_002", "甲"), _card("card_003", "乙")]
    roles = [_role("card_005", watch=["card_002", "card_003"]),
             _role("card_009", key="old_domain", watch=[], duty="archived")]
    text = render_role_cabinet_region(_ctx(cards, roles))
    assert "当前阁员 1 人在值" in text
    assert "1 人已撤岗" in text


def test_region_covers_all_tasks_and_ordering_is_fixed():
    """注册表口径：order 175，紧跟 170 直接子卡现状、早于 200 记忆区。"""
    from src.api import context_regions as cr
    keys = {r["key"]: r for r in cr.REGION_TABLE}
    assert "role_cabinet" in keys
    spec = keys["role_cabinet"]
    assert spec["order"] == 175
    assert spec["delivery"] == "system"
    assert spec["profiles"] == ("main",)
    orders = sorted(r["order"] for r in cr.REGION_TABLE)
    assert orders == sorted(set(orders)), "order 不许重复"
    assert 170 < spec["order"] < 200