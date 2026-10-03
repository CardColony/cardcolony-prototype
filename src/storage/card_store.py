# -*- coding: utf-8 -*-
"""卡片存储。卡片文件 = YAML Front Matter + Markdown 正文（四区）。

全局铁律：任何代码路径都不允许删除 cards\\ 或 sessions\\ 下的文件。

Phase 7：卡片归属于 work，路径 = works/{work_id}/cards/card_NNN.md。
序号（NNN）在每个 work 内独立自增。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import canvas_store, data_snapshot, work_store
from .hash_utils import (
    apply_hash,
    calc_card_hash,
    parse_front_matter,
    replace_fm_field,
    split_body_sections,
)

logger = logging.getLogger("storage.card")

BASE_DIR = Path(__file__).resolve().parents[2]
WORKS_DIR = BASE_DIR / "works"

# 旧路径常量保留，供 monkeypatch 兼容旧测试时使用（不直接用）
CARDS_DIR = BASE_DIR / "cards"
SESSIONS_DIR = BASE_DIR / "sessions"

# [phase8.3] status 二态已废除：新卡不写 status，读侧容忍存量卡的 status 字段
ALLOWED_SOURCE = ("讨论", "用户直接创建", "模型生成", "系统派生")
ALLOWED_CARD_TYPE = ("剧本", "世界观", "行为体", "节点", "假设", "线索")

_CARD_FILE_RE = re.compile(r"^card_(\d{3,})\.md$")

# 每个文件路径一把 asyncio.Lock（模块级 dict 缓存）
_locks: dict[str, asyncio.Lock] = {}

LOCK_TIMEOUT = 1.0


class CardLockedError(Exception):
    """获取文件锁超时（错误码 423 语义）。"""

    code = 423


class CardDeleteDeniedError(Exception):
    """删除被结构性防护拒绝（有子卡/剧本根卡，错误码 400/403 语义）。"""

    code = 400


class CardNotFoundError(Exception):
    code = 404


def _get_lock(path: Path) -> asyncio.Lock:
    key = str(path)
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


async def _acquire(lock: asyncio.Lock) -> None:
    try:
        await asyncio.wait_for(lock.acquire(), timeout=LOCK_TIMEOUT)
    except asyncio.TimeoutError:
        raise CardLockedError("文件锁获取超时(423)") from None


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _q(value: str) -> str:
    """YAML 双引号字符串（用 JSON 转义，YAML 兼容）。"""
    return json.dumps(value, ensure_ascii=False)


def _resolve_card_type(card_type: str) -> str:
    """非法值一律降级为"线索"（不报错不丢弃）。"""
    return card_type if card_type in ALLOWED_CARD_TYPE else "线索"


def _cards_dir(work_id: str) -> Path:
    return WORKS_DIR / work_id / "cards"


def _sessions_dir(work_id: str) -> Path:
    return WORKS_DIR / work_id / "sessions"


def _card_path(work_id: str, card_id: str) -> Path:
    return _cards_dir(work_id) / f"{card_id}.md"


def _session_name(card_id: str) -> str:
    seq = card_id.split("_", 1)[1]
    return f"session_{seq}.md"


def _session_path(work_id: str, card_id: str) -> Path:
    return _sessions_dir(work_id) / _session_name(card_id)


def _next_seq(work_id: str) -> int:
    cd = _cards_dir(work_id)
    max_seq = 0
    if cd.exists():
        for path in cd.glob("card_*.md"):
            m = _CARD_FILE_RE.match(path.name)
            if m:
                max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def _normalize_parent(work_id: str, parent: Optional[str], card_type: str) -> str:
    """parent 规则：
    - 剧本卡无视 parent 入参，强制为 ""
    - parent=None 或父卡不存在 → 回退 work_card_id
    """
    if card_type == "剧本":
        return ""
    wid = work_store.work_card_id_of(work_id)
    if parent is None or parent == "":
        return wid
    # 卡不存在回退 work_card_id
    if not _card_path(work_id, parent).exists():
        logger.warning("parent 指向不存在的卡 %s，回退 work_card_id", parent)
        return wid
    return parent


def _render_card(meta: dict[str, Any], charter: str, conclusion: str,
                 rejections: str, history_lines: list[str]) -> str:
    """按固定字段顺序渲染卡片全文（content_hash 由 apply_hash 回填）。

    四区顺序：## 职责区 → ## 结论区 → ## 否决记录区 → ## 修改历史。
    """
    fm = [
        "---",
        f"title: {_q(meta['title'])}",
        f"source: {_q(meta['source'])}",
        f"card_type: {_q(meta['card_type'])}",
        f"parent: {_q(meta['parent'])}",
        'content_hash: ""',
        f"tags: {json.dumps(meta.get('tags', []), ensure_ascii=False)}",
        f"relations: {json.dumps(meta.get('relations', []), ensure_ascii=False)}",
        f"session_ref: {_q(meta['session_ref'])}",
        f"methodology_refs: {json.dumps(meta.get('methodology_refs', []), ensure_ascii=False)}",
        f"pending_sync: {json.dumps(bool(meta.get('pending_sync', False)))}",
        f"created_at: {_q(meta['created_at'])}",
        f"updated_at: {_q(meta['updated_at'])}",
        "---",
    ]
    parts = ["\n".join(fm), "", "## 职责区", ""]
    if charter.strip():
        parts += [charter.strip("\n"), ""]
    parts += ["## 结论区", ""]
    if conclusion.strip():
        parts += [conclusion.strip("\n"), ""]
    parts += ["## 否决记录区", ""]
    if rejections.strip():
        parts += [rejections.strip("\n"), ""]
    parts += ["## 修改历史", ""]
    parts += history_lines
    return "\n".join(parts) + "\n"


def _parse_history_lines(history_text: str) -> list[str]:
    return [line for line in history_text.splitlines() if line.strip()]


def _append_history_line(content: str, line: str) -> str:
    """在 '## 修改历史' 小节末尾追加一行；小节不存在则在文件末尾补建。"""
    lines = content.splitlines(keepends=True)
    heading_idx = None
    for i, l in enumerate(lines):
        if l.strip() == "## 修改历史":
            heading_idx = i
            break
    if heading_idx is None:
        suffix = "" if content.endswith("\n") else "\n"
        return content + f"{suffix}\n## 修改历史\n\n{line}\n"
    end = len(lines)
    for j in range(heading_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    insert_at = end
    while insert_at > heading_idx + 1 and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    trailing = "".join(lines[insert_at:end])
    new_lines = lines[:insert_at] + [line + "\n"] + ([trailing] if trailing else []) + lines[end:]
    return "".join(new_lines)


# ============================================================
# 子树遍历（供防环、权限、目录使用）
# ============================================================

def _meta_of(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """轻量读 meta（不校验哈希、不锁），卡片不存在返回 None。"""
    p = _card_path(work_id, card_id)
    if not p.exists():
        return None
    try:
        meta, _ = parse_front_matter(p.read_text(encoding="utf-8"))
        if not meta.get("card_type"):
            meta["card_type"] = "线索"  # 旧卡兜底
        if "parent" not in meta:
            meta["parent"] = work_store.work_card_id_of(work_id)
        return meta
    except Exception:
        return None


def _subtree_ids(work_id: str, root_id: str) -> set[str]:
    """root_id 整棵子树（含 root 自身）。"""
    result: set[str] = set()
    stack = [root_id]
    while stack:
        cid = stack.pop()
        if cid in result:
            continue
        result.add(cid)
        # 找 parent == cid 的所有卡
        cd = _cards_dir(work_id)
        if cd.exists():
            for path in cd.glob("card_*.md"):
                child_id = path.stem
                if child_id in result:
                    continue
                m = _meta_of(work_id, child_id)
                if m and m.get("parent") == cid:
                    stack.append(child_id)
    return result


def _is_descendant(work_id: str, candidate: str, ancestor: str) -> bool:
    """candidate 是否是 ancestor 的（严格）后代（不含 ancestor 自身）。"""
    return candidate in (_subtree_ids(work_id, ancestor) - {ancestor})


# ============================================================
# 公开 API
# ============================================================

async def create_card(
    work_id: str,
    title: str,
    source: str,
    card_type: str = "线索",
    parent: Optional[str] = None,
    charter: str = "",
    conclusion: str = "",
) -> dict[str, Any]:
    """创建卡片 + 对应会话文件，更新 canvas 缓存，git 提交。

    任何一步失败：保留已创建的文件，记日志返回错误。
    """
    # work 必须存在
    if work_store.read_work(work_id) is None:
        return {"error": f"剧本不存在: {work_id}"}
    if source not in ALLOWED_SOURCE:
        return {"error": f"source 非法: {source}，只允许 {ALLOWED_SOURCE}"}
    try:
        resolved_type = _resolve_card_type(card_type)
        resolved_parent = _normalize_parent(work_id, parent, resolved_type)

        _cards_dir(work_id).mkdir(parents=True, exist_ok=True)
        _sessions_dir(work_id).mkdir(parents=True, exist_ok=True)
        seq = _next_seq(work_id)
        card_id = f"card_{seq:03d}"
        session_name = _session_name(card_id)
        now = _now_iso()
        meta = {
            "title": title,
            "source": source,
            "card_type": resolved_type,
            "parent": resolved_parent,
            "tags": [],
            "relations": [],
            "session_ref": f"sessions/{session_name}",
            "methodology_refs": [],
            "pending_sync": False,
            "created_at": now,
            "updated_at": now,
        }
        history_lines = [f"- [{now}] 卡片创建"]
        content = _render_card(meta, charter, conclusion, "", history_lines)
        content = apply_hash(content)

        card_path = _card_path(work_id, card_id)
        lock = _get_lock(card_path)
        await _acquire(lock)
        try:
            card_path.write_text(content, encoding="utf-8")
            session_path = _sessions_dir(work_id) / session_name
            if not session_path.exists():
                session_path.write_text(
                    f"<!-- session: {card_id}, created: {now} -->\n", encoding="utf-8"
                )
        finally:
            lock.release()

        await canvas_store.update_cache(work_id)
        # [数据与代码分家 2026-10-03] 不再 git 提交运行数据（分支操作曾整体回退 works/），
        # 改由独立快照目录留痕（与代码仓库物理分离）。
        await data_snapshot.snapshot(f"[snapshot][{work_id}] create: {card_id}")
        # [影子卡 2026-10-03] 建正卡即派生影子（一正一影，R-03）；失败只记日志不阻断建卡
        from . import shadow_store
        try:
            await shadow_store.derive_shadow(work_id, card_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("影子派生失败（不阻断建卡）work=%s card=%s: %s", work_id, card_id, exc)
        return await read_card(work_id, card_id)
    except CardLockedError:
        raise
    except Exception as exc:
        logger.error("create_card 失败（已创建文件保留）: %s", exc)
        return {"error": str(exc)}


async def read_card(work_id: str, card_id: str) -> dict[str, Any]:
    """读卡并校验哈希；检测到外部修改则按规格两步 git 提交并回写。"""
    card_path = _card_path(work_id, card_id)
    if not card_path.exists():
        raise CardNotFoundError(f"卡片不存在: {card_id}")

    lock = _get_lock(card_path)
    await _acquire(lock)
    try:
        raw = card_path.read_text(encoding="utf-8")
        meta, _ = parse_front_matter(raw)
        # 双保险兜底（迁移已物理写入，这里防御旧格式文件）
        if not meta.get("card_type"):
            meta["card_type"] = "线索"
        if "parent" not in meta:
            meta["parent"] = work_store.work_card_id_of(work_id)

        stored = meta.get("content_hash", "")
        computed = calc_card_hash(raw)
        if computed != stored:
            # [数据与代码分家 2026-10-03] 运行数据不再 git 提交，改走独立快照
            await data_snapshot.snapshot(f"[snapshot][{work_id}] external_modification_raw: {card_id}")
            now = _now_iso()
            raw = _append_history_line(raw, f"- [{now}] [外部修改]")
            raw = replace_fm_field(raw, "updated_at", f"updated_at: {_q(now)}\n")
            raw = apply_hash(raw)
            card_path.write_text(raw, encoding="utf-8")
            await data_snapshot.snapshot(f"[snapshot][{work_id}] external_modification_logged: {card_id}")
            meta, _ = parse_front_matter(raw)
            if not meta.get("card_type"):
                meta["card_type"] = "线索"
            if "parent" not in meta:
                meta["parent"] = work_store.work_card_id_of(work_id)
    finally:
        lock.release()

    _, body = parse_front_matter(raw)
    sections = split_body_sections(body)
    meta["card_id"] = card_id  # 补回 card_id（文件名即 id，meta 文件内不重复存）
    meta.pop("status", None)  # [phase8.3] 二态废除：存量卡的 status 读侧容忍但不外露
    return {
        "meta": meta,
        "body": {
            "charter": sections["charter"],
            "conclusion": sections["conclusion"],
            "rejections": sections["rejections"],
            "history": _parse_history_lines(sections["history_text"]),
        },
        "raw": raw,
    }


async def update_card(
    work_id: str,
    card_id: str,
    updates: Optional[dict[str, Any]],
    history_note: str,
) -> dict[str, Any]:
    """通用写入：应用修改→追加修改历史→刷新 updated_at/content_hash→写回→git 提交。

    updates 支持键（含 Phase 7 扩展）：
      title/tags/relations/session_ref/pending_sync/card_type/parent
      charter/conclusion/rejections（正文区）

    [phase8.3] status 二态已废除：updates 中若带 status 一律忽略（不报错）。
    """
    updates = updates or {}
    updates.pop("status", None)

    card_path = _card_path(work_id, card_id)
    if not card_path.exists():
        raise CardNotFoundError(f"卡片不存在: {card_id}")

    # 先读现状（不锁，用于防环/剧本卡保护判定）
    current = await read_card(work_id, card_id)
    cur_meta = current["meta"]
    is_work_card = cur_meta.get("card_type") == "剧本"

    # 剧本卡保护：card_type / parent 不允许修改
    if is_work_card and ("card_type" in updates or "parent" in updates):
        return {"error": "剧本卡的 card_type/parent 不允许修改"}

    # card_type 降级
    if "card_type" in updates:
        new_type = _resolve_card_type(updates["card_type"])
        # 不允许把任意卡改成剧本（剧本只能由 create_work 流程产生）
        if new_type == "剧本" and not is_work_card:
            new_type = "线索"
            logger.warning("update_card: 禁止把非剧本卡改成剧本，降级线索 card=%s", card_id)
        updates["card_type"] = new_type

    # parent 变更防环
    if "parent" in updates and not is_work_card:
        new_parent = updates["parent"]
        if new_parent == card_id:
            return {"error": "parent 不能指向自身或后代"}
        if new_parent and new_parent != "" and _is_descendant(work_id, new_parent, card_id):
            return {"error": "parent 不能指向自身或后代"}
        # parent 卡必须存在，否则回退 work_card_id
        if new_parent and new_parent != "" and not _card_path(work_id, new_parent).exists():
            new_parent = work_store.work_card_id_of(work_id)
            updates["parent"] = new_parent

    lock = _get_lock(card_path)
    await _acquire(lock)
    try:
        raw = card_path.read_text(encoding="utf-8")
        meta, body = parse_front_matter(raw)
        sections = split_body_sections(body)

        for key in ("title", "tags", "relations", "session_ref",
                    "pending_sync", "card_type", "parent", "methodology_refs"):
            if key in updates:
                meta[key] = updates[key]
        charter = updates.get("charter", sections["charter"])
        conclusion = updates.get("conclusion", sections["conclusion"])
        rejections = updates.get("rejections", sections["rejections"])

        now = _now_iso()
        meta["updated_at"] = now
        history_lines = _parse_history_lines(sections["history_text"])
        history_lines.append(f"- [{now}] {history_note}")

        content = _render_card(meta, charter, conclusion, rejections, history_lines)
        content = apply_hash(content)
        card_path.write_text(content, encoding="utf-8")
    finally:
        lock.release()

    await canvas_store.update_cache(work_id)
    # [数据与代码分家 2026-10-03] 运行数据不再 git 提交（曾因分支操作被整体回退），改走独立快照
    await data_snapshot.snapshot(f"[snapshot][{work_id}] update: {card_id}")

    # [phase8.3] "剧本卡切确定→派生目标卡"触发已废除；
    # 目标卡改为创建剧本时即派生（见 derive_ending / work_store.create_work）。

    return await read_card(work_id, card_id)


async def delete_card(work_id: str, card_id: str) -> dict[str, Any]:
    """[用户删卡 2026-10-02] 删除卡片文件＋对应 session 文件，git 落档。

    结构性防护（机械判据，不判内容）：
    - 剧本不存在 → {"error": ...}
    - 卡片不存在 → CardNotFoundError
    - card_id == work_card_id（剧本根卡）→ CardDeleteDeniedError（403 语义）
    - 有子卡（parent 链上任何卡挂它）→ CardDeleteDeniedError（防孤儿）

    删除顺序：锁内移除 card .md → 锁外移除 session .md → canvas 重建 → git 提交。
    中途失败：已删文件不恢复（git 可回溯），错误记日志返回。
    """
    if work_store.read_work(work_id) is None:
        return {"error": f"剧本不存在: {work_id}"}
    if card_id == work_store.work_card_id_of(work_id):
        raise CardDeleteDeniedError(f"剧本根卡不可删除: {card_id}")

    card_path = _card_path(work_id, card_id)
    if not card_path.exists():
        raise CardNotFoundError(f"卡片不存在: {card_id}")

    # 子卡防护：任何卡的 parent 指向 card_id → 拒删
    cd = _cards_dir(work_id)
    if cd.exists():
        for path in cd.glob("card_*.md"):
            other_id = path.stem
            if other_id == card_id:
                continue
            m = _meta_of(work_id, other_id)
            if m and m.get("parent") == card_id:
                raise CardDeleteDeniedError(
                    f"存在子卡 {other_id}，先删子卡或改挂他卡"
                )

    lock = _get_lock(card_path)
    await _acquire(lock)
    try:
        card_path.unlink()
    finally:
        lock.release()

    session_path = _session_path(work_id, card_id)
    try:
        if session_path.exists():
            session_path.unlink()
    except Exception as exc:  # noqa: BLE001
        logger.warning("session 文件删除失败（不中断）%s: %s", session_path, exc)

    # [影子卡 2026-10-03] 删正卡→影子同删（随正卡生命周期存续，不变量 3）；失败不中断
    from . import shadow_store
    try:
        await shadow_store.remove_shadow(work_id, card_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("影子删除失败（不中断）work=%s card=%s: %s", work_id, card_id, exc)

    await canvas_store.update_cache(work_id)
    # [数据与代码分家 2026-10-03] 运行数据不再 git 提交，改走独立快照
    await data_snapshot.snapshot(f"[snapshot][{work_id}] delete: {card_id}")
    return {"ok": True, "deleted": card_id}


async def derive_ending(work_id: str) -> Optional[str]:
    """[phase8.3] 目标卡派生：创建剧本时调用（work_store.create_work），
    存量剧本由 scripts/derive_endings.py 补跑。

    ending_derived 仅作幂等标志：已派生直接返回 None。
    失败只记日志返回 None，不影响主流程。成功返回目标卡 id。
    """
    wdata = work_store.read_work(work_id)
    if wdata is None:
        return None
    if wdata.get("ending_derived"):
        return None  # 已派生（幂等）
    work_card_id = wdata.get("work_card_id") or ""
    if not work_card_id:
        logger.error("derive_ending: work_card_id 缺失 work=%s", work_id)
        return None

    # [无内容预设改造 2026-10-02] 派生章程清空：目标卡只带结构出生（标题+锚定关系），
    # 内容性章程（"钉死目标判定…"等）属于预置思考指令，不再随系统写入。
    charter = ""
    try:
        result = await create_card(
            work_id, "目标", "系统派生",
            card_type="节点", parent=work_card_id,
            charter=charter, conclusion="",
        )
        if "error" in result:
            logger.error("目标卡派生失败 work=%s: %s", work_id, result["error"])
            return None
        ending_id = result["meta"]["card_id"]
        # 目标卡 relations 加一条指向剧本卡的"锚定"
        await _append_relation(work_id, ending_id, {
            "target": work_card_id, "type": "锚定",
        })
        await canvas_store.update_cache(work_id)  # relations 变更同步进画布缓存
        # ending_derived 置 true（幂等标志）
        work_store.update_work(work_id, {"ending_derived": True})
        # [数据与代码分家 2026-10-03] 运行数据不再 git 提交，改走独立快照
        await data_snapshot.snapshot(f"[snapshot][{work_id}] derive ending: {ending_id}")
        return ending_id
    except Exception as exc:  # noqa: BLE001
        logger.error("目标卡派生异常 work=%s: %s", work_id, exc)
        return None


async def _append_relation(work_id: str, card_id: str, rel: dict[str, Any]) -> None:
    """向指定卡的 relations 追加一条 relation（不重复），刷新哈希后写回。"""
    card_path = _card_path(work_id, card_id)
    lock = _get_lock(card_path)
    await _acquire(lock)
    try:
        raw = card_path.read_text(encoding="utf-8")
        meta, body = parse_front_matter(raw)
        sections = split_body_sections(body)
        rels = list(meta.get("relations") or [])
        if not any(r.get("target") == rel.get("target") and r.get("type") == rel.get("type") for r in rels):
            rels.append(rel)
            meta["relations"] = rels
            history_lines = _parse_history_lines(sections["history_text"])
            now = _now_iso()
            history_lines.append(f"- [{now}] 追加 relation: {rel}")
            content = _render_card(
                meta, sections["charter"], sections["conclusion"],
                sections["rejections"], history_lines,
            )
            content = apply_hash(content)
            card_path.write_text(content, encoding="utf-8")
    finally:
        lock.release()
