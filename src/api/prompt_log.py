# -*- coding: utf-8 -*-
"""prompt 日志落盘。文件名 logs/prompt_{card_id}_{YYYYMMDDHHMMSS}.json，
同秒冲突加 _2/_3... 后缀。字段规格固定，是验收判定依据。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import model_config

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
    pair_id: Optional[str] = None,
    regions: Optional[list[dict[str, Any]]] = None,
    fold_ops: Optional[list[dict[str, Any]]] = None,
    work_id: str = "",
) -> Optional[Path]:
    """写一条 prompt 日志，返回文件路径；失败只记日志返回 None，不中断主流程。

    Phase 7：新增 tool_events 字段（工具循环记录）。
    [T10-5-2/5-3] 新增 pair_id 字段（会话配对标识）；落盘时记一行（含文件名与标识）。
    文件名形态不变（含同秒冲突的 _{n} 后缀机制，:36-40 原样）。
    [T11-T4/T7] 新增 regions／fold_ops 两个可选键（纯加法）：regions＝每区
    order/名称/通道/席位/字节数/指纹/条目数（只存指针与指纹、不复制正文）；
    fold_ops＝每轮折叠原字节/折后字节/结果。两键只在非空时写入——缺省不传
    （或传空）时日志与既往逐字同构，既有键一个不改、不改名、不删。
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
            # [日志字段修复 2026-10-20] 原来写的是 `default_model()`——**不带 work/card**，
            # 于是永远记全局默认（minimax），把三层分层、卡/项目覆盖全盖掉了。
            # 实测 5700 自动卡：实际 model_used=k3 / deepseek-v4.1-flash-1017，
            # 但 model_requested 一直写 minimax-m31-flash-preview，看起来像"没按配置走"。
            "model_requested": model_config.default_model(
                work_id=work_id or None, card_id=card_id or None),
            "model_used": model_used,
            "system_prompt": system_prompt,
            "prior_messages": prior_messages,
            "user_prompt": user_prompt,
            "response": response,
            "error": error,
            "tool_events": tool_events or [],
            "pair_id": pair_id,
        }
        # [T11] 新键为空时不写入（约束 13）：日志与既往逐字同构
        if regions:
            payload["regions"] = regions
        if fold_ops:
            payload["fold_ops"] = fold_ops
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        # [T10-5] 落盘打点：含文件名与标识（既有 logger 通道）
        logger.info("prompt 日志落盘 file=%s pair_id=%s", path.name, pair_id)
        return path
    except Exception as exc:
        logger.warning("prompt 日志写入失败（不中断）: %s", exc)
        return None


# ------------------------------------------------------------
# [THINK-ECO-permgmt T7 · 2026-10-09] 权限判定打点
#
# 落 logs/prompt_perm_audit.jsonl：一行一判定、机械字段、追加写；
# **无闸**——无轮转/无长度上限/无采样（R-C：打点是留痕非闸）。
# 落盘失败只告警，绝不阻断权限判定本身。
# ------------------------------------------------------------

PERM_LOG_FILENAME = "prompt_perm_audit.jsonl"


def append_perm_log(record: dict[str, Any]) -> None:
    """权限判定留痕（任务书 T7/交办单）：check、check_with_scope、
    build_tool_schemas、check_file_access 每次判定各记一条机械字段记录。

    LOGS_DIR 在调用时读取（与 write_prompt_log 同一沙箱手法，测试重定向生效）。
    """
    entry = {"ts": datetime.now().isoformat(timespec="milliseconds"), **record}
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOGS_DIR / PERM_LOG_FILENAME, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        logger.warning("[perm_log] 权限打点落盘失败（不中断）: %s", exc)
