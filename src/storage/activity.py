# -*- coding: utf-8 -*-
"""活跃度派生（T17-B1）：卡级／项目级「最后活跃」的唯一定义与计算入口。

口径（方案 §4.1；RD-06：请求时现算、绝不入盘）：
    卡级 last_active_at = max(该卡会话文件末条消息 timestamp, 卡前置字段 updated_at)
                          两源皆无 → ""
    项目级 last_active_at = max(该项目所有卡的卡级活跃, work.created_at)

会话末条只聊天不改卡时也会前进（updated_at 兜底只覆盖「从未聊过但被编辑过」），
这是用户第 4/6 条「按最近活跃排序」能排对的关键。所有值都是 _now_iso() 同源
产生的 ISO8601 带时区字符串，直接按字典序比较；空串视为最旧、不参与 max。

被否备选（方案 §4.1/§4.4 已裁）：文件 mtime（备份恢复会改写）、canvas.json
缓存（聊天不触发重建，必然滞后）。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterator

from .hash_utils import parse_front_matter
from . import session_store, work_store

logger = logging.getLogger("storage.activity")

BASE_DIR = Path(__file__).resolve().parents[2]
WORKS_DIR = BASE_DIR / "works"

_CARD_FILE_RE = re.compile(r"^card_(\d{3,})\.md$")


def _max_iso(a: str, b: str) -> str:
    """两个 ISO8601 字符串按字典序取 max；空串不参与（视为最旧）。"""
    if not a:
        return b or ""
    if not b:
        return a
    return a if a >= b else b


def iter_card_metas(work_id: str) -> Iterator[dict[str, Any]]:
    """逐张解析 works/{work_id}/cards/card_*.md 的前置 meta（附 card_id）。

    卡文件是真身（canvas.json 只是可重建缓存且不含 updated_at），
    /api/nav 每次请求都从这里现读；解析失败跳过并记 warning
    （与 canvas_store.rebuild 同口径，绝不中断导航）。
    """
    cd = WORKS_DIR / work_id / "cards"
    if not cd.exists():
        return
    for path in sorted(cd.glob("card_*.md")):
        if not _CARD_FILE_RE.match(path.name):
            continue
        try:
            meta, _body = parse_front_matter(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("activity: 解析卡片失败，跳过 %s: %s", path.name, exc)
            continue
        meta["card_id"] = path.stem
        yield meta


def card_last_active(work_id: str, card_id: str, updated_at: str = "") -> str:
    """卡级活跃 = max(会话末条 timestamp, 卡 updated_at)；均无返回 ""。

    会话末条经 session_store.last_ts() 尾部反向扫描取得（禁全量解析，R-2）。
    """
    ts = session_store.last_ts(work_id, card_id)
    return _max_iso(ts, updated_at or "")


def work_last_active(work_id: str) -> str:
    """项目级活跃 = max(所有卡的卡级活跃, work.created_at)；空项目兜底 created_at。"""
    best = ""
    for meta in iter_card_metas(work_id):
        best = _max_iso(
            best, card_last_active(work_id, meta.get("card_id", ""), meta.get("updated_at", ""))
        )
    data = work_store.read_work(work_id) or {}
    return _max_iso(best, data.get("created_at", ""))
