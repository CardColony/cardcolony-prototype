# -*- coding: utf-8 -*-
"""同一段读 `.env` 的 python 代码：**旧代码 / 新代码并排跑**（python 工具这一侧）。

**为什么不重启实例做这一对**：5713 是**共享**隔离实例，同期有别的卡在里面跑实验
（日志里能看到别人 channel_walk 的请求）。为了取证把实例降到旧版本再起，等于把
别人正在跑的实验一起换掉尺子。所以 python 工具这一侧改用同源对照：
`git show <改动前的提交>:src/api/card_tools.py` 取出旧文件，按包路径
`src.api._card_tools_pre_fix` 载入（相对 import 照常解析），对**同一个沙箱根**
分别调新旧两版的 `_h_python`，贴两次返回。

**它证的是什么**：同一入参、同一沙箱、同一段代码，只改被测代码版本 ⇒ 差异只能
来自本次改动。子进程是真起的（真的 exec 了那段 python）。

bash 工具那一侧不需要这套：真机实例上已用同一段代码前后各跑一次（见
evidence_cred_before.json / evidence_cred_after.json）。

用法：
    python verify/5713/cred_guard_oldcode_probe.py --pre-commit cab7535
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MARK = "sk-probe-5713-credguard-0001"

# 同一段代码：跑沙箱里那份自检脚本（用 runpy 才拿得到 __file__，exec 拿不到）
CODE_RUNPY = ("import runpy; "
              "runpy.run_path('trading-workspace/env_check.py', run_name='__main__')")
# 直读：静态闸该拒的那条
CODE_DIRECT = "print(open('.env', encoding='utf-8').read())"


def _load(path: Path, name: str):
    """按**包内名字**载入外部文件：相对 import（..permissions 等）照常解析。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _call(mod, code: str) -> dict:
    return await mod._h_python("w", "c1", {"code": code}, {})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pre-commit", default="cab7535", help="改动前的提交（旧 card_tools）")
    ap.add_argument("--sandbox-root",
                    default=r"D:\AI_station\推演agent\.worktrees\5713_sandbox")
    ap.add_argument("--out", default="verify/5713/evidence_cred_python_tool.json")
    args = ap.parse_args()

    import asyncio

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

    from src.api import card_tools as new_mod
    from src.api import credential_guard, file_tools

    sb = Path(args.sandbox_root)
    (sb / ".env").write_text(
        f"DEEPSEEK_API_KEY={MARK}\nPROBE_NOTE=probe-only-not-a-real-secret\n",
        encoding="utf-8")
    (sb / "trading-workspace").mkdir(parents=True, exist_ok=True)
    (sb / "trading-workspace" / "env_check.py").write_text(
        'from pathlib import Path\n'
        'p = Path(__file__).resolve().parents[1] / ".env"\n'
        'for line in p.read_text(encoding="utf-8").splitlines():\n'
        '    if "=" in line:\n'
        '        k, v = line.split("=", 1)\n'
        '        print(f"{k.strip()} = {v.strip()}")\n',
        encoding="utf-8")

    old_src = subprocess.run(
        ["git", "show", f"{args.pre_commit}:src/api/card_tools.py"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", check=True).stdout
    tmp = Path(os.environ.get("TEMP", ".")) / "_card_tools_pre_fix.py"
    tmp.write_text(old_src, encoding="utf-8")

    # 同一个沙箱根：新旧两版都指向 5713_sandbox
    real_base = file_tools.BASE_DIR
    file_tools.BASE_DIR = sb
    try:
        old_mod = _load(tmp, "src.api._card_tools_pre_fix")
        credential_guard.reset_cache()
        ev: dict = {"at": datetime.now().isoformat(timespec="seconds"),
                    "pre_commit": args.pre_commit, "mark": MARK, "cases": []}
        for label, code in (("runpy_indirect", CODE_RUNPY), ("direct_env", CODE_DIRECT)):
            old_r = asyncio.run(_call(old_mod, code))
            new_r = asyncio.run(_call(new_mod, code))
            credential_guard.reset_cache()
            rec = {
                "case": label,
                "code": code,
                "pre_fix": old_r,
                "post_fix": new_r,
                "pre_fix_leaks": MARK in json.dumps(old_r, ensure_ascii=False),
                "post_fix_leaks": MARK in json.dumps(new_r, ensure_ascii=False),
                "post_fix_has_redacted": credential_guard.REDACTED in json.dumps(
                    new_r, ensure_ascii=False),
            }
            ev["cases"].append(rec)
            print(f"--- {label}")
            print("  旧:", json.dumps(old_r, ensure_ascii=False)[:300])
            print("  新:", json.dumps(new_r, ensure_ascii=False)[:300])
    finally:
        file_tools.BASE_DIR = real_base
        credential_guard.reset_cache()
        try:
            tmp.unlink()
        except OSError:
            pass

    ev["verdict"] = ("FIXED"
                     if all(c["pre_fix_leaks"] and not c["post_fix_leaks"] for c in ev["cases"])
                     else "NOT_PROVEN")
    Path(args.out).write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n判定：" + ev["verdict"])
    print("证据：" + args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())