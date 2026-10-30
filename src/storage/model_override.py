# -*- coding: utf-8 -*-
"""模型配置表（每个项目一张，**只存结果，不存规则**）。

[模型配置表 2026-10-21 · 用户裁定]
  1. 每个项目有且只有一张模型配置表，整个系统调模型都读这张表；
  2. 新建项目/新建卡时，按一套默认策略算一次，把结果**填进表**；
  3. 改卡片模型 = 更新表里那一行。
  ⇒ 运行时只读表，不做任何优先级仲裁，因此不可能出现「改了不生效 / 谁赢谁」。
     默认策略（tiers / bindings / types / system）只在**填表那一刻**被读一次。

载体：``works/{work_id}/model_config_table.json``，结构：
    {
      "_meta": {"version": 2},
      "cards": {
        "card_001": {"model": "kimi-k3", "bindings": {"viewpoint": ["a","b"]}},
        "card_002": {"model": "ds-v41-flash-aliyun"}
      },
      "policy": {"types": {"project": "..."}}   // 建卡策略快照（只读，不参与运行）
    }
- ``cards[card_id].model``  这张卡的主模型（一行一个，改它就是改这张卡）
- ``cards[card_id].bindings`` 可选的角色例外（目前只有 viewpoint 用：三席池）
- 表里没有这张卡 ⇒ 运行时**当场按默认策略算一次并写入**（自愈；老项目零迁移）
- 坏文件/缺文件 ⇒ fail-open 空表，绝不抛

老格式（v1：``work`` + ``cards[].default_model`` + ``types``）在读入时归一：
``default_model`` → ``model``；``work.default_model`` 一次性展开到还没有 model 的行；
``types`` 搬进 ``policy.types``。归一结果下一次写入即落成 v2。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from . import work_store

logger = __import__("logging").getLogger("storage.model_override")

#: 新文件名。旧名 ``model_override.json`` 仍会被读取（迁移用）。
TABLE_FILE = "model_config_table.json"
LEGACY_FILE = "model_override.json"
OVERRIDE_FILE = TABLE_FILE          # 兼容旧引用


def path_for(work_id: str) -> Path:
    return work_store._work_dir(work_id) / TABLE_FILE


def _legacy_path_for(work_id: str) -> Path:
    return work_store._work_dir(work_id) / LEGACY_FILE


# ============================================================
# 读
# ============================================================


def _normalize_row(raw: dict) -> dict:
    """一行标准化：model（主模型）+ fallbacks（层内兜底）+ bindings（角色例外）。"""
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    v = raw.get("model")
    if not (isinstance(v, str) and v.strip()):
        v = raw.get("default_model")          # v1 兼容
    if isinstance(v, str) and v.strip():
        out["model"] = v.strip()
    # [层内兜底 2026-10-21] 同层其余成员（按优先序）。首选在主模型里，这里不重复。
    fb = raw.get("fallbacks")
    if isinstance(fb, (list, tuple)):
        items = [str(x).strip() for x in fb if isinstance(x, str) and str(x).strip()]
        items = [x for x in items if x != out.get("model")]
        if items:
            out["fallbacks"] = items
    b = raw.get("bindings")
    if isinstance(b, dict):
        cleaned: dict[str, Any] = {}
        for role, val in b.items():
            if val is None or val == "" or val == []:
                continue
            if isinstance(val, list):
                items = [str(x) for x in val if isinstance(x, str) and x.strip()]
                if items:
                    cleaned[str(role)] = items
            elif isinstance(val, str) and val.strip():
                cleaned[str(role)] = val.strip()
        if cleaned:
            out["bindings"] = cleaned
    # [wb_exec 2026-10-23] 执行体选择：这一行额外带一个 ``exec_backend``。
    #   auto            缺省 ⇒ coder 卡走 WorkBuddy 执行体（按配比器选模型）
    #   system          ⇒ 用推演agent 自己的模型（用户在卡上手动切回来的落点）
    #   wb:<版本>:<模型>  ⇒ 钉死某个 WorkBuddy 模型，不参与配比
    # **只做形状归一，不做任何裁决**（裁决在 src/api/wb_exec/bridge.py）。
    eb = raw.get("exec_backend")
    if isinstance(eb, str) and eb.strip():
        out["exec_backend"] = eb.strip()
    # [wb_exec 2026-10-23] 上次**实际**路由到的（版本, 模型）——
    # 卡面 UI 开工后显示的就是它。必须活过 put_row 的往返
    # （put_row 是整行替换 ⇒ 归一化时丢了就永远存不住）。
    lr = raw.get("last_routed")
    if isinstance(lr, dict) and lr:
        out["last_routed"] = {
            "executor": str(lr.get("executor") or ""),
            "model": str(lr.get("model") or ""),
            "at": int(lr.get("at") or 0),
            "tool_calls": int(lr.get("tool_calls") or 0),
        }
        # [回落 2026-10-23 19:55] 委派失败**静默回落**系统模型时的记录：
        #   fallback="system" ＋ reason（原因）。
        #   **必须活过归一化**（put_row 是整行替换 ⇒ 归一化时丢了就永远存不住）
        #   ⇒ 卡面才能如实显示"本轮实际走系统自己的模型（原因…）"，不装成在 WorkBuddy 上跑。
        fb = lr.get("fallback")
        if isinstance(fb, str) and fb.strip():
            out["last_routed"]["fallback"] = fb.strip()
            out["last_routed"]["reason"] = str(lr.get("reason") or "")[:200]
    src = raw.get("src")
    out["src"] = "manual" if src == "manual" else "default"
    return out


def _normalize(data: dict, work_id: str) -> dict:
    """任意版本 → v2 结构（不落盘）。"""
    out: dict[str, Any] = {"_meta": {"version": 2}, "cards": {}, "policy": {}}
    if not isinstance(data, dict):
        return out
    cards_in = data.get("cards")
    cards: dict[str, dict] = {}
    if isinstance(cards_in, dict):
        for cid, row in cards_in.items():
            r = _normalize_row(row)
            if r:
                cards[str(cid)] = r
    # v1 的 work.default_model：只补到「还没有主模型」的行（不覆盖已填的卡）
    work_ov = data.get("work")
    if isinstance(work_ov, dict):
        wd = work_ov.get("default_model")
        if isinstance(wd, str) and wd.strip():
            for cid in cards:
                cards[cid].setdefault("model", wd.strip())
        wb = work_ov.get("bindings")
        if isinstance(wb, dict):
            nb = _normalize_row({"bindings": wb}).get("bindings", {})
            for cid in cards:
                cards[cid].setdefault("bindings", nb)
    types_in = data.get("types")
    if isinstance(types_in, dict):
        clean = {str(k): str(v).strip() for k, v in types_in.items()
                 if isinstance(v, str) and v.strip()}
        if clean:
            out["policy"]["types"] = clean
    pol = data.get("policy")
    if isinstance(pol, dict):
        pt = pol.get("types")
        if isinstance(pt, dict):
            clean = {str(k): str(v).strip() for k, v in pt.items()
                     if isinstance(v, str) and v.strip()}
            if clean:
                out["policy"]["types"] = clean
    out["cards"] = cards
    return out


def load(work_id: str) -> dict[str, Any]:
    """读整张表（v2 归一）。缺/坏 ⇒ 空表。不抛。"""
    for p in (path_for(work_id), _legacy_path_for(work_id)):
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("模型配置表读取失败 work=%s file=%s: %s", work_id, p.name, exc)
            continue
        return _normalize(data, work_id)
    return {"_meta": {"version": 2}, "cards": {}, "policy": {}}


def has_any(work_id: str) -> bool:
    return bool(load(work_id).get("cards"))


def get_row(work_id: str, card_id: str) -> Optional[dict]:
    """取某张卡那一行；没有 ⇒ None（调用方负责按默认策略填）。"""
    if not card_id:
        return None
    row = (load(work_id).get("cards") or {}).get(str(card_id))
    return row if isinstance(row, dict) and row else None


def list_rows(work_id: str) -> dict[str, dict]:
    return dict(load(work_id).get("cards") or {})


def load_types(work_id: str) -> dict[str, str]:
    """项目的按卡类型策略快照（只在建卡时被读）。"""
    t = (load(work_id).get("policy") or {}).get("types")
    return dict(t) if isinstance(t, dict) else {}


# ============================================================
# 写
# ============================================================


def _atomic_write(data: dict[str, Any], work_id: str) -> None:
    """原子落盘（v2 结构）。

    ★ 项目目录**必须已存在**才写：表是项目的附属物，绝不能因为一次「取模型」
      就凭空造出一个 works/{wid}/ 目录（测试/探针用假 work_id 时会污染真实数据，
      实测踩过：work_100 被造出来，后续测试读到脏行）。
    """
    p = path_for(work_id)
    if not p.parent.exists():
        logger.debug("模型配置表跳过写入（项目目录不存在）work=%s", work_id)
        return
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # 迁移完成后旧文件退役（内容已在 v2 里）
    lp = _legacy_path_for(work_id)
    if lp.exists() and lp != p:
        try:
            lp.unlink()
        except OSError:
            pass


def put_row(work_id: str, card_id: str, row: dict, *, src: str = "manual") -> dict:
    """写一行（改这张卡的模型）。src=manual（人改，默认）/ default（策略填的）。"""
    if not card_id:
        raise ValueError("put_row 需要 card_id")
    data = load(work_id)
    clean = _normalize_row(row)
    clean["src"] = "manual" if src == "manual" else "default"
    cards = data.setdefault("cards", {})
    if clean:
        cards[str(card_id)] = clean
    else:
        cards.pop(str(card_id), None)
    data["_meta"] = {"version": 2, "updated_at": _now()}
    _atomic_write(data, work_id)
    return data


def put_rows(work_id: str, rows: dict[str, dict]) -> dict:
    """批量写（一次原子落盘）。"""
    data = load(work_id)
    cards = data.setdefault("cards", {})
    for cid, row in (rows or {}).items():
        clean = _normalize_row(row)
        if clean:
            cards[str(cid)] = clean
        else:
            cards.pop(str(cid), None)
    data["_meta"] = {"version": 2, "updated_at": _now()}
    _atomic_write(data, work_id)
    return data


def update_all_models(work_id: str, model: str) -> dict:
    """项目级改模型：把表里**每一行**的主模型改成它（bindings 保留）。

    [层内兜底 2026-10-21] 换首选时把它从兜底链里剔掉（它已经是首选了），
    其余同层成员保持——兜底仍然是「这一层里的其他模型」，不是全局兜底。
    """
    data = load(work_id)
    m = str(model or "").strip()
    for row in (data.get("cards") or {}).values():
        if row.get("src") == "manual":
            continue          # 人手改过的那一行不动（手动权重最高）
        if m:
            row["model"] = m
            fb = [x for x in (row.get("fallbacks") or []) if x != m]
            if fb:
                row["fallbacks"] = fb
            else:
                row.pop("fallbacks", None)
        else:
            row.pop("model", None)
    data["_meta"] = {"version": 2, "updated_at": _now()}
    _atomic_write(data, work_id)
    return data


def seed_types(work_id: str, template: dict) -> dict:
    """写入项目的按卡类型策略快照（建项目时从全局模板拷贝）。"""
    data = load(work_id)
    clean = {str(k): str(v).strip() for k, v in (template or {}).items()
             if isinstance(v, str) and v.strip()}
    data.setdefault("policy", {})["types"] = clean
    data["_meta"] = {"version": 2, "updated_at": _now()}
    _atomic_write(data, work_id)
    return data


def clear_card(work_id: str, card_id: str) -> dict:
    """删掉某张卡那一行（下次访问会按默认策略重新填）。"""
    data = load(work_id)
    cards = data.get("cards")
    if isinstance(cards, dict):
        cards.pop(str(card_id), None)
    data["_meta"] = {"version": 2, "updated_at": _now()}
    _atomic_write(data, work_id)
    return data


def clear_work(work_id: str) -> dict:
    """清空整张表（所有行与策略快照）。"""
    for p in (path_for(work_id), _legacy_path_for(work_id)):
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    return {"_meta": {"version": 2}, "cards": {}, "policy": {}}


# ============================================================
# 兼容旧接口（settings_api / 老调用方）
# ============================================================


def load_scope(work_id: str, card_id: Optional[str] = None) -> tuple[dict, dict]:
    """兼容：返回 (空 work 级, 该卡那一行)。表里没有 work 级——项目级＝改所有行。"""
    return {}, ((get_row(work_id, card_id) or {}) if card_id else {})


def update_card(work_id: str, card_id: str, override: dict) -> dict:
    """兼容：写一行（override 里的 default_model 归一成 model）。"""
    return put_row(work_id, card_id, override or {})


def update_work(work_id: str, override: dict) -> dict:
    """兼容：项目级改模型＝改表里每一行的主模型（bindings 保留）。"""
    m = (override or {}).get("default_model") or (override or {}).get("model") or ""
    return update_all_models(work_id, m)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ============================================================
# 设置界面的读/写形状（前端沿用旧 JSON 形状，内部已经是表）
# ============================================================


def _clean(override: dict) -> dict:
    """兼容旧调用：把入参归一成 ``{"default_model": ..., "bindings": {...}}``。

    只做形状标准化，不写盘。校验（validate_override）与 put_* 都吃这个形状。
    """
    row = _normalize_row(override or {})
    out: dict[str, Any] = {}
    if row.get("model"):
        out["default_model"] = row["model"]
    if row.get("fallbacks"):
        out["fallbacks"] = row["fallbacks"]
    if row.get("bindings"):
        out["bindings"] = row["bindings"]
    if row.get("exec_backend"):
        out["exec_backend"] = row["exec_backend"]
    return out


def table_summary(work_id: str) -> dict[str, Any]:
    """给设置界面的视图：``{work, cards, types}``。

    - ``cards``：表里每一行 → 旧形状 ``{default_model, bindings}``
    - ``work``：表里所有行主模型**一致**时视为项目默认，否则 None
      （表里没有「work 级」这一层，项目默认只是"所有行都长这样"的呈现）
    """
    data = load(work_id)
    cards_raw = data.get("cards") or {}
    cards: dict[str, dict] = {}
    models = set()
    for cid, row in cards_raw.items():
        item: dict[str, Any] = {}
        if row.get("model"):
            item["default_model"] = row["model"]
            models.add(row["model"])
        if row.get("fallbacks"):
            item["fallbacks"] = row["fallbacks"]      # [层内兜底] 同层其余成员
        if row.get("bindings"):
            item["bindings"] = row["bindings"]
        if row.get("src") == "manual":
            item["src"] = "manual"
        cards[str(cid)] = item
    work = {"default_model": models.pop()} if len(models) == 1 else None
    return {"work": work, "cards": cards, "types": load_types(work_id)}


def project_default(work_id: str) -> Optional[str]:
    """表里所有行一致的那个主模型；不一致/空表 ⇒ None。"""
    w = table_summary(work_id).get("work")
    return (w or {}).get("default_model") if w else None
