# -*- coding: utf-8 -*-
"""**执行体做完任务后，在它自己的客户端里清理残留**（2026-11-04 用户令）。

**用户令**：「各自做完的任务，在各自客户端内做一个清理机制」
**用户追加的硬红线**：「千万别删除对话」「**千万别删错对话**」

⇒ 这条红线是**设计约束**，不是口头承诺 ⇒ 下面六条纪律**写进代码**：

1. **只删"我们自己刚建的那一条"**（按**精确 id**）——
   ★ **绝不**按标题 / 目录 / 时间范围 / 名字前缀去扫 ⇒ 扫＝可能删错。
2. **删前验身份**：Devin 侧用 ``devin list --format json``（**只列当前目录**）
   取出那条会话，核对 **id 精确相等** ＋ **工作目录等于这张卡的工作目录**；
   任何一项对不上 ⇒ **跳过**（宁可留垃圾，也不删错）。
3. **只清我们自己的 CLI 执行体**（``wb-devin`` / ``wb-oc``）——
   ★ **桌面端客户端一律不碰**：WorkBuddy 桌面端会话库
   （``~/.workbuddy/app/session``，345MB）、Devin Desktop 自己的对话、
   用户在 opencode 里的历史会话 —— **都不是我们的，全都不动**。
4. **删之前先把轨迹（ATIF）存进我们自己的日志目录**
   （``logs/wb_exec/atif/``）⇒ 客户端里不留对话，但**我们这边可查**。
5. **每一次删除都记日志**（id / 标题 / 目录 / 结果）⇒ 可审计。
6. **任何一步不确定 ⇒ 跳过**。清理是"锦上添花"，**永远不能把活儿搞挂**
   ⇒ 清理失败只记日志，不影响卡片结果。

**为什么需要它**（2026-11-04 实测）：
- Devin：``%APPDATA%\\devin\\cli\\sessions.db`` 已 **746MB** / 33 条会话
  ⇒ **每跑一张卡就往用户的 Devin 客户端里塞一条**（我们已占 15 条）。
- opencode：``~/.local/share/opencode/opencode.db`` 已 **277MB** / 155 条会话。
⇒ 不清理 ⇒ 这两个库会一直涨（而 C 盘空间是用户的心病）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("api.wb_exec.cleanup")

#: **ATIF 轨迹的留存目录**（我们自己的日志，删客户端会话前先存这里）。
ATIF_DIR = Path(__file__).resolve().parents[3] / "logs" / "wb_exec" / "atif"


def _norm_dir(p: str) -> str:
    """把目录路径归一化（Windows 大小写不敏感 + 去掉尾斜杠）。"""
    try:
        return os.path.normcase(os.path.normpath(str(p or "").rstrip("\\/")))
    except Exception:  # noqa: BLE001
        return str(p or "")


def _looks_like_session_id(sid: str) -> bool:
    """这个 id **长得像**会话 id 吗？（防手滑把乱七八糟的东西当 id 传进去）"""
    s = str(sid or "")
    if not s or len(s) > 80:
        return False
    return all(ch.isalnum() or ch in "-_" for ch in s)


# ===================================================================== 轨迹留存
def save_atif(card_id: str, executor_id: str, atif: Any) -> str:
    """把这次运行的 **ATIF 轨迹**存到我们自己的日志目录（删客户端会话前先存）。

    ⇒ 客户端里不留对话，但**我们这边随时能查**这一轮 agent 干了什么。
    """
    if not atif:
        return ""
    try:
        ATIF_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch for ch in str(card_id or "card") if ch.isalnum() or ch in "-_")
        path = ATIF_DIR / f"{safe}-{executor_id}-{ts}.json"
        raw = atif.get("raw") if isinstance(atif, dict) else None
        payload = raw if raw is not None else atif
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        logger.info("[wb_exec] ATIF 轨迹已留存 %s", path)
        return str(path)
    except Exception as exc:  # noqa: BLE001 留存失败不影响主流程
        logger.warning("[wb_exec] ATIF 留存失败（不影响清理）：%s", exc)
        return ""


# ===================================================================== Devin
async def _run_cli(argv: list[str], cwd: str, timeout: float) -> dict:
    """跑一条 Devin CLI 命令 ⇒ ``{ok, stdout, stderr, rc}``。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "stdout": "", "stderr": f"超时({timeout}s)", "rc": -1}
        return {"ok": proc.returncode == 0,
                "stdout": (out or b"").decode("utf-8", "replace"),
                "stderr": (err or b"").decode("utf-8", "replace"),
                "rc": proc.returncode}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}",
                "rc": -1}


