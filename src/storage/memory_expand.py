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

## 预算

dsh `X=∞`（CAPS-01 取消上限）、`X_ROUND=3X`、`PAGE_CHARS=X` ⇒ 本项目同样**不设上限**，
只提供 `page` 参数做分页（调用方要分页才分）。

## 本项目不做的一件

**degraded 的"再描述"不在这里调模型**：本项目把它做成**返回值里的标记**（`needs_redescribe=True`），
由调用方决定要不要调——理由：本模块保持纯（可测、无副作用），LLM 调用归上层。
（dsh 把它塞在工具里；差异如实记在此。）
"""

from __future__ import annotations

from typing import Any, Optional

from . import memory_domain as md, memory_lifecycle as lc, origin_store


def _find(chain: list[dict[str, Any]], seq: int, role: str) -> Optional[dict[str, Any]]:
    for n in chain or []:
        if int((n.get("anchor") or {}).get("seq", -1)) == int(seq) \
                and str(n.get("role") or "gist") == role:
            return n
    return None


def expand(work_id: str, card_id: str, seq: int, role: str = "gist",
           *, page: int = 0, page_chars: Optional[int] = None,
           now_ms: Optional[int] = None) -> dict[str, Any]:
    """展开一个锚点。返回结构化结果（**不抛**）。

    返回字段：
    - `found`：是否找到；`reason`：未找到时的原因（**明说**）；
    - `sunk` / `degraded` / `needs_redescribe`；
    - `gist`（结论）、`process`（描述层，来自 archive）、`blocks`（原文块，逐字）；
    - `revived`：本次是否复活；
    - `page` / `total_chars` / `truncated`（分页信息）。
    """
    chain = md.load_chain(work_id, card_id)
    node = _find(chain, seq, role)
    if node is None:
        return {"found": False, "reason": f"链上没有 anchor.seq={seq} role={role} 的条目",
                "seq": int(seq), "role": role}

    result: dict[str, Any] = {
        "found": True, "seq": int(seq), "role": role,
        "gist": str(node.get("gist") or ""),
        "sunk": bool(node.get("sunk")),
        "degraded": bool(node.get("degraded")),
        "needs_redescribe": False,
        "revived": False,
        "process": "",
        "blocks": [],
        "page": int(page or 0),
        "total_chars": 0,
        "truncated": False,
    }

    # 描述层正文（archive）——dsh 口径：chain 只留 gist，长正文在 archive
    arc = md.load_archive(work_id, card_id, seq=seq, role=role)
    row = arc.get((int(seq), role))
    if row:
        result["process"] = str(row.get("process") or "")

    # 已沉底 ⇒ 复活（sunk=false，父链 lastUsedAt 刷新）
    if result["sunk"]:
        now = int(now_ms) if now_ms is not None else 0
        revived_chain = lc.revive_update(chain, int(seq), now)
        md.save_chain(work_id, card_id, revived_chain)
        result["revived"] = True
        result["sunk"] = False
        result["note"] = "该条目此前已沉底（从渲染剔除），本次展开已复活。"

    # 降级 ⇒ 标记"需再描述"（本模块不调模型，见模块头注）
    if result["degraded"]:
        result["needs_redescribe"] = True
        result["note"] = (result.get("note", "") +
                          "该条目已降级（描述与结论同文），建议上层调一次再描述。")

    # 逐字原文（coveredSeqs → 原文表）
    covered = node.get("coveredSeqs")
    seqs = [int(x) for x in covered] if (isinstance(covered, list) and covered) else [int(seq)]
    lo, hi = min(seqs), max(seqs)
    span = origin_store.read_origin_span(work_id, card_id, lo, hi)
    blocks = [{"seq": e["seq"], "type": e.get("type", ""), "content": e.get("content", "")}
              for e in span["events"]]
    result["blocks"] = blocks
    total = sum(len(b["content"]) for b in blocks)
    result["total_chars"] = total
    if page_chars:
        start = int(page or 0) * int(page_chars)
        text_all = "\n".join(b["content"] for b in blocks)
        result["truncated"] = (start + int(page_chars)) < len(text_all)
        result["text"] = text_all[start:start + int(page_chars)]
    return result


def render(result: dict[str, Any]) -> str:
    """把展开结果渲染成给模型看的文本（dsh 工具回执同风格：先结论、再描述、再原文）。"""
    if not result.get("found"):
        return f"未找到：{result.get('reason', '')}"
    parts = [f"【结论】{result.get('gist') or ''}"]
    if result.get("note"):
        parts.append(f"【说明】{result['note']}")
    if result.get("process"):
        parts.append(f"【描述】{result['process']}")
    blocks = result.get("blocks") or []
    if blocks:
        body = "\n".join(f"[块{b['seq']}] {b['content']}" for b in blocks)
        parts.append(f"【原文】\n{body}")
    else:
        parts.append("【原文】（原文表里没有这段，可能尚未入库——先跑 backfill_origin）")
    return "\n\n".join(parts)
