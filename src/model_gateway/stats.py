"""模型网关调用统计：累计 token、调用次数、最近调用日志。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


class GatewayStats:
    """累计各模型的调用次数、Token 消耗与最近调用日志。"""

    def __init__(self) -> None:
        self.total_tokens: int = 0
        self.total_calls: int = 0
        self.by_model: dict[str, dict[str, int]] = defaultdict(
            lambda: {"calls": 0, "tokens": 0, "failures": 0}
        )
        self.recent: list[dict[str, Any]] = []

    def record(
        self, model_name: str, tokens: int, latency_ms: int, success: bool = True
    ) -> None:
        """记录一次调用。"""
        self.total_calls += 1
        self.total_tokens += tokens
        entry = self.by_model[model_name]
        entry["calls"] += 1
        entry["tokens"] += tokens
        if not success:
            entry["failures"] += 1
        self.recent.append(
            {
                "model": model_name,
                "tokens": tokens,
                "latency_ms": latency_ms,
                "success": success,
            }
        )
        if len(self.recent) > 100:
            self.recent = self.recent[-100:]

    def summary(self) -> dict[str, Any]:
        """返回统计摘要。"""
        return {
            "total_tokens": self.total_tokens,
            "total_calls": self.total_calls,
            "by_model": {k: dict(v) for k, v in self.by_model.items()},
            "recent": self.recent[-10:],
        }
