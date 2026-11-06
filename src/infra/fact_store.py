# -*- coding: utf-8 -*-
"""harness 数据集：部件化事实的持久化查询层（[2026-10-28 用户定：按 sqlite 重做]）。

## 为什么从内存改成 sqlite

第一版把事实全放内存（dict + 三本索引）。实测 work_027（875 MB / 153 个文件）：

    25 万条事实 ⇒ 内存 256 MB、每条约 1.1 KB、重启要重建 12 秒、重建峰值 493 MB。

而需求本身是**触发式**——日常不查询，只在「新建或修改策略」时回溯一次。也就是说
「查询 0.8 ms」这个优势根本用不上，却要每天付 256 MB 和 12 秒。用户 2026-10-28 定：按 sqlite 重做。

## 边界（用户约束，必须守）

- **不动 `cards.db`**：那是卡面与会话的唯一真相。本模块只写 ``works/<work>/facts.db``。
- **不动 `events/*.jsonl`**：那是上下文与回放的原文。本模块只读它。
- 本库是**派生数据**：删掉即可重建，不影响上下文与回放。所以它才是
  「harness 自己收集的事件信息」，7 天保留期的合法对象就是它。

## 表

    facts(seq, card, ts, ts_iso, type, run_id, round, parts)
        PRIMARY KEY (card, seq)        -- seq 只在卡内唯一，跨卡会撞号
        parts 是 JSON：该事件类型声明的零件（catalog 的 part + optional）

    file_watermark(path, size)         -- 增量重建用：每个事件文件已读到哪

索引：``(ts)`` / ``(type, ts)`` / ``(card, ts)`` —— 覆盖「时间窗」「什么操作」「哪张卡」三轴。

## 查询怎么走

SQL 只做**能走索引的三轴**（时间窗 / 类型 / 卡），``where`` 的字段条件在结果集上
用 ``harness_cond.match_where`` 过滤——语义与条件集完全一致，不另写一套；
而且三轴收窄后结果集通常只有几十到几百条，字段过滤是微秒级。

## 写入开销

WAL + ``synchronous=NORMAL``：单条 INSERT 约零点几毫秒，事件级写入可接受。
重建时用事务批量提交（每 2000 条一次），比逐条快一个数量级。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger("infra.fact_store")

#: 默认保留期：7 天（用户 2026-10-28 定）
DEFAULT_RETENTION_SEC = 7 * 86400

#: 事实库文件名（放在 work 目录下，与 cards.db 同级）
DB_NAME = "facts.db"

#: 重建时每多少条提交一次
_BATCH = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    seq     INTEGER NOT NULL,
    card    TEXT    NOT NULL DEFAULT '',
    ts      REAL,
    ts_iso  TEXT    NOT NULL DEFAULT '',
    type    TEXT    NOT NULL,
    run_id  TEXT    NOT NULL DEFAULT '',
    round   INTEGER NOT NULL DEFAULT 0,
    parts   TEXT    NOT NULL DEFAULT '{}',
    PRIMARY KEY (card, seq)
);
CREATE INDEX IF NOT EXISTS idx_facts_ts        ON facts(ts);
CREATE INDEX IF NOT EXISTS idx_facts_type_ts   ON facts(type, ts);
CREATE INDEX IF NOT EXISTS idx_facts_card_ts   ON facts(card, ts);
CREATE TABLE IF NOT EXISTS file_watermark (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL DEFAULT 0
);
"""

#: 逐字流帧：渲染用的 token 增量，**不是事实**。
#: 实测占 work_027 条数的 83%（122 万条），收进来只会把库撑大、把重建拖慢。
_TOKEN_FRAMES = frozenset({"reasoning_chunk", "content_chunk"})

#: 可作标量零件的类型
_SCALAR = (str, int, float, bool)


