# -*- coding: utf-8 -*-
"""系统消息投递层（harness 投递）：规则命中后，把提醒沿节点网络送到目标卡。

[基础设施 2026-10-26 · 用户裁定「那就做投递层吧」]

## 它做什么

输入＝一条触发事件；输出＝若干条**系统提醒**，落在目标卡的收件箱与消息流里。

```
fire(work_id, ev_type, card_id, data)
  ├ 取候选规则（harness_rules.rules_for）
  ├ 逐条求值 guard（只看状态，不用墙钟）
  ├ 解析目标（self / parent / ancestors / links / card:<id>）
  ├ 渲染模板（{card_id} {card_title} {card_type} {parent} {trigger_type}）
  ├ 防重（同规则 × 同卡 × 同触发事件只发一次，落 harness_fired）
  └ 投递：收件箱 + 消息块 + 事件流 + 后台唤醒
```

## 为什么消息要"走既有通道"

用户口径：「消息也是通过节点网络连线通信，是智能网里的其中一个节点，而不是完全
自造一套体系」。故投递不做第二套：落信走 ``card_db.append_inbox``、消息块走
``session_facade.emit_message_event``、唤醒走 ``message_runner.deliver_now``——
与卡间通信同一条路，消息天然挂在两卡的连线上、可被 ``read_inbox`` 与事件流追溯。

## 零阻断

投递是旁路能力：任何一步失败只记 warning，绝不影响触发它的主流程。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional

from . import harness_rules

logger = logging.getLogger("infra.harness")

#: 后台评估任务的强引用（防 GC 中途回收，同 message_runner._spawn_tracked 手法）
_PENDING: set[Any] = set()

#: 系统消息的发送者标识（不是真实卡；消息头 from 会显示它）
SYSTEM_SENDER = "system"

#: 消息块/事件类型
EV_TYPE = "harness_message"

#: 判据求值时回看的事件条数上限（够覆盖一轮；不追求全历史）
_LOOKBACK = 200

_FIRED_DDL = """CREATE TABLE IF NOT EXISTS harness_fired (
    work_id     TEXT NOT NULL,
    rule_id     TEXT NOT NULL,
    card_id     TEXT NOT NULL,
    trigger_seq INTEGER NOT NULL,
    ts          TEXT DEFAULT '',
    PRIMARY KEY (work_id, rule_id, card_id, trigger_seq)
)"""


# ---------------------------------------------------------------- 上下文
async def build_ctx(work_id: str, card_id: str, trigger_type: str,
                    trigger_data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """收集判据需要的事实（卡 meta + 正文三块 + 最近事件）。失败字段留空。"""
    from ..storage import card_store

    ctx: dict[str, Any] = {
        "work_id": work_id, "card_id": card_id,
        "card_type": "", "card_title": "", "parent": "", "state": "",
        "mission": "", "status_block": "", "requirements": "",
        "trigger_type": trigger_type, "trigger_data": trigger_data or {},
        "events": [],
    }
    try:
        cur = await card_store.read_card(work_id, card_id)
        meta = cur.get("meta") or {}
        body = cur.get("body") or {}
    except Exception as exc:  # noqa: BLE001 读不到卡 ⇒ 判据按空值走（宁可少发）
        logger.warning("[harness] 读卡失败 card=%s: %s", card_id, exc)
        meta, body = {}, {}
    ctx["card_type"] = str(meta.get("card_type") or "")
    ctx["card_title"] = str(meta.get("title") or "")
    ctx["parent"] = str(meta.get("parent") or "")
    ctx["state"] = str(meta.get("state") or "")
    ctx["mission"] = str(body.get("mission") or "")
    ctx["status_block"] = str(body.get("status_block") or "")
    ctx["requirements"] = str(body.get("requirements") or "")
    try:
        # [判据只认事件 2026-10-26 · 用户令「明明做了事件机制，为什么还拿卡的状态
        # 来定义」] 事件源必须是**实时的 jsonl 时序**，不是汇总表 work_events——
        # 汇总表是缓冲批量落（阈值 40 条 / 1.5s），触发事件自己很可能还在缓冲里，
        # 读汇总表会漏掉"刚刚发生的这一条"。timeline(tail=N) 走文件尾字节扫描，
        # 几十毫秒，且是后台任务，不堵主链。返回旧→新，这里转成新→旧。
        from ..storage import event_log
        recs = event_log.timeline(work_id, card_id, tail=_LOOKBACK) or []
        ctx["events"] = list(reversed(recs))
    except Exception as exc:  # noqa: BLE001 事件读不到 ⇒ no_event/has_event 判不了
        logger.warning("[harness] 读事件失败 card=%s: %s", card_id, exc)
        ctx["events"] = []
    return ctx


def _is_blank(text: str) -> bool:
    """块是否"空"：空串或只有模板占位（如「（本卡全部需求的确定版…）」这种说明行）。"""
    t = (text or "").strip()
    if not t:
        return True
    # 只有说明性占位（无实质内容）：全是括号说明、或长度极短且无换行
    if t.startswith("（") and t.endswith("）") and "\n" not in t:
        return True
    return False


# ---------------------------------------------------------------- 判据
def evaluate_guard(guard: Any, ctx: dict[str, Any]) -> bool:
    """全部判据满足才 True（空/None＝恒真）。

    [harness 第 2 步 2026-10-28] **换成通用求值器**（`infra/harness_cond.py`）。

    改前只有两种判据（`no_event` / `has_event`），而且只支持"某类事件在本轮/
    本卡有没有发生过、某个**顶层**字段**等于**某个值"——问不了嵌套、数值、
    次数、先后。用户口径：「条件的组合要更灵活机动，不能有太多限制，这样才能
    组合出丰富且智能的控制机制。」

    现在三种输入都接受（语义向后兼容）：
      · ``None`` / ``[]``                ⇒ 恒真
      · list（旧写法，或混写）            ⇒ 逐项 AND
      · dict（条件树：all/any/not/event/count/field）

    认不出的形状一律 **False**（宁可少发，不误发）——这条纪律不变。
    """
    from . import harness_cond
    try:
        return harness_cond.eval_cond(harness_cond.normalize_guard(guard), ctx)
    except Exception as exc:  # noqa: BLE001 求值异常⇒不成立（宁可少发）
        logger.warning("[harness] 条件求值异常 ⇒ 不触发: %s", exc)
        return False


def _eq(a: Any, b: Any) -> bool:
    """宽松相等（True/true/"true" 视为同值）。保留给外部旧引用，判据已走 harness_cond。"""
    if a == b:
        return True
    return str(a).strip().lower() == str(b).strip().lower()


def _match_data(data: Any, where: dict[str, Any],
                ctx: Optional[dict[str, Any]] = None) -> bool:
    """事件的 data 是否满足字段条件。**已改为委托**（[harness 第 2 步 2026-10-28]）。

    新实现支持点号路径、比较器、``$ctx`` 取值，见 `infra/harness_cond.match_where`。
    保留本函数只为不打断既有外部引用。
    """
    from . import harness_cond
    return harness_cond.match_where(data, where, ctx or {})


def _has_event(ctx: dict[str, Any], guard: dict[str, Any]) -> bool:
    """在 scope 范围内有没有 types 里的事件。**已改为委托**（[harness 第 2 步]）。

    旧的 scope=turn 窗口算法（跳过触发事件自身、跳过本轮自己的 turn_end、
    遇到第二个 turn_end 即上一轮边界）已**逐字搬进** `harness_cond._turn_window`，
    语义不变。保留本函数只为不打断既有外部引用。
    """
    from . import harness_cond
    spec: dict[str, Any] = {"types": list(guard.get("types") or []),
                            "within": str(guard.get("scope") or "turn")}
    if guard.get("where"):
        spec["where"] = dict(guard.get("where") or {})
    return bool(harness_cond.select_events(spec, ctx))


def _child_count(ctx: dict[str, Any]) -> int:
    """已废弃：判据只允许事件。保留函数体仅为兼容旧引用，不再被 evaluate_guard 调用。"""
    return 0


def _cmp(a: int, op: str, b: int) -> bool:
    return {"<": a < b, "<=": a <= b, "==": a == b,
            ">=": a >= b, ">": a > b}.get(op, False)


# ---------------------------------------------------------------- 目标
def resolve_targets(target: str, ctx: dict[str, Any]) -> list[str]:
    """解析目标卡号列表（去重、排除自身）。"""
    from ..storage import card_store

    wid = ctx["work_id"]
    me = ctx["card_id"]
    out: list[str] = []

    if target == "self":
        out = [me]
    elif target == "parent":
        p = ctx.get("parent") or ""
        out = [p] if p else []
    elif target == "ancestors":
        cur = ctx.get("parent") or ""
        metas = card_store.iter_card_metas(wid)
        guard_n = 0
        while cur and guard_n < 50:
            out.append(cur)
            cur = str((metas.get(cur) or {}).get("parent") or "")
            guard_n += 1
    elif target == "links":
        metas = card_store.iter_card_metas(wid)
        me_meta = metas.get(me) or {}
        out = [str(l.get("target")) for l in (me_meta.get("links") or [])
               if l.get("target")]
    elif target.startswith("card:"):
        out = [target.split(":", 1)[1].strip()]
    else:
        logger.warning("[harness] 未知 target=%r", target)
        return []

    seen: set[str] = set()
    final: list[str] = []
    for t in out:
        t = str(t or "").strip()
        if not t or t in seen:
            continue
        # self 就是要发给自己；其余目标里出现自己才排除（父卡链/连线可能绕回自己）
        if t == me and target != "self":
            continue
        seen.add(t)
        final.append(t)
    return final


# ---------------------------------------------------------------- 渲染
def render_message(rule: dict[str, Any], ctx: dict[str, Any]) -> str:
    tpl = str(rule.get("message") or "")
    vals = {
        "card_id": ctx.get("card_id", ""),
        "card_title": ctx.get("card_title", ""),
        "card_type": ctx.get("card_type", ""),
        "parent": ctx.get("parent", ""),
        "trigger_type": ctx.get("trigger_type", ""),
    }
    try:
        return tpl.format(**vals)
    except Exception:  # noqa: BLE001 模板写错不许炸；原样返回并告警
        logger.warning("[harness] 消息模板占位符异常 rule=%s", rule.get("id"))
        return tpl


# ---------------------------------------------------------------- 防重
async def _claim(work_id: str, rule_id: str, card_id: str, trigger_seq: int) -> bool:
    """占位：同一 (规则, 卡, 触发事件) 只允许投递一次。"""
    from ..storage import card_db

    def _fn(conn: Any) -> bool:
        conn.execute(_FIRED_DDL)
        cur = conn.execute(
            "INSERT OR IGNORE INTO harness_fired"
            " (work_id, rule_id, card_id, trigger_seq, ts)"
            " VALUES (?,?,?,?,datetime('now'))",
            (work_id, rule_id, card_id, int(trigger_seq)))
        return bool(cur.rowcount)

    try:
        return bool(await card_db.run_write(work_id, _fn))
    except Exception as exc:  # noqa: BLE001 判不了就当首次（宁可多发一次，不吞提醒）
        logger.warning("[harness] 防重判定失败 rule=%s: %s", rule_id, exc)
        return True


async def _deliver_continue(work_id: str, target: str, text: str, *,
                             rule_id: str = "") -> Optional[str]:
    """续接：**原状态再调起一次**，并塞一句说明为什么叫它。

    [2026-10-29 用户口径]「停下来时的最后一次调用是什么状态，就原状态再次调起，
    并根据情况不同，塞不同的提示词」——两种情况：①没有通过/不通过 ②全部通过了
    但没有写正稿。

    与 ``inject`` 的唯一差别：inject 只排队等下一次自然跑，continue **立刻叫醒**。
    与 ``message`` 的差别：message 把话写进**会话历史**（永久），continue 只给
    **这一轮**（一次性提示，不落历史）。
    """
    from ..api import event_stream, message_runner
    from ..storage import card_store

    # [2026-10-29 用户令]「continue 和 message 本质上很像，所以 continue 你需要标注，
    # 暂时只对评审卡开放」——不是评审卡就直接不续接，等语义定下来再放开。
    # 标注在代码里，不靠调用方自觉：规则写错了也不会误叫普通卡。
    try:
        _card = await card_store.read_card(work_id, target)
        _ctype = str((_card.get("meta") or {}).get("card_type") or "")
    except Exception as exc:  # noqa: BLE001 读不到卡就不续接
        logger.warning("[harness] continue 读卡失败 work=%s card=%s: %s",
                       work_id, target, exc)
        return None
    if _ctype != "review":
        logger.info("[harness] continue 暂时只对评审卡开放，跳过 card=%s type=%s",
                    target, _ctype or "(空)")
        return None

    try:
        # [2026-10-30 实弹修正]「原状态再调起」对评审卡＝**接着跑评审**，不是起一轮对话。
        # 改前走 `deliver_now` ⇒ 评审卡被叫醒后跑的是普通对话（实弹 card_123 事件流：
        # context→round_start→…→turn_end），评审一步都没推进，批次就此停住。
        from ..api import review_runner
        r = await review_runner.run_once(work_id, target)
        ok = bool(isinstance(r, dict) and r.get("reason") not in
                  ("no_batch", "already_running", "speak_failed"))
        try:
            event_stream.emit(work_id, target, EV_TYPE, {
                "rule_id": rule_id, "to": str(target), "wake": ok,
                "mode": "continue", "started": ok,
                "reason": str((r or {}).get("reason") or ""),
                "text": text[:300],
            })
        except Exception:  # noqa: BLE001
            pass
        return f"continue:{int(ok)}"
    except Exception as exc:  # noqa: BLE001 投递失败不影响触发它的主流程
        logger.warning("[harness] 续接失败 work=%s to=%s rule=%s: %s",
                       work_id, target, rule_id, exc)
        return None


# ---------------------------------------------------------------- 投递
async def deliver(work_id: str, target: str, text: str, *,
                  rule_id: str = "", wake: bool = True,
                  mode: str = "message") -> Optional[str]:
    """把一条系统提醒送到目标卡。

    [harness 三手段 2026-10-28 · 用户定义] `mode` 决定用哪种手段：
      · ``message``（默认）—— 写进**会话历史**：永久，之后每一轮上下文都带着它
      · ``inject``        —— 只拼进**下一轮**上下文：不进历史、不留痕，用完即清
      · ``continue``      —— **原状态再调起一次**：不落历史，只给这一轮一句话，
                             然后立刻起轮（停下来是什么状态，就接着那个状态跑）

    ``wake=True`` 两种模式都适用：message 模式是"提醒并叫醒"，inject 模式是
    "把这句塞进下一轮，并立刻让它开始那一轮"（否则要等它自然跑时才生效）。
    与卡间通信同一条路（用户口径：不自造第二套体系）。
    """
    if mode == "inject":
        return await _deliver_inject(work_id, target, text,
                                     rule_id=rule_id, wake=wake)
    if mode == "continue":
        # [2026-10-29 用户裁定] 固定能力，与 message / inject 并列：
        # 按卡停下来的状态原样再调起一次，塞不同的提示词。
        return await _deliver_continue(work_id, target, text, rule_id=rule_id)
    try:
        from ..api import event_stream, message_runner
        from ..storage import session_facade

        # [2026-10-26 · 用户裁定「同步/通知类消息不该用 inbox，也不该只在当轮可见」]
        # 落成**会话块**：永久在历史里，下次任何回合的上下文都带着它。
        # 与 sync 同一条路（session_facade.append_card_message），不造第二套。
        rec = await session_facade.append_card_message(
            work_id, target, from_card=SYSTEM_SENDER, text=text,
            msg_type="msg_notify", event_type=EV_TYPE)
        # seq=0 是合法值（会话第一块），不能用 `or ""` 吞掉
        _seq = (rec or {}).get("seq")
        msg_id = str(_seq) if _seq is not None else ""
        # 事件流留痕（这条提醒本身也是网里发生的事）
        # [harness 第 1 步 2026-10-28] 补 `to` / `wake` 两个零件：
        # 只记 rule_id 无法回答"提醒发给了谁""有没有把人叫起来"——而"提醒过但
        # 没唤醒"和"提醒并唤醒"是两种完全不同的干预强度，条件面必须分得开。
        try:
            event_stream.emit(work_id, target, EV_TYPE,
                              {"rule_id": rule_id, "to": str(target),
                               "wake": bool(wake),
                               "seq": msg_id,
                               "text": text[:300]})
        except Exception:  # noqa: BLE001 展示层零阻断
            pass
        if wake:
            # [2026-10-26] 消息已落会话块 ⇒ 唤醒走 text 模式（不取件，只起轮）
            message_runner._spawn_tracked(message_runner.deliver_now(
                work_id, target,
                text="系统给你发来一条提醒，正文已在上文。请按提醒处理。",
                source={"kind": "system", "rule": rule_id}))
        return msg_id or None
    except Exception as exc:  # noqa: BLE001 投递失败不阻断触发方
        logger.warning("[harness] 投递失败 work=%s to=%s rule=%s: %s",
                       work_id, target, rule_id, exc)
        return None


async def _deliver_inject(work_id: str, target: str, text: str, *,
                          rule_id: str = "", wake: bool = True) -> Optional[str]:
    """注入模式：**只排队、不唤醒**。

    [2026-10-28 用户口径]「不是那次调用，是那一整轮都留着，而且不会唤醒，
    只有在下一轮行为的时候，加一次调用」——
      · 排队，等目标卡**下一次跑**时生效（不主动起轮，wake 参数在这里无效）
      · 生效方式：装配进那一轮的 system，**整轮共用**（一轮里多次调用都看得到）
      · 那一轮结束即清（不进会话历史，所以之后的轮次看不到）
    """
    from ..storage import card_store

    try:
        from ..api import event_stream
        n = await card_store.push_pending_inject(work_id, target, text)
        try:
            event_stream.emit(work_id, target, EV_TYPE,
                              {"rule_id": rule_id, "to": str(target),
                               "wake": False, "mode": "inject",
                               "queue_len": n, "text": text[:300]})
        except Exception:  # noqa: BLE001
            pass
        return f"inject:{n}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[harness] 注入失败 work=%s to=%s rule=%s: %s",
                       work_id, target, rule_id, exc)
        return None


# ---------------------------------------------------------------- 主入口
async def fire(work_id: str, ev_type: str, card_id: str,
               data: Optional[dict[str, Any]] = None,
               *, trigger_seq: int = 0) -> list[dict[str, Any]]:
    """一条事件发生后评估所有候选规则并投递。返回投递记录（诊断/测试用）。

    ``trigger_seq`` 用于防重（同一条事件重复评估不会重复发）。
    """
    out: list[dict[str, Any]] = []
    rules = harness_rules.rules_for(ev_type)
    if not rules:
        return out

    ctx: Optional[dict[str, Any]] = None
    for rule in rules:
        rid = str(rule.get("id") or "")
        if ctx is None:
            ctx = await build_ctx(work_id, card_id, ev_type, data)
        # 卡类型过滤：**取事件自带字段**，不读卡（事件没带 ⇒ 不在白名单 ⇒ 不发）
        cts = (rule.get("trigger") or {}).get("card_types") or []
        if cts:
            _ev_ct = str((data or {}).get("card_type") or "")
            if _ev_ct not in cts:
                continue

        if not evaluate_guard(rule.get("guard") or [], ctx):
            continue

        targets = resolve_targets(str(rule.get("target") or ""), ctx)
        if not targets:
            continue
        text = render_message(rule, ctx)
        for t in targets:
            if not await _claim(work_id, rid, t, trigger_seq):
                continue
            # [2026-10-29] 规则可以指定投递手段：message（默认）/ inject / continue
            msg_id = await deliver(work_id, t, text, rule_id=rid,
                                   mode=str(rule.get("mode") or "message"))
            if msg_id:
                out.append({"rule_id": rid, "to": t, "msg_id": msg_id})
    return out


def interested(ev_type: str) -> bool:
    """有没有规则监听这个事件类型。

    接线用：事件是高频通道（tool_call / reasoning 每秒数十条），没有规则
    监听时不该白起一次评估任务。规则表改了就即时生效（现读）。
    """
    try:
        return bool(harness_rules.rules_for(str(ev_type)))
    except Exception:  # noqa: BLE001 判不出 ⇒ 当作不关心（不白跑）
        return False


def fire_async(work_id: str, ev_type: str, card_id: str,
               data: Optional[dict[str, Any]] = None,
               *, trigger_seq: int = 0) -> None:
    """**后台**评估（不阻塞事件落盘）。异常只记日志，绝不影响主流程。

    接线点：``event_log.append`` 落盘成功后调用（此时 rec 已有 seq，
    可作防重键）。只有 ``interested(ev_type)`` 为真才会被调用。
    """
    if not card_id:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return          # 无事件循环（同步写路径）⇒ 不评估，等下次异步落盘

    async def _run() -> None:
        try:
            await fire(work_id, ev_type, card_id, data, trigger_seq=trigger_seq)
        except Exception as exc:  # noqa: BLE001 评估失败不阻断
            logger.warning("[harness] 评估失败 work=%s type=%s card=%s: %s",
                           work_id, ev_type, card_id, exc)

    t = loop.create_task(_run())
    _PENDING.add(t)
    t.add_done_callback(_PENDING.discard)


async def drain() -> int:
    """等待在途评估任务（测试确定性用）。"""
    tasks = list(_PENDING)
    for t in tasks:
        try:
            await t
        except Exception:  # noqa: BLE001
            pass
    return len(tasks)
