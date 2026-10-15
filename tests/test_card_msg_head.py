# -*- coding: utf-8 -*-
"""卡间消息「头」· 回归网（2026-10-14）。

用户 23:25 分工：正文分段（任务本身／是哪张卡／要你干嘛）由**功能线**承载；
**头由上下文侧管**——两件事：

1. **role 必须合法**：`actor` 以 `card:` 开头的块，投影 role ＝ `user`
   （原实现走 `.get(actor, actor)` 兜底 ⇒ 字面 `card:card_002`
   ⇒ 端点只认 system/user/assistant/tool ⇒ 一进请求即 400）；
2. **机器头行不进模型视野**：投影时 `[[card-msg …]]` 换成固定头 `〔卡间消息〕`，
   且**块本身不改**（`find_pending` 仍读原始头行——块是真相源）。

铁律一：沙箱内跑，不碰真实 works/。
"""
from __future__ import annotations

from src.api import card_msg
from src.storage import card_store, session_facade, work_store

_RAW = ("[[card-msg id=card_001-abc12345 type=msg_ask from=card_001 reply_to=]]\n"
        "数字是多少？")


async def _make_work():
    wdata = await work_store.create_work("卡间消息头剧本")
    wid = wdata["work_id"]
    await card_store.create_card(wid, "根卡标题", "用户直接创建")
    return wid, "card_001"


# ---------------------------------------------------------------------------
# V1：纯函数——机器头 → 固定头（头不承担归属）
# ---------------------------------------------------------------------------


def test_header_replaced_by_fixed_head():
    out = card_msg.render_for_context(_RAW)
    assert out == "〔卡间消息〕\n数字是多少？"
    assert "[[card-msg" not in out
    assert "card_001" not in out, "头不承担归属：不得带出任何卡号"


def test_no_header_unchanged():
    assert card_msg.render_for_context("普通正文") == "普通正文"
    assert card_msg.render_for_context("") == ""


def test_find_pending_still_reads_raw_header():
    """待处理推导读的是块（原始头行），不受投影换头影响。"""
    blocks = [{"actor": "card:card_001", "content": _RAW}]
    assert card_msg.find_pending(blocks) is not None


# ---------------------------------------------------------------------------
# V2：落盘→读回，role 合法（user），且块本身仍是原始头行
# ---------------------------------------------------------------------------


async def test_actor_card_maps_to_user_role(sandbox):
    wid, cid = await _make_work()
    await session_facade.append(wid, cid, {"actor": f"card:{cid}", "content": _RAW,
                                           "behavior": "", "end": ""})
    blocks = await session_facade.read(wid, cid)
    cm = [b for b in blocks if str(b.get("actor", "")).startswith("card:")]
    assert cm, "卡间块应已落盘"
    for b in cm:
        assert b["role"] == "user", "card: actor 必须映射为合法 user 角色"
        assert b["role"] != f"card:{cid}"
    # 块本身不改：原始头行仍在（find_pending 的锚）
    assert "[[card-msg" in cm[0]["content"]
    # 投影才换头
    assert card_msg.render_for_context(cm[0]["content"]).startswith("〔卡间消息〕")


async def test_projection_does_not_touch_block(sandbox):
    """投影只改模型视野；块落盘内容逐字不变。"""
    wid, cid = await _make_work()
    await session_facade.append(wid, cid, {"actor": f"card:{cid}", "content": _RAW,
                                           "behavior": "", "end": ""})
    blocks = await session_facade.read(wid, cid)
    cm = next(b for b in blocks if str(b.get("actor", "")).startswith("card:"))
    before = cm["content"]
    card_msg.render_for_context(before)          # 投影（不改块）
    after = await session_facade.read(wid, cid)
    cm2 = next(b for b in after if str(b.get("actor", "")).startswith("card:"))
    assert cm2["content"] == before
