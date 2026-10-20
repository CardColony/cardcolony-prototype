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
    auto_driver, card_msg, card_tools, context_assembler, control, message_runner,
    model_service, prompts, routes, serial_gate,
)
from src.permissions import policy
from src.storage import card_db, card_store

REPO = Path(__file__).resolve().parents[1]


def _thinkchain_enabled() -> bool:
    """思维链总开关（生产口径可能为 False＝纯单模型）。断言语义须与它解耦。"""
    from src.thinkchain import enabled as _enabled
    return bool(_enabled())


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
        auto_driver._IDLE_ROUNDS.clear()   # [阶段2.7] 空转计数跨用例不清会串味

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


# ============================================================
# 块 0.4 · 目标工具 ＋ 上下文区 ＋ 提示词（TC5 口径：保留计划六件）
# ============================================================

#: 施工前记录的 t22-1 BASE_PROMPT sha256（红线 10 判据：片段化后拼装结果
#: 必须逐字节一致）。
T22_1_BASE_PROMPT_SHA256 = "9657ae2f422c461d21cb9b9fc5e21f527155d4317ba9ef5ae89f2226801afec5"

#: auto 版工具清单必须**逐件在册**（块0.4 硬判据 4）。
_AUTO_ONLY_TOOL_LINES = (
    "edit_card(card_id, content)",
    "update_shadow(content)",
    "read_linked(target_id, part)",
    "run_tool_card(script_name",
    "update_goal(action, reason?, await_children?)",
    "send_card_message(target_card_id, msg_type, content, reply_to?)",
    "read_inbox(pending_only?, direction?)",
    "create_card(title, parent?, content?, goal?)",
)

#: TC5：生产**保留**计划六件（与 Cardlo 沙箱版 ND1 口径相反）——auto 卡工具面
#: 与提示词里都必须有。
_PLAN_TOOL_LINES = ("create_plan", "plan_reorder", "plan_insert",
                    "plan_modify", "plan_remove", "step_complete")


async def test_block4_normal_toolface_unchanged(typed_registry, work):
    """硬判据1：非 auto 卡工具面逐字不变（无 update_goal、create_card 仍三参）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)
    cards = card_tools._collect_card_meta(wid)
    tools = card_tools.build_tool_schemas(card_id=a, work_id=wid, cards=cards)
    names = [t["name"] for t in tools]
    assert "update_goal" not in names
    cc = next(t for t in tools if t["name"] == "create_card")
    assert set(cc["parameters"]["properties"]) == {"title", "parent", "content"}


async def test_block4_auto_toolface_is_normal_plus_update_goal(
        typed_registry, work, no_kick):
    """块0.4 硬判据2（TC5 口径）：auto 卡工具面＝常规 **＋ update_goal**。

    ★ 与 Cardlo 沙箱版的差异：那版摘计划六件（ND1「最原始单模型」实验口径）；
      TC5 裁定生产**保留**计划六件与计划板 ⇒ 这里是纯粹的「＋1 件」。
    """
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)
    cards = card_tools._collect_card_meta(wid)
    normal = [t["name"] for t in
              card_tools.build_tool_schemas(card_id=a, work_id=wid, cards=cards)]

    await card_store.update_card(wid, a, {"auto_mode": 1}, "开自动")
    cards = card_tools._collect_card_meta(wid)
    auto_tools = card_tools.build_tool_schemas(card_id=a, work_id=wid, cards=cards)
    auto_names = [t["name"] for t in auto_tools]

    assert set(auto_names) == ((set(normal) - {"delete_card"})
                               | set(card_tools.AUTO_ONLY_TOOLS)), (
        f"auto 面应为「常规 - delete_card（TC1） + auto 专用件」："
        f"多={set(auto_names)-set(normal)-set(card_tools.AUTO_ONLY_TOOLS)} "
        f"少={set(normal)-set(auto_names)-{'delete_card'}}")
    # 追加段＝末尾连续 len(AUTO_ONLY_TOOLS) 件，且恰为该集合（不钉字母序）
    _tail = auto_names[-len(card_tools.AUTO_ONLY_TOOLS):]
    assert set(_tail) == set(card_tools.AUTO_ONLY_TOOLS), \
        f"auto 专用件应追加在末尾，实得 {_tail}"
    assert _tail[0] == "update_goal", "update_goal 仍是追加段首位（既有语义保留）"
    # G2：自主通信两件在 auto 面（041e25d/e4e612d）
    assert {"send_card_message", "read_inbox"} <= set(auto_names)
    # ★ [2026-10-17 口径同步] 计划六件与 thinkchain 开关同生共死，不再断言「必须在」：
    # 上面那条等值断言（auto 面 == normal − delete + AUTO_ONLY）已覆盖真意图
    # ——**auto 开口不额外摘除常规面里的任何工具**；六件在不在取决于开关，
    # 两端都自洽（开关开：normal 有六件 ⇒ auto 也有；开关关：都没有）。
    _tc_on = _thinkchain_enabled()
    for p in _PLAN_TOOL_LINES:
        assert (p in auto_names) == (p in normal), (
            f"auto 面与常规面在 {p} 上应一致（不额外摘除）；开关={'开' if _tc_on else '关'}")
    cc = next(t for t in auto_tools if t["name"] == "create_card")
    assert "goal" in cc["parameters"]["properties"], "auto 卡 create_card 应带 goal 参数"
    # 注册表原字面量未被污染（否则非 auto 卡的工具面也会变）
    lit = card_tools._schema_of(card_tools.TOOL_REGISTRY["create_card"])
    assert "goal" not in lit["parameters"]["properties"]


async def test_block4_update_goal_complete_sets_state(typed_registry, work, no_kick):
    """硬判据3：update_goal(complete) ⇒ goal_state=complete，驱动器不再起轮。"""
    wid, a = await _mk_auto(work, goal="把甲做完")
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, a, "update_goal",
                                      {"action": "complete"}, cards)
    assert r["ok"] is True, r
    assert card_store._meta_of(wid, a)["goal_state"] == "complete"
    assert auto_driver.will_continue(wid, a) is False


async def test_block4_update_goal_denied_on_non_auto(typed_registry, work):
    """执行层双保险①：非 auto 卡调 update_goal ⇒ denied。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, a, "update_goal",
                                      {"action": "complete"}, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied", r


