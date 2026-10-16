# -*- coding: utf-8 -*-
"""上下文组装器（白板版 2026-10-02）。

只组装机械事实，不注入任何内容：
- 卡片职责：类型注册表里每类卡一行的职责说明（类型级，非卡级）
- 卡片清单：全部卡的 id · 标题 · 父卡（不含类型、不含正文摘要、无空位提示）
- 当前卡：id · 标题（正文不推送——任何卡片正文一律经 read_card 拉取，
  杜绝"上轮模型写入的卡片内容变成下轮常设输入"的回流）
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..storage import card_db, card_store, memory_store, work_store
from ..storage.hash_utils import parse_front_matter
from . import context_regions

logger = logging.getLogger("api.context_assembler")

BASE_DIR = Path(__file__).resolve().parents[2]


def _load_cards_index(work_id: str) -> list[dict[str, Any]]:
    """查库，返回 [{card_id, title, parent}]（按 card_id 排序）。

    [卡片存储迁移 SQLite 2026-10-14] 数据源从 ``cards/*.md`` 改为库——
    **这是「减污染」的关键点**：卡片清单仍只取 id/title/parent 三样，
    取什么查什么，正文不再有机会被整份读进上下文（方案 §3.1 B）。
    """
    def _fn(conn: Any) -> list[dict[str, Any]]:
        return [
            {"card_id": r["card_id"], "title": r["title"] or "",
             "parent": r["parent"] or ""}
            for r in conn.execute(
                "SELECT card_id, title, parent FROM cards ORDER BY card_id")
        ]

    return card_db.read_sync(work_id, _fn, []) or []


def _load_direct_child_status(work_id: str, parent_id: str) -> list[dict[str, Any]]:
    """只取当前卡的**直接子卡** id/title/status_block。

    **SQL 不递归**（`WHERE work_id=? AND parent=?`，只一层）⇒ 孙级在结构上取不到
    ——「父卡只看直接子级」不靠过滤逻辑保证，靠查询本身。
    只取所需三列：不读子卡的 mission/rulings/history，更不碰影子（减污染）。
    空 status_block 保留到纯 renderer，由区的「空内容不出现」规则统一过滤。
    """
    if not parent_id:
        return []

    def _fn(conn: Any) -> list[dict[str, Any]]:
        return [
            {"card_id": r["card_id"], "title": r["title"] or "",
             "status_block": r["status_block"] or ""}
            for r in conn.execute(
                "SELECT card_id, title, status_block FROM cards "
                "WHERE work_id = ? AND parent = ? ORDER BY card_id",
                (work_id, parent_id),
            )
        ]

    return card_db.read_sync(work_id, _fn, []) or []


def _control_path() -> Path:
    """控制面配置位置；调用时派生路径，不缓存模块级路径常量。"""
    return BASE_DIR / "config" / "context_regions.json"


def _type_texts_path() -> Path:
    """「按卡类型挂的文本」（多维度）配置位置；调用时派生，测试可 monkeypatch。"""
    return BASE_DIR / "config" / "type_texts.json"


def load_type_texts() -> dict[str, dict[str, str]]:
    """读「按卡类型挂的文本」（多维度）。**fail-open**：读不到/坏 JSON/结构不对 → 空表。

    与类型注册表同纪律：**配置是控制源，不是可用性依赖**——绝不因它阻断装配。
    返回 ``{维度: {类型: 文本}}``；每个维度只保留 value 为非空字符串的条目。
    下划线前缀键（如 ``_meta``）是文档元数据、不是维度，跳过。
    类型清单不在本文件、不校验（唯一来源＝类型注册表）。
    """
    import json as _json
    try:
        raw = _type_texts_path().read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[type_texts] 配置读取失败（视为全空）: %s", exc)
        return {}
    try:
        data = _json.loads(raw)
    except ValueError as exc:
        logger.warning("[type_texts] 配置解析失败（视为全空）: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for dim, table in data.items():
        if str(dim).startswith("_") or not isinstance(table, dict):
            continue
        clean = {str(k): v for k, v in table.items()
                 if isinstance(v, str) and v.strip()}
        if clean:
            out[str(dim)] = clean
    return out


def load_control() -> dict[str, Any]:
    """读控制面配置（区的启停/顺序）。**fail-open**：读不到/坏 JSON → 空表＝全用注册表默认。

    纪律同类型注册表：**配置是控制源，不是可用性依赖**——绝不因它阻断装配。
    """
    import json as _json
    try:
        raw = _control_path().read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[context_regions] 控制面配置读取失败（用注册表默认）: %s", exc)
        return {}
    try:
        data = _json.loads(raw)
    except ValueError as exc:
        logger.warning("[context_regions] 控制面配置解析失败（用注册表默认）: %s", exc)
        return {}
    regs = data.get("regions") if isinstance(data, dict) else None
    return regs if isinstance(regs, dict) else {}


def _load_rule_sections(work_id: str) -> dict[str, str]:
    """扫本 work 第一张 card_type=rule 的卡，正文（mission 列）按 `## ` 切区。

    一次 IO；多张 rule 卡取首张 + ops 留痕（Open Item 2）。无规则卡／读失败
    → 空 dict（「没有来源就不出现这个区」，不阻断装配）。
    """
    def _fn(conn: Any) -> str:
        row = conn.execute(
            "SELECT mission FROM cards WHERE card_type='rule' "
            "ORDER BY card_id LIMIT 1").fetchone()
        return (row["mission"] or "") if row else ""

    try:
        body = card_db.read_sync(work_id, _fn, "") or ""
    except Exception:  # noqa: BLE001 取不到＝无来源，不阻断装配
        return {}
    if not body.strip():
        return {}
    sections = context_regions.split_rule_sections(body)
    sections.pop("", None)  # 区头前游离正文不进注入面（只留存储完整性）
    return sections


def build_ctx(work_id: str, current_id: str,
              memory_state: dict[str, Any] | None = None,
              history_source: list[dict[str, Any]] | None = None) -> context_regions.RegionCtx:
    """取数一次（**装配链上唯一的 IO 处**）：卡片索引／当前卡／树类／类型注册表／凝练态。

    渲染器（`context_regions`）保持纯函数——它们只读这个 ctx，不自己取数。
    """
    cards = _load_cards_index(work_id)
    cur = next((c for c in cards if c["card_id"] == current_id), None)
    try:
        pc = card_store._root_project_class(work_id)
    except Exception:  # noqa: BLE001 取不到树类＝无来源，不阻断装配
        pc = ""
    state = memory_state
    if state is None and memory_store.condense_enabled():
        state = memory_store.load_memory(work_id, current_id)
    return context_regions.RegionCtx(
        work_id=work_id,
        card_id=current_id,
        cards=cards,
        work_card_id=work_store.work_card_id_of(work_id),
        cur=cur,
        cur_meta=card_store._meta_of(work_id, current_id) or {},
        direct_child_status=_load_direct_child_status(work_id, current_id),
        project_class=pc,
        type_registry=card_store.load_type_registry(),
        type_texts=load_type_texts(),
        memory_state=state,
        memory_entries=(state or {}).get("entries") or [],
        controls=load_control(),
        rule_sections=_load_rule_sections(work_id),
        history_source=history_source or [],
        history_mark=int((state or {}).get("mark", 0) or 0),
    )


def build_context_regions(work_id: str, current_id: str,
                          memory_state: dict[str, Any] | None = None,
                          *, ctx: context_regions.RegionCtx | None = None) -> list[dict[str, Any]]:
    """兼容入口：返回本卡 system 上下文区（不含使用说明）。

    实际渲染经统一 context registry；保留既有返回契约，供 draft/日志/测试消费。
    """
    ctx = ctx or build_ctx(work_id, current_id, memory_state)
    rows = context_regions.render_registered_regions(
        ctx, profile="main", deliveries={"system"}, source_owner="assembler")
    return [{
        "order": r["order"], "name": r["name"], "channel": r["channel"],
        "seats": r["seats"], "text": r["payload"], "entries": r["entries"],
    } for r in rows]


def build_context_bundle(work_id: str, current_id: str,
                         memory_state: dict[str, Any] | None = None,
                         history_source: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """一次建好主模型/三席上下文帧；system、history 均由同一注册表渲染。"""
    ctx = build_ctx(work_id, current_id, memory_state, history_source)
    main = context_regions.compose_model_context(ctx, profile="main")
    viewpoints = context_regions.compose_model_context(ctx, profile="viewpoint")
    return {
        "context": ctx,
        "main": main,
        "viewpoints": viewpoints,
        "regions": main["system_regions"],
    }


def build_neutral_context(work_id: str, current_id: str) -> str:
    """中性上下文（主模型与三思考席共用同一份）：卡片清单 ＋ 当前卡标识。

    [T11-T2] 本函数降为薄包装：分区渲染后按 order join，输出与改造前逐字
    一致（V1 断言含空 ctx 分支与 rstrip 两条路径）。
    [拉取裁定 2026-10-02] 不推送任何卡片正文——模型需要时经 read_card 调取。
    上一轮模型写入的卡片内容不会再自动进入任何后续输入（回流断根）。
    """
    regions = build_context_regions(work_id, current_id)
    return context_regions.assemble_regions([r["text"] for r in regions])
