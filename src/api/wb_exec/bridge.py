# -*- coding: utf-8 -*-
"""桥接层：coder 卡的回合交给 WorkBuddy 执行体。

**接线点**：`src/api/message_runner.py` 的 ``step()`` 顶部（中止缝之后、``rounds += 1`` 之前）。
命中委派 ⇒ 本函数跑完外部执行体并**直接返回** ``StepOutcome("deliver", …)``，
让**既有交付路径**（write_logs → compose_delivery → create_cards → finish）原样跑完。

**不做的事**（避免越界）：
- 不碰 ``src/model_gateway/gateway.py``（项目纪律：禁改）——WorkBuddy 是**进程外**执行体，
  与推演agent 的模型网关毫无关系。
- 不给 ``cards`` 表加列（``card_db`` 是禁改文件）——"选哪个执行体"落在
  ``model_override`` 的**每卡一行**里（``exec_backend`` 字段，JSON 文件，不在库里）。
- 不新造完工闸——完工复用现成的 ``card_tools._h_report_done``（判态→coder_final→
  completed→影子留痕→发 ``coder_done`` 回执给父卡）。

**失败语义**（用户令 2026-10-23 09:56）：
- 上游额度耗尽（``category=="quota"``）或重连失败 ⇒ **fallback 到 hy3**
  （先同侧网关、再另一侧；只改 model，不重发 prompt）。
- 续跑次数超上限 ⇒ 回卡 + 结构化错误（由上层置 ``goal_state=blocked``）。
- 检测到"用户中断" ⇒ **不自动续**，回卡由人决定。
- 执行体整体不可用（没登录 / 网关起不来）⇒ 按 ``on_unavailable.mode``：
  ``fallback_system``（默认，安全）⇒ 回落系统自己的模型；``fail`` ⇒ 本轮失败。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import acp_client, picker as picker_mod
from . import switch
from .acp_client import (AcpClient, AcpNotification, CATEGORY_QUOTA,
                         WbAcpError, WbExecutorUnavailable, WbModelUnavailable)
from .config import Candidate, WbExecConfig, load as load_config
from .picker import Picker  # noqa: F401  （保留直引：类型标注与外部调用方用）
from .registry import get_registry
from .watchdog import Watchdog

logger = logging.getLogger("api.wb_exec.bridge")

#: 执行体选择：``auto``（走配比器）｜``system``（系统自己的模型）｜``wb:<版本>:<模型>``（钉死）。
BACKEND_AUTO = "auto"
BACKEND_SYSTEM = "system"
BACKEND_PREFIX = "wb:"

#: 现状块里进度小节的标题（幂等续跑的**事实来源**）。
PROGRESS_HEADING = "## wb_exec 进度"


@dataclass
class _Exec:
    """一次委派回合的执行上下文。"""

    state: Any
    cfg: WbExecConfig
    picker: Picker
    watchdog: Watchdog = field(default_factory=Watchdog)
    text_parts: list[str] = field(default_factory=list)
    tool_calls: int = 0
    tool_ok: int = 0
    #: **轮号**（WorkBuddy 的内部步）＝ 第几个工具调用。
    #: 不映射的话卡面会"满屏 round 1"（看着像死循环，其实它在正常干活）。
    step: int = 0
    model: str = ""
    executor_id: str = ""
    session_id: str = ""
    resumed: int = 0
    fallback_used: list[str] = field(default_factory=list)
    _tool_seen: dict[str, str] = field(default_factory=dict)
    #: 每个工具调用**自己的**步号（一条工具会连发多条 update ⇒ 不能用当前步）。
    _tool_step: dict[str, int] = field(default_factory=dict)


# ===================================================================== 判定
# [差事卡委派 2026-11-01 · 用户令「差事卡和编程卡一样走 wb 执行体」]
# qa_card/pm_card 的差事回合同样交给 WorkBuddy——执行体干活（读文件、跑
# 检查、写正文），收口时由桥接层机械完成差事语义（report 回派遣方 /
# cardwrite 落对象卡），因为执行体没有 card_tools。
#: **默认**可委派卡型（**兜底**；真值在配置 ``delegable_card_types`` 里）。
DEFAULT_DELEGABLE_CARD_TYPES = ("coder", "qa_card", "pm_card")
#: 兼容旧引用（外部/历史代码按这个名字取）。
DELEGABLE_CARD_TYPES = set(DEFAULT_DELEGABLE_CARD_TYPES)
DEPLOY_CARD_TYPES = {"qa_card", "pm_card"}


async def is_delegable_card(work_id: str, card_id: str,
                            cfg: Optional[WbExecConfig] = None) -> bool:
    """这张卡**能不能走执行体**？（= 卡型白名单，**配置化**）

    ★★ **2026-11-04 用户令**：「验收卡、pm卡，本质上应该和coder卡用一套路由机制，
    而不是分开的」⇒ 白名单放进配置（``wb_exec.json`` 的
    ``delegable_card_types``）⇒ **后端判定、接口返回、前端渲染门控共用同一份**
    ⇒ 以后加卡型只改配置一处。

    **读不到卡 ⇒ 不委派**（安全默认）。
    """
    if cfg is None:
        cfg = load_config()
    types = cfg.delegable_card_types or DEFAULT_DELEGABLE_CARD_TYPES
    try:
        from ...storage import card_store
        card = await card_store.read_card(work_id, card_id)
        return str((card.get("meta") or {}).get("card_type") or "") in types
    except Exception:  # noqa: BLE001 读不到 ⇒ 不委派（安全默认）
        return False


async def is_coder_card(work_id: str, card_id: str) -> bool:
    try:
        from ...storage import card_store
        card = await card_store.read_card(work_id, card_id)
        return str((card.get("meta") or {}).get("card_type") or "") == "coder"
    except Exception:  # noqa: BLE001 读不到 ⇒ 不委派（安全默认）
        return False


def exec_backend_of(work_id: str, card_id: str) -> str:
    """读卡的执行体选择（来自 ``model_override`` 每卡一行的 ``exec_backend``）。"""
    return exec_backend_raw_of(work_id, card_id) or BACKEND_AUTO


def exec_backend_raw_of(work_id: str, card_id: str) -> Optional[str]:
    """读**原始**的 ``exec_backend``（没设过 ⇒ None；与"默认 auto"区分得开）。

    ``scope.mode == "opt_in"`` 时用它判"这张卡是不是显式点了要接管的"。
    """
    try:
        from ...storage import model_override
        row = model_override.get_row(work_id, card_id) or {}
        val = str(row.get("exec_backend") or "").strip()
        return val or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[wb_exec] 读 exec_backend 失败（按未设）card=%s: %s",
                       card_id, exc)
        return None


def _parse_pinned(backend: str) -> Optional[tuple[str, str]]:
    """``<版本>:<模型>`` **或** ``wb:<版本>:<模型>`` ⇒ (executor_id, model)。

    **★ 两种都要认**（2026-10-25 实测发现的既有 bug）：
    前端下拉（``routes._wb_exec_options``）生成的 value 是 **``<executor>:<model>``
    （两段、没有 ``wb:`` 前缀）**，而这里早先只认三段 ⇒
    **卡面手动钉死执行体其实一直是失效的**（PUT 存进去了、bridge 解析不出来
    ⇒ 落回 auto）⇒ 现在两种格式都认。
    """
    b = str(backend or "").strip()
    if not b:
        return None
    if b.startswith(BACKEND_PREFIX):
        rest = b[len(BACKEND_PREFIX):]
        if ":" not in rest:
            return None
        executor_id, _, model = rest.partition(":")
    else:
        if ":" not in b:
            return None
        executor_id, _, model = b.partition(":")
    executor_id = executor_id.strip()
    model = model.strip()
    if not executor_id or not model:
        return None
    return executor_id, model


# ===================================================================== 入口
async def maybe_delegate(state: Any) -> Optional[Any]:
    """``step()`` 的委派钩子。**不委派 ⇒ 返回 None**（调用方照常走原路径）。"""
    cfg = load_config()
    # [轮号] 记住**进 bridge 之前**的轮号 —— 任何回落路径都要原样还回去
    # （回落后系统路径会做 ``state.rounds += 1``；我要是动过它，卡面就"满屏 round 1"）。
    _orig_rounds = state.rounds
    # [总开关 2026-10-23] 手动开关（运行时文件，**不用重启**）：
    #   开 ⇒ 走 WorkBuddy；关 ⇒ 全部走系统自己的模型。
    #   优先级：运行时开关 > config.enabled（fail-open：文件坏了退回配置）。
    if not switch.is_enabled():
        return None
    if not cfg.enabled:
        return None
    if not cfg.candidates:
        logger.info("[wb_exec] 配比表为空 ⇒ 不委派")
        return None
    # 判定顺序：**先看是不是可委派卡**（库读），再看它的执行体选择（覆盖表文件读）
    # ——不可委派的卡不付 model_override 的文件 IO。
    if not await is_delegable_card(state.work_id, state.card_id, cfg):
        return None
    raw = exec_backend_raw_of(state.work_id, state.card_id)
    backend = raw or BACKEND_AUTO
    if backend == BACKEND_SYSTEM:
        return None
    # [开闸范围] ``opt_in``：只有**显式**点了 auto 的卡才接管 ⇒
    # 存量卡默认留在系统自己的模型上，逐张接管（开闸零冲击）。
    if cfg.scope.mode == "opt_in" and raw is None:
        return None
    try:
        outcome = await run_delegated(state, cfg, backend)
    except Exception as exc:  # noqa: BLE001 委派失败绝不拖垮主链路
        logger.error("[wb_exec] 委派异常（回落系统模型）card=%s: %s",
                     state.card_id, exc, exc_info=True)
        if cfg.on_unavailable == "fail":
            state.error = f"WorkBuddy 执行体失败: {exc}"
            from ..message_runner import StepOutcome
            return StepOutcome("error", "wb_exec_failed")
        # **如实记账**：本轮实际走的是系统自己的模型（卡面必须显示，别装成在 WorkBuddy 上跑）
        await _record_fallback(state, f"委派异常：{exc}")
        # [轮号 2026-10-23 实跑教训] **不要动 state.rounds**！
        #   回落路径随后会做 ``state.rounds += 1``；我在这里写 0 ⇒ **每轮都被
        #   重置回 1** ⇒ 卡面"满屏 round 1"（用户 2026-10-23 16:50 当场指出）。
        #   ⇒ 回落时**原样保留**进 bridge 之前的轮号。
        state.rounds = _orig_rounds
        return None
    return outcome


# ===================================================================== 主流程
async def run_delegated(state: Any, cfg: WbExecConfig, backend: str) -> Any:
    """跑一次 WorkBuddy 委派回合。返回 ``StepOutcome("deliver", …)``。"""
    from ..message_runner import StepOutcome, _land_round_reply

    # **进程级**配比器（跨回合存活 ⇒ 轮询状态能推进；每回合新建会让配比退化成
    # 100% 打第一个候选，见 picker.get_picker 的说明）。
    ex = _Exec(state=state, cfg=cfg, picker=picker_mod.get_picker(cfg.candidates))
    ex.watchdog = Watchdog(
        connect_timeout_sec=cfg.watchdog.connect_timeout_sec,
        read_timeout_sec=cfg.watchdog.read_timeout_sec,
        busy_wait_sec=cfg.watchdog.busy_wait_sec,
    )
    # [轮号] 进 bridge 之前的轮号（任何回落路径都要原样还回去）
    _orig_rounds = state.rounds
    state.rounds = 1
    pinned = _parse_pinned(backend)
    cand: Optional[Candidate] = None
    cwd = _work_dir(state.work_id)
    if pinned:
        executor_id, model = pinned
        cand = Candidate(executor=executor_id, model=model, weight=1)
        ex.executor_id, ex.model = executor_id, model
    else:
        # ★ **优先级瀑布**（2026-11-04 用户令）：
        #   「先全部排 devin，你得测它并发，它排满了或额度用完了，就落 opencode，
        #     opencode 不行，再落 workbuddy」
        #   ⇒ **不再按权重分流**（配比会让还空着的 devin 只吃 40% ⇒ 浪费）。
        #   ⇒ 每一级都过三道**状态**闸：额度耗尽 / 实测没这模型 / 槽排满。
        #   ⇒ **占不到槽就立刻往下一级溢流**（不干等、不回落系统模型＝花钱）。
        cand, skips = await _select_candidate(cfg, state, cwd)
        for s in skips:
            if s.get("kind") == "saturated":
                # ★ **排满溢流**＝换了执行体 ⇒ 用 `wb_exec_fallback`（看得见
                #   "从谁落到谁"），不是 `unavailable`（那表示"这一路彻底不行"）。
                _emit(state, "wb_exec_fallback", {
                    "from": f"{s.get('executor')}:{s.get('model')}",
                    "to": (f"{cand.executor}:{cand.model}" if cand else ""),
                    "reason": s.get("reason") or "并发槽已排满",
                })
            else:
                _emit(state, "wb_exec_unavailable", {
                    "executor": s.get("executor"), "model": s.get("model"),
                    "reason": s.get("reason") or "不可用",
                })
        if cand is not None:
            ex.executor_id, ex.model = cand.executor, cand.model
        else:
            # 所有候选都**确定不可用**（额度耗尽 / 实测没有这模型）⇒
            # **安静地不委派**（回落系统模型，但**不**在事件流里刷噪音）
            logger.info("[wb_exec] 没有可用的执行体候选 ⇒ 本轮走系统自己的模型")
            # **如实记账**（卡面不能显示成"在 WorkBuddy 上跑"）
            await _record_fallback(
                state, "优先级表里的候选当前都不可用（额度耗尽 / 该执行体没有这个模型）")
            state.rounds = _orig_rounds
            return None

    _emit(state, "round_start", {"round": 1, "phase": "wb_exec",
                                 "executor": ex.executor_id, "model": ex.model})
    _emit(state, "wb_exec_start", {
        "executor": ex.executor_id, "model": ex.model,
        "backend": backend,
    })

    holder = _holder_id(state)
    try:
        await _run_with_fallback(state, cfg, ex, cand, cwd)
        # 记下**实际**路由到的（优先级瀑布选的 / 降级换过的）⇒
        # 卡面 UI 开工后显示的就是它。
        await _record_routed(state, ex)
        # ★ **真调成功 ⇒ 额度状态清掉 + 份额还回去**（状态驱动，不是时间到期）
        #   ⇒ 手动钉死一个"额度耗尽"的模型跑一轮成功 ⇒ 自动恢复全份额。
        _restore_quota(cfg, ex, ex.executor_id, ex.model)
    except WbAcpError as exc:
        logger.error("[wb_exec] 委派最终失败 card=%s: %s", state.card_id, exc)
        _emit(state, "wb_exec_error", {"error": str(exc)[:400],
                                       "resumed": ex.resumed,
                                       "fallbacks": ex.fallback_used})
        if cfg.on_unavailable == "fail":
            state.error = f"WorkBuddy 执行体失败: {exc}"
            return StepOutcome("error", "wb_exec_failed")
        # 安全默认：回落系统自己的模型（本次委派当作没发生）
        # **如实记账**：卡面显示"本轮实际走系统自己的模型（原因…）"，
        #   不能让人以为在 WorkBuddy 上跑（假信号）。
        #   **kind="slot_full"** ⇒ 这一轮是"并发槽已满"⇒ 下一轮先让路
        await _record_fallback(state, str(exc))
        # [轮号] **原样还回去**——系统路径的 step() 随后会做 rounds += 1；
        # 我在这里写 0 ⇒ 每轮都被重置回 1 ⇒ 卡面"满屏 round 1"。
        state.rounds = _orig_rounds
        return None
    finally:
        # ★ **释放槽位预留**（幂等）：这一轮结束（或换候选）后，
        #   这个执行体的占用数要降下来 ⇒ 后面的卡才不会被误判成"排满"。
        get_registry().release_reserve(ex.executor_id, holder)

    # 成功：把执行体产出落成"这一轮的回复"
    final_text = "".join(ex.text_parts).strip()
    if not final_text:
        final_text = (f"（WorkBuddy 执行体 {ex.executor_id}/{ex.model} 未产出正文；"
                      f"工具调用 {ex.tool_calls} 次）")
    state.model_used = ex.model
    # **只设 final_reply，不塞 analysis_parts** —— T14 拼接是
    # ``"\n\n".join(analysis_parts + [final_reply])``，两个都设会把正文拼两遍
    # （analysis_parts 在既有链路里只收**工具轮**的正文，末轮正文走 final_reply）。
    state.final_reply = final_text
    try:
        await _land_round_reply(state, final_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[wb_exec] 正文事件落盘失败（不阻断）: %s", exc)

    _emit(state, "wb_exec_done", {
        "executor": ex.executor_id, "model": ex.model,
        "session_id": ex.session_id, "tool_calls": ex.tool_calls,
        "resumed": ex.resumed, "fallbacks": ex.fallback_used,
        "elapsed_sec": round(time.monotonic() - ex.watchdog.started_at, 1),
    })
    await _finish_card(state, ex, final_text)
    return StepOutcome("deliver", "wb_exec")


async def _select_candidate(cfg: WbExecConfig, state: Any,
                             cwd: str) -> tuple[Optional[Candidate], list[dict]]:
    """**优先级瀑布**：按优先级从高到低，挑一个**现在就能接下**的候选。

    ★ **用户令（2026-11-04）**：「先全部排 devin，你得测它并发，它排满了或额度用完了，
    就落 opencode，opencode 不行，再落 workbuddy」
    ⇒ **不再按权重分流**：配比会让还空着的 devin 只吃 40% ⇒ 浪费。

    **每一级过三道闸（全部是状态判定，不是时间闸）**：
      ① **额度耗尽**（上游明确报的 quota / free_tier_limit）⇒ 跳过；
      ② **实测没有这个模型**（真调失败报 Model unavailable）⇒ 跳过；
      ③ **槽排满了**（占不到预留）⇒ **立刻溢流到下一级**（不干等）。

    **全部排满怎么办**：**不回落系统模型**（＝花钱）⇒ 在**最高优先级那一路排队**
    （强制预留 ⇒ 占用数照样算得对 ⇒ 后面的卡会正确地溢流到下一级）。

    返回 ``(候选, 跳过的原因列表)``；候选为 ``None`` ⇒ 全部候选都**确定不可用**
    （这时才回落系统模型，并如实记账）。
    """
    reg = get_registry()
    holder = _holder_id(state)
    order = picker_mod.priority_order(cfg.candidates)
    notes: list[dict] = []
    wait_target: Optional[Candidate] = None
    for c in order:
        if reg.is_exhausted(c.executor, c.model):
            why = reg.exhausted_reason(c.executor, c.model) or "额度耗尽"
            logger.info("[wb_exec] %s/%s 额度耗尽（%s）⇒ 跳到下一优先级",
                        c.executor, c.model, why)
            notes.append({"executor": c.executor, "model": c.model,
                          "kind": "quota",
                          "reason": f"额度耗尽（{why}）"})
            continue
        if reg.model_unavailable(c.executor, c.model):
            logger.info("[wb_exec] %s 实测没有模型 %s ⇒ 跳到下一优先级",
                        c.executor, c.model)
            notes.append({"executor": c.executor, "model": c.model,
                          "kind": "unavailable",
                          "reason": "该执行体实测没有这个模型"})
            continue
        if await reg.try_reserve(c.executor, c.model, cwd, holder):
            logger.info("[wb_exec] 选中候选 %s/%s（优先级 %d）",
                        c.executor, c.model, c.priority)
            return c, notes
        # **槽排满 ⇒ 立刻溢流**（用户令：「它排满了…就落 opencode」）
        notes.append({"executor": c.executor, "model": c.model,
                      "kind": "saturated",
                      "reason": "并发槽已排满"})
        if wait_target is None:
            wait_target = c
    if wait_target is not None:
        # **全都排满 ⇒ 在最高优先级那一路排队**（不回落系统模型＝不花钱）。
        # 强制预留（即使超编）⇒ 占用数算得对 ⇒ 后来的卡会正确地溢流到下一级。
        await reg.try_reserve(wait_target.executor, wait_target.model, cwd,
                              holder, force=True)
        logger.info("[wb_exec] 所有执行体都排满 ⇒ 在 %s/%s 排队等槽",
                    wait_target.executor, wait_target.model)
        return wait_target, notes
    return None, notes


async def _run_with_fallback(state: Any, cfg: WbExecConfig, ex: _Exec,
                             cand: Optional[Candidate], cwd: str) -> None:
    """跑任务；quota ⇒ 切 hy3 重试；连接类失败 ⇒ 重连续跑（上限 ``max_resume``）。

    **三种失败分开处理**（2026-10-23 实测踩出来的）：
    - **模型不存在**（这个版本没这个模型）⇒ **换候选**，不算失败次数；
    - **额度耗尽**（``category=="quota"``）⇒ 走 fallback 链（同侧 hy3 → 另一侧 hy3）；
    - **连接/传输类** ⇒ **续跑**：能恢复就恢复原会话（不重发已完成的工作），
      恢复不了才新建会话重跑。
    """
    registry = get_registry()
    # ★ **降级链 = 优先级顺序**（2026-11-04 用户令：「opencode 不行，再落 workbuddy」）
    #   ⇒ devin → opencode → workbuddy(glm) → … → hy3（表内最后一级）。
    #   ⇒ **不是**老的"只跳 fallback.models"（那个只会在 hy3 之间跳）。
    chain = picker_mod.cascade_chain(cfg.candidates, cand) if cand else []
    chain_idx = 0
    current = cand
    attempt = 0
    tried: set[tuple[str, str]] = set()
    holder = _holder_id(state)

    async def _advance(failed: Candidate) -> Optional[Candidate]:
        """**往下一优先级走**（跳过排满 / 额度尽 / 没这模型的候选）。

        ★ 每一级都要**先占到位**（:meth:`try_reserve`）⇒ 排满了就**继续往下**，
        而不是停在这一级干等。
        """
        nonlocal chain_idx
        while chain_idx < len(chain):
            nxt = chain[chain_idx]
            chain_idx += 1
            if (nxt.executor, nxt.model) in tried:
                continue
            if registry.is_exhausted(nxt.executor, nxt.model):
                continue
            if registry.model_unavailable(nxt.executor, nxt.model):
                continue
            if await registry.try_reserve(nxt.executor, nxt.model, cwd, holder):
                return nxt
            logger.info("[wb_exec] %s/%s 排满 ⇒ 继续往下一优先级",
                        nxt.executor, nxt.model)
        return None
    while True:
        if current is None:
            raise WbAcpError("没有可用候选（配比表为空）")
        ex.executor_id, ex.model = current.executor, current.model
        # **账号未登录的执行体直接跳过**（进程内记忆）——
        # 没登录时重连多少次都没用，别浪费一张 coder 卡 3 次重连。
        if registry.is_unauthenticated(current.executor):
            # **文案要说实话**（2026-10-23 实跑教训）：这里的"未登录"其实是
            # **执行体被熔断**（上一次撞到过鉴权失败）——桌面端登录态可能好好的。
            # 早先写"请重新登录 WorkBuddy 桌面端"⇒ 把人引到错误的方向。
            raise WbAcpError(
                f"执行体 {current.executor} 暂时不可用"
                f"（上次鉴权失败，已熔断；稍后自动恢复）")
        try:
            # ★ acquire：优先 **CLI 进程池**（一进程一 agent 槽 ⇒ 真并行）；
            #   没有 ⇒ 桌面端主进程自己监听的那个网关（并发 1）。
            # **★ 必须和下面的活儿在同一个 try 里**（2026-10-23 20:55 修）：
            #   早先 acquire 单独一个 try，它抛的"执行体不可用"**穿过了**下面的
            #   异常处理 ⇒ 换候选的逻辑根本没机会跑 ⇒ 整张卡白失败。
            if _is_oc_executor(cfg, current.executor):
                # **opencode 走另一条传输**（HTTP + SSE，不是 ACP）
                await _run_opencode(state, cfg, ex, current, cwd, registry)
                return
            if _is_devin_executor(cfg, current.executor):
                # **Devin 走 CLI 进程**（本地 agent ⇒ 能写本机工作目录）
                await _run_devin(state, cfg, ex, current, cwd, registry)
                return
            if _is_cc_executor(cfg, current.executor):
                # **Command Code 走 node 起的 CLI**（NDJSON 流式事件）
                await _run_cc(state, cfg, ex, current, cwd, registry)
                return
            gw = await registry.acquire(current.executor, cwd, current.model)
            gw = await registry.ensure_connection(gw)
            client = AcpClient(
                gw.base_url, timeout=float(cfg.watchdog.read_timeout_sec),
                connect_timeout=float(cfg.watchdog.connect_timeout_sec),
                on_notification=lambda n: _on_notification(ex, n),
                on_keepalive=ex.watchdog.note_keepalive,
                extra_headers=gw.headers(),
            )
            # [槽位 2026-10-24] **占槽，不排队**：
            # [槽位 2026-10-24] **槽满 ⇒ 排队等**（用户令：「槽满就他妈排队」）。
            #   ★ **不换候选、不回落系统模型** —— 让路＝走系统自己的模型＝**花钱**，
            #     用户不接受 ⇒ 一律在 WorkBuddy 侧排队等。
            #   ★ 排队是**可见的**：emit「排队中」⇒ 人看得见它在等（不是"卡死"）。
            #   ★ 真·卡死的根因**不是排队**，而是**槽被永久占着**
            #     （CLI 卡在等人工批准 ⇒ 没人批 ⇒ 槽不释放）⇒ 根治在
            #     「**自动批准权限请求**」，不在这里。
            holder = _holder_id(state)
            sem = registry.semaphore_for(gw.base_url)
            # ★ **先登记等待者再判断**（2026-11-03 修竞态）：
            #   早先只看 ``sem.locked()`` ⇒ 两张卡都可能在对方 acquire **之前**
            #   检查到"没锁" ⇒ 实际排了队（串行跑完）但**没人发过"排队中"**
            #   ⇒ 卡面上看不见它在等（看着像"卡住"）。
            # ⇒ 现在：「槽被占」**或**「已经有人在等」⇒ 我就得排队 ⇒ 发"排队中"。
            _n_wait = registry.note_waiter(gw.base_url)
            try:
                if sem.locked() or _n_wait > 1:
                    _emit(state, "wb_exec_queued", {
                        "executor": current.executor,
                        "model": current.model,
                        "waiting_for": registry.slot_holder(gw.base_url),
                    })
                await sem.acquire()
            finally:
                registry.clear_waiter(gw.base_url)
            registry.mark_slot(gw.base_url, holder)
            # ★ **运行时已经占上槽了** ⇒ **释放选择期的预留**
            #   （否则占用数被算两遍 ⇒ 这个执行体会被误判成"排满"一倍）。
            registry.release_reserve(current.executor, holder)
            try:
                async with client:
                    # **复用连接**（ACP 连接没有 disconnect 端点，maxConnections=32
                    # ⇒ 每张卡新建连接迟早撞上限）。
                    if gw.connection_id and gw.session_token:
                        client.connection_id = gw.connection_id
                        client.session_token = gw.session_token
                    else:
                        await client.connect()
                        await client.initialize()
                    if attempt > 0 and ex.session_id:
                        # **续跑**：恢复上一次那个会话（接着它做到哪来），
                        # 而不是从头新建（新建 ⇒ agent 从零开始 ⇒ 重做）。
                        sid, models = await _resume_session(
                            client, ex.session_id, cwd)
                        if not sid:
                            sid, models = await _ensure_session(
                                client, state.card_id, cwd)
                    else:
                        sid, models = await _ensure_session(
                            client, state.card_id, cwd)
                    ex.session_id = sid
                    # [任务文本] **建好会话之后**再拼 ⇒ 分段标记里的 run=
                    # 是真实会话 id（早先提前拼 ⇒ 永远 run=pending）。
                    task_text = await _build_task_text(state, cfg, ex, cwd)
                    # 记下该执行体的模型目录（**只是诊断用**：这是 session/load
                    # 返回的**被过滤过的本地列表**，不能拿来判断"有没有这个模型"）。
                    if models:
                        registry.remember_models(current.executor, models)
                    # **★ 不再拿本地目录判"模型不存在"**（2026-10-23 20:45 实测更正）：
                    #   国版 session/load 只回 5 个，可 hy3 / step-5-preview **实测都能调**
                    #   ⇒ 早先这个检查把国版那 30% 权重全跳过了。
                    #   ⇒ 现在只认**实测证据**：真调失败报 "Model unavailable" 才算。
                    await _apply_guards(client, sid, cfg, gw.source)
                    await client.set_config_option(sid, "model", current.model)
                    ex.model = current.model
                    _emit(state, "wb_exec_model", {
                        "executor": current.executor, "model": current.model,
                        "session_id": sid,
                    })
                    result = await client.prompt(
                        sid, task_text,
                        # ★ **不设读超时**（2026-11-03）：ACP 的 prompt 响应是
                        #   **SSE 流**，httpx 的 read timeout 判的是「两帧之间多久
                        #   没数据」——agent 跑大 bash 时**本来就不发帧** ⇒ 有限读
                        #   超时会把**正在干活的长任务**误判成「连接断了」⇒ 续跑
                        #   （重发整段 prompt）⇒ 又超时 ⇒ 30 分钟没动静。
                        #   实测 wb-cn/hy3 总耗时中位 373.8s、最大 3351s ⇒ 必然踩。
                        # ⇒ **判死活只靠状态与连接事件**（agentPhase / 在途工具 /
                        #   吐字 / SSE 结束 / httpx 异常）。
                        read_timeout=None,
                    )
                    # ★ **失败藏在返回体里**（2026-10-23 18:10 实测）：
                    #   WorkBuddy 把鉴权失败**不抛异常**，而是返回
                    #   ``{"stopReason":"refusal", "_meta":{"codebuddy.ai/errorMessage":
                    #   "{\"code\":-32000,\"message\":\"Authentication required\",…
                    #   \"category\":\"auth\"}"}}``。
                    #   ⇒ 不检查的话，这张卡会**假装成功**（0 工具 0 正文），
                    #   卡面显示"已完成"、实际啥也没干。必须当失败处理。
                    _raise_if_prompt_failed(result)
                    # ★ **空结果守卫**（2026-11-01 加）：0 工具 + 0 正文 ⇒ **一定没干成活**
                    #   （现场 card_143 **首轮**：wb-intl/deepseek-v4.1-flash 交回空回执
                    #     ——工具调用 0 次、无正文、无产物 ⇒ 系统却发了 ``done``
                    #     且 ``model_calls=0`` ⇒ **这张卡「假装成功」**，父卡只能靠
                    #     人工读事件流才发现"未开工"）。
                    # ⇒ 现在**判定失败** ⇒ 换候选/如实报错，不许当成功。
                    if ex.tool_calls == 0 and not "".join(ex.text_parts).strip():
                        raise WbAcpError(
                            "WorkBuddy 执行体返回空结果（0 工具 0 正文）⇒ 判定失败，不当成功")
            finally:
                # **★ 槽位一定要还回去**（异常/取消/成功都还）——
                #   不还 ⇒ 这个槽**永远**占着 ⇒ 后面的卡全被挡 ⇒ 又变回"卡死"。
                registry.release_slot(gw.base_url, holder)
                sem.release()
            return
        except WbModelUnavailable as exc:
            # 该版本没这个模型（**实测出来的**）⇒ **往下一优先级**走
            # （只在优先级表内跳 ⇒ 不换表外付费模型）。
            registry.note_model_unavailable(current.executor, current.model)
            tried.add((current.executor, current.model))
            registry.release_reserve(current.executor, holder)
            nxt = await _advance(current)
            if nxt is None:
                raise WbAcpError(
                    f"优先级表里的模型在本机都不存在: {exc}") from exc
            ex.fallback_used.append(f"{nxt.executor}:{nxt.model}")
            _emit(state, "wb_exec_fallback", {
                "from": f"{current.executor}:{current.model}",
                "to": f"{nxt.executor}:{nxt.model}",
                "reason": str(exc)[:300],
            })
            logger.warning("[wb_exec] 模型不存在 ⇒ 换下一优先级：%s → %s（%s）",
                           f"{current.executor}:{current.model}",
                           f"{nxt.executor}:{nxt.model}", exc)
            current = nxt
            continue
        except WbExecutorUnavailable as exc:
            # **这个执行体现在用不了**（网关没了/没登录）⇒ **往下一优先级**走
            # （实测 2026-10-23 20:50：桌面端托管的网关会自己消失）。
            tried.add((current.executor, current.model))
            registry.release_reserve(current.executor, holder)
            nxt = await _advance(current)
            if nxt is None:
                raise WbAcpError(
                    f"优先级表里的执行体都暂时用不了: {exc}") from exc
            ex.fallback_used.append(f"{nxt.executor}:{nxt.model}")
            _emit(state, "wb_exec_fallback", {
                "from": f"{current.executor}:{current.model}",
                "to": f"{nxt.executor}:{nxt.model}",
                "reason": str(exc)[:300],
            })
            logger.warning("[wb_exec] 执行体不可用 ⇒ 换下一优先级：%s → %s（%s）",
                           f"{current.executor}:{current.model}",
                           f"{nxt.executor}:{nxt.model}", exc)
            current = nxt
            continue
        except WbAcpError as exc:
            if exc.is_user_interruption:
                _emit(state, "wb_exec_interrupted", {"error": str(exc)[:300]})
                raise
            if exc.is_auth_required:
                # **不可重试**：这次确实是鉴权失败（文本命中）。
                # 标记该执行体**暂时**不可用（熔断 60 秒，不永久）⇒
                # 之后它的卡秒级回落，但**到期自动恢复**（一次误判不会瘫掉通道）。
                registry.mark_unauthenticated(current.executor)
                _emit(state, "wb_exec_unavailable", {
                    "executor": current.executor,
                    "reason": "鉴权失败（桌面端登录态失效），已熔断，稍后自动恢复",
                    "error": str(exc)[:300],
                })
                raise
            if exc.is_quota:
                # ★ **上游明确说额度用完了**（ACP 的
                #   ``_meta['codebuddy.ai/errorMessage'].category == "quota"``）
                #   ⇒ 记进额度状态表 ⇒ **这个候选之后让路**（跳到下一优先级）
                #   ⇒ 不用每次都先失败一轮才换（那会白花一次请求 + 一段时间）。
                registry.mark_exhausted(current.executor, current.model, "quota")
                _emit(state, "wb_exec_unavailable", {
                    "executor": current.executor, "model": current.model,
                    "reason": "额度耗尽，已让路给下一优先级",
                    "error": str(exc)[:300],
                })
            if exc.is_quota:
                # ★ **额度尽 ⇒ 立刻往下一优先级走**（用户令：「额度用完了，就落 opencode」）
                tried.add((current.executor, current.model))
                registry.release_reserve(current.executor, holder)
                nxt = await _advance(current)
                if nxt is None:
                    raise WbAcpError(
                        f"候选全部额度耗尽（{ex.fallback_used}）: {exc}") from exc
                ex.fallback_used.append(f"{nxt.executor}:{nxt.model}")
                logger.warning("[wb_exec] 额度/过载 ⇒ 落下一优先级：%s → %s（%s）",
                               f"{current.executor}:{current.model}",
                               f"{nxt.executor}:{nxt.model}", exc)
                _emit(state, "wb_exec_fallback", {
                    "from": f"{current.executor}:{current.model}",
                    "to": f"{nxt.executor}:{nxt.model}",
                    "reason": str(exc)[:300],
                })
                current = nxt
                continue
            # 连接/传输类 ⇒ **重试同一个执行体**（★ 2026-11-04 改）
            #   原来这里是"换一次候选"⇒ **每失败一次就换一个执行体从头干**
            #   ⇒ 现场表现：反反复复失败、反反复复换人接手（card_211 白跑 4 轮）。
            #   ⇒ "排满"是**容量**问题（该溢流到下一优先级）；
            #   "传输失败"是**这一路自身**的问题（该重试它，别换人）。
            attempt += 1
            if attempt > cfg.watchdog.max_resume:
                raise WbAcpError(
                    f"续跑 {attempt} 次仍未成功: {exc}") from exc
            ex.resumed = attempt
            logger.warning("[wb_exec] 第 %s 次续跑（同一执行体 %s/%s）：%s",
                           attempt, current.executor, current.model, exc)
            _emit(state, "wb_exec_resume", {
                "attempt": attempt, "error": str(exc)[:300],
            })
            continue
        except RuntimeError as exc:
            # **普通 RuntimeError**（不是 ACP 错误）⇒ 多半是"发现网关失败"
            # （桌面端网关被回收 / 没起）⇒ 当成"这个执行体不可用"⇒ **换下一优先级**。
            # （**必须放在 WbAcpError 之后**：WbAcpError 是 RuntimeError 的子类。）
            tried.add((current.executor, current.model))
            registry.release_reserve(current.executor, holder)
            nxt = await _advance(current)
            if nxt is None:
                raise WbExecutorUnavailable(
                    f"优先级表里的执行体都暂时用不了: {exc}") from exc
            ex.fallback_used.append(f"{nxt.executor}:{nxt.model}")
            _emit(state, "wb_exec_fallback", {
                "from": f"{current.executor}:{current.model}",
                "to": f"{nxt.executor}:{nxt.model}",
                "reason": str(exc)[:300],
            })
            logger.warning("[wb_exec] 执行体不可用（%s）⇒ 换下一优先级：%s → %s",
                           exc, f"{current.executor}:{current.model}",
                           f"{nxt.executor}:{nxt.model}")
            current = nxt
            continue


def _raise_if_prompt_failed(result: Any) -> None:
    """**prompt 返回体里的失败 ⇒ 抛异常**（别把失败当成功）。

    2026-10-23 18:10 实测（国际版网关上带凭据跑出来的真实返回）::

        {"stopReason": "refusal",
         "_meta": {"codebuddy.ai/errorMessage":
                   "{\\"code\\":-32000,\\"message\\":\\"Authentication required\\",
                     \\"data\\":{\\"details\\":\\"Authentication required. Please use
                     /login command to sign in to your account\\",\\"category\\":\\"auth\\"}}",
                   "codebuddy.ai/outcome": "FAILED_MODEL_REQUEST"}}

    ⇒ **它不抛异常**，只是把错误塞进 ``_meta``；不检查 ⇒
    这张卡会以"0 工具 0 正文"**假装成功**（卡面显示已完成）。

    **★ "这个模型不可用"是另一类**（2026-10-23 20:45 实测）::

        {"stopReason": "refusal", "_meta": {"codebuddy.ai/outcome":
           "FAILED_MODEL_REQUEST", "codebuddy.ai/errorMessage":
           "{\\"code\\":-32603,\\"message\\":\\"Internal error\\",\\"data\\":
           {\\"statusCode\\":400,\\"displayMsg\\":{\\"en\\":\\"Model unavailable.
            Please switch models\\",\\"zh\\":\\"当前模型不可用...\\"}}}}}

    ⇒ 抛 :class:`WbModelUnavailable` ⇒ **换配比表里的另一个候选**
    （而不是让这张卡失败、也不是静默回落系统模型）。
    """
    meta = (result or {}).get("_meta") if isinstance(result, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    raw = str(meta.get("codebuddy.ai/errorMessage") or "")
    if not raw:
        return
    low = raw.lower()
    # **模型不可用**（含中英文文案）⇒ 换候选
    if ("model unavailable" in low or "当前模型不可" in raw
            or "please switch models" in low):
        raise WbModelUnavailable(f"该执行体没有这个模型: {raw[:300]}")
    if ("authentication required" in low or '"category":"auth"' in low
            or "not logged in" in low or "please use /login" in low):
        raise WbAcpError(f"鉴权失败（桌面端登录态失效）: {raw[:300]}",
                         code=-32000, category="auth")
    if '"category":"quota"' in low or "quota" in low:
        raise WbAcpError(f"额度/过载: {raw[:300]}", code=-32000,
                         category=acp_client.CATEGORY_QUOTA)
    # 其它失败（如 FAILED_MODEL_REQUEST 的别的原因）⇒ 如实抛，别静默
    raise WbAcpError(f"WorkBuddy 执行体返回失败: {raw[:300]}", code=-32000)


def _holder_id(state: Any) -> str:
    """槽位占用者标识（谁占着这个 agent 槽）。

    带 ``id(state)`` ⇒ 同一张卡的**不同轮次**互不干扰（上一轮的槽没还回来时，
    新一轮能另占一个，不会被自己挡住）。
    """
    return f"{getattr(state, 'work_id', '?')}/{getattr(state, 'card_id', '?')}/{id(state)}"


async def _cleanup_session(cfg: WbExecConfig, state: Any, ex: "_Exec",
                            current: Candidate, cwd: str, *,
                            session_id: str = "", client: Any = None,
                            atif: Any = None) -> None:
    """★ **卡片做完后 ⇒ 在这个执行体自己的客户端里清掉残留**。

    **红线（用户令：「千万别删除对话」「千万别删错对话」）**——
    只删**我们自己刚建的那一条**（精确 id ＋ 删前验身份），
    桌面端客户端与用户自己的会话**一律不碰**。
    详细纪律见 :mod:`src.api.wb_exec.cleanup`。

    ★ **清理失败只记日志，不影响卡片结果**。
    """
    ex_cfg = cfg.executor(current.executor)
    if ex_cfg is None or not getattr(ex_cfg, "cleanup_after_run", False):
        return
    from . import cleanup as cleanup_mod
    try:
        rep = await cleanup_mod.cleanup_after_run(
            current.executor, session_id=session_id, work_dir=cwd,
            client=client, cli_exe=str(getattr(ex_cfg, "devin_cli_exe", "") or ""),
            card_id=str(getattr(state, "card_id", "") or ""), atif=atif)
    except Exception as exc:  # noqa: BLE001 清理永远不能拖垮主流程
        logger.warning("[wb_exec] 清理会话异常（已忽略）：%s", exc)
        return
    if rep.get("deleted"):
        logger.info("[wb_exec] 客户端清理完成 %s：%s", current.executor,
                    {k: v for k, v in rep.items() if k != "executor"})
    elif rep.get("skipped"):
        logger.info("[wb_exec] 未清理（%s）：%s", current.executor, rep["skipped"])


def _is_oc_executor(cfg: WbExecConfig, executor_id: str) -> bool:
    """这个执行体是不是走 **opencode**（HTTP + SSE 传输，不是 ACP）。"""
    ex = cfg.executor(executor_id)
    return bool(ex and ex.oc_serve_exe and ex.oc_pool_size > 0)


def _is_devin_executor(cfg: WbExecConfig, executor_id: str) -> bool:
    """这个执行体是不是走 **Devin**（CLI 进程：本地 agent ⇒ 能写本机目录）。"""
    ex = cfg.executor(executor_id)
    return bool(ex and ex.devin_cli_exe)


def _is_cc_executor(cfg: WbExecConfig, executor_id: str) -> bool:
    """这个执行体是不是走 **Command Code**（node 起的 CLI + NDJSON 事件流）。"""
    ex = cfg.executor(executor_id)
    return bool(ex and getattr(ex, "cc_cli_entry", ""))


async def _run_cc(state: Any, cfg: WbExecConfig, ex: "_Exec",
                  current: Candidate, cwd: str, registry: Any) -> None:
    """跑一轮 **Command Code**（2026-11-04 加，第五路候选）。

    **事件是流式的**（NDJSON）：工具调用/正文/思考**边跑边出** ⇒
    卡面上能实时看到它在干什么（比 Devin 的 ATIF 事后解析强）。

    **★ 实测出来的三个坑**（见 :mod:`src.api.wb_exec.cc_client` 的文件头）：
    ``--yolo`` 必须给旗标（``--permission-mode yolo`` 不够）；
    ``--tools-all`` 必须给（print 模式默认扣工具）；
    目录里的 FREE 标签会过期 ⇒ **只认真调出来的**。
    """
    runner = registry.cc_runner(current.executor)
    if runner is None:
        raise WbExecutorUnavailable(
            f"执行体 {current.executor} 没有配 Command Code 入口")
    sem = registry.cc_sem(current.executor)
    task_text = await _build_task_text(state, cfg, ex, cwd)
    ex.executor_id, ex.model = current.executor, current.model

    def _on_event(kind: str, payload: dict) -> None:
        """**边跑边映射**成我们已有的事件（前端零改动）。"""
        if kind == "tool_call":
            ex.tool_calls += 1
            _emit(state, "tool_call", {
                "tool": str(payload.get("name") or ""), "target": "",
                "arguments": payload.get("input") or {}, "round": state.rounds,
            })
        elif kind == "tool_result":
            ex.tool_ok += 1
            _emit(state, "tool_result", {
                "tool": str(payload.get("name") or ""), "ok": True,
                "status": str(payload.get("status") or "completed"),
                "summary": str(payload.get("output") or "")[:2000],
                "round": state.rounds,
            })
        elif kind == "thinking":
            _emit(state, "reasoning_chunk", {
                "text": str(payload.get("text") or "")[:2000],
                "round": state.rounds,
            })

    async with sem:
        _emit(state, "wb_exec_model", {
            "executor": current.executor, "model": current.model,
            "session_id": "", "transport": "command-code",
        })
        res = await runner.run(task_text, cwd, model=current.model,
                               on_event=_on_event)

    text = str(res.get("text") or "")
    if text:
        ex.text_parts.append(text)
        _emit(state, "assistant_text", {"content": text, "round": state.rounds})
    errs = [str(e) for e in (res.get("errors") or [])]
    # ★ **额度耗尽要能认出来**（与 Devin 同一条纪律）⇒ 记额度状态 ⇒ 下一优先级接管。
    for e in errs:
        if _looks_exhausted(e):
            registry.mark_exhausted(current.executor, current.model,
                                    "command-code 报额度/配额用完")
            break
    if not res.get("ok"):
        raise WbAcpError(
            f"Command Code 执行失败（rc={res.get('rc')}）："
            f"{(' | '.join(errs) or '无正文')[:300]}")
    # ★ **空结果守卫**（同 Devin）：0 工具 + 0 正文 ⇒ 一定没干成活 ⇒ 判失败。
    if ex.tool_calls == 0 and not text.strip():
        raise WbAcpError(
            "Command Code 执行体返回空结果（0 工具 0 正文）⇒ 判定失败，不当成功")


async def _run_devin(state: Any, cfg: WbExecConfig, ex: "_Exec",
                     current: Candidate, cwd: str, registry: Any) -> None:
    """跑一轮 **Devin**（2026-11-03 加、2026-11-04 改本地模式）。

    **形态**（本地 agent，**不是** ``--cloud``）::

        devin -p --prompt-file F --export O --permission-mode bypass
              --model swe-2-max --respect-workspace-trust false

    ★ **为什么不能走云端**（2026-11-04 实测）：``--cloud`` 跑在 **Devin 的 VM** 里
    ⇒ **看不到本机的 ``works\\<work>`` 目录** ⇒ agent 只会回一句
    「这是你本机的路径，请批准本地访问」⇒ **工具调用 0 次** ⇒ **这张卡假装成功**
    （现场 card_010：tool_calls=0、22.3s、实际啥也没干）。
    ⇒ 本地 agent 跑在**本机** ⇒ 能直接读写卡的工作目录（实测：ATIF 11 步、
      文件真写出）。

    ★ **模型用候选表里的**（候选表是唯一事实源）：``current.model``
    （如 ``swe-2-max``）⇒ **别写家族名**（``swe-2`` 会落到家族默认档，
    实测解析成 ``swe-2-high``）。

    **事件映射**：ATIF 里的工具调用 → ``tool_call`` / ``tool_result``；
    最终正文 → ``assistant_text`` ⇒ 前端零改动。
    """
    runner = registry.devin_runner(current.executor)
    if runner is None:
        raise WbExecutorUnavailable(f"执行体 {current.executor} 没有配 Devin CLI")
    sem = registry.devin_sem(current.executor)
    task_text = await _build_task_text(state, cfg, ex, cwd)
    ex.executor_id, ex.model = current.executor, current.model
    # ★ **实时事件**（2026-11-04）：Devin 的 ATIF 要等**进程退出**才写出来
    #   ⇒ 卡面在跑的这几分钟**一片空白**（用户现场：「页面上没有任何回应的？」）
    #   ⇒ 这里并行轮询 Devin 的会话库 ⇒ **边跑边**把工具调用/结果/思考发到卡面。
    live: dict = {"tools": 0, "text": "", "texts": []}

    def _on_event(kind: str, payload: dict) -> None:
        if kind == "tool_call":
            live["tools"] += 1
            ex.tool_calls += 1
            _emit(state, "tool_call", {
                "tool": str(payload.get("name") or ""), "target": "",
                "arguments": payload.get("input") or {}, "round": state.rounds,
                # ★ `live="log"` ⇒ 这条是**日志尾随**来的（只有工具名，
                #   参数/结果等收工的 ATIF）⇒ 卡面上标出来，不假装有明细。
                "live": str(payload.get("live") or ""),
            })
        elif kind == "tool_result":
            ex.tool_ok += 1
            _emit(state, "tool_result", {
                "tool": str(payload.get("name") or ""), "ok": True,
                "status": "completed",
                "summary": str(payload.get("summary") or "")[:2000],
                "round": state.rounds,
            })
        elif kind == "thinking":
            _emit(state, "reasoning_chunk", {
                "text": str(payload.get("text") or "")[:2000],
                "round": state.rounds,
            })
        elif kind == "text":
            live["text"] = str(payload.get("text") or "")
            live["texts"].append(_norm_text(live["text"]))
            _emit(state, "assistant_text", {
                "content": live["text"], "round": state.rounds,
            })

    async with sem:
        _emit(state, "wb_exec_model", {
            "executor": current.executor, "model": current.model,
            "session_id": "", "transport": "devin-cli",
        })
        res = await runner.run(task_text, cwd, model=current.model,
                               marker=_segment_marker(cfg, state, ex),
                               on_event=_on_event)
    # ★ **ATIF 兜底**：只有实时流没出事件时（老路径/没落库）才用它补
    #   ⇒ **不重复**发同一批工具调用。
    if not live["tools"]:
        _tools = (res.get("atif") or {}).get("tools") or []
        for t in _tools:
            st = str(t.get("status") or "").lower()
            ok = st in ("completed", "success", "ok", "done", "")
            _emit(state, "tool_call", {
                "tool": str(t.get("name") or ""), "target": "",
                "arguments": t.get("input") or {}, "round": state.rounds,
            })
            _emit(state, "tool_result", {
                "tool": str(t.get("name") or ""), "ok": ok,
                "status": st or "completed",
                "summary": str(t.get("output") or "")[:2000], "round": state.rounds,
            })
            ex.tool_calls += 1
            if ok:
                ex.tool_ok += 1
    text = str(res.get("text") or "")
    if text:
        ex.text_parts.append(text)
        # ★ **实时发过正文 ⇒ 收口不再发**（实时那条就是最终正文；
        #   重发一遍 ⇒ 卡面上同一段话出现两次）。
        #   实时没发过（老路径/没落库）⇒ 用 ATIF 兜底，保证卡面至少有正文。
        if not (live.get("texts") or []):
            _emit(state, "assistant_text", {"content": text, "round": state.rounds})
    # ★ **额度耗尽要能认出来**（2026-11-04）：Devin 报额度用完时，
    #   文本里会带上游自己的措辞 ⇒ 记进额度状态表 ⇒ **这个候选之后让路**
    #   ⇒ 流量自动溢到下一优先级（用户令：「额度用完了，就落 opencode」）。
    _err_text = f"{res.get('stderr') or ''}\n{res.get('stdout') or ''}\n{text}"
    if _looks_exhausted(_err_text):
        registry.mark_exhausted(current.executor, current.model,
                                "devin 报额度/配额用完")
    # ★ **用完就清**：在 **Devin 自己的客户端**里删掉**我们这一条**会话
    #   （红线：只按精确 id 删我们自己刚建的那一条；用户自己的对话一个不碰
    #   —— 见 cleanup.py 的六条纪律）。**清理失败不影响卡片结果**。
    ex.session_id = str(res.get("session_id") or "")
    await _cleanup_session(cfg, state, ex, current, cwd,
                           session_id=ex.session_id, atif=res.get("atif"))
    if not res.get("ok"):
        raise WbAcpError(
            f"Devin 执行失败（rc={res.get('rc')}）："
            f"{str(res.get('stderr') or res.get('stdout') or '')[:300]}")
    # ★ **空结果守卫**（与 ACP 那边同一条纪律，2026-11-04 加）：
    #   **0 工具 + 0 正文 ⇒ 一定没干成活** ⇒ 判失败、往下一优先级走，
    #   **不许当成功**（否则卡面显示"已完成"，实际啥也没干）。
    if ex.tool_calls == 0 and not text.strip():
        raise WbAcpError(
            "Devin 执行体返回空结果（0 工具 0 正文）⇒ 判定失败，不当成功")


#: 上游"额度/配额用完了"的各种说法（**上游自己的措辞**，实测收集）。
#: ⇒ 命中任一个 ⇒ 记进额度状态表 ⇒ 该候选**让路**（不是每次都先失败一轮）。
_QUOTA_WORDS = (
    "free usage", "free usage limit", "free limit", "free_tier_limit",
    "quota", "rate limit", "exceeded your current quota",
    "insufficient_quota", "额度", "配额",
)


def _looks_exhausted(msg: str) -> bool:
    """这段上游报错**说的是额度/配额用完了**吗？（按**上游自己的措辞**判定）

    ⇒ 只认明确提到额度/配额的措辞；其它错（500/超时/鉴权）**不**当额度耗尽
    ⇒ 避免把一次偶发故障误判成"永久没额度"（误判会让那一档永久让路）。
    """
    low = str(msg or "").lower()
    return any(w in low for w in _QUOTA_WORDS)


def _restore_quota(cfg: WbExecConfig, ex: Any, executor_id: str,
                   model: str) -> None:
    """**真调成功 ⇒ 额度回来了** ⇒ 清状态（**状态驱动**，不是时间到期）。

    ⇒ 恢复路径 = **手动钉一次这个模型跑成功**（钉死那条路**不走**候选预过滤
    ⇒ 它会被真调 ⇒ 成功即恢复 ⇒ 重新参与优先级瀑布的第一优先级）。

    ★ 2026-11-04：优先级模式下**不再需要"把权重还回去"**（份额转移是配比时代的
    机制）⇒ 这里只清额度状态。
    """
    reg = get_registry()
    if not reg.is_exhausted(executor_id, model):
        return
    reg.clear_exhausted(executor_id, model)
    logger.info("[wb_exec] %s/%s 真调成功 ⇒ 额度状态已清（重回优先级瀑布）",
                executor_id, model)


def _emit_oc_part(state: Any, ex: "_Exec", pt: dict) -> None:
    """opencode 的 part ⇒ 推演agent 事件（**前端零改动**）。"""
    t = str(pt.get("type") or "")
    if t == "reasoning":
        _emit(state, "reasoning_chunk",
              {"text": str(pt.get("text") or ""), "round": state.rounds})
    elif t == "text":
        _emit(state, "assistant_text",
              {"content": str(pt.get("text") or ""), "round": state.rounds})
    elif t == "tool":
        stt = pt.get("state") or {}
        status = str(stt.get("status") or "")
        if status == "running":
            ex.tool_calls += 1
            _emit(state, "tool_call", {
                "tool": str(pt.get("tool") or ""),
                "arguments": stt.get("input") or {},
                "round": state.rounds,
            })
        elif status in ("completed", "error"):
            out = stt.get("output")
            if out is None:
                out = stt.get("error")
            _emit(state, "tool_result", {
                "tool": str(pt.get("tool") or ""),
                "ok": status == "completed",
                "status": status,
                "summary": str(out or "")[:2000],
                "round": state.rounds,
            })


async def _run_opencode(state: Any, cfg: WbExecConfig, ex: "_Exec",
                        current: Candidate, cwd: str, registry: Any) -> None:
    """跑一轮 **opencode**（``opencode serve`` 的 HTTP + SSE 事件流）。

    **★ 为什么不复用 ACP 那条路**：opencode 的主接口是 REST（162 端点），
    事件走 **SSE**；ACP 只走 stdio ndJSON ⇒ 另一套传输。
    """
    from . import oc_client as oc_mod

    ex.executor_id, ex.model = current.executor, current.model
    ex_cfg = cfg.executor(current.executor)
    provider = (ex_cfg.oc_provider_id if ex_cfg else "") or "wb-direct"
    model = current.model
    if ex_cfg and ex_cfg.oc_models and model not in ex_cfg.oc_models:
        model = ex_cfg.oc_models[0]
    holder = _holder_id(state)

    def _on_wait() -> None:
        _emit(state, "wb_exec_queued", {
            "executor": current.executor,
            "model": current.model,
            "waiting_for": "opencode 槽（池满 ⇒ 排队等，不回落、不花钱）",
        })

    gw = await registry._acquire_oc(current.executor, cwd, current.model,
                                    on_wait=_on_wait)
    if gw is None:
        raise WbExecutorUnavailable(
            f"执行体 {current.executor} 的 opencode 池不可用")
    client = gw.client
    sid = gw.session_id
    ex.session_id = sid          # 外层 wb_exec_done 事件要用
    server = gw.cli_worker
    registry.mark_slot(gw.base_url, holder)
    # ★ **运行时已经占上槽了**（busy_session 已设）⇒ **释放选择期的预留**
    #   ⇒ 占用数不被重复计算（否则这个执行体会被误判成"排满"一倍）。
    registry.release_reserve(current.executor, holder)
    try:
        task_text = await _build_task_text(state, cfg, ex, cwd)
        done = asyncio.Event()
        #: **opencode 报出来的错**（2026-11-01 加）。
        #: ★ `prompt_async` 是 **204 立即返回** ⇒ 上游的错误（如 403 地区限制）
        #: 发生在**之后**、由 opencode 内部产生 ⇒ **HTTP 层完全看不出来**
        #:   ⇒ 早先不接 `session.error` 事件 ⇒ 错误被**静默丢弃**
        #:   ⇒ 后面只读到「0 工具 0 正文」⇒ **这张卡会「假装成功」**
        #:   （与 WorkBuddy 那边同一个坑：失败塞在返回体里，不检查就当成功）。
        #: ⇒ 这里把 `session.error` 收下来，收口时**抛出去** ⇒ 上层换候选/回落。
        #: 每条是 ``{"msg": str, "exhausted": bool, "reason": str}``：
        #: ``exhausted=True`` ⇒ 上游**明确说这是配额/额度问题** ⇒ 记进额度状态表
        #: ⇒ 之后这个候选**让路**（见 :func:`run_delegated` 的预过滤）。
        oc_error: list[dict] = []

        async def _watch() -> None:
            try:
                async for ev in client.events(cwd):
                    props = ev.get("properties") or {}
                    if not isinstance(props, dict):
                        continue
                    # **只收自己 session 的事件**（/event 是全局流 ⇒ 必须过滤）
                    esid = str(props.get("sessionID") or "")
                    if esid and esid != sid:
                        continue
                    etype = str(ev.get("type") or "")
                    if etype == "message.part.updated":
                        pt = props.get("part") or {}
                        if isinstance(pt, dict):
                            _emit_oc_part(state, ex, pt)
                    elif etype == "message.part.delta":
                        if str(props.get("field") or "") == "text":
                            _emit(state, "reasoning_chunk", {
                                "text": str(props.get("delta") or ""),
                                "round": state.rounds,
                            })
                    elif etype == "session.error":
                        # ★ 真实错误（实测 2026-11-01：APIError 403
                        #   「This model is not available in your country」）
                        err = props.get("error") or {}
                        data = err.get("data") or {} if isinstance(err, dict) else {}
                        msg = str(data.get("message") or err.get("name") or "未知错误")
                        code = data.get("statusCode")
                        text = (f"{err.get('name') or 'Error'}: {msg}"
                                + (f"（HTTP {code}）" if code else ""))
                        oc_error.append({"msg": text,
                                         "exhausted": _looks_exhausted(msg),
                                         "reason": "api_error"})
                        logger.error("[wb_exec] opencode 报错：%s", text)
                        _emit(state, "wb_exec_error", {
                            "executor": current.executor, "model": model,
                            "error": text[:300],
                        })
                        done.set()
                    elif etype == "session.status":
                        st = props.get("status") or {}
                        stt = str(st.get("type") or "")
                        if stt == "idle":
                            done.set()
                        elif stt == "retry":
                            # ★ **opencode 的「重试」事件**（2026-11-01 实测撞到）
                            #   {"type":"retry","attempt":1,
                            #    "message":"Free usage exceeded, subscribe to Go",
                            #    "action":{"reason":"free_tier_limit",
                            #               "title":"Free limit reached", …}}
                            #   ⇒ **它不是 `session.error`，而且后面没有 `session.idle`**
                            #   ⇒ 不接它 ⇒ `done` 永远不被 set
                            #   ⇒ 干等 `read_timeout_sec`（600 秒 = 10 分钟）
                            #   ⇒ **卡片表现为「卡住」**（现场 card_143）。
                            action = st.get("action") or {}
                            msg = str(st.get("message") or action.get("message") or "")
                            reason = str(action.get("reason") or "")
                            if action:
                                # **带 `action` ⇒ opencode 要「用户去做点什么」**
                                #   （订阅/额度/授权）⇒ **重试无意义**
                                #   ⇒ 立刻判失败 ⇒ 上层换候选
                                #   （不空等、不假装成功——空等就是这次的卡死）
                                text = (f"opencode 拒绝执行"
                                        f"（{reason or '需要用户动作'}）：{msg}")
                                oc_error.append({"msg": text, "exhausted": True,
                                                 "reason": reason or "需要用户动作"})
                                logger.error("[wb_exec] opencode 重试但需用户动作：%s",
                                             text)
                                _emit(state, "wb_exec_error", {
                                    "executor": current.executor, "model": model,
                                    "error": text[:300],
                                })
                                done.set()
                            else:
                                logger.info("[wb_exec] opencode 正在重试 attempt=%s %s",
                                            st.get("attempt"), msg)
                    elif etype == "session.idle":
                        done.set()
            except Exception as exc:  # noqa: BLE001 事件流断了不致命
                logger.warning("[wb_exec] opencode 事件流结束: %s", exc)
                done.set()

        watcher = asyncio.create_task(_watch())
        try:
            await client.prompt_async(sid, provider, model, task_text, cwd)
            _emit(state, "wb_exec_model", {
                "executor": current.executor, "model": model,
                "session_id": sid,
            })
            await asyncio.wait_for(
                done.wait(), timeout=float(cfg.watchdog.read_timeout_sec))
        finally:
            watcher.cancel()
        # ★ **opencode 报出来的错 ⇒ 抛出去**（2026-11-01 加）。
        #   不抛的话，上层会当成「跑完了」⇒ 0 工具 0 正文 ⇒ **假装成功**。
        if oc_error:
            # ★ **上游明确说"额度/配额用完了" ⇒ 记进额度状态表**
            #   ⇒ 之后这个候选**让路**（份额自动分给其它候选）⇒ 不再每次都去打它。
            for e in oc_error:
                if e.get("exhausted"):
                    registry.mark_exhausted(current.executor, model,
                                            str(e.get("reason") or "额度耗尽"))
            # ★ **带上 category="quota"** ⇒ 上层按「额度/过载」处理
            #   ⇒ 走**换候选**那条路（emit ``wb_exec_fallback``）
            #   而不是「续跑」那条（语义不对：不是断线续跑，是额度没了换人）。
            _first = oc_error[0]
            raise WbAcpError(
                f"opencode 执行体报错：{_first['msg']}",
                category=(CATEGORY_QUOTA if _first.get("exhausted") else ""))
        # **正文在 messages 里**（`POST .../message` 的响应体 parts 可能是空的）
        msgs = await client.messages(sid, cwd)
        text = oc_mod.session_text(msgs)
        if text:
            ex.text_parts.append(text)
        # ★ **真调成功 ⇒ 额度状态清掉 + 份额还回去**（状态驱动，不是时间到期）
        #   ⇒ 这也是**恢复探针**：手动钉死这个模型跑一轮，成功了就自动恢复全份额。
        _restore_quota(cfg, ex, current.executor, model)
        # ★ **空结果守卫**（2026-11-01 加）：0 工具 + 0 正文 ⇒ **一定没干成活**
        #   （实测：beam-free 被 403 地区限制时就是这个样子）⇒ 不许当成功。
        _tools = 0
        for _m in msgs:
            if not isinstance(_m, dict):
                continue
            for _p in (_m.get("parts") or []):
                if isinstance(_p, dict) and _p.get("type") == "tool":
                    _tools += 1
        if not text.strip() and _tools == 0 and ex.tool_calls == 0:
            raise WbAcpError(
                "opencode 执行体返回空结果（0 工具 0 正文）⇒ 判定失败，不当成功")
        # 收口事件由外层统一 emit（避免重复两遍）
    finally:
        registry.release_slot(gw.base_url, holder)
        if server is not None:
            try:
                server.busy_session = ""
            except Exception:  # noqa: BLE001
                pass
        # ★ **用完就清**：在 **opencode 自己的存储**里删掉**我们这一轮那个**会话
        #   （只按精确 id 删自己建的；用户的会话一个不碰 —— 见 cleanup.py）。
        #   ★ 放在 `client.close()` **之前** ⇒ 连接还在 ⇒ 才能删。
        await _cleanup_session(cfg, state, ex, current, cwd,
                               session_id=sid, client=client)
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


async def _resume_session(client: AcpClient, session_id: str,
                          cwd: str) -> tuple[str, list[str]]:
    """**续跑**：恢复上一次那个会话（接着它做到哪来）。

    ``session/load`` 会**抢写者权**（``_meta["codebuddy.ai/takeWriter"]=true``）——
    抢不到就只能只读跟随（``followOnlyAccept``），此时**不能再发 prompt**
    ⇒ 抢不到就当恢复失败，退回新建会话。
    """
    try:
        res = await client.session_load(session_id, cwd, take_writer=True)
    except WbAcpError as exc:
        logger.info("[wb_exec] 恢复会话 %s 失败（%s）⇒ 改建新会话",
                    session_id, exc)
        return "", []
    sid = str((res or {}).get("sessionId") or session_id)
    if not sid:
        return "", []
    logger.info("[wb_exec] 已恢复会话 %s ⇒ 接着上次继续", sid)
    return sid, []


async def _ensure_session(client: AcpClient, card_id: str, cwd: str,
                           desktop_ids: Optional[set[str]] = None
                           ) -> tuple[str, list[str]]:
    """建（或恢复）本卡的工作会话，返回 ``(session_id, 可用模型列表)``。

    **★ 用 `session/load` 而不是 `session/new`（2026-10-23 实测的关键修正）**

    桌面端网关的 ACP `newSession()` 内部是::

        E = (type=="new") && (!this.currentSessionId) && (网关自己的 sessionId 非空)
        POST /internal/agent { ...E?{sessionId: 网关自己的}:{},
                               ...t.sessionId && {sessionId: t.sessionId} }

    ⇒ **不传 `sessionId` ⇒ 落回网关自己的 primarySession**（= 桌面端正在跟你聊的
    那个会话）；传了 ⇒ 服务端按我的 id 建会话。
    **但 primary 活跃时，服务端会无视我传的 id，一律返回 primarySession**
    （实测：连发 3 次 `session/new` 全部拿回 ``7e7fa98e-…`` = 桌面端会话）。

    ⇒ 后果很严重：**任务会打进用户自己的桌面端对话**（事件串台就是这么来的），
    而且两个并发任务**抢同一个会话的写者** ⇒ 一个赢一个输、另一个挂到超时。

    ⇒ 改用 ``session/load {sessionId: 我们自己的 id, cwd, takeWriter:true}``：
    实测**用全新 id 能直接建出会话**（不走 primary 那条路），
    而且**同一个 id 再 load 就是恢复** ⇒ 天然的"续跑"。
    （`session/load` 的 result 不回 sessionId ⇒ 用我们自己给的 id。）

    另外：**cwd 必须先存在** —— 目录不存在时 `session/new` **静默失败**
    （既没有 result 也没有 error，只有一堆通知）。
    """
    if cwd:
        try:
            Path(cwd).mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 建不了目录就让会话去报
            logger.warning("[wb_exec] 建工作目录失败 %s: %s", cwd, exc)
    # ① 用自己的 id 建/恢复会话（避开桌面端 primarySession）
    res = await client.session_load(card_id, cwd, take_writer=True)
    sid = card_id
    models = _model_ids(res)
    # ② 保险：万一拿回来的是桌面端那个会话 ⇒ **绝不能用**（会污染用户对话）
    if desktop_ids and sid in desktop_ids:
        raise WbAcpError(
            f"会话 {sid} 是桌面端正在用的会话，拒绝使用"
            f"（WorkBuddy 没能为这张卡新建独立会话）")
    logger.info("[wb_exec] 会话就绪 sid=%s（cwd=%s，可用模型 %d 个）",
                sid, cwd, len(models))
    return sid, models


def _model_ids(session_new_result: dict) -> list[str]:
    """``session/new`` 结果里的可用模型 id 列表（空 ⇒ 不做可用性校验）。"""
    models = (session_new_result or {}).get("models") or {}
    out: list[str] = []
    for m in (models.get("availableModels") or []):
        mid = str(m.get("modelId") or "")
        if mid:
            out.append(mid)
    return out


async def _apply_guards(client: AcpClient, session_id: str,
                        cfg: WbExecConfig, source: str = "") -> None:
    """设护栏：权限模式 / 沙箱。**任一失败只告警不阻断**。

    **真实的 config id**（2026-10-23 实测 ``session/new`` 返回的 configOptions）：
    ``mode``（select，8 档，桌面端默认是 ``fullAccess``——**必须改**）、
    ``model``、``thought_level``、``sandbox``（select，值是**字符串** ``"true"``/``"false"``）。
    **没有** ``context_window`` 这一项 ⇒ 不再下发（发了会被服务端拒）。

    **★ CLI 进程池要用 ``acceptEdits``**（2026-10-23 21:05 实测）：
    ``dontAsk`` 下 CLI 的 Write / Bash / PowerShell **全被拒**（"需要审批"）
    ⇒ agent 写不了文件 ⇒ 任务做不成；``acceptEdits`` 实测能写。
    """
    g = cfg.guards
    is_cli = source == "cli"
    mode = g.cli_permission_mode if is_cli else g.permission_mode
    pairs = [("mode", mode)]
    if not is_cli:
        # **CLI 侧不下发 sandbox**（本地沙箱会把写文件挡掉）
        pairs.append(("sandbox", "true" if g.sandbox else "false"))
    for config_id, value in pairs:
        try:
            await client.set_config_option(session_id, config_id, value)
        except WbAcpError as exc:
            logger.warning("[wb_exec] 设 %s 失败（不阻断）: %s", config_id, exc)


# ===================================================================== 事件
def _emit(state: Any, ev_type: str, data: dict[str, Any]) -> None:
    """把外部执行体的事件转成推演agent 事件。

    [harness 第 1 步 2026-10-28 · 修通道] ``reasoning_chunk`` 必须走**逐字流**
    （``emit_token``），不能走主通道（``event_stream.emit``）。

    病根：系统自己的思考走 ``emit_token`` —— 只进内存 journal（给前端直播），
    到轮界由 ``_flush_reasoning`` 合并成一条 ``reasoning`` 落盘；而这里原先走
    ``event_stream.emit`` ⇒ **逐帧落盘**。实测后果：全库 169 万条事件里
    ``reasoning_chunk`` 占 77%（130 万条），其中 96% 带 bridge 独有的 ``step``
    字段、且高度集中在跑过外部执行体的两个 work（work_027 122 万、work_025 8.6 万）。

    这不是"收不收"的取舍，是**同一个语义走了两条通道**。修回后思考仍被完整保留
    （合并成 ``reasoning`` 全文），只是不再逐帧占盘。
    """
    try:
        from .. import event_stream
        if ev_type == "reasoning_chunk":
            text = str(data.get("text") or "")
            extra = {k: v for k, v in data.items() if k != "text"}
            event_stream.emit_token(state.work_id, state.card_id, ev_type,
                                    text, extra or None)
            return
        event_stream.emit(state.work_id, state.card_id, ev_type, data)
    except Exception as exc:  # noqa: BLE001 展示层零阻断
        logger.warning("[wb_exec] 事件 %s 落盘失败（不阻断）: %s", ev_type, exc)


def _on_notification(ex: _Exec, note: AcpNotification) -> None:
    """ACP 通知 → 推演agent 事件流（**映射到前端已认识的事件类型，零前端改动**）。

    **必须按 sessionId 过滤**（2026-10-23 实测踩到的坑）：
    同一 ACP 连接上**别的会话的事件会串进来**——桌面端自己正在聊的会话
    （它的 ``agent_message_chunk`` / ``tool_call``）会混进我们的流里。
    不过滤 ⇒ 卡面会显示别人的对话与工具调用。
    """
    state = ex.state
    kind = note.update_kind
    if note.is_keepalive:
        ex.watchdog.note_keepalive()
        return
    # [会话过滤] params.sessionId 缺省时（部分通知不带）当作自己的
    sess = str(note.params.get("sessionId") or "")
    if sess and ex.session_id and sess != ex.session_id:
        logger.debug("[wb_exec] 丢弃别会话的通知 %s（%s）", kind, sess)
        return
    # [状态 2026-10-23] WorkBuddy 自己给的状态：``_meta["codebuddy.ai/agentPhase"].phase``
    #   （idle / preparing / …）⇒ 记进状态账（**不拿时间猜它在不在干活**）。
    meta = note.params.get("_meta") or {}
    ph = meta.get("codebuddy.ai/agentPhase")
    if isinstance(ph, dict):
        ex.watchdog.note_phase(str(ph.get("phase") or ""))
    if kind == "agent_thought_chunk":
        ex.watchdog.note_business()
        text = _text_of(note.params.get("update", {}).get("content"))
        if text:
            ex.watchdog.note_text(len(text))   # 吐字 = 活着的第一手证据
            # [轮号] 用**卡自己的轮号**（state.rounds）——WorkBuddy 的内部步
            #   另放 ``step`` 字段。早先这里用 ex.step（WorkBuddy 内部步）⇒
            #   和系统路径的轮号**共用一个字段** ⇒ 卡面同时出现"第 1 轮"和
            #   "第 4/5/6 轮"两套号（用户 2026-10-23 17:30 当场指出）。
            _emit(state, "reasoning_chunk", {"text": text,
                                            "round": state.rounds,
                                            "step": ex.step})
        return
    if kind == "agent_message_chunk":
        ex.watchdog.note_business()
        text = _text_of(note.params.get("update", {}).get("content"))
        if text:
            ex.text_parts.append(text)
        return
    if kind == "tool_call":
        ex.watchdog.note_business()
        upd = note.params.get("update") or {}
        call_id = str(upd.get("toolCallId") or "")
        title = str(upd.get("title") or upd.get("kind") or "tool")
        raw = upd.get("rawInput")
        args = raw if isinstance(raw, dict) else {}
        ex.tool_calls += 1
        # [step] = **第几个工具调用**（WorkBuddy 的内部步）。
        #   实测：一次工具调用会连发**多条** ``tool_call_update``（流式输出），
        #   而它们的 ``status`` 常常是空串 ⇒ 按"更新完成"递增会永远停在 1。
        #   ⇒ 按**新的 tool_call 出现**递增：一个工具 = 一步。
        ex.step += 1
        ex._tool_seen[call_id] = title
        ex._tool_step[call_id] = ex.step
        ex.watchdog.note_tool_started(call_id)   # 在途（跑大工具时不吐字也是在干活）
        _emit(state, "tool_call", {
            "tool": title, "target": "", "arguments": args,
            "tool_call_id": call_id, "round": state.rounds, "step": ex.step,
        })
        return
    if kind == "tool_call_update":
        ex.watchdog.note_business()
        upd = note.params.get("update") or {}
        call_id = str(upd.get("toolCallId") or "")
        status = str(upd.get("status") or "")
        ok = status not in ("failed", "error")
        if ok:
            ex.tool_ok += 1
        text = _text_of(upd.get("content"))
        _emit(state, "tool_result", {
            "tool": ex._tool_seen.get(call_id, "tool"),
            "card_id": "", "ok": ok, "status": status,
            "tool_call_id": call_id, "round": state.rounds,
            "step": ex._tool_step.get(call_id, ex.step),
            "summary": text[:400],
        })
        ex.watchdog.note_tool_finished(call_id)
        return
    if kind == "terminal_output_chunk":
        ex.watchdog.note_business()
        return
    # 其它通知（plan / available_commands / permission…）不阻断，落个 sys 事件留痕
    ex.watchdog.note_business()


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text") or "")
        return ""
    if isinstance(content, list):
        return "".join(_text_of(x) for x in content)
    return ""


# ===================================================================== 任务与收口
def _work_dir(work_id: str) -> str:
    try:
        from ...storage import work_store
        return str(work_store._work_dir(work_id))
    except Exception:  # noqa: BLE001
        return ""


def _norm_text(s: str) -> str:
    """正文归一化（去所有空白）⇒ 用来判"实时发过没有"（避免重复发同一段）。"""
    return "".join(str(s or "").split())


def _segment_marker(cfg: WbExecConfig, state: Any, ex: _Exec) -> str:
    """**分段标记**（幂等续跑的判据）。

    ★ 也被 Devin 的**实时会话认领**用（``role=user`` 消息里带这个标记
    ⇒ 那条会话就是这张卡的 ⇒ 同 work 目录里别人的会话不会被算进来）。
    """
    return cfg.task.segment_marker.format(
        card_id=state.card_id, seg=1, total=1, run_id=ex.session_id or "pending")


async def _build_task_text(state: Any, cfg: WbExecConfig, ex: "_Exec",
                            cwd: str) -> str:
    """任务文本 ＝ 分段标记 ＋ **卡上事实** ＋ 原任务 ＋ 工作目录说明。

    **卡上事实是必需的**（2026-10-23 实测发现的洞）：
    存量未完工的 coder 卡被 WorkBuddy 接手时，**ACP 侧是全新会话**
    （`session/new` ⇒ WorkBuddy 不知道这张卡之前做到哪）。
    ⇒ 必须把**卡是事实来源**（任务书 §四③）落进 prompt：
    使命（要干什么）＋ 现状（做到哪）＋ 影子产物清单（已经产出了什么文件）。
    不喂 ⇒ WorkBuddy 从零开始 ⇒ 重做、覆盖已有产物。
    """
    marker = _segment_marker(cfg, state, ex)
    lines = [marker]
    if cwd:
        # **给绝对路径**（2026-10-23 实测）：桌面端网关的 ``session/load`` **不转发
        # cwd** ⇒ 会话的工作目录 = 网关进程的目录（桌面端启动目录），**不是**这个
        # work 目录 ⇒ 只说"当前目录"的话，产出文件会落到别处。
        lines.append(f"（工作目录：{cwd}）")
        lines.append(f"（所有产出文件一律写到 {cwd} 下面，不要写到别处）")
    lines.append("")
    snapshot = await _card_facts(state)
    if snapshot:
        lines.append("【这张卡已有的事实（来自卡片本身，WorkBuddy 侧此前没有这些上下文）】")
        lines.append(snapshot)
        lines.append("")
    lines.append(str(state.content or ""))
    lines.append("")
    # [回显指令要说清楚是哪一行]（实测 2026-10-23 18:24）：任务文本里有两个
    # 【…】块（卡上事实 + 分段标记），笼统说"回显上面的【…】标记"⇒
    # agent 经常回显错那个（把"卡上事实"当标记回显了）⇒ 幂等续跑的判据失效。
    lines.append(f"完成后请在回复的**第一行**原样回显这一行，然后再说别的：")
    lines.append(marker)
    return "\n".join(lines)


async def _card_facts(state: Any) -> str:
    """读卡上已有的事实（使命／现状／影子产物／**最近跑过的工具**）。

    **四层都必要**（2026-10-23 实测发现的洞）：
    存量未完工的 coder 卡被 WorkBuddy 接手时，**ACP 侧是全新会话**
    ⇒ WorkBuddy 不知道这卡之前做到哪。逐层说明：

    | 层 | 给什么 | 少了会怎样 |
    |---|---|---|
    | 使命 mission | 这卡要干什么 | 不知道目标 |
    | 影子 shadow | 已产出的文件路径 | 不知道哪些文件别重做 |
    | **最近工具调用** | **实际跑过的命令/改过的文件** | **可能重跑、重复副作用** |

    最后这层从**卡的会话文件**里挖（`session_facade.read`）——
    卡面是"索引"，会话是"手脚记忆"，两个都要。
    """
    out: list[str] = []
    try:
        from ...storage import card_store
        card = await card_store.read_card(state.work_id, state.card_id)
        body = card.get("body") or {}
        meta = card.get("meta") or {}
        mission = str(body.get("mission") or "").strip()
        # [2026-10-28] 不再读 status_block：编程卡按设计没有现状块，这一层只会
        # 读到桥接层自己 append 的执行体原文（card_076 曾 86 万字）。
        # 续跑要的"做到哪了"由下面两层给：影子产物 + 最近工具调用（一手事实）。
        if mission:
            out.append("## 使命\n" + mission)
        if meta.get("goal"):
            out.append("## 当前目标\n" + str(meta.get("goal")))
    except Exception as exc:  # noqa: BLE001 读不到不影响委派
        logger.warning("[wb_exec] 读卡事实失败（不阻断）card=%s: %s",
                       state.card_id, exc)
    try:
        from ...storage import shadow_store
        shadow = await shadow_store.read_shadow_raw(state.work_id, state.card_id)
        if shadow:
            out.append("## 影子（已有产物与过程）\n" + str(shadow)[:4000])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[wb_exec] 读影子失败（不阻断）card=%s: %s",
                       state.card_id, exc)
    actions = await _recent_actions(state)
    if actions:
        out.append("## 已经跑过的动作（**不要重复跑**；已完成的部分接着做）\n" + actions)
    return "\n\n".join(out)


async def _recent_actions(state: Any, limit: int = 12) -> str:
    """从卡的会话里挖**最近跑过的工具调用**（"手脚记忆"）。**只读，零阻断。**"""
    try:
        from ...storage import session_facade
        msgs = await session_facade.read(state.work_id, state.card_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[wb_exec] 读会话历史失败（不阻断）card=%s: %s",
                       state.card_id, exc)
        return ""
    lines: list[str] = []
    for blk in msgs:
        if not isinstance(blk, dict):
            continue
        for tc in (blk.get("tool_calls") or []):
            fn = (tc or {}).get("function") or {}
            name = str(fn.get("name") or "")
            if not name:
                continue
            raw = str(fn.get("arguments") or "")
            try:
                args = json.loads(raw) if raw else {}
            except Exception:  # noqa: BLE001
                args = {}
            if isinstance(args, dict):
                hint = " ".join(
                    f"{k}={str(v)[:60]}" for k, v in list(args.items())[:3])
            else:
                hint = str(args)[:120]
            lines.append(f"- {name}（{hint}）")
    if not lines:
        return ""
    tail = lines[-limit:]
    more = f"（更早还有 {len(lines) - len(tail)} 次调用，未列出）" \
        if len(lines) > len(tail) else ""
    return "\n".join(tail) + more


async def _record_routed(state: Any, ex: _Exec) -> None:
    """把**实际路由到的**（版本, 模型）记进这张卡的覆盖行 ⇒ 卡面 UI 开工后显示它。

    **读-改-写**（``put_row`` 是整行替换 ⇒ 必须先读出整行再改一个字段，
    否则会把该卡原有的 model/fallbacks/bindings 抹掉）。**只写，零阻断。**
    """
    try:
        from ...storage import model_override
        cur = dict(model_override.get_row(state.work_id, state.card_id) or {})
        cur["last_routed"] = {
            "executor": ex.executor_id,
            "model": ex.model,
            "at": int(time.time()),
            "tool_calls": ex.tool_calls,
        }
        model_override.put_row(state.work_id, state.card_id, cur, src="manual")
    except Exception as exc:  # noqa: BLE001 记账失败不阻断交付
        logger.warning("[wb_exec] 记 last_routed 失败（不阻断）card=%s: %s",
                       state.card_id, exc)


async def _record_fallback(state: Any, reason: str, kind: str = "") -> None:
    """**回落记账**：本轮实际走的是**系统自己的模型** ⇒ 如实写进卡面。

    **为什么必须有这个**（2026-10-23 19:55 用户要求"靠谱"）：
    委派失败会**静默**回落系统路径，卡面若还显示"走 WorkBuddy"，
    人就会以为"在 WorkBuddy 上跑"——**假信号**。
    ⇒ 回落时把 ``last_routed.fallback = "system"`` ＋原因写进去，
    卡面显示"本轮实际走系统自己的模型（原因…）"。

    ``kind="slot_full"`` ⇒ 这一轮是**因并发槽已满**回落
    """
    try:
        from ...storage import model_override
        cur = dict(model_override.get_row(state.work_id, state.card_id) or {})
        cur["last_routed"] = {
            "executor": "", "model": "",
            "at": int(time.time()),
            "fallback": "system",
            "reason": str(reason)[:200],
        }
        if kind:
            cur["last_routed"]["kind"] = kind
        model_override.put_row(state.work_id, state.card_id, cur, src="manual")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[wb_exec] 记回落失败（不阻断）card=%s: %s",
                       state.card_id, exc)


async def _finish_card(state: Any, ex: _Exec, final_text: str) -> None:
    """完工收口：**复用现成的** ``card_tools._h_report_done``（不新造闸）。

    [2026-10-28 用户令「这个动作不合理」] 改前这里还会把执行体产出**追加**进卡的
    现状块（`_write_status_block`，理由：父卡读 coder 卡时先看现状块）。已删除：

    · **编程卡按设计没有现状块**（`edit_status` 对 coder 直接拒），桥接层直接写库
      是在绕过那道闸——同一件事两套口径；
    · 父卡拿结果本来就靠 `report_done` 的 summary ＋ `coder_done` 回执，已在跑；
    · 那种写法是 `old + 新` 纯追加、无上限，而 `_card_facts` 每轮又会把它拼进
      执行体 prompt ⇒ 自激放大（card_076 实测 24 轮滚到 86 万字）。

    [差事卡 2026-11-01] qa_card/pm_card 收口走差事语义——执行体没有
    card_tools，桥接层机械完成：report 判据 ⇒ 正文作差事回执发派遣方；
    cardwrite 判据 ⇒ 正文作草稿写到对象卡面。最后差事卡置 completed。
    """
    try:
        from ...storage import card_store
        card = await card_store.read_card(state.work_id, state.card_id)
        meta = card.get("meta") or {}
        if str(meta.get("card_type") or "") in DEPLOY_CARD_TYPES:
            await _finish_deploy_card(state, ex, final_text, meta)
            return
        from .. import card_tools
        cards_meta = {state.card_id: meta}
        summary = (f"WorkBuddy 执行体完成（{ex.executor_id}/{ex.model}，"
                   f"工具调用 {ex.tool_calls} 次）\n{final_text[:1500]}")
        res = await card_tools._h_report_done(
            state.work_id, state.card_id, {"summary": summary}, cards_meta)
        if isinstance(res, dict) and res.get("error"):
            logger.error("[wb_exec] report_done 失败 card=%s: %s",
                         state.card_id, res.get("error"))
            return
        logger.info("[wb_exec] 完工收口完成 card=%s owner=%s",
                    state.card_id, res.get("owner") if isinstance(res, dict) else "")
    except Exception as exc:  # noqa: BLE001 收口失败不阻断本轮交付
        logger.error("[wb_exec] 完工收口异常 card=%s: %s", state.card_id, exc)


async def _finish_deploy_card(state: Any, ex: _Exec, final_text: str,
                              meta: dict) -> None:
    """差事卡的 wb 收口——**执行体干活、桥接层交差**。

    执行体（WorkBuddy agent）没有 card_tools，它在文件系统里把活干完、
    把结论写进最终回复正文；差事的"回执/落稿"由这里机械完成：

      · ``report`` 判据（QA）：正文作 ``msg_report`` 落派遣方会话＋唤醒；
      · ``cardwrite:<块>`` 判据（PM）：正文作草稿写到对象卡的草稿列
        （走现成的 ``_h_edit_requirements``，差事作用域放行对象卡）。

    之后 ``sweep_deployments`` 惰性判完持久关账，差事卡置
    ``state=completed``——差事卡是一次性场，办完即完工。
    """
    from .. import card_tools
    from ..message_runner import _synth_seat_meta
    from ...storage import card_store, session_facade
    from ...storage import function_store as _fs
    wid, cid = state.work_id, state.card_id
    deps = _fs.active_for_venue(wid, cid, "deploy")
    if not deps:
        logger.info("[wb_exec] 差事卡 %s 没有在派差事，仅置完工", cid)
        await card_store.update_card(wid, cid, {"state": "completed"},
                                     "差事卡完工（无在派差事）")
        return
    dep = deps[0]
    fn = str(dep.get("function_id") or "")
    delegator = str(dep.get("delegator") or "")
    target = str(dep.get("target_card") or "")
    exit_spec = str(dep.get("exit_spec") or "")
    # cards 映射：真卡 meta + 职能席位的合成面（能力闸/回执投递都要查它）
    cards: dict[str, dict[str, Any]] = {cid: dict(meta)}
    for other in (delegator, target):
        if other and other not in cards and ":" not in other:
            try:
                oc = await card_store.read_card(wid, other)
                cards[other] = dict(oc.get("meta") or {})
            except Exception:  # noqa: BLE001
                pass
    cards[fn] = _synth_seat_meta(wid, fn, cards)

    async def _receipt(body: str) -> None:
        """差事回执——用**场解析出的 dep** 发（同职能多差事也不发错派遣方）。"""
        if not delegator or ":" in delegator or delegator not in cards:
            return
        title = str(dep.get("venue_label") or cid)
        text = f"【差事回执·{title}({fn})】\n{body}"
        from .. import message_runner as _mr
        rec = await session_facade.append_card_message(
            wid, delegator, from_card=fn, text=text, msg_type="msg_report")
        _mr._spawn_tracked(_mr.deliver_now(
            wid, delegator,
            text=f"你收到一条来自 {fn} 的差事回执，正文已在上文。",
            source={"kind": "card_msg", "from": fn}))
        return rec

    if exit_spec.startswith("cardwrite:"):
        # PM：执行体的产出文本＝需求文档草稿 → 写到对象卡草稿列（全量替换）。
        # 顺序：先回执后落稿——cardwrite 一落账差事即惰性判完，回执必须先发。
        await _receipt(f"需求文档草稿已写入 {target}（{len(final_text)} 字）")
        cur = str((cards.get(target) or {}).get("requirements_draft") or "")
        # actor_ctx：card_block_edited 的 by 归因来自这里——判据比 by==职能，
        # 不包一层会落成 by=system ⇒ 差事永远判不完。
        with card_store.actor_ctx(fn):
            res = await card_tools._h_edit_requirements(
                wid, fn, {"card_id": target, "old_string": cur,
                          "new_string": final_text}, cards)
        if isinstance(res, dict) and res.get("error"):
            logger.error("[wb_exec] PM 落稿失败 card=%s: %s", cid,
                         res.get("error"))
        else:
            logger.info("[wb_exec] PM 草稿已写对象卡 %s card=%s", target, cid)
    else:
        await _receipt(final_text[:3000])
        logger.info("[wb_exec] QA 结论已回派遣方 %s card=%s", delegator, cid)
    await _fs.sweep_deployments(wid)
    await card_store.update_card(wid, cid, {"state": "completed"},
                                 "差事卡完工（执行体交付）")


