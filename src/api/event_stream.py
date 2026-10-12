# -*- coding: utf-8 -*-
"""事件流过程通道（确定性动作直答与全链事件 · 甲线核心）。
（2026-10-11：快车道/全链分流机制退役，本模块随 triage 判定链统一发事件。）

纯展示层：内存事件日志（每 (work_id, card_id) 一份、seq 单调）＋ SSE 订阅 fanout。
铁则：
- 事件不进 session/prior/模型输入（A42）——本模块不写任何 works 文件；
- 单进程限制继承（A44）：不落盘、不另起进程；
- 同卡并发 journal 清零重放（A45）：run_start 清空该卡日志（新请求以 run_start 开场）；
- 与打点分离（A22/A24/A96）：审计打点走 logging（本模块 emit 内同步打一行结构化
  日志），事件走内存日志，两通道不混。

[对话流渲染 T4 · 无闸] 本模块三处「丢字闸」已清除（用户要看的字一个都不能丢）：
  ① 单帧 8KB 截断（原丢后半）→ 删；逐字流帧天然小帧、SSE 无单帧硬限制；
  ② 逐字流 5000 条环形弹出（原丢前字）→ 删；单轮 chunk 由 run_start/round_start
     每轮清空，天然有界，以完整保留优先；
  ③ 订阅队列满 ``put_nowait`` 静默丢 → 改「丢最旧中间帧、保最新 seq 游标、打警告」。
  主通道 300 条环形弹出同属「丢步骤闸」，一并随 T3（过程卡族退役）退场。
  丢帧造成的 **seq 空洞为设计使然、正常态**：前端 from=0 重放的完整 journal 可补齐
  未丢失部分，后端**不补任何补丁帧**。

事件本体论 19 类（接口①并表，A101）：15 原（run_start/context/round_start/
round_end/rescue/tool_call/tool_result/seats_enter/seat_start/seat_end/deliver/
proposal/done/error/ping）＋ card_op（确定性动作直答事件）＋ 逐字流 2 类。
（2026-10-11：快车道/全链分流机制退役——fastlane_route 事件随之移除，
 确定性动作识别并入 triage 判定链，用户路径上只有一套判断。）
Schema 统一 {"seq":n,"ts":...,"type":"...","data":{...}}。ping 为 SSE 保层
心跳帧（不占 journal，由 SSE 生成器定期发送）。

[迭代一·A5/T092-T099] 分段标记制（run_start 清零制退役）：
- journal 不再清零——新回合开新段、旧段保留（「上一轮过程被下一轮开始抹掉」
  从机制上消失）；轮界分段信号沿用 19 类内轮界类（run_start/round_start，
  T095「沿用」路径，本体论仍 19 类）；
- boundary 假帧（data._boundary）退役（T094）——前端 static/stream.js 只按
  type 判界（run_start/round_start → clearText/clearRows），分段标记帧与旧
  边界帧对前端呈现等价，前端消费面零改动（T096/X7）；
- seq 单一取号（T097）：主通道与逐字流共用 _seq_counters（单一分配者），
  两路帧仅携带——客户端凭 seq 交织（T093），seq 空洞仍为正常态（R-01）；
- 每帧携带 (pair_id, seq, turn) 三锚（T093），锚由 run_start/round_start
  就地登记、统一附带；
- R5 过程线持久化通道（T099/T110/T145）：persist_process 配置位默认 off
  （config/session_base.json），on 时主 journal 帧逐帧落
  works/{work}/process/{card}.jsonl，格式不随开关变化。
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator

logger = logging.getLogger("api.event_stream")

# [信息流重出 Task4] 当前逐字流归属的卡。用 contextvars 而非改 7 处 call_main 调用点：
# 模型调用链上没有 work_id/card_id，逐层穿参会污染 凝练/折叠 等无卡上下文的调用方。
# 未设置时（凝练席、归拢等后台调用）逐字流自然关闭——不硬塞。
_stream_ctx: contextvars.ContextVar[Any] = contextvars.ContextVar("flow_stream_ctx", default=None)


def set_stream_context(work_id: str, card_id: str):
    """标记「本协程链路的逐字流属于这张卡」，返回 token 供还原。"""
    return _stream_ctx.set((work_id, card_id))


def reset_stream_context(token: Any) -> None:
    try:
        _stream_ctx.reset(token)
    except Exception:  # noqa: BLE001 跨上下文还原失败时退化为清空
        _stream_ctx.set(None)


def current_stream_context():
    return _stream_ctx.get()

# [信息流重出 Task4] 逐字流事件：新增 2 类 → 事件本体论 17 → 19。
# 独立成对：走**独立 SSE 流 + 独立 journal + 独立 seq 计数器**（红线4
# 「与既有 17 类隔离、不混合排 seq」）。
TOKEN_EVENT_TYPES: tuple[str, ...] = ("reasoning_chunk", "content_chunk")

# 事件本体论（19 类 = 17 原 + 2 逐字流；V-T4-2 判据锚）
EVENT_TYPES: tuple[str, ...] = (
    "run_start", "context", "round_start", "round_end", "rescue",
    "tool_call", "tool_result", "seats_enter", "seat_start", "seat_end",
    "deliver", "proposal", "done", "error", "ping",
    "card_op",
    "triage_verdict", "plan_created", "step_advanced",
) + TOKEN_EVENT_TYPES

# [对话流渲染 T4] 以下两个上限常量已删——都是「丢字/丢步骤闸」：
#   单帧 8KB 数据上限：截断丢后半；
#   主通道 300 条条数上限：环形弹出丢最前面的步骤。
#   两个 journal 都由 run_start 每轮清空（见 emit），单次请求天然有界，无需条数上限。

# 兼容映射：emit 兼调 progress.set_stage（旧 /progress 端点与旧前端零感知兼容 A67）
_PROGRESS_STAGE = {
    "run_start": "started",
    "round_start": "tools",
    "tool_call": "tools",
    "seats_enter": "viewpoints",
    "seat_end": "viewpoints",
    "deliver": "synthesize",
    "proposal": "cards",
    "done": "done",
    "error": "error",
}

_journals: dict[tuple[str, str], list[dict[str, Any]]] = {}
_subscribers: dict[tuple[str, str], set[asyncio.Queue]] = {}
# seq 按卡全局单调：run_start 清 journal（A45）不清计数器——长连监听者的
# seq 游标跨请求安全（新请求事件不会被当作已读吞掉）
_seq_counters: dict[tuple[str, str], int] = {}


# ------------------------------------------------------------
# [迭代一·A5/T092-T099] 分段标记制基础设施
# ------------------------------------------------------------
# 回合锚登记：(work, card) → {"pair_id", "turn"}——由 run_start/round_start
# 就地更新，emit/emit_token 统一给帧附带 (pair_id, seq, turn) 三锚（T093）。
_anchors: dict[tuple[str, str], dict[str, Any]] = {}


def _next_seq(key: tuple[str, str]) -> int:
    """seq 单一取号（T097）：主通道与逐字流共用，单一分配者，帧仅携带。"""
    _seq_counters[key] = _seq_counters.get(key, 0) + 1
    return _seq_counters[key]


def _attach_anchors(key: tuple[str, str], evt: dict[str, Any]) -> dict[str, Any]:
    a = _anchors.get(key)
    if a:
        evt["pair_id"] = a.get("pair_id", "")
        evt["turn"] = a.get("turn", "")
    return evt


def _load_persist_config() -> bool:
    """R5 配置位（T099/T110）：config/session_base.json 的 persist_process，默认 off。"""
    try:
        cfg = Path(__file__).resolve().parents[2] / "config" / "session_base.json"
        with open(cfg, encoding="utf-8") as f:
            return bool(json.load(f).get("persist_process", False))
    except Exception:  # noqa: BLE001 配置缺失/损坏＝off（P-D 缺省，不阻断）
        return False


_PERSIST_PROCESS = _load_persist_config()


def set_persist_process(on: bool) -> None:
    """拨 persist_process 开关（运维/测试用；只改行为不改格式，T145）。"""
    global _PERSIST_PROCESS
    _PERSIST_PROCESS = bool(on)


def _persist_frame(work_id: str, card_id: str, evt: dict[str, Any]) -> None:
    """R5 过程线持久化写入通道（T099）：on 时主 journal 帧逐帧落
    works/{work}/process/{card}.jsonl（追加式）。off＝零动作。"""
    if not _PERSIST_PROCESS:
        return
    try:
        from ..storage import session_store
        p = session_store.WORKS_DIR / work_id / "process" / f"{card_id}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 展示层零阻断
        pass

# [信息流重出 Task4] 逐字流 journal 独立存储。
# [迭代一·A5/T093/T097] seq 单一取号：逐字流与主通道共用 _seq_counters（单一
# 分配者），两路帧仅携带——客户端凭 seq 交织。独立的是 journal 与订阅者，
# 不再是计数器（原「独立 seq 计数器」红线随分段制退役，设计输入 4.6②）。
_token_journals: dict[tuple[str, str], list[dict[str, Any]]] = {}
_token_subscribers: dict[tuple[str, str], set[asyncio.Queue]] = {}


def _key(work_id: str, card_id: str) -> tuple[str, str]:
    return (work_id, card_id)


def _jsonable(data: dict[str, Any]) -> dict[str, Any]:
    """只兜住「不可 JSON 序列化」的载荷，防 SSE 生成器 ``json.dumps`` 抛错中断整条流。

    [对话流渲染 T4] 原 8KB 截断分支已删除——它是丢字闸。
    此处仅保留**非尺寸**的序列化兜底（不限制长度/体积/时间，不是闸）。
    """
    try:
        json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"_note": "unserializable"}
    return data


def _seq_of(item: Any) -> Any:
    return item.get("seq") if isinstance(item, dict) else None


def _fanout(subs: Any, evt: dict[str, Any], work_id: str, card_id: str, channel: str) -> None:
    """SSE 订阅 fanout：队列满**不静默丢**（[对话流渲染 T4]）。

    原写法 ``q.put_nowait(evt)`` 在 QueueFull 时被 except 静默吞掉——用户要看的
    字/步骤就这样悄悄没了、且无任何痕迹。现改为：队列满则弹出其中最旧一帧腾位，
    再把**最新**帧放入，并每次打 warning 留痕。

    - 保「最新」＝保最新 seq 游标可继续推进；
    - 丢「最旧中间帧」＝只损失已排队但未送达的旧帧，这些帧仍在 journal 里，
      前端 from=0 重放可补齐；
    - 后端**不补任何补丁帧**（seq 空洞为正常态，接口契约不变）。
    """
    for q in list(subs):
        try:
            q.put_nowait(evt)
        except asyncio.QueueFull:
            dropped = None
            try:
                dropped = q.get_nowait()          # 丢最旧中间帧，腾位
            except Exception:  # noqa: BLE001 取空队列竞态：忽略
                dropped = None
            try:
                q.put_nowait(evt)                 # 保最新帧
            except Exception:  # noqa: BLE001
                pass
            logger.warning(
                "[flow] %s 订阅队列满：丢最旧中间帧(seq=%s) 保最新(seq=%s) work=%s card=%s",
                channel, _seq_of(dropped), evt.get("seq"), work_id, card_id,
            )
        except Exception:  # noqa: BLE001 单订阅者失效不拖累其他
            pass


def _emit_token_segment(work_id: str, card_id: str, ev_type: str,
                        extra: dict[str, Any]) -> None:
    """[迭代一·A5] 轮界分段标记帧入逐字流 journal（T094/T095）。

    沿用既有轮界类 type——前端 stream.js 按 type 判界（clearText），与旧
    边界帧呈现等价、消费面零改动；`data._boundary` 假帧位退役（T094），
    改携带 segment 语义与三锚。帧仍占统一 seq（单一取号，T097）。
    """
    try:
        key = _key(work_id, card_id)
        evt = _attach_anchors(key, {
            "seq": _next_seq(key),
            "ts": time.time(),
            "type": ev_type,
            "data": _jsonable(dict(extra)),
        })
        _token_journals.setdefault(key, []).append(evt)
        _fanout(_token_subscribers.get(key, ()), evt, work_id, card_id, "逐字流")
    except Exception:  # noqa: BLE001 展示层零阻断
        pass


def emit(work_id: str, card_id: str, ev_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """落一条事件：seq 单调、fanout 订阅者、兼调 progress 兼容映射。

    零阻塞零抛出（展示层绝不影响主流程）。
    [迭代一·A5/T092] run_start 清空 journal 制退役——旧段保留，分段标记承载
    轮界（run_start/round_start 沿用，T095）；帧附 (pair_id, seq, turn) 三锚
    （T093）；persist_process 开时主 journal 帧落盘（T099，默认 off）。
    """
    try:
        key = _key(work_id, card_id)
        d = data or {}
        if ev_type == "run_start":
            # [A5/T092] 清零退役：旧段保留；登记回合锚（pair_id）＋逐字流开新段
            _anchors[key] = {"pair_id": d.get("pair_id", ""), "turn": 0}
            _emit_token_segment(work_id, card_id, ev_type, {"segment": "run"})
        elif ev_type == "round_start":
            # [A5/T092] round_start 不再清逐字流——分段标记帧承载轮界（T094/T095）
            try:
                _anchors.setdefault(key, {"pair_id": "", "turn": 0})["turn"] = \
                    int(d.get("round", 0) or 0)
            except Exception:  # noqa: BLE001 展示层零阻断
                pass
            _emit_token_segment(work_id, card_id, ev_type,
                                {"segment": "round", "round": d.get("round", "")})
        journal = _journals.setdefault(key, [])
        evt = _attach_anchors(key, {
            "seq": _next_seq(key),
            "ts": time.time(),
            "type": ev_type,
            "data": _jsonable(data or {}),
        })
        journal.append(evt)
        _persist_frame(work_id, card_id, evt)
        _fanout(_subscribers.get(key, ()), evt, work_id, card_id, "主通道")
        # 兼容映射（旧 /progress 零感知）
        stage = _PROGRESS_STAGE.get(ev_type)
        if stage:
            try:
                from . import progress
                progress.set_stage(work_id, card_id, stage, str((data or {}).get("detail") or ev_type)[:120])
            except Exception:  # noqa: BLE001
                pass
        # 审计打点（与事件分离的日志通道；只写不判）
        logger.info("[flow] type=%s work=%s card=%s seq=%s data=%s",
                    ev_type, work_id, card_id, evt["seq"],
                    json.dumps(evt["data"], ensure_ascii=False)[:300])
        return evt
    except Exception as exc:  # noqa: BLE001 展示层零阻断
        logger.warning("[flow] emit 失败（不阻断主流程）type=%s card=%s: %s", ev_type, card_id, exc)
        return {}


def journal_of(work_id: str, card_id: str) -> list[dict[str, Any]]:
    """当前日志快照（from=0 重放源）。"""
    return list(_journals.get(_key(work_id, card_id), ()))


async def subscribe(work_id: str, card_id: str, from_seq: int = 0) -> AsyncIterator[dict[str, Any]]:
    """SSE 订阅：先重放 journal 中 seq>=from_seq 的存量，再实时跟进。

    断连（生成器被关闭/取消）自动清理订阅者队列（A31）。
    """
    key = _key(work_id, card_id)
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers.setdefault(key, set()).add(queue)
    try:
        for evt in journal_of(work_id, card_id):
            if evt["seq"] >= from_seq:
                yield evt
        last = from_seq - 1
        while True:
            evt = await queue.get()
            if evt["seq"] <= last:
                continue
            last = evt["seq"]
            yield evt
    finally:
        try:
            _subscribers.get(key, set()).discard(queue)
        except Exception:  # noqa: BLE001
            pass


def subscriber_count(work_id: str, card_id: str) -> int:
    return len(_subscribers.get(_key(work_id, card_id), ()))


# ============================================================================
# [信息流重出 Task4] 逐字流独立通道
#
# 与既有 17 类事件**完全隔离**（红线4）：
#   - 独立 journal（_token_journals）、独立 seq 计数器（_token_seq_counters）
#   - 不进 _journals，不占主 seq
#   - 不调 progress.set_stage：逐字流不是流程阶段，汇报给 /progress 会污染其语义
#   - 不写 works 文件、不进 session/prior/模型输入（红线3，A42）
# 展示层铁则同主通道：零阻塞、零抛出。
# [对话流渲染 T4] 逐字流 journal 不再有 5000 条环形弹出——单轮 chunk 由
# run_start/round_start 每轮清空（见 emit），天然有界，以完整保留优先。
# ============================================================================

def emit_token(work_id: str, card_id: str, ev_type: str, text: str,
               extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """落一条逐字流增量（reasoning_chunk / content_chunk）。

    与 ``emit`` 的区别只在「另开一套 journal 与 seq」，对主通道零干扰。
    [对话流渲染 T4] 单帧超长**不再截断**——原 8KB 截断会把一帧的字砍掉后半，
    属丢字闸。逐字流帧天然是小帧、SSE 无单帧硬限制，完整保留。

    [思考流支持层 2026-10-12] ``extra``：随帧附带的展示层字段（缺省 None＝
    原行为逐字不变）。三席思考增量带 ``model``（席名），前端据此把并发交织的
    思考按模型分桶；主模型思考不带 model，仍走正文条目内的弱化区。
    """
    if ev_type not in TOKEN_EVENT_TYPES:
        return {}
    try:
        text = text or ""
        if not text:
            return {}
        key = _key(work_id, card_id)
        journal = _token_journals.setdefault(key, [])
        data: dict[str, Any] = {"text": text}
        if extra:
            data.update(extra)
        # [迭代一·A5/T097] seq 单一取号：与主通道共用计数器，帧仅携带
        evt = _attach_anchors(key, {
            "seq": _next_seq(key),
            "ts": time.time(),
            "type": ev_type,
            "data": _jsonable(data),
        })
        journal.append(evt)
        # [对话流渲染 T4] 已删 5000 条环形弹出（原 pop(0) 丢前字）
        # 逐字流每帧给 /progress 打一次心跳：仍在吐字的超长请求持续被判为
        # 「仍在推进」，刷新后仍能补出「进行中」（V-T4-1）。
        # 只刷 ts、不改 stage —— 逐字流不是流程阶段，汇报 stage 会污染其语义。
        try:
            from . import progress as _progress
            _progress.heartbeat(work_id, card_id)
        except Exception:  # noqa: BLE001
            pass
        _fanout(_token_subscribers.get(key, ()), evt, work_id, card_id, "逐字流")
        return evt
    except Exception as exc:  # noqa: BLE001 展示层零阻断
        logger.warning("[flow] emit_token 失败（不阻断主流程）type=%s card=%s: %s",
                       ev_type, card_id, exc)
        return {}


def token_journal_of(work_id: str, card_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    """逐字流存量（from=0 重放源）。``kind`` 可选过滤：content_chunk / reasoning_chunk。"""
    items = list(_token_journals.get(_key(work_id, card_id), ()))
    if kind:
        items = [e for e in items if e["type"] == kind]
    return items


async def subscribe_tokens(work_id: str, card_id: str, from_seq: int = 0) -> AsyncIterator[dict[str, Any]]:
    """逐字流 SSE 订阅：先重放存量（seq>=from_seq），再实时跟进。断连自动清理。

    队列满由 ``_fanout`` 处理为「丢最旧中间帧、保最新 seq、打警告」（不静默丢）。
    """
    key = _key(work_id, card_id)
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _token_subscribers.setdefault(key, set()).add(queue)
    try:
        for evt in token_journal_of(work_id, card_id):
            if evt["seq"] >= from_seq:
                yield evt
        last = from_seq - 1
        while True:
            evt = await queue.get()
            if evt["seq"] <= last:
                continue
            last = evt["seq"]
            yield evt
    finally:
        try:
            _token_subscribers.get(key, set()).discard(queue)
        except Exception:  # noqa: BLE001
            pass


def token_subscriber_count(work_id: str, card_id: str) -> int:
    return len(_token_subscribers.get(_key(work_id, card_id), ()))
