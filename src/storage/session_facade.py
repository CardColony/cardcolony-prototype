# -*- coding: utf-8 -*-
"""会话统一读入门面（迭代一·会话底座 · S1 格式层 ＋ S2 门面层）。

[施工依据] 《迭代一会话底座施工任务书》p4b 终稿 · 施工件 S1（T046-T068）＋
S2（T069-T077），同批交付；设计输入《迭代一会话底座方案-链终稿-20261010.md》
4.1／4.2（167 行／sha16=AD028889D5C0A1FC）。
[裁定留痕] R2 对齐决议＝A（板子按会话实例数据落 works/{work_id}/，与工具卡
剧本库分层；代拍 2026-10-10，依据＝用户「直接执行」授权＋任务书 §2.4 推荐口径，
用户可事后推翻）。本件不涉 R2 落地，仅留痕。
[S6b 衔接桥·已登记] V2 块白名单无 pair_id（uuid）位（设计 T057：pair＝响应
发起块 seq，吸收其语义）；但五键等价（判据 1）与重发去重（routes :561-567）
要求 V1 请求身份跨升格存活——故 uuid 经 T063 前向兼容通道落盘（pair_id 行，
读取侧 unknown_field 保留原值），白名单九字段不动。S6b 切换时由消费方裁定
该通道的最终归属（升格为正式字段／迭代二重定义请求身份）。

分层（A-08 接口化，T070）：
- 门面（本模块对外接口）是唯一被消费方看到的层；
- 解析器（本模块内部）是唯一知道字节格式的层。

V1 路径（T071）：session_store.read_session／append_message／last_ts 函数语义
保留、调用面收进门面——本模块以**委托**方式复用（零拷贝、零签名改动），
旧消费方的切换在 S6。R-2 禁令随统一入口作废（T072）。

V2 格式（4.1／T046-T065）：
- 文件头：文件以 `---SESSION---` 行起头（V2 判别依据：首个非空行＝该标记；
  否则判 V1），随后 `k: v` 行至独立 `---` 行止。字段：version: 2／session_id／
  identity 三键（展平为 identity_owner／identity_purpose／identity_mode——
  owner: human|execution、purpose: discussion|execution|mixed、mode 预留）／
  inbox_ref（R4 收件位索引，缺省空）／condense_mark（凝练水位直读位，迁移
  翻译写入；现场升格时真值未知则省略，读侧缺省按 0——绝不写假值）。
- 块结构：`---MSG---` 起、`---END---` 止；块头 `k: v` 行至独立 `---` 行止。
  块头白名单九字段：seq／actor（human|assistant|card:card_NNN）／from（仅
  actor=card 时出现）／turn／pair（响应发起块 seq，human 块缺省自身）／model／
  behavior（类型留位，迭代一恒空）／end（仅轮末块：
  model_done|policy_stop|human_stop|error）／ts。
- 轮界与结束合一（T061）：turn 值相同的连续块即一轮，轮末块 end 闭合；
  不设独立轮界标记行。门面 append 自动闭合：assistant 块缺省 end=model_done
  （显式传入优先），human/card 块不闭合轮。
- 转义（T062）：机制一字不动沿用 session_store.py:47-54（行首反斜杠、精确行
  匹配），结构行集合从 {---MSG---} 扩为 {---MSG---, ---END---, ---SESSION---}——
  同一机制覆盖新增结构行，不引入新转义符号。V1 解析行为零变化。
- 语法三规则（T063-T065）：①白名单外字段行——保留原值＋unknown_field 标记，
  不炸不吞（前向兼容）；②必字段（seq/actor/turn）缺失或不可解析→块标
  damaged；③未闭合块标 damaged（交体检处置，体检器与 damaged/ 旁路在 S3/A3
  落地）。**damaged 块以占位条目存在于 read() 输出、不吞并**（T117 水位语义
  的承重前提：块序位次与 seq 恒等对应）。
- V1 兼容读（T066-T068）：委托旧解析＋门面内补派生字段——seq 现算（0 起，
  与旧 read_session 同语义）；actor 由 role 映射（user→human／assistant→
  assistant）；turn/pair/end 按块序推：assistant 块闭合轮（其后块轮号 +1，
  首块 turn=0），human 块 pair=自身 seq，assistant 块 pair=本轮最近 human 块
  seq（推不出＝"unknown"）；end 推导：V1 的 assistant 块全部出自成功交付路径
  （message_runner.finish），故推 model_done；human 块不闭合轮，end 空串。
  **V1 文件只读不写**——任何追加先现场升格（4.8：转换→tmp→os.replace→再追加，
  转换逐块一一对应、块数不变）。

投影（4.2／T074-T076）：project() 输出带 derived: true 头＋生成时刻；
rebuild() 从真相（会话文件）全量重建；写路径（append/scan_tail）永不读投影
（结构上不引用，测试用故障注入钉死）。

扫描快路径（T073）：scan_tail 对 V1 委托 session_store.last_ts（尾部反向扫描
语义保留）；对 V2 自带尾部反向扫描（CRLF 归一化自 :200-202 一并吸收），seq
自末块头直读。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Optional

import aiofiles

from . import card_store, event_log, origin_store, revision_store, session_ops, session_store

logger = logging.getLogger("storage.session_facade")

# ---------------------------------------------------------------- 常量
HEAD_MARKER = "---SESSION---"
BLOCK_MARKER = session_store.MARKER          # ---MSG---
BLOCK_BODY_SEP = "---"
BLOCK_END_MARKER = "---END---"

# V2 结构行全集（转义覆盖面；机制沿用 session_store.py:47-54）
V2_STRUCT_LINES = (BLOCK_MARKER, BLOCK_END_MARKER, HEAD_MARKER)

HEADER_FIELDS = (
    "version", "session_id",
    "identity_owner", "identity_purpose", "identity_mode",
    "inbox_ref", "condense_mark",
)
# [工具结果落会话 2026-10-17] 白名单新增 tool_calls／tool_call_id 两字段：
# 工具调用（actor=assistant，content=""，带 tool_calls 数组）与工具结果
# （actor=tool，带 tool_call_id）成对落会话，成为对话的一部分（对齐通用 agent）。
# 存量会话旧块无这两字段 → 解析为零迁移（缺字段＝空）。
BLOCK_FIELDS = ("seq", "actor", "from", "turn", "pair", "model", "behavior", "end", "ts",
                "tool_calls", "tool_call_id",
                # [保留式思考 2026-11-12] Kimi k3.1-pro 恒推理且要求历史 assistant
                # 原样回传 reasoning_content——字段必须落会话块，否则跨回合思考
                # 全蒸发（改前只挂在 _reply_blk 入参上，白名单重建时被剥掉）。
                "reasoning_content")
BLOCK_REQUIRED = ("seq", "actor", "turn")
END_VALUES = ("model_done", "policy_stop", "human_stop", "error")
ACTOR_HUMAN = "human"
ACTOR_ASSISTANT = "assistant"

_IDENTITY_DEFAULT = {"owner": ACTOR_HUMAN, "purpose": "discussion", "mode": ""}


# ---------------------------------------------------------------- 转义（机制沿用 :47-54）
def _escape_v2(content: str) -> str:
    """正文转义：结构行精确匹配加行首反斜杠（机制同 session_store._escape）。"""
    return "\n".join(
        "\\" + line if line in V2_STRUCT_LINES else line
        for line in content.split("\n")
    )


def _unescape_v2(lines: list[str]) -> list[str]:
    """还原转义（机制同 session_store._unescape：精确行匹配）。"""
    return [
        line[1:] if (line.startswith("\\") and line[1:] in V2_STRUCT_LINES) else line
        for line in lines
    ]


# ---------------------------------------------------------------- 路径
def _session_path(work_id: str, card_id: str) -> Path:
    # 动态引用 session_store 模块属性——conftest 沙箱对 WORKS_DIR 的重定向
    # 必须对本模块生效（activity.py 自带常量的盲区教训，勿改成 from-import）。
    return session_store._session_path(work_id, card_id)


def _damaged_dir(work_id: str) -> Path:
    return session_store.WORKS_DIR / work_id / "damaged"


def _projection_path(work_id: str) -> Path:
    return session_store.WORKS_DIR / work_id / "_projection" / "session_cards.json"


# ------------------------------------------------ Windows 文件锁竞态重试（2026-10-17）
# [WinErr13 竞态 · 2026-10-17 实测归纳] 重试预算：5 次尝试，指数退避
# 0.08/0.16/0.32/0.64 秒 ⇒ 单次写最多让出约 1.2 秒。
_LOCK_RETRY_ATTEMPTS = 5
_LOCK_RETRY_BASE_SLEEP = 0.08
_FILE_ATTRIBUTE_READONLY = 0x1


def _is_windows_lock_race(exc: OSError, path: Path) -> bool:
    """判「Windows 文件锁竞态」——只对可恢复的占用放行重试，其余原样上抛。

    Windows 上两类 errno 13（PermissionError）形态不同，必须分开：

    - ``winerror`` 5/32：**双路径**形态（``[WinError 5] 拒绝访问。: 'src' -> 'dst'``），
      来自 ``os.replace``／MoveFileEx 撞独占句柄——经典共享冲突，交给重试。
    - ``winerror`` 为 None 的**单路径**形态（``[Errno 13] Permission denied: 'path'``），
      来自 ``open()``——CRT 把 CreateFile 失败折成 errno 而不带 winerror。**这才是
      线上真实 502 的形态**：改动前唯独给 ``os.replace`` 配了重试，而 append/写 tmp 的
      ``open()`` 零重试，撞上即整轮 502。

    但它与「真无权限」（只读属性／ACL 拒绝）**异常形态完全相同**，仅凭异常分不开。
    故再查一次目标文件的只读属性：带只读位＝真无权限 ⇒ 立即上抛、零重试；
    不带只读位＝按锁竞态处理（有界重试）。
    """
    if os.name != "nt" or not isinstance(exc, PermissionError):
        return False
    winerror = getattr(exc, "winerror", None)
    if winerror in (5, 32):
        return True
    if winerror is not None:
        return False          # 其它 winerror（非本次两类）⇒ 行为不变，不重试
    try:
        attrs = os.stat(path).st_file_attributes
    except (OSError, AttributeError):
        return False          # 目标已消失等异常形态 ⇒ 不猜、不重试
    return not bool(attrs & _FILE_ATTRIBUTE_READONLY)


def _log_lock_race(exc: OSError, path: Path, attempt: int, what: str,
                   give_up: bool = False) -> None:
    """锁竞态对账日志：**每次写操作至多一条**（首次重试一条、重试耗尽再一条）。"""
    logger.warning(
        "[facade] Windows 文件锁竞态 %s %s：attempt=%d/%d winerror=%s errno=%s path=%s",
        "重试耗尽，上抛" if give_up else "退避重试",
        what, attempt + 1, _LOCK_RETRY_ATTEMPTS,
        getattr(exc, "winerror", None), getattr(exc, "errno", None), path)


def _open_with_lock_retry(path: Path, mode: str, what: str) -> Any:
    """按 mode 打开文件，遇 Windows 文件锁竞态做有界重试＋指数退避（真让出）。

    **只重试 ``open`` 本身**：失败时一个字节都还没落盘，重试无副作用；
    写盘／flush／fsync 一律留在调用方原地，其失败行为与改动前逐字相同。
    非锁竞态异常与重试耗尽 ⇒ 原对象上抛，不静默吞。
    """
    for attempt in range(_LOCK_RETRY_ATTEMPTS):
        try:
            return open(path, mode, encoding="utf-8")
        except PermissionError as exc:
            if attempt == _LOCK_RETRY_ATTEMPTS - 1 or not _is_windows_lock_race(exc, path):
                if attempt and _is_windows_lock_race(exc, path):
                    _log_lock_race(exc, path, attempt, what, give_up=True)
                raise
            if not attempt:
                _log_lock_race(exc, path, attempt, what)
            time.sleep(_LOCK_RETRY_BASE_SLEEP * (2 ** attempt))
    raise AssertionError("unreachable")  # pragma: no cover


def _replace_with_lock_retry(src: Path, dst: Path, what: str) -> None:
    """tmp→目标原子改名，带同一套有界重试＋退避。

    ``MoveFileEx`` 失败是原子的（要么换成功、要么源仍在），故重试不会产生半态；
    非「文件被占用」两类（如跨卷 EXDEV）照旧立即上抛。
    """
    for attempt in range(_LOCK_RETRY_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            if attempt == _LOCK_RETRY_ATTEMPTS - 1 or not _is_windows_lock_race(exc, dst):
                if attempt and _is_windows_lock_race(exc, dst):
                    _log_lock_race(exc, dst, attempt, what, give_up=True)
                raise
            if not attempt:
                _log_lock_race(exc, dst, attempt, what)
            time.sleep(_LOCK_RETRY_BASE_SLEEP * (2 ** attempt))


# ---------------------------------------------------------------- 原子写（T079 机制，先例 memory_store.py:179-182）
def _atomic_write_text(path: Path, text: str) -> None:
    """覆盖路径：tmp 写＋flush＋fsync→os.replace（T079；fsync 自 S3/T078 起）。

    ★ [卡片底座第一步 2026-10-14 修] 两处 Windows 加固（语义零变化）：

    1. ``os.replace`` **必须在 ``with`` 之外**——原写法把 rename 放在
       ``with open(tmp)`` 块内，源文件句柄尚未关闭时 MoveFileEx 会失败。
    2. rename **失败即短重试**。读路径（``read``）**不拿锁**，读句柄可能正压在
       目的文件上；Windows 的 MoveFileEx 遇到目的文件被占用的句柄会回
       ``WinError 5/32``（POSIX 允许 rename 覆盖被读的文件，Windows 不允许）。
       实测：沙箱内同一用例连跑 6 次挂 3 次（rename 一次，而测试每 50ms 读一次
       会话文件）。重试 4 次、退避 20/40/60/80ms 即稳（同目录 rename，不跨卷）。

    这不是新问题，是既有隐患；本步的卡间调起链路让「收件方**首次**落块」走到
    这条 tmp→replace 路径（改前该路径由发信块先走过），从而把它暴露出来。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with _open_with_lock_retry(tmp, "w", "atomic-write-tmp") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    _replace_with_lock_retry(tmp, path, "atomic-replace")


