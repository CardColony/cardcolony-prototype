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
  summary()           职能卡面=台账动态拼装(职责书/观点簿/警报簿/指令簿,
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
# 职能卡面:台账动态拼装(照 role_room.role_summary 先例;不用通用三块冒充)
# ---------------------------------------------------------------------------

def _fmt_ts(ts: str) -> str:
    return str(ts or "")[:16].replace("T", " ")


#: [卡面定制 2026-10-24] 台账 code → 面区块标题(function_ledgers 的展示名)。
#: 目录条目自带 ledgers([{code,title}]),这里只是**非目录职能**回不到目录时的
#: 兜底标题表;渲染一律以目录 face_blocks 为准。
LEDGER_TITLES = {
    "premises": "前提台账",
    "tech_requirements": "技术裁定簿",
    "criteria": "判据与验收台账",
    "health": "判据登记与体检",
    "decisions": "决策簿",
    "reports": "汇报簿",
    "divergence": "分歧待裁",
}


def _entry_face(work_id: str, function_id: str,
                prof: dict[str, Any]) -> tuple[dict[str, Any] | None,
                                               list[str],
                                               dict[str, str]]:
    """取该职能的目录条目 ⇒ (entry, face_blocks, title→ledger_code 映射)。

    非目录职能(work_025 存量)⇒ (None, 默认五节, {})——旧形态逐字保留。
    """
    code = catalog_code_of(prof)
    if not code:
        return None, [], {}
    e = entry(code) or {}
    blocks = [str(b) for b in (e.get("face_blocks") or [])]
    lmap = {str(l.get("title")): str(l.get("code"))
            for l in (e.get("ledgers") or []) if l.get("code")}
    return e, blocks, lmap


def _render_ledger_block(lines: list[str], title: str,
                         entries: list[dict[str, Any]], limit: int) -> None:
    lines.append(f"## {title}(最近 {limit} 条)")
    if entries:
        for it in entries:
            if it.get("superseded_by"):
                continue                     # 被取代条目不占面(台账里可回溯)
            body = str(it.get("text") or "").replace("\n", " ")[:200]
            lines.append(f"- [{_fmt_ts(it.get('ts', ''))}] {body}")
    else:
        lines.append("- （暂无记录)")
    lines.append("")


def summary(work_id: str, function_id: str,
            verdicts_limit: int = 8) -> dict[str, Any]:
    """职能的「卡面」:**按目录 face_blocks 装配**(卡面定制 2026-10-24)。

    - 目录职能:职责书/管辖/观点簿/警报簿/指令簿按 face_blocks 顺序出;
      专业台账(PM 前提台账/CTO 技术裁定簿/QA 判据验收/涌现守门体检/
      总裁 决策簿·汇报簿·分歧待裁)从 function_ledgers 现拼——每职能的面
      结构由目录定义,这正是「按职能量身定制」的落点。
    - 非目录职能(work_025 存量):默认五节,与旧形态逐字一致。
    只读、不落库——职能卡 cards 行的三块只是身份占位,真相在台账里。
    """
    prof = function_store.get_profile(work_id, function_id) or {}
    name = str(prof.get("function_name") or function_id)
    charter = str((prof.get("charter") or {}).get("text") or "").strip()
    jur = prof.get("jurisdiction") or {}
    watch = prof.get("watch") or {}
    scope = str(watch.get("scope") or jur.get("scope") or "")
    watch_cards = [str(c) for c in (watch.get("cards") or [])]

    verdicts: list[dict[str, Any]] = []
    alarms: list[dict[str, Any]] = []
    directives: list[dict[str, Any]] = []
    try:
        verdicts = function_store.list_verdicts(work_id, function_id,
                                                limit=verdicts_limit) or []
    except Exception:  # noqa: BLE001
        pass
    try:
        alarms = function_store.list_alarms(work_id, status="open",
                                            function_id=function_id) or []
    except Exception:  # noqa: BLE001
        pass
    try:
        directives = function_store.list_directives(work_id, function_id,
                                                    status="issued") or []
    except Exception:  # noqa: BLE001
        pass

    entry_e, face_blocks, lmap = _entry_face(work_id, function_id, prof)
    blocks = face_blocks or ["职责书", "管辖", "观点簿", "警报簿", "指令簿"]

    ledger_cache: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {"verdicts": len(verdicts),
                              "open_alarms": len(alarms),
                              "issued_directives": len(directives)}
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
        elif title == "观点簿":
            lines.append(f"## 观点簿(最近 {verdicts_limit} 条)")
            if verdicts:
                for v in verdicts:
                    dec = str(v.get("decision") or "")
                    reason = str(v.get("reason") or "").replace("\n", " ")[:160]
                    tg = "、".join(str(t) for t in (v.get("targets") or [])[:4])
                    lines.append(f"- [{_fmt_ts(v.get('ts', ''))}] {dec}"
                                 f"{' → ' + tg if tg else ''}:{reason}")
            else:
                lines.append("- （尚无观点记录)")
            lines.append("")
        elif title == "警报簿":
            lines.append("## 警报簿(open)")
            if alarms:
                for a in alarms:
                    lines.append(f"- [{a.get('severity')}] {a.get('title')}"
                                 f"({_fmt_ts(a.get('ts', ''))})")
            else:
                lines.append("- （无未决警报)")
            lines.append("")
        elif title == "指令簿":
            lines.append("## 指令簿(issued)")
            if directives:
                for d in directives:
                    lines.append(f"- → {d.get('target_card_id')}:"
                                 f"{str(d.get('instruction') or '')[:120]}"
                                 f"({_fmt_ts(d.get('created_at', ''))})")
            else:
                lines.append("- （无在途指令)")
            lines.append("")
        sections_out.append(title)

    for title in blocks:
        lcode = lmap.get(title)
        if lcode == "divergence":
            # 分歧待裁是**跨职能**的:任何职能写的分歧都聚到总裁面上
            # (按 work 聚合,带写入方名)。本职能名下的单查没意义。
            if "divergence" not in ledger_cache:
                try:
                    _rows = function_store.list_ledger_work(
                        work_id, "divergence", limit=10) or []
                except Exception:  # noqa: BLE001
                    _rows = []
                _names = {p["function_id"]: p.get("function_name") or p["function_id"]
                          for p in function_store.list_profiles(work_id)}
                for _r in _rows:
                    _r["text"] = (f"[{_names.get(_r['function_id'], _r['function_id'])}] "
                                  + str(_r.get("text") or ""))
                ledger_cache["divergence"] = _rows
                counts["ledger_divergence"] = len(_rows)
            _render_ledger_block(lines, title, ledger_cache["divergence"], 10)
            sections_out.append(title)
        elif lcode:
            if lcode not in ledger_cache:
                try:
                    ledger_cache[lcode] = function_store.list_ledger(
                        work_id, function_id, lcode, limit=10) or []
                except Exception:  # noqa: BLE001
                    ledger_cache[lcode] = []
                counts[f"ledger_{lcode}"] = len(ledger_cache[lcode])
            _render_ledger_block(lines, title, ledger_cache[lcode], 10)
            sections_out.append(title)
        else:
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
#   spawn()             观点回合:职能读事件批次 → record_verdict(观点簿),
#                       沉默也落账(机械补记);总裁的观点回合同法。
#   统合与统筹(父卡读观点簿摘要→统合文;总裁读统合文→决策)不设独立回合
#   ——它们发生在父卡/总裁的下一个自然回合:观点簿摘要以区块注入(P-B 后续批)。
#   本文件先落「事件→观点」这半条回路:它是「职能引导者」的机械前提——
#   没有事件驱动唤醒,职能永远是被戳才说话的应答机。
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
        _mr._spawn_tracked(_deliver())
    except Exception as exc:  # noqa: BLE001 提示失败不挡闸本身
        logger.warning("[auto] 激活提示投递失败 card=%s: %s", card_id, exc)
