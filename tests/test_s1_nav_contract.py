# -*- coding: utf-8 -*-
"""[左侧边栏 S1] 后端接口族验收（任务书 V1~V6 沙箱腿；U2 真实计时另跑脚本）。

经 httpx.ASGITransport 走真实 HTTP 层（conftest 沙箱，不碰真实 works）；
activity.WORKS_DIR 是 conftest 盲区（T17 同款，本文件逐用例补齐铁律一）。
U1（旧前端真实渲染不崩）＝用户确认项，不在本套件；
U2（version <50ms）＝真实 works 只读实测，见 scripts/_s1_u2_timing_20261011.py。
"""
from __future__ import annotations

import httpx
from fastapi import FastAPI

# [左栏接线 步1] 字段定格 8→9：增 card_type（消费方＝左栏「工具卡不进侧栏」过滤）。
# 追实依据＝接线方案 步1（docs/exploration/左侧边栏-接线方案-20261012.md）；
# 契约文档追实排在步 5（清尾），此处先追测试防红。
FIELD_SET = {"card_id", "title", "parent", "child_count",
             "last_active_at", "branch_active_at", "work_id", "state",
             "card_type"}


def _client():
    from src.api.routes import router
    app = FastAPI()
    app.include_router(router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


async def _mkcard(wid: str, title: str, parent: str) -> None:
    from src.storage import card_store
    r = await card_store.create_card(wid, title, "用户直接创建", parent=parent)
    assert "error" not in r, r


def _cid_by_title(wid: str, title: str) -> str:
    from src.storage import card_store
    for m in card_store.iter_card_metas(wid).values():
        if m.get("title") == title:
            return m["card_id"]
    raise AssertionError(f"找不到卡: {title}")


async def _set_col(wid: str, cid: str, col: str, value) -> None:
    """直改库列（[卡片存储迁移 SQLite] 原为改 md front matter）。

    用于模拟存量卡缺字段：把该列写成 NULL。
    """
    from src.storage import card_db

    def _fn(conn):
        card_db.update_card_row(conn, cid, {col: value}, {})

    await card_db.run_write(wid, _fn)


async def test_v1_v2_v4_contract_new_card(sandbox, work, monkeypatch):
    """V1 work_id 归属正确｜V2 字段恰好 9 项｜V4 新建卡 state=active＋落盘＋201。"""
    from src.storage import activity, card_store
    monkeypatch.setattr(activity, "WORKS_DIR", sandbox.works)
    wid, root = work["work_id"], work["work_card_id"]
    await _mkcard(wid, "S1验收子卡A", root)
    async with _client() as client:
        # V4-③ 建卡 HTTP 201（闸4 放行：无迁移报错）
        r = await client.post(f"/api/works/{wid}/cards",
                              json={"title": "S1验收子卡B", "source": "用户直接创建",
                                    "parent": root})
        assert r.status_code == 201, r.text
        # V1/V2/V4-① 接口面
        r = await client.get("/api/nav")
        assert r.status_code == 200
        cards = next(p for p in r.json()["projects"]
                     if p["work_id"] == wid)["cards"]
        assert len(cards) == 2
        for c in cards:
            assert set(c.keys()) == FIELD_SET, c        # V2 恰好 9 项无多余无短缺
            assert c["work_id"] == wid                  # V1 归属==所在项目
            assert c["state"] == "active"               # V4-① 新建＝active
    # V4-② 落库：新建卡 state 列非空、读侧 meta 可见（[卡片存储迁移 SQLite] 原断言 md 行）
    from src.storage import card_store
    metas = card_store.iter_card_metas(wid)
    new_ids = [cid for cid in metas if cid != root]
    assert len(new_ids) == 2
    for cid in new_ids:
        assert metas[cid]["state"] == "active", cid


async def test_v4_legacy_card_empty_state(sandbox, work, monkeypatch):
    """V4-①/④ 存量无 state 卡：接口返回空串、其余字段完整、不崩（零迁移）。"""
    from src.storage import activity, card_store
    monkeypatch.setattr(activity, "WORKS_DIR", sandbox.works)
    wid, root = work["work_id"], work["work_card_id"]
    await _mkcard(wid, "将被削成存量的卡", root)
    cid = _cid_by_title(wid, "将被削成存量的卡")
    # 模拟存量：库内 state 列置 NULL（沙箱内改写，不涉真实数据）
    await _set_col(wid, cid, "state", None)
    async with _client() as client:
        r = await client.get("/api/nav")
        assert r.status_code == 200
        c = next(p for p in r.json()["projects"]
                 if p["work_id"] == wid)["cards"][0]
        assert c["state"] == ""                         # 存量＝空串
        assert c["card_id"] == cid
        assert c["title"] == "将被削成存量的卡"          # 其余字段完整不崩


async def test_v5_children_endpoint(sandbox, work, monkeypatch):
    """V5：结构｜同契约｜全直系｜parent=""＝第一层且排除根卡｜404｜child_count 对账。"""
    from src.storage import activity
    monkeypatch.setattr(activity, "WORKS_DIR", sandbox.works)
    wid, root = work["work_id"], work["work_card_id"]
    await _mkcard(wid, "一层卡A", root)
    await _mkcard(wid, "二层卡B", _cid_by_title(wid, "一层卡A"))
    cid_a = _cid_by_title(wid, "一层卡A")
    async with _client() as client:
        r = await client.get("/api/nav")
        nav_cards = {c["card_id"]: c for c in next(
            p for p in r.json()["projects"] if p["work_id"] == wid)["cards"]}

        # ① 结构＋② 契约同套
        r = await client.get("/api/nav/children", params={"work_id": wid, "parent": ""})
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"work_id", "parent", "cards"}
        assert body["work_id"] == wid and body["parent"] == ""
        # ④ parent=""＝第一层，且**排除根卡**（根卡 parent="" 是分组头，与 /api/nav 同口径）
        ids = [c["card_id"] for c in body["cards"]]
        assert ids == [cid_a], f"第一层应只含 A（根卡排除）: {ids}"
        assert root not in ids
        for c in body["cards"]:
            assert set(c.keys()) == FIELD_SET           # ② 契约与全量同一套
        # ⑤ parent 非空＝该卡直系；全直系
        r = await client.get("/api/nav/children",
                             params={"work_id": wid, "parent": cid_a})
        kids = r.json()["cards"]
        assert [c["card_id"] for c in kids] and \
               all(c["parent"] == cid_a for c in kids)   # ③ 全直系无深层混入
        # 显式 parent=根卡 → 同第一层
        r = await client.get("/api/nav/children",
                             params={"work_id": wid, "parent": root})
        assert [c["card_id"] for c in r.json()["cards"]] == [cid_a]
        # 叶子层空
        cid_b = kids[0]["card_id"]
        r = await client.get("/api/nav/children",
                             params={"work_id": wid, "parent": cid_b})
        assert r.json()["cards"] == []
        # ⑥ child_count 与全量对账（A 应为 1：B 挂它下面）
        assert body["cards"][0]["child_count"] == nav_cards[cid_a]["child_count"] == 1
        # ⑤ 404：work 不存在 / parent 不存在
        r = await client.get("/api/nav/children",
                             params={"work_id": "work_999", "parent": ""})
        assert r.status_code == 404
        r = await client.get("/api/nav/children",
                             params={"work_id": wid, "parent": "card_999"})
        assert r.status_code == 404


