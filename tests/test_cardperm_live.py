# -*- coding: utf-8 -*-
"""[卡片权限改造 2026-10-07] V7 真模型实弹（禁 mock）。

默认跳过（离线回归网不烧 token、不依赖外网）；显式设 CARDPERM_LIVE=1
后经 model_service 真网关走两轮真实模型调用：

  第一轮：模型发起 read_card 读旁支卡 → 真实 execute_tool 执行 → 成功；
  第二轮：模型发起 edit_card 越权写旁支卡 → 真实 execute_tool 执行 → 拒绝。

模型只负责真实发起调用（与生产同一条 function calling 通道），工具执行
走真实存储（conftest 沙箱落盘 tmp_path）。
"""
from __future__ import annotations

import json
import os

import pytest

from src.api import card_tools
from src.storage import card_store

pytestmark = pytest.mark.skipif(
    os.environ.get("CARDPERM_LIVE") != "1",
    reason="真模型实弹须显式 CARDPERM_LIVE=1",
)


async def _mk_child(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "模型生成", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


def _extract_call(result: dict, name: str) -> dict | None:
    """兼容两种形状：OpenAI 裸形（function.name/arguments 串）与本网关归一化形
    （name/arguments 已是 dict）。统一返回 {name, arguments: dict}。"""
    for tc in result.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw_name, raw_args = fn.get("name"), fn.get("arguments")
        if raw_name is None:
            raw_name, raw_args = tc.get("name"), tc.get("arguments")
        if raw_name != name:
            continue
        if isinstance(raw_args, str):
            raw_args = json.loads(raw_args or "{}")
        return {"name": name, "arguments": raw_args or {}}
    return None


async def test_v7_live_model_read_open_and_write_denied(work):
    from pathlib import Path

    from dotenv import load_dotenv

    from src.api import model_service

    # 与生产进程对齐：main.py 启动时 load_dotenv 供钥匙；model_service 的
    # get_gateway 直读 models.yaml 不走 load_config，测试进程须自装 .env。
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)
    os.environ.pop("MOCK_LLM", None)   # 实弹保真：确保非 Mock 网关
    model_service._gateway = None

    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲卡", root)
    b = await _mk_child(wid, "乙卡", root)
    cards = card_tools._collect_card_meta(wid)
    tools = card_tools.build_tool_schemas()

    # 铺垫：乙卡自己的对话写入远端数据（真实落盘）
    secret = "远端数据标记 XYZ-42"
    r = await card_tools.execute_tool(
        wid, b, "edit_card", {"card_id": b, "content": f"乙卡正文：{secret}"}, cards)
    assert r.get("ok") is True

    sys_p = "你是工具链路测试执行者。严格按指令调用工具，不要自行发挥，不要额外调用。"

    # 第一轮：真模型发起 read_card 读旁支卡——改后应成功（改前旧判据必拒）
    u1 = f"当前在甲卡（{a}）对话。请调用 read_card 读取 {b}（乙卡）的正文，只调用这一个工具。"
    result1, model_used = await model_service.call_main(sys_p, u1, tools=tools)
    fn = _extract_call(result1, "read_card")
    assert fn is not None, f"模型未发起 read_card：model={model_used} result={result1}"
    args1 = fn["arguments"]   # _extract_call 已归一化为 dict
    out1 = await card_tools.execute_tool(wid, a, "read_card", args1, cards)
    assert "error" not in out1, f"read_card 旁支被拒（应为放行）: {out1}"
    assert out1["card_id"] == b and secret in out1["body"]

    # 第二轮：真模型发起 edit_card 越权写旁支卡——应被拒且不落盘
    u2 = f"当前在甲卡（{a}）对话。请调用 edit_card 把 {b}（乙卡）的正文改为「越权测试」，只调用这一个工具。"
    result2, model_used2 = await model_service.call_main(sys_p, u2, tools=tools)
    fn2 = _extract_call(result2, "edit_card")
    assert fn2 is not None, f"模型未发起 edit_card：model={model_used2} result={result2}"
    args2 = fn2["arguments"]   # _extract_call 已归一化为 dict
    assert args2.get("card_id") == b, f"模型改写目标漂移: {args2}"
    out2 = await card_tools.execute_tool(wid, a, "edit_card", args2, cards)
    assert "越权" in out2.get("error", ""), f"越权 edit 未被拒: {out2}"
    final = (await card_store.read_card(wid, b))["body"]["status_block"]
    assert "越权测试" not in final   # 确实没落盘
