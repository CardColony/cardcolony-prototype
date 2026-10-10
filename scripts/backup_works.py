# -*- coding: utf-8 -*-
"""works 冷备份脚本（迭代一·A9，部署窗口第 2 步）。

职责：把 works/ 树整树冷拷贝到目标目录 ＋ md5 逐文件清单 ＋ 抽验比对。
铁律：
- 只读源树（零写入、零移动、零删除）；
- md5 清单全量落盘（备份目录内 manifest.json），抽验比对结果进 stdout 与清单；
- 抽验不一致＝备份失败（非零退出码，部署窗口必须停下）。

用法：
    python scripts/backup_works.py --dest <备份目录> [--sample 20]

（演练/生产共用：--dest 必须显式给出，不做缺省写入。）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKS = REPO / "works"


def md5_of(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True, help="备份目标目录（必须显式给出）")
    ap.add_argument("--sample", type=int, default=20, help="md5 抽验文件数（默认 20）")
    args = ap.parse_args()

    dest = Path(args.dest).resolve()
    if dest.exists() and any(dest.iterdir()):
        print(f"[backup] 目标目录非空，拒绝覆盖: {dest}", file=sys.stderr)
        return 2
    if not WORKS.exists():
        print(f"[backup] 源目录不存在: {WORKS}", file=sys.stderr)
        return 2

    t0 = time.time()
    shutil.copytree(WORKS, dest, ignore=shutil.ignore_patterns("*.tmp"))
    print(f"[backup] 冷拷贝完成 -> {dest}（{time.time() - t0:.1f}s）")

    # 全量 md5 清单
    manifest: dict[str, dict[str, object]] = {}
    files = sorted(p for p in dest.rglob("*") if p.is_file())
    for p in files:
        rel = p.relative_to(dest).as_posix()
        src = WORKS / rel
        manifest[rel] = {
            "bytes": p.stat().st_size,
            "md5": md5_of(p),
            "src_md5": md5_of(src) if src.exists() else None,
        }
    match = sum(1 for v in manifest.values() if v["md5"] == v["src_md5"])
    print(f"[backup] 清单文件数={len(manifest)} 源目标md5全对比一致={match}/{len(manifest)}")

    # 抽验（等距抽样再算一遍，防「同进程缓存侥幸」）
    sample = files[:: max(1, len(files) // max(1, args.sample))][: args.sample]
    bad = []
    for p in sample:
        rel = p.relative_to(dest).as_posix()
        if manifest[rel]["md5"] != md5_of(p):
            bad.append(rel)
    print(f"[backup] 抽验 {len(sample)} 个，不一致={len(bad)} {bad[:5]}")

    (dest / "manifest.json").write_text(
        json.dumps({"created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "files": len(manifest), "md5_match": match,
                    "sample": len(sample), "sample_bad": bad,
                    "entries": manifest}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"[backup] manifest.json 已落盘: {dest / 'manifest.json'}")
    return 1 if (bad or match != len(manifest)) else 0


if __name__ == "__main__":
    sys.exit(main())
