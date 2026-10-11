# -*- coding: utf-8 -*-
"""[THINK-ECO-permgmt T8] 权限收口验收（任务书 V4/V5/V6/V8 ＋ 定夺 1/2 机械锁）。

- V4：tool 分类卡 build_tool_schemas 不含文件四件（存量 tool 卡＝0，按交办单
  §五.1 构造 meta 验证，不用生产数据造卡）；正控 sub_project / 根卡仍见全量。
- V5：无文件权限分类 check_file_access 返回 False；check() 拒绝抛 PermissionError。
- V6：未注册分类写影子 check(SHADOW_WRITE) 拒绝（定夺 1 五键全授 SHADOW_WRITE，
  已注册分类不拒 → 拒绝证据按交办单 §五.2 改用未注册分类触发）。
- V8：三处打点（check/check_with_scope、build_tool_schemas、check_file_access）
  在 prompt_log（prompt_perm_audit.jsonl）留有判定记录——沙箱 LOGS_DIR 读回实证。
- 定夺 1 锁：POLICY 五键位集合逐键比对；定夺 2 锁：_type_key 取键优先级。
- 连线/解线收紧实证（交办单「待用户知悉」项）：TREE_SCOPE 目标只能连/解自己
  子树内的卡；悬空线保留直接可解。
- R3 等价回归锚：已删子树判据的定格副本 vs policy._is_in_subtree（逐例相等）。
"""
from __future__ import annotations

import json
import random

import pytest

from src.api import card_tools
from src.permissions import policy
from src.storage import card_store, shadow_store


# ---------------------------------------------------------------- 定夺锁

_DINGDUO1: dict[str, set[str]] = {
    "rd_project": {"READ", "EDIT", "CREATE_CHILD", "LINK", "UNLINK", "TREE_SCOPE",
                   "SEARCH", "TOOLS_FILE", "TOOLS_WEB", "TOOLS_CARD",
                   "CARD_PROPOSAL", "SHADOW_WRITE"},
    "research_project": {"READ", "EDIT", "CREATE_CHILD", "LINK", "UNLINK", "TREE_SCOPE",
                         "SEARCH", "TOOLS_FILE", "TOOLS_WEB", "TOOLS_CARD",
                         "CARD_PROPOSAL", "SHADOW_WRITE"},
    "sub_project": {"READ", "EDIT", "CREATE_CHILD", "DELETE", "LINK", "UNLINK",
                    "TREE_SCOPE", "SEARCH", "TOOLS_FILE", "TOOLS_WEB", "TOOLS_CARD",
                    "SHADOW_WRITE"},
    "topic": {"READ", "EDIT", "CREATE_CHILD", "DELETE", "LINK", "UNLINK",
              "TREE_SCOPE", "SEARCH", "TOOLS_FILE", "TOOLS_WEB", "TOOLS_CARD",
              "SHADOW_WRITE"},
    "tool": {"READ", "EDIT", "DELETE", "SEARCH", "TOOLS_CARD", "SHADOW_WRITE"},
}


def test_dingduo1_policy_values_locked():
    """定夺 1：五键值＝原方案 §2.2 逐字＋各键 SHADOW_WRITE 偏离位（机械锁）。"""
    assert set(policy.POLICY) == set(_DINGDUO1)
    for key, expected in _DINGDUO1.items():
        got = {name for name, bit in policy.Perm._member_map_.items()
               if bit and policy.POLICY[key] & bit}
        assert got == expected, f"POLICY[{key}] = {got} ≠ 定夺 1 {expected}"


def test_dingduo2_type_key_precedence():
    """定夺 2：查表键先 project_class（仅根卡）后 card_type；缺省空串。"""
    assert policy._type_key({"card_type": "project", "project_class": "research_project"}) == "research_project"
    assert policy._type_key({"card_type": "sub_project"}) == "sub_project"
    assert policy._type_key({"card_type": "tool"}) == "tool"
    assert policy._type_key({}) == ""
    assert policy._type_key(None) == ""
    # 根卡按 project_class 落行：rd_project 授权含 READ（无定夺 2 则查 "project" 落默认关门）
    assert policy.grants_for("rd_project") & policy.Perm.READ


