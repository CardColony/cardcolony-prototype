# -*- coding: utf-8 -*-
"""《对话流照抄 dsh 完全版 v3》（2026-10-17）前端改动的机械守卫。

前端没有 pytest DOM 载体：DOM 行为归 jsdom 取证脚本
`_evidence_dsh_flow_v3_20261017.mjs`（输入 / 排队 / 视图 / 装配器 四组 54 条）；
状态机与装配器纯逻辑归 `tests/flow_machine.test.mjs`（12 条）与
`tests/assembler.test.mjs`（28 条）。本文件只做**源码级机械断言**，锁住本次重构
的关键契约、防回归：

- 输入状态机与提交策略必须存在且按 dsh 原文分支（flow/machine.js、submission-policy.js）；
- 运行中输入**不得被锁**（disabled 判据只有 dsh 的五条）；
- 忙时 Enter 走 queue（排队），主键两态；
- 扁平节点表：order + nodes:Map，没有轮分组三道；
- 24px 跟随 / 回底钮 / 运行条 15s 时钟；
- 退役清单里的自创物**必须已删除**（不是注释掉）；
- 版本戳：全链每模块唯一 URL。

★ 历史：本文件在 2026-10-16 锁的是「轮分组 + 组视图增量装配」那套（T1–T6）。
  2026-10-17 的 v3 任务书 §三 明确退役轮分组，改 dsh 的扁平节点表 ⇒ 本文件
  同步改写；旧断言（ensureGroupView / gv.views / renderGroup / pruneGroups）
  **随实现一起退场**，不是放松。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STREAM = ROOT / "static" / "stream.js"
WS = ROOT / "static" / "workspace.js"
ASSEMBLER = ROOT / "static" / "esm" / "assembler.js"
STYLE = ROOT / "static" / "style.css"
CARD_HTML = ROOT / "templates" / "card.html"
FLOW = ROOT / "static" / "esm" / "flow"


def _strip_js_comments(src: str) -> str:
    """去掉 /* */ 与 // 注释：源码级断言不能被注释里的旧词条带偏。"""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def _brace_block(src: str, marker: str) -> str:
    """从 marker 之后第一个 `{` 起做花括号配平（用于非 function 的块，如事件绑定）。"""
    i = src.index(marker)
    j = src.index("{", i)
    depth = 0
    k = j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise AssertionError(f"块未闭合：{marker}")


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


# ---------------------------------------------------------------- T1 移植件在场

def test_t1_flow_modules_exist():
    """§二 文件级对照表：dsh 的文件边界原样保留在 static/esm/flow/。"""
    expect = [
        "machine.js", "submission-policy.js", "composer-submission.js",
        "input-bar.js", "queue-dock.js", "queue-store.js",
        "chat-view.js", "message-item.js",
    ]
    for name in expect:
        assert (FLOW / name).exists(), f"缺移植件 flow/{name}"
    # dsh 的 queue/store.ts 是纯投影；本机无 Host 队列 ⇒ 适配层落地为 queue-store.js
    assert "createQueueStore" in (FLOW / "queue-store.js").read_text(encoding="utf-8")


def test_t1_machine_phases_match_dsh():
    """状态机相位集＝dsh 原文四相位（plain/claimed/adjudicating/submitting）。"""
    src = _strip_js_comments((FLOW / "machine.js").read_text(encoding="utf-8"))
    for phase in ("plain", "claimed", "adjudicating", "submitting"):
        assert f'"{phase}"' in src, f"相位 {phase} 缺失"
    # 忙时 Enter 丢弃（原文 :461）
    body = _fn_body(src, "onEnter")
    assert 'if (this.phase === "adjudicating" || this.phase === "submitting") return []' in body, \
        "忙时 Enter 必须丢弃本次（不吞草稿、不报错）"
    # 任何相位都可编辑：onDraftChanged 不得有相位早退
    draft_body = _fn_body(src, "onDraftChanged")
    assert "this.phase" not in draft_body, "onDraftChanged 不得做相位检查（dsh 原文无）"
    # 发送成功清撤销栈
    committed = _fn_body(src, "onSendCommitted")
    assert "this.log = []" in committed, "发送成功必须清撤销栈（已发内容不得复活）"


