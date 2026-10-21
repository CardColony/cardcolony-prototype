# -*- coding: utf-8 -*-
"""质量域复现实验：噪声闸的「隔离单跑」档（每用例独立进程 ×3）。只跑测试，不改任何代码/断言。"""
import subprocess, sys, os, json, re
BASE = os.path.dirname(os.path.abspath(__file__))
PY = r"D:\AI_station\推演agent\.venv\Scripts\python.exe"
TARGETS = [
    "tests/test_board.py::test_apply_revision_dual_ledger_and_dirty_flush",
    "tests/test_card_msg.py::test_k3_live_a_to_b_invokes_receiver",
    "tests/test_coder_card.py::test_create_coder_card_builds_and_starts",
    "tests/test_coder_card.py::test_report_done_calls_owner_and_coder_stays_silent",
]
REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
out = {}
for t in TARGETS:
    seq = []
    for i in range(REPS):
        p = subprocess.run([PY, "-m", "pytest", t, "-q", "-p", "no:cacheprovider"],
                           cwd=BASE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        tail = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""
        m = re.search(r"(\d+) (passed|failed|error)", tail)
        seq.append(("FAIL" if (p.returncode != 0) else "pass") + " | " + tail[-60:])
    out[t] = seq
    print(t, seq, flush=True)
json.dump(out, open(os.path.join(BASE, "_c16_iso.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print("DONE", flush=True)