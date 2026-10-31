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
2. **L1 全量**：一次看**全部切块**，为每块写三栏（低格式【块N】优先，JSON 兼容）；
3. **L2 全量**：一次看切块原文 + 全部 L1，为每块写细节子题。

## 本项目的硬规矩（dsh 同源）

**覆盖范围、锚点、消耗段全部由代码机械计算**——模型只产叙事文本，不给坐标。
`assemble()` 就是那台"机械计算器"：拿 `groups` 的 `end` 反推每块的原文区间，
再把它落到节点的 `coveredSeqs` 上。

## [2026-10-31 移植修复] 本轮对齐 dsh 的四处

- **F3 定界 abort**：S2a 产出过 G-1 闸（严格递增 + 界内），失败带机械错误报告
  重试 1 次、二次失败整批作废（mark 不动）——「解析不出 ⇒ 逐块兜底」的静默降级
  已处死（它造出「340 条、中位覆盖 1 块」）。
- **F4 凝练背景**：三段消息头注入「memory 全景」（全部 L1 ＋ 最近一批 L2，dsh
  `renderCondenseMemory` 同位），凝练产出不再与已有结论重复/碎片化。
- **F5 选材约束**：L1/L2 prompt 恢复 dsh 原版约束（以果为纲选材／中间结果归 L2／
  没做完但有目标必须写；每个有影响的果单独成条／不许事件清单／不写摘要不压缩、
  宁可长不可缺）＋ 人称硬要求 ＋ 保真红线 ＋ 史书读者契约。
- **F6 输出协议双格式**：低格式（【块N】纯文本）优先、JSON 回退；剥 <think>、
  块号容忍【块1/3】、end 尾标剥离、◇ 子题、无子题声明、整块合成单子题兜底。
- **F7 L1/L2 重试**：组数不符/解析失败 → 同批重试（最多 3 次），超限才 abort——
  原版 retries>3 才 abortL3，移植版曾零重试直接整批作废。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from . import memory_domain as md

logger = logging.getLogger("storage.condense_two_layer")


#: [处置 · 2026-10-31 用户令「根据状态做相应的处置：断连就重试；模型失效、
#: key 失效等不可能再运转下去的情况，就报错给用户」] 不可恢复类反馈的机械判据。
#: 放本模块（与截断信号同址）——run_two_layer 的重试逻辑吞异常，fatal 必须
#: 在被吞之前识别并上抛。memory_store re-export。
_FATAL_PAT = re.compile(
    r"401|403|api ?key|unauthorized|forbidden|invalid[_ ]?model|model[_ ]?not[_ ]?found"
    r"|不存在|未注册|credit|balance|欠费|余额|quota", re.I)

#: 可重试类（断连/超时/限流——梯子重试）
_RETRYABLE_PAT = re.compile(r"timeout|timed out|超时|connection|503|rate|429|overload", re.I)


def model_error_disposition(exc: BaseException) -> str:
    """按错误反馈定处置：``"fatal"``（key/模型/额度失效等不可能再运转——上抛
    报给用户）或 ``"retry"``（断连/超时/限流——同批重试/留待下轮）。"""
    st = str(exc) or ""
    if _FATAL_PAT.search(st):
        return "fatal"
    return "retry" if _RETRYABLE_PAT.search(st) else "fatal"


class CondenseTruncated(RuntimeError):
    """max-tokens 截断信号（F8a）：端点回 finish_reason=length ⇒ 产出被掐断。

    [2026-10-31 补口 · 审计发现] 定义在本模块（截断从三段调用的宽 except 里
    **透传**出去，不能被吞）：memory_store 侧 re-export。被吞的后果实测：
    截断在日志里伪装成「L1 解析失败」、L2 段截断后批照成、mark 照推进——
    「截断不再表现为 JSON 解析失败」的验收完全不成立。
    """

# ---------------------------------------------------------------------------
# 提示词（照抄 dsh `condense/prompts.ts` 的 L1/L2 契约，压缩掉与规则/知识相关的部分）
# ---------------------------------------------------------------------------

