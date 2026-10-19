# -*- coding: utf-8 -*-
"""影子卡存储（影子卡机制 2026-10-03，依据 SHADOW_CARD_RULINGS.md 九条裁定）。

影子卡 = 正卡的配对过程性附卡：
- 一正卡一影子，落在**库内** ``shadows`` 表（主键＝正卡编号）；
- 由正卡（模型）经 update_shadow 工具主动书写；另有**代码自动写的一条窄通道**：
  `append_to_section`（工具卡底座 T8 / EC-086~EC-114）——只在**工具卡终态时**
  把产物**路径指针**回传到 owner（主卡）影子卡的「## 产物清单」节，一次。
  该通道不改正文判断、不写结论、不搬运正文（用户 2026-10-09 12:54 裁定：
  「产物清单就是文档栏目」「自动记入」）。这是**设计变更**而非口径矛盾：
  「系统不自动写内容」收紧为「系统不自动写**正文与结论**」，
  路径指针的自动登记由 append_to_section 独占。
- 随正卡生命周期存续：建卡即派生（失败不阻断）、删卡同删
- 读侧走统一关口 READ 判定（不另设判据）；写侧只有自己的正卡（工具签名无 card_id），
  关口须授 SHADOW_WRITE
- 人不能直接改：不提供人的写通道，不提供删影子工具

[卡片存储迁移 SQLite 2026-10-14] 影子 md 退役：各节（进度／待协作事项／产物清单／
迭代历史）在库内**各占独立列**（方案 §1.2），读什么查什么。``read_shadow_raw`` 仍返回
「front matter ＋ 各节正文」的可读全文（消费方 `read_shadow` 工具读它）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import card_db, card_store, data_snapshot
from .hash_utils import parse_front_matter

logger = logging.getLogger("storage.shadow")

# 影子正文节标题骨架（用户 15:05 原话三类，代拍 4）
#
# [卡片区域语义 2026-10-14] **刻意未加「## 待协作事项」**：本任务只新增列与读写
# 通道（块 1 拆/渲染/追加），不改「建卡即派生」时写进库的那份初始正文。派生影子
# 的 coordination 因此为空，与红 2「新列为空即空」一致；渲染侧仍会显示这一节。
SHADOW_BODY_TEMPLATE = """## 进度

（暂无记录）

## 产物清单

（暂无记录）

## 迭代历史

（暂无记录）
"""

_PLACEHOLDER = "（暂无记录）"

# 节标题 → 库列名（与 card_db.SHADOW_SECTIONS 同源，此处按序取用）
# [卡片区域语义 2026-10-14] 新增「## 待协作事项」——方案 §4.2 影子的四节顺序。
# 只**插入**这一节，既有三节的相对次序（进度 → 产物清单 → 迭代历史）一字未动。
_SECTION_ORDER = ("## 进度", "## 待协作事项", "## 产物清单", "## 迭代历史")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _q(value: str) -> str:
    """YAML 双引号字符串（与 card_store._q 同族）。"""
    return json.dumps(value, ensure_ascii=False)


def shadow_path(work_id: str, owner_card_id: str) -> Path:
    """[退役中] 旧影子 md 路径（清理步删除）。库化后影子不再落文件。

    保留仅为让仍按旧路径断言的脚本在迁移期可读；**本模块不再使用它**。
    """
    return card_store.WORKS_DIR / work_id / "shadows" / f"{owner_card_id}.md"


def shadow_id_of(owner_card_id: str) -> str:
    """影子的可寻址 id（进 _collect_card_meta 字典的键；库主键仍是正卡编号）。"""
    return f"shadow_{owner_card_id}"


def render_shadow(owner_card_id: str, body: str, created_at: str, updated_at: str) -> str:
    """按固定字段顺序渲染影子**可读全文**（供 read_shadow_raw / read_shadow 工具）。

    front matter 七字段（代拍 1）；正文＝调用方给的各节内容。
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


# ===========================================================================
# 各节 ↔ 列
# ===========================================================================

