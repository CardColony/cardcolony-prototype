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
import { API } from "./esm/api.js?v=39";
import { escapeHtml } from "./esm/util.js?v=30";
// 右栏"读全文"用与对话页同一份 Markdown 渲染（不另造一套）
import { renderMarkdown } from "./esm/md.js?v=31";
import { store } from "./esm/store.js?v=30";
import { RenderNav } from "./esm/nav_system.js?v=37";
import { notify } from "./esm/api.js?v=39";

let __VIEW_RESTORED__ = false;   // [F2⑥] 视图态仅首次恢复
// 卡片 → 最近一条活动事件（renderCanvas 每轮重建，供 createNode 查）
let _evByCard = new Map();
// 当前视图内最大的正文体量（重量条的 100% 基准）
let _maxWeight = 1;
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
  function expandAllCards() {
    let n = 0;
    for (const [cid, kids] of Object.entries(state.index.childMap)) {
      if (kids.length && !state.view.expanded.has(cid)) {
        state.view.expanded.add(cid);
        n++;
      }
    }
    if (!n) return;
    scheduleSaveView();
    renderAll();
    setTimeout(() => fitToContent(), 320);
  }

  function collapseAllCards() {
    if (!state.view.expanded.size) return;
    state.view.expanded.clear();
    state.view.openCells.clear();
    scheduleSaveView();
    renderAll();
    setTimeout(() => fitToContent(), 320);
  }

  const moreWrap = document.getElementById("moreWrap");
  const moreBtn = document.getElementById("moreBtn");
  if (moreWrap && moreBtn) {
    const closeMore = () => {
      moreWrap.classList.remove("is-open");
      moreBtn.setAttribute("aria-expanded", "false");
    };
    moreBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = moreWrap.classList.toggle("is-open");
      moreBtn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    moreWrap.addEventListener("click", (e) => {
      if (e.target.closest && e.target.closest(".cv-mi-btn")) closeMore();
    });
    document.getElementById("expandAllBtn")?.addEventListener("click", expandAllCards);
    document.getElementById("collapseAllBtn")?.addEventListener("click", collapseAllCards);
    document.addEventListener("mousedown", (e) => {
      if (!moreWrap.contains(e.target)) closeMore();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeMore();
    });
  }

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
      // [画布 2026-10-19] 卡间信（from_card→to_card）。第二层「通信线」的数据源，
      // 由 refreshUnread 的同一个请求带回，不额外发请求。
      cardMsgs: [],
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
      // [画布 2026-10-19] 第三层「卡片关系」（先后/关联）默认**不画**：用户令
      // "卡片关系应该给我一个按钮手动展示出来"。关系线是**查证时才需要**的信息，
      // 常驻会把"父子结构"这条主线淹掉。开关状态不持久化（每次进来默认关）。
      showRelations: false,
      // [画布 2026-10-19] 全局视图＝**模式**，不是缩放区间。
      // 之前把它绑在"zoom < 0.42 才画块"上，结果 39 卡的项目在 40% 下整棵树
      // 只占屏幕中间一小块——形状看得见，但太小。全局视图该做的是"装下全部 +
      // 不画字"，装下多少就放大多少，不受块状档阈值限制。
      overview: false,
      // [画布 2026-10-19] 哪些卡处于"就地展开"状态。必须持久：发完消息会 reloadCanvas
      // 重建节点，不记住就会自己合上——"多张同时展开"直接失效。
      openCells: new Set(),
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
      // [画布 2026-10-19 · 实时状况] 内存态「谁在跑」：card_id -> {stage,detail,ts}。
      // 来自 /pulse 的 3s 轮询（不是投影字段——投影是缓存，装了会把某次运行冻住）。
      running: null,      // Map
      pulse: null,        // 最近一次 /pulse 原始响应（活动抽屉用）
      // [画布 2026-10-19 · 批量指令] 已提交但还没轮到跑的卡：card_id -> 提交时间。
      // 串行执行模式下同一 work 只有一张卡在跑，其余在服务端排队——但**排队没有
      // 任何进度记录**（progress 只在真正开跑时写）。不本地记一笔，用户批量下完
      // 指令后画布上只有 1 张在动，另外 4 张毫无变化，等于"指令丢了"。
      queued: null,       // Map
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
    // [画布 2026-10-19] 把缩放暴露成 CSS 变量 --z：总览层（lm）需要「屏幕上恒定
    // 大小」的状态点，写法是 calc(5px / var(--z))。JS 里算也行，但那要求每次缩放
    // 都遍历节点写内联样式——232 卡时正是最贵的那条路；变量只需写一次。
    world.style.setProperty("--z", String(state.view.zoom));
    const pct = Math.round(state.view.zoom * 100);
    if (zoomDisplay) zoomDisplay.textContent = pct + "%";
    // 滑块由视图态单向驱动（用户拖滑块时不回写，避免与输入打架）
    if (zoomSlider && document.activeElement !== zoomSlider) {
      zoomSlider.value = String(Math.min(200, Math.max(25, pct)));
    }
    updateLOD();
  }

  // [画布第三步 T2] 档位判据的 zoom 改参数传入（V1：本函数在渲染 10 函数名单内；
  // zoom 属视图态容器——T2③ 口径「从视图态容器取/参数传入，不从投影取」）
  function currentLODLevel(zoom) {
    // [画布 2026-10-18] 阈值 0.9：这是"LOD 与 fit 耦合"的解法。
    //
    // 死结：卡片高度随档位变（骨架 75 / 完整 150），而 fit 按高度算 zoom，
    // zoom 又决定档位 ⇒ 0.85 时如果走完整档（150px），9 张卡 1350px 装不进 956px，
    // fit 会缩到 0.65，又切回骨架档，卡片变矮、装得下了、zoom 回到 0.85……来回振荡。
    //
    // 把骨架档覆盖到 0.9，0.85 稳定落在骨架档：9 张卡 75px + 间距 ≈ 995px，
    // 0.85 缩放后 846px，刚好装进可用高度（844px）。标题 15×0.85 ≈ 12.75px 可读。
    //
    // [画布 2026-10-19] 新增 lm（总览层，zoom < 0.68）。
    // ──────────────────────────────────────────────────────────────────────────
    // 病根：原来只有 l0/l1/l2 三档，文字一律跟着缩放一起缩小。232 张卡全展开时
    // 缩放钳到 25%，标题实际只有 **3.3px** —— 整片画布成了不可读的灰噪点。
    // 越复杂的项目越看不见，方向完全反了，而"复杂多线任务"恰恰是画布存在的理由。
    //
    // 做法：低于 0.68 干脆**不画字**，卡片退化成「块」——宽度编码层级、左侧色条编码
    // 深度、状态点编码待裁定/运行中。这时画布回答的是"结构长什么样、哪里有活"，
    // 不是"这行字写了什么"；要读字就放大（点块即聚焦）。这就是语义缩放：
    // 缩小看结构，放大看内容。0.68 是下限——再低，13px 标题乘完不足 8.8px。
    // [画布 2026-10-19] lm 阈值 0.68 → **0.42**。
    // 0.68 是按"13px×0.68≈8.8px，太小"定的——但标题字号早改成了
    // `max(13px, 11px/zoom)`，屏幕上永远 ≥11px，那条理由已经失效。
    // 现在 lm 只在"字的下限撑不进卡片"时才接管：标题 11px 屏幕 ⇒ 世界里
    // 11/zoom，卡片行高 28px，11/zoom > 26 即 zoom < 0.42 时字会溢出卡片。
    if (state.view.overview) return "lm";   // 全局视图：不论缩放都画块
    if (zoom < 0.42) return "lm";
    if (zoom < 0.96) return "l0";
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
    // [画布 2026-10-19] 低缩放标记：计数（←N / 定N）没有屏幕字号下限，
    // 0.7 以下只剩 6px 的灰点——读不出任何东西，却各占 20~30px 卡宽，
    // 把标题挤成"上下文化…"。这些档位只留标题 + 状态点 + 展开徽章。
    if (world) world.classList.toggle("cv-lowzoom", zoom < 0.7);
    // 全局视图模式：块要**带名字**。原来直接复用 lm（不画字），于是整张图是一堆
    // 匿名矩形——"中间那块大的"是什么？不知道。不能指认的全局图没有用。
    if (world) world.classList.toggle("cv-overview", !!state.view.overview);
    // [画布 2026-10-19] 全局视图里"带名字"只在 zoom ≥ 0.5 时成立。
    // 33% 下 11px 字实际 3.6px——不是"小字"，是噪点：既读不出，又把块撑高。
    // 低于这条线就退回纯块（结构靠形状/面积/颜色表达），放大到 50% 再给名字。
    if (world) world.classList.toggle("cv-ov-names", !!state.view.overview && zoom >= 0.5);
    applyWeightMap(level);
    for (const [cid, node] of nodeMap) {
      node.classList.remove("l0", "l1", "l2", "lm");
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

  // ══════════════════════════════════════════════════════════════════════════
  // 重量地图（只在总览层 lm）
  // ──────────────────────────────────────────────────────────────────────────
  // [画布 2026-10-19] 画布区别于列表的地方是它能用**面积**编码一个列表根本没有的
  // 维度：**这条线有多重**。列表里每行一样高，"主战场在哪"永远看不出来。
  //
  // 为什么只在总览层做：块是抽象的（不画字），大小可以直接表达重量而不撒谎；
  // 在正常档位里把卡片撑大就必须填内容，填不出来就是一片假空白。
  //
  // 缩放区间 0.55~1.4 倍：最轻的块仍然可点（280×0.55≈154px 宽），
  // 最重的块不至于把整棵树挤爆。
  // ══════════════════════════════════════════════════════════════════════════
  function applyWeightMap(level) {
    if (level !== "lm") {
      for (const [, node] of nodeMap) {
        if (node.dataset.wm === "1") {
          node.style.width = "";       // 恢复"深度即层级"的宽度
          node.style.minHeight = "";
          delete node.dataset.wm;
        }
      }
      return;
    }
    let maxW = 1;
    for (const [, node] of nodeMap) {
      maxW = Math.max(maxW, Number(node.dataset.weight || 0));
    }
    for (const [, node] of nodeMap) {
      const w = Number(node.dataset.weight || 0);
      // 开方压缩：字数分布是长尾的（中位 2.3k、最大 40k），线性缩放会让一半的块
      // 缩到最小、另一半挤成巨块。开方后差异仍然清楚，但不会失真。
      // 总览层：块大小＝体量。基准**统一** 260（不按深度），否则面积同时被
      // "深度"和"重量"影响——实测最重的卡 card_018（depth 3，基准 184）反而更小，
      // 我把"浅"读成了"重"。层级由位置(x)和左侧色条表达，面积只编码重量。
      const t = Math.min(1, Math.sqrt(w / maxW));
      node.style.width = Math.round(260 * (0.55 + 0.85 * t)) + "px";
      node.style.minHeight = Math.round(52 * (0.62 + 1.0 * t)) + "px";
      node.dataset.wm = "1";
    }
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
    // [画布 2026-10-18] 同级子卡按**用户拖拽的 y** 排序（没拖过的按原顺序）。
    // 拖拽＝调顺序：存储的 y 只作排序依据，绝对位置仍由布局算——这样既不会
    // "拖了又弹回"，也不会像 pinned 那样累积偏移把布局拉散。
    const kidsOf = (cid) => {
      const raw = ((cid === rootId && rootAlwaysOpen) || expanded.has(cid)) ? (childMap[cid] || []) : [];
      if (raw.length < 2) return raw;
      return [...raw].sort((a, b) => {
        const pa = positions[a.card_id], pb = positions[b.card_id];
        const ya = pa && Number.isFinite(pa.y) ? pa.y : null;
        const yb = pb && Number.isFinite(pb.y) ? pb.y : null;
        if (ya !== null && yb !== null) return ya - yb;
        if (ya !== null) return -1;
        if (yb !== null) return 1;
        return 0;
      });
    };

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
    // 后序：算每个节点的**子树包围盒**（宽 + 高）。
    //
    // ★ [画布 2026-10-19] 必须算「子树宽」，不能只算「卡片宽」。
    //   病根：columnize 把兄弟卡横排成多列，但列宽原来只按子卡自身宽度算
    //   （224+COL_GAP）。可是子卡右边还要长出它自己的子树——兄弟 A 的子树从
    //   x=832 向右铺到 1564，兄弟 B 的子树从 1132 铺到 1864，两者横向重叠
    //   432px；它们又在同一行（同一 y 带），于是整片子树互相穿插。
    //   实测 232 卡全展开：233 对卡片重叠，最深压住一整张卡。
    //   正解是标准 tidy-tree：**单元格尺寸＝子树包围盒**，兄弟之间按子树宽隔开，
    //   这样任意两棵子树在平面上都不相交，重叠从结构上就不可能出现。
    const subtreeH = {};
    const subtreeW = {};
    const seen = new Set();
    (function measure(cid, depth) {
      if (seen.has(cid)) return;          // 脏数据成环：已算过，直接用缓存
      seen.add(cid);
      const kids = kidsOf(cid);
      const cols = columnize(kids);
      const rows = cols.reduce((m, c) => Math.max(m, c.length), 0);
      for (const col of cols) for (const k of col) measure(k.card_id, depth + 1);
      // 列宽＝该列所有子卡「子树宽」的最大值
      const colW = cols.map((col) => Math.max(0,
        ...col.map((k) => subtreeW[k.card_id] || widthOf(k.card_id, depth + 1))));
      const rowH = [];
      for (let r = 0; r < rows; r++) {
        let m = 0;
        for (const col of cols) {
          if (col[r]) m = Math.max(m, subtreeH[col[r].card_id] || heightOf(col[r].card_id, depth + 1));
        }
        rowH.push(m);
      }
      const totalW = colW.reduce((a, b) => a + b, 0) + Math.max(0, cols.length - 1) * COL_GAP;
      const totalH = rowH.reduce((a, b) => a + b, 0) + Math.max(0, rows - 1) * GAP_Y;
      // 子树盒：自身 + 跨层间距 + 子卡整体的横向跨度
      subtreeW[cid] = kids.length ? widthOf(cid, depth) + GAP_X + totalW : widthOf(cid, depth);
      subtreeH[cid] = kids.length ? Math.max(heightOf(cid, depth), totalH) : heightOf(cid, depth);
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
          anchorY = result[cid].y;
          childX = x + widthOf(cid, depth) + GAP_X;
        } else {
          // ★ [画布 2026-10-19] 卡片贴**子树盒顶部**，不在盒内垂直居中。
          //
          // 原来这里写的是 `topY + (subtreeH - 自身高)/2`，即卡片在自己的子树盒里
          // 居中。后果：同一行的两张兄弟卡，因为各自子树高度不同（一个 696、一个
          // 558），卡片就被居中到不同的 y —— 实测差 138px。同一排的卡高低错落，
          // 读者没法按行扫，"这一排是同级"这个最直观的层级线索就没了。
          //
          // 改成贴顶之后：同一行的卡 y 完全相等；行高仍按该行最大子树跨度分配，
          // 所以子树之间纵向也不会挤到。顺带把小数坐标消掉了（原来那些 /2 的居中
          // 偏移产生 .5px，实测 119 张卡带小数坐标）。
          result[cid] = { x: x, y: topY };
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
      // 每列的 x 起点：前几列的**子树宽**之和 + 列间距。
      // ★ 必须用子树宽（subtreeW）而不是卡片宽——见 measure 处注释：
      //   列宽只按卡片宽算时，兄弟卡右侧长出的子树会横向插进下一列，
      //   同一行就撞在一起（实测 232 卡 233 对重叠）。
      //   用 COL_GAP 而不是 GAP_X：同层并排，不是换层。
      const colWs = cols.map((col) => Math.max(0,
        ...col.map((k) => subtreeW[k.card_id] || widthOf(k.card_id, depth + 1))));
      const colX = [];
      let cursorX = childX;
      for (let ci = 0; ci < cols.length; ci++) {
        colX.push(cursorX);
        cursorX += colWs[ci] + COL_GAP;
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
          // ★ [画布 2026-10-19] **只推纵向，永不推横向**。
          //
          // 原来是「MTD 最短轴、纵向优先」：py <= px 推 y，否则推 x。问题出在
          // 那个 else 分支——只要横向重叠比纵向小，卡片就被横向推开。
          //
          // 在层级树里 **x 编码的是层级**（同一深度的卡必须落在同一条列上），
          // 横向一推层级就散了。实测 232 张卡全展开时：深度 1 的卡出现 549/559
          // 两个 x，深度 2 散成 73 个 x，其中 119 张带小数坐标（如 1666.25px）——
          // 这正是用户说的"一级卡二级卡混在一起、无序摆放"。而且推了 24 轮还剩
          // 114 对重叠，等于既破坏了结构又没解决问题。
          //
          // 纵向推是安全的：y 只表达"顺序"，推开了不影响读者理解层级；同层并排的
          // 卡本来就在不同列（COL_GAP 隔开），横向重叠只是"不在同一列"，不是真重叠。
          // 所以这里只要纵向有重叠就推 y，横向重叠量不参与决策。
          {
            const dir = dy >= 0 ? 1 : -1;   // b 在下则分别向上下推开
            if (aP)      out[b].y += dir * py;
            else if (bP) out[a].y -= dir * py;
            else { out[a].y -= dir * py / 2; out[b].y += dir * py / 2; }
          }
          moved = true;
        }
      }
    }
    if (moved) console.warn("[separateOverlaps] 24 轮内未完全收敛，可能残留个别相交");
    return out;
  }

  // 开发态重叠断言（默认关闭，`window.__CV_DEBUG_OVERLAP__ = true` 打开）。
  // 布局算对了就不该有重叠；这里只报警、不改坐标——把问题暴露出来，
  // 而不是像 separateOverlaps 那样推卡片把症状盖掉。
  function assertNoOverlap(layout, sizes) {
    const ids = Object.keys(layout);
    const w = (cid) => (sizes[cid] && sizes[cid].w) || 220;
    const h = (cid) => (sizes[cid] && sizes[cid].h) || 86;
    let n = 0;
    for (let i = 0; i < ids.length && n < 5; i++) {
      for (let j = i + 1; j < ids.length; j++) {
        const a = ids[i], b = ids[j];
        const ox = Math.min(layout[a].x + w(a), layout[b].x + w(b)) - Math.max(layout[a].x, layout[b].x);
        const oy = Math.min(layout[a].y + h(a), layout[b].y + h(b)) - Math.max(layout[a].y, layout[b].y);
        if (ox > 1 && oy > 1) {
          console.warn("[布局断言] 卡片重叠:", a, b, "重叠", Math.round(ox) + "x" + Math.round(oy));
          n++;
          if (n >= 5) break;
        }
      }
    }
    if (n >= 5) console.warn("[布局断言] 重叠过多，已截断输出");
    return n;
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
    // ★ [画布 2026-10-19] **不再跑 separateOverlaps**——布局本身已经保证零重叠。
    //
    // 原来这里接一步 `separateOverlaps(layout0, ...)` 做"事后避让"。实测证明这步
    // 是净损失：它按 MTD 最短轴推卡片（纵向推不动就横向推），而横向在层级树里
    // 编码的是层级，一推就散。232 卡全展开实测——
    //
    //     跑避让：   46 对重叠，同一父卡同列的行间距变成 363/66/255/73/22/54/130
    //     不跑避让：  0 对重叠，三列 y 完全一致（-2556/-2464/-2372…，整 92 间距）
    //
    // 也就是说避让在"修"一个它自己制造的问题，还没修干净。根因在布局侧：
    // 列宽必须按**子树包围盒**算、同排卡片必须**贴子树盒顶部**（两处已修）。
    // 布局算对了，避让就是纯多余动作——而且会把规整网格搅成参差。
    //
    // 保留函数本体（canvas_layout.test.mjs 仍在测它），只是不再进渲染链路。
    // 换成开发态断言：真有重叠就报警留痕，而不是悄悄推卡片把问题盖掉。
    const layout = layout0;
    if (window.__CV_DEBUG_OVERLAP__) assertNoOverlap(layout0, input.sizes);    const edges = computeEdgeGeometry(visibleIds, layout, input.sizes,
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
      //
      // [画布 5711 · P2-4 修] expanded 写之前按当前卡片表过滤。
      // 病根：读侧（restoreViewState）会过滤失效 id，写侧却原样写全 ⇒
      // 删掉的卡，其 id 永远留在存档里，存档单调增长、每次读都要过滤一遍
      // 越来越大的集合。写侧过滤 ⇒ 存档与真实卡片表同口径。
      const expanded = [];
      for (const id of state.view.expanded) {
        if (state.index.cardMap[id]) expanded.push(id);
      }
      // [画布 5711 · P2-5 修] openCells 一起存。
      // 病根：state.view.openCells 的注释写着"必须持久"，但 saveViewState
      // 根本没存它 ⇒ 刷新页面后所有就地展开的卡全部合上（用户以为丢了内容）。
      const openCells = [];
      for (const id of (state.view.openCells || [])) {
        if (state.index.cardMap[id]) openCells.push(id);
      }
      localStorage.setItem(`projView:${WORK_ID}`, JSON.stringify({
        panX: state.view.panX, panY: state.view.panY, zoom: state.view.zoom,
        expanded,
        openCells,
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
      // [画布 5711 · P2-5 修] 恢复"就地展开"的卡（与写侧对称）。
      if (Array.isArray(v.openCells)) {
        state.view.openCells = new Set(v.openCells.filter(id => state.index.cardMap[id]));
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

    // [画布 5711 · P0-1 修] **异常安全的两阶段渲染**。
    //
    // 病根：原来 `world.style.visibility="hidden"` 在函数中段、恢复语句在函数末尾
    // （不是 finally）。隐藏窗口覆盖了 createNode 循环、applyLODClasses、
    // measureSizes()（读全部 offsetWidth/offsetHeight，强制同步布局）、computeProjection
    // 与写坐标——其中任何一步抛异常，#world 就**永久**停在 visibility:hidden：
    // 整屏空白、卡片不可点、**控制台不报任何错**（异常被 reloadCanvas 的 catch 吃掉，
    // 而它不解除 hidden）。用户看到的就是"画布打开是空的，怎么点都没反应"。
    //
    // 作者在 renderSubtree 的注释里写过同一个坑（"会永久停在 visibility:hidden
    // （整屏空白，且不报任何错）"），但那里也只是靠"本轮是否隐藏过"判断，不是
    // finally —— 一样会漏。两处一并改成 finally。
    //
    // 为什么必须用 visibility（不是 display:none）：元素仍在布局流、尺寸可读——
    // display:none 的元素不参与布局、量不到尺寸，两阶段会静默退化成"全用估算值"
    // 而表面一切正常。这条纪律保留。
    let projection = null;
    try {
      world.style.visibility = "hidden";
      // 每张卡最近一条活动（后端按 ts 倒序给，取首条即最新）
      _maxWeight = 1;
      for (const cid of visibleIds) {
        const c = vm.cardsById[cid];
        if (c) _maxWeight = Math.max(_maxWeight, Number(c.content_len || 0));
      }
      _evByCard = new Map();
      for (const e of (vm.recentEvents || [])) {
        if (e && e.card_id && !_evByCard.has(e.card_id)) _evByCard.set(e.card_id, e);
      }
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
          // [画布 5711 · P1 修] animationend 不是可靠信号：系统开了"减少动效"时
          // CSS 把 .entering 的 animation 置为 none ⇒ 永远不触发 animationend ⇒
          // .entering 永不移除 ⇒ 新卡永久带 entering 的副作用（transition:none、
          // cursor:grabbing、z-index:30 压住邻卡、亮描边重投影）。
          // ⇒ 加 animationcancel ＋ setTimeout 双保险（动画 200ms，260ms 收口）。
          const _clearEntering = () => node.classList.remove("entering");
          node.addEventListener("animationend", _clearEntering, { once: true });
          node.addEventListener("animationcancel", _clearEntering, { once: true });
          setTimeout(_clearEntering, 260);
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
      projection = computeProjection({ ...pin, sizes: measureSizes() });
      lastProjection = projection;   // [画布重构 2.1] 供 renderSubtree 打补丁（连线的全画布基底）
      for (const cid of visibleIds) {
        const node = nodeMap.get(cid);
        const p = projection.layout[cid];
        if (node && p) {
          node.style.left = p.x + "px";
          node.style.top = p.y + "px";
        }
      }
    } finally {
      // 无论成败都必须恢复可见——这是"绝不出现静默白屏"的最后一道闸。
      world.style.visibility = "";
    }
    if (!projection) return;   // 投影算不出来 ⇒ 不画连线（异常已向上抛，这里只是不继续画）

    // [画布 5711 · P1-3 修] 选中集必须与在场节点求交。
    // 病根：state.view.selected 只在选中动作里改，从不与 nodeMap 求交 ⇒
    // 收起某张卡的父卡后，那张看不见的卡**仍留在选中集**里；此时按 Delete
    // 会把它一起删掉——**删掉一个用户在画布上根本看不到的卡**（数据丢失）。
    pruneSelectionToVisible();

    renderEdges(projection, vm);
    renderBreadcrumb(projection.breadcrumb);
    applySelectionClasses();
    applyTransform();
    lazyLoadVisibleSummaries(vm.cardsById);

    // 恢复"就地展开"的卡：重建节点后重新挂上对话（否则发完消息自己合上）
    if (state.view.openCells && state.view.openCells.size) {
      for (const cid of state.view.openCells) {
        const n = nodeMap.get(cid);
        if (!n) continue;
        n.dataset.open = "1";
        // [画布 5711 · P1-4 修] 展开的对话写进 .cv-cell ⇒ 卡片变高 ⇒ 布局要重排，
        // 否则它压住下一张卡。对照 toggleCell：那条路径 await 之后有 scheduleReflow，
        // 只有这条（重建后恢复已展开的卡）漏了。
        loadCell(cid).then(() => scheduleReflow()).catch(() => { /* 读取失败不阻断 */ });
      }
    }

    // [画布 2026-10-19] 不再自动弹并行层：卡片现在可以就地展开成对话，
    // 那才是"同时看几条线并回复"的形态。浮层是历史遗留，等确认后再整块删。

    // [画布 2026-10-19 · 用户令] "自动模式下卡片随时被自动新建，卡片与卡片之间的
    // 信息如何传递，有没有动效展示？"
    // 位置落定之后，给本次**新出现**的卡放一次"从父卡流过去"的光点：
    // 卡片淡入只说明"多了个东西"，光点从父卡沿主干线滑到子卡才说明"它是从那里
    // 派生出来的、信息是从那边过来的"。首帧不播（prevIds 为空时全算新增，
    // 一次性播 200 个光点等于噪声）。
    if (prevIds.size) {
      for (const cid of visibleIds) {
        if (prevIds.has(cid)) continue;
        const card = vm.cardsById[cid];
        if (!card || !card.parent || !prevIds.has(card.parent)) continue;
        pulseEdge(card.parent, cid, "#5b8cff");
      }
    }
  }

  // ── 传递动效：一个光点沿两点间的折线滑过去，然后自己消失 ──
  // 用 SVG animateMotion（声明式、不占 JS 帧），播完即移除节点，不留残留。
  function pulseEdge(fromCid, toCid, color) {
    const a = nodeMap.get(fromCid), b = nodeMap.get(toCid);
    if (!a || !b) return;
    const ax = (parseFloat(a.style.left) || 0) + a.offsetWidth;
    const ay = (parseFloat(a.style.top) || 0) + a.offsetHeight / 2;
    const bx = parseFloat(b.style.left) || 0;
    const by = (parseFloat(b.style.top) || 0) + b.offsetHeight / 2;
    const midX = (ax + bx) / 2;
    const d = `M ${ax} ${ay} L ${midX} ${ay} L ${midX} ${by} L ${bx} ${by}`;
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("r", "5");
    dot.setAttribute("fill", color || "#5b8cff");
    dot.setAttribute("class", "cv-pulse-dot");
    const anim = document.createElementNS("http://www.w3.org/2000/svg", "animateMotion");
    anim.setAttribute("dur", "0.75s");
    anim.setAttribute("fill", "freeze");
    anim.setAttribute("path", d);
    dot.appendChild(anim);
    edgesSvg.appendChild(dot);
    setTimeout(() => dot.remove(), 900);
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
    //
    // [画布 5711 · P0-2 修] **用 try/finally 收口**。
    // 病根：隐藏语句在这里、恢复语句在函数末尾（不是 finally）。隐藏窗口里
    // createNode 抛一次（脏数据、缺字段、OOM）⇒ #world **永久**停在
    // visibility:hidden ⇒ 整屏空白、点任何卡片都无反应、**不报任何错**。
    // 注释里本来就写着这个后果（"会永久停在 visibility:hidden（整屏空白，
    // 且不报任何错）"），但当时只用"本轮是否隐藏过"判断，不是 finally —— 一样会漏。
    let hidWorld = false;
    try {
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
          // [画布 5711 · P1 修] animationend 在"减少动效"下不触发 ⇒ 兜底移除
          const _clearEntering = () => node.classList.remove("entering");
          node.addEventListener("animationend", _clearEntering, { once: true });
          node.addEventListener("animationcancel", _clearEntering, { once: true });
          setTimeout(_clearEntering, 260);
          world.appendChild(node);
          nodeMap.set(cid, node);
        }
      }

      // ③ 锚点自己的徽标翻面（▸↔▾）——就地改这一处文本，不重建锚点节点
      updateExpandBadge(anchorNode, (pin.childMap[cardId] || []).length,
                        pin.expanded.has(cardId));

      // ④~⑥ [画布 2026-10-19] 结构性变更（展开/收起）后走**全量重排**。
      //
      // 原来走的是"只排 anchor 单元 + 局部避让"，两个毛病：
      //   ① 局部避让还是 separateOverlaps，会横向推卡，把层级列推散；
      //   ② 单元之外不重排：展开的子树变高后会压到邻居。实测 114 卡项目残留
      //      3 对重叠、232 卡项目六组兄弟的三列 y 完全对不上。
      //      而同一份数据只要强制全量重排就 0 重叠、三列 y 逐项相等。
      //
      // 全量布局现在是 O(n) 纯函数（避让已摘除），232 张卡重排也就几毫秒，
      // 不值得为省这点计算再维护第二套布局实现——两套实现必然漂移，而漂移的
      // 表现就是"点开一张卡，别处的卡歪了"。收起/叶子卡切换同样走这里，
      // 顺带保证任何时刻画布上都只有一种布局结果。
      if (hidWorld) world.style.visibility = "";
      reflowAll();
      // [画布 5711] 增量渲染自检：节点建出来了但坐标没落到位（布局里缺这张卡）
      // ⇒ 节点永远停在占位坐标 + .entering（透明）⇒ 卡片不可见，且**不报错**。
      // 命中就退回全量重绘：慢一点，但一定对。
      _assertNoGhostNodes();
    } finally {
      // 兜底：异常路径也绝不许把 #world 留在 hidden（静默白屏）。
      if (hidWorld) world.style.visibility = "";
    }
    // [画布 5711 · P1-3 修] 收起子树 ⇒ 在场节点减少 ⇒ 选中集必须同步裁剪，
    // 否则"收起一张卡的父卡"后，那张看不见的卡仍留在选中集里，
    // 按 Delete 会把它一起删掉（删掉用户根本没看见的卡 ＝ 数据丢失）。
    pruneSelectionToVisible();
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

  // 卡面那一行「这张卡刚说了什么」。
  // [画布 2026-10-19 · 实时状况] 抽成函数：运行中要把这一行**就地换成**
  // 「正在做什么」，运行结束再换回来。两处各拼一遍 HTML，就会出现"跑完恢复成
  // 另一个样子"的分叉（少个 data-who、多一层 span），而这种分叉只在跑完那一刻
  // 出现，平时看不见。
  function msgLineHtml(card) {
    if (!card) return "";
    const lm = card.last_msg && card.last_msg.text ? card.last_msg : null;
    if (!lm) return "";
    const who = lm.actor === "human" ? "你" : lm.actor === "assistant" ? "AI" : (lm.actor || "");
    return `<div class="cv-msgline" data-who="${lm.actor === "human" ? "you" : "ai"}" title="${escapeHtml(who + "：" + lm.text)}"><span class="cv-msg-who">${escapeHtml(who)}</span>${escapeHtml(lm.text)}</div>`;
  }

  // [画布第三步 T2] 改吃视图模型 vm（动作层装配）：viewRoot/positions/childMap/
  // expanded/_unreadByCard 一律从 vm 取——不直摸 state（V1）
  // 规模显示：3000 字 → "3.0k"。用户靠它判断"这张卡值不值得现在碰"。
  function createNode(card, pos, vm, depth) {
    const node = document.createElement("div");
    // 键盘可达：role=button + aria-label（屏幕阅读器要能读出"哪张卡、什么状态"）
    node.setAttribute("role", "button");
    node.setAttribute("tabindex", "-1");
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
    if (ws) node.dataset.ws = ws;
    if (isCardUnread(card)) node.dataset.unread = "1";

    // ══════════════════════════════════════════════════════════════════════
    // [画布 2026-10-19 · 画布的不可替代性] 列表能回答"有哪些""刚出了什么事"，
    // 但它回答不了**"这个项目现在的形状对不对"**——哪块在动、哪块死了、
    // 重量压在哪里。那是"实时掌控全局"唯一真正的含义，而它只能靠**视觉维度**：
    // 位置（层级）、面积（重量）、明度（活跃）、连线（耦合）。
    //
    // 这里补的是"明度"：最近 24 小时动过的卡提亮。不是"时间戳"（那个浮层里有），
    // 是**活跃区域**——一屏扫过去，亮的那几处就是此刻在推进的地方，暗的地方是停了的。
    // 这是我之前删掉新鲜度点时丢掉的一维，删的理由（"时间不改变动作"）只对
    // "逐卡判断"成立，对"看形状"不成立。
    // ══════════════════════════════════════════════════════════════════════
    // 重量（正文字数）：两个地方用——总览层决定块的大小（applyWeightMap），
    // 正常档位用底边一条横条表达（长度 ∝ 开方后的体量）。后者是我补的：
    // 只在总览层做，用户平时根本看不到，等于没做。
    node.dataset.weight = String(Number(card.content_len || 0));
    // 基准宽度与 CSS 的"深度即层级"保持一致（0→360 / 1→280 / 2→224 / 3→184），
    // 重量地图在这个基准上做缩放，否则一进总览层宽度就跳变。
    // 基准宽度＝深度（0→360 / 1→280 / 2→224 / 3→184）。
    //
    // ⚠ [画布 2026-10-19 · 试过又撤回] 我试过让宽度**常驻**编码体量（0.62~1.4 倍），
    // 结果：最轻的卡缩到 161px，标题只剩 3 个字（"上下…"）。**面积编码和可读标题
    // 在卡片这个尺寸上不可兼得**——卡片必须显示标题，标题必须够宽，那宽度就不能
    // 自由伸缩。所以面积只在总览层（块状、无字）里用。
    node.dataset.basew = String([360, 280, 224, 184][Math.max(0, Math.min(3, d))] || 280);

    const _up = Date.parse(String(card.updated_at || ""));
    if (Number.isFinite(_up) && Date.now() - _up < 24 * 3600 * 1000) node.dataset.fresh = "hot";
    // [画布 2026-10-19] 卡面那段文字的**来源**：explicit＝卡里真有「结论：」，
    // fallback＝没有结论、取现状块第一行。两者原来长得一模一样，用户直接问
    // "这段文字是什么信息"。用 data-ck 驱动 CSS 的 ::before 标签，不占布局。
    if (card.conclusion_kind) node.dataset.ck = String(card.conclusion_kind);
    const pendingN = Number(card.pending_count || 0);
    // 骨架档（zoom<0.7）只显示标题，待裁定数用 data-pending 挂在右上角
    if (pendingN > 0) node.dataset.pending = String(pendingN);
    // 根卡是项目本体，不是待办项——不给它挂"未开始/已结论"这类工作状态标签
    const isRootCard = card.card_id === state.nav.viewRoot;
    // ══════════════════════════════════════════════════════════════════════
    // [画布 2026-10-19 · 用户裁定] 状态只用**一个点**表达，不外露任何文字/数字。
    //
    // 用户原话：「待裁定你只需要一个最简单的 icon 告诉我有信息等裁定就可以了，
    // 根本不需要信息外露，因为我不论如何也不可能在画布上看清看明白直接做裁定的，
    // 我不论如何都要点进去看的」。
    //
    // 这是对"信息层级"的判定标准：**外层只放能独立讲明白的信息**。
    // 一个数字（"5 处待裁定"）讲不明白任何事——哪 5 处？要决定什么？都得点进去。
    // 既然无论如何都要点进去，画布上就只该回答"这里有没有活等我"——那是一个点。
    // 数字留在唯一有意义的地方：顶栏总数（全项目欠多少 → 逐条过）。
    const wsBadge = (() => {
      const st = isRootCard
        ? (pendingN > 0 ? "pending" : "")
        : (ws === "pending" && pendingN > 0 ? "pending" : ws);
      if (!st) return "";
      const title = st === "pending" ? `${pendingN} 处待裁定（点进去逐条过）`
        : st === "concluded" ? "已结论"
        : st === "empty" ? "未开始"
        : "讨论中";
      return `<span class="cv-wsdot" data-ws="${st}" title="${escapeHtml(title)}"></span>`;
    })();

    // ── 卡面正文：**一张卡只说一件最该被知道的事** ──
    //   pending    → 列出在等你的那几条（可直接读，不用点进去）
    //   concluded  → 结论
    //   discussing → 这段在讨论什么
    //   empty      → 什么都不说（留白比写"未开始"更有信息量）
    // 待裁定最多列 2 条：卡片是"索引"，不是"全文"；剩下的用"还有 N 处"收口。
    // ── 自动模式活动：这张卡处在自动推进的哪个阶段 ──
    // goal_state 取值（auto_driver.py）：active / paused / complete / blocked。
    //
    // [画布 2026-10-19 · 用户令] "卡片正在运行中、卡片任务已完成，需要不同的状态显示"。
    // 上一版只区分"在跑 / 没跑"：complete 和 blocked 都被当成"没跑"，于是一张
    // 已经跑完的卡和一张卡住的卡，在画布上长得一模一样——人得点进去才知道结果。
    // 现在四态各给一套视觉，并且**在总览层也能分辨**（靠左侧色条 + 顶部条，
    // 不依赖文字）：
    //   running  蓝 呼吸条 + 蓝描边 + "第 N 轮 · 当前步骤"
    //   done     绿 静态条 + 绿左条 + 底栏"✓ 已完成"
    //   blocked  红 静态条 + 红左条 + 底栏"受阻"
    //   paused   灰 静态条 + 灰左条 + 底栏"已暂停"
    // 关键区别：running 是**动**的（呼吸），done/blocked/paused 是**静**的。
    // 一眼扫过去，"还在动的"就是还需要你等的，"不动的"是可以去处理结果的。
    const goalState = String(card.goal_state || "");
    const runState = !card.auto_mode ? ""
      : goalState === "active" ? "running"
      : goalState === "complete" ? "done"
      : goalState === "blocked" ? "blocked"
      : goalState === "paused" ? "paused"
      : "";
    if (runState) node.dataset.runstate = runState;
    const running = runState === "running";
    const runRound = Number(card.goal_rounds || 0);
    const runStep = String(card.toolcard_step || "").trim();
    const runLine = running
      ? `<div class="cv-runline"><span class="cv-run-dot"></span>第 ${runRound || 1} 轮${runStep ? " · " + escapeHtml(runStep.slice(0, 22)) : ""}</div>`
      : "";
    // 结束态用底栏小胶囊说明结果（running 由上面那行动态行说明，不再重复）
    // 结束态同样只给图标，不给文字（文字在 title 里，悬停可读）
    const runChip = runState === "done" ? `<span class="cv-runchip" data-rs="done" title="自动任务已完成">✓</span>`
      : runState === "blocked" ? `<span class="cv-runchip" data-rs="blocked" title="自动任务受阻，需要处理">✕</span>`
      : runState === "paused" ? `<span class="cv-runchip" data-rs="paused" title="自动任务已暂停">‖</span>`
      : "";

    // ── 新鲜度：时间维度**长在卡片上**，不是另开一条时间轴 ──
    //
    // 画布是空间的，活动是时间的。把活动做成底部横条＝往空间画布里硬塞时间轴，
    // 既遮内容又和卡片对不上（用户还得自己把"14:32 某卡"映射到画布上的位置）。
    // 正确做法：时间体现为卡片自己的状态——
    //   6 小时内动过 → 呼吸的绿点（"这张刚被 AI 或你碰过"）
    //   48 小时内   → 静态绿点
    //   更早        → 无标记（沉默）
    // [画布 2026-10-19] 卡面不再挂新鲜度绿点（它是第二个"看不懂的小色块"）。
    // "多久前动过 + 动的是什么"挪到浮层元信息里——那里有空间把话讲完整。

    // [画布 2026-10-19 · 随时了解状况] "这张卡是不是已经死了"——只在**超过 3 天**
    // 没动时出现。每张卡都挂一个相对时间等于全是噪音；而"9 天没动"是真正的信号：
    // 它可能已经被放弃，或者卡在某个没人知道的依赖上。
    // l0 及更低档不显示（卡太窄，多一个数字会把标题挤成残句）。
    const _lastTs = String(card.updated_at || (card.last_msg && card.last_msg.ts) || "");
    const _staleDays = _lastTs ? (Date.now() - new Date(_lastTs).getTime()) / 86400000 : 0;
    const staleChip = _staleDays >= 3
      ? `<span class="cv-stale" title="最后活动：${escapeHtml(fmtRelative(_lastTs))} —— 已经 ${Math.floor(_staleDays)} 天没动过">${Math.floor(_staleDays)}天</span>`
      : "";

    const isBareRoot = isRootCard
      && !(state.index.childMap[card.card_id] || []).length
      && !String(card.conclusion || "").trim()
      // 新建项目的根卡带一段 57 字的模板占位（"当前状况快照…"），所以不能判 ===0；
      // 200 字以下视为"还没实质内容"。
      && Number(card.content_len || 0) < 200;
    // ══════════════════════════════════════════════════════════════════════
    // [画布 2026-10-19 · 用户裁定] 卡面正文**一律显示结论**，不再铺待裁定条目。
    //
    // 用户原话：「为什么待裁定对你来说这么重要，拢共给我造了三层，每一层全是待裁定」。
    // 数一下当时一张卡上的出现次数：正文第一条 + 底栏"N 处待裁定" + l0 右上角数字
    // ——三处；再加浮层元信息和顶栏总数，共五处。信息重复到这种程度，
    // 读者只会得到一个印象：**这个项目全是问题，没有任何产出**。
    //
    // 定性：**结论是资产，待裁定是负债。** 画布第一眼要展示的是"想清楚了什么"，
    // 负债用一个计数标记就够（l0 右上角数字 / l1 底栏胶囊，不同档只出现一个），
    // 条目本身属于"逐条过"，从顶栏总数进。
    // ══════════════════════════════════════════════════════════════════════
    // [画布 2026-10-19 · 用户裁定] **卡片不显示任何正文。**
    //
    // 用户原话：「结论不论如何都是不可能在卡片上显示全的，这么点窗口，你好好想想
    // 到底显示什么吧。何况，现在已经没有结论了，哪来的结论字段？」
    //
    // 查库确认他说得对：卡片里只有一个正文块 status_block（中位 **2316 字**，
    // 37/39 张有内容）；mission 几乎全空、rulings 只有 3 张且中位 4 字。
    // 所谓"结论"是我用正则从这 2316 字里抠 `结论:` 行抠出来的，39 张里只抠到 5 张。
    // 280px 宽的卡片对 2316 字——不管截几行都是残句，露出来只是让人误以为读到了什么。
    //
    // 所以卡面只放**属性**（都是完整、可独立理解的事实）：
    //   标题 / 状态点 / 子卡数 / 字数 / 引用数 / 多久没动
    // 内容去它读得完的地方：悬停浮层（约 120 字，带"结论/现状"标签）→
    // 并行窗口的「全文」→ 完整对话页。
    // ══════════════════════════════════════════════════════════════════════
    // [画布 2026-10-19 · 用户三问之后] **卡面唯一的正文＝最近一条定论。**
    //
    // 判据：画布上你只做一个判断——这条线想清楚了没有、我现在要不要管它。
    // 能回答这个的短信息只有一种：**它决定了什么**（定论 D01…）。
    // 现状、背景、讨论到哪了，都是给模型续上下文的，不是给人做判断的。
    //
    // 为什么它放得下而"结论"放不下：定论条目中位 **38 字**，卡面一行 18 字、
    // 两行 36 字——**一条定论恰好装得下两行**。而之前那个从自由文本里抠出来的
    // "结论"中位 79 字，永远是残句。不是"短一点就行"，是"它天生就是这个长度"。
    //
    // 没有定论的卡就只留标题（不写"未定论"——那是一句每张卡都一样的废话）。
    // ══════════════════════════════════════════════════════════════════════
    // ══════════════════════════════════════════════════════════════════════
    // [画布 2026-10-19 · 用户令] 卡面显示**这张卡最后一条对话**。
    //
    // 卡片本身就是对话单元（每张卡自带 session）。所以卡面最该显示的不是正文、
    // 不是定论，而是"这张卡刚刚在说什么"——那是唯一会实时变、且能让人立刻判断
    // "要不要插手"的东西。对话为空才退回定论。
    // ══════════════════════════════════════════════════════════════════════
    const msgLine = msgLineHtml(card);

    const lastDec = card.last_decision && card.last_decision.text ? card.last_decision : null;
    const decLine = lastDec
      ? `<div class="cv-dec-line" title="${escapeHtml(lastDec.id + "【" + lastDec.kind + "】" + lastDec.text)}"><span class="cv-dec-id">${escapeHtml(lastDec.id)}</span>${escapeHtml(lastDec.text)}</div>`
      : "";

    let bodyHtml = "";
    if (isBareRoot) {
      // 空项目的根卡：告诉用户下一步做什么，而不是一张空白卡片
      bodyHtml = `<div class="cv-empty-hint">这是项目的根卡。<br>点右上「＋ 新建卡」或按 <b>N</b> 建第一张子卡。</div>`;
    } else {
      // [画布 2026-10-19] 正文只在 **l2（>120%，放大读）** 出现。
      //
      // 这一层是"缩放即语义"的第三档：
      //   l0 结构（标题+状态+引用+子卡）→ l1 略大（同上）→ l2 内容（这段文字可读）
      // 之前把正文从所有档都删掉，结果是 200% 放大后卡片巨大却空无一物——
      // 用户放大就是为了读，读完发现没有东西可读，那缩放就失去了意义。
      //
      // 内容直接取投影里的 conclusion（后端算好的），**不需要再发请求**；
      // 它在 l0/l1 是 display:none，零布局成本。
      const ck = String(card.conclusion_kind || "");
      const ctext = String(card.conclusion || "").trim();
      bodyHtml = ctext
        ? `<div class="summary" data-ck="${ck}">${escapeHtml(ctext)}</div>`
        : "";
    }

    // [画布 2026-10-19] 状态点从底栏挪到**标题行左侧**。
    //
    // 它回答的是"这张卡现在要不要我动手"——这是读一张卡时的**第一个判断**，
    // 却原来待在底栏左侧、和右下角的计数隔着一整行空白：39 张卡扫下来，
    // 每张都要多一次视线下移。挪到标题前，一次注视就同时拿到"叫什么 + 要不要管"，
    // 而且所有点在同一条竖线上对齐，可以整列扫。
    //
    // 读屏标签：一张卡对读屏用户必须能读成"标题 + 状态 + 几处待裁定 + 几张子卡"
    node.setAttribute("aria-label", [
      card.title || card.card_id,
      ws === "pending" ? `${pendingN} 处待裁定` : ws === "concluded" ? "已结论" : "",
      childCount ? `${childCount} 张子卡` : "",
    ].filter(Boolean).join("，"));

    node.innerHTML = `
      ${msgBadge}
      <button class="cv-open" type="button" title="打开完整对话页" aria-label="打开完整对话">↗</button>
      <button class="cv-chat" type="button" title="在此展开对话（不离开画布）" aria-label="在此展开对话">💬</button>
      <div class="cv-opening" aria-hidden="true">正在打开对话…</div>
      ${runState ? '<div class="cv-runbar"></div>' : ""}
      <div class="cv-card-row">
        ${wsBadge}
        <div class="tname">${escapeHtml(card.title)}</div>
        ${runChip}
        ${Number(card.decision_count || 0) > 0 ? `<span class="cv-dec" title="已形成 ${card.decision_count} 条定论（D01…）——这条线已经收敛过 ${card.decision_count} 次">定${card.decision_count}</span>` : ""}
        ${Number(card.ref_count || 0) > 0 ? `<span class="cv-ref" title="有 ${card.ref_count} 张卡在正文里引用了这张卡">←${card.ref_count}</span>` : ""}
        ${staleChip}
        ${kidBadge}
      </div>
      ${msgLine || decLine}
      ${runLine}
      <div class="cv-cell"></div>
      ${bodyHtml}
    `;

    bindNodeEvents(node, card);
    return node;
  }

  // [画布 5711] 卡面**子元素**的监听（子卡徽章 / ↗ / 待裁定条目）。
  //
  // 为什么单独成函数：卡面内容被整体换掉时（后台实时更新），子元素是新的、
  // 监听器没了，必须**重绑**；而节点级监听（click / keydown / dblclick /
  // 拖拽 / 悬停）绑在 node 本身，node 没换 ⇒ **不能重绑**——重绑就是重复绑定
  // （单击跳两次页、展开翻两次）。
  function bindCardFace(node, card) {
    // [T08-3-1] ▸/▾ 徽章单击＝展开/折叠该卡子树（原语义为下钻 setViewRoot，已改）
    const badge = node.querySelector(".kid-badge");
    if (badge) {
      badge.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        toggleExpand(card.card_id);
      });
    }

    // [画布 2026-10-19 · 自查] 键盘操作。全局快捷键只管"已选中的那张卡"，
    // 这里补上"焦点就在这张卡上"时的两个动作：
    //   Enter  → 就地展开这张卡的对话（不离开画布）
    //   空格   → 展开/折叠子卡（展开徽章是鼠标热区，键盘用户原先完全够不着）
    //
    // [画布 5711 · 产品] 键盘的分工与鼠标**一一对应**，不重复也不打架：
    //   Enter ≡ 💬（就地展开对话）   空格 ≡ ▸N（展开子卡）
    //   ⇒ 鼠标单击走的是"打开完整对话页"（另一件事），键盘不再与它重叠。
    node.addEventListener("keydown", (e) => {
      if (e.target !== node) return;           // 卡内按钮/输入各自处理
      if (e.key === "Enter") {
        e.preventDefault();
        // [画布 5711 · P0 修] **必须止冒泡**。
        // 病根：document 级还有一个全局 keydown（`ids.length===1` ⇒ 同样 toggleCell）。
        // 焦点在卡上时，节点级先展开、事件继续冒泡、全局级又展开一次 ⇒ 展开又被收起
        // ⇒ **按 Enter 看着像没反应**（全量套件 T3.8 抓到的就是这条）。
        // ⇒ 止冒泡后：焦点在卡上 ⇒ 只有节点级处理；焦点在别处 ⇒ 只有全局级处理。
        e.stopPropagation();
        toggleCell(card.card_id);
      } else if (e.key === " " || e.key === "Spacebar") {
        if (!(state.index.childMap[card.card_id] || []).length) return;
        e.preventDefault();
        // 同上：全局的 Space 是"抓手态"（按住空格拖动平移）⇒ 节点级处理完必须止冒泡，
        // 否则**同一按空格**既展开子卡、又把视口切进抓手态（两个动作抢一个键）。
        e.stopPropagation();
        toggleExpand(card.card_id);
      }
    });

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
    //
    // [画布 5711 · 产品] **双击手势已撤除**（原 dblclick → 就地展开对话）。
    // 撤除理由（用户 2026-10-20 当面指出："单击卡，和折叠展开根本就是冲突的"）：
    //   单击要跳走，就得留 220ms 的双击判据窗口；而普通人双击间隔是 150–300ms
    //   ——**超过 220ms 时第一次 click 已经把页面跳走了，双击永远到不了**。
    //   ⇒ "就地展开对话"这个功能（用户 10-19 反复要的）在真实使用中时灵时不灵，
    //     而它和单击是**同一个手势的两个互斥结果**。
    //   ⇒ 现在：单击**立即**跳走（不再有"点了没反应"的那 220ms）；
    //     就地展开对话**只**走三个明确入口：卡上的 💬 按钮、右键菜单、Enter 键。
    //   ⇒ 一个语义一个入口，没有任何一个手势有两种结果。
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
      selectOnly(card.card_id);
      // [画布 5711 · 产品] **立即**给出反馈并跳转。
      // 原来有 220ms 的延迟（留给双击判据），那 220ms 里界面毫无反应 ⇒
      // "点了没反应，过一会儿突然整页跳走"。双击撤除后，延迟已无存在理由。
      // 现在：点下去立刻亮出"正在打开对话…"，随即跳走——延迟不再是"沉默的等待"。
      node.classList.add("is-opening");
      markCardRead(card.card_id, (card.last_msg || {}).ts);
      openCardChat(card.card_id);
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

    // [画布 5711 · 产品] 「在此展开对话」按钮（💬）：**不离开画布**地看这张卡的对话
    // 并直接回复。
    //
    // 为什么要有它（产品经理视角，不是代码视角）：
    //   画布上原本只有两条进对话的路——单击（220ms 后整页跳走）与双击（就地展开）。
    //   后者是用户 10-19 反复要的"在画布上同时看几个对话的进程并随时回复"，但它是个
    //   **纯隐藏手势**：卡面上没有任何东西提示"这里可以双击"。而唯一显式的入口 ↗
    //   又只做"跳整页"这一件事。
    //   ⇒ 用户站在画布上问"我怎么进对话"，答案是"你得知道双击这个隐藏手势"。
    //   ⇒ 把"就地展开"变成一个**看得见、点得到**的按钮：功能从"隐藏手势"变成"入口"。
    // [画布 5711 · 产品] 双击手势已撤除 ⇒ 这里**不再有**"双击仍保留"的快捷方式，
    // 就地展开的三个入口是：💬 按钮 / 右键菜单 / Enter 键（一个语义一个入口）。
    const chatBtn = node.querySelector(".cv-chat");
    if (chatBtn) {
      chatBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        toggleCell(card.card_id);
      });
    }
  }

  function bindNodeEvents(node, card) {
    // 卡面子元素的监听（换卡面后要重绑，见 bindCardFace 注释）。
    bindCardFace(node, card);

    // 明确入口：卡右上角的 ↗（常驻，不靠 hover 才发现）——进**完整对话页**。
    //
    // [画布 5711 · 产品] 这一段原来写的是"双击＝在画布上打开这张卡的对话"，那是
    // 2026-10-19 的设计，**已被 2026-10-20 的用户裁定推翻**：
    //   · 双击手势**已撤除**（原 dblclick → toggleCell 就地展开）。
    //     理由见 click 处理器的注释：单击与双击是同一手势的两个互斥结果，而双击在
    //     真实使用中（间隔 150–300ms > 判据窗口 220ms）根本到不了。
    //   · "就地展开对话"（不跳页）现在只走三个明确入口：**💬 按钮 / 右键菜单 / Enter**。
    //   · ↗ 的语义从头到尾没变过：**跳整页**（需要大屏长文时用）。
    // 保留这段空实现是为了让"为什么没有双击"在代码里有据可查，而不是无声消失。

    // 拖拽
    let dragState = null;
    // [画布商业化 2026-10-17] 多选整组拖动：拖的是选中集里的卡且选中多于 1 张时，
    // 整组一起动（这是多选的唯一意义，否则多选只能用来删除）。
    let dragGroup = null;
    node.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      // [画布 5711 · P1 修] 按在**卡内控件**上（💬 / ↗ / ▸N 徽标）时，**不进拖拽/平移分支**。
      //
      // 病根（实测抓到的真 bug）：平移态的 CSS 是
      //   `.page-canvas #viewport.panning .card-node { pointer-events: none; }`
      // —— 平移中**所有卡片都不可点**。而根卡（视图根）的 mousedown 会进
      // "拖根卡＝平移"分支 ⇒ 按下的瞬间 viewport 就进了 .panning ⇒
      // **鼠标抬起的落点变成 viewport** ⇒ click 事件的 target 是 viewport，
      // 控件的 click 处理器**根本没被调用**。
      // ⇒ 症状：**根卡上的 💬（在此展开对话）／↗（打开对话页）／▸N（展开子卡）点了没反应**
      //   —— 而根卡恰是打开画布看到的第一张卡、最显眼的那张。
      //   （其它卡不受影响：它们的 mousedown 只起拖拽、不加 .panning。）
      // ⇒ 修法：控件上的按下**不抢**——让控件自己处理这次点击。
      if (e.target && e.target.closest && e.target.closest("button, .kid-badge, input, textarea, select, a")) {
        return;
      }
      // [R1] 若上一次拖拽的监听器尚未卸掉（异常路径），先卸，避免重复挂载
      if (activeDragDetach) activeDragDetach();
      // [画布商业化 2026-10-17] 根卡是布局坐标系的锚点（computeLayout 恒把它放在
      // 原点，子卡相对它排布）——单独拖根卡会被下一轮布局拉回原位，表现为"拖了又弹回"。
      // 故拖根卡＝拖画布：语义是"移动整个视图"，落点即时生效、不会弹。
      //
      // [画布 5711 · P1 修] 锚点判据改成 **viewRoot**，不是 workCardId。
      // 病根：computeLayout 钉在原点的那个卡是 **viewRoot**（collectProjectionInput
      // 传的就是 state.nav.viewRoot；下钻 setViewRoot 会改它），而这里判的是
      // workCardId（项目根卡，全生命期不变）。⇒ **下钻之后两者不是同一张卡**：
      //   ① 当前视图根（就是标着 .is-root 的那张）走普通拖拽分支 ⇒ 落库坐标后
      //      reflowAll 又被 computeLayout 拉回 (0,0) ⇒ **拖了又弹回**，
      //      而且用户已经"拖成功"了（卡跟着鼠标动过），落空感极强；
      //   ② 真正该"拖根卡=平移"的项目根卡此时不在可见集里 ⇒ 下钻后没有任何卡
      //      能用来平移视图（只能去拖空白处）。
      // 改成 viewRoot 后：默认视图行为**逐字不变**（viewRoot === workCardId），
      // 下钻后则由当前视图根承担"拖＝移动视图"，与 .is-root 样式、布局口径完全一致。
      if (card.card_id === state.nav.viewRoot
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
        const movedIds = [];
        for (const g of dragGroup) {
          g.node.classList.remove("dragging");
          persistCardPos(g.id, parseFloat(g.node.style.left), parseFloat(g.node.style.top));
          movedIds.push(g.id);
        }
        // [画布 5711 · P1 修] 同层顺序归一化（见 normalizeSiblingOrder 注释）：
        // 整组拖动时，组内各卡的落点已是彼此的相对位置，只需把**没被拖的同层卡**
        // 的当前位置显式记下来，这一层的顺序才是"人摆的顺序"。
        normalizeSiblingOrder(movedIds);
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
        // [画布 5711 · P1 修] 同层顺序归一化：把"用户把卡放在哪"变成"同层里它排第几"。
        // 不做这一步的话，同层里第一次拖某张卡时它会被提到该层最前面（用户落点被忽略）。
        normalizeSiblingOrder([card.card_id]);
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
    // 点卡即退出全局视图——点它说明你想看这张卡，不是想看形状
    node.addEventListener("click", () => exitOverview(), true);
    node.addEventListener("mouseenter", () => {
      highlightEdges(card.card_id);
      // ══════════════════════════════════════════════════════════════════
      // [画布 2026-10-19 · 用户澄清] 悬停浮层＝**这张卡的对话 + 输入框**。
      // 删的是"纯文字框"（正文/子卡名单那种只读不看的东西），不是这个——
      // 这个能当场回一句，是画布上唯一"不跳页就能说话"的入口。
      // ══════════════════════════════════════════════════════════════════
      const warm = tooltip.classList.contains("active");
      hoverTimer = setTimeout(() => showTooltip(card, node), warm ? 0 : 260);
    });
    node.addEventListener("mouseleave", () => {
      highlightEdges("");
      clearTimeout(hoverTimer);
      clearTimeout(hideTimer);
      hideTimer = setTimeout(hideTooltip, 220);
    });
    node.addEventListener("mouseenter", () => { clearTimeout(hideTimer); });

    // （原此处有一个 `dblclick → preventDefault()` 的空监听：它会与上面真正的
    //  双击进入监听抢事件，已随本次改造删除。）
  }

  // ══════════════════════════════════════════════════════════════════════
  // [画布商业化 2026-10-17] 选择系统
  //
  // [画布 5711 · 产品] 语义已再次演进，这段说明按**当前**事实重写（原文本描述的是
  // 一版已被推翻的设计：那时是"单击＝选中、双击＝进入"）：
  //   现在单击＝**进对话页**（唯一的主操作，2026-10-20 用户裁定）；
  //   双击手势**已撤除**（单击与双击互斥，且真实双击间隔 150–300ms 根本到不了
  //   220ms 判据窗口，双击永远等不到）；
  //   选择走 Shift/Ctrl/⌘+单击，展开子卡走 ▸N 徽标或空格，下钻走 Alt+单击。
  //   进对话有两条路：跳整页（单击 / ↗ / 右键「打开对话」）、就地展开
  //   （💬 / 右键「在此展开对话」/ Enter）。
  // ══════════════════════════════════════════════════════════════════════
  function selectionIds() {
    return [...state.view.selected];
  }

  function applySelectionClasses() {
    for (const [cid, node] of nodeMap) {
      node.classList.toggle("is-selected", state.view.selected.has(cid));
    }
    // [画布 2026-10-19 · 自查] 键盘可达（roving tabindex）。
    //
    // 病根：卡片一个 tabindex 都没有 ⇒ **键盘用户根本选不中任何一张卡**。
    // 后面的 ↑↓←→ / Enter / Delete / Tab 快捷键全都依赖"已选中一张卡"，
    // 而选中只能靠鼠标点。等于这套快捷键只对鼠标用户有效。
    //
    // 做法是列表控件标准的 roving tabindex：整棵树只有**一个** Tab 停靠点，
    // 停在当前选中的卡上（没选中就停在视图根卡）；Tab 进来之后用方向键在卡间走。
    // 这样 Tab 不会被 232 张卡困住，也不会出现"焦点在画布外"的死角。
    syncRovingTabindex();
    // 只选中一张时聚焦它的连线；多选或空选恢复全亮
    const ids = [...state.view.selected];
    highlightEdges(ids.length === 1 ? ids[0] : "");
    renderSelectionBar();
    renderCardDetail();
  }

  function syncRovingTabindex() {
    if (!nodeMap.size) return;
    const ids = [...state.view.selected];
    const stop = (ids.length === 1 && nodeMap.has(ids[0]))
      ? ids[0]
      : (nodeMap.has(state.nav.viewRoot) ? state.nav.viewRoot : nodeMap.keys().next().value);
    for (const [cid, node] of nodeMap) {
      const v = cid === stop ? "0" : "-1";
      if (node.getAttribute("tabindex") !== v) node.setAttribute("tabindex", v);
    }
  }

  function setSelection(ids, additive = false) {
    if (!additive) state.view.selected.clear();
    for (const id of ids) {
      if (!id) continue;
      if (additive && state.view.selected.has(id)) state.view.selected.delete(id);
      else state.view.selected.add(id);
    }
    applySelectionClasses();
    focusSelectedCard();
  }

  // [画布 2026-10-19 · 自查] 让 DOM 焦点跟上视觉选中。
  // 不做的后果：点一张卡看起来选中了，但焦点还留在 body —— 读屏软件不会播报，
  // :focus-visible 不出现，后续 Tab 也从页面开头重新走。
  // preventScroll：画布是 transform 平移的，focus 默认滚动会把整页拽走。
  function focusSelectedCard() {
    if (document.querySelector(".cv-modal")) return;   // 弹窗开着时不抢焦点
    const ids = [...state.view.selected];
    if (ids.length !== 1) return;
    const node = nodeMap.get(ids[0]);
    if (node && document.activeElement !== node) {
      try { node.focus({ preventScroll: true }); } catch (e) { /* 老浏览器无 options */ }
    }
  }

  // [画布 2026-10-18 · 修真 bug] 右栏浮层打开后，**被选中的卡可能正好在它下面**。
  // 实测：选中右侧的卡 → 浮层弹出盖住它 → 之后所有点击都落在浮层上（连拖拽都失效，
  // 因为 mousedown 根本没到卡片）。所以选中后要把视野左移，保证卡在可见区内。
  function ensureCardVisible(cid) {
    const node = nodeMap.get(cid);
    if (!node) return;
    const panel = document.getElementById("rulePanel");
    const panelW = (panel && panel.classList.contains("is-open")) ? panel.offsetWidth : 0;
    if (!panelW) return;
    const vp = viewport.getBoundingClientRect();
    const r = node.getBoundingClientRect();
    const safeRight = vp.right - panelW - 16;
    if (r.right <= safeRight) return;
    const dx = Math.round(r.right - safeRight);
    applyViewState({ panX: state.view.panX - dx });
    scheduleSaveView();
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

    // 未选中：浮层收起（画布满宽）。规则卡改由工具栏按钮打开。
    if (!ids.length) {
      cardDetailEl.hidden = true;
      cardDetailEl.innerHTML = "";
      if (rulePanelHeadEl) rulePanelHeadEl.hidden = false;
      if (rulePanelBodyEl) rulePanelBodyEl.hidden = false;
      const panel = document.getElementById("rulePanel");
      if (panel && panel.dataset.mode === "card") {
        panel.classList.remove("is-open");
        panel.dataset.mode = "";
      }
      return;
    }

    if (rulePanelHeadEl) rulePanelHeadEl.hidden = true;
    if (rulePanelBodyEl) rulePanelBodyEl.hidden = true;
    cardDetailEl.hidden = false;
    cardDetailEl.innerHTML = "";
    const sidePanel = document.getElementById("rulePanel");
    if (sidePanel) {
      sidePanel.classList.add("is-open");
      sidePanel.dataset.mode = "card";
    }

    // 多选：列出选中的卡 + 批量操作
    //
    // [画布 2026-10-18 · 并行] 多选的意义不是"一次删掉"，而是**一次对多张卡做同一件事**。
    // 这里给三个真正用得上的：全部展开、全部折叠、批量删除。
    if (ids.length > 1) {
      // 多选**不打开右栏浮层**：批量操作用底部那条操作条（cv-selbar）。
      // 浮层一开就盖住画布右侧，而多选的目的正是在画布上批量操作
      // （实测：多选后浮层遮挡，拖拽的 mousedown 全落在浮层上，整组拖动完全失效）。
      const p2 = document.getElementById("rulePanel");
      if (p2 && p2.dataset.mode === "card") {
        p2.classList.remove("is-open");
        p2.dataset.mode = "";
      }
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

    // ══════════════════════════════════════════════════════════════════════
    // [画布 2026-10-19 · 用户裁定] **单卡不再开右栏**。
    //
    // 右栏原来的内容是：标题 + 状态/引用/字数/时间 + 结论 + 全部待裁定条目 +
    // 读全文 + 打开对话。除了"读全文"，其余每一项都和悬停浮层、卡片面重复——
    // 用户原话：「显示的信息毫无用处」「和浮窗信息高度重合」「裁定不需要，你右侧
    // 还是出现」。更糟的是它盖住画布右侧 380px，等于拿画布空间换重复信息。
    //
    // 现在的分工（一个信息只出现在一个地方）：
    //   卡片面     → 这是什么 + 第一条待裁定（索引）
    //   悬停浮层   → 结论全文（卡面读不全的那部分）
    //   并行对话窗 → 这张卡的对话进程 + 随时回复 + 读全文（唯一"做事"的地方）
    //   顶栏       → 全项目待裁定总数 → 逐条过
    // 于是这里只负责把右栏收起来。
    const pnl = document.getElementById("rulePanel");
    if (pnl && pnl.dataset.mode === "card") {
      pnl.classList.remove("is-open");
      pnl.dataset.mode = "";
    }
    cardDetailEl.hidden = true;
    cardDetailEl.innerHTML = "";
  }

  // [画布 5711 · P1-5b] 轻量选中态：**只切 class**，不重建任何面板。
  // 框选拖动过程中每个 mousemove 都会调它（重面板留到 mouseup）。
  function applySelectionClassesLite() {
    for (const [cid, node] of nodeMap) {
      const on = state.view.selected.has(cid);
      if (node.classList.contains("is-selected") !== on) {
        node.classList.toggle("is-selected", on);
      }
    }
  }

  function selectOnly(id) {
    state.view.selected.clear();
    if (id) state.view.selected.add(id);
    applySelectionClasses();
    focusSelectedCard();   // 点选后把 DOM 焦点交给这张卡（读屏播报 + 后续键盘操作）
    // 浮层在 applySelectionClasses → renderCardDetail 里打开，等它开完再校正视野
    if (id) setTimeout(() => ensureCardVisible(id), 0);
  }

  // ══════════════════════════════════════════════════════════════════════
  // [画布 5711 · P1-3 修] 选中集与在场节点求交（防"删掉看不见的卡"）
  //
  // 病根：state.view.selected 只在选中动作里改，**从不与 nodeMap 求交**。
  // 于是「选中 2 张 → 收起其中一张的父卡」之后，那张已经不在画布上的卡
  // 仍然留在选中集里；此时按 Delete 会把它一起删掉——删的是一张
  // **用户在画布上根本看不到**的卡。这是数据丢失，不是视觉瑕疵。
  //
  // 触发点：renderCanvas（全量重建）、renderSubtree（收起子树）之后，
  // 以及 deleteSelected 执行前（最后一道闸）。
  // ══════════════════════════════════════════════════════════════════════
  function pruneSelectionToVisible() {
    if (!state.view.selected || !state.view.selected.size) return 0;
    const before = state.view.selected.size;
    for (const id of [...state.view.selected]) {
      if (!nodeMap.has(id)) state.view.selected.delete(id);
    }
    const after = state.view.selected.size;
    if (after !== before) {
      // 选中数变了 ⇒ 选择条/右栏的文案得跟着变（它们读的是 state.view.selected）
      renderSelectionBar();
      renderCardDetail();
    }
    return before - after;
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
  // ══════════════════════════════════════════════════════════════════════════
  // 多选操作条
  //
  // [画布 2026-10-19 · 并行操作] 多选的意义不是"一次删掉"，而是**一次对多张卡
  // 做同一件事**。改造前这条只有 聚焦 / 取消 / 删除——三个里两个是"退出"，
  // 真正对多张卡做事的只有删除。而右栏那份「全部展开 / 全部折叠」在多选时
  // 被刻意收起了（避免遮挡画布），于是那套批量能力**根本点不到**，是死代码。
  //
  // 现在这条操作条是画布"并行处理多个任务"的主入口：
  //   展开/折叠 —— 一次铺开几个领域的子树
  //   下指令     —— 一句话同时发给 N 张卡（各自在卡内执行）
  //   对比       —— 两张卡并排读结论（决策场景）
  //   并行盯     —— 把这几张卡放进并行对话层，同时看进度、随时回
  // ══════════════════════════════════════════════════════════════════════════
  function renderSelectionBar() {
    const n = state.view.selected.size;
    if (n < 2) { if (selBar) { selBar.remove(); selBar = null; } return; }
    if (!selBar) {
      selBar = document.createElement("div");
      selBar.className = "cv-selbar";
      viewport.appendChild(selBar);
    }
    const ids = [...state.view.selected];
    const hasExpandable = ids.some((id) => (state.index.childMap[id] || []).length > 0);
    selBar.innerHTML = `
      <span class="cv-selbar-count">已选 <b>${n}</b> 张</span>
      <span class="cv-selbar-sep"></span>
      ${hasExpandable ? `<button class="cv-selbar-btn" data-act="expand">展开</button>
      <button class="cv-selbar-btn" data-act="collapse">折叠</button>` : ""}
      <button class="cv-selbar-btn is-primary" data-act="command">下指令</button>
      ${n === 2 ? `<button class="cv-selbar-btn" data-act="cmp">对比</button>` : ""}
      ${n <= 4 ? `<button class="cv-selbar-btn" data-act="watch">并行盯</button>` : ""}
      <span class="cv-selbar-sep"></span>
      <button class="cv-selbar-btn" data-act="focus">聚焦</button>
      <button class="cv-selbar-btn" data-act="clear">取消</button>
      <button class="cv-selbar-btn is-danger" data-act="del">删除</button>
    `;
    selBar.querySelectorAll(".cv-selbar-btn").forEach((b) => {
      b.onclick = (ev) => {
        ev.stopPropagation();
        const act = b.dataset.act;
        const cur = [...state.view.selected];
        if (act === "clear") clearSelection();
        else if (act === "del") deleteSelected();
        else if (act === "focus") { if (cur[0]) focusCard(cur[0]); }
        else if (act === "cmp") openCompare(cur);
        else if (act === "watch") {
          for (const cid of cur.slice(0, 4)) watchAdd(cid);
          notify(`已加入并行对话层（${Math.min(cur.length, 4)} 张）`);
        } else if (act === "expand" || act === "collapse") {
          let changed = 0;
          for (const cid of cur) {
            const hasKids = (state.index.childMap[cid] || []).length > 0;
            if (!hasKids) continue;
            const isOpen = state.view.expanded.has(cid);
            if (act === "expand" && !isOpen) { toggleExpand(cid); changed++; }
            if (act === "collapse" && isOpen) { toggleExpand(cid); changed++; }
          }
          if (!changed) notify(act === "expand" ? "选中的卡都已经展开了" : "选中的卡都已经收起");
        } else if (act === "command") {
          batchCommand(cur);
        }
      };
    });
  }

  // ── 批量下指令 ────────────────────────────────────────────────────────────
  //
  // [画布 2026-10-19 · 并行操作] 这是"画布 vs 列表"最硬的一条差异：
  // 列表里一次只能进一张卡、发一句话、退出来、再进下一张；画布上一次选中 N 张，
  // 一句话同时下达。执行仍然是串行排队（系统的执行闸），但**下达**这个动作
  // 是一次性的，人不用重复 N 遍。
  //
  // 两个必须处理的细节：
  //   ① 正在跑的卡跳过——给在途的卡再发会 409，且语义上是"打断"，不是批量该做的事；
  //   ② 排队没有进度记录（progress 只在真正开跑时写），所以本地记一笔 queued，
  //      让画布显示「排队中」，否则用户以为指令丢了。
  async function batchCommand(ids) {
    const targets = (ids || []).filter((id) => state.index.cardMap[id]);
    if (!targets.length) return;
    const running = state.async.running || new Map();
    const skip = targets.filter((id) => running.has(id));
    const todo = targets.filter((id) => !running.has(id));
    if (!todo.length) { notify("选中的卡都在跑，等它们跑完再下指令", "warn"); return; }

    const text = await cvDialog({
      title: `给 ${todo.length} 张卡同时下指令`,
      lines: [
        "每条指令分别发给选中的每一张卡，在各自卡内的对话里执行。",
        skip.length ? `正在跑的 ${skip.length} 张会自动跳过（不打断在途任务）。` : "",
        "项目若是串行执行模式，会按顺序排队跑——画布上会标「排队中」。",
      ].filter(Boolean),
      input: true,
      defaultValue: "",
      okText: "发送",
    });
    if (!text || !text.trim()) return;
    const content = text.trim();

    if (!state.async.queued) state.async.queued = new Map();
    for (const cid of todo) state.async.queued.set(cid, Date.now());
    applyLiveStates();

    let failed = 0;
    for (const cid of todo) {
      // 不 await：串行闸下这个请求会一直挂到轮到它，await 会把界面锁住几分钟。
      API.postMessage(WORK_ID, cid, content).catch(() => {
        failed++;
        state.async.queued.delete(cid);
        applyLiveStates();
      });
    }
    notify(`已向 ${todo.length} 张卡下达指令${skip.length ? `（跳过 ${skip.length} 张在跑的）` : ""}`, "success");
    // 尽快把「排队中」换成「运行中」
    setTimeout(pollPulse, 900);
    setTimeout(pollPulse, 3000);
    if (failed) notify(`${failed} 张发送失败（可能已在途）`, "error");
  }

  // ══════════════════════════════════════════════════════════════════════════
  // 应用内弹窗（替代 window.prompt / window.confirm）
  // ──────────────────────────────────────────────────────────────────────────
  // [画布 2026-10-19 · 自查] 重命名用 window.prompt、删除用 window.confirm——
  // 这两个是**浏览器原生对话框**：不可换肤、不可本地化、会冻结主线程、
  // 长得像"网页在警告你"。整个产品其余部分都是深色定制界面，只有这两处露出
  // 浏览器原生 UI，一眼就是"半成品"。何况删除是不可撤销操作，更需要一个能写清
  // 后果、能把要删的卡名排版出来的对话框——原生 confirm 里那串卡名是一坨纯文本。
  //
  // 返回 Promise：输入框模式返回字符串（取消返回 null），确认模式返回 true/null。
  // ══════════════════════════════════════════════════════════════════════════
  function cvDialog({ title, lines = [], input = false, defaultValue = "",
                      okText = "确定", cancelText = "取消", danger = false }) {
    return new Promise((resolve) => {
      const mask = document.createElement("div");
      mask.className = "cv-modal-mask";
      const body = lines.length
        ? `<div class="cv-modal-body">${lines.map((l) => `<div>${escapeHtml(l)}</div>`).join("")}</div>`
        : "";
      mask.innerHTML = `
        <div class="cv-modal" role="dialog" aria-modal="true" aria-label="${escapeHtml(title)}">
          <div class="cv-modal-title">${escapeHtml(title)}</div>
          ${body}
          ${input ? `<input class="cv-modal-input" type="text" value="${escapeHtml(defaultValue)}" aria-label="${escapeHtml(title)}">` : ""}
          <div class="cv-modal-ops">
            <button class="cv-modal-btn" type="button" data-role="cancel">${escapeHtml(cancelText)}</button>
            <button class="cv-modal-btn ${danger ? "is-danger" : "is-primary"}" type="button" data-role="ok">${escapeHtml(okText)}</button>
          </div>
        </div>`;
      document.body.appendChild(mask);
      const inp = mask.querySelector(".cv-modal-input");
      const okBtn = mask.querySelector('[data-role="ok"]');
      const cancelBtn = mask.querySelector('[data-role="cancel"]');
      let done = false;
      const close = (v) => {
        if (done) return;
        done = true;
        document.removeEventListener("keydown", onKey, true);
        mask.remove();
        resolve(v);
      };
      const confirm = () => close(input ? (inp.value.trim() || null) : true);
      okBtn.onclick = confirm;
      cancelBtn.onclick = () => close(null);
      mask.addEventListener("mousedown", (e) => { if (e.target === mask) close(null); });
      const onKey = (e) => {
        // 捕获阶段拦下：弹窗开着时，Escape/Enter 不许漏到画布快捷键（否则
        // 关弹窗的同时把选中的卡也清了）。其余按键放行给输入框。
        if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); close(null); return; }
        if (e.key === "Enter") { e.stopPropagation(); e.preventDefault(); confirm(); return; }
        // [画布 2026-10-19 · 自查] 焦点陷阱：`aria-modal="true"` 承诺"模态"，
        // 但 Tab 仍会跑到背后的画布上——读屏用户会读出弹窗外的内容，
        // 键盘用户也会在弹窗开着的时候莫名其妙选中一张卡。这里把 Tab 圈在弹窗内。
        if (e.key === "Tab") {
          const f = [...mask.querySelectorAll('button, input, [tabindex]:not([tabindex="-1"])')]
            .filter((el) => el.offsetParent !== null);
          if (!f.length) return;
          e.preventDefault();
          e.stopPropagation();
          const i = f.indexOf(document.activeElement);
          const next = e.shiftKey
            ? (i <= 0 ? f.length - 1 : i - 1)
            : (i === f.length - 1 || i < 0 ? 0 : i + 1);
          f[next].focus();
        }
      };
      document.addEventListener("keydown", onKey, true);
      if (inp) { inp.focus(); inp.select(); } else okBtn.focus();
    });
  }

  // ══════════════════════════════════════════════════════════════════════════
  // 回收站面板
  // ──────────────────────────────────────────────────────────────────────────
  // [画布 2026-10-19 · 自查] 撤销条只活 12 秒——错过就再也找不回那张卡，
  // 而后端的回收站里它明明还在。等于"做了保险箱但没给钥匙"。
  // 这个面板是长期入口：列出最近删除的卡（最多 100 张），逐条恢复。
  // ══════════════════════════════════════════════════════════════════════════
  async function openTrash() {
    let items = [];
    try {
      items = (await API.getTrash(WORK_ID)).items || [];
    } catch (e) {
      notify("回收站读取失败", "error");
      return;
    }
    const mask = document.createElement("div");
    mask.className = "cv-modal-mask";
    const rows = items.length
      ? `<div class="cv-trash">${items.map((it) => `
          <div class="cv-trash-row" data-id="${it.card_id}">
            <div class="cv-trash-main">
              <div class="cv-trash-title">${escapeHtml(it.title || it.card_id)}</div>
              <div class="cv-trash-meta">${escapeHtml(it.card_id)} · 删除于 ${escapeHtml(String(it.deleted_at || "").replace("T", " "))}</div>
            </div>
            <button class="cv-modal-btn is-primary" type="button" data-restore="${it.card_id}">恢复</button>
          </div>`).join("")}</div>`
      : `<div class="cv-trash-empty">回收站是空的。删掉的卡会先在这里留一份，随时可以恢复。</div>`;
    mask.innerHTML = `
      <div class="cv-modal is-wide" role="dialog" aria-modal="true" aria-label="回收站">
        <div class="cv-modal-title">回收站 <span class="cv-trash-count">${items.length}</span></div>
        <div class="cv-modal-body">${rows}</div>
        <div class="cv-modal-ops"><button class="cv-modal-btn" type="button" data-role="close">关闭</button></div>
      </div>`;
    document.body.appendChild(mask);
    const close = () => { document.removeEventListener("keydown", onKey, true); mask.remove(); };
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); close(); }
    };
    document.addEventListener("keydown", onKey, true);
    mask.addEventListener("mousedown", (e) => { if (e.target === mask) close(); });
    mask.querySelector('[data-role="close"]').onclick = close;

    mask.querySelectorAll("[data-restore]").forEach((btn) => {
      btn.onclick = async () => {
        const cid = btn.dataset.restore;
        btn.disabled = true; btn.textContent = "…";
        try {
          await API.restoreCard(WORK_ID, cid);
          const row = mask.querySelector(`.cv-trash-row[data-id="${cid}"]`);
          if (row) row.remove();
          const left = mask.querySelectorAll(".cv-trash-row").length;
          const cnt = mask.querySelector(".cv-trash-count");
          if (cnt) cnt.textContent = String(left);
          if (!left) mask.querySelector(".cv-modal-body").innerHTML =
            `<div class="cv-trash-empty">回收站是空的。删掉的卡会先在这里留一份，随时可以恢复。</div>`;
          await reloadCanvas();
          notify("已恢复一张卡片");
        } catch (e) {
          btn.disabled = false; btn.textContent = "恢复";
          notify("恢复失败：" + (e && e.message ? e.message : e), "error");
        }
      };
    });
  }

  // ══════════════════════════════════════════════════════════════════════════
  // 并排比较（2–3 张卡）
  // ──────────────────────────────────────────────────────────────────────────
  // [画布 2026-10-19] 推演的本质是**同时推进几条分支**，最后要决定"收哪条"。
  // 决定的那一刻，人需要的是把两条分支各自"想清楚了什么"摆在一起看——
  // 而不是分别点开两张卡、记住左边那段话、再回来看右边。
  //
  // 为什么比较的是正文而不是"对话轮次"：时间线里 77% 是 reasoning（实测
  // card_069：257 条事件里 199 条是思考链）。把两条思考链按轮次对齐，得到的是
  // 两屏噪声。真正需要对照的是**各自沉淀下来的那段话**——它是这张卡对外的
  // 唯一接口，也是别的卡能读到的东西。
  // ══════════════════════════════════════════════════════════════════════════
  async function openCompare(ids) {
    const list = (ids || []).filter((id) => state.index.cardMap[id]).slice(0, 3);
    if (list.length < 2) { notify("先选中 2 张以上卡片再比较", "warn"); return; }

    const mask = document.createElement("div");
    mask.className = "cv-modal-mask";
    const cols = list.map((id) => {
      const c = state.index.cardMap[id] || {};
      const ws = String(c.work_state || "");
      const wsLabel = ws === "concluded" ? "已结论" : ws === "pending" ? `${c.pending_count || 0} 处待裁定`
        : ws === "empty" ? "未开始" : "讨论中";
      const ev = _evByCard.get(id);
      const meta = [
        Number(c.content_len || 0) > 0 ? `${c.content_len} 字` : "",
        Number(c.ref_count || 0) > 0 ? `${c.ref_count} 处引用` : "",
        ev && ev.ts ? fmtRelative(ev.ts) : (c.updated_at ? fmtRelative(c.updated_at) : ""),
      ].filter(Boolean).join(" · ");
      return `
        <div class="cmp-col" data-id="${id}">
          <div class="cmp-head">
            <div class="cmp-title">${escapeHtml(c.title || id)}</div>
            <span class="cmp-ws" data-ws="${ws}">${wsLabel}</span>
          </div>
          <div class="cmp-meta">${escapeHtml(meta)}</div>
          <div class="cmp-body" data-id="${id}">正在读取…</div>
          <div class="cmp-ops">
            <button class="cv-modal-btn is-primary" type="button" data-act="open" data-id="${id}">开对话</button>
          </div>
        </div>`;
    }).join("");

    mask.innerHTML = `
      <div class="cv-modal is-compare" role="dialog" aria-modal="true" aria-label="并排比较">
        <div class="cv-modal-title">并排比较 ${list.length} 张卡
          <span class="cmp-hint">看的是各自沉淀下来的那段正文，不是思考过程</span>
        </div>
        <div class="cmp-grid">${cols}</div>
        <div class="cv-modal-ops"><button class="cv-modal-btn" type="button" data-role="close">关闭</button></div>
      </div>`;
    document.body.appendChild(mask);

    const close = () => { document.removeEventListener("keydown", onKey, true); mask.remove(); };
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); close(); } };
    document.addEventListener("keydown", onKey, true);
    mask.addEventListener("mousedown", (e) => { if (e.target === mask) close(); });
    mask.querySelector('[data-role="close"]').onclick = close;
    mask.querySelectorAll('[data-act="open"]').forEach((b) => {
      b.onclick = () => { close(); openCardWatch(b.dataset.id); };
    });

    // 正文按列异步补：三列并行取，谁先回来谁先填，不阻塞其余两列。
    //
    // 这里取的是**整段正文**（status_block），不是 getSummary 那段一两百字的摘要。
    // 摘要够"看一眼是什么"，不够"比较"。要判断该收哪条分支，得看两边各自
    // 把事情说到了哪一步——那正是这 2000 多字里的东西。
    for (const id of list) {
      const cell = mask.querySelector(`.cmp-body[data-id="${id}"]`);
      API.getCard(WORK_ID, id).then((data) => {
        if (!cell || !cell.isConnected) return;
        const md = String((data && data.body && data.body.status_block) || "").trim();
        if (!md) { cell.textContent = "（这张卡还没有正文）"; return; }
        cell.innerHTML = `<div class="md">${renderMarkdown(md)}</div>`;
      }).catch(() => {
        if (cell && cell.isConnected) cell.textContent = "（读取失败）";
      });
    }
  }

  // ---- 重命名：画布内直接改名（不跳对话页）----
  async function renameCard(cardId) {
    const card = state.index.cardMap[cardId] || {};
    const next = await cvDialog({
      title: "重命名卡片",
      lines: ["改名只动标题，卡片内容、位置、子卡都不变。"],
      input: true,
      defaultValue: card.title || "",
      okText: "保存",
    });
    if (next === null) return;
    const title = next.trim();
    if (!title || title === card.title) return;
    API.patchCard(WORK_ID, cardId, { title }, "画布内重命名")
      .then(() => reloadCanvas())
      .then(() => notify("已重命名"))
      .catch((err) => notify("重命名失败：" + (err && err.message ? err.message : err), "error"));
  }

  // ══════════════════════════════════════════════════════════════════════════
  // 卡片就地展开成"工作单元"
  // ──────────────────────────────────────────────────────────────────────────
  // [画布 2026-10-19 · 用户令] 卡片本身就是对话单元（每张卡自带 session）。
  // 所以"同时看几条线并回复"不该再开一个浮层——**卡片自己长大**：
  //   折叠＝一行索引；展开＝这张卡的对话 + 输入框，就地。
  // 多张同时展开，就是一屏多个对话，每个都能直接回。不需要第二个容器。
  // ══════════════════════════════════════════════════════════════════════════
  async function toggleCell(cardId) {
    const node = nodeMap.get(cardId);
    if (!node) return;
    const open = node.dataset.open === "1";
    if (open) {
      node.dataset.open = "";
      state.view.openCells.delete(cardId);
      const cell = node.querySelector(".cv-cell");
      if (cell) { cell.innerHTML = ""; cell.dataset.loaded = ""; }
      scheduleReflow();
      return;
    }
    node.dataset.open = "1";
    state.view.openCells.add(cardId);
    const cell = node.querySelector(".cv-cell");
    if (!cell) return;
    cell.innerHTML = `<div class="cv-cell-empty">正在读取对话…</div>`;
    await loadCell(cardId);
    scheduleReflow();
    const inp = node.querySelector(".cv-cell-input");
    if (inp) inp.focus();
  }

  async function loadCell(cardId) {
    const node = nodeMap.get(cardId);
    if (!node) return;
    const cell = node.querySelector(".cv-cell");
    if (!cell) return;
    try {
      const d = await API.getTimeline(WORK_ID, cardId);
      const lines = (d.items || []).slice(-12).map(watchLineOf).filter(Boolean);
      cell.innerHTML = `
        <div class="cv-cell-log">${
          lines.length
            ? lines.map((l) => `<div class="cv-cell-line" data-role="${l.role}"><span class="cv-cell-tag">${l.label}</span><span class="cv-cell-text">${escapeHtml(l.text.slice(0, 300))}</span></div>`).join("")
            : `<div class="cv-cell-empty">还没有对话。在下面说第一句。</div>`
        }</div>
        <div class="cv-cell-foot">
          <input class="cv-cell-input" type="text" placeholder="回复这张卡…（Enter 发送）">
          <button class="cv-cell-send" type="button">发送</button>
        </div>`;
      const log = cell.querySelector(".cv-cell-log");
      if (log) log.scrollTop = log.scrollHeight;
      const input = cell.querySelector(".cv-cell-input");
      const btn = cell.querySelector(".cv-cell-send");
      const send = async () => {
        const text = input.value.trim();
        if (!text) return;
        input.value = "";
        input.disabled = true; btn.disabled = true; btn.textContent = "…";
        markLocalRunning(cardId, "等待执行");
        try {
          await API.postMessage(WORK_ID, cardId, text);
          await loadCell(cardId);
          setTimeout(() => reloadCanvas(), 700);
        } catch (e) {
          notify("发送失败：" + (e && e.message ? e.message : e), "error");
          input.value = text;
        } finally {
          input.disabled = false; btn.disabled = false; btn.textContent = "发送";
          clearLocalRunning(cardId);
          input.focus();
        }
      };
      btn.onclick = send;
      input.onkeydown = (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.stopPropagation(); send(); }
        if (e.key === "Escape") { e.stopPropagation(); toggleCell(cardId); }
      };
    } catch (e) {
      cell.innerHTML = `<div class="cv-cell-empty">读取失败</div>`;
    }
  }

  // 在并行层打开这张卡的对话（并排比较弹窗里的「开对话」按钮走这条）
  function openCardWatch(cardId) {
    if (!cardId) return;
    selectOnly(cardId);
    watchAdd(cardId);
  }

  // 打开这张卡的完整对话页（↗ / 右键菜单用；需要大屏长文时才走这条）
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

  // ══════════════════════════════════════════════════════════════════════════
  // [画布 2026-10-18] 迭代式整幅适配
  //
  // 为什么要迭代：卡片高度**随 LOD 档位变**（骨架 75 / 完整 150），而 fitView 按
  // 当前 DOM 高度算 zoom，zoom 又决定档位 ⇒ 直接 fit 会算错。
  // 实测：9 张卡按 150 高算出 0.55，切到骨架档后其实装得下 0.95，画面却停在 0.55。
  // 解法：先按目标 zoom 刷 LOD → **强制一次重排**（`void world.offsetHeight`，
  // 否则同一同步任务里 offsetHeight 还是旧值）→ 重算，最多迭代 3 次。
  // ══════════════════════════════════════════════════════════════════════════
  function fitToContent() {
    if (!nodeMap.size) return false;
    // [画布 2026-10-18] 整幅适配＝**根卡居中 + 固定 90%**。
    //
    // 不用"按 bbox 居中"：实测 work_001 上算出的 panY 让卡片跑到视口外（y=-64），
    // 因为 bbox 用的是 l1 尺寸而实际渲染是 l0，两者不一致。
    // 根卡是布局原点（子卡以它为锚向右上下排），居中它就能看到整棵树。
    //
    // 0.9 是量出来的：骨架档 9 个节点（根 70 + 8×56 + 8×40 间距）≈ 838px，
    // 0.9 下 754px，落在可用高度 840 内；标题 13×0.9 ≈ 11.7px、结论 11×0.9 ≈ 9.9px。
    const Z = 0.9;
    // 按**当前 DOM 的实际 bbox** 居中（此时已经渲染成骨架档，尺寸是真的）。
    // 不要用根卡居中：根卡在左、子卡在右，居中根卡会让整体偏左、右侧空一大片。
    const nodes = [...world.querySelectorAll(".card-node")];
    if (!nodes.length) return false;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes) {
      const x = parseFloat(n.style.left) || 0;
      const y = parseFloat(n.style.top) || 0;
      minX = Math.min(minX, x);
      minY = Math.min(minY, y);
      maxX = Math.max(maxX, x + n.offsetWidth);
      maxY = Math.max(maxY, y + n.offsetHeight);
    }
    // [画布 2026-10-19] 用满可用空间，并且**跑两遍**。
    //
    // 一遍不够：第一次量到的 bbox 是"当前档位"的卡高，应用 zoom 后档位可能变、
    // 卡高跟着变（虽然 l0/l1 已统一，字体/内边距仍随档位变），第二遍才是真实值。
    // 两遍足够收敛：高度已不跨档跳变，第三遍的差异 < 1%。
    // 下限 0.9 是硬线——卡多时缩到看不清，不如让人滚动。
    const pad = 40;
    const availW = Math.max(80, viewport.clientWidth - pad * 2);
    const availH = Math.max(80, viewport.clientHeight - pad * 2);
    let z = 0.9;
    for (let pass = 0; pass < 2; pass++) {
      const ns = [...world.querySelectorAll(".card-node")];
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of ns) {
        const nx = parseFloat(n.style.left) || 0, ny = parseFloat(n.style.top) || 0;
        x0 = Math.min(x0, nx); y0 = Math.min(y0, ny);
        x1 = Math.max(x1, nx + n.offsetWidth); y1 = Math.max(y1, ny + n.offsetHeight);
      }
      if (!Number.isFinite(x0)) break;
      const fitZ = Math.min(availW / Math.max(1, x1 - x0), availH / Math.max(1, y1 - y0));
      // [画布 2026-10-19] 不再有 0.9 硬线。
      // 原来的理由是"卡多时缩到看不清，不如让人滚动"——但那条理由只在**文字没有
      // 屏幕下限**时成立。现在标题有 `max(13px, 11px/zoom)` 的下限，0.55 时屏幕上
      // 仍是 11px：装得下就装下，比"只看到半棵树"有用。真正看不清的档位（<0.42）
      // 由 lm 档接住——那里本来就不画字。
      z = Math.max(0.25, Math.min(1.02, fitZ));
      applyViewState({
        zoom: z,
        panX: Math.round(viewport.clientWidth / 2 - ((x0 + x1) / 2) * z),
        panY: Math.round(viewport.clientHeight / 2 - ((y0 + y1) / 2) * z),
      });
      applyLODClasses(z, true);
      // 关键：只 `void world.offsetHeight` 不够——那只强制样式重算，
      // **卡片位置（间距）是 reflowAll 才算的**。实测：fit 时量到 bbox 高 955，
      // 布局稳定后实际只有 856，差值全在间距里，于是适配永远停在 0.91 而不是 ~1.0。
      reflowAll();
      void world.offsetHeight;
    }
    scheduleSaveView();
    return true;
  }

  // ── 只适配指定的几张卡（"自上次以来变过的"）──
  function fitToCards(ids, Z = 0.9) {
    const nodes = (ids || []).map((id) => nodeMap.get(id)).filter(Boolean);
    if (!nodes.length) return false;
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
    applyViewState({
      zoom: Z,
      panX: Math.round(viewport.clientWidth / 2 - cx * Z),
      panY: Math.round(viewport.clientHeight / 2 - cy * Z),
    });
    scheduleSaveView();
    return true;
  }

  // ══════════════════════════════════════════════════════════════════════════
  // 接续：自上次打开以来发生了什么
  // ──────────────────────────────────────────────────────────────────────────
  // 这个项目跨天推进（39 张卡、5 天）。每次回到画布，人真正要回答的第一个问题是
  // "我上次走之后，哪里变了"——现在只能靠把 39 张卡重新扫一遍，而卡面已经不显示
  // 时间了（时间不改变动作，删了）。所以这件事必须在**项目级**回答一次，而不是
  // 挂在每张卡上：一行增量，点一下只看那几张。
  //
  // 存两个数：上次访问的时间戳、上次的总待裁定数。前者算"哪几张更新过"，
  // 后者算"+几处待裁定"。写回时机＝离开页面（beforeunload），这样刷新一次
  // 不会把增量清掉——刷新完你还想看"刚才那批"。
  // ══════════════════════════════════════════════════════════════════════════
  const LS_VISIT = `projLastVisit:${WORK_ID}`;
  let _catchup = null;   // {since, updated:[ids], pendingDelta, newCards}

  function readLastVisit() {
    try {
      const raw = localStorage.getItem(LS_VISIT);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  function computeCatchup() {
    const prev = readLastVisit();
    const cards = (state.data.cards || []).filter((c) => c.card_type !== "rule");
    const pendingNow = cards.reduce((a, c) => a + Number(c.pending_count || 0), 0);
    if (!prev || !prev.ts) {
      _catchup = null;
      return;
    }
    const since = Date.parse(prev.ts);
    if (!Number.isFinite(since)) { _catchup = null; return; }
    // ══════════════════════════════════════════════════════════════════════
    // [画布 2026-10-19 · 按使用场景重做] 这一行要回答的是**早上打开时那句话**：
    // "我不在的时候，出了什么事？"
    //
    // 原来它只说"N 张更新"，而 N 是从 updated_at 数出来的——**"有人改过"**。
    // 对一个"AI 自己推进任务"的系统，这是反的：你不在的时候真正发生的是
    // 「哪条跑完了」「哪条卡住了」「哪条又攒了活等你」，不是"谁动过这张卡"。
    // 那三类在历史行里有明确痕迹（update_goal 落的是"模型声明 complete/blocked/pause"），
    // 分类一下就能说出来。
    // ══════════════════════════════════════════════════════════════════════
    const KIND_OF = (detail) => {
      const d = String(detail || "");
      if (/模型声明\s*complete|完工|report_done/.test(d)) return "done";
      if (/模型声明\s*blocked|卡住|做不下去/.test(d)) return "blocked";
      if (/模型声明\s*pause|暂停/.test(d)) return "paused";
      if (/收口|定论|裁定/.test(d)) return "decided";
      if (/卡片创建|新建/.test(d)) return "created";
      return "edited";
    };
    const updated = [];
    const counts = { done: 0, blocked: 0, paused: 0, decided: 0, created: 0, edited: 0 };
    for (const c of cards) {
      const t = Date.parse(String(c.updated_at || ""));
      if (!Number.isFinite(t) || t <= since) continue;
      const ev = _evByCard.get(c.card_id);
      const kind = KIND_OF(ev && ev.detail);
      updated.push({ id: c.card_id, kind, detail: (ev && ev.detail) || "", ts: (ev && ev.ts) || c.updated_at });
      counts[kind] = (counts[kind] || 0) + 1;
    }
    _catchup = {
      since,
      updated,
      counts,
      pendingDelta: pendingNow - Number(prev.pending || 0),
    };
    // 只读探针：供验收/取证（不改任何语义）
    window.__catchup = () => _catchup;
  }

  // 时间点的说法要跟间隔匹配：同一天说"14:30"，跨天说"10/16"，跨年说"2025/10/16"。
  // 只写"10/18"会让"自 10/18 以来"看起来像隔了很久，其实可能只是半小时前。
  function fmtSince(ms) {
    const d = new Date(ms), now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    if (sameDay) return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
    if (d.getFullYear() === now.getFullYear()) {
      return d.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
    }
    return d.toLocaleDateString("zh-CN", { year: "numeric", month: "numeric", day: "numeric" });
  }

  // 把一批卡"露出来"：展开它们的所有祖先，使卡片真正出现在画布上。
  // 不这么做的话，选中一个在折叠分支里的卡 = 什么都不会发生（它不在 DOM 上），
  // 实测就是"点了没反应"。
  function revealCards(ids) {
    const need = new Set();
    for (const id of ids) {
      let cur = (state.index.cardMap[id] || {}).parent;
      while (cur && cur !== state.nav.viewRoot) {
        need.add(cur);
        cur = (state.index.cardMap[cur] || {}).parent;
      }
    }
    let changed = false;
    for (const id of need) {
      if (!state.view.expanded.has(id)) { state.view.expanded.add(id); changed = true; }
    }
    if (changed) { scheduleSaveView(); renderAll(); }
  }

  function markVisit() {
    try {
      const cards = (state.data.cards || []).filter((c) => c.card_type !== "rule");
      localStorage.setItem(LS_VISIT, JSON.stringify({
        ts: new Date().toISOString(),
        pending: cards.reduce((a, c) => a + Number(c.pending_count || 0), 0),
      }));
    } catch (e) { /* 隐私模式下不可写，零阻断 */ }
  }

  // ── 接续清单：悬停"自上次以来 N 张更新"时展开，逐条说明改了什么 ──
  let catchupEl = null;
  let catchupHideTimer = 0;

  function hideCatchupList() {
    clearTimeout(catchupHideTimer);
    catchupHideTimer = setTimeout(() => {
      if (catchupEl) { catchupEl.remove(); catchupEl = null; }
    }, 160);
  }

  function showCatchupList(anchorEl) {
    clearTimeout(catchupHideTimer);
    if (catchupEl) catchupEl.remove();
    const cu = _catchup;
    if (!cu || !cu.updated.length) return;
    // updated 现在是 {id, kind, detail, ts}——先按"出事的"排前面：
    // 卡住的 > 跑完的 > 出定论的 > 其它。用户点开这一行，最想知道的是坏消息。
    const ORDER = { blocked: 0, done: 1, decided: 2, paused: 3, created: 4, edited: 5 };
    const KIND_WORD = { blocked: "卡住", done: "跑完", decided: "出定论", paused: "暂停", created: "新建", edited: "改动" };
    const rows = [...cu.updated]
      .sort((a, b) => (ORDER[a.kind] ?? 9) - (ORDER[b.kind] ?? 9))
      .slice(0, 8)
      .map((it) => {
        const c = state.index.cardMap[it.id] || {};
        return `<div class="cu-row" data-id="${it.id}">
          <span class="cu-kind" data-k="${it.kind}">${KIND_WORD[it.kind] || "改动"}</span>
          <span class="cu-title">${escapeHtml(c.title || it.id)}</span>
          <span class="cu-detail">${escapeHtml(it.detail || "有更新")}</span>
          <span class="cu-time">${it.ts ? escapeHtml(fmtRelative(it.ts)) : ""}</span>
        </div>`;
      }).join("");
    const more = cu.updated.length > 8 ? `<div class="cu-more">还有 ${cu.updated.length - 8} 张</div>` : "";
    const el = document.createElement("div");
    el.className = "cv-catchup-list";
    el.innerHTML = `<div class="cu-head">自 ${escapeHtml(fmtSince(cu.since))} 以来</div>${rows}${more}`;
    document.body.appendChild(el);
    const r = anchorEl.getBoundingClientRect();
    el.style.left = Math.max(12, Math.min(r.left, window.innerWidth - el.offsetWidth - 12)) + "px";
    el.style.top = r.bottom + 6 + "px";
    // 鼠标能移进清单（否则读不完就消失）
    el.addEventListener("mouseenter", () => clearTimeout(catchupHideTimer));
    el.addEventListener("mouseleave", hideCatchupList);
    el.querySelectorAll(".cu-row").forEach((row) => {
      row.addEventListener("click", () => {
        hideCatchupList();
        const id = row.dataset.id;
        revealCards([id]);
        setSelection([id]);
        focusCard(id);
      });
    });
    catchupEl = el;
  }

  // [画布 5711 · P2-3 修] **可见集单一来源**（V3 不变量）。
  // 原来这里返回视图根的**全部后代**（不看 expanded），唯一使用点是"整理画布"
  // —— 于是折叠着的分支里的卡也被重排/解钉，而它们在画布上根本看不见
  // （用户以为没动的卡，位置被改了）。
  // 现在用投影里的 visibleIds（computeVisibleSet 的产物，与渲染同一份）。
  function getVisibleIds() {
    if (lastProjection && lastProjection.visibleIds) {
      return new Set(lastProjection.visibleIds);
    }
    // 兜底（还没有投影时）：按展开态算一次，不返回"全部后代"。
    const ids = new Set([state.nav.viewRoot]);
    const stack = [state.nav.viewRoot];
    while (stack.length) {
      const id = stack.pop();
      if (!state.view.expanded.has(id)) continue;
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
    // [画布 5711 · P1-6 修] 连线按父卡深度分层的样式**从未生效**。
    // 病根：深度表读的是 `vm.depthOf`，而 collectViewModel() 里**根本没有这个字段**
    // ⇒ vm.depthOf 恒 undefined ⇒ `d` 恒为 0 ⇒ 所有边都走 else 分支（同一权重）。
    // 而 computeProjection 的返回值里**本来就带 depthOf**（computeVisibleSet 的产物）
    // —— 一直都在，只是没人读。
    // 后果：全展开时 200+ 条边同权重，糊成一片（这正是这段注释想避免的）。
    // 修法：读 projection.depthOf（同一份投影、同一轮、同一个可见集）。
    const depthOf = projection.depthOf || (vm && vm.depthOf) || {};
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
      const d = depthOf[seg.from] || 0;
      if (d >= 2) { path.setAttribute("stroke-width", "1"); path.setAttribute("opacity", "0.4"); }
      else if (d >= 1) { path.setAttribute("stroke-width", "1.1"); path.setAttribute("opacity", "0.6"); }
      else { path.setAttribute("opacity", "0.7"); }
      pcFrag.appendChild(path);
    }
    pcG.appendChild(pcFrag);
    console.info(`[T08] 父子线：绘制 ${elbowSegs.length} 条（可见卡 ${visibleIds.size} 张）`);

    // ══════════════════════════════════════════════════════════════════════
    // [画布 2026-10-19 · 用户令] 连线分三层，各自回答不同的问题：
    //
    //   第一层 父子线（上面 pcG）—— **常驻**。"这张卡在结构里的哪里"
    //   第二层 通信线（下面 commG）—— **只在通信时出现**。"刚才谁给谁递了东西"
    //   第三层 关系线（下面 relG）—— **默认关，按钮手动开**。"这两张卡论证上有关联"
    //
    // 为什么必须分：三层同时常驻时，200 张卡的画布上会出现几百条同权重的线，
    // 糊成一片电线——读者反而看不出结构。结构（父子）是底图，事件（通信）是
    // 瞬时高亮，论证（关系）是按需查证。三者信息性质不同，出现时机就不该相同。
    // ══════════════════════════════════════════════════════════════════════

    // ── 第二层：通信线（卡间信 from_card → to_card，只在时间窗内画）──
    const commG = document.createElementNS(SVGNS, "g");
    commG.setAttribute("data-edge-group", "communication");
    edgesSvg.appendChild(commG);
    // 通信是**事件**不是结构：30 分钟前的信不再画线，只留收件箱里的记录。
    // 这样画布上出现的每一条通信线都意味着"刚刚发生了信息流动"。
    const COMM_WINDOW_MS = 30 * 60 * 1000;
    const nowTs = Date.now();
    const fresh = (vm.cardMsgs || []).filter((m) => {
      const t = Date.parse(String(m.ts || ""));
      return Number.isFinite(t) && nowTs - t < COMM_WINDOW_MS
        && visibleIds.has(m.from_card) && visibleIds.has(m.to_card)
        && layout[m.from_card] && layout[m.to_card];
    });
    for (const m of fresh) {
      const p1 = layout[m.from_card], p2 = layout[m.to_card];
      const s1 = nodeSize(m.from_card), s2 = nodeSize(m.to_card);
      const x1 = p1.x + s1.w / 2, y1 = p1.y + s1.h / 2;
      const x2 = p2.x + s2.w / 2, y2 = p2.y + s2.h / 2;
      // 曲线而不是直线：通信线要能穿过中间的卡而不被误读成父子线
      const mx = (x1 + x2) / 2, my = (y1 + y2) / 2 - Math.min(90, Math.abs(x2 - x1) * 0.22 + 30);
      const path = document.createElementNS(SVGNS, "path");
      path.setAttribute("d", `M ${x1} ${y1} Q ${mx} ${my} ${x2} ${y2}`);
      path.setAttribute("class", "cv-comm-line");
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", "#5b8cff");
      path.setAttribute("stroke-width", "1.8");
      path.setAttribute("stroke-dasharray", "7 6");
      path.setAttribute("stroke-linecap", "round");
      commG.appendChild(path);
      // 端点：收件方一个实心点，表示"信落到这里"
      const dot = document.createElementNS(SVGNS, "circle");
      dot.setAttribute("cx", x2); dot.setAttribute("cy", y2);
      dot.setAttribute("r", "4");
      dot.setAttribute("class", "cv-comm-dot");
      dot.setAttribute("fill", "#5b8cff");
      commG.appendChild(dot);
    }

    // ── 第三层：关系线组（默认不画，按钮开）──
    const relG = document.createElementNS("http://www.w3.org/2000/svg", "g");
    relG.setAttribute("data-edge-group", "links");
    relG.style.display = vm.showRelations ? "" : "none";
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
        relG.appendChild(path);   // [画布 2026-10-19] 归入第三层，随关系线开关一起显隐
        const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        circle.setAttribute("cx", midX);
        circle.setAttribute("cy", midY);
        circle.setAttribute("r", "5");
        circle.setAttribute("stroke", "#f85149");
        circle.setAttribute("stroke-width", "1.5");
        circle.setAttribute("fill", "none");
        relG.appendChild(circle);
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
        // [画布 2026-10-19] 徽章挂进底栏，不再挂在卡片外沿。
        // 原来 append 到 node 上、样式是 top:-8px;right:-8px，等于刻意伸出卡外 8px。
        // 卡与卡之间只有 20px 间距，伸出去就压到邻居；全量验收里它被判成"超框"
        // （L100 R4 T-3 B-19）。🔗 表达的是"这张卡有关系指向当前视图之外"，
        // 和 ←N（被引用数）、▸N（子卡数）是同一类信息，本来就该并排放在底栏。
        (node.querySelector(".cv-card-row") || node).appendChild(badge);
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
  // ══════════════════════════════════════════════════════════════════════════
  // [画布 2026-10-18 · 修"文字显示不全"] hover 浮层给**完整内容**
  //
  // 卡片是索引：骨架档下标题 1 行、结论 1 行，长文本必然截断——截断本身没错
  // （39 张卡不可能每张都摊开），错的是**截断后没有地方读全文**。
  // 浮层只放卡面装不下的东西（见下方实现里的四条）。
  // ══════════════════════════════════════════════════════════════════════════
  // ══════════════════════════════════════════════════════════════════════════
  // 悬停提示：只剩"折叠分支里有什么"
  // ──────────────────────────────────────────────────────────────────────────
  // [画布 2026-10-19 · 用户令] 原来整张卡悬停就弹一块 420×620 的纯文字浮层，
  // 把底下的卡片全遮住，内容还和卡面重复。现在：
  //   卡片悬停 → 什么都不弹（要看对话就点 💬 / 右键「在此展开对话」/ 按 Enter）
  //   徽章 ▸N 悬停 → 只列里面几张卡的名字 + 几张有活
  // 这是卡面确实没有的信息，而且只在"要不要展开"这一刻才需要。
  // ══════════════════════════════════════════════════════════════════════════
  function showTooltip(card, node) {
    const ws = String(card.work_state || "");
    const stateLabel = ws === "concluded" ? "已结论"
      : ws === "pending" ? "在等裁定"
      : ws === "empty" ? "还没开始"
      : "讨论中";

    const metaBits = [];
    if (Number(card.content_len || 0) > 0) metaBits.push(`${card.content_len} 字`);
    if (Number(card.ref_count || 0) > 0) metaBits.push(`${card.ref_count} 处引用`);
    const _ev = _evByCard.get(card.card_id);
    if (_ev && _ev.ts) metaBits.push(`${fmtRelative(_ev.ts)} · ${_ev.detail || "有更新"}`);
    else if (card.updated_at) metaBits.push(fmtRelative(card.updated_at));

    tooltip.innerHTML = `
      <div class="tt-meta">
        <span class="tt-ws" data-ws="${ws}">${stateLabel}</span>
        ${metaBits.map((b) => `<span class="tt-dot">·</span><span>${escapeHtml(b)}</span>`).join("")}
        <button class="tt-pin" type="button" title="钉在画布右侧（可同时钉多个）" aria-label="钉在画布右侧">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 2.5l4 4-2 1-3.2 3.2.2 2.3-1.4 1.4-4.5-4.5 1.4-1.4 2.3.2L9.5 6l1-2z"/><path d="M5.5 10.5L2 14"/></svg>
        </button>
      </div>
      <div class="tt-conv" data-cid="${card.card_id}"><div class="tt-conv-empty">正在读取…</div></div>
      <div class="tt-reply">
        <input class="tt-input" type="text" placeholder="回复这张卡…（Enter 发送）" aria-label="回复这张卡">
        <button class="tt-send" type="button">发送</button>
      </div>
    `;
    tooltip.dataset.cid = card.card_id;
    tooltip.classList.add("active");
    markCardRead(card.card_id, (card.last_msg || {}).ts);

    const convEl = tooltip.querySelector(".tt-conv");
    const cid = card.card_id;
    const loadConv = async () => {
      try {
        const d = await API.getTimeline(WORK_ID, cid);
        const items = (d.items || []).map(watchLineOf).filter(Boolean);
        let lastHuman = -1;
        for (let i = items.length - 1; i >= 0; i--) {
          if (items[i].role === "you") { lastHuman = i; break; }
        }
        const round = (lastHuman >= 0 ? items.slice(lastHuman) : items.slice(-2))
          .filter((x) => x.role === "you" || x.role === "ai");
        if (!tooltip.classList.contains("active") || tooltip.dataset.cid !== cid) return;
        convEl.innerHTML = round.length
          ? round.map((l) => `<div class="tt-line" data-role="${l.role}"><span class="tt-tag">${l.label}</span><span class="tt-text">${escapeHtml(l.text)}</span></div>`).join("")
          : `<div class="tt-conv-empty">还没有对话。在下面说第一句。</div>`;
        convEl.scrollTop = 0;
        placeTooltip(node);
      } catch (e) {
        if (convEl) convEl.innerHTML = `<div class="tt-conv-empty">读取失败</div>`;
      }
    };

    function placeTooltip(anchor) {
      const r = anchor.getBoundingClientRect();
      tooltip.style.left = "-9999px";
      tooltip.style.top = "0px";
      const W = tooltip.offsetWidth;
      const H = tooltip.offsetHeight;
      const tx = r.right + 10;
      const left = tx + W > window.innerWidth - 12 ? Math.max(12, r.left - W - 10) : tx;
      const top = Math.max(56, Math.min(r.top, window.innerHeight - H - 12));
      tooltip.style.left = left + "px";
      tooltip.style.top = top + "px";
    }
    placeTooltip(node);
    loadConv();

    tooltip.querySelector(".tt-pin").onclick = (e) => {
      e.stopPropagation();
      e.preventDefault();
      dockAdd(cid);
      hideTooltip();
    };

    const input = tooltip.querySelector(".tt-input");
    const sendBtn = tooltip.querySelector(".tt-send");
    const doSend = async () => {
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      input.disabled = true; sendBtn.disabled = true; sendBtn.textContent = "…";
      markLocalRunning(cid, "等待执行");
      try {
        await API.postMessage(WORK_ID, cid, text);
        await loadConv();
        setTimeout(() => reloadCanvas(), 700);
      } catch (err) {
        notify("发送失败：" + (err && err.message ? err.message : err), "error");
        input.value = text;
      } finally {
        input.disabled = false; sendBtn.disabled = false; sendBtn.textContent = "发送";
        clearLocalRunning(cid);
        input.focus();
      }
    };
    sendBtn.onclick = doSend;
    input.onkeydown = (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.stopPropagation(); doSend(); }
    };
  }

  const dock = { ids: [] };

  function dockRoot() {
    let el = document.getElementById("cvDock");
    if (!el) {
      el = document.createElement("div");
      el.id = "cvDock";
      el.className = "cv-dock";
      document.body.appendChild(el);
    }
    return el;
  }

  function dockSync() {
    document.body.classList.toggle("has-dock", dock.ids.length > 0);
    setTimeout(() => { if (nodeMap.size) fitToContent(); }, 260);
  }

  function dockAdd(cid) {
    if (!cid || !state.index.cardMap[cid]) return;
    if (!dock.ids.includes(cid)) dock.ids.push(cid);
    renderDock();
    dockSync();
  }

  function dockRemove(cid) {
    dock.ids = dock.ids.filter((x) => x !== cid);
    renderDock();
    dockSync();
  }

  async function dockLoad(cid, bodyEl) {
    try {
      const d = await API.getTimeline(WORK_ID, cid);
      const items = (d.items || []).map(watchLineOf).filter(Boolean);
      let lastHuman = -1;
      for (let i = items.length - 1; i >= 0; i--) {
        if (items[i].role === "you") { lastHuman = i; break; }
      }
      const round = (lastHuman >= 0 ? items.slice(lastHuman) : items.slice(-2))
        .filter((x) => x.role === "you" || x.role === "ai");
      if (!bodyEl.isConnected) return;
      bodyEl.innerHTML = round.length
        ? round.map((l) => `<div class="tt-line" data-role="${l.role}"><span class="tt-tag">${l.label}</span><span class="tt-text">${escapeHtml(l.text)}</span></div>`).join("")
        : `<div class="tt-conv-empty">还没有对话。在下面说第一句。</div>`;
      bodyEl.scrollTop = bodyEl.scrollHeight;
    } catch (e) {
      if (bodyEl.isConnected) bodyEl.innerHTML = `<div class="tt-conv-empty">读取失败</div>`;
    }
  }

  function renderDock() {
    const el = dockRoot();
    if (!dock.ids.length) { el.innerHTML = ""; return; }
    el.innerHTML = dock.ids.map((cid) => {
      const c = state.index.cardMap[cid] || {};
      return `<div class="cv-dock-panel" data-cid="${escapeHtml(cid)}">
        <div class="cv-dock-head">
          <span class="cv-dock-title">${escapeHtml(c.title || cid)}</span>
          <button class="cv-dock-close" type="button" aria-label="取消钉住">×</button>
        </div>
        <div class="cv-dock-body"></div>
        <div class="cv-dock-reply">
          <input class="cv-dock-input" type="text" placeholder="回复这张卡…" aria-label="回复这张卡">
          <button class="cv-dock-send" type="button">发送</button>
        </div>
      </div>`;
    }).join("");

    el.querySelectorAll(".cv-dock-panel").forEach((panel) => {
      const cid = panel.dataset.cid;
      const body = panel.querySelector(".cv-dock-body");
      const input = panel.querySelector(".cv-dock-input");
      const send = panel.querySelector(".cv-dock-send");
      panel.querySelector(".cv-dock-close").onclick = () => dockRemove(cid);
      const doSend = async () => {
        const text = input.value.trim();
        if (!text) return;
        input.value = "";
        input.disabled = true; send.disabled = true; send.textContent = "…";
        markLocalRunning(cid, "等待执行");
        try {
          await API.postMessage(WORK_ID, cid, text);
          await dockLoad(cid, body);
        } catch (e) {
          notify("发送失败：" + (e && e.message ? e.message : e), "error");
          input.value = text;
        } finally {
          input.disabled = false; send.disabled = false; send.textContent = "发送";
          clearLocalRunning(cid);
        }
      };
      send.onclick = doSend;
      input.onkeydown = (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.stopPropagation(); doSend(); }
      };
      dockLoad(cid, body);
    });
  }

  function hideTooltip() {
    tooltip.classList.remove("active");
  }

  // ⚠ [画布 2026-10-19] hideTimer 必须声明在**模块作用域**，不能放在 bindNodeEvents 里。
  // 原来它写在 bindNodeEvents（每张卡各一个闭包），而下面浮层自己的 mouseenter
  // 在模块作用域引用同一个名字 ⇒ 鼠标一移进浮层就 `ReferenceError: hideTimer is
  // not defined`。表现是"浮层能显示、但鼠标挪进去就报错且立刻消失"——也就是
  // 可滚动/可选中这段功能从来没生效过。卡与浮层共用同一个定时器，语义也才对。
  let hideTimer = 0;

  // （bodyCache 已随浮层改为"对话视图"退役：浮层不再取正文）
  // cid -> [{id,kind,text}]：从正文里解析出的定论条目
  const decCache = new Map();
  // ⚠ 排除骨架占位行 `D01【裁定】……（日期·依据）`：那是模板，不是决定。
  const DEC_RE = /^D(\d{2})【(裁定|决定|收敛)】\s*(?!…|\.\.\.)(.+?)\s*$/gm;

  function parseDecisions(md) {
    const out = [];
    DEC_RE.lastIndex = 0;
    let m;
    while ((m = DEC_RE.exec(md || "")) !== null) {
      out.push({ id: "D" + m[1], kind: m[2], text: m[3] });
    }
    return out.slice(-20);
  }

  // 浮层自身：鼠标移进去时保持显示（可滚动读长文）
  tooltip.addEventListener("mouseenter", () => { clearTimeout(hideTimer); });
  tooltip.addEventListener("mouseleave", () => { hideTooltip(); });

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

  // [画布 2026-10-19 · 索引化] 摘要只作为**字符串**供给浮层，不再往卡面写。
  //
  // 卡面已经没有 .summary / .meta2 节点了，原来那些 `querySelector(...)` 全是空查，
  // 而且每次回填还会 scheduleReflow()（一次整幅避让重算）——纯粹白烧。
  // 现在：命中缓存直接返回；未命中就走投影里带的 conclusion（后端算好的那段），
  // 再没有才发一次 getCard 兜底。
  async function getSummary(cardId, card) {
    if (summaryCache.has(cardId)) return summaryCache.get(cardId);

    const fastText = (card && typeof card.conclusion === "string" && card.conclusion)
      ? card.conclusion
      : (card && typeof card.conclusion_summary === "string" ? card.conclusion_summary : null);
    if (fastText !== null) {
      summaryCache.set(cardId, fastText);
      return fastText;
    }

    try {
      const data = await API.getCard(WORK_ID, cardId);
      const body = (data && data.body) || {};
      const text = String(body.status_block || "");
      const firstLine = text.split("\n").map((x) => x.trim()).find((x) => x) || "";
      const summary = firstLine.length > 120 ? firstLine.slice(0, 120) + "…" : firstLine;
      summaryCache.set(cardId, summary);
      return summary;
    } catch (e) {
      return "";
    }
  }

  // 自动懒加载可见卡的摘要（[phase8.4] 不再按缩放档设门槛，任何档均加载）
  // [画布第三步 T2] 卡数据改吃参数（cardsById＝vm.cardsById）——V1
  // [画布 2026-10-19 · 索引化] 卡面不再有 .summary 元素，本函数随之退役。
  //
  // 原来它每轮渲染对**每张可见卡**做一次 `node.querySelector(".summary")` 并把摘要
  // 写进卡面——232 张卡就是每轮 232 次查询 + 232 次写 DOM，全是白干。
  // 现在摘要只在"悬停"这一个时刻需要（showTooltip 自己调 getSummary 取字符串），
  // 缓存命中即返回，零 DOM 依赖。保留空函数只为不牵动调用点（renderCanvas /
  // renderSubtree 各一处），语义上它是"无需预热"。
  function lazyLoadVisibleSummaries() { /* 索引化后无需预热摘要 */ }


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
        // [画布 2026-10-19 · 自查] 顶栏的 compact 判据是 innerWidth < 1400，
        // 但原来只在 renderProjectStat 里算一次 ⇒ **拉窗口不会重算**：
        // 从 1600 缩到 1280，顶栏还是完整版，挤出边界；反过来则白占空间。
        // 这里跟着窗口尺寸重算一次（同一次防抖里，成本可忽略）。
        renderProjectStat();
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
    exitOverview();
    // [画布 2026-10-19 · 自查] 浮层挂在 #viewport **内部**，而这里无条件
    // preventDefault —— 于是浮层里那段可滚动的内容**永远滚不动**（滚轮被画布
    // 吃掉去平移了）。浮层是唯一能读全文的地方，读不了等于白设。
    // 只放行"确实能滚"的情况：滚到底之后继续滚，仍然交给画布平移。
    const tt = e.target && e.target.closest ? e.target.closest(".card-tooltip") : null;
    if (tt && tt.scrollHeight > tt.clientHeight + 1) {
      const atTop = tt.scrollTop <= 0 && e.deltaY < 0;
      const atBottom = tt.scrollTop + tt.clientHeight >= tt.scrollHeight - 1 && e.deltaY > 0;
      if (!atTop && !atBottom) return;      // 放行：浏览器原生滚动浮层
    }
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
      //
      // [画布 5711 · P1-5a 修] 尺寸**不再逐节点读 DOM**。
      // 病根：offsetWidth/offsetHeight 是布局属性，在 mousemove 里逐个读 ⇒
      // 每帧一次强制同步布局（232 卡时框选掉帧）。现在读上一轮投影里已经测好的
      // 尺寸缓存（lastProjection.sizes，与布局用的是同一份）——零 DOM 查询。
      // 拿不到缓存时回退原读法（不是所有路径都有投影）。
      const z = state.view.zoom;
      const cachedSizes = (lastProjection && lastProjection.sizes) || null;
      const hit = [];
      for (const [cid, node] of nodeMap) {
        const nx = parseFloat(node.style.left) || 0;
        const ny = parseFloat(node.style.top) || 0;
        const sx = nx * z + state.view.panX;
        const sy = ny * z + state.view.panY;
        const cs = cachedSizes && cachedSizes[cid];
        const sw = (cs ? cs.w : (node.offsetWidth || 220)) * z;
        const sh = (cs ? cs.h : (node.offsetHeight || 120)) * z;
        if (sx < x + w && sx + sw > x && sy < y + h && sy + sh > y) hit.push(cid);
      }
      const next = new Set(marqueeState.base);
      for (const id of hit) next.add(id);
      state.view.selected = next;
      // [画布 5711 · P1-5b 修] 拖动过程中只做**轻量**选中态切换。
      // 原来每个 mousemove 都走 applySelectionClasses()，而它内部会
      // renderSelectionBar()（重建 innerHTML + 重绑监听）＋ renderCardDetail()
      // （重建整个右栏）＋ highlightEdges()（遍历全部边 path）——拖一个框
      // = 几百次 DOM 重建。
      // 现在：拖动中只切 class（纯样式，可撤销），
      // 重面板留到 mouseup 一次性做（见 detachDragListeners 之后的收尾）。
      applySelectionClassesLite();
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
      // [画布 5711 · P1-5b 修] 拖动过程中只做了轻量选中态（applySelectionClassesLite），
      // 这里补一次**完整**的面板重建：选择条（已选几张 + 批量动作）、右栏
      // （多选清单与批量操作）、连线高亮——它们都读 state.view.selected，
      // 不在这里刷就会停留在框选前的状态（选择条上的张数是旧的）。
      applySelectionClasses();
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

  // ══════════════════════════════════════════════════════════════════════════
  // 全局视图（一键）
  // ──────────────────────────────────────────────────────────────────────────
  // [画布 2026-10-19] 画布唯一不可替代的是"看形状"：层级（位置）、重量（面积）、
  // 活跃（明度）。但这三样只有**整棵树一屏装下**时才成立——滚动的树看不出形状。
  // 所以它不是"再缩放一次"，是一个明确的视图：装下全部 + 强制块状（不画字）。
  //
  // 为什么单独做一键：以前要靠拖缩放条到 42% 以下才进块状档，等于"看全局"这个
  // 动作藏在了一个滑杆里。列表没有这个动作，这是画布独有的。
  // ══════════════════════════════════════════════════════════════════════════
  // 视口"真正可用"的区域：扣掉被顶栏盖住的那一段。
  // [画布 2026-10-19] 实测：按 clientHeight 算出的居中点比可见区域低 44px，
  // 结果是顶部一排卡被顶栏压住、底部一排被裁掉——"一屏装下"名不副实。
  function _viewAvail() {
    const vr = viewport.getBoundingClientRect();
    const tb = document.querySelector(".topbar");
    const cut = tb ? Math.max(0, tb.getBoundingClientRect().bottom - vr.top) : 0;
    return { vw: vr.width || 1, vh: Math.max(120, vr.height - cut), topCut: cut };
  }

  // 全局视图只做一件事：把整棵树装进"真正可见"的矩形里。
  // 返回实际 zoom（0 表示没算出来）。
  function _fitOverviewOnce() {
    if (!state.view.overview || !nodeMap.size) return 0;
    const { vw, vh, topCut } = _viewAvail();
    const pad = 26;
    const availW = Math.max(80, vw - pad * 2);
    const availH = Math.max(80, vh - pad * 2);
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of world.querySelectorAll(".card-node")) {
      const nx = parseFloat(n.style.left) || 0, ny = parseFloat(n.style.top) || 0;
      x0 = Math.min(x0, nx); y0 = Math.min(y0, ny);
      x1 = Math.max(x1, nx + n.offsetWidth); y1 = Math.max(y1, ny + n.offsetHeight);
    }
    if (!Number.isFinite(x0)) return 0;
    const w = Math.max(1, x1 - x0), h = Math.max(1, y1 - y0);
    const z = Math.max(0.12, Math.min(1.0, Math.min(availW / w, availH / h)));
    applyViewState({
      zoom: z,
      panX: Math.round(pad + (availW - w * z) / 2 - x0 * z),
      panY: Math.round(topCut + pad + (availH - h * z) / 2 - y0 * z),
    });
    applyLODClasses(z, true);
    reflowAll();
    return z;
  }

  // 全局视图的图例（常驻到退出）。
  //
  // [画布 2026-10-19] 原来靠一句 3 秒就消失的 notify 解释"块大小/亮度是什么意思"——
  // 用户还没看完就没了，等于没解释。而全局视图里**没有任何文字**，图例是唯一
  // 能让人读懂"面积/颜色/左条各代表什么"的东西。它必须一直在，直到退出。
  function showOverviewLegend(on) {
    let el = document.getElementById("cvOvLegend");
    if (!on) { if (el) el.remove(); return; }
    if (el) return;
    el = document.createElement("div");
    el.id = "cvOvLegend";
    el.className = "cv-ovlegend";
    el.innerHTML = `
      <div class="cv-ovl-row"><span class="cv-ovl-area"></span>面积＝体量（字数）</div>
      <div class="cv-ovl-row"><span class="cv-ovl-dot" data-k="warn"></span>有待裁定</div>
      <div class="cv-ovl-row"><span class="cv-ovl-dot" data-k="live"></span>正在跑</div>
      <div class="cv-ovl-row"><span class="cv-ovl-bar"></span>左条深浅＝层级</div>`;
    document.body.appendChild(el);
  }

  function viewOverview() {
    if (!nodeMap.size) return;
    hideTooltip();          // 进全局视图时把浮层收掉：块状档没有可读的字，浮层只会挡视线
    state.view.overview = true;
    _userMoved = false;
    // 立即两遍收敛 + 两次延迟兜底。
    // 为什么需要延迟：全局视图里块**带名字**（cv-overview），名字换行会改卡高 →
    // 布局变高 → 第一次算的 zoom 就装不下了。实测 39 卡项目底部一排被裁掉。
    _fitOverviewOnce();
    _fitOverviewOnce();
    const retry = (ms) => setTimeout(() => {
      if (state.view.overview && !_userMoved) _fitOverviewOnce();
    }, ms);
    retry(280);
    retry(900);
    scheduleSaveView();
    showOverviewLegend(true);
    notify("全局视图 · 滚轮或点击任意卡退出");
    return true;
  }

  // 退出全局视图：任何"进入具体位置"的动作（滚轮、拖拽、选中卡片）都算退出。
  // 模式必须能被自然的动作打破，否则它会变成一个困住用户的壳。
  function exitOverview() {
    if (!state.view.overview) return;
    state.view.overview = false;
    showOverviewLegend(false);
    applyLODClasses(state.view.zoom, true);
  }

  function doFitAll() {
    fitToContent();
  }

  function zoomByStep(dir) {
    const rect = viewport.getBoundingClientRect();
    zoomAtPoint(rect.left + rect.width / 2, rect.top + rect.height / 2, state.view.zoom * (dir > 0 ? 1.25 : 0.8));
  }

  async function deleteSelected() {
    // [画布 5711 · P1-3 修] 删卡前的最后一道闸：只删**画布上看得见**的卡。
    // 病根：选中集从不与 nodeMap 求交，收起父卡后看不见的卡还留在选中集里，
    // 按 Delete 会把它一起删掉——数据丢失。这里先裁一次再进入删除流程。
    pruneSelectionToVisible();
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
    const ok = await cvDialog({
      title: `删除 ${deletable.length} 张卡片`,
      // [画布 2026-10-19] 文案必须跟着能力走：回收站上线后，删卡是**可撤销**的。
      // 原来那句"此操作不可撤销"现在是一句假话，会让人不敢删或删了不敢回头。
      lines: [...names, "", "删除后可在回收站恢复（画布空白处右键 → 回收站）。"],
      danger: true,
      okText: "删除",
    });
    if (!ok) return;
    const done = [];
    for (const id of deletable) {
      try { await API.deleteCard(WORK_ID, id); done.push(id); } catch (err) { /* 单张失败不阻断其余 */ }
    }
    clearSelection();
    reloadCanvas();
    if (done.length) offerUndoDelete(done);
  }

  // [画布 2026-10-19] 删除后的撤销入口。
  //
  // 后端删卡时已经把整行 + 修改历史 + 会话正文存进回收站（见 card_store.delete_card），
  // 这里只负责在 12 秒内给一个"点一下就能回来"的入口。
  // 为什么不做成"先标记、超时再真删"：那会让画布在 12 秒内处于"删了但没删"的
  // 中间态，刷新页面就前功尽弃；回收站是持久事实，撤销随时有效，不受刷新影响。
  function offerUndoDelete(ids) {
    const label = ids.length === 1
      ? `已删除「${(state.index.cardMap[ids[0]] || {}).title || ids[0]}」`
      : `已删除 ${ids.length} 张卡片`;
    notify(`${label} —— 点此撤销`, "warn", {
      ttl: 12000,
      onClick: async () => {
        let n = 0;
        for (const id of ids) {
          try { await API.restoreCard(WORK_ID, id); n++; } catch (err) { /* 单张失败不阻断 */ }
        }
        await reloadCanvas();
        notify(n === ids.length ? `已恢复 ${n} 张卡片` : `恢复了 ${n}/${ids.length} 张`, n === ids.length ? "info" : "error");
      },
    });
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
      // [画布 2026-10-19] 裁定模式下 Esc 要**真的退出**：原来只把 triageList 清空，
      // 右栏那块内容原样留着，读者以为还在队列里，其实已经不在——按 → 再也没反应。
      if (triageList.length) {
        triageList = [];
        const pnl = document.getElementById("rulePanel");
        const cd = document.getElementById("cardDetail");
        if (pnl && pnl.dataset.mode === "card") {
          pnl.classList.remove("is-open");
          pnl.dataset.mode = "";
        }
        if (cd) { cd.hidden = true; cd.innerHTML = ""; }
        return;
      }
      clearSelection();
      return;
    }

    // 裁定模式下 ← → 是"逐处走"，不是"在卡片树里移动"——队列里读者关心的是
    // 下一处裁定，不是下一张卡。所以这一条必须排在方向键导航之前。
    if (triageList.length && (e.key === "ArrowRight" || e.key === "ArrowLeft")) {
      e.preventDefault();
      triageStep(e.key === "ArrowRight" ? 1 : -1);
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
      toggleCell(ids[0]);   // [画布 2026-10-19] 就地展开成工作单元
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

    if ((e.key === "n" || e.key === "N") && !mod) {
      // [画布 2026-10-19 · 自查] 新建卡从 Tab 改绑 n。
      //
      // 原来 Tab 被 preventDefault 抢去当"新建卡"——这在鼠标时代无所谓，
      // 但卡片现在可以 Tab 聚焦了，抢掉 Tab 等于**键盘用户永远出不了画布**
      // （每按一次 Tab 就弹一次新建框，焦点再也走不到别处）。
      // Tab 是浏览器/读屏软件的"下一个焦点"，任何应用都不该占用它。
      // n ＝ new，单键、不与输入冲突（isTypingTarget 已在前置返回）。
      e.preventDefault();
      openNewCardDialog(ids.length === 1 ? ids[0] : null);
      return;
    }
    if ((e.key === "f" || e.key === "F") && ids.length) {
      e.preventDefault();
      focusCard(ids[0]);
      return;
    }
    if (e.key === "g" || e.key === "G") {
      // [画布 2026-10-19] 看全局＝看形状。这是画布独有的动作，列表给不了。
      e.preventDefault();
      viewOverview();
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
    // [画布 2026-10-18 · 修设计错误] 拖拽**不再钉住**（原来 pinned:true）。
    //
    // 为什么改：拖一下就永久钉住，多次拖拽后存储坐标累积偏移，布局被拉散
    // （实测 work_001：根卡和子卡间距拉到 1180px，7 张卡只有 2 张在视口内）。
    // 现在拖拽表达的是**顺序**：y 作为同级排序依据（见 layoutTree 的 kidsOf），
    // 位置仍由布局算——用户拖完不弹回，布局也永远整齐。
    // "钉住"保留给明确的右键菜单动作（锁定不被自动布局移动）。
    const keepPinned = !!(state.data.positions[cid] && state.data.positions[cid].pinned);
    state.data.positions[cid] = { x, y, pinned: keepPinned };
    pendingPatches[cid] = { x, y, pinned: keepPinned };
    if (patchTimer) clearTimeout(patchTimer);
    patchTimer = setTimeout(flushPatches, 500);
  }

  // [画布 5711 · P1 修] **同层顺序归一化**：让"落点"真正表达"排第几"。
  //
  // 病根（实测复现）：布局的同层排序是"按存储的 y 排；**没存过 y 的排在有 y 的后面**"
  // （见 layoutTree 的 kidsOf）。⇒ **同层里第一次拖某张卡时，它会被提到该层最前面**，
  // 用户的落点被完全忽略。实测：把 card_017 拖到 card_010 下面（落点 y=50，介于
  // card_010 的 20 与 card_016 的 108 之间），结果它跑到第 3 位 —— 想插在中间，
  // 却插到了最前面。只有"以前拖过的卡"之间才比得公平。
  //
  // 修法（不碰布局纯函数、不改存储格式）：落子那一刻，把**同层所有卡**的 y 统一
  // 写成"它们当前占的位置"（被拖的那张写成落点 y）——一层人的顺序于是被显式记下，
  // 布局按 y 排序即可原样复现用户的意图。
  // 为什么不写进布局：布局是纯函数（card_045 E1 纪律），不该读 DOM；而"同层当前
  // 次序"是**运行时**信息，只有落子那一刻才确定。
  // 为什么不新增字段：positions 的 {x,y,pinned} 结构是前后端共用的（存储语义
  // 见 card_045 E7），复用 y 作排序键最省事，也不牵动后端。
  function normalizeSiblingOrder(draggedIds) {
    const moved = new Set(draggedIds);
    const groups = new Map();          // 父卡 → 本次被拖的卡
    for (const cid of moved) {
      const c = state.index.cardMap[cid];
      if (!c) continue;
      const p = c.parent || "";
      if (!groups.has(p)) groups.set(p, []);
      groups.get(p).push(cid);
    }
    for (const parent of groups.keys()) {
      const sibs = state.index.childMap[parent] || [];
      for (const s of sibs) {
        if (moved.has(s.card_id)) continue;      // 被拖的那几张保留自己的落点 y
        const n = nodeMap.get(s.card_id);
        if (!n) continue;                         // 不在场 ⇒ 不参与这一层的排序
        persistCardPos(s.card_id, parseFloat(n.style.left) || 0, parseFloat(n.style.top) || 0);
      }
    }
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
    // [画布 5711 · 产品] 统一到 cvDialog（与"重命名""删除"同一套弹窗）。
    //
    // 病根（产品不一致 + 一个真 bug）：新建卡原来用的是**另一套自制小面板**
    // （inline style 拼的、右上角浮出、无遮罩）。同一族"改卡"操作用两套交互语言，
    // 而且这套自制面板有两个实打实的问题：
    //   ① 它的 Escape 只 `node.remove()`，**事件继续冒泡** ⇒ 画布的全局 Esc 处理器
    //      跟着执行 ⇒ **关弹窗的同时把选中的卡也清了**（一个动作干了两件事）；
    //   ② 没有遮罩、没有焦点陷阱、没有 aria-modal ⇒ 读屏/键盘用户完全没提示
    //      "弹窗开着"（Tab 会跑到背后的画布上）。
    // cvDialog 这三样都有（捕获阶段拦 Escape/Enter、Tab 焦点陷阱、初始焦点在输入框）。
    //
    // 行为保持：子卡＝就地建卡并刷新画布；顶层卡＝填完标题跳草稿页（产品决定不变）。
    (async () => {
      const title = await cvDialog({
        title: isChild ? "新建子卡" : "新建卡片",
        lines: isChild ? ["挂在当前卡下。"] : ["先说清楚要做什么，再落库。"],
        input: true,
        defaultValue: "",
        okText: isChild ? "创建" : "下一步",
      });
      if (title === null) return;                 // 取消
      const t = String(title).trim();
      if (!t) { API.toast("请输入标题", "error"); return; }
      if (isChild) {
        try {
          await API.createCard(WORK_ID, { title: t, source: "用户直接创建", card_type: "", parent: parentId });
          state.view.expanded.add(parentId);
          scheduleSaveView();
          await reloadCanvas();
          notify("已创建：" + t);
        } catch (e) {
          notify("创建失败：" + (e && e.message ? e.message : e), "error");
        }
        return;
      }
      // [惰性新建 2026-10-17 · 治建卡延迟] **先出画面，数据后到**：
      // 提交标题立刻跳进这张卡的对话画面（零等待、零 POST）；落库由对话页**后台立刻发起**。
      // 标题/父卡/类型经 sessionStorage 一次性携带（跳页清空内存；URL 只承担页面身份）。
      try {
        sessionStorage.setItem("newCard:" + WORK_ID, JSON.stringify({
          title: t, parent: state.nav.viewRoot || "", card_type: "",
        }));
      } catch (e) { /* 存储不可用：退化为默认标题，不阻断跳转 */ }
      location.assign("/work/" + WORK_ID + "/card/draft");
    })();
  }

  if (newCardBtn) newCardBtn.onclick = () => openNewCardDialog(null);

  // [画布商业化 2026-10-17] 工具栏新入口（元素在 work.html，id 固定）
  const fitBtn = document.getElementById("fitBtn");
  const searchBtn = document.getElementById("searchBtn");
  const ruleToggleBtn = document.getElementById("ruleToggleBtn");
  const rulePanelEl = document.getElementById("rulePanel");
  if (fitBtn) fitBtn.onclick = () => doFitAll();

  // [画布 2026-10-19] 第三层关系线开关：只翻显示，不重算布局。
  // 关系线在 renderEdges 里已经画好（12 条，成本可忽略），开关只切 <g> 的 display，
  // 所以点一下立刻响应、不闪、不重排。
  const relToggleBtn = document.getElementById("relToggleBtn");
  if (relToggleBtn) {
    relToggleBtn.onclick = () => {
      state.view.showRelations = !state.view.showRelations;
      const on = state.view.showRelations;
      relToggleBtn.setAttribute("aria-pressed", on ? "true" : "false");
      relToggleBtn.classList.toggle("is-on", on);
      const g = edgesSvg.querySelector('[data-edge-group="links"]');
      if (g) g.style.display = on ? "" : "none";
    };
  }
  if (searchBtn) searchBtn.onclick = () => openPalette();

  // 规则面板收起/展开：状态记 localStorage（跨刷新保持），切换后画布宽度变了，
  // 小地图与 fit 都要跟着重算——否则视口框和内容位置会错位。
  // 规则卡：工具栏按钮打开右侧浮层（不再常驻挤压画布）
  if (ruleToggleBtn) {
    ruleToggleBtn.onclick = () => {
      const panel = document.getElementById("rulePanel");
      const detail = document.getElementById("cardDetail");
      const head = document.getElementById("rulePanelHead");
      const body = document.getElementById("rulePanelBody");
      if (!panel) return;
      const isRulesOpen = panel.classList.contains("is-open") && panel.dataset.mode === "rules";
      if (isRulesOpen) {
        panel.classList.remove("is-open");
        panel.dataset.mode = "";
        return;
      }
      panel.classList.add("is-open");
      panel.dataset.mode = "rules";
      if (detail) { detail.hidden = true; detail.innerHTML = ""; }
      if (head) head.hidden = false;
      if (body) body.hidden = false;
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
        { act: "trash", label: "回收站（找回已删除的卡）", run: () => openTrash() },
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
      // [画布 5711 · P1 修] "打开对话"**不再标 ↵**：↵ 的真实语义是**就地展开对话**
      // （toggleCell，见节点级与全局级两个 Enter 处理），不是跳整页。
      // 原来两条提示正好写反了：↵ 标在"打开对话"上、"就地展开"标着"双击"。
      // 而"双击"上一轮已撤除（单击与双击互斥），按双击什么都不会发生 ⇒ 纯骗人。
      // 现在：↵ 只标在它真正对应的动作上，其余不标。
      { act: "open", label: multi ? `打开对话（${state.view.selected.size} 张中第一张）` : "打开对话", run: () => openCardChat(cid) },
      // [画布 5711 · 产品] 「在此展开对话」：不离开画布地看这张卡的对话并直接回复。
      // 与卡面上的 💬 按钮同一个动作 —— 菜单里也必须有一份，否则"按钮在卡上、
      // 菜单里没有"会让人以为这是两件事。
      { act: "expand-chat", label: "在此展开对话（不离开画布）", kbd: "↵", run: () => toggleCell(cid) },
      { act: "focus", label: "以此卡为中心", kbd: "F", run: () => focusCard(cid) },
      { act: "watch", label: "盯这张卡（并行对话层）", run: () => watchAdd(cid) },
      multi ? { act: "cmp", label: `并排比较这 ${state.view.selected.size} 张`, run: () => openCompare([...state.view.selected]) } : null,
      { sep: true },
      { act: "child", label: "新建子卡", run: () => openNewCardDialog(cid) },
      hasKids ? { act: "toggle", label: isExpanded ? "收起子卡" : "展开子卡", run: () => toggleExpand(cid) } : null,
      // [画布 5711 · P1 修] 原来写"Alt+双击"——下钻的真实手势是 **Alt+单击**
      // （click 处理器里 e.altKey 分支），双击根本到不了这里。
      { act: "drill", label: "下钻到此卡（Alt+单击）", run: () => setViewRoot(cid) },
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
        ${(() => {
          // [画布 2026-10-19 · 自查] 这里原来判 `c.state === "completed"` —— 而
          // 投影里的 state 只有 "" 和 "active" 两种值，那个"已完成"标记**永远不出现**，
          // 是一段死代码。搜索结果真正该回答的是"这几张里哪张有活等我"，
          // 那个信息在 work_state / pending_count 里。
          const n = Number(c.pending_count || 0);
          if (n > 0) return `<span class="cv-palette-state" data-k="pending">${n} 处待裁定</span>`;
          if (c.work_state === "concluded") return `<span class="cv-palette-state" data-k="done">已结论</span>`;
          return "";
        })()}
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


  // ==================== 数据加载 ====================
  async function reloadCanvas(opts) {
    // [画布 5711] opts.silent：静默重拉（兜底轮询命中时用）——画布上已经有内容了，
    // 再弹一次骨架屏就是一次"白一下"，没必要。默认行为逐字不变。
    const _silent = !!(opts && opts.silent);
    if (!_silent) showCanvasState("loading");   // [F3④]
    try {
      const data = await API.getCanvas(WORK_ID);
      state.data.cards = data.cards || [];
      state.data.links = data.links || [];
      // [画布 2026-10-18] 最近活动（来自 card_history，后端已滤掉批量噪音）
      state.data.recentEvents = data.recent_events || [];
      // ⚠ 索引必须在这里建，不能等 renderCanvas——`_evByCard` 原来只在 renderCanvas
      // 里重建，而 computeCatchup 在它**之前**跑，读到的是上一轮（首屏则是空表）。
      // 表现：接续条把"卡住/跑完"全判成"改动"，清单里每条 detail 都是"有更新"。
      _evByCard = new Map();
      for (const e of state.data.recentEvents) {
        if (e && e.card_id && !_evByCard.has(e.card_id)) _evByCard.set(e.card_id, e);
      }
      // [画布 2026-10-19] 先按"上次访问"算增量，再立刻把本次访问写回去。
      // 不用 beforeunload 记时间：实测无头导航/bfcache 下它可能不触发，写不进去
      // 就等于这个功能从不存在。加载即写是可预测的——"自上次打开以来"的字面语义。
      computeCatchup();
      markVisit();
      // [card_045 E7 · L2 位置独立于卡片语义] 卡片改名、换类型、变状态、换归属
      // 一律不影响其坐标记录（positions 只按 card_id 键挂靠，不携带任何卡片
      // 语义字段）；验收＝改一张卡的标题／类型后，坐标记录不变、画布位置不变。
      state.data.positions = data.positions || {};
      if (!state.nav.workCardId) {
        // [T07-1-10] 判据源切换：根卡不再按 card_type 找，改按 card_id == work_card_id
        // （work_card_id 取自既有 /api/works 端点的 work.json 权威字段）
        // [画布 5711 · P0 修] **根卡不再等 /api/works**（这一改是修一个"永久白屏"）。
        //
        // 病根（实测抓到的 P0）：原来这里 `await API.listWorks()` 是**出图的关键路径**
        // 上的一次网络往返。浏览器对同一源有 **6 条并发连接**的上限（HTTP/1.1），
        // 而画布页**常驻**着 SSE 长连接（工作级 /events ＋ 单卡 stream）⇒ 页面加载
        // 时的并发请求一旦把连接槽占满，这条往返就会被**排队、永远发不出去** ⇒
        // `await` 悬着 ⇒ reloadCanvas 停在半路 ⇒ **画布永远空白（骨架屏不撤）**。
        // 实测：屏蔽工作级 /events ⇒ 画布正常出卡；不屏蔽 ⇒ 永久 0 张卡。
        //
        // 修法：**根卡本来就画布数据里带着**——parent 为空的那张就是项目本体
        // （card_store._normalize_parent：剧本卡 parent 强制 ""）。用它直接出图，
        // **零往返**；/api/works 退化成异步补充（只用来核对权威 id 与补标题），
        // 拿不到也不影响出图。
        let rootCard = state.data.cards.find(c => !(c.parent || ""));
        if (!rootCard) rootCard = state.data.cards[0];
        t07SwitchLog("canvas.reloadCanvas.rootFind", WORK_ID,
          state.data.cards.some(c => c.card_type === "\u5267\u672c"), !!(rootCard && !(rootCard.parent || "")));
        state.nav.workCardId = rootCard ? rootCard.card_id : "";
        state.nav.workTitle = rootCard ? rootCard.title : "";
        state.nav.viewRoot = state.nav.workCardId;
        // 权威字段（work.json 的 work_card_id）异步补：与数据推出的根卡不一致时
        // （极少见）以权威为准并重画；标题也用它。**全程不阻塞出图。**
        try {
          API.listWorks().then((_works) => {
            const _list = Array.isArray(_works) ? _works : (_works.works || []);
            const _w = _list.find(x => (x.work_id || x.id) === WORK_ID);
            if (!_w) return;
            const _wcId = _w.work_card_id || "";
            if (!_wcId) return;
            if (_wcId === state.nav.workCardId) {
              if (_w.title) state.nav.workTitle = _w.title;
              return;
            }
            const _wc = state.data.cards.find(c => c.card_id === _wcId);
            if (!_wc) return;
            console.warn("[canvas] 权威 work_card_id 与数据推出的根卡不一致，以权威为准",
              _wcId, state.nav.workCardId);
            state.nav.workCardId = _wcId;
            state.nav.workTitle = _wc.title || state.nav.workTitle;
            state.nav.viewRoot = _wcId;
            renderAll();
          }).catch(() => { /* 拿不到就用数据里的根卡，不影响出图 */ });
        } catch (e) { /* listWorks 不可用零阻断 */ }
      }
      rebuildCardMap();
      // [画布商业化 2026-10-17] 首次加载：有存档就恢复，无存档就**整幅适配**。
      // 旧行为是无存档时留在 (80,40) 默认位，内容一多就溢出视口上方（首屏只见半张图）。
      let _needFit = false;
      if (!__VIEW_RESTORED__) {
        _needFit = !restoreViewState();
        __VIEW_RESTORED__ = true;
      }
      // [画布 5711 · P1 修] **骨架屏必须在渲染完成之后才移除**。
      // 病根：原来 `showCanvasState("clear")` 在 renderAll() **之前** ⇒ 中间那几帧
      // 骨架屏没了、卡片也还没画出来 ⇒ 用户看到的是「打开画布先白一下」，
      // 而不是「骨架 → 内容」的连贯过渡。移除动作挪到 renderAll 之后。
      renderAll();          // [画布第三步 T4] 数据就绪 → 统一重绘入口
      if (!_silent) showCanvasState("clear");   // ← 渲染完成，此刻才收起骨架屏
      // [画布 2026-10-18 · 工作台] 首屏**不再把整张图缩到装得下**。
      // 43 张卡全装进 1600×1000 要缩到 60%，待裁定条目在 60% 下根本读不了——
      // 那等于"打开了但什么也读不到"。工作台打开就该带你去该干活的地方：
      //   100% 缩放，视野落在**第一张有待裁定的卡**上；没有待裁定才退回整幅适配。
      if (_needFit) {
        // [画布 5711 · P1-2 修] `_autoFitPending` 原来在这里被置 **false**，
        // 而全文件只有这一处、还有 `scheduleAutoFit` 里的两处读取——三处全是 false，
        // 于是 `scheduleAutoFit()` 的 `if (!_autoFitPending) return;` **恒成立**：
        // 注释描述的「回填改变卡高 → reflow → 重试适配」这套机制**一次也没跑过**。
        // 后果：首屏 fit 算在"矮版本"卡高上，随后内容回填把布局撑长 ⇒ 画布溢出、
        // 底部卡片被裁掉（用户看到"下面还有卡，但怎么滚都看不到"）。
        // 现在真正打开它：首屏 fit 之后保持 pending，让后续 reflow 能触发重适配。
        _autoFitPending = true;
        _autoFitRounds = 0;
        _userMoved = false;
        // [画布 2026-10-18 · 画布自己承载全局] 首屏**整幅适配**，一屏看见整棵树。
        // 之前锚定"第一张子卡 100%"，一屏只看得见 5 张卡，所以才需要左栏目录来补
        // ——那是用外部列表弥补画布的失败。现在靠 LOD 保证低缩放下的可读性：
        // 50% 时每张卡仍显示标题 + 待裁定数（见 canvas.css 的 .l0 段）。
        // ⚠ 要 fit **两次**：第一次按 l1 的卡高（150px）算，缩到 65% 会切到骨架档，
        // 卡片变 64px 高、bbox 大幅变小——不重算的话会停在 55%，字小到读不了。
        // 第二次 fit 用的是骨架档的真实尺寸，通常能回到 100%。
        // 首屏整幅适配（迭代式，见 fitToContent 注释）
        fitToContent();
      }
      if (!state.data.cards.length) showCanvasState("empty");   // [F3④] 空态
      // [T17-F6/R-1] 不再无条件 hideWorkspace()：「刷新画布数据」与「收起对话层」是两件事。
      // 视图切换改由 .app-main[data-view] 显隐驱动（workspace.js setAppView）；「← 画布」
      // 按钮为纯显隐（R-043）；建卡/删卡/对话生卡等数据变更已在各自源头调 ReloadCanvas 刷新。
      // hideWorkspace() 函数保留（setViewRoot 下钻路径仍在用，现为无害空操作）。
    } catch (e) {
      // [迭代四 F3④] 三态容器：加载失败可见（原静默 catch 界面无态——B-18）
      //
      // [画布 5711 · P0-3 修] 原来这里只有 `showCanvasState("error")`：
      //   ① 异常对象 `e` 被**整个丢弃**——控制台没有堆栈、界面只有一句"画布加载失败"，
      //      排障完全没有入口（B-18 只解决了"有没有态"，没解决"知不知道原因"）；
      //   ② 叠加 renderCanvas 的 visibility 死锁 ⇒ 画布既空白又看不出为什么。
      // 现在：记日志（带堆栈）＋ 把真因写进错误框＋ 先解除 world 的隐藏
      //      （宁可让用户看到一堆叠在一起的占位卡，也不要静默白屏）。
      console.error("[canvas] reloadCanvas 失败", e);
      try { if (world) world.style.visibility = ""; } catch (_e) { /* 忽略 */ }
      showCanvasState("error", (e && (e.message || String(e))) || "未知错误");
    }
  }

  // [迭代四 F3④] 画布三态容器：loading／error（失败＋重试）／empty。
  function showCanvasState(kind, detail) {
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
      // [画布 5711 · P0-3 修] 错误框带真因（前端直接显示，不再只显示 HTTP 500 那样的
      // 空话）。原因来自后端或前端异常的真实 message，排障不必再猜。
      const _detail = detail ? `<div class="cs-error-detail">${escapeHtml(String(detail))}</div>` : "";
      box.innerHTML = `<div class="cs-error">画布加载失败 ${_detail}<button class="btn" id="csRetry">重试</button></div>`;
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
        <div class="cv-empty-hint">点右上「＋ 新建卡」开始，或按 <span class="cv-empty-key">N</span> 建第一张</div>
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
      // [画布 2026-10-19] 三层连线的数据面：
      //   cardMsgs  → 第二层「通信线」（卡间信 from_card→to_card，只在通信时画）
      //   showRelations → 第三层「卡片关系」（先后/关联，默认关，手动开）
      cardMsgs: state.data.cardMsgs || [],
      showRelations: !!state.view.showRelations,
      // [画布 2026-10-19 · 自查] recent_events 原来**拉了从来没用**（后端算、
      // 前端存进 state.data.recentEvents 就没人再读）——一条死管线。
      // 现在用它给"新鲜度点"补上"最近一次到底发生了什么"。
      recentEvents: state.data.recentEvents || [],
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

  // 在队列里走一步。到边界不绕回——队列是有端点的，"走到头了"本身就是信息。
  function triageStep(delta) {
    if (!triageList.length) return;
    const next = triageIdx + delta;
    if (next < 0 || next >= triageList.length) return;
    triageIdx = next;
    showTriageItem();
  }

  function showTriageItem() {
    const it = triageList[triageIdx];
    if (!it) return;
    // [画布 2026-10-19 · 裁定闭环] 记住"我正停在哪一处"：点进卡里裁完返回时，
    // 靠它接着下一处（_resumeTriage）。记 text 而不只记下标——列表会因裁定变短。
    _saveTriagePos(it);
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
    const sidePanel = document.getElementById("rulePanel");
    if (sidePanel) {
      sidePanel.classList.add("is-open");
      sidePanel.dataset.mode = "card";
    }

    // [画布 2026-10-19] 裁定队列的每一屏必须回答三个问题，缺一个就得来回点：
    //   ① 我在整条队列的哪里 —— 第 N/M 处 + 进度条（否则"还有多少"完全靠记忆）
    //   ② 这张卡在讲什么     —— 卡面摘要（没有上下文就没法判断一处该不该裁）
    //   ③ 这一处具体问什么   —— 待裁定原文
    // 原来只有 ①的数字 和 ③，②缺失，于是每处都得先"打开卡片"读一遍再回来，
    // 队列等于退化成一个个孤立的弹窗。
    const sameCard = triageList.filter((x) => x.cardId === it.cardId).length;
    const idxInCard = triageList.slice(0, triageIdx + 1).filter((x) => x.cardId === it.cardId).length;

    const head = el("div", "cd-head");
    const counter = el("div", "cd-triage-count",
      `第 ${triageIdx + 1} / ${triageList.length} 处` +
      (sameCard > 1 ? ` · 这张卡 ${idxInCard}/${sameCard}` : ""));
    head.appendChild(counter);
    const bar = el("div", "cd-triage-bar");
    const fill = el("div", "cd-triage-bar-fill");
    fill.style.width = `${Math.round(((triageIdx + 1) / triageList.length) * 100)}%`;
    bar.appendChild(fill);
    head.appendChild(bar);
    head.appendChild(el("div", "cd-title", it.title));
    if (it.section) head.appendChild(el("div", "cd-dim", it.section));
    cardDetailEl.appendChild(head);

    // ② 上下文：这张卡的摘要（结论或现状）。异步补，先占位不留空白跳动。
    const ctx = el("div", "cd-triage-ctx");
    ctx.textContent = "读取这张卡的上下文…";
    cardDetailEl.appendChild(ctx);
    const cardObj = state.index.cardMap[it.cardId] || {};
    getSummary(it.cardId, cardObj).then((txt) => {
      if (!ctx.isConnected) return;              // 已经翻到别处了
      ctx.textContent = txt || "（这张卡还没有正文）";
    });

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

    const hint = el("div", "cd-dim",
      "← → 逐处走 · Enter 打开这张卡去裁定 · Esc 退出。裁定本身在卡片对话里完成，这里只负责把队列走完。");
    cardDetailEl.appendChild(hint);
  }

  // ══════════════════════════════════════════════════════════════════════
  // [画布 2026-10-18 · 工作台] 项目状态条
  //
  // 打开画布的第一眼必须回答"有多少事在等我"。原来顶栏只有一个项目名，
  // 43 张卡、74 处待裁定、3 张已结论——这些全要用户自己数。
  // 数据来自 canvas 投影（每张卡带 work_state / pending_count），一次遍历算完。
  // ══════════════════════════════════════════════════════════════════════
  const projectStatEl = document.getElementById("projectStat");
  let _statKey = "";   // 状态条内容签名（3 秒轮询下避免无谓重建）
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
    // [画布 2026-10-19 · 实时状况] "N 运行中"：并行处理多任务的入口。
    // 点它＝把正在跑的卡全部选中并适配到视野里——一眼看到"我这几个任务现在都在哪"。
    // 这是侧边栏列表给不了的：列表里运行中的卡散落在不同位置，还得一张张找。
    const runN = (state.async.running || new Map()).size;
    const runItem = runN
      ? `<span class="ps-item is-live" data-act="running" role="button" tabindex="0"
               title="${runN} 张卡正在跑 —— 点击选中它们并适配视野">
           <span class="ps-live-dot"></span><b>${runN}</b> 运行中
         </span><span class="ps-sep"></span>`
      : "";
    // 窄屏（1280 展开目录时顶栏只剩 ~330px）只留两个数字：卡数、待裁定数。
    // 其余项在完整版里；砍掉的用 title 兜住，鼠标停一下就能看到。
    const compact = window.innerWidth < 1400;
    const triageItem = `<span class="ps-item${pendingTotal ? " is-warn" : ""}" data-act="triage" role="button" tabindex="0"
            title="${pendingCards} 张卡上有待你裁定的事项 —— 点击逐条过">
        <b>${pendingTotal}</b> 处待裁定
      </span>`;
    // [画布 2026-10-19] 接续一行："自 X 以来 · N 张有更新 · +M 处待裁定"
    // 只在**确实有变化**时出现；点它＝只把那几张卡选中并适配到视野里。
    const cu = _catchup;
    const cuN = cu ? cu.updated.length : 0;
    // 文案只说**出了什么事**，且只报有事的那几类（"0 张跑完"是噪音）。
    const _seg = (n, word) => (n ? `<b>${n}</b> ${word}` : "");
    const cuBits = cu
      ? [
          _seg(cu.counts.blocked, "张卡住"),
          _seg(cu.counts.done, "张跑完"),
          _seg(cu.counts.paused, "张暂停"),
          _seg(cu.counts.decided, "张出定论"),
          _seg(cu.counts.created, "张新建"),
          _seg(cu.counts.edited, "张改动"),
        ].filter(Boolean)
      : [];
    const cuChip = cuN
      ? `<span class="ps-sep"></span>
         <span class="ps-item ps-catchup${cu.counts.blocked ? " is-alert" : ""}" data-act="catchup"
               title="点此只看这几张">
           自 ${fmtSince(cu.since)} 以来 ${cuBits.join(" · ")}${cu.pendingDelta > 0 ? ` · <b>+${cu.pendingDelta}</b> 待裁定` : ""}
         </span>`
      : "";
    // [画布 2026-10-19] 3 秒轮询会反复调本函数。内容没变就**不重建**——
    // 否则顶栏 innerHTML 每 3 秒重排一次，悬停 catchup 浮层会被打断，
    // 而且无谓地丢焦点。key 里带上所有会影响文案的量。
    const statKey = [total, pendingTotal, concluded, empty, runN, cuN,
                     cu ? cu.pendingDelta : 0, compact ? 1 : 0].join("|");
    if (statKey === _statKey) return;
    _statKey = statKey;

    projectStatEl.innerHTML = compact
      ? `<span class="ps-item" title="${total} 张卡"><b>${total}</b> 卡</span>
         <span class="ps-sep"></span>
         ${runItem}
         ${triageItem}${cuChip}`
      : `<span class="ps-item"><b>${total}</b> 卡</span>
         <span class="ps-sep"></span>
         ${runItem}
         ${triageItem}
         <span class="ps-sep"></span>
         <span class="ps-item"><b>${concluded}</b> 已结论</span>
         ${empty ? `<span class="ps-sep"></span><span class="ps-item is-dim"><b>${empty}</b> 未开始</span>` : ""}${cuChip}`;
    const cuEl = projectStatEl.querySelector('[data-act="catchup"]');
    if (cuEl) {
      const go = () => {
        const ids = (_catchup?.updated || []).filter((id) => state.index.cardMap[id]);
        if (!ids.length) return;
        revealCards(ids);      // 先展开祖先，否则折叠分支里的卡根本不在 DOM 上
        setSelection(ids);
        fitToCards(ids);
      };
      cuEl.addEventListener("click", go);
      // [画布 2026-10-19] 悬停给出"具体变了什么"。
      // 只报"N 张更新"等于把人送回原点：你还得逐张看过去才知道变了什么，
      // 而 recent_events 里每条都带着 detail（"接了 SSE"、"补了回收站"…）。
      // 接续这件事的价值全在这几行细节上。
      cuEl.addEventListener("mouseenter", () => showCatchupList(cuEl));
      cuEl.addEventListener("mouseleave", hideCatchupList);
      cuEl.addEventListener("focus", () => showCatchupList(cuEl));
      cuEl.addEventListener("blur", hideCatchupList);
      cuEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
      });
    }
    const triageEl = projectStatEl.querySelector('[data-act="triage"]');
    if (triageEl && pendingTotal) {
      triageEl.addEventListener("click", () => openTriage(0));
      triageEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openTriage(0); }
      });
    }
    // 运行中 → 选中全部在跑的卡并适配视野（并行任务的"点名"）
    const runEl = projectStatEl.querySelector('[data-act="running"]');
    if (runEl) {
      const goRun = () => {
        const ids = [...(state.async.running || new Map()).keys()]
          .filter((id) => state.index.cardMap[id]);
        if (!ids.length) return;
        revealCards(ids);
        setSelection(ids);
        fitToCards(ids);
      };
      runEl.addEventListener("click", goRun);
      runEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); goRun(); }
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
    // [画布 2026-10-19] 从裁定队列点进去的卡，回来接着下一处（见 _resumeTriage）
    _resumeTriage(cid);
  }

  function renderAll() {
    renderProjectStat();
    renderCanvas(collectProjectionInput(), collectViewModel());
    applyPendingSelect();
  }

  // ── 裁定队列的断点续传 ────────────────────────────────────────────────────
  //
  // [画布 2026-10-19 · 裁定闭环] 从队列点进某张卡、裁完再回来 ⇒ **接着下一处**。
  //
  // 没有这一步，"逐条过 74 处待裁定"的真实流程是：点开 → 裁定 → 返回 → 点顶栏
  // 待裁定 → 从头开始 → 手动翻到刚才那一条 → 再点开。每裁一处要重复五步，
  // 队列等于白做。
  //
  // 判据用"那一条还在不在"而不是"下标 +1"：裁掉一条后列表会变短，下标会错位；
  // 而"刚裁的那句话是否还在队列里"是唯一可靠的事实。
  const _triageKey = () => `cvTriage:${WORK_ID}`;
  function _saveTriagePos(it) {
    if (!it) return;
    try {
      sessionStorage.setItem(_triageKey(), JSON.stringify({
        cid: it.cardId, text: it.text, idx: triageIdx, at: Date.now(),
      }));
    } catch (e) { /* 隐私模式无 storage，零阻断 */ }
  }
  function _resumeTriage(cid) {
    let st = null;
    try { st = JSON.parse(sessionStorage.getItem(_triageKey()) || "null"); } catch (e) { st = null; }
    if (!st || st.cid !== cid) return;
    if (Date.now() - Number(st.at || 0) > 30 * 60 * 1000) {
      try { sessionStorage.removeItem(_triageKey()); } catch (e) { /* 零阻断 */ }
      return;
    }
    try { sessionStorage.removeItem(_triageKey()); } catch (e) { /* 零阻断 */ }
    const list = buildTriageList();
    if (!list.length) { notify("待裁定已清空 —— 这个项目没有等你裁定的事了", "success"); return; }
    const stillThere = list.findIndex((x) => x.cardId === st.cid && x.text === st.text);
    if (stillThere >= 0) {
      // 没裁掉（只是去看了一眼）⇒ 停在原处，不假装推进
      openTriage(stillThere);
      notify(`这一处还在等你（第 ${stillThere + 1}/${list.length} 处）`, "warn");
      return;
    }
    // 已裁掉 ⇒ 从原来的位置接着走（后面的条目整体前移一位）
    const next = Math.min(list.length - 1, Math.max(0, Number(st.idx) || 0));
    openTriage(next);
    notify(`已裁定，接着看下一处（第 ${next + 1}/${list.length} 处）`, "success");
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
  //
  // [画布 5711 · P1-2 修] 原来这里只有 `_autoFitPending = false` 一句收尾 ⇒
  // 整套"重试适配"机制从未生效（见 reloadCanvas 里的说明）。现在：
  //   ① 首屏 fit 之后 _autoFitPending 保持 true；
  //   ② 每次 reflow（内容/卡高变了）都来重试一次；
  //   ③ 用「布局签名」判断内容是否还在变——不变就收手（_autoFitPending=false），
  //      还变就再来一轮，但最多 _AUTO_FIT_MAX_ROUNDS 轮（防无限重排）；
  //   ④ 用户一动（_userMoved）立即放弃，绝不跟用户抢镜头。
  let _autoFitTimer = 0;
  let _autoFitRounds = 0;
  const _AUTO_FIT_MAX_ROUNDS = 3;

  // 布局签名：只用坐标（style.left/top），不读 offsetWidth/Height（不触发强制布局）。
  // 坐标变了 ⇒ 布局变了 ⇒ 需要重新适配。
  function _layoutSignature() {
    if (!lastProjection || !lastProjection.layout) return "";
    let sig = "";
    for (const cid of Object.keys(lastProjection.layout).sort()) {
      const p = lastProjection.layout[cid];
      if (!p) continue;
      sig += cid + ":" + Math.round(p.x) + "," + Math.round(p.y) + ";";
    }
    return sig;
  }

  function scheduleAutoFit() {
    if (!_autoFitPending || _userMoved) return;
    clearTimeout(_autoFitTimer);
    _autoFitTimer = setTimeout(() => {
      if (!_autoFitPending || _userMoved) return;
      const before = _layoutSignature();
      // [画布 2026-10-19 · 自查] 这里原来调 `fitView(1)` —— 那是**第二套适配**，
      // 下限 0.25、无内容判定；而首屏真正用的是 fitToContent（下限 0.9、两遍收敛）。
      // 两套并存的结果：fitToContent 先算出可读的 0.9，220ms 后 fitView 把它覆盖成
      // 0.55（39 卡实测）——**打开一个 39 卡的项目，看到的是 39 个没有任何文字的灰块**。
      // 现在只有一套：fitToContent。
      fitToContent();
      const after = _layoutSignature();
      if (before !== after && _autoFitRounds < _AUTO_FIT_MAX_ROUNDS) {
        _autoFitRounds++;
        // 内容还在变 ⇒ 保持 pending（下一轮 reflow 会再触发一次）
      } else {
        _autoFitPending = false;   // 稳定了 ⇒ 收手，别再跟用户抢镜头
        _autoFitRounds = 0;
      }
      scheduleSaveView();
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

  // ══════════════════════════════════════════════════════════════════════════
  // 并行对话层（watch layer）
  // ──────────────────────────────────────────────────────────────────────────
  // [画布 2026-10-19 · 用户令] "在画布界面，我就可以同时看到我想看的几个对话的
  // 进程，并随时回复其中任意一个窗口"。
  //
  // 这是画布相对对话页的**唯一结构性优势**：对话页一次只能开一个会话，切换就
  // 丢上下文；而并行推进 N 条线时，人真正需要的是"一屏看到 N 条线各自的进度，
  // 哪条卡住了就当场回一句"。
  //
  // ⚠ 前两次在这件事上翻车，教训写死在这里：
  //   ① 左侧导航树 —— 等于承认画布自己承载不了全局，被否；
  //   ② 右侧快捷消息栏 —— 挤掉画布一半宽度，被否。
  // 两次的病根相同：**拿画布的空间去换信息**。所以这次画布**一寸不让**：
  // 对话窗是浮在画布上层的独立窗口（fixed 定位、可折叠成一条标题、可关闭），
  // 画布尺寸、缩放、视野一律不变。窗口挡住的那点内容，用户可以折叠或拖开。
  //
  // 数据：进程 = 该卡的时序表（timeline：用户消息 / AI 回复 / 工具调用 / 判定）。
  // 回复：POST 该卡消息，与对话页同一个端点——这里回的话就是那张卡的对话。
  // 刷新：3 秒轮询增量（from=lastSeq）。不用 SSE 是因为要同时盯 N 张卡，
  //       每张开一条 SSE 连接在服务端是 N 倍长连接；轮询在 N≤4 时更省也更稳。
  // ══════════════════════════════════════════════════════════════════════════
  const WATCH_MAX = 4;
  const watch = {
    open: false,
    ids: [],
    collapsed: new Set(),
    items: new Map(),     // cid -> [{seq,type,...}]
    lastSeq: new Map(),   // cid -> 已渲染到的落盘 seq
    es: new Map(),        // cid -> EventSource（每卡一条实时流）
    esDead: new Set(),    // SSE 断掉的卡 → 退回轮询
    full: new Set(),      // 处于"读全文"模式的卡
    fullHtml: new Map(),  // cid -> 已渲染的正文 HTML（避免反复请求）
    // [画布 2026-10-19] 已读游标：cid -> 用户最后一次"看过"的 seq。
    // 并行层的意思是"同时盯几条线"，那么最基本的一件事是——**哪条线刚动了**。
    // 三个窗口同时在跑时，如果没有任何标记，你只能一个个看过去；而重跑一遍
    // 是这层最贵的动作。有这个游标，头部一个小标记就够。
    seen: new Map(),
    timer: 0,
  };

  function watchLayer() {
    let el = document.getElementById("watchLayer");
    if (!el) {
      el = document.createElement("div");
      el.id = "watchLayer";
      el.className = "watch-layer";
      document.body.appendChild(el);
    }
    return el;
  }

  function watchAdd(cid) {
    if (!cid || watch.ids.includes(cid)) return;
    if (watch.ids.length >= WATCH_MAX) {
      notify(`最多同时盯 ${WATCH_MAX} 张卡，先关掉一个`, "warn");
      return;
    }
    watch.ids.push(cid);
    watch.items.set(cid, []);
    watch.lastSeq.set(cid, 0);
    watch.open = true;
    syncWatchBtn();
    renderWatchLayer();
    // 先拉历史（把已有进程铺出来），历史到位后再挂实时流——
    // 顺序反了会丢中间那段：SSE 只送"从现在起"的帧。
    loadWatch(cid).then(() => {
      // 首次铺历史不算"新内容"——那是这张卡的既有进程，不是刚发生的。
      watch.seen.set(cid, watch.lastSeq.get(cid) || 0);
      markWatchFresh(cid, false);
      watchStream(cid);
    });
    startWatchPoll();
  }

  function watchRemove(cid) {
    watch.ids = watch.ids.filter((x) => x !== cid);
    watch.collapsed.delete(cid);
    watch.items.delete(cid);
    watch.lastSeq.delete(cid);
    const es = watch.es.get(cid);
    if (es) { try { es.close(); } catch (e) { /* 已关 */ } }
    watch.es.delete(cid);
    watch.esDead.delete(cid);
    watch.full.delete(cid);
    watch.fullHtml.delete(cid);
    renderWatchLayer();
    if (!watch.ids.length) stopWatchPoll();
    syncWatchBtn();
  }

  function syncWatchBtn() {
    const b = document.getElementById("watchBtn");
    if (!b) return;
    b.classList.toggle("is-on", watch.open && watch.ids.length > 0);
    b.setAttribute("aria-pressed", watch.open && watch.ids.length ? "true" : "false");
    if (watch.ids.length) b.dataset.count = String(watch.ids.length);
    else delete b.dataset.count;
  }

  function watchRunStateOf(cid) {
    const n = nodeMap.get(cid);
    return (n && n.dataset.runstate) || "";
  }

  // 把一条时序记录压成一行可读文本。进程里真正要看的只有"谁说了什么 / 调了什么"，
  // 思考链（reasoning）不在这里展开——那是对话页的事。
  function watchLineOf(it) {
    const t = String(it.type || "");
    const d = it.data || {};
    const txt = String(d.text || d.content || d.summary || d.name || "").replace(/\s+/g, " ").trim();
    if (t === "user_message") return { role: "you", label: "你", text: txt };
    if (t === "assistant_text") return { role: "ai", label: "AI", text: txt };
    // 工具帧的真实字段是 tool / target / ok / result（**没有** text/content/name），
    // 上一版按 text 取 ⇒ 渲染成一排空的"▸ 调用"，等于没显示。
    if (t === "tool_call") {
      const name = String((d.tool && d.tool.name) || d.tool || d.name || "");
      const tgt = String(d.target || d.path || "").slice(0, 36);
      return { role: "tool", label: "▸", text: name + (tgt ? " · " + tgt : "") };
    }
    if (t === "tool_result") {
      if (d.ok !== false) return { role: "tool", label: "✓", text: "完成" };
      const err = (d.result && d.result.error && (d.result.error.message || d.result.error)) || "失败";
      return { role: "tool", label: "✕", text: String(err).slice(0, 120) };
    }
    if (t === "reasoning") return null;                    // 思考不铺开
    if (t.startsWith("step_") || t === "judgment") return { role: "step", label: "·", text: txt || t };
    if (!txt) return null;
    return { role: "other", label: "·", text: txt };
  }

  // 只生成"进程"这一段。**必须与窗口外壳分开**——外壳里有输入框，
  // 轮询整窗重建会把用户正在打的字和焦点一起清掉。
  function watchBodyHtml(cid) {
    const items = watch.items.get(cid) || [];
    const lines = items.map(watchLineOf).filter(Boolean).slice(-40);

    // [画布 2026-10-19] 卡间信（别人递过来的东西）单独一块，**不进时间线**。
    // 理由：它们来自另一张卡的收件箱，和这张卡的对话不是同一个时钟，混进时间线
    // 就得猜先后。但它们恰恰是"为什么现在要看这张卡"的原因——有未读的卡，
    // 窗口里却只显示"还没有对话记录"，那等于把最该读的那条藏了。
    const inbox = (state.data.cardMsgs || [])
      .filter((m) => m.to_card === cid)
      .slice(0, 3);
    const inboxHtml = inbox.length
      ? `<div class="watch-inbox">${inbox.map((m) => `
          <div class="watch-inbox-item">
            <span class="watch-inbox-from">⇠ ${escapeHtml(m.from_title || m.from_card || "")}</span>
            <span class="watch-inbox-text">${escapeHtml(String(m.summary || "").slice(0, 240))}</span>
          </div>`).join("")}</div>`
      : "";
    // 空态要告诉人"下一步能做什么"，而不是只宣布"这里什么都没有"。
    // 这两个窗口里恰好两件事都能做：直接说话＝开对话；「全文」＝读已有正文。
    if (!lines.length) {
      return inboxHtml + `<div class="watch-empty">这张卡还没有对话记录。<br>
        在下面直接说第一句，就等于开这张卡的对话；<br>
        想先读它已有的正文，点右上「全文」。</div>`;
    }
    return inboxHtml + lines.map((l) =>
      `<div class="watch-line" data-role="${l.role}"><span class="watch-tag">${l.label}</span><span class="watch-text">${escapeHtml(l.text.slice(0, 300))}</span></div>`
    ).join("");
  }

  // 轮询时只刷这一段：输入框、焦点、折叠态、滚动位置全部保留。
  // 只有在"本来就在底部"时才跟着滚到底——用户往上翻看历史时不许被拽回去。
  function updateWatchBodies() {
    const layer = document.getElementById("watchLayer");
    if (!layer) return;
    for (const win of layer.querySelectorAll(".watch-win")) {
      const body = win.querySelector(".watch-body");
      if (!body) continue;
      const cid = win.dataset.id;
      // [画布 2026-10-19 · 自查] 处于「全文」模式的窗口**不能**被这里刷回进程。
      // 原来这行无条件写 watchBodyHtml，于是点了「全文」之后最多 3 秒（下一次轮询）
      // 就被覆盖回对话进程——等于那个按钮只是闪了一下。实测发现，不是设计取舍。
      if (watch.full.has(cid)) {
        const html = watch.fullHtml.get(cid);
        if (html && body.dataset.mode !== "full") {
          body.innerHTML = html;
          body.dataset.mode = "full";
        }
        continue;
      }
      if (body.dataset.mode === "full") { body.dataset.mode = ""; }
      const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 48;
      body.innerHTML = watchBodyHtml(cid);
      if (atBottom) body.scrollTop = body.scrollHeight;
    }
  }

  function renderWatchLayer() {
    const layer = watchLayer();
    if (!watch.open || !watch.ids.length) {
      layer.classList.remove("is-open");
      layer.innerHTML = "";
      return;
    }
    layer.classList.add("is-open");
    layer.innerHTML = watch.ids.map((cid) => {
      const card = state.index.cardMap[cid] || { title: cid };
      const rs = watchRunStateOf(cid);
      const rsLabel = rs === "running" ? "运行中" : rs === "done" ? "已完成"
        : rs === "blocked" ? "受阻" : rs === "paused" ? "已暂停" : "";
      const collapsed = watch.collapsed.has(cid);
      return `
        <section class="watch-win${collapsed ? " is-collapsed" : ""}" data-id="${cid}">
          <header class="watch-head">
            <span class="watch-dot" data-rs="${rs || "idle"}"></span>
            <span class="watch-title" title="${escapeHtml(card.title || cid)}">${escapeHtml(card.title || cid)}</span>
            ${rsLabel ? `<span class="watch-rs" data-rs="${rs}">${rsLabel}</span>` : ""}
            <span class="watch-new" title="你看过之后又有新内容">新</span>
            <span class="watch-sp"></span>
            <button class="watch-btn-full${watch.full.has(cid) ? " is-on" : ""}" type="button"
                    title="${watch.full.has(cid) ? "回到进程" : "读这张卡的全文"}">${watch.full.has(cid) ? "进程" : "全文"}</button>
            <button class="watch-btn-min" type="button" title="${collapsed ? "展开" : "收起"}">${collapsed ? "▸" : "▾"}</button>
            <button class="watch-btn-x" type="button" title="关闭">×</button>
          </header>
          <div class="watch-body">${watch.full.has(cid) ? (watch.fullHtml.get(cid) || '<div class="watch-empty">正在读取全文…</div>') : watchBodyHtml(cid)}</div>
          <div class="watch-foot">
            <textarea class="watch-input" rows="1" placeholder="回复这张卡…（Enter 发送）"></textarea>
            <button class="watch-send" type="button">发送</button>
          </div>
        </section>`;
    }).join("");

    layer.querySelectorAll(".watch-win").forEach((win) => {
      const cid = win.dataset.id;
      // 碰过就算看过：点标题、点正文、点输入框都清"新"标记。
      // 用 pointerdown 捕获阶段，这样点输入框那一刻就清掉，不用等 blur。
      win.addEventListener("pointerdown", () => markWatchSeen(cid), true);
      win.querySelector(".watch-input").addEventListener("focus", () => markWatchSeen(cid));
      win.querySelector(".watch-btn-x").onclick = (e) => { e.stopPropagation(); watchRemove(cid); };
      const fullBtn = win.querySelector(".watch-btn-full");
      if (fullBtn) fullBtn.onclick = async (e) => {
        e.stopPropagation();
        if (watch.full.has(cid)) { watch.full.delete(cid); renderWatchLayer(); return; }
        watch.full.add(cid);
        renderWatchLayer();
        if (watch.fullHtml.has(cid)) return;
        try {
          const data = await API.getCard(WORK_ID, cid);
          const body = (data && data.body) || {};
          const md = String(body.status_block || "");
          watch.fullHtml.set(cid, md
            ? `<div class="watch-full md">${renderMarkdown(md)}</div>`
            : '<div class="watch-empty">这张卡还没有正文</div>');
        } catch (err) {
          watch.fullHtml.set(cid, `<div class="watch-empty">读取失败：${escapeHtml(String(err && err.message || err))}</div>`);
        }
        if (watch.full.has(cid)) renderWatchLayer();
      };
      win.querySelector(".watch-btn-min").onclick = (e) => {
        e.stopPropagation();
        if (watch.collapsed.has(cid)) watch.collapsed.delete(cid); else watch.collapsed.add(cid);
        renderWatchLayer();
      };
      const input = win.querySelector(".watch-input");
      const send = win.querySelector(".watch-send");
      const doSend = async () => {
        const text = input.value.trim();
        if (!text) return;
        input.value = "";
        input.disabled = true; send.disabled = true; send.textContent = "…";
        markLocalRunning(cid, "等待执行");
        try {
          await API.postMessage(WORK_ID, cid, text);
          // 用户自己发的话**不在 SSE 帧里**，而且此刻 lastSeq 很可能已经被
          // 生命周期帧推到它后面 ⇒ 用增量拉会把它跳过（实测：自己发的话不显示）。
          // 这里把游标归零整卡重拉，保证"我说的话"立刻出现在窗口里。
          watch.lastSeq.set(cid, 0);
          watch.items.set(cid, []);
          await loadWatch(cid);
          watchStream(cid);
        } catch (err) {
          notify("发送失败：" + (err && err.message ? err.message : err), "error");
          input.value = text;   // 失败不吞用户输入
        } finally {
          input.disabled = false; send.disabled = false; send.textContent = "发送";
          clearLocalRunning(cid);
          input.focus();
        }
      };
      send.onclick = doSend;
      input.onkeydown = (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); }
      };
      const body = win.querySelector(".watch-body");
      if (body) body.scrollTop = body.scrollHeight;
    });
  }

  // 标记/清除"有新内容"。只切 class，不重建 DOM——轮询每 3 秒跑一次，
  // 每次都重建窗口会把折叠态、焦点、输入内容全冲掉（这个坑已经踩过）。
  function markWatchFresh(cid, on) {
    const win = document.querySelector(`.watch-win[data-id="${cid}"]`);
    if (!win) return;
    win.classList.toggle("is-fresh", !!on);
  }

  // 用户碰了这个窗口（点进去 / 聚焦输入框）⇒ 这之后的内容都算看过了。
  function markWatchSeen(cid) {
    watch.seen.set(cid, watch.lastSeq.get(cid) || 0);
    markWatchFresh(cid, false);
  }

  async function loadWatch(cid) {
    if (!watch.ids.includes(cid)) return;
    const from = watch.lastSeq.get(cid) || 0;
    try {
      const d = await API.getTimeline(WORK_ID, cid, from > 0 ? from : undefined);
      const items = d.items || [];
      if (items.length) {
        const cur = watch.items.get(cid) || [];
        const had = new Set(cur.map((x) => x.seq));
        let added = 0;
        for (const it of items) if (!had.has(it.seq)) { cur.push(it); added++; }
        cur.sort((a, b) => (a.seq || 0) - (b.seq || 0));
        watch.items.set(cid, cur);
        watch.lastSeq.set(cid, cur[cur.length - 1].seq || 0);
        // 增量到了、而且用户没在看这个窗口 ⇒ 挂"新"标记。
        // 正在看的窗口不标：内容就在眼前滚，再标一个"新"是噪音。
        if (added > 0 && !watch.seen.has(cid)) watch.seen.set(cid, 0);
        if (added > 0 && watch.lastSeq.get(cid) > (watch.seen.get(cid) || 0)) {
          markWatchFresh(cid, true);
        }
      }
    } catch (e) { /* 单张卡拉不到不影响其它窗口 */ }
    updateWatchBodies();   // 只刷进程区，不动输入框
  }

  // ── 每卡一条 SSE：只当"有新进展"的**触发器**，不承载内容 ──
  //
  // 实测（抓原始帧）：`/cards/{id}/stream` 只发生命周期帧
  //   run_start / context / round_start / deliver / proposal / done
  // **没有** user_message / assistant_text / tool_call —— 内容只在时间线里。
  // 所以第一版"把帧直接塞进窗口"是错的：它什么都显示不出来，还把轮询关掉了。
  //
  // 现在的分工：轮询负责**内容**（3 秒一次，可靠），SSE 负责**及时性**——
  // 一收到帧就立刻重拉这张卡的时间线（250ms 防抖），阶段推进（开始/交付/收口）
  // 都能在毫秒级反映到窗口上，而不用等下一个 3 秒。
  function watchStream(cid) {
    if (!watch.ids.includes(cid)) return;
    const old = watch.es.get(cid);
    if (old) { try { old.close(); } catch (e) { /* 已关 */ } }
    try {
      const es = new EventSource(`/api/works/${WORK_ID}/cards/${cid}/stream?from=0`);
      watch.es.set(cid, es);
      es.onopen = () => { watch.esDead.delete(cid); };
      es.onmessage = () => { scheduleWatchReload(cid); };
      es.onerror = () => {
        // 断了就不重连（断网时狂重连只会更糟）；轮询本来就一直在跑，内容不会停。
        watch.esDead.add(cid);
        try { es.close(); } catch (e) { /* 已关 */ }
        watch.es.delete(cid);
      };
    } catch (e) { watch.esDead.add(cid); }
  }

  const _watchReloadTimers = new Map();
  function scheduleWatchReload(cid) {
    const t = _watchReloadTimers.get(cid);
    if (t) clearTimeout(t);
    _watchReloadTimers.set(cid, setTimeout(() => {
      _watchReloadTimers.delete(cid);
      if (watch.ids.includes(cid)) loadWatch(cid);
    }, 250));
  }

  // 内容通道＝轮询（可靠、不依赖连接状态）；SSE 只负责把"该重拉了"提前通知。
  // 两张卡同时跑也不会互相拖慢：每次只拉增量（from=lastSeq）。
  function startWatchPoll() {
    if (watch.timer) return;
    watch.timer = setInterval(() => {
      if (!watch.open || !watch.ids.length) return;
      if (document.hidden) return;                 // 后台标签页不空转
      for (const cid of watch.ids) {
        loadWatch(cid);
        if (watch.esDead.has(cid)) watchStream(cid);   // SSE 断了的顺手重连
      }
    }, 3000);
  }
  function stopWatchPoll() {
    if (watch.timer) clearInterval(watch.timer);
    watch.timer = 0;
  }

  // 打开时的默认选择：优先"正在跑"的卡，其次最近更新过的。用户点一次就能看到
  // 最该盯的那几张，而不是先看到空面板再去挑。
  // [画布 2026-10-19] 自动挑选的优先级：**有东西可看** > 正在跑 > 最近更新。
  //
  // 原来只有"正在跑 → 最近更新"。实测在一个没有自动任务的项目里，打开并行层
  // 得到的是三个空窗口（最近更新的是被人在画布上改过标题的卡，它们根本没有对话），
  // 第一眼就是"这功能没用"。有未读的卡一定有信进来过，那才是值得盯的。
  // ══════════════════════════════════════════════════════════════════════════
  // 监控态：有任务在跑，画布自动变成控制台
  // ──────────────────────────────────────────────────────────────────────────
  // [画布 2026-10-19 · 用户点破] 画布的身份不是"显示卡片的容器"，是**多路实时作业的
  // 控制台**：多条线同时跑、实时看进展、就地回复、随时知道全局。这四件事列表做不到
  // ——列表一次只能看一个会话，也看不见"谁在跑、跑到哪"。
  //
  // 我把它做成了需要发现的功能（藏在工具栏按钮里），所以它等于不存在。
  // 现在：**只要有卡在跑，画布自己进入监控态**——不用找、不用点。
  // 没有卡在跑时什么都不做（不打扰）。
  // ══════════════════════════════════════════════════════════════════════════
  let _autoWatchDone = false;
  function autoWatchIfRunning() {
    if (_autoWatchDone || watch.open) return;
    const running = (state.data.cards || [])
      .filter((c) => c.auto_mode && c.goal_state === "active")
      .slice(0, WATCH_MAX);
    if (!running.length) return;
    _autoWatchDone = true;
    running.forEach((c) => watchAdd(c.card_id));
    notify(`有 ${running.length} 条线正在跑，已自动打开监控`, "info");
  }

  function watchAutoPick() {
    const cards = state.data.cards || [];
    const unread = state.async._unreadByCard || {};
    // 三档优先级拼成一条有序列表，再取前 3 个**去重**的。
    // 不能"有未读就只开未读"——只有 1 张有未读时，打开并行层就只出现一个窗口，
    // 而并行层的意思是"同时盯几条线"，它应该尽量填满。
    const byUnread = cards
      .filter((c) => Number(unread[c.card_id] || 0) > 0)
      .sort((a, b) => Number(unread[b.card_id]) - Number(unread[a.card_id]));
    const byRunning = cards.filter((c) => c.auto_mode && c.goal_state === "active");
    const byRecent = [...cards].sort(
      (a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || ""))
    );
    const seen = new Set();
    const out = [];
    for (const c of [...byUnread, ...byRunning, ...byRecent]) {
      if (seen.has(c.card_id)) continue;
      seen.add(c.card_id);
      out.push(c.card_id);
      if (out.length >= 3) break;
    }
    return out;
  }

  const watchBtnEl = document.getElementById("watchBtn");
  if (watchBtnEl) {
    watchBtnEl.onclick = () => {
      if (watch.open && watch.ids.length) {          // 再点一次＝收起整个层（不丢窗口）
        watch.open = false;
        renderWatchLayer();
        syncWatchBtn();
        return;
      }
      if (!watch.ids.length) {
        const picks = watchAutoPick();
        if (!picks.length) { notify("还没有卡片可以盯", "warn"); return; }
        picks.forEach((cid) => watchAdd(cid));
      } else {
        watch.open = true;
        renderWatchLayer();
        syncWatchBtn();
        startWatchPoll();
      }
    };
  }

  // 初始加载
  reloadCanvas();

  // [迭代四 F2③] 模块导出（原 window.SetViewRoot/ReloadCanvas/GetCanvasState 语义）
  export { setViewRoot, reloadCanvas };

  // ══════════════════════════════════════════════════════════════════════════
  // 实时活态（pulse）—— 画布区别于列表的第一件事：**谁在跑，一眼看见**
  //
  // [画布 2026-10-19 · 用户令] "随时了解状况"。
  //
  // 之前画布上的运行态来自卡片的 `goal_state` 字段——那是**持久化的目标状态**，
  // 只在 reloadCanvas 时读一次。后果：你在卡里发一条消息，模型跑了 3 分钟，
  // 画布上那张卡从头到尾没有任何变化。用户必须点进去才知道它在跑。
  //
  // 而系统本来就有准确的内存态：`progress` 记录每张卡当前的阶段与 detail
  // （"调用工具 · read_file"），`/api/nav/running` 就是读它。左栏用了，画布没用。
  //
  // 这里 3 秒拉一次 `/pulse`（一次请求覆盖全部卡 + 最近活动），并且：
  //   · 卡面那一行从"最后一条对话"换成"正在做什么"（高度不变，不引起重排抖动）
  //   · 卡片左侧呼吸竖条 + 描边（动的东西＝还要等的东西）
  //   · 顶栏出现"N 运行中"，点一下＝选中这些卡并适配视野（并行处理多任务的入口）
  //   · 页面隐藏时不轮询（后台标签页不烧 CPU）
  // ══════════════════════════════════════════════════════════════════════════
  const PULSE_MS = 3000;
  const _STAGE_CN = {
    started: "已提交", streaming: "生成中", tools: "调用工具",
    viewpoints: "三席分析", synthesize: "统合中", cards: "建卡",
    done: "完成", error: "出错",
  };
  let _liveApplied = new Map();   // cid -> 上次写入的文本（文本没变就不碰 DOM）

  function liveText(r) {
    const stage = _STAGE_CN[r.stage] || r.stage || "运行中";
    const detail = String(r.detail || "").trim();
    // detail 与阶段同名时不重复（"调用工具 · 调用工具"）
    return detail && detail !== r.stage && detail !== stage
      ? `${stage} · ${detail}` : stage;
  }

  const _readKey = () => `cvRead:${WORK_ID}`;
  function _readMap() {
    try { return JSON.parse(localStorage.getItem(_readKey()) || "{}") || {}; } catch (e) { return {}; }
  }
  function isCardUnread(card) {
    const lm = card && card.last_msg;
    if (!lm || !lm.ts) return false;
    const seen = _readMap()[card.card_id];
    return !seen || String(lm.ts) > String(seen);
  }
  function markCardRead(cid, ts) {
    if (!cid) return;
    const m = _readMap();
    const stamp = ts || new Date().toISOString();
    if (!m[cid] || String(stamp) > String(m[cid])) m[cid] = String(stamp);
    try { localStorage.setItem(_readKey(), JSON.stringify(m)); } catch (e) { }
    const node = nodeMap.get(cid);
    if (node) delete node.dataset.unread;
  }


  // 乐观运行态：点发送的**那一刻**画布就要有反应。
  // 服务端 progress 要等模型链路真正开始才写，中间有几百毫秒到几秒的空白；
  // 这段时间卡片如果毫无变化，用户会以为没发出去、再点一次。
  // __local 标记让 applyPulse 保留它（真数据一到就覆盖），请求结束再清。
  function markLocalRunning(cid, detail) {
    if (!state.async.running) state.async.running = new Map();
    state.async.running.set(cid, {
      stage: "started", detail: detail || "等待执行",
      ts: Date.now() / 1000, __local: true,
    });
    applyLiveStates();
  }
  function clearLocalRunning(cid) {
    const cur = state.async.running && state.async.running.get(cid);
    if (cur && cur.__local) {
      state.async.running.delete(cid);
      applyLiveStates();
    }
  }

  function applyLiveStates() {
    const run = state.async.running || new Map();
    const queued = state.async.queued || new Map();
    const now = Date.now();
    let added = false;
    for (const [cid, node] of nodeMap) {
      const r = run.get(cid);
      if (r) queued.delete(cid);            // 真正开跑了，排队标记功成身退
      // 排队超时兜底：10 分钟还没轮上（被前一张饿死 / 服务端丢了）就不再骗人
      if (!r && queued.has(cid) && now - queued.get(cid) > 10 * 60 * 1000) queued.delete(cid);
      const isQueued = !r && queued.has(cid);
      const liveState = r ? "running" : isQueued ? "queued" : "";
      if (liveState) node.dataset.live = liveState;
      else delete node.dataset.live;

      let line = node.querySelector(".cv-msgline");
      if (liveState) {
        const want = r ? liveText(r) : "排队中";
        if (!line) {
          line = document.createElement("div");
          line.className = "cv-msgline cv-live-line";
          const row = node.querySelector(".cv-card-row");
          if (row && row.nextSibling) node.insertBefore(line, row.nextSibling);
          else if (row) node.appendChild(line);
          else node.appendChild(line);
          added = true;
        }
        if (_liveApplied.get(cid) === liveState + "|" + want) continue;
        // data-who 用 "live"/"queued" 两个语义值（CSS 与验收都按它判），
        // 不用 "running"——它是节点级 dataset.live 的取值，混用会让样式选择器落空。
        line.dataset.who = r ? "live" : "queued";
        line.innerHTML = `<span class="cv-msg-who">${r ? "跑" : "等"}</span>${escapeHtml(want)}`;
        line.title = r ? `正在运行：${want}` : "已提交，正在排队（串行执行）";
        _liveApplied.set(cid, liveState + "|" + want);
      } else if (line && (line.dataset.who === "live" || line.dataset.who === "queued")) {
        // 跑完/放弃排队：换回投影里的最后一条对话（没有就删掉这一行）
        const card = state.index.cardMap[cid] || {};
        const html = msgLineHtml(card);
        if (html) line.outerHTML = html;
        else line.remove();
        _liveApplied.delete(cid);
        added = true;
      }
    }
    if (added) scheduleReflow();
    renderProjectStat();
  }

  async function pollPulse() {
    if (document.hidden) return;
    const d = await API.getPulse(WORK_ID, 30);
    if (!d) return;
    state.async.pulse = d;
    const prev = state.async.running || new Map();
    const next = new Map();
    for (const r of (d.running || [])) next.set(r.card_id, r);
    // [画布 2026-10-19 · 手感] 刚发出去、服务端还没写进 progress 的那几秒，
    // 本地乐观标记要留住——否则用户点完发送，卡片先亮一下、3 秒后又灭，
    // 看起来像"没发出去"。服务端一旦报出真状态就覆盖它（真数据优先）。
    for (const [cid, r] of prev) {
      if (r && r.__local && !next.has(cid)) next.set(cid, r);
    }
    state.async.running = next;
    applyLiveStates();
    renderActivity(d.recent || []);
  }

  // ── 项目动态抽屉（活动流）─────────────────────────────────────────────────
  //
  // 为什么是抽屉不是常驻横条：画布是空间的，常驻时间轴会挤压内容；但"最近发生了什么"
  // 又必须随时能看。折中＝顶栏一个入口（带未读点），点开是右侧浮层，默认不占空间。
  // 这不是"再放一个列表"——列表里的每条都**连着卡**，点一条就飞到那张卡；
  // 而侧边栏的列表只告诉你"有这张卡"，不告诉你"它刚被改了什么"。
  const actDrawer = document.getElementById("activityDrawer");
  const actList = document.getElementById("activityList");
  const actBtn = document.getElementById("activityBtn");
  let _actSig = "";
  let _actOpen = false;

  function _actSeenKey() { return `cvActSeen:${WORK_ID}`; }
  function _actSeen() {
    try { return localStorage.getItem(_actSeenKey()) || ""; } catch (e) { return ""; }
  }
  function _actMarkSeen() {
    const first = (state.async.pulse?.recent || [])[0];
    if (!first) return;
    try { localStorage.setItem(_actSeenKey(), first.ts || ""); } catch (e) { /* 零阻断 */ }
    _paintActDot(0);
  }
  function _paintActDot(n) {
    if (!actBtn) return;
    let dot = actBtn.querySelector(".cv-act-dot");
    if (n > 0) {
      if (!dot) {
        dot = document.createElement("span");
        dot.className = "cv-act-dot";
        actBtn.appendChild(dot);
      }
      dot.textContent = n > 9 ? "9+" : String(n);
    } else if (dot) dot.remove();
  }

  function _fmtClock(iso) {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }
  function _dayLabel(iso) {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "更早";
    const now = new Date();
    const same = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
    if (same(d, now)) return "今天";
    const y = new Date(now); y.setDate(y.getDate() - 1);
    if (same(d, y)) return "昨天";
    return `${d.getMonth() + 1}月${d.getDate()}日`;
  }

  function renderActivity(recent) {
    if (!actList) return;
    const run = state.async.running || new Map();
    const queued = state.async.queued || new Map();
    const seen = _actSeen();
    const unseen = recent.filter((e) => e.ts && (!seen || e.ts > seen)).length;
    _paintActDot(_actOpen ? 0 : unseen);

    // 3 秒一次的轮询不能每次重排列表（会闪、会打断阅读）。
    // 签名＝首条时间＋条数＋运行中/排队中卡数。
    const sig = `${recent[0]?.ts || ""}|${recent.length}|${recent.map(e => e.card_id + e.detail).join("|").length}|${run.size}|${queued.size}|${_actOpen ? 1 : 0}`;
    if (sig === _actSig) return;
    _actSig = sig;

    const parts = [];
    // ① 运行中 + 排队中（时间性的第一层：现在正在发生 / 即将发生）
    const liveN = run.size + queued.size;
    if (liveN) {
      parts.push(`<div class="cv-act-sec">正在运行 <b>${liveN}</b></div>`);
      for (const [cid, r] of run) {
        const c = state.index.cardMap[cid] || {};
        parts.push(`<button class="cv-act-row is-live" data-cid="${escapeHtml(cid)}">
          <span class="cv-act-dot-live"></span>
          <span class="cv-act-body"><span class="cv-act-title">${escapeHtml(c.title || cid)}</span>
          <span class="cv-act-detail">${escapeHtml(liveText(r))}</span></span></button>`);
      }
      for (const [cid] of queued) {
        if (run.has(cid)) continue;
        const c = state.index.cardMap[cid] || {};
        parts.push(`<button class="cv-act-row is-queued" data-cid="${escapeHtml(cid)}">
          <span class="cv-act-dot-live is-queued"></span>
          <span class="cv-act-body"><span class="cv-act-title">${escapeHtml(c.title || cid)}</span>
          <span class="cv-act-detail">排队中 · 等前一张跑完</span></span></button>`);
      }
    }
    // ② 最近活动（时间倒序，按天分组）
    //
    // [画布 2026-10-19 · 信息密度] 原始事件是**逐次编辑**：同一张卡一分钟里改 4 次
    // 正文，就是 4 条一模一样的"由「X」对话编辑正文"。实测 30 条里 12 条是这种重复，
    // 时间线变成"一张卡的编辑流水"，反而看不出项目在动什么。
    // 相邻同卡同类事件合并成一条（×N），保留最新时间。
    const merged = [];
    for (const e of recent) {
      const last = merged[merged.length - 1];
      if (last && last.card_id === e.card_id && last.detail === e.detail) {
        last.n = (last.n || 1) + 1;
        continue;
      }
      merged.push({ ...e, n: 1 });
    }
    let day = "";
    for (const e of merged) {
      const dl = _dayLabel(e.ts);
      if (dl !== day) {
        day = dl;
        parts.push(`<div class="cv-act-sec">${dl}</div>`);
      }
      parts.push(`<button class="cv-act-row" data-cid="${escapeHtml(e.card_id)}" title="${escapeHtml(e.ts || "")}">
        <span class="cv-act-time">${_fmtClock(e.ts)}</span>
        <span class="cv-act-body"><span class="cv-act-title">${escapeHtml(e.title || e.card_id)}${e.n > 1 ? `<span class="cv-act-n">×${e.n}</span>` : ""}</span>
        <span class="cv-act-detail">${escapeHtml(e.detail || "")}</span></span></button>`);
    }
    actList.innerHTML = parts.join("") || `<div class="cv-act-empty">这个项目还没有活动记录。</div>`;

    actList.querySelectorAll(".cv-act-row").forEach((row) => {
      row.addEventListener("click", () => {
        const cid = row.dataset.cid;
        if (!cid || !state.index.cardMap[cid]) return;
        revealCards([cid]);
        selectOnly(cid);
        focusCard(cid, { keepZoom: true });
      });
    });
  }

  function setActivityOpen(on) {
    _actOpen = !!on;
    if (actDrawer) actDrawer.classList.toggle("is-open", _actOpen);
    if (actBtn) {
      actBtn.setAttribute("aria-pressed", _actOpen ? "true" : "false");
      actBtn.classList.toggle("is-on", _actOpen);
    }
    if (_actOpen) {
      _actMarkSeen();
      _actSig = "";                 // 强制重排（打开态不同）
      renderActivity(state.async.pulse?.recent || []);
    } else {
      _paintActDot(0);
    }
  }

  if (actBtn) {
    actBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      setActivityOpen(!_actOpen);
    });
  }
  const actClose = document.getElementById("activityClose");
  if (actClose) actClose.addEventListener("click", () => setActivityOpen(false));

  // 初始 + 轮询（页面隐藏时不跑）
  pollPulse();
  setInterval(pollPulse, PULSE_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollPulse(); });

  // [迭代四 F4④] 未读角标数据面：拉 K4 端点 unread_by_card → 更新徽章；
  // 收件方自己的键上监听 SSE card_message 帧增量（分键 A21）；60s 轮询兜底。
  // [画布 5711 · P0 修] 增量帧改由**工作级**流带过来（见下面 subscribeWorkEvents）：
  // 原来那条独立 SSE 监听了**错的键**（card_id 位置传的是 work_id）⇒ 从来没触发过。
  let _unreadTimer = 0;
  function scheduleUnreadRefresh() {
    if (_unreadTimer) clearTimeout(_unreadTimer);
    _unreadTimer = setTimeout(() => { _unreadTimer = 0; refreshUnread(); }, 300);
  }
  async function refreshUnread() {
    try {
      const r = await fetch(`/api/works/${WORK_ID}/card-messages?limit=200`);
      if (!r.ok) return;
      const d = await r.json();
      // [画布 2026-10-19] 同一份响应顺手喂给第二层「通信线」——不再单独请求。
      // 通信线只画 30 分钟内的信（见 renderEdges），所以这里存全量、由渲染侧筛。
      state.data.cardMsgs = d.items || [];
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
      // 有新的卡间信 ⇒ 通信线要跟着出现/刷新（数据变了，几何也要重画）
      if (lastProjection) renderEdges(lastProjection, collectViewModel());
    } catch (e) { /* 展示层零阻断 */ }
  }
  // [画布 5711 · P0 修] **不再单独开一条"单卡 stream"**。
  //
  // 原来开的是 `/api/works/{w}/cards/{w}/stream` —— card_id 那个位置传的是
  // **work_id**（不是卡号）⇒ 订阅的是一条**永远不会有帧**的键 ⇒
  //   ① 未读角标的增量通道**从来没活过**（只剩 60s 轮询）；
  //   ② 却**长期占着一条浏览器连接**——而浏览器每源只有 6 条并发连接（HTTP/1.1），
  //      画布页的常驻连接必须省着用（见 reloadCanvas 里"找根卡不再走网络"那段）。
  // ⇒ 现在：未读增量由**工作级**流（/events）带过来——后端落卡间信时额外广播
  //   一帧 card_message（card_tools.py），画布一条连接收全。
  setInterval(refreshUnread, 60000);
  refreshUnread();

  // ══════════════════════════════════════════════════════════════════════
  // [画布 5711] 工作级实时更新 —— "不用刷新才更新"
  // ══════════════════════════════════════════════════════════════════════
  //
  // 病根（实测复现，不是推测）：画布上卡片的**结论／待裁定数／work_state／
  // 最新对话／运行态**只写在 createNode 里 ⇒ 只有 renderAll（全量重建）才生效；
  // 而 renderAll **只由用户自己的操作触发**（建卡、删卡、重命名、拖动落定…）。
  // 后台真实发生的变化——模型写卡、裁定被记下、自动模式推进——**到不了画布**。
  // ⇒ 用户看到的画布永远停在"我上次点它时"的样子 ⇒ 必须手动刷新页面。
  //
  // 断点（逐条核实过）：
  //   ① 既有 SSE 是**单卡**的（订阅表 key=(work,card)），没有工作级广播；
  //   ② canvas.js 订阅的是 card_id=WORK_ID（根卡）那一条 ⇒ 别的卡收不到；
  //   ③ /works/{w}/pulse 的 running/recent 不驱动卡面重绘（applyLiveStates
  //      只写 dataset.live 与一行消息条，conclusion/pending_count/work_state
  //      一个都不碰）；
  //   ④ 结论/待裁定/work_state/runstate 都写在 createNode 里。
  //
  // 解法：订阅 /api/works/{w}/events（后端写卡时推一帧 card_changed，载荷带
  // 那张卡的最新投影摘要）→ **就地换掉那一张卡的节点**。
  // 不重建整棵树 ⇒ 零闪烁、零全量布局重排、毫秒级。
  //
  // 为什么是"换节点"而不是"逐字段改"：卡面有十几个字段（标题／状态点／待裁定数／
  // 引用数／定论／最新对话／多久没动／子卡数…）散在 DOM 多个位置，逐字段映射
  // 必然漏、必然漂。createNode 是卡面的**唯一构造口**——换节点＝复用它，
  // 于是"事件驱动的卡面"与"首屏的卡面"不可能不一致。
  // ══════════════════════════════════════════════════════════════════════

  // ── 订阅工作级事件流 ──
  //
  // [画布 5711] 订阅**健康度**单独记一个标志：EventSource 对象存在 ≠ 连接可用
  // （企业代理会"接受连接但不投递"，onerror 甚至可能不触发）。兜底轮询必须按
  // **健康度**决定接管与否，不能只看对象在不在——否则 SSE 静默失效时兜底永远
  // 不上场，画布就永远不更新（这正是"必须刷新"的另一种成因）。
  let _workEs = null;
  let _workEsHealthy = false;
  const _liveQueue = new Map();   // card_id → 最新载荷（同一张卡的多次变化合并）
  let _liveTimer = 0;

  function scheduleLiveApply(cardId, payload) {
    _liveQueue.set(cardId, payload);
    if (_liveTimer) return;
    // 合并到一帧：多张卡同时变 ⇒ 一次批量应用（不是 N 次 DOM 抖动）。
    _liveTimer = requestAnimationFrame(() => {
      _liveTimer = 0;
      flushLiveApply();
    });
  }

  function flushLiveApply() {
    if (!_liveQueue.size) return;
    const items = [..._liveQueue.entries()];
    _liveQueue.clear();
    let structureChanged = false;
    for (const [cid, payload] of items) {
      if (applyCardChange(cid, payload)) structureChanged = true;
    }
    if (structureChanged && lastProjection) {
      renderEdges(lastProjection, collectViewModel());
    }
    // 顶栏项目概览（待裁定总数／结论分布）跟着变——它读的是 state.data.cards。
    renderProjectStat();
  }

  // 返回 true 表示"结构/几何变了"（需要重画连线）。
  function applyCardChange(cardId, payload) {
    const kind = String(payload.kind || "updated");
    const card = payload.card || null;

    if (kind === "deleted") {
      // [画布 5711 · 修] **两张表都要改**：state.data.cards 与 state.index。
      //
      // 病根（实测捕获的严重回归）：本函数原来只改 state.index，而**布局算法读的是
      // state.data.cards**（collectProjectionInput → computeLayout(input.cards…)）。
      // 两张表一分裂，布局算出来的位置里就没有这张卡 ⇒ 增量渲染写不回坐标 ⇒
      // 节点永远停在占位坐标 (0,0) 且带 .entering（入场动画起点 opacity:0）
      // ⇒ **整片画布的卡片全部不可见，只剩连线**。
      // 这是"索引是派生数据、原始列表才是真相"的典型：只改派生数据 = 制造不一致。
      const _ci = state.data.cards.findIndex((c) => c && c.card_id === cardId);
      if (_ci >= 0) state.data.cards.splice(_ci, 1);
      delete state.index.cardMap[cardId];
      state.view.selected.delete(cardId);
      const node = nodeMap.get(cardId);
      if (node) { node.remove(); nodeMap.delete(cardId); }
      // 索引重建（childMap 由 cards 派生 ⇒ 必须整体重建，不能局部补丁）
      rebuildCardMap();
      // 子树没了 ⇒ 布局要重排（后面的卡要往上收）＋连线要重画。
      reflowAll();
      return true;
    }

    if (!card) return false;
    // [画布 5711 · 修] 同样两张表都要改（见上）。卡片是**新增**时进 cards 列表，
    // 是**更新**时就地替换——布局读的就是这张列表。
    //
    // ⚠ 新卡必须**按 card_id 升序插到正确位置**，不能 push 到末尾：
    //   childMap 的顺序决定同层卡的排布顺序，顺序一乱，布局就和"刷新后"不一样
    //   ⇒ 实时更新过的画布与刷新过的画布**长得不一样**（实测：e2e 的坐标点击
    //   因此落到不同的卡上，表现为"点击行为与基线不一致"）。
    //   服务端投影是 ORDER BY card_id，这里保持同口径 ⇒ 两条路径产出同一布局。
    const _arr = state.data.cards;
    let _ci = -1;
    for (let _k = 0; _k < _arr.length; _k++) {
      if (_arr[_k] && _arr[_k].card_id === cardId) { _ci = _k; break; }
    }
    if (_ci >= 0) {
      _arr[_ci] = card;
    } else {
      let _j = _arr.length;
      while (_j > 0 && _arr[_j - 1] && String(_arr[_j - 1].card_id) > String(cardId)) _j--;
      _arr.splice(_j, 0, card);
    }
    const parentId = card.parent || "";
    // 索引整体重建（childMap 由 cards 派生）
    rebuildCardMap();

    const onCanvas = nodeMap.has(cardId);
    if (onCanvas) {
      // 画布上那张卡 ⇒ 就地换掉它的节点（内容、状态点、待裁定数、最新对话全跟着变）。
      refreshCardNode(cardId, card);
    }
    // 父卡的"子卡数"徽章也可能变了（新卡/删卡）⇒ 父卡也要刷一次。
    const parentNode = parentId ? nodeMap.get(parentId) : null;
    if (parentNode) {
      const pcard = state.index.cardMap[parentId];
      if (pcard) refreshCardNode(parentId, pcard);
    }
    // 新卡若其父卡在场且已展开，它应当**出现**（增量入口：只动父卡这一单元）。
    if (kind === "created" && parentId && nodeMap.has(parentId)
        && state.view.expanded.has(parentId)) {
      renderSubtree(parentId);
      _assertNoGhostNodes();
      return true;
    }
    // 新卡若父卡不在画布上（或没展开）⇒ 画布结构没变，数据更新即可。
    return false;
  }

  // [画布 5711] **半渲染自检**：增量渲染之后，检查是否还有节点停在占位坐标。
  //
  // 为什么要有这道闸：增量路径（renderSubtree）失败时**不报错**——节点建出来了，
  // 但坐标写不进去（因为布局里没有它），节点就永远停在 (0,0) 且带 .entering
  // （入场动画的起点是 opacity:0）⇒ **卡片不可见**。而这类失败在控制台上是安静的，
  // 用户只看到"画布空了"。
  //
  // 判据（机械、可测）：在场节点里若还有"既是 (0,0)、又带 .entering"的 ⇒
  // 说明这一轮增量渲染没把坐标落到位 ⇒ 退回**全量重绘**。
  // 全量重绘慢一点，但一定对；半渲染错一点点，就是整片看不见。
  function _assertNoGhostNodes() {
    let ghosts = 0;
    for (const [cid, node] of nodeMap) {
      // 根卡本来就坐在 (0,0)（它是布局原点）⇒ 不算幽灵。
      if (cid === state.nav.viewRoot) continue;
      const l = parseFloat(node.style.left);
      const t = parseFloat(node.style.top);
      if (l === 0 && t === 0 && node.classList.contains("entering")) ghosts++;
    }
    if (!ghosts) return true;
    console.warn("[canvas] 增量渲染留下未落位的节点（" + ghosts + " 张）⇒ 退回全量重绘");
    renderAll();
    return false;
  }

  // 更新一张卡：**保留节点身份，只换卡面**。
  //
  // [画布 5711] 为什么不换节点（实测回归）：
  //   早先的做法是 `old.replaceWith(fresh)` —— 后台一次更新就把用户**正要点的**
  //   那张卡换掉了，那个 click 落在一个已脱离文档的节点上 ⇒ 点了没反应。
  //   实测：e2e「A10a 单击＝进入对话页」在换节点版本上 FAIL（URL 停在
  //   /work/work_001），保留节点的版本 PASS。真实场景同构：用户点卡的瞬间
  //   后台写了这张卡 ⇒ 点击丢失。
  //
  // 现在：节点身份保留 ⇒ 监听器、焦点、拖拽态、展开的对话单元全都在；
  // 只把**卡面**搬过去。createNode 仍是卡面的唯一构造口（不重复实现卡面）。
  function refreshCardNode(cardId, card) {
    const old = nodeMap.get(cardId);
    if (!old) return false;
    const vm = collectViewModel();
    const pos = { x: parseFloat(old.style.left) || 0, y: parseFloat(old.style.top) || 0 };
    const depth = depthFromViewRoot(cardId);
    // 用 createNode 生成一份新卡面（它是卡面的唯一真相），只取内容不取节点。
    const fresh = createNode(card, pos, vm, depth);
    // LOD 档位：createNode 不加档位类（档位由 applyLODClasses 统一刷）。
    // 老节点自带档位类（节点没换，档位不变）⇒ 这里不需要动。
    const wasOpen = old.dataset.open === "1";
    old.innerHTML = fresh.innerHTML;
    // [画布 5711] 卡面被换掉 ⇒ "正在打开对话…"的瞬时状态一并作废（它属于旧卡面）。
    old.classList.remove("is-opening");
    // 数据属性同步：fresh 有的搬过去，没有的删掉（否则旧值会赖着不走）。
    for (const k of ["ws", "pending", "ck", "weight", "basew", "fresh",
                     "unread", "runstate", "depth", "type"]) {
      if (fresh.dataset[k] !== undefined) old.dataset[k] = fresh.dataset[k];
      else delete old.dataset[k];
    }
    old.setAttribute("aria-label", fresh.getAttribute("aria-label") || "");
    old.classList.toggle("is-pinned", fresh.classList.contains("is-pinned"));
    // 卡面里的子元素是新的 ⇒ 重绑它们的监听（节点级监听不动）。
    bindCardFace(old, card);
    // 就地展开的对话单元：内容不能被冲掉（重新拉一次）。
    if (wasOpen) {
      old.dataset.open = "1";
      loadCell(cardId);
    }
    applySelectionClasses();
    return true;
  }

  // ── 订阅工作级事件流 ──
  function subscribeWorkEvents() {
    if (_workEs) return;
    try {
      const es = new EventSource(`/api/works/${WORK_ID}/events`);
      _workEs = es;
      es.onopen = () => { _workEsHealthy = true; };
      es.onerror = () => { _workEsHealthy = false; /* 断线 ⇒ 兜底接管 */ };
      es.onmessage = (m) => {
        _workEsHealthy = true;
        let evt = null;
        try { evt = JSON.parse(m.data); } catch (e) { return; }
        if (!evt) return;
        const d = evt.data || {};
        // [画布 5711 · P0 修] 卡间信帧：工作级通道顺带带过来（后端落信时多广播一帧）
        // ⇒ 未读角标与通信线能及时刷新。原来这条走的是一条**监听了错键**的独立
        // SSE（card_id 位置传的是 work_id）⇒ 从来没触发过，只有 60s 轮询在扛。
        if (evt.type === "card_message") {
          scheduleUnreadRefresh();
          return;
        }
        if (evt.type !== "card_changed") return;
        if (!d.card_id) return;
        scheduleLiveApply(d.card_id, d);
      };
    } catch (e) {
      // EventSource 不可用（老浏览器/代理）⇒ 退回轮询兜底（见下）。
      _workEs = null;
      _workEsHealthy = false;
    }
  }
  subscribeWorkEvents();
  // 兜底：SSE 不可用或**不健康**时，用卡片版本指纹做低频差分探测。
  // 指纹端点（/api/nav/version）现成，左栏已用它 60s 探测；这里复用同口径，
  // 间隔更短（4s），命中即重拉。
  let _liveFallbackTimer = 0;
  let _liveFallbackSig = "";
  async function liveFallbackProbe() {
    if (_workEsHealthy) return;      // SSE 活着就不轮询
    try {
      const r = await fetch("/api/nav/version");
      if (!r.ok) return;
      const d = await r.json();
      const sig = (d.by_work || {})[WORK_ID] || "";
      if (_liveFallbackSig && sig && sig !== _liveFallbackSig) {
        // 静默重拉：画布上已经有内容了，别再弹骨架屏（那是一次"白一下"）。
        await reloadCanvas({ silent: true });
      }
      _liveFallbackSig = sig;
    } catch (e) { /* 零阻断 */ }
  }
  _liveFallbackTimer = setInterval(liveFallbackProbe, 4000);
  if (_liveFallbackTimer && typeof _liveFallbackTimer.unref === "function") _liveFallbackTimer.unref();
