# -*- coding: utf-8 -*-
"""系统能力清单的读取与校验（[能力清单 2026-10-30 · 基础设施]）。

## 它是什么

``config/capabilities.json`` 是系统级事实源：系统里有哪些**能力**、每项能力由什么
实现、能挂到哪类对象上。本模块是它的唯一读取入口，不写库、不改运行时状态。

**能力不是工具。** 工具是能力的一种实现方式（``impl.kind="tools"``）。将来为某类
对象新造的能力，可以在 ``impl`` 里声明别的实现方式，不必先造一件工具。

## 三个集合，互斥且穷尽

    capabilities[].impl.tools   是能力，挂在对象上就能用
    not_capabilities[]          是工具但不是能力（内部实现 / 机械兜底 / 系统自用）
    pending[]                   尚未归类（迁移用，只减不增）

任何一件已注册工具必须恰好落在其中一个集合里。守护见
``tests/test_capability_registry.py``：漏了红、重复了红、指向不存在的工具红。

## 挂载怎么生效

对象上存一组能力 code（``function_profiles.abilities``）。下发面＝挂载 ∩ 执行层
判据——**执行层判据不在本文件里**，仍由各工具 handler 判（子树、连线、正稿等）。
故挂载是"允许它做"，不是"一定成功"。

## fail-open

文件读不到 / 解析失败 / 结构不合法 ⇒ ``enabled=False``。调用方一律按"没有清单"
处理：挂载不生效，工具面按既有路径走。清单是新增的一层，它坏了不能把系统带坏。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger("infra.capability_registry")

#: 环境变量覆盖（测试指向临时文件；与 FUNCTION_CATALOG_PATH 同法）
ENV_PATH = "CAPABILITIES_PATH"

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "capabilities.json"

#: 允许出现在 ``applies_to`` 里的对象种类。加新对象种类＝在这里加一个值。
OBJECT_KINDS: frozenset[str] = frozenset({"function", "card"})

#: 已知的实现方式。加新方式＝在这里加一个值（能力不必是工具）。
IMPL_KINDS: frozenset[str] = frozenset({"tools"})

_cache: dict[str, Any] = {"at": None, "data": None}


def reset_cache() -> None:
    """清缓存（测试用；改清单文件后也调它）。"""
    _cache["at"] = None
    _cache["data"] = None


def path() -> Path:
    env = os.environ.get(ENV_PATH, "").strip()
    return Path(env) if env else _DEFAULT_PATH


def _empty(reason: str) -> dict[str, Any]:
    return {"enabled": False, "reason": reason,
            "capabilities": [], "not_capabilities": [], "pending": []}


def load(force: bool = False) -> dict[str, Any]:
    """读清单（带 mtime 缓存）。**fail-open**：读不到/坏文件 ⇒ ``enabled=False``。"""
    p = path()
    try:
        at = (p.stat().st_mtime_ns, str(p))
    except OSError:
        reset_cache()
        return _empty("file_missing")
    if not force and _cache["at"] == at and _cache["data"] is not None:
        return _cache["data"]
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 坏文件＝没有清单
        logger.warning("[capability] 清单读取失败 %s: %s", p, exc)
        reset_cache()
        return _empty(f"parse_error: {exc}")
    if not isinstance(raw, dict):
        return _empty("not_an_object")
    caps = raw.get("capabilities")
    caps = caps if isinstance(caps, list) else []
    data = {
        "enabled": True,
        "reason": "",
        "version": int((raw.get("_meta") or {}).get("version") or 0),
        "capabilities": [c for c in caps if isinstance(c, dict)],
        "not_capabilities": [x for x in (raw.get("not_capabilities") or [])
                             if isinstance(x, dict)],
        "pending": [str(x) for x in (raw.get("pending") or []) if str(x).strip()],
    }
    _cache["at"] = at
    _cache["data"] = data
    return data


# ---------------------------------------------------------------- 查询
def capabilities() -> list[dict[str, Any]]:
    return list(load().get("capabilities") or [])


def codes() -> list[str]:
    return [str(c.get("code") or "") for c in capabilities() if c.get("code")]


def entry(code: str) -> Optional[dict[str, Any]]:
    for c in capabilities():
        if str(c.get("code") or "") == str(code or ""):
            return c
    return None


def impl_tools(cap: dict[str, Any]) -> list[str]:
    """一项能力的工具实现。``impl.kind`` 不是 tools ⇒ 空（该能力不靠工具实现）。"""
    impl = cap.get("impl") or {}
    if str(impl.get("kind") or "") != "tools":
        return []
    return [str(t) for t in (impl.get("tools") or []) if str(t).strip()]


def tools_of(mounts: Iterable[str]) -> set[str]:
    """挂载集合 → 工具集合。未知 code 忽略（脏挂载不炸，也不放大）。"""
    out: set[str] = set()
    for code in mounts or []:
        cap = entry(code)
        if cap is not None:
            out.update(impl_tools(cap))
    return out


def mountable(code: str, kind: str) -> bool:
    """这项能力能不能挂到 ``kind`` 这类对象上。"""
    cap = entry(code)
    if cap is None:
        return False
    return str(kind or "") in [str(k) for k in (cap.get("applies_to") or [])]


def filter_mounts(mounts: Iterable[str], kind: str) -> list[str]:
    """按对象种类收敛挂载：不存在的 code、不适用的对象、重复项都剔掉（保序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for code in mounts or []:
        c = str(code or "")
        if not c or c in seen or not mountable(c, kind):
            continue
        seen.add(c)
        out.append(c)
    return out


