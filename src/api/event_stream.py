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
[模型过程数据 2026-10-12] 判断/拆解/步进/步开始四类过程数据补全：主通道
+judgment＋step_start（本体论 19 → 21 原 + 2 逐字流 = 23），plan_created/
step_advanced/tool_call/tool_result/seat_end 只补字段不换通道。
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
import os
import time
from datetime import datetime
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


def clear_stream_context():
    """把本协程上下文的逐字流归属清空（返回 token 供还原）。

    [2026-11-01 凝练流泄露实案] ``create_task`` 复制 contextvar 上下文——
    回合末异步调度的凝练/归拢任务继承了卡回合的 stream_ctx，管线调用的
    reasoning/content 帧被归到该卡名下落进对话流（card_104 的 reasoning
    事件里出现 L2 子题生成过程）。管线内部模型调用一律经本函数脱钩。
    """
    return _stream_ctx.set(None)


def current_stream_context():
    return _stream_ctx.get()

# [信息流重出 Task4] 逐字流事件：新增 2 类 → 事件本体论 17 → 19。
# 独立成对：走**独立 SSE 流 + 独立 journal + 独立 seq 计数器**（红线4
# 「与既有 17 类隔离、不混合排 seq」）。
TOKEN_EVENT_TYPES: tuple[str, ...] = ("reasoning_chunk", "content_chunk")

# 事件本体论（原 19 类 = 17 原 + 2 逐字流；V-T4-2 判据锚）。
# [模型过程数据 2026-10-12] 主通道 +2 类 → 21 原 + 2 逐字流 = 23：
#   judgment  —— 首轮机械推断（tool_calls 含 create_plan → plan，否则 direct）；
#   step_start —— 每步开始（当前板步首次被观测时发一次）。
# 只补类型与字段，/stream、/tokens 两条通道与传输协议不动。
EVENT_TYPES: tuple[str, ...] = (
    "run_start", "context", "round_start", "round_end", "rescue",
    "tool_call", "tool_result", "seats_enter", "seat_start", "seat_end",
    "deliver", "proposal", "done", "error", "ping",
    "card_op",
    "triage_verdict", "plan_created", "step_advanced",
    "judgment", "step_start",
    # [当前轮注入消息事件 2026-10-14] 两类注入消息（本体论 23 → 25）：
    #   context_preamble  —— 固定总领（思维链固定执行纪律；工具卡本任务不发）；
    #   context_injection —— 动态步骤注入（工具卡步骤 / 思维链当前步）。
    # 语义与顺序见 docs/当前轮注入消息事件-施工方案-20261014.md。
    "context_preamble", "context_injection",
    # [判断协议改注入 2026-10-14] 判断协议注入（本体论 25 → 26）：从 BASE_PROMPT
    # 摘出，改在「本回合首次调用、且本卡无活板」时注入一条（第一人称自述）。单列
    # 一类——语义是「入口判断协议」，不同于 context_preamble（工作规范）。
    "judgment_protocol",
    # [三席身份注入 2026-10-14] 三席身份注入（本体论 26 → 27）：提交轮三席进场时
    # 一条，role=assistant 自述、抬头〔三席身份〕。单列一类——语义是「三席身份」，
    # 既非固定总领也非判断协议（复用会让前端误标「上下文注入·固定总领」）。
    "context_seat_identity",
    # [更新现状按钮改造 2026-10-16] 「更新现状」专用操作回合的轮首任务注入（本体论 27 → 28）：
    # 点按钮＝一次带 op 的专用回合，轮首注入一条任务指令＋当前现状全文，role=assistant
    # 自述、抬头〔现状更新〕。单列一类——语义是「专用操作任务」，复用 judgment_protocol
    # 或 context_preamble 都会让前端把标签渲染错。
    "status_update_task",
    # [需求确定按钮改造 2026-10-29] 【需求确定】按钮的两种专用回合任务注入
    # （本体论 29 → 31）：根卡那一轮直接改正稿（requirements_final_task）、
    # 其他卡那一轮拟草稿后开评审（requirements_draft_task）。单列两类——
    # 注入的正文与流程不同，复用 status_update_task 会让前端把标签渲染错。
    "requirements_final_task", "requirements_draft_task",
    # [运行态单一真相源 2026-10-21] 回合终止帧（本体论 28 → 29）：``_run_message``
    # 的 finally 里无条件发一次，携带 outcome（ok / error / aborted）。它是**运行态**
    # 的权威终止信号——done/error 是「内容层」收口（由 compose_delivery /
    # handle_error 发，异常早于二者时就没有），turn_end 保证任何路径都有终点。
    # 前端**不把它渲染成行**（stream.js applyLiveFrame 直接跳过），只用于收口。
    "turn_end",
) + TOKEN_EVENT_TYPES