def test_t1_submission_policy_resolve_branches():
    """resolve() 四条分支逐条对应 submission-policy.ts:48-57。"""
    src = _strip_js_comments((FLOW / "submission-policy.js").read_text(encoding="utf-8"))
    body = _fn_body(src, "resolve")
    assert 'if (!running || !steeringAvailable) return "queue"' in body, \
        "不忙 / 不支持 steer ⇒ queue"
    assert 'if (gesture === "enter") return preferred' in body, "普通 Enter ⇒ 偏好"
    assert 'return preferred === "queue" ? "steer" : "queue"' in body, "加速键 ⇒ 反向"
    assert 'DEFAULT_BUSY_ENTER_BEHAVIOR = "queue"' in \
        (FLOW / "composer-submission.js").read_text(encoding="utf-8"), "默认偏好＝queue"


# ---------------------------------------------------------------- T2 输入条

def test_t2_disabled_excludes_running():
    """disabled 判据只有 dsh 的五条；**运行中不在其中**。"""
    src = _strip_js_comments((FLOW / "input-bar.js").read_text(encoding="utf-8"))
    body = _fn_body(src, "isDisabled")
    assert "removed" in body and "live" in body and "blocked" in body and "parentOffline" in body, \
        "disabled 五条判据缺一"
    assert "running" not in body, "★ disabled 判据不得含运行中（Running input stays free）"


def test_t2_primary_toggles_and_enter_queues():
    """主键两态（发送↔停止）＋ 运行中 Enter 排队 ＋ 防连发 ＋ IME。"""
    src = _strip_js_comments((FLOW / "input-bar.js").read_text(encoding="utf-8"))
    prim = _fn_body(src, "refreshPrimary")
    assert "primaryStops" in prim and "s.running" in prim, "主键必须按 running 出两态"
    key = _brace_block(src, 'ta.addEventListener("keydown"')
    assert "if (e.repeat) return" in key, "★ 防连发：按住 Enter 不得机关枪"
    assert "isComposing" in key and "keyCode === 229" in key, "★ IME 双保险（isComposing / 229）"
    assert "shiftKey" in key, "Shift+Enter 必须先于 IME 判据（恒为换行）"
    assert "policy.resolve" in key, "Enter 的投递方式必须走 submission-policy"
    # 运行中 Enter ⇒ queue：由 resolve(running=true, enter, true) 给出
    assert "!!snap().running" in key, "Enter 必须把 running 交给策略解析"


def test_t2_queue_dock_shape():
    """排队条：空则隐藏、单条一行、多条折叠计数、可编辑可删。"""
    src = _strip_js_comments((FLOW / "queue-dock.js").read_text(encoding="utf-8"))
    assert "queue.length === 0" in src and 'root.style.display = "none"' in src, "空队列必须不渲染"
    assert "listVisible" in src and "expanded" in src, "多条折叠判据缺一"
    assert "flow-queue-editor" in src, "编辑态输入框缺"
    assert 'kind: "remove"' in src and 'kind: "edit"' in src, "队列动作缺一"
    assert "collapsed = true" in src, "默认收起（QueueDock.tsx:38）"


def test_t2_queue_autosend_after_turn():
    """轮结束自动依序发出：send 的 finally 必须出队。"""
    body = _fn_body(_strip_js_comments(WS.read_text(encoding="utf-8")), "async function send")
    assert "drainQueue(" in body, "send 收口必须调 drainQueue（轮结束自动发出）"
    assert "finally" in body, "出队必须在 finally 里（成功失败都要走）"
    drain = _fn_body(_strip_js_comments(WS.read_text(encoding="utf-8")), "function drainQueue")
    assert "dequeue()" in drain and "send()" in drain, "drainQueue 必须出队并重发"


def test_t2_selfheal_poll_retired():
    """15 秒自愈轮询已退役（任务书 §三）。"""
    src = _strip_js_comments(WS.read_text(encoding="utf-8"))
    assert "}, 15000)" not in src, "★ 15 秒自愈轮询必须已删除"
    assert "createWaitingBubble" not in src, "★ 等待气泡必须已删除（不是注释掉）"
    assert "inputBox.disabled" not in src, "★ 运行中锁输入框必须已删除"


# ---------------------------------------------------------------- T3 扁平节点表

