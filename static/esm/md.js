/* esm/md.js —— 对话区 Markdown 渲染器（商业级版）
 *
 * 安全模型不变：**先转义、再解析**，原始 HTML 一律不执行（白名单式）。
 *
 * 相比上一版增强四件（对话界面商业化 2026-10-17）：
 *   1. 代码块：语言标签 + 一键复制 + 轻量语法高亮（自研 tokenizer，零外部依赖、离线可用）。
 *   2. 表格：外层 .table-wrap 承担圆角与横向滚动（表格本身不再撑破内容列）。
 *   3. 列表：真嵌套（按缩进建树），支持任务列表、有序/无序混排。
 *   4. 段落：段内软换行保留为 <br>（中文模型输出的「一句一行」是常态，
 *      合并成一行反而不可读）；段落间距交给 CSS。
 *
 * 刻意不做：脚注、内嵌 HTML、图片自动加载（外部资源会破坏离线与隐私边界）。
 */

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ESC[c]);
}

/* ---------------------------------------------------------------------------
   语义标记加壳：字符本身不改，只包一层 <span class="sm …">，着色交 CSS。
   --------------------------------------------------------------------------- */
function marks(s) {
  return s
    .replace(/\u26A0\uFE0F?/g, (m) => '<span class="sm warn">' + m + "</span>")
    .replace(/[\u2705\u2713\u2714\u274C\u2717\u2718\u2753\u2610\u2611]/g, (c) => {
      const cls =
        c === "\u2705" || c === "\u2713" || c === "\u2714" ? "ok"
        : c === "\u274C" || c === "\u2717" || c === "\u2718" ? "no"
        : c === "\u2753" ? "ask"
        : c === "\u2611" ? "box on"
        : "box";
      return '<span class="sm ' + cls + '">' + c + "</span>";
    });
}

/* ---------------------------------------------------------------------------
   语法高亮（轻量 tokenizer）
   ---------------------------------------------------------------------------
   设计取舍：不用正则一把梭（字符串里的 // 会误判成注释），改用**顺序扫描**——
   每步只判断「当前位置是什么」，命中就整体吞掉，永不回退。O(n)、无回溯、
   对不完整代码（流式输出中途）也稳定。
   --------------------------------------------------------------------------- */

const KW = {
  js: "const let var function return if else for while do break continue class extends new this super import export from default async await try catch finally throw typeof instanceof in of null undefined true false switch case default delete void yield static get set",
  ts: "const let var function return if else for while do break continue class extends implements interface type enum new this super import export from default async await try catch finally throw typeof instanceof in of null undefined true false switch case default readonly public private protected abstract as satisfies keyof infer never unknown any string number boolean void",
  py: "def class return if elif else for while break continue import from as pass raise try except finally with lambda yield global nonlocal assert del in is not and or None True False async await match case self",
  go: "func var const type struct interface return if else for range break continue switch case default go defer map chan package import nil true false make new select",
  rust: "fn let mut const struct enum impl trait return if else for while loop break continue match use mod pub crate self super as where unsafe async await move ref dyn box true false",
  java: "public private protected class interface extends implements static final void int long double float boolean char String new return if else for while do switch case break continue try catch finally throw throws import package null true false this super",
  sh: "if then else elif fi for while do done case esac function return exit export local readonly set unset shift source echo cd ls cat grep sed awk rm cp mv mkdir",
  sql: "select from where insert into values update set delete create table alter drop index join left right inner outer on group by order having limit offset as and or not null distinct union all",
  css: "important media supports keyframes from to",
  json: "true false null",
  yaml: "true false null yes no",
  html: "",
  xml: "",
  text: "",
};