async def test_block4_update_goal_rejects_bad_action(typed_registry, work, no_kick):
    wid, a = await _mk_auto(work, goal="把甲做完")
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, a, "update_goal", {"action": "nope"}, cards)
    assert r["ok"] is False and r["error"]["code"] == "validation", r


def test_block4_goal_region_renders_only_for_auto():
    """硬判据4（渲染层）：非 auto ⇒ 空串（区不出现）。

    区里给的是**机器态原值**（active/paused/blocked/complete）——模型要拿它跟
    update_goal 的 action 语义对齐；中文标签只在**前端标记行**用。
    """
    from src.api import context_regions as cr
    assert cr.render_goal_region(None) == ""
    assert cr.render_goal_region({"auto_mode": 0}) == ""
    txt = cr.render_goal_region({"auto_mode": 1, "goal": "把 X 做完",
                                 "goal_state": "active", "goal_rounds": 3})
    assert "把 X 做完" in txt and "active" in txt and "3" in txt


async def test_block4_goal_region_present_only_for_auto_card(
        typed_registry, work, no_kick):
    """硬判据4（装配层）：goal 区出现在 auto 卡上下文；非 auto 卡不出现。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)
    names0 = [r["name"] for r in context_assembler.build_context_regions(wid, a)]
    assert "自动模式·目标" not in names0
    await card_store.update_card(wid, a, {"auto_mode": 1}, "开自动")
    names1 = [r["name"] for r in context_assembler.build_context_regions(wid, a)]
    assert "自动模式·目标" in names1


def test_block4_judgment_region_blank_for_auto_only():
    """TC5：判断协议区只对 auto 卡返空；非 auto 卡逐字不变（ND1 红线 11）。

    dev 与 Cardlo 的差异：那版是**全局**返空（沙箱「原生单模型」口径）；本版
    只屏蔽 auto 卡——auto 卡已有 goal，再判「直答还是上板」会跟 goal 打架。
    """
    from src.api import context_regions as cr

    from src.thinkchain import enabled as _tc_enabled
    ctx_auto = cr.RegionCtx(cur_meta={"auto_mode": 1}, request_once=[{"x": 1}])
    assert cr.render_judgment_region(ctx_auto) == []      # auto 卡永屏蔽（本任务）
    ctx_normal = cr.RegionCtx(cur_meta={"auto_mode": 0}, request_once=[{"x": 1}])
    ctx_none = cr.RegionCtx(cur_meta=None, request_once=[])
    assert cr.render_judgment_region(ctx_none) == []
    if _tc_enabled():
        # 思维链开着：非 auto 卡逐字不变（原样返回 request_once）
        assert cr.render_judgment_region(ctx_normal) == [{"x": 1}]
    else:
        # [纯单模型口径] 全局关链 ⇒ 一律不注入（与非 auto 卡同为零注入）
        assert cr.render_judgment_region(ctx_normal) == []


def test_block4_prompt_version_and_section():
    """版本口径：普通版 t22-1 **不动**（文本逐字节不变）；自动版随 G 推进。

    [回灌 5700 §十 版本号推进表] auto-t1＝块0.4 初版；auto-t2＝G2（自主通信
    两件进 auto 面）；auto-t3＝G4（协作协议两条义务式规则）。
    """
    assert prompts.BASE_PROMPT_VERSION == "t22-1"
    assert prompts.AUTO_BASE_PROMPT_VERSION == "auto-t7"
    assert "## 自动模式" not in prompts.BASE_PROMPT
    assert "## 自动模式" in prompts.AUTO_BASE_PROMPT


def test_block4_tool_table_and_registry():
    assert card_tools.TOOL_TABLE_VERSION == "tools-tc-15"
    assert "update_goal" in card_tools.TOOL_REGISTRY
    assert card_tools.TOOL_SWITCH["update_goal"] == policy.SWITCH_PLAN
    assert "update_goal" in card_tools.AUTO_ONLY_TOOLS
    # G2：自主通信两件注册＋映射＋条件下发
    assert "send_card_message" in card_tools.TOOL_REGISTRY
    assert "read_inbox" in card_tools.TOOL_REGISTRY
    assert card_tools.TOOL_SWITCH["send_card_message"] == policy.SWITCH_SEND_MESSAGE
    assert card_tools.TOOL_SWITCH["read_inbox"] == policy.SWITCH_READ
    assert card_tools.AUTO_ONLY_TOOLS == {"update_goal", "send_card_message",
                                          "read_inbox"}


# ============================================================
# 块 0.4 返工判据 · 两套 instruction（任务书 §块0.4 硬判据 5-7）
# ============================================================

def test_rework_1_base_prompt_byte_identical_to_t22_1():
    """硬判据（红线 10）：片段化后拼装结果与 t22-1 逐字节一致（sha256）。"""
    import hashlib
    base = prompts.BASE_PROMPT
    assert hashlib.sha256(base.encode("utf-8")).hexdigest() == T22_1_BASE_PROMPT_SHA256, \
        "BASE_PROMPT 与记录的 t22-1 sha256 不符"
    assert base == prompts._join_sections(
        prompts._SEC_HEAD, prompts._SEC_TOOLS, prompts._SEC_WRITE,
        prompts._SEC_CODER, prompts._SEC_RULE, prompts._SEC_ERR,
        prompts._SEC_CARDMSG,
    )


def test_rework_2_versions():
    """BASE_PROMPT_VERSION 不动；AUTO_BASE_PROMPT_VERSION 存在且非空。"""
    assert prompts.BASE_PROMPT_VERSION == "t22-1"
    assert isinstance(prompts.AUTO_BASE_PROMPT_VERSION, str)
    assert prompts.AUTO_BASE_PROMPT_VERSION.strip()


async def test_rework_3_instruction_region_selects_by_auto_mode(
        typed_registry, work, no_kick):
    """硬判据（V9）：非 auto 卡拿 BASE_PROMPT；auto 卡拿 AUTO_BASE_PROMPT。"""
    from src.api import context_regions as cr

    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)
    spec = cr._spec_by_key("instruction")
    assert spec is not None

    ctx0 = cr.RegionCtx(cur_meta={"auto_mode": 0})
    assert spec["render"](ctx0) == prompts.BASE_PROMPT

    ctx1 = cr.RegionCtx(cur_meta={"auto_mode": 1})
    assert spec["render"](ctx1) == prompts.AUTO_BASE_PROMPT

    # 真实装配链（非合成 ctx）：非 auto ⇒ 区文本 == BASE_PROMPT
    bundle0 = context_assembler.build_context_bundle(wid, a)
    assert bundle0["main"]["system_prompt"].startswith(prompts.BASE_PROMPT)
    await card_store.update_card(wid, a, {"auto_mode": 1}, "开自动")
    bundle1 = context_assembler.build_context_bundle(wid, a)
    assert bundle1["main"]["system_prompt"].startswith(prompts.AUTO_BASE_PROMPT)


def test_rework_4_auto_prompt_has_full_toolset_and_no_false_sentence():
    """硬判据 4：auto 版含全部全权工具＋计划六件（TC5）；不含「工具已收回」假话。"""
    auto = prompts.AUTO_BASE_PROMPT
    for line in _AUTO_ONLY_TOOL_LINES:
        assert line in auto, f"auto 版工具清单缺：{line}"
    assert "工具已收回" not in auto, "auto 版仍写着「工具已收回」（对全权卡是假话）"
    assert "你可以直接改写卡片正文" in auto, "auto 版内容写入节应为全权口径"
    # ★ [2026-10-17 口径同步] 计划六件在提示词里的有无**必须与开关一致**：
    # 开关开 ⇒ 提示词可声称（工具确实下发）；开关关 ⇒ 不得声称（否则是假话）。
    _tc_on = _thinkchain_enabled()
    for name in _PLAN_TOOL_LINES:
        assert (name in auto) == _tc_on, (
            f"提示词与开关不一致：{name} 在提示词里={name in auto}，开关={_tc_on}")
    assert "大事拆小" in auto, "_SEC_AUTO 须有「大事拆小」条目（两种口径下都在）"


def test_rework_5_normal_prompt_has_no_auto_text():
    """硬判据 5（红线 10）：非 auto 卡提示词不含自动版任何新增文本。"""
    base = prompts.BASE_PROMPT
    assert "## 自动模式" not in base
    assert "update_goal" not in base
    assert "你拥有全部工具与全部权限" not in base
    assert "本卡处于自动模式" not in base
    # 自动版新增的三段（TOOLS_AUTO / WRITE_AUTO / AUTO）的字句不得泄进普通版
    for frag in (prompts._SEC_TOOLS_AUTO, prompts._SEC_WRITE_AUTO, prompts._SEC_AUTO):
        assert frag not in base
    # 反向：普通版独有的「起草＋用户确认」节在自动版里不存在
    assert "你负责起草" not in prompts.AUTO_BASE_PROMPT
    assert "用户负责确认写入" not in prompts.AUTO_BASE_PROMPT


def test_rework_6_shared_sections_identical_in_both():
    """拼装表：HEAD/CODER/RULE/ERR/CARDMSG 两版**共用、逐字不动**。"""
    base = prompts.BASE_PROMPT
    auto = prompts.AUTO_BASE_PROMPT
    for frag in (prompts._SEC_HEAD, prompts._SEC_CODER, prompts._SEC_RULE,
                 prompts._SEC_ERR, prompts._SEC_CARDMSG):
        assert frag in base, "共用片段不在普通版里"
        assert frag in auto, "共用片段不在自动版里"
    # 两版只差三处：TOOLS→TOOLS_AUTO、WRITE→WRITE_AUTO、尾部 +AUTO
    assert prompts.AUTO_BASE_PROMPT != prompts.BASE_PROMPT
    assert prompts._SEC_TOOLS in base and prompts._SEC_TOOLS not in auto
    assert prompts._SEC_TOOLS_AUTO in auto and prompts._SEC_TOOLS_AUTO not in base


# ============================================================
# 块 0.3 收尾 · 依赖 update_goal 的两条端到端驱动用例
# （D2 建卡即起跑；红线 5 驱动器在互斥体释放后）
# ============================================================

async def test_block3_create_child_with_goal_kicks(typed_registry, work, monkeypatch):
    """D2 建卡即起跑：auto 父卡建带目标的子卡 ⇒ 子卡**自动起跑**并跑到声明完成。"""
    wid, p = await _mk_auto(work)          # 父卡 auto 但**无目标**（不参与续跑）
    calls: list[str] = []
    state = {"n": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, **_):
        calls.append(str(user_prompt))
        state["n"] += 1
        if state["n"] == 2:
            return ({"content": "", "tool_calls": [{
                "name": "update_goal", "arguments": {"action": "complete"},
            }]}, "fake")
        return ({"content": "子卡：处理中。", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")

    c = await _mk(wid, "子卡", p, goal="子目标")
    await _drain_trigger_tasks()

    assert calls, "子卡未自动起跑（D2 建卡即起跑）"
    assert any("子目标" in x for x in calls), calls
    m = card_store._meta_of(wid, c)
    assert m["goal_state"] == "complete"
    assert m["goal_rounds"] == 1


async def test_block3_driver_runs_after_mutex_release(typed_registry, work, monkeypatch):
    """硬判据6（红线 5）：驱动检查排在互斥体释放之后 ⇒ 连续两轮不被 409 拒。"""
    wid, a = await _mk_auto(work, goal="把甲做完")
    calls: list[str] = []
    state = {"declared": False}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, **_):
        calls.append(str(user_prompt))
        if len(calls) == 1:
            return ({"content": "第一轮：先做一半。", "tool_calls": []}, "fake")
        if not state["declared"]:
            state["declared"] = True
            return ({"content": "", "tool_calls": [{
                "name": "update_goal",
                "arguments": {"action": "complete", "reason": "做完了"},
            }]}, "fake")
        return ({"content": "已完成。", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")

    await routes._run_message(wid, a, "开始")
    await _drain_trigger_tasks()

    assert len(calls) >= 2, "第二轮没跑起来（驱动器被互斥体 409 拒？）"
    m = card_store._meta_of(wid, a)
    assert m["goal_state"] == "complete"
    assert m["goal_rounds"] >= 1


# ============================================================
# 块 0.5 · 前端（源码级机械断言；DOM 级验证见验收阶段的 jsdom 一次性脚本）
#
# 本仓 pytest 纪律：DOM 走 jsdom 一次性脚本——这里钉的是「开关/标记/样式/
# 版本戳确实进了源码」，不是渲染结果。
# ============================================================

def test_block5_auto_toggle_in_source():
    """auto 开关落在底边栏（card.html #flowInputLeft ⇒ input-bar 收编进 modes）。"""
    card_html = (REPO / "templates" / "card.html").read_text(encoding="utf-8")
    assert 'id="autoToggleBtn"' in card_html, "底边栏缺 auto 开关按钮"
    assert 'id="flowInputLeft"' in card_html, "开关须在 input-bar 收编位里"
    ws = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
    assert "function renderAutoToggle" in ws, "缺开关状态刷新"
    assert "async function toggleAutoMode" in ws, "缺开关动作（PATCH auto_mode）"
    assert "auto_mode: next" in ws, "开关须经 PATCH 落 auto_mode"
    assert 'getElementById("autoToggle")' in ws, "右栏卡详情开关未绑"


