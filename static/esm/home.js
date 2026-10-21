/* esm/home.js —— [首页两态 2026-10-15] 首页入口（旧项目列表页退役后的继任者）。
 *
 * 入口判定（方案定稿 v2，全在前端——后端读不到 localStorage）：
 *   1. localStorage nav.recent.v1 有最近记录 → 跳上次停留的对话页 /work/{wid}/card/{cid}
 *   2. 无最近记录但 /api/works 有项目     → 跳最近活跃 work 的对话页
 *   3. 全空                                → 停在空态首页（左栏展开空列表 +【＋新项目】、画布中央【创建】）
 *
 * 为什么用 ES module 而不是旧 IIFE 薄壳：静态 import 保证 API 在本模块顶层执行前
 * 就绪。此前那个「薄壳异步 import 晚于页面顶层 load()」的时序缺口
 * （见 git 7d09280 修复记录）在静态 import 形态下不存在，无需等待轮询。
 */
import { API, toast } from "./api.js?v=43";
import { storageGet } from "./state_layers.js?v=32";
import { NAV_RECENT_KEY } from "./nav_find.js?v=31";

const newBtn = document.getElementById("homeNewBtn");
const createBtn = document.getElementById("homeCreateBtn");
const createBox = document.querySelector(".home-create-box");
const navEmpty = document.getElementById("homeNavEmpty");
const searchInput = document.getElementById("homeSearchInput");

/* ---------- 新建（两处入口复用同一条链路） ----------
   [惰性新建 2026-10-17] 不再 POST /api/works 真落库——改为跳到**草稿项目页**
   /work/draft（一次页面加载，草稿态由 work.html 前端接管）：点新建即出空画布，
   零落库；此后任何产生数据的动作（发对话/改名/建卡/规则卡保存/状态变更）
   才真正 POST 落库，落库前离开/刷新 → 草稿蒸发、磁盘零残留。
   端点 /api/works 契约零改动，只是调用时机后移（草稿落库序列在画布/对话页内）。 */
let creating = false;
function createProject() {
  if (creating) return;
  creating = true;
  [newBtn, createBtn].forEach(b => { if (b) b.disabled = true; });
  location.href = "/work/draft";
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
  /* 1. 有最近访问 → 上次停留的对话页（[阶段2 A2.4] 新地址形态 /work/{wid}/card/{cid}）。 */
  const recent = readRecent();
  if (recent) {
    location.replace("/work/" + recent.wid + "/card/" + recent.cid);
    return;
  }

  /* 2. 无最近记录但有项目 → 最近活跃 work 的对话页。
     /api/works 已按 last_active_at 降序（work_store.list_works），首项即最近活跃。
     [阶段2 A2.4] 根卡地址走新形态 /work/{wid}/card/{rootCid}；根卡 id 缺失时退回纯
     /work/{wid}（停在画布页，至少进得去）。 */
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
    location.replace("/work/" + w.work_id + (rootCid ? "/card/" + rootCid : ""));
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
