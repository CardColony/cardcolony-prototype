# -*- coding: utf-8 -*-
"""★ **优先级瀑布**（2026-11-04 用户令）的单测。

用户令原话：「**先全部排 devin**，你得测它并发，**它排满了或额度用完了，就落
opencode**，opencode 不行，再落 workbuddy」。

⇒ 这份文件钉死三件事：
  ① **顺序**：按 ``priority``（越小越优先），不是按权重配比；
  ② **溢流**：上一级**排满**（占不到槽）或**额度用完** ⇒ **立刻落下一级**
     —— 不是干等、**更不是回落系统模型（＝花钱）**；
  ③ **全满**：全都排满 ⇒ 在**最高优先级**那一路**排队**（仍然不花钱）。
"""
from __future__ import annotations

import json

import pytest

from src.api.wb_exec import bridge, picker
from src.api.wb_exec import config as wb_config


# ===================================================================== 夹具
def _write_cfg(tmp_path, candidates, executors=None):  # noqa: ANN001, ANN201
    raw = {
        "enabled": True,
        "executors": executors or [
            {"id": "wb-devin", "label": "Devin", "install_root": "", "port": 0,
             "devin_cli_exe": "devin.exe", "devin_max_concurrent": 2},
            {"id": "wb-oc", "label": "opencode", "install_root": "", "port": 0,
             "oc_serve_exe": "opencode.exe", "oc_pool_size": 2},
            {"id": "wb-cn", "label": "CN", "install_root": "", "port": 0},
        ],
        "candidates": candidates,
        "fallback": {"models": ["hy3"], "prefer_same_executor": True},
        "on_unavailable": {"mode": "fallback_system"},
        "scope": {"mode": "all"},
    }
    p = tmp_path / "wb_exec.json"
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture
def cfg_priority(tmp_path, monkeypatch):
    """四路优先级表：devin → opencode → wb-cn → hy3（兜底）。"""
    p = _write_cfg(tmp_path, [
        {"executor": "wb-devin", "model": "swe-2-max", "weight": 0, "priority": 10},
        {"executor": "wb-oc", "model": "flint-40b-free", "weight": 0, "priority": 20},
        {"executor": "wb-cn", "model": "glm-5.3-flash", "weight": 0, "priority": 30},
        {"executor": "wb-cn", "model": "hy3", "weight": 0, "priority": 90},
    ])
    monkeypatch.setenv(wb_config.ENV_CONFIG_PATH, str(p))
    wb_config.reset_cache()
    try:
        yield wb_config.load()
    finally:
        wb_config.reset_cache()


class _FakeState:
    def __init__(self) -> None:
        self.work_id = "work_x"
        self.card_id = "card_x"
        self.rounds = 1


class _FakeReg:
    """只实现 ``_select_candidate`` 用到的那几个方法。"""

    def __init__(self, *, full=(), exhausted=(), unavailable=()) -> None:
        self.full = set(full)              # 占不到槽的执行体
        self.exhausted = set(exhausted)    # (executor, model)
        self.unavailable = set(unavailable)
        self.reserved: list[tuple] = []

    def is_exhausted(self, executor_id, model):
        return (str(executor_id), str(model)) in self.exhausted

    def exhausted_reason(self, executor_id, model):
        return "额度耗尽" if self.is_exhausted(executor_id, model) else ""

    def model_unavailable(self, executor_id, model):
        return (str(executor_id), str(model)) in self.unavailable

    async def try_reserve(self, executor_id, model="", work_dir="", holder="",
                          force=False):
        if force:
            self.reserved.append((executor_id, model, "force"))
            return True
        if executor_id in self.full:
            return False
        self.reserved.append((executor_id, model, "normal"))
        return True

    def release_reserve(self, executor_id, holder=""):
        return None


# ===================================================================== ① 顺序
def test_priority_order_follows_priority_field_not_weight():
    """**顺序只认 priority**（weight 不再分流）。"""
    cands = [
        wb_config.Candidate("a", "m1", 40, priority=30),
        wb_config.Candidate("b", "m2", 99, priority=10),
        wb_config.Candidate("c", "m3", 1, priority=20),
    ]
    got = [(c.executor, c.model) for c in picker.priority_order(cands)]
    assert got == [("b", "m2"), ("c", "m3"), ("a", "m1")]


def test_priority_order_defaults_to_table_order():
    """**没写 priority ⇒ 按表里的行序**（第一行＝最高优先级）。"""
    cands = [
        wb_config.Candidate("a", "m1", 40),
        wb_config.Candidate("b", "m2", 30),
        wb_config.Candidate("c", "m3", 0),
    ]
    got = [(c.executor, c.model) for c in picker.priority_order(cands)]
    assert got == [("a", "m1"), ("b", "m2"), ("c", "m3")]


def test_cascade_chain_skips_current_and_tried():
    """**降级链＝优先级顺序**（不是"只跳 fallback.models"）。"""
    cands = [
        wb_config.Candidate("devin", "swe-2-max", 0, priority=10),
        wb_config.Candidate("oc", "flint-40b-free", 0, priority=20),
        wb_config.Candidate("cn", "glm-5.3-flash", 0, priority=30),
        wb_config.Candidate("cn", "hy3", 0, priority=90),
    ]
    chain = picker.cascade_chain(cands, cands[0])
    assert [(c.executor, c.model) for c in chain] == [
        ("oc", "flint-40b-free"), ("cn", "glm-5.3-flash"), ("cn", "hy3")]
    # 已试过的要跳过
    chain2 = picker.cascade_chain(cands, cands[0], {("oc", "flint-40b-free")})
    assert [(c.executor, c.model) for c in chain2] == [
        ("cn", "glm-5.3-flash"), ("cn", "hy3")]


