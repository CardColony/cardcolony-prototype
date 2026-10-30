# -*- coding: utf-8 -*-
"""系统消息规则层（harness 规则层）：可任意增删条目，热加载，逐条校验。

[基础设施 2026-10-26 · 用户裁定「先做规则层吧，规则层应该是可以任意加减条目进去的」]

## 定位

事件记录层（``event_hub`` / ``work_events``）回答「发生了什么」；本模块回答
**「什么情景该提醒谁、说什么」**。执行（评估与投递）在 ``harness`` 引擎里，
本模块只负责：**规则的形状、加载、校验、按事件类型索引**。

## 可增删

- 规则表＝``config/harness_rules.json``，**每次现读**（改文件即生效，无需重启）；
- 坏条目**只标记 invalid 并告警**，不拖垮整表、不影响其他规则；
- ``enabled=false`` 保留配置但停用，便于试跑与回滚。

## 判据：条件写成数据，不用墙钟

项目铁则（``review_batches`` 同款）：「不要设墙钟，全部都用状态判断」。

[harness 第 2 步 2026-10-28] 判据交给通用求值器（``harness_cond``），条件是
``all/any/not/event/count/field`` 的任意嵌套树——能问嵌套字段、数值比较、
出现次数、先后、组合。**"不用墙钟"的边界因此要说清**：

- 禁的是**由时间驱动系统**：定时唤醒、超时兜底、轮询补漏；
- 不禁**把 ts 当事实查询**：条件在触发那一刻求值，没有任何东西在等。
  所以 ``ts`` 就是普通字段，可以用 gt/lt 比较（例："距上次汇报超过两小时"）。

## 「难以判断的不发」

不靠运行时猜，靠**规则表的保守**：判不出来的情景就不写规则，自然不发。
新增情景＝新增一条规则，不是改引擎。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("infra.harness_rules")

#: 规则表路径（测试/沙箱可重定向）
ENV_RULES_PATH = "ECO_HARNESS_RULES_PATH"
_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "harness_rules.json")

#: 合法判据类型
#: [2026-10-26 · 用户令「系统消息只允许跟事件挂钩，不允许用任意其他判断逻辑」]
#: **只有事件类判据**。曾经有过的 card_state / field_empty / field_not_empty /
#: child_count 全部删除——它们读的是卡的状态快照，不是事件，属于"另造判断逻辑"。
#: 旧的两种事件判据（**已不再是白名单**）。
#: [harness 第 2 步 2026-10-28] 改前这里是「允许哪些 kind」的枚举，等于预先规定
#: 了"允许问哪几类问题"——那本身就是限制。现在判据交给通用求值器
#: （`harness_cond`），条件写成 all/any/not/event/count/field 的任意嵌套树；
#: 下面两个 kind 仍然合法（旧写法照样跑），只是不再垄断"能问什么"。
GUARD_KINDS = frozenset({"no_event", "has_event"})

#: 合法目标
TARGETS = frozenset({"self", "parent", "ancestors", "links"})

_cache: Optional[dict[str, Any]] = None
_cache_mtime: float = 0.0


# ---------------------------------------------------------------- 校验
def validate_rule(rule: dict[str, Any]) -> list[str]:
    """校验一条规则，返回错误清单（空＝合法）。"""
    errs: list[str] = []
    if not isinstance(rule, dict):
        return ["规则不是对象"]
    rid = str(rule.get("id") or "").strip()
    if not rid:
        errs.append("缺 id")
    if not str(rule.get("message") or "").strip():
        errs.append("缺 message")

    trig = rule.get("trigger") or {}
    if not isinstance(trig, dict):
        errs.append("trigger 不是对象")
        trig = {}
    types = trig.get("types") or []
    if not isinstance(types, list) or not types:
        errs.append("trigger.types 必须是非空列表")
    else:
        from . import event_hub
        catalog = event_hub.load_catalog()
        for t in types:
            if t not in catalog:
                errs.append(f"trigger.types 含未登记事件类型: {t}")
            elif not catalog[t].get("triggerable"):
                errs.append(f"trigger.types 含不可触发类型: {t}"
                            f"（在 event_catalog.json 里置 triggerable=true 才可用）")

    cts = trig.get("card_types") or []
    if cts and not isinstance(cts, list):
        errs.append("trigger.card_types 必须是列表或留空")

    target = str(rule.get("target") or "").strip()
    if not target:
        errs.append("缺 target")
    elif target not in TARGETS and not target.startswith("card:"):
        errs.append(f"target 非法: {target}（只允许 {'/'.join(sorted(TARGETS))} 或 card:<id>）")

    guard = rule.get("guard")
    if guard is not None:
        # [harness 第 2 步 2026-10-28] 判据不再枚举"允许哪几种 kind"——改由通用
        # 求值器校验结构（`harness_cond.validate_cond`）。旧写法（no_event /
        # has_event）照样合法，混写也行。用户口径：「不能有太多限制」。
        from . import harness_cond
        errs += harness_cond.validate_cond(
            harness_cond.normalize_guard(guard), path="guard")
    return errs


# ---------------------------------------------------------------- 加载
def load_rules(*, include_invalid: bool = False) -> list[dict[str, Any]]:
    """读规则表并逐条校验。

    - 合法条目原样返回（附 ``_errors: []``）；
    - 坏条目：``include_invalid=True`` 时返回并附 ``_errors``，否则丢弃；
    - 文件缺失/坏 JSON ⇒ 返回空表并告警（fail-open，不挡主流程）。
    """
    global _cache, _cache_mtime
    path = os.environ.get(ENV_RULES_PATH) or _DEFAULT_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        logger.warning("[harness_rules] 规则表不存在: %s（视为空表）", path)
        return []
    if _cache is not None and mtime == _cache_mtime and not include_invalid:
        return list(_cache.get("rules") or [])
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:  # noqa: BLE001 坏表 ⇒ 空表，不挡主流程
        logger.warning("[harness_rules] 规则表读取失败 %s: %s", path, exc)
        return []

    raw_rules = raw.get("rules") or []
    if not isinstance(raw_rules, list):
        logger.warning("[harness_rules] rules 不是列表，视为空表")
        raw_rules = []

    good: list[dict[str, Any]] = []
    bad: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in raw_rules:
        errs = validate_rule(r)
        rid = str((r or {}).get("id") or "") if isinstance(r, dict) else ""
        if rid and rid in seen:
            errs = errs + [f"id 重复: {rid}"]
        if rid:
            seen.add(rid)
        item = dict(r) if isinstance(r, dict) else {"_raw": r}
        item["_errors"] = errs
        if errs:
            bad.append(item)
            logger.warning("[harness_rules] 规则非法 id=%s: %s", rid or "?", "; ".join(errs))
        else:
            good.append(item)

    _cache = {"rules": good, "invalid": bad, "path": path}
    _cache_mtime = mtime
    if include_invalid:
        return good + bad
    return list(good)


def invalid_rules() -> list[dict[str, Any]]:
    """最近一次加载里被判非法的条目（诊断用）。"""
    load_rules()
    return list((_cache or {}).get("invalid") or [])


def reset_cache() -> None:
    global _cache, _cache_mtime
    _cache = None
    _cache_mtime = 0.0


# ---------------------------------------------------------------- 索引
def rules_for(ev_type: str) -> list[dict[str, Any]]:
    """取该事件类型下的候选规则（enabled 且 trigger.types 命中）。"""
    out = []
    for r in load_rules():
        if not bool(r.get("enabled", True)):
            continue
        types = (r.get("trigger") or {}).get("types") or []
        if ev_type in types:
            out.append(r)
    return out


def all_rules() -> list[dict[str, Any]]:
    """全部规则（含停用；供管理界面/诊断）。"""
    return load_rules()


def summary() -> dict[str, Any]:
    """一句话体检：总数 / 启用数 / 非法数 / 覆盖的事件类型。"""
    rules = load_rules(include_invalid=True)
    good = [r for r in rules if not r.get("_errors")]
    bad = [r for r in rules if r.get("_errors")]
    types: set[str] = set()
    for r in good:
        types.update((r.get("trigger") or {}).get("types") or [])
    return {
        "total": len(rules),
        "enabled": sum(1 for r in good if bool(r.get("enabled", True))),
        "invalid": len(bad),
        "trigger_types": sorted(types),
    }
