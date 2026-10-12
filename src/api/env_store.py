# -*- coding: utf-8 -*-
"""`.env` 读写专用模块（模型设置 · 第二步）。

为什么独立成模块：`src/api/file_tools.py` 有凭证黑名单红线（模型的文件工具不许碰凭证），
而设置页**就是要**管密钥。两者不同源、不同权限域，混在一个文件里会让红线审查失效。
独立成模块 → 安全边界一眼可辨：这里只碰 `NAME=value` 行，其余文件一律不读不写。

四个动作（设置界面用）：
    read_keys(path)                  解析 .env → {名: 值}
    mask(value)                      "sk-abc…wxyz" → "····wxyz"（只留末 4 位）
    list_key_status(path, names)     逐键报 {configured, tail}——**明文永不出本模块**
    set_key(path, name, value)       备份 → 原子写 → os.environ 刷新

三条硬要求（方案 §五·风险 2）：
    1. 写完必须 `os.environ[name] = value`：gateway 是**调用时**才 os.environ.get(...)
       （gateway.py:687-688），只写文件不刷新进程，新密钥要重启才生效——违背「热生效」。
    2. 保留其它行与注释原样：逐行替换，不重排、不删注释、不重排空行。
    3. 键名校验 `^[A-Za-z_][A-Za-z0-9_]*$`：防注入非法键名（能把 .env 写花）。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("api.env_store")

BASE_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = BASE_DIR / ".env"
BACKUP_DIR = BASE_DIR / "_backups"

# 合法环境变量名（防注入：只允许字母/下划线开头的标识符）
# 用 fullmatch 而非 match+`$`：`$` 会匹配「末尾换行之前」，`"NAME\n"` 会被放行
# ——那等于往 .env 里注入一条断裂的赋值行。
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# .env 行语法：可选 export 前缀、NAME=VALUE。VALUE 可能带引号。
_ASSIGN_RE = re.compile(r"^(?P<lead>\s*)(?P<export>export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<rest>.*)$")

# 无需加引号即可安全解析的取值（python-dotenv 原样读入）
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_\-./:+=@]*$")


def _is_valid_name(name: str) -> bool:
    return bool(name) and bool(_NAME_RE.fullmatch(name))


def _unquote(raw: str) -> str:
    """剥掉取值两侧引号与行尾注释（仅引号闭合时才剥，容错非标准写法）。"""
    val = raw.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        return val[1:-1]
    return val


def _quote(value: str) -> str:
    """安全取值直出；含空格/井号/引号等则双引号包裹并转义。"""
    if _SAFE_VALUE_RE.match(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read_keys(path: Optional[Path] = None) -> dict[str, str]:
    """解析 .env，返回 {名: 值}。

    忽略空行、`#` 开头的注释行与非法行。缺文件 → 空 dict（fail-open，不抛）。
    这是**唯一**返回明文的函数——调用方（设置接口）只取末 4 位，绝不外传。
    """
    p = Path(path) if path else ENV_PATH
    out: dict[str, str] = {}
    try:
        text = p.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ASSIGN_RE.match(line)
        if not m:
            continue
        out[m.group("name")] = _unquote(m.group("rest"))
    return out


def mask(value: str) -> str:
    """打码：只留末 4 位，其余一律盖成圆点。

    "sk-abcdefghijkl" → "····ijkl"（长度不足时全盖，不泄露任何原字符）。
    """
    val = value or ""
    if not val:
        return ""
    tail = val[-4:] if len(val) > 4 else ""
    return "····" + tail


def list_key_status(path: Optional[Path], names: list[str]) -> dict[str, dict[str, Any]]:
    """逐键报状态：{名: {configured: bool, tail: str}}。

    **明文永不出这里**：已配置且够长才给末 4 位，否则 tail 为空串。
    """
    keys = read_keys(path)
    out: dict[str, dict[str, Any]] = {}
    for name in names or []:
        val = keys.get(name) or ""
        out[name] = {"configured": bool(val), "tail": val[-4:] if val else ""}
    return out


def backup_env(path: Optional[Path] = None) -> Optional[Path]:
    """把当前 .env 备份到 _backups/.env_YYYYmmdd_HHMMSS。缺文件返 None。"""
    p = Path(path) if path else ENV_PATH
    if not p.exists():
        return None
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dst = BACKUP_DIR / f".env_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(p, dst)
        return dst
    except OSError as exc:
        logger.warning(".env 备份失败（不阻断写入）: %s", exc)
        return None


def set_key(path: Optional[Path], name: str, value: str) -> None:
    """写一个键：备份 → 原子写 → 刷新进程内 os.environ。

    - 逐行替换 `NAME=...`，找不到则**追加到末尾**（不重排、不动注释）
    - 原子写：临时文件 → os.replace（同目录，保证同卷）
    - 写完 `os.environ[name] = value`：gateway 调用时读 os.environ，不刷新就等下次重启
    - 键名非法 → ValueError（调用方转 400）
    """
    if not _is_valid_name(name):
        raise ValueError(f"非法环境变量名: {name!r}")

    p = Path(path) if path else ENV_PATH

    try:
        original = p.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        original = ""

    newline = f"{name}={_quote(value)}"
    lines = original.splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith("#"):
            continue
        m = _ASSIGN_RE.match(line)
        if m and m.group("name") == name:
            lead = m.group("lead") or ""
            exp = m.group("export") or ""
            lines[i] = f"{lead}{exp}{newline}"
            replaced = True
            break
    if not replaced:
        # 文件末尾若无换行，先补一个，避免和末行粘连
        if lines and original and not original.endswith("\n"):
            lines.append("")
        lines.append(newline)

    body = "\n".join(lines) + "\n"

    backup_env(p)

    fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # 关键：刷新进程内环境变量，否则新密钥要重启才生效（方案 §五·风险 2）
    os.environ[name] = value
    logger.info(".env 已更新: %s（值不落日志）", name)


def remove_key(path: Optional[Path], name: str) -> bool:
    """删一个键（注释掉而非删行，保住用户对该键的说明）。返是否命中。"""
    if not _is_valid_name(name):
        raise ValueError(f"非法环境变量名: {name!r}")
    p = Path(path) if path else ENV_PATH
    try:
        original = p.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return False

    lines = original.splitlines()
    hit = False
    for i, line in enumerate(lines):
        if line.strip().startswith("#"):
            continue
        m = _ASSIGN_RE.match(line)
        if m and m.group("name") == name:
            lines[i] = "# [删除于设置界面] " + line
            hit = True
            break
    if not hit:
        return False

    backup_env(p)
    body = "\n".join(lines) + "\n"
    fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    os.environ.pop(name, None)
    return True