def test_block5_auto_mark_in_source():
    """卡详情 auto 标记（徽标＋目标态＋轮次＋目标）＋样式段＋版本戳。"""
    ws = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
    assert "auto-badge" in ws and "auto-goal" in ws, "卡详情缺 auto 标记"
    assert "function goalStateLabel" in ws, "缺目标态中文标签"
    css = (REPO / "static" / "style.css").read_text(encoding="utf-8")
    assert ".auto-badge" in css and ".auto-switch" in css, "style.css 缺 auto 样式"
    card_html = (REPO / "templates" / "card.html").read_text(encoding="utf-8")
    assert "main_chat.js?v=3.0" in card_html
    assert "style.css?v=45" in card_html
    work_html = (REPO / "templates" / "work.html").read_text(encoding="utf-8")
    assert "style.css?v=45" in work_html


# ============================================================


# ============================================================
# 阶段 1 · G1 断链修复（019a0f0：HTTP 建卡透传 goal）
#
# 背景：存储层 create_card 早有 goal 形参，但 CreateCardRequest 未声明该字段
# ⇒ Pydantic 静默丢弃 ⇒ HTTP/前端建的 auto 子卡 goal 为空、永不点火（只有
# 模型侧 create_card(goal=...) 通）。G1 补上 user/前端这条断链。
# ============================================================

