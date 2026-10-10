# -*- coding: utf-8 -*-
"""板子基建五类判据（迭代二 S1 交付判据）＋ 12:24 版消费点验证。

任务书 S1 验收原文：单测五类全绿——
①正常建板落盘 ②强杀重建（断尾 JSON → 明报，不做产物重放）
③孤儿三态（终态删/卡无删/卡活留，零时间戳）④幂等（重复指针写不撕裂）
⑤并发快照（锁内原子）。
另验：12:24 版两处直接消费（session_ops.ops_log plan_* 动作、
revision_store.record_revision(entity="plan")＋板上 revisions[] 自动记）、
防护①（重启恢复序）、防护⑤（清理后旧引用拒绝）、防护⑥（锁超时明报）、
plan_remove 当前步不可删（结构性拒绝）、置脏异步合并落盘。
零真实 works 触碰（conftest 铁律一）；不 import main（铁律二）。
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from src.storage import card_store, session_store, work_store  # noqa: F401
from src.thinkchain import board
from src.thinkchain.board import (
    BoardBusyError,
    BoardRestoreError,
    create_board,
    current_step_prompt,
    finalize_board,
    get_board,
    has_live_board,
    sweep_orphans,
)


@pytest.fixture(autouse=True)
def _reset_board_registry():
    """清板子模块级注册表——每个沙箱的 work_id 都从 work_001 起，
    残留的 (work_id, card_id) 键会命中上一用例的旧板（指向已删 tmp）。"""
    board._boards.clear()
    board._locks.clear()
    board._background_tasks.clear()
    yield
    board._boards.clear()
    board._locks.clear()
    board._background_tasks.clear()


def _ops_actions() -> list[str]:
    p = session_store.WORKS_DIR / "_ops" / "session_ops.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line)["action"])
    return out


def _steps(*prompts: str) -> list[dict]:
    # [迭代三 S3 同步] type 必填契约落地后构造统一补 execute（类型语义对推进/
    # 改板/清理用例无关紧要；专测类型契约的用例在 test_iter3_type_contract.py）
    return [{"id": f"s{i + 1}", "prompt": p, "type": "execute"}
            for i, p in enumerate(prompts)]


async def _wait_disk(path, pred, tries: int = 300):
    """等置脏合并任务把状态写到盘（修订/产物是异步合并落盘）。"""
    for _ in range(tries):
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if pred(data):
                return data
        await asyncio.sleep(0.01)
    raise AssertionError(f"盘上状态未达预期: {path}")


# ------------------------------------------------------------- ① 正常建板落盘

async def test_create_board_persists_and_logs(work):
    wid, cid = work["work_id"], work["work_card_id"]
    r = await create_board(wid, cid, "三步计划", _steps("第一步", "第二步", "第三步"))
    assert r["ok"] is True, r

    pp = board.plan_path(wid, cid)  # works/{w}/plans/plan_{card}.json
    assert pp.exists() and pp.parent.name == "plans"
    data = json.loads(pp.read_text(encoding="utf-8"))
    assert data["current"] == 0
    assert data["name"] == "三步计划"
    assert data["revisions"] == []
    assert isinstance(data["created_at"], str) and data["created_at"]
    # 三字段＋type（迭代三 S3 契约：helper 补 execute 后落新契约值）
    assert data["steps"][0] == {"id": "s1", "prompt": "第一步",
                                "status": "todo", "type": "execute", "has_artifact": False}
    assert len(data["steps"]) == 3

    assert has_live_board(wid, cid) is True
    assert await current_step_prompt(wid, cid) == "第一步"
    assert "plan_create" in _ops_actions()

    # 重复建板拒绝、不覆盖；结构非法拒绝
    r2 = await create_board(wid, cid, "x", _steps("别的"))
    assert r2["ok"] is False and r2["error"] == "board_exists"
    assert (await create_board(wid, cid, "x", []))["ok"] is False
    dup = await create_board(wid, cid, "x", [{"id": "a", "prompt": "1", "type": "execute"},
                                             {"id": "a", "prompt": "2", "type": "execute"}])
    assert dup["ok"] is False and "重复" in dup["error"]
    nop = await create_board(wid, cid, "x", [{"id": "a", "prompt": "", "type": "execute"}])
    assert nop["ok"] is False


# ----------------------------------------------------- ② 强杀断尾明报 ＋ 防护①恢复序

async def test_truncated_json_reports_clearly(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await create_board(wid, cid, "n", _steps("a", "b"))
    board._boards.clear()  # 模拟重启（内存清空）
    pp = board.plan_path(wid, cid)
    pp.write_text('{"steps": [{"id": "s1", "prom', encoding="utf-8")  # 手工断尾

    with pytest.raises(BoardRestoreError) as ei:
        await get_board(wid, cid)
    assert str(pp) in str(ei.value)  # 明报含路径
    assert await current_step_prompt(wid, cid) is None  # 热路径不炸


async def test_restore_after_restart_keeps_pointer(work):
    """防护①：重启重建——指针同步落盘后，内存清空仍从盘恢复到同一位。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await create_board(wid, cid, "n", _steps("a", "b", "c"))
    b = await get_board(wid, cid)
    await b.mark_done_and_advance("结果一")
    await b.mark_done_and_advance("结果二")

    board._boards.clear()  # 重启
    b2 = await get_board(wid, cid)
    assert b2 is not None
    snap = b2.to_snapshot()
    assert snap["current"] == 2
    assert [s["status"] for s in snap["steps"]] == ["done", "done", "todo"]
    r = await b2.mark_done_and_advance("结果三")  # 恢复后接着推——链路不断
    assert r["advanced"] and r["all_steps_done"]


