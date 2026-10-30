# -*- coding: utf-8 -*-
"""伴生评审卡存储（[伴生评审卡 2026-10-27]）。

一张 `sub_project` / `topic` 卡 = 一张**评审卡**：一对一、离树（`off_tree`）、
不设 `parent`、随子卡归档。评审卡上跑"批次"：每个阶段收口开一个批次，
全员 `approve` 才收口。

存储分工（P1：卡面是唯一权威事实源）
- **卡面**：判据在第三块（`requirements`）；当前批次与通过状态在现状块（系统渲染）
- **cards 列**：`review_of`（配对的子卡）、`review_state`（当前批次 JSON）
- **review_approvals 表**：每次 approve 一行，绑 `(draft_ver, criteria_ver)`
- **事件流**：`review_batch_opened` / `review_batch_closed` 留痕

**不建 members 表**：成员＝用 `relations` 连到评审卡的卡（P7／P5）。
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

#: relations 的 kind 值域沿用现有两种（依赖／关联）——不新增线类型（P1）
REL_KIND = "关联"

#: 关系说明前缀（用于认"参与评审"这条边）
REL_PARTICIPATE = "评审·参与"
REL_TARGET = "评审·被评"

MISSION_TPL = (
    "对 {title}（{cid}）的阶段成果做验收。"
    "判据在本卡第三块；批次与通过状态在本卡现状块。"
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
        # 配对列 + 双向 relations（子卡 ←→ 评审卡）
        await _write_pair(work_id, rid, child_id)
        return rid
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 伴生卡派生异常 child=%s: %s", child_id, exc)
        return ""


async def _write_pair(work_id: str, review_id: str, child_id: str) -> None:
    """写 review_of 与两侧 relations（幂等：已存在则不重复加）。"""

    def _fn(conn: Any) -> None:
        conn.execute("UPDATE cards SET review_of=? WHERE card_id=?",
                     (child_id, review_id))
        for me, other, detail in ((review_id, child_id, REL_TARGET),
                                  (child_id, review_id, REL_PARTICIPATE)):
            row = conn.execute("SELECT relations FROM cards WHERE card_id=?",
                               (me,)).fetchone()
            try:
                rels = json.loads((row["relations"] if row else "") or "[]")
            except (TypeError, ValueError):
                rels = []
            if not isinstance(rels, list):
                rels = []
            if any(isinstance(x, dict) and x.get("target") == other
                   for x in rels):
                continue
            rels.append({"target": other, "kind": REL_KIND, "detail": detail})
            conn.execute("UPDATE cards SET relations=? WHERE card_id=?",
                         (json.dumps(rels, ensure_ascii=False), me))

    try:
        await card_db.run_write(work_id, _fn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 配对写入失败 review=%s child=%s: %s",
                       review_id, child_id, exc)


# ---------------------------------------------------------------- 成员
async def members_of(work_id: str, review_id: str) -> list[str]:
    """评审成员＝relations 里标了「评审·参与」的卡（不含被评卡本身）。"""

    def _fn(conn: Any) -> list[str]:
        row = conn.execute("SELECT relations FROM cards WHERE card_id=?",
                           (review_id,)).fetchone()
        try:
            rels = json.loads((row["relations"] if row else "") or "[]")
        except (TypeError, ValueError):
            return []
        out: list[str] = []
        for x in rels if isinstance(rels, list) else []:
            if not isinstance(x, dict):
                continue
            if str(x.get("detail") or "") != REL_PARTICIPATE:
                continue
            t = str(x.get("target") or "")
            if t:
                out.append(t)
        return out

    try:
        return list(await card_db.run_read(work_id, _fn, []) or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 读成员失败 review=%s: %s", review_id, exc)
        return []


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
                "criteria": int(st.get("criteria") or 1),
            })
        return out

    try:
        return list(await card_db.run_read(work_id, _fn, []) or [])
    except Exception as exc:  # noqa: BLE001 读不到＝没有入口，不影响左栏
        logger.warning("[review] 列进行中批次失败 work=%s: %s", work_id, exc)
        return []


async def state_of(work_id: str, review_id: str) -> dict[str, Any]:
    """评审卡当前批次状态（review_state 列）。空＝没有进行中批次。"""

    def _fn(conn: Any) -> dict[str, Any]:
        row = conn.execute("SELECT review_state FROM cards WHERE card_id=?",
                           (review_id,)).fetchone()
        raw = (row["review_state"] if row else "") or ""
        try:
            d = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            d = {}
        return d if isinstance(d, dict) else {}

    try:
        return dict(await card_db.run_read(work_id, _fn, {}) or {})
    except Exception:  # noqa: BLE001
        return {}


async def open_batch(work_id: str, review_id: str, focus: str,
                     members: Optional[list[str]] = None,
                     target: str = "", brief: str = "") -> dict[str, Any]:
    """开一个批次：写 review_state（批次号 + 版本 1/1）＋把成员连进来。

    [P6 换上下文不压缩] 上一批次的正稿**原文**带进来（`prev_verdict`），
    过程（发言、工具结果）不带——它们留在会话里归档。

    [草稿＝子卡卡面] 不另存草稿：`draft_hash` 记子卡当时的 `content_hash`。
    子卡改卡面 ⇒ hash 变 ⇒ 全员重评（P3：通过绑草稿版本）。

    `brief`＝发起方填写的**任务背景／当前状况／要评审的内容**，**原样**进上下文
    （不截断，P6）。
    """
    old = await state_of(work_id, review_id)
    prev = str(old.get("verdict") or old.get("prev_verdict") or "")
    tgt = str(target or old.get("target") or "")
    st = {"batch": _new_batch_id(), "draft": 1, "criteria": 1,
          "focus": str(focus or "")[:2000], "opened_at": _now(),
          "prev_verdict": prev, "target": tgt,
          "brief": str(brief or ""),
          "draft_hash": await _card_hash(work_id, tgt)}
    for m in members or []:
        await _add_member(work_id, review_id, m)

    def _fn(conn: Any) -> None:
        conn.execute("UPDATE cards SET review_state=? WHERE card_id=?",
                     (json.dumps(st, ensure_ascii=False), review_id))

    await card_db.run_write(work_id, _fn)
    return st


async def sync_draft(work_id: str, review_id: str) -> bool:
    """子卡卡面变了 ⇒ **草稿版本 +1**（返回是否变了）。

    [P3 通过绑版本] 版本一变，`review_approvals` 里旧 `draft_ver` 全部失效，
    不需要逐个 revoke——失效是**版本比较的结果**，不是一次批量改写。

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
    st2["closed_batch"] = batch
    st2["closed_at"] = _now()
    st2["verdict"] = str(verdict or "")

    def _fn(conn: Any) -> None:
        conn.execute("UPDATE cards SET review_state=? WHERE card_id=?",
                     (json.dumps(st2, ensure_ascii=False), review_id))

    await card_db.run_write(work_id, _fn)
    return st2


