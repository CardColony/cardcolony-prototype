# -*- coding: utf-8 -*-
"""T4 · 产物落库（EC-041 / EC-042 / EC-043 / EC-044）。

落点：``works/{work_id}/toolcards/{card_id}/``（EC-042）
命名：``{两位序号}-{stepId}-{名称}-{版本号}.{ext}``，**版本化不覆盖**——
重做同一步落 ``-02``，``-01`` 原样保留（EC-043）。

**「落库是记录不是闸门」**（EC-043 逐字继承）：

- **不判质量**——产物好不好只有模型能说，代码一个字都不评；
- **不拦推进**——落库失败**不抛**、不阻断步骤推进、不阻断终态收口；
- **失败不抛**——任何异常都收进失败账 ``_failures.json`` 并返回 IO 事实。

失败账与「终态重试一次」（EC-044）：终态收口时把失败账里的条目**再试一次**，
**不试第三次**；重试仍失败就留在账里，**不产出任何对内容的结论**——它只是
一条「两次 IO 都没成」的记录。

**关于 ZhuGe 的 ``ARTIFACT_CHAINS``（EC-023/任务书 T4 明令）**：ZhuGe 的
``chain-artifact-store.ts`` L33 有 ``ARTIFACT_CHAINS = ['proposal','taskbook']``
这样的**硬编码白名单**——只有名单里的链才落产物。本移植**刻意不复制它**：
**任何带产物的步骤都落库**，本模块内不存在任何「这条链/这一步算不算产物」的
白名单或开关。落不落库由**调用方把产物交过来**这个动作本身决定，不由代码
预先判断该交不该交。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..storage import card_store

logger = logging.getLogger("toolcard.artifact_store")

MANIFEST_NAME = "manifest.json"
FAILURES_NAME = "_failures.json"

# 进程内 manifest 登记（键＝card_id）。与 driver 的运行时 Map 同生命周期：
# 一次运行内逐步登记，终态由 finalize 一次性落盘。
_manifest: dict[str, list[dict[str, Any]]] = {}
# 失败账登记（键＝card_id）；同一内存 + 磁盘双载（终态重试一次要用）
_failures: dict[str, list[dict[str, Any]]] = {}

# 文件名里不该出现的字符（路径分隔符等）。**这是结构性护栏，不是内容判定**：
# 它保证产物落在本目录内，与「目标步不存在」同类。
_UNSAFE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_.-]")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def artifact_dir(work_id: str, card_id: str) -> Path:
    """产物目录：``works/{work_id}/toolcards/{card_id}/``。

    走 ``card_store.WORKS_DIR`` ⇒ 测试沙箱（conftest 改写 WORKS_DIR）自动跟随。
    """
    return card_store.WORKS_DIR / work_id / "toolcards" / card_id


def _safe(name: str) -> str:
    """把名称里的路径分隔符等换掉（结构性护栏，保证落点在本目录内）。"""
    s = _UNSAFE_RE.sub("_", str(name or "")).strip("._-")
    return s or "artifact"


def build_filename(seq: int, step_id: str, name: str, version: int, ext: str) -> str:
    """按命名规则拼文件名（纯拼字符串，零判断）。"""
    e = str(ext or "").lstrip(".")
    suffix = f".{e}" if e else ""
    return f"{int(seq):02d}-{_safe(step_id)}-{_safe(name)}-{int(version):02d}{suffix}"


def next_version(work_id: str, card_id: str, seq: int, step_id: str, name: str) -> int:
    """算下一个版本号＝同前缀已有最大版本 +1（默认 1）。**版本化不覆盖**。

    这是**目录扫描的机械结果**，不是「这个产物好不好／该不该重做」的判断。
    """
    d = artifact_dir(work_id, card_id)
    if not d.exists():
        return 1
    prefix = f"{int(seq):02d}-{_safe(step_id)}-{_safe(name)}-"
    mx = 0
    for p in d.glob(f"{prefix}*"):
        m = re.match(rf"^{re.escape(prefix)}(\d{{2}})", p.name)
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


def _atomic_write(path: Path, content: str) -> None:
    """原子写：tmp → rename（同目录同盘，rename 在 POSIX/NTFS 上均为原子替换）。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _append_failure(work_id: str, card_id: str, entry: dict[str, Any]) -> None:
    """失败入账（内存 + ``_failures.json``），**不抛**。"""
    lst = _failures.setdefault(card_id, [])
    lst.append(entry)
    try:
        d = artifact_dir(work_id, card_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / FAILURES_NAME).write_text(
            json.dumps(lst, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("失败账落盘异常 card=%s: %s", card_id, exc)


def commit_artifact(
    work_id: str, card_id: str, seq: int, step_id: str, name: str,
    content: str, ext: str = "md",
) -> dict[str, Any]:
    """把一份产物落盘并登记进 manifest。**返回 IO 事实，绝不抛**。

    返回 ``{"ok":写成功没有, "path":..., "filename":..., "seq":..,
    "step_id":.., "at":.., "bytes":..}``。

    ``ok`` **只是写盘成功没有**——它**不代表**产物写得好、内容对不对、这步
    算不算完成（EC-043：不判质量、不拦推进、失败不抛）。

    **不去重、不判重**：同一 seq+step+name 再交一次 ⇒ 落 ``-02``，``-01`` 保留。
    """
    at = _now_iso()
    version = next_version(work_id, card_id, seq, step_id, name)
    fname = build_filename(seq, step_id, name, version, ext)
    d = artifact_dir(work_id, card_id)
    entry: dict[str, Any] = {
        "序号": int(seq),
        "stepId": str(step_id),
        "文件": fname,
        "提交时刻": at,
        "字节数": len(content.encode("utf-8")),
        "写入成功": False,
    }
    try:
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write(d / fname, content)
        entry["写入成功"] = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("产物落盘失败 card=%s file=%s: %s", card_id, fname, exc)
        _append_failure(work_id, card_id, {
            "序号": int(seq), "stepId": str(step_id), "文件": fname,
            "提交时刻": at, "字节数": entry["字节数"], "写入成功": False,
            "原因": str(exc), "重试次数": 0,
        })
    _manifest.setdefault(card_id, []).append(entry)
    return {
        "ok": entry["写入成功"], "path": str(d / fname), "filename": fname,
        "seq": int(seq), "step_id": str(step_id), "at": at,
        "bytes": entry["字节数"],
    }


def manifest_entries(card_id: str) -> list[dict[str, Any]]:
    """取本卡 manifest 条目（内存副本）。只读，不判内容。"""
    return list(_manifest.get(card_id, []))


def failures_of(card_id: str) -> list[dict[str, Any]]:
    """取本卡失败账（内存副本）。只读，不判内容。"""
    return list(_failures.get(card_id, []))


def write_manifest(work_id: str, card_id: str) -> bool:
    """把 manifest 落成文件。**返回写盘成功没有**。不抛、不阻断收口。"""
    try:
        d = artifact_dir(work_id, card_id)
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write(d / MANIFEST_NAME, json.dumps(
            {
                "card_id": card_id,
                "updated_at": _now_iso(),
                "artifacts": manifest_entries(card_id),
            },
            ensure_ascii=False, indent=2,
        ))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("manifest 落盘失败 card=%s: %s", card_id, exc)
        return False


def retry_pending_once(work_id: str, card_id: str) -> dict[str, Any]:
    """终态重试一次（EC-044）。

    把失败账里的条目**再落一次盘**（同 seq/step/name ⇒ 版本号自动 +1），
    **只试这一次**：不试第二次、不试第三次。仍失败就留在失败账里，
    **不产出任何对内容的结论**。

    返回 ``{"retried":条数, "ok":成功条数}``——**纯 IO 计数**。
    """
    pending = [f for f in _failures.get(card_id, []) if not f.get("写入成功")]
    ok = 0
    for f in pending:
        entry = _manifest.setdefault(card_id, [])
        try:
            version = next_version(
                work_id, card_id, f["序号"], f["stepId"],
                Path(f["文件"]).stem.split("-")[-2] if "-" in f["文件"] else "artifact",
            )
            fname = build_filename(
                f["序号"], f["stepId"], Path(f["文件"]).stem, version,
                Path(f["文件"]).suffix,
            )
            d = artifact_dir(work_id, card_id)
            d.mkdir(parents=True, exist_ok=True)
            _atomic_write(d / fname, str(f.get("内容", "")))
            f["写入成功"] = True
            f["重试次数"] = int(f.get("重试次数", 0)) + 1
            entry.append({
                "序号": int(f["序号"]), "stepId": str(f["stepId"]),
                "文件": fname, "提交时刻": _now_iso(),
                "字节数": int(f.get("字节数", 0)), "写入成功": True,
            })
            ok += 1
        except Exception as exc:  # noqa: BLE001
            f["重试次数"] = int(f.get("重试次数", 0)) + 1
            logger.warning(
                "产物终态重试仍失败 card=%s file=%s: %s", card_id, f["文件"], exc
            )
    return {"retried": len(pending), "ok": ok}


def artifact_paths_for_step(work_id: str, card_id: str, step_id: str) -> list[str]:
    """取某步已落盘产物的**文件路径**列表（代码自己落过的东西）。

    给 step_prompt 用：注入「上一步我落盘的文件是 xxx」——**这是代码自己做过
    的事的记录**（EC-050 允许）。⚠️ 本函数**只列路径、不判文件是否存在、
    不判非空、不判好坏**：它返回的是 manifest 里登记过的文件名，
    不是「这个文件好不好」或「这步做完没」。

    注意：返回的路径**不保证此刻仍可读**——那属于 IO 事实，代码不据此下结论。
    """
    return [
        e["文件"] for e in _manifest.get(card_id, [])
        if e.get("stepId") == step_id
    ]


def reset(card_id: str) -> None:
    """清掉某卡的进程内登记（供测试隔离；生产不调）。"""
    _manifest.pop(card_id, None)
    _failures.pop(card_id, None)


def latest_step_id(work_id: str, card_id: str) -> Optional[str]:
    """manifest 里最后一个登记的 stepId（**最后一个被登记的**，不是「最后一个该有的」）。

    ⚠️ 这是**机械取末条**，不是「判断进行到哪一步」——后者归模型。
    """
    entries = _manifest.get(card_id, [])
    return entries[-1]["stepId"] if entries else None
