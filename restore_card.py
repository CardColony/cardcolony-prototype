# -*- coding: utf-8 -*-
"""卡片恢复脚本：把指定卡片恢复到 git 历史中的任一版本。

用法（在项目根目录）：
  python restore_card.py <work_id> <card_id> [git_ref]
  git_ref 缺省 = 该卡最近一次被修改前一个版本（即"撤销最后一次改动"）。

不附加任何拦截；恢复本身也走 git auto-commit 落档（可再撤）。
"""
import asyncio
import io
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE = __file__.rsplit("\\", 1)[0]


async def main() -> None:
    if len(sys.argv) < 3:
        print("用法: python restore_card.py <work_id> <card_id> [git_ref]")
        sys.exit(2)
    work_id, card_id = sys.argv[1], sys.argv[2]
    ref = sys.argv[3] if len(sys.argv) > 3 else None
    rel = f"works/{work_id}/cards/{card_id}.md"

    def git(*args: str) -> str:
        r = subprocess.run(["git", *args], capture_output=True, text=True,
                           encoding="utf-8", cwd=BASE)
        if r.returncode != 0:
            print(f"git {' '.join(args)} 失败: {r.stderr.strip()}")
            sys.exit(1)
        return r.stdout.strip()

    # 找该文件的历史版本
    log = git("log", "--follow", "--pretty=format:%h %ad %s",
              "--date=format:%m-%d %H:%M", "--", rel)
    if not log:
        print(f"无历史: {rel}")
        sys.exit(1)
    lines = log.splitlines()
    print(f"{card_id} 历史 {len(lines)} 个版本（新→旧）:")
    for l in lines[:10]:
        print(f"  {l}")
    if ref is None:
        # 撤销最后一次改动 = 恢复到倒数第二个版本；只有一个版本则恢复它
        ref = lines[1].split()[0] if len(lines) > 1 else lines[0].split()[0]
        print(f"未指定 ref，恢复到倒数第二版本: {ref}")

    content = git("show", f"{ref}:{rel}")
    # 经存储层正规通道写回（带修改历史落痕），而非直接覆盖文件
    sys.path.insert(0, BASE)
    from src.storage import card_store, git_auto_commit
    await card_store.update_card(
        work_id, card_id, {"conclusion": content},
        f"restore_card 恢复到 {ref}",
    )
    await git_auto_commit.commit(f"[restore][{work_id}] {card_id} -> {ref}")
    size = len(content.encode("utf-8"))
    print(f"已恢复 {card_id} -> {ref}（{size} 字节正文，经 update_card 正规通道）")


asyncio.run(main())
