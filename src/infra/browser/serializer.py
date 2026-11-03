# -*- coding: utf-8 -*-
"""语义树序列化器（注入式 JS + ref→xpath 映射）。

设计参照 minimax-code 的 inspect：给模型的不是 HTML 也不是截图，
而是「可操作的页面语义地图」——角色/名字/状态/位置/可交互性，
每元素配一个快照域 ref。

与 minimax 的差异（v1 简化）：它合并 DOMSnapshot+DOM+AXTree 三路
CDP 域；本实现只在页面内 JS 走 DOM（参照 browser-use 路线），
AX 语义用启发式近似（role 属性 > 标签映射 > aria-*）。够用、省一半
工程量；真不够用再上 AX 域。

ref 语义（对齐 minimax）：
- 形如 ``e<gen>.<n>``，不透明，只对签发它的快照有效；
- 映射表 ``window.__cb_refs`` 存页面 window 上——页面一导航/reload，
  映射表随 window 消失 ⇒ **旧 ref 自动作废**（STALE_REF）；
- gen 由 session 层管（每次成功导航 +1）。

安全：input[type=password] 一律不带 value；其余 value 截断。
"""

from __future__ import annotations

#: 注入页的序列化函数。参数 {gen, maxEls, includeInvisible}
#: 返回 {url,title,gen,viewport,count,truncated,elements[]}
SERIALIZER_JS = r"""
(function (opts) {
  var gen = (opts && opts.gen) || 1;
  var maxEls = (opts && opts.maxEls) || 400;
  var refs = {};
  refs.__gen = gen;
  var out = [];
  var n = 0;

  var ROLE_TAGS = {
    a: 'link', button: 'button', select: 'combobox', textarea: 'textbox',
    summary: 'button', h1: 'heading', h2: 'heading', h3: 'heading',
    h4: 'heading', h5: 'heading', h6: 'heading', nav: 'navigation',
    main: 'main', form: 'form', table: 'table', ul: 'list', ol: 'list',
    li: 'listitem', img: 'img', dialog: 'dialog', aside: 'complementary',
    footer: 'contentinfo', header: 'banner', section: 'region',
    fieldset: 'group', progress: 'progressbar'
  };
  var INPUT_ROLES = {
    button: 'button', submit: 'button', reset: 'button', image: 'button',
    checkbox: 'checkbox', radio: 'radio', range: 'slider',
    search: 'searchbox', email: 'textbox', tel: 'textbox', url: 'textbox',
    password: 'textbox', text: 'textbox', number: 'spinbutton',
    file: 'button', color: 'button', date: 'textbox', time: 'textbox',
    'datetime-local': 'textbox', month: 'textbox', week: 'textbox'
  };
  var INTERACTIVE_ROLES = {
    button: 1, link: 1, textbox: 1, checkbox: 1, radio: 1, combobox: 1,
    listbox: 1, option: 1, menuitem: 1, tab: 1, switch: 1, slider: 1,
    spinbutton: 1, searchbox: 1, treeitem: 1
  };
  var SKIP_TAGS = {
    script: 1, style: 1, noscript: 1, template: 1, meta: 1, link: 1,
    title: 1, head: 1, br: 1, hr: 1, wbr: 1, base: 1
  };

  function clip(s, n) {
    if (s == null) return '';
    s = String(s).replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  function xpathOf(el) {
    if (el.id && /^[A-Za-z][\w\-:.]*$/.test(el.id)) {
      var same = el.ownerDocument.querySelectorAll('[id="' +
        el.id.replace(/"/g, '\\"') + '"]');
      if (same.length === 1) return '//*[@id="' + el.id + '"]';
    }
    var parts = [];
    var cur = el;
    while (cur && cur.nodeType === 1 && cur !== el.ownerDocument.documentElement) {
      var tag = cur.tagName.toLowerCase();
      var idx = 1;
      var sib = cur.previousElementSibling;
      while (sib) {
        if (sib.tagName === cur.tagName) idx++;
        sib = sib.previousElementSibling;
      }
      parts.unshift(tag + '[' + idx + ']');
      cur = cur.parentElement;
      if (parts.length > 12) break;
    }
    return '/html/' + parts.join('/');
  }

  function isVisible(el) {
    var st = el.ownerDocument.defaultView.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' ||
        st.visibility === 'collapse') return false;
    var r = el.getBoundingClientRect();
    if (r.width <= 0 && r.height <= 0) {
      // 纯 inline 空壳（如 <span>）允许通过——有 rect 才谈得上点
      if (el.getClientRects().length === 0) return false;
    }
    return true;
  }

  function roleOf(el, tag) {
    var r = el.getAttribute('role');
    if (r) return r.split(' ')[0].toLowerCase();
    if (tag === 'input') {
      var t = (el.getAttribute('type') || 'text').toLowerCase();
      return INPUT_ROLES[t] || 'textbox';
    }
    return ROLE_TAGS[tag] || 'generic';
  }

  function nameOf(el, tag) {
    var v = el.getAttribute('aria-label');
    if (v && v.trim()) return clip(v, 80);
    var lb = el.getAttribute('aria-labelledby');
    if (lb) {
      var parts = [];
      lb.split(/\s+/).forEach(function (id) {
        var n2 = el.ownerDocument.getElementById(id);
        if (n2) parts.push(n2.innerText || n2.textContent || '');
      });
      v = parts.join(' ');
      if (v.trim()) return clip(v, 80);
    }
    v = el.getAttribute('alt'); if (v && v.trim()) return clip(v, 80);
    v = el.getAttribute('title'); if (v && v.trim()) return clip(v, 80);
    v = el.getAttribute('placeholder'); if (v && v.trim()) return clip(v, 80);
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      if (el.labels && el.labels.length) {
        var lt = [];
        for (var i = 0; i < el.labels.length; i++) lt.push(el.labels[i].innerText || '');
        v = lt.join(' ');
        if (v.trim()) return clip(v, 80);
      }
      var bt = (el.getAttribute('type') || '').toLowerCase();
      if (bt === 'button' || bt === 'submit' || bt === 'reset') {
        v = el.getAttribute('value');
        if (v && v.trim()) return clip(v, 80);
      }
    }
    // 直接文本（不含子元素的文本）
    var own = '';
    for (var c = el.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 3) own += c.textContent;
    }
    if (own.trim()) return clip(own, 80);
    if (tag === 'a' || tag === 'button' || tag === 'summary' ||
        INTERACTIVE_ROLES[roleOf(el, tag)]) {
      v = el.innerText || el.textContent || '';
      if (v.trim()) return clip(v, 80);
    }
    return '';
  }

  function actionable(el, tag, role) {
    if (el.disabled) return false;
    if (tag === 'a') return !!el.getAttribute('href') || el.hasAttribute('onclick');
    if (tag === 'button' || tag === 'select' || tag === 'textarea' ||
        tag === 'summary') return true;
    if (tag === 'input') {
      var t = (el.getAttribute('type') || 'text').toLowerCase();
      return t !== 'hidden';
    }
    if (INTERACTIVE_ROLES[role]) return true;
    if (el.hasAttribute('onclick') || el.isContentEditable) return true;
    var ti = el.getAttribute('tabindex');
    if (ti != null && parseInt(ti, 10) >= 0) return true;
    var cur = el.ownerDocument.defaultView.getComputedStyle(el).cursor;
    return cur === 'pointer';
  }

  function ownText(el) {
    var t = '';
    for (var c = el.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 3) t += c.textContent;
    }
    return clip(t, 80);
  }

  function collect(el, depth) {
    if (n >= maxEls || !el || el.nodeType !== 1) return;
    var tag = el.tagName.toLowerCase();
    if (SKIP_TAGS[tag]) return;
    // 开 shadow root 递归（closed 拿不到，放弃）
    if (el.shadowRoot) {
      var kids = el.shadowRoot.children;
      for (var i = 0; i < kids.length; i++) collect(kids[i], depth + 1);
      return;
    }
    if (tag === 'iframe' || tag === 'frame') {
      if (isVisible(el)) {
        n++;
        var rr = el.getBoundingClientRect();
        var ref = 'e' + gen + '.' + n;
        refs[ref] = { xpath: xpathOf(el) };
        out.push({
          ref: ref, tag: tag, role: 'frame',
          name: clip(el.getAttribute('name') || el.getAttribute('title') || '', 80),
          attrs: { src: clip(el.getAttribute('src') || '', 200) },
          rect: { x: rr.x, y: rr.y, w: rr.width, h: rr.height },
          inView: rr.bottom > 0 && rr.top < innerHeight &&
                  rr.right > 0 && rr.left < innerWidth,
          act: false, states: {}
        });
      }
      return;
    }
    var vis = isVisible(el);
    if (vis) {
      var role = roleOf(el, tag);
      var name = nameOf(el, tag);
      var act = actionable(el, tag, role);
      // 无名 generic 容器不独立成项（裁语义树的关键一刀），但容器本身
      // 仍要往下走——有意义的元素可能在它里面。
      var keep = act || name || role !== 'generic' ||
                 tag === 'a' || tag === 'img';
      if (keep) {
        n++;
        var rect = el.getBoundingClientRect();
        var ref = 'e' + gen + '.' + n;
        refs[ref] = { xpath: xpathOf(el) };
        var attrs = {};
        if (tag === 'a') attrs.href = clip(el.getAttribute('href') || '', 200);
        if (tag === 'input' || tag === 'textarea' || tag === 'select') {
          attrs.type = (el.getAttribute('type') || 'text').toLowerCase();
          if (attrs.type === 'password') {
            attrs.value = el.value ? '••••••' : '';
          } else if (tag !== 'select') {
            attrs.value = clip(el.value || '', 80);
          }
          if (el.placeholder) attrs.placeholder = clip(el.placeholder, 80);
        }
        if (tag === 'select') {
          var ops = [];
          for (var oi = 0; oi < el.options.length && oi < 40; oi++) {
            ops.push(clip(el.options[oi].text, 60));
          }
          attrs.options = ops;
          attrs.value = clip(el.value || '', 80);
        }
        if (tag === 'img') attrs.src = clip(el.getAttribute('src') || '', 200);
        if (el.id) attrs.id = el.id;
        var states = {};
        if (el.disabled) states.disabled = true;
        if (el.checked !== undefined && (attrs.type === 'checkbox' || attrs.type === 'radio'))
          states.checked = !!el.checked;
        var ar = el.getAttribute('aria-expanded');
        if (ar != null) states.expanded = ar === 'true';
        var as = el.getAttribute('aria-selected') || (el.selected ? 'true' : null);
        if (as != null && (role === 'option' || role === 'tab' || tag === 'option'))
          states.selected = as === 'true';
        if (el.required) states.required = true;
        if (el.readOnly) states.readonly = true;
        var e = {
          ref: ref, tag: tag, role: role, name: name,
          text: ownText(el),
          rect: { x: Math.round(rect.x * 10) / 10,
                  y: Math.round(rect.y * 10) / 10,
                  w: Math.round(rect.width * 10) / 10,
                  h: Math.round(rect.height * 10) / 10 },
          inView: rect.bottom > 0 && rect.top < innerHeight &&
                  rect.right > 0 && rect.left < innerWidth,
          act: act, states: states
        };
        if (Object.keys(attrs).length) e.attrs = attrs;
        out.push(e);
      }
    }
    var kids2 = el.children;
    for (var j = 0; j < kids2.length; j++) {
      if (n >= maxEls) break;
      collect(kids2[j], depth + 1);
    }
  }

  try {
    collect(document.body || document.documentElement, 0);
  } catch (e) {
    return { error: String(e), url: location.href };
  }
  window.__cb_refs = refs;
  // 可操作＋视口内优先，其次按文档序（收集序即文档序，已稳定）
  out.sort(function (a, b) {
    var ka = (a.act ? 0 : 2) + (a.inView ? 0 : 1);
    var kb = (b.act ? 0 : 2) + (b.inView ? 0 : 1);
    return ka - kb;
  });
  return {
    url: location.href, title: document.title, gen: gen,
    viewport: { w: innerWidth, h: innerHeight, scrollY: scrollY },
    count: out.length, truncated: n >= maxEls, elements: out
  };
})
"""

#: ref → 元素解析器。返回 {ok, rect, tag, reason}
#: 顺带做代次校验（__cb_refs.__gen 不匹配 ⇒ STALE）。
RESOLVER_JS = r"""
(function (args) {
  var ref = args.ref, gen = args.gen;
  var store = window.__cb_refs;
  if (!store || store.__gen !== gen)
    return { ok: false, reason: 'STALE' };
  var rec = store[ref];
  if (!rec) return { ok: false, reason: 'NO_REF' };
  var el = null;
  try {
    el = document.evaluate(rec.xpath, document, null,
      XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
  } catch (e) {}
  if (!el || el.nodeType !== 1) return { ok: false, reason: 'GONE' };
  var r = el.getBoundingClientRect();
  var st = getComputedStyle(el);
  return {
    ok: true,
    tag: el.tagName.toLowerCase(),
    rect: { x: r.x, y: r.y, w: r.width, h: r.height },
    cx: r.left + r.width / 2, cy: r.top + r.height / 2,
    visible: st.display !== 'none' && st.visibility !== 'hidden' &&
             (r.width > 0 || r.height > 0),
    disabled: !!el.disabled
  };
})
"""
