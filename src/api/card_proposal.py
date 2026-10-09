# -*- coding: utf-8 -*-
"""建卡协议解析（白板版 2026-10-02，双形态跨度制）。

[建卡工具化 2026-10-08] 本模块降级为**兜底通道**：建卡主通道已改为标准工具
create_card（见 card_tools.py）。本模块仍解析文本协议块（模型过渡期还会写），
两条通道并存、同轮去重（见 routes）。本模块一行不删——它同时是三席观点清洗
的剥离器（simple_service.clean_viewpoint_text 依赖它）。

模型旧建卡格式（实际出现过两种形态，都必须接住）：
  形态一（标准）：```card-proposal\n[...]\n```
  形态二（DeepSeek 系泄漏）：<｜｜card-proposal｜｜>\n[...]\n<｜｜/card-proposal｜｜>
    —— 竖线可能全角(U+FF5C)/半角(U+007C)、单条/双条/零条(10-03 实录纯 ASCII 形态)、标记词两侧可能有空白

parse_proposals(reply_text) -> (clean_text, proposals)
  - 提取所有形态的块；clean_text = 剥离协议块后的正文
  - fenced 块内容里嵌 ```（如 content 含代码围栏）会让闭合点歧义：
    对每个候选闭合点按序尝试 JSON 解析，解析成功即为真闭合（纯机械重试）
  - JSON 解析失败：整块跳过，记 warning，不影响正文
  - 只认 title / parent / content 三个字段；多余字段忽略
  - title 空 → 跳过该项

apply_proposals(work_id, current_card_id, proposals, exclude_titles=None) -> (created, denied)
  - 逐项调 create_card(source="模型生成")；content 落存储层正文区
  - parent 缺失/不存在回退 current_card_id
  - 连线不经建卡协议（link_cards 工具单独走）
  - **[建卡工具化 2026-10-08][权限收口 2026-10-09] 权限（代码现状口径）＝
    只能建自己的子卡片**：文本块里的 parent 同走统一关口（CREATE_CHILD），
    越权项不建、记入 denied（机械事实随created 一并回给调用方——不静默丢卡）
  - **exclude_titles**：同轮工具通道已建的卡名集合，标题命中者跳过（跨通道去重，
    防同一轮既发 create_card 工具又写文本块 → 建两次）

⚠️ **返回值破坏性变更**：旧签名返回 list[created]，新签名返回
    (created, denied) 二元组。调用方仅routes.py:920 一处（已同步改）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from ..permissions import policy
from ..storage import card_store

logger = logging.getLogger("api.card_proposal")

# 形态一：fenced 开标记
_FENCE_OPEN_RE = re.compile(r"```card-proposal\s*\n")
# fenced 候选闭合：行首三反引号（含前导换行，共 4 字符）
_FENCE_CLOSE_RE = re.compile(r"\n```")
# 形态二：尖括号标记（竖线全角/半角、单/双/零条、标记词两侧空白均可；
#   竖线是 DeepSeek 特殊 token 的可打印近似，条数与有无均随机——10-03 10:56 实录
#   纯 ASCII <card-proposal>…</card-proposal> 零竖线（泄漏家族第五形态），故按 0-2 条全覆盖；
# 闭合标记两种泄漏变体都接：竖线在前 <|/card-proposal|> 或斜杠在前 </card-proposal|>）
_BLOCK_ANGLE_RE = re.compile(
    r"<[|｜]{0,2}\s*card-proposal\s*[|｜]{0,2}>"
    r"(.*?)"
    r"(?:<[|｜]{0,2}\s*/card-proposal\s*[|｜]{0,2}>|</card-proposal\s*[|｜]{0,2}>)",
    re.DOTALL,
)


def _extract_fenced_spans(reply_text: str) -> list[tuple[int, int, str]]:
    """fenced 块提取：对每个候选闭合点按序尝试 JSON 解析，成功者为真闭合。

    返回 [(start, end, raw_body)]。无任何闭合点时取余文（随后解析失败会留 warning）。
    """
    spans: list[tuple[int, int, str]] = []
    pos = 0
    while True:
        m = _FENCE_OPEN_RE.search(reply_text, pos)
        if not m:
            break
        start, body_start = m.start(), m.end()
        closes = [c.start() for c in _FENCE_CLOSE_RE.finditer(reply_text, body_start)]
        chosen: tuple[int, int, str] | None = None
        for c in closes:
            raw = reply_text[body_start:c]
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                continue
            chosen = (start, c + 4, raw)  # 闭合含 "\n```" 四字符
            break
        if chosen is None:
            if closes:
                c = closes[-1]
                chosen = (start, c + 4, reply_text[body_start:c])
            else:
                chosen = (start, len(reply_text), reply_text[body_start:])
        spans.append(chosen)
        pos = chosen[1]
    return spans


def _repair_control_chars(raw: str) -> str:
    """严格解析失败时的确定性格式修复：字符串字面量内的裸控制字符转义。

    模型偶发把 content 里的换行写成真实换行（JSON 规范外）。只做机械转义，
    不改任何内容字符。仅当 json.loads 严格失败时才尝试。
    """
    out: list[str] = []
    in_str = False
    esc = False
    for ch in raw:
        if in_str:
            if esc:
                out.append(ch)
                esc = False
            elif ch == "\\":
                out.append(ch)
                esc = True
            elif ch == '"':
                out.append(ch)
                in_str = False
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
    return "".join(out)


def _parse_items(raw: str, proposals: list[dict[str, Any]]) -> None:
    """解析一个块的 JSON，产出 proposals 项。失败整块跳过留 warning。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        repaired = _repair_control_chars(raw)
        if repaired == raw:
            logger.warning("card-proposal 块 JSON 解析失败，整块跳过")
            return
        try:
            data = json.loads(repaired)
            logger.info("card-proposal 块经控制字符修复后解析成功")
        except json.JSONDecodeError as exc:
            logger.warning("card-proposal 块 JSON 解析失败（含修复尝试），整块跳过: %s", exc)
            return
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            logger.warning("card-proposal 项缺 title，跳过: %s", item)
            continue
        proposals.append({
            "title": title,
            "parent": (item.get("parent") or "").strip(),
            "content": (item.get("content") or "").strip(),
        })


