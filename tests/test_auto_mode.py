# -*- coding: utf-8 -*-
"""自动模式回灌 5700（2026-10-17）——本任务全部新测试。

任务书：`docs/自动模式-回灌5700-任务书-20261017.md`（§六 阶段 0 块 0.1-0.5 硬判据
＋ §五 红线；阶段 1 G1-G4）
方案：  `docs/自动模式-回灌5700-迭代方案-20261017.md`

分块对应：
- 块 0.1 存储层：四列 ＋ 幂等升级 ＋ 存量默认值 ＋ create_card goal 参数
- 块 0.2 权限层：AUTO_SWITCHES（TC1 裁案 A 落地）＋ 开关动作 ＋ 子卡继承
  ＋ refresh_perms 跳过
- 块 0.3 驱动器 ＋ 执行闸（串行排队 / 并行放行 / 停止三件 / 红线 5 接线位置）
- 块 0.4 目标工具 ＋ 上下文区 ＋ 两套 instruction（片段拼装，按 auto_mode 选）
- 块 0.5 前端（源码级机械断言；DOM 级验证见验收阶段的 jsdom 一次性脚本）
- 阶段 1 G1-G4 协作能力（随各 G 追加）
- 红线 1：全链无闸（可 grep 断言；唯一「上限」是卡树防环兜底，明写不是闸）

★ 与 Cardlo 沙箱版（tests/test_auto_mode.py @ automode-6701）的差异＝本任务书
  的裁剪口径（ND1-ND3 / TC1 / TC2 / TC5）：
  · 不测「计划六件摘除」（TC5：生产保留计划六件与计划板）；
  · 不测「出厂默认 parallel」（TC2：dev 出厂 serial 灰度）；
  · 不测「_sync_judgment 全局早退」（ND1：生产保留全局思维链，只对 auto 卡
    屏蔽判断协议区）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from src.api import (
    auto_driver, card_tools, context_assembler, control, message_runner,
    model_service, prompts, routes, serial_gate,
)
from src.permissions import policy
from src.storage import card_db, card_store

REPO = Path(__file__).resolve().parents[1]


# ============================================================
# 夹具
# ============================================================

@pytest.fixture(autouse=True)
def _clean_process_state():
    """进程级状态进/出例清零。

    本套件断言「某个卡有没有被起轮」「锁被谁持有」——这些都挂在模块级表上，
    跨用例不清会把上一例的痕迹算到本例头上：
      · `serial_gate._locks/_holder`（且 asyncio.Lock 不可跨事件循环复用）；
      · `message_runner._TRIGGER_TASKS`（调起任务强引用表）；
      · `card_msg.TRIGGERS` / `event_stream._journals`（与 test_coder_card 同因）。
    """
    from src.api import card_msg, event_stream

    def _reset():
        serial_gate.reset()
        message_runner._TRIGGER_TASKS.clear()
        card_msg.TRIGGERS.clear()
        event_stream._journals.clear()

    _reset()
    yield
    _reset()


@pytest.fixture
def no_kick(monkeypatch):
    """拦掉真实起轮（不真起），只记录**真 kick 会点火**的那些调用。

    ★ 必须按 `will_continue` 同源判据过滤：`kick` 本身对非 auto / 无目标 /
    目标已终止 / 有未终止子卡的卡一律 no-op——若不分青红皂白全记，会把
    「非 auto 卡零动作」（红线 2）的断言污染成假红。
    """
    calls: list[tuple[str, str]] = []

    def _rec(w: str, c: str):
        if auto_driver.will_continue(w, c):
            calls.append((w, c))
        return None

    monkeypatch.setattr(auto_driver, "kick", _rec)
    return calls


@pytest.fixture
def serial_mode(monkeypatch, tmp_path):
    """把执行模式切成串行（配置走模块级常量 ⇒ monkeypatch 隔离）。"""
    cfg = tmp_path / "exec_mode.json"
    cfg.write_text(json.dumps({"mode": "serial"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(serial_gate, "EXEC_MODE_PATH", cfg)
    return cfg


async def _mk(wid: str, title: str, parent: str, **kw) -> str:
    r = await card_store.create_card(wid, title, "模型生成", parent=parent, **kw)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def _mk_auto(work, *, goal: str = "") -> tuple[str, str]:
    """root → 一张 auto 卡（已开自动；给了 goal 就连目标态一起置 active）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "自动卡", root)
    await card_store.update_card(wid, a, {"auto_mode": 1}, "开自动模式")
    if goal:
        await card_store.update_card(
            wid, a, {"goal": goal, "goal_state": "active"}, "设定目标")
    return wid, a


