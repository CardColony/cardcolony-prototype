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
from pathlib import Path
from typing import Any, Iterator

from .hash_utils import parse_front_matter
from . import session_facade, session_store, work_store

logger = logging.getLogger("storage.activity")

BASE_DIR = Path(__file__).resolve().parents[2]
WORKS_DIR = BASE_DIR / "works"


def _max_iso(a: str, b: str) -> str:
    """两个 ISO8601 字符串按字典序取 max；空串不参与（视为最旧）。"""
    if not a:
        return b or ""
    if not b:
        return a
    return a if a >= b else b


def iter_card_metas(work_id: str) -> Iterator[dict[str, Any]]:
    """逐张取库内正卡的 meta（附 card_id）。

    [卡片存储迁移 SQLite] 数据源从**卡片 md 目录**改为库内 ``cards`` 表
    （一次 SELECT，比逐文件解析更快）。**库才是真身**（canvas.json 只是
    可重建缓存且不含 updated_at），/api/nav 每次请求都从这里现读。
    """
    from . import card_db

    def _fn(conn: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in conn.execute("SELECT * FROM cards ORDER BY card_id"):
            meta = card_db.row_to_meta(row)
            meta["card_id"] = row["card_id"]
            out.append(meta)
        return out

    for meta in card_db.read_sync(work_id, _fn, []) or []:
        yield meta


def card_last_active(work_id: str, card_id: str, updated_at: str = "") -> str:
    """卡级活跃 = max(会话末条 timestamp, 卡 updated_at)；均无返回 ""。

    会话末条经门面 scan_tail 尾部反向扫描取得（[A6a/T073] 统一读入口切换；
    V1 路径委托 session_store.last_ts 语义不变，R-2 禁令随统一入口作废）。
    """
    ts = session_facade.scan_tail(work_id, card_id)["ts"]
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


def compute_card_activity(work_id: str) -> dict[str, dict[str, str]]:
    """每卡活跃同源一次现算（主界面优化 E1）：own＝卡级活跃，branch＝分支活跃。

    own 与 card_last_active 逐卡同口径（max(会话末条, updated_at)）；branch＝
    该卡及其全部子孙 own 的 max——与卡级活跃同源遍历合并计算（源方案 E1），
    避免同一批卡文件与会话尾扫做两遍。孤儿卡（parent 不在树内）自成一支、
    不向上归并；parent 成环时环防护按自身收口、不无限递归。
    请求时现算、绝不入盘（RD-06 同口径）。返回 {card_id: {"own":…, "branch":…}}。
    """
    metas: dict[str, dict[str, Any]] = {}
    for m in iter_card_metas(work_id):
        cid = m.get("card_id", "")
        if cid:
            metas[cid] = m
    own: dict[str, str] = {}
    children: dict[str, list[str]] = {}
    for cid, m in metas.items():
        own[cid] = card_last_active(work_id, cid, m.get("updated_at", ""))
        parent = m.get("parent", "")
        if parent in metas:
            children.setdefault(parent, []).append(cid)
    branch: dict[str, str] = {}

    def _resolve(cid: str, visiting: set[str]) -> str:
        if cid in branch:
            return branch[cid]
        if cid in visiting:   # 环防护：成环回边按自身活跃收口，不下探
            return own.get(cid, "")
        visiting.add(cid)
        best = own.get(cid, "")
        for ch in children.get(cid, ()):
            best = _max_iso(best, _resolve(ch, visiting))
        visiting.discard(cid)
        branch[cid] = best
        return best

    for cid in metas:
        _resolve(cid, set())
    return {cid: {"own": own[cid], "branch": branch.get(cid, own[cid])} for cid in metas}
