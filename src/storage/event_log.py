# -*- coding: utf-8 -*-
"""卡级事件日志（消息底座能力对齐 · 件 1，2026-10-13）。

**为什么有这个模块**：此前过程事件只在内存 journal 里活一轮（event_stream），
刷新即失、重启即失；会话文件（sessions/*.md）只存消息块，过程、思考、工具
事实一条都不落。本模块给每张卡一条 **append-only jsonl 事件流**，把消息、
过程、思考三路并到同一条时序线上，使「这一轮到底发生了什么」可回溯、可重放、
可软删。

落盘位置：``works/{work_id}/events/{card_id}.jsonl``（一行一条 JSON）。

记录形态（七键定死，缺键补空值、不增不减）：

    {"seq":42,"card_id":"card_001","run_id":"<pair_id>","round":3,
     "type":"tool_call","data":{...},"model":""}

- ``seq``：单卡文件内单调，**从文件尾 +1 续**——进程重启不归零、不重复
  （判据 2）；新文件从 1 起。
- ``run_id``＝现有 pair_id（一次请求一个）；``round``＝对话轮次（现有 turn，
  ≠ 工具循环 rounds）。
- ``model``：仅 reasoning 三席带（主模型思考不带＝空串），其余类型空串
  （裁定 C2）。席位归属信息本身在 ``data`` 里，字段不重复承载。
- ``data``：**结构化对象**，不是文本 blob（裁定 C5）。

铁则：
- **fail-open**：坏行/缺文件/解析失败只记 warning、返回空或跳过，绝不因
  事件日志拖垮主流程（与 event_stream「展示层零阻断」同族）。
- **只追加**：revoke 也是追加一条标记记录（软删），**不物理删任何字节**
  （card_store.py:4 铁律的精神延伸）。
- **路径动态取**：一切路径经 ``session_store.WORKS_DIR`` 现取（模块级常量
  快照会绕过 conftest 沙箱重定向——activity.py 的盲区教训）。
- 锁与原子写先例：同卡 ``asyncio.Lock``（session_store._session_lock 家族同款）
  ＋ 追加 write→flush→fsync（session_facade.append 追加路径同款）。
"""
from __future__ import annotations

import asyncio
import json

#: json.loads 的**模块内别名**：测试可数「解析了多少行」，
#: 用来钉住「尾部窗口不许退回全量解析」这条性能契约（不碰全局 json）。
_loads = json.loads
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from . import session_store

logger = logging.getLogger("storage.event_log")

# 七键定死——落盘记录的键集恒定（新增语义走 data 内部，不动骨架）
RECORD_KEYS = ("seq", "card_id", "run_id", "round", "type", "data", "model")

# 控制类记录：不进 timeline 渲染面（不是给用户看的内容，是时序控制位）
CONTROL_TYPES = frozenset({"revoke"})

_TAIL_WINDOW = 8192          # 尾部反扫初始窗口
_TAIL_WINDOW_MAX = 1 << 20   # 上限 1MB（单卡事件文件的体量上限内）
_TAIL_TRY_LINES = 5          # 尾部最多试解析的行数（坏行容错）


# ---------------------------------------------------------------- 路径
def events_dir(work_id: str) -> Path:
    """卡事件目录（动态取 WORKS_DIR —— 沙箱 monkeypatch 必须对本模块生效）。"""
    return session_store.WORKS_DIR / work_id / "events"


def event_path(work_id: str, card_id: str) -> Path:
    return events_dir(work_id) / f"{card_id}.jsonl"


# ---------------------------------------------------------------- 锁
# 异步锁（同卡串行，session_store._session_lock 同款键控 asyncio.Lock）
_async_locks: dict[tuple[str, str], asyncio.Lock] = {}
# 字节写互斥：同步兜底路径（无事件循环）与异步路径共用一把 threading 锁——
# 两条路都可能写同一个文件，seq 取号与追加必须在同一临界区内完成。
_write_lock = threading.RLock()


# seq 进程内游标：(work, card) → 已用掉的最大 seq。首次写卡时从文件尾播种一次
# （跨重启不归零、不重复＝判据 2），其后单进程内自增——每次写都反扫文件尾实测
# 8.2ms/条，高频思考增量会被它拖垮（A44 单进程前提成立，游标与文件一致）。
# 文件被外部截断/删除时游标会偏大（seq 跳号不重复），语义仍安全。
_seq_cursor: dict[tuple[str, str], int] = {}