# ===================================================================== ② 溢流
@pytest.mark.asyncio
async def test_picks_top_priority_when_everything_is_free(cfg_priority,
                                                           monkeypatch):
    """**全都空着 ⇒ 挑最高优先级那一档**（devin 吃满流量）。"""
    reg = _FakeReg()
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)
    cand, notes = await bridge._select_candidate(cfg_priority, _FakeState(), "D:/w")
    assert cand is not None
    assert (cand.executor, cand.model) == ("wb-devin", "swe-2-max")
    assert notes == []
    assert reg.reserved == [("wb-devin", "swe-2-max", "normal")]


@pytest.mark.asyncio
async def test_saturated_top_priority_spills_to_next(cfg_priority, monkeypatch):
    """★ **devin 排满 ⇒ 立刻落 opencode**（用户令：「它排满了…就落 opencode」）。"""
    reg = _FakeReg(full={"wb-devin"})
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)
    cand, notes = await bridge._select_candidate(cfg_priority, _FakeState(), "D:/w")
    assert cand is not None
    assert (cand.executor, cand.model) == ("wb-oc", "flint-40b-free")
    assert any(n.get("executor") == "wb-devin" and "排满" in str(n.get("reason"))
               for n in notes), notes


@pytest.mark.asyncio
async def test_exhausted_top_priority_spills_to_next(cfg_priority, monkeypatch):
    """★ **devin 额度用完 ⇒ 立刻落 opencode**（用户令：「额度用完了，就落 opencode」）。"""
    reg = _FakeReg(exhausted={("wb-devin", "swe-2-max")})
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)
    cand, notes = await bridge._select_candidate(cfg_priority, _FakeState(), "D:/w")
    assert cand is not None
    assert (cand.executor, cand.model) == ("wb-oc", "flint-40b-free")
    assert any("额度耗尽" in str(n.get("reason")) for n in notes), notes


@pytest.mark.asyncio
async def test_all_saturated_queues_on_top_priority(cfg_priority, monkeypatch):
    """★ **全都排满 ⇒ 在最高优先级那一路排队**（**不回落系统模型＝不花钱**）。"""
    reg = _FakeReg(full={"wb-devin", "wb-oc", "wb-cn"})
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)
    cand, notes = await bridge._select_candidate(cfg_priority, _FakeState(), "D:/w")
    assert cand is not None, "全满时不该返回 None（None＝回落系统模型＝花钱）"
    assert (cand.executor, cand.model) == ("wb-devin", "swe-2-max")
    # 强制预留（超编排队）⇒ 占用数算得对 ⇒ 后来的卡会正确溢流
    assert reg.reserved and reg.reserved[-1][2] == "force"


@pytest.mark.asyncio
async def test_all_unavailable_returns_none(cfg_priority, monkeypatch):
    """**额度全尽 ⇒ 返回 None**（这时才回落系统模型，并如实记账）。"""
    reg = _FakeReg(exhausted={("wb-devin", "swe-2-max"),
                              ("wb-oc", "flint-40b-free"),
                              ("wb-cn", "glm-5.3-flash"),
                              ("wb-cn", "hy3")})
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)
    cand, notes = await bridge._select_candidate(cfg_priority, _FakeState(), "D:/w")
    assert cand is None
    assert len(notes) == 4


# ===================================================================== ③ 容量算术
@pytest.mark.asyncio
async def test_registry_capacity_counts_reservations(cfg_priority, monkeypatch):
    """**Devin 的并发闸**：`devin_max_concurrent=2` ⇒ 第三个必须占不到（⇒ 溢流）。"""
    from src.api.wb_exec.registry import ExecutorRegistry
    reg = ExecutorRegistry()
    assert await reg.try_reserve("wb-devin", "swe-2-max", "D:/w", "h1") is True
    assert await reg.try_reserve("wb-devin", "swe-2-max", "D:/w", "h2") is True
    assert await reg.try_reserve("wb-devin", "swe-2-max", "D:/w", "h3") is False
    # 释放一个 ⇒ 又能占到了
    reg.release_reserve("wb-devin", "h1")
    assert await reg.try_reserve("wb-devin", "swe-2-max", "D:/w", "h3") is True
    # 同一个 holder 重复占是幂等的（不重复计数）
    assert await reg.try_reserve("wb-devin", "swe-2-max", "D:/w", "h3") is True


@pytest.mark.asyncio
async def test_registry_force_reserve_over_capacity(cfg_priority, monkeypatch):
    """**强制预留（排队）可以超编**（用户要的排队，不是 bug）。"""
    from src.api.wb_exec.registry import ExecutorRegistry
    reg = ExecutorRegistry()
    for i in range(3):
        assert await reg.try_reserve("wb-devin", "swe-2-max", "D:/w", f"h{i}",
                                     force=True) is True
    assert len(reg.reservations_snapshot().get("wb-devin", {})) == 3
