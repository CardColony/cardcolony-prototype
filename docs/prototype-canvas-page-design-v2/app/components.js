/* ═══════════════════════════════════════════════════════════════
   components.js — 组件层（React 形态：函数组件 + hooks + htm 模板）
   位置即状态：布局结果全部受控，连线由同一状态派生（B4 根治）
   ═══════════════════════════════════════════════════════════════ */
const { useState, useEffect, useMemo, useRef, useCallback } = preactHooks;
const html = htm.bind(preact.h);

/* ── 图标库：24 网格 / 1.6 描边 / round 端点 / currentColor ── */
const P = { /* path 数据 */
  crown:'M4 17.5 L4 8.5 L8.5 12 L12 6.5 L15.5 12 L20 8.5 L20 17.5 Z',
  branch:'M6 4.5 L6 19.5 M6 6.5 C6 12 18 10.5 18 17 M18 14.5 L18 19.5 M4.2 4.5 L7.8 4.5 M16.2 19.5 L19.8 19.5 M4.2 19.5 L7.8 19.5',
  cube:'M12 3.2 L20 7.5 L20 16.5 L12 20.8 L4 16.5 L4 7.5 Z M4 7.5 L12 11.8 L20 7.5 M12 11.8 L12 20.8',
  gear:'M12 8.6 A3.4 3.4 0 1 0 12 15.4 A3.4 3.4 0 1 0 12 8.6 M12 2.8 L12 5.4 M12 18.6 L12 21.2 M2.8 12 L5.4 12 M18.6 12 L21.2 12 M5.5 5.5 L7.3 7.3 M16.7 16.7 L18.5 18.5 M18.5 5.5 L16.7 7.3 M7.3 16.7 L5.5 18.5',
  shield:'M12 3 L19 6 L19 12 C19 16.5 15.8 19.6 12 21 C8.2 19.6 5 16.5 5 12 L5 6 Z M8.8 11.8 L11.2 14.2 L15.6 9.6',
  pen:'M4 20 L8.5 19 L19.2 8.3 C19.8 7.7 19.8 6.7 19.2 6.1 L17.9 4.8 C17.3 4.2 16.3 4.2 15.7 4.8 L5 15.5 Z M14.5 6.5 L17.5 9.5',
  monitor:'M4 5 L20 5 L20 16 L4 16 Z M12 16 L12 20 M8.5 20 L15.5 20',
  plus:'M12 5 L12 19 M5 12 L19 12',
  minus:'M5 12 L19 12',
  fit:'M4 9 L4 4 L9 4 M15 4 L20 4 L20 9 M20 15 L20 20 L15 20 M9 20 L4 20 L4 15',
  search:'M10.5 4 A6.5 6.5 0 1 0 10.5 17 A6.5 6.5 0 1 0 10.5 4 M15.3 15.3 L20 20',
  sun:'M12 8 A4 4 0 1 0 12 16 A4 4 0 1 0 12 8 M12 2.5 L12 5 M12 19 L12 21.5 M2.5 12 L5 12 M19 12 L21.5 12 M5.3 5.3 L7 7 M17 17 L18.7 18.7 M18.7 5.3 L17 7 M7 17 L5.3 18.7',
  moon:'M20 13.5 A8 8 0 1 1 10.5 4 A6.5 6.5 0 0 0 20 13.5 Z',
  dots:'M5 12 L5.01 12 M12 12 L12.01 12 M19 12 L19.01 12',
  layers:'M12 3.5 L20.5 8 L12 12.5 L3.5 8 Z M4.5 12 L12 16 L19.5 12 M4.5 16 L12 20 L19.5 16',
  close:'M6 6 L18 18 M18 6 L6 18',
  chevD:'M6 9 L12 15 L18 9',
  chevL:'M14 6 L8 12 L14 18',
  chevR:'M10 6 L16 12 L10 18',
  edit:'M4 20 L8.5 19 L19.2 8.3 C19.8 7.7 19.8 6.7 19.2 6.1 L17.9 4.8 C17.3 4.2 16.3 4.2 15.7 4.8 L5 15.5 Z M14.5 6.5 L17.5 9.5',
  archive:'M4 4 L20 4 L20 8 L4 8 Z M6 8 L6 20 L18 20 L18 8 M10 12 L14 12',
  chat:'M4 5 L20 5 L20 16 L12 16 L8 20 L8 16 L4 16 Z',
  states:'M12 3 L20 7.5 L20 16.5 L12 21 L4 16.5 L4 7.5 Z M12 8 L15.5 10 L15.5 14 L12 16 L8.5 14 L8.5 10 Z',
  play:'M8 5 L19 12 L8 19 Z',
  help:'M12 4 A8 8 0 1 0 12 20 A8 8 0 1 0 12 4 M9.6 9.2 A2.5 2.5 0 1 1 12.6 11.6 C12 12 12 12.5 12 13.2 M12 16.5 L12.01 16.5',
  rule:'M6 3.5 L18 3.5 L18 20.5 L12 16.5 L6 20.5 Z',
  clock:'M12 4 A8 8 0 1 0 12 20 A8 8 0 1 0 12 4 M12 7.5 L12 12 L15 14',
  inbox:'M4 13 L7 5 L17 5 L20 13 L20 19 L4 19 Z M4 13 L9 13 L10 15.5 L14 15.5 L15 13 L20 13',
  alert:'M12 4 L21 19 L3 19 Z M12 10 L12 14 M12 16.5 L12.01 16.5',
  lock:'M6.5 10.5 L6.5 8 A5.5 5.5 0 0 1 17.5 8 L17.5 10.5 M5 10.5 L19 10.5 L19 20 L5 20 Z',
  skull:'M5 4 L19 4 L19 13 L16.5 13 L16.5 17 L14 20 L10 20 L7.5 17 L7.5 13 L5 13 Z M9.3 9 L9.31 9 M14.7 9 L14.71 9',
  msg:'M4 5 L20 5 L20 16 L12 16 L8 20 L8 16 L4 16 Z',
  folder:'M3.5 6.5 L9.5 6.5 L11.5 9 L20.5 9 L20.5 19 L3.5 19 Z',
  scale:'M7 4 L4 4 L4 7 M17 4 L20 4 L20 7 M4 17 L4 20 L7 20 M20 17 L20 20 L17 20 M9.5 9.5 L14.5 14.5',
  cmd:'M8.5 3.5 A2.5 2.5 0 1 0 6 6 L6 8.5 L3.5 8.5 A2.5 2.5 0 1 0 6 11 L8.5 11 L8.5 15.5 L6 15.5 A2.5 2.5 0 1 0 8.5 18 L8.5 20.5 M8.5 11 L15.5 11',
  moon2:'M20 13.5 A8 8 0 1 1 10.5 4 A6.5 6.5 0 0 0 20 13.5 Z',
};
const Icon = ({ n, size=16, class:cls }) => html`<svg viewBox="0 0 24 24" width=${size} height=${size} class=${cls}
  fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d=${P[n]||''} /></svg>`;
