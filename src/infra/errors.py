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