# [对话流渲染 T4] 以下两个上限常量已删——都是「丢字/丢步骤闸」：
#   单帧 8KB 数据上限：截断丢后半；
#   主通道 300 条条数上限：环形弹出丢最前面的步骤。
#   [画板四机制 T6 2026-10-16] 这里原写「两个 journal 都由 run_start 每轮清空、天然有界」
#   ——**该说法已过时**：T092 已把「run_start 清空整本」退役（旧段保留）。内存有界现由
#   `_trim_journals` 的**滚动窗**负责（保最近 FLOW_JOURNAL_MAX_RUNS 轮，默认 50）。
#   二者不矛盾：T092 退役的是「清空整本」，T6 是「有界滚动窗」（见 _trim_journals 注释）。

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
    # [幻影转圈修复 2026-11-06] turn_end 是 _run_message finally 无条件发的
    # 权威终止帧（turn 死在 handle_error 之前时 done/error 都不发）——
    # 漏了它 ⇒ 死 turn 的 progress.active 永真 ⇒ 左栏永久转圈。映射 done
    # 即终态（outcome ok/error/aborted 一律收口）。
    "turn_end": "done",
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


# ------------------------------------------------------------
# [消息底座能力对齐 2026-10-13 · 件2] 事件落盘通道（裁定 C1/C2/C7）
# ------------------------------------------------------------
# 内存 journal 是**实时 SSE 源**（进程内、重启即失）；works/{work}/events/
# {card}.jsonl 是**可回溯的时序真相**（append-only、重启续号）。两路并存：
# 一路管「现在正在发生什么」，一路管「过去发生了什么」——不互相替代。
# 落盘字段：run_id＝回合锚 pair_id，round＝轮锚 turn，model 仅 reasoning 带。
# 零阻断：落盘失败只记 warning，不影响 SSE 与主流程（展示层同族纪律）。
# [reasoning 合并落盘 2026-10-20] 逐帧落盘会把一次思考拆成上千条事件：
# 实测 card_005 17394 条事件里 16822 条是 reasoning（占 87% 字节 / 3.64MB），
# 对话页回放时装配器逐条 rev++ 重渲染 ⇒ 页面卡死。改为**按轮累积、到轮界合并落一条**：
# 实时 SSE 逐帧不变（前端仍逐字出），只是**落盘**合并。
# [5715 · 修] 缓冲**必须按 (model, run, round) 分桶**：
#   ① **model 维度**：三席的思考是**并发交织**的（每个席位一个 model）⇒ 只按卡分桶
#      会把甲席与乙席的思考**混成一条**落盘 ⇒ 前端按 model 分桶时只看到一条
#      ⇒ 席位并排显示串桶。
#   ② **run/round 维度**：同一席位在同一张卡的**不同轮**（或不同 run）也是不同的
#      思考段 ⇒ 混成一条会让「这一轮想了什么」失真。
#   （原提交自己写的意图就是「按 (card, run_id, round, model) 累积」，实现漏了后三维。）
_reason_buf: dict[tuple[str, str], dict[tuple[str, str, int], dict[str, Any]]] = {}


def flush_pending_reasoning(work_id: str, card_id: str) -> None:
    """把缓冲里的思考落盘（回合收口/异常时调用）——**不丢字**。

    思考是「到轮界才合并落一条」的；万一某一轮没走到任何轮界事件（异常/中断），
    缓冲里的思考会随回合一起丢掉 ⇒ 收口时无条件 flush 一次（空缓冲是 no-op）。
    """
    _flush_reasoning(work_id, card_id)


