# -*- coding: utf-8 -*-
"""评审窗口的前端接线（[2026-10-29 用户口径，第四次修正]）——源码级钉子。

为什么不跑 DOM：`workspace.js` 是整页入口（import 即装配导航/事件流/输入卡），
jsdom 里起不来；这里要防的是「接线掉了」这类回归，源码级断言足够。

用户原话（逐条钉住）：
> 「评审不激活的时候不显示」
> 「你是不是缩小了正卡对话区域的宽度？就为了你这张浮窗？」
> 「你摆在正卡信息流右侧，空间是足足够够的」
> 「不是真的浮一个窗口在所有信息上方，它的层级和卡片信息流是同级的」
> 「右侧边栏展开，应该盖住它」
> 「点击它展开不是向右展开，是向左展开，挤压卡片本身的信息流，给它留出空间，而且是充分展开」
> 「收缩状态不需要显示那么多信息，显示最近的一条就足够了，永远显示最近的一条，动态的更新」
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _css_block(selector: str) -> str:
    css = _read("static/ui.css")
    i = css.index(selector)
    return css[i:css.index("}", i)]


# ---------------------------------------------------------------- 1. 不激活不显示
def test_window_hidden_by_default_and_only_batch_turns_it_on():
    html = _read("templates/card.html")
    seg = html[html.index('id="rvWin"'):html.index('id="rvWin"') + 300]
    assert "hidden" in seg, "窗口默认必须 hidden（没有批次时界面上什么都不多）"

    js = _read("static/workspace.js")
    assert "if (!st || !st.has_review || !st.active) { rvSetOn(false); return; }" in js
    assert "if (!on) { el.hidden = true; rvStop();" in js
    assert "RV.closedBatch" in js, "用户手动关掉的批次不再自动弹"


# ---------------------------------------------------------------- 1b. 抽屉不许推正文
def test_inspector_is_overlay_so_card_body_never_moves():
    """用户：「右侧边栏的展开，应该不动卡片正文信息，现在正文信息会随右侧边栏
    展开而移动，根本没有必要」

    病根：抽屉原来是 flex 子项（flex:none + width 0→348）⇒ 展开时 .ws-right 变窄
    ⇒ 正文列在更窄的空间里重新居中 ⇒ 左移。它浮在右侧空白上、不压正文，
    不该让正文动 ⇒ 改成绝对定位浮层，.ws-right 宽度恒定。
    """
    css = _read("static/ui.css")
    seg = _css_block("body.page-chat .inspector {")
    assert "position: absolute" in seg, "抽屉必须是浮层（占布局就会推正文）"
    assert "right: 0" in seg
    assert "width: 0" in seg, "收起时不占任何宽度"


# ---------------------------------------------------------------- 2. 不动对话区宽度
def test_compact_state_never_touches_the_column_width():
    """用户：「你是不是缩小了正卡对话区域的宽度？就为了你这张浮窗？」

    收缩态是落在对话列**右侧空白带**里的，绝不能给对话区加宽度约束。
    让位只允许发生在展开态（rv-full）。
    """
    css = _read("static/ui.css")
    # 任何给 .workspace / .ws-right 让位的规则都必须挂在 rv-full 下
    for line in css.splitlines():
        s = line.strip()
        if ("padding-right" in s or "right:" in s) and ".workspace" in s:
            assert "rv-full" in s, f"收缩态不许动对话区：{s}"
    assert ".workspace { right: 320px" not in css


def test_window_is_a_window_not_a_sidebar():
    css = _read("static/ui.css")
    block = _css_block("body.page-chat .fwin {")
    assert "position: fixed" in block
    assert "border-radius" in block, "四角圆——是窗口不是贴边的列"
    assert "box-shadow" in block, "有投影"
    assert "bottom:" not in block, "**不顶天顶地**"


def test_layer_is_below_the_inspector():
    """用户：「不是真的浮一个窗口在所有信息上方，它的层级和卡片信息流是同级的」
    「右侧边栏展开，应该盖住它」"""
    css = _read("static/ui.css")
    win = _css_block("body.page-chat .fwin {")
    assert "z-index: 55" in win
    assert "body.page-chat .inspector { z-index: 70; }" in css, \
        "inspector 必须高于窗口（它展开时盖住窗口）"


def test_expand_squeezes_body_but_inspector_never_does():
    """两条**不同**规则，别混：

    · 评审窗口展开 ⇒ 挤正文（用户：「挤压卡片本身的信息流，给它留出空间，
      而且是充分展开」）—— 正文变窄但仍在。
    · 卡详情抽屉展开 ⇒ **不动正文**（用户：「右侧边栏的展开，应该不动卡片正文信息，
      现在正文信息会随右侧边栏展开而移动，根本没有必要」）。
    """
    css = _read("static/ui.css")

    # 评审窗口：展开挤正文
    assert "rv-full .ws-right {" in css
    seg = _css_block("body.page-chat.rv-full .ws-right {")
    assert "padding-right" in seg, "评审窗口展开要挤正文"
    full = _css_block('body.page-chat .fwin[data-mode="full"] {')
    assert "width: var(--rv-full-w)" in full, "充分展开"
    assert "--rv-full-w: min(56vw, 900px, calc(100vw - 860px))" in css, \
        "给正文留 500px 下限"
    # 但不许把正文藏掉
    assert "visibility: hidden" not in css

    # 抽屉：浮层，不参与布局
    insp = _css_block("body.page-chat .inspector {")
    assert "position: absolute" in insp

    # iframe 里不要再出现第二条左栏
    js = _read("static/workspace.js")
    assert ".app-nav{display:none!important}" in js


# ---------------------------------------------------------------- 3. 收缩只留最近一条
def test_compact_shows_scrolling_event_list():
    """[2026-10-31 用户令]「为什么所有动作、思考、事件都不显示在界面上？」

    这条**取代**了 2026-10-29 的旧口径「收缩状态…永远显示最近的一条」——
    旧实现每条新事件把上一行**覆盖**掉，用户看不到过程。
    现在：已完成事件**追加**进滚动列表（rvEvents），底部一行显示正在逐字输出的内容。
    """
    html = _read("templates/card.html")
    assert 'id="rvEvents"' in html, "收缩态要有滚动事件列表"
    assert 'id="rvLastText"' in html, "底部实时行仍在（逐字流）"
    js = _read("static/workspace.js")
    assert "function rvPush(" in js, "事件要追加进列表"
    # [2026-10-31 用户令] 不许为了性能**删数据**；只能限制渲染条数／分页。
    assert "removeChild(box.firstChild)" not in js, "不许删事件（数据只增不删）"
    assert "RV_PAGE" in js, "渲染按页来"
    assert "RV.events.push" in js, "全部事件要留在内存数组里"
    assert "rvRenderTail" in js, "只渲染尾部 RV.shown 条"
    assert "RV.shown += RV_PAGE" in js, "滚到顶要能加载更多（分页）"
    assert "上面还有" in js, "要提示上方还有多少条"
    assert "box.scrollTop = box.scrollHeight" in js, "新事件要自动滚到最新"
    assert "function rvSetLast(" in js and "function rvTyping(" in js, "实时行仍走 rvSetLast"
    # rvPaint 必须走 rvPush（追加），不能只写实时行
    i = js.index("function rvPaint(")
    seg = js[i:i + 2600]
    assert "rvPush(html, ts, _cls)" in seg, "rvPaint 要追加进列表"
    css = _read("static/ui.css")
    assert "body.page-chat .fwin-events {" in css, "列表容器要有自己的样式"
    assert "overflow-y: auto" in css, "列表要能滚"
    assert "body.page-chat .fwin-live {" in css


def test_latest_line_covers_all_event_kinds():
    """「所有事件」：调模型/调工具/工具返回/开始发言/改稿/收口都要有文案。"""
    js = _read("static/workspace.js")
    for t in ("review_round_start", "review_speaking", "tool_call", "tool_result",
              "review_spoke", "review_revise", "review_all_passed",
              "review_batch_closed", "error"):
        assert t in js, t
    assert "default:" in js and "rvEsc(t)" in js, "没单独排版的事件照示类型名"


def test_backend_emits_tool_roundtrip_in_review():
    runner = _read("src/api/review_runner.py")
    assert "_emit_tool_call" in runner and "_emit_tool_result" in runner
    assert runner.count("_emit_tool_call(") >= 4, "speak/revise 两处工具分支都要发"
    routes = _read("src/api/routes.py")
    assert '"target_title": target_title' in routes
    catalog = _read("config/event_catalog.json")
    assert '"review_speaking"' in catalog


# ---------------------------------------------------------------- 4. 打开先补最近一条
def test_window_replays_history_before_subscribing():
    """`from=0` 的 SSE 只推新事件 ⇒ 打开时要用 timeline 补最近一条。"""
    js = _read("static/workspace.js")
    assert "async function rvLoadHistory()" in js
    assert "getTimeline" in js
    i = js.index("await rvLoadHistory();")
    j = js.index("rvSubscribe();", i)
    assert j > i, "必须先补历史再订阅"


# ---------------------------------------------------------------- 5. 展开＝同一份对话页
def test_expand_loads_the_same_card_page():
    js = _read("static/workspace.js")
    assert "/work/${WORK_ID}/card/${RV.rid}" in js


# ---------------------------------------------------------------- 6. 左栏临时入口
def test_nav_has_temporary_review_entry():
    render = _read("static/esm/nav_render.js")
    assert 'row.kind === "review"' in render and "buildReviewEl" in render
    assert "p.reviews" in render
    nav = _read("static/esm/nav_system.js")
    assert 'closest("[data-review]")' in nav
    model = _read("static/esm/row_model.js")
    assert 'this.kind === "review"' in model


def test_nav_backend_only_returns_active_reviews():
    src = _read("src/storage/review_store.py")
    assert "async def list_active" in src
    assert 'if not isinstance(st, dict) or not str(st.get("batch") or ""):' in src
    routes = _read("src/api/routes.py")
    assert '"reviews": _reviews' in routes
    assert "review-status" in routes
