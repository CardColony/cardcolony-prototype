# -*- coding: utf-8 -*-
"""记忆域 · 因果链模型（照抄 dsh `gen3/memory/memory-domain.ts` ＋ `gen3/condense/entry-invariants.ts`）。

## 为什么要有这个模块

我们的记忆条目原来是**扁平列表**（`{seq, conclusion, evidence, …}`）——这正是"移植不到位"
的那一层：dsh 的条目是**因果链上的节点**，靠 `anchor / level / role / upper / lower /
coveredSeqs` 组成一棵可校验、可追溯的图。扁平条目做不到：

- **追溯**：`coveredSeqs` 明确"这条压自哪些原文 seq"（我们已用 `seq_from/seq_to` 近似，
  但多对多、可跨段的能力没有）；
- **校验**：I1–I5 五条不变量**只能**在图结构上判定；
- **生长**：`upper/lower` 让记忆能收敛成层级（gist 摘要 vs description 描述），
  扁平列表只能一路堆。

## 照抄的口径（逐字对应 dsh）

`CausalNodeShape`（只搬本项目需要的字段；dsh 里规则/知识/soul 相关的不搬）：
```
anchor {sessionId, seq}      gist        status: ongoing|done
kind: fact|decision|todo|exclusion|assumption
coveredSeqs[]                process     lesson
start / result               docPaths[]  docRefs[{path, seq}]
merged  level  role: gist|description   upper[]  lower[]
lastUsedAt  sunk  degraded  importance: critical|normal|detail
eventRef  supersedes[]  supersededBy  timeline{from,to}  batchNo
children[]  complex  noSubtopic  boundaryObjection[]  update{changes,newCoveredIdx}
```

五条不变量（dsh `entry-invariants.ts`，**纯函数、零 LLM、零 IO**）：
- **I1 互链**：`C∈N.lower ⟺ N.anchor∈C.upper`（含悬空/半边/身份重复）
- **I2 层级差**：`C∈N.lower → C.level = N.level + 1`
- **I3 同层收敛**：同一父下 gist 子条 `coveredSeqs` 互不重叠
- **I4 顶层**：`level=1 ⟺ upper 空`（双向）
- **I5 森林**：`|upper| ≤ 1`

键口径（dsh ADJ-1）：条目身份＝**裸 anchor**（`sessionId + seq`）；解析态按
`(anchor, 对端 role)` 复合消歧——`C ∈ N.lower` 判定的实现见 `_lower_element_resolves`。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# 枚举（与 dsh schema 逐字一致）
# ---------------------------------------------------------------------------

KINDS: tuple[str, ...] = ("fact", "decision", "todo", "exclusion", "assumption")
STATUSES: tuple[str, ...] = ("ongoing", "done")
ROLES: tuple[str, ...] = ("gist", "description")
IMPORTANCE: tuple[str, ...] = ("critical", "normal", "detail")

#: `level` 缺省、`role` 缺省（dsh `normalize()` 同口径）
DEFAULT_LEVEL = 1
DEFAULT_ROLE = "gist"


# ---------------------------------------------------------------------------
# 构造与归一化
# ---------------------------------------------------------------------------

def new_node(session_id: str, seq: int, gist: str, *,
             kind: str = "fact", status: str = "ongoing",
             **extra: Any) -> dict[str, Any]:
    """造一个合法节点（字段缺省同 dsh；`extra` 里允许带 coveredSeqs/process/upper/lower…）。"""
    if kind not in KINDS:
        raise ValueError(f"非法 kind: {kind}（只许 {KINDS}）")
    if status not in STATUSES:
        raise ValueError(f"非法 status: {status}（只许 {STATUSES}）")
    node: dict[str, Any] = {
        "anchor": {"sessionId": str(session_id), "seq": int(seq)},
        "gist": str(gist),
        "status": status,
        "kind": kind,
    }
    node.update(extra)
    return node


def _norm_one(node: dict[str, Any]) -> dict[str, Any]:
    """单节点归一化视图：optional 字段取缺省（level 缺省 1 / role 缺省 gist / upper·lower 缺省 []）。"""
    return {
        "node": node,
        "level": int(node.get("level", DEFAULT_LEVEL) or DEFAULT_LEVEL),
        "role": str(node.get("role") or DEFAULT_ROLE),
        "upper": list(node.get("upper") or []),
        "lower": list(node.get("lower") or []),
    }


def normalize(chain: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """整条链的归一化视图（顺序保持存储序）。"""
    return [_norm_one(n) for n in chain or []]


def _eq_anchor(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return str(a.get("sessionId")) == str(b.get("sessionId")) and \
        int(a.get("seq", -1)) == int(b.get("seq", -2))


def _covered(node: dict[str, Any]) -> list[int]:
    """`coveredSeqs` 缺省退化为 `[anchor.seq]`（dsh 同口径）。"""
    cs = node.get("coveredSeqs")
    if isinstance(cs, list) and cs:
        return [int(x) for x in cs]
    return [int(node.get("anchor", {}).get("seq", 0))]


# ---------------------------------------------------------------------------
# 五条不变量
# ---------------------------------------------------------------------------

def check_invariants(chain: list[dict[str, Any]]) -> list[str]:
    """检查一条链的全部条目不变量。

    返回违例清单（人类可读、以不变量编号开头，如 `"I1: …"`）；**空列表 = 通过**。
    与 dsh `checkInvariants()` 逐条对应（含消息措辞）。
    """
    v: list[str] = []
    es = normalize(chain)

    # ── 身份唯一性（I1 复合寻址的前提）：gist 按 seq 唯一；description 按 seq 至多一条 ──
    gist_by_seq: dict[int, dict[str, Any]] = {}
    desc_by_seq: dict[int, dict[str, Any]] = {}
    for e in es:
        seq = int(e["node"]["anchor"]["seq"])
        if e["role"] == "gist":
            if seq in gist_by_seq:
                v.append(f"I1: gist 身份重复 seq={seq}（同 seq 出现多条 gist 条目）")
            else:
                gist_by_seq[seq] = e
        elif seq in desc_by_seq:
            v.append(f"I1: description 身份重复 seq={seq}（构造保证每 gist 至多一条描述子条）")
        else:
            desc_by_seq[seq] = e

    def lower_element_resolves(parent: dict[str, Any], seq: int) -> bool:
        """反向解析：`seq` 的 lower 元素是否指向「upper 含 parent.anchor」的某条目。"""
        return any(
            int(e["node"]["anchor"]["seq"]) == int(seq)
            and any(_eq_anchor(u, parent["node"]["anchor"]) for u in e["upper"])
            for e in es)

    for e in es:
        ident = f"{e['role']}@{e['node']['anchor']['seq']}"

        # ── I5 森林：|upper| ≤ 1 ──
        if len(e["upper"]) > 1:
            v.append(f"I5: {ident} 的 |upper|={len(e['upper'])} > 1")

        # ── I4 顶层：level=1 ⟺ upper 空（双向）──
        if e["level"] == 1 and e["upper"]:
            v.append(f"I4: {ident} level=1 但 upper 非空")
        if e["level"] != 1 and not e["upper"]:
            v.append(f"I4: {ident} level={e['level']} 但 upper 为空")

        # ── I1 正向 + I2：每条 upper 边必须被父 lower 反向持有，且层级差恰为 1 ──
        for u in e["upper"]:
            useq = int(u.get("seq", -1))
            parent = gist_by_seq.get(useq)
            if parent is None or not _eq_anchor(parent["node"]["anchor"], u):
                v.append(f"I1: {ident} 的 upper 指向不存在的 gist 条目 seq={useq}")
                continue
            if not any(_eq_anchor(lo, e["node"]["anchor"]) for lo in parent["lower"]):
                v.append(f"I1: {ident} ← upper 指向 seq={useq}，但父条目 lower 未反向持有该锚（半边链）")
            # ── I2 层级差 ──
            if e["level"] != parent["level"] + 1:
                v.append(f"I2: {ident} level={e['level']} ≠ 父({parent['role']}@{useq}) "
                         f"level={parent['level']} + 1")

        # ── I1 反向：lower 每个元素必须存在 upper 回指本条目的条目 ──
        for lo in e["lower"]:
            if not lower_element_resolves(e, int(lo.get("seq", -1))):
                v.append(f"I1: {ident} 的 lower 含悬空元素 seq={lo.get('seq')}"
                         "（无任何条目 upper 回指本条目）")

        # ── I3 同层收敛：同一父下的 gist 子条 coveredSeqs 互不重叠 ──
        if e["role"] == "gist" and len(e["lower"]) > 1:
            gist_children = []
            for lo in e["lower"]:
                c = gist_by_seq.get(int(lo.get("seq", -1)))
                if c is not None and any(_eq_anchor(u, e["node"]["anchor"]) for u in c["upper"]):
                    gist_children.append(c)
            for i in range(len(gist_children)):
                for j in range(i + 1, len(gist_children)):
                    si = set(_covered(gist_children[i]["node"]))
                    sj = set(_covered(gist_children[j]["node"]))
                    if si & sj:
                        v.append(
                            f"I3: {ident} 的 gist 子条 seq={gist_children[i]['node']['anchor']['seq']}"
                            f" 与 seq={gist_children[j]['node']['anchor']['seq']} coveredSeqs 重叠")
    return v


def is_valid(chain: list[dict[str, Any]]) -> bool:
    """便捷判据：无违例即合法。"""
    return not check_invariants(chain)


def make_description_child(parent: dict[str, Any], text: str,
                           role: str = "description") -> dict[str, Any]:
    """造一个 **description 子节点**（dsh `migrate-archive` 的物化规则，逐条照抄）。

    | dsh 规则 | 本实现 |
    |---|---|
    | **anchor ＝其父 gist 的 anchor**（ADJ-1=A：身份撞锚由**解析态**按 `(anchor, 对端 role)` 复合消歧承担，**存储零变更**） | 同 |
    | `role='description'` | 同 |
    | `level = 父.level + 1` | 同 |
    | `upper = [父 anchor]` | 同 |
    | `coveredSeqs` ＝父的拷贝（父没有则 `[父.anchor.seq]`） | 同 |
    | **文本 == 父 gist ⇒ `degraded=true`**（没写出新东西 ⇒ 降级标记） | 同 |

    父链侧由调用方负责回填 `lower`（否则 I1 半边链）——用 `link_child()`。
    """
    p_anchor = dict(parent.get("anchor") or {})
    p_covered = parent.get("coveredSeqs")
    covered = list(p_covered) if (isinstance(p_covered, list) and p_covered) \
        else [int(p_anchor.get("seq", 0))]
    child = {
        "anchor": p_anchor,                      # ← 与父同 anchor（ADJ-1=A）
        "gist": str(text or ""),
        "status": str(parent.get("status") or "ongoing"),
        "kind": str(parent.get("kind") or "fact"),
        "role": role,
        "level": int(parent.get("level", DEFAULT_LEVEL) or DEFAULT_LEVEL) + 1,
        "upper": [p_anchor],                    # ← 指向父
        "lower": [],
        "coveredSeqs": covered,                  # ← 父的拷贝
    }
    if str(text or "") == str(parent.get("gist") or ""):
        child["degraded"] = True                 # 没写出新东西 ⇒ 降级
    return child


def link_child(chain: list[dict[str, Any]], parent: dict[str, Any],
               child: dict[str, Any]) -> list[dict[str, Any]]:
    """把子节点挂进父的 `lower`（**I1 互链的另一半**）——返回新链。

    父链侧 `upper` 已在 `make_description_child` 里写好；这里只补 `lower`，
    否则 I1 判"半边链"。
    """
    out: list[dict[str, Any]] = []
    p_seq = int((parent.get("anchor") or {}).get("seq", -1))
    c_anchor = dict(child.get("anchor") or {})
    for n in chain:
        if int((n.get("anchor") or {}).get("seq", -2)) == p_seq \
                and str(n.get("role") or "gist") == str(parent.get("role") or "gist"):
            nn = dict(n)
            lowers = list(nn.get("lower") or [])
            if not any(int((x or {}).get("seq", -3)) == int(c_anchor.get("seq", -4))
                       for x in lowers):
                lowers.append(c_anchor)
            nn["lower"] = lowers
            out.append(nn)
        else:
            out.append(n)
    return out


def migrate_archive(chain: list[dict[str, Any]],
                    archive: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """**archive → 条目空间物化**（照抄 dsh `condense/migrate-archive.ts` 的纯变换）。

    dsh 口径：archive 里存着"描述层正文"，条目空间只有 gist；物化把描述层正文
    变成 **description 子节点**（规则见 `make_description_child`）。

    - **孤儿描述跳过并计数**（父 gist 不在链上 ⇒ 不入条目空间，否则 I1 必违例）；
    - 物化后跑 `check_invariants`：**有违例就拒绝**（返回违例清单，调用方不写库）;
    - 幂等：已存在同 (anchor, role='description') 的子节点 ⇒ 不重复造。

    返回 `(新链, 报告)`；报告含 `created / skippedOrphans / violations`。
    """
    out: list[dict[str, Any]] = []
    existing_desc = {
        (int((n.get("anchor") or {}).get("seq", -1)), str(n.get("role") or "gist"))
        for n in chain
    }
    created = 0
    skipped = 0
    for row in archive or []:
        text = str(row.get("process") or "")
        if not text:
            skipped += 1
            continue
        seq = int(row.get("seq", -1))
        parent = next((n for n in chain
                       if int((n.get("anchor") or {}).get("seq", -2)) == seq
                       and str(n.get("role") or "gist") == "gist"), None)
        if parent is None:
            skipped += 1            # 孤儿描述：不入条目空间
            continue
        if (seq, "description") in existing_desc:
            continue                # 幂等
        child = make_description_child(parent, text)
        out = link_child(out or chain, parent, child)
        out.append(child)
        existing_desc.add((seq, "description"))
        created += 1
    new_chain = out if out else list(chain)
    violations = check_invariants(new_chain)
    return new_chain, {"created": created, "skippedOrphans": skipped,
                       "violations": violations}


def covered_seqs_of(chain: list[dict[str, Any]]) -> list[int]:
    """整条链覆盖的全部原文 seq（升序去重）——追溯"这条链读了哪些原文"。"""
    out: set[int] = set()
    for n in chain or []:
        out.update(_covered(n))
    return sorted(out)


# ---------------------------------------------------------------------------
# 扁平条目 → 单层链（过渡形态：模型还没产出 upper/lower 时的合法退化）
# ---------------------------------------------------------------------------

def attach_descriptions(nodes: list[dict[str, Any]], descs: list[dict[str, Any]],
                        session_id: str) -> tuple[list[dict[str, Any]], int]:
    """把 L2 子题挂成 **level=2 的 gist 子节点**（dsh `condense.ts:3278-3289` 口径）。

    [三步凝练 2026-10-29] 与 `make_description_child`（archive 物化那套：anchor 与父
    同、role=description）**不是一回事**——这里：

    - `anchor` 取子题**自己区间的首块**（不是父的）⇒ 与父天然不同、全链不重复；
    - `coveredSeqs` 由 `condense_two_layer.assemble` 机械切分 ⇒ 同父下互不重叠（I3）；
    - `upper=[父 anchor]` ＋ 父的 `lower` 回填 ⇒ I1 互链成立、I2 层级差恰为 1。

    这正是「锚点由构造保证唯一」的落点：原单次凝练让模型自己填 `anchor.seq`，
    340 条只占 287 个块号，物化 L2 时撞 I1 被整批拒写，L2 一条都落不成。

    返回 `(节点列表, 挂上的条数)`；`nodes` 里的父对象会被就地回填 `lower`。
    """
    by_seq = {int((n.get("anchor") or {}).get("seq", 0) or 0): n for n in nodes}
    out = list(nodes)
    added = 0
    for d in descs or []:
        p = by_seq.get(int(d.get("parent_seq") or 0))
        if p is None:
            continue
        child = new_node(session_id, int(d.get("seq") or 0),
                         str(d.get("process") or ""), kind="fact", status="done")
        child["level"] = 2
        child["role"] = "gist"
        child["upper"] = [dict(p.get("anchor") or {})]
        child["coveredSeqs"] = list(d.get("coveredSeqs") or [])
        if d.get("title"):
            child["title"] = str(d["title"])
        out.append(child)
        p["lower"] = list(p.get("lower") or []) + [dict(child["anchor"])]
        added += 1
    return out, added


def chain_from_entries(entries: list[dict[str, Any]],
                       session_id: str,
                       folded_fingerprints: set[str] | None = None) -> list[dict[str, Any]]:
    """把现有的扁平记忆条目转成**单层链**（每条＝一个 level=1 的 root 节点）。

    - `anchor.seq` ＝条目的 `seq`；`gist` ＝`conclusion`；
    - `coveredSeqs` ＝ `[seq_from..seq_to]`（条目已带追溯范围）；
    - `level=1`、`upper=[]`、`lower=[]` ⇒ **天然满足 I1–I5**（单层森林）。

    `folded_fingerprints`：低频归拢（`memory_store._maybe_fold` 的 `state.folded` 指纹账）
    里已归拢的成员。这些条目**物理仍在 entries**（既有实现不删），但已不是顶层散条 ⇒
    对应节点标 `supersededBy="folded"`，由 `memory_lifecycle.visible_chain` 过滤掉，
    **节点与原文都保留可查**（与 dsh「旧成员降层保留」同口径）。

    这是过渡形态：dsh 的链是模型产出 `upper/lower` 的多层图；我们先把"可校验、可追溯"
    这层立住，多层生长由 `migrate_archive` 物化 description 子节点承担（见回报 §五）。
    """
    folded = folded_fingerprints or set()
    chain: list[dict[str, Any]] = []
    for e in entries or []:
        # [三步凝练 2026-10-29] 条目现在就是 CausalNode（`anchor`/`gist`/`level`/
        # `upper`/`lower`）。原实现只读旧字段 `seq`/`conclusion` 并强制 `level=1`：
        # 链里 gist 全空、锚点全 0、L2 子节点被拍平——与「记忆区 92 行 [块?]」同源。
        a = e.get("anchor")
        if isinstance(a, dict):
            try:
                seq = int(a.get("seq", 0) or 0)
            except (TypeError, ValueError):
                seq = 0
        else:
            seq = int(e.get("seq", 0) or 0)
        gist = str(e.get("gist") or e.get("conclusion") or "")
        status = str(e.get("status") or "ongoing")
        kind = str(e.get("kind") or "fact")
        node = new_node(session_id, seq, gist,
                        kind=kind if kind in KINDS else "fact",
                        status=status if status in STATUSES else "ongoing")
        covered = e.get("coveredSeqs")
        if not (isinstance(covered, list) and covered):
            lo = e.get("seq_from")
            hi = e.get("seq_to")
            if lo is not None and hi is not None:
                covered = list(range(int(lo), int(hi) + 1))
            else:
                covered = [seq]
        node["coveredSeqs"] = [int(x) for x in covered]
        # 层级与父子边**原样保留**：L2 子节点靠 `upper` 挂在 L1 下、父靠 `lower`
        # 反向持有（I1 互链）。强制 level=1 会把两层链拍平成单层。
        node["level"] = int(e.get("level", 1) or 1)
        node["upper"] = [dict(u) for u in (e.get("upper") or []) if isinstance(u, dict)]
        node["lower"] = [dict(l) for l in (e.get("lower") or []) if isinstance(l, dict)]
        if str(e.get("role") or "gist") != "gist":
            node["role"] = str(e.get("role") or "gist")
        # 过程/教训/起因/结果（模型有写就带，没写就不出键——dsh 同口径：optional 新增）
        for src, dst in (("process", "process"), ("evidence", "process"),
                         ("lesson", "lesson"), ("start", "start"), ("result", "result")):
            val = e.get(src)
            if val and not node.get(dst):
                node[dst] = str(val)
        # [F9/F10 · 2026-10-31] docPaths/docRefs/timeline/title 随链透传——archive
        # 已冻结（F17），这些字段只活在条目/节点行上；展开与渲染要从链上拿到。
        if isinstance(e.get("docPaths"), list) and e["docPaths"]:
            node["docPaths"] = [str(p) for p in e["docPaths"]]
        if isinstance(e.get("docRefs"), list) and e["docRefs"]:
            node["docRefs"] = [dict(r) for r in e["docRefs"] if isinstance(r, dict)]
        if isinstance(e.get("timeline"), dict) and (e["timeline"].get("from") or e["timeline"].get("to")):
            node["timeline"] = {"from": str(e["timeline"].get("from") or ""),
                                "to": str(e["timeline"].get("to") or "")}
        if e.get("title"):
            node["title"] = str(e["title"])
        # [2026-10-31 · 修「链覆写丢件」] 批级簿记键随链透传：`memory` 表一行两用
        # （entries 经 _save_to_db 写、链经 save_chain 写），save_chain 后写 ⇒ 载荷
        # 必须无损承载 entries，否则 load_memory 读回的 entries 丢 seq_from/
        # seq_to/occurred_at/fingerprint（幂等判定与追溯链随之失效）。
        for _k in ("fingerprint", "occurred_at", "seq_from", "seq_to"):
            if e.get(_k) is not None:
                node[_k] = e[_k]
        # 已被低频归拢吸收的成员：标 supersededBy（节点不删，渲染时过滤）
        if folded and str(e.get("fingerprint") or "") in folded:
            node["supersededBy"] = "folded"
        chain.append(node)
    return chain


# ---------------------------------------------------------------------------
# 落库 / 读库（dsh `memory` 表：每会话一条 = 链 + 两个半开水位）
# ---------------------------------------------------------------------------

def save_chain(work_id: str, card_id: str, chain: list[dict[str, Any]], *,
               condense_mark: Optional[int] = None,
               fold_mark: Optional[int] = None) -> bool:
    """把链写进 `memory` 表（幂等 upsert）。失败只记日志、不抛。"""
    import json
    import logging
    from datetime import datetime

    from . import card_db
    log = logging.getLogger("storage.memory_domain")
    payload = json.dumps(chain or [], ensure_ascii=False)
    now = datetime.now().astimezone().isoformat()

    def _fn(conn: Any) -> int:
        conn.execute(
            "INSERT INTO memory (card_id, chain, condense_mark, fold_mark, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(card_id) DO UPDATE SET "
            "chain=excluded.chain, "
            "condense_mark=COALESCE(excluded.condense_mark, memory.condense_mark), "
            "fold_mark=COALESCE(excluded.fold_mark, memory.fold_mark), "
            "updated_at=excluded.updated_at",
            (card_id, payload, condense_mark, fold_mark, now))
        return 1

    try:
        return bool(card_db._write_sync(work_id, _fn))  # noqa: SLF001 同包写事务边界
    except Exception as exc:  # noqa: BLE001 落库失败不阻断
        log.warning("[memory_domain] 链落库失败 card=%s: %s", card_id, exc)
        return False


def load_chain(work_id: str, card_id: str) -> list[dict[str, Any]]:
    """读回链（读不到＝空链，不抛）。"""
    import json

    from . import card_db

    def _fn(conn: Any) -> str:
        row = conn.execute("SELECT chain FROM memory WHERE card_id = ?", (card_id,)).fetchone()
        return str(row["chain"] or "") if row else ""

    try:
        raw = card_db.read_sync(work_id, _fn, "") or ""
    except Exception:  # noqa: BLE001
        return []
    if not raw:
        return []
    try:
        got = json.loads(raw)
        return got if isinstance(got, list) else []
    except (ValueError, TypeError):
        return []


# ---------------------------------------------------------------------------
# archive 接线（dsh `archive` 表：描述层 process ／ 文档路径 ／ 锚点索引）
# ---------------------------------------------------------------------------

def save_archive(work_id: str, card_id: str, chain: list[dict[str, Any]]) -> int:
    """把链上的**描述层**写进 `memory_archive`（幂等 upsert）。

    dsh 口径：`process`（描述层正文）、`docPaths`/`docRefs`（文档路径与首现 seq）
    **出生即随节点写入 archive**，与 chain 的 gist（结果）分离——chain 只留结论，
    长正文进 archive，展开（memory_expand）时按锚点取。
    没有这些字段的节点不写行（archive 只承载"有描述层"的条目）。
    """
    import json

    from . import card_db
    rows = []
    for n in chain or []:
        process = n.get("process")
        doc_paths = n.get("docPaths")
        doc_refs = n.get("docRefs")
        if not process and not doc_paths and not doc_refs:
            continue
        anchor = n.get("anchor") or {}
        rows.append((
            card_id, int(anchor.get("seq", 0) or 0), str(n.get("role") or "gist"),
            str(process or ""),
            json.dumps(doc_paths, ensure_ascii=False) if doc_paths else "",
            json.dumps(doc_refs, ensure_ascii=False) if doc_refs else "",
        ))
    if not rows:
        return 0

    def _fn(conn: Any) -> int:
        conn.executemany(
            "INSERT INTO memory_archive (card_id, seq, role, process, doc_paths, doc_refs) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(card_id, seq, role) DO UPDATE SET "
            "process=excluded.process, doc_paths=excluded.doc_paths, doc_refs=excluded.doc_refs",
            rows)
        return len(rows)

    try:
        return int(card_db._write_sync(work_id, _fn) or 0)  # noqa: SLF001 同包写事务边界
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger("storage.memory_domain").warning(
            "[memory_domain] archive 落库失败 card=%s n=%d: %s", card_id, len(rows), exc)
        return 0


def load_archive(work_id: str, card_id: str, seq: Optional[int] = None,
                 role: str = "gist") -> dict[str, Any]:
    """读描述层：给 `seq` 取单条；不给则取全表 `{(seq,role): row}`。"""
    import json

    from . import card_db

    def _fn(conn: Any) -> list[dict[str, Any]]:
        if seq is None:
            cur = conn.execute(
                "SELECT seq, role, process, doc_paths, doc_refs FROM memory_archive "
                "WHERE card_id = ? ORDER BY seq", (card_id,))
        else:
            cur = conn.execute(
                "SELECT seq, role, process, doc_paths, doc_refs FROM memory_archive "
                "WHERE card_id = ? AND seq = ? AND role = ?", (card_id, int(seq), role))
        return [dict(r) for r in cur.fetchall()]

    try:
        rows = card_db.read_sync(work_id, _fn, []) or []
    except Exception:  # noqa: BLE001
        return {}
    out: dict[Any, dict[str, Any]] = {}
    for r in rows:
        item = {
            "seq": int(r["seq"]), "role": str(r.get("role") or "gist"),
            "process": str(r.get("process") or ""),
            "docPaths": json.loads(r["doc_paths"]) if r.get("doc_paths") else [],
            "docRefs": json.loads(r["doc_refs"]) if r.get("doc_refs") else [],
        }
        out[(item["seq"], item["role"])] = item
    return out