def split_shadow_sections(body: str) -> dict[str, str]:
    """把影子正文文本拆成各节（进度／待协作事项／产物清单／迭代历史）。

    某节不存在 → 该列空串。节内容取行、去首尾空行（与卡片侧 split_body_sections
    的 strip("\\n") 同口径）。

    [卡片区域语义 2026-10-14] 键集由 card_db.SHADOW_SECTIONS 的**值**派生——
    新增节只需在 card_db 登记标题↔列，这里自动跟着长，不与那份清单脱钩。
    """
    out: dict[str, str] = {col: "" for col in card_db.SHADOW_SECTIONS.values()}
    cur: Optional[str] = None
    buf: dict[str, list[str]] = {k: [] for k in out}
    for line in body.splitlines():
        s = line.strip()
        if s in card_db.SHADOW_SECTIONS:
            cur = card_db.SHADOW_SECTIONS[s]
            continue
        if cur:
            buf[cur].append(line)
    for k, v in buf.items():
        out[k] = "\n".join(v).strip("\n")
    return out


def _render_shadow_body(row: Any) -> str:
    """库行 → 各节正文文本。

    [卡片区域语义 2026-10-14] 按 ``_SECTION_ORDER``（含新增的「## 待协作事项」）
    渲染四节。比 ``SHADOW_BODY_TEMPLATE`` 多一节——模板刻意未改，见该常量说明。
    """
    return "\n".join(
        f"{h}\n\n{row[card_db.SHADOW_SECTIONS[h]] or ''}\n" for h in _SECTION_ORDER
    )


def _append_lines_to_section(current: str, lines: list[str]) -> str:
    """把若干行追加进某一节（占位符替换 ＋ 机械去重）。

    [EC-107] 该节若仍是占位行「（暂无记录）」→ **替换**掉占位，不在其后追加。
    [EC-098] 新旧行做**字符串完全相等**比较，相同则跳过——纯字符串比对，
    不判语义、不判重复得像不像、不判该不该再写一遍。同批内也逐行去重。
    这是 ``_section_bounds_replace`` 的**列版**（md 文本操作随 md 退役）。
    """
    seg_lines = current.split("\n") if current else []
    stripped = [s for s in seg_lines if s.strip()]
    only_placeholder = (not stripped) or all(s.strip() == _PLACEHOLDER for s in stripped)
    kept = [] if only_placeholder else list(seg_lines)
    existing = {s.strip() for s in kept}
    added = [s for s in lines if s.strip() and s.strip() not in existing]
    seen = set(existing)
    uniq: list[str] = []
    for s in added:
        if s.strip() in seen:
            continue
        seen.add(s.strip())
        uniq.append(s)
    return "\n".join(kept + uniq).strip("\n")


# ===========================================================================
# 读
# ===========================================================================

def _meta_of(work_id: str, owner_card_id: str) -> Optional[dict[str, Any]]:
    """轻量读影子 meta（与 card_store._meta_of 同族）；无影子返回 None。**同步**。"""
    def _fn(conn: Any) -> Optional[dict[str, Any]]:
        row = card_db.get_shadow_row(conn, owner_card_id)
        if row is None:
            return None
        return _meta_from_row(owner_card_id, row)

    try:
        return card_db.read_sync(work_id, _fn, None)
    except card_db.CardsNotMigratedError:
        raise
    except Exception:  # noqa: BLE001
        return None


def _meta_from_row(owner_card_id: str, row: Any) -> dict[str, Any]:
    return {
        "card_id": shadow_id_of(owner_card_id),
        "card_type": "shadow",
        "parent": owner_card_id,
        "shadow_of": owner_card_id,
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
    }


def iter_shadow_metas(work_id: str) -> dict[str, dict[str, Any]]:
    """轻量收集 work 下所有影子的 meta（公开接口，供跨层调用方批量读）。

    返回 {shadow_id: meta}；shadow_id＝shadow_{正卡id}。**同步**（公开契约不改）。
    """
    def _fn(conn: Any) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for row in card_db.iter_shadow_rows(conn):
            owner = row["owner_card_id"]
            m = _meta_from_row(owner, row)
            out[m["card_id"]] = m
        return out

    return card_db.read_sync(work_id, _fn, {}) or {}


