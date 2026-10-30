"""元提示词事实校验。

判据分两类：

- **硬断言**（现在就必须成立）：错误码登记齐全、无幽灵工具、三条线声明完整。
- **待补清单**（`PENDING_*`，只能减不能增）：提示词还没写到的错误码／消息类型。
  新加一种错误码或消息类型而提示词不写 ⇒ 红。

改前实测的 8 类不一致，全部登记在 `PENDING_*` 里，逐条消灭。
"""

from __future__ import annotations

import glob
import re
from pathlib import Path

from src.api import card_msg, card_tools, prompt_facts, prompts

_ROOT = Path(__file__).resolve().parents[1]

#: 提示词还没写到的错误码（只能减）。
PENDING_CODES: frozenset[str] = frozenset({"invalid", "ambiguous"})

#: 提示词还没写到的消息类型（只能减）。
#: [通信收拢 2026-10-28] 四种消息类型已写进 `_SEC_CARDMSG`（按类型给了应对），
#: 从待补清单清空。空集＝以后新增类型必须同时写提示词，否则这条会红。
PENDING_MSG_TYPES: frozenset[str] = frozenset()

#: 提示词里提到、但注册表里没有的工具（幽灵，只能减）。
PENDING_GHOSTS: frozenset[str] = frozenset({"run_tool_card"})


def _codes_in_source() -> set[str]:
    out: set[str] = set()
    for p in glob.glob(str(_ROOT / "src" / "**" / "*.py"), recursive=True):
        s = Path(p).read_text(encoding="utf-8", errors="replace")
        out |= set(re.findall(r'\berr\(\s*"([a-z_]+)"', s))
    return out


def _both_prompts() -> str:
    return prompts.BASE_PROMPT + "\n" + prompts.AUTO_BASE_PROMPT


# ---------------------------------------------------------------- 硬断言
def test_every_error_code_is_declared():
    """代码里 `err("...")` 用到的每个 code，必须在 `ERROR_CODES` 里。

    新增一个 code 不改这张表 ⇒ 红。这张表是提示词与模型的唯一说明源。
    """
    undeclared = _codes_in_source() - set(prompt_facts.ERROR_CODES)
    assert not undeclared, (
        "这些错误码代码在用、事实源没声明（请加进 prompt_facts.ERROR_CODES）：\n"
        + "\n".join(f"  - {c}" for c in sorted(undeclared))
    )


def test_error_codes_have_advice():
    """每个 code 都要写「收到它该怎么办」，不能只列名字。"""
    for code, advice in prompt_facts.ERROR_CODES.items():
        assert len(str(advice).strip()) >= 8, f"{code} 缺处置说明"


def test_no_new_ghost_tool():
    """提示词里提到的工具名，注册表里必须真有。"""
    txt = _both_prompts()
    reg = set(card_tools.TOOL_REGISTRY)
    cand = {n for n in re.findall(r"\b([a-z][a-z_0-9]{3,})\(", txt) if "_" in n}
    ghosts = cand - reg - set(PENDING_GHOSTS)
    assert not ghosts, (
        "提示词提到但注册表没有（幽灵工具）：\n"
        + "\n".join(f"  - {n}" for n in sorted(ghosts))
    )


def test_pending_ghosts_still_ghosts():
    """PENDING_GHOSTS 里不许留已经修好的（否则表会烂掉）。"""
    txt = _both_prompts()
    reg = set(card_tools.TOOL_REGISTRY)
    cand = {n for n in re.findall(r"\b([a-z][a-z_0-9]{3,})\(", txt) if "_" in n}
    fixed = set(PENDING_GHOSTS) & reg
    gone = set(PENDING_GHOSTS) - cand
    assert not fixed, f"这些已经在注册表里了，从 PENDING_GHOSTS 删掉：{sorted(fixed)}"
    assert not gone, f"这些提示词里已经不提了，从 PENDING_GHOSTS 删掉：{sorted(gone)}"


def test_three_lines_declared():
    """三条线必须各有「管什么」和「不管什么」——只写管什么，混用就是这么来的。"""
    keys = {ln.key for ln in prompt_facts.LINES}
    assert keys == {"parent", "links", "relations"}, keys
    for ln in prompt_facts.LINES:
        assert len(ln.duty) >= 10, f"{ln.key} 缺职责"
        assert len(ln.cannot) >= 10, f"{ln.key} 缺「不管什么」"


def test_blocks_come_from_source():
    """三块必须从 `hash_utils.NEW_SECTION_MAPPING` 派生，不许另写一份名字。"""
    got = {b.key for b in prompt_facts.blocks()}
    assert got == {"mission", "status_block", "requirements"}, got


# ---------------------------------------------------------------- 待补清单
def test_prompt_covers_all_error_codes():
    """提示词要写到每个错误码——模型收到 `code=invalid` 却不知道怎么办。"""
    txt = _both_prompts()
    missing = {c for c in prompt_facts.ERROR_CODES if c not in txt}
    new = missing - set(PENDING_CODES)
    assert not new, f"提示词漏了这些错误码（新增的必须写）：{sorted(new)}"


def test_pending_codes_still_missing():
    txt = _both_prompts()
    fixed = {c for c in PENDING_CODES if c in txt}
    assert not fixed, f"这些错误码提示词已经写了，从 PENDING_CODES 删掉：{sorted(fixed)}"


def test_prompt_covers_all_msg_types():
    """提示词要写到每种消息类型——模型收到 `msg_sync` 却不知道该怎么应对。"""
    txt = _both_prompts()
    missing = {c for c, _ in prompt_facts.msg_types() if c not in txt}
    new = missing - set(PENDING_MSG_TYPES)
    assert not new, f"提示词漏了这些消息类型（新增的必须写）：{sorted(new)}"


def test_pending_msg_types_still_missing():
    txt = _both_prompts()
    fixed = {c for c in PENDING_MSG_TYPES if c in txt}
    assert not fixed, f"这些消息类型提示词已经写了，从 PENDING_MSG_TYPES 删掉：{sorted(fixed)}"


# ---------------------------------------------------------------- 事实源自洽
def test_msg_types_and_card_types_nonempty():
    assert prompt_facts.msg_types(), "消息类型注册表读不到"
    assert len(prompt_facts.card_types()) >= 5, "卡类型注册表读不到"
    assert prompt_facts.functions(), "职能目录读不到"


def test_renderers_produce_text():
    for fn in (prompt_facts.render_lines, prompt_facts.render_blocks,
               prompt_facts.render_error_codes, prompt_facts.render_msg_types,
               prompt_facts.render_functions):
        t = fn()
        assert len(t) >= 40, f"{fn.__name__} 渲染为空"
