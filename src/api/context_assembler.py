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


def _control_path() -> Path:
    """控制面配置位置；调用时派生路径，不缓存模块级路径常量。"""
    return BASE_DIR / "config" / "context_regions.json"


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


def build_ctx(work_id: str, current_id: str,
              memory_state: dict[str, Any] | None = None) -> context_regions.RegionCtx:
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
        project_class=pc,
        type_registry=card_store.load_type_registry(),
        memory_state=state,
        memory_entries=(state or {}).get("entries") or [],
    )


def build_context_regions(work_id: str, current_id: str,
                          memory_state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """分区构建（**底座 2026-10-15：遍历注册表——装配器不再认识任何具体区**）。

    三条规则（与既有总表 #2/#3 一致）：
    1. 只渲染 ``owner == "assembler"`` 的区（prompt / runner 归属的区由各自模块装配）；
    2. 按 ``order`` 升序；**控制面配置可覆盖 order 与启停**；
    3. **渲染零加工**：调 ``render(ctx)``，**空串 ＝「没有来源就不出现这个区」**。

    加 / 删 / 调序一个区＝改 ``context_regions.REGION_TABLE`` 或控制面配置，本函数一字不动。

    [T12-SP-05] memory_state＝routes 层读好的凝练态（水位 mark＋entries）——IO 分离。
    开关关（RD-08 缺省）或空记忆（entries 空）→ 不出现 200 区。
    """
    ctx = build_ctx(work_id, current_id, memory_state)
    control = load_control()
    out: list[dict[str, Any]] = []
    for spec in context_regions.REGION_TABLE:
        if spec.get("owner") != "assembler":
            continue
        ctl = control.get(spec["key"]) if isinstance(control, dict) else None
        if isinstance(ctl, dict) and ctl.get("enabled") is False:
            continue
        order = spec["order"]
        if isinstance(ctl, dict) and isinstance(ctl.get("order"), int):
            order = ctl["order"]
        text = spec["render"](ctx)
        if not text:  # 没有来源就不出现这个区
            continue
        cnt = spec.get("count", 1)
        out.append({
            "order": order,
            "name": spec["name"],
            "channel": spec["channel"],
            "seats": spec["seats"],
            "text": text,
            "entries": cnt(ctx, text) if callable(cnt) else cnt,
        })
    out.sort(key=lambda r: r["order"])  # 控制面可能改 order ⇒ 收完再排
    return out


def build_neutral_context(work_id: str, current_id: str) -> str:
    """中性上下文（主模型与三思考席共用同一份）：卡片清单 ＋ 当前卡标识。

    [T11-T2] 本函数降为薄包装：分区渲染后按 order join，输出与改造前逐字
    一致（V1 断言含空 ctx 分支与 rstrip 两条路径）。
    [拉取裁定 2026-10-02] 不推送任何卡片正文——模型需要时经 read_card 调取。
    上一轮模型写入的卡片内容不会再自动进入任何后续输入（回流断根）。
    """
    regions = build_context_regions(work_id, current_id)
    return context_regions.assemble_regions([r["text"] for r in regions])