async def read_shadow_raw(work_id: str, owner_card_id: str) -> Optional[str]:
    """读影子**可读全文**（纯读：不写回）。影子不存在返回 None。

    全文＝front matter ＋ 各节正文；各节由库列现渲染（读什么查什么）。
    """
    def _fn(conn: Any) -> Optional[str]:
        row = card_db.get_shadow_row(conn, owner_card_id)
        if row is None:
            return None
        return render_shadow(
            owner_card_id, _render_shadow_body(row),
            str(row["created_at"] or ""), str(row["updated_at"] or ""),
        )

    return await card_db.run_read(work_id, _fn, None)


# ===========================================================================
# 写
# ===========================================================================

def _upsert_sections_sync(conn: Any, work_id: str, owner_card_id: str,
                          sections: dict[str, str], now: str) -> bool:
    """库内 upsert 各节；返回是否**新建**（False＝更新既有）。"""
    row = card_db.get_shadow_row(conn, owner_card_id)
    if row is None:
        card_db.insert_shadow(conn, owner_card_id, work_id, now, now, sections)
        return True
    card_db.update_shadow_sections(conn, owner_card_id, sections, now)
    return False


async def write_shadow_raw(
    work_id: str, owner_card_id: str, content: str, reason: str = "shadow update"
) -> None:
    """写影子（content＝可读全文或各节正文；覆盖写各节）＋快照钩点。

    兼容入口：若 content 带 front matter 则先剥掉，再按节入库。
    """
    body = content
    if content.lstrip().startswith("---"):
        _, body = parse_front_matter(content)
    sections = split_shadow_sections(body)
    now = _now_iso()

    def _fn(conn: Any) -> None:
        _upsert_sections_sync(conn, work_id, owner_card_id, sections, now)
        card_db.bump_rev(conn)

    await card_db.run_write(work_id, _fn)
    logger.info("影子写入 work=%s card=%s (%s)", work_id, owner_card_id, reason)
    await data_snapshot.schedule(f"[snapshot][{work_id}] {reason}: {owner_card_id}")


async def derive_shadow(work_id: str, owner_card_id: str) -> dict[str, Any]:
    """为正卡派生影子（共用函数：create_card 钩点与存量补跑脚本都调它）。

    幂等：影子已存在则跳过（绝不覆盖既有内容）。
    正卡不存在 → {"error": ...}；成功 → {"ok": True, ...}。
    失败向上抛异常，由调用方决定是否阻断（create_card 钩点捕获后不阻断）。
    """
    if not card_store.card_exists(work_id, owner_card_id):
        return {"error": f"正卡不存在: {owner_card_id}"}
    now = _now_iso()
    sections = split_shadow_sections(SHADOW_BODY_TEMPLATE)

    def _fn(conn: Any) -> dict[str, Any]:
        row = card_db.get_shadow_row(conn, owner_card_id)
        if row is not None:
            return {"ok": True, "existed": True, "owner_card_id": owner_card_id}
        card_db.insert_shadow(conn, owner_card_id, work_id, now, now, sections)
        card_db.bump_rev(conn)
        return {"ok": True, "created": True, "owner_card_id": owner_card_id}

    result = await card_db.run_write(work_id, _fn)
    if result.get("created"):
        await data_snapshot.schedule(f"[snapshot][{work_id}] shadow create: {owner_card_id}")
    return result


async def update_shadow(work_id: str, owner_card_id: str, body: str) -> dict[str, Any]:
    """正卡写自己的影子（**整篇覆盖写各节**；created_at 保留，updated_at 刷新）。

    [卡片区域语义 2026-10-14] 覆盖语义**沿用原样，未改**：``split_shadow_sections``
    对 body 里没出现的节给空串，而 ``update_shadow_sections`` 会把 sections 里
    出现的列全写一遍——故模型交来的影子正文若只含「## 待协作事项」，其余各节会被
    清空。这是既有三节一直如此的行为，新节照此复用（红 1：只新增、不动既有节）。
    模型写影子须交**完整正文**，与改动前要求相同。
    """
    sections = split_shadow_sections(body)
    now = _now_iso()

    def _fn(conn: Any) -> None:
        _upsert_sections_sync(conn, work_id, owner_card_id, sections, now)
        card_db.bump_rev(conn)

    await card_db.run_write(work_id, _fn)
    await data_snapshot.schedule(f"[snapshot][{work_id}] shadow update: {owner_card_id}")
    return {"ok": True, "owner_card_id": owner_card_id, "updated_at": now}