/** 语言别名归一。 */
function normLang(raw) {
  const s = String(raw || "").trim().toLowerCase();
  if (!s) return "";
  const map = {
    javascript: "js", jsx: "js", mjs: "js", cjs: "js", node: "js",
    typescript: "ts", tsx: "ts",
    python: "py", py3: "py",
    golang: "go", rs: "rust",
    bash: "sh", shell: "sh", zsh: "sh", console: "sh", terminal: "sh",
    postgres: "sql", postgresql: "sql", mysql: "sql", sqlite: "sql",
    yml: "yaml", htm: "html", vue: "html", svelte: "html",
    plaintext: "text", txt: "text", "": "",
  };
  return map[s] !== undefined ? map[s] : s;
}

/** 该语言用哪种行注释前缀。 */
function lineComment(lang) {
  if (lang === "py" || lang === "sh" || lang === "yaml" || lang === "rb" || lang === "r") return "#";
  if (lang === "sql") return "--";
  if (lang === "text" || lang === "") return "";
  return "//";
}

function hasBlockComment(lang) {
  return !["py", "sh", "yaml", "rb", "r", "sql", "text", ""].includes(lang);
}

const ID_START = /[A-Za-z_$]/;
const ID_CHAR = /[A-Za-z0-9_$]/;
const DIGIT = /[0-9]/;

/**
 * @param {string} code 原始代码（未转义）
 * @param {string} lang 归一后的语言
 * @returns {string} 已转义、带 <span class="tok-*"> 的 HTML
 */
function highlight(code, lang) {
  const kws = new Set((KW[lang] || "").split(/\s+/).filter(Boolean));
  const lc = lineComment(lang);
  const block = hasBlockComment(lang);
  const n = code.length;
  let out = "";
  let i = 0;

  const push = (cls, text) => {
    const t = esc(text);
    out += cls ? '<span class="tok-' + cls + '">' + t + "</span>" : t;
  };

  while (i < n) {
    const c = code[i];

    // 行注释
    if (lc && c === lc[0] && code.startsWith(lc, i)) {
      let j = code.indexOf("\n", i);
      if (j < 0) j = n;
      push("com", code.slice(i, j));
      i = j;
      continue;
    }
    // 块注释
    if (block && c === "/" && code[i + 1] === "*") {
      let j = code.indexOf("*/", i + 2);
      j = j < 0 ? n : j + 2;
      push("com", code.slice(i, j));
      i = j;
      continue;
    }
    // 字符串（含模板串、三引号）
    if (c === '"' || c === "'" || c === "`") {
      const triple = code.startsWith(c + c + c, i);
      if (triple) {
        let j = code.indexOf(c + c + c, i + 3);
        j = j < 0 ? n : j + 3;
        push("str", code.slice(i, j));
        i = j;
        continue;
      }
      let j = i + 1;
      while (j < n) {
        if (code[j] === "\\") { j += 2; continue; }
        if (code[j] === c) { j++; break; }
        if (code[j] === "\n" && c !== "`") break;   // 未闭合单行串：到行尾止，不吞后文
        j++;
      }
      push("str", code.slice(i, j));
      i = j;
      continue;
    }
    // 数字
    if (DIGIT.test(c) && (i === 0 || !ID_CHAR.test(code[i - 1]))) {
      let j = i;
      while (j < n && /[0-9a-fA-FxX._]/.test(code[j])) j++;
      push("num", code.slice(i, j));
      i = j;
      continue;
    }
    // 标识符
    if (ID_START.test(c)) {
      let j = i + 1;
      while (j < n && ID_CHAR.test(code[j])) j++;
      const word = code.slice(i, j);
      if (kws.has(word)) push("key", word);
      else if (code[j] === "(") push("fn", word);
      else if (/^[A-Z]/.test(word) && word.length > 1) push("var", word);
      else out += esc(word);
      i = j;
      continue;
    }
    // 操作符
    if ("=+-*/%<>!&|^~?:".includes(c)) {
      push("op", c);
      i++;
      continue;
    }
    out += esc(c);
    i++;
  }
  return out;
}

/* ---------------------------------------------------------------------------
   行内解析
   --------------------------------------------------------------------------- */
