# -*- coding: utf-8 -*-
"""两层凝练（照抄 dsh `condense/consolidation/condense.ts` 的 S2a → L1 → L2 三段）。

## 为什么分两层（dsh 的原话，逐条保留其意）

一次压完，模型会**把细节和结论混在一起**：既写不清"到底落了什么果"，又把过程铺成流水账。
dsh 拆成两层，**粒度不同，不是省略**：

| 层 | 写什么 | 落到本项目 |
|---|---|---|
| **L1（结论层）** | 每块写「开始／经过／完成（＋教训）」三栏；**以果为纲**：只记最重要的那个果 + 与它目标相关的事；**第一人称**（"我"=助手，对方称"用户"） | **gist 节点**（顶层结论） |
| **L2（细节层）** | 把 L1 那个果**展开**成细节子题（事实链必须承接 L1）；只记**有影响的果**，顺序按因果 | **description 子节点**（进 archive） |

## 三段调用

1. **S2a 定界**：把原文按**事件边界**切块，输出 `{"groups":[{"end":N}]}`；
2. **L1 全量**：一次看**全部切块**，为每块写三栏，输出 `{"l1":[{"group":N,"开始","经过","完成","教训"}]}`
   —— **块数必须与输入一致，不得跳块**；
3. **L2 全量**：一次看切块原文 + 全部 L1，为每块写细节子题，输出
   `{"l2":[{"group":N,"subtopics":[{"title","gist","end"}]}]}`。

## 本项目的一条硬规矩（dsh 同源）

**覆盖范围、锚点、消耗段全部由代码机械计算**——模型只产叙事文本，不给坐标。
`assemble()` 就是那台"机械计算器"：拿 `groups` 的 `end` 反推每块的原文区间，
再把它落到节点的 `coveredSeqs` 上。

## 状态

**本模块尚未接入凝练主链路**（开关 `CONDENSE_TWO_LAYER` 未接）。原因：它是三次模型调用，
接进去会改变生产凝练的行为与成本——**要用户点头**。模块与解析/装配已完备并有测试。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional

from . import memory_domain as md

logger = logging.getLogger("storage.condense_two_layer")

# ---------------------------------------------------------------------------
# 提示词（照抄 dsh `condense/prompts.ts` 的 L1/L2 契约，压缩掉与规则/知识相关的部分）
# ---------------------------------------------------------------------------

S2A_PROMPT = (
    "你是记忆凝练的**分块器**。下面是一段未凝练的对话原文，每块标了块号 `[块N]`。\n"
    "\n"
    "# 任务\n"
    "按**事件边界**把它切成若干组：一件事从起头到落定算一组，不要把两件事塞进一组，"
    "也不要把一件事拆成两组。\n"
    "\n"
    "# 输出格式（纯 JSON，不要解释、不要代码块包裹）\n"
    '{"groups": [{"end": <本组最后一块的块号>}, ...]}\n'
    "组的顺序＝原文顺序；每组的 `end` 必须递增；最后一组的 `end` 必须是最后一块。"
)

L1_PROMPT = (
    "你是记忆凝练的分层写作器（L1 层）。下面是**已按事件边界分好组**的原文，每组标了组号与块号范围。\n"
    "\n"
    "# 任务（唯一职责：为每一组写「开始／经过／完成」分栏自述）\n"
    "开始＝这事的起因与目标；经过＝过程（关键步骤、转折、卡点与解法）；完成＝结果"
    "（最终状态／结论／留下的决定）。像史书的分栏纪年：读者不看原文也能明白「因何而起、怎么走的、落成什么」。\n"
    "\n"
    "# 写作要求\n"
    "- 每组一条，按 开始／经过／完成（＋可选 教训）分栏；`group` 从 1 按组序对应，"
    "**组数必须与输入一致，不得跳组**。\n"
    "- 三栏**不强制齐全**：有就写、没有就不写。事情还没做完就没有「完成」栏；拿不准就少写一栏，绝不编造。\n"
    "- **以果为纲**：只记**最重要的那个结果**，以及与它目标相关的事；"
    "中间结果、分支结果不在这一层铺开；顺序按因果（因在前、果在后），不倒序。\n"
    "- **没做完但有目标的必须写**：开始栏写清目标，经过栏写清做到哪、为什么没完成；完成栏不写。\n"
    "- **没有目标的动作不进这一层**（既没有果，也不指向任何目标）。\n"
    "- 保留具体事实：文件名、数字、裁定结果——这些是检索与回溯的锚。\n"
    "- 只依据所给原文，不臆测、不补全、不推测动机。\n"
    "\n"
    "# 叙述人称（硬要求）\n"
    "一律用**第一人称**：这是「我」的自述。「我」＝本会话里干活的助手；对方称「用户」。\n"
    "禁止第三人称史官口吻（「助手确认…」「模型随后…」）。\n"
    "\n"
    "# 输出格式（纯 JSON）\n"
    '{"l1": [{"group": 1, "开始": "...", "经过": "...", "完成": "...", "教训": "..."}, ...]}\n'
    "（「教训」可选：没有就不写该键或留空）"
)

L2_PROMPT = (
    "你是记忆凝练的分层写作器（L2 层）。下面是切块原文与**已经写好的 L1 三栏自述**。\n"
    "\n"
    "# 任务\n"
    "为每一组写**细节子题**：把 L1 里那个果**展开**——中间结果、分支结果、走过的弯路、"
    "定下的具体决定，都记在这里（这些在 L1 是不铺开的）。\n"
    "\n"
    "# 硬约束\n"
    "- **L2 是 L1 的展开**：事实链必须承接，L1 那个果也要出现；不得与 L1 冲突。\n"
    "- 只记**有影响的果**（判据：会不会带来后续行为）；顺序按因果，不倒序。\n"
    "- 不臆造：因果只能从原文读出来，宁缺一个理由，不造一个理由。\n"
    "- 某组确实没有细节可写 ⇒ `subtopics` 给空数组（不要硬凑）。\n"
    "\n"
    "# 输出格式（纯 JSON）\n"
    '{"l2": [{"group": 1, "subtopics": [{"title": "子题名", "gist": "细节", "end": <该子题覆盖到的块号>}]}, ...]}\n'
    "`end` 是该子题覆盖原文的结束块号（组内递增）；组数必须与输入一致。"
)


# ---------------------------------------------------------------------------
# 解析（全部纯函数）
# ---------------------------------------------------------------------------

def _loose_json(text: str) -> Any:
    raw = str(text or "")
    cands = [raw]
    lo, hi = raw.find("{"), raw.rfind("}")
    if 0 <= lo < hi:
        cands.append(raw[lo:hi + 1])
    for cand in cands:
        try:
            return json.loads(cand)
        except (ValueError, TypeError):
            continue
    return None


def parse_groups(text: str, block_seqs: list[int]) -> list[tuple[int, int]]:
    """解析 S2a 的分组 → `[(首块seq, 末块seq), ...]`（**机械兜底**：解析不出 ⇒ 每块一组）。

    dsh 口径：分组只是"事件边界"，**取不到就退化为逐块**——宁可多切，不能少切
    （少切会把两件事混成一条结论）。
    """
    data = _loose_json(text)
    groups: list[tuple[int, int]] = []
    ends = []
    if isinstance(data, dict) and isinstance(data.get("groups"), list):
        ends = [int(g.get("end", -1)) for g in data["groups"] if isinstance(g, dict)]
    if not ends or not block_seqs:
        return [(s, s) for s in block_seqs]

    prev_idx = -1
    for end in ends:
        # end 是块号（原文里的 [块N]），映射到 block_seqs 的下标
        try:
            idx = block_seqs.index(end)
        except ValueError:
            continue
        if idx <= prev_idx:
            continue
        groups.append((block_seqs[prev_idx + 1], block_seqs[idx]))
        prev_idx = idx
    if prev_idx < len(block_seqs) - 1:          # 尾巴没收进来 ⇒ 补一组
        groups.append((block_seqs[prev_idx + 1], block_seqs[-1]))
    return groups or [(s, s) for s in block_seqs]


def parse_l1(text: str, n_groups: int) -> Optional[list[dict[str, Any]]]:
    """解析 L1 → `[{"开始","经过","完成","教训"}, ...]`；**组数不符 ⇒ None**（整批拒绝）。"""
    data = _loose_json(text)
    if not isinstance(data, dict) or not isinstance(data.get("l1"), list):
        return None
    rows = [r for r in data["l1"] if isinstance(r, dict)]
    if len(rows) != int(n_groups):
        logger.warning("[two_layer] L1 组数不符：给 %d 组，回 %d 组（整批拒绝）",
                       n_groups, len(rows))
        return None
    out = []
    for r in rows:
        out.append({
            "开始": str(r.get("开始") or ""),
            "经过": str(r.get("经过") or ""),
            "完成": str(r.get("完成") or ""),
            "教训": str(r.get("教训") or ""),
        })
    return out


def parse_l2(text: str, n_groups: int) -> Optional[list[list[dict[str, Any]]]]:
    """解析 L2 → 每组一个子题列表；组数不符 ⇒ None。"""
    data = _loose_json(text)
    if not isinstance(data, dict) or not isinstance(data.get("l2"), list):
        return None
    rows = [r for r in data["l2"] if isinstance(r, dict)]
    if len(rows) != int(n_groups):
        logger.warning("[two_layer] L2 组数不符：给 %d 组，回 %d 组（整组细节作废）",
                       n_groups, len(rows))
        return None
    out: list[list[dict[str, Any]]] = []
    for r in rows:
        subs = []
        for s in (r.get("subtopics") or []):
            if not isinstance(s, dict):
                continue
            gist = str(s.get("gist") or "").strip()
            if not gist:
                continue
            subs.append({"title": str(s.get("title") or ""), "gist": gist,
                         "end": int(s.get("end", 0) or 0)})
        out.append(subs)
    return out


# ---------------------------------------------------------------------------
# 装配（机械计算覆盖范围——模型不给坐标）
# ---------------------------------------------------------------------------

def assemble(block_seqs: list[int], groups: list[tuple[int, int]],
             l1: list[dict[str, Any]],
             l2: Optional[list[list[dict[str, Any]]]] = None,
             session_id: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 L1/L2 装配成 `(结论节点列表, 描述层列表)`。

    - **结论节点**：每组一个（`level=1`），`coveredSeqs` ＝该组覆盖的块号区间（**机械算**）；
      `gist` ＝「完成」栏（没有就用「开始」），`start`/`process`/`lesson` 随栏落；
    - **描述层**：L2 的每个子题一条，`coveredSeqs` ＝ 从**上一个子题的 end+1** 到**本子题 end**
      （**机械算**，不信模型给的起点）；`process` ＝ 子题 gist；
    - 组内块号缺失（原文没有）⇒ 跳过该组并记日志（不臆造）。
    """
    nodes: list[dict[str, Any]] = []
    descs: list[dict[str, Any]] = []
    for gi, (g_from, g_to) in enumerate(groups):
        covered = [s for s in block_seqs if g_from <= s <= g_to]
        if not covered:
            logger.warning("[two_layer] 组 %d 的块号区间 [%s,%s] 在原文里没有块（跳过）",
                           gi + 1, g_from, g_to)
            continue
        row = l1[gi] if gi < len(l1) else {"开始": "", "经过": "", "完成": "", "教训": ""}
        gist = (row.get("完成") or "").strip() or (row.get("开始") or "").strip()
        node = md.new_node(session_id, covered[0], gist, kind="fact",
                           status="done" if row.get("完成") else "ongoing")
        node["level"] = 1
        node["upper"] = []
        node["lower"] = []
        node["coveredSeqs"] = covered
        if row.get("开始"):
            node["start"] = row["开始"]
        if row.get("经过"):
            node["process"] = row["经过"]
        if row.get("教训"):
            node["lesson"] = row["教训"]
        nodes.append(node)

        # 描述层：子题按 end 递增机械切区间
        prev_end = covered[0] - 1
        for sub in (l2[gi] if l2 and gi < len(l2) else []):
            end = int(sub.get("end") or 0)
            if end < covered[0] or end > covered[-1] or end <= prev_end:
                continue
            sub_covered = [s for s in covered if prev_end < s <= end]
            prev_end = end
            if not sub_covered:
                continue
            descs.append({
                "seq": sub_covered[0],
                "role": "description",
                "process": sub["gist"],
                "title": sub.get("title") or "",
                "coveredSeqs": sub_covered,
                "parent_seq": covered[0],
            })
    return nodes, descs


