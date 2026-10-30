# -*- coding: utf-8 -*-
"""消息类型「前后端必须齐」的机械约束。

[2026-10-26 · 用户令「你他妈不会遵守的」] 靠自觉没用，靠测试卡住。

背景（今天的四个遗漏，全是"改了产生端、没查消费端"）：
  · 加 `msg_sync` 但 `assembler.js` 的 REGISTRY 不认 ⇒ 落 `sys` 兜底，样式全丢
  · 加 `msg_report` 但 `CARD_MSG_DISPLAY` 只有 3 型 ⇒ 显示成原始类型码
  · 改落点但 `_block_event_type` 对 human 直接返回 user_message ⇒ 冒充用户消息

本文件把「消息类型」这条链路的消费方写成断言：**加类型必须一次补齐**，
漏一处测试就红，不依赖谁记得。
"""
from __future__ import annotations

import json
import os
import re

import pytest

from src.api import card_msg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 后端注册表 → 前端四处映射 + 事件登记表，全部必须覆盖
FRONTEND_CONSUMERS = [
    ("static/esm/assembler.js", "REGISTRY"),
    ("static/esm/flow/card-msg-node.js", "CARD_MSG_DISPLAY"),
    ("static/stream.js", "CARD_MSG_DISPLAY"),
    ("static/workspace.js", "CARD_MSG_DISPLAY"),
]


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_every_msg_type_reaches_every_consumer():
    """每个消息类型都必须在：前端 4 处映射 + 事件登记表 里出现。"""
    codes = list(card_msg.type_codes())
    assert codes, "消息类型注册表为空"

    missing: list[str] = []
    for rel, _sym in FRONTEND_CONSUMERS:
        text = _read(rel)
        for c in codes:
            if not re.search(rf"\b{re.escape(c)}\b", text):
                missing.append(f"{rel} 缺 {c}")

    catalog = json.loads(_read("config/event_catalog.json"))["types"]
    for c in codes:
        if c not in catalog:
            missing.append(f"config/event_catalog.json 缺 {c}")

    assert not missing, (
        "新增消息类型必须一次补齐所有消费方（漏一处就会样式错乱/掉兜底）：\n  "
        + "\n  ".join(missing))


def test_frontend_maps_every_type_to_cardmsg_kind():
    """assembler.js 的 REGISTRY 必须把每个消息类型映射成 cardmsg（不能落 sys 兜底）。"""
    text = _read("static/esm/assembler.js")
    missing = [c for c in card_msg.type_codes()
               if not re.search(rf"\b{re.escape(c)}\s*:\s*\{{\s*kind:\s*\"cardmsg\"", text)]
    assert not missing, f"assembler.js 未映射成 cardmsg: {missing}"


def test_msg_type_registry_shape():
    """注册表每个类型必须五键齐全（display/note/when_to_use/reply_expected/reply_type）。"""
    data = json.loads(_read("config/message_type_registry.json"))
    bad = []
    for code, entry in data["types"].items():
        if not isinstance(entry, dict):
            bad.append(f"{code}: 不是对象")
            continue
        for k in ("display", "note", "when_to_use", "reply_expected", "reply_type"):
            if k not in entry:
                bad.append(f"{code}: 缺 {k}")
        if not str(entry.get("display") or "").strip():
            bad.append(f"{code}: display 为空")
    assert not bad, "消息类型条目不完整：\n  " + "\n  ".join(bad)