# ---------------------------------------------------------------- 时间
def parse_ts(raw: Any) -> Optional[float]:
    """ISO 字符串 / epoch 数字 ⇒ epoch 秒。解析不出 ⇒ None（**绝不当成过期**）。"""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _fmt_ts(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds")


def _is_token_frame(raw: bytes) -> bool:
    """行首快速判定逐字流帧（不做 json.loads）。

    事件记录的键序固定（seq/card_id/run_id/round/type/data/…），``type`` 必在
    正文之前，前 300 字节足够命中——跳过 122 万条时不必解析那 274 MB。
    """
    head = raw
    return b'"reasoning_chunk"' in head or b'"content_chunk"' in head


# ---------------------------------------------------------------- 摊平
def flatten_parts(ev_type: str, data: Any) -> dict[str, Any]:
    """按 catalog 的字段契约，把 data 摊成扁平零件（只取 part + optional）。

    嵌套对象摊成点号键（``source.kind``），深度只摊一层——再深的对象不是零件，
    是结构。列表原样保留（``contains`` / ``in`` 用得上）。
    """
    from . import event_hub

    d = data if isinstance(data, dict) else {}
    spec = event_hub.fields_of(str(ev_type or ""))
    keys = list(spec.get("part") or []) + list(spec.get("optional") or [])
    out: dict[str, Any] = {}
    for k in keys:
        if k not in d:
            continue
        v = d[k]
        if isinstance(v, dict):
            for kk, vv in v.items():
                if vv is None or isinstance(vv, _SCALAR):
                    out[f"{k}.{kk}"] = vv
            continue
        if v is None or isinstance(v, _SCALAR):
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = list(v)
    return out


# ---------------------------------------------------------------- 数据集
class FactStore:
    """按 work 分区的事实库（每个 work 一个 ``facts.db``）。"""

    def __init__(self, works_dir: Any = None,
                 retention_sec: float = DEFAULT_RETENTION_SEC) -> None:
        if works_dir is None:
            from ..storage import card_store
            works_dir = card_store.WORKS_DIR
        self._root = Path(works_dir)
        self.retention_sec = float(retention_sec)
        self._conns: dict[str, sqlite3.Connection] = {}
        self._ingested = 0
        self._skipped = 0

    # ------------------------------------------------------------ 连接
    def db_path(self, work_id: str) -> Path:
        # [库搬出施工目录 2026-11-07] facts.db 也放 data/{wid}/（与 works/
        # 平级）——works/ 是卡的 exec 工作区，生产库不该躺在里面。
        return self._data_dir(work_id) / DB_NAME

    def _data_dir(self, work_id: str) -> Path:
        return self._root.parent / "data" / str(work_id)

    def _conn(self, work_id: str) -> sqlite3.Connection:
        wid = str(work_id)
        c = self._conns.get(wid)
        if c is not None:
            return c
        # 旧位置还在 ⇒ 原子搬进 data/（_conns 单例串行，无并发搬迁）
        from ..storage import card_store as _cs
        path = _cs.migrate_db_file(self._root / wid / DB_NAME, self.db_path(wid))
        path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript(_SCHEMA)
        c.commit()
        self._conns[wid] = c
        return c

    def close(self, work_id: Optional[str] = None) -> None:
        """关连接（进程退出/测试清理用）。"""
        if work_id is None:
            for c in self._conns.values():
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            self._conns.clear()
            return
        c = self._conns.pop(str(work_id), None)
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 写入
    def ingest(self, work_id: str, rec: dict[str, Any], *,
               commit: bool = True) -> Optional[dict[str, Any]]:
        """把一条事件记录摊成事实写库。返回事实（无类型 / 逐字流帧 ⇒ None）。"""
        ev_type = str(rec.get("type") or "")
        if not ev_type or not work_id:
            return None
        if ev_type in _TOKEN_FRAMES:
            self._skipped += 1
            return None
        seq = rec.get("seq")
        seq = int(seq) if isinstance(seq, int) and not isinstance(seq, bool) else 0
        t = parse_ts(rec.get("ts"))
        card = str(rec.get("card_id") or rec.get("card") or "")
        parts = flatten_parts(ev_type, rec.get("data"))

        fact: dict[str, Any] = {
            "seq": seq,
            "ts": str(rec.get("ts") or "") if t is None else _fmt_ts(t),
            "work": str(work_id),
            "card": card,
            "type": ev_type,
            "run": str(rec.get("run_id") or ""),
            "round": int(rec.get("round") or 0) if isinstance(rec.get("round"), int) else 0,
            "_t": t,
        }
        fact.update(parts)

        conn = self._conn(work_id)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO facts"
                " (seq, card, ts, ts_iso, type, run_id, round, parts)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (seq, card, t, fact["ts"], ev_type, fact["run"], fact["round"],
                 json.dumps(parts, ensure_ascii=False)))
            if commit:
                conn.commit()
            self._ingested += 1
        except sqlite3.Error as exc:  # noqa: BLE001 零阻断
            logger.warning("[fact_store] 写入失败 work=%s type=%s seq=%s: %s",
                           work_id, ev_type, seq, exc)
            return None
        return fact

    def sort(self, work_id: str) -> int:
        """空实现：sqlite 版按查询时 ORDER BY，不需要维护物理顺序（保留接口）。"""
        return 0

    # ------------------------------------------------------------ 淘汰
    def evict(self, work_id: str, now: Optional[float] = None) -> int:
        """按保留期删掉过期事实（**只删这份派生副本**）。返回删除条数。"""
        cutoff = (now if now is not None else time.time()) - self.retention_sec
        conn = self._conn(work_id)
        try:
            cur = conn.execute(
                "DELETE FROM facts WHERE ts IS NOT NULL AND ts < ?", (cutoff,))
            conn.commit()
            return int(cur.rowcount or 0)
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[fact_store] 淘汰失败 work=%s: %s", work_id, exc)
            return 0

    # ------------------------------------------------------------ 查询
    def query(self, work_id: str, *,
              types: Optional[Iterable[str]] = None,
              card: Optional[str] = None,
              cards: Optional[Iterable[str]] = None,
              from_: Optional[str] = None,
              to: Optional[str] = None,
              since: Optional[float] = None,
              until: Optional[float] = None,
              where: Optional[dict[str, Any]] = None,
              ctx: Optional[dict[str, Any]] = None,
              limit: int = 0,
              newest_first: bool = False) -> list[dict[str, Any]]:
        """按维度筛事实，返回**时间升序**列表。

        SQL 只做能走索引的三轴（时间窗 / 类型 / 卡），``where`` 在结果集上用
        ``harness_cond.match_where`` 过滤——语义与条件集同源，不另写一套。

        ``cards`` 收一组卡（「父卡对**子卡们**做了什么」天然是多卡）。
        ``limit``：取**最新**的 N 条（与"最近"语义一致），返回仍升序。
        """
        sql = ["SELECT seq, card, ts, ts_iso, type, run_id, round, parts FROM facts"]
        args: list[Any] = []
        cond: list[str] = []

        if since is not None:
            cond.append("ts IS NOT NULL AND ts >= ?")
            args.append(float(since))
        if until is not None:
            cond.append("ts IS NOT NULL AND ts <= ?")
            args.append(float(until))

        type_set = [str(t) for t in (types or [])]
        if type_set:
            cond.append("type IN (%s)" % ",".join("?" * len(type_set)))
            args.extend(type_set)

        card_set = [str(c) for c in (cards or [])]
        if card:
            card_set.append(str(card))
        if card_set:
            cond.append("card IN (%s)" % ",".join("?" * len(card_set)))
            args.extend(card_set)

        if cond:
            sql.append("WHERE " + " AND ".join(cond))
        sql.append("ORDER BY ts IS NULL, ts ASC, card, seq")

        conn = self._conn(work_id)
        try:
            rows = conn.execute(" ".join(sql), args).fetchall()
        except sqlite3.Error as exc:  # noqa: BLE001 查不到 ⇒ 空（宁可少发）
            logger.warning("[fact_store] 查询失败 work=%s: %s", work_id, exc)
            return []

        from . import harness_cond

        w = dict(where or {})
        if from_ is not None:
            w["from"] = from_
        if to is not None:
            w["to"] = to

        out: list[dict[str, Any]] = []
        for r in rows:
            fact = self._row_to_fact(work_id, r)
            if w and not harness_cond.match_where(fact, w, ctx or {}, event=fact):
                continue
            out.append(fact)

        if limit and len(out) > limit:
            out = out[-int(limit):]
        if newest_first:
            out = list(reversed(out))
        return out

    @staticmethod
    def _row_to_fact(work_id: str, r: Any) -> dict[str, Any]:
        seq, card, t, ts_iso, ev_type, run_id, round_, parts = r
        fact: dict[str, Any] = {
            "seq": int(seq or 0),
            "ts": str(ts_iso or ""),
            "work": str(work_id),
            "card": str(card or ""),
            "type": str(ev_type or ""),
            "run": str(run_id or ""),
            "round": int(round_ or 0),
            "_t": (float(t) if t is not None else None),
        }
        try:
            p = json.loads(parts) if parts else {}
        except (ValueError, TypeError):
            p = {}
        if isinstance(p, dict):
            fact.update(p)
        return fact

    def undated(self, work_id: str) -> list[dict[str, Any]]:
        """没有时间戳的事实（不进时间窗；重建时可据此报警）。"""
        conn = self._conn(work_id)
        try:
            rows = conn.execute(
                "SELECT seq, card, ts, ts_iso, type, run_id, round, parts"
                " FROM facts WHERE ts IS NULL ORDER BY card, seq").fetchall()
        except sqlite3.Error:  # noqa: BLE001
            return []
        return [self._row_to_fact(work_id, r) for r in rows]

    # ------------------------------------------------------------ 维护
    def clear(self, work_id: Optional[str] = None) -> None:
        """丢掉事实（**只丢这份派生副本**，账本与上下文不动）。"""
        if work_id is None:
            for wid in list(self._conns):
                self.clear(wid)
            return
        conn = self._conn(work_id)
        try:
            conn.execute("DELETE FROM facts")
            conn.execute("DELETE FROM file_watermark")
            conn.commit()
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[fact_store] 清空失败 work=%s: %s", work_id, exc)

    def stats(self) -> dict[str, Any]:
        works: dict[str, Any] = {}
        for wid in list(self._conns):
            try:
                n = self._conn(wid).execute("SELECT COUNT(*) FROM facts").fetchone()[0]
                span = self._conn(wid).execute(
                    "SELECT MIN(ts_iso), MAX(ts_iso) FROM facts WHERE ts IS NOT NULL"
                ).fetchone()
            except sqlite3.Error:
                continue
            works[wid] = {"facts": int(n or 0), "span": [x for x in (span or ()) if x]}
        return {"works": works, "ingested": self._ingested, "skipped": self._skipped}

    # ------------------------------------------------------------ 水位
    def watermark(self, work_id: str, path: str) -> int:
        try:
            row = self._conn(work_id).execute(
                "SELECT size FROM file_watermark WHERE path=?", (path,)).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0

    def set_watermark(self, work_id: str, path: str, size: int) -> None:
        try:
            conn = self._conn(work_id)
            conn.execute(
                "INSERT INTO file_watermark(path, size) VALUES(?,?)"
                " ON CONFLICT(path) DO UPDATE SET size=excluded.size",
                (path, int(size)))
            conn.commit()
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[fact_store] 水位写入失败 work=%s: %s", work_id, exc)