# ---------------------------------------------------------------- 格式判别与解析（S1）
def _detect_format(text: str) -> int:
    """无 V2 头即判 V1（T066）。判据：首个非空行是否为 ---SESSION---。"""
    for line in text.split("\n"):
        if line.strip() == "":
            continue
        return 2 if line == HEAD_MARKER else 1
    return 1  # 空文件按 V1（读出空列表，语义不变）


def _parse_kv_lines(lines: list[str], whitelist: tuple[str, ...]) -> tuple[dict, list[str]]:
    """解析 `k: v` 行。白名单外字段：保留原值＋登记（T063，不炸不吞）。

    裸 `k:` 行（冒号收尾、无空格）＝空值字段，与 `k: ` 等价。
    """
    kv: dict[str, str] = {}
    unknown: list[str] = []
    for line in lines:
        if ": " in line:
            k, v = line.split(": ", 1)
        elif line.endswith(":") and len(line) > 1:
            k, v = line[:-1], ""
        else:
            unknown.append(f"!raw:{line}")
            continue
        kv[k] = v
        if whitelist and k not in whitelist:
            unknown.append(k)
    return kv, unknown


def _parse_v2(text: str, content_limit: int = 0) -> dict[str, Any]:
    """V2 解析器。返回 {"header": dict, "blocks": [输出形态块]}。

    damaged 块以占位条目存在、不吞并（T117 承重语义）。

    〔2026-10-31〕``content_limit``：每个块的正文**只保留前 N 字符**
    （投影读用；0＝全保留＝旧行为）。目的是**别先造出整份正文再丢**——
    card_021 的 117MB 正文先 join 出来＝瞬时多占 900MB。
    """
    lines = text.split("\n")
    n = len(lines)
    i = 0
    # 文件头：HEAD_MARKER 之后到独立 --- 行
    header: dict[str, Any] = {}
    header_unknown: list[str] = []
    if lines and lines[i] == HEAD_MARKER:
        i += 1
        head_lines: list[str] = []
        while i < n and lines[i] != BLOCK_BODY_SEP:
            head_lines.append(lines[i])
            i += 1
        if i < n:
            i += 1  # 跳过 ---
        kv, header_unknown = _parse_kv_lines(head_lines, HEADER_FIELDS)
        header = dict(kv)
        header.pop("version", None)
        header["version"] = int(kv["version"]) if kv.get("version", "").isdigit() else kv.get("version", "")
        if "condense_mark" in header and str(header["condense_mark"]).isdigit():
            header["condense_mark"] = int(header["condense_mark"])
    if header_unknown:
        header["unknown_fields"] = header_unknown

    blocks: list[dict[str, Any]] = []
    while i < n:
        if lines[i] != BLOCK_MARKER:
            i += 1
            continue
        i += 1
        head_lines = []
        while i < n and lines[i] != BLOCK_BODY_SEP:
            head_lines.append(lines[i])
            i += 1
        header_closed = i < n
        if header_closed:
            i += 1  # 跳过 ---
        kv, unknown_fields = _parse_kv_lines(head_lines, BLOCK_FIELDS)
        content_lines: list[str] = []
        while i < n and lines[i] != BLOCK_END_MARKER:
            content_lines.append(lines[i])
            i += 1
        closed = i < n
        if closed:
            i += 1  # 跳过 ---END---
        else:
            # 未闭合块：剥离一个尾部空行（content 与 ---END--- 之间的分隔换行
            # 在撕裂时仍会落盘；语义沿用旧解析 :135-136 的尾空行剥离）
            if content_lines and content_lines[-1] == "":
                content_lines.pop()
        if content_limit > 0:
            content_lines = _truncate_content_lines(content_lines, content_limit)
        blocks.append(_materialize_v2_block(kv, content_lines, header_closed, closed, unknown_fields))
    return {"header": header, "blocks": blocks}


def _materialize_v2_block(kv: dict, content_lines: list[str],
                          header_closed: bool, closed: bool,
                          unknown_fields: list[str]) -> dict[str, Any]:
    missing: list[str] = []
    seq: Optional[int] = None
    if kv.get("seq", "").isdigit():
        seq = int(kv["seq"])
    else:
        missing.append("seq")
    actor = kv.get("actor", "")
    if not actor:
        missing.append("actor")
    turn: Optional[int] = None
    if kv.get("turn", "").lstrip("-").isdigit():
        turn = int(kv["turn"])
    else:
        missing.append("turn")

    content = "\n".join(_unescape_v2(content_lines))
    # [卡间消息·头 2026-10-14] actor 以 `card:` 开头（卡间来信）→ role 映射为
    # **`user`**。原实现走 `.get(actor, actor)` 兜底 ⇒ 投影出字面 `card:card_002`，
    # **不是合法角色**（端点只认 system/user/assistant/tool）⇒ 一进请求即 400。
    # 卡间信是"外部输入"，语义上就是 user；"是谁发的"由正文承载，不由 role/头。
    if actor and str(actor).startswith("card:"):
        role = "user"
    else:
        role = {"human": "user", "assistant": "assistant"}.get(actor, actor or "unknown")
    pair_raw = kv.get("pair", "")
    # [工具结果落会话 2026-10-17] tool_calls（工具调用块，JSON 数组串）与
    # tool_call_id（工具结果块）读回：缺字段＝空（存量旧块零迁移）。
    tool_calls_raw = kv.get("tool_calls", "")
    tool_calls: Optional[list[dict[str, Any]]] = None
    if tool_calls_raw:
        try:
            _parsed_tc = json.loads(tool_calls_raw)
            if isinstance(_parsed_tc, list):
                tool_calls = _parsed_tc
        except (ValueError, TypeError):
            logger.warning("[facade] tool_calls 字段解析失败（视同无）seq=%s: %r",
                           kv.get("seq", "?"), tool_calls_raw)
    legacy_pair_id = kv.get("pair_id", "")  # V1 uuid 经前向兼容通道保留（见模块头 S6b 衔接桥）
    # [保留式思考 2026-11-12] reasoning_content 是 JSON 单行字符串；缺字段＝空
    # （存量旧块零迁移），解析失败退化为原文串（不丢内容）。
    _rc_raw = kv.get("reasoning_content", "")
    reasoning_content = ""
    if _rc_raw:
        try:
            _rc = json.loads(_rc_raw)
            reasoning_content = _rc if isinstance(_rc, str) else str(_rc)
        except (ValueError, TypeError):
            reasoning_content = _rc_raw
    pair_id_out = legacy_pair_id if legacy_pair_id != "" else (str(pair_raw) if pair_raw != "" else "")
    blk: dict[str, Any] = {
        # 五键（legacy 契约键）
        "role": role,
        "content": content,
        "timestamp": kv.get("ts", ""),
        "model": kv.get("model") or None,
        "pair_id": pair_id_out,
        "seq": seq,
        # V2 六事实字段
        "actor": actor or "unknown",
        "from": kv.get("from", ""),
        "turn": turn,
        "pair": str(pair_raw),
        "end": kv.get("end", ""),
        "behavior": kv.get("behavior", ""),
        "ts": kv.get("ts", ""),
        # 工具块两字段（缺＝None/空串；存量旧块零迁移）
        "tool_calls": tool_calls,
        "tool_call_id": kv.get("tool_call_id", "") or "",
        "reasoning_content": reasoning_content,
        "format": "v2",
    }
    damaged = bool(missing) or not header_closed or not closed
    if damaged:
        blk["damaged"] = True
        if missing:
            blk["damaged_reason"] = "missing:" + ",".join(missing)
        elif not header_closed:
            blk["damaged_reason"] = "unclosed_header"
        else:
            blk["damaged_reason"] = "unclosed"
    if unknown_fields:
        blk["unknown_fields"] = list(unknown_fields)
        vals = {k: kv[k] for k in unknown_fields if not k.startswith("!raw:") and k in kv}
        if vals:
            blk["unknown_values"] = vals  # T063：白名单外字段保留原值
    return blk