# ---------------------------------------------------------------------------
# 编排（三次调用；任何一段失败 ⇒ 整批作废，mark 不动）
# ---------------------------------------------------------------------------

async def run_two_layer(blocks: list[dict[str, Any]],
                        call_llm: Callable[[str, str], Awaitable[str]],
                        session_id: str = "") -> dict[str, Any]:
    """跑 S2a → L1 → L2。`call_llm(system, user) -> text`（异步，由调用方注入）。

    返回 `{"ok": bool, "nodes": [...], "descriptions": [...], "stage": 失败在哪一段}`。
    **任一段失败 ⇒ `ok=False`**，调用方必须**不改 mark**（dsh 同口径：整批拒绝）。
    """
    seqs = [int((b.get("seq") if isinstance(b, dict) else b) or 0) for b in blocks]
    body = "\n\n".join(f"[块{int(b.get('seq') or 0)}]\n{b.get('content') or ''}"
                       for b in blocks if isinstance(b, dict))
    if not seqs:
        return {"ok": False, "stage": "empty", "nodes": [], "descriptions": []}

    # S2a 定界（失败/不成形 ⇒ 逐块兜底，不阻断）
    try:
        g_text = await call_llm(S2A_PROMPT, body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[two_layer] S2a 调用失败，退化为逐块: %s", exc)
        g_text = ""
    groups = parse_groups(g_text, seqs)

    # L1 三栏（组数不符 ⇒ 整批拒绝）
    try:
        l1_text = await call_llm(L1_PROMPT, body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[two_layer] L1 调用失败（整批作废）: %s", exc)
        return {"ok": False, "stage": "l1", "nodes": [], "descriptions": []}
    l1 = parse_l1(l1_text, len(groups))
    if l1 is None:
        return {"ok": False, "stage": "l1", "nodes": [], "descriptions": []}

    # L2 细节（失败 ⇒ 只丢细节，结论照常）
    l2 = None
    try:
        l2_text = await call_llm(L2_PROMPT, body + "\n\n# L1 三栏\n" +
                                 json.dumps({"l1": l1}, ensure_ascii=False))
        l2 = parse_l2(l2_text, len(groups))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[two_layer] L2 调用失败（只丢细节）: %s", exc)

    nodes, descs = assemble(seqs, groups, l1, l2, session_id)
    return {"ok": True, "stage": "done", "nodes": nodes, "descriptions": descs,
            "groups": groups}
