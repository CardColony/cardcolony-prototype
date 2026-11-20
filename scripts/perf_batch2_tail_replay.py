# -*- coding: utf-8 -*-
"""批二验收回放：卡片清单＋直接子卡现状迁当轮事实消息（turn_tail）的前后量测。

口径：不动语义只做位搬移——旧 system_prompt 里裁掉 order100/order170 两区
（以快照 regions[].bytes 记录长度切取，验证切片指纹与记录一致），
新布局里这两段文本改由 turn_tail 携带。

读数：
- 每份快照：system 旧字节 → 新字节（裁后），迁入 tail 的字节。
- 相邻同卡快照对：旧 system 最长公共前缀 vs 新 system 最长公共前缀
  （前缀缓存可复用长度的真实变化）。

用法：python scripts/perf_batch2_tail_replay.py [glob]
"""
import glob
import hashlib
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


MOVED = ("卡片清单", "直接子卡（活性·使命·现状）")


def region_bounds(sp: str, regions: list[dict]) -> list[tuple[int, int]]:
    """按 regions 元数据（名称→记录字节）在 system_prompt 里定位两区切片。

    切片判据：从 '## <区名>' 标题起，取 UTF-8 字节数等于记录 bytes 的文本，
    并要求紧随其后的字节恰是区拼接符 '\\n\\n'——指纹对账防错切。
    """
    out = []
    for name in MOVED:
        meta = next((r for r in regions if r.get("name") == name), None)
        if not meta:
            continue
        start = sp.find("## " + name)
        if start < 0:
            print(f"  !! {name} 未在 system_prompt 找到标题"); continue
        want = int(meta["bytes"])
        # 逐字符扩张到字节数命中（中文 3B/字符，线性扫足够）
        end, b = start, 0
        while end < len(sp) and b < want:
            b += len(sp[end].encode("utf-8")); end += 1
        seg = sp[start:end]
        ok_bytes = len(seg.encode("utf-8")) == want
        ok_sep = sp[end:end + 2] == "\n\n" or end == len(sp)
        fp = "sha256:" + hashlib.sha256(seg.encode("utf-8")).hexdigest()[:16]
        ok_fp = fp == meta.get("fingerprint")
        out.append((start, end, name, want, ok_bytes and ok_sep and ok_fp))
        print(f"  区[{name}] {start}..{end} 记录{want}B 切出{len(seg.encode('utf-8'))}B "
              f"指纹{'✓' if ok_fp else '✗'} 分隔{'✓' if ok_sep else '✗'}")
    return out


def strip_regions(sp: str, bounds: list[tuple[int, int]]) -> str:
    """裁掉区文本＋一个拼接分隔，保持其余逐字不变。"""
    pieces, cur = [], 0
    for start, end, *_ in sorted(bounds):
        pieces.append(sp[cur:start])
        cur = end + 2 if sp[end:end + 2] == "\n\n" else end
    pieces.append(sp[cur:])
    return "".join(pieces)


def lcp(a: str, b: str) -> int:
    n, i = min(len(a), len(b)), 0
    while i < n and a[i] == b[i]:
        i += 1
    return len(a[:i].encode("utf-8"))


def main() -> None:
    pat = sys.argv[1] if len(sys.argv) > 1 else "logs/prompt_card_007_2026112*.json"
    files = sorted(glob.glob(pat))
    print(f"样本 {len(files)} 份：{pat}\n")
    rows = []
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        sp, regions = d.get("system_prompt", ""), d.get("regions") or []
        old_b = len(sp.encode("utf-8"))
        print(f"== {f.split('/')[-1]}  model={d.get('model_requested')}"
              f"  system={old_b:,}B")
        bounds = region_bounds(sp, regions)
        new_sp = strip_regions(sp, bounds)
        moved = sum(e - s for s, e, *_ in bounds) + 2 * len(bounds)
        new_b = len(new_sp.encode("utf-8"))
        print(f"  → 新 system={new_b:,}B（迁出 {moved:,}B 到 turn_tail，"
              f"−{(old_b - new_b) / old_b * 100:.1f}%）\n")
        rows.append((f, sp, new_sp, old_b, new_b))
    print("=" * 60)
    print("相邻快照对：旧 LCP → 新 LCP（UTF-8 字节）")
    for (fa, sa, na, *_), (fb, sb, nb, *__) in zip(rows, rows[1:]):
        o, n = lcp(sa, sb), lcp(na, nb)
        flag = "  <— 清单处分叉" if o < n else ""
        print(f"  {fa.split('_')[-1][:12]}→{fb.split('_')[-1][:12]}"
              f"  旧 {o:,}B → 新 {n:,}B{flag}")


if __name__ == "__main__":
    main()
