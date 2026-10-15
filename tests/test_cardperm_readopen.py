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


async def test_v1_full_open_read_nonkin(typed_registry, work):
    """V1 读放开：非亲属/非连线/非锚定的旁支卡可读（注册分类全授 READ）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", root)   # 与甲互为旁支
    cards = card_tools._collect_card_meta(wid)

    # 关口层：注册类型全授 read → 不抛即放行；缺卡在关口抛
    # （工具层有存在性先判，走 not_found，关口见不到缺卡）
    policy.check(a, b, policy.SWITCH_READ, cards)
    policy.check(b, a, policy.SWITCH_READ, cards)
    with pytest.raises(PermissionError):
        policy.check(a, "card_999", policy.SWITCH_READ, cards)

    # 工具层：真实走 execute_tool → card_store（T-0010 信封：载荷在 data）
    r = await card_tools.execute_tool(wid, a, "read_card", {"card_id": b}, cards)
    assert r["ok"] is True and r["error"] is None
    assert r["data"]["card_id"] == b and r["data"]["title"] == "乙"


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
                  if t["name"] == "update_shadow")
    assert "card_id" not in schema["parameters"]["properties"]
    raw_b_before = await shadow_store.read_shadow_raw(wid, b)
    body = "## 进度\n- 标记XYZ\n\n## 产物清单\n- 无\n\n## 迭代历史\n- v1\n"
    r = await card_tools.execute_tool(
        wid, a, "update_shadow", {"card_id": b, "content": body}, cards)
    assert r.get("ok") is True
    raw_a = await shadow_store.read_shadow_raw(wid, a)
    assert f"shadow_of: \"{a}\"" in raw_a and "标记XYZ" in raw_a
    assert await shadow_store.read_shadow_raw(wid, b) == raw_b_before


async def test_v4_shadow_and_memory_read_open(typed_registry, work):
    """V4 影子/记忆读随读放开一并放行；影子写对注册分类放行（定夺 1 偏离位）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", root)
    cards = card_tools._collect_card_meta(wid)

    body = "## 进度\n- 甲的影子正文\n\n## 产物清单\n- 无\n\n## 迭代历史\n- v1\n"
    r = await card_tools.execute_tool(wid, a, "update_shadow", {"content": body}, cards)
    assert r.get("ok") is True

    # 旁支读影子：放行
    r = await card_tools.execute_tool(wid, b, "read_shadow", {"card_id": a}, cards)
    assert r["ok"] is True
    assert r["data"]["owner_card_id"] == a and "甲的影子正文" in r["data"]["content"]

    # 旁支读记忆：放行
    await memory_store.save_memory(wid, a, {
        "card_id": a, "mark": 1,
        "entries": [{"seq": 0, "conclusion": "【结论】甲结论",
                     "evidence": "【依据】read_card",
                     "occurred_at": "2026-10-07T12:00:00+08:00",
                     "fingerprint": "sha256:ff"}]})
    r = await card_tools.execute_tool(wid, b, "read_memory", {"card_id": a}, cards)
    assert r["ok"] is True
    assert r["data"]["card_id"] == a
    assert r["data"]["entries"][0]["conclusion"] == "【结论】甲结论"


# ---------------------------------------------------------------- 重做后的新钉子

async def test_perms_are_frozen_on_card_surface(typed_registry, work):
    """加固化（裁定 4）：建卡即落 perms ＋ perms_version，值＝配置，卡面可读。

    两级定位：根卡 project_class ＋ card_type="project"；子卡从本树继承
    project_class ＋ card_type="sub_project"（工牌只挂根卡）。
    """
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    metas = card_tools._collect_card_meta(wid)
    expect = {
        root: ("rd_project", "project"),
        a: ("rd_project", "sub_project"),
    }
    version = policy.config_version()
    assert version >= 1
    for cid, (pc, ct) in expect.items():
        assert metas[cid]["card_type"] == ct, cid
        assert metas[cid][policy.PERM_KEY] == policy.perms_for(pc, ct), cid
        assert metas[cid][policy.PERM_VERSION_KEY] == version, cid
        # [卡片存储迁移 SQLite] 固化字段在库行/读侧 meta 里（不再有 md front matter）
        assert metas[cid].get(policy.PERM_KEY) is not None, cid
        assert metas[cid].get(policy.PERM_VERSION_KEY) is not None, cid


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


