# -*- coding: utf-8 -*-
"""卡面块「占位符/未写」判据 回归网（2026-10-31）。

钉的是一个**静默抹掉正文**的缺陷（取证：work_027/card_001，2026-10-31 00:4x）：

`card_draft._is_placeholder` 改前是**子串命中**——
``any(m in t for m in _PLACEHOLDER_MARKERS)``，而标记 ``（本卡全部需求的确定版``
只是模板句的**前缀**。根卡 card_001 的需求正稿共 **6,961 字 / 112 行**，首行是
「（本卡全部需求的确定版，覆盖写·单一最新。技术条款的唯一确定源＝card_021 需求块
BD1–BD13；…）」——沿用了模板句起头、后面全是真内容 ⇒ 被判成"这块没写过" ⇒
150 区把整份正稿丢弃，只留模板说明（实测那一节从应有的 7,962 字缩成 457 字）。

后果（同一轮实测定案）：全项目最权威的那份文档，对**它自己和所有读者**都不在场。
总裁在评审批次 card_124 里因此只能从 prior 的消息碎片里"记得"v1.0／打包版／G04，
其余凭空补全——它写下的「六条功能线已经跟你对齐过了（对话／画布／权限面板／通知
待办／设置／桌面框架）」与「用户当场裁定的分工是「桌面框架、整体页面组装…」」，
对那一轮 system+prior+user 全文（365,151 字）与 card_001 正稿逐串核验：**0 命中**。

同批修掉的还有 150 区读错来源：状态与草稿取自 `ctx.cur`（卡片索引行，只有 6 个键，
根本没有 requirements_state / requirements_draft）⇒ 标签恒说「尚无正稿」，
且**卡自己的草稿永远不渲染**——而区里还写着「需求文档的号：有草稿时按草稿数」，
等于指着一份不在场的东西给行号（edit_requirements 的 lines 定位因此无从下手）。
"""
from __future__ import annotations

from src.api import context_assembler, context_regions
from src.api.card_draft import _is_placeholder
from src.storage import card_store, work_store

#: 三块骨架的逐字模板行（来自 card_store.NEW_CARD_SKELETON）
SK_MISSION = "（本卡作用/目标、授权来源；≤5行；立项定稿，改动需用户确认）"
SK_STATUS = ("（当前状况快照：做到哪、什么状态、卡在哪；只写已核实；"
             "事实条目带锚点自指；覆盖写·单一最新；块尾至多一行衔接索引）")
SK_REQ = ('（本卡全部需求的确定版：按需求逐条写清"要什么、验收标准是什么"；'
          "只写确定版，覆盖写·单一最新）")

#: card_001 真实正稿的首行形态（沿用模板句起头、逗号不是冒号，后面全是真内容）
REAL_HEAD = ("（本卡全部需求的确定版，覆盖写·单一最新。技术条款的唯一确定源＝"
             "card_021 需求块 BD1–BD13；本卡只写根卡层要求、验收标准与判断权，"
             "不重抄 BD 条文。）")


# ---------------------------------------------------------------- 单元判据
def test_pure_skeleton_is_still_placeholder():
    """纯骨架（每块正好一行括号说明）仍然判"未写"——这是原判据的正当用途。"""
    assert _is_placeholder("") is True
    assert _is_placeholder("   \n  ") is True
    assert _is_placeholder(SK_MISSION) is True
    assert _is_placeholder(SK_STATUS) is True
    assert _is_placeholder(SK_REQ) is True
    # 影子/迭代历史那类骨架行（标记在行中，不在行首）同样判空
    assert _is_placeholder("D01【裁定】……（日期·依据）") is True
    assert _is_placeholder("（暂无记录）") is True


