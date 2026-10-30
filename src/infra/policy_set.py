# -*- coding: utf-8 -*-
"""条件集：值班员与小本子（[2026-10-28 用户定规格 · 第二个东西]）。

## 它是什么

用户口径（2026-10-28）：

> 一个是条件集，数据集根据条件集的【激活状态的策略】来进行状态记录，
> 一旦出现符合策略的条件，就触发 harness 任务。

用大白话说：

- **数据集**是一本按时间排好的台账，谁在什么时候对谁做了什么，一查就有；
- **条件集**是一排值班员，每条策略一个。值班员不去翻台账——事情一发生就送到他手上，
  他往自己的小本子上记一笔。账凑够条件就喊人，喊完把本子撕了重记。

## 三个关键设计

**一、本子是「每个作用域一份」，不是「每条策略一份」。**

    scope.kind = "card"       一张卡一本
    scope.kind = "relation"   **一对父子**一本   ← 跨卡的关键
    scope.kind = "work"       整个 work 一本

「父卡对子卡做了什么」的状态不是挂在某一张卡上，而是挂在每一对 (父, 子) 上。
新子卡建出来，自动多一本空本子，不需要谁去登记。

**二、只记自己关心的那几类事。**

``watch`` 声明关心哪几类事件。事件进来时先按类型找关心它的值班员（倒排），
其余值班员连看都不看一眼。这是「N 条策略不会全跑」的来源。

**三、喊完就撕本子重记。**

触发后该作用域的状态归零、窗口重置到此刻。只撕那一本，别的本子不动。
清零本身防了重复触发，所以原来的防重表可以退役。

## 本子里记什么

    window_start  窗口左边界
    entries       窗口内的事实（按时间升序）
    counters      各类事件次数
    last          各类事件最近一次的时刻

超窗口的老账**惰性**划掉——下次记账时顺手剔，没有事件时本子不动也不需要动
（这正好就是零墙钟要的样子）。

## 和数据集的分工

    事件进来 → 数据集写库（冷，落盘）→ 条件集更新本子（热，内存）
    新建策略 → 从数据集回溯一次，把每本本子补记好，之后靠增量

热状态在内存，冷事实在 sqlite。重启时本子清空，从数据集回溯重建。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("infra.policy_set")

#: 时间窗写法：24h / 7d / 90m / 45s / none（不限）
_WINDOW_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.I)
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}

#: 合法的作用域类型
SCOPE_KINDS = ("card", "relation", "work")

#: 触发「卡树变了」的事件类型（用于让父卡缓存失效）
_TREE_EVENTS = frozenset({"card_created", "card_deleted", "card_archived",
                          "card_restored", "card_changed"})


def parse_window(raw: Any) -> Optional[float]:
    """``"24h"`` / ``"7d"`` / ``"90m"`` / ``"none"`` ⇒ 秒（None＝不限）。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    s = str(raw).strip().lower()
    if not s or s in ("none", "all", "0"):
        return None
    m = _WINDOW_RE.match(s)
    if not m:
        raise ValueError(f"时间窗写法非法: {raw!r}（要 24h / 7d / 90m / none）")
    return float(m.group(1)) * _UNIT[m.group(2).lower()]


# ---------------------------------------------------------------- 策略
@dataclass
class Policy:
    """一条策略（一个值班员）。"""

    id: str
    active: bool = True
    desc: str = ""
    scope_kind: str = "card"
    scope: dict[str, Any] = field(default_factory=dict)
    watch: list[str] = field(default_factory=list)
    window: str = "none"
    when: Any = None
    then: dict[str, Any] = field(default_factory=dict)
    #: 只对某个 work 生效；空串＝全局（所有 work 都挂）。根卡给自己项目配的策略填它。
    work: str = ""

    @property
    def window_sec(self) -> Optional[float]:
        return parse_window(self.window)

    @property
    def watches_all(self) -> bool:
        """``watch`` 为空 ＝ 所有事件都关心（配错了会很吵，但语义明确）。"""
        return not self.watch


