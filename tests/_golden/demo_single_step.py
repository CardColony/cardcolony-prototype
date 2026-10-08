# -*- coding: utf-8 -*-
"""T-0009 §3.3 结构验收 · 演示「循环可单步推进」（只读，不改 src）。

证据价值：证明 step() 真的能**一次只走一个状态点**（而不是把整个循环
藏在一个函数里）。做法＝手工搭 state，只调一次 step，打印 step 前后
可观测量的delta，再自己决定要不要调第二次。

不复现完整请求（那由金样本回放与 pytest 负责）；此处只验结构。
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from src.api import message_runner as mr  # noqa: E402


def snapshot(state: mr.RunState) -> dict:
    return {
        "rounds": state.rounds,
        "model_calls": state.model_calls,
        "tool_events": len(state.tool_events),
        "analysis_parts": len(state.analysis_parts),
        "tool_full_texts": len(state.tool_full_texts),
        "prior_len": len(state.prior_for_gateway),
        "seats_done": state.seats_done,
        "final_reply_set": state.final_reply is not None,
    }


async def main() -> int:
    print("=" * 68)
    print("[结构验收] 签名证据")
    print("=" * 68)
    print(f"step:{inspect.signature(mr.step)}")
    print(f"run_seats          : {inspect.signature(mr.run_seats)}")
    print(f"check_control      : {inspect.signature(mr.check_control)}")
    print(f"RunState 字段数: {len(mr.RunState.__dataclass_fields__)}")
    src = inspect.getsource(mr.check_control)
    # 用 AST 取真实可执行体（剥docstring 后应只剩一条 Return）
    import ast
    tree = ast.parse(textwrap.dedent(src))
    fn = tree.body[0]
    exec_stmts = [n for n in fn.body if not isinstance(n, ast.Expr)]
    print(f"check_control 可执行语句 = {[type(n).__name__ for n in exec_stmts]}"
          f"  （须仅 ['Return']，函数体为空实现）")
    body = ["return None"] if (
        len(exec_stmts) == 1 and isinstance(exec_stmts[0], ast.Return)
    ) else ["<非空实现>"]

    print()
    print("=" * 68)
    print("[结构验收] 单步推进演示（手工 state，只调一次 step）")
    print("=" * 68)

    calls: list[dict] = []

    async def fake_call_main(system_prompt, user_prompt, prior_messages, tools=None):
        """第一轮发一个工具调用（复刻金样本 s2 的第一轮形态）。"""
        calls.append({"user_prompt": user_prompt, "prior_len": len(prior_messages)})
        return (
            {"content": "正在查证…",
             "tool_calls": [{"name": "read_card", "arguments": {"card_id": "card_001"}}]},
            "fake-main",
        )

    from src.api import model_service
    orig_call_main = model_service.call_main
    model_service.call_main = fake_call_main

    try:
        state = mr.RunState(work_id="work_001", card_id="card_001",
                            content="查看一下卡片", pair_id="0" * 32)
        state.user_prompt = state.content
        state.mode = "fastlane"
        # 手工装配最小可跑 state（真实装配在 build_context 内，涉 IO 不在此演示）
        state.prior = []
        state.prior_for_gateway = []
        state.history_len = 0
        from src.api import tool_fold
        state.slot = tool_fold.ToolTurnSlot(state.prior_for_gateway)
        state.tools = [{"name": "read_card"}]
        state.system_prompt = "SP"

        async def fake_execute(work_id, card_id, name, args, cards_meta):
            return {"card_id": "card_001", "title": "根卡", "parent": "",
                    "relations": [], "body": "根卡正文"}

        from src.api import card_tools
        orig_exec = card_tools.execute_tool
        card_tools.execute_tool = fake_execute

        before = snapshot(state)
        print(f"step 前: {before}")

        outcome = await mr.step(state)

        after = snapshot(state)
        print(f"step 后: {after}")
        print()
        print(f"outcome = {outcome}")
        print()
        print("=== 单步 delta（应恰好推进一轮：rounds +1、model_calls +1、"
              "tool_events +1、analysis +1、prior 增长）===")
        for k in before:
            b, a = before[k], after[k]
            flag = "" if b == a else f"  ← 变化（{b} → {a}）"
            print(f"  {k:20s} {b!s:>6} → {a!s:<6}{flag}")

        print()
        print(f"model_calls 调用次数 = {len(calls)}（单步只应 1 次，"
              f"证明没有把整个循环跑完）")

        card_tools.execute_tool = orig_exec
    finally:
        model_service.call_main = orig_call_main

    print()
    print("=" * 68)
    ok = (outcome.kind == "continue"
          and after["rounds"] == 1
          and after["model_calls"] == 1
          and len(calls) == 1
          and body == ["return None"])
    print(f"单步推进判定 = {'通过' if ok else '不通过'}（outcome=continue、"
          f"恰好一轮、只调 1 次模型、check_control 空实现）")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))