const IconDots = ({ size=16 }) => html`<svg viewBox="0 0 24 24" width=${size} height=${size} aria-hidden="true">
  <circle cx="5" cy="12" r="1.3" fill="currentColor"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/><circle cx="19" cy="12" r="1.3" fill="currentColor"/></svg>`;

/* ── 品牌资产（brand/ 规范：金只表品牌；UI 字标 19-20px 暗态） ── */
const BrandMark = ({ size=24 }) => html`<svg width=${size} height=${size} viewBox="0 0 48 48" fill="none" aria-hidden="true">
  <line x1="24" y1="7" x2="24" y2="17" stroke="currentColor" stroke-width="3.5" stroke-linecap="round"/>
  <circle cx="24" cy="6" r="3.4" fill="currentColor"/>
  <path d="M31.5 33.5 A10.5 10.5 0 1 1 31.5 21" stroke="currentColor" stroke-width="5.5" stroke-linecap="round"/>
  <circle cx="18.5" cy="26.5" r="2.6" fill="currentColor"/>
  <circle cx="29.5" cy="26.5" r="2.6" fill="currentColor"/>
</svg>`;
const BrandWordmark = () => html`<span role="img" aria-label="CardColony" style=${{display:'inline-flex',alignItems:'center'}}>
  <svg width="21" height="21" viewBox="0 0 48 48" fill="none" aria-hidden="true" style=${{marginRight:'1px'}}>
    <line x1="24" y1="7" x2="24" y2="17" stroke="currentColor" stroke-width="3.5" stroke-linecap="round"/>
    <circle cx="24" cy="6" r="3.4" fill="currentColor"/>
    <path d="M31.5 33.5 A10.5 10.5 0 1 1 31.5 21" stroke="currentColor" stroke-width="5.5" stroke-linecap="round"/>
    <circle cx="18.5" cy="26.5" r="2.6" fill="currentColor"/>
    <circle cx="29.5" cy="26.5" r="2.6" fill="currentColor"/>
  </svg>
  <svg height="20" viewBox="0 0 178 40" aria-hidden="true" style=${{display:'block'}}>
    <text x="2" y="31" font-family="Noto Sans SC,system-ui,sans-serif" font-size="30" font-weight="650" letter-spacing="-0.5" fill="currentColor" style="opacity:.88">ard</text>
    <text x="52" y="31" font-family="Noto Sans SC,system-ui,sans-serif" font-size="30" font-weight="700" letter-spacing="2.5" fill="currentColor">Colony</text>
  </svg>
</span>`;

/* ── 布局引擎：列对齐 + 纵向紧凑堆叠（照 v3 移植，纯函数） ── */
const NW = 290, PAD = 48, GAPX = 76;
/* B1 根因修复：行距曾是常量 96px，而 full 档卡面实际内容高约 145px → 同列卡纵向压叠、字被遮。
   现在卡高按 LOD 档位显式定义，行距＝卡高＋20 留白，锚点取卡高中点，全部随档联动。 */
const CARD_H = { far: 112, near: 156, full: 192, ultra: 216 };
const cardH = lod => CARD_H[lod] || CARD_H.full;
const ROWGAP = 20;
const rowh = lod => cardH(lod) + ROWGAP;
const kidsOf = id => Object.keys(NODES).filter(k => NODES[k].parent === id)
  .sort((a,b) => NODES[a].ord - NODES[b].ord);