def _flush_reasoning(work_id: str, card_id: str) -> None:
    """把累积的 reasoning 文本**按 (model, run, round) 各自**合并成一条落盘。"""
    key = _key(work_id, card_id)
    by_bucket = _reason_buf.pop(key, None)
    if not by_bucket:
        return
    # [占位思考过滤 2026-11-05] echo_fill 端点的 wire 兜底占位串可能被端点/模型
    # 原样回吐成"思考"——它是格式件不是思考，不落盘（懒导入防环：gateway→api）。
    from ..model_gateway.gateway import REASONING_PLACEHOLDER as _RP
    for (model, run_id, round_), buf in by_bucket.items():
        text = (buf.get("text") or "").replace(_RP, "")
        if not text.strip():
            continue
        try:
            from ..storage import event_log
            event_log.append_bg(work_id, card_id, {
                "run_id": run_id or buf.get("run_id") or "",
                "round": int(round_ or 0),
                "type": "reasoning",
                "data": {"text": text},
                "model": model or "",
                "ts": buf.get("ts"),
            })
        except Exception as exc:  # noqa: BLE001 展示层零阻断
            logger.warning("[flow] reasoning 合并落盘失败 card=%s: %s", card_id, exc)


def _persist_log(work_id: str, card_id: str, ev_type: str,
                 data: Any, model: str = "", ts: Any = None) -> None:
    try:
        from ..storage import event_log
        anchor = _anchors.get(_key(work_id, card_id)) or {}
        payload = data if isinstance(data, dict) else {"value": data}
        event_log.append_bg(work_id, card_id, {
            "run_id": str(anchor.get("pair_id") or ""),
            "round": int(anchor.get("turn") or 0),
            "type": ev_type,
            "data": payload,
            "model": model or "",
            # [对话界面商业化 2026-10-17] 落盘时间取帧的 ts（同一事件内存与磁盘
            # 同一时刻，刷新前后时间轴一致）。秒级 float → ISO；缺省由
            # event_log._normalize 补当前时刻。
            "ts": _iso_ts(ts),
        })
    except Exception as exc:  # noqa: BLE001 展示层零阻断
        logger.warning("[flow] 事件落盘失败（不阻断）type=%s card=%s: %s",
                       ev_type, card_id, exc)


def _iso_ts(value: Any) -> str:
    """秒级时间戳（或 ISO 串）→ ISO 8601 字符串；无法识别则空串（交给下游补）。"""
    if not value:
        return ""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value)).astimezone().isoformat(
                timespec="milliseconds")
        s = str(value)
        # 已是 ISO 就原样（只做一次可解析性校验）
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return s
    except (ValueError, OSError, OverflowError):
        return ""


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


def _put_evt(q: Any, evt: dict[str, Any], work_id: str, card_id: str, channel: str) -> None:
    """单订阅队列投递（含满队列丢旧保新）——**必须在队列所属 loop 上调用**。"""
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


