# -*- coding: utf-8 -*-
"""端到端：全链路离线跑通「HTTP → 上下文组装 → 工具循环 → 落卡」。

card_012 工作项 5。两条纪律：

1. **不 import main**（`main.py:83-85` 导入期执行 startup_init，建目录、跑 git、
   写 pid）。改为自行用 `src.api.routes.router` 装配 app——同一批路由函数，
   少挂一个 `/static`，链路其余部分一字不改。
2. **零外网。** 两条路线：
   - 真 `MOCK_LLM=1` → `build_gateway` 返回 `MockGateway`（gateway.py:598），
     读真实 `config/models.yaml`，等价于「离线预检」的既有跑法；
   - `FakeGateway` 替身 → 用来定点触发工具轮与建卡提案（MockGateway 只在
     system_prompt 含「客观统合者」时假造工具调用，白板版已无该字样）。
   替身 `call` 签名与真网关逐参数对齐，否则测到的不是真实调用面。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import model_service, routes
from src.storage import shadow_store

FAKE_CONFIG: dict[str, Any] = {
    "models": {
        "main": {"provider": "openai", "model": "fake-main", "temperature": 0.3},
        "vp1": {"provider": "openai", "model": "fake-vp", "temperature": 0.7},
    },
    "simple": {"enabled": True, "viewpoints": ["vp1"]},
}


class FakeGateway:
    """model_service.get_gateway() 的替身：记录调用，按开关发工具调用／建卡块。"""

    def __init__(self, tool_script: list[dict[str, Any]] | None = None,
                 proposal: list[dict[str, Any]] | None = None):
        self.config = FAKE_CONFIG
        self.tool_script = tool_script or []
        self.proposal = proposal
        self.calls: list[dict[str, Any]] = []
        # [T-0001 §6-3] 工具脚本只发一次：主链路工具循环无轮次封顶，
        # 每轮都发会让模型永远在调工具（挂起，不是红）
        self._tool_issued = False

    async def call(self, model_name, system_prompt, user_prompt, temperature=0.5,
                   max_tokens=None, timeout=None, skip_completion_marker=False,
                   tools=None, on_status=None, stream=False, on_chunk=None,
                   prior_messages=None):
        self.calls.append({
            "model": model_name,
            "tools": [t["name"] for t in (tools or [])],
            "user_prompt": user_prompt,
            "prior_len": len(prior_messages or []),
        })
        if on_status:
            on_status({"phase": "start", "model": model_name})
            on_status({"phase": "success", "model": model_name})
        # [T-0001 §6-3] 一次性：首轮发脚本首个工具调用，之后不再发
        tool_calls = []
        if self.tool_script and tools and not self._tool_issued:
            tool_calls = [self.tool_script[0]]
            self._tool_issued = True
        return {
            "content": self._content(model_name),
            "tool_calls": tool_calls,
            "truncated_tool_call": None,
            "finish_reason": "stop",
            "tokens_used": 1,
            "latency_ms": 1,
        }

    async def stream_call(self, model_name, system_prompt, user_prompt, temperature=0.5,
                          max_tokens=None, timeout=None, on_status=None, tools=None,
                          prior_messages=None, **_):
        # 主路径 10-08 起走 gw.stream_call（model_service.call_main 内部）——本替身
        # 与 call 同款记录（calls 结构一致，断言按 model/user_prompt/prior_len 取），
        # 含让出点（真实网关每圈网络 IO 让出事件循环，人停刹车可达；Mock 零 IO
        # 需补一次对齐——S5 诊断定案）。async generator：async for 逐帧消费。
        import asyncio
        await asyncio.sleep(0)
        self.calls.append({
            "model": model_name,
            "tools": [t["name"] for t in (tools or [])],
            "user_prompt": user_prompt,
            "prior_len": len(prior_messages or []),
        })
        if on_status:
            on_status({"phase": "start", "model": model_name})
            on_status({"phase": "success", "model": model_name})
        tool_calls = []
        if self.tool_script and tools and not self._tool_issued:
            tool_calls = [self.tool_script[0]]
            self._tool_issued = True
        content = self._content(model_name)
        yield {"chunk_type": "content", "text": content}
        yield {"content": content, "tool_calls": tool_calls,
               "truncated_tool_call": None, "finish_reason": "stop",
               "tokens_used": 1, "latency_ms": 1}

    def _content(self, model_name: str) -> str:
        if model_name == "vp1":
            return "[替身观点:vp1] 这轮先别急着建卡。"
        if self.tool_script:
            return "先读一下这张卡。"
        if self.proposal:
            block = json.dumps(self.proposal, ensure_ascii=False)
            return f"我先建卡：\n\n```card-proposal\n{block}\n```\n"
        return "本轮无需建卡。"


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(routes.router)
    return a


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ---------------------------------------------------------------- 真离线网关

async def test_mock_llm_round_trip(client, sandbox, monkeypatch):
    """完成判据原文：MOCK_LLM=1 下跑通一轮消息，全程无外网。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    # 不替换 _gateway：让 model_service 读真实 config/models.yaml 建 MockGateway

    wid = (await client.post("/api/works", json={"title": "Mock 剧本"})).json()["work_id"]
    r = await client.post(f"/api/works/{wid}/cards/card_001/messages",
                          json={"content": "你好"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "mock"
    assert body["reply"].startswith("[mock:main]")

    # [思维链真实落地] 直答回合零三席（C6：三席只在拆解链收口后的提交轮进场）
    assert body["viewpoints_meta"] == []


# ---------------------------------------------------------------- 一轮干净对话

async def test_offline_round_trip(client, sandbox):
    model_service._gateway = FakeGateway()

    r = await client.post("/api/works", json={"title": "端到端剧本"})
    assert r.status_code == 201, r.text
    wid, root = r.json()["work_id"], r.json()["work_card_id"]

    r = await client.post(f"/api/works/{wid}/cards/{root}/messages",
                          json={"content": "你好"})
    assert r.status_code == 200, r.text
    body = r.json()
    # [T-0002 §6-6] 行为基线：parse_proposals 对正文无条件做 `.strip() + "\n"`
    # （card_proposal.py:166）→ API reply 恒带一个尾部换行。此处按现行为逐字钉死，
    # 不是放宽；该行为已记入 card_012 问题台账第 7 条，待步骤二/三裁定。
    assert body["reply"] == "本轮无需建卡。\n"
    assert body["model"] == "fake-main"      # 非 mock 模式：真实模型名字段
    assert body["tool_events"] == []
    assert body["created_cards"] == []

    # [思维链真实落地] 直答回合零三席（C6）
    assert body["viewpoints_meta"] == []

    # session 追加式：用户 ＋ 助手两段（[思维链真实落地] 判定块随独立判定轮
    # 退役——判断归模型，代码零判定，session 无 tool 形态判定块）。
    s = await client.get(f"/api/works/{wid}/cards/{root}/session")
    assert [m["role"] for m in s.json()] == ["user", "assistant"]

    # 无提案无工具 → 树没多出节点
    canvas = (await client.get(f"/api/works/{wid}/canvas")).json()
    assert [c["card_id"] for c in canvas["cards"]] == [root]

    # 审计双通道之一：prompt 日志落盘、字段完整、含三席实际收到的 system prompt
    logs = sorted(sandbox.logs.glob(f"prompt_{root}_*.json"))
    assert len(logs) == 1
    payload = json.loads(logs[0].read_text(encoding="utf-8"))
    assert payload["user_prompt"] == "你好"
    assert payload["error"] is None
    assert payload["model_used"] == "fake-main"
    assert payload["response"] == "本轮无需建卡。"
    assert payload["tool_events"] == []
    assert "## 卡片清单" in payload["vp_system_prompt"]
    # [思维链真实落地] 直答回合零三席 → prompt 日志 viewpoints 键不出现
    # （「空则不同构」纪律）；vp_system_prompt 仍落（白板证据位）。
    assert "viewpoints" not in payload


# ---------------------------------------------------------------- 工具循环闭环

async def test_tool_loop_closes_with_tool_result(client, sandbox):
    fake = FakeGateway(tool_script=[{"name": "read_card",
                                     "arguments": {"card_id": "card_001"}}])
    model_service._gateway = fake

    wid = (await client.post("/api/works", json={"title": "工具链剧本"})).json()["work_id"]
    r = await client.post(f"/api/works/{wid}/cards/card_001/messages",
                          json={"content": "看看根卡"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool_events"] == [{"tool": "read_card", "card_id": "card_001", "ok": True}]
    # [T-0002 §6-6→T14 更新] reply 尾部换行属现行为。带工具轮正文按 T14 新语义
    # 进交付（分析在前、回执在后）——FakeGateway 罐头两轮同文，故 reply 为同文两段；
    # 真实场景分析与回执不同文（同文重复＝罐头形态，观察项在 T14 报告）。
    assert body["reply"] == "先读一下这张卡。\n\n先读一下这张卡。\n"

    main_calls = [c for c in fake.calls if c["model"] == "main"]
    # [思维链真实落地] 主循环真实调用序（判定轮退役——判断归模型；直答链
    # 零三席）：模型调 read_card 拿结果后空手交付，全程 2 次主模型调用：
    #   0 = 首轮（带全量工具面、prior=0、发 read_card）
    #   1 = 工具续跳（"工具结果已返回。"、prior=3＝user 原文固化+assistant(tool_calls)+tool 结果）
    assert len(main_calls) == 2
    assert main_calls[0]["prior_len"] == 0
    assert main_calls[1]["prior_len"] == 3
    assert main_calls[1]["user_prompt"] == "工具结果已返回。"
    # 工具面：卡片工具 ＋ 文件工具都在首轮手里（判断+行动合一，全量工具面）
    assert "read_card" in main_calls[0]["tools"]
    assert "write_file" in main_calls[0]["tools"]


# ---------------------------------------------------------------- 建卡落树

async def test_card_proposal_creates_children(client, sandbox):
    proposals = [
        {"title": "子卡甲", "parent": "", "content": "甲正文"},
        {"title": "子卡乙", "parent": "card_001", "content": "乙正文"},
    ]
    model_service._gateway = FakeGateway(proposal=proposals)

    wid = (await client.post("/api/works", json={"title": "建卡剧本"})).json()["work_id"]
    r = await client.post(f"/api/works/{wid}/cards/card_001/messages",
                          json={"content": "拆一下"})
    body = r.json()

    # 协议块必须从回复正文里剥净
    assert "card-proposal" not in body["reply"]
    assert [c["title"] for c in body["created_cards"]] == ["子卡甲", "子卡乙"]
    # parent 留空 → 回退当前卡
    assert all(c["parent"] == "card_001" for c in body["created_cards"])

    canvas = (await client.get(f"/api/works/{wid}/canvas")).json()
    assert {c["card_id"]: c["title"] for c in canvas["cards"]} == {
        "card_001": "建卡剧本", "card_002": "子卡甲", "card_003": "子卡乙",
    }
    # 一正一影：新建卡自动派生影子
    assert shadow_store.shadow_path(wid, "card_002").exists()


# ---------------------------------------------------------------- 错误路径

async def test_missing_work_and_card_are_404(client, sandbox):
    assert (await client.get("/api/works/work_999/canvas")).status_code == 404
    r = await client.post("/api/works/work_999/cards/card_001/messages",
                          json={"content": "x"})
    assert r.status_code == 404

    wid = (await client.post("/api/works", json={"title": "空剧本"})).json()["work_id"]
    r = await client.post(f"/api/works/{wid}/cards/card_099/messages",
                          json={"content": "x"})
    assert r.status_code == 404


async def test_delete_root_card_is_403(client, sandbox):
    wid = (await client.post("/api/works", json={"title": "删根剧本"})).json()["work_id"]
    r = await client.delete(f"/api/works/{wid}/cards/card_001")
    assert r.status_code == 403