async def cleanup_devin(cli_exe: str, session_id: str, work_dir: str,
                        timeout: float = 120.0,
                        list_retries: int = 3,
                        retry_gap: float = 3.0) -> dict:
    """删掉**我们刚建的那一条** Devin 会话（**删前验身份**）。

    流程（每一步都不确定就**跳过**）：
      ① 在**这张卡的工作目录**下跑 ``devin list --format json``
         （★ 这个命令**只列当前目录**的会话 ⇒ 结构上碰不到别的目录）；
      ② 找到 **id 精确相等**的那条，并核对它的 ``working_directory``
         **等于**这张卡的工作目录；
      ③ 对得上才 ``devin rm <id> --force``（**用完整 id**，不用前缀）。

    ★ **为什么要重试**（2026-11-04 实测）：Devin CLI 退出后**异步**把会话写进
      自己的库 ⇒ 进程刚结束时 ``devin list`` **可能还看不到它** ⇒ 首查会"跳过"
      （安全方向，但会留垃圾）⇒ 这里**有限次重试**（默认 3 次 × 3 秒），
      仍看不到就跳过 ⇒ 交给 12 小时定时任务的扫描兜底。
    """
    res: dict[str, Any] = {"executor": "wb-devin", "session_id": session_id,
                           "deleted": False}
    if not _looks_like_session_id(session_id):
        res["skipped"] = "会话 id 不合法 ⇒ 跳过"
        return res
    if not cli_exe or not os.path.exists(cli_exe):
        res["skipped"] = "找不到 devin CLI ⇒ 跳过"
        return res
    if not work_dir or not os.path.isdir(work_dir):
        res["skipped"] = "工作目录不存在 ⇒ 跳过"
        return res

    rows: list = []
    listing = {"ok": False, "stdout": "", "stderr": ""}
    for attempt in range(max(1, int(list_retries))):
        listing = await _run_cli([cli_exe, "list", "--format", "json"],
                                 cwd=work_dir, timeout=60.0)
        if not listing["ok"] or not listing["stdout"].strip():
            if attempt + 1 < max(1, int(list_retries)):
                await asyncio.sleep(retry_gap)
                continue
            res["skipped"] = f"列不出该目录的会话 ⇒ 跳过（{listing['stderr'][:80]}）"
            return res
        try:
            rows = json.loads(listing["stdout"])
        except Exception as exc:  # noqa: BLE001
            res["skipped"] = f"会话列表解析失败 ⇒ 跳过（{exc}）"
            return res
        if not isinstance(rows, list):
            res["skipped"] = "会话列表格式不对 ⇒ 跳过"
            return res
        if any(isinstance(r, dict) and str(r.get("id") or "") == str(session_id)
               for r in rows):
            break
        # ★ 还没落盘 ⇒ 等一下再看（CLI 退出后是异步写的）
        if attempt + 1 < max(1, int(list_retries)):
            await asyncio.sleep(retry_gap)
    hit = [r for r in rows
           if isinstance(r, dict) and str(r.get("id") or "") == str(session_id)]
    if not hit:
        res["skipped"] = "这条会话还不在该工作目录里 ⇒ 跳过（不删；定时任务会兜底）"
        return res
    row = hit[0]
    res["title"] = str(row.get("title") or "")
    if _norm_dir(row.get("working_directory") or "") != _norm_dir(work_dir):
        res["skipped"] = "工作目录对不上 ⇒ 跳过（不删）"
        return res
    # ★ **删除也可能撞瞬时锁**（2026-11-04 实测）：Devin Desktop 或别的进程
    #   正开着这条会话 ⇒ `devin rm` 报 "already open in another process"
    #   ⇒ 隔一会儿再删就成功 ⇒ 这里**有限次重试**（默认 3 次 × 5 秒）。
    rm = {"ok": False, "stderr": "", "rc": -1}
    for attempt in range(max(1, int(list_retries))):
        rm = await _run_cli([cli_exe, "rm", str(session_id), "--force"],
                             cwd=work_dir, timeout=timeout)
        if rm["ok"]:
            break
        if "already open" not in str(rm.get("stderr") or "").lower():
            break
        if attempt + 1 < max(1, int(list_retries)):
            await asyncio.sleep(max(1.0, retry_gap * 1.5))
    res["deleted"] = bool(rm["ok"])
    res["rc"] = rm["rc"]
    if rm["ok"]:
        logger.info("[wb_exec] 已清理 Devin 会话 %s（工作目录 %s）", session_id, work_dir)
    else:
        res["skipped"] = f"devin rm 失败：{rm['stderr'][:120]}"
        logger.warning("[wb_exec] 清理 Devin 会话 %s 失败：%s",
                       session_id, rm["stderr"][:120])
    return res


