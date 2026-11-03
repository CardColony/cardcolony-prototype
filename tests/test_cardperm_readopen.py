# -*- coding: utf-8 -*-
"""[卡片权限改造 2026-10-07 → 权限收口 2026-10-09 → 权限底座重做 2026-10-12]
读侧全盘放开的政策断言。

重做后（任务书《权限底座重做》2026-10-12）：权限＝**按卡片类型分配**，表在
config/permissions.json，两级定位 project_class ＋ card_type；**建卡时算一次
写进卡的 perms**，运行时只读卡上的 perms；配置没配到的类型＝全拒（默认关门）。

本文件钉的是**这组裁定带来的行为**：读＝注册类型全授 read（存在即放行）；
写＝edit/delete ＋ 树作用域（只能动自己子树内的卡）；护栏（根卡拒删/有子卡
拒删/影子写签名级独占）原样。

原先直调 policy 纯函数（check_with_scope / Perm 位图）的断言已随本次重做改为
新关口（policy.check ＋ 开关常量）的等价断言——判据本体换了，被钉的行为没换。

改前对照组见 _cardperm_20261007/verify_control.py（施工留痕，不入套件）。
"""
from __future__ import annotations

import pytest

from src.api import card_tools
from src.permissions import policy
from src.storage import card_db, card_store, memory_store, shadow_store