def _fanout(subs: Any, evt: dict[str, Any], work_id: str, card_id: str, channel: str) -> None:
    """SSE 订阅 fanout：队列满**不静默丢**（[对话流渲染 T4]）。

    原写法 ``q.put_nowait(evt)`` 在 QueueFull 时被 except 静默吞掉——用户要看的
    字/步骤就这样悄悄没了、且无任何痕迹。现改为：队列满则弹出其中最旧一帧腾位，
    再把**最新**帧放入，并每次打 warning 留痕。

    - 保「最新」＝保最新 seq 游标可继续推进；
    - 丢「最旧中间帧」＝只损失已排队但未送达的旧帧，这些帧仍在 journal 里，
      前端 from=0 重放可补齐；
    - 后端**不补任何补丁帧**（seq 空洞为正常态，接口契约不变）。

    [双循环隔离 2026-11-05] 订阅队列活在**服务循环 B**（SSE 消费方），而 emit
    跑在**主循环 A**（跑卡域）——asyncio.Queue 跨 loop ``put_nowait`` 唤不醒
    属主 loop 上的 ``queue.get()``（唤醒走属主 loop 的 call_soon，非线程安全）。
    ⇒ 每个队列创建时烙 ``_es_owner_loop``；fanout 逐队列判属主：同 loop 直接
    put，跨 loop 走 ``call_soon_threadsafe`` 回属主 loop 上 put。
    未烙印（旧订阅者/测试）退化为原地 put——单循环语义不变。
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:  # 无线程本地运行环（后台线程 emit）
        running = None
    for q in list(subs):
        owner = getattr(q, "_es_owner_loop", None)
        if owner is None or owner is running:
            _put_evt(q, evt, work_id, card_id, channel)
            continue
        try:
            owner.call_soon_threadsafe(_put_evt, q, evt, work_id, card_id, channel)
        except RuntimeError:  # noqa: BLE001 属主 loop 已关闭：该订阅者随连接死
            pass
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


# ------------------------------------------------------------
# [画板四机制 T6 2026-10-16] 内存 journal 滚动窗（治 P7）
# ------------------------------------------------------------
# 病：`_journals` / `_token_journals` 只 append 不减 ⇒ 一张长活卡的内存占用，以及
#     `/stream` 每 0.4s 一次的 `journal_of` 全量拷贝，都随轮数线性膨胀。
# 药：每开新轮（run_start）把两个 journal 裁到**最近 N 轮**（默认 50）。
#
# ★ 与 T092 裁定的关系（防后人误判二者矛盾）：T092 退役的是「run_start **清空整本**」
#   ——那会让「上一轮过程被下一轮开始抹掉」；本项是「**有界滚动窗**」——旧段照留，
#   只把最老的轮整段丢掉。语义不同，二者不冲突，**不是**把 T092 恢复回来。
#
# 四条铁律（施工与验收都按这查）：
#   ① 磁盘零改——events/{card}.jsonl 照落全量。本函数只动内存 list；`emit` 里落盘
#      （_persist_frame/_persist_log）**先于**裁剪完成。timeline/恢复读磁盘
#      （event_log.timeline），完全不受影响。
#   ② seq 计数器不重置——`_next_seq` 用 `_seq_counters`，与 journal 长度无关，
#      裁头部后新帧 seq 依旧单调。
#   ③ `journal_last_seq` 语义不变——/stream 起流游标（from=0 落末帧）与断点续传
#      （from>0 过滤 seq>frm）只看**末帧**，对头部裁剪无感。
#   ④ 裁剪时机放 run_start（开新轮）而非每次 emit——触发次数＝轮数，成本最低。
def _journal_max_runs() -> int:
    """滚动窗上限（**轮**数）。环境变量 `FLOW_JOURNAL_MAX_RUNS`，默认 50。

    惰性读取（先例：`memory_store` 的 CONDENSE_TRIGGER_TOKENS，同样 os.environ 惰性
    读取——测试可 monkeypatch）。非法值退回 50 并留 warning；0 或负＝不裁（关窗）。
    """
    raw = os.environ.get("FLOW_JOURNAL_MAX_RUNS", "").strip()
    if not raw:
        return 50
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("[flow] FLOW_JOURNAL_MAX_RUNS 非法（%r），用缺省 50", raw)
        return 50


def _cut_at(book: list[dict[str, Any]] | None, maxr: int, is_boundary) -> None:
    """把 book 原地裁到「最后一个边界帧起、往回数 maxr 个边界」处（删头）。

    边界落在**轮首**，故窗口不会切碎某一轮；book 为空或边界不足 maxr 时不动。
    """
    if not book:
        return
    starts = [i for i, e in enumerate(book) if is_boundary(e)]
    if len(starts) > maxr:
        # 保**尾部**最近 maxr 轮：从第 (n-maxr) 个轮首起保留，其前的整段删掉。
        del book[:starts[len(starts) - maxr]]


def _trim_journals(key: tuple[str, str]) -> None:
    """把某卡的两个内存 journal 裁到最近 N 轮（N＝`_journal_max_runs()`）。

    轮界：主 journal 以 `type == "run_start"` 计；逐字流以 `data.segment == "run"`
    的分段帧计（`_emit_token_segment` 所落）。两个 journal 用**同一 N**，各自按
    自己的轮界裁，故窗口对齐在轮首。
    """
    maxr = _journal_max_runs()
    if maxr <= 0:
        return
    _cut_at(_journals.get(key), maxr, lambda e: e.get("type") == "run_start")
    _cut_at(_token_journals.get(key), maxr,
            lambda e: (e.get("data") or {}).get("segment") == "run")


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
        # 轮界先落掉累积的思考文本（保证顺序：reasoning 在后续事件之前）
        _flush_reasoning(work_id, card_id)
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
        # [消息底座能力对齐 · 件2] 过程事件同步落 events jsonl（裁定 C1）
        _persist_log(work_id, card_id, ev_type, evt["data"], ts=evt.get("ts"))
        # [画板四机制 T6] 每开新轮裁一次（**先落盘、后裁剪**：磁盘照落全量，内存有界）。
        # 放在 run_start 分支之外、两个 append 之后——此刻主 journal 与本轮逐字流
        # 分段帧都已入册，两个 journal 的轮数一致，窗口对齐在轮首。
        if ev_type == "run_start":
            _trim_journals(key)
        _fanout(_subscribers.get(key, ()), evt, work_id, card_id, "主通道")
        # [P28 · 连接预算 2026-10-26] 卡级帧**顺带**投给工作级的「卡级帧订阅者」。
        # 目的：让**一条**工作级连接带全部卡的帧 ⇒ 浏览器连接数与卡数**解耦**
        # （浏览器同源 HTTP/1.1 只有 6 条；画布原来给每张被盯的卡各开一条）。
        # 详见本文件「工作级流『顺带』带卡级帧」一节。
        _fanout_work_card(work_id, card_id, evt)
        # 兼容映射（旧 /progress 零感知）
        stage = _PROGRESS_STAGE.get(ev_type)
        if stage:
            try:
                from . import progress
                progress.set_stage(work_id, card_id, stage, str((data or {}).get("detail") or ev_type))
            except Exception:  # noqa: BLE001
                pass
        # 审计打点（与事件分离的日志通道；只写不判）
        logger.info("[flow] type=%s work=%s card=%s seq=%s data=%s",
                    ev_type, work_id, card_id, evt["seq"],
                    json.dumps(evt["data"], ensure_ascii=False))
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
    queue._es_owner_loop = asyncio.get_running_loop()  # [双循环] 烙印属主
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
# [画布 5711] 工作级事件广播 —— 画布"实时更新"的地基
# ============================================================================
#
# 病根（实测复现）：画布上卡片的**结论 / 待裁定数 / work_state / 运行态 / 最新对话**
# 只有 renderAll（全量重建）才会更新；而 renderAll **只由用户自己的操作触发**
# （建卡、删卡、重命名、拖动落定…）。后台真实发生的变化——模型写卡、裁定被记下、
# 自动模式推进——**没有任何通道能通知画布**。⇒ 用户看到的画布永远停在"我上次点它时"
# 的样子 ⇒ 必须手动刷新页面。
#
# 断点（逐条核实过，不是推测）：
#   ① 既有 SSE 是**单卡**的：订阅表 key=(work, card)，没有工作级广播；
#   ② canvas.js 订阅的是 card_id=WORK_ID（根卡）那一条流 ⇒ 别的卡的
#      card_message 收不到；
#   ③ /works/{w}/pulse 的 running/recent 不驱动卡面重绘（applyLiveStates 只写
#      dataset.live 与一行消息条，conclusion/pending_count/work_state 一个都不碰）；
#   ④ 结论/待裁定/work_state/runstate 都写在 createNode 里 ⇒ 只有重建节点才生效。
#
# 解法：新增**工作级**订阅（key=work_id）。写卡路径发一帧 `card_changed`
# （带该卡的最新投影摘要），画布页订阅后**就地换掉那一张卡的节点**，
# 不重建整棵树 ⇒ 毫秒级、零闪烁。
#
# 与主通道的隔离（沿用逐字流通道的既有纪律）：
#   - 不进 _journals、不占主 seq（工作级有自己的 _work_seq）；
#   - 不写 works 文件、不进 session/prior/模型输入；
#   - 零阻塞、零抛出（展示层绝不影响主流程）。
# ============================================================================

_work_subscribers: dict[str, set[asyncio.Queue]] = {}
_work_seq: dict[str, int] = {}

#: 工作级事件落盘用的**伪卡号**：它不是真实卡片，只作为事件流的"工作级"维度，
#: 落到 works/{work}/events/_work.jsonl，并在汇总表里以 "_work" 单独统计。
#: 用固定值而不是空串，是为了让"按卡分开记录"这条在汇总表里没有例外分支。
_WORK_EVENT_CARD = "_work"


def emit_work(work_id: str, ev_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """工作级事件广播。零阻塞零抛出。

    帧格式与主通道一致（seq/ts/type/data），便于前端用同一套解析。
    """
    try:
        if not work_id:
            return {}
        _work_seq[work_id] = _work_seq.get(work_id, 0) + 1
        evt = {
            "seq": _work_seq[work_id],
            "ts": time.time(),
            "type": ev_type,
            "data": _jsonable(data or {}),
        }
        _fanout(_work_subscribers.get(work_id, ()), evt, work_id, "*", "工作级")
        # [基础设施 2026-10-26 · 事件管理] 工作级事件**也落盘**。
        # 改前这里只广播、不写盘 ⇒ card_changed / card_running / 工作级
        # card_message 零持久记录（卡面编辑在事件流里一条都没有，正是这个缺口）。
        # 落点＝固定的工作级伪卡号 "_work"（works/{work}/events/_work.jsonl），
        # 不污染任何真实卡的事件流；汇总表里同样以 "_work" 这一"卡"分开统计。
        try:
            from ..storage import event_log
            event_log.append_bg(work_id, _WORK_EVENT_CARD, {
                "run_id": "",
                "round": 0,
                "type": ev_type,
                "data": _jsonable(data or {}),
                "ts": _iso_ts(evt.get("ts")),
            })
        except Exception as exc:  # noqa: BLE001 落盘失败不阻断广播
            logger.warning("[flow] 工作级事件落盘失败（不阻断）type=%s work=%s: %s",
                           ev_type, work_id, exc)
        logger.info("[flow] work-event type=%s work=%s seq=%s data=%s",
                    ev_type, work_id, evt["seq"],
                    json.dumps(evt["data"], ensure_ascii=False))
        return evt
    except Exception as exc:  # noqa: BLE001 展示层零阻断
        logger.warning("[flow] emit_work 失败（不阻断主流程）type=%s work=%s: %s",
                       ev_type, work_id, exc)
        return {}


async def subscribe_work(work_id: str) -> AsyncIterator[dict[str, Any]]:
    """工作级 SSE 订阅：只推「连上之后新发生的事」（无存量重放）。

    为什么不重放：工作级事件描述的是"变化"而不是"状态"，重放旧变化会让前端
    以为卡刚变过。连上之后的每一次变化都会再触发一次前端刷新，重放是冗余的。
    断连自动清理订阅者队列。
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    queue._es_owner_loop = asyncio.get_running_loop()  # [双循环] 烙印属主
    _work_subscribers.setdefault(work_id, set()).add(queue)
    try:
        while True:
            evt = await queue.get()
            yield evt
    finally:
        try:
            _work_subscribers.get(work_id, set()).discard(queue)
        except Exception:  # noqa: BLE001
            pass


