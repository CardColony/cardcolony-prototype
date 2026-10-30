# -*- coding: utf-8 -*-
"""评审回合执行器（[伴生评审卡 2026-10-27]）。

**一次评审发言 ＝ 用「职能卡的模型 ＋ 职能的身份提示词」，在「评审卡的上下文」里跑一轮。**

上下文边界（用户 2026-10-27 硬要求）：
- **进**：子卡三块（使命／现状／需求文档，原样）＋ 发起方填写的 brief（原样）
  ＋ 判据 ＋ 上一批次正稿 ＋ 本批次已有发言
- **不进**：职能卡自己的会话——**评审内容不污染职能卡的日常上下文**

不另造投递/存储（第 1 步「融合机制」）：
- 发言 → `session_facade.append` 落**评审卡**会话
- 工具 → `card_tools.execute_tool` / handler
- 模型 → `model_service.call_main`，模型名取**职能卡上配置的模型**
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from ..storage import card_store, function_store, review_store, session_facade
from . import event_stream, model_service, prompt_log

#: 评审职责书（每个角色**在评审席上**的职责；与日常职责 function_profiles.charter 分开）
REVIEW_CHARTERS = Path(__file__).resolve().parents[2] / "config" / "review_charters.json"

logger = logging.getLogger("api.review_runner")

#: 测试环境开关（照 ECO_NO_AUTO_DELIVER 先例）：置 1 则不起评审回合
ENV_NO_AUTO = "ECO_NO_AUTO_REVIEW"

#: 评审卡会话里**本批次**的发言全部带进上下文（P6：不截断、不压缩）。
#: 上一批次的发言不带——它们只以"上一批次正稿"的形式进（换上下文，不是压缩）。
#: 靠发言首行的 `· {batch}` 标记区分批次，不靠条数截断。

RULES = """## 评审规则

- 判据在下方「判据」一节。
- **通过＝调 `approve`**：调了就是通过，之后本批次你不再参与讨论。
  `approve` 的 `review_card_id` 填**评审卡 id**（下面「评审卡」一节给的就是它），
  **不是批次号**（`rv_xxx` 那个是批次，不是卡）。
- **不调 `approve` ＝ 不通过**，在发言里写明理由。
- 你可以调的工具：`read_card` / `read_file` / `list_files` / `search_files` / `read_shadow`。
  你调阅的结果会留在评审现场，后面发言的人直接看得到。