# ---------------------------------------------------------------- V1 兼容读（T066-T068）
def _enrich_v1(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """旧 read_session 输出 → 门面块：五键原样透传（契约）＋派生字段补齐。"""
    out: list[dict[str, Any]] = []
    turn = 0
    last_human_seq: Optional[int] = None
    for m in msgs:
        seq = m["seq"]  # 旧解析已现算（0 起）——「seq 现算（V1 时代语义）」
        role = m.get("role", "")
        actor = {"user": ACTOR_HUMAN, "assistant": ACTOR_ASSISTANT}.get(role, role or "unknown")
        if actor == ACTOR_HUMAN:
            pair = str(seq)
            last_human_seq = seq
        else:
            pair = str(last_human_seq) if last_human_seq is not None else "unknown"
        # end 推导：V1 assistant 块全部出自成功交付路径（finish）→ model_done
        end = "model_done" if role == "assistant" else ""
        out.append({
            "role": role,
            "content": m["content"],
            "timestamp": m["timestamp"],
            "model": m.get("model"),
            "pair_id": m.get("pair_id", ""),
            "seq": seq,
            "actor": actor,
            "from": "",
            "turn": turn,
            "pair": pair,
            "end": end,
            "behavior": "",
            "ts": m["timestamp"],
            "format": "v1",
        })
        if role == "assistant":
            turn += 1
            last_human_seq = None
    return out


def _read_v1_sync(work_id: str, card_id: str) -> list[dict[str, Any]]:
    """V1 会话整读＋解析的**同步门面**——给 ``asyncio.to_thread`` 用。

    〔2026-11-04〕``session_store.read_session`` 是协程，to_thread 只能吃同步
    函数；worker 线程里没有运行中的循环 ⇒ ``asyncio.run`` 起一个把它跑完。
    """
    return asyncio.run(session_store.read_session(work_id, card_id))


# ---------------------------------------------------------------- 读（T023）
# 〔2026-10-26 · 「页面的内容存进内存态」〕**会话解析结果的内存缓存**。
#
# 病根（实测）：旧档案扫描（`/card-messages` 的源二）每次都要把**每张卡的整份
# 会话文件**读一遍＋解析一遍：work_027 的 20 张卡 / 16427 块 ⇒
# **读 1308ms（磁盘慢）＋ 解析 802ms ＝ 1.7~2.1 秒**，而且**每次刷新都重算**
# ⇒ 画布页首屏那条 `card-messages?limit=200` 实测 **4417ms**，把同一时刻的
# `nav/version`（4239ms）、`rule`（2262ms）全拖慢 ⇒ 整页卡。
#
# ⇒ 缓存口径：**key = work/card，失效判据 = 文件的 (mtime_ns, size)**
# （文件一改就自动失效 ⇒ 写后读不会拿到旧内容）。
# 命中的块对象与今天一致（今天每次 read 都重新解析 ⇒ 每次都是新 dict；
# 缓存后是**共享 dict**）——已逐个核对全部调用点（card_draft / memory_store /
# message_runner / role_room / routes / wb_exec.bridge）**都只读不写**这些块
# （切片/过滤/投影）⇒ 共享安全。`read()` 末尾本就会新建过滤后的列表。
_PARSE_CACHE: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}
#: 缓存条目上限（超出按插入顺序丢最旧）——内存封顶，不是时间闸。
_PARSE_CACHE_MAX = 512

#: 〔2026-10-31 · 治「疯狂占内存」〕缓存的**字节预算**（进程内驻留上限）。
#:
#: 病根：原来只有**条数**上限（512 条）⇒ 而**单条**体积随会话线性增长。
#: 实测（work_027）：``session_021.md`` 117MB → 解析后 **909MB** Python 对象
#: （raw→dict 放大 **6.8~7.7×**）；前 12 张卡合计 **2.96GB** ⇒
#: 512 条最坏 **123GB** 常驻 ⇒ 进程 commit 被打穿 ⇒ 整机「低虚拟内存」。
#: 实证（Windows 事件 Resource-Exhaustion-Detector/2004）：7 天内 **28 次**
#: 「low virtual memory condition」，每次的榜首都是 python.exe（**16~42.8GB**），
#: **dwm.exe 每次都在前三**（2.5~3.7GB）⇒ dwm 被挤掉 ⇒ **桌面黑、鼠标失灵**；
#: 服务随后 `MemoryError` 崩溃（logs/server_errors.jsonl：10-24 00:07 的
#: `/card-messages`、10-30 00:20 的 `/timeline`）⇒ watchdog 重启（新 PID）。
#:
#: ⇒ 封顶必须按**字节**（= 用户口径的「容量」），不是「张数」。
_PARSE_CACHE_MAX_BYTES = int(
    os.environ.get("ECO_PARSE_CACHE_MB") or 512) * 1024 * 1024

#: 单条超过预算的这个占比就**不入缓存**——否则它一条吃光预算、把其余全挤掉
#: （每次都重解析 ⇒ 更慢）。超大会话本来就得重解析，不如老实不缓存。
_PARSE_CACHE_ENTRY_MAX_RATIO = 0.25

#: 「解析后**常驻**字节 / 源文件字节」的实测系数（1.09~2.75×，取 3.0 留余量）。
#: ⚠ 别用峰值系数（2.8~7.7×）——那是**解析过程中**的瞬时副本
#: （text ＋ split 行表 ＋ content 行表 ＋ join 出的正文），解析完就释放了；
#: **缓存里留下的是常驻那份** ⇒ 封顶按常驻算才准。
_PARSE_RETAINED_FACTOR = 3.0

#: 每条缓存的估算字节数（key → 估算值）。与 `_PARSE_CACHE` 同生命周期。
_PARSE_CACHE_BYTES: dict[str, int] = {}

#: 投影读：只保留这些字段（＋ `content` 截断到 `_PROJECT_CONTENT_CHARS`）。
#: 给「旧档案扫描」用——它只要 ``actor``/``content`` 头几行/``seq``/``ts``/``from``，
#: 却整份解析并**常驻**每张卡的全部会话（work_027 一次 = 3GB）。
_PROJECT_CONTENT_CHARS = 8192

#: 投影读时，V2 文件的正文**按字节**截断（源字节，不是解码后的字符）。
#: 为何按字节：正文解码成 str 要 3~4 倍空间（UCS-4），按字节计才拦得住内存；
#: 字符数必然 ≤ 字节数 ⇒ 截出来的字符数也 ≤ 这个值。
_PROJECT_CONTENT_BYTES = 8192


def _estimate_block_bytes(blocks: list[dict[str, Any]]) -> int:
    """估算一组块**常驻**的字节数（字符串净长 × 实测系数 ＋ 每块固定开销）。

    不做 ``sys.getsizeof`` 递归（对 4000+ 块的会话太慢）；用实测系数
    （常驻/文件 = 1.09~2.75×，取 3.0）估算 ⇒ 宁可略微高估（宁可早淘汰，不可超预算）。
    """
    text_len = 0
    for b in blocks:
        for v in b.values():
            if isinstance(v, str):
                text_len += len(v)
    return int(text_len * _PARSE_RETAINED_FACTOR) + len(blocks) * 512


def _project_blocks(blocks: list[dict[str, Any]], keys: tuple[str, ...],
                    content_chars: int = _PROJECT_CONTENT_CHARS
                    ) -> list[dict[str, Any]]:
    """**投影**：只保留需要的字段（＋ ``content`` 截断）。

    给「只要摘要/头行」的扫描用——旧档案扫描要的只是 ``actor``/``content`` 的
    头几行/``seq``/``ts``/``from``，却整份解析并**常驻**每张卡的全部会话
    （work_027 一次 = 3GB ⇒ 撑穿整机 commit ⇒ 桌面黑）。
    """
    keep = tuple(dict.fromkeys(("seq", "actor", "turn") + tuple(keys)))
    out: list[dict[str, Any]] = []
    for b in blocks:
        nb = {k: b[k] for k in keep if k in b}
        c = nb.get("content")
        if isinstance(c, str) and content_chars > 0 and len(c) > content_chars:
            nb["content"] = c[:content_chars]
        out.append(nb)
    return out


def _truncate_content_lines(lines: list[str], limit: int) -> list[str]:
    """只保留前 ``limit`` 个字符的行——**解析期**截断（别先造整份正文再丢）。"""
    if limit <= 0:
        return lines
    kept: list[str] = []
    total = 0
    for ln in lines:
        if total >= limit:
            break
        kept.append(ln)
        total += len(ln) + 1
    return kept


#: 通用换行（与文本模式读 `newline=None` 一致）：\r\n／\r／\n 都算换行。
_LINE_SPLIT = re.compile(b"\r\n|\r|\n")


class _ByteLineReader:
    """按**字节**行迭代 ＋ 一行回看。

    为什么要字节：V2 会话文件动辄上百 MB（work_027 的 ``session_021.md`` 117MB），
    ``text.split("\\n")`` 会额外造一份**行表**（实测 +320MB），而正文解码成
    UCS-4 又要 +396MB ⇒ 峰值 ~900MB。字节流式扫：标记行全是 ASCII（直接比字节），
    只在真的要留的行上解码 ⇒ 峰值 O(保留内容)。

    ⚠ 换行语义必须与**文本模式读**逐字一致（通用换行转换）：``\\r\\n``／``\\r``／
    ``\\n`` 都算换行。实测 work_027 有 2 个会话文件里混着**孤立 ``\\r``**
    ⇒ 只按 ``\\n`` 切会在内容里留下 ``\\r``，与整读版不一致。
    """

    def __init__(self, fh, chunk: int = 1 << 20) -> None:
        self._fh = fh
        self._chunk = chunk
        self._pending = b""
        self._queue: deque[bytes] = deque()
        self._eof = False
        self._buf: bytes | None = None

    def _next_raw(self) -> bytes | None:
        while not self._queue:
            if self._eof:
                if self._pending:
                    out = self._pending
                    self._pending = b""
                    if out.endswith(b"\r"):     # 文件末尾的孤立 \r 也是换行
                        out = out[:-1]
                    return out
                return None
            data = self._fh.read(self._chunk)
            if not data:
                self._eof = True
                continue
            data = self._pending + data
            tail = b""
            if data.endswith(b"\r"):
                # 末尾的 \r 可能是 \r\n 的一半 ⇒ 留到下一块再判
                data, tail = data[:-1], b"\r"
            parts = _LINE_SPLIT.split(data)
            rest = parts.pop() if parts else b""
            self._pending = rest + tail
            self._queue.extend(parts)
        return self._queue.popleft()

    def peek(self) -> bytes | None:
        if self._buf is None:
            self._buf = self._next_raw()
        return self._buf

    def next(self) -> bytes | None:
        v = self.peek()
        self._buf = None
        return v


def _detect_format_stream(path: Path) -> int:
    """首个非空行是不是 ``---SESSION---``（＝`_detect_format` 的流式版，判据逐字一致）。"""
    with open(path, "rb") as fh:
        it = _ByteLineReader(fh)
        while True:
            line = it.peek()
            if line is None:
                break
            it.next()
            s = line.decode("utf-8", "replace")
            if s.strip() == "":
                continue
            return 2 if s == HEAD_MARKER else 1
    return 1  # 空文件按 V1（与 `_detect_format` 一致）


