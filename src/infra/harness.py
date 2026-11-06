# -*- coding: utf-8 -*-
"""harness 投递层：策略命中后，把提醒送到目标卡。

[基础设施 2026-10-26 · 用户裁定「先做投递层吧」]
[2026-10-31 · 两代并轨 · 用户令「整合进一代，以二代为准，二代缺陷的就补足」]
  第一代「规则表 + 逐条现场查」路径（``harness_rules`` / ``fire`` /
  ``harness_fired`` 去重表）退役——条件集是**唯一**引擎：
  判断归 ``policy_set``（值班员+小本子），求值归 ``harness_cond``，
  接线归 ``policy_runner.feed``，**本模块只做投递**：解析目标、渲染
  消息、按手段送达。

## 投递手段（``then.mode``）

- ``message``（默认）：落一条**永久会话消息**；``wake=true`` 时同时唤醒一轮
  （多数系统提醒只想让对方知道 ⇒ 策略里默认 ``wake=False``）；
- ``inject``：把 ``then.message`` 塞进目标卡**下一轮**的提示词区
  （``cards.pending_inject``，一次性、仅提示词，不进历史）；
- ``continue``：唤醒**评审卡**走 ``review_runner.run_once``（一醒=一步）。
  [2026-10-29 · 用户裁定「暂时只对评审卡开放」] 非评审卡直接跳过；
  ``then.message`` 通过 ``run_once(hint=...)`` 进这一步的用户提示词——
  评审回合不读 ``pending_inject``，只能显式传参。

## 目标解析（``then.target``）

``self``/``parent``/``ancestors``/``links``/``card:<id>``/``event:<路径>``
——``event:`` 从触发事实里取卡号（``ctx.trigger_data``，如 ``event:target``）。

## 占位符（``then.message``）

``{card_id}``/``{card_title}``/``{card_type}``/``{parent}``/``{trigger_type}``
与 ``{event.<路径>}``（取触发事实的字段，如 ``{event.batch}``）。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from .harness_cond import get_path

logger = logging.getLogger("infra.harness")

#: harness 发出的系统提醒在会话里的发件人（与用户口径一致：普通卡间通信的同一条路）
SYSTEM_SENDER = "system"

#: 投递留痕的事件类型
EV_TYPE = "harness_message"

#: ``{event.<路径>}`` 占位符
_EVENT_PH_RE = re.compile(r"\{event\.([A-Za-z_][\w.]*)\}")


# ---------------------------------------------------------------- 目标
def resolve_targets(target: str, ctx: dict[str, Any],
                    allow_self: bool = False) -> list[str]:
    """解析目标卡号列表（去重、排除自身）。

    ``event:<路径>`` 从 ``ctx["trigger_data"]``（触发事实）取字段值——
    取不到 / 不是卡号 ⇒ 解析为空（宁可不发，不误发）。

    ``allow_self``：deploy 的场（venue）可以就是触发卡本身——QA 就在
    报完工的那张卡上跑验收；"不发给自己"是给送话路径的规矩。
    """
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
    elif target.startswith("event:"):
        # 从触发事实取卡号：`event:target` ⇒ ctx.trigger_data["target"]
        path = target.split(":", 1)[1].strip()
        v = get_path(ctx.get("trigger_data") or {}, path) if path else None
        out = [str(v)] if isinstance(v, str) and v.strip() else []
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
        if t == me and target != "self" and not allow_self:
            continue
        seen.add(t)
        final.append(t)
    return final


# ---------------------------------------------------------------- 渲染
def render_message(rule: dict[str, Any], ctx: dict[str, Any]) -> str:
    """渲染消息模板。

    先替 ``{event.<路径>}``（触发事实字段），再走 ``.format`` 的普通占位符——
    顺序不能反：``{event.x}`` 不是合法 format 字段，会被 format 吃掉成异常。
    """
    tpl = str(rule.get("message") or "")
    trigger = ctx.get("trigger_data") or {}

    def _ev_sub(m: "re.Match[str]") -> str:
        v = get_path(trigger, m.group(1))
        return "" if v is None else str(v)

    if isinstance(trigger, dict):
        tpl = _EVENT_PH_RE.sub(_ev_sub, tpl)

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


# ---------------------------------------------------------------- 投递
async def deliver(work_id: str, target: str, text: str, *,
                  rule_id: str = "", wake: bool = True,
                  mode: str = "message", action_id: str = "") -> Optional[str]:
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
                                     rule_id=rule_id, wake=wake,
                                     action_id=action_id)
    if mode == "continue":
        # [2026-10-29 用户裁定] 固定能力，与 message / inject 并列：
        # 按卡停下来的状态原样再调起一次，塞不同的提示词。
        return await _deliver_continue(work_id, target, text, rule_id=rule_id,
                                       action_id=action_id)
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
                               "mode": "message",
                               "action_id": str(action_id or ""),
                               "text": text})
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
                          rule_id: str = "", wake: bool = True,
                          action_id: str = "") -> Optional[str]:
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
                               "queue_len": n,
                               "action_id": str(action_id or ""),
                               "text": text})
        except Exception:  # noqa: BLE001
            pass
        return f"inject:{n}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[harness] 注入失败 work=%s to=%s rule=%s: %s",
                       work_id, target, rule_id, exc)
        return None


async def _deliver_continue(work_id: str, target: str, text: str, *,
                            rule_id: str = "",
                            action_id: str = "") -> Optional[str]:
    """续接：**原状态再调起一次**，并塞一句说明为什么叫它。

    [2026-10-29 用户口径]「停下来时的最后一次调用是什么状态，就原状态再次调起，
    并根据情况不同，塞不同的提示词」——两种情况：①没有通过/不通过 ②全部通过了
    但没有写正稿。

    与 ``inject`` 的唯一差别：inject 只排队等下一次自然跑，continue **立刻叫醒**。
    与 ``message`` 的差别：message 把话写进**会话历史**（永久），continue 只给
    **这一轮**（一次性提示，不落历史）。

    [2026-10-31 · 两代并轨] ``text`` 改走 ``run_once(hint=...)`` 显式传参——
    评审回合的用户提示词是直组的（不走 context_assembler），
    ``pending_inject`` 塞进去也递不到席位手里。
    """
    from ..api import event_stream
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
        r = await review_runner.run_once(work_id, target, hint=text)
        # [2026-10-31] 到站原因补两个：**stopped**（用户点了停止）与
        # **review_busy**（评审卡被别的回合占着，本步让路）——都不是"这一步干成了"，
        # 不该报成 continue 成功（否则前端会显示"已续跑"，实际一步没走）。
        ok = bool(isinstance(r, dict) and r.get("reason") not in
                  ("no_batch", "already_running", "speak_failed",
                   "stopped", "review_busy"))
        try:
            event_stream.emit(work_id, target, EV_TYPE, {
                "rule_id": rule_id, "to": str(target), "wake": ok,
                "mode": "continue", "started": ok,
                "reason": str((r or {}).get("reason") or ""),
                "action_id": str(action_id or ""),
                "text": text,
            })
        except Exception:  # noqa: BLE001
            pass
        return f"continue:{int(ok)}"
    except Exception as exc:  # noqa: BLE001 投递失败不影响触发它的主流程
        logger.warning("[harness] 续接失败 work=%s to=%s rule=%s: %s",
                       work_id, target, rule_id, exc)
        return None
