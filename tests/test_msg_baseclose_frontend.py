# -*- coding: utf-8 -*-
"""会话渲染单源化（2026-10-15）前端改动的机械守卫。

前端没有 pytest 单测载体（DOM 行为归 jsdom 取证脚本
`_evidence_singlesource_20261015.mjs`，装配器纯逻辑归 `tests/assembler.test.mjs`），
本文件只做**源码级机械断言**，锁住本次重构的关键契约、防回归：

- 裁定 C4：旧缝合族（游标计数／插入锚点／思考归位回扫／消息跳过表／组封存）
  必须**在本仓库彻底不存在**——不是注释掉，故此处按符号名 grep 断言＝0；
- 裁定 C2：节点位置由数据自带。渲染层不得再出现「数 DOM 定位置」的写法；
- 数据契约：reasoning 的 model 取**落盘记录顶层**（event_log 七键），
  轮次锚取 run_id（顶层 round 不是对话轮次，见 assembler.js 文件头）；
- 件2 遗留：老卡四类环节块（缺结构化字段、只有 content）仍走降级渲染。

【2026-10-15 改写说明】本文件原有一条用例守护的是被裁��� C4 删净的旧缝合写法
（要求 stream.js 里存在思考归位 helper 与按轮定位变量），与新裁定直接冲突，
已按新契约重写为下面的等价强度守卫。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STREAM = ROOT / "static" / "stream.js"
ASSEMBLER = ROOT / "static" / "esm" / "assembler.js"
STYLE = ROOT / "static" / "style.css"

# 裁定 C4 删净清单（任务书红线6 原样）
DEAD_SYMBOLS = [
    "_insertAnchor",
    "_countUpToUser",
    "nextAssistant",
    "attachReplayReasoning",
    "MSG[t]",
    "beginGroup",
]


def _strip_js_comments(src: str) -> str:
    """去掉 /* */ 与 // 注释：源码级断言不能被注释里的旧词条带偏。"""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def test_dead_stitching_symbols_absent():
    """红线6：缝合族符号在 stream.js 里一个都不许剩（含注释）。"""
    src = STREAM.read_text(encoding="utf-8")
    left = [s for s in DEAD_SYMBOLS if s in src]
    assert not left, f"缝合族符号残留：{left}"


def test_no_dom_position_math():
    """裁定 C2：渲染层不得再「扫已有消息元素」来定位置。

    判据只锁**扫消息**这一种写法（旧实现的游标/锚点都长这样）：
    按 .msg 元素集合数位置、或就地接管那条 live 气泡。
    组内 insertBefore(思考块, 正文容器) 这类**元素引用**插入不算——它用的是
    本函数刚建出来的引用，不是 DOM 现状推算。
    """
    src = _strip_js_comments(STREAM.read_text(encoding="utf-8"))
    for pattern in (r"querySelectorAll\(\s*[\"']\.msg", r"querySelectorAll\(\s*[\"']:scope > \.msg[^\"']*\.msg\b",
                    r"querySelector\(\s*[\"']\.msg\.assistant\.live", r"_countUpTo", r"insertAnchor"):
        assert not re.search(pattern, src), f"渲染层仍存在 DOM 定位写法：{pattern}"
    # 位置只能来自快照：扁平节点表按 order 落位（[对话流照抄 dsh v3] 轮组容器已退役）
    assert "renderSnapshot" in src and "renderAffected" in src, "缺快照/增量两个入口"
    assert "v.render(win, revGetter(snap))" in src, "全序落位必须读快照 order"
    assert "renderGroup" not in src, "★ 轮组渲染必须已退役（改扁平节点表）"


def test_reasoning_reads_top_level_model():
    """数据契约：reasoning 的 model 取落盘记录顶层（event_log 七键），非 data.model。"""
    asm = ASSEMBLER.read_text(encoding="utf-8")
    assert re.search(r"rec\.model", asm), "reasoning 分派必须读落盘记录顶层 rec.model"
    # 落盘 model 只在 reasoning 带，其余为空串——故 model 为空才并进 assistant 思考区
    assert re.search(r"if\s*\(model\)\s*\{", asm), "带 model 的 reasoning 必须先走席卡分支"
    assert "pendingReasoning" in asm, "该轮尚无 assistant 时思考必须挂 pending（不得丢弃）"


def test_run_id_is_carried_not_grouped():
    """[对话流照抄 dsh v3 2026-10-17] 轮次锚：**不再分组**，但 run_id 必须留在节点上。

    旧断言锁的是「分组键 run_id 优先、缺失回落 round」。v3 任务书 §三 把轮分组
    整层退役（改扁平节点表）⇒ 分组键不存在了；但「顶层 round 不是对话轮次、
    真实轮锚是 run_id」这条**数据事实**没变，故改为锁「run_id 进节点字段」。
    """
    asm = ASSEMBLER.read_text(encoding="utf-8")
    assert re.search(r"run_id:\s*runId", asm), "节点必须带 run_id 字段（真实轮锚）"
    assert "groupKeyOf" not in asm and '"r0:"' not in asm, "★ 轮分组键必须已删除"


def test_legacy_content_types_guard():
    """件2 遗留：老卡四类环节块降级集 ＋ 降级文案仍在。"""
    src = STREAM.read_text(encoding="utf-8")
    assert "LEGACY_CONTENT_TYPES" in src
    for t in ("judgment", "plan_created", "step_advanced", "synthesize"):
        assert t in src, f"降级集缺类型 {t}"
    assert "历史记录（旧格式）" in src, "缺降级文案"


def test_unknown_type_fallback_row():
    """裁定 C5：注册表没有的类型必须落 sys 兜底行，不丢不炸。"""
    src = STREAM.read_text(encoding="utf-8")
    assert "未识别事件" in src, "缺未识别事件的兜底文案"
    asm = _strip_js_comments(ASSEMBLER.read_text(encoding="utf-8"))
    assert re.search(r"function kindOf[\s\S]{0,240}?\"sys\"", asm), \
        "kindOf 必须把未注册类型兜底成 sys"
    assert re.search(r"REGISTRY\[type\]", asm), "kindOf 必须查注册表"


def test_single_source_channel():
    """裁定 C1：/session 消息渲染通道已退役，workspace 不得再调 getSession 渲染。"""
    ws_raw = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    body = _strip_js_comments(ws_raw)
    assert "API.getSession" not in body, "workspace 仍在读 /session 渲染消息（第二数据源）"
    # 注意别误伤同族函数 renderChatCrumb（面包屑，与消息渲染无关）
    assert not re.search(r"\bfunction\s+renderChat\s*\(", body), "renderChat 未退役"
    assert not re.search(r"(?<!Crumb)\brenderChat\s*\(\s*\)", body), "仍有 renderChat 调用点"
    assert "EventFlow.resync" in body, "结算回流必须走 timeline"
    assert "EventFlow.replayTrace" in body, "刷新/换卡恢复必须走 timeline"


def test_reasoning_style_present():
    """思考块样式挂在正式回复气泡下（.msg.assistant .think）。"""
    css = STYLE.read_text(encoding="utf-8")
    assert ".msg.assistant .think" in css, "缺 assistant 气泡内的思考块样式"
    # [对话流照抄 dsh v3] 轮组容器 .msgs-round 已退役；靠右由 .flow-user-row 的
    # align-items: flex-end 承担（不再依赖「父级是 flex 列」这条隐式不变量）。
    assert ".flow-user-row" in css and "align-items: flex-end" in css, "用户气泡靠右缺样式"
