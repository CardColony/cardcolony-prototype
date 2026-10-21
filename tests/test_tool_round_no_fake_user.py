# -*- coding: utf-8 -*-
"""工具轮不再编造「用户发言」（2026-10-21）。

背景：`_build_options` 原来**无条件**拼一条 `{"role":"user","content":user_prompt}`，
工具轮没有真实用户输入 ⇒ 调用方塞占位符「工具结果已返回。」填坑 ⇒ 模型把它当成
**用户说的话**（实测每轮思考都在念它）。dsh / WorkBuddy 均无此占位。

判据：
U1 工具轮（prior 末尾是 tool、user 位留空）⇒ 请求里**没有**尾随 user 消息，末条是 tool
U2 有真实用户输入时行为**逐字不变**（user 消息照拼、位置仍在 prior 之后）
U3 占位符文案在源码里已不存在（防止回归塞回来）
"""
from __future__ import annotations

import pathlib

from src.api import model_service
from src.model_gateway.llm_openai_compat.serialize import serialize_request

PRIOR_TOOL = [
    {"role": "user", "content": "读一下那张卡"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"id": "c1", "type": "function",
                     "function": {"name": "read_card", "arguments": "{}"}}]},
    {"role": "tool", "content": '{"ok":true,"data":"卡片正文"}', "tool_call_id": "c1"},
]


def _wire(user_prompt: str) -> list[dict]:
    gw = model_service.get_gateway()
    name = list(gw.config["models"])[0]
    opts = gw._build_options(name, "SYS", user_prompt, 0.3, None, None,
                             list(PRIOR_TOOL), None, None, stream=False)
    return serialize_request(opts)["messages"]


# ---------------------------------------------------------------- U1

def test_tool_round_has_no_trailing_user():
    msgs = _wire("")
    roles = [m["role"] for m in msgs]
    print("线序:", roles)
    assert roles == ["system", "user", "assistant", "tool"], roles
    assert msgs[-1]["role"] == "tool", "末尾必须是工具结果，模型据此续写"
    assert msgs[-1]["tool_call_id"] == "c1"


def test_tool_round_no_user_message_at_all_after_tool():
    msgs = _wire("")
    after_tool = [m for m in msgs[[i for i, m in enumerate(msgs)
                                  if m["role"] == "tool"][-1] + 1:]]
    assert after_tool == [], f"工具结果之后不得再有任何消息，实际: {after_tool}"


# ---------------------------------------------------------------- U2

def test_real_user_input_unchanged():
    msgs = _wire("继续")
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "user"]
    assert msgs[-1] == {"role": "user", "content": "继续"}


def test_none_user_prompt_also_skipped():
    msgs = _wire(None)  # type: ignore[arg-type]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"]


# ---------------------------------------------------------------- U3

def test_placeholder_text_gone_from_source():
    """占位符不得再作为**代码/字符串**出现（注释里说明来历允许）。"""
    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    bad: list[str] = []
    for p in root.rglob("*.py"):
        for no, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            code = line.split("#", 1)[0]
            if "工具结果已返回" in code:
                bad.append(f"{p.name}:{no}: {line.strip()[:80]}")
    assert bad == [], f"占位符又回到代码里了: {bad}"
