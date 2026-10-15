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

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import aiofiles

from . import card_store, event_log, revision_store, session_ops, session_store

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
BLOCK_FIELDS = ("seq", "actor", "from", "turn", "pair", "model", "behavior", "end", "ts")
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
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    last_exc: Optional[OSError] = None
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_exc = exc
            # 只对 Windows 的「文件被占用」两码重试；其余（如跨卷）立即上抛
            if getattr(exc, "winerror", None) not in (5, 32) or attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1))
    if last_exc is not None:  # pragma: no cover - 循环内必 return 或 raise
        raise last_exc


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


def _parse_v2(text: str) -> dict[str, Any]:
    """V2 解析器。返回 {"header": dict, "blocks": [输出形态块]}。

    damaged 块以占位条目存在、不吞并（T117 承重语义）。
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
    legacy_pair_id = kv.get("pair_id", "")  # V1 uuid 经前向兼容通道保留（见模块头 S6b 衔接桥）
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


# ---------------------------------------------------------------- 读（T023）
async def read(work_id: str, card_id: str, *, from_seq: Optional[int] = None,
               to_seq: Optional[int] = None, actor: Optional[str] = None,
               turn: Optional[int] = None, limit: Optional[int] = None) -> list[dict[str, Any]]:
    """统一读入口。V1 委托旧解析＋补派生；V2 走 V2 解析器。

    damaged 占位条目（seq 缺失者）在任何 seq 过滤下都保留——位次对齐承重语义
    （T117）；显式 actor/turn 过滤按块字段正常生效。
    """
    path = _session_path(work_id, card_id)
    if not path.exists():
        return []
    async with aiofiles.open(path, mode="r", encoding="utf-8") as f:
        text = await f.read()
    if _detect_format(text) == 1:
        blocks = _enrich_v1(await session_store.read_session(work_id, card_id))
    else:
        blocks = _parse_v2(text)["blocks"]

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
    return _parse_v2(text)["header"]


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


def _block_event_type(actor: str, hint: str) -> str:
    """消息块 → 事件 type（裁定 C1）。

    - human→user_message／assistant→assistant_text（消息类，唯一映射）；
    - ``card:`` 卡间信→msg_ask/msg_answer/msg_notify，**type 由发信方显式
      给定**（落信处知道消息类型），缺省按 notify 兜底——代码不猜语义；
    - 其余（actor=tool 的环节留痕块）→ 空串＝不落事件：过程侧已有
      plan_created/step_advanced 等事件，重复落一条只是双份噪音。
    """
    if actor == ACTOR_HUMAN:
        return "user_message"
    if actor == ACTOR_ASSISTANT:
        return "assistant_text"
    if actor.startswith("card:"):
        return hint or "msg_notify"
    return ""


async def emit_message_event(work_id: str, card_id: str, ev_type: str,
                             content: str, run_id: str = "",
                             round_: int = 0,
                             model: str = "", end: str = "") -> None:
    """消息类事件落 events jsonl（**单一落点**）。

    [卡片底座第一步 2026-10-14] 卡间信不再写会话块（改落待处理收件箱），但其
    **事件流留痕照旧**——故把这条写路径抽成公开函数：会话块路径（``append`` →
    ``_emit_block_event``）与卡间发信路径（``card_tools._h_send_card_message``）
    共用它，避免两处各写一套形状。data 是结构化对象（``{"content": ...}``），
    不是文本 blob。零阻断（事件日志失败不影响调用方主流程）。
    [会话渲染单源化·偏差③补 2026-10-15] data 增 model/end（assistant_text 的
    元信息徽标数据源——/session 渲染通道退役后 timeline 是唯一数据源，不带
    这两字段则徽标无源可渲染）。user_message/card 事件两字段空串不落键。
    """
    if not ev_type:
        return
    try:
        data: dict[str, Any] = {"content": content}
        if model:
            data["model"] = str(model)
        if end:
            data["end"] = str(end)
        await event_log.append(work_id, card_id, {
            "run_id": str(run_id or ""),
            "round": int(round_ or 0),
            "type": ev_type,
            "data": data,
        })
    except Exception as exc:  # noqa: BLE001 事件日志零阻断
        logger.warning("[facade] 消息事件落盘失败（不阻断）card=%s type=%s: %s",
                       card_id, ev_type, exc)


async def _emit_block_event(work_id: str, card_id: str,
                            blk: dict[str, Any], hint: str) -> None:
    """[消息底座能力对齐 · 件2] 消息块同步落 events jsonl（裁定 C1/C5）。

    落盘位置＝append 成功后、ops 打点之前：会话文件与事件流同一次写路径内
    完成，不新增第二次落盘点。形状由 ``emit_message_event`` 承载（单一来源）。
    """
    ev_type = _block_event_type(str(blk.get("actor") or ""), hint)
    await emit_message_event(
        work_id, card_id, ev_type, blk.get("content", ""),
        run_id=str(blk.get("pair_id") or ""), round_=int(blk.get("turn") or 0),
        model=str(blk.get("model") or ""), end=str(blk.get("end") or ""),
    )


async def append(work_id: str, card_id: str, block: dict[str, Any]) -> dict[str, Any]:
    """追加一块，返回落盘后的完整块（T024）。

    门面内自动分配 seq（写入时分配落盘，T053）、turn、pair、闭合轮界
    （assistant 缺省 end=model_done，显式传入优先）。块入参：
    {content, actor|role, model?, end?, from?, behavior?, pair?}。
    V1 文件先现场升格（4.8：转换→tmp→os.replace→再追加，块数不变）。
    与旧写路径共用 session_store 字节锁（切换过渡期双写方互斥）。
    """
    actor = block.get("actor")
    if not actor:
        role = block.get("role", "")
        actor = {"user": ACTOR_HUMAN, "assistant": ACTOR_ASSISTANT}.get(role, role or "unknown")
    end = block.get("end") or ""
    if end and end not in END_VALUES:
        raise ValueError(f"非法 end 值: {end!r}（允许：{END_VALUES}）")
    if not end and actor == ACTOR_ASSISTANT:
        end = "model_done"  # 闭合轮界（T024/T061）
    content = block.get("content", "")
    model = block.get("model")
    pair_id_legacy = block.get("pair_id", "")  # V1 uuid（S6b 衔接桥：经前向兼容通道落盘）
    from_ = block.get("from", "")
    behavior = block.get("behavior", "")
    # [消息底座能力对齐 · 件2] 卡间信的 msg_* 类型由调用方显式给定（瞬态提示，
    # 不进 V2 块头白名单、不落会话文件）；其余块忽略。
    event_type_hint = str(block.get("event_type") or "")

    path = _session_path(work_id, card_id)
    lock = session_store._session_lock(work_id, card_id)
    async with lock:
        if path.exists():
            async with aiofiles.open(path, mode="r", encoding="utf-8") as f:
                text = await f.read()
        else:
            text = ""
        fmt = _detect_format(text)
        if fmt == 2:
            existing = _parse_v2(text)["blocks"]
        else:
            existing = _enrich_v1(await session_store.read_session(work_id, card_id))

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

        new_blk: dict[str, Any] = {
            "role": {"human": "user", "assistant": "assistant"}.get(actor, actor),
            "content": content,
            "timestamp": session_store._now_iso(),
            "model": model,
            "pair_id": pair_id_legacy or str(pair),
            "seq": seq,
            "actor": actor,
            "from": from_,
            "turn": turn,
            "pair": str(pair),
            "end": end,
            "behavior": behavior,
            "ts": session_store._now_iso(),
            "format": "v2",
        }
        block_text = _render_v2_block(new_blk)

        if not path.exists():
            _atomic_write_text(path, _render_v2_header(_default_header(card_id)) + block_text)
        elif fmt == 2:
            # [S3/T078] 追加路径：锁内 write＋flush＋os.fsync——崩溃时最坏丢尾部、
            # 不产生半块被吞。同步微写（追加体量小、锁内执行）换确定性 fsync。
            with open(path, "a", encoding="utf-8") as f:
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
                                {"from": "v1", "to": "v2", "blocks": len(conv) + 1})
            revision_store.record_revision(work_id, "session", "v1_to_v2",
                                           {"card": card_id, "blocks": len(conv)})
            logger.info("[facade] 现场升格 V1→V2 work=%s card=%s blocks=%d（含新块）",
                        work_id, card_id, len(conv) + 1)
    # [消息底座能力对齐 · 件2] 消息块同步落 events jsonl（会话落盘后同路径完成）
    await _emit_block_event(work_id, card_id, new_blk, event_type_hint)
    # [T152] append 点位：work/card/seq/turn/actor/结果
    session_ops.ops_log("append", work_id, card_id,
                        {"seq": new_blk["seq"], "turn": new_blk["turn"],
                         "actor": new_blk["actor"], "result": "ok"})
    return new_blk


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
