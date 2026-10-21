# -*- coding: utf-8 -*-
"""低频归拢（照抄 dsh `gen3/condense/fold-l1.ts` 的 job 语义）。

## 它解决什么

日常凝练只压"新的对话"，结论会**越积越多**——同一条线的事散成七八条顶层结论。
归拢就是**隔一段时间把顶层结论合并一次**：几条讲同一件事的，并成一条。

## dsh 的口径（逐条照抄）

1. **触发**：日常凝练**批提交成功后**检查批数，每 `FOLD_EVERY_BATCHES=5` 批挂一次；
2. **触发 ≠ 必然凝练**：先看**有没有可合并的**——顶层节点 **< 2 个 ⇒ 直接返回 `judge-empty`，
   一次模型调用都不发**；
3. **三步**：① step1 判断（纯文本，给模型全部顶层 gist 行，**不含 process 长正文**）；
   ② 有合并才走 step2；③ 转填（`start/process/result/kind/covers`）；
4. **失败语义**：chain / mark **一点不动**，下轮重试；
5. **不拒绝模型产出**（09-24 裁定）：模型点名合并的，**点不中也照样建节点**；
   但日志必须出「三数」——**merges / 点名 / 落地**，落空逐条带原因（否则"落地全落空"也看不出来）。

## 合并后旧节点怎么处理（本项目裁断，dsh 用 supersedes/supersededBy 表达取代关系）

新节点写 `supersedes=[旧 seq...]`；**旧节点写 `supersededBy={seq}`**（不删、不动原文），
渲染时由 `visible_chain` 剔除「沉底 or 已被取代」。理由：合并是"换一个说法"，
原文与旧结论都得留着可查（可追溯优先）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from . import memory_domain as md

logger = logging.getLogger("storage.fold_l1")

#: 每几批触发一次归拢（dsh 同值）
FOLD_EVERY_BATCHES = 5

#: step1 输出契约（纯 JSON）
FOLD_STEP1_PROMPT = (
    "你是记忆归拢器（第一步：只判断）。下面是某会话的全部**顶层结论行**（只有结论，没有长正文）。\n"
    "\n"
    "# 任务\n"
    "找出**讲的是同一件事、可以并成一条**的结论。并的时候按「谁 → 因 → 事 → 果」重新写成一条"
    "（读者是没有任何上下文的未来读者）。\n"
    "\n"
    "# 硬约束\n"
    "- 只依据给出的结论行判断，不臆测、不补原文里没有的因果；\n"
    "- 拿不准就不要并（宁可少并，不可并错）；\n"
    "- 只输出纯 JSON，不要解释文字、不要代码块包裹。\n"
    "\n"
    "# 输出格式\n"
    '{"merges": [{"covers": [{"seq": <被并的顶层结论 seq>}, ...], "gist": "<并成的一条>", '
    '"start": "<起因，可空>", "process": "<过程，可空>", "result": "<结果，可空>", '
    '"kind": "fact|decision|todo|exclusion|assumption"}]}\n'
    "没有可并的，返回 {\"merges\": []}。"
)


def should_fold(batch_count: int, last_fold_batch: int = 0) -> bool:
    """闸：距上次归拢是否已满 `FOLD_EVERY_BATCHES` 批（dsh 同口径）。"""
    return int(batch_count or 0) - int(last_fold_batch or 0) >= FOLD_EVERY_BATCHES


def foldable_tops(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """可归拢的顶层节点（dsh `foldableTop` 同口径）：

    `level == 1` ∧ `role == 'gist'` ∧ `upper` 空 ∧ `status == 'done'` ∧ **未沉底**。
    """
    out: list[dict[str, Any]] = []
    for n in chain or []:
        if int(n.get("level", 1) or 1) != 1:
            continue
        if str(n.get("role") or "gist") != "gist":
            continue
        if n.get("upper"):
            continue
        if str(n.get("status") or "") != "done":
            continue
        if n.get("sunk") is True:
            continue
        out.append(n)
    return out


def render_tops(chain: list[dict[str, Any]]) -> str:
    """step1 的输入：**只给结论行**（不含 process 长正文，dsh 同口径）。"""
    lines = []
    for n in foldable_tops(chain):
        seq = int((n.get("anchor") or {}).get("seq", 0) or 0)
        lines.append(f"[seq {seq}] {n.get('gist') or ''}")
    return "\n".join(lines)


def _parse_merges(text: str) -> Optional[list[dict[str, Any]]]:
    """宽松取 `{"merges":[...]}`；取不到 ⇒ None（调用方按失败处理、chain 不动）。"""
    raw = str(text or "")
    cands = [raw]
    lo, hi = raw.find("{"), raw.rfind("}")
    if 0 <= lo < hi:
        cands.append(raw[lo:hi + 1])
    for cand in cands:
        try:
            data = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("merges"), list):
            return data["merges"]
    return None


def apply_merges(chain: list[dict[str, Any]], merges: list[dict[str, Any]],
                 session_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把合并结果落进链（**纯函数**）：新节点 + 旧节点标 `supersededBy`。

    返回 `(新链, 报告)`；报告出「三数」：`merges / named / landed` ＋ `missed`（落空逐条带原因）。
    - **点不中也照样建节点**（dsh 09-24 裁定）；
    - 旧节点**不删、不动原文**，只标 `supersededBy`。
    """
    by_seq = {int((n.get("anchor") or {}).get("seq", -1)): n for n in chain}
    new_chain = list(chain)
    report: dict[str, Any] = {"merges": 0, "named": 0, "landed": 0, "missed": []}

    for m in merges or []:
        if not isinstance(m, dict):
            continue
        gist = str(m.get("gist") or "").strip()
        if not gist:
            continue
        covers = m.get("covers") or []
        named_seqs = [int((c or {}).get("seq", -1)) for c in covers if isinstance(c, dict)]
        report["merges"] += 1
        report["named"] += len(named_seqs)

        landed: list[int] = []
        for seq in named_seqs:
            old = by_seq.get(seq)
            if old is None:
                report["missed"].append({"seq": seq, "why": "链上无此 seq"})
                continue
            landed.append(seq)
        report["landed"] += len(landed)

        # 新节点用**合成锚点**（链上最大 seq + 1）——**不能**用被并节点的 seq：
        # 旧节点仍留在链上（不删），同 seq 再来一条 gist 会撞 I1「身份重复」。
        # dsh 同现象（提交日志里叫「无原文指针的合成锚点」），追溯由 coveredSeqs 承担。
        anchor_seq = (max(by_seq) + 1) if by_seq else 0
        node = md.new_node(session_id, anchor_seq, gist,
                           kind=str(m.get("kind") or "fact"),
                           status="done")
        node["level"] = 1
        node["upper"] = []
        node["lower"] = []
        covered: list[int] = []
        for seq in landed:
            covered.extend(md._covered(by_seq[seq]))   # noqa: SLF001 同包复用（父覆盖区间）
        node["coveredSeqs"] = sorted(set(covered)) or [anchor_seq]
        for key in ("start", "process", "result", "lesson"):
            if m.get(key):
                node[key] = str(m[key])
        if landed:
            node["supersedes"] = sorted(set(landed))

        # 旧节点标 supersededBy（不删）
        replaced = set(landed)
        for i, n in enumerate(new_chain):
            seq = int((n.get("anchor") or {}).get("seq", -2))
            if seq in replaced and str(n.get("role") or "gist") == "gist":
                nn = dict(n)
                nn["supersededBy"] = {"sessionId": session_id, "seq": anchor_seq}
                new_chain[i] = nn
        new_chain.append(node)
        by_seq[anchor_seq] = node

    return new_chain, report


