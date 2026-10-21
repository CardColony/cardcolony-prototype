
import subprocess, os, sys, time, json
WT = r"D:\AI_station\推演agent\.worktrees\5713"
EV = os.path.join(WT, "_flake_ev")
PY = r"D:\AI_station\推演agent\.venv\Scripts\python.exe"
N = int(sys.argv[1]) if len(sys.argv)>1 else 5
res = []
for i in range(1, N+1):
    xml = os.path.join(EV, "junit_run%d.xml" % i)
    log = os.path.join(EV, "run%d.log" % i)
    t0 = time.time()
    with open(log, "wb") as f:
        p = subprocess.run([PY, "-m", "pytest", "tests", "-q", "--junitxml", xml],
                           cwd=WT, stdout=f, stderr=subprocess.STDOUT)
    dt = time.time()-t0
    res.append({"run": i, "rc": p.returncode, "secs": round(dt,1)})
    with open(os.path.join(EV, "runs.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