const hasKids = id => kidsOf(id).length > 0;
function visibleIds(collapsed) {
  const out = [];
  (function walk(id) {
    out.push(id);
    if (collapsed.has(id)) return;
    kidsOf(id).forEach(walk);
  })('root');
  return out;
}
/* 纯函数布局：输入折叠集合 → 输出每卡 pos（不碰 DOM） */
function computeLayout(collapsed, lod) {
  const rowH = rowh(lod);
  const pos = {};
  let row = 0;
  (function place(id, d) {
    const kids = collapsed.has(id) ? [] : kidsOf(id);
    const x = PAD + d * (NW + GAPX);
    if (!kids.length) { pos[id] = { x, y: PAD + row * rowH }; row++; return; }
    const ys = kids.map(k => { place(k, d+1); return pos[k].y; });
    pos[id] = { x, y: Math.round((Math.min(...ys) + Math.max(...ys)) / 2) };
  })('root', 0);
  return pos;
}
const LOD = z => z < 58 ? 'far' : (z < 72 ? 'near' : (z < 96 ? 'full' : 'ultra'));
const STATE_VAR = { run:'--st-run', queued:'--st-queued', done:'--st-done', blocked:'--st-blocked', paused:'--st-paused' };
const KIND_VAR  = { master:'--kd-master', manager:'--kd-manager', unit:'--kd-unit', worker:'--kd-worker',
                    qa:'--kd-qa', pm:'--kd-pm', ui:'--kd-ui' };
/* 父子线锚点（照 v3：右锚出、左锚入、三次贝塞尔） */
function pathP(a, b) {
  const dx = Math.max(40, (b.x - a.x) * 0.5);
  return `M ${a.x} ${a.y} C ${a.x+dx} ${a.y}, ${b.x-dx} ${b.y}, ${b.x} ${b.y}`;
}
/* 折叠卡出线锚点：右缘中点（子线从折叠标记出发，不再指向不存在的卡） */
/* 父子线锚点：右缘中点出、左缘中点入；y 由卡的档位高度决定（不再写死 42） */
const midY = (id, pos, lod) => pos[id].y + cardH(lod) / 2;
const anchorR = (id, pos, lod) => ({ x: pos[id].x + NW, y: midY(id, pos, lod) });
const anchorL = (id, pos, lod) => ({ x: pos[id].x, y: midY(id, pos, lod) });

/* ── 通信流星（照 v3：弧长采样拖尾） ── */
const COMM_SEG = 40, COMM_TAIL_MIN = 70, COMM_TAIL_MAX = 240;
const qpt = (p0,p1,p2,u) => { const v=1-u; return { x:v*v*p0.x+2*v*u*p1.x+u*u*p2.x, y:v*v*p0.y+2*v*u*p1.y+u*u*p2.y }; };
function commPaths(comms, pos, visible, now, lod) {
  const out = [];
  comms.forEach(m => {
    const age = (now - m.born) / 1000;
    if (age < 0 || age > PULSE_LIFE) return;
    if (!visible.has(m.a) || !visible.has(m.b)) return;
    const g1 = pos[m.a], g2 = pos[m.b];
    const x1 = g1.x + NW, y1 = midY(m.a, pos, lod), x2 = g2.x, y2 = midY(m.b, pos, lod);
    const lift = Math.min(90, Math.abs(x2-x1)*0.22 + 30);
    const P0 = {x:x1,y:y1}, P1 = {x:(x1+x2)/2, y:(y1+y2)/2 - lift}, P2 = {x:x2,y:y2};
    const pts = [], cum = [0];
    for (let i=0; i<=COMM_SEG; i++) pts.push(qpt(P0,P1,P2,i/COMM_SEG));
    for (let i=1; i<=COMM_SEG; i++) cum.push(cum[i-1] + Math.hypot(pts[i].x-pts[i-1].x, pts[i].y-pts[i-1].y));
    const total = cum[COMM_SEG] || 1;
    const p = Math.max(0, Math.min(1, age/COMM_TRAVEL));
    const headD = total * p;
    const tail = Math.min(COMM_TAIL_MAX, Math.max(COMM_TAIL_MIN, total*0.55));
    const tailD = Math.max(0, headD - tail);
    const idxAt = d => { let k=0; while(k<COMM_SEG && cum[k+1]<d) k++; return k; };
    const hi = idxAt(headD), ti = idxAt(tailD);
    const fade = age > PULSE_LIFE - COMM_FADE ? Math.max(0,(PULSE_LIFE-age)/COMM_FADE) : 1;
    const arrived = age >= COMM_TRAVEL;
    const segs = [];
    const span = Math.max(1, hi - ti);
    for (let k=ti; k<=hi; k++) {
      const u = (k-ti)/span;
      const op = 0.04 + 0.72*Math.pow(u,1.7);
      const w = 0.9 + 2.6*Math.pow(u,1.5);
      const a = pts[k], b = pts[Math.min(hi,k+1)];
      segs.push(html`<path class="comm-tail" d=${`M ${a.x.toFixed(1)} ${a.y.toFixed(1)} L ${b.x.toFixed(1)} ${b.y.toFixed(1)}`}
        stroke-opacity=${op.toFixed(3)} stroke-width=${w.toFixed(2)} />`);
    }
    const head = pts[hi];
    out.push(html`<g opacity=${fade.toFixed(2)}>
      ${segs}
      ${!arrived ? html`<g><circle class="comm-head-glow" cx=${head.x.toFixed(1)} cy=${head.y.toFixed(1)} r="10"/>
        <circle class="comm-head" cx=${head.x.toFixed(1)} cy=${head.y.toFixed(1)} r="3.4"/></g>`
      : html`<g><circle class="comm-head" cx=${x2} cy=${y2} r="3.2" opacity=${Math.max(0,1-(age-COMM_TRAVEL)/0.9).toFixed(2)}/>
        <circle class="comm-dot-ring" cx=${x2} cy=${y2} r=${(4+Math.min(1,(age-COMM_TRAVEL)/0.9)*9).toFixed(1)} opacity=${(0.6*(1-Math.min(1,(age-COMM_TRAVEL)/0.9))).toFixed(2)}/></g>`}
      <title>${m.txt||''}</title></g>`);
  });
  return out;
}

