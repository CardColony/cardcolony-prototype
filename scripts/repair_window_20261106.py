# -*- coding: utf-8 -*-
"""窗口修复：22:09→23:05 清零事故丢写的定向回补（events/*.jsonl 是磁盘真相源）。

三件事：
1) 重建 card_436 / card_437 的 cards 主行（title/mission 从父卡 create_worker_card 调用里抠）
2) 重放窗口内所有 card_block_edited → 把 status_block/state/requirements_draft 等卡字段推回末态
3) work_events 全量回灌（INSERT OR IGNORE，PK=work_id+card_id+seq）
"""
import json, sqlite3, glob, os, sys, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf8", errors="replace")

WID = "work_027"
DB = r"D:\AI_station\推演agent\works_data\work_027\cards.db"
EVDIR = rf"D:\AI_station\推演agent\works\{WID}\events"
LO, HI = "2026-11-06T22:09", "2026-11-06T23:59"   # 窗口：导出点→服务被杀
BLOCK_COLS = {  # card_block_edited 的 block 名 → cards 列白名单
    "state", "status_block", "requirements", "requirements_draft",
    "mission", "title", "goal", "coder_final", "review_state",
}

con = sqlite3.connect(DB)
con.execute("PRAGMA busy_timeout=15000")
cols = {r[1] for r in con.execute("pragma table_info(cards)")}

# ---------- 1) 重建 card_436 / card_437 ----------
rebuilt = []
for cid, parent, born in [("card_436", "card_165", "2026-11-06T22:46:24.248554+08:00"),
                          ("card_437", "card_156", "2026-11-06T22:48:16.439549+08:00")]:
    if con.execute("select 1 from cards where card_id=?", (cid,)).fetchone():
        print(f"[rebuild] {cid} 已存在，跳过"); continue
    title = mission = None
    with open(rf"{EVDIR}\{parent}.jsonl", encoding="utf8") as f:
        for line in f:
            d = json.loads(line)
            if d["type"] == "tool_call" and d.get("data", {}).get("tool") == "create_worker_card":
                args = d["data"].get("arguments") or {}
                # 按建卡时间戳精确匹配（父卡可能连建多卡）
                if args.get("title") and d.get("ts", "")[:19] == born[:19]:
                    title, mission = args["title"], args.get("task", "")
    if not title:
        print(f"[rebuild] {cid} 找不到父卡建卡参数，人工补"); continue
    last_ts = ""
    with open(rf"{EVDIR}\{cid}.jsonl", encoding="utf8") as f:
        for line in f:
            try: last_ts = json.loads(line)["ts"]
            except Exception: pass
    row = {c: None for c in cols}
    row.update({
        "card_id": cid, "work_id": WID, "title": title, "source": "模型生成",
        "card_type": "worker", "parent": parent, "state": "active",
        "tags": "[]", "links": "[]", "relations": "[]",
        "session_ref": f"sessions/session_{cid.split('_')[1]}.md",
        "mission": mission, "created_at": born, "updated_at": last_ts or born,
        "pinned": 0, "auto_mode": 0, "mode": "manual",
    })
    con.execute(
        f"insert into cards ({','.join(row)}) values ({','.join('?'*len(row))})",
        list(row.values()))
    rebuilt.append((cid, title[:60], born, last_ts))
con.commit()
for r in rebuilt: print("[rebuild]", r)

# ---------- 2) 重放 card_block_edited ----------
applied = {}
for path in glob.glob(rf"{EVDIR}\card_*.jsonl"):
    base = os.path.basename(path)[:-6]
    if base.startswith(("shadow_", "_")): continue
    edits = []
    with open(path, encoding="utf8", errors="replace") as f:
        for line in f:
            if '"card_block_edited"' not in line: continue
            try: d = json.loads(line)
            except Exception: continue
            if d["type"] == "card_block_edited" and LO <= d.get("ts", "") <= HI:
                dd = d.get("data") or {}
                blk = dd.get("block")
                if blk in BLOCK_COLS and blk in cols:
                    edits.append((d["ts"], blk, dd.get("new", "")))
    if not edits: continue
    edits.sort()
    sets, vals = [], []
    for ts, blk, new in edits:
        sets.append(f"{blk}=?"); vals.append(new)
    vals += [edits[-1][0], base]
    con.execute(f"update cards set {','.join(sets)}, updated_at=? where card_id=?", vals)
    applied[base] = (len(edits), {b for _, b, _ in edits})
con.commit()
print(f"[replay] 重放了 {len(applied)} 张卡的块编辑:")
for c, (n, bs) in sorted(applied.items()):
    st = con.execute("select state from cards where card_id=?", (c,)).fetchone()
    print(f"   {c}: {n} 次 → {sorted(bs)} | state={st[0] if st else '??'}")

# ---------- 3) work_events 全量回灌 ----------
total_new = total_seen = 0
for path in glob.glob(rf"{EVDIR}\*.jsonl"):
    card = os.path.basename(path)[:-6]
    batch = []
    with open(path, encoding="utf8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try: d = json.loads(line)
            except Exception: continue
            batch.append((WID, card, d.get("seq", 0), d.get("type", ""),
                          d.get("run_id", ""), d.get("round", 0),
                          str(d.get("ts", "")), json.dumps(d.get("data") or {}, ensure_ascii=False)))
            if len(batch) >= 2000:
                cur = con.executemany(
                    "insert or ignore into work_events(work_id,card_id,seq,type,run_id,round,ts,data) values(?,?,?,?,?,?,?,?)", batch)
                con.commit(); total_new += cur.rowcount; total_seen += len(batch); batch = []
    if batch:
        cur = con.executemany(
            "insert or ignore into work_events(work_id,card_id,seq,type,run_id,round,ts,data) values(?,?,?,?,?,?,?,?)", batch)
        con.commit(); total_new += cur.rowcount; total_seen += len(batch)
print(f"[events] 扫描 {total_seen} 行，新增 {total_new} 行")
print("work_events 总数:", con.execute("select count(*) from work_events").fetchone()[0])
con.close()
