# -*- coding: utf-8 -*-
"""消息底座收尾（2026-10-14）件1/件2 前端改动的机械守卫。

前端没有 pytest 单测载体（DOM 行为归 jsdom 取证脚本 `_e2e_replay_harness2.mjs`），
本件只做**源码级机械断言**，锁住两件的关键契约、防回归：

- 件1：`replayTrace` 的 reasoning 分支按**落盘记录顶层 model** 分派（不是 data.model），
  主模型思考走 `attachReplayReasoning`（挂进正式回复气泡），不新建 `.live` 气泡；
- 件2：`renderEvent` 对老卡四类环节块（缺结构化字段、只有 content）降级渲染。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STREAM = ROOT / "static" / "stream.js"
STYLE = ROOT / "static" / "style.css"


def test_replay_reasoning_reads_top_level_model():
    """件1：model 在落盘记录**顶层**（event_log 七键），分派必须读 rec.model。"""
    src = STREAM.read_text(encoding="utf-8")
    assert re.search(r"rec\.model\s*\|\|\s*d\.model", src), \
        "reasoning 分派必须读落盘记录顶层 model（rec.model）"
    assert "attachReplayReasoning(" in src, "缺件1 的挂载 helper"
    assert "replies[replyIdx]" in src, "缺件1 的按轮归位定位"


def test_legacy_content_types_guard():
    """件2：老卡四类环节块降级集 + 降级文案。"""
    src = STREAM.read_text(encoding="utf-8")
    assert "LEGACY_CONTENT_TYPES" in src
    for t in ("judgment", "plan_created", "step_advanced", "synthesize"):
        assert t in src, f"降级集缺类型 {t}"
    assert "历史记录（旧格式）" in src, "缺件2 降级文案"


def test_reasoning_style_broadened_to_reply_bubble():
    """件1：弱化区样式从 .live 放宽到 .msg.assistant（正式回复气泡共用）。"""
    css = STYLE.read_text(encoding="utf-8")
    assert ".msg.assistant .live-reasoning" in css
    assert ".msg.assistant.live .live-reasoning" not in css