- 没有核实过的事实，按「未核实」处理。"""

#: 评审现场可用的查阅工具（用户 2026-10-27：每个角色都能调查阅工具，结果留在现场）
READ_TOOLS: tuple[str, ...] = (
    "read_card", "read_file", "list_files", "search_files", "read_shadow")


# ---------------------------------------------------------------- 事件：工具往返
# [评审窗口 2026-10-29 · 用户令「我要看的是详细事件的流式输出，所有事件，调起模型，
#  调工具，工具返回，开始发言」]
# 评审回合里的工具调用**不走 message_runner**（那里才是 tool_call/tool_result 的常规
# 产出点），所以窗口里看不到"它在调什么"。这里补两个小事件，格式与 message_runner
# 同族（tool / ok / 摘要），前端一套渲染吃两边。
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
    "open_review", "approve", "reject", "set_criteria",
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


def _review_read_tools(work_id: str, target: str,
                       cards: dict[str, dict[str, Any]]) -> list[str]:
    """评审能用的**查询类工具**：取子卡工具面里的只读项。

    [2026-10-30 用户裁定]「评审和子卡一样，知道事情的来龙去脉，可以调用所有查询的
    工具，进一步了解事实」——不是固定那 5 个，是**子卡能查的它都能查**。
    只读＝`ToolDef.readonly`，不按名字硬编码（新增查询工具自动跟上）。
    """
    from . import card_tools          # 延迟 import：card_tools 体量大且反向引用
    try:
        schemas = card_tools.build_tool_schemas(target, work_id, cards)
    except Exception as exc:  # noqa: BLE001 取不到就回落到基础几个
        logger.warning("[review] 取子卡工具面失败 %s: %s", target, exc)
        return list(READ_TOOLS)
    ro = {n for n, d in card_tools.TOOL_REGISTRY.items()
          if getattr(d, "readonly", False)}
    out = [str(x.get("name") or "") for x in schemas]
    return [n for n in out if n in ro]


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
    _INFLIGHT.add(review_id)
    try:
        return await _round_loop(work_id, review_id)
    finally:
        _INFLIGHT.discard(review_id)


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
    _RUNNING.add(key)
    try:
        return await _run_step(work_id, review_id)
    finally:
        _RUNNING.discard(key)


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

    「已通过的不再被叫」由 `status()` 保证：票只在**判据变了**或**被撤销**时失效，
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
                "criteria": int(st.get("criteria") or 1),
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
    它读到的是评审现场（判据 ＋ 未通过成员的原话），它写的是自己的草稿列。
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
    rounds = 0
    while True:
        rounds += 1
        # [2026-10-30 实跑抓到] 这里原来抄了 speak 的「再调起时收敛成 approve/reject」——
        # 改稿回合根本不许投那两票，于是**第 2 轮起工具面只剩两个它不能用的工具**，
        # 它拿不到 edit_requirements，只能空手回一句 ⇒ changed=False ⇒ 判「子卡不改」。
        # 第 1 轮给全；之后收敛成**只剩终态工具**——和 speak 收敛成 approve/reject
        # 一个做法：它要么交回来，要么继续用工具改。
        tools_now = tools if rounds == 1 else [
            t for t in tools if str((t.get("function") or t).get("name") or "") == terminal]
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
            # [2026-10-29 用户令] 工具调用写成标签（格式错）⇒ 不算终态，再调起它重试。
            # 只有"没有工具、也没有像工具标签的正文"才算它表态"不用改"。
            bad = _text_tool_tag(text, tools)
            if bad:
                convo.append({"role": "assistant", "content": text or "（本轮没有输出）"})
                convo.append({"role": "user", "content":
                              f"（你上面写的 `<{bad}>` 是正文标签，不是工具调用，"
                              f"**没有执行**。要用工具调用接口提交，参数按工具清单里的定义填。）"})
                continue
            # [2026-10-30 用户令] 出口是工具，不是"它不说话了"。
            convo.append({"role": "assistant", "content": text or "（本轮没有输出）"})
            convo.append({"role": "user", "content":
                          f"（这一轮还没结束。改完了就调 `{terminal}` 交回；"
                          f"没改完就继续用工具改。）"})
            continue
        follow = False
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
                follow = True
                continue

            if name in READ_TOOLS:
                key = name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True)
                if key in seen:
                    payload = "（同一个目标你已经查过了，结果同上；不要重复查）"
                    called.append({"tool": name, "ok": True, "cached": True})
                else:
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
                    seen[key] = payload
                    try:
                        await session_facade.append(work_id, review_id, {
                            "actor": "tool",
                            "content": f"〔{name} · {batch}〕\n{payload}",
                            "model": used,
                        })
                    except Exception:  # noqa: BLE001
                        pass
                convo.append({"role": "user",
                              "content": f"【工具结果 {name}】\n{payload}"})
                follow = True
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
                follow = True
                continue
            called.append({"tool": name, "ok": False,
                           "error": f"工具面里没有 {name}（改稿回合不投通过/不通过）"})
        if submitted:
            break
        if not follow:
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
            await session_facade.append(work_id, review_id, {
                "actor": "assistant",
                "content": f"〔{await _name_of(work_id, target)} · {batch}〕\n{text}",
                "model": used,
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
    """版本已被别处推进（改判据／撤销通过）后，起一轮重评。"""
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
    # cards 要先取（工具面按它的权限算），打标记在后面
    cards = card_store.iter_card_metas(work_id)
    names = ("approve", "reject") + tuple(_review_read_tools(work_id, target, cards))
    tools = [_schema_of(card_tools, n) for n in names if n in card_tools.TOOL_REGISTRY]
    # [2026-10-29 用户令「哪个 agent 不讲明白」] 工具清单写进提示词：
    # 名字／干什么／参数怎么填，全部从 schema 生成（不手写，不会与工具定义脱节）。
    sys_p = sys_p + "\n\n" + _tools_block(tools)
    model = _model_of(work_id, role_id)
    # [2026-10-29 用户令「评审卡应该能读所有卡的卡面，卡面才是唯一事实源」]
    # 评审席上的人（card_001 / fn_004 / fn_005）读全树：给本人打标记，
    # policy 见到标记就放开读作用域（只放开读，写仍走原规则）。
    _me = dict(cards.get(role_id) or {})
    _me["_review_seat"] = True
    cards[role_id] = _me
    # [实弹 2026-10-27] 评审角色是**职能**，职能不一定有 cards 行 ⇒ `cards.get(role)`
    # 为空 ⇒ `check_file_access(card_meta=None)` 恒 False ⇒ 查阅工具一调就
    # 「越权：无权访问 xxx」（实弹里模型连查 57 次全是被拒，才一直重试）。
    # 按职能的权限行补一份 meta 给它，让「看得见就调得动」。
    if role_id not in cards:
        try:
            from ..permissions import policy as _pol
            from ..storage import work_store as _ws
            root_id = _ws.work_card_id_of(work_id) or ""
            pc = str((cards.get(root_id) or {}).get("project_class") or "")
            if not pc and root_id:
                pc = str(((await card_store.read_card(work_id, root_id))
                          .get("meta") or {}).get("project_class") or "")
            cards[role_id] = {
                "card_id": role_id, "card_type": "function",
                "project_class": pc,
                "perms": _pol.perms_for(pc or "rd_project", "function"),
            }
        except Exception as exc:  # noqa: BLE001 补不上就退回原样（工具会报越权）
            logger.warning("[review] 职能权限补齐失败 role=%s: %s", role_id, exc)

    texts: list[str] = []
    thinks: list[str] = []
    called: list[dict[str, Any]] = []
    used = model
    seen: dict[str, str] = {}   # (工具名+参数) → 上次结果：同一目标不重复真查

    rounds = 0
    while True:
        rounds += 1
        # [2026-10-29 用户令] 「每一次必须调工具，要么不通过要么通过，否则就再调起它」
        # 再调起时**把工具面收敛成二选一**：只剩 approve / reject，不给查阅工具。
        # 这是结构（把可选动作收敛到终态），不是次数上限。
        tools_now = tools if rounds == 1 else [
            t for t in tools
            if str((t.get("function") or t).get("name") or "")
            in ("approve", "reject")]
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
        follow = False          # 有没有「查证类」工具 ⇒ 要不要再问一轮
        terminal = False        # 有没有终态（approve / reject）⇒ 这轮发言才算完
        for tc in tcs:
            # [2026-10-27 修·实弹抓到的死结] 网关返回的 tool_call 是**扁平**结构：
            #   {"name": "approve", "arguments": {...}}
            # 原实现按 OpenAI 嵌套形态取 `tc["function"]["name"]` ⇒ 恒为空串 ⇒
            # 每一次正确的 approve 都被判成"只允许 approve"而拒掉 ⇒ **全员通过
            # 永远不可能发生**。两种形态都认，arguments 也接受 dict 或 JSON 串。
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

            if name == "approve":
                args.setdefault("review_card_id", review_id)
                _emit_tool_call(work_id, review_id, role_id, name, args)
                try:
                    r = await card_tools._h_approve(work_id, role_id, args, cards)
                    called.append({"tool": name, "ok": bool(r.get("ok")),
                                   "error": str(r.get("error") or "")})
                except Exception as exc:  # noqa: BLE001
                    r = {"ok": False, "error": str(exc)}
                    called.append({"tool": name, "ok": False, "error": str(exc)})
                _emit_tool_result(work_id, review_id, role_id, name, r)
                terminal = True
                continue

            if name == "reject":
                _emit_tool_call(work_id, review_id, role_id, name, args)
                try:
                    r = await card_tools._h_reject(work_id, role_id, args, cards)
                    called.append({"tool": name, "ok": bool(r.get("ok")),
                                   "error": str(r.get("error") or "")})
                except Exception as exc:  # noqa: BLE001
                    r = {"ok": False, "error": str(exc)}
                    called.append({"tool": name, "ok": False, "error": str(exc)})
                _emit_tool_result(work_id, review_id, role_id, name, r)
                terminal = True
                continue

            if name in READ_TOOLS:
                # [实弹② 2026-10-27] 同一个 (工具, 参数) 只真查一次：实弹里一个角色
                # 连查 57 次（read_card 17 / list_files 17 / read_file 11 …），
                # 全是同一批目标反复读。重复调用直接回放上次结果，不再烧一次工具。
                key = name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True)
                if key in seen:
                    payload = "（同一个目标你已经查过了，结果同上；不要重复查，够了就表态）"
                    called.append({"tool": name, "ok": True, "cached": True})
                else:
                    _emit_tool_call(work_id, review_id, role_id, name, args)
                    try:
                        r = await card_tools.execute_tool(
                            work_id, role_id, name, args, cards)
                        payload = json.dumps(r, ensure_ascii=False)
                        called.append({"tool": name, "ok": bool(r.get("ok"))})
                    except Exception as exc:  # noqa: BLE001
                        payload = f"工具异常：{exc}"
                        r = {"ok": False, "error": str(exc)}
                        called.append({"tool": name, "ok": False, "error": str(exc)})
                    _emit_tool_result(work_id, review_id, role_id, name, r)
                    seen[key] = payload
                    # 结果留在现场（后续角色直接看到，不用各自重查）
                    try:
                        await session_facade.append(work_id, review_id, {
                            "actor": "tool",
                            "content": f"〔{name} · {batch}〕\n{payload}",
                            "model": used,
                        })
                    except Exception:  # noqa: BLE001
                        pass
                convo.append({"role": "user",
                              "content": f"【工具结果 {name}】\n{payload}"})
                follow = True
                continue

            called.append({"tool": name, "ok": False,
                           "error": "只允许 approve / reject 与查阅工具"})
        if terminal:
            break
        if follow:
            continue
        # [2026-10-29 用户令] 「每一次必须调工具，要么不通过要么通过，否则就再调起它」
        # 只说了话、既没表态也没查证 ⇒ 这一轮不算完：把它的发言和事实塞回去，再调一次。
        convo.append({"role": "assistant", "content": text or "（本轮没有输出）"})
        convo.append({"role": "user", "content":
                      "（本批次还没有你的表态：approve 和 reject 都没调；"
                      "接下来只接受这两个动作）"})

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
            await session_facade.append(work_id, review_id, {
                "actor": "assistant",
                # 首行带批次标记：`_prior_messages` 靠它只取本批次发言（P6）
                "content": f"〔{await _name_of(work_id, role_id)} · {batch}〕\n{text}",
                "model": used,
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
    system / prior / user 三件。评审**只是把 user 换成评审专用块**。

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

    ctx = _ca.build_ctx(
        work_id, target or role_id,
        history_source=prior_src,
        function_meta={"function_id": role_id, "function_name": name,
                       "charter": {"text": duty}})
    user_content = await _user_prompt(work_id, review_id, st)
    msg = _cr.compose_model_context(ctx, profile="viewpoint",
                                    user_content=user_content)
    sys_p = str(msg.get("system_prompt") or "").strip()
    sys_p = f"{sys_p}\n\n## 评审卡\nid = {review_id or '（未知）'}\n\n{RULES}"
    prior = list(msg.get("prior_messages") or [])
    return sys_p, prior, str(msg.get("user_prompt") or "")


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
    return (f"{base}\n\n"
            f"## 评审卡\nid = {review_id or '（未知）'}\n\n"
            f"{RULES}")


async def _user_prompt(work_id: str, review_id: str,
                       st: dict[str, Any]) -> str:
    """发起方填写（原样）＋子卡使命/现状 ＋ **待评审的草稿** ＋ 判据 ＋ 上一批次正稿。

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
    criteria = ""
    try:
        criteria = str((await card_store.read_card(
            work_id, review_id)).get("body", {}).get("requirements") or "")
    except Exception:  # noqa: BLE001
        pass
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
        "## 判据（评审卡第三块）\n" + (criteria or "（未写判据）"),
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
                  f"现在轮到你按意见修改。下面有判据、你的草稿、还没通过的成员说的话。\n\n"
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
    """改稿现场的上下文：判据 ＋ 当前草稿 ＋ **未通过成员的原话** ＋ 子卡三块。

    与 `_user_prompt` 的差别：评审成员看的是「子卡三块（原样）」，
    子卡看的是「自己现在的草稿 ＋ 别人到底要求改什么」——后者才是它动手的依据。
    """
    body: dict[str, Any] = {}
    try:
        body = (await card_store.read_card(work_id, target)).get("body") or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] revise 读子卡失败 %s: %s", target, exc)
    criteria = ""
    try:
        criteria = str((await card_store.read_card(
            work_id, review_id)).get("body", {}).get("requirements") or "")
    except Exception:  # noqa: BLE001
        pass
    said = await _pending_said(work_id, review_id,
                               str(st.get("batch") or ""), pending)
    draft = str((await _draft_of(work_id, target)) or "")
    return "\n\n".join([
        # [2026-10-30 实跑] 这一轮的**边界**：改稿是「一轮」，改完就交回评审，
        # 不是一直润色。改前没说，它 20 分钟改了 8 版还在改，三个成员一次都没
        # 重新评过。说的是"这一轮什么时候算完"，不是限制它用几次工具。
        "## 这一轮的范围\n"
        "你这一轮只做一件事：把上面这些还没通过的人提的意见改进去，改完就回一句"
        "「改了哪几处」交回评审。下一轮改不改、还要改什么，由他们重新评了再说。\n"
        "（工具用几次、先查什么后改什么，你自己定，不用报告过程。）",
        "## 判据\n" + (criteria or "（未写判据）"),
        "## 你现在的草稿（要改的就是这一份）\n" + (draft or "（草稿为空）"),
        "## 还没通过的成员说了什么（原话）\n" + said,
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


async def _pending_said(work_id: str, review_id: str, batch: str,
                        pending: list[str]) -> str:
    """本批次里**未通过成员**的发言原话（不含工具结果）。

    为什么不把全批次发言都给：已通过的人说的话是"当时为什么没过"，
    对子卡改稿是噪音；子卡要的是**还没点头的人**要求什么。
    """
    try:
        blocks = await session_facade.read(work_id, review_id) or []
        if isinstance(blocks, dict):          # 兼容旧形态
            blocks = blocks.get("blocks") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] revise 读会话失败 review=%s: %s", review_id, exc)
        return "（读不到意见，按判据自行核对）"

    names: list[tuple[str, str]] = []
    for p in pending:
        try:
            names.append((p, await _name_of(work_id, p)))
        except Exception:  # noqa: BLE001
            names.append((p, p))

    mark = f"· {batch}〕" if batch else ""
    out: list[str] = []
    for b in blocks:
        if str(b.get("actor") or "") != "assistant":
            continue                      # 只要成员发言，不要工具结果
        content = str(b.get("content") or "")
        if not content.strip():
            continue
        head, _, rest = content.partition("\n")
        if mark and mark not in head:
            continue                      # 上一批次的发言
        for pid, nm in names:
            if nm and nm in head:
                out.append(f"【{nm}】\n{rest.strip() or content}")
                break
    return "\n\n".join(out) or "（成员还没有留下书面意见，按判据自行核对）"


async def _prior_messages(work_id: str, review_id: str,
                          batch: str = "", target: str = "") -> list[dict[str, Any]]:
    """评审卡会话里**本批次**已有的发言（看得见别人说过什么）。

    [P6] 只按**批次标记**过滤，**不按条数截断**——本批次有几句就带几句。

    [2026-10-27 修] `session_facade.read` 返回的是 **list**，原实现按 dict 取
    `.get("blocks")` ⇒ AttributeError ⇒ 被 except 吞成空 ⇒ **别人说过的话从来没
    进过上下文**（"共用现场"这条是空的）。实弹跑时抓到的。
    """
    try:
        blocks = await session_facade.read(work_id, review_id) or []
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
        if mark:
            head = content.split("\n", 1)[0]
            if mark not in head:
                continue      # 上一批次的发言不带进来（只带它的正稿）
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