def test_t3_assembler_is_flat():
    """装配器快照＝{order, nodes:Map}；没有轮分组三道。"""
    raw = ASSEMBLER.read_text(encoding="utf-8")
    asm = _strip_js_comments(raw)
    assert "snapCache = { order, nodes: byKey, rev: structRev }" in asm, \
        "快照形状必须是 {order, nodes, rev}"
    assert "rounds" not in asm, "★ 轮分组（snap.rounds）必须已删除"
    assert "groupKeyOf" not in asm, "★ 分组键必须已删除"
    assert "adoptLocalGroup" not in asm, "★ 组改名认领必须已删除（扁平表用 claimed 标记）"
    assert "claimed = true" in asm, "认领必须落 claimed 标记"
    assert "if (snapCache && snapCacheRev === structRev) return snapCache" in asm, \
        "结构未变必须直接返回缓存引用"


def test_t3_renderer_is_flat_and_incremental():
    """渲染层：order 落位、按 rev 只换那一行；组视图三件已删。"""
    src = _strip_js_comments(STREAM.read_text(encoding="utf-8"))
    assert "createChatView" in src, "视图骨架必须来自 flow/chat-view"
    assert "function viewOf" in src, "必须有每会话视图入口"
    assert "v.render(win, revGetter(snap))" in src, "全序落位必须走 chat-view.render"
    assert "v.refresh(k, rev)" in src, "内容变化必须只换那一行"
    assert "requestAnimationFrame(flush)" in src, "逐字更新必须按帧合并（dsh animation-frame）"
    for dead in ("ensureGroupView", "renderGroup", "pruneGroups", "lanesOf",
                 "paintToggle", "msgs-round",
                 # [5702 · C-1 2026-10-20] 旧 live 旁路（吞字根因）整段退役：
                 # 正文改为按 run:round 进装配器，轮界不再清字。
                 "appendChunk", "clearText", "ensureLiveSlot", "liveSlot(", "scheduleMd"):
        assert dead not in src, f"★ 退役物 {dead} 仍在 stream.js"
    # 整列清空重建＝P2/P3 的根：全文件不得再有「清空列」的写法
    assert "while (box.firstChild)" not in src, "仍在整列清空重建"
    assert "scrollBottom" not in src, "★ 每帧强制滚底必须已删除（换 24px 跟随）"


def test_t3_chat_view_follow_and_status():
    """24px 跟随 ＋ 回底钮 ＋ 运行条 15s 时钟。"""
    src = _strip_js_comments((FLOW / "chat-view.js").read_text(encoding="utf-8"))
    assert "const FOLLOW_THRESHOLD = 24" in src, "★ 跟随阈值必须是 24px"
    assert "observedTop" in src, "必须有「程序写入台账」（程序滚底不夺权）"
    assert "flow-view-to-bottom" in src, "回底钮缺"
    assert "Deep diving..." in src, "运行条文案必须是 Deep diving..."
    assert "15000" in src, "★ 时钟必须 15 秒后才显"
    assert "captureAnchor" in src and "anchor" in src, "上翻保位锚点缺"


# ---------------------------------------------------------------- T4 视觉原值

def test_t4_visual_values_are_dsh_originals():
    """748px 列 / 气泡 min(525px,82%) / 22px 圆角 / 16px 节拍 / 输入卡 22px。"""
    css = STYLE.read_text(encoding="utf-8")
    assert "max-width: 748px" in css, "★ 内容列必须是 748px（dsh --dsh-chat-content-width）"
    assert "min(525px, 82%)" in css, "★ 用户气泡上限必须是 min(525px, 82%)（dsh 原值）"
    m = re.search(r"\.flow-user-bubble\s*\{[^}]*\}", css)
    assert m, "缺 .flow-user-bubble"
    blk = m.group(0)
    assert "border-radius: 22px" in blk, "气泡圆角必须是 22px"
    assert "padding: 10px 16px" in blk, "气泡内距必须是 10px 16px"
    assert "font-size: 16px" in blk and "line-height: 24px" in blk, "16/24 节拍缺"
    card = re.search(r"\.flow-input-card\s*\{[^}]*\}", css)
    assert card and "border-radius: 22px" in card.group(0), "输入卡圆角必须是 22px"
    assert "--flow-composer-text-max-height: 336px" in css, "草稿盒高度上限必须是 336px（14×24）"


