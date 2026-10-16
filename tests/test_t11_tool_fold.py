# -*- coding: utf-8 -*-
"""T11 · 轮槽折叠器离线套件（V3/V5/V7/V8＋不折红线三条＋T9 开关＋T10 白名单）。

折叠压缩的模型压出在行为用例里 monkeypatch _compress_round（确定性结论）；
端到端用例走真实 gateway 链路（MOCK_LLM=1）驱动 routes._run_message 五轮
工具循环，验证折叠关/开的装配与落盘。
"""
from __future__ import annotations

import json

import pytest

from src.api import card_tools, prompt_log, routes, tool_fold
from src.api.tool_fold import KEEP_WINDOW, ToolTurnSlot, fold_enabled
from src.storage import session_facade, session_store


# ---------------------------------------------------------------------------
# T9：开关解析（缺省关；字面 "1"/"true" 开，其余关）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expect", [
    (None, False), ("", False), ("1", True), ("true", True),
    ("0", False), ("false", False), ("True", False), ("TRUE", False),
    ("yes", False), (" on", False), ("1 ", False),
])
def test_t9_switch_parsing(raw, expect, monkeypatch):
    if raw is None:
        monkeypatch.delenv("TOOL_FOLD_ENABLED", raising=False)
    else:
        monkeypatch.setenv("TOOL_FOLD_ENABLED", raw)
    assert fold_enabled() is expect


# ---------------------------------------------------------------------------
# T10：只读白名单（落工具定义处；10 只读，写工具不在列）
# ---------------------------------------------------------------------------


def test_t10_readonly_whitelist():
    # [T12] 增补 read_memory（记忆锚点下探，condense_fold 展开通道，只读拉取）——
    # T11 原名单一字不动，白名单随工具表 append 最小随改。
    # [B-11 2026-10-12] 联网两件插件化：未配搜索通道时被剪枝，故名单按注册表
    # 现状求交集，不写死。写死会在「配了通道」与「没配通道」两种环境里
    # 必有一种变假红——件数不该是魔法数字。
    expected = {
        "read_card", "read_shadow", "list_manuscript", "read_manuscript",
        "search_manuscript", "web_search", "fetch_web",
        "list_files", "read_file", "search_files",
        "read_memory",
        # [卡片底座第一步 2026-10-14] 新增只读工具 read_linked（沿通信线读）
        "read_linked",
        # [规则卡 2026-10-15 · 块3] 新增 draft_rule_edit（根卡起草规则区修改，
        # 全程零写入——落盘走用户审批 /rule/commit，不经过本工具）
        "draft_rule_edit",
    } & set(card_tools.TOOL_REGISTRY)

    assert card_tools.READONLY_TOOLS == frozenset(expected)
    for w in ("edit_card", "delete_card", "update_shadow",
              "link_cards", "unlink_cards", "write_file"):
        assert w not in card_tools.READONLY_TOOLS


# ---------------------------------------------------------------------------
# 折叠关：轮槽零加工（T6 判据——逐字同旧）
# ---------------------------------------------------------------------------


def _round(call_id: str, name: str, args: dict, result: dict) -> tuple[dict, dict]:
    assistant = {"role": "assistant", "content": f"正文-{call_id}",
                 "tool_calls": [{"id": call_id, "type": "function",
                                 "function": {"name": name,
                                              "arguments": json.dumps(args, ensure_ascii=False)}}]}
    tool = {"role": "tool", "tool_call_id": call_id,
            "content": json.dumps(result, ensure_ascii=False)}
    return assistant, tool


def _build_slot_with_four_rounds():
    messages: list[dict] = [{"role": "user", "content": "用户原文"}]
    slot = ToolTurnSlot(messages)
    slot.append_plain({"role": "user", "content": "用户原文"})  # 首轮固化
    for i in range(1, 5):
        a, t = _round(f"call_{i}", "read_card", {"card_id": f"card_{i:03d}"},
                      {"card_id": f"card_{i:03d}", "body": f"第 {i} 轮返回正文" * (i * 200)})
        slot.begin_round(a)
        slot.append_tool_result(t)
    slot.append_plain({"role": "assistant", "content": "抢救回注正文"})  # 抢救回注
    return slot, messages


