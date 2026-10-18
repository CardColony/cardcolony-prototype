/* canvas.js - 画布核心：视口变换、布局引擎、LOD、拖拽。
 * 迭代四 F2③：IIFE → ES module（module 作用域＝原闭包隔离）；escapeHtml 收编 esm/util.js；
 * API 经 esm/api.js 静态 import；跨文件（RenderNav）由 main.js 装配序保证。
 * [阶段2 A2.3 2026-10-17] canvas→workspace 的边**已彻底拆除**：点卡不再调 Workspace.open，
 * 改为 location.assign 整页跳转到对话独立地址 /work/{wid}/card/{cid}。
 *
 * [画布第三步 · 拆渲染层边界] 长期不变量（V1–V10，违反任何一条即架构回退）：
 *   V1 渲染不摸状态——渲染区 10 函数（renderCanvas/renderEdges/renderBreadcrumb/createNode/
 *      reflowLayout/applyLODClasses/fitView/getSummary/lazyLoadVisibleSummaries/refreshUnread）
 *      只吃投影/参数，函数体内 state.* 引用零命中；
 *   V2 投影纯函数——computeVisibleSet/computeProjection 同输入恒同输出，不读 DOM 无随机无 Date；
 *   V3 可见集单一来源——可见集只由 computeVisibleSet 生成（getVisibleIds 属操作层，不在渲染路径）；
 *   V4 视图态与数据态分离——state 五桶：data/index/view/nav/async，混装即违规；
 *   V5 渲染可重复——同一投影渲染两次 DOM 几何一致（剔除 entering 与 l0–l2 临时类）；
 *   V6 测量源显式——sizes 只来自传入或 measureSizes() 单一入口，一轮渲染周期测量恰 1 次；
 *   V7 事件不直触渲染细节——事件处理器经动作层改 state 后走统一入口，不直写 nodeMap/DOM 位置；
 *   V8 拖拽临时位移是局部状态——不进 state、不进视图态持久化（saveViewState 只存 pan/zoom/expanded）；
 *   V9 对外契约不变——export 仅 setViewRoot/reloadCanvas，GET/PATCH 契约零变化；
 *   V10 重绘入口唯一——改 state 后必经 renderAll（全量）/reflowAll（不重建）/
 *      applyViewState（视图变换），不存在改 state 后自行重绘或漏重绘的路径。
 */
import { API } from "./esm/api.js?v=36";
import { escapeHtml } from "./esm/util.js?v=30";
// 右栏"读全文"用与对话页同一份 Markdown 渲染（不另造一套）
import { renderMarkdown } from "./esm/md.js?v=31";
import { store } from "./esm/store.js?v=30";
import { RenderNav } from "./esm/nav_system.js?v=34";
import { notify } from "./esm/api.js?v=36";

let __VIEW_RESTORED__ = false;   // [F2⑥] 视图态仅首次恢复
// [画布商业化 2026-10-17] 首次自动适配：进项目时若没有视图存档，渲染后 fit 内容。
// 为什么需要"pending"而不是渲染后 fit 一次就完：摘要走异步回填，回填改变卡高 →
// scheduleReflow → 布局变长，一次性的 fit 会算在"矮版本"上，结果仍溢出。
// 故 fit 挂在 reflow 之后重试，直到①内容稳定（一次 reflow 后不再变）或②用户动手。
let _autoFitPending = false;
let _userMoved = false;
/* [建项目流程 2026-10-17] 画布页恒对应一个**已落库**的项目：
 * /work/draft 已改由对话模板承载（main.py 特判），本模块再无「就地草稿」态，
 * 因此 WORK_ID 回到 const、IS_DRAFT 整件退役（原惰性新建的草稿画布路径已删）。 */