def test_t4_bubble_cap_now_adopted():
    """★ 口径变更（2026-10-17）：dsh 的 525px 气泡上限**已采纳**。

    2026-10-16 的旧断言写「未采纳：与『我的发言永不超出模型消息边界』冲突，留用户裁」。
    2026-10-17 的 v3 任务书 §一.6 与 §五「视觉」明确要求 `min(525px, 82%)` ⇒ 采纳。
    本断言锁住「已采纳」，防有人按旧口径又改回去。
    """
    css = STYLE.read_text(encoding="utf-8")
    assert "min(525px, 82%)" in css, "v3 任务书要求采纳 dsh 气泡上限；若回退请同步改本断言并说明"


def test_t4_input_card_structure():
    """输入卡＝一个滚动口 + 两层文字（backdrop 出字 / textarea 透明留光标 / mirror 定高）。"""
    src = (FLOW / "input-bar.js").read_text(encoding="utf-8")
    for cls in ("flow-input-scroll", "flow-input-grow", "flow-input-backdrop",
                "flow-input-mirror", "flow-input-ta", "flow-input-card"):
        assert cls in src, f"输入卡缺 {cls}"
    css = STYLE.read_text(encoding="utf-8")
    assert "color: transparent" in css, "textarea 必须透明（出字靠 backdrop）"
    assert "caret-color" in css, "光标色必须显式给（透明字下没有光标就看不见）"


# ---------------------------------------------------------------- T5 接线

