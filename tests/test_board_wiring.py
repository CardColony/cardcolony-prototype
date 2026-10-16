# -*- coding: utf-8 -*-
"""《对话流画板架构》A 组 T1/T4 接线契约的机械守卫（源码级）。

**为什么要有这个文件**：两套 jsdom 取证脚本（`_evidence_*.mjs`）只 import
`static/stream.js`，**根本不加载 `static/workspace.js`** —— 也就是说 workspace 侧的
接线（open 同步 activate、9 个直贴点写自己会话容器、stop 带卡号）此前**没有任何
自动化守卫**。本文件把这几条契约锁死，防回归。

判据逐条对应任务书 §六 T1/T4：

- T4-1 `open()` 在**第一个 `await` 之前**同步 `EventFlow.activate(WORK_ID, cardId)`；
- T4-2 系统行落点全部走 `ensureMsgs(cardId)`，且 `ensureMsgs` 的实现是
       `EventFlow.board(...)`（不再是「#chatArea 下唯一 .msgs」＝P4 根因）；
- T4-3 `EventFlow.stop(...)` 在 workspace 里恒带 `(WORK_ID, cardId)`；
- T4-4 15 秒回流路径（`tryRecover`）用卡号取画板、`resync` 用卡号。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WS = ROOT / "static" / "workspace.js"
STREAM = ROOT / "static" / "stream.js"


def _strip_js_comments(src: str) -> str:
    """去掉 /* */ 与 // 注释：源码级断言不能被注释里的旧词条带偏。"""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def _fn_body(src: str, header: str) -> str:
    """按 `function <name>(` / `async function <name>(` 取函数体（花括号配平）。"""
    m = re.search(re.escape(header) + r"\s*\([^)]*\)\s*\{", src)
    assert m, f"找不到函数 {header}"
    i = m.end()
    depth = 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[m.start():i]


def test_t4_open_activates_before_first_await():
    """open() 必须在第一个 await 之前**同步** activate —— 点下去画板立刻翻页。"""
    body = _strip_js_comments(_fn_body(WS.read_text(encoding="utf-8"), "async function open"))
    i_act = body.find("EventFlow.activate(")
    i_await = body.find("await ")
    assert i_act >= 0, "open() 未调用 EventFlow.activate（画板不会立刻翻页）"
    assert i_await >= 0, "open() 里找不到 await（判据失效，请核对实现）"
    assert i_act < i_await, "activate 必须在第一个 await 之前（否则旧卡内容滞留、快速连点串卡）"


def test_t4_ensure_msgs_is_session_scoped():
    """ensureMsgs 取的是**该卡自己的**画板容器，不是全局唯一 .msgs。"""
    src = _strip_js_comments(WS.read_text(encoding="utf-8"))
    body = _fn_body(src, "function ensureMsgs")
    assert "EventFlow.board(" in body, "ensureMsgs 未走会话表取容器"
    assert "querySelector" not in body, "ensureMsgs 仍按「#chatArea 下唯一 .msgs」取容器（P4 根因）"
    calls = [c.strip() for c in re.findall(r"ensureMsgs\(([^)]*)\)", src) if c.strip()]
    bad = [c for c in calls if c not in ("cardId", "cardIdAtSend")]
    assert not bad, f"有 ensureMsgs 调用点未传卡号：{bad}"


def test_t4_stop_is_card_scoped():
    """workspace 里 EventFlow.stop 恒带 (WORK_ID, cardId)——不无参停「当前会话」。"""
    src = _strip_js_comments(WS.read_text(encoding="utf-8"))
    calls = re.findall(r"EventFlow\.stop\(([^)]*)\)", src)
    assert calls, "找不到 EventFlow.stop 调用点"
    bad = [c for c in calls if "WORK_ID" not in c or "cardId" not in c]
    assert not bad, f"EventFlow.stop 未带 (WORK_ID, cardId)：{bad}"


def test_t4_recover_path_is_card_scoped():
    """15 秒回流（tryRecover）：resync 用卡号；系统提示行写该卡自己的容器。"""
    body = _strip_js_comments(_fn_body(WS.read_text(encoding="utf-8"), "async function tryRecover"))
    assert re.search(r"EventFlow\.resync\(WORK_ID,\s*cardId\)", body), "resync 未带卡号"
    assert "ensureMsgs(cardId)" in body, "回流提示行未写进该卡自己的容器"


# ---------------------------------------------------------------- T5 回流增量（K，治 P6）
# 任务书 §六 T5 完成标准②：代码级断言「pullUserBubble 路径的 getTimeline 调用带 from>0」。
# ★ 另一条铁律：**两套 seq 严禁互写**——`st.lastSeq` 是内存 journal seq，
#   `/timeline` 的 `from` 走落盘 events seq。下面的断言把这条钉死。

API_JS = ROOT / "static" / "esm" / "api.js"


def test_t5_get_timeline_opens_from_param():
    """api.js 的 getTimeline 开 `from` 口，且**缺省不带参数**（＝现行为／全量，向后兼容）。"""
    src = _strip_js_comments(API_JS.read_text(encoding="utf-8"))
    m = re.search(r"getTimeline:\s*\(([^)]*)\)\s*=>", src)
    assert m, "找不到 getTimeline"
    assert "from" in m.group(1), "getTimeline 未开 from 形参"
    body = src[m.start():m.start() + 500]
    assert "?from=" in body, "getTimeline 未拼 ?from="
    assert re.search(r"from\s*>\s*0", body), "缺省（from<=0）必须不带参数——否则不再是「现行为」"


def test_t5_pull_user_bubble_is_incremental():
    """pullUserBubble 按游标增量拉；游标必须落在**落盘 seq 空间**（不得读 st.lastSeq）。"""
    src = _strip_js_comments(STREAM.read_text(encoding="utf-8"))
    body = _fn_body(src, "async function pullUserBubble")
    assert re.search(r"fetchTimeline\(st,\s*wid,\s*cid,\s*st\.pullSeq\)", body), \
        "pullUserBubble 未按游标增量拉取"
    assert "st.pullSeq" in body and "diskSeq" in body, "游标未播种自落盘水位"
    assert "lastSeq" not in body, \
        "pullUserBubble 读了 st.lastSeq（内存 journal seq）——两套 seq 空间严禁互写"


def test_t5_fetch_timeline_tracks_disk_seq():
    """fetchTimeline 透传 from 并维护落盘水位 st.diskSeq（增量游标的唯一播种源）。"""
    src = _strip_js_comments(STREAM.read_text(encoding="utf-8"))
    body = _fn_body(src, "async function fetchTimeline")
    assert "API.getTimeline(wid, cid, from)" in body, "fetchTimeline 未把 from 透传下去"
    assert "st.diskSeq" in body, "fetchTimeline 未维护落盘水位"
    assert "lastSeq" not in body, "fetchTimeline 读了 st.lastSeq——两套 seq 严禁互写"
