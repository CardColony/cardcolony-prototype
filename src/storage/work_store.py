# -*- coding: utf-8 -*-
"""剧本（work）存储。一个 work = 一个独立推演剧本，含自己的 cards/sessions/canvas。

work.json 结构：
    {"work_id","title","work_card_id","ending_derived","created_at"}

剧本卡（work_card_id 指向）= 剧本的总架构卡，card_type="剧本"、parent=""。
work 不可删除（不提供删除函数）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("storage.work")

BASE_DIR = Path(__file__).resolve().parents[2]
WORKS_DIR = BASE_DIR / "works"

_WORK_DIR_RE = re.compile(r"^work_(\d{3,})$")


def _work_dir(work_id: str) -> Path:
    return WORKS_DIR / work_id


def _work_json(work_id: str) -> Path:
    return _work_dir(work_id) / "work.json"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _next_seq() -> int:
    max_seq = 0
    if WORKS_DIR.exists():
        for p in WORKS_DIR.iterdir():
            m = _WORK_DIR_RE.match(p.name)
            if m and p.is_dir():
                max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def list_works() -> list[dict[str, Any]]:
    """扫描 works/work_*/work.json，每项附 card_count（cards 目录 .md 文件数）。"""
    works: list[dict[str, Any]] = []
    if not WORKS_DIR.exists():
        return works
    for p in sorted(WORKS_DIR.iterdir()):
        if not (p.is_dir() and _WORK_DIR_RE.match(p.name)):
            continue
        jf = p / "work.json"
        if not jf.exists():
            continue
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("解析 work.json 失败，跳过 %s: %s", jf, exc)
            continue
        cards_dir = p / "cards"
        card_count = (
            len([f for f in cards_dir.glob("card_*.md")]) if cards_dir.exists() else 0
        )
        data["card_count"] = card_count
        works.append(data)
    return works


def read_work(work_id: str) -> Optional[dict[str, Any]]:
    """读 work.json；不存在返回 None。"""
    jf = _work_json(work_id)
    if not jf.exists():
        return None
    try:
        return json.loads(jf.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("解析 work.json 失败 %s: %s", jf, exc)
        return None


def write_work(work_id: str, data: dict[str, Any]) -> None:
    """写 work.json（覆盖）。"""
    _work_dir(work_id).mkdir(parents=True, exist_ok=True)
    _work_json(work_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def update_work(work_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
    """更新 work.json；仅允许 title、ending_derived 两个字段。"""
    data = read_work(work_id)
    if data is None:
        return None
    for key in ("title", "ending_derived"):
        if key in updates:
            data[key] = updates[key]
    write_work(work_id, data)
    return data


async def create_work(title: str) -> dict[str, Any]:
    """分配 work_{NNN}，建目录、写 work.json，然后创建剧本卡（card_type=剧本）。

    剧本卡 id 写回 work.json.work_card_id；ending_derived=false。
    任何一步失败：保留已创建的目录与文件，返回 {"error": ...}。
    """
    # 局部 import，避免与 card_store 形成顶层循环
    from . import card_store, git_auto_commit

    work_id = f"work_{_next_seq():03d}"
    wd = _work_dir(work_id)
    (wd / "cards").mkdir(parents=True, exist_ok=True)
    (wd / "sessions").mkdir(parents=True, exist_ok=True)

    now = _now_iso()
    work_data: dict[str, Any] = {
        "work_id": work_id,
        "title": title,
        "work_card_id": "",  # 占位，剧本卡创建后回填
        "ending_derived": False,
        "created_at": now,
    }
    write_work(work_id, work_data)

    # 创建剧本卡：parent="" 强制（card_store 内部对 card_type=剧本 会无视 parent 入参）
    result = await card_store.create_card(
        work_id, title, "用户直接创建",
        card_type="剧本", parent="", charter="", conclusion="",
    )
    if "error" in result:
        logger.error("create_work: 剧本卡创建失败 work=%s: %s", work_id, result["error"])
        return result
    work_card_id = result["meta"]["card_id"]
    work_data["work_card_id"] = work_card_id
    write_work(work_id, work_data)

    # [白板裁定 2026-10-02] 不再自动派生"目标"卡——系统不预置任何语义卡；
    # 卡从两个口子来：用户手建（前端）或模型经 card-proposal 建。
    # ending_derived 字段保留恒 false（存量兼容，前端 🏁 标志自然熄灭）。

    # [数据与代码分家 2026-10-03] 运行数据不再 git 提交（曾因分支操作被整体回退），改走独立快照
    from . import data_snapshot
    await data_snapshot.snapshot(f"[snapshot][{work_id}] create work: {title}")
    work_data = read_work(work_id) or work_data  # 取回 ending_derived 最新值
    work_data["card_count"] = len(
        list((_work_dir(work_id) / "cards").glob("card_*.md"))
    )
    return work_data


def work_card_id_of(work_id: str) -> str:
    """读取本 work 的剧本卡 id；不存在返回 ""。供 card_store 兜底 parent 用。"""
    data = read_work(work_id)
    if data is None:
        return ""
    return data.get("work_card_id") or ""
