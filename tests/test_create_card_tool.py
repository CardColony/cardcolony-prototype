# -*- coding: utf-8 -*-
"""建卡工具通道（create_card）· 回归网

[建卡工具化 2026-10-08] 钉住三件事：
1. **权限口径**：只能建自己的子卡片（用户 2026-10-08 14:14 定）——越权必拒。
2. **回执真身**：成功后返回真身 card_id，模型能确证建成（工具化的核心收益）。
3. **双通道并存 + 跨通道去重**：工具通道建过的卡名，文本块同名不重复建。

机制层铁律同conftest：不 import main、不触真实数据（沙箱由 autouse fixture 提供）。
真模型实弹验证走 tests/test_cardperm_live.py 那套（须显式开关），
本文件只钉纯行为，不烧模型额度。
"""
from __future__ import annotations

import pytest

from src.api import card_proposal, card_tools
from src.storage import card_store, shadow_store


# ---------------------------------------------------------------- schema 面

def _cards_with_full_perms() -> dict:
    """构造一张 rd_project/sub_project 卡（该类型十项开关全开）。

    [权限底座重做 2026-10-12] 工具面是按「卡上固化的 perms」下发的：不带卡
    上下文 ⇒ 默认关门（空列表）。要看完整工具面必须给一张有 perms 的卡。
    """
    from src.permissions import policy

    return {
        "card_x": {
            "card_id": "card_x",
            "card_type": "sub_project",
            "perms": policy.perms_for("rd_project", "sub_project"),
        }
    }


def test_create_card_in_tool_schemas():
    """工具表含 create_card，且带 title/parent/content 三参数。"""
    cards = _cards_with_full_perms()
    names = [t["name"] for t in card_tools.build_tool_schemas("card_x", cards=cards)]
    assert names.count("create_card") == 1
    schema = next(t for t in card_tools.build_tool_schemas("card_x", cards=cards)
                  if t["name"] == "create_card")
    assert set(schema["parameters"]["properties"]) == {"title", "parent", "content"}
    assert schema["parameters"]["required"] == ["title"]


def test_no_card_context_returns_empty_toolface():
    """[权限底座重做 2026-10-12] 默认关门：认不出卡 ⇒ 空工具面（不再是全放行）。"""
    assert card_tools.build_tool_schemas() == []
    assert card_tools.build_tool_schemas("", cards=None) == []


def test_create_card_is_write_tool_not_readonly():
    """建卡落存储层新卡 ⇒ 属写工具，不进只读白名单（折叠【依据】语义依赖此分类）。"""
    assert "create_card" not in card_tools.READONLY_TOOLS


# [销红 2026-10-12] 裁定1（docs/工具线收拢后清理-任务书-20261012.md:128 「revert 回 f14e717」）：
# 按 `git checkout f14e717 -- tests/test_create_card_tool.py` 整文件还原；
# 下两处版断言对齐 dev 现态 src/api/card_tools.py:134 = "tools-tc-3"、src/api/prompts.py:38 = "t17-1"。
def test_tool_table_version_bumped():
    """工具集变更须版本化（B-15/ T12-R3 同款纪律）。

    [卡片信息结构改造 S3-8 · 2026-10-08] 推进到 tc-2：edit_card schema 随闸语义更新。
    [工具卡底座 T7 · 2026-10-09] 推进到 tc-3：新增第 19 件工具 run_tool_card（调用侧，
    工具卡底座的保留件；T9「主卡写工具收回」已按用户 2026-10-09 17:38 令回滚，
    故此处只记工具集本身的变更，不涉角色口径）。
    [卡片底座第一步 2026-10-14] 推进到 tc-4：新增第 20 件工具 read_linked（沿通信线读）。
    [编程卡 2026-10-15] 推进到 tc-5：新增 report_done / create_coder_card 两件，
    build_tool_schemas 增第三段 card_type 闸。
    [规则卡 2026-10-15] 推进到 tc-6：新增 draft_rule_edit（第 19 件，根卡起草
    规则区修改），build_tool_schemas 增第四段根卡闸。
    """
    # [依赖关联线 2026-10-16] tc-6 → tc-7（新增 link_rel / unlink_rel）。
    # [更新现状按钮改造 2026-10-16] tc-7 → tc-8（新增 propose_status，op 专用）。
    # [自动模式 2026-10-17 · 回灌 5700 块0.4] tc-8 → tc-9（新增 update_goal，
    # auto 卡工具面件数变化；常规面仍不含它——AUTO_ONLY_TOOLS 机制）。
    assert card_tools.TOOL_TABLE_VERSION == "tools-tc-10"


