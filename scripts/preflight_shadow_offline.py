# -*- coding: utf-8 -*-
"""影子卡机制离线预检（重启前自检，隔离在临时目录，不碰真实 works/）。

覆盖：建卡派生钩点／派生幂等／序号不变量／画布不变量／工具读写／
edit_card 拒写／删卡同删／派生失败不阻断／读不增历史。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from src.api import card_tools  # noqa: E402
from src.storage import card_store, canvas_store, data_snapshot, shadow_store, work_store  # noqa: E402

PASS = []
FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="shadow_preflight_"))
    works = tmp / "works"
    works.mkdir()
    card_store.WORKS_DIR = works
    work_store.WORKS_DIR = works
    canvas_store.WORKS_DIR = works
    data_snapshot.SNAPSHOT_ROOT = tmp / "snapshots"

    # 1. create_work → 根卡经 create_card → 影子自动派生（钩点生效）
    wdata = await work_store.create_work("预检剧本")
    wid = wdata["work_id"]
    root_id = wdata["work_card_id"]
    sp = shadow_store.shadow_path(wid, root_id)
    check("建剧本根卡→影子自动派生", sp.exists(), str(sp))

    # 2. create_card 子卡 → 影子派生
    r = await card_store.create_card(wid, "子卡甲", "模型生成", parent=root_id, conclusion="内容")
    cid = r["meta"]["card_id"]
    check("建子卡→影子自动派生", shadow_store.shadow_path(wid, cid).exists())

    # 3. 派生幂等：再调 derive_shadow → existed，不覆盖
    before = shadow_store.shadow_path(wid, cid).read_text(encoding="utf-8")
    again = await shadow_store.derive_shadow(wid, cid)
    check("派生幂等（existed 不覆盖）", again.get("existed") is True
          and shadow_store.shadow_path(wid, cid).read_text(encoding="utf-8") == before)

    # 4. 序号不变量（V3 同判据）：影子存在前后 _next_seq 逐字相同
    seq_before = card_store._next_seq(wid)
    await shadow_store.update_shadow(wid, cid, "## 进度\n- 测试\n\n## 产物清单\n- 无\n\n## 迭代历史\n- v1\n")
    seq_after = card_store._next_seq(wid)
    check("序号分配不受影子影响", seq_before == seq_after, f"{seq_before}=={seq_after}")

    # 5. 画布不变量（V2 同判据）：update_shadow 前后 canvas.json 逐字节相同
    canvas_path = works / wid / "canvas.json"
    canvas_bytes = canvas_path.read_bytes()
    cards_meta = card_tools._collect_card_meta(wid)
    sid = shadow_store.shadow_id_of(cid)
    check("影子进 meta 字典", sid in cards_meta and cards_meta[sid].get("card_type") == "shadow")
    check("影子 parent=正卡", cards_meta[sid].get("parent") == cid)
    check("canvas.json 影子不进画布", sid not in canvas_path.read_text(encoding="utf-8"))
    tr = await card_tools.execute_tool(wid, cid, "update_shadow", {"content": "## 进度\n- v2\n\n## 产物清单\n- a.md\n\n## 迭代历史\n- v2\n"}, cards_meta)
    check("update_shadow 工具返回 ok", tr.get("ok") is True, str(tr))
    check("update_shadow 前后 canvas.json 逐字节相同", canvas_path.read_bytes() == canvas_bytes)
    glob_count = len(list((works / wid / "cards").glob("card_*.md")))
    check("context_assembler glob 条数不变", glob_count == 2, f"count={glob_count}")

    # 6. read_shadow 工具：自己读自己 / 上级读子卡
    tr = await card_tools.execute_tool(wid, cid, "read_shadow", {}, cards_meta)
    check("read_shadow 缺省=自己", tr.get("owner_card_id") == cid and "content" in tr and "updated_at" in tr)
    tr = await card_tools.execute_tool(wid, root_id, "read_shadow", {"card_id": cid}, cards_meta)
    check("上级 read_shadow 子卡（推送到达机制）", tr.get("owner_card_id") == cid and "## 进度" in tr.get("content", ""))

    # 7. edit_card 拒写影子（后门守卫）
    tr = await card_tools.execute_tool(wid, root_id, "edit_card", {"card_id": sid, "content": "黑入影子"}, cards_meta)
    check("edit_card 拒写影子", "error" in tr and "update_shadow" in tr["error"], str(tr.get("error", "")))

    # 8. 删卡同删（V4/V5 同判据）：子卡可删（影子不构成子卡判据）→ 影子同删无残留
    dr = await card_store.delete_card(wid, cid)
    check("删正卡不因影子报『存在子卡』", dr.get("ok") is True and dr.get("deleted") == cid, str(dr))
    check("删正卡→影子同删无残留", not shadow_store.shadow_path(wid, cid).exists())

    # 9. 派生失败不阻断（V6 同判据，模拟权限异常）＋ warning 日志
    warnings: list[str] = []
    handler_ok = True
    orig_derive = shadow_store.derive_shadow

    def boom(*a, **k):
        raise PermissionError("模拟权限异常(预检)")

    import logging
    class Cap(logging.Handler):
        def emit(self, record):
            warnings.append(record.getMessage())
    logging.getLogger("storage.card").addHandler(Cap())
    shadow_store.derive_shadow = boom  # type: ignore[assignment]
    try:
        r2 = await card_store.create_card(wid, "子卡乙", "模型生成", parent=root_id)
        check("派生失败→建卡照常成功（201 语义）", "error" not in r2, str(r2)[:120])
        check("派生失败→warning 日志含 card_id", any("影子派生失败" in w and "card_00" in w for w in warnings),
              warnings[-1] if warnings else "无日志")
    finally:
        shadow_store.derive_shadow = orig_derive  # type: ignore[assignment]
        logging.getLogger("storage.card").removeHandler(Cap())

    # 10. 写后连读不增历史（V9 同判据）：读是纯读，文件字节不变
    cid2 = "card_002"
    await shadow_store.update_shadow(wid, cid2, "## 进度\n- v1\n\n## 产物清单\n- 无\n\n## 迭代历史\n- v1\n")
    raw0 = shadow_store.shadow_path(wid, cid2).read_bytes()
    for _ in range(5):
        await shadow_store.read_shadow_raw(wid, cid2)
    raw1 = shadow_store.shadow_path(wid, cid2).read_bytes()
    check("连读 5 次影子字节不变（历史不增）", raw0 == raw1)

    # 11. 编辑正卡正文不影响影子（方案 4.2 行 2：不加代码，影子存活）
    shadow_before = shadow_store.shadow_path(wid, cid2).read_bytes()
    await card_store.update_card(wid, cid2, {"conclusion": "正文更新"}, "预检编辑")
    check("编辑正卡→影子一字节不动", shadow_store.shadow_path(wid, cid2).read_bytes() == shadow_before)

    check("handler 旗标", handler_ok)
    print(f"\n预检结果：PASS {len(PASS)} / FAIL {len(FAIL)}  临时目录={tmp}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
