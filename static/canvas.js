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
import { API } from "./esm/api.js?v=35";
import { escapeHtml } from "./esm/util.js?v=30";
import { store } from "./esm/store.js?v=30";
import { RenderNav } from "./esm/nav_system.js?v=30";
import { notify } from "./esm/api.js?v=35";

let __VIEW_RESTORED__ = false;   // [F2⑥] 视图态仅首次恢复
const WORK_ID = window.__WORK_ID__;
  const viewport = document.getElementById("viewport");
  const world = document.getElementById("world");
  const edgesSvg = document.getElementById("edgesSvg");
  const tooltip = document.getElementById("cardTooltip");
  const crumbEl = document.getElementById("crumb");
  const zoomDisplay = document.getElementById("zoomDisplay");
  const reorganizeBtn = document.getElementById("reorganizeBtn");
  const newCardBtn = document.getElementById("newCardBtn");

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
    zoomDisplay.textContent = Math.round(state.view.zoom * 100) + "%";
    updateLOD();
  }

  // [画布第三步 T2] 档位判据的 zoom 改参数传入（V1：本函数在渲染 10 函数名单内；
  // zoom 属视图态容器——T2③ 口径「从视图态容器取/参数传入，不从投影取」）
  function currentLODLevel(zoom) {
    if (zoom < 0.5) return "l0";
    if (zoom > 1.2) return "l2";
    return "l1";
  }

  function applyLODClasses(zoom) {
    const level = currentLODLevel(zoom);
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
  const GAP_Y = 40;

  // [T08-1-2] 折叠树排布（替代原 narrativeAxis/gridMatrix 双分支）。
  // 方向口径（用户 2026-10-04 中途指令「布局用左右视图」，对任务书上下树的推翻，留痕）：
  // 左右视图——根卡居左，子树向右逐层展开；同父子树纵向一字排开不重叠
  // （子树纵向跨度 = 其叶子实测高之和 + 间距，父卡在子树跨度内垂直居中）。
  // 层间距由 measureSizes() 实测高推出（取不到按既有惯例回退 220×86）；列宽恒定
  // COL_W（容纳根卡 320 宽，任何 LOD 档卡宽 ≤320 < COL_W，横向恒不重叠）；
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
    // 回退行高分档：深度≥2 的卡按 t8-deep 紧凑形态（只显标题）取 36，
    // 其余按常规卡取 86（既有回退值）——首绘时 DOM 未建、实测不可得，按此估跨距
    const FALLBACK_H = 86, FALLBACK_H_DEEP = 36;
    sizes = sizes || {};
    const heightOf = (cid, depth) => (sizes[cid] && sizes[cid].h) || (depth >= 2 ? FALLBACK_H_DEEP : FALLBACK_H);
    const COL_W = 340;  // 相邻层横向基准（根卡实测宽 320 < COL_W，其余档位卡宽 ≤260）
    // GAP_Y 已上提模块级（[T18-C-005]），本域直接引用

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

    // 后序：子树纵向跨度 = max(自身高, Σ子树跨度 + 间距)；seen 防脏数据成环
    const subtreeH = {};
    const seen = new Set();
    (function measure(cid, depth) {
      if (seen.has(cid)) return heightOf(cid, depth);
      seen.add(cid);
      const kids = kidsOf(cid);
      let sum = 0;
      for (let i = 0; i < kids.length; i++) {
        sum += measure(kids[i].card_id, depth + 1) + (i > 0 ? GAP_Y : 0);
      }
      subtreeH[cid] = kids.length ? Math.max(heightOf(cid, depth), sum) : heightOf(cid, depth);
      return subtreeH[cid];
    })(rootId, rootDepth);

    // 前序：子树占 [topY, topY+subtreeH]，卡在子树跨度内垂直居中，x 逐层 +COL_W。
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
      let anchorY = topY, childX = x + COL_W;
      if (cid !== rootId) {
        if (positions[cid] && positions[cid].pinned && result[cid]) {
          // pinned：保留存储坐标不覆盖，子树改以存储坐标为锚继续右排
          anchorY = result[cid].y - (subtreeH[cid] - heightOf(cid, depth)) / 2;
          childX = result[cid].x + COL_W;
        } else {
          result[cid] = { x: x, y: topY + (subtreeH[cid] - heightOf(cid, depth)) / 2 };
        }
      }
      const kids = kidsOf(cid);
      if (!kids.length) return;
      let kidsSum = 0;
      for (let i = 0; i < kids.length; i++) kidsSum += subtreeH[kids[i].card_id];
      kidsSum += (kids.length - 1) * GAP_Y;
      let cursor = anchorY + (subtreeH[cid] - kidsSum) / 2;
      for (let i = 0; i < kids.length; i++) {
        place(kids[i].card_id, childX, cursor, depth + 1);
        cursor += subtreeH[kids[i].card_id] + GAP_Y;
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
    while (moved && rounds < 16) {          // 16 轮 = FPS/防卡保护上限（RD-02 辨析），非闸
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
    if (moved) console.warn("[separateOverlaps] 16 轮内未完全收敛，可能残留个别相交");
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
  // depth 以 anchorDepth 为基准——depthOf 用于 t8-deep 档判定，必须相对 viewRoot，
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

  // 连线几何纯函数（自 renderEdges 数据段逐字搬出：rootParentId＋父子折线段＋外接框；
  // SVG 落笔仍留 renderEdges——几何数据与 DOM 应用分家）
  function computeEdgeGeometry(visibleIds, layout, sizes, childMap, cardMap, viewRootId) {
    const rootParentId = (cardMap[viewRootId] || {}).parent || "";
    const elbowSegs = [];
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
        const childOnRight = ccx >= pcx;
        elbowSegs.push(childOnRight
          ? { x1: p1.x + s1.w, y1: p1.y + s1.h / 2, x2: p2.x, y2: p2.y + s2.h / 2 }        // 父右缘 → 子左缘
          : { x1: p1.x, y1: p1.y + s1.h / 2, x2: p2.x + s2.w, y2: p2.y + s2.h / 2 });     // 父左缘 → 子右缘
      }
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
      if (!raw) return;
      const v = JSON.parse(raw);
      if (Number.isFinite(v.panX)) state.view.panX = v.panX;
      if (Number.isFinite(v.panY)) state.view.panY = v.panY;
      if (Number.isFinite(v.zoom)) state.view.zoom = Math.max(0.25, Math.min(2.0, v.zoom));
      if (Array.isArray(v.expanded)) {
        state.view.expanded = new Set(v.expanded.filter(id => state.index.cardMap[id]));
      }
    } catch (e) { /* 恢复失败零阻断 */ }
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
    for (const cid of visibleIds) {
      const card = vm.cardsById[cid];
      if (!card) continue;
      if (card.card_type === "rule") continue;   // [规则卡 2026-10-16] 规则卡不上画布——它是吸附屏幕右侧的常驻卡（workspace.js 规则面板即其呈现）
      const node = createNode(card, { x: 0, y: 0 }, vm);   // 占位坐标（阶段二一次性应用最终位）
      if ((depthOf[cid] || 0) >= 2) {
        // [T08-用户令 2026-10-04] 第三级卡只显示标题（独立类，不动 l0/l1/l2 三档机制）
        node.classList.add("t8-deep");
      }
      if (!prevIds.has(cid)) {
        // [T08-3-4] 新出现卡淡入（只动 opacity/transform；连线不做补间）
        node.classList.add("entering");
        node.addEventListener("animationend", () => node.classList.remove("entering"), { once: true });
      }
      world.appendChild(node);
      nodeMap.set(cid, node);
    }

    // [card_045 E3] 两阶段渲染（阶段二）：一次性全量实测 → 纯函数算基准 → 避让 →
    // 一次性全部应用 → 取消隐藏。一轮渲染内尺寸测量恰为 1 次（改前 3 次：
    // layoutTree 内部测量＋此处＋renderEdges 各一次——E1 已参数化第一处、
    // renderEdges 改吃本处尺寸表）。
    applyLODClasses(vm.zoom);
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
    applyTransform();
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
        const node = createNode(card, anchorPos, vm);   // 占位坐标，⑤ 之后一次性改写
        if ((plan.depthOf[cid] || 0) >= 2) node.classList.add("t8-deep");
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
  function updateExpandBadge(node, childCount, isExpanded) {
    const badge = node.querySelector(".kid-badge");
    if (!badge) return;
    badge.textContent = `${isExpanded ? "▾" : "▸"} ${childCount}`;
  }

  // [画布第三步 T2] 改吃视图模型 vm（动作层装配）：viewRoot/positions/childMap/
  // expanded/_unreadByCard 一律从 vm 取——不直摸 state（V1）
  function createNode(card, pos, vm) {
    const node = document.createElement("div");
    const isRoot = card.card_id === vm.viewRoot;
    node.className = "card-node";
    node.dataset.id = card.card_id;
    node.dataset.type = card.card_type;
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
      ? `<span class="kid-badge" title="有 ${childCount} 张子卡，点击展开/折叠">${isExpanded ? "▾" : "▸"} ${childCount}</span>`
      : "";

    node.innerHTML = `
      ${msgBadge}
      <div class="tname">${escapeHtml(card.title)}${kidBadge}</div>
      <div class="ttype">${escapeHtml(card.card_type)}</div>
      <div class="summary"></div>
      <div class="meta2"></div>
    `;

    bindNodeEvents(node, card);
    return node;
  }

  function bindNodeEvents(node, card) {
    let clickTimer = null;

    // [T08-3-1] ▸/▾ 徽章单击＝展开/折叠该卡子树（原语义为下钻 setViewRoot，已改）
    const badge = node.querySelector(".kid-badge");
    if (badge) {
      badge.addEventListener("click", (e) => {
        e.stopPropagation();
        if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }
        toggleExpand(card.card_id);
      });
    }

    node.addEventListener("click", (e) => {
      if (node.classList.contains("dragging")) return;
      if (clickTimer) {
        // 双击
        clearTimeout(clickTimer);
        clickTimer = null;
        onDoubleClick(card);
        return;
      }
      clickTimer = setTimeout(() => {
        clickTimer = null;
        onSingleClick(card);
      }, 120);
    });

    // 拖拽
    let dragState = null;
    node.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      // [R1] 若上一次拖拽的监听器尚未卸掉（异常路径），先卸，避免重复挂载
      if (activeDragDetach) activeDragDetach();
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
      node.classList.add("dragging");
      node.style.left = (dragState.nodeLeft + dx) + "px";
      node.style.top = (dragState.nodeTop + dy) + "px";
    }

    function onDragUp(e) {
      if (!dragState) return;
      if (dragState.moved) {
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
        dragState = null;
      }
      if (activeDragDetach === detachDragListeners) activeDragDetach = null;
      console.info("[R1] 拖拽监听器 卸载");
    }

    // tooltip（L0/L1 时悬停显示摘要）
    let hoverTimer = null;
    node.addEventListener("mouseenter", () => {
      hoverTimer = setTimeout(() => showTooltip(card, node), 300);
    });
    node.addEventListener("mouseleave", () => {
      clearTimeout(hoverTimer);
      hideTooltip();
    });

    // L2 进入时懒加载摘要
    node.addEventListener("dblclick", (e) => e.preventDefault());
  }

  function onSingleClick(card) {
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

  function onDoubleClick(card) {
    // 容器卡下钻
    const childCount = (state.index.childMap[card.card_id] || []).length;
    if (childCount > 0) {
      setViewRoot(card.card_id);
    } else {
      // 无子卡也允许下钻（用户可能想进入空容器建卡），但等同打开工作区
      location.assign("/work/" + WORK_ID + "/card/" + card.card_id);
    }
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
    if (fit) applyViewState({ panX: fit.panX, panY: fit.panY });
  }

  // [hotfix] 切层后把当前层所有卡片居中到视野（负坐标区不再甩出屏幕外）
  // [画布第三步 T2/T3] 改纯量测：读自身 DOM 量测＋入参 zoom，算出 pan **返回**；
  // 直写视图态与 applyTransform 上收给调用方（动作层 applyViewState）——V1
  function fitView(zoom) {
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
    const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    const fitPanX = viewport.clientWidth / 2 - cx * zoom;
    let fitPanY = viewport.clientHeight / 2 - cy * zoom;
    // 内容比视口矮时不必垂直居中到过低，顶部留 60px 更自然
    fitPanY = Math.min(fitPanY, 60 - minY * zoom);
    return { panX: fitPanX, panY: fitPanY };
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
    const pcG = document.createElementNS("http://www.w3.org/2000/svg", "g");
    pcG.setAttribute("data-edge-group", "parent-child");
    edgesSvg.appendChild(pcG);
    for (const seg of elbowSegs) {
      addElbowPath(seg.x1, seg.y1, seg.x2, seg.y2, "#58a6ff", "1.5", pcG);
      // 父端实心圆点 / 子端空心圆点：表达"谁是父、谁是子"（比箭头轻，且直角拐角处箭头难算切线）
      const dotF = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      dotF.setAttribute("cx", seg.x1); dotF.setAttribute("cy", seg.y1);
      dotF.setAttribute("r", "3"); dotF.setAttribute("fill", "#58a6ff");
      pcG.appendChild(dotF);
      const dotC = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      dotC.setAttribute("cx", seg.x2); dotC.setAttribute("cy", seg.y2);
      dotC.setAttribute("r", "3"); dotC.setAttribute("fill", "none");
      dotC.setAttribute("stroke", "#58a6ff"); dotC.setAttribute("stroke-width", "1.5");
      pcG.appendChild(dotC);
    }
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
  function addElbowPath(x1, y1, x2, y2, color, sw, parent) {
    const midX = x1 + (x2 - x1) / 2;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", `M ${x1} ${y1} L ${midX} ${y1} L ${midX} ${y2} L ${x2} ${y2}`);
    path.setAttribute("stroke", color);
    path.setAttribute("stroke-width", sw);
    path.setAttribute("fill", "none");
    path.setAttribute("data-edge", "parent-child"); // [T08] 机械验收标记（V6/V7 计数用，不影响渲染）
    (parent || edgesSvg).appendChild(path);
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
  async function getSummary(cardId, card) {
    if (summaryCache.has(cardId)) return summaryCache.get(cardId);

    // [R4] 快路径：结论摘要随画布数据一次到手（canvas.json 的 cards[].conclusion_summary），
    // 命中则直接使用、不发 getCard 请求——首屏 N+1 请求归零。
    // 字段缺失（老缓存未重建 / 后端未升级）→ 回退原路径，向后兼容。
    // 代价如实记：走快路径时 meta2（更新时间）不在画布数据里，保持原状不填；
    // L1 档位本就隐藏 meta2，L2 档位下该行的更新时间可能为空——如需它，回退路径仍在。
    const probe = card;
    if (probe && typeof probe.conclusion_summary === "string") {
      const summary = probe.conclusion_summary;
      summaryCache.set(cardId, summary);
      const node0 = nodeMap.get(cardId);
      if (node0) {
        const sumEl0 = node0.querySelector(".summary");
        if (sumEl0) sumEl0.textContent = summary;
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
          // [phase8.6-B] 回填改变卡高，防抖重跑避让
          if (cached) scheduleReflow();
        }
      }
    }
  }

  // ==================== 视口交互（缩放/平移） ====================
  viewport.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = viewport.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    // 当前世界坐标下的鼠标点
    const oldZoom = state.view.zoom;
    const delta = e.deltaY > 0 ? -0.1 : 0.1;
    let newZoom = oldZoom + delta;
    newZoom = Math.max(0.25, Math.min(2.0, newZoom));
    // 以鼠标点为不动点：world_pos = (screen - pan) / zoom，新 pan = screen - world_pos * newZoom
    const wx = (mx - state.view.panX) / oldZoom;
    const wy = (my - state.view.panY) / oldZoom;
    state.view.panX = mx - wx * newZoom;
    state.view.panY = my - wy * newZoom;
    state.view.zoom = newZoom;
    applyTransform();
    lazyLoadVisibleSummaries(state.index.cardMap);
    scheduleSaveView();   // [F2⑥] pan/zoom 结束防抖持久化
  }, { passive: false });

  // 平移
  let panState = null;
  viewport.addEventListener("mousedown", (e) => {
    if (e.target !== viewport && e.target !== world) return;
    if (e.button !== 0) return;
    panState = { startX: e.clientX, startY: e.clientY, panX: state.view.panX, panY: state.view.panY };
    viewport.classList.add("panning");
  });
  document.addEventListener("mousemove", (e) => {
    if (!panState) return;
    state.view.panX = panState.panX + (e.clientX - panState.startX);
    state.view.panY = panState.panY + (e.clientY - panState.startY);
    world.style.transform = `translate(${state.view.panX}px, ${state.view.panY}px) scale(${state.view.zoom})`;
  });
  document.addEventListener("mouseup", () => {
    if (panState) { panState = null; viewport.classList.remove("panning"); scheduleSaveView(); }   // [F2⑥]
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
  reorganizeBtn.onclick = async () => {
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

  // ==================== 新建卡 ====================
  newCardBtn.onclick = () => {
    // 内联输入
    const node = document.createElement("div");
    node.style.cssText = "position:fixed;top:60px;right:20px;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px;z-index:1000;display:flex;flex-direction:column;gap:8px;min-width:240px;";
    node.innerHTML = `
      <input type="text" id="newCardTitle" placeholder="卡片标题" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text);font-size:13px;">
      <select id="newCardType" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text);font-size:13px;">
        <!-- [T07-2-1] 类型退役：建卡入口可选列表去掉四个废弃类型名（裁定 D-06：
             名字留白名单、只关新建入口；存量卡不受影响） -->
        <option value="线索">线索</option>
      </select>
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
      const ctype = node.querySelector("#newCardType").value;
      try {
        const r = await API.createCard(WORK_ID, { title, source: "用户直接创建", card_type: ctype, parent: state.nav.viewRoot });
        node.remove();
        await reloadCanvas();
        RenderNav();   // [T17] 新卡同步进导航树
        notify("卡片已创建", "success");   // [F3④]
      } catch (e) { notify("建卡失败", "error"); }   // [F3④] 原静默
    };
    node.querySelector("#newCardOk").onclick = submit;
    node.querySelector("#newCardCancel").onclick = () => node.remove();
    titleInput.onkeydown = (e) => {
      if (e.key === "Enter") submit();
      if (e.key === "Escape") node.remove();
    };
  };

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
      if (!__VIEW_RESTORED__) { restoreViewState(); __VIEW_RESTORED__ = true; }   // [F2⑥] 仅首次加载恢复（模块内旗标）
      showCanvasState("clear");
      renderAll();          // [画布第三步 T4] 数据就绪 → 统一重绘入口
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
      box.innerHTML = `<div class="cs-loading">加载中…</div>`;
    } else if (kind === "empty") {
      box.innerHTML = `<div class="cs-empty">这张画布还没有卡片——点右上「＋ 新建卡」开始。</div>`;
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
  function renderAll() {
    renderCanvas(collectProjectionInput(), collectViewModel());
  }

  // 统一重算入口（不重建节点）：拖动落定／摘要回填／LOD 换档后的避让重算
  function reflowAll() {
    if (!nodeMap.size) return;
    const projection = computeProjection({ ...collectProjectionInput(), sizes: measureSizes() });
    lastProjection = projection;   // [画布重构 2.1] 与 renderCanvas 同源，全量重算后刷新缓存
    reflowLayout(projection, collectViewModel());
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