# ---------------------------------------------------------------- 本子
class State:
    """一个作用域实例的小本子。"""

    __slots__ = ("policy_id", "scope_key", "window_start", "entries",
                 "counters", "last")

    def __init__(self, policy_id: str, scope_key: tuple[str, ...],
                 now: float) -> None:
        self.policy_id = policy_id
        self.scope_key = scope_key
        self.window_start = now
        self.entries: list[dict[str, Any]] = []
        self.counters: dict[str, int] = {}
        self.last: dict[str, float] = {}

    # -------------------------------------------------- 记账
    def add(self, fact: dict[str, Any], window_sec: Optional[float],
            now: float) -> None:
        """记一笔，顺手划掉超窗口的老账。

        **保持 entries 升序**（`prune` 只从头部剔，靠的就是这个前提）：
        正常情况事件按发生顺序到达，直接追加；**迟到的旧事件**（补历史、跨进程
        乱序）必须插到正确位置——否则它滞留在尾部，`prune` 永远划不到它，
        窗口就失效了（它会被永久计入 count）。
        """
        t = fact.get("_t")
        if (self.entries and isinstance(t, (int, float))
                and isinstance(self.entries[-1].get("_t"), (int, float))
                and t < self.entries[-1]["_t"]):
            import bisect
            keys = [e.get("_t") or 0 for e in self.entries]
            self.entries.insert(bisect.bisect_right(keys, t), fact)
        else:
            self.entries.append(fact)
        ev = str(fact.get("type") or "")
        self.counters[ev] = self.counters.get(ev, 0) + 1
        if isinstance(t, (int, float)):
            self.last[ev] = float(t)
        if window_sec is not None:
            self.prune(now - window_sec)

    def prune(self, cutoff: float) -> int:
        """剔掉 ``_t < cutoff`` 的账（惰性淘汰）。返回剔掉条数。

        **只从头部剔**：本子是按时间追加的（事件按发生顺序进来），超窗的必然在
        最前面。改前是「遍历整个本子重建列表」——长窗口下每记一笔都要扫全表，
        实测 7 天窗 + 24 万条事件会直接跑不完（O(n²)）。
        """
        entries = self.entries
        n = 0
        total = len(entries)
        while n < total:
            t = entries[n].get("_t")
            if isinstance(t, (int, float)) and t < cutoff:
                n += 1
                continue
            break
        if not n:
            return 0
        dropped: dict[str, int] = {}
        for f in entries[:n]:
            ev = str(f.get("type") or "")
            dropped[ev] = dropped.get(ev, 0) + 1
        self.entries = entries[n:]
        for ev, cnt in dropped.items():
            left = self.counters.get(ev, 0) - cnt
            if left > 0:
                self.counters[ev] = left
            else:
                self.counters.pop(ev, None)
                lt = self.last.get(ev)
                if isinstance(lt, (int, float)) and lt < cutoff:
                    self.last.pop(ev, None)
        if self.entries:
            t0 = self.entries[0].get("_t")
            if isinstance(t0, (int, float)):
                self.window_start = max(self.window_start, t0)
        return n

    def clear(self, now: float) -> None:
        """撕本子重记（触发后调）。只清这一本。"""
        self.window_start = now
        self.entries = []
        self.counters = {}
        self.last = {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "policy": self.policy_id,
            "scope": list(self.scope_key),
            "entries": len(self.entries),
            "counters": dict(self.counters),
            "last": dict(self.last),
            "window_start": self.window_start,
        }


