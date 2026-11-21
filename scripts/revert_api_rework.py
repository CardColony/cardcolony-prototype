# -*- coding: utf-8 -*-
"""把 api.js 临时退回「返工修复前」的那一版（用于取修前失败的证据）。

只回退三处（返工项），其余（批次一的共享 GET 通道本身）保持不变：
  ① 2a：条目只在 promise 的 finally 里摘表（不在「最后一个消费者退订」那一刻摘）
  ② 2b：头到即解除 abort 连线（正文读取不受取消影响）
  ③ 2c：纪元在写**之前**推进

用法：
  python scripts/revert_api_rework.py on    # 退回修前版
  python scripts/revert_api_rework.py off   # 还原（从 .api_fixed.bak）
"""
from __future__ import annotations

import pathlib
import shutil
import sys

API = pathlib.Path(__file__).resolve().parents[1] / "static" / "esm" / "api.js"
BAK = API.with_suffix(".js.reworkfix.bak")

R1 = ("""  if (entry && entry.dropped) entry = null;
""", "")

R2 = ("""    if (entry.consumers.size === 0) _dropEntry(entry);""",
      """    if (entry.consumers.size === 0) {
          try { entry.ctrl.abort(); } catch (e) { /* 零阻断 */ }
        }""")

R3 = ("""      /* ★ holdSignal（返工 2b）：保留「外部取消 → 派生 signal」的连线直到
         **正文读完**。否则头一到就解除连线 ⇒ 消费者这时退订，正文还在拉
         （首屏那条时序 1.3MB ⇒ 白拉一整条）。 */
      holdSignal: true,
""", "")

R4 = ("""  try {
    return await _callDirect(method, url, body, o);
  } finally {
    /* ★ 纪元在**写落定之后**推进，不是之前（2026-11-20 返工）。
       之前推进 ⇒「写期间发起的 GET」与「提交后的重读」拿到**同一个纪元**
       ⇒ 重读会并到写期间那条上 ⇒ **保存成功后仍然读到旧值**（实测复现）。
       写落定后推进 ⇒ 写期间那条（纪元 E）与提交后重读（纪元 E+1）**不合并** ✓
       （「提交后重读必须拿到写后的新值」这条语义由此守住）。
       ★ 写失败也推进：写可能部分落地，宁可作废在途读也不给旧值。 */
    _epoch++;
  }
""", """  _epoch++;
  return _callDirect(method, url, body, o);
""")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "off"
    if mode == "off":
        if not BAK.exists():
            print("[skip] 没有 .reworkfix.bak，无需还原")
            return 0
        shutil.copy2(BAK, API)
        BAK.unlink()
        print("[ok] 已还原 api.js（修复后版本）")
        return 0
    src = API.read_text(encoding="utf-8")
    BAK.write_text(src, encoding="utf-8")
    for old, new in (R1, R2, R3, R4):
        if old not in src:
            print(f"[warn] 找不到待回退片段：{old.strip()[:50]}")
        src = src.replace(old, new)
    API.write_text(src, encoding="utf-8")
    print("[ok] 已退回「返工修复前」的 api.js（备份在 %s）" % BAK.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
