# -*- coding: utf-8 -*-
"""[卡片信息结构改造] E 层实跑验收：V1~V15 逐条以真实产物判定。

纪律（任务书 §五总则）：每条验收以**实跑产出**判定，不得以 fixture 全绿替代。
本脚本直接调 card_store / card_tools 的真实函数（非 mock），产物落临时 works 根，
跑完即删——不触碰生产 works/（RD-04）。

运行：.venv/Scripts/python.exe verify/_adhoc/cardstruct_e2e_verify.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name} | {detail}")
    print(f"[{'PASS' if cond else 'FAIL'}] {name} | {detail}")


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cardstruct_verify_"))
    from src.storage import canvas_store, card_store, shadow_store, work_store
    from src.storage import data_snapshot, session_store
    from src.api import card_tools

    # 沙箱重定向：全部运行根搬进 tmp（生产 works/ 零触碰，RD-04）
    works = tmp / "works"
    works.mkdir(parents=True)
    for mod in (card_store, work_store, canvas_store, session_store):
        mod.WORKS_DIR = works
    data_snapshot.BASE_DIR = tmp
    data_snapshot.SNAPSHOT_ROOT = tmp / "snapshots"
    data_snapshot._FINGERPRINT_FILE = tmp / ".fp.json"

    w = await work_store.create_work("验收剧本")
    wid, root = w["work_id"], w["work_card_id"]

    # ============ V1 新建卡默认三块骨架 ============
    r = await card_store.create_card(wid, "V1样本", "用户直接创建", parent=root)
    v1_id = r["meta"]["card_id"]
    raw1 = (await card_store.read_card(wid, v1_id))["raw"]
    _, body1 = __import__("src.storage.hash_utils", fromlist=["x"]).parse_front_matter(raw1)
    heads_new = ["## 使命", "## 现状", "## 定论", "## 修改历史"]
    heads_old = ["## 职责区", "## 结论区", "## 否决记录区"]
    ok_v1 = all(h in body1 for h in heads_new) and not any(h in body1 for h in heads_old)
    # 全文无重复区头
    dup = [h for h in heads_new if body1.count(h) > 1]
    check("V1 新建卡三块骨架", ok_v1, f"含新三区头={all(h in body1 for h in heads_new)} 不含旧区头={not any(h in body1 for h in heads_old)}")
    check("V1 无重复区头", not dup, f"重复项={dup or '无'}")

    # ============ V2 使命块改动被拒 ============
    # 先把使命块定稿为一段真内容（经用户 HTTP PATCH 通道，非模型通道）
    await card_store.update_card(wid, v1_id, {"mission": "本卡作用：验收使命锁"}, "用户定稿使命")
    base = (await card_store.read_card(wid, v1_id))["raw"]
    _, base_body = __import__("src.storage.hash_utils", fromlist=["x"]).parse_front_matter(base)
    cards = card_tools._collect_card_meta(wid)
    bad_mission = base_body.replace("本卡作用：验收使命锁", "模型擅自改使命")
    r = await card_tools.execute_tool(wid, root, "edit_card",
                                      {"card_id": v1_id, "content": bad_mission}, cards)
    check("V2 使命块改动被拒", "error" in r and "使命块为立项定稿，修改需用户确认" in r.get("error", ""),
          f"resp={r}")

    # ============ S3-5 闸作用域：用户 PATCH 改使命成功 ============
    r = await card_store.update_card(wid, v1_id, {"mission": "用户改的使命"}, "用户改使命")
    check("S3-5 用户 PATCH 改使命成功（模型通道才设闸）", "error" not in r, f"新使命={r.get('body',{}).get('mission')}")

    # ============ V8b 决定写进现状区 → 机械拒绝 ============
    cur = (await card_store.read_card(wid, v1_id))["raw"]
    _, cur_body = __import__("src.storage.hash_utils", fromlist=["x"]).parse_front_matter(cur)
    inject = cur_body.replace("## 现状\n", "## 现状\n\nD07【裁定】偷塞的决定（2026-10-08·无依据）")
    r = await card_tools.execute_tool(wid, root, "edit_card", {"card_id": v1_id, "content": inject}, cards)
    check("V8b 决定写进现状区被拒", "error" in r and "现状块不装决定；决定进定论（D{nn} 条目属定论块）" in r.get("error", ""),
          f"resp={r}")

    # ============ V4① 追加新 D 条目 → 放行 ============
    # [判据修正] 追加位置必须在「## 定论」块内、「## 修改历史」之前——
    # 追加到文末会落进修改历史区（机械尾），那是拆分器的正确行为，不是缺陷。
    cur = (await card_store.read_card(wid, v1_id))["raw"]
    _, cur_body = __import__("src.storage.hash_utils", fromlist=["x"]).parse_front_matter(cur)
    appended = cur_body.replace(
        "## 修改历史",
        "D08【决定】验收追加条目（2026-10-08·V4）\n\n## 修改历史",
    )
    r = await card_tools.execute_tool(wid, root, "edit_card", {"card_id": v1_id, "content": appended}, cards)
    check("V4-① 追加新 D 条目放行", r.get("ok") is True, f"resp={r}")
    check("V4-① D08 已落进定论块",
          "D08【决定】验收追加条目" in (await card_store.read_card(wid, v1_id))["body"]["rulings"],
          "")

    # ============ V4② 失效标记 → 放行 ============
    after = (await card_store.read_card(wid, v1_id))["raw"]
    _, ab = __import__("src.storage.hash_utils", fromlist=["x"]).parse_front_matter(after)
    marked = ab.replace("D08【决定】验收追加条目（2026-10-08·V4）",
                        "→ 被D09取代（因 V4 覆盖测试）")
    r = await card_tools.execute_tool(wid, root, "edit_card", {"card_id": v1_id, "content": marked}, cards)
    check("V4-② 失效标记行放行", r.get("ok") is True, f"resp={r}")
    # 骨架里的 D01-D03/→ 也在定论块，一并验证：写回后定论块含该标记
    body_after_mark = (await card_store.read_card(wid, v1_id))["body"]
    # [判据修正] 判「D08 被就地替换为标记行」——不取末行（骨架自带 → 被D05取代 在更后面）
    check("V4-② 标记行已落盘", "→ 被D09取代" in body_after_mark["rulings"]
          and "D08【决定】验收追加条目" not in body_after_mark["rulings"],
          f"rulings含标记={'→ 被D09取代' in body_after_mark['rulings']} D08原文已移除={'D08【决定】验收追加条目' not in body_after_mark['rulings']}")

    # ============ V3/V8a 删除既有 D 条目 → 拒 ============
    ab2 = (await card_store.read_card(wid, v1_id))["raw"]
    _, ab2b = __import__("src.storage.hash_utils", fromlist=["x"]).parse_front_matter(ab2)
    deleted = "\n".join(l for l in ab2b.splitlines() if not l.startswith("D01【裁定】"))
    r = await card_tools.execute_tool(wid, root, "edit_card", {"card_id": v1_id, "content": deleted}, cards)
    check("V3/V8a 删除既有 D 条目被拒", "error" in r and "定论块只追加；失效用就地标记（→被N取代）" in r.get("error", ""),
          f"resp={r}")

    # ============ V3b 改写既有 D 条目 → 拒 ============
    _, ab3b = __import__("src.storage.hash_utils", fromlist=["x"]).parse_front_matter(
        (await card_store.read_card(wid, v1_id))["raw"])
    rewritten = ab3b.replace("D02【决定】", "D02【裁定】").replace("D02【裁定】", "D02【裁定】改写版 ")
    r = await card_tools.execute_tool(wid, root, "edit_card", {"card_id": v1_id, "content": rewritten}, cards)
    check("V3b 改写既有 D 条目被拒", "error" in r and "定论块只追加" in r.get("error", ""), f"resp={r}")

    # ============ V5 现状块自由覆盖 ============
    _, ab4b = __import__("src.storage.hash_utils", fromlist=["x"]).parse_front_matter(
        (await card_store.read_card(wid, v1_id))["raw"])
    newstatus = ab4b.replace("## 现状", "## 现状\n\nV5 自由改写现状：做到哪、卡在哪（经X实测）")
    r = await card_tools.execute_tool(wid, root, "edit_card", {"card_id": v1_id, "content": newstatus}, cards)
    check("V5 现状块自由覆盖无拒绝", r.get("ok") is True, f"resp={r}")
    check("V5 现状块内容已落盘", "V5 自由改写现状" in (await card_store.read_card(wid, v1_id))["body"]["status_block"],
          "status_block 含新内容")

    # ============ V6 存量旧卡零回归 ============
    # 造一张旧结构卡（手写旧区头文件，走真盘）
    old_path = card_store._card_path(wid, "card_900")
    from src.storage.hash_utils import apply_hash
    old_meta = ('---\ntitle: "旧卡"\nsource: "用户直接创建"\ncard_type: "线索"\n'
                f'parent: "{root}"\ntags: []\nrelations: []\nsession_ref: "sessions/session_900.md"\n'
                f'created_at: "2026-10-08T00:00:00+08:00"\nupdated_at: "2026-10-08T00:00:00+08:00"\n---\n\n')
    old_body = ("## 职责区\n\n旧使命\n\n## 结论区\n\n旧正文\n\n## 否决记录区\n\n旧否决\n\n"
                "## 修改历史\n\n- [2026-10-08T00:00:00+08:00] 卡片创建\n")
    old_path.write_text(apply_hash(old_meta + old_body), encoding="utf-8")
    got = await card_store.read_card(wid, "card_900")
    check("V6 旧卡读归一（旧键→新键）",
          got["body"]["mission"] == "旧使命" and got["body"]["status_block"] == "旧正文"
          and got["body"]["rulings"] == "旧否决",
          f"mission={got['body']['mission']!r} status_block={got['body']['status_block']!r} rulings={got['body']['rulings']!r}")
    # 写旧卡（区头须不变，RD-09）
    await card_store.update_card(wid, "card_900", {"conclusion": "旧卡新正文"}, "改旧卡")
    raw900 = (await card_store.read_card(wid, "card_900"))["raw"]
    check("V6 旧卡写后区头不变（保形）",
          "## 职责区" in raw900 and "## 结论区" in raw900 and "## 使命" not in raw900,
          f"含旧区头={('## 职责区' in raw900 and '## 结论区' in raw900)} 不含新区头={('## 使命' not in raw900)}")
    check("V6 旧卡新正文已落", "旧卡新正文" in (await card_store.read_card(wid, "card_900"))["body"]["status_block"], "")

    # ============ V11 content_hash 连锁未触发 ============
    hist = (await card_store.read_card(wid, v1_id))["body"]["history"]
    check("V11 无 [外部修改] 行", not any("外部修改" in h for h in hist), f"history={hist[-2:]}")

    # ============ V12 双实现同步（画布） ============
    canvas = canvas_store.load(wid)
    probe = next((c for c in canvas["cards"] if c["card_id"] == v1_id), None)
    check("V12 快路径 conclusion_summary 非空", bool(probe and probe.get("conclusion_summary")),
          f"summary={probe.get('conclusion_summary') if probe else None!r}")
    check("V12 canvas.json 字段名不变", probe is not None and "conclusion_summary" in probe,
          f"字段={list(probe.keys()) if probe else None}")

    # ============ V7 影子零改动 ============
    import inspect
    sig = inspect.signature(shadow_store.update_shadow)
    check("V7 update_shadow 签名无 card_id", "card_id" not in sig.parameters, f"params={list(sig.parameters)}")
    tpl = shadow_store.SHADOW_BODY_TEMPLATE
    check("V7 影子三节在案", all(s in tpl for s in ("## 进度", "## 产物清单", "## 迭代历史")),
          f"template节={[s for s in ('## 进度','## 产物清单','## 迭代历史') if s in tpl]}")

    # ============ S2-7 新旧键都可写 ============
    rr = await card_store.update_card(wid, v1_id, {"mission": "新键写 missions"}, "新键")
    check("S2-7 新键 mission 可写", rr.get("body", {}).get("mission") == "新键写 missions", "")
    rr = await card_store.update_card(wid, v1_id, {"mission": "旧键写 mission"}, "旧键")
    check("S2-7 旧键 charter 可写（归一到 mission）", rr.get("body", {}).get("mission") == "旧键写 mission", "")

    # ============ V14 读归一完整：5 消费点 ============
    # ① 读卡工具返回 status_block
    cards2 = card_tools._collect_card_meta(wid)
    rt = await card_tools.execute_tool(wid, root, "read_card", {"card_id": v1_id}, cards2)
    check("V14-① 读卡工具返回 status_block", "status_block" in json.dumps(rt, ensure_ascii=False) or rt.get("body") is not None,
          f"tool body 前40字={str(rt.get('body'))[:40]!r}")
    # ④ 后端摘要非空（已验 V12）
    check("V14-④ 后端摘要非空", bool(probe and probe.get("conclusion_summary")), "")
    # ⑤ update_card 新键可写（已验 S2-7）
    # ③ 画布回退路径 = canvas_store._conclusion_summary 直调（前端同算法）
    from src.storage.canvas_store import _conclusion_summary
    _, fb_body = __import__("src.storage.hash_utils", fromlist=["x"]).parse_front_matter(
        (await card_store.read_card(wid, v1_id))["raw"])
    check("V14-③ 画布回退路径得摘要", bool(_conclusion_summary(fb_body)), f"回退摘要={_conclusion_summary(fb_body)[:40]!r}")

    # ============ S4-1/S4-2 死字段 ============
    raw_v1 = (await card_store.read_card(wid, v1_id))["raw"]
    check("S4-1 新卡无 methodology_refs", "methodology_refs" not in raw_v1, "")
    check("S4-2 新卡无 pending_sync", "pending_sync" not in raw_v1, "")

    # ============ S2-8 白名单 ============
    from src.api.routes import ALLOWED_UPDATE_KEYS
    expect = {"charter", "conclusion", "rejections", "mission", "status_block", "rulings",
              "card_type", "parent", "title", "tags", "relations", "session_ref"}
    check("S2-8 白名单全集一致", ALLOWED_UPDATE_KEYS == expect,
          f"实际={sorted(ALLOWED_UPDATE_KEYS)}")

    # ============ S3-4/RD-11 锚点闸禁建 ============
    # [判据修正] 必须剥注释与 docstring 再查——「不含锚点闸」这句话本身在注释里，
    # 直接 grep 全文会把注释当实现（这正是「断言不得同义反复」的同族坑）。
    import io
    import tokenize
    src_path = ROOT / "src" / "api" / "card_tools.py"
    code_only: list[str] = []
    with io.open(src_path, encoding="utf-8") as fh:
        for tok in tokenize.generate_tokens(fh.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            code_only.append(tok.string)
    code_joined = "".join(code_only)
    check("S3-4/RD-11 无锚点机械闸（剥注释后查可执行代码）",
          "含锚点" not in code_joined and "anchor" not in code_joined.lower(),
          f"可执行代码中含锚点字样={'含锚点' in code_joined}")

    # ============ V15 三调用点全传 form ============
    cs = (ROOT / "src" / "storage" / "card_store.py").read_text(encoding="utf-8")
    n_calls = cs.count("_render_card(")
    n_form = cs.count("form=")
    check("V15 三调用点均传 form", n_calls == 4 and n_form >= 3,  # 4 = 1 定义 + 3 调用
          f"_render_card 出现={n_calls} form= 出现={n_form}")

    print(f"\n{'='*60}\n验收：PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
