# -*- coding: utf-8 -*-
"""影子卡存储（影子卡机制 2026-10-03，依据 SHADOW_CARD_RULINGS.md 九条裁定）。

影子卡 = 正卡的配对过程性附卡：
- 一正卡一影子，落 works/{work_id}/shadows/{owner_card_id}.md（文件名＝正卡编号）
- 由正卡（模型）经 update_shadow 工具主动书写；另有**代码自动写的一条窄通道**：
  `append_to_section`（工具卡底座 T8 / EC-086~EC-114）——只在**工具卡终态时**
  把产物**路径指针**回传到 owner（主卡）影子卡的「## 产物清单」节，一次。
  该通道不改正文判断、不写结论、不搬运正文（用户 2026-10-09 12:54 裁定：
  「产物清单就是文档栏目」「自动记入」）。这是**设计变更**而非口径矛盾：
  「系统不自动写内容」收紧为「系统不自动写**正文与结论**」，
  路径指针的自动登记由 append_to_section 独占。
- 随正卡生命周期存续：建卡即派生（失败不阻断）、删卡同删
- 读侧走统一关口 READ 判定（不另设判据）；写侧只有自己的正卡（工具签名无 card_id），关口须授 SHADOW_WRITE
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


def iter_shadow_metas(work_id: str) -> dict[str, dict[str, Any]]:
    """轻量收集 work 下所有影子的 meta（公开接口，供跨层调用方批量读）。

    返回 {shadow_id: meta}；shadow_id＝shadow_{正卡id}（进 _collect_card_meta 字典的键）。
    meta 含 card_type/parent/shadow_of 等 front matter 字段，并补回 card_id 键。
    影子目录不存在或读取失败返回空 dict。
    """
    shadows: dict[str, dict[str, Any]] = {}
    sd = _shadows_dir(work_id)
    if not sd.exists():
        return shadows
    for path in sd.glob("*.md"):
        m = _meta_of(work_id, path.stem)
        if m is None:
            continue
        sid = m.get("card_id") or shadow_id_of(path.stem)
        m["card_id"] = sid
        shadows[sid] = m
    return shadows


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
    if not card_store.card_exists(work_id, owner_card_id):
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


async def append_to_section(
    work_id: str, owner_card_id: str, section: str, lines: list[str],
) -> bool:
    """往影子卡某一节**追加**若干行。**返回写盘成功没有。永不抛**（EC-109）。

    工具卡底座 T8 的自动回传通道（EC-102~EC-109）。四条纪律：

    1. **一次锁内完成读-改-写**：``card_store._get_lock(path)`` 取锁（与
       ``write_shadow_raw`` L136 同一把锁，路径锁唯一来源），锁内读全文 →
       定位节边界 → 改 → 重渲 → 锁内写回；释放锁后才发快照钩子
       （``data_snapshot.snapshot``）。**不在锁外做读-改-写**（会与
       正卡的 update_shadow 互相覆盖）。
    2. **占位符替换**：该节若仍是占位行「（暂无记录）」，**替换**掉占位，
       **不在其后追加**（否则产物指针下面永远挂着一个"暂无记录"）。
    3. **机械去重**（EC-098）：新旧行做**字符串完全相等**比较，相同则跳过。
       这是字符串比对，**不判语义、不判重复得像不像、不判该不该再写一遍**。
    4. **不抛、不阻断**（EC-109）：任何异常记日志返 False，由调用方
       （工具卡终态收口）决定是否继续——链的收口不因回传失败而中断。

    通道合法性（EC-108）：本函数写的是**主卡自己的影子**（owner＝正卡），
    与 `card_tools` L231-233「影子卡只能由对应正卡经 update_shadow 修改」
    同口径——写的人就是那张正卡的宿主，不存在第三方越权。

    ``section`` 传节标题（含 ``## ``），如 "## 产物清单"。节不存在则追加在
    正文末尾（这是文本结构的机械处置，不是内容判断）。
    """
    if not lines:
        return False
    try:
        p = shadow_path(work_id, owner_card_id)
        await asyncio.to_thread(_shadows_dir(work_id).mkdir, parents=True, exist_ok=True)
        lock = card_store._get_lock(p)
        await card_store._acquire(lock)
        try:
            now = _now_iso()
            if p.exists():
                raw = p.read_text(encoding="utf-8")
                meta, _ = parse_front_matter(raw)
                created_at = str(meta.get("created_at") or now)
                body = _section_bounds_replace(p, raw, section, lines, now)
            else:
                # 影子还没有 ⇒ 先按三节模板建出（派生是 IO 动作，不是判断）
                created_at = now
                body = _section_bounds_replace(
                    p, render_shadow(owner_card_id, SHADOW_BODY_TEMPLATE, now, now),
                    section, lines, now,
                )
            content = apply_hash(render_shadow(owner_card_id, body, created_at, now))
            p.write_text(content, encoding="utf-8")
        finally:
            lock.release()
    except Exception as exc:  # noqa: BLE001
        logger.warning("影子分节追加失败 work=%s card=%s sec=%s: %s",
                       work_id, owner_card_id, section, exc)
        return False
    logger.info("影子分节追加 work=%s card=%s (%s)", work_id, owner_card_id, section)
    try:
        await data_snapshot.snapshot(
            f"[snapshot][{work_id}] shadow append: {owner_card_id} {section}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("影子追加快照异常 work=%s card=%s: %s",
                       work_id, owner_card_id, exc)
    return True


_PLACEHOLDER = "（暂无记录）"


def _section_bounds_replace(
    path: Path, full_text: str, section: str, lines: list[str], now: str,
) -> str:
    """在影子全文里定位 ``section`` 节并机械改写，返回新正文。

    纯文本机械操作：拆 front matter → 按 ``## `` 切节 → 目标节内去重追加
    （占位符替换） → 重组全文。**不判内容、不评好坏、不看别的节**。
    """
    meta, body = parse_front_matter(full_text)
    lines_all = body.splitlines()
    # 目标节起止：节头行 → 下一个 "## " 行（或文末）
    start: int | None = None
    for i, ln in enumerate(lines_all):
        if ln.strip() == section:
            start = i + 1
            break
    if start is None:
        # 节不存在 → 追加到正文末尾（结构处置，非内容判断）
        seg = ["", section, ""] + list(lines)
        new_body = body.rstrip("\n") + "\n" + "\n".join(seg) + "\n"
        return new_body
    end = len(lines_all)
    for j in range(start, len(lines_all)):
        if lines_all[j].startswith("## "):
            end = j
            break
    seg_lines = lines_all[start:end]
    # 占位符替换：整节只有占位行时**替换**，不在其后追加（EC-107）
    stripped = [s for s in seg_lines if s.strip()]
    only_placeholder = (not stripped) or all(s.strip() == _PLACEHOLDER for s in stripped)
    kept = [] if only_placeholder else list(seg_lines)
    existing = {s.strip() for s in kept}
    added = [s for s in lines if s.strip() and s.strip() not in existing]
    # 机械去重（EC-098）：字符串完全相等即跳过；同批内也逐行去重
    seen = set(existing)
    uniq: list[str] = []
    for s in added:
        if s.strip() in seen:
            continue
        seen.add(s.strip())
        uniq.append(s)
    new_seg = kept + uniq
    new_lines = lines_all[:start] + new_seg + lines_all[end:]
    return "\n".join(new_lines).rstrip("\n") + "\n"


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