async def mark_spoken(work_id: str, review_id: str, role_id: str) -> list[str]:
    """记「本轮这个成员发过言了」，返回本轮已发言的名单。

    [2026-10-30 用户口径] 一轮＝**全员发完言，再改一次稿**；不是「一人发言就改稿」。
    所以需要知道"本轮谁已经说过"——一轮的边界就是**草稿版本**：草稿一变，
    名单自动清空，下一轮从头再问。
    """
    st = await state_of(work_id, review_id)
    if not st.get("batch"):
        return []
    draft = int(st.get("draft") or 1)
    sp = st.get("spoken") or {}
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
    st = await state_of(work_id, review_id)
    sp = st.get("spoken") or {}
    if int(sp.get("draft") or -1) != int(st.get("draft") or 1):
        return []
    return [str(x) for x in (sp.get("roles") or [])]


async def _add_member(work_id: str, review_id: str, member_id: str) -> None:
    def _fn(conn: Any) -> None:
        row = conn.execute("SELECT relations FROM cards WHERE card_id=?",
                           (review_id,)).fetchone()
        try:
            rels = json.loads((row["relations"] if row else "") or "[]")
        except (TypeError, ValueError):
            rels = []
        if not isinstance(rels, list):
            rels = []
        if any(isinstance(x, dict) and x.get("target") == member_id
               and x.get("detail") == REL_PARTICIPATE for x in rels):
            return
        rels.append({"target": member_id, "kind": REL_KIND,
                     "detail": REL_PARTICIPATE})
        conn.execute("UPDATE cards SET relations=? WHERE card_id=?",
                     (json.dumps(rels, ensure_ascii=False), review_id))

    try:
        await card_db.run_write(work_id, _fn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 加成员失败 review=%s m=%s: %s",
                       review_id, member_id, exc)