def test_t5_workspace_wired_to_flow():
    """workspace 的输入区已整件交给 flow/，且旧入口不再存在。"""
    src = _strip_js_comments(WS.read_text(encoding="utf-8"))
    assert "createInputBar" in src and "createQueueDock" in src, "workspace 未接 flow 输入系统"
    assert "ComposerSubmissionPolicy" in src, "workspace 未建提交策略"
    body = _fn_body(src, "function setRunState")
    # [运行态单一真相源 2026-10-21] 运行态拆成两半：setRunState＝回写事件流，
    # applyRunState＝更新本文件投影（输入条/排队条）。两半都必须存在。
    assert "EventFlow.setRunning" in body and "applyRunState" in body, \
        "setRunState 必须落到事件流运行条（并经 applyRunState 更新投影）"
    proj = _fn_body(src, "function applyRunState")
    assert "inputBar.refresh()" in proj, "运行态投影变化必须刷新输入条"
    html = CARD_HTML.read_text(encoding="utf-8")
    assert 'id="composerSeat"' in html, "card.html 缺输入座位"
    assert 'id="inputBox"' not in html, "★ #inputBox 必须已退役"
    assert 'id="runBtn"' not in html, "★ #runBtn 必须已退役（主键由 input-bar 生成）"
    assert 'id="draftBtn"' in html and 'id="statusDraftBtn"' in html, "项目自有起草键不得丢"


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
    """版本基线（对话流照抄 dsh v3 终态）。

    纪律不变：改哪个模块哪个升号、全链每模块唯一 URL。

    ★ 历史（前序）：
      [对话流画板架构 T7 2026-10-16] 统一升 35；
      [更新现状按钮改造 2026-10-16] stream/workspace/style/canvas 升 36、main 36.0、assembler 34；
      [左栏新建项目 2026-10-16] nav_render 31、workspace 39、canvas 38、main 38.0；
      [惰性新建 2026-10-17] workspace 45、canvas 43、rule_panel 31、nav_system 33、home 1.2、
        main 43.0、main_chat 1.2、nav.css 13。
    ★ 本轮（v3）：
      stream.js 36→37（渲染层换扁平节点表）、workspace.js 45→46（输入系统整件改 flow/）、
      assembler.js 34→35（扁平 order/nodes）、style.css 37→38（flow 样式段）、
      main_chat.js 1.2→1.3（它 import 了 stream/workspace）；
      canvas.js / main.js 本轮**未涉改**（canvas→workspace 的边已在阶段2 拆除）⇒ 不升号。
      flow/* 新建 ⇒ 1。
    ★ [建项目流程 2026-10-17] workspace 46→48（草稿项目装配；二次因 chat-view 初始态修正
      连带升 stream 37→38）、canvas 43→44（画布页＋改整页跳转 /work/draft）、
      main 43.0→45.0、main_chat 1.3→1.5、chat-view 1→2（初始态与无更多一致）；
      阶段3 退役草稿画布路径：canvas 44→45（IS_DRAFT 整件删除）、nav.css 13→14
      （.draft-badge 删除）。
    ★ [＋按钮删除 2026-10-17] 命令面不存在 ⇒ input-bar 的 `+` 按钮整件删（死件纪律，
      不取原文 :741 disabled 分支）：input-bar 1→2、workspace 51→52、main_chat 1.8→1.9、
      style 38→39（.flow-input-add 死样式同删）。
    ★ [画布 2026-10-19 · 实时状况/并行操作] canvas 62→72、main 62.0→72.0：
      新增 /pulse 轮询、卡面运行态、项目动态抽屉、多选批量下指令与排队态、
      全局视图图例与适配修正。canvas.css 19→20（同步升）。
    ★ [画布 5711 · 画布优化] canvas 72→77、main 72.0→77.0、canvas.css 20→23、
      build-stamp v72→v79、nav.css 15→16（nav.css 是并入 dev 的角色机制那条线）：
      ① 两阶段渲染改 try/finally（异常不再留下永久白屏）；② 骨架屏改在渲染完成后
      才移除（首屏不再白一下）；③ 浮层级联修复（display 收回 .active——原来恒为
      flex，浮层永久可见且吃掉左上角点击）；④ 选中集与在场节点求交（不再删掉
      看不见的卡）；⑤ 连线分层读 projection.depthOf（原来读 vm.depthOf 恒 undefined，
      深度样式从未生效）；⑥ 新增工作级实时更新（订阅 /api/works/{w}/events，
      写卡后**就地**换掉那一张卡，不重建整树）；⑦ 首屏适配重试机制（_autoFitPending
      原来恒 false，机制从未跑过）。
    ★ [画布 5711 · 新建卡弹窗统一] canvas 82→83、main 82.0→83.0、build-stamp v83→v84：
      "新建卡片"原来自制 inline-style 小面板（另一套交互语言），本轮并入 cvDialog
      （与重命名/删除同一套）：顺带修掉一个真 bug——旧面板的 Escape 只 remove 自己、
      事件继续冒泡 ⇒ 关弹窗的同时把画布选中的卡也清了。
    ★ [性能 2026-10-20 · 画布与对话流] canvas.js 76→77、canvas.css 21→22、
      main.js 73.4→77.0、stream.js 50→51、api.js 40→41：
      ① 画布 11 处 infinite 动画 60fps→10fps（steps）；cv-comm-dot 的 r 几何动画
        改 transform:scale；通信线拆独立 <svg id=commSvg>（实测空闲 CPU 117~159%→55~69%）。
      ② 对话流分页：/timeline 新增 tail/before/has_earlier，打开卡片只拉尾部 400 条
        （card_004 全量 1964 条→400 条，装配节点 1185→约 200），往上翻再补更早一段。
      api.js 被六个模块 import ⇒ 全链同升 41（双版本断言会抓）。
    ★ [画布 5711 · P0 修"画布永久空白"] canvas 83→84、main 83.0→84.0、build-stamp v84→v85：
      ① 画布"找根卡"不再 await /api/works（它挡在出图关键路径上，浏览器 6 连接上限
      一被常驻 SSE 占满就永远排队 ⇒ 画布永久空白）；根卡改从画布数据里取，零往返。
      ② 去掉一条**监听了错键**的常驻 SSE（card_id 位置传的是 work_id）⇒ 画布页
      常驻长连接 2 条降为 1 条，未读角标的增量通道改由工作级流带（原来从没活过）。
    ★ [换父 2026-10-20 · 5718] canvas 77→78、main 73.4→78.0、canvas.css 22→23、
      build-stamp v73→v74：拖拽语义从「调同层顺序」改成「改父子归属」（拖到卡上＝
      挂到它下面，拖到空白/非法目标＝回弹），新增落点高亮 .drop-ok/.drop-bad。
    """
    vm = _version_map()
    expect = {
        # [合并 2026-10-20] 两侧各改各的：对话流那条线（5702/5710/5715）动了
        # stream/workspace/assembler/style/main_chat/flow/*；画布 5711 这条线动了
        # canvas.js 与 main.js（main.js 改了 import canvas.js 的版本戳 ⇒ 自身戳也升）。
        # 版本戳处置按 5715 那次的规程：**在 dev 的编号基础上重算**，不是逐文件取大
        # ⇒ 对话侧全取 dev 的号；画布链取 dev 的号 +1（canvas.js 75→76、
        # canvas.css 20→21、main.js 73.3→73.4、build-stamp v72→v73）。
"stream.js": "53", "workspace.js": "76", "canvas.js": "83",
        "assembler.js": "40", "style.css": "49", "main.js": "78.5",
        "main_chat.js": "4.8",
        "machine.js": "1", "submission-policy.js": "1", "composer-submission.js": "1",
        "input-bar.js": "5", "queue-dock.js": "3", "queue-store.js": "2",
        "chat-view.js": "7", "message-item.js": "1",
    }
    for name, ver in expect.items():
        assert vm.get(name) == {ver}, f"{name} 版本应为 {ver}，实为 {vm.get(name)}"