def _async_lock(work_id: str, card_id: str) -> asyncio.Lock:
    key = (work_id, card_id)
    lock = _async_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _async_locks[key] = lock
    return lock


# ---------------------------------------------------------------- 尾部 seq
def _tail_seq(path: Path) -> int:
    """读文件尾最后一条可解析记录的 seq（fail-open：读不出＝0）。

    反向窗口扫描＋最多试 ``_TAIL_TRY_LINES`` 行——坏行（撕裂写/人工编辑）不
    致整卡 seq 归零重复；全窗口试不出才退回 0 并留 warning。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size <= 0:
        return 0
    window = min(_TAIL_WINDOW, size)
    with open(path, "rb") as f:
        while True:
            f.seek(size - window)
            chunk = f.read(window).decode("utf-8", errors="replace")
            if window < size:
                nl = chunk.find("\n")
                chunk = chunk[nl + 1:] if nl >= 0 else ""
            lines = [ln for ln in chunk.split("\n") if ln.strip()]
            tried = 0
            for ln in reversed(lines):
                tried += 1
                if tried > _TAIL_TRY_LINES:
                    break
                try:
                    rec = _loads(ln)
                except (ValueError, TypeError):
                    continue
                if isinstance(rec, dict):
                    s = rec.get("seq")
                    if isinstance(s, bool):
                        continue
                    if isinstance(s, int):
                        return s
                    if isinstance(s, str) and s.isdigit():
                        return int(s)
            if window >= size or window >= _TAIL_WINDOW_MAX:
                break
            window = min(window * 4, size, _TAIL_WINDOW_MAX)
    logger.warning("[event_log] 尾部 seq 不可解析（按 0 续，可能重复）file=%s", path.name)
    return 0


def last_seq(work_id: str, card_id: str) -> int:
    """该卡当前最大 seq（文件缺失＝0）。恢复通道跳过重放用。"""
    return _tail_seq(event_path(work_id, card_id))


# ---------------------------------------------------------------- 记录归一化
def _normalize(work_id: str, card_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """补齐骨架字段，data 非 dict 时原样装箱（绝不静默丢载荷）。

    [对话界面商业化 2026-10-17] 新增 ``ts``（ISO 8601 字符串）：
    时间轴是对话界面的基本事实——没有它就做不了日期分隔、hover 时间、
    消息排序的二次校验。此前只有 SSE 帧带 ``time.time()``（float，且只活在
    内存），落盘记录零时间，刷新后时间信息整体丢失。

    兼容口径：旧记录没有该键 ⇒ 读侧照原样返回（不补、不猜、不回填假值）；
    新记录写入时由本函数补齐。前端按「有则显示、无则留白」渐进增强。
    """
    data = record.get("data")
    if data is None:
        data = {}
    ts = record.get("ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        # [2026-11-07] reasoning 合并落盘把 time.time() float 原样 str() 进 ts，
        # 与 ISO 串混写 ⇒ 前端按 ts 排/显全乱。数字一律转 ISO。
        ts = datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="milliseconds")
    if not ts:
        ts = datetime.now().astimezone().isoformat(timespec="milliseconds")
    out = {
        "seq": record.get("seq"),
        "card_id": record.get("card_id") or card_id,
        "run_id": record.get("run_id") or "",
        "round": record.get("round") if isinstance(record.get("round"), int) else 0,
        "type": record.get("type") or "",
        "data": data,
        "model": record.get("model") or "",
        "ts": str(ts),
    }
    if not isinstance(out["data"], dict):
        out["data"] = {"value": out["data"]}
    return out


def _serialize(rec: dict[str, Any]) -> str:
    try:
        return json.dumps(rec, ensure_ascii=False)
    except (TypeError, ValueError):
        # data 内含不可序列化对象：降级为占位，行本身仍落盘（丢内容不丢时序位）
        fallback = {**rec, "data": {"_note": "unserializable"}}
        return json.dumps(fallback, ensure_ascii=False)


def _append_locked(work_id: str, card_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """真正的一次追加（调用方已持锁）：取号 → 写行 → fsync。"""
    path = event_path(work_id, card_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("[event_log] 事件目录创建失败 work=%s card=%s: %s", work_id, card_id, exc)
        return {}
    rec = _normalize(work_id, card_id, record)
    if not isinstance(rec["seq"], int):
        key = (work_id, card_id)
        cur = _seq_cursor.get(key)
        if cur is None:
            cur = _tail_seq(path)            # 首次播种：跨重启从文件尾续
        rec["seq"] = cur + 1
        _seq_cursor[key] = rec["seq"]
    try:
        # flush 到 OS、不做 fsync：事件流是**高频观测通道**（思考增量逐帧落盘），
        # 实测 fsync 约 10ms/条（320 帧 3.7s），会把主链路拖慢；进程内崩溃零丢失
        # （已 flush 进 OS 缓冲），仅机器级断电可能丢最后一行——坏行由 read
        # 侧跳过（fail-open）。真相源仍是会话文件与卡面（那里照旧 fsync）。
        with open(path, "a", encoding="utf-8") as f:
            f.write(_serialize(rec) + "\n")
            f.flush()
    except OSError as exc:
        logger.warning("[event_log] 追加失败（不阻断）work=%s card=%s: %s", work_id, card_id, exc)
        return {}
    # [基础设施 2026-10-26] 汇总面：**唯一真正的写点**收进 event_hub 缓冲
    # （同步入缓冲、微秒级；落表由 append 的异步链批量驱动）。放在这里而不是
    # event_stream.emit 里，是因为还有两条绕过 emit 的直写路径
    # （goal/driver 的 goal_round、session_facade 的消息块事件）也必须被汇总。
    try:
        from ..infra import event_hub
        event_hub.record(work_id, card_id, rec)
    except Exception as exc:  # noqa: BLE001 汇总失败不影响事件落盘
        logger.warning("[event_log] 汇总记录失败（不阻断）card=%s: %s", card_id, exc)
    return rec


def _notify_harness(work_id: str, card_id: str, rec: dict[str, Any]) -> None:
    """事件落盘后交给条件集（异步路径与同步路径共用）。

    [2026-10-28 · 换成条件集] ``policy_runner.feed``：写数据集 → 喂值班员 →
    谁凑够了就投递。
    [2026-10-31 · 两代并轨] 第一代「规则表 + ``harness.fire``」退役，
    ``ECO_HARNESS_LEGACY`` 回退开关一并拆除——条件集是**唯一**接线。

    零阻断：接线失败绝不影响事件本身。
    """
    try:
        from ..infra import policy_runner
        policy_runner.feed(work_id, rec)
    except Exception as exc:  # noqa: BLE001 接线失败不影响事件
        logger.warning("[event_log] 条件集接线失败（不阻断）: %s", exc)


# ---------------------------------------------------------------- 写
async def append(work_id: str, card_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """追加一条记录，返回落盘后的完整记录（seq 已定）。

    同卡 asyncio.Lock 内完成「读文件尾 seq+1 → 追加一行」——两步不可分，
    分开就会在同卡并发下取到同一个 seq。
    """
    try:
        async with _async_lock(work_id, card_id):
            with _write_lock:
                rec = _append_locked(work_id, card_id, record)
        # [基础设施 2026-10-26] 批量落汇总表（阈值由 event_hub 控制）
        if rec:
            try:
                from ..infra import event_hub
                await event_hub.maybe_flush(work_id)
            except Exception as exc:  # noqa: BLE001 汇总不阻断事件落盘
                logger.warning("[event_log] 汇总落表失败（不阻断）: %s", exc)
            # [系统消息 2026-10-26 · 用户令「现在先做第一个事件」] 事件 → 规则 → 提醒。
            # **后台**评估：规则命中要读卡、读事件、发消息，比落盘慢得多，
            # 直接 await 会把高频事件通道拖垮。
            _notify_harness(work_id, card_id, rec)
        return rec
    except Exception as exc:  # noqa: BLE001 事件日志零阻断
        logger.warning("[event_log] append 失败（不阻断）card=%s: %s", card_id, exc)
        return {}


def append_sync(work_id: str, card_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """同步追加（无事件循环的调用点使用；与异步路径共用字节写互斥）。

    [系统消息 2026-10-26] **也接线**：``goal/change`` 走的就是这条路径
    （``goal/service._commit`` 用 append_sync 写盘）。落盘后照常喂条件集；
    投递是否起得来看 ``policy_runner``（无事件循环时只记账不投递，不阻断写盘）。
    """
    try:
        with _write_lock:
            rec = _append_locked(work_id, card_id, record)
        if rec:
            _notify_harness(work_id, card_id, rec)
        return rec
    except Exception as exc:  # noqa: BLE001 事件日志零阻断
        logger.warning("[event_log] append_sync 失败（不阻断）card=%s: %s", card_id, exc)
        return {}


# 同步调用点（event_stream.emit 是同步函数）排下的在途任务：
# ① 保持强引用防 GC 中途回收；② 供 drain() 等待落盘（测试/关停确定性）。
_pending: set[asyncio.Task] = set()


def append_bg(work_id: str, card_id: str, record: dict[str, Any]) -> None:
    """同步调用点的落盘入口：有事件循环→排任务；无→当前线程同步写。

    event_stream.emit/emit_token 是同步函数（展示层零阻塞契约），而本模块
    的写是协程（asyncio.Lock）。两条路都写同一个文件、seq 由文件尾现算，
    故无论走哪条路时序与取号都一致。
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is None:
        append_sync(work_id, card_id, record)
        return

    def _mk() -> None:
        async def _run() -> None:
            await append(work_id, card_id, record)
        task = asyncio.get_running_loop().create_task(_run())
        _pending.add(task)
        task.add_done_callback(_pending.discard)

    # [双循环 2026-11-04] emit 是同步函数，可能被服务循环 B 上的白名单
    # 读/SSE 路径调到——直接把 append 协程生在 B 上会去抢 A 绑定的
    # asyncio 写锁（"bound to a different event loop"⇒ 事件静默丢）。
    # ⇒ 落盘任务固定生到主循环 A（无注册主循环＝单循环场景，原地跑）。
    try:
        from ..infra import loops as _loops
        if _loops.main_loop() is not None and running is not _loops.main_loop():
            _loops.run_sync_on_main(_mk)
            return
    except Exception:  # noqa: BLE001 桥失败不阻断——退回调用方 loop
        pass
    _mk()


