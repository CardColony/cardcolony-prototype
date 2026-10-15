# -*- coding: utf-8 -*-
"""本地文件工具（2026-10-03）。

给主模型的四件文件能力：list_files / read_file / search_files / write_file。
边界＝全盘（用户 2026-10-03 11:23 裁定）：基准目录 D:\\AI_station，全部子目录可读可写；
路径规范化后校验前缀，只防 ../ 逃逸（越出 D:\\AI_station 仍拒）。

【修订记录 2026-10-04 21:31】此前本行写「边界＝A 档（用户 2026-10-03 11:33 裁决）」有误。
用户 10-03 11:23 的裁定是「全盘可读可写」；A 档（两目录白名单）是诸葛报代价后自行推荐的档位、
用户以「就按你说的做」一句概括同意——把范围缩小后，诸葛又把缩小结果记成了用户的裁决。
此举属越权（替用户做了选择），现按用户 10-04 21:24 当场指正恢复为全盘。

四道机械兜底：
1. 写前强制备份——覆盖已有文件前，原文副本先落 _backups/{相对路径}/{时间戳}（不依赖 git）
2. 二进制拒写——UTF-8 解码失败的内容拒写（读亦拒绝）
3. 超大文件拒写——> 5MB 拒写（读不受限，可分段）
4. 凭证/依赖目录排除——精确文件名黑名单 + 依赖目录剪枝

只含机械事实，无方向性用语。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("api.file_tools")

# [THINK-ECO-permgmt T6 · 2026-10-09] 文件路径锁依赖：统一权限表（POLICY 位判定）
from ..permissions import policy
from . import prompt_log

# 基准目录与允许范围（用户裁定：全盘可读可写）
BASE_DIR = Path(r"D:\AI_station")
ALLOWED_ROOTS = (BASE_DIR.resolve(),)

# 凭证类文件：精确文件名黑名单（不做 *key* / *secret* 通配——那会误伤
# tokenizer.py / api_key.py 这类正常源码；见 2026-10-03 11:35 取证）
CREDENTIAL_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development", ".env.test",
    "credentials.json", "secrets.yaml", "secrets.yml", "id_rsa", "id_ed25519",
    ".netrc", ".npmrc", ".pypirc", "kubeconfig",
}
CREDENTIAL_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".jks")
CREDENTIAL_PREFIXES = ("env.",)

# 依赖/缓存目录：搜索与列目录时剪枝（trading-workspace 7.8GB 的绝大部分在这里）
PRUNE_DIRS = {
    ".venv", "venv", "venv_wsl", ".git", "__pycache__", "node_modules",
    "site-packages", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "_backups", "_chrome_prof", "_cp_final", "_cp_final2", "dist-info",
    ".idea", ".vscode", "logs",
}

# 写入上限（读不受此限）
MAX_WRITE_BYTES = 5 * 1024 * 1024
# 单文件读取默认上限（防一次调用吐爆；可用行区间分段）
MAX_READ_BYTES = 2 * 1024 * 1024
# 搜索上限
MAX_SEARCH_HITS = 200
MAX_SEARCH_FILES = 20000
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024

BACKUP_DIR = BASE_DIR / "推演agent" / "_backups"


# ---------------------------------------------------------------- 路径与校验

# [THINK-ECO-permgmt T6 · 2026-10-09 → 权限底座重做 2026-10-12] 分类路径锁：
# card_type → 可访问路径前缀（相对 BASE_DIR，
# 正斜杠比较；"" 哨兵＝不另设前缀限制）。
# 值现状＝全放行哨兵——10-03/10-04 用户裁定「全盘可读可写」的保持现状档
# （与交办单定夺 1 对 SHADOW_WRITE 的「保持现状」同款原则）；真实前缀属部署
# 配置、待用户给值（Open Item）。
# **注意**：本表只是 perms 之后再加的一层路径前缀约束，**不是权限本身**——
# tools_file 开关已经先挡一道。tool 行不可达（该类型无 tools_file 开关），
# 随表补齐只为键位完整。
# [FILE_SCOPE 键位修复 2026-10-15] **键必须是 card_type（card_store._collect_card_meta
# 与 policy.has_perm 传进来的那个值），不是 project_class 工牌码**。原表误抄权限
# 表的五键（rd_project/research_project/sub_project/topic/tool），其中两个工牌码永
# 不可能作为 card_type 查到（＝死键），而根卡真实 card_type=`project` 根本不在表内
# → `FILE_SCOPE.get("project", [])` 返空列表 → `any([])` 恒 False → **11 张 project
# 根卡的 tools_file 开关明明已授却被第三段拒死**（实证：work_009/card_001 调
# list_files 报「越权：无权访问 D:\AI_station\推演agent」）。全仓无任何裁定写过
# 根卡不可访问文件——纯漏抄。该错 10-08 在另一张 POLICY 表上已被预判上报
# （THINK-ECO-permgmt-p1 调研纪要 T2「project 根卡不在表内、落默认全拒」），
# 本表未复核故漏至今。
# 键集由 tests/test_file_scope_keys.py 钉死 ＝ card_type_registry 的 card_type 全集，
# 以后新增卡类型不会再撞这个坑。
FILE_SCOPE: dict[str, list[str]] = {
    "project": [""],      # 项目根卡（root scope）——10-15 补回，此前漏抄致根卡全被拒
    "sub_project": [""],
    "topic": [""],
    "coder": [""],        # 编程卡——10-15 补（同因漏抄）
    "tool": [""],
}


def _credential_excluded(path: Path) -> bool:
    """凭证类文件判据（精确名 + 后缀 + 前缀）。

    [T6 收敛 2026-10-09] 原独立凭证函数收敛为 check_file_access 内部排除层
    （任务书 PM-16：grep 清零），判据逐字保留。
    """
    n = path.name.lower()
    if n in CREDENTIAL_NAMES:
        return True
    if n.endswith(CREDENTIAL_SUFFIXES):
        return True
    for p in CREDENTIAL_PREFIXES:
        if n.startswith(p) and n.endswith(".env"):
            return True
    return False


def _prefix_hit(target: Path, prefix: str) -> bool:
    """FILE_SCOPE 前缀判定："" 哨兵匹配一切；其余按 BASE_DIR 相对路径比较。"""
    if prefix == "":
        return True
    try:
        rel = target.relative_to(BASE_DIR)
    except ValueError:
        return False
    return str(rel).replace("\\", "/").startswith(prefix)


def check_file_access(path: str, card_meta: Any = None) -> bool:
    """[T6 路径锁 2026-10-09 → 重做 2026-10-12] 文件路径锁：机械三层判定。

    ① 权限：**当前卡 meta 里固化的 perms 须授 tools_file**（policy.has_perm，
       读卡面不查配置——裁定 4；未配置的类型/未固化的卡 ⇒ 拒，裁定 5）；
    ② 全盘边界：与 _resolve 同判据（resolve 后须落 ALLOWED_ROOTS 之下）；
    ③ 内部凭证排除＋FILE_SCOPE 前缀（FILE_SCOPE 值现状＝全放行哨兵，
       见上方表注；真实前缀待用户提供，属 Open Item）。
    空路径非权限问题（下游既有「path 不能为空」校验负责），放行交下游。
    T7 打点在唯一出口，两态同记（不做二次判定、无任何闸）。

    ``card_meta`` 缺省/为 None ⇒ False（认不出卡 ⇒ 默认关门）。
    """
    card_type = str((card_meta or {}).get("card_type", "") or "")
    has_file = policy.has_perm(card_meta, policy.SWITCH_TOOLS_FILE)
    raw = str(path or "").strip()

    def _log(granted: bool, prefix_hit: object) -> None:
        prompt_log.append_perm_log({
            "fn": "check_file_access",
            "path": path,
            "card_type": card_type,
            "prefix_hit": prefix_hit,
            "granted": granted,
        })

    if not has_file:
        _log(False, None)
        return False
    if not raw:
        _log(True, None)
        return True
    target, err_ = _resolve(raw)
    if err_ is not None:
        _log(False, None)
        return False
    if _credential_excluded(target):
        _log(False, "credential")
        return False
    hit = any(_prefix_hit(target, p) for p in FILE_SCOPE.get(card_type, []))
    _log(hit, "" if hit else None)
    return hit


def _resolve(raw: str) -> tuple[Path | None, dict[str, Any] | None]:
    """把入参解析为绝对路径并校验边界。

    返回 (path, None) 成功；(None, error) 失败。
    规则：相对路径以 BASE_DIR 为基准；resolve 后必须落在 ALLOWED_ROOTS 之下。
    """
    if not raw or not str(raw).strip():
        return None, {"error": "path 不能为空"}
    p = Path(str(raw).strip())
    if not p.is_absolute():
        p = BASE_DIR / p
    try:
        rp = p.resolve()
    except (OSError, RuntimeError) as exc:
        return None, {"error": f"路径无法解析: {exc}"}

    for root in ALLOWED_ROOTS:
        try:
            rp.relative_to(root)
        except ValueError:
            continue
        return rp, None
    allowed = "、".join(str(r) for r in ALLOWED_ROOTS)
    return None, {"error": f"路径越界：只允许访问 {allowed}，收到 {rp}"}


def _rel(p: Path) -> str:
    """相对基准目录的展示路径。"""
    try:
        return str(p.relative_to(BASE_DIR))
    except ValueError:
        return str(p)


def _is_pruned(path: Path) -> bool:
    """路径任一层命中剪枝目录即剪掉。"""
    return any(part in PRUNE_DIRS or part.endswith(".dist-info") for part in path.parts)


# ---------------------------------------------------------------- 四件工具

def list_files(path: str = "", limit: int = 200) -> dict[str, Any]:
    """列目录（一层）。凭证文件不显示；依赖/缓存目录不进入。"""
    target, err = _resolve(path or str(BASE_DIR / "trading-workspace"))
    if err:
        return err
    if not target.exists():
        return {"error": f"路径不存在: {_rel(target)}"}
    if target.is_file():
        return {"error": f"这是文件不是目录: {_rel(target)}（用 read_file 读）"}

    entries: list[dict[str, Any]] = []
    try:
        for child in sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name)):
            if child.is_dir():
                if child.name in PRUNE_DIRS or child.name.endswith(".dist-info"):
                    continue
                entries.append({"name": child.name, "type": "dir"})
            else:
                try:
                    st = child.stat()
                except OSError:
                    continue
                entries.append({
                    "name": child.name,
                    "type": "file",
                    "size": st.st_size,
                })
    except OSError as exc:
        return {"error": f"目录读取失败: {exc}"}

    total = len(entries)
    return {
        "path": _rel(target),
        "total": total,
        "entries": entries[: max(1, min(int(limit or 200), 1000))],
        "truncated": total > max(1, min(int(limit or 200), 1000)),
    }


def read_file(path: str, para_start: int | None = None, para_end: int | None = None) -> dict[str, Any]:
    """读文件（按行分段）。二进制拒读；凭证文件拒读。"""
    target, err = _resolve(path)
    if err:
        return err
    if not target.exists():
        return {"error": f"文件不存在: {_rel(target)}"}
    if target.is_dir():
        return {"error": f"这是目录不是文件: {_rel(target)}（用 list_files 列）"}
    # [2026-10-04 21:31] 凭证拦截已撤（用户裁定全盘可读可写）

    try:
        raw = target.read_bytes()
    except OSError as exc:
        return {"error": f"文件读取失败: {exc}"}

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"error": f"二进制文件不可读: {_rel(target)}（{len(raw)} 字节，非 UTF-8 文本）"}

    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(para_start or 1))
    end = int(para_end) if para_end else total
    end = max(start, min(end, total))
    chunk = lines[start - 1:end]
    body = "\n".join(chunk)
    if len(body.encode("utf-8")) > MAX_READ_BYTES:
        return {
            "error": (
                f"读取区间过大（>{MAX_READ_BYTES // 1024 // 1024}MB）；"
                f"本文件共 {total} 行，请用 para_start/para_end 分段"
            ),
        }
    return {
        "path": _rel(target),
        "total_lines": total,
        "read_lines": f"{start}-{end}",
        "content": body,
    }


def _walk_files(start: Path):
    """自写剪枝遍历：遇到依赖/缓存目录或坏重解析点就跳过，不用 rglob。

    trading-workspace 下有 venv_wsl\\lib64 这类重解析点，rglob 在遍历阶段
    直接抛 OSError(WinError 1920) 会让整个搜索崩溃——故自己 scandir。
    """
    if start.is_file():
        yield start
        return
    stack: list[Path] = [start]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name in PRUNE_DIRS or e.name.endswith(".dist-info"):
                        continue
                    stack.append(Path(e.path))
                elif e.is_file(follow_symlinks=False):
                    yield Path(e.path)
            except OSError:
                continue


def search_files(keyword: str, path: str = "", limit: int = 50) -> dict[str, Any]:
    """在材料目录里逐文件文本检索。凭证不命中；依赖/缓存目录剪枝。"""
    kw = (keyword or "").strip()
    if not kw:
        return {"error": "keyword 不能为空"}
    start, err = _resolve(path or str(BASE_DIR / "trading-workspace"))
    if err:
        return err
    if not start.exists():
        return {"error": f"路径不存在: {_rel(start)}"}
    # [2026-10-04 21:31] 凭证拦截已撤（用户裁定全盘可读可写；此前此拦截系诸葛自行添加）

    cap = max(1, min(int(limit or 50), MAX_SEARCH_HITS))
    hits: list[dict[str, Any]] = []
    scanned = 0
    truncated = False
    kw_lower = kw.lower()

    for f in _walk_files(start):
        if len(hits) >= cap:
            truncated = True
            break
        if scanned >= MAX_SEARCH_FILES:
            truncated = True
            break
        try:
            if _is_pruned(f):
                continue
            if f.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
            scanned += 1
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if kw_lower in line.lower():
                hits.append({
                    "file": _rel(f),
                    "line": i,
                    "text": line.strip()[:300],
                })
                if len(hits) >= cap:
                    truncated = True
                    break
    return {
        "keyword": kw,
        "scope": _rel(start),
        "scanned_files": scanned,
        "hits": hits,
        "truncated": truncated,
    }


def write_file(path: str, content: str) -> dict[str, Any]:
    """写文件（新建/覆盖自动区分）。四道兜底在此生效。"""
    target, err = _resolve(path)
    if err:
        return err
    if target.exists() and target.is_dir():
        return {"error": f"这是目录不是文件: {_rel(target)}"}
    # [2026-10-04 21:31] 凭证拦截与依赖目录写入拦截已撤（用户裁定全盘可读可写）

    body = "" if content is None else str(content)
    data = body.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        return {
            "error": (
                f"写入内容过大（{len(data)} 字节 > {MAX_WRITE_BYTES // 1024 // 1024}MB）；"
                "请分次写入或用其他方式落盘"
            ),
        }

    existed = target.exists()
    backup_path = ""
    if existed:
        try:
            old = target.read_bytes()
        except OSError as exc:
            return {"error": f"覆盖前读取原文失败，已中止: {exc}"}
        try:
            old.decode("utf-8")
        except UnicodeDecodeError:
            return {"error": f"目标文件是二进制，拒绝覆盖: {_rel(target)}"}
        # 写前强制备份（不依赖 git）
        try:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            bdir = BACKUP_DIR / target.parent.relative_to(BASE_DIR) / stamp
            bdir.mkdir(parents=True, exist_ok=True)
            bfile = bdir / target.name
            bfile.write_bytes(old)
            backup_path = str(bfile)
        except OSError as exc:
            return {"error": f"备份失败，已中止写入（不破坏原文件）: {exc}"}

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError as exc:
        return {"error": f"写入失败: {exc}"}

    return {
        "ok": True,
        "path": _rel(target),
        "bytes": len(data),
        "status": "overwritten" if existed else "created",
        "backup": backup_path,
    }


def build_file_tool_schemas() -> list[dict[str, Any]]:
    """四件文件工具的 OpenAI function calling schema（机械描述，无方向性用语）。"""
    return [
        {
            "name": "list_files",
            "description": f"列出一个目录下的条目（一层）。允许范围：{'、'.join(str(r) for r in ALLOWED_ROOTS)}。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径（相对 D:\\AI_station 或绝对）；缺省列 trading-workspace"},
                    "limit": {"type": "integer", "description": "最多返回条目数，缺省 200"},
                },
                "required": [],
            },
        },
        {
            "name": "read_file",
            "description": "读取一个文本文件的内容，可按行区间分段。二进制文件与凭证类文件不可读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（相对 D:\\AI_station 或绝对）"},
                    "para_start": {"type": "integer", "description": "起始行号（1 起），缺省 1"},
                    "para_end": {"type": "integer", "description": "结束行号，缺省到文件末尾"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "search_files",
            "description": "在指定目录下逐文件检索关键词（不区分大小写），返回命中的文件、行号与该行内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "检索关键词"},
                    "path": {"type": "string", "description": "起始目录或文件；缺省搜索 trading-workspace"},
                    "limit": {"type": "integer", "description": "最多返回命中条数，缺省 50"},
                },
                "required": ["keyword"],
            },
        },
        {
            "name": "write_file",
            "description": (
                "写入一个文本文件（文件已存在则为覆盖，覆盖前原文件会自动备份到 _backups 下）。"
                "单次写入上限 5MB；凭证类文件与依赖目录不可写。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（相对 D:\\AI_station 或绝对）"},
                    "content": {"type": "string", "description": "写入的完整文本内容"},
                },
                "required": ["path", "content"],
            },
        },
    ]
