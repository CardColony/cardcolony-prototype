# -*- coding: utf-8 -*-
"""记忆展开（照抄 dsh `gen3/tools/retrieval/expand-tool.ts` 的核心语义）。

## 它是什么

**"可追溯"的用户侧入口**：模型觉得某条结论太粗，指一个锚点（`seq`），
系统把**更深一层**的东西给它——描述层正文 → 逐字原文。

## dsh 的四条分支（逐条照抄）

| 命中 | 行为 |
|---|---|
| **已沉底（sunk）** | 返回**占位说明** ＋ **`coveredSeqs` 直达原文** ＋ **单条复活**（`sunk=false`，父链 `lastUsedAt` 刷新） |
| **降级（degraded）** | 读原文 → **一次再描述**（全系统唯一 LLM 例外）→ 原位替换＋清 degraded；配置缺省时**降级为原样返回** |
| 正常 | 先给 gist，再给 archive 的 `process`（描述层），再给原文（按 `coveredSeqs`） |
| 锚点找不到 | **明确回"未找到"**（"没查到"本身即结论，不得静默空） |

## 覆盖与分页（2026-11-20 记忆找回方案 §二）

- **定范围**：新节点按 `coveredSeqs` 去重升序取（空洞保持空洞——min/max
  区间读曾把覆盖集中间几十条无关块一并回灌）；旧条目才回退
  `seq_from/seq_to` 闭区间或单锚点兜底；`coverage` 区分
  exact/legacy_range/anchor/unknown（合成锚点缺覆盖 ⇒ unknown）。
- **定页**：逐字原文按 `page`（0 起）+ `page_chars`（缺省 6000）分页，
  每页 ≤6000 字且序列化回执 ≤32KiB（不够就缩页，下页从实际断点续）；
  `section="details"` 分页看完整结论/描述/子条目；
  `has_more`/`next_page`/`revision`（版本随内容、顺序、范围变）；
  负页码由调用方判 validation，页尽返回空页。
- **保真**：`text` 只放本页内容、原文片段页内只一份、逐块纯拼接零加工；
  `fragments` 逐段附块号/角色/时间/隔离标记与原文·text 双半开区间。

## 本项目不做的一件

**degraded 的"再描述"不在这里调模型**：本项目把它做成**返回值里的标记**（`needs_redescribe=True`），
由调用方决定要不要调——理由：本模块保持纯（可测、无副作用），LLM 调用归上层。
（dsh 把它塞在工具里；差异如实记在此。）
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from . import memory_domain as md, memory_lifecycle as lc, origin_store

#: 逐字原文单页字符上限（方案 §二.2：6,000 字/页，元数据另计 32KiB 回执顶）
PAGE_CHARS = 6000
#: 缺块清单一页条数（清单也分页）
MISSING_PAGE_ITEMS = 50


def _anchor_seq(node: dict[str, Any]) -> Optional[int]:
    """条目锚点：`anchor.seq` 优先，旧格式顶层 `seq` 兜底（0 是合法块号）。
    与 `read_memory` 索引同一条规则——索引列出的，这里必须找得到。"""
    a = node.get("anchor")
    if isinstance(a, dict):
        v = a.get("seq")
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    v = node.get("seq")
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _find(chain: list[dict[str, Any]], seq: int, role: str) -> Optional[dict[str, Any]]:
    for n in chain or []:
        if _anchor_seq(n) == int(seq) \
                and str(n.get("role") or "gist") == role:
            return n
    return None


def _coverage_seqs(node: dict[str, Any], seq: int) -> tuple[list[int], str]:
    """覆盖集解析：新节点 coveredSeqs（去重升序）→ 旧 seq_from/seq_to 闭区间
    → 单锚点兜底（anchor.seq 或旧顶层 seq）→ unknown（合成锚点缺覆盖）。"""
    covered = node.get("coveredSeqs")
    if isinstance(covered, list) and covered:
        try:
            return sorted({int(x) for x in covered}), "exact"
        except (TypeError, ValueError):
            pass
    sf, st = node.get("seq_from"), node.get("seq_to")
    if (isinstance(sf, int) and not isinstance(sf, bool)
            and isinstance(st, int) and not isinstance(st, bool)
            and 0 <= sf <= st):
        return list(range(sf, st + 1)), "legacy_range"
    a = _anchor_seq(node)
    if a is not None:
        return [a], "anchor"
    return [], "unknown"


def _fetch_covered(work_id: str, card_id: str, seqs: list[int],
                   coverage: str, *, conn: Any = None) -> tuple[list[dict[str, Any]], list[int]]:
    """按覆盖集取原文块（空洞保持空洞；返回 (命中块, 缺块清单)）。

    exact/anchor ⇒ **按块号** IN 查询（card_017/2913：5 块覆盖不再捞 41 块）；
    legacy_range ⇒ 区间读取（覆盖本来就是闭区间）。
    """
    if not seqs:
        return [], []
    if coverage == "legacy_range":
        if conn is None:
            got = origin_store.read_origin_span(
                work_id, card_id, seqs[0], seqs[-1])["events"]
        else:
            got = origin_store.read_origin_span_tx(conn, card_id, seqs[0], seqs[-1])
    elif conn is not None:
        got = origin_store.read_origin_seqs_tx(conn, card_id, seqs)
    else:
        got = origin_store.read_origin_seqs(work_id, card_id, seqs)
    have = {int(e["seq"]) for e in got}
    return got, [s for s in seqs if s not in have]


def _sender_of(ev: dict[str, Any]) -> str:
    """片段发信者（角色/发信卡——data.from 是卡间消息的发信卡）。"""
    d = ev.get("data") or {}
    for k in ("from", "sender", "from_card", "card_id", "source_card"):
        v = str(d.get(k) or "")
        if v:
            return v
    return str(ev.get("type") or "")


def _verbatim_corpus(events: list[dict[str, Any]], *,
                     work_id: str = "", card_id: str = "",
                     sanitized: bool = False, conn: Any = None) -> tuple[
                         str, list[dict[str, Any]], list[tuple[int, str]]]:
    """原文块逐字拼接（零加工零分隔），逐段出溯源区间。

    返回 (全文, fragments, parts)——fragment.src_range/text_range 均为半开区间；
    src_range 相对本块原文，text_range 相对拼接全文；parts=[(seq, 正文)] 供
    版本取哈希（块边界与**实际返回的来源字段**都是版本内容的一部分）。

    ``sanitized=True``（归拢回灌口）——与凝练结算**完全同一筛选**
    （`_read_origin`：`review_scope.drop_stale_review(keep=0)` ＋
    `predicates.enters_context`）：
    - 隔离块投 `ctx_content` 净化视图；
    - 工具结果/调用壳、评审消息、留痕块一律**略去正文**，换一行宿主状态
      （保块号/事件型/发信者/结构化 status——失败如实写出）；
    显式 memory_expand 工具仍发逐字原文。
    """
    from . import predicates, review_scope
    # 评审过滤（凝练口径 keep=0：评审流量一条不留；评审型卡全流蒸发）——
    # 代理块携 data 全字段，actor/behavior/source 判据原样生效。
    proxies = [dict(e.get("data") or {}) for e in events]
    for p, e in zip(proxies, events):
        p["seq"], p["content"] = e["seq"], e.get("content") or ""
    kept = review_scope.drop_stale_review(work_id, card_id, proxies, keep=0, conn=conn) \
        if sanitized else proxies
    kept_ids = {id(p) for p in kept}

    parts: list[tuple[int, str]] = []
    texts: list[str] = []
    frags: list[dict[str, Any]] = []
    pos = 0
    for ev, proxy in zip(events, proxies):
        d = ev.get("data") or {}
        frag: dict[str, Any] = {
            "seq": int(ev["seq"]),
            "type": str(ev.get("type") or ""),
            "sender": _sender_of(ev),
            "time": ev.get("time"),
            "quarantine": str(d.get("quarantine") or ""),
        }
        if sanitized and (id(proxy) not in kept_ids
                          or not predicates.enters_context(proxy)):
            # 不进凝练视野的块（工具结果/调用壳/评审/留痕/空 ctx 隔离块）：
            # 正文略去，留宿主状态行——结构化 status 如实带出（失败可查）。
            _st = str(d.get("status") or "")
            c = (f"〔系统〕[块{int(ev['seq'])} "
                 f"{str(ev.get('type') or '?')}"
                 f"{(' from ' + frag['sender']) if frag['sender'] else ''}"
                 f"{(' status=' + _st) if _st else ''}]"
                 " 正文按凝练口径略去（档案可查）。")
            frag["omitted"] = True
            frag["src_range"] = [0, 0]
        elif sanitized and d.get("quarantine"):
            c = str(d.get("ctx_content") or "")
            frag["src_range"] = [0, len(c)]
        else:
            c = str(ev.get("content") or "")
            frag["src_range"] = [0, len(c)]
        frag["text_range"] = [pos, pos + len(c)]
        frags.append(frag)
        # 版本取回执实际使用的来源字段——正文、块边界、型/发信者/时间/
        # 隔离态任一改写都必须换版（发信卡 card_017→card_049 这种同正文
        # 来源改写不能用旧 revision 串新页）。
        parts.append((int(ev["seq"]),
                      f"{frag['type']}|{frag['sender']}|{ev.get('time')}"
                      f"|{frag['quarantine']}|{c}"))
        texts.append(c)
        pos += len(c)
    return "".join(texts), frags, parts


def _page_slice(corpus: str, frags: list[dict[str, Any]], offset: int,
                page_chars: int) -> dict[str, Any]:
    """切一页逐字内容。fragment 落在页内的部分给相对本页的双半开区间
    （src_range 仍指原文块坐标）。

    page 语义＝**字符偏移**（0 起）：页长 ≤page_chars，32KiB 截短页时
    下页从实际断点续读——窗口式 page*N 会在截短页后跳字。
    """
    total = len(corpus)
    start = max(0, int(offset))
    if start >= total:
        return {"text": "", "fragments": [], "has_more": False,
                "offset": start, "chars": 0}
    end = min(start + int(page_chars), total)
    page_frags = []
    for f in frags:
        fs, fe = f["text_range"]
        s, e = max(fs, start), min(fe, end)
        if s >= e:
            continue
        page_frags.append({
            **f,
            "src_range": [f["src_range"][0] + (s - fs),
                          f["src_range"][0] + (e - fs)],
            "text_range": [s - start, e - start],
        })
    return {"text": corpus[start:end], "fragments": page_frags,
            "has_more": end < total, "offset": start, "chars": end - start}


def _details_corpus(node: dict[str, Any], process: str,
                    children: list[dict[str, Any]]) -> tuple[
                        str, list[tuple[int, str]]]:
    """details 区语料：完整结论＋描述层＋子条目（各段标题分隔）。

    返回 (全文, parts)——parts=[(seq, 段文)] 供版本取哈希（跨段挪动
    一个字符也要换版）。
    """
    parts: list[tuple[int, str]] = [
        (int((node.get("anchor") or {}).get("seq") or -1),
         f"结论：{str(node.get('gist') or '')}")]
    if process:
        parts.append((-1, f"描述：{process}"))
    for c in children:
        line = f"子条目 [seq {c.get('seq')}] {c.get('gist') or ''}".rstrip()
        if c.get("process"):
            line += f"\n{c['process']}"
        parts.append((int(c.get("seq") or -1), line))
    return "\n\n".join(p for _, p in parts), parts


def _revision(coverage: str, seqs: list[int], section: str,
              parts: list[tuple[int, str]], missing: list[int]) -> str:
    """内容版本：随影响分页的**内容、顺序、范围和块边界**变化（账目刷新不变）。

    逐段取哈希而非整文——同长同拼接、跨块挪一个字符 ⇒ 片段坐标变了，
    版本必须变（旧 revision 串新旧两页）。
    """
    h = hashlib.sha256(
        json.dumps({"cov": coverage, "seqs": seqs, "sec": section,
                    "miss": missing}, ensure_ascii=False).encode("utf-8"))
    for seq, part in parts:
        h.update(json.dumps([seq, part], ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()[:16]


def expand(work_id: str, card_id: str, seq: int, role: str = "gist",
           *, page: int = 0, section: str = "verbatim",
           revision: Optional[str] = None,
           page_chars: Optional[int] = None,
           missing_page: int = 0,
           sanitized: bool = False,
           revive: bool = True,
           now_ms: Optional[int] = None,
           conn: Any = None) -> dict[str, Any]:
    """展开一个锚点。返回结构化结果（**不抛**）。

    返回字段：
    - `found`/`reason`；`sunk`/`degraded`/`needs_redescribe`/`revived`；
    - `gist`、`process`、`children`（结构化照旧）；
    - `coverage`（exact/legacy_range/anchor/unknown）＋ `missing`（缺块，分页）；
    - 分页：`page`/`has_more`/`next_page`/`revision`/`text`/`fragments`；
    - `section`：verbatim（默认逐字原文）/ details（结论＋描述＋子条目）。
    """
    page = int(page or 0)
    section = str(section or "verbatim")
    page_chars = int(page_chars or PAGE_CHARS)

    tx_kw = {"conn": conn} if conn is not None else {}
    chain = md.load_chain(work_id, card_id, **tx_kw)
    node = _find(chain, seq, role)
    if node is None:
        return {"found": False, "reason": f"链上没有 anchor.seq={seq} role={role} 的条目",
                "seq": int(seq), "role": role, "coverage": "unknown"}

    result: dict[str, Any] = {
        "found": True, "seq": int(seq), "role": role,
        "gist": str(node.get("gist") or ""),
        "sunk": bool(node.get("sunk")),
        "degraded": bool(node.get("degraded")),
        "needs_redescribe": False,
        "revived": False,
        "process": "",
        "children": [],
        "coverage": "unknown",
        "missing": [],
        "missing_total": 0,
        "section": section,
        "page": page,
        "has_more": False,
        "next_page": None,
        "revision": "",
        "text": "",
        "fragments": [],
        "total_chars": 0,
    }

    # 描述层正文（archive）——dsh 口径：chain 只留 gist，长正文在 archive
    arc = md.load_archive(work_id, card_id, seq=seq, role=role, **tx_kw)
    row = arc.get((int(seq), role))
    if row:
        result["process"] = str(row.get("process") or "")
    # [F17/F19 补口 · 2026-10-31 审计发现] archive 已冻结（F17：新链不再写行），
    # 新节点的「经过」直接活在节点/条目上——archive 查不到时**回退节点自带字段**，
    # 否则 L1 展开回执丢「经过」栏（实测：节点带 process、archive 空 ⇒ process=''）。
    if not result["process"]:
        result["process"] = str(node.get("process") or "")

    # 已沉底 ⇒ 复活（sunk=false，父链 lastUsedAt 刷新）；revive=False
    # 是归拢回灌口——回灌不触发复活写入（复活语义是"模型主动找回"）。
    if result["sunk"] and revive and conn is None:
        # [F19 · 修] now 缺省用**当前时刻**（原缺省 0 ⇒ lastUsedAt=0，
        # 复活即重新进入回收线，等于白复活）。
        # [§三.2] 复活改走事务内局部更新（revive_sunk）：读-改-写同事务，
        # 旧链快照整链覆盖新记忆的路径不存在。
        import time as _time
        now = int(now_ms) if now_ms is not None else int(_time.time() * 1000)
        # 复活状态以**实际写入结果**为准——落库失败如实报没复活。
        saved = md.revive_sunk(work_id, card_id, int(seq), now_ms=now)
        result["revived"] = bool(saved)
        result["sunk"] = not bool(saved)
        result["note"] = ("该条目此前已沉底（从渲染剔除），本次展开已复活。"
                          if saved else
                          "该条目已沉底，本次复活写入未落库——下次展开会再试。")

    # [F19 · 整组展开] 该锚点名下的 L2 子条（upper 回指本锚）随组给出：
    # 结论 → 细节一次看全，不必逐条再展开。
    children: list[dict[str, Any]] = []
    for n in chain or []:
        if n is node:
            continue
        ups = n.get("upper") or []
        if any(int((u or {}).get("seq", -2)) == int(seq) for u in ups if isinstance(u, dict)):
            children.append({
                "seq": int((n.get("anchor") or {}).get("seq", 0) or 0),
                "gist": str(n.get("gist") or ""),
                "process": str(n.get("process") or ""),
                "sunk": bool(n.get("sunk")),
            })
    result["children"] = children

    # 降级 ⇒ 标记"需再描述"（本模块不调模型，见模块头注）
    if result["degraded"]:
        result["needs_redescribe"] = True
        result["note"] = (result.get("note", "") +
                          "该条目已降级（描述与结论同文），建议上层调一次再描述。")

    # ── 覆盖集 → 原文块（空洞保持空洞）────────────────────────────
    seqs, coverage = _coverage_seqs(node, int(seq))
    events, missing = _fetch_covered(work_id, card_id, seqs, coverage, **tx_kw)
    # 合成锚点（无覆盖、锚点块在原文库也不存在）⇒ 范围如实报未知，
    # 不拿"按锚点兜底"伪装成有覆盖。
    if coverage == "anchor" and not events:
        coverage = "unknown"
    result["coverage"] = coverage
    result["missing_total"] = len(missing)
    # 缺块清单独立页轴 missing_page（page 是逐字偏移，两轴不混）
    mp = max(0, int(missing_page or 0))
    m_start = mp * MISSING_PAGE_ITEMS
    result["missing"] = missing[m_start:m_start + MISSING_PAGE_ITEMS]
    result["missing_has_more"] = (m_start + MISSING_PAGE_ITEMS) < len(missing)
    result["missing_page"] = mp
    # [2026-11-20] blocks 兼容字段保留（旧调用方读 content），但工具回执
    # 不再全量回灌——覆盖集内块内容进逐字语料分页发。
    result["blocks"] = [{"seq": int(e["seq"]), "type": str(e.get("type") or ""),
                         "content": str(e.get("content") or ""),
                         "chars": len(str(e.get("content") or ""))}
                        for e in events]

    # ── 分页语料 ─────────────────────────────────────────────────
    if section == "details":
        corpus, rev_parts = _details_corpus(node, result["process"], children)
        slice_frags: list[dict[str, Any]] = []
    else:
        corpus, slice_frags, rev_parts = _verbatim_corpus(
            events, work_id=work_id, card_id=card_id, sanitized=sanitized, **tx_kw)
    result["total_chars"] = len(corpus)
    rev = _revision(coverage, seqs, section, rev_parts, missing)
    result["revision"] = rev
    # 版本不匹配 ⇒ 不动页位：如实报当前版本，请回调第 0 页（账目刷新沿用旧页
    # 是调用方的事——这里只保证版位一致）。
    if revision and str(revision) != rev:
        result["revision_mismatch"] = True
        result["note"] = (result.get("note", "") +
                          f"内容版本已变（当前 {rev}），请从 page=0 重新读取。")
        return result

    if section == "verbatim":
        pg = _page_slice(corpus, slice_frags, page, page_chars)
        result["text"] = pg["text"]
        result["fragments"] = pg["fragments"]
        result["has_more"] = pg["has_more"]
        result["next_page"] = (pg["offset"] + pg["chars"]
                             if pg["has_more"] else None)
    else:
        # details：同一分页口径（page＝字符偏移），无溯源 fragments
        total = len(corpus)
        start = max(0, page)
        text = "" if start >= total else corpus[start:start + page_chars]
        result["text"] = text
        end = start + len(text)
        result["has_more"] = end < total
        result["next_page"] = end if result["has_more"] else None

    # 历史引文明示（方案 §二.3）：所带内容是历史引文，不是本轮新事实。
    result["citation_note"] = "text/fragments 所带为该锚点覆盖的历史原文（引文）。"

    # page_chars 兼容旧参：旧签名 page_chars=None 表示不分页（整页返回）——
    # 新口径统一分页；旧调用方未传时按默认页长行为一致（page=0 首页）。
    if page_chars and page_chars > 0:
        result["truncated"] = result["has_more"]
    return result


def render(result: dict[str, Any]) -> str:
    """把展开结果渲染成给模型看的文本（dsh 工具回执同风格：先结论、再描述、再原文）。"""
    if not result.get("found"):
        return f"未找到：{result.get('reason', '')}"
    # [F23 · 2026-10-31 残留清理] 回执不再用【结论】/【依据】形态——工具回执是
    # 模型面文本，那对标记会教模型在正文里模仿假工具调用（与轮槽折叠同病的根）。
    parts = [f"【记忆】{result.get('gist') or ''}"]
    if result.get("note"):
        parts.append(f"【说明】{result['note']}")
    if result.get("process"):
        parts.append(f"【描述】{result['process']}")
    # [F19 · 整组] 细节子条随组渲染
    children = result.get("children") or []
    if children:
        lines = []
        for c in children:
            line = f"  ◇ [seq {c.get('seq')}] {c.get('gist') or ''}".rstrip()
            if c.get("process"):
                line += f"\n    {c['process']}"
            lines.append(line)
        parts.append("【细节组】\n" + "\n".join(lines))
    blocks = result.get("blocks") or []
    text = str(result.get("text") or "")
    if text:
        head = "【原文】" + (
            f"（第 {result.get('page', 0)} 页"
            f"{'，还有下一页' if result.get('has_more') else ''}）"
            if result.get("has_more") or result.get("page") else "")
        parts.append(f"{head}\n{text}")
    else:
        parts.append("【原文】（原文表里没有这段，可能尚未入库——先跑 backfill_origin）")
    if result.get("revision_mismatch"):
        parts.append(f"【翻页】{result.get('note') or '版本已变，请回到第 0 页'}")
    return "\n\n".join(parts)
