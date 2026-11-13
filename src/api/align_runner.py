# -*- coding: utf-8 -*-
"""[对齐场 2026-11-11 · 用户令] 对齐卡的轮转驱动。

一场＝一个后台循环任务（``_spawn_tracked`` 挂在场卡上）：

  ① 开场（仅根卡发起）：开场席（根卡）说一次即退场——它不是轮转成员，
     之后不再被唤起；
  ② 轮转：按 participants 顺序轮流叫席——每个席位跑**一个普通卡回合**
     （act_as=自己、ctx_card=自己 ⇒ 上下文＝自己卡的上下文＋场内对话记录
     走 prior_extra 追加），发言落场会话、带 from=席卡；
  ③ 收场：成员自己调 `align_end` 宣布说完 ⇒ 全员 ended ⇒ 散场
     （卡 state=completed，一次性退役）；子卡发起时发起方就是轮转首位
     （「发起方先发言」）。

刹车（全是机械事实，不靠模型自觉）：
  - 场卡被停（halt）⇒ 循环退出；
  - 某席被停 ⇒ 这一轮跳过它（它能活过来下轮照样轮到）；
  - 一轮里**一个席位回合都没跑成**（锁冲突/模型挂/全被停）⇒ 连续
    _STALL_LIMIT 轮无进展就收场记 stalled，不在空转上烧钱；
  - turns 到 max_turns ⇒ 收场记 max_turns；
  - 场卡 completed/closed ⇒ 循环退出（散场幂等）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ..storage import align_store, card_store, session_facade
from . import control, event_stream

logger = logging.getLogger(__name__)

#: 无进展轮数上限——一轮里若一席回合都没跑成，连续这么些轮就收场。
_STALL_LIMIT = 3
#: 全员暂时都叫不动（全被停）时，两轮之间的歇气秒数。
_PARK_SLEEP_S = 30.0
#: 在场内跑着的对齐任务（防同场双驱）。按 (work,card) 记；runner 单写者
# 拓扑下场卡恒归同一进程，内存登记即可。
_RUNNING: set[tuple[str, str]] = set()


def running(work_id: str, align_id: str) -> bool:
    return (str(work_id), str(align_id)) in _RUNNING


def kick(work_id: str, align_id: str) -> bool:
    """唤起/接续一场的轮转驱动（幂等——在场即不重开）。

    [多进程拆分] 循环只在**场卡属主** runner 上跑（事件/seq 单空间，
    同 review_step 口径）：web 进程或非属主 runner 调到这里时，透明
    POST 到属主 ``/api/ctl/align_kick``——端点落地后属主＝本进程，
    递归调用直接走本地 spawn，不会再转。
    """
    try:
        from ..infra import procs as _procs
        if (_procs.is_web()
                or (_procs.is_runner()
                    and _procs.owner_index_of(work_id, align_id)
                        != _procs.runner_index())):
            import asyncio as _a
            _a.ensure_future(_procs.forward_ctl(
                work_id, align_id, "/ctl/align_kick",
                {"work_id": str(work_id),
                 "align_card_id": str(align_id)}))
            return True
    except Exception:  # noqa: BLE001 分片读不出⇒就地跑（旧行为）
        pass
    if running(work_id, align_id):
        return False
    try:
        from . import message_runner as _mr
        _mr._spawn_tracked(run_align(str(work_id), str(align_id)),
                           work_id=str(work_id), card_id=str(align_id))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[align] 场驱动唤起失败 card=%s: %s", align_id, exc)
        return False


# ---------------------------------------------------------------- 场内对话 → 席位历史
async def _dialogue_blocks(work_id: str, align_id: str) -> list[dict[str, Any]]:
    """场内对话记录 → 追加进席位历史的块（用户令：对话记录全量给，不收敛）。

    每席发言带 from=席卡 ⇒ 投影成「—— card_XXX ——」块；用户在场内的插话
    全留（同评审场口径「我说话它们看不见，那我为什么要说话」）；
    工具块/不带 from 的 assistant（场内务）丢。
    """
    try:
        blocks = await session_facade.read(work_id, align_id) or []
        if isinstance(blocks, dict):
            blocks = blocks.get("blocks") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[align] 读场会话失败 card=%s: %s", align_id, exc)
        return []
    out: list[dict[str, Any]] = []
    for b in blocks:
        content = str(b.get("content") or "")
        if not content.strip():
            continue
        actor = str(b.get("actor") or "")
        if actor == "assistant":
            seat = str(b.get("from") or "").strip()
            if seat:
                out.append({"role": "assistant",
                            "content": f"—— {seat} ——\n{content}"})
        elif actor != "tool":
            out.append({"role": "user", "content": content})
    return out


def _toolface(opener: bool = False) -> set[str]:
    """场授面＝全部只读件 ∪ {align_end}（开场席非成员，不给 align_end）。

    「席位自己的能力上限」由 build_tool_schemas(act_as) 收窄——这只是场的
    许可上限（同评审场口径：在场内读+发言+收场；写产物/建卡/通信不在场）。
    """
    from . import card_tools  # 延迟 import：card_tools 体量大且反向引用

    readonly = {n for n, d in card_tools.TOOL_REGISTRY.items()
                if getattr(d, "readonly", False)}
    return set(readonly) | (set() if opener else {"align_end"})


async def _name_of(work_id: str, card_id: str) -> str:
    try:
        c = await card_store.read_card(work_id, card_id)
        return str((c.get("meta") or {}).get("title") or card_id)
    except Exception:  # noqa: BLE001
        return str(card_id)


# ---------------------------------------------------------------- 席位回合
async def _seat_turn(work_id: str, align_id: str, seat: str,
                     st: dict[str, Any], *, opener: bool = False
                     ) -> dict[str, Any]:
    """一席＝**一个普通卡回合**：act_as=席、ctx_card=席（自己的上下文）。

    发言落场会话（执行锚＝场卡）、带 from=席；mandate 不落会话
    （append_user=False，它是场务不是用户的话——同评审场口径）。
    """
    from . import routes  # 延迟 import：routes 体量大且反向引用

    topic = str(st.get("topic") or "")
    parts = [str(p) for p in (st.get("participants") or [])]
    others = [p for p in parts if p != seat]
    name = await _name_of(work_id, seat)
    try:
        event_stream.emit(work_id, align_id, "align_speaking",
                          {"seat": seat, "name": name, "opener": opener})
    except Exception:  # noqa: BLE001
        pass

    if opener:
        mandate = (
            f"## 你是谁\n{name}（{seat}）——本场的**发起方/开场席**。\n"
            f"## 这一场\n你在对齐场 {align_id} 里开了对齐「{topic}」，"
            f"参与对齐的成员：{'、'.join(parts)}。\n"
            "## 这一轮\n说出你的开场发言：对齐的目的、背景、要交代的事实与"
            "要求——**说完这一次你不再参与**（之后由成员们自己轮转对齐，"
            "你不会再被这个场唤起）。你的发言直接落进场的对话记录，全员可见。")
    else:
        duty = (
            "## 这一轮的差事\n**轮到你发言**：读上面的「本场对话记录」，接着对齐"
            "发言（问、答、补事实、约分工都行——你的发言直接落进场的对话记录，"
            "其他成员下一轮看得到）。\n"
            "对齐到你这边说完了、没有还要谈的了，就调 `align_end`"
            f"（align_card_id={align_id}）宣布你这边结束——**全员都调了场才散**；"
            "调的时候在 `conclusion` 里留下你这边对齐出来的结论/要点"
            "（散场时各席结论会汇总回投进每个成员自己的卡会话）；"
            "还想继续谈就只发言不调，下一轮还轮得到你。")
        who = (f"## 你是谁\n{name}（{seat}）——对齐成员之一。\n"
               f"## 这一场\n你在对齐场 {align_id} 里对齐「{topic}」。"
               f"参与成员：{'、'.join(parts)}（除你外：{'、'.join(others) or '（无）'}）。\n"
               "你的上下文＝你自己卡的上下文＋本场的对话记录（贴在下方历史区）；"
               "场内说过的话不进你自己卡的会话——散场时**各席结论会汇总回投**"
               "到你的会话，完整记录留在场卡里（一次性场，退役可查）。")
        mandate = f"{who}\n{duty}"

    err = ""
    try:
        result = await routes._run_message(
            work_id, align_id, mandate,
            append_user=False,
            source={"kind": "align_turn", "seat": seat,
                    "opener": bool(opener)},
            source_kind="internal",
            act_as=seat, act_meta=None, ctx_card=seat,
            tool_allow=_toolface(opener),
            prior_extra=await _dialogue_blocks(work_id, align_id))
    except Exception as exc:  # noqa: BLE001 锁冲突/模型挂也是一步的事实
        result = {}
        err = f"{type(exc).__name__}: {exc}"
        logger.warning("[align] 席位回合异常 card=%s seat=%s: %s",
                       align_id, seat, err)
    if not err:
        if not isinstance(result, dict):
            err = (f"turn_http_{getattr(result, 'status_code', '')}"
                   or "turn_non_dict_result")
        elif result.get("error"):
            err = str((result.get("error") or {}).get("message")
                      or result.get("error"))
    # 场卡锁被占（用户在场内说话的回合在跑/另一席位回合未结）——
    # 是「场忙」不是「场死」：主循环按此标记不算空转、稍后重试。
    busy = bool(err) and ("占用" in err or "423" in err
                          or "CardLocked" in err or "locked" in err)
    return {"seat": seat, "ok": not err, "error": err, "busy": busy}


# ---------------------------------------------------------------- 散场结论回投
async def deliver_conclusions(work_id: str, align_id: str) -> int:
    """散场时把「话题＋各席结论」落进**每个成员**自己的卡会话（actor=system）。

    [用户裁定 2026-11-11] 各席调 `align_end` 时可顺手留一句自己的结论；
    回投只给轮转成员——根卡开场席说了开场即退场，不投（它不醒来读，
    落了也是给下次它自己看的东西，裁定为不投）。

    幂等：`claim_conclusion` 原子占位，三个收口路径＋handler 谁先抢到谁投。
    场内对话本会留在场卡会话（退役卡仍可查）；成员自己的会话只收这一块
    汇总——它在场内的发言不进自己卡，这一块就是它带走的全部。
    返回投出的成员数。
    """
    st = await align_store.state_of(work_id, align_id)
    if not st:
        return 0
    if not await align_store.claim_conclusion(work_id, align_id):
        return 0
    parts = [str(p) for p in (st.get("participants") or [])]
    concl = {str(k): str(v) for k, v
             in (st.get("conclusions") or {}).items()}
    topic = str(st.get("topic") or "")
    reason = str(st.get("close_reason") or "all_ended")
    lines = [f"〔对齐场散场 · 结论回投〕{align_id} · 话题：{topic}"]
    if reason != "all_ended":
        lines.append(f"（散场原因：{reason}——可能不是全员都给了结论）")
    for p in parts:
        name = await _name_of(work_id, p)
        c = concl.get(p)
        lines.append(f"— {name}（{p}）：{c}" if c
                     else f"— {name}（{p}）：（未留结论）")
    lines.append(f"完整对话记录在对齐卡 {align_id} 里（场是一次性的，已退役可查）。")
    text = "\n".join(lines)

    # 场卡自己也落一块收尾（卡面记录完整——结论是谁的、场怎么散的）。
    try:
        await session_facade.append(work_id, align_id,
                                    {"actor": "system", "content": text})
    except Exception as exc:  # noqa: BLE001
        logger.warning("[align] 结论块落场卡失败 card=%s: %s", align_id, exc)

    sent = 0
    for p in parts:
        try:
            await session_facade.append(
                work_id, p, {"actor": "system", "content": text})
            sent += 1
        except Exception as exc:  # noqa: BLE001 一席投不到不挡别人
            logger.warning("[align] 结论回投失败 card=%s seat=%s: %s",
                           align_id, p, exc)
    return sent


async def _close_field(work_id: str, align_id: str, reason: str,
                       **extra) -> None:
    """散场收口：置 closed＋回投结论＋发 align_closed。各收口路径同一序列。"""
    await align_store.close(work_id, align_id, reason=reason)
    await deliver_conclusions(work_id, align_id)
    st = await align_store.state_of(work_id, align_id)
    event_stream.emit(work_id, align_id, "align_closed", {
        "reason": reason, **extra,
        "conclusions": (st or {}).get("conclusions") or {}})


# ---------------------------------------------------------------- 轮转主循环
async def run_align(work_id: str, align_id: str) -> None:
    """场驱动主循环：开场席（若有）→ 成员轮流发言 → 全员 align_end 散场。"""
    key = (str(work_id), str(align_id))
    if key in _RUNNING:
        return
    _RUNNING.add(key)
    stalled_laps = 0
    try:
        st = await align_store.state_of(work_id, align_id)
        if not st or str(st.get("status") or "") != "open":
            logger.info("[align] 场非 open 不启动 card=%s status=%s",
                        align_id, st.get("status"))
            return
        parts = [str(p) for p in (st.get("participants") or []) if str(p)]
        opener = str(st.get("opener") or "")
        max_turns = int(st.get("max_turns") or align_store.DEFAULT_MAX_TURNS)
        event_stream.emit(work_id, align_id, "align_open", {
            "topic": st.get("topic"), "participants": parts,
            "initiator": st.get("initiator"), "opener": opener})

        # ① 开场席（根卡发起才有；它不在 participants 里 ⇒ 说一次即退场）
        if opener and opener not in parts:
            await _seat_turn(work_id, align_id, opener, st, opener=True)
            await align_store.bump_turn(work_id, align_id)

        # ② 轮转：按序叫未结束的成员，全员 ended ⇒ 散场
        while True:
            st = await align_store.state_of(work_id, align_id)
            if str(st.get("status") or "") != "open":
                return
            if control.is_halted(work_id, align_id):
                # [halted 收口 2026-11-15] 场被停不能裸退——裸退留 open 尸，
                # 场卡永远是 active/discussing、结论永不回投（card_029 实陷）。
                # 停＝用户判这场死刑，机械收场退役，已登记的结论照常回投。
                await _close_field(work_id, align_id, "halted",
                                   turns=int(st.get("turns") or 0))
                return
            ended = {str(x) for x in (st.get("ended") or [])}
            remaining = [p for p in parts if p not in ended]
            if not remaining:
                break
            turns = int(st.get("turns") or 0)
            if turns >= max_turns:
                await _close_field(work_id, align_id,
                                   f"max_turns:{max_turns}", turns=turns)
                return

            lap_ran = 0
            for seat in remaining:
                st = await align_store.state_of(work_id, align_id)
                if str(st.get("status") or "") != "open":
                    return
                if control.is_halted(work_id, align_id):
                    await _close_field(work_id, align_id, "halted",
                                       turns=int(st.get("turns") or 0))
                    return
                if str(seat) in {str(x) for x in (st.get("ended") or [])}:
                    continue
                if control.is_halted(work_id, seat):
                    logger.info("[align] 席位被停跳过 seat=%s card=%s",
                                seat, align_id)
                    continue
                res = await _seat_turn(work_id, align_id, seat, st)
                await align_store.bump_turn(work_id, align_id)
                if res.get("ok"):
                    lap_ran += 1

            # 一轮实绩评估：一个回合都没跑成＝空转一轮
            if lap_ran == 0:
                stalled_laps += 1
                logger.warning("[align] 空转轮 %s/%s card=%s",
                               stalled_laps, _STALL_LIMIT, align_id)
                if stalled_laps >= _STALL_LIMIT:
                    await _close_field(work_id, align_id, "stalled")
                    return
                # 全叫不动（多被停）时歇口气再试，不烧空轮转
                await asyncio.sleep(_PARK_SLEEP_S)
            else:
                stalled_laps = 0

        # ③ 全员 align_end ⇒ 散场（幂等；align_end handler 里已先收过一轮）
        if await align_store.close(work_id, align_id, reason="all_ended"):
            await deliver_conclusions(work_id, align_id)
            event_stream.emit(work_id, align_id, "align_closed",
                              {"reason": "all_ended",
                               "conclusions":
                                   (await align_store.state_of(
                                       work_id, align_id))
                                   .get("conclusions") or {}})
            logger.info("[align] 散场 card=%s", align_id)
    finally:
        _RUNNING.discard(key)
