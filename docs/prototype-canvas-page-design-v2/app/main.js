/* ═══════════════════════════════════════════════════════════════
   main.js — App 根组件 + 画布（状态收敛、连线派生、流星时钟）
   设计说明面板的数据与组件在 design.js；本文件只引用 DesignPanel。
   ═══════════════════════════════════════════════════════════════ */

function App() {
  const [theme, setTheme] = useState('dark');
  const [zoom, setZoom] = useState(100);
  const [tx, setTx] = useState(0);
  const [ty, setTy] = useState(0);
  const [collapsed, setCollapsed] = useState(function () { return new Set(); });
  const [selected, setSelected] = useState(null);
  const [sheet, setSheet] = useState(null);
  const [chatId, setChatId] = useState(null);
  const [cmdk, setCmdk] = useState(false);
  const [menu, setMenu] = useState(null);
  const [nodeMenu, setNodeMenu] = useState(null);
  const [mpop, setMpop] = useState(null);
  const [tip, setTip] = useState(null);
  const [design, setDesign] = useState(false);
  const [rpanel, setRpanel] = useState(false);
  const [rm, setRm] = useState(false);
  const [commOn, setCommOn] = useState(true);
  const [gm, setGm] = useState(GMODEL);
  const [nodes, setNodes] = useState(NODES);
  const [events, setEvents] = useState(EVENTS);
  const [filter, setFilter] = useState({ running: false, blocked: false, archived: false });

  /* 交互稿（v3 定版）本来就有整套画布手势：拖拽平移、滚轮平移、Ctrl/⌘+滚轮以光标为锚缩放、
     两指触摸平移。我上一版把它们丢了，画布退化成一张图。这里按 v3 原文逐条移植。 */
  const stageRef = useRef(null);
  const layerRef = useRef(null);
  const txRef = useRef(0), tyRef = useRef(0), zoomRef = useRef(100);
  useEffect(function () { txRef.current = tx; }, [tx]);
  useEffect(function () { tyRef.current = ty; }, [ty]);
  useEffect(function () { zoomRef.current = zoom; }, [zoom]);

  const lod = LOD(zoom);
  const pos = useMemo(function () { return computeLayout(collapsed, lod); }, [collapsed, lod]);
  const visible = useMemo(function () { return new Set(visibleIds(collapsed)); }, [collapsed]);

  /* 流星时钟：有流星在飞才逐帧重渲染 */
  const [now, setNow] = useState(function () {
    COMMS.forEach(function (m) { m.born = performance.now() - m.ago * 1000; });
    return performance.now();
  });
  const raf = useRef(null);
  useEffect(function () {
    if (!commOn) return;
    const alive = function () {
      return COMMS.some(function (m) {
        const a = (performance.now() - m.born) / 1000;
        return a >= 0 && a < PULSE_LIFE;
      });
    };
    const step = function () {
      setNow(performance.now());
      if (alive()) raf.current = requestAnimationFrame(step); else raf.current = null;
    };
    if (alive() && raf.current == null) raf.current = requestAnimationFrame(step);
    return function () { if (raf.current) cancelAnimationFrame(raf.current); raf.current = null; };
  }, [commOn, now]);

  useEffect(function () { document.documentElement.dataset.theme = theme; }, [theme]);
  useEffect(function () { document.body.classList.toggle('rm', rm); }, [rm]);

  /* 全局键盘：Esc 关浮层（Esc 是加速键，每层自身还有鼠标出口） */
  useEffect(function () {
    const onKey = function (e) {
      if (e.key === 'Escape') {
        if (mpop) setMpop(null);
        else if (nodeMenu) setNodeMenu(null);
        else if (menu) setMenu(null);
        else if (cmdk) setCmdk(false);
        else if (design) setDesign(false);
        else if (sheet) setSheet(null);
        else if (selected) setSelected(null);
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setCmdk(function (v) { return !v; });
      }
    };
    window.addEventListener('keydown', onKey);
    return function () { window.removeEventListener('keydown', onKey); };
  }, [mpop, nodeMenu, menu, cmdk, design, sheet, selected]);

  const setZoomAt = function (z) { setZoom(Math.max(25, Math.min(200, z))); };
  /* 以光标为锚的缩放（v3 setZoom(z,cx,cy) 同式）：t1 = t0 - (p - t0)*(z1/z0 - 1) */
  const zoomAt = function (nz, cx, cy) {
    const host = stageRef.current;
    const z1 = Math.max(25, Math.min(200, nz));
    if (!host) { setZoom(z1); return; }
    const r = host.getBoundingClientRect();
    const px = cx - r.left, py = cy - r.top;
    const z0 = zoomRef.current / 100, zz = z1 / 100;
    setTx(function (t) { return t - (px - t) * (zz / z0 - 1); });
    setTy(function (t) { return t - (py - t) * (zz / z0 - 1); });
    setZoom(z1);
  };

  /* 画布手势（照 v3）：左键空白处拖＝平移；滚轮上下/左右＝平移；Shift+滚轮＝横向；
     Ctrl/⌘+滚轮＝以光标为锚缩放；两指触摸＝平移。落点在浮窗/按钮上时不接管。 */
  useEffect(function () {
    const host = stageRef.current;
    if (!host) return;
    let pan = null, touch = null;
    const onDown = function (e) {
      if (e.button !== 0 && e.button !== 1) return;
      if (e.button === 0 && e.target.closest('.node')) return;
      if (e.target.closest('.sheet,.edge,.rp-handle,.menu,.node-menu,.cmdk,.demo-dock,.tip,.mpop,.rpanel')) return;
      pan = { sx: e.clientX, sy: e.clientY, tx: txRef.current, ty: tyRef.current };
      host.classList.add('panning');
    };
    const onMove = function (e) {
      if (!pan) return;
      setTx(pan.tx + (e.clientX - pan.sx));
      setTy(pan.ty + (e.clientY - pan.sy));
    };
    const onUp = function () { if (pan) { pan = null; host.classList.remove('panning'); } };
    const onWheel = function (e) {
      e.preventDefault();
      const k = e.deltaMode === 1 ? 16 : (e.deltaMode === 2 ? 400 : 1);
      const dx = e.deltaX * k, dy = e.deltaY * k;
      if (e.ctrlKey || e.metaKey) { zoomAt(zoomRef.current * (dy < 0 ? 1.1 : 0.9), e.clientX, e.clientY); return; }
      if (e.shiftKey && !dx) { setTx(function (t) { return t - dy; }); return; }
      setTx(function (t) { return t - dx; });
      setTy(function (t) { return t - dy; });
    };
    const mid = function (t) { return { x: (t[0].clientX + t[1].clientX) / 2, y: (t[0].clientY + t[1].clientY) / 2 }; };
    const onTStart = function (e) {
      if (e.touches.length === 2) { const m = mid(e.touches); touch = { mx: m.x, my: m.y, tx: txRef.current, ty: tyRef.current }; }
      else touch = null;
    };
    const onTMove = function (e) {
      if (!touch || e.touches.length !== 2) return;
      e.preventDefault();
      const m = mid(e.touches);
      setTx(touch.tx + (m.x - touch.mx)); setTy(touch.ty + (m.y - touch.my));
    };
    const onTEnd = function (e) { if (e.touches && e.touches.length >= 2) return; touch = null; };
    host.addEventListener('mousedown', onDown);
    host.addEventListener('wheel', onWheel, { passive: false });
    host.addEventListener('touchstart', onTStart, { passive: true });
    host.addEventListener('touchmove', onTMove, { passive: false });
    host.addEventListener('touchend', onTEnd, { passive: true });
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return function () {
      host.removeEventListener('mousedown', onDown);
      host.removeEventListener('wheel', onWheel);
      host.removeEventListener('touchstart', onTStart);
      host.removeEventListener('touchmove', onTMove);
      host.removeEventListener('touchend', onTEnd);
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, []);

  /* 适配：包围盒按「目标缩放档位的卡高」算，迭代收敛。
     原实现用当前 lod（首帧 zoom=100→ultra 卡高 216）算包围盒，得出 26% 后档位掉到 far（卡高 112），
     于是图更小、四周大片空白——包围盒与实渲染不同源（自相依赖）。改成迭代到档位不再变化。 */
  const fit = function () {
    const host = document.getElementById('world-host');
    if (!host) return;
    const r = host.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const ids = visibleIds(collapsed);
    const pad = 56;
    let z = zoom, useLod = lod, usePos = pos;
    for (let i = 0; i < 4; i++) {
      /* 关键：行距也随档位变（rowh=cadH+gap），必须用候选档位重算布局，
         否则坐标仍是旧档行距排出来的，包围盒与实际渲染不同源。 */
      const p2 = computeLayout(collapsed, useLod);
      const h0 = cardH(useLod);
      let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
      ids.forEach(function (id) {
        const p = p2[id];
        if (!p) return;
        x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y);
        x1 = Math.max(x1, p.x + NW); y1 = Math.max(y1, p.y + h0);
      });
      const w = Math.max(1, x1 - x0), hh = Math.max(1, y1 - y0);
      const nz = Math.max(25, Math.min(200, Math.min((r.width - pad * 2) / w, (r.height - pad * 2) / hh) * 100));
      const nl = LOD(nz);
      usePos = p2;
      if (Math.round(nz) === Math.round(z) && nl === useLod) { z = nz; break; }
      z = nz; useLod = nl;
    }
    const rh = cardH(useLod);
    let ax = 1e9, ay = 1e9, bx = -1e9, by = -1e9;
    ids.forEach(function (id) {
      const p = usePos[id];
      if (!p) return;
      ax = Math.min(ax, p.x); ay = Math.min(ay, p.y);
      bx = Math.max(bx, p.x + NW); by = Math.max(by, p.y + rh);
    });
    const ww = Math.max(1, bx - ax), hh2 = Math.max(1, by - ay);
    setZoom(Math.round(z));
    setTx((r.width - ww * z / 100) / 2 - ax * z / 100);
    setTy((r.height - hh2 * z / 100) / 2 - ay * z / 100);
  };
  useEffect(function () { fit(); }, []);

  const onFold = function (id) {
    setCollapsed(function (s) {
      const n = new Set(s);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
  };
  const openChat = function (id) { setSheet('chat'); setChatId(id); };

  /* 节点手势（照 v3）：单击卡体＝折叠/展开该支；双击卡体＝开合该卡对话。
     单击要等过双击判定窗（200ms）再执行——否则双击前会先折叠一次再展开，画面抖一下。 */
  useEffect(function () {
    const layer = layerRef.current;
    if (!layer) return;
    let timer = null;
    const pick = function (e) {
      if (e.target.closest('button,input,[contenteditable=true]')) return null;
      const el = e.target.closest('.node');
      return el ? el.dataset.id : null;
    };
    const onClick = function (e) {
      const id = pick(e); if (!id) return;
      if (e.detail > 1) { if (timer) { clearTimeout(timer); timer = null; } return; }
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () { timer = null; onFold(id); }, 200);
    };
    const onDbl = function (e) {
      const id = pick(e); if (!id) return;
      if (timer) { clearTimeout(timer); timer = null; }
      if (sheet === 'chat' && chatId === id) setSheet(null); else openChat(id);
    };
    layer.addEventListener('click', onClick);
    layer.addEventListener('dblclick', onDbl);
    return function () {
      layer.removeEventListener('click', onClick);
      layer.removeEventListener('dblclick', onDbl);
      if (timer) clearTimeout(timer);
    };
  }, [sheet, chatId, collapsed]);

  const onModelPick = function (m) {
    if (!mpop) return;
    if (mpop.t === 'node') {
      setNodes(function (ns) {
        const o = Object.assign({}, ns);
        o[mpop.id] = Object.assign({}, ns[mpop.id], { model: m });
        return o;
      });
    } else {
      setGm(function (g) { const o = Object.assign({}, g); o[mpop.kind] = m; return o; });
      setNodes(function (ns) {
        const o = Object.assign({}, ns);
        Object.keys(o).forEach(function (k) {
          if (o[k].kind === mpop.kind) o[k] = Object.assign({}, o[k], { model: m });
        });
        return o;
      });
    }
    setMpop(null);
  };

  /* 连线：父子线 + 依赖线全部从 pos 派生（隐藏卡不产生线段） */
  const wireEls = useMemo(function () {
    const els = [];
    visible.forEach(function (id) {
      const n = nodes[id];
      if (n && n.parent && visible.has(n.parent)) {
        els.push(html`<path class="wire-p" key=${'w' + id} d=${pathP(anchorR(n.parent, pos, lod), anchorL(id, pos, lod))} />`);
      }
    });
    DEPS.forEach(function (d, i) {
      if (!visible.has(d.a) || !visible.has(d.b)) return;
      const a = anchorR(d.a, pos, lod), b = anchorL(d.b, pos, lod);
      const m1 = 'M ' + a.x + ' ' + (a.y - 4) + ' L ' + (a.x + 4) + ' ' + a.y + ' L ' + a.x + ' ' + (a.y + 4) + ' L ' + (a.x - 4) + ' ' + a.y + ' Z';
      const m2 = 'M ' + b.x + ' ' + (b.y - 4) + ' L ' + (b.x + 4) + ' ' + b.y + ' L ' + b.x + ' ' + (b.y + 4) + ' L ' + (b.x - 4) + ' ' + b.y + ' Z';
      els.push(html`<g key=${'d' + i}>
        <path class="dep-line" d=${pathP(a, b)} />
        <path class="dep-mark" d=${m1} />
        <path class="dep-mark" d=${m2} />
      </g>`);
    });
    return els;
  }, [pos, visible, nodes]);

  const commEls = commOn ? commPaths(COMMS, pos, visible, now) : [];

  const worldStyle = 'transform:translate(' + tx + 'px,' + ty + 'px) scale(' + (zoom / 100) + ')';

  const showNodes = visibleIds(collapsed).filter(function (id) {
    if (filter.running && nodes[id].state !== 'run') return false;
    if (filter.blocked && nodes[id].state !== 'blocked') return false;
    return true;
  });

  const setThemeToggle = function () {
    setTheme(function (t) { return t === 'dark' ? 'light' : 'dark'; });
  };

  const nodeEls = showNodes.map(function (id) {
    return html`<${NodeCard} key=${id} id=${id} node=${nodes[id]} pos=${pos[id]}
      zoom=${zoom} lod=${lod} collapsed=${collapsed}
      selected=${selected === id} dim=${selected && selected !== id}
      onFold=${onFold}
      onSelect=${function (nid) { setSelected(function (s) { return s === nid ? null : nid; }); }}
      onModel=${function (t, e) {
        e.stopPropagation();
        const r = e.currentTarget.getBoundingClientRect();
        setMpop({ t: 'node', id: t.id, kind: t.kind, anchor: { x: r.left, y: r.top } });
      }}
      onMenu=${function (id2, x, y, kind) {
        if (kind === 'chat') openChat(id2); else setNodeMenu({ id: id2, x: x, y: y });
      }}
      onTip=${function (id2, x, y) {
        if (!id2) { setTip(null); return; }
        setTip({ id: id2, x: x, y: y });
      }} />`;
  });

  const rpHandle = rpanel ? null : html`<button class="rp-handle" title="规则面板" aria-label="规则面板"
    onClick=${function () { setRpanel(true); }}><${Icon} n="chevL" size=${13} /></button>`;

  const sheetEl = sheet === 'ev' ? html`<${EventSheet} onClose=${function () { setSheet(null); }} />`
    : sheet === 'chat' ? html`<${ChatSheet} id=${chatId || 'protoA'} onClose=${function () { setSheet(null); }} />`
    : sheet === 'rules' ? html`<${RulesSheet} onClose=${function () { setSheet(null); }} />`
    : sheet === 'arch' ? html`<${ArchSheet} onClose=${function () { setSheet(null); }} />`
    : sheet === 'fs' ? html`<${FourStateSheet} onClose=${function () { setSheet(null); }} />`
    : null;

  const rpCards = RULES.map(function (r) {
    return html`<div class="rp-card"><div class="h"><${Icon} n="rule" size=${12} />${r.id} · ${r.t}</div>
      <div class="d">${r.d}</div></div>`;
  });

  return html`<div class="app" onClick=${function () { if (menu) setMenu(null); if (nodeMenu) setNodeMenu(null); }}>
    <${Rail} theme=${theme} onTheme=${setThemeToggle}
      collapsed=${collapsed} onFold=${onFold} cur=${selected}
      onGo=${function (id) { setSelected(id); }}
      filter=${filter}
      onFilter=${function (k, v) { setFilter(function (f) { const o = Object.assign({}, f); o[k] = v; return o; }); }} />
    <div class="main">
      <${Topbar} theme=${theme} onTheme=${setThemeToggle}
        zoom=${zoom} onZoom=${function (d) { setZoomAt(zoom + d); }} onFit=${fit} lod=${lod}
        onSearch=${function () { setCmdk(true); }} onCmdk=${function () { setCmdk(true); }}
        onMore=${function (e) {
          e.stopPropagation();
          const r = e.currentTarget.getBoundingClientRect();
          setMenu(function (m) { return m ? null : { x: r.right - 210, y: r.bottom + 6 }; });
        }}
        onDesign=${function () { setDesign(true); }} evCount=${events.length} archCount=${ARCHIVED.length}
        rpanel=${rpanel} onRp=${function () { setRpanel(function (v) { return !v; }); }} />
      <div class="stage" ref=${stageRef}>
        <div id="world-host">
          <div class="world" style=${worldStyle}>
            <svg class="wires" width=${4000} height=${2400} viewBox="0 0 4000 2400">
              ${wireEls}${commEls}
            </svg>
            <div class="nodes-layer" ref=${layerRef}>${nodeEls}</div>
          </div>
        </div>
        ${rpHandle}
        <${EdgeGroup} open=${sheet}
          onEv=${function () { setSheet(function (s) { return s === 'ev' ? null : 'ev'; }); }}
          onChat=${function () { setSheet(function (s) { return s === 'chat' ? null : 'chat'; }); }}
          onRules=${function () { setSheet(function (s) { return s === 'rules' ? null : 'rules'; }); }}
          onArch=${function () { setSheet(function (s) { return s === 'arch' ? null : 'arch'; }); }}
          evCount=${events.length} archCount=${ARCHIVED.length} />
        ${sheetEl}
      </div>
      <${ModelBar} gm=${gm} onKind=${function (kd, e) {
        const r = e.currentTarget.getBoundingClientRect();
        setMpop({ t: 'kind', kind: kd, anchor: { x: r.left, y: r.top } });
      }} />
    </div>
    <aside class=${'rpanel' + (rpanel ? ' open' : '')}>
      <div class="rp-hd"><span class="t">规则卡面板</span>
        <button class="x" title="收起" onClick=${function () { setRpanel(false); }}><${Icon} n="close" size=${13} /></button></div>
      <div class="rp-body">
        ${rpCards}
        <div class="rp-card"><div class="h"><${Icon} n="states" size=${12} />LOD 档位</div>
          <div class="d">far &lt;58% · near 58–71 · full 72–95 · ultra 96+（当前：${lod} @ ${zoom}%）</div></div>
      </div>
    </aside>
    ${mpop ? html`<${ModelPop} pick=${mpop} gm=${gm} nodes=${nodes} onPickModel=${onModelPick} onClose=${function () { setMpop(null); }} />` : null}
    <${Cmdk} open=${cmdk} onClose=${function () { setCmdk(false); }} onGo=${function (id) { setSelected(id); }} />
    <${MoreMenu} open=${!!menu} at=${menu || { x: 0, y: 0 }} onClose=${function () { setMenu(null); }}
      onStates=${function () { setMenu(null); setSheet('fs'); }}
      onTheme=${setThemeToggle} theme=${theme} />
    <${NodeMenu} open=${!!nodeMenu} at=${nodeMenu || { x: 0, y: 0 }} id=${nodeMenu ? nodeMenu.id : null}
      onClose=${function () { setNodeMenu(null); }}
      onEditFace=${function () { setNodeMenu(null); setDesign(true); }}
      onModel=${function () {
        const id = nodeMenu.id;
        setNodeMenu(null);
        const el = document.querySelector('.node[data-id="' + id + '"] .n-model');
        if (el) { const r = el.getBoundingClientRect(); setMpop({ t: 'node', id: id, anchor: { x: r.left, y: r.top } }); }
      }}
      onChat=${function () { const id = nodeMenu.id; setNodeMenu(null); openChat(id); }}
      onFold=${function () { onFold(nodeMenu.id); setNodeMenu(null); }} />
    <${Tip} tip=${tip} />
    <div class="demo-dock" onClick=${function (e) { e.stopPropagation(); }}>
      <${Icon} n="play" size=${13} /><span class="dd-t">演示</span>
      <button onClick=${function () { setSheet('fs'); }}>四态</button>
      <button onClick=${function () { const a = COMMS[0]; a.born = performance.now(); a.ago = 0; setNow(performance.now()); }}>流星</button>
      <button onClick=${function () { setCommOn(function (v) { return !v; }); }}>${commOn ? '关流星' : '开流星'}</button>
    </div>
    <${DesignPanel} open=${design} onClose=${function () { setDesign(false); }}
      rm=${rm} setRm=${setRm} lod=${lod} zoom=${zoom} theme=${theme} />
  </div>`;
}

/* ── 挂载 ── */
try {
  /* preact 渲染期/effect 期异常显式捕获（file:// 全局 onerror 会被跨源掩蔽） */
  var __oe = preact.options.__e;
  preact.options.__e = function (err, vnode) {
    var el = document.getElementById('boot-err');
    if (el) el.textContent += '[preact] ' + (err && err.message) + '\n' +
      (err && err.stack ? err.stack.split('\n').slice(0, 5).join('\n') : '') + '\n';
    if (__oe) __oe(err, vnode);
  };
  preact.render(html`<${App} />`, document.getElementById('app'));
} catch (e) {
  var el2 = document.getElementById('boot-err');
  if (el2) el2.textContent += '[render] ' + (e && e.message) + '\n' + (e && e.stack ? e.stack.split('\n').slice(0, 6).join('\n') : '');
}
