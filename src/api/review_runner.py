# -*- coding: utf-8 -*-
"""评审回合执行器（[伴生评审卡 2026-10-27]）。

**一次评审发言 ＝ 用「职能卡的模型 ＋ 职能的身份提示词」，在「评审卡的上下文」里跑一轮。**

上下文边界（用户 2026-10-27 硬要求）：
- **进**：子卡三块（使命／现状／需求文档，原样）＋ 发起方填写的 brief（原样）
  ＋ 上一批次正稿 ＋ 本批次已有发言（含用户自己说的每一句）
- **不进**：职能卡自己的会话——**评审内容不污染职能卡的日常上下文**

不另造投递/存储（第 1 步「融合机制」）：
- 发言 → `session_facade.append` 落**评审卡**会话
- 工具 → `card_tools.execute_tool` / handler
- 模型 → `model_service.call_main`，模型名取**职能卡上配置的模型**
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
import os
import re
from pathlib import Path
from typing import Any

from ..storage import card_store, function_store, review_store, session_facade
from . import control, event_stream, model_service, prompt_log

#: 评审职责书（每个角色**在评审席上**的职责；与日常职责 function_profiles.charter 分开）
REVIEW_CHARTERS = Path(__file__).resolve().parents[2] / "config" / "review_charters.json"

logger = logging.getLogger("api.review_runner")

#: 测试环境开关（照 ECO_NO_AUTO_DELIVER 先例）：置 1 则不起评审回合
ENV_NO_AUTO = "ECO_NO_AUTO_REVIEW"

#: 评审卡会话里**本批次**的发言全部带进上下文（P6：不截断、不压缩）。
#: 上一批次的发言不带——它们只以"上一批次正稿"的形式进（换上下文，不是压缩）。
#: 靠发言首行的 `· {batch}` 标记区分批次，不靠条数截断。


#: 评审现场可用的查阅工具（用户 2026-10-27：每个角色都能调查阅工具，结果留在现场）
READ_TOOLS: tuple[str, ...] = (
    "read_card", "read_file", "list_files", "search_files", "read_shadow")


# ---------------------------------------------------------------- 事件：工具往返
# [评审窗口 2026-10-29 · 用户令「我要看的是详细事件的流式输出，所有事件，调起模型，
#  调工具，工具返回，开始发言」]
# 评审回合里的工具调用**不走 message_runner**（那里才是 tool_call/tool_result 的常规
# 产出点），所以窗口里看不到"它在调什么"。这里补两个小事件，格式与 message_runner
# 同族（tool / ok / 摘要），前端一套渲染吃两边。
def _sid(review_id: str, batch: str) -> str:
    """评审会话的卡号：**一个批次一个独立会话**。

    [2026-10-31 用户令]「每一个批次都是独立的 session 就好了，新批次就是新 session，
    那就不需要那么多判断逻辑了」⇒ 会话文件名按 (评审卡, 批次) 分开，
    旧批次的东西物理上进不来 ⇒ 不再需要"批次守卫"那种判断。
    """
    return f"{review_id}-{batch}" if batch else review_id


def _brief(value: Any) -> str:
    """工具调用的**参数摘要**：只挑几个定位用的键，不砍长度。

    [2026-10-29 用户令「我最恨数字闸，墙钟」] 原来这里带 `limit=80` 硬砍，
    砍多少是我拍的。现在改成**选什么**（挑定位键）而不是**砍多少**——
    长度交给前端那一行的 CSS 省略号，那是展示层的事。
    """
    # [2026-10-29 修] 原来按白名单挑键，`reason` / `block` / `before` 这些全被过滤掉
    # ⇒ 事件里看不到 reject 的理由、看不到改了哪一块，等于没留痕。
    # 现在原样序列化：**参数全给**，长度交给前端那一行的 CSS 省略号。
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return " ".join(text.split())


#: 子卡改稿时**不给**的工具：建子卡／汇报／通信／停卡这类"对别人动手"的动作。
#: [2026-10-30 用户裁定]「子卡则和在正式对话里一样，拥有几乎所有工具
#: （除了建子卡、汇报、通信这些）」——按名字列，其余一律给。
REVISE_DENY: tuple[str, ...] = (
    "create_card", "reparent_card", "archive_card", "unarchive_card",
    "report_up", "report_to_user", "sync", "direct",
    "stop_card", "silence_card", "continue_task",
    "open_review", "approve", "reject",
    "revoke_approve", "decide", "promote_draft",
)


#: 改稿这一轮的**终态工具**：调了才算这一轮结束。
#: [2026-10-30 用户令]「你为什么不效仿通过/不通过？用工具，让模型自己提交说已经改完了」
#: ——出口不能是"它不再调工具"，那既可能是改完了也可能是它歇了一下；
#: 必须是它自己交回来，和 approve/reject 同一种做法。
_SUBMIT_REVISION: dict[str, Any] = {
    "name": "submit_revision",
    "description": (
        "**提交本轮改稿**：这一轮你认为该改的都改完了，就调它交回评审。\n"
        "调了才算这一轮结束；只回一段文字不算。\n"
        "下一轮要不要再改，由评审重新评了再说。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {"type": "string",
                        "description": "改了哪几处（一条一行，写能核对的事实）"},
        },
        "required": ["summary"],
    },
}


def _revise_tools(work_id: str, target: str,
                  cards: dict[str, dict[str, Any]]) -> list[str]:
    """子卡改稿的工具面＝**正式对话的工具面**（减建子卡／汇报／通信）。

    「和在正式对话里一样」意味着不另造一套窄工具面：子卡平时能用的，
    改稿时也能用（它能自己查证据、自己改现状块、自己改草稿）。
    """
    from . import card_tools
    try:
        schemas = card_tools.build_tool_schemas(target, work_id, cards)
    except Exception as exc:  # noqa: BLE001 取不到就回落到最小面
        logger.warning("[review] 取改稿工具面失败 %s: %s", target, exc)
        # [2026-10-30 删] update_status 已删（整块覆盖退役、且它没有开关）。
        return ["edit_requirements"] + list(READ_TOOLS)
    out = [str(x.get("name") or "") for x in schemas]
    return [n for n in out if n and n not in REVISE_DENY]


def _prepare_review_actor(work_id: str, role_id: str,
                          cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """将受邀角色作为当前评审身份装入工具判定上下文。

    函数/职能身份来自职能表，不保证有普通卡行。无卡行时以 function 卡型和该类
    能力挂载构造本轮临时 meta；不写入数据库、不改职能资料。真实职能卡已有挂载则沿用。
    ``_review_seat`` 标记只声明这是评审场景，用于读范围判定。
    """
    from ..permissions import policy
    from ..storage import work_store

    actor = dict(cards.get(role_id) or {})
    is_function = False
    try:
        is_function = function_store.function_exists(work_id, role_id)
    except Exception:  # noqa: BLE001 身份不能确认时不合成职能卡
        pass

    if is_function and str(actor.get("card_type") or "") != "function":
        root_id = work_store.work_card_id_of(work_id) or ""
        root = cards.get(root_id) or {}
        project_class = str(root.get("project_class") or "")
        try:
            perms, _perm_ver = policy.compute_perms(project_class or "rd_project", "function")
            from ..infra import capability_registry

            abilities = capability_registry.abilities_for(perms, "manual", "function")
        except Exception as exc:  # noqa: BLE001 无法算能力时明确空挂载，执行端关门
            logger.warning("[review] 职能能力挂载生成失败 role=%s: %s", role_id, exc)
            abilities, perms, _perm_ver = [], [], 0
        actor.update({
            "card_id": role_id,
            "card_type": "function",
            "project_class": project_class,
            "mode": "manual",
            "abilities": abilities,
        })

    actor["_review_seat"] = True
    cards[role_id] = actor
    return actor


def _review_read_tools(work_id: str, role_id: str,
                       cards: dict[str, dict[str, Any]]) -> list[str]:
    """评审场景的工具面＝**当前评审角色自己的能力工具面 ∩ 只读工具**。

    场景由调用点 ``speak`` 明确进入：正在进行的评审批次正在召集 ``role_id`` 发言。
    所以工具面以这个角色为当前卡调用 ``build_tool_schemas``，再由评审机制筛成只读；
    不从被评审子卡继承工具。成员能力不同，得到的只读工具也可以不同。

    二次核对能力清单中的工具归属，避免职能目录的附加工具越过该角色的能力挂载。
    读取工具以 ``ToolDef.readonly`` 标记判定，不按工具名硬编码。
    工具面生成失败时不给查询工具，不回退到共享固定名单。
    """
    from . import card_tools          # 延迟 import：card_tools 体量大且反向引用
    try:
        abilities = card_tools._abilities_of(cards.get(role_id), cards)
        if abilities is None:
            return []
        allowed_by_capability = {
            str(tool)
            for cap in card_tools._cap_capabilities()
            if str(cap.get("code") or "") in abilities
            for tool in ((cap.get("impl") or {}).get("tools") or [])
        }
        schemas = card_tools.build_tool_schemas(role_id, work_id, cards)
    except Exception as exc:  # noqa: BLE001 评审工具面不可判定 ⇒ 不给查询工具
        logger.warning("[review] 按角色生成只读工具面失败 role=%s: %s", role_id, exc)
        return []
    readonly = {name for name, definition in card_tools.TOOL_REGISTRY.items()
                if getattr(definition, "readonly", False)}
    visible = [str(schema.get("name") or "") for schema in schemas]
    return [name for name in visible
            if name and name in readonly and name in allowed_by_capability]


def _emit_tool_call(work_id: str, review_id: str, actor: str,
                    tool: str, args: Any) -> None:
    try:
        event_stream.emit(work_id, review_id, "tool_call", {
            "tool": tool, "by": actor, "arguments": _brief(args)})
    except Exception:  # noqa: BLE001 展示层零阻断
        pass


def _emit_tool_result(work_id: str, review_id: str, actor: str,
                      tool: str, res: Any) -> None:
    try:
        ok = bool(res.get("ok")) if isinstance(res, dict) else True
        event_stream.emit(work_id, review_id, "tool_result", {
            "tool": tool, "by": actor, "ok": ok,
            "summary": "" if ok else str(res.get("error") or "")})
    except Exception:  # noqa: BLE001 展示层零阻断
        pass


#: 正在跑的评审回合（同一张评审卡同时只跑一轮）。
#: 不是"防模型"，是**防同一版本被并发评两遍**——第二轮在收尾时会自检新版本，
#: 所以这里丢掉的触发不会丢工作（见 `_round_loop` 末尾的版本自检）。
_INFLIGHT: set[str] = set()


async def run_round(work_id: str, review_id: str) -> list[dict[str, Any]]:
    """跑一轮：**尚未通过**的成员依次以本职能身份发言一次。

    收尾自检：本轮进行中若子卡又改了卡面（草稿版本推进），按新版本再来一轮。
    退出条件是**状态**（没有批次／全过／版本没变），不设次数上限。
    """
    if os.environ.get(ENV_NO_AUTO) == "1":
        return []
    if review_id in _INFLIGHT:
        return []
    # [2026-10-31] 与 run_once 同一口径：占住评审卡回合锁，停止才停得下来、
    # 用户消息轮与评审轮才不会交错。抢不到 ⇒ 有人在跑，本次触发让路（不排队、
    # 不设墙钟）；丢掉的工作由 _round_loop 收尾时的版本自检兜住。
    _owner = f"review_round:{review_id}"
    if not session_facade.turn_try_acquire(work_id, review_id, _owner):
        return []
    _INFLIGHT.add(review_id)
    # [2026-10-31] 与 run_once 同口径登记 turn task：模型调用停在半路时，
    # 停止端点的 `cancel_turn` 才有目标可 cancel（否则只能等下一个圈边界消费停止位）。
    _task = asyncio.current_task()
    if _task is not None:
        control.bind_turn_task(work_id, review_id, _task)
    try:
        return await _round_loop(work_id, review_id)
    finally:
        _INFLIGHT.discard(review_id)
        control.unbind_turn_task(work_id, review_id, _task)
        control.discard_stop(work_id, review_id)
        session_facade.turn_release(work_id, review_id, _owner)


async def _is_promoted(work_id: str, target: str) -> bool:
    """这张卡的正稿写了没有（`requirements_state == "approved"`）。"""
    try:
        card = await card_store.read_card(work_id, target)
        return str((card.get("meta") or {}).get("requirements_state") or "") == "approved"
    except Exception:  # noqa: BLE001
        return False


_RUNNING: set[str] = set()


async def run_once(work_id: str, review_id: str) -> dict[str, Any]:
    """**跑一步**：叫一个未通过的成员发言；有人不通过就让子卡改稿；全过就写正稿。

    一步＝**发言一个角色 ＋（该改时才）改一次稿**，做完就返回，不在这里循环。
    流程存续交给 harness 的 `continue`：每步结束落 `review_round_done`，
    由策略决定要不要再叫下一步。

    [2026-10-30 实弹修正，两处]
    · **没人 reject 就不叫子卡改稿**：原来无条件改，于是「总裁通过 → 叫子卡改 →
      子卡说不用改 → changed=False → 停」，另外两个成员一次都没轮到
      （实弹 card_116 停在 pending=['fn_004','fn_005']，draft 15）。
    · **没全过就不算终态**：不再把「子卡没改稿」当成到站。只要还没有终态，
      这一步就没结束——继续叫。
    """
    # 与 `run_round` 同一个开关：测试环境不自动起轮（否则改一次卡就会真调模型）。
    if os.environ.get(ENV_NO_AUTO) == "1":
        return {"continued": False, "reason": "no_auto"}
    key = f"{work_id}/{review_id}"
    if key in _RUNNING:
        return {"continued": False, "reason": "already_running"}
    # [2026-10-31 修·用户实测「不能停止」] 评审轮必须**占住评审卡的回合锁**。
    # `control.request_stop` 的第一道判据是 `session_facade.turn_holder(...)`：
    # 拿不到 holder 就直接 return no_inflight_turn ⇒ 停止位根本不置、cancel 也不发
    # ⇒ 改前用户在评审卡上点停止是**彻底的 no-op**（只登记 task 也没用）。
    # 占锁顺带解决第二件事：同一张评审卡上「用户消息轮」与「评审步」不再并发
    # （实测 card_124 事件流 23:01:16 宣布 fn_004 发言、23:02:33 落账却是 card_001，
    #   就是两步交错的产物）。
    _owner = f"review_step:{review_id}"
    if not session_facade.turn_try_acquire(work_id, review_id, _owner):
        logger.info("[review] 评审卡 %s 上有别的回合在跑，本步让路", review_id)
        return {"continued": False, "reason": "review_busy"}
    _RUNNING.add(key)
    # [2026-10-31 修·用户实测「不能停止」] 登记回合 task ⇒ 停止端点
    # （control.halt → request_stop → cancel_turn）找得到目标、能真 cancel。
    # 改前评审轮由 _spawn_tracked(run_once(...)) 起，从不登记 ⇒ cancel 恒 no-op。
    _task = asyncio.current_task()
    if _task is not None:
        control.bind_turn_task(work_id, review_id, _task)
    try:
        return await _run_step(work_id, review_id)
    finally:
        _RUNNING.discard(key)
        control.unbind_turn_task(work_id, review_id, _task)
        control.discard_stop(work_id, review_id)
        session_facade.turn_release(work_id, review_id, _owner)


async def _run_step(work_id: str, review_id: str) -> dict[str, Any]:
    """`run_once` 的实体（不含并发闸）。

    一步只做一件事，顺序是**串行**的（[2026-10-30 用户口径]）：

        一轮（草稿第 N 版）
          ├ 叫 1 号发言（还没发言的）
          ├ 叫 2 号发言
          ├ 叫 3 号发言
          ├ 全员发完 → 汇总意见 → 子卡改一次稿
          └ 草稿进第 N+1 版 → 下一轮

    改前是「叫一个人 → 他一反对就改稿 → 叫下一个」，三个人三次改稿、互相踩，
    一轮要十几分钟。现在**一轮只改一次稿**。
    """
    st = await review_store.state_of(work_id, review_id)
    if not st.get("batch"):
        return {"continued": False, "reason": "no_batch"}
    batch = str(st.get("batch") or "")
    st_now = await review_store.status(work_id, review_id)

    # ① 全员通过 → 定稿
    if st_now.get("all_passed"):
        return await _step_promote(work_id, review_id, st, st_now, batch)

    # ② 本轮还有人没发言 → 叫下一个
    members = [str(x) for x in (st_now.get("members") or [])]
    spoken = set(await review_store.spoken_round(work_id, review_id))
    todo = [m for m in members if m not in spoken]
    if todo:
        role = todo[0]
        try:
            spoke = await speak(work_id, review_id, role, st)
        except Exception as exc:  # noqa: BLE001 单个角色失败不挡其余
            logger.warning("[review] 角色发言失败 role=%s: %s", role, exc)
            print(f"[review] 角色发言失败 role={role}: {exc}", flush=True)
            spoke = {"role": role, "error": str(exc)}
        if isinstance(spoke, dict) and spoke.get("error"):
            _emit_round_done(work_id, review_id, batch, st_now, changed=False,
                             reason="speak_failed", continued=False)
            return {"continued": False, "reason": "speak_failed",
                    "role": role, "error": str(spoke.get("error"))}
        await review_store.mark_spoken(work_id, review_id, role)
        st_now = await review_store.status(work_id, review_id)
        if st_now.get("all_passed"):
            return await _step_promote(work_id, review_id, st, st_now, batch)
        _emit_round_done(work_id, review_id, batch, st_now, changed=False,
                         reason="spoken", continued=True)
        return {"continued": True, "reason": "spoken", "role": role}

    # ③ 本轮全员发完 → 子卡改**一次**稿（把所有人的意见一起给它）
    rev = await revise(work_id, review_id, st)
    if isinstance(rev, dict) and rev.get("busy"):
        # 子卡自己的回合在跑：本步让路、不续（它跑完自己会发事件接力）
        _emit_round_done(work_id, review_id, batch, st_now, changed=False,
                         reason="child_busy", continued=False)
        return {"continued": False, "reason": "child_busy"}
    changed = bool(isinstance(rev, dict) and rev.get("changed"))
    st_now = await review_store.status(work_id, review_id)
    if st_now.get("all_passed"):
        return await _step_promote(work_id, review_id, st, st_now, batch)
    _emit_round_done(work_id, review_id, batch, st_now, changed=changed,
                     reason="revised" if changed else "child_refused",
                     continued=True)
    return {"continued": True,
            "reason": "revised" if changed else "child_refused"}


async def _step_promote(work_id: str, review_id: str, st: dict[str, Any],
                        st_now: dict[str, Any], batch: str) -> dict[str, Any]:
    """全过之后那一步：叫子卡自己写正稿（系统不代劳）。"""
    target = str(st.get("target") or "")
    if target and not await _is_promoted(work_id, target):
        rev = await revise(work_id, review_id, st, ask_promote=True)
        if isinstance(rev, dict) and rev.get("stopped"):
            _emit_round_done(work_id, review_id, batch, st_now, changed=False,
                             reason="stopped", continued=False)
        if isinstance(rev, dict) and rev.get("busy"):
            _emit_round_done(work_id, review_id, batch, st_now, changed=False,
                             reason="child_busy", continued=False)
            return {"continued": False, "reason": "child_busy"}
        done = bool(isinstance(rev, dict) and rev.get("promoted"))
        _emit_round_done(work_id, review_id, batch, st_now, changed=done,
                         reason="promoted" if done else "promote_pending",
                         continued=not done)
        return {"continued": not done,
                "reason": "promoted" if done else "promote_pending"}
    _emit_round_done(work_id, review_id, batch, st_now, changed=False,
                     reason="all_passed", continued=False)
    return {"continued": False, "reason": "all_passed"}


def _emit_round_done(work_id: str, review_id: str, batch: str,
                     st: dict[str, Any], *, changed: bool, reason: str,
                     continued: bool = True) -> None:
    """一步跑完的落点：harness 的 continue 规则挂在这个事件上。

    `continued` 是**这一步自己的判断**：还有活就 true，到站就 false。
    策略只照它办（guard: continued=true），不另行推断——否则「模型不可用」
    这种环境失败会被当成"还没完"，无限重试一个挂掉的模型。
    """
    try:
        event_stream.emit(work_id, review_id, "review_round_done", {
            "batch": batch,
            "draft": int(st.get("draft") or 1),
            "all_passed": bool(st.get("all_passed")),
            "passed": list(st.get("passed") or []),
            "pending": list(st.get("pending") or []),
            "changed": bool(changed),
            "reason": reason,
            "continued": bool(continued),
        })
    except Exception:  # noqa: BLE001 事件失败不影响主流程
        pass


async def _round_loop(work_id: str, review_id: str) -> list[dict[str, Any]]:
    """评审循环：**一轮发言 → 子卡改草稿 → 再一轮**，直到全员通过。

    [2026-10-29 用户口径，第一手]
    > 「评审链路是一个一边评审一边修改草稿的链路，所以，一次评审全员发言，
    >   子卡直接在评审卡内，对草稿进行修改，然后再全员评审，直到所有成员通过为止」
    > 「通过之后，它就不再被唤起进行后续的评审（因为还会有其他成员不通过，
    >   所以即便 A 通过，后面也会继续改继续评审）」

    改前只做前半段：叫未通过的成员发言，跑完版本没推进就 break ⇒ 停在原地，
    没人改稿、没人再叫（work_027 实跑就是发了三轮言、一条 approve 都没有）。

    现在的循环（每一步的退出条件都是**状态**，无墙钟、无次数上限）：

        有批次？ → 全过了？ → 还有谁没通过？
          ↓ 逐个发言（只叫未通过的；已通过的不再叫）
        全过了？ → 有人没表态成功？ → 停（叫不动人，再改材料也没人评）
          ↓
        子卡在评审卡现场改草稿
          ↓ 改了 → 下一轮；没改 → 停（它认为不用改，等人介入）

    「已通过的不再被叫」由 `status()` 保证：票只在**被撤销**时失效，
    草稿版本推进不再让票作废（见 `review_store.status` 的 docstring）。
    """
    out: list[dict[str, Any]] = []
    while True:
        st = await review_store.state_of(work_id, review_id)
        if not st.get("batch"):
            break
        st_now = await review_store.status(work_id, review_id)
        if st_now.get("all_passed"):
            break
        pending = list(st_now.get("pending") or [])
        if not pending:
            break
        batch = str(st.get("batch") or "")
        try:
            event_stream.emit(work_id, review_id, "review_round_start", {
                "batch": batch,
                "draft": int(st.get("draft") or 1),
                            "pending": pending,
            })
        except Exception:  # noqa: BLE001 事件失败不影响评审
            pass

        spoke_ok = 0
        for role in pending:
            try:
                r = await speak(work_id, review_id, role, st)
                out.append(r)
                if not (isinstance(r, dict) and r.get("error")):
                    spoke_ok += 1
            except Exception as exc:  # noqa: BLE001 单个角色失败不挡其余
                logger.warning("[review] 角色发言失败 role=%s: %s", role, exc)
                out.append({"role": role, "error": str(exc)})

        st2 = await review_store.status(work_id, review_id)
        if st2.get("all_passed"):
            break
        if not list(st2.get("pending") or []):
            break
        if spoke_ok == 0:
            # 这一轮没有任何成员成功表态（模型挂了/全被拒）⇒ 停。
            # 再让子卡改材料也没有人能评，继续转只会空烧额度。
            logger.warning("[review] 本轮无成员成功表态，停止循环 review=%s", review_id)
            break

        # ---- 有人未通过 ⇒ 子卡在评审卡现场改草稿 ----
        rev = await revise(work_id, review_id, st)
        out.append(rev)
        if isinstance(rev, dict) and rev.get("busy"):
            # 子卡还在跑自己的回合（open_review 就在它那一轮里）⇒ 本轮不抢它的
            # 互斥体。它那一轮结束时若改了草稿会自动触发下一轮；没改就是停在原地。
            logger.info("[review] 子卡回合在跑，本轮改稿跳过 review=%s", review_id)
            break
        if not (isinstance(rev, dict) and rev.get("changed")):
            # 子卡看过意见、认为不用改 ⇒ 停在原地等介入（不催办、不超时）。
            logger.info("[review] 子卡未改草稿，评审停在 pending=%s", st2.get("pending"))
            break
    return out


# ---------------------------------------------------------------- 子卡改稿
async def revise(work_id: str, review_id: str,
                 st: dict[str, Any] | None = None, *,
                 ask_promote: bool = False) -> dict[str, Any]:
    """**子卡在评审卡现场改自己的需求文档草稿**（[2026-10-29 用户口径]）。

    用户原话：「子卡直接在评审卡内，对草稿进行修改」。

    「在评审卡内」的意思不是把子卡搬进评审卡，而是**这一轮跑的现场是评审卡**：
    它读到的是评审现场（未通过成员的原话），它写的是自己的草稿列。
    所以上下文取评审卡，写入口是自己的 `edit_requirements`。

    与 `speak` 的区别：
      - 身份＝**子卡**（被评方），不是职能；
      - 工具＝`edit_requirements` ＋ 只读件，**没有 approve**（它不是评审成员）；
      - 模型＝子卡卡上配置的模型（不是职能的）。

    返回 `{"role","changed","chars","tools"}`；`changed` 是**草稿指纹真的变了**，
    不是"模型说它改了"——调用方靠这一位决定要不要再来一轮。
    """
    from . import card_tools          # 延迟 import：card_tools 体量大且反向引用

    st = st if st is not None else await review_store.state_of(work_id, review_id)
    target = str(st.get("target") or "")
    if not target:
        return {"role": "", "changed": False, "error": "批次没有目标子卡"}
    # [2026-10-29] 与子卡**自己的回合**互斥：open_review 是它在本轮里调的工具，
    # 返回后它还会继续这一轮。评审后台跑到改稿这一步时，它很可能还在跑。
    # 抢不到就本轮跳过——等它这一轮结束：若它自己改了草稿会自动触发下一轮，
    # 没改就等同「子卡不改」，评审停在原地（不设墙钟、不重试）。
    _owner = f"review:{review_id}"
    if not session_facade.turn_try_acquire(work_id, target, _owner):
        logger.info("[review] 子卡 %s 正在跑自己的回合，本轮改稿跳过", target)
        return {"role": target, "changed": False, "busy": True}
    try:
        return await _revise_inner(work_id, review_id, st, target,
                                   ask_promote=ask_promote)
    finally:
        session_facade.turn_release(work_id, target, _owner)


async def _revise_inner(work_id: str, review_id: str, st: dict[str, Any],
                        target: str, *, ask_promote: bool = False) -> dict[str, Any]:
    from . import card_tools          # 延迟 import：card_tools 体量大且反向引用

    batch = str(st.get("batch") or "")
    # 改前指纹：只算草稿列（与 status() 同源）
    h0 = await review_store._card_hash(work_id, target)

    try:
        event_stream.emit(work_id, review_id, "review_speaking", {
            "batch": batch, "role": target, "kind": "revise",
            "name": await _name_of(work_id, target),
        })
    except Exception:  # noqa: BLE001 展示层零阻断
        pass

    sys_p = await _revise_system_prompt(work_id, target, review_id,
                                       ask_promote=ask_promote)
    st_now = await review_store.status(work_id, review_id)
    user_p = await _revise_user_prompt(
        work_id, review_id, st, target, list(st_now.get("pending") or []))
    if ask_promote:
        # [2026-10-29] 全员通过之后叫它，不能再给它「按意见改草稿」那套话——
        # 它看到"改草稿"就会去改草稿，永远不写正稿，批次就转不出去。
        # 这一轮的任务只剩一件：调 promote_draft 定稿。
        user_p = (
            "## 这一轮的任务\n\n"
            "评审**已全员通过**，草稿不用再改了。\n"
            "请调 `promote_draft` 把当前草稿转成正稿——这是本批次唯一剩下的动作。\n\n"
            "---\n\n" + user_p)
    convo = await _prior_messages(work_id, review_id, batch, target)
    # [2026-10-30 用户裁定] 改稿工具面＝**正式对话的工具面**（减建子卡／汇报／通信）：
    # 子卡平时能用的它都能用——能查证据、能改现状块、能改草稿。
    # cards 要先取（工具面按它的权限算），打标记在后面
    cards = card_store.iter_card_metas(work_id)
    names = tuple(_revise_tools(work_id, target, cards))
    if ask_promote:
        # [2026-10-29] 全员通过后叫子卡写正稿：这时才给它 promote_draft。
        names = ("promote_draft",) + names
    tools = [_schema_of(card_tools, n) for n in names if n in card_tools.TOOL_REGISTRY]
    if not ask_promote:
        # 定稿那一步的终态工具是 promote_draft（已在面里）；改稿这一步是 submit_revision。
        tools = tools + [dict(_SUBMIT_REVISION)]
    # [2026-10-29 用户令「哪个 agent 不讲明白」] 工具清单写进提示词：
    # 名字／干什么／参数怎么填，全部从 schema 生成（不手写，不会与工具定义脱节）。
    sys_p = sys_p + "\n\n" + _tools_block(tools)
    model = _model_of(work_id, target)
    # [2026-10-29 用户令「评审卡应该能读所有卡的卡面，卡面才是唯一事实源」]
    # 改稿是在评审现场改，它同样要核对全树（证据文件、别的卡的现状块）⇒ 也放开读。
    _me = dict(cards.get(target) or {})
    _me["_review_seat"] = True
    cards[target] = _me

    texts: list[str] = []
    thinks: list[str] = []
    called: list[dict[str, Any]] = []
    used = model
    seen: dict[str, str] = {}

    # 工具面**就是**许可面：不在面里的工具一律不执行（approve/reject 已被
    # REVISE_DENY 挡在面外，改稿回合投不了票）。
    _allowed = {str(n) for n in names}
    terminal = "promote_draft" if ask_promote else "submit_revision"
    submitted = False
    # [2026-10-31 修·评审发言不进对话界面] 评审轮没有 human 块 ⇒
    # `session_facade._assign_block` 的 pair 取不到 ⇒ pair_id="unknown"
    # ⇒ 前端拿不到真实 run_id ⇒ 发言落不进任何一轮，界面只剩工具行
    #（实测 card_124 的 assistant_text run_id 全是 "unknown"，正常卡是 UUID）。
    # ⇒ 每轮给一个真实 turn id，工具块与发言块共用它。
    _turn_id = uuid.uuid4().hex
    name_of_terminal = False
    # [2026-10-31 用户令] **一个席位/一次回应＝一轮**；但**轮内可以连续调工具直到它完成**
    # （改草稿 → 拿结果 → 交回，这是同一个人的一次工作，不是换视角）。
    # 只有它没按当轮要求做完（没表态/没交回）时，才由 harness 再叫一轮。
    while True:
        # 停止＝直接把正在跑的这个任务掐掉（control.cancel_turn 取消本任务），
        # 不在循环里查标志位。
        # [2026-10-31 用户令] 工具面**永远完整**：改稿轮同样不做收敛。
        # 「模型不可能无限查下去，没有模型会无限查下去」。
        # 改前第 2 轮起只剩 submit_revision ⇒ 它拿不到 edit_requirements，
        # 想改也改不动，只能空手回一句 ⇒ changed=False ⇒ 判「子卡不改」。
        tools_now = tools
        res, used = await model_service.call_main(
            sys_p, user_p, convo, tools_now, model)
        text = str(res.get("content") or "")
        think = str(res.get("reasoning_content") or "")
        if text.strip():
            # 只留**最后一轮**有内容的发言：无限重试时不再累积（测试里曾因此 MemoryError）
            texts = [text]
        if think:
            thinks.append(think)

        tcs = list(res.get("tool_calls") or [])
        if not tcs:
            # 没有工具调用 ⇒ 模型认为做完了（或选择不做），自然结束。
            # 不再"再调起它重试"——那是 harness 层的事（review_round_done 的事件策略）。
            # changed 由草稿指纹判定，不依赖模型自述。
            break
        for tc in tcs:
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
            name = str(fn.get("name") or "")
            raw = fn.get("arguments")
            if isinstance(raw, dict):
                args = dict(raw)
            else:
                try:
                    args = json.loads(raw) if raw else {}
                except Exception:  # noqa: BLE001
                    args = {}

            if name == terminal:
                _emit_tool_call(work_id, review_id, target, name, args)
                if terminal == "promote_draft":
                    r = await card_tools.execute_tool(
                        work_id, target, name, args, cards)
                else:
                    r = {"ok": True, "data": {"summary": str(args.get("summary") or "")}}
                _emit_tool_result(work_id, review_id, target, name, r)
                called.append({"tool": name, "ok": bool(r.get("ok"))})
                submitted = True
                break

            if name == "edit_requirements":
                # 强制写自己：评审现场改的是被评卡的草稿，不给它改别人的机会
                args["card_id"] = target
                _emit_tool_call(work_id, review_id, target, name, args)
                try:
                    r = await card_tools._h_edit_requirements(
                        work_id, target, args, cards)
                    called.append({"tool": name, "ok": bool(r.get("ok")),
                                   "error": str(r.get("error") or "")})
                    payload = json.dumps(r, ensure_ascii=False)
                except Exception as exc:  # noqa: BLE001
                    called.append({"tool": name, "ok": False, "error": str(exc)})
                    r = {"ok": False, "error": str(exc)}
                    payload = f"工具异常：{exc}"
                _emit_tool_result(work_id, review_id, target, name, r)
                convo.append({"role": "user",
                              "content": f"【工具结果 {name}】\n{payload}"})
                # 改完要再问一次：让它把"改了哪几处"说出来，落进评审卡会话
                # （后面的成员要看到改了什么）。说完不再调工具就自然退出。
                # 失败同理——工具失败＝没改成，必须再调起它重试。
                continue

            if name in READ_TOOLS:
                # [2026-10-31 用户令] 不做重复查询拦截（同评审轮）。
                _emit_tool_call(work_id, review_id, target, name, args)
                try:
                    r = await card_tools.execute_tool(
                        work_id, target, name, args, cards)
                    payload = json.dumps(r, ensure_ascii=False)
                    called.append({"tool": name, "ok": bool(r.get("ok"))})
                except Exception as exc:  # noqa: BLE001
                    payload = f"工具异常：{exc}"
                    r = {"ok": False, "error": str(exc)}
                    called.append({"tool": name, "ok": False, "error": str(exc)})
                _emit_tool_result(work_id, review_id, target, name, r)
                try:
                    await session_facade.append(work_id, _sid(review_id, batch), {
                        "actor": "tool",
                        "content": f"〔{name} · {batch}〕\n{payload}",
                        "model": used,
                        "pair_id": _turn_id,
                    })
                except Exception:  # noqa: BLE001
                    pass
                convo.append({"role": "user",
                              "content": f"【工具结果 {name}】\n{payload}"})
                continue

            # [2026-10-30 实跑抓到] 工具面给了 27 个，执行器只认 edit_requirements
            # 与查阅——`promote_draft` / `update_status` 全是**假笔**：模型调了也被拒。
            # 现在按工具面执行（权限仍由 `execute_tool` 自己判）。
            if name in _allowed and name in card_tools.TOOL_REGISTRY:
                _emit_tool_call(work_id, review_id, target, name, args)
                try:
                    r = await card_tools.execute_tool(
                        work_id, target, name, args, cards)
                    payload = json.dumps(r, ensure_ascii=False)
                    called.append({"tool": name, "ok": bool(r.get("ok"))})
                except Exception as exc:  # noqa: BLE001
                    payload = f"工具异常：{exc}"
                    r = {"ok": False, "error": str(exc)}
                    called.append({"tool": name, "ok": False, "error": str(exc)})
                _emit_tool_result(work_id, review_id, target, name, r)
                convo.append({"role": "user",
                              "content": f"【工具结果 {name}】\n{payload}"})
                continue
            called.append({"tool": name, "ok": False,
                           "error": f"工具面里没有 {name}（改稿回合不投通过/不通过）"})
        # 轮内全交给模型：调了工具就继续，没调就是它给出的回应。
        if not tcs:
            break

    text = "\n".join(texts)
    think = "\n".join(thinks)

    try:
        prompt_log.write_prompt_log(
            card_id=review_id, work_id=work_id,
            system_prompt=sys_p, prior_messages=convo, user_prompt=user_p,
            model_requested=model, model_used=used, response=text,
            error=None, reasoning=think)
    except Exception as exc:  # noqa: BLE001 日志失败不影响评审
        logger.warning("[review] revise prompt 日志写失败 review=%s: %s", review_id, exc)

    if text.strip():
        try:
            await session_facade.append(work_id, _sid(review_id, batch), {
                "actor": "assistant",
                "content": f"〔{await _name_of(work_id, target)} · {batch}〕\n{text}",
                "model": used,
                "pair_id": _turn_id,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("[review] revise 落块失败 review=%s: %s", review_id, exc)


    h1 = await review_store._card_hash(work_id, target)
    changed = bool(h1) and h1 != h0
    try:
        event_stream.emit(work_id, review_id, "review_revise", {
            "batch": batch, "target": target,
            "name": await _name_of(work_id, target),
            "model": used, "chars": len(text), "changed": changed,
        })
    except Exception:  # noqa: BLE001
        pass
    _promoted = bool(ask_promote and await _is_promoted(work_id, target))
    return {"role": target, "model": used, "chars": len(text),
            "promoted": _promoted,
            "changed": changed, "tools": called}


# ---------------------------------------------------------------- 触发
async def maybe_rerun(work_id: str, child_id: str) -> bool:
    """**子卡卡面变了** ⇒ 只推进草稿版本，**不起轮**。

    [2026-10-30 用户令「谁让你做这么多独立的起轮口子」]
    这里原来还顺手 `rerun_if_pending()` 起一轮——而它由 `card_store.update_card`
    每次真变都触发。子卡在一次改稿里写十几次卡面 ⇒ 十几次起轮，全部撞在
    改稿那把卡级锁上、秒回 busy，再被当成"还没跑完"继续叫 ⇒ 空转。
    **写卡不是起轮的时机**：正在跑的那一步自己会收尾并发事件接力。
    这里只做一件事：把草稿版本推进（下一轮据此知道"第几版"）。
    """
    try:
        rid = await review_store.review_card_of(work_id, child_id)
        if not rid:
            return False
        return await review_store.sync_draft(work_id, rid)
    except Exception as exc:  # noqa: BLE001 触发失败不影响写卡
        logger.warning("[review] 重评触发失败 child=%s: %s", child_id, exc)
        return False


async def rerun_if_pending(work_id: str, review_id: str) -> bool:
    """版本已被别处推进（撤销通过等）后，起一轮重评。"""
    try:
        st = await review_store.status(work_id, review_id)
        if not st.get("batch") or st.get("all_passed"):
            return False
        spawn_round(work_id, review_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 重评起轮失败 review=%s: %s", review_id, exc)
        return False


def spawn_round(work_id: str, review_id: str) -> Any:
    """后台**跑一步**（不阻塞调用方）。返回 Task，测试可 await。

    [2026-10-29 用户裁定「用 harness 接管评审卡的流程存续」]
    改前起的是 `run_round`（进程内 while 跑到底，重启就断、卡住没人叫）。
    现在只跑 `run_once` 一步：它发 `review_round_done`，由策略 continue 决定
    要不要再叫下一次。流程存续不再依赖某个活着的 task。
    """
    from . import message_runner as _mr
    return _mr._spawn_tracked(run_once(work_id, review_id))



async def _run_card_turn(work_id: str, card_id: str, *,
                          system_prompt: str, user_prompt: str,
                          prior: list[dict[str, Any]],
                          tools: list[dict[str, Any]]) -> str:
    """**完整复用普通卡的对话回合**：构造 RunState → 驱动 message_runner.step 到交付。

    轮内怎么调模型、怎么执行工具、什么时候算完，全在 `message_runner.step` 里
    （和每一张普通卡走的是同一套），这里没有任何自写循环判断。
    返回本轮的最终回复正文。
    """
    from . import message_runner as _mr

    state = _mr.RunState(work_id=work_id, card_id=card_id, content=user_prompt)
    state.system_prompt = system_prompt
    state.vp_system_prompt = system_prompt
    state.user_prompt = user_prompt
    state.prior = list(prior)
    state.prior_for_gateway = list(prior)
    state.history_len = len(state.prior_for_gateway)
    state.tools = list(tools)
    while True:
        outcome = await _mr.step(state)
        if getattr(outcome, "kind", "") in ("deliver", "error"):
            break
    return str(state.final_reply or "")


async def speak(work_id: str, review_id: str, role_id: str,
                st: dict[str, Any] | None = None) -> dict[str, Any]:
    """一个角色发言一次：拼上下文 → 调（职能的模型）→ 执行工具 → 落会话。

    工具循环：模型可以**先查证再表态**（`read_card` / `read_file` / …），
    查到的结果留在评审卡会话里——后面发言的人直接看到，不必各自重查。
    循环的退出条件是「模型不再调工具」，不设次数上限（同项目既有口径）。
    """
    from . import card_tools          # 延迟 import：card_tools 体量大且反向引用

    st = st if st is not None else await review_store.state_of(work_id, review_id)
    batch = str(st.get("batch") or "")
    target = str(st.get("target") or "")   # 被评的子卡：评审站在它的语境里
    target = str(st.get("target") or "")   # 被评的子卡：评审站在它的语境里
    # [评审窗口 2026-10-29] 告诉界面「现在轮到谁」——逐字流帧本身不带说话人，
    # 没有这一条，窗口里只能显示"思考中…"，看不出是谁在思考。
    try:
        event_stream.emit(work_id, review_id, "review_speaking", {
            "batch": batch, "role": role_id, "kind": "member",
            "name": await _name_of(work_id, role_id),
        })
    except Exception:  # noqa: BLE001 展示层零阻断
        pass
    # [2026-10-30 用户裁定] **一次装配**：system/prior/user 全由装配层给，
    # 评审直接接子卡的上下文（history 区就是子卡的 prior），只换身份＋补评审块。
    sys_p, convo, user_p = await _speak_context(work_id, role_id, review_id, st)
    # 再叠**本批次评审现场**的发言（子卡历史里没有这些）
    convo = convo + await _prior_messages(work_id, review_id, batch)
    # 评审是明确的卡内场景：以**当前受邀角色自己的能力身份**生成基础工具面，
    # 然后只留只读工具；approve/reject 由评审机制单独提供。
    cards = card_store.iter_card_metas(work_id)
    _prepare_review_actor(work_id, role_id, cards)
    review_read_names = _review_read_tools(work_id, role_id, cards)
    names = ("approve", "reject") + tuple(review_read_names)
    tools = [_schema_of(card_tools, n) for n in names if n in card_tools.TOOL_REGISTRY]
    # [2026-10-29 用户令「哪个 agent 不讲明白」] 工具清单写进提示词：
    # 名字／干什么／参数怎么填，全部从 schema 生成（不手写，不会与工具定义脱节）。
    sys_p = sys_p + "\n\n" + _tools_block(tools)
    model = _model_of(work_id, role_id)

    texts: list[str] = []
    thinks: list[str] = []
    called: list[dict[str, Any]] = []
    used = model

    # [2026-10-31 用户令] 工具面**永远完整**，不做任何收敛：
    # 「模型不可能无限查下去，没有模型会无限查下去」。
    # 改前每轮把工具砍成 approve/reject（实测第 2 轮只收到 2 件）⇒ 模型查一次
    # 就没工具可用了。现在给多少就是多少，查多久由模型自己决定。
    # [2026-10-31 修·评审发言不进对话界面] 没有 human 块 ⇒ pair_id="unknown"
    # ⇒ 前端拿不到真实 run_id ⇒ 发言落不进任何一轮。每轮给真实 turn id。
    _turn_id = uuid.uuid4().hex
    # [2026-10-31 用户令] **一个席位/一次回应＝一轮**；但**轮内可以连续调工具直到它完成**
    # （改草稿 → 拿结果 → 交回，这是同一个人的一次工作，不是换视角）。
    # 只有它没按当轮要求做完（没表态/没交回）时，才由 harness 再叫一轮。
    # [2026-10-31 用户令] **轮内全交给普通卡那套回合**：不再自写模型调用与工具循环。
    text = await _run_card_turn(
        work_id, review_id,
        system_prompt=sys_p, user_prompt=user_p,
        prior=convo, tools=tools)
    think = ""
    texts = [text] if text.strip() else []
    text = "\n".join(texts)
    think = "\n".join(thinks)

    # [评审可观测 2026-10-27] 评审调用**也要留 prompt 日志**：改前只有 message_runner
    # 写，评审回合一条都没有 ⇒ 思考过程与送进模型的上下文事后无从追溯（实弹跑时
    # 沙箱 logs/ 是空的才发现）。
    try:
        prompt_log.write_prompt_log(
            card_id=review_id, work_id=work_id,
            system_prompt=sys_p, prior_messages=convo, user_prompt=user_p,
            model_requested=model, model_used=used, response=text,
            error=None, reasoning=think)
    except Exception as exc:  # noqa: BLE001 日志失败不影响评审
        logger.warning("[review] prompt 日志写失败 review=%s: %s", review_id, exc)

    if text.strip():
        try:
            await session_facade.append(work_id, _sid(review_id, batch), {
                "actor": "assistant",
                # 首行带批次标记：`_prior_messages` 靠它只取本批次发言（P6）
                "content": f"〔{await _name_of(work_id, role_id)} · {batch}〕\n{text}",
                "model": used,
                "pair_id": _turn_id,
            })
        except Exception as exc:  # noqa: BLE001 落块失败不影响工具执行
            logger.warning("[review] 发言落块失败 review=%s: %s", review_id, exc)


    # [评审可观测 2026-10-27] 每次发言落一条事件：谁、什么模型、说了多少字、
    # 有没有调 approve。改前评审回合在事件流里**一条记录都没有**——"为什么又跑了
    # 一轮"这种问题只能靠读会话猜。
    try:
        event_stream.emit(work_id, review_id, "review_spoke", {
            "batch": batch, "role": role_id,
            "name": await _name_of(work_id, role_id),
            "model": used, "chars": len(text),
            "approve": any(c.get("tool") == "approve" and c.get("ok")
                           for c in called),
        })
    except Exception:  # noqa: BLE001 事件失败不影响评审
        pass

    return {"role": role_id, "model": used, "chars": len(text), "tools": called}


# ---------------------------------------------------------------- 上下文
def _review_charter(role_id: str) -> str:
    """读**评审职责书**（`config/review_charters.json`，按角色 id 存）。

    [2026-10-29 用户令] 「它的日常职责和它的评审职责，是两个职责，评审职责是专门写的」
    ⇒ 评审席上读的必须是这一份，不是 `function_profiles.charter`（那是日常职责）。
    没写到的角色返回空串，调用方回落到日常职责（并在标题上区分，能看出缺哪几份）。
    """
    try:
        data = json.loads(REVIEW_CHARTERS.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 读不到＝没写，回落
        logger.warning("[review] 读评审职责书失败: %s", exc)
        return ""
    row = data.get(role_id) or {}
    return str(row.get("text") or "") if isinstance(row, dict) else ""


def _text_tool_tag(text: str, tools: list[dict[str, Any]]) -> str:
    """正文里有没有**写成标签的工具调用**（如 `<read_file">`、`<edit_requirements>`）。

    [2026-10-29 用户令] 「工具调错，你应该继续调起模型，让它继续尝试，怎么会卡住？」
    有些模型不用结构化 tool_calls，而是把调用写成标签：`<read_file">\n<path>..</path>`。
    这种输出既不是合法工具调用、也不是"我认为不用改"的表态 —— 属于**格式错**，
    必须再调起它重试，不能当成终态 break。

    返回命中的标签名（没命中返回空串）。工具名从本回合工具面取，不硬编码。
    """
    names = set()
    for t in tools or []:
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        n = str(fn.get("name") or "")
        if n:
            names.add(n)
    for m in re.finditer(r"<([A-Za-z_][\w]*)[\s\"'>/]", text or ""):
        if m.group(1) in names:
            return m.group(1)
    return ""


def _tools_block(tools: list[dict[str, Any]]) -> str:
    """把本回合的工具面**原样展开**成提示词里的一节：名字、干什么、参数怎么填。

    [2026-10-29 用户令] 「哪个 agent 不讲明白？」——工具清单不能只靠 API 的
    `tools` 参数，要在提示词里讲清每个工具是什么、参数填什么。
    内容**从 schema 生成**（不手写），保证与工具定义永远一致。
    """
    if not tools:
        return ""
    lines = ["## 你这一轮能调的工具", ""]
    for t in tools:
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = str(fn.get("name") or "")
        if not name:
            continue
        desc = str(fn.get("description") or "").strip()
        lines.append(f"### `{name}`")
        if desc:
            lines.append(desc)
        params = ((fn.get("parameters") or {}).get("properties") or {})
        required = set((fn.get("parameters") or {}).get("required") or [])
        if params:
            lines.append("参数：")
            for k, v in params.items():
                if not isinstance(v, dict):
                    continue
                flag = "（必填）" if k in required else "（可选）"
                pdesc = str(v.get("description") or v.get("type") or "").strip()
                lines.append(f"- `{k}`{flag}：{pdesc}")
        lines.append("")
    return "\n".join(lines).strip()


async def _speak_context(work_id: str, role_id: str, review_id: str,
                       st: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str]:
    """评审席的**一次装配**：直接接子卡的上下文，只换身份、补评审专用块。

    [2026-10-30 用户裁定·第二次纠正]
    > 「评审卡是伴生卡，它应该就取的就是子卡的上下文，直接接入，并替换相应的部分，
    >   而不是你重做一套」

    所以这里不手拼历史、不手拼区域：`build_ctx(子卡)` ＋ `history_source=子卡 prior`
    ＋ `function_meta=评审身份` ＋ `profile="viewpoint"`，由装配层一次给出
    system / prior / user 三件。评审**只换身份职责区（145）＋把 user 换成评审专用块**。

    [2026-10-31 用户裁定·第三次纠正，逐字]
    > 「评审席拿到的应该是……和子卡的上下文基本一致，只有身份职责区要替换」
    > 「它不需要根卡或者职能卡自己的任何信息，那些信息全部都只是污染」

    ⇒ 锚点必须是**子卡**；评审席自己那张卡（根卡／职能卡）的卡面、记忆、直接子卡、
    影子一律不进。

    返回 `(system_prompt, prior_messages, user_prompt)`。
    """
    from ..storage import memory_store as _ms
    from ..storage import origin_store as _os
    from . import context_assembler as _ca
    from . import context_regions as _cr

    target = str(st.get("target") or "")
    name = await _name_of(work_id, role_id)
    duty = _review_charter(role_id)
    if not duty:
        try:
            prof = function_store.get_profile(work_id, role_id) or {}
            ch = prof.get("charter") or {}
            duty = str(ch.get("text") if isinstance(ch, dict) else ch or "")
        except Exception:  # noqa: BLE001
            duty = ""

    # 子卡的历史：**原样取法**（凝练水位起读），交给装配层的 history 区渲染
    prior_src: list[dict[str, Any]] = []
    if target:
        try:
            _mark = int((_ms.load_memory(work_id, target) or {}).get("mark", 0) or 0)
            prior_src = list(_os.read_blocks(work_id, target, _mark) or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[review] 读子卡历史失败 %s: %s", target, exc)

    # [2026-10-31 用户裁定 · 撤销同日早先的锚点漂移]
    # 锚点＝**被评子卡**。评审席拿到的上下文与子卡**基本一致**，唯一被替换的是
    # 身份职责区（145，由 function_meta 承载评审职责书）。
    # 根卡／职能卡自己的卡面、记忆、直接子卡、影子**一律不进评审席**——用户原话：
    # 「它不需要根卡或者职能卡自己的任何信息，那些信息全部都只是污染」。
    # （同日早先我把锚点换成评审席自己，导致根卡 139,802 字记忆＋19,881 字直接子卡
    #   全部灌进评审席；那是一次推理跳步：从「总裁编造了一次对根卡正稿的引用」
    #   直接推成「把根卡正稿塞给它」，而正确的修法是出处纪律＋要引用就自己 read_card。
    #   已撤销。）
    ctx = _ca.build_ctx(
        work_id, target or role_id,
        history_source=prior_src,
        function_meta={"function_id": role_id, "function_name": name,
                       "charter": {"text": duty}})
    user_content = await _user_prompt(work_id, review_id, st)
    msg = _cr.compose_model_context(ctx, profile="viewpoint",
                                    user_content=user_content)
    sys_p = str(msg.get("system_prompt") or "").strip()
    # [2026-10-31 用户令] 代码里**不许写规则**：RULES 整块已删。
    # 只保留一条**事实**：评审卡 id（approve/reject 的参数要填它）。
    sys_p = f"{sys_p}\n\n## 评审卡\nid = {review_id or '（未知）'}"

    # [2026-10-31 用户令] 提示词结构重排：
    #   · 前面全部是**背景资料**（被评卡的卡面与历史），必须显式标明归属，
    #     否则模型会把「你是 card_074」读成自己，把上一轮历史当成自己说过的话；
    #   · **身份、评审职责、当轮任务压在最下方**（当轮输入任务口），
    #     让模型最后读到的是"我是谁、这一轮做什么"。
    _duty = _review_charter(role_id) or ""
    _tgt_title = ""
    try:
        _tgt_title = str((await card_store.read_card(
            work_id, target)).get("meta", {}).get("title") or "")
    except Exception:  # noqa: BLE001
        _tgt_title = ""
    _task = "\n\n".join([
        "## 资料归属（先说清：上面不是你的）\n"
        f"以上全部是**被评卡 {target}"
        + (f"（{_tgt_title}）" if _tgt_title else "")
        + "**的卡面与历史，不是你的。\n"
        f"那些区域里出现的「你」指的是 {target}，指的是它。\n"
        f"你自己那张卡（{role_id}）的卡面、记忆、子卡不在上面。",
        f"## 你是谁\n你是这次评审的**评审席之一**：{await _name_of(work_id, role_id)}"
        f"（{role_id}）。\n本批次的评审卡 id = {review_id or '（未知）'}。",
        ("## 你的评审职责\n" + _duty) if _duty else "",
        f"## 这一轮的任务\n评审**{target}"
        + (f"（{_tgt_title}）" if _tgt_title else "")
        + "**的需求文档草稿——就在上面「待评审的需求文档草稿」一节，正文原样未截断。\n"
        "通过就调 approve；不通过就调 reject。\n"
        "**理由写在你的发言里**（哪一条不满足、要改成什么）——reject 只是投票，不带理由。",
    ]).strip()
    # [2026-10-31 用户令] 身份/职责/当轮任务压在最下方（当轮输入任务口）
    _up = str(msg.get("user_prompt") or "")
    if _task:
        _up = _up + "\n\n" + _task
    prior = list(msg.get("prior_messages") or [])
    return sys_p, prior, _up


async def _system_prompt(work_id: str, role_id: str,
                         review_id: str = "", target: str = "") -> str:
    """评审席的 system：**站在子卡的语境里**，只换身份（[2026-10-30 用户裁定]）。

    用户原话：「评审卡是子卡的伴生卡不是完全独立的卡，所以评审应该站在语境里，
    而不是独立的上下文，评审卡是继承子卡上下文，又同时对上下文进行定制化的版本」。

    做法＝复用既有的 `viewpoint` profile：`build_ctx(子卡)` 让「当前卡／卡片清单／
    直接子卡／现有职能」这些信息类区域**自动就是子卡的**；只有「你的职能」用
    `function_meta` 换成评审身份（评审职责书）。其余区域原样继承，零手写。

    [2026-10-27] 评审卡 id 必须写进来：实弹里三个角色都把 `approve` 的
    `review_card_id` 填成了批次号（`rv_xxx`）⇒ approve 全被拒。
    """
    from . import context_assembler as _ca
    from . import context_regions as _cr

    name = await _name_of(work_id, role_id)
    duty = _review_charter(role_id)
    if not duty:
        # 没写评审职责的角色：回落到日常职责（标题不同，一眼看出缺哪几份）
        try:
            prof = function_store.get_profile(work_id, role_id) or {}
            ch = prof.get("charter") or {}
            duty = str(ch.get("text") if isinstance(ch, dict) else ch or "")
        except Exception:  # noqa: BLE001
            duty = ""

    ctx = _ca.build_ctx(
        work_id, target or role_id,
        function_meta={"function_id": role_id, "function_name": name,
                       "charter": {"text": duty}})
    msg = _cr.compose_model_context(ctx, profile="viewpoint")
    base = str(msg.get("system_prompt") or "").strip()
    # [2026-10-31 用户令] 代码里不许写规则；只留评审卡 id 这条事实。
    return (f"{base}\n\n"
            f"## 评审卡\nid = {review_id or '（未知）'}")


async def _user_prompt(work_id: str, review_id: str,
                       st: dict[str, Any]) -> str:
    """发起方填写（原样）＋子卡使命/现状 ＋ **待评审的草稿** ＋ 上一批次正稿。

    [2026-10-29 修·用户抓到] 原来给的是 `body["requirements"]`（**正稿**）——
    评审成员一直在拿正稿评，而评审对象是 `requirements_draft`（草稿）。
    正稿是上一批通过后转的，拿它评等于评一份**已经定稿**的东西，
    "改草稿"这条链路从头到尾没被真正评过。

    [P6] 一律**不截断**：这是已知信息的唯一事实源（P1）。
    """
    target = str(st.get("target") or "")
    body: dict[str, Any] = {}
    if target:
        try:
            body = (await card_store.read_card(work_id, target)).get("body") or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[review] 读子卡失败 %s: %s", target, exc)
    # 草稿在 meta 列，不在 body 三块里
    draft = await _draft_of(work_id, target) if target else ""
    brief = str(st.get("brief") or "").strip()
    if not brief:
        brief = f"（发起方未填写，仅有焦点：{st.get('focus') or '无'}）"
    prev = str(st.get("prev_verdict") or "").strip()
    return "\n\n".join([
        "## 发起方填写：任务背景／当前状况／要评审的内容（原文）\n" + brief,
        "## 子卡使命（原样）\n" + str(body.get("mission") or ""),
        # [2026-10-30 用户纠正] 评审**理应看到全部信息、对齐全部信息**——
        # 遮住现状块是错的。要解决的是「看到了却改不动」，不是「别让它看到」。
        "## 子卡现状（原样，供你对齐信息）\n" + str(body.get("status_block") or ""),
        "## 待评审的需求文档草稿（**评审对象就是这一份**）\n"
        + (draft or "（草稿为空）"),
        "## 上一批次正稿（对比基准，不是评审对象）\n" + (prev or "（本卡第一次评审）"),
    ])


async def _revise_system_prompt(work_id: str, target: str,
                                review_id: str, *,
                                ask_promote: bool = False) -> str:
    """子卡改稿时的身份提示词：**你是被评方，不是评审成员**。

    [2026-10-29 用户口径] 评审是「一边评一边改」：子卡在评审现场读意见、
    改自己的草稿。所以这一段要说清三件事——它现在在哪、它能改什么、
    它不能做什么（approve）。
    """
    from . import context_assembler as _ca
    from . import context_regions as _cr

    name = await _name_of(work_id, target)
    # [2026-10-30 用户裁定] 改稿也是**子卡自己的语境**（同构），只多一段现场说明：
    # 它是谁、在哪、能改什么——其余区域（当前卡／卡片清单／现有职能／直接子卡…）
    # 全部继承它平时的样子。不另造一套窄上下文。
    if ask_promote:
        # [2026-10-30 实跑抓到] 全员通过后还写「正稿你改不动」⇒ 和这一轮的任务
        # （调 promote_draft 定稿）直接打架，模型只好什么都不做，批次转不出去。
        prefix = (f"## 你现在在哪\n"
                  f"评审现场（评审卡 {review_id}）。你的需求文档草稿**已经全员通过**，"
                  f"这一轮只剩定稿。\n\n"
                  f"## 你能做什么\n"
                  f"- 调 `promote_draft` 把草稿转成正稿；需要核对就先用查阅工具。\n")
    else:
        prefix = (f"## 你现在在哪\n"
                  f"评审现场（评审卡 {review_id}）。你的需求文档草稿正在被评审，"
                  f"现在轮到你按意见修改。下面有你的草稿、还没通过的成员说的话。\n\n"
                  f"## 你能做什么\n"
                  f"- 改**你自己的**草稿／现状块；查证据；做你在正式对话里能做的绝大部分事。\n"
                  f"- 正稿由评审通过后产生，你改不动它。\n")
    try:
        ctx = _ca.build_ctx(work_id, target)
        msg = _cr.compose_model_context(ctx, profile="draft",
                                        system_prefix=prefix)
        base = str(msg.get("system_prompt") or "").strip()
        if base:
            return base
    except Exception as exc:  # noqa: BLE001 装配失败回落到最小说明（不阻断改稿）
        logger.warning("[review] 改稿上下文装配失败 %s: %s", target, exc)
    return f"你是{name}（{target}）。\n\n{prefix}"


async def _revise_user_prompt(work_id: str, review_id: str, st: dict[str, Any],
                              target: str, pending: list[str]) -> str:
    """改稿现场的上下文：当前草稿 ＋ **未通过成员的原话** ＋ 子卡三块。

    与 `_user_prompt` 的差别：评审成员看的是「子卡三块（原样）」，
    子卡看的是「自己现在的草稿 ＋ 别人到底要求改什么」——后者才是它动手的依据。
    """
    body: dict[str, Any] = {}
    try:
        body = (await card_store.read_card(work_id, target)).get("body") or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] revise 读子卡失败 %s: %s", target, exc)
    said = await _batch_transcript(work_id, review_id,
                                   str(st.get("batch") or ""))
    draft = str((await _draft_of(work_id, target)) or "")
    return "\n\n".join([
        "## 你现在的草稿（要改的就是这一份）\n" + (draft or "（草稿为空）"),
        "## 本批次评审现场全部发言（按顺序原样）\n" + said,
        # [2026-10-31 用户令] 这是**当轮的任务口**：说清这一轮怎么算完成。
        "## 这一轮你要做的\n"
        "把上面这些发言里、你认为该改的改掉（用 `edit_requirements`）；\n"
        "改完（或者你决定不改）之后，**调 `submit_revision` 交回本轮结论**——"
        "不交回，这一轮就不算完成，系统会再来问一次。\n"
        "如果你不同意某条意见，可以不改，但要在**你的发言里**说明理由（不是塞进工具参数）。\n"
        "（要查证据就用查阅工具，查几次、先查什么由你定。）",
        "## 你的使命（原样）\n" + str(body.get("mission") or ""),
        "## 你的现状（原样）\n" + str(body.get("status_block") or ""),
    ])


async def _draft_of(work_id: str, card_id: str) -> str:
    """读子卡当前草稿（`requirements_draft`）。读不到返回空串。"""
    try:
        meta = (await card_store.read_card(work_id, card_id)).get("meta") or {}
        return str(meta.get("requirements_draft") or "")
    except Exception:  # noqa: BLE001
        return ""


async def _batch_transcript(work_id: str, review_id: str,
                           batch: str) -> str:
    """本批次评审现场的**全部发言，按会话里的原始顺序排列**。

    [2026-10-31 用户令] 「上下文按顺序排不就好了吗？做你妈的乱七八糟逻辑」
    ⇒ 不做"取未通过成员的意见"这种抽取：谁说的、按什么顺序说的，原样排下来。
    子卡自己读得懂顺序，不需要我替它挑。
    """
    try:
        blocks = await session_facade.read(work_id, _sid(review_id, batch)) or []
        if isinstance(blocks, dict):          # 兼容旧形态
            blocks = blocks.get("blocks") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 读评审会话失败 review=%s: %s", review_id, exc)
        return "（评审会话读不到）"
    mark = f"· {batch}〕" if batch else ""
    out: list[str] = []
    for b in blocks:
        content = str(b.get("content") or "")
        if not content.strip():
            continue
        head = content.split("\n", 1)[0]
        if mark and mark not in head:
            continue                      # 只取本批次
        out.append(content.strip())
    return "\n\n".join(out) or "（本批次还没有发言）"


async def _prior_messages(work_id: str, review_id: str,
                          batch: str = "", target: str = "") -> list[dict[str, Any]]:
    """评审卡会话里**本批次**已有的发言（看得见别人说过什么）。

    [P6] 只按**批次标记**过滤，**不按条数截断**——本批次有几句就带几句。

    [2026-10-27 修] `session_facade.read` 返回的是 **list**，原实现按 dict 取
    `.get("blocks")` ⇒ AttributeError ⇒ 被 except 吞成空 ⇒ **别人说过的话从来没
    进过上下文**（"共用现场"这条是空的）。实弹跑时抓到的。
    """
    try:
        blocks = await session_facade.read(work_id, _sid(review_id, batch)) or []
        if isinstance(blocks, dict):          # 兼容旧形态
            blocks = blocks.get("blocks") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 读评审卡会话失败 review=%s: %s", review_id, exc)
        return []
    # [2026-10-30 用户裁定] 评审**站在子卡的语境里**：先接子卡自己的历史
    # （「评审和子卡一样，知道事情的来龙去脉」），再接本批次的评审现场发言。
    # 评审卡自己的历次历史不带（「历次的评审任务的历史不需要留，正稿就是全部意义」）。
    # [2026-10-30 用户纠正]「评审卡是伴生卡，它取的就是子卡的上下文，直接接入，
    # 并替换相应的部分」——**不在这里自己拼历史**。子卡的 prior 由装配层
    # （`build_context_bundle` 的 history 区）统一渲染，评审传同一份进去即可，
    # 见 `_speak_context`。本函数只保留"本批次评审现场发言"这一层。
    out: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []

    mark = f"· {batch}〕" if batch else ""
    for b in blocks:
        content = str(b.get("content") or "")
        if not content.strip():
            continue
        # [2026-10-31 用户令]「我说话，它们看不见，那我为什么要说话」
        # ⇒ 用户（actor="human"）说的每一句都进评审现场，不受批次标记限制。
        # 用户发言没有 `· rv_xxx〕` 标记，改前被下面这行过滤掉 ⇒ 成员从来看不到。
        # 这里只认 actor 一个字段，不解释内容、不写任何状态。
        if mark and str(b.get("actor") or "") != "human":
            head = content.split("\n", 1)[0]
            if mark not in head:
                continue      # 上一批次的成员发言不带进来（只带它的正稿）
        role = "assistant" if str(b.get("actor") or "") == "assistant" else "user"
        out.append({"role": role, "content": content})
    return out


# ---------------------------------------------------------------- 身份／模型
async def _name_of(work_id: str, role_id: str) -> str:
    try:
        prof = function_store.get_profile(work_id, role_id) or {}
        return str(prof.get("function_name") or role_id)
    except Exception:  # noqa: BLE001
        return role_id


def _model_of(work_id: str, role_id: str) -> str:
    """**职能卡上配置的模型**（模型配置表里的那一行），不是全局默认。

    [2026-10-27 修] 原实现 import `model_config.resolve_card_model` —— **该函数不存在**
    （`model_config` 里的真名是 `pick(role, work_id=, card_id=)`）。异常被吞后一路
    回落到 `pick_model("default")`，于是"每次对职能卡的调用都对应它自己配置的模型"
    这条需求**从来没生效过**：三个职能全跑在全局默认模型上。
    现在改读模型配置表（`model_config.pick` 会读表、缺行则按默认策略自愈填行）。
    """
    try:
        from . import model_config
        got = model_config.pick("default", work_id=work_id, card_id=role_id)
        name = str(got or "")
        if name:
            return name
    except Exception as exc:  # noqa: BLE001 取表失败才回落
        logger.warning("[review] 读职能卡模型失败 role=%s: %s", role_id, exc)
    try:
        from ..thinkchain.model_pick import pick_model
        return str(pick_model("default", work_id=work_id, card_id=role_id))
    except Exception:  # noqa: BLE001
        return ""


def _schema_of(card_tools: Any, name: str) -> dict[str, Any]:
    return card_tools._schema_of(card_tools.TOOL_REGISTRY[name])