/* ── 卡片节点 ── */
function NodeCard({ id, node, pos, zoom, lod, collapsed, selected, dim, onFold, onSelect, onModel, onMenu, onTip }) {
  const kd = KINDS[node.kind], stc = STATE_VAR[node.state], kdc = KIND_VAR[node.kind];
  const kids = hasKids(id);
  const isFold = collapsed.has(id);
  const factsVisible = (lod==='full' || lod==='ultra');
  /* B1 修复：full 档卡面高度＝标题 22 + 状态 18 + 事实 20 + 脚部 28 + 内边距，
     都按内容盒算出，LOD 切档时行高不硬裁——字不再被遮 */
  const style = { left: pos.x+'px', top: pos.y+'px', '--stc': `var(${stc})`, '--kdc': `var(${kdc})`,
    '--iz': String(Math.min(4, Math.max(1, 100 / Math.max(25, zoom)))) };
  const openMenu = e => { e.stopPropagation(); onMenu(id, e.clientX, e.clientY); };
  return html`<div class=${'node'+(selected?' sel':'')} data-id=${id} style=${style}
      data-state=${node.state} data-lod=${lod}
      onMouseEnter=${e => { if (onTip) onTip(id, e.clientX, e.clientY); }}
      onMouseMove=${e => { if (onTip) onTip(id, e.clientX, e.clientY); }}
      onMouseLeave=${() => { if (onTip) onTip(null); }}>
    <div class="n-body">
      <div class="n-top">
        ${kids ? html`<button class=${'n-caret'+(isFold?' closed':'')} title=${isFold?'展开该支':'折叠该支'}
          onClick=${e => { e.stopPropagation(); onFold(id); }}><${Icon} n="chevD" size=${11} /></button>`
        : html`<span class="n-caret ghost" />`}
        <span class="n-type" title=${kd.n}><${Icon} n=${kd.ic} size=${11} />${kd.s}</span>
        ${isFold && kids ? html`<span class="n-reason" style=${{marginLeft:'auto'}}>
          <span class="rc mono">+${kids}</span></span>` : null}
      </div>
      <div class="n-title" title=${node.title}>${node.title}<span class="code">${node.code}</span></div>
      <div class="n-state"><span class="sd"></span>${STATE_LABEL[node.state]}
        ${node.reason ? html`<span class="n-reason" style=${{marginLeft:'6px'}}><span class="rc">${node.reason.code}</span></span>` : null}
      </div>
      ${node.reason && factsVisible ? html`<div class="n-why">${node.reason.text}</div>` : null}
      ${factsVisible ? html`<div class="n-facts">${node.facts.map(f => html`<span class="f">${f[0]}<b>${f[1]}</b></span>`)}</div>` : null}
      ${node.live && factsVisible ? html`<div class="n-live">${node.live}</div>` : null}
      <div class="n-foot">
        <span class="n-model" title=${'这张卡用的模型：' + modelName(node.model) + '（改模型见卡面菜单）'}>${modelName(node.model)}</span>
        <div class="n-acts">
          <button class="n-act" title="编辑卡面（呼出卡面菜单）" aria-label="编辑卡面"
            onClick=${openMenu}><${Icon} n="edit" size=${13} /></button>
          <button class="n-act" title="打开对话" aria-label="打开对话"
            onClick=${e => { e.stopPropagation(); onMenu(id, e.clientX, e.clientY, 'chat'); }}><${Icon} n="chat" size=${13} /></button>
        </div>
      </div>
    </div>
  </div>`;
}

