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
import mmap
import sys

#: json.loads 的**模块内别名**：测试可数「解析了多少行」，
#: 用来钉住「尾部窗口不许退回全量解析」这条性能契约（不碰全局 json）。
_loads = json.loads
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from . import session_store

logger = logging.getLogger("storage.event_log")

# 〔2026-11-19 返工2〕文件身份校验只在 Windows 生效（st_ctime=创建时间、
# st_ino=文件索引号——追加不变、替换必变、os.utime 伪装不了）。POSIX 上
# st_ctime＝inode change time（追加也变）语义不同，回退指纹行为。
_WIN_IDENTITY = sys.platform == "win32" 

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
def _message_event(path: Path, kind: str, msg_id: str) -> dict[str, Any] | None:
    """持写锁时按消息号找落盘事件。读失败交回调用方重试。"""
    needle = msg_id.encode("utf-8")
    try:
        with path.open("rb") as stream:
            for line in stream:
                if needle not in line:
                    continue
                try:
                    old = _loads(line)
                except (ValueError, TypeError):
                    continue
                if (isinstance(old, dict) and old.get("type") == kind
                        and isinstance(old.get("data"), dict)
                        and old["data"].get("msg_id") == msg_id
                        and isinstance(old.get("seq"), int)):
                    return old
    except FileNotFoundError:
        pass
    return None


async def append(work_id: str, card_id: str, record: dict[str, Any], *,
                 once_per_message: bool = False) -> dict[str, Any]:
    """追加一条记录，返回落盘后的完整记录（seq 已定）。

    同卡 asyncio.Lock 内完成「读文件尾 seq+1 → 追加一行」——两步不可分，
    分开就会在同卡并发下取到同一个 seq。
    """
    try:
        async with _async_lock(work_id, card_id):
            with _write_lock:
                if once_per_message:
                    old = _message_event(event_path(work_id, card_id), record["type"],
                                         record["data"]["msg_id"])
                    if old is not None:
                        return old
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
def _mmap_ro(path: Path) -> mmap.mmap | None:
    """事件文件的只读 mmap 视图（找不到/打不开/空文件 → ``None``）。

    〔2026-11-15 · card_007 事件文件 152MB〕为什么不再 ``read_bytes()``：
    整文件一份 bytes 是一次 **152MB 的连续分配**，再 ``split(b"\\n")`` 又翻
    2–4 倍 ⇒ 页面请求直接 ``MemoryError``。mmap 只占虚拟地址，find/rfind/
    切片与 bytes 同接口、切片才产生真实拷贝；文件关掉后视图仍有效
    （fd 已被 mmap 内部 dup/映射持有）。**调用方负责 ``close()``**。"""
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("[event_log] 事件文件读取失败 file=%s: %s", path.name, exc)
        return None
    try:
        return mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    except (ValueError, OSError):
        return None
    finally:
        f.close()


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

    本实现：mmap 只读视图 ⇒ 从尾 ``rfind(b"\\n")`` **倒逐行** ⇒ **只解析
    尾部需要的那些** ⇒ 收够 want 条就停。为什么不再「整文件读一次再切行」：
    切行本身才是内存炸弹——``152MB × split`` 产出数万 bytes 对象 ≈ 2–4 倍
    原文（card_007 2026-11-12 实测 ``MemoryError``）；mmap 切片只拷**单行**，
    峰值为「一条最大行」级（card_061 单行曾 4.6MB）。
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
    raw = _mmap_ro(path)
    if raw is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        end = len(raw)
        while end > 0:
            nl = raw.rfind(b"\n", 0, end)
            ls = nl + 1                    # 无 \n ⇒ ls=0＝文件首行
            ln = raw[ls:end]
            end = nl                       # 下一轮行尾＝这枚 \n 的位置
            if ln.strip():
                try:
                    rec = _loads(ln)
                except (ValueError, TypeError):
                    rec = None
                if isinstance(rec, dict):
                    s = rec.get("seq")
                    s = int(s) if isinstance(s, int) and not isinstance(s, bool) else (
                        int(s) if isinstance(s, str) and s.isdigit() else None)
                    if s is not None:
                        rec["seq"] = s
                        if s < min_seq:
                            break           # 再往前 seq 只会更小 ⇒ 后面的都不用要了
                        if str(rec.get("type") or "") in CONTROL_TYPES:
                            pass            # 控制记录不进渲染面（与改前一致）
                        elif not _in_revoked(s, spans):
                            out.append(rec) # 被 revoke 覆盖的段（区间并集）跳过
                            if len(out) >= want:
                                break
            if ls == 0:
                break                       # 已处理到文件首行
    finally:
        raw.close()
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