function inline(s) {
  const codes = [];
  let t = esc(s).replace(/`([^`\n]+)`/g, (m, c) => {
    codes.push(c);
    return "\u0001" + (codes.length - 1) + "\u0001";
  });
  t = t
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
    .replace(/(^|[^=!<>])==([^=\n]+?)==(?!\s*[=!])/g, "$1<mark>$2</mark>");
  t = marks(t);
  return t.replace(/\u0001(\d+)\u0001/g, (m, n) => "<code>" + codes[+n] + "</code>");
}

function cells(row) {
  const a = row.split("|");
  if (a.length && a[0].trim() === "") a.shift();
  if (a.length && a[a.length - 1].trim() === "") a.pop();
  return a.map((c) => inline(c.trim()));
}

/* ---------------------------------------------------------------------------
   列表（按缩进建树，支持嵌套）
   --------------------------------------------------------------------------- */
function listItemHtml(text, ordered, taskMark) {
  if (taskMark !== null) {
    const done = taskMark !== " ";
    return (
      '<li class="task ' + (done ? "done" : "open") + '">' +
      '<span class="cb">' + (done ? "\u2611" : "\u2610") + "</span>" +
      "<span>" + inline(text) + "</span></li>"
    );
  }
  return "<li>" + inline(text) + "</li>";
}

function renderList(lines, start) {
  const items = [];
  let i = start;
  while (i < lines.length) {
    const raw = lines[i];
    const m = raw.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (!m) {
      /* 宽松列表（loose list）：项之间夹一个空行仍是同一个列表。
         模型输出里极常见「每项之间空一行」，按严格 markdown 会断成多个 <ul>，
         视觉上就变成一堆孤立圆点——这里容忍空行，列表保持整体。 */
      if (raw.trim() === "" && i + 1 < lines.length &&
          /^(\s*)([-*+]|\d+[.)])\s+/.test(lines[i + 1])) {
        i++;
        continue;
      }
      break;
    }
    const indent = m[1].replace(/\t/g, "  ").length;
    const ordered = /^\d/.test(m[2]);
    const body = m[3];
    const task = body.match(/^\[([ xX])\]\s*(.*)$/);
    items.push({
      indent,
      ordered,
      text: task ? task[2] : body,
      taskMark: task ? task[1] : null,
    });
    i++;
  }

  // 栈式建树：每层 {ordered, children:[{item|sub}]}
  const root = { indent: -1, ordered: items.length ? items[0].ordered : false, nodes: [] };
  const stack = [root];
  for (const it of items) {
    while (stack.length > 1 && it.indent < stack[stack.length - 1].indent) stack.pop();
    let top = stack[stack.length - 1];
    if (it.indent > top.indent && top !== root) {
      const sub = { indent: it.indent, ordered: it.ordered, nodes: [] };
      top.nodes.push({ sub });
      stack.push(sub);
      top = sub;
    } else if (it.indent > top.indent) {
      const sub = { indent: it.indent, ordered: it.ordered, nodes: [] };
      top.nodes.push({ sub });
      stack.push(sub);
      top = sub;
    }
    top.nodes.push({ item: it });
  }

  function emit(node) {
    const tag = node.ordered ? "ol" : "ul";
    let html = "<" + tag + ">";
    for (const nd of node.nodes) {
      if (nd.item) html += listItemHtml(nd.item.text, nd.item.ordered, nd.item.taskMark);
      else if (nd.sub) {
        // 子列表挂在上一项内部
        html = html.replace(/<\/li>$/, "") + emit(nd.sub) + "</li>";
      }
    }
    return html + "</" + tag + ">";
  }
  return { html: emit(root), next: i };
}

/* ---------------------------------------------------------------------------
   主渲染
   --------------------------------------------------------------------------- */
export function renderMarkdown(src) {
  const lines = String(src == null ? "" : src).split("\n");
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const raw = lines[i];
    const t = raw.trim();

    // 围栏代码块（支持 ``` 与 ~~~，可带语言）
    const fence = t.match(/^(```+|~~~+)\s*([A-Za-z0-9_+#.-]*)\s*$/);
    if (fence) {
      const marker = fence[1][0];
      const langRaw = fence[2] || "";
      const lang = normLang(langRaw);
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(marker.repeat(3))) {
        buf.push(lines[i]);
        i++;
      }
      i++; // 跳过收尾围栏（未闭合时自然越界，循环条件兜住）
      const code = buf.join("\n");
      const body = lang === "text" || lang === "" ? esc(code) : highlight(code, lang);
      out.push(
        '<div class="code-block" data-lang="' + esc(lang || "text") + '">' +
        '<div class="code-head"><span class="code-lang">' + esc(langRaw || "text") + "</span>" +
        '<button type="button" class="code-copy" data-copy="1">' +
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true">' +
        '<rect x="5.2" y="5.2" width="8" height="8" rx="1.6"/><path d="M10.8 3.2H3.6a.8.8 0 0 0-.8.8v7.2"/></svg>' +
        "<span>复制</span></button></div>" +
        "<pre><code>" + body + "</code></pre></div>"
      );
      continue;
    }

    if (t === "") { i++; continue; }

    // 表格：当前行以 | 开头、下一行是分隔行
    if (t.startsWith("|") && i + 1 < lines.length &&
        /^\s*\|[\s:|-]+\|/.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      let rows = "<thead><tr>" + cells(raw).map((c) => "<th>" + c + "</th>").join("") + "</tr></thead><tbody>";
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        rows += "<tr>" + cells(lines[i]).map((c) => "<td>" + c + "</td>").join("") + "</tr>";
        i++;
      }
      out.push('<div class="table-wrap"><table>' + rows + "</tbody></table></div>");
      continue;
    }

    // 标题
    const h = t.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const n = h[1].length;
      out.push("<h" + n + ">" + inline(h[2]) + "</h" + n + ">");
      i++;
      continue;
    }

    // 水平线
    if (/^([-*_])\1{2,}$/.test(t)) { out.push("<hr>"); i++; continue; }

    // 引用（支持内部列表与多段）
    if (t.startsWith(">")) {
      const q = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        q.push(lines[i].trim().replace(/^>\s?/, ""));
        i++;
      }
      out.push("<blockquote>" + renderMarkdown(q.join("\n")) + "</blockquote>");
      continue;
    }

    // 列表
    if (/^\s*([-*+]|\d+[.)])\s+/.test(raw)) {
      const r = renderList(lines, i);
      out.push(r.html);
      i = r.next;
      continue;
    }

    // 段落（段内软换行保留 <br>）
    const para = [raw];
    i++;
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !/^\s*(```|~~~|#{1,6}\s|>)/.test(lines[i]) &&
      !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) &&
      !/^\s*\|/.test(lines[i]) &&
      !/^([-*_])\1{2,}\s*$/.test(lines[i].trim())
    ) {
      para.push(lines[i]);
      i++;
    }
    out.push("<p>" + para.map((s) => inline(s)).join("<br>") + "</p>");
  }

  return out.join("\n");
}

/** 代码块复制：由宿主在 document 上做一次事件委托（本模块保持纯函数）。 */
export function bindCopyButtons(root) {
  const host = root || document;
  host.addEventListener("click", (e) => {
    const btn = e.target.closest && e.target.closest(".code-copy");
    if (!btn) return;
    const block = btn.closest(".code-block");
    const code = block && block.querySelector("pre code");
    if (!code) return;
    const text = code.textContent || "";
    const done = () => {
      const label = btn.querySelector("span");
      const old = label ? label.textContent : "";
      if (label) label.textContent = "已复制";
      btn.classList.add("is-ok");
      setTimeout(() => {
        if (label) label.textContent = old || "复制";
        btn.classList.remove("is-ok");
      }, 1600);
    };
    const fallback = () => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); done(); } catch (err) { /* 静默 */ }
      document.body.removeChild(ta);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  });
}