async def _drain_trigger_tasks(limit: int = 12) -> None:
    """把调起链跑干净（每跑完一批可能又起新一批，直到没有在途任务）。"""
    for _ in range(limit):
        pending = [t for t in list(message_runner._TRIGGER_TASKS) if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


# ============================================================
# 块 0.1 · 存储层：四列 ＋ 幂等升级
# ============================================================

async def test_block1_new_db_has_four_auto_columns(work):
    """硬判据1（新库一路）：建库即含四列。"""
    wid = work["work_id"]
    cols = await card_db.run_read(
        wid, lambda conn: {r["name"] for r in conn.execute("PRAGMA table_info(cards)")},
        set())
    assert {"auto_mode", "goal", "goal_state", "goal_rounds"} <= cols


async def test_block1_old_db_upgraded_idempotent_defaults(work):
    """硬判据1-3（老库一路）：缺列即 ALTER 补列；幂等；存量卡取默认值。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk(wid, "存量卡", root)

    # 造一个「迁移前的库」：把四列摘掉（模拟旧 schema）。
    db = card_db.db_path(wid)
    card_db.close_work(wid)
    conn = sqlite3.connect(str(db))
    for col in ("auto_mode", "goal", "goal_state", "goal_rounds"):
        conn.execute(f"ALTER TABLE cards DROP COLUMN {col}")
    conn.commit()
    conn.close()

    # 首次连接 ⇒ 幂等补列
    cols = await card_db.run_read(
        wid, lambda c: {r["name"] for r in c.execute("PRAGMA table_info(cards)")}, set())
    assert {"auto_mode", "goal", "goal_state", "goal_rounds"} <= cols

    # 硬判据3：存量卡四列取默认值
    meta = card_store._meta_of(wid, cid) or {}
    assert meta.get("auto_mode") == 0
    assert meta.get("goal_rounds") == 0
    assert not str(meta.get("goal_state") or "")
    assert not str(meta.get("goal") or "")

    # 硬判据2：重复跑 _ensure_schema 幂等（结构与数据零变化）
    again = await card_db.run_read(
        wid,
        lambda c: (card_db._ensure_schema(c),
                   {r["name"] for r in c.execute("PRAGMA table_info(cards)")})[1],
        None)
    assert again == cols
    assert card_store._meta_of(wid, cid) == meta


async def test_block1_create_card_without_goal_unchanged(typed_registry, work):
    """硬判据4：不传 goal ⇒ 行为与改造前一致（非 auto 父卡下的普通建卡）。"""
    wid, root = work["work_id"], work["work_card_id"]
    b = await _mk(wid, "普通子卡", root)
    m = card_store._meta_of(wid, b)
    assert m["auto_mode"] == 0
    assert m["goal_rounds"] == 0
    assert not str(m.get("goal") or "")
    assert not str(m.get("goal_state") or "")


# ============================================================
# 块 0.2 · 权限层：AUTO_SWITCHES ＋ 开关动作 ＋ 子卡继承
# ============================================================

def test_block2_auto_switches_excludes_delete():
    """TC1 裁案 A 落地：全开集＝16 码减 delete（删卡不可逆、无备份）；
    其余 15 码全开；perms_version 专用值 -1。

    ★ 任务书黑名单字面（card_delete/file_write/rule_edit）与开关词汇表对不上
    （词汇表无这三个码）——按「不可逆」意图只排 delete：file 写有 _backups
    兜底（半可逆）、rule_edit 走 ROOT_ONLY＋用户审批本就不可直达。
    """
    assert policy.AUTO_SWITCHES == policy.SWITCHES - {"delete"}
    assert len(policy.AUTO_SWITCHES) == 15
    assert "delete" not in policy.AUTO_SWITCHES
    assert "read" in policy.AUTO_SWITCHES
    assert "tools_file" in policy.AUTO_SWITCHES
    perms, ver = policy.auto_perms()
    assert set(perms) == set(policy.AUTO_SWITCHES)
    assert ver == policy.AUTO_PERMS_VERSION == -1


async def test_block2_toggle_on_off_recomputes_perms(typed_registry, work):
    """硬判据1-2：开 ⇒ AUTO_SWITCHES ＋ -1；关 ⇒ 按类型还原（与 compute_perms 一致）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)
    pc = card_store._root_project_class(wid)
    normal, nver = policy.compute_perms(pc, card_store._meta_of(wid, a)["card_type"])

    m0 = card_store._meta_of(wid, a)
    assert m0.get("auto_mode") == 0
    assert set(m0[policy.PERM_KEY]) == set(normal)

    await card_store.update_card(wid, a, {"auto_mode": 1}, "开自动")
    m1 = card_store._meta_of(wid, a)
    assert m1["auto_mode"] == 1
    assert set(m1[policy.PERM_KEY]) == set(policy.AUTO_SWITCHES)
    assert m1[policy.PERM_VERSION_KEY] == policy.AUTO_PERMS_VERSION

    await card_store.update_card(wid, a, {"auto_mode": 0}, "关自动")
    m2 = card_store._meta_of(wid, a)
    assert m2["auto_mode"] == 0
    assert set(m2[policy.PERM_KEY]) == set(normal)
    assert m2[policy.PERM_VERSION_KEY] == nver