@pytest.fixture
async def g1_client(sandbox):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    app = FastAPI()
    app.include_router(routes.router)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def test_g1_http_create_card_passes_goal(typed_registry, work, g1_client,
                                               no_kick):
    """硬判据1：HTTP 建卡带 goal ⇒ 卡上 goal 字段有值（非 auto 父卡下也落）。"""
    wid, root = work["work_id"], work["work_card_id"]
    r = await g1_client.post(f"/api/works/{wid}/cards", json={
        "title": "带目标的新卡", "source": "用户直接创建",
        "parent": root, "goal": "把这件事做完",
    })
    assert r.status_code == 201, r.text
    cid = r.json()["meta"]["card_id"]
    m = card_store._meta_of(wid, cid) or {}
    assert m.get("goal") == "把这件事做完"


async def test_g1_http_create_card_without_goal_unchanged(
        typed_registry, work, g1_client):
    """硬判据2：不带 goal ⇒ 行为与改造前一致（空串、非 auto 不起跑）。"""
    wid, root = work["work_id"], work["work_card_id"]
    r = await g1_client.post(f"/api/works/{wid}/cards", json={
        "title": "无目标的新卡", "source": "用户直接创建", "parent": root,
    })
    assert r.status_code == 201, r.text
    cid = r.json()["meta"]["card_id"]
    m = card_store._meta_of(wid, cid) or {}
    assert m.get("auto_mode") == 0
    assert not str(m.get("goal") or "")
    assert not str(m.get("goal_state") or "")


