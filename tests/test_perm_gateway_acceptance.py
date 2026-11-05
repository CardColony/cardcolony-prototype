# -*- coding: utf-8 -*-
"""权限底座重做（2026-10-12）验收：统一关口＋工具面过滤＋文件锁＋打点。

锁的内容（用户裁定 1-7 的机械证据）：

- **配置值锁**：config/permissions.json 的两级表值逐项比对（裁定 1/3）。
- **两级定位**：根卡查 permissions[project_class].project；子卡查
  permissions[project_class][card_type]；没配到的组合＝空（裁定 5）。
- **类型不可变**的证据在 tests/test_cardperm_readopen.py（存储层）。
- V4：tool 类型卡 build_tool_schemas 不含文件四件/联网两件；正控（有卡上下文的
  注册类型）见全量 25 件；**无卡上下文 ⇒ 空列表**（裁定 5，修掉原先的全放行）。
- V5：没有 tools_file 开关的卡 → check_file_access False。
- V6：未配置类型写影子 → 拒；注册类型（sub_project）照写。
- 连线/解线收紧实证：树类开关叠树作用域，只能连/解自己子树内的卡；悬空线保留
  直接可解。
- 版本刷新：scripts/refresh_perms.py 幂等（第二次跑零改动）。
- V8：四处打点在 prompt_log（prompt_perm_audit.jsonl）留判定记录。
- R3 等价回归锚：已删子树判据的定格副本 vs policy._is_in_subtree（逐例相等）。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from src.api import card_tools
from src.permissions import policy
from src.storage import card_store, shadow_store

_REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 配置值锁

# 方案《权限底座重做-方案-20261012.md》§二.2（v2 · 2026-10-12 工具面补齐后）：
#   删死开关 search（无对应工具）、新增 manuscript / invoke_tool / plan。
#   [卡片底座第一步 2026-10-14 · v3] 新增 read_linked（沿通信线读）与
#   send_card_message（沿关系发消息）——给**所有已授 read 的类型**一并配两码。
_LINKED = {"read_linked", "send_card_message"}

#: [执行权集中在执行卡 2026-10-21] 合成「权限全开」夹具。
#: 这里要验的机制是「有没有开关 ⇒ 工具看不看得见」；以前拿 sub_project
#: 当全开样本，现在管理卡不再有执行权，就不能再拿它当夹具。
#: 用 SWITCHES 全集（不属于任何真实卡型），测试意图不变。
_FULL_SWITCHES: set[str] = set(policy.SWITCHES)
#: [v14 · 2026-10-24 · 用户裁定「查资料≠动手」] **所有卡型都有 tools_web**
#: （web_search/fetch_web＝查外部资料）；执行面（tools_file/bash/python/write_file/
#: invoke_tool）一概不动。⇒ 每行都比 v13 多了 tools_web。
_EXPECTED: dict[tuple[str, str], set[str]] = {
    ("rd_project", "project"): {
        "read", "edit", "create_child", "link", "unlink",
        "manuscript", "invoke_tool", "plan",
        "tools_read_file", "tools_web", "shadow_write"} | _LINKED,
    ("rd_project", "sub_project"): {
        "read", "edit", "delete", "create_child", "link", "unlink",
        "manuscript", "invoke_tool", "plan",
        "tools_read_file", "tools_web", "shadow_write"} | _LINKED,
    ("rd_project", "tool"): {
        "read", "edit", "delete", "plan", "shadow_write", "tools_web"} | _LINKED,
    ("research_project", "project"): {
        "read", "edit", "create_child", "link", "unlink",
        "manuscript", "invoke_tool", "plan",
        "tools_read_file", "tools_web", "shadow_write"} | _LINKED,
    ("research_project", "topic"): {
        "read", "edit", "delete", "create_child", "link", "unlink",
        "manuscript", "invoke_tool", "plan",
        "tools_read_file", "tools_web", "shadow_write"} | _LINKED,
}


def test_config_values_locked():
    """配置值逐项比对（方案 §二.2）；根卡无 delete、tool 无 create_child/文件，
    **但所有卡型都能查资料（tools_web）**——〔v14 · 2026-10-24 用户裁定：
    「只是不能动手，为啥不能查资料」〕。"""
    for (pc, ct), expected in _EXPECTED.items():
        got = set(policy.perms_for(pc, ct))
        assert got == expected, f"permissions[{pc}][{ct}] = {got} ≠ {expected}"
    # 未配置的两级组合 ⇒ 空（默认关门）
    assert policy.perms_for("rd_project", "topic") == []
    assert policy.perms_for("research_project", "sub_project") == []
    assert policy.perms_for("no_such_class", "project") == []
    assert policy.perms_for("rd_project", "no_such_type") == []


def test_every_card_type_can_look_up_materials_but_still_cannot_act():
    """〔v14 · 2026-10-24 · 用户裁定「查资料≠动手」】

    **所有**卡型都有 ``tools_web``（查外部资料：web_search / fetch_web）；
    **动手面**（``tools_file`` 带的写文件 / bash / python / write_file、
    ``invoke_tool`` 调执行体）仍按各行原口径——「不让他动手」与
    「让他查资料」是**两条独立的闸**，不该绑在一起。

    这条网钉的是**裁定本身**：配置里每行都得有 tools_web；
    且「能查资料」不得连带把「能动手」也打开。
    """
    cfg = json.loads((_REPO / "config" / "permissions.json").read_text(encoding="utf-8"))
    for pc, types in cfg["permissions"].items():
        for ct, perms in types.items():
            assert "tools_web" in perms, f"{pc}/{ct} 应有 tools_web（查资料）"
            got = set(policy.perms_for(pc, ct))
            assert got == set(perms), f"{pc}/{ct} 权限与配置不一致：{got} ≠ {set(perms)}"
    # 动手面没有被顺带放开：bash / python / write_file 都挂 tools_file
    for pc, ct in (("rd_project", "project"), ("rd_project", "sub_project"),
                   ("research_project", "project"), ("research_project", "topic"),
                   ("rd_project", "tool"), ("rd_project", "role"),
                   ("research_project", "role")):
        perms = set(policy.perms_for(pc, ct))
        assert "tools_file" not in perms, (
            f"{pc}/{ct} 不应有 tools_file（「能查资料」不等于「能动手」）")


def test_two_level_lookup_and_switch_vocabulary():
    """两级定位 + 开关词汇表：配置值必须是 SWITCHES 子集；版本号来自配置。"""
    # 根卡与子卡各查各的行
    assert "delete" not in policy.perms_for("rd_project", "project")      # 根卡删不掉
    assert "delete" in policy.perms_for("rd_project", "sub_project")
    for switch in policy.perms_for("rd_project", "tool"):
        assert switch in policy.SWITCHES
    assert "create_child" not in policy.perms_for("rd_project", "tool")   # tool 不长树
    assert "tools_file" not in policy.perms_for("rd_project", "tool")    # tool 不碰文件
    # 〔v14 · 2026-10-24〕但 tool **能查资料**（tools_web 全卡型都有）——
    # 「不让他动手」不等于「不让他查资料」。
    assert "tools_web" in policy.perms_for("rd_project", "tool")
    # 树类开关＝卡片操作五项；read 不在其中 ⇒ 读不受子树限定
    assert policy.TREE_SWITCHES == {"edit", "delete", "link", "unlink", "create_child"}
    assert policy.config_version() >= 1


def _full_meta() -> dict:
    """权限全开的合成卡面（夹具用，非真实卡型）——见 _FULL_SWITCHES 说明。"""
    return {
        "card_type": "sub_project",
        "perms": sorted(_FULL_SWITCHES),
        "perms_version": policy.config_version(),
    }


def _meta(card_type: str, project_class: str = "rd_project") -> dict:
    """按配置造一张卡的卡面（perms 从配置算，不手写——手写＝两套事实）。"""
    return {
        "card_type": card_type,
        "perms": policy.perms_for(project_class, card_type),
        "perms_version": policy.config_version(),
    }


# ---------------------------------------------------------------- V4

def test_v4_tool_class_card_has_no_file_tools():
    """V4：tool 类型卡 → 工具面只剩它有权的那几件。

    [工具面补齐 2026-10-12] 原断言把 tool 的工具面钉成 19 件（含
    create_card / link_cards / manuscript 三件 / run_tool_card），那是
    **工具面只按 file/web 两组过滤**时的旧行为——卡片开关只在执行层拦，
    模型看得见调不动。现已同源：tool 无 create_child/link/unlink/manuscript/
    invoke_tool/tools_file ⇒ 那些工具压根不下发。

    〔v14 · 2026-10-24 · 用户裁定「查资料≠动手」〕**联网两件现在 tool 也看得见**
    （所有卡型都有 tools_web）——「不让他动手」不该顺带「不让他查资料」；
    动手面（文件四件/bash/python/write_file）仍然不给。
    """
    cards = {"card_tool_x": _meta("tool")}
    schemas = card_tools.build_tool_schemas("card_tool_x", cards=cards)
    names = {s["name"] for s in schemas}
    assert not names & {"write_file", "read_file", "list_files", "search_files"}
    # 〔v14〕查资料：tool 也看得见（v13 之前是「不联网」）
    assert names >= {"web_search", "fetch_web"}, (
        f"tool 应能查资料（v14：所有卡型都有 tools_web），实见 {sorted(names)}")
    # tool 有：read / edit / delete / shadow_write / plan / read_linked
    # [卡片底座第一步 2026-10-14] tool 类型 v3 起配了 read_linked ⇒ read_linked 可见
    # [v12 2026-10-22] tool 类型本来就配了 send_card_message（v3 起），只是以前
    # 该工具被 AUTO_ONLY_TOOLS 挡在自动卡之外——移出后按开关正常可见。
    # [wake_card] 上游唤醒（2026-10-22 新增，映射 SWITCH_EDIT）：tool 有 edit ⇒ 可见。
    # [wake_card] 不在 tool 上：唤醒是**组织动作**，挂 SWITCH_CREATE_CHILD。
    # tool 没有 create_child（它是被调用的执行体，不组织别人）⇒ 看不见 wake_card。
    # 依据 D04「执行权与组织权分离」：组织权只在 project/sub_project/role 手上。
    expect = {"read_card", "read_memory", "edit_card",
              "delete_card", "edit_shadow", "read_shadow", "read_linked",
              "send_card_message", "web_search", "fetch_web",
              # [归档 2026-10-25] archive/unarchive 挂 delete（tool 有 delete）
              # [2026-10-26] read_archive / stage_open 挂 read（tool 有 read）
              "archive_card", "unarchive_card", "read_archive", "stage_open"}
    # [口径解耦 2026-10-17] 计划六件随 thinkchain 开关（纯单模型模式下不注册）
    if __import__("src.thinkchain", fromlist=["enabled"]).enabled():
        expect |= {"create_plan", "plan_reorder", "plan_insert",
                   "plan_remove", "plan_modify", "step_complete"}
    assert names == expect


def test_v4_positive_control_registered_types_see_all():
    """V4 正控：注册类型见全量——**根卡除外，它没有 delete 开关**。

    [工具面补齐 2026-10-12] 原断言三组合都要求 25 件。根卡（project）配置里
    没有 delete（删不掉），工具面同源后 ``delete_card`` 不再下发 ⇒ 少 1 件是
    **正确行为**，不是漏发。

    [B-11 2026-10-12] 件数不再写死为魔法数字，改从注册表现取：
    联网两件（web_search/fetch_web）已插件化，**测试环境不读 .env**（conftest
    铁律二禁 import main，.env 因此未加载），故无通道配置 ⇒ 两件被剪枝 ⇒
    本环境是 23 件、生产环境（配了通道）是 25 件。写死数字会在任一环境里
    变成假红或假绿——这正是「魔法数字＝漂移源」的又一次复现，故改为派生。
    断言的**意图**（注册类型见全量、根卡少 delete）一字未改。

    [worker 卡 C3 2026-10-15] **全量口径随类型闸改派生**：``report_done`` 挂进
    ``WORKER_ONLY_TOOLS`` 后只对 worker 下发——注册类型（非 worker）的「全量」
    ＝ ``TOOL_SWITCH − WORKER_ONLY_TOOLS``（判据6 字面口径），根卡再减 delete。
    意图不变：注册类型见它该见的全量、根卡少 delete。

    [规则卡 2026-10-15 · 块3] **全量口径再随根卡闸改派生**：``draft_rule_edit``
    挂进 ``ROOT_ONLY_TOOLS`` 后只对剧本根卡（card_id==work_card_id_of(work_id)）
    下发；本测试用合成卡面、不带 work_id ⇒ 第四段 fail-closed 收走 root-only
    工具（同上理由：件数不写死，按注册表现取）。意图不变。

    [更新现状按钮改造 2026-10-16] **全量口径再随 op 专用工具改派生**：
    ``propose_status`` 列 ``OP_ONLY_TOOLS``（**非卡片内挂**，只在 op 回合追加）
    ⇒ 无 op 的常规工具面里它不出现，基线再减 ``OP_ONLY_TOOLS``。意图不变。
    """
    coder_only = set(card_tools.WORKER_ONLY_TOOLS)
    root_only = set(card_tools.ROOT_ONLY_TOOLS)
    op_only = set(card_tools.OP_ONLY_TOOLS)
    # [v1.1 · 判据6 字面口径] 基线集合＝TOOL_SWITCH − CODER_ONLY − ROOT_ONLY − OP_ONLY
    # [自动模式 2026-10-17 · 回灌 5700 块0.4] 再减 AUTO_ONLY_TOOLS（update_goal——
    # 非卡片内挂，只在 auto_mode=1 的卡上追加，不进常规面）。
    auto_only = set(card_tools.AUTO_ONLY_TOOLS)
    # [角色机制 5710] ROLE_ONLY_TOOLS 同属「非卡片内挂」，只在角色卡末段追加，
    # 不进常规工具面 ⇒ 期望件数也要减掉。
    role_only = set(card_tools.ROLE_ONLY_TOOLS)
    # [口径解耦 2026-10-17] 只算**注册表在册**的：纯单模型模式下计划六件不注册，
    # TOOL_SWITCH 里若仍有其开关映射（历史遗留），不能算进期望件数。
    expected_all = len((set(card_tools.TOOL_SWITCH) - coder_only - root_only
                        - op_only - auto_only - role_only) & set(card_tools.TOOL_REGISTRY))
    # [强制停止 2026-10-24] stop_card 在基础池被排除(同职能件),但按类型闸
    # **追加**给 project/sub_project/topic(治理权,通用不挂目录)——合成卡
    # 是 sub_project ⇒ 期望 +1。
    _synth_type = str(_full_meta().get("card_type") or "")
    if _synth_type in ("project", "sub_project", "topic"):
        # stop_card+silence_card(治理权)无条件按类型闸追加;
        # report_up(汇报权)仅目录启用时追加(目录关 = 旧世界)。
        # stop_card+silence_card+continue_task(治理与延续,无条件按类型闸追加)
        expected_all += 3
    n_all = len(card_tools.build_tool_schemas(
        "card_x", cards={"card_x": _full_meta()}))
    assert n_all == expected_all, (
        f"工作单元型卡应见 {expected_all} 件"
        f"（TOOL_SWITCH {len(card_tools.TOOL_SWITCH)} 件减 "
        f"WORKER_ONLY_TOOLS {sorted(coder_only)} 减 "
        f"ROOT_ONLY_TOOLS {sorted(root_only)} 减 "
        f"OP_ONLY_TOOLS {sorted(op_only)} 减 "
        f"AUTO_ONLY_TOOLS {sorted(auto_only)}），实 {n_all} 件"
    )
    # [执行权集中 2026-10-21] 管理卡（project / sub_project / topic）
    # 不再看到写与执行类工具，但**仍看得到只读类**——「看得见、不动手」。
    # 直接断言这条契约，而不是数工具总数（数总数会把工具增删也当成契约变化）。
    # 〔v14 · 2026-10-24〕web_search/fetch_web **从执行类里挪出**：它们是
    # 「查资料」（只读检索），所有卡型都有 tools_web ⇒ 管理卡看得见；
    # 动手面（文件写/改/删、bash、python）仍然不给。
    SELF_EXEC_TOOLS = {"write_file", "edit_file", "move_file", "rename_file",
                       "delete_file", "bash", "python"}
    READONLY_TOOLS = {"read_file", "list_files", "search_files", "glob_files"}
    for pc, ct in (("rd_project", "sub_project"),
                   ("research_project", "topic"),
                   ("rd_project", "project")):
        names = {s["name"] for s in card_tools.build_tool_schemas(
            "card_x", cards={"card_x": _meta(ct, pc)})}
        bad = names & SELF_EXEC_TOOLS
        assert not bad, f"{pc}/{ct} 不应看到执行类工具，却看到 {sorted(bad)}"
        assert READONLY_TOOLS <= names, (
            f"{pc}/{ct} 应能看到只读文件工具，缺 {sorted(READONLY_TOOLS - names)}")
        # 〔v14〕查资料：管理卡也看得见（v13 之前 root/sub_project/topic 没有 tools_web）
        assert {"web_search", "fetch_web"} <= names, (
            f"{pc}/{ct} 应能查资料（v14：所有卡型都有 tools_web），缺 "
            f"{sorted({'web_search', 'fetch_web'} - names)}")
        assert "create_card" in names and "set_child_mission" in names, (
            f"{pc}/{ct} 必须能建卡并立使命")
        if ct == "project":
            assert "delete_card" not in names



def test_v4_default_closed_when_no_card_context():
    """V4 关门分支：认不出卡 ⇒ 空工具面（不再全放行）。"""
    assert card_tools.build_tool_schemas() == []
    assert card_tools.build_tool_schemas("card_x", cards={}) == []
    assert card_tools.build_tool_schemas(
        "", cards={"card_x": _meta("sub_project")}) == []


# ---------------------------------------------------------------- V5

def test_v5_no_file_permission_type_denied():
    """V5：没有 tools_file 开关的卡 → check_file_access False；关口抛 PermissionError。"""
    from src.api import file_tools

    assert file_tools.check_file_access("docs/notes.md", _meta("tool")) is False
    assert file_tools.check_file_access("docs/notes.md", None) is False      # 认不出卡
    assert file_tools.check_file_access("docs/notes.md", {"card_type": "tool"}) is False
    # 未配置类型（空 perms）经文件工具通道同样拒绝
    assert file_tools.check_file_access("docs/notes.md", _meta("no_such_type")) is False
    # [执行权集中 2026-10-21] 执行权在 worker 手上：它能读写文件。
    # [只读档 2026-10-22 · 用户令「把查找文件、读文件也屏蔽了，这显然不行」]
    # 管理卡（sub_project）没有 tools_file ⇒ **写**仍然拒；但它有 tools_read_file
    # ⇒ **读**必须放行。原来这条断言把「读」也一并锁死，等于让 10-21 配的只读开关
    # 形同虚设（工具面看得见 read_file、一调就被执行层拒）。
    assert file_tools.check_file_access("trading-workspace/notes.md",
                                        _meta("worker")) is True
    assert file_tools.check_file_access("trading-workspace/notes.md",
                                        _meta("sub_project")) is True
    assert file_tools.check_file_access("trading-workspace/notes.md",
                                        _meta("sub_project"), write=True) is False
    # 凭证排除照常生效（与开关无关的第二层）
    assert file_tools.check_file_access(".env", _meta("sub_project")) is False
    # 关口层：未配置类型连 read 都拒
    cards = {"card_u": {"card_type": "no_such_type"}}
    with pytest.raises(PermissionError):
        policy.check("card_u", "card_u", policy.SWITCH_READ, cards)


# ---------------------------------------------------------------- V6

async def test_v6_shadow_write_unconfigured_type_denied():
    """V6：未配置类型写影子 → 关口 shadow_write 拒（真实 execute_tool 通道）。"""
    cards = {"card_m": {"card_type": "神秘分类"}}
    r = await card_tools._h_update_shadow("work_999", "card_m", {"content": "## 进度\n- x\n\n## 产物清单\n- 无\n\n## 迭代历史\n- v1\n"}, cards)
    assert r["ok"] is False
    assert "越权" in r["error"]["message"]


async def test_v6_positive_registered_type_shadow_ok(typed_registry, work):
    """V6 正控：注册类型（sub_project）写自己影子照常。"""
    wid, root = work["work_id"], work["work_card_id"]
    r = await card_store.create_card(wid, "甲", "模型生成", parent=root)
    a = r["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)
    body = "## 进度\n- 定夺1影子\n\n## 产物清单\n- 无\n\n## 迭代历史\n- v1\n"
    r = await card_tools._h_update_shadow(wid, a, {"content": body}, cards)
    assert r.get("ok") is True
    raw = await shadow_store.read_shadow_raw(wid, a)
    assert raw is not None and "定夺1影子" in raw


# ---------------------------------------------------------------- 连线/解线收紧

async def test_link_unlink_no_tree_scope_with_type_gate(typed_registry, work):
    """建线/解线**不叠 parent 树作用域**，但类型分权照旧把关。

    [5713 通道试走 2026-10-20·用户口径「卡间通信有专门的通信线」] 本用例原名
    ``test_link_unlink_subtree_tightening``，断言「旁支不可连/不可解」——那属于
    **旧的树作用域**口径，已被本次改动**有意推翻**：通信的边界是线本身，不是父子
    关系；平级卡若建不出线，就永远通不了话（先有鸡先有蛋）。

    仍保留的硬约束（一条不少）：
      · 类型分权——目标卡没授 link/unlink 就拒（用 tool 类型卡构造）；
      · 自身 → 拒（不能连自己）；
      · 悬空线（目标已删）→ 不查卡、直接可解（既有清理语义）。
    """
    wid, root = work["work_id"], work["work_card_id"]

    async def mk(title, parent):
        r = await card_store.create_card(wid, title, "模型生成", parent=parent)
        assert "error" not in r, r
        return r["meta"]["card_id"]
    a = await mk("甲", root)
    b = await mk("乙", root)      # a 的旁支（旧口径：不可连；新口径：可连）
    c = await mk("丙", a)         # a 的子
    d = await mk("丁", a)         # a 的子（稍后删成悬空）

    def cards():
        return card_tools._collect_card_meta(wid)

    # 旁支可连（[5713] 有意放开：建线不受树作用域约束）
    r = await card_tools.execute_tool(
        wid, a, "link_cards", {"target_id": b, "type": "关联"}, cards())
    assert r["ok"] is True and r["error"] is None, r
    # 自己子树内可连（历史行为，不变）
    r = await card_tools.execute_tool(
        wid, a, "link_cards", {"target_id": c, "type": "关联"}, cards())
    assert r["ok"] is True and r["error"] is None
    # 根（血统上游）可连子树内任意卡
    r = await card_tools.execute_tool(
        wid, root, "link_cards", {"target_id": b, "type": "关联"}, cards())
    assert r["ok"] is True
    # 类型分权仍把关：tool 类型卡没授 link ⇒ 拒（作用域放开不等于授权放开）
    with pytest.raises(PermissionError):
        policy.check(a, "card_tool_y", policy.SWITCH_LINK,
                     {"card_tool_y": _meta("tool")})
    # 自身 → 拒
    r = await card_tools.execute_tool(
        wid, a, "link_cards", {"target_id": a, "type": "关联"}, cards())
    assert r["ok"] is False and r["error"]["code"] == "validation"

    # 解线：子树内可解
    r = await card_tools.execute_tool(
        wid, a, "unlink_cards", {"target_id": c, "type": "关联"}, cards())
    assert r["ok"] is True and r["data"]["removed"] == 1
    # 旁支可解（与建线对称：自己建得起，就解得掉）
    r = await card_tools.execute_tool(
        wid, a, "unlink_cards", {"target_id": b, "type": "关联"}, cards())
    assert r["ok"] is True and r["data"]["removed"] == 1
    # 悬空线：目标已删 → 不在关口判定面 → 直接可解（既有清理语义保留）
    r = await card_tools.execute_tool(
        wid, a, "link_cards", {"target_id": d, "type": "关联"}, cards())
    assert r["ok"] is True
    r = await card_tools.execute_tool(wid, a, "delete_card", {"card_id": d}, cards())
    assert r["ok"] is True
    assert card_tools._collect_card_meta(wid).get(d) is None   # d 已不在卡面
    r = await card_tools.execute_tool(
        wid, a, "unlink_cards", {"target_id": d, "type": "关联"}, cards())
    assert r["ok"] is True and r["data"]["removed"] == 1


# ---------------------------------------------------------------- 版本刷新

async def test_refresh_perms_is_idempotent(typed_registry, work):
    """改配置后按 perms_version 批量刷新；重复跑＝零改动（幂等）。"""
    from scripts.refresh_perms import refresh_one_work

    wid, root = work["work_id"], work["work_card_id"]
    await card_store.create_card(wid, "甲", "模型生成", parent=root)
    version = policy.config_version()

    first = await refresh_one_work(wid, version, apply=True)
    assert first["total"] >= 2
    assert first["failed"] == 0
    assert first["updated"] + first["unchanged"] == first["total"]

    before = {cid: await card_store.read_card(wid, cid)
              for cid in card_store.iter_card_metas(wid)}
    second = await refresh_one_work(wid, version, apply=True)
    assert second["updated"] == 0, "第二次跑应为全跳过"
    assert second["unchanged"] == second["total"]
    after = {cid: await card_store.read_card(wid, cid)
             for cid in card_store.iter_card_metas(wid)}
    assert before == after, "幂等跑必须零改动"


async def test_version_bump_refreshes_stale_cards(typed_registry, work,
                                                  monkeypatch, tmp_path):
    """验收第 6 项：改配置（version+1、权限值也改）→ 跑刷新 → 旧卡跟到新配置。

    卡先按 v1 建好（perms_version=1），再换成 v2 配置跑刷新——这正是「类型不变、
    但某个类型配哪些权限会随迭代变」这个前提的机械证据。
    """
    from scripts.refresh_perms import refresh_one_work

    wid, root = work["work_id"], work["work_card_id"]
    a = (await card_store.create_card(wid, "甲", "模型生成", parent=root))["meta"]["card_id"]
    metas = card_store.iter_card_metas(wid)
    # [工具面补齐 2026-10-12] 版本号来自配置，不写死 1——配置已演进到 v2，
    # 写死会把「每次改配置都要同步改测试」变成新的 fossil。
    cur_v = policy.config_version()
    assert metas[a]["perms_version"] == cur_v
    assert "delete" in metas[a][policy.PERM_KEY]

    v2 = {
        "version": cur_v + 1,
        "permissions": {
            "rd_project": {"project": ["read"],
                           "sub_project": ["read", "delete"]},
        },
    }
    cfg_path = tmp_path / "permissions.json"
    cfg_path.write_text(json.dumps(v2, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(policy, "PERMISSIONS_PATH", cfg_path)
    assert policy.config_version() == cur_v + 1

    stats = await refresh_one_work(wid, 2, apply=True)
    assert stats["failed"] == 0
    metas = card_store.iter_card_metas(wid)
    assert metas[root][policy.PERM_KEY] == ["read"]              # 根卡跟到 v2
    assert metas[a][policy.PERM_KEY] == ["read", "delete"]
    assert metas[a][policy.PERM_VERSION_KEY] == cur_v + 1
    assert "delete" not in metas[root][policy.PERM_KEY]


# ---------------------------------------------------------------- V8

async def test_v8_prompt_log_records_all(typed_registry, work):
    """V8：四处打点在 prompt_log 留判定记录（沙箱 LOGS_DIR 读回实证）。"""
    from src.api import file_tools, prompt_log

    # ① 关口（放行＋拒绝各一）
    cards = {"card_v8": _meta("sub_project")}
    policy.check("card_v8", "card_v8", policy.SWITCH_EDIT, cards)
    cards_deny = {"card_v8d": {"card_type": "神秘分类"}}
    with pytest.raises(PermissionError):
        policy.check("card_v8d", "card_v8d", policy.SWITCH_READ, cards_deny)
    # ② 工具面过滤
    card_tools.build_tool_schemas("card_v8", cards=cards)
    # ③ 文件访问锁
    file_tools.check_file_access("whatever.txt", cards["card_v8"])

    log_path = prompt_log.LOGS_DIR / prompt_log.PERM_LOG_FILENAME
    assert log_path.exists(), "打点文件应已落盘"
    recs = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    fns = {r["fn"] for r in recs}
    assert {"check", "tree_scope", "build_tool_schemas",
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
    """R3 回归锚：定格副本与 policy._is_in_subtree 逐例相等（边界＋随机林）。

    树作用域本次从权限开关里**拆出**成为独立函数，但判据逐字未动——本锚就是
    「拆出时没改坏」的证据。
    """
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
