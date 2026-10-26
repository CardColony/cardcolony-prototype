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
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..storage import (
    activity, canvas_store, card_db, card_store, event_log, function_store,
    manuscript_store, memory_store, session_facade, session_ops, session_store,
    work_store,
)
from ..storage.card_store import CardLockedError, CardNotFoundError
from ..storage.card_store import CardDeleteDeniedError
from ..storage.hash_utils import parse_front_matter, split_body_sections
from ..storage.manuscript_store import ChapterNotFoundError
from . import auto_driver, card_msg, card_proposal, card_tools, control, context_assembler, context_regions, event_stream, message_runner, model_service, progress, prompt_log, prompts, role_room, serial_gate, simple_service, steer, text_tool_rescue, tool_fold

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
    # [自动模式 2026-10-17 · 阶段2冒烟实测补漏] auto_mode 是**用户开关**（U1：
    # 任何卡随时可切），前端 toggleAutoMode 经 PATCH {updates:{auto_mode}} 落库。
    # 本集合漏了它 ⇒ 点击开关被 400「updates 含不允许的键」，开关整体不可用
    # （移植自 Cardlo 时漏抄，冒烟抓到）。goal/goal_state/goal_rounds **不放**
    # ——目标由消息/update_goal 设，不给用户直改通道。
    "auto_mode",
}
# [phase8.3] status 二态已废除：PATCH 带 status 一律忽略（不报错）

# [卡片编辑机制改造 2026-10-12 · 块1] 读侧块集判据：rd_project → 需求文档
# （requirements），其余（research_project 及未分类存量）→ 需求文档（rulings）。
# card_store.read_card 返回的 body 恒含 rulings 键；此处按根卡 project_class
# 过滤——rd 卡不外露 rulings、换 requirements；其他卡不外露 requirements。
# 存量研发卡的需求文档块内容在 rd 卡上以 requirements 呈现（第三块语义随树类走），
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


# [wb_exec 2026-10-23] 执行体选择（谁干活：WorkBuddy 还是系统自己的模型）
class SetExecBackendRequest(BaseModel):
    backend: str = Field(min_length=1)   # auto | system | wb:<版本>:<模型>


# [wb_exec 2026-10-23 16:20] 执行体**总开关**（手动、运行时生效，不用重启）
class SetWbExecSwitchRequest(BaseModel):
    enabled: bool


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
    # [预设职能 2026-10-24] 建项目即按目录播种(总裁落根卡+其余职能建卡)。
    # fail-open:目录未启用/播种失败不挡建项目,只留痕。
    try:
        from . import function_room as _function_room
        seeded = await _function_room.seed_work(str(result.get("work_id") or ""))
        result["functions_seeded"] = seeded
    except Exception as exc:  # noqa: BLE001
        logger.warning("[works] 职能播种失败(不阻断建项目): %s", exc)
        result["functions_seeded"] = {"error": str(exc)}
    # [规则=项目基建 2026-10-25] 建项目即播种默认规则(通用工作流)。
    try:
        await ensure_rules(str(result.get("work_id") or ""))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[works] 规则播种失败(不阻断): %s", exc)
    return JSONResponse(status_code=201, content=result)


