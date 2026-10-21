# -*- coding: utf-8 -*-
"""[function HTTP 端点 2026-10-21 · card_034] 从系统外部创建/列出职能的通道。

判据对照（任务书验收判据）：
- 判据①/⑥: openapi.json 的 paths 出现 /works/{work_id}/functions（GET 与 POST）；
- 判据②：真实 POST 建成职能，function_id 形如 fn_NNN；
- 判据③：card_profiles 多 1 行、cards 表行数未增（职能不进卡片世界）；
- 判据④：GET 列出该职能，字段齐全；
- 判据⑤：缺 charter 的请求返回 4xx；缺 function_name 亦 4xx；work 不存在 404。

全部用例走**真实 HTTP 链路**（conftest 的 client 夹具挂 src.api.routes.router），
测的是同一批路由函数（与真服务同源）。
"""
from __future__ import annotations

import sqlite3

from src.storage import card_db, work_store


def _counts(work_id: str) -> tuple[int, int]:
    """只读核对：该 work 的 function_profiles 行数 与 cards 表行数。"""
    def _fn(conn: sqlite3.Connection):
        n_fn = int(conn.execute(
            "SELECT COUNT(*) FROM function_profiles WHERE work_id=?",
            (work_id,)).fetchone()[0])
        n_cards = int(conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
        return n_fn, n_cards
    return card_db.read_sync(work_id, _fn, (0, 0))


def _new_work_title() -> str:
    """幂等专用 work 标题：测试固定，避免各用例各建一个空剧本。"""
    return "HTTP职能端点核查"


async def _ensure_work(client) -> dict:
    """按固定标题复用/新建专用 work，返回 {work_id, work_card_id}。"""
    # 先扫已有 work，避免每次跑都堆一个空剧本（验收器 D 项同款幂等手法）。
    r = await client.get("/api/works")
    assert r.status_code == 200, r.text
    for w in (r.json().get("works") or []):
        if w.get("title") == _new_work_title():
            return {"work_id": w["work_id"], "work_card_id": w.get("work_card_id")}
    r = await client.post("/api/works", json={"title": _new_work_title()})
    assert r.status_code in (200, 201), r.text
    return {"work_id": r.json()["work_id"], "work_card_id": r.json().get("work_card_id")}


async def test_function_routes_in_openapi(client):
    """判据①：openapi.json 的 paths 里出现 /works/{work_id}/functions，GET/POST 都在。"""
    r = await client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    paths = spec.get("paths") or {}
    assert "/api/works/{work_id}/functions" in paths, "openapi paths 缺 functions 端点"
    methods = set(paths["/api/works/{work_id}/functions"].keys())
    assert {"get", "post"} <= methods, f"functions 端点缺方法: {methods}"


async def test_create_function_returns_fn_id_and_stays_out_of_cards(client):
    """判据②③：POST 建成职能 → function_id 形如 fn_NNN，cards 表行数未增、
    function_profiles 多 1 行。"""
    w = await _ensure_work(client)
    wid = w["work_id"]
    before_fn, before_cards = _counts(wid)

    r = await client.post(f"/api/works/{wid}/functions", json={
        "function_name": "质量管理",
        "charter": "对交付质量负责，发现缺口才发言，其余沉默",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    fid = body.get("function_id")
    assert fid and fid.startswith("fn_") and len(fid.split("_")[1]) == 3, f"function_id 形如 fn_NNN: {fid!r}"
    assert body.get("function_name") == "质量管理"
    assert body.get("in_cards_table") is False

    after_fn, after_cards = _counts(wid)
    assert after_fn == before_fn + 1, "function_profiles 应多 1 行"
    assert after_cards == before_cards, "cards 表行数不得因建职能增加（职能不进卡片世界）"


async def test_list_functions_returns_full_fields(client):
    """判据④：GET 列出该职能，字段齐全（function_id/function_name/duty/watch）。"""
    w = await _ensure_work(client)
    wid = w["work_id"]
    r = await client.post(f"/api/works/{wid}/functions", json={
        "function_name": "风险巡检",
        "charter": "负责巡检风险，发现异常才告警",
        "watch_card_ids": [],
    })
    assert r.status_code == 201, r.text
    fid = r.json()["function_id"]

    r = await client.get(f"/api/works/{wid}/functions")
    assert r.status_code == 200, r.text
    fns = r.json().get("functions") or []
    hit = [f for f in fns if f.get("function_id") == fid]
    assert hit, f"列表里应有新建职能 {fid}（现: {fns}）"
    f = hit[0]
    assert f.get("function_name") == "风险巡检"
    assert f.get("duty") == "active"
    assert "watch" in f


async def test_missing_charter_returns_4xx(client):
    """判据⑤：缺 charter 的请求返回 4xx + 明确 message。"""
    w = await _ensure_work(client)
    wid = w["work_id"]
    r = await client.post(f"/api/works/{wid}/functions", json={
        "function_name": "缺职责书",
        # 无 charter
    })
    assert r.status_code == 400, r.status_code
    assert "error" in r.json()


async def test_missing_function_name_returns_4xx(client):
    """判据⑤：缺 function_name 的请求返回 4xx + 明确 message。"""
    w = await _ensure_work(client)
    wid = w["work_id"]
    r = await client.post(f"/api/works/{wid}/functions", json={
        "charter": "有职责书但无职能名",
    })
    assert r.status_code == 400, r.status_code
    assert "error" in r.json()


async def test_unknown_work_returns_4xx(client):
    """判据⑤：work 不存在返回 404。"""
    r = await client.post("/api/works/work_99999/functions", json={
        "function_name": "幽灵职能",
        "charter": "不应建成",
    })
    assert r.status_code == 404
    assert "error" in r.json()
    r = await client.get("/api/works/work_99999/functions")
    assert r.status_code == 404