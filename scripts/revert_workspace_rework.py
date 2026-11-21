# -*- coding: utf-8 -*-
"""把 workspace.js 临时退回「返工修复前」的那一版（用于取修前失败的证据）。

只回退三处（返工项 #1 / #3），其余（批次一的窄投影/懒加载/并发）保持不变：
  ① #1：详情归属只比 card_id（不比 work_id）＋ 渲染前不校验当前选择
  ② #3：取卡失败只回滚地址栏（不退 WORK_ID / 画板 / 输入框）

用法：
  python scripts/revert_workspace_rework.py on    # 退回修前版
  python scripts/revert_workspace_rework.py off   # 还原（从 .reworkfix.bak）
"""
from __future__ import annotations

import pathlib
import shutil
import sys

WS = pathlib.Path(__file__).resolve().parents[1] / "static" / "workspace.js"
BAK = WS.with_suffix(".js.reworkfix.bak")

R1 = ("""        const curCid = currentCard ? (currentCard.meta || {}).card_id : "";
        if (currentCard && WORK_ID === wid && curCid === cid) currentCard = full;""",
      """        const curCid = currentCard ? (currentCard.meta || {}).card_id : "";
        if (currentCard && curCid === cid) currentCard = full;""")

R2 = ("""    const wantWid = WORK_ID;
    const wantCid = currentCard ? (currentCard.meta || {}).card_id : "";
    await ensureCardFull();
    if (!currentCard) return;
    const curCid = (currentCard.meta || {}).card_id;
    if (wantCid && (WORK_ID !== wantWid || curCid !== wantCid)) return;  // 已切走 ⇒ 不画
""", """    await ensureCardFull();
    if (!currentCard) return;
""")

R3 = ("""        const back = _lastGood;
        if (back && back.cid) {
          // ① 项目上下文
          try { WORK_ID = back.wid; window.__WORK_ID__ = back.wid; } catch (e0) { /* 零阻断 */ }
          // ② 对话画板
          try { bindBoard(back.wid, back.cid); } catch (e0) { /* 零阻断 */ }
          // ③ 输入框（会话键）
          try { switchComposer(sessionKeyOf(back.cid)); } catch (e0) { /* 零阻断 */ }
          // ④ 左栏选中态 ＋ 地址栏
          try { navStore.select.set(back.wid, back.cid); } catch (e0) { /* 零阻断 */ }
          try {
            history.replaceState(null, "",
              `/work/${back.wid}/card/${encodeURIComponent(back.cid)}`);
          } catch (e0) { /* 零阻断 */ }
          try { window.__CARD_ID__ = back.cid; } catch (e0) { /* 零阻断 */ }
          try {
            window.dispatchEvent(new CustomEvent("ui:card-changed", { detail: { cardId: back.cid } }));
          } catch (e0) { /* 零阻断 */ }
        } else {
          /* 没有「上一次成功打开」⇒ 退到项目根（首屏就是这条坏链接的场景）。 */
          try { history.replaceState(null, "", `/work/${WORK_ID}`); } catch (e0) { /* 零阻断 */ }
        }
""", """        const prevCid = (prevSel && prevSel.cid && prevSel.cid !== cardId) ? prevSel.cid : "";
        try {
          if (prevCid) {
            history.replaceState(null, "", `/work/${WORK_ID}/card/${encodeURIComponent(prevCid)}`);
          } else {
            history.replaceState(null, "", `/work/${WORK_ID}`);
          }
        } catch (e0) { /* 零阻断 */ }
        if (prevCid) {
          try { navStore.select.set(prevSel.wid, prevCid); } catch (e0) { /* 零阻断 */ }
          try { window.dispatchEvent(new CustomEvent("ui:card-changed", { detail: { cardId: prevCid } })); } catch (e0) { /* 零阻断 */ }
        }
""")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "off"
    if mode == "off":
        if not BAK.exists():
            print("[skip] 没有 .reworkfix.bak，无需还原")
            return 0
        shutil.copy2(BAK, WS)
        BAK.unlink()
        print("[ok] 已还原 workspace.js（修复后版本）")
        return 0
    src = WS.read_text(encoding="utf-8")
    BAK.write_text(src, encoding="utf-8")
    for old, new in (R1, R2, R3):
        if old not in src:
            print(f"[warn] 找不到待回退片段：{old.strip()[:60]}")
        src = src.replace(old, new)
    WS.write_text(src, encoding="utf-8")
    print("[ok] 已退回「返工修复前」的 workspace.js（备份在 %s）" % BAK.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