# ------------------------------------------------------------- ③ 孤儿三态（零时间戳）

async def test_orphan_three_states_no_timestamp(work):
    wid, root_card = work["work_id"], work["work_card_id"]
    rB = await card_store.create_card(wid, "终态卡", "用户直接创建")
    assert "error" not in rB, rB
    card_b = rB["meta"]["card_id"]

    # 三块板：根卡（将打终态）/ 真卡B（卡活无终态）/ 幽灵卡（卡文件不存在）
    assert (await create_board(wid, root_card, "n1", _steps("a")))["ok"]
    assert (await create_board(wid, card_b, "n2", _steps("b")))["ok"]
    ghost = "card_999"
    assert (await create_board(wid, ghost, "n3", _steps("c")))["ok"]

    fr = await finalize_board(wid, root_card)
    assert fr["ok"] is True and fr["card_marked"] is True

    # 零时间戳：把三块板文件 mtime 全改到 2000 年，判定必须不变
    old = 946684800.0
    for c in (root_card, card_b, ghost):
        os.utime(board.plan_path(wid, c), (old, old))

    rep = sweep_orphans(wid)
    assert sorted(rep["removed"]) == sorted([root_card, ghost])
    assert rep["kept"] == [card_b]
    assert not board.plan_path(wid, root_card).exists()   # 查一：终态删
    assert not board.plan_path(wid, ghost).exists()       # 查二：卡无删
    assert board.plan_path(wid, card_b).exists()          # 查三：卡活留


# ------------------------------------------------------------- ④ 幂等（指针不撕裂）