def work_subscriber_count(work_id: str) -> int:
    return len(_work_subscribers.get(work_id, ()))


# ============================================================================
# [P28 · 连接预算 2026-10-26] 工作级流「顺带」带卡级帧
# ============================================================================
#
# 病根（实测，不是推测）：浏览器同源 HTTP/1.1 只有 **6 条**并发连接，而画布
# 「监控态」原来给**每张被盯的卡**各开一条 SSE（canvas.js 的 watchStream）——
# 10 张卡并行 ⇒ 10 条 SSE ⇒ **必然打满** ⇒ 普通请求（timeline / card / nav）
# 排队拿不到连接 ⇒ fetch 抛 TypeError ⇒ 弹「网络错误」、切卡卡死。
#
# 关键事实（canvas.js 自己写着）：那条每卡 SSE **不承载内容**——内容是 3 秒
# 轮询拉的，SSE 只当「有变化了，去重拉」的触发器。
# ⇒ 每卡一条连接换来的信息量，等于工作级流里多带几个字节。
#
# 解法：卡级帧**顺带**投给工作级订阅者 ⇒ 一条工作级连接带全部卡的帧
# ⇒ **连接数与卡数解耦**（画布 1 条 ⇒ 10 张卡也还是 1 条）。
#
# 隔离纪律（沿用工作级既有纪律，逐条）：
#   - **单独的订阅表**（_work_card_subscribers），不混进 _work_subscribers
#     ⇒ 不订阅卡级帧的连接（对话页）零浪费；
#   - **opt-in**：路由层 ?cards=1 才挂上；
#   - **逐字帧不投**（content_chunk / reasoning_chunk）——那是"每个 token 一帧"，
#     10 张卡并行会把工作级队列打爆，而画布并不需要逐字；
#   - 帧加 ``card`` 字段（卡号）⇒ 与工作级原生帧（card_changed / card_message /
#     card_running，**顶层没有** card 字段）可区分。
#     ⚠ 卡级帧**也可能有 type="card_message"**（card_tools.py 落卡间信走的就是
#     emit()）⇒ 前端必须**先判 card 再判 type**，否则卡级卡间信会被当成工作级帧
#     处理掉（丢掉那张卡的刷新触发）。
# ============================================================================

