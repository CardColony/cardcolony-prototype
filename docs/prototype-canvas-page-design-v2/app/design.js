/* ═══════════════════════════════════════════════════════════════
   design.js — 设计说明面板（令牌 / 决策 / 缺陷修复 / 待裁定 / 自检 / 组件）
   数据与面板同文件，App 只引用 DesignPanel 一个名字。
   ═══════════════════════════════════════════════════════════════ */

/* 深浅两套令牌全量字典（与 CSS :root / [data-theme] 同源维护） */
const TOKENS = [
  ['--bg','画布外围底','背景最深一级','次深；面板浮起'],
  ['--bg-canvas','画布底','最深（画布是主角）','次深'],
  ['--surface','面板／条底','面板一级','亮一级'],
  ['--raised','浮层／悬停底','最亮一级','纯白'],
  ['--overlay','遮罩','rgba(6,9,13,.62)','rgba(40,46,42,.40)'],
  ['--text-1','主文字','≥4.5:1','≥4.5:1'],
  ['--text-2','次文字','≥4.5:1','≥4.5:1'],
  ['--text-3','辅助文字','≥4.5:1（小字非正文处）','≥4.5:1'],
  ['--text-dis','禁用文字','弱化','弱化'],
  ['--line','常规边框','非文本对 ≥3:1','≥3:1'],
  ['--line-strong','强边框／父子线','≥3:1','≥3:1'],
  ['--brand','品牌蜂蜜金（只表品牌）','dark #e2a63d','light #c9901f'],
  ['--st-run','运行中','#3fc79a','#188a5e'],
  ['--st-done','已完成','#6f9fd8','#3568a0'],
  ['--st-blocked','受阻','#e07a5f','#c04f2e'],
  ['--st-paused','已暂停','#b0a06b','#8a7a3a'],
  ['--st-queued','排队中','#8a93a0','#5f6a76'],
  ['--st-arch','归档','#8b97a3','#77817a'],
  ['--kd-master','Master 类型色','#c9a0e8','#8a4fc0'],
  ['--kd-manager','Manager 类型色','#7fb8e8','#2c6ea8'],
  ['--kd-unit','Unit 类型色','#7fd0c0','#208a76'],
  ['--kd-worker','worker 类型色','#a3b3c2','#5a6b7c'],
  ['--kd-qa','QA 类型色','#e8a0b8','#c04f7e'],
  ['--kd-pm','PM 类型色／依赖线','#e8c87f','#a07820'],
  ['--kd-ui','UI 类型色','#9fc2ef','#3a6fb0'],
  ['--ft-load','四态·加载','#6f9fd8','#3568a0'],
  ['--ft-empty','四态·空','#8a93a0','#5f6a76'],
  ['--ft-error','四态·错误','#e07a5f','#c04f2e'],
  ['--ft-denied','四态·无权限','#b0a06b','#8a7a3a'],
  ['--sh-1','阴影一级（卡片）','0 1px 2px rgba(0,0,0,.4)','同源'],
  ['--sh-2','阴影二级（面板）','0 2px 8px rgba(0,0,0,.45)','同源'],
  ['--sh-3','阴影三级（浮层／弹窗）','0 8px 28px rgba(0,0,0,.55)','同源'],
  ['--ease','缓动曲线','cubic-bezier(.2,.7,.3,1)','同'],
];