S2A_PROMPT = (
    "你是记忆凝练的**分块器**。下面是一段未凝练的对话原文，每块标了块号 `[idx N]`。\n"
    "\n"
    "# 任务\n"
    "按**事件边界**把它切成若干组：一件事从起头到落定算一组，不要把两件事塞进一组，"
    "也不要把一件事拆成两组。\n"
    "\n"
    "# 输出格式（纯 JSON，不要解释、不要代码块包裹）\n"
    '{"groups": [{"end": <本组最后一块的窗口下标>}, ...]}\n'
    "组的顺序＝原文顺序；每组的 `end` 必须严格递增；`end` 是窗口下标（0 起、连续无洞），"
    "最后一组的 `end` 至多是最后一块的下标；拿不准的后段可以不切（留给下一批）。"
)

# [F5 · 2026-10-31 对齐 dsh prompts.ts CONDENSE_L1_SYS] L1 选材约束逐条恢复：
# 以果为纲选材／中间结果归 L2／没做完但有目标必须写／人称硬要求／保真红线／
# 史书读者契约。输出格式改低格式（【块N】三栏）。
L1_PROMPT = (
    "你是记忆凝练的分层写作器（L1 层）。下面是**已按事件边界分好组**的原文"
    "（可能另附【memory 全景】——已有的记忆条目，供你去重对照），每组标了组号与块号范围。\n"
    "\n"
    "# 任务（唯一职责：为每一组写「开始／经过／完成」分栏自述）\n"
    "开始＝这事的起因与目标；经过＝过程（关键步骤、转折、卡点与解法）；完成＝结果"
    "（最终状态／结论／留下的决定）。像史书的分栏纪年：读者不看原文也能明白"
    "「因何而起、怎么走的、落成什么」。\n"
    "\n"
    "# 选材约束（硬要求）\n"
    "- **以果为纲选材**：L1 只记**最重要的那个结果**，以及与它目标直接相关的事；"
    "中间结果、分支结果、走过的弯路**不进这一层**——它们归 L2。\n"
    "- **没做完但有目标的必须写**：开始栏写清目标，经过栏写清做到哪、为什么没完成；"
    "完成栏不写（不许因为“没做完”就整组漏掉）。\n"
    "- **没有目标的动作不进这一层**（既没有果，也不指向任何目标）。\n"
    "- 保留具体事实：文件名、数字、裁定结果——这些是检索与回溯的锚。\n"
    "- 与【memory 全景】已有的结论重复的事，只写增量，不整段复述旧条目。\n"
    "- 只依据所给原文，不臆测、不补全、不推测动机。\n"
    "\n"
    "# 保真红线（优先于一切文采要求）\n"
    "原文没有的「因」不得补——宁可缺因，不虚构。三栏不强制齐全：有就写、没有就不写。\n"
    "\n"
    "# 史书读者契约\n"
    "每条写给**没有任何上下文的未来读者**（几个月后的模型/换过的模型/人）："
    "他不知道编号指什么、缩写是什么，只读你这一条就要懂。编号/章节引用首现即人话括注，"
    "或整体下沉到 L2；不得裸留。\n"
    "\n"
    "# 叙述人称（硬要求）\n"
    "一律用**第一人称**：这是「我」的自述。「我」＝本会话里干活的助手；对方称「用户」。\n"
    "禁止第三人称史官口吻（「助手确认…」「模型随后…」）。\n"
    "\n"
    "# 输出格式（低格式·纯文本）\n"
    "每组一段，以【块N】开头（N＝组号，从 1 递增），组内分栏：\n"
    "【块1】\n开始：…\n经过：…\n完成：…\n教训：…\n"
    "【块2】\n…\n"
    "（教训可选：没有就不写该行；**组数必须与输入一致，不得跳组**；"
    "不要输出 JSON、不要代码块包裹、不要任何额外解释。）"
)