def pending() -> list[str]:
    return list(load().get("pending") or [])


def not_capabilities() -> list[dict[str, Any]]:
    return list(load().get("not_capabilities") or [])


# ---------------------------------------------------------------- 校验
def validate(tool_names: Iterable[str]) -> list[str]:
    """清单与注册表的一致性检查。返回问题清单（空＝通过），**不抛**。

    调用方：守护测试（不通过即红）、运维排查（可随时跑一遍看清单烂在哪）。
    """
    problems: list[str] = []
    data = load()
    if not data.get("enabled"):
        problems.append(f"清单不可用：{data.get('reason')}")
        return problems

    known = {str(t) for t in tool_names}
    seen_codes: set[str] = set()
    seen_tools: dict[str, str] = {}

    for cap in data.get("capabilities") or []:
        code = str(cap.get("code") or "")
        if not code:
            problems.append("能力缺 code")
            continue
        if code in seen_codes:
            problems.append(f"能力 code 重复：{code}")
        seen_codes.add(code)
        if not str(cap.get("name") or "").strip():
            problems.append(f"{code}：缺 name")
        kinds = cap.get("applies_to")
        if not isinstance(kinds, list) or not kinds:
            problems.append(f"{code}：applies_to 必须是非空列表")
        else:
            for k in kinds:
                if str(k) not in OBJECT_KINDS:
                    problems.append(f"{code}：applies_to 含未知对象种类 {k!r}")
        impl = cap.get("impl")
        if not isinstance(impl, dict) or not impl:
            problems.append(f"{code}：缺 impl")
            continue
        kind = str(impl.get("kind") or "")
        if kind not in IMPL_KINDS:
            problems.append(f"{code}：impl.kind 未知 {kind!r}")
            continue
        if kind == "tools":
            tools = impl_tools(cap)
            if not tools:
                problems.append(f"{code}：impl.kind=tools 但 tools 为空")
            for t in tools:
                if t not in known:
                    problems.append(f"{code}：tools 指向不存在的工具 {t}")
                prev = seen_tools.get(t)
                if prev is not None:
                    problems.append(f"工具 {t} 同时属于两项能力：{prev} / {code}")
                else:
                    seen_tools[t] = code

    for row in data.get("not_capabilities") or []:
        t = str(row.get("tool") or "")
        if not t:
            problems.append("not_capabilities 有一条缺 tool")
            continue
        if t not in known:
            problems.append(f"not_capabilities 指向不存在的工具 {t}")
        if not str(row.get("reason") or "").strip():
            problems.append(f"not_capabilities[{t}]：缺 reason")
        if t in seen_tools:
            problems.append(f"工具 {t} 既在能力 {seen_tools[t]} 里又在排除名单里")

    for t in data.get("pending") or []:
        if t not in known:
            problems.append(f"pending 里有不存在的工具 {t}")
        if t in seen_tools:
            problems.append(f"工具 {t} 已归入能力 {seen_tools[t]}，不该还在 pending")
        if any(str(r.get("tool") or "") == t for r in data.get("not_capabilities") or []):
            problems.append(f"工具 {t} 已在 not_capabilities，不该还在 pending")

    covered = set(seen_tools)
    covered.update(str(r.get("tool") or "") for r in data.get("not_capabilities") or [])
    covered.update(data.get("pending") or [])
    for t in sorted(known - covered):
        problems.append(f"工具 {t} 未归类：既不在任何能力里，也不在排除名单，也不在 pending")

    return problems