# ---------------------------------------------------------------- V4

def test_v4_tool_class_card_has_no_file_tools():
    """V4：tool 分类卡构造 meta → 工具面不含 write_file/read_file（交办单 §五.1）。"""
    cards = {"card_tool_x": {"card_type": "tool", "title": "构造工具卡"}}
    schemas = card_tools.build_tool_schemas("card_tool_x", cards=cards)
    names = {s["name"] for s in schemas}
    assert not names & {"write_file", "read_file", "list_files", "search_files"}
    assert not names & {"web_search", "fetch_web"}  # tool 亦无 TOOLS_WEB
    # [工具卡底座 T7 · 2026-10-09] 工具集新增第 19 件 run_tool_card（调用侧，保留件，
    # 不属文件/联网组 ⇒ 分类过滤不收它）。故 tool 分类可见由 12 件变 13 件。
    # （T9「主卡写工具收回」已按用户 2026-10-09 17:38 令回滚，不影响本断言：
    #   分类过滤按 POLICY 表走，与本处计数无关。）
    # [迭代二 S2] 注册表 +7（思维链七工具）——随注册表全量派生，与历次增补
    # 同法；接替未启用前 tool 分类＝全量穿过（EC-131 现状），七件同样可见。
    # [迭代四 K2] 注册表 +1（send_card_message，卡间发信写工具）——同法，
    # 全量派生自动可见。
    # [12:34 定性] send_card_message 已摘出模型工具面（基础设施，非通用工具）→ 不再出现在任何工具面
    assert names == {"read_card", "read_memory", "create_card", "edit_card",
                     "delete_card", "update_shadow", "read_shadow",
                     "list_manuscript", "read_manuscript", "search_manuscript",
                     "link_cards", "unlink_cards", "run_tool_card",
                     "create_plan", "plan_reorder", "plan_insert",
                     "plan_remove", "plan_modify", "step_complete",
                     "escalate_to_plan"}


def test_v4_positive_control_registered_classes_see_all():
    """V4 正控：注册分类（含按 project_class 取键的根卡）见全量 26 件。

    [工具卡底座 T7 · 2026-10-09] 注册表由 18 件增至 19 件（新增 run_tool_card）。
    [迭代二 S2] 注册表 +7（思维链七工具），19 → 26。
    [迭代四 K2] 注册表 +1（send_card_message，卡间发信），26 → 27。
    [12:34 定性] send_card_message 摘出模型工具面（基础设施接口，非通用工具），27 → 26。
    """
    n_all = len(card_tools.build_tool_schemas())
    assert n_all == 26
    for meta in ({"card_type": "sub_project"},
                 {"card_type": "project", "project_class": "rd_project"},
                 {"card_type": "topic"}):
        schemas = card_tools.build_tool_schemas("card_x", cards={"card_x": meta})
        assert len(schemas) == 26, f"{meta} 应见 26 件，实 {len(schemas)}"


# ---------------------------------------------------------------- V5

def test_v5_no_file_permission_class_denied():
    """V5：无文件权限分类 → check_file_access False；check() 抛 PermissionError。"""
    assert file_tools_check("docs/notes.md", "tool") is False
    assert file_tools_check("docs/notes.md", "线索") is False
    cards = {"card_u": {"card_type": "线索"}}
    with pytest.raises(PermissionError):
        policy.check("card_u", policy.Perm.READ, cards)
    # 未注册分类经文件工具通道同样拒绝（真实 handler 包装层）
    # 凭证排除与全盘边界照常生效（注册分类）
    assert file_tools_check(".env", "sub_project") is False


def file_tools_check(path: str, type_key: str) -> bool:
    from src.api import file_tools
    return file_tools.check_file_access(path, type_key)


# ---------------------------------------------------------------- V6

