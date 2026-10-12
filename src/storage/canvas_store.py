# -*- coding: utf-8 -*-
"""canvas.json 缓存维护。canvas.json 是可重建缓存，卡片文件才是真身。

Phase 7：每个 work 各有一份 canvas.json，路径 = works/{work_id}/canvas.json。
canvas.json 顶层新增 positions 字段（卡片在画布上的坐标 + pinned 标记）：
    {
      "project_name": "...",
      "created_at": "...",
      "cards": [...],
      "relations": [...],
      "positions": {"card_001": {"x":0,"y":0,"pinned":false}, ...}
    }

写入失败只记日志，绝不抛出中断调用方，绝不删除任何文件。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .hash_utils import parse_front_matter, split_body_sections

logger = logging.getLogger("storage.canvas")

# ============================================================
# [画布第二步 D5] 拆存储长期不变量（L1–L8，拦下一个改这块代码的人）：
#
# L1 用户布局只由人给——任何系统推算（避让/仲裁/自动排布）都不许写进
#    layout.json；系统算的位置只用于显示。
# L2 canvas.json 可随时删除、自动重建、用户无感（验收实证：删文件→GET→
#    画布全开、钉住坐标原样）。
# L3 layout.json 丢了＝用户布局永久丢失、不可重建——它的保护靠快照
#    （data_snapshot PROTECTED_DIRS 含 works/，layout.json 自动入快照），
#    不靠 rebuild（rebuild 救不了它，也不许假装能救）。
# L4 两文件职责不混：用户字段（positions）不进 canvas.json，派生字段
#    （cards/relations/conclusion_summary 等）不进 layout.json。
# L5 对外接口不变：GET /canvas 响应仍含 positions 字段、PATCH /positions
#    端点与 merge_positions 签名原样——前端零改动是拆存储的边界条件。
# L6 layout.json 稀疏：只记用户表态过的卡（拖过/钉过/整理过），不为
#    全量卡建空记录；无坐标的 work 不建空文件。
# L7 pinned 与 x/y 同进同出、同居一条记录，不许分离存储（深合并保证
#    局部更新不拆散三字段；迁移按整条搬运）。
# L8 任一文件缺失或损坏都不阻断画布打开：load/rebuild/load_layout 全部
#    fail-open（返空/重建），绝不抛出中断调用方。
# ============================================================


BASE_DIR = Path(__file__).resolve().parents[2]
WORKS_DIR = BASE_DIR / "works"
CARDS_DIR = BASE_DIR / "cards"        # 旧路径（兼容）
CANVAS_PATH = BASE_DIR / "canvas.json"  # 旧路径（兼容）

PROJECT_NAME = "推演agent"

_CARD_FILE_RE = re.compile(r"^card_(\d{3,})\.md$")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _canvas_path(work_id: str) -> Path:
    return WORKS_DIR / work_id / "canvas.json"


def _cards_dir(work_id: str) -> Path:
    return WORKS_DIR / work_id / "cards"


# [R4] 结论文本摘要上限——与前端 canvas.js 的 getSummary 逐字一致
# （前端原文：firstLine.length > 120 ? firstLine.slice(0,120) + "…" : firstLine）
SUMMARY_MAX = 120


def _conclusion_summary(body: str) -> str:
    """取现状块（status_block）第一行非空文字，超 120 字截断加省略号。

    与前端 getSummary 的算法逐字一致（先按行 split、取第一个非空 trim 行、超长截断）。
    现状块为空/全空白 → 返回空字符串（不写占位文字）。
    解析失败不抛出——返回空串，绝不中断 rebuild。
    [卡片信息结构改造 S2-11/G-7] 取数键随拆分器归一改 status_block；
    canvas.json 字段名 conclusion_summary **不变**（S2-11，前端快路径依赖它）。
    """
    try:
        sections = split_body_sections(body or "")
        concl = sections.get("status_block", "") or ""
    except Exception:
        return ""
    first_line = ""
    for line in concl.split("\n"):
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
    用户坐标的唯一真相来源是 layout.json（load_layout/save_layout）。
    保留旧 canvas.json 的 created_at（既有行为，不动）。
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

    cards: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    cd = _cards_dir(work_id)
    if cd.exists():
        for path in sorted(cd.glob("card_*.md")):
            if not _CARD_FILE_RE.match(path.name):
                continue
            card_id = path.stem
            try:
                meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("解析卡片失败，跳过 %s: %s", path.name, exc)
                continue
            cards.append({
                "card_id": card_id,
                "title": meta.get("title", ""),
                "card_type": meta.get("card_type") or "线索",
                "parent": meta.get("parent", ""),
                "created_at": meta.get("created_at", ""),
                # [R4] 结论摘要随画布一次到手——前端 getSummary 优先读本字段，
                # 免去首屏对每张可见卡各发一次 getCard 请求（N+1）。
                # canvas.json 是可重建缓存（卡片文件才是真身），本字段不写用户真身数据。
                "conclusion_summary": _conclusion_summary(body),
            })
            for rel in meta.get("relations") or []:
                if isinstance(rel, dict):
                    item = {"source": card_id, "target": rel.get("target", ""), "type": rel.get("type", "")}
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
    """v1 用全量重建代替增量维护。"""
    return rebuild(work_id)


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


def merge_positions(work_id: str, positions: dict[str, Any]) -> dict[str, Any]:
    """合并写入用户坐标（增量更新单卡坐标）。

    [画布第二步 D2] 读写全部改走 ``layout.json``（薄委托 ``save_layout``，
    深合并单实现）——**绝不再碰 canvas.json**（用户数据与派生缓存分家，
    不变量 L4）。深合并语义照旧（第一步 E6 成果，RD-04 不许回退）：
    逐字段合并、缺失字段保留原值，安全性放在收口处。
    对外接口不变（L5）：函数签名与调用点原样，PATCH /positions 端点原样。
    返回值＝layout 文档（``{version, positions}``；旧版返回 canvas 全量
    dict——前端不消费 PATCH 响应体，形态变化无消费方）。
    """
    return save_layout(work_id, positions)


# ============================================================
# [画布第二步 D1] 用户布局存储 layout.json（拆存储）。
#
# 目标形态（方案 §2.1–§2.4）：
#   works/{work_id}/layout.json  ← 用户数据（唯一真相来源，本节两个函数管它）
#   works/{work_id}/canvas.json  ← 派生缓存（rebuild 管；D3 之后不再含 positions）
# D1 阶段两函数**纯加法**：任何既有路径都还不调用它们（rebuild/merge_positions/
# routes 不动）；D2 才切换读写路径。
# ============================================================

LAYOUT_VERSION = 1


def _layout_path(work_id: str) -> Path:
    return WORKS_DIR / work_id / "layout.json"


def load_layout(work_id: str) -> dict[str, Any]:
    """读用户布局文件 ``works/{work_id}/layout.json``。

    不存在或损坏一律返回 ``{}``，不抛异常、不阻断（不变量 L8：任一文件缺失或
    损坏都不阻断画布打开）。**绝不回退读 canvas.json 旧 positions**（RD-08：
    兼容读会留下第二个真相来源，将来没人敢删）。
    """
    path = _layout_path(work_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("layout.json 读取失败，按空布局处理 work=%s: %s", work_id, exc)
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("positions"), dict):
        logger.warning("layout.json 形态非法，按空布局处理 work=%s", work_id)
        return {}
    return data


def save_layout(work_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """深合并写 layout.json：逐字段合并、缺失字段保留原值（与 E6 同一语义）。

    - ``patch`` 形态＝``{card_id: {部分或全量字段}}``；某卡只带部分字段（如
      ``{pinned: false}``）时该卡其余字段（x/y）原样保留——深合并语义在收口处
      保护，不放在调用方（第一步 E6 裁定，RD-04 不许回退）。
    - 写入 ``version`` 字段（演进抓手，LAYOUT_VERSION＝当前 1；将来字段
      结构演进时递增版本号并配套迁移，旧文件按 version 分支解读）。
    - 写入失败只记日志，绝不抛出中断调用方（与 rebuild 写盘同一纪律）。
    """
    cur_doc = load_layout(work_id)
    cur = cur_doc.get("positions") if isinstance(cur_doc.get("positions"), dict) else {}
    for cid, rec in (patch or {}).items():
        base = cur.get(cid) if isinstance(cur.get(cid), dict) else {}
        if isinstance(rec, dict):
            base.update(rec)
        cur[cid] = base
    doc = {"version": LAYOUT_VERSION, "positions": cur}
    path = _layout_path(work_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("layout.json 写入失败（不中断）: %s", exc)
    return doc