_work_card_subscribers: dict[str, set[asyncio.Queue]] = {}


def _fanout_work_card(work_id: str, card_id: str, evt: dict[str, Any]) -> None:
    """把一张卡的帧投给该 work 的「卡级帧订阅者」（帧加 ``card`` 标记）。"""
    subs = _work_card_subscribers.get(work_id)
    if not subs:
        return
    try:
        frame = dict(evt)
        frame["card"] = card_id
        _fanout(subs, frame, work_id, card_id, "工作级·卡帧")
    except Exception:  # noqa: BLE001 展示层零阻断
        pass


async def subscribe_work_cards(work_id: str) -> AsyncIterator[dict[str, Any]]:
    """工作级「卡级帧」订阅（opt-in）。只推「连上之后新发生的事」（无存量重放）。

    为什么不重放：与工作级原生帧同口径——卡级帧描述的是"变化"不是"状态"，
    重放旧变化会让前端以为卡刚变过（画布会白刷一轮）。
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    queue._es_owner_loop = asyncio.get_running_loop()  # [双循环] 烙印属主
    _work_card_subscribers.setdefault(work_id, set()).add(queue)
    try:
        while True:
            yield await queue.get()
    finally:
        try:
            _work_card_subscribers.get(work_id, set()).discard(queue)
        except Exception:  # noqa: BLE001
            pass


def work_card_subscriber_count(work_id: str) -> int:
    return len(_work_card_subscribers.get(work_id, ()))


def journal_last_seq(work_id: str, card_id: str) -> int:
    """主 journal 当前末帧 seq（无帧＝0）。

    [消息底座能力对齐 · 件4] 恢复统一走 /timeline（C4）：SSE from=0 不再整卡
    重放，起流游标直接落在末帧——只推「连上之后新发生的事」。
    """
    j = _journals.get(_key(work_id, card_id)) or []
    return int(j[-1]["seq"]) if j else 0


def token_journal_last_seq(work_id: str, card_id: str) -> int:
    """逐字流 journal 当前末帧 seq（无帧＝0）。同 journal_last_seq 语义。"""
    j = _token_journals.get(_key(work_id, card_id)) or []
    return int(j[-1]["seq"]) if j else 0


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
        # [消息底座能力对齐 · 件2] 思考增量落盘 type=reasoning（裁定 C2）：
        # 三席带 model（extra.model＝席位模型名，前端据此分桶），主模型空串。
        # 正文增量（content_chunk）**不落盘**——最终 assistant_text 一条落盘（裁定 C7）。
        if ev_type == "reasoning_chunk":
            # 累积到缓冲，等轮界（emit 任意其它事件时）合并落一条
            # [5715 · 修] 缓冲**按 (model, run, round) 分桶**（三席的思考并发交织
            # ⇒ 不能混成一条；同一席的不同轮也是不同的思考段）。
            _rk = _key(work_id, card_id)
            _anchor = _anchors.get(_rk) or {}
            _model = str(data.get("model") or "")
            _run_id = str(_anchor.get("pair_id") or "")
            _round = int(_anchor.get("turn") or 0)
            _bk = (_model, _run_id, _round)
            _by_bucket = _reason_buf.setdefault(_rk, {})
            _b = _by_bucket.get(_bk)
            if _b is None:
                _b = {"text": "", "model": _model, "run_id": _run_id,
                      "round": _round, "ts": evt.get("ts")}
                _by_bucket[_bk] = _b
            _b["text"] += text
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
    queue._es_owner_loop = asyncio.get_running_loop()  # [双循环] 烙印属主
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
