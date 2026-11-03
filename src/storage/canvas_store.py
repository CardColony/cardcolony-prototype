# -*- coding: utf-8 -*-
"""canvas.json 缓存维护。canvas.json 是可重建缓存，卡片文件才是真身。

Phase 7：每个 work 各有一份 canvas.json，路径 = works/{work_id}/canvas.json。
[画布重构 2026-10-15] canvas.json **不再持有 positions**（也不再有 layout.json）：
卡片坐标是**卡片自身属性**，直接住在 cards 表的 ``pos_x``/``pos_y``/``pinned``
三列上（见 ``card_db.POSITION_COLUMNS``），由 ``load_positions`` /
``merge_positions`` 读写。于是 canvas.json 回到「纯派生缓存」这一件事：
    {
      "project_name": "...",
      "created_at": "...",
      "cards": [...],
      "links": [...]
    }

写入失败只记日志，绝不抛出中断调用方，绝不删除任何文件。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from .hash_utils import parse_front_matter, split_body_sections
from . import card_db

logger = logging.getLogger("storage.canvas")

# ============================================================
# [画布第二步 D5] 拆存储长期不变量（L1–L8）—— [画布重构 2026-10-15] L3/L4/L6
# 随 layout.json 退役改写，其余逐字保留（它们约束的是"人给的位置"这件事，
# 与位置存在哪儿无关；换存储不等于换纪律）。
#
# L1 用户布局只由人给——任何系统推算（避让/仲裁/自动排布）都不许写进
#    cards 表的坐标列；系统算的位置只用于显示、只活在内存里。
# L2 canvas.json 可随时删除、自动重建、用户无感（验收实证：删文件→GET→
#    画布全开、钉住坐标原样）。
# L3 ★ 已改写：cards 表是用户坐标的**唯一真相来源**。它丢了＝用户布局永久
#    丢失、不可重建——保护靠快照（data_snapshot 的 PROTECTED_DIRS 含
#    works/，cards.db 自动入快照），**不靠 rebuild**（rebuild 救不了它，
#    也不许假装能救）。反过来说：位置不再有第二个存放点，删掉 layout.json
#    这条路是被**封死**的，不是"顺手清理"。
# L4 ★ 已改写：用户字段（坐标）与派生字段（cards/links/
#    conclusion_summary 等）彻底分家——用户在 cards 表的列上，派生数据在
#    canvas.json 的键上，两者不再共处一个文档，故"不许互相混写"由类型系统
#    与查询条件各自把关。
# L5 对外接口不变：GET /canvas 响应仍含 positions 字段、PATCH /positions
#    端点与 merge_positions 签名原样——前端零改动是拆存储的边界条件。
#    ★ [画布重构] 存储换了、HTTP 契约一字未动，这是本次改动的硬边界。
# L6 ★ 已改写：坐标列稀疏——只记用户表态过的卡（拖过/钉过/整理过）：
#    未摆过的卡 pos_x/pos_y 恒为 NULL，**不建空记录**（自动排每次现算、
#    不落盘，任务书约束 4）。
# L7 pinned 与 x/y 同居一张卡的同一行，不许分离存储、也不许分表——三者
#    一次 UPDATE 到位，不存在"搬了一半"的中间态。
# L8 任一数据缺失或损坏都不阻断画布打开：load/rebuild/load_positions 全部
#    fail-open（返空/重建），绝不抛出中断调用方。
# ============================================================


BASE_DIR = Path(__file__).resolve().parents[2]
WORKS_DIR = BASE_DIR / "works"
CARDS_DIR = BASE_DIR / "cards"        # 旧路径（兼容）
CANVAS_PATH = BASE_DIR / "canvas.json"  # 旧路径（兼容）

PROJECT_NAME = "推演agent"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _canvas_path(work_id: str) -> Path:
    return WORKS_DIR / work_id / "canvas.json"


# [R4] 结论文本摘要上限——与前端 canvas.js 的 getSummary 逐字一致
# （前端原文：firstLine.length > 120 ? firstLine.slice(0,120) + "…" : firstLine）
SUMMARY_MAX = 120


#: 新建卡时播种的模板占位文案（现状块里出现这些片段 ⇒ 视为"还没写"）。
#: 与 card_draft._PLACEHOLDER_MARKERS 同源语义，但本模块不 import api 层
#: （存储不依赖上层），故各自持有——两处都只做"是否占位"的机械判断。
_PLACEHOLDER_MARKERS = (
    "（当前状况快照", "(当前状况快照",
    "做到哪、什么状态、卡在哪",
)

#: **元信息行**：写的是"这张卡的记录格式"，不是内容。
#: 真实数据里每张卡的现状块第一行都是「状态：现状实测（2026-10-03）…依据：xxx.html(258行)」，
#: 原样当摘要 → 画布上每张卡都写着"状态：现状实测…"，用户扫一眼什么也读不到。
#: 这类行一律跳过，继续找有信息量的正文。
_META_LINE_PREFIX = (
    "状态：", "状态:", "依据：", "依据:", "说明：", "说明:",
    "本卡", "本文", "范围：", "范围:", "信源：", "信源:",
    "立项卡", "建卡于", "父卡：", "父卡:",
)


def _strip_md_prefix(line: str) -> str:
    """剥掉行首的 Markdown 结构标记：标题井号、引用、列表符、加粗包裹。

    为什么必须剥：真实项目的现状块第一行常常是 `# 子项目` / `- 完成 X`，
    原样当摘要会让画布上每张卡都写着"# 子项目"——既无信息量又像半成品。
    """
    t = line.strip()
    t = re.sub(r"^#{1,6}\s*", "", t)          # # 标题
    t = re.sub(r"^>\s*", "", t)               # > 引用
    t = re.sub(r"^([-*+]\s+|\d+\.\s+)", "", t)  # - 列表 / 1. 有序
    t = t.replace("**", "").replace("__", "")  # 加粗标记
    return t.strip()


def _conclusion_summary(status_block: str, title: str = "") -> str:
    """取现状块（status_block 列）第一行**有信息量的正文**，超 120 字截断。

    [画布商业化 2026-10-17] 三处收紧（原实现取第一个非空行原样返回）：
      ① 剥 Markdown 前缀（`# 子项目` → `子项目`）——否则画布上一片"# xxx"；
      ② 只有标题符号、剥完是空的行**跳过**，继续找下一行；
      ③ 模板占位文案视为"还没写"→ 返回空（卡面不显示摘要，而不是显示占位说明）；
      ④ **与卡标题相同的行跳过**——真实数据里现状块第一行常是 `# <卡标题>`，
         剥完井号后与标题逐字相同，卡面就成了"标题下面再写一遍标题"。
    [卡片信息结构改造 S2-11/G-7] canvas.json 字段名 conclusion_summary **不变**。
    """
    t_norm = (title or "").strip()
    lines = (status_block or "").split("\n")
    # 两轮扫描：先取**正文行**（不以 # 开头），没有再退回标题行。
    # 真实现状块常是 `# 卡名` → `## 小节` → 正文，只取第一个非空行会永远拿到标题。
    for want_heading in (False, True):
        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            if any(m in raw for m in _PLACEHOLDER_MARKERS):
                return ""
            is_heading = raw.startswith("#")
            if is_heading != want_heading:
                continue
            s = _strip_md_prefix(raw)
            if not s:
                continue
            if t_norm and s == t_norm:
                continue
            if s.startswith(_META_LINE_PREFIX):
                continue   # 元信息行（"状态：现状实测…""依据：xxx"）不是内容
            # 纯小节标记（"【思维链收口】""【待裁定】"）是结构，不是内容
            if re.fullmatch(r"【[^】]{1,12}】", s):
                continue
            # 自述"无结论"的元信息
            if "无结论" in s or "未结论" in s:
                continue
            # 建卡记录（"2026-10-07 用户令「建一张子卡」…"）是历史，不是这段在讨论什么
            if re.match(r"^\d{4}-\d{2}-\d{2}", s):
                continue
            # 字段行（"计划名: xxx""终态: 已收口""目标: xxx"）——思维链收口的元数据
            if re.match(r"^(?:计划名|终态|目标|轮次|进度|下一步|耗时|模型|席位)\s*[:：]", s):
                continue
            # 空壳（"边界规则：" 后面什么都没有）
            if s.endswith(("：", ":")):
                continue
            # 行尾元信息残留（"…。父卡 card_001。"）
            s = re.sub(r"[。;；]?\s*父卡\s*card_\d{3}[。.]?$", "", s).strip()
            if raw.startswith("|") or set(s) <= set("-: "):
                continue   # Markdown 表格行/分隔行（"| 层 | 载体 | 脚本 |"）不是结论
            return (s[:SUMMARY_MAX] + "…") if len(s) > SUMMARY_MAX else s
    return ""
    if len(first_line) > SUMMARY_MAX:
        return first_line[:SUMMARY_MAX] + "…"
    return first_line


#: 「等用户裁定」的标记。这个项目的工作方式是：模型提议一律标【待裁定】，
#: 用户点头才算数（见 cards 表 requirements 列与各卡正文）。
#: ⇒ 画布上**最该突出的是"哪里在等你裁定"**，不是"哪张卡拖得好看"。
_PENDING_RE = re.compile(r"待裁定|【待定】|待确认")

#: 结论文本：`结论：xxx` / `**结论**：xxx` / `- 结论: xxx` / `裁定：xxx`
_CONCLUSION_RE = re.compile(
    r"^[\s>*\-]*(?:\*\*)?(?:结论|裁定)(?:\*\*)?\s*[:：]\s*(.+)$",
    re.MULTILINE,
)


#: 单条待裁定文本的展示上限（卡片上只有两行位置，超了没意义）
PENDING_ITEM_MAX = 72


_HEADING_RE = re.compile(r"^(#{2,6})\s+(.*)$")

#: 小节内以这些字符开头的行是**说明**，不是待办项
_NOTE_PREFIX = ("（", "(", "〔", "【注", "注：", "注:", "说明", ">", "上述", "以上", "本节")


def _extract_pending_items(status_block: str, limit: int = 99) -> list[dict[str, str]]:
    """从「待裁定」小节里取出**条目**，返回 [{section, text}]。

    [画布 2026-10-18 · 宁可留空，不猜] 前一版按"行内含待裁定"猜，实测在真实卡上
    三处全错：把卡的定义句（"承载「工具层」的实读事实与待裁定："）当成待办、
    把说明句（"凡标【待裁定】者为模型提议"）当成待办、把表格行当成待办。
    猜错比留空更糟——用户会误信。

    真实数据的结构（39 张卡逐张实读）是用户**自己划的小节**：
        ## 四、待裁定（须用户拍板，非本卡结论）
        1. 卡的身份是否改为与位置解耦的稳定 id。
        2. `card_...` 的...
        ### 11.6 待裁定（默认即执行，不阻塞）
        1. 拼接顺序与分隔 → 默认「分析在前、回执在后」。

    所以按"小节"取：小节标题含「待裁定」→ 该节下的列表项/编号项就是待办；
    节结束于下一个同级或更高级标题。另外补一层：行内含【待裁定】标记的列表项
    （8 张卡用的是行内标记），但要排除说明句。
    """
    lines = (status_block or "").split("\n")
    items: list[dict[str, str]] = []
    seen: set[str] = set()

    def push(section: str, text: str) -> bool:
        t = text.strip(" ：:，,。;；-—·")
        if len(t) < 4:
            return False
        # 兜底闸：任何以日期开头的追记/表格行都不算待办（实测它们会从多条路径漏进来）
        if re.match(r"^\d{4}-\d{2}-\d{2}", t) or t.startswith("|"):
            return False
        key = t[:24]
        if key in seen:
            return False
        seen.add(key)
        items.append({"section": section, "text": t[:PENDING_ITEM_MAX] + ("…" if len(t) > PENDING_ITEM_MAX else "")})
        return len(items) >= limit

    # ── 第一轮：按「待裁定」小节取 ──
    section = ""
    level = 0
    inside = False
    for raw in lines:
        st = raw.strip()
        if not st:
            continue
        m = _HEADING_RE.match(st)
        if m:
            h_level = len(m.group(1))
            h_text = _strip_md_prefix(st)
            # ⚠ 必须是「以待裁定**为主**」的小节，不是"标题里提到待裁定"。
            # 实测踩过：`## 六、用户提出：回复与上一句脱节（诊断记录 · 待裁定 · 归属待定）`
            # 标题里含"待裁定"，把下面整段诊断记录都算成了待办（一张卡报出 99 条）。
            # 判据：去掉编号后**以「待裁定」开头**（含【待裁定】/〔待裁定〕形式）。
            h_core = re.sub(r"^[\d一二三四五六七八九十]+(?:[.、．]\d+)*[.、．]?\s*", "", h_text).strip()
            is_pending_heading = bool(re.match(r"^[【\[]?[〔(]?待裁定", h_core))
            if is_pending_heading:
                inside, level, section = True, h_level, h_text
            elif inside and h_level <= level:
                inside = False
            continue
        if not inside:
            continue
        # ⚠ 先剥掉列表符再判类型：`- （新增，见 §七）Q1…` 以 "-" 开头，
        # 直接 startswith("（") 判不出来，会被当成条目（实测漏进来 10 条）。
        bare = re.sub(r"^(?:[-*+]\s+|\d+[.、]\s+)", "", st).strip()
        if bare.startswith(_NOTE_PREFIX):
            continue
        if bare.startswith("|"):          # Markdown 表格行
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}", bare):   # 日期开头的追记
            continue
        is_item = bool(re.match(r"^(?:[-*+]\s+|\d+[.、]\s+)", st))
        text = _strip_md_prefix(st)
        text = re.sub(r"[【\[]\s*待裁定\s*[】\]]", "", text)
        if not is_item and len(text) > 90:
            continue          # 小节里的长叙述不是条目
        if push(section, text) :
            return items

    # ── 第二轮：行内【待裁定】标记（排除说明句）──
    for raw in lines:
        st = raw.strip()
        if "【待裁定】" not in st:
            continue
        if st.startswith("#"):
            continue
        if re.search(r"(?:凡|均|一律|都)\s*标.{0,8}待裁定|待裁定\s*者|标【待裁定】", st):
            continue
        if st.startswith("|"):
            continue
        text = _strip_md_prefix(st)
        text = re.sub(r"[【\[]\s*待裁定\s*[】\]]", "", text).strip(" ：:，,。")
        if push("（行内标记）", text):
            return items

    return items


#: 需求文档条目语法（card_store.NEW_CARD_SKELETON 定义）：`D01【裁定】内容（日期·依据）`。
#: 这是**系统自己规定的结论格式**，不是我从自由文本里猜的：
#:   使命 = 立项定稿 / 现状 = 覆盖写快照 / 需求文档 = 只追加的决定
#: 卡片对外的唯一接口就是这三块，其中"想清楚了什么"全部落在需求文档里。
#: ⚠ 骨架里的示例行 `D01【裁定】……（日期·依据）` **不是需求文档**——它是模板占位。
#: 用 `(?!…)` 把以省略号开头的行排除掉，否则每张新建卡一出生就算"有 1 条需求文档"，
#: 画布上会显示 `定1`、收口闸也会被它骗过去（实测踩过）。
_DECISION_RE = re.compile(r"^D(\d{2})【(裁定|决定|收敛)】\s*(?!…|\.\.\.)(.+?)\s*$", re.M)


def _last_message(work_id: str, card_id: str) -> dict[str, str] | None:
    """这张卡会话里的**最后一条消息**（谁说的、说了什么、什么时候）。

    [画布 2026-10-19 · 用户令] "把实时最新更新的对话信息状态直接拉到卡片上"。
    卡片本身就是对话单元（每张卡自带 session），所以卡面最该显示的不是正文，
    而是**这张卡刚刚在说什么**——那是唯一会实时变、且能让人立刻判断"要不要插手"的东西。

    [重建重做 2026-11-03] 实现委托给 ``session_facade.last_message``——
    尾部扩窗反扫、不读全文。改前这里 ``path.read_text()`` 整读+全解析：
    实测一个 work 的会话总量 900MB，每次重建把它们挨个吞一遍。
    """
    from . import session_facade as _sf
    return _sf.last_message(work_id, card_id)


#: 活动流噪音过滤（实测 381 行历史里两类占 182 条）：
#:   [权限刷新] perms → v4   批量刷权限，不是工作
#:   [T7存量迁移] 线索 …      一次性迁移
#: 真正有信息量的是"卡片创建""由「X」对话编辑正文"。
_ACTIVITY_NOISE = ("%[权限刷新]%", "%[T7存量迁移]%")


def recent_activity(work_id: str, limit: int = 40) -> list[dict[str, str]]:
    """这个 work 最近发生的**工作事件**（活动流 / 接续提示的唯一数据源）。

    [画布 2026-10-19] 抽成公开函数：投影（canvas.json）与实时脉搏（/pulse）
    都要它。两处各写一遍正则，就会在"哪天改了过滤规则"时分叉——一处滤掉噪音、
    另一处把噪音当活动播报，用户看到的是"权限刷新了 150 次"。
    """
    def _fn(conn: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(
            # INNER JOIN：卡已删除的历史事件不显示（实测有 6 条指向已删卡，
            # 卡片名只能回退成 card_079，读起来像乱码）
            "SELECT h.card_id, h.ts, h.text, c.title "
            "FROM card_history h JOIN cards c ON c.card_id = h.card_id "
            "WHERE h.text NOT LIKE ? AND h.text NOT LIKE ? "
            "ORDER BY h.ts DESC LIMIT ?",
            (*_ACTIVITY_NOISE, int(limit)),
        )]

    out: list[dict[str, str]] = []
    for e in (card_db.read_sync(work_id, _fn, []) or []):
        txt = str(e.get("text") or "")
        # 历史行格式固定：`- [2026-10-18T11:27:01.576270+08:00] 卡片创建`
        m = re.match(r"^\s*-\s*\[[^\]]+\]\s*(.+)$", txt)
        detail = (m.group(1) if m else txt).strip()
        out.append({
            "card_id": e.get("card_id") or "",
            "title": e.get("title") or e.get("card_id") or "",
            "ts": e.get("ts") or "",
            "detail": detail[:80],
        })
    return out


def _parse_decisions(status_block: str) -> list[dict[str, str]]:
    """抽出一张卡的需求文档条目（最多保留最近 20 条）。

    [画布 2026-10-19] 之前画布上那段"结论"是拿正则从**自由文本**里抠 `结论:` 行，
    39 张卡只抠到 5 张，且抠出来的是现状块开头的一句话——那不是结论。
    需求文档不同：它是系统规范里明确规定的、只追加的决定条目，天生短、自包含。
    """
    out: list[dict[str, str]] = []
    for m in _DECISION_RE.finditer(status_block or ""):
        out.append({"id": f"D{m.group(1)}", "kind": m.group(2), "text": m.group(3).strip()})
    return out[-20:]


def _card_work_state(status_block: str, title: str = "") -> tuple[str, int, str, str]:
    """从卡片内容推断「这张卡现在是什么工作状态」，返回 (state, pending_count, conclusion)。

    [画布 2026-10-18 · 从"白板"改回"工作台"] 卡片上原本显示的是现状块第一行
    （"状态：现状实测（2026-10-03）…依据：templates/index.html(258行)…"），
    那是给模型看的记录格式，不是给人看的结论。用户扫一眼画布，看不出
    哪张卡有判断、哪张卡在等他、哪张卡还没动。

    四态（按"用户要不要行动"排序）：
      pending    含【待裁定】——**用户必须亲自处理**，画布要醒目
      concluded  有「结论：」——这张卡想清楚了，可以引用
      discussing 有内容但没结论——还在讨论
      empty      现状块为空——还没开始
    """
    sb = status_block or ""
    if not sb.strip():
        return "empty", 0, ""

    items = _extract_pending_items(sb)
    pending = len(items)
    # explicit＝正文里**显式写了**「结论：」——只有这种才算"想清楚了"。
    # 摘要（第一行正文）只是"这张卡在讲什么"，不能当成结论，否则每张有字的卡
    # 都会标成"已结论"，状态就失去意义。
    # 显式「结论：」才算"想清楚了"。但真实数据里有一批空壳：
    #   "汇总："（只有冒号）、"全部内容为【提议】，无结论"（这是元信息）。
    # 空壳比留空更糟——用户会以为这张卡有结论。故加两条硬过滤。
    explicit = ""
    for m in _CONCLUSION_RE.finditer(sb):
        cand = m.group(1).strip().replace("**", "").replace("`", "").strip(" ：:，,。")
        if len(cand) < 8:
            continue                     # "汇总：" 这类空壳
        if "无结论" in cand or "未结论" in cand:
            continue                     # "全部内容为提议，无结论"
        if cand.startswith(_META_LINE_PREFIX):
            continue
        explicit = cand
        break
    text = explicit or _conclusion_summary(sb, title)
    # [画布 2026-10-19] **这段文字有两种来源，必须区分开**：
    #   explicit → 卡里真有「结论：」段落，这是"想清楚了什么"
    #   fallback → 没有结论，取现状块第一行正文，这只是"这张卡在讲什么"
    # 两者原来塞进同一个字段、在卡面上长得一模一样，用户直接问"这段文字是什么信息"。
    # 现在把来源一起带出去，前端分别标注（结论 / 现状），读者才认得出来。
    kind = "explicit" if explicit else ("fallback" if text else "")

    if pending:
        return "pending", pending, text, kind
    if explicit:
        return "concluded", 0, text, kind
    return "discussing", 0, text, kind


#: canvas.json 投影结构版本。**加/删/改投影字段、以及任何改变字段内容的算法，必须 +1**。
#: 踩过的坑：改了待裁定提取规则但没升版本 ⇒ 读到旧缓存，后端算出 100 条、界面显示 81 条。
#:
#: 为什么必须有这个版本号：canvas.json 是缓存，``load()`` 命中就直接返回，
#: 从不比对结构。实测缺口——给投影加了 ``state`` / ``updated_at`` 之后，
#: 存量 work 的旧 canvas.json 里没有这两个键，``load()`` 原样返回 ⇒ 画布上
#: 「已完成」徽标与更新时间**永远不出现**，且没有任何报错。版本号让
#: "缓存是旧结构的"这件事变成可判定的事实，命中时自动重建。
CANVAS_SCHEMA_VERSION = 17


def rebuild(work_id: str) -> dict[str, Any]:
    """全量重建 work 的 canvas.json 并写盘，返回结果字典。

    [画布第二步 D3] 拆存储收口：canvas.json 是**纯派生缓存**（不变量 L2——
    可随时删除、自动重建、用户无感），rebuild 不再抢救/写入 positions；
    [画布重构 2026-10-15] 用户坐标的唯一真相来源是 **cards 表的坐标列**
    （load_positions/merge_positions），canvas.json 里连空 ``positions``
    键都不再生成。
    保留旧 canvas.json 的 created_at（既有行为，不动）。
    [卡片存储迁移 SQLite] 数据源从**卡片 md 目录**改为库内 ``cards`` 表
    （canvas.json 本身仍为文件——画布线，不在迁移范围）。
    """
    canvas_path = _canvas_path(work_id)
    # [重建重做 2026-11-03] 脏标记在**开读前**清掉，不是写完后：
    # 重建期间落的新写会重新置脏 ⇒ 下一次 load 再补一次；
    # 反过来（写完才清）会把重建期间的新标记吞掉，缓存停在旧代。
    _dirty_path(work_id).unlink(missing_ok=True)
    created_at = _now_iso()
    try:
        old = json.loads(canvas_path.read_text(encoding="utf-8"))
        if isinstance(old, dict):
            if old.get("created_at"):
                created_at = old["created_at"]
    except Exception:
        pass

    def _fn(conn: Any) -> list[dict[str, Any]]:
        # [依赖关联线 2026-10-16] 画布本期只用**通信线**：原 relations 列已改名
        # links（新线 relations 不进画布——用户裁定「画布展示本次不做」）。
        return [dict(r) for r in conn.execute(
            "SELECT card_id, title, card_type, parent, created_at, updated_at, state,"
            " links, status_block, requirements,"
            # [画布 2026-10-18 · 自动模式可视化] 这五列决定"AI 正在怎么用这套系统"：
            # auto_mode＝是否自动推进；goal_state＝跑/停/成；goal_rounds＝第几轮；
            # toolcard_step/round＝当前步骤。真实项目里目前全空（自动模式一次没跑过），
            # 但字段在、机制在，画布必须先有表达，否则这个核心能力永远不可见。
            " auto_mode, goal_state, goal_rounds, toolcard_step, toolcard_round"
            " FROM cards ORDER BY card_id"
        )]

    rows = card_db.read_sync(work_id, _fn, []) or []

    # ── 最近活动（活动流的数据源）──
    # [画布 2026-10-19] 实现抽到 recent_activity()：/pulse 实时口与投影共用同一份，
    # 避免两处过滤规则分叉（详见函数注释）。
    recent_events = recent_activity(work_id, 40)

    # [画布 2026-10-18 · 逻辑关系] 卡片之间的真实关系不在 relations/links 列里
    # （实测这两列全空），而在**正文**里：用户写卡时会引用邻卡
    # （"card_002「上下文记忆系统」：上下文由什么组成…"）。全库实测 115 条引用边，
    # card_002 被 11 张卡引用——它是这个项目的地基。
    # 这里先算「被引用次数」随投影下发：卡片上标一个 ←11，一眼看出哪几张是核心。
    # （全部 115 条线同时画出来是一团乱麻，故线只在悬停/选中时显示。）
    _ref_counter: dict[str, int] = {}
    for _r in rows:
        for _m in set(re.findall(r"card_\d{3}", _r["status_block"] or "")):
            if _m != _r["card_id"]:
                _ref_counter[_m] = _ref_counter.get(_m, 0) + 1

    cards: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    for row in rows:
        card_id = row["card_id"]
        # [角色机制 5710] 角色卡离树，不进工作画布（它在左栏单列/角色页里活）。
        if str(row["card_type"] or "") == "role":
            continue
        # 需求文档条目：一张卡解析一次，下面 count 与 last 共用
        # 需求文档可能落在两个字段里：老卡把整篇正文塞进 status_block（含 ## 需求文档 段），
        # 新卡走三列模型，需求文档在独立的 requirements 列。两处都扫，否则换一种写法就看不见。
        _decisions = _parse_decisions(
            (row["status_block"] or "") + "\n" + (row["requirements"] or ""))
        cards.append(_card_entry(work_id, row, _decisions,
                                 _ref_counter.get(card_id, 0)))
        try:
            rels = json.loads(row["links"] or "[]")
        except (TypeError, ValueError):
            rels = []
        for rel in (rels or []):
            if isinstance(rel, dict):
                item = {"source": card_id, "target": rel.get("target", ""),
                        "type": rel.get("type", "")}
                if rel.get("note"):
                    item["note"] = rel["note"]
                links.append(item)

    # [R4-3-5] 重建打点（沿用既有 logger，不参与分支、不设闸）
    summary_count = sum(1 for c in cards if c.get("conclusion_summary"))
    logger.info("canvas 重建：%d 卡，含摘要 %d 条", len(cards), summary_count)

    result = {
        "project_name": PROJECT_NAME,
        "work_id": work_id,
        "schema_version": CANVAS_SCHEMA_VERSION,
        "created_at": created_at,
        "recent_events": recent_events,
        "cards": cards,
        "links": links,
    }
    try:
        canvas_path.parent.mkdir(parents=True, exist_ok=True)
        # [重建重做 2026-11-03] 原子写：tmp＋os.replace——读方不再可能
        # 读到写了一半的 canvas.json（旧路径 write_text 直覆，读到一半
        # 会触发调用方再重建一次的浪费）。写完才清脏标记：写失败
        # canvas.dirty 留着，下一次 load 重试。
        _tmp = canvas_path.with_name("canvas.tmp")
        _tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        os.replace(_tmp, canvas_path)
    except Exception as exc:
        logger.warning("canvas.json 写入失败（不中断）: %s", exc)
    return result


def _card_entry(work_id: str, row: dict[str, Any],
                decisions: list[dict[str, Any]],
                ref_count: int) -> dict[str, Any]:
    """一卡行的投影条目——``rebuild``（全量）与 ``card_projection``（单卡现算）
    的**唯一构造口**：两条路径产出同一形状，事件载荷与 /canvas 首屏不可能分叉。
    """
    card_id = row["card_id"]
    _decisions = decisions
    return {
        "card_id": card_id,
        "title": row["title"] or "",
        "card_type": row["card_type"] or "线索",
        "parent": row["parent"] or "",
        "created_at": row["created_at"] or "",
        # [画布商业化 2026-10-17] 状态与更新时间随画布一次到手：
        #   状态用于卡片视觉分层（已完成/进行中），更新时间用于卡面底部元信息。
        #   两者都是纯增量字段——旧前端不读即不受影响。
        "state": row["state"] or "",
        "updated_at": row["updated_at"] or "",
        # [R4] 结论摘要随画布一次到手——前端 getSummary 优先读本字段，
        # 免去首屏对每张可见卡各发一次 getCard 请求（N+1）。
        "conclusion_summary": _conclusion_summary(row["status_block"] or "", row["title"] or ""),
        # [画布 2026-10-18 · 工作台] 工作状态三件套：状态 / 待裁定数 / 结论。
        # 卡片按 work_state 上色与排序，顶栏据此算项目概览。
        **dict(zip(("work_state", "pending_count", "conclusion", "conclusion_kind"),
                   _card_work_state(row["status_block"] or "", row["title"] or ""))),
        # [画布 2026-10-19] 需求文档：系统规范里的"决定"条目。
        # 卡面只显示**条数**（一个完整、可比较的事实：9 条＝这条线已经收敛，
        # 0 条＝还在打转），条目正文给浮层——那里有空间把它读完。
        # 最后一条对话消息：卡面实时显示"这张卡刚在说什么"
        "last_msg": _last_message(work_id, card_id),
        "decision_count": len(_decisions),
        "last_decision": (_decisions[-1] if _decisions else None),
        # 被引用次数：这个项目里"谁被最多卡引用"＝谁是地基
        "ref_count": ref_count,
        # 待裁定**条目**（不是数字）：卡片上直接列出"在等你的那几件事"
        "pending_items": _extract_pending_items(row["status_block"] or ""),
        # 规模：现状块字数。3000 字和 40000 字的工作量差一个数量级，
        # 它决定"这张卡要不要现在碰"，是用户排优先级的关键输入。
        "content_len": len(row["status_block"] or ""),
        # ── 自动模式活动（画布上"AI 正在干活"的唯一表达）──
        "auto_mode": bool(row.get("auto_mode")),
        "goal_state": row.get("goal_state") or "",
        "goal_rounds": int(row.get("goal_rounds") or 0),
        "toolcard_step": row.get("toolcard_step") or "",
        "toolcard_round": int(row.get("toolcard_round") or 0),
    }


def _ref_count_of(work_id: str, status_blocks: list[str],
                  card_id: str) -> int:
    """``card_id`` 被其它卡现状块点名的次数（``rebuild`` 与 ``card_projection``
    共用口径：全库 status_block 正则扫）。"""
    n = 0
    for text in status_blocks:
        for m in set(re.findall(r"card_\d{3}", text or "")):
            if m != card_id:
                n += 1
    return n


async def update_cache(work_id: str) -> dict[str, Any]:
    """全量重建（线程内跑，避免库读阻塞事件循环）。

    [重建重做 2026-11-03] **写卡路径不再调用它**——见 ``mark_dirty``。
    保留本函数供测试/脚本与「立即要新鲜投影」的少数路径使用。
    """
    return await asyncio.to_thread(rebuild, work_id)


def _dirty_path(work_id: str) -> Path:
    return _canvas_path(work_id).with_name("canvas.dirty")


def mark_dirty(work_id: str) -> None:
    """写卡后置脏：下一次 ``load()`` 会重建一次。

    [重建重做 2026-11-03] 旧设计＝写卡即 ``await update_cache`` 全量重建
    （100 卡 × 会话全文读 ≈ 1GB IO，还挂在写卡路径上同步等）。现在写卡
    只 touch 这个标记文件（约 0.1ms），canvas.json 推迟到**真有人打开
    画布**时才重建——页面加载是稀有事件，重建发生在那里才对。
    标记落盘而不是内存位：进程重启/崩溃后标记还在，不错过重建。
    """
    try:
        p = _dirty_path(work_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch(exist_ok=True)
    except OSError as exc:
        logger.warning("canvas 脏标记失败（不阻断）work=%s: %s", work_id, exc)


def load(work_id: str) -> dict[str, Any]:
    """读 work 的 canvas.json；不存在、解析失败、结构过期或**被写脏**则重建。"""
    canvas_path = _canvas_path(work_id)
    try:
        data = json.loads(canvas_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            # 结构版本不符 ⇒ 旧缓存，重建（不是"缺字段就算了"——缺字段会让前端
            # 静默降级，比报错更难发现）。
            if int(data.get("schema_version") or 0) != CANVAS_SCHEMA_VERSION:
                logger.info("canvas.json 结构版本过期（%s≠%s），触发重建 work=%s",
                            data.get("schema_version"), CANVAS_SCHEMA_VERSION, work_id)
                return rebuild(work_id)
            # 写卡后留的脏标记 ⇒ 缓存过期，重建一次（重建成功自清标记）。
            if _dirty_path(work_id).exists():
                return rebuild(work_id)
            return data
        logger.warning("canvas.json 内容非法，触发重建 work=%s", work_id)
    except FileNotFoundError:
        logger.info("canvas.json 不存在，触发重建 work=%s", work_id)
    except Exception as exc:
        logger.warning("canvas.json 读取失败，触发重建 work=%s: %s", work_id, exc)
    return rebuild(work_id)


# ============================================================
# [画布 5711] 单卡投影摘要 —— 工作级事件的载荷
# ============================================================
# 为什么要有它：画布要"实时更新"，后端就得能把**一张卡**的最新投影推给前端；
# 推整份投影（300 卡 = 24KB）不现实，也不该——前端只需要变更的那一张。
#
# [重建重做 2026-11-03] **现算**，不再借道 canvas.json。旧实现＝
# 写卡 → 全量 rebuild → 读回文件摘一条 ⇒ 推一条 card_changed 要吞近 1GB
# 会话读＋一次 100 卡全扫。现算＝一行卡片＋全库引用计数＋会话尾读，毫秒级；
# 字段与 /canvas **完全同源**——同一个 ``_card_entry`` 构造口，形状不可能分叉。
#
# 零阻断：读不到就返回 None，调用方按"没有摘要"处理（前端保留旧值）。

def card_projection(work_id: str, card_id: str) -> dict[str, Any] | None:
    """现算一张卡的投影条目；卡不存在（已删）或读不到 ⇒ None。"""
    try:
        def _fn(conn: Any) -> tuple[Any, list[str]]:
            row = conn.execute(
                "SELECT card_id, title, card_type, parent, created_at, updated_at,"
                " state, links, status_block, requirements,"
                " auto_mode, goal_state, goal_rounds, toolcard_step, toolcard_round"
                " FROM cards WHERE card_id=?", (card_id,)).fetchone()
            # 引用计数与 rebuild 同口径：全库 status_block 扫一遍。
            blocks = [r["status_block"] or "" for r in
                      conn.execute("SELECT status_block FROM cards")]
            return row, blocks
        row, status_blocks = card_db.read_sync(work_id, _fn, (None, []))
        if row is None:
            return None
        # 角色卡不进画布投影（rebuild 同口径跳过）。
        if str(row["card_type"] or "") == "role":
            return None
        decisions = _parse_decisions(
            (row["status_block"] or "") + "\n" + (row["requirements"] or ""))
        return _card_entry(work_id, dict(row), decisions,
                           _ref_count_of(work_id, status_blocks, card_id))
    except Exception as exc:  # noqa: BLE001 零阻断
        logger.warning("单卡投影读取失败（不阻断）work=%s card=%s: %s",
                       work_id, card_id, exc)
    return None


# ============================================================
# [画布重构 2026-10-15 · 阶段 1.2] 用户坐标存储：layout.json → cards 表
#
# 旧形态（画布第二步 D1/D2）：works/{work_id}/layout.json 存用户坐标，
#   canvas.json 存派生缓存，两文件分家。
# 新形态：位置是**卡片自身属性**，直接住在 cards 表的 pos_x/pos_y/pinned
#   三列上（card_db.POSITION_COLUMNS）——少一个文件、少一次整文档重写、
#   少一个"两份数据对不上"的可能。
#
# 对外接口一个字没动（不变量 L5）：GET /canvas 的响应仍带 positions，
# PATCH /positions 端点与请求/响应体原样，merge_positions 仍是
# `{card_id: {x?, y?, pinned?}}` 进、`{card_id: {x, y, pinned}}` 出。
# 前端零改动。
#
# 【深合并语义保留 · E6/RD-04】载荷可以是**部分字段**（前端"整理画布"发
# 的就是 `{card_id: {pinned: false}}`，没有坐标）。故 UPDATE 只拼载荷里
# 真正出现过的列，没带的列原值不动——不拿「全字段覆盖」去实现，那会把
# 用户摆好的坐标抹成 NULL。
# ============================================================


def _positions_of(rows: Any) -> dict[str, dict[str, Any]]:
    """cards 行 → ``{card_id: {x, y, pinned}}``（形状与旧 layout.json 记录同构）。

    - ``x``/``y``：有值给浮点、无值给 ``None``。前端 ``canvas.js`` 判的是
      ``Number.isFinite(pos.x)``——``None`` 与缺字段同样落 false，于是
      「钉住但没坐标」的卡自动走布局，不会被当成有效坐标（这一支前端早
      就写好了，本函数只是别破坏它）。
    - ``pinned``：给 ``bool``（旧记录里就是 JSON ``true``/``false``）。
    """
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out[r["card_id"]] = {
            "x": r["pos_x"],
            "y": r["pos_y"],
            "pinned": bool(r["pinned"] or 0),
        }
    return out


async def load_positions(work_id: str) -> dict[str, dict[str, Any]]:
    """读该 work 的用户坐标：``{card_id: {x, y, pinned}}``。

    稀疏（L6）：只出**用户表态过的卡**——坐标非空，或钉住了但坐标为空。
    什么都没表态过的卡不在返回里（前端拿到就是"无记录"，走自动排），
    与旧 layout.json 的稀疏原则同口径。

    fail-open（L8）：库不存在／读失败一律返 ``{}``，绝不抛出中断调用方。
    """
    def _fn(conn: Any) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            "SELECT card_id, pos_x, pos_y, pinned FROM cards"
            " WHERE pos_x IS NOT NULL OR pos_y IS NOT NULL OR COALESCE(pinned, 0) <> 0"
        )
        return _positions_of(rows)

    try:
        return await card_db.run_read(work_id, _fn, {}) or {}
    except Exception as exc:  # noqa: BLE001 fail-open，同 rebuild 纪律
        logger.warning("位置读取失败，按空布局处理 work=%s: %s", work_id, exc)
        return {}


async def merge_positions(work_id: str, positions: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """合并写入用户坐标，返回**合并后的全量**坐标（``{card_id: {x,y,pinned}}``）。

    - **逐字段深合并**（E6 语义随迁，RD-04 不许回退）：载荷缺哪个字段就
      不动哪个列。``{pinned: false}`` 只清钉住标记，坐标原样保留。
    - **脏行过滤**（方案书风险 2）：``UPDATE ... WHERE card_id = ?`` 命中 0
      行＝载荷指向库里不存在的卡（卡已删、载荷来自旧前端缓存），此时
      **不插入任何新行**——cards 表的 card_id 是外键事实，位置不许凭空
      长出一张卡来。只记 warning，不抛（fail-open）。
    - 对外签名与返回语义不变（不变量 L5）：前端不消费 PATCH 响应体，
      这里回全量只是"改完顺手给个真相"。
    """
    updates: list[tuple[str, str, tuple[Any, ...]]] = []
    for cid, rec in (positions or {}).items():
        if not isinstance(rec, dict):
            continue
        sets: list[str] = []
        vals: list[Any] = []
        # x/y 只认有限数：前端自己也会判 Number.isFinite，别把 NaN/inf 写进库
        for key, col in (("x", "pos_x"), ("y", "pos_y")):
            if rec.get(key) is None:
                continue
            try:
                num = float(rec[key])
            except (TypeError, ValueError):
                logger.warning("坐标字段非数值，按未提供处理 card=%s field=%s value=%r",
                               cid, key, rec.get(key))
                continue
            if not math.isfinite(num):
                logger.warning("坐标字段非有限数，按未提供处理 card=%s field=%s value=%r",
                               cid, key, rec.get(key))
                continue
            sets.append(f"{col} = ?")
            vals.append(num)
        if rec.get("pinned") is not None:
            sets.append("pinned = ?")
            vals.append(1 if rec["pinned"] else 0)
        if sets:
            updates.append((cid, ", ".join(sets), tuple(vals)))

    if not updates:
        return await load_positions(work_id)

    def _fn(conn: Any) -> list[str]:
        skipped: list[str] = []
        for cid, set_clause, vals in updates:
            cur = conn.execute(
                f"UPDATE cards SET {set_clause} WHERE card_id = ?", (*vals, cid)
            )
            if not cur.rowcount:
                skipped.append(cid)
        return skipped

    try:
        skipped = await card_db.run_write(work_id, _fn) or []
    except Exception as exc:  # noqa: BLE001 写失败只记日志，绝不中断调用方
        logger.warning("坐标写入失败（不中断）work=%s: %s", work_id, exc)
        return await load_positions(work_id)

    if skipped:
        logger.warning("坐标载荷指向不存在的卡片，已过滤（不写脏行）work=%s cards=%s",
                       work_id, skipped)
    return await load_positions(work_id)