async def test_g1_auto_parent_child_with_goal_kicks(typed_registry, work,
                                                    g1_client, monkeypatch):
    """硬判据3：auto 父卡经 HTTP 建带 goal 的子卡 ⇒ auto_mode=1 ＋ goal 有值
    ＋ 建卡即起跑（D2 链在 HTTP 入口闭合）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "父卡", root)
    await card_store.update_card(wid, a, {"auto_mode": 1}, "开自动")

    calls: list[str] = []
    state = {"declared": False}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, **_):
        calls.append(str(user_prompt))
        if not state["declared"]:
            state["declared"] = True
            return ({"content": "", "tool_calls": [{
                "name": "update_goal", "arguments": {"action": "complete"},
            }]}, "fake")
        # complete 之后的那一轮：空手交付——否则工具循环无闸会把 fake 无限
        # 追问下去（每轮都返同一个工具调用＝死循环打转，块0.3 同款手法）。
        return ({"content": "已完成。", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")

    r = await g1_client.post(f"/api/works/{wid}/cards", json={
        "title": "子卡", "source": "用户直接创建",
        "parent": a, "goal": "子卡任务",
    })
    assert r.status_code == 201, r.text
    cid = r.json()["meta"]["card_id"]
    m = card_store._meta_of(wid, cid) or {}
    assert m.get("auto_mode") == 1, "auto 父卡的子卡应继承 auto"
    assert m.get("goal") == "子卡任务"
    assert m.get("goal_state") == "active"
    await _drain_trigger_tasks()
    assert calls, "HTTP 建的 auto 子卡未起跑（断链未闭合）"
    assert any("子卡任务" in x for x in calls), calls


# ============================================================
# 阶段 1 · G2 交回模型（041e25d + e4e612d：send_card_message/read_inbox 进
# auto 面——模型自主协作跑通）
#
# 不变量：2026-10-11 12:34 裁定（发信不是通用工具）**不撤销**——常规面仍
# 不下发两件；auto 卡是显式例外（AUTO_ONLY_TOOLS 条件下发）。
# ============================================================

async def test_g2_normal_toolface_has_no_comm_tools(typed_registry, work):
    """硬判据1/4：非 auto 卡工具面不含 send_card_message/read_inbox（裁定不破）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)
    cards = card_tools._collect_card_meta(wid)
    names = [t["name"] for t in
             card_tools.build_tool_schemas(card_id=a, work_id=wid, cards=cards)]
    assert "send_card_message" not in names
    assert "read_inbox" not in names