# ═══════════════════════════════════════════════════════════════════════════
# [增量撤销/索引扫描 2026-11-19 · 性能批次二]
#
# 病：撤销区间缓存按 (mtime_ns, size) 整体键控——事件文件**每追加一行**键就变，
# 下一次 timeline 全文件重扫（card_007 实测 747MB ⇒ 每次追加重付秒级）。
# ``before`` 翻页更是一直走全量 ``read()``（整文件逐行 json.loads，109MB 实测
# ~360ms、内存翻 2-4 倍）。
#
# 药：**读侧可重建的增量扫描器**——
#   · 状态按路径记：已扫到的**完整记录边界** ``size_scanned``、撤销区间
#     ``spans``、可选的行位置索引 ``index``（seq/ts/offset，供翻页免全量）；
#   · 文件**只增**（追加）⇒ 只扫 ``[size_scanned, 最后一个 \n 边界)`` 的新增
#     完整行——「并发追加时只发布对应完整记录边界的结果，未写完整的行由
#     下次补读」；前缀一致性用头 64 字节指纹确认；
#   · 文件**被替换/截断/前缀无法确认/同长改写**（mtime 变而长度未增）⇒ 从
#     现有原文**安全重建**；
#   · 条目上限沿用 ``_REVOKE_CACHE_MAX``（按路径条数淘汰最旧，不按时间）；
#     单路径索引行数超 ``_INDEX_MAX_ENTRIES`` 时弃用索引、翻页回退全量路径。
# 巨型行零整行拷贝：行定位与 "revoke" 子串探测全走 ``mmap.find``（C 层），
# seq/ts 抽取只切行首/行尾小片；抽取失败才整行解析兜底（正确性优先）。
# ═══════════════════════════════════════════════════════════════════════════

#: (mtime_ns, size) → spans 的缓存（撤区间只随文件变化而变）。
#: 〔P24〕没有它：**同一次切卡**会多次调 timeline（打开 + 结算回流 + 增量对账）
#: ⇒ 每次都重扫一遍 109MB ⇒ 「切卡卡死」的成本被乘以次数。
#: 〔P25 补〕**必须有上限**（内存封顶原则：只按容量淘汰，**不按时间**）。
#: 每条 = 一个卡的事件文件的撤区间列表 ⇒ 条数 ≈ 卡数；不封顶的话卡越多越涨。
_SCAN_STATE: dict[str, dict[str, Any]] = {}
#: 撤区间缓存的条目上限（超出按插入顺序丢最旧）。
_REVOKE_CACHE_MAX = 512
#: 单路径索引的行数上限（超过就弃用索引，翻页回退全量路径）。
#: 〔2026-11-19 返工·反例③〕原来只有条目闸（20 万条最坏 ~30-46MB/路径，
#: 512 路径最坏 20GB+）。改**条目+字节双闸**：每条估算开销＝ts 字节 + 120B
#: 固定（tuple + int + bool），任一闸到顶即弃用索引（spans 照常扫完，
#: 翻页回退全量路径——正确性不受影响，只是回退到旧性能）。
_INDEX_MAX_ENTRIES = 100_000
_INDEX_MAX_BYTES = 24 * 1024 * 1024
#: 「小行」阈值：以内整行 json.loads 抽 seq/ts，超过走行首/行尾正则。
_INDEX_PARSE_MAX = 64 * 1024

#: 索引行条目：(归一化 ts, seq, 行起偏移, 行止偏移, 是否控制记录)。
#: ts 归一化与 ``_chrono_key`` 同一函数 ⇒ 排序语义逐字一致。
#: 〔2026-11-19 返工·反例③〕扫描锁改**恒定容量分桶**：原来按路径一把锁，
#: 锁 dict 随卡数无限涨（512+ 卡的服务里就是 512+ 把常驻锁）。现在 256 把
#: 锁按路径 md5 哈希映射——锁数恒定；不同路径偶共享同一把锁只是多等一下，
#: 互斥语义（同路径扫描串行、并发追加只发布完整边界结果）不破。
_SCAN_LOCKS: list[threading.Lock] = [threading.Lock() for _ in range(256)]
_SCAN_STATS: dict[str, int] = {"full_rebuilds": 0, "delta_scans": 0,
                               "cache_hits": 0, "bytes_scanned": 0,
                               "bytes_skipped": 0, "index_lines": 0}