def test_base_prompt_declares_tool_and_drops_text_protocol():
    """新 prompt 教工具通道、旧 card-proposal 段已删（避免两套教法并存）。"""
    from src.api import prompts
    # [T-0010 §3.4 · 2026-10-08] 推进到 t14-1：新增「工具错误码」小节（信封错误码进 prompt）。
    # [卡片信息结构改造 S1-6 · 2026-10-08] 此前推进到 tc-2：edit_card 说明改写为三块语法。
    # [迭代五 S3 镜像更新] 推进到 t15-1：新增「卡间消息处理」节（应对规则进 prompt）。
    # [思维链真实落地 2026-10-11] 推进到 t16-1：判断协议注入 BASE_PROMPT 开头。
    # [卡片编辑机制改造 2026-10-12] 推进到 t17-1：edit_card/update_shadow 收回，改为
    #   「起草＋用户确认写入」说明（工具列表不再定义这两项）。
    # [判断协议改注入 2026-10-14] 推进到 t18-1：判断协议从 BASE_PROMPT 摘出（改注入）。
    # [卡片区域语义 v3 2026-10-14] 推进到 t19-1：read_shadow 工具行改为「只读本卡自己的
    # 影子」（去掉「上级卡查看子卡影子」）。
    # [编程卡 2026-10-15] 推进到 t20-1：增 create_coder_card / report_done 两工具行＋
    # 「## 编程卡」节。
    # [规则卡 2026-10-15] 推进到 t21-1：增「## 规则卡」使用说明节（根卡
    # draft_rule_edit 起草＋用户审批落盘）。
    # [依赖关联线 2026-10-16] t21-1 → t22-1（通信线 relations→links；新增
    # 依赖关联线工具两行）。
    assert prompts.BASE_PROMPT_VERSION == "t22-1"
    assert "## 卡间消息处理" in prompts.BASE_PROMPT
    assert "create_card(title, parent?, content?)" in prompts.BASE_PROMPT
    assert "card-proposal" not in prompts.BASE_PROMPT
    assert "建卡接口" not in prompts.BASE_PROMPT
    # [卡片编辑机制改造 2026-10-12] edit_card/update_shadow 不再是工具定义行
    # [销红 2026-10-12] 裁定1：此二条 `not in` 断言保留（f14e717 原样，对齐 dev 现态 not in），
    # 钉的是 src/api/prompts.py:86 的真实文字。但该文字与工具面矛盾——P-01（说明书称"已收回"，
    # 而 src/api/card_tools.py:97/102 TOOL_SWITCH 仍含两件、:857/:899 仍在 TOOL_REGISTRY）。
    # 缺陷在 src 不在测试，已立 T-01（merge toolfold-20261012 或等效改真 prompts.py:86）；
    # 届时本处须**翻转**为 `in`，并同步推进 BASE_PROMPT_VERSION。
    assert "- edit_card(" not in prompts.BASE_PROMPT
    assert "- update_shadow(" not in prompts.BASE_PROMPT
    assert "起草＋用户确认" in prompts.BASE_PROMPT
    # [T-0010 §3.3] 错误码小节随协议进 prompt（五码齐全）
    assert "## 工具错误码" in prompts.BASE_PROMPT
    for _code in card_tools.TOOL_ERROR_CODES:
        assert f"{_code}：" in prompts.BASE_PROMPT, _code


# ---------------------------------------------------------------- 权限面

async def _mk(work_id: str, title: str, parent: str) -> str:
    r = await card_store.create_card(work_id, title, "用户直接创建", parent=parent)
    return r["meta"]["card_id"]


async def test_create_card_defaults_parent_to_current(work, sandbox):
    """parent 缺省＝挂当前卡自身（建自己的下一层）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(
        wid, cur, "create_card", {"title": "子议题", "content": "正文"}, cards)
    # [T-0010 §1] 成功载荷在 data 内
    assert out["ok"] is True
    assert out["data"]["parent"] == cur


async def test_create_card_accepts_own_descendant(work, sandbox):
    """parent 落在自己子树内 ⇒ 放行（建孙卡合法）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    child = await _mk(wid, "一级子卡", cur)
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(
        wid, child, "create_card", {"title": "孙卡"}, cards)
    assert out["ok"] is True
    assert out["data"]["parent"] == child


