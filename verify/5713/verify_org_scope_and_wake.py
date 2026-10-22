# -*- coding: utf-8 -*-
"""端到端验证三条需求（在沙箱里造真实卡面，真调 handler）。"""
import asyncio
import io
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\AI_station\推演agent")

tmp = tempfile.mkdtemp(prefix="three_")
from pathlib import Path
from src.storage import card_store, work_store, canvas_store, session_store, role_store
for m in (card_store, work_store, canvas_store, session_store):
    m.WORKS_DIR = Path(tmp) / "works"

from src.api import card_tools as ct

ok = True


def check(label, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label} {extra}")


async def main():
    w = await work_store.create_work("三条需求验证")
    wid, root = w["work_id"], w["work_card_id"]
    print(f"work={wid} root={root}\n")

    # 造：角色卡（离树）+ 一张普通子树卡 + 一张孙卡
    r = await card_store.create_card(wid, "产品负责人", "模型生成", card_type="role")
    rid = r["meta"]["card_id"]
    c1 = await card_store.create_card(wid, "子项目A", "模型生成", parent=root)
    c1id = c1["meta"]["card_id"]
    c2 = await card_store.create_card(wid, "子项目B", "模型生成", parent=root)
    c2id = c2["meta"]["card_id"]
    print(f"角色={rid}（parent={(card_store._meta_of(wid, rid) or {}).get('parent')!r}）")
    print(f"树内卡: {c1id}, {c2id}\n")

    cards = ct._collect_card_meta(wid)

    # ══ 需求 3：角色卡能在树内任意位置挂载卡片 ══
    print("=== 需求3-A：角色卡建卡挂到树内任意位置 ===")
    res = await ct._h_create_card(
        wid, rid,
        {"title": "角色派的活", "mission": "由产品负责人派出的执行单元，负责某一具体交付面。",
         "parent": c1id},
        cards)
    print(f"  建卡返回: {str(res)[:160]}")
    check("角色卡能在别人的子树下建卡", res.get("ok") is True, str(res)[:120])
    new_id = (res.get("card_id") or "")
    if new_id:
        m = card_store._meta_of(wid, new_id) or {}
        check("新卡真的挂到指定父卡下", str(m.get("parent")) == c1id,
              f"parent={m.get('parent')}")

    # ══ 需求3-B：角色卡能建 coder 挂到指定位置 ══
    print("\n=== 需求3-B：角色卡建执行卡挂到指定位置 ===")
    cards = ct._collect_card_meta(wid)
    res_b = await ct._h_create_coder_card(
        wid, rid,
        {"title": "执行单元", "task": "完成产品负责人交代的一件事。",
         "parent": c2id},
        cards)
    print(f"  返回: {str(res_b)[:160]}")
    check("角色卡能建 coder 到指定父卡下", res_b.get("ok") is True, str(res_b)[:120])

    # ══ 需求3-C：角色卡能把已有卡挪位置 ══
    print("\n=== 需求3-C：角色卡把已有卡挂到别处 ===")
    if new_id:
        cards = ct._collect_card_meta(wid)
        res_c = await ct._h_reparent_card(
            wid, rid, {"card_id": new_id, "new_parent_id": c2id}, cards)
        print(f"  返回: {str(res_c)[:160]}")
        check("角色卡能移动树内任意卡", res_c.get("ok") is True, str(res_c)[:120])

    # ══ 反证：角色卡不能动根卡 ══
    print("\n=== 反证：角色卡动不了根卡 ===")
    cards = ct._collect_card_meta(wid)
    res_d = await ct._h_reparent_card(
        wid, rid, {"card_id": root, "new_parent_id": c1id}, cards)
    check("动根卡被拒", res_d.get("ok") is False, str(res_d)[:120])

    # ══ 需求 1：根卡能唤醒树内任意卡（含无 goal / 非 auto / 已完成）══
    print("\n=== 需求1：wake_card 唤醒各种状态的卡 ===")
    # 造一张「非 auto、无 goal、completed」的卡
    c3 = await card_store.create_card(wid, "沉睡的卡", "模型生成", parent=root)
    c3id = c3["meta"]["card_id"]
    await card_store.update_card(wid, c3id,
                                 {"auto_mode": 0, "state": "completed"},
                                 "测试：造一个唤不醒的状态")
    m3 = card_store._meta_of(wid, c3id) or {}
    print(f"  {c3id}: auto={m3.get('auto_mode')} state={m3.get('state')}")
    cards = ct._collect_card_meta(wid)
    res_w = await ct._h_wake_card(wid, root, {"card_id": c3id,
                                              "why": "检查一下你上次留下的东西"}, cards)
    print(f"  wake 返回: {str(res_w)[:200]}")
    check("根卡能唤醒非 auto 的卡", res_w.get("ok") is True, str(res_w)[:140])

    # ══ 需求 2：根卡能唤醒角色卡 ══
    print("\n=== 需求2：根卡唤醒角色卡 ===")
    cards = ct._collect_card_meta(wid)
    res_r = await ct._h_wake_card(wid, root, {"card_id": rid,
                                              "why": "现在做一次巡检"}, cards)
    print(f"  wake 返回: {str(res_r)[:220]}")
    check("根卡能唤醒角色卡", res_r.get("ok") is True, str(res_r)[:140])
    # 角色卡被唤醒有两条路：有待处理同步→走巡检通道；没有→主动造一轮。
    # 两条都算「能激活」，故断言 started 为真且 via 属于这两者之一。
    check("角色卡真的跑起来了（巡检通道 或 主动唤醒）",
          res_r.get("started") is True
          and res_r.get("via") in ("role_manual_check", "wake_prompt"),
          f"via={res_r.get('via')} started={res_r.get('started')}")

    # ══ 反证：非根卡不能越权唤醒 ══
    print("\n=== 反证：兄弟卡之间不能互相唤醒 ===")
    cards = ct._collect_card_meta(wid)
    res_x = await ct._h_wake_card(wid, c1id, {"card_id": c2id}, cards)
    check("c1 唤醒兄弟 c2 被拒", res_x.get("ok") is False, str(res_x)[:140])


asyncio.run(main())
print("\n总判定:", "PASS" if ok else "FAIL")
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if ok else 1)