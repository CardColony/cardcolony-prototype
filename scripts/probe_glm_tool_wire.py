# -*- coding: utf-8 -*-
"""一次性探针：抓 GLM Coding 端点工具调用的**原始 SSE 帧**。

只发一个最小请求（历史实测 ~66 token），把 delta 里所有字段原样落盘，
用于确认「finish_reason=tool_calls 但 tool_calls=[]」到底丢在哪一层。

用法：python scripts/probe_glm_tool_wire.py
输出：logs/glm_wire_raw.jsonl
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(BASE, ".env"))
except Exception:  # noqa: BLE001
    pass

logging.disable(logging.CRITICAL)

import src.model_gateway.llm_openai_compat.adapter as ad  # noqa: E402
from src.api import model_config, model_service  # noqa: E402
from src.storage import memory_store as m  # noqa: E402

OUT = os.path.join(BASE, "logs", "glm_wire_raw.jsonl")


async def main() -> int:
    orig = ad.parse_sse
    n = [0]

    async def tap(src, *a, **kw):
        async for payload in orig(src, *a, **kw):
            n[0] += 1
            try:
                obj = json.loads(payload)
            except Exception:  # noqa: BLE001
                obj = {"_nonjson": payload[:500]}
            with open(OUT, "a", encoding="utf-8") as f:
                f.write(json.dumps({"seq": n[0], "obj": obj}, ensure_ascii=False) + "\n")
            yield payload

    ad.parse_sse = tap
    model = model_config.pick_one("condense")
    print(f"模型 {model}  输出 {OUT}", flush=True)
    try:
        r, _used = await model_service.call_main(
            "你是工具调用器。只调用 apply_folds，参数 merges 为空数组。不要输出任何解释文字。",
            "执行。",
            prior_messages=[], tools=[m.APPLY_FOLDS_TOOL], model_name=model)
        print("结果字段:", {k: (len(v) if isinstance(v, (list, str)) else v)
                            for k, v in r.items() if k != "reasoning_content"}, flush=True)
        print("tool_calls:", json.dumps(r.get("tool_calls"), ensure_ascii=False)[:600], flush=True)
        print("content 前 300:", str(r.get("content") or "")[:300], flush=True)
    except Exception as e:  # noqa: BLE001
        print("异常:", type(e).__name__, str(e)[:300], flush=True)
    print(f"原始帧 {n[0]} 条 → {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