def _project_v2_lines(lines: list[bytes], content_limit: int) -> list[dict[str, Any]]:
    """按**字节行**解析 V2（投影口径）：每块只留「头字段 ＋ 正文前 N 字节」。

    与 `_parse_v2` **逐字同构**（同一状态机、同一个 `_materialize_v2_block`），
    差别只在于不把整文件解码成 str ⇒ 峰值从 ~900MB 降到 ~250MB。
    超出截断长度的行**连解码都省了**（只跳过）⇒ CPU 也省。
    """
    b_msg = BLOCK_MARKER.encode("utf-8")
    b_sep = BLOCK_BODY_SEP.encode("utf-8")
    b_end = BLOCK_END_MARKER.encode("utf-8")
    b_head = HEAD_MARKER.encode("utf-8")
    blocks: list[dict[str, Any]] = []
    n = len(lines)
    i = 0
    # 文件头（HEAD_MARKER → 独立 --- 行）：投影用不上，只跳过
    if n and lines[i] == b_head:
        i += 1
        while i < n and lines[i] != b_sep:
            i += 1
        if i < n:
            i += 1
    while i < n:
        if lines[i] != b_msg:
            i += 1
            continue
        i += 1                      # 吃掉 ---MSG---
        head_lines: list[str] = []
        while i < n and lines[i] != b_sep:
            head_lines.append(lines[i].decode("utf-8", "replace"))
            i += 1
        header_closed = i < n
        if header_closed:
            i += 1                  # 跳过 ---
        kv, unknown_fields = _parse_kv_lines(head_lines, BLOCK_FIELDS)
        content_lines: list[str] = []
        kept_bytes = 0
        while i < n and lines[i] != b_end:
            raw = lines[i]
            room = content_limit - kept_bytes
            if room > 0:
                s = raw.decode("utf-8", "replace")
                if len(raw) + 1 > room:
                    # ⚠ **单行超长**（实测会话里一整块正文常在**一行**里）：
                    # 只按行截断会把它整条留下 ⇒ 必须在**行内**再切一刀。
                    s = s[:max(room - 1, 0)]
                    kept_bytes = content_limit
                else:
                    kept_bytes += len(raw) + 1
                content_lines.append(s)
            i += 1
        closed = i < n
        if closed:
            i += 1                  # 跳过 ---END---
        else:
            # 未闭合块：剥掉一个尾部空行（与 `_parse_v2` 同语义）
            if content_lines and content_lines[-1] == "":
                content_lines.pop()
        blocks.append(_materialize_v2_block(
            kv, content_lines, header_closed, closed, unknown_fields))
    return blocks


def _project_v2_stream(path: Path, content_limit: int) -> list[dict[str, Any]]:
    """**流式**读 V2 会话文件并做投影解析。

    为什么按字节读：``text = f.read()`` 会把 117MB 的文件解码成 **UCS-4** 字符串
    （文件里有非 BMP 字符 ⇒ 每字 4 字节）＝ **396MB**，再 ``split("\\n")`` 又要
    **320MB** 行表 ⇒ 峰值 ~900MB。按字节读 + ``splitlines()`` ⇒ 只 ~240MB，
    而且**只解码要留的那些行** ⇒ CPU 也省。
    """
    raw = path.read_bytes()
    lines = raw.splitlines()      # bytes 行（通用换行语义与文本模式读一致）
    del raw
    return _project_v2_lines(lines, content_limit)


def _evict_parse_cache() -> int:
    """按**字节**预算淘汰（插入序丢最旧）——**不按时间**（用户明令：只按容量封顶）。

    返回被淘汰的条数。
    """
    total = sum(_PARSE_CACHE_BYTES.values())
    if total <= _PARSE_CACHE_MAX_BYTES:
        return 0
    dropped = 0
    for key in list(_PARSE_CACHE.keys()):
        if total <= _PARSE_CACHE_MAX_BYTES:
            break
        _PARSE_CACHE.pop(key, None)
        total -= _PARSE_CACHE_BYTES.pop(key, 0)
        dropped += 1
    return dropped


def reset_cache() -> None:
    """清解析缓存。

    [2026-10-27 · 测试隔离] `_PARSE_CACHE` 按 `(work_id, card_id)` 键 + 文件 mtime 戳
    索引，而各用例的 work_id 相同（work_001）但库/文件在不同 tmp 目录 ⇒ 跨用例会读到
    上一个沙箱的块。conftest 的 sandbox fixture 里调本函数清掉。
    """
    _PARSE_CACHE.clear()
    _PARSE_CACHE_BYTES.clear()


def _file_stamp(path: Path) -> tuple[int, int]:
    """文件的 (mtime_ns, size)——缓存失效判据。取不到就当「已变」。"""
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


async def _read_projected(work_id: str, card_id: str, path: Path,
                           project: tuple[str, ...]) -> list[dict[str, Any]]:
    """投影读：V2 文件走**流式**解析；V1（会话库）走旧解析再投影。"""
    if _detect_format_stream(path) == 2:
        # 〔2026-11-04〕流式投影内部同样是整文件 read_bytes＋解析，挪线程池。
        blocks = await asyncio.to_thread(_project_v2_stream, path, _PROJECT_CONTENT_BYTES)
    else:
        blocks = _project_blocks(
            _enrich_v1(await asyncio.to_thread(_read_v1_sync, work_id, card_id)), project)
        return blocks
    return _project_blocks(blocks, project)


async def _read_parsed(work_id: str, card_id: str,
                       project: Optional[tuple[str, ...]] = None
                       ) -> list[dict[str, Any]]:
    """读＋解析会话文件（**带内存缓存**：文件没变就不读不解析）。

    〔2026-10-31 · 治「疯狂占内存」〕``project``：只要其中几个字段（＋ ``content``
    截断）。旧档案扫描用它——那个扫描**整份解析并常驻**每张卡的全部会话
    （work_027 一次 = 3GB ⇒ 撑穿整机 commit ⇒ dwm 被挤掉 ⇒ 桌面黑）。
    投影在**解析期**就生效（``content`` 在 ``_parse_v2`` 内截断）⇒
    峰值与常驻都按投影后的小体积算。
    """
    path = _session_path(work_id, card_id)
    if not path.exists():
        return []
    stamp = _file_stamp(path)
    key = f"{work_id}/{card_id}"
    if project:
        key += "|p:" + ",".join(project)
    hit = _PARSE_CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    if project:
        # 〔2026-10-31〕投影读走**流式**路径：只留要的头字段 ＋ 正文前 N 字符。
        # 为什么不整读：`_parse_v2` 把整个文件读成 str 再 split 成行表
        # （实测 117MB 文件 ⇒ 文本 396MB ＋ 行表 320MB ⇒ 峰值 909MB），
        # 而投影要的只是「每块的头几行」⇒ 边读边丢，内存 O(块数 × 截断长度)。
        blocks = await _read_projected(work_id, card_id, path, project)
    else:
        async with aiofiles.open(path, mode="r", encoding="utf-8") as f:
            text = await f.read()
        if _detect_format(text) == 1:
            # 同下：V1 逐行解析同样是同步 CPU，挪线程池（协程函数用 asyncio.run
            # 在 worker 线程内起独立循环跑掉）。
            blocks = _enrich_v1(await asyncio.to_thread(_read_v1_sync, work_id, card_id))
        else:
            # 〔2026-11-04 · 治「一切接口一起慢」〕``_parse_v2`` 是**同步纯 CPU**：
            # card_021 的 239MB 会话解析一次约 4~8s，而超大条目按
            # ``_PARSE_CACHE_ENTRY_MAX_RATIO`` 永不入缓存 ⇒ 在跑卡**每回合**
            # 都在事件循环上重解析一次 ⇒ 循环被掐死 ⇒ /api/nav 实测 21s、
            # getCard 5.7s、切卡/刷新全部排队转圈。挪到线程池：CPU 照烧，
            # 但 GIL 切换让事件循环保持调度，其它请求不再饿死。
            blocks = (await asyncio.to_thread(_parse_v2, text))["blocks"]
    if len(_PARSE_CACHE) >= _PARSE_CACHE_MAX:
        # 丢最旧的（插入序）——内存封顶
        for old in list(_PARSE_CACHE.keys())[:len(_PARSE_CACHE) - _PARSE_CACHE_MAX + 1]:
            _PARSE_CACHE.pop(old, None)
            _PARSE_CACHE_BYTES.pop(old, None)
    # 〔2026-10-31〕**按字节**记账与封顶（不是条数；**不按时间**）
    est = _estimate_block_bytes(blocks)
    if est <= _PARSE_CACHE_MAX_BYTES * _PARSE_CACHE_ENTRY_MAX_RATIO:
        _PARSE_CACHE[key] = (stamp, blocks)
        _PARSE_CACHE_BYTES[key] = est
        _evict_parse_cache()
    else:
        # 单条就超份额 ⇒ **不入缓存**（否则它把其余全挤掉 ⇒ 反复重解析更慢）
        _PARSE_CACHE.pop(key, None)
        _PARSE_CACHE_BYTES.pop(key, None)
    return blocks


async def read(work_id: str, card_id: str, *, from_seq: Optional[int] = None,
               to_seq: Optional[int] = None, actor: Optional[str] = None,
               turn: Optional[int] = None, limit: Optional[int] = None,
               project: Optional[tuple[str, ...]] = None) -> list[dict[str, Any]]:
    """统一读入口。V1 委托旧解析＋补派生；V2 走 V2 解析器。

    damaged 占位条目（seq 缺失者）在任何 seq 过滤下都保留——位次对齐承重语义
    （T117）；显式 actor/turn 过滤按块字段正常生效。

    〔2026-10-26〕解析结果走**内存缓存**（见 `_read_parsed`）：文件没变就不重解析。

    〔2026-10-31 · 治「疯狂占内存」〕``project``：**投影读**——只要列出的几个字段
    （＋ ``content`` 截断到 8KB）。给「旧档案扫描」用：它只看摘要与头行，
    却整份解析并常驻每张卡的全部会话（work_027 一次 ≈ 3GB ⇒ 撑穿整机 commit
    ⇒ dwm 被挤掉 ⇒ 桌面黑）。投影后同一扫描 ≈ 30MB。
    ⚠ 投影块**不含**正文全文 ⇒ 只用于「只看摘要/头行」的扫描；
      需要正文全文的调用点**不要**传 project。
    """
    blocks = await _read_parsed(work_id, card_id, project=project)
    if not blocks:
        return []

    def _keep(b: dict[str, Any]) -> bool:
        if from_seq is not None and b.get("seq") is not None and b["seq"] < from_seq:
            return False
        if to_seq is not None and b.get("seq") is not None and b["seq"] > to_seq:
            return False
        if actor is not None and b.get("actor") != actor:
            return False
        if turn is not None and b.get("turn") is not None and b["turn"] != turn:
            return False
        return True

    blocks = [b for b in blocks if _keep(b)]
    if limit is not None:
        blocks = blocks[:limit]
    return blocks