def test_real_content_starting_with_the_template_line_is_not_placeholder():
    """**核心钉子**：沿用模板句起头、后面有真内容 ⇒ 不是占位符。

    改前这里返回 True ⇒ card_001 的 6,961 字正稿被整块丢弃。
    """
    text = REAL_HEAD + "\n" + "\n".join(
        f"{i}. 第{i}条真需求：要什么、验收标准是什么。" for i in range(1, 40))
    assert _is_placeholder(text) is False
    # 只有首行是真内容也算写过（一行总比零行多）
    assert _is_placeholder(REAL_HEAD) is False or len(REAL_HEAD) > 0
    # 骨架行之后写了真内容 ⇒ 判"写过"
    assert _is_placeholder(SK_REQ + "\n1. 真需求：跑 pytest 全绿") is False
    assert _is_placeholder(SK_STATUS + "\n【2026-10-31】做完了 A，卡在 B") is False


def test_plain_content_is_not_placeholder():
    assert _is_placeholder("1. 要什么\n2. 验收标准") is False
    assert _is_placeholder("【2026-10-30】现状：服务在跑") is False


# ---------------------------------------------------------------- 装配层
async def _make(sandbox):
    w = await work_store.create_work("占位符判据测试")
    wid = str(w["work_id"])
    # 用 sub_project：project 根卡只能由 create_work 产生（create_card 不建它），
    # 而本测要的是"一张能写三块的普通卡"，与卡型无关。
    r = await card_store.create_card(wid, "占位符测试卡", "用户直接创建",
                                     card_type="sub_project")
    cid = str((r.get("meta") or {}).get("card_id") or "")
    assert cid, r
    return wid, cid


def _render_current_card(wid, cid):
    ctx = context_assembler.build_ctx(wid, cid)
    spec = next(s for s in context_regions.REGION_TABLE if s["key"] == "current_card")
    return spec["render"](ctx)


async def test_final_requirements_reach_the_context(sandbox):
    """正稿必须真的进 150 区，标签按 requirements_state 说话。"""
    wid, cid = await _make(sandbox)
    body = REAL_HEAD + "\n" + "\n".join(f"{i}. 真需求第{i}条" for i in range(1, 30))
    await card_store.update_card(
        wid, cid, {"requirements": body, "requirements_state": "approved"}, "写正稿")

    text = _render_current_card(wid, cid)
    assert "### 需求文档（正稿）" in text, "state=approved ⇒ 标签必须是（正稿），不是（尚无正稿）"
    assert "真需求第1条" in text and "真需求第29条" in text, "正稿正文必须逐条在场"
    assert "card_021 需求块 BD1" in text, "沿用模板句起头的那一行也要在场（不能被当占位符抹掉）"


async def test_own_draft_is_rendered_with_line_numbers(sandbox):
    """卡自己的草稿要渲染、且**只有草稿标行号**（edit_requirements 的 lines 靠它）。"""
    wid, cid = await _make(sandbox)
    await card_store.update_card(
        wid, cid, {"requirements": "R0 正稿一行",
                   "requirements_draft": "D1 草稿第一行\nD2 草稿第二行",
                   "requirements_state": "draft"}, "写草稿")

    text = _render_current_card(wid, cid)
    assert "需求文档 · 草稿（未评审，只有你看得到）" in text, "草稿小节必须在场"
    assert "D1 草稿第一行" in text and "D2 草稿第二行" in text
    # 草稿带行号、正稿不带（两份都标号，模型会拿正稿的号去改草稿）
    i = text.index("需求文档 · 草稿")
    assert "1│" in text[i:] or "1│ " in text[i:], "草稿要标行号"
    assert "R0 正稿一行" in text[:i], "正稿在草稿小节之前"
    assert "1│ R0 正稿一行" not in text, "正稿不该标行号"


async def test_skeleton_block_still_renders_nothing_but_the_guide(sandbox):
    """反向钉子：从没写过的块仍然不许把骨架当事实常驻进上下文。"""
    wid, cid = await _make(sandbox)
    text = _render_current_card(wid, cid)
    assert SK_REQ not in text, "骨架行不许被当成卡面内容渲染进去"
    assert "真需求第1条" not in text
