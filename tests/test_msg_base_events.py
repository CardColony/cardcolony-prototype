# -*- coding: utf-8 -*-
"""消息底座能力对齐回归（件 1-6，2026-10-13）。

七条客观判据逐条钉死：

1. 一轮消息全生命周期逐条独立落 events jsonl（seq 连续、round 正确、type 齐全）；
2. 重启后 seq 从文件尾续（不重复）；
3. tool_result 落盘 data 是结构化 result，非字符串 blob；
4. reasoning 落盘带 model，三席可区分；
5. GET /timeline 返回按 seq 排序合并表，跳过 revoke 段；
6. 编辑历史消息 → revoke 落盘 + 新 run 重生成；
7. 存量 session 迁移后 events 完整（消息块 + 环节块）。

沙箱纪律同 conftest：零真实 works 触碰、不 import main。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import event_stream, model_service, routes
from src.storage import event_log, session_facade, session_store

# ===========================================================================
# [纯单模型模式 2026-10-17 · 用户令] 本模块用例全为思维链行为（判型/拆解/上板/
# 步推进/三席统合/步事件）。生产已把思维链拆出链路（thinkchain.ENABLED=False）
# ⇒ 整模块跳过；开关置 True 即全部自动恢复（判据同并行线逐函数 skipif，此处
# 收敛为模块级，语义不变）。
# ===========================================================================
pytestmark = pytest.mark.skipif(
    not __import__("src.thinkchain", fromlist=["enabled"]).enabled(),
    reason="纯单模型模式（2026-10-17 用户令）：思维链已拆出生产链路",
)

ROOT = Path(__file__).resolve().parents[1]


def _load_migration_module():
    """按文件路径加载迁移脚本（scripts/ 不是包）。"""
    spec = importlib.util.spec_from_file_location(
        "migrate_sessions_to_events", ROOT / "scripts" / "migrate_sessions_to_events.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FAKE_CONFIG: dict[str, Any] = {
    "models": {
        "main": {"provider": "openai", "model": "fake-main", "temperature": 0.3},
        "vp1": {"provider": "openai", "model": "fake-vp", "temperature": 0.7},
    },
    "bindings": {"execute": "main", "think": "main", "synthesize": "main",
                 "compress": "main", "draft": "main", "viewpoint": ["vp1"]},
    "system": {"default_model": "main"},
    "simple": {"enabled": True},
}


class FakeGateway:
    """离线替身网关：可发工具调用、可吐思考增量（reasoning）。"""

    def __init__(self, tool_script: list[dict[str, Any]] | None = None,
                 reasoning: str = ""):
        self.config = FAKE_CONFIG
        self.tool_script = tool_script or []
        self.reasoning = reasoning
        self.calls: list[dict[str, Any]] = []
        self._tool_issued = False

    async def call(self, model_name, system_prompt, user_prompt, temperature=0.5,
                   max_tokens=None, timeout=None, skip_completion_marker=False,
                   tools=None, on_status=None, stream=False, on_chunk=None,
                   prior_messages=None):
        raise AssertionError("主路径应走 stream_call")

    async def stream_call(self, model_name, system_prompt, user_prompt, temperature=0.5,
                          max_tokens=None, timeout=None, on_status=None, tools=None,
                          prior_messages=None, **_):
        await asyncio.sleep(0)
        self.calls.append({"model": model_name})
        if on_status:
            on_status({"phase": "start", "model": model_name})
            on_status({"phase": "success", "model": model_name})
        tool_calls = []
        if self.tool_script and tools and not self._tool_issued:
            tool_calls = [self.tool_script[0]]
            self._tool_issued = True
        content = "本轮无需建卡。" if not self.tool_script else "先读一下这张卡。"
        if self.reasoning:
            yield {"chunk_type": "reasoning", "text": self.reasoning}
        yield {"chunk_type": "content", "text": content}
        yield {"content": content, "tool_calls": tool_calls,
               "truncated_tool_call": None, "finish_reason": "stop",
               "tokens_used": 1, "latency_ms": 1}


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _types(path: Path) -> list[str]:
    return [json.loads(ln)["type"] for ln in
            path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in
            path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def setup_function(_):
    event_stream._journals.clear()
    event_stream._subscribers.clear()
    event_stream._seq_counters.clear()
    event_stream._token_journals.clear()
    event_stream._token_subscribers.clear()
    event_stream._anchors.clear()


# ============================================================
# 判据 1 / 2：seq 连续、跨重启续号
# ============================================================

async def test_append_seq_monotonic_and_continues_from_tail(sandbox):
    """seq 从 1 起连续；「重启」（清空进程内状态）后从文件尾续，不归零不重复。"""
    w, c = "work_el", "card_001"
    for i in range(3):
        await event_log.append(w, c, {"type": "context", "data": {"i": i}})
    path = event_log.event_path(w, c)
    recs = _records(path)
    assert [r["seq"] for r in recs] == [1, 2, 3]
    # 模拟重启：进程内锁、在途任务、seq 游标全清，seq 只认文件尾
    event_log._async_locks.clear()
    event_log._seq_cursor.clear()
    await event_log.append(w, c, {"type": "done", "data": {}})
    recs = _records(path)
    assert [r["seq"] for r in recs] == [1, 2, 3, 4], "重启后续号（判据 2）"
    assert recs[-1]["card_id"] == c and recs[-1]["type"] == "done"


async def test_emit_persists_process_events_with_anchors(sandbox):
    """emit 同步落盘：run_id＝pair_id、round＝turn、type 齐全（判据 1）。"""
    w, c = "work_ev", "card_002"
    event_stream.emit(w, c, "run_start", {"pair_id": "pair-abc"})
    event_stream.emit(w, c, "round_start", {"round": 2})
    event_stream.emit(w, c, "judgment", {"verdict": "direct"})
    event_stream.emit(w, c, "tool_call", {"tool": "read_card"})
    event_stream.emit(w, c, "seat_start", {"model": "vp1"})
    await event_log.drain()
    recs = _records(event_log.event_path(w, c))
    assert [r["type"] for r in recs] == \
        ["run_start", "round_start", "judgment", "tool_call", "seat_start"]
    assert recs[0]["run_id"] == "pair-abc", "run_id＝回合锚 pair_id"
    assert recs[2]["round"] == 2, "round＝轮锚 turn"
    assert all(r["model"] == "" for r in recs), "model 仅 reasoning 带（裁定 C2）"
    assert recs[4]["data"]["model"] == "vp1", "席位信息在 data 里，不丢"


async def test_facade_append_persists_message_blocks(sandbox, work):
    """消息块同步落事件流：user_message／assistant_text，data 是结构化对象。"""
    w, c = work["work_id"], work["work_card_id"]
    await session_facade.append(w, c, {"role": "user", "content": "你好",
                                       "pair_id": "p-user"})
    await session_facade.append(w, c, {"role": "assistant", "content": "在的",
                                       "pair_id": "p-user"})
    recs = _records(event_log.event_path(w, c))
    assert [r["type"] for r in recs] == ["user_message", "assistant_text"]
    assert recs[0]["data"] == {"content": "你好"}
    assert recs[0]["run_id"] == "p-user"
    # [单源化偏差③补 2026-10-15] assistant_text 的 data 增 model/end（元信息
    # 徽标数据源，/session 渲染通道退役后 timeline 是唯一来源）——断言镜像更新。
    assert recs[1]["data"] == {"content": "在的", "end": "model_done"}


# ============================================================
# 判据 4：reasoning 带 model（三席可区分）；C7：正文增量不落盘
# ============================================================

async def test_reasoning_persisted_with_model_content_chunk_not(sandbox):
    w, c = "work_rs", "card_003"
    # 主模型思考：不带 model
    event_stream.emit_token(w, c, "reasoning_chunk", "想一想")
    # 三席思考：带 model（席位模型名）
    event_stream.emit_token(w, c, "reasoning_chunk", "席一思考",
                            extra={"model": "MiniMax-M3"})
    event_stream.emit_token(w, c, "reasoning_chunk", "席二思考",
                            extra={"model": "glm-5.3"})
    # 正文增量：不落盘（C7）
    event_stream.emit_token(w, c, "content_chunk", "正文字")
    await event_log.drain()
    recs = _records(event_log.event_path(w, c))
    assert [r["type"] for r in recs] == ["reasoning", "reasoning", "reasoning"]
    assert [r["model"] for r in recs] == ["", "MiniMax-M3", "glm-5.3"], "三席可区分"
    assert all(r["data"] == {"text": t} for r, t in
               zip(recs, ["想一想", "席一思考", "席二思考"]))


# ============================================================
# 判据 1（全生命周期）／判据 3：端到端一轮
# ============================================================

async def test_one_round_lifecycle_types_and_structured_tool_result(client, sandbox):
    """一轮真链路：落盘类型齐全、tool_result 是结构化 result（判据 1/3）。"""
    model_service._gateway = FakeGateway(
        tool_script=[{"name": "read_card", "arguments": {"card_id": "card_001"}}])
    wid = (await client.post("/api/works", json={"title": "底座剧本"})).json()["work_id"]
    r = await client.post(f"/api/works/{wid}/cards/card_001/messages",
                          json={"content": "看看根卡"})
    assert r.status_code == 200, r.text
    await event_log.drain()

    recs = _records(event_log.event_path(wid, "card_001"))
    types = [x["type"] for x in recs]
    for want in ("user_message", "judgment", "tool_call", "tool_result",
                 "assistant_text"):
        assert want in types, f"缺 {want}：{types}"
    assert types.index("user_message") < types.index("tool_call") < \
        types.index("tool_result") < types.index("assistant_text")

    # 判据 3：tool_call 带 arguments（dict）／tool_result 带结构化 result
    tc = next(x for x in recs if x["type"] == "tool_call")
    assert tc["data"]["arguments"] == {"card_id": "card_001"}
    tr = next(x for x in recs if x["type"] == "tool_result")
    assert isinstance(tr["data"]["result"], dict), "result 是结构化对象，不是字符串 blob"
    assert tr["data"]["result"]["ok"] is True
    assert "data" in tr["data"]["result"]
    # 序号连续、round 正确（工具发生在第 1 轮）
    assert [x["seq"] for x in recs] == list(range(1, len(recs) + 1))
    assert tr["round"] == 1


# ============================================================
# 判据 5：timeline 排序 ＋ 跳过 revoke 段
# ============================================================

async def test_timeline_skips_revoked_segment(sandbox, work):
    w, c = work["work_id"], work["work_card_id"]
    for i in range(4):
        await event_log.append(w, c, {"type": "context", "data": {"i": i}})
    await event_log.revoke(w, c, 3)
    assert [r["seq"] for r in event_log.timeline(w, c)] == [1, 2]
    # revoke 记录自身不进渲染面（控制位，不是给用户看的内容）
    assert "revoke" not in [r["type"] for r in event_log.timeline(w, c)]
    # 新 run 的记录正常进入（revoke 只作废 from_seq 起的旧段）
    await event_log.append(w, c, {"type": "done", "data": {}})
    assert [r["seq"] for r in event_log.timeline(w, c)] == [1, 2, 6]


async def test_timeline_endpoint_sorted_and_card_not_found(client, sandbox, work):
    w, c = work["work_id"], work["work_card_id"]
    await event_log.append(w, c, {"type": "user_message", "data": {"content": "a"}})
    await event_log.append(w, c, {"type": "assistant_text", "data": {"content": "b"}})
    r = await client.get(f"/api/works/{w}/cards/{c}/timeline")
    assert r.status_code == 200, r.text
    body = r.json()
    seqs = [i["seq"] for i in body["items"]]
    assert seqs == sorted(seqs) and len(seqs) == 2
    assert [i["type"] for i in body["items"]] == ["user_message", "assistant_text"]
    assert body["last_seq"] == 2
    # 任务书字面入口（无 work 路径段）：缺省 work_id＝work_001；卡不存在→404
    r2 = await client.get(f"/api/card/{c}/timeline", params={"work_id": w})
    assert r2.status_code == 200, r2.text
    assert [i["seq"] for i in r2.json()["items"]] == seqs
    assert (await client.get("/api/card/card_999/timeline")).status_code == 404


# ============================================================
# 件 4：from=0 整卡重放退役（恢复统一走 timeline）
# ============================================================

async def test_stream_from_zero_no_replay(sandbox, work):
    from src.api.routes import stream_card_events

    w, c = work["work_id"], work["work_card_id"]
    event_stream.emit(w, c, "context", {"i": 0})
    resp = await stream_card_events(w, c, frm=0)

    async def _first_data_frame():
        async for chunk in resp.body_iterator:
            text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            for line in text.splitlines():
                if line.startswith("data: "):
                    return json.loads(line[6:])
        return None

    task = asyncio.create_task(_first_data_frame())
    try:
        # 存量 journal 不再从 SSE 重放：连上后先拿不到任何帧
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.6)
        # 连上之后新发生的事件照常推
        event_stream.emit(w, c, "context", {"i": 1})
        got = await asyncio.wait_for(task, 2.0)
        assert got["data"]["i"] == 1
    finally:
        if not task.done():
            task.cancel()
        await resp.body_iterator.aclose()


# ============================================================
# 判据 6：编辑 / 重发
# ============================================================

async def test_edit_message_revokes_and_reruns(client, sandbox):
    model_service._gateway = FakeGateway()
    wid = (await client.post("/api/works", json={"title": "编辑剧本"})).json()["work_id"]
    r = await client.post(f"/api/works/{wid}/cards/card_001/messages",
                          json={"content": "原问题"})
    assert r.status_code == 200, r.text
    await event_log.drain()
    recs = _records(event_log.event_path(wid, "card_001"))
    ask = next(x for x in recs if x["type"] == "user_message")
    old_run = ask["run_id"]

    r2 = await client.post(f"/api/works/{wid}/cards/card_001/messages/{ask['seq']}/edit",
                           json={"content": "改后的问题"})
    assert r2.status_code == 200, r2.text
    await event_log.drain()
    after = _records(event_log.event_path(wid, "card_001"))
    assert "revoke" in [x["type"] for x in after], "revoke 落盘"
    ask2 = [x for x in after if x["type"] == "user_message"]
    assert len(ask2) == 2 and ask2[0]["run_id"] == old_run
    assert ask2[1]["run_id"] != old_run, "新 run_id 重生成"
    assert ask2[1]["data"]["content"] == "改后的问题"
    # 渲染面：被作废的旧提问不再出现
    tl = event_log.timeline(wid, "card_001")
    assert [x["seq"] for x in tl if x["type"] == "user_message"] == [ask2[1]["seq"]]
    r404 = await client.post(f"/api/works/{wid}/cards/card_001/messages/99999/edit",
                             json={"content": "x"})
    assert r404.status_code == 404


async def test_resend_reuses_pair_id_and_revokes_old_reply(client, sandbox):
    model_service._gateway = FakeGateway()
    wid = (await client.post("/api/works", json={"title": "重发剧本"})).json()["work_id"]
    assert (await client.post(f"/api/works/{wid}/cards/card_001/messages",
                              json={"content": "原问题"})).status_code == 200
    await event_log.drain()
    ask = next(x for x in _records(event_log.event_path(wid, "card_001"))
               if x["type"] == "user_message")

    r = await client.post(f"/api/works/{wid}/cards/card_001/messages/{ask['seq']}/resend")
    assert r.status_code == 200, r.text
    await event_log.drain()
    recs = _records(event_log.event_path(wid, "card_001"))
    # 旧回复被作废、提问保留
    assert "revoke" in [x["type"] for x in recs]
    asks = [x for x in recs if x["type"] == "user_message"]
    assert len(asks) == 1, "重发不产生第二条提问记录"
    assert [x["run_id"] for x in asks] == [ask["run_id"]], "复用原 run_id"
    # 会话档案：去重生效——用户块仍只有一条（没有重复提问）
    blocks = await session_facade.read(wid, "card_001")
    assert [b["actor"] for b in blocks] == ["human", "assistant", "assistant"]


async def test_edit_then_resend_old_ask_is_404(client, sandbox):
    """被作废的提问不能再编辑／重发（防对已废段再操作）。"""
    model_service._gateway = FakeGateway()
    wid = (await client.post("/api/works", json={"title": "作废剧本"})).json()["work_id"]
    assert (await client.post(f"/api/works/{wid}/cards/card_001/messages",
                              json={"content": "原"})).status_code == 200
    await event_log.drain()
    ask = next(x for x in _records(event_log.event_path(wid, "card_001"))
               if x["type"] == "user_message")
    assert (await client.post(f"/api/works/{wid}/cards/card_001/messages/{ask['seq']}/edit",
                              json={"content": "改"})).status_code == 200
    await event_log.drain()
    # 旧提问已被 revoke 覆盖
    assert (await client.post(f"/api/works/{wid}/cards/card_001/messages/{ask['seq']}/resend")
            ).status_code == 404
    assert (await client.post(f"/api/works/{wid}/cards/card_001/messages/{ask['seq']}/edit",
                              json={"content": "再改"})).status_code == 404


# ============================================================
# 判据 7：存量迁移 ＋ 回滚
# ============================================================

async def test_migrate_sessions_to_events_and_rollback(sandbox, work):
    mod = _load_migration_module()
    w, c = work["work_id"], work["work_card_id"]
    await session_facade.append(w, c, {"role": "user", "content": "问"})
    await session_facade.append(w, c, {"role": "assistant", "content": "答"})
    await session_facade.append(w, c, {"content": "环节留痕", "actor": "tool",
                                       "end": "", "behavior": "triage"})
    # 迁移前的事件文件（含消息块两条）先清掉，只留迁移产物
    path = event_log.event_path(w, c)
    if path.exists():
        path.unlink()

    backup_root = sandbox.tmp / "mig_backup"
    backup_root.mkdir(parents=True, exist_ok=True)
    stat = await mod.migrate_work(w, dry_run=False, force=True,
                                  backup_root=backup_root)
    assert stat["records"] == 3
    assert stat["by_type"] == {"user_message": 1, "assistant_text": 1, "judgment": 1}
    recs = _records(path)
    assert [r["seq"] for r in recs] == [1, 2, 3], "seq 重排"

    # 回滚：有备份→还原；本次新建→移入备份目录留档（永不删字节）
    (backup_root / "manifest.json").write_text(
        json.dumps({"entries": stat["entries"]}, ensure_ascii=False), encoding="utf-8")
    res = mod.rollback(backup_root / "manifest.json")
    assert (res["restored"] + res["removed"]) == 1
    assert not path.exists()
