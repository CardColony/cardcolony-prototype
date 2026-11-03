# -*- coding: utf-8 -*-
"""配比器：平滑加权轮询（smooth weighted round-robin）＋ fallback 链。

**为什么用平滑加权轮询而不是最小负载**（用户令 2026-10-23 09:56：
*"最小负载是什么意思，就平均走吧"*、*"不永远走国内版"*）：
平滑轮询在权重固定时给出**严格均匀**的分配，且同权重项**交替出现**
（不固定某一侧），这正是用户要的"平均走"。

**fallback**：用户令 *"上游额度耗尽，或者重连失败，就用 hy3"*。
⇒ 不是按权重逐级降档，而是跳到 fallback 模型（hy3）：
先试**同侧**网关的 hy3（同 session，只改 model，不重发 prompt），
再试另一侧的 hy3（需换网关 ⇒ 重连 + session/load）。
"""
from __future__ import annotations

import threading
from typing import Optional, Sequence

from .config import Candidate


class Picker:
    """线程安全的平滑加权轮询配比器。

    算法（nginx smooth weighted round-robin）：
      每次 pick：所有候选 current += weight；取 current 最大者为胜者；
      胜者 current -= 总权重。
    ⇒ 权重相等时严格交替；权重不等时按比例平滑逼近。
    """

    def __init__(self, candidates: Sequence[Candidate]) -> None:
        #: **全部候选**（含 ``weight == 0`` 的**备用端点**）——
        #: ★ weight=0 的语义：**平时不吃流量**；等原候选的额度用完时，
        #:   它的**份额被转移过来**（见 :meth:`set_weight`）。
        #:   2026-11-03 用户令：hy3（免费）额度用完 ⇒ 全部换收费的两端
        #:   glm-5.3-flash ⇒ 备用端点就写成 weight=0 的两行。
        self._all: tuple[Candidate, ...] = tuple(
            c for c in candidates if c.weight >= 0)
        #: **当前活跃**的候选（权重 > 0 ⇒ 吃流量）。
        self._candidates: tuple[Candidate, ...] = tuple(
            c for c in self._all if c.weight > 0)
        #: 每个候选的**当前权重**（动态调整会改写它）。
        self._weights: dict[str, int] = {
            f"{c.executor}|{c.model}": int(c.weight) for c in self._all}
        #: **配置里的原始权重**（恢复时按它还回去）。
        self._base: dict[str, int] = dict(self._weights)
        self._current: dict[str, int] = {
            f"{c.executor}|{c.model}": 0 for c in self._candidates}
        self._total = sum(self._weights[f"{c.executor}|{c.model}"]
                          for c in self._candidates)
        self._lock = threading.Lock()

    @property
    def candidates(self) -> tuple[Candidate, ...]:
        return self._candidates

    def __len__(self) -> int:
        return len(self._candidates)

    # -------------------------------------------------- 动态调整（额度耗尽让路）
    def get_weight(self, executor: str, model: str) -> int:
        """这个候选**当前**的权重（备用端点平时是 0）。"""
        return int(self._weights.get(f"{executor}|{model}", 0))

    def base_weight(self, executor: str, model: str) -> int:
        """这个候选在**配置里**的原始权重（恢复时按它还回去）。"""
        return int(self._base.get(f"{executor}|{model}", 0))

    def set_weight(self, executor: str, model: str, weight: int) -> bool:
        """**改权重** ⇒ 动态调整（2026-11-03 用户令的机制核心）。

        - ``weight > 0`` ⇒ 进轮询，吃流量；
        - ``weight == 0`` ⇒ 退出轮询（份额让给别人，**不浪费**）。

        用法：**额度耗尽 ⇒ 把它的权重转移给同侧的备用端点**
        （``set_weight(原, 0)`` + ``set_weight(备用, 原的权重)``）
        ⇒ 后续所有卡自动走备用端点，**份额 100% 保留**（不是浪费掉）。

        ★ 为什么不能只"跳过"：平滑加权轮询里被跳过的候选那一轮仍然
        ``current += weight`` ⇒ 它的份额**被浪费** ⇒ 实际流量只剩 75%。
        """
        key = f"{executor}|{model}"
        with self._lock:
            if key not in self._weights:
                return False
            self._weights[key] = max(0, int(weight))
            self._rebuild()
            return True

    def _rebuild(self) -> None:
        """按当前权重重建活跃列表 + 轮询状态 + 总权重（**改权重后必须调**）。"""
        self._candidates = tuple(
            c for c in self._all
            if self._weights.get(f"{c.executor}|{c.model}", 0) > 0)
        active = {f"{c.executor}|{c.model}" for c in self._candidates}
        for key in list(self._current):
            if key not in active:
                self._current.pop(key, None)
        for key in active:
            self._current.setdefault(key, 0)
        self._total = sum(self._weights.get(k, 0) for k in active)

    def pick(self) -> Optional[Candidate]:
        """按配比挑一个候选。无候选 ⇒ None。"""
        if not self._candidates:
            return None
        with self._lock:
            best: Optional[Candidate] = None
            best_key = ""
            for c in self._candidates:
                key = f"{c.executor}|{c.model}"
                self._current[key] = self._current.get(key, 0) + self._weights.get(key, 0)
                if best is None or self._current[key] > self._current[f"{best.executor}|{best.model}"]:
                    best = c
                    best_key = key
            if best is None:
                return None
            self._current[best_key] -= self._total
            return best

    def peek_sequence(self, n: int) -> list[Candidate]:
        """预览接下来 n 次的分配（测试用；不改变内部状态）。"""
        out: list[Candidate] = []
        cur = dict(self._current)
        for _ in range(max(0, n)):
            if not self._candidates:
                break
            best: Optional[Candidate] = None
            best_key = ""
            for c in self._candidates:
                key = f"{c.executor}|{c.model}"
                cur[key] = cur.get(key, 0) + self._weights.get(key, 0)
                if best is None or cur[key] > cur[f"{best.executor}|{best.model}"]:
                    best = c
                    best_key = key
            if best is None:
                break
            cur[best_key] -= self._total
            out.append(best)
        return out


