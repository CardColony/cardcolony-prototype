# -*- coding: utf-8 -*-
"""伴生评审卡存储（[伴生评审卡 2026-10-27]）。

一张 `sub_project` / `topic` 卡 = 一张**评审卡**：一对一、离树（`off_tree`）、
不设 `parent`、随子卡归档。评审卡上跑"批次"：每个阶段收口开一个批次，
全员 `approve` 才收口。

存储分工（P1：卡面是唯一权威事实源）
- **卡面**：当前批次与通过状态在现状块（系统渲染）
- **cards 列**：`review_of`（配对的子卡）、`review_state`（当前批次 JSON）
- **review_approvals 表**：每次 approve 一行
- **事件流**：`review_batch_opened` / `review_batch_closed` 留痕

**成员只属于一次批次邀请**：从职能表取出的邀请名单保存在当前 `review_state` 批次里；不写入 `relations`，批次关闭时清空。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Optional

from . import card_db, card_store

logger = logging.getLogger("storage.review")

#: 评审卡类型码（注册表已登记：off_tree / 不可有子卡 / 无模板）
REVIEW_TYPE = "review"

MISSION_TPL = (
    "你是「{title}（{cid}）」评审场的**场务**："
    "评审席在这里评它的需求文档草稿，批次与各席位的票在本卡现状块。\n"
    "用户在这里说话时，你答**流程问题**——评审到哪一步、谁说过什么、"
    "还差谁表态。你**不占席位、不评内容、无表态权**："
    "内容意见留在现场给评审席看，别替他们评。"
)


# ---------------------------------------------------------------- 卡
async def review_card_of(work_id: str, child_id: str) -> str:
    """子卡的伴生评审卡 id（没有则空串）。"""

    def _fn(conn: Any) -> str:
        row = conn.execute(
            "SELECT card_id FROM cards WHERE work_id=? AND review_of=?"
            " AND card_type=? LIMIT 1",
            (work_id, child_id, REVIEW_TYPE),
        ).fetchone()
        return str(row["card_id"]) if row else ""

    try:
        return str(card_db.read_sync(work_id, _fn, "") or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 查伴生卡失败 child=%s: %s", child_id, exc)
        return ""


async def ensure_review_card(work_id: str, child_id: str,
                             child_title: str) -> str:
    """建/确认子卡的伴生评审卡（幂等）。失败只记日志，绝不阻断建子卡。"""
    try:
        got = await review_card_of(work_id, child_id)
        if got:
            return got
        body = ("## 使命\n"
                + MISSION_TPL.format(title=child_title, cid=child_id)
                + "\n\n## 现状\n\n\n## 需求文档\n")
        r = await card_store.create_card(
            work_id, f"评审 · {child_title}", "系统派生",
            card_type=REVIEW_TYPE, parent="", charter=body, skip_kick=True)
        rid = str((r.get("meta") or {}).get("card_id") or "")
        if not rid:
            logger.warning("[review] 伴生卡建卡失败 child=%s: %s", child_id, r)
            return ""
        # 专用 review_of 配对；评审邀请不构成卡间关系线。
        await _write_pair(work_id, rid, child_id)
        # [评审流量治理 2026-11-02] 新评审卡入场：流量判据集合立即失效重建，
        # 否则 60s 缓存窗口内它的消息会被当普通卡间流量。
        try:
            from . import review_scope
            review_scope.invalidate_cache(work_id)
        except Exception:  # noqa: BLE001
            pass
        return rid
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 伴生卡派生异常 child=%s: %s", child_id, exc)
        return ""


async def _write_pair(work_id: str, review_id: str, child_id: str) -> None:
    """写专用 review_of 配对列；评审关系不是依赖/影响线。"""

    def _fn(conn: Any) -> None:
        conn.execute("UPDATE cards SET review_of=? WHERE card_id=?",
                     (child_id, review_id))

    try:
        await card_db.run_write(work_id, _fn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 配对写入失败 review=%s child=%s: %s",
                       review_id, child_id, exc)


# ---------------------------------------------------------------- 临时邀请成员
async def members_of(work_id: str, review_id: str) -> list[str]:
    """当前批次被邀请的职能名单；无进行中批次时为空，不代表持久关系。"""
    st = await state_of(work_id, review_id)
    if not str(st.get("batch") or ""):
        return []
    return [str(x) for x in (st.get("members") or []) if str(x)]


# ---------------------------------------------------------------- 批次
def _new_batch_id() -> str:
    return "rv_" + uuid.uuid4().hex[:10]


async def list_active(work_id: str) -> list[dict[str, Any]]:
    """本 work 里**有进行中批次**的评审卡（[评审入口 2026-10-29] 左栏临时出入口）。

    用户口径：「左侧列表页临时出入口」——评审卡本身不进卡列表（离树、靠
    `review_of` 配对），但**有评审在跑的时候**用户要能从列表直接进去。
    所以只返回 `review_state.batch` 非空的：收口后 batch 被清空 ⇒ 入口自然消失，
    不需要任何"关闭入口"的动作。
    """
    def _fn(conn: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in conn.execute(
                "SELECT card_id, review_of, review_state FROM cards"
                " WHERE work_id=? AND card_type=?", (work_id, REVIEW_TYPE)):
            try:
                st = json.loads(r["review_state"] or "{}")
            except (TypeError, ValueError):
                st = {}
            if not isinstance(st, dict) or not str(st.get("batch") or ""):
                continue
            out.append({
                "review_card_id": str(r["card_id"]),
                "target": str(r["review_of"] or ""),
                "batch": str(st.get("batch") or ""),
                "draft": int(st.get("draft") or 1),
            })
        return out

    try:
        return list(await card_db.run_read(work_id, _fn, []) or [])
    except Exception as exc:  # noqa: BLE001 读不到＝没有入口，不影响左栏
        logger.warning("[review] 列进行中批次失败 work=%s: %s", work_id, exc)
        return []


async def state_of(work_id: str, review_id: str, *,
                   strict: bool = False) -> dict[str, Any]:
    """评审卡当前批次状态。strict 模式区分无批次与读取失败。"""

    def _fn(conn: Any) -> dict[str, Any]:
        row = conn.execute("SELECT review_state FROM cards WHERE card_id=?",
                           (review_id,)).fetchone()
        raw = (row["review_state"] if row else "") or ""
        try:
            d = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            if strict:
                raise
            d = {}
        if strict and not isinstance(d, dict):
            raise ValueError("review_state must be an object")
        return d if isinstance(d, dict) else {}

    try:
        state = await card_db.run_read(work_id, _fn)
        if state is None and strict:
            raise RuntimeError("review_state database unavailable")
        return state or {}
    except Exception:  # noqa: BLE001
        if strict:
            raise
        return {}


async def open_batch(work_id: str, review_id: str, focus: str,
                     members: Optional[list[str]] = None,
                     target: str = "", brief: str = "",
                     batch_type: str = "requirements",
                     next_members: Optional[list[str]] = None, *,
                     expect_batch: str = "") -> dict[str, Any]:
    """在评审卡属主上归档旧会话，发布新批次与空白会话。"""
    from ..infra import procs
    from . import session_facade, session_store

    if (procs.is_web() or (procs.is_runner()
            and procs.owner_index_of(work_id, review_id) != procs.runner_index())):
        result = await procs.forward_ctl(
            work_id, review_id, "/ctl/review_open_batch",
            {"work_id": work_id, "review_id": review_id, "focus": focus,
             "members": members, "target": target, "brief": brief,
             "batch_type": batch_type, "next_members": next_members,
             "expect_batch": expect_batch})
        if not result.get("batch"):
            raise RuntimeError(f"评审属主开批失败: {result.get('error') or result}")
        return result

    async with session_store._session_lock(work_id, review_id):
        path = session_facade._session_path(work_id, review_id)
        previous, _, blocks = await session_facade._read_existing_locked(work_id, review_id, path)
        return await _open_batch_locked(
            work_id, review_id, focus, members, target, brief,
            batch_type, next_members, expect_batch, previous, bool(blocks))


async def _open_batch_locked(work_id: str, review_id: str, focus: str,
                             members: Optional[list[str]], target: str, brief: str,
                             batch_type: str, next_members: Optional[list[str]],
                             expect_batch: str, previous: str,
                             has_blocks: bool) -> dict[str, Any]:
    """开一个批次：写 review_state（含本批临时邀请名单），不建立卡间关系线。

    [P6 换上下文不压缩] 上一批次的正稿**原文**带进来（`prev_verdict`），
    过程（发言、工具结果）不带——它们留在会话里归档。

    [草稿＝子卡卡面] 不另存草稿：`draft_hash` 记子卡当时的 `content_hash`。
    子卡改卡面 ⇒ hash 变 ⇒ 全员重评（P3：通过绑草稿版本）。

    `brief`＝发起方填写的**任务背景／当前状况／要评审的内容**，**原样**进上下文
    （不截断，P6）。
    """
    from . import session_facade

    old = await state_of(work_id, review_id, strict=True)
    if expect_batch and old.get("batch") != expect_batch:
        raise RuntimeError("评审批次已变化")
    prev = str(old.get("verdict") or old.get("prev_verdict") or "")
    tgt = str(target or old.get("target") or "")
    invited = list(dict.fromkeys(str(m) for m in (members or []) if str(m)))
    st = {"batch": _new_batch_id(), "draft": 1,
          "focus": str(focus or ""), "opened_at": _now(),
          "prev_verdict": prev, "target": tgt,
          "brief": str(brief or ""),
          "draft_hash": "",
          # [venue] 场型：本批是哪种场（需求评审 requirements／将来的验收 acceptance）。
          # 只留字段不建分派表——第二种场出现时按它分发席位集/mandate。
          "type": str(batch_type or "requirements"),
          # [venue] 流程计数器：asked_seq=已向正卡要过第几次回应；
          # responded_seq=正卡已交回几次（submit_revision）。相等＝无未收回应。
          "asked_seq": 0, "responded_seq": 0,
          # [turn_end 激活 2026-11-04] 「这轮干了活没有」的水位基线：
          # report_target_turn_end 拿 draft/responded_seq 跟这两个值比——
          # 涨了＝刚结束的这轮有实质推进（草稿改过或交回过）。
          "turn_end_draft": 1, "turn_end_responded": 0,
          # [venue] 席位机械状态：{role: {"response_seen": n}}——
          # 否决席被叫时看到的正卡回应水位，re_eval 只在有新回应时才重叫。
          "seats": {},
          # [两轮评审 2026-11-14] 对齐批带下一轮的既定名单：本批通过时
          # _advance 照此开正式批，不再走点名/默认决议（名单在开批时已过滤）。
          "next_members": [str(m) for m in (next_members or []) if str(m)],
          # [两轮评审 2026-11-14] 上一批号随新批带走（审计用）——对齐批
          # 晋级时这里留的是刚收口的对齐批，语义同 prev_verdict 之于正稿。
          "closed_batch": str(old.get("batch") or
                              old.get("closed_batch") or ""),
          # 邀请名单只在这次进行中的批次有效；它不是 relations，也不是职能的永久成员关系。
          "members": invited}

    def _fn(conn: Any, archive_id: str) -> None:
        current = _state_in_txn(conn, review_id)
        if (current.get("batch"), current.get("closed_batch")) != (
                old.get("batch"), old.get("closed_batch")):
            raise RuntimeError("评审批次已变化")
        row = conn.execute("SELECT requirements_draft FROM cards WHERE card_id=?",
                           (tgt,)).fetchone()
        if row:
            st["draft_hash"] = hashlib.sha1(
                str(row["requirements_draft"] or "").encode("utf-8")).hexdigest()
        # 带走原文账（含旧版本可能只有 SQLite 副本的行），新批活跃板从空开始。
        conn.execute("UPDATE session_blocks SET card_id=? WHERE card_id=?",
                     (archive_id, review_id))
        conn.execute("DELETE FROM context_board WHERE card_id=?", (review_id,))
        conn.execute("UPDATE cards SET review_state=? WHERE card_id=?",
                     (json.dumps(st, ensure_ascii=False), review_id))
        # [2026-10-31 用户裁定「每一次调新批次，旧批次的所有内容，都应该清干净，
        #  每一次评审都是全新的开始」]
        # 只删**本评审卡**旧批次的审批票（review_approvals 没有 card_id 列 ⇒
        # 从旧 state 取 batch/closed_batch，精确限定范围，不误伤同 work 其他评审卡）。
        _old_batches = {str(old.get("batch") or ""), str(old.get("closed_batch") or "")}
        _old_batches.discard("")
        for _ob in _old_batches:
            conn.execute("DELETE FROM review_approvals WHERE work_id=? AND batch_id=?",
                         (work_id, _ob))

    old_batch = str(old.get("batch") or old.get("closed_batch") or "pre")
    await session_facade.publish_review_session_locked(
        work_id, review_id, previous=previous,
        replacement=session_facade._render_v2_header(session_facade._default_header(review_id)),
        archive_id=f"{review_id}-{old_batch}", archive_text=previous if has_blocks else "",
        update=_fn)

    # [2026-10-31 用户裁定] 新批次＝**新开会话**，旧内容不删——
    # 归档成「评审卡-旧批次」的独立会话文件留在磁盘（可审计、可回溯），
    # 场会话从空白重新起。上一批次的**正稿**通过 review_state.prev_verdict
    # 带过来（P6：换上下文不压缩），那是唯一要带的东西。
    # [venue 批次守卫] 在途旧批回合先作废再归档：席位回合=评审卡上的普通
    # 回合（占回合锁）。跨协程直接 cancel——停不掉的是"同协程"（调用方自己
    # 就在这个回合里），那种由 _seat_turn 收尾时发现批次已换、再清一次场。
    try:
        if session_facade.turn_holder(work_id, review_id):
            from ..api import control as _ctl
            _ctl.cancel_turn(work_id, review_id)
    except Exception as exc:  # noqa: BLE001 停不掉不阻断开批
        logger.warning("[review] 在途旧批回合作废失败（不阻断开批）review=%s: %s",
                       review_id, exc)
    return st


async def sync_draft(work_id: str, review_id: str) -> bool:
    """正卡回合收口 ⇒ 草稿指纹变了就**版本 +1**（返回是否变了）。

    [P3 通过绑版本] 版本一变，`review_approvals` 里旧 `draft_ver` 全部失效，
    不需要逐个 revoke——失效是**版本比较的结果**，不是一次批量改写。

    [轮粒度 2026-11-14] 唯一调用点是回合收口的 report_target_turn_end——
    写钩不再随写推版，轮内 N 次编辑只落一版。
    只在 `content_hash` 真变时推进；没有进行中批次时不动（返回 False）。
    """
    st = await state_of(work_id, review_id)
    if not str(st.get("batch") or ""):
        return False
    tgt = str(st.get("target") or "")
    if not tgt:
        return False
    cur = await _card_hash(work_id, tgt)
    base = str(st.get("draft_hash") or "")
    if not cur or cur == base:
        return False
    st2 = dict(st)
    st2["draft"] = int(st.get("draft") or 1) + 1
    st2["draft_hash"] = cur
    st2["draft_changed_at"] = _now()

    def _fn(conn: Any) -> None:
        conn.execute("UPDATE cards SET review_state=? WHERE card_id=?",
                     (json.dumps(st2, ensure_ascii=False), review_id))

    await card_db.run_write(work_id, _fn)
    return True


async def _card_hash(work_id: str, card_id: str) -> str:
    """子卡**需求文档草稿**的指纹（草稿版本的判据）。读不到返回空串。

    [2026-10-27 修] 原实现读 `cards.content_hash` 列——**该列在 SQLite 库里
    根本不存在**（那是卡面 markdown 镜像的字段），异常被吞成空串 ⇒ 草稿版本
    永远不推进 ⇒ 「改草稿全员重评」形同未接。改为直接取正文指纹。

    [2026-10-29 用户口径「评审是一边评审一边改草稿的链路」] **只算
    `requirements_draft`**，不再算 mission / status_block / requirements：
    评审对象就是草稿，其余三块不是评审材料。
    改前把现状块也算进去 ⇒ 子卡在评审期间写一句「评审中」就把 draft 顶到 2、
    全员票作废、所有人被重新唤起——work_027 实跑就是被这一条打回原点的
    （14:11 子卡 edit_status → 14:16 全员重跑）。
    """
    if not card_id:
        return ""

    def _fn(conn: Any) -> str:
        row = conn.execute(
            "SELECT requirements_draft FROM cards WHERE card_id=?",
            (card_id,)).fetchone()
        if not row:
            return ""
        raw = str(row["requirements_draft"] or "")
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    try:
        return str(await card_db.run_read(work_id, _fn, "") or "")
    except Exception:  # noqa: BLE001
        return ""


async def close_batch(work_id: str, review_id: str,
                      verdict: str) -> dict[str, Any]:
    """收口：批次号清空、正稿落 `verdict`（下一批次读它，P6）。"""
    st = await state_of(work_id, review_id)
    batch = str(st.get("batch") or "")
    if not batch:
        return {}
    st2 = dict(st)
    st2["batch"] = ""
    # 邀请只在当前批次有效；收口后成员资格结束。
    st2["members"] = []
    st2["closed_batch"] = batch
    st2["closed_at"] = _now()
    st2["verdict"] = str(verdict or "")

    def _fn(conn: Any) -> None:
        conn.execute("UPDATE cards SET review_state=? WHERE card_id=?",
                     (json.dumps(st2, ensure_ascii=False), review_id))

    await card_db.run_write(work_id, _fn)
    return st2


async def mark_spoken(work_id: str, review_id: str, role_id: str,
                      *, draft: int | None = None) -> list[str]:
    """记「本轮这个成员发过言了」，返回本轮已发言的名单。

    [2026-10-30 用户口径] 一轮＝**全员发完言，再改一次稿**；不是「一人发言就改稿」。
    所以需要知道"本轮谁已经说过"——一轮的边界就是**草稿版本**：草稿一变，
    名单自动清空，下一轮从头再问。

    ``draft`` 显式给值＝**这次发言所属的稿次**（席位回合按起叫时刻的
    快照落账——回合中稿次推进了，这次发言仍属旧轮，不冒充新轮的发言）；
    库里 spoken 已记到更新的稿次 ⇒ 本记属旧轮残账，不落（新轮名单是权威）。
    """
    st = await state_of(work_id, review_id)
    if not st.get("batch"):
        return []
    cur_draft = int(st.get("draft") or 1)
    draft = cur_draft if draft is None else int(draft)
    sp = st.get("spoken") or {}
    if int(sp.get("draft") or -1) > draft:
        return [str(x) for x in (sp.get("roles") or [])]
    roles = [str(x) for x in (sp.get("roles") or [])] \
        if int(sp.get("draft") or -1) == draft else []
    if role_id and role_id not in roles:
        roles.append(role_id)
    st2 = dict(st)
    st2["spoken"] = {"draft": draft, "roles": roles}

    def _fn(conn: Any) -> None:
        conn.execute("UPDATE cards SET review_state=? WHERE card_id=?",
                     (json.dumps(st2, ensure_ascii=False), review_id))

    await card_db.run_write(work_id, _fn)
    return roles


async def spoken_round(work_id: str, review_id: str) -> list[str]:
    """本轮已发言的成员（草稿版本变了就自动作废，返回空）。"""
    return spoken_at_draft(await state_of(work_id, review_id))


def spoken_at_draft(st: dict[str, Any]) -> list[str]:
    sp = st.get("spoken") or {}
    if int(sp.get("draft") or -1) != int(st.get("draft") or 1):
        return []
    return [str(x) for x in (sp.get("roles") or [])]




# ---------------------------------------------------------------- 票
async def _draft_text(work_id: str, card_id: str) -> str:
    """子卡当前 `requirements_draft` 原文（票的 draft_snapshot 原料）。"""
    if not card_id:
        return ""

    def _fn(conn: Any) -> str:
        row = conn.execute(
            "SELECT requirements_draft FROM cards WHERE card_id=?",
            (card_id,)).fetchone()
        return str(row["requirements_draft"] or "") if row else ""

    try:
        return str(await card_db.run_read(work_id, _fn, "") or "")
    except Exception:  # noqa: BLE001
        return ""


async def _vote(work_id: str, review_id: str, role_id: str,
                verdict: str, *, expect: Any = None) -> dict[str, Any]:
    """落一票：同席位同批次**最近一票为准**（upsert 覆盖 verdict/版本/快照/时间，
    撤销位清 0——投新票自然复活，不用单独先撤销）。

    [venue 真票 2026-10-31] reject 是真票：先通过又不通过＝不通过；
    改主意＝再投一次，不是撤销。
    `draft_snapshot`＝这一票所批的草稿原文：stale 判据与 drift「只评增量」
    diff 全靠它（机器比对的是文本，不是版本号）。

    [批三·材料绑定 2026-11-20] ``expect``＝席位回合的材料身份
    ``{batch, draft, draft_fp}``——**写事务内**复核：批次换过、稿次
    推进过、或同稿次正文变了（中段改稿不推版本）⇒ 拒落返回
    ``stale``——旧材料的票落不到新稿新批次。无 expect＝现行口径
    （普通调用方不变）。
    """
    st = await state_of(work_id, review_id)
    if not str(st.get("batch") or ""):
        return {"ok": False, "error": "当前没有进行中的批次"}

    def _fn(conn: Any) -> Any:
        # [批三返修② · 同一事务读数] 批次/稿次/被评原文**全部在写事务里
        # 读**：核验用的正文就是落票写快照的那份——事务外预读会留下
        # 「核验过 A、快照存 B」的对不上窗口（草稿在两次读之间换版）。
        cur = _state_in_txn(conn, review_id)
        batch = str(cur.get("batch") or "")
        if not batch:
            return {"stale": "batch"}
        # [批三返修③] 成员资格进**同一写事务**：外层 _vote_gate 只在调用
        # 前核过旧批名单——换批后事务内读到的是新批 members，新批没有
        # 这席 ⇒ 拒落（旧批的票写不进新批的票表）。
        if role_id not in [str(m) for m in (cur.get("members") or [])]:
            return {"denied": "not_member", "batch": batch}
        draft_ver = int(cur.get("draft") or 1)
        row2 = conn.execute(
            "SELECT requirements_draft FROM cards WHERE card_id=?",
            (str(cur.get("target") or ""),)).fetchone()
        snapshot = str(row2["requirements_draft"] or "") if row2 else ""
        if isinstance(expect, dict) and expect:
            if batch != str(expect.get("batch") or ""):
                return {"stale": "batch"}
            if draft_ver != int(expect.get("draft") or 0):
                return {"stale": "draft"}
            exp_fp = str(expect.get("draft_fp") or "")
            if exp_fp and hashlib.sha1(
                    snapshot.encode("utf-8")).hexdigest() != exp_fp:
                return {"stale": "draft_fp"}
        previous = conn.execute(
            "SELECT verdict, draft_ver, revoked FROM review_approvals"
            " WHERE work_id=? AND batch_id=? AND role_id=?",
            (work_id, batch, role_id)).fetchone()
        conn.execute(
            "INSERT INTO review_approvals"
            " (work_id,batch_id,role_id,draft_ver,ts,revoked,verdict,draft_snapshot)"
            " VALUES (?,?,?,?,?,0,?,?)"
            " ON CONFLICT(work_id,batch_id,role_id) DO UPDATE SET"
            " draft_ver=excluded.draft_ver,"
            " ts=excluded.ts, revoked=0,"
            " verdict=excluded.verdict,"
            " draft_snapshot=excluded.draft_snapshot",
            (work_id, batch, role_id, draft_ver, _now(), verdict, snapshot))
        # 新票落账＝新事实——尤其新否决，它就是要求重派的事件：把瞬时
        # 故障攒下的 dispatch_fails 清位，封顶不做单向棘轮
        # （[解棘轮 2026-11-15] 否则封顶后席位再怎么否也派不出去）。
        if int(cur.get("dispatch_fails") or 0):
            st2 = dict(cur)
            st2["dispatch_fails"] = 0
            conn.execute("UPDATE cards SET review_state=? WHERE card_id=?",
                         (json.dumps(st2, ensure_ascii=False), review_id))
        return {"batch": batch, "draft": draft_ver,
                "changed": previous is None or bool(previous["revoked"]) or (
                    previous["verdict"] or "approve",
                    previous["draft_ver"]) != (verdict, draft_ver)}

    r = await card_db.run_write(work_id, _fn)
    if isinstance(r, dict) and r.get("denied"):
        return {"ok": False, "error": "not_member",
                "batch": str(r.get("batch") or st.get("batch") or ""),
                "role": role_id}
    if isinstance(r, dict) and r.get("stale"):
        return {"ok": False, "error": "stale_material",
                "stale": str(r["stale"]),
                "batch": str(st.get("batch") or ""), "role": role_id}
    return {"ok": True, "batch": str((r or {}).get("batch") or ""),
            "role": role_id, "draft": int((r or {}).get("draft") or 0),
            "verdict": verdict, "changed": bool((r or {}).get("changed"))}


async def approve(work_id: str, review_id: str, role_id: str,
                  *, expect: Any = None) -> dict[str, Any]:
    """记录一次通过。"""
    return await _vote(work_id, review_id, role_id, "approve", expect=expect)


async def reject(work_id: str, review_id: str, role_id: str,
                 *, expect: Any = None) -> dict[str, Any]:
    """记录一次不通过（真票，与 approve 同一个 upsert 键）。

    [2026-10-31 用户裁定] 不设机械僵局检测——上下文（评审现场发言＋正卡
    回应）给到位，席位与正卡自然收敛；收敛与否是模型的判断，不归机制数。
    """
    return await _vote(work_id, review_id, role_id, "reject", expect=expect)


# ---------------------------------------------------------------- 回应计数器
def _expect_matches(st: dict, expect: Any) -> bool:
    """[派活写回核对 2026-11-20 · 批次A第五轮返工] 写事务内比对当前
    状态与快照登记的 {batch,draft} 身份——任一不符 ⇒ False（旧批次
    的写回不得落进新批）。``expect`` 没带/没声明的键不参与。"""
    if not isinstance(expect, dict) or not expect:
        return True
    if "batch" in expect and str((st or {}).get("batch") or "") \
            != str(expect.get("batch") or ""):
        return False
    if "draft" in expect:
        try:
            a = int((st or {}).get("draft") or 1)
        except (TypeError, ValueError):
            a = -1
        try:
            b = int(expect.get("draft") or 1)
        except (TypeError, ValueError):
            b = -2
        if a != b:
            return False
    return True


def _state_in_txn(conn: Any, review_id: str) -> dict:
    """写事务内读 ``review_state``（与随后 UPDATE 同连接同事务 ⇒
    读到什么写什么，中间没有第二份快照）。"""
    row = conn.execute(
        "SELECT review_state FROM cards WHERE card_id=?",
        (review_id,)).fetchone()
    try:
        cur = json.loads((row["review_state"] if row else "") or "{}")
    except (TypeError, ValueError):
        cur = {}
    return cur if isinstance(cur, dict) else {}


async def _cas_mutate(work_id: str, review_id: str, mutator, *,
                      expect: Any = None) -> bool:
    """派活写回的 CAS 通道：写事务内 ``_state_in_txn`` 读当前状态 ⇒
    ``_expect_matches`` 核身份 ⇒ 变更 ⇒ UPDATE。身份不符 ⇒ **不写**
    返回 False（旧批次的写回拒进新批）；写成功 True。"""
    def _fn(conn: Any) -> bool:
        cur = _state_in_txn(conn, review_id)
        if not _expect_matches(cur, expect):
            return False
        mutator(cur)
        conn.execute("UPDATE cards SET review_state=? WHERE card_id=?",
                     (json.dumps(cur, ensure_ascii=False), review_id))
        return True

    return bool(await card_db.run_write(work_id, _fn))


async def mark_asked(work_id: str, review_id: str,
                     at_seq: Any = None, *,
                     expect: Any = None) -> bool:
    """评审**已向正卡要过一次回应**：asked_seq = responded_seq + 1（期望的下一次）。

    未收回应＝`responded_seq < asked_seq`。机械计数，不看内容。

    ``at_seq``＝指令块在**正卡会话**里的落点——「ask 被消费」的锚：
    正卡会话此后有新块（seq 推进过它）⇒ 它起过回合读到指令。
    新一次派活同时把 ``wake_nudges`` 归零（新指令＝新唤醒预算）。

    ``asked_draft``＝派活时的草稿版——这张 ask **业务上覆盖到 vN 的否决**
    （指令携带的发言就是那批）。此后出现 ``draft_ver > asked_draft`` 的
    否决票 ⇒ 新否决它没覆盖 ⇒ ask 过期，该重派而不是 nudge
    （2026-11-02 实案：draft-14 的 ask 挂账，三席在 draft-30 新否决，
    意见从没进过正卡会话，classify 却把旧 ask 当"信还在途"空戳）。

    ``expect={batch,draft}``：快照身份——写事务内复核，批次/稿次
    已换 ⇒ 不写返回 False（批次A第五轮返工：旧批次派活落信期间
    换批，写回不得落进新批）。
    """
    def _m(cur: dict) -> None:
        cur["asked_seq"] = int(cur.get("responded_seq") or 0) + 1
        cur["asked_at_seq"] = at_seq
        cur["asked_draft"] = int(cur.get("draft") or 0)
        cur["wake_nudges"] = 0

    return await _cas_mutate(work_id, review_id, _m, expect=expect)


async def mark_responded(work_id: str, review_id: str) -> int:
    """正卡交回一次（`submit_revision`）：`responded_seq` 加一。

    [promote 对账 2026-11-01] 交回落在 ``promote_asked`` 上 ⇒ 那次请定稿
    **被回复消费掉了但没成**（正卡答了别的，没调 promote_draft）——清位，
    下一唤醒 classify 自然重派（与 ``set_drift_pending`` 清位同一条规矩：
    ask 落了但没拿到要的结果就清掉）。不清的话 `promote_asked` 恒真挡在
    `awaiting_promote`——work_027 实测：正卡按过期上下文交回应而未转正，
    批次就此停尸。正卡答完顺手也转了正的：批次已关，清位无害。"""
    st = await state_of(work_id, review_id)
    st2 = dict(st)
    seq = int(st.get("responded_seq") or 0) + 1
    st2["responded_seq"] = seq
    st2["promote_asked"] = False
    # 正卡真交回了 ⇒ 双向信道活着——派活失败计数清位，封顶不再单向
    # 锁死（[解棘轮 2026-11-15] 瞬时停卡攒的 fails 曾让一批永久拒派）。
    st2["dispatch_fails"] = 0

    def _fn(conn: Any) -> None:
        conn.execute("UPDATE cards SET review_state=? WHERE card_id=?",
                     (json.dumps(st2, ensure_ascii=False), review_id))

    await card_db.run_write(work_id, _fn)
    return seq


async def mark_promote_asked(work_id: str, review_id: str,
                             at_seq: Any = None, *,
                             expect: Any = None) -> bool:
    """记「已向正卡请过定稿」（`promote_asked`）：下一步不重复派活。

    正卡调 `promote_draft` 收口后批次自清；它若撞上 drift 闸，
    `set_drift_pending` 会把这位清掉，确认轮补完后才再派。

    ``at_seq``＝指令块在正卡会话里的落点（同 ``mark_asked`` 的锚语义）；
    新一次请定稿把 ``wake_nudges`` 归零。

    [drift 残留清账 2026-11-01] 能再派 ⇒ stale 席的确认轮已补完（classify
    收口区先判 drift∧stale）⇒ `drift_pending` 一并摘掉——它是**一次性
    触发标记**，不清会永远留在状态面上当假象。

    ``expect={batch,draft}``：快照身份——写事务内复核，批次/稿次
    已换 ⇒ 不写返回 False（批次A第五轮返工：派活落信期间换批，
    旧批的 ``promote_asked`` 不得写进新批）。
    """
    def _m(cur: dict) -> None:
        cur["promote_asked"] = True
        cur["promote_asked_seq"] = at_seq
        cur["wake_nudges"] = 0
        cur["drift_pending"] = {}

    return await _cas_mutate(work_id, review_id, _m, expect=expect)


async def mark_wake_nudge(work_id: str, review_id: str,
                          kind: str, at_seq: Any = None, *,
                          expect: Any = None) -> bool:
    """一次**纯唤醒已发出**（ask 被回合消费但没拿到结果）：`wake_nudges` 加一
    并把对应 ask 的锚**重锚到当前会话尾**——下一次唤醒只在「这之后又起过
    回合」时才再发（一次唤醒对应一段新活动，不重复轰炸同一段静默）。

    ``expect``：快照身份（同 ``mark_promote_asked`` 的 CAS 口径）。
    """
    def _m(cur: dict) -> None:
        cur["wake_nudges"] = int(cur.get("wake_nudges") or 0) + 1
        if kind == "promote":
            cur["promote_asked_seq"] = at_seq
        else:
            cur["asked_at_seq"] = at_seq

    return await _cas_mutate(work_id, review_id, _m, expect=expect)


async def mark_dispatch_failed(work_id: str, review_id: str, *,
                               expect: Any = None) -> bool:
    """派活**没落成**（消息没进正卡会话）：`dispatch_fails` 加一。

    机械计数：封顶判断在挑活的一侧（`_classify`），这里只如实记。

    ``expect``：快照身份（同 CAS 口径）——旧批次的失败账不加进新批。
    """
    def _m(cur: dict) -> None:
        cur["dispatch_fails"] = int(cur.get("dispatch_fails") or 0) + 1

    return await _cas_mutate(work_id, review_id, _m, expect=expect)


async def clear_dispatch_fails(work_id: str, review_id: str, *,
                               expect: Any = None) -> bool:
    """派活落成 ⇒ `dispatch_fails` 清零（新批次天然无此键＝0）。

    ``expect``：快照身份（同 CAS 口径）——批次已换 ⇒ 不动新批；
    清零本来幂等，但仍走同一条核对通道，防写回落在错批次上。
    """
    def _m(cur: dict) -> None:
        cur["dispatch_fails"] = 0

    return await _cas_mutate(work_id, review_id, _m, expect=expect)


async def set_drift_pending(work_id: str, review_id: str,
                            stale: list[str]) -> None:
    """promote_draft 撞版本闸 ⇒ 挂 `drift_pending`＋**清掉 `promote_asked`**。

    [drift 收窄口径 2026-10-31] stale 席位的「只评增量」确认轮由这一步
    触发：pending 挂上后 `run_once` 才会叫它们；确认补完（票面不再是旧版）
    后 promote_asked 已清 ⇒ 下一步自然重新派定稿。
    """
    st = await state_of(work_id, review_id)
    st2 = dict(st)
    st2["drift_pending"] = {"at": _now(),
                           "stale": [str(x) for x in stale]}
    st2["promote_asked"] = False

    def _fn(conn: Any) -> None:
        conn.execute("UPDATE cards SET review_state=? WHERE card_id=?",
                     (json.dumps(st2, ensure_ascii=False), review_id))

    await card_db.run_write(work_id, _fn)


async def mark_seen(work_id: str, review_id: str, role_id: str, *,
                    batch: str = "", vote_nudge_basis: list | None = None,
                    response_seen: int | None = None) -> None:
    """记"该席位最近一次被叫时看到的回应序号"（response_seen=responded_seq）。

    reject 席位只在**有新回应**时才重叫：被叫当下把已到的回应序号记给它，
    下次只有当 responded_seq 再涨它才又可被叫。

    ``response_seen`` 显式给值时按值落——席位回合**成功后才记**的调用方
    传的是起叫时刻的水位快照；不给则照旧读库内 responded_seq。
    """
    def _fn(conn: Any) -> None:
        row = conn.execute("SELECT review_state FROM cards WHERE card_id=?",
                           (review_id,)).fetchone()
        try:
            st = json.loads(row["review_state"] or "{}") if row else {}
        except (TypeError, ValueError):
            return
        if not isinstance(st, dict) or not st.get("batch") or (batch and st["batch"] != batch):
            return
        seats = st.setdefault("seats", {})
        seat = seats.setdefault(role_id, {})
        seat["response_seen"] = int(
            response_seen if response_seen is not None
            else st.get("responded_seq") or 0)
        if vote_nudge_basis is not None:
            seat["vote_nudge_basis"] = vote_nudge_basis
            seat["vote_nudge_finished"] = int(seat.get("vote_nudge_finished") or 0) + 1
        conn.execute("UPDATE cards SET review_state=? WHERE card_id=?",
                     (json.dumps(st, ensure_ascii=False), review_id))

    await card_db.run_write(work_id, _fn)


async def revoke(work_id: str, review_id: str, role_id: str,
                 *, batch: str = "") -> dict[str, Any]:
    """撤销一席的票。``batch`` 非空＝**只撤那个批次**的票——旧回合退场
    清理按旧批次点名，换批后别席/同席在新批次的合法票不动。
    缺省＝现行口径（撤当前批次的票）。"""
    st = await state_of(work_id, review_id)
    cur = str(st.get("batch") or "")
    tgt = str(batch or "") or cur
    if not tgt:
        return {"ok": False, "error": "当前没有进行中的批次"}

    def _fn(conn: Any) -> None:
        conn.execute("UPDATE review_approvals SET revoked=1"
                     " WHERE work_id=? AND batch_id=? AND role_id=?",
                     (work_id, tgt, role_id))

    await card_db.run_write(work_id, _fn)
    return {"ok": True, "batch": tgt, "role": role_id, "revoked": True}


async def status(work_id: str, review_id: str) -> dict[str, Any]:
    """批次状态：谁有效通过、谁没有、是否全员通过。

    有效＝`revoked=0`（没被撤销）。

    [2026-10-29 用户口径「通过之后它就不再被唤起进行后续的评审」]
    **草稿版本变了不再让票失效**：一次评审里 A 通过之后，其他人还会继续提意见、
    子卡继续改草稿、继续评，但 A 不再被叫回来重投。
    `draft_changed` 仍然照算，但它现在只是**展示用的事实**（"草稿在本批次内
    改过几版"），不参与票的有效性判定。
    """
    st = await state_of(work_id, review_id)
    batch = str(st.get("batch") or "")
    if not batch:
        # [2026-10-27 修·实弹抓到] 收口后 batch 被清空，原实现照旧拿空批次去查
        # approvals ⇒ 谁都查不到 ⇒ `all_passed=False`、`pending=全体`。
        # 于是"刚刚全员通过"这件事在状态上读不出来（实弹收口后实测就是这样）。
        # 收口后的状态以**正稿**为准。
        closed = str(st.get("closed_batch") or "")
        return {"batch": "", "closed_batch": closed,
                "type": str(st.get("type") or ""),
                "draft": int(st.get("draft") or 1),
                "members": await members_of(work_id, review_id),
                "passed": [], "pending": [], "rejected": [],
                "votes": {}, "stale": [], "draft_changed": False,
                "closed": bool(closed),
                "verdict": str(st.get("verdict") or ""),
                "all_passed": bool(closed) and bool(st.get("verdict"))}
    members = await members_of(work_id, review_id)
    draft = int(st.get("draft") or 1)
    # 草稿是否已变（展示用，不影响票）
    draft_changed = False
    tgt = str(st.get("target") or "")
    if tgt:
        cur = await _card_hash(work_id, tgt)
        base = str(st.get("draft_hash") or "")
        draft_changed = bool(base) and bool(cur) and cur != base

    def _fn(conn: Any) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            "SELECT role_id,draft_ver,revoked,ts,verdict,draft_snapshot"
            " FROM review_approvals"
            " WHERE work_id=? AND batch_id=?", (work_id, batch)).fetchall()
        return {str(r["role_id"]): dict(r) for r in rows}

    try:
        rows = dict(await card_db.run_read(work_id, _fn, {}) or {})
    except Exception:  # noqa: BLE001
        rows = {}

    passed: list[str] = []
    rejected: list[str] = []
    pending: list[str] = []
    votes: dict[str, dict[str, Any]] = {}
    stale: list[str] = []
    for m in members:
        r = rows.get(m)
        revoked = bool(r) and int(r.get("revoked") or 0)
        # [venue 真票] verdict 列：approve/reject；存量票 verdict=NULL 按 approve
        # 归一（历史上有票即通过）。被撤销的票＝回待定，不计任何阵营。
        verdict = ""
        if r and not revoked:
            verdict = str(r.get("verdict") or "") or "approve"
        if verdict:
            votes[m] = {"verdict": verdict,
                        "draft_ver": int(r.get("draft_ver") or 0),
                        "snapshot": str(r.get("draft_snapshot") or "")}
        if verdict == "approve":
            passed.append(m)
            # [venue 版本闸] 批准版早于当前草稿 ⇒ stale：promote 时不收口、
            # 只对它补「只评增量」确认轮。
            if int(r.get("draft_ver") or 0) < draft:
                stale.append(m)
        elif verdict == "reject":
            rejected.append(m)
            pending.append(m)
        else:
            pending.append(m)

    return {"batch": batch, "draft": draft,
            "type": str(st.get("type") or ""),
            # [venue] 收口校验要拿目标卡（finalize 读它判「正稿写了没」）
            "target": tgt,
            "members": members, "passed": passed, "pending": pending,
            "rejected": rejected, "votes": votes, "stale": stale,
            "draft_changed": draft_changed,
            "all_passed": bool(members) and not pending}


async def finalize(work_id: str, review_id: str,
                   verdict: str = "") -> dict[str, Any]:
    """全员通过 ⇒ 收口（批次清空）。未全过则不动作。

    [2026-10-29 · 结构约束，用户口径「最后也一定是调工具写正稿为结束」]
    **收口＝正稿写完**。正稿（子卡的 `requirements_state`）还不是 `approved` 时
    **拒绝收口** —— 否则批次被清掉、正稿却没写，`run_once` 看到「没批次」就再也
    不处理，流程永久卡死（实弹：card_038 / card_071 各卡了十几版）。

    这不是防御性校验，是把「清批次」和「正稿已写」绑成同一件事：
    任何调用方（现在的、以后新增的）都绕不过去。
    """
    st = await status(work_id, review_id)
    if not st.get("all_passed"):
        return {}
    target = str(st.get("target") or "")
    if target:
        from . import card_store as _cs
        try:
            _card = await _cs.read_card(work_id, target)
            _state = str((_card.get("meta") or {}).get("requirements_state") or "")
        except Exception as exc:  # noqa: BLE001 读不到卡就不收口（宁可不收）
            logger.warning("[review] 收口前读子卡失败 %s: %s", target, exc)
            return {}
        if _state != "approved":
            logger.warning("[review] 正稿未写（state=%s），拒绝收口 review=%s target=%s",
                           _state or "(空)", review_id, target)
            return {}
    text = str(verdict or "").strip() or _render_verdict(st)
    out = await close_batch(work_id, review_id, text)
    return out


def _render_verdict(st: dict[str, Any]) -> str:
    """正稿（系统渲染的机械部分；不含判断，只陈述本批次结果）。"""
    return "\n".join([
        f"批次 {st.get('batch')} · 全员通过（{_now()}）",
        f"通过 {len(st.get('passed') or [])}/{len(st.get('members') or [])}："
        f"{', '.join(st.get('passed') or [])}",
        "本批次全员通过。下一批次以本正稿 ＋ 子卡最新卡面为输入。",
    ])


def _now() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")
