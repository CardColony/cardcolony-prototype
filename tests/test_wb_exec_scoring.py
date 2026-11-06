# -*- coding: utf-8 -*-
"""执行体表现分：中断扣分 / 低分永久除名 / 只有人工能加回。

用户令（2026-11-06）：「比如模型的表现，任务一直中断，就给它记个分值，
分值一低，就永久去掉，除非我说加回去」。
"""
import pytest

from src.api.wb_exec import scoring


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    p = tmp_path / "scores.json"
    monkeypatch.setattr(scoring, "STATE_PATH", p)
    scoring._cache = {}
    scoring._cache_stamp = (0.0, -1)
    yield
    scoring._cache = {}
    scoring._cache_stamp = (0.0, -1)


def test_interrupt_drops_score_then_bans():
    e = scoring.note_interrupt("wb-devin", "swe-2-max", reason="socket hang up")
    assert e["score"] == scoring.START_SCORE - scoring.PENALTY_INTERRUPT
    assert e["banned"] is False
    scoring.note_interrupt("wb-devin", "swe-2-max")
    e = scoring.note_interrupt("wb-devin", "swe-2-max")
    assert e["score"] <= scoring.BAN_BELOW
    assert e["banned"] is True
    assert scoring.is_banned("wb-devin", "swe-2-max") is True


def test_success_does_not_unban():
    for _ in range(6):
        scoring.note_interrupt("wb-oc", "flint-40b-free")
    assert scoring.is_banned("wb-oc", "flint-40b-free") is True
    # 真跑成也不解禁 ⇒ 「除非我说加回去」
    e = scoring.note_success("wb-oc", "flint-40b-free")
    assert e["banned"] is True
    assert scoring.is_banned("wb-oc", "flint-40b-free") is True


def test_restore_is_the_only_way_back():
    for _ in range(6):
        scoring.note_interrupt("wb-cn", "hy4free")
    assert scoring.is_banned("wb-cn", "hy4free") is True
    e = scoring.restore("wb-cn", "hy4free")
    assert e["banned"] is False
    assert e["score"] == scoring.START_SCORE
    assert scoring.is_banned("wb-cn", "hy4free") is False


def test_success_recovers_score_before_ban():
    scoring.note_interrupt("wb-cc", "reflection/beam:free")
    e = scoring.note_success("wb-cc", "reflection/beam:free")
    assert e["banned"] is False
    assert e["score"] == (scoring.START_SCORE
                         - scoring.PENALTY_INTERRUPT
                         + scoring.REWARD_SUCCESS)


def test_state_survives_cache_reset():
    """跨重启：清掉进程内缓存后，除名状态还在（确实落盘了）。"""
    for _ in range(6):
        scoring.note_interrupt("wb-cn", "hy3")
    scoring._cache = {}
    scoring._cache_stamp = (0.0, -1)
    assert scoring.is_banned("wb-cn", "hy3") is True