async def test_create_card_denies_out_of_subtree(work, sandbox):
    """越权：parent 是别的分支的卡 ⇒ 拒，且**不建卡**（结构事实，不是模型自觉）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    other = await _mk(wid, "别人的分支卡", cur)
    me = await _mk(wid, "我的分支卡", cur)
    before = len(card_store.iter_card_metas(wid))
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(
        wid, me, "create_card", {"title": "越权卡", "parent": other}, cards)
    # [T-0010 §1] 权限拒绝 → denied，文案逐字不动
    assert out["ok"] is False
    assert out["error"]["code"] == "denied"
    assert "只能建自己的子卡片" in out["error"]["message"]
    after = len(card_store.iter_card_metas(wid))
    assert after == before, "越权必须零落盘"


async def test_create_card_rejects_sibling_parent(work, sandbox):
    """兄弟卡不可作为 parent（同级不属自己子树）——口径最严的一档。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    a = await _mk(wid, "甲", cur)
    b = await _mk(wid, "乙", cur)
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(
        wid, a, "create_card", {"title": "越权", "parent": b}, cards)
    assert out["ok"] is False and out["error"]["code"] == "denied"


async def test_create_card_nonexistent_parent_not_oversight_error(work, sandbox):
    """已删/不存在的 parent 报「不存在」，不误报「越权」（语义修正同 read_card）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(
        wid, cur, "create_card", {"title": "X", "parent": "card_999"}, cards)
    # [T-0010 §1] 不存在 → not_found（不是 denied）
    assert out["ok"] is False
    assert out["error"]["code"] == "not_found"
    assert out["error"]["message"].startswith("父卡不存在")


async def test_create_card_empty_title_rejected(work, sandbox):
    """title 空 ⇒ 拒（不建空名卡）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(wid, cur, "create_card", {"title": "   "}, cards)
    # [T-0010 §1] 参数缺失/为空 → validation
    assert out["ok"] is False and out["error"]["code"] == "validation"


# ---------------------------------------------------------------- 回执面

async def test_create_card_receipt_has_real_card_id(work, sandbox):
    """回执带真身 card_id —— 模型据此确证建成，不必再猜卡号。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(wid, cur, "create_card", {"title": "回执卡"}, cards)
    assert out["ok"] is True
    new_id = out["data"]["card_id"]
    assert new_id.startswith("card_")
    # 回执卡号必须是库内真身（不靠猜、不靠预判）
    assert card_store.card_exists(wid, new_id)
    # 正卡即派生影子卡（一正一影，R-03；影子在库内）
    assert shadow_store._meta_of(wid, new_id) is not None


async def test_create_card_syncs_meta_cache(work, sandbox):
    """建卡后同轮后续工具能看见新卡（meta 缓存同步，不留幽灵）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(wid, cur, "create_card", {"title": "新卡"}, cards)
    assert out["data"]["card_id"] in cards, "新卡须进入同轮 cards 字典"


# ------------------------------------------------- 文本通道同口径 + 跨通道去重

async def test_text_channel_uses_same_rule_as_tool_channel(work, sandbox):
    """文本块通道与 create_card 工具通道**同一口径**（重做 2026-10-12）。

    此前两条通道口径不一（工具通道＝分类位＋子树范围，文本通道＝仅分类位），
    导致「在别人分支下建卡」走文本块能过、走工具被拒——本次按任务书统一为
    同一函数同一参数顺序（policy.check(actor, parent, create_child, cards)）。
    本用例钉**统一后的现状**：parent＝兄弟卡 ⇒ 两通道一样被拒、零落盘。
    """
    wid = work["work_id"]
    cur = work["work_card_id"]
    other = await _mk(wid, "他人卡", cur)
    mine = await _mk(wid, "本卡", cur)
    before = len(card_store.iter_card_metas(wid))
    created, denied = await card_proposal.apply_proposals(
        wid, mine, [{"title": "文本卡", "parent": other, "content": ""}])
    assert created == [], f"越权项必须不建，实 created={created}"
    assert len(denied) == 1 and "越权" in denied[0]["reason"]
    after = len(card_store.iter_card_metas(wid))
    assert after == before, "越权必须零落盘"


async def test_text_channel_allows_own_subtree(work, sandbox):
    """文本块在子树内 ⇒ 照旧建卡（兜底通道不被砍）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    created, denied = await card_proposal.apply_proposals(
        wid, cur, [{"title": "文本建卡", "parent": "", "content": "x"}])
    assert len(created) == 1 and created[0]["parent"] == cur
    assert denied == []


async def test_cross_channel_dedup_same_title(work, sandbox):
    """同轮工具通道已建同名 ⇒ 文本块跳过（防一次回复建两遍）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    created, denied = await card_proposal.apply_proposals(
        wid, cur, [{"title": "同名卡", "parent": "", "content": "x"}],
        exclude_titles={"同名卡"},
    )
    assert created == []
    assert len(denied) == 1 and "去重" in denied[0]["reason"]


async def test_apply_proposals_returns_tuple(work):
    """破坏性变更的显式钉子：返回 (created, denied) 二元组。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    out = await card_proposal.apply_proposals(wid, cur, [])
    assert isinstance(out, tuple) and len(out) == 2