# ---------------------------------------------------------------- 通过
async def approve(work_id: str, review_id: str, role_id: str) -> dict[str, Any]:
    """记录一次通过：绑定当前 (draft, criteria) 版本。"""
    st = await state_of(work_id, review_id)
    batch = str(st.get("batch") or "")
    if not batch:
        return {"ok": False, "error": "当前没有进行中的批次"}

    def _fn(conn: Any) -> None:
        conn.execute(
            "INSERT INTO review_approvals"
            " (work_id,batch_id,role_id,draft_ver,criteria_ver,ts,revoked)"
            " VALUES (?,?,?,?,?,?,0)"
            " ON CONFLICT(work_id,batch_id,role_id) DO UPDATE SET"
            " draft_ver=excluded.draft_ver, criteria_ver=excluded.criteria_ver,"
            " ts=excluded.ts, revoked=0",
            (work_id, batch, role_id, int(st.get("draft") or 1),
             int(st.get("criteria") or 1), _now()))

    await card_db.run_write(work_id, _fn)
    return {"ok": True, "batch": batch, "role": role_id,
            "draft": st.get("draft"), "criteria": st.get("criteria")}


async def revoke(work_id: str, review_id: str, role_id: str) -> dict[str, Any]:
    st = await state_of(work_id, review_id)
    batch = str(st.get("batch") or "")
    if not batch:
        return {"ok": False, "error": "当前没有进行中的批次"}

    def _fn(conn: Any) -> None:
        conn.execute("UPDATE review_approvals SET revoked=1"
                     " WHERE work_id=? AND batch_id=? AND role_id=?",
                     (work_id, batch, role_id))

    await card_db.run_write(work_id, _fn)
    return {"ok": True, "batch": batch, "role": role_id, "revoked": True}


async def status(work_id: str, review_id: str) -> dict[str, Any]:
    """批次状态：谁有效通过、谁没有、是否全员通过。

    有效＝`revoked=0` 且 **判据版本**与当前一致。

    [2026-10-29 用户口径「通过之后它就不再被唤起进行后续的评审」]
    **草稿版本变了不再让票失效**：一次评审里 A 通过之后，其他人还会继续提意见、
    子卡继续改草稿、继续评，但 A 不再被叫回来重投。
    改前是「票绑 (draft, criteria) 双版本」，草稿一改全员票作废 ⇒ 每轮都全员
    重跑（work_027 实跑：总裁被叫了三次）。这与「通过就不再唤起」直接冲突：
    票作废了却不叫回来投，批次永远收不了口；叫回来投，又违反用户口径。

    **判据变了仍然全失效**（`criteria_ver != crit`）：判据是评审依据，
    依据换了，之前的判断不成立——这条不受本次改动影响。

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
                "draft": int(st.get("draft") or 1),
                "criteria": int(st.get("criteria") or 1),
                "members": await members_of(work_id, review_id),
                "passed": [], "pending": [], "draft_changed": False,
                "closed": bool(closed),
                "verdict": str(st.get("verdict") or ""),
                "all_passed": bool(closed) and bool(st.get("verdict"))}
    members = await members_of(work_id, review_id)
    draft, crit = int(st.get("draft") or 1), int(st.get("criteria") or 1)
    # 草稿是否已变（展示用，不影响票）
    draft_changed = False
    tgt = str(st.get("target") or "")
    if tgt:
        cur = await _card_hash(work_id, tgt)
        base = str(st.get("draft_hash") or "")
        draft_changed = bool(base) and bool(cur) and cur != base

    def _fn(conn: Any) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            "SELECT role_id,draft_ver,criteria_ver,revoked,ts FROM review_approvals"
            " WHERE work_id=? AND batch_id=?", (work_id, batch)).fetchall()
        return {str(r["role_id"]): dict(r) for r in rows}

    try:
        rows = dict(await card_db.run_read(work_id, _fn, {}) or {})
    except Exception:  # noqa: BLE001
        rows = {}

    passed: list[str] = []
    pending: list[str] = []
    for m in members:
        r = rows.get(m)
        # 有效票只看两件事：没被撤销 ＋ 判据版本仍是投票那一版。
        # 草稿版本**不参与**（见 docstring）。
        ok = bool(r) \
            and not int(r.get("revoked") or 0) \
            and int(r.get("criteria_ver") or 0) == crit
        (passed if ok else pending).append(m)

    return {"batch": batch, "draft": draft, "criteria": crit,
            "members": members, "passed": passed, "pending": pending,
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
        "本批次判据全部满足。下一批次以本正稿 ＋ 子卡最新卡面为输入。",
    ])


def _now() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")
