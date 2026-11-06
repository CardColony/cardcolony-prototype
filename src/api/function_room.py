# -*- coding: utf-8 -*-
"""职能室(function_room):预设职能目录的加载、播种与职能卡面台账渲染。

[预设职能卡 2026-10-24 · 用户裁定] 职能从「根卡自由组建」改为「目录预设」:
总裁(根卡兼职能卡)/产品经理/技术总监/测试验收总监/涌现守门。
职责书/管辖/盯线/唤醒事件/工具面/权限/卡面区块**全部由目录定义**
(``config/function_catalog.json``,唯一事实源);本模块是目录在生产链路的唯一入口:

  load()              目录(fail-open:读不到 ⇒ disabled,行为与旧世界逐字不变)
  seed_work()         建 work 后播种(根卡按 ceo 条目职能化 + 其余职能建卡)
  function_meta_for() 145 区取数——职能/总裁回合的身份来源,补上装配层缺的那一针
                      (context_assembler.build_ctx 的 docstring 预告过
                       「由 function_room 回合执行器显式传进来」,本模块即它)
  summary()           职能卡面=身份+差事账拼装(职责书/管辖/在派任务/参与记录
                      ——[两源模型 2026-11-01] 台账块已退役,
                      照 role_room.role_summary 先例,不用通用三块冒充)

后续汇聚协议(观点→统合→统筹)同样落在本模块(见 docs/ 设计文档)。
目录里一切文本是**通用契约**,不含任何具体任务事实(用户令:不进 BASE_PROMPT,
不因任务定制而失去通用性)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from ..storage import function_store, work_store
from ..storage import session_ops

logger = logging.getLogger(__name__)


def _now() -> str:
    """ISO 时间戳(与会话/台账同形态;本模块多处留痕用)。"""
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).isoformat()

#: 目录路径的环境覆盖(测试指向临时文件;与 wb_exec 的 WB_EXEC_CONFIG_PATH 同法)。
ENV_CATALOG_PATH = "FUNCTION_CATALOG_PATH"

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "function_catalog.json"

_cache: dict[str, Any] = {"at": None, "data": None}


def reset_cache() -> None:
    """清目录缓存(测试用;改目录文件后也调它)。"""
    _cache["at"] = None
    _cache["data"] = None


def _catalog_path() -> Path:
    env = os.environ.get(ENV_CATALOG_PATH, "").strip()
    return Path(env) if env else _DEFAULT_PATH


def load(force: bool = False) -> dict[str, Any]:
    """读目录(带 mtime 缓存)。**fail-open**:读不到/解析失败 ⇒ ``{"enabled": False}``,
    调用方一律按「没有目录」处理——播种不动、身份不注,与旧世界逐字一致。"""
    p = _catalog_path()
    try:
        at = (p.stat().st_mtime_ns, str(p))
    except OSError:
        reset_cache()
        return {"enabled": False, "functions": []}
    if not force and _cache["at"] == at and _cache["data"] is not None:
        return _cache["data"]
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 坏目录 ⇒ 当没有
        logger.warning("[function_room] 目录读取失败(%s),按未启用处理: %s", p, exc)
        reset_cache()
        return {"enabled": False, "functions": []}

    entries = [e for e in (raw.get("functions") or [])
               if isinstance(e, dict) and str(e.get("code") or "").strip()]
    meta = raw.get("_meta") or {}
    # 开关既可写顶层也可写 _meta 内(仓库目录放在 _meta 里,与文档/版本同区)
    def _flag(key: str) -> bool:
        return bool(raw.get(key, meta.get(key)))
    data = {
        "enabled": _flag("seed_on_create") and bool(entries),
        "free_form_enabled": _flag("free_form_enabled"),
        "lateral_enabled": _flag("lateral_enabled"),
        "functions": entries,
        "version": meta.get("version"),
        # [venue 默认席位 2026-10-31] 场默认建议名单按**场型**挂在 _meta 下
        # （requirements→[ceo,pm,cto]；将来 acceptance→[qa,ceo]）。
        "venue_defaults": meta.get("venue_defaults") or {},
        # [差事装配 2026-10-31] 场授面（venue_kind→能力 code 清单）与
        # 差事文模板（venue_kind→mandate 模板，占位符 {xx}）。
        "venue_grants": meta.get("venue_grants") or {},
        "mandate_templates": meta.get("mandate_templates") or {},
    }
    _cache["at"], _cache["data"] = at, data
    return data


def entries() -> list[dict[str, Any]]:
    return list(load().get("functions") or [])


def entry(code: str) -> Optional[dict[str, Any]]:
    code = str(code or "").strip()
    for e in entries():
        if str(e.get("code")) == code:
            return e
    return None


def ceo_entry() -> Optional[dict[str, Any]]:
    return entry("ceo")


def catalog_code_of(profile: dict[str, Any]) -> str:
    """从档案反查它来自目录的哪个条目(播种时写进 jurisdiction.catalog_code)。"""
    jur = profile.get("jurisdiction") or {}
    return str(jur.get("catalog_code") or "")


def profile_from_entry(e: dict[str, Any]) -> dict[str, Any]:
    """目录条目 → function_store 档案形状(jurisdiction 里带 catalog_code,
    播种幂等与后续按条目路由都靠它)。"""
    charter_text = str(e.get("charter") or "").strip()
    return {
        "function_name": str(e.get("name") or e.get("code") or ""),
        "jurisdiction": {
            "catalog_code": str(e.get("code") or ""),
            "domain": str((e.get("jurisdiction") or {}).get("domain") or ""),
            "statement": str((e.get("jurisdiction") or {}).get("statement") or ""),
            "scope": str((e.get("jurisdiction") or {}).get("scope")
                         or (e.get("watch") or {}).get("scope") or ""),
        },
        "watch": {
            "scope": str((e.get("watch") or {}).get("scope") or "all"),
            "cards": list((e.get("watch") or {}).get("cards") or []),
            "signals": list((e.get("watch") or {}).get("signals") or []),
        },
        "speak_contract": dict(e.get("speak_contract") or {}),
        "escalation": {"advice_to": "ceo", "alarm_to": "human"},
        "charter": {"version": 1, "text": charter_text,
                    "ratified_by": "目录预设", "ratified_at": ""},
    }


async def seed_work(work_id: str) -> dict[str, Any]:
    """按目录播种一个 work:**幂等**(按 jurisdiction.catalog_code 去重)。

    - ceo 条目 ``lands_on=root``:给根卡补一份档案(function_id=根卡 id,
      不占 fn 号段)——根卡由此「职能化」:145 区身份、93 区阁员行、后续总裁工具面。
    - 其余条目建独立职能卡(function_store.create_function 发 fn_NNN +
      ensure_function_card 建 cards 行)。
    - 目录未启用/读不到 ⇒ no-op(fail-open);单个职能失败不挡其余(逐项 try)。
    """
    cat = load()
    out: dict[str, Any] = {"work_id": work_id, "seeded": [], "skipped": [],
                           "failed": [], "enabled": cat.get("enabled")}
    if not cat.get("enabled"):
        out["reason"] = "catalog_disabled"
        return out
    root_id = ""
    try:
        root_id = work_store.work_card_id_of(work_id) or ""
    except Exception as exc:  # noqa: BLE001 读不到根卡 ⇒ 不播种(fail-open)
        out["reason"] = f"no_root_card: {exc}"
        return out

    have = set()
    try:
        for prof in function_store.list_profiles(work_id):
            have.add(catalog_code_of(prof))
    except Exception:  # noqa: BLE001 列不出 ⇒ 视为空(逐项 INSERT OR IGNORE 仍幂等)
        have = set()

    for e in cat.get("functions") or []:
        code = str(e.get("code") or "")
        if not code or code in have:
            out["skipped"].append(code)
            continue
        try:
            if str(e.get("lands_on") or "card") == "root":
                if not root_id:
                    out["failed"].append({"code": code, "error": "no_root_card"})
                    continue
                await function_store.ensure_profile(
                    work_id, root_id, profile_from_entry(e))
                out["seeded"].append({"code": code, "function_id": root_id})
                continue
            # 延迟 import:card_tools 体量大,且反过来要引用本模块(read 卡分叉)
            from . import card_tools
            prof = await function_store.create_function(
                work_id, str(e.get("name") or code), profile_from_entry(e))
            fid = str(prof.get("function_id") or "")
            if not fid:
                out["failed"].append({"code": code, "error": "no_function_id"})
                continue
            await card_tools.ensure_function_card(work_id, fid,
                                                  str(e.get("name") or code))
            out["seeded"].append({"code": code, "function_id": fid})
        except Exception as exc:  # noqa: BLE001 单个失败不挡其余
            logger.warning("[function_room] 播种失败 work=%s code=%s: %s",
                           work_id, code, exc)
            out["failed"].append({"code": code, "error": str(exc)})
    return out


def function_meta_for(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """145 区取数:这张卡有没有职能档案(独立职能卡 fn_NNN,或职能化的根卡=总裁)。

    fail-open:查不到 ⇒ None(145 区按「没有来源就不出现」自然降级)。
    """
    try:
        return function_store.get_profile(work_id, str(card_id or ""))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# [差事装配 2026-10-31] act_as 三参数的供给侧：人格（act_meta）、授面
#（toolface）、差事文（mandate）、台账 schema。「何时叫」由调用方/harness
# 决定——这边只管「叫来的是谁、穿什么职责书、发什么钥匙、记什么账」。
# ---------------------------------------------------------------------------

def venue_defaults() -> dict[str, list[str]]:
    """场型 → 默认席位目录 code 集（_meta.venue_defaults）。"""
    return {k: [str(x) for x in (v or [])]
            for k, v in (load().get("venue_defaults") or {}).items()}


def venue_grants() -> dict[str, list[str]]:
    """场型 → 默认授面（能力 code 清单，_meta.venue_grants）。
    条目级 venue_grants[场型] 可覆盖——catalog 条目优先。"""
    return {k: [str(x) for x in (v or [])]
            for k, v in (load().get("venue_grants") or {}).items()}


def mandate_templates() -> dict[str, str]:
    """场型 → mandate 模板（_meta.mandate_templates，占位符 {xx}）。"""
    return {str(k): str(v)
            for k, v in (load().get("mandate_templates") or {}).items()}


def render_mandate(venue_kind: str, **fields: Any) -> str:
    """占位符展开。没有模板 ⇒ 空串（调用方自己写 mandate）；
    占位符缺值 ⇒ 原样保留（不误炸）。"""
    tpl = str(mandate_templates().get(str(venue_kind or "")) or "")
    if not tpl:
        return ""

    class _Safe(dict):
        def __missing__(self, k: Any) -> str:
            return "{" + str(k) + "}"

    try:
        return tpl.format_map(_Safe({k: str(v) for k, v in fields.items()}))
    except Exception:  # noqa: BLE001 模板坏了给原文，总好过半成品
        return tpl


def _profile_for(work_id: str, fn_or_code: str) -> tuple[dict[str, Any], str]:
    """fn_NNN 或目录 code 都收 ⇒ (profile, function_id)。查不到 ⇒ ({}, '')。"""
    ident = str(fn_or_code or "").strip()
    if not ident:
        return {}, ""
    prof = function_store.get_profile(work_id, ident)
    if prof:
        return prof, str(prof.get("function_id") or ident)
    try:
        for p in function_store.list_profiles(work_id):
            if catalog_code_of(p) == ident:
                return p, str(p.get("function_id") or "")
    except Exception:  # noqa: BLE001
        pass
    return {}, ""


def venue_charter_for(code: str, venue_kind: str) -> str:
    """某职能在某种场穿的职责书：**目录条目 charters[场型]** ⇒
    （评审场特判）config/review_charters.json 按目录 code ⇒ ''。
    日常职责书不在这回——回落在 identity_for 里做。"""
    e = entry(code) or {}
    ch = e.get("charters") or {}
    txt = str(ch.get(str(venue_kind or "")) or "").strip()
    if txt or str(venue_kind or "") != "review":
        return txt
    try:
        path = _catalog_path().parent / "review_charters.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        node = data.get(str(code or "")) or {}
        return str((node.get("text") if isinstance(node, dict) else node)
                   or "").strip()
    except Exception:  # noqa: BLE001 查不到 ⇒ ''，交给 identity_for 回落
        return ""


def identity_for(work_id: str, fn_or_code: str,
                 venue_kind: str = "") -> dict[str, Any]:
    """act_as/act_meta 的供给侧：这个人格进场穿什么。

    职责书解析链：目录条目 charters[场型] →（评审场）review_charters.json
    按目录 code → 档案日常职责书。模型不在 act_meta 里——RunState 按
    act_as 卡自己的 profile 取（与评审席位同构）。
    输出形状与评审线 act_meta 契约对齐：{function_id, function_name,
    catalog_code, charter:{text}}，另带 kind/venue_kind 元信息。
    ``resolved``：档案真查到了没有——function_id 兜底回显输入串，判
    「解没解出来」看这个位，或再调 ``function_exists``（幽灵职能坑）。
    """
    prof, fid = _profile_for(work_id, fn_or_code)
    code = catalog_code_of(prof) or str(fn_or_code or "")
    e = entry(code) or {}
    name = str(prof.get("function_name") or e.get("name") or code)
    duty = venue_charter_for(code, venue_kind)
    if not duty:
        ch = prof.get("charter") or {}
        duty = str(ch.get("text") if isinstance(ch, dict) else ch or "")
    return {"function_id": fid or str(fn_or_code or ""),
            "function_name": name, "catalog_code": code,
            "kind": str(e.get("kind") or ""),
            "venue_kind": str(venue_kind or ""),
            # 档案查没查到：function_id 永远非空（回显输入串），判「解没
            # 解出来」必须看这一位——幽灵职能会"解析成功"。
            "resolved": bool(prof),
            "charter": {"text": duty}}


def _mounted_abilities(work_id: str, function_id: str,
                       prof: dict[str, Any]) -> set[str]:
    """这张职能实挂的能力集：档案 abilities 列优先；空 ⇒ 按卡面元数据走
    ``card_tools._abilities_of`` 同一条回退路（卡型/模式现算，与建卡同源）。"""
    ab = [str(a) for a in (prof.get("abilities") or [])]
    base: set[str] = set()
    if ab:
        base = set(ab)
    else:
        try:
            from ..storage import card_store
            from . import card_tools
            meta = card_store._meta_of(work_id, function_id) or {}
            got = card_tools._abilities_of(
                meta, card_store.iter_card_metas(work_id))
            base = set(got or set())
        except Exception:  # noqa: BLE001
            base = set()
    # [执笔挂载 2026-11-01] 目录条目 ``extra_abilities``：标准回退路给不出的
    # 岗位能力（PM 的 edit_other_face——差事作用域替它把住边界），
    # 以目录为准额外挂载；档案实挂 abilities 时同样叠加。
    try:
        e = entry(catalog_code_of(prof)) or {}
        base |= {str(a) for a in (e.get("extra_abilities") or []) if str(a)}
    except Exception:  # noqa: BLE001 目录坏了不额外挂
        pass
    return base


def grant_for(work_id: str, fn_or_code: str,
              venue_kind: str = "") -> list[str]:
    """本场该发哪些工具（函数侧上限）＝挂载 abilities ∩ 场授面 → 展开工具名。

    场授面查法：条目 ``venue_grants[场型]`` → 目录 ``_meta.venue_grants[场型]``。
    两方都没配 ⇒ 空授权（宁缺勿滥——场不授就不发）。
    """
    prof, fid = _profile_for(work_id, fn_or_code)
    code = catalog_code_of(prof)
    e = entry(code) or {}
    grants = list((e.get("venue_grants") or {}).get(str(venue_kind or "")) or []) \
        or list(venue_grants().get(str(venue_kind or "")) or [])
    mounted = _mounted_abilities(work_id, fid or str(fn_or_code or ""), prof)
    eff = [a for a in mounted if a in set(grants)]
    try:
        from ..infra import capability_registry as _cap
        return sorted(_cap.tools_of(eff))
    except Exception:  # noqa: BLE001 清单坏 ⇒ 退回能力 code 本身
        return sorted(set(eff))


# ---------------------------------------------------------------------------
# 职能卡面:按目录 face_blocks 拼装(照 role_room.role_summary 先例;
# 不用通用三块冒充)。
# ---------------------------------------------------------------------------

def _fmt_ts(ts: str) -> str:
    return str(ts or "").replace("T", " ")


def _entry_face(work_id: str, function_id: str,
                prof: dict[str, Any]) -> tuple[dict[str, Any] | None,
                                               list[str]]:
    """取该职能的目录条目 ⇒ (entry, face_blocks)。

    非目录职能(work_025 存量)⇒ (None, 默认三节)——旧形态逐字保留。
    """
    code = catalog_code_of(prof)
    if not code:
        return None, []
    e = entry(code) or {}
    blocks = [str(b) for b in (e.get("face_blocks") or [])]
    return e, blocks


def summary(work_id: str, function_id: str) -> dict[str, Any]:
    """职能的「卡面」:**按目录 face_blocks 装配**(卡面定制 2026-10-24)。

    - 目录职能:职责书/管辖/指令簿/在派任务/参与记录按 face_blocks 顺序出;
      [两源模型 2026-11-01 · 用户令「卡面是唯一权威事实源，不需要台账」]
      台账区块整体退役——专业准则写在 rules.md（人改的基建文件），
      产出落在场卡面上，面不再渲染任何台账内容。
    - 非目录职能(work_025 存量):默认三节(职责书/管辖/指令簿)。
    - [2026-10-31] 观点簿/警报簿已随 record_verdict / raise_alarm 退役。
    只读、不落库——职能卡 cards 行的三块只是身份占位。
    """
    prof = function_store.get_profile(work_id, function_id) or {}
    name = str(prof.get("function_name") or function_id)
    charter = str((prof.get("charter") or {}).get("text") or "").strip()
    jur = prof.get("jurisdiction") or {}
    watch = prof.get("watch") or {}
    scope = str(watch.get("scope") or jur.get("scope") or "")
    watch_cards = [str(c) for c in (watch.get("cards") or [])]

    # [2026-10-31 用户令「判断、警报我明确不需要，拆掉」] 观点簿/警报簿随
    # record_verdict / raise_alarm 一起退役——这两本账不再取数、不再渲染。
    directives: list[dict[str, Any]] = []
    try:
        directives = function_store.list_directives(work_id, function_id,
                                                    status="issued") or []
    except Exception:  # noqa: BLE001
        pass

    # [差事面 2026-10-31] 「在派任务」「参与记录」两块从 deployments 账本
    # 现拼——它此刻在哪、去过哪，脸上一眼看完（face_blocks 里有才渲）。
    deploy_active: list[dict[str, Any]] = []
    deploy_hist: list[dict[str, Any]] = []
    try:
        deploy_active = function_store.active_for_function(
            work_id, function_id) or []
        deploy_hist = function_store.deployment_history(
            work_id, function_id, limit=10) or []
    except Exception:  # noqa: BLE001
        pass

    entry_e, face_blocks = _entry_face(work_id, function_id, prof)
    blocks = face_blocks or ["职责书", "管辖", "指令簿"]

    counts: dict[str, int] = {"issued_directives": len(directives)}
    lines: list[str] = []
    sections_out: list[str] = []

    def _std_block(title: str) -> None:
        nonlocal lines
        if title == "职责书":
            lines += ["## 职责书", charter or "（职责书待补）", ""]
        elif title == "管辖":
            lines += ["## 管辖",
                      f"- 域:{jur.get('domain') or '—'}"
                      f"｜范围:{scope or '—'}"
                      f"｜盯:{'、'.join(watch_cards) if watch_cards else '（按范围）'}",
                      ""]
        elif title == "指令簿":
            lines.append("## 指令簿(issued)")
            if directives:
                for d in directives:
                    lines.append(f"- → {d.get('target_card_id')}:"
                                 f"{str(d.get('instruction') or '')}"
                                 f"({_fmt_ts(d.get('created_at', ''))})")
            else:
                lines.append("- （无在途指令)")
            lines.append("")
        elif title == "在派任务":
            lines.append("## 在派任务")
            if deploy_active:
                for d in deploy_active:
                    lines.append(
                        f"- [{d.get('venue_kind')}] "
                        f"{d.get('venue_label') or d.get('venue_ref')}"
                        f"｜{d.get('delegator') or '?'} 派"
                        f"｜退出:{d.get('exit_kind')}"
                        f"｜{str(d.get('mandate') or '')}")
            else:
                lines.append("- （没有在派差事）")
            lines.append("")
        elif title == "参与记录":
            lines.append("## 参与记录(最近 10)")
            if deploy_hist:
                for d in deploy_hist:
                    lines.append(
                        f"- [{d.get('effective_status') or d.get('status')}] "
                        f"{d.get('venue_label') or d.get('venue_ref')}"
                        f"｜{str(d.get('result') or '—')}"
                        f"｜{_fmt_ts(d.get('closed_at') or d.get('updated_at'))}")
            else:
                lines.append("- （暂无参与记录)")
            lines.append("")
        sections_out.append(title)

    for title in blocks:
        _std_block(title)

    return {
        "function_id": function_id,
        "function_name": name,
        "catalog_code": catalog_code_of(prof),
        "duty": prof.get("duty") or {},
        "sections": sections_out,
        "text": "\n".join(lines),
        "counts": counts,
    }


# ===========================================================================
# 汇聚协议(P2 · 2026-10-24):观点 → 统合 → 统筹
#
# [用户裁定] 「当有汇报信息的时候,所有职能都发表观点,由父卡进行统合整理,
#  再发回给总裁,总裁根据总结,对所有现状进行统筹式管理(包括卡面编辑、
#  下达指令),然后向用户做汇报(中间汇报留痕)。」
#
# 机械形态(role_room 的 sync+spawn 同构模板):
#   notify_event()      事件源:任何卡发生「应到的」事件(汇报到达/面变/收口申请)
#                       → 全部目录职能(总裁必达)进 function_sync 队列;
#   spawn()             观点回合:职能读事件批次 → 落观点(观点簿),
#                       沉默也落账(机械补记);总裁的观点回合同法。
#   统合与统筹(父卡读观点摘要→统合文;总裁读统合文→决策)不设独立回合
#   ——它们发生在父卡/总裁的下一个自然回合:观点摘要以区块注入(P-B 后续批)。
#   本文件先落「事件→观点」这半条回路:它是「职能引导者」的机械前提——
#   没有事件驱动唤醒,职能永远是被戳才说话的应答机。
#
# [2026-10-31 现状] 上面这条回路**从未实现**(notify_event / spawn 两个函数
# 不存在),且观点簿已随 record_verdict 退役——「事件→观点」这半条不再有
# 落点。此段仅作设计留档,不要据此以为职能有自动唤醒。
# ===========================================================================

def requirements_approved(work_id: str, card_id: str) -> bool:
    """本卡的需求文档是否是**正稿**（requirements_state == "approved"）。

    [需求文档生命周期 2026-10-28 · 用户令] 改前判据是「总裁决策簿里有本卡条目」
    （`requirements_curated`）——那是「总裁策展过」的代理指标。用户定下流程后，
    真正的判据是**有没有正稿**：
        使命 → 写草稿 → 评审 → 通过才成**正稿** → 才能派工/点火
    所以子卡自己写草稿、评审通过，同样可以点火；反之总裁在决策簿写过条目、
    但需求文档没有正稿，也不能点火（否则等于按不存在的需求开工）。

    读不到 ⇒ 放行（判不出时不误杀；宁可放过，由卡自己汇报卡在哪）。
    """
    try:
        from ..storage import card_store  # noqa: PLC0415
        card = card_store._meta_of(work_id, card_id) or {}
        return str(card.get("requirements_state") or "") == "approved"
    except Exception:  # noqa: BLE001
        return True


def _notify_activation_blocked(work_id: str, card_id: str,
                               meta: dict[str, Any]) -> None:
    """激活被拒 ⇒ 给上级卡投一条提示（落会话块，**不唤醒**）。

    [通信收拢 2026-10-28] 改前走 `card_db.append_inbox`（收件箱 + 水位），
    而收件箱已取消（消息落会话块才是唯一可见面）。本函数在 sync 上下文里
    （`auto_driver.kick` 不是 async），故用 `_spawn_tracked` 起一个后台协程。
    """
    parent = str(meta.get("parent") or "")
    if not parent:
        return

    async def _deliver() -> None:
        from ..storage import session_facade
        title = str(meta.get("title") or card_id)
        await session_facade.append_card_message(
            work_id, parent, from_card=card_id,
            text=f"【激活被拒】子卡 {card_id}《{title}》没有需求文档"
                 f"(总裁未策展),无法激活开工。请总裁用 curate_face 为它"
                 f"策展需求文档,策展后它会自动开工。",
            msg_type="msg_notify")

    try:
        from . import message_runner as _mr
        _mr._spawn_tracked(_deliver(), work_id=work_id, card_id=parent)
    except Exception as exc:  # noqa: BLE001 提示失败不挡闸本身
        logger.warning("[auto] 激活提示投递失败 card=%s: %s", card_id, exc)