async def test_unconfigured_type_is_denied_everything(work):
    """默认关门（裁定 5）：卡上没有（或空）perms ⇒ 一项权限都没有。"""
    ghost = {"card_id": "card_ghost", "card_type": "tool"}
    for switch in policy.SWITCHES:
        assert policy.has_perm(ghost, switch) is False, switch
    with pytest.raises(PermissionError):
        policy.check("card_ghost", "card_ghost", policy.SWITCH_READ,
                     {"card_ghost": ghost})
    # 未知开关同样拒（不存在的开关谈不上授权）
    assert policy.has_perm({"perms": ["read"]}, "read_everything") is False


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
                "mission", "status_block", "rulings",
                "conclusion", "rejections", "charter", "parent"):
        assert key in routes.ALLOWED_UPDATE_KEYS, key


def _visible(card_meta: dict) -> set[str]:
    """给定一张卡，返回「下发给模型的工具名集合」。"""
    tools = [{"name": n} for n in card_tools.TOOL_SWITCH]
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
    full = policy.perms_for("rd_project", "sub_project")
    base = {"card_id": "card_x", "card_type": "sub_project", "perms": full}

    # 基线：全权限时每件工具都可见
    assert _visible(base) == set(card_tools.TOOL_SWITCH), "全权限不该有工具被藏"

    # 逐个开关摘掉 → 对应工具必须从可见集合里消失
    for switch, tool_name in (
        ("edit", "edit_card"),
        ("delete", "delete_card"),
        ("create_child", "create_card"),
        ("link", "link_cards"),
        ("unlink", "unlink_cards"),
        ("shadow_write", "update_shadow"),
        ("manuscript", "read_manuscript"),
        ("invoke_tool", "run_tool_card"),
        ("tools_file", "read_file"),
        ("tools_web", "web_search"),
        ("plan", "create_plan"),
    ):
        card = dict(base, perms=[p for p in full if p != switch])
        assert tool_name not in _visible(card), \
            f"摘掉 {switch} 后 {tool_name} 仍可见 —— 工具面没跟上执行层"

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
    assert _visible(card) == {"read_card", "read_memory", "read_shadow",
                              "read_linked",
                              "edit_card", "delete_card", "update_shadow",
                              "create_plan", "plan_reorder", "plan_insert",
                              "plan_remove", "plan_modify", "step_complete"}
    # [编程卡 2026-10-15 · C2 裁定] search 回归词表＝**预留机关**（coder 行占位码；
    # 仍无独立 search 工具、搜索由 web_search/fetch_web 承担，暂无消费方——
    # 配了不等于能用）。断言意图由「已删」随裁定翻新为「在册为预留」。
    assert "search" in policy.SWITCHES
    assert hasattr(policy, "SWITCH_SEARCH")


async def test_manuscript_and_invoke_tool_have_exec_gate():
    """这 4 件工具原先既无开关也无关口＝裸奔，现在执行层必须也拒。

    工具面挡的是「看不见」，执行层挡的是「真被调到」——两层都要有。
    """
    cards = {"card_x": {"card_id": "card_x", "card_type": "sub_project",
                        "perms": []}}
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

    # 有 plan/manuscript/invoke_tool 开关时，manuscript 三件应放行到业务层
    ok_cards = {"card_x": {"card_id": "card_x", "card_type": "sub_project",
                           "perms": ["read", "manuscript"]}}
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
    full = policy.perms_for("rd_project", "sub_project")
    base = {"card_id": "card_x", "card_type": "sub_project", "perms": full}
    assert _visible(base) == set(card_tools.TOOL_SWITCH)

    card = dict(base, perms=[p for p in full if p != "plan"])
    vis = _visible(card)
    for name in ("create_plan", "plan_reorder", "plan_insert",
                 "plan_remove", "plan_modify", "step_complete"):
        assert name not in vis, f"摘掉 plan 后 {name} 仍可见"