# ---------------------------------------------------------------- 输入会话按卡

def test_composer_state_is_per_card():
    """[5702 · 2026-10-20] 输入会话按卡隔离。

    防回退：一旦有人把 machine/queue 改回全局单例，切卡时草稿/排队/运行态
    又会跟着走（用户报的「输入框文字被带走」）。
    """
    ws = _strip_js_comments(WS.read_text(encoding="utf-8"))
    assert "composerSessions" in ws, "缺按卡输入会话表"
    assert "function switchComposer" in ws, "缺切卡换绑入口"
    assert "inputBar.bindSession(" in ws, "切卡必须换绑 machine"
    assert "queueDock.setStore(" in ws, "切卡必须换绑队列"

    assert "switchComposer(" in _fn_body(ws, "async function open"), \
        "open() 未换绑输入会话（点下去草稿必须立刻按卡切换）"
    send_body = _fn_body(ws, "async function send")
    assert "sendingMachine" in send_body, "send() 未捕获发起时的 machine"
    assert "commitSent(sendingMachine)" in send_body, "commitSent 未绑定发起会话"
    assert "commitSent()" not in ws, "★ 仍有不带目标的 commitSent()（切卡后会清错卡）"

    bar = _strip_js_comments((FLOW / "input-bar.js").read_text(encoding="utf-8"))
    assert "let machine = new InputMachine" in bar, "machine 必须可换绑（let）"
    assert "bindSession(next)" in bar, "input-bar 缺 bindSession"
    dock = _strip_js_comments((FLOW / "queue-dock.js").read_text(encoding="utf-8"))
    assert "setStore(next)" in dock, "queue-dock 缺 setStore"


def test_nav_project_row_click_uses_single_identity_gate():
    """[当前卡单源 2026-10-21] 「是不是当前这张卡」只许问一个口：select.is(wid, cid)。

    实测 5700 的 12 个项目**根卡 id 全是 card_001**；原来项目行早退拼了三个条件
    （在对话页 且 当前卡存在 且 卡号相同）却少比项目号 ⇒ 点 B 项目被判成"已经在看"，
    毫无反应（用户报「没办法从 A 项目直接点到 B 项目的根卡上」）。
    """
    nav = (ROOT / "static" / "esm" / "nav_system.js").read_text(encoding="utf-8")
    assert "navStore.select.is(phead.dataset.wid, rootCid)" in nav, \
        "项目行早退必须走 select.is(项目, 卡)"
    assert "cur().meta.card_id === rootCid" not in nav, \
        "不许再手拼「卡号相同」的判断（漏项目号）"
    # 跨项目跳转走页面身份路径，不再拼旧 hash
    assert '"/work/" + wid + "/card/" + cid' in nav
    assert '"/work/" + wid + "#" + cid' not in nav

    # 身份源：state_layers 的 select 提供唯一判断口，get() 只读路径
    st = (ROOT / "static" / "esm" / "state_layers.js").read_text(encoding="utf-8")
    assert "function selectIs(wid, cid)" in st, "缺唯一判断口 select.is"
    assert "is: selectIs" in st
    assert "parseHash(loc.hash)" not in st, \
        "get() 不该再回退读 hash（画布页会误判成「有当前卡」）"