/* ── 左栏 ── */
function Rail({ cur, collapsed, filter, onGo, onFold, onFilter, theme, onTheme }) {
  const rows = [];
  (function walk(id, pad) {
    const n = NODES[id], kids = hasKids(id);
    rows.push(html`<div class=${'tree-row'+(cur===id?' cur':'')} style=${{paddingLeft:(8+pad*14)+'px'}}
        onClick=${() => onGo(id)}>
      <span class="tree-dot" style=${{background:`var(${STATE_VAR[n.state]})`}}></span>
      <span class="tree-t" title=${n.title}>${n.title}</span>
      ${kids ? html`<button class=${'tree-fold'+(collapsed.has(id)?' closed':'')} title=${collapsed.has(id)?'展开':'折叠'}
        onClick=${e => { e.stopPropagation(); onFold(id); }}><${Icon} n="chevD" size=${11} /></button>` : null}
    </div>`);
    if (kids && !collapsed.has(id)) kidsOf(id).forEach(k => walk(k, pad+1));
  })('root', 0);
  return html`<aside class="rail">
    <div class="rail-slim">
      <span class="rail-logo"><${BrandMark} size=${22} /></span>
      <span class="rail-btn on" title="项目树"><${Icon} n="folder" size=${16} /></span>
      <span class="rail-btn" title="过滤"><${Icon} n="rule" size=${16} /></span>
      <span class="rail-btn" title="图例"><${Icon} n="layers" size=${16} /></span>
      <span style=${{flex:1}} />
      <button class="rail-btn" title=${theme==='dark'?'切到浅色主题':'切到深色主题'} onClick=${onTheme}>
        <${Icon} n=${theme==='dark'?'sun':'moon'} size=${16} /></button>
    </div>
    <div class="rail-full">
      <div class="rail-hd"><span class="wm"><${BrandWordmark} /></span>
        <button class="rail-btn" style=${{marginLeft:'auto'}} title=${theme==='dark'?'切到浅色主题':'切到深色主题'}
          onClick=${onTheme}><${Icon} n=${theme==='dark'?'sun':'moon'} size=${16} /></button>
      </div>
      <div class="rail-sec" style=${{flex:1}}>
        <div class="rail-cap">项目树</div>
        ${rows}
      </div>
      <div class="rail-sec">
        <div class="rail-cap">过滤</div>
        <div class="rail-filter">
          <label class="ck-row"><input type="checkbox" checked=${filter.running} onChange=${e => onFilter('running', e.target.checked)} />仅运行中</label>
          <label class="ck-row"><input type="checkbox" checked=${filter.blocked} onChange=${e => onFilter('blocked', e.target.checked)} />仅受阻</label>
          <label class="ck-row"><input type="checkbox" checked=${filter.archived} onChange=${e => onFilter('archived', e.target.checked)} />含已归档</label>
        </div>
      </div>
      <div class="rail-sec" style=${{borderBottom:0}}>
        <div class="rail-cap">图例</div>
        <div class="legend">
          <div class="lg-row"><span class="lg-swatch" style=${'border-top-color:var(--line-strong)'}></span><span class="lg-n">父子线 ×${legendCount(collapsed).p}</span></div>
          <div class="lg-row"><span class="lg-swatch" style=${'border-top-color:var(--st-done);border-top-style:dotted'}></span><span class="lg-n">通信线 ×${legendCount(collapsed).c}</span></div>
          <div class="lg-row"><span class="lg-swatch" style=${'border-top-color:var(--kd-pm);border-top-style:dashed'}></span><span class="lg-n">依赖关联 ×${legendCount(collapsed).d}</span></div>
        </div>
      </div>
    </div>
  </aside>`;
}
function legendCount(collapsed) {
  const vis = new Set(visibleIds(collapsed || new Set()));
  return { p: Object.keys(NODES).filter(k => NODES[k].parent && vis.has(k) && vis.has(NODES[k].parent)).length,
           c: COMMS.filter(m => vis.has(m.a) && vis.has(m.b)).length,
           d: DEPS.filter(m => vis.has(m.a) && vis.has(m.b)).length };
}

/* ── 顶栏 ── */
function Topbar({ theme, onTheme, zoom, onZoom, onFit, lod, onSearch, onCmdk, onMore, onDesign, evCount, archCount, rpanel, onRp }) {
  /* 统计从数据派生（原为硬编码 15/6/1，改状态后不会更新） */
  const all = Object.keys(NODES).map(function (k) { return NODES[k]; });
  const runCount = all.filter(function (n) { return n.state === 'run'; }).length;
  const blockedCount = all.filter(function (n) { return n.state === 'blocked'; }).length;
  return html`<header class="topbar">
    <div class="tb-brand"><${BrandWordmark} /></div>
    <div class="tb-sep" />
    <div class="crumb"><span class="code">card_001</span><span class="path">底层重构 / 全部子卡</span></div>
    <div class="pstat" id="projectStat"><span><b>${Object.keys(NODES).length}</b> 卡</span><span><b>${runCount}</b> 运行中</span><span class="blk"><b>${blockedCount}</b> 受阻</span></div>
    <div class="tb-spring" />
    <div class="zoomctl">
      <button class="tb-btn" title="缩小" aria-label="缩小" onClick=${() => onZoom(-10)}><${Icon} n="minus" size=${15} /></button>
      <span class="zoom-pct" id="zoomPct">${zoom}%</span>
      <button class="tb-btn" title="放大" aria-label="放大" onClick=${() => onZoom(10)}><${Icon} n="plus" size=${15} /></button>
      <button class="tb-btn" title="适配画布" aria-label="适配画布" onClick=${onFit}><${Icon} n="fit" size=${15} /></button>
      <span class="lod-tag" title="LOD 档位">${lod}</span>
    </div>
    <div class="tb-sep" />
    <button class="tb-btn" title="搜索（点击或 Ctrl+K）" aria-label="搜索" onClick=${onSearch}><${Icon} n="search" size=${16} /></button>
    <button class="tb-btn" title=${theme==='dark'?'切到浅色主题':'切到深色主题'} aria-label="切换主题" onClick=${onTheme}>
      <${Icon} n=${theme==='dark'?'sun':'moon'} size=${16} /></button>
    <button class=${'tb-btn'+(rpanel?' on':'')} title="规则卡面板" aria-label="规则卡面板" onClick=${onRp}><${Icon} n="rule" size=${16} /></button>
    <button class="tb-btn" title="更多" aria-label="更多" onClick=${onMore}><${IconDots} size=${16} /></button>
    <button class="tb-btn" title="设计说明（本卡验收面）" aria-label="设计说明" onClick=${onDesign}><${Icon} n="layers" size=${16} /><span style=${{fontSize:'12px'}}>设计说明</span></button>
  </header>`;
}

