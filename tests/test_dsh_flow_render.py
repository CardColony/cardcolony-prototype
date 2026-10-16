# -*- coding: utf-8 -*-
"""《对话流照抄 dsh》（2026-10-16）前端改动的机械守卫。

前端没有 pytest DOM 载体：DOM 行为归 jsdom 取证脚本
`_evidence_dsh_flow_20261016.mjs`（T1–T4 ＋ T7 一致性/切卡，21 条全绿）
与 `_evidence_singlesource_20261015.mjs`（旧六条判据，25 条全绿）；
装配器纯逻辑归 `tests/assembler.test.mjs`。本文件只做**源码级机械断言**，
锁住本次重构的关键契约、防回归：

- T1 乐观上屏：send() 必须先落本地用户节点，再开事件流；回流对账保留；
  失败回退要撤本地气泡；
- T2 增量装配：渲染层按节点 key/rev 复用 DOM；**不存在整列清空重建**；
  15s 自愈轮询不得无条件 resync（必须先问后端是否仍在跑）；
- T3/T4：Think 行与工具行按 dsh 规格出元素（摘要/折叠/两段式）；
- T6 版本戳：全链每模块**唯一 URL**（同模块不得两个 ?v=）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STREAM = ROOT / "static" / "stream.js"
WS = ROOT / "static" / "workspace.js"
ASSEMBLER = ROOT / "static" / "esm" / "assembler.js"
STYLE = ROOT / "static" / "style.css"


def _strip_js_comments(src: str) -> str:
    """去掉 /* */ 与 // 注释：源码级断言不能被注释里的旧词条带偏。"""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def _fn_body(src: str, header: str) -> str:
    """按 `function <name>(` 或 `async function <name>(` 取函数体（花括号配平）。"""
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


# ---------------------------------------------------------------- T1 乐观上屏

def test_t1_send_optimistic_before_stream():
    """点发送瞬间先落用户气泡（optimisticUser），再开事件流（start）。"""
    body = _strip_js_comments(_fn_body(WS.read_text(encoding="utf-8"), "async function send"))
    i_opt = body.find("EventFlow.optimisticUser")
    i_start = body.find("EventFlow.start")
    assert i_opt >= 0, "send() 未调用乐观上屏"
    assert i_start >= 0, "send() 未开事件流"
    assert i_opt < i_start, "必须先落用户气泡再开流（否则过程行抢在提问之前）"
    assert "EventFlow.pullUserBubble" in body, "timeline 回流对账必须保留"
    assert "EventFlow.dropOptimisticUser" in body, "发送失败必须撤下本地气泡"


def test_t1_assembler_local_round_and_pending_upgrade():
    """装配器：本地轮（ns=local）＋ 认领改名 ＋ 内容相同则原地升级。"""
    asm = _strip_js_comments(ASSEMBLER.read_text(encoding="utf-8"))
    assert 'return "local:" +' in asm, "本地记录必须自成一组"
    assert "adoptLocalGroup" in asm and 'g.key = "run:" + runId' in asm, "本地轮必须能被真实 run 认领"
    assert re.search(r"n\.pending\b", asm), "本地用户节点必须带 pending 标记"
    assert re.search(r"pend\.pending = false", asm), "真实记录到达必须原地升级（不出双节点）"
    assert "dropLocal" in asm, "必须有撤下本地轮的入口"


# ---------------------------------------------------------------- T2 增量装配

def test_t2_renderer_is_incremental():
    """渲染层按节点 key/rev 复用元素；没有整列清空重建。"""
    src = _strip_js_comments(STREAM.read_text(encoding="utf-8"))
    assert "ensureGroupView" in src, "必须有「按 data-key 认领组容器」的入口"
    assert "gv.views" in src, "必须有「节点键 → 已渲染元素」的视图表"
    assert re.search(r"v\.rev !== \(node\.rev \|\| 0\)", src), "必须按 rev 判内容是否变化"
    assert "pruneGroups" in src, "必须剪枝（组改名/软删后旧容器退场）"
    # 整列清空重建＝P2/P3 的根：全文件不得再有「清空列」的写法
    assert "while (box.firstChild)" not in src, "仍在整列清空重建（P2 根因）"
    assert "while (ca.firstChild)" not in src, "仍在整列清空重建"
    # 单源不变：快照仍是唯一位置来源
    for sym in ("renderSnapshot", "renderAffected", "renderGroup"):
        assert sym in src, f"缺 {sym}"


def test_t2_recover_poll_gated_by_progress():
    """15s 自愈轮询不得无条件 resync——必须先问后端是否仍在跑。"""
    body = _strip_js_comments(_fn_body(WS.read_text(encoding="utf-8"), "async function tryRecover"))
    i_prog = body.find("API.getProgress")
    i_alive = body.find("active")
    i_resync = body.find("EventFlow.resync")
    assert i_prog >= 0 and i_alive >= 0, "tryRecover 必须先查后端进度"
    assert i_resync > i_prog, "resync 必须在「仍在跑就不动」的判据之后"
    assert "streamDead" in body, "断流也是恢复的合法触发条件之一"


def test_t2_snapshot_reference_stable():
    """装配器：结构未变时快照返回同一引用（sameReferences 语义）。"""
    asm = ASSEMBLER.read_text(encoding="utf-8")
    assert "snapCacheRev" in asm and "structRev" in asm, "快照必须有结构版本缓存"
    assert re.search(r"if\s*\(snapCache && snapCacheRev === structRev\)\s*return snapCache", asm), \
        "结构未变必须直接返回缓存引用"


# ---------------------------------------------------------------- T3 Think 行

