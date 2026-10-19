# -*- coding: utf-8 -*-
"""模型设置按项目/卡片覆盖（per-scope model override）。

覆盖四层：
1. 存储层：model_override 的 _clean 标准化、写/清空/读回。
2. 解析层：model_config.resolve / default_model 的优先序 card > work > global。
3. 校验层：validate_override 拒绝引用不存在模型的覆盖。
4. API 层：settings 的 model-overrides 端点（work/card 的 GET/PUT/DELETE、有效值、404/400）。

全部打 tmp 沙箱，不碰真实 works/ 与真实 models.yaml；gateway 换成确定性 stub，
使 model_names() / default 等与真实配置解耦。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api import model_config, model_service, settings_api  # noqa: E402
from src.storage import model_override, work_store  # noqa: E402


@pytest.fixture()
def gw(monkeypatch):
    """确定性网关：models={exec-global,m1,m2,m3,fb}；execute 绑定 exec-global；默认 m1。"""
    cfg = {
        "models": {"exec-global": {}, "m1": {}, "m2": {}, "m3": {}, "fb": {}},
        "bindings": {"execute": "exec-global"},
        "system": {"default_model": "m1", "fallback_model": "fb"},
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


# ---------------------------------------------------------------- 存储层


def test_clean_drops_empty_and_normalizes(ws):
    out = model_override._clean({
        "default_model": "m1", "fallback_model": "",
        "bindings": {"execute": "m2", "think": "", "viewpoint": ["m1", "m2"],
                     "gone": None, "empty_list": []},
    })
    assert out == {"default_model": "m1",
                   "bindings": {"execute": "m2", "viewpoint": ["m1", "m2"]}}


def test_update_clear_roundtrip(ws):
    model_override.update_work("work_100", {"default_model": "m2"})
    assert model_override.load_scope("work_100")[0]["default_model"] == "m2"

    model_override.update_card("work_100", "card_002", {"default_model": "m3"})
    _, co = model_override.load_scope("work_100", "card_002")
    assert co["default_model"] == "m3"

    model_override.clear_card("work_100", "card_002")
    assert model_override.load_scope("work_100", "card_002")[1] == {}

    model_override.clear_work("work_100")
    assert model_override.load("work_100") == {}


# ---------------------------------------------------------------- 解析层


def test_precedence_card_than_work_than_global(ws, gw):
    model_override.update_work("work_100",
                               {"default_model": "m2", "bindings": {"execute": "m2"}})
    model_override.update_card("work_100", "card_002",
                               {"default_model": "m3", "bindings": {"execute": "m3"}})

    assert model_config.default_model() == "m1"
    assert model_config.default_model(work_id="work_100") == "m2"
    assert model_config.default_model(work_id="work_100", card_id="card_002") == "m3"
    # 未覆盖的卡 → 回落 work 级
    assert model_config.default_model(work_id="work_100", card_id="card_009") == "m2"

    assert model_config.resolve("execute") == "exec-global"
    assert model_config.resolve("execute", work_id="work_100") == "m2"
    assert model_config.resolve("execute", work_id="work_100", card_id="card_002") == "m3"


def test_clear_falls_back_to_global(ws, gw):
    model_override.update_work("work_100", {"default_model": "m2"})
    model_override.update_card("work_100", "card_002", {"default_model": "m3"})

    model_override.clear_card("work_100", "card_002")
    assert model_config.default_model(work_id="work_100", card_id="card_002") == "m2"

    model_override.clear_work("work_100")
    assert model_config.default_model(work_id="work_100", card_id="card_002") == "m1"
    assert model_config.resolve("execute", work_id="work_100", card_id="card_002") == "exec-global"


def test_validate_override_rejects_unknown_models(gw):
    assert model_config.validate_override({"default_model": "nope"})
    assert model_config.validate_override({"bindings": {"execute": "nope"}})
    assert not model_config.validate_override(
        {"default_model": "m2", "bindings": {"execute": "m2"}})


# ---------------------------------------------------------------- API 层


@pytest.fixture()
def client(ws, gw, monkeypatch):
    from src.storage import card_store
    monkeypatch.setattr(card_store, "card_exists",
                        lambda wid, cid: cid == "card_002")
    app = FastAPI()
    app.include_router(settings_api.router)
    return TestClient(app)


def test_api_work_roundtrip_and_effective(client):
    r = client.put("/api/settings/model-overrides/work_100",
                   json={"default_model": "m2", "bindings": {"execute": "m2"}})
    assert r.status_code == 200, r.text
    assert r.json()["work"]["default_model"] == "m2"

    g = client.get("/api/settings/model-overrides/work_100").json()
    assert g["work"]["default_model"] == "m2"

    eff = client.get("/api/settings/model-overrides/work_100/effective").json()
    assert eff["default_model"] == "m2"
    assert eff["bindings"]["execute"] == "m2"

    assert client.delete("/api/settings/model-overrides/work_100").status_code == 200
    assert client.get("/api/settings/model-overrides/work_100").json()["work"] is None


def test_api_card_roundtrip(client):
    r = client.put("/api/settings/model-overrides/work_100/card/card_002",
                   json={"default_model": "m3"})
    assert r.status_code == 200, r.text
    g = client.get("/api/settings/model-overrides/work_100").json()
    assert g["cards"]["card_002"]["default_model"] == "m3"

    eff = client.get("/api/settings/model-overrides/work_100/effective?card_id=card_002").json()
    assert eff["default_model"] == "m3"

    client.delete("/api/settings/model-overrides/work_100/card/card_002")
    assert client.get("/api/settings/model-overrides/work_100").json()["cards"] == {}


def test_api_rejects_unknown_model(client):
    r = client.put("/api/settings/model-overrides/work_100", json={"default_model": "nope"})
    assert r.status_code == 400
    assert "nope" in r.json()["errors"][0]


def test_api_404_unknown_work(client):
    assert client.get("/api/settings/model-overrides/nope_work").status_code == 404


def test_api_404_unknown_card(client):
    r = client.put("/api/settings/model-overrides/work_100/card/nope_card",
                   json={"default_model": "m2"})
    assert r.status_code == 404


def test_api_list_marks_points(client):
    client.put("/api/settings/model-overrides/work_100", json={"default_model": "m2"})
    lst = client.get("/api/settings/model-overrides").json()["works"]
    hit = next((w for w in lst if w["work_id"] == "work_100"), None)
    assert hit is not None
    assert hit["has_work"] is True


# ---------------------------------------------------------------- 主回合接线


def test_pick_step_model_uses_card_override(ws, gw):
    """主 agent 回合真正走卡片覆盖：纯单模型下 _pick_step_model 取该卡默认模型。"""
    from src.api import message_runner
    from src.thinkchain import enabled as _tc_enabled

    if _tc_enabled():  # 若某测试把思维链开了，本断言前提不成立
        pytest.skip("thinkchain 已开启，跳过单模型口径断言")

    model_override.update_card("work_100", "card_002", {"default_model": "m3"})
    model_override.update_work("work_100", {"default_model": "m2"})

    state = message_runner.RunState(work_id="work_100", card_id="card_002", content="x")
    model = message_runner._pick_step_model(state)
    assert model == "m3", f"应取卡片覆盖 m3，实际 {model}"