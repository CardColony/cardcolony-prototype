# -*- coding: utf-8 -*-
"""canvas.json 缓存维护。canvas.json 是可重建缓存，卡片文件才是真身。

Phase 7：每个 work 各有一份 canvas.json，路径 = works/{work_id}/canvas.json。
[画布重构 2026-10-15] canvas.json **不再持有 positions**（也不再有 layout.json）：
卡片坐标是**卡片自身属性**，直接住在 cards 表的 ``pos_x``/``pos_y``/``pinned``
三列上（见 ``card_db.POSITION_COLUMNS``），由 ``load_positions`` /
``merge_positions`` 读写。于是 canvas.json 回到「纯派生缓存」这一件事：
    {
      "project_name": "...",
      "created_at": "...",
      "cards": [...],
      "relations": [...]
    }

写入失败只记日志，绝不抛出中断调用方，绝不删除任何文件。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from .hash_utils import parse_front_matter, split_body_sections
from . import card_db

logger = logging.getLogger("storage.canvas")

# ============================================================
# [画布第二步 D5] 拆存储长期不变量（L1–L8）—— [画布重构 2026-10-15] L3/L4/L6
# 随 layout.json 退役改写，其余逐字保留（它们约束的是"人给的位置"这件事，
# 与位置存在哪儿无关；换存储不等于换纪律）。
#
# L1 用户布局只由人给——任何系统推算（避让/仲裁/自动排布）都不许写进
#    cards 表的坐标列；系统算的位置只用于显示、只活在内存里。
# L2 canvas.json 可随时删除、自动重建、用户无感（验收实证：删文件→GET→
#    画布全开、钉住坐标原样）。
# L3 ★ 已改写：cards 表是用户坐标的**唯一真相来源**。它丢了＝用户布局永久
#    丢失、不可重建——保护靠快照（data_snapshot 的 PROTECTED_DIRS 含
#    works/，cards.db 自动入快照），**不靠 rebuild**（rebuild 救不了它，
#    也不许假装能救）。反过来说：位置不再有第二个存放点，删掉 layout.json
#    这条路是被**封死**的，不是"顺手清理"。
# L4 ★ 已改写：用户字段（坐标）与派生字段（cards/relations/
#    conclusion_summary 等）彻底分家——用户在 cards 表的列上，派生数据在
#    canvas.json 的键上，两者不再共处一个文档，故"不许互相混写"由类型系统
#    与查询条件各自把关。
# L5 对外接口不变：GET /canvas 响应仍含 positions 字段、PATCH /positions
#    端点与 merge_positions 签名原样——前端零改动是拆存储的边界条件。
#    ★ [画布重构] 存储换了、HTTP 契约一字未动，这是本次改动的硬边界。
# L6 ★ 已改写：坐标列稀疏——只记用户表态过的卡（拖过/钉过/整理过）：
#    未摆过的卡 pos_x/pos_y 恒为 NULL，**不建空记录**（自动排每次现算、
#    不落盘，任务书约束 4）。
# L7 pinned 与 x/y 同居一张卡的同一行，不许分离存储、也不许分表——三者
#    一次 UPDATE 到位，不存在"搬了一半"的中间态。
# L8 任一数据缺失或损坏都不阻断画布打开：load/rebuild/load_positions 全部
#    fail-open（返空/重建），绝不抛出中断调用方。
# ============================================================


BASE_DIR = Path(__file__).resolve().parents[2]
WORKS_DIR = BASE_DIR / "works"
CARDS_DIR = BASE_DIR / "cards"        # 旧路径（兼容）
CANVAS_PATH = BASE_DIR / "canvas.json"  # 旧路径（兼容）

PROJECT_NAME = "推演agent"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _canvas_path(work_id: str) -> Path:
    return WORKS_DIR / work_id / "canvas.json"


# [R4] 结论文本摘要上限——与前端 canvas.js 的 getSummary 逐字一致
# （前端原文：firstLine.length > 120 ? firstLine.slice(0,120) + "…" : firstLine）
SUMMARY_MAX = 120


def _conclusion_summary(status_block: str) -> str:
    """取现状块（status_block 列）第一行非空文字，超 120 字截断加省略号。

    与前端 getSummary 的算法逐字一致（先按行 split、取第一个非空 trim 行、超长截断）。
    现状块为空/全空白 → 返回空字符串（不写占位文字）。
    [卡片信息结构改造 S2-11/G-7] canvas.json 字段名 conclusion_summary **不变**
    （前端快路径依赖它）。
    [卡片存储迁移 SQLite] 入参从「正文全文」改为「现状块列内容」——库内已是独立列，
    不再需要拆块。
    """
    first_line = ""
    for line in (status_block or "").split("\n"):
        s = line.strip()
        if s:
            first_line = s
            break
    if len(first_line) > SUMMARY_MAX:
        return first_line[:SUMMARY_MAX] + "…"
    return first_line


def rebuild(work_id: str) -> dict[str, Any]:
    """全量重建 work 的 canvas.json 并写盘，返回结果字典。

    [画布第二步 D3] 拆存储收口：canvas.json 是**纯派生缓存**（不变量 L2——
    可随时删除、自动重建、用户无感），rebuild 不再抢救/写入 positions；
    [画布重构 2026-10-15] 用户坐标的唯一真相来源是 **cards 表的坐标列**
    （load_positions/merge_positions），canvas.json 里连空 ``positions``
    键都不再生成。
    保留旧 canvas.json 的 created_at（既有行为，不动）。
    [卡片存储迁移 SQLite] 数据源从**卡片 md 目录**改为库内 ``cards`` 表
    （canvas.json 本身仍为文件——画布线，不在迁移范围）。
    """
    canvas_path = _canvas_path(work_id)
    created_at = _now_iso()
    try:
        old = json.loads(canvas_path.read_text(encoding="utf-8"))
        if isinstance(old, dict):
            if old.get("created_at"):
                created_at = old["created_at"]
    except Exception:
        pass

    def _fn(conn: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(
            "SELECT card_id, title, card_type, parent, created_at,"
            " relations, status_block FROM cards ORDER BY card_id"
        )]

    rows = card_db.read_sync(work_id, _fn, []) or []
    cards: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    for row in rows:
        card_id = row["card_id"]
        cards.append({
            "card_id": card_id,
            "title": row["title"] or "",
            "card_type": row["card_type"] or "线索",
            "parent": row["parent"] or "",
            "created_at": row["created_at"] or "",
            # [R4] 结论摘要随画布一次到手——前端 getSummary 优先读本字段，
            # 免去首屏对每张可见卡各发一次 getCard 请求（N+1）。
            "conclusion_summary": _conclusion_summary(row["status_block"] or ""),
        })
        try:
            rels = json.loads(row["relations"] or "[]")
        except (TypeError, ValueError):
            rels = []
        for rel in (rels or []):
            if isinstance(rel, dict):
                item = {"source": card_id, "target": rel.get("target", ""),
                        "type": rel.get("type", "")}
                if rel.get("note"):
                    item["note"] = rel["note"]
                relations.append(item)

    # [R4-3-5] 重建打点（沿用既有 logger，不参与分支、不设闸）
    summary_count = sum(1 for c in cards if c.get("conclusion_summary"))
    logger.info("canvas 重建：%d 卡，含摘要 %d 条", len(cards), summary_count)

    result = {
        "project_name": PROJECT_NAME,
        "work_id": work_id,
        "created_at": created_at,
        "cards": cards,
        "relations": relations,
    }
    try:
        canvas_path.parent.mkdir(parents=True, exist_ok=True)
        canvas_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("canvas.json 写入失败（不中断）: %s", exc)
    return result


async def update_cache(work_id: str) -> dict[str, Any]:
    """v1 用全量重建代替增量维护（线程内跑，避免库读阻塞事件循环）。"""
    return await asyncio.to_thread(rebuild, work_id)


def load(work_id: str) -> dict[str, Any]:
    """读 work 的 canvas.json；不存在或解析失败则自动重建。"""
    canvas_path = _canvas_path(work_id)
    try:
        data = json.loads(canvas_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        logger.warning("canvas.json 内容非法，触发重建 work=%s", work_id)
    except FileNotFoundError:
        logger.info("canvas.json 不存在，触发重建 work=%s", work_id)
    except Exception as exc:
        logger.warning("canvas.json 读取失败，触发重建 work=%s: %s", work_id, exc)
    return rebuild(work_id)


# ============================================================
# [画布重构 2026-10-15 · 阶段 1.2] 用户坐标存储：layout.json → cards 表
#
# 旧形态（画布第二步 D1/D2）：works/{work_id}/layout.json 存用户坐标，
#   canvas.json 存派生缓存，两文件分家。
# 新形态：位置是**卡片自身属性**，直接住在 cards 表的 pos_x/pos_y/pinned
#   三列上（card_db.POSITION_COLUMNS）——少一个文件、少一次整文档重写、
#   少一个"两份数据对不上"的可能。
#
# 对外接口一个字没动（不变量 L5）：GET /canvas 的响应仍带 positions，
# PATCH /positions 端点与请求/响应体原样，merge_positions 仍是
# `{card_id: {x?, y?, pinned?}}` 进、`{card_id: {x, y, pinned}}` 出。
# 前端零改动。
#
# 【深合并语义保留 · E6/RD-04】载荷可以是**部分字段**（前端"整理画布"发
# 的就是 `{card_id: {pinned: false}}`，没有坐标）。故 UPDATE 只拼载荷里
# 真正出现过的列，没带的列原值不动——不拿「全字段覆盖」去实现，那会把
# 用户摆好的坐标抹成 NULL。
# ============================================================


def _positions_of(rows: Any) -> dict[str, dict[str, Any]]:
    """cards 行 → ``{card_id: {x, y, pinned}}``（形状与旧 layout.json 记录同构）。

    - ``x``/``y``：有值给浮点、无值给 ``None``。前端 ``canvas.js`` 判的是
      ``Number.isFinite(pos.x)``——``None`` 与缺字段同样落 false，于是
      「钉住但没坐标」的卡自动走布局，不会被当成有效坐标（这一支前端早
      就写好了，本函数只是别破坏它）。
    - ``pinned``：给 ``bool``（旧记录里就是 JSON ``true``/``false``）。
    """
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out[r["card_id"]] = {
            "x": r["pos_x"],
            "y": r["pos_y"],
            "pinned": bool(r["pinned"] or 0),
        }
    return out


async def load_positions(work_id: str) -> dict[str, dict[str, Any]]:
    """读该 work 的用户坐标：``{card_id: {x, y, pinned}}``。

    稀疏（L6）：只出**用户表态过的卡**——坐标非空，或钉住了但坐标为空。
    什么都没表态过的卡不在返回里（前端拿到就是"无记录"，走自动排），
    与旧 layout.json 的稀疏原则同口径。

    fail-open（L8）：库不存在／读失败一律返 ``{}``，绝不抛出中断调用方。
    """
    def _fn(conn: Any) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            "SELECT card_id, pos_x, pos_y, pinned FROM cards"
            " WHERE pos_x IS NOT NULL OR pos_y IS NOT NULL OR COALESCE(pinned, 0) <> 0"
        )
        return _positions_of(rows)

    try:
        return await card_db.run_read(work_id, _fn, {}) or {}
    except Exception as exc:  # noqa: BLE001 fail-open，同 rebuild 纪律
        logger.warning("位置读取失败，按空布局处理 work=%s: %s", work_id, exc)
        return {}


async def merge_positions(work_id: str, positions: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """合并写入用户坐标，返回**合并后的全量**坐标（``{card_id: {x,y,pinned}}``）。

    - **逐字段深合并**（E6 语义随迁，RD-04 不许回退）：载荷缺哪个字段就
      不动哪个列。``{pinned: false}`` 只清钉住标记，坐标原样保留。
    - **脏行过滤**（方案书风险 2）：``UPDATE ... WHERE card_id = ?`` 命中 0
      行＝载荷指向库里不存在的卡（卡已删、载荷来自旧前端缓存），此时
      **不插入任何新行**——cards 表的 card_id 是外键事实，位置不许凭空
      长出一张卡来。只记 warning，不抛（fail-open）。
    - 对外签名与返回语义不变（不变量 L5）：前端不消费 PATCH 响应体，
      这里回全量只是"改完顺手给个真相"。
    """
    updates: list[tuple[str, str, tuple[Any, ...]]] = []
    for cid, rec in (positions or {}).items():
        if not isinstance(rec, dict):
            continue
        sets: list[str] = []
        vals: list[Any] = []
        # x/y 只认有限数：前端自己也会判 Number.isFinite，别把 NaN/inf 写进库
        for key, col in (("x", "pos_x"), ("y", "pos_y")):
            if rec.get(key) is None:
                continue
            try:
                num = float(rec[key])
            except (TypeError, ValueError):
                logger.warning("坐标字段非数值，按未提供处理 card=%s field=%s value=%r",
                               cid, key, rec.get(key))
                continue
            if not math.isfinite(num):
                logger.warning("坐标字段非有限数，按未提供处理 card=%s field=%s value=%r",
                               cid, key, rec.get(key))
                continue
            sets.append(f"{col} = ?")
            vals.append(num)
        if rec.get("pinned") is not None:
            sets.append("pinned = ?")
            vals.append(1 if rec["pinned"] else 0)
        if sets:
            updates.append((cid, ", ".join(sets), tuple(vals)))

    if not updates:
        return await load_positions(work_id)

    def _fn(conn: Any) -> list[str]:
        skipped: list[str] = []
        for cid, set_clause, vals in updates:
            cur = conn.execute(
                f"UPDATE cards SET {set_clause} WHERE card_id = ?", (*vals, cid)
            )
            if not cur.rowcount:
                skipped.append(cid)
        return skipped

    try:
        skipped = await card_db.run_write(work_id, _fn) or []
    except Exception as exc:  # noqa: BLE001 写失败只记日志，绝不中断调用方
        logger.warning("坐标写入失败（不中断）work=%s: %s", work_id, exc)
        return await load_positions(work_id)

    if skipped:
        logger.warning("坐标载荷指向不存在的卡片，已过滤（不写脏行）work=%s cards=%s",
                       work_id, skipped)
    return await load_positions(work_id)
