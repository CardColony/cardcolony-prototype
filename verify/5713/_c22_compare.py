# -*- coding: utf-8 -*-
"""card_022 · 新旧基线对比：条数 + nodeid 差异清单（本次最有价值的产出）。

旧基线 = 用**隔壁项目 venv** 采的那份（已备份为 baseline_known_red.json.bak-badvenv-<ts>）；
新基线 = 用项目自己的 .venv 重采的 baseline_known_red.json。

用法：python verify/5713/_c22_compare.py [旧基线路径] [新基线路径]
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def fmt(sig: dict) -> str:
    return "%-18s %-38s %s" % (sig.get("exc_type", "?") or "-",
                               sig.get("loc", "?") or "-",
                               (sig.get("snippet", "") or "")[:90])


def main(argv) -> int:
    old_p = Path(argv[0]) if len(argv) > 0 else None
    new_p = Path(argv[1]) if len(argv) > 1 else HERE / "baseline_known_red.json"
    if old_p is None:
        cands = sorted(HERE.glob("baseline_known_red.json.bak-*"))
        if not cands:
            print("[cmp] 找不到旧基线备份")
            return 2
        old_p = cands[-1]
    old, new = load(old_p), load(new_p)
    o = {k["nodeid"]: k for k in old.get("known_red", [])}
    n = {k["nodeid"]: k for k in new.get("known_red", [])}

    print("[cmp] 旧基线：%s" % old_p.name)
    print("      commit=%s rounds=%s counts=%s" % (str(old.get("commit"))[:12],
                                                 old.get("rounds"), old.get("counts")))
    print("      cmd[0]=%s" % (old.get("command_scope", {}).get("cmd") or ["?"])[0])
    print("[cmp] 新基线：%s" % new_p.name)
    print("      commit=%s rounds=%s counts=%s" % (str(new.get("commit"))[:12],
                                                 new.get("rounds"), new.get("counts")))
    print("      cmd[0]=%s" % (new.get("command_scope", {}).get("cmd") or ["?"])[0])
    print()
    print("[cmp] known-red 条数：旧 %d → 新 %d" % (len(o), len(n)))
    only_old = sorted(set(o) - set(n))
    only_new = sorted(set(n) - set(o))
    both = sorted(set(o) & set(n))
    sig_changed = [k for k in both if o[k]["signature"].get("sig") != n[k]["signature"].get("sig")]
    print("[cmp] 共同保留 %d｜只在旧基线 %d｜只在新基线 %d｜signature 变了 %d"
          % (len(both), len(only_old), len(only_new), len(sig_changed)))
    print()
    if only_old:
        print("[cmp] ── 只在旧基线里红（新环境下已不红）%d 条" % len(only_old))
        for k in only_old:
            print("  - %s\n      旧：%s" % (k, fmt(o[k]["signature"])))
    if only_new:
        print("[cmp] ── 只在新基线里红（旧环境下不红／没红够轮次）%d 条" % len(only_new))
        for k in only_new:
            print("  + %s\n      新：%s" % (k, fmt(n[k]["signature"])))
    if sig_changed:
        print("[cmp] ── 两边都红但失败面变了 %d 条（nodeid 相同、签名不同）" % len(sig_changed))
        for k in sig_changed:
            print("  ~ %s\n      旧：%s\n      新：%s" % (k, fmt(o[k]["signature"]), fmt(n[k]["signature"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))