# -*- coding: utf-8 -*-
"""[自动模式启动必须轮 2026-10-21 · card_040] 根卡 auto 启动第一轮只建职能、不搭交付树。

契约（判据全部取自卡面事实，不看请求参数——驱动器发起的回合不带 op）：
    是「必须轮」 =
          根卡（card_id == work_card_id_of(work_id)）
      且  auto_mode == 1
      且  本 work 一个职能都没有（function_store.list_profiles(work_id) 为空）
      且  goal_rounds == 0
命中时必须轮 ⇒ build_tool_schemas 返回的工具面里：
    · 必须没有 create_card、create_worker_card（这一轮不搭交付树）
    · 必须有 create_function（建职能的唯一通道）
    · 保留：update_status、edit_requirements、update_shadow、update_goal、read_card、
      list_functions、只读文件类（read_file / list_files / search_files）
任一条件不成立 ⇒ 回到常规工具面（有 create_card）。

判据对照（任务书验收判据）：
- 判据② 场景 A 命中必须轮：根卡+auto=1+无职能+rounds=0 ⇒ 无 create_card/create_worker_card、有 create_function
- 判据③ 场景 B/C/D/E 四条反例：已跑过一轮 / 已有职能 / 非根卡 / 非 auto ⇒ 有 create_card
- 判据④ 场景 F：必须轮面上 create_function/update_status/edit_requirements 都在
- 判据⑤ 场景 G：只读件 read_card/read_file/list_files/search_files 都在
- 判据⑥ fail-open：function_store.list_profiles 抛异常 ⇒ 不触发闸门（仍有 create_card），并留 logger.warning
（判据① 走 verify/5713/verify_must_round.py；判据⑦ 由整套 pytest 跑分。）
"""
from __future__ import annotations

import pytest

from src.api import card_tools
from src.permissions import policy
from src.storage import card_store, function_store, work_store


async def _faces(wid, root, *, auto, rounds, n_functions=0, sub=False):
    """构造卡面后取 build_tool_schemas 工具名集合。

    真实在沙箱 work 里开 auto_mode / 设 goal_rounds / 建职能（判据③的「已有职能」
    走真库：function_store 建一行），再由 _collect_card_meta 收集真实卡面——
    与生产链路同源，不造合成 cards 键。
    """
    sub_ids = []
    if sub:
        r = await card_store.create_card(wid, "子项目", "模型生成", parent=root)
        assert "error" not in r, r
        sub_ids.append(r["meta"]["card_id"])
    target = sub_ids[0] if sub else root
    await card_store.update_card(wid, target, {"auto_mode": auto, "goal_rounds": rounds},
                                 "测试设 auto/rounds")
    for _ in range(n_functions):
        p = await function_store.create_function(wid, "测试职能")
        assert "error" not in p, p
    cards = card_tools._collect_card_meta(wid)
    names = {t["name"] for t in card_tools.build_tool_schemas(
        card_id=target, work_id=wid, cards=cards)}
    return names


async def test_a_must_round_hits(typed_registry, work):
    """场景 A：根卡 + auto=1 + 无职能 + rounds=0 ⇒ 摘 create_card/create_worker_card、留 create_function。"""
    wid, root = work["work_id"], work["work_card_id"]
    names = await _faces(wid, root, auto=1, rounds=0)
    assert "create_card" not in names, f"必须轮不得搭交付树，实得含 create_card"
    assert "create_worker_card" not in names
    assert "create_function" in names, "必须轮须有建职能唯一通道 create_function"


async def test_b_ran_a_round_untouched(typed_registry, work):
    """场景 B：已跑过一轮（rounds=2）⇒ 解除必须轮，返回常规面（有 create_card）。"""
    wid, root = work["work_id"], work["work_card_id"]
    names = await _faces(wid, root, auto=1, rounds=2)
    assert "create_card" in names