def build_fold_prompt(chain: list[dict[str, Any]]) -> str:
    """step1 的完整提示（system 部分由调用方给，这里只出 user 段）。"""
    return "# 顶层结论行\n" + render_tops(chain)


def apply_fold_text(chain: list[dict[str, Any]], text: str,
                    session_id: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把模型的 step1 输出落进链（异步调用方用这个：先 await 拿 text，再调本函数）。"""
    merges = _parse_merges(text)
    if merges is None:
        logger.warning("[fold_l1] 归拢输出不成形（链不动）: %r", str(text)[:120])
        return chain, {"outcome": "abort", "reason": "bad-output", "merges": 0}
    if not merges:
        return chain, {"outcome": "judge-empty", "reason": "no-merge", "merges": 0}
    new_chain, report = apply_merges(chain, merges, session_id)
    report["outcome"] = "committed"
    viol = md.check_invariants(new_chain)
    if viol:
        logger.warning("[fold_l1] 归拢后不变量违例（如实回报）: %s", viol[:3])
        report["violations"] = viol
    logger.info("[fold_l1] 归拢 merges=%d 点名=%d 落地=%d%s",
                report["merges"], report["named"], report["landed"],
                "" if not report["missed"] else f" 落空={report['missed'][:5]}")
    return new_chain, report


def run_fold(chain: list[dict[str, Any]], call_llm: Callable[[str], str],
             session_id: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """跑一次归拢（**同步**，失败不动链）。

    `call_llm(prompt) -> text` 由调用方注入（测试可给假实现）。

    返回 `(链, 报告)`，报告含 `outcome`：
    - `'judge-empty'`：顶层 < 2，或模型说没得并（**零副作用**）；
    - `'committed'`：合并已落链；
    - `'abort'`：模型输出不成形 / 抛错（**链原样返回**，下轮重试）。
    """
    tops = foldable_tops(chain)
    if len(tops) < 2:
        return chain, {"outcome": "judge-empty", "reason": "tops<2", "merges": 0}
    try:
        text = call_llm(FOLD_STEP1_PROMPT + "\n\n" + build_fold_prompt(chain))
    except Exception as exc:  # noqa: BLE001 失败不动链
        logger.warning("[fold_l1] 模型调用失败（链不动）: %s", exc)
        return chain, {"outcome": "abort", "reason": "llm-error", "merges": 0}
    return apply_fold_text(chain, text, session_id)