async def drain() -> int:
    """等待全部在途落盘任务完成，返回等待的任务数（测试确定性用）。"""
    if not _pending:
        return 0
    tasks = list(_pending)
    for t in tasks:
        try:
            await t
        except Exception as exc:  # noqa: BLE001 落盘任务异常不外溢
            logger.warning("[event_log] 在途落盘任务异常: %s", exc)
    return len(tasks)


# ---------------------------------------------------------------- 读
def _iter_lines(path: Path) -> Iterable[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    yield line
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("[event_log] 事件文件读取失败 file=%s: %s", path.name, exc)
        return


def read(work_id: str, card_id: str, from_seq: int = 0) -> list[dict[str, Any]]:
    """读 ``seq >= from_seq`` 的记录（含 revoke 控制记录；文件序＝seq 升序）。"""
    path = event_path(work_id, card_id)
    out: list[dict[str, Any]] = []
    for line in _iter_lines(path):
        try:
            rec = _loads(line)
        except (ValueError, TypeError):
            logger.warning("[event_log] 坏行跳过 file=%s", path.name)
            continue
        if not isinstance(rec, dict):
            continue
        s = rec.get("seq")
        s = int(s) if isinstance(s, int) and not isinstance(s, bool) else (
            int(s) if isinstance(s, str) and s.isdigit() else None)
        if s is None or s < from_seq:
            continue
        rec["seq"] = s
        out.append(rec)
    out.sort(key=lambda r: r["seq"])
    return out


def _tail_records(path: Path, spans: list[tuple[int, int]],
                  want: int, min_seq: int = 0) -> list[dict[str, Any]]:
    """从**文件尾**往前读，收集最后 ``want`` 条**未被撤销**的记录。

    〔5715 · 治「切卡卡死」〕为什么不能「全量读再切」：长会话的事件文件能到
    **十几 MB / 数万行**，全量 ``json.loads`` 每行一遍要**秒级**（实测 47492 行
    ⇒ 0.25~1.6s）——而打开卡片只要**尾部几百条**。

    为什么不能「多取 N 行再截」：事件文件里**同一条 tool_call 会有多条
    tool_result**（seq 递增但条数 ≫ 调用数）⇒「行数」与「记录数」不成比例，
    多取固定条数不能保证「窗口内记录数 ≥ want」⇒ 会与原实现差几条。

    本实现：**整文件按字节读一次**（十几 MB 约 20ms——慢的是 json.loads，不是
    IO）⇒ 切行 ⇒ **只解析尾部需要的那些** ⇒ 收够 want 条就停。
    撤销语义与改前**逐字同构**：revoke{from_seq:N} 撤掉「N ≤ seq < revoke 自身
    seq」的记录（升序处理时区间天然重叠 ⇒ 等价于区间并集）⇒ 这里用区间并集判定。

    ``min_seq``（2026-10-30 增）：文件序＝seq 升序，从尾往前读时 seq 单调递减，
    **一旦 ``seq < min_seq`` 就可以直接停**——这是「增量拉取」路径
    （`timeline(from_seq=N)`）的解药：原实现走全量 `read()`，把整个文件逐行
    `json.loads`（card_061 实测 3831 行 / 53.5MB、单行最大 4.6MB）⇒ 内存爆
    `MemoryError`，页面打不开。加了停点后只解析 from_seq 之后那几条。
    """
    if want <= 0:
        return []
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if not data:
        return []
    out: list[dict[str, Any]] = []
    for ln in reversed(data.split(b"\n")):
        if not ln.strip():
            continue
        try:
            rec = _loads(ln)
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        s = rec.get("seq")
        s = int(s) if isinstance(s, int) and not isinstance(s, bool) else (
            int(s) if isinstance(s, str) and s.isdigit() else None)
        if s is None:
            continue
        rec["seq"] = s
        if s < min_seq:
            break                       # 再往前 seq 只会更小 ⇒ 后面的都不用要了
        if str(rec.get("type") or "") in CONTROL_TYPES:
            continue                    # 控制记录不进渲染面（与改前一致）
        if _in_revoked(s, spans):
            continue                    # 被 revoke 覆盖的段（区间并集）
        out.append(rec)
        if len(out) >= want:
            break
    out.reverse()                        # 收集是「新→旧」⇒ 反转成 seq 升序
    out.sort(key=lambda r: int(r.get("seq") or 0))
    return out


#: 判「这条是不是 revoke 控制记录」的**行首**正则（type 字段一般就在行首一百多字节）。
#: 〔P24 · 2026-10-26〕先在行首判定 type，**不是 revoke 就不必整行 json.loads**——
#: 事件文件里有**巨型内容行**（实测 card_021：8277 行 / 109.5MB ⇒ 平均 13KB/行），
#: 整行解析一行要 ~285ms ⇒ 光是 3 行「内容里恰好出现 revoke 字样」的巨型行
#: 就要 855ms（这才是旧实现那 1.1 秒的真正来源，不是扫描本身）。
_REVOKE_TYPE_RE = re.compile(rb'"type"\s*:\s*"([A-Za-z_]+)"')

#: 行首多少字节内判 type（超出就保守整行解析——正确性优先）。
_REVOKE_HEAD_BYTES = 2048

#: (mtime_ns, size) → spans 的缓存（撤区间只随文件变化而变）。
#: 〔P24〕没有它：**同一次切卡**会多次调 timeline（打开 + 结算回流 + 增量对账）
#: ⇒ 每次都重扫一遍 109MB ⇒ 「切卡卡死」的成本被乘以次数。
#: 〔P25 补〕**必须有上限**（内存封顶原则：只按容量淘汰，**不按时间**）。
#: 每条 = 一个卡的事件文件的撤区间列表 ⇒ 条数 ≈ 卡数；不封顶的话卡越多越涨。
_REVOKE_CACHE: dict[str, tuple[int, int, list[tuple[int, int]]]] = {}
#: 撤区间缓存的条目上限（超出按插入顺序丢最旧）。
_REVOKE_CACHE_MAX = 512


def _revoke_spans(path: Path) -> list[tuple[int, int]]:
    """扫出所有 revoke 的作废区间 ``(from_seq, revoke_seq)``（**字节级**扫描）。

    〔5715 · 治「切卡卡死」〕原来的 :func:`timeline` 为了让 revoke 生效，
    对**全文件**逐行 ``json.loads``（47492 行 ⇒ 秒级）——而这个函数是
    **同步**调用、跑在 async 端点里 ⇒ **堵死事件循环** ⇒ 期间所有请求排队
    ⇒ 页面「没反应」。

    这里只做**子串命中**（``"revoke"``）才 json.loads 那一行。

    〔P24 · 2026-10-26〕三重提速（实测 card_021：109.5MB / 8277 行 / 平均 13KB 每行）：
      ① 定位命中处用 ``bytes.find``（C 层）而不是 Python 逐行迭代；
      ② **先在行首 2KB 内判 type**——不是 revoke 就不必**整行** json.loads
         （巨型内容行整行解析要 ~285ms；旧实现那 1.1 秒里 855ms 就在这）；
      ③ 按 (mtime_ns, size) **缓存**撤区间（同一次切卡会多次调 timeline）。
    实测：旧 930ms → 新 **106ms**（语义等价：只有行首明确写着别的 type 才跳过整行解析）。
    """
    try:
        st = path.stat()
    except OSError:
        return []
    key = str(path)
    cached = _REVOKE_CACHE.get(key)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]

    spans: list[tuple[int, int]] = []
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return spans
    except OSError as exc:
        logger.warning("[event_log] 事件文件读取失败 file=%s: %s", path.name, exc)
        return spans
    if b"revoke" in raw:
        pos = 0
        while True:
            i = raw.find(b"revoke", pos)
            if i < 0:
                break
            pos = i + 6
            # 回溯到行首、顺延到行尾（只把这**一行**送去判）
            s = raw.rfind(b"\n", 0, i) + 1
            e = raw.find(b"\n", i)
            if e < 0:
                e = len(raw)
            # 〔P24〕行首已明确写着别的 type ⇒ 铁定不是控制记录 ⇒ 不必整行解析
            head = raw[s:min(e, s + _REVOKE_HEAD_BYTES)]
            mt = _REVOKE_TYPE_RE.search(head)
            if mt is not None and mt.group(1) != b"revoke":
                continue
            try:
                rec = _loads(raw[s:e].decode("utf-8", errors="replace"))
            except (ValueError, TypeError):
                continue
            if not isinstance(rec, dict) or str(rec.get("type") or "") not in CONTROL_TYPES:
                continue
            data = rec.get("data") or {}
            try:
                fseq = int(data.get("from_seq"))
            except (TypeError, ValueError):
                continue
            s2 = rec.get("seq")
            try:
                rseq = int(s2)
            except (TypeError, ValueError):
                rseq = fseq
            spans.append((fseq, rseq))
    try:
        # 〔P25 补〕内存封顶：超上限就丢最旧的（**不按时间**淘汰——用户明令不许按时间释放）。
        if len(_REVOKE_CACHE) >= _REVOKE_CACHE_MAX and key not in _REVOKE_CACHE:
            for old in list(_REVOKE_CACHE.keys())[:len(_REVOKE_CACHE) - _REVOKE_CACHE_MAX + 1]:
                _REVOKE_CACHE.pop(old, None)
        _REVOKE_CACHE[key] = (st.st_mtime_ns, st.st_size, spans)
    except Exception:  # noqa: BLE001 缓存失败不影响主流程
        pass
    return spans


