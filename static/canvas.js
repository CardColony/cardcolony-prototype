/* canvas.js - 画布核心：视口变换、布局引擎、LOD、拖拽。
 * 迭代四 F2③：IIFE → ES module（module 作用域＝原闭包隔离）；escapeHtml 收编 esm/util.js；
 * API 经 esm/api.js 静态 import；跨文件（Workspace/RenderNav）由 main.js 装配序保证。 */
import { API } from "./esm/api.js?v=11";
import { escapeHtml } from "./esm/util.js?v=11";
import { store } from "./esm/store.js?v=11";
import { Workspace, RenderNav } from "./workspace.js?v=11";
import { notify } from "./esm/api.js?v=11";

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

  // 画布状态
  let state = {
    cards: [],            // [{card_id,title,card_type,parent,created_at}]
    relations: [],        // [{source,target,type}]
    positions: {},        // {card_id:{x,y,pinned}}
    workCardId: "",       // 剧本卡 id
    workTitle: "",
    viewRoot: "",         // 当前视图根（默认 = workCardId）
    cardMap: {},          // card_id -> card
    childMap: {},         // parent_id -> [card_id]
    // [T08-1-1] 已展开卡集合（纯内存态）：默认空集＝全折叠；不写任何持久化载体（红线1/6），刷新即回默认
    expanded: new Set(),  // Set<card_id>
  };

  // 视口变换
  let panX = 80, panY = 40, zoom = 1.0;

  // 懒加载缓存：card_id -> 结论摘要（会话内）
  const summaryCache = new Map();
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
    world.style.transform = `translate(${panX}px, ${panY}px) scale(${zoom})`;
    zoomDisplay.textContent = Math.round(zoom * 100) + "%";
    updateLOD();
  }

  function currentLODLevel() {
    if (zoom < 0.5) return "l0";
    if (zoom > 1.2) return "l2";
    return "l1";
  }

  function applyLODClasses() {
    const level = currentLODLevel();
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
    const relGroup = edgesSvg.querySelector('[data-edge-group="relations"]');
    if (pcGroup) pcGroup.style.opacity = isL0 ? "0.55" : "1";
    if (relGroup) relGroup.style.display = isL0 ? "none" : "";
    return level;
  }

  // [phase8.6-B] 换档穿越（L0↔L1↔L2）后重跑 实测→避让→回写→renderEdges，100ms 防抖
  let lastLODLevel = null;
  function updateLOD() {
    const level = applyLODClasses();
    if (lastLODLevel !== null && level !== lastLODLevel) {
      scheduleReflow();
    }
    lastLODLevel = level;
  }

  let reflowTimer = null;
  function scheduleReflow() {
    if (reflowTimer) clearTimeout(reflowTimer);
    reflowTimer = setTimeout(reflowLayout, 100);
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
  function computeLayout(viewRootId, cards, relations, positions, expanded, sizes) {
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

  function layoutNarrativeAxis(viewRootId, children, relations, positions, result) {
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
    for (const r of relations) {
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

    // 节点排序：relations 中 type==先后 做拓扑序
    const plotIds = new Set(plots.map(c => c.card_id));
    const edges = relations.filter(r => r.type === "先后" && plotIds.has(r.source) && plotIds.has(r.target));
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
      for (const r of relations) {
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
  // 只写内存 result，不读不写任何持久化载体（红线1/6）；pinned 卡由 computeLayout
  // 步骤1写入 result、此处不覆盖（保留规则），其子树以存储坐标为锚继续右排。
  function layoutTree(viewRootId, childMap, positions, expanded, result, sizes) {
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

    // 布局树的「子边」：viewRoot 直接子卡恒在；其余卡的子卡仅当该卡 ∈ expanded
    // （与 T08-1-4 可见集同一规则，保证布局覆盖恰为可见集）
    const kidsOf = (cid) =>
      (cid === viewRootId || expanded.has(cid)) ? (childMap[cid] || []) : [];

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
    })(viewRootId, 0);

    // 前序：子树占 [topY, topY+subtreeH]，卡在子树跨度内垂直居中，x 逐层 +COL_W。
    // 根卡子树跨度以 y=0（根卡纵位）为中心上下分半。
    seen.clear();
    (function place(cid, x, topY, depth) {
      if (seen.has(cid)) return;
      seen.add(cid);
      let anchorY = topY, childX = x + COL_W;
      if (cid !== viewRootId) {
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
    })(viewRootId, 0, -subtreeH[viewRootId] / 2, 0);
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
  // pinned 三档语义（写死）：①pinned 永不被移动；②free–pinned 相交 → 推 free 全量 MTD；
  // ③free–free 相交 → 两卡各推半量；④double pinned → 不动 + console.warn
  // （不新增由 C3 resolveDrop 保证；历史遗留交「整理画布」按钮清）。
  // 诚实口径（RD-07）：分离尽力——16 轮内收敛则零相交，16 轮未收敛 warn 留痕。
  // 16 轮上限是前端本地有限步 FPS/防卡保护，不是对模型调用/轮次的闸（RD-02 辨析）。
  // 行高回退 86 对齐 layoutTree.FALLBACK_H；深度≥2 的 36 档在首绘 DOM 未建时本签名不可知，
  // 86 为偏高保守估计（只多让不少让），实测尺寸回填后经 scheduleReflow→reflow 复用本函数再收敛。
  function separateOverlaps(layout, sizes, pinnedIds, GAP = GAP_Y) {
    const pinned = pinnedIds || new Set();
    const out = {};
    for (const cid in layout) out[cid] = { x: layout[cid].x, y: layout[cid].y };
    const ids = Object.keys(out);
    const wh = (cid) => ({
      w: (sizes[cid] && sizes[cid].w) || 220,
      h: (sizes[cid] && sizes[cid].h) || 86,
    });
    let moved = true;
    let rounds = 0;
    let dpWarned = false;
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
          const aP = pinned.has(a), bP = pinned.has(b);
          if (aP && bP) {
            if (!dpWarned) {
              console.warn("[separateOverlaps] 双 pinned 相交（历史遗留，交「整理画布」清）:", a, b);
              dpWarned = true;
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

  function pinnedIdSet() {
    const s = new Set();
    for (const cid in state.positions) {
      if (state.positions[cid] && state.positions[cid].pinned) s.add(cid);
    }
    return s;
  }

  // [迭代四 F2⑥] 视图态持久化：localStorage["projView:{work_id}"]（原纯内存态
  // expanded ＋ pan/zoom）。写入＝wheel/pan 结束与展开收起后防抖（照 positions
  // 持久化防抖先例）；读取＝画布初始化（reloadCanvas 内、renderCanvas 之前）。
  function saveViewState() {
    try {
      localStorage.setItem(`projView:${WORK_ID}`, JSON.stringify({
        panX, panY, zoom, expanded: [...state.expanded],
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
      if (Number.isFinite(v.panX)) panX = v.panX;
      if (Number.isFinite(v.panY)) panY = v.panY;
      if (Number.isFinite(v.zoom)) zoom = Math.max(0.25, Math.min(2.0, v.zoom));
      if (Array.isArray(v.expanded)) {
        state.expanded = new Set(v.expanded.filter(id => state.cardMap[id]));
      }
    } catch (e) { /* 恢复失败零阻断 */ }
  }

  // [phase8.6-B] 重跑：实测尺寸 → 避让 → 回写节点坐标 → 重画连线
  // [card_045 E3] 快路径与全量重绘走**同一计算链**（实测尺寸 → computeLayout 纯函数
  // 现算基准 → separateOverlaps 避让 → 一次性应用），唯一区别＝不重建节点（节点已在，
  // 只更新位置）。不再读任何快照——拖动落定的存储坐标即刻参与下一次重算（病灶 1：
  // 「防抖重算用旧基准快照覆盖用户刚放下的位置」在此拆除）。
  function reflowLayout() {
    if (!nodeMap.size) return;
    const sizes = measureSizes();
    const layout = computeLayout(state.viewRoot, state.cards, state.relations,
                                 state.positions, state.expanded, sizes);
    const adjusted = separateOverlaps(layout, sizes, pinnedIdSet());
    for (const [cid, node] of nodeMap) {
      const p = adjusted[cid];
      if (!p) continue;
      node.style.left = p.x + "px";
      node.style.top = p.y + "px";
    }
    renderEdges(new Set(nodeMap.keys()), adjusted, sizes);
  }

  // ==================== 渲染 ====================
  function rebuildCardMap() {
    state.cardMap = {};
    state.childMap = {};
    for (const c of state.cards) {
      state.cardMap[c.card_id] = c;
      const p = c.parent || "";
      if (!state.childMap[p]) state.childMap[p] = [];
      state.childMap[p].push(c);
    }
  }

  function renderBreadcrumb() {
    // 从 viewRoot 沿 parent 上溯到剧本卡
    const chain = [];
    let cur = state.viewRoot;
    const guard = new Set();
    while (cur && state.cardMap[cur] && !guard.has(cur)) {
      guard.add(cur);
      chain.unshift(state.cardMap[cur]);
      cur = state.cardMap[cur].parent;
    }
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

  function renderCanvas() {
    // [T08-3-4] 记录重绘前已在场的卡：重绘后仅对新增卡做淡入
    const prevIds = new Set(nodeMap.keys());
    // 清除旧节点（保留 svg）
    for (const [cid, node] of nodeMap) node.remove();
    nodeMap.clear();

    // [T08-1-4] 可见集（规则写死，不动点计算）：
    // ① viewRoot 恒在集合内；② viewRoot 的直接子卡恒在集合内；
    // ③ 对集合内任意卡 C，若 C ∈ state.expanded，则 C 的全部直接子卡加入集合；
    // ④ 重复③直至集合不再增长。
    // （原两层模型「viewRoot + 直属子卡」由本构造替代；展开态只存内存，刷新即回全折叠）
    // depthOf＝相对视图根深度：根=0，直接子卡=1，更深层=2+（供第三级卡只显标题挂类）
    const visibleIds = new Set([state.viewRoot]);
    const depthOf = {};
    depthOf[state.viewRoot] = 0;
    for (const child of (state.childMap[state.viewRoot] || [])) {
      visibleIds.add(child.card_id);
      depthOf[child.card_id] = 1;
    }
    let grew = true;
    while (grew) {
      grew = false;
      for (const cid of Array.from(visibleIds)) {
        if (!state.expanded.has(cid)) continue;
        for (const child of (state.childMap[cid] || [])) {
          if (!visibleIds.has(child.card_id)) {
            visibleIds.add(child.card_id);
            depthOf[child.card_id] = (depthOf[cid] || 0) + 1;
            grew = true;
          }
        }
      }
    }

    // [card_045 E3] 两阶段渲染（阶段一）：按占位坐标建全部节点，整层**隐藏但占位**。
    // ✱ 纪律性约束（写死）：必须用「隐藏但占位」的方式（visibility——元素仍在布局流、
    // 尺寸可读），不许用「不占位」的方式（display:none——元素不参与布局、量不到尺寸）。
    // 这是沿用既有约定（static/style.css「隐藏的画布保持可测量」条），不是本任务新立的规矩。
    // 改错的具体后果：不会报错、不会异常——只会让测量拿到 0 或拿不到，然后整个
    // 两阶段静默退化成「全用估算值」，而表面上一切正常。
    world.style.visibility = "hidden";
    for (const cid of visibleIds) {
      const card = state.cardMap[cid];
      if (!card) continue;
      const node = createNode(card, { x: 0, y: 0 });   // 占位坐标（阶段二一次性应用最终位）
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
    applyLODClasses();
    const sizes = measureSizes();
    const layout = computeLayout(state.viewRoot, state.cards, state.relations,
                                 state.positions, state.expanded, sizes);
    const adjusted = separateOverlaps(layout, sizes, pinnedIdSet());
    for (const cid of visibleIds) {
      const node = nodeMap.get(cid);
      const p = adjusted[cid];
      if (node && p) {
        node.style.left = p.x + "px";
        node.style.top = p.y + "px";
      }
    }
    world.style.visibility = "";

    renderEdges(visibleIds, adjusted, sizes);
    renderBreadcrumb();
    applyTransform();
    // [phase8.4] 渲染完成即懒加载可见卡摘要（首次进入/下钻/Reload 后均填充）
    lazyLoadVisibleSummaries();
  }

  function createNode(card, pos) {
    const node = document.createElement("div");
    const isRoot = card.card_id === state.viewRoot;
    node.className = "card-node";
    node.dataset.id = card.card_id;
    node.dataset.type = card.card_type;
    node.style.left = pos.x + "px";
    node.style.top = pos.y + "px";
    if (isRoot) node.classList.add("is-root");
    // [迭代四 F4④] 卡间消息角标（左外角 -6px 终值）：未读数取 K4 端点
    // unread_by_card、SSE card_message 帧增量更新（refreshUnread）。
    const unread = (state._unreadByCard || {})[card.card_id] || 0;
    const msgBadge = unread > 0
      ? `<span class="msg-badge">${unread > 99 ? "99+" : unread}</span>`
      : "";
    // [诸葛 2026-10-04 21:52] pinned 视觉标识。
    // 原缺口：拖一下卡就 pinned=true（永久不参与自动避让），但 style.css 里
    // 搜 "pinned" 零命中——画布上看不出来，用户不知道自己什么时候把卡钉死了。
    // 现给钉住的卡挂类，样式在 style.css（右上角小图钉 ＋ 边框色区分）。
    if (state.positions[card.card_id] && state.positions[card.card_id].pinned) {
      node.classList.add("is-pinned");
    }

    const childCount = (state.childMap[card.card_id] || []).length;
    // [T08-3-1/3-5] 徽章语义＝展开/折叠开关：▸（折叠态）↔ ▾（展开态）；N 恒为该卡子卡数
    const isExpanded = state.expanded.has(card.card_id);
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
      const dx = (e.clientX - dragState.startX) / zoom;
      const dy = (e.clientY - dragState.startY) / zoom;
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
        // [card_045 E4] 拖动落定后立刻走统一重算的「不重建节点」分支（reflowLayout）：
        // 显示坐标与连线吃同一份现算结果——不再有快照可读，也不需要。
        // 拖动卡即刻 pinned（E5 语义），重算不会移动它——它停在用户松手的位置。
        reflowLayout();
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
      const hasKids = (state.childMap[card.card_id] || []).length > 0;
      if (hasKids && !state.expanded.has(card.card_id)) {
        toggleExpand(card.card_id);
        return;
      }
    }
    Workspace.open(card.card_id);
  }

  // [T08-用户令] 相对当前视图根的深度：沿 parent 链上溯到 viewRoot 的步数
  // （viewRoot=0，直接子卡=1；上溯不到 viewRoot 的孤儿卡无意义，返回步数即可）
  function depthFromViewRoot(cardId) {
    let d = 0, cur = cardId;
    const guard = new Set();
    while (cur && cur !== state.viewRoot && state.cardMap[cur] && !guard.has(cur)) {
      guard.add(cur);
      cur = state.cardMap[cur].parent;
      d++;
    }
    return d;
  }

  function onDoubleClick(card) {
    // 容器卡下钻
    const childCount = (state.childMap[card.card_id] || []).length;
    if (childCount > 0) {
      setViewRoot(card.card_id);
    } else {
      // 无子卡也允许下钻（用户可能想进入空容器建卡），但等同打开工作区
      Workspace.open(card.card_id);
    }
  }

  // [T08-3-2] 就地展开/折叠：只翻 state.expanded 成员关系后全量重绘。
  // 不调 setViewRoot（视图根不变＝「就地」的全部含义）、不调 fitView（视野不跳）。
  function toggleExpand(cardId) {
    if (state.expanded.has(cardId)) state.expanded.delete(cardId);
    else state.expanded.add(cardId);
    scheduleSaveView();   // [F2⑥] 展开态持久化
    renderCanvas();
  }

  function setViewRoot(newRoot) {
    // [card_045 E7 · L8 位置跨视图共享] 坐标记录是 work 级的：下钻/返回只改
    // 视图根（可见集），任何卡的位置不因视图切换而改变——下钻再返回，位置不变。
    state.viewRoot = newRoot;
    summaryCache.clear();
    // [诸葛 2026-10-04 21:52] 下钻换根时清掉旧展开态。
    // 原缺口：只清 summaryCache，expanded 跟着带去新视图——钻进去后可能有若干卡
    // 莫名已展开（T08 新增 expanded 机制时漏了这一处，属新机制铺开的边角遗漏）。
    state.expanded.clear();
    hideWorkspace();
    renderCanvas();
    fitView();
  }

  // [hotfix] 切层后把当前层所有卡片居中到视野（负坐标区不再甩出屏幕外）
  function fitView() {
    const nodes = [...world.querySelectorAll(".card-node")];
    if (!nodes.length) return;
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
    panX = viewport.clientWidth / 2 - cx * zoom;
    panY = viewport.clientHeight / 2 - cy * zoom;
    // 内容比视口矮时不必垂直居中到过低，顶部留 60px 更自然
    panY = Math.min(panY, 60 - minY * zoom);
    applyTransform();
  }

  function getVisibleIds() {
    const ids = new Set([state.viewRoot]);
    const stack = [state.viewRoot];
    while (stack.length) {
      const id = stack.pop();
      for (const child of (state.childMap[id] || [])) {
        if (!ids.has(child.card_id)) { ids.add(child.card_id); stack.push(child.card_id); }
      }
    }
    return ids;
  }

  // ==================== 关系线渲染 ====================
  // [card_045 E3] sizes 参数化：一轮渲染内只测一次（renderCanvas/reflowLayout 传入）；
  // 省缺时回退现测（外部零散调用兼容）。
  function renderEdges(visibleIds, layout, sizesArg) {
    const sizes = sizesArg || measureSizes();

    // [T08-2-1/2-3] 父子直角折线段先收集（这是一条新增循环，不替换既有 relations 循环）。
    // 锚点（左右视图口径，用户中途指令推翻任务书上下向，留痕）＝父卡右缘中点 (x+w, y+h/2)
    // → 子卡左缘中点 (x, y+h/2)，w/h 取 measureSizes() 实测。
    // 不画跨层线（子卡不可见不补画）；视图根与其自身父卡的线不画（该父卡按可见集规则
    // 不可能在集合内，此处按任务书再显式守一道）。
    const rootParentId = (state.cardMap[state.viewRoot] || {}).parent || "";
    const elbowSegs = [];
    for (const pid in state.childMap) {
      for (const childCard of state.childMap[pid]) {
        const ckid = childCard.card_id;
        if (pid === rootParentId && ckid === state.viewRoot) continue;
        if (!visibleIds.has(pid) || !visibleIds.has(ckid)) continue;
        const p1 = layout[pid], p2 = layout[ckid];
        if (!p1 || !p2) continue;
        const s1 = sizes[pid] || { w: 220, h: 86 };
        const s2 = sizes[ckid] || { w: 220, h: 86 };
        // [诸葛 2026-10-04 21:52] 锚点按相对位置自动选边（原写死"父右缘→子左缘"）。
        // 原写死的问题：子卡被拖到父卡左侧时，线必须从父卡右缘出发再折回左边，
        // 竖段必穿过父卡本体（card_007 §14.4 记为"T08 未处理的组合"）。
        // 现按两卡中心横向相对位置选边：子卡中心在父卡中心右侧走右缘→左缘，反之反过来。
        // 折线路径算法（addElbowPath）不动——midX 本就在两点之间取中，改锚点即可。
        const pcx = p1.x + s1.w / 2, ccx = p2.x + s2.w / 2;
        const childOnRight = ccx >= pcx;
        elbowSegs.push(childOnRight
          ? { x1: p1.x + s1.w, y1: p1.y + s1.h / 2, x2: p2.x, y2: p2.y + s2.h / 2 }        // 父右缘 → 子左缘
          : { x1: p1.x, y1: p1.y + s1.h / 2, x2: p2.x + s2.w, y2: p2.y + s2.h / 2 });     // 父左缘 → 子右缘
      }
    }

    // [T08-2-4] 外接框口径：所有 visibleIds 卡片包围盒 ∪ 所有父子折线产出点，
    // 向外扩 max(所有可见卡 w, h) × 0.5（max 从 measureSizes() 返回值现算）
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
    const w = maxX - minX, h = maxY - minY;
    edgesSvg.setAttribute("width", w);
    edgesSvg.setAttribute("height", h);
    edgesSvg.style.left = minX + "px";
    edgesSvg.style.top = minY + "px";
    edgesSvg.setAttribute("viewBox", `${minX} ${minY} ${w} ${h}`);
    edgesSvg.innerHTML = "";

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
    relG.setAttribute("data-edge-group", "relations");
    edgesSvg.appendChild(relG);

    // 普通关系线（[phase8.6-B] 锚点 = 实测尺寸的卡中心）
    for (const r of state.relations) {
      if (!visibleIds.has(r.source) || !visibleIds.has(r.target)) continue;
      const p1 = layout[r.source], p2 = layout[r.target];
      if (!p1 || !p2) continue;
      const isForeshFulfill = r.source && state.cardMap[r.source] &&
        state.cardMap[r.source].card_type === "假设" && (r.type || "").includes("兑现");
      const color = isForeshFulfill ? "#f85149" : "#8b949e";
      const sw = isForeshFulfill ? "2" : "1.5";
      const dash = isForeshFulfill ? "" : "5 4";   // 假设兑现保持实线（语义强），其余弱关联用虚线
      const s1 = nodeSize(r.source), s2 = nodeSize(r.target);
      addLine(p1.x + s1.w / 2, p1.y + s1.h / 2, p2.x + s2.w / 2, p2.y + s2.h / 2, color, sw, dash, relG);
    }

    // 假设悬空红线（无兑现边）
    for (const cid of visibleIds) {
      const card = state.cardMap[cid];
      if (!card || card.card_type !== "假设") continue;
      const pos = layout[cid];
      if (!pos) continue;
      const hasFulfill = state.relations.some(r => r.source === cid && (r.type || "").includes("兑现") && visibleIds.has(r.target));
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
      const card = state.cardMap[cid];
      if (!card) continue;
      const node = nodeMap.get(cid);
      if (!node) continue;
      // 移除旧徽章
      const oldBadge = node.querySelector(".cross-link-badge");
      if (oldBadge) oldBadge.remove();
      // 检查是否有指向视图外目标的关系
      const hasCrossLink = state.relations.some(r => r.source === cid && !visibleIds.has(r.target));
      if (hasCrossLink) {
        const badge = document.createElement("span");
        badge.className = "cross-link-badge";
        badge.textContent = "🔗";
        badge.onclick = (e) => {
          e.stopPropagation();
          // 找到第一个跨视图目标，打开其工作区
          const crossRel = state.relations.find(r => r.source === cid && !visibleIds.has(r.target));
          if (crossRel) Workspace.open(crossRel.target);
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
  // 既有 addLine 不删——relations 线仍用（红线7）；两类线调用点不交叉。
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
    if (zoom > 1.2) return; // L2 已显示，不需要 tooltip
    const summary = await getSummary(card.card_id);
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

  async function getSummary(cardId) {
    if (summaryCache.has(cardId)) return summaryCache.get(cardId);

    // [R4] 快路径：结论摘要随画布数据一次到手（canvas.json 的 cards[].conclusion_summary），
    // 命中则直接使用、不发 getCard 请求——首屏 N+1 请求归零。
    // 字段缺失（老缓存未重建 / 后端未升级）→ 回退原路径，向后兼容。
    // 代价如实记：走快路径时 meta2（更新时间）不在画布数据里，保持原状不填；
    // L1 档位本就隐藏 meta2，L2 档位下该行的更新时间可能为空——如需它，回退路径仍在。
    const probe = state.cardMap[cardId];
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
  function lazyLoadVisibleSummaries() {
    for (const [cid, node] of nodeMap) {
      if (!summaryCache.has(cid)) {
        getSummary(cid);
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
    const oldZoom = zoom;
    const delta = e.deltaY > 0 ? -0.1 : 0.1;
    let newZoom = oldZoom + delta;
    newZoom = Math.max(0.25, Math.min(2.0, newZoom));
    // 以鼠标点为不动点：world_pos = (screen - pan) / zoom，新 pan = screen - world_pos * newZoom
    const wx = (mx - panX) / oldZoom;
    const wy = (my - panY) / oldZoom;
    panX = mx - wx * newZoom;
    panY = my - wy * newZoom;
    zoom = newZoom;
    applyTransform();
    lazyLoadVisibleSummaries();
    scheduleSaveView();   // [F2⑥] pan/zoom 结束防抖持久化
  }, { passive: false });

  // 平移
  let panState = null;
  viewport.addEventListener("mousedown", (e) => {
    if (e.target !== viewport && e.target !== world) return;
    if (e.button !== 0) return;
    panState = { startX: e.clientX, startY: e.clientY, panX, panY };
    viewport.classList.add("panning");
  });
  document.addEventListener("mousemove", (e) => {
    if (!panState) return;
    panX = panState.panX + (e.clientX - panState.startX);
    panY = panState.panY + (e.clientY - panState.startY);
    world.style.transform = `translate(${panX}px, ${panY}px) scale(${zoom})`;
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
    state.positions[cid] = { x, y, pinned: true };
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
      if (cid === state.viewRoot) continue;
      patch[cid] = { pinned: false };
      state.positions[cid] = state.positions[cid] || {};
      state.positions[cid].pinned = false;
    }
    try {
      if (Object.keys(patch).length > 0) await API.patchPositions(WORK_ID, patch);
    } catch (e) { notify("整理画布保存失败", "error"); }   // [F3④] 原静默
    renderCanvas();
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
        const r = await API.createCard(WORK_ID, { title, source: "用户直接创建", card_type: ctype, parent: state.viewRoot });
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
      state.cards = data.cards || [];
      state.relations = data.relations || [];
      // [card_045 E7 · L2 位置独立于卡片语义] 卡片改名、换类型、变状态、换归属
      // 一律不影响其坐标记录（positions 只按 card_id 键挂靠，不携带任何卡片
      // 语义字段）；验收＝改一张卡的标题／类型后，坐标记录不变、画布位置不变。
      state.positions = data.positions || {};
      if (!state.workCardId) {
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
          state.cards.some(c => c.card_type === "\u5267\u672c"), !!_wcId);
        const wc = _wcId ? state.cards.find(c => c.card_id === _wcId) : null;
        state.workCardId = wc ? wc.card_id : (state.cards[0] && state.cards[0].card_id) || "";
        state.workTitle = wc ? wc.title : "";
        state.viewRoot = state.workCardId;
      }
      rebuildCardMap();
      if (!__VIEW_RESTORED__) { restoreViewState(); __VIEW_RESTORED__ = true; }   // [F2⑥] 仅首次加载恢复（模块内旗标）
      showCanvasState("clear");
      renderCanvas();
      if (!state.cards.length) showCanvasState("empty");   // [F3④] 空态
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

  // [迭代四 F2⑤] 画布状态注册进单一 store（main.js 的 window.GetCanvasState 读此处）
  store.setState({ canvasState: state });

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
      state._unreadByCard = unread;
      for (const n of world.querySelectorAll(".card-node")) {
        const cid = n.dataset.id;
        const cnt = (state._unreadByCard || {})[cid] || 0;
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