async def test_block2_child_inherits_auto_and_all_open(typed_registry, work, no_kick):
    """硬判据3：auto 父卡下新建卡 auto_mode=1 且 perms 全开；带 goal ⇒ active。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)
    await card_store.update_card(wid, a, {"auto_mode": 1}, "开自动")

    b = await _mk(wid, "乙", a, goal="把乙这件事做完")
    m = card_store._meta_of(wid, b)
    assert m["auto_mode"] == 1
    assert set(m[policy.PERM_KEY]) == set(policy.AUTO_SWITCHES)
    assert m[policy.PERM_VERSION_KEY] == policy.AUTO_PERMS_VERSION
    assert m["goal"] == "把乙这件事做完"
    assert m["goal_state"] == "active"


async def test_block2_non_auto_parent_child_stays_normal(typed_registry, work):
    """红线 2 的镜像：父卡非 auto ⇒ 子卡不继承、perms 按类型。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)          # 父卡未开自动
    b = await _mk(wid, "乙", a, goal="x")
    m = card_store._meta_of(wid, b)
    pc = card_store._root_project_class(wid)
    assert m["auto_mode"] == 0
    assert set(m[policy.PERM_KEY]) == set(policy.compute_perms(pc, m["card_type"])[0])
    assert not str(m.get("goal_state") or "")   # 非 auto ⇒ 不置 active


async def test_block2_refresh_perms_skips_auto(typed_registry, work, no_kick):
    """硬判据4（红线 4）：刷新不冲 auto 卡的全开权限。"""
    from scripts.refresh_perms import refresh_one_work

    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)
    await card_store.update_card(wid, a, {"auto_mode": 1}, "开自动")
    before = card_store._meta_of(wid, a)

    stats = await refresh_one_work(wid, policy.config_version(), apply=True)
    assert stats["failed"] == 0
    after = card_store._meta_of(wid, a)
    assert after[policy.PERM_KEY] == before[policy.PERM_KEY]
    assert after[policy.PERM_VERSION_KEY] == policy.AUTO_PERMS_VERSION


def test_block2_permissions_json_has_no_auto_rows():
    """硬判据5（红线 3）：自动模式是**绕过**类型分权，不是新增一种分权。"""
    cfg = json.loads((REPO / "config" / "permissions.json").read_text(encoding="utf-8"))
    assert "auto" not in json.dumps(cfg, ensure_ascii=False).lower(), \
        "permissions.json 不该出现 auto 相关行"


# ============================================================
# 块 0.3 · 驱动器 ＋ 执行闸
# ============================================================

async def test_block3_after_turn_continues_and_bumps(typed_registry, work, no_kick):
    """硬判据1：auto 卡目标 active ⇒ 计轮 ＋ 起下一轮。"""
    wid, a = await _mk_auto(work, goal="把甲做完")
    await auto_driver.after_turn(wid, a)
    assert card_store._meta_of(wid, a)["goal_rounds"] == 1
    assert (wid, a) in no_kick


async def test_block3_after_turn_stops_on_complete(typed_registry, work, no_kick):
    """硬判据1（后半）：模型声明完成 ⇒ 不再起轮。"""
    wid, a = await _mk_auto(work, goal="把甲做完")
    await card_store.update_card(wid, a, {"goal_state": "complete"}, "模型声明完成")
    await auto_driver.after_turn(wid, a)
    assert card_store._meta_of(wid, a)["goal_rounds"] == 0
    assert (wid, a) not in no_kick


