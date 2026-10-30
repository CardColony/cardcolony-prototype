# -*- coding: utf-8 -*-
"""层内兜底（fallback）：每一层内部互相兜底，绝不跨层。

[2026-10-21 · 用户令] 「模型还是需要一个 fallback 机制，但不是原来那套用一个模型
兜所有底，而是按照层级策略，每一个层级内互相兜底」。

钉住五件事：
1. 建卡时表行**带上整层候选链**（首选 + fallbacks），不再挑一个就把池丢掉；
2. ``layer_fallbacks`` 只返回**同层**其余成员；
3. 网关按链逐个尝试，层内全挂才抛；
4. 流式**已经吐过字就不换模型**（否则同一轮输出两遍）；
5. 设置界面保存分层时拒绝发明模型，并重算已有卡的默认行。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api import model_config, model_service  # noqa: E402
from src.infra.errors import ErrorCode, ModelError  # noqa: E402
from src.model_gateway.gateway import ModelGateway  # noqa: E402
from src.storage import card_store, model_override, work_store  # noqa: E402


@pytest.fixture()
def gw(monkeypatch):
    """确定性网关：四层各有池，depth_0 是 m1/m2（有兜底），depth_1 只有一个。"""
    cfg = {
        "models": {"m1": {}, "m2": {}, "m3": {}, "m4": {}},
        "bindings": {},
        "system": {"default_model": "m1"},
        "tiers": {
            "depth_0": ["m1", "m2"],
            "depth_1": ["m3"],
            "depth_2": ["m4", "m2"],
            "default": ["m1"],
        },
    }
    monkeypatch.setattr(model_service, "_gateway", SimpleNamespace(config=cfg))
    return cfg


@pytest.fixture()
def ws(monkeypatch, tmp_path):
    root = tmp_path / "works"
    wd = root / "work_100"
    wd.mkdir(parents=True)
    (wd / "work.json").write_text(
        '{"work_id":"work_100","title":"Test","work_card_id":"card_001","created_at":"x"}',
        encoding="utf-8")
    monkeypatch.setattr(work_store, "WORKS_DIR", root)
    return root


@pytest.fixture()
def depth0(monkeypatch):
    """把这张卡钉在第 0 层（不造 cards.db）。"""
    monkeypatch.setattr(card_store, "_meta_of", lambda w, c: {"title": "t"})
    monkeypatch.setattr(model_config, "_card_depth", lambda w, c: 0)


# ---------------------------------------------------------------- 表行带链


def test_policy_row_carries_whole_layer_chain(gw, ws, depth0):
    """建卡填表时，**整层候选链**都要记下来——只挑一个写进去就无从兜底。"""
    row = model_config._policy_row("work_100", "card_001")
    assert row["model"] in ("m1", "m2")
    rest = row.get("fallbacks") or []
    assert rest, "同层还有另一个模型，必须进 fallbacks"
    assert set([row["model"]] + rest) == {"m1", "m2"}
    assert row["model"] not in rest, "首选不该重复出现在兜底链里"


def test_single_model_layer_has_no_fallbacks(gw, ws, monkeypatch):
    """层里只有一个模型 ⇒ 没有兜底（不是「去全局找一个」）。"""
    monkeypatch.setattr(card_store, "_meta_of", lambda w, c: {"title": "t"})
    monkeypatch.setattr(model_config, "_card_depth", lambda w, c: 1)
    row = model_config._policy_row("work_100", "card_002")
    assert row["model"] == "m3"
    assert not row.get("fallbacks")


# ---------------------------------------------------------------- 同层取兜底


def test_layer_fallbacks_stays_inside_layer(gw, ws, depth0):
    """[2026-10-26 用户令「不要兜底」] 兜底整体取消 ⇒ 恒返回空。

    改前断言 `== ["m2"]`（层内另一个模型进兜底链）；现在选了哪个就只跑哪个。
    用户原话：「我网页选哪个模型就是哪个模型，不需要兜底，何况兜底还不是我选的」。
    """
    model_override.put_row("work_100", "card_001",
                           {"model": "m1", "fallbacks": ["m2"]}, src="default")
    assert model_config.layer_fallbacks("m1", work_id="work_100",
                                        card_id="card_001") == []


def test_layer_fallbacks_never_crosses_layer(gw, ws, depth0):
    """兜底取消后，**任何**模型都不会被自动带进来（层内层外一视同仁）。"""
    model_override.put_row("work_100", "card_001",
                           {"model": "m1", "fallbacks": ["m2"]}, src="default")
    got = model_config.layer_fallbacks("m1", work_id="work_100", card_id="card_001")
    assert got == [], f"兜底已取消，不该返回任何兜底模型：{got}"


def test_fallback_chain_primary_first(gw, ws, depth0):
    model_override.put_row("work_100", "card_001",
                           {"model": "m2", "fallbacks": ["m1"]}, src="default")
    chain = model_config.fallback_chain("default", work_id="work_100",
                                        card_id="card_001")
    assert chain[0] == "m2"
    assert set(chain) == {"m1", "m2"}


def test_fallback_chain_heals_missing_fallbacks(gw, ws, depth0):
    """老行没有 fallbacks ⇒ 按当前层池补一次并写回表（自愈，主模型不动）。"""
    model_override.put_row("work_100", "card_001", {"model": "m1"}, src="default")
    chain = model_config.fallback_chain("default", work_id="work_100",
                                        card_id="card_001")
    assert chain[0] == "m1" and "m2" in chain
    row = model_override.get_row("work_100", "card_001") or {}
    assert row.get("fallbacks") == ["m2"], "自愈结果要落回表里，别每次现算"


# ---------------------------------------------------------------- 网关换人


class _FakeGateway(ModelGateway):
    """只实现单模型那一步；``call`` 的换人循环是真正被测的东西。"""

    def __init__(self, fail=(), partial=None):
        self.fail = set(fail)
        self.partial = set(partial or ())
        self.tried: list[str] = []

    async def _call_once(self, model_name, *args, **kwargs):
        self.tried.append(model_name)
        if model_name in self.fail:
            raise ModelError(ErrorCode.MODEL_CALL_FAILED, f"{model_name} 挂了")
        return {"content": model_name}

    async def _stream_once(self, model_name, *args, **kwargs):
        self.tried.append(model_name)
        if model_name in self.partial:
            yield {"chunk_type": "content", "chunk": "半句"}
            raise ModelError(ErrorCode.MODEL_CALL_FAILED, f"{model_name} 中途断")
        if model_name in self.fail:
            raise ModelError(ErrorCode.MODEL_CALL_FAILED, f"{model_name} 挂了")
        yield {"chunk_type": "content", "chunk": model_name}
        yield {"content": model_name}


@pytest.mark.asyncio
async def test_gateway_switches_within_layer():
    g = _FakeGateway(fail={"m1"})
    res = await g.call("m1", "sys", "user", fallbacks=["m2", "m3"])
    assert res["content"] == "m2"
    assert g.tried == ["m1", "m2"]


@pytest.mark.asyncio
async def test_gateway_raises_when_layer_exhausted():
    """层内全挂 ⇒ 如实失败，**不跨层**（没有第四个人可换）。"""
    g = _FakeGateway(fail={"m1", "m2", "m3"})
    with pytest.raises(ModelError):
        await g.call("m1", "sys", "user", fallbacks=["m2", "m3"])
    assert g.tried == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_gateway_dedupes_chain():
    g = _FakeGateway(fail={"m1"})
    await g.call("m1", "sys", "user", fallbacks=["m1", "", "m2", "m2"])
    assert g.tried == ["m1", "m2"]


@pytest.mark.asyncio
async def test_stream_switches_when_nothing_emitted():
    g = _FakeGateway(fail={"m1"})
    got = [ev async for ev in g.stream_call("m1", "sys", "user", fallbacks=["m2"])]
    assert g.tried == ["m1", "m2"]
    assert got[-1]["content"] == "m2"


@pytest.mark.asyncio
async def test_stream_never_switches_after_partial_output():
    """已经吐过字就不能换模型——否则用户看到半句话接另一个模型的开头。"""
    g = _FakeGateway(partial={"m1"})
    events = []
    with pytest.raises(ModelError):
        async for ev in g.stream_call("m1", "sys", "user", fallbacks=["m2"]):
            events.append(ev)
    assert g.tried == ["m1"], "吐过字之后不许再试第二个模型"
    assert [e.get("chunk") for e in events] == ["半句"]


# ---------------------------------------------------------------- 设置界面保存


def test_save_tiers_rejects_unknown_model(gw, ws, tmp_path, monkeypatch):
    p = tmp_path / "models.yaml"
    p.write_text("models: {m1: {}, m2: {}}\ntiers: {}\n", encoding="utf-8")
    monkeypatch.setattr(model_config, "CONFIG_PATH", p)
    res = model_config.save_tiers({"depth_0": ["m1", "nope"]})
    assert res["ok"] is False
    assert any("nope" in e for e in res["errors"])


def test_save_tiers_writes_and_reseeds(gw, ws, depth0, tmp_path, monkeypatch):
    p = tmp_path / "models.yaml"
    p.write_text("models: {m1: {}, m2: {}}\ntiers: {}\n", encoding="utf-8")
    monkeypatch.setattr(model_config, "CONFIG_PATH", p)
    # 已有卡按旧链（m4 不在 depth_0）
    model_override.put_row("work_100", "card_001",
                           {"model": "m4", "fallbacks": []}, src="default")
    res = model_config.save_tiers({"depth_0": ["m2", "m1"]})
    assert res["ok"] is True, res
    assert res["tiers"]["depth_0"] == ["m2", "m1"]
    assert (res.get("reseed") or {}).get("changed") == 1, "规则变了要重算已有默认行"
    row = model_override.get_row("work_100", "card_001") or {}
    assert row["model"] in ("m1", "m2")


def test_save_tiers_keeps_manual_rows(gw, ws, depth0, tmp_path, monkeypatch):
    """人手改过的行（src=manual）在保存分层时不动。"""
    p = tmp_path / "models.yaml"
    p.write_text("models: {m1: {}, m2: {}}\ntiers: {}\n", encoding="utf-8")
    monkeypatch.setattr(model_config, "CONFIG_PATH", p)
    model_override.put_row("work_100", "card_009", {"model": "m4"}, src="manual")
    res = model_config.save_tiers({"depth_0": ["m2", "m1"]})
    assert res["ok"] is True
    row = model_override.get_row("work_100", "card_009") or {}
    assert row["model"] == "m4", "人手改的行不许被重算覆盖"


@pytest.mark.asyncio
async def test_gateway_reports_actual_model_used():
    """切过人之后，结果里要写**真跑的是谁**——否则日志仍显示首选，排查被误导。"""
    g = _FakeGateway(fail={"m1"})
    res = await g.call("m1", "sys", "user", fallbacks=["m2"])
    assert res["model_used"] == "m2"


@pytest.mark.asyncio
async def test_stream_reports_actual_model_used():
    g = _FakeGateway(fail={"m1"})
    got = [ev async for ev in g.stream_call("m1", "sys", "user", fallbacks=["m2"])]
    final = [e for e in got if "chunk_type" not in e][-1]
    assert final["model_used"] == "m2"


def test_put_tiers_api_round_trip(gw, ws, depth0, tmp_path, monkeypatch):
    """设置界面保存分层走的就是这个接口：200 回新分层，未知模型 400。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api import settings_api

    p = tmp_path / "models.yaml"
    p.write_text("models: {m1: {}, m2: {}}\ntiers: {}\n", encoding="utf-8")
    monkeypatch.setattr(model_config, "CONFIG_PATH", p)

    app = FastAPI()
    app.include_router(settings_api.router)
    client = TestClient(app)

    r = client.put("/api/settings/tiers", json={"tiers": {"depth_0": ["m2", "m1"]}})
    assert r.status_code == 200, r.text
    assert r.json()["tiers"]["depth_0"] == ["m2", "m1"]

    bad = client.put("/api/settings/tiers", json={"tiers": {"depth_0": ["nope"]}})
    assert bad.status_code == 400
    assert any("nope" in e for e in bad.json()["errors"])


def test_put_tiers_api_round_trip(gw, ws, depth0, tmp_path, monkeypatch):
    """设置界面保存分层走的就是这个接口：200 回新分层，未知模型 400。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api import settings_api

    p = tmp_path / "models.yaml"
    p.write_text("models: {m1: {}, m2: {}}\ntiers: {}\n", encoding="utf-8")
    monkeypatch.setattr(model_config, "CONFIG_PATH", p)

    app = FastAPI()
    app.include_router(settings_api.router)
    client = TestClient(app)

    r = client.put("/api/settings/tiers", json={"tiers": {"depth_0": ["m2", "m1"]}})
    assert r.status_code == 200, r.text
    assert r.json()["tiers"]["depth_0"] == ["m2", "m1"]

    bad = client.put("/api/settings/tiers", json={"tiers": {"depth_0": ["nope"]}})
    assert bad.status_code == 400
    assert any("nope" in e for e in bad.json()["errors"])