_PREFIX_FP_BYTES = 64


def scan_stats() -> dict[str, int]:
    """扫描器累计读数（观测：触及/跳过的历史字节量、重建与增量次数）。"""
    return dict(_SCAN_STATS)


def _norm_ts(ts: Any) -> str:
    """ts 归一化（与 ``_chrono_key`` 同口径）：数字/数字串 epoch → ISO；其余 str 化。"""
    ts = str(ts or "")
    if ts and ts[0].isdigit() and "T" not in ts:
        try:
            ts = datetime.fromtimestamp(float(ts)).astimezone().isoformat()
        except (ValueError, OSError, OverflowError):
            pass
    return ts


def _seq_of(rec: Any) -> Optional[int]:
    s = rec.get("seq") if isinstance(rec, dict) else None
    return int(s) if isinstance(s, int) and not isinstance(s, bool) else (
        int(s) if isinstance(s, str) and s.isdigit() else None)


_SEQ_HEAD_RE = re.compile(rb'"seq"\s*:\s*(\d+)')
_TS_TAIL_RE = re.compile(rb'"ts"\s*:\s*("([^"]*)"|\d+(?:\.\d+)?)')


def _top_ts_of(raw: "mmap.mmap", pos: int, end: int) -> "bytes | None":
    """行 [pos,end) 的**顶层** ts 原始值片段（含引号/数字），取不到 ⇒ None。

    〔2026-11-19 返工·反例②〕骨架里 ts 是**最后一个键**（其后只有 ``}``），
    但 data 尾部内容里可能恰好出现 ``"ts": "…"` 字样——`search` 取第一个
    匹配会被 data 污染 ⇒ 顶层 ts 错 ⇒ (ts,seq) 排序错 ⇒ 翻页旧页漏/错记录。
    这里 ``rfind(b'"ts"')`` 从行尾取**最后一次**键名出现（顶层 ts 一定在
    data 之后），从那个位置起再匹配值——data 再怎么嵌都污染不到。
    """
    i = raw.rfind(b'"ts"', pos, end)
    if i < 0:
        return None
    mt = _TS_TAIL_RE.match(raw, i)
    if mt is None:
        # 键名后不是合法值形态（畸形行）⇒ 交整行解析兜底
        return None
    return mt.group(1)


