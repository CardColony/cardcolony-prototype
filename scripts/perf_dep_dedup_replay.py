# -*- coding: utf-8 -*-
"""成本优化任务书 · 批一验收回放：依赖区正稿去重（2026-11-20）。

做法：取 logs/prompt_card_009_20261120094647.json 里**模型实际收到的**
system_prompt，把「## 依赖关联」区反解成 out/in 条目——
① 用改前渲染算法重渲染，断言与快照区**逐字一致**（证明解析忠实）；
② 用新 ``render_dep_relations_region`` 渲染，报告：
   - 各目标正稿全文在区内的出现次数（旧 vs 新）；
   - 区字节、整 system_prompt 字节的删减量；
   - 边条数、反向条数不变。
用法：.venv/Scripts/python.exe -X utf8 scripts/perf_dep_dedup_replay.py [快照路径]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api import context_regions  # noqa: E402

SNAP = ROOT / "logs" / "prompt_card_009_20261120094647.json"


def _parse_region(region: str):
    """把渲染出的依赖区反解为条目（kind 留空、整个括号尾缀存 detail——
    经 _rel_suffix 回渲逐字不变，去重判据只吃 target+requirements）。"""
    out_entries, in_entries = [], []
    lines = region.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("- 我依赖/关联："):
            body = ln[len("- 我依赖/关联："):]
            target, _, suffix = body.partition("（")
            suffix = suffix.rstrip("）")
            ent = {"target": target, "kind": "", "detail": suffix}
            i += 1
            # 正稿块：「  {target} 的需求文档（随它卡面更新）：」＋若干「  」行
            if i < len(lines) and lines[i].startswith(
                    f"  {target} 的需求文档（随它卡面更新）："):
                i += 1
                doc = []
                while i < len(lines) and lines[i].startswith("  "):
                    doc.append(lines[i][2:])
                    i += 1
                ent["requirements"] = "\n".join(doc)
            out_entries.append(ent)
            continue
        if ln.startswith("- 依赖/关联我的："):
            body = ln[len("- 依赖/关联我的："):]
            source, _, suffix = body.partition("（")
            in_entries.append({"source": source, "kind": "",
                               "detail": suffix.rstrip("）")})
        i += 1
    return out_entries, in_entries


def _render_old(out_entries, in_entries) -> str:
    """改前渲染算法（与 git 历史版逐行同构，供对账）。"""
    lines = []
    for r in out_entries:
        target = str(r.get("target") or "")
        if not target:
            continue
        lines.append(f"- 我依赖/关联：{target}（{r.get('detail') or ''}）")
        req = str(r.get("requirements") or "").strip()
        if req:
            lines.append(f"  {target} 的需求文档（随它卡面更新）：")
            lines.extend(f"  {ln}" for ln in req.split("\n"))
    for r in in_entries:
        source = str(r.get("source") or "")
        if source:
            lines.append(f"- 依赖/关联我的：{source}（{r.get('detail') or ''}）")
    if not lines:
        return ""
    return "## 依赖关联\n" + "\n".join(lines)


def main() -> int:
    snap_path = Path(sys.argv[1]) if len(sys.argv) > 1 else SNAP
    d = json.loads(snap_path.read_text(encoding="utf-8"))
    sp = d["system_prompt"]
    i = sp.find("## 依赖关联")
    assert i >= 0, "快照里没有依赖区"
    m = re.search(r"\n## ", sp[i + 10:])
    j = i + 10 + m.start() if m else len(sp)
    # 区切片尾部可能带上 assemble_regions 的区间分隔空行——那属于拼接缝，
    # 不属于区本体；对账与字节都以渲染出的区为准（剥掉尾部空行）。
    region = sp[i:j].rstrip("\n")

    out_entries, in_entries = _parse_region(region)

    # ① 保真对账：旧算法重渲染须与快照区逐字一致
    old_region = _render_old(out_entries, in_entries)
    if old_region != region:
        print("!! 解析不忠实：旧算法重渲染与快照区不一致（先看解析，不看优化数）")
        for k, (a, b) in enumerate(zip(old_region.split("\n"),
                                       region.split("\n"))):
            if a != b:
                print(f"  首个差异行 {k}:\n    重渲={a[:120]!r}\n    快照={b[:120]!r}")
                break
        return 1

    # ② 新渲染
    new_region = context_regions.render_dep_relations_region(
        out_entries, in_entries)

    # ③ 读数
    print(f"快照: {snap_path.name}")
    print(f"正向边 {len(out_entries)} 条 / 反向 {len(in_entries)} 条 —— 渲染后不变: "
          f"{new_region.count('- 我依赖/关联：')} / "
          f"{new_region.count('- 依赖/关联我的：')}")
    print()
    # 每个带正稿的 target：全文首行在旧/新区各出现几次
    seen = set()
    for e in out_entries:
        req = (e.get("requirements") or "").strip()
        t = e["target"]
        if not req or (t, req[:40]) in seen:
            continue
        seen.add((t, req[:40]))
        head = req.split("\n")[0]
        n_old, n_new = region.count(head), new_region.count(head)
        print(f"  {t} 正稿（{len(req.encode('utf-8'))}B，首行指纹 "
              f"{head[:30]!r}）: 旧 {n_old} 份 → 新 {n_new} 份")
    print()
    old_b = len(region.encode("utf-8"))
    new_b = len(new_region.encode("utf-8"))
    old_sp = len(sp.encode("utf-8"))
    new_sp = old_sp - old_b + new_b
    print(f"依赖区: {old_b:,}B → {new_b:,}B（删 {old_b - new_b:,}B，"
          f"{(old_b - new_b) / old_b:.1%}）")
    print(f"整 system_prompt: {old_sp:,}B → {new_sp:,}B（删 {old_sp - new_sp:,}B，"
          f"{(old_sp - new_sp) / old_sp:.1%}）")
    n_ptr = new_region.count("不重复展开")
    print(f"指针行: {n_ptr} 条（每条 ~60B，已计入新字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
