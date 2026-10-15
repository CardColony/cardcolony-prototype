/* esm/home.js —— [首页两态 2026-10-15] 首页入口（旧项目列表页退役后的继任者）。
 *
 * 入口判定（方案定稿 v2，全在前端——后端读不到 localStorage）：
 *   1. localStorage nav.recent.v1 有最近记录 → 跳上次停留的对话页 /work/{wid}#{wid}/{cid}
 *   2. 无最近记录但 /api/works 有项目     → 跳最近活跃 work 的对话页
 *   3. 全空                                → 停在空态首页（左栏展开空列表 +【＋新项目】、画布中央【创建】）
 *
 * 为什么用 ES module 而不是旧 IIFE 薄壳：静态 import 保证 API 在本模块顶层执行前
 * 就绪。此前那个「薄壳异步 import 晚于页面顶层 load()」的时序缺口
 * （见 git 7d09280 修复记录）在静态 import 形态下不存在，无需等待轮询。
 */
import { API, toast } from "./api.js?v=30";
import { storageGet } from "./state_layers.js?v=30";
import { NAV_RECENT_KEY } from "./nav_find.js?v=30";

const newBtn = document.getElementById("homeNewBtn");
const createBtn = document.getElementById("homeCreateBtn");
const createBox = document.querySelector(".home-create-box");
const navEmpty = document.getElementById("homeNavEmpty");
const searchInput = document.getElementById("homeSearchInput");

/* ---------- 新建（两处入口复用同一条链路） ----------
   端点 /api/works 零改动：沿用旧列表页既有的 POST → 跳新 work 语义。
   标题用固定默认名而非弹窗：空态首页的定位是「一键开始」，多一次输入框
   会挡住主路径；建完进画布后仍可在左栏改名。 */
let creating = false;
async function createProject() {
  if (creating) return;
  creating = true;
  [newBtn, createBtn].forEach(b => { if (b) b.disabled = true; });
  try {
    const w = await API.createWork("新项目");
    location.href = "/work/" + w.work_id + "#";
  } catch (e) {
    /* 失败原因已由 api.js 的 call() 弹 toast */
    creating = false;
    [newBtn, createBtn].forEach(b => { if (b) b.disabled = false; });
  }
}
if (newBtn) newBtn.onclick = createProject;
if (createBtn) createBtn.onclick = createProject;

/* 空态搜索框：当前无内容可搜（空态的前提就是无项目），聚焦不做事、
   留空实现以免浏览器聚焦无响应控件——左栏结构与工作页同构，语义位置在此。 */
if (searchInput) {
  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") e.preventDefault();   /* 空态无结果，不触发任何跳转 */
  });
}

/* ---------- 最近访问读取（与工作页 R-25 同一份数据，键与解析口径共用 nav_find） ---------- */
function readRecent() {
  try {
    const raw = storageGet(localStorage, NAV_RECENT_KEY, "[]");
    const v = JSON.parse(raw || "[]");
    if (!Array.isArray(v)) return null;
    const hit = v.find(e => e && e.wid && e.cid);
    return hit || null;
  } catch (e) {
    return null;   /* 坏 JSON 容错：与 nav_find.createRecent 同一口径，当作无记录 */
  }
}

/* ---------- 入口判定 ---------- */
/* 空态的两块内容（左栏提示、画布中央【创建】）在 home.html 里初始 display:none——
   判定未落定前先藏着，否则"其实要跳走"的那些访问会先闪一下空态再跳转。
   判定为空态时点亮；判定为跳转时页面直接换掉，用户根本看不到它们。 */
function showEmptyState() {
  if (navEmpty) navEmpty.style.display = "";
  if (createBox) createBox.style.display = "";
}

async function boot() {
  /* 1. 有最近访问 → 上次停留的对话页。hash 用复合键 #wid/cid（state_layers
     serializeHash 的口径）：工作页 restoreFromHash 按此解析并 open() 那张卡，
     同时进入 chat 视图。若只写 #cid 会回落到「当前页 WORK_ID」——同页时也能开卡，
     但跨 work 时会开错，故一律写复合键。 */
  const recent = readRecent();
  if (recent) {
    location.replace("/work/" + recent.wid + "#" + recent.wid + "/" + recent.cid);
    return;
  }

  /* 2. 无最近记录但有项目 → 最近活跃 work 的对话页。
     /api/works 已按 last_active_at 降序（work_store.list_works），首项即最近活跃。
     注意：无 hash 时工作页停在画布态而非对话态；这里补上根卡 hash，
     让 restoreFromHash 打开根卡并切到 chat 视图——对齐「落在对话页」的验收口径。
     根卡 id 从列表项的 work_card_id 取；该字段缺失时退回纯 /work/{wid}（至少进得去）。 */
  let works = [];
  try {
    const data = await API.listWorks();
    works = (data && data.works) || [];
  } catch (e) {
    /* 拉不到项目列表时不猜：按空态呈现，用户仍可【创建】起步 */
    works = [];
  }
  if (works.length) {
    const w = works[0];
    const rootCid = w.work_card_id || "";
    location.replace("/work/" + w.work_id + (rootCid ? "#" + w.work_id + "/" + rootCid : ""));
    return;
  }

  /* 3. 全新无任何项目 → 空态首页 */
  showEmptyState();
}

boot().catch(() => {
  /* 判定链路自身异常也不把用户堵在一片空白上：退到空态，【创建】仍可用 */
  showEmptyState();
  toast("首页初始化异常，已按空态呈现", "warn");
});
