# -*- coding: utf-8 -*-
"""影子卡存储（影子卡机制 2026-10-03，依据 SHADOW_CARD_RULINGS.md 九条裁定）。

影子卡 = 正卡的配对过程性附卡：
- 一正卡一影子，落 works/{work_id}/shadows/{owner_card_id}.md（文件名＝正卡编号）
- 由正卡（模型）经 update_shadow 工具主动书写，系统不自动写内容
- 随正卡生命周期存续：建卡即派生（失败不阻断）、删卡同删
- 读侧跟随现有 can_read（不另设判据）；写侧只有自己的正卡（工具签名无 card_id）
- 人不能直接改：不提供人的写通道，不提供删影子工具
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import card_store, data_snapshot
from .hash_utils import apply_hash, parse_front_matter

logger = logging.getLogger("storage.shadow")

# 影子正文三节标题（用户 15:05 原话三类，代拍 4）
SHADOW_BODY_TEMPLATE = """## 进度

（暂无记录）

## 产物清单

（暂无记录）

## 迭代历史

（暂无记录）
"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _q(value: str) -> str:
    """YAML 双引号字符串（与 card_store._q 同族）。"""
    return json.dumps(value, ensure_ascii=False)


def _shadows_dir(work_id: str) -> Path:
    """影子目录唯一来源（与 card_store._cards_dir 同构）。"""
    return card_store.WORKS_DIR / work_id / "shadows"


def shadow_path(work_id: str, owner_card_id: str) -> Path:
    """影子文件路径：works/{work_id}/shadows/{owner_card_id}.md。"""
    return _shadows_dir(work_id) / f"{owner_card_id}.md"


def shadow_id_of(owner_card_id: str) -> str:
    """影子的可寻址 id（进 _collect_card_meta 字典的键；文件名仍是正卡编号）。"""
    return f"shadow_{owner_card_id}"


def render_shadow(owner_card_id: str, body: str, created_at: str, updated_at: str) -> str:
    """按固定字段顺序渲染影子全文（content_hash 由 apply_hash 回填）。

    front matter 七字段（代拍 1）；正文＝调用方给的三节内容（新建＝空模板）。
    """
    fm = [
        "---",
        f"card_id: {_q(shadow_id_of(owner_card_id))}",
        'card_type: "shadow"',
        f"parent: {_q(owner_card_id)}",
        f"shadow_of: {_q(owner_card_id)}",
        f"created_at: {_q(created_at)}",
        f"updated_at: {_q(updated_at)}",
        'content_hash: ""',
        "---",
    ]
    return "\n".join(fm) + "\n\n" + body.rstrip("\n") + "\n"


def _meta_of(work_id: str, owner_card_id: str) -> Optional[dict[str, Any]]:
    """轻量读影子 meta（不锁不校验，与 card_store._meta_of 同族）；无影子返回 None。"""
    p = shadow_path(work_id, owner_card_id)
    if not p.exists():
        return None
    try:
        meta, _ = parse_front_matter(p.read_text(encoding="utf-8"))
        if meta.get("card_type") != "shadow":
            return None
        meta.setdefault("shadow_of", owner_card_id)
        return meta
    except Exception:  # noqa: BLE001
        return None


async def read_shadow_raw(work_id: str, owner_card_id: str) -> Optional[str]:
    """读影子文件全文（纯读：不校哈希、不写回——影子无 [外部修改] 追加逻辑）。

    影子不存在返回 None。
    """
    p = shadow_path(work_id, owner_card_id)
    if not p.exists():
        return None
    return await asyncio.to_thread(p.read_text, encoding="utf-8")


async def write_shadow_raw(
    work_id: str, owner_card_id: str, content: str, reason: str = "shadow update"
) -> None:
    """锁内写影子全文（路径锁与 update_card 共用 card_store._get_lock）＋快照钩点。"""
    p = shadow_path(work_id, owner_card_id)
    await asyncio.to_thread(_shadows_dir(work_id).mkdir, parents=True, exist_ok=True)
    lock = card_store._get_lock(p)
    await card_store._acquire(lock)
    try:
        await asyncio.to_thread(p.write_text, content, encoding="utf-8")
    finally:
        lock.release()
    logger.info("影子写入 work=%s card=%s (%s)", work_id, owner_card_id, reason)
    await data_snapshot.snapshot(f"[snapshot][{work_id}] {reason}: {owner_card_id}")


async def derive_shadow(work_id: str, owner_card_id: str) -> dict[str, Any]:
    """为正卡派生影子（共用函数：create_card 钩点与存量补跑脚本都调它）。

    幂等：影子已存在则跳过（绝不覆盖既有内容）。
    正卡不存在 → {"error": ...}；成功 → {"ok": True, ...}。
    失败向上抛异常，由调用方决定是否阻断（create_card 钩点捕获后不阻断）。
    """
    if not card_store._card_path(work_id, owner_card_id).exists():
        return {"error": f"正卡不存在: {owner_card_id}"}
    p = shadow_path(work_id, owner_card_id)
    if p.exists():
        return {"ok": True, "existed": True, "owner_card_id": owner_card_id, "path": str(p)}
    now = _now_iso()
    content = apply_hash(render_shadow(owner_card_id, SHADOW_BODY_TEMPLATE, now, now))
    await write_shadow_raw(work_id, owner_card_id, content, reason="shadow create")
    return {"ok": True, "created": True, "owner_card_id": owner_card_id, "path": str(p)}


async def update_shadow(work_id: str, owner_card_id: str, body: str) -> dict[str, Any]:
    """正卡写自己的影子（覆盖写正文三节；created_at 保留，updated_at 刷新）。"""
    raw = await read_shadow_raw(work_id, owner_card_id)
    now = _now_iso()
    if raw is None:
        content = render_shadow(owner_card_id, body, now, now)
    else:
        meta, _ = parse_front_matter(raw)
        created_at = meta.get("created_at") or now
        content = render_shadow(owner_card_id, body, str(created_at), now)
    content = apply_hash(content)
    await write_shadow_raw(work_id, owner_card_id, content, reason="shadow update")
    return {"ok": True, "owner_card_id": owner_card_id, "updated_at": now}


async def remove_shadow(work_id: str, owner_card_id: str) -> bool:
    """删影子（唯一调用点：card_store.delete_card 钩点；无工具暴露）。无影子返回 False。"""
    p = shadow_path(work_id, owner_card_id)
    if not p.exists():
        return False
    lock = card_store._get_lock(p)
    await card_store._acquire(lock)
    try:
        await asyncio.to_thread(p.unlink)
    finally:
        lock.release()
    logger.info("影子删除 work=%s card=%s", work_id, owner_card_id)
    await data_snapshot.snapshot(f"[snapshot][{work_id}] shadow delete: {owner_card_id}")
    return True