def priority_order(candidates: Sequence[Candidate]) -> list[Candidate]:
    """**优先级瀑布**的顺序（2026-11-04 用户令）。

    用户令：「先全部排 devin，它排满了或额度用完了，就落 opencode，
    opencode 不行，再落 workbuddy」⇒ **不再按权重分流**，而是**按优先级排队**。

    - ``priority`` **越小越优先**；
    - 没写 ``priority`` 的行**按表里的行序**（config 解析时已填成行号）；
    - 同优先级 ⇒ 保持表序（稳定排序）。

    ★ **为什么配比器不再是主路径**：配比是"平均分流"，而用户要的是
    "**能用好的先用，好的排不下了才用次的**"⇒ 配比会让 Devin 只吃 40%，
    明明它还空着 ⇒ 浪费。
    """
    return [c for _, c in sorted(enumerate(candidates),
                                 key=lambda t: (int(t[1].priority), t[0]))]


def cascade_chain(candidates: Sequence[Candidate],
                  current: Optional[Candidate],
                  tried: Optional[set[tuple[str, str]]] = None) -> list[Candidate]:
    """**当前候选失败后的降级链**＝按优先级的其余候选（跳过已试过的）。

    ★ 与 :func:`fallback_chain` 的区别：那个是"只跳 fallback.models 里的模型"
    （老的 hy3 降级）；这个是**整张表的优先级顺序**（Devin → opencode → WorkBuddy）。

    用户令（2026-11-04）：「opencode 不行，再落 workbuddy」⇒ 降级链必须能
    **跨执行体**、**跨模型**，按优先级一路往下走。
    """
    done = set(tried or ())
    cur = (current.executor, current.model) if current is not None else None
    out: list[Candidate] = []
    for c in priority_order(candidates):
        key = (c.executor, c.model)
        if cur is not None and key == cur:
            continue
        if key in done:
            continue
        out.append(c)
    return out


def fallback_chain(current: Candidate,
                   candidates: Sequence[Candidate],
                   fallback_models: Sequence[str],
                   prefer_same_executor: bool = True,
                   ) -> list[Candidate]:
    """算当前候选失败后的降级链。

    规则（用户令）：
      1. 对每个 fallback 模型（当前是 hy3）：
         先试**同侧**执行器的该模型，再试其它执行器的该模型；
      2. 不重复当前候选本身；
      3. 只用**配比表里真实存在**的 (执行器, 模型) 组合——配比表是唯一事实源。
    """
    out: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    cur_key = (current.executor, current.model)

    def _add(c: Candidate) -> None:
        key = (c.executor, c.model)
        if key == cur_key or key in seen:
            return
        seen.add(key)
        out.append(c)

    for model in fallback_models:
        if prefer_same_executor:
            for c in candidates:
                if c.executor == current.executor and c.model == model:
                    _add(c)
        for c in candidates:
            if c.executor != current.executor and c.model == model:
                _add(c)
        if not prefer_same_executor:
            for c in candidates:
                if c.executor == current.executor and c.model == model:
                    _add(c)
    return out


# ===================================================================== 进程级配比器
_pickers: dict[tuple, "Picker"] = {}
_pickers_lock = threading.Lock()


def _signature(candidates: Sequence[Candidate]) -> tuple:
    return tuple((c.executor, c.model, c.weight) for c in candidates)


def get_picker(candidates: Sequence[Candidate]) -> "Picker":
    """**进程级**配比器（按配比表签名缓存）。

    **为什么不能每回合新建**（这是实测踩到的坑）：
    平滑加权轮询的状态（``current`` 累加值）在 ``pick()`` 里推进。
    若每个回合新建一个 Picker，状态每回合归零 ⇒ 第一次 pick 永远是权重最高的那一个
    ⇒ **配比退化成 100% 打在第一个候选上**（40/20/20/10/10 会变成 100% deepseek）。

    ⇒ 配比器必须是**跨回合存活**的进程级单例（配比表变了才重建）。
    """
    sig = _signature(candidates)
    with _pickers_lock:
        p = _pickers.get(sig)
        if p is None:
            p = Picker(candidates)
            _pickers[sig] = p
        return p


def reset_pickers() -> None:
    """测试用：清空进程级配比器。"""
    with _pickers_lock:
        _pickers.clear()