/* ── 右缘书签组 ── */
function EdgeGroup({ open, onEv, onChat, onRules, onArch, evCount, archCount }) {
  return html`<div class="edge">
    <div class="re-group">
      <button class=${'re-seg'+(open==='ev'?' on':'')} title="事件" onClick=${onEv}>
        <${Icon} n="clock" size=${14} /><span>事件</span><span class="re-n">${evCount}</span></button>
      <button class=${'re-seg'+(open==='chat'?' on':'')} title="对话" onClick=${onChat}>
        <${Icon} n="chat" size=${14} /><span>对话</span></button>
    </div>
    <div class="re-group">
      <button class=${'re-seg'+(open==='rules'?' on':'')} title="规则卡" onClick=${onRules}>
        <${Icon} n="rule" size=${14} /><span>规则卡</span><span class="re-n">${RULES.length}</span></button>
      <button class=${'re-seg'+(open==='arch'?' on':'')} title="归档" onClick=${onArch}>
        <${Icon} n="archive" size=${14} /><span>归档</span><span class="re-n">${archCount}</span></button>
    </div>
  </div>`;
}

/* ── 浮窗 ── */
function Sheet({ title, icon, sub, onClose, children, class:cls, style }) {
  return html`<div class=${'sheet '+(cls||'')} style=${style} onClick=${e => e.stopPropagation()}>
    <div class="sheet-hd">
      <span class="ic"><${Icon} n=${icon} size=${14} /></span>
      <span class="t">${title}</span>${sub ? html`<span class="sub">${sub}</span>` : null}
      <button class="x" title="关闭" aria-label="关闭" onClick=${onClose}><${Icon} n="close" size=${13} /></button>
    </div>
    <div class="sheet-bd">${children}</div>
  </div>`;
}

function EventSheet({ onClose }) {
  const [list, setList] = useState(EVENTS);
  return html`<${Sheet} title="事件" icon="clock" sub=${'共 '+list.length+' 条'} onClose=${onClose} class="ev-sheet">
    <div class="ev-list">
      ${list.length===0 ? html`<div class="ev-empty">还没有事件。有变化时会出现在这里。</div>`
      : list.map(e => html`<div class="ev-row">
        <span class="ev-k">${EVK[e.k]||e.k}</span>
        <div class="ev-bd"><div class="ev-m">${e.m}</div>
          <div class="ev-f"><span class="c">${e.who}</span><span>${e.time}</span></div></div>
      </div>`)}
    </div>
  <//>`;
}

function ChatSheet({ id, onClose }) {
  const chat = CHATS[id] || { title: NODES[id] ? NODES[id].title : '对话', code: NODES[id]?.code || '', msgs: [] };
  const [msgs, setMsgs] = useState(chat.msgs);
  const [val, setVal] = useState('');
  const send = () => {
    const t = val.trim(); if (!t) return;
    setMsgs(m => [...m, { who:'card_257', me:true, text:t }]);
    setVal('');
  };
  return html`<${Sheet} title=${chat.title} icon="chat" sub=${chat.code} onClose=${onClose} class="chat-sheet">
    <div class="chat-bd">
      ${msgs.length===0 ? html`<div class="ev-empty">还没有消息。发送第一条。</div>`
      : msgs.map((m,i) => html`<div class=${'msg '+(m.me?'me':'them')}>
        <div class="who">${m.who}</div>${m.text}</div>`)}
    </div>
    <div class="chat-ft">
      <input class="chat-in" placeholder="输入消息，Enter 发送" value=${val}
        onInput=${e => setVal(e.target.value)}
        onKeyDown=${e => { if (e.key==='Enter') send(); }} />
      <button class="chat-send" onClick=${send}>发送</button>
    </div>
  <//>`;
}

function RulesSheet({ onClose }) {
  return html`<${Sheet} title="规则卡" icon="rule" sub=${RULES.length+' 条'} onClose=${onClose} class="rules-sheet">
    <div class="rules-list">
      ${RULES.map(r => html`<div class="rule-it"><span class="rid">${r.id}</span>
        <div class="rt">${r.t}</div><div class="rd">${r.d}</div></div>`)}
    </div>
  <//>`;
}

