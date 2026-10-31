# -*- coding: utf-8 -*-
"""T11 · 轮槽离线套件（折叠已移除 2026-10-31 · 用户裁定）。

[2026-10-31] 工具轮折叠（【结论】/【依据】两件套）按用户裁定**整体移除**：
它把工具调用压成文本标记，误导模型在正文输出假工具调用（原版 dsh
scrubMarkerLines 治的正是这个病）。本套件现在钉两件事：
① ``wire_messages()`` 恒返回原列表（零加工、同对象）；
② 折叠机器（开关/压缩/依据/窗口）不复存在——防回魂。
端到端用例仍走真实 gateway 链路（MOCK_LLM=1）驱动 routes._run_message 五轮
工具循环，验证轮槽装配与落盘形态不变（工具结果只在当前轮在场）。
"""
from __future__ import annotations

import json

import pytest

from src.api import card_tools, prompt_log, routes, tool_fold
from src.api.tool_fold import ToolTurnSlot
from src.storage import session_facade, session_store


# ---------------------------------------------------------------------------
# 折叠机器已拆除（防回魂）
# ---------------------------------------------------------------------------


def test_fold_machinery_removed():
    """折叠函数/开关/窗口常量一律不复存在——谁把折叠加回来，这里先红。"""
    for name in ("fold_enabled", "_compress_round", "_fold_round", "_evidence_for",
                 "_COMPRESS_SYSTEM", "KEEP_WINDOW", "FOLD_ENV"):
        assert not hasattr(tool_fold, name), f"折叠机器残骸: {name}"
    assert "TOOL_FOLD_ENABLED" not in open(tool_fold.__file__, encoding="utf-8").read(), \
        "TOOL_FOLD_ENABLED 开关不应再出现"


# ---------------------------------------------------------------------------
# T10：只读白名单（落工具定义处；与注册表现状求交集，不写死件数）
# ---------------------------------------------------------------------------


def test_t10_readonly_whitelist():
    # [2026-10-31] 名单随注册表现状走：read_inbox 已随自动模式收口退场、
    # read_archive 入列；后续 memory_expand（F19）入列时在此同步。
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
        # [更新现状按钮改造 2026-10-16] 新增 propose_status（「更新现状」专用回合里
        # 只提议、不落盘地交出「新现状」；落盘走用户确认后的 /commit，不经过本工具）
        "propose_status",
        # [goal 域 2026-10-20] get_goal 只读
        "get_goal",
        # [基础工具找回] glob_files 只读
        "glob_files",
        # [角色机制 5710] list_roles→list_functions（职能实体化改名，仍为只读清单）
        "list_functions",
        # [归档 2026-10-25] read_archive 读归档记录，只读
        "read_archive",
        # [F19 2026-10-31] memory_expand 展开工具（只读下探）
        "memory_expand",
    } & set(card_tools.TOOL_REGISTRY)

    assert card_tools.READONLY_TOOLS == frozenset(expected)
    for w in ("edit_card", "delete_card", "edit_shadow",
              "link_cards", "unlink_cards", "write_file"):
        assert w not in card_tools.READONLY_TOOLS


# ---------------------------------------------------------------------------
# 轮槽：恒零加工（wire_messages 恒返回原列表对象）
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


def _sync_wire(slot):
    import asyncio
    return asyncio.run(slot.wire_messages())


def test_wire_is_same_list_object_always(monkeypatch):
    """wire_messages 恒返回**原列表对象**（零加工——连旧「折叠关」都逐字同）。"""
    slot, messages = _build_slot_with_four_rounds()
    wire = _sync_wire(slot)
    assert wire is messages
    roles = [m["role"] for m in wire]
    assert roles == ["user", "user", "assistant", "tool"] * 1 + ["assistant", "tool"] * 3 + ["assistant"]
    # 内容逐字（旧轮工具结果原样在场——本回合内都在；跨回合才被 enters_context 排除）
    assert wire[3]["content"] == json.dumps(
        {"card_id": "card_001", "body": "第 1 轮返回正文" * 200}, ensure_ascii=False)
    # 多次装配结果恒同
    assert _sync_wire(slot) is messages


