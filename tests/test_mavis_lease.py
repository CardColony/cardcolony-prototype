# -*- coding: utf-8 -*-
"""mavis_lease（MiniMax Code broker 租约桥）单测。

全部 mock：不依赖 MiniMax Code App 常驻、不碰真实管道、零网络。
真实全链验证（App 在跑时）见 tests/test_mavis_live.py（无 App 自动跳过）。
"""
from __future__ import annotations

import time

import pytest

from src.model_gateway import mavis_lease
from src.model_gateway.llm.errors import LlmError


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def client():
    return mavis_lease.MavisLeaseClient()


# ---------------------------------------------------------------- 缓存与续期


async def test_token_cached_until_near_expiry(client, monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(mavis_lease.time, "time", clock)
    calls = []

    def minter():
        calls.append(1)
        return {
            "accessToken": f"tok-{len(calls)}",
            "expiresAtMs": (clock.now + 600) * 1000,  # 10 分钟后过期
            "generation": len(calls),
            "audience": "agent-backend",
            "scopes": ["agent.default"],
        }

    client._minter = minter
    assert await client.get_token() == "tok-1"
    assert await client.get_token() == "tok-1"  # 仍新鲜 → 缓存
    assert len(calls) == 1

    # 推进到距过期 < _REFRESH_MARGIN_MS → 必须换新
    clock.now += 600 - mavis_lease._REFRESH_MARGIN_MS / 1000 + 1
    assert await client.get_token() == "tok-2"
    assert len(calls) == 2


async def test_minter_failure_propagates_loudly(client):
    def minter():
        raise LlmError("broker 不应允", "AUTH")

    client._minter = minter
    with pytest.raises(LlmError):
        await client.get_token()


async def test_already_expired_token_rejected(client, monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(mavis_lease.time, "time", clock)

    def minter():
        return {"accessToken": "stale", "expiresAtMs": clock.now * 1000 - 1, "generation": 1}

    client._minter = minter
    with pytest.raises(LlmError):
        await client.get_token()


async def test_concurrent_callers_share_one_mint(client, monkeypatch):
    import asyncio

    clock = _FakeClock()
    monkeypatch.setattr(mavis_lease.time, "time", clock)
    calls = []

    def slow_minter():
        calls.append(1)
        time.sleep(0.05)  # 同步慢铸（契约：minter 同步；锁仍应串行化）
        return {"accessToken": "tok", "expiresAtMs": (clock.now + 600) * 1000, "generation": 1}

    client._minter = slow_minter
    tokens = await asyncio.gather(*[client.get_token() for _ in range(5)])
    assert tokens == ["tok"] * 5
    assert len(calls) == 1  # 锁防惊群


# ---------------------------------------------------------------- 管道协议


def test_lease_request_shape(monkeypatch):
    """发出的请求帧负载：version/requestId/capability/method/minValidityMs。"""
    captured = {}

    def fake_roundtrip(pipe, payload):
        captured["pipe"] = pipe
        captured["payload"] = payload
        return {"ok": True, "result": {"accessToken": "t", "expiresAtMs": 1, "generation": 1}}

    monkeypatch.setattr(mavis_lease, "_pipe_roundtrip", fake_roundtrip)
    monkeypatch.setattr(mavis_lease, "find_broker_pipe", lambda: "\\\\.\\pipe\\mcode-auth-lease-" + "a" * 16)
    monkeypatch.setattr(mavis_lease, "_load_capability", lambda: "c" * 43)

    result = mavis_lease.mint_lease_token()
    assert result["accessToken"] == "t"
    payload = captured["payload"]
    assert payload["version"] == 1
    assert payload["method"] == "lease"
    assert payload["capability"] == "c" * 43
    assert 60_000 <= payload["minValidityMs"] <= 300_000  # 协议上下限
    assert captured["pipe"].startswith("\\\\.\\pipe\\mcode-auth-lease-")


def test_broker_rejection_raises(monkeypatch):
    monkeypatch.setattr(mavis_lease, "find_broker_pipe", lambda: "\\\\.\\pipe\\mcode-auth-lease-" + "b" * 16)
    monkeypatch.setattr(mavis_lease, "_load_capability", lambda: "c" * 43)
    monkeypatch.setattr(
        mavis_lease,
        "_pipe_roundtrip",
        lambda pipe, payload: {"ok": False, "error": {"code": "CAPABILITY_REJECTED"}},
    )
    with pytest.raises(LlmError, match="CAPABILITY_REJECTED"):
        mavis_lease.mint_lease_token()


def test_no_pipe_found_raises(monkeypatch):
    monkeypatch.setattr(mavis_lease, "find_broker_pipe", lambda: (_ for _ in ()).throw(
        LlmError("未找到 MiniMax Code 的鉴权 broker（App 未运行？）——mavis 座位不可用", "AUTH")
    ))
    with pytest.raises(LlmError, match="App 未运行"):
        mavis_lease.mint_lease_token()