async def test_v6_version_endpoint(sandbox, work, monkeypatch):
    """V6：结构｜不变性｜敏感性（改动仅本 work 指纹变）｜删卡敏感性（实现层修订补强）。"""
    from src.api.routes import get_nav_version
    from src.storage import activity, work_store
    monkeypatch.setattr(activity, "WORKS_DIR", sandbox.works)
    wid, root = work["work_id"], work["work_card_id"]
    v1 = await get_nav_version()
    assert set(v1.keys()) == {"version", "by_work"}        # ① 结构
    v2 = await get_nav_version()
    assert v1 == v2                                        # ② 不变性
    # 造第二个 work 证 work 级差分隔离
    w2 = await work_store.create_work("第二剧本")
    assert "error" not in w2
    await _mkcard(wid, "敏感性探针卡", root)
    v3 = await get_nav_version()
    assert v3["version"] != v1["version"]                  # ③ 变化
    assert v3["by_work"][wid] != v1["by_work"][wid]        # 仅该 work 变
    for k, v in v1["by_work"].items():
        if k != wid:
            assert v3["by_work"][k] == v                   # 其他 work 不动
    # 删卡敏感性：任务书 T6「mtime 取 max」的盲区——实现层修订（列表哈希）的补强断言
    from src.storage import card_store
    probe = next(cid for cid in card_store.iter_card_metas(wid) if cid != root)
    v4 = await get_nav_version()
    await card_store.delete_card(wid, probe)
    v5 = await get_nav_version()
    assert v5["by_work"][wid] != v4["by_work"][wid]        # 删卡必须被捕捉
    # 内容修改敏感性（改卡 → 库 rev 变）
    await _mkcard(wid, "追加探针卡", root)
    v6 = await get_nav_version()
    await card_store.update_card(
        wid, _cid_by_title(wid, "追加探针卡"), {"status_block": "内容变了"}, "改内容")
    v7 = await get_nav_version()
    assert v7["by_work"][wid] != v6["by_work"][wid]


