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
  function computeLayout(viewRootId, cards, relations, positions) {
    const result = {};
    const childMap = {};
    for (const c of cards) {
      const p = c.parent || "";
      if (!childMap[p]) childMap[p] = [];
      childMap[p].push(c);
    }
    const directChildren = childMap[viewRootId] || [];
    const root = cards.find(c => c.card_id === viewRootId);
    const isTopLevel = root && root.card_type === "剧本";

    // 1. pinned 卡用存储坐标
    for (const c of cards) {
      const pos = positions[c.card_id];
      if (pos && pos.pinned) {
        result[c.card_id] = { x: pos.x, y: pos.y };
      }
    }

    // 2. viewRoot 固定顶部中央 (0,0)
    result[viewRootId] = { x: 0, y: 0 };

    if (isTopLevel) {
      // ===== 顶层叙事轴布局 =====
      layoutNarrativeAxis(viewRootId, directChildren, relations, positions, result);
    } else {
      // ===== 非顶层网格矩阵 =====
      layoutGridMatrix(directChildren, result);
    }
    return result;
  }

  function layoutNarrativeAxis(viewRootId, children, relations, positions, result) {
    // 分类
    const plots = children.filter(c => c.card_type === "节点");
    const settings = children.filter(c => c.card_type === "世界观");
    const chars = children.filter(c => c.card_type === "行为体");
    const foreshs = children.filter(c => c.card_type === "假设");
    const ideas = children.filter(c => c.card_type === "线索");

    // 找"目标"卡（标题为目标 或 与剧本卡有锚定关系）
    const anchoredIds = new Set();
    for (const r of relations) {
      if (r.type === "锚定" && r.source !== viewRootId) anchoredIds.add(r.source);
    }
    const isEnding = (c) => c.title === "目标" || anchoredIds.has(c.card_id);

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
    // 清除旧节点（保留 svg）
    for (const [cid, node] of nodeMap) node.remove();
    nodeMap.clear();

    // 计算布局
    const layout = computeLayout(state.viewRoot, state.cards, state.relations, state.positions);
    state._baseLayout = layout;

    // 当前视图应显示的卡：viewRoot + 直属子卡（孙卡通过 ▸N 徽章 + 双击下钻可见）
    const visibleIds = new Set([state.viewRoot]);
    for (const child of (state.childMap[state.viewRoot] || [])) visibleIds.add(child.card_id);

    // 渲染节点（先按初始布局入 DOM）
    for (const cid of visibleIds) {
      const card = state.cardMap[cid];
      if (!card) continue;
      const pos = layout[cid] || { x: 0, y: 0 };
      const node = createNode(card, pos);
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
    const kidBadge = childCount > 0
      ? `<span class="kid-badge" title="有 ${childCount} 张子卡，点击进入（或双击卡片）">▸ ${childCount}</span>`
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

    // ▸N 徽章单击即下钻（比双击更可发现）
    const badge = node.querySelector(".kid-badge");
    if (badge) {
      badge.addEventListener("click", (e) => {
        e.stopPropagation();
        if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }
        setViewRoot(card.card_id);
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
    window.Workspace && window.Workspace.open(card.card_id);
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
    // 计算需要的 SVG 尺寸（所有卡的外接框）
    let minX = 0, minY = 0, maxX = 0, maxY = 0;
    for (const cid of visibleIds) {
      const p = layout[cid];
      if (!p) continue;
      minX = Math.min(minX, p.x - 200);
      minY = Math.min(minY, p.y - 100);
      maxX = Math.max(maxX, p.x + 400);
      maxY = Math.max(maxY, p.y + 200);
    }
    const w = maxX - minX, h = maxY - minY;
    edgesSvg.setAttribute("width", w);
    edgesSvg.setAttribute("height", h);
    edgesSvg.style.left = minX + "px";
    edgesSvg.style.top = minY + "px";
    edgesSvg.setAttribute("viewBox", `${minX} ${minY} ${w} ${h}`);
    edgesSvg.innerHTML = "";

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
        <option value="线索">线索</option>
        <option value="世界观">世界观</option>
        <option value="行为体">行为体</option>
        <option value="节点">节点</option>
        <option value="假设">假设</option>
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
        // 找剧本卡
        const wc = state.cards.find(c => c.card_type === "剧本");
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