def _in_revoked(seq: int, spans: list[tuple[int, int]]) -> bool:
    """这条记录是否落在某个 revoke 的作废区间内。"""
    for f, r in spans:
        if f <= seq < r:
            return True
    return False


def timeline(work_id: str, card_id: str, from_seq: int = 0,
             tail: int = 0, before: int = 0) -> list[dict[str, Any]]:
    """合并时序表：按 seq 升序，**跳过被 revoke 覆盖的段**（软删语义）。

    - revoke 记录自身不进渲染面（它是控制位，不是给用户看的内容）；
    - 遇到 revoke{from_seq:N} → 已收进结果的 seq>=N 全部撤下（含被编辑掉的
      旧提问与其后一切），其后的新 run 正常进入；
    - 存量文件（无 revoke）＝原样全量，零行为变化。

    [对话流分页 2026-10-20] ``tail`` / ``before`` 是**窗口参数**：
      · ``tail=N``   —— 只回最后 N 条（打开卡片时用，长会话从上千条降到几百条）；
      · ``before=S`` —— 只回 seq < S 的部分（往上翻历史时用）。
    为什么放在**过滤之后**截：revoke 必须按全量时序生效，先截会把已作废的段复活。

    〔5715 · 治「切卡卡死」〕**tail 路径不再全量解析**：
      · revoke 区间改为**字节级**扫描（子串命中才解析）⇒ 十几 MB 的文件几十毫秒；
      · 记录只从**文件尾**读「尾部 N 条 ＋ 被撤条数余量」⇒ 与原语义等价
        （原实现是「全量收 → 撤 → 截尾」；这里先多取被撤的量，撤完仍够 N 条）。
      · ``before`` / ``from_seq>0`` 仍走全量（翻历史/增量是少数路径），
        但调用方已把它挪进线程（见 routes.py）⇒ 不再堵事件循环。
    """
    path = event_path(work_id, card_id)
    spans = _revoke_spans(path)
    items: list[dict[str, Any]] = []

    want_tail = 0
    try:
        want_tail = int(tail)
    except (TypeError, ValueError):
        want_tail = 0

    # [2026-11-07 · 写乱序] 显示序＝**时间序 (ts, seq)**，不再是 seq 升序：
    # 落盘是异步排任务（append_bg → create_task → 写完才取号），加上
    # append_sync 插队与多进程同写一张卡 ⇒ 慢写的事件拿到更大 seq，seq 与
    # 真实时刻脱钩（实测：用户消息排在它之前就开始跑的工具串上面）。
    # ts＝事件产生时刻（落盘已带 ISO），seq 做同刻 tiebreak；无 ts 的记录全在
    # ts 时代（2026-10-17）之前，`""` 天然排在最前——与真实老→新一致。
    # 数字串 ts 是 reasoning 合并落盘把 epoch float 写进 ts 的老账——归一成
    # ISO 再比，否则 "1..." < "2..." 会让它们整批跳到最前面。
    def _chrono_key(r: dict[str, Any]) -> tuple[str, int]:
        ts = str(r.get("ts") or "")
        if ts and ts[0].isdigit() and "T" not in ts:
            try:
                ts = datetime.fromtimestamp(float(ts)).astimezone().isoformat()
            except (ValueError, OSError, OverflowError):
                pass
        return (ts, int(r.get("seq") or 0))

    if want_tail > 0 and not before and from_seq <= 0:
        # ── 尾部窗口路径：从文件尾往前读，够 N 条就停（不再全量解析）──────
        items = _tail_records(path, spans, want_tail)
        items.sort(key=_chrono_key)
        return items

    if from_seq > 0 and not before:
        # ── 增量拉取路径（2026-10-30 增）：从文件尾往前读到 from_seq 就停 ──
        # 原实现落到下面的全量 `read()`：把整个事件文件逐行 json.loads。
        # card_061 实测 3831 行 / 53.5MB、单行最大 4.6MB（tool_result 回灌整张卡）
        # ⇒ `line.strip()` 抛 MemoryError，页面直接打不开。
        # 文件序＝seq 升序，从尾往前读时 seq 单调递减，遇到 seq < from_seq 即可停，
        # 只解析真正需要的那几条。revoke 语义由 `_in_revoked`（区间并集）照旧处理。
        items = _tail_records(path, spans, want=1_000_000, min_seq=from_seq)
        items.sort(key=_chrono_key)
        return items

    # ── 全量路径（翻历史）：与改前逐字一致 ───────────────────────
    for rec in read(work_id, card_id):
        if str(rec.get("type") or "") in CONTROL_TYPES:
            data = rec.get("data") or {}
            f = data.get("from_seq")
            try:
                f = int(f)
            except (TypeError, ValueError):
                continue
            items = [x for x in items if int(x.get("seq") or 0) < f]
            continue
        if int(rec.get("seq") or 0) >= from_seq:
            items.append(rec)
    items.sort(key=_chrono_key)
    if before:
        try:
            b = int(before)
        except (TypeError, ValueError):
            b = 0
        if b > 0:
            # before 指「当前显示段首条的 seq」——按**序位**截（写乱序时段里
            # seq 不再等于时序，比大小会把早写晚落的记录错排进旧页）。
            idx = next((i for i, x in enumerate(items)
                        if int(x.get("seq") or 0) == b), None)
            items = (items[:idx] if idx is not None
                     else [x for x in items if int(x.get("seq") or 0) < b])
    if want_tail > 0:
        items = items[-want_tail:]
    return items


# ---------------------------------------------------------------- 软删
async def revoke(work_id: str, card_id: str, from_seq: int) -> dict[str, Any]:
    """标记 ``from_seq`` 起全部记录作废（追加一条 revoke 记录，不物理删）。"""
    return await append(work_id, card_id, {
        "type": "revoke", "data": {"from_seq": int(from_seq)},
    })


def revoke_sync(work_id: str, card_id: str, from_seq: int) -> dict[str, Any]:
    return append_sync(work_id, card_id, {
        "type": "revoke", "data": {"from_seq": int(from_seq)},
    })
