# -*- coding: utf-8 -*-
"""条件求值器（[harness 第 2 步 2026-10-28]）。

## 为什么重做

第 1 步把事实收全了，但旧判据只能问一种问题：

    「某类事件在本轮/本卡有没有发生过，且某个**顶层**字段**等于**某个值」

问不了嵌套、问不了数值、问不了次数、问不了先后。而且 `GUARD_KINDS` 这个
白名单**本身就是限制**——它预先规定了"允许问哪几类问题"。

用户口径（2026-10-28）：「条件的组合要更灵活机动，不能有太多限制，这样才能
组合出丰富且智能的控制机制。」故这里不再枚举"允许哪些判据"，改成一套
**通用求值器**：条件写成数据，求值器照数据走。

## 条件是一棵 JSON 树

节点五种，可任意嵌套：

    {"all":  [c, c, ...]}                全部成立
    {"any":  [c, c, ...]}                任一成立
    {"not":  c}                          取反
    {"event": spec}                      spec 选中**至少一条**事件
    {"count": spec, "cmp": op, "value": n}   spec 选中事件的条数满足 op n
    {"field": "a.b", "cmp": op, "value": v}  ctx 里的值满足 op v

``spec``（选事件）三个槽：

    {"types": ["msg_report"], "where": {"kind": "done"}, "within": "turn"}

    types   事件类型集合（空＝不限类型）
    where   字段条件，键支持点号路径（"result.ok"），值见下
    within  turn（本轮）｜card（本卡至今，默认）｜all（同 card）｜{"last": N}

``where`` 的值可以是标量（等于），也可以是带比较器的对象：

    {"eq": x} {"ne": x} {"gt": n} {"ge": n} {"lt": n} {"le": n}
    {"in": [...]} {"contains": s} {"exists": true|false}

值以 ``$`` 开头 ⇒ 取 ctx 字段（例 ``{"from": "$parent"}`` 表示"发件方是本卡父卡"）。

## 与"零墙钟"的关系

条件在**触发那一刻**求值：某条事件已经发生，这时回头问"距上次同类事件多久"。
没有任何东西在等、没有定时器。零墙钟禁的是**由时间驱动系统**（定时唤醒、
超时兜底），不是"拿 ts 当事实查询"。故 `ts` 就是个普通字段，可以用 gt/lt 比。

## 向后兼容

旧写法（`[{"kind":"no_event",...}]`）自动翻译成上面的树，语义逐字不变。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("infra.harness_cond")

#: 比较器（顺序即优先级；未知 op ⇒ 判不成立，宁可少发）
OPS = ("eq", "ne", "gt", "ge", "lt", "le", "in", "contains", "exists", "matches")

#: 符号写法 ⇒ 名字（写配置时 ``">="`` 比 ``"ge"`` 自然，两种都认）
_OP_ALIAS = {
    "==": "eq", "=": "eq", "!=": "ne", "<>": "ne",
    ">": "gt", ">=": "ge", "<": "lt", "<=": "le",
}


def norm_op(raw: Any) -> str:
    """把比较器写法归一成名字（``">="`` → ``"ge"``；未知原样返回）。"""
    s = str(raw or "").strip()
    return _OP_ALIAS.get(s, s)

#: 节点关键字
NODE_KEYS = ("all", "any", "not", "event", "count", "field", "after")


# ---------------------------------------------------------------- 取值
def get_path(obj: Any, path: str) -> Any:
    """按点号路径取值（``"a.b.c"``）。取不到 ⇒ None，绝不抛。"""
    cur = obj
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def _norm(v: Any) -> Any:
    """宽松归一：True/true/"true" 视为同值；数字字符串与数字视为同值。"""
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, (int, float)):
        return ("n", float(v))
    s = str(v).strip()
    low = s.lower()
    if low in ("true", "false"):
        return ("b", low == "true")
    try:
        return ("n", float(s))
    except ValueError:
        return ("s", s)


def compare(actual: Any, op: str, expected: Any) -> bool:
    """按 op 比较。判不出来一律 False（宁可少发，不误发）。

    ``op`` 认两种写法：名字（``ge``）与符号（``>=``）。
    """
    op = norm_op(op)
    if op == "exists":
        want = bool(expected) if not isinstance(expected, str) else \
            str(expected).strip().lower() != "false"
        return (actual is not None) == want
    # 取不到值 ⇒ 判不了。**不许**拿 None 去和字符串比大小（那样 "None" > "2026-…"
    # 会成立，条件会假成立）。
    if actual is None:
        return False
    if op == "in":
        if not isinstance(expected, (list, tuple, set, frozenset)):
            return False
        return any(_norm(actual) == _norm(x) for x in expected)
    if op == "contains":
        if actual is None:
            return False
        if isinstance(actual, (list, tuple, set, frozenset)):
            return any(_norm(x) == _norm(expected) for x in actual)
        return str(expected) in str(actual)
    if op == "matches":
        import re
        try:
            return re.search(str(expected), str(actual or "")) is not None
        except re.error:
            return False
    a, b = _norm(actual), _norm(expected)
    if op == "eq":
        return a == b
    if op == "ne":
        return a != b
    # 数值比较：两边都得是数字
    if a[0] != "n" or b[0] != "n":
        # 允许时间字符串按 ISO 字典序比较（同格式下等价于时间序）
        if a[0] == "s" and b[0] == "s":
            return {"gt": a[1] > b[1], "ge": a[1] >= b[1],
                    "lt": a[1] < b[1], "le": a[1] <= b[1]}.get(op, False)
        return False
    return {"gt": a[1] > b[1], "ge": a[1] >= b[1],
            "lt": a[1] < b[1], "le": a[1] <= b[1]}.get(op, False)


def _resolve(v: Any, ctx: dict[str, Any]) -> Any:
    """值以 ``$`` 开头 ⇒ 取 ctx 字段；否则原样。"""
    if isinstance(v, str) and v.startswith("$"):
        return ctx.get(v[1:])
    return v


# ---------------------------------------------------------------- 选事件
def _turn_window(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """本轮＝从触发事件往前，到**最近一个 run_start**（不含）为止。

    [harness 第 2 步 2026-10-28 · 修窗口] 改前用「跳过第一个 turn_end、遇第二个
    才停」的算法，理由是"触发事件之前应该先有自己的 turn_end"。但生产顺序是
    **done → turn_end**（`done` 在 finish 里发、`turn_end` 在 finally 里发），
    所以 `done` 触发那一刻本轮的 turn_end **还没写**——往前遇到的第一个 turn_end
    其实是**上一轮**的，被当成"自己的"跳过后，窗口就跨进了上一轮。

    实测后果：`scope=turn` 的判据会被上一轮的事实满足（上一轮汇报过 ⇒ 本轮
    判成"已汇报"），该提醒的不提醒。旧测试没抓到，是因为它按 turn_end → done
    的**假顺序**造事件，正好绕过了这个缺陷。

    改用 ``run_start`` 作锚：一轮＝一个 run，顺序上必然在触发事件之前、
    且不受 done/turn_end 先后影响。取不到（窗口被截断）才退回 turn_end 边界。
    """
    evs = list(ctx.get("events") or [])
    start = 0
    if evs and str(evs[0].get("type")) == str(ctx.get("trigger_type") or ""):
        start = 1
    for j in range(start, len(evs)):
        if str(evs[j].get("type")) == "run_start":
            return evs[start:j]
    for j in range(start, len(evs)):
        if str(evs[j].get("type")) == "turn_end":
            return evs[start:j]
    return evs[start:]


def select_events(spec: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """按 spec 选出事件（新→旧序）。"""
    types = {str(t) for t in (spec.get("types") or [])}
    where = spec.get("where") or {}
    within = spec.get("within") or spec.get("scope") or "card"

    evs = list(ctx.get("events") or [])
    if within == "turn":
        evs = _turn_window(ctx)
    elif isinstance(within, dict):
        n = within.get("last")
        if isinstance(n, int) and n >= 0:
            evs = evs[:n]

    out: list[dict[str, Any]] = []
    for e in evs:
        if types and str(e.get("type")) not in types:
            continue
        if where and not match_where(e.get("data"), where, ctx, event=e):
            continue
        out.append(e)
    return out


def match_where(data: Any, where: dict[str, Any], ctx: dict[str, Any],
                event: Optional[dict[str, Any]] = None) -> bool:
    """事件是否满足字段条件（键支持点号路径）。

    [harness 第 2 步 2026-10-28] 先在 ``data`` 里找，找不到再在**事件顶层**找——
    `ts` / `seq` / `round` / `type` 都在顶层。这样「距上次汇报超过两小时」这类
    条件才能写成 ``{"ts": {"lt": …}}``，而不是被当成"字段不存在"。
    """
    d = data if isinstance(data, dict) else {}
    for k, v in (where or {}).items():
        actual = get_path(d, k)
        if actual is None and isinstance(event, dict):
            actual = get_path(event, k)
        if isinstance(v, dict) and any(op in v for op in OPS):
            # 显式比较器；一个对象里可以写多个（and）
            for op in OPS:
                if op in v and not compare(actual, op, _resolve(v[op], ctx)):
                    return False
            continue
        if not compare(actual, "eq", _resolve(v, ctx)):
            return False
    return True


def _matches(event: dict[str, Any], spec: dict[str, Any],
             ctx: dict[str, Any]) -> bool:
    """单条事件是否满足 spec（类型 + 字段条件）。``within`` 忽略——已在窗口内。

    两种形状都要认：事件流里字段在 ``data`` 下；数据集里事实是扁平的
    （字段直接在顶层）。``match_where`` 本身有"先 data 后顶层"的兜底，
    但传进去的 data 必须是那一层。
    """
    types = {str(t) for t in (spec.get("types") or [])}
    if types and str(event.get("type")) not in types:
        return False
    where = spec.get("where") or {}
    if where:
        payload = event.get("data")
        if not isinstance(payload, dict):
            payload = event
        if not match_where(payload, where, ctx, event=event):
            return False
    return True


def eval_after(spec: Any, ctx: dict[str, Any]) -> bool:
    """``{"after": {"anchor": spec, "expect": spec}}`` —— 最近一次 anchor 之后没有 expect。

    为什么必须有它：「干完了没汇报」是**先后关系**，不是"窗口内有没有"。
    用「窗口内没有 report_up」来表达，会让上一轮的汇报把这一轮盖住
    （24 小时内汇报过一次 ⇒ 之后永远不提醒）。旧规则说的是"本轮 done 之后"，
    换成时间窗之后只有先后关系才等价。
    """
    if not isinstance(spec, dict):
        return False
    anchor_spec = spec.get("anchor") or {}
    expect_spec = spec.get("expect") or {}
    if not isinstance(anchor_spec, dict) or not isinstance(expect_spec, dict):
        return False
    evs = list(ctx.get("events") or [])          # 新→旧
    anchor_idx = None
    for i, e in enumerate(evs):
        if _matches(e, anchor_spec, ctx):
            anchor_idx = i
            break
    if anchor_idx is None:
        return False
    for e in evs[:anchor_idx]:                   # 比 anchor 更新的那些
        if _matches(e, expect_spec, ctx):
            return False
    return True


# ---------------------------------------------------------------- 求值
def eval_cond(cond: Any, ctx: dict[str, Any]) -> bool:
    """求值一个条件节点。认不出的形状 ⇒ False（宁可少发，不误发）。"""
    if cond is None:
        return True
    if isinstance(cond, bool):
        return cond
    if not isinstance(cond, dict):
        return False

    if "all" in cond:
        items = cond.get("all") or []
        return all(eval_cond(c, ctx) for c in items) if isinstance(items, list) else False
    if "any" in cond:
        items = cond.get("any") or []
        return any(eval_cond(c, ctx) for c in items) if isinstance(items, list) else False
    if "not" in cond:
        return not eval_cond(cond.get("not"), ctx)
    if "event" in cond:
        spec = cond.get("event")
        if not isinstance(spec, dict):
            return False
        return bool(select_events(spec, ctx))
    if "count" in cond:
        spec = cond.get("count")
        if not isinstance(spec, dict):
            return False
        n = len(select_events(spec, ctx))
        return compare(n, str(cond.get("cmp") or "ge"),
                       cond.get("value", 1))
    if "after" in cond:
        return eval_after(cond.get("after"), ctx)
    if "field" in cond:
        actual = get_path(ctx, str(cond.get("field") or ""))
        return compare(actual, str(cond.get("cmp") or "eq"),
                       _resolve(cond.get("value"), ctx))

    # 旧写法（单条 legacy 判据）也接受，便于混写
    if str(cond.get("kind") or "") in ("no_event", "has_event"):
        return eval_cond(legacy_to_cond(cond), ctx)

    logger.warning("[harness_cond] 认不出的条件节点 ⇒ 不成立: %s", list(cond))
    return False


# ---------------------------------------------------------------- 旧写法翻译
def legacy_to_cond(g: dict[str, Any]) -> dict[str, Any]:
    """把旧的 ``no_event``/``has_event`` 翻译成条件树（语义逐字不变）。"""
    spec: dict[str, Any] = {}
    if g.get("types"):
        spec["types"] = list(g.get("types") or [])
    if g.get("where"):
        spec["where"] = dict(g.get("where") or {})
    spec["within"] = str(g.get("scope") or "turn")
    node = {"event": spec}
    return {"not": node} if str(g.get("kind")) == "no_event" else node


def normalize_guard(guard: Any) -> dict[str, Any]:
    """把规则的 `guard` 归一成一棵条件树。

    三种输入都接受：
      · ``None`` / ``[]``        ⇒ 恒真
      · list（旧写法或混合）      ⇒ ``{"all": [...]}``（每项各自归一）
      · dict（新写法）           ⇒ 原样
    """
    if guard is None:
        return {"all": []}
    if isinstance(guard, list):
        items = []
        for g in guard:
            if isinstance(g, dict) and str(g.get("kind") or "") in ("no_event", "has_event"):
                items.append(legacy_to_cond(g))
            else:
                items.append(g)
        return {"all": items}
    return guard


def validate_cond(cond: Any, *, path: str = "guard") -> list[str]:
    """结构校验（不枚举"允许哪些判据"，只查"这个节点说不说得清"）。

    返回错误清单（空＝合法）。
    """
    errs: list[str] = []
    if cond is None or isinstance(cond, bool):
        return errs
    if not isinstance(cond, dict):
        return [f"{path}: 条件必须是对象"]
    if str(cond.get("kind") or "") in ("no_event", "has_event"):
        # 旧写法：只查 types 与 scope
        if not cond.get("types"):
            errs.append(f"{path}: {cond.get('kind')} 缺 types")
        if str(cond.get("scope") or "turn") not in ("turn", "card"):
            errs.append(f"{path}: scope 非法（turn|card）")
        if cond.get("where") is not None and not isinstance(cond.get("where"), dict):
            errs.append(f"{path}: where 必须是对象")
        return errs

    # 写了 kind 但不在旧写法之列 ⇒ 报"判据 kind 非法"（沿用旧措辞，便于老规则表
    # 一眼看懂），并指出新的写法。**必须先判 kind**：旧判据 `field_empty` 带着
    # 一个 `field` 键，若不先拦，会被当成新写法里的 `field` 节点放过去。
    if "kind" in cond:
        return [f"{path}: 判据 kind 非法: {cond.get('kind')!r}"
                f"（旧写法只允许 no_event/has_event；"
                f"复杂条件请用 {'/'.join(NODE_KEYS)}）"]

    keys = [k for k in NODE_KEYS if k in cond]
    if not keys:
        return [f"{path}: 认不出的条件节点（需含 {'/'.join(NODE_KEYS)} 之一）"]
    for k in ("all", "any"):
        if k in cond:
            v = cond.get(k)
            if not isinstance(v, list):
                errs.append(f"{path}.{k} 必须是列表")
            else:
                for i, c in enumerate(v):
                    errs += validate_cond(c, path=f"{path}.{k}[{i}]")
    if "not" in cond:
        errs += validate_cond(cond.get("not"), path=f"{path}.not")
    if "event" in cond:
        spec = cond.get("event")
        if not isinstance(spec, dict):
            errs.append(f"{path}.event 必须是对象")
        else:
            _validate_spec(spec, f"{path}.event", errs)
    if "after" in cond:
        spec = cond.get("after")
        if not isinstance(spec, dict):
            errs.append(f"{path}.after 必须是对象")
        else:
            for k in ("anchor", "expect"):
                s = spec.get(k)
                if not isinstance(s, dict):
                    errs.append(f"{path}.after.{k} 必须是对象")
                else:
                    _validate_spec(s, f"{path}.after.{k}", errs)
    if "count" in cond:
        spec = cond.get("count")
        if not isinstance(spec, dict):
            errs.append(f"{path}.count 必须是对象")
        else:
            _validate_spec(spec, f"{path}.count", errs)
        if norm_op(cond.get("cmp") or "ge") not in OPS:
            errs.append(f"{path}.count.cmp 非法")
        if "value" not in cond:
            errs.append(f"{path}.count 缺 value")
    if "field" in cond:
        if not str(cond.get("field") or "").strip():
            errs.append(f"{path}.field 不能为空")
        if norm_op(cond.get("cmp") or "eq") not in OPS:
            errs.append(f"{path}.field.cmp 非法")
    return errs


def _validate_spec(spec: dict[str, Any], path: str, errs: list[str]) -> None:
    types = spec.get("types")
    if types is not None and not isinstance(types, list):
        errs.append(f"{path}.types 必须是列表")
    where = spec.get("where")
    if where is not None and not isinstance(where, dict):
        errs.append(f"{path}.where 必须是对象")
    within = spec.get("within", spec.get("scope"))
    if within is not None and not isinstance(within, (str, dict)):
        errs.append(f"{path}.within 必须是字符串或 {{last:N}}")
    elif isinstance(within, str) and within not in ("turn", "card", "all"):
        errs.append(f"{path}.within 非法: {within!r}（turn|card|all）")
