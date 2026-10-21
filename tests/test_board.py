# -*- coding: utf-8 -*-
"""板子基建五类判据（迭代二 S1 原型；[思维链真实落地 2026-10-11] 五态重写）。

验收原文＋判据化扩展：
①正常建板落盘（steps 含 criteria/deliverable，status 起步 pending，result 空串）
②强杀重建（断尾 JSON → 明报，不做产物重放）＋ restore 快照重建（从当前步续）
③孤儿三态（终态删/卡无删/卡活留，零时间戳）④幂等（重复指针写不撕裂；
result 随步落板）⑤并发快照（锁内原子）。
另验：ops plan_* 动作、revision_store 双留痕＋板上 revisions[] 自动记、
防护①（重启恢复序——restore 单一路径）、防护⑤（清理后旧引用拒绝）、
防护⑥（锁超时明报）、plan_remove 当前步不可删（结构性拒绝）、置脏异步
合并落盘、criteria/deliverable 非空结构性拒绝、plan_modify 五态 status。
零真实 works 触碰（conftest 铁律一）；不 import main（铁律二）。
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

import async_settle
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
    restore,
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
    # [思维链真实落地] 新契约：criteria/deliverable 必填（判据可观测）
    return [{"id": f"s{i + 1}", "prompt": p, "criteria": f"{p}的判据X",
             "deliverable": f"{p}的产出物Y", "type": "execute"}
            for i, p in enumerate(prompts)]


async def _wait_disk(path, pred):
    """等置脏合并任务把状态写到盘（修订是异步合并落盘）。

    [5713 · M2 2026-10-20] 旧实现每 10ms **读一次盘**去等谓词成立——读句柄与
    flush 的 ``tmp→os.replace`` 争同一个 plan 文件，Windows 上 rename 直接
    ``WinError 5``；而 ``board._atomic_write_json`` 的 ``os.replace`` 是裸的
    （无重试），flush 任务当场异常退出，盘上永远到不了预期 ⇒ 3 秒后报
    「盘上状态未达预期」。现改为：**等这批 flush 任务自己跑完**（循环条件＝
    「没有未完成的异步合并」），收口后读一次盘。断言一个字没改。
    """
    await async_settle.settle()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert pred(data), f"盘上状态未达预期: {path}"
    return data


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
    # 新步结构：criteria/deliverable 必落、status 起步 pending、result 空串
    assert data["steps"][0] == {"id": "s1", "prompt": "第一步",
                                "criteria": "第一步的判据X",
                                "deliverable": "第一步的产出物Y",
                                "type": "execute", "status": "pending", "result": ""}
    assert len(data["steps"]) == 3

    assert has_live_board(wid, cid) is True
    assert await current_step_prompt(wid, cid) == "第一步"
    assert "plan_create" in _ops_actions()

    # 重复建板拒绝、不覆盖；结构非法拒绝（含 criteria/deliverable 非空校验）
    r2 = await create_board(wid, cid, "x", _steps("别的"))
    assert r2["ok"] is False and r2["error"] == "board_exists"
    assert (await create_board(wid, cid, "x", []))["ok"] is False
    dup = await create_board(wid, cid, "x", [{"id": "a", "prompt": "1",
                                              "criteria": "c", "deliverable": "d", "type": "execute"},
                                             {"id": "a", "prompt": "2",
                                              "criteria": "c", "deliverable": "d", "type": "execute"}])
    assert dup["ok"] is False and "重复" in dup["error"]
    nop = await create_board(wid, cid, "x", [{"id": "a", "prompt": "", "criteria": "c",
                                              "deliverable": "d", "type": "execute"}])
    assert nop["ok"] is False
    nocrit = await create_board(wid, cid, "x", [{"id": "a", "prompt": "1",
                                                 "criteria": "", "deliverable": "d", "type": "execute"}])
    assert nocrit["ok"] is False and "criteria" in nocrit["error"]
    fakecrit = await create_board(wid, cid, "x", [{"id": "a", "prompt": "1", "criteria": "c",
                                                   "deliverable": "", "type": "execute"}])
    assert fakecrit["ok"] is False and "deliverable" in fakecrit["error"]


# ----------------------------------------------------- ② 强杀断尾明报 ＋ 防护①恢复序（restore）

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
    """防护①：重启重建——指针同步落盘后，内存清空仍从盘恢复到同一位；
    已推进步的 result 随快照恢复（跨回合不丢步成果）。"""
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
    assert [s["status"] for s in snap["steps"]] == ["done", "done", "pending"]
    assert [s["result"] for s in snap["steps"]] == ["结果一", "结果二", ""]
    r = await b2.mark_done_and_advance("结果三")  # 恢复后接着推——链路不断
    assert r["advanced"] and r["all_steps_done"]


async def test_restore_from_snapshot_dict_registers_runtime(work):
    """restore(snapshot)：从快照 dict 显式重建并注册——强杀/重启续链的统一
    入口；从当前步续（不从头不重跑）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await create_board(wid, cid, "恢复计划", _steps("a", "b", "c"))
    b = await get_board(wid, cid)
    await b.mark_done_and_advance("第一步成果")
    snap = b.to_snapshot()
    board._boards.clear()  # 模拟强杀（内存全空）

    rb = restore(snap, work_id=wid, card_id=cid)
    assert rb is not None and has_live_board(wid, cid)
    st = rb.current_step()
    assert st["id"] == "s2" and st["status"] == "pending"  # 从第 2 步续
    assert snap["steps"][0]["result"] == "第一步成果"
    # 恢复后的板可继续推进且落盘可见
    r = await rb.mark_done_and_advance("第二步成果")
    assert r["advanced"] is True
    disk = json.loads(board.plan_path(wid, cid).read_text(encoding="utf-8"))
    assert disk["current"] == 2


