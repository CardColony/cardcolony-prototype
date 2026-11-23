# -*- coding: utf-8 -*-
"""[成本优化批五 · COST_HOP_RETRY_BUDGET] 同一逻辑模型跳次的共享尝试预算。

一份预算对象贯穿：网关内层重试、同层换座、循环器同位置重发
（``_call_model_resumable``）。扣账边界＝适配器即将提交实际模型请求时
（``transport.send_stream`` 调 ``client.send`` 之前）；确认发送、真实终帧、
成功/失败分类分账。工具续跳产生自己的预算对象（新跳新账）。

额度＝首选座 ``retry.max_attempts``（缺省 3，含首次）。层内有合规备选座
且失败允许换座时，保留最后一次尝试给下一座：额度 2 ⇒ A→B，额度 3 ⇒
A→A→B。确定性拒绝（auth/deterministic）不自动重发。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class HopBudgetExhausted(Exception):
    """同跳尝试预算耗尽——可上抛的控制结果：不触发再重发/换座，直接收口。

    ``.budget`` 带完整账目：used/sent/terminal/逐次尝试（座、确认、终帧、
    成败分类）——对账与事件报告都取它。报告快照**惰性**生成（``gate``
    持锁抛本异常，构造时取锁会死锁）。
    """

    def __init__(self, budget: "HopBudget") -> None:
        super().__init__(
            f"同跳尝试预算耗尽 used={budget.used}/{budget.limit}")
        self.budget = budget
        self._report: Optional[dict] = None

    @property
    def report_data(self) -> dict:
        if self._report is None:
            self._report = dict(self.budget.report(), exhausted=True)
        return self._report


class HopBudgetUnverified(Exception):
    """无法确认原跳的续接位置或已用额度。"""

    def __init__(self, identity: dict, used: int | None) -> None:
        super().__init__(f"原跳续接待核实，已记出站尝试 {used if used is not None else '未知'} 次")
        self.facts = {**identity, "used": used, "status": "unverified"}


class HopBudget:
    """一次逻辑跳次的尝试预算（线程安全——网关/外层可能在不同协程读账）。"""

    #: 确定性拒绝类别：不自动重发（同一份请求重发注定同一拒绝）。
    NO_RESEND_REASONS = ("auth", "deterministic")

    def __init__(self, *, limit: int, identity: Optional[dict] = None,
                 reserve_attempt: Callable[[dict, int], tuple[bool, int | None]] | None = None) -> None:
        self.limit = max(1, int(limit))
        self.used = 0          # 已领用尝试（进入发送调用即记）
        self.sent = 0          # 已确认发送（on_dispatch 实发回执）
        self.terminal = 0      # 真实终帧数
        self.exhausted = False
        self.identity = dict(identity or {})
        self.seats: list[str] = []
        self.attempts: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._reserve_attempt = reserve_attempt
        self.recovery_error: HopBudgetUnverified | None = None

    # ---------------------------------------------------------- 账目
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit - self.used)

    def gate(self, seat: str) -> int:
        """发送边界原子领用一次尝试。余额不足 ⇒ HopBudgetExhausted。

        进入发送调用即记一次尝试——连不上/未确认发送也保留本地尝试记录
        （sent 位保持 False，正是「发出未确认」的证据）。
        """
        with self._lock:
            if self.recovery_error is not None:
                raise self.recovery_error
            if self.used >= self.limit:
                self.exhausted = True
                raise HopBudgetExhausted(self)
            if self._reserve_attempt is not None:
                reserved, prior = self._reserve_attempt(self.identity, self.used)
                if not reserved:
                    self.recovery_error = HopBudgetUnverified(self.identity, prior)
                    raise self.recovery_error
            self.used += 1
            self.seats.append(str(seat))
            self.attempts.append({
                "n": self.used, "seat": str(seat),
                "sent": False, "terminal": False, "outcome": "",
            })
            return self.used

    def confirm_sent(self) -> None:
        """``on_dispatch`` 实发确认：请求体确实提交到线路上。"""
        with self._lock:
            self.sent += 1
            if self.attempts:
                self.attempts[-1]["sent"] = True

    def note_terminal(self) -> None:
        """真实终帧到达（finish 帧）。"""
        with self._lock:
            self.terminal += 1
            if self.attempts:
                self.attempts[-1]["terminal"] = True

    def settle(self, outcome: str) -> None:
        """本次尝试的成败/失败分类（ok/503/auth/deterministic/…）。"""
        with self._lock:
            if self.attempts:
                self.attempts[-1]["outcome"] = str(outcome)

    def report(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self.identity,
                "limit": self.limit,
                "used": self.used,
                "sent": self.sent,
                "terminal": self.terminal,
                "exhausted": self.exhausted,
                "seats": list(dict.fromkeys(self.seats)),
                "attempts": [dict(a) for a in self.attempts],
            }