const DECISIONS = [
  ['明度分配：画布最深 → 面板 → 浮层', '画布是主角，框架让位于内容。层级靠明度三级（bg-canvas → surface → raised），不靠重描边。'],
  ['强调色＝品牌金，只出现在两处', '品牌字标与计数徽标。绝不进状态色、按钮、选中态——否则「这是品牌」的信息被稀释，状态色也被污染。'],
  ['状态只占「顶部 2px 发丝线＋色点」', 'v1 曾把运行中/受阻整卡边框染色，15 卡同屏一团彩边。改为发丝线后，视线沿卡顶一扫即知全队状态，卡体保持中性。'],
  ['标题 15px/600 是卡面唯一主角色', '信息层级：标题 > 状态词 > 类型徽标 > 事实行 > 模型。模型名是等宽小字——它是配置不是身份。'],
  ['三类线两维可分', '父子＝灰实线贝塞尔（无端点）；通信＝蓝曲线＋流星拖尾＋收件端实心点；依赖＝紫短划线＋两端菱形。颜色×形态两维，不只靠色。'],
  ['折叠后线从折叠标记出', '子卡消失后父卡仍保留出线语义，不再指向不存在的坐标——v1「线乱」的根治。'],
  ['位置即状态（React 受控）', '布局是纯函数 computeLayout(collapsed)→pos；连线由同一 pos 派生。没有 CSS 位移过渡，卡与线永远同步——不存在「卡在飞、线已到」。'],
  ['LOD 切档只减信息，不改尺寸', 'far<58%：标题＋状态点；near：＋类型；full 72–95%：＋事实行＋脚部；ultra≥96%：＋live 行。卡宽 290 恒定。'],
  ['流星按弧长采样拖尾', '照 v3：头亮、尾渐隐、到站停 1.35s、整体渐隐 0.85s。逐帧算（RAF，无线时自停），不靠 CSS 动画。'],
  ['命中区 ≥24px', '所有可点元素（缩放钮 24、卡面动作 24、树行 24）不小于 24×24；图标视觉尺寸小于命中区，靠 padding 补足。'],
  ['中文界面：字号阶 6 档', '11 辅助 / 11.5 事实 / 12.5 次要 / 13 面板题 / 14 正文 / 15 卡标题(600)。等宽仅用于编号与模型名。'],
  ['reduced-motion 全停，功能不缺', '呼吸/流星/骨架 shimmer 全部 animation:none；骨架保留静态灰块（形状仍在），流星降为静态曲线。'],
];

const DEFECTS = [
  ['B1 · ≥70% 卡面文字被遮', '卡面按内容自适应高度（不再写死高度），LOD 只控制「哪些块渲染」，不裁行高。'],
  ['B2 · 悬停按钮语义错', '卡面按钮＝「编辑卡面」，呼出卡面菜单（编辑卡面 / 换模型 / 打开对话 / 折叠展开）。'],
  ['B3 · 对话输入框看不见', '消息区 min-height:0 ＋ footer flex:none 固定高度——输入框永远在视口内。'],
  ['B4 · 收起后线全乱', '连线与卡位同源于 computeLayout 的同一输出，隐藏卡不产生任何线段。'],
];

const OPENCQ = [
  { q:'① 画布底 vs 面板底：画布应是最深吗？',
    why:'现方案：画布最深（bg-canvas），面板浮起。备选 A：面板更深、画布浅。影响：内容主角地位互换。',
    opts:[['现：画布最深（建议）','内容为主角，框架退后'],['备 A：面板最深','chrome 为主角，编辑器感']] },
  { q:'② 「运行中」用青绿＋呼吸，还是并入强调蓝？',
    why:'现方案：青绿 #3fc79a ＋ 色点呼吸。备选：并入强调蓝（更冷静，但「在动」的直觉弱）。',
    opts:[['现：青绿＋呼吸（建议）','running＝动的，直觉强'],['备 A：并入强调蓝','更克制，少了点「活」感']] },
  { q:'③ 依赖关联线的形态：短划线 vs 点线？',
    why:'现方案：紫短划线＋菱形端点。备选：紫点线（更轻，但与通信线的「点状」语素接近）。',
    opts:[['现：短划线（建议）','与通信虚点区分更稳'],['备 A：点线','更轻，但可能与通信拖尾混']] },
];