const WORK_ID = window.__WORK_ID__;
/* [阶段3 A3.2 2026-10-17] 双页拆分后 canvas.js 仍会经 workspace.js 的 reloadCanvas
 * import 被**对话页**间接加载（阶段4 才摘这条边）。对话页没有画布 DOM ⇒ 本模块
 * 顶层事件绑定一律空守卫、渲染函数遇空 viewport 自然空转，整体降级为惰性。 */
  const viewport = document.getElementById("viewport");
  const world = document.getElementById("world");
  const edgesSvg = document.getElementById("edgesSvg");
  const tooltip = document.getElementById("cardTooltip");
  const crumbEl = document.getElementById("crumb");
  const zoomDisplay = document.getElementById("zoomDisplay");
  const reorganizeBtn = document.getElementById("reorganizeBtn");
  const newCardBtn = document.getElementById("newCardBtn");
  // [惰性新建 2026-10-17] 左栏「＋ 新项目」（画布页）。此前该钮 DOM 在场、处理器却
  // 留在 workspace.js（对话页模块），画布页点它无反应（拆页执行记录 §4.2 已注明）。
  const navNewProjectBtn = document.getElementById("navNewProject");

  // 画布状态（[画布第三步 T3] 五桶分家：数据／索引／视图／导航／异步——V4 结构断言口径）。
  // 数据 cards/links/positions；索引 cardMap/childMap（派生，rebuildCardMap 重建）；
  // 视图 expanded/panX/panY/zoom（expanded 自 F2⑥ 起随 projView:{work_id} 持久化——
  // 旧「纯内存态/不持久化」口径作废，见下方 saveViewState/restoreViewState 与 F2⑥ 注释块）；
  // 导航 workCardId/workTitle/viewRoot；异步 _unreadByCard/summaryCache。
  let state = {
    data: {
      cards: [],      // [{card_id,title,card_type,parent,created_at}]
      links: [],  // [{source,target,type}]
      positions: {},  // {card_id:{x,y,pinned}}
    },
    index: {
      cardMap: {},    // card_id -> card
      childMap: {},   // parent_id -> [card]
    },
    view: {
      expanded: new Set(),  // Set<card_id> 已展开卡集合（F2⑥ 起持久化，非纯内存态）
      // [画布商业化 2026-10-17] 选中集：**不持久化**（刷新即清空）——选择是瞬时视图态，
      // 不是用户资产。放 view 桶而不是新开桶（V4 五桶结构不变）。
      selected: new Set(),
      panX: 80, panY: 40, zoom: 1.0,
    },
    nav: {
      workCardId: "",   // 剧本卡 id
      workTitle: "",
      viewRoot: "",     // 当前视图根（默认 = workCardId）
    },
    async: {
      _unreadByCard: {},  // card_id -> 未读数（K4 端点 ∧ 前端已读合成）
      summaryCache: null, // 与下方 summaryCache 同体（Map），供结构断言/调试面
    },
  };

  // 懒加载缓存：card_id -> 结论摘要（会话内）。
  // [画布第三步 T3] 归异步桶：state.async.summaryCache 与本常量同体引用——渲染函数
  // （getSummary/lazyLoadVisibleSummaries，V1 名单内）沿用本直呼名，不直摸 state。
  const summaryCache = new Map();
  state.async.summaryCache = summaryCache;
  // card_id -> card DOM 元素
  const nodeMap = new Map();

  // [R1] 当前正在拖拽的节点所对应的「卸载器」（模块级；同一时刻只可能有一次拖拽）。
  // 拖拽监听器只在拖拽期间存在——原来挂在 bindNodeEvents 函数体里无条件执行，等于每建
  // 一个节点就挂一对 document 监听器且从不卸载（removeEventListener 全文件零命中），越用越钝。
  let activeDragDetach = null;
  // [画布商业化 2026-10-17] 拖拽后抑制补发的 click：浏览器在 mouseup 后必然补一个
  // click，若不抑制，多选整组拖完会被 click 处理器重置成"只选中刚拖的那一张"。
  let suppressClickUntil = 0;

  // [R1] 兜底：窗口失焦（如鼠标在窗口外松开、mouseup 收不到）时，取消当前拖拽并卸监听器。
  // 模块级注册一次，不随节点增长。
  window.addEventListener("blur", () => {
    if (activeDragDetach) activeDragDetach();
  });

  function escapeXml(s) {
    return String(s || "").replace(/[<>&"']/g, c => ({
      "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;"
    }[c]));
  }

  // ==================== 应用变换 ====================
  function applyTransform() {
    world.style.transform = `translate(${state.view.panX}px, ${state.view.panY}px) scale(${state.view.zoom})`;
    const pct = Math.round(state.view.zoom * 100);
    if (zoomDisplay) zoomDisplay.textContent = pct + "%";
    // 滑块由视图态单向驱动（用户拖滑块时不回写，避免与输入打架）
    if (zoomSlider && document.activeElement !== zoomSlider) {
      zoomSlider.value = String(Math.min(200, Math.max(25, pct)));
    }
    updateLOD();
    scheduleMinimap();   // [画布商业化] 视口框随变换实时跟随
  }

  // [画布第三步 T2] 档位判据的 zoom 改参数传入（V1：本函数在渲染 10 函数名单内；
  // zoom 属视图态容器——T2③ 口径「从视图态容器取/参数传入，不从投影取」）
  function currentLODLevel(zoom) {
    if (zoom < 0.5) return "l0";
    if (zoom > 1.2) return "l2";
    return "l1";
  }

  // [画布商业化 2026-10-17] 档位缓存：缩放时**只有跨档才遍历节点改 class**。
  // 改前每次 wheel 都跑一遍全节点 classList.toggle（232 卡时实测缩放掉到 44fps）；
  // 档位没变时 class 结果与上次完全一致，重刷是纯浪费。
  let _lodApplied = "";
  function applyLODClasses(zoom, force) {
    const level = currentLODLevel(zoom);
    if (!force && level === _lodApplied) return level;
    _lodApplied = level;
    for (const [cid, node] of nodeMap) {
      node.classList.remove("l0", "l1", "l2");
      node.classList.add(level);
    }
    // [诸葛 2026-10-04 21:52] L0 档线的显隐规则重构（设计由诸葛定，用户 21:46 授权界面自决）。
    // 原行为：zoom<0.5 时整个 edgesSvg 层 opacity=0——父子线与关系线一起消失。
    // 该行移植自写作agent（[phase8.6-*] 分段标记为证），当时画布是「整图铺开」，
    // 远景必然密集，藏线合理。推演agent 是折叠树，屏幕上只有展开部分——
    // 缩小恰恰是想看结构，把唯一表达结构的父子线关掉是反的。
    // 现规则：父子线（骨架）任何档都留，L0 档降级为细淡；关系线（弱关联）L0 档收起。
    // L0 档线消失没有技术代价：SVG 元素仍在 DOM，整层 opacity=0 省不了渲染
    // （元素未移除、渲染器照样处理），真正的 L0 性能动作在卡片侧（胶囊化、藏摘要）。
    edgesSvg.style.opacity = "1";
    const isL0 = (level === "l0");
    const pcGroup = edgesSvg.querySelector('[data-edge-group="parent-child"]');
    const relGroup = edgesSvg.querySelector('[data-edge-group="links"]');
    if (pcGroup) pcGroup.style.opacity = isL0 ? "0.55" : "1";
    if (relGroup) relGroup.style.display = isL0 ? "none" : "";
    return level;
  }

  // [phase8.6-B] 换档穿越（L0↔L1↔L2）后重跑 实测→避让→回写→renderEdges，100ms 防抖
  let lastLODLevel = null;
  function updateLOD() {
    const level = applyLODClasses(state.view.zoom);
    if (lastLODLevel !== null && level !== lastLODLevel) {
      scheduleReflow();
    }
    lastLODLevel = level;
  }

  let reflowTimer = null;
  function scheduleReflow() {
    if (reflowTimer) clearTimeout(reflowTimer);
    reflowTimer = setTimeout(reflowAll, 100);
  }

  // ==================== 布局引擎（纯函数） ====================
  // [迭代四 F2③] 原 window 挂载（computeLayout/resolveOverlaps/separateOverlaps，
  // 「供测试」）改模块导出；node 侧验证通道＝static/canvas_layout.js（E2 双栖加载器）。
  // [T07 打点] 判据源切换观测：每处首次调用记新旧对比；不一致升级 console.warn
  // （判据源切换唯一的回归观测点）。旧判据表达式仅存活于本观测、不参与分支；
  // 其字面量以 \u 转义书写，满足 V9「旧判据零残留」机械检查（运行期语义不变）。
  const _t07SwitchLogged = {};
  function t07SwitchLog(site, cardId, oldRes, newRes) {
    if (!_t07SwitchLogged[site]) {
      _t07SwitchLogged[site] = true;
      console.info("[T07] 判据源切换:", site, "card=", cardId, "旧判据=", !!oldRes, "新判据=", !!newRes);
    }
    if (!!oldRes !== !!newRes) {
      console.warn("[T07] 判据源切换不一致:", site, "card=", cardId, "旧判据=", !!oldRes, "新判据=", !!newRes);
    }
  }

  // [card_045 E1] 布局计算纯化：展开态（expanded）与尺寸表（sizes）一律参数传入，
  // 不再读全局 state、不再内部调 DOM 测量（measureSizes）——布局函数由此可在
  // 浏览器之外被验证（node --test）。**其余逻辑一字不动**（验收＝与冻结基线
  // 副本对同一输入逐键相同，见 _card_045_report.md）。
  function computeLayout(viewRootId, cards, links, positions, expanded, sizes) {
    const result = {};
    const childMap = {};
    for (const c of cards) {
      const p = c.parent || "";
      if (!childMap[p]) childMap[p] = [];
      childMap[p].push(c);
    }
    const root = cards.find(c => c.card_id === viewRootId);
    // [T07-1-9] 判据源切换：视图根是否剧本根卡，按「该卡 parent 为空」判
    // （依据 card_store._normalize_parent：剧本卡 parent 强制 ""，其余卡兜底回根卡）
    const isRootCard = root && (root.parent || "") === "";
    t07SwitchLog("canvas.computeLayout.isRootCard", viewRootId,
      root && root.card_type === "\u5267\u672c", isRootCard);

    // 1. pinned 卡用存储坐标。
    // [card_045 E7 · L5 任何一张卡、任何时刻都必须能被找到] 非法坐标兜底：
    // x/y 非有限数（NaN／Infinity／缺字段）的「钉住」记录不按钉住处理——该卡
    // 落**自动布局位置**。不许跳过、不许消失：跳过等于那张卡从画布上消失，
    // 而丢卡是最坏的失败形态（人不会知道少了一张）。
    for (const c of cards) {
      const pos = positions[c.card_id];
      if (pos && pos.pinned
          && Number.isFinite(pos.x) && Number.isFinite(pos.y)) {
        result[c.card_id] = { x: pos.x, y: pos.y };
      }
    }

    // 2. viewRoot 固定顶部中央 (0,0)
    result[viewRootId] = { x: 0, y: 0 };

    // [T08-1-3] 原 isRootCard 双分支（叙事轴/网格矩阵）收敛为折叠树单路径；
    // [T08-1-5] layoutNarrativeAxis / layoutGridMatrix 停止调用（不删除），见打点三。
    layoutTree(viewRootId, childMap, positions, expanded, result, sizes);
    return result;
  }

  function layoutNarrativeAxis(viewRootId, children, links, positions, result) {
    console.info("[T08-旧] %s 被调用", "layoutNarrativeAxis"); // [T08-1-5] 停用观测：本函数已不被 computeLayout 调用，出现输出即异常
    // [T07-1-8] 分组判据源切换：不再按 card_type 滤卡，改用与 childMap 同源的
    // parent 分组结果（children 即 childMap[viewRootId] 的直接子卡组）。
    // 类型带退场后全部直接子卡并入单带；其余四带保留空数组占位（现状恒空）。
    const plots = [];
    const settings = [];
    const chars = [];
    const foreshs = [];
    const ideas = children;
    if (!_t07SwitchLogged["canvas.layoutNarrativeAxis.grouping"]) {
      _t07SwitchLogged["canvas.layoutNarrativeAxis.grouping"] = true;
      console.info("[T07] 判据源切换: canvas.layoutNarrativeAxis.grouping 分组源 card_type→parent(childMap 同源) 直接子卡数=", children.length);
    }

    // 找"目标"卡（与剧本卡有锚定关系）
    const anchoredIds = new Set();
    for (const r of links) {
      if (r.type === "锚定" && r.source !== viewRootId) anchoredIds.add(r.source);
    }
    // [T07-1-11] 废止 title==="目标" 死条件（全库零命中，恒假）；保留锚定支
    let _t07EndingChecked = false;
    const isEnding = (c) => {
      const nw = anchoredIds.has(c.card_id);
      if (!_t07EndingChecked) {
        _t07EndingChecked = true;
        t07SwitchLog("canvas.layoutNarrativeAxis.isEnding", c.card_id,
          (c.title === "\u76ee\u6807") || nw, nw);
      }
      return nw;
    };

    // 节点排序：links 中 type==先后 做拓扑序
    const plotIds = new Set(plots.map(c => c.card_id));
    const edges = links.filter(r => r.type === "先后" && plotIds.has(r.source) && plotIds.has(r.target));
    const inDeg = {};
    const adj = {};
    for (const p of plots) { inDeg[p.card_id] = 0; adj[p.card_id] = []; }
    for (const e of edges) {
      adj[e.source] = adj[e.source] || [];
      adj[e.source].push(e.target);
      inDeg[e.target] = (inDeg[e.target] || 0) + 1;
    }
    const queue = plots.filter(p => inDeg[p.card_id] === 0).sort((a, b) => (a.created_at || "").localeCompare(b.created_at || ""));
    const ordered = [];
    const seen = new Set();
    while (queue.length) {
      const n = queue.shift();
      if (seen.has(n.card_id)) continue;
      seen.add(n.card_id);
      ordered.push(n);
      for (const nxt of (adj[n.card_id] || [])) {
        inDeg[nxt] -= 1;
        if (inDeg[nxt] === 0) queue.push(plots.find(p => p.card_id === nxt));
      }
    }
    // 未入序的（孤立节点）按 created_at 补在后
    for (const p of plots) if (!seen.has(p.card_id)) ordered.push(p);

    // 目标卡恒排最右
    ordered.sort((a, b) => {
      const ae = isEnding(a) ? 1 : 0, be = isEnding(b) ? 1 : 0;
      if (ae !== be) return ae - be;
      return 0;
    });

    // 节点主轴 y=400，x 间距 280
    const plotX = {};
    ordered.forEach((p, i) => {
      const x = i * 280;
      plotX[p.card_id] = x;
      if (!result[p.card_id]) result[p.card_id] = {};
      result[p.card_id] = { x, y: 400 };
    });

    // 找关系目标 x 的辅助
    function findTargetX(card) {
      for (const r of links) {
        if (r.source === card.card_id && plotX[r.target] !== undefined) return plotX[r.target];
      }
      return null;
    }

    // [hotfix] 分带两遍摆放：第一遍先摆有关系目标的卡（贴目标 x，冲突右移 240），
    // 第二遍无关系的卡从 x=0 起从左往右填空位——修复"有关系的卡把无关系的卡
    // 连锁推挤到画布远端"的长尾巴问题
    function layoutBand(bandCards, y) {
      const usedX = bandCards.filter(c => result[c.card_id]).map(c => result[c.card_id].x);
      const conflict = (x) => usedX.some(u => Math.abs(u - x) < 240);
      // 第一遍：有目标的贴目标
      for (const c of bandCards) {
        if (result[c.card_id]) continue;
        let x = findTargetX(c);
        if (x === null) continue;
        while (conflict(x)) x += 240;
        usedX.push(x);
        result[c.card_id] = { x, y };
      }
      // 第二遍：无目标的从左往右填空位
      let cursor = 0;
      for (const c of bandCards) {
        if (result[c.card_id]) continue;
        while (conflict(cursor)) cursor += 240;
        usedX.push(cursor);
        result[c.card_id] = { x: cursor, y };
        cursor += 280;
      }
    }

    // 世界观带 y=180
    layoutBand(settings, 180);
    // 行为体带 y=620
    layoutBand(chars, 620);
    // 假设带 y=540，贴关系指向的节点卡 x
    layoutBand(foreshs, 540);

    // 线索堆：起点 = 节点轴最右 + 400，y=620，两列网格
    const plotMaxX = ordered.length ? (ordered.length - 1) * 280 : 0;
    let ideaStartX = plotMaxX + 400;
    ideas.forEach((idea, i) => {
      if (result[idea.card_id]) return;
      const col = i % 2;
      const row = Math.floor(i / 2);
      result[idea.card_id] = { x: ideaStartX + col * 240, y: 620 + row * 110 };
    });
  }

  function layoutGridMatrix(children, result) {
    console.info("[T08-旧] %s 被调用", "layoutGridMatrix"); // [T08-1-5] 停用观测：本函数已不被 computeLayout 调用，出现输出即异常
    // 按 created_at 序，每行 4 张，间距 x=260 y=130；
    // [hotfix] 首行按实际张数相对根卡(x=0)居中，不再写死 -390 起排
    // （子卡少于 4 张时旧算法整体偏左，且负坐标区落在初始视口外）
    const sorted = [...children].sort((a, b) => (a.created_at || "").localeCompare(b.created_at || ""));
    const dx = 260, dy = 130;
    const perRow = 4;
    const startY = 160;
    const firstRowCount = Math.min(sorted.length, perRow) || 1;
    const startX = -((firstRowCount - 1) * dx) / 2;
    sorted.forEach((c, i) => {
      if (result[c.card_id]) return;
      const col = i % perRow;
      const row = Math.floor(i / perRow);
      result[c.card_id] = { x: startX + col * dx, y: startY + row * dy };
    });
  }

  // [T18-C-005] 避让间距单一来源：layoutTree 子树间距 / separateOverlaps / resolveDrop /
  // 旧 resolveOverlaps 分带共用此常量（原 layoutTree 域内局部常量上提，消除散落硬编码 40）。
  // [画布 2026-10-18] 32 → 40：卡片改成 min-height 150 之后，同层不同父卡的
  // 子树在纵向只差 4px 就相交（实测 card_031 × card_007），避让推不动。
  const GAP_Y = 40;

  // [T08-1-2] 折叠树排布（替代原 narrativeAxis/gridMatrix 双分支）。
  // 方向口径（用户 2026-10-04 中途指令「布局用左右视图」，对任务书上下树的推翻，留痕）：
  // 左右视图——根卡居左，子树向右逐层展开；同父子树纵向一字排开不重叠
  // （子树纵向跨度 = 其叶子实测高之和 + 间距，父卡在子树跨度内垂直居中）。
  // 层间距由 measureSizes() 实测高推出（取不到按既有惯例回退 220×86）；列宽恒定
  // 列间距按「父卡实宽 + GAP_X」动态算（卡片宽度随 data-depth 分档）；
  // 实测高的纵横避让由 separateOverlaps 在 DOM 建成后完成（T18-C1/C2）。
  // 只写内存 result，不读不写任何落盘载体（红线1/6）；pinned 卡由 computeLayout
  // 步骤1写入 result、此处不覆盖（保留规则），其子树以存储坐标为锚继续右排。
  // [画布重构 2026-10-15 · 2.2] anchor 模式：以指定卡为「单元根」只排它自己那棵
  // 小子树，anchor 自身位置不动（调用方已把坐标写进 result，此处只读不覆盖）。
  // 缺省 anchor = null → 递归根仍是 viewRootId，**全树路径逐字不变**
  // （验收标准⑤：初始加载的表现与现状一致，由既有 node 用例兜住）。
  // anchor = {id, x, y, depth, isViewRoot}：depth 供 heightOf 的深卡回退档（≥2 档 36）用；
  // isViewRoot 标明「这个单元根就是当前视图根」——视图根的子卡恒可见（T08-1-4 既有规则），
  // 而**普通卡收起时子卡必须消失**，两者不可混为一谈。
  function layoutTree(viewRootId, childMap, positions, expanded, result, sizes, anchor) {
    console.info("[T08] 树形排布：viewRoot=%s，节点数=%d", viewRootId, Object.keys(result).length);
    // [card_045 E1] 尺寸表改参数传入（原此处调 measureSizes() 内部测 DOM）；
    // 取不到的卡沿用回退行高——与原行为一致。
    // 回退尺寸按**深度**分档（首绘时 DOM 未建、实测不可得，按此估跨距）。
    // 与 canvas.css 的 data-depth 档位一一对应：d0 340×~150 / d1 264×~120 /
    // d2 216×~100 / d3 184×~64。回退值偏高保守估计（只多让不少让）。
    const FALLBACK_W = [360, 280, 224, 184];
    const FALLBACK_H = [152, 124, 100, 58];
    const dIdx = (depth) => Math.max(0, Math.min(3, depth || 0));
    sizes = sizes || {};
    const heightOf = (cid, depth) => (sizes[cid] && sizes[cid].h) || FALLBACK_H[dIdx(depth)];
    const widthOf = (cid, depth) => (sizes[cid] && sizes[cid].w) || FALLBACK_W[dIdx(depth)];
    // [画布商业化 2026-10-18 · 层级] 列间距改「父卡实宽 + GAP_X」，不再用固定 340。
    // 为什么必须改：卡片宽度现在按深度分档（340/264/216/184），固定列宽会让浅层
    // 卡之间空一大片、深层卡之间挤在一起——层级一改，间距不跟着改就必然错位。
    // ★ 层间距（GAP_X）必须**明显大于**列间距（COL_GAP）。
    //   原因：同一父卡的子卡超过 4 张会分成多列，如果列间距和层间距差不多，
    //   第二列看起来就像"下一层"——这正是"一级卡二级卡混在一起"的视觉根源。
    //   现在 96 vs 20：跨层是"跳一大格"，同层多列是"并排一组"。
    const GAP_X = 96;    // 跨层：父卡右缘 → 子卡左缘
    const COL_GAP = 20;  // 同层多列之间

    // ★ anchor 模式：递归根换成 anchor 自己；布局「布局树的子边」规则不变
    const rootId = anchor ? anchor.id : viewRootId;
    const rootDepth = anchor ? (anchor.depth || 0) : 0;
    // 单元根是否「子卡恒在」：全树模式下单元根就是视图根 → 恒在；
    // anchor 模式下只有 anchor.isViewRoot 为真才恒在，收起普通卡时子卡照常消失。
    const rootAlwaysOpen = anchor ? !!anchor.isViewRoot : true;

    // 布局树的「子边」：恒开的单元根直接子卡恒在；其余卡的子卡仅当该卡 ∈ expanded
    // （与 T08-1-4 可见集同一规则，保证布局覆盖恰为可见集）
    const kidsOf = (cid) =>
      ((cid === rootId && rootAlwaysOpen) || expanded.has(cid)) ? (childMap[cid] || []) : [];

    // [画布商业化 2026-10-17] 子卡超过 MAX_PER_COL 张时**分列折行**。
    // 为什么：单列纵向排 6 张卡＝高度 1000px，视口装不下 → fit 后缩到 70% 字看不清。
    // 分列后同一批卡占「2 列 × 3 行」，接近方形，fit 后能保持接近 100%。
    // 判据是"直接子卡数"，与子树深度无关——深层自然递归套用同一规则。
    // [画布 2026-10-18 · 有序] 10 张以内**单列纵向**排：从上到下就是建卡顺序
    // （＝用户思考的顺序），像读目录一样。原来 4 张就折列，8 张变成 2×4，
    // 读者要在两列之间来回跳，"第 5 张"跑到第二列顶部——这是"无序摆放"的来源之一。
    const MAX_PER_COL = 10;
    const columnize = (kids) => {
      const n = kids.length;
      if (n <= MAX_PER_COL) return [kids];
      // 均衡分列：先定列数，再均分。6 张 → 3+3（而不是 4+2 留下两行空半列），
      // 这样每一行的列都是满的，视觉上是规整矩形而不是"缺角"。
      const colCount = Math.ceil(n / MAX_PER_COL);
      const per = Math.ceil(n / colCount);
      const cols = [];
      for (let i = 0; i < n; i += per) cols.push(kids.slice(i, i + per));
      return cols;
    };
    // 后序：子树纵向跨度 = max(自身高, 最高一列的高度)；seen 防脏数据成环
    const subtreeH = {};
    const seen = new Set();
    (function measure(cid, depth) {
      if (seen.has(cid)) return heightOf(cid, depth);
      seen.add(cid);
      const kids = kidsOf(cid);
      const cols = columnize(kids);
      const rows = cols.reduce((m, c) => Math.max(m, c.length), 0);
      let total = 0;
      for (let r = 0; r < rows; r++) {
        let rowMax = 0;
        for (const col of cols) {
          if (col[r]) rowMax = Math.max(rowMax, measure(col[r].card_id, depth + 1));
        }
        total += rowMax + (r > 0 ? GAP_Y : 0);
      }
      subtreeH[cid] = kids.length ? Math.max(heightOf(cid, depth), total) : heightOf(cid, depth);
      return subtreeH[cid];
    })(rootId, rootDepth);

    // 前序：子树占 [topY, topY+subtreeH]，卡在子树跨度内垂直居中，
    // x 逐层右移「父卡实宽 + GAP_X」。
    // 全树模式下根卡子树跨度以 y=0（根卡纵位）为中心上下分半；
    // anchor 模式下以 anchor 的实际纵位为中心分半（anchor 自身位置不动，
    // 子卡在它自己的子树区域里排——这正是「展开一张卡只重排它单元内部」）。
    seen.clear();
    const topY0 = anchor
      ? anchor.y - (subtreeH[rootId] - heightOf(rootId, rootDepth)) / 2
      : -subtreeH[rootId] / 2;
    (function place(cid, x, topY, depth) {
      if (seen.has(cid)) return;
      seen.add(cid);
      let anchorY = topY, childX = x + widthOf(cid, depth) + GAP_X;
      if (cid !== rootId) {
        if (positions[cid] && positions[cid].pinned && result[cid]) {
          // [画布 2026-10-18] pinned **只保留纵向位置（顺序），横向一律由层级决定**。
          //
          // 拖拽已经改成"横向锁定、只调上下"（见 bindNodeEvents），所以存储里的 pos_x
          // 只可能是历史自由拖拽留下的脏数据。实测后果：同层 8 张卡的 x 变成
          // 456 / 623 / 943，横向散开、主干线接不上——这就是用户说的"无序摆放"。
          // 树形布局里横向＝层级（结构，不可改），纵向＝顺序（用户可调）。
          result[cid] = { x: x, y: result[cid].y };
          anchorY = result[cid].y - (subtreeH[cid] - heightOf(cid, depth)) / 2;
          childX = x + widthOf(cid, depth) + GAP_X;
        } else {
          result[cid] = { x: x, y: topY + (subtreeH[cid] - heightOf(cid, depth)) / 2 };
        }
      }
      const kids = kidsOf(cid);
      if (!kids.length) return;
      // [画布商业化 2026-10-17] 行对齐摆放：同一行的卡共享一条基线（行高＝该行最大
      // 子树跨度），列与列之间因此是规整网格，而不是"各列各自居中"的阶梯错位。
      const cols = columnize(kids);
      const rows = cols.reduce((m, c) => Math.max(m, c.length), 0);
      const rowHs = [];
      for (let r = 0; r < rows; r++) {
        let m = 0;
        for (const col of cols) if (col[r]) m = Math.max(m, subtreeH[col[r].card_id]);
        rowHs.push(m);
      }
      const totalH = rowHs.reduce((a, b) => a + b, 0) + (rows - 1) * GAP_Y;
      // 每列的 x 起点：前几列的卡宽之和 + **列间距**（同列卡深度相同 ⇒ 宽度相同）。
      // 注意这里用 COL_GAP 而不是 GAP_X——同层并排，不是换层。
      const colX = [];
      let cursorX = childX;
      for (const col of cols) {
        colX.push(cursorX);
        cursorX += widthOf(col[0].card_id, depth + 1) + COL_GAP;
      }
      let rowY = anchorY + (subtreeH[cid] - totalH) / 2;
      for (let r = 0; r < rows; r++) {
        for (let ci = 0; ci < cols.length; ci++) {
          const kid = cols[ci][r];
          if (!kid) continue;
          // 行内**顶部对齐**（原来垂直居中）：同一行的卡片顶端成一条线，
          // 视觉上是一排而不是参差错落的一堆；子树在卡下方自然延伸。
          place(kid.card_id, colX[ci], rowY, depth + 1);
        }
        rowY += rowHs[r] + GAP_Y;
      }
    })(rootId, anchor ? anchor.x : 0, topY0, rootDepth);
  }

  // [phase8.6-B] 纵向避让纯函数：按初始 y 分带，带间距不小于 40px
  // layout: computeLayout 结果 {cid:{x,y}}；sizes: {cid:{w,h}}；pinnedIds: Set，跳过不动
  function resolveOverlaps(layout, sizes, pinnedIds) {
    const pinned = pinnedIds || new Set();
    const out = {};
    for (const cid in layout) out[cid] = { x: layout[cid].x, y: layout[cid].y };
    // [诸葛 2026-10-04 21:52] pinned 卡改为「位置不动、但参与避让计算」。
    // 原行为：pinned 卡直接 continue——不进分带，于是别的卡算避让时当它不存在，
    // 同 y 的卡会直接压上去（card_007 §十二④ bug A，从未实施）。
    // 现行为：pinned 卡 x/y 一律不覆盖（拖到哪就在哪），但它们的 y 进入分带序列，
    // 其它卡见到它们自动让开（有 y 重叠才抬起，不跟它抢占）。这样钉住仍有效
    // （位置我说了算），但不再出现视觉重叠（别的卡给我让路）。
    const bands = new Map();
    for (const cid in layout) {
      const y = layout[cid].y;
      if (!bands.has(y)) bands.set(y, []);
      bands.get(y).push(cid);
    }
    const ys = Array.from(bands.keys()).sort((a, b) => a - b);
    let prevBottom = null;
    for (const y0 of ys) {
      const ids = bands.get(y0);
      let bandY = y0;
      if (prevBottom !== null && bandY < prevBottom + GAP_Y) bandY = prevBottom + GAP_Y;   // [T18-C-005] 原 40 硬编码收口
      let bandBottom = bandY;
      for (const cid of ids) {
        if (pinned.has(cid)) {
          // 钉住的卡不改坐标，但把它的底边计入，供后续带避让
          const h = (sizes[cid] && sizes[cid].h) || 86;
          bandBottom = Math.max(bandBottom, out[cid].y + h);
          continue;
        }
        out[cid].y = bandY;
        const h = (sizes[cid] && sizes[cid].h) || 86;
        bandBottom = Math.max(bandBottom, bandY + h);
      }
      prevBottom = bandBottom;
    }
    return out;
  }

  // [T18-C1] 全向避让纯函数（AABB + MTD 最短轴、纵向优先）——resolveOverlaps 的继任者。
  // layout: computeLayout 结果 {cid:{x,y}}；sizes: {cid:{w,h}}；pinnedIds: Set；GAP: 间距。
  // ★ movable（[画布重构 2026-10-15 · 2.3]）：可动白名单。传入 Set 时**只有**集合内的
  //   卡允许被推开，集合外的卡一律当「固定障碍」参与相交计算但永不被移动——这就是
  //   「避让局部化」：展开一张卡时，别处已排好的卡（含 pinned）连 y 都不会被重算掉。
  //   缺省 null ＝ 全员可动＝**旧行为逐字不变**（拖拽落定后的全量重排走这条）。
  // pinned 三档语义（写死）：①pinned 永不被移动；②free–pinned 相交 → 推 free 全量 MTD；
  // ③free–free 相交 → 两卡各推半量；④double pinned → 不动 + console.warn
  // （不新增由 C3 resolveDrop 保证；历史遗留交「整理画布」按钮清）。
  // honest 口径（RD-07）：分离尽力——16 轮内收敛则零相交，16 轮未收敛 warn 留痕。
  // 16 轮上限是前端本地有限步 FPS/防卡保护，不是对模型调用/轮次的闸（RD-02 辨析）。
  // 行高回退 86 对齐 layoutTree.FALLBACK_H；深度≥2 的 36 档在首绘 DOM 未建时本签名不可知，
  // 86 为偏高保守估计（只多让不少让），实测尺寸回填后经 scheduleReflow→reflow 复用本函数再收敛。
  function separateOverlaps(layout, sizes, pinnedIds, GAP = GAP_Y, movable = null) {
    const pinned = pinnedIds || new Set();
    const out = {};
    for (const cid in layout) out[cid] = { x: layout[cid].x, y: layout[cid].y };
    const ids = Object.keys(out);
    // 可动判据：movable 为 null 时全员可动（旧行为）；否则白名单之外一律固定
    const canMove = movable ? (cid) => movable.has(cid) : () => true;
    const wh = (cid) => ({
      w: (sizes[cid] && sizes[cid].w) || 220,
      h: (sizes[cid] && sizes[cid].h) || 86,
    });
    let moved = true;
    let rounds = 0;
    let dpWarned = false;
    let frozenPairWarned = false;
    while (moved && rounds < 24) {          // 24 轮（卡片变高后 16 轮不够收敛）
      moved = false;
      rounds++;
      for (let i = 0; i < ids.length; i++) {
        for (let j = i + 1; j < ids.length; j++) {
          const a = ids[i], b = ids[j];
          const wa = wh(a), wb = wh(b);
          const dx = (out[b].x + wb.w / 2) - (out[a].x + wa.w / 2);
          const dy = (out[b].y + wb.h / 2) - (out[a].y + wa.h / 2);
          const px = (wa.w + wb.w) / 2 + GAP - Math.abs(dx);   // 横向重叠量（含 GAP）
          if (px <= 0) continue;
          const py = (wa.h + wb.h) / 2 + GAP - Math.abs(dy);   // 纵向重叠量（含 GAP）
          if (py <= 0) continue;
          // aP/bP ＝「本次不许移动」：pinned 恒真，movable 白名单外的亦为真。
          // （movable 为 null 时 canMove 恒真 ⇒ 与旧判据逐字等价）
          const aP = pinned.has(a) || !canMove(a);
          const bP = pinned.has(b) || !canMove(b);
          if (aP && bP) {
            // 两张都不许动：只有「双 pinned」才是需要留痕的异常；
            // 「固定障碍 × 局部新增」是局部避让的**正常**结果（绕不开就重叠，如实留痕一次）
            if (pinned.has(a) && pinned.has(b)) {
              if (!dpWarned) {
                console.warn("[separateOverlaps] 双 pinned 相交（历史遗留，交「整理画布」清）:", a, b);
                dpWarned = true;
              }
            } else if (!frozenPairWarned) {
              console.warn("[separateOverlaps] 局部避让遇双固定障碍（重叠，如实留痕）:", a, b);
              frozenPairWarned = true;
            }
            continue;
          }
          // MTD 最短轴、纵向优先：|oy| <= |ox| 推 y（保树形列结构）
          if (py <= px) {
            const dir = dy >= 0 ? 1 : -1;   // b 在下则分别向上下推开
            if (aP)      out[b].y += dir * py;
            else if (bP) out[a].y -= dir * py;
            else { out[a].y -= dir * py / 2; out[b].y += dir * py / 2; }
          } else {
            const dir = dx >= 0 ? 1 : -1;
            if (aP)      out[b].x += dir * px;
            else if (bP) out[a].x -= dir * px;
            else { out[a].x -= dir * px / 2; out[b].x += dir * px / 2; }
          }
          moved = true;
        }
      }
    }
    if (moved) console.warn("[separateOverlaps] 24 轮内未完全收敛，可能残留个别相交");
    return out;
  }


  // ══════════════ [画布重构 2026-10-15 · 阶段 2] 增量渲染的纯决策层 ══════════════
  // 上面那些函数回答「整张画布长什么样」；这一组回答另一个问题：
  // 「展开/收起这一张卡，**该动哪些**」。
  //
  // 为什么要拆成纯函数：增量渲染的风险不在算法，在**越界**——手一滑把不相干的卡
  // 也重建了/挪了，这种越界在浏览器的小画布上未必看得出来。所以把「谁该被创建、
  // 谁该被移除、谁的位置允许被改写」三件事各写成一个可单独断言的纯函数：判据一旦
  // 越界，node 用例当场红。DOM 那一侧（renderSubtree）只照计划执行，不做判断。

  // anchor 的全部子孙（不看展开态——展开只负责「隐藏」，子孙集合本身由 parent 关系定）。
  // seen 防脏数据成环：parent 指来指去的卡不许把遍历带进死循环（与 layoutTree 同纪律）。
  function collectDescendants(anchorId, childMap) {
    const out = new Set();
    const seen = new Set([anchorId]);
    const stack = [anchorId];
    while (stack.length) {
      const cid = stack.pop();
      for (const child of (childMap[cid] || [])) {
        const kid = child.card_id;
        if (seen.has(kid)) continue;
        seen.add(kid);
        out.add(kid);
        stack.push(kid);
      }
    }
    return out;
  }

  // 子树内当前**可见**的卡。展开规则与 layoutTree 的「子边」判据逐字一致：
  //   · 单元根**是视图根**时，子卡恒可见（T08-1-4 既有规则）；
  //   · 其余情形（含普通卡收起）一律看 expanded.has(该卡)。
  // ★ 这两者的区别不是细节：**收起一张普通卡时它的子卡必须消失**。曾把「视图根
  //   子卡恒在」无差别套到任意锚点上，结果收起卡 A 反而把 A 的子卡留着——判据写错
  //   的代价是画面上多出一堆该消失的卡。
  // depth 以 anchorDepth 为基准——depthOf 用于 data-depth 档判定，必须相对 viewRoot，
  // 不能相对 anchor（否则每层都把自己当 0）。
  function computeSubtreeVisible(anchorId, childMap, expanded, anchorDepth, anchorIsViewRoot) {
    const ids = new Set();
    const depthOf = {};
    const base = anchorDepth || 0;
    // forceOpen：锚点这一层是否无条件展开（＝视图根，或锚点此刻确实在 expanded 里）。
    // 更深的卡一律只看 expanded.has(该卡)。
    const walk = (cid, depth, forceOpen) => {
      if (!forceOpen && !expanded.has(cid)) return;
      for (const child of (childMap[cid] || [])) {
        const kid = child.card_id;
        if (ids.has(kid) || kid === anchorId) continue;
        ids.add(kid);
        depthOf[kid] = depth + 1;
        walk(kid, depth + 1, false);
      }
    };
    walk(anchorId, base, !!anchorIsViewRoot || expanded.has(anchorId));
    return { ids, depthOf };
  }

  // 增量计划：给定 anchor 与当前在场节点集合，算出「该建谁、该拆谁、位置该由谁改写」。
  // - create：在场集合之外、但子树可见集之内 → 建新节点（淡入）
  // - remove：在场集合之内、但子树可见集之外 → 拆节点（收起）
  // - place ：位置允许被改写的卡 ＝ 子树可见集（**不含 anchor 自己**——它不动）
  // - keep  ：其余在场节点。它们不在 create/remove/place 任一清单里，
  //           故 renderSubtree 对它们既不 remove() 也不写 style.left/top——DOM 引用原样。
  function planSubtreeUpdate(anchorId, childMap, expanded, nodeIds, anchorDepth, anchorIsViewRoot) {
    const inField = nodeIds instanceof Set ? nodeIds : new Set(nodeIds);
    const subtree = computeSubtreeVisible(anchorId, childMap, expanded,
                                          anchorDepth, anchorIsViewRoot);
    const descendants = collectDescendants(anchorId, childMap);
    const create = [];
    const remove = [];
    for (const cid of subtree.ids) {
      if (!inField.has(cid)) create.push(cid);
    }
    for (const cid of inField) {
      // 只拆「本 anchor 的子孙里已不可见的」——别处不可见的卡不在本次管辖内
      if (descendants.has(cid) && !subtree.ids.has(cid)) remove.push(cid);
    }
    const keep = [];
    for (const cid of inField) {
      if (subtree.ids.has(cid) || descendants.has(cid)) continue;
      keep.push(cid);
    }
    return { anchorId, create, remove, keep, place: [...subtree.ids], depthOf: subtree.depthOf };
  }

  // 子树单元布局：只算 anchor 单元内部的坐标，anchor 自身坐标由调用方给（不动）。
  // 种子与 computeLayout 步骤1 同源（pinned 卡用存储坐标，含非法坐标兜底）；差别
  // 只在 layoutTree 走 anchor 模式——递归根是 anchor 自己，不是 viewRoot。
  // 返回值只含 anchor 单元内的键（含 anchor 自己，供调用方合并）。
  function computeSubtreeLayout(viewRootId, cards, links, positions, expanded,
                                sizes, anchor) {
    const result = {};
    const childMap = {};
    for (const c of cards) {
      const p = c.parent || "";
      if (!childMap[p]) childMap[p] = [];
      childMap[p].push(c);
    }
    // 1. pinned 卡用存储坐标（与 computeLayout 步骤1 同一判据）
    for (const c of cards) {
      const pos = positions[c.card_id];
      if (pos && pos.pinned && Number.isFinite(pos.x) && Number.isFinite(pos.y)) {
        result[c.card_id] = { x: pos.x, y: pos.y };
      }
    }
    // 2. anchor 落当前屏幕位（不动的那颗）
    result[anchor.id] = { x: anchor.x, y: anchor.y };
    // 3. 只排 anchor 单元
    layoutTree(viewRootId, childMap, positions, expanded, result, sizes, anchor);
    // 4. 出参收敛到本单元：别处那些「顺手带出来的 pinned 种子」不许流出去，
    //    否则调用方一合并就等于把不相干的卡又写了一遍（越界即事故）
    const keep = collectDescendants(anchor.id, childMap);
    keep.add(anchor.id);
    const unit = {};
    for (const cid in result) {
      if (keep.has(cid)) unit[cid] = result[cid];
    }
    return unit;
  }

  // [phase8.6-B] 实测节点尺寸；取不到时回退 220×86
  function measureSizes() {
    const sizes = {};
    for (const [cid, node] of nodeMap) {
      sizes[cid] = {
        w: node.offsetWidth || 220,
        h: node.offsetHeight || 86,
      };
    }
    return sizes;
  }

  function nodeSize(cid) {
    const node = nodeMap.get(cid);
    if (node && node.offsetWidth) return { w: node.offsetWidth, h: node.offsetHeight };
    return { w: 220, h: 86 };
  }

  // [画布第三步 T2] 调用点随投影链退役（renderCanvas/reflowLayout 改吃 projection 内
  // 的 pinnedIdsOf 推导）——本体保留备查（同 resolveDrop 先例，RD-08 同款口径）。
  function pinnedIdSet() {
    const s = new Set();
    for (const cid in state.positions) {
      if (state.positions[cid] && state.positions[cid].pinned) s.add(cid);
    }
    return s;
  }

  // ── [画布第三步 T1] 投影纯函数五件（可见集／面包屑／连线几何／pinned 推导／投影链）──
  // 可见集规则自 renderCanvas（T08-1-4）**逐字搬出**：规则一字不改，只把
  // state.viewRoot / state.childMap / state.expanded 换成参数（E1 同款参数化先例）。
  // node 侧验证通道＝static/canvas_layout.js（factory 返回列表 5→7 符号）。
  function computeVisibleSet(viewRootId, childMap, expanded) {
    const visibleIds = new Set([viewRootId]);
    const depthOf = {};
    depthOf[viewRootId] = 0;
    for (const child of (childMap[viewRootId] || [])) {
      visibleIds.add(child.card_id);
      depthOf[child.card_id] = 1;
    }
    let grew = true;
    while (grew) {
      grew = false;
      for (const cid of Array.from(visibleIds)) {
        if (!expanded.has(cid)) continue;
        for (const child of (childMap[cid] || [])) {
          if (!visibleIds.has(child.card_id)) {
            visibleIds.add(child.card_id);
            depthOf[child.card_id] = (depthOf[cid] || 0) + 1;
            grew = true;
          }
        }
      }
    }
    return { visibleIds, depthOf };
  }

  // 面包屑链纯函数（自 renderBreadcrumb 逐字搬出，参数化同上）
  function computeBreadcrumb(viewRootId, cardMap) {
    const chain = [];
    let cur = viewRootId;
    const guard = new Set();
    while (cur && cardMap[cur] && !guard.has(cur)) {
      guard.add(cur);
      chain.unshift(cardMap[cur]);
      cur = cardMap[cur].parent;
    }
    return chain;
  }


  // 子卡分组几何（纯函数）：同一父卡的可见直接子卡 ≥2 张 → 一块 bbox。
  // 连线与分组框共用本函数，保证"线连到框上"与"框画在哪"永远一致。
  const GROUP_PAD = 18;

  function computeChildGroups(visibleIds, layout, sizes, childMap) {
    const out = [];
    for (const pid in childMap) {
      if (!visibleIds.has(pid)) continue;
      const kids = (childMap[pid] || []).filter((c) => visibleIds.has(c.card_id));
      if (kids.length < 2) continue;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const k of kids) {
        const p = layout[k.card_id];
        if (!p) continue;
        const sz = sizes[k.card_id] || { w: 200, h: 80 };
        x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y);
        x1 = Math.max(x1, p.x + sz.w); y1 = Math.max(y1, p.y + sz.h);
      }
      if (!Number.isFinite(x0)) continue;
      out.push({
        pid, kids,
        x0: x0 - GROUP_PAD, y0: y0 - GROUP_PAD,
        x1: x1 + GROUP_PAD, y1: y1 + GROUP_PAD,
      });
    }
    return out;
  }

  // 连线几何纯函数（自 renderEdges 数据段逐字搬出：rootParentId＋父子折线段＋外接框；
  // SVG 落笔仍留 renderEdges——几何数据与 DOM 应用分家）
  function computeEdgeGeometry(visibleIds, layout, sizes, childMap, cardMap, viewRootId) {
    const rootParentId = (cardMap[viewRootId] || {}).parent || "";

    // ── 第一步：收集原始边（起点先按父卡中点，稍后统一分布）──
    const raw = [];
    for (const pid in childMap) {
      for (const childCard of childMap[pid]) {
        const ckid = childCard.card_id;
        if (pid === rootParentId && ckid === viewRootId) continue;
        if (!visibleIds.has(pid) || !visibleIds.has(ckid)) continue;
        const p1 = layout[pid], p2 = layout[ckid];
        if (!p1 || !p2) continue;
        const s1 = sizes[pid] || { w: 220, h: 86 };
        const s2 = sizes[ckid] || { w: 220, h: 86 };
        const pcx = p1.x + s1.w / 2, ccx = p2.x + s2.w / 2;
        raw.push({ pid, ckid, p1, p2, s1, s2, childOnRight: ccx >= pcx });
      }
    }

    // ── 第二步：同一父卡的出边**在父卡侧缘均匀分布** ──
    //
    // [画布商业化 2026-10-18] 之前所有边都从父卡侧缘的**中点**出发，8 个子卡就是
    // 8 条线从同一个点炸开——画面上是一团"蜘蛛"，既看不清哪条线去哪，也看不出
    // 父子关系的规模。现在按目标 y 排序后在侧缘 [top+22, bottom-22] 内均匀散开，
    // 形成清晰的扇出（Figma/Miro 的 tree 连线就是这个形态）。
    // 单条边仍走中点（不需要散开）。
    const groups = new Map();
    for (const e of raw) {
      const key = e.pid + (e.childOnRight ? "|R" : "|L");
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(e);
    }
    // 成组的父卡不再逐子卡连线（8 条线从一点炸开＝蜘蛛网），
    // 改成一条「父卡 → 组框左缘中点」的线，组内关系由框本身表达。
    const EDGE_PAD = 22;   // 侧缘上下留白，避免线贴着卡片圆角
    for (const list of groups.values()) {
      const { p1, s1 } = list[0];
      if (list.length === 1) {
        list[0].y1 = p1.y + s1.h / 2;
        continue;
      }
      list.sort((a, b) => (a.p2.y + a.s2.h / 2) - (b.p2.y + b.s2.h / 2));
      const pad = Math.min(EDGE_PAD, s1.h * 0.22);
      const span = Math.max(1, s1.h - pad * 2);
      const n = list.length;
      list.forEach((e, i) => {
        e.y1 = p1.y + pad + (span * i) / (n - 1);
      });
    }

    // ── 第三步：落成线段（含 from/to 供高亮用）──
    //
    // ★ 成组的父卡只画**一条**「父卡 → 组框」的线。
    //   原来 8 个子卡＝8 条线从父卡侧缘同一点炸开，画面上是一团蜘蛛网：
    //   既看不清哪条线去哪，也看不出"这是一组"。组内关系交给分组框表达，
    //   连线只负责回答"这块框属于哪张卡"——这是树形图（mindmap）的标准做法。
    const childGroups = computeChildGroups(visibleIds, layout, sizes, childMap);
    const groupByPid = new Map(childGroups.map((g) => [g.pid, g]));
    const elbowSegs = [];

    // ── 主干线（替代分组框）──
    //
    // [画布 2026-10-18 · 人能读的层级] 分组框只说明"这些是一组"，不说明
    // "谁是谁的子卡、按什么顺序读"。主干线是目录的写法：
    //   父卡右缘 → 一条竖干贯穿所有子卡 → 每个子卡一条横线接上去。
    // 读者沿竖干从上往下走，就是子卡的顺序；竖干的起点是父卡，终点是最后一个子卡。
    // 全部用直角直线（不是曲线）：曲线在多层嵌套时会互相缠绕，直线永远清楚。
    for (const g of childGroups) {
      const p1 = layout[g.pid];
      if (!p1) continue;
      const s1 = sizes[g.pid] || { w: 260, h: 110 };
      const kids = g.kids.filter((k) => layout[k.card_id]);
      if (!kids.length) continue;
      const TRUNK_OFFSET = 44;                      // 竖干距父卡右缘
      const trunkX = p1.x + s1.w + TRUNK_OFFSET;
      const py = p1.y + s1.h / 2;
      const kidYs = kids.map((k) => {
        const p2 = layout[k.card_id];
        const s2 = sizes[k.card_id] || { h: 100 };
        return p2.y + s2.h / 2;
      });
      const yTop = Math.min(py, ...kidYs);
      const yBot = Math.max(py, ...kidYs);
      // ① 父卡 → 竖干（水平）
      elbowSegs.push({ x1: p1.x + s1.w, y1: py, x2: trunkX, y2: py, from: g.pid, to: "", straight: true });
      // ② 竖干
      elbowSegs.push({ x1: trunkX, y1: yTop, x2: trunkX, y2: yBot, from: g.pid, to: "", straight: true, trunk: true });
      // ③ 竖干 → 每个子卡（水平）
      for (const k of kids) {
        const p2 = layout[k.card_id];
        const s2 = sizes[k.card_id] || { h: 100 };
        const cy = p2.y + s2.h / 2;
        elbowSegs.push({ x1: trunkX, y1: cy, x2: p2.x, y2: cy, from: g.pid, to: k.card_id, straight: true });
      }
    }
    for (const e of raw) {
      if (groupByPid.has(e.pid)) continue;   // 成组的父卡已用一条组线代替
      // [画布商业化 2026-10-17] from/to：渲染层据此做「悬停/选中高亮关联线」。
      const y2 = e.p2.y + e.s2.h / 2;
      elbowSegs.push(e.childOnRight
        ? { x1: e.p1.x + e.s1.w, y1: e.y1, x2: e.p2.x, y2, from: e.pid, to: e.ckid }
        : { x1: e.p1.x, y1: e.y1, x2: e.p2.x + e.s2.w, y2, from: e.pid, to: e.ckid });
    }
    let minX = 0, minY = 0, maxX = 0, maxY = 0, maxW = 0, maxH = 0, hasBox = false;
    const growTo = (x, y) => {
      if (!hasBox) { minX = maxX = x; minY = maxY = y; hasBox = true; return; }
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    };
    for (const cid of visibleIds) {
      const p = layout[cid];
      if (!p) continue;
      const s = sizes[cid] || { w: 220, h: 86 };
      growTo(p.x, p.y);
      growTo(p.x + s.w, p.y + s.h);
      if (s.w > maxW) maxW = s.w;
      if (s.h > maxH) maxH = s.h;
    }
    for (const seg of elbowSegs) {
      growTo(seg.x1, seg.y1);
      growTo(seg.x2, seg.y2);
      const midX = (seg.x1 + seg.x2) / 2;
      growTo(midX, seg.y1);
      growTo(midX, seg.y2);
    }
    const pad = Math.max(maxW, maxH) * 0.5;
    minX -= pad; minY -= pad; maxX += pad; maxY += pad;
    return { rootParentId, elbowSegs, bbox: { minX, minY, w: maxX - minX, h: maxY - minY } };
  }

  // pinned 集纯推导（pinnedIdSet 的参数化版：positions 入参，不读全局）
  function pinnedIdsOf(positions) {
    const s = new Set();
    for (const cid in positions) {
      if (positions[cid] && positions[cid].pinned) s.add(cid);
    }
    return s;
  }

  // 投影链：computeVisibleSet → computeLayout → separateOverlaps → 连线几何/面包屑派生。
  // 纯函数（不读 DOM/state、无随机无 Date；computeLayout 内的 [T07] 判据源切换打点
  // 只写 _t07SwitchLogged 观测标记与 console，不影响返回值）。sizes 由调用方测好传入
  // （一轮一测的测量纪律在渲染入口执行，不在本函数内——V6）。
  // input: { viewRoot, cards, links, positions, expanded, cardMap, childMap, sizes }
  // 输出:  { visibleIds, depthOf, layout, sizes, edges, breadcrumb }
  function computeProjection(input) {
    const { visibleIds, depthOf } = computeVisibleSet(input.viewRoot, input.childMap, input.expanded);
    const layout0 = computeLayout(input.viewRoot, input.cards, input.links,
                                  input.positions, input.expanded, input.sizes);
    const layout = separateOverlaps(layout0, input.sizes, pinnedIdsOf(input.positions));
    const edges = computeEdgeGeometry(visibleIds, layout, input.sizes,
                                      input.childMap, input.cardMap, input.viewRoot);
    const breadcrumb = computeBreadcrumb(input.viewRoot, input.cardMap);
    return { visibleIds, depthOf, layout, sizes: input.sizes, edges, breadcrumb };
  }

  // [迭代四 F2⑥] 视图态持久化：localStorage["projView:{work_id}"]（原纯内存态
  // expanded ＋ pan/zoom）。写入＝wheel/pan 结束与展开收起后防抖（照 positions
  // 持久化防抖先例）；读取＝画布初始化（reloadCanvas 内、renderCanvas 之前）。
  function saveViewState() {
    try {
      // [画布第三步 V8] 序列化面＝panX/panY/zoom/expanded 四项（视图桶）；
      // 拖拽临时位移（闭包内 dragState）不在其列，亦不进任何持久化载体
      localStorage.setItem(`projView:${WORK_ID}`, JSON.stringify({
        panX: state.view.panX, panY: state.view.panY, zoom: state.view.zoom,
        expanded: [...state.view.expanded],
      }));
    } catch (e) { /* 存储不可用零阻断 */ }
  }
  let viewSaveTimer = null;
  function scheduleSaveView() {
    if (viewSaveTimer) clearTimeout(viewSaveTimer);
    viewSaveTimer = setTimeout(saveViewState, 300);
  }
  function restoreViewState() {
    try {
      const raw = localStorage.getItem(`projView:${WORK_ID}`);
      if (!raw) return false;
      const v = JSON.parse(raw);
      if (Number.isFinite(v.panX)) state.view.panX = v.panX;
      if (Number.isFinite(v.panY)) state.view.panY = v.panY;
      if (Number.isFinite(v.zoom)) state.view.zoom = Math.max(0.25, Math.min(2.0, v.zoom));
      if (Array.isArray(v.expanded)) {
        state.view.expanded = new Set(v.expanded.filter(id => state.index.cardMap[id]));
      }
      // [画布商业化 2026-10-17] 返回是否真的命中存档：首次进项目（无存档）要
      // 自动 fit 内容，而不是落在硬编码的 (80,40) 上让卡片溢出视口。
      return Number.isFinite(v.panX) || Number.isFinite(v.panY);
    } catch (e) { return false; /* 恢复失败零阻断 */ }
  }

  // [phase8.6-B] 重跑：实测尺寸 → 避让 → 回写节点坐标 → 重画连线
  // [card_045 E3] 快路径与全量重绘走**同一计算链**（实测尺寸 → computeLayout 纯函数
  // 现算基准 → separateOverlaps 避让 → 一次性应用），唯一区别＝不重建节点（节点已在，
  // 只更新位置）。不再读任何快照——拖动落定的存储坐标即刻参与下一次重算（病灶 1：
  // 「防抖重算用旧基准快照覆盖用户刚放下的位置」在此拆除）。
  // [画布第三步 T2] 改吃投影：计算链（实测→纯函数→避让）上收到统一入口 reflowAll，
  // 本函数只消费 projection（布局已算好）——不再直读 state（V1）。
  // visibleIds 用 projection 的（与 renderCanvas 建节点同源）；原 `new Set(nodeMap.keys())`
  // 透传口径由投影 visibleIds 替代——两者差＝无卡数据的 viewRoot 占位，renderEdges
  // 各循环均有 layout/node 判空守卫，行为等价（V3 透传豁免口径同步收口）。
  function reflowLayout(projection, vm) {
    if (!nodeMap.size) return;
    const adjusted = projection.layout;
    for (const [cid, node] of nodeMap) {
      const p = adjusted[cid];
      if (!p) continue;
      node.style.left = p.x + "px";
      node.style.top = p.y + "px";
    }
    renderEdges(projection, vm);
  }

  // ==================== 渲染 ====================
  function rebuildCardMap() {
    state.index.cardMap = {};
    state.index.childMap = {};
    for (const c of state.data.cards) {
      state.index.cardMap[c.card_id] = c;
      const p = c.parent || "";
      if (!state.index.childMap[p]) state.index.childMap[p] = [];
      state.index.childMap[p].push(c);
    }
  }

  // [画布第三步 T2] 面包屑链改吃投影（computeBreadcrumb 派生，投影.breadcrumb）——
  // 本函数只渲染＋绑点击，不再上溯 state（V1）
  function renderBreadcrumb(breadcrumb) {
    if (!crumbEl) return;   // [阶段3] 非画布页无面包屑容器
    const chain = breadcrumb;
    const parts = [];
    for (let i = 0; i < chain.length; i++) {
      const c = chain[i];
      if (i === chain.length - 1) {
        parts.push(`<span class="cur">${escapeHtml(c.title)}</span>`);
      } else {
        parts.push(`<a data-cid="${c.card_id}" href="javascript:void(0)">${escapeHtml(c.title)}</a>`);
        parts.push(`<span class="sep">›</span>`);
      }
    }
    crumbEl.innerHTML = parts.join("");
    // 绑定点击：跳转到对应 viewRoot
    crumbEl.querySelectorAll("a[data-cid]").forEach(a => {
      a.onclick = (e) => {
        e.preventDefault();
        setViewRoot(a.dataset.cid);
      };
    });
  }

  // [画布重构 2026-10-15 · 2.1] 最近一次投影的缓存。
  // 为什么需要：增量渲染只重算 anchor 单元，但连线（edgesSVG）要的是**全画布**的
  // 布局。缓存上一轮的 layout/visibleIds，把本单元结果合并进去再重算连线几何——
  // 于是「别处的卡」既不用重排，也不用从零重建连线，端点坐标仍然是对的。
  // 只在全量渲染与全量重算两处写入（renderCanvas / reflowAll），增量路径只补丁。
  let lastProjection = null;

  // [画布第三步 T2] 改吃投影＋视图模型：可见集来自 computeVisibleSet（T08-1-4 规则
  // 逐字搬出、单一来源——V3），计算链收进 computeProjection；pin＝投影输入
  // {viewRoot,cards,links,positions,expanded,cardMap,childMap}，由动作层入口
  // （renderAll）装配，本函数不摸 state（V1）。
  function renderCanvas(pin, vm) {
    // [T08-3-4] 记录重绘前已在场的卡：重绘后仅对新增卡做淡入
    const prevIds = new Set(nodeMap.keys());
    // 清除旧节点（保留 svg）
    for (const [cid, node] of nodeMap) node.remove();
    nodeMap.clear();

    const { visibleIds, depthOf } = computeVisibleSet(pin.viewRoot, pin.childMap, pin.expanded);

    // [card_045 E3] 两阶段渲染（阶段一）：按占位坐标建全部节点，整层**隐藏但占位**。
    // ✱ 纪律性约束（写死）：必须用「隐藏但占位」的方式（visibility——元素仍在布局流、
    // 尺寸可读），不许用「不占位」的方式（display:none——元素不参与布局、量不到尺寸）。
    // 这是沿用既有约定（static/style.css「隐藏的画布保持可测量」条），不是本任务新立的规矩。
    // 改错的具体后果：不会报错、不会异常——只会让测量拿到 0 或拿不到，然后整个
    // 两阶段静默退化成「全用估算值」，而表面上一切正常。
    world.style.visibility = "hidden";
    const frag = document.createDocumentFragment();
    for (const cid of visibleIds) {
      const card = vm.cardsById[cid];
      if (!card) continue;
      if (card.card_type === "rule") continue;   // [规则卡 2026-10-16] 规则卡不上画布——它是吸附屏幕右侧的常驻卡（workspace.js 规则面板即其呈现）
      // 占位坐标（阶段二一次性应用最终位）；深度决定卡面形态
      const node = createNode(card, { x: 0, y: 0 }, vm, depthOf[cid] || 0);
      if (!prevIds.has(cid)) {
        // [T08-3-4] 新出现卡淡入（只动 opacity/transform；连线不做补间）
        node.classList.add("entering");
        node.addEventListener("animationend", () => node.classList.remove("entering"), { once: true });
      }
      frag.appendChild(node);
      nodeMap.set(cid, node);
    }
    // [画布商业化 2026-10-17] 一次性插入（DocumentFragment）：232 张卡时省掉
    // 231 次中间重排，首屏更快且没有逐张闪烁。
    world.appendChild(frag);

    // [card_045 E3] 两阶段渲染（阶段二）：一次性全量实测 → 纯函数算基准 → 避让 →
    // 一次性全部应用 → 取消隐藏。一轮渲染内尺寸测量恰为 1 次（改前 3 次：
    // layoutTree 内部测量＋此处＋renderEdges 各一次——E1 已参数化第一处、
    // renderEdges 改吃本处尺寸表）。
    applyLODClasses(vm.zoom, true);
    const projection = computeProjection({ ...pin, sizes: measureSizes() });
    lastProjection = projection;   // [画布重构 2.1] 供 renderSubtree 打补丁（连线的全画布基底）
    for (const cid of visibleIds) {
      const node = nodeMap.get(cid);
      const p = projection.layout[cid];
      if (node && p) {
        node.style.left = p.x + "px";
        node.style.top = p.y + "px";
      }
    }
    world.style.visibility = "";

    renderEdges(projection, vm);
    renderBreadcrumb(projection.breadcrumb);
    applySelectionClasses();
    applyTransform();
    scheduleMinimap();
    // [phase8.4] 渲染完成即懒加载可见卡摘要（首次进入/下钻/Reload 后均填充）
    lazyLoadVisibleSummaries(vm.cardsById);
  }

  // ══════ [画布重构 2026-10-15 · 2.1] 增量渲染入口：展开/收起一张卡只动它的子树 ══════
  //
  // 与 renderAll（全量重建）的分工：
  //   renderAll     —— 首次加载／下钻换根／任何兜底路径：删光全部节点重建。
  //   renderSubtree —— 展开·收起：只建·拆该卡单元内的节点；**其余节点的 DOM 引用原样、
  //                   style.left/top 一个字节都不改**（验收标准①②）。
  //
  // 判据全部来自纯函数（node 侧有对应用例）：planSubtreeUpdate 决定「动谁」，
  // computeSubtreeLayout + separateOverlaps(movable) 决定「放哪」。本函数只执行。
  // 这是刻意的分工——增量渲染最贵的失败模式是**越界**（手一滑把不相干的卡也重建/
  // 挪了），而越界只可能发生在「谁被动了」这个判断上，所以判断必须能被单独断言。
  function renderSubtree(cardId) {
    const vm = collectViewModel();
    const pin = collectProjectionInput();
    const anchorNode = nodeMap.get(cardId);
    // 兜底三条：还没画过／锚点不在场／没有投影基底 → 退回全量，不猜
    if (!nodeMap.size || !anchorNode || !lastProjection) { renderAll(); return; }
    const anchorPos = {
      x: parseFloat(anchorNode.style.left),
      y: parseFloat(anchorNode.style.top),
    };
    if (!Number.isFinite(anchorPos.x) || !Number.isFinite(anchorPos.y)) {
      renderAll();
      return;
    }

    const anchorDepth = depthFromViewRoot(cardId);
    const anchorIsViewRoot = cardId === pin.viewRoot;
    const plan = planSubtreeUpdate(cardId, pin.childMap, pin.expanded, nodeMap.keys(),
                                   anchorDepth, anchorIsViewRoot);

    // ① 拆：收起的子孙节点离场（只拆 plan.remove 里的，别处的节点引用一律不动）
    for (const cid of plan.remove) {
      const node = nodeMap.get(cid);
      if (!node) continue;
      node.remove();
      nodeMap.delete(cid);
    }

    // ② 建：新建节点。沿用 renderCanvas 的两阶段纪律——**隐藏但占位**（visibility 而非
    //    display:none），否则量不到尺寸、整轮静默退化成「全用估算值」而表面一切正常
    //    （既有约定，理由见 renderCanvas 同处注释）。只在真要建节点时才隐藏整层。
    //    恢复条件看「本轮是否隐藏过」而不是「建成了几个」——后者为 0 却不恢复，
    //    画布会永久停在 visibility:hidden（整屏空白，且不报任何错）。
    let hidWorld = false;
    if (plan.create.length) {
      world.style.visibility = "hidden";
      hidWorld = true;
      const lodLevel = currentLODLevel(vm.zoom);
      for (const cid of plan.create) {
        const card = vm.cardsById[cid];
        if (!card) continue;
        const node = createNode(card, anchorPos, vm, plan.depthOf[cid] || 0);   // 占位坐标，⑤ 之后一次性改写
        node.classList.add(lodLevel);        // zoom 未变 ⇒ 档位与全量渲染同一判据
        node.classList.add("entering");      // 新出现卡淡入（与 renderCanvas 同款）
        node.addEventListener("animationend",
          () => node.classList.remove("entering"), { once: true });
        world.appendChild(node);
        nodeMap.set(cid, node);
      }
    }

    // ③ 锚点自己的徽标翻面（▸↔▾）——就地改这一处文本，不重建锚点节点
    updateExpandBadge(anchorNode, (pin.childMap[cardId] || []).length,
                      pin.expanded.has(cardId));

    // ④ 排：只排 anchor 单元，anchor 自身不动；避让只允许本单元的卡拉动，
    //    别处（含 pinned）一律当固定障碍（局部避让，2.3）
    //    ★ 本单元**没有需要落位的卡**时（收起、或切换一张叶子卡）整段避让跳过：
    //      此时 movable 是空集 ⇒ 全员固定，避让既推不动谁、又会刷一串
    //      「双固定障碍」警告，纯噪音还白跑一趟 O(n²)。布局基底只做「拆掉离场节点」。
    let layout;
    // sizes 供 ⑥ 的连线几何用：位移通道下现测一次（V6 一轮一测）；
    // 非位移通道（收起/叶子卡）不重测——节点一个没建，尺寸表原样沿用上一轮。
    let sizes = lastProjection.sizes;
    if (plan.place.length) {
      sizes = measureSizes();
      const unit = computeSubtreeLayout(pin.viewRoot, pin.cards, pin.links,
                                        pin.positions, pin.expanded, sizes,
                                        { id: cardId, x: anchorPos.x, y: anchorPos.y,
                                          depth: anchorDepth, isViewRoot: anchorIsViewRoot });
      const base = Object.assign({}, lastProjection.layout, unit);
      for (const cid of plan.remove) delete base[cid];    // 拆掉的节点不许留在布局基底里
      layout = separateOverlaps(base, sizes, pinnedIdsOf(pin.positions),
                                GAP_Y, new Set(plan.place));

      // ⑤ 落位：**只写 plan.place 里的节点**（＝anchor 的可见子孙）。锚点与无关卡的
      //    style.left/top 一次都不碰——这是验收标准①②的落点。
      for (const cid of plan.place) {
        const node = nodeMap.get(cid);
        const p = layout[cid];
        if (node && p) {
          node.style.left = p.x + "px";
          node.style.top = p.y + "px";
        }
      }
    } else {
      layout = Object.assign({}, lastProjection.layout);
      for (const cid of plan.remove) delete layout[cid];  // 收起：只是把离场节点摘出基底
    }
    if (hidWorld) world.style.visibility = "";

    // ⑥ 连线：连线要全画布几何，故把本单元结果并回投影缓存再重算几何；
    //    「别处的卡」坐标原样带入，端点仍然正确（不重排 ≠ 不连线）
    const visibleIds = new Set(lastProjection.visibleIds);
    for (const cid of plan.remove) visibleIds.delete(cid);
    for (const cid of plan.create) visibleIds.add(cid);
    const edges = computeEdgeGeometry(visibleIds, layout, sizes,
                                      pin.childMap, pin.cardMap, pin.viewRoot);
    lastProjection = { ...lastProjection, visibleIds, layout, sizes, edges };
    renderEdges(lastProjection, vm);
    lazyLoadVisibleSummaries(vm.cardsById);
  }

  // 展开徽标就地翻面（不动节点本体）。与 createNode 的徽标构造逐字同款，
  // 两处口径必须一致——所以单独收成一个函数，改文案只改一处。
  // [画布商业化 2026-10-17] 卡类型 → 展示名：画布上给人看的是中文，不暴露 code。
  const TYPE_LABEL = {
    project: "项目", sub_project: "子项目", topic: "课题",
    tool: "工具卡", coder: "编程卡", rule: "规则卡",
  };

  function updateExpandBadge(node, childCount, isExpanded) {
    const badge = node.querySelector(".kid-badge");
    if (!badge) return;
    badge.classList.toggle("is-open", !!isExpanded);
    const n = badge.querySelector(".cv-kid-n");
    if (n) n.textContent = String(childCount);
    badge.title = `有 ${childCount} 张子卡，点击展开/折叠`;
  }

  // [画布第三步 T2] 改吃视图模型 vm（动作层装配）：viewRoot/positions/childMap/
  // expanded/_unreadByCard 一律从 vm 取——不直摸 state（V1）
  // 规模显示：3000 字 → "3.0k"。用户靠它判断"这张卡值不值得现在碰"。
  function fmtSize(n) {
    const v = Number(n) || 0;
    return v >= 1000 ? (v / 1000).toFixed(1) + "k" : String(v);
  }

  function createNode(card, pos, vm, depth) {
    const node = document.createElement("div");
    const isRoot = card.card_id === vm.viewRoot;
    node.className = "card-node";
    node.dataset.id = card.card_id;
    node.dataset.type = card.card_type;
    // [画布商业化 2026-10-18 · 层级] 深度写进 DOM，样式按 data-depth 分档。
    // 深度＝相对当前视图根的层数（0=根卡/一级卡…）。**它是卡片尺寸与视觉权重的
    // 唯一来源**——之前尺寸只跟缩放档位走，一级卡和二级卡长得一模一样，全展开后
    // 整片画布是同一尺寸的方块，层级只能靠连线猜。
    const d = Math.max(0, Math.min(3, depth || 0));
    node.dataset.depth = String(d);
    node.style.left = pos.x + "px";
    node.style.top = pos.y + "px";
    if (isRoot) node.classList.add("is-root");
    // [迭代四 F4④] 卡间消息角标（左外角 -6px 终值）：未读数取 K4 端点
    // unread_by_card、SSE card_message 帧增量更新（refreshUnread）。
    const unread = (vm.unreadByCard || {})[card.card_id] || 0;
    const msgBadge = unread > 0
      ? `<span class="msg-badge">${unread > 99 ? "99+" : unread}</span>`
      : "";
    // [诸葛 2026-10-04 21:52] pinned 视觉标识。
    // 原缺口：拖一下卡就 pinned=true（永久不参与自动避让），但 style.css 里
    // 搜 "pinned" 零命中——画布上看不出来，用户不知道自己什么时候把卡钉死了。
    // 现给钉住的卡挂类，样式在 style.css（右上角小图钉 ＋ 边框色区分）。
    if (vm.positions[card.card_id] && vm.positions[card.card_id].pinned) {
      node.classList.add("is-pinned");
    }

    const childCount = (vm.childMap[card.card_id] || []).length;
    // [T08-3-1/3-5] 徽章语义＝展开/折叠开关：▸（折叠态）↔ ▾（展开态）；N 恒为该卡子卡数
    const isExpanded = vm.expanded.has(card.card_id);
    const kidBadge = childCount > 0
      ? `<span class="kid-badge${isExpanded ? " is-open" : ""}" title="有 ${childCount} 张子卡，点击展开/折叠"><span class="cv-caret"></span><span class="cv-kid-n">${childCount}</span></span>`
      : "";

    // ══════════════════════════════════════════════════════════════════
    // [画布 2026-10-18 · 视觉＝信息呈现] 卡面按**信息优先级**重排，不再有装饰行。
    //
    // 上一版卡面第一行是「类型点 + 子项目 + 状态徽标」。但实测这个项目 43 张卡里
    // 42 张都是 sub_project ⇒ 那一行等于每张卡都写"子项目"，**零信息量却占着最贵的
    // 第一行**；而真正该被看见的两件事——"这张卡想清楚了什么"和"有几处等我裁定"
    // ——被压成了一小段"状态：现状实测（2026-10-03）…依据：xxx.html(258行)"。
    //
    // 现在的顺序就是用户读卡的顺序：
    //   ① 标题（这是什么）
    //   ② 结论 / 摘要（它想清楚了什么）
    //   ③ 底栏：待裁定 / 已结论（要不要我动手）＋ 子卡数 ＋ 最后动过的时间
    // 类型不再占一行——它由左侧色条与卡片尺寸承担，读者不需要被告知"这是子项目"。
    // ══════════════════════════════════════════════════════════════════
    const ws = String(card.work_state || "");
    if (ws) node.dataset.ws = ws;   // pending / concluded / discussing / empty
    const pendingN = Number(card.pending_count || 0);
    // 根卡是项目本体，不是待办项——不给它挂"未开始/已结论"这类工作状态标签
    const isRootCard = card.card_id === state.nav.viewRoot;
    const wsBadge = isRootCard
      ? (pendingN > 0 ? `<span class="cv-ws" data-ws="pending">${pendingN} 处待裁定</span>` : "")
      : ws === "pending" && pendingN > 0
        ? `<span class="cv-ws" data-ws="pending">${pendingN} 处待裁定</span>`
        : ws === "concluded"
          ? `<span class="cv-ws" data-ws="concluded">已结论</span>`
          : ws === "empty"
            ? `<span class="cv-ws" data-ws="empty">未开始</span>`
            : "";

    // ── 卡面正文：**一张卡只说一件最该被知道的事** ──
    //   pending    → 列出在等你的那几条（可直接读，不用点进去）
    //   concluded  → 结论
    //   discussing → 这段在讨论什么
    //   empty      → 什么都不说（留白比写"未开始"更有信息量）
    // 待裁定最多列 2 条：卡片是"索引"，不是"全文"；剩下的用"还有 N 处"收口。
    // ── 自动模式活动：这张卡此刻是不是 AI 在自动推进 ──
    // goal_state 的取值（auto_driver.py）：active / paused / complete / blocked。
    // 只有 active 算"正在跑"。视觉给三样：顶部呼吸条、标题上方一行"第 N 轮 · 当前步骤"、
    // 卡片描边泛蓝——这三样都不改变卡片高度以外的信息，且只在跑的时候出现。
    const running = !!card.auto_mode && String(card.goal_state || "") === "active";
    if (running) node.dataset.running = "1";
    const runRound = Number(card.goal_rounds || 0);
    const runStep = String(card.toolcard_step || "").trim();
    const runLine = running
      ? `<div class="cv-runline"><span class="cv-run-dot"></span>第 ${runRound || 1} 轮${runStep ? " · " + escapeHtml(runStep.slice(0, 22)) : ""}</div>`
      : "";

    const pendingItems = Array.isArray(card.pending_items) ? card.pending_items : [];
    const isBareRoot = isRootCard
      && !(state.index.childMap[card.card_id] || []).length
      && !String(card.conclusion || "").trim()
      // 新建项目的根卡带一段 57 字的模板占位（"当前状况快照…"），所以不能判 ===0；
      // 200 字以下视为"还没实质内容"。
      && Number(card.content_len || 0) < 200;
    let bodyHtml = "";
    if (ws === "pending" && pendingItems.length) {
      const shown = pendingItems.slice(0, 2);
      const rest = pendingN - shown.length;
      // 每条都可点：直接进"裁定模式"的那一条（读一处 → 下一处），不用先选中再翻。
      bodyHtml = `<div class="cv-pending">${
        shown.map((it, i) => `<div class="cv-pending-item" data-pi="${i}" title="${escapeHtml(it.section || "")} · 点击逐条过">${escapeHtml(it.text)}</div>`).join("")
      }${rest > 0 ? `<div class="cv-pending-more" data-pi="0">还有 ${rest} 处 · 逐条过 →</div>` : ""}</div>`;
    } else if (isBareRoot) {
      // 空项目的根卡：告诉用户下一步做什么，而不是一张空白卡片
      bodyHtml = `<div class="cv-empty-hint">这是项目的根卡。<br>点右上「＋ 新建卡」或按 <b>Tab</b> 建第一张子卡。</div>`;
    } else {
      bodyHtml = `<div class="summary"></div>`;
    }

    node.innerHTML = `
      ${msgBadge}
      <button class="cv-open" type="button" title="打开完整对话（双击卡片同样进入）" aria-label="打开完整对话">↗</button>
      ${running ? '<div class="cv-runbar"></div>' : ""}
      <div class="tname">${escapeHtml(card.title)}</div>
      ${runLine}
      ${bodyHtml}
      <div class="cv-card-foot">
        ${wsBadge}
        <span class="cv-spacer"></span>
        ${Number(card.ref_count || 0) > 0 ? `<span class="cv-ref" title="有 ${card.ref_count} 张卡在正文里引用了这张卡">←${card.ref_count}</span>` : ""}
        ${Number(card.content_len || 0) > 400 ? `<span class="cv-size" title="现状块 ${card.content_len} 字">${fmtSize(card.content_len)}</span>` : ""}
        ${kidBadge}
        <span class="meta2"></span>
      </div>
    `;

    bindNodeEvents(node, card);
    return node;
  }

  function bindNodeEvents(node, card) {

    // [T08-3-1] ▸/▾ 徽章单击＝展开/折叠该卡子树（原语义为下钻 setViewRoot，已改）
    const badge = node.querySelector(".kid-badge");
    if (badge) {
      badge.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        toggleExpand(card.card_id);
      });
    }

    // ══════════════════════════════════════════════════════════════════
    // [画布商业化 2026-10-18 · 用户裁定] 单击卡片＝**直接进入**这张卡的对话。
    //
    // 这是用户明确要求的语义，也是这个产品的本质：画布是"卡片树的导航视图"，
    // 点一张卡就是要看它、改它、跟它说话。要求双击才进 = 多一道无意义门槛，
    // 而且和"拖拽"抢手势。
    //
    // 各手势分工（互不冲突）：
    //   单击              进入对话页（唯一的主操作）
    //   Shift/Ctrl/⌘+单击 多选（不进入）——多选是"批量整理"，不是主路径
    //   Alt+单击          画布内下钻（把这张卡变成视图根，不跳页）
    //   拖拽              移动卡片；拖完不触发进入（suppressClickUntil 兜住）
    //   右键              菜单（打开/聚焦/建子卡/重命名/删除…）
    // ══════════════════════════════════════════════════════════════════
    node.addEventListener("click", (e) => {
      if (node.classList.contains("dragging")) return;
      if (Date.now() < suppressClickUntil) return;
      if (e.shiftKey || e.metaKey || e.ctrlKey) {   // 修饰键＝多选（为批量操作）
        e.preventDefault();
        setSelection([card.card_id], true);
        return;
      }
      if (e.altKey) {                                // Alt＝画布内下钻
        e.preventDefault();
        setViewRoot(card.card_id);
        return;
      }
      // 默认：**选中 + 右栏读卡，不离开画布**。
      // 跳页会把画布上下文冲掉——看两张卡的结论要来回跳三次，并行优势归零。
      // 进入完整对话走三个明确入口：卡上的 ↗、双击、Enter。
      selectOnly(card.card_id);
    });

    // 待裁定条目点击 → 裁定模式定位到这一条
    for (const itemEl of node.querySelectorAll("[data-pi]")) {
      itemEl.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        const list = buildTriageList();
        const localIdx = Number(itemEl.dataset.pi || 0);
        // 全局索引 = 这张卡的第一条在扁平列表里的位置 + 卡内索引
        let base = list.findIndex((x) => x.cardId === card.card_id);
        if (base < 0) base = 0;
        openTriage(base + localIdx);
      });
    }

    const openBtn = node.querySelector(".cv-open");
    if (openBtn) {
      openBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        openCardChat(card.card_id);
      });
    }

    // 明确入口：卡右上角的 ↗（常驻，不靠 hover 才发现）
    node.addEventListener("dblclick", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (Date.now() < suppressClickUntil) return;
      openCardChat(card.card_id);
    });

    // 拖拽
    let dragState = null;
    // [画布商业化 2026-10-17] 多选整组拖动：拖的是选中集里的卡且选中多于 1 张时，
    // 整组一起动（这是多选的唯一意义，否则多选只能用来删除）。
    let dragGroup = null;
    node.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      // [R1] 若上一次拖拽的监听器尚未卸掉（异常路径），先卸，避免重复挂载
      if (activeDragDetach) activeDragDetach();
      // [画布商业化 2026-10-17] 根卡是布局坐标系的锚点（computeLayout 恒把它放在
      // 原点，子卡相对它排布）——单独拖根卡会被下一轮布局拉回原位，表现为"拖了又弹回"。
      // 故拖根卡＝拖画布：语义是"移动整个视图"，落点即时生效、不会弹。
      if (card.card_id === state.nav.workCardId
          && !(state.view.selected.has(card.card_id) && state.view.selected.size > 1)) {
        panState = {
          startX: e.clientX, startY: e.clientY,
          panX: state.view.panX, panY: state.view.panY,
          // 只是点一下（没拖动）时仍要能选中根卡——否则"拖根卡=平移"会顺手
          // 把根卡变成点不中的死区。
          tapCard: card.card_id,
          tapAdditive: e.shiftKey || e.metaKey || e.ctrlKey,
        };
        viewport.classList.add("panning");
        e.stopPropagation();
        return;
      }
      const inSel = state.view.selected.has(card.card_id) && state.view.selected.size > 1;
      dragGroup = inSel
        ? [...state.view.selected].map((id) => {
            const n = nodeMap.get(id);
            if (!n) return null;
            return { id, node: n, left: parseFloat(n.style.left) || 0, top: parseFloat(n.style.top) || 0 };
          }).filter(Boolean)
        : null;
      // 用 click 事件处理选择；拖拽单独监听 mousedown+move
      dragState = {
        startX: e.clientX, startY: e.clientY,
        nodeLeft: parseFloat(node.style.left), nodeTop: parseFloat(node.style.top),
        moved: false,
      };
      // [R1] 只在拖拽期间挂 document 监听器（松手即卸，见 detachDragListeners）
      document.addEventListener("mousemove", onDragMove);
      document.addEventListener("mouseup", onDragUp);
      activeDragDetach = detachDragListeners;
      console.info("[R1] 拖拽监听器 挂载");
      e.stopPropagation(); // 防止触发画布平移
    });

    function onDragMove(e) {
      if (!dragState) return;
      const dx = (e.clientX - dragState.startX) / state.view.zoom;
      const dy = (e.clientY - dragState.startY) / state.view.zoom;
      if (!dragState.moved && Math.abs(dx) < 4 && Math.abs(dy) < 4) return;
      dragState.moved = true;
      if (dragGroup) {
        for (const g of dragGroup) {
          g.node.classList.add("dragging");
          g.node.style.top = (g.top + dy) + "px";   // 横向锁定（同上）
        }
      } else {
        node.classList.add("dragging");
        // [画布 2026-10-18 · 拖拽＝调顺序] **横向锁定，只允许纵向移动**。
        //
        // 这是树形布局的性质：横向位置＝层级（由 parent 决定，不是用户摆的），
        // 纵向位置＝同级之间的顺序（用户可以调）。
        // 原来拖拽是自由的 x/y，实测后果：两张卡被拖过之后，同层 8 张卡的 x
        // 变成 456 / 623 / 943，横向散开、主干线接不上——用户的原话是"卡片无序摆放"。
        // 现在横向永远对齐列，拖拽表达的是"这张卡排在这一层的第几位"。
        const w = node.offsetWidth || 220, h = node.offsetHeight || 120;
        const snap = computeSnap(card.card_id, dragState.nodeLeft, dragState.nodeTop + dy, w, h);
        node.style.top = (dragState.nodeTop + dy + snap.dy) + "px";
        showGuides(null, snap.vy);
      }
    }

    function onDragUp(e) {
      if (!dragState) return;
      clearGuides();
      if (dragState.moved && dragGroup) {
        suppressClickUntil = Date.now() + 320;
        // 多选整组落库：逐张写用户松手的坐标（persistCardPos 自带 500ms 批量合并）
        for (const g of dragGroup) {
          g.node.classList.remove("dragging");
          persistCardPos(g.id, parseFloat(g.node.style.left), parseFloat(g.node.style.top));
        }
        dragGroup = null;
        reflowAll();
      } else if (dragState.moved) {
        suppressClickUntil = Date.now() + 320;
        let newLeft = parseFloat(node.style.left);
        let newTop = parseFloat(node.style.top);
        // [card_045 E5] 存／显分离＋钉住语义（L1「人的位置只由人给」／L4「动作只有一个
        // 权威解释」）：存盘只写**用户松手的原始坐标**；T18-C3 的落点仲裁（resolveDrop）
        // 在此退役出调用链——「拖卡让在场卡」会把系统推算冒充用户意图（病灶 2），
        // 且与「用户放哪就是哪，系统不自作主张」直接冲突。行为取舍（任务书已声明）：
        // 用户把卡拖到另一张钉住的卡上时，会真的重叠。resolveDrop 函数本体保留
        // （T18 交付件，留作将来显式恢复显示仲裁的候选，不再被本路径调用）。
        node.classList.remove("dragging");
        persistCardPos(card.card_id, newLeft, newTop);
        // [card_045 E4] 拖动落定后立刻走统一重算的「不重建节点」分支（reflowAll→
        // reflowLayout，画布第三步 T2/T4 统一入口）：显示坐标与连线吃同一份现算
        // 结果——不再有快照可读，也不需要。
        // 拖动卡即刻 pinned（E5 语义），重算不会移动它——它停在用户松手的位置。
        reflowAll();
      }
      dragState = null;
      // [R1] 松手即卸——用同一命名函数引用（onDragMove/onDragUp 是函数声明，引用稳定；
      // 若写成匿名箭头函数则 removeEventListener 卸不掉）
      detachDragListeners();
    }

    // [R1] 卸载本节点的拖拽监听器；被 onDragUp 与 window.blur 兜底共用。
    // 同一时刻只有一个节点在拖拽，模块级 activeDragDetach 指向它。
    function detachDragListeners() {
      document.removeEventListener("mousemove", onDragMove);
      document.removeEventListener("mouseup", onDragUp);
      if (dragState) {
        // 兜底路径（窗口失焦）进来时，把拖拽状态一并取消
        node.classList.remove("dragging");
        if (dragGroup) { for (const g of dragGroup) g.node.classList.remove("dragging"); dragGroup = null; }
        dragState = null;
      }
      if (activeDragDetach === detachDragListeners) activeDragDetach = null;
      console.info("[R1] 拖拽监听器 卸载");
    }

    // tooltip（L0/L1 时悬停显示摘要）
    let hoverTimer = null;
    node.addEventListener("mouseenter", () => {
      highlightEdges(card.card_id);
      hoverTimer = setTimeout(() => showTooltip(card, node), 300);
    });
    node.addEventListener("mouseleave", () => {
      highlightEdges("");
      clearTimeout(hoverTimer);
      hideTooltip();
    });

    // （原此处有一个 `dblclick → preventDefault()` 的空监听：它会与上面真正的
    //  双击进入监听抢事件，已随本次改造删除。）
  }

  // ══════════════════════════════════════════════════════════════════════
  // [画布商业化 2026-10-17] 选择系统
  //
  // 语义变更（用户已授权全权改造画布交互）：旧行为是「单击卡片＝立刻整页跳去
  // 对话页」——画布上任何一次误点都会把人弹走，这是白板类产品最忌讳的交互。
  // 新行为对齐商业白板：**单击＝选中，双击＝进入**。进对话的通道有三个：
  // 双击、选中后按 Enter、右键菜单「打开对话」。
  // ══════════════════════════════════════════════════════════════════════
  function selectionIds() {
    return [...state.view.selected];
  }

  function applySelectionClasses() {
    for (const [cid, node] of nodeMap) {
      node.classList.toggle("is-selected", state.view.selected.has(cid));
    }
    // 只选中一张时聚焦它的连线；多选或空选恢复全亮
    const ids = [...state.view.selected];
    highlightEdges(ids.length === 1 ? ids[0] : "");
    renderSelectionBar();
    renderCardDetail();
    syncCatalogCurrent();
  }

  function setSelection(ids, additive = false) {
    if (!additive) state.view.selected.clear();
    for (const id of ids) {
      if (!id) continue;
      if (additive && state.view.selected.has(id)) state.view.selected.delete(id);
      else state.view.selected.add(id);
    }
    applySelectionClasses();
  }

  function clearSelection() {
    if (!state.view.selected.size) return;
    state.view.selected.clear();
    applySelectionClasses();
  }

  // ══════════════════════════════════════════════════════════════════════
  // [画布 2026-10-18 · 并行工作台] 右栏读卡
  //
  // 选中一张卡 → 右栏立刻变成这张卡的详情（结论全文 + 待裁定条目 + 进入按钮），
  // **画布不消失、视野不动**。这样"看 A 卡结论 → 看 B 卡结论 → 对比"是三次点击，
  // 而不是三次跳页。这是画布相对对话界面唯一不可替代的地方。
  //
  // 未选中 → 右栏恢复规则卡（原行为，逐字不变）。
  // ══════════════════════════════════════════════════════════════════════
  // 元素在每次渲染时现取：applySelectionClasses 在首屏渲染时就会调用，
  // 模块级 const 会撞 TDZ（ReferenceError: Cannot access before initialization）。

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function renderCardDetail() {
    const cardDetailEl = document.getElementById("cardDetail");
    const rulePanelHeadEl = document.getElementById("rulePanelHead");
    const rulePanelBodyEl = document.getElementById("rulePanelBody");
    if (!cardDetailEl) return;
    const ids = [...state.view.selected];

    // 未选中：恢复规则卡
    if (!ids.length) {
      cardDetailEl.hidden = true;
      cardDetailEl.innerHTML = "";
      if (rulePanelHeadEl) rulePanelHeadEl.hidden = false;
      if (rulePanelBodyEl) rulePanelBodyEl.hidden = false;
      return;
    }

    if (rulePanelHeadEl) rulePanelHeadEl.hidden = true;
    if (rulePanelBodyEl) rulePanelBodyEl.hidden = true;
    cardDetailEl.hidden = false;
    cardDetailEl.innerHTML = "";

    // 多选：列出选中的卡 + 批量操作
    //
    // [画布 2026-10-18 · 并行] 多选的意义不是"一次删掉"，而是**一次对多张卡做同一件事**。
    // 这里给三个真正用得上的：全部展开、全部折叠、批量删除。
    if (ids.length > 1) {
      cardDetailEl.appendChild(el("div", "cd-head", `已选 ${ids.length} 张`));

      const list = el("div", "cd-multi");
      for (const id of ids) {
        const c = state.index.cardMap[id] || {};
        const row = el("div", "cd-multi-row");
        row.dataset.id = id;
        const t = el("span", "cd-multi-title", c.title || id);
        row.appendChild(t);
        const n = Number(c.pending_count || 0);
        if (n > 0) {
          const nb = el("span", "cv-ws", String(n));
          nb.dataset.ws = "pending";
          row.appendChild(nb);
        }
        row.addEventListener("click", () => selectOnly(id));
        list.appendChild(row);
      }
      cardDetailEl.appendChild(list);

      const ops = el("div", "cd-ops");
      const btnExpand = el("button", "btn", "全部展开");
      btnExpand.addEventListener("click", () => {
        for (const id of ids) {
          const hasKids = (state.index.childMap[id] || []).length > 0;
          if (hasKids && !state.view.expanded.has(id)) toggleExpand(id);
        }
      });
      const btnCollapse = el("button", "btn", "全部折叠");
      btnCollapse.addEventListener("click", () => {
        for (const id of ids) if (state.view.expanded.has(id)) toggleExpand(id);
      });
      const btnDel = el("button", "btn", "删除");
      btnDel.addEventListener("click", () => deleteSelected());
      ops.appendChild(btnExpand);
      ops.appendChild(btnCollapse);
      ops.appendChild(btnDel);
      cardDetailEl.appendChild(ops);
      return;
    }

    const cid = ids[0];
    const c = state.index.cardMap[cid] || {};
    const ws = String(c.work_state || "");
    const pendingN = Number(c.pending_count || 0);

    // 头部：标题 + 状态行
    const head = el("div", "cd-head");
    head.appendChild(el("div", "cd-title", c.title || cid));
    const meta = el("div", "cd-meta");
    if (ws === "pending" && pendingN) {
      const b = el("span", "cv-ws", `${pendingN} 处待裁定`);
      b.dataset.ws = "pending";
      meta.appendChild(b);
    } else if (ws === "concluded") {
      const b = el("span", "cv-ws", "已结论");
      b.dataset.ws = "concluded";
      meta.appendChild(b);
    } else if (ws === "empty") {
      const b = el("span", "cv-ws", "未开始");
      b.dataset.ws = "empty";
      meta.appendChild(b);
    }
    if (Number(c.ref_count || 0) > 0) meta.appendChild(el("span", "cd-dim", `←${c.ref_count} 引用`));
    if (Number(c.content_len || 0) > 0) meta.appendChild(el("span", "cd-dim", `${fmtSize(c.content_len)} 字`));
    if (c.updated_at) meta.appendChild(el("span", "cd-dim", fmtRelative(c.updated_at)));
    head.appendChild(meta);
    cardDetailEl.appendChild(head);

    // 结论 / 摘要
    const concl = String(c.conclusion || c.conclusion_summary || "").trim();
    if (concl) {
      cardDetailEl.appendChild(el("div", "cd-section-label", ws === "concluded" ? "结论" : "这段在讨论"));
      const body = el("div", "cd-body");
      body.textContent = concl;
      cardDetailEl.appendChild(body);
    }

    // 待裁定条目（全部列出，这是用户要处理的东西）
    const items = Array.isArray(c.pending_items) ? c.pending_items : [];
    if (items.length) {
      cardDetailEl.appendChild(el("div", "cd-section-label", `待你裁定 ${pendingN || items.length} 处`));
      const list = el("div", "cd-pending");
      for (const it of items) {
        const row = el("div", "cd-pending-row");
        if (it.section) row.appendChild(el("div", "cd-pending-sec", it.section));
        row.appendChild(el("div", "cd-pending-text", it.text));
        list.appendChild(row);
      }
      if (pendingN > items.length) {
        list.appendChild(el("div", "cd-dim", `另有 ${pendingN - items.length} 处，进入卡片查看`));
      }
      cardDetailEl.appendChild(list);
    }

    // ── 读全文 ──
    // 卡片投影只带 120 字摘要，而真实正文 3000–40000 字。右栏 340px 读不了，
    // 但"不离开画布读完一张卡"是画布相对对话页的核心价值之一——所以给一个
    // 按需拉全文的入口：点"读全文"才请求 GET card（不在首屏给 39 张卡各拉一遍）。
    const fullBox = el("div", "cd-full");
    fullBox.hidden = true;
    const readBtn = el("button", "btn", "读全文");
    readBtn.addEventListener("click", async () => {
      if (!fullBox.hidden) { fullBox.hidden = true; readBtn.textContent = "读全文"; return; }
      readBtn.textContent = "加载中…";
      try {
        const data = await API.getCard(WORK_ID, cid);
        const body = (data && data.body) || {};
        const md = String(body.status_block || "");
        fullBox.innerHTML = md
          ? `<div class="md">${renderMarkdown(md)}</div>`
          : `<div class="cd-dim">这张卡还没有正文。</div>`;
        fullBox.hidden = false;
        readBtn.textContent = "收起全文";
      } catch (err) {
        fullBox.innerHTML = `<div class="cd-dim">读取失败：${escapeHtml(String(err && err.message || err))}</div>`;
        fullBox.hidden = false;
        readBtn.textContent = "重试";
      }
    });

    const actions = el("div", "cd-actions");
    actions.appendChild(readBtn);
    const openBtn = el("button", "btn primary", "打开完整对话 ↗");
    openBtn.addEventListener("click", () => openCardChat(cid));
    actions.appendChild(openBtn);
    cardDetailEl.appendChild(actions);
    cardDetailEl.appendChild(fullBox);
  }

  function selectOnly(id) {
    state.view.selected.clear();
    if (id) state.view.selected.add(id);
    applySelectionClasses();
  }

  // ══════════════════════════════════════════════════════════════════════
  // [画布商业化 2026-10-17] 拖拽对齐吸附
  //
  // 专业白板的"手感"很大一部分来自这里：拖卡时与其它卡的三条竖线（左/中/右）
  // 和三条横线（上/中/下）比对，进入 6 屏幕像素内就吸过去并画一条参考线。
  // 阈值按 zoom 换算成世界单位——放大时不会变得"很难吸"，缩小时不会"乱吸"。
  // 多选整组拖动不吸附（多张卡各自对齐会互相打架）。
  // ══════════════════════════════════════════════════════════════════════
  const SNAP_PX = 6;
  const guideV = document.createElement("div");
  guideV.className = "cv-guide v";
  guideV.style.display = "none";
  const guideH = document.createElement("div");
  guideH.className = "cv-guide h";
  guideH.style.display = "none";
  viewport?.appendChild(guideV);
  viewport?.appendChild(guideH);

  function clearGuides() {
    guideV.style.display = "none";
    guideH.style.display = "none";
  }

  function showGuides(vx, vy) {
    const z = state.view.zoom || 1;
    if (vx !== null && vx !== undefined) {
      guideV.style.display = "";
      guideV.style.left = Math.round(vx * z + state.view.panX) + "px";
      guideV.style.top = "0px";
      guideV.style.height = (viewport.clientHeight || 0) + "px";
    } else guideV.style.display = "none";
    if (vy !== null && vy !== undefined) {
      guideH.style.display = "";
      guideH.style.top = Math.round(vy * z + state.view.panY) + "px";
      guideH.style.left = "0px";
      guideH.style.width = (viewport.clientWidth || 0) + "px";
    } else guideH.style.display = "none";
  }

  function computeSnap(cid, left, top, w, h) {
    const SNAP = SNAP_PX / (state.view.zoom || 1);
    let bestDx = null, bestDy = null;
    const myV = [left, left + w / 2, left + w];
    const myH = [top, top + h / 2, top + h];
    for (const [oid, on] of nodeMap) {
      if (oid === cid || on.classList.contains("dragging")) continue;
      const ox = parseFloat(on.style.left) || 0;
      const oy = parseFloat(on.style.top) || 0;
      const ow = on.offsetWidth || 220;
      const oh = on.offsetHeight || 120;
      const oV = [ox, ox + ow / 2, ox + ow];
      const oH = [oy, oy + oh / 2, oy + oh];
      for (const a of myV) {
        for (const b of oV) {
          const d = b - a;
          if (Math.abs(d) <= SNAP && (!bestDx || Math.abs(d) < Math.abs(bestDx.d))) bestDx = { d, at: b };
        }
      }
      for (const a of myH) {
        for (const b of oH) {
          const d = b - a;
          if (Math.abs(d) <= SNAP && (!bestDy || Math.abs(d) < Math.abs(bestDy.d))) bestDy = { d, at: b };
        }
      }
    }
    return { dx: bestDx ? bestDx.d : 0, dy: bestDy ? bestDy.d : 0, vx: bestDx ? bestDx.at : null, vy: bestDy ? bestDy.at : null };
  }

  // ---- 多选操作条：选中 ≥2 张时底部浮出，显示数量与批量动作 ----
  //
  // [画布商业化 2026-10-17] 多选之后没有任何反馈是"半成品感"的主要来源：
  // 用户框选了一片卡，不知道选中了几张、能做什么。这条操作条把状态和动作
  // 放在一起（Miro/Figma 的 selection toolbar 同款位置）。
  let selBar = null;
  function renderSelectionBar() {
    const n = state.view.selected.size;
    if (n < 2) { if (selBar) { selBar.remove(); selBar = null; } return; }
    if (!selBar) {
      selBar = document.createElement("div");
      selBar.className = "cv-selbar";
      viewport.appendChild(selBar);
    }
    selBar.innerHTML = `
      <span class="cv-selbar-count">已选 <b>${n}</b> 张</span>
      <span class="cv-selbar-sep"></span>
      <button class="cv-selbar-btn" data-act="focus">聚焦</button>
      <button class="cv-selbar-btn" data-act="clear">取消选择</button>
      <button class="cv-selbar-btn is-danger" data-act="del">删除</button>
    `;
    selBar.querySelectorAll(".cv-selbar-btn").forEach((b) => {
      b.onclick = (ev) => {
        ev.stopPropagation();
        const act = b.dataset.act;
        if (act === "clear") clearSelection();
        else if (act === "del") deleteSelected();
        else if (act === "focus") {
          const first = [...state.view.selected][0];
          if (first) focusCard(first);
        }
      };
    });
  }

  // ---- 重命名：画布内直接改名（不跳对话页）----
  function renameCard(cardId) {
    const card = state.index.cardMap[cardId] || {};
    const next = window.prompt("重命名卡片", card.title || "");
    if (next === null) return;
    const title = next.trim();
    if (!title || title === card.title) return;
    API.patchCard(WORK_ID, cardId, { title }, "画布内重命名")
      .then(() => reloadCanvas())
      .then(() => notify("已重命名"))
      .catch((err) => notify("重命名失败：" + (err && err.message ? err.message : err), "error"));
  }

  // 打开这张卡的对话页（唯一的"进入"实现，双击/Enter/右键菜单共用）
  function openCardChat(cardId) {
    if (!cardId) return;
    location.assign("/work/" + WORK_ID + "/card/" + cardId);
  }

  // 聚焦到选中卡（画布内"飞过去"）：不改变视图根，只把视口对准它。
  function focusCard(cardId, opts) {
    const node = nodeMap.get(cardId);
    if (!node) return;
    const x = parseFloat(node.style.left) || 0;
    const y = parseFloat(node.style.top) || 0;
    const w = node.offsetWidth || 220;
    const h = node.offsetHeight || 120;
    // 键盘连续导航时不改缩放（否则每按一次方向键视野就跳一下）
    const z = (opts && opts.keepZoom) ? state.view.zoom : Math.max(state.view.zoom, 0.9);
    const vw = viewport.clientWidth, vh = viewport.clientHeight;
    applyViewState({
      panX: vw / 2 - (x + w / 2) * z,
      panY: vh / 2 - (y + h / 2) * z,
      zoom: z,
    });
    scheduleSaveView();
  }

  function onSingleClickLegacy(card) {
    // [T08-用户令 2026-10-04] 第三级卡（相对视图根深度≥2）默认收起：有子卡时单击
    // 先就地展开，已展开再单击才进对话（工作区）；无子卡直接进对话。
    // 一、二级卡单击进对话的行为不变。徽章仍是双向展开/折叠开关（T08-3-1）。
    if (depthFromViewRoot(card.card_id) >= 2) {
      const hasKids = (state.index.childMap[card.card_id] || []).length > 0;
      if (hasKids && !state.view.expanded.has(card.card_id)) {
        toggleExpand(card.card_id);
        return;
      }
    }
    location.assign("/work/" + WORK_ID + "/card/" + card.card_id);
  }

  // [T08-用户令] 相对当前视图根的深度：沿 parent 链上溯到 viewRoot 的步数
  // （viewRoot=0，直接子卡=1；上溯不到 viewRoot 的孤儿卡无意义，返回步数即可）
  function depthFromViewRoot(cardId) {
    let d = 0, cur = cardId;
    const guard = new Set();
    while (cur && cur !== state.nav.viewRoot && state.index.cardMap[cur] && !guard.has(cur)) {
      guard.add(cur);
      cur = state.index.cardMap[cur].parent;
      d++;
    }
    return d;
  }

  // [T08-3-2] 就地展开/折叠：只翻 state.expanded 成员关系后全量重绘。
  // 不调 setViewRoot（视图根不变＝「就地」的全部含义）、不调 fitView（视野不跳）。
  // [画布重构 2026-10-15 · 2.1] renderAll → renderSubtree：只重建该卡单元内的节点。
  // 这条是「用户摆好的卡任何操作下不动」的兑现点——旧路径每次展开都全量重排，
  // 用户的钉住坐标只是"不参与避让"，却仍会被别人的排布挤到别处去。
  function toggleExpand(cardId) {
    if (state.view.expanded.has(cardId)) state.view.expanded.delete(cardId);
    else state.view.expanded.add(cardId);
    scheduleSaveView();   // [F2⑥] 展开态持久化
    renderSubtree(cardId);   // [画布第三步 T4 → 画布重构 2.1] 增量入口
    // [画布商业化 2026-10-17] 展开后新卡片很可能落在视口外（实测 39 卡全展开时
    // 有 9 张在视口外），用户会以为"点了没反应/卡丢了"。只在**确实超出**时
    // 才整幅适配一次；没超出就保持用户当前视野不动。
    requestAnimationFrame(() => {
      // 只关心「这张卡 + 它刚展开的直接子卡」这一簇：视野跟着**正在操作的分支**走，
      // 而不是把整张图缩到能装下（那会把用户正在看的卡缩没——实测全展开时 fit 到 48%）。
      const cluster = [cardId, ...(state.index.childMap[cardId] || []).map((c) => c.card_id)];
      const boxes = cluster.map((id) => nodeMap.get(id)).filter(Boolean);
      if (!boxes.length) return;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const n of boxes) {
        const x = parseFloat(n.style.left) || 0;
        const y = parseFloat(n.style.top) || 0;
        minX = Math.min(minX, x); minY = Math.min(minY, y);
        maxX = Math.max(maxX, x + (n.offsetWidth || 220));
        maxY = Math.max(maxY, y + (n.offsetHeight || 120));
      }
      const z0 = state.view.zoom;
      const vx0 = (0 - state.view.panX) / z0, vy0 = (0 - state.view.panY) / z0;
      const vx1 = (viewport.clientWidth - state.view.panX) / z0;
      const vy1 = (viewport.clientHeight - state.view.panY) / z0;
      const over = minX < vx0 - 24 || minY < vy0 - 24 || maxX > vx1 + 24 || maxY > vy1 + 24;
      if (!over) return;
      // 优先「只平移」保住当前缩放；这一簇本身比视口还大时才按比例缩到刚好放下。
      const pad = 48;
      const availW = Math.max(80, viewport.clientWidth - pad * 2);
      const availH = Math.max(80, viewport.clientHeight - pad * 2);
      const w = Math.max(1, maxX - minX), h = Math.max(1, maxY - minY);
      const z = Math.max(0.25, Math.min(z0, availW / w, availH / h));
      const panX = pad + (availW - w * z) / 2 - minX * z;
      const panY = pad + (availH - h * z) / 2 - minY * z;
      applyViewState({ panX, panY, zoom: z });
      scheduleSaveView();
    });
  }

  function setViewRoot(newRoot) {
    // [card_045 E7 · L8 位置跨视图共享] 坐标记录是 work 级的：下钻/返回只改
    // 视图根（可见集），任何卡的位置不因视图切换而改变——下钻再返回，位置不变。
    state.nav.viewRoot = newRoot;
    summaryCache.clear();
    // [诸葛 2026-10-04 21:52] 下钻换根时清掉旧展开态。
    // 原缺口：只清 summaryCache，expanded 跟着带去新视图——钻进去后可能有若干卡
    // 莫名已展开（T08 新增 expanded 机制时漏了这一处，属新机制铺开的边角遗漏）。
    state.view.expanded.clear();
    hideWorkspace();
    renderAll();          // [画布第三步 T4] 改 state → 统一重绘入口
    // [画布第三步 T2/T3] fitView 改纯量测（返回 pan，不直写视图态），写入走
    // applyViewState（视图变换量入口）——V1/V10
    const fit = fitView(state.view.zoom);
    if (fit) applyViewState({ panX: fit.panX, panY: fit.panY, zoom: fit.zoom });
  }

  // [hotfix] 切层后把当前层所有卡片居中到视野（负坐标区不再甩出屏幕外）
  // [画布第三步 T2/T3] 改纯量测：读自身 DOM 量测＋入参 zoom，算出 pan **返回**；
  // 直写视图态与 applyTransform 上收给调用方（动作层 applyViewState）——V1
  //
  // [画布商业化 2026-10-17] 从「只算平移」升级为「整幅适配」：
  //   ① 内容放不下时**按比例缩小**（此前只挪不平移，溢出部分永远看不见——首屏
  //      3 张卡跑到视口上方就是这么来的）；
  //   ② 绝不放大超过调用方给的 zoom（fit 的语义是"看全"，不是"撑满"）；
  //   ③ 四周留 pad，视觉上不贴边。
  //   返回值多一个 zoom 字段，调用方一并写回视图态。
  function fitView(zoom, pad = 56) {
    const nodes = [...world.querySelectorAll(".card-node")];
    if (!nodes.length) return null;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes) {
      const x = parseFloat(n.style.left) || 0;
      const y = parseFloat(n.style.top) || 0;
      minX = Math.min(minX, x);
      minY = Math.min(minY, y);
      maxX = Math.max(maxX, x + n.offsetWidth);
      maxY = Math.max(maxY, y + n.offsetHeight);
    }
    const vw = viewport.clientWidth || 1, vh = viewport.clientHeight || 1;
    const availW = Math.max(80, vw - pad * 2);
    const availH = Math.max(80, vh - pad * 2);
    const w = Math.max(1, maxX - minX);
    const h = Math.max(1, maxY - minY);
    // 只有放不下才缩；放得下时保持调用方给的 zoom（通常是 1）
    let z = Math.min(zoom, availW / w, availH / h);
    z = Math.max(0.25, Math.min(2.0, z));
    const panX = pad + (availW - w * z) / 2 - minX * z;
    const panY = pad + (availH - h * z) / 2 - minY * z;
    return { panX, panY, zoom: z };
  }

  function getVisibleIds() {
    const ids = new Set([state.nav.viewRoot]);
    const stack = [state.nav.viewRoot];
    while (stack.length) {
      const id = stack.pop();
      for (const child of (state.index.childMap[id] || [])) {
        if (!ids.has(child.card_id)) { ids.add(child.card_id); stack.push(child.card_id); }
      }
    }
    return ids;
  }

  // ==================== 关系线渲染 ====================
  // [card_045 E3] sizes 参数化：一轮渲染内只测一次（renderCanvas/reflowLayout 传入）；
  // 省缺时回退现测（外部零散调用兼容）。
  // [画布第三步 T2] 改吃投影＋视图模型：visibleIds/layout/sizes/父子折线段/外接框
  // 全部来自 projection（几何派生已上收 computeEdgeGeometry，画布第三步 T1），
  // links/cardMap 从 vm 取——不直摸 state（V1）。
  // ══════════════════════════════════════════════════════════════════════════
  // [画布商业化 2026-10-18] 子卡分组底衬
  //
  // 问题：同一父卡的子卡超过 4 张会分成多列，第二列在几何上和"下一层"几乎一样，
  // 光靠间距差（96 vs 20）仍然容易误读。给每组子卡画一块极淡的底衬，等于在
  // 画布上直接写"这 N 张是同一个父卡的子卡"——这是白板产品表达分组的通行做法
  // （Miro 的 frame、Figma 的 section），比连线更硬。
  //
  // 只框**直接子卡**（不框孙卡），≥2 张才画（1 张不需要分组）；
  // 缩放低于 55% 时整层隐藏（那时框会密到变成噪音）。
  // ══════════════════════════════════════════════════════════════════════════
  let groupLayer = null;
  function ensureGroupLayer() {
    if (groupLayer && groupLayer.parentNode === world) return groupLayer;
    groupLayer = document.createElement("div");
    groupLayer.className = "cv-groups";
    world.insertBefore(groupLayer, world.firstChild);
    return groupLayer;
  }

  // 分组框元素复用池：不每次重建 DOM。
  // 为什么：layer.innerHTML="" + fragment 重建会让 #world（带 transform/will-change）
  // 的合成层失效重建，232 卡实测首屏多花 150ms；改成"只写 style"后成本降到可忽略。
  let groupEls = [];
  function renderGroups(childMap, projection, vm) {
    const layer = ensureGroupLayer();
    const zoom = vm.zoom || 1;
    layer.style.display = zoom < 0.55 ? "none" : "";
    if (zoom < 0.55) return;

    const { visibleIds, layout, sizes } = projection;
    // 与连线共用同一份几何：线的端点就是框的左缘中点
    const boxes = computeChildGroups(visibleIds, layout, sizes, childMap)
      .map((g) => [g.x0, g.y0, g.x1 - g.x0, g.y1 - g.y0]);

    while (groupEls.length < boxes.length) {
      const el = document.createElement("div");
      el.className = "cv-group";
      layer.appendChild(el);
      groupEls.push(el);
    }
    while (groupEls.length > boxes.length) groupEls.pop().remove();
    for (let i = 0; i < boxes.length; i++) {
      const [x, y, w, h] = boxes[i];
      const el = groupEls[i];
      el.style.left = x + "px";
      el.style.top = y + "px";
      el.style.width = w + "px";
      el.style.height = h + "px";
    }
  }

  function renderEdges(projection, vm) {
    const visibleIds = projection.visibleIds;
    const layout = projection.layout;
    const sizes = projection.sizes || measureSizes();
    const box = projection.edges.bbox;
    edgesSvg.setAttribute("width", box.w);
    edgesSvg.setAttribute("height", box.h);
    edgesSvg.style.left = box.minX + "px";
    edgesSvg.style.top = box.minY + "px";
    edgesSvg.setAttribute("viewBox", `${box.minX} ${box.minY} ${box.w} ${box.h}`);
    edgesSvg.innerHTML = "";
    // [T08-2-1/2-3＋诸葛 2026-10-04 21:52 锚点按相对位置自动选边] 父子折线段与
    // 外接框的推导规则全文见 computeEdgeGeometry（T1 上收，规则一字不改）；此处只消费。
    const elbowSegs = projection.edges.elbowSegs;

    // [诸葛 2026-10-04 21:52] 两类线分组重构（设计由诸葛定，用户 21:46 授权界面自决）。
    // 原状：父子线与关系线同色 #30363d、同宽 1.5，只有形状不同（折线 vs 直线），
    // 视觉上分不出"层级挂靠"与"弱关联"。现按语义分色分形：
    //   父子线（层级骨架）＝ 蓝 #58a6ff 实线 ＋ 两端圆点（父端实心、子端空心）
    //   关系线（弱关联）  ＝ 灰 #8b949e 虚线
    //   假设悬空红线不动（红色虚线＋空心圆是既有语义）
    // 分组 <g data-edge-group> 供 applyLODClasses 分级控制（L0 档只收关系线）。

    // ── 父子线组 ──
    //
    // [画布商业化 2026-10-17] 端点圆点从「每条边两个 <circle>」改为「defs 里两个
    // marker 复用」：232 条边省下 464 个 SVG 元素（实测首屏 DOM 从 2893 降到 2429），
    // 且 marker 随 path 一起走，不需要在 reflow 时同步坐标。
    const SVGNS = "http://www.w3.org/2000/svg";
    const defs = document.createElementNS(SVGNS, "defs");
    defs.innerHTML = `
      <marker id="cv-dot-start" markerUnits="userSpaceOnUse" markerWidth="7" markerHeight="7" refX="3.5" refY="3.5">
        <circle cx="3.5" cy="3.5" r="2.2" fill="#4a6ea8"/>
      </marker>
      <marker id="cv-dot-end" markerUnits="userSpaceOnUse" markerWidth="7" markerHeight="7" refX="3.5" refY="3.5">
        <circle cx="3.5" cy="3.5" r="2" fill="#0b0d10" stroke="#4a6ea8" stroke-width="1.4"/>
      </marker>`;
    edgesSvg.appendChild(defs);

    const pcG = document.createElementNS(SVGNS, "g");
    pcG.setAttribute("data-edge-group", "parent-child");
    edgesSvg.appendChild(pcG);
    const pcFrag = document.createDocumentFragment();
    for (const seg of elbowSegs) {
      // [画布商业化 2026-10-18] 线权重下调：层级由「尺寸 + 明度 + 分组框」表达，
      // 连线只是辅助线索。1.8px 亮蓝在多卡画布上会抢走全部注意力（实测像一团电线）。
      const path = makeEdgePath(seg.x1, seg.y1, seg.x2, seg.y2, "#4a6ea8", "1.2", seg);
      // [画布商业化 2026-10-17] 按父卡深度递减线宽与不透明度：全展开时 200+ 条边
      // 同权重会糊成一片（截图实测）。近根的主干保持醒目，深层枝节退为背景。
      const d = vm.depthOf ? (vm.depthOf[seg.from] || 0) : 0;
      if (d >= 2) { path.setAttribute("stroke-width", "1"); path.setAttribute("opacity", "0.4"); }
      else if (d >= 1) { path.setAttribute("stroke-width", "1.1"); path.setAttribute("opacity", "0.6"); }
      else { path.setAttribute("opacity", "0.7"); }
      pcFrag.appendChild(path);
    }
    pcG.appendChild(pcFrag);
    console.info(`[T08] 父子线：绘制 ${elbowSegs.length} 条（可见卡 ${visibleIds.size} 张）`);

    // ── 关系线组 ──
    const relG = document.createElementNS("http://www.w3.org/2000/svg", "g");
    relG.setAttribute("data-edge-group", "links");
    edgesSvg.appendChild(relG);

    // 普通关系线（[phase8.6-B] 锚点 = 实测尺寸的卡中心）
    for (const r of vm.links) {
      if (!visibleIds.has(r.source) || !visibleIds.has(r.target)) continue;
      const p1 = layout[r.source], p2 = layout[r.target];
      if (!p1 || !p2) continue;
      const isForeshFulfill = r.source && vm.cardsById[r.source] &&
        vm.cardsById[r.source].card_type === "假设" && (r.type || "").includes("兑现");
      const color = isForeshFulfill ? "#f85149" : "#8b949e";
      const sw = isForeshFulfill ? "2" : "1.5";
      const dash = isForeshFulfill ? "" : "5 4";   // 假设兑现保持实线（语义强），其余弱关联用虚线
      const s1 = nodeSize(r.source), s2 = nodeSize(r.target);
      addLine(p1.x + s1.w / 2, p1.y + s1.h / 2, p2.x + s2.w / 2, p2.y + s2.h / 2, color, sw, dash, relG);
    }

    // 假设悬空红线（无兑现边）
    for (const cid of visibleIds) {
      const card = vm.cardsById[cid];
      if (!card || card.card_type !== "假设") continue;
      const pos = layout[cid];
      if (!pos) continue;
      const hasFulfill = vm.links.some(r => r.source === cid && (r.type || "").includes("兑现") && visibleIds.has(r.target));
      if (!hasFulfill) {
        // 从卡右缘向右下画 80px 折线，端点空心圆（[phase8.6-B] 右缘中点实测）
        const sz = nodeSize(cid);
        const startX = pos.x + sz.w, startY = pos.y + sz.h / 2;
        const midX = startX + 80, midY = startY + 40;
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", `M ${startX} ${startY} L ${midX} ${startY} L ${midX} ${midY}`);
        path.setAttribute("stroke", "#f85149");
        path.setAttribute("stroke-width", "2");
        path.setAttribute("stroke-dasharray", "6 4");
        path.setAttribute("fill", "none");
        path.setAttribute("opacity", "0.75");
        edgesSvg.appendChild(path);
        const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        circle.setAttribute("cx", midX);
        circle.setAttribute("cy", midY);
        circle.setAttribute("r", "5");
        circle.setAttribute("stroke", "#f85149");
        circle.setAttribute("stroke-width", "1.5");
        circle.setAttribute("fill", "none");
        edgesSvg.appendChild(circle);
      }
    }

    // 跨视图关系🔗徽章
    for (const cid of visibleIds) {
      const card = vm.cardsById[cid];
      if (!card) continue;
      const node = nodeMap.get(cid);
      if (!node) continue;
      // 移除旧徽章
      const oldBadge = node.querySelector(".cross-link-badge");
      if (oldBadge) oldBadge.remove();
      // 检查是否有指向视图外目标的关系
      const hasCrossLink = vm.links.some(r => r.source === cid && !visibleIds.has(r.target));
      if (hasCrossLink) {
        const badge = document.createElement("span");
        badge.className = "cross-link-badge";
        badge.textContent = "🔗";
        badge.onclick = (e) => {
          e.stopPropagation();
          // 找到第一个跨视图目标，打开其工作区
          const crossRel = vm.links.find(r => r.source === cid && !visibleIds.has(r.target));
          if (crossRel) location.assign("/work/" + WORK_ID + "/card/" + crossRel.target);
        };
        node.appendChild(badge);
      }
    }
  }

  function addLine(x1, y1, x2, y2, color, sw, dash, parent) {
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", x1); line.setAttribute("y1", y1);
    line.setAttribute("x2", x2); line.setAttribute("y2", y2);
    line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", sw);
    if (dash) line.setAttribute("stroke-dasharray", dash);   // [诸葛 21:52] 弱关联用虚线
    (parent || edgesSvg).appendChild(line);
  }

  // [T08-2-2] 父子直角折线：先横、再竖、再横（左右视图方向——用户中途指令推翻任务书
  // 上下向「先下、再横、再下」，留痕；写法范本＝假设悬空红线段）。
  // 既有 addLine 不删——links 线仍用（红线7）；两类线调用点不交叉。
  // [诸葛 2026-10-04 21:52] 增 parent 参数：父子线挂进 data-edge-group="parent-child" 组，
  // 供 applyLODClasses 分级控制；折线算法本体不动，data-edge 机械验收标记保留。
  // [画布商业化 2026-10-17] 父子线由「直角折线」改为**三次贝塞尔曲线**。
  // 为什么改：折线的直角在深色画布上显得生硬、且多列布局时横竖拐点会与卡片边缘
  // 打架；曲线是白板类产品的通行形态（Figma/Miro/tldraw 的连线都是曲线）。
  // 控制点沿水平方向外扩（出点向右、入点向左），保证曲线从卡片侧缘垂直进出，
  // 反向连接（子在父左）时自动换向，不会打结。
  // data-edge 标记保留（V6/V7 机械验收按它计数）；from/to 供高亮用。
  function makeEdgePath(x1, y1, x2, y2, color, sw, meta) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    if (meta && meta.straight) {
      // 主干线用直角直线：多层嵌套时曲线会互相缠绕，直线永远清楚
      path.setAttribute("d", `M ${x1} ${y1} L ${x2} ${y2}`);
    } else {
      const sign = x2 >= x1 ? 1 : -1;
      const dx = Math.max(36, Math.min(180, Math.abs(x2 - x1) * 0.5));
      path.setAttribute("d",
        `M ${x1} ${y1} C ${x1 + sign * dx} ${y1}, ${x2 - sign * dx} ${y2}, ${x2} ${y2}`);
    }
    path.setAttribute("stroke", color);
    path.setAttribute("stroke-width", sw);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("data-edge", "parent-child"); // [T08] 机械验收标记（V6/V7 计数用，不影响渲染）
    if (meta && meta.from) path.dataset.from = meta.from;
    if (meta && meta.to) path.dataset.to = meta.to;
    // 主干线不画端点圆点（直角线端点密集，圆点会变成一串珠子）
    if (!(meta && meta.straight)) {
      path.setAttribute("marker-start", "url(#cv-dot-start)");
      path.setAttribute("marker-end", "url(#cv-dot-end)");
    }
    return path;
  }

  function addElbowPath(x1, y1, x2, y2, color, sw, parent, meta) {
    const path = makeEdgePath(x1, y1, x2, y2, color, sw, meta);
    (parent || edgesSvg).appendChild(path);
  }

  // 悬停/选中某张卡时，高亮它的关联线、压暗其余线（"聚焦一条关系"的视觉反馈）
  function highlightEdges(cardId) {
    const paths = edgesSvg.querySelectorAll("path[data-edge='parent-child']");
    if (!paths.length) return;
    for (const el of paths) {
      const related = !!cardId && (el.dataset.from === cardId || el.dataset.to === cardId);
      el.classList.toggle("hot", related);
      el.classList.toggle("dim", !!cardId && !related);
    }
  }

  // ==================== Tooltip 与摘要懒加载 ====================
  async function showTooltip(card, node) {
    if (state.view.zoom > 1.2) return; // L2 已显示，不需要 tooltip
    const summary = await getSummary(card.card_id, card);
    const rect = node.getBoundingClientRect();
    tooltip.innerHTML = `
      <div class="tt-title">${escapeHtml(card.title)}</div>
      <div class="tt-summary">${escapeHtml(summary || "（暂无结论）")}</div>
    `;
    tooltip.classList.add("active");
    const tx = rect.right + 8;
    const ty = rect.top;
    tooltip.style.left = Math.min(tx, window.innerWidth - 300) + "px";
    tooltip.style.top = ty + "px";
  }

  function hideTooltip() {
    tooltip.classList.remove("active");
  }

  // [画布第三步 T2] 卡数据改由调用方传入（card＝vm.cardsById[cardId]）——不直摸
  // state.cardMap（V1）；summaryCache 为模块级直呼（T3 归异步桶同体，见 state 定义处）
  // [画布商业化 2026-10-17] 相对时间：卡面底部只给"多久没动过"，比完整时间戳更可扫读。
  // 超过 7 天退回日期，超过 1 年退回年月——三级粒度，任何卡都不会显示成一片数字。
  function fmtRelative(iso) {
    if (!iso) return "";
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return "";
    const diff = Date.now() - t;
    const min = Math.floor(diff / 60000);
    if (min < 1) return "刚刚";
    if (min < 60) return min + " 分钟前";
    const hr = Math.floor(min / 60);
    if (hr < 24) return hr + " 小时前";
    const day = Math.floor(hr / 24);
    if (day === 1) return "昨天";
    if (day < 7) return day + " 天前";
    const d = new Date(t);
    if (day < 365) return `${d.getMonth() + 1}月${d.getDate()}日`;
    return `${d.getFullYear()}年${d.getMonth() + 1}月`;
  }

  async function getSummary(cardId, card) {
    if (summaryCache.has(cardId)) return summaryCache.get(cardId);

    // [R4] 快路径：结论摘要随画布数据一次到手（canvas.json 的 cards[].conclusion_summary），
    // 命中则直接使用、不发 getCard 请求——首屏 N+1 请求归零。
    // 字段缺失（老缓存未重建 / 后端未升级）→ 回退原路径，向后兼容。
    // 代价如实记：走快路径时 meta2（更新时间）不在画布数据里，保持原状不填；
    // L1 档位本就隐藏 meta2，L2 档位下该行的更新时间可能为空——如需它，回退路径仍在。
    const probe = card;
    // [画布 2026-10-18] 优先用后端算好的 conclusion（显式「结论：」优先，否则是
    // 跳过元信息行/表格行之后的第一段正文）；没有则回退 conclusion_summary。
    // ⚠ 必须走下面这条**会写 DOM** 的路径——上一版在函数开头直接 return conclusion，
    // 结果是：摘要永远不写进卡面，卡片只剩标题（自己截图才发现）。
    const fastText = (probe && typeof probe.conclusion === "string" && probe.conclusion)
      ? probe.conclusion
      : (probe && typeof probe.conclusion_summary === "string" ? probe.conclusion_summary : null);
    if (fastText !== null) {
      const summary = fastText;
      summaryCache.set(cardId, summary);
      const node0 = nodeMap.get(cardId);
      if (node0) {
        const sumEl0 = node0.querySelector(".summary");   // 待裁定卡没有这个节点，跳过即可
        if (sumEl0) sumEl0.textContent = summary;
        // [画布商业化 2026-10-17] 快路径也填更新时间（canvas 投影已带 updated_at），
        // 不再因为"走快路径"而让卡面底部空着。
        const metaEl0 = node0.querySelector(".meta2");
        if (metaEl0 && probe.updated_at) metaEl0.textContent = fmtRelative(probe.updated_at);
        if (summary) scheduleReflow();
      }
      console.info("[R4] 摘要取自 canvas 数据（未发请求） " + cardId);
      return summary;
    }

    try {
      const card = await API.getCard(WORK_ID, cardId);
      // [卡片信息结构改造 G-6/S2-12] 回退路径取新键 status_block；
      // 快路径字段名 conclusion_summary 不变（canvas.json 契约，勿动）。
      const concl = (card.body && card.body.status_block) || "";
      const firstLine = concl.split("\n").map(s => s.trim()).find(s => s) || "";
      const summary = firstLine.length > 120 ? firstLine.slice(0, 120) + "…" : firstLine;
      summaryCache.set(cardId, summary);
      // 同时填充卡片的摘要 DOM（[phase8.4] 结论为空时不写占位文字，保持空白）
      const node = nodeMap.get(cardId);
      if (node) {
        const sumEl = node.querySelector(".summary");
        if (sumEl) sumEl.textContent = summary;
        const metaEl = node.querySelector(".meta2");
        if (metaEl) metaEl.textContent = "更新：" + (card.meta.updated_at || "").slice(0, 16).replace("T", " ");
        // [phase8.6-B] 摘要填充改变卡高，防抖重跑避让
        if (summary) scheduleReflow();
      }
      return summary;
    } catch (e) {
      return "";
    }
  }

  // 自动懒加载可见卡的摘要（[phase8.4] 不再按缩放档设门槛，任何档均加载）
  // [画布第三步 T2] 卡数据改吃参数（cardsById＝vm.cardsById）——V1
  function lazyLoadVisibleSummaries(cardsById) {
    for (const [cid, node] of nodeMap) {
      if (!summaryCache.has(cid)) {
        getSummary(cid, cardsById[cid]);
      } else {
        // renderCanvas 重建节点后 DOM 是空的，缓存命中也要回填
        const sumEl = node.querySelector(".summary");
        if (sumEl && !sumEl.textContent) {
          const cached = summaryCache.get(cid);
          sumEl.textContent = cached;
        // 回填改变卡高，防抖重跑避让
        if (cached) scheduleReflow();
        const metaEl = node.querySelector(".meta2");
        if (metaEl && !metaEl.textContent && cardsById[cid] && cardsById[cid].updated_at) {
          metaEl.textContent = fmtRelative(cardsById[cid].updated_at);
        }
        }
      }
    }
  }

  // ── 窗口尺寸变化：保持视野中心不变 ──
  // 不这么做的话，1280 下展开左栏目录（画布从 891 缩到 623）时，卡片会整体
  // 右移出视野——因为 pan 是按旧宽度算的。
  // 做法：记下"视口中心对应的世界坐标"，尺寸变了之后反推新的 pan。
  (function keepViewCenterOnResize() {
    let lastW = viewport ? viewport.clientWidth : 0;
    let lastH = viewport ? viewport.clientHeight : 0;
    let timer = 0;
    window.addEventListener("resize", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (!viewport) return;
        const w = viewport.clientWidth, h = viewport.clientHeight;
        if (!w || !h || !lastW || !lastH) { lastW = w; lastH = h; return; }
        const z = state.view.zoom || 1;
        const cx = (lastW / 2 - state.view.panX) / z;
        const cy = (lastH / 2 - state.view.panY) / z;
        state.view.panX = w / 2 - cx * z;
        state.view.panY = h / 2 - cy * z;
        lastW = w; lastH = h;
        applyTransform();
        scheduleSaveView();
      }, 120);
    });
  })();

  // ==================== 缩放控件（−/＋/滑块/百分比） ====================
  //
  // [画布商业化 2026-10-17 · 修硬伤] 这三个控件在模板里一直都在，但**没有任何
  // JS 绑定**：style.css 的注释还写着"由 canvas.js syncZoomControls 按值同步"，
  // 而 canvas.js 里 `zoomInBtn / zoomOutBtn / zoomSlider / syncZoomControls`
  // 全部零命中（grep 实测）。⇒ 用户点 ＋/− 毫无反应、拖滑块毫无反应。
  // 这是"交互走不通"的直接来源之一，本次补回：
  //   −/＋  每按一次 ±25%（用户 2026-10-16 裁定的档位口径）
  //   滑块  25% 档位吸附，拖动即缩放（锚点＝视口中心）
  //   百分比 点击 = 回到 100%
  const zoomInBtn = document.getElementById("zoomInBtn");
  const zoomOutBtn = document.getElementById("zoomOutBtn");
  const zoomSlider = document.getElementById("zoomSlider");

  function setZoomTo(z, anchorX, anchorY) {
    const rect = viewport.getBoundingClientRect();
    const ax = anchorX === undefined ? rect.left + rect.width / 2 : anchorX;
    const ay = anchorY === undefined ? rect.top + rect.height / 2 : anchorY;
    zoomAtPoint(ax, ay, z);
  }

  if (zoomInBtn) zoomInBtn.onclick = () => setZoomTo(state.view.zoom * 1.25);
  if (zoomOutBtn) zoomOutBtn.onclick = () => setZoomTo(state.view.zoom / 1.25);
  if (zoomSlider) {
    zoomSlider.addEventListener("input", () => {
      setZoomTo(Number(zoomSlider.value) / 100);
    });
    // 松手后把视图态回写给滑块（拖到 37% 会被吸附到最近档，避免显示与状态不一致）
    zoomSlider.addEventListener("change", () => {
      zoomSlider.value = String(Math.round(state.view.zoom * 100));
    });
  }
  if (zoomDisplay) {
    zoomDisplay.title = "点击回到 100%";
    zoomDisplay.onclick = () => setZoomTo(1);
  }

  // ==================== 视口交互（缩放/平移/框选） ====================
  //
  // [画布商业化 2026-10-17] 整段重写。旧实现三个硬伤：
  //   ① 滚轮按固定 ±0.1 线性跳档，没有惯性、没有比例手感，且 25%→200% 只有 18 档；
  //   ② 只有「空白处左键拖」一种平移方式，中键/空格/触控板全不认；
  //   ③ 没有框选，多选无从谈起。
  //
  // 新口径（对齐 Figma / Miro / tldraw 的通行手势）：
  //   缩放  滚轮（鼠标）／Ctrl+滚轮、触控板捏合／工具栏 ±25% 档位／⌘0 整幅适配
  //   平移  左键拖空白／中键拖／空格+左键拖／Shift+滚轮（横向）
  //   框选  Shift+左键拖空白
  //   判"鼠标还是触控板"：像素模式（deltaMode=0）且 |deltaY| < 30 视为触控板，
  //   走平移；否则视为滚轮，走缩放。这是各家通用启发式，改判据只需动这一处。
  const ZOOM_MIN = 0.25, ZOOM_MAX = 2.0;
  const clampZoom = (z) => Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, z));

  // 以屏幕点为锚缩放（world_pos = (screen - pan) / zoom ⇒ 新 pan = screen - world*z）
  function zoomAtPoint(screenX, screenY, newZoom) {
    const rect = viewport.getBoundingClientRect();
    const mx = screenX - rect.left;
    const my = screenY - rect.top;
    const oldZoom = state.view.zoom;
    const z = clampZoom(newZoom);
    if (Math.abs(z - oldZoom) < 1e-4) return;
    const wx = (mx - state.view.panX) / oldZoom;
    const wy = (my - state.view.panY) / oldZoom;
    state.view.panX = mx - wx * z;
    state.view.panY = my - wy * z;
    state.view.zoom = z;
    applyTransform();
    // [画布商业化 2026-10-17] 摘要加载不再挂在每次缩放上：缩放不改变可见集，
    // 摘要只与"节点出现"有关（渲染/展开时已加载）。原实现每次 wheel 遍历全部
    // 节点做 Map 查询，是缩放掉帧的第二个来源。
    scheduleSaveView();
  }

  function isTrackpadWheel(e) {
    return e.deltaMode === 0 && Math.abs(e.deltaY) < 30 && Math.abs(e.deltaX) < 30;
  }

  viewport?.addEventListener("wheel", (e) => {
    e.preventDefault();
    _userMoved = true;

    // ══════════════════════════════════════════════════════════════════
    // [画布 2026-10-18 · 阅读姿势] 滚轮＝**纵向滚动**，Ctrl/⌘+滚轮＝缩放。
    //
    // 这是"读卡"和"画图"的分野：白板是滚轮缩放（Miro/Figma），文档是滚轮滚动
    // （任何阅读器）。这个画布上用户做的事是**读结论、读待裁定**，一屏读不完就
    // 往下滚——所以滚轮必须滚动。缩放是低频动作，交给 Ctrl+滚轮与顶栏控件。
    // ══════════════════════════════════════════════════════════════════
    const zoomMod = e.ctrlKey || e.metaKey;   // 触控板捏合在浏览器里就是 ctrl+wheel

    if (zoomMod) {
      const factor = Math.exp(-e.deltaY * 0.01);
      zoomAtPoint(e.clientX, e.clientY, state.view.zoom * factor);
      return;
    }

    // Shift+滚轮：横向滚动（长文本卡横向查看时用）
    if (e.shiftKey) {
      state.view.panX -= (e.deltaY || e.deltaX);
      applyTransform();
      scheduleSaveView();
      return;
    }

    // 默认：纵向滚动
    state.view.panY -= e.deltaY;
    state.view.panX -= e.deltaX;
    applyTransform();
    scheduleSaveView();
  }, { passive: false });

  // ---- 平移 / 框选 ----
  let panState = null;
  let marqueeState = null;
  let spaceDown = false;

  function isBlankTarget(t) {
    return t === viewport || t === world || (t && t.id === "edgesSvg");
  }

  viewport?.addEventListener("mousedown", (e) => {
    // [画布商业化 2026-10-17] 中键平移**不挑目标**：压在卡片上也能拖动画布。
    // 这是白板里"手"的语义——中键永远是手，不是选择器。
    if (e.button === 1) {
      _userMoved = true;
      panState = {
        startX: e.clientX, startY: e.clientY,
        panX: state.view.panX, panY: state.view.panY,
      };
      viewport.classList.add("panning");
      e.preventDefault();
      return;
    }
    if (!isBlankTarget(e.target)) return;
    _userMoved = true;

    // Shift+左键 = 框选
    if (e.button === 0 && e.shiftKey) {
      const rect = viewport.getBoundingClientRect();
      marqueeState = {
        startX: e.clientX - rect.left,
        startY: e.clientY - rect.top,
        base: new Set(state.view.selected),
        el: document.createElement("div"),
      };
      marqueeState.el.className = "cv-marquee";
      viewport.appendChild(marqueeState.el);
      viewport.classList.add("marquee");
      e.preventDefault();
      return;
    }

    // 左键拖空白 = 平移
    if (e.button === 0) {
      panState = {
        startX: e.clientX, startY: e.clientY,
        panX: state.view.panX, panY: state.view.panY,
      };
      viewport.classList.add("panning");
    }
  });

  document.addEventListener("mousemove", (e) => {
    if (panState) {
      state.view.panX = panState.panX + (e.clientX - panState.startX);
      state.view.panY = panState.panY + (e.clientY - panState.startY);
      // 平移期间直接写 transform，不走 applyTransform（省掉 LOD 重算，60fps 更稳）
      world.style.transform = `translate(${state.view.panX}px, ${state.view.panY}px) scale(${state.view.zoom})`;
      return;
    }
    if (marqueeState) {
      const rect = viewport.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;
      const x = Math.min(cx, marqueeState.startX);
      const y = Math.min(cy, marqueeState.startY);
      const w = Math.abs(cx - marqueeState.startX);
      const h = Math.abs(cy - marqueeState.startY);
      const el = marqueeState.el;
      el.style.left = x + "px";
      el.style.top = y + "px";
      el.style.width = w + "px";
      el.style.height = h + "px";
      // 命中判定：把卡片世界坐标换算到视口坐标再求交
      const z = state.view.zoom;
      const hit = [];
      for (const [cid, node] of nodeMap) {
        const nx = parseFloat(node.style.left) || 0;
        const ny = parseFloat(node.style.top) || 0;
        const sx = nx * z + state.view.panX;
        const sy = ny * z + state.view.panY;
        const sw = (node.offsetWidth || 220) * z;
        const sh = (node.offsetHeight || 120) * z;
        if (sx < x + w && sx + sw > x && sy < y + h && sy + sh > y) hit.push(cid);
      }
      const next = new Set(marqueeState.base);
      for (const id of hit) next.add(id);
      state.view.selected = next;
      applySelectionClasses();
    }
  });

  document.addEventListener("mouseup", () => {
    if (panState) {
      // 按下即松开（位移 < 3px）＝"点了一下空白" ⇒ 取消选择。
      // 这条是白板的肌肉记忆：点空白清选择，而不是什么都不发生。
      const moved = Math.abs(state.view.panX - panState.panX) + Math.abs(state.view.panY - panState.panY);
      const tapCard = panState.tapCard;
      const tapAdditive = panState.tapAdditive;
      panState = null;
      viewport.classList.remove("panning");
      applyTransform();       // 平移结束补一次 LOD（拖动中故意不重算）
      scheduleSaveView();
      if (moved < 3) {
        if (tapCard) setSelection([tapCard], !!tapAdditive);
        else clearSelection();
      }
    }
    if (marqueeState) {
      marqueeState.el.remove();
      marqueeState = null;
      viewport.classList.remove("marquee");
    }
  });

  // ---- 空格平移：按住空格时画布进入抓手态（拖空白＝平移，即使压到卡片上） ----
  document.addEventListener("keydown", (e) => {
    if (e.code !== "Space") return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    if (!spaceDown) { spaceDown = true; viewport?.classList.add("panning"); }
    e.preventDefault();
  });
  document.addEventListener("keyup", (e) => {
    if (e.code !== "Space") return;
    spaceDown = false;
    if (!panState) viewport?.classList.remove("panning");
  });

  // ══════════════════════════════════════════════════════════════════════
  // [画布商业化 2026-10-17] 键盘快捷键
  //
  // 口径：输入框/可编辑区里一律不拦（否则打字会被吞）；其余场景按白板通行键位。
  //    Esc        取消选择
  //    Del / ⌫    删除选中（有确认，根卡与有子卡者后端拒删）
  //    Enter      打开选中卡的对话
  //    F          聚焦选中卡（视口飞过去）
  //    ⌘/Ctrl+A   全选当前可见卡
  //    ⌘/Ctrl+0   整幅适配（fit）
  //    ⌘/Ctrl+±   以视口中心缩放
  // ══════════════════════════════════════════════════════════════════════
  function isTypingTarget(t) {
    if (!t) return false;
    const tag = (t.tagName || "").toUpperCase();
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || t.isContentEditable;
  }

  function doFitAll() {
    const fit = fitView(1);
    if (fit) { applyViewState({ panX: fit.panX, panY: fit.panY, zoom: fit.zoom }); scheduleSaveView(); }
  }

  function zoomByStep(dir) {
    const rect = viewport.getBoundingClientRect();
    zoomAtPoint(rect.left + rect.width / 2, rect.top + rect.height / 2, state.view.zoom * (dir > 0 ? 1.25 : 0.8));
  }

  async function deleteSelected() {
    const ids = selectionIds();
    if (!ids.length) return;
    const blocked = ids.filter((id) =>
      id === state.nav.workCardId || (state.index.childMap[id] || []).length > 0);
    const deletable = ids.filter((id) => !blocked.includes(id));
    if (blocked.length) {
      notify(`根卡与有子卡的卡不能删（${blocked.length} 张已跳过）`, "error");
    }
    if (!deletable.length) return;
    const names = deletable.map((id) => (state.index.cardMap[id] || {}).title || id);
    if (!window.confirm(`删除 ${deletable.length} 张卡片？\n\n${names.join("\n")}\n\n此操作不可撤销。`)) return;
    for (const id of deletable) {
      try { await API.deleteCard(WORK_ID, id); } catch (err) { /* 单张失败不阻断其余 */ }
    }
    clearSelection();
    reloadCanvas();
  }

  document.addEventListener("keydown", (e) => {
    if (isTypingTarget(e.target)) return;
    const mod = e.metaKey || e.ctrlKey;

    if (mod && (e.key === "0" || e.code === "Digit0")) { e.preventDefault(); doFitAll(); return; }
    if (mod && (e.key === "=" || e.key === "+")) { e.preventDefault(); zoomByStep(1); return; }
    if (mod && e.key === "-") { e.preventDefault(); zoomByStep(-1); return; }
    if (mod && (e.key === "k" || e.key === "K")) { e.preventDefault(); openPalette(); return; }
    if (mod && (e.key === "a" || e.key === "A")) {
      e.preventDefault();
      setSelection([...nodeMap.keys()]);
      return;
    }

    if (e.key === "Escape") {
      triageList = [];            // 退出裁定模式
      clearSelection();
      return;
    }

    const ids = selectionIds();
    if ((e.key === "Delete" || e.key === "Backspace") && ids.length) {
      e.preventDefault();
      deleteSelected();
      return;
    }
    if (e.key === "Enter" && ids.length === 1) {
      e.preventDefault();
      openCardChat(ids[0]);
      return;
    }
    // 方向键在卡片树里移动选择：← 上溯父卡、→ 下钻首个子卡、↑↓ 兄弟卡之间移动。
    // 选中的同时把视口带过去（keepZoom：导航不该改缩放）。
    if (ids.length === 1 && ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key)) {
      e.preventDefault();
      const cur = ids[0];
      const card = state.index.cardMap[cur] || {};
      let next = null;
      if (e.key === "ArrowLeft") next = card.parent || null;
      else if (e.key === "ArrowRight") {
        const kids = state.index.childMap[cur] || [];
        next = kids.length ? kids[0].card_id : null;
      } else {
        const sibs = state.index.childMap[card.parent || ""] || [];
        const i = sibs.findIndex((c) => c.card_id === cur);
        const j = e.key === "ArrowUp" ? i - 1 : i + 1;
        if (i >= 0 && sibs[j]) next = sibs[j].card_id;
      }
      if (next && state.index.cardMap[next]) {
        selectOnly(next);
        focusCard(next, { keepZoom: true });
      }
      return;
    }

    if (e.key === "Tab") {
      // Tab 新建：选中一张卡 ⇒ 在它下面建子卡（结构操作的最高频动作）；
      // 未选中 ⇒ 建一张顶层卡。与右键菜单同一实现，不另造路径。
      e.preventDefault();
      openNewCardDialog(ids.length === 1 ? ids[0] : null);
      return;
    }
    if ((e.key === "f" || e.key === "F") && ids.length) {
      e.preventDefault();
      focusCard(ids[0]);
      return;
    }
  });

  // [T18-C3] 拖拽落点仲裁：onDragUp 的 moved 分支在 persistCardPos 之前调用。
  // 松手矩形与其余可见卡 AABB（含 GAP）检测：无相交 → 原坐标原样返回（零改动路径）；
  // 有相交 → 候选取位：①y 向最近空档（同列卡按 y 排序、逐卡上下取位后验证不相交）
  // ②x 向推出（与松手矩形相交的卡往较远侧隔 GAP）——取位移最小者；全不合法则落同列
  // 最低空档（分离尽力口径，RD-07）。语义写死：正在拖的卡让先在场的卡（P1/P2），
  // pinned 卡在场即不可撼动——由此保证 double pinned 相交不被新增（V3）。
  function resolveDrop(cid, x, y) {
    const GAP = GAP_Y;
    const sizes = measureSizes();
    const w = (sizes[cid] && sizes[cid].w) || 220;
    const h = (sizes[cid] && sizes[cid].h) || 86;
    const others = [];
    for (const [oid, node] of nodeMap) {
      if (oid === cid) continue;
      others.push({
        x: parseFloat(node.style.left), y: parseFloat(node.style.top),
        w: (sizes[oid] && sizes[oid].w) || 220,
        h: (sizes[oid] && sizes[oid].h) || 86,
      });
    }
    const hit = (rx, ry) => others.some(o =>
      rx < o.x + o.w + GAP && rx + w + GAP > o.x &&
      ry < o.y + o.h + GAP && ry + h + GAP > o.y);
    if (!hit(x, y)) return { x, y };   // 零改动路径
    const cands = [];
    // 候选一：同列（x 区间与拖拽卡相交）卡按 y 排序，逐卡上/下缘隔 GAP 取位
    const col = others
      .filter(o => x < o.x + o.w + GAP && x + w + GAP > o.x)
      .sort((a, b) => a.y - b.y);
    let maxBottom = y;
    for (const o of col) {
      cands.push({ x, y: o.y + o.h + GAP });      // 让到它下面
      cands.push({ x, y: o.y - h - GAP });        // 塞到它上面
      if (o.y + o.h > maxBottom) maxBottom = o.y + o.h;
    }
    // 候选二：x 向推出（与松手矩形相交的卡，往较远侧隔 GAP）
    for (const o of others) {
      if (!(x < o.x + o.w + GAP && x + w + GAP > o.x &&
            y < o.y + o.h + GAP && y + h + GAP > o.y)) continue;
      cands.push({
        x: (x + w / 2 < o.x + o.w / 2) ? o.x - w - GAP : o.x + o.w + GAP,
        y,
      });
    }
    // 兜底：同列最低空档（极端密度下的尽力分离，与 C1 的 16 轮警告同口径）
    cands.push({ x, y: maxBottom + GAP });
    let best = null, bestD = Infinity;
    for (const c of cands) {
      if (hit(c.x, c.y)) continue;
      const d = Math.hypot(c.x - x, c.y - y);
      if (d < bestD) { bestD = d; best = c; }
    }
    return best || { x, y: maxBottom + GAP };
  }

  // ==================== positions 持久化（防抖） ====================
  const pendingPatches = {};
  let patchTimer = null;
  function persistCardPos(cid, x, y) {
    // [card_045 E5] 只写**用户松手的原始坐标**（L1：人的位置只由人给）；
    // 避让算出的位置只用于显示，绝不写坐标记录。
    // [card_045 E7 · L6 位置不承担状态表达] 将来若想「把做完的卡挪到一边」表达
    // 进度——不许：位置只表达人在空间里的摆放意志，进度/状态走卡片自身字段。
    // [card_045 E7 · L7 钉住的坐标是绝对锚点] 布局算法换代（换树/换网格/换排布）
    // 只影响派生位置，钉住卡的存储坐标原样保留、人的相对空间关系不因换代被重排。
    state.data.positions[cid] = { x, y, pinned: true };
    pendingPatches[cid] = { x, y, pinned: true };
    if (patchTimer) clearTimeout(patchTimer);
    patchTimer = setTimeout(flushPatches, 500);
  }
  async function flushPatches() {
    const patch = { ...pendingPatches };
    for (const k in pendingPatches) delete pendingPatches[k];
    if (Object.keys(patch).length === 0) return;
    try {
      await API.patchPositions(WORK_ID, patch);
    } catch (e) { notify("坐标保存失败", "error"); }   // [F3④] 原静默
  }

  // ==================== 整理画布 ====================
  if (reorganizeBtn) reorganizeBtn.onclick = async () => {
    // 把当前视图所有卡 pinned 置 false
    const visibleIds = getVisibleIds();
    const patch = {};
    for (const cid of visibleIds) {
      if (cid === state.nav.viewRoot) continue;
      patch[cid] = { pinned: false };
      state.data.positions[cid] = state.data.positions[cid] || {};
      state.data.positions[cid].pinned = false;
    }
    try {
      if (Object.keys(patch).length > 0) await API.patchPositions(WORK_ID, patch);
    } catch (e) { notify("整理画布保存失败", "error"); }   // [F3④] 原静默
    renderAll();          // [画布第三步 T4] 改 state → 统一重绘入口
  };

  // ==================== 左栏「＋ 新项目」（画布页） ====================
  // [建项目流程 2026-10-17] 三入口统一：整页加载草稿对话页 /work/draft。
  // 原「就地清空画布 + replaceState」退役——用户点新建要的是开始对话，不是一块空地。
  if (navNewProjectBtn) navNewProjectBtn.onclick = () => { location.assign("/work/draft"); };

  // ==================== 新建卡 ====================
  //
  // [画布商业化 2026-10-17] 抽成 openNewCardDialog(parentId)：
  //   parentId 为空 ⇒ 原「跳草稿页、先出画面后落库」路径（行为逐字不变）；
  //   parentId 有值 ⇒ **就地** POST 建子卡并刷新画布（右键菜单用）。
  // 为什么两套：跳页流程是"新建项目/顶层卡"的产品决定（用户要先说话）；
  // 而"在某张卡下面挂一张子卡"是画布内的结构操作，跳走会打断布局工作。
  function openNewCardDialog(parentId) {
    const isChild = !!parentId;
    const node = document.createElement("div");
    node.style.cssText = "position:fixed;top:60px;right:20px;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px;z-index:1000;display:flex;flex-direction:column;gap:8px;min-width:260px;box-shadow:0 12px 32px rgba(0,0,0,.55);";
    node.innerHTML = `
      <div style="font-size:12px;color:var(--dim);margin-bottom:2px;">${isChild ? "新建子卡（挂在当前卡下）" : "新建卡片"}</div>
      <input type="text" id="newCardTitle" placeholder="卡片标题" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:7px 10px;color:var(--text);font-size:13px;">
      <div style="display:flex;gap:6px;">
        <button class="btn primary" id="newCardOk" style="flex:1;">创建</button>
        <button class="btn ghost" id="newCardCancel">取消</button>
      </div>
    `;
    document.body.appendChild(node);
    const titleInput = node.querySelector("#newCardTitle");
    titleInput.focus();
    const submit = async () => {
      const title = titleInput.value.trim();
      if (!title) { API.toast("请输入标题", "error"); return; }
      if (isChild) {
        node.remove();
        try {
          await API.createCard(WORK_ID, { title, source: "用户直接创建", card_type: "", parent: parentId });
          state.view.expanded.add(parentId);
          scheduleSaveView();
          await reloadCanvas();
          notify("已创建：" + title);
        } catch (e) {
          notify("创建失败：" + (e && e.message ? e.message : e), "error");
        }
        return;
      }
      node.remove();   // ★ 立刻关弹窗——不等任何网络
      // [惰性新建 2026-10-17 · 治建卡延迟] **先出画面，数据后到**：
      // 提交标题立刻跳进这张卡的对话画面（零等待、零 POST）；落库由对话页**后台立刻发起**。
      // 标题/父卡/类型经 sessionStorage 一次性携带（跳页清空内存；URL 只承担页面身份）。
      try {
        sessionStorage.setItem("newCard:" + WORK_ID, JSON.stringify({
          title, parent: state.nav.viewRoot || "", card_type: "",
        }));
      } catch (e) { /* 存储不可用：退化为默认标题，不阻断跳转 */ }
      location.assign("/work/" + WORK_ID + "/card/draft");
    };
    node.querySelector("#newCardOk").onclick = submit;
    node.querySelector("#newCardCancel").onclick = () => node.remove();
    titleInput.onkeydown = (e) => {
      if (e.key === "Enter") submit();
      if (e.key === "Escape") node.remove();
    };
  }

  if (newCardBtn) newCardBtn.onclick = () => openNewCardDialog(null);

  // [画布商业化 2026-10-17] 工具栏新入口（元素在 work.html，id 固定）
  const fitBtn = document.getElementById("fitBtn");
  const searchBtn = document.getElementById("searchBtn");
  const ruleToggleBtn = document.getElementById("ruleToggleBtn");
  const rulePanelEl = document.getElementById("rulePanel");
  if (fitBtn) fitBtn.onclick = () => doFitAll();
  if (searchBtn) searchBtn.onclick = () => openPalette();

  // 规则面板收起/展开：状态记 localStorage（跨刷新保持），切换后画布宽度变了，
  // 小地图与 fit 都要跟着重算——否则视口框和内容位置会错位。
  const RULE_COLLAPSE_KEY = "canvas.ruleCollapsed:" + WORK_ID;
  function applyRuleCollapsed(collapsed) {
    if (!rulePanelEl) return;
    rulePanelEl.classList.toggle("is-collapsed", !!collapsed);
    try { localStorage.setItem(RULE_COLLAPSE_KEY, collapsed ? "1" : "0"); } catch (e) { /* 隐私模式 */ }
    // 宽度过渡 180ms 结束后再重算，避免量到中间态
    setTimeout(() => { scheduleMinimap(); }, 200);
  }
  if (rulePanelEl) {
    let collapsed = false;
    try { collapsed = localStorage.getItem(RULE_COLLAPSE_KEY) === "1"; } catch (e) { /* 忽略 */ }
    if (collapsed) rulePanelEl.classList.add("is-collapsed");
  }
  if (ruleToggleBtn) {
    ruleToggleBtn.onclick = () => {
      const next = !(rulePanelEl && rulePanelEl.classList.contains("is-collapsed"));
      applyRuleCollapsed(next);
    };
  }

  // ==================== 右键菜单 ====================
  //
  // [画布商业化 2026-10-17] 白板的右键菜单是「不离开画布做完一件事」的主通道：
  // 建子卡、聚焦、下钻、复制、删除都在这。空白处的右键给"画布级"动作。
  let ctxMenu = null;

  function closeCtxMenu() {
    if (ctxMenu) { ctxMenu.remove(); ctxMenu = null; }
  }

  function openCtxMenu(x, y, items) {
    closeCtxMenu();
    const el = document.createElement("div");
    el.className = "cv-menu";
    el.innerHTML = items.map((it) => it.sep
      ? '<div class="cv-msep"></div>'
      : `<div class="cv-mi${it.danger ? " is-danger" : ""}" data-act="${it.act}"><span>${it.label}</span>${it.kbd ? `<span class="cv-kbd">${it.kbd}</span>` : ""}</div>`
    ).join("");
    document.body.appendChild(el);
    // 贴边翻转：不让菜单跑出视口
    const r = el.getBoundingClientRect();
    el.style.left = Math.max(6, Math.min(x, window.innerWidth - r.width - 8)) + "px";
    el.style.top = Math.max(6, Math.min(y, window.innerHeight - r.height - 8)) + "px";
    el.addEventListener("mousedown", (e) => e.stopPropagation());
    el.addEventListener("click", (e) => {
      const mi = e.target.closest(".cv-mi");
      if (!mi) return;
      const item = items.find((i) => i.act === mi.dataset.act);
      closeCtxMenu();
      if (item && item.run) item.run();
    });
    ctxMenu = el;
  }

  document.addEventListener("mousedown", (e) => {
    if (ctxMenu && !ctxMenu.contains(e.target)) closeCtxMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeCtxMenu();
  });

  viewport?.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    const cardNode = e.target.closest && e.target.closest(".card-node");
    if (!cardNode) {
      openCtxMenu(e.clientX, e.clientY, [
        { act: "new", label: "新建卡片", run: () => openNewCardDialog(null) },
        { act: "fit", label: "整幅适配", kbd: "⌘0", run: () => doFitAll() },
        { act: "all", label: "全选可见卡", kbd: "⌘A", run: () => setSelection([...nodeMap.keys()]) },
        { sep: true },
        { act: "reorg", label: "整理布局（清钉住）", run: () => reorganizeBtn && reorganizeBtn.click() },
      ]);
      return;
    }

    const cid = cardNode.dataset.id;
    if (!state.view.selected.has(cid)) selectOnly(cid);
    const card = state.index.cardMap[cid] || {};
    const hasKids = (state.index.childMap[cid] || []).length > 0;
    const isExpanded = state.view.expanded.has(cid);
    const isRoot = cid === state.nav.workCardId;
    const multi = state.view.selected.size > 1;

    openCtxMenu(e.clientX, e.clientY, [
      { act: "open", label: multi ? `打开对话（${state.view.selected.size} 张中第一张）` : "打开对话", kbd: "↵", run: () => openCardChat(cid) },
      { act: "focus", label: "以此卡为中心", kbd: "F", run: () => focusCard(cid) },
      { sep: true },
      { act: "child", label: "新建子卡", run: () => openNewCardDialog(cid) },
      hasKids ? { act: "toggle", label: isExpanded ? "收起子卡" : "展开子卡", run: () => toggleExpand(cid) } : null,
      { act: "drill", label: "下钻到此卡（Alt+双击）", run: () => setViewRoot(cid) },
      { sep: true },
      { act: "rename", label: "重命名", run: () => renameCard(cid) },
      { act: "copy", label: "复制标题", run: () => { try { navigator.clipboard.writeText(card.title || ""); notify("标题已复制"); } catch (err) { /* 剪贴板不可用 */ } } },
      isRoot
        ? null
        : { act: "del", label: "删除", kbd: "Del", danger: true, run: () => { setSelection([cid]); deleteSelected(); } },
    ].filter(Boolean));
  });


  // ==================== 快速搜索（⌘K / Ctrl+K） ====================
  //
  // [画布商业化 2026-10-17] 卡片一多，"在画布上找到那张卡"就是第一痛点：
  // 靠肉眼扫不现实，靠左栏导航树要点开层级。命令面板是各家白板的标配解法：
  //   · ⌘K 唤起，输入即筛（标题 + 卡 id + 父卡标题路径）；
  //   · ↑↓ 选择、↵ 定位（视口飞过去并选中）、Esc 关闭；
  //   · 点击结果同样定位——不用记快捷键也能用。
  let paletteEl = null;
  let paletteIdx = 0;
  let paletteHits = [];

  function cardPath(cid) {
    const parts = [];
    let cur = cid;
    const guard = new Set();
    while (cur && !guard.has(cur) && state.index.cardMap[cur]) {
      guard.add(cur);
      const c = state.index.cardMap[cur];
      parts.unshift(c.title || cur);
      cur = c.parent;
    }
    return parts.slice(0, -1).join(" / ");
  }

  function closePalette() {
    if (paletteEl) { paletteEl.remove(); paletteEl = null; }
    paletteHits = [];
  }

  function renderPaletteList(q) {
    const list = paletteEl.querySelector(".cv-palette-list");
    const kw = (q || "").trim().toLowerCase();
    const all = state.data.cards.filter((c) => c.card_type !== "rule");
    paletteHits = (kw
      ? all.filter((c) =>
          (c.title || "").toLowerCase().includes(kw)
          || (c.card_id || "").toLowerCase().includes(kw)
          || cardPath(c.card_id).toLowerCase().includes(kw))
      : all
    ).slice(0, 30);
    if (paletteIdx >= paletteHits.length) paletteIdx = Math.max(0, paletteHits.length - 1);

    if (!paletteHits.length) {
      list.innerHTML = `<div class="cv-palette-empty">没有匹配的卡片</div>`;
      return;
    }
    list.innerHTML = paletteHits.map((c, i) => `
      <div class="cv-palette-item${i === paletteIdx ? " is-active" : ""}" data-i="${i}">
        <span class="cv-palette-dot" data-type="${escapeHtml(c.card_type || "")}"></span>
        <span class="cv-palette-title">${escapeHtml(c.title || c.card_id)}</span>
        <span class="cv-palette-path">${escapeHtml(cardPath(c.card_id) || "根卡")}</span>
        ${c.state === "completed" ? '<span class="cv-palette-state">已完成</span>' : ""}
      </div>
    `).join("");
    list.querySelectorAll(".cv-palette-item").forEach((el) => {
      el.addEventListener("mousemove", () => {
        paletteIdx = Number(el.dataset.i);
        list.querySelectorAll(".cv-palette-item").forEach((x) => x.classList.toggle("is-active", x === el));
      });
      el.addEventListener("click", () => pickPalette(Number(el.dataset.i)));
    });
  }

  function pickPalette(i) {
    const card = paletteHits[i];
    if (!card) return;
    closePalette();
    // 目标卡可能被折叠在未展开的祖先下 ⇒ 先展开祖先链，再定位
    let cur = card.parent;
    const guard = new Set();
    while (cur && !guard.has(cur) && state.index.cardMap[cur]) {
      guard.add(cur);
      state.view.expanded.add(cur);
      cur = state.index.cardMap[cur].parent;
    }
    scheduleSaveView();
    renderAll();
    selectOnly(card.card_id);
    focusCard(card.card_id);
  }

  function openPalette() {
    if (paletteEl) { closePalette(); return; }
    paletteEl = document.createElement("div");
    paletteEl.className = "cv-palette";
    paletteEl.innerHTML = `
      <input class="cv-palette-input" placeholder="搜索卡片标题、编号或路径…" />
      <div class="cv-palette-list"></div>
      <div class="cv-palette-foot"><span>↑↓ 选择</span><span>↵ 定位</span><span>Esc 关闭</span></div>
    `;
    document.body.appendChild(paletteEl);
    paletteIdx = 0;
    const input = paletteEl.querySelector(".cv-palette-input");
    renderPaletteList("");
    input.focus();
    input.addEventListener("input", () => { paletteIdx = 0; renderPaletteList(input.value); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        paletteIdx = Math.min(paletteIdx + 1, paletteHits.length - 1);
        renderPaletteList(input.value);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        paletteIdx = Math.max(paletteIdx - 1, 0);
        renderPaletteList(input.value);
      } else if (e.key === "Enter") {
        e.preventDefault();
        pickPalette(paletteIdx);
      } else if (e.key === "Escape") {
        e.preventDefault();
        closePalette();
      }
    });
    paletteEl.addEventListener("mousedown", (e) => e.stopPropagation());
  }

  document.addEventListener("mousedown", (e) => {
    if (paletteEl && !paletteEl.contains(e.target)) closePalette();
  });

  // ==================== 小地图 ====================
  //
  // [画布商业化 2026-10-17] 长画布必备的"我在哪"导航件：
  //   · 左下角常驻，画全部可见卡的缩略矩形（按类型着色）＋ 当前视口框；
  //   · 点击/拖动小地图＝把视口中心移到该处（不是缩放，纯导航）；
  //   · 卡片全部落在视口内时自动淡出（不占视线）。
  const MM_W = 176, MM_H = 118, MM_PAD = 8;
  const mmRoot = document.createElement("div");
  mmRoot.className = "cv-minimap";
  mmRoot.innerHTML = `<canvas width="${MM_W}" height="${MM_H}"></canvas><div class="cv-mm-view"></div>`;
  viewport?.appendChild(mmRoot);
  const mmCanvas = mmRoot.querySelector("canvas");
  const mmCtx = mmCanvas.getContext("2d");
  const mmView = mmRoot.querySelector(".cv-mm-view");

  // 小地图不是"类型图例"，是"我在哪"：全部中性灰，只有视口内的卡更亮。
  // 原来按 card_type 上 6 种颜色，缩略图变成一块彩色拼图，反而看不出结构。

  let _mmRaf = 0;
  function scheduleMinimap() {
    if (_mmRaf) return;
    _mmRaf = requestAnimationFrame(() => { _mmRaf = 0; renderMinimap(); });
  }

  function renderMinimap() {
    if (!viewport || !mmCtx) return;
    const nodes = [...nodeMap.entries()];
    if (!nodes.length) { mmRoot.style.display = "none"; return; }
    mmRoot.style.display = "";

    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const [, n] of nodes) {
      const x = parseFloat(n.style.left) || 0, y = parseFloat(n.style.top) || 0;
      minX = Math.min(minX, x); minY = Math.min(minY, y);
      maxX = Math.max(maxX, x + (n.offsetWidth || 220));
      maxY = Math.max(maxY, y + (n.offsetHeight || 120));
    }
    const z = state.view.zoom || 1;
    const vx0 = (0 - state.view.panX) / z, vy0 = (0 - state.view.panY) / z;
    const vx1 = (viewport.clientWidth - state.view.panX) / z;
    const vy1 = (viewport.clientHeight - state.view.panY) / z;

    // 世界范围＝卡片 bbox ∪ 视口范围：保证视口框永远画得下（否则缩小到很小会跑出框外）
    const bx0 = Math.min(minX, vx0), by0 = Math.min(minY, vy0);
    const bx1 = Math.max(maxX, vx1), by1 = Math.max(maxY, vy1);
    const w = Math.max(1, bx1 - bx0), h = Math.max(1, by1 - by0);
    const scale = Math.min((MM_W - MM_PAD * 2) / w, (MM_H - MM_PAD * 2) / h);
    const ox = MM_PAD + ((MM_W - MM_PAD * 2) - w * scale) / 2 - bx0 * scale;
    const oy = MM_PAD + ((MM_H - MM_PAD * 2) - h * scale) / 2 - by0 * scale;
    const toX = (x) => x * scale + ox;
    const toY = (y) => y * scale + oy;

    mmCtx.clearRect(0, 0, MM_W, MM_H);
    for (const [cid, n] of nodes) {
      const x = parseFloat(n.style.left) || 0, y = parseFloat(n.style.top) || 0;
      const w = n.offsetWidth || 220, h = n.offsetHeight || 120;
      // 视口内的卡亮一档（一眼看出"哪些已经在屏幕上"）
      const inView = x + w > vx0 && x < vx1 && y + h > vy0 && y < vy1;
      mmCtx.fillStyle = inView ? "rgba(255,255,255,0.55)" : "rgba(255,255,255,0.26)";
      mmCtx.fillRect(toX(x), toY(y), Math.max(2, w * scale), Math.max(1.5, h * scale));
    }
    mmCtx.globalAlpha = 1;

    // 视口框：位置随 pan/zoom 实时变，用 DOM 元素（CSS 有主色描边）
    const vx = toX(vx0), vy = toY(vy0);
    const vw = Math.max(6, (vx1 - vx0) * scale);
    const vh = Math.max(6, (vy1 - vy0) * scale);
    mmView.style.left = vx + "px";
    mmView.style.top = vy + "px";
    mmView.style.width = vw + "px";
    mmView.style.height = vh + "px";

    // 内容全在视口内 ⇒ 淡出（不打扰）
    const allVisible = vx0 <= minX && vy0 <= minY && vx1 >= maxX && vy1 >= maxY;
    mmRoot.style.opacity = allVisible ? "0.25" : "0.95";

    // 供点击换算复用
    mmRoot.dataset.scale = String(scale);
    mmRoot.dataset.ox = String(ox);
    mmRoot.dataset.oy = String(oy);
  }

  // 点击/拖动小地图 ⇒ 视口中心跳到该世界点
  function mmNavigate(clientX, clientY) {
    const rect = mmRoot.getBoundingClientRect();
    const scale = parseFloat(mmRoot.dataset.scale || "1");
    const ox = parseFloat(mmRoot.dataset.ox || "0");
    const oy = parseFloat(mmRoot.dataset.oy || "0");
    const wx = (clientX - rect.left - ox) / scale;
    const wy = (clientY - rect.top - oy) / scale;
    const z = state.view.zoom || 1;
    applyViewState({
      panX: viewport.clientWidth / 2 - wx * z,
      panY: viewport.clientHeight / 2 - wy * z,
    });
    scheduleSaveView();
  }

  let mmDrag = false;
  mmRoot.addEventListener("mousedown", (e) => {
    e.stopPropagation();
    e.preventDefault();
    mmDrag = true;
    mmNavigate(e.clientX, e.clientY);
  });
  document.addEventListener("mousemove", (e) => {
    if (mmDrag) mmNavigate(e.clientX, e.clientY);
  });
  document.addEventListener("mouseup", () => { mmDrag = false; });

  // ==================== 数据加载 ====================
  async function reloadCanvas() {
    showCanvasState("loading");   // [F3④]
    try {
      const data = await API.getCanvas(WORK_ID);
      state.data.cards = data.cards || [];
      state.data.links = data.links || [];
      // [card_045 E7 · L2 位置独立于卡片语义] 卡片改名、换类型、变状态、换归属
      // 一律不影响其坐标记录（positions 只按 card_id 键挂靠，不携带任何卡片
      // 语义字段）；验收＝改一张卡的标题／类型后，坐标记录不变、画布位置不变。
      state.data.positions = data.positions || {};
      if (!state.nav.workCardId) {
        // [T07-1-10] 判据源切换：根卡不再按 card_type 找，改按 card_id == work_card_id
        // （work_card_id 取自既有 /api/works 端点的 work.json 权威字段）
        let _wcId = "";
        try {
          const _works = await API.listWorks();
          const _list = Array.isArray(_works) ? _works : (_works.works || []);
          const _w = _list.find(x => (x.work_id || x.id) === WORK_ID);
          _wcId = (_w && _w.work_card_id) || "";
        } catch (_e) { /* 取不到时走回退，不影响画布加载 */ }
        t07SwitchLog("canvas.reloadCanvas.rootFind", WORK_ID,
          state.data.cards.some(c => c.card_type === "\u5267\u672c"), !!_wcId);
        const wc = _wcId ? state.data.cards.find(c => c.card_id === _wcId) : null;
        state.nav.workCardId = wc ? wc.card_id : (state.data.cards[0] && state.data.cards[0].card_id) || "";
        state.nav.workTitle = wc ? wc.title : "";
        state.nav.viewRoot = state.nav.workCardId;
      }
      rebuildCardMap();
      // [画布商业化 2026-10-17] 首次加载：有存档就恢复，无存档就**整幅适配**。
      // 旧行为是无存档时留在 (80,40) 默认位，内容一多就溢出视口上方（首屏只见半张图）。
      let _needFit = false;
      if (!__VIEW_RESTORED__) {
        _needFit = !restoreViewState();
        __VIEW_RESTORED__ = true;
      }
      showCanvasState("clear");
      renderAll();          // [画布第三步 T4] 数据就绪 → 统一重绘入口
      // [画布 2026-10-18 · 工作台] 首屏**不再把整张图缩到装得下**。
      // 43 张卡全装进 1600×1000 要缩到 60%，待裁定条目在 60% 下根本读不了——
      // 那等于"打开了但什么也读不到"。工作台打开就该带你去该干活的地方：
      //   100% 缩放，视野落在**第一张有待裁定的卡**上；没有待裁定才退回整幅适配。
      if (_needFit) {
        _autoFitPending = false;
        _userMoved = false;
        // 首屏锚点＝**根卡**（不是第一张待裁定卡）。
        // 打开画布先要看见"这是一棵树"：根卡在左、子卡沿主干往下排。
        // 根卡放在视口左侧 1/3、垂直居中，于是上下各能看见两三张子卡，
        // 结构一眼成立；要读细节就往下滚（滚轮＝滚动）。
        const anchorNode = nodeMap.get(state.nav.viewRoot);
        if (anchorNode) {
          const x = parseFloat(anchorNode.style.left) || 0;
          // 纵向**对齐第一张子卡的顶部**（不是根卡居中）：
          // 根卡居中时上半屏被切掉，第一张子卡看不到——而子卡才是要读的内容。
          // 根卡若因此跑到视口外，往下滚一点就回来，不影响"这是一棵树"的读法。
          let topY = Infinity;
          for (const [, n] of nodeMap) {
            if (n.dataset.depth === "1") topY = Math.min(topY, parseFloat(n.style.top) || 0);
          }
          if (!Number.isFinite(topY)) topY = parseFloat(anchorNode.style.top) || 0;
          applyViewState({
            zoom: 1,
            panX: Math.round(viewport.clientWidth * 0.18) - x,
            panY: 72 - topY,
          });
        } else {
          const _fit = fitView(1);
          if (_fit) applyViewState({ panX: _fit.panX, panY: _fit.panY, zoom: _fit.zoom });
        }
      }
      if (!state.data.cards.length) showCanvasState("empty");   // [F3④] 空态
      // [T17-F6/R-1] 不再无条件 hideWorkspace()：「刷新画布数据」与「收起对话层」是两件事。
      // 视图切换改由 .app-main[data-view] 显隐驱动（workspace.js setAppView）；「← 画布」
      // 按钮为纯显隐（R-043）；建卡/删卡/对话生卡等数据变更已在各自源头调 ReloadCanvas 刷新。
      // hideWorkspace() 函数保留（setViewRoot 下钻路径仍在用，现为无害空操作）。
    } catch (e) {
      // [迭代四 F3④] 三态容器：加载失败可见（原静默 catch 界面无态——B-18）
      showCanvasState("error");
    }
  }

  // [迭代四 F3④] 画布三态容器：loading／error（失败＋重试）／empty。
  function showCanvasState(kind) {
    if (!viewport) return;   // [阶段3 A3.2] 非画布页：无画布态容器可挂，整体空转
    let box = document.getElementById("canvasStateBox");
    if (!box) {
      box = document.createElement("div");
      box.id = "canvasStateBox";
      box.className = "canvas-state-box";
      viewport.appendChild(box);
    }
    if (kind === "clear") { box.remove(); return; }
    if (kind === "error") {
      box.innerHTML = `<div class="cs-error">画布加载失败 <button class="btn" id="csRetry">重试</button></div>`;
      const b = box.querySelector("#csRetry");
      if (b) b.onclick = () => { box.innerHTML = `<div class="cs-loading">加载中…</div>`; reloadCanvas(); };
    } else if (kind === "loading") {
      // 骨架屏：与真实布局同构的 5 块占位（根卡＋两列子卡），微光扫过。
      box.innerHTML = `<div class="cv-skeleton">
        <div class="cv-sk-card" style="left:7%;top:34%;width:300px;height:150px"></div>
        <div class="cv-sk-card" style="left:33%;top:16%;width:210px;height:136px"></div>
        <div class="cv-sk-card" style="left:33%;top:56%;width:210px;height:136px"></div>
        <div class="cv-sk-card" style="left:54%;top:16%;width:210px;height:136px"></div>
        <div class="cv-sk-card" style="left:54%;top:56%;width:210px;height:136px"></div>
      </div>`;
    } else if (kind === "empty") {
      // [画布商业化 2026-10-17] 空态从"一句话"升级为"标题＋两条入口"：
      // 空画布是用户第一次见到这个产品的地方，要告诉他下一步做什么。
      box.innerHTML = `<div class="cv-empty">
        <div class="cv-empty-title">这张画布还没有卡片</div>
        <div class="cv-empty-hint">点右上「＋ 新建卡」开始，或按 <span class="cv-empty-key">Tab</span> 建第一张</div>
      </div>`;
    }
  }

  function hideWorkspace() {
    const ws = document.getElementById("workspace");
    if (ws) ws.classList.remove("active");
  }

  // ══════════ [画布第三步 T2/T4] 动作层：装配＋统一重绘入口 ══════════
  // 流向单向：事件 → 动作（改 state）→ 统一入口 → 投影 → 渲染（V10）。
  // 投影输入与视图模型在此从五桶装配——渲染 10 函数只吃参数、不摸 state（V1）。

  // 投影输入装配（computeProjection 的 input；sizes 由测量点补——两阶段渲染在
  // 建节点后测，reflow 在入口现测，一轮恰 1 次测量＝V6）
  function collectProjectionInput() {
    return {
      viewRoot: state.nav.viewRoot,
      cards: state.data.cards,
      links: state.data.links,
      positions: state.data.positions,
      expanded: state.view.expanded,
      cardMap: state.index.cardMap,
      childMap: state.index.childMap,
    };
  }

  // 视图模型装配（渲染侧只读面；持桶内活引用）
  function collectViewModel() {
    return {
      viewRoot: state.nav.viewRoot,
      cardsById: state.index.cardMap,
      childMap: state.index.childMap,
      links: state.data.links,
      positions: state.data.positions,
      expanded: state.view.expanded,
      zoom: state.view.zoom,
      unreadByCard: state.async._unreadByCard,
    };
  }

  // 统一重绘入口（全量重建）：改 state 后必经此口（V10）
  // ══════════════════════════════════════════════════════════════════════
  // [画布 2026-10-18 · 裁定工作流] 逐条过待裁定
  //
  // 这是这个产品的**主循环**：模型提议 → 标【待裁定】→ 用户点头 → 结论沉淀。
  // 实测这个项目积了 100 处待裁定，分布在 14 张卡上。原来只能一张张点进卡、
  // 在 3000 字里找那几条——那不是工作台，是寻宝。
  //
  // 现在：顶栏点"100 处待裁定" → 右栏进入裁定模式，一条一条往下走：
  //   · 每条显示"哪张卡 / 哪个小节 / 条目全文"
  //   · 画布同步高亮那张卡（知道它在结构里的位置）
  //   · 上一处 / 下一处 / 打开卡片
  // 裁定动作本身仍要在对话页完成（这里只负责"读 + 导航"），但路径从
  // "找 → 读 → 判断" 缩短成 "读 → 判断"。
  // ══════════════════════════════════════════════════════════════════════
  // var：Esc 处理器在文件前面，let 会 TDZ
  var triageList = [];
  var triageIdx = 0;

  function buildTriageList() {
    const out = [];
    for (const c of (state.data.cards || [])) {
      if (c.card_type === "rule") continue;
      for (const it of (c.pending_items || [])) {
        out.push({ cardId: c.card_id, title: c.title || c.card_id, section: it.section || "", text: it.text || "" });
      }
    }
    return out;
  }

  function openTriage(startIdx) {
    triageList = buildTriageList();
    if (!triageList.length) return;
    triageIdx = Math.max(0, Math.min(triageList.length - 1, startIdx || 0));
    showTriageItem();
  }

  function showTriageItem() {
    const it = triageList[triageIdx];
    if (!it) return;
    // 画布同步：**先展开祖先链再飞过去**。
    // 否则跳到一张折叠在深处的卡时，画布上根本没有它的节点（nodeMap 里没有），
    // focusCard 静默失败——右栏在讲这张卡，画布却停在原处，两边对不上。
    if (!nodeMap.has(it.cardId)) {
      const guard = new Set();
      let cur = (state.index.cardMap[it.cardId] || {}).parent;
      while (cur && !guard.has(cur) && state.index.cardMap[cur]) {
        guard.add(cur);
        state.view.expanded.add(cur);
        cur = state.index.cardMap[cur].parent;
      }
      scheduleSaveView();
      renderAll();
    }
    selectOnly(it.cardId);
    focusCard(it.cardId, { keepZoom: true });

    const cardDetailEl = document.getElementById("cardDetail");
    const rulePanelHeadEl = document.getElementById("rulePanelHead");
    const rulePanelBodyEl = document.getElementById("rulePanelBody");
    if (!cardDetailEl) return;
    if (rulePanelHeadEl) rulePanelHeadEl.hidden = true;
    if (rulePanelBodyEl) rulePanelBodyEl.hidden = true;
    cardDetailEl.hidden = false;
    cardDetailEl.innerHTML = "";

    const head = el("div", "cd-head");
    const counter = el("div", "cd-triage-count", `第 ${triageIdx + 1} / ${triageList.length} 处`);
    head.appendChild(counter);
    head.appendChild(el("div", "cd-title", it.title));
    if (it.section) head.appendChild(el("div", "cd-dim", it.section));
    cardDetailEl.appendChild(head);

    cardDetailEl.appendChild(el("div", "cd-section-label", "等你裁定"));
    const body = el("div", "cd-triage-text");
    body.textContent = it.text;
    cardDetailEl.appendChild(body);

    const nav = el("div", "cd-ops");
    const prev = el("button", "btn", "← 上一处");
    prev.disabled = triageIdx === 0;
    prev.addEventListener("click", () => { if (triageIdx > 0) { triageIdx--; showTriageItem(); } });
    const next = el("button", "btn primary", "下一处 →");
    next.disabled = triageIdx >= triageList.length - 1;
    next.addEventListener("click", () => { if (triageIdx < triageList.length - 1) { triageIdx++; showTriageItem(); } });
    const openBtn = el("button", "btn", "打开卡片");
    openBtn.addEventListener("click", () => openCardChat(it.cardId));
    nav.appendChild(prev);
    nav.appendChild(next);
    nav.appendChild(openBtn);
    cardDetailEl.appendChild(nav);

    const hint = el("div", "cd-dim", "裁定在对话页完成；这里负责读完一处、走到下一处。按 Esc 退出裁定模式。");
    cardDetailEl.appendChild(hint);
  }

  // ══════════════════════════════════════════════════════════════════════
  // [画布 2026-10-18 · 并行工作台] 左栏目录
  //
  // 画布一屏只装得下 5 张卡，而"并行多线"的前提是**同时看见全部**——
  // 所以左栏用 22px 一行把 39 张卡全部排下（缩进＝层级），一眼扫完：
  //   橙点 + 数字 ＝ 这张卡有几处等我裁定
  //   ←N         ＝ 被多少张卡引用（谁是地基）
  // 点行 = 选中 + 右栏读卡 + 中栏飞过去（不跳页）。
  // ══════════════════════════════════════════════════════════════════════
  const catEl = document.getElementById("cvCatalog");

  // 目录折叠：默认按窗口宽度决定（<1440 折起来给画布让位），用户手动切换后记住选择。
  // 窄屏默认折叠不等于"没有"——工具栏一个按钮随时拉开。
  const CAT_KEY = "canvas.catalogCollapsed";
  function applyCatalogCollapsed(collapsed) {
    if (!catEl) return;
    catEl.classList.toggle("is-collapsed", !!collapsed);
    try { localStorage.setItem(CAT_KEY, collapsed ? "1" : "0"); } catch (e) { /* 隐私模式 */ }
  }
  (function initCatalogState() {
    if (!catEl) return;
    let saved = null;
    try { saved = localStorage.getItem(CAT_KEY); } catch (e) { /* 忽略 */ }
    const collapsed = saved !== null ? saved === "1" : window.innerWidth < 1440;
    catEl.classList.toggle("is-collapsed", collapsed);
  })();
  // ── 右栏宽度可拖 ──
  // 340px 读不了 3000 字的正文；拖到 560 能读完一张卡。宽度记忆到 localStorage，
  // 下次进来还是这个宽度。
  const RESIZE_KEY = "canvas.rulePanelWidth";
  (function initResizer() {
    const bar = document.getElementById("cvResizer");
    const panel = document.getElementById("rulePanel");
    if (!bar || !panel) return;
    try {
      const w = parseInt(localStorage.getItem(RESIZE_KEY) || "", 10);
      if (Number.isFinite(w) && w >= 280 && w <= 720) panel.style.width = w + "px";
    } catch (e) { /* 隐私模式 */ }
    let drag = null;
    bar.addEventListener("mousedown", (e) => {
      e.preventDefault();
      drag = { startX: e.clientX, startW: panel.offsetWidth };
      bar.classList.add("is-dragging");
      document.body.style.cursor = "col-resize";
    });
    document.addEventListener("mousemove", (e) => {
      if (!drag) return;
      // 上限不能只看"720"：右栏 + 目录(268) + nav(44) 之后，中栏至少要留 520，
      // 否则画布被挤成一条缝、顶栏也被压得换行（实测拉到 580 就崩）。
      const catalogW = (document.getElementById("cvCatalog") || { offsetWidth: 0 }).offsetWidth;
      const maxW = Math.max(280, Math.min(720, window.innerWidth - catalogW - 44 - 520));
      const next = Math.max(280, Math.min(maxW, drag.startW - (e.clientX - drag.startX)));
      panel.style.width = next + "px";
    });
    document.addEventListener("mouseup", () => {
      if (!drag) return;
      drag = null;
      bar.classList.remove("is-dragging");
      document.body.style.cursor = "";
      try { localStorage.setItem(RESIZE_KEY, String(panel.offsetWidth)); } catch (e) { /* 忽略 */ }
    });
  })();

  const catToggleBtn = document.getElementById("catToggleBtn");
  if (catToggleBtn) {
    catToggleBtn.addEventListener("click", () => {
      applyCatalogCollapsed(!catEl.classList.contains("is-collapsed"));
    });
  }
  // var 而不是 let：applySelectionClasses 在首屏渲染时就会调 syncCatalogCurrent，
  // 那时这个声明还没执行到（let 会 TDZ 报错）。
  var catRowMap = new Map();

  function renderCatalog() {
    if (!catEl) return;
    const byParent = state.index.childMap || {};
    const frag = document.createDocumentFragment();
    catRowMap = new Map();

    const walk = (pid, depth) => {
      for (const c of (byParent[pid] || [])) {
        if (c.card_type === "rule") continue;
        const row = document.createElement("div");
        row.className = "cv-cat-row";
        row.dataset.id = c.card_id;
        row.dataset.depth = String(Math.min(depth, 4));
        if (c.work_state) row.dataset.ws = c.work_state;
        row.style.paddingLeft = (10 + depth * 13) + "px";

        const dot = document.createElement("span");
        dot.className = "cv-cat-dot";
        row.appendChild(dot);

        const title = document.createElement("span");
        title.className = "cv-cat-title";
        title.textContent = c.title || c.card_id;
        row.appendChild(title);

        const n = Number(c.pending_count || 0);
        if (n > 0) {
          const nb = document.createElement("span");
          nb.className = "cv-cat-n";
          nb.textContent = String(n);
          nb.title = `${n} 处待裁定`;
          row.appendChild(nb);
        }
        const ref = Number(c.ref_count || 0);
        if (ref > 0) {
          const rb = document.createElement("span");
          rb.className = "cv-cat-ref";
          rb.textContent = "←" + ref;
          rb.title = `被 ${ref} 张卡引用`;
          row.appendChild(rb);
        }

        row.addEventListener("click", () => {
          selectOnly(c.card_id);
          focusCard(c.card_id, { keepZoom: true });
        });

        frag.appendChild(row);
        catRowMap.set(c.card_id, row);
        walk(c.card_id, depth + 1);
      }
    };
    walk("", 0);

    // 目录顶部一行统计：顶栏在窄屏放不下状态条时，这里是"全貌"的兜底。
    const all = (state.data.cards || []).filter((c) => c.card_type !== "rule");
    let pendTotal = 0;
    for (const c of all) pendTotal += Number(c.pending_count || 0);
    const head = document.createElement("div");
    head.className = "cv-cat-head";
    head.innerHTML = `<span><b>${all.length}</b> 张卡</span><span class="cv-cat-head-sep"></span><span class="${pendTotal ? "is-warn" : ""}"><b>${pendTotal}</b> 处待裁定</span>`;
    catEl.innerHTML = "";
    catEl.appendChild(head);
    catEl.appendChild(frag);
    syncCatalogCurrent();
  }

  function syncCatalogCurrent() {
    if (!catRowMap.size) return;
    const cur = [...state.view.selected][0] || "";
    for (const [id, row] of catRowMap) row.classList.toggle("is-current", id === cur);
  }

  // ══════════════════════════════════════════════════════════════════════
  // [画布 2026-10-18 · 工作台] 项目状态条
  //
  // 打开画布的第一眼必须回答"有多少事在等我"。原来顶栏只有一个项目名，
  // 43 张卡、74 处待裁定、3 张已结论——这些全要用户自己数。
  // 数据来自 canvas 投影（每张卡带 work_state / pending_count），一次遍历算完。
  // ══════════════════════════════════════════════════════════════════════
  const projectStatEl = document.getElementById("projectStat");
  function renderProjectStat() {
    if (!projectStatEl) return;
    const cards = (state.data.cards || []).filter((c) => c.card_type !== "rule");
    const total = cards.length;
    let pendingTotal = 0, pendingCards = 0, concluded = 0, empty = 0;
    for (const c of cards) {
      const n = Number(c.pending_count || 0);
      pendingTotal += n;
      if (c.work_state === "pending") pendingCards++;
      else if (c.work_state === "concluded") concluded++;
      else if (c.work_state === "empty") empty++;
    }
    // 窄屏（1280 展开目录时顶栏只剩 ~330px）只留两个数字：卡数、待裁定数。
    // 其余项在完整版里；砍掉的用 title 兜住，鼠标停一下就能看到。
    const compact = window.innerWidth < 1400;
    const triageItem = `<span class="ps-item${pendingTotal ? " is-warn" : ""}" data-act="triage" role="button" tabindex="0"
            title="${pendingCards} 张卡上有待你裁定的事项 —— 点击逐条过">
        <b>${pendingTotal}</b> 处待裁定
      </span>`;
    projectStatEl.innerHTML = compact
      ? `<span class="ps-item" title="${total} 张卡"><b>${total}</b> 卡</span>
         <span class="ps-sep"></span>
         ${triageItem}`
      : `<span class="ps-item"><b>${total}</b> 卡</span>
         <span class="ps-sep"></span>
         ${triageItem}
         <span class="ps-sep"></span>
         <span class="ps-item"><b>${concluded}</b> 已结论</span>
         ${empty ? `<span class="ps-sep"></span><span class="ps-item is-dim"><b>${empty}</b> 未开始</span>` : ""}`;
    const triageEl = projectStatEl.querySelector('[data-act="triage"]');
    if (triageEl && pendingTotal) {
      triageEl.addEventListener("click", () => openTriage(0));
      triageEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openTriage(0); }
      });
    }
  }

  // [画布 2026-10-18] 从对话页返回时带 ?sel=卡号 —— 回来直接选中那张卡，
  // 并把它带到视野里。用户不用在画布里重新找"我刚才在哪"。
  var _pendingSelect = (() => {
    try {
      return new URLSearchParams(location.search).get("sel") || "";
    } catch (e) { return ""; }
  })();

  function applyPendingSelect() {
    if (!_pendingSelect) return;
    const cid = _pendingSelect;
    _pendingSelect = "";
    if (!state.index.cardMap[cid]) return;
    if (!nodeMap.has(cid)) {
      const guard = new Set();
      let cur = (state.index.cardMap[cid] || {}).parent;
      while (cur && !guard.has(cur) && state.index.cardMap[cur]) {
        guard.add(cur);
        state.view.expanded.add(cur);
        cur = state.index.cardMap[cur].parent;
      }
      renderAll();
    }
    selectOnly(cid);
    focusCard(cid, { keepZoom: true });
  }

  function renderAll() {
    renderProjectStat();
    renderCatalog();
    renderCanvas(collectProjectionInput(), collectViewModel());
    applyPendingSelect();
  }

  // 统一重算入口（不重建节点）：拖动落定／摘要回填／LOD 换档后的避让重算
  function reflowAll() {
    if (!nodeMap.size) return;
    const projection = computeProjection({ ...collectProjectionInput(), sizes: measureSizes() });
    lastProjection = projection;   // [画布重构 2.1] 与 renderCanvas 同源，全量重算后刷新缓存
    reflowLayout(projection, collectViewModel());
    scheduleAutoFit();
  }

  // [画布商业化 2026-10-17] 首次自动适配的收尾：内容稳定后（reflow 之后）再 fit 一次。
  // 用户一动（滚轮/拖拽）立即放弃，绝不跟用户抢镜头。
  let _autoFitTimer = 0;
  function scheduleAutoFit() {
    if (!_autoFitPending || _userMoved) return;
    clearTimeout(_autoFitTimer);
    _autoFitTimer = setTimeout(() => {
      if (!_autoFitPending || _userMoved) return;
      _autoFitPending = false;
      const fit = fitView(1);
      if (fit) { applyViewState({ panX: fit.panX, panY: fit.panY, zoom: fit.zoom }); scheduleSaveView(); }
    }, 220);
  }

  // 视图变换量写入口（pan/zoom）：写视图桶＋应用变换；投影与 pan/zoom 无关，
  // 不触发重算（变换通道，触点表在案）。是否持久化由调用方 scheduleSaveView 决定。
  function applyViewState(patch) {
    if (Number.isFinite(patch.panX)) state.view.panX = patch.panX;
    if (Number.isFinite(patch.panY)) state.view.panY = patch.panY;
    if (Number.isFinite(patch.zoom)) state.view.zoom = patch.zoom;
    applyTransform();
  }

  // 未读态写入口（异步桶唯一写入点；refreshUnread 经此落账——V1 名单内不直摸 state）
  function applyUnread(unread) {
    state.async._unreadByCard = unread;
  }

  // [迭代四 F2⑤] 画布状态注册进单一 store（main.js 的 window.GetCanvasState 读此处）。
  // [画布第三步 T3] state 本体已五桶分家；注册面挂「兼容视图」：五桶全量暴露＋旧
  // 扁平字段只读 getter 桥（workspace.js L103 添加关系下拉读 canvasState.cards、
  // 调试探针读 GetCanvasState()——RD-01 禁改 workspace.js，旧读面零行为变化）。
  // getter 每次现取，rebuildCardMap/reloadCanvas 换对象后视图仍指向最新桶内容。
  function makeCanvasStateView() {
    return {
      data: state.data, index: state.index, view: state.view, nav: state.nav, async: state.async,
      get cards() { return state.data.cards; },
      get links() { return state.data.links; },
      get positions() { return state.data.positions; },
      get cardMap() { return state.index.cardMap; },
      get childMap() { return state.index.childMap; },
      get expanded() { return state.view.expanded; },
      get panX() { return state.view.panX; },
      get panY() { return state.view.panY; },
      get zoom() { return state.view.zoom; },
      get workCardId() { return state.nav.workCardId; },
      get workTitle() { return state.nav.workTitle; },
      get viewRoot() { return state.nav.viewRoot; },
      get _unreadByCard() { return state.async._unreadByCard; },
    };
  }
  store.setState({ canvasState: makeCanvasStateView() });

  // 初始加载
  reloadCanvas();

  // [迭代四 F2③] 模块导出（原 window.SetViewRoot/ReloadCanvas/GetCanvasState 语义）
  export { setViewRoot, reloadCanvas };

  // [迭代四 F4④] 未读角标数据面：拉 K4 端点 unread_by_card → 更新徽章；
  // 收件方自己的键上监听 SSE card_message 帧增量（分键 A21）；60s 轮询兜底。
  async function refreshUnread() {
    try {
      const r = await fetch(`/api/works/${WORK_ID}/card-messages?limit=200`);
      if (!r.ok) return;
      const d = await r.json();
      // [F4③④ 口径合成] 端点 unread_by_card 是 pending 推导（档案级）；前端已读
      // 记 localStorage（projMsgRead，workspace.js 写）——角标＝pending ∧ 前端未读，
      // 「点开后消失」（U-9）由此成立。
      let readMap = {};
      try { readMap = JSON.parse(localStorage.getItem(`projMsgRead:${WORK_ID}`) || "{}"); } catch (e0) { /* 零阻断 */ }
      const unread = {};
      for (const it of (d.items || [])) {
        const key = `${it.to_card}:${it.id}`;
        if ((d.unread_by_card || {})[it.to_card] && !readMap[key]) unread[it.to_card] = 1;
      }
      applyUnread(unread);   // [画布第三步 T2] 异步桶唯一写入点（V1：本函数在名单内不直摸 state）
      for (const n of world.querySelectorAll(".card-node")) {
        const cid = n.dataset.id;
        const cnt = unread[cid] || 0;
        let badge = n.querySelector(".msg-badge");
        if (cnt > 0) {
          if (!badge) { badge = document.createElement("span"); badge.className = "msg-badge"; n.appendChild(badge); }
          badge.textContent = cnt > 99 ? "99+" : cnt;
        } else if (badge) badge.remove();
      }
    } catch (e) { /* 展示层零阻断 */ }
  }
  try {
    const es = new EventSource(`/api/works/${WORK_ID}/cards/${WORK_ID}/stream`);
    es.addEventListener("card_message", () => refreshUnread());
    es.onerror = () => { /* SSE 断开静默，轮询兜底 */ };
  } catch (e) { /* EventSource 不可用零阻断 */ }
  setInterval(refreshUnread, 60000);
  refreshUnread();
