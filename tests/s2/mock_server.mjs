/* tests/s2/mock_server.mjs —— [S2] DOM 观察用的纯 mock 载体（用户令：真实项目不做测试）。
 *
 * 内存假数据 + 一次性 node 进程 + 高位端口；static/ 与 templates/ 只读。
 * 不写任何 works、不碰 5700/5701。判据操作结束后直接杀进程即销毁。
 *
 * 起法：node tests/s2/mock_server.mjs [port=18777]
 * 测试控制端点（只影响本进程内存假数据）：
 *   POST /__test/retitle   → 改 mock 卡标题并 bump version（验 V-R3/V-V6 变化路径）
 */
import http from "node:http";
import { readFileSync, existsSync } from "node:fs";
import { join, extname, normalize } from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";

const ROOT = join(fileURLToPath(new URL(".", import.meta.url)), "..", "..");
const PORT = Number(process.argv[2] || 18777);
const MIME = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".mjs": "text/javascript" };

/* ---------- 内存假数据（全部虚构，零真实卡号/标题） ---------- */
function mkCard(card_id, title, parent, child_count, work_id) {
  return {
    card_id, title, parent, child_count,
    last_active_at: "2026-10-11T10:00:00+08:00",
    branch_active_at: null, work_id, state: "",
  };
}
const cards = {
  // 形状对齐生产实测（2026-10-11 work_002）：根卡(work_card_id)不在 cards 数组，
  // 根层＝parent===work_card_id 的卡
  work_m1: [
    mkCard("card_m_a", "假卡甲-A", "card_m_root", 1, "work_m1"),
    mkCard("card_m_a1", "假卡甲-A-一（长标题截断测试 Lorem ipsum dolor sit amet consectetur）", "card_m_a", 0, "work_m1"),
    mkCard("card_m_b", "假卡甲-B", "card_m_root", 0, "work_m1"),
  ],
  work_m2: [
    mkCard("card_m2_x", "假卡乙-X", "card_m2_root", 0, "work_m2"),
  ],
  // [S3] 深树：L1..L5（L4 depth=3 触发面包屑；L5 depth=4 触发引导线）＋branch_active 活跃样本
  work_m3: [
    mkCard("card_m3_l1", "假卡丙-L1", "card_m3_root", 1, "work_m3"),
    mkCard("card_m3_l2", "假卡丙-L2", "card_m3_l1", 1, "work_m3"),
    mkCard("card_m3_l3", "假卡丙-L3", "card_m3_l2", 1, "work_m3"),
    mkCard("card_m3_l4", "假卡丙-L4（深卡）", "card_m3_l3", 1, "work_m3"),
    mkCard("card_m3_l5", "假卡丙-L5（引导线档）", "card_m3_l4", 0, "work_m3"),
  ],
};
const projects = [
  { work_id: "work_m1", title: "假项目甲", work_card_id: "card_m_root", card_count: 3, last_active_at: "2026-10-11T10:00:00+08:00" },
  { work_id: "work_m2", title: "假项目乙", work_card_id: "card_m2_root", card_count: 1, last_active_at: "2026-10-10T10:00:00+08:00" },
  { work_id: "work_m3", title: "假项目丙", work_card_id: "card_m3_root", card_count: 5, last_active_at: "2026-10-11T11:00:00+08:00" },
];
// [S3] 节点活跃徽章样本：card_m_a 距今 1h（<48h 应亮「活跃」），其余 branch_active_at 为空
cards.work_m1[0].branch_active_at = new Date(Date.now() - 3600 * 1000).toISOString();

// [S3/R-21 观察用] 幽灵卡注册表：模拟「曾在但已被删」的卡——getCard 仍应答（带 parent），
// 但 /api/nav 与 /api/nav/children 永远不含（快照缺失→点击走灰态兜底）。纯内存。
const ghosts = new Map();
function fingerprint(w) {
  // 全内容 sha256(生产口径全长指纹;截断指纹会让标题变化不进指纹)
  return createHash("sha256").update(JSON.stringify(cards[w])).digest("hex");
}
let versionSeq = 1;
const version = () => "v" + String(versionSeq).padStart(4, "0");