async def _mk_child(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "模型生成", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def test_v1_read_requires_a_line(typed_registry, work):
    """[关系治理 2026-10-30] 读按线判，**推翻**旧的「V1 读放开」。

    用户令：「read 不是全局都能读，权限应该是按三种线来判断的」——
    两张旁支卡之间既无父子线也无关系线 ⇒ 读不到（旧口径是"注册类型全授 read 就能读"）。
    父卡读子卡仍在（父子线）。
    """
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", root)   # 与甲互为旁支，没有任何线
    cards = card_tools._collect_card_meta(wid)

    # 工具层：旁支之间读不到（denied 信封，文案指出"没有线"）
    r = await card_tools.execute_tool(wid, a, "read_card", {"card_id": b}, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied"
    assert "没有父子线也没有关系线" in r["error"]["message"]

    # 父子线：父读子仍然通
    r2 = await card_tools.execute_tool(wid, root, "read_card", {"card_id": b}, cards)
    assert r2["ok"] is True and r2["data"]["card_id"] == b

    # 不存在的卡仍走 not_found（不是越权）
    r3 = await card_tools.execute_tool(wid, a, "read_card", {"card_id": "card_999"}, cards)
    assert r3["ok"] is False and r3["error"]["code"] == "not_found"


async def test_v2_write_still_restricted(typed_registry, work):
    """V2 写仍受限：越权 edit/delete 明确拒绝且不落盘。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", root)
    cards = card_tools._collect_card_meta(wid)

    # 关口层：旁支不可写（树类开关叠树作用域），自己可写
    with pytest.raises(PermissionError):
        policy.check(a, b, policy.SWITCH_EDIT, cards)
    policy.check(a, a, policy.SWITCH_EDIT, cards)

    before = (await card_store.read_card(wid, b))["body"]
    r = await card_tools.execute_tool(
        wid, a, "edit_card", {"card_id": b, "content": "越权写入"}, cards)
    assert "越权" in r["error"]["message"]
    assert (await card_store.read_card(wid, b))["body"] == before

    r = await card_tools.execute_tool(wid, a, "delete_card", {"card_id": b}, cards)
    assert "越权" in r["error"]["message"]
    assert card_store.card_exists(wid, b)   # 未删


async def test_v3_guards_intact(typed_registry, work):
    """V3 护栏不破：根卡拒删/有子卡拒删/影子写签名级独占（读放开后行为不变）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", a)
    cards = card_tools._collect_card_meta(wid)

    # 根卡不可删（工具层身份护栏；收口后前移一行，拒绝文案逐字不变）
    r = await card_tools.execute_tool(wid, root, "delete_card", {"card_id": root}, cards)
    assert "不可删除" in r["error"]["message"]
    # 有子卡的卡不可删（存储层结构护栏；关口 DELETE 对自卡放行后由存储层拒）
    r = await card_tools.execute_tool(wid, a, "delete_card", {"card_id": a}, cards)
    assert r["ok"] is False
    assert card_store.card_exists(wid, a)

    # update_shadow 签名级独占：schema 无 card_id；恶意注入 card_id 也写不出别的目标
    schema = next(t for t in card_tools.build_tool_schemas(root, cards=cards)
                  if t["name"] == "edit_shadow")
    assert "card_id" not in schema["parameters"]["properties"]
    raw_b_before = await shadow_store.read_shadow_raw(wid, b)
    body = "## 进度\n- 标记XYZ\n\n## 产物清单\n- 无\n\n## 迭代历史\n- v1\n"
    r = await card_tools._h_update_shadow(wid, a, {"card_id": b, "content": body}, cards)
    assert r.get("ok") is True
    raw_a = await shadow_store.read_shadow_raw(wid, a)
    assert f"shadow_of: \"{a}\"" in raw_a and "标记XYZ" in raw_a
    assert await shadow_store.read_shadow_raw(wid, b) == raw_b_before


async def test_v4_shadow_and_memory_read_open(typed_registry, work):
    """V4 影子/记忆读**按线判**（2026-10-30 推翻旧「读放开」）；影子写对注册分类放行。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", root)
    cards = card_tools._collect_card_meta(wid)

    body = "## 进度\n- 甲的影子正文\n\n## 产物清单\n- 无\n\n## 迭代历史\n- v1\n"
    r = await card_tools._h_update_shadow(wid, a, {"content": body}, cards)
    assert r.get("ok") is True

    # [关系治理 2026-10-30] 影子/记忆同 read_card：**按线判**，旁支读不到。
    r = await card_tools.execute_tool(wid, b, "read_shadow", {"card_id": a}, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied"
    r = await card_tools.execute_tool(wid, a, "read_shadow", {"card_id": a}, cards)
    assert r["ok"] is True and "甲的影子正文" in r["data"]["content"]

    # 旁支读记忆：同样拒
    await memory_store.save_memory(wid, a, {
        "card_id": a, "mark": 1,
        "entries": [{"seq": 0, "conclusion": "【结论】甲结论",
                     "evidence": "【依据】read_card",
                     "occurred_at": "2026-10-07T12:00:00+08:00",
                     "fingerprint": "sha256:ff"}]})
    r = await card_tools.execute_tool(wid, b, "read_memory", {"card_id": a}, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied"
    r = await card_tools.execute_tool(wid, root, "read_memory", {"card_id": a}, cards)
    assert r["ok"] is True and r["data"]["card_id"] == a


# ---------------------------------------------------------------- 重做后的新钉子

async def test_capabilities_are_frozen_on_card_surface(typed_registry, work):
    """建卡即落 mode ＋ abilities（[perms 退役 2026-10-30] 取代原「perms 冻结」）。

    卡上不再有 perms——「这张卡最多能碰什么」只在建卡那一刻用来算挂载上限，
    落库的是算出来的能力清单。
    """
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    metas = card_tools._collect_card_meta(wid)
    for cid in (root, a):
        m = metas[cid]
        assert "perms" not in m, f"{cid} 不该再有 perms"
        assert m.get("mode") == "manual", m.get("mode")
        assert isinstance(m.get("abilities"), list) and m["abilities"], cid

async def test_card_type_is_immutable(work):
    """类型不可变（裁定 2）：update_card 改 card_type / project_class 一律拒，
    且**卡面零改动**。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    before = await card_store.read_card(wid, a)
    rev_before = card_db.read_rev(wid)

    for updates in ({"card_type": "tool"},
                    {"project_class": "research_project"}):
        res = await card_store.update_card(wid, a, updates, "试改类型")
        assert "error" in res, updates
        assert "不可修改" in res["error"], updates
    assert await card_store.read_card(wid, a) == before, "拒改类型必须零改动"
    assert card_db.read_rev(wid) == rev_before, "拒改类型不得动库版本号"


async def test_unconfigured_type_has_no_capabilities(work):
    """默认关门（原裁定 5 的能力版）：卡型没配 ⇒ 算不出任何能力 ⇒ 空工具面。

    [perms 退役 2026-10-30] 原判据是「卡上没有 perms ⇒ 一项权限都没有」；
    现在 perms 没了，同一条「默认关门」落在能力上：算不出能力 ⇒ 什么都不下发。
    """
    ghost = {"card_id": "card_ghost", "card_type": "no_such_type"}
    assert card_tools._abilities_of(ghost, {}) == set()
    assert card_tools.build_tool_schemas(
        "card_ghost", cards={"card_ghost": ghost}) == []

async def test_refresh_then_root_card_still_cannot_delete(work):
    """验收第 4 项：根卡（配置无 delete 开关）经关口 DELETE 必拒。"""
    wid, root = work["work_id"], work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)
    assert policy.has_perm(cards[root], policy.SWITCH_DELETE) is False
    r = await card_tools.execute_tool(wid, root, "delete_card",
                                      {"card_id": root}, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied"


async def test_api_update_whitelist_has_no_card_type():
    """裁定 2 的 API 层落点：card_type 不在 routes.ALLOWED_UPDATE_KEYS。

    堵的是「非法键过了 API 白名单、才轮到存储层拒」这条缝——两层都在时
    API 层就该先拒（错误更早暴露、契约更干净）。钉白名单本身，
    防以后有人把 card_type 加回去。
    """
    from src.api import routes
    assert "card_type" not in routes.ALLOWED_UPDATE_KEYS
    # project_class 从来不在白名单（建卡时随 project 写入），一并钉住
    assert "project_class" not in routes.ALLOWED_UPDATE_KEYS
    # 其余合法键不许被误删
    for key in ("title", "tags", "relations", "session_ref",
                "mission", "status_block", "requirements",
                "conclusion", "rejections", "charter", "parent"):
        assert key in routes.ALLOWED_UPDATE_KEYS, key


def _visible(card_meta: dict) -> set[str]:
    """给定一张卡，返回「下发给模型的工具名集合」。

    [自动模式 2026-10-17 · 回灌 5700 块0.4] 初始池与 build_tool_schemas 的
    **真实工具池同口径**：排除 OP_ONLY_TOOLS 与 AUTO_ONLY_TOOLS（两类都
    非卡片内挂、不进四段过滤）——否则分类层镜头会把 update_goal 算进
    tool 卡可见集，而真实工具面它永远拿不到（只在 auto 卡末段追加）。
    """
    pool = [n for n in card_tools.TOOL_SWITCH
            if n not in card_tools.OP_ONLY_TOOLS
            and n not in card_tools.AUTO_ONLY_TOOLS
            and n not in card_tools.ROLE_ONLY_TOOLS]
    tools = [{"name": n} for n in pool]
    cards = {"card_x": card_meta}
    return {t["name"] for t in
            card_tools._filter_by_category(tools, "card_x", "", cards)}


def test_tool_switch_covers_every_registered_tool():
    """映射完备性：注册表里的每件工具都要在 TOOL_SWITCH 里，且开关合法。

    钉的是「以后新加工具忘了配开关」——那件工具会被默认关门拦下（不可见），
    是安全方向；但它意味着功能静默失效，所以用测试逼着补上。
    """
    for name in card_tools.TOOL_REGISTRY:
        switch = card_tools.TOOL_SWITCH.get(name)
        assert switch is not None, f"{name} 未配开关（会被工具面静默拦下）"
        assert switch in policy.SWITCHES, f"{name} 的开关不在词汇表: {switch}"
    # 文件四件来自 file_tools 追加，同样要有映射
    for name in card_tools._FILE_GROUP_TOOLS + card_tools._WEB_GROUP_TOOLS:
        assert name in card_tools.TOOL_SWITCH, name


def test_no_permission_means_tool_invisible_not_just_denied():
    """**用户裁定：没权限的卡压根看不见对应工具**，不是「看得见、调下去被拒」。

    这是本次补齐的核心。此前 _filter_by_category 只按 tools_file/tools_web
    两组过滤，卡片 6 个开关与 shadow_write 只在执行层有关口——配置里删掉
    edit，模型照样看得见 edit_card。改一个开关要同时收掉「可见」和「可调」，
    否则权限收束对模型是假象。
    """
    # [执行权集中 2026-10-21] 用合成全开关当夹具，
    # 不再拿 sub_project（管理卡已无执行权，见 _FULL_SWITCHES 说明）
    full = sorted(policy.SWITCHES)
    base = {"card_id": "card_x", "card_type": "sub_project", "perms": full}

    # 基线：全权限时每件工具都可见
    assert _visible(base) == set(card_tools.TOOL_SWITCH) - card_tools.OP_ONLY_TOOLS - card_tools.AUTO_ONLY_TOOLS - card_tools.ROLE_ONLY_TOOLS, "全权限不该有工具被藏（非卡片内挂件除外）"  # [自动模式块0.4] AUTO_ONLY 同 OP_ONLY 口径排除；[角色机制5710] ROLE_ONLY 同

    # 逐个开关摘掉 → 对应工具必须从可见集合里消失
    for switch, tool_name in (
        ("edit", "edit_card"),
        ("delete", "delete_card"),
        ("create_child", "create_card"),
        ("link", "link_cards"),
        ("unlink", "unlink_cards"),
        ("shadow_write", "edit_shadow"),
        ("manuscript", "read_manuscript"),
        ("invoke_tool", "run_tool_card"),
        # [执行权集中 2026-10-21] 读文件改挂 tools_read_file（读写分离）：
        # 摘掉 tools_file 只收走写/执行，读要看 tools_read_file。
        # 读文件的开关有「写隐含读」语义（policy._SWITCH_IMPLIES），
        # 单独摘 tools_read_file 不该让 read_file 消失 —— 见循环后的专项断言。
        ("tools_file", "write_file"),
        ("tools_web", "web_search"),
        ("plan", "create_plan"),
    ):
        card = dict(base, perms=[p for p in full if p != switch])
        assert tool_name not in _visible(card), \
            f"摘掉 {switch} 后 {tool_name} 仍可见 —— 工具面没跟上执行层"

    # ── 读写分离的专项断言（[执行权集中 2026-10-21]）──────────────
    # 语义：tools_file（写）隐含 tools_read_file（读）——写权限包含读权限，
    #       policy._SWITCH_IMPLIES 里有这条；反过来不成立。
    no_write = dict(base, perms=[p for p in full if p != "tools_file"])
    assert "write_file" not in _visible(no_write)
    assert "read_file" in _visible(no_write), "留 tools_read_file 时应能读（写已摘）"

    no_read = dict(base, perms=[p for p in full if p != "tools_read_file"])
    assert "read_file" in _visible(no_read), "留 tools_file 时读仍可见（写隐含读）"

    neither = dict(base, perms=[p for p in full
                                if p not in ("tools_file", "tools_read_file")])
    assert "read_file" not in _visible(neither), "读写开关都摘掉后不应能读"
    assert "write_file" not in _visible(neither)

    # 默认关门：卡上没有 perms / 空 perms ⇒ 一件工具都看不见
    for bad in ({"card_id": "card_x", "card_type": "sub_project"},
                {"card_id": "card_x", "card_type": "sub_project", "perms": []}):
        assert _visible(bad) == set(), "无 perms 的卡不应看见任何工具"


def test_tool_card_toolface_matches_its_perms():
    """tool 卡只看得见 read/edit/delete/shadow_write/plan 对应的那几件。

    tool 是被调用的执行体：不该建卡、不该碰文件联网、不该查资料、不该调工具卡；
    但能推进自己的剧本步骤（plan）。
    """
    tool_perms = policy.perms_for("rd_project", "tool")
    card = {"card_id": "card_t", "card_type": "tool", "perms": tool_perms}
    # [卡片底座第一步 2026-10-14] tool 类型 v3 起配了 read_linked ⇒ read_linked 可见
    # [规则卡 2026-10-15 · 块3 · 随版本推进翻新] draft_rule_edit 挂 read ⇒ 在本测试
    # 的「分类层」镜头（_visible 只跑第二段 _filter_by_category）里可见——它属 read
    # 家族；真实下发面还要过第四段根卡闸（ROOT_ONLY_TOOLS），tool 卡拿不到：
    # 见 test_rule_card.py::test_draft_rule_edit_toolface_root_only。
    # [更新现状按钮改造 2026-10-16] propose_status 挂 read，但**不进本测试的
    # _visible 池**（列 OP_ONLY_TOOLS、非卡片内挂；_visible 的初始池已与
    # build_tool_schemas 真实工具池同口径排除它）⇒ 分类层镜头里也看不见：
    # 见 tests/test_status_update.py::test_propose_status_not_in_normal_toolface。
    # [换父 2026-10-20] reparent_card 挂 edit ⇒ **分类层镜头里看得见**（本测试
    # 只跑 _filter_by_category，不跑后四段）；真实下发面被 build_tool_schemas 的
    # 五级闸摘掉（tool 卡 can_have_children=false，没有子树可调）：
    # 见 tests/test_reparent_card.py::test_toolface_only_for_types_that_can_have_children。
    # [2026-10-22] tool 的 perms 自 v3 起就含 send_card_message，本期望此前漏了它。
    # wake_card 不在：它挂 CREATE_CHILD，tool 没有该开关（唤醒是组织动作，见 D04）。
    # [v14 · 2026-10-24 · 用户裁定「查资料≠动手」] tool 现在也有 tools_web
    # ⇒ 查资料两件（web_search/fetch_web）在分类层镜头里可见；
    # **动手面一件没多**（文件四件/bash/python 仍不在）。
    assert _visible(card) == {"read_card", "read_memory", "read_shadow",
                              "read_linked", "draft_rule_edit", "list_functions",
                              "edit_card", "delete_card",
                              # [影子精准编辑/卡片笔记 2026-10-27] 两件都挂 shadow_write：
                              # edit_shadow（按节精准）＋ edit_note（笔记）。
                              # [去全量更新 2026-10-28] update_shadow 已退役；
                              # 现状改写件 edit_status 挂 edit ⇒ 分类层可见。
                              "edit_shadow", "edit_note", "edit_status",
                              # [需求确定按钮改造 2026-10-29] edit_requirements 挂 edit
                              # ⇒ 分类层镜头里可见；真实下发面被末段⑦按卡类型摘掉
                              # （tool 卡没有「需求文档→评审→正稿」这条流程）。
                              "edit_requirements",
                              "reparent_card",
                              # [归档 2026-10-25] archive/unarchive 挂 delete；
                              # [归档读口/stage_open 2026-10-26] read_archive 与
                              # stage_open 挂 read ⇒ tool 有 read+delete ⇒ 分类层可见
                              "archive_card", "unarchive_card",
                              "read_archive", "stage_open",
                              "create_plan", "plan_reorder", "plan_insert",
                              "plan_remove", "plan_modify", "step_complete",
                              # [harness 给根卡 2026-10-28] design_policy 挂 plan ⇒
                              # 分类层镜头里可见（tool 有 plan）；真实下发面被第四段
                              # 根卡闸（ROOT_ONLY_TOOLS）收走，tool 卡拿不到——
                              # 与 draft_rule_edit 同一种处理。
                              "design_policy",
                              # [2026-10-30 评审流程] 两件新工具：
                              # reject 与 approve 同挂 read（评审表态，分类层可见）；
                              # update_status 挂 edit（改自己的现状块）。
                              # 真实下发面仍由末段按卡类型收口（tool 卡走不到评审流程）。
                              "reject",
                              # [2026-11-02 翻新] update_status 已退役（注册表移除）；
                              # 同期漂移补记：report 挂 read（差事回执）、
                              # memory_expand 挂 read（记忆下探）、
                              # submit_revision 挂 edit（评审修订）⇒ 分类层均可见。
                              "report", "memory_expand", "submit_revision",
                              # promote_draft 挂 edit ⇒ 分类层可见；它自己判
                              # 「有草稿 + 评审全员通过」，没草稿的卡调了必被拒。
                              "promote_draft",
                              # [浏览器能力 2026-11-02] browser 挂 tools_web ⇒
                              # 分类层镜头里可见（tool 有 tools_web）；真实下发面
                              # 再被能力挂载闸收窄（tool 卡默认不挂 browser）。
                              "browser",
                              "web_search", "fetch_web"}
    # [编程卡 2026-10-15 · C2 裁定] search 回归词表＝**预留机关**（coder 行占位码；
    # 仍无独立 search 工具、搜索由 web_search/fetch_web 承担，暂无消费方——
    # 配了不等于能用）。断言意图由「已删」随裁定翻新为「在册为预留」。
    assert "search" in policy.SWITCHES
    assert hasattr(policy, "SWITCH_SEARCH")


async def test_manuscript_and_invoke_tool_have_exec_gate():
    """这 4 件工具原先既无开关也无关口＝裸奔，现在执行层必须也拒。

    工具面挡的是「看不见」，执行层挡的是「真被调到」——两层都要有。
    """
    # [perms 退役 2026-10-30] 用「明确挂了 0 项能力」表达"未授权"
    cards = {"card_x": {"card_id": "card_x", "card_type": "sub_project",
                        "abilities": []}}
    wid, cid = "work_999", "card_x"
    args = {"chapter": "ch_1", "keyword": "x", "script_name": "s"}
    probes = ((card_tools._h_list_manuscript, "list_manuscript"),
              (card_tools._h_read_manuscript, "read_manuscript"),
              (card_tools._h_search_manuscript, "search_manuscript"),
              (card_tools._h_run_tool_card, "run_tool_card"))
    for fn, name in probes:
        r = await fn(wid, cid, args, cards)
        assert r.get("ok") is False, f"{name} 无权限时竟未被拒: {r}"
        assert r["error"]["code"] == "denied", f"{name} 拒绝码不对: {r}"

    # 挂了「查资料」能力时，manuscript 三件应放行到业务层
    # [perms 退役 2026-10-30] 授权载体从 perms 开关换成能力 code
    ok_cards = {"card_x": {"card_id": "card_x", "card_type": "sub_project",
                           "abilities": ["manuscript"]}}
    r = await card_tools._h_list_manuscript(wid, cid, args, ok_cards)
    assert r.get("ok") is not False or r["error"]["code"] != "denied", \
        f"授了 manuscript 仍被拒: {r}"


async def test_plan_tools_have_exec_gate():
    """思维链六件同属 plan 开关，两层都要接（此前完全裸奔）。

    特别防一类回归：``unmapped → 不下发`` 的默认关门规则，会把没配开关的
    工具静默拦掉——思维链六件注册在同一个 TOOL_REGISTRY 里，漏配就会让
    整套思维链工具对所有卡消失。故这里逐件验证。
    """
    from src.thinkchain import plan_tools

    assert plan_tools._plan_gate("card_x", {"card_x": {"perms": []}}) is not None
    assert plan_tools._plan_gate("card_x", {"card_x": {"perms": ["plan"]}}) is None
    # 认不出的卡（不在 cards 里）也拒＝默认关门
    assert plan_tools._plan_gate("card_x", {}) is not None

    cards = {"card_x": {"card_id": "card_x", "card_type": "sub_project",
                        "perms": []}}
    args = {"name": "p", "steps": [{"id": "s1", "prompt": "x",
                                    "criteria": "y", "deliverable": "z",
                                    "type": "execute"}]}
    r = await plan_tools._h_create_plan("work_999", "card_x", args, cards)
    assert r.get("ok") is False and r["error"]["code"] == "denied", r
    r = await plan_tools._h_step_complete("work_999", "card_x",
                                          {"result": "a", "criteria_met": "b"},
                                          cards)
    assert r.get("ok") is False and r["error"]["code"] == "denied", r
    # 改板四动作的公共点也要拒
    r = await plan_tools._h_plan_reorder("work_999", "card_x", {"order": []}, cards)
    assert r.get("ok") is False and r["error"]["code"] == "denied", r


async def test_plan_switch_controls_toolface():
    """摘掉 plan 开关 ⇒ 思维链六件从工具面消失（不是只被拒）。"""
    # [执行权集中 2026-10-21] 用合成全开关当夹具，
    # 不再拿 sub_project（管理卡已无执行权，见 _FULL_SWITCHES 说明）
    full = sorted(policy.SWITCHES)
    base = {"card_id": "card_x", "card_type": "sub_project", "perms": full}
    assert _visible(base) == set(card_tools.TOOL_SWITCH) - card_tools.OP_ONLY_TOOLS - card_tools.AUTO_ONLY_TOOLS - card_tools.ROLE_ONLY_TOOLS  # [自动模式块0.4] AUTO_ONLY 同 OP_ONLY 口径排除；[角色机制5710] ROLE_ONLY 同

    card = dict(base, perms=[p for p in full if p != "plan"])
    vis = _visible(card)
    for name in ("create_plan", "plan_reorder", "plan_insert",
                 "plan_remove", "plan_modify", "step_complete"):
        assert name not in vis, f"摘掉 plan 后 {name} 仍可见"
