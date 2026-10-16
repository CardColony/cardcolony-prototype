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

    # ---- 模型层底座（llm）错误码扩展 --------------------------------
    # 抄自 dsh-llm 的 provider 无关码表（原版 error.ts / adapter.ts）。
    # 新增原因：模型层底座整体抄写（2026-10-16）——新底座按这些码做重试分级与
    # 前端状态展示；旧 gateway 的 MODEL_CALL_FAILED / MISSING_API_KEY 保留不动。
    AUTH = "AUTH"                              # 401/403：凭据无效，不可重试
    RATE_LIMIT = "RATE_LIMIT"                  # 429：限流，可重试
    QUOTA = "QUOTA"                            # 配额/余额耗尽，不可重试
    CONTEXT_WINDOW_EXCEEDED = "CONTEXT_WINDOW_EXCEEDED"  # 请求超出上下文窗口
    INVALID_REQUEST = "INVALID_REQUEST"        # 400（非上下文超限）
    SERVER = "SERVER"                          # ≥500，可重试
    TIMEOUT = "TIMEOUT"                        # 空闲看门狗超时，可重试
    TRANSPORT = "TRANSPORT"                    # 连接层失败，可重试
    EMPTY_RESPONSE = "EMPTY_RESPONSE"          # 正常完成但零内容，可重试
    STREAM_CLOSED = "STREAM_CLOSED"            # SSE 流未以 [DONE] 正常收尾
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"  # 载荷 JSON 解析失败
    NO_ADAPTER = "NO_ADAPTER"                  # 未注册该 provider 的适配器
    UNSUPPORTED_REASONING_EFFORT = "UNSUPPORTED_REASONING_EFFORT"
    UNSUPPORTED_CONTENT = "UNSUPPORTED_CONTENT"
    INVALID_CREDENTIAL = "INVALID_CREDENTIAL"  # 凭据存在但不可用（畸形）


class AppError(Exception):
    """应用统一错误，携带错误码。

    ``code`` 可以是 :class:`ErrorCode`（本仓既有用法），也可以是**裸字符串**
    （模型层底座抄来的 provider 无关码，如 ``"AUTH"`` / 动态 ``"HTTP_503"``）；
    两种都能被 ``except ModelError`` / ``except AppError`` 捕获（2026-10-16）。
    """

    def __init__(self, code: ErrorCode | str, msg: str):
        self.code = code
        self.msg = msg
        code_str = code.value if isinstance(code, ErrorCode) else str(code)
        super().__init__(f"[{code_str}] {msg}")


class ModelError(AppError):
    """模型调用相关错误。"""
