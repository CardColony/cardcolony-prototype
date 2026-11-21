# -*- coding: utf-8 -*-
"""轮槽（THINK-ECO-T11 · 移植对应总表 #9 新造）。

[2026-10-31 · 用户裁定：折叠整体移除] 原本本模块还承担「工具轮折叠」——把窗口外
旧工具轮压成【结论】…【依据】… 文本标记。**这套折叠已删除**，理由（裁定原文口径）：

- 它把工具调用压成 `【结论】/【依据】` 文本标记，会导致模型模仿这个形态、
  在正文里输出假工具调用（而非走结构化 ``tool_calls`` 通道）——这正是原版
  dsh ``scrubMarkerLines`` / 动作通道提醒治的病；
- 工具结果**只在当前轮展示**：更早回合的工具结果本来就不进上下文
  （``predicates.enters_context`` 排除工具块，该逻辑保留、不是缺陷）。

现在本模块只剩**轮槽**这一件事：请求局部对象，包住 routes/message_runner 的
prior messages 列表，提供三类 append 口（普通块 / 工具轮开轮 / 工具结果），
``wire_messages()`` 无触发时**恒返回原列表对象**；触发体积上限时返回
**投影副本**（原列表与其内 dict 永不改写）。

[2026-11-02 · card_021 实案] 「工具结果只在当前轮展示」不等于「当前轮无顶」：
重读循环把同一 8 万 token 的块×12 轮压上 1M 模型窗口。``wire_messages()``
因此在投影侧加了四道纯机械闸——单条硬顶 / 同回合去重 / 旧轮截头 /
累计预算（详 `wire_messages` docstring）。这是**体积边界**，不是折叠：
不发明【结论】/【依据】式标记、不改条数与配对、不写回原列表，
提示语全走 `[...]` 说明文本（与 tool_error 同通道）。

- append 面：首轮用户原文固化与抢救回注走 append_plain（非轮）；
  assistant＋tool 结果走 begin_round／append_tool_result（成轮）。
- 边界：原列表永不改写（prompt 日志与失败兜底都吃原文）。
- ``fold_ops`` 属性保留为恒空列表（prompt_log「空则不同构」——空 ⇒ 键不落盘）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger("api.tool_fold")


# [2026-11-02 · card_021 实案] 回合内 wire 顶——折叠移除后 wire 零收口：
# 同一工具结果每轮全量驻留到回合结束。实案：模型改需求文档反复 not_found，
# 把 8 万 token 的块整块回读 12 轮，当轮上线 109 万 token 撞穿 1M 窗口。
# 以下四道机械闸在 wire_messages() 投影时生效（投影不改写原列表）：
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except ValueError:
        logger.warning("[tool_fold] %s 非法（%r），用缺省 %s", name, raw, default)
        return default


def _wire_result_cap() -> int:
    """单条工具结果的上线硬顶（字符）——再大的结果也只带前 N 字符。"""
    return _env_int("WIRE_RESULT_MAX_CHARS", 60000)


def _wire_old_cap() -> int:
    """非最新轮的工具结果在 wire 上的保留头长（字符）。"""
    return _env_int("WIRE_OLD_RESULT_CHARS", 3000)


def _wire_budget() -> int:
    """本回合 wire 上工具结果的累计上限（字符）。超了从最早的开始省略。"""
    return _env_int("WIRE_BUDGET_CHARS", 300000)


def _wire_dedup_min() -> int:
    """小于此长度的相同工具结果不去重（去重提示反而比原文长）。"""
    return _env_int("WIRE_DEDUP_MIN_CHARS", 2000)


def _wire_note(msg: str) -> str:
    """wire 投影提示语——纯方括号说明，与 tool_error 同通道、不含工具
    调用形态标记（【结论】/【依据】诱发假调用之鉴，见顶部裁定注）。"""
    return f"[{msg}]"


def _flat_cap(content: str, cap: int, ev: dict[str, Any] | None,
              note: str) -> str:
    """平截兜底——[返修] 有证据的试点结果**连宿主标记一起入账**：

    ``content`` 形如 ``prefix + 正文 + suffix``（抢救纠正提示、任务证据
    rc: 行）。旧实现 ``content[:cap] + note`` 会把尾部 suffix 截丢且
    实发 = cap + len(note) 超顶。这里剥出正文按 ``cap - 标记 - 提示``
    收，prefix/suffix 原样保留；极端 cap 连标记信封都装不下时退化为
    头截（标记尽量留在可见头部），并如实计入预算。
    """
    marker = _wire_note(note)
    pre = str((ev or {}).get("prefix") or "")
    suf = str((ev or {}).get("suffix") or "")
    body = content
    if pre and body.startswith(pre):
        body = body[len(pre):]
    if suf and body.endswith(suf):
        body = body[:-len(suf)]
    room = cap - len(pre) - len(suf) - len(marker)
    if room > 0:
        return pre + body[:room] + marker + suf
    # 标记＋提示已超顶：正文归零也保标记信封；仍超顶就**如实超顶**——
    # 超量计入累计预算由后续结算/回报负责，绝不头截吞掉 rc: 尾标。
    return pre + marker + suf


# ================================================================
# [成本优化 · 第四批 2026-11-21] 工具证据投影（search_files / read_file）
#
# 缩量发生在 wire_messages() 生成请求副本时：结构化证据元数据随轮槽传递，
# 原列表和内层字典保持原样，内部字段留在宿主。开关 ``TOOL_EVIDENCE_WIRE=1``
# （缺省关 ⇒ 逐字同基线）；只动普通 main 回合里这两种工具的**成功**结果。
# ================================================================

def _evidence_wire_on() -> bool:
    """第四批证据投影开关：显式 ``TOOL_EVIDENCE_WIRE=1`` 才启用。"""
    return os.environ.get("TOOL_EVIDENCE_WIRE") == "1"


#: 本批试点工具——只这两件的成功结果进证据投影。
_EVIDENCE_TOOLS = frozenset({"search_files", "read_file"})


def _ev_key(ev: dict[str, Any]) -> tuple | None:
    """read_file 同回合引用键：文件身份＋实际行区间＋**整份文件**字节指纹。

    「同一文件、同一区间、同一版本」三要素缺一不可：换路径/换区间/文件
    换版（哪怕只改了读取区间外的一行）各回各的结果，绝不合并。指纹＝
    handler 对已读到的整份文件字节求的 sha256（信封层随结果带出，
    不另读盘）；凭据缺失的读取**不参与**引用合并（各自结果各自处理）。
    """
    if str(ev.get("tool") or "") != "read_file":
        return None
    d = ev.get("data")
    fp = str(ev.get("file_sha256") or "")
    if not isinstance(d, dict) or not fp:
        return None                          # 版本凭据缺失 ⇒ 不合并
    return (str(d.get("path") or ""), str(d.get("read_lines") or ""), fp)


def _dumps(d: dict[str, Any]) -> str:
    return json.dumps(d, ensure_ascii=False)


def _search_fragment(text: str, keyword: str, pattern: str) -> str | None:
    """命中行长时从匹配处取逐字片段（±40 字）；定位失败 ⇒ None（纯坐标）。

    定位依据是**与 handler 同序的真实匹配语义**：pattern 非空时只有
    正则说了算（命中的行被 strip 过，边缘空白匹配可能消失——如
    ``\\s+$``——这时**不许**拿 keyword 里的另一段文字冒充命中片段，
    老实只给坐标）；pattern 空才走 keyword 子串（不区分大小写）。
    """
    t = str(text or "")
    pos, mlen = -1, 0
    if pattern:
        try:
            m = re.search(pattern, t, re.IGNORECASE)
        except re.error:
            m = None
        if m is None:
            return None                            # 正则定位失败 ⇒ 纯坐标
        pos, mlen = m.start(), max(1, m.end() - m.start())
    elif keyword:
        pos = t.lower().find(keyword.lower())
        mlen = len(keyword)
    if pos < 0:
        return None
    lo, hi = max(0, pos - 40), min(len(t), pos + mlen + 40)
    return ("…" if lo > 0 else "") + t[lo:hi] + ("…" if hi < len(t) else "")


def _project_search(data: dict[str, Any], args: dict[str, Any],
                    cap: int) -> str | None:
    """search_files 证据投影：范围/扫描数/命中坐标（1 起行号）优先保留；
    命中行正文只留短原文或可定位的逐字片段；放不下时保有序坐标前缀。

    工具扫描截断（truncated）与出站展示省略（shown<len(hits)）分开记录。
    """
    hits = [h for h in (data.get("hits") or []) if isinstance(h, dict)]
    kw = str(data.get("keyword") or "")
    # [返修] 与 handler 同口径归一化：pattern 先 strip——纯空白 pattern 在
    # handler 里按空处理走 keyword 搜索，投影若拿原始空白当正则会把行内
    # 空格误当命中位置。
    pat = str(args.get("pattern") or "").strip()
    meta = {"keyword": kw, "scope": str(data.get("scope") or ""),
            "scanned_files": data.get("scanned_files"),
            "truncated": bool(data.get("truncated"))}

    def _render(items: list[dict[str, Any]], note: str = "") -> str:
        d = dict(meta)
        d["hits"] = items
        if note:
            d["note"] = note
        return _dumps(d)

    # ① 全坐标＋正文/片段（短行照原文，长行留可定位片段，定位失败只给坐标）
    rich: list[dict[str, Any]] = []
    for h in hits:
        it = {"file": h.get("file"), "line": h.get("line")}
        t = str(h.get("text") or "")
        if len(t) <= 120:
            it["text"] = t                     # 短行逐字
        else:
            frag = _search_fragment(t, kw, pat)
            if frag is not None and len(frag) < len(t):
                it["fragment"] = frag          # 明确标为片段
        rich.append(it)
    if hits:
        out = _render(rich)
        if len(out) <= cap:
            return out
    # ② 纯坐标（无正文）：全部命中坐标优先
    coords = [{"file": h.get("file"), "line": h.get("line")} for h in hits]
    out = _render(coords, "命中行正文未展示；read_file(path, line, line) 可回读原文")
    if len(out) <= cap:
        return out
    # ③ 坐标也装不下 ⇒ 保有序前缀，注明展示/总数与续查办法
    lo = 0
    while lo < len(coords):
        note = (f"仅展示前 {lo}/{len(coords)} 条命中坐标；"
                f"用更窄的 path 参数继续检索可得其余")
        out = _render(coords[:lo], note)
        if len(out) > cap and lo > 0:
            break
        lo = lo * 2 + 1
    lo = max(0, lo - 1)
    while lo > 0:
        note = (f"仅展示前 {lo}/{len(coords)} 条命中坐标；"
                f"用更窄的 path 参数继续检索可得其余")
        out = _render(coords[:lo], note)
        if len(out) <= cap:
            return out
        lo -= 1
    out = _render([], f"共 {len(coords)} 条命中坐标装不下出站预算；"
                      f"用更窄的 path 参数继续检索")
    if len(out) <= cap:
        return out
    # ④ 极小预算：最小状态信封——keyword/scope/note 全让位，只保 hits
    #    坐标前缀＋truncated＋（被裁时的）总数。零命中＋扫描未完成时
    #    {"hits":[],"truncated":true} ≈35 字符也发得出去，这两条事实
    #    优先于半截 JSON 头。
    for keep_n in range(len(coords), -1, -1):
        cand = {"hits": coords[:keep_n],
                "truncated": bool(data.get("truncated"))}
        if keep_n < len(coords):
            cand["total_hits"] = len(coords)
        out = _dumps(cand)
        if len(out) <= cap:
            return out
    return None


def _project_read(data: dict[str, Any], cap: int) -> str | None:
    """read_file 证据投影：实际 path/total_lines/read_lines＋完整行段
    （头尾各留），省略区间显式标注可回读的行号。

    [返修] 预算按**最终序列化结果**收——JSON 转义（引号/换行各占一格
    变两格）、宿主标记与回读提示一起计入上限。头尾贪心后按序列化实测
    收敛，逐行让位直到装下或触底。
    """
    content = str(data.get("content") or "")
    rng = str(data.get("read_lines") or "1-1")
    try:
        start = int(rng.split("-", 1)[0])
    except (ValueError, IndexError):
        start = 1
    meta = {"path": data.get("path"), "total_lines": data.get("total_lines"),
            "read_lines": rng}
    if not content:
        return _dumps(dict(meta, content=""))      # 空区间语义原样
    lines = content.split("\n")
    end = start + len(lines) - 1

    def _render(n_head: int, n_tail: int) -> str:
        e_start, e_end = start + n_head, end - n_tail
        marker = (f"[已省略行 {e_start}–{e_end}"
                  f"（共 {e_end - e_start + 1} 行）："
                  f"read_file(path, {e_start}, {e_end}) 可回读]")
        return _dumps(dict(
            meta,
            content="\n".join(
                lines[:n_head] + [marker]
                + (lines[len(lines) - n_tail:] if n_tail else [])),
            shown={"head": f"{start}-{start + n_head - 1}" if n_head else "",
                   "tail": f"{end - n_tail + 1}-{end}" if n_tail else "",
                   "elided": f"{e_start}-{e_end}"}))

    # 初估：转义膨胀最坏 ~2 倍，先按 cap/2 收原始正文，再按实测收敛。
    body_cap = max(120, (cap - 300) // 2)
    n_head = n_tail = 0
    head_used = tail_used = 0
    for ln in lines:
        if head_used + len(ln) + 1 > body_cap // 2:
            break
        head_used += len(ln) + 1
        n_head += 1
    for ln in reversed(lines[n_head:]):
        if tail_used + len(ln) + 1 > body_cap // 2:
            break
        tail_used += len(ln) + 1
        n_tail += 1
    # 序列化实测收敛：还超就轮流从长边让位一行，直到装下或全让光。
    while n_head + n_tail > 0:
        if n_head + n_tail >= len(lines):
            return None                            # 全装下 ⇒ 不是投影
        out = _render(n_head, n_tail)
        if len(out) <= cap:
            return out
        if n_head >= n_tail:
            n_head -= 1
        else:
            n_tail -= 1
    # 连一行完整行都装不下——如实显示限制与原记录位置
    first_len = len(lines[0])
    note = _dumps(dict(meta, content="", note=(
        f"行 {start} 起的完整行装不下本次出站预算（首行 {first_len} 字符），"
        f"未展示任何正文；原记录在 {data.get('path')} 行段 {rng}，"
        f"需其他方式回读")))
    return note if len(note) <= cap else None


def _project_evidence(ev: dict[str, Any], cap: int) -> str | None:
    """试点工具结果的结构化投影；非试点/无数据/装不下 ⇒ None（走原路径）。

    prefix/suffix 是宿主行（抢救纠正提示、任务证据 rc: 行）——投影只换
    中间的结果正文，两件照旧保留。[返修] cap 按**最终序列化结果**收：
    宿主标记也计入上限（cap 先扣 prefix/suffix 再给正文投影）。
    """
    data = ev.get("data")
    if not isinstance(data, dict):
        return None
    tool = str(ev.get("tool") or "")
    pre, suf = str(ev.get("prefix") or ""), str(ev.get("suffix") or "")
    inner = cap - len(pre) - len(suf)
    if inner <= 0:
        return None                                # 宿主标记本身就超顶
    if tool == "search_files":
        core = _project_search(data, ev.get("args") or {}, inner)
    elif tool == "read_file":
        core = _project_read(data, inner)
    else:
        return None
    if core is None:
        return None
    out = pre + core + suf
    return out if len(out) <= cap else None


class ToolTurnSlot:
    """请求局部轮槽：包住 routes 的 prior_for_gateway 同一条 messages 列表。

    - ``wire_messages()`` 无触发时**恒返回原列表对象**（折叠已移除
      2026-10-31 用户裁定），工具结果只在当前轮展示、更早回合不进上下文；
      触发体积上限时返回投影副本（2026-11-02 card_021 实案），原列表不改写。
    - append 面：首轮用户原文固化与抢救回注走 append_plain（非轮）；
      assistant＋tool 结果走 begin_round／append_tool_result（成轮）。
    """

    def __init__(self, messages: list[dict[str, Any]], *,
                 evidence_wire: bool = False):
        self._messages = messages
        # 每轮：{"assistant_idx", "tool_idxs"}——只记结构，不再承担任何折叠状态。
        self._rounds: list[dict[str, Any]] = []
        self._open: dict[str, Any] | None = None
        self.fold_ops: list[dict[str, Any]] = []  # 恒空（折叠已移除；prompt_log 空则不同构）
        # [批四 · 工具证据] 本轮证据投影总闸：开关开＋普通 main 回合才 True。
        self._evidence_wire = bool(evidence_wire and _evidence_wire_on())
        #: msg_idx → 结构化证据元数据（tool/args/data/prefix/suffix）。
        #: 内部字段留在宿主——绝不写进 msg 字典。
        self._evidence: dict[int, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # append 面
    # ------------------------------------------------------------------
    def append_plain(self, msg: dict[str, Any]) -> None:
        """非轮消息：首轮用户原文固化、抢救回注等。"""
        self._messages.append(msg)

    def begin_round(self, assistant_msg: dict[str, Any]) -> None:
        """开新轮：含工具调用的 assistant 消息（结构由调用方按原样构造）。"""
        r = {"assistant_idx": len(self._messages), "tool_idxs": []}
        self._rounds.append(r)
        self._open = r
        self._messages.append(assistant_msg)

    def append_tool_result(self, msg: dict[str, Any],
                           *, evidence: dict[str, Any] | None = None) -> None:
        """工具返回入槽：归入当前开放轮（配对结构由调用方的 tool_call_id 保证）。

        ``evidence``＝批四结构化证据（宿主侧元数据，不进 msg）。"""
        idx = len(self._messages)
        if self._open is not None:
            self._open["tool_idxs"].append(idx)
        self._messages.append(msg)
        if evidence:
            self._evidence[idx] = evidence

    # ------------------------------------------------------------------
    # 装配面
    # ------------------------------------------------------------------
    @property
    def round_count(self) -> int:
        return len(self._rounds)

    async def wire_messages(self) -> list[dict[str, Any]]:
        """装配：**无触发时恒返回原列表对象本身**；触发上限时返回投影副本。

        折叠（【结论】/【依据】两件套）已于 2026-10-31 按用户裁定整体移除：
        它把工具调用压成文本标记、误导模型在正文输出假工具调用。工具结果的
        生命周期归 ``predicates.enters_context``：当前轮在场、更早回合不进上下文。

        [2026-11-02 · card_021 实案] 但「只在当前轮展示」≠「当前轮无顶」——
        同一工具结果每轮全量驻留，重读循环把 8 万 token 的块×12 轮压上
        1M 窗口。三道机械闸（仅工具结果内容、仅投影；原列表与配对不动）：
          ① 去重——同回合逐字节相同的大结果只留**最新一份**，旧的出现位换
             成一行指引（小结果不去重，提示比原文还长）；
          ② 单条硬顶——任何一条超过 WIRE_RESULT_MAX_CHARS 截头；
          ③ 累计预算——WIRE_BUDGET_CHARS 超了才动旧轮：先截头、仍超再从
             最早开始整段省略、还超压最新轮。**不过线则完全零加工**
             （恒返回原列表对象——2026-10-31 裁定的 T6 判据原样成立）。
        提示语走 ``[...]`` 纯说明文本（与 tool_error 同通道），**绝不引入**
        任何调用形态标记——裁定删的是诱发假调用的标记，不是体积边界。
        投影全部在副本上做：``self._messages`` 与其内 dict 永不改写
        （prompt 日志与失败兜底继续吃原文）。
        """
        msgs = self._messages
        tool_pos: list[tuple[int, int]] = []   # (round_idx, msg_idx) wire 序
        for r_i, r in enumerate(self._rounds):
            for idx in r.get("tool_idxs") or []:
                if 0 <= idx < len(msgs) and msgs[idx].get("role") == "tool":
                    tool_pos.append((r_i, idx))
        if not tool_pos:
            return msgs

        last_round = tool_pos[-1][0]
        out: list[dict[str, Any]] = list(msgs)
        changed = 0
        ev_wire = self._evidence_wire
        refs: dict[int, int] = {}          # 引用位 idx → 代表项 idx（预算后核对用）

        # ① 同回合去重：逐字节相同的大结果，仅最新一份保留全文。
        #    [批四] 试点工具改用**证据键**去重——同一解析后文件身份＋实际
        #    行区间＋该次读取取得的字节指纹相同才算同一证据（不同文件同文、
        #    同文件不同区间都不合并；引用注明路径/行段/保留位 tool_call_id）。
        by_content: dict[str, list[int]] = {}
        by_ev: dict[tuple, list[int]] = {}
        for _, idx in tool_pos:
            ev = self._evidence.get(idx) if ev_wire else None
            if ev is not None:
                # [返修] 试点结果**只**走新判据：有证据对象 ⇒ 不进旧逐字节闸。
                # 凭据缺失（read_file 无指纹）/search_files（本批不设引用键）
                # ⇒ 各自保留——否则两份缺凭据长读结果、重复搜索仍会被旧闸
                # 合并成「与下文完全相同」，而下文可能已被裁成预览。
                key = _ev_key(ev)
                if key is not None:
                    by_ev.setdefault(key, []).append(idx)
                continue
            by_content.setdefault(
                str(msgs[idx].get("content") or ""), []).append(idx)
        for key, idxs in by_ev.items():
            if len(idxs) < 2:
                continue
            keep_idx = idxs[-1]
            keep_id = str(msgs[keep_idx].get("tool_call_id") or "")
            path, rng = key[0], key[1]
            for idx in idxs[:-1]:
                content = str(msgs[idx].get("content") or "")
                ev = self._evidence.get(idx) or {}
                note = (str(ev.get("prefix") or "")
                        + _wire_note(
                            f"同一读取证据已在本轮 {keep_id} 展示（{path} "
                            f"行段 {rng}，同版本原文见该条），不再重复；"
                            f"要当前版本请重新调用 read_file")
                        + str(ev.get("suffix") or ""))
                if len(note) >= len(content):
                    continue              # 投影不更短 ⇒ 不替换
                out[idx] = {**msgs[idx], "content": note}
                refs[idx] = keep_idx
                changed += 1
        dedup_min = _wire_dedup_min()
        for content, idxs in by_content.items():
            if len(idxs) < 2 or len(content) < dedup_min:
                continue
            keep_idx = idxs[-1]
            keep_id = str(msgs[keep_idx].get("tool_call_id") or "")
            for idx in idxs[:-1]:
                out[idx] = {**msgs[idx], "content": _wire_note(
                    f"本轮工具结果与下文 {keep_id} 完全相同"
                    f"（{len(content)} 字符），不再重复展示")}
                changed += 1

        # ② 单条硬顶（任何轮——单条怪物本身就够撞窗）。
        #    [批四] 试点工具先试结构化投影（坐标/行段＋可回读标注），
        #    装不下或更长才落回平截头。
        #    [返修] 平截走 _flat_cap——宿主标记剥出重挂，尾部 rc: 不丢，
        #    实发不超 cap（标记信封超顶时如实超顶入账）。
        res_cap, old_cap = _wire_result_cap(), _wire_old_cap()
        for r_i, idx in tool_pos:
            if out[idx] is not msgs[idx]:
                continue                       # 已是去重指引
            content = str(out[idx].get("content") or "")
            if len(content) > res_cap:
                ev = self._evidence.get(idx) if ev_wire else None
                proj = (_project_evidence(ev or {}, res_cap)
                        if ev_wire else None)
                if proj is not None and len(proj) < len(content):
                    out[idx] = {**msgs[idx], "content": proj}
                elif ev is not None:
                    # [返修] 试点结果的平截：宿主标记剥出重挂、实发≤cap
                    out[idx] = {**msgs[idx], "content": _flat_cap(
                        content, res_cap, ev,
                        f"结果共 {len(content)} 字符，仅展示前 "
                        f"{res_cap}；需要更多请按行段重读来源")}
                else:
                    out[idx] = {**msgs[idx], "content": (
                        content[:res_cap] + _wire_note(
                            f"结果共 {len(content)} 字符，仅展示前 "
                            f"{res_cap}；需要更多请按行段重读来源"))}
                changed += 1

        # ③ 累计预算——只有过线才动旧轮：先截头、仍超再整段省略、
        #    还超压最新轮。预算内零加工（同对象返回的判据靠这道闸守着）。
        #    [批四] 试点工具的旧轮缩头/省略改用证据投影（坐标/回读标注），
        #    投影更短才替换；整段省略位按证据给出路径＋行段回读指引。
        #    [返修] 提成 _settle：④引用降级会**放大**总量（实测定额附近
        #    +12 字符），最终出站前必须再结一次。
        budget = _wire_budget()
        settled_touched: set[int] = set()   # 历次 _settle 已改写过的位置

        def _settle() -> int:
            """一轮预算结算（旧轮截头→整段省略→最新轮压），返回结算后总量。

            [返修] ``settled_touched`` 跨调用累积：本函数改写过的位置在
            **下一次**结算里不再动——否则平截笔记（cap+提示 ≈ old_cap+55）
            会被反复重截（「结果共 1000」变「结果共 347」），既改文案又
            永不收敛；同一调用内 ③a 截→③b 省照旧允许（与单调用旧语义
            逐字一致）。
            """
            nonlocal changed
            done: set[int] = set()
            total = sum(len(str(out[i].get("content") or ""))
                        for _, i in tool_pos)
            if total <= budget:
                return total
            for r_i, idx in tool_pos:
                if total <= budget or r_i == last_round \
                        or idx in settled_touched:
                    continue
                m = out[idx]
                content = str(m.get("content") or "")
                if len(content) > old_cap:
                    ev = self._evidence.get(idx) if ev_wire else None
                    proj = (_project_evidence(ev or {}, old_cap)
                            if ev_wire else None)
                    if proj is not None and len(proj) < len(content):
                        m2 = {**msgs[idx], "content": proj}
                    elif ev is not None:
                        m2 = {**msgs[idx], "content": _flat_cap(
                            content, old_cap, ev,
                            f"本轮较早的工具结果共 {len(content)} 字符，"
                            f"仅保留前 {old_cap}；需要原文请重新调用该工具")}
                    else:
                        m2 = {**msgs[idx], "content": (
                            content[:old_cap] + _wire_note(
                                f"本轮较早的工具结果共 {len(content)} 字符，"
                                f"仅保留前 {old_cap}；需要原文请重新调用该工具"))}
                    out[idx] = m2
                    done.add(idx)
                    total -= len(content) - len(str(m2["content"]))
                    changed += 1
            if total > budget:
                for r_i, idx in tool_pos:
                    if total <= budget or r_i == last_round \
                            or idx in settled_touched:
                        continue
                    content = str(out[idx].get("content") or "")
                    ev = self._evidence.get(idx) if ev_wire else None
                    d = ev.get("data") if ev else None
                    if isinstance(d, dict) and str(
                            ev.get("tool") or "") == "read_file":
                        _s, _e = (str(d.get("read_lines")
                                     or "1-1").split("-", 1) + ["1"])[:2]
                        note = _wire_note(
                            f"本轮较早的读取原文已省略（{d.get('path')} 行段 "
                            f"{d.get('read_lines')}，tool_call_id="
                            f"{out[idx].get('tool_call_id')}）；"
                            f"read_file(path, {_s}, {_e}) 可回读")
                    elif isinstance(d, dict) and str(
                            ev.get("tool") or "") == "search_files":
                        note = _wire_note(
                            f"本轮较早的检索结果已省略（{d.get('scope')} 内 "
                            f"{len(d.get('hits') or [])} 条命中，tool_call_id="
                            f"{out[idx].get('tool_call_id')}）；"
                            f"search_files 原参数重查可复核")
                    else:
                        note = _wire_note(
                            f"本轮较早的工具结果已省略（原 {len(content)} 字符，"
                            f"tool_call_id={out[idx].get('tool_call_id')}）；"
                            "需要内容请重新调用该工具")
                    # [返修] 宿主标记（抢救纠正/任务证据 rc:）随省略位保留
                    note = (str((ev or {}).get("prefix") or "") + note
                            + str((ev or {}).get("suffix") or ""))
                    out[idx] = {**msgs[idx], "content": note}
                    done.add(idx)
                    total -= len(content) - len(note)
                    changed += 1
            if total > budget:
                for r_i, idx in reversed(tool_pos):
                    if total <= budget or r_i != last_round \
                            or idx in settled_touched:
                        continue
                    content = str(out[idx].get("content") or "")
                    if len(content) <= old_cap:
                        continue
                    ev = self._evidence.get(idx) if ev_wire else None
                    proj = (_project_evidence(ev or {}, old_cap)
                            if ev_wire else None)
                    if proj is not None and len(proj) < len(content):
                        out[idx] = {**msgs[idx], "content": proj}
                        total -= len(content) - len(proj)
                    elif ev is not None:
                        out[idx] = {**msgs[idx], "content": _flat_cap(
                            content, old_cap, ev,
                            f"结果共 {len(content)} 字符，本轮 wire 已超预算，"
                            f"仅展示前 {old_cap}；需要更多请按行段重读来源")}
                        total -= len(content) - len(str(
                            out[idx]["content"]))
                    else:
                        out[idx] = {**msgs[idx], "content": (
                            content[:old_cap] + _wire_note(
                                f"结果共 {len(content)} 字符，本轮 wire 已超预算，"
                                f"仅展示前 {old_cap}；需要更多请按行段重读来源"))}
                        total -= len(content) - old_cap
                    done.add(idx)
                    changed += 1
            settled_touched.update(done)
            return total

        # ④⑤ [批四] 引用×预算联合结算：代表项若已被缩/省略，「原文见该条」
        #    即成事实错误——引用**无条件**换成行段回读说明（区分「之前读取
        #    的版本」与「现在重读」），宿主标记（prefix/suffix）照样保留。
        #    [返修] 降级会放大总量、再裁又会砍**另一组**的代表项——
        #    交替结算直到引用与预算**同时稳定**（每轮引用只减不增、内容
        #    只缩不涨，单调收敛；上限 8 轮防死循环，实测 2-3 轮即稳）。
        def _revalidate_refs() -> None:
            nonlocal changed
            for idx, keep_idx in refs.items():
                if out[keep_idx] is msgs[keep_idx]:
                    continue                       # 代表项原文仍在
                ev = self._evidence.get(idx) or {}
                d = ev.get("data") or {}
                _s, _e = (str(d.get("read_lines")
                             or "1-1").split("-", 1) + ["1"])[:2]
                degraded = (str(ev.get("prefix") or "")
                            + _wire_note(
                                f"本轮更早展示的 {d.get('path')} 行段 "
                                f"{d.get('read_lines')} 原文已被压缩/移出；"
                                f"那是**之前读取的版本**——要现在的内容请用 "
                                f"read_file(path, {_s}, {_e}) 重读")
                            + str(ev.get("suffix") or ""))
                if out[idx].get("content") != degraded:
                    out[idx] = {**msgs[idx], "content": degraded}
                    changed += 1

        total = _settle()
        if refs:
            # 只有证据引用存在时才需要二次结算——refs 恒空（开关关闭或非
            # 试点回合）时跑第二遍会把上一轮的裁剪笔记当新目标再裁一遍
            # （「共 1000 字符」变「共 347」），纯扰动。
            for _i in range(_env_int("WIRE_REF_SETTLE_MAX", 8)):
                _c0 = changed
                _revalidate_refs()
                total = _settle()
                if changed == _c0:
                    break                        # 引用与预算同时稳定
            # [返修] 触顶兜底：循环上限也可能留下「指向已裁代表项」的
            # 悬空引用（连锁裁剪 8 轮未必收敛完）——出站前**无条件**
            # 逐条重核，剩余悬空引用一律降级（宁可超预算也不放错指引）。
            _revalidate_refs()
            total = sum(len(str(out[i].get("content") or ""))
                        for _, i in tool_pos)
        if total > budget:
            logger.warning("[tool_fold] wire 结算后仍超预算 "
                           "%s > %s（宿主标记保底所致，如实出站）",
                           total, budget)

        if not changed:
            return msgs
        logger.info("[tool_fold] wire 投影生效 msgs=%s tool=%s 改写=%s",
                    len(msgs), len(tool_pos), changed)
        return out
