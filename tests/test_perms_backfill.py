# -*- coding: utf-8 -*-
"""[5713 存量卡 perms 回溯 · card_036] 沙箱测试。

覆盖（任务书验收④ 的 ①—④ 条）：
- dry_run 后库里 perms 逐字未变（且库文件 hash 一致）——回填 dry-run 零写；
- apply 后旧快照卡 perms 与当前配置一致（无 tools_file/tools_bash、有
  tools_read_file）——配置演进真正回填；
- apply 后该卡其它字段（title/parent/auto_mode/goal/goal_rounds/使命正文）
  一行不变——只写 perms/perms_version 两字段；
- worker 卡跑同一函数不丢执行权（tools_file/tools_web 保留，与真实 v9 配置
  worker 行一致）——不自己发明规则；
- apply 幂等：复跑 applied=0。

沙箱：conftest autouse 把 WORKS_DIR 指到 tmp；本套件自指真实
config/card_type_registry.json（建 sub_project / worker 卡需要，与
test_worker_card.py 同一手法）。**不碰任何真实 work。**
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from src.permissions import backfill, policy
from src.storage import card_db, card_store, work_store

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def perm_registry(monkeypatch):
    """建 sub_project / worker 卡需真实注册表（conftest 沙箱默认无注册表降级）。"""
    monkeypatch.setattr(
        card_store, "TYPE_REGISTRY_PATH",
        REPO / "config" / "card_type_registry.json",
    )


async def _make_work():
    """建一个干净 work，返回 (work_id, root_card_id)。"""
    wdata = await work_store.create_work("perms 回填测试", "rd_project")
    assert "error" not in wdata, wdata
    return wdata["work_id"], wdata["work_card_id"]


async def _make_card(wid, root_id, card_type, title, mission, goal=""):
    res = await card_store.create_card(
        wid, title, "模型生成", card_type=card_type,
        parent=root_id, mission=mission, goal=goal,
    )
    assert "error" not in res, res
    return res["meta"]["card_id"]


def _seed_old_snapshot(wid, card_id, perms_list, perms_version=6, auto_mode=1):
    """直接写库模拟「存量卡当年按旧配置落库的快照」（绕开建卡期当前配置计算）。

    这是**唯一**贴近真实的方式：存量卡的 perms 是历史落盘值，不是建卡产物；
    测试里用 SQL 直写，模拟那一段历史。含 tools_bash——历史/未知开关名
    （不在当前 SWITCHES 词汇表），正是回填要清掉的「过期快照」证据。
    """
    db = card_db.db_path(wid)
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "UPDATE cards SET perms=?, perms_version=?, auto_mode=? WHERE card_id=?",
            (json.dumps(perms_list, ensure_ascii=False), perms_version, auto_mode,
             card_id),
        )
        con.commit()
    finally:
        con.close()
    card_db.reset_caches()  # 让后续读经新连接看到已提交值


def _read_perms_raw(wid, card_id) -> str:
    db = card_db.db_path(wid)
    con = sqlite3.connect(str(db))
    try:
        return con.execute(
            "SELECT perms FROM cards WHERE card_id=?", (card_id,)
        ).fetchone()[0]
    finally:
        con.close()


def _read_row(wid, card_id) -> sqlite3.Row:
    db = card_db.db_path(wid)
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT * FROM cards WHERE card_id=?", (card_id,)
        ).fetchone()
    finally:
        con.close()


def _db_sha(wid) -> str:
    return hashlib.sha256(card_db.db_path(wid).read_bytes()).hexdigest()


#: 旧配置快照：管理卡当年握着执行权（tools_file/tools_bash/tools_web）
OLD_ADMIN_SNAPSHOT = [
    "read", "edit", "tools_file", "tools_bash", "tools_web", "shadow_write",
]


@pytest.mark.asyncio
async def test_dry_run_does_not_touch_db(perm_registry):
    """验收④①：dry_run 后库里 perms 逐字未变，且库文件 hash 一致（零写）。"""
    wid, root_id = await _make_work()
    sub_id = await _make_card(wid, root_id, "sub_project",
                              "旧快照子卡", "使命正文A", goal="目标A")
    _seed_old_snapshot(wid, sub_id, OLD_ADMIN_SNAPSHOT, auto_mode=1)

    raw_before = _read_perms_raw(wid, sub_id)
    sha_before = _db_sha(wid)

    report = backfill.recompute_perms_for_work(wid, dry_run=True)

    # dry-run 报告：该卡在待改列表，diff 正确（去掉执行权）
    item = next(c for c in report["changed"] if c["card_id"] == sub_id)
    assert item["auto_mode"] == 1
    assert "tools_file" in item["removed"]
    assert "tools_bash" in item["removed"]
    # 〔v14 · 2026-10-24〕tools_web 不再被移除（所有卡型都有：查资料）
    assert "tools_web" not in item["removed"]
    assert "tools_read_file" in item["added"]
    assert report["applied"] == 0

    # 库里逐字未变 + 库文件 hash 一致
    assert _read_perms_raw(wid, sub_id) == raw_before
    assert _db_sha(wid) == sha_before


@pytest.mark.asyncio
async def test_apply_recomputes_perms_and_preserves_other_fields(perm_registry):
    """验收④②③：apply 后旧快照卡 perms 对齐当前配置（无 tools_file/tools_bash、
    有 tools_read_file），其它字段一行不变。"""
    wid, root_id = await _make_work()
    sub_id = await _make_card(wid, root_id, "sub_project",
                              "旧快照子卡", "使命正文A", goal="目标A")
    _seed_old_snapshot(wid, sub_id, OLD_ADMIN_SNAPSHOT, auto_mode=1)

    # apply 前记录其它字段（逐字）
    before = _read_row(wid, sub_id)
    fields = ["title", "parent", "auto_mode", "goal", "goal_rounds", "mission"]
    before_vals = {f: before[f] for f in fields}

    report = backfill.recompute_perms_for_work(wid, dry_run=False)
    assert report["applied"] == 1

    target = set(policy.compute_perms("rd_project", "sub_project")[0])
    after = _read_row(wid, sub_id)
    after_perms = json.loads(after["perms"])
    assert set(after_perms) == target
    assert "tools_file" not in after_perms
    assert "tools_bash" not in after_perms
    # 〔v14 · 2026-10-24〕tools_web **不再被回填移除**——所有卡型都有（查资料）；
    # 被回填收走的仍是**动手面**（tools_file / tools_bash）。
    assert "tools_web" in after_perms
    assert "tools_read_file" in after_perms
    assert after["perms_version"] == policy.config_version()

    # 其它字段一行不变
    for f in fields:
        assert after[f] == before_vals[f], f"字段 {f} 被改动"
    # auto_mode 保持 1（存量自动卡被回填、不被跳过，也不被改写）
    assert after["auto_mode"] == 1


@pytest.mark.asyncio
async def test_worker_keeps_execution_switches(perm_registry):
    """验收④④：worker 卡跑同一函数，tools_file/tools_web 执行权不被削——
    与真实 v9 配置 worker 行一致（不自己发明规则）。"""
    wid, root_id = await _make_work()
    cod_id = await _make_card(wid, root_id, "worker", "旧快照worker 卡", "使命正文B")
    # 旧快照：worker 卡也握着执行权，但缺了当前 worker 行的 read/edit 等
    _seed_old_snapshot(wid, cod_id, ["tools_file", "tools_web", "shadow_write"],
                       auto_mode=0)

    report = backfill.recompute_perms_for_work(wid, dry_run=False)
    assert report["applied"] == 1

    target = set(policy.compute_perms("rd_project", "worker")[0])
    after_perms = json.loads(_read_perms_raw(wid, cod_id))
    assert set(after_perms) == target
    assert "tools_file" in after_perms
    assert "tools_web" in after_perms


@pytest.mark.asyncio
async def test_apply_is_idempotent(perm_registry):
    """验收④（附加）：apply 幂等——复跑 applied=0、changed=0，不再写库。"""
    wid, root_id = await _make_work()
    sub_id = await _make_card(wid, root_id, "sub_project",
                              "旧快照子卡", "使命正文A")
    _seed_old_snapshot(wid, sub_id, OLD_ADMIN_SNAPSHOT, auto_mode=1)

    first = backfill.recompute_perms_for_work(wid, dry_run=False)
    assert first["applied"] == 1

    second = backfill.recompute_perms_for_work(wid, dry_run=False)
    assert second["applied"] == 0
    assert second["changed"] == []


@pytest.mark.asyncio
async def test_resolve_work_id_accepts_id_and_path(perm_registry):
    """resolve_work_id：work_id 与目录路径两种入参都能解析成同一 work_id。"""
    wid, _root = await _make_work()
    work_dir = card_store.WORKS_DIR / wid
    assert backfill.resolve_work_id(wid) == wid
    assert backfill.resolve_work_id(str(work_dir)) == wid
    assert backfill.resolve_work_id(str(work_dir / "cards.db").replace("cards.db", "")) == wid
    with pytest.raises(ValueError):
        backfill.resolve_work_id("不存在_work_999")


@pytest.mark.asyncio
async def test_pc_unresolvable_goes_to_failed_not_diff(perm_registry):
    """[card_039 返工] 卡上 project_class 为空 + 根卡取不到 pc ⇒ **不产出 diff**、
    而是记进 failed。

    旧代码缺陷：pc 两路都空时 `_target_perms` 调 `compute_perms('', type)` 返回
    **空集**，被当成「目标权限＝空」→ 生成「去掉全部开关」的 diff；一旦 --apply
    就把该卡权限清空、失去所有工具能力。空集在 compute_perms 里是「该组合未配置
    ⇒ 全拒」语义，**不是**「应回填成空权限」。本条断言：查不到 ⇒ 跳过 + failed。
    """
    wid, root_id = await _make_work()
    sub_id = await _make_card(wid, root_id, "sub_project", "无 pc 子卡", "使命X")
    _seed_old_snapshot(wid, sub_id, OLD_ADMIN_SNAPSHOT, auto_mode=1)

    # 构造「根卡取不到 pc」：
    # 1) work.json 的 work_card_id 指向不存在的卡 → card_store._root_project_class 返回空串；
    # 2) 根卡自身 project_class 也置 NULL → 兜底（从根卡 meta 直读）同样取不到。
    wj = card_store.WORKS_DIR / wid / "work.json"
    data = json.loads(wj.read_text(encoding="utf-8"))
    data["work_card_id"] = "card_999"  # 不存在
    wj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    db = card_db.db_path(wid)
    con = sqlite3.connect(str(db))
    try:
        con.execute("UPDATE cards SET project_class=NULL WHERE card_id=?", (root_id,))
        con.commit()
    finally:
        con.close()
    card_db.reset_caches()

    assert card_store._root_project_class(wid) == ""

    report = backfill.compute_perms_report(wid)

    # 该卡不产出 diff
    assert not any(c["card_id"] == sub_id for c in report["changed"]), \
        "pc 解析失败时不得生成 diff（旧代码会把它算成『清空权限』）"
    # 而是记进 failed，且原因可辨（pc 解析失败 / 目标为空集）
    failed = {f["card_id"]: f["error"] for f in report["failed"]}
    assert sub_id in failed, f"该卡应记进 failed，实际 failed={failed}"
    reason = failed[sub_id]
    assert ("pc" in reason) or ("project_class" in reason) or ("空" in reason), reason
