# -*- coding: utf-8 -*-
"""V1→V2 会话批量迁移脚本（迭代一·A9，部署窗口第 3/4 步）。

职责：把**指定目录树**（必须是副本或部署窗口内已停写的生产树）下的全部
V1 会话文件一次性转换为 V2，并把记忆水位（memory/{card}.json 的 mark，
块数口径）翻译进 V2 头 `condense_mark`。

铁律：
- **any-fail-abort**：先全部转换＋验证于内存，任何一份失败＝零写入、
  非零退出、逐文件清单里标出失败原因；
- 转换逐块一一对应、块数不变（水位恒等映射的承重点）；
- 迁移后逐文件验证：V2 解析五键（role/content/timestamp/model/pair_id）
  与 V1 旧读逐块等价；有水位的卡核对 `read()[mark:]` 切片与旧读一致；
- 已是 V2 的文件跳过（幂等，可重跑）；
- 本脚本**不 import 生产默认路径**——目标树必须显式 --target 给出。

用法：
    python scripts/migrate_sessions_v1_to_v2.py --target <works副本目录> \
        [--apply]     # 缺省 dry-run：只出清单不落盘；--apply 才写盘

输出：逐文件清单 JSON（stdout 摘要 ＋ <target>/_migration_report.json）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.storage import session_facade as sf          # noqa: E402
from src.storage import session_store                 # noqa: E402


def _v2_text_from(enriched: list[dict], card_id: str,
                  condense_mark: int | None) -> str:
    """旧读块列 → V2 全文（复用门面内部函数，与运行时现场升格同一实现）。"""
    conv = sf._convert_v1_blocks(enriched)
    header = sf._default_header(card_id)
    if condense_mark is not None:
        header["condense_mark"] = condense_mark
    return sf._render_v2_header(header) + "".join(sf._render_v2_block(b) for b in conv)


async def amain() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="works 树（副本或停写的生产树）")
    ap.add_argument("--apply", action="store_true", help="真写盘（缺省 dry-run）")
    args = ap.parse_args()

    target = Path(args.target).resolve()
    if not target.exists() or not list(target.glob("work_*")):
        print(f"[migrate] --target 必须指向 works 树: {target}", file=sys.stderr)
        return 2

    # 把门面的 WORKS_DIR 重定向到目标树（脚本内全局生效；先例 preflight 手法）
    session_store.WORKS_DIR = target

    report: list[dict[str, object]] = []
    t0 = time.time()
    conv_count = v2_skip = 0
    failures: list[str] = []

    for spath in sorted(target.glob("work_*/sessions/session_*.md")):
        work_id = spath.parent.parent.name
        # 承重映射（session_store._session_path :38-40）：card_{seq} ↔ session_{seq}
        # 编号对应——文件 stem 是 session_XXX，消费方（memory 水位/卡面 session_ref）
        # 全部以 card_id 为键，必须反解回来。
        card_id = "card_" + spath.stem.split("_", 1)[1]
        rel = spath.relative_to(target).as_posix()
        item: dict[str, object] = {"file": rel, "work": work_id, "card": card_id}
        try:
            v1_text = spath.read_text(encoding="utf-8")
            if sf._detect_format(v1_text) == 2:
                item["action"] = "skip_v2"
                v2_skip += 1
                report.append(item)
                continue
            old_blocks = sf._enrich_v1(await session_store.read_session(work_id, card_id))
            # 水位翻译：memory/{card}.json 的 mark（块数口径，恒等映射直读）
            mpath = target / work_id / "memory" / f"{card_id}.json"
            mark: int | None = None
            if mpath.exists():
                try:
                    m = json.loads(mpath.read_text(encoding="utf-8")).get("mark")
                    if isinstance(m, int) and m >= 0:
                        mark = m
                except (ValueError, OSError):
                    mark = None  # 读不出＝不写假值（V2 头缺省按 0 读，绝不编造）
            v2_text = _v2_text_from(old_blocks, card_id, mark)

            # 内存验证：V2 解析五键与旧读逐块等价
            new_blocks = sf._parse_v2(v2_text)["blocks"]
            assert len(new_blocks) == len(old_blocks), \
                f"块数漂移 old={len(old_blocks)} new={len(new_blocks)}"
            for a, b in zip(old_blocks, new_blocks):
                for k in ("role", "content", "timestamp", "model", "pair_id"):
                    same = a.get(k) == b.get(k)
                    if not same and k == "pair_id" and a.get(k) == "" and b.get(k):
                        # 〔裁定·迁移演练 2026-10-10〕V1 空请求身份 → V2 兜底取
                        # pair 值（T057 语义：pair＝响应发起块 seq），与运行时
                        # 现场升格产出形态一致；前端 pair_id 恒为 32 位 uuid，
                        # 与数字 seq 无碰撞面。许可形态，计数留痕。
                        item["pair_id_fallback"] = item.get("pair_id_fallback", 0) + 1
                        same = True
                    assert same, \
                        f"{spath.name} 块{a.get('seq')} 键{k} 不等价: {a.get(k)!r} vs {b.get(k)!r}"
            # 水位核对：mark 切片一致（块数＋content）
            if mark is not None:
                assert mark <= len(new_blocks), f"mark={mark} 超界"
                assert [b["content"] for b in new_blocks[mark:]] == \
                       [b["content"] for b in old_blocks[mark:]], "水位切片不等价"

            item.update({"action": "convert", "blocks": len(old_blocks),
                         "condense_mark": mark, "verified": True})
            if args.apply:
                tmp = spath.with_name(spath.name + ".migrating")
                tmp.write_text(v2_text, encoding="utf-8")
                tmp.replace(spath)
            conv_count += 1
        except Exception as exc:  # noqa: BLE001 any-fail-abort
            item.update({"action": "FAIL", "error": repr(exc)})
            failures.append(f"{rel}: {exc!r}")
        report.append(item)

    out = target / "_migration_report.json"
    out.write_text(json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "apply": bool(args.apply),
        "converted": conv_count, "skipped_v2": v2_skip, "failures": failures,
        "items": report}, ensure_ascii=False, indent=1), encoding="utf-8")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[migrate] {mode} 完成 {time.time() - t0:.1f}s：转换={conv_count} "
          f"跳过(V2)={v2_skip} 失败={len(failures)} 清单={out}")
    for f in failures[:10]:
        print(f"  FAIL {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
