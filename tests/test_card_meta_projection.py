# -*- coding: utf-8 -*-
"""[窄投影 2026-11-20 · 性能批次一] 卡面窄读的测试。

钉的事：
M1  窄读**没有 body 键**（调用方据此知道「正文未读」），且带 body_keys
    （body 键集 ＝ 整卡 body 的键集 ⇒ 前端两种读口下行为一致）。
M2  窄读的 meta 与整卡的 meta **同源同口径**：首屏要的判据
    （card_id/title/card_type/parent/source/auto_mode/goal_state…）逐字相同。
M3  窄读**不取正文大列**（mission/status_block/requirements/goal 都不在 meta 里）。
M4  路由层：``GET /cards/{cid}?projection=meta`` 走窄读；**不带参数时契约不变**
    （仍有 body，且键集与改前一致）。
M5  不存在的卡 ⇒ 404（与整卡同语义）。
M6  exec-backend 读口也走窄读（它只要 card_type，不该白付正文 IO）。
"""
from __future__ import annotations

import json

import pytest

from src.api import routes
from src.storage import card_store, work_store


async def _seed_work(title: str = "窄投影") -> str:
    wdata = await work_store.create_work(title)
    return str(wdata["work_id"])


async def _mk_card(wid: str, title: str, card_type: str = "sub_project") -> str:
    r = await card_store.create_card(wid, title, "模型生成",
                                     card_type=card_type, skip_kick=True)
    return str(r["meta"]["card_id"])


# ---------------------------------------------------------------- M1 形状

async def test_meta_projection_has_no_body_but_has_body_keys(sandbox):
    wid = await _seed_work()
    cid = await _mk_card(wid, "宿主卡")

    light = await card_store.read_card_meta(wid, cid)
    assert "body" not in light, "窄读不得回正文（正文是右栏卡详情的区域载荷）"
    assert "meta" in light
    assert set(light["body_keys"]) == set(card_store.CARD_BODY_KEYS), (
        f"body_keys 必须与整卡 body 的键集同源：{light['body_keys']}")


# ---------------------------------------------------------------- M2 同源同口径

async def test_meta_projection_matches_full_meta_on_first_screen_fields(sandbox):
    wid = await _seed_work()
    cid = await _mk_card(wid, "宿主卡")
    # 写点正文与运行态，让窄读不能靠「默认值」蒙混过关
    await card_store.update_card(wid, cid, {"mission": "使命正文", "status_block": "现状正文"}, "测试写入")

    full = await card_store.read_card(wid, cid)
    light = await card_store.read_card_meta(wid, cid)

    for key in ("card_id", "title", "card_type", "parent", "source",
                "auto_mode", "goal_state", "goal_rounds"):
        assert light["meta"].get(key) == full["meta"].get(key), (
            f"meta.{key} 窄读与整卡必须一致："
            f"窄={light['meta'].get(key)!r} 整={full['meta'].get(key)!r}")
    assert light["meta"].get("tags") == full["meta"].get("tags")
    assert light["meta"].get("links") == full["meta"].get("links")


# ---------------------------------------------------------------- M3 不取正文大列

async def test_meta_projection_excludes_body_columns(sandbox):
    wid = await _seed_work()
    cid = await _mk_card(wid, "宿主卡")
    await card_store.update_card(wid, cid, {"mission": "使命正文"}, "测试写入")

    light = await card_store.read_card_meta(wid, cid)
    for key in ("mission", "status_block", "requirements", "goal"):
        assert key not in light["meta"], (
            f"meta.{key} 是正文大列，不该出现在窄读里")


# ---------------------------------------------------------------- M4 路由契约

async def test_route_projection_meta_returns_narrow_shape(sandbox):
    wid = await _seed_work()
    cid = await _mk_card(wid, "宿主卡")
    await card_store.update_card(wid, cid, {"mission": "使命正文"}, "测试写入")

    body = await routes.get_work_card(wid, cid, projection="meta")
    assert "body" not in body, "projection=meta 不得回正文"
    assert body["meta"]["card_id"] == cid
    assert body["meta"]["title"] == "宿主卡"


async def test_route_default_contract_unchanged(sandbox):
    """不带 projection 时**逐字不变**：仍有 body，键集与改前一致。"""
    wid = await _seed_work()
    cid = await _mk_card(wid, "宿主卡")
    await card_store.update_card(wid, cid, {"mission": "使命正文"}, "测试写入")

    body = await routes.get_work_card(wid, cid)
    assert "body" in body, "默认读口必须仍然回正文（旧入口兼容性）"
    assert set(body["body"]) == set(card_store.CARD_BODY_KEYS)
    assert body["body"]["mission"] == "使命正文"


# ---------------------------------------------------------------- M5 不存在

async def test_route_meta_projection_404_for_missing_card(sandbox):
    wid = await _seed_work()
    resp = await routes.get_work_card(wid, "card_does_not_exist", projection="meta")
    assert resp.status_code == 404


# ---------------------------------------------------------------- M6 exec-backend

async def test_exec_backend_reads_narrow_meta(sandbox, monkeypatch):
    """exec-backend 只要 card_type ⇒ 不该再走整卡读（白付 500KB 正文 IO）。"""
    wid = await _seed_work()
    cid = await _mk_card(wid, "执行体卡", card_type="worker")
    await card_store.update_card(wid, cid, {"mission": "使命正文"}, "测试写入")

    called = {"full": 0}
    orig_read = card_store.read_card

    async def _spy(work_id, card_id):
        called["full"] += 1
        return await orig_read(work_id, card_id)

    monkeypatch.setattr(card_store, "read_card", _spy)
    body = await routes.get_exec_backend(wid, cid)
    assert body["card_type"] == "worker"
    assert called["full"] == 0, "exec-backend 不该再拉整张卡（正文 IO 是白付）"
