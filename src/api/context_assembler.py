# -*- coding: utf-8 -*-
"""上下文组装器（白板版 2026-10-02）。

只组装机械事实，不注入任何内容：
- 卡片职责：类型注册表里每类卡一行的职责说明（类型级，非卡级）
- 卡片清单：全部卡的 id · 标题 · 父卡（不含类型、不含正文摘要、无空位提示）
- 当前卡：id · 标题（正文不推送——任何卡片正文一律经 read_card 拉取，
  杜绝"上轮模型写入的卡片内容变成下轮常设输入"的回流）
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from ..storage import card_db, card_store, memory_store, work_store
from ..storage.hash_utils import parse_front_matter
from . import context_regions

logger = logging.getLogger("api.context_assembler")

BASE_DIR = Path(__file__).resolve().parents[2]


def _load_cards_index(work_id: str) -> list[dict[str, Any]]:
    """查库，返回 [{card_id, title, parent}]（按 card_id 排序）。

    [卡片存储迁移 SQLite 2026-10-14] 数据源从 ``cards/*.md`` 改为库——
    **这是「减污染」的关键点**：卡片清单仍只取 id/title/parent 三样，
    取什么查什么，正文不再有机会被整份读进上下文（方案 §3.1 B）。
    """
    def _fn(conn: Any) -> list[dict[str, Any]]:
        import json as _json
        out: list[dict[str, Any]] = []
        # [依赖关联线 2026-10-16] 多带一列 relations（依赖关联线）——供
        # build_ctx 反向扫（谁依赖/关联我）零额外 IO。清单渲染只用前三键，
        # 多带一列不影响它。
        # [第二层区 2026-10-20] 多带 created_at：供「组阁之后又新增了卡」这条机械信号
        # （清单渲染仍只用 id/title/parent，多带的列不进上下文）。
        # [2026-11-01] ① relations 权威源是 card_relations 边表（cards.relations
        #   列只是双写回退）——此前这里只读旧列，绕开 update_card 写的边会丢；
        #   ② 多带 requirements：250 区要把被依赖卡的**当前**对外信息投影给
        #   依赖方（「接口改了经关系线自动更新」，不需要通知推送）。
        edge_rels: dict[str, list[dict[str, Any]]] = {}
        try:
            for e in conn.execute(
                    "SELECT from_card,to_card,kind,detail FROM card_relations "
                    "ORDER BY rowid"):
                edge_rels.setdefault(e["from_card"], []).append(
                    {"target": e["to_card"], "kind": e["kind"],
                     "detail": e["detail"]})
        except Exception:  # noqa: BLE001 边表不在 ⇒ 全靠列回退
            edge_rels = {}
        for r in conn.execute(
                "SELECT card_id, title, parent, relations, created_at, card_type,"
                " requirements FROM cards ORDER BY card_id"):
            try:
                rels = _json.loads(r["relations"] or "[]")
            except (TypeError, ValueError):
                rels = []
            cid = r["card_id"]
            out.append({"card_id": cid, "title": r["title"] or "",
                        "parent": r["parent"] or "",
                        "relations": (edge_rels[cid] if cid in edge_rels
                                      else (rels if isinstance(rels, list) else [])),
                        "created_at": r["created_at"] or "",
                        "card_type": r["card_type"] or "",
                        "requirements": r["requirements"] or ""})
        return out

    return card_db.read_sync(work_id, _fn, []) or []


def _dep_relations_for(cards: list[dict[str, Any]], current_id: str,
                       cur_meta: dict[str, Any]) -> tuple[list[dict[str, Any]],
                                                          list[dict[str, Any]]]:
    """在已加载的全库卡索引上算**双向**依赖关联线（零额外 IO）。

    - 正向 ``out``＝当前卡自己的 ``relations``；
    - 反向 ``in`` ＝全库扫 ``target == current_id`` 的条目，补上发起卡 ``source``。

    纯计算：只读传入的索引与当前卡 meta，不查库（装配链的唯一 IO 在别处）。
    """
    raw_out = (cur_meta or {}).get("relations")
    out_entries = ([r for r in raw_out if isinstance(r, dict)]
                   if isinstance(raw_out, list) else [])
    in_entries: list[dict[str, Any]] = []
    req_of = {str(c.get("card_id") or ""): str(c.get("requirements") or "")
              for c in cards or []}
    for c in cards or []:
        cid = c.get("card_id")
        if cid == current_id:
            continue
        for r in (c.get("relations") or []):
            if isinstance(r, dict) and str(r.get("target") or "") == current_id:
                in_entries.append({"source": cid, **r})
    # [2026-11-01 · 用户令「接口改了经关系线自动更新，不需要通知」] 出边条目
    # 带上目标的**当前**需求文档（对外信息）：依赖方的上下文每轮装配即最新，
    # 卡面是唯一事实源——读侧投影永不过期。
    out_entries = [{**r, "requirements": req_of.get(str(r.get("target") or ""), "")}
                   for r in out_entries]
    return out_entries, in_entries


#: [上游需求文档 2026-11-10 · 用户令「UI、qa、pm、验收卡都应该看到其父卡的
#: 需求文档，需求文档是它们的参照物，是应该直接进入它们上下文的」]
#: [2026-11-10 · 用户补令] worker（编程卡）也要——委派执行体路径由 kick
#: 正文带，系统回合路径靠本区罩住（两路同一事实源）。
#: 只有这类执行/差事卡取数——别的卡型零 IO。
_UPSTREAM_REQ_TYPES = {"ui", "worker", "qa_card", "pm_card"}


def _load_upstream_requirements(work_id: str, current_id: str,
                                cur_meta: dict[str, Any],
                                cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """执行/差事卡的上游需求文档**投影**（参照尺，读侧投影＝每轮装配现取）。

    - ui/worker → 父卡的需求文档；
    - qa_card/pm_card → 父卡（派遣方）＋对象卡（差事账 ``target_card``，与父卡
      不同才单列——target 缺省＝派遣方时会重）。

    requirements 列已在 ``_load_cards_index`` 里 ⇒ 卡面事实**零额外 IO**；
    唯一新查询是差事账（且只对 qa/pm 类型发）。读侧投影，不写第二份。
    """
    ctype = str((cur_meta or {}).get("card_type") or "")
    if ctype not in _UPSTREAM_REQ_TYPES:
        return []
    req_of = {str(c.get("card_id") or ""):
              (str(c.get("title") or ""), str(c.get("requirements") or ""))
              for c in cards or []}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(label: str, cid: str) -> None:
        if not cid or cid in seen:
            return
        title, req = req_of.get(cid, ("", ""))
        req = str(req or "").strip()
        # 占位模板不算正稿（新卡骨架）——「没有参照物不硬撑一行」。
        # 判据复用 card_draft._is_placeholder（与子卡区同一口径，不另起一套）。
        if not req:
            return
        from .card_draft import _is_placeholder  # noqa: PLC0415 延迟 import 破环
        if _is_placeholder(req):
            return
        seen.add(cid)
        out.append({"label": label, "card_id": cid,
                    "title": title, "requirements": req})

    parent = str((cur_meta or {}).get("parent") or "")
    _add("父卡", parent)
    if ctype in ("qa_card", "pm_card"):
        try:
            from ..storage import function_store as _fs
            for dep in _fs.active_for_venue(work_id, current_id,
                                          venue_kind="deploy"):
                tgt = str(dep.get("target_card") or "")
                if tgt and tgt != parent:
                    _add("对象卡", tgt)
        except Exception:  # noqa: BLE001 差事账读不到＝只给父卡那份
            pass
    return out


def _load_direct_child_status(work_id: str, parent_id: str) -> list[dict[str, Any]]:
    """只取当前卡的**直接子卡** id/title/status_block/mission/card_type＋活性。

    **SQL 不递归**（`WHERE work_id=? AND parent=?`，只一层）⇒ 孙级在结构上取不到
    ——「父卡只看直接子级」不靠过滤逻辑保证，靠查询本身。
    [2026-10-27 用户裁定] 加 `mission`：父卡的判断是「三块填没填」（原文：
    「我核对一下两张卡的使命、现状和需求文档，分别确认哪些已填」），只给现状
    等于答非所问 ⇒ 实测父卡每轮还去 read_card 135 次。**需求文档仍不给**（太长）。
    仍不读子卡的 history、不碰影子。

    [2026-11-02 · 用户令「父卡需要看到子卡的实时状态」] 每个子卡带一行
    **活性**（运行中/待推进/已暂停/卡住了/已收口 · 最近活动 · 已跑轮次），
    与 read_card 的「活性」字段同一出处（card_tools._card_activity）——
    同一个口径，装配即最新，不需要模型再逐卡 read_card 查活没活着。
    card_type 一并选出：渲染层跳过 worker 的「现状」要靠它（此前 SQL 没选，
    那个分支一直是死的）。
    """
    if not parent_id:
        return []

    def _fn(conn: Any) -> list[dict[str, Any]]:
        return [
            {"card_id": r["card_id"], "title": r["title"] or "",
             "card_type": r["card_type"] or "",
             "mission": r["mission"] or "",
             "status_block": r["status_block"] or "",
             "goal_state": r["goal_state"] or "",
             "goal_rounds": r["goal_rounds"] or 0}
            for r in conn.execute(
                "SELECT card_id, title, card_type, mission, status_block,"
                " goal_state, goal_rounds FROM cards "
                "WHERE work_id = ? AND parent = ? ORDER BY card_id",
                (work_id, parent_id),
            )
        ]

    rows = card_db.read_sync(work_id, _fn, []) or []
    try:
        from . import card_tools as _ct  # 延迟 import：card_tools 反向依赖本包
        for row in rows:
            row["activity"] = _ct._card_activity(work_id, row["card_id"], row)
    except Exception:  # noqa: BLE001 活性算不出不挡上下文装配
        for row in rows:
            row.setdefault("activity", "")
    return rows


def _load_current_card_state(work_id: str, card_id: str
                             ) -> tuple[dict[str, Any], dict[str, str]]:
    """当前卡正文三块 ＋ 影子四节（**150 区常驻用，一次 IO 拿全**）。

    [常驻当前卡 2026-10-19 · permexp] 用户令：卡片自己的现状与影子常驻上下文，
    模型不必每轮 read_card / read_shadow。取数与渲染分离（本函数只取数）。
    取不到（卡不存在/库异常）⇒ 空 dict，渲染器按「空块不出现」自然降级。
    """
    def _fn(conn: Any) -> tuple[dict[str, Any], dict[str, str]]:
        row = conn.execute(
            "SELECT mission, status_block, requirements FROM cards WHERE card_id = ?",
            (card_id,),
        ).fetchone()
        if row is None:
            return ({}, {})
        body = {
            "mission": row["mission"] or "",
            "status_block": row["status_block"] or "",
            "requirements": row["requirements"] or "",
        }
        sh = conn.execute(
            # [卡片笔记 2026-10-27] 带上 note：笔记常驻 150 区（本卡私有）。
            "SELECT progress, coordination, artifacts, iterations, note "
            "FROM shadows WHERE owner_card_id = ?",
            (card_id,),
        ).fetchone()
        shadow = (
            {
                "progress": sh["progress"] or "",
                "coordination": sh["coordination"] or "",
                "artifacts": sh["artifacts"] or "",
                "iterations": sh["iterations"] or "",
                "note": (sh["note"] if "note" in sh.keys() else "") or "",
            }
            if sh is not None else {}
        )
        return (body, shadow)

    try:
        return card_db.read_sync(work_id, _fn, ({}, {})) or ({}, {})
    except Exception:  # noqa: BLE001 取不到＝无来源，不阻断装配
        return ({}, {})


def _control_path() -> Path:
    """控制面配置位置；调用时派生路径，不缓存模块级路径常量。"""
    return BASE_DIR / "config" / "context_regions.json"


def _type_texts_path() -> Path:
    """「按卡类型挂的文本」（多维度）配置位置；调用时派生，测试可 monkeypatch。"""
    return BASE_DIR / "config" / "type_texts.json"


def load_type_texts() -> dict[str, dict[str, str]]:
    """读「按卡类型挂的文本」（多维度）。**fail-open**：读不到/坏 JSON/结构不对 → 空表。

    与类型注册表同纪律：**配置是控制源，不是可用性依赖**——绝不因它阻断装配。
    返回 ``{维度: {类型: 文本}}``；每个维度只保留 value 为非空字符串的条目。
    下划线前缀键（如 ``_meta``）是文档元数据、不是维度，跳过。
    类型清单不在本文件、不校验（唯一来源＝类型注册表）。
    """
    import json as _json
    try:
        raw = _type_texts_path().read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[type_texts] 配置读取失败（视为全空）: %s", exc)
        return {}
    try:
        data = _json.loads(raw)
    except ValueError as exc:
        logger.warning("[type_texts] 配置解析失败（视为全空）: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for dim, table in data.items():
        if str(dim).startswith("_") or not isinstance(table, dict):
            continue
        clean = {str(k): v for k, v in table.items()
                 if isinstance(v, str) and v.strip()}
        if clean:
            out[str(dim)] = clean
    return out


def load_control() -> dict[str, Any]:
    """读控制面配置（区的启停/顺序）。**fail-open**：读不到/坏 JSON → 空表＝全用注册表默认。

    纪律同类型注册表：**配置是控制源，不是可用性依赖**——绝不因它阻断装配。
    """
    import json as _json
    try:
        raw = _control_path().read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[context_regions] 控制面配置读取失败（用注册表默认）: %s", exc)
        return {}
    try:
        data = _json.loads(raw)
    except ValueError as exc:
        logger.warning("[context_regions] 控制面配置解析失败（用注册表默认）: %s", exc)
        return {}
    regs = data.get("regions") if isinstance(data, dict) else None
    return regs if isinstance(regs, dict) else {}


# [2026-11-01 幻影区告警去重] （work_id, 孤儿区名集合）已告警过的组合不重复打。
_ORPHAN_RULE_WARNED: set[tuple[str, tuple[str, ...]]] = set()


def _load_rule_sections(work_id: str) -> dict[str, str]:
    """读本项目的规则分区（区名→正文），只保留有内容的区。

    [规则节点 2026-10-28 · 用户令「一个项目一个文件」] 规则是本项目的基建文件
    ``works/<work_id>/rules.md``：不占 card_id、不进 cards 表、不上列表/画布。
    ``work_store.get_rules`` 已转发到 ``rule_store``（含 work.json 旧形态的惰性
    迁移），所以这里不再需要「扫规则卡」那段 legacy 兜底——那次迁移发生在
    ``rule_store.read_sections`` 内部，只做一次。
    """
    try:
        from ..storage import work_store as _ws
        rules = _ws.get_rules(work_id)
    except Exception:  # noqa: BLE001 读不到＝无来源，不阻断装配
        return {}
    out = {str(k): str(v or "").strip() for k, v in (rules or {}).items()
           if str(v or "").strip() and str(k).strip()}
    # [2026-11-01 幻影区告警] 契约名单外且非空的区＝写了但任何卡都读不到的
    # 孤儿区（实案：一篇带 `## 一、`…`## 七、` 的文档落进 project 区后被切
    # 碎，规则静默零注入）。装配是沉默发生的地方——在这里留痕，去重防刷屏。
    try:
        from . import context_regions as _cr, function_room as _fr
        from ..storage import card_store as _cs
        known = set(_cr.rule_section_names(
            _cs.load_type_registry(),
            fn_codes=[str(e.get("code") or "")
                      for e in (_fr.entries() or [])]))
        orphans = sorted(k for k in out if k not in known)
        if orphans:
            key = (work_id, tuple(orphans))
            if key not in _ORPHAN_RULE_WARNED:
                _ORPHAN_RULE_WARNED.add(key)
                logger.warning(
                    "[rule] %s 存在 %d 个非契约分区（内容不进任何卡的上下文）："
                    "%s——归位到契约区或从文件移除", work_id, len(orphans),
                    "、".join(orphans))
    except Exception:  # noqa: BLE001 告警失败不阻断装配
        pass
    return out


def _load_role_cabinet(work_id: str, current_id: str,
                        cards: list[dict[str, Any]]) -> dict[str, Any]:
    """取「第二层职能角色」现状与机械信号（只对项目根卡取数，其它卡返回空）。

    **为什么要有这个区**：实测（card_004 的 D06/D07）——根卡手上有全部调阁工具、
    也知道有角色（`list_roles` → `read_card` 读过），但局面变了它不会去改档/撤岗；
    组阁更是「问一次才有」。结论：**调阁触发必须外置成机制**，不能靠文档提醒。

    这里只做两件事，**都不含任何"你应该组阁"的指令**（D02：职能不定死，
    设不设/怎么设是 CEO 的判断）：
      1. 把当前阁员摊开（域/值班/职责书版本/关注/最近一次判词）——省掉它自己查；
      2. 把**机械能检出的局面变化**列成事实（新卡没人看、角色空转、连续沉默、已撤岗）。
    最后落一句中性问话，交由模型判断。

    取数口径：一次 read_sync 查库（role_profiles ＋ 每人最近 3 条判词），零逐人 IO。
    """
    root_id = ""
    try:
        root_id = work_store.work_card_id_of(work_id)
    except Exception:  # noqa: BLE001 取不到根卡＝无来源，不阻断装配
        return {}
    if not root_id or current_id != root_id:
        return {}          # 只有根卡能调阁，别的卡不给这个区

    def _fn(conn: Any) -> dict[str, Any]:
        import json as _json
        roles: list[dict[str, Any]] = []
        for r in conn.execute(
                "SELECT card_id, jurisdiction, watch, duty, charter, created_at"
                " FROM role_profiles WHERE work_id=? ORDER BY card_id", (work_id,)):
            def _j(raw: Any) -> dict[str, Any]:
                try:
                    v = _json.loads(raw or "{}")
                except (TypeError, ValueError):
                    return {}
                return v if isinstance(v, dict) else {}
            jur, watch, duty, charter = (_j(r["jurisdiction"]), _j(r["watch"]),
                                         _j(r["duty"]), _j(r["charter"]))
            vs = [dict(x) for x in conn.execute(
                "SELECT ts, decision FROM role_verdicts WHERE work_id=? AND role_id=?"
                " ORDER BY ts DESC LIMIT 3", (work_id, r["card_id"]))]
            roles.append({
                "role_id": r["card_id"],
                "exclusive_key": str(jur.get("exclusive_key") or jur.get("domain") or ""),
                "watch": [str(c) for c in (watch.get("cards") or [])],
                "duty": str(duty.get("state") or "active"),
                "charter_version": int(charter.get("version") or 1),
                "created_at": r["created_at"] or "",
                "recent": vs,
            })
        return {"roles": roles}

    try:
        return card_db.read_sync(work_id, _fn, {}) or {}
    except Exception as exc:  # noqa: BLE001 该区取不到就不出现，绝不影响装配
        logger.warning("[region] 第二层取数失败 work=%s: %s", work_id, exc)
        return {}


def _load_function_roster(work_id: str, current_id: str) -> list[dict[str, Any]]:
    """取本 work 的**全部职能档案**（93 区「现有职能」的数据源）。

    **只在装配层取一次**，渲染器只读（context_regions 是纯函数、不做 IO——
    模块头硬约束）。取自 ``function_store.list_profiles``（function_profiles 表，
    职能**没有 cards 行**，所以这里只能直接查职能表，查 cards 是取不到的）。

    只对项目根卡取：只有根卡能组阁/调阁，把职能清单摊给别的卡既无用也是噪音。
    取不到（库异常／work 不存在）⇒ 空列表 ⇒ 区不出现（fail-open，不阻断装配）。
    """
    root_id = ""
    try:
        root_id = work_store.work_card_id_of(work_id)
    except Exception:  # noqa: BLE001 取不到根卡＝没有来源，不阻断装配
        return []
    if not root_id or current_id != root_id:
        return []                      # 非根卡：取都不取

    try:
        from ..storage import function_store
        return function_store.list_profiles(work_id) or []
    except Exception as exc:  # noqa: BLE001 该区取不到就不出现，绝不影响装配
        logger.warning("[region] 职能清单取数失败 work=%s: %s", work_id, exc)
        return []


def _load_dispatch_targets(work_id: str, card_id: str,
                           deployment_id: int) -> list[str]:
    """只取当前回合指明、且归属本卡的在派差事对象。"""
    try:
        from ..storage import function_store as _fs
        rows = _fs.active_for_venue(work_id, card_id, venue_kind="deploy")
        return [str(dep["target_card"]) for dep in rows
                if dep.get("id") == deployment_id and dep.get("target_card")]
    except Exception:  # noqa: BLE001 差事账读不到＝判不出对象⇒回退 unknown
        return []


def _load_board_criteria(work_id: str, card_id: str) -> str:
    """活板**当前步**的判据文本（没有板／没有当前步 ⇒ 空串）。

    活板是「任务判据」的结构化载体（步自带 criteria/deliverable）。
    取不到不阻断装配（判据缺 ⇒ 锚缺 ⇒ 依赖正稿定位不到 ⇒ 按档位回退）。
    """
    try:
        from ..thinkchain import board as _board
        snap = _board.snapshot_sync(work_id, card_id)
    except Exception:  # noqa: BLE001 取板失败＝无判据，不阻断装配
        return ""
    if not snap:
        return ""
    try:
        cur = int(snap.get("current") or 0)
        steps = snap.get("steps") or []
        if not (0 <= cur < len(steps)):
            return ""
        step = steps[cur] or {}
        return " ".join(str(step.get(k) or "") for k in ("criteria", "prompt"))
    except Exception:  # noqa: BLE001 板结构怪＝无判据，不阻断装配
        return ""


def _resolve_task_scope(work_id: str, current_id: str, cur_meta: dict[str, Any],
                        children: list[dict[str, Any]],
                        scope_hint: dict[str, Any] | None) -> Any:
    """判定本轮任务范围（批二·按任务缩量）。**scope_hint 为 None ⇒ 不判定**。

    不判定 ⇒ unknown ⇒ 渲染逐字同旧（既有调用方、既有测试零行为变化）。
    """
    from . import task_scope as _ts
    if scope_hint is None:
        return _ts.TaskScope(_ts.MODE_UNKNOWN, basis="not_requested",
                              reason="调用方未请求范围判定（默认逐字同旧）")
    # [批二·按任务缩量 2026-11-20] **回退闸**：CONTEXT_SCOPE=0 ⇒ 一律完整材料
    # （试点期可整体退回既有装配，不必回滚代码；与 CONTEXT_BOARD=0 同族口径）。
    if os.environ.get("CONTEXT_SCOPE") != "1":
        return _ts.TaskScope(_ts.MODE_UNKNOWN, basis="switch_off",
                              reason="CONTEXT_SCOPE=0 ⇒ 完整材料回退")
    src = dict(scope_hint.get("source") or {})
    is_goal_turn = src.get("kind") == "goal" and bool(src.get("goalId"))
    deployment_id = src.get("deployment_id")
    targets = (_load_dispatch_targets(work_id, current_id, deployment_id)
               if src.get("kind") == "deploy" and isinstance(deployment_id, int) else [])
    return _ts.resolve_scope(
        card_id=current_id,
        card_type=str((cur_meta or {}).get("card_type") or ""),
        children=children,
        source=src,
        source_kind=str(scope_hint.get("source_kind") or ""),
        act_as=str(scope_hint.get("act_as") or ""),
        ctx_card=str(scope_hint.get("ctx_card") or ""),
        dispatch_targets=targets,
        board_criteria=_load_board_criteria(work_id, current_id) if is_goal_turn else "",
        readback_available=bool(scope_hint.get("readback_available", True)),
        # [返工 2026-11-20] **显式任务绑定**：回合入口/调用方最清楚本轮任务
        # 绑在哪（``{"kind": "card"|"jurisdiction", "card_id": ...}``）。
        task_binding=scope_hint.get("task_binding"),
        own_task=is_goal_turn,
    )


def _load_cards_index_light(work_id: str) -> list[dict[str, Any]]:
    """只取清单要的列（id·标题·父卡·卡型·建卡时间）——**不取正稿**。

    [批二·按任务缩量 2026-11-20] 同一回合内刷新当轮事实时用：清单区只用
    这几列，正稿（requirements）是依赖区的数据源、且依赖区走 system 通道
    （同回合缓存）⇒ 刷新时不带它，省掉一次十几万字的读。
    """
    def _fn(conn: Any) -> list[dict[str, Any]]:
        return [
            {"card_id": r["card_id"], "title": r["title"] or "",
             "parent": r["parent"] or "", "card_type": r["card_type"] or "",
             "created_at": r["created_at"] or ""}
            for r in conn.execute(
                "SELECT card_id, title, parent, card_type, created_at "
                "FROM cards ORDER BY card_id")
        ]

    return card_db.read_sync(work_id, _fn, []) or []


def refresh_tail_facts(ctx: context_regions.RegionCtx, work_id: str,
                       card_id: str) -> None:
    """**同一逻辑回合内**刷新当轮事实（清单 ＋ 直接子卡现状 ＋ 对象卡正文）。

    [批二·按任务缩量 2026-11-20 · 成本优化任务书 §二.3] 清单与子卡现状走
    **tail 通道**（每个请求重渲染），但它们的数据取自**回合开始时**装配的
    那份 ctx ⇒ 回合里子卡跑了一步、建了新卡，下一跳仍看不到旧版本。
    ⇒ 每次工具续跳前刷新这两份数据：下一跳看到**当前有效**的那一份。

    [返工 2026-11-20] **对象卡正文（scope_target_body）也在 tail 里** ⇒ 同样
    要刷：对象卡现状更新后，下一跳仍发旧正文就是错版（复核复现过）。

    只刷 tail 绑定的数据：system 通道的区走同回合缓存（稳定前缀不动）。
    已有卡保留原正稿（依赖区若重渲染仍拿得到全文），新卡只带清单要的列。
    """
    try:
        light = _load_cards_index_light(work_id)
    except Exception:  # noqa: BLE001 刷新失败＝沿用旧版本，不阻断回合
        return
    old = {str(c.get("card_id") or ""): c for c in (ctx.cards or [])}
    merged: list[dict[str, Any]] = []
    for c in light:
        cid = str(c.get("card_id") or "")
        prev = old.get(cid)
        if prev is None:
            merged.append(dict(c))          # 新卡：清单要的列齐了
            continue
        for k in ("title", "parent", "card_type", "created_at"):
            prev[k] = c.get(k, prev.get(k, ""))
        merged.append(prev)
    ctx.cards = merged
    try:
        ctx.direct_child_status = _load_direct_child_status(work_id, card_id)
    except Exception:  # noqa: BLE001 刷不到＝沿用旧版本，不阻断回合
        pass
    # 对象卡正文：重新按**当前读权限**取材（权限可能也变了）
    scope = getattr(ctx, "task_scope", None)
    tgt = str(getattr(scope, "target_card", "") or "") if scope is not None else ""
    if (scope is not None and getattr(scope, "mode", "") == "single"
            and tgt and tgt != card_id):
        ctx.scope_target_body = _load_target_body(
            work_id, card_id, tgt, merged)


def _load_target_body(work_id: str, current_id: str, target_id: str,
                       cards: list[dict[str, Any]]) -> dict[str, Any]:
    """对象卡正文**按现有读权限逐块取材**（与 ``read_card`` 同一道闸）。

    [返工 2026-11-20 · 复核算二] 之前这里是**无条件**投影对象卡的使命/现状 ⇒
    隔离库里复现过：一条**未授予 read_card** 的差事，工具回 denied，目标卡的
    现状却进了模型上下文——**绕过读权限的暗门**。

    现在逐块按既有权限取：

    - **父子线** ⇒ 可读全卡面（使命＋现状）；
    - **关系线** ⇒ 只可读「需求文档」（对外信息）⇒ 使命/现状**不给**
      （需求文档由依赖区按关系线投影，权限一致）；
    - **无线** ⇒ 查差事授面（``_deployment_scope(..., "read_card")``）——
      差事实发面含 read_card 才算临时读权；否则**不给**。

    与 ``card_tools._h_read_card`` 同一套闸（同一行判定、同一 denials）。
    """
    if not target_id or target_id == current_id:
        return {}
    cards_map = {str(c.get("card_id") or ""): c for c in (cards or [])
                 if str(c.get("card_id") or "")}
    try:
        from . import card_tools as _ct
        line = _ct._line_between(work_id, current_id, target_id, cards_map)
    except Exception:  # noqa: BLE001 闸算不出来＝不给（不绕过）
        return {}
    full_read = False
    if line == "parent":
        full_read = True
    elif line == "relation":
        full_read = False          # 关系线只读需求文档 ⇒ 使命/现状不给
    else:
        try:
            from . import card_tools as _ct
            full_read = bool(_ct._deployment_scope(
                work_id, current_id, target_id, "read_card"))
        except Exception:  # noqa: BLE001 授面算不出来＝不给
            full_read = False
    if not full_read:
        return {}

    def _fn(conn: Any) -> dict[str, Any]:
        row = conn.execute(
            "SELECT card_id, title, mission, status_block FROM cards "
            "WHERE card_id = ?", (target_id,)).fetchone()
        if row is None:
            return {}
        return {"card_id": row["card_id"], "title": row["title"] or "",
                "mission": row["mission"] or "",
                "status_block": row["status_block"] or ""}

    try:
        return card_db.read_sync(work_id, _fn, {}) or {}
    except Exception:  # noqa: BLE001 取不到＝不出该段，不阻断装配
        return {}


def build_ctx(work_id: str, current_id: str,
              memory_state: dict[str, Any] | None = None,
              history_source: list[dict[str, Any]] | None = None,
              function_meta: dict[str, Any] | None = None,
              history_extra: list[dict[str, Any]] | None = None,
              current_pair_id: str = "",
              scope_hint: dict[str, Any] | None = None,
              history_id: str = "",
              memory_snapshot: dict[str, Any] | None = None) -> context_regions.RegionCtx:
    """取数一次（**装配链上唯一的 IO 处**）：卡片索引／当前卡／树类／类型注册表／凝练态。

    渲染器（`context_regions`）保持纯函数——它们只读这个 ctx，不自己取数。

    ``function_meta``（[card_041]）：职能回合的「我是谁」档案。职能没有 cards 行，
    由 function_room 回合执行器把 function_store.get_profile 的结果显式传进来，
    145 区（function_self）才会渲染。缺省 None ⇒ 行为与既往逐字不变（区不出）。

    ``scope_hint``（[批二·按任务缩量 2026-11-20]）：本轮任务范围的判定输入
    （回合来源／身份锚／读卡能力）。**缺省 None ⇒ 不判定 ⇒ 范围 unknown ⇒
    清单／子卡／依赖三处逐字同旧**（既有调用方零行为变化）。
    """
    cards = _load_cards_index(work_id)
    cur = next((c for c in cards if c["card_id"] == current_id), None)
    try:
        pc = card_store._root_project_class(work_id)
    except Exception:  # noqa: BLE001 取不到树类＝无来源，不阻断装配
        pc = ""
    # [§三.2 · 2026-11-20] 同版本快照：memory 行＋节点索引＋缓存板在一个读
    # 事务里取回——一次模型请求看到的是同一版本，不再「新水位配旧板」。
    history_id = history_id or current_id
    snap = memory_snapshot
    if snap is None and memory_store.condense_enabled():
        snap = memory_store.read_snapshot(work_id, history_id)
    state = memory_state
    if snap is not None:
        _snap_state = snap.get("state") or {}
        state = _snap_state
    elif state is None and memory_store.condense_enabled():
        state = memory_store.load_memory(work_id, history_id)
    cur_meta = card_store._meta_of(work_id, current_id) or {}
    # [常驻当前卡 2026-10-19 · permexp] 当前卡正文＋影子一次读全（150 区常驻）。
    cur_body, cur_shadow = _load_current_card_state(work_id, current_id)
    # [依赖关联线 2026-10-16] 双向依赖关联线：在已加载的 cards 索引上算（零额外 IO）。
    dep_out, dep_in = _dep_relations_for(cards, current_id, cur_meta)
    children = _load_direct_child_status(work_id, current_id)
    # [批二·按任务缩量 2026-11-20] 本轮任务范围判定（**由可信任务绑定驱动**；
    # 判不出来＝unknown ⇒ 逐字同旧）。判定所需取数（差事账对象／活板当前步判据／
    # 目标续轮标记）在本处一次取完。
    scope = _resolve_task_scope(work_id, current_id, cur_meta, children, scope_hint)
    # single 档且对象≠本卡 ⇒ 把对象卡正文带进首跳（**按现有读权限逐块取材**）
    target_body: dict[str, Any] = {}
    if (scope is not None and getattr(scope, "mode", "") == "single"
            and scope.target_card and scope.target_card != current_id):
        target_body = _load_target_body(
            work_id, current_id, scope.target_card, cards)
    # [批三·评审按阶段缩量 2026-11-20 · 任务书 §三] 受限当轮决定：席位回合
    # 材料快照判 restricted ⇒ 本轮只发完成表态所需的材料——清单/子卡/被评
    # 长历史/记忆/依赖全文在渲染侧跳过（REGION_TABLE 各区自查
    # `ctx.turn_decision`）。快照不带 ⇒ 空 dict ⇒ 逐字同旧。
    turn_decision: dict[str, Any] = {}
    _src = (scope_hint or {}).get("source") or {}
    if isinstance(_src, dict):
        _mat = _src.get("material")
        if isinstance(_mat, dict) and _mat.get("restricted"):
            turn_decision = dict(_mat)
    hs = list(history_source or [])
    board_rows = (snap or {}).get("board_rows") if snap is not None else None
    snap_hist = (snap or {}).get("raw_history")
    if turn_decision:
        # 被评卡长历史整段不发——场内的有效用户发言/席位过程走
        # history_extra 同一通道照常在（一条材料只经一个入口）。
        hs = []
        board_rows = []
        snap_hist = []
    return context_regions.RegionCtx(
        work_id=work_id,
        card_id=current_id,
        cards=cards,
        work_card_id=work_store.work_card_id_of(work_id),
        cur=cur,
        cur_meta=cur_meta,
        cur_body=cur_body,
        cur_shadow=cur_shadow,
        dep_relations_out=dep_out,
        dep_relations_in=dep_in,
        direct_child_status=children,
        # [批二·按任务缩量 2026-11-20] 本轮任务范围（unknown ⇒ 逐字同旧）。
        task_scope=scope,
        # [批三·评审按阶段缩量 2026-11-20] 受限当轮决定（空 ⇒ 逐字同旧）。
        turn_decision=turn_decision,
        # [批二·按任务缩量 2026-11-20] 对象卡卡面（single 档且对象≠本卡）。
        scope_target_body=target_body,
        # [上游需求文档 2026-11-10] 执行/差事卡的参照尺（类型闸外零 IO）。
        upstream_requirements=_load_upstream_requirements(
            work_id, current_id, cur_meta, cards),
        # [第二层区 2026-10-20] 只对根卡取数（别的卡返回 {}，区不出现）。
        role_cabinet=_load_role_cabinet(work_id, current_id, cards),
        # [function 实体化第二步 2026-10-21 · card_031] 职能两区取数一次（装配层）。
        # function_roster 只对根卡非空（别的卡空列表 ⇒ 93 区不出现）；
        # function_meta 由调用方显式传入（[card_041] 职能回合执行器喂档案，
        # 145 区 function_self 才会渲染；缺省空 ⇒ fail-open 空串，行为不变）。
        function_roster=_load_function_roster(work_id, current_id),
        function_meta=dict(function_meta or {}),
        project_class=pc,
        type_registry=card_store.load_type_registry(),
        type_texts=load_type_texts(),
        memory_state=state,
        # [记忆编号体系 2026-10-29] 记忆区改吃 `memory_nodes`（条目级事实源）：
        # 每行一个节点、自带编号与时间戳。不再读 `chain` 里的 entries——
        # 装配与渲染各读一份不同结构，正是上次「92 行 [块?]」的根因。
        # [2026-10-31 · 修 V6 开关契约] 改前 load_nodes 无条件执行 ⇒ 开关关
        # （V6：关＝装配逐字旧路）记忆区照样出——RD-08 契约破。
        # 现在与 memory_state 同闸：开关关且调用方未显式喂记忆态 ⇒ 零取数、区不出。
        memory_entries=((snap or {}).get("nodes")
                        if snap is not None else
                         (memory_store.load_nodes(work_id, history_id)
                         if (memory_store.condense_enabled() or state is not None) else [])),
        # [§三.2] 板行随快照同版本下发；None＝快照判板不新鲜，渲染侧走懒重建旧路
        board_rows=board_rows,
        snapshot_history=snap_hist,
        controls=load_control(),
        rule_sections=_load_rule_sections(work_id),
        history_source=hs,
        history_id=history_id,
        history_mark=int((state or {}).get("mark", 0) or 0),
        history_extra=list(history_extra or []),
        current_pair_id=str(current_pair_id or ""),
        # [harness 提示词注入 2026-10-28 · 第三道接口] 取走并清空注入队列。
        # 一次装配取一次 ⇒ 这些提示只出现在**这一轮**的 system 里：
        # 整轮共用（system 每轮只组一次）、不进会话历史、下一轮不再出现。
        pending_inject=card_store.take_pending_inject_sync(work_id, current_id),
    )


def build_context_regions(work_id: str, current_id: str,
                          memory_state: dict[str, Any] | None = None,
                          *, ctx: context_regions.RegionCtx | None = None) -> list[dict[str, Any]]:
    """兼容入口：返回本卡 system 上下文区（不含使用说明）。

    实际渲染经统一 context registry；保留既有返回契约，供 draft/日志/测试消费。
    """
    ctx = ctx or build_ctx(work_id, current_id, memory_state)
    rows = context_regions.render_registered_regions(
        ctx, profile="main", deliveries={"system"}, source_owner="assembler")
    return [{
        "order": r["order"], "name": r["name"], "channel": r["channel"],
        "seats": r["seats"], "text": r["payload"], "entries": r["entries"],
    } for r in rows]


def build_context_bundle(work_id: str, current_id: str,
                         memory_state: dict[str, Any] | None = None,
                         history_source: list[dict[str, Any]] | None = None,
                         function_meta: dict[str, Any] | None = None,
                         history_extra: list[dict[str, Any]] | None = None,
                         current_pair_id: str = "",
                         with_viewpoints: bool = True,
                         scope_hint: dict[str, Any] | None = None,
                         history_id: str = "",
                         memory_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """一次建好主模型/三席上下文帧；system、history 均由同一注册表渲染。

    [2026-11-19 性能批次三] ``with_viewpoints=False``：跳过 viewpoint 剖面的
    整单渲染。生产主链路（message_runner.build_context）里那份回合开场的
    viewpoints 渲染从未作为模型输入——席位进场时 run_seats 会带 seat_identity
    **重新 compose** 一份真正的三席请求；开场渲染只被写进 prompt 日志当摆设，
    等于每回合白付一次全量分区渲染（直答回合＝绝大多数回合，零三席）。
    缺省 True ⇒ 既有调用方（测试/其他链路）行为逐字不变。

    ``scope_hint``（[批二·按任务缩量 2026-11-20]）：传给 build_ctx 做任务范围
    判定。缺省 None ⇒ 不判定 ⇒ 范围 unknown ⇒ 逐字同旧。
    """
    ctx = build_ctx(work_id, current_id, memory_state, history_source,
                    function_meta=function_meta, history_extra=history_extra,
                    current_pair_id=current_pair_id, scope_hint=scope_hint,
                    history_id=history_id, memory_snapshot=memory_snapshot)
    main = context_regions.compose_model_context(ctx, profile="main")
    viewpoints = (context_regions.compose_model_context(ctx, profile="viewpoint")
                  if with_viewpoints else None)
    return {
        "context": ctx,
        "main": main,
        "viewpoints": viewpoints,
        "regions": main["system_regions"],
    }


def build_neutral_context(work_id: str, current_id: str) -> str:
    """中性上下文（主模型与三思考席共用同一份）：卡片清单 ＋ 当前卡标识。

    [T11-T2] 本函数降为薄包装：分区渲染后按 order join，输出与改造前逐字
    一致（V1 断言含空 ctx 分支与 rstrip 两条路径）。
    [拉取裁定 2026-10-02] 不推送任何卡片正文——模型需要时经 read_card 调取。
    上一轮模型写入的卡片内容不会再自动进入任何后续输入（回流断根）。
    """
    regions = build_context_regions(work_id, current_id)
    return context_regions.assemble_regions([r["text"] for r in regions])