# ---------------------------------------------------------------- 重建
def rebuild_from_file(store: FactStore, work_id: str, path: Any,
                      *, since: Optional[float] = None,
                      chunk_bytes: int = 4 * 1024 * 1024,
                      max_bytes: int = 512 * 1024 * 1024,
                      incremental: bool = True) -> int:
    """从一份事件 jsonl **倒着**读到 ``since`` 为止，写进事实库。

    为什么倒读：账本按时间追加，最近的都在尾部。重建"最近 7 天"只需读尾部。

    ``incremental=True`` 时先看水位：文件没长过 ⇒ 直接返回 0（重启不重复劳动）。
    返回喂入条数。
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        return 0
    if not size:
        return 0

    key = str(p)
    start = 0
    if incremental:
        prev = store.watermark(work_id, key)
        if prev == size:
            return 0
        if 0 < prev < size:
            start = prev          # 只读新增部分（正着读，因为尾部是新的）

    if start:
        fed = _read_forward(store, work_id, p, start, since)
        store.set_watermark(work_id, key, size)
        return fed

    fed = 0
    tail = b""
    read_total = 0
    with open(p, "rb") as fh:
        pos = size
        while pos > 0 and read_total < max_bytes:
            step = min(chunk_bytes, pos)
            pos -= step
            fh.seek(pos)
            block = fh.read(step) + tail
            read_total += step
            lines = block.split(b"\n")
            tail = lines[0] if pos > 0 else b""
            body = lines[1:] if pos > 0 else lines
            stop = False
            for raw in reversed(body):
                if not raw.strip():
                    continue
                if _is_token_frame(raw):
                    continue
                rec = _loads(raw)
                if rec is None:
                    continue
                t = parse_ts(rec.get("ts"))
                if since is not None and t is not None and t < since:
                    stop = True
                    break
                if store.ingest(work_id, rec, commit=False) is not None:
                    fed += 1
                    if fed % _BATCH == 0:
                        store._conn(work_id).commit()
            if stop:
                break
    store._conn(work_id).commit()
    store.set_watermark(work_id, key, size)
    return fed


def _read_forward(store: FactStore, work_id: str, p: Path,
                  start: int, since: Optional[float]) -> int:
    """从字节偏移 ``start`` 往后读新增内容（增量路径）。"""
    fed = 0
    with open(p, "rb") as fh:
        fh.seek(start)
        for raw in fh:
            if not raw.strip():
                continue
            if _is_token_frame(raw):
                continue
            rec = _loads(raw)
            if rec is None:
                continue
            t = parse_ts(rec.get("ts"))
            if since is not None and t is not None and t < since:
                continue
            if store.ingest(work_id, rec, commit=False) is not None:
                fed += 1
                if fed % _BATCH == 0:
                    store._conn(work_id).commit()
    store._conn(work_id).commit()
    return fed


def rebuild_work(store: FactStore, work_id: str, events_dir: Any,
                 *, since: Optional[float] = None,
                 incremental: bool = True,
                 max_bytes_per_file: int = 512 * 1024 * 1024) -> int:
    """把一个 work 的事件目录整体重建进事实库（增量优先）。"""
    d = Path(events_dir)
    if not d.is_dir():
        return 0
    fed = 0
    for f in sorted(d.glob("*.jsonl")):
        fed += rebuild_from_file(store, work_id, f, since=since,
                                 max_bytes=max_bytes_per_file,
                                 incremental=incremental)
    if since is not None:
        store.evict(work_id)
    return fed


def _loads(raw: bytes) -> Optional[dict[str, Any]]:
    try:
        rec = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, TypeError):
        return None
    return rec if isinstance(rec, dict) else None


# ---------------------------------------------------------------- 进程级单例
_STORE: Optional[FactStore] = None


def store() -> FactStore:
    """进程级事实库（懒建；保留期 7 天）。"""
    global _STORE
    if _STORE is None:
        _STORE = FactStore()
    return _STORE


def reset(store_: Optional[FactStore] = None) -> FactStore:
    """替换单例（测试/重启重建用）。"""
    global _STORE
    _STORE = store_ or FactStore()
    return _STORE
