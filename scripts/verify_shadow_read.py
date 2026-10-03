# -*- coding: utf-8 -*-
"""影子读权限四组样本验收（V8，方案 3 节第 7 条指定脚本名）。

在测试库新建临时剧本构造样本卡，经 card_tools.execute_tool 逐组调 read_shadow，
记录 ok 与返回体；另验 V9（写后连读历史行数不增）与 edit_card 拒写守卫。
结束清理临时剧本目录。不建 tests/ 目录。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from src.api import card_tools  # noqa: E402
from src.storage import card_store, shadow_store, work_store  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} | {detail}")


async def main() -> int:
    wdata = await work_store.create_work("V8影子读权限验收临时剧本")
    wid = wdata["work_id"]
    root = wdata["work_card_id"]
    print(f"临时剧本 {wid}（根卡 {root}）")
    try:
        a = await card_store.create_card(wid, "样本A", "模型生成", parent=root)
        aid = a["meta"]["card_id"]
        b = await card_store.create_card(wid, "样本B", "模型生成", parent=root)
        bid = b["meta"]["card_id"]
        c = await card_store.create_card(wid, "样本C", "模型生成", parent=bid)
        cid = c["meta"]["card_id"]
        await card_store._append_relation(wid, aid, {"target": bid, "type": "关联"})

        cards = card_tools._collect_card_meta(wid)

        # ① 子树样本：根卡（上级）读子卡 A 的影子
        r = await card_tools.execute_tool(wid, root, "read_shadow", {"card_id": aid}, cards)
        record("①子树:根卡 read_shadow(子卡A)", r.get("owner_card_id") == aid and "content" in r,
               f"keys={sorted(r.keys())[:4]}")

        # ② 血统链样本：子卡 A 读父卡（根卡）的影子
        r = await card_tools.execute_tool(wid, aid, "read_shadow", {"card_id": root}, cards)
        record("②血统链:子卡A read_shadow(根卡)", r.get("owner_card_id") == root and "content" in r,
               f"keys={sorted(r.keys())[:4]}")

        # ③ relations 样本：A 经连线读 B 的影子
        r = await card_tools.execute_tool(wid, aid, "read_shadow", {"card_id": bid}, cards)
        record("③relations:A read_shadow(B)", r.get("owner_card_id") == bid and "content" in r,
               f"keys={sorted(r.keys())[:4]}")

        # ④ 锚卡样本：任意卡（深层 C）调根卡影子
        r = await card_tools.execute_tool(wid, cid, "read_shadow", {"card_id": root}, cards)
        record("④锚卡:深层C read_shadow(根卡)", r.get("owner_card_id") == root and "content" in r,
               f"keys={sorted(r.keys())[:4]}")

        # 负样本：C 与 A 无关系无血统 → 越权
        r = await card_tools.execute_tool(wid, cid, "read_shadow", {"card_id": aid}, cards)
        record("负样本:C read_shadow(A) 应越权", "error" in r and "越权" in r["error"], str(r.get("error", "")))

        # edit_card 拒写守卫（改件三第 15 条）
        r = await card_tools.execute_tool(wid, root, "edit_card",
                                          {"card_id": shadow_store.shadow_id_of(aid), "content": "x"}, cards)
        record("拒写:edit_card(shadow_A)", "error" in r and "update_shadow" in r["error"], str(r.get("error", "")))

        # V9：写影子后连读 5 次，影子文件字节不变（历史行数不增）
        await shadow_store.update_shadow(wid, aid, "## 进度\n- v1\n\n## 产物清单\n- 无\n\n## 迭代历史\n- v1\n")
        p = shadow_store.shadow_path(wid, aid)
        raw0 = p.read_bytes()
        hist0 = raw0.count(b"\n")
        for _ in range(5):
            await shadow_store.read_shadow_raw(wid, aid)
        raw1 = p.read_bytes()
        record("V9:写后连读5次字节不变（历史行数不增）", raw0 == raw1, f"bytes={len(raw0)} lines={hist0}")
        record("V9附加:影子无 [外部修改] 追加段", b"\xe5\xa4\x96\xe9\x83\xa8\xe4\xbf\xae\xe6\x94\xb9" not in raw1
               and b"## \xe4\xbf\xae\xe6\x94\xb9\xe5\x8e\x86\xe5\x8f\xb2" not in raw1, "无『修改历史/外部修改』字样")
    finally:
        # 清理：删非根卡（影子级联），再整体移除临时剧本目录
        try:
            cards_dir = BASE_DIR / "works" / wid / "cards"
            for cp in sorted(cards_dir.glob("card_*.md"), reverse=True):
                try:
                    await card_store.delete_card(wid, cp.stem)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            shutil.rmtree(BASE_DIR / "works" / wid, ignore_errors=True)
            print(f"临时剧本 {wid} 已清理")

    fails = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nV8+V9 结果：PASS {len(RESULTS) - len(fails)} / FAIL {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