async def test_g2_read_inbox_outgoing_view(typed_registry, work, no_kick):
    """硬判据3（出向视图，8eaf33e 同源实现）：发问方读 outgoing 看得见
    answered/unanswered_sent——没人答时不得默默接受。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)
    # 乙建成**甲的子卡**：同树闸（2026-10-09 口径）只放行父子/祖先链互发，
    # 兄弟挡死——自动模式的协作形态正是父派子、子报父。
    b = await _mk(wid, "乙", a)
    # 甲问乙一条 msg_ask（经流程薄壳落信，不走模型工具）
    r = await card_tools.send_card_message(
        wid, a, target_id=b, msg_type="msg_ask", content="乙，这事怎么看？")
    assert "error" not in r, r
    ask_id = r["msg_id"]
    # 乙还没答 ⇒ 甲的 outgoing 视图：answered=False ＋ unanswered_sent 含该信
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(
        wid, a, "read_inbox", {"direction": "outgoing"}, cards)
    assert out["ok"] is True, out
    assert out["data"]["unanswered_sent"] == [ask_id], out
    mine = [m for m in out["data"]["messages"] if m["msg_id"] == ask_id][0]
    assert mine["answered"] is False
    # 乙答了（msg_answer 带 reply_to）⇒ answered 翻真、unanswered_sent 清空
    r2 = await card_tools.send_card_message(
        wid, b, target_id=a, msg_type="msg_answer",
        content="我认为应该先做甲。", reply_to=ask_id)
    assert "error" not in r2, r2
    out2 = await card_tools.execute_tool(
        wid, a, "read_inbox", {"direction": "outgoing"}, cards)
    assert out2["ok"] is True, out2
    assert out2["data"]["unanswered_sent"] == [], out2
    mine2 = [m for m in out2["data"]["messages"] if m["msg_id"] == ask_id][0]
    assert mine2["answered"] is True


async def test_g2_auto_cards_can_talk_end_to_end(typed_registry, work, monkeypatch):
    """硬判据3（主链）：auto 卡模型可调 send_card_message 发消息；收件方可调
    read_inbox 读——K3 调起/S4 回发现有链对 auto 卡正常工作。"""
    wid, root = work["work_id"], work["work_card_id"]
    wid, a = await _mk_auto(work, goal="问乙一件事")   # _mk_auto 返 (wid, card_id)
    # 乙建成甲的子卡（同树闸放行父子互发——见 test_g2_read_inbox_outgoing_view 注）
    b = await _mk(wid, "乙", a)
    await card_store.update_card(wid, b, {"auto_mode": 1}, "开自动")

    seen: dict[str, list] = {"a_tools": [], "b_tools": []}

    async def fake(system_prompt, user_prompt, prior_messages=None, tools=None, **_):
        # 按 user_prompt 识别是哪张卡（两卡都是 auto 面，工具面无法区分）：
        # 乙的调起激励文带甲发出的消息正文；甲的起轮带目标/开始语。
        names = [t["name"] for t in (tools or [])]
        cid = b if "乙，报告一下现状" in str(user_prompt) else a
        seen.setdefault(cid, []).append(names)
        n = len(seen[cid])
        if cid == a:
            # 甲：第 1 轮发问；第 2 轮声明完成；此后空手交付（收敛，防无闸循环）
            if n == 1:
                return ({"content": "", "tool_calls": [{
                    "name": "send_card_message",
                    "arguments": {"target_card_id": b, "msg_type": "msg_ask",
                                  "content": "乙，报告一下现状"},
                }]}, "fake")
            if n == 2:
                return ({"content": "", "tool_calls": [{
                    "name": "update_goal", "arguments": {"action": "complete"},
                }]}, "fake")
            return ({"content": "甲已完成。", "tool_calls": []}, "fake")
        # 乙：第 1 轮读收件箱（看见问话）；第 2 轮应答；此后空手交付
        if n == 1:
            return ({"content": "", "tool_calls": [{
                "name": "read_inbox", "arguments": {},
            }]}, "fake")
        if n == 2:
            return ({"content": "", "tool_calls": [{
                "name": "send_card_message",
                "arguments": {"target_card_id": a, "msg_type": "msg_answer",
                              "content": "乙的现状：一切正常。", "reply_to": "ask"},
            }]}, "fake")
        return ({"content": "乙已应答。", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake)
    monkeypatch.setenv("MOCK_LLM", "1")

    await routes._run_message(wid, a, "开始问乙")
    await _drain_trigger_tasks()
    # 甲的工具面见过 send_card_message（auto 面下发），乙也被调起过
    assert any("send_card_message" in ns for ns in seen.get(a, [])), \
        f"甲的 auto 工具面未见 send_card_message：{seen.get(a)}"
    assert seen.get(b), "乙未被调起（K3 链对 auto 卡失效）"


# ============================================================
# 阶段 1 · G4 悬空兜底（8eaf33e + aa335ca + 61b83fb + b6e339a）
#
# 三条判据的机械面：
#   · 问出去的信必有回音或明确 blocked（不存在永久悬空）
#   · 判据用**实质事实**（answer 落库即算回应），不用会陈旧的 status——
#     否则双卡死锁（移交件已踩过 work_027）
#   · blocked 自动通知发问方
# ============================================================

async def test_g4_complete_blocked_by_unanswered_ask(typed_registry, work, no_kick):
    """硬判据1：带着未回应的 msg_ask 调 update_goal(complete) ⇒ 拒绝＋给出路。"""
    wid, root = work["work_id"], work["work_card_id"]
    wid, a = await _mk_auto(work, goal="做完这件事")
    b = await _mk(wid, "乙", a)   # 乙是 a 的子卡（父子可互发）
    # 乙向甲发一条 msg_ask（甲收件箱里有未回应询问）
    r = await card_tools.send_card_message(
        wid, b, target_id=a, msg_type="msg_ask", content="甲，这个数是多少？")
    assert "error" not in r, r
    cards = card_tools._collect_card_meta(wid)
    res = await card_tools.execute_tool(
        wid, a, "update_goal", {"action": "complete"}, cards)
    assert res["ok"] is False and res["error"]["code"] == "validation", res
    assert "未回应" in res["error"]["message"]
    assert "msg_answer" in res["error"]["message"], "拒绝必须自带出路（验收纪律4）"
    # 卡面零改动（被拒时 goal_state 仍是 active）
    assert card_store._meta_of(wid, a)["goal_state"] == "active"


async def test_g4_complete_passes_after_answer(typed_registry, work, no_kick):
    """硬判据1+3：答了（answer 落库）即可 complete——实质判据，不依赖
    会陈旧的 pending/consumed 状态（防双卡死锁）。"""
    wid, root = work["work_id"], work["work_card_id"]
    wid, a = await _mk_auto(work, goal="做完这件事")
    b = await _mk(wid, "乙", a)
    r = await card_tools.send_card_message(
        wid, b, target_id=a, msg_type="msg_ask", content="甲，这个数是多少？")
    ask_id = r["msg_id"]
    # 甲回应（msg_answer 带 reply_to）——注意**不做 consumed 置位**：
    # 判据若依赖 consumed 就会在此刻仍判「未回应」（陈旧窗）；实质判据应放行。
    r2 = await card_tools.send_card_message(
        wid, a, target_id=b, msg_type="msg_answer", content="是 42。",
        reply_to=ask_id)
    assert "error" not in r2, r2
    cards = card_tools._collect_card_meta(wid)
    res = await card_tools.execute_tool(
        wid, a, "update_goal", {"action": "complete"}, cards)
    assert res["ok"] is True, res
    assert card_store._meta_of(wid, a)["goal_state"] == "complete"


async def test_g4_blocked_notifies_sender(typed_registry, work, no_kick):
    """硬判据4：blocked ⇒ 系统自动给未获回答的发问方发 msg_notify（无回信义务）。"""
    wid, root = work["work_id"], work["work_card_id"]
    wid, a = await _mk_auto(work, goal="做完这件事")
    b = await _mk(wid, "乙", a)
    r = await card_tools.send_card_message(
        wid, b, target_id=a, msg_type="msg_ask", content="甲，这个数是多少？")
    ask_id = r["msg_id"]
    cards = card_tools._collect_card_meta(wid)
    res = await card_tools.execute_tool(
        wid, a, "update_goal",
        {"action": "blocked", "reason": "缺上游数据，做不下去"}, cards)
    assert res["ok"] is True, res
    assert card_store._meta_of(wid, a)["goal_state"] == "blocked"
    # 乙的收件箱收到一条 msg_notify（来自甲），且内容带 reason/原 ask msg_id
    rows = await card_db.list_inbox(wid, b)
    notes = [x for x in rows
             if str(x.get("from_card")) == a and str(x.get("type")) == "msg_notify"]
    assert len(notes) == 1, f"应有一条 blocked 通知，实得 {len(notes)}"
    assert "blocked" in str(notes[0].get("content"))
    assert ask_id in str(notes[0].get("content"))
    assert "缺上游数据" in str(notes[0].get("content"))
    # msg_notify 无回信义务（reply_expected=false）⇒ 不会造新义务/无循环
    assert card_msg.reply_expected_of("msg_notify") is False


def test_g4_auto_prompt_has_two_obligation_rules():
    """硬判据（aa335ca）：auto 版含两条义务式规则；普通版零泄漏；auto-t3。"""
    auto = prompts.AUTO_BASE_PROMPT
    assert "问出去的信必须有回音" in auto
    assert "没收到回复前不要认为任务完成" in auto
    assert "收到 ask 必须回应" in auto
    assert "不能无视" in auto
    assert "unanswered_sent" in auto
    assert prompts.AUTO_BASE_PROMPT_VERSION == "auto-t7"
    assert "问出去的信必须有回音" not in prompts.BASE_PROMPT
    assert "收到 ask 必须回应" not in prompts.BASE_PROMPT


def test_g4_respawn_guard_named_as_cycle_guard():
    """61b83fb/b6e339a：re-spawn 限次在册且明写「防环兜底，不是闸」（红线 1）。"""
    src = (REPO / "src" / "api" / "message_runner.py").read_text(encoding="utf-8")
    assert "_RESPAWN_MAX = 3" in src
    assert "card_msg_unanswered_giveup" in src
    assert "card_msg_respawn" in src
    assert "不是轮次/墙钟/token 闸" in src, "限次语义必须明写不是闸"


# ============================================================
# 阶段 2.7 · 空转兜底（TC3：止损，不是闸）
# ============================================================

async def test_p27_idle_stop_pauses_after_threshold(typed_registry, work, no_kick):
    """连续 _IDLE_PAUSE_THRESHOLD 轮零工具 ⇒ 自动 pause ＋ 不再起轮。

    注意：这不是闸——只处理「光说不做」这一种病态；有工具调用的轮永不计入。
    """
    wid, a = await _mk_auto(work, goal="把甲做完")
    th = auto_driver._IDLE_PAUSE_THRESHOLD
    for _ in range(th - 1):
        await auto_driver.after_turn(wid, a, tool_n=0)
        assert card_store._meta_of(wid, a)["goal_state"] == "active"
    await auto_driver.after_turn(wid, a, tool_n=0)   # 第 th 轮
    assert card_store._meta_of(wid, a)["goal_state"] == "paused"
    # 暂停后驱动器不再起轮
    assert auto_driver.will_continue(wid, a) is False


async def test_p27_tool_call_resets_idle_counter(typed_registry, work, no_kick):
    """中间任一轮有工具调用 ⇒ 空转计数清零（不误伤正常推进的长任务）。"""
    wid, a = await _mk_auto(work, goal="把甲做完")
    th = auto_driver._IDLE_PAUSE_THRESHOLD
    for _ in range(th - 1):
        await auto_driver.after_turn(wid, a, tool_n=0)
    await auto_driver.after_turn(wid, a, tool_n=2)   # 有工具 ⇒ 清零
    assert card_store._meta_of(wid, a)["goal_state"] == "active"
    for _ in range(th - 1):
        await auto_driver.after_turn(wid, a, tool_n=0)
    assert card_store._meta_of(wid, a)["goal_state"] == "active", "清零后应重新计数"


async def test_p27_unknown_tool_n_no_judgement(typed_registry, work, no_kick):
    """tool_n=None（无信息）⇒ 不判定：与 2.7 前行为逐字一致（向后兼容）。"""
    wid, a = await _mk_auto(work, goal="把甲做完")
    for _ in range(auto_driver._IDLE_PAUSE_THRESHOLD + 2):
        await auto_driver.after_turn(wid, a)   # 不传 tool_n
    assert card_store._meta_of(wid, a)["goal_state"] == "active"
    assert card_store._meta_of(wid, a)["goal_rounds"] >= 1, "正常驱动不受影响"


def test_p27_rounds_warn_is_not_a_gate():
    """红线 1：轮次告警线只告警不拦——常量在册且语义明写。"""
    src = (REPO / "src" / "api" / "auto_driver.py").read_text(encoding="utf-8")
    assert auto_driver._ROUNDS_WARN == 200
    assert "只告警不拦" in src
    assert "不设轮次上限" in src


# ============================================================
# 阶段 2 冒烟抓到的漏项回归（2026-10-17）
# ============================================================

async def test_stage2_patch_auto_mode_lands_and_reverts(typed_registry, work,
                                                         g1_client):
    """回归：PATCH updates.auto_mode 能落库并重算权限、关回能还原。

    冒烟实测抓到：ALLOWED_UPDATE_KEYS 漏 auto_mode（移植 Cardlo 时漏抄）⇒
    前端 toggleAutoMode 发 PATCH 会被 400「updates 含不允许的键」——开关整体
    不可用。本用例钉住：① 开自动 ⇒ auto_mode=1 ＋ perms=AUTO_SWITCHES（15，
    不含 delete）＋ version=-1；② 关回 ⇒ auto_mode=0 ＋ perms 集合还原。
    """
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "开关卡", root)
    pc = card_store._root_project_class(wid)
    base_meta = card_store._meta_of(wid, a)
    normal = set(base_meta[policy.PERM_KEY])

    r = await g1_client.patch(f"/api/works/{wid}/cards/{a}", json={
        "updates": {"auto_mode": 1}, "history_note": "[测试] 开自动"})
    assert r.status_code == 200, r.text
    m1 = card_store._meta_of(wid, a)
    assert m1["auto_mode"] == 1
    assert set(m1[policy.PERM_KEY]) == set(policy.AUTO_SWITCHES)
    assert m1[policy.PERM_VERSION_KEY] == policy.AUTO_PERMS_VERSION
    assert "delete" not in set(m1[policy.PERM_KEY]), "TC1：delete 不在全开集"

    r2 = await g1_client.patch(f"/api/works/{wid}/cards/{a}", json={
        "updates": {"auto_mode": 0}, "history_note": "[测试] 回手动"})
    assert r2.status_code == 200, r2.text
    m2 = card_store._meta_of(wid, a)
    assert m2["auto_mode"] == 0
    assert set(m2[policy.PERM_KEY]) == normal, "关回后权限集合应还原"


def test_auto_prompt_no_unconditional_restart_claim():
    """[6702 移植 P7] 自动版说明书不得再有无条件的「不声明就再起一轮」断言。

    根因：``will_continue`` 里有 ``has_active_child → return False`` ⇒ 父卡有在跑
    的子卡时，不声明**不会**再起轮，而是自然停住、子卡完工后被递归唤醒。
    旧文案无条件说"会再起一轮"，把模型逼去用 pause 表达"我在等"，才引出
    await_children / 唤醒 / 活锁那一整族机制。
    """
    from src.api import prompts
    sec = prompts._SEC_AUTO
    assert "- **只把话说完而不调用 update_goal，系统会当成「还没做完」，自动再起一轮。**" not in sec
    assert "只要没有在跑的子卡" in sec
    assert "你有正在跑的子卡时，直接结束回合即可" in sec


def test_idle_stop_skips_when_children_running(monkeypatch):
    """[6702 移植 P7] 有在跑的子卡时，零工具轮不得计入空转（让位等待≠空转）。"""
    import asyncio
    from src.api import auto_driver

    monkeypatch.setattr(auto_driver, "has_active_child", lambda w, c: True)
    auto_driver._IDLE_ROUNDS.clear()
    for _ in range(5):
        assert asyncio.run(auto_driver._idle_stop("w_test", "card_x", 0)) is False
    assert auto_driver._IDLE_ROUNDS.get(("w_test", "card_x"), 0) == 0
    auto_driver._IDLE_ROUNDS.clear()


def test_idle_stop_still_counts_without_children(monkeypatch):
    """[6702 移植 P7] 没有子卡时，空转兜底必须照旧生效（别把兜底改没了）。"""
    import asyncio
    from src.api import auto_driver

    monkeypatch.setattr(auto_driver, "has_active_child", lambda w, c: False)
    monkeypatch.setattr(auto_driver.card_store, "update_card", _async_noop)
    auto_driver._IDLE_ROUNDS.clear()
    assert asyncio.run(auto_driver._idle_stop("w_test2", "card_y", 0)) is False
    assert auto_driver._IDLE_ROUNDS[("w_test2", "card_y")] == 1
    auto_driver._IDLE_ROUNDS.clear()


def test_auto_prompt_version_after_p7():
    """[6702 移植 P7] 版本号：普通版不动，自动版到 auto-t7。"""
    from src.api import prompts
    assert prompts.BASE_PROMPT_VERSION == "t22-1"
    assert prompts.AUTO_BASE_PROMPT_VERSION == "auto-t7"


async def _async_noop(*a, **kw):
    return None