def test_wire_never_produces_marker_text(monkeypatch):
    """任何装配形态下都不出现【结论】/【依据】替身（假工具调用形态防回魂）。"""
    slot, messages = _build_slot_with_four_rounds()
    wire = _sync_wire(slot)
    assert all("【结论】" not in str(m.get("content") or "") for m in wire)
    assert all("【依据】" not in str(m.get("content") or "") for m in wire)
    # 原列表永不改写
    assert messages[3]["content"] == json.dumps(
        {"card_id": "card_001", "body": "第 1 轮返回正文" * 200}, ensure_ascii=False)
    # 工具调用入参保持原 JSON（不再换成 folded 标记）
    a1 = wire[2]
    assert json.loads(a1["tool_calls"][0]["function"]["arguments"]) == {"card_id": "card_001"}
    # fold_ops 恒空（prompt_log 空则不同构 ⇒ 键不落盘）
    assert slot.fold_ops == []


# ---------------------------------------------------------------------------
# 端到端：routes._run_message 五轮工具循环（MOCK_LLM=1，真实工具执行）
# ---------------------------------------------------------------------------


async def test_e2e_tool_loop_with_slot(work, sandbox, monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    wid = work["work_id"]
    # [执行权集中 2026-10-22 · D04] 本测试验「工具轮槽」机制，需要卡能**读大文件**。
    # project 卡（管理卡）现在没有 tools_file（执行权只在 coder 手上）。
    # [2026-10-31 修夹具] 权限在**建卡期固化**（裁定 4），update_card 不吃 perms、
    # 能力底座 2026-10-30 后库里也没有 perms 列可改——改前夹具两次尝试
    # （update_card / 直改列）都静默无效 ⇒ read_file 恒 denied、
    # 「原文全量在场」断言必红。现在改打**权限判定缝**（policy.has_perm 测试替身）：
    # 只给本测试放行 tools_file/tools_web，不代表生产配置。
    from src.permissions import policy as _policy
    monkeypatch.setattr(_policy, "has_perm",
                        lambda meta, switch: str(switch) in ("tools_file", "tools_web"))

    big = "行内容一二三四五六七八九十\n" * 12000  # ≈330KB 文本
    big_path = sandbox.base / "trading-workspace" / "big_e2e.txt"
    big_path.parent.mkdir(parents=True, exist_ok=True)
    big_path.write_text(big, encoding="utf-8")

    calls = {"n": 0}
    captured_wires: list[list[dict]] = []

    def _tc(name, args):
        return {"name": name, "arguments": args}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
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

    result = await routes._run_message(wid, "card_001", "端到端轮槽场景提问")
    assert "error" not in result
    assert result["reply"].strip() == "五轮循环收束回复。"
    assert calls["n"] == 5

    # 会话文件：只有 user＋assistant 两条，轮槽加工替身不进会话（约束 9）
    sess_all = await session_facade.read(wid, "card_001")
    sess = [m for m in sess_all if m.get("role") != "tool"
            and not (m.get("role") == "assistant" and m.get("tool_calls"))]
    assert [m["role"] for m in sess] == ["user", "assistant"]
    assert sess[0]["content"] == "端到端轮槽场景提问"
    assert all("【结论】" not in m.get("content", "") for m in sess_all), "折叠替身不进会话"

    # round 5 装配线：零折叠 ⇒ 五轮工具调用/结果全部原样在场、配对完整
    wire5 = captured_wires[-1]
    roles = [m["role"] for m in wire5]
    assert roles == ["user"] + ["assistant", "tool"] * 4
    assert json.dumps(wire5, ensure_ascii=False).count("【结论】") == 0
    # 轮 1 的调用入参原样（不再是 folded 标记）
    a1 = wire5[1]
    assert json.loads(a1["tool_calls"][0]["function"]["arguments"]) == \
        {"path": "trading-workspace/big_e2e.txt"}
    assert wire5[2]["tool_call_id"] == a1["tool_calls"][0]["id"]

    # prompt 日志：regions 落盘；fold_ops 键因恒空而**不出现**（空则不同构）
    import glob
    logs = sorted(glob.glob(str(sandbox.logs / "prompt_card_001_*.json")))
    assert logs
    payload = json.loads(open(logs[-1], encoding="utf-8").read())
    assert "fold_ops" not in payload
    orders = [r["order"] for r in payload["regions"]]
    assert 0 in orders and 300 in orders and 900 in orders and 999 in orders
    # 原文全量在场：影子 8 万字在 prior 的 assistant 入参里；大文件返回原文在 prior
    prior_dump = json.dumps(payload["prior_messages"], ensure_ascii=False)
    assert "影子正文" * 50 in prior_dump
    # big 内容经 JSON 转义（\n→\\n）入账，按无换行的行内容计数（T1 原文 1.2 万行＋T4 两行）
    assert prior_dump.count("行内容一二三四五六七八九十") >= 12000
