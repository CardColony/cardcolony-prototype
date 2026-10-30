# -*- coding: utf-8 -*-
"""投递即消费（增量投递）——2026-10-23 用户令的回归钉。

用户原话：「逻辑应该是增量逻辑，读过的信息就从 box 里清掉，存起来，让它不必再读到」

**故障（实测 work_025）**：投递路径（`deliver_now` / K3）把 pending 消息全文拼进
prompt，但**不推读取水位**；而水位只在模型主动调 `read_inbox(scope="unread")` 时
才推进（全仓唯一调用点）。模型在 prompt 里已经看到消息，没有理由再调 read_inbox
⇒ 水位永远不动 ⇒ 同一批被反复投递：
  · 299 条永久 pending；17 张卡水位=0（从没调过 read_inbox）
  · card_029 一次投递 46 条 / 82,720 字符 = prompt 的 98%
  · 模型自己在会话里写「已是前一轮归档过的内容，无需重复处理」

**修法**：`card_db.take_pending` —— 读 + 推水位在**同一个写事务**里；
两个投递点改用它。行不删（messages 表永久留档，可回溯）。

判据：
T1 take 一次拿到全部未读，且返回后水位推进
T2 再 take ⇒ 空（同一批不会投第二遍）
T3 期间来新信 ⇒ 只拿新的
T4 行不删：take 之后 list_inbox 仍能查到（存档可回溯）
T5 两个并发 take 不会拿到同一批（原子性）
T6 投递路径真的走 take（静态钉：message_runner 的投递点不许再用 list_pending 拼 prompt）
"""
from __future__ import annotations

import asyncio
import pytest

from src.api import card_tools
from src.storage import card_db


async def _cards(wid: str) -> dict:
    return card_tools._collect_card_meta(wid)


async def _mk_pair(work) -> tuple[str, str, str]:
    """返回 (work_id, A卡, B卡)。"""
    from src.storage import card_store

    wid = work["work_id"]
    root = work["work_card_id"]
    a = await card_store.create_card(wid, "甲", "模型生成", parent=root)
    b = await card_store.create_card(wid, "乙", "模型生成", parent=root)
    aid, bid = a["meta"]["card_id"], b["meta"]["card_id"]
    r = await card_tools.execute_tool(
        wid, aid, "link_cards", {"target_id": bid, "type": "关联"},
        await _cards(wid))
    assert r.get("ok") is True, r
    return wid, aid, bid


async def _send(wid: str, frm: str, to: str, body: str) -> str:
    # [通信收拢 2026-10-28] `send_card_message` 工具已退役（接口只有 sync/direct）。
    # 本文件钉的是**账本原语**（append_inbox / take_pending / 水位），与工具面无关，
    # 故直接落账本，不经过任何模型工具。
    import uuid as _uuid
    await card_db.append_inbox(
        wid, msg_id=f"{frm}-{_uuid.uuid4().hex[:8]}", target_card_id=to,
        from_card=frm, type="msg_notify", content=body)
    rows = await card_db.list_inbox(wid, to)
    return str(rows[-1]["msg_id"])


@pytest.mark.asyncio
async def test_take_gets_all_and_advances(work):
    """T1：take 一次拿到全部未读，返回后水位推进。"""
    wid, a, b = await _mk_pair(work)
    await _send(wid, a, b, "第一封")
    await _send(wid, a, b, "第二封")

    taken = await card_db.take_pending(wid, b)
    assert len(taken) == 2, taken
    bodies = "".join(str(m.get("content") or "") for m in taken)
    assert "第一封" in bodies and "第二封" in bodies

    seen = await card_db.get_seen_seq(wid, b)
    assert seen >= max(int(m["seq"]) for m in taken), "take 之后水位必须越过本批"


@pytest.mark.asyncio
async def test_second_take_returns_nothing(work):
    """T2：同一批不会投第二遍——这是用户要的「不必再读到」。"""
    wid, a, b = await _mk_pair(work)
    await _send(wid, a, b, "只有一封")
    assert len(await card_db.take_pending(wid, b)) == 1
    assert await card_db.take_pending(wid, b) == [], "读过的不能再投一遍"


@pytest.mark.asyncio
async def test_take_only_new_after_more_arrive(work):
    """T3：期间来新信 ⇒ 只拿新的。"""
    wid, a, b = await _mk_pair(work)
    await _send(wid, a, b, "旧信")
    await card_db.take_pending(wid, b)
    await _send(wid, a, b, "新信")
    taken = await card_db.take_pending(wid, b)
    assert len(taken) == 1, taken
    assert "新信" in str(taken[0].get("content") or "")


@pytest.mark.asyncio
async def test_rows_are_archived_not_deleted(work):
    """T4：行不删——移出收件箱 ≠ 删档，历史仍可回溯。"""
    wid, a, b = await _mk_pair(work)
    mid = await _send(wid, a, b, "留档测试")
    await card_db.take_pending(wid, b)
    rows = await card_db.list_inbox(wid, b)
    assert any(str(r["msg_id"]) == mid for r in rows), "take 之后必须还能查到这条"


@pytest.mark.asyncio
async def test_concurrent_take_does_not_double_deliver(work):
    """T5：并发 take 不会拿到同一批（读与推水位在同一事务）。"""
    wid, a, b = await _mk_pair(work)
    for i in range(3):
        await _send(wid, a, b, f"信{i}")

    r1, r2 = await asyncio.gather(
        card_db.take_pending(wid, b), card_db.take_pending(wid, b))
    ids = {str(m["msg_id"]) for m in r1} & {str(m["msg_id"]) for m in r2}
    assert ids == set(), f"同一批被两个投递各拿一次：{ids}"
    assert len(r1) + len(r2) == 3, (len(r1), len(r2))


def test_delivery_paths_use_take_not_list():
    """T6：投递点不许再「只读不消费」——静态钉住，防回退。

    判定：message_runner 里凡是把 pending 拼成 prompt 之前，必须已经 take 过。
    最简可靠的机械判据：投递函数体内出现 take_pending，且拼 prompt 的调用
    （_build_card_msg_prompt）出现在 take 之后。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "api" / "message_runner.py").read_text(encoding="utf-8")
    code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())

    # deliver_now 与 K3 各有一处投递：take 必须出现两次（两处投递点）
    assert code.count("card_db.take_pending(") >= 2, \
        "两个投递点（deliver_now / K3）都必须用 take_pending"

    # 每次 _build_card_msg_prompt 调用之前都要有 take_pending
    # （排除函数定义行 `async def _build_card_msg_prompt(`）
    build_idx = [i for i in range(len(code))
                 if code.startswith("_build_card_msg_prompt(", i)
                 and not code[max(0, i - 4):i].endswith("def ")]
    take_idx = [i for i in range(len(code))
                if code.startswith("card_db.take_pending(", i)]
    assert len(build_idx) == len(take_idx), (
        f"拼 prompt 调用 {len(build_idx)} 处、take {len(take_idx)} 处——必须一一对应")
    for bi in build_idx:
        assert any(ti < bi for ti in take_idx), \
            "存在「先拼 prompt 后 take」或「只拼不 take」的投递点"