async def read_header(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """读 V2 文件头（体检器／迁移通道用）。非 V2 或文件不存在返回 None。"""
    path = _session_path(work_id, card_id)
    if not path.exists():
        return None
    async with aiofiles.open(path, mode="r", encoding="utf-8") as f:
        text = await f.read()
    if _detect_format(text) != 2:
        return None
    return (await asyncio.to_thread(_parse_v2, text))["header"]


# ---------------------------------------------------------------- 写（T024 ＋ 4.8 现场升格）
def _render_v2_header(header: dict[str, Any]) -> str:
    lines = [HEAD_MARKER]
    for k in ("version", "session_id", "identity_owner", "identity_purpose",
              "identity_mode", "inbox_ref"):
        if k in header:
            lines.append(f"{k}: {header[k]}")
    if header.get("condense_mark") is not None:
        lines.append(f"condense_mark: {header['condense_mark']}")
    lines.append(BLOCK_BODY_SEP)
    return "\n".join(lines) + "\n"


def _render_v2_block(b: dict[str, Any]) -> str:
    lines = [BLOCK_MARKER, f"seq: {b['seq']}", f"actor: {b['actor']}"]
    if b.get("from"):
        lines.append(f"from: {b['from']}")
    lines.append(f"turn: {b['turn']}")
    if b.get("pair") not in (None, ""):
        lines.append(f"pair: {b['pair']}")
    if b.get("pair_id"):
        # S6b 衔接桥：V1 请求身份（uuid）经 T063 前向兼容通道跨升格保留——
        # 白名单九字段不动，读取侧按 unknown_field 保留原值
        lines.append(f"pair_id: {b['pair_id']}")
    if b.get("model"):
        lines.append(f"model: {b['model']}")
    if b.get("behavior"):
        lines.append(f"behavior: {b['behavior']}")
    if b.get("tool_calls"):
        # [工具结果落会话 2026-10-17] 工具调用块（actor=assistant）：调用数组
        # JSON 单行（转义规则同 content——json.dumps 默认把换行转义为 \n，保单行）
        lines.append("tool_calls: " + json.dumps(b["tool_calls"], ensure_ascii=False))
    if b.get("tool_call_id"):
        # [工具结果落会话 2026-10-17] 工具结果块（actor=tool）：与上面调用一一配对的 id
        lines.append(f"tool_call_id: {b['tool_call_id']}")
    if b.get("reasoning_content"):
        # [保留式思考 2026-11-12] JSON 单行（同 tool_calls 规则：换行转义保单行）
        lines.append("reasoning_content: " +
                     json.dumps(b["reasoning_content"], ensure_ascii=False))
    if b.get("end"):
        lines.append(f"end: {b['end']}")
    lines.append(f"ts: {b.get('ts') or session_store._now_iso()}")
    lines.append(BLOCK_BODY_SEP)
    return "\n".join(lines) + "\n" + _escape_v2(b.get("content", "")) + "\n" + BLOCK_END_MARKER + "\n"


def _default_header(card_id: str, identity: Optional[dict] = None) -> dict[str, Any]:
    ident = dict(_IDENTITY_DEFAULT)
    if identity:
        ident.update({k: v for k, v in identity.items() if k in ("owner", "purpose", "mode")})
    return {
        "version": 2,
        "session_id": card_id,
        "identity_owner": ident["owner"],
        "identity_purpose": ident["purpose"],
        "identity_mode": ident["mode"],
        "inbox_ref": "",
    }


def _convert_v1_blocks(enriched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """V1 块 → V2 块（逐块一一对应，块数不变——水位恒等映射的承重点）。"""
    conv: list[dict[str, Any]] = []
    for b in enriched:
        nb = {
            "seq": b["seq"],
            "actor": b["actor"],
            "from": "",
            "turn": b["turn"],
            "pair": b["pair"] if str(b["pair"]).isdigit() else "",
            "pair_id": b.get("pair_id", ""),  # V1 uuid 跨升格保留（S6b 衔接桥）
            "model": b.get("model"),
            "behavior": "",
            "end": b["end"] if b["end"] in END_VALUES else "",
            "ts": b["ts"],
            "content": b["content"],
        }
        conv.append(nb)
    return conv


def _last_human_in_turn(blocks: list[dict[str, Any]], current_turn: int) -> Optional[int]:
    for b in reversed(blocks):
        if b.get("turn") is not None and b["turn"] < current_turn:
            break
        if b.get("actor") == ACTOR_HUMAN and b.get("seq") is not None:
            return b["seq"]
    return None


def _block_event_type(actor: str, hint: str, tool_calls: Any = None) -> str:
    """消息块 → 事件 type（裁定 C1）。

    - human→user_message／assistant→assistant_text（消息类，唯一映射）；
    - **带 tool_calls 的 assistant 块 → 空串＝不落事件**（[工具结果落会话
      2026-10-17]：content="" 的消息事件是噪音；过程通道已有 tool_call 事件）；
    - ``card:`` 卡间信→msg_ask/msg_answer/msg_notify，**type 由发信方显式
      给定**（落信处知道消息类型），缺省按 notify 兜底——代码不猜语义；
    - 其余（actor=tool 的工具结果块／环节留痕块）→ 空串＝不落事件：过程侧已有
      tool_call/tool_result/plan_created 等事件，重复落一条只是双份噪音。
    """
    if actor == ACTOR_HUMAN:
        return "user_message"
    if actor == ACTOR_ASSISTANT:
        if tool_calls:
            return ""   # 工具调用块：过程通道已有 tool_call，不发消息事件
        return "assistant_text"
    if actor.startswith("card:"):
        return hint or "msg_notify"
    return ""


async def emit_message_event(work_id: str, card_id: str, ev_type: str,
                             content: str, run_id: str = "",
                             round_: int = 0,
                             model: str = "", end: str = "",
                             source: Any = None,
                             from_card: str = "",
                             msg_id: str = "",
                             reply_to: str = "",
                             extra: dict[str, Any] | None = None) -> None:
    """消息类事件落 events jsonl（**单一落点**）。

    [卡片底座第一步 2026-10-14] 卡间信不再写会话块（改落待处理收件箱），但其
    **事件流留痕照旧**——故把这条写路径抽成公开函数：会话块路径（``append`` →
    ``_emit_block_event``）与卡间发信路径（``card_tools._h_send_card_message``）
    共用它，避免两处各写一套形状。data 是结构化对象（``{"content": ...}``），
    不是文本 blob。零阻断（事件日志失败不影响调用方主流程）。
    [会话渲染单源化·偏差③补 2026-10-15] data 增 model/end（assistant_text 的
    元信息徽标数据源——/session 渲染通道退役后 timeline 是唯一数据源，不带
    这两字段则徽标无源可渲染）。user_message/card 事件两字段空串不落键。

    [harness 第 1 步 2026-10-28 · 消息零件化] 消息类事件补 `from`/`to`/`msg_type`/
    `msg_id`/`reply_to` 五个零件。
    改前的病根是**同一次发信被劈成两条事件**：``msg_*`` 有正文没发件人，
    ``card_message`` 有发件人没正文，且 ``msg_*`` 侧连 ``msg_id`` 都没有 ⇒
    两条事后 join 不起来，"谁给谁发了什么类型"这个问题无法回答。
    现在一条事件带全五样，条件面才立得住（catalog 的 fields.part 已声明）。
    """
    if not ev_type:
        return
    try:
        data: dict[str, Any] = {"content": content}
        # [2026-10-26 · 记录补全] 消息事件带**发信方**。改前只有 content，
        # 判据无法回答「这条是谁发来的」——「父卡派发的任务」这类判断因此不可做。
        if from_card:
            data["from"] = str(from_card)
        if model:
            data["model"] = str(model)
        if end:
            data["end"] = str(end)
        # [harness 第 1 步] 卡间消息的收件方与类型：`to` 就是本条事件所在的卡
        # （消息落在收件方自己的键上）；`msg_type` 直接取事件型，两者同源不另算。
        if str(ev_type).startswith("msg_") or str(ev_type) in ("coder_done", "worker_done"):
            data["to"] = str(card_id or "")
            data["msg_type"] = str(ev_type)
            if msg_id:
                data["msg_id"] = str(msg_id)
            if reply_to:
                data["reply_to"] = str(reply_to)
        # [goal 域 2026-10-20 · 照抄 dsh] 回合来源。续轮消息带
        # {"kind":"goal","goalId","revision","round"}——这是 goal fold 里
        # roundsStarted 的**唯一**推进依据（原版 GoalMessageSource）。
        if source:
            data["source"] = dict(source)
        # [harness 第 1 步 2026-10-28] 调用方追加的零件（如汇报的 kind）。
        # 放在最后合并：调用方给的是**该消息类型的专属事实**，不覆盖上面的公共零件。
        if extra:
            for _k, _v in dict(extra).items():
                if _k not in data:
                    data[_k] = _v
        await event_log.append(work_id, card_id, {
            "run_id": str(run_id or ""),
            "round": int(round_ or 0),
            "type": ev_type,
            "data": data,
        })
        # [2026-11-07] assistant_text 补一条 journal-only 帧进直播流。
        # 病根：本函数只落盘（event_log.append）不进内存 journal ⇒ SSE 回放
        # 里 assistant_text 一帧都没有 ⇒ 直播态「中间发言全没了」、只能刷新补。
        # journal_only=True 防双写（落盘就是上面的 append）；anchor 用实参
        # run_id/round——读 _anchors 当前锚是错的（user 消息先于 run_start、
        # 卡间消息落他卡键）。前端按 stream:{run}:{round} 键与落盘记录同键
        # 去重，直播帧与刷新后回放合成同一节点，不双份。
        if ev_type == "assistant_text":
            try:
                from ..api import event_stream as _es
                _es.emit(work_id, card_id, ev_type, data,
                         anchor=(str(run_id or ""), int(round_ or 0)),
                         journal_only=True)
            except Exception:  # noqa: BLE001 展示层零阻断
                pass
    except Exception as exc:  # noqa: BLE001 事件日志零阻断
        logger.warning("[facade] 消息事件落盘失败（不阻断）card=%s type=%s: %s",
                       card_id, ev_type, exc)


async def _emit_block_event(work_id: str, card_id: str,
                            blk: dict[str, Any], hint: str,
                            event_content: str | None = None,
                            origin: dict[str, Any] | None = None) -> None:
    """[消息底座能力对齐 · 件2] 消息块同步落 events jsonl（裁定 C1/C5）。

    落盘位置＝append 成功后、ops 打点之前：会话文件与事件流同一次写路径内
    完成，不新增第二次落盘点。形状由 ``emit_message_event`` 承载（单一来源）。

    [5715] ``event_content`` 非 None 时用它作为**事件流**里的内容（会话块 content 不变），
    None ⇒ 用块内容（既有行为，逐字不变）。

    [harness 第 1 步 2026-10-28 · 修发件人丢失] ``origin`` ＝**入参原块**
    （``_assign_block`` 之前的那份）。
    病根：``_assign_block`` 的输出是按白名单重建的新 dict，只把 ``block["from"]``
    映射进 ``out["from"]``，而 ``append_card_message`` 传进来的键叫 ``from_card``
    ⇒ **发件人在这一层就掉了**；本函数再去读 ``blk.get("from_card")`` 自然恒空
    ⇒ ``data["from"]`` 从来没写出来过（全库 88 条 msg_directive 无一条带 from）。
    修法：入参原块随调用方一起传进来，发件人/消息 id/reply_to 从**原块**取，
    不再指望白名单输出里还留着它们（会话文件的块形状因此零变化）。
    """
    ev_type = _block_event_type(str(blk.get("actor") or ""), hint, blk.get("tool_calls"))
    content = blk.get("content", "") if event_content is None else event_content
    src = origin if isinstance(origin, dict) else {}
    _from = str(src.get("from_card") or src.get("from") or "")
    _extra = src.get("_extra")
    await emit_message_event(
        work_id, card_id, ev_type, content,
        run_id=str(blk.get("pair_id") or ""), round_=int(blk.get("turn") or 0),
        model=str(blk.get("model") or ""), end=str(blk.get("end") or ""),
        source=blk.get("source"),
        from_card=_from,
        msg_id=str(src.get("msg_id") or ""),
        reply_to=str(src.get("reply_to") or ""),
        extra=_extra if isinstance(_extra, dict) else None,
    )


def _assign_block(existing: list[dict[str, Any]], block: dict[str, Any]) -> dict[str, Any]:
    """按当前已落块序列分配 seq/turn/pair/end，返回待落盘的完整块（纯函数、不落盘）。

    [工具结果落会话 2026-10-17] 从 append 抽出，供 append／append_many 共用：
    批量落盘时逐块在"虚拟已落序列"上分配，结果与逐次 append 完全一致。

    - seq＝len(existing)（0 起；damaged 占位计入——位次恒等）；
    - turn＝末块有 end 则 +1，否则同轮（工具块不闭合轮 ⇒ 与同轮回复块同 turn）；
    - end 自动补：actor=assistant 且**不带 tool_calls** → model_done；
      带 tool_calls 的 assistant 块不补（结构上不是轮末，T024/T061 语义延伸；
      若补会把工具块算进下一 turn、工具块与用户不在同一轮）；
    - pair：human 缺省自身 seq（T057）；其余取本轮最近 human 块 seq。
    """
    actor = block.get("actor")
    if not actor:
        role = block.get("role", "")
        actor = {"user": ACTOR_HUMAN, "assistant": ACTOR_ASSISTANT}.get(role, role or "unknown")
    tool_calls = block.get("tool_calls") or None
    end = block.get("end") or ""
    if end and end not in END_VALUES:
        raise ValueError(f"非法 end 值: {end!r}（允许：{END_VALUES}）")
    if not end and actor == ACTOR_ASSISTANT and not tool_calls:
        end = "model_done"  # 闭合轮界（T024/T061）——带 tool_calls 的块不补
    seq = len(existing)  # 0 起；damaged 占位计入（不吞并 → 位次恒等）
    if existing:
        last = existing[-1]
        last_turn = last.get("turn")
        last_turn = last_turn if isinstance(last_turn, int) else 0
        turn = last_turn + (1 if last.get("end") else 0)
    else:
        turn = 0
    if actor == ACTOR_HUMAN:
        pair: Any = str(seq)  # human 块缺省自身（T057）
    else:
        init_seq = _last_human_in_turn(existing, turn)
        pair = block.get("pair") or (str(init_seq) if init_seq is not None else "unknown")
    now = session_store._now_iso()
    out = {
        # [卡间消息身份 2026-10-28 · 用户令「修」] `card:<发信卡>` **不是合法 role**
        # ——端点只认 system/user/assistant/tool。原来 `.get(actor, actor)` 把 actor
        # 原样当 role 存，于是库里出现 `role: "card:fn_005"`；读文件那条路
        # （session_facade:366）有 `card:` → user 的改写，读库那条（origin_store.
        # read_blocks）没有 ⇒ 块一进请求就 400（「重试 3 次后仍失败」）。
        # 修在**写入**这一处：卡间来信是「外部输入」，语义就是 user；「是谁发的」
        # 由 actor 承载，不由 role。存对了，读取就不用每次猜。
        "role": ("user" if str(actor).startswith("card:")
                 else {"human": "user", "assistant": "assistant"}.get(actor, actor)),
        "content": block.get("content", ""),
        "timestamp": now,
        "model": block.get("model"),
        "pair_id": block.get("pair_id", "") or str(pair),  # V1 uuid（S6b 衔接桥）
        "seq": seq,
        "actor": actor,
        "from": block.get("from", ""),
        "turn": turn,
        "pair": str(pair),
        "end": end,
        "behavior": block.get("behavior", ""),
        "tool_calls": tool_calls,
        "tool_call_id": block.get("tool_call_id", "") or "",
        "reasoning_content": block.get("reasoning_content") or "",
        "ts": now,
        "format": "v2",
    }
    # [goal 域 2026-10-20 · 照抄 dsh] 回合来源要跟着块走，否则 _emit_block_event
    # 拿不到它，goal fold 也就无法把这一轮记进 roundsStarted（原版
    # GoalMessageSource 随 user/message 事件一起落盘）。
    if block.get("source"):
        out["source"] = dict(block["source"])
    return out


async def _read_existing_locked(work_id: str, card_id: str,
                                path: Path) -> tuple[str, int, list[dict[str, Any]]]:
    """锁内读现状：返回 (原文, 格式号, 已落块列表)。V1 委托旧解析并补派生字段。"""
    if path.exists():
        # [读路径锁重试 2026-10-20] 原来这里裸 `aiofiles.open`——撞上 Windows 文件锁
        # 直接抛 Errno 13，整轮被按「模型调用失败」收口。与写路径同一套重试。
        text = await session_store.aread_text_with_lock_retry(path, "facade-read")
    else:
        text = ""
    fmt = _detect_format(text)
    if fmt == 2:
        # 〔2026-11-04〕与 _read_parsed 同因挪线程池：append 每块都要走这里，
        # 大会话的同步全量解析会把事件循环掐死在**会话锁内**（比读路径更糟）。
        existing = (await asyncio.to_thread(_parse_v2, text))["blocks"]
    else:
        existing = _enrich_v1(await asyncio.to_thread(_read_v1_sync, work_id, card_id))
    return text, fmt, existing


def _write_new_blocks_locked(work_id: str, card_id: str, path: Path,
                             text: str, fmt: int,
                             existing: list[dict[str, Any]],
                             new_blks: list[dict[str, Any]]) -> None:
    """锁内落盘新块（首建／追加／现场升格三分支，与旧 append 逐字同逻辑）。

    批量与单块共用：new_blks 是一个工具轮的 A＋N×B 时，写盘一次完成（原子）。
    """
    block_text = "".join(_render_v2_block(b) for b in new_blks)
    if not path.exists():
        _atomic_write_text(path, _render_v2_header(_default_header(card_id)) + block_text)
    elif fmt == 2:
        # [S3/T078] 追加路径：锁内 write＋flush＋os.fsync——崩溃时最坏丢尾部、
        # 不产生半块被吞。同步微写（追加体量小、锁内执行）换确定性 fsync。
        with _open_with_lock_retry(path, "a", "session-append") as f:
            f.write(block_text)
            f.flush()
            os.fsync(f.fileno())
    else:
        # 现场升格（T113）：V1→V2 全文重写（tmp→os.replace）→ 再追加
        header = _default_header(card_id)
        conv = _convert_v1_blocks(existing)
        full_text = _render_v2_header(header) + "".join(_render_v2_block(b) for b in conv) + block_text
        _atomic_write_text(path, full_text)
        # [T114] 升格留痕：R6 通道首笔真实消费＋全日志打点（T152 升格点位）
        session_ops.ops_log("upgrade", work_id, card_id,
                            {"from": "v1", "to": "v2", "blocks": len(conv) + len(new_blks)})
        revision_store.record_revision(work_id, "session", "v1_to_v2",
                                       {"card": card_id, "blocks": len(conv)})
        logger.info("[facade] 现场升格 V1→V2 work=%s card=%s blocks=%d（含新块）",
                    work_id, card_id, len(conv) + len(new_blks))

    # [原文入库 2026-10-20] md 落盘成功后，把**同一批块**写进原文表（双写）。
    # 追溯链：记忆条目 seq_from/seq_to → origin_store.trace_entry → 这里的行。
    # md 仍是写路径的真相源；库是第二份可查副本，写失败只记日志（待 backfill 补）。
    origin_store.write_blocks_sync(work_id, card_id, new_blks)


async def append(work_id: str, card_id: str, block: dict[str, Any],
                 event_content: str | None = None,
                 emit_event: bool = True) -> dict[str, Any]:
    """追加一块，返回落盘后的完整块（T024）。

    门面内自动分配 seq（写入时分配落盘，T053）、turn、pair、闭合轮界
    （assistant 缺省 end=model_done，显式传入优先；**带 tool_calls 的 assistant
    块不补 end**——见 _assign_block）。块入参：
    {content, actor|role, model?, end?, from?, behavior?, pair?, tool_calls?,
    tool_call_id?}。V1 文件先现场升格（4.8：转换→tmp→os.replace→再追加，
    块数不变）。与旧写路径共用 session_store 字节锁（切换过渡期双写方互斥）。

    [5715] ``event_content``：**事件流里这条消息的内容覆盖**（会话块 content 不变）。
    用于「会话块给模型看的内容」与「事件流给界面看的内容」不同的场景
    （如：各轮正文已按轮单独落事件，收口那条就不该再落一遍拼接全文）。
    它是**函数参数不是块字段** ⇒ 不进会话文件、不进块头白名单、不影响模型上下文。
    """
    # [消息底座能力对齐 · 件2] 卡间信的 msg_* 类型由调用方显式给定（瞬态提示，
    # 不进 V2 块头白名单、不落会话文件）；其余块忽略。
    event_type_hint = str(block.get("event_type") or "")
    path = _session_path(work_id, card_id)
    lock = session_store._session_lock(work_id, card_id)
    async with lock:
        text, fmt, existing = await _read_existing_locked(work_id, card_id, path)
        new_blk = _assign_block(existing, block)
        _write_new_blocks_locked(work_id, card_id, path, text, fmt, existing, [new_blk])
    # [消息底座能力对齐 · 件2] 消息块同步落 events jsonl（会话落盘后同路径完成）
    if emit_event:
        await _emit_block_event(work_id, card_id, new_blk, event_type_hint,
                                event_content, origin=block)
    # [T152] append 点位：work/card/seq/turn/actor/结果
    session_ops.ops_log("append", work_id, card_id,
                        {"seq": new_blk["seq"], "turn": new_blk["turn"],
                         "actor": new_blk["actor"], "result": "ok"})
    return new_blk


async def append_card_message(work_id: str, card_id: str, *,
                              from_card: str, text: str,
                              msg_type: str = "msg_notify",
                              event_type: str = "",
                              reply_to: str = "",
                              extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """把一条**卡间消息**落成会话块（不进收件箱、不唤醒）。

    [2026-10-26 · 用户裁定] 同步/通知类消息不该走 inbox，也不该只在投递当轮可见：

    - ``inbox`` 是**水位消费**（``take_pending`` 读一次就推进水位）⇒ 模型看一次
      就永远查不到了，而且空闲卡根本不会去查；
    - **工具结果只保留当轮** ⇒ 塞进 tool_result 更是看一眼就没。

    落成会话块则相反：它是**历史的一部分**，下次任何回合 ``prior_messages``
    都带着它——**不需要唤醒**也能被看到（上下文机制本来就支持）。

    口吻：第二人称、明确标出处（「卡 card_067（《标题》）给你发来一条通知」），
    模型能分清这不是用户本人说的话，不会误当指令来源。
    """
    title = from_card
    try:
        from . import card_store
        meta = (await card_store.read_card(work_id, from_card)).get("meta") or {}
        title = str(meta.get("title") or from_card)
    except Exception:  # noqa: BLE001 取不到标题不影响送达
        pass
    label = {"msg_notify": "通知", "msg_sync": "同步", "msg_report": "汇报",
             "msg_ask": "询问", "msg_answer": "应答",
             "msg_directive": "指令", "coder_done": "完工回执", "worker_done": "完工回执"}.get(msg_type, "消息")
    # [2026-10-26 · 用户令「上下文里用用户消息塞，但不要污染客户端样式」]
    # 必须用 **`card:` actor**，不能用 role="user"：
    #   · ``_block_event_type`` 对 actor=human **直接返回 user_message、忽略
    #     event_type** ⇒ 事件流里全是用户消息，前端按用户气泡渲染（分不清哪句
    #     是用户说的）；
    #   · actor=`card:<发信卡>` ⇒ 事件类型是 hint（msg_sync/msg_directive…），
    #     前端 ``routes.py:748`` 专门认这个前缀、按消息块渲染；
    #   · 上下文装配（``session_facade`` 第 366 行）把 `card:` 映射成 role=user
    #     ⇒ **模型侧照样当外部输入看得到**。
    # 正文首行带结构化头（build_header），前端据此取 id/type/from。
    from ..api import card_msg
    msg_id = f"cmsg-{uuid.uuid4().hex[:12]}"
    header = card_msg.build_header(msg_id, msg_type, str(from_card or ""),
                                   reply_to)
    if str(from_card) in ("system", "", "harness"):
        body = f"系统给你发来一条{label}：\n{text}"
    else:
        body = f"卡 {from_card}（《{title}》）给你发来一条{label}：\n{text}"
    rec = await append(work_id, card_id, {
        "actor": f"card:{from_card}" if from_card else "card:system",
        "content": header + "\n" + body,
        "event_type": event_type or msg_type,
        "from_card": str(from_card or ""),
        # [harness 第 1 步 2026-10-28] msg_id 一并带进入参块：事件流要拿它做
        # 零件（"这条是对哪条的回话"），否则两条消息事后无法配对。
        # 它只活在入参块里，_assign_block 的白名单不会把它写进会话文件。
        "msg_id": msg_id,
        # 调用方追加的零件（如汇报的 kind）。同样只活在入参块，不进会话文件。
        **({"_extra": dict(extra)} if extra else {}),
        **({"reply_to": reply_to} if reply_to else {}),
    })
    # [2026-10-27] 返回里带上 msg_id：调用方（发信工具）要把它交给模型做 reply_to。
    out = dict(rec or {})
    out["msg_id"] = msg_id
    return out


async def append_many(work_id: str, card_id: str,
                      blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """**批量追加（单次锁、单次写、原子）**——一个工具轮的 A＋N×B 一次落盘。

    [工具结果落会话 2026-10-17] N1 红线（不得出现孤立 tool 块）的原子性保证：
    A（assistant+tool_calls）与全部 B（tool 结果）在同一把锁、同一次写入内完成，
    读者要么看到全无、要么看到成对齐全，不存在"A 已落、B 未落"的中间态——
    该中间态会让端点 400（role=tool 必须紧跟其配对的 assistant(tool_calls)）。

    与 append 共用 _assign_block：逐块在"虚拟已落序列"上分配 seq/turn/pair，
    结果与逐次 append 完全一致。返回落盘后的完整块列表（与入参等长、同序）。
    """
    if not blocks:
        return []
    path = _session_path(work_id, card_id)
    lock = session_store._session_lock(work_id, card_id)
    async with lock:
        text, fmt, existing = await _read_existing_locked(work_id, card_id, path)
        working = list(existing)
        new_blks: list[dict[str, Any]] = []
        for b in blocks:
            nb = _assign_block(working, b)
            working.append(nb)
            new_blks.append(nb)
        _write_new_blocks_locked(work_id, card_id, path, text, fmt, existing, new_blks)
    # 事件流：工具块经 _block_event_type 一律返回空串（不发消息事件）
    # [harness 第 1 步] 逐块带**入参原块**（origin）——发件人/消息 id 只在原块里，
    # 白名单输出（_assign_block）已把它们丢掉。
    for nb, _src in zip(new_blks, blocks):
        await _emit_block_event(work_id, card_id, nb, "", origin=_src)
    for nb in new_blks:
        session_ops.ops_log("append", work_id, card_id,
                            {"seq": nb["seq"], "turn": nb["turn"],
                             "actor": nb["actor"], "result": "ok"})
    return new_blks


# ---------------------------------------------------------------- 尾部快路径（T025/T073）
def scan_tail(work_id: str, card_id: str) -> dict[str, Any]:
    """末块 ts＋seq（导航活跃度消费，替代 last_ts 的门面入口）。

    V1：委托 session_store.last_ts（尾部反向扫描语义保留）；V1 无落盘 seq，
    seq 置 None（消费方仅用 ts，与现状语义一致）。
    V2：尾部反向扫描（CRLF 归一化自 :200-202 吸收），ts/seq 自末块头直读；
    末块头残缺时向前找上一完整块头。
    """
    path = _session_path(work_id, card_id)
    if not path.exists():
        return {"ts": "", "seq": None, "format": None}
    try:
        with open(path, "rb") as f:
            head = f.read(1024).decode("utf-8", errors="replace").replace("\r\n", "\n")
            first_line = head.split("\n", 1)[0]
            if first_line != HEAD_MARKER:
                return {"ts": session_store.last_ts(work_id, card_id), "seq": None, "format": "v1"}
            f.seek(0, 2)
            size = f.tell()
            window = 65536
            while True:
                read_len = min(size, window)
                f.seek(size - read_len)
                chunk = f.read(read_len).decode("utf-8", errors="replace").replace("\r\n", "\n")
                if read_len < size:
                    nl = chunk.find("\n")
                    if nl < 0:
                        if window >= size:
                            return {"ts": "", "seq": None, "format": "v2"}
                        window *= 4
                        continue
                    chunk = chunk[nl + 1:]
                # 自后向前找最近的「块头完整（含 ts 行）」的块
                search_end = len(chunk)
                while True:
                    idx = chunk.rfind("\n" + BLOCK_MARKER + "\n", 0, search_end)
                    if idx < 0 and chunk.startswith(BLOCK_MARKER + "\n"):
                        idx = -1
                        body_start = len(BLOCK_MARKER) + 1
                    elif idx >= 0:
                        body_start = idx + 1 + len(BLOCK_MARKER) + 1
                    else:
                        break
                    ts, seq = "", None
                    for line in chunk[body_start:].split("\n"):
                        if line == BLOCK_BODY_SEP:
                            break
                        if line.startswith("ts: "):
                            ts = line[len("ts: "):].strip()
                        elif line.startswith("seq: "):
                            try:
                                seq = int(line[len("seq: "):].strip())
                            except ValueError:
                                seq = None
                    if ts or seq is not None:
                        return {"ts": ts, "seq": seq, "format": "v2"}
                    if idx < 0:
                        break
                    search_end = idx  # 末块头残缺 → 向前找上一块
                if window >= size:
                    return {"ts": "", "seq": None, "format": "v2"}
                window *= 4
    except OSError as exc:
        logger.warning("scan_tail 尾部扫描失败 work=%s card=%s: %s", work_id, card_id, exc)
        return {"ts": "", "seq": None, "format": None}


#: ``last_message`` 的尾扫上限——窗口到此还没找到对话块就放弃。
#: 会话块可以很大（工具帧动辄几十 KB），但一张卡 8MB 的尾部全是工具帧时，
#: "最后一句人话"早已没有时效性，按 None 处理比全文读划算。
_LAST_MSG_START = 256 * 1024
_LAST_MSG_CAP = 8 * 1024 * 1024


def last_message(work_id: str, card_id: str,
                 text_limit: int = 220) -> dict[str, str] | None:
    """这张卡会话里的**最后一条对话消息**（谁说的、说了什么、什么时候）。

    尾部扩窗反扫（机制同 ``scan_tail``）：256KB 起步、×4 扩到 8MB 封顶，
    只解析窗口内的块——**不读整个会话文件**（投影场景的会话可以到几十 MB，
    全量读+全量解析是画布重建的主要成本来源）。

    过滤口径与画布原口径一致：跳过工具帧（内容是参数 JSON）与空消息，
    只看 ``human``/``assistant``。窗口封顶内没找到 ⇒ ``None``。
    """
    path = _session_path(work_id, card_id)
    try:
        if not path.exists():
            return None
        size = path.stat().st_size
        if size <= 0:
            return None
        window = _LAST_MSG_START
        with open(path, "rb") as f:
            while True:
                read_len = min(size, window)
                f.seek(size - read_len)
                chunk = f.read(read_len).decode(
                    "utf-8", errors="replace").replace("\r\n", "\n")
                if read_len < size:
                    # 切掉第一行残行——窗口起点可能落在行中间。
                    nl = chunk.find("\n")
                    if nl < 0:
                        if window >= size or window >= _LAST_MSG_CAP:
                            return None
                        window *= 4
                        continue
                    chunk = chunk[nl + 1:]
                # 残缺块头由 _parse_v2 吸收：非 ---MSG--- 行一律跳过，
                # 窗口里找到的块都是完整块（结构行在正文里有转义，不会误中）。
                for b in reversed(_parse_v2(chunk)["blocks"]):
                    actor = str(b.get("actor") or "")
                    content = str(b.get("content") or "").strip()
                    if not content or actor not in (ACTOR_HUMAN, ACTOR_ASSISTANT):
                        continue
                    if content.startswith("{") and content.rstrip().endswith("}"):
                        continue          # 参数 JSON，不是人话
                    return {
                        "actor": actor,
                        "text": content[:text_limit],
                        "ts": str(b.get("ts") or ""),
                        "seq": str(b.get("seq") if b.get("seq") is not None else ""),
                    }
                if window >= size or window >= _LAST_MSG_CAP:
                    return None
                window *= 4
    except Exception as exc:  # noqa: BLE001 投影失败不阻断
        logger.warning("last_message 尾扫失败 work=%s card=%s: %s",
                       work_id, card_id, exc)
        return None


# ---------------------------------------------------------------- 投影（T026/T074-T076）
def rebuild(work_id: str) -> dict[str, Any]:
    """从真相（各卡会话文件）全量重建投影并落盘（T075）。

    [卡片存储迁移 SQLite 2026-10-14] 卡清单数据源从 ``cards/*.md`` 改为库内
    ``cards`` 表（会话本身仍是文件——真相没变，只是「有哪些卡」改查库）。
    """
    from . import card_db

    def _fn(conn: Any) -> list[str]:
        return [r["card_id"] for r in conn.execute("SELECT card_id FROM cards ORDER BY card_id")]

    items: list[dict[str, Any]] = []
    for cid in card_db.read_sync(work_id, _fn, []) or []:
        st = scan_tail(work_id, cid)
        items.append({"card": cid, "ts": st["ts"], "seq": st["seq"]})
    proj = {"derived": True, "generated_at": session_store._now_iso(), "items": items}
    _atomic_write_text(_projection_path(work_id),
                       json.dumps(proj, ensure_ascii=False, indent=2) + "\n")
    return proj


def project(work_id: str, filter: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """派生视图（导航列表消费）。投影文件缺失/损坏→自动 rebuild（真相可弃投影）。

    filter：对 items 的键值精确匹配（如 {"card": "card_003"}）。
    写路径（append/scan_tail）永不调用本函数（T076，测试故障注入钉死）。
    """
    path = _projection_path(work_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("derived") is True:
                if filter:
                    data = {**data,
                            "items": [it for it in data.get("items", [])
                                      if all(it.get(k) == v for k, v in filter.items())]}
                return data
        except Exception as exc:  # noqa: BLE001 投影可丢弃——真相在会话文件
            logger.warning("投影读取失败，触发重建 work=%s: %s", work_id, exc)
    return rebuild(work_id)


# ---------------------------------------------------------------- 建卡铺会话事务（T027/B-27）
async def _rollback_create(work_id: str, card_id: str, session_path: Path) -> None:
    """事务回滚：刚建的卡与会话**移入 damaged/ 留档**（移动留档，不删除任何
    字节——card_store.py 铁律「不允许删除 cards\\ 或 sessions\\ 下文件」的
    精神延伸；不留半事务产物）。

    [卡片存储迁移 SQLite 2026-10-14] 卡片进库后不再是 md 文件：
    留档改为**导出卡内容为 JSON** 存进 damaged/（字节不丢），随后删除库行
    （卡＋影子），等价于旧「把卡文件 rename 进 damaged/」。会话仍为文件，照旧 rename。
    """
    from . import card_db

    d = _damaged_dir(work_id)
    try:
        d.mkdir(parents=True, exist_ok=True)
        stamp = session_store._now_iso().replace(":", "").replace("-", "")
        try:
            card = await card_store.read_card(work_id, card_id)
            (d / f"{card_id}.json.orphan_{stamp}").write_text(
                json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            def _fn(conn: Any) -> None:
                card_db.delete_card_row(conn, card_id)
                card_db.delete_shadow_row(conn, card_id)
                card_db.bump_rev(conn)

            await card_db.run_write(work_id, _fn)
            logger.warning("[facade] 事务回滚：卡留档并撤库行 work=%s card=%s", work_id, card_id)
        except Exception as exc:  # noqa: BLE001 卡侧回滚失败不掩盖原始异常
            logger.error("[facade] 事务回滚：卡撤库失败 work=%s card=%s: %s", work_id, card_id, exc)
        if session_path.exists():
            session_path.rename(d / f"{session_path.name}.orphan_{stamp}")
            logger.warning("[facade] 事务回滚：会话移入 damaged/ work=%s card=%s", work_id, card_id)
    except Exception as exc:  # noqa: BLE001 回滚本身失败不掩盖原始异常
        logger.error("[facade] 事务回滚移动失败 work=%s card=%s: %s", work_id, card_id, exc)


async def create_with_session(work_id: str, title: str, source: str, *,
                              card_type: str = "", parent: Optional[str] = None,
                              charter: str = "", conclusion: str = "",
                              identity: Optional[dict] = None) -> dict[str, Any]:
    """建卡铺会话事务（B-27）：card_store.create_card（建卡＋铺会话两步）＋
    V2 头落位。失败整体回滚、不留孤儿会话文件（T077）。

    - 建卡失败（create_card 返回 error/抛错）→ 原样返回/抛出；会话侧未动，
      无孤儿。
    - V2 头落位失败 → 刚建的卡与会话移入 damaged/ 留档后抛出——cards/ 下
      不落盘半事务产物（验收判据：会话创建失败→卡片不落盘）。
    新会话文件以 V2 头替代 create_card 的占位注释行（创建时间已在卡面
    created_at，不重复承载）。
    """
    result = await card_store.create_card(
        work_id, title, source, card_type=card_type, parent=parent,
        charter=charter, conclusion=conclusion,
    )
    if not isinstance(result, dict) or "error" in result:
        return result  # 建卡失败：会话未铺，无孤儿（T077 上半）
    card_id = result["meta"]["card_id"]
    spath = _session_path(work_id, card_id)
    try:
        existing = spath.read_text(encoding="utf-8") if spath.exists() else ""
        if BLOCK_MARKER in existing:
            raise RuntimeError(
                f"create_with_session: 新建卡会话文件已含会话块（异常状态）work={work_id} card={card_id}")
        _atomic_write_text(spath, _render_v2_header(_default_header(card_id, identity)))
    except Exception:
        await _rollback_create(work_id, card_id, spath)
        raise
    return result


# ============================================================
# 逻辑互斥体（S4 / 4.4，T083-T086）——主线回合与凝练支线同一登记处
# ============================================================

# 在途登记：(work, card) → owner（"turn:xxxx"＝主线回合；"condense"＝凝练支线）。
# 纯内存 dict、无 await 区间内读写（事件循环内原子）；字节锁（session_store._session_lock）
# 保留为门面内部实现细节（T083）。旧防护（重发去重、写锁语义）不动不删（T087），
# 退役时点＝生产生效时刻。
_active_turns: dict[tuple[str, str], str] = {}


def turn_try_acquire(work_id: str, card_id: str, owner: str) -> bool:
    """登记在途；已被占（任何 owner）→ False。主线第二回合到达即拒（T084，
    10-08 口径「同卡 409 不排队」，排队策略归迭代二 T085）。"""
    key = (work_id, card_id)
    if key in _active_turns:
        return False
    _active_turns[key] = owner
    return True


def turn_release(work_id: str, card_id: str, owner: str) -> None:
    key = (work_id, card_id)
    if _active_turns.get(key) == owner:
        del _active_turns[key]


def turn_holder(work_id: str, card_id: str) -> str:
    """当前在途 owner（诊断/幂等判定用）；无在途返回空串。"""
    return _active_turns.get((work_id, card_id), "")


# ============================================================
# 打开体检与损坏处置（S3 / 4.3，T080-T082）
# ============================================================

def health_check_sync(work_id: str, card_id: str) -> dict[str, Any]:
    """打开体检（T080）：头存在性／版本可识别／块闭合性／seq 单调性。

    同步版（读文件＋解析，无 IO 并发需求）。实做 O(文件全解析)——单文件
    ≤86KB 量级毫秒级，比「尾部＋抽样」口径更严（全量口径下无漏检）。
    体检报告进全日志打点（T082）。V1 文件：无 V2 头概念，header/version
    两项按不适用（None）报告；V1 宽松解析无 damaged 概念。
    """
    path = _session_path(work_id, card_id)
    if not path.exists():
        report: dict[str, Any] = {"exists": False, "format": None}
        session_ops.ops_log("health_check", work_id, card_id, report)
        return report
    text = path.read_text(encoding="utf-8")
    fmt = _detect_format(text)
    report = {"exists": True, "format": fmt}
    if fmt == 2:
        parsed = _parse_v2(text)
        header = parsed["header"]
        blocks = parsed["blocks"]
        report["header_ok"] = header.get("version") == 2
        report["version_ok"] = header.get("version") == 2
        damaged = [{"seq": b.get("seq"), "reason": b.get("damaged_reason", "")}
                   for b in blocks if b.get("damaged")]
        report["blocks"] = len(blocks)
        report["damaged_count"] = len(damaged)
        report["damaged"] = damaged
        seqs = [b["seq"] for b in blocks if b.get("seq") is not None]
        report["seq_monotonic"] = all(b - a == 1 for a, b in zip(seqs, seqs[1:]))
        report["unclosed"] = sum(1 for b in blocks
                                 if b.get("damaged_reason") in ("unclosed", "unclosed_header"))
    else:
        report["header_ok"] = None      # V1 无头概念（不适用）
        report["version_ok"] = None
        report["blocks"] = None
        report["damaged_count"] = 0
        report["damaged"] = []
        report["seq_monotonic"] = None  # V1 seq 读时现算，恒单调
        report["unclosed"] = 0
    session_ops.ops_log("health_check", work_id, card_id, report)
    return report


async def health_check(work_id: str, card_id: str) -> dict[str, Any]:
    """异步壳（与门面 IO 风格一致；实现见 health_check_sync）。"""
    return health_check_sync(work_id, card_id)


def repair_damaged_tail(work_id: str, card_id: str) -> dict[str, Any]:
    """损坏处置三步（T081）：检出→残块旁路 damaged/ 留档（**永不删**，
    card_store.py:4 铁律精神延伸——移动留档非删除）→从末个完好块重建可用尾部。

    append-only 文件的写损伤只可能出现在尾部（中段损伤＝磁盘级异常，只报告
    不擅动）。V2 文件专属；V1 无 damaged 概念不处置。
    """
    report: dict[str, Any] = {"exists": False, "repaired": False, "fragments": []}
    path = _session_path(work_id, card_id)
    if not path.exists():
        session_ops.ops_log("damage_repair", work_id, card_id, report)
        return report
    text = path.read_text(encoding="utf-8")
    if _detect_format(text) != 2:
        report["format"] = 1
        session_ops.ops_log("damage_repair", work_id, card_id, report)
        return report
    report["exists"] = True
    parsed = _parse_v2(text)
    damaged = [b for b in parsed["blocks"] if b.get("damaged")]
    report["damaged_count"] = len(damaged)
    tail_unclosed = parsed["blocks"] and parsed["blocks"][-1].get("damaged") \
        and parsed["blocks"][-1].get("damaged_reason", "").startswith("unclosed")
    if not tail_unclosed:
        session_ops.ops_log("damage_repair", work_id, card_id, report)
        return report  # 无尾部未闭合：无可处置（中段 damaged 只报告）
    # 残块＝最后一个 ---END--- 之后的全部字节（含未闭合块起头）
    last_end = text.rfind(BLOCK_END_MARKER)
    if last_end < 0:
        report["note"] = "全文无闭合块：不自动处置，人工介入"
        session_ops.ops_log("damage_repair", work_id, card_id, report)
        return report
    cut = last_end + len(BLOCK_END_MARKER)
    fragment = text[cut:]
    # 旁路留档（永不删）
    d = _damaged_dir(work_id)
    d.mkdir(parents=True, exist_ok=True)
    stamp = session_store._now_iso().replace(":", "").replace("-", "").replace("+", "")
    frag_path = d / f"{card_id}.frag_{stamp}.md"
    with open(frag_path, "w", encoding="utf-8") as f:
        f.write(fragment)
        f.flush()
        os.fsync(f.fileno())
    report["fragments"].append(str(frag_path.name))
    # 重建可用尾部：截到末个完好块（tmp→os.replace，T079）
    rebuilt = text[:cut] + "\n"
    _atomic_write_text(path, rebuilt)
    report["repaired"] = True
    report["blocks_after"] = len(_parse_v2(rebuilt)["blocks"])
    session_ops.ops_log("damage_repair", work_id, card_id, report)
    logger.warning("[facade] 损坏处置：残块旁路 %s，尾部重建 blocks=%d work=%s card=%s",
                   frag_path.name, report["blocks_after"], work_id, card_id)
    return report
