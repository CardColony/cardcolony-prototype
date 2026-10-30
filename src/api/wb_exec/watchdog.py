# -*- coding: utf-8 -*-
"""委派回合的**状态账**（2026-10-23 重写：去掉一切时间闸）。

**为什么重写**：早先这里有三套时间阈值（网关心跳 90s / prompt 心跳 150s /
业务事件 240s）＋ 一个 ``rpc_timeout_sec=300``，看起来在"判断死活"，
其实全是**拿时间猜**——猜错就误杀正在干活的长任务（实测：一次 615.9 秒的
正常任务被当成"卡住"；一次网关进程重建的瞬时空档被当成"未登录"，
再叠加"永久熔断"把整条通道打瘫）。

**现在只认状态**（全部来自 WorkBuddy 自己，不猜）：

=====================  =======================================================
状态来源                含义
=====================  =======================================================
``agentPhase``         ACP 通知 ``_meta["codebuddy.ai/agentPhase"].phase``
                       ⇒ ``idle`` / ``preparing`` / …  **agent 在不在干活**
``stopReason``         本轮结果：``end_turn`` / ``refusal`` / …
``run.active``         ``GET /api/v1/runs/{runId}`` ⇒ 这个 run 还在不在
``status.busy``        ``GET /api/v1/status`` ⇒ 网关这个 agent 忙不忙
``runStatus``          同上 ⇒ ``idle`` / ``model_streaming`` / …
**连接事件**            SSE 结束 / httpx 异常 ⇒ **连接真的断了**（不是"多久没动静"）
=====================  =======================================================

⇒ **"忙"就去排队（按 busy 状态）**；**"死了"就是连接真的断了**（事件）。
⇒ 唯一保留的时间是**传输层兜底**：建连超时、读超时（必须 > WorkBuddy 的
60 秒 keepalive 间隔，否则会把"正在思考"误判成"断了"）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

#: WorkBuddy 的 prompt 级 keepalive 间隔（实测 60s）⇒ 读超时必须显著大于它。
KEEPALIVE_INTERVAL_SEC = 60.0


@dataclass
class Watchdog:
    """一次委派回合的**状态**（不是"活了多久"）。"""

    #: 建连超时（秒）——纯传输层。
    connect_timeout_sec: float = 30.0
    #: 读超时（秒）——**传输层兜底**（> keepalive 间隔），不是业务判据。
    read_timeout_sec: float = 600.0
    #: 网关**按状态**显示忙时的排队上限（秒）——"排队"的上限，不是判据。
    busy_wait_sec: float = 900.0

    started_at: float = field(default_factory=time.monotonic)
    #: 最近的 agentPhase（idle / preparing / …）——"agent 在不在干活"。
    phase: str = ""
    phase_at: float = field(default_factory=time.monotonic)
    #: 本轮结果（end_turn / refusal / …）。
    stop_reason: str = ""
    #: **在途的工具调用**（有 tool_call、还没结果）⇒ 它在跑那个工具。
    #: 跑大脚本期间**不吐字**，但这个集合非空 ⇒ 依然是"在干活"（状态，不是时间）。
    pending_tools: set[str] = field(default_factory=set)
    #: 收到的帧数 / 业务事件数 / **吐字次数**（诊断用）。
    frame_count: int = 0
    business_count: int = 0
    text_count: int = 0
    #: 连接真的断了吗（**事件**，不是时间）。
    disconnected: bool = False

    # ---------------------------------------------------------------- 状态
    def note_frame(self) -> None:
        """收到任何 SSE 帧（含 ``: heartbeat`` 注释帧）。"""
        self.frame_count += 1

    def note_keepalive(self) -> None:
        """收到 prompt 级 keepalive（说明 agent 还活着）。"""
        self.note_frame()

    def note_business(self) -> None:
        """收到业务事件（agent 在干活）。"""
        self.note_frame()
        self.business_count += 1

    def note_text(self, n: int = 1) -> None:
        """**收到吐字**（思考/正文）——"它在动"的第一手证据。

        用户原话（2026-10-23）：*"正在思考，他妈的是会吐字的，你但凡读吐字，
        都不会存在误判"* ⇒ **吐字本身就是状态**，不需要拿时间去猜它卡没卡。
        """
        self.text_count += int(n)
        self.note_business()

    def note_tool_started(self, call_id: str) -> None:
        """工具调用开始了（**还没结果 ⇒ 它在跑那个工具**，可能长时间不吐字）。"""
        if call_id:
            self.pending_tools.add(call_id)

    def note_tool_finished(self, call_id: str) -> None:
        """工具调用有结果了（completed / failed 都算结束）。"""
        self.pending_tools.discard(call_id)

    def note_phase(self, phase: str) -> None:
        """记下 agentPhase（**WorkBuddy 给的状态**，不猜）。"""
        if phase:
            self.phase = phase
            self.phase_at = time.monotonic()

    def note_stop(self, stop_reason: str) -> None:
        """记下本轮结果。"""
        if stop_reason:
            self.stop_reason = stop_reason

    def note_disconnected(self) -> None:
        """连接真的断了（SSE 结束 / 传输异常）——**这是事件，不是"多久没动静"**。"""
        self.disconnected = True

    # ---------------------------------------------------------------- 判定
    def busy(self) -> bool:
        """agent 现在**忙**吗？（按状态，不按时间）

        - ``phase`` 明确不是 ``idle`` ⇒ 忙；
        - 没有 phase 信息 ⇒ **不猜**（返回 False，让上层去问网关的 ``status.busy``）。
        """
        if self.pending_tools:
            return True          # 工具还在跑 ⇒ 忙（哪怕不吐字）
        if not self.phase:
            return False
        return self.phase not in ("idle", "completed", "done", "stopped")

    def snapshot(self) -> dict:
        return {
            "elapsed_sec": round(time.monotonic() - self.started_at, 1),
            "phase": self.phase,
            "busy": self.busy(),
            "stop_reason": self.stop_reason,
            "disconnected": self.disconnected,
            "frames": self.frame_count,
            "business": self.business_count,
            "text": self.text_count,
            "pending_tools": len(self.pending_tools),
        }
