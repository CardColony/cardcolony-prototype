/* canvas.js - 画布核心：视口变换、布局引擎、LOD、拖拽 */
(function () {
  "use strict";

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

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

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
    // L0 时隐藏关系线
    edgesSvg.style.opacity = (zoom < 0.5) ? "0" : "1";
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

  function computeLayout(viewRootId, cards, relations, positions) {
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

    // 1. pinned 卡用存储坐标
    for (const c of cards) {
      const pos = positions[c.card_id];
      if (pos && pos.pinned) {
        result[c.card_id] = { x: pos.x, y: pos.y };
      }
    }

    // 2. viewRoot 固定顶部中央 (0,0)
    result[viewRootId] = { x: 0, y: 0 };

    // [T08-1-3] 原 isRootCard 双分支（叙事轴/网格矩阵）收敛为折叠树单路径；
    // [T08-1-5] layoutNarrativeAxis / layoutGridMatrix 停止调用（不删除），见打点三。
    layoutTree(viewRootId, childMap, positions, state.expanded, result);
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

  // [T08-1-2] 折叠树排布（替代原 narrativeAxis/gridMatrix 双分支）。
  // 方向口径（用户 2026-10-04 中途指令「布局用左右视图」，对任务书上下树的推翻，留痕）：
  // 左右视图——根卡居左，子树向右逐层展开；同父子树纵向一字排开不重叠
  // （子树纵向跨度 = 其叶子实测高之和 + 间距，父卡在子树跨度内垂直居中）。
  // 层间距由 measureSizes() 实测高推出（取不到按既有惯例回退 220×86）；列宽恒定
  // COL_W（容纳根卡 320 宽，任何 LOD 档卡宽 ≤320 < COL_W，横向恒不重叠）；
  // 实测高的纵向避让仍由 resolveOverlaps 在 DOM 建成后完成。
  // 只写内存 result，不读不写任何持久化载体（红线1/6）；pinned 卡由 computeLayout
  // 步骤1写入 result、此处不覆盖（保留规则），其子树以存储坐标为锚继续右排。
  function layoutTree(viewRootId, childMap, positions, expanded, result) {
    console.info("[T08] 树形排布：viewRoot=%s，节点数=%d", viewRootId, Object.keys(result).length);
    const sizes = measureSizes();
    // 回退行高分档：深度≥2 的卡按 t8-deep 紧凑形态（只显标题）取 36，
    // 其余按常规卡取 86（既有回退值）——首绘时 DOM 未建、实测不可得，按此估跨距
    const FALLBACK_H = 86, FALLBACK_H_DEEP = 36;
    const heightOf = (cid, depth) => (sizes[cid] && sizes[cid].h) || (depth >= 2 ? FALLBACK_H_DEEP : FALLBACK_H);
    const COL_W = 340;  // 相邻层横向基准（根卡实测宽 320 < COL_W，其余档位卡宽 ≤260）
    const GAP_Y = 40;   // 同父相邻子树纵向间距

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
    // 按初始 y 值分带（pinned 卡不参与分带、不被移动）
    const bands = new Map();
    for (const cid in layout) {
      if (pinned.has(cid)) continue;
      const y = layout[cid].y;
      if (!bands.has(y)) bands.set(y, []);
      bands.get(y).push(cid);
    }
    const ys = Array.from(bands.keys()).sort((a, b) => a - b);
    let prevBottom = null;
    for (const y0 of ys) {
      const ids = bands.get(y0);
      let bandY = y0;
      if (prevBottom !== null && bandY < prevBottom + 40) bandY = prevBottom + 40;
      let bandBottom = bandY;
      for (const cid of ids) {
        out[cid].y = bandY;
        const h = (sizes[cid] && sizes[cid].h) || 86;
        bandBottom = Math.max(bandBottom, bandY + h);
      }
      prevBottom = bandBottom;
    }
    return out;
  }

  // 暴露纯函数供测试/外部调用
  window.computeLayout = computeLayout;
  window.resolveOverlaps = resolveOverlaps;

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

  // [phase8.6-B] 重跑：实测尺寸 → 避让 → 回写节点坐标 → 重画连线
  function reflowLayout() {
    const base = state._baseLayout;
    if (!base) return;
    const sizes = measureSizes();
    const adjusted = resolveOverlaps(base, sizes, pinnedIdSet());
    state._layout = adjusted;
    for (const [cid, node] of nodeMap) {
      const p = adjusted[cid];
      if (!p) continue;
      node.style.left = p.x + "px";
      node.style.top = p.y + "px";
    }
    renderEdges(new Set(nodeMap.keys()), adjusted);
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

    // 计算布局
    const layout = computeLayout(state.viewRoot, state.cards, state.relations, state.positions);
    state._baseLayout = layout;

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

    // 渲染节点（先按初始布局入 DOM）
    for (const cid of visibleIds) {
      const card = state.cardMap[cid];
      if (!card) continue;
      const pos = layout[cid] || { x: 0, y: 0 };
      const node = createNode(card, pos);
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

    // [phase8.6-B] 先套当前 LOD 档位类再实测尺寸 → 纵向避让 → 回写坐标
    applyLODClasses();
    const sizes = measureSizes();
    const adjusted = resolveOverlaps(layout, sizes, pinnedIdSet());
    state._layout = adjusted;
    for (const cid of visibleIds) {
      const node = nodeMap.get(cid);
      const p = adjusted[cid];
      if (node && p) {
        node.style.left = p.x + "px";
        node.style.top = p.y + "px";
      }
    }

    renderEdges(visibleIds, adjusted);
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

    const childCount = (state.childMap[card.card_id] || []).length;
    // [T08-3-1/3-5] 徽章语义＝展开/折叠开关：▸（折叠态）↔ ▾（展开态）；N 恒为该卡子卡数
    const isExpanded = state.expanded.has(card.card_id);
    const kidBadge = childCount > 0
      ? `<span class="kid-badge" title="有 ${childCount} 张子卡，点击展开/折叠">${isExpanded ? "▾" : "▸"} ${childCount}</span>`
      : "";

    node.innerHTML = `
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
      }, 250);
    });

    // 拖拽
    let dragState = null;
    node.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      // 用 click 事件处理选择；拖拽单独监听 mousedown+move
      dragState = {
        startX: e.clientX, startY: e.clientY,
        nodeLeft: parseFloat(node.style.left), nodeTop: parseFloat(node.style.top),
        moved: false,
      };
      e.stopPropagation(); // 防止触发画布平移
    });

    document.addEventListener("mousemove", onDragMove);
    document.addEventListener("mouseup", onDragUp);

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
        const newLeft = parseFloat(node.style.left);
        const newTop = parseFloat(node.style.top);
        node.classList.remove("dragging");
        persistCardPos(card.card_id, newLeft, newTop);
        // [phase8.6-B] 用已避让布局重画连线（persistCardPos 已更新 state._layout）
        renderEdges(new Set(nodeMap.keys()), state._layout || {});
      }
      dragState = null;
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
    window.Workspace && window.Workspace.open(card.card_id);
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
      window.Workspace && window.Workspace.open(card.card_id);
    }
  }

  // [T08-3-2] 就地展开/折叠：只翻 state.expanded 成员关系后全量重绘。
  // 不调 setViewRoot（视图根不变＝「就地」的全部含义）、不调 fitView（视野不跳）。
  function toggleExpand(cardId) {
    if (state.expanded.has(cardId)) state.expanded.delete(cardId);
    else state.expanded.add(cardId);
    renderCanvas();
  }

  function setViewRoot(newRoot) {
    state.viewRoot = newRoot;
    summaryCache.clear();
    hideWorkspace();
    renderCanvas();
    fitView();
  }
  window.SetViewRoot = setViewRoot;

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
  function renderEdges(visibleIds, layout) {
    const sizes = measureSizes();

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
        elbowSegs.push({ x1: p1.x + s1.w, y1: p1.y + s1.h / 2, x2: p2.x, y2: p2.y + s2.h / 2 });
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

    // [T08-2-1] 父子线绘制（新增；下方 relations 循环原样保留——红线7）
    for (const seg of elbowSegs) {
      addElbowPath(seg.x1, seg.y1, seg.x2, seg.y2, "#30363d", "1.5");
    }
    console.info(`[T08] 父子线：绘制 ${elbowSegs.length} 条（可见卡 ${visibleIds.size} 张）`);

    // 普通关系线（[phase8.6-B] 锚点 = 实测尺寸的卡中心）
    for (const r of state.relations) {
      if (!visibleIds.has(r.source) || !visibleIds.has(r.target)) continue;
      const p1 = layout[r.source], p2 = layout[r.target];
      if (!p1 || !p2) continue;
      const isForeshFulfill = r.source && state.cardMap[r.source] &&
        state.cardMap[r.source].card_type === "假设" && (r.type || "").includes("兑现");
      const color = isForeshFulfill ? "#f85149" : "#30363d";
      const sw = isForeshFulfill ? "2" : "1.5";
      const s1 = nodeSize(r.source), s2 = nodeSize(r.target);
      addLine(p1.x + s1.w / 2, p1.y + s1.h / 2, p2.x + s2.w / 2, p2.y + s2.h / 2, color, sw);
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
          if (crossRel) window.Workspace.open(crossRel.target);
        };
        node.appendChild(badge);
      }
    }
  }

  function addLine(x1, y1, x2, y2, color, sw) {
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", x1); line.setAttribute("y1", y1);
    line.setAttribute("x2", x2); line.setAttribute("y2", y2);
    line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", sw);
    edgesSvg.appendChild(line);
  }

  // [T08-2-2] 父子直角折线：先横、再竖、再横（左右视图方向——用户中途指令推翻任务书
  // 上下向「先下、再横、再下」，留痕；写法范本＝假设悬空红线段）。
  // 既有 addLine 不删——relations 线仍用（红线7）；两类线调用点不交叉。
  function addElbowPath(x1, y1, x2, y2, color, sw) {
    const midX = x1 + (x2 - x1) / 2;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", `M ${x1} ${y1} L ${midX} ${y1} L ${midX} ${y2} L ${x2} ${y2}`);
    path.setAttribute("stroke", color);
    path.setAttribute("stroke-width", sw);
    path.setAttribute("fill", "none");
    path.setAttribute("data-edge", "parent-child"); // [T08] 机械验收标记（V6/V7 计数用，不影响渲染）
    edgesSvg.appendChild(path);
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
    try {
      const card = await API.getCard(WORK_ID, cardId);
      const concl = (card.body && card.body.conclusion) || "";
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
    if (panState) { panState = null; viewport.classList.remove("panning"); }
  });

  // ==================== positions 持久化（防抖） ====================
  const pendingPatches = {};
  let patchTimer = null;
  function persistCardPos(cid, x, y) {
    // 本地立即更新
    state.positions[cid] = { x, y, pinned: true };
    state._layout = state._layout || {};
    state._layout[cid] = { x, y };
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
    } catch (e) { /* toast 已处理 */ }
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
    } catch (e) { /* toast 已处理 */ }
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
      } catch (e) { /* toast 已处理 */ }
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
    try {
      const data = await API.getCanvas(WORK_ID);
      state.cards = data.cards || [];
      state.relations = data.relations || [];
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
      renderCanvas();
      hideWorkspace();
    } catch (e) { /* toast 已处理 */ }
  }

  function hideWorkspace() {
    const ws = document.getElementById("workspace");
    if (ws) ws.classList.remove("active");
  }
  window.ReloadCanvas = reloadCanvas;
  window.GetCanvasState = () => state;

  // 初始加载
  reloadCanvas();
})();