def _index_key_of(raw: "mmap.mmap", pos: int, end: int) -> "tuple[str, int] | None":
    """行 [pos,end) 的 (归一化 ts, seq)。小行整行解析；巨型行只切头尾小片，
    抽取失败（畸形/极端结构）才整行解析兜底——正确性优先于速度。
    返回 None ⇒ 该行无可解析 seq（与 read() 一样不进时序面）。"""
    n = end - pos
    if n <= _INDEX_PARSE_MAX:
        try:
            rec = _loads(raw[pos:end].decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            return None
        seq = _seq_of(rec)
        if seq is None:
            return None
        return _norm_ts(rec.get("ts")), seq
    # 巨型行：seq 是骨架第一个键（行首）、ts 是最后一个键（行尾）。
    head = raw[pos:min(end, pos + 256)]
    m = _SEQ_HEAD_RE.search(head)
    if m is None:
        return None
    seq = int(m.group(1))
    val = _top_ts_of(raw, pos, end)
    if val is None:
        # 兜底：整行解析（巨型行一次 ~200ms，只在极端结构时发生）
        try:
            rec = _loads(raw[pos:end].decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            return None
        seq2 = _seq_of(rec)
        if seq2 is None:
            return None
        return _norm_ts(rec.get("ts")), seq2
    ts = val[1:-1].decode("utf-8", errors="replace") if val.startswith(b'"') \
        else val.decode("ascii", errors="replace")
    return _norm_ts(ts), seq


def _prefix_ok(path: Path, s: dict) -> bool:
    """头 ``fp_len`` 字节指纹一致 ⇒ 已扫前缀可信任（追加场景的廉价确认）。

    〔2026-11-19 返工〕补**尾指纹**：``size_scanned`` 边界往前 64B 也核对——
    「头尾都没动、中段被原地替换」的改写现在会被第二道指纹拦下（mtime
    仍是主防线；同长同 mtime 的中段改写从『不可检测』降为『需连边界 64B
    都恰好不动』）。核对读的是**旧文件已扫前缀的尾部**，与当前文件无关。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(s["fp_len"])
            ok = _md5(head) == s["fp"]
            if ok and s.get("tail_fp"):
                tail_off = max(0, s["size_scanned"] - 64)
                if s["size_scanned"] > 0:
                    f.seek(tail_off)
                    ok = _md5(f.read(min(64, s["size_scanned"]))) == s["tail_fp"]
            return ok
    except OSError:
        return False


def _md5(b: bytes) -> str:
    import hashlib
    return hashlib.md5(b).hexdigest()


def _scan_state(path: Path, want_index: bool) -> "tuple[list, list | None]":
    """撤销区间（+可选行索引）的增量扫描入口。返回 (spans, index|None)。

    · (mtime, size) 未变 ⇒ 直接命中缓存（O(stat)，零扫描）；
    · 只增（size 变大且前缀指纹一致）⇒ 只扫新增完整行；
    · 其余（替换/截断/同长改写/前缀不确认/索引缺口）⇒ 全量重建。
    """
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return [], None
    size, mtime = st.st_size, st.st_mtime_ns
    # 〔2026-11-19 返工2·文件身份〕替换检测的关键信号：**Windows 上
    # st_ino（文件索引号）与 st_ctime（创建时间）**。追加写两者不变；
    # 「用更大的文件替换旧事件文件」（os.replace/重写再改名）两者**必变**，
    # 且 os.utime 改不了创建时间——对抗性伪装不了。头/尾 64B 指纹可以被
    # 「保留端点、只改中段」的真实替换绕过（原样反例实测），身份信号抓得住。
    # POSIX 上 st_ctime＝inode change time（追加也会变）语义不同 ⇒ 仅
    # Windows 启用（生产平台 win32）；其他平台回退指纹行为。
    ino, ctime = st.st_ino, getattr(st, "st_ctime_ns", 0)
    import hashlib as _hl
    _bucket = int(_hl.md5(key.encode("utf-8")).hexdigest()[:8], 16) % len(_SCAN_LOCKS)
    with _SCAN_LOCKS[_bucket]:
        s = _SCAN_STATE.get(key)
        _identity_same = (not _WIN_IDENTITY) or (
            s is not None and s.get("ino") == ino and s.get("ctime") == ctime)
        if (s is not None and _identity_same
                and s["mtime_ns"] == mtime and s["size"] == size
                and (not want_index or s["has_index"] or s["index_dropped"])):
            _SCAN_STATS["cache_hits"] += 1
            return s["spans"], (s["index"] if want_index and s["has_index"]
                                else None)

        need_full = (s is None or size < s["size_scanned"]
                     or (mtime != s["mtime_ns"] and size <= s["size_scanned"])
                     or (not _identity_same)
                     or (not _prefix_ok(path, s))
                     or (want_index and not s["has_index"]
                         and not s["index_dropped"]))
        raw = _mmap_ro(path)
        if raw is None:
            return [], None
        t0 = time.perf_counter()
        spans: list[tuple[int, int]] = [] if need_full or s is None \
            else list(s["spans"])
        # 索引携带规则：增量路径把旧索引一并延伸（只解析新增行——否则
        # spans-only 的增量扫描会把索引打丢，每次翻页都退化成全量重建）；
        # 全量重建只在 want_index 时建索引（tail 契约：禁止 eager 全量解析）。
        prev_index: list | None = None
        if (s is not None and s["has_index"] and not s["index_dropped"]
                and not need_full):
            prev_index = s["index"]
        index: list | None
        # 字节账随索引携带（增量延伸接着累计；全量重建清零重算）
        _index_bytes = 0
        if need_full:
            index = [] if want_index else None
        elif s is not None and s["index_dropped"]:
            # 〔2026-11-19 返工3·反例⑥〕弃用期间**始终回退**：增量片段（只含
            # 新增行的残缺索引）不许当索引用——否则翻页锚在旧历史时 vis 无
            # 该锚 ⇒ 历史页返回**空页**（实测：5 条历史页返回空）。
            index = None
        elif prev_index is not None:
            index = list(prev_index)
            _index_bytes = sum(len(e[0]) + 120 for e in prev_index)
        elif want_index:
            index = []
        else:
            index = None
        # 〔返工2〕弃用状态**跨扫描保持**：delta 重扫时不许重置——否则
        # 「索引超限弃用后追加普通事件」的下一次翻页又会触发全量重建。
        index_dropped = bool(s["index_dropped"]) if (s is not None and not need_full) \
            else False
        try:
            start = 0 if need_full else min(s["size_scanned"], size)
            # 完整记录边界＝最后一个 \n 之后（半行留给下次补读）
            last_nl = raw.rfind(b"\n")
            boundary = (last_nl + 1) if last_nl >= 0 else 0
            if boundary <= start:
                # 没有新的完整行：只刷新水位（mtime/size），缓存语义不变
                if s is not None and not need_full:
                    s["mtime_ns"], s["size"] = mtime, size
                    _SCAN_STATE[key] = s
                    return spans, (s["index"] if want_index and s["has_index"]
                                   else None)
                return spans, (index if want_index and index else None)
            pos = start
            while pos < boundary:
                nl = raw.find(b"\n", pos, boundary)
                end = nl if nl >= 0 else boundary
                if end > pos:
                    # —— 撤销判定（与 P24 语义逐字一致：行首 2KB 判 type）——
                    if raw.find(b"revoke", pos, end) >= 0:
                        head = raw[pos:min(end, pos + _REVOKE_HEAD_BYTES)]
                        mt = _REVOKE_TYPE_RE.search(head)
                        if mt is None or mt.group(1) == b"revoke":
                            try:
                                rec = _loads(raw[pos:end].decode(
                                    "utf-8", errors="replace"))
                            except (ValueError, TypeError):
                                rec = None
                            if isinstance(rec, dict) and \
                                    str(rec.get("type") or "") in CONTROL_TYPES:
                                data = rec.get("data") or {}
                                try:
                                    fseq = int(data.get("from_seq"))
                                except (TypeError, ValueError):
                                    fseq = None
                                if fseq is not None:
                                    try:
                                        rseq = int(rec.get("seq"))
                                    except (TypeError, ValueError):
                                        rseq = fseq
                                    spans.append((fseq, rseq))
                    # —— 行索引（翻页免全量）——
                    if index is not None and (end - pos) > 0:
                        ik = _index_key_of(raw, pos, end)
                        if ik is not None:
                            head = raw[pos:min(end, pos + _REVOKE_HEAD_BYTES)]
                            mt = _REVOKE_TYPE_RE.search(head)
                            ctrl = (mt is not None
                                    and mt.group(1) == b"revoke")
                            index.append((ik[0], ik[1], pos, end, ctrl))
                            _index_bytes += len(ik[0]) + 120
                            if (len(index) > _INDEX_MAX_ENTRIES
                                    or _index_bytes > _INDEX_MAX_BYTES):
                                # 弃用索引：**继续**扫完 spans（不许因索引
                                # 超限截断撤销区间），仅索引永久停用
                                index = None
                                index_dropped = True
                pos = end + 1
            scanned = boundary
            fp_len = min(_PREFIX_FP_BYTES, scanned)
            fp = _md5(raw[:fp_len]) if fp_len > 0 else ""
            # 尾指纹：已扫边界往前 64B（同长原地改写的第二道闸）
            tail_off = max(0, scanned - 64)
            tail_fp = _md5(raw[tail_off:scanned]) if scanned > 0 else ""
            dt = (time.perf_counter() - t0) * 1000.0
            if need_full:
                _SCAN_STATS["full_rebuilds"] += 1
                _SCAN_STATS["bytes_scanned"] += scanned
                logger.info("[event_log] 撤销/索引全量重建 file=%s "
                            "scanned=%.1fMB spans=%d index=%s took=%.0fms",
                            path.name, scanned / 1e6, len(spans),
                            "no" if index is None else len(index), dt)
            else:
                _SCAN_STATS["delta_scans"] += 1
                _SCAN_STATS["bytes_scanned"] += scanned - start
                _SCAN_STATS["bytes_skipped"] += start
                logger.info("[event_log] 撤销增量扫描 file=%s new=%.1fKB "
                            "skipped=%.1fMB spans=%d took=%.1fms",
                            path.name, (scanned - start) / 1e3,
                            start / 1e6, len(spans), dt)
            if index is not None:
                _SCAN_STATS["index_lines"] = max(_SCAN_STATS["index_lines"],
                                                 len(index))
            # 〔P25 口径〕**先淘汰后写入**——顺序反了（写入后 key 恒在 state，
            # prune 的 `key not in` 守卫永不成立 ⇒ 路径上限从未生效，2026-11-19
            # 反例③实证）。
            _prune_scan_state(key)
            _SCAN_STATE[key] = {"mtime_ns": mtime, "size": size,
                                "ino": ino, "ctime": ctime,
                                "size_scanned": scanned, "spans": spans,
                                "index": index, "has_index": index is not None,
                                "index_dropped": index_dropped,
                                "fp": fp, "fp_len": fp_len,
                                "tail_fp": tail_fp,
                                "index_bytes": _index_bytes}
            return spans, index
        finally:
            raw.close()


def _prune_scan_state(key: str) -> None:
    """〔P25 口径沿用〕超上限丢最旧（按插入序，不按时间）。"""
    if len(_SCAN_STATE) >= _REVOKE_CACHE_MAX and key not in _SCAN_STATE:
        for old in list(_SCAN_STATE.keys())[:len(_SCAN_STATE)
                                            - _REVOKE_CACHE_MAX + 1]:
            _SCAN_STATE.pop(old, None)


def invalidate_scan_cache(path: Optional[Path] = None,
                          work_id: Optional[str] = None,
                          card_id: Optional[str] = None) -> int:
    """显式失效撤销/索引读侧缓存（2026-11-19 返工3）。

    **供原地改写事件文件的入口调用**——原地覆盖写（同 inode、同创建时间）
    无法被身份信号检测，改写方**必须**在写后调用本函数；否则热读会沿用
    旧 spans/index（已撤销记录复现/旧页错乱）。走**原子替换**
    （写临时文件 + ``os.replace``）的入口不需要它——inode/创建时间变化
    会被身份校验自动识别。

    参数：``path``＝事件文件路径；或 ``work_id``+``card_id`` 组合；
    全缺省＝清空全部（谨慎：多卡场景会让下一次读全部走重建）。
    返回清掉的条目数。
    """
    keys: list[str]
    if path is not None:
        keys = [str(path)]
    elif work_id and card_id:
        keys = [str(event_path(work_id, card_id))]
    else:
        keys = list(_SCAN_STATE.keys())
    n = 0
    for k in keys:
        if _SCAN_STATE.pop(k, None) is not None:
            n += 1
    return n


def _revoke_spans(path: Path) -> list[tuple[int, int]]:
    """扫出所有 revoke 的作废区间 ``(from_seq, revoke_seq)``。

    〔5715 · 治「切卡卡死」〕原来的 :func:`timeline` 为了让 revoke 生效，
    对**全文件**逐行 ``json.loads``（47492 行 ⇒ 秒级）——而这个函数是
    **同步**调用、跑在 async 端点里 ⇒ **堵死事件循环** ⇒ 期间所有请求排队
    ⇒ 页面「没反应」。

    〔P24 · 2026-10-26〕三重提速（实测 card_021：109.5MB / 8277 行）：
      ① 定位命中处用 ``bytes.find``（C 层）而不是 Python 逐行迭代；
      ② **先在行首 2KB 内判 type**——不是 revoke 就不必**整行** json.loads；
      ③ 缓存撤区间（同一次切卡会多次调 timeline）。
    〔2026-11-19 性能批次二〕④ **增量**：追加只扫新增完整行，不再每追加一次
    就全文件重扫（旧缓存键 (mtime,size) 追加即失效——card_007 747MB 每次重付秒级）。
    """
    spans, _idx = _scan_state(path, want_index=False)
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
        return (_norm_ts(r.get("ts")), int(r.get("seq") or 0))

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

    if before:
        # ── 翻历史·索引路径（2026-11-19 性能批次二）────────────────────
        # 旧实现走全量 read()（整文件逐行 json.loads：109MB 实测 ~360ms、
        # 内存翻 2-4 倍，747MB 真卡即 MemoryError 高危）。改用行位置索引：
        # 可见集合按 (ts, seq) 排序后按**序位**截窗口，只解析窗口内那些行。
        spans, index = _scan_state(path, want_index=True)
        if index is not None:
            try:
                b = int(before)
            except (TypeError, ValueError):
                b = 0
            vis = [(ts, seq, off, end)
                   for (ts, seq, off, end, ctrl) in index
                   if not ctrl and seq >= from_seq
                   and not _in_revoked(seq, spans)]
            vis.sort(key=lambda x: (x[0], x[1]))   # 稳定排序：同键保持文件序
            window = vis
            if b > 0:
                # before 指「当前显示段首条的 seq」——按**序位**截（与全量
                # 路径逐字同语义：写乱序时段里 seq ≠ 时序，比大小会错排）。
                idx = next((i for i, x in enumerate(vis) if x[1] == b), None)
                window = (vis[:idx] if idx is not None
                          else [x for x in vis if x[1] < b])
            if want_tail > 0:
                window = window[-want_tail:]
            raw = _mmap_ro(path)
            if raw is None:
                return []
            try:
                items = []
                for _ts, seq, off, end in window:
                    try:
                        rec = _loads(raw[off:end].decode("utf-8",
                                                         errors="replace"))
                    except (ValueError, TypeError):
                        continue
                    if isinstance(rec, dict):
                        rec["seq"] = seq      # 与 read() 同口径：seq int 化
                        items.append(rec)
            finally:
                raw.close()
            items.sort(key=_chrono_key)
            return items
        # 索引不可用（超行数上限/极端文件）⇒ 落到下面的全量路径（原语义兜底）

    # ── 全量路径（翻历史兜底）：与改前逐字一致 ───────────────────────
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


def timeline_events(work_id: str, card_id: str,
                    seqs: list[int]) -> tuple[list[dict[str, Any]], set[int]]:
    """[窄投影补详情 2026-11-21 · 性能批次二] 按 seq 精确取**完整**记录。

    ``timeline?view=brief`` 把重字段投影成预览 + ``_brief`` 标记；前端展开
    折叠详情时按 seq 回取原文——本函数就是那条按需读取的取数路。

    返回 ``(items, revoked_seqs)``：
      - items：命中的完整记录（与 timeline() 同形态，含 revoke 软删检查——
        被撤记录**不回**，其 seq 进 revoked_seqs）；
      - revoked_seqs：请求里已进撤销区间的 seq（前端据此显示「已撤销」
        而不是「加载失败」）。
    找不到的 seq 两边都不进（前端按缺失处理）。

    取数走 ``_scan_state`` 行索引（seq→字节偏移定点读），索引不可用
    （超行数上限/极端文件）时退回全量 ``read()`` 逐条比对——语义等价、
    只是慢，属少数路径。
    """
    want: dict[int, int] = {}
    for s in seqs:
        try:
            want[int(s)] = 0
        except (TypeError, ValueError):
            continue
    if not want:
        return [], set()
    path = event_path(work_id, card_id)
    spans, index = _scan_state(path, want_index=True)
    revoked = {s for s in want if _in_revoked(s, spans)}
    items: list[dict[str, Any]] = []
    if index is not None:
        offs = [(s, off, end) for (_ts, s, off, end, _c) in index
                if s in want and s not in revoked]
        if offs:
            raw = _mmap_ro(path)
            if raw is not None:
                try:
                    for s, off, end in offs:
                        try:
                            rec = _loads(raw[off:end].decode(
                                "utf-8", errors="replace"))
                        except (ValueError, TypeError):
                            continue
                        if isinstance(rec, dict):
                            rec["seq"] = s
                            items.append(rec)
                finally:
                    raw.close()
        return items, revoked

    # ── 兜底：索引不可用 ⇒ 全量比对（与 timeline() 兜底同语义）──────
    for rec in read(work_id, card_id):
        s = int(rec.get("seq") or 0)
        if s in want and s not in revoked:
            items.append(rec)
    return items, revoked


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