# ============================================================
# [左栏接线 步1] 增列 card_type ＋ /api/nav/running 总览
# ============================================================

async def test_step1_card_type_field(sandbox, work, monkeypatch):
    """card_type：新建卡有值｜存量（削掉该行）返回空串不崩｜两端口同套。"""
    from src.storage import activity, card_store
    monkeypatch.setattr(activity, "WORKS_DIR", sandbox.works)
    wid, root = work["work_id"], work["work_card_id"]
    await _mkcard(wid, "类型探针卡", root)
    async with _client() as client:
        r = await client.get("/api/nav")
        c = next(p for p in r.json()["projects"]
                 if p["work_id"] == wid)["cards"][0]
        assert c["card_type"] == "sub_project", c      # 新建卡类型有值

        r = await client.get("/api/nav/children",
                             params={"work_id": wid, "parent": root})
        assert r.json()["cards"][0]["card_type"] == "sub_project"   # 两端点同契约

    # 模拟存量：库内 card_type 列置 NULL → 空串、其余字段完整、不崩（零迁移）
    await _set_col(wid, _cid_by_title(wid, "类型探针卡"), "card_type", None)
    async with _client() as client:
        r = await client.get("/api/nav")
        c = next(p for p in r.json()["projects"]
                 if p["work_id"] == wid)["cards"][0]
        assert c["card_type"] == ""                    # 存量＝空串
        assert c["card_id"] == _cid_by_title(wid, "类型探针卡")   # 其余字段完整不崩
        assert c["title"] == "类型探针卡"


async def test_step1_running_overview(sandbox, work, monkeypatch):
    """/api/nav/running：结构｜活跃态列到｜终止态不列｜纯读零写盘。"""
    from src.api import progress
    from src.storage import activity
    monkeypatch.setattr(activity, "WORKS_DIR", sandbox.works)
    wid, root = work["work_id"], work["work_card_id"]

    progress._store.clear()                            # 隔离用例间内存态
    async with _client() as client:
        r = await client.get("/api/nav/running")
        assert r.status_code == 200
        assert set(r.json().keys()) == {"running"}     # ① 结构
        assert r.json()["running"] == []               # ② 空态如实为空

        # ③ 活跃态列到（写点声明 active=True 的阶段）
        progress.set_stage(wid, root, "synthesize", "三席观点统合中")
        r = await client.get("/api/nav/running")
        got = r.json()["running"]
        assert len(got) == 1 and got[0]["card_id"] == root and got[0]["work_id"] == wid
        assert got[0]["stage"] == "synthesize"

        # ④ 终止态不列（done/error/idle 不复活——防「早就跑完了」被误标运行中）
        progress.set_stage(wid, root, "done", "完成")
        assert (await client.get("/api/nav/running")).json()["running"] == []

        # ⑤ 心跳不复活终止态（与 set_stage 同口径防漂移）
        progress.heartbeat(wid, root)
        assert (await client.get("/api/nav/running")).json()["running"] == []

        # ⑥ 多卡并发列举
        progress.set_stage(wid, "card_777", "tools", "查证中")
        progress.set_stage(wid, "card_888", "streaming")
        ids = {x["card_id"] for x in (await client.get("/api/nav/running")).json()["running"]}
        assert ids == {"card_777", "card_888"}

    progress._store.clear()