# ---------------------------------------------------------------- 条件集
class PolicySet:
    """管理所有值班员与它们的小本子。"""

    def __init__(self, *, parent_provider: Optional[Callable[[str, str], str]] = None,
                 now_fn: Callable[[], float] = time.time) -> None:
        self._policies: dict[str, Policy] = {}
        self._states: dict[tuple[str, str, tuple[str, ...]], State] = {}
        self._by_watch: dict[str, set[str]] = {}
        self._wildcard: set[str] = set()
        self._parent_provider = parent_provider
        self._parents: dict[str, dict[str, str]] = {}
        self._now = now_fn
        self._fired = 0

    # ------------------------------------------------------------ 加载
    def load(self, policies: Iterable[dict[str, Any]]) -> list[str]:
        """加载并校验策略表。返回错误清单（空＝全部合法）。

        坏条目**不拖垮整表**：标 ``active=False`` 并记错误，其余照常工作。
        """
        self._policies.clear()
        self._by_watch.clear()
        self._wildcard.clear()
        errs: list[str] = []
        seen: set[str] = set()
        for i, raw in enumerate(policies or []):
            if not isinstance(raw, dict):
                errs.append(f"[{i}] 不是对象")
                continue
            pid = str(raw.get("id") or "").strip()
            if not pid:
                errs.append(f"[{i}] 缺 id")
                continue
            if pid in seen:
                errs.append(f"{pid}: id 重复")
                continue
            seen.add(pid)
            p, e = self._build(raw)
            if e:
                errs.extend(f"{pid}: {x}" for x in e)
                continue
            self._policies[pid] = p
            if p.active:
                self._index(p)
        return errs

    def _build(self, raw: dict[str, Any]) -> tuple[Policy, list[str]]:
        errs: list[str] = []
        pid = str(raw.get("id") or "").strip()

        scope = raw.get("scope") if isinstance(raw.get("scope"), dict) else {}
        kind = str(scope.get("kind") or "card").strip()
        if kind not in SCOPE_KINDS:
            errs.append(f"scope.kind 非法: {kind!r}（要 {'/'.join(SCOPE_KINDS)}）")

        watch = raw.get("watch") or []
        if not isinstance(watch, list):
            errs.append("watch 必须是列表")
            watch = []
        watch = [str(x) for x in watch if str(x).strip()]

        window = raw.get("window", "none")
        try:
            parse_window(window)
        except ValueError as exc:
            errs.append(str(exc))

        when = raw.get("when")
        if when is not None:
            from . import harness_cond
            errs.extend(harness_cond.validate_cond(when, path="when"))

        then = raw.get("then") if isinstance(raw.get("then"), dict) else {}
        if not then:
            errs.append("缺 then（触发后做什么）")

        p = Policy(
            id=pid,
            active=bool(raw.get("active", True)),
            desc=str(raw.get("desc") or ""),
            scope_kind=kind,
            scope=dict(scope),
            watch=watch,
            window=str(window),
            when=when,
            then=dict(then),
            work=str(raw.get("work") or "").strip(),
        )
        return p, errs

    def _index(self, p: Policy) -> None:
        if p.watches_all:
            self._wildcard.add(p.id)
            return
        for t in p.watch:
            self._by_watch.setdefault(t, set()).add(p.id)

    def reload(self) -> list[str]:
        """从配置文件重读（改文件即生效，不必重启）。"""
        from . import policy_config
        errs = self.load(policy_config.load_raw())
        self._gc_states()
        return errs

    def _gc_states(self) -> int:
        """丢掉「策略已不存在或已停用」的账本，返回清掉的账本数。

        [2026-10-28 · 子 agent 盲测抓到] 改前 `load` 只换策略表、**不动账本**：
        一条策略被删掉之后，它那批账本（每个 scope 实例一本）会永远留在
        `_states` 里——没人再喂它、也没人清它，纯占内存。
        账本不回收本身是有意的（重新启用要能接着攒），但**策略没了就该清**。
        """
        alive = {pid for pid, p in self._policies.items() if p.active}
        dead = [k for k in self._states if k[1] not in alive]
        for k in dead:
            del self._states[k]
        return len(dead)

    # ------------------------------------------------------------ 激活
    def activate(self, pid: str) -> bool:
        p = self._policies.get(pid)
        if p is None or p.active:
            return False
        p.active = True
        self._index(p)
        return True

    def deactivate(self, pid: str) -> bool:
        """停用：摘掉它的所有本子，不再喂事件（别的策略不受影响）。"""
        p = self._policies.get(pid)
        if p is None or not p.active:
            return False
        p.active = False
        self._wildcard.discard(pid)
        for t in list(self._by_watch):
            self._by_watch[t].discard(pid)
            if not self._by_watch[t]:
                self._by_watch.pop(t, None)
        for k in [k for k in self._states if k[1] == pid]:
            self._states.pop(k, None)
        return True

    def policies(self) -> list[Policy]:
        return list(self._policies.values())

    def policy(self, pid: str) -> Optional[Policy]:
        return self._policies.get(pid)

    # ------------------------------------------------------------ 卡树
    def set_parent_provider(self, fn: Callable[[str, str], str]) -> None:
        self._parent_provider = fn

    def invalidate_tree(self, work_id: Optional[str] = None) -> None:
        if work_id is None:
            self._parents.clear()
        else:
            self._parents.pop(work_id, None)

    def _parent_of(self, work_id: str, card_id: str) -> str:
        """某张卡的父卡（缓存；卡树事件进来时失效）。"""
        if not card_id:
            return ""
        if self._parent_provider is not None:
            try:
                return str(self._parent_provider(work_id, card_id) or "")
            except Exception:  # noqa: BLE001 拿不到就当没有父
                return ""
        cache = self._parents.get(work_id)
        if cache is None:
            try:
                from ..storage import card_store
                metas = card_store.iter_card_metas(work_id) or {}
                cache = {str(c): str((m or {}).get("parent") or "")
                         for c, m in metas.items()}
            except Exception as exc:  # noqa: BLE001
                logger.warning("[policy_set] 读卡树失败 work=%s: %s", work_id, exc)
                cache = {}
            self._parents[work_id] = cache
        return str(cache.get(str(card_id)) or "")

    # ------------------------------------------------------------ 作用域
    def _scope_keys(self, p: Policy, work_id: str,
                    fact: dict[str, Any]) -> list[tuple[str, ...]]:
        """这条事实落在哪些作用域实例上。"""
        if p.scope_kind == "work":
            return [("",)]
        if p.scope_kind == "card":
            c = str(fact.get("card") or "")
            return [(c,)] if c else []
        # relation：一对 (父, 子)
        out: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()
        for c in (fact.get("card"), fact.get("from"), fact.get("to"), fact.get("by")):
            c = str(c or "")
            if not c:
                continue
            parent = self._parent_of(work_id, c)
            if not parent:
                continue
            key = (parent, c)
            if key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def _state(self, work_id: str, p: Policy,
               key: tuple[str, ...], now: float) -> State:
        k = (work_id, p.id, key)
        st = self._states.get(k)
        if st is None:
            st = State(p.id, key, now)
            self._states[k] = st
        return st

    # ------------------------------------------------------------ 求值
    def _ctx(self, p: Policy, work_id: str, key: tuple[str, ...],
             st: State) -> dict[str, Any]:
        """把本子包成求值上下文（条件树原样复用，写法不变）。

        ``harness_cond`` 期望 ``events`` 是**新→旧**，本子里是升序，这里反转。
        """
        card_id = key[-1] if p.scope_kind != "work" else ""
        parent = self._parent_of(work_id, card_id) if card_id else ""
        return {
            "work_id": work_id,
            "card_id": card_id,
            "parent": parent,
            "scope": list(key),
            "events": list(reversed(st.entries)),
        }

    def _brief_ctx(self, p: Policy, work_id: str,
                   key: tuple[str, ...]) -> dict[str, Any]:
        """触发记录里带的上下文（不含 entries——那可能几百条，不能塞进结果）。"""
        card_id = key[-1] if p.scope_kind != "work" else ""
        return {
            "work_id": work_id,
            "card_id": card_id,
            "parent": (self._parent_of(work_id, card_id) if card_id else ""),
            "scope": list(key),
        }

    def _satisfied(self, p: Policy, work_id: str,
                   key: tuple[str, ...], st: State) -> bool:
        if p.when is None:
            return False
        from . import harness_cond
        try:
            return bool(harness_cond.eval_cond(p.when, self._ctx(p, work_id, key, st)))
        except Exception as exc:  # noqa: BLE001 判不了 ⇒ 不触发（宁可少发）
            logger.warning("[policy_set] 条件求值异常 %s: %s", p.id, exc)
            return False

    # ------------------------------------------------------------ 派发
    def dispatch(self, work_id: str, fact: dict[str, Any]) -> list[dict[str, Any]]:
        """一条事实进来：喂给关心它的值班员，谁凑够了就返回谁（并撕本子）。"""
        ev_type = str(fact.get("type") or "")
        if not ev_type:
            return []
        if ev_type in _TREE_EVENTS:
            self.invalidate_tree(work_id)

        pids = set(self._wildcard)
        pids |= self._by_watch.get(ev_type, set())
        if not pids:
            return []

        now = self._now()
        fired: list[dict[str, Any]] = []
        for pid in pids:
            p = self._policies.get(pid)
            if p is None or not p.active:
                continue
            if p.work and p.work != work_id:
                continue          # 这条策略只管它自己的 work
            win = p.window_sec
            for key in self._scope_keys(p, work_id, fact):
                st = self._state(work_id, p, key, now)
                st.add(fact, win, now)
                if self._satisfied(p, work_id, key, st):
                    self._fired += 1
                    fired.append({
                        "policy": p.id,
                        "scope": list(key),
                        "desc": p.desc,
                        "then": dict(p.then),
                        "counters": dict(st.counters),
                        "ctx": self._brief_ctx(p, work_id, key),
                    })
                    st.clear(now)      # 撕本子重记：只清这一本
        return fired

    # ------------------------------------------------------------ 回溯
    def seed(self, work_id: str, facts: Iterable[dict[str, Any]]) -> dict[str, int]:
        """用历史事实把本子补记好（新建/激活策略时调，之后靠增量）。

        返回 ``{policy_id: 补记条数}``。补完**不求值**——求值交给调用方决定要不要
        立刻触发（``check_all``），这样"补记"和"判断"两件事分开。
        """
        now = self._now()
        fed: dict[str, int] = {}
        for fact in facts:
            ev_type = str(fact.get("type") or "")
            if ev_type in _TREE_EVENTS:
                self.invalidate_tree(work_id)
            pids = set(self._wildcard) | self._by_watch.get(ev_type, set())
            for pid in pids:
                p = self._policies.get(pid)
                if p is None or not p.active:
                    continue
                if p.work and p.work != work_id:
                    continue
                win = p.window_sec
                for key in self._scope_keys(p, work_id, fact):
                    st = self._state(work_id, p, key, now)
                    st.add(fact, win, now)
                    fed[pid] = fed.get(pid, 0) + 1
        return fed

    def check_all(self, work_id: str) -> list[dict[str, Any]]:
        """对某 work 的全部本子求值一次（回溯完立刻看有没有已满足的）。"""
        now = self._now()
        out: list[dict[str, Any]] = []
        for (wid, pid, key), st in list(self._states.items()):
            if wid != work_id:
                continue
            p = self._policies.get(pid)
            if p is None or not p.active:
                continue
            if p.work and p.work != work_id:
                continue
            if self._satisfied(p, work_id, key, st):
                self._fired += 1
                out.append({"policy": pid, "scope": list(key),
                            "desc": p.desc, "then": dict(p.then),
                            "counters": dict(st.counters),
                            "ctx": self._brief_ctx(p, work_id, key)})
                st.clear(now)
        return out

    # ------------------------------------------------------------ 诊断
    def states(self, work_id: Optional[str] = None) -> list[dict[str, Any]]:
        out = []
        for (wid, _pid, _key), st in self._states.items():
            if work_id is not None and wid != work_id:
                continue
            d = st.snapshot()
            d["work"] = wid
            out.append(d)
        return out

    def stats(self) -> dict[str, Any]:
        return {
            "policies": len(self._policies),
            "active": sum(1 for p in self._policies.values() if p.active),
            "states": len(self._states),
            "fired": self._fired,
        }

    def reset(self) -> None:
        """清空所有本子（测试/重启用）。策略定义保留。"""
        self._states.clear()
        self._parents.clear()
        self._fired = 0


# ---------------------------------------------------------------- 进程级单例
_SET: Optional[PolicySet] = None


def dispatcher() -> PolicySet:
    """进程级条件集（懒建 + 懒加载配置）。"""
    global _SET
    if _SET is None:
        from . import policy_config
        _SET = PolicySet()
        _SET.load(policy_config.load_raw())
    return _SET


def reset_set(set_: Optional[PolicySet] = None) -> PolicySet:
    """替换单例（测试/改配置后重载用）。"""
    global _SET
    _SET = set_ or PolicySet()
    return _SET
