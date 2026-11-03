# -*- coding: utf-8 -*-
"""★ **验收卡 / 执笔卡 / coder 卡用同一套路由**（2026-11-04 用户令）。

用户令原话：「**验收卡、pm卡，本质上应该和coder卡用一套路由机制，而不是分开的**」。

本文件钉死三件事：
  ① **同一套路由**：三类卡都走同一张候选表、同一个优先级瀑布、同一个额度状态
     ⇒ 判定不再按卡型分叉；
  ② **单一事实源**：**卡型白名单在配置里**（``delegable_card_types``）⇒
     后端判定、接口返回、**前端渲染门控**共用同一份 ⇒ 前端不再硬编码
     ``card_type === "coder"``（那正是"看起来分成两套"的可见面）；
  ③ **前端机械守卫**：源码级断言，锁住"前端不自己判卡型"这条契约
     （前端没有 jsdom 载体，机械断言是既有做法，见 test_msg_baseclose_frontend.py）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.api.wb_exec import bridge, config as wb_config

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_JS = ROOT / "static" / "workspace.js"
EXEC_JS = ROOT / "static" / "esm" / "exec_backend.js"


# ===================================================================== 夹具
async def _make_card(work_id: str, card_type: str, title: str = "卡") -> str:
    from src.storage import card_store
    card = await card_store.create_card(
        work_id, title, "模型生成", card_type=card_type,
        charter="## 使命\n干活的使命", goal="干活的使命", skip_kick=True)
    assert "error" not in card, card
    return str(card["meta"]["card_id"])


@pytest.fixture
def cfg_all_types(tmp_path, monkeypatch):
    """白名单 = coder + qa_card + pm_card（**配置化**）。"""
    p = tmp_path / "wb_exec.json"
    p.write_text(json.dumps({
        "enabled": True,
        "delegable_card_types": ["coder", "qa_card", "pm_card"],
        "executors": [{"id": "wb-cn", "label": "CN", "install_root": "",
                       "cli_pool_size": 1}],
        "candidates": [{"executor": "wb-cn", "model": "hy3", "weight": 0,
                        "priority": 10}],
        "fallback": {"models": ["hy3"]},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv(wb_config.ENV_CONFIG_PATH, str(p))
    wb_config.reset_cache()
    try:
        yield wb_config.load()
    finally:
        wb_config.reset_cache()


# ===================================================================== ① 同一套路由
@pytest.mark.asyncio
async def test_three_card_types_share_one_routing(work, cfg_all_types):
    """★ **验收卡 / 执笔卡 / coder 卡都能走执行体**（同一套路由，不是分开）。"""
    cfg = wb_config.load()
    for ct in ("coder", "qa_card", "pm_card"):
        cid = await _make_card(work["work_id"], ct, f"{ct} 卡")
        assert await bridge.is_delegable_card(
            work["work_id"], cid, cfg) is True, ct


@pytest.mark.asyncio
async def test_non_delegable_card_type_is_skipped(work, cfg_all_types):
    """★ **不在白名单里的卡型不委派**（不误伤别的卡）。"""
    cfg = wb_config.load()
    cid = await _make_card(work["work_id"], "review", "评审卡")
    assert await bridge.is_delegable_card(work["work_id"], cid, cfg) is False


@pytest.mark.asyncio
async def test_whitelist_is_configurable(tmp_path, monkeypatch, work):
    """★ **白名单改配置就改判定**（不再硬编码在代码里）。"""
    p = tmp_path / "wb_exec.json"
    p.write_text(json.dumps({
        "enabled": True,
        "delegable_card_types": ["coder", "function"],
        "executors": [{"id": "wb-cn", "label": "CN", "install_root": "",
                       "cli_pool_size": 1}],
        "candidates": [{"executor": "wb-cn", "model": "hy3", "weight": 0,
                        "priority": 10}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv(wb_config.ENV_CONFIG_PATH, str(p))
    wb_config.reset_cache()
    try:
        cfg = wb_config.load()
        assert cfg.delegable_card_types == ("coder", "function")
        fn_card = await _make_card(work["work_id"], "function", "职能卡")
        qa_card = await _make_card(work["work_id"], "qa_card", "验收卡")
        assert await bridge.is_delegable_card(work["work_id"], fn_card, cfg) is True
        assert await bridge.is_delegable_card(work["work_id"], qa_card, cfg) is False
    finally:
        wb_config.reset_cache()


@pytest.mark.asyncio
async def test_exec_backend_endpoint_reports_delegable(work, client, cfg_all_types):
    """★ **接口明说这张卡能不能走执行体**（前端照它渲染，不再自己判卡型）。"""
    for ct, want in (("coder", True), ("qa_card", True), ("pm_card", True),
                      ("review", False)):
        cid = await _make_card(work["work_id"], ct, f"{ct} 卡")
        r = await client.get(f"/api/works/{work['work_id']}/cards/{cid}/exec-backend")
        assert r.status_code == 200
        data = r.json()
        assert data["card_type"] == ct
        assert data["delegable"] is want, (ct, data)
        # ★ 白名单也一并回给前端（诊断用）
        assert "delegable_card_types" in data


# ===================================================================== ③ 前端机械守卫
def test_frontend_does_not_gate_on_coder_card_type():
    """★ **前端不再硬编码 ``card_type === "coder"`` 门控执行体块**。

    那是"看起来分成两套路由"的**可见面**：验收卡/执笔卡卡面上根本没有入口。
    ⇒ 现在门控由**后端**的 ``delegable`` 决定。
    """
    text = WORKSPACE_JS.read_text(encoding="utf-8")
    assert "execInfo.delegable" in text, "前端没按后端的 delegable 渲染执行体块"
    # ★ 不许再出现按 coder 卡型硬编码的执行体门控
    bad = re.findall(r"card_type\s*===\s*[\"']coder[\"'][^\n]*exec", text)
    assert not bad, f"前端又按 coder 卡型门控执行体块了: {bad}"


def test_frontend_copy_is_not_workbuddy_only():
    """★ 前端文案**不再写死 WorkBuddy**（现在有 Devin / opencode / WorkBuddy）。"""
    text = EXEC_JS.read_text(encoding="utf-8")
    assert "走 WorkBuddy 执行体" not in text
    assert "谁干活：WorkBuddy 还是系统自己的模型" not in text
    assert "外部执行体" in text


def test_frontend_version_bumped():
    """★ 前端改过 ⇒ **``?v=`` 版本号必须升**（缓存失效的唯一手段）。"""
    text = WORKSPACE_JS.read_text(encoding="utf-8")
    m = re.search(r"exec_backend\.js\?v=([0-9.]+)", text)
    assert m, "workspace.js 里找不到 exec_backend.js 的版本引用"
    assert m.group(1) != "1.0", "exec_backend.js 改过了却没升版本号"