def parse_proposals(reply_text: str) -> tuple[str, list[dict[str, Any]]]:
    """提取所有 card-proposal 块（双形态），返回 (clean_text, proposals)。"""
    proposals: list[dict[str, Any]] = []
    angle_spans = [(m.start(), m.end(), m.group(1))
                   for m in _BLOCK_ANGLE_RE.finditer(reply_text)]
    fenced_spans = _extract_fenced_spans(reply_text)
    # 去重：fenced 块完全落在某个 angle 块内部（内容嵌套）时跳过
    fenced_spans = [
        s for s in fenced_spans
        if not any(a[0] <= s[0] and s[1] <= a[1] for a in angle_spans)
    ]
    all_spans = sorted(angle_spans + fenced_spans, key=lambda s: s[0])
    for _, _, raw in all_spans:
        _parse_items(raw, proposals)
    clean_text = reply_text
    for start, end, _ in sorted(all_spans, key=lambda s: s[0], reverse=True):
        clean_text = clean_text[:start] + clean_text[end:]
    # 清理由剥离产生的多余空行（连续 3+ 换行压成 2）
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text).strip() + "\n"
    return clean_text, proposals


async def apply_proposals(
    work_id: str,
    current_card_id: str,
    proposals: list[dict[str, Any]],
    exclude_titles: Optional[set[str]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """逐项调 create_card(source='模型生成')。parent 缺失/不存在回退 current_card_id。

    **[建卡工具化 2026-10-08][权限收口 2026-10-09] 两处新增**：
    ① 权限（代码现状口径）＝只能建自己的子卡片。判据统一走 policy.check
       （parent 分类须授 CREATE_CHILD）——与 create_card 工具通道同一关口，
       不新增第二套规则。越权项不建，记入 denied（title + reason），
       随返回值可见，不静默丢。
    ② exclude_titles：同轮工具通道已建卡名，命中者跳过（跨通道去重）。

    返回 (created, denied)：
      created = [{"card_id","title","parent"}]成功建卡列表
      denied  = [{"title","reason"}] 因越权或去重未建的项
    单项失败记日志继续下一项（原语义不变）。
    """
    from . import card_tools  # 延迟导入：与 card_tools 互为反向引用，顶层并列导入会成环

    created: list[dict[str, Any]] = []
    denied: list[dict[str, str]] = []
    skip_titles = exclude_titles or set()
    # 权限判定需要全卡 meta（parent 链）；只取一次，供整批复用
    cards = card_tools._collect_card_meta(work_id)
    for p in proposals:
        title = p["title"]
        # 跨通道去重：工具通道同轮已建同名的，不重复建
        if title in skip_titles:
            logger.info("[建卡去重] 工具通道同轮已建同名卡，文本块跳过 title=%r", title)
            denied.append({"title": title, "reason": "同轮工具通道已建同名卡（去重）"})
            continue
        parent = p.get("parent") or ""
        if parent == "":
            parent = current_card_id
        # 存在性先判：已删/不存在的 parent 不误报越权（与 card_tools 同序）
        if parent not in cards:
            denied.append({"title": title, "reason": f"父卡不存在: {parent}"})
            logger.warning("[建卡未建] 父卡不存在 title=%r parent=%r", title, parent)
            continue
        # 权限＝关口 CREATE_CHILD 判定（任务书 T3/交办单：统一 check）。
        # 拒绝文案沿用原文（本批只机械替换判据本体）；文案中「子树」表述与
        # 新判据（分类位）存在文面张力，已在回报中呈报待用户定文案。
        try:
            policy.check(parent, policy.Perm.CREATE_CHILD, cards)
        except PermissionError:
            reason = f"越权：只能建自己的子卡片，{parent} 不在 {current_card_id} 的子树内"
            denied.append({"title": title, "reason": reason})
            logger.warning("[建卡未建] %s title=%r parent=%r", reason, title, parent)
            continue
        try:
            result = await card_store.create_card(
                work_id,
                title,
                "模型生成",
                parent=parent,
                conclusion=p.get("content", ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("apply_proposals 单项失败 title=%s: %s", title, exc)
            continue
        if "error" in result:
            logger.error("apply_proposals 单项失败 title=%s: %s", title, result["error"])
            continue
        meta = result["meta"]
        created.append({
            "card_id": meta["card_id"],
            "title": meta["title"],
            "parent": meta["parent"],
        })
    return created, denied