def test_nav_project_row_click_uses_single_identity_gate():
    """[当前卡单源 2026-10-21] 「是不是当前这张卡」只许问一个口：select.is(wid, cid)。

    实测 5700 的 12 个项目**根卡 id 全是 card_001**；原来项目行早退拼了三个条件
    （在对话页 且 当前卡存在 且 卡号相同）却少比项目号 ⇒ 点 B 项目被判成"已经在看"，
    毫无反应（用户报「没办法从 A 项目直接点到 B 项目的根卡上」）。
    """
    nav = (ROOT / "static" / "esm" / "nav_system.js").read_text(encoding="utf-8")
    assert "navStore.select.is(phead.dataset.wid, rootCid)" in nav, \
        "项目行早退必须走 select.is(项目, 卡)"
    assert "cur().meta.card_id === rootCid" not in nav, \
        "不许再手拼「卡号相同」的判断（漏项目号）"
    # 跨项目跳转走页面身份路径，不再拼旧 hash
    assert '"/work/" + wid + "/card/" + cid' in nav
    assert '"/work/" + wid + "#" + cid' not in nav

    # 身份源：state_layers 的 select 提供唯一判断口，get() 只读路径
    st = (ROOT / "static" / "esm" / "state_layers.js").read_text(encoding="utf-8")
    assert "function selectIs(wid, cid)" in st, "缺唯一判断口 select.is"
    assert "is: selectIs" in st
    assert "parseHash(loc.hash)" not in st, \
        "get() 不该再回退读 hash（画布页会误判成「有当前卡」）"


def test_nav_delete_card_is_not_a_dead_button():
    """[删卡 2026-10-21] 用户报「左侧 ⋯ 里的删除是无效按钮」。

    三处都要成立：
      ① 置灰的删除被点时**说清原因**（原来静默 return ⇒ 点了完全没反应）；
      ② 删的是当前卡 ⇒ 回项目画布页（原来只 replaceState 去 hash，而 hash 早没了
         ⇒ 等于什么都没做，页面停在指向已删卡的死地址）；
      ③ 画布页删卡 ⇒ 通知宿主就地重建（只 renderNav 的话画布上那张卡还挂着）。
    """
    nav = (ROOT / "static" / "esm" / "nav_system.js").read_text(encoding="utf-8")
    assert "项目根卡不能删" in nav and "还有子卡" in nav, "置灰的删除必须给出原因"
    assert 'location.href = "/work/" + wid' in nav, "删当前卡应回项目画布页"
    assert "cardsChanged()" in nav, "删卡后要通知宿主重建（画布页）"
    assert "onCardsChanged" in nav, "缺「卡数据变了」注入口"

    main_js = (ROOT / "static" / "esm" / "main.js").read_text(encoding="utf-8")
    assert "reloadCanvas" in main_js and "onCardsChanged" in main_js, \
        "画布页必须注入 reloadCanvas 作为重建回调"


def test_nav_delete_card_is_not_a_dead_button():
    """[删卡 2026-10-21] 用户报「左侧 ⋯ 里的删除是无效按钮」。

    三处都要成立：
      ① 置灰的删除被点时**说清原因**（原来静默 return ⇒ 点了完全没反应）；
      ② 删的是当前卡 ⇒ 回项目画布页（原来只 replaceState 去 hash，而 hash 早没了
         ⇒ 等于什么都没做，页面停在指向已删卡的死地址）；
      ③ 画布页删卡 ⇒ 通知宿主就地重建（只 renderNav 的话画布上那张卡还挂着）。
    """
    nav = (ROOT / "static" / "esm" / "nav_system.js").read_text(encoding="utf-8")
    assert "项目根卡不能删" in nav and "还有子卡" in nav, "置灰的删除必须给出原因"
    assert 'location.href = "/work/" + wid' in nav, "删当前卡应回项目画布页"
    assert "cardsChanged()" in nav, "删卡后要通知宿主重建（画布页）"
    assert "onCardsChanged" in nav, "缺「卡数据变了」注入口"

    main_js = (ROOT / "static" / "esm" / "main.js").read_text(encoding="utf-8")
    assert "reloadCanvas" in main_js and "onCardsChanged" in main_js, \
        "画布页必须注入 reloadCanvas 作为重建回调"
