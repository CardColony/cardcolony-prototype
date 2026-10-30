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
        self._candidates: tuple[Candidate, ...] = tuple(
            c for c in candidates if c.weight > 0)
        self._current: dict[str, int] = {
            f"{c.executor}|{c.model}": 0 for c in self._candidates}
        self._total = sum(c.weight for c in self._candidates)
        self._lock = threading.Lock()

    @property
    def candidates(self) -> tuple[Candidate, ...]:
        return self._candidates

    def __len__(self) -> int:
        return len(self._candidates)

    def pick(self) -> Optional[Candidate]:
        """按配比挑一个候选。无候选 ⇒ None。"""
        if not self._candidates:
            return None
        with self._lock:
            best: Optional[Candidate] = None
            best_key = ""
            for c in self._candidates:
                key = f"{c.executor}|{c.model}"
                self._current[key] = self._current.get(key, 0) + c.weight
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
                cur[key] = cur.get(key, 0) + c.weight
                if best is None or cur[key] > cur[f"{best.executor}|{best.model}"]:
                    best = c
                    best_key = key
            if best is None:
                break
            cur[best_key] -= self._total
            out.append(best)
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
