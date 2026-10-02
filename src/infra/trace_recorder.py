"""每轮对话的思考链路归档（v0.03 新增）。

一轮对话 = 一个 trace 文档。每个 worker 的调用都追加记录到
``traces/{trace_id}.md``，保证整轮思考链路可复盘：

- 输入了什么 prompt（system/user 摘要）
- 调了哪个模型、耗时、token
- 输出了什么内容
- 成功还是失败

设计原则：
- **append-only**：每次 record 追加一个 markdown section，绝不覆写
- **失败不阻塞**：归档写入失败只记日志，不影响流水线
- **人可读**：markdown 格式，开发者可直接打开 traces/{trace_id}.md 复盘
- **无侵入**：orchestrator 调 ``record_step``，不改变 worker/gateway 接口

文件位置：``<workspace_root>/traces/{trace_id}.md``
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..infra.logging import get_logger

logger = get_logger(__name__)


def _truncate(text: str, limit: int = 2000) -> str:
    """截断长文本，超限加省略标记。"""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...（已截断，完整长度 {len(text)} 字符）"


class TraceRecorder:
    """每轮对话的思考链路归档器。

    一个 trace_id 对应一个 markdown 文件。首次 record 会写 header，
    后续 record_step 追加 section，finalize 写 footer。
    """

    def __init__(self, traces_dir: str | Path):
        self._dir = Path(traces_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        # 记录已写过 header 的 trace_id，避免重复
        self._headers_written: set[str] = set()

    def _file_for(self, trace_id: str) -> Path:
        return self._dir / f"{trace_id}.md"

    def write_header(
        self,
        trace_id: str,
        session_id: str,
        user_input: str,
        mode: str = "normal",
    ) -> None:
        """写 trace 文档头部（一轮对话只写一次）。"""
        if trace_id in self._headers_written:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = (
            f"# Trace {trace_id}\n\n"
            f"- **时间**：{ts}\n"
            f"- **会话**：{session_id}\n"
            f"- **模式**：{mode}\n"
            f"- **用户输入**：\n\n```\n{_truncate(user_input, 4000)}\n```\n\n"
            f"---\n\n"
        )
        try:
            # 文件不存在则创建并写 header
            path = self._file_for(trace_id)
            path.write_text(header, encoding="utf-8")
            self._headers_written.add(trace_id)
        except OSError as exc:
            logger.warning("trace_header_write_failed", trace_id=trace_id, error=str(exc))

    def record_step(
        self,
        trace_id: str,
        step: str,
        worker_name: str,
        model_name: str,
        input_summary: str,
        output: Any,
        success: bool,
        tokens_used: int = 0,
        latency_ms: int = 0,
        error_msg: Optional[str] = None,
    ) -> None:
        """记录一个工序步骤的完整输入输出。

        Args:
            trace_id: 链路 ID
            step: 步骤名（如 "step1_purify"、"context_fetch_stage1"）
            worker_name: worker 角色名
            model_name: 实际模型名（如 generator_5）
            input_summary: 喂给模型的输入摘要（user_prompt 或关键字段）
            output: worker 产出（WorkerOutput.data 或原始 content）
            success: 是否成功
            tokens_used: token 消耗
            latency_ms: 耗时毫秒
            error_msg: 失败时的错误信息
        """
        ts = datetime.now().strftime("%H:%M:%S")
        status_mark = "OK" if success else "FAIL"

        # 输出内容格式化：dict 转 key-value 列表，str 直接展示
        if isinstance(output, dict):
            # 过滤掉内部透传字段（_tool_calls 等）
            clean = {k: v for k, v in output.items() if not k.startswith("_")}
            output_text = "\n".join(
                f"- **{k}**：{_truncate(str(v), 1500)}"
                for k, v in clean.items()
            ) or "（空）"
        else:
            output_text = _truncate(str(output), 2000)

        section = (
            f"## {step} — {worker_name} [{status_mark}]\n\n"
            f"- **时间**：{ts}\n"
            f"- **模型**：{model_name}\n"
            f"- **耗时**：{latency_ms} ms\n"
            f"- **Token**：{tokens_used}\n"
        )
        if error_msg:
            section += f"- **错误**：{error_msg}\n"
        section += f"\n**输入**：\n\n```\n{_truncate(input_summary, 2000)}\n```\n\n"
        section += f"**输出**：\n\n{output_text}\n\n---\n\n"

        try:
            path = self._file_for(trace_id)
            if not path.exists():
                # 兜底：header 没写过就先写个简版
                self.write_header(trace_id, "?", "(header missing)", "?")
            with path.open("a", encoding="utf-8") as f:
                f.write(section)
        except OSError as exc:
            logger.warning(
                "trace_step_write_failed",
                trace_id=trace_id, step=step, error=str(exc),
            )

    def finalize(
        self,
        trace_id: str,
        final_reply: str,
        total_tokens: int = 0,
        status: str = "completed",
    ) -> None:
        """写 trace 文档尾部（总结）。"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        footer = (
            f"\n## 总结 [{status}]\n\n"
            f"- **完成时间**：{ts}\n"
            f"- **总 Token**：{total_tokens}\n"
            f"- **最终回复**：\n\n```\n{_truncate(final_reply, 4000)}\n```\n"
        )
        try:
            path = self._file_for(trace_id)
            with path.open("a", encoding="utf-8") as f:
                f.write(footer)
        except OSError as exc:
            logger.warning("trace_finalize_failed", trace_id=trace_id, error=str(exc))


__all__ = ["TraceRecorder"]
