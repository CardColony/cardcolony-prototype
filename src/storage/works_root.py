"""works 根目录：默认仓库 works/；ECO_WORKS_DIR 给隔离拓扑用。

[联调任务书工作一] 双 runner 隔离取证要求 works 与生产分开。写在一个
共享模块里，6 个 storage 模块同取一个值，避免各自读环境变量出现分叉。
生产不设 ECO_WORKS_DIR ⇒ 与旧 `BASE_DIR / "works"` 逐字等价。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]


def works_dir() -> Path:
    env = os.environ.get("ECO_WORKS_DIR", "").strip()
    return Path(env).resolve() if env else BASE_DIR / "works"
