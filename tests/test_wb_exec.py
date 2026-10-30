# -*- coding: utf-8 -*-
"""[wb_exec 2026-10-23] WorkBuddy 执行体（coder 卡委派）的测试。

**不碰真实 WorkBuddy**：本文件自带一个**假 ACP 网关**（FastAPI + uvicorn 线程），
复刻实测到的 WorkBuddy 网关契约：
- ``GET  /api/v1/health``            → ``{"data":{"status":"ok"}}``
- ``POST /api/v1/acp/connect``       → ``{"data":{"connectionId","sessionToken"}}``
- ``POST /api/v1/acp``               → SSE（``:ok`` 首帧 → 通知 → JSON-RPC 结果）
- 必带头 ``x-codebuddy-request: 1``（缺 ⇒ 403）

**隔离**：全程走 conftest 的沙箱（works/logs/快照根都在 tmp_path），
不 import main、不起真网关、不发真消息。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from src.api import event_stream, message_runner
from src.api.wb_exec import (acp_client, bridge, config as wb_config, picker,
                              switch as wb_switch, watchdog)
from src.storage import card_store, work_store

REPO = Path(__file__).resolve().parents[1]


class _FakeCliWorker:
    """假的 CLI 池进程（alive 恒 True）。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def alive(self) -> bool:
        return True

    def stop(self) -> None:
        pass


def _use_cli_pool(reg, monkeypatch, gw_ref, executor_id: str = "wb-cn"):
    """把假网关当成**原生 CLI 池里的一个进程**注入注册表。

    2026-10-24 用户令：执行体**只走原生 CLI**（桌面端那条路已物理删除）
    ⇒ 测试不能再注入"桌面端网关"。

    **多次调用共享一张表**（按 executor_id 分派）⇒ 两个执行体各一个进程。
    """
    from src.api.wb_exec.registry import GatewayProcess
    from src.api.wb_exec.config import Executor

    ex = Executor(id=executor_id, label=executor_id, install_root="",
                  product_config="", cli_bin="")
    gw = GatewayProcess(
        executor=ex, port=int(gw_ref.base_url.rsplit(":", 1)[1]),
        base_url_override=gw_ref.base_url, source="cli", credentialed=True)
    gw.cli_worker = _FakeCliWorker(gw_ref.base_url)

    table = getattr(reg, "_test_cli_pool_map", None)
    # **每个用例一张新表**（registry 是进程级单例 ⇒ 上一个用例留下的假网关
    #   已经关掉了 ⇒ 必须重建，不能跨用例复用）
    current = getattr(type(reg), "_acquire_cli", None)
    if table is None or getattr(current, "__name__", "") != "_acquire":
        table = {}
        reg._test_cli_pool_map = table
        # ★ 并发闸/槽位表也是**进程级单例** ⇒ 上一个用例残留的锁会让本用例死等
        reg._gw_sems.clear()
        reg._slots.clear()

        async def _acquire(self, ex_id, work_dir="", model=""):
            return table.get(ex_id)

        monkeypatch.setattr(type(reg), "_acquire_cli", _acquire)
    table[executor_id] = gw
    return gw



