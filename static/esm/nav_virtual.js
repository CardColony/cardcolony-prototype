/* esm/nav_virtual.js —— [S2/R-16] 导航虚拟滚动：行序列前缀和 + 可视区切片 + DOM 数恒定。
 *
 * - 可视区用「行数组前缀和」计算（混合行高：项目行 30px + 卡行 38px，非 38×n 公式）。
 * - DOM 行数 ＝ 可视区行数 + 上下各 1 缓冲，与总行数无关（V-V1）。
 * - 行元素 absolute 定位在 spacer 内；mount/unmount/patch 委托 renderer 回调。
 * - 滚动（scroll）与容器尺寸变化（ResizeObserver）都触发重切——rail 态浮层是 CSS
 *   hover 显隐、无 JS 事件，只有 RO 能事件驱动地抓到 0→N 的尺寸变化。
 * - sliceRange 是纯函数（测试直接打它，V-V1）。
 */
import { NAV_ROW_HEIGHT, NAV_PHEAD_HEIGHT } from "./state_layers.js?v=32";

// offsets[i] = 第 i 行 top；返回可视行区间（含上下各 buf 行缓冲）与总高
export function sliceRange(offsets, heights, viewTop, viewHeight, buf = 1) {
  const n = offsets.length;
  if (n === 0 || viewHeight <= 0) return { start: 0, end: -1, total: 0 };
  let total = 0;
  for (let i = 0; i < n; i++) total += heights[i];
  // 二分：第一个 bottom > viewTop 的行
  let lo = 0, hi = n - 1, start = n - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (offsets[mid] + heights[mid] > viewTop) { start = mid; hi = mid - 1; }
    else lo = mid + 1;
  }
  // 顺序找：第一个 top >= viewTop + viewHeight 的行（可视底之外）
  let end = start;
  while (end < n && offsets[end] < viewTop + viewHeight) end++;
  end--;   // 最后一个可视行
  return {
    start: Math.max(0, start - buf),
    end: Math.min(n - 1, end + buf),
    total,
  };
}

export function createVirtualList(container, getRows, rowHeight = NAV_ROW_HEIGHT, opts = {}) {
  const renderer = opts.renderer;        // { build(row) → el, patch(el, row) }
  const header = opts.header || null;    // 序列外固定头（如 .nav-hint），滚动偏移计其高
  const buf = opts.buffer != null ? opts.buffer : 1;

  const spacer = document.createElement("div");
  spacer.className = opts.spacerClass || "nav-vspacer";
  if (header) container.appendChild(header);
  container.appendChild(spacer);

  const mounted = new Map();   // rowKey → { el, index }
  let seq = 0;                 // rows 版次：getRows() 结果每变一次 +1，滚动中不重复构建

  function headerH() { return header ? (header.offsetHeight || 0) : 0; }

  function measure(rows) {
    const n = rows.length;
    const offsets = new Array(n);
    const heights = new Array(n);
    let acc = 0;
    for (let i = 0; i < n; i++) {
      const h = (rows[i].rowHeight && rows[i].rowHeight()) || rowHeight;
      offsets[i] = acc;
      heights[i] = h;
      acc += h;
    }
    return { offsets, heights, total: acc };
  }

  function reslice() {
    const rows = getRows() || [];
    const { offsets, heights, total } = measure(rows);
    spacer.style.height = total + "px";
    const viewTop = Math.max(0, container.scrollTop - headerH());
    const viewH = container.clientHeight || 0;
    const { start, end } = sliceRange(offsets, heights, viewTop, viewH, buf);

    const keep = new Set();
    for (let i = start; i <= end; i++) {
      const row = rows[i];
      const key = row.key();
      keep.add(key);
      const m = mounted.get(key);
      if (m) {
        m.index = i;
        m.el.style.top = offsets[i] + "px";
        if (renderer && renderer.patch) renderer.patch(m.el, row);
      } else {
        // [2026-10-29 修·实弹] 一行 build 抛错，原来会把整棵树从那里截断
        // （左栏只剩第一个项目、父卡行消失、评审少几条，全是同一个断点）。
        // 这里逐行兜：坏行只丢自己，其余照常渲染，并在控制台点名是哪一行。
        let el;
        try {
          el = renderer && renderer.build ? renderer.build(row) : document.createElement("div");
        } catch (err) {
          console.error("[nav] 行渲染失败，已跳过", row && row.kind, row && row.card, err);
          el = document.createElement("div");
        }
        el.classList.add(opts.rowClass || "nav-vrow");
        el.style.top = offsets[i] + "px";
        el.__navRow = row;
        spacer.appendChild(el);
        mounted.set(key, { el, index: i });
      }
    }
    // 卸载切片外行（R-13：unmount 原语）
    for (const [key, m] of Array.from(mounted)) {
      if (!keep.has(key)) {
        m.el.remove();
        mounted.delete(key);
      }
    }
    seq++;
    return { count: mounted.size, total, start, end };
  }

  container.addEventListener("scroll", () => { reslice(); }, { passive: true });
  let ro = null;
  if (typeof ResizeObserver !== "undefined") {
    ro = new ResizeObserver(() => { reslice(); });
    ro.observe(container);
  }

  return {
    reslice,
    spacer,
    headerH,   // [S3/R-18/V3-L4] 吸顶/定位消费的在流头部高度（S2 预留口；唯一新增导出，零逻辑改动）
    mountedCount() { return mounted.size; },
    version() { return seq; },
    destroy() {
      if (ro) ro.disconnect();
      for (const [, m] of mounted) m.el.remove();
      mounted.clear();
      spacer.remove();
      if (header) header.remove();
    },
  };
}
