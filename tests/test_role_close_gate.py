# -*- coding: utf-8 -*-
"""[5713 收口闸 · card_023] 零角色收口闸：根卡收口时必须真的有人盯。

[方向变更 2026-10-21 · card_004 通知 msg card_004-799a7954] 「写【无人盯】理由即可放行」
作废——用户原话「强制它做一步拆分团队，不要规定它拆法，只是强制它拆」。唯一出路＝
真的调 create_role 建成至少一个非空壳 active 角色（duty.state=active 且 exclusive_key
与职责书均非空）；空壳与不设同罪。闸只要求「拆」，绝不规定拆法（守 D02）。

判据（纯机械，同时满足才拦）——落点在 goal_tools._zero_role_close_gate_block：
  ① 收口者是项目根卡（current_id == work_store.work_card_id_of(work_id)）；
  ② 该 work 工作卡 ≥ 2（card_type != "role"、parent != ""、不是根卡自己，
     影子卡 card_type="shadow" 天然排除）；
  ③ 无非空壳 active 角色（duty.state=="active" 才算在盯，suspended/archived 不算；
     active 角色里至少一个 exclusive_key 非空、职责书非空才算「非空壳」）。

闸同时挂两条路径（单一判据源）：
  · 生产路径 goal_tools.h_update_goal（指令闸后、authority 前——未开放回合也拦）；
  · 任务书指定位置 card_tools._h_update_goal（定论闸后、未回应询问闸前）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from src.api import auto_driver, card_tools, role_room
from src.goal import authority as goal_authority
from src.goal import tools as goal_tools
from src.goal.service import get_service
from src.storage import card_store, role_store


@pytest.fixture(autouse=True)
def _reset_goal_service_cache():
    """清空 GoalService 进程级单例缓存（跨沙箱测试串扰根治）。

    所有用例共享 work_001/card_001，而 ``goal.service.SERVICE`` 是进程级单例，
    其 ``_caches`` 按 (work_id, card_id) 缓存且不随 conftest 沙箱重置——旧用例
    的 ``observed_seq`` 残留会让新沙箱的 goal/change 事件（seq 从 1 重新计）被
    ``_sync`` 跳过，fold 永远停在旧态，complete 投影不落库（实测整跑红、单测绿）。
    """
    from src.goal.service import SERVICE
    SERVICE._caches.clear()
    yield
    SERVICE._caches.clear()


@pytest.fixture
def no_kick(monkeypatch):
    """拦掉真实起轮（同 test_auto_mode / test_decision_gate）。"""
    calls: list[tuple[str, str]] = []

    def _rec(w: str, c: str):
        if auto_driver.will_continue(w, c):
            calls.append((w, c))
        return None

    monkeypatch.setattr(auto_driver, "kick", _rec)
    return calls


async def _mk_child(wid: str, root: str, title: str) -> str:
    r = await card_store.create_card(
        wid, title=title, source="用户直接创建", card_type="sub_project",
        parent=root, charter=f"## 使命\n{title}")
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def _mk_role(wid: str, root: str) -> str:
    """建一个非空壳 active 角色（经 create_role 真实路径）。"""
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools._h_create_role(wid, root, {
        "title": "一致性观察员",
        "charter": "盯住工作卡之间是否自相矛盾；发现矛盾先发建议，结构性矛盾发警报；无新增矛盾则沉默。",
        "jurisdiction": {"domain": "timeline_contract", "statement": "接口层时间口径"},
        "watch_card_ids": [],
        "signals": ["turn_completed"],
    }, cards)
    assert "error" not in r, r
    return r["role_card_id"]


async def _mk_shell_role(wid: str, *, kind: str = "charter") -> str:
    """建一个空壳 active 角色：kind="charter" ⇒ 职责书空；kind="key" ⇒ exclusive_key 空。"""
    r = await card_store.create_card(
        wid, title="空壳角色", source="系统派生", card_type="role",
        parent="", charter="## 使命\n空壳", skip_kick=True)
    assert "error" not in r, r
    rid = r["meta"]["card_id"]
    now = datetime.now().astimezone().isoformat()
    if kind == "key":
        jur = {"domain": "", "exclusive_key": "", "statement": ""}
        charter_text = "有职责书但无排他域"
    else:
        jur = {"domain": "shell_domain", "exclusive_key": "shell_key", "statement": ""}
        charter_text = ""
    prof = {
        "jurisdiction": jur,
        "watch": {"cards": [], "signals": ["turn_completed"]},
        "speak_contract": {"advice_when": ["gap_or_risk"],
                           "alarm_when": ["mutually_exclusive_fact"],
                           "silence_default": True, "max_messages_per_wake": 2},
        "escalation": {"advice_to": "source_card", "alarm_to": "human",
                       "critical_to": "root_and_human"},
        "duty": {"state": "active", "since": now, "last_wake_at": "", "pending_sync": 0},
        "charter": {"version": 1, "text": charter_text,
                    "ratified_by": "root", "ratified_at": now},
    }
    await role_store.ensure_profile(wid, rid, prof)
    return rid


async def _gate(wid: str, cid: str) -> dict | None:
    """机械判据：直接调零角色闸 helper（生产路径与任务书位置同源）。"""
    cards = card_tools._collect_card_meta(wid)
    return await goal_tools._zero_role_close_gate_block(wid, cid, cards)


async def _mk_root_auto(work) -> str:
    """根卡置自动模式（生产 complete 需 auto 卡）。"""
    wid, root = work["work_id"], work["work_card_id"]
    await card_store.update_card(
        wid, root,
        {"auto_mode": 1, "goal": "把项目做完", "goal_state": "active"},
        "开自动模式")
    return root


# ---------------------------------------------------------------- 机械判据
async def test_root_two_work_cards_zero_roles_rejected(work):
    """根卡＋2 张工作卡＋零角色 ⇒ 拦；错误含卡片数量与 create_role 出路。"""
    wid, root = work["work_id"], work["work_card_id"]
    await _mk_child(wid, root, "甲")
    await _mk_child(wid, root, "乙")
    res = await _gate(wid, root)
    assert res is not None and res["ok"] is False, res
    assert res["error"]["code"] == "validation", res
    msg = res["error"]["message"]
    assert "2 张工作卡" in msg, msg
    assert "create_role" in msg, msg
    assert "必须组阁" not in msg and "应该组阁" not in msg, "不得出现指令式组阁措辞"


async def test_root_one_work_card_passes(work):
    """只有 1 张工作卡 ⇒ 不拦（判据②不成立）。"""
    wid, root = work["work_id"], work["work_card_id"]
    await _mk_child(wid, root, "甲")
    assert await _gate(wid, root) is None


async def test_non_root_card_not_blocked(work):
    """非根卡（子卡）complete ⇒ 不拦（判据①不成立），即使 work 下 2 张工作卡零角色。"""
    wid, root = work["work_id"], work["work_card_id"]
    await _mk_child(wid, root, "甲")
    await _mk_child(wid, root, "乙")
    a = await _mk_child(wid, root, "子卡甲")
    assert await _gate(wid, a) is None


async def test_root_two_work_cards_solid_role_passes(work):
    """已建 1 个非空壳 active 角色 ⇒ 放行。"""
    wid, root = work["work_id"], work["work_card_id"]
    await _mk_child(wid, root, "甲")
    await _mk_child(wid, root, "乙")
    await _mk_role(wid, root)
    assert await _gate(wid, root) is None


async def test_root_two_work_cards_all_archived_rejected(work):
    """角色存在但全部 archived ⇒ 仍拦（撤岗了就是没人盯）。"""
    wid, root = work["work_id"], work["work_card_id"]
    await _mk_child(wid, root, "甲")
    await _mk_child(wid, root, "乙")
    rid = await _mk_role(wid, root)
    await role_room.set_duty(wid, rid, "archived", actor="测试撤岗")
    res = await _gate(wid, root)
    assert res is not None and res["ok"] is False, res
    assert "2 张工作卡" in res["error"]["message"]
    assert "create_role" in res["error"]["message"]


async def test_root_two_work_cards_shell_charter_rejected(work):
    """只建了空壳角色（职责书空）⇒ 仍拦。"""
    wid, root = work["work_id"], work["work_card_id"]
    await _mk_child(wid, root, "甲")
    await _mk_child(wid, root, "乙")
    await _mk_shell_role(wid, kind="charter")
    res = await _gate(wid, root)
    assert res is not None and res["ok"] is False, res
    assert "create_role" in res["error"]["message"]


async def test_root_two_work_cards_shell_key_rejected(work):
    """只建了空壳角色（exclusive_key 空）⇒ 仍拦。"""
    wid, root = work["work_id"], work["work_card_id"]
    await _mk_child(wid, root, "甲")
    await _mk_child(wid, root, "乙")
    await _mk_shell_role(wid, kind="key")
    res = await _gate(wid, root)
    assert res is not None and res["ok"] is False, res


# ---------------------------------------------------------------- 生产路径接线
async def test_production_handler_blocks_before_authority(work, no_kick):
    """生产路径 goal_tools.h_update_goal：根卡＋2 工作卡＋零角色 complete ⇒ 在
    authority 之前就被零角色闸拦下（错误码 validation、含 create_role）。"""
    wid = work["work_id"]
    root = await _mk_root_auto(work)
    await _mk_child(wid, root, "甲")
    await _mk_child(wid, root, "乙")

    # 目标已被 _mk_root_auto 的 legacy goal_state 自动回填；读回即可拿到 view。
    # 零角色闸在 authority 之前拦截，goal_id/revision 不会被校验到（同指令闸手法）。
    view = await get_service().get(wid, root)
    assert view is not None, "legacy goal_state 应回填出 goal"
    token = goal_authority.set_turn_source({"kind": "user"})
    try:
        cards = card_tools._collect_card_meta(wid)
        res = await goal_tools.h_update_goal(wid, root, {
            "goal_id": view.id, "revision": view.revision, "action": "complete",
        }, cards)
    finally:
        goal_authority.reset_turn_source(token)
    assert res["ok"] is False, res
    assert res["error"]["code"] == "validation", res
    assert "create_role" in res["error"]["message"]


async def test_production_handler_passes_with_solid_role(work, no_kick):
    """生产路径：2 工作卡＋已建非空壳 active 角色 complete ⇒ 放行并落 complete。"""
    wid = work["work_id"]
    root = await _mk_root_auto(work)
    await _mk_child(wid, root, "甲")
    await _mk_child(wid, root, "乙")
    await _mk_role(wid, root)

    # 目标已被 _mk_root_auto 的 legacy goal_state 自动回填；读回即可拿到 view。
    view = await get_service().get(wid, root)
    assert view is not None, "legacy goal_state 应回填出 goal"
    token = goal_authority.set_turn_source({"kind": "user"})
    try:
        cards = card_tools._collect_card_meta(wid)
        res = await goal_tools.h_update_goal(wid, root, {
            "goal_id": view.id, "revision": view.revision, "action": "complete",
        }, cards)
    finally:
        goal_authority.reset_turn_source(token)
    assert res["ok"] is True, res
    assert card_store._meta_of(wid, root)["goal_state"] == "complete"