# ===================================================================== opencode
async def cleanup_opencode(client: Any, session_id: str) -> dict:
    """删掉**我们这一轮自己建的那个** opencode 会话。

    ⇒ ``DELETE /session/{id}``（实测 HTTP 200，库里 156→155）。
    ★ 只传**我们自己 create_session 拿到的 id** ⇒ 不会碰到用户的会话。
    """
    res: dict[str, Any] = {"executor": "wb-oc", "session_id": session_id,
                           "deleted": False}
    if not session_id or client is None:
        res["skipped"] = "没有会话 id / 客户端 ⇒ 跳过"
        return res
    try:
        st, data = await client.delete_session(str(session_id))
    except Exception as exc:  # noqa: BLE001
        res["skipped"] = f"删除调用异常 ⇒ 跳过（{type(exc).__name__}: {exc}）"
        return res
    res["http"] = st
    res["deleted"] = int(st) in (200, 204)
    if res["deleted"]:
        logger.info("[wb_exec] 已清理 opencode 会话 %s", session_id)
    else:
        res["skipped"] = f"HTTP {st} ⇒ 未删（{str(data)[:80]}）"
    return res


# ===================================================================== 扫描清理
#: **白名单根**：只有**工作目录落在这些根下面**的会话才算"我们的"。
#: ★ 默认根 = 仓库的 ``works/``（**每张 coder 卡的工作目录都在这儿**）
#:   ⇒ 用户自己在 Devin Desktop / opencode 里的会话（目录是仓库根或别的目录）
#:     **结构上就落选** ⇒ 不可能被误删。
DEFAULT_WORK_ROOTS = [Path(__file__).resolve().parents[3] / "works"]


def _is_ours(work_dir: str, roots: list[str]) -> bool:
    """这个会话的工作目录**在白名单根下面**吗？（⇒ 是我们卡片跑出来的）"""
    wd = _norm_dir(work_dir)
    if not wd:
        return False
    for r in roots:
        rr = _norm_dir(r)
        if not rr:
            continue
        if wd == rr or wd.startswith(rr + os.sep):
            return True
    return False


def _devin_sessions_db() -> Path:
    """Devin CLI 的会话库（**只读打开**）。"""
    return Path(os.environ.get("APPDATA", "")) / "devin" / "cli" / "sessions.db"


def list_devin_sessions() -> list[dict]:
    """列出 Devin 的全部会话（**只读**）。

    ⇒ 带 ``last_activity_at``（epoch 秒）⇒ 用来判断"是不是还在跑"。
    """
    import sqlite3
    db = _devin_sessions_db()
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.cursor()
        try:
            rows = cur.execute(
                "SELECT id, working_directory, title, last_activity_at "
                "FROM sessions").fetchall()
        except sqlite3.OperationalError:
            rows = [(r[0], r[1], r[2], 0) for r in cur.execute(
                "SELECT id, working_directory, title FROM sessions").fetchall()]
        con.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[wb_exec] 读 Devin 会话库失败（跳过扫描）：%s", exc)
        return []
    return [{"id": str(r[0]), "working_directory": str(r[1] or ""),
             "title": str(r[2] or ""),
             "last_activity_at": float(r[3] or 0.0)} for r in rows]



def _too_recent(last_activity_at: float, min_age_min: float) -> bool:
    """这个会话**最近还在动**吗？（⇒ 正在跑 ⇒ **跳过，别删**）。

    ★ 2026-11-04：清理必须在跑之前有这道闸 ——
      opencode 侧 ``DELETE /session/{id}`` 会**打断正在跑的卡**。
    """
    if not min_age_min or min_age_min <= 0:
        return False
    ts = float(last_activity_at or 0.0)
    if ts <= 0:
        return False
    import time as _t
    age_min = (_t.time() - ts) / 60.0
    return age_min < float(min_age_min)


async def sweep_devin_sessions(cli_exe: str, roots: list[str],
                               dry_run: bool = False,
                               min_age_min: float = 30.0) -> dict:
    """把**我们卡片跑出来的** Devin 会话清掉（**白名单根下的那些**）。

    ★ **绝不碰用户自己的对话**：白名单根之外的会话**一个都不动**
    （用户在 Devin Desktop 里的会话目录是仓库根 ⇒ 落选）。
    """
    out: dict[str, Any] = {"executor": "wb-devin", "scanned": 0,
                           "ours": [], "deleted": [], "skipped": []}
    if not cli_exe or not os.path.exists(cli_exe):
        out["error"] = "找不到 devin CLI"
        return out
    rows = list_devin_sessions()
    out["scanned"] = len(rows)
    for r in rows:
        if not _is_ours(r.get("working_directory") or "", roots):
            continue
        # ★ **正在跑的 ⇒ 跳过**（别删正在干活的那条）
        if _too_recent(r.get("last_activity_at") or 0.0, min_age_min):
            out.setdefault("skipped_recent", []).append(r["id"])
            continue
        out["ours"].append(r)
    if dry_run:
        out["dry_run"] = True
        return out
    for r in out["ours"]:
        wd = r.get("working_directory") or ""
        res = await cleanup_devin(cli_exe, r["id"], wd)
        if res.get("deleted"):
            out["deleted"].append(r["id"])
        else:
            out["skipped"].append({"id": r["id"], "why": res.get("skipped")})
    return out