def test_fold_off_wire_is_same_list_object(monkeypatch):
    monkeypatch.delenv("TOOL_FOLD_ENABLED", raising=False)
    slot, messages = _build_slot_with_four_rounds()
    wire = _sync_wire(slot)
    assert wire is messages
    roles = [m["role"] for m in wire]
    assert roles == ["user", "user", "assistant", "tool"] * 1 + ["assistant", "tool"] * 3 + ["assistant"]
    # 内容逐字（折叠关零加工）
    assert wire[3]["content"] == json.dumps(
        {"card_id": "card_001", "body": "第 1 轮返回正文" * 200}, ensure_ascii=False)


def _sync_wire(slot):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(slot.wire_messages()) \
        if False else asyncio.run(slot.wire_messages())


# ---------------------------------------------------------------------------
# 折叠开：两件套、配对结构、窗口 K=2、红线、fold_ops（V3/V7）
# ---------------------------------------------------------------------------


@pytest.fixture
def fold_on(monkeypatch):
    monkeypatch.setenv("TOOL_FOLD_ENABLED", "1")
    return None


def test_fold_on_two_piece_and_pairing(fold_on, monkeypatch):
    async def fake_compress(am, tms):
        return f"压缩结论-{len(tms)}条返回"

    monkeypatch.setattr(tool_fold, "_compress_round", fake_compress)
    slot, messages = _build_slot_with_four_rounds()
    wire = _sync_wire(slot)

    # 消息条数不变（V3 前提：只压内容、不删消息）
    assert len(wire) == len(messages)
    # 原列表永不改写
    assert messages[3]["content"] == json.dumps(
        {"card_id": "card_001", "body": "第 1 轮返回正文" * 200}, ensure_ascii=False)

    # 4 轮 → 窗口外 = 轮 1、2 折叠；轮 3、4（最近 K=2）全文
    folded_rounds = [1, 2]
    full_rounds = [3, 4]
    for i in folded_rounds:
        a, t = wire[2 + (i - 1) * 2], wire[3 + (i - 1) * 2]
        # V7：工具返回含【结论】＋【依据】两件套，逐字可辨
        assert t["content"].startswith("【结论】压缩结论-1条返回\n【依据】")
        assert "【依据】工具=read_card；参数={card_id=" in t["content"]
        assert "可按原参数重跑该工具重取原文" in t["content"]
        # 入参折叠：arguments 换成 folded 标记 JSON（含依据），id/结构不变
        assert a["role"] == "assistant" and a["content"] == f"正文-call_{i}"
        fc = a["tool_calls"][0]
        assert fc["id"] == f"call_{i}"
        folded_args = json.loads(fc["function"]["arguments"])
        assert folded_args["folded"] is True and folded_args["tool"] == "read_card"
        assert "依据" in folded_args
        # 配对一一对应（V3）
        assert t["tool_call_id"] == fc["id"]
    for i in full_rounds:
        _a, t = wire[2 + (i - 1) * 2], wire[3 + (i - 1) * 2]
        assert t["content"] == json.dumps(
            {"card_id": f"card_{i:03d}", "body": f"第 {i} 轮返回正文" * (i * 200)},
            ensure_ascii=False)

    # 红线：首轮用户原文固化与抢救回注原样在场
    assert wire[0] == {"role": "user", "content": "用户原文"}
    assert wire[-1] == {"role": "assistant", "content": "抢救回注正文"}

    # fold_ops：两轮折叠账（原字节/折后字节/结果）
    ops = slot.fold_ops
    assert [o["round"] for o in ops] == [1, 2]
    assert all(o["ok"] for o in ops)
    assert all(o["folded_bytes"] < o["original_bytes"] for o in ops)
    assert ops[0]["tools"] == ["read_card"]