async def test_restore_rejects_bad_snapshot(work):
    wid, cid = work["work_id"], work["work_card_id"]
    with pytest.raises(BoardRestoreError):
        restore({"steps": "不是列表"}, work_id=wid, card_id=cid)
    with pytest.raises(BoardRestoreError):
        restore({"steps": []}, work_id="", card_id="")


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


# ------------------------------------------------------------- ④ 幂等（指针不撕裂＋result 落板）

async def test_mark_done_idempotent_no_tear(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await create_board(wid, cid, "n", _steps("a", "b"))
    b = await get_board(wid, cid)

    r1 = await b.mark_done_and_advance("结果一")
    assert r1["advanced"] is True and r1["prompt"] == "b"
    # 下一步全字段随返回（plan_tools 按附录 C 拼引导的材料）
    assert r1["next_step"]["id"] == "s2"
    assert r1["next_step"]["criteria"] == "b的判据X"
    disk = json.loads(board.plan_path(wid, cid).read_text(encoding="utf-8"))
    assert disk["current"] == 1 and disk["steps"][0]["status"] == "done"
    assert disk["steps"][0]["result"] == "结果一"  # result 随步落板

    r2 = await b.mark_done_and_advance("结果二")
    assert r2["advanced"] is True and r2["all_steps_done"] is True
    assert "next_step" not in r2  # 无下一步

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
                                          "step": {"id": "s_new", "prompt": "新步",
                                                   "criteria": "新步判据", "deliverable": "新步产出",
                                                   "type": "execute"}})
    assert r["ok"] is True, r
    snap = r["board"]
    assert [s["id"] for s in snap["steps"]] == ["s1", "s_new", "s2", "s3"]
    assert snap["current"] == 2  # s2 的新位（指针按 id 重定位）
    assert snap["steps"][1]["status"] == "pending"  # 插入步 pending 起步

    # insert 缺 criteria/deliverable → 结构性拒绝（写入口②同契约）
    assert "criteria" in (await b.apply_revision(
        "insert", {"after_id": "s1",
                   "step": {"id": "s_x", "prompt": "x", "deliverable": "d", "type": "execute"}}))["error"]
    assert "deliverable" in (await b.apply_revision(
        "insert", {"after_id": "s1",
                   "step": {"id": "s_y", "prompt": "x", "criteria": "c", "type": "execute"}}))["error"]

    r2 = await b.apply_revision("remove", {"id": "s1"})  # 删非当前步
    assert r2["ok"] and r2["board"]["current"] == 1  # s2 新位（序变 [s_new, s2, s3]）

    # 当前步不可删（结构性拒绝——删了指针没处指）
    assert (await b.apply_revision("remove", {"id": "s2"}))["error"] == "当前步不可删"

    r3 = await b.apply_revision("modify", {"id": "s3", "prompt": "改写后的要求"})
    assert r3["ok"] and r3["board"]["steps"][2]["prompt"] == "改写后的要求"

    # modify 新字段：criteria/deliverable/status（五态）
    r5 = await b.apply_revision("modify", {"id": "s2", "criteria": "改后的判据",
                                           "deliverable": "改后的产出", "status": "blocked"})
    assert r5["ok"] and r5["board"]["steps"][1]["criteria"] == "改后的判据"
    assert r5["board"]["steps"][1]["status"] == "blocked"
    bad_status = await b.apply_revision("modify", {"id": "s2", "status": "done_x"})
    assert bad_status["ok"] is False and "status" in bad_status["error"]

    r4 = await b.apply_revision("remove", {"id": "s_new"})  # 再删一个非当前步
    assert r4["ok"] and [s["id"] for s in r4["board"]["steps"]] == ["s2", "s3"]

    # 非法改板拒绝
    assert (await b.apply_revision("reorder", {"order": ["s2"]}))["ok"] is False
    assert (await b.apply_revision("remove", {"id": "不存在"}))["ok"] is False
    assert (await b.apply_revision("warp", {}))["ok"] is False
    assert (await b.apply_revision("modify", {"id": "s2"}))["ok"] is False  # 零字段

    # 板上 revisions[] 自动记（动作/时间/改前快照/改后快照）——负例被拒不记
    disk = await _wait_disk(board.plan_path(wid, cid), lambda d: len(d["revisions"]) == 5)
    revs = disk["revisions"]
    assert [rv["action"] for rv in revs] == ["insert", "remove", "modify", "modify", "remove"]
    assert all(rv["at"] and rv["before"] and rv["after"] for rv in revs)
    assert revs[0]["after"][1]["id"] == "s_new"

    # revision_store.record_revision(entity="plan") 流水逐行核对
    revp = session_store.WORKS_DIR / wid / "_ops" / "revisions.jsonl"
    lines = [json.loads(l) for l in revp.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [l["entity"] for l in lines] == ["plan"] * 5
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


# ------------------------------------------------- 附录 C 步执行提示词模板（单点定义）

def test_step_brief_template_verbatim():
    """附录 C 模板全文（一字不改）＋字段拼装。"""
    text = board.step_brief({"prompt": "查资料", "criteria": "文件存在",
                             "deliverable": "notes.md"})
    assert "【步骤执行】你现在执行计划的一步，只做本步的事。" in text
    assert "本步要求：查资料" in text
    assert "完成判据：文件存在（满足它，这一步才算完成）" in text
    assert "产出物：notes.md" in text
    assert '3. 完成判据不满足，就说"没完成"，不许谎报"已完成"。' in text
    assert '4. 做完立刻调 step_complete：result 写本步成果，criteria_met 写"判据如何满足"。' in text
    # 缺字段容错：如实标注不编造
    bare = board.step_brief({"prompt": "x"})
    assert "（本步未定判据" in bare and "（本步未定产出物" in bare