function ArchSheet({ onClose }) {
  return html`<${Sheet} title="归档记录" icon="archive" sub=${ARCHIVED.length+' 条'} onClose=${onClose} class="arch-sheet">
    <div class="arch-note">归档卡不占画布槽位；记录可回档，回档后重新出现在父卡之下。</div>
    <div class="arch-list">
      ${ARCHIVED.map(a => html`<div class="arch-it">
        <${Icon} n="archive" size=${14} />
        <div class="arch-bd"><div class="arch-t">${a.title}<span class="c">${a.code}</span></div>
          <div class="arch-w">${a.at} · ${a.reason} · ${a.why}</div></div>
        <button class="arch-restore">回档</button>
      </div>`)}
    </div>
  <//>`;
}

/* 四态面板（B 系硬门槛：加载＝骨架；空＝确知 0） */
function FourStateSheet({ onClose }) {
  const [tab, setTab] = useState('normal');
  const FT = [
    { k:'normal', t:'正常',   ic:'states', v:'--st-run' },
    { k:'loading',t:'加载中', ic:'clock',  v:'--ft-load' },
    { k:'empty',  t:'空',     ic:'inbox',  v:'--ft-empty' },
    { k:'error',  t:'错误',   ic:'alert',  v:'--ft-error' },
    { k:'denied', t:'无权限', ic:'lock',   v:'--ft-denied' },
  ];
  const cur = FT.find(f => f.k===tab);
  return html`<${Sheet} title="四态面板" icon="states" sub="同一容器 · 四态互异" onClose=${onClose} class="fs-sheet">
    <div class="fs-tabs">
      ${FT.map(f => html`<button class=${'fs-tab'+(tab===f.k?' on':'')} style=${{'--ftc':`var(${f.v})`}}
        onClick=${() => setTab(f.k)}><${Icon} n=${f.ic} size=${12} />${f.t}</button>`)}
    </div>
    <div class="fs-body">
      ${tab==='normal' ? html`<div class="fs-pane">
        <div class="fs-msg"><b>卡片树已取回：15 张卡 / 3 层。</b>这是有真实内容的样子——右侧四态都是它的异常分支。</div>
      </div>` : null}
      ${tab==='loading' ? html`<div class="fs-pane">
        <div class="fs-msg" style=${{marginBottom:'8px'}}><b>正在取卡片树……</b>加载是骨架，不是空白——形状先站出来，内容到了再替换。</div>
        ${[0,1,2].map(i => html`<div class="sk-row">
          <span class="sk sk-dot"></span>
          <span style=${{flex:1}}><span class="sk sk-l1" style=${{display:'block'}}></span><span class="sk sk-l2" style=${{display:'block'}}></span></span>
        </div>`)}
      </div>` : null}
      ${tab==='empty' ? html`<div class="fs-pane">
        <div class="fs-empty">
          <${Icon} n="inbox" size=${30} />
          <div class="t">这里确认为空</div>
          <div class="d">接口已返回：此父卡下没有任何子卡。不是还在加载。</div>
        </div>
      </div>` : null}
      ${tab==='error' ? html`<div class="fs-pane">
        <div class="fs-error">
          <${Icon} n="alert" size=${30} />
          <div class="t">取卡片树失败</div>
          <div class="d">网络中断，已保留上一次的画面；重试前内容仍是旧数据。</div>
          <span class="code">ERR-CANVAS-SNAPSHOT-TIMEOUT</span>
          <div style=${{marginTop:'10px'}}><button class="chat-send" style=${{background:'var(--ft-error)'}}>重试</button></div>
        </div>
      </div>` : null}
      ${tab==='denied' ? html`<div class="fs-pane">
        <div class="fs-denied">
          <${Icon} n="lock" size=${30} />
          <div class="t">无权限查看此分支</div>
          <div class="d">此分支被父卡设为「对下级隐藏」；你能看到它存在，但看不到内容。</div>
        </div>
      </div>` : null}
    </div>
  <//>`;
}

/* ── 底部全局模型条 ── */
function ModelBar({ gm, onKind }) {
  return html`<footer class="modelbar">
    <span class="mb-lead">全局模型</span>
    ${KORDER.map(kd => {
      const c = Object.keys(NODES).filter(k => NODES[k].kind===kd).length;
      return html`<button class=${'mb-cell'+(c?'':' zero')} title=${KINDS[kd].n+' · 点选改这一类的全部 '+c+' 张卡'}
        style=${{'--kdc':`var(${KIND_VAR[kd]})`}} onClick=${e => onKind(kd, e)}>
        <span class="mb-krow"><${Icon} n=${KINDS[kd].ic} size=${12} />
          <span class="mb-kn">${KINDS[kd].s}</span><span class="mb-kc">${c||'—'}</span></span>
        <span class="mb-md" title=${modelName(gm[kd])}>${modelName(gm[kd])}</span>
      </button>`;
    })}
  </footer>`;
}