def test_v8_no_length_cap_on_conclusion_and_evidence(fold_on, monkeypatch):
    """【结论】＋【依据】无长度上限、无 200B 截断：50KB 结论逐字在场。"""
    big_conclusion = "很长的结论" * 12800  # 64000 字
    async def fake_compress(am, tms):
        return big_conclusion

    monkeypatch.setattr(tool_fold, "_compress_round", fake_compress)
    messages: list[dict] = []
    slot = ToolTurnSlot(messages)
    big_body = "X" * 200000  # 200KB 工具返回
    a, t = _round("call_1", "write_file", {"path": "a/b.txt", "content": "Y" * 100000},
                  {"ok": True, "echo": big_body})
    slot.begin_round(a)
    slot.append_tool_result(t)
    a2, t2 = _round("call_2", "list_manuscript", {}, {"chapters": []})
    slot.begin_round(a2)
    slot.append_tool_result(t2)
    a3, _ = _round("call_3", "read_card", {"card_id": "card_003"}, {"body": "r3"})
    slot.begin_round(a3)
    slot.append_tool_result(t3 := {"role": "tool", "tool_call_id": "call_3",
                                   "content": json.dumps({"body": "r3"}, ensure_ascii=False)})

    wire = _sync_wire(slot)
    folded_tool = wire[1]
    # 结论全量在场（无截断——【结论】整段逐字、依据整段逐字）
    assert big_conclusion in folded_tool["content"]
    assert folded_tool["content"].count(big_conclusion) == 1
    assert folded_tool["content"].count("【结论】") == 1
    assert folded_tool["content"].count("【依据】") == 1
    # 依据里 content 全文不出现（折叠本身），只有长度标记；标记数字精确
    assert "Y" * 1000 not in folded_tool["content"]
    assert "content=<已折叠，共 100000 字>" in folded_tool["content"]
    # 工具返回的 200KB 原文也不在折叠替身里（整段被两件套替换），但原列表保真
    assert big_body in messages[1]["content"]
    assert json.loads(messages[1]["content"])["echo"] == big_body


def test_v5_failure_not_silent_full_text_kept(fold_on, monkeypatch):
    async def failing_compress(am, tms):
        raise RuntimeError("模型压缩调用失败（注入）")

    monkeypatch.setattr(tool_fold, "_compress_round", failing_compress)
    slot, messages = _build_slot_with_four_rounds()
    wire = _sync_wire(slot)
    # 全部轮次全文保留（该折的没折＝没省下，不是数据错了）
    assert [m["role"] for m in wire] == [m["role"] for m in messages]
    for w, o in zip(wire, messages):
        assert w == o
    # 留痕：fold_ops 记错误条目（不留白）
    assert len(slot.fold_ops) == 2
    assert all(not o["ok"] for o in slot.fold_ops)
    assert "注入" in slot.fold_ops[0]["error"]
    assert slot.fold_ops[0]["folded_bytes"] == slot.fold_ops[0]["original_bytes"]
    # 同请求内已失败轮不重试：第二次 wire 不再新增错误条目
    _sync_wire(slot)
    assert len(slot.fold_ops) == 2


def test_v6_switch_off_returns_legacy_path(fold_on, monkeypatch):
    """开关一关即回旧路：同一槽，关闸后 wire 与折叠关逐字一致、无需数据迁移。"""
    async def fake_compress(am, tms):
        return "结论"

    monkeypatch.setattr(tool_fold, "_compress_round", fake_compress)
    slot, messages = _build_slot_with_four_rounds()
    wire_on = _sync_wire(slot)
    assert any("【结论】" in m.get("content", "") for m in wire_on)
    monkeypatch.delenv("TOOL_FOLD_ENABLED", raising=False)
    wire_off = _sync_wire(slot)
    assert wire_off is messages
    for w, o in zip(wire_off, messages):
        assert w == o