/* ---------- 端点 ---------- */
function api(req, res, url) {
  const p = url.pathname;
  const send = (o, code = 200) => { res.writeHead(code, { "Content-Type": "application/json" }); res.end(JSON.stringify(o)); };
  if (p === "/api/nav") return send({ projects: projects.map(pr => ({ ...pr, cards: cards[pr.work_id] })) });
  if (p === "/api/nav/version") {
    return send({ version: version(), by_work: Object.fromEntries(projects.map(pr => [pr.work_id, fingerprint(pr.work_id)])) });
  }
  if (p === "/api/nav/children") {
    const wid = url.searchParams.get("work_id") || "";
    const parent = url.searchParams.get("parent") || "";
    const list = cards[wid] || [];
    const root = (projects.find(x => x.work_id === wid) || {}).work_card_id || "";
    // S1 实测语义：parent="" ＝ 根卡直系（parent===work_card_id）
    const out = list.filter(c => parent === "" ? c.parent === root : c.parent === parent);
    return send({ work_id: wid, parent, cards: out });
  }
  if (p === "/__test/retitle" && req.method === "POST") {
    const c = (cards.work_m1.find(x => x.card_id === "card_m_a"));
    c.title = "假卡甲-A（标题已被改动 " + Date.now() + "）";
    projects[0].title = "假项目甲（项目标题已被改动）";
    versionSeq++;
    return send({ ok: true, version: version() });
  }
  // [S3/R-21 观察用] 幽灵卡注册：{wid,cid,title,parent} → 快照永不返回、getCard 仍应答
  if (p === "/__test/ghostcard" && req.method === "POST") {
    let body = "";
    req.on("data", (ch) => { body += ch; });
    req.on("end", () => {
      try {
        const o = JSON.parse(body || "{}");
        if (!o.cid) return send({ ok: false, error: "cid required" }, 400);
        ghosts.set(o.cid, { card_id: o.cid, title: o.title || "幽灵卡", parent: o.parent || "", card_id_full: o.cid });
        return send({ ok: true });
      } catch (e) { return send({ ok: false, error: String(e) }, 400); }
    });
    return;
  }
  // open()/renderChat/refreshBadge/画布 等页面依赖端点：结构兜底
  const mCard = p.match(/^\/api\/works\/(work_m\d)\/cards\/(card_\w+)$/);
  if (mCard) {
    const ghost = ghosts.get(mCard[2]);
    return send({ meta: ghost || { card_id: mCard[2], title: "假卡", parent: "", card_id_full: mCard[2] }, body: {} });
  }
  return send({});
}

/* ---------- 静态 + 模板 ---------- */
let currentWork = "work_m1";   // [S3] /work/<wid> 动态定 WORK_ID（跨项目观察用；缺省 work_m1 不变）
function serveStatic(pathname, res) {
  let rel = decodeURIComponent(pathname);
  if (rel === "/") rel = "/templates/work.html";
  if (rel.startsWith("/work/")) {
    const w = rel.split("/")[2] || "";
    if (/^work_m\d$/.test(w)) currentWork = w;
    rel = "/templates/work.html";
  }
  const fp = normalize(join(ROOT, rel));
  if (!fp.startsWith(normalize(ROOT)) || !existsSync(fp)) { res.writeHead(404); return res.end("nf"); }
  const body = readFileSync(fp);
  const type = MIME[extname(fp)] || "application/octet-stream";
  res.writeHead(200, { "Content-Type": type + "; charset=utf-8", "Cache-Control": "no-store" });
  if (rel.endsWith("work.html")) {
    return res.end(body.toString("utf-8").replaceAll("{{WORK_ID}}", currentWork));
  }
  res.end(body);
}

http.createServer((req, res) => {
  const url = new URL(req.url, "http://x");
  if (url.pathname.startsWith("/__test/")) return api(req, res, url);
  if (url.pathname.startsWith("/api/")) return api(req, res, url);
  return serveStatic(url.pathname, res);
}).listen(PORT, "127.0.0.1", () => console.log("mock ready http://127.0.0.1:" + PORT + "/work/work_m1"));