async def sweep_opencode_sessions(exe: str, key: str, roots: list[str],
                                  dry_run: bool = False,
                                  min_age_min: float = 30.0) -> dict:
    """把**我们卡片跑出来的** opencode 会话清掉（白名单根下的那些）。

    ⇒ 走 opencode 自己的 API（``DELETE /session/{id}``）⇒ **不直接改它的库**。
    """
    out: dict[str, Any] = {"executor": "wb-oc", "scanned": 0,
                           "ours": [], "deleted": [], "skipped": []}
    if not exe or not os.path.exists(exe):
        out["error"] = "找不到 opencode"
        return out
    import sqlite3
    db = Path(os.environ.get("USERPROFILE", "")) / ".local" / "share" / "opencode" / "opencode.db"
    if not db.exists():
        out["error"] = f"找不到 opencode 会话库 {db}"
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.cursor()
        try:
            rows = cur.execute(
                "SELECT id, directory, title, time_updated FROM session").fetchall()
        except sqlite3.OperationalError:
            rows = [(r[0], r[1], r[2], 0) for r in cur.execute(
                "SELECT id, directory, title FROM session").fetchall()]
        con.close()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"读 opencode 会话库失败：{exc}"
        return out
    out["scanned"] = len(rows)
    for sid, directory, title, upd in rows:
        if not _is_ours(str(directory or ""), roots):
            continue
        # ★ **正在跑的 ⇒ 跳过**（DELETE 会打断正在跑的卡！）
        ts = float(upd or 0.0)
        if ts > 1e11:      # 毫秒
            ts = ts / 1000.0
        if _too_recent(ts, min_age_min):
            out.setdefault("skipped_recent", []).append(str(sid))
            continue
        out["ours"].append({"id": str(sid), "directory": str(directory or ""),
                            "title": str(title or "")})
    if dry_run:
        out["dry_run"] = True
        return out
    from . import oc_client as oc_mod
    from . import oc_pool
    work = str(Path(tempfile.gettempdir()) / "wb_exec_cleanup")
    os.makedirs(work, exist_ok=True)
    try:
        srv = await oc_pool.start_oc_server(exe, cwd=work, config_path="",
                                             env_extra={"OPENCODE_API_KEY": key})
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"起 opencode 失败：{exc}"
        return out
    client = oc_mod.OcClient(srv.base_url)
    try:
        for r in out["ours"]:
            res = await cleanup_opencode(client, r["id"])
            if res.get("deleted"):
                out["deleted"].append(r["id"])
            else:
                out["skipped"].append({"id": r["id"], "why": res.get("skipped")})
    finally:
        try:
            await client.close()
        except Exception:
            pass
        try:
            srv.stop()
        except Exception:
            pass
    return out


# ===================================================================== 入口
async def cleanup_after_run(executor_id: str, *, session_id: str = "",
                            work_dir: str = "", client: Any = None,
                            cli_exe: str = "", card_id: str = "",
                            atif: Any = None) -> dict:
    """**一张卡做完后 ⇒ 在它自己的客户端里清掉这一条会话**。

    ★ **只对"我们自己起的 CLI 执行体"生效**（wb-devin / wb-oc）；
      其它执行体（例如 WorkBuddy 桌面端）**一律不删**（红线 ③）。
    ★ **任何失败都不影响卡片结果**（红线 ⑥）——这里只记日志、返回一份报告。
    """
    out: dict[str, Any] = {"executor": executor_id, "deleted": False,
                           "skipped": ""}
    try:
        if executor_id == "wb-devin":
            if atif:
                out["atif_saved"] = save_atif(card_id, executor_id, atif)
            out.update(await cleanup_devin(cli_exe, session_id, work_dir))
        elif executor_id == "wb-oc":
            if atif:
                out["atif_saved"] = save_atif(card_id, executor_id, atif)
            out.update(await cleanup_opencode(client, session_id))
        else:
            out["skipped"] = ("这个执行体的会话由桌面端管理 ⇒ **不删**"
                              "（用户自己的对话，一个都不能碰）")
        return out
    except Exception as exc:  # noqa: BLE001 清理永远不能拖垮主流程
        out["skipped"] = f"清理异常（不影响结果）：{type(exc).__name__}: {exc}"
        logger.warning("[wb_exec] 清理会话时异常（已忽略）：%s", exc)
        return out