/* ── 模型选择浮层 ── */
function ModelPop({ pick, gm, nodes, onPickModel, onClose }) {
  if (!pick) return null;
  const cur = pick.t==='node' ? (nodes[pick.id]?.model || gm[nodes[pick.id]?.kind]) : gm[pick.kind];
  const hd = pick.t==='node' ? (nodes[pick.id].code+' · 只改这一张') : (KINDS[pick.kind].n+' · 该类全部');
  const r = pick.anchor;
  const left = Math.min(r.x, window.innerWidth-232), top = Math.max(8, r.y-218);
  return html`<div class="mpop open" style=${{left, top}}>
    <div class="mpop-hd">${hd}</div>
    ${MODELS.map(m => html`<div class=${'mpop-it'+(m===cur?' on':'')} onClick=${() => onPickModel(m)}>
      ${m}<span class="ck">${m===cur ? '✓' : ''}</span></div>`)}
  </div>`;
}

/* ── 命令面板 ── */
function Cmdk({ open, onClose, onGo }) {
  const [q, setQ] = useState('');
  const [idx, setIdx] = useState(0);
  useEffect(() => { if (open) { setQ(''); setIdx(0); } }, [open]);
  const hits = useMemo(() => {
    const all = Object.keys(NODES).map(k => ({ id:k, t:NODES[k].title, c:NODES[k].code }));
    return all.filter(n => !q || n.t.toLowerCase().includes(q.toLowerCase()) || n.c.toLowerCase().includes(q.toLowerCase())).slice(0,8);
  }, [q]);
  if (!open) return null;
  const sel = i => { onGo(hits[i]?.id); onClose(); };
  return html`<div class="cmdk-wrap open" onClick=${onClose}>
    <div class="cmdk" onClick=${e => e.stopPropagation()}>
      <div class="cmdk-in"><${Icon} n="search" size=${15} />
        <input autoFocus placeholder="搜索卡片（标题 / 编号）…" value=${q} onInput=${e => { setQ(e.target.value); setIdx(0); }}
          onKeyDown=${e => {
            if (e.key==='ArrowDown') { e.preventDefault(); setIdx(i => Math.min(hits.length-1, i+1)); }
            else if (e.key==='ArrowUp') { e.preventDefault(); setIdx(i => Math.max(0, i-1)); }
            else if (e.key==='Enter') sel(idx);
            else if (e.key==='Escape') onClose();
          }} />
      </div>
      <div class="cmdk-list">
        ${hits.length===0 ? html`<div class="ev-empty">没有匹配的卡。</div>`
        : hits.map((h,i) => html`<div class=${'cmdk-it'+(i===idx?' on':'')} onClick=${() => sel(i)}
            onMouseEnter=${() => setIdx(i)}>
          <${Icon} n=${KINDS[NODES[h.id].kind].ic} size=${14} />
          <span class="t">${h.t}</span><span class="k">${h.c}</span></div>`)}
      </div>
      <div class="cmdk-ft"><span><b>↑↓</b> 选择</span><span><b>Enter</b> 定位</span><span><b>Esc</b> 关闭</span></div>
    </div>
  </div>`;
}

/* ── 更多菜单 ── */
function MoreMenu({ open, at, onClose, onStates, onTheme, theme }) {
  if (!open) return null;
  return html`<div class="menu open" style=${{left: at.x, top: at.y}} onClick=${e => e.stopPropagation()}>
    <div class="mm-cap">演示</div>
    <button class="mm-it" onClick=${onStates}><${Icon} n="states" size=${14} />四态面板</button>
    <div class="mm-sep" />
    <div class="mm-cap">视图</div>
    <button class="mm-it" onClick=${onTheme}><${Icon} n=${theme==='dark'?'moon':'sun'} size=${14} />${theme==='dark'?'切到浅色主题':'切到深色主题'}</button>
    <div class="mm-sep" />
    <button class="mm-it" onClick=${onClose}><${Icon} n="close" size=${14} />关闭菜单</button>
  </div>`;
}

/* ── 卡面菜单（B2：编辑卡面，不是改名） ── */
function NodeMenu({ open, at, onClose, onEditFace, onChat, onModel, onFold, id }) {
  if (!open) return null;
  const n = NODES[id];
  return html`<div class="node-menu open" style=${{left: at.x, top: at.y}} onClick=${e => e.stopPropagation()}>
    <div class="mm-cap">${n ? n.code+' · 卡面操作' : ''}</div>
    <button class="nm-row" onClick=${onEditFace}><${Icon} n="edit" size=${14} />编辑卡面<span class="k">E</span></button>
    <button class="nm-row" onClick=${onModel}><${Icon} n="gear" size=${14} />换模型<span class="k">M</span></button>
    <button class="nm-row" onClick=${onChat}><${Icon} n="chat" size=${14} />打开对话<span class="k">C</span></button>
    <div class="nm-sep" />
    <button class="nm-row" onClick=${onFold}><${Icon} n="chevD" size=${14} />${'折叠/展开该支'}</button>
  </div>`;
}

/* ── tooltip ── */
function Tip({ tip }) {
  if (!tip) return null;
  const n = NODES[tip.id]; if (!n) return null;
  const x = Math.min(tip.x+14, window.innerWidth-336), y = Math.min(tip.y+14, window.innerHeight-120);
  return html`<div class="tip show" style=${{left:x+'px', top:y+'px'}}>
    <div class="tip-hd"><b>${n.title}</b><span class="c">${n.code}</span></div>
    <div class="tip-msg">${n.last ? n.last.text : ''}</div>
  </div>`;
}
