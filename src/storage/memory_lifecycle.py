# -*- coding: utf-8 -*-
"""记忆生命周期（照抄 dsh `gen3/lifecycle/record-use.ts` ＋ `reclaim.ts` 的机械部分）。

**纯函数、零 LLM、零 IO**——和 `memory_domain.check_invariants` 同一哲学：判定归判定，
落库归调用方。作用于因果链（`list[CausalNode]`），返回**新链**（不改入参）。

## 照抄的口径

`record-use.ts`（时效账本，**`lastUsedAt` 的唯一定价口**）：
- `entryTouchUpdate(cur, seq, role, now)`：目标条目 **＋ 沿唯一父链（I5 |upper|≤1）向上**
  全部刷新 `lastUsedAt`；
- `reviveUpdate(cur, seq, now)`：`sunk=false` ＋ 目标与父链同次落账；
- 父链遍历**环防御封顶 64**（dsh 同值）。

`reclaim.ts`（时效回收，两个存储动作）：
- **深层沉没**：`level ≥ 2` 且 `now - lastUsedAt > T_SINK_MS` ⇒ 整棵 lower 子树标
  `sunk=True`（从渲染/索引剔除；条目/锚点/coveredSeqs/原文**全保留**，幂等、mark 不动）；
- **顶层退常驻**：顶层（level=1）`status=done` 且超 `T_TOP_MS`、且不在最近 `R_TOP` 条内
  ⇒ `sunk=True`；
- **无 touch 账的条目豁免**（零误伤优先：迁移/出生未触达的条目不沉没）。

常量（dsh 文件级 const，不进 config）：`T_SINK_MS=7d`、`T_TOP_MS=3d`、`R_TOP=6`。

## 本项目未搬的（dsh 依赖 obs-paths / monitor / tool_use 表）

`record-use` 的 `kind='toolRun'` 分支（写 `tool_use` 表）、`appendStageLine` 打点、
`monitor.recordDepth` 遥测——都依赖尚未移植的观测层，**本模块不含**。
"""

from __future__ import annotations

from typing import Any, Optional

# ── 常量（dsh 同值；文件级 const，不进 config schema）──
T_SINK_MS = 7 * 24 * 60 * 60 * 1000   # 深层沉没阈值：7 天
T_TOP_MS = 3 * 24 * 60 * 60 * 1000    # 顶层退常驻阈值：3 天
R_TOP = 6                              # 顶层最近 N 条豁免退常驻

#: 父链遍历环防御封顶（dsh 同值）
_ANCESTOR_GUARD = 64


def _seq(n: dict[str, Any]) -> int:
    return int((n.get("anchor") or {}).get("seq", -1))


def _role(n: dict[str, Any]) -> str:
    return str(n.get("role") or "gist")


def _level(n: dict[str, Any]) -> int:
    return int(n.get("level", 1) or 1)


def _find(chain: list[dict[str, Any]], seq: int, role: str = "gist") -> Optional[dict[str, Any]]:
    for n in chain:
        if _seq(n) == int(seq) and _role(n) == role:
            return n
    return None


def ancestor_seqs_of(chain: list[dict[str, Any]], seq: int) -> set[int]:
    """祖先链 seq 集（**唯一父链**向上；环防御封顶 64，dsh 同口径）。"""
    anc: set[int] = set()
    cursor = _find(chain, seq, "gist")
    for _ in range(_ANCESTOR_GUARD):
        if cursor is None:
            break
        ups = cursor.get("upper") or []
        if not ups:
            break
        up_seq = int(ups[0].get("seq", -1))
        if up_seq in anc:
            break
        anc.add(up_seq)
        cursor = _find(chain, up_seq, "gist")
    return anc


def entry_touch_update(chain: list[dict[str, Any]], seq: int, role: str,
                       now_ms: int) -> list[dict[str, Any]]:
    """条目命中刷新（**含父链传播**）——返回新链。

    `role='description'` 时**不传播**（dsh：描述子条不沿父链刷新，只有 gist 走父链）。
    """
    anc = ancestor_seqs_of(chain, seq) if role == "gist" else set()
    out: list[dict[str, Any]] = []
    for n in chain:
        hit = _seq(n) == int(seq) and _role(n) == role
        if hit or _seq(n) in anc:
            nn = dict(n)
            nn["lastUsedAt"] = int(now_ms)
            out.append(nn)
        else:
            out.append(n)
    return out


