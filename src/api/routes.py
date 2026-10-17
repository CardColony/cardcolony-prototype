# -*- coding: utf-8 -*-
"""HTTP API 路由。

错误约定：CardNotFoundError→404；CardLockedError→423；请求体校验失败→400；
模型调用失败→502。错误响应 {"error": "说明"}。

Phase 7：
- 新增 /api/works 全套路由（多剧本）。
- messages 主链路：context_assembler 组装 → prompts.build_system_prompt →
  带工具的工具循环（不设轮次封顶）→ parse_proposals → apply_proposals → tool_events。
- 旧路由 /api/canvas、/api/cards... 保留，内部固定转发 work_001（兼容层）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..storage import (
    activity, canvas_store, card_db, card_store, event_log, manuscript_store,
    memory_store, session_facade, session_ops, session_store, work_store,
)
from ..storage.card_store import CardLockedError, CardNotFoundError
from ..storage.card_store import CardDeleteDeniedError
from ..storage.hash_utils import parse_front_matter, split_body_sections
from ..storage.manuscript_store import ChapterNotFoundError
from . import auto_driver, card_msg, card_proposal, card_tools, control, context_assembler, context_regions, event_stream, message_runner, model_service, progress, prompt_log, prompts, serial_gate, simple_service, text_tool_rescue, tool_fold

logger = logging.getLogger("api.routes")

router = APIRouter(prefix="/api")

DEFAULT_WORK_ID = "work_001"  # 兼容层固定转发目标

# [卡片信息结构改造 S2-8] 白名单全集。新键 mission/status_block/rulings 为当前口径；
# 旧键 charter/conclusion/rejections 保留（存量前端与脚本照旧可用，存储层内归一）。
# 两个校验点（新版 PATCH :423 ＋ 兼容层 PATCH :623）共用本集合——改一处即两处同步。
# [权限底座重做 2026-10-12 · 裁定 2] card_type 已移出白名单：类型建卡时定死、
# 运行时不可变，API 层就该拒（早于存储层拒），不再让非法键过白名单后才被存储层拦。
# project_class 从来不在本集合（建卡时随 project 写入），无需处理。
ALLOWED_UPDATE_KEYS = {
    # [依赖关联线 2026-10-16] links＝通信线（原 relations 改名）；relations＝依赖
    # 关联线（新线）——两键并存，PATCH 两条通道共用本集合。
    "title", "tags", "links", "relations", "session_ref",
    "mission", "status_block", "rulings",  # 正文三块·新键
    "conclusion", "rejections", "charter",  # 正文三块·旧键（兼容保留）
    "parent",
}
# [phase8.3] status 二态已废除：PATCH 带 status 一律忽略（不报错）

# [卡片编辑机制改造 2026-10-12 · 块1] 读侧块集判据：rd_project → 需求文档
# （requirements），其余（research_project 及未分类存量）→ 定论（rulings）。
# card_store.read_card 返回的 body 恒含 rulings 键；此处按根卡 project_class
# 过滤——rd 卡不外露 rulings、换 requirements；其他卡不外露 requirements。
# 存量研发卡的定论块内容在 rd 卡上以 requirements 呈现（第三块语义随树类走），
# 零数据删除、零迁移；缺失块返回空串不报错。
def _apply_block_set(result: dict, work_id: str) -> dict:
    """按本树 project_class 过滤 read_card 返回的 body 块键（读侧，纯展示层）。"""
    try:
        pc = card_store._root_project_class(work_id)
    except Exception:  # noqa: BLE001
        pc = ""
    if pc != "rd_project":
        return result  # 非研发卡：body 原样（rulings 已在；requirements 不外露）
    body = result.get("body") or {}
    # [卡片存储迁移 SQLite 2026-10-14] 第三块在库内是**一列**（body["rulings"]）——
    # 研发树的语义名是「需求文档」，读侧外露为 requirements；存储键/列名仍是
    # rulings（「叫什么名」由本树 project_class 决定，盘上不再有区头可判）。
    req = (body.get("rulings") or "").strip()
    result = dict(result)
    result["body"] = {
        "mission": body.get("mission", ""),
        "status_block": body.get("status_block", ""),
        "requirements": req,
        "history": body.get("history", []),
    }
    return result


# [无闸改造 2026-10-02] MAX_TOOL_ROUNDS 已删除：工具循环不设封顶


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


# ============================================================
# Request models
# ============================================================

class CreateWorkRequest(BaseModel):
    title: str = Field(min_length=1)


class UpdateWorkRequest(BaseModel):
    title: Optional[str] = None
    ending_derived: Optional[bool] = None


class PatchPositionsRequest(BaseModel):
    positions: dict[str, dict[str, Any]]


class CreateCardRequest(BaseModel):
    title: str = Field(min_length=1)
    source: str
    # [底座收口 T5.b] 缺省"线索"→""：空值由 create_card 按树词表映射
    # （rd→sub_project / research→topic）；「线索」已退役，旧缺省会让
    # 前端 UI 建卡全被闸1 拒。
    card_type: str = ""
    parent: Optional[str] = None
    charter: str = ""
    conclusion: str = ""
    # [自动模式 2026-10-17 · 回灌 5700 G1（019a0f0）] goal＝本卡目标文本。
    # 存储层 create_card 早有 goal 形参，但本模型未声明该字段 ⇒ Pydantic 静默
    # 丢弃 ⇒ HTTP 建的 auto 子卡继承到 auto_mode=1 却 goal 为空、永不点火
    # （只有模型侧 create_card(goal=...) 那条路通——前端/用户建卡断链）。
    # 缺省空串 ⇒ 行为与改造前逐字一致（非 auto 卡、不带 goal 的建卡零变化）。
    goal: str = ""


class PatchCardRequest(BaseModel):
    updates: dict[str, Any] = Field(default_factory=dict)
    history_note: str


class PostMessageRequest(BaseModel):
    content: str = Field(min_length=1)
    # [T10-3-3] 重发去重：前端重发时携带被重发那条提问的原 pair_id；正常提问缺省 None。
    pair_id: Optional[str] = None
    # [更新现状按钮改造 2026-10-16] 本轮操作标记（专用通道的入口标记）。
    # 缺省 None/"" ⇒ 普通回合，行为逐字不变；"update_status" ⇒ 「更新现状」专用回合
    # （轮首注入专用任务 + 工具面临时追加 propose_status）。
    op: Optional[str] = None


# [卡片编辑机制改造 2026-10-12 · 块2] draft/commit 请求体
class DraftRequest(BaseModel):
    kind: str = Field(min_length=1)  # requirements | status（mission 不可经此口）
    feedback: Optional[str] = None
    prev_draft: Optional[str] = None


class CommitRequest(BaseModel):
    kind: str = Field(min_length=1)
    draft: str = ""
    base_hash: str = Field(min_length=1)
    feedback_rounds: int = 0


# [规则卡 2026-10-15] 规则面板/审批通道请求体
class PatchRuleSectionRequest(BaseModel):
    content: str = ""
    base_hash: str = Field(min_length=0, default="")


class RuleCommitRequest(BaseModel):
    section: str = Field(min_length=1)
    content: str = ""
    base_hash: str = Field(min_length=0, default="")


# [无内容预设改造 2026-10-02] PatchMethodologyRequest 已删除（方法论库停用）


# ============================================================
# Works 路由
# ============================================================

@router.get("/works")
async def list_works():
    return {"works": work_store.list_works()}


@router.post("/works")
async def create_work(req: CreateWorkRequest):
    result = await work_store.create_work(req.title)
    if "error" in result:
        return _err(400, result["error"])
    return JSONResponse(status_code=201, content=result)


# ============================================================
# [T17-B4] 跨项目导航树（一次请求拿全，避免 N+1）
# ============================================================

def _card_obj(m: dict, act: dict, child_count: int, wid: str) -> dict:
    """[S1·R-05] 卡对象构造（/api/nav 与 /api/nav/children 共用，一套防漂移）。

    字段集合定格 9 项（S1·R-02 原 8 项 + [左栏接线 步1] card_type）：
    card_id/title/parent/child_count/last_active_at/branch_active_at/
    work_id/state/card_type——克制判据＝每个字段都有已设计的消费方
    （契约文档：docs/exploration/左侧边栏-S1契约-nav接口-20261011.md）。
    work_id＝归属由提供方声明（S1·R-01，前端禁止自行反查推断）；
    state＝生命周期（S1·R-04），存量卡无此字段返回空串（前端按无状态容错）；
    card_type＝卡片类型（左栏定稿「工具卡不进侧栏」的消费方——此前接口不送类型，
    前端无从过滤；存量卡无此字段同样返回空串，前端按未知类型容错）。
    """
    cid = m.get("card_id", "")
    return {
        "card_id": cid,
        "title": m.get("title", ""),
        "parent": m.get("parent", ""),
        "child_count": child_count,
        "last_active_at": act.get(cid, {}).get("own", ""),
        # [主界面优化 E2] 分支活跃（该卡及其全部子孙最近活跃）现算增列
        "branch_active_at": act.get(cid, {}).get("branch", ""),
        "work_id": wid,
        "state": m.get("state", ""),
        "card_type": m.get("card_type", ""),
    }


@router.get("/nav")
async def get_nav():
    """返回 {"projects": [...]}，供工作区左栏对话导航渲染。

    排序契约（方案 §4.3，服务端负责、前端不重排）：
    - projects 按 last_active_at 降序（list_works 已排好）；
    - 每项目 cards 按 last_active_at 降序，无时间者排末位，
      并列以 card_id 升序稳定收尾（刷新不跳序）；
    - 卡清单从卡文件真身现读（activity.iter_card_metas），活跃度请求时现算、
      不入盘（RD-06）；根卡作为分组头不重复列树（方案 §3.2）；
    - 孤儿卡（parent 指向已删卡）不丢弃：parent 原样返回，前端挂该项目根层末尾。
    既有端点返回结构零改动；/api/works 仅按 B-3 附 last_active_at 并改排序。
    """
    projects = []
    for w in work_store.list_works():
        wid = w.get("work_id", "")
        metas = list(activity.iter_card_metas(wid))
        child_count: dict[str, int] = {}
        for m in metas:
            p = m.get("parent", "")
            if p:
                child_count[p] = child_count.get(p, 0) + 1
        root_id = w.get("work_card_id", "")
        # [主界面优化 E2] 同源一次现算每卡 own/branch 活跃（activity.compute_card_activity）；
        # 端点现算增列、不写任何真身文件（RD-02 数据层零写）。
        act = activity.compute_card_activity(wid)
        cards = []
        for m in metas:
            cid = m.get("card_id", "")
            if cid and cid == root_id:
                continue
            cards.append(_card_obj(m, act, child_count.get(cid, 0), wid))
        # 两段稳定排序：先并列收尾键 card_id 升序，再按活跃降序（空串排末位）
        cards.sort(key=lambda c: c["card_id"])
        cards.sort(key=lambda c: c["last_active_at"] or "", reverse=True)
        projects.append({
            "work_id": wid,
            "title": w.get("title", ""),
            "work_card_id": root_id,
            "last_active_at": w.get("last_active_at", ""),
            "card_count": w.get("card_count", 0),
            "cards": cards,
        })
    return {"projects": projects}


@router.get("/nav/children")
async def get_nav_children(work_id: str = "", parent: str = ""):
    """[S1·R-05] 单层直系子卡端点（S2 异步加载：展开才拉，省传输与解析）。

    与 /api/nav 并存（旧前端继续吃全量，S3 前不切换）；cards 复用 _card_obj
    同一套契约（含 work_id/state）。parent 空串＝第一层，实现为**根卡直系**
    （根卡是分组头、其 parent 虽为空串但不进树——与 /api/nav 同口径排除
    根卡本体；显式 parent=根卡 等价同层）。child_count 照算（展开箭头依赖）。
    work_id 不存在→404、parent 非空且不存在→404（与既有「剧本/卡片不存在」
    404 语义对齐）。只答一层直系、无越层参数——防退化成全量遍历。
    孤儿卡（parent 指向已删卡）不出现在 children 任一层，其「挂根层末尾」
    展示仍由 /api/nav 全量承担（与 nav 孤儿契约一致，前端根层兜底）。
    """
    w = work_store.read_work(work_id)
    if w is None:
        return _err(404, f"剧本不存在: {work_id}")
    root_id = w.get("work_card_id", "")
    if parent and not card_store.card_exists(work_id, parent):
        return _err(404, f"卡片不存在: {parent}")
    metas = list(activity.iter_card_metas(work_id))
    child_count: dict[str, int] = {}
    for m in metas:
        p = m.get("parent", "")
        if p:
            child_count[p] = child_count.get(p, 0) + 1
    act = activity.compute_card_activity(work_id)
    eff_parent = parent or root_id
    cards = [
        _card_obj(m, act, child_count.get(m.get("card_id", ""), 0), work_id)
        for m in metas
        if m.get("parent", "") == eff_parent
    ]
    # 与 /api/nav 同款两段稳定排序（card_id 升序收尾→活跃降序，刷新不跳序）
    cards.sort(key=lambda c: c["card_id"])
    cards.sort(key=lambda c: c["last_active_at"] or "", reverse=True)
    return {"work_id": work_id, "parent": parent, "cards": cards}


@router.get("/nav/running")
async def get_nav_running():
    """[左栏接线 步1] 「谁在跑」总览：当前处于活跃调用中的卡清单。

    左栏定稿要在行尾亮「运行中」转圈（所有 agent 通态——模型每轮可能走很久）。
    此前只有单点问法 /cards/{cid}/progress，一屏几十张卡不可能逐张轮询，故增此口。
    判据＝progress 写点声明的 active 位（终止态 done/error/idle 不复活），
    前端不再靠阶段白名单反推。纯读内存 _store，零写盘、不拦截（RD-02 同口径）。
    进程重启后为空＝当前无运行中，如实反映。
    """
    return {"running": progress.iter_active()}


async def _work_fingerprint(wid: str) -> str:
    """[S1·R-06] 单 work 变化指纹：work.json、sessions/*.md 的「路径+大小+mtime」
    全列表哈希，**＋库内 cards.rev**（只读遍历，不侵入任何写路径）。

    [卡片存储迁移 SQLite 2026-10-14] 卡片侧改读库内 ``meta.rev``（每次写 +1）——
    理由（方案 §7.1.1）：SQLite 默认 WAL 把写入落在 ``cards.db-wal``，主库文件
    ``cards.db`` 的 mtime 可能不变，靠文件 stat 算指纹会**漏报**「卡片变了」，
    前端就不刷新。rev 不依赖文件系统语义。

    实现层修订两笔（对任务书 T6「mtime 取 max」，如实记录）：
    ① max→列表哈希：纯 max 探测不到删卡——删掉非最新文件 max 不变，S2
      侧栏会挂幽灵卡；仿仓内既有 data_snapshot._fingerprint 同法升级，
      增删改全捕捉；响应契约 {"version","by_work"} 不变。
    ② glob+stat→os.scandir：Windows 下 scandir 的 entry.stat() 直接取
      目录枚举自带数据、免二次 stat，语义不变。
    """
    base = card_store.WORKS_DIR / wid
    entries: list[tuple[str, int, int]] = []
    try:
        st = (base / "work.json").stat()
        entries.append(("work.json", st.st_size, int(st.st_mtime)))
    except OSError:
        pass
    try:
        with os.scandir(base / "sessions") as it:
            for e in it:
                if e.name.endswith(".md") and e.is_file():
                    st = e.stat()
                    entries.append((f"sessions/{e.name}", st.st_size, int(st.st_mtime)))
    except OSError:
        pass                      # work 无 sessions 子目录＝空指纹，合法
    rev = await card_db.read_rev_async(wid)
    h = hashlib.sha256()
    for name, size, mtime in sorted(entries):
        h.update(f"{name}|{size}|{mtime}\n".encode("utf-8"))
    h.update(f"cards.rev|{rev}\n".encode("utf-8"))
    return h.hexdigest()


@router.get("/nav/version")
async def get_nav_version():
    """[S1·R-06] 变化指纹端点（S2 的 60s 探测源：没变零开销、变了重拉+diff）。

    by_work＝各 work 指纹（work 级差分：哪个 work 变了只 diff 哪个）；
    全局 version＝各 work 指纹的复合哈希（确定性、重启可复算——不用内存
    计数器：重启即丢且须侵入全部写路径）。不做 SSE/WebSocket（本系统
    文件即数据、无事件源）；不用目录 mtime（改内容不更新目录 mtime）。
    work 集合＝works/ 下含 work.json 的目录——**不经 work_store.list_works()**：
    其附带活跃度现算（会话尾扫），U2 实测 56ms 占该端点大头；版本探测
    只认目录与文件、不需要 work 元数据。7 works/158 文件指纹本体 0.7ms。
    """
    by_work: dict[str, str] = {}
    try:
        dirs = list(os.scandir(card_store.WORKS_DIR))
    except OSError:
        dirs = []
    for e in dirs:
        wid = e.name
        if not wid or not e.is_dir():
            continue
        if not (card_store.WORKS_DIR / wid / "work.json").exists():
            continue
        by_work[wid] = await _work_fingerprint(wid)
    h = hashlib.sha256()
    for wid in sorted(by_work):
        h.update(f"{wid}|{by_work[wid]}\n".encode("utf-8"))
    return {"version": h.hexdigest(), "by_work": by_work}


@router.get("/works/{work_id}/card-messages")
async def list_card_messages(work_id: str, limit: int = 200):
    """[迭代四 K4] 卡间消息通知端点（B4「不得静默」的数据面；纯读、零新增存储）。

    **数据源＝inbox 收件箱**（[卡片端点改读收件箱 2026-10-15]）：卡间信已改落
    收件箱（card_db.append_inbox），本端点随之改读同一张表——待处理从此是**显式
    状态**（status），不再靠 find_pending 从对话块顺序推导。

    响应结构**逐键不变**（前端零改动的硬依据）：`items` ＋ `unread_by_card`。
    - `items` ＝ **inbox 全量（含 consumed）∪ 旧档案扫描的 `card:` 块**，按 `ts`
      倒序、`msg_id` 去重。两源并存是**过渡形态**：旧机制的信没进 inbox，不并
      进去那 953 条历史就在通知面板里消失（V3）。同一 msg_id 不会双源出现
      （旧机制的落对话块、新机制的落收件箱），去重只是防御。
    - `format_ok`：inbox 行都是结构化写入的 ⇒ 恒 `true`；只有旧档案块可能头行
      解析失败（前端据此归「格式异常」分组，恒真不影响分组正确性）。
    - `summary` ＝ 剥掉首行机器头行后的正文前 80 字（两源同语义，前端展示不变）。
    - `type_display` 仍由**后端**从注册表取（前端零硬编码；未知类型返回 code）。
    - `unread_by_card`：**只**看 inbox 的 `status=pending`，每卡二值计 1，无 pending
      的卡不出现在字典里。旧机制那套 find_pending 推导在此退役。
    - `card_msg_consumed` 打点**已移除**（同批）：该打点的语义是「端点侧发现信已
      被处理」，而 inbox 下 consumed 事实由 `card_db.mark_consumed` 在回合收口时
      落库（message_runner），端点不必再推断。本端点仍**纯读**——本次是删写、
      不是加写。（`card_msg.mark_consumed` 函数本体保留，退役归化石清理批次。）
    - 性能口径如实：inbox 侧是单表索引查询（idx_inbox_work / idx_inbox_target），
      比旧的逐卡整档扫描省得多；但按上面第一条，**旧扫描仍在跑**（历史源），
      故总耗时暂未净改善——两源并存到化石清理批次退役旧源为止（不在本任务）。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    cards = card_store.iter_card_metas(work_id)
    items: list[dict[str, Any]] = []
    unread_by_card: dict[str, int] = {}
    seen: set[str] = set()

    # ── 源一：收件箱（新机制，含 consumed）────────────────────────────
    for row in await card_db.list_inbox(work_id):
        msg_id = str(row.get("msg_id") or "")
        sender = str(row.get("from_card") or "")
        target = str(row.get("target_card_id") or "")
        msg_type = str(row.get("type") or "")
        # 正文＝剥掉首行机器头行（与旧档案源同语义：summary 不含 [[card-msg …]]）
        summary = card_msg.strip_header(str(row.get("content") or ""))[:80]
        items.append({
            "id": msg_id,
            "ts": row.get("created_at") or "",
            "seq": row.get("seq"),
            "from_card": sender,
            "from_title": (cards.get(sender) or {}).get("title", ""),
            "to_card": target,
            "msg_type": msg_type,
            "type_display": card_msg.display_of(msg_type),
            "summary": summary,
            # inbox 行全部经 _h_send_card_message 结构化写入 ⇒ 无解析失败形态
            "format_ok": True,
        })
        if msg_id:
            seen.add(msg_id)
        if row.get("status") == card_db.INBOX_PENDING and target:
            unread_by_card[target] = 1

    # ── 源二：会话档案里的 card: 块（旧机制，仅存量）──────────────────
    for cid in cards:
        blocks = await session_facade.read(work_id, cid)
        for b in blocks:
            if not str(b.get("actor") or "").startswith("card:"):
                continue
            content = str(b.get("content") or "")
            hdr = card_msg.parse_header(content)
            fmt_ok = hdr is not None
            sender = str(b.get("from") or "") or (hdr or {}).get("from", "")
            msg_id = (hdr or {}).get("id") or f"unparsed-{cid}-{b.get('seq', '')}"
            if msg_id in seen:
                continue
            items.append({
                "id": msg_id,
                "ts": b.get("ts") or "",
                "seq": b.get("seq"),
                "from_card": sender,
                "from_title": (cards.get(sender) or {}).get("title", ""),
                "to_card": cid,
                "msg_type": (hdr or {}).get("type", ""),
                "type_display": card_msg.display_of((hdr or {}).get("type", "")) if fmt_ok else "",
                "summary": card_msg.strip_header(content)[:80],
                "format_ok": fmt_ok,
            })

    items.sort(key=lambda x: str(x["ts"]), reverse=True)
    if limit >= 0:
        items = items[:limit]
    return {"items": items, "unread_by_card": unread_by_card}


@router.get("/works/{work_id}/canvas")
async def get_work_canvas(work_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    # [画布第二步 D2] 读路径合并：positions 由用户坐标存储供给，canvas.json
    # 只出派生数据——响应结构逐键不变（V5），前端零改动（L5）。
    # [画布重构 2026-10-15] 用户坐标的真相来源从 layout.json 换成 cards 表的
    # 坐标列，**本行的键名、类型、嵌套形状一字未动**。
    data = canvas_store.load(work_id)
    data["positions"] = await canvas_store.load_positions(work_id)
    return data


@router.patch("/works/{work_id}/canvas/positions")
async def patch_work_positions(work_id: str, req: PatchPositionsRequest):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    # 响应信封逐字段不变（不变量 L5：前端零改动是本次换存储的硬边界）。
    # ★ 信封在**端点层**成形而不是存储层：存储层只回"合并后的全量坐标"，
    # "version" 这个历史包袱由 API 层扛着——存储层不该知道有个 layout 文档
    # 版本号可言（layout.json 已退役）。
    return {"version": 1,
            "positions": await canvas_store.merge_positions(work_id, req.positions)}


@router.post("/works/{work_id}/cards")
async def create_work_card(work_id: str, req: CreateCardRequest):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    if req.source not in card_store.ALLOWED_SOURCE:
        return _err(400, f"source 非法: {req.source}，只允许 {list(card_store.ALLOWED_SOURCE)}")
    try:
        result = await card_store.create_card(
            work_id, req.title, req.source,
            card_type=req.card_type, parent=req.parent,
            charter=req.charter, conclusion=req.conclusion,
            goal=req.goal,   # [自动模式 G1] HTTP 建卡透传 goal（缺省空=不变）
        )
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    if "error" in result:
        return _err(400, result["error"])
    # [规则卡 2026-10-15] 建卡即播种六区空模板（mission 列；单一写闸 update_card）
    if req.card_type == "rule":
        card_id = (result.get("meta") or {}).get("card_id", "")
        if card_id:
            try:
                await card_store.update_card(
                    work_id, card_id, {"mission": _rule_template_body()},
                    "规则卡创建：播种六区空模板",
                )
            except CardLockedError:
                pass  # 播种失败不阻断建卡（面板可后补）
            except Exception as exc:  # noqa: BLE001
                logger.warning("规则卡模板播种失败（不阻断）work=%s: %s", work_id, exc)
    return JSONResponse(status_code=201, content=result)


@router.get("/works/{work_id}/cards/{card_id}")
async def get_work_card(work_id: str, card_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    try:
        result = await card_store.read_card(work_id, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    # [卡片编辑机制改造 2026-10-12 · 块1] 读侧按块集判据过滤 body 键
    return _apply_block_set(result, work_id)


# [用户删卡 2026-10-02] 仅前端调用；模型工具清单不含删卡（模型无权删卡）
@router.delete("/works/{work_id}/cards/{card_id}")
async def delete_work_card(work_id: str, card_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    try:
        result = await card_store.delete_card(work_id, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    except CardDeleteDeniedError as exc:
        # 根卡与带子卡的卡拒绝删除（结构性防护，机械判据）
        is_root = card_id == work_store.work_card_id_of(work_id)
        return _err(403 if is_root else 400, str(exc))
    if "error" in result:
        return _err(400, result["error"])
    return result


@router.patch("/works/{work_id}/cards/{card_id}")
async def patch_work_card(work_id: str, card_id: str, req: PatchCardRequest):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    if not req.history_note.strip():
        return _err(400, "history_note 必填非空")
    req.updates.pop("status", None)  # [phase8.3] 收到 status 忽略，不报错
    illegal = set(req.updates) - ALLOWED_UPDATE_KEYS
    if illegal:
        return _err(400, f"updates 含不允许的键: {sorted(illegal)}")
    try:
        result = await card_store.update_card(work_id, card_id, req.updates, req.history_note)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    if "error" in result:
        return _err(400, result["error"])
    return _apply_block_set(result, work_id)


@router.get("/works/{work_id}/cards/{card_id}/session")
async def get_work_session(work_id: str, card_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    try:
        await card_store.read_card(work_id, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    # [A6a/T069-T072] 读切换：统一读入门面（V1 委托旧解析，输出五键契约不变）
    return await session_facade.read(work_id, card_id)


# [卡片编辑机制改造 2026-10-12 · 块2] 起草/确认写入两路由。
# draft 独立于 message_runner（无工具纯文本调用，不进工具循环）；
# commit 是写入唯一入口（hash 比对 → update_card 整块替换 → 影子记迭代历史）。
@router.post("/works/{work_id}/cards/{card_id}/draft")
async def post_work_draft(work_id: str, card_id: str, req: DraftRequest):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    from . import card_draft
    result = await card_draft.draft(
        work_id, card_id, req.kind,
        feedback=req.feedback, prev_draft=req.prev_draft,
    )
    if "error" in result:
        return _err(400, result["error"])
    return result


@router.post("/works/{work_id}/cards/{card_id}/commit")
async def post_work_commit(work_id: str, card_id: str, req: CommitRequest):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    from . import card_draft
    result = await card_draft.commit(
        work_id, card_id, req.kind, req.draft, req.base_hash,
        feedback_rounds=req.feedback_rounds,
    )
    if "error" in result:
        # hash 冲突走 409 语义（前端弹窗提示重新起草）；其余 400
        status = 409 if result.get("code") == "hash_mismatch" else 400
        return _err(status, result["error"])
    return result


# ============ [规则卡 2026-10-15]规则卡分区读写与审批落盘 ============
# 规则卡正文整体存 mission 列（单一写闸＝card_store.update_card）；分区＝
# ## {区名} 行级切分（context_regions.split_rule_sections 纯函数）；hash＝
# sha256(区内容)（card_draft 同口径）。仅根卡经 draft_rule_edit 提议、
# 用户弹窗确认后走 /rule/commit 落盘；面板直改走 PATCH，即改即存。

def _find_rule_card(work_id: str) -> Optional[dict[str, Any]]:
    """查本 work 首张 rule 卡（{card_id, mission}）；无 → None。"""
    def _fn(conn: Any) -> Optional[dict[str, Any]]:
        row = conn.execute(
            "SELECT card_id, mission FROM cards WHERE card_type = 'rule' "
            "ORDER BY card_id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {"card_id": row["card_id"], "mission": row["mission"] or ""}
    return card_db.read_sync(work_id, _fn, None)


def _rule_section_names() -> list[str]:
    """合法分区名＝词表 codes（root 在前、去重、声明序）。"""
    return context_regions.rule_section_names(card_store.load_type_registry())


def _rule_section_hash(content: str) -> str:
    """区内容指纹：sha256 hex（card_draft._block_hash 同算法）。"""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _replace_rule_section(mission: str, section: str, content: str) -> str:
    """行级外科手术：只替换 `## {section}` 区内容，其它区字节不变。

    缺区则文尾追加新区（含区头）。mission 原样保留（含区头行）。
    """
    lines = (mission or "").split("\n")
    hdr = f"## {section}"
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == hdr:
            start = i
            break
    new_body_lines = (content or "").split("\n") if content else []
    if start is None:
        # 缺区：文尾追加
        base = "\n".join(lines).rstrip("\n")
        block = hdr + ("\n" + "\n".join(new_body_lines) if new_body_lines else "")
        return (base + "\n\n" if base else "") + block
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    rebuilt = lines[: start + 1] + new_body_lines + lines[end:]
    return "\n".join(rebuilt)


def _rule_template_body() -> str:
    """规则卡初建空模板：六区齐、内容空。"""
    return "\n\n".join(f"## {n}" for n in _rule_section_names())


@router.get("/works/{work_id}/rule")
async def get_work_rule(work_id: str):
    """规则面板读口：六区齐全（含空区）、hashes、正被 N 类卡注入。"""
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    names = _rule_section_names()
    counts = context_regions.rule_inject_counts(card_store.load_type_registry())
    row = _find_rule_card(work_id)
    if row is None:
        return {"exists": False, "card_id": "",
                "sections": {n: "" for n in names},
                "hashes": {n: _rule_section_hash("") for n in names},
                "inject_type_counts": {n: counts.get(n, 0) for n in names}}
    sections = context_regions.split_rule_sections(row["mission"])
    sections.pop("", None)  # 区头前游离文不外露
    out_sections = {n: sections.get(n, "") for n in names}
    hashes = {n: _rule_section_hash(out_sections[n]) for n in names}
    return {"exists": True, "card_id": row["card_id"],
            "sections": out_sections, "hashes": hashes,
            "inject_type_counts": {n: counts.get(n, 0) for n in names}}


@router.patch("/works/{work_id}/rule/sections/{section}")
async def patch_work_rule_section(work_id: str, section: str,
                                  req: PatchRuleSectionRequest):
    """面板直改即存：hash 比对（base_hash 不符→409），经 update_card 写闸。"""
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    names = _rule_section_names()
    if section not in names:
        return _err(400, f"非法区名: {section}（只允许 {names}）")
    row = _find_rule_card(work_id)
    if row is None:
        return _err(404, "规则卡不存在（本 work 尚未创建 rule 卡）")
    sections = context_regions.split_rule_sections(row["mission"])
    cur = sections.get(section, "")
    old_hash = _rule_section_hash(cur)
    if req.base_hash != old_hash:
        return _err(409, f"hash 不符（当前区 hash={old_hash[:16]}），请刷新后重试")
    new_mission = _replace_rule_section(row["mission"], section, req.content)
    try:
        result = await card_store.update_card(
            work_id, row["card_id"], {"mission": new_mission},
            f"用户面板保存规则区 {section}",
        )
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    if "error" in result:
        return _err(400, result["error"])
    new_hash = _rule_section_hash(req.content)
    return {"ok": True, "card_id": row["card_id"], "section": section,
            "old_hash": old_hash, "new_hash": new_hash}


@router.post("/works/{work_id}/rule/commit")
async def post_work_rule_commit(work_id: str, req: RuleCommitRequest):
    """根卡提议审批落盘：draft_rule_edit 产物经用户「同意」后写入。

    hash 比对同 PATCH（不符→409）；落盘后影子 ## 迭代历史 记一行
    （时间+section+旧/新hash+来源=根卡审批）。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    names = _rule_section_names()
    if req.section not in names:
        return _err(400, f"非法区名: {req.section}（只允许 {names}）")
    row = _find_rule_card(work_id)
    if row is None:
        return _err(404, "规则卡不存在（本 work 尚未创建 rule 卡）")
    sections = context_regions.split_rule_sections(row["mission"])
    cur = sections.get(req.section, "")
    old_hash = _rule_section_hash(cur)
    if req.base_hash != old_hash:
        return _err(409, f"hash 不符（当前区 hash={old_hash[:16]}），请重新提议")
    new_mission = _replace_rule_section(row["mission"], req.section, req.content)
    try:
        result = await card_store.update_card(
            work_id, row["card_id"], {"mission": new_mission},
            f"根卡提议审批写入规则区 {req.section}",
        )
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    if "error" in result:
        return _err(400, result["error"])
    new_hash = _rule_section_hash(req.content)
    # 影子 ## 迭代历史 记一行（失败不阻断，与 card_draft.commit 同口径）
    from ..storage import shadow_store
    from datetime import datetime as _dt
    try:
        await shadow_store.append_to_section(
            work_id, row["card_id"], "## 迭代历史",
            [f"- [{_dt.now().astimezone().isoformat()}] 规则审批 {req.section} "
             f"旧hash={old_hash[:16]} 新hash={new_hash[:16]} 来源=根卡审批"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("规则审批影子记失败（不阻断）work=%s: %s", work_id, exc)
    return {"ok": True, "card_id": row["card_id"], "section": req.section,
            "old_hash": old_hash, "new_hash": new_hash}


@router.get("/works/{work_id}/cards/{card_id}/timeline")
async def get_card_timeline(work_id: str, card_id: str, frm: int = Query(0, alias="from")):
    """[消息底座能力对齐 · 件4] 恢复唯一入口（C3/C4）：合并时序表。

    items＝消息块（user_message/assistant_text/msg_*）＋过程事件（tool_call/
    tool_result/judgment/step_*/seat_*/…）＋思考（reasoning），按 seq 升序、
    每条自带 ``type``，前端按 type 直接渲染。被 revoke 覆盖的段已剔除（软删）。

    恢复统一走本口：内存 journal 是进程内的（重启即失），events jsonl 才是
    可回溯的时序真相（C4 二合一——过程与思考合流，不再分 /stream 与 /tokens
    两路重放）。实时增量仍走两条 SSE（见下方端点）。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    try:
        await card_store.read_card(work_id, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    items = event_log.timeline(work_id, card_id, from_seq=frm)
    return {
        "work_id": work_id,
        "card_id": card_id,
        "items": items,
        "last_seq": items[-1]["seq"] if items else 0,
    }


# [消息底座能力对齐 · 件4] 任务书字面的卡级入口（无 work 路径段）：work_id 由
# 查询参数给定，缺省转发 work_001（与既有 /api/cards/{id}/… 兼容层同口径）。
@router.get("/card/{card_id}/timeline")
async def get_card_timeline_compat(card_id: str, work_id: str = DEFAULT_WORK_ID,
                                   frm: int = Query(0, alias="from")):
    # frm 显式传：直接调路由函数时 Query(...) 默认值是 Query 对象而非 0
    return await get_card_timeline(work_id, card_id, frm)


@router.get("/works/{work_id}/cards/{card_id}/stream")
async def stream_card_events(work_id: str, card_id: str, frm: int = Query(0, alias="from")):
    """[信息流 Task3] SSE 过程通道：内存事件日志（event_stream）按 seq 跟进。

    [消息底座能力对齐 · 件4] **from=0 整卡重放退役**（C4）：起流游标落在当前
    末帧——只推「连上之后新发生的事」。整卡恢复统一走 ``GET .../timeline``
    （events jsonl，跨重启可回溯）；内存 journal 不再充当恢复源（进程内、
    重启即失）。``from>0`` 的断点续传语义不动（同一连接按 seq 游标跟进）。
    事件纯展示层，不进 session/prior（A42）；单进程内存态（A44）。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")

    async def gen():
        yield "retry: 3000\n\n"
        # from=0 → 游标落在末帧（零重放）；from>0 → 断点续传（逐字不变）
        last = frm - 1 if frm > 0 else event_stream.journal_last_seq(work_id, card_id)
        idle = 0.0
        while True:
            batch = [e for e in event_stream.journal_of(work_id, card_id) if e["seq"] > last]
            if batch:
                for evt in batch:
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                    last = evt["seq"]
                idle = 0.0
                continue
            await asyncio.sleep(0.4)
            idle += 0.4
            if idle >= 15.0:
                yield ": ping\n\n"
                idle = 0.0

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/works/{work_id}/cards/{card_id}/tokens")
async def stream_card_tokens(work_id: str, card_id: str, frm: int = Query(0, alias="from")):
    """[信息流重出 Task4] SSE 逐字流通道（**独立**于 /stream 的过程卡流）。

    与既有 17 类事件隔离申报（红线4）：独立 journal、不占主 seq。帧形态与
    /stream 同构：{"seq","ts","type","data"}，type ∈ {reasoning_chunk,
    content_chunk}。

    [消息底座能力对齐 · 件4] **from=0 整卡重放退役**（C4，同 /stream）：
    思考的整卡恢复统一走 ``GET .../timeline``（reasoning 已落 events jsonl，
    裁定 C2）。``from>0`` 断点续传不动。

    纯展示层：不进 session/prior/模型输入（红线3/A42）；**思考增量落盘**
    （events jsonl，C2）——不落盘的只有正文增量 content_chunk（C7）。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")

    async def gen():
        yield "retry: 3000\n\n"
        last = frm - 1 if frm > 0 else event_stream.token_journal_last_seq(work_id, card_id)
        idle = 0.0
        while True:
            batch = [e for e in event_stream.token_journal_of(work_id, card_id) if e["seq"] > last]
            if batch:
                for evt in batch:
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                    last = evt["seq"]
                idle = 0.0
                continue
            await asyncio.sleep(0.25)
            idle += 0.25
            if idle >= 10.0:
                yield ": ping\n\n"
                idle = 0.0

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/works/{work_id}/cards/{card_id}/messages")
async def post_work_message(work_id: str, card_id: str, req: PostMessageRequest):
    """对话主链路（模块 H2）。"""
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    # [编程卡 2026-10-15 · §六块3 静默判定①] 完工编程卡不再被激活：
    # 只读 card_type/state 两字段的**机械判定**（任务书红线 2），在 pair_id
    # 进入回合前拦下。读卡失败（卡不存在等）不在此处置，仍走 _run_message
    # 既有口径（静默判定只多不少地拦，不改既有错误语义）。
    try:
        _meta = (await card_store.read_card(work_id, card_id)).get("meta") or {}
    except Exception:  # noqa: BLE001
        _meta = {}
    if str(_meta.get("card_type") or "") == "coder" \
            and str(_meta.get("state") or "") == "completed":
        return _err(400, "编程卡已完工，不再激活；新任务请新建编程卡")
    # [自动模式 2026-10-17 · 回灌 5700 块0.3 · D2] auto 卡收到**首条消息** ⇒
    # 该消息成为 goal、goal_state=active（起跑条件齐备；本轮跑完后驱动器自动
    # 续轮）。已有目标 ⇒ 不动（中途引导，目标不变——方案 §3.4）。非 auto 卡
    # ⇒ no-op（红线 2：非 auto 卡零变化）。
    try:
        await auto_driver.ensure_goal_from_message(work_id, card_id, req.content)
    except Exception as exc:  # noqa: BLE001 目标设定失败不阻断本次消息
        logger.warning("[auto] 首条消息设定目标失败 work=%s card=%s: %s",
                       work_id, card_id, exc)
    return await _run_message(work_id, card_id, req.content, req.pair_id,
                              op=req.op or "")


# ============================================================
# [消息底座能力对齐 · 件5] 消息编辑 / 重发（C6）
# ============================================================

class EditMessageRequest(BaseModel):
    content: str = Field(min_length=1)


def _find_message_record(work_id: str, card_id: str, seq: int,
                         want_type: str) -> Optional[dict[str, Any]]:
    """按事件 seq 取一条指定类型的记录（找不到＝None）。

    seq 是**事件流**的 seq（timeline 渲染面用的就是它），不是会话块 seq——
    用户点的是屏幕上的那一条，屏幕渲染的是 timeline。
    """
    for rec in event_log.read(work_id, card_id):
        if int(rec.get("seq") or -1) == int(seq) and rec.get("type") == want_type:
            return rec
    return None


def _is_revoked(work_id: str, card_id: str, seq: int) -> bool:
    """该 seq 是否已被某条 revoke 覆盖（软删判定）。

    判据：存在 revoke{from_seq:F} 记录，其自身 seq 大于本条（＝作废在它之后
    发生）且 F ≤ 本条 seq。
    """
    for rec in event_log.read(work_id, card_id):
        if rec.get("type") != "revoke":
            continue
        try:
            f = int((rec.get("data") or {}).get("from_seq"))
            r_seq = int(rec.get("seq"))
        except (TypeError, ValueError):
            continue
        if r_seq > seq and f <= seq:
            return True
    return False


def _has_valid_reply(work_id: str, card_id: str, pair_id: str) -> Optional[bool]:
    """该提问（run_id）在事件流里是否已有**未作废**的回复。

    返回 None＝事件流无判据（无事件文件/找不到该提问记录，典型＝存量卡未
    迁移）→ 调用方回落既有「会话块」口径（判定语义逐字不变）。
    """
    try:
        items = event_log.timeline(work_id, card_id)
    except Exception:  # noqa: BLE001 事件流不可读＝无判据，回落既有口径
        return None
    idx = None
    for i, rec in enumerate(items):
        if rec.get("type") == "user_message" and str(rec.get("run_id") or "") == pair_id:
            idx = i
            break
    if idx is None:
        return None
    for rec in items[idx + 1:]:
        if rec.get("type") == "assistant_text" and str(rec.get("run_id") or "") == pair_id:
            return True
    return False


@router.post("/works/{work_id}/cards/{card_id}/messages/{seq}/edit")
async def edit_work_message(work_id: str, card_id: str, seq: int, req: EditMessageRequest):
    """编辑历史提问：revoke 该条及其后全部 → 以**新 run_id** 重生成（C6）。

    - 定位用事件流 seq（屏幕渲染的就是 timeline 的 seq）；
    - revoke 是**软删**：追加一条 revoke 记录标记 from_seq 起作废，不物理删字节；
    - 新回合走全新 pair_id ⇒ 会话文件追加一条新提问块（编辑后的正文）＋新回复；
    - 会话 .md 是 append-only 真相源，旧块仍在盘上（本任务不引入会话级软删，
      属下一步；timeline 渲染面已不显示被 revoke 的段）。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    rec = _find_message_record(work_id, card_id, seq, "user_message")
    if rec is None:
        return _err(404, f"事件流中无此提问记录: seq={seq}")
    if _is_revoked(work_id, card_id, seq):
        return _err(404, f"该提问已被作废（seq={seq}），请对最新一条提问操作")
    await event_log.revoke(work_id, card_id, seq)
    session_ops.ops_log("message_edit", work_id, card_id,
                        {"seq": seq, "run_id": str(rec.get("run_id") or "")})
    logger.info("[件5] 编辑重生成 work=%s card=%s seq=%s（旧 run_id=%s 已作废）",
                work_id, card_id, seq, rec.get("run_id") or "")
    return await _run_message(work_id, card_id, req.content, None)


@router.post("/works/{work_id}/cards/{card_id}/messages/{seq}/resend")
async def resend_work_message(work_id: str, card_id: str, seq: int):
    """重发历史提问：作废旧回复 → **复用原 pair_id** 重生成（C6）。

    复用 pair_id 是去重机制（既有「后端二次判定」）：判定口径扩展为「该提问
    在事件流里是否已有未作废的回复」——旧回复已被本口 revoke ⇒ 判为无回复
    ⇒ 不重复追加提问块，只重新产出回复。事件流无判据（存量未迁移卡）时
    回落既有会话块口径，行为逐字不变。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    rec = _find_message_record(work_id, card_id, seq, "user_message")
    if rec is None:
        return _err(404, f"事件流中无此提问记录: seq={seq}")
    if _is_revoked(work_id, card_id, seq):
        return _err(404, f"该提问已被作废（seq={seq}），请对最新一条提问操作")
    run_id = str(rec.get("run_id") or "")
    content = str((rec.get("data") or {}).get("content") or "")
    if not content.strip():
        return _err(400, "该提问记录正文为空，无法重发")
    # 只作废回复（提问保留）：找同 run_id 的第一条 assistant_text
    for later in event_log.read(work_id, card_id, from_seq=int(rec["seq"]) + 1):
        if later.get("type") == "assistant_text" and str(later.get("run_id") or "") == run_id:
            await event_log.revoke(work_id, card_id, int(later["seq"]))
            break
    session_ops.ops_log("message_resend", work_id, card_id, {"seq": seq, "run_id": run_id})
    logger.info("[件5] 重发重生成 work=%s card=%s seq=%s（复用 run_id=%s）",
                work_id, card_id, seq, run_id)
    return await _run_message(work_id, card_id, content, run_id or None)


# [A8/R3-T119-T123] 人停端点：置位控制面（consume-once，接口被消费才算数）；
# 无在途回合＝幂等 no-op（不报错、不造轮）。
@router.post("/works/{work_id}/cards/{card_id}/stop")
async def post_work_stop(work_id: str, card_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    # [自动模式 2026-10-17 · 回灌 5700 块0.3] 停止三件：①停当轮（request_stop，
    # consume-once）②关自动继续＋级联子卡＋释放执行锁（stop_auto）。非 auto 卡
    # ⇒ stop_auto 全 no-op，停止行为逐字不变（红线 2）。合并返回（前端一次拿到
    # 全量结果）。
    res = control.request_stop(work_id, card_id)
    try:
        auto_res = await control.stop_auto(work_id, card_id)
    except Exception as exc:  # noqa: BLE001 停止路径绝不抛
        logger.error("[control] stop_auto 异常 work=%s card=%s: %s",
                     work_id, card_id, exc)
        auto_res = {"paused": [], "lock_released": False}
    return {**res, **auto_res}


# [实时反馈 2026-10-02] 调用进度查询：纯展示层，前端轮询用。
# 只读内存态，不进任何模型输入，不拦截任何调用——不是闸。
@router.get("/works/{work_id}/cards/{card_id}/progress")
async def get_work_progress(work_id: str, card_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    st = progress.get_stage(work_id, card_id)
    return st if st is not None else {"stage": "idle", "detail": "", "ts": 0.0}


# [phase8.5] 原文只读端点（给未来前端留口，不加写端点）
@router.get("/works/{work_id}/manuscript")
async def list_work_manuscript(work_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    return {"chapters": manuscript_store.list_chapters(work_id)}


@router.get("/works/{work_id}/manuscript/{chapter}")
async def read_work_manuscript(
    work_id: str,
    chapter: str,
    para_start: Optional[int] = None,
    para_end: Optional[int] = None,
):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    try:
        return manuscript_store.read_chapter(
            work_id, chapter, para_start=para_start, para_end=para_end,
        )
    except ChapterNotFoundError:
        return _err(404, f"章节不存在: {chapter}")


# [无内容预设改造 2026-10-02] 方法论库三端点（GET /methodology 等）已删除：
# 模型自产内容入库并回流注入是自我繁殖污染链（m_0001/m_0002 事件）。


# ============================================================
# 兼容层（旧前端不断服）：固定转发 work_001
# ============================================================

@router.get("/canvas")
async def get_canvas_compat():
    # [画布第二步 D2] 兼容端点随主端点一并合并 positions（RD-06：不许留死角——
    # load() 返回里不再有 positions，不改这里会静默丢键）。
    # [画布重构 2026-10-15] 同主端点改走 cards 表坐标列，响应结构不变。
    return {**canvas_store.load(DEFAULT_WORK_ID),
            "positions": await canvas_store.load_positions(DEFAULT_WORK_ID)}


@router.post("/cards")
async def create_card_compat(req: CreateCardRequest):
    if req.source not in card_store.ALLOWED_SOURCE:
        return _err(400, f"source 非法: {req.source}，只允许 {list(card_store.ALLOWED_SOURCE)}")
    try:
        result = await card_store.create_card(
            DEFAULT_WORK_ID, req.title, req.source,
            card_type=req.card_type, parent=req.parent,
            charter=req.charter, conclusion=req.conclusion,
            goal=req.goal,   # [自动模式 G1] 兼容层建卡同口径透传 goal
        )
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    if "error" in result:
        return _err(400, result["error"])
    return JSONResponse(status_code=201, content=result)


@router.get("/cards/{card_id}")
async def get_card_compat(card_id: str):
    try:
        result = await card_store.read_card(DEFAULT_WORK_ID, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    return _apply_block_set(result, DEFAULT_WORK_ID)


@router.patch("/cards/{card_id}")
async def patch_card_compat(card_id: str, req: PatchCardRequest):
    if not req.history_note.strip():
        return _err(400, "history_note 必填非空")
    req.updates.pop("status", None)  # [phase8.3] 收到 status 忽略，不报错
    illegal = set(req.updates) - ALLOWED_UPDATE_KEYS
    if illegal:
        return _err(400, f"updates 含不允许的键: {sorted(illegal)}")
    try:
        result = await card_store.update_card(DEFAULT_WORK_ID, card_id, req.updates, req.history_note)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    if "error" in result:
        return _err(400, result["error"])
    return _apply_block_set(result, DEFAULT_WORK_ID)


@router.get("/cards/{card_id}/session")
async def get_session_compat(card_id: str):
    try:
        await card_store.read_card(DEFAULT_WORK_ID, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    # [A6a/T069-T072] 读切换（兼容层）：统一读入门面
    return await session_facade.read(DEFAULT_WORK_ID, card_id)


@router.post("/cards/{card_id}/messages")
async def post_message_compat(card_id: str, req: PostMessageRequest):
    return await _run_message(DEFAULT_WORK_ID, card_id, req.content, req.pair_id)


# ============================================================
# messages 主链路（工具循环 + 建卡协议闭环）
# ============================================================

async def _run_message(work_id: str, card_id: str, content: str,
                       client_pair_id: Optional[str] = None,
                       append_user: bool = True,
                       op: str = "") -> Any:
    """完整 messages 链路，新旧路由共用。

    顺序：读卡 → 取历史 → 追加用户消息 → context_assembler 组装 →
    prompts.build_system_prompt → 带工具的工具循环 → parse_proposals →
    apply_proposals → 写 prompt 日志 → 追加 assistant 消息 → 返回。

    [T10] client_pair_id：前端重发时携带的被重发提问原 pair_id（正常提问为 None）。
    [迭代四 K3] append_user：卡间调起复用主链时传 False——激励文只走当轮
    user_prompt，绝不落收件方档案（「假人声混进档案」禁区）。默认 True ⇒
    全部既有调用方（三参形态）行为逐字不变。
    """
    # [信息流重出 Task4] 标记本请求的逐字流归属卡。model_service.call_main 经
    # contextvar 取到它，才把增量落到 /tokens 独立流；函数退出时还原，
    # 避免污染同进程后续请求（凝练/归拢等无卡调用自然无逐字流）。
    _stream_ctx_token = event_stream.set_stream_context(work_id, card_id)
    # [A4/S4-T083-T084] 主线回合登记逻辑互斥体：同卡已有在途（主线回合或凝练
    # 支线共用一登记处，T086）→ 409 拒绝、不排队（10-08 口径；排队策略归迭代二
    # T085）。owner=turn:{uuid 前 8 位}；拒绝经 ops 通道留痕。
    turn_owner = f"turn:{uuid.uuid4().hex[:8]}"
    if not session_facade.turn_try_acquire(work_id, card_id, turn_owner):
        holder = session_facade.turn_holder(work_id, card_id)
        session_ops.ops_log("mutex_reject", work_id, card_id,
                            {"owner": turn_owner, "holder": holder})
        logger.warning("[A4] 同卡在途拒绝 409 work=%s card=%s holder=%s",
                       work_id, card_id, holder)
        event_stream.reset_stream_context(_stream_ctx_token)
        return _err(409, f"卡片回合进行中（{holder}），请稍后重试")
    # [自动模式 2026-10-17 · 回灌 5700 块0.3 · 方案 §3.8] 起回合前经**执行闸**：
    # 串行模式下拿不到锁＝**排队等待**（await，不是 409——这是与上面卡互斥体的
    # 关键差别）；并行模式直接过；同卡已持有（＝本卡目标生命周期内继续跑）⇒
    # no-op。取在互斥体之后：同卡重入被互斥体挡在前面，闸上不会出现同卡双取。
    await serial_gate.acquire(work_id, card_id)
    failed = False
    try:
        result = await _run_message_inner(work_id, card_id, content, client_pair_id,
                                          append_user=append_user, op=op)
    except BaseException:
        # 异常＝回合未正常收口（dsh 口径：异常结果**不自动重试**）⇒ 本卡不再起轮，
        # 执行锁必须在 finally 里放掉，否则串行队列全部饿死。
        failed = True
        raise
    finally:
        # [A8 清扫位] 回合内未被步边界消费的残留停止置位（典型：无工具轮，
        # 调用返回后直接交付）——收口清扫，防误伤下一回合（T124）。
        control.discard_stop(work_id, card_id)
        session_facade.turn_release(work_id, card_id, turn_owner)
        event_stream.reset_stream_context(_stream_ctx_token)
        if failed:
            serial_gate.release(work_id, card_id)
    # ---- [自动模式 2026-10-17 · 回灌 5700 块0.3 · 红线 5] 驱动检查排在
    # **互斥体释放之后** ----
    # 结构＝「取结果 → finally 释放 → 后置 → 返回」（编排语义逐字不变：返回同一个
    # result）。顺序不可调换：互斥体未释放就起下一轮 ⇒ turn_try_acquire 直接 409，
    # 驱动器空转。
    # 执行锁：本卡还要继续跑目标 ⇒ **保留**（「一张卡的整个目标生命周期」持有，
    # 满足 U6/U8「一张子卡跑完再下一张」）；否则释放，让排队者前进。
    try:
        keep = auto_driver.will_continue(work_id, card_id)
    except Exception as exc:  # noqa: BLE001 判据异常不得吃掉回合结果
        logger.warning("[auto] 回合末执行锁判定异常 work=%s card=%s: %s",
                       work_id, card_id, exc)
        keep = False
    if not keep:
        serial_gate.release(work_id, card_id)
    try:
        await auto_driver.after_turn(work_id, card_id)
    except Exception as exc:  # noqa: BLE001 驱动器异常不影响本回合返回
        logger.error("[auto] 驱动检查异常 work=%s card=%s: %s", work_id, card_id, exc)
    return result


async def _run_message_inner(work_id: str, card_id: str, content: str,
                             client_pair_id: Optional[str] = None,
                             append_user: bool = True,
                             op: str = "") -> Any:
    """_run_message 的实际链路体（外层只负责逐字流 contextvar 的设/还原）。

    [T-0009 拆分] 本函数**只做三件事**：① 装配（读卡/pair 去重/上下文）
    → ② 循环步进（step/state）→ ③ 落盘与返回。六段编排细节已迁至
    `message_runner`（RunState ＋ step ＋ check_control 中止缝）。
    **编排语义逐字不变**——判据＝tests/_golden/ 三剧本金样本逐字节回放一致。
    """
    # 段① 读卡 ＋ pair_id 去重追加（详见旧 L524-564，逻辑逐字迁移）
    state = message_runner.RunState(work_id=work_id, card_id=card_id, content=content)
    # [更新现状按钮改造 2026-10-16] 本轮操作标记：op 回合触发专用注入与专用工具，
    # 缺省空串 ⇒ 普通回合逐字不变。
    state.op = op

    # a. 确认卡片存在
    try:
        await card_store.read_card(work_id, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")

    # a0. [T10-1-1] 读取历史之前生成本次请求标识（UUID4 十六进制串），打点含标识与卡号
    pair_id = uuid.uuid4().hex
    state.pair_id = pair_id
    logger.info("[T10] 生成请求标识 pair_id=%s card=%s", pair_id, card_id)

    # b. 先取历史（此时不含本条）
    # [A6a/T069-T072] 读切换：统一读入门面（五键契约不变，重发去重/装配同尺）
    prior = await session_facade.read(work_id, card_id)
    state.prior = prior

    # c. 追加用户消息
    # [T10-3-3] 后端二次判定（最终仲裁，3-5）：请求携带的 pair_id 已存在且其后无回复条
    # → 复用而不追加。判定口径只有这一条，不含「时间接近」类模糊条件（3-4）；
    # 前端漏判时后端拦住。
    # 〔代拍 R-048〕4.3 后端二次判定（改 routes.py:346）——此条由诸葛代拍，用户可事后推翻；
    # 依据＝用户 18:58 授权＋ 17:54 立的边界 ＋ 本改动只加判据、不删任何既有行为。
    # 此标注须原样保留，不得抹去（红线 10）。
    reuse_client_pair = False
    if client_pair_id:
        # [消息底座能力对齐 · 件5] 事件流有判据时**以事件流为准**：被 revoke 的
        # 旧回复不算回复 ⇒ 重发可复用原 pair_id 而不重复追加提问块。事件流无
        # 判据（存量卡未迁移／无事件文件）→ 回落既有会话块口径，逐字不变。
        ev_verdict = _has_valid_reply(work_id, card_id, client_pair_id)
        if ev_verdict is None:
            for idx in range(len(prior) - 1, -1, -1):
                if prior[idx].get("pair_id") == client_pair_id:
                    # [工具结果落会话 2026-10-17] 「已有回复」判据＝**不带 tool_calls
                    # 的 assistant 块**（真正的回复块）。工具调用块（actor=assistant
                    # 但带 tool_calls）不是回复——否则一次中断在工具轮的回合会被误判
                    # 「已回复」而不复用 pair_id，导致用户条重复落盘。
                    reuse_client_pair = not any(
                        m.get("role") == "assistant" and not m.get("tool_calls")
                        for m in prior[idx + 1:]
                    )
                    break
        else:
            reuse_client_pair = not ev_verdict
    if reuse_client_pair:
        pair_id = client_pair_id
        state.pair_id = pair_id
        logger.info("[T10] 重发复用 pair_id=%s card=%s（用户条不重复落盘）", pair_id, card_id)
    else:
        # [A6b/T024] 写切换：门面追加（seq/turn/pair 写时分配；user 块不闭合轮）
        # [迭代四 K3] append_user=False（卡间调起）时不落 user 块——激励文绝不
        # 进收件方档案（卡间册 §4.4.4「本方案唯一改既有函数签名处」；默认 True
        # ⇒ 既有调用方行为逐字不变）。重发分支（上方 pair 复用）本就不落盘。
        if append_user:
            await session_facade.append(work_id, card_id,
                                        {"role": "user", "content": content,
                                         "pair_id": pair_id})
            logger.info("[T10] 用户消息落盘 pair_id=%s card=%s", pair_id, card_id)

    # [信息流 Task3] run_start：清该卡 journal 重放（A45）＋过程卡秒现（V1）。
    # 位置＝落盘之后、装配之前——先清后写，context 埋点才能留在 journal 里。
    event_stream.emit(work_id, card_id, "run_start",
                      {"pair_id": pair_id, "chars": len(content)})

    # 段②③前半：上下文组装＋工具列＋轮槽
    message_runner.build_context(state)

    # [思维链真实落地 2026-10-11] 判断归模型（C1）：判断协议随 BASE_PROMPT
    # 进 system 区（prompts.py），模型首轮带全量工具自主判断——直答（空手
    # 正文即答复）或调 create_plan 拆解上板。代码层零判定、零机械预检。
    state.user_prompt = content

    # 段③ 工具循环：**单步推进**（每轮一个显式状态点）
    # [无闸改造 2026-10-02] 工具循环不设轮次封顶：模型停止调用工具即自然结束
    try:
        progress.set_stage(work_id, card_id, "started", "已提交")
        while True:
            outcome = await message_runner.step(state)
            if outcome.kind == "seats":
                outcome = await message_runner.run_seats(state)
            if outcome.kind != "continue":
                break
    except Exception as exc:
        message_runner.handle_error(state, exc)

    # 段④ prompt 日志（无论成败）
    message_runner.write_logs(state)

    if state.error is not None:
        return _err(502, f"模型调用失败: {state.error}")

    # 段⑤ T14 拼接交付（落位在日志写之后、parse_proposals 之前）
    delivered = message_runner.compose_delivery(state)
    state.final_reply = delivered

    # 段⑥ 建卡 · 工具通道 ＋ 文本通道并存，跨通道去重
    await message_runner.create_cards(state, delivered)

    # [迭代四 K3] 卡间来信调起（A11 两行之间，卡间册 §4.4.3）：A 的回合收口时
    # 对本回合记下的全部 pending target 异步起 B 的回合（非阻塞；激励文走
    # append_user=False 不落档；B 忙 409 → 信已在档案、下轮收口补齐）。
    await message_runner.schedule_card_message_triggers(state)

    # 段⑧ assistant 落盘＋回合末凝练触发＋done 埋点＋返回
    return await message_runner.finish(state)