async def append_to_section(
    work_id: str, owner_card_id: str, section: str, lines: list[str],
) -> bool:
    """往影子卡某一节**追加**若干行。**返回写成功没有。永不抛**（EC-109）。

    工具卡底座 T8 的自动回传通道（EC-102~EC-109）。四条纪律：

    1. **一次事务内完成读-改-写**（``card_db.run_write`` 的 work 写锁 ＋ 事务，
       与 ``write_shadow_raw`` 同一把锁），**不在锁外做读-改-写**（会与正卡的
       update_shadow 互相覆盖）。
    2. **占位符替换**：该节若仍是占位行「（暂无记录）」，**替换**掉占位，
       **不在其后追加**（否则产物指针下面永远挂着一个"暂无记录"）。
    3. **机械去重**（EC-098）：新旧行做**字符串完全相等**比较，相同则跳过。
    4. **不抛、不阻断**（EC-109）：任何异常记日志返 False，由调用方
       （工具卡终态收口）决定是否继续——链的收口不因回传失败而中断。

    通道合法性（EC-108）：本函数写的是**主卡自己的影子**（owner＝正卡），
    与 `card_tools` L231-233「影子卡只能由对应正卡经 update_shadow 修改」
    同口径——写的人就是那张正卡的宿主，不存在第三方越权。

    ``section`` 传节标题（含 ``## ``），如 "## 产物清单"／"## 待协作事项"。
    影子不存在则先按模板建出（派生是 IO 动作，不是判断）；未知节标题 → 记日志
    返 False（库内只有这几列，没有「正文末尾」可挂——旧 md 版本的兜底不再适用）。
    """
    if not lines:
        return False
    col = card_db.SHADOW_SECTIONS.get(section)
    if col is None:
        logger.warning("影子分节追加：未知节标题（库内无此列）work=%s card=%s sec=%s",
                       work_id, owner_card_id, section)
        return False
    try:
        now = _now_iso()

        def _fn(conn: Any) -> None:
            row = card_db.get_shadow_row(conn, owner_card_id)
            if row is None:
                card_db.insert_shadow(conn, owner_card_id, work_id, now, now,
                                      split_shadow_sections(SHADOW_BODY_TEMPLATE))
                row = card_db.get_shadow_row(conn, owner_card_id)
            new_val = _append_lines_to_section(row[col] or "", lines)
            card_db.update_shadow_sections(conn, owner_card_id, {col: new_val}, now)
            card_db.bump_rev(conn)

        await card_db.run_write(work_id, _fn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("影子分节追加失败 work=%s card=%s sec=%s: %s",
                       work_id, owner_card_id, section, exc)
        return False
    logger.info("影子分节追加 work=%s card=%s (%s)", work_id, owner_card_id, section)
    try:
        await data_snapshot.schedule(
            f"[snapshot][{work_id}] shadow append: {owner_card_id} {section}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("影子追加快照异常 work=%s card=%s: %s",
                       work_id, owner_card_id, exc)
    return True


async def remove_shadow(work_id: str, owner_card_id: str) -> bool:
    """删影子（唯一调用点：card_store.delete_card 钩点；无工具暴露）。无影子返回 False。"""
    def _fn(conn: Any) -> int:
        n = card_db.delete_shadow_row(conn, owner_card_id)
        if n:
            card_db.bump_rev(conn)
        return n

    n = await card_db.run_write(work_id, _fn)
    if not n:
        return False
    logger.info("影子删除 work=%s card=%s", work_id, owner_card_id)
    await data_snapshot.schedule(f"[snapshot][{work_id}] shadow delete: {owner_card_id}")
    return True
