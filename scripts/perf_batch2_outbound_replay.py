# -*- coding: utf-8 -*-
"""成本优化任务书 · 批二验收回放（2026-11-20）：**指定生产样本**的三组读数。

样本＝work_073（CardColony 重构）的三张卡，与任务书 §四的验收矩阵一一对应：

- ``card_007`` 全局管控（管辖 13 个直接子卡）
- ``card_057`` 单卡委派（对象＝本卡，子卡全是执行件）
- ``card_009`` 依赖重边（四条出边＋六条入边）

三组口径（同一棵树、同一装配入口，只换槽位与范围档）：

- **A 第一批完成**：清单/子卡还在 system（移位前）＋ 依赖去重已落地；
- **B 仅移位**：清单/子卡走当轮事实消息（tail），**不缩量**；
- **C 按任务缩量**：移位 ＋ 按可信任务范围缩量（清单/子卡/依赖正稿三处）。

**只读**：本脚本不写生产库、不起模型回合、不写观测账（观测目录重定向到临时
目录）。历史（prior）不在本脚本的计量内——真实回合的历史字节见旧 prompt 日志
（``logs/prompt_card_*.json``）与隔离回放的实际出站。

用法：
    python scripts/perf_batch2_outbound_replay.py [work_id]
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api import context_assembler as ca  # noqa: E402
from src.api import context_regions as cr  # noqa: E402

WORK = sys.argv[1] if len(sys.argv) > 1 else "work_073"
CARDS = ("card_007", "card_057", "card_009")
OUT = ROOT / "logs" / "batch2" / "batch2_samples.json"

LIST_MARK = "〔卡片清单〕"
CHILD_MARK = "〔直接子卡现状〕"


def _measure(card_id: str, delivery: str, scope_on: bool) -> dict:
    """跑一组装配并量字节。"""
    from src.infra import observe as _obs

    _obs.LOGS_DIR = Path(tempfile.gettempdir()) / "batch2_observe"
    os.environ["CONTEXT_SCOPE"] = "1" if scope_on else "0"
    patched = []
    for spec in cr.REGION_TABLE:
        if spec.get("key") in ("card_list", "child_status"):
            patched.append((spec, spec.get("delivery"), spec.get("render")))
            spec["delivery"] = delivery
            if delivery == "system":
                orig = spec["render"]

                def _as_system(ctx, _orig=orig):
                    out = _orig(ctx)
                    if isinstance(out, list):
                        if not out:
                            return ""
                        c = str((out[0] or {}).get("content") or "")
                        parts = c.split("\n", 1)
                        return parts[1] if len(parts) > 1 else ""
                    return out

                spec["render"] = _as_system
    try:
        ctx = ca.build_ctx(WORK, card_id,
                           scope_hint={"source": None, "source_kind": "human",
                                       "act_as": "", "ctx_card": "",
                                       "readback_available": True})
        out = cr.compose_model_context(ctx, profile="main")
    finally:
        for spec, old_delivery, old_render in patched:
            spec["delivery"] = old_delivery
            spec["render"] = old_render
    system = str(out.get("system_prompt") or "")
    tail_msgs = out.get("turn_tail") or []
    tail_text = "\n".join(str(m.get("content") or "") for m in tail_msgs)
    dep = ""
    i = system.find("## 依赖关联")
    if i >= 0:
        j = system.find("\n## ", i + 5)
        dep = system[i:j if j > 0 else len(system)]
    scope = ctx.task_scope
    return {
        "mode": str(getattr(scope, "mode", "")),
        "basis": str(getattr(scope, "basis", "")),
        "reason": str(getattr(scope, "reason", "")),
        "target_card": str(getattr(scope, "target_card", "")),
        "system_bytes": len(system.encode("utf-8")),
        "tail_bytes": len(tail_text.encode("utf-8")),
        "model_input_bytes": len(system.encode("utf-8")) + len(tail_text.encode("utf-8")),
        "list_bytes": len(tail_text.split(LIST_MARK)[-1].encode("utf-8")) if LIST_MARK in tail_text else 0,
        "child_bytes": len(tail_text.split(CHILD_MARK)[-1].encode("utf-8")) if CHILD_MARK in tail_text else 0,
        "dep_bytes": len(dep.encode("utf-8")),
        "tail_messages": len(tail_msgs),
    }


def main() -> int:
    rows = []
    for card in CARDS:
        a = _measure(card, "system", False)
        b = _measure(card, "tail", False)
        c = _measure(card, "tail", True)
        rows.append({"card_id": card,
                     "A_batch1_only": a, "B_tail_only": b, "C_tail_and_narrowed": c})
        print("=" * 72)
        print(f"{card}  A/B/C 的模型输入字节："
              f"{a['model_input_bytes']:,} → {b['model_input_bytes']:,} → "
              f"{c['model_input_bytes']:,}")
        print(f"  A 清单在 system：{a['system_bytes']:,}B system；"
              f"子卡 {a['child_bytes']:,}B；依赖 {a['dep_bytes']:,}B")
        print(f"  B system {b['system_bytes']:,}B；tail {b['tail_bytes']:,}B"
              f"（清单 {b['list_bytes']:,}B／子卡 {b['child_bytes']:,}B）；"
              f"依赖 {b['dep_bytes']:,}B")
        print(f"  C 范围={c['mode']}（{c['basis']}）system {c['system_bytes']:,}B；"
              f"tail {c['tail_bytes']:,}B（清单 {c['list_bytes']:,}B／"
              f"子卡 {c['child_bytes']:,}B）；依赖 {c['dep_bytes']:,}B")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"work_id": WORK, "rows": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n证据已写：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
