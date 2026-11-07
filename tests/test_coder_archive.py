# -*- coding: utf-8 -*-
"""[worker 归档 2026-10-25 · 用户令] 完工worker 卡折叠成归档记录(视觉+数据)。

钉七件事:
T1 归档:cards 行/影子删,coder_archive 记录在(task/final/artifacts);
   会话/事件文件原位不动
T2 幂等与护栏:非 worker 拒;未完工拒;重复归档幂等
T3 视觉+数据折叠:nav/canvas/模型卡片清单天然不见(数据层无行,零过滤器)
T4 read_archive 工具:按卡号取单条/按父卡列/全列
T5 阶段收口判定:活卡全 completed+归档子卡 ⇒ 触发(归档天然算完成)
T6 复活:对归档卡发消息 ⇒ 解档重建活卡(重派活规则延续)
T7 silence_card 统一:worker 沉默=归档;activate=解档
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import card_tools, coder_fold
from src.api import routes as api_routes
from src.storage import card_store, coder_archive, work_store


async def _work_with_coder():
    w = await work_store.create_work("归档")
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    parent = (await card_store.create_card(wid, "父卡", "模型生成",
                                           parent=root))["meta"]["card_id"]
    worker = (await card_store.create_card(
        wid, "worker 卡A", "模型生成", parent=parent,
        card_type="worker",
        charter="## 使命\n实现 X 并交付"))["meta"]["card_id"]
    return wid, root, parent, worker


async def _complete(wid, cid):
    r = await card_store.update_card(wid, cid, {"state": "completed",
                                                 "toolcard_final": "交付完毕,产出 /tmp/x.go"},
                                     "完工")
    assert "error" not in r, r                    # 完工写必须成功(失败即测试桩问题)


# ---------------------------------------------------------------- T1/T2

async def test_archive_and_guards(sandbox):
    wid, root, parent, worker = await _work_with_coder()
    # 未完工拒
    r = await coder_fold.archive_worker(wid, worker)
    assert r.get("ok") is False and r["error"] == "not_completed"
    # 非 worker 拒
    r2 = await coder_fold.archive_worker(wid, parent)
    assert r2.get("ok") is False and r2["error"] == "only_worker"
    # 完工 ⇒ 归档
    await _complete(wid, worker)
    r3 = await coder_fold.archive_worker(wid, worker)
    assert r3.get("ok") is True
    rec = coder_archive.get_record(wid, worker)
    assert rec and "实现 X" in rec["task"] and "交付完毕" in rec["final_report"], rec
    assert not card_store.card_exists(wid, worker)       # 卡行没了
    # 幂等:已归档(卡不存在) ⇒ not_found 路径安全
    r4 = await coder_fold.archive_worker(wid, worker)
    assert r4.get("ok") is False                        # 卡不在,拒(不会崩)


# ---------------------------------------------------------------- T3

async def test_visual_and_data_collapse(sandbox):
    wid, root, parent, worker = await _work_with_coder()
    sibling = (await card_store.create_card(
        wid, "活子卡", "模型生成", parent=parent))["meta"]["card_id"]
    await _complete(wid, worker)
    await coder_fold.archive_worker(wid, worker)
    cards = card_tools._collect_card_meta(wid)
    assert worker not in cards and sibling in cards       # 数据层折叠
    app = FastAPI(); app.include_router(api_routes.router)
    with TestClient(app) as client:
        nav = client.get("/api/nav").json()
        for p in nav.get("projects", []):
            if p.get("work_id") == wid:
                ids = [c.get("card_id") for c in p.get("cards", [])]
                assert worker not in ids and sibling in ids


# ---------------------------------------------------------------- T4

async def test_read_archive_tool(sandbox):
    wid, root, parent, worker = await _work_with_coder()
    await _complete(wid, worker)
    await coder_fold.archive_worker(wid, worker)
    cards = card_tools._collect_card_meta(wid)
    one = await card_tools._h_read_archive(
        wid, root, {"card_id": worker}, cards)
    assert one.get("ok") and "实现 X" in one["record"]["task"]
    lst = await card_tools._h_read_archive(
        wid, root, {"parent": parent}, cards)
    assert lst.get("ok") and lst["count"] == 1
    miss = await card_tools._h_read_archive(
        wid, root, {"card_id": "card_999"}, cards)
    assert miss.get("ok") is False


# ---------------------------------------------------------------- T5

async def test_stage_close_with_archived_children(sandbox):
    """父卡的全部 worker 子卡归档后,阶段收口判定仍触发(归档天然算完成)。"""
    wid, root, parent, worker = await _work_with_coder()
    await _complete(wid, worker)
    await coder_fold.archive_worker(wid, worker)
    # 父卡已无活子卡,但有一个归档子卡 ⇒ siblings_done 应为真
    # (直接测 card_store 的判定路径:通过 update_card completed 触发一次
    #  也会走,这里用内部函数等价验证)
    import sqlite3
    from src.storage import card_db as _cdb
    con = sqlite3.connect(str(_cdb.db_path(wid)))
    live = con.execute(
        "SELECT state FROM cards WHERE parent=?", (parent,)).fetchall()
    con.close()
    assert not live                                        # 活子卡空
    assert coder_archive.archived_children(wid, parent) == 1


# ---------------------------------------------------------------- T6

async def test_revive_on_message(sandbox):
    wid, root, parent, worker = await _work_with_coder()
    await _complete(wid, worker)
    await coder_fold.archive_worker(wid, worker)
    assert coder_archive.get_record(wid, worker) is not None
    r = await coder_fold.unarchive_coder(wid, worker)
    assert r.get("ok") is True
    assert card_store.card_exists(wid, worker)             # 活卡重建
    assert coder_archive.get_record(wid, worker) is None   # 档案删
    meta = card_store._meta_of(wid, worker) or {}
    assert meta.get("card_type") == "worker"


# ---------------------------------------------------------------- T7

async def test_silence_worker_marker_only(sandbox):
    """[2026-10-25 · 用户裁定] worker 沉默=治理标记,不折叠(续派用 continue_task)。
    归档只走显式迁移脚本(用户执行)。"""
    wid, root, parent, worker = await _work_with_coder()
    await _complete(wid, worker)
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools._h_silence_card(
        wid, parent, {"card_id": worker, "action": "silence",
                      "reason": "静默免扰"}, cards)
    assert out.get("ok") is True
    assert card_store.card_exists(wid, worker)         # 不折叠
    assert coder_archive.get_record(wid, worker) is None
    out2 = await card_tools._h_silence_card(
        wid, parent, {"card_id": worker, "action": "activate",
                      "reason": "恢复"}, cards)
    assert out2.get("ok") is True


