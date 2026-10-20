# -*- coding: utf-8 -*-
"""复核证据文件里没有真实 .env 的明文值（进版本控制前的硬闸）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.api import credential_guard as cg  # noqa: E402

REAL_ENV = Path(r"D:\AI_station\推演agent\.env")
vals = sorted(cg._harvest_file(REAL_ENV), key=len, reverse=True) if REAL_ENV.is_file() else []
print("real_env_values:", len(vals))

targets = [
    "verify/5713/evidence_cred_regression.txt",
    "verify/5713/evidence_cred_coverage_oldnew.txt",
    "verify/5713/evidence_noise_before.json",
    "verify/5713/evidence_noise_after.json",
]
bad = 0
for f in targets:
    p = Path(f)
    if not p.exists():
        print(f, "MISSING")
        continue
    text = p.read_text(encoding="utf-8")
    leaks = [v for v in vals if v in text]
    bad += len(leaks)
    print(f"{f}: leaks={len(leaks)} masked_markers={text.count('<masked len=')} "
          f"probe_markers={text.count('<probe-key')}")
print("VERDICT:", "CLEAN" if bad == 0 else f"LEAKED {bad}")