async def test_block3_non_auto_card_has_no_driver(typed_registry, work, no_kick):
    """红线 2：非 auto 卡驱动器零动作。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)
    await auto_driver.after_turn(wid, a)
    assert no_kick == []


async def test_block3_parent_yields_while_child_active(typed_registry, work, no_kick):
    """硬判据2：父卡有未终止子卡 ⇒ 父卡不起轮（不空转）。"""
    wid, p = await _mk_auto(work, goal="父目标")
    _c = await _mk(wid, "子卡", p, goal="子目标")   # 继承 auto ＋ active
    assert auto_driver.has_active_child(wid, p)
    await auto_driver.after_turn(wid, p)
    assert (wid, p) not in no_kick
    assert card_store._meta_of(wid, p)["goal_rounds"] == 0


async def test_block3_child_done_wakes_parent(typed_registry, work, no_kick):
    """硬判据2（后半）：子卡全部终止 ⇒ 父卡恢复起轮。"""
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡完成")
    await auto_driver.after_turn(wid, c)
    assert (wid, p) in no_kick
    assert card_store._meta_of(wid, p)["goal_rounds"] == 1


async def test_block3_serial_gate_single_holder_and_queue(serial_mode):
    """硬判据3：串行下任一时刻只有一个持有者；拿不到＝排队（不是 409）。"""
    assert serial_gate.is_serial()
    await serial_gate.acquire("w1", "card_A")
    assert serial_gate.holder_of("w1") == "card_A"

    task = asyncio.create_task(serial_gate.acquire("w1", "card_B"))
    await asyncio.sleep(0.02)
    assert not task.done(), "拿不到锁应排队等待（不得拒绝）"
    assert serial_gate.holder_of("w1") == "card_A", "任一时刻只有一个持有者"

    serial_gate.release("w1", "card_A")
    await asyncio.wait_for(task, timeout=2.0)
    assert serial_gate.holder_of("w1") == "card_B"


async def test_block3_parallel_mode_gate_disabled(monkeypatch, tmp_path):
    """硬判据4：并行模式＝执行闸不启用（＝今天的行为）。"""
    cfg = tmp_path / "e.json"
    cfg.write_text('{"mode":"parallel"}', encoding="utf-8")
    monkeypatch.setattr(serial_gate, "EXEC_MODE_PATH", cfg)
    assert not serial_gate.is_serial()
    assert await serial_gate.acquire("w2", "card_A") is False
    assert serial_gate.holder_of("w2") == ""


def test_block3_shipped_default_is_serial():
    """TC2 裁决：出厂默认 serial（先 serial 灰度，压测后转 parallel）。

    与 Cardlo 沙箱版（出厂 parallel）的口径差异——dev 的机器级需求是本地
    算力机跑不了并行（U7），且灰度从最稳的模式开始。
    """
    cfg = json.loads((REPO / "config" / "exec_mode.json").read_text(encoding="utf-8"))
    assert cfg["mode"] == "serial"


def test_block3_exec_mode_fail_open(monkeypatch, tmp_path):
    """配置缺失 / 坏 JSON ⇒ 按 parallel（fail-open：配置坏了不能把系统卡死）。"""
    monkeypatch.setattr(serial_gate, "EXEC_MODE_PATH", tmp_path / "missing.json")
    assert serial_gate.exec_mode() == "parallel"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(serial_gate, "EXEC_MODE_PATH", bad)
    assert serial_gate.exec_mode() == "parallel"


async def test_block3_stop_pauses_cascade_and_releases_lock(
        serial_mode, typed_registry, work, no_kick):
    """硬判据5：停止 ⇒ 关自动继续 ＋ 级联子卡 ＋ 释放执行锁。"""
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await serial_gate.acquire(wid, p)          # 模拟父卡正持有执行锁
    assert serial_gate.holder_of(wid) == p

    await control.stop_auto(wid, p)
    assert card_store._meta_of(wid, p)["goal_state"] == "paused"
    assert card_store._meta_of(wid, c)["goal_state"] == "paused"
    assert serial_gate.holder_of(wid) == "", "停止必须释放执行锁（队列不饿死）"


async def test_block3_stop_auto_noop_for_non_auto(serial_mode, typed_registry, work):
    """红线 2：非 auto 卡 ⇒ stop_auto 全 no-op（停止语义逐字不变）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)
    r = await control.stop_auto(wid, a)
    assert r == {"paused": [], "lock_released": False}
    assert not str(card_store._meta_of(wid, a).get("goal_state") or "")


def test_block3_no_gate_in_chain():
    """红线 1：全链无闸——驱动器/闸/接线段不出现轮次/墙钟/token 上限。

    唯一允许的「上限」字样是卡树防环兜底（_CARD_TREE_CYCLE_GUARD），且注释
    明写它不是闸。这里用 grep 机械断言：两个新文件不得出现闸类关键词。
    ★ 剔除三类说明性文字再扫（它们恰恰是在声明「没有闸」）：
      ① 防环常量行；② 含「不设/不是/无/无任何」的负向表述行；③ 空行。
    """
    import re
    files = [REPO / "src" / "api" / "auto_driver.py",
             REPO / "src" / "api" / "serial_gate.py"]
    pat = re.compile(r"max_rounds|MAX_ROUNDS|round_limit|max_tokens|wall_clock"
                     r"|deadline|轮次上限|墙钟|token.*上限")
    neg = re.compile(r"不设|不是|无闸|无任何|无超时|兜底|防环")
    for f in files:
        text = f.read_text(encoding="utf-8")
        body = "\n".join(
            ln for ln in text.splitlines()
            if "_CARD_TREE_CYCLE_GUARD" not in ln
            and not neg.search(ln)
            and ln.strip())
        hits = [ln for ln in body.splitlines() if pat.search(ln)]
        assert not hits, f"{f.name} 出现闸类关键词: {hits}"
