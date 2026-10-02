"""错误码体系与降级策略。"""

from __future__ import annotations

from enum import Enum


class ErrorCode(Enum):
    MODEL_CALL_FAILED = "MODEL_CALL_FAILED"
    MISSING_API_KEY = "MISSING_API_KEY"
    WORKER_VALIDATION_FAILED = "WORKER_VALIDATION_FAILED"
    CONTEXT_READ_FAILED = "CONTEXT_READ_FAILED"
    CONTEXT_WRITE_FAILED = "CONTEXT_WRITE_FAILED"
    PIPELINE_ERROR = "PIPELINE_ERROR"
    REQUIREMENT_COVERAGE_GAP = "REQUIREMENT_COVERAGE_GAP"  # 骨架书未覆盖 must_have
    CONFLICT = "CONFLICT"          # 并发冲突（如取消已完成的任务 -> 409）


class AppError(Exception):
    """应用统一错误，携带错误码。"""

    def __init__(self, code: ErrorCode, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(f"[{code.value}] {msg}")


class ModelError(AppError):
    """模型调用相关错误。"""


# 降级策略表：每个工序失败时的处理方式
FALLBACK_STRATEGY: dict[str, dict[str, str | bool]] = {
    "purifier": {"on_fail": "fallback_model", "critical": True},
    "decomposer": {"on_fail": "skip", "critical": False},
    "gap_checker": {"on_fail": "skip", "critical": False},
    "generator": {"on_fail": "skip", "critical": False},  # 单个生成模型挂了跳过
    "synthesizer": {"on_fail": "fallback_model", "critical": True},
    "clarifier": {"on_fail": "skip", "critical": False},  # 澄清失败直接进生成
    "reviewer": {"on_fail": "skip", "critical": False},
    "condenser": {"on_fail": "skip", "critical": False},
}