@router.post("/works/{work_id}/functions/seed")
async def seed_work_functions(work_id: str):
    """[预设职能 2026-10-24] 给**存量** work 手动播种目录职能(幂等:
    按 jurisdiction.catalog_code 去重,已播种的条目跳过)。不动 work_025 之类的
    历史数据——播种与否由人显式决定。"""
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    from . import function_room as _function_room
    return await _function_room.seed_work(work_id)


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
            # [职能实体化 A 案 2026-10-22] 职能在 cards 表里有一行（为复用基础设施），
            # 但**不进卡列表**——它们走下面单独的 functions 段。
            if str(m.get("card_type") or "") == "function":
                continue
            cards.append(_card_obj(m, act, child_count.get(cid, 0), wid))
        # 两段稳定排序：先并列收尾键 card_id 升序，再按活跃降序（空串排末位）
        cards.sort(key=lambda c: c["card_id"])
        cards.sort(key=lambda c: c["last_active_at"] or "", reverse=True)
        # [角色机制 5710] 角色卡离树，排在项目内所有作业卡之后（左栏一眼分区）。
        cards.sort(key=lambda c: 0 if c.get("card_type") == "role" else -1)
        # [职能入口 2026-10-22 · 用户令「新的 fn 角色卡，前端没有任何入口，
        # 应该像以前的角色卡一样挂在列表上」] 职能**不进 cards 表**（立身之本），
        # 所以它天然不出现在 cards 里 ⇒ 左栏看不到。这里单独给一份清单，
        # 数据源与 GET /works/{wid}/functions 同源（function_profiles），
        # 前端在项目卡列表之后渲染成职能行。读失败不影响 nav（fail-open 给空表）。
        try:
            _functions = [
                {
                    "function_id": p.get("function_id", ""),
                    "function_name": p.get("function_name", ""),
                    "duty": (p.get("duty") or {}).get("state") or "active",
                    "watch": list(((p.get("watch") or {}).get("cards")) or []),
                }
                for p in function_store.list_profiles(wid)
                if p.get("function_id")
                # [总裁=根卡 2026-10-24 · 用户令「不需要单独的总裁卡,两者一体」]
                # 总裁档案落在根卡上——根卡已在卡列表里(其抽屉有总裁台账面板),
                # 职能段**不再重复列出**总裁,否则看起来像多出一张卡。
                and p.get("function_id") != root_id
                # [旧职能清理 2026-10-24] 归档(撤岗)的不再显示——旧职能已全部
                # 归档,台账与判词留痕在库,不销毁,可复活。
                and (p.get("duty") or {}).get("state") != "archived"
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[nav] 读职能失败 work=%s: %s", wid, exc)
            _functions = []
        projects.append({
            "work_id": wid,
            "title": w.get("title", ""),
            "work_card_id": root_id,
            "last_active_at": w.get("last_active_at", ""),
            "card_count": w.get("card_count", 0),
            # [角色机制 5710] 未处理警报数：左栏项目行红点的数据源。
            "open_alarms": len(role_room.list_alarms(wid, "open")),
            "cards": cards,
            # [职能入口 2026-10-22] 职能清单（不在 cards 表里）
            "functions": _functions,
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
        # [职能实体化 A 案 2026-10-22] 职能不进卡树（它们走 functions 段）
        and str(m.get("card_type") or "") != "function"
    ]
    # [角色机制 5710] 角色卡 parent=""，不属于任何一层的子卡；根层展开时
    # 一并带上（排最后），否则异步灌段会把左栏的角色行冲掉。
    if not parent:
        cards.extend(
            _card_obj(m, act, 0, work_id) for m in metas
            if str(m.get("card_type") or "") == "role"
        )
    # 与 /api/nav 同款两段稳定排序（card_id 升序收尾→活跃降序，刷新不跳序）
    cards.sort(key=lambda c: c["card_id"])
    cards.sort(key=lambda c: c["last_active_at"] or "", reverse=True)
    if not parent:
        cards.sort(key=lambda c: 0 if c.get("card_type") == "role" else -1)
    return {"work_id": work_id, "parent": parent, "cards": cards}


@router.get("/health")
async def get_health(request: Request):
    """[服务自报 2026-10-25] 这个服务跑的是**哪份代码**？

    改了后端要重启才生效，而「重启了没」全靠人猜 ⇒ 这里把**启动那一刻**
    关键源文件的 mtime 与 git HEAD 报出来：拿它和盘上文件 mtime 一比，
    盘上更新 ⇒ **服务跑的是旧代码，该重启**。

    另外顺带报三样运维要的数：keep-alive 超时、日志目录、当前访问日志行数。
    """
    stamp = {}
    try:
        stamp = dict(getattr(request.app.state, "build_stamp", {}) or {})
    except Exception:  # noqa: BLE001 取不到就如实报空
        stamp = {}
    logs = card_store.WORKS_DIR.parent / "logs"
    n_access = 0
    try:
        p = logs / "access_log.jsonl"
        if p.exists():
            n_access = sum(1 for _ in p.open("r", encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "build": stamp,
        "logs_dir": str(logs),
        "access_log_lines": n_access,
        "now": time.time(),
    }


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


@router.get("/works/{work_id}/pulse")
async def get_work_pulse(work_id: str, limit: int = 20):
    """画布「活态」：谁在跑 + 最近发生了什么（一次到手）。

    [画布 2026-10-19 · 实时状况] 为什么必须单开一口：
      ① 运行态只在 `progress` 的内存里（**不是**卡片字段），投影（canvas.json）
         是缓存、不能装它——装了就会把"某次运行"冻进缓存，进程重启后还在说"在跑"。
      ② 一屏几十上百张卡，逐张问 `/cards/{cid}/progress` 在 232 卡时是 232 个请求，
         而这里 1 个。
      ③ 活动流要的是"最近发生了什么"，与投影同源（card_history，已滤两类噪音），
         但投影只在 reloadCanvas 时重建；这里每次现读，能反映刚刚的改动。

    返回：
      running: [{card_id, stage, detail, ts}]   —— ts 为 epoch 秒（前端算已跑时长）
      recent:  [{card_id, title, ts, detail}]   —— 最近 limit 条工作事件（时间倒序）
      ts:      服务端当前 epoch 秒
    纯读：不写盘、不拦截、不进模型输入。
    """
    running = [
        r for r in progress.iter_active()
        if str(r.get("work_id") or "") == work_id
    ]
    return {
        "work_id": work_id,
        "running": running,
        "recent": canvas_store.recent_activity(work_id, max(1, min(60, int(limit)))),
        "ts": time.time(),
    }


#: 指纹计算的**专用**线程池（2026-10-25）。
#: 为什么不用默认池：35 个指纹任务（每个都要开一次 SQLite 库、可能等写锁）
#: 会把默认线程池占满 ⇒ **业务自己的** to_thread 工作排队
#: ⇒ 实测一次客户端错误上报被拖到 **6333ms**（访问日志实测）。
#: 专用池有界（8 个）：既拿到并发提速，又不会抢业务的池子。
_FP_EXECUTOR: Optional[ThreadPoolExecutor] = None


def _fp_executor() -> ThreadPoolExecutor:
    """指纹计算的专用线程池（懒建；进程内单例）。"""
    global _FP_EXECUTOR
    if _FP_EXECUTOR is None:
        _FP_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="nav-fp")
    return _FP_EXECUTOR


def _work_fingerprint_sync(wid: str) -> str:
    """[S1·R-06] 单 work 变化指纹：work.json、sessions/*.md 的「路径+大小+mtime」
    全列表哈希，**＋库内 cards.rev**（只读遍历，不侵入任何写路径）。

    〔5715 · 治「切卡卡死」〕改成**同步函数**：调用方（get_nav_version）用
    ``asyncio.to_thread`` 把它挪出事件循环——它是**扫盘**（每 work 扫 work.json ＋
    sessions/*.md ＋ 读 cards.rev）⇒ 放在 async 端点里直接跑会**堵住整个事件循环**
    （前端每 60s 探一次版本 ⇒ 那 60s 里所有请求排队 ⇒ 页面像死了）。

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
    rev = card_db.read_rev(wid)
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
    wids: list[str] = []
    for e in dirs:
        wid = e.name
        if not wid or not e.is_dir():
            continue
        if not (card_store.WORKS_DIR / wid / "work.json").exists():
            continue
        wids.append(wid)
    # 〔P22 · 治「页面卡顿」· 2026-10-25〕**并发**算指纹。
    # 原来是对每个 work **串行** await 一次 to_thread（35 个 work ＝ 35 次开 SQLite 库
    # 串行等）⇒ 实测 68ms（空载）～ **4387ms**（磁盘忙时，访问日志实测）⇒
    # 左栏每 60 秒探一次版本 ⇒ 一次就能顶掉好几秒。
    # 拆开量过：sessions 目录扫描只要 **2ms**（296 个 .md），
    # 大头是 `card_db.read_rev` 的**逐个开库**（串行 358ms → 并发 **19ms**）。
    # 〔5715 · 治「切卡卡死」〕指纹是**同步扫盘**（每 work 扫 work.json ＋
    # sessions/*.md ＋ 读 cards.rev）⇒ 放线程里跑，**不许堵事件循环**
    # （前端每 60s 探一次版本；堵住＝那 60s 内所有请求排队 ⇒ 页面像死了）。
    if wids:
        loop = asyncio.get_running_loop()
        _fps = await asyncio.gather(*(
            loop.run_in_executor(_fp_executor(), _work_fingerprint_sync, w) for w in wids))
        by_work = dict(zip(wids, _fps))
    h = hashlib.sha256()
    for wid in sorted(by_work):
        h.update(f"{wid}|{by_work[wid]}\n".encode("utf-8"))
    return {"version": h.hexdigest(), "by_work": by_work}


async def _unread_by_card(work_id: str, cards: dict, inbox_rows=None) -> dict[str, int]:
    """未读角标＝「收件箱里 seq 超过该卡读取水位(seen_seq)的那些」。

    〔P21 · 治「切页卡死」· 2026-10-25〕**不扫旧档案**（源二）。
    旧档案块（会话里的 `card:` 块）**从来不参与未读计算**（见 list_card_messages
    注释：unread_by_card 只看 inbox）⇒ 只算角标时扫它纯属白干：
    work_025 有 44 张卡 ⇒ 44 份整会话档案读 ⇒ 实测 1.7~2.1s，
    而左栏角标**每 60 秒**拉一次（nav_system.refreshBadge）⇒ 持续后台负载。

    seen_seq 读取改**并发**（原来 44 次串行 await）。
    """
    cids = list(cards.keys())
    _seqs = await asyncio.gather(
        *(card_db.get_seen_seq(work_id, c) for c in cids),
        return_exceptions=True)
    seen_seq: dict[str, int] = {
        c: (-1 if isinstance(s, BaseException) else int(s))
        for c, s in zip(cids, _seqs)
    }
    rows = inbox_rows if inbox_rows is not None else await card_db.list_inbox(work_id)
    unread: dict[str, int] = {}
    for row in rows:
        target = str(row.get("target_card_id") or "")
        seq = row.get("seq")
        if target and isinstance(seq, int) and seq > seen_seq.get(target, -1):
            unread[target] = 1
    return unread


@router.get("/works/{work_id}/card-messages/unread")
async def list_card_messages_unread(work_id: str):
    """〔P21 · 治「切页卡死」· 2026-10-25〕**只算未读角标**的轻端点。

    与 `/card-messages` 的关系：响应**只有** `unread_by_card` 一个键，
    数值口径**逐字相同**（同一个 `_unread_by_card` 实现，不另写一套）。
    差别只有一个：**不扫旧档案**（源二）⇒ 1.7~2.1s → 毫秒级。

    为什么需要它：左栏未读角标每 60 秒轮询一次，只要一个计数，
    却每次都拖着 44 张卡的整会话档案（= 每分钟一次 1.9 秒的后台负载）。

    旧端点 `/card-messages` **原样保留**（通知面板与画布通信线要 items 全量）。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    cards = card_store.iter_card_metas(work_id)
    return {"unread_by_card": await _unread_by_card(work_id, cards)}


# ============================================================
# [客户端错误上报 2026-10-25] 连接层失败落盘（前端主动报上来）
# ============================================================

class ClientErrorRequest(BaseModel):
    """前端连接层失败的上报体。字段全可选——上报本身绝不能成为新的失败源。"""
    where: str = ""
    url: str = ""
    name: str = ""
    message: str = ""
    href: str = ""
    ua: str = ""


@router.post("/client-errors")
async def report_client_error(req: ClientErrorRequest):
    """客户端连接层错误 → `logs/client_errors.jsonl`。

    为什么需要这个口：用户看到的「网络错误」（Chrome 的
    `TypeError: Failed to fetch`）发生在**连接层**——请求**压根没进到应用**
    ⇒ 服务端日志（原先只有 500 的 `server_errors.jsonl`）**天然看不见**
    ⇒ 排障只能靠猜（这次先后怀疑了释放策略、事件循环堵死，最后才测到
    uvicorn 5 秒空闲断 keep-alive）。⇒ 由前端把这类失败主动报上来落盘留证。
    """
    from src.api import access_log
    # [2026-10-25] 写盘挪进**专用**写线程：async 端点里同步写盘会堵事件循环
    # （P20 治的病）；共用默认线程池又会被业务占满（实测被拖到 6333ms）。
    await access_log.write_client_error({
        "where": req.where,
        "url": req.url[:500],
        "name": req.name,
        "message": req.message[:500],
        "href": req.href[:300],
        "ua": req.ua[:160],
    })
    return {"ok": True}


#: 旧档案扫描（源二）的**按 work 缓存**（TTL）＋ 每 work 的串行闸。
#:
#: 〔P26 · 2026-10-26 · 治「一个旧页面拖死整个服务」〕实测：
#: 一个**旧版前端**标签页每 60 秒拉一次**全量** card-messages ⇒ 每次都重扫该 work
#: 的全部会话文件（**活卡的会话文件一直在变 ⇒ 解析缓存命中不了**）⇒ 3~8 秒/次；
#: 而它把磁盘/线程池占住 ⇒ **整个服务被拖慢**（实测 nav/running 最慢 2978ms、
#: nav/version 3162ms 都是被它拖的；另一次实测**页面 DCL 106683ms —— 106 秒**）。
#: ⇒ 前端已改（角标走轻端点 scope=recent），但**旧 bundle 还在跑** ⇒
#:   服务端也必须扛得住：同一 work 的重复扫描**合并**（TTL 内复用）＋ 串行闸
#:   （不占满线程池）。
#:   ⇒ 顺带说明：为什么解析缓存救不了——**活卡的会话文件每次写都变** ⇒
#:     stamp 变 ⇒ 缓存失效 ⇒ 只能靠这里的 TTL 合并。
_ARCHIVE_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
#: 旧档案 items 的缓存时长（秒）。前端轮询是 60 秒 ⇒ 取 120 秒可合并掉约一半。
_ARCHIVE_TTL = 120.0
_ARCHIVE_LOCKS: dict[str, asyncio.Lock] = {}


async def _old_archive_items(work_id: str, cards: dict) -> list[dict[str, Any]]:
    """旧档案里的 card: 块（**带按 work 的 TTL 缓存 ＋ 串行闸**）。"""
    now = time.monotonic()
    hit = _ARCHIVE_CACHE.get(work_id)
    if hit is not None and (now - hit[0]) < _ARCHIVE_TTL:
        return list(hit[1])
    lock = _ARCHIVE_LOCKS.setdefault(work_id, asyncio.Lock())
    async with lock:
        # 拿到闸后再查一次（等闸期间别人可能已经扫完 ⇒ 直接复用）
        hit = _ARCHIVE_CACHE.get(work_id)
        if hit is not None and (time.monotonic() - hit[0]) < _ARCHIVE_TTL:
            return list(hit[1])
        out: list[dict[str, Any]] = []
        _all_blocks = await asyncio.gather(
            *(session_facade.read(work_id, cid) for cid in cards))
        for cid, blocks in zip(cards.keys(), _all_blocks):
            for b in blocks:
                if not str(b.get("actor") or "").startswith("card:"):
                    continue
                content = str(b.get("content") or "")
                hdr = card_msg.parse_header(content)
                fmt_ok = hdr is not None
                sender = str(b.get("from") or "") or (hdr or {}).get("from", "")
                msg_id = (hdr or {}).get("id") or f"unparsed-{cid}-{b.get('seq', '')}"
                out.append({
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
        _ARCHIVE_CACHE[work_id] = (time.monotonic(), list(out))
        return out


@router.get("/works/{work_id}/card-messages")
async def list_card_messages(work_id: str, limit: int = 200, scope: str = "all"):
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
      〔P21 · 2026-10-25〕两处收敛：① 未读计算与 seen_seq 读取搬进
      `_unread_by_card`（**并发**读 seen_seq，原来 44 次串行 await）；
      ② 只算未读的场景（**左栏角标每 60 秒轮询一次**）改走
      `/card-messages/unread`——**不扫旧档案**，1.7~2.1s → 毫秒级。
      本端点保留 items 全量语义（通知面板与画布通信线要用）。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    cards = card_store.iter_card_metas(work_id)
    items: list[dict[str, Any]] = []
    unread_by_card: dict[str, int] = {}
    seen: set[str] = set()

    # ── 源一：收件箱（新机制，含 consumed）────────────────────────────
    _inbox_rows = await card_db.list_inbox(work_id)
    # 未读口径与 /card-messages/unread **共用同一份实现**（不另写一套，见 _unread_by_card）
    unread_by_card = await _unread_by_card(work_id, cards, inbox_rows=_inbox_rows)
    for row in _inbox_rows:
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
        # [取消 inbox 状态机 2026-10-22 · D25] 未读不再看 status（那个位已删），
        # 改为「该卡读取水位之后收到的消息」——即真实未处理的那批。
        # 〔P21〕判定搬进 _unread_by_card（与 /card-messages/unread 同一份实现）。

    # ── 源二：会话档案里的 card: 块（旧机制，仅存量）──────────────────
    # 〔5715 · 治「切卡卡死」〕**并发读**（原来逐张 await ⇒ 44 张卡串行 1.6~4.5s）。
    # 内容口径逐字不变（items 仍含全部旧档案块），只是把 N 次等待变成 1 轮并发。
    #
    # 〔P25 · 2026-10-26 · 「页面的内容存进内存态」〕`scope="recent"` ⇒ **不扫旧档案**
    # （只回 inbox 的信）。为什么需要：画布页首屏那条 `card-messages?limit=200`
    # 实测 **4417ms**（旧档案扫描：20 张卡 / 16427 块 ⇒ 读 1308ms ＋ 解析 802ms），
    # 而它把同一时刻的 `nav/version`（4239ms）、`rule`（2262ms）**全拖慢** ⇒ 整页卡。
    # 而画布的通信线**只画 30 分钟内的信** ⇒ 旧档案（历史遗留）本就不在窗口内
    # ⇒ 画布页改用 scope=recent ⇒ 首屏不再有那条 4.2 秒的请求。
    # 默认 scope="all" 逐字不变（通知面板要看全部历史，含旧档案）。
    if str(scope or "all").lower() != "recent":
        # 去重仍在这里做（inbox 源已把 msg_id 记进 seen ⇒ 同一 id 不双出）
        for _it in await _old_archive_items(work_id, cards):
            if _it.get("id") in seen:
                continue
            items.append(_it)

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
    # [职能实体化 A 案 2026-10-22] 职能不进交付树：画布卡列表里滤掉它们
    # （它们走下面单独附的 functions，画成顶部一排的观察者节点）。
    data["cards"] = [c for c in (data.get("cards") or [])
                     if str(c.get("card_type") or "") != "function"]
    # [画布职能层 2026-10-22 · 用户令「再在画布上，也把角色卡都显示出来，显示在
    # 最顶上一排，角色卡与卡片之间的信息互动也一并表现」] 职能不进 cards 表 ⇒
    # canvas 投影里天然没有它们。这里单独附一份（含 watch 目标），前端画成
    # 顶部一排的独立节点 + 到被关注卡的虚线。读失败不影响出图（fail-open 空表）。
    try:
        _root_id = work_store.work_card_id_of(work_id) or ""
        data["functions"] = [
            {
                "function_id": p.get("function_id", ""),
                "function_name": p.get("function_name", ""),
                "duty": (p.get("duty") or {}).get("state") or "active",
                "watch": list(((p.get("watch") or {}).get("cards")) or []),
            }
            for p in function_store.list_profiles(work_id)
            if p.get("function_id")
            # [总裁=根卡 2026-10-24] 总裁档案在根卡上——根卡已是画布主节点,
            # 顶部职能排不再重复画一个总裁节点(用户令:两者一体,不单出)。
            and p.get("function_id") != _root_id
            # [旧职能清理 2026-10-24] 归档的不画(撤岗留痕,不显示)。
            and (p.get("duty") or {}).get("state") != "archived"
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[canvas] 读职能失败 work=%s: %s", work_id, exc)
        data["functions"] = []
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
    # [角色机制 5710 v2] 角色卡读侧附独立结构（六节/判断簿/警报簿），
    # 不让通用三块冒充角色卡结构。
    if str((result.get("meta") or {}).get("card_type") or "") == "role":
        result = _apply_block_set(result, work_id)
        result["role"] = await role_room.role_summary(work_id, card_id)
        return result
    # [预设职能 2026-10-24] 职能卡读侧附台账动态面(职责书/观点簿/警报簿/指令簿);
    # 根卡若已职能化(总裁)也附带其职能身份摘要。fail-open:拼不出不挡读。
    try:
        if str((result.get("meta") or {}).get("card_type") or "") == "function":
            result = _apply_block_set(result, work_id)
            from . import function_room as _function_room
            result["function"] = _function_room.summary(work_id, card_id)
            return result
    except Exception as _exc:  # noqa: BLE001
        logger.warning("[cards] 职能卡面拼装失败 work=%s fn=%s: %s",
                       work_id, card_id, _exc)
    # [卡片编辑机制改造 2026-10-12 · 块1] 读侧按块集判据过滤 body 键
    result = _apply_block_set(result, work_id)
    # [总裁制 P1 2026-10-24] 根卡已职能化(总裁档案落根卡)⇒ 附总裁职能摘要
    # (观点簿/决策簿/警报簿)。总裁的 card_type 仍是 project——身份在档案表,
    # 不改类型;读侧附带,前端与模型都能看到"这张根卡同时是总裁"。
    try:
        _root_id = work_store.work_card_id_of(work_id) or ""
        if card_id == _root_id:
            from . import function_room as _function_room
            _prof = _function_room.function_meta_for(work_id, card_id)
            if _prof:
                result["ceo"] = _function_room.summary(work_id, card_id)
    except Exception as _exc:  # noqa: BLE001
        logger.warning("[cards] 总裁摘要拼装失败 work=%s: %s", work_id, _exc)
    return result


# ============================================================
# [wb_exec 2026-10-23] coder 卡的**执行体选择**（谁干活：WorkBuddy 还是系统自己的模型）
#
# 为什么不复用 `PUT /api/settings/model-overrides/{wid}/card/{cid}`：
# 那个端点是**整行替换**（`model_override.put_row` 覆盖 cards[card_id]），
# 只带 ``exec_backend`` 提交会把这一行原有的 model/fallbacks/bindings **抹掉**。
# ⇒ 这里另开**读-改-写**的专用端点：只改 exec_backend 一项。
# ============================================================
@router.get("/works/{work_id}/cards/{card_id}/exec-backend")
async def get_exec_backend(work_id: str, card_id: str):
    """读这张卡的执行体选择 ＋ 上次实际路由到的模型（卡面 UI 用）。"""
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    try:
        card = await card_store.read_card(work_id, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")

    from .wb_exec import bridge as _wb_bridge
    from .wb_exec import config as _wb_config
    cfg = _wb_config.load()
    row = {}
    try:
        from ..storage import model_override as _mo
        row = _mo.get_row(work_id, card_id) or {}
    except Exception:  # noqa: BLE001 读不到就当没设
        row = {}
    backend = str(row.get("exec_backend") or "auto").strip() or "auto"
    routed = row.get("last_routed")
    if not isinstance(routed, dict):
        routed = None
    return {
        "ok": True,
        "card_id": card_id,
        "card_type": str((card.get("meta") or {}).get("card_type") or ""),
        "enabled": bool(cfg.enabled),
        "backend": backend,
        "routed": routed,
        "options": _wb_exec_options(cfg),
    }


@router.put("/works/{work_id}/cards/{card_id}/exec-backend")
async def put_exec_backend(work_id: str, card_id: str, req: SetExecBackendRequest):
    """改这张卡的执行体选择（**只改 exec_backend**；下一回合生效）。"""
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    backend = str(req.backend or "").strip()
    if not backend:
        return _err(400, "backend 不能为空")
    if backend not in ("auto", "system") and not backend.startswith("wb:"):
        return _err(400, f"backend 非法: {backend}")
    from .wb_exec import config as _wb_config
    cfg = _wb_config.load()
    valid = {o["value"] for o in _wb_exec_options(cfg)}
    if backend not in valid:
        return _err(400, f"该执行体不存在: {backend}")
    try:
        from ..storage import model_override as _mo
        cur = _mo.get_row(work_id, card_id) or {}
        new_row = dict(cur)
        new_row["exec_backend"] = backend
        _mo.put_row(work_id, card_id, new_row, src="manual")
    except Exception as exc:  # noqa: BLE001
        logger.error("[wb_exec] 写 exec_backend 失败 card=%s: %s", card_id, exc)
        return _err(500, f"写执行体失败: {exc}")
    return {"ok": True, "card_id": card_id, "backend": backend,
            "note": "已保存；下一回合生效（在跑的回合不打断）"}


# ============================================================
# [wb_exec 2026-10-23 16:20] 执行体**总开关**
#
# 用户令：*"再做一个手动开关，开启走workbuddy，关闭走系统模型"*
#
# **运行时生效**（写 config/wb_exec_switch.json，**不用重启 5700**）：
#   开 ⇒ coder 卡走 WorkBuddy 执行体（按配比选模型）
#   关 ⇒ coder 卡全部走**系统自己的模型**（WorkBuddy 一点不碰）
#
# 优先级：运行时开关 > config/wb_exec.json 的 enabled（文件坏了退回配置）。
# ============================================================
@router.get("/wb-exec/switch")
async def get_wb_exec_switch():
    """读执行体总开关（给卡面 UI 用）。"""
    from .wb_exec import switch as _wb_switch
    st = _wb_switch.state()
    return {"ok": True, **st}


@router.put("/wb-exec/switch")
async def put_wb_exec_switch(req: SetWbExecSwitchRequest):
    """切执行体总开关（**立即生效**，在跑的回合不打断）。"""
    from .wb_exec import switch as _wb_switch
    on = _wb_switch.set_enabled(bool(req.enabled))
    st = _wb_switch.state()
    logger.info("[wb_exec] 执行体总开关切换 ⇒ %s", on)
    return {"ok": True, **st,
            "note": ("已开启：coder 卡走 WorkBuddy 执行体" if on
                     else "已关闭：coder 卡全部走系统自己的模型")}


def _wb_exec_options(cfg) -> list[dict]:
    """执行体下拉的候选项：auto / system / 配比表里的每个 (版本, 模型)。"""
    opts = [
        {"value": "auto", "label": "自动（按配比）"},
        {"value": "system", "label": "系统自己的模型"},
    ]
    labels = {ex.id: ex.label for ex in cfg.executors}
    for c in cfg.candidates:
        # ★ **值必须带 ``wb:`` 前缀**（2026-10-25 实测发现的既有 bug）：
        #   下面 ``put_exec_backend`` 的校验**只接受** ``auto``/``system``/``wb:*``，
        #   而这里早先生成的是 ``<executor>:<model>``（**无前缀**）
        #   ⇒ 前端下拉选任何一项、PUT 都会被 400 拒 ⇒
        #   **「卡面手动钉死执行体」一直是失效的**（落回 auto）。
        val = f"wb:{c.executor}:{c.model}"
        lab = labels.get(c.executor, c.executor)
        opts.append({"value": val, "label": f"{lab} · {c.model}",
                     "executor": c.executor, "model": c.model})
    # **执行体级选项**（2026-10-25）：像 ``wb-oc`` 这种**不在配比表里**的执行体
    # （配比由用户定，不擅自加）⇒ 也得能在卡面**手动钉**它
    # ⇒ 否则 ``put_exec_backend`` 的校验（值必须在 options 里）会把它挡掉。
    in_table = {f"wb:{c.executor}:{c.model}" for c in cfg.candidates}
    for ex in cfg.executors:
        if not ex.oc_models:
            continue
        for m in ex.oc_models:
            val = f"wb:{ex.id}:{m}"
            if val in in_table:
                continue
            opts.append({"value": val,
                         "label": f"{ex.label} · {m}（不在配比表·需手动选）",
                         "executor": ex.id, "model": m})
    return opts


# [用户删卡 2026-10-02] 仅前端调用；模型工具清单不含删卡（模型无权删卡）
@router.get("/works/{work_id}/trash")
async def list_work_trash(work_id: str):
    """[画布 2026-10-19] 回收站列表：删卡后的长期找回入口（撤销条只活 12 秒）。"""
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    return {"items": await card_store.list_trash(work_id)}


@router.post("/works/{work_id}/cards/{card_id}/restore")
async def restore_work_card(work_id: str, card_id: str):
    """[画布 2026-10-19] 撤销删除：从回收站按**原 card_id** 恢复。

    与 DELETE 对称的一条口。幂等（已存在返回 already），不因重复点撤销报错。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    result = await card_store.restore_card(work_id, card_id)
    if "error" in result:
        return _err(404, result["error"])
    return result


@router.delete("/works/{work_id}")
async def delete_work_api(work_id: str):
    """删项目：整个项目目录移到 works/_trash/（可恢复）。

    [删项目 2026-10-21 · 用户令] 原来 work 层根本没有删除函数，根卡又不让删 ⇒
    用户建错的项目永远删不掉。现在提供删除；确认只在前端做一个「是否删除」弹窗，
    后端不再要求输入项目名（用户明确：删就删，别搞多余确认）。
    """
    w = work_store.read_work(work_id)
    if w is None:
        return _err(404, f"剧本不存在: {work_id}")
    result = work_store.delete_work(work_id)
    if "error" in result:
        return _err(500, result["error"])
    return result


@router.delete("/works/{work_id}/cards/{card_id}")
async def delete_work_card(work_id: str, card_id: str, cascade: bool = False):
    """删卡。``?cascade=true`` ⇒ 连同整棵子树一起删（每张都进回收站，可逐个恢复）。

    [级联删除 2026-10-21 · 用户令] 原来有子卡就 400 拒删，把复杂度推给用户
    （手动一层层删）。删父卡的正确语义就是删掉它和它下面的一切。
    项目根卡仍然拒删（它是项目入口，删了项目就空了）。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    try:
        result = await card_store.delete_card(work_id, card_id, cascade=cascade)
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


_RULE_DEFAULTS: dict[str, str] = {
    "project": (
        "1. 拆解用户意图；没有来源的前提列出来向用户确认（confirm_with_user），"
        "不默认成立。\n"
        "2. 需求文档由你策展（curate_face）——子卡激活的前提是需求已策展。\n"
        "3. 派工后等职能评审观点；分歧未消带上分歧问用户。\n"
        "4. 阶段完成向用户汇报（report_to_user），不等被问。"),
    "function": (
        "1. 职责书定义你是谁、什么情况开口；本区是本项目对你的附加规则。\n"
        "2. 你只在阶段收口被叫醒——对整个阶段表态（speak），沉默不落账等于失职。\n"
        "3. 与父卡统合判断相左，写分歧（write_ledger, ledger=divergence）。"),
    "sub_project": (
        "1. 开工前先读需求文档（总裁策展）；没有就 report_up(kind=ask) 请示。\n"
        "2. **阶段任务=新建子卡**：一件阶段性的活建一张子卡（需求+验收判据写清），"
        "不自己在当前卡上直接干；你的卡是管理者——拆阶段、建子卡、等完工。\n"
        "3. 工作细节你域内自决，不上报待裁；现状块只写能核实的事实。\n"
        "4. 完工调 update_goal(complete) 收口——这一刻职能会来做阶段验收。\n"
        "5. 阶段完工才向上汇报（report_up, kind=phase），日常不打扰上级。"),
    "tool": "按剧本执行；产物与用途落清单。",
    "coder": (
        "1. 任务在使命块里；产出＝代码＋report_done 回执。\n"
        "2. 你没有现状块——进展由回执与代码承载。"),
    "rule": "本区写规则卡自身的元规则（很少需要）。",
    "topic": (
        "1. 开工前先读需求文档；没有就 report_up(kind=ask) 请示。\n"
        "2. 研究结论写需求文档块，只追加不改。"),
}


def _rule_template_body() -> str:
    """规则卡默认模板：七区齐，各区带**通用工作流默认值**（面板可改）。

    [2026-10-25 · 用户令「新项目成立就应该自动建好,很多信息可以默认带进去,
    比如工作流程」] 默认内容只写通用流程,不含任何任务事实。
    """
    parts = []
    for n in _rule_section_names():
        parts.append(f"## {n}\n{_RULE_DEFAULTS.get(n, '')}")
    return "\n\n".join(parts)


async def ensure_rules(work_id: str) -> None:
    """[规则=项目基建 2026-10-25 · 用户令「规则卡混成普通卡才会出现在列表里」]

    规则存 **work.json 的 rules 字段**(不是 cards 表里的一张卡——混进卡片
    数据层才会出现在列表/画布/清单里)。建项目即播种默认内容(通用工作流);
    legacy 兜底:基建字段为空而库里存在带内容的旧规则卡 ⇒ 先迁移进来再继续
    (内容不丢)。幂等。
    """
    rules = work_store.get_rules(work_id)
    if any(str(v or "").strip() for v in rules.values()):
        return
    # legacy 迁移:旧规则卡里有实质内容就接过来
    row = _find_rule_card(work_id)
    if row is not None:
        legacy = context_regions.split_rule_sections(row["mission"] or "")
        legacy.pop("", None)
        legacy = {k: v for k, v in legacy.items() if str(v or "").strip()}
        if legacy:
            work_store.set_rules(work_id, legacy)
            return
    work_store.set_rules(work_id, _RULE_DEFAULTS)


@router.get("/works/{work_id}/rule")
async def get_work_rule(work_id: str):
    """规则面板读口：七区齐全、hashes、正被 N 类卡注入。

    [规则=项目基建 2026-10-25] 读 work.json 的 rules 字段(不再是卡);
    exists 恒 True——规则随项目自带,不存在「还没有规则」的状态。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    names = _rule_section_names()
    counts = context_regions.rule_inject_counts(card_store.load_type_registry())
    await ensure_rules(work_id)
    sections = {n: work_store.get_rules(work_id).get(n, "") for n in names}
    hashes = {n: _rule_section_hash(sections[n]) for n in names}
    return {"exists": True, "card_id": "",
            "sections": sections, "hashes": hashes,
            "inject_type_counts": {n: counts.get(n, 0) for n in names}}


@router.patch("/works/{work_id}/rule/sections/{section}")
async def patch_work_rule_section(work_id: str, section: str,
                                  req: PatchRuleSectionRequest):
    """面板直改即存：hash 比对（base_hash 不符→409），写 work.json rules。"""
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    names = _rule_section_names()
    if section not in names:
        return _err(400, f"非法区名: {section}（只允许 {names}）")
    await ensure_rules(work_id)
    rules = work_store.get_rules(work_id)
    cur = rules.get(section, "")
    old_hash = _rule_section_hash(cur)
    if req.base_hash != old_hash:
        return _err(409, f"hash 不符（当前区 hash={old_hash[:16]}），请刷新后重试")
    work_store.set_rules(work_id, {**rules, section: req.content})
    new_hash = _rule_section_hash(req.content)
    return {"ok": True, "section": section,
            "old_hash": old_hash, "new_hash": new_hash}


@router.post("/works/{work_id}/rule/commit")
async def post_work_rule_commit(work_id: str, req: RuleCommitRequest):
    """根卡提议审批落盘：draft_rule_edit 产物经用户「同意」后写入
    work.json rules（hash 比对同 PATCH）。"""
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    names = _rule_section_names()
    if req.section not in names:
        return _err(400, f"非法区名: {req.section}（只允许 {names}）")
    await ensure_rules(work_id)
    rules = work_store.get_rules(work_id)
    cur = rules.get(req.section, "")
    old_hash = _rule_section_hash(cur)
    if req.base_hash != old_hash:
        return _err(409, f"hash 不符（当前区 hash={old_hash[:16]}），请重新提议")
    work_store.set_rules(work_id, {**rules, req.section: req.content})
    new_hash = _rule_section_hash(req.content)
    return {"ok": True, "section": req.section,
            "old_hash": old_hash, "new_hash": new_hash}


@router.get("/works/{work_id}/cards/{card_id}/timeline")
async def get_card_timeline(work_id: str, card_id: str, frm: int = Query(0, alias="from"),
                            tail: int = Query(0), before: int = Query(0)):
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
    items = await asyncio.to_thread(
        event_log.timeline, work_id, card_id, from_seq=frm, tail=tail, before=before)
    # 〔2026-10-26 · 治「切卡卡死」〕**响应层载荷裁剪**（落盘不动）：
    # 实测打开一张大卡的 `timeline?tail=400` 响应体 **36.9MB**（其中 31.8MB 是
    # 一条 `tool_result`——read_linked 把整张卡的内容回灌进了事件日志）
    # ⇒ 服务端序列化 3~7 秒 ＋ 浏览器 parse 36.9MB ＋ 撞 15 秒客户端超时
    # ⇒ 「请求超时」＋ 内存态建不起来（看着像被释放）⇒ 只能刷新。
    # 而前端工具行**默认折叠**（摘要只要 content 的第一行）⇒ 裁掉零可见变化。
    from . import payload_cap
    payload_cap.cap_records(items)
    # [对话流分页 2026-10-20] has_earlier＝「这次是窗口请求，且拿满了窗口」
    # ⇒ 更早还有。前端据此决定「往上翻」是否还有东西可拉。
    # 全量请求（tail=0）恒 false：已经拿到全部。
    try:
        _tail_n = int(tail or 0)
    except (TypeError, ValueError):
        _tail_n = 0
    has_earlier = bool(_tail_n) and len(items) >= _tail_n
    return {
        "work_id": work_id,
        "card_id": card_id,
        "items": items,
        "last_seq": items[-1]["seq"] if items else 0,
        "first_seq": items[0]["seq"] if items else 0,
        "has_earlier": has_earlier,
    }


# [消息底座能力对齐 · 件4] 任务书字面的卡级入口（无 work 路径段）：work_id 由
# 查询参数给定，缺省转发 work_001（与既有 /api/cards/{id}/… 兼容层同口径）。
@router.get("/card/{card_id}/timeline")
async def get_card_timeline_compat(card_id: str, work_id: str = DEFAULT_WORK_ID,
                                   frm: int = Query(0, alias="from"),
                                   tail: int = Query(0), before: int = Query(0)):
    # frm 显式传：直接调路由函数时 Query(...) 默认值是 Query 对象而非 0
    return await get_card_timeline(work_id, card_id, frm, tail, before)


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
        # [5702 · C-1 2026-10-20] 真推送：订阅 journal（先补 from 之后的存量，再随
        # emit 的 fanout 实时送达），不再每 0.4s 全量拷贝一次 journal。
        # from=0 → 游标落在末帧（零重放）；from>0 → 断点续传（语义不变）。
        start = frm if frm > 0 else event_stream.journal_last_seq(work_id, card_id) + 1
        it = event_stream.subscribe(work_id, card_id, from_seq=start)
        nxt = None
        try:
            while True:
                if nxt is None:
                    nxt = asyncio.create_task(it.__anext__())
                done, _pending = await asyncio.wait({nxt}, timeout=15.0)
                if not done:
                    yield ": ping\n\n"          # 空闲保活；不取消挂起的 anext
                    continue
                task, nxt = nxt, None
                try:
                    evt = task.result()
                except StopAsyncIteration:
                    break
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
        finally:
            if nxt is not None:
                nxt.cancel()
            try:
                await it.aclose()
            except Exception:  # noqa: BLE001 关闭期异常不阻断
                pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/works/{work_id}/events")
async def stream_work_events(work_id: str):
    """[画布 5711] **工作级** SSE：画布页订阅，卡片一变就推。

    为什么需要这个端点（实测复现的断点，不是推测）：
      既有 SSE 是**单卡**的（`/cards/{cid}/stream`，订阅表 key=(work,card)），
      没有任何"整个项目变了"的广播；而画布上卡片的结论／待裁定数／work_state／
      最新对话**只在 renderAll 全量重建才更新**，renderAll 又只由用户自己的
      操作触发 ⇒ 后台真实发生的变化（模型写卡、裁定被记下、自动模式推进）
      **到不了画布** ⇒ 用户必须手动刷新页面才看得到。

    本端点补上这一层：写卡路径（`card_store`）发一帧 `card_changed`
    （带那张卡的最新投影摘要），画布页订阅后**就地换掉那一张卡的节点**——
    毫秒级、零闪烁、不重建整棵树。

    契约：
      帧形态与 `/cards/{cid}/stream` 同构：``{"seq","ts","type","data"}``；
      type  当前只有 ``card_changed``；
      data  ``{card_id, kind: created|updated|deleted, card: {...}|None}``；
      无存量重放（工作级事件描述的是"变化"，重放会让前端误判"刚变过"）；
      15s 心跳（注释帧），与既有两条流同口径。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")

    async def gen():
        """带心跳的 SSE 生成器。

        订阅队列空转时每 15s 发一个注释帧（`: ping`），防止中间代理掐断长连——
        与既有两条流（/stream、/tokens）同口径。

        〔P23 · 治「推送不推送」· 2026-10-25〕**必须用手动 task 模式**，不能
        `asyncio.wait_for(it.__anext__(), timeout=15)`：
        wait_for 超时会**取消**挂起的 `__anext__` ⇒ 异步生成器被打断，
        下一轮 `__anext__` 直接抛（already running / CancelledError）⇒
        **这条流每 15 秒断一次**（访问日志实测：/events 的连接每 18 秒重连一次
        ＝ 15s 心跳 + 3s 重连）⇒ 「推送为主」其实一直靠兜底轮询撑着。
        另两条流（/stream、/tokens）早就是手动 task 模式（超时**不**取消挂起的
        anext）⇒ 稳定。⇒ 这里与它们同款。
        """
        yield "retry: 3000\n\n"
        it = event_stream.subscribe_work(work_id).__aiter__()
        nxt = None
        try:
            while True:
                if nxt is None:
                    nxt = asyncio.create_task(it.__anext__())
                done, _pending = await asyncio.wait({nxt}, timeout=15.0)
                if not done:
                    yield ": ping\n\n"        # 空闲保活；**不取消挂起的 anext**
                    continue
                task, nxt = nxt, None
                try:
                    evt = task.result()
                except StopAsyncIteration:
                    break
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
        finally:
            if nxt is not None:
                nxt.cancel()
            try:
                await it.aclose()
            except Exception:  # noqa: BLE001 关闭期异常不阻断
                pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/works/{work_id}/cards/{card_id}/tokens")
async def stream_card_tokens(work_id: str, card_id: str, frm: int = Query(0, alias="from"),
                             replay: int = Query(0)):
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
        # [5702 · C-1 2026-10-20] 同 /stream：逐字流也改真推送（原 0.25s 轮询
        # ＋ 每次全量拷贝 token journal，是「吐字一顿一顿」的另一半原因）。
        if frm > 0:
            start = frm
        elif replay:
            # 打开**正在跑**的卡：从本 run 的首帧回放，补出已经吐过的字
            # （dsh 的 host 快照自带当前 blocks；本机没有快照，用 journal 回放等价）。
            start = 1
            for _e in reversed(event_stream.token_journal_of(work_id, card_id)):
                if (_e.get("type") == "run_start"
                        and (_e.get("data") or {}).get("segment") == "run"):
                    start = int(_e.get("seq") or 1)
                    break
        else:
            start = event_stream.token_journal_last_seq(work_id, card_id) + 1
        it = event_stream.subscribe_tokens(work_id, card_id, from_seq=start)
        nxt = None
        try:
            while True:
                if nxt is None:
                    nxt = asyncio.create_task(it.__anext__())
                done, _pending = await asyncio.wait({nxt}, timeout=10.0)
                if not done:
                    yield ": ping\n\n"
                    continue
                task, nxt = nxt, None
                try:
                    evt = task.result()
                except StopAsyncIteration:
                    break
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
        finally:
            if nxt is not None:
                nxt.cancel()
            try:
                await it.aclose()
            except Exception:  # noqa: BLE001 关闭期异常不阻断
                pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _revive_coder_card(work_id: str, card_id: str, content: str) -> None:
    """完工的编程卡**重新派活**：卡复位 active ＋ 目标重新激活。

    [coder 卡可继续派活 2026-10-21 · 用户令] 改前完工卡收到消息直接 400
    「不再激活；新任务请新建编程卡」⇒ 同一件事的后续、同一处返工，只能另开
    一张卡，上下文与留痕全断（用户原话：「明明是继续的任务，它也只能新开卡执行」）。
    现在把「再发一条消息」当作**重新派活**：
      · ``state: completed → active``（否则 report_done 判态、K3 调起都会拦）；
      · 目标是 complete ⇒ 用这条消息开**新生命周期**（create 是唯一合法入口），
        消息内容就是新目标——它本来就是「接下来要干什么」；
      · paused/blocked ⇒ resume（清等待位与活锁计数）。
    """
    await card_store.update_card(
        work_id, card_id, {"state": "active"}, "重新派活：完工编程卡复位为 active")
    meta = card_store._meta_of(work_id, card_id) or {}
    if not bool(meta.get("auto_mode")):
        return
    from ..goal.service import get_service
    from ..goal.types import CreateGoalRequest

    svc = get_service()
    goal = await svc.get(work_id, card_id)
    text = str(content or "").strip()
    if goal is None:
        if text:
            await svc.create(work_id, card_id, CreateGoalRequest(objective=text))
        return
    if goal.phase == "complete":
        await svc.create(work_id, card_id,
                         CreateGoalRequest(objective=text or goal.objective))
    elif goal.phase in ("paused", "blocked"):
        await svc.resume(work_id, card_id, goal.ref())
    # 目标重开后会立刻 arm ⇒ 驱动器抢在用户这条消息之前起一轮 ⇒ 这条消息撞 409。
    # 先 disarm：本轮由这条消息来跑，跑完 after_turn 按 goal_state=active 正常续轮。
    try:
        await svc.disarm(work_id, card_id)
    except Exception as exc:  # noqa: BLE001 撤销 arm 失败不阻断（最坏是多起一轮）
        logger.warning("[coder] 重新派活后撤销 arm 失败 card=%s: %s", card_id, exc)


@router.post("/works/{work_id}/cards/{card_id}/messages")
async def post_work_message(work_id: str, card_id: str, req: PostMessageRequest):
    """对话主链路（模块 H2）。"""
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    # [编程卡 2026-10-15 · §六块3 静默判定①] 完工编程卡不再被激活。
    # [coder 卡可继续派活 2026-10-21 · 用户令] 这条**改了**：不再 400 拒绝，
    # 而是把「再发一条消息」当成重新派活（卡复位 + 目标重新激活）——否则同一件
    # 事的后续只能另开一张卡。读卡失败仍走 _run_message 既有口径。
    try:
        _meta = (await card_store.read_card(work_id, card_id)).get("meta") or {}
    except Exception:  # noqa: BLE001
        _meta = {}
    # [coder 归档复活 2026-10-25] 归档卡不是活卡(read_card 查不到)——
    # 对归档 coder 发消息 = 解档复活,再走下面的重派活分支(同规则)。
    if not _meta:
        from ..storage import coder_archive as _ca
        if _ca.get_record(work_id, card_id) is not None:
            from . import coder_fold
            _un = await coder_fold.unarchive_coder(work_id, card_id)
            if _un.get("ok"):
                try:
                    _meta = (await card_store.read_card(
                        work_id, card_id)).get("meta") or {}
                except Exception:  # noqa: BLE001
                    _meta = {}
    _revive_owner = ""
    if str(_meta.get("card_type") or "") == "coder" \
            and str(_meta.get("state") or "") == "completed":
        # [重新派活竞态 2026-10-21] 改目标会 emit goal/changed ⇒ 驱动器立刻
        # request_drive 抢一轮，把互斥体占掉 ⇒ 用户这条消息反而撞 409。
        # 所以在改目标期间**先占住互斥体**（临时 owner），让驱动器这一跳被跳过；
        # 下面 ensure 完再释放，_run_message 紧接着自己拿。
        _revive_owner = f"revive:{uuid.uuid4().hex[:8]}"
        session_facade.turn_try_acquire(work_id, card_id, _revive_owner)
        try:
            await _revive_coder_card(work_id, card_id, req.content)
        except Exception as exc:  # noqa: BLE001 复位失败才如实拒绝
            logger.error("[coder] 重新派活失败 work=%s card=%s: %s",
                         work_id, card_id, exc)
            if _revive_owner:
                session_facade.turn_release(work_id, card_id, _revive_owner)
            return _err(500, f"编程卡重新派活失败：{exc}")
    # [自动模式 2026-10-17 · 回灌 5700 块0.3 · D2] auto 卡收到**首条消息** ⇒
    # 该消息成为 goal、goal_state=active（起跑条件齐备；本轮跑完后驱动器自动
    # 续轮）。已有目标 ⇒ 不动（中途引导，目标不变——方案 §3.4）。非 auto 卡
    # ⇒ no-op（红线 2：非 auto 卡零变化）。
    try:
        await auto_driver.ensure_goal_from_message(work_id, card_id, req.content)
    except Exception as exc:  # noqa: BLE001 目标设定失败不阻断本次消息
        logger.warning("[auto] 首条消息设定目标失败 work=%s card=%s: %s",
                       work_id, card_id, exc)
    finally:
        if _revive_owner:
            session_facade.turn_release(work_id, card_id, _revive_owner)
    return await _run_message(work_id, card_id, req.content, req.pair_id,
                              op=req.op or "",
                              source_kind="human")


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
    return await _run_message(work_id, card_id, req.content, None,
                               source_kind="human")


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
    return await _run_message(work_id, card_id, content, run_id or None,
                               source_kind="human")


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
    # [强制停止 2026-10-24 · 用户令「停止必须强制」] 用户停止=强制:杀轮 +
    # goal 级联暂停 + **停止标记**(拒绝内部唤醒,直到用户再次发话)。
    res = control.halt(work_id, card_id, reason="用户停止", by="user")
    try:
        auto_res = await control.stop_auto(work_id, card_id)
    except Exception as exc:  # noqa: BLE001 停止路径绝不抛
        logger.error("[control] stop_auto 异常 work=%s card=%s: %s",
                     work_id, card_id, exc)
        auto_res = {"paused": [], "lock_released": False}
    return {**res, **auto_res}


@router.post("/works/{work_id}/cards/{card_id}/steer")
async def post_card_steer(work_id: str, card_id: str, req: dict):
    """**插话**：把一条用户消息投递进**正在跑**的这一轮。

    [5715] 此前 UI 上有"插话"（Cmd/Ctrl+Enter、`T.steerQueue` 文案），
    但实现是空的——运行中只弹一句「轮结束后依序发出」，用户等的是整轮跑完（1–8 分钟）。
    本端点是真投递：``steer.request_steer`` 置位 ⇒ ``message_runner`` 下一次模型
    调用前插进轮槽 ⇒ 模型下一轮就能看到。

    - **无在途回合** ⇒ ``applied:false``（前端回退成普通发送，不假装成功）；
    - 已在途 ⇒ ``applied:true``，消息进队列，下一轮模型调用生效。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    text = str((req or {}).get("content") or (req or {}).get("text") or "")
    res = await steer.request_steer(work_id, card_id, text)
    # [5715] 「没插上」也返回 200：端点成功执行了「尝试插话」这个动作，只是这一轮
    # 赶不上了（无在途回合）。用 409 会让前端弹一个错误提示——那不是错误，
    # 是正常的时序，处置是「回退成排队」而不是「报错」。
    return res


@router.post("/works/{work_id}/cards/{card_id}/auto/resume")
async def post_card_auto_resume(work_id: str, card_id: str):
    """**人工**恢复：把停在 paused / blocked 的自动模式卡重新拉起。

    [6702 移植 2026-10-17] 死锁兜底（``auto_await_resume_giveup``）会把卡停在
    paused 并只留一条 ops 痕迹——但全仓没有任何路径能把 ``goal_state`` 写回
    active：``goal_state`` 不在 ``ALLOWED_UPDATE_KEYS``；``ensure_goal_from_message``
    在 goal 非空时直接返回；切 ``auto_mode`` 只重算 perms。故补本端点作为
    **人工处置出口**（不动 PATCH 白名单——那会把内部态开成用户可任意写的口子）。

    语义：
      · 卡不存在 / 读不出            ⇒ 404
      · 非 auto 卡                   ⇒ 400（信息自带出路）
      · 无 goal                      ⇒ 400（信息自带出路）
      · ``goal_state == active``     ⇒ 200 ``{"resumed": False}``（幂等，不重复起轮）
      · ``paused`` / ``blocked``     ⇒ 落三字段 ＋ kick ⇒ 200 ``{"resumed": True}``
      · ``complete``                 ⇒ 用同一 objective 新建 goal（轮次归零）＋ kick
        [coder 卡可重新激活 2026-10-21 · 用户令] 改前这里 400「已完成不能恢复」，
        而发消息也不会动（goal 非空即早退）⇒ 跑完的卡只能新建一张卡才能接着干。
    """
    from . import auto_driver

    try:
        meta = card_store._meta_of(work_id, card_id) or {}
    except Exception as exc:  # noqa: BLE001
        return _err(404, f"读卡失败：{exc}")
    if not meta:
        return _err(404, f"卡片不存在：{card_id}")
    if not bool(meta.get("auto_mode")):
        return _err(400, "该卡不是自动模式卡；请先在卡详情里开启自动模式")
    if not str(meta.get("goal") or "").strip():
        return _err(400, "该卡还没有目标；给它发一条消息即可设定目标并起跑")
    state = str(meta.get("goal_state") or "")
    if state == auto_driver.GOAL_ACTIVE:
        return {"resumed": False, "reason": "已在推进中"}
    ok = await auto_driver.reactivate_card(work_id, card_id)
    if not ok:
        return _err(500, "恢复失败（状态写入未成功），请查看服务日志")
    return {"resumed": True, "goal_state": auto_driver.GOAL_ACTIVE,
            "from": state}


# [goal 域 2026-10-20 · 照抄 dsh-command-goal] 人面 /goal 命令。
# 语法：input 为空＝看状态；否则 clear / pause / resume / edit <objective> / <objective>。
@router.post("/works/{work_id}/cards/{card_id}/goal")
async def post_goal_command(work_id: str, card_id: str, req: dict):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    from ..goal import command as goal_command
    raw = str((req or {}).get("input") or "")
    result = await goal_command.execute_command(work_id, card_id, raw)
    # [coder 卡可改目标 2026-10-21 · 用户令] 建/改目标之后，auto 卡应当**立刻**按新
    # 目标跑——否则用户改完目标看着卡一动不动，以为又坏了。
    # 只对 create/edit 生效：pause/clear/resume 各自语义已在命令里落地，别插手。
    try:
        from . import auto_driver

        kind = str(goal_command.parse(raw).get("kind") or "")
        if result.get("ok") and kind in ("create", "edit"):
            meta = card_store._meta_of(work_id, card_id) or {}
            if bool(meta.get("auto_mode")):
                if str(meta.get("goal_state") or "") == auto_driver.GOAL_ACTIVE:
                    auto_driver.kick(work_id, card_id)
                else:
                    await auto_driver.reactivate_card(work_id, card_id)
    except Exception as exc:  # noqa: BLE001 起跑失败不影响命令本身的结果
        logger.warning("[goal] 改目标后起跑失败 work=%s card=%s: %s", work_id, card_id, exc)
    return result


# [实时反馈 2026-10-02] 调用进度查询：纯展示层，前端轮询用。
# 只读内存态，不进任何模型输入，不拦截任何调用——不是闸。
@router.get("/works/{work_id}/cards/{card_id}/progress")
async def get_work_progress(work_id: str, card_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    st = progress.get_stage(work_id, card_id)
    return st if st is not None else {"stage": "idle", "detail": "", "ts": 0.0}


# ============================================================
# [角色机制 5710] 角色卡：组阁 / 关注 / 巡检 / 判词 / 警报
# ============================================================

class CreateRoleRequest(BaseModel):
    title: str
    charter: str
    jurisdiction: Optional[dict[str, Any]] = None
    watch_card_ids: list[str] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)
    speak_contract: Optional[dict[str, Any]] = None
    escalation: Optional[dict[str, Any]] = None


class RoleWatchRequest(BaseModel):
    card_ids: list[str] = Field(default_factory=list)
    action: str = "add"


class RoleProfileRequest(BaseModel):
    jurisdiction: Optional[dict[str, Any]] = None
    watch_card_ids: Optional[list[str]] = None
    signals: Optional[list[str]] = None
    speak_contract: Optional[dict[str, Any]] = None
    escalation: Optional[dict[str, Any]] = None
    charter: Optional[str] = None


class RoleDutyRequest(BaseModel):
    state: str
    note: str = ""


class RoleDirectiveRequest(BaseModel):
    target_card_id: str
    instruction: str
    acceptance: str
    due_at: str = ""


class RoleDirectiveVerifyRequest(BaseModel):
    status: str
    evidence: str = ""


@router.get("/works/{work_id}/roles")
async def list_work_roles(work_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    roles = []
    for m in role_room.list_roles(work_id):
        roles.append(await role_room.role_summary(
            work_id, str(m.get("card_id") or "")))
    return {"roles": roles}


@router.post("/works/{work_id}/roles")
async def create_work_role(work_id: str, req: CreateRoleRequest):
    """前端组阁入口：与根卡工具 create_role 同一执行体（同一套校验）。"""
    w = work_store.read_work(work_id)
    if w is None:
        return _err(404, f"剧本不存在: {work_id}")
    root_id = str(w.get("work_card_id") or "")
    cards = card_tools._collect_card_meta(work_id)
    res = await card_tools._h_create_role(work_id, root_id, {
        "title": req.title,
        "charter": req.charter,
        "jurisdiction": req.jurisdiction,
        "watch_card_ids": list(req.watch_card_ids or []),
        "signals": list(req.signals or []),
        "speak_contract": req.speak_contract,
        "escalation": req.escalation,
    }, cards)
    if isinstance(res, dict) and res.get("ok") is False:
        err = res.get("error") or {}
        return _err(400, str(err.get("message") or err.get("code") or "创建失败"))
    return JSONResponse(status_code=201, content=res)


@router.get("/works/{work_id}/roles/{role_id}")
async def get_work_role(work_id: str, role_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    meta = card_store._meta_of(work_id, role_id) or {}
    if not meta:
        return _err(404, f"卡片不存在: {role_id}")
    if str(meta.get("card_type") or "") != "role":
        return _err(400, f"{role_id} 不是角色卡")
    return await role_room.role_summary(work_id, role_id)


@router.patch("/works/{work_id}/roles/{role_id}")
async def patch_work_role(work_id: str, role_id: str, req: RoleProfileRequest):
    """根卡/用户更新角色档案（管辖域/关注信号/发言契约/升级线/职责书）。"""
    w = work_store.read_work(work_id)
    if w is None:
        return _err(404, f"剧本不存在: {work_id}")
    root_id = str(w.get("work_card_id") or "")
    args: dict[str, Any] = {"role_card_id": role_id}
    for key in ("jurisdiction", "speak_contract", "escalation", "charter"):
        val = getattr(req, key)
        if val is not None:
            args[key] = val
    if req.watch_card_ids is not None:
        args["watch_card_ids"] = list(req.watch_card_ids)
    if req.signals is not None:
        args["signals"] = list(req.signals)
    cards = card_tools._collect_card_meta(work_id)
    res = await card_tools._h_update_role_profile(work_id, root_id, args, cards)
    if isinstance(res, dict) and res.get("ok") is False:
        err = res.get("error") or {}
        return _err(400, str(err.get("message") or err.get("code") or "更新失败"))
    return res


@router.post("/works/{work_id}/roles/{role_id}/directives")
async def issue_role_directive(work_id: str, role_id: str, req: RoleDirectiveRequest):
    """UI 以角色身份下指令（同一个执行体；权限仍按角色闸）。"""
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    cards = card_tools._collect_card_meta(work_id)
    res = await card_tools._h_issue_directive(work_id, role_id, {
        "target_card_id": req.target_card_id,
        "instruction": req.instruction,
        "acceptance": req.acceptance,
        "due_at": req.due_at,
    }, cards)
    if isinstance(res, dict) and res.get("ok") is False:
        err = res.get("error") or {}
        return _err(400, str(err.get("message") or err.get("code") or "下指令失败"))
    return JSONResponse(status_code=201, content=res)


@router.post("/works/{work_id}/roles/{role_id}/directives/{directive_id}/verify")
async def verify_role_directive(work_id: str, role_id: str, directive_id: str,
                                req: RoleDirectiveVerifyRequest):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    cards = card_tools._collect_card_meta(work_id)
    res = await card_tools._h_verify_directive(work_id, role_id, {
        "directive_id": directive_id,
        "status": req.status,
        "evidence": req.evidence,
    }, cards)
    if isinstance(res, dict) and res.get("ok") is False:
        err = res.get("error") or {}
        return _err(400, str(err.get("message") or err.get("code") or "验收失败"))
    return res


@router.post("/works/{work_id}/roles/{role_id}/duty")
async def set_work_role_duty(work_id: str, role_id: str, req: RoleDutyRequest):
    w = work_store.read_work(work_id)
    if w is None:
        return _err(404, f"剧本不存在: {work_id}")
    root_id = str(w.get("work_card_id") or "")
    cards = card_tools._collect_card_meta(work_id)
    res = await card_tools._h_set_role_duty(work_id, root_id, {
        "role_card_id": role_id, "state": req.state, "note": req.note,
    }, cards)
    if isinstance(res, dict) and res.get("ok") is False:
        err = res.get("error") or {}
        return _err(400, str(err.get("message") or err.get("code") or "操作失败"))
    return res


@router.post("/works/{work_id}/roles/{role_id}/check")
async def check_work_role(work_id: str, role_id: str):
    """人工触发一次巡检（有新同步才起回合；无新增则 started=false）。"""
    meta = card_store._meta_of(work_id, role_id) or {}
    if not meta:
        return _err(404, f"卡片不存在: {role_id}")
    if str(meta.get("card_type") or "") != "role":
        return _err(400, f"{role_id} 不是角色卡")
    return await role_room.manual_check(work_id, role_id)


@router.post("/works/{work_id}/roles/{role_id}/watch")
async def watch_role_cards(work_id: str, role_id: str, req: RoleWatchRequest):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    cards = card_tools._collect_card_meta(work_id)
    root_id = str(work_store.read_work(work_id).get("work_card_id") or "")
    res = await card_tools._h_role_watch_cards(work_id, root_id, {
        "role_card_id": role_id, "action": req.action,
        "card_ids": list(req.card_ids or []),
    }, cards)
    if isinstance(res, dict) and res.get("ok") is False:
        err = res.get("error") or {}
        return _err(400, str(err.get("message") or err.get("code") or "操作失败"))
    return res


@router.delete("/works/{work_id}/roles/{role_id}/watch/{card_id}")
async def unwatch_role_card(work_id: str, role_id: str, card_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    return await role_room.unlink(work_id, role_id, card_id, actor="用户")


@router.get("/works/{work_id}/alarms")
async def list_work_alarms(work_id: str, status: str = "open"):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    return {"alarms": role_room.list_alarms(work_id, status=status)}


@router.post("/works/{work_id}/alarms/{alarm_id}/ack")
async def ack_work_alarm(work_id: str, alarm_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    hits = [it for it in role_room.list_alarms(work_id, status="")
            if str(it.get("id") or "") == alarm_id]
    if not hits:
        return _err(404, f"警报不存在: {alarm_id}")
    ok = await role_room.ack_alarm(work_id, alarm_id)
    return {"ok": bool(ok), "alarm_id": alarm_id}


@router.post("/works/{work_id}/alarms/{alarm_id}/correct")
async def correct_work_alarm(work_id: str, alarm_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    ok = await role_room.correct_alarm(work_id, alarm_id, "user")
    if not ok:
        return _err(404, f"警报不存在或不可更正: {alarm_id}")
    return {"ok": True, "alarm_id": alarm_id, "status": "corrected"}


# ============================================================
# [function 实体化 · card_034] 职能：外部创建 / 列表（独立号段 fn_NNN，不进 cards）
# ============================================================

class CreateFunctionRequest(BaseModel):
    """外部建职能请求体（首测端点）。function_name / charter 必填（缺失在
    handler 内统一判 400，不依赖 pydantic 422——保证「4xx＋明确 message」口径）。"""
    function_name: Optional[str] = None
    charter: Optional[str] = None
    jurisdiction: Optional[dict[str, Any]] = None
    watch_card_ids: list[str] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)
    speak_contract: Optional[dict[str, Any]] = None
    escalation: Optional[dict[str, Any]] = None
    ratified_by: Optional[str] = None


@router.get("/works/{work_id}/functions")
async def list_work_functions(work_id: str):
    """列出该 work 全部职能的 function_id / function_name / duty / watch。"""
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    profs = function_store.list_profiles(work_id)
    out = []
    for prof in profs:
        watch = (prof.get("watch") or {})
        out.append({
            "function_id": prof.get("function_id"),
            "function_name": prof.get("function_name"),
            "duty": (prof.get("duty") or {}).get("state") or "active",
            "watch": watch.get("cards") or [],
        })
    return {"functions": out}


@router.get("/works/{work_id}/functions/{function_id}")
async def get_work_function(work_id: str, function_id: str):
    """单个职能的完整档案（左栏点击职能行时用）。

    [职能入口 2026-10-22 · 用户令「fn 角色卡前端没有任何入口，应该像以前的角色卡
    一样挂在列表上」] nav 里只带 id/名字/状态（省传输），职责书等正文按需拉这一条。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    prof = function_store.get_profile(work_id, function_id)
    if not prof:
        return _err(404, f"职能不存在: {function_id}")
    return {"function": prof}


@router.get("/works/{work_id}/functions/{function_id}/timeline")
async def get_function_timeline(work_id: str, function_id: str,
                                tail: int = 60):
    """职能的对话流（职能回合产生的事件）。

    [职能对话 2026-10-22 · 用户「职能卡没有对话的吗？点不进去啊」]
    职能不进 cards 表 ⇒ 走不了 `/cards/{cid}/timeline`（那条会先校验卡存在）。
    事件流本身是按 key 落盘的（``events/{key}.jsonl``），所以这里直接按 function_id 读。
    还没跑过回合的职能返回空数组（不是错误）。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    prof = function_store.get_profile(work_id, function_id)
    if not prof:
        return _err(404, f"职能不存在: {function_id}")
    # [职能实体化 A 案 2026-10-22] 职能回合以 **card_id** 跑（cards 行是基础设施句柄）
    # ⇒ 事件流落在 card_id 的键上，不是 fn_NNN。旧职能（迁移前）才按 fn_id 落。
    key = str(prof.get("card_id") or "") or function_id
    items = event_log.timeline(work_id, key, tail=max(1, min(int(tail or 60), 400)))
    return {"function_id": function_id, "card_id": key, "items": items}


@router.post("/works/{work_id}/functions")
async def create_work_function(work_id: str, req: CreateFunctionRequest):
    """外部建职能：独立号段 fn_NNN，不写 cards 表（职能不进卡片世界）。

    与根卡工具 create_function 同一存储体（function_store.create_function），
    档案形状照 role_room.make_profile（默认值与旧角色路径同源）。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    name = (req.function_name or "").strip()
    charter = (req.charter or "").strip()
    if not name:
        return _err(400, "function_name 不能为空（写清这是什么职能）")
    if not charter:
        return _err(400, "charter 不能为空（职责书：对什么负责、什么情况发言、什么情况沉默）")
    watch = [str(c) for c in (req.watch_card_ids or []) if c]
    prof = role_room.make_profile(
        name, charter,
        jurisdiction=req.jurisdiction,
        watch_cards=watch,
        signals=req.signals,
        speak_contract=req.speak_contract,
        escalation=req.escalation,
        ratified_by=req.ratified_by or "http:external",
    )
    prof["function_name"] = name
    created = await function_store.create_function(work_id, name, prof)
    fid = str(created.get("function_id") or "")
    # [职能实体化 A 案 2026-10-22] 同工具路径：建职能同时建它的 cards 行（id=fn_NNN）。
    await card_tools.ensure_function_card(work_id, fid, name)
    return JSONResponse(status_code=201, content={
        "function_id": fid,
        "function_name": name,
        "watch": [str(c) for c in ((created.get("watch") or {}).get("cards") or [])],
        "duty": (created.get("duty") or {}).get("state") or "active",
        "in_cards_table": True,
    })



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
    return await _run_message(DEFAULT_WORK_ID, card_id, req.content, req.pair_id,
                               source_kind="human")


# ============================================================
# messages 主链路（工具循环 + 建卡协议闭环）
# ============================================================

async def _run_message(work_id: str, card_id: str, content: str,
                       client_pair_id: Optional[str] = None,
                       append_user: bool = True,
                       op: str = "",
                       source: Optional[dict[str, Any]] = None,
                       source_kind: str = "internal") -> Any:
    """完整 messages 链路，新旧路由共用。

    顺序：读卡 → 取历史 → 追加用户消息 → context_assembler 组装 →
    prompts.build_system_prompt → 带工具的工具循环 → parse_proposals →
    apply_proposals → 写 prompt 日志 → 追加 assistant 消息 → 返回。

    [T10] client_pair_id：前端重发时携带的被重发提问原 pair_id（正常提问为 None）。
    [迭代四 K3] append_user：卡间调起复用主链时传 False——激励文只走当轮
    user_prompt，绝不落收件方档案（「假人声混进档案」禁区）。默认 True ⇒
    全部既有调用方（三参形态）行为逐字不变。
    """

    # [强制停止 2026-10-24] 停止标记闸:halted 的卡拒绝一切**内部**唤醒
    # (goal 续轮已被 pause 治住;这里治收件箱调起/汇聚/巡检这些旁路)。
    # 用户直接发话(source_kind="human")= 解除停止并继续——人是最高权威。
    _halt = control.is_halted(work_id, card_id)
    if _halt is not None:
        if source_kind == "human":
            control.unhalt(work_id, card_id)
            logger.info("[control] 用户直接发话,解除强制停止 card=%s", card_id)
        else:
            session_ops.ops_log("card_halted_reject", work_id, card_id,
                                {"reason": _halt.get("reason"),
                                 "wake": str(source_kind)})
            logger.info("[control] 强制停止中的卡拒绝内部唤醒(%s) card=%s",
                        source_kind, card_id)
            return {"error": {"code": "halted",
                              "message": "该卡已被强制停止:"
                                         + str(_halt.get("reason") or "")[:120]}}
    # [沉默 2026-10-24] 沉默的卡拒内部唤醒(信留收件箱);用户直接发话可达
    # (人是最高权威)但**不解除沉默**——解除唯一口=silence_card(activate)。
    try:
        from ..storage import governance_store as _gov
        if source_kind != "human" and _gov.is_silenced(work_id, card_id):
            session_ops.ops_log("card_silenced_reject", work_id, card_id,
                                {"wake": str(source_kind)})
            return {"error": {"code": "silenced",
                              "message": "该卡处于沉默态(完工后被上级静音),"
                                         "不再接收内部消息"}}
    except Exception:  # noqa: BLE001 治理表读不出 ⇒ 不挡(旧行为)
        pass
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
        # [立刻停止 2026-10-21] 括号里原来是内部回合编号（turn:1a2b3c4d），
        # 用户看不懂也没法处理。说人话 + 给出唯一有效动作。
        return _err(409, "这张卡正在跑（回合进行中）。点「停止」会立刻中断，然后就能发消息。")
    # [立刻停止 2026-10-21 · 用户令] 把本轮协程登记到 control：停止端点直接
    # cancel 它，**不再「置位等步边界」**——一轮模型调用可能跑几分钟，步边界
    # 恰恰在最需要停的时候等不到（实测 card_031 卡在流里 9 分钟，停止无效）。
    _turn_task = asyncio.current_task()
    control.bind_turn_task(work_id, card_id, _turn_task)
    # [自动模式 2026-10-17 · 回灌 5700 块0.3 · 方案 §3.8] 起回合前经**执行闸**：
    # 串行模式下拿不到锁＝**排队等待**（await，不是 409——这是与上面卡互斥体的
    # 关键差别）；并行模式直接过；同卡已持有（＝本卡目标生命周期内继续跑）⇒
    # no-op。取在互斥体之后：同卡重入被互斥体挡在前面，闸上不会出现同卡双取。
    try:
        await serial_gate.acquire(work_id, card_id)
    except asyncio.CancelledError:
        # 停在「等执行闸」这一瞬间：互斥体已登记，必须在这里放掉（还没进下面的 try）
        session_facade.turn_release(work_id, card_id, turn_owner)
        control.unbind_turn_task(work_id, card_id, _turn_task)
        event_stream.reset_stream_context(_stream_ctx_token)
        raise
    failed = False
    # [goal 域 2026-10-20 · 照抄 dsh] 回合来源。工具权威判定（create/edit/pause/
    # resume 必须直接人类回合；complete/blocked 允许当前 goal round）全靠它。
    # 缺省＝人类回合（原版 ``Agent.followup()`` 省略 source 时解析为 user）。
    from ..goal import authority as goal_authority
    from ..goal import driver as goal_driver
    turn_source = dict(source) if source else {"kind": "user"}
    source_token = goal_authority.set_turn_source(turn_source)
    if turn_source.get("kind") == "user":
        # 人插话 ⇒ 自动轮让位（原版 agent/inbox/inserted 的 competingQueued）
        goal_driver.notify_human_input(work_id, card_id)
    outcome = "ok"
    try:
        result = await _run_message_inner(work_id, card_id, content, client_pair_id,
                                          append_user=append_user, op=op, source=turn_source)
    except asyncio.CancelledError:
        # 取消 ⇒ 已预留/已接纳的那一轮必须 pause 目标，绝不能自动重启（原版
        # turn/end reason=aborted 的 idle 检查点语义）。
        outcome = "aborted"
        failed = True
        raise
    except BaseException:
        # 异常＝回合未正常收口（dsh 口径：异常结果**不自动重试**）⇒ 撤销续轮，
        # 执行锁必须在 finally 里放掉，否则串行队列全部饿死。
        outcome = "error"
        failed = True
        raise
    finally:
        # [5715 · 性能] 回合结束**必落盘**缓冲里的思考：思考是按轮累积、到轮界
        # 才合并落一条的；万一这一轮**没走到任何轮界事件**（异常/中断/直退），
        # 缓冲里的思考会**随回合一起丢掉**（刷新后看不到这段思考）。
        # ⇒ 收口时无条件 flush 一次（空缓冲是 no-op）。
        event_stream.flush_pending_reasoning(work_id, card_id)
        # [运行态单一真相源 2026-10-21] 回合终止的**权威帧**：成功 / 异常 / 取消
        # 三条路径都必然发一次。前端运行态据此收口——此前只认 done/error，而它们由
        # compose_delivery / handle_error 发，若链路在两者之前抛（context 组装、
        # 交付拼接等），就一个终止信号都没有 ⇒ 前端永远停在「运行中」，只能刷新。
        # 位置必须在 notify_idle **之前**：idle 会起下一轮，下一轮的 run_start 必须
        # 晚于本帧到达前端，否则前端可能先起后收（顺序反了）。
        try:
            event_stream.emit(work_id, card_id, "turn_end", {"outcome": outcome})
        except Exception as exc:  # noqa: BLE001 帧发不出去不影响回合返回
            logger.warning("[flow] turn_end 帧发送失败 card=%s: %s", card_id, exc)
        # [A8 清扫位] 回合内未被步边界消费的残留停止置位（典型：无工具轮，
        # 调用返回后直接交付）——收口清扫，防误伤下一回合（T124）。
        control.discard_stop(work_id, card_id)
        # [5715] 插话残留清扫（对齐 control.discard_stop 的纪律）：回合窗口外
        # 的插话一律作废——不能留给下一回合，否则那句话会凭空出现在下一轮。
        steer.discard_steer(work_id, card_id)
        session_facade.turn_release(work_id, card_id, turn_owner)
        # [立刻停止 2026-10-21] 解登记本轮协程（传 task 只解自己，防误删后续回合）
        control.unbind_turn_task(work_id, card_id, _turn_task)
        event_stream.reset_stream_context(_stream_ctx_token)
        if failed:
            serial_gate.release(work_id, card_id)
        # [goal 域 2026-10-20 · 照抄 dsh] **idle 事件**：成功/异常/取消三条路径
        # 都报一次。这就是「自动模式会不断唤醒」的落点——不再依赖回合尾部的
        # 顺序调用（原版 ctx.on('agent/status', idle)）。
        try:
            goal_driver.notify_idle(work_id, card_id, outcome)
            goal_driver.notify_parent_idle(work_id, card_id)
        except Exception as exc:  # noqa: BLE001 驱动异常不影响回合返回
            logger.warning("[goal] idle 通知失败 card=%s: %s", card_id, exc)
        goal_authority.reset_turn_source(source_token)
        # [排队语义 2026-10-23 · 用户令] **摘牌之后**再捞队列：
        #   「所有消息排队等这一轮跑完，再下一个回合一起进」。
        # 改前是回合内部自己起下一回合（互斥体还没摘 ⇒ 必然 409，实测一天 82 次
        # mutex_reject / 12 张卡，消息就此搁死）。现在统一在这里接续：
        #   · deliver_now 自带判据：无 pending 不动、有人占着就等、多条一起进一轮；
        #   · 只在**本轮成功收口**时接续——回合失败时自动续跑会变成失败打转，
        #     队列留着，等下一次真实触发（人发消息 / 卡间消息 / 目标续轮）。
        if outcome == "ok":
            try:
                _picked = await message_runner.deliver_now(work_id, card_id)
                if _picked:
                    logger.info("[排队] 摘牌后接续队列 card=%s（消息已在下一回合一起进）",
                                card_id)
            except Exception as exc:  # noqa: BLE001 接续失败不影响本回合返回
                logger.warning("[排队] 摘牌后接续队列失败 card=%s: %s", card_id, exc)
        # [2026-10-25 · 用户裁定「coder 不能自动归档,应可续派任务延续」]
        # 自动归档撤除:完工 coder 保持活卡形态(上下文连续,天然可续派)。
        # 折叠只走显式通道:silence_card(silence)由上级决定。
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
    # [goal 域 2026-10-20] 旧 `auto_driver.after_turn` 的续轮职责已整体移交
    # `src/goal/driver.py`（idle 事件驱动，见上方 finally）；父卡唤醒改由
    # `notify_parent_idle` 承担。此处不再调 after_turn——两套并存会双起点火。
    # [角色机制 5710] 回合收口同步给关注它的角色卡（离树观察者）。
    # 排在互斥体释放之后、与 auto 驱动同级：同步失败/角色卡起不来绝不
    # 影响本回合返回；角色卡自己的回合不再触发（after_turn 内部跳过 role）。
    try:
        await role_room.after_turn(work_id, card_id, result)
    except Exception as exc:  # noqa: BLE001 角色同步异常不影响本回合返回
        logger.error("[role] 回合收口同步异常 work=%s card=%s: %s",
                     work_id, card_id, exc)
    return result


async def _run_message_inner(work_id: str, card_id: str, content: str,
                             client_pair_id: Optional[str] = None,
                             append_user: bool = True,
                             op: str = "",
                             source: Optional[dict[str, Any]] = None) -> Any:
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

    # [goal 域 2026-10-20 · 照抄 dsh pre-step] 续轮回合进入模型**之前**再校验一次
    # 预留是否仍然有效（goal id / revision / round 未变、未 stale、未 cancelled）。
    # 失效 ⇒ 本轮直接拒（不调模型、不落 user 消息），并把预留丢掉让驱动重排。
    if source and source.get("kind") == "goal":
        from ..goal import driver as _goal_driver
        _ok_res, _why = _goal_driver.validate_reservation(work_id, card_id, source)
        if not _ok_res:
            _goal_driver.drop_attempt(work_id, card_id)
            logger.warning("[goal] 续轮预留失效，拒绝本轮 card=%s reason=%s", card_id, _why)
            event_stream.emit(work_id, card_id, "goal_round_rejected",
                              {"round": source.get("round"), "reason": _why})
            _goal_driver.request_drive(work_id, card_id)
            return _err(409, f"续轮预留失效（{_why}），本轮不执行")

    # a0. [T10-1-1] 读取历史之前生成本次请求标识（UUID4 十六进制串），打点含标识与卡号
    pair_id = uuid.uuid4().hex
    state.pair_id = pair_id
    logger.info("[T10] 生成请求标识 pair_id=%s card=%s", pair_id, card_id)

    # ============================================================
    # [起回合带新消息 2026-10-26 · 线上 work_027 067→061 实证]
    # 收件箱里的未处理消息**必须在本轮就能看到**。改前只有 deliver_now 会
    # take_pending，而 deliver_now 只由「有人发信」或「回合收口捞队列」触发
    # ⇒ 空闲卡被人发话起轮时**全程不读收件箱**，sync 这类只落信的消息就永远
    # 看不到（061 实测：只在 read_inbox 的一格 tool_result 里露过一次）。
    #
    # 位置：拼进**本轮模型输入**（user_prompt）之前，**不落任何会话块**——
    # 落块就会占用用户通道（卡间来信会冒充成「用户说的话」，线上实证见 061
    # 对话里那段「上一轮之后，你收到了来自其他卡的消息…」）。
    # 消息的可见面由发送侧落 events 的 msg_* 消息块承载（routes.py:1353 合并渲染）。
    # deliver_now 起轮时传 append_user=False（它自己已取过件），这里不重复取。
    # ============================================================
    _msg_prefix = ""
    if append_user:
        try:
            _pend = await card_db.take_pending(work_id, card_id)
            if _pend:
                _msg_prefix = (await message_runner._build_card_msg_prompt(
                    work_id, _pend)) + "\n\n"
                logger.info("[起回合带新消息] card=%s n=%d", card_id, len(_pend))
        except Exception as exc:  # noqa: BLE001 取件失败不挡本轮
            logger.warning("[起回合带新消息] 取件失败 card=%s: %s", card_id, exc)

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
            _block: dict[str, Any] = {"role": "user", "content": content, "pair_id": pair_id}
            if source and source.get("kind") == "goal":
                # 续轮消息：带 GoalMessageSource，这是 roundsStarted 的唯一推进口
                _block["source"] = dict(source)
            await session_facade.append(work_id, card_id, _block)
            if source and source.get("kind") == "goal":
                from ..goal import driver as _goal_driver
                from ..goal.service import get_service as _goal_service
                _goal_driver.mark_admitted(work_id, card_id, source)
                # roundsStarted 在这一刻推进（fold），投影要同步刷新（见该方法说明）
                await _goal_service().refresh_projection(work_id, card_id)
            logger.info("[T10] 用户消息落盘 pair_id=%s card=%s", pair_id, card_id)

    # [信息流 Task3] run_start：清该卡 journal 重放（A45）＋过程卡秒现（V1）。
    # 位置＝落盘之后、装配之前——先清后写，context 埋点才能留在 journal 里。
    event_stream.emit(work_id, card_id, "run_start",
                      {"pair_id": pair_id, "chars": len(content),
                       # [2026-10-26 · 记录补全] 这一轮**是谁叫醒的**：
                       # {"kind":"user"|"card_msg"|"goal"|"wake", "from": 卡号}
                       # 改前只有 pair_id/chars ⇒ 判据无法区分「父卡派活起的轮」
                       # 与「用户随口问一句起的轮」。
                       "source": dict(source or {})})

    # 段②③前半：上下文组装＋工具列＋轮槽
    message_runner.build_context(state)

    # [思维链真实落地 2026-10-11] 判断归模型（C1）：判断协议随 BASE_PROMPT
    # 进 system 区（prompts.py），模型首轮带全量工具自主判断——直答（空手
    # 正文即答复）或调 create_plan 拆解上板。代码层零判定、零机械预检。
    state.user_prompt = _msg_prefix + content

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
