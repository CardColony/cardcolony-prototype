/* esm/md.js —— 零依赖 Markdown 渲染器（对话区会话流样式 · 第一步：内容正确渲染）。
 *
 * escape-first＋白名单：先整段转义、再解析，原始 HTML 一律不渲染（防 XSS）。
 * 覆盖语法：标题（#~######）／粗体／斜体／删除线（~~）／高亮（==）／有序无序列表／
 *           任务列表（- [ ] / - [x]）／表格／行内码／代码块／引用／链接／水平线。
 * 语义标记：✓ ✅ ✗ ❌ ⚠ ☐ ☑ 加 <span class="sm …"> 壳（字符本身不变），着色交 CSS。
 * 行内码内容受占位保护：`**x**` 不会被二次解析成粗体。
 * 高亮 == 带守卫：!== / === / !== 这类代码比较不误判（前一位是 = ! < > 时不匹配）。
 * 纯函数，无 DOM 依赖；只供助手正文渲染使用（用户消息仍走 escapeHtml/pre-wrap）。
 * 与既有 esm 模块同风格（util.js 同款五字符转义，此处为模块内自足，不跨文件依赖）。
 */
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ESC[c]);
}

/* 语义标记加壳：字符本身不改，只包一层 <span class="sm …">，着色交 CSS。
   覆盖（按真实语料出现次数取，93 个 sessions/*.md 实测）：
     ok   ✓ U+2713(127) ✅ U+2705(388) ✔ U+2714(40)
     no   ✗ U+2717(6)  ❌ U+274C(13)  ✘ U+2718(3)
     warn ⚠ U+26A0(30) —— 先处理，连带吃掉变体选择符 U+FE0F（⚠️ 常以两码点出现）
     ask  ❓ U+2753(1)
     box  ☐ U+2610(8) ／ box on ☑ U+2611(15) */
function marks(s) {
  return s
    .replace(/\u26A0\uFE0F?/g, m => '<span class="sm warn">' + m + "</span>")
    .replace(/[\u2705\u2713\u2714\u274C\u2717\u2718\u2753\u2610\u2611]/g, c => {
      const cls = (c === "\u2705" || c === "\u2713" || c === "\u2714") ? "ok"
                : (c === "\u274C" || c === "\u2717" || c === "\u2718") ? "no"
                : (c === "\u2753") ? "ask"
                : (c === "\u2611") ? "box on" : "box";
      return '<span class="sm ' + cls + '">' + c + "</span>";
    });
}

function inline(s) {
  /* 行内码先抽出、占位保护，最后回填——否则 `**x**` 里的星号会被二次解析 */
  const codes = [];
  let t = esc(s).replace(/`([^`\n]+)`/g, (m, c) => {
    codes.push(c);
    return "\u0001" + (codes.length - 1) + "\u0001";
  });
  t = t
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
    .replace(/(^|[^=!<>])==([^=\n]+?)==(?!\s*[=!])/g, "$1<mark>$2</mark>");
  t = marks(t);
  return t.replace(/\u0001(\d+)\u0001/g, (m, n) => "<code>" + codes[+n] + "</code>");
}

function cells(row) {
  let a = row.split("|");
  if (a.length && a[0].trim() === "") a.shift();
  if (a.length && a[a.length - 1].trim() === "") a.pop();
  return a.map(c => inline(c.trim()));
}

export function renderMarkdown(src) {
  const lines = String(src == null ? "" : src).split("\n");
  const out = [];
  let i = 0, inCode = false, codeBuf = [];
  while (i < lines.length) {
    const raw = lines[i];
    const t = raw.trim();
    if (t.startsWith("```")) {
      if (!inCode) { inCode = true; codeBuf = []; }
      else { inCode = false; out.push("<pre><code>" + esc(codeBuf.join("\n")) + "</code></pre>"); }
      i++; continue;
    }
    if (inCode) { codeBuf.push(raw); i++; continue; }
    if (t === "") { i++; continue; }

    /* 表格：当前行以 | 开头、且下一行是分隔行 */
    if (t.startsWith("|") && i + 1 < lines.length &&
        /^\s*\|[\s:|-]+\|/.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      let rows = "<tr>" + cells(raw).map(c => "<th>" + c + "</th>").join("") + "</tr>";
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        rows += "<tr>" + cells(lines[i]).map(c => "<td>" + c + "</td>").join("") + "</tr>";
        i++;
      }
      out.push("<table>" + rows + "</table>"); continue;
    }
    /* 标题 */
    const h = t.match(/^(#{1,6})\s+(.*)$/);
    if (h) { const n = h[1].length; out.push("<h" + n + ">" + inline(h[2]) + "</h" + n + ">"); i++; continue; }
    /* 水平线 */
    if (/^-{3,}$/.test(t)) { out.push("<hr>"); i++; continue; }
    /* 引用 */
    if (t.startsWith(">")) {
      const q = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) { q.push(lines[i].trim().replace(/^>\s?/, "")); i++; }
      out.push("<blockquote>" + q.map(s => "<p>" + inline(s) + "</p>").join("") + "</blockquote>"); continue;
    }
    /* 列表（任务项／无序／有序／两级缩进子项） */
    if (/^[-*]\s+/.test(t) || /^\d+\.\s+/.test(t)) {
      const ordered = /^\d+\.\s+/.test(t);
      const items = [];
      while (i < lines.length) {
        const tl = lines[i].trim();
        const task = tl.match(/^[-*]\s+\[([ xX])\]\s*(.*)$/);
        if (task) {
          const done = task[1] !== " ";
          items.push('<li class="task ' + (done ? "done" : "open") + '"><span class="cb">' +
            (done ? "\u2611" : "\u2610") + "</span>" + inline(task[2]) + "</li>");
          i++;
        }
        else if (/^[-*]\s+/.test(tl)) { items.push("<li>" + inline(tl.replace(/^[-*]\s+/, "")) + "</li>"); i++; }
        else if (/^\d+\.\s+/.test(tl)) { items.push("<li>" + inline(tl.replace(/^\d+\.\s+/, "")) + "</li>"); i++; }
        else if (/^\s{2,}[-*]\s+/.test(lines[i])) { items.push("<li>" + inline(tl.replace(/^\s*[-*]\s+/, "")) + "</li>"); i++; }
        else break;
      }
      out.push("<" + (ordered ? "ol" : "ul") + ">" + items.join("") + "</" + (ordered ? "ol" : "ul") + ">");
      continue;
    }
    /* 普通段落（合并连续非空非块首行） */
    const para = [raw];
    i++;
    while (i < lines.length && lines[i].trim() !== "" &&
           !/^\s*(```|#{1,6}\s|>|-{3,}$)/.test(lines[i]) &&
           !/^\s*\|/.test(lines[i]) && !/^[-*]\s+/.test(lines[i]) && !/^\d+\.\s+/.test(lines[i])) {
      para.push(lines[i]); i++;
    }
    out.push("<p>" + para.map(s => inline(s)).join("<br>") + "</p>");
  }
  return out.join("\n");
}
