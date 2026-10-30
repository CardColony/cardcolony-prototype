# -*- coding: utf-8 -*-
"""[2026-10-25 用户令] 根卡（总裁）在自动模式下要能编辑自己的需求文档块。

事故链：根卡自动模式工具面里 **`edit_requirements` 与 `edit_card` 都没有**——
`edit_card` 被 `_apply_auto_toolface` 按设计摘掉（auto 卡走分区写），
`edit_requirements` 被总裁目录条目的 `toolface.deny` 摘掉 ⇒ 根卡**没有任何
写第三块的通道**。

而目录自己的契约（`_meta.role_model.写权阶梯`）写着「**总裁写全树引导块
（需求文档/裁定）**」——所以 deny 掉 edit_requirements 是**实现与契约矛盾**，
不是有意限制。

修法：从 `config/function_catalog.json` 的 ceo 条目 deny 里移除
`edit_requirements`（目录是工具面的唯一事实源，不在代码里特判）。

本文件直接读**仓库里的配置文件**（不读运行目录）：测的就是那份契约。
"""
from __future__ import annotations

import json
import pathlib

_CATALOG = pathlib.Path(__file__).resolve().parents[1] / "config" / "function_catalog.json"


def _cfg() -> dict:
    return json.loads(_CATALOG.read_text(encoding="utf-8"))


def _ceo_toolface() -> dict:
    for e in _cfg().get("functions") or []:
        if str(e.get("code")) == "ceo":
            return e.get("toolface") or {}
    raise AssertionError("config/function_catalog.json 里应有 ceo 条目")


def test_ceo_deny_does_not_block_edit_requirements():
    """根卡写第三块的通道不许被 deny 掉（否则自动模式根卡写不了需求文档）。"""
    deny = set(_ceo_toolface().get("deny") or [])
    assert "edit_requirements" not in deny, (
        f"ceo 的 deny 不应包含 edit_requirements"
        f"（根卡自动模式要能编辑自己的需求文档块）：{sorted(deny)}"
    )


def test_ceo_deny_keeps_other_limits():
    """总裁制的其余限制没被误删：发信仍被 deny。"""
    deny = set(_ceo_toolface().get("deny") or [])
    # [通信收拢 2026-10-28] send_card_message 已从注册表退役，deny 列表随之清空。
    from src.api import card_tools as _ct
    assert "send_card_message" not in _ct.TOOL_REGISTRY, sorted(deny)


def test_ceo_allow_unchanged():
    """allow 清单未被本次改动波及（总裁出口仍是那五件）。"""
    allow = set(_ceo_toolface().get("allow") or [])
    # [通信收拢 2026-10-28] issue_directive 已退役（并进 direct）。
    for name in ("curate_face", "direct", "confirm_with_user",
                 "report_to_user", "write_ledger"):
        assert name in allow, f"ceo 的 allow 缺 {name}：{sorted(allow)}"


def test_catalog_contract_says_ceo_writes_requirements():
    """契约自证：role_model 的写权阶梯写明总裁写需求文档——deny 它是矛盾。"""
    rm = (_cfg().get("_meta") or {}).get("role_model") or {}
    ladder = str(rm.get("写权阶梯") or "")
    assert "总裁写全树引导块" in ladder, f"契约文本变了，需重新核对 deny 清单：{ladder[:80]}"
