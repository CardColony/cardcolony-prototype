/* 展开全部后检查卡片重叠 / 越界（5707） */
import { spawn } from "node:child_process";
import os from "node:os"; import path from "node:path"; import fs from "node:fs";
const BASE = process.argv[2] || "http://127.0.0.1:5707";
const WID = process.argv[3] || "real_work_002";
const PORT = 9720 + Math.floor(Math.random() * 60);
const CHROME = path.join(os.homedir(), "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe");
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const child = spawn(CHROME, ["--remote-debugging-port="+PORT,"--headless=new","--disable-gpu","--no-sandbox","--hide-scrollbars","--window-size=1600,1000","--user-data-dir="+path.join(os.tmpdir(),"ov"+Date.now()),"about:blank"], {stdio:"ignore"});
let tr=null; for(let i=0;i<80&&!tr;i++){try{const r=await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`,{method:"PUT"}); if(r.ok) tr=await r.json();}catch{} if(!tr) await sleep(200);}
const ws=new WebSocket(tr.webSocketDebuggerUrl); let id=0; const pend=new Map(); const errs=[];
const send=(m,p={})=>new Promise(res=>{const i=++id; pend.set(i,res); ws.send(JSON.stringify({id:i,method:m,params:p}));});
await new Promise(r=>{ws.onopen=r;});
ws.onmessage=(e)=>{const m=JSON.parse(e.data); if(m.id&&pend.has(m.id)){pend.get(m.id)(m); pend.delete(m.id);} else {
  if(m.method==="Runtime.consoleAPICalled" && m.params.type!=="log" && m.params.type!=="info") errs.push(m.params.type+": "+m.params.args.map(a=>a.value??"").join(" ").slice(0,150));
}};
await send("Page.enable"); await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride",{width:1600,height:1000,deviceScaleFactor:1,mobile:false});
const ev=async(x)=>{const r=await send("Runtime.evaluate",{expression:x,returnByValue:true,awaitPromise:true}); return r.result&&r.result.result?r.result.result.value:undefined;};

await send("Page.navigate",{url:`${BASE}/work/${WID}`}); await sleep(3800);
// 展开所有有子卡的卡（反复点徽章，直到没有新的可展开）
for (let round=0; round<6; round++) {
  const n = await ev(`(()=>{const bs=[...document.querySelectorAll('.kid-badge')].filter(b=>!b.classList.contains('is-open')); if(!bs.length) return 0; bs.forEach(b=>b.click()); return bs.length;})()`);
  if (!n) break;
  await sleep(900);
}
// 整幅适配后再量（否则量的是"簇跟随"后的局部视野，越界判断无意义）
await send("Input.dispatchKeyEvent", { type: "keyDown", key: "0", code: "Digit0", modifiers: 2, windowsVirtualKeyCode: 48 });
await send("Input.dispatchKeyEvent", { type: "keyUp", key: "0", code: "Digit0", modifiers: 2, windowsVirtualKeyCode: 48 });
await sleep(600);
const total = await ev("document.querySelectorAll('.card-node').length");

const rep = JSON.parse(await ev(`(()=>{
  const nodes=[...document.querySelectorAll('.card-node')];
  const rects=nodes.map(n=>{const r=n.getBoundingClientRect(); return {id:n.dataset.id, x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height)};});
  const overlaps=[];
  for(let i=0;i<rects.length;i++) for(let j=i+1;j<rects.length;j++){
    const a=rects[i], b=rects[j];
    const ox=Math.min(a.x+a.w,b.x+b.w)-Math.max(a.x,b.x);
    const oy=Math.min(a.y+a.h,b.y+b.h)-Math.max(a.y,b.y);
    if(ox>2&&oy>2) overlaps.push({a:a.id,b:b.id,ox,oy});
  }
  const vp={w:innerWidth,h:innerHeight};
  const outside=rects.filter(r=>r.x+r.w<0||r.y+r.h<0||r.x>vp.w||r.y>vp.h).map(r=>r.id);
  return JSON.stringify({count:rects.length, overlaps:overlaps.slice(0,12), overlapCount:overlaps.length, outside, zoom:(document.getElementById('zoomDisplay')||{}).textContent});
})()`));
const shot = await send("Page.captureScreenshot", { format: "png" });
fs.writeFileSync("_shots/overlap.png", Buffer.from(shot.result.data, "base64"));
console.log(JSON.stringify({wid:WID, rendered:total, ...rep, errs:errs.slice(0,3)}, null, 1));
ws.close(); child.kill(); process.exit(0);