def test_t3_think_row_spec():
    """Think 行＝图标＋「Think」＋摘要＋计时；完成取首行、运行取最新行。"""
    src = _strip_js_comments(STREAM.read_text(encoding="utf-8"))
    assert "thinkSummary" in src, "必须有摘要函数"
    assert re.search(r"running \? lines\[lines\.length - 1\] : lines\[0\]", src), \
        "摘要口径：运行中取最新一行、完成取第一行"
    assert 'think-lab">Think' in src, "头行标题必须是 Think（dsh ReasoningRow）"
    assert "think-ico" in src and "think-sum" in src and "think-time" in src, "头行三件套缺一"
    assert "collapsed" in src, "Think 行默认折叠"
    css = STYLE.read_text(encoding="utf-8")
    for sel in (".msg.assistant .think-ico", ".msg.assistant .think-sum",
                ".msg.assistant .think.collapsed .think-body"):
        assert sel in css, f"缺样式 {sel}"


# ---------------------------------------------------------------- T4 工具行

def test_t4_tool_row_spec():
    """工具行＝图标＋工具名＋参数摘要＋状态；结果首段摘要常显、全文点开。"""
    src = _strip_js_comments(STREAM.read_text(encoding="utf-8"))
    body = _fn_body(src, "function renderTool")
    assert "tool-row" in body and "tool-ico" in body and "tool-sum" in body, "工具行三件套缺一"
    assert "argSummary" in src, "必须有参数摘要"
    assert "resultSummary" in src, "必须有结果首段摘要"
    assert 'classList.toggle("open")' in body, "点整行必须切换展开态"
    css = STYLE.read_text(encoding="utf-8")
    assert ".tool-row .tool-body { flex: 1 0 100%; display: none; }" in css, "完整内容默认折叠"
    assert ".tool-row.open .tool-body { display: block; }" in css, "展开态可见"
    assert ".tool-row.running .tool-ico" in css, "运行中态要有动画"


# ---------------------------------------------------------------- T5 视觉规格（受可读性网约束）

def test_t5_user_bubble_compact_rounded():
    """用户气泡：右侧、浅底、圆角、紧凑（dsh MessageItem）。"""
    css = STYLE.read_text(encoding="utf-8")
    m = re.search(r"\.msg\.user\s*\{[^}]*\}", css)
    assert m, "缺 .msg.user"
    blk = m.group(0)
    assert "align-self: flex-end" in blk, "用户气泡必须靠右"
    assert re.search(r"border-radius:\s*\d+px", blk), "用户气泡必须圆角"
    assert "var(--c-user)" in blk, "用户气泡必须有浅底"


def test_t5_dsh_bubble_cap_not_adopted_documented():
    """dsh 的 min(525px,82%) 气泡宽上限**未采纳**：与「我的发言永不超出模型
    消息边界」（2026-10-12 用户裁定）冲突，属产品语义、留用户裁。
    本断言锁住「没被悄悄加进来」，防后人以为漏做。"""
    css = STYLE.read_text(encoding="utf-8")
    assert "525px" not in css, "若采纳了 dsh 气泡宽上限，请同步删掉本断言并在回报中说明"


# ---------------------------------------------------------------- T6 版本戳唯一

_REF_RE = re.compile(r"""["']([^"']*\?v=[^"']*)["']""")


def _version_map():
    """全链（static/**/*.js ＋ templates/*.html）扫出 {模块文件名: {版本}}。"""
    out: dict[str, set[str]] = {}
    files = list((ROOT / "static").rglob("*.js")) + list((ROOT / "templates").glob("*.html"))
    for p in files:
        for ref in _REF_RE.findall(p.read_text(encoding="utf-8")):
            path_part, ver = ref.split("?v=", 1)
            name = path_part.rsplit("/", 1)[-1]
            out.setdefault(name, set()).add(ver)
    return out


def test_t6_every_module_has_exactly_one_version():
    """T6：同一模块在全链只能有一个 URL（否则浏览器吃旧缓存、双份代码并存）。"""
    bad = {k: sorted(v) for k, v in _version_map().items() if len(v) > 1}
    assert not bad, "同模块双版本：" + repr(bad)


def test_t6_touched_modules_bumped():
    """版本基线：**共享前端 36**（stream/workspace/canvas/style 本轮涉改 ⇒ 35→36，
    入口 main.js 36.0）；assembler 本轮新增一类事件映射 ⇒ 33→34。
    纪律不变：改哪个模块哪个升号、全链每模块唯一 URL。

    ★ 历史：T7（2026-10-16《对话流画板架构》）统一升到 35；
      [更新现状按钮改造 2026-10-16] 涉改 stream.js / workspace.js / style.css，
      并因 import 链联动 canvas.js（它 import workspace.js）⇒ 四件升 36；
      `esm/main.js`（引用它们的 import 变了）随升 36.0；
      `esm/assembler.js`（新增 status_update_task 映射）33→34。
      [左栏新建项目 2026-10-16] 涉改 workspace.js（左栏按钮接线）、esm/nav_render.js（删空态死链）
      ⇒ nav_render.js 30→31、workspace.js 37→39（并因 import 链联动 canvas.js 36→38、
      `esm/main.js` 37.0→38.0）。nav.css 内容变 ⇒ 其 ?v= 升 12（本断言不含 nav.css，
      由唯一性用例兜底）；home.css 只删规则 ⇒ 其 ?v= 升 1.1。"""
    vm = _version_map()
    expect = {"stream.js": "36", "workspace.js": "41", "canvas.js": "40",
              "assembler.js": "34", "style.css": "36", "main.js": "40.0"}
    for name, ver in expect.items():
        assert vm.get(name) == {ver}, f"{name} 版本应为 {ver}，实为 {vm.get(name)}"
