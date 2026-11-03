# -*- coding: utf-8 -*-
"""浏览器工具（2026-11-02）：模型侧单件 ``browser`` 工具，action+input 紧凑形态。

机制移植自 MiniMax Code（MIT）的 in-agent browser：一个工具、会话级
隔离、语义树＋快照域 ref、显式效果校验。实现见 src/infra/browser/
（纯标准库核心，与 wb_exec 执行体用的 CLI 通道同源）。

挂载：能力码 ``browser``（config/capabilities.json），权限类别
tools_web。卡面上 abilities 挂了才下发；执行层 perm_gate 同判据。

会话归属：一卡一会话（b_<card_id>），profile 落 works/<work_id>/
browser_sessions/——跨回合保持登录态与页上下文，卡间隔离。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("api.browser_tool")

#: schema 描述＝紧凑操作手册（照 minimax compact 形态：工具描述即规则）。
_TOOL_DESCRIPTION = (
    "开一个真实浏览器（headless Chrome，本会话级隔离）看页面、操作页面。"
    "循环：navigate 打开 → inspect 拿语义地图（每个可操作元素带 ref，"
    "形如 e2.7）→ 用 ref 调动作 → inspect/query 验证效果。"
    "铁律：①ref 只在当前快照代次有效，导航后必须重新 inspect；"
    "②click 成功只证明输入已派发（effect.verificationRequired=true 时"
    "要再 inspect/query 验证预期效果）；③预期导航的点击用 "
    "click_and_wait_for_navigation；④一次一个动作；⑤fill 清原值再写、"
    "type 追加输入；⑥query 拿页面文本/DOM/控制台/网络诊断；"
    "⑦screenshot 落盘成证据文件；⑧完事 close。"
)


def build_browser_tool_schemas() -> list[dict[str, Any]]:
    from ..infra.browser import session as _bs
    return [{
        "name": "browser",
        "description": _TOOL_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_bs.ACTIONS),
                    "description": "动作名",
                },
                "input": {
                    "type": "object",
                    "description": "该动作的参数（ref/url/text/kind/ms…）",
                    "additionalProperties": True,
                },
            },
            "required": ["action"],
        },
    }]


def _sessions_root(work_id: str) -> str:
    from ..storage import work_store
    try:
        wdir = work_store.work_dir(work_id)
        if wdir:
            return str(Path(wdir) / "browser_sessions")
    except Exception:  # noqa: BLE001
        pass
    return f"works/{work_id}/browser_sessions"


async def h_browser(work_id: str, current_id: str,
                    arguments: dict[str, Any],
                    cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """``browser`` 工具 handler：分发到共享核心（to_thread 隔阻塞）。"""
    from ..infra.browser import session as _bs
    action = str((arguments or {}).get("action") or "")
    inp = (arguments or {}).get("input") or {}
    if not isinstance(inp, dict):
        return {"ok": False,
                "error": {"code": "INVALID_INPUT",
                          "message": "input 必须是对象"}}
    sess = _bs.BrowserSession(_sessions_root(work_id), f"b_{current_id}")
    try:
        data = await asyncio.to_thread(sess.act, action, inp)
        return {"ok": True, "data": data}
    except _bs.BrowserError as exc:
        return {"ok": False,
                "error": {"code": exc.code, "message": exc.message}}
    except Exception as exc:  # noqa: BLE001
        logger.error("browser 工具异常 card=%s action=%s: %s",
                     current_id, action, exc)
        return {"ok": False,
                "error": {"code": "INTERNAL",
                          "message": f"{type(exc).__name__}: {exc}"}}
