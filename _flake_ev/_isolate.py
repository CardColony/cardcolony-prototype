
import subprocess, os, sys, json, time
WT = r"D:\AI_station\推演agent\.worktrees\5713"
EV = os.path.join(WT, "_flake_ev")
PY = r"D:\AI_station\推演agent\.venv\Scripts\python.exe"
ids = json.load(open(os.path.join(EV,"fail_ids.json"), encoding="utf-8"))
res = {}
for i, nid in enumerate(ids, 1):
    t0 = time.time()
    p = subprocess.run([PY, "-m", "pytest", nid, "-q", "--no-header"],
                       cwd=WT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    status = "PASS" if p.returncode == 0 else ("FAIL" if p.returncode == 1 else "ERR%d" % p.returncode)
    tail = [l for l in out.splitlines() if l.strip().startswith("E ")]
    res[nid] = {"status": status, "secs": round(time.time()-t0,1), "tail": (tail[0][:200] if tail else "")}
    json.dump(res, open(os.path.join(EV,"isolation.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(i, status, nid, flush=True)