async def test_c_has_function_untouched(typed_registry, work):
    """场景 C：已有职能 ⇒ **仍**命中必须轮。

    [契约轮换代 1ed5f3a] 原判据「已有职能 ⇒ 解除必须轮」已废除——现在的
    契约轮是「首轮固定写需求文档」，与有没有职能无关（function_store 建
    行只为验证它不影响判定）。解除条件只剩：非根卡 / 非 auto / rounds>0。
    """
    wid, root = work["work_id"], work["work_card_id"]
    names = await _faces(wid, root, auto=1, rounds=0, n_functions=1)
    assert "create_card" not in names
    assert "create_function" in names


async def test_d_non_root_untouched(typed_registry, work):
    """场景 D：非根卡（sub_project）+ auto=1 + 无职能 + rounds=0 ⇒
    不吃**必须轮**；但它另有「无正稿 ⇒ 摘派工」的需求闸（card_tools.py:9058
    用户令），新卡 requirements_state 未批 ⇒ create_card 本就不在面。
    必须轮只作用于根卡，这里钉的是 sub_project 不该因 rounds=0 再被摘一层。
    """
    wid, root = work["work_id"], work["work_card_id"]
    names = await _faces(wid, root, auto=1, rounds=0, sub=True)
    assert "create_card" not in names  # 需求闸（非必须轮）
    assert "create_function" not in names  # 建职能是根卡口


async def test_e_not_auto_untouched(typed_registry, work):
    """场景 E：非 auto（auto=0）⇒ 不受管。"""
    wid, root = work["work_id"], work["work_card_id"]
    names = await _faces(wid, root, auto=0, rounds=0)
    assert "create_card" in names


async def test_f_org_tools_stay_in_must_round(typed_registry, work):
    """场景 F：必须轮面上组织类仍在——create_function/update_status/edit_requirements。"""
    wid, root = work["work_id"], work["work_card_id"]
    names = await _faces(wid, root, auto=1, rounds=0)
    for t in ("create_function", "edit_status"):
        assert t in names, f"必须轮组织类 {t} 应保留"
    # 根卡的需求写入是 op 轮专属的 write_requirements_final（OP_ONLY_TOOLS，
    # 只在需求确定那一轮下发）；edit_requirements 不进常规面（契约轮换代后）。
    assert "edit_requirements" not in names


async def test_g_readonly_not_harmed(typed_registry, work):
    """场景 G：只读件未被误伤——read_card/read_file/list_files/search_files。"""
    wid, root = work["work_id"], work["work_card_id"]
    names = await _faces(wid, root, auto=1, rounds=0)
    for t in ("read_card", "read_file", "list_files", "search_files"):
        assert t in names, f"只读件 {t} 应保留"


async def test_goal_update_and_shadow_stay_in_must_round(typed_registry, work):
    """必须轮保留 update_goal / update_shadow（「停」「记录」的权威不能摘）。"""
    wid, root = work["work_id"], work["work_card_id"]
    names = await _faces(wid, root, auto=1, rounds=0)
    for t in ("update_goal", "edit_shadow", "list_functions"):
        assert t in names, f"{t} 应保留在必须轮面"


async def test_fail_open_when_list_profiles_raises(typed_registry, work, monkeypatch, caplog):
    """场景⑥ fail-open：function_store.list_profiles 抛异常 ⇒ 不触发闸门（仍有 create_card），并留 logger.warning。"""
    wid, root = work["work_id"], work["work_card_id"]
    await card_store.update_card(wid, root, {"auto_mode": 1, "goal_rounds": 0}, "设 auto")

    # [契约轮换代 1ed5f3a] 原 fail-open 缝是 function_store.list_profiles
    # 抛异常 ⇒ 按「已有职能」放行；现在 _is_must_round **压根不读职能**
    # （三条件：根卡/auto/rounds==0）——list_profiles 炸了也必须轮照触，
    # 证明函数依赖已彻底移除（防回归引入隐藏读）。
    def _boom(work_id):
        raise RuntimeError("DB 读失败（模拟）")
    monkeypatch.setattr(function_store, "list_profiles", _boom)

    cards = card_tools._collect_card_meta(wid)
    names = {t["name"] for t in card_tools.build_tool_schemas(
        card_id=root, work_id=wid, cards=cards)}
    assert "create_card" not in names, \
        "必须轮不读职能：list_profiles 抛异常不得影响判定"