# ---------------------------------------------------------------- 模式清单
# 一个模式一张能力配置清单：config/modes/<code>.json
# 结构：{"code","name","cards":{卡型:[能力 code...]}}——值是**上限**。
MODES_DIR = _DEFAULT_PATH.parent / "modes"

_mode_cache: dict[str, Any] = {}


def modes_dir() -> Path:
    env = os.environ.get("CAPABILITIES_MODES_DIR", "").strip()
    return Path(env) if env else MODES_DIR


def mode_codes() -> list[str]:
    try:
        return sorted(p.stem for p in modes_dir().glob("*.json"))
    except OSError:
        return []


def load_mode(code: str) -> Optional[dict[str, Any]]:
    """读一个模式清单（带 mtime 缓存）。读不到/坏文件 ⇒ None（调用方回退旧路径）。"""
    name = str(code or "").strip()
    if not name:
        return None
    p = modes_dir() / f"{name}.json"
    try:
        at = (p.stat().st_mtime_ns, str(p))
    except OSError:
        return None
    hit = _mode_cache.get(name)
    if hit and hit.get("at") == at:
        return hit.get("data")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 坏文件＝没有这个模式
        logger.warning("[capability] 模式清单读取失败 %s: %s", p, exc)
        return None
    if not isinstance(data, dict):
        return None
    _mode_cache[name] = {"at": at, "data": data}
    return data


def mode_upper(code: str, card_type: str) -> Optional[list[str]]:
    """这个模式下，这类卡**最多**能挂哪些能力。模式或卡型没配 ⇒ None。"""
    data = load_mode(code)
    if not data:
        return None
    cards = data.get("cards") or {}
    if not isinstance(cards, dict):
        return None
    row = cards.get(str(card_type or ""))
    if not isinstance(row, list):
        return None
    return [str(x) for x in row]


def abilities_for(perms: Iterable[str], mode: str, card_type: str) -> list[str]:
    """建卡时算这张卡默认挂哪些能力＝**模式上限 ∩ 这张卡的 perms 覆盖到的能力**。

    为什么还要跟 perms 取交集：能力是岗位配置，权限是这张卡**最多**能碰什么。
    建卡那一刻 perms 已经算好（`policy.compute_perms`），所以挂载不能超过它——
    否则等于"建卡就自授了权限"。

    perms 的判定走 ``policy.has_perm``（同一套隐含规则，``tools_file`` ⇒ 具备
    ``tools_read_file``）；裸查列表会让 coder 卡漏掉「看文件」。

    清单/模式读不到 ⇒ 空列表（调用方据此保持未迁移，走旧路径）。
    """
    from ..permissions import policy

    upper = mode_upper(mode, card_type)
    if not upper:
        return []
    meta = {policy.PERM_KEY: list(perms or [])}
    out: list[str] = []
    for code in upper:
        cap = entry(code)
        if cap is None:
            continue
        sw = str((cap.get("impl") or {}).get("switch") or "")
        if not sw or policy.has_perm(meta, sw):
            out.append(code)
    return out


def reset_mode_cache() -> None:
    _mode_cache.clear()
