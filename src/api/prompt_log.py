# -*- coding: utf-8 -*-
"""prompt 日志落盘。文件名 logs/prompt_{card_id}_{YYYYMMDDHHMMSS}.json，
同秒冲突加 _2/_3... 后缀。字段规格固定，是验收判定依据。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("api.prompt_log")

BASE_DIR = Path(__file__).resolve().parents[2]
LOGS_DIR = BASE_DIR / "logs"


def write_prompt_log(
    card_id: str,
    system_prompt: str,
    prior_messages: list[dict[str, Any]],
    user_prompt: str,
    model_used: Optional[str],
    response: Optional[str],
    error: Optional[str],
    tool_events: Optional[list[dict[str, Any]]] = None,
) -> Optional[Path]:
    """写一条 prompt 日志，返回文件路径；失败只记日志返回 None，不中断主流程。

    Phase 7：新增 tool_events 字段（工具循环记录）。
    """
    try:
        LOGS_DIR.mkdir(exist_ok=True)
        now = datetime.now().astimezone()
        stamp = now.strftime("%Y%m%d%H%M%S")
        path = LOGS_DIR / f"prompt_{card_id}_{stamp}.json"
        n = 2
        while path.exists():
            path = LOGS_DIR / f"prompt_{card_id}_{stamp}_{n}.json"
            n += 1
        payload = {
            "card_id": card_id,
            "timestamp": now.isoformat(),
            "model_requested": "main",
            "model_used": model_used,
            "system_prompt": system_prompt,
            "prior_messages": prior_messages,
            "user_prompt": user_prompt,
            "response": response,
            "error": error,
            "tool_events": tool_events or [],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
    except Exception as exc:
        logger.warning("prompt 日志写入失败（不中断）: %s", exc)
        return None