# [F5 · 对齐 dsh CONDENSE_L2_SYS] 每个有影响的果单独成条／不许事件清单／
# 不写摘要不压缩、宁可长不可缺。
L2_PROMPT = (
    "你是记忆凝练的分层写作器（L2 层）。下面是切块原文与**已经写好的 L1 三栏自述**。\n"
    "\n"
    "# 任务\n"
    "为每一组写**细节子题**：把 L1 里那个果**展开**——中间结果、分支结果、走过的弯路、"
    "定下的具体决定，都记在这里（这些在 L1 是不铺开的）。\n"
    "\n"
    "# 硬约束\n"
    "- **每个有影响的果单独成条**（判据：会不会带来后续行为）：一个果一个子题，"
    "不许把多个果捏进一条；顺序按因果，不倒序。\n"
    "- **不许写成事件清单**：「先做了 A，又做了 B，然后做了 C」这种流水账不是细节——"
    "每个子题要写清它改变了什么、留下了什么。\n"
    "- **人称硬要求**：与 L1 同一人称——一律第一人称自述，「我」＝本会话里干活的助手，"
    "对方称「用户」；禁止第三人称史官口吻（「助手确认…」「模型随后…」）。\n"
    "- **保真红线**：原文没有的因/果不得补——宁可缺，不虚构；不臆测动机。\n"
    "- **史书读者契约**：每个子题写给**没有任何上下文的未来读者**，"
    "编号/缩写首现即人话括注，或整体下沉到细节正文；不得裸留。\n"
    "- **不写摘要、不压缩、宁可长不可缺**：L2 是细节层，把过程写全；"
    "「详见上文」「同上」一律禁止。\n"
    "- **L2 是 L1 的展开**：事实链必须承接，L1 那个果也要出现；不得与 L1 冲突。\n"
    "- 不臆造：因果只能从原文读出来，宁缺一个理由，不造一个理由。\n"
    "- 某组确实没有细节可写 ⇒ 该组写「（无子题）」（不要硬凑）。\n"
    "\n"
    "# 输出格式（低格式·纯文本）\n"
    "每组一段，以【块N】开头（N＝组号，从 1 递增），组内每个子题以 ◇ 开头：\n"
    "【块1】\n◇ 子题名\n细节正文……\n◇ 子题名2\n细节正文……\n"
    "（子题正文写到哪一块，可在段末标注（至块N）——不标也行；"
    "**组数必须与输入一致**；不要输出 JSON、不要代码块包裹。）"
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


def _strip_think(text: str) -> str:
    """剥 <think>…</think>（reasoning 模型把思考带进正文时，解析不吃思考）。"""
    return re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.S | re.I)


# 低格式的块头：容忍【块1】【块 1】【块1/3】（"1/3"＝共 3 组里的第 1 组）
_BLOCK_HEAD_RE = re.compile(r"【\s*块\s*(\d+)\s*(?:[/／]\s*\d+\s*)?】")
# L2 子题尾标：（至块N）／(至块N)／（end N）——剥离并捕获
_SUB_END_RE = re.compile(r"[（(]\s*(?:至块|end\s*)\s*(\d+)\s*[)）]\s*$")
_NO_SUBTOPIC_RE = re.compile(r"无子题|没有子题|（无）|\(无\)")


def _split_block_sections(text: str) -> list[str]:
    """按【块N】头切段（头前的杂文丢弃）。没有块头 ⇒ 空列表。"""
    out: list[str] = []
    matches = list(_BLOCK_HEAD_RE.finditer(str(text or "")))
    if not matches:
        return out
    for i, m in enumerate(matches):
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append(text[m.end():seg_end])
    return out


_L1_LABEL_RE = re.compile(r"^\s*(开始|起因|经过|完成|结果|教训)\s*[:：]\s*", re.M)


def parse_low_l1(text: str, n_groups: int) -> Optional[list[dict[str, Any]]]:
    """低格式 L1 解析：【块N】段 ＋ 开始/经过/完成/教训 分栏。

    - 剥 <think>；块号容忍【块1/3】，段序即组序（不要求块号＝组号）；
    - 段数 ≠ n_groups ⇒ None（整批重试/拒绝）；
    - 标签容忍别名：起因→开始、结果→完成；标签后的续行并入该栏（多行支持）；
    - 某栏缺失 ⇒ 空串（三栏不强制齐全）。
    """
    body = _strip_think(text)
    sections = _split_block_sections(body)
    if not sections or len(sections) != int(n_groups):
        return None
    out: list[dict[str, Any]] = []
    for seg in sections:
        row = {"开始": "", "经过": "", "完成": "", "教训": ""}
        # 抓标签位（起因→开始、结果→完成 归一）
        marks: list[tuple[str, int, int]] = []
        for m in _L1_LABEL_RE.finditer(seg):
            label = {"起因": "开始", "结果": "完成"}.get(m.group(1), m.group(1))
            marks.append((label, m.end(), m.start()))
        if not marks:
            return None        # 一段里一个分栏标签都没有 ⇒ 形态不对，交回退
        for i, (label, val_start, _) in enumerate(marks):
            val_end = marks[i + 1][2] if i + 1 < len(marks) else len(seg)
            row[label] = seg[val_start:val_end].strip()
        out.append(row)
    return out


