"""元提示词事实审计：拿 `prompt_facts` 对照现有提示词，输出不一致清单。

用法：``.venv/Scripts/python.exe scripts/prompt_audit.py``

四类检查：

1. **错误码**：代码里用到的每个 `err("...")` 是否都在 `ERROR_CODES` 里，
   且提示词是否都写到了（模型会收到全部 code，少写一个它就不知道怎么办）。
2. **消息类型**：注册表全部类型，提示词是否都写到了。
3. **幽灵**：提示词里提到的工具名，注册表里是否真有。
4. **按卡型覆盖**：各类卡的实际工具面，提示词是否都提了、有没有提它拿不到的。
"""

from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api import card_tools, prompt_facts, prompts  # noqa: E402
from src.storage import card_store  # noqa: E402

WORK = "work_027"


def _codes_in_source() -> set[str]:
    out: set[str] = set()
    for p in glob.glob("src/**/*.py", recursive=True):
        s = Path(p).read_text(encoding="utf-8", errors="replace")
        out |= set(re.findall(r'\berr\(\s*"([a-z_]+)"', s))
    return out


def main() -> int:
    base = prompts.BASE_PROMPT
    auto = prompts.AUTO_BASE_PROMPT
    both = base + "\n" + auto
    problems: list[str] = []

    # ---- 1. 错误码
    print("=" * 62)
    print("1. 错误码")
    src = _codes_in_source()
    undeclared = sorted(src - set(prompt_facts.ERROR_CODES))
    unused = sorted(set(prompt_facts.ERROR_CODES) - src)
    print(f"   代码里用到的 code（{len(src)}）：{sorted(src)}")
    print(f"   事实源声明的（{len(prompt_facts.ERROR_CODES)}）："
          f"{sorted(prompt_facts.ERROR_CODES)}")
    if undeclared:
        print(f"   ✗ 代码用了但事实源没声明：{undeclared}")
        problems.append(f"错误码未声明：{undeclared}")
    if unused:
        print(f"   ! 声明了但代码没用：{unused}（留着无妨，删掉更干净）")
    missing = [c for c in prompt_facts.ERROR_CODES if c not in both]
    if missing:
        print(f"   ✗ 提示词没写：{missing}")
        problems.append(f"提示词漏错误码：{missing}")

    # ---- 2. 消息类型
    print("=" * 62)
    print("2. 消息类型")
    for code, disp in prompt_facts.msg_types():
        ok = code in both
        print(f"   {'✓' if ok else '✗'} {code:16s} {disp}")
        if not ok:
            problems.append(f"提示词漏消息类型：{code}")

    # ---- 3. 幽灵工具
    print("=" * 62)
    print("3. 幽灵（提示词提到、注册表没有）")
    reg = set(card_tools.TOOL_REGISTRY)
    # 只认「形如 tool_name(」的写法，且看起来像工具名
    cand = set(re.findall(r"\b([a-z][a-z_0-9]{3,})\(", both))
    ghosts = sorted(n for n in cand if n not in reg and "_" in n)
    if ghosts:
        print(f"   ✗ {ghosts}")
        problems.append(f"幽灵工具：{ghosts}")
    else:
        print("   ✓ 无")

    # ---- 4. 按卡型覆盖
    print("=" * 62)
    print("4. 按卡型覆盖（实际工具面 vs 提示词）")
    try:
        cards = card_store.iter_card_metas(WORK)
    except Exception as exc:  # noqa: BLE001
        print(f"   跳过（读不到 {WORK}）：{exc}")
        cards = {}
    for cid in ("card_061", "fn_004"):
        if cid not in cards:
            continue
        actual = {t["name"] for t in card_tools.build_tool_schemas(cid, WORK, cards)}
        meta = cards[cid]
        txt = auto if meta.get("auto_mode") else base
        not_mentioned = sorted(n for n in actual if n not in txt)
        cannot = sorted(n for n in actual if n in txt)  # 占位，下面才算反向
        # 反向：提示词提到但该卡拿不到
        listed = set(re.findall(r"^- ([a-z_0-9]+)\(", txt, re.M))
        ghost_for_card = sorted(listed - actual)
        print(f"   {cid}（{meta.get('card_type')}）实际 {len(actual)} 件")
        print(f"     实际有、提示词没提 {len(not_mentioned)}：{not_mentioned}")
        print(f"     提示词列了、它拿不到 {len(ghost_for_card)}：{ghost_for_card}")
        if ghost_for_card:
            problems.append(f"{cid} 提示词列了拿不到的工具：{ghost_for_card}")

    # ---- 汇总
    print("=" * 62)
    if problems:
        print(f"共 {len(problems)} 类不一致：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("全部一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