async def test_v6_shadow_write_unregistered_class_denied():
    """V6：未注册分类写影子 → 关口 SHADOW_WRITE 拒（真实 execute_tool 通道）。"""
    cards = {"card_m": {"card_type": "神秘分类"}}
    r = await card_tools.execute_tool(
        "work_999", "card_m", "update_shadow",
        {"content": "## 进度\n- x\n\n## 产物清单\n- 无\n\n## 迭代历史\n- v1\n"},
        cards)
    assert r["ok"] is False
    assert "越权" in r["error"]["message"]


async def test_v6_positive_registered_class_shadow_ok(typed_registry, work):
    """V6 正控：注册分类（sub_project）写自己影子照常（定夺 1 偏离位的本意）。"""
    wid, root = work["work_id"], work["work_card_id"]
    r = await card_store.create_card(wid, "甲", "模型生成", parent=root)
    a = r["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)
    body = "## 进度\n- 定夺1影子\n\n## 产物清单\n- 无\n\n## 迭代历史\n- v1\n"
    r = await card_tools.execute_tool(wid, a, "update_shadow", {"content": body}, cards)
    assert r.get("ok") is True
    raw = shadow_store.shadow_path(wid, a).read_text(encoding="utf-8")
    assert "定夺1影子" in raw


# ---------------------------------------------------------------- 连线/解线收紧

async def test_link_unlink_subtree_tightening(typed_registry, work):
    """交办单「待用户知悉」实证：TREE_SCOPE 目标只能连/解自己子树内的卡；
    悬空线（目标已删）保留直接可解；tool 目标不可连。"""
    wid, root = work["work_id"], work["work_card_id"]
    async def mk(title, parent):
        r = await card_store.create_card(wid, title, "模型生成", parent=parent)
        assert "error" not in r, r
        return r["meta"]["card_id"]
    a = await mk("甲", root)
    b = await mk("乙", root)      # a 的旁支
    c = await mk("丙", a)         # a 的子
    d = await mk("丁", a)         # a 的子（稍后删成悬空）

    def cards():
        return card_tools._collect_card_meta(wid)

    # 旁支不可连（TREE_SCOPE 目标收紧为子树内）
    r = await card_tools.execute_tool(
        wid, a, "link_cards", {"target_id": b, "type": "关联"}, cards())
    assert r["ok"] is False and "越权" in r["error"]["message"]
    # 自己子树内可连
    r = await card_tools.execute_tool(
        wid, a, "link_cards", {"target_id": c, "type": "关联"}, cards())
    assert r["ok"] is True and r["error"] is None
    # 根（血统上游）可连子树内任意卡
    r = await card_tools.execute_tool(
        wid, root, "link_cards", {"target_id": b, "type": "关联"}, cards())
    assert r["ok"] is True
    # tool 目标不可连（构造工具卡 meta 直接走关口层）
    with pytest.raises(PermissionError):
        policy.check_with_scope(a, "card_tool_y", policy.Perm.LINK,
                                {"card_tool_y": {"card_type": "tool"}})

    # 解线：子树内可解
    r = await card_tools.execute_tool(
        wid, a, "unlink_cards", {"target_id": c, "type": "关联"}, cards())
    assert r["ok"] is True and r["data"]["removed"] == 1
    # 旁支不可解（目标在盘且含 TREE_SCOPE）
    r = await card_tools.execute_tool(
        wid, a, "unlink_cards", {"target_id": b, "type": "关联"}, cards())
    assert r["ok"] is False and "越权" in r["error"]["message"]
    # 悬空线：目标已删 → 不在关口判定面 → 直接可解（既有清理语义保留）。
    # 悬空线只由「删目标」产生（link_cards 对已删目标本就走 not_found）：
    # a 先连自己的子 d，再删 d，a 卡上遗留的悬空线直接解掉。
    r = await card_tools.execute_tool(
        wid, a, "link_cards", {"target_id": d, "type": "关联"}, cards())
    assert r["ok"] is True
    r = await card_tools.execute_tool(wid, a, "delete_card", {"card_id": d}, cards())
    assert r["ok"] is True
    assert card_tools._collect_card_meta(wid).get(d) is None   # d 已不在卡面
    r = await card_tools.execute_tool(
        wid, a, "unlink_cards", {"target_id": d, "type": "关联"}, cards())
    assert r["ok"] is True and r["data"]["removed"] == 1


# ---------------------------------------------------------------- V8

async def test_v8_prompt_log_records_all_three(typed_registry, work):
    """V8：三处打点在 prompt_log 留判定记录（沙箱 LOGS_DIR 读回实证）。"""
    from src.api import prompt_log

    # ① check / check_with_scope（放行＋拒绝各一）
    cards = {"card_v8": {"card_type": "sub_project"}}
    policy.check_with_scope("card_v8", "card_v8", policy.Perm.EDIT, cards)
    cards_deny = {"card_v8d": {"card_type": "神秘分类"}}
    with pytest.raises(PermissionError):
        policy.check("card_v8d", policy.Perm.READ, cards_deny)
    # ② build_tool_schemas
    card_tools.build_tool_schemas("card_v8", cards=cards)
    # ③ check_file_access
    from src.api import file_tools
    file_tools.check_file_access("whatever.txt", "sub_project")

    log_path = prompt_log.LOGS_DIR / prompt_log.PERM_LOG_FILENAME
    assert log_path.exists(), "打点文件应已落盘"
    recs = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    fns = {r["fn"] for r in recs}
    assert {"check", "check_with_scope", "build_tool_schemas",
            "check_file_access"} <= fns, f"缺打点: {fns}"
    print("\n[V8] prompt_perm_audit.jsonl 判定记录（末 6 条）：")
    for r in recs[-6:]:
        print("    ", json.dumps(r, ensure_ascii=False))


# ---------------------------------------------------------------- R3 冻结锚

def _legacy_subtree_verdict(current_id, target_id, cards):
    """已删子树判据的定格副本（收口前 card_tools 写侧判据，原 L877-902 逐字）。

    仅存于测试：R3 等价回归锚（等价验证 27,605 对 0 破例，报告
    docs/exploration/THINK-ECO-permgmt-R3-等价验证-20261009_0010.md）。
    """
    if target_id == current_id:
        return True
    tgt = cards.get(target_id)
    if tgt is None:
        return False
    visited: set[str] = set()
    node = tgt.get("parent", "")
    guard = 0
    while node and guard < 1000:
        guard += 1
        if node in visited:
            break
        visited.add(node)
        if node == current_id:
            return True
        nm = cards.get(node)
        if nm is None:
            break
        node = nm.get("parent", "")
    return False


def test_r3_frozen_anchor_still_equivalent():
    """R3 回归锚：定格副本与 policy._is_in_subtree 逐例相等（边界＋随机林）。"""
    forests = [
        {"card_001": {"parent": ""}},
        {"card_001": {"parent": ""}, "card_002": {"parent": "card_001"},
         "card_003": {"parent": "card_001"}, "card_004": {"parent": "card_002"}},
        {"card_001": {"parent": "card_002"}, "card_002": {"parent": "card_001"},
         "card_003": {"parent": ""}},
        {"card_001": {"parent": "card_999"}, "card_002": {"parent": "card_001"}},
        {"card_001": {"parent": "card_001"}},
    ]
    compared = 0
    for cards in forests:
        ids = list(cards) + ["card_999"]
        for actor in ids:
            for target in ids:
                assert _legacy_subtree_verdict(actor, target, cards) == \
                    policy._is_in_subtree(actor, target, cards)
                compared += 1
    rng = random.Random(20261009)
    for _ in range(100):
        n = rng.randint(1, 10)
        ids = [f"card_{i:03d}" for i in range(1, n + 1)]
        cards = {cid: {"parent": rng.choice([""] + ids[: i + 1] + ["card_999"])}
                 for i, cid in enumerate(ids)}
        for actor in ids + ["card_999"]:
            for target in ids + ["card_999"]:
                assert _legacy_subtree_verdict(actor, target, cards) == \
                    policy._is_in_subtree(actor, target, cards)
                compared += 1
    assert compared >= 5000
