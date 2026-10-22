# -*- coding: utf-8 -*-
"""验证修复：用**生产库真实卡面**确认根卡能给 role 建线、role 能发消息。"""
import io
import json
import os
import sqlite3
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\AI_station\推演agent")

from src.permissions import policy

ok = True


def check(label, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label} {extra}")


def load_cards(work_id):
    db = os.path.join(r"D:\AI_station\推演agent\works", work_id, "cards.db")
    c = sqlite3.connect(f"file:{db.replace(chr(92),'/')}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    out = {}
    for r in c.execute("SELECT card_id, title, card_type, parent, perms, "
                       "perms_version FROM cards"):
        d = dict(r)
        try:
            d["perms"] = json.loads(d.get("perms") or "[]")
        except (TypeError, ValueError):
            d["perms"] = []
        out[d["card_id"]] = d
    c.close()
    return out


for wid in ("work_018", "work_022"):
    print(f"\n=== {wid} ===")
    cards = load_cards(wid)
    role_ids = [k for k, v in cards.items() if v.get("card_type") == "role"]
    root_ids = [k for k, v in cards.items() if v.get("card_type") == "project"]
    if not role_ids or not root_ids:
        print(f"  跳过（role={role_ids} project={root_ids}）")
        continue
    root = root_ids[0]
    for rid in role_ids:
        r = cards[rid]
        print(f"  {rid}「{str(r['title'])[:24]}」v{r['perms_version']} "
              f"link={'link' in r['perms']}")
        # ① 根卡给 role 建线
        try:
            policy.check(root, rid, policy.SWITCH_LINK, cards,
                         scope=policy.SCOPE_ANY)
            check(f"  根卡 {root} → {rid} 建线", True)
        except PermissionError as e:
            check(f"  根卡 {root} → {rid} 建线", False, str(e))
        # ② role 给根卡发消息（read 关口 + 建线后可发）
        try:
            policy.check(rid, root, policy.SWITCH_READ, cards)
            check(f"  {rid} → {root} read 关口", True)
        except PermissionError as e:
            check(f"  {rid} → {root} read 关口", False, str(e))

print("\n=== 配置版本 ===")
d = json.load(open(r"D:\AI_station\推演agent\config\permissions.json",
                   encoding="utf-8"))
print("  version =", d["version"])
p, ver = policy.compute_perms("rd_project", "role")
print(f"  role 应有(v{ver}) = {sorted(p)}")
check("配置里 role 含 link", "link" in p)
check("配置里 role 含 unlink", "unlink" in p)

print("\n总判定:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)