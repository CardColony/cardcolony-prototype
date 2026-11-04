/* esm/i18n.js —— [双语自适应 2026-11-03] 中英双语层（零依赖、对业务代码零侵入）。
 *
 * 机制：
 *   1. 语言判定：localStorage "ui.lang" → navigator.language（zh* → zh，其余 → en）。
 *   2. 文本替换：整节点文本命中 i18n_dict.js 词条才换（按钮/标签/空态这类整体文案）；
 *      title / placeholder / aria-label / input[value] 四个属性同样按词条换。
 *   3. MutationObserver：JS 后续渲染出的节点自动过一遍——业务代码不用改一行。
 *   4. 右上语言钮：中|EN 切换，写回 localStorage 后 reload（最省心的全量重渲）。
 *
 * 边界（如实写）：
 *   - 只翻"整节点文案"，不做句内替换；卡面正文/用户内容若整段恰好与某词条逐字
 *     相同会被误译（词条全是 UI 短语，概率低，原型接受）；
 *   - <script>/<style>/<textarea>/[data-no-i18n] 子树跳过；
 *   - 词条表是唯一真相：往 i18n_dict.js 加一行，全站自动生效。
 *
 * 接线：四个模板各挂一行 <script type="module" src="/static/esm/i18n.js?v=2">。
 * 改本文件或词典：升下面 import 的 ?v=（浏览器按 URL 缓存模块）。
 */
import { ZH_EN } from "./i18n_dict.js?v=2";

const LS_KEY = "ui.lang";
const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "TEXTAREA", "NOSCRIPT"]);

function resolveLang() {
  try {
    const saved = localStorage.getItem(LS_KEY);
    if (saved === "zh" || saved === "en") return saved;
  } catch (e) { /* 隐私模式/禁用 storage 时落到浏览器语言 */ }
  const nav = (navigator.language || "").toLowerCase();
  return nav.startsWith("zh") ? "zh" : "en";
}

const LANG = resolveLang();
const ENABLED = LANG === "en" && ZH_EN && Object.keys(ZH_EN).length > 0;

/* "xxx：" 结尾的词条允许前缀命中：'发送失败：xxx' → 'Send failed: xxx'（后半保留原文） */
const PREFIX_KEYS = Object.keys(ZH_EN)
  .filter((k) => k.endsWith("："))
  .sort((a, b) => b.length - a.length);

function translateText(text) {
  if (!text) return text;
  const t = text.trim();
  const hit = ZH_EN[t];
  if (hit !== undefined) return hit;
  for (const k of PREFIX_KEYS) {
    if (t.startsWith(k)) return ZH_EN[k] + " " + t.slice(k.length).trimStart();
  }
  return text;
}

/* 一个元素/节点子树：文本节点整换 + 四个属性逐字换 */
function translateRoot(root) {
  if (!root || !ENABLED) return;
  const walker = document.createTreeWalker(
    root.nodeType === Node.TEXT_NODE ? root.parentNode || root : root,
    NodeFilter.SHOW_TEXT,
    {
      acceptNode(n) {
        if (!n.nodeValue || !/[一-鿿]/.test(n.nodeValue)) return NodeFilter.FILTER_REJECT;
        const p = n.parentElement;
        if (!p || SKIP_TAGS.has(p.tagName) || p.closest("[data-no-i18n]")) {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    }
  );
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes) {
    const next = translateText(n.nodeValue);
    if (next !== n.nodeValue) n.nodeValue = next;
  }
  if (root.querySelectorAll) {
    for (const el of root.querySelectorAll("[title],[placeholder],[aria-label]")) {
      for (const attr of ["title", "placeholder", "aria-label"]) {
        const v = el.getAttribute(attr);
        if (v) {
          const hit = ZH_EN[v.trim()];
          if (hit !== undefined) el.setAttribute(attr, hit);
        }
      }
    }
    for (const el of root.querySelectorAll("input[value]")) {
      const hit = ZH_EN[(el.value || "").trim()];
      if (hit !== undefined) el.value = hit;
    }
  }
}

/* <title> 在 head 里，单独翻 */
function translateTitle() {
  if (!ENABLED) return;
  const hit = ZH_EN[document.title.trim()];
  if (hit !== undefined) document.title = hit;
}

/* 右上语言切换钮：固定角落，不依赖任何页面结构 */
function mountToggle() {
  if (document.getElementById("i18nToggle")) return;
  const box = document.createElement("div");
  box.id = "i18nToggle";
  box.title = ZH_EN["切换语言 / Switch language"] || "Switch language";
  box.style.cssText =
    "position:fixed;right:12px;top:12px;z-index:99999;display:flex;" +
    "font:12px/1.4 system-ui,sans-serif;border:1px solid rgba(128,128,128,.45);" +
    "border-radius:14px;overflow:hidden;user-select:none;cursor:pointer;" +
    "background:var(--i18n-bg,rgba(30,30,34,.85));backdrop-filter:blur(4px);";
  const mk = (code, label) => {
    const s = document.createElement("span");
    s.textContent = label;
    s.dataset.lang = code;
    s.style.cssText =
      "padding:4px 9px;" +
      (code === LANG
        ? "background:rgba(90,140,255,.85);color:#fff;font-weight:600;"
        : "color:inherit;opacity:.65;");
    s.onclick = () => {
      try { localStorage.setItem(LS_KEY, code); } catch (e) {}
      location.reload();
    };
    return s;
  };
  box.appendChild(mk("zh", "中"));
  box.appendChild(mk("en", "EN"));
  document.body.appendChild(box);
}

function boot() {
  document.documentElement.lang = LANG === "zh" ? "zh-CN" : "en";
  if (ENABLED) {
    translateRoot(document.body);
    translateTitle();
    /* 动态内容：新插入的子树过同一遍。characterData 也看（流式吐字会把
       文本节点先写空后填充）。改属性不观察——避免自身 setAttribute 触发循环。 */
    const mo = new MutationObserver(muts => {
      for (const m of muts) {
        if (m.type === "characterData") {
          translateRoot(m.target);
        } else {
          for (const n of m.addedNodes) {
            if (n.nodeType === Node.ELEMENT_NODE || n.nodeType === Node.TEXT_NODE) {
              translateRoot(n);
            }
          }
        }
      }
    });
    mo.observe(document.body, { childList: true, subtree: true, characterData: true });
  }
  mountToggle();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