/* ── 自检：现算（不写死数字） ── */
function hexLum(s) {
  s = String(s || '').trim();
  var m = s.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (!m) {
    var r2 = s.match(/^rgba?\\(([^)]+)\\)$/i);
    if (!r2) return null;
    var p = r2[1].split(',').map(function (x) { return parseFloat(x); });
    if (p.length < 3) return null;
    if (p.length > 3 && p[3] < 0.95) return null; /* 半透明不与底色比 */
    return lumOf(p[0], p[1], p[2]);
  }
  var h = m[1];
  if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  return lumOf(parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16));
}
function lumOf(r, g, b) {
  var c = [r, g, b].map(function (v) {
    v = v / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}
function ratio(a, b) {
  var la = hexLum(a), lb = hexLum(b);
  if (la == null || lb == null) return null;
  var hi = Math.max(la, lb), lo = Math.min(la, lb);
  return (hi + 0.05) / (lo + 0.05);
}
function readVars(theme) {
  var el = document.createElement('div');
  el.setAttribute('data-theme', theme);
  el.style.cssText = 'position:absolute;left:-9999px;top:0;width:0;height:0;pointer-events:none';
  document.body.appendChild(el);
  var cs = getComputedStyle(el), out = {};
  for (var i = 0; i < TOKENS.length; i++) out[TOKENS[i][0]] = cs.getPropertyValue(TOKENS[i][0]).trim();
  document.body.removeChild(el);
  return out;
}
/* 对比度对：文字 ≥4.5:1；非文本 ≥3:1 */
var PAIRS = [
  ['--text-1', '--bg-canvas', 4.5, '正文/画布底'],
  ['--text-1', '--surface', 4.5, '正文/面板底'],
  ['--text-1', '--raised', 4.5, '正文/浮层底'],
  ['--text-2', '--bg-canvas', 4.5, '次要/画布底'],
  ['--text-2', '--surface', 4.5, '次要/面板底'],
  ['--text-2', '--raised', 4.5, '次要/浮层底'],
  ['--text-3', '--bg-canvas', 4.5, '辅助/画布底'],
  ['--text-3', '--surface', 4.5, '辅助/面板底'],
  ['--line', '--surface', 3, '常规边框/面板'],
  ['--line-strong', '--bg-canvas', 3, '强边框/画布'],
  ['--line-strong', '--surface', 3, '强边框/面板'],
  ['--brand', '--bg-canvas', 3, '品牌金/画布底（非文本）'],
  ['--st-run', '--bg-canvas', 3, '运行中/画布底（非文本）'],
  ['--st-done', '--bg-canvas', 3, '已完成/画布底（非文本）'],
  ['--st-blocked', '--bg-canvas', 3, '受阻/画布底（非文本）'],
  ['--st-paused', '--bg-canvas', 3, '已暂停/画布底（非文本）'],
  ['--kd-pm', '--bg-canvas', 3, '依赖线紫/画布底（非文本）']
];
function runChecks() {
  var dk = readVars('dark'), lt = readVars('light');
  var ct = [], pass = 0, fail = 0, minTxt = 99, minNon = 99;
  PAIRS.forEach(function (p) {
    ['dark', 'light'].forEach(function (th) {
      var v = th === 'dark' ? dk : lt;
      var r = ratio(v[p[0]], v[p[1]]);
      var ok = r != null && r >= p[2];
      if (ok) pass++; else fail++;
      if (r != null) {
        if (p[2] >= 4.5) minTxt = Math.min(minTxt, r);
        else minNon = Math.min(minNon, r);
      }
      ct.push({ th: th === 'dark' ? '深' : '浅', a: p[0], b: p[1], use: p[3], thr: p[2], v: r, ok: ok });
    });
  });
  /* 命中区：只量真实渲染、可点的元素 */
  var els = document.querySelectorAll('button, input, label.ck-row, .tree-row');
  var minHit = 1e9, minHitWhat = '';
  var hitCount = 0;
  for (var i = 0; i < els.length; i++) {
    var el = els[i];
    /* 复选框：实际命中区＝包它的 label（点击整行都生效） */
    if (el.tagName === 'INPUT' && el.closest('label')) el = el.closest('label');
    var rc = el.getBoundingClientRect();
    if (rc.width < 1 || rc.height < 1) continue;
    if (rc.top > 800 || rc.bottom < 0) continue;
    hitCount++;
    var m = Math.min(rc.width, rc.height);
    if (m < minHit) { minHit = m; minHitWhat = (el.getAttribute('aria-label') || el.getAttribute('title') || el.className || el.tagName) + ' ' + rc.width.toFixed(1) + '×' + rc.height.toFixed(1); }
  }
  /* 令牌齐备性：两套都要有值 */
  var missing = [];
  TOKENS.forEach(function (t) {
    if (!dk[t[0]]) missing.push('dark:' + t[0]);
    if (!lt[t[0]]) missing.push('light:' + t[0]);
  });
  /* reduced-motion：动画名（呼吸在 .sd 的 ::after；骨架在 .sk 的 ::after 且只在四态面板里） */
  var rmNames = [];
  [['.sd', '::after'], ['.sk', '::after']].forEach(function (p) {
    var e = document.querySelector(p[0]);
    if (!e) { rmNames.push(p[0] + p[1] + '→(当前未渲染)'); return; }
    var cs = getComputedStyle(e, p[1]);
    rmNames.push(p[0] + p[1] + '→' + (cs.animationName || 'none'));
  });
  return { ct: ct, pass: pass, fail: fail, minTxt: minTxt === 99 ? null : minTxt, minNon: minNon === 99 ? null : minNon,
           hitCount: hitCount, minHit: minHit === 1e9 ? null : minHit, minHitWhat: minHitWhat,
           missing: missing, rmNames: rmNames, dk: dk, lt: lt };
}

/* 设计说明面板（本卡对外验收面） */
function DesignPanel(props) {
  const open = props.open, onClose = props.onClose, rm = props.rm, setRm = props.setRm;
  const lod = props.lod, zoom = props.zoom, theme = props.theme;
  const st = useState('check');
  const tab = st[0], setTab = st[1];
  const cs = useState({});
  const choices = cs[0], setChoices = cs[1];
  if (!open) return null;

  const TABS = [['token','令牌'],['decision','决策'],['defect','缺陷修复'],['open','待裁定'],['check','自检'],['comp','组件']];

  const tabBtn = TABS.map(function (p) {
    const k = p[0], t = p[1];
    return html`<button class=${'ds-tab' + (tab === k ? ' on' : '')} onClick=${function () { setTab(k); }}>${t}</button>`;
  });

  const tokenRows = TOKENS.map(function (t) {
    return html`<tr><td class="mono">${t[0]}</td><td>${t[1]}</td><td>${t[2]}</td><td>${t[3]}</td></tr>`;
  });

  const decisionRows = DECISIONS.map(function (d, i) {
    return html`<div class="ds-li"><b>${i + 1}. ${d[0]}</b> — ${d[1]}</div>`;
  });

  const defectRows = DEFECTS.map(function (d) {
    return html`<div class="ds-li"><span class="ds-fix">●</span> <b>${d[0]}</b><br/>${d[1]} <span class="ds-ok">［已修］</span></div>`;
  });

  const choiceRows = OPENCQ.map(function (c, ci) {
    const optRows = c.opts.map(function (o, oi) {
      const sel = choices[ci] === oi;
      return html`<label class=${'ds-opt' + (sel ? ' sel' : '')}>
        <input type="radio" name=${'cq' + ci} checked=${sel}
          onChange=${function () { setChoices(function (ch) { const n = Object.assign({}, ch); n[ci] = oi; return n; }); }} />
        <span><span class="t">${o[0]}</span><span class="d">${o[1]}</span></span></label>`;
    });
    return html`<div class="ds-choice"><div class="q">${c.q}</div><div class="why">${c.why}</div>${optRows}</div>`;
  });

  const kindChips = KORDER.map(function (kd) {
    return html`<span class="ds-chip" style=${'color:var(' + KIND_VAR[kd] + ')'}><${Icon} n=${KINDS[kd].ic} size=${12} />${KINDS[kd].s}</span>`;
  });
  const stateChips = Object.keys(STATE_LABEL).map(function (s) {
    const col = 'var(' + STATE_VAR[s] + ')';
    return html`<span class="ds-chip" style=${'color:' + col}>
      <span style=${'width:7px;height:7px;border-radius:50%;background:' + col + ';display:inline-block'}></span>${STATE_LABEL[s]}</span>`;
  });

  const bodyTok = html`<div class="ds-sec">
    <h4>语义令牌（33 项）<span class="n">同名令牌深浅两套必有值 · 无断层</span></h4>
    <table class="ds-table"><thead><tr><th>令牌</th><th>用途</th><th>深色值</th><th>浅色值</th></tr></thead>
      <tbody>${tokenRows}</tbody></table></div>`;
  const bodyDec = html`<div class="ds-sec"><h4>关键视觉决策（12 条）</h4>${decisionRows}</div>`;
  const bodyDef = html`<div class="ds-sec"><h4>上一版四缺陷 · 根因与修法</h4>${defectRows}</div>`;
  const bodyOpen = html`<div class="ds-sec"><h4>需要你裁定（点选）</h4>${choiceRows}</div>`;
  const ck = runChecks();
  const ctRows = ck.ct.map(function (r) {
    return html`<tr><td>${r.th}</td><td class="mono">${r.a}</td><td class="mono">${r.b}</td><td>${r.use}</td>
      <td class="mono">${r.thr}:1</td><td class="mono">${r.v == null ? '—' : r.v.toFixed(2) + ':1'}</td>
      <td class=${r.ok ? 'ds-ok' : 'ds-fix'}>${r.ok ? '通过' : '未过'}</td></tr>`;
  });
  const bodyChk = html`<div class="ds-sec">
    <h4>自检 · 现算读数<span class="n">页面加载时按当前 CSS 变量与真实布局计算</span></h4>
    <div class="ds-check-row">
      <input type="checkbox" id="rmChk" checked=${rm}
        onChange=${function (e) { setRm(e.target.checked); }} />
      <label for="rmChk">模拟 prefers-reduced-motion（全停动画、功能不缺）</label></div>
    <div class="ds-kv" style="margin-top:10px">
      <span class="k">对比度合计</span><span class="v">${ck.pass} 通过 / ${ck.fail} 未过（共 ${ck.ct.length} 对）</span>
      <span class="k">文字最低</span><span class="v">${ck.minTxt == null ? '—' : ck.minTxt.toFixed(2) + ':1'}（门槛 4.5:1）</span>
      <span class="k">非文本最低</span><span class="v">${ck.minNon == null ? '—' : ck.minNon.toFixed(2) + ':1'}（门槛 3:1）</span>
      <span class="k">命中区</span><span class="v">视口内可点 ${ck.hitCount} 个，最小 ${ck.minHit == null ? '—' : ck.minHit.toFixed(1) + 'px'}（${ck.minHitWhat || '—'}）</span>
      <span class="k">令牌齐备</span><span class="v">${TOKENS.length} 项 × 深浅两套，缺项 ${ck.missing.length}${ck.missing.length ? '：' + ck.missing.join(', ') : ''}</span>
      <span class="k">动画名（当前）</span><span class="v">${ck.rmNames.join(' · ') || '—'}</span>
      <span class="k">当前主题 / 缩放</span><span class="v">${theme === 'dark' ? '深色' : '浅色'} · ${zoom}% · ${lod} 档</span>
      <span class="k">reduced-motion</span><span class="v">${rm ? '开：呼吸/流星/骨架全停' : '关'}</span>
    </div>
    <h4 style="margin-top:14px">对比度逐对</h4>
    <table class="ds-table"><thead><tr><th>套</th><th>前景</th><th>背景</th><th>用途</th><th>门槛</th><th>实测</th><th>判定</th></tr></thead>
      <tbody>${ctRows}</tbody></table>
  </div>`;
  const bodyComp = html`<div class="ds-sec">
    <h4>组件全态（与画布同源渲染）</h4>
    <div class="ds-comp">${kindChips}</div>
    <div class="ds-comp" style="margin-top:10px">${stateChips}</div>
    <div class="ds-li" style="margin-top:10px">按钮四态：默认 / 悬停（raised 底）/ 按下（位移 0.5px）/ 禁用（text-dis）。</div>
    <div class="ds-li">输入框：默认（line 边）/ 聚焦（强调环）/ 禁用 / 错误（blocked 边＋说明行）。</div>
    <div class="ds-li">四态容器：加载＝骨架、空＝确知 0＋说明、错误＝可重试＋错误码、无权限＝锁＋解释。</div>
  </div>`;

  const body = tab === 'token' ? bodyTok
    : tab === 'decision' ? bodyDec
    : tab === 'defect' ? bodyDef
    : tab === 'open' ? bodyOpen
    : tab === 'check' ? bodyChk
    : bodyComp;

  return html`<div class="ds-wrap open" onClick=${onClose}>
    <div class="ds" onClick=${function (e) { e.stopPropagation(); }}>
      <div class="ds-hd"><span class="t">设计说明</span><span class="v">${'v2 · React 形态'}</span>
        <button class="x" title="关闭" onClick=${onClose}><${Icon} n="close" size=${14} /></button></div>
      <div class="ds-tabs">${tabBtn}</div>
      <div class="ds-body">${body}</div>
    </div></div>`;
}