def parse_low_l2(text: str, n_groups: int) -> Optional[list[list[dict[str, Any]]]]:
    """低格式 L2 解析：【块N】段 ＋ ◇ 子题。

    - ◇ 行＝子题名（行内「◇ 名（至块N）」剥尾标捕获 end）；段末尾标也认；
    - 「（无子题）」声明 ⇒ 空数组；无 ◇ 且无声明 ⇒ **整块合成单子题兜底**；
    - 段数 ≠ n_groups ⇒ None。
    """
    body = _strip_think(text)
    sections = _split_block_sections(body)
    if not sections or len(sections) != int(n_groups):
        return None
    out: list[list[dict[str, Any]]] = []
    for seg in sections:
        seg = seg.strip()
        if not seg or _NO_SUBTOPIC_RE.search(seg):
            out.append([])
            continue
        subs: list[dict[str, Any]] = []
        parts = re.split(r"^\s*◇\s*", seg, flags=re.M)
        lead = parts[0].strip()
        if lead:
            # ◇ 之前的杂文（模型没按 ◇ 起头）：整块合成单子题兜底
            end_m = _SUB_END_RE.search(lead)
            end_v = int(end_m.group(1)) if end_m else 0
            subs.append({"title": "", "gist": _SUB_END_RE.sub("", lead).strip(),
                         "end": end_v})
        for p in parts[1:]:
            lines = p.strip().splitlines()
            if not lines:
                continue
            title = lines[0].strip()
            end_v = 0
            end_m = _SUB_END_RE.search(title)
            if end_m:
                end_v = int(end_m.group(1))
                title = _SUB_END_RE.sub("", title).strip()
            # 尾标也可以写在子题正文末尾（（至块N）收尾行）——剥正文、捕获 end
            body = "\n".join(lines[1:]).strip()
            if not end_v:
                body_m = _SUB_END_RE.search(body)
                if body_m:
                    end_v = int(body_m.group(1))
                    body = _SUB_END_RE.sub("", body).strip()
            gist = body
            if title or gist:
                subs.append({"title": title, "gist": gist or title, "end": end_v})
        out.append(subs)
    return out


def parse_groups(text: str, block_seqs: list[int]) -> Optional[list[tuple[int, int]]]:
    """解析 S2a 的分组 → `[(首块seq, 末块seq), ...]`；**非法 ⇒ None**（整批作废）。

    `end` 口径 = **窗口下标**（0 起、连续无洞），与 S2A_PROMPT 字面一致：
    「第 1 组从 idx 0 开始，之后每组从前一组 end+1 开始」。

    [F3 · 2026-10-31 对齐 dsh G-1 闸] 原实现「解析不出 ⇒ 逐块兜底」是**静默降级**——
    实测后果就是「340 条结论、中位覆盖 1 块」（每条消息一条结论）。dsh 的裁定是：
    定界失败过不了 G-1 闸（两两不相交 ＋ 并集＝连续前缀）就**整批作废重试**，
    「静默降级已处死」。现在机械校验三条，任一不满足 ⇒ None：
      ① 解析得出至少一个合法 end（解析不出/非 JSON/groups 非列表 ⇒ None）；
      ② end 严格递增且都在 0..n-1 内（出现一个非法 end 即整批非法，**不再跳过**）；
      ③ 组间两两不相交（①②已蕴含）且并集＝从 0 起的连续前缀（①②已蕴含）。

    **不补尾巴**：dsh 口径「被消耗段 = 最后一个 end+1 条，可以是窗口前缀——
    拿不准的后段留给下一批」。补一组会把没定界的尾巴和前一组合并，等于让模型
    的「拿不准」被代码改写成「就是一组」。
    """
    data = _loose_json(_strip_think(text))
    ends: list[int] = []
    if isinstance(data, dict) and isinstance(data.get("groups"), list):
        raw_groups = data["groups"]
        if not raw_groups:
            return None                # groups 空列表 ⇒ 没定界，整批非法
        for g in raw_groups:
            if not isinstance(g, dict):
                return None            # 组元素不是对象 ⇒ 整批非法
            try:
                ends.append(int(g.get("end")))
            except (TypeError, ValueError):
                return None            # end 缺失/非整数 ⇒ 整批非法
    if not ends or not block_seqs:
        return None
    # 任一 end 越界 ⇒ 整批非法（调用方带错误报告重试，二次失败 abort）
    if any(e < 0 or e >= len(block_seqs) for e in ends):
        return None
    if any(ends[i] <= ends[i - 1] for i in range(1, len(ends))):
        return None                    # 非严格递增 ⇒ 整批非法
    # 逐块映射（G-1 闸已过 ⇒ 组间必不相交、并集＝连续前缀）
    groups: list[tuple[int, int]] = []
    prev = -1
    for end in ends:
        groups.append((block_seqs[prev + 1], block_seqs[end]))
        prev = end
    return groups