def test_window_keeps_last_two_full(fold_on, monkeypatch):
    async def fake_compress(am, tms):
        return "结论"

    monkeypatch.setattr(tool_fold, "_compress_round", fake_compress)
    slot, _messages = _build_slot_with_four_rounds()
    assert slot.round_count == 4
    assert KEEP_WINDOW == 2
    wire = _sync_wire(slot)
    # 槽内布局：[seed, 首轮固化, (A1,T1), (A2,T2), (A3,T3), (A4,T4), 抢救回注]
    # 最近 2 轮（轮 3、4）全文＝「当前轮全文」红线的机械保证
    assert "【结论】" not in wire[7]["content"]
    assert "【结论】" not in wire[9]["content"]
    assert "【结论】" in wire[3]["content"]
    assert "【结论】" in wire[5]["content"]


# ---------------------------------------------------------------------------
# 端到端：routes._run_message 五轮工具循环（MOCK_LLM=1，真实工具执行）
# ---------------------------------------------------------------------------


async def test_e2e_tool_loop_with_slot(work, sandbox, monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("TOOL_FOLD_ENABLED", "1")
    wid = work["work_id"]

    big = "行内容一二三四五六七八九十\n" * 12000  # ≈330KB 文本
    big_path = sandbox.base / "trading-workspace" / "big_e2e.txt"
    big_path.parent.mkdir(parents=True, exist_ok=True)
    big_path.write_text(big, encoding="utf-8")

    calls = {"n": 0}
    captured_wires: list[list[dict]] = []

    def _tc(name, args):
        return {"name": name, "arguments": args}

    # [销红 2026-10-12] G.1 替身签名裁定：生产链路 message_runner 现会传 model_name=
    # （model_service.py:50-56 call_main 签名含 model_name: Optional[str] = None，
    # [参数收口 2026-10-07]新增）。替身缺该形参 ⇒ TypeError ⇒ _run_message 走错误返回
    # 路径返回 JSONResponse ⇒ :304 的 `"error" not in result` 撞上不可迭代。
    # 照 call_main 的签名顺序与默认值补形参；:304 断言不动（替身修好后自然成立）。
    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None):
        calls["n"] += 1
        captured_wires.append([dict(m) for m in (prior_messages or [])])
        n = calls["n"]
        if n == 1:      # round 1（simple 统合段）：大文件读取
            return ({"content": "", "tool_calls": [
                        _tc("read_file", {"path": "trading-workspace/big_e2e.txt"})]},
                    "fake")
        if n == 2:      # round 2：写工具（全文在入参）
            return ({"content": "", "tool_calls": [
                        _tc("update_shadow", {"content": "影子正文" * 20000})]},
                    "fake")
        if n == 3:      # round 3：list_manuscript
            return ({"content": "", "tool_calls": [_tc("list_manuscript", {})]}, "fake")
        if n == 4:      # round 4：小文件读取
            return ({"content": "", "tool_calls": [
                        _tc("read_file", {"path": "trading-workspace/big_e2e.txt",
                                          "para_start": 1, "para_end": 2})]},
                    "fake")
        return ({"content": "五轮循环收束回复。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)

    result = await routes._run_message(wid, "card_001", "端到端折叠场景提问")
    assert "error" not in result
    assert result["reply"].strip() == "五轮循环收束回复。"
    assert calls["n"] == 5

    # 会话文件：只有 user＋assistant 两条，折叠替身不进会话（约束 9）
    # [销红 2026-10-12] G.1 级联修正（修替身签名后本行才执行到，任务书未载的新发现）：
    # a0c9aae（会话底座重构）起落盘为 V2 块格式（session_facade.append:510-516 写 role 键），
    # 而 session_store.read_session 是 V1-only 旧读口（:138 只解析 V1 的 `role:` 头），
    # 读 V2 文件得空 role ⇒ ['','']。改用生产统一读口 session_facade.read
    # （session_facade.py:335「统一入口：V1 委托旧读，V2 走 V2 解析」，V2 块含 role 键）。
    # 断言本体（两条、user→assistant、折叠不进会话）不动。
    sess = await session_facade.read(wid, "card_001")
    assert [m["role"] for m in sess] == ["user", "assistant"]
    assert "【结论】" not in sess[1]["content"]
    assert sess[0]["content"] == "端到端折叠场景提问"

    # round 5 装配线：轮 1、2 已折叠（窗口 K=2 保轮 3、4），条数与配对完整
    wire5 = captured_wires[-1]
    roles = [m["role"] for m in wire5]
    assert roles == ["user"] + ["assistant", "tool"] * 4
    a1 = wire5[1]
    assert json.loads(a1["tool_calls"][0]["function"]["arguments"])["folded"] is True
    assert wire5[2]["content"].startswith("【结论】")
    assert wire5[2]["tool_call_id"] == a1["tool_calls"][0]["id"]
    a2 = wire5[3]
    assert json.loads(a2["tool_calls"][0]["function"]["arguments"])["folded"] is True
    assert "content=<已折叠，共 80000 字>" in wire5[4]["content"]
    # 窗口内轮 3、4 全文
    assert "【结论】" not in wire5[6]["content"]
    assert "【结论】" not in wire5[8]["content"]

    # prompt 日志：regions＋fold_ops 落盘；prior_messages 落原文全量
    import glob
    import os
    logs = sorted(glob.glob(str(sandbox.logs / "prompt_card_001_*.json")))
    assert logs
    payload = json.loads(open(logs[-1], encoding="utf-8").read())
    # [卡片区域说明 2026-10-14] 区集合随迭代：90（卡片职责）/155（卡片区域说明）已入表。
    # 本断言只钉"regions 落了哪些区"，与下方 T-02 阻塞无关。
    assert [r["order"] for r in payload["regions"]] == [0, 90, 100, 150, 155, 300, 900, 999]
    assert len(payload["regions"]) == 8
    ops = payload["fold_ops"]
    assert [o["round"] for o in ops] == [1, 2]
    assert [o["tools"] for o in ops] == [["read_file"], ["update_shadow"]]
    # [销红 2026-10-12 · 未销 · 阻塞回报] 本条按 §六.4「不得改断言迁就」保持原样，未动。
    # 修完替身签名（G.1）后本行暴露**未在册的 src 缺陷（待办 T-02）**：
    # 轮 1 的 read_file 实际被拒——工具结果是 62 字节的
    # `[tool_error code=denied] 越权：无权读 trading-workspace/big_e2e.txt`，
    # 故 original_bytes=346 < folded_bytes=770（折叠 346 字节错误串反而变大）。
    # 根因（已实证，非推断）：src/api/file_tools.py:81-87 FILE_SCOPE 的键位是
    # 底座收口前的旧表（rd_project/research_project/sub_project/topic/tool），
    # **没有 project**；而根卡 card_type=project（card_store/work_store 现态）。
    # check_file_access:157 `FILE_SCOPE.get(card_type, [])` 对 project 取空列表
    # ⇒ 前缀判定恒 False ⇒ 即便 config/permissions.json:22 给 project 授了
    # tools_file，根卡的 list/read/search/write 四件文件工具在生产上也全被拒。
    # 生产实证：work_001/card_001 perms 含 tools_file，check_file_access 仍 False。
    # 修法在 src（给 FILE_SCOPE 补 "project" 行或改键位模型），不在测试。
    assert all(o["ok"] and o["folded_bytes"] < o["original_bytes"] for o in ops)
    # 原文全量在场：影子 8 万字在 prior 的 assistant 入参里；大文件返回原文在 prior
    prior_dump = json.dumps(payload["prior_messages"], ensure_ascii=False)
    assert "影子正文" * 50 in prior_dump
    # big 内容经 JSON 转义（\n→\\n）入账，按无换行的行内容计数（T1 原文 1.2 万行＋T4 两行）
    assert prior_dump.count("行内容一二三四五六七八九十") >= 12000
