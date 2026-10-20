# -*- coding: utf-8 -*-
"""卡片存储。卡片 ＝ SQLite 库里一行（正文三块各占独立列），不再是 md 文件。

[卡片存储迁移 SQLite 2026-10-14] 库是卡片与影子的**唯一真相**：库形态、表结构、
并发模型见 ``src/storage/card_db.py``。本模块只写 SQL 与业务规则，不碰路径常量。

全局铁律（改写后）：删卡只走 :func:`delete_card` 一个口；``sessions/`` 下的文件
仍受原铁律保护（任何代码路径都不允许删除）。``cards/`` 目录已不存在。

Phase 7 沿革：卡片归属于 work；序号（NNN）在每个 work 内独立自增——现由库内
``meta`` 表的 ``seq`` 承载，发号与建卡同事务。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import card_db, canvas_store, data_snapshot, work_store
from .hash_utils import (
    OLD_KEY_TO_NEW,
    apply_hash,
    calc_card_hash,
    effective_form,
    parse_front_matter,
    replace_fm_field,
    split_body_sections,
)

logger = logging.getLogger("storage.card")

BASE_DIR = Path(__file__).resolve().parents[2]
WORKS_DIR = BASE_DIR / "works"

# [卡片分类基座 T2] 类型注册表路径（模块级常量 ⇒ 测试可monkeypatch 隔离，
# 与 WORKS_DIR 同一套手法，见 tests/conftest.py）
TYPE_REGISTRY_PATH = BASE_DIR / "config" / "card_type_registry.json"

# 旧路径常量保留，供 monkeypatch 兼容旧测试时使用（不直接用）
CARDS_DIR = BASE_DIR / "cards"
SESSIONS_DIR = BASE_DIR / "sessions"

# [phase8.3] status 二态已废除：新卡不写 status，读侧容忍存量卡的 status 字段
ALLOWED_SOURCE = ("讨论", "用户直接创建", "模型生成", "系统派生")
# [卡片分类基座 T6] 旧六值词表已退役（legacy_types，写路径由闸 1 拦截）。
# **保留常量供读路径宽容与迁移期判定使用**，不再作为「允许写入」的白名单——
# 写路径合法值以 config/card_type_registry.json 为唯一事实源。
# [底座收口 T2.f] 加 "project"：无注册表环境（conftest 沙箱）里 create_work
# 建根卡传 project，白名单不含它会被降级成"线索"。
ALLOWED_CARD_TYPE = ("project", "剧本", "世界观", "行为体", "节点", "假设", "线索")

# [工具卡底座 T11 · EC-183] 工具卡 owner 的合法类型集合（用户 2026-10-09
# 11:54 裁定逐字「子项目卡或根卡」）。**集合严格止于此二者**——放宽范围
# 不得自行扩大至 topic 等其他类型（R-F）；未裁前 topic 仍被拒。
# 与 config/card_type_registry.json 的 gate_3_owner_required.desc 同批改
# （两处口径必须一致，只改一处＝代码与注册表两套事实，R-F）。
TOOL_CARD_OWNER_TYPES: tuple[str, ...] = ("sub_project", "project")

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


class CardTypeValueError(Exception):
    """card_type 取值非法（卡片分类基座 T6·废除静默降级）。

    与既有写函数的错误契约对齐：抛在**解析层**，由 create_card /
    update_card 的 try 捕获后转成 {"error": ...} 返回，不外泄给调用方。
    """
    code = 400


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
    """card_type 取值校验。

    [卡片分类基座 T6] **废除静默降级**（原行为：非法值一律 logger.warning 后
    静默变「线索」，让调用方以为写入成功）。现行为：
      - 注册表**已加载**时：只放行新词表 L1 值；legacy 值与非词表值**显式报错**
        （抛 CardTypeValueError，由调用点捕获转 {"error": ...}）。
      - 注册表**缺失/坏 JSON**时：回落旧白名单放行（保持 T-0007 补丁态，
        避免注册表不可用时阻断写路径）。

    [T07 打点] 保留一条 warning 留痕线：非法值走异常而非静默，但日志仍记原值。
    """
    if not card_type:
        return card_type
    registry = load_type_registry()
    if not registry:
        # 注册表不可用 ⇒ 回落旧白名单（不阻断写路径）
        if card_type in ALLOWED_CARD_TYPE:
            return card_type
        logger.warning("类型名非法（无注册表，按旧白名单放行）: %r", card_type)
        return "线索"
    l1_codes = _l1_all_codes(registry)
    if card_type in l1_codes:
        return card_type
    legacy_codes = _registry_codes(registry, "legacy_types")
    # [T6 渐进口径] 只拦 write_blocked=true 的 legacy 值（详见 _write_gate 闸 1）。
    # 存量的「剧本」「线索」在迁移完成前仍放行，否则系统建不出卡。
    blocked = {str(x.get("code", "")) for x in (registry.get("legacy_types") or [])
               if isinstance(x, dict) and x.get("write_blocked")}
    if card_type in blocked:
        logger.warning("类型名已退役（写路径拦截）: %r", card_type)
        raise CardTypeValueError(
            f"非法 card_type: {card_type}（已退役；已定义词表：{_current_l1_codes(registry)}）"
        )
    if card_type in legacy_codes:
        # 未标 write_blocked 的 legacy 值：放行并留痕（迁移期常态，不算错误）
        logger.info("legacy card_type 放行（迁移未完成）: %r", card_type)
        return card_type
    logger.warning("类型名非法（注册表拒绝）: %r", card_type)
    raise CardTypeValueError(
        f"非法 card_type: {card_type}（已定义词表：{_current_l1_codes(registry)}）"
    )


def _perm_fields(project_class: str, card_type: str) -> dict[str, Any]:
    """[权限底座重做 2026-10-12 · 裁定 4] 建卡期的权限固化字段。

    两级定位 ``project_class``（工牌）＋ ``card_type``（卡类型）→ 查
    ``config/permissions.json``，算出 ``(perms, perms_version)`` 落进 meta。

    ``perms`` 一旦落盘即成这张卡的权限真身——**运行时只读卡上的 perms，
    不再查配置**（这正是「每次现查」被废弃的地方）。配置改版靠
    ``scripts/refresh_perms.py`` 按 perms_version 批量重算。

    延迟 import：避免 src.storage 与 src.api 在模块加载期互拉成环。
    """
    from ..permissions import policy

    perms, version = policy.compute_perms(project_class, card_type)
    return {policy.PERM_KEY: perms, policy.PERM_VERSION_KEY: version}


def _auto_perm_fields() -> dict[str, Any]:
    """[自动模式 2026-10-17 · 回灌 5700 块0.1/0.2] 自动模式卡的权限固化字段。

    与 ``_perm_fields`` 并列（**不改后者签名**）：自动模式不是「按类型分权」的
    一种，是**绕过**它（迭代方案 §2.6），故走 ``policy.auto_perms()``（全开集 ＋
    专用 ``perms_version = -1``）。``config/permissions.json`` 一个字不动
    （红线 3：自动模式是绕过类型分权，不是新增一种分权）。
    """
    from ..permissions import policy

    perms, version = policy.auto_perms()
    return {policy.PERM_KEY: perms, policy.PERM_VERSION_KEY: version}


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


def _card_id_seq_path(work_id: str) -> Path:
    """[T07-1-2] 编号序列载体：works/{work_id}/.card_id_seq.json。

    落点在工作目录内、但不在 cards/ 与 shadows/ 的任何扫描通道内
    （8 处 glob 全部只扫 cards/）。会进快照——预期行为（属剧本状态）。
    """
    return WORKS_DIR / work_id / ".card_id_seq.json"


def _scan_max_seq(work_id: str) -> int:
    """旧推算路径：扫 cards/ 取目录最大号（留作序列种子与对账基准）。"""
    cd = _cards_dir(work_id)
    max_seq = 0
    if cd.exists():
        for path in cd.glob("card_*.md"):
            m = _CARD_FILE_RE.match(path.name)
            if m:
                max_seq = max(max_seq, int(m.group(1)))
    return max_seq


def _load_card_id_seq(work_id: str) -> dict[str, Any]:
    """读序列载体；缺失/损坏时以目录最大号为种子初始化（老卡不补号，D-02）。"""
    p = _card_id_seq_path(work_id)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("seq"), int):
                data.setdefault("issued", [])
                return data
        except Exception:  # noqa: BLE001
            pass
    return {"seq": _scan_max_seq(work_id), "issued": []}


def _save_card_id_seq(work_id: str, state: dict[str, Any]) -> None:
    """序列载体写盘（每次发号成功后立即调用，D-05 新号只发不回溯）。"""
    p = _card_id_seq_path(work_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _next_seq(work_id: str) -> int:
    """[T07-1-1] 编号来源改「发放式」：库内 meta.seq 的下一个号。

    [卡片存储迁移 SQLite] 序列载体从 ``.card_id_seq.json`` 换成库内 ``meta`` 表
    （库自包含：备份一个文件即完整；发号与建卡同事务，见 create_card）。
    输出形态保持现有 card_{seq:03d}（格式化在 create_card）。
    """
    def _fn(conn: Any) -> int:
        return card_db.next_seq(conn)

    return int(card_db.read_sync(work_id, _fn, 0) or 0)


# [T07 打点] 判据源切换观测：每处首次调用记新旧对比行；不一致升级 warning
# （判据源切换唯一的回归观测点）。不设自动收口，由人看零命中日志后裁定。
_judgment_switch_logged: set[str] = set()


def _log_judgment_switch(site: str, card_id: str, old_result: Any, new_result: Any) -> None:
    if site not in _judgment_switch_logged:
        _judgment_switch_logged.add(site)
        logger.info(
            "判据源切换: site=%s card=%s 旧判据=%s 新判据=%s",
            site, card_id, old_result, new_result,
        )
    if old_result != new_result:
        logger.warning(
            "判据源切换不一致: site=%s card=%s 旧判据=%s 新判据=%s",
            site, card_id, old_result, new_result,
        )


def _normalize_parent(work_id: str, parent: Optional[str], card_type: str) -> str:
    """parent 规则：
    - project 根卡无视 parent 入参，强制为 ""（[底座收口 T2.b] 原判「剧本」
      随类型退役换为 project；防御纵深——闸2 已拦子卡写 project）
    - parent=None 或父卡不存在 → 回退 work_card_id
    """
    if card_type == "project":
        return ""
    wid = work_store.work_card_id_of(work_id)
    if parent is None or parent == "":
        return wid
    # 卡不存在回退 work_card_id
    if not card_exists(work_id, parent):
        logger.warning("parent 指向不存在的卡 %s，回退 work_card_id", parent)
        return wid
    return parent


# [卡片信息结构改造 S1-1] 新建卡默认三块骨架（模块级常量，逐字模板）。
# 注入方式见 create_card：整篇正文形态过 split_body_sections 取三块，
# 再以 form="new" 交 _render_card——**不由 create_card 的 charter 参数默认值承载**
# （S1-3：参数签名不变；_render_card 自输区头，参数里带区头会落出重复区头）。
NEW_CARD_SKELETON = """## 使命
（本卡作用/目标、授权来源；≤5行；立项定稿，改动需用户确认）
## 现状
（当前状况快照：做到哪、什么状态、卡在哪；只写已核实；事实条目带锚点自指；覆盖写·单一最新；块尾至多一行衔接索引）
## 定论
（对本卡相关事项的决定；只追加，仅经收口仪式写入）
D01【裁定】……（日期·依据）
D02【决定】……（日期·依据）
D03【收敛】……（日期·死过什么）
→ 被D05取代（因……）"""

# 区头随形态走（G-1/G-3）。旧形态存量卡保形：区头逐字不变（RD-09）。
_SECTION_HEADINGS = {
    "old": ("## 职责区", "## 结论区", "## 否决记录区"),
    "new": ("## 使命", "## 现状", "## 定论"),
}
HISTORY_HEADING = "## 修改历史"


def _render_card(meta: dict[str, Any], mission: str, status_block: str,
                 rulings: str, history_lines: list[str],
                 form: str = "old") -> str:
    """按固定字段顺序渲染卡片全文（content_hash 由 apply_hash 回填）。

    [卡片信息结构改造 G-3] 加 form 参数：三块区头随形态走——
    form="new" → ## 使命 / ## 现状 / ## 定论；form="old" → 旧三区头（存量卡保形）。
    三调用点（create_card / update_card / _append_relation）必须全传 form，
    漏传即回落 "old"——新卡会被写回旧区头（V15 判据）。
    修改历史恒为机械尾，两形态同名，不在 form 内。
    """
    if form not in _SECTION_HEADINGS:
        # mixed 未收敛时不得落盘（拆分器侧已收敛；此处兜底防漏）
        raise ValueError(f"_render_card: form 非法 {form!r}（须为 old/new）")
    h_mission, h_status, h_rulings = _SECTION_HEADINGS[form]
    fm = [
        "---",
        f"title: {_q(meta['title'])}",
        f"source: {_q(meta['source'])}",
        f"card_type: {_q(meta['card_type'])}",
        f"parent: {_q(meta['parent'])}",
        'content_hash: ""',
        f"tags: {json.dumps(meta.get('tags', []), ensure_ascii=False)}",
        # [依赖关联线 2026-10-16] 通信线让名：原 relations → links。
        f"links: {json.dumps(meta.get('links', []), ensure_ascii=False)}",
        f"session_ref: {_q(meta['session_ref'])}",
        # [卡片分类基座 T1] 新分类字段：非空才渲染 FM 行（空串＝不写，存量兼容）。
        # 位置在 session_ref 之后、created_at 之前——读路径 _meta_of 按key 取，
        # 顺序只影响人读；不参与 content_hash 之外的任何判据。
        *([f"project_class: {_q(meta['project_class'])}"]
          if meta.get("project_class") else []),
        *([f"owner_card_id: {_q(meta['owner_card_id'])}"]
          if meta.get("owner_card_id") else []),
        *([f"state: {_q(meta['state'])}"] if meta.get("state") else []),
        # [工具卡底座 T3 · EC-037] 工具卡状态三字段（＋终态声明）。
        # 位置在 state 之后、created_at 之前；读路径 _meta_of 按 key 取。
        # **字段名纪律（EC-038）**：此处只可能出现「位置」与「模型自报的声明」，
        # **绝不出现 step_status/done/通过/失败 之类评价字样**——那是对内容的
        # 判定，只归模型（任务书 §四 R-A）。因此三字段的命名不可改。
        # 条件渲染：空串＝不写（存量兼容，与 project_class/owner_card_id 同手法）；
        # round 例外用 `is not None` 判，因为 round=0 是合法值不是「没写过」。
        *([f"toolcard_script: {_q(meta['toolcard_script'])}"]
          if meta.get("toolcard_script") else []),
        *([f"toolcard_step: {_q(meta['toolcard_step'])}"]
          if meta.get("toolcard_step") else []),
        *([f"toolcard_round: {int(meta['toolcard_round'])}"]
          if meta.get("toolcard_round") is not None else []),
        *([f"toolcard_final: {_q(meta['toolcard_final'])}"]
          if meta.get("toolcard_final") else []),
        # [迭代二 S1] 思维链板终态声明（board.finalize_board 写入、孤儿扫描判定键）。
        # 同法 toolcard_final：进 update_card 键清单＋此处条件渲染行——两处缺一
        # 该键即被静默丢弃（本函数只搬清单内键，渲染按固定清单）。
        *([f"thinkchain_final: {_q(meta['thinkchain_final'])}"]
          if meta.get("thinkchain_final") else []),
        # [权限底座重做 2026-10-12] 固化权限两行（裁定 4）。条件渲染与
        # toolcard 三字段同一手法：meta 里有才写行，存量老卡不受影响；
        # perms_version 用 `is not None` 判（0 是合法值＝配置不可用，
        # 不是「没写过」）。
        *([f"perms: {json.dumps(list(meta['perms']), ensure_ascii=False)}"]
          if meta.get("perms") is not None else []),
        *([f"perms_version: {int(meta['perms_version'])}"]
          if meta.get("perms_version") is not None else []),
        # [S4-1/S4-2] methodology_refs / pending_sync 的 FM 行同步删除。
        # 只删 meta 初始化而留着这两行，meta.get 的默认值会把它们原样写回，
        # 等于没删。存量卡的这两行在其下次写入时自然消失（字段死，无读取方）。
        f"created_at: {_q(meta['created_at'])}",
        f"updated_at: {_q(meta['updated_at'])}",
        "---",
    ]
    parts = ["\n".join(fm), "", h_mission, ""]
    if mission.strip():
        parts += [mission.strip("\n"), ""]
    parts += [h_status, ""]
    if status_block.strip():
        parts += [status_block.strip("\n"), ""]
    parts += [h_rulings, ""]
    if rulings.strip():
        parts += [rulings.strip("\n"), ""]
    parts += [HISTORY_HEADING, ""]
    parts += history_lines
    return "\n".join(parts) + "\n"


def _normalize_update_keys(updates: dict[str, Any]) -> dict[str, Any]:
    """updates 键归一（S2-7）：旧正文键→新正文键，其余键原样透传。

    两套键并存于同一份 dict，落盘只经 _render_card 一处 ⇒ 不产生第二套真值。
    新旧同键同时出现时以新键为准（旧键被覆盖）——新键是当前口径。
    """
    out: dict[str, Any] = {}
    for k, v in updates.items():
        out[OLD_KEY_TO_NEW.get(k, k)] = v
    return out


def _parse_history_lines(history_text: str) -> list[str]:
    return [line for line in history_text.splitlines() if line.strip()]


def _append_history_line(content: str, line: str) -> str:
    """在 '## 修改历史' 小节末尾追加一行；小节不存在则在文件末尾补建。

    [卡片信息结构改造 G-1] 区名改取 HISTORY_HEADING 常量（两形态同名，
    改动集中一处；原为 docstring 与判定各写一份字面量）。
    """
    lines = content.splitlines(keepends=True)
    heading_idx = None
    for i, l in enumerate(lines):
        if l.strip() == HISTORY_HEADING:
            heading_idx = i
            break
    if heading_idx is None:
        suffix = "" if content.endswith("\n") else "\n"
        return content + f"{suffix}\n{HISTORY_HEADING}\n\n{line}\n"
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

def _meta_from_row(work_id: str, row: Any) -> dict[str, Any]:
    """库行 → meta（补旧读侧的两处兜底：空 card_type→"线索"、缺 parent→根卡）。

    [卡片存储迁移 SQLite] 与旧 ``parse_front_matter`` 读侧同口径，只是来源换成列。
    """
    meta = card_db.row_to_meta(row)
    if not meta.get("card_type"):
        meta["card_type"] = "线索"  # 旧卡兜底
    if meta.get("parent") is None:
        meta["parent"] = work_store.work_card_id_of(work_id)
    return meta


def _meta_of(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """轻量读 meta（不锁、不校验），卡片不存在返回 None。

    **同步**（供 _write_gate / _subtree_ids / toolcard 等同步调用方）：单条索引
    SELECT，与改造前的同步文件读同量级；异步写路径不经过它。
    """
    def _fn(conn: Any) -> Optional[dict[str, Any]]:
        row = card_db.get_card_row(conn, card_id)
        if row is None:
            return None
        return _meta_from_row(work_id, row)

    try:
        return card_db.read_sync(work_id, _fn, None)
    except card_db.CardsNotMigratedError:
        raise
    except Exception:  # noqa: BLE001 与旧行为一致：读不动＝没有
        return None


def load_type_registry() -> dict[str, Any]:
    """加载类型注册表（卡片分类基座 T2）。

    独立 JSON 配置文件（config/card_type_registry.json），不进 DB——
    改词表不改 schema（任务书 §四裁断 3）。

    [沙箱纪律] 路径由模块级常量 TYPE_REGISTRY_PATH 派生，测试可monkeypatch
    改写（与 WORKS_DIR 同一套隔离手法，见 tests/conftest.py）。
    加载失败（文件缺失/坏 JSON）→ 返回空 dict 并留痕，调用方按「无注册表」
    降级放行——**注册表是校验源，不是可用性依赖**，不得因它阻断写路径。
    """
    import json as _json
    try:
        raw = TYPE_REGISTRY_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[card_type_registry] 注册表读取失败（按无注册表放行）: %s", exc)
        return {}
    try:
        data = _json.loads(raw)
    except ValueError as exc:
        logger.warning("[card_type_registry] 注册表解析失败（按无注册表放行）: %s", exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("[card_type_registry] 注册表结构异常（按无注册表放行）")
        return {}
    return data


def _registry_codes(registry: dict[str, Any], section: str) -> set[str]:
    """从注册表取某节的 code 集合（项目分类或 legacy）。"""
    return {str(x.get("code", "")) for x in (registry.get(section) or []) if isinstance(x, dict)}


def _l1_type_spec(registry: dict[str, Any], project_class: str,
                  card_type: str) -> Optional[dict[str, Any]]:
    """取 L1 类型规格（按 project_class 词表 + card_type 定位）。"""
    vocab = (registry.get("l1_vocabularies") or {}).get(project_class) or {}
    for t in (vocab.get("types") or []):
        if isinstance(t, dict) and str(t.get("code", "")) == card_type:
            return t
    return None


def _l1_all_codes(registry: dict[str, Any]) -> set[str]:
    """注册表内所有已定义 L1 词表 code 的并集。"""
    out: set[str] = set()
    for vocab in (registry.get("l1_vocabularies") or {}).values():
        if not isinstance(vocab, dict):
            continue
        for t in (vocab.get("types") or []):
            if isinstance(t, dict):
                out.add(str(t.get("code", "")))
    return out


def _current_l1_codes(registry: dict[str, Any]) -> str:
    """已定义 L1 词表的可读摘要（错误提示用，机械拼接）。"""
    out: set[str] = set()
    for scope, vocab in (registry.get("l1_vocabularies") or {}).items():
        if not isinstance(vocab, dict):
            continue
        for t in (vocab.get("types") or []):
            if isinstance(t, dict):
                out.add(f"{scope}:{t.get('code', '')}")
    return "、".join(sorted(out)) or "（无）"


def _root_project_class(work_id: str) -> str:
    """读本树根卡的 project_class（读路径宽容：缺省视为空串＝未分类）。"""
    wid = work_store.work_card_id_of(work_id)
    m = _meta_of(work_id, wid) or {}
    return str(m.get("project_class", "") or "")


def _default_child_type(work_id: str) -> str:
    """[底座收口 T5.a] 按本树 project_class 取该树 L1 词表的子卡缺省类型。

    规则：取词表里**第一个 requires_owner=false 的类型**（sub_project/topic
    都是挂载主体；tool 是被调用的执行体，绝不作缺省）。
    映射不出（无注册表 / 树未标 pc / 词表全 requires_owner）→ 返回 ""，
    由调用方回落旧缺省"线索"（conftest 沙箱行为不变）。
    """
    registry = load_type_registry()
    if not registry:
        return ""
    tree_pc = _root_project_class(work_id)
    if not tree_pc:
        return ""
    vocab = (registry.get("l1_vocabularies") or {}).get(tree_pc) or {}
    for t in (vocab.get("types") or []):
        if isinstance(t, dict) and not t.get("requires_owner"):
            return str(t.get("code", "")) or ""
    return ""


def _write_gate(work_id: str, action: str, card_id: str,
                payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """写路径统一校验挂点（唯一门上的闸位）＝卡片分类基座 T3 三闸 + 状态机。

    [T-0007 补丁 · 写路径唯一门] 立此挂点（恒放行）；
    [卡片分类基座 T3] 在此挂三道闸 + 闸 4 状态机，注册表为
    config/card_type_registry.json。

    闸序（固定，短路）：
      闸1 非法值   —— card_type/project_class 必须在注册表内，legacy 写路径拒
      闸 2 词表归属 —— 子卡 card_type 须属本树 project_class 的 L1 词表
      闸 3 工具卡 owner —— card_type=="tool" 时 owner_card_id 非空且 owner 合法
      闸 4 状态机  —— 迁移须在 allowed_transitions 内

    契约：
      1. 只校验、不落盘、不改 payload（调用方持有写入权）；
      2. 拦截一律走 {"error": ...} 形态，与既有写函数的失败返回同构；
      3. **不得抛异常**——三个写函数的错误契约是返回 dict 而非 raise。

    [降级] 注册表缺失/坏JSON → 放行（不阻断写路径）；legacy 值在此读路径仍
    可经**存量文件**读取，但**经本闸写入即拒**（T6 口径）。
    """
    registry = load_type_registry()
    # 无注册表 → 不做任何校验（保持 T-0007 补丁时的 no-op 语义）
    if not registry:
        return None

    payload = payload or {}
    pc_codes = _registry_codes(registry, "project_classes")
    legacy_codes = _registry_codes(registry, "legacy_types")
    project_class = str(payload.get("project_class", "") or "")
    card_type = str(payload.get("card_type", "") or "")
    # [根卡判定·判据①③] ①已落盘且等于 work_card_id；③**根卡创建期**——payload
    # 带 project_class 且该 work 尚无根卡（create_work 先写 work.json 占位空串、
    # 建卡后才回填，创建那一刻判据①必为 False）。
    # [底座收口 T2.a] 判据②（card_type=="剧本"）已删——类型不再兼任根卡判据
    # （基座任务书 T5 划界声明落地；P1 硬前提由判据③承接，删兜底前先验通）。
    is_root = (card_id == work_store.work_card_id_of(work_id)) or (
        bool(project_class) and not work_store.work_card_id_of(work_id)
    )

    # ---------- 闸 1：非法值显式报错（废除静默降级） ----------
    if project_class and project_class not in pc_codes:
        return {"error": f"非法 project_class: {project_class}"
                         f"（只允许 {sorted(pc_codes)}）"}
    if card_type and card_type in legacy_codes:
        # [T6 渐进口径] 只拦「write_blocked=true」的 legacy 值。
        # 「剧本」「线索」暂 false——存量 3 根卡 + 67 子卡全属这两类，
        # 迁移未完成前拦截会让系统建不出任何卡（create_card 默认 card_type='线索'）。
        # T7 迁移完成后由注册表把「线索」改 true，无需改代码。
        blocked = {str(x.get("code", "")) for x in (registry.get("legacy_types") or [])
                   if isinstance(x, dict) and x.get("write_blocked")}
        if card_type in blocked:
            return {"error": f"card_type「{card_type}」已退役（legacy·写路径拦截），"
                             f"写入须用新词表值：{_current_l1_codes(registry)}"}
    if card_type and not _l1_all_codes(registry):
        # 注册表有结构但无任何 L1 词表（如只有 research_project 未立项）
        return {"error": f"card_type「{card_type}」不在任何已定义词表内"}

    # ---------- 闸 2：类型/项目分类一致性 ----------
    # [根卡豁免] 根卡的 card_type 是历史值（剧本），不进 L1 词表校验——
    # 根卡身份由 project_class 承载，词表只约束子卡。
    # [迁移期豁免] 未标 write_blocked 的 legacy 值（当前：「线索」）**不进闸 2**：
    # 闸 1 已放行它，此处再拒即自相矛盾，且会让存量默认 card_type 建不出卡。
    # 迁移期语义＝「旧卡可继续被写，新卡须用新词表」，由迁移脚本负责改存量。
    legacy_unblocked = {str(x.get("code", "")) for x in (registry.get("legacy_types") or [])
                        if isinstance(x, dict) and not x.get("write_blocked")}
    if card_type and not is_root and card_type not in legacy_unblocked:
        tree_pc = project_class or _root_project_class(work_id)
        if not tree_pc:
            return {"error": "本树根卡未标 project_class，无法校验 card_type"
                             "（须先给根卡标 project_class）"}
        spec = _l1_type_spec(registry, tree_pc, card_type)
        if spec is None:
            allowed = sorted(
                str(t.get("code", "")) for t in
                ((registry.get("l1_vocabularies") or {}).get(tree_pc) or {}).get("types") or []
                if isinstance(t, dict)
            )
            return {"error": f"card_type「{card_type}」不属于 project_class"
                             f"「{tree_pc}」的词表（只允许 {allowed}）"}
    # project_class 只许根卡写
    if project_class and not is_root:
        return {"error": "project_class 只允许写在剧本根卡上"}

    # ---------- 闸 3：工具卡 owner 必填且合法 ----------
    if card_type == "tool":
        owner = str(payload.get("owner_card_id", "") or "")
        if not owner:
            return {"error": "工具卡必须带 owner_card_id"
                             f"（指向调用它的子项目卡）"}
        if not card_exists(work_id, owner):
            return {"error": f"owner_card_id 指向的卡不存在: {owner}"}
        owner_meta = _meta_of(work_id, owner) or {}
        owner_type = str(owner_meta.get("card_type", "") or "")
        # [底座收口 T2.d] 正向判据取代负向（legacy∪剧本）：legacy 全退役后
        # 负向判据恒 False＝闸3 失效。
        # [工具卡底座 T11 · EC-183 · 用户 2026-10-09 11:54 裁定逐字「子项目卡或根卡」]
        # 放宽为 {sub_project, project}。**范围严格止于此二者**：
        # 放宽范围**不得自行扩大**至 topic 等其他类型（R-F / EC-183）——
        # topic 是否可作 owner 属未裁项，见任务书【待用户裁】，未裁前按拒处理。
        if owner_type not in TOOL_CARD_OWNER_TYPES:
            return {"error": f"owner「{owner}」的 card_type「{owner_type}」"
                             f"不在允许集合 {sorted(TOOL_CARD_OWNER_TYPES)} 内，"
                             f"不能作工具卡 owner"}
    # ---------- 工具卡不可有子卡 ----------
    if card_type == "tool":
        for other_id, other in iter_card_metas(work_id).items():
            if other_id == card_id:
                continue
            if other.get("parent") == card_id:
                return {"error": f"工具卡不可有子卡（{other_id} 挂在它下面）"}

    # ---------- 闸 4：状态机迁移 ----------
    desired = str(payload.get("state", "") or "")
    if desired:
        sm = registry.get("state_machine") or {}
        cur_meta = _meta_of(work_id, card_id) or {}
        cur = str(cur_meta.get("state", "") or sm.get("initial", "active"))
        allowed = ((sm.get("transitions") or {}).get(cur)) or []
        if desired != cur and desired not in allowed:
            return {"error": f"非法状态迁移 {cur} → {desired}"
                             f"（只允许 {allowed} 或保持 {cur}）"}

    return None


def cards_dir(work_id: str) -> Path:
    """cards 目录路径（公开接口，供跨层调用方取目录）。"""
    return _cards_dir(work_id)


def card_exists(work_id: str, card_id: str) -> bool:
    """卡片存在性检查（公开接口，供跨层调用方判存在）。"""
    def _fn(conn: Any) -> bool:
        return card_db.get_card_row(conn, card_id) is not None

    return bool(card_db.read_sync(work_id, _fn, False))


def iter_card_metas(work_id: str) -> dict[str, dict[str, Any]]:
    """轻量收集 work 下所有正卡的 meta（公开接口，供跨层调用方批量读）。

    返回 {card_id: meta}；meta 含 parent/card_type/links/relations 等字段，
    并补回 card_id 键（id 即主键，meta 内不重复存）。
    卡片不存在或读取失败返回空 dict。

    **同步**（公开契约不改）：单条 SELECT 全表，与改造前的同步目录扫描同量级。
    """
    def _fn(conn: Any) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for row in conn.execute("SELECT * FROM cards ORDER BY card_id"):
            cid = row["card_id"]
            m = _meta_from_row(work_id, row)
            m["card_id"] = cid
            out[cid] = m
        return out

    return card_db.read_sync(work_id, _fn, {}) or {}


def _subtree_ids(work_id: str, root_id: str) -> set[str]:
    """root_id 整棵子树（含 root 自身）。

    [卡片存储迁移 SQLite] 一次拉 parent 映射再在内存里走（等价移植；原为
    「每层 glob 全目录」的 O(N²)）。
    """
    def _fn(conn: Any) -> dict[str, Any]:
        return {r["card_id"]: r["parent"]
                for r in conn.execute("SELECT card_id, parent FROM cards")}

    parents: dict[str, Any] = card_db.read_sync(work_id, _fn, {}) or {}
    children: dict[str, list[str]] = {}
    for cid, p in parents.items():
        children.setdefault(p, []).append(cid)
    result: set[str] = set()
    stack = [root_id]
    while stack:
        cid = stack.pop()
        if cid in result:
            continue
        result.add(cid)
        stack.extend(children.get(cid, ()))
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
    card_type: str = "",
    parent: Optional[str] = None,
    charter: str = "",
    conclusion: str = "",
    project_class: str = "",
    owner_card_id: str = "",
    goal: str = "",
    skip_kick: bool = False,
) -> dict[str, Any]:
    """创建卡片 + 对应会话文件，更新 canvas 缓存。

    ``skip_kick=True``：**建卡不点火**——给「建卡方自己负责首跑」的调用路径用
    （`create_coder_card` 用 `_spawn_tracked` 带任务正文起跑；若这里也点一次，
    会起一个没有任务正文的空回合，把正式首跑挡在互斥体外面，实测 coder 卡
    因此零产出）。

    [底座收口 T5.a · 建卡入口适配] card_type 缺省从"线索"改为 ""：
    空值时按本树根卡 project_class 映射该树 L1 词表的唯一/首个子卡类型
    （rd_project→sub_project、research_project→topic）——「线索」退役后
    三个建卡入口（模型工具/前端 UI/文本通道）都不必各自知道词表。
    无注册表（降级态）⇒ 回落旧缺省"线索"，行为与改造前一致（conftest 沙箱）。
    根卡创建不受本映射影响——create_work 显式传 card_type="project"。

    [卡片信息结构改造 S1-2] 骨架注入四步：
      ①入参（charter＋conclusion 拼成的候选正文）先过区头检测；
      ②含任一区头 → 原样作正文；不含 → 以 NEW_CARD_SKELETON 常量为正文；
      ③过 split_body_sections 取三块；
      ④以 form="new" 交 _render_card。
    [S1-3] charter/conclusion 参数默认值保持 ""，签名不变——骨架不由参数承载。
    [S4-1/S4-2] meta 不再写 methodology_refs / pending_sync（死字段清理）。

    [卡片分类基座 T1] 新增两个**可选**参数（默认空串＝行为与改造前逐字一致）：
      - project_class：仅根卡可写；决定本树 L1 词表作用域
      - owner_card_id：仅工具卡必填；指向调用它的子项目卡
    空串 ＝不落库该字段（存量兼容），非空才写入。

    [卡片存储迁移 SQLite] 卡片进库：正文三块各占独立列；发号（meta.seq）与建卡
    同一事务（避免并发重号）；不再建 ``cards/`` 目录、不再写卡片 md。
    会话文件仍写 ``sessions/session_NNN.md``（会话不进库，方案 §三决策 2）。

    任何一步失败：保留已写下的库行/会话文件，记日志返回错误。
    """
    # work 必须存在
    if work_store.read_work(work_id) is None:
        return {"error": f"剧本不存在: {work_id}"}
    if source not in ALLOWED_SOURCE:
        return {"error": f"source 非法: {source}，只允许 {ALLOWED_SOURCE}"}
    try:
        # [底座收口 T5.a] 空类型按树映射；映射不出（无注册表）回落旧缺省
        if not card_type:
            card_type = _default_child_type(work_id)
        resolved_type = _resolve_card_type(card_type)
        resolved_parent = _normalize_parent(work_id, parent, resolved_type)
        _sessions_dir(work_id).mkdir(parents=True, exist_ok=True)
        now = _now_iso()
        # [S1-2②] 区头检测：入参拼正文，含任一区头则原样用，否则用骨架常量
        candidate = "\n\n".join(p for p in (charter, conclusion) if p and p.strip())
        has_heading = any(ln.strip().startswith("## ") for ln in candidate.splitlines())
        body_seed = candidate if has_heading else NEW_CARD_SKELETON
        sections = split_body_sections(body_seed)
        body = {
            "mission": sections.get("mission", ""),
            "status_block": sections.get("status_block", ""),
            "rulings": sections.get("rulings", ""),
        }
        history_line = f"- [{now}] 卡片创建"

        def _fn(conn: Any) -> tuple[str, Any, str, int]:
            # [卡片存储迁移 SQLite] 发号与建卡同事务：seq 取号 → 建卡 → 写回 seq，
            # 全部在一次 BEGIN IMMEDIATE 内（方案 §三决策 4）。
            seq = card_db.next_seq(conn)
            card_id = f"card_{seq:03d}"
            session_name = _session_name(card_id)
            # [自动模式 2026-10-17 · 回灌 5700 块0.1/0.2] 子卡**建卡时继承**父卡的
            # 自动模式（逐代传；父卡退出不影响已建子卡——继承是一次性快照，不回溯）。
            # 读父卡 auto_mode 走本事务内的同一连接（同事务内一致）。
            parent_meta: dict[str, Any] = {}
            if resolved_parent:
                _prow = card_db.get_card_row(conn, resolved_parent)
                if _prow is not None:
                    parent_meta = card_db.row_to_meta(_prow)
            is_auto = bool(parent_meta.get("auto_mode"))
            goal_text = str(goal or "").strip()
            meta: dict[str, Any] = {
                "title": title,
                "source": source,
                "card_type": resolved_type,
                "parent": resolved_parent,
                "tags": [],
                # [依赖关联线 2026-10-16] 两线并存：links＝通信线，relations＝依赖关联线。
                "links": [],
                "relations": [],
                "session_ref": f"sessions/{session_name}",
                # [卡片分类基座 T1] 新分类字段：非空才落库（空串＝不写，存量兼容）
                **({"project_class": project_class} if project_class else {}),
                **({"owner_card_id": owner_card_id} if owner_card_id else {}),
                # [S1·R-04] 新卡默认 active（与注册表 state_machine.initial 对齐）
                "state": "active",
                # [自动模式 2026-10-17 · 回灌 5700 块0.1] 四列：开关恒落 0/1、
                # 轮次恒落 0（同「state 恒落」口径，新卡不留 NULL 歧义）；
                # goal/goal_state 只在有目标时落（0.2 起继承到 auto 才置 active）。
                "auto_mode": 1 if is_auto else 0,
                "goal_rounds": 0,
                # [6702 移植] 等子卡意图位：新卡恒落 0（同「state 恒落」口径，
                # 不留 NULL 歧义）。
                "goal_await_children": 0,
                # [6702 移植] 等子卡活锁兜底：唤醒计数器新卡恒落 0。
                "goal_await_wakes": 0,
                **({"goal": goal_text} if goal_text else {}),
                **({"goal_state": "active"} if (is_auto and goal_text) else {}),
                # [权限底座重做 2026-10-12 · 裁定 4] 权限固化：建卡算一次写死进卡。
                # project_class 取法＝根卡用入参（此刻卡还没落盘，读不到自己），
                # 子卡从本树根卡继承（project_class 只挂根卡）。
                # [自动模式 2026-10-17 · 回灌 5700 块0.2] 父卡 auto ⇒ 本卡**全开**
                # （policy.auto_perms()——绕过类型分权，不是新增一种分权）。
                # [2026-10-19 实验 permexp] 自动模式不再全开权限：与普通卡同一份
                # 类型分权（compute_perms）。auto_mode 只保留「自动续轮」语义，
                # 不再改写 perms——原「绕过类型分权」口径已由本次实验撤除。
                **(_perm_fields(
                    project_class or _root_project_class(work_id), resolved_type)),
                "created_at": now,
                "updated_at": now,
            }
            # [T-0007 补丁] 写路径唯一门·create 闸位。插在建卡前、参数校验后。
            gate = _write_gate(work_id, "create", card_id, dict(meta))
            if gate is not None:
                return ("gate", gate, card_id, seq)
            card_db.insert_card(
                conn, {"card_id": card_id, "work_id": work_id, **meta}, body
            )
            card_db.append_history(conn, card_id, history_line, now)
            card_db.set_meta_value(conn, "seq", str(seq))
            _append_issued(conn, card_id)
            card_db.bump_rev(conn)
            return ("ok", card_id, card_id, seq)

        outcome, payload, card_id, seq = await card_db.run_write(work_id, _fn)
        if outcome == "gate":
            return payload

        # 会话文件仍为文件（会话不进库）；锁内不需要——路径唯一由 card_id 保证
        session_path = _sessions_dir(work_id) / _session_name(card_id)
        try:
            if not session_path.exists():
                session_path.write_text(
                    f"<!-- session: {card_id}, created: {now} -->\n", encoding="utf-8"
                )
        except OSError as exc:  # noqa: BLE001 会话文件写失败不阻断建卡
            logger.warning("会话文件创建失败（不阻断建卡）%s: %s", session_path, exc)

        logger.info("编号发放: work=%s card=%s seq=%d", work_id, card_id, seq)

        await canvas_store.update_cache(work_id)
        # [数据与代码分家 2026-10-03] 运行数据不再 git 提交，改走独立快照
        await data_snapshot.schedule(f"[snapshot][{work_id}] create: {card_id}")
        # [影子卡 2026-10-03] 建正卡即派生影子（一正一影，R-03）；失败只记日志不阻断建卡
        from . import shadow_store
        try:
            await shadow_store.derive_shadow(work_id, card_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("影子派生失败（不阻断建卡）work=%s card=%s: %s", work_id, card_id, exc)
        # [自动模式 2026-10-17 · 回灌 5700 块0.4 · D2 建卡即起跑] 子卡继承到 auto
        # 且带目标 ⇒ 点火。放在 create_card 内（而非各调用方）⇒ HTTP 与模型工具
        # 两条建卡入口**自动覆盖**。
        #
        # [调度改造 2026-10-19 · 5704 承载力] 原先是**直接 kick**（建卡即起一个回合）。
        # 实测 k3 几分钟建 104 张卡 ⇒ 104 个回合同时起跑 ⇒ 端点打爆、进程被
        # safe-delete 守卫杀掉。改为**入调度队列**：卡照建、目标照设、照样会跑，
        # 只是从「瞬间全点火」变成「按容量排队点火」。行为不变，只改同时性。
        # 调度器不可用（脚本/测试/未启动）⇒ 退回直点，保证旧路径逐字可用。
        if skip_kick:
            return await read_card(work_id, card_id)
        try:
            from ..api import scheduler as _sched
            _sched.enqueue(work_id, card_id)
        except Exception as exc:  # noqa: BLE001 入队失败 ⇒ 退回直点，绝不阻断建卡
            logger.warning("[sched] 入队失败，退回直点 work=%s card=%s: %s",
                           work_id, card_id, exc)
            try:
                from ..api import auto_driver
                auto_driver.kick(work_id, card_id)
            except Exception as exc2:  # noqa: BLE001 起跑失败不阻断建卡
                logger.warning("自动模式起跑失败（不阻断建卡）work=%s card=%s: %s",
                               work_id, card_id, exc2)
        return await read_card(work_id, card_id)
    except CardLockedError:
        raise
    except Exception as exc:
        logger.error("create_card 失败（已写下的库行保留）: %s", exc)
        return {"error": str(exc)}


def _append_issued(conn: Any, card_id: str) -> None:
    """把新号记进 meta.issued（审计用，与旧 .card_id_seq.json 的 issued 同义）。"""
    raw = card_db.get_meta_value(conn, "issued", "[]")
    try:
        issued = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    except (TypeError, ValueError):
        issued = []
    if not isinstance(issued, list):
        issued = []
    issued.append(card_id)
    card_db.set_meta_value(conn, "issued", json.dumps(issued, ensure_ascii=False))


async def read_card(work_id: str, card_id: str) -> dict[str, Any]:
    """读卡（库查）。

    [卡片存储迁移 SQLite] 返回契约保留 ``{meta, body:{mission,
    status_block,rulings,history}}``（history 为行列表）；**``raw`` 退役**——它原是「md 整篇
    文本」，与「区域独立、不留 md」相悖（方案 §三决策 1）。
    哈希校验与「外部修改」回写一并退役（不留 md，不让手改后失去对象）。
    """
    def _fn(conn: Any) -> Optional[dict[str, Any]]:
        row = card_db.get_card_row(conn, card_id)
        if row is None:
            return None
        meta = _meta_from_row(work_id, row)
        meta["card_id"] = card_id  # 补回 card_id（id 即主键，meta 内不重复存）
        meta.pop("status", None)   # [phase8.3] 二态废除：存量 status 读侧不外露
        return {
            "meta": meta,
            "body": {
                "mission": row["mission"] or "",
                "status_block": row["status_block"] or "",
                "rulings": row["rulings"] or "",
                "history": card_db.get_history(conn, card_id),
            },
        }

    result = await card_db.run_read(work_id, _fn, None)
    if result is None:
        raise CardNotFoundError(f"卡片不存在: {card_id}")
    return result


async def update_card(
    work_id: str,
    card_id: str,
    updates: Optional[dict[str, Any]],
    history_note: str,
) -> dict[str, Any]:
    """通用写入：应用修改→追加修改历史→刷新 updated_at/content_hash→写回→git 提交。

    updates 支持键（含 Phase 7 扩展）：
      title/tags/links/relations/session_ref/card_type/parent
      mission/status_block/rulings（正文块·新键）
      charter/conclusion/rejections（正文块·旧键，保留兼容，归一到新键后落同一处）

    [权限底座重做 2026-10-12 · 用户裁定 2] **类型不可变**：updates 里出现
    card_type / project_class 一律返回 {"error": ...}，卡面零改动。这使得下方
    根卡保护块里的「剧本→project 迁移特例」不可达（死分支），保留仅为沿革。

    [卡片信息结构改造 S2-7] 新旧两套键在函数内归一，落同一处存储——
    **不产生双写路径**（旧键不再有独立分支，只在归一表里查一次）。
    [S2-4] 写侧保留旧键路径：旧卡用旧键 PATCH 照旧可用，落盘区头随原形态不变。

    [phase8.3] status 二态已废除：updates 中若带 status 一律忽略（不报错）。
    """
    updates = updates or {}
    updates.pop("status", None)
    # [权限底座重做 2026-10-12 · 裁定 2] **类型不可变**：card_type 与
    # project_class 建卡时定死，此后一律拒改。前置到根卡保护与 card_type
    # 解析之前——不区分根卡/子卡、不设迁移特例；拒的时候卡面零改动。
    if "card_type" in updates or "project_class" in updates:
        return {"error": "卡片类型不可修改：card_type / project_class 建卡时定死"}
    # [S2-7] 旧键→新键归一（只此一处；下游一律只见新键）
    updates = _normalize_update_keys(updates)

    if not card_exists(work_id, card_id):
        raise CardNotFoundError(f"卡片不存在: {card_id}")

    # 先读现状（用于防环/根卡保护判定）
    current = await read_card(work_id, card_id)
    cur_meta = current["meta"]
    cur_type = str(cur_meta.get("card_type", "") or "")
    # [T07-1-4 + 底座收口 T2.c] 根卡保护判据统一为 card_id==work_card_id
    # （原 is_work_card 双轨「剧本」判据随类型退役删除）。
    is_root_card = card_id == work_store.work_card_id_of(work_id)
    _log_judgment_switch(
        "card_store.update_card.root_protect", card_id, cur_type == "project", is_root_card
    )

    # 根卡保护：card_type / parent 不允许修改。
    # [底座收口 T2.c/T3] 唯一放宽＝迁移对「剧本→project」（一次性、单向）：
    # 三张存量根卡迁移走唯一写门必须过这里；迁移完成后盘上再无「剧本」根卡，
    # 该分支自然成死路（无需回收）。
    if is_root_card and ("card_type" in updates or "parent" in updates):
        migration_pair = (
            "card_type" in updates
            and cur_type == "剧本"
            and str(updates.get("card_type", "") or "") == "project"
        )
        if not migration_pair:
            return {"error": "项目根卡的 card_type/parent 不允许修改"}

    # card_type 解析（静默降级已废除：非法值转 {"error": ...} 返回）
    if "card_type" in updates:
        try:
            updates["card_type"] = _resolve_card_type(updates["card_type"])
        except CardTypeValueError as exc:
            return {"error": str(exc)}

    # parent 变更防环
    if "parent" in updates and not is_root_card:
        new_parent = updates["parent"]
        if new_parent == card_id:
            return {"error": "parent 不能指向自身或后代"}
        if new_parent and new_parent != "" and _is_descendant(work_id, new_parent, card_id):
            return {"error": "parent 不能指向自身或后代"}
        # parent 卡必须存在，否则回退 work_card_id
        if new_parent and new_parent != "" and not card_exists(work_id, new_parent):
            new_parent = work_store.work_card_id_of(work_id)
            updates["parent"] = new_parent

    # [自动模式 2026-10-17 · 回灌 5700 块0.2] **开关动作**：auto_mode 变化 ⇒
    # 权限随之重算。0→1：perms 重算为全开集（policy.auto_perms()，专用
    # perms_version=-1）；1→0：按该卡 (project_class, card_type) 重算回正常集
    # （与 compute_perms 一致）。只认「有变化」——同值重复 PATCH 不重写 perms
    # （幂等，不制造无谓留痕）。U1：开关可随时切换，不是建卡定死。
    if "auto_mode" in updates:
        from ..permissions import policy
        new_auto = 1 if bool(updates.get("auto_mode")) else 0
        updates["auto_mode"] = new_auto
        if new_auto != (1 if bool(cur_meta.get("auto_mode")) else 0):
            # [2026-10-19 实验 permexp] 0/1 一律按类型分权重算——auto_mode 不再
            # 授予全开权限，只影响轮次驱动器。原 auto_perms() 分支已撤除。
            pc = str(cur_meta.get("project_class") or "") or _root_project_class(work_id)
            perms, ver = policy.compute_perms(pc, cur_type)
            updates[policy.PERM_KEY] = perms
            updates[policy.PERM_VERSION_KEY] = ver

    # [T-0007 补丁] 写路径唯一门·update 闸位（no-op 放行）。
    # 插在写盘前、全部参数校验（含根卡保护/card_type 降级/parent 防环）之后——
    # 此时 updates 已是归一后的最终形态，与各校验分支的退出路径互不干扰。
    gate = _write_gate(work_id, "update", card_id, dict(updates))
    if gate is not None:
        return gate

    # [卡片存储迁移 SQLite] 写库：meta 字段 UPDATE ＋ 正文三块 UPDATE ＋ 追加
    # card_history ＋ rev+1，全部一次事务。落盘键清单与旧 _render_card 的
    # 「只搬清单内键」同口径——不进清单的键仍被静默丢弃。
    _UPDATABLE_META_KEYS = (
        # [依赖关联线 2026-10-16] links＝通信线（原 relations 改名）；
        # relations＝依赖关联线（新线，模型侧 link_rel/unlink_rel 写它）。
        "title", "tags", "links", "relations", "session_ref",
        "card_type", "parent",
        "project_class", "owner_card_id", "state",
        "perms", "perms_version",
        "toolcard_script", "toolcard_step", "toolcard_round", "toolcard_final",
        "thinkchain_final",
        # [自动模式 2026-10-17 · 回灌 5700 块0.1] 四列。auto_mode＝用户侧开关
        # （PATCH 白名单，块0.5）；goal/goal_state/goal_rounds＝目标读写
        # （内部写：建卡、update_goal 工具、驱动器计轮、停止置 paused）——
        # 都在本唯一写门上落盘。
        "auto_mode", "goal", "goal_state", "goal_rounds", "goal_await_children",
        "goal_await_wakes",
    )

    def _fn(conn: Any) -> Optional[str]:
        row = card_db.get_card_row(conn, card_id)
        if row is None:
            return None
        now = _now_iso()
        meta_patch = {k: updates[k] for k in _UPDATABLE_META_KEYS if k in updates}
        meta_patch["updated_at"] = now
        body_patch = {
            "mission": updates.get("mission", row["mission"] or ""),
            "status_block": updates.get("status_block", row["status_block"] or ""),
            "rulings": updates.get("rulings", row["rulings"] or ""),
        }
        card_db.update_card_row(conn, card_id, meta_patch, body_patch)
        card_db.append_history(conn, card_id, f"- [{now}] {history_note}", now)
        card_db.bump_rev(conn)
        return now

    if await card_db.run_write(work_id, _fn) is None:
        raise CardNotFoundError(f"卡片不存在: {card_id}")

    await canvas_store.update_cache(work_id)
    # [数据与代码分家 2026-10-03] 运行数据不再 git 提交（曾因分支操作被整体回退），改走独立快照
    await data_snapshot.schedule(f"[snapshot][{work_id}] update: {card_id}")

    # [phase8.3] "剧本卡切确定→派生目标卡"触发已废除；
    # 目标卡改为创建剧本时即派生（见 derive_ending / work_store.create_work）。

    return await read_card(work_id, card_id)


async def delete_card(work_id: str, card_id: str) -> dict[str, Any]:
    """[用户删卡 2026-10-02] 删除卡片（库行 ＋ 修改历史）＋对应 session 文件。

    结构性防护（机械判据，不判内容）：
    - 剧本不存在 → {"error": ...}
    - 卡片不存在 → CardNotFoundError
    - card_id == work_card_id（剧本根卡）→ CardDeleteDeniedError（403 语义）
    - 有子卡（parent 链上任何卡挂它）→ CardDeleteDeniedError（防孤儿）

    [卡片存储迁移 SQLite] 删除顺序：库内 DELETE（卡行＋历史＋影子）→ 会话文件
    → canvas 重建 → 快照。**删卡只走本函数一个口**（改写后的铁律）。
    """
    if work_store.read_work(work_id) is None:
        return {"error": f"剧本不存在: {work_id}"}
    if card_id == work_store.work_card_id_of(work_id):
        raise CardDeleteDeniedError(f"剧本根卡不可删除: {card_id}")

    if not card_exists(work_id, card_id):
        raise CardNotFoundError(f"卡片不存在: {card_id}")

    # 子卡防护：任何卡的 parent 指向 card_id → 拒删
    for other_id, m in iter_card_metas(work_id).items():
        if other_id == card_id:
            continue
        if m.get("parent") == card_id:
            raise CardDeleteDeniedError(
                f"存在子卡 {other_id}，先删子卡或改挂他卡"
            )

    # [T-0007 补丁] 写路径唯一门·delete 闸位（no-op 放行）。
    # 插在所有结构性防护之后、实际删除之前——保证闸位放行后才动数据。
    gate = _write_gate(work_id, "delete", card_id, {})
    if gate is not None:
        return gate

    # [画布 2026-10-19] 删前留一份完整快照进回收站（撤销用）。
    # 会话正文是**文件**，必须在下面 unlink 之前读出来——晚一步就没了。
    session_text = ""
    try:
        sp = _session_path(work_id, card_id)
        if sp.exists():
            session_text = sp.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("回收站：会话读取失败（仍可撤销卡片本体）%s: %s", card_id, exc)

    def _fn(conn: Any) -> int:
        # 快照与删除在**同一个写事务**里：要么都成功，要么都不发生。
        # 分开做的话，中间崩溃会留下"卡删了、快照没写"的不可撤销状态。
        row = card_db.get_card_row(conn, card_id)
        if row is None:
            return 0
        hist = [{"seq": r[0], "ts": r[1], "text": r[2]} for r in conn.execute(
            "SELECT seq, ts, text FROM card_history WHERE card_id = ? ORDER BY seq", (card_id,))]
        payload = json.dumps(
            {"card": {k: row[k] for k in row.keys()}, "history": hist, "session": session_text},
            ensure_ascii=False,
        )
        card_db.trash_card(conn, card_id, work_id, payload, datetime.now().isoformat(timespec="seconds"))
        n = card_db.delete_card_row(conn, card_id)
        if n:
            card_db.bump_rev(conn)
        return n

    if not await card_db.run_write(work_id, _fn):
        raise CardNotFoundError(f"卡片不存在: {card_id}")

    session_path = _session_path(work_id, card_id)
    try:
        if session_path.exists():
            session_path.unlink()
    # [2026-10-19 崩溃修复] 沙箱的 safe-delete 守卫在"一次删超阈值"时抛
    # SystemExit（BaseException，普通 except Exception 抓不住）⇒ 会一路冒到
    # 主线程把整个进程杀掉（实测 54 并发时撞了两次）。删除是维护动作，
    # 失败只记日志，绝不许反噬主链路。
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        logger.warning("session 文件删除失败（不中断）%s: %s", session_path, exc)

    # [影子卡 2026-10-03] 删正卡→影子同删（随正卡生命周期存续，不变量 3）；失败不中断
    from . import shadow_store
    try:
        await shadow_store.remove_shadow(work_id, card_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("影子删除失败（不中断）work=%s card=%s: %s", work_id, card_id, exc)

    await canvas_store.update_cache(work_id)
    # [数据与代码分家 2026-10-03] 运行数据不再 git 提交，改走独立快照
    await data_snapshot.schedule(f"[snapshot][{work_id}] delete: {card_id}")
    return {"ok": True, "deleted": card_id}


async def list_trash(work_id: str) -> list[dict[str, Any]]:
    """列出回收站里的卡（撤销条错过之后的**唯一找回入口**）。

    只回 card_id / 删除时间 / 标题：正文可能几百 KB，列表不该把它拖出来。
    """
    out: list[dict[str, Any]] = []

    def _fn(conn: Any) -> int:
        for row in card_db.list_trash(conn, work_id):
            payload = json.loads(row["payload"])
            card = payload.get("card") or {}
            out.append({
                "card_id": row["card_id"],
                "deleted_at": row["deleted_at"],
                "title": card.get("title") or row["card_id"],
                "parent": card.get("parent") or "",
            })
        return len(out)

    await card_db.run_write(work_id, _fn)
    return out


async def restore_card(work_id: str, card_id: str) -> dict[str, Any]:
    """从回收站恢复一张卡（撤销删除）。

    [画布 2026-10-19] 恢复必须**保留原 card_id**：卡片正文里到处是
    "见 card_037"、links 里也存着 id，换一个新 id 恢复等于把这些引用全指向空气。
    所以走回收站快照，不重新生成 id。

    幂等：卡已存在 → 返回 {"ok": True, "already": True}，不报错（前端重复点撤销
    不该弹红字）。回收站里没有 → 返回 {"error": ...}。
    """
    if work_store.read_work(work_id) is None:
        return {"error": f"剧本不存在: {work_id}"}

    session_text: list[str] = []

    def _fn(conn: Any) -> int:
        row = card_db.get_trash(conn, card_id)
        if row is None:
            return 0
        payload = json.loads(row["payload"] if not isinstance(row, tuple) else row[2])
        card = payload.get("card") or {}
        session_text.append(payload.get("session") or "")
        if not card_db.restore_card_row(conn, card, payload.get("history") or []):
            return -1                      # 已存在
        card_db.drop_trash(conn, card_id)
        card_db.bump_rev(conn)
        return 1

    res = await card_db.run_write(work_id, _fn)
    if res is None:
        return {"error": f"卡片不存在: {card_id}"}
    if res == 0:
        # 回收站里没有这张卡：要么从没删过，要么已经恢复过了。
        # 两种情况都不该报错——前端重复点撤销是正常操作，幂等返回即可；
        # 卡确实存在才算"已恢复"，否则如实说找不到。
        if card_exists(work_id, card_id):
            return {"ok": True, "already": True, "card_id": card_id}
        return {"error": f"回收站里没有这张卡: {card_id}"}
    if res == -1:
        return {"ok": True, "already": True, "card_id": card_id}

    # 会话文件写回（正文丢了卡片还能用，但对话记录一起丢才是真损失）
    if session_text and session_text[0]:
        try:
            sp = _session_path(work_id, card_id)
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text(session_text[0], encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("恢复会话文件失败（卡片已恢复）%s: %s", card_id, exc)

    await canvas_store.update_cache(work_id)
    await data_snapshot.schedule(f"[snapshot][{work_id}] restore: {card_id}")
    return {"ok": True, "restored": card_id}


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
        # 目标卡 links 加一条指向剧本卡的"锚定"
        # [依赖关联线 2026-10-16] 系统派生的「锚定」是**通信线**（沿它 read_linked 可读），
        # 随旧数据留在 links 里，语义不变（不迁到新线——见施工回报 Open Item 2）。
        await _append_relation(work_id, ending_id, {
            "target": work_card_id, "type": "锚定",
        })
        await canvas_store.update_cache(work_id)  # links 变更同步进画布缓存
        # ending_derived 置 true（幂等标志）
        work_store.update_work(work_id, {"ending_derived": True})
        # [数据与代码分家 2026-10-03] 运行数据不再 git 提交，改走独立快照
        await data_snapshot.schedule(f"[snapshot][{work_id}] derive ending: {ending_id}")
        return ending_id
    except Exception as exc:  # noqa: BLE001
        logger.error("目标卡派生异常 work=%s: %s", work_id, exc)
        return None


async def _append_relation(work_id: str, card_id: str, rel: dict[str, Any]) -> None:
    """向指定卡的 **links（通信线）** 追加一条 relation（不重复）。

    [依赖关联线 2026-10-16] 本函数是系统派生「锚定」的写入通道——锚定是通信线
    （沿它 read_linked 可读对方），故随旧数据写在 links 上，语义一字不变。
    """
    def _fn(conn: Any) -> None:
        row = card_db.get_card_row(conn, card_id)
        if row is None:
            return
        rels = list(card_db.row_to_meta(row).get("links") or [])
        if any(r.get("target") == rel.get("target")
               and r.get("type") == rel.get("type") for r in rels):
            return
        rels.append(rel)
        now = _now_iso()
        card_db.update_card_row(conn, card_id, {"links": rels, "updated_at": now}, {})
        card_db.append_history(conn, card_id, f"- [{now}] 追加 relation: {rel}", now)
        card_db.bump_rev(conn)

    await card_db.run_write(work_id, _fn)