# ===================================================================== 假网关
class FakeAcpGateway:
    """复刻 WorkBuddy ACP 网关的最小实现（够跑通委派全流程）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.sessions: dict[str, dict] = {}
        self.prompt_mode = "ok"          # ok | quota | interrupted
        self.prompt_calls: list[dict] = []
        self.set_config: list[tuple[str, str, Any]] = []
        #: ``acp/connect`` 被调了几次（**连接复用**的度量）。
        self.connect_calls = 0
        #: prompt 的模拟耗时（秒）——并发重叠的度量需要它。
        self.prompt_delay = 0.0
        #: ``session/new`` 返回的可用模型（**空 ⇒ 不做模型可用性校验**）。
        self.available_models: list[str] = []
        self.app = FastAPI()
        self._wire()

    # ---------------------------------------------------------------- 路由
    def _wire(self) -> None:
        @self.app.get("/api/v1/health")
        async def health() -> JSONResponse:
            return JSONResponse({"data": {"status": "ok", "uptime": 1.0,
                                          "platforms": ["generic"]}})

        @self.app.post("/api/v1/acp/connect")
        async def connect(request: Request) -> JSONResponse:
            if request.headers.get("x-codebuddy-request") != "1":
                return JSONResponse(
                    {"error": "Missing required header: x-codebuddy-request"},
                    status_code=403)
            self.connect_calls += 1
            return JSONResponse({"data": {"connectionId": "conn-1",
                                          "sessionToken": "tok-1"}})

        @self.app.post("/api/v1/acp")
        async def acp(request: Request) -> Any:
            if request.headers.get("x-codebuddy-request") != "1":
                return JSONResponse(
                    {"error": "Missing required header: x-codebuddy-request"},
                    status_code=403)
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                return JSONResponse({"error": "bad json"}, status_code=400)
            if isinstance(body, list):
                return JSONResponse({"error": "batch not supported"},
                                    status_code=400)
            return StreamingResponse(self._stream(body),
                                     media_type="text/event-stream")

    # ---------------------------------------------------------------- SSE
    async def _stream(self, body: dict):
        method = str(body.get("method") or "")
        params = body.get("params") or {}
        self.calls.append((method, params))
        yield ":ok\n\n"
        if method == "initialize":
            yield self._msg({"jsonrpc": "2.0", "id": body.get("id"), "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"promptCapabilities": {"image": True,
                                                             "embeddedContext": True},
                                      "loadSession": True},
                "authMethods": [{"id": "internal", "name": "Login with WeChat"}],
            }})
            return
        if method == "session/new":
            sid = str(params.get("sessionId") or "sess-generated")
            cwd = str(params.get("cwd") or "")
            # **真实行为复刻**：cwd 不存在 ⇒ session/new **静默失败**
            # （既没有 result 也没有 error，只有通知）⇒ 调用方必须先建目录。
            if cwd and not Path(cwd).is_dir():
                yield self._notify("session/update", {
                    "sessionId": sid,
                    "update": {"sessionUpdate": "config_option_update",
                               "configOptions": []},
                })
                return
            self.sessions[sid] = {"cwd": params.get("cwd"), "model": None}
            yield self._notify("session/update", {
                "sessionId": sid,
                "update": {"sessionUpdate": "config_option_update",
                           "configOptions": []},
            })
            yield self._msg({"jsonrpc": "2.0", "id": body.get("id"), "result": {
                "sessionId": sid,
                "models": {"currentModelId": "auto",
                           "availableModels": [{"modelId": m} for m in
                                               self.available_models]},
                "modes": {"currentModeId": "default"},
                "configOptions": [],
            }})
            return
        if method == "session/load":
            sid = str(params.get("sessionId") or "")
            if not sid:
                yield self._err(body.get("id"), -32602, "Session not found")
                return
            if sid not in self.sessions:
                # **真网关行为**：用**全新** id 做 session/load ⇒ 直接建出会话
                # （不走 primarySession 那条路）。session/load 的 result
                # **不回 sessionId**（照抄真网关，免得测试掩盖"读返回值"的错）。
                self.sessions[sid] = {"cwd": params.get("cwd"), "model": None}
            yield self._msg({"jsonrpc": "2.0", "id": body.get("id"), "result": {
                "models": {"currentModelId": "auto",
                           "availableModels": [{"modelId": m} for m in
                                               self.available_models]},
            }})
            return
        if method == "session/set_config_option":
            sid = str(params.get("sessionId") or "")
            self.set_config.append((sid, str(params.get("configId") or ""),
                                    params.get("value")))
            if sid in self.sessions and params.get("configId") == "model":
                self.sessions[sid]["model"] = params.get("value")
            yield self._msg({"jsonrpc": "2.0", "id": body.get("id"), "result": {}})
            return
        if method == "session/prompt":
            sid = str(params.get("sessionId") or "")
            text = ""
            blocks = params.get("prompt") or []
            for b in blocks:
                if isinstance(b, dict):
                    text += str(b.get("text") or "")
            self.prompt_calls.append({"session_id": sid, "text": text})
            if self.prompt_delay:
                # 模拟真实耗时（并发重叠的度量需要它）
                await asyncio.sleep(self.prompt_delay)
            if self.prompt_mode == "quota":
                yield self._err(body.get("id"), -32003, "Quota exceeded: 额度用完",
                                data={"category": "quota"})
                return
            if self.prompt_mode == "auth":
                yield self._err(body.get("id"), -32000,
                                "Authentication required (no valid authSession)")
                return
            if self.prompt_mode == "interrupted":
                yield self._err(body.get("id"), -32000, "Task was interrupted by user")
                return
            if self.prompt_mode == "transport":
                # 连接/传输类失败（既不是 quota 也不是 auth ⇒ 触发续跑）
                yield self._err(body.get("id"), -32603, "socket hang up (transport)")
                return
            if self.prompt_mode == "model_unavailable":
                # **模型不可用**（2026-10-23 20:45 实测的真实返回体形状）：
                # stopReason=refusal ＋ FAILED_MODEL_REQUEST ＋
                # displayMsg{"en":"Model unavailable. Please switch models"}
                # ⇒ bridge 必须判成"换候选"，而不是失败/回落。
                yield self._msg({
                    "jsonrpc": "2.0", "id": body.get("id"),
                    "result": {
                        "stopReason": "refusal",
                        "_meta": {
                            "codebuddy.ai/outcome": "FAILED_MODEL_REQUEST",
                            "codebuddy.ai/errorMessage": json.dumps({
                                "code": -32603, "message": "Internal error",
                                "data": {"statusCode": 400, "displayMsg": {
                                    "en": "Model unavailable. Please switch models",
                                    "zh": "当前模型不可用，请切换模型"}}},
                                ensure_ascii=False)}}})
                return
            # 正常：思考 → 工具 → 工具结果 → 正文
            yield self._notify("session/update", {
                "sessionId": sid,
                "update": {"sessionUpdate": "agent_thought_chunk",
                           "content": {"type": "text", "text": "先看看目录"}},
            })
            yield self._notify("session/update", {
                "sessionId": sid,
                "update": {"sessionUpdate": "tool_call", "toolCallId": "tc-1",
                           "title": "Read", "kind": "read",
                           "rawInput": {"path": "a.py"}},
            })
            yield self._notify("session/update", {
                "sessionId": sid,
                "update": {"sessionUpdate": "tool_call_update", "toolCallId": "tc-1",
                           "status": "completed",
                           "content": [{"type": "content",
                                        "content": {"type": "text", "text": "ok"}}]},
            })
            yield self._notify("session/update", {
                "sessionId": sid,
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": "做完了：已改 a.py"}},
            })
            yield self._msg({"jsonrpc": "2.0", "id": body.get("id"),
                             "result": {"stopReason": "end_turn"}})
            return
        if method == "session/enqueue_followup":
            yield self._msg({"jsonrpc": "2.0", "id": body.get("id"), "result": {}})
            return
        yield self._err(body.get("id"), -32601, f"Method not found: {method}")

    @staticmethod
    def _msg(obj: dict) -> str:
        return ("event: message\ndata: "
                + json.dumps(obj, ensure_ascii=False) + "\n\n")

    @staticmethod
    def _notify(method: str, params: dict) -> str:
        return FakeAcpGateway._msg({"jsonrpc": "2.0", "method": method,
                                    "params": params})

    @staticmethod
    def _err(req_id: Any, code: int, message: str,
             data: Optional[dict] = None) -> str:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return FakeAcpGateway._msg({"jsonrpc": "2.0", "id": req_id, "error": err})


def _start_fake_gw():
    """起一个假 ACP 网关（真端口、真 SSE，线程内跑 uvicorn）。"""
    gw = FakeAcpGateway()
    cfg = uvicorn.Config(gw.app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(cfg)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "假网关未启动"
    port = server.servers[0].sockets[0].getsockname()[1]
    gw.base_url = f"http://127.0.0.1:{port}"
    return gw, server, thread


@pytest.fixture
def fake_gw():
    """第一个假 ACP 网关。"""
    gw, server, thread = _start_fake_gw()
    try:
        yield gw
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def fake_gw_b():
    """**第二个**假 ACP 网关（不同端口 ⇒ 不同网关实例 ⇒ 可以真并行）。"""
    gw, server, thread = _start_fake_gw()
    try:
        yield gw
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _wb_cfg_payload(tmp_path, **over) -> Path:
    """写一份 wb_exec 配置到 tmp（**不改仓库里的 config/wb_exec.json**）。"""
    import json as _json
    raw = {
        "enabled": True,
        "executors": [
            {"id": "wb-cn", "label": "CN", "install_root": str(tmp_path),
             "product_config": "", "cli_bin": str(tmp_path / "nope"),
             "port": 0},
        ],
        "candidates": [
            {"executor": "wb-cn", "model": "custom-local:deepseek-v4-pro",
             "weight": 20},
            {"executor": "wb-cn", "model": "hy3", "weight": 10},
        ],
        "fallback": {"models": ["hy3"], "prefer_same_executor": True},
        "guards": {"permission_mode": "dontAsk", "sandbox": True,
                   "context_window": 300000},
        "watchdog": {"gateway_heartbeat_sec": 90, "prompt_keepalive_sec": 150,
                     "business_event_sec": 240, "max_resume": 3,
                     "rpc_timeout_sec": 30},
        "on_unavailable": {"mode": "fallback_system"},
        "scope": {"mode": "all"},
        "task": {"segment_marker": "【任务卡 {card_id} · 段 {seg}/{total}】",
                 "auto_segment": False},
    }
    raw.update(over)
    p = tmp_path / "wb_exec.json"
    p.write_text(_json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture
def wb_cfg(tmp_path, monkeypatch):
    """把 wb_exec 配置指到 tmp 里的一个副本（不改仓库里的 config/wb_exec.json）。"""
    p = _wb_cfg_payload(tmp_path)
    monkeypatch.setenv(wb_config.ENV_CONFIG_PATH, str(p))
    wb_config.reset_cache()
    # **★ 运行时总开关也要显式"开"**（2026-10-23 23:55）：
    #   全局夹具把总开关**关掉**了（避免其它用例真去连本机的 WorkBuddy），
    #   而总开关优先级**高于**配置 ⇒ 本文件的用例要委派就必须自己把它打开。
    _sw_on = tmp_path / "wb_exec_switch_on.json"
    _sw_on.write_text(json.dumps({"enabled": True}, ensure_ascii=False),
                      encoding="utf-8")
    monkeypatch.setattr(wb_switch, "SWITCH_PATH", _sw_on)
    # **配比器是进程级的**（跨回合存活才让轮询推进）⇒ 用例间会互相影响
    # ⇒ 每例开头清一次，保证确定性。
    picker.reset_pickers()
    try:
        yield wb_config.load()
    finally:
        wb_config.reset_cache()
        picker.reset_pickers()


@pytest.fixture
def wb_cfg_opt_in(tmp_path, monkeypatch):
    """开闸范围＝``opt_in``：只有显式点了 auto 的卡才接管。"""
    p = _wb_cfg_payload(tmp_path, scope={"mode": "opt_in"})
    monkeypatch.setenv(wb_config.ENV_CONFIG_PATH, str(p))
    wb_config.reset_cache()
    _sw_on = tmp_path / "wb_exec_switch_on.json"
    _sw_on.write_text(json.dumps({"enabled": True}, ensure_ascii=False),
                      encoding="utf-8")
    monkeypatch.setattr(wb_switch, "SWITCH_PATH", _sw_on)
    picker.reset_pickers()
    try:
        yield wb_config.load()
    finally:
        wb_config.reset_cache()
        picker.reset_pickers()


# ===================================================================== 配置
def test_config_missing_is_disabled(tmp_path, monkeypatch):
    """fail-open：配置读不到 ⇒ 全链路旁路，绝不把 coder 卡搞挂。"""
    monkeypatch.setenv(wb_config.ENV_CONFIG_PATH, str(tmp_path / "nope.json"))
    wb_config.reset_cache()
    cfg = wb_config.load()
    assert cfg.enabled is False
    assert cfg.candidates == ()
    wb_config.reset_cache()


def test_config_broken_is_disabled(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"
    p.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setenv(wb_config.ENV_CONFIG_PATH, str(p))
    wb_config.reset_cache()
    assert wb_config.load().enabled is False
    wb_config.reset_cache()


def test_repo_config_shape_is_valid(monkeypatch):
    """仓库里的真实配置必须能解析（改了配比就跑这条）。

    **注意**：conftest 的 sandbox 会把 ``WB_EXEC_CONFIG_PATH`` 指到一份**关着**的临时
    配置（防止用例真去起 WorkBuddy 网关）⇒ 这条要**显式**指回仓库里的真配置。
    """
    monkeypatch.setenv(wb_config.ENV_CONFIG_PATH, str(REPO / "config" / "wb_exec.json"))
    wb_config.reset_cache()
    cfg = wb_config.load()
    # 2026-10-23 用户令：执行体已开闸（enabled=true）
    assert cfg.enabled is True
    assert len(cfg.candidates) == 4
    weights = {f"{c.executor}:{c.model}": c.weight for c in cfg.candidates}
    # ★ **用户指定的配比，原样**（2026-10-23 用户令 + 10-25 加档 + **10-26 删 step-5**）：
    #   deepseek-v4.1-flash 40 / **wb-oc/beam-free 20** / hy3 10×2
    #   ⇒ 合计 80（**配比器按相对权重分配**，见 picker 的 sum(weight)）
    # **不许替他换模型**（换到火山方舟/GLM 那些付费端点＝替他花钱）。
    assert weights["wb-intl:deepseek-v4.1-flash"] == 40
    # ★ 2026-10-25 用户令：opencode 只用 beam-free（端点上唯一**真免费**的档）
    assert weights["wb-oc:beam-free"] == 20
    assert weights["wb-cn:hy3"] == 10
    assert weights["wb-intl:hy3"] == 10
    assert sum(weights.values()) == 80
    # ★ 2026-10-26 用户令：两个 WorkBuddy 的 step-5-preview **已不免费** ⇒ 移出候选表
    assert "wb-cn:step-5-preview" not in weights
    assert "wb-intl:step-5-preview" not in weights
    # **表里不许出现付费/自定义端点**（用户明确：那些一样要花钱）
    for key in weights:
        model = key.split(":", 1)[1]
        assert not model.startswith("custom-local:"), f"表里不许有付费端点: {key}"
        assert model not in ("fast-model", "balanced-model", "deep-model"), key
    # ★ opencode 执行体只留 beam-free（2026-10-25 用户令）⇒ 别的模型钉进来
    #   会被静默换成 beam-free（**不是**换成收费档）
    oc = cfg.executor("wb-oc")
    assert oc is not None
    assert list(oc.oc_models) == ["beam-free"]
    assert cfg.fallback_models == ("hy3",)
    assert cfg.guards.permission_mode != "fullAccess"
    wb_config.reset_cache()


# ===================================================================== 配比器
def test_picker_follows_weights():
    cands = [
        wb_config.Candidate("a", "m1", 40),
        wb_config.Candidate("b", "m2", 20),
        wb_config.Candidate("c", "m3", 20),
        wb_config.Candidate("d", "m4", 10),
        wb_config.Candidate("e", "m5", 10),
    ]
    p = picker.Picker(cands)
    seq = [f"{c.executor}:{c.model}" for c in p.peek_sequence(100)]
    counts: dict[str, int] = {}
    for s in seq:
        counts[s] = counts.get(s, 0) + 1
    assert counts["a:m1"] == 40
    assert counts["b:m2"] == 20
    assert counts["c:m3"] == 20
    assert counts["d:m4"] == 10
    assert counts["e:m5"] == 10


def test_picker_same_weight_alternates_not_fixed():
    """同权重档内**平均轮流**（不固定某一边）——用户令 2026-10-23 09:56。"""
    cands = [wb_config.Candidate("cn", "step-5-preview", 20),
             wb_config.Candidate("intl", "step-5-preview", 20)]
    p = picker.Picker(cands)
    seq = p.peek_sequence(8)
    sides = [c.executor for c in seq]
    assert sides == ["cn", "intl"] * 4
    # 两个方向都要出现过 ⇒ 没有"永远走国内版"
    assert "cn" in sides and "intl" in sides


def test_fallback_chain_same_side_then_other():
    cands = [
        wb_config.Candidate("intl", "deepseek-v4.1-flash", 40),
        wb_config.Candidate("cn", "step-5-preview", 20),
        wb_config.Candidate("intl", "step-5-preview", 20),
        wb_config.Candidate("cn", "hy3", 10),
        wb_config.Candidate("intl", "hy3", 10),
    ]
    chain = picker.fallback_chain(cands[0], cands, ("hy3",), True)
    assert [(c.executor, c.model) for c in chain] == [
        ("intl", "hy3"), ("cn", "hy3")]


def test_fallback_chain_skips_current():
    cands = [wb_config.Candidate("cn", "hy3", 10),
             wb_config.Candidate("intl", "hy3", 10)]
    chain = picker.fallback_chain(cands[0], cands, ("hy3",), True)
    assert [(c.executor, c.model) for c in chain] == [("intl", "hy3")]


def test_picker_is_process_level_across_turns():
    """**配比器必须跨回合存活**。

    踩过的坑：若每个回合新建一个 Picker，平滑轮询的累加状态每回合归零
    ⇒ 第一次 pick 永远是权重最高的那个 ⇒ **配比退化成 100% 打第一个候选**
    （40/20/20/10/10 会变成 100% deepseek）。
    """
    cands = [wb_config.Candidate("intl", "ds", 40),
             wb_config.Candidate("cn", "s5", 20),
             wb_config.Candidate("intl", "s5", 20),
             wb_config.Candidate("cn", "hy3", 10),
             wb_config.Candidate("intl", "hy3", 10)]
    picker.reset_pickers()
    try:
        p1 = picker.get_picker(cands)
        p2 = picker.get_picker(cands)
        assert p1 is p2, "配比器必须跨回合复用同一实例"
        # 连着挑 10 次：分布要贴近配比
        seq = [picker.get_picker(cands).pick() for _ in range(10)]
        firsts = [f"{c.executor}:{c.model}" for c in seq]
        assert firsts[0] == "intl:ds"
        assert len(set(firsts)) >= 3, firsts
        counts: dict[str, int] = {}
        for s in firsts:
            counts[s] = counts.get(s, 0) + 1
        assert counts["intl:ds"] >= 3, counts
        # **两个回合不该挑同一个**（状态推进了）
        assert firsts[0] != firsts[1]
    finally:
        picker.reset_pickers()


def test_picker_rebuilds_when_table_changes():
    """配比表变了 ⇒ 重建配比器（签名不同就是新实例）。"""
    a = [wb_config.Candidate("cn", "m1", 10)]
    b = [wb_config.Candidate("cn", "m1", 20)]
    picker.reset_pickers()
    try:
        assert picker.get_picker(a) is not picker.get_picker(b)
        assert picker.get_picker(a) is picker.get_picker(a)
    finally:
        picker.reset_pickers()


# ===================================================================== 看门狗
def test_watchdog_judges_by_state_not_by_time():
    """**按状态判断，不按时间**（2026-10-23 重写）。

    早先这里是三套时间阈值（心跳 90s / keepalive 150s / 业务事件 240s）⇒
    拿时间猜死活 ⇒ 猜错就误杀正在干活的长任务（实测 615.9 秒的正常任务）。
    ⇒ 现在只认 WorkBuddy 给的状态：agentPhase / stopReason / 连接断开事件。
    """
    wd = watchdog.Watchdog()
    # 没有 phase ⇒ **不猜**
    assert wd.busy() is False
    assert wd.disconnected is False
    # phase 明确 ⇒ 按 phase 判忙
    wd.note_phase("preparing")
    assert wd.busy() is True
    wd.note_phase("idle")
    assert wd.busy() is False
    # 结果是状态
    wd.note_stop("end_turn")
    assert wd.stop_reason == "end_turn"
    # 连接真断了 ⇒ 事件
    wd.note_disconnected()
    assert wd.disconnected is True
    wd.note_business()
    assert wd.business_count == 1


# ===================================================================== ACP 客户端
@pytest.mark.asyncio
async def test_acp_client_connect_and_prompt(fake_gw):
    seen: list[str] = []

    async def on_note(n: acp_client.AcpNotification) -> None:
        seen.append(n.update_kind)

    async with acp_client.AcpClient(fake_gw.base_url, timeout=20,
                                    on_notification=on_note) as c:
        await c.connect()
        assert c.connection_id == "conn-1"
        assert c.session_token == "tok-1"
        res = await c.session_new("C:/tmp", session_id="card_042")
        assert res["sessionId"] == "card_042"
        await c.set_config_option("card_042", "model", "hy3")
        out = await c.prompt("card_042", "干活")
    assert out.get("stopReason") == "end_turn"
    assert "agent_thought_chunk" in seen
    assert "tool_call" in seen
    assert "agent_message_chunk" in seen
    assert fake_gw.set_config[-1] == ("card_042", "model", "hy3")


@pytest.mark.asyncio
async def test_acp_client_quota_error_category(fake_gw):
    fake_gw.prompt_mode = "quota"
    async with acp_client.AcpClient(fake_gw.base_url, timeout=20) as c:
        await c.connect()
        await c.session_new("C:/tmp", session_id="s1")
        with pytest.raises(acp_client.WbAcpError) as ei:
            await c.prompt("s1", "干活")
    assert ei.value.is_quota is True


@pytest.mark.asyncio
async def test_acp_client_user_interruption(fake_gw):
    fake_gw.prompt_mode = "interrupted"
    async with acp_client.AcpClient(fake_gw.base_url, timeout=20) as c:
        await c.connect()
        await c.session_new("C:/tmp", session_id="s1")
        with pytest.raises(acp_client.WbAcpError) as ei:
            await c.prompt("s1", "干活")
    assert ei.value.is_user_interruption is True


@pytest.mark.asyncio
async def test_acp_client_missing_header_rejected(fake_gw):
    """不带 ``x-codebuddy-request`` ⇒ 403（真网关的既有行为）。"""
    import httpx
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{fake_gw.base_url}/api/v1/acp/connect", json={})
    assert r.status_code == 403


# ===================================================================== 桥接层
async def _make_coder_card(work_id: str, title: str = "编程卡") -> str:
    card = await card_store.create_card(
        work_id, title, "模型生成", card_type="coder",
        charter="## 使命\n把 a.py 改好",
        goal="把 a.py 改好", skip_kick=True)
    assert "error" not in card, card
    return str(card["meta"]["card_id"])


@pytest.mark.asyncio
async def test_bridge_not_delegate_when_disabled(work, tmp_path, monkeypatch):
    """执行体关着 ⇒ 一切照旧（不委派）。"""
    p = _wb_cfg_payload(tmp_path, enabled=False)
    monkeypatch.setenv(wb_config.ENV_CONFIG_PATH, str(p))
    wb_config.reset_cache()
    picker.reset_pickers()
    assert wb_config.load().enabled is False
    card_id = await _make_coder_card(work["work_id"])
    st = message_runner.RunState(work_id=work["work_id"], card_id=card_id,
                                 content="干活")
    assert await bridge.maybe_delegate(st) is None
    wb_config.reset_cache()
    picker.reset_pickers()


@pytest.mark.asyncio
async def test_bridge_system_backend_not_delegated(work, wb_cfg, monkeypatch):
    """``exec_backend="system"`` ⇒ 走系统自己的模型（用户在卡上手动切回来的落点）。"""
    from src.storage import model_override
    card_id = await _make_coder_card(work["work_id"])
    model_override.put_row(work["work_id"], card_id,
                           {"exec_backend": "system"}, src="manual")
    assert bridge.exec_backend_of(work["work_id"], card_id) == "system"
    st = message_runner.RunState(work_id=work["work_id"], card_id=card_id,
                                 content="干活")
    assert await bridge.maybe_delegate(st) is None


@pytest.mark.asyncio
async def test_bridge_feeds_recent_tool_calls_to_workbuddy(work, wb_cfg,
                                                           fake_gw, monkeypatch):
    """**手脚记忆**：卡之前跑过的工具调用要喂给 WorkBuddy（否则它会重跑、重复副作用）。"""
    from src.storage import session_facade
    reg = bridge.get_registry()
    from src.api.wb_exec.registry import GatewayProcess
    from src.api.wb_exec.config import Executor
    _use_cli_pool(reg, monkeypatch, fake_gw, "wb-cn")
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)

    card_id = await _make_coder_card(work["work_id"])
    # 造一段"之前跑过"的历史：assistant 带 tool_calls
    await session_facade.append(work["work_id"], card_id, {
        "role": "assistant", "content": "我先装依赖",
        "tool_calls": [{"id": "call_x_1_0", "type": "function",
                        "function": {"name": "bash",
                                     "arguments": json.dumps(
                                         {"command": "pip install -r req.txt"})}}],
    })

    st = message_runner.RunState(work_id=work["work_id"], card_id=card_id,
                                 content="继续")
    st.pair_id = "pair-003"
    outcome = await bridge.maybe_delegate(st)
    assert outcome is not None
    sent = fake_gw.prompt_calls[-1]["text"]
    assert "已经跑过的动作" in sent, sent
    assert "bash" in sent, sent
    assert "pip install" in sent, sent


@pytest.mark.asyncio
async def test_bridge_auth_required_falls_back_without_retrying(work, wb_cfg,
                                                              fake_gw, monkeypatch):
    """**账号未登录** ⇒ 不可重试、且记住该执行体（后续秒级回落）。"""
    reg = bridge.get_registry()
    from src.api.wb_exec.registry import GatewayProcess
    from src.api.wb_exec.config import Executor
    _use_cli_pool(reg, monkeypatch, fake_gw, "wb-cn")
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)
    reg.clear_unauthenticated()

    # 假网关的 prompt 报「账号未登录」
    orig = FakeAcpGateway._stream

    n_calls = {"n": 0}

    async def patched(self, body):
        if str(body.get("method")) == "session/prompt":
            n_calls["n"] += 1
            # **只在第一次**报 auth（之后恢复正常）⇒ 才能验证
            # "这一张卡回落、别的卡照常委派"（故障局部化）
            self.prompt_mode = "auth" if n_calls["n"] == 1 else "ok"
        async for chunk in orig(self, body):
            yield chunk

    monkeypatch.setattr(FakeAcpGateway, "_stream", patched)

    card_id = await _make_coder_card(work["work_id"])
    st = message_runner.RunState(work_id=work["work_id"], card_id=card_id,
                                 content="干活")
    st.pair_id = "pair-auth"
    outcome = await bridge.maybe_delegate(st)
    # 回落系统模型（on_unavailable=fallback_system）⇒ 返回 None
    assert outcome is None
    # **只试了一次**（没走 3 次重连）
    assert len(fake_gw.prompt_calls) == 1
    # [故障局部化 2026-10-23 实跑教训] 鉴权失败**只影响这一张卡**：
    #   早先做成"整个执行体永久熔断"⇒ 一次瞬时故障（网关进程被回收重建的窗口期）
    #   就把**所有**卡都挡住 ⇒ 整条通道瘫痪。
    #   ⇒ 现在只记一个诊断计数，**别的卡照常委派**。
    assert reg.is_unauthenticated("wb-cn") is False, "不许全局熔断"
    assert reg.auth_failures("wb-cn") == 1
    card2 = await _make_coder_card(work["work_id"], "另一张卡")
    st2 = message_runner.RunState(work_id=work["work_id"], card_id=card2,
                                   content="再干")
    st2.pair_id = "pair-auth2"
    out2 = await bridge.maybe_delegate(st2)
    assert out2 is not None, "别的卡必须照常委派（不许被连坐）"
    assert len(fake_gw.prompt_calls) == 2
    reg.clear_unauthenticated()


@pytest.mark.asyncio
async def test_fallback_is_recorded_so_card_face_is_not_a_fake_signal(
        work, wb_cfg, fake_gw, monkeypatch):
    """**回落必须如实记账**（2026-10-23 19:55 用户要求"靠谱"）。

    委派失败会**静默**回落系统路径 ⇒ 若卡面还显示"走 WorkBuddy"，
    人就以为"在 WorkBuddy 上跑"——**假信号**。
    ⇒ 回落时 ``last_routed.fallback == "system"`` ＋原因要写进覆盖行。
    """
    from src.storage import model_override

    reg = bridge.get_registry()
    from src.api.wb_exec.registry import GatewayProcess
    from src.api.wb_exec.config import Executor
    _use_cli_pool(reg, monkeypatch, fake_gw, "wb-cn")
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)

    fake_gw.prompt_mode = "auth"          # ⇒ 鉴权失败 ⇒ 回落
    card_id = await _make_coder_card(work["work_id"])
    st = message_runner.RunState(work_id=work["work_id"], card_id=card_id,
                                 content="干活")
    st.pair_id = "pair-fb"
    outcome = await bridge.maybe_delegate(st)
    assert outcome is None, "鉴权失败 ⇒ 回落系统模型"

    row = model_override.get_row(work["work_id"], card_id) or {}
    routed = row.get("last_routed") or {}
    assert routed.get("fallback") == "system", routed
    assert str(routed.get("reason") or ""), "必须带出原因，别让人一头雾水"
    fake_gw.prompt_mode = "ok"


@pytest.mark.asyncio
async def test_bridge_opt_in_scope_leaves_existing_cards_alone(work, wb_cfg_opt_in,
                                                              monkeypatch):
    """``scope.mode="opt_in"`` ⇒ **存量卡默认不接管**（开闸零冲击，逐张接管）。"""
    card_id = await _make_coder_card(work["work_id"])
    # 没设过 exec_backend ⇒ 存量卡 ⇒ 不委派
    assert bridge.exec_backend_raw_of(work["work_id"], card_id) is None
    st = message_runner.RunState(work_id=work["work_id"], card_id=card_id,
                                 content="干活")
    assert await bridge.maybe_delegate(st) is None

    # 显式设成 auto ⇒ 才接管
    from src.storage import model_override
    model_override.put_row(work["work_id"], card_id,
                           {"exec_backend": "auto"}, src="manual")
    assert bridge.exec_backend_raw_of(work["work_id"], card_id) == "auto"


@pytest.mark.asyncio
async def test_bridge_delegates_and_finishes_card(work, wb_cfg, fake_gw,
                                                   monkeypatch):
    """全流程：委派 ⇒ 事件落盘 ⇒ 卡 completed ⇒ 回执给父卡。"""
    from src.api import card_tools
    from src.storage import model_override, shadow_store

    # 把执行器指向假网关（不 spawn 真进程）
    reg = bridge.get_registry()
    from src.api.wb_exec.registry import GatewayProcess
    from src.api.wb_exec.config import Executor
    _use_cli_pool(reg, monkeypatch, fake_gw, "wb-cn")
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)

    card_id = await _make_coder_card(work["work_id"])
    model_override.put_row(work["work_id"], card_id,
                           {"exec_backend": "auto"}, src="manual")

    st = message_runner.RunState(work_id=work["work_id"], card_id=card_id,
                                 content="把 a.py 改好")
    st.pair_id = "pair-001"
    outcome = await bridge.maybe_delegate(st)
    assert outcome is not None
    assert outcome.kind == "deliver"
    assert outcome.detail == "wb_exec"
    # 正文不重复：T14 拼接是 join(analysis_parts + [final_reply])，
    # 两个都设会把正文拼两遍 ⇒ 只设 final_reply。
    assert st.analysis_parts == []
    assert st.final_reply == "做完了：已改 a.py"

    # ① 事件流里有过程事件（思考/工具/正文）
    journal = event_stream.journal_of(work["work_id"], card_id)
    kinds = [e.get("type") for e in journal]
    assert "wb_exec_start" in kinds
    # [harness 第 1 步 2026-10-28 · 通道修正] 思考走**逐字流**（token journal），
    # 与系统自己的思考同一条通道；主 journal 里不该有逐帧记录（改前这里断言
    # 主 journal 有 reasoning_chunk，正是"一个语义两条通道"的病根）。
    assert "reasoning_chunk" not in kinds, "逐帧思考不许再进主通道（会逐帧落盘）"
    tkinds = [e.get("type") for e in
              event_stream.token_journal_of(work["work_id"], card_id)]
    assert "reasoning_chunk" in tkinds, "思考没进逐字流 ⇒ 前端直播会看不到"
    # 落盘侧：轮界合并成一条 reasoning（全文不丢）
    from src.storage import event_log as _elog
    event_stream.flush_pending_reasoning(work["work_id"], card_id)
    await _elog.drain()
    rk = [e for e in (_elog.read(work["work_id"], card_id) or [])
          if e.get("type") == "reasoning"]
    assert rk, "思考没有合并落盘 ⇒ 刷新后看不到"
    assert str((rk[-1].get("data") or {}).get("text") or "").strip(), "合并落盘的思考是空的"
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert "wb_exec_done" in kinds

    # ② 卡完工（复用 report_done）
    card = await card_store.read_card(work["work_id"], card_id)
    assert str(card["meta"].get("state")) == "completed"
    # coder_final 是 report_done 直写 SQL 的列（不在 read_card 的 meta 投影里），
    # 这里直接查库确认它落到位了。
    import sqlite3
    dbp = Path(card_store.WORKS_DIR) / work["work_id"] / "cards.db"
    with sqlite3.connect(dbp) as conn:
        row = conn.execute("SELECT coder_final FROM cards WHERE card_id=?",
                           (card_id,)).fetchone()
    assert row and row[0] and "WorkBuddy 执行体完成" in str(row[0])

    # ③ 回执给父卡（父卡＝根卡）
    # [harness 第 1 步 2026-10-28] 改前断言"收件箱里有 coder_done"，但 10-27 起
    # 消息落点已从 inbox 改成**会话块 + 事件流**（用户裁定「消息会留在上下文里
    # 直到被凝练掉」）⇒ list_pending 恒空，这条断言早已不成立。
    # 现在按新落点验：父卡的会话里有块、事件流里有 coder_done。
    from src.storage import event_log as _elog2, session_facade
    root = work_store.work_card_id_of(work["work_id"])
    await _elog2.drain()
    root_evs = [e for e in (_elog2.read(work["work_id"], root) or [])
                if e.get("type") == "coder_done"]
    assert root_evs, "父卡事件流里没有 coder_done 回执"
    d0 = root_evs[-1].get("data") or {}
    assert d0.get("from") == card_id, f"回执发件人丢了: {d0}"
    blocks = await session_facade.read(work["work_id"], root) or []
    assert any(str(b.get("actor") or "").startswith("card:") for b in blocks), \
        "父卡会话里没有回执块（消息块 actor 应是 card:<发件卡>）"

    # ④ 影子留了完工行
    shadow = await shadow_store.read_shadow_raw(work["work_id"], card_id)
    assert shadow and "完工" in str(shadow)

    # ⑤ 护栏按配置下发（mode 不用 fullAccess）
    #    **CLI 侧不下发 sandbox**（本地沙箱会把写文件挡掉）⇒ 只断言 mode
    cfg_ids = {c for _, c, _ in fake_gw.set_config}
    assert "mode" in cfg_ids
    modes = [v for _, c, v in fake_gw.set_config if c == "mode"]
    assert modes and all(m != "fullAccess" for m in modes)

    # ⑥ **卡上事实喂给了 WorkBuddy**（存量未完工卡被接手的必备条件：
    #    ACP 侧是全新会话，WorkBuddy 不知道卡之前做到哪）
    sent = fake_gw.prompt_calls[-1]["text"]
    assert "把 a.py 改好" in sent, sent
    assert "使命" in sent, sent

    # ⑦ **产出不再写进现状块**（[2026-10-28 用户令「这个动作不合理」]）
    # 编程卡按设计没有现状块；结果由 report_done 的 summary ＋ coder_done 回执给父卡。
    # 改前这里是 append 执行体原文，24 轮滚到 86 万字（card_076 实证）。
    card2 = await card_store.read_card(work["work_id"], card_id)
    sb = str(card2["body"].get("status_block") or "")
    assert sb == "", f"编程卡的现状块必须保持空：{sb[:200]}"
    # 结果走回执：父卡侧应有 coder_done 消息
    from src.storage import session_facade
    parent = str((card2["meta"] or {}).get("parent") or "")
    msgs = await session_facade.read(work["work_id"], parent) or []
    assert any("做完了：已改 a.py" in str(m.get("content") or "") for m in msgs), \
        "完工结果必须经 coder_done 回执送到父卡"


@pytest.mark.asyncio
async def test_bridge_quota_falls_back_to_hy3(work, wb_cfg, fake_gw,
                                              monkeypatch):
    """上游额度耗尽 ⇒ fallback 到 hy3（先同侧）。"""
    reg = bridge.get_registry()
    from src.api.wb_exec.registry import GatewayProcess
    from src.api.wb_exec.config import Executor
    _use_cli_pool(reg, monkeypatch, fake_gw, "wb-cn")
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)

    card_id = await _make_coder_card(work["work_id"])
    st = message_runner.RunState(work_id=work["work_id"], card_id=card_id,
                                 content="干活")
    st.pair_id = "pair-002"

    # 第一次 prompt 报 quota，第二次（换模型后）成功
    calls = {"n": 0}
    orig = FakeAcpGateway._stream

    async def patched(self, body):
        if str(body.get("method")) == "session/prompt":
            calls["n"] += 1
            self.prompt_mode = "quota" if calls["n"] == 1 else "ok"
        async for chunk in orig(self, body):
            yield chunk

    monkeypatch.setattr(FakeAcpGateway, "_stream", patched)

    outcome = await bridge.maybe_delegate(st)
    assert outcome is not None
    models = [v for _, c, v in fake_gw.set_config if c == "model"]
    assert "hy3" in models, models
    journal = event_stream.journal_of(work["work_id"], card_id)
    assert "wb_exec_fallback" in [e.get("type") for e in journal]


# ===================================================================== 2026-10-23 校正
@pytest.mark.asyncio
async def test_model_unavailable_switches_candidate(work, wb_cfg, fake_gw,
                                                   monkeypatch):
    """**实测报"这个模型不可用" ⇒ 换一个候选**（不是失败，也不该白重试）。

    **判据是实测错误，不是本地目录**（2026-10-23 20:45 实测更正）：
    早先拿 ``session/load`` 的 ``availableModels`` 当目录 ⇒ 那是**被过滤过的
    本地列表**（cn 只回 5 个，可 hy3 / step-5-preview 实测都能调）
    ⇒ **害得国版那 30% 权重全被跳过**。
    ⇒ 现在只有真调失败报 "Model unavailable" 才换候选。
    """
    reg = bridge.get_registry()
    from src.api.wb_exec.registry import GatewayProcess
    from src.api.wb_exec.config import Executor
    _use_cli_pool(reg, monkeypatch, fake_gw, "wb-cn")
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)

    # **本地目录里没有这个模型 —— 不代表它不能用**（不得据此跳过！）
    fake_gw.available_models = ["hy3"]
    # 真调的时候报"模型不可用" ⇒ 才换候选
    orig = FakeAcpGateway._stream
    calls = {"n": 0}

    async def patched(self, body):
        if str(body.get("method")) == "session/prompt":
            calls["n"] += 1
            self.prompt_mode = "model_unavailable" if calls["n"] == 1 else "ok"
        async for chunk in orig(self, body):
            yield chunk

    monkeypatch.setattr(FakeAcpGateway, "_stream", patched)

    card_id = await _make_coder_card(work["work_id"])
    st = message_runner.RunState(work_id=work["work_id"], card_id=card_id,
                                 content="干活")
    st.pair_id = "pair-model"
    outcome = await bridge.maybe_delegate(st)
    assert outcome is not None, "换候选后应该能跑起来"
    models = [v for _, c, v in fake_gw.set_config if c == "model"]
    assert len(models) >= 2, models
    journal = event_stream.journal_of(work["work_id"], card_id)
    assert "wb_exec_fallback" in [e.get("type") for e in journal]
    # **实测不可用的模型要记下来** ⇒ 以后不再选它（按状态判定）
    assert reg.model_unavailable("wb-cn", models[0]) is True
    fake_gw.prompt_mode = "ok"


@pytest.mark.asyncio
async def test_other_session_events_are_filtered(work, wb_cfg, fake_gw,
                                                  monkeypatch):
    """**别会话的事件不能进卡面**（实测：同一 ACP 连接上桌面端自己的会话会串进来）。"""
    reg = bridge.get_registry()
    from src.api.wb_exec.registry import GatewayProcess
    from src.api.wb_exec.config import Executor
    _use_cli_pool(reg, monkeypatch, fake_gw, "wb-cn")
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)

    orig = FakeAcpGateway._stream

    async def patched(self, body):
        if str(body.get("method")) == "session/prompt":
            # 混入一条**别的会话**的正文
            yield self._notify("session/update", {
                "sessionId": "别人的会话",
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": "不该出现在卡面"}},
            })
        async for chunk in orig(self, body):
            yield chunk

    monkeypatch.setattr(FakeAcpGateway, "_stream", patched)

    card_id = await _make_coder_card(work["work_id"])
    st = message_runner.RunState(work_id=work["work_id"], card_id=card_id,
                                 content="干活")
    st.pair_id = "pair-filter"
    outcome = await bridge.maybe_delegate(st)
    assert outcome is not None
    assert st.final_reply == "做完了：已改 a.py"
    assert "不该出现在卡面" not in st.final_reply


@pytest.mark.asyncio
async def test_ensure_session_creates_cwd_and_uses_own_session_id(
        work, wb_cfg, fake_gw, tmp_path, monkeypatch):
    """会话两件事（实测）：

    ① **cwd 必须先存在** —— 不存在时 ``session/new`` 静默失败 ⇒ 桥接层自己建；
    ② **用 ``session/load`` + 自己的 id 建会话**（不传 id 会落回桌面端那个
       primarySession ⇒ 任务会打进用户自己的对话）。
    """
    from src.api.wb_exec import acp_client as ac
    from src.api.wb_exec.bridge import _ensure_session

    async with ac.AcpClient(fake_gw.base_url, timeout=20) as c:
        await c.connect()
        # 原始 session/new：cwd 不存在 ⇒ 静默失败（既没 result 也没 error）
        missing = str(tmp_path / "no-such-dir-xyz")
        with pytest.raises(ac.WbAcpError):
            await c.session_new(missing)
        # 桥接层：自己建目录 + 用自己的 id 建会话
        sid, models = await _ensure_session(c, "card_x", missing, set())
        assert sid == "card_x", "会话 id 必须是**我们自己的**（不能是桌面端那个）"
        assert Path(missing).is_dir(), "cwd 必须被建出来"
        # 桌面端当前会话 ⇒ 拒绝使用（绝不能把任务打进用户对话）
        with pytest.raises(ac.WbAcpError):
            await _ensure_session(c, "desktop-sess", str(tmp_path), {"desktop-sess"})




@pytest.mark.asyncio
async def test_resume_reuses_session_after_transport_failure(
        work, wb_cfg, fake_gw, monkeypatch):
    """**续跑**：连接类失败 ⇒ 恢复**同一个会话**接着跑（不是新建会话从头再来）。

    用户令：「workbuddy 尤其国际版经常会中断，你需要考虑好怎么自动续任务」。
    """
    reg = bridge.get_registry()
    from src.api.wb_exec.registry import GatewayProcess
    from src.api.wb_exec.config import Executor
    _use_cli_pool(reg, monkeypatch, fake_gw, "wb-cn")
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)

    calls = {"n": 0}
    orig = FakeAcpGateway._stream

    async def patched(self, body):
        if str(body.get("method")) == "session/prompt":
            calls["n"] += 1
            self.prompt_mode = "transport" if calls["n"] == 1 else "ok"
        async for chunk in orig(self, body):
            yield chunk

    monkeypatch.setattr(FakeAcpGateway, "_stream", patched)

    card_id = await _make_coder_card(work["work_id"])
    st = message_runner.RunState(work_id=work["work_id"], card_id=card_id,
                                 content="干活")
    st.pair_id = "pair-resume"
    outcome = await bridge.maybe_delegate(st)
    assert outcome is not None
    # 断过一次 ⇒ 重试 ⇒ 走的是 **session/load**（恢复原会话）
    methods = [m for m, _ in fake_gw.calls]
    assert "session/load" in methods, methods
    # 两次 prompt 用的是**同一个会话 id**
    sids = [p["session_id"] for p in fake_gw.prompt_calls]
    assert len(sids) == 2 and sids[0] == sids[1], sids
    journal = event_stream.journal_of(work["work_id"], card_id)
    assert "wb_exec_resume" in [e.get("type") for e in journal]


# ===================================================================== 并行派发
@pytest.mark.asyncio
async def test_sequential_delegations_share_one_connection_and_get_own_sessions(
        work, wb_cfg, tmp_path, fake_gw, monkeypatch):
    """**同网关先后两张卡** ⇒

    ① 只建**一条** ACP 连接（ACP 端点没有 disconnect、网关 maxConnections=32
       ⇒ 并发时不能各建一条）；
    ② 两张卡拿到**各自的会话**（不串）；
    ③ 配比器跨任务推进 ⇒ 两张卡**拿到不同的模型**（不都打同一个）。

    **★ 为什么改成"先后"**（2026-10-24 实跑教训）：同一个网关**只有一个 agent 槽**
    ⇒ 两张卡**同时**进去时，第二张**占不到槽** ⇒ 早先这里是
    ``asyncio.Semaphore(1)`` ⇒ 它**无限等在信号量上** ⇒ **永久卡死**
    （现场：card_019 的事件流只有 ``wb_exec_start``，后续永不到）。
    ⇒ 现在改成**占/不占**：占不到就换候选/回落，**不排队**。
    同时并发的语义由
    :func:`test_same_executor_delegations_do_not_queue_but_fall_back` 守着。
    """
    # 同权重两个候选 ⇒ 配比器轮流挑 ⇒ 两张卡必然拿到不同模型
    p = _wb_cfg_payload(tmp_path, candidates=[
        {"executor": "wb-cn", "model": "hy3", "weight": 50},
        {"executor": "wb-cn", "model": "custom-local:deepseek-v4-pro",
         "weight": 50},
    ])
    monkeypatch.setenv(wb_config.ENV_CONFIG_PATH, str(p))
    wb_config.reset_cache()
    picker.reset_pickers()

    reg = bridge.get_registry()
    from src.api.wb_exec.registry import GatewayProcess
    from src.api.wb_exec.config import Executor
    _use_cli_pool(reg, monkeypatch, fake_gw, "wb-cn")
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)

    card_a = await _make_coder_card(work["work_id"], "并行A")
    card_b = await _make_coder_card(work["work_id"], "并行B")
    st_a = message_runner.RunState(work_id=work["work_id"], card_id=card_a,
                                   content="A 干活")
    st_a.pair_id = "par-a"
    st_b = message_runner.RunState(work_id=work["work_id"], card_id=card_b,
                                   content="B 干活")
    st_b.pair_id = "par-b"

    out_a = await bridge.maybe_delegate(st_a)
    out_b = await bridge.maybe_delegate(st_b)

    assert out_a is not None and out_b is not None
    # ① 连接只建一次
    assert fake_gw.connect_calls == 1, fake_gw.connect_calls
    # ② 两张卡各自的会话
    sids = {p["session_id"] for p in fake_gw.prompt_calls}
    assert len(sids) == 2, fake_gw.prompt_calls
    # ③ 配比器推进 ⇒ 两次挑的模型不同
    models = [v for _, c, v in fake_gw.set_config if c == "model"]
    assert len(set(models)) == 2, models
    # ④ 两张卡各拿到自己的正文（没串）
    assert st_a.final_reply == "做完了：已改 a.py"
    assert st_b.final_reply == "做完了：已改 a.py"
    wb_config.reset_cache()
    picker.reset_pickers()



@pytest.mark.asyncio
async def test_parallel_delegations_do_not_cross_events(work, wb_cfg, fake_gw,
                                                        monkeypatch):
    """并行时**别张卡的事件不能串进本卡**（实测坑：同连接上会话事件会串）。"""
    reg = bridge.get_registry()
    from src.api.wb_exec.registry import GatewayProcess
    from src.api.wb_exec.config import Executor
    _use_cli_pool(reg, monkeypatch, fake_gw, "wb-cn")
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)

    orig = FakeAcpGateway._stream

    async def patched(self, body):
        if str(body.get("method")) == "session/prompt":
            # 混入**别的会话**的正文
            yield self._notify("session/update", {
                "sessionId": "别人的会话",
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": "串台内容"}},
            })
        async for chunk in orig(self, body):
            yield chunk

    monkeypatch.setattr(FakeAcpGateway, "_stream", patched)

    card_a = await _make_coder_card(work["work_id"], "并行A")
    st_a = message_runner.RunState(work_id=work["work_id"], card_id=card_a,
                                   content="A 干活")
    st_a.pair_id = "par-x"
    await bridge.maybe_delegate(st_a)
    assert "串台内容" not in (st_a.final_reply or "")


@pytest.mark.asyncio
async def test_cross_executor_delegations_run_in_parallel(work, wb_cfg, fake_gw,
                                                          fake_gw_b, tmp_path,
                                                          monkeypatch):
    """**跨版本要真并行**：两版是两个独立网关进程 ⇒ 两个 agent 各跑各的。"""
    # 配置里要有两个执行体（默认夹具只有一个）
    p = _wb_cfg_payload(tmp_path, executors=[
        {"id": "wb-cn", "label": "CN", "install_root": str(tmp_path),
         "product_config": "", "cli_bin": "", "port": 0},
        {"id": "wb-intl", "label": "INTL", "install_root": str(tmp_path),
         "product_config": "", "cli_bin": "", "port": 0},
    ])
    monkeypatch.setenv(wb_config.ENV_CONFIG_PATH, str(p))
    wb_config.reset_cache()
    picker.reset_pickers()

    reg = bridge.get_registry()
    # 让两个 prompt 各花 0.4s ⇒ 重叠可测（否则瞬时完成、断言会假失败）
    fake_gw.prompt_delay = 0.4
    fake_gw_b.prompt_delay = 0.4
    _use_cli_pool(reg, monkeypatch, fake_gw, "wb-cn")
    _use_cli_pool(reg, monkeypatch, fake_gw_b, "wb-intl")
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)

    spans: list[tuple[float, float]] = []
    fake_gw.prompt_delay = 0.4      # 让"重叠/不重叠"可测
    orig = FakeAcpGateway._stream

    async def patched(self, body):
        if str(body.get("method")) == "session/prompt":
            t0 = time.time()
            async for chunk in orig(self, body):
                yield chunk
            spans.append((t0, time.time()))
        else:
            async for chunk in orig(self, body):
                yield chunk

    monkeypatch.setattr(FakeAcpGateway, "_stream", patched)

    card_a = await _make_coder_card(work["work_id"], "并A")
    card_b = await _make_coder_card(work["work_id"], "并B")
    st_a = message_runner.RunState(work_id=work["work_id"], card_id=card_a,
                                   content="A")
    st_a.pair_id = "cross-a"
    st_b = message_runner.RunState(work_id=work["work_id"], card_id=card_b,
                                   content="B")
    st_b.pair_id = "cross-b"

    # 把两张卡**钉到不同版本**（跨版本才该并行）
    from src.storage import model_override
    model_override.put_row(work["work_id"], card_a,
                           {"exec_backend": "wb:wb-cn:custom-local:deepseek-v4-pro"},
                           src="manual")
    model_override.put_row(work["work_id"], card_b,
                           {"exec_backend": "wb:wb-intl:hy3"}, src="manual")

    out_a, out_b = await asyncio.gather(
        bridge.maybe_delegate(st_a), bridge.maybe_delegate(st_b))
    assert out_a is not None and out_b is not None
    assert len(spans) == 2, spans
    (s1, e1), (s2, e2) = sorted(spans)
    assert e1 > s2, f"跨版本两段应当重叠（真并行）: {spans}"


@pytest.mark.asyncio
async def test_acquire_returns_desktop_gateway_without_spawning_children(
        work, tmp_path, fake_gw, monkeypatch):
    """**不再起租约子网关**（2026-10-23 18:10 实测证伪）。

    桌面端网关的 ``POST /api/v1/process/start`` 起出来的 CLI 子进程
    **继承不了登录态** ⇒ 调不了模型（要么 ``session/load`` 就
    ``Authentication required``，要么 prompt 时 ``refusal`` ＋ ``auth``）。
    ⇒ ``acquire`` 必须直接返回桌面端那一个网关，扩池机制整条作废。
    """
    p = _wb_cfg_payload(tmp_path, concurrency={"per_executor": 3})
    monkeypatch.setenv(wb_config.ENV_CONFIG_PATH, str(p))
    wb_config.reset_cache()
    picker.reset_pickers()

    reg = bridge.get_registry()
    from src.api.wb_exec.registry import GatewayProcess
    from src.api.wb_exec.config import Executor
    _use_cli_pool(reg, monkeypatch, fake_gw, "wb-cn")
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)

    got = await reg.acquire("wb-cn")
    assert got.base_url == fake_gw.base_url
    assert not hasattr(got, "lease_worker"), "不该再有租约 worker 概念"
    assert not hasattr(reg, "_grow_leases"), "扩池逻辑整条作废"
    # 再要一次仍然可用（不崩）
    got2 = await reg.acquire("wb-cn")
    assert got2.base_url == fake_gw.base_url
    wb_config.reset_cache()


@pytest.mark.asyncio
async def test_local_catalog_must_not_decide_model_availability(
        work, tmp_path, monkeypatch):
    """**本地目录里没有这个模型 ≠ 没有这个模型**（2026-10-23 20:45 实测更正）。

    早先拿 ``session/load`` 返回的 ``availableModels`` 当"该版本的模型目录" ⇒
    **那是被过滤过的本地列表**（国版只回 5 个）⇒ 国版的 ``hy3`` / ``step-5-preview``
    被误判成"没有" ⇒ **国版那 30% 权重全被跳过** ⇒ 实测国版这两个模型
    都能调（``stopReason=end_turn``）。

    ⇒ 现在**只有实测证据**算数：真调失败报 "Model unavailable" 才记下。
    """
    p = _wb_cfg_payload(tmp_path, candidates=[
        {"executor": "wb-intl", "model": "deepseek-v4.1-flash", "weight": 40},
        {"executor": "wb-cn", "model": "step-5-preview", "weight": 20},
        {"executor": "wb-intl", "model": "step-5-preview", "weight": 20},
        {"executor": "wb-cn", "model": "hy3", "weight": 10},
        {"executor": "wb-intl", "model": "hy3", "weight": 10},
    ])
    monkeypatch.setenv(wb_config.ENV_CONFIG_PATH, str(p))
    wb_config.reset_cache()
    picker.reset_pickers()

    reg = bridge.get_registry()
    # 国版 sidecar 的 session/load 目录（实测只有这 5 个，**但 hy3/step-5 能调**）
    reg.remember_models("wb-cn", ["fast-model", "balanced-model", "deep-model",
                                  "custom-local:deepseek-v4-pro",
                                  "custom-local:glm-5.3"])
    # **本地目录缺它 ⇒ 不代表没有** ⇒ 不得判为不可用（早先就是这么把国版判死的）
    assert reg.model_unavailable("wb-cn", "hy3") is False
    assert reg.model_unavailable("wb-cn", "step-5-preview") is False
    assert reg.model_unavailable("wb-intl", "hy3") is False

    # **实测报不可用** ⇒ 这才算（按状态判定，不是猜）
    reg.note_model_unavailable("wb-cn", "hy3")
    assert reg.model_unavailable("wb-cn", "hy3") is True
    assert reg.model_unavailable("wb-cn", "step-5-preview") is False
    assert reg.model_unavailable("wb-intl", "hy3") is False, "别的执行体不受影响"

    cfg = wb_config.load()
    pkr = picker.get_picker(cfg.candidates)
    picked = []
    for _ in range(10):
        c = pkr.pick()
        picked.append(f"{c.executor}:{c.model}")
    # **绝不能出现付费端点**
    for s in picked:
        assert "custom-local" not in s, s
        assert s.split(":")[1] not in ("fast-model", "balanced-model",
                                       "deep-model"), s
    wb_config.reset_cache()
    picker.reset_pickers()


def test_prompt_model_unavailable_raises_model_unavailable():
    """**模型不可用**的返回体 ⇒ 抛 ``WbModelUnavailable``（⇒ 换候选，不是失败）。

    实测形状（2026-10-23 20:45）：
    ``stopReason=refusal`` ＋ ``FAILED_MODEL_REQUEST`` ＋
    ``displayMsg:{"en":"Model unavailable. Please switch models"}``。
    """
    from src.api.wb_exec.acp_client import WbModelUnavailable
    from src.api.wb_exec.bridge import _raise_if_prompt_failed

    body = json.dumps({
        "code": -32603, "message": "Internal error",
        "data": {"statusCode": 400, "displayMsg": {
            "en": "Model unavailable. Please switch models",
            "zh": "当前模型不可用，请切换模型"}}}, ensure_ascii=False)
    with pytest.raises(WbModelUnavailable):
        _raise_if_prompt_failed({
            "stopReason": "refusal",
            "_meta": {"codebuddy.ai/errorMessage": body,
                      "codebuddy.ai/outcome": "FAILED_MODEL_REQUEST"}})


# ===================================================================== model_override
def test_model_override_exec_backend_roundtrip(work):
    """``exec_backend`` 随每卡行落盘/读回（执行体选择的落点）。"""
    from src.storage import model_override
    work_id = work["work_id"]
    model_override.put_row(work_id, "card_042",
                           {"exec_backend": "system"}, src="manual")
    row = model_override.get_row(work_id, "card_042")
    assert row.get("exec_backend") == "system"

    model_override.put_row(work_id, "card_043",
                           {"exec_backend": "wb:intl:hy3"}, src="manual")
    assert model_override.get_row(work_id, "card_043")["exec_backend"] \
        == "wb:intl:hy3"


def test_pinned_backend_parse():
    assert bridge._parse_pinned("wb:intl:deepseek-v4.1-flash") == \
        ("intl", "deepseek-v4.1-flash")
    assert bridge._parse_pinned("auto") is None
    assert bridge._parse_pinned("system") is None
    assert bridge._parse_pinned("wb:bad") is None


# ===================================================================== HTTP 端点
@pytest.mark.asyncio
async def test_exec_backend_endpoint_get_and_put(work, client, wb_cfg):
    """执行体端点：读得到选项、改得动、且**不抹掉该卡原有的模型覆盖**。"""
    from src.storage import model_override
    card_id = await _make_coder_card(work["work_id"])
    wid = work["work_id"]
    # 该卡原本有模型覆盖（model + fallbacks + bindings）——PUT 只能改 exec_backend
    model_override.put_row(wid, card_id, {
        "model": "some-model",
        "fallbacks": ["other-model"],
        "bindings": {"execute": "x-model"},
        "exec_backend": "auto",
    }, src="manual")

    r = await client.get(f"/api/works/{wid}/cards/{card_id}/exec-backend")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["backend"] == "auto"
    assert data["routed"] is None            # 未开工
    values = {o["value"] for o in data["options"]}
    assert "auto" in values and "system" in values
    # ★ **value 必须带 ``wb:`` 前缀**（2026-10-25 修的既有 bug）：
    #   ``put_exec_backend`` 的校验只接受 ``auto``/``system``/``wb:*``，
    #   而 options 早先生成的是 ``<executor>:<model>``（无前缀）
    #   ⇒ 前端下拉选任何一项、PUT 都被 400 拒 ⇒ **钉死执行体一直是失效的**。
    assert "wb:wb-cn:custom-local:deepseek-v4-pro" in values

    r2 = await client.put(f"/api/works/{wid}/cards/{card_id}/exec-backend",
                          json={"backend": "system"})
    assert r2.status_code == 200
    assert r2.json()["backend"] == "system"

    # **原有模型覆盖必须还在**（put_row 是整行替换 ⇒ 端点必须读-改-写）
    row = model_override.get_row(wid, card_id)
    assert row["exec_backend"] == "system"
    assert row["model"] == "some-model"
    assert row["fallbacks"] == ["other-model"]
    assert row["bindings"] == {"execute": "x-model"}

    # 非法值被拒
    r3 = await client.put(f"/api/works/{wid}/cards/{card_id}/exec-backend",
                          json={"backend": "wb-cn:不存在的模型"})
    assert r3.status_code == 400


@pytest.mark.asyncio
async def test_exec_backend_endpoint_shows_routed_after_work(work, client,
                                                            wb_cfg, fake_gw,
                                                            monkeypatch):
    """开工后 ⇒ 端点返回**实际路由到的**（版本, 模型）。"""
    reg = bridge.get_registry()
    from src.api.wb_exec.registry import GatewayProcess
    from src.api.wb_exec.config import Executor
    _use_cli_pool(reg, monkeypatch, fake_gw, "wb-cn")
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)

    card_id = await _make_coder_card(work["work_id"])
    wid = work["work_id"]
    st = message_runner.RunState(work_id=wid, card_id=card_id, content="干活")
    st.pair_id = "pair-004"
    outcome = await bridge.maybe_delegate(st)
    assert outcome is not None

    r = await client.get(f"/api/works/{wid}/cards/{card_id}/exec-backend")
    data = r.json()
    assert data["routed"], data
    assert data["routed"]["executor"] == "wb-cn"
    assert data["routed"]["model"] == "custom-local:deepseek-v4-pro"


# ===================================================================== 总开关
@pytest.mark.asyncio
async def test_master_switch_gates_delegation(work, wb_cfg, fake_gw,
                                                monkeypatch):
    """**手动总开关**：关 ⇒ 全部走系统自己的模型（不委派）；开 ⇒ 照常委派。

    用户令（2026-10-23 16:20）：*"再做一个手动开关，开启走workbuddy，
    关闭走系统模型"*。
    """
    from src.api.wb_exec import switch as _sw
    reg = bridge.get_registry()
    from src.api.wb_exec.registry import GatewayProcess
    from src.api.wb_exec.config import Executor
    _use_cli_pool(reg, monkeypatch, fake_gw, "wb-cn")
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)

    card_id = await _make_coder_card(work["work_id"])
    st = message_runner.RunState(work_id=work["work_id"], card_id=card_id,
                                 content="干活")
    st.pair_id = "pair-sw-on"
    # 先关
    _sw.set_enabled(False)
    assert _sw.is_enabled() is False
    assert await bridge.maybe_delegate(st) is None, "关着就不该委派"
    assert not fake_gw.prompt_calls, "关着就不该发 prompt"
    # 再开
    _sw.set_enabled(True)
    st2 = message_runner.RunState(work_id=work["work_id"], card_id=card_id,
                                  content="干活")
    st2.pair_id = "pair-sw-off"
    out = await bridge.maybe_delegate(st2)
    assert out is not None, "开着就该委派"
    assert fake_gw.prompt_calls
    _sw.set_enabled(True)


@pytest.mark.asyncio
async def test_master_switch_endpoints(work, client, wb_cfg):
    """总开关端点：GET 读、PUT 切（**运行时生效，不用重启**）。"""
    from src.api.wb_exec import switch as _sw
    r = await client.get("/api/wb-exec/switch")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "enabled" in data and "source" in data
    r2 = await client.put("/api/wb-exec/switch", json={"enabled": False})
    assert r2.status_code == 200
    assert r2.json()["enabled"] is False
    assert _sw.is_enabled() is False
    r3 = await client.put("/api/wb-exec/switch", json={"enabled": True})
    assert r3.status_code == 200
    assert r3.json()["enabled"] is True


# ===================================================================== 轮号
@pytest.mark.asyncio
async def test_round_number_advances_per_tool_call(work, wb_cfg):
    """**轮号语义**（2026-10-23 实跑两次踩坑后定稿）：

    · ``round`` ＝ **卡自己的轮号**（``state.rounds``）⇒ 同一轮里所有事件同一个号，
      不会和系统路径的轮号打架（早先我拿 WorkBuddy 的内部步当 round ⇒
      卡面同时出现"第 1 轮"和"第 4/5/6 轮"两套号）。
    · ``step`` ＝ **WorkBuddy 的内部步**（另放一个字段，供诊断看它推进到哪）。
    """
    from src.api.wb_exec.acp_client import AcpNotification
    card_id = await _make_coder_card(work["work_id"])
    st = message_runner.RunState(work_id=work["work_id"], card_id=card_id,
                                 content="干活")
    st.pair_id = "pair-round"
    st.rounds = 1                      # 卡现在在第 1 轮
    ex = bridge._Exec(state=st, cfg=wb_cfg,
                      picker=picker.get_picker(wb_cfg.candidates))

    def note(kind, **extra):
        upd = {"sessionUpdate": kind}
        upd.update(extra)
        return AcpNotification(method="session/update",
                               params={"sessionId": "card_x", "update": upd})

    bridge._on_notification(ex, note("agent_thought_chunk",
                                      content={"type": "text", "text": "想"}))
    for i in range(3):
        bridge._on_notification(ex, note("tool_call", toolCallId=f"tc{i}",
                                          title="Edit"))
        bridge._on_notification(ex, note("tool_call_update", toolCallId=f"tc{i}",
                                          status="completed"))
        bridge._on_notification(ex, note("agent_thought_chunk",
                                          content={"type": "text",
                                                   "text": f"第{i}步之后"}))

    journal = event_stream.journal_of(work["work_id"], card_id)
    events = [e for e in journal
              if e.get("type") in ("tool_call", "tool_result", "reasoning_chunk")]
    # round ＝ 卡的轮号 ⇒ 同一轮里**只有一个**号（不打架）
    rounds = {e.get("data", {}).get("round") for e in events}
    assert rounds == {1}, f"round 只能是卡的轮号 1: {rounds}"
    # step ＝ WorkBuddy 内部步 ⇒ **推进**
    steps = [e.get("data", {}).get("step") for e in events]
    steps = [x for x in steps if isinstance(x, int)]
    assert steps, "事件里应有 WorkBuddy 内部步"
    assert max(steps) > min(steps), f"内部步应推进: {steps}"


# ===================================================================== 并发槽位（2026-10-24）
# **背景（用户实跑反馈"任务卡死"）**：work_027 的 card_019 两次委派都停在
# ``wb_exec_start`` 之后、``wb_exec_model`` 之前 ⇒ 永久挂死。取证结论：
#   ① 早先用 ``asyncio.Semaphore(1)`` 按网关限流 ⇒ 后来的卡**无限等在信号量上**；
#   ② CLI 池 round-robin **不看占用** ⇒ 第 N+1 张卡分到正被占的进程；
#   ③ ``wait_until_idle(busy_wait_sec=900)`` ⇒ 空等 15 分钟；
#   ④ CLI 侧 ``acceptEdits`` **不放行 Bash/PowerShell** ⇒ agent 卡在
#      ``runStatus=waiting_for_permission`` ⇒ 那个槽**永久占着**。
# ⇒ 现在：**占/不占**（不排队）、池子占用感知、忙就换、权限给足。


def test_slot_claim_is_non_blocking():
    """**占槽不排队**：占不到就 False（**绝不无限等**）。"""
    from src.api.wb_exec.registry import ExecutorRegistry
    reg = ExecutorRegistry()
    assert reg.claim_slot("http://x:1", "A") is True
    assert reg.claim_slot("http://x:1", "B") is False, "别人占着就不能再占"
    assert reg.claim_slot("http://x:1", "A") is True, "同一持有者重复占 ⇒ 幂等"
    reg.release_slot("http://x:1", "B")
    assert reg.slot_holder("http://x:1") == "A", "不是持有者不许放别人的槽"
    reg.release_slot("http://x:1", "A")
    assert reg.slot_holder("http://x:1") == ""


def test_cli_permission_mode_is_bypass_by_default(tmp_path, monkeypatch):
    """**权限给足**（用户 2026-10-24："权限给它不就行了"）。

    ``acceptEdits`` 只自动放行"文件编辑"，**Bash / PowerShell 仍要人工审批**
    ⇒ agent 一跑命令就进 ``waiting_for_permission`` 干等 ⇒ 槽被永久占住。
    ⇒ CLI 侧默认 ``bypassPermissions``。
    """
    from src.api.wb_exec import config as _cfg
    assert _cfg.Guards().cli_permission_mode == "bypassPermissions"
    p = _wb_cfg_payload(tmp_path)
    monkeypatch.setenv(_cfg.ENV_CONFIG_PATH, str(p))
    _cfg.reset_cache()
    try:
        assert _cfg.load().guards.cli_permission_mode == "bypassPermissions"
    finally:
        _cfg.reset_cache()


def test_repo_config_uses_bypass_permissions():
    """仓库里的真配置也必须给足权限（否则实跑又会卡在审批）。"""
    from src.api.wb_exec import config as _cfg
    import os as _os
    old = _os.environ.get(_cfg.ENV_CONFIG_PATH)
    _os.environ[_cfg.ENV_CONFIG_PATH] = str(REPO / "config" / "wb_exec.json")
    _cfg.reset_cache()
    try:
        assert _cfg.load().guards.cli_permission_mode == "bypassPermissions"
    finally:
        _cfg.reset_cache()
        if old is None:
            _os.environ.pop(_cfg.ENV_CONFIG_PATH, None)
        else:
            _os.environ[_cfg.ENV_CONFIG_PATH] = old


@pytest.mark.asyncio
async def test_slot_full_queues_instead_of_falling_back(work, wb_cfg, fake_gw,
                                                         monkeypatch):
    """**槽满 ⇒ 排队等**（用户令 2026-10-24：「槽满就他妈排队」）。

    ★ **不回落系统模型** —— 让路＝走系统自己的模型＝**花钱**，用户不接受。
    ⇒ 两张卡打同一个 CLI 槽 ⇒ 第二张**排队**，第一张跑完后它接着跑。
    ⇒ 而且排队是**可见的**：事件流里有 ``wb_exec_queued``。
    """
    from src.api import event_stream as _ev_stream

    reg = bridge.get_registry()
    _use_cli_pool(reg, monkeypatch, fake_gw, "wb-cn")
    monkeypatch.setattr(bridge, "get_registry", lambda: reg)

    spans: list[tuple[float, float]] = []
    fake_gw.prompt_delay = 0.6
    orig = FakeAcpGateway._stream

    async def patched(self, body):
        if str(body.get("method")) == "session/prompt":
            t0 = time.time()
            async for chunk in orig(self, body):
                yield chunk
            spans.append((t0, time.time()))
        else:
            async for chunk in orig(self, body):
                yield chunk

    monkeypatch.setattr(FakeAcpGateway, "_stream", patched)

    card_a = await _make_coder_card(work["work_id"], "排A")
    card_b = await _make_coder_card(work["work_id"], "排B")
    st_a = message_runner.RunState(work_id=work["work_id"], card_id=card_a,
                                   content="A")
    st_a.pair_id = "q-a"
    st_b = message_runner.RunState(work_id=work["work_id"], card_id=card_b,
                                   content="B")
    st_b.pair_id = "q-b"
    out_a, out_b = await asyncio.gather(
        bridge.maybe_delegate(st_a), bridge.maybe_delegate(st_b))

    # **两张都跑成了**（排队的等到了，不是回落系统模型）
    assert out_a is not None and out_b is not None, \
        f"槽满应排队等，不该回落（{out_a}/{out_b}）"
    assert len(spans) == 2, spans
    # **串行**（同一个槽 ⇒ 两个 prompt 不重叠）
    (s1, e1), (s2, e2) = sorted(spans)
    assert e1 <= s2 + 0.05, f"同槽两段不该重叠: {spans}"
    # **排队是可见的** ⇒ 卡面能看见它在等（不是"卡死"）
    evs = event_stream.journal_of(work["work_id"], card_b)
    types = [e.get("type") for e in evs]
    assert "wb_exec_queued" in types, types