async def test_mark_done_idempotent_no_tear(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await create_board(wid, cid, "n", _steps("a", "b"))
    b = await get_board(wid, cid)

    r1 = await b.mark_done_and_advance("结果一")
    assert r1["advanced"] is True and r1["prompt"] == "b"
    disk = json.loads(board.plan_path(wid, cid).read_text(encoding="utf-8"))
    assert disk["current"] == 1 and disk["steps"][0]["status"] == "done"  # 指针同步落盘

    r2 = await b.mark_done_and_advance("结果二")
    assert r2["advanced"] is True and r2["all_steps_done"] is True

    r3 = await b.mark_done_and_advance("多余")  # 越界再调：接得住，指针不动
    assert r3["advanced"] is False and r3["all_steps_done"] is True
    disk2 = json.loads(board.plan_path(wid, cid).read_text(encoding="utf-8"))
    assert disk2["current"] == 2

    # 同锁重入防御：当前步已 done → 不推进不撕裂，如实返回当前步要求
    b._data["current"] = 0  # 人工构造异常态
    r4 = await b.mark_done_and_advance("重复")
    assert r4["idempotent"] is True and r4["advanced"] is False
    assert r4["prompt"] == "a"
    assert b._data["current"] == 0


# ------------------------------------------------------------- ⑤ 并发快照（锁内原子）

async def test_concurrent_advance_serialized(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await create_board(wid, cid, "n", _steps("a", "b", "c"))
    b = await get_board(wid, cid)

    rs = await asyncio.gather(*(b.mark_done_and_advance(f"r{i}") for i in range(3)))
    assert all(r["advanced"] for r in rs)  # 三次并发各推进一步，锁内串行
    snap = b.to_snapshot()
    assert snap["current"] == 3
    assert all(s["status"] == "done" for s in snap["steps"])
    disk = json.loads(board.plan_path(wid, cid).read_text(encoding="utf-8"))
    assert disk["current"] == 3 and disk["steps"] == snap["steps"]  # 盘＝内存，无半截


# ------------------------------------------------- 12:24 消费点：revision 双留痕 ＋ 置脏异步合并

async def test_apply_revision_dual_ledger_and_dirty_flush(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await create_board(wid, cid, "n", _steps("a", "b", "c"))
    b = await get_board(wid, cid)
    await b.mark_done_and_advance()  # current -> 1 (s2/b)

    r = await b.apply_revision("insert", {"after_id": "s1",
                                          "step": {"id": "s_new", "prompt": "新步", "type": "execute"}})
    assert r["ok"] is True, r
    snap = r["board"]
    assert [s["id"] for s in snap["steps"]] == ["s1", "s_new", "s2", "s3"]
    assert snap["current"] == 2  # s2 的新位（指针按 id 重定位）

    r2 = await b.apply_revision("remove", {"id": "s1"})  # 删非当前步
    assert r2["ok"] and r2["board"]["current"] == 1  # s2 新位（序变 [s_new, s2, s3]）

    # 当前步不可删（结构性拒绝——删了指针没处指）
    assert (await b.apply_revision("remove", {"id": "s2"}))["error"] == "当前步不可删"

    r3 = await b.apply_revision("modify", {"id": "s3", "prompt": "改写后的要求"})
    assert r3["ok"] and r3["board"]["steps"][2]["prompt"] == "改写后的要求"

    r4 = await b.apply_revision("remove", {"id": "s_new"})  # 再删一个非当前步
    assert r4["ok"] and [s["id"] for s in r4["board"]["steps"]] == ["s2", "s3"]

    # 非法改板拒绝
    assert (await b.apply_revision("reorder", {"order": ["s2"]}))["ok"] is False
    assert (await b.apply_revision("remove", {"id": "不存在"}))["ok"] is False
    assert (await b.apply_revision("warp", {}))["ok"] is False

    # 板上 revisions[] 自动记（动作/时间/改前快照/改后快照）
    disk = await _wait_disk(board.plan_path(wid, cid), lambda d: len(d["revisions"]) == 4)
    revs = disk["revisions"]
    assert [rv["action"] for rv in revs] == ["insert", "remove", "modify", "remove"]
    assert all(rv["at"] and rv["before"] and rv["after"] for rv in revs)
    assert revs[0]["after"][1]["id"] == "s_new"

    # revision_store.record_revision(entity="plan") 流水逐行核对
    revp = session_store.WORKS_DIR / wid / "_ops" / "revisions.jsonl"
    lines = [json.loads(l) for l in revp.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [l["entity"] for l in lines] == ["plan"] * 4
    assert [l["change"] for l in lines] == ["insert", "remove", "modify", "remove"]
    assert "plan_revise" in _ops_actions()


# ------------------------------------------------- 终态收口 ＋ 防护⑤清理后拒绝复活

async def test_cleanup_then_stale_reference_rejected(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await create_board(wid, cid, "n", _steps("a"))
    b = await get_board(wid, cid)

    cr = await b.cleanup_board()
    assert cr["ok"] is True, cr
    assert has_live_board(wid, cid) is False
    assert await get_board(wid, cid) is None
    assert not board.plan_path(wid, cid).exists()  # plan 文件已删
    assert "plan_cleanup" in _ops_actions()

    with pytest.raises(BoardRestoreError):  # 旧引用再操作：明报拒绝，绝不复活
        await b.mark_done_and_advance("x")

    assert (await b.cleanup_board())["ok"] is True  # 重复清理：fail-open 幂等


# ------------------------------------------------- 防护⑥：锁超时明报

async def test_lock_timeout_raises_board_busy(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await create_board(wid, cid, "n", _steps("a"))
    b = await get_board(wid, cid)
    lock = board._get_lock((wid, cid))
    await lock.acquire()  # 人工占锁不放
    try:
        with pytest.raises(BoardBusyError):
            await b.mark_done_and_advance("x")  # 锁超时明报，不静默等待
    finally:
        lock.release()


# ------------------------------------------------- 产物异步落库（thinkchain 白名单目录）

async def test_save_artifact_async_lands_file(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await create_board(wid, cid, "n", _steps("a", "b"))
    b = await get_board(wid, cid)

    ret = await b.save_artifact_async("s1", "本步产物正文")
    assert ret["ok"] is True and ret["scheduled"] is True
    path = board.artifacts_dir(wid, cid) / "01-s1.md"  # thinkchain/{card}/01-s1.md

    for _ in range(300):
        if path.exists():
            break
        await asyncio.sleep(0.01)
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "本步产物正文"
    # has_artifact 置位置脏合并回板
    data = await _wait_disk(board.plan_path(wid, cid),
                            lambda d: d["steps"][0]["has_artifact"] is True)
    assert data["steps"][0]["has_artifact"] is True

    assert (await b.save_artifact_async("s_99", "x"))["ok"] is False  # 不存在的步