def revive_update(chain: list[dict[str, Any]], seq: int, now_ms: int) -> list[dict[str, Any]]:
    """复活：`sunk=False` ＋ 目标与父链 `lastUsedAt` 同次落账。"""
    anc = ancestor_seqs_of(chain, seq)
    out: list[dict[str, Any]] = []
    for n in chain:
        if _seq(n) == int(seq) or _seq(n) in anc:
            nn = dict(n)
            nn["lastUsedAt"] = int(now_ms)
            if _seq(n) == int(seq):
                nn["sunk"] = False
            out.append(nn)
        else:
            out.append(n)
    return out


def collect_subtree(chain: list[dict[str, Any]], root_seq: int) -> list[dict[str, Any]]:
    """子树收集（沿 `lower` 边，含同锚 description 子条；环防御靠 seen）。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(entry: dict[str, Any]) -> None:
        key = f"{_seq(entry)}#{_role(entry)}"
        if key in seen:
            return
        seen.add(key)
        out.append(entry)
        for lo in entry.get("lower") or []:
            lo_seq = int(lo.get("seq", -1))
            child = next(
                (x for x in chain
                 if _seq(x) == lo_seq
                 and any(int(u.get("seq", -2)) == _seq(entry) for u in (x.get("upper") or []))),
                None)
            if child is not None:
                walk(child)

    root = _find(chain, root_seq)
    if root is not None:
        walk(root)
    return out


def reclaim(chain: list[dict[str, Any]], now_ms: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """时效回收：返回 `(新链, 报告)`。

    - **深层沉没**：`level ≥ 2` 且 `now - lastUsedAt > T_SINK_MS` ⇒ 整棵子树 `sunk=True`；
    - **顶层退常驻**：顶层 `status=done` 且超 `T_TOP_MS`、且不在最近 `R_TOP` 条 ⇒ `sunk=True`；
    - **无 touch 账（缺 `lastUsedAt`）一律豁免**——零误伤优先（dsh 同口径）。
    - 幂等：已 `sunk` 的不重复计数。
    """
    sunk: set[tuple[int, str]] = set()

    # 深层沉没
    for n in chain:
        if _level(n) < 2:
            continue
        touched = n.get("lastUsedAt")
        if touched is None:
            continue                       # 无 touch 账 ⇒ 豁免
        if int(now_ms) - int(touched) > T_SINK_MS:
            for sub in collect_subtree(chain, _seq(n)):
                sunk.add((_seq(sub), _role(sub)))

    # 顶层退常驻：done ∧ 超 T_TOP_MS ∧ 非最近 R_TOP 条
    tops = [n for n in chain if _level(n) == 1]
    touched_tops = [n for n in tops if n.get("lastUsedAt") is not None]
    recent = {(_seq(n), _role(n)) for n in sorted(
        touched_tops, key=lambda x: int(x.get("lastUsedAt") or 0), reverse=True)[:R_TOP]}
    for n in tops:
        if str(n.get("status") or "") != "done":
            continue
        touched = n.get("lastUsedAt")
        if touched is None:
            continue
        if int(now_ms) - int(touched) <= T_TOP_MS:
            continue
        if (_seq(n), _role(n)) in recent:
            continue
        sunk.add((_seq(n), _role(n)))

    out: list[dict[str, Any]] = []
    newly = 0
    for n in chain:
        key = (_seq(n), _role(n))
        if key in sunk and not n.get("sunk"):
            nn = dict(n)
            nn["sunk"] = True
            out.append(nn)
            newly += 1
        else:
            out.append(n)
    return out, {"sunk_total": len(sunk), "newly_sunk": newly,
                 "t_sink_ms": T_SINK_MS, "t_top_ms": T_TOP_MS, "r_top": R_TOP}


def visible_chain(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """渲染/索引可见的链：剔除「沉底」与「已被归拢取代」（原文与条目本身**不删**）。

    `supersededBy` 由低频归拢（fold_l1）写：合并后旧结论不再露，但**留在链上可查**。
    """
    return [n for n in chain or []
            if not n.get("sunk") and not n.get("supersededBy")]
