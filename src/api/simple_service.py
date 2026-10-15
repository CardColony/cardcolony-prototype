# -*- coding: utf-8 -*-
"""[phase9-E] simple 多模型编排器：观点并发 + 单模型统合。

[思维链真实落地 2026-10-11] 本件收敛为**提交轮三席统合**的调用层（C3/C6）：
三席（run_viewpoints）与统合官由 message_runner.run_seats 在 board 收口后
调用——三席输入＝全部步 result＋判据满足情况＋工具结果全文，质询「成果真
满足判据了吗、解决用户问题了吗」；统合官 call_main 统合交付。旧观点前置
编排（call_simple）与旧后置注入件（build_informed_prompt）随三席旁路机制
整体退役。

设计依据（用户设计哲学）：模型只生成假设，确定性代码负责验证与裁决；
多模型出观点=群体审议，统合模型是唯一建卡/工具出口。

- 观点阶段（run_viewpoints）：并发调 bindings.viewpoint 绑定的各模型，tools=None、
  跳过完成标记；单个失败记 meta 不中断；观点文本用 card_proposal
  剥离函数剥净协议块，另剥 <think> 推理块（MiniMax M 系实测全系带）。
- 注入（build_review_prompt）：步成果（result＋criteria＋criteria_met）
  与工具结果全文进三席输入（不折叠不截断）。
- 统合阶段：user_prompt 追加参考观点段（build_viewpoint_prompt），走
  call_main（带 tools）。
- enabled: false 或 viewpoint 绑定为空 → 三席零调用（run_viewpoints 返回 ([], [])）。
- 观点全失败 → 留痕（warning），主模型携步成果独自交付（口径②）。
- 上下文回流铁则：观点全文不进 session、不进 prior_messages（由 runner 保证）。

[思考流支持层 2026-10-12] 三席思考与主模型一样走 think 流展示，并拆两个雷：
- **雷①**（clean_viewpoint_text 对未闭合思考块整段作废 → 「观点为空」）：改为按
  开始标记切分——标记前正文保留、标记后的思考区剥掉；整段都是思考才返空串。
- **雷②**（非流式 gw.call 只认 reasoning_content 单字段、思考无处可取）：三席改走
  ``gw.stream_call``（网关既有能力，gateway.py 一字不碰），思考增量经
  ``chunk_type=reasoning`` 与本模块的标签切分器双路汇入 ``event_stream.emit_token``
  → ``/tokens`` → 前端按模型分组的弱化区。
- 铁则：思考增量**只**走 /tokens 展示通道，不进 session / prior / 模型输入；
  三席正文增量一律**不**上 /tokens（观点不是助手回复正文）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

from . import model_config, model_service
from .card_proposal import parse_proposals

logger = logging.getLogger("api.simple_service")

# [无内容预设改造 2026-10-02] 统合头中性化：只陈述事实，不含"可采纳可反驳"等处理方式指令。
# [职责分离裁定 2026-10-02 15:07] 注入段去掉模型名（模型名诱发统合官的裁决者姿态），
# 观点按序号标注；模型名只留在 meta/log 供审计，不进统合官输入。
VIEWPOINT_HEADER = "## 其他模型的输出"

# [T10-6-1/6-2] 任务说明定稿句（逐字）：插在 VIEWPOINT_HEADER 之后、三段分析之前，
# 给统合官划清「参考材料」与「用户问题」的界线（V8 判据）。
# 段名「## 其他模型的输出」保持逐字不改（6-3 既有契约）；三段匿名形态不动（6-4，用户已裁定）。
VIEWPOINT_TASK_NOTE = "以下是三份独立分析的参考材料，不是用户的问题。你的任务是回答上面用户提出的问题。"

# ============================================================
# [思考流支持层 2026-10-12] 各家思考块形态表（探针实测 2026-10-12）
# ============================================================
# 思考的落点分两类，适配方式不同：
#
#   ① 字段类（delta.reasoning_content / delta.reasoning）
#      —— qwen-max、glm-5.3、deepseek-pro、deepseek-flash、haiku 一类。
#      网关 stream_call 已双路解包并 yield {"chunk_type": "reasoning"}，
#      本模块零解析，直接转发 /tokens。
#
#   ② 标签内联类（思考带 <think>…</think> 标签**混在 content 里**）
#      —— minimax-m3 **实测**：thinking.type 只接受 adaptive|disabled（传
#      enabled 直接 400：invalid thinking.type）；不关思考时 delta 里**没有**
#      reasoning 字段，思考以标签文本内联在 delta.content 中（探针实测
#      <think>@0 </think>@333，闭合后是干净正文）。故须由 ThinkStreamSplitter
#      在流上按标签边界切出思考、剥离到展示通道。
#
# 两类并存时：字段类原样转发 + 内联类切分转发，同一席两个来源都汇进同一条
# think 流（前端按 model 分桶，不区分来源）。
_THINK_TAGS: tuple[tuple[str, str], ...] = (
    ("<think>", "</think>"),          # minimax-m3 实测形态
    ("<thinking>", "</thinking>"),    # 部分端点的写法，一并适配
)
_THINK_OPEN = tuple(p for p, _q in _THINK_TAGS)
_THINK_CLOSE = tuple(q for _p, q in _THINK_TAGS)
_ALL_THINK_TAGS = tuple(sorted({t for pair in _THINK_TAGS for t in pair},
                               key=len, reverse=True))
_MAX_TAG_LEN = max(len(t) for t in _ALL_THINK_TAGS)
# 闭合思考块：成对整段剥离
_THINK_BLOCK_RES = tuple(
    re.compile(re.escape(p) + r".*?" + re.escape(q), re.DOTALL) for p, q in _THINK_TAGS
)


class ThinkStreamSplitter:
    """把**内联在正文流里**的思考块按标签边界切出来（逐帧有状态，纯逻辑可单测）。

    标签可能跨帧切裂（"<thi" + "nk>…"），故帧尾不足一个标签的残片留在缓冲，
    等下一帧补齐再判；缓冲也可能是标签前缀（"</think>" 的 "</thin"），只有
    真正补齐了才判定。in_think 期间到闭合标签之间的文本＝思考；之外＝正文。
    """

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""

    @property
    def in_think(self) -> bool:
        return self._in_think

    @property
    def pending(self) -> str:
        """尚在缓冲、不足以判定的残片长度（诊断用）。"""
        return self._buf

    def feed(self, delta: str) -> tuple[str, str]:
        """吃一帧正文增量 → (思考增量, 正文增量)。两路都只含**标签之间的字**，不含标签本身。"""
        buf = self._buf + (delta or "")
        think_parts: list[str] = []
        body_parts: list[str] = []
        while buf:
            idx = -1
            tag = ""
            for t in _ALL_THINK_TAGS:
                i = buf.find(t)
                if i >= 0 and (idx < 0 or i < idx):
                    idx, tag = i, t
            if idx < 0:
                keep = self._partial_tag_len(buf)
                rest, self._buf = buf[:len(buf) - keep], buf[len(buf) - keep:]
                (think_parts if self._in_think else body_parts).append(rest)
                break
            before, buf = buf[:idx], buf[idx + len(tag):]
            (think_parts if self._in_think else body_parts).append(before)
            self._in_think = tag in _THINK_OPEN
        return "".join(think_parts), "".join(body_parts)

    def flush(self) -> tuple[str, str]:
        """流结束：把缓冲残片按当前归属吐出（截断帧不因残片被吞字）。"""
        rest, self._buf = self._buf, ""
        return (rest, "") if self._in_think else ("", rest)

    @staticmethod
    def _partial_tag_len(buf: str) -> int:
        """buf 尾部若是某个标签的真前缀，返回该前缀长度（否则 0）。"""
        for k in range(min(len(buf), _MAX_TAG_LEN - 1), 0, -1):
            tail = buf[-k:]
            if any(t.startswith(tail) for t in _ALL_THINK_TAGS):
                return k
        return 0


def clean_viewpoint_text(text: str) -> str:
    """观点文本清洗（纯函数，可单测）：剥思考块 + 剥建卡协议块。

    [拆雷① 2026-10-12] 思考块**未闭合**（模型中断 / 截断）时不再整段作废：
    按开始标记切分——标记**前**的正文保留、标记后未闭合的思考区剥掉；整段都是
    思考（开始标记在开头且无闭合）才返回空串（旧行为在这种情况下逐字不变，
    tests/test_proposal_parse.py 钉的就是这一条）。两种标签写法一视同仁。
    """
    text = text or ""
    for rx in _THINK_BLOCK_RES:
        text = rx.sub("", text)
    # 未闭合的思考区：取最早的开始标记，之前的正文保留、之后的思考丢掉
    idx = -1
    for p in _THINK_OPEN:
        i = text.find(p)
        if i >= 0 and (idx < 0 or i < idx):
            idx = i
    if idx >= 0:
        text = text[:idx]
    clean, _proposals = parse_proposals(text)
    return clean.strip()


def build_viewpoint_prompt(user_prompt: str, ok_metas: list[dict[str, Any]]) -> str:
    """统合阶段 user_prompt 组装（纯函数，可单测）。ok_metas 空则原样返回。

    [职责分离裁定 2026-10-02] 观点按序号标注，不带模型名——
    模型名只留 meta/log 供审计，不进统合官输入（防裁决者姿态诱导）。
    """
    if not ok_metas:
        return user_prompt
    sections = "\n\n".join(
        f"### 输出 {i + 1}\n{m['text']}" for i, m in enumerate(ok_metas)
    )
    # [T10-6-1] 任务说明行插在 VIEWPOINT_HEADER 之后、三段分析之前
    return f"{user_prompt}\n\n{VIEWPOINT_HEADER}\n{VIEWPOINT_TASK_NOTE}\n{sections}"


# ============================================================
# [思维链真实落地 2026-10-11] 提交轮三席质询输入（C3/C6）
# ============================================================

REVIEW_HEADER = "## 本轮计划执行材料"
REVIEW_TASK_NOTE = (
    "上面的任务已按计划逐步执行完毕。请独立质询三问：\n"
    "1. 每一步的成果真满足该步的完成判据了吗？（对照 criteria 逐条核）\n"
    "2. 全部成果合起来，真解决了用户的问题吗？\n"
    "3. 有没有谎报完成——声称已落盘/已完成，但材料不支持？\n"
    "对每问给出结论：通过／不通过＋一句理由。先读下面的执行材料再判断。"
)


def build_review_prompt(user_prompt: str,
                        steps_record: list[dict[str, Any]],
                        criteria_mets: list[str],
                        tool_results: list[str]) -> str:
    """提交轮三席质询输入组装（纯函数，可单测）。

    [C6] 输入＝用户原问题＋全部步的 result 与判据满足情况（steps_record
    每步含 prompt/criteria/deliverable/status/result；criteria_mets＝模型
    每次 step_complete 的 criteria_met 自述，按调用序与步对齐）＋本轮工具
    结果全文（不折叠不截断）。空表 → 原样返回 user_prompt。
    """
    if not steps_record and not tool_results:
        return user_prompt
    sections: list[str] = []
    for i, s in enumerate(steps_record):
        if not isinstance(s, dict):
            continue
        met = criteria_mets[i] if i < len(criteria_mets) else "（无自述）"
        sections.append(
            f"### 步 {s.get('id', i + 1)} [{s.get('status', '')}]\n"
            f"要求: {s.get('prompt', '')}\n"
            f"完成判据: {s.get('criteria', '')}\n"
            f"产出物声明: {s.get('deliverable', '')}\n"
            f"成果(result): {(s.get('result') or '').strip() or '（无）'}\n"
            f"模型自述判据满足: {met}")
    if tool_results:
        sections.append("### 本轮工具查证结果\n" + "\n\n".join(tool_results))
    return f"{user_prompt}\n\n{REVIEW_HEADER}\n{REVIEW_TASK_NOTE}\n" + "\n\n".join(sections)


# ============================================================
# [三席身份注入 2026-10-14] 三席身份提示词 —— 走注入（role=assistant 自述）
# ============================================================

# 抬头：与既有注入同一形态（〔工作规范〕/〔当前流程〕/〔判断协议〕）。
SEAT_IDENTITY_MARK = "〔三席身份〕"

# 身份文案（第一人称自述）：三席共享同一段、不含席号。
# 三席 system（共享卡片上下文）与 user 位质询文（REVIEW_TASK_NOTE 三问）都不动，
# 身份只经这条注入下发。
SEAT_IDENTITY_NOTE = (
    "我是本轮的三位观点审议席之一。三席各自独立并行，我看不到另外两席的输出。\n"
    "\n"
    "我的任务：基于共享的卡片上下文与执行材料，给出我自己的意见。\n"
    "- 我独立判断，不附和；\n"
    "- 我有依据地说，指出依据在哪；\n"
    "- 我的结论明确（通过／不通过、同意／不同意）。"
)


def build_seat_identity_message() -> dict[str, Any]:
    """三席身份注入消息（role=assistant，第一人称自述）。

    经 ``gw.stream_call`` 的 ``turn_tail`` 下发——追加在质询文（user 位）之后，
    与主模型注入同构；不进 system、不进 session/prior。
    """
    return {"role": "assistant", "content": f"{SEAT_IDENTITY_MARK}\n{SEAT_IDENTITY_NOTE}"}


async def run_viewpoints(
    vp_system_prompt: str,
    user_prompt: str,
    prior_messages: Optional[list[dict[str, Any]]] = None,
    on_progress: Optional[Any] = None,
    on_seat: Optional[Any] = None,
    turn_tail: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """三席观点并发（可独立调用，不与 call_simple 绑定）。返回 (ok_metas, 全量 metas)。

    [E-探针制] 自 call_simple 拆出，供 routes 在工具循环收尾处调用——
    三席踩着工具事实出观点。单席语义不变：tools=None（10-02 零工具裁定，
    三席不出现 tools 入参）、skip_completion_marker、失败记 meta 不中断。
    [E-口径②] 全败留痕（warning）不阻断，由调用方决定独自交付。
    enabled: false 或 viewpoint 绑定为空 → ([], [])（零调用）。
    [信息流 Task3 2026-10-07] 增可选参 on_seat(kind, meta)（缺省 None 零行为
    变化）：每席起止转发事件流（seat_start/seat_end），只读不判定、异常静默。
    [三席身份注入 2026-10-14] 增可选参 turn_tail（缺省 None 零行为变化）：三席
    身份注入消息，逐席透传至 gw.stream_call（追加在质询文之后）。
    """
    gw = model_service.get_gateway()
    cfg = gw.config.get("simple") or {}
    viewpoint_binding = model_config.resolve("viewpoint")
    viewpoints = list(viewpoint_binding) if isinstance(viewpoint_binding, list) else []
    if not cfg.get("enabled") or not viewpoints:
        return [], []

    def _report(stage: str, detail: str = "") -> None:
        if on_progress:
            try:
                on_progress(stage, detail)
            except Exception:
                pass

    # [拆闸裁定 2026-10-02] 无 viewpoint_timeout / max_tokens：闸就是空观点的根因
    _report("viewpoints", f"0/{len(viewpoints)}")

    done_count = [0]

    def _vp_done(meta: dict[str, Any]) -> None:
        done_count[0] += 1
        mark = "✓" if meta.get("ok") else "✗"
        secs = round((meta.get("latency_ms") or 0) / 1000, 1)
        _report("viewpoints", f"{done_count[0]}/{len(viewpoints)} · {mark}{meta.get('model','')} {secs}s")

    raw_metas = await asyncio.gather(
        *[
            _call_viewpoint(gw, n, vp_system_prompt, user_prompt,
                            prior_messages, on_done=_vp_done, on_seat=on_seat,
                            turn_tail=turn_tail)
            for n in viewpoints
        ],
        return_exceptions=True,
    )
    metas: list[dict[str, Any]] = []
    for name, m in zip(viewpoints, raw_metas):
        if isinstance(m, BaseException):
            # _call_viewpoint 自吞异常，此处为 gather 双保险
            metas.append({"model": name, "ok": False, "latency_ms": 0,
                          "chars": 0, "error": f"{type(m).__name__}: {m}"})
        else:
            metas.append(m)

    ok_metas = [m for m in metas if m.get("ok")]
    if viewpoints and not ok_metas:
        logger.warning("[E] 三席观点全败（%d 席尝试，留痕不阻断）", len(metas))
    return ok_metas, metas


def _emit_think(text: str, label: str) -> None:
    """思考增量 → /tokens（带 model 标签，前端按席分组）。纯展示层，零阻断。

    无卡上下文（凝练/归拢等后台调用）时不落逐字流——与主模型同语义。
    """
    if not text:
        return
    try:
        from . import event_stream as _es
        ctx = _es.current_stream_context()
        if ctx:
            _es.emit_token(ctx[0], ctx[1], "reasoning_chunk", text,
                           extra={"model": label})
    except Exception:  # noqa: BLE001 展示层零阻断
        pass


async def _call_viewpoint(
    gw: Any,
    name: str,
    system_prompt: str,
    user_prompt: str,
    prior_messages: Optional[list[dict[str, Any]]],
    on_done: Optional[Any] = None,
    on_seat: Optional[Any] = None,
    turn_tail: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """调单个观点模型。永不抛异常，失败记入 meta。

    meta: {model, ok, latency_ms, chars, error?, text?}——text 仅供
    统合与日志用，routes 返回响应前须剔除。
    [信息流 Task3 2026-10-07] on_seat(kind, meta) 每席起止转发（缺省 None
    零行为变化；异常静默）。
    [模型过程数据 2026-10-12] 成功席的 seat_end 事件**带 text 正文全文**
    （与 meta["text"]/prompt 日志 viewpoints[].text 同一份），供前端渲染三席
    观点；HTTP 响应里的 viewpoints_meta 仍按既有口径剔除 text
    （message_runner 交付返回体处，零改动）。

    [思考流支持层 2026-10-12 · 拆雷②] 改走 ``gw.stream_call``（**不传
    max_tokens**——拆闸铁律，测试请求也是请求）：
    - stream_call 天然不做完成标记检测＝等价旧的 ``skip_completion_marker=True``；
    - ``chunk_type=reasoning`` 增量原样转发 /tokens（字段类思考）；
    - ``chunk_type=content`` 增量只过标签切分器取思考（内联类思考），**正文
      一律不上 /tokens**（三席观点不是助手回复正文，绝不串进对话流）；
    - 终帧（含 content/tool_calls/latency_ms）与旧 call 同契约，meta 组装不动。
    """
    models_cfg = gw.config.get("models") or {}
    # name 已是 bindings 解析后的模型名（run_viewpoints 经
    # model_config.resolve("viewpoint") 得到），键集已校验，直接作 seat 调用。
    seat = name
    label = (models_cfg.get(seat) or {}).get("model", seat)
    meta: dict[str, Any] = {"model": label, "ok": False, "latency_ms": 0, "chars": 0}
    if seat not in models_cfg:
        meta["error"] = f"模型配置无此模型条目: {seat}"
        return meta
    if on_seat:
        try:
            on_seat("seat_start", {"model": label})
        except Exception:
            pass
    t0 = time.monotonic()
    try:
        result: dict[str, Any] = {}
        splitter = ThinkStreamSplitter()
        saw_think = False
        # [直接停止 2026-10-15] 三席生成循环同查人停置位（软断直停）
        from . import control as _stmt_ctl
        from . import event_stream as _stmt_es
        _stmt_ctx = _stmt_es.current_stream_context()
        async for ev in gw.stream_call(
            seat,
            system_prompt,
            user_prompt,
            temperature=float(models_cfg[seat].get("temperature", 0.7)),
            tools=None,
            prior_messages=prior_messages or None,
            turn_tail=turn_tail or None,   # [三席身份注入 2026-10-14] 缺省 None ⇒ 逐字同旧
        ):
            if _stmt_ctx and _stmt_ctl.pending_stop(_stmt_ctx[0], _stmt_ctx[1]):
                break
            if "chunk_type" in ev:
                text = ev.get("text") or ""
                if not text:
                    continue
                if ev.get("chunk_type") == "reasoning":
                    saw_think = True
                    _emit_think(text, label)
                elif ev.get("chunk_type") == "content":
                    think_part, _body = splitter.feed(text)
                    if think_part:
                        saw_think = True
                        _emit_think(think_part, label)
            elif "content" in ev:
                result = ev
        tail_think, _tail_body = splitter.flush()
        if tail_think:
            saw_think = True
            _emit_think(tail_think, label)
        if not result:
            result = {"content": "", "latency_ms": int((time.monotonic() - t0) * 1000)}
        meta["latency_ms"] = int(result.get("latency_ms") or (time.monotonic() - t0) * 1000)
        text = clean_viewpoint_text(result.get("content") or "")
        if not text:
            meta["error"] = ("观点为空（正文缺失；本席思考已走 think 流展示）" if saw_think
                             else "观点为空（内容缺失或全为思考/协议块）")
            return meta
        meta["ok"] = True
        meta["chars"] = len(text)
        meta["text"] = text
        if on_done:
            try:
                on_done(meta)
            except Exception:
                pass
        if on_seat:
            try:
                on_seat("seat_end", {"model": label, "ok": True,
                                     "latency_ms": meta["latency_ms"],
                                     "chars": meta["chars"],
                                     # [模型过程数据 2026-10-12] 观点正文全文随
                                     # 事件带出（此前刻意剔除，现补回）：与 prompt
                                     # 日志 viewpoints[].text 同一份清洗后正文。
                                     "text": text})
            except Exception:
                pass
        return meta
    except Exception as exc:
        meta["latency_ms"] = int((time.monotonic() - t0) * 1000)
        meta["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        if on_done:
            try:
                on_done(meta)
            except Exception:
                pass
        if on_seat:
            try:
                on_seat("seat_end", {"model": label, "ok": False,
                                     "latency_ms": meta["latency_ms"],
                                     "error": meta.get("error", "")[:160]})
            except Exception:
                pass
        return meta