def parse_l1(text: str, n_groups: int) -> Optional[list[dict[str, Any]]]:
    """解析 L1 → `[{"开始","经过","完成","教训"}, ...]`。

    [F6 输出协议双格式] **低格式【块N】优先，JSON 回退**；组数不符 ⇒ None（整批重试）。
    """
    low = parse_low_l1(text, n_groups)
    if low is not None:
        return low
    data = _loose_json(_strip_think(text))
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
    """解析 L2 → 每组一个子题列表；低格式优先、JSON 回退；组数不符 ⇒ None。"""
    low = parse_low_l2(text, n_groups)
    if low is not None:
        return low
    data = _loose_json(_strip_think(text))
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

#: [F23 同源 · 2026-10-31 实测] 模型会把【结论】/【依据】标记抄进字段值
#: （它凝练的源文本是旧摘要、含这对标记）——装配时机械剥除，防止标记形态
#: 经记忆区回流进上下文、教模型在正文里输出假工具调用（dsh scrubMarkerLines 同理）。
_SCRUB_MARK_RE = re.compile(r"^(?:\s*(?:【结论】|【依据】|【记忆】)\s*)+")


def _scrub_marks(text: str) -> str:
    return _SCRUB_MARK_RE.sub("", str(text or "")).strip()


def assemble(block_seqs: list[int], groups: list[tuple[int, int]],
             l1: list[dict[str, Any]],
             l2: Optional[list[list[dict[str, Any]]]] = None,
             session_id: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 L1/L2 装配成 `(结论节点列表, 描述层列表)`。

    - **结论节点**：每组一个（`level=1`），`coveredSeqs` ＝该组覆盖的块号区间（**机械算**）；
      `gist` ＝「完成」栏（没有就用「开始」），`start`/`process`/`lesson` 随栏落；
    - **描述层**：L2 的每个子题一条，`coveredSeqs` ＝ 从**上一个子题的 end+1** 到**本子题 end**
      （**机械算**，不信模型给的起点）；子题没标 end 且是组内最后一个 ⇒ 覆盖到组尾；
      `process` ＝ 子题 gist；
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
        row = {k: _scrub_marks(v) for k, v in row.items()}   # 字段值剥标记（F23 同源）
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

        # 描述层：子题按 end 递增机械切区间。
        # ★ 起点必须落在 `covered[0]`（父的首块）**之后**：父的首块已被 L1 占作 anchor，
        #   子题若从它起算，首个子题的 anchor 就会与父撞号 ⇒ I1「gist 身份重复」。
        #   dsh `condense.ts:3273` 用全局 `used` 集合达成的正是这件事（每个 seq 只归属
        #   一个节点）。
        # [F6 低格式] 子题没标 end：**末位子题**兜底覆盖到组尾，非末位子题跳过
        # （没有坐标就不臆造区间）。
        prev_end = covered[0]
        subs = list(l2[gi] if l2 and gi < len(l2) else [])
        for si, sub in enumerate(subs):
            end = int(sub.get("end") or 0)
            if not end and si == len(subs) - 1:
                end = covered[-1]       # 末位子题未标 end ⇒ 覆盖到组尾
            if end < covered[0] or end > covered[-1] or end <= prev_end:
                continue
            sub_covered = [s for s in covered if prev_end < s <= end]
            prev_end = end
            if not sub_covered:
                continue
            descs.append({
                "seq": sub_covered[0],
                "role": "description",
                "process": _scrub_marks(sub["gist"]),
                "title": sub.get("title") or "",
                "coveredSeqs": sub_covered,
                "parent_seq": covered[0],
            })
    return nodes, descs


# ---------------------------------------------------------------------------
# 编排（三次调用；S2a/L1 失败 ⇒ 整批作废；L2 失败只丢细节）
# ---------------------------------------------------------------------------

#: L1/L2 段的同批重试上限（F7 · 对齐 dsh retries>3 才 abortL3）
MAX_STAGE_RETRIES = 3


async def run_two_layer(blocks: list[dict[str, Any]],
                        call_llm: Callable[[str, str], Awaitable[str]],
                        session_id: str = "",
                        domain_full: str = "") -> dict[str, Any]:
    """跑 S2a → L1 → L2。`call_llm(system, user) -> text`（异步，由调用方注入）。

    返回 `{"ok": bool, "nodes": [...], "descriptions": [...], "stage": 失败在哪一段,
    "retries": 重试计数}`。
    **S2a/L1 段失败 ⇒ `ok=False`**，调用方必须**不改 mark**（dsh 同口径：整批拒绝）；
    L2 段失败 ⇒ 只丢细节，结论照常。

    `domain_full`（F4）：「memory 全景」文本（调用方现算），拼进三段消息头——
    凝练产出不再与已有结论重复/碎片化；空串＝无全景（老调用方兼容）。
    """
    seqs = [int((b.get("seq") if isinstance(b, dict) else b) or 0) for b in blocks]
    if not seqs:
        return {"ok": False, "stage": "empty", "nodes": [], "descriptions": [],
                "retries": 0}

    def _text(i: int) -> str:
        b = blocks[i] if i < len(blocks) else {}
        return str((b.get("content") if isinstance(b, dict) else "") or "")

    # F4：全景消息头（有才拼——dsh renderCondenseMemory 注入位＝三段消息正文开头）
    domain_head = f"【memory 全景】\n{domain_full}\n\n" if domain_full.strip() else ""
    total_retries = 0

    # S2a 的输入按**窗口下标**标注（`[idx i]`，0 起连续无洞）——与 S2A_PROMPT
    # 的字面口径一致。
    s2a_body = domain_head + "\n\n".join(f"[idx {i}]\n{_text(i)}" for i in range(len(seqs)))

    # S2a 定界（F3：G-1 闸 ＋ 失败带机械错误报告重试 1 次，二次失败 abort 整批。
    # 删掉了原「解析不出 ⇒ 逐块兜底」的静默降级——那正是「340 条、中位覆盖 1 块」
    # 的来源。abort ⇒ 整批作废、mark 不动（调用方按 ok=False 处理）。）
    groups: Optional[list[tuple[int, int]]] = None
    g_err = ""
    for attempt in (1, 2):
        try:
            g_text = await call_llm(
                S2A_PROMPT, s2a_body + (f"\n\n# 上次产出被拒（机械校验）\n{g_err}" if g_err else ""))
        except CondenseTruncated:
            raise                    # [F8a] 截断是终态反馈：整批作废，不许当解析失败重试
        except Exception as exc:  # noqa: BLE001
            if model_error_disposition(exc) == "fatal":
                raise                # [处置] key/模型失效类：重试枉然，上抛报给用户
            g_err = f"调用异常 {type(exc).__name__}: {exc}"
            logger.warning("[two_layer] S2a 调用失败（第 %d 次）: %s", attempt, exc)
            continue
        groups = parse_groups(g_text, seqs)
        if groups is not None:
            break
        g_err = (f"产出未过机械校验：end 必须是严格递增、且都在 0..{len(seqs) - 1} 的"
                 f"窗口下标；输出格式 {{\"groups\":[{{\"end\":N}},...]}}。请严格按格式重出。")
        logger.warning("[two_layer] S2a 产出未过 G-1 闸（第 %d 次），带错误报告重试", attempt)
    if groups is None:
        logger.warning("[two_layer] S2a 二次仍失败 ⇒ 整批作废（mark 不动）")
        return {"ok": False, "stage": "s2a", "nodes": [], "descriptions": [],
                "retries": total_retries}

    # L1/L2 的输入**必须带组头与组数**（dsh `buildBlockMessages` 口径：
    # `【组 i/N】（原文 idx a–b）`）。
    def _grouped() -> str:
        parts: list[str] = []
        for gi, (g_from, g_to) in enumerate(groups):
            idxs = [i for i, s in enumerate(seqs) if g_from <= s <= g_to]
            if not idxs:
                continue
            inner = "\n\n".join(f"[idx {i}]\n{_text(i)}" for i in idxs)
            parts.append(f"【组 {gi + 1}/{len(groups)}】"
                         f"（idx {idxs[0]}–{idxs[-1]}，块号 {g_from}–{g_to}）\n{inner}")
        return "\n\n".join(parts)

    # L1 三栏（F7：解析失败/组数不符 ⇒ 同批带错误报告重试，最多 MAX_STAGE_RETRIES 次，
    # 超限才整批作废——原版 retries>3 才 abortL3）
    l1: Optional[list[dict[str, Any]]] = None
    l1_err = ""
    for attempt in range(1 + MAX_STAGE_RETRIES):
        try:
            l1_text = await call_llm(
                L1_PROMPT, domain_head + _grouped() +
                (f"\n\n以上共 {len(groups)} 组，每组写一条。"
                 + (f"\n\n# 上次产出被拒\n{l1_err}" if l1_err else "")))
        except CondenseTruncated:
            raise                    # [F8a] 同上：截断透传（整批作废、mark 不动）
        except Exception as exc:  # noqa: BLE001
            if model_error_disposition(exc) == "fatal":
                raise                # [处置] 同上
            l1_err = f"调用异常 {type(exc).__name__}: {exc}"
            logger.warning("[two_layer] L1 调用失败（第 %d 次）: %s", attempt + 1, exc)
            continue
        l1 = parse_l1(l1_text, len(groups))
        if l1 is not None:
            break
        l1_err = (f"L1 组数或格式不符：必须恰好 {len(groups)} 组，每组以【块N】开头、"
                  f"组内「开始：/经过：/完成：/教训：」分栏。请严格按格式重出。")
        total_retries += 1
        logger.warning("[two_layer] L1 解析失败（第 %d 次），带错误报告重试", attempt + 1)
    if l1 is None:
        return {"ok": False, "stage": "l1", "nodes": [], "descriptions": [],
                "retries": total_retries}

    # L2 细节（F7：同样重试；超限 ⇒ 只丢细节，结论照常）
    l2: Optional[list[list[dict[str, Any]]]] = None
    l2_err = ""
    for attempt in range(1 + MAX_STAGE_RETRIES):
        try:
            l2_text = await call_llm(
                L2_PROMPT, domain_head + _grouped() + "\n\n# L1 三栏\n" +
                json.dumps({"l1": l1}, ensure_ascii=False) +
                (f"\n\n# 上次产出被拒\n{l2_err}" if l2_err else ""))
            l2 = parse_l2(l2_text, len(groups))
        except CondenseTruncated:
            raise                    # [F8a] L2 截断同样整批作废（「只丢细节」只适用于解析失败）
        except Exception as exc:  # noqa: BLE001
            if model_error_disposition(exc) == "fatal":
                raise                # [处置] 同上
            l2_err = f"调用异常 {type(exc).__name__}: {exc}"
            logger.warning("[two_layer] L2 调用失败（第 %d 次）: %s", attempt + 1, exc)
            continue
        if l2 is not None:
            break
        l2_err = (f"L2 组数或格式不符：必须恰好 {len(groups)} 组，每组以【块N】开头、"
                  f"组内子题以 ◇ 开头；无细节的组写「（无子题）」。请严格按格式重出。")
        total_retries += 1
        logger.warning("[two_layer] L2 解析失败（第 %d 次），带错误报告重试", attempt + 1)
    if l2 is None:
        logger.warning("[two_layer] L2 重试超限 ⇒ 只丢细节（结论照常）")

    nodes, descs = assemble(seqs, groups, l1, l2, session_id)
    return {"ok": True, "stage": "done", "nodes": nodes, "descriptions": descs,
            "groups": groups, "retries": total_retries}
