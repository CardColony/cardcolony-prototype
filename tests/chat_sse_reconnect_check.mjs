/* node --experimental-eventsource tests/chat_sse_reconnect_check.mjs
 * 原生 EventSource 走本地 HTTP。用固定 runner 快照复验实际刷新、合并和重连回调。
 * 夹具没有周期轮询，重连阶段的状态读只能由 onopen 发起。 */
import assert from "node:assert/strict";
import { once } from "node:events";
import fs from "node:fs";
import { createServer } from "node:http";
import { setTimeout as sleep } from "node:timers/promises";
import vm from "node:vm";
import { createNavStateStore, mergeCanvasRunning } from "../static/esm/state_layers.js";

function source(file, start, end) {
  const text = fs.readFileSync(new URL(`../static/${file}`, import.meta.url), "utf8");
  const from = text.indexOf(start);
  const to = text.indexOf(end, from + start.length);
  assert(from >= 0 && to > from, `找不到 ${file} 中的测试入口`);
  return text.slice(from, to);
}

async function until(check, label) {
  const deadline = performance.now() + 10000;
  while (!check()) {
    assert(performance.now() < deadline, label);
    await sleep(40);
  }
}

assert.equal(typeof EventSource, "function", "请加 --experimental-eventsource 启动 Node");
const navState = createNavStateStore();
const canvasState = { async: { running: new Map() } };
const clients = [];
const streams = new Set();
const events = [];
let phase = "initial";
let holdPulse = false;
let stalePulse;
const a = { work_id: "w", card_id: "A", _runner: 0 };
const b = { work_id: "w", card_id: "B", _runner: 1 };
let running = [a, b];

const server = createServer((req, res) => {
  const pathname = new URL(req.url, "http://localhost").pathname;
  if (pathname === "/api/works/w/events") {
    if (phase === "offline") { res.destroy(); return; }
    streams.add(res);
    res.on("close", () => streams.delete(res));
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-store" });
    res.write("retry: 100\n\n: connected\n\n");
    return;
  }
  events.push({ phase, request: pathname });
  const body = JSON.stringify({ running, recent: [], coverage: { ok: 2, total: 2, missing: [] } });
  res.setHeader("Content-Type", "application/json");
  if (pathname.endsWith("/pulse") && holdPulse) {
    holdPulse = false;
    stalePulse = () => {
      stalePulse = undefined;
      res.end(body);
    };
    return;
  }
  res.end(body);
});

server.listen(0, "127.0.0.1");
await once(server, "listening");
const base = `http://127.0.0.1:${server.address().port}`;
const get = async (path) => (await fetch(base + path)).json();

function eventSourceFor(surface) {
  return class extends EventSource {
    constructor(url) {
      super(base + url);
      clients.push(this);
      for (const kind of ["open", "error"]) {
        this.addEventListener(kind, () => events.push({ phase, surface, kind }));
      }
    }
  };
}

const nav = vm.createContext({
  WORK_ID: "w", EventSource: eventSourceFor("nav"), window: {},
  API: {}, api: { getNavRunning: () => get("/api/nav/running") }, store: navState, render() {},
});
vm.runInContext(source("esm/nav_render.js", "  async function probeRunning()", "  /* ---------- 渲染入口"), nav);
nav.navSystem = {
  probeRunning: nav.probeRunning,
  probeVersion: () => get("/api/nav/version"),
  refreshBadge: () => get("/api/works/w/card-messages/unread"),
};
vm.runInContext(source("workspace.js", "  let _workEs = null;", "  // ==================== [T17-F4]"), nav);

const canvas = vm.createContext({
  WORK_ID: "w", EventSource: eventSourceFor("canvas"), window: {}, document: { hidden: false },
  _workEs: null, _workEsHealthy: false, _workEsDropped: false,
  pulseRequest: 0, pulseApplied: 0, state: canvasState, mergeCanvasRunning,
  API: { getPulse: () => get("/api/works/w/pulse") },
  reloadCanvas: () => get("/api/works/w/canvas"), applyLiveStates() {}, renderActivity() {},
});
vm.runInContext(source("canvas.js", "  async function pollPulse()", "  // ── 项目动态抽屉"), canvas);
vm.runInContext(source("canvas.js", "  // ── 订阅工作级事件流", "\n  subscribeWorkEvents();"), canvas);

const canvasCards = () => [...canvasState.async.running.keys()].sort();
const navCards = () => ["A", "B"].filter(id => navState.temp.isRunning("w", id));
const seen = (p, kind, surface) => events.some(e => e.phase === p && e.kind === kind && e.surface === surface);
let staleRead;
try {
  await Promise.all([nav.probeRunning(), canvas.pollPulse()]);
  assert.deepEqual(navCards(), ["A", "B"]);
  assert.deepEqual(canvasCards(), ["A", "B"]);
  vm.runInContext("subscribeWorkEvents()", nav);
  vm.runInContext("subscribeWorkEvents()", canvas);
  await until(() => seen("initial", "open", "nav") && seen("initial", "open", "canvas"), "初次连接超时");

  holdPulse = true;
  staleRead = canvas.pollPulse();
  await until(() => stalePulse, "旧 pulse 未进入在途状态");
  phase = "offline";
  for (const stream of streams) stream.destroy();
  await until(() => seen("offline", "error", "nav") && seen("offline", "error", "canvas"), "两端未观测到断线");
  running = [b];
  assert.deepEqual(navCards(), ["A", "B"]);
  assert.deepEqual(canvasCards(), ["A", "B"]);

  phase = "reconnect";
  await until(() => seen("reconnect", "open", "nav") && seen("reconnect", "open", "canvas"), "重连未打开");
  await until(() => navCards().join() === "B" && canvasCards().join() === "B", "重连后两端未清除 A");
  const reconnectReads = events.filter(e => e.phase === "reconnect" && e.request).map(e => e.request);
  assert.deepEqual(reconnectReads.toSorted(), [
    "/api/nav/running", "/api/nav/version", "/api/works/w/card-messages/unread",
    "/api/works/w/canvas", "/api/works/w/pulse",
  ].toSorted());

  stalePulse();
  await staleRead;
  assert.deepEqual(canvasCards(), ["B"], "旧 pulse 回包复活了已完成的 A");
  const result = { nav: navCards(), canvas: canvasCards(), reconnectReads, stalePulseIgnored: true, events };
  fs.mkdirSync("logs", { recursive: true });
  const file = `logs/chat-sse-reconnect-${Date.now().toString(36)}.json`;
  fs.writeFileSync(file, JSON.stringify(result, null, 2));
  console.log(`PASS ${file}`, JSON.stringify({ nav: result.nav, canvas: result.canvas, reconnectReads }));
} finally {
  for (const client of clients) client.close();
  stalePulse?.();
  server.closeAllConnections();
  await staleRead?.catch(() => {});
  await new Promise(resolve => server.close(resolve));
}
