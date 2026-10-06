# -*- coding: utf-8 -*-
"""轮槽＋折叠器＋开关（THINK-ECO-T11 · 移植对应总表 #9 新造）。

[范围澄清] 折叠只取 dsh「已核实结论」机制的**模型层范式**（结论/依据/不静默/
有界/无内容不注入，evidence-index.ts 实读：结论模型压、原文出处代码机械写、
最近 K 条有界、无内容不注入）；机器层工具调用索引不移植——推演侧无事件流
钩子，且折叠的【依据】已承担「怎么找回原文」的职责。

形态（方案 §六＋任务书折叠器行为细则，执行方照此实现）：
- 折叠单元＝工具轮：一条含工具调用的 assistant＋其对应若干条工具返回。
- 不折红线三条（结构性保证）：首轮用户原文固化、抢救回注——这两类消息
  经 append_plain 进槽，根本不构成「轮」（只有 begin_round 登记的
  assistant+tool 组才是轮）；当前轮全文——由保留窗口 K=2 机械保证
  （最近 2 轮全文保留，更早的轮次才折叠）。
- 两件套：【结论】＝这次调用得到了什么（折叠发生时由模型压出，一句话，
  不设任何长度限制——200B 上限作废）；【依据】＝工具名＋参数＋原文位置
  （代码机械写，不动脑子、不会编，全程无任何截断逻辑）。
- 产出机制：装配时（wire_messages）对窗口外旧轮次执行一次折叠（请求局部、
  折后形态缓存在槽内不重复压）；只压内容、不删消息——消息条数与配对结构
  （tool_call_id ↔ tool_calls 一一对应）不改变；失败→该轮不折、全文保留、
  fold_ops 记错误条目（不留白）；同请求内已失败轮不重试。
- 边界：【结论】仅是轮槽内压缩替身——不进会话文件、不进证据索引、不推送。
  原列表永不改写（prompt 日志与失败兜底都吃原文）。

轮槽为请求局部对象、不做全局内存态（单进程并发处理不同卡请求不串味）。
分组按轮次、不按调用 id（id 每轮从 0 重起、跨轮重复——观察项首版不改）。
开关：环境变量 TOOL_FOLD_ENABLED（T9：缺省关；字面 "1"/"true" 开，其余关）。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from . import model_service
from .card_tools import READONLY_TOOLS

logger = logging.getLogger("api.tool_fold")

FOLD_ENV = "TOOL_FOLD_ENABLED"          # T9 开关命名
KEEP_WINDOW = 2                          # 保留窗口 K=2（方案自拍 #14）

# 全文在入参的参数键（write_file/edit_card/update_shadow 的 content）——
# 折叠入参时按【键名】机械替换为长度标记（不是截断：是折叠本身）
_BULK_ARG_KEYS = frozenset({"content"})

_COMPRESS_SYSTEM = (
    "你是工具轮折叠器。输入是一轮工具调用的入参与返回原文。"
    "请输出一句【结论】正文：这一轮工具做了什么、得到了什么。"
    "只输出结论正文本身，不加任何前缀或解释；结论不设长度限制，"
    "把该轮最重的信息完整压进去。"
)


def fold_enabled() -> bool:
    """T9：TOOL_FOLD_ENABLED，缺省关；字面 "1"/"true" 开，其余关。"""
    v = os.environ.get(FOLD_ENV)
    return v == "1" or v == "true"


def _round_bytes(assistant_msg: dict[str, Any], tool_msgs: list[dict[str, Any]]) -> int:
    """一轮消息的序列化字节数（fold_ops 原字节/折后字节口径）。"""
    blob = json.dumps([assistant_msg] + tool_msgs, ensure_ascii=False).encode("utf-8")
    return len(blob)


def _evidence_for(tool_name: str, arguments_json: str, readonly: bool) -> str:
    """【依据】＝工具名＋参数＋原文位置。代码机械写：无截断、无判断、不会编。

    全文在入参的键（content）按折叠语义替换为长度标记——这是折叠本身，
    不是截断；其余参数逐字保留（只读工具的参数即重取键，全量在场）。
    """
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except Exception:  # noqa: BLE001
        args = None
    if isinstance(args, dict):
        bits: list[str] = []
        for k, v in args.items():
            if k in _BULK_ARG_KEYS and isinstance(v, str):
                bits.append(f"{k}=<已折叠，共 {len(v)} 字>")
            else:
                bits.append(f"{k}={json.dumps(v, ensure_ascii=False)}")
        param_part = "参数={" + ", ".join(bits) + "}"
    else:
        param_part = f"参数=<不可解析为 JSON，原文 {len(arguments_json)} 字>"
    if readonly:
        where = "原文位置=只读工具返回，可按原参数重跑该工具重取原文"
    else:
        where = ("原文位置=写工具，效果已落存储，"
                 "正文可经对应读工具（read_file/read_card/read_shadow）查证")
    return f"工具={tool_name}；{param_part}；{where}"


async def _compress_round(assistant_msg: dict[str, Any],
                          tool_msgs: list[dict[str, Any]]) -> str:
    """对一轮做一次模型压出（独立压缩动作）。

    不设任何闸：不设 max_tokens、不设超时、不设采样参数（走网关缺省）、
    不设轮次上限。返回压缩出的结论正文（strip 后非空）。
    失败抛异常，由调用方按 V5 处理（该轮不折、全文保留、留痕）。
    """
    parts: list[str] = []
    for tc in assistant_msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        parts.append(f"调用 {fn.get('name', '')}，入参：{fn.get('arguments', '')}")
    for m in tool_msgs:
        parts.append(f"返回（tool_call_id={m.get('tool_call_id', '')}）：{m.get('content', '')}")
    gw = model_service.get_gateway()
    result = await gw.call("main", _COMPRESS_SYSTEM, "\n".join(parts),
                           skip_completion_marker=True)
    content = (result.get("content") or "").strip()
    if not content:
        raise RuntimeError("折叠压缩返回空内容")
    return content


class ToolTurnSlot:
    """请求局部轮槽：包住 routes 的 prior_for_gateway 同一条 messages 列表。

    - 折叠关：wire_messages() 原样返回列表本身（逐字同旧，零加工——T6 判据）。
    - 折叠开：装配时对窗口外旧轮次做一次折叠（折后形态缓存），返回带替换的
      **新列表**；被包列表永不改写。
    - append 面（T6）：首轮用户原文固化与抢救回注走 append_plain（非轮、
      永不折）；assistant＋tool 结果走 begin_round／append_tool_result（成轮）。
    """

    def __init__(self, messages: list[dict[str, Any]]):
        self._messages = messages
        # 每轮：{"assistant_idx", "tool_idxs", "folded": None|{"assistant","tool_contents"},
        #        "fold_error": ""}
        self._rounds: list[dict[str, Any]] = []
        self._open: Optional[dict[str, Any]] = None
        self.fold_ops: list[dict[str, Any]] = []  # T7：落 prompt 日志 fold_ops 键

    # ------------------------------------------------------------------
    # append 面
    # ------------------------------------------------------------------
    def append_plain(self, msg: dict[str, Any]) -> None:
        """非轮消息：首轮用户原文固化、抢救回注等——永不折叠（不折红线）。"""
        self._messages.append(msg)

    def begin_round(self, assistant_msg: dict[str, Any]) -> None:
        """开新轮：含工具调用的 assistant 消息（结构由调用方按原样构造）。

        轮记录在 begin 时即入册（wire_messages 只在轮与轮之间被调用——
        入册轮在装配时必然已收完工具返回，无需显式 close）。
        """
        r = {"assistant_idx": len(self._messages), "tool_idxs": [],
             "folded": None, "fold_error": ""}
        self._rounds.append(r)
        self._open = r
        self._messages.append(assistant_msg)

    def append_tool_result(self, msg: dict[str, Any]) -> None:
        """工具返回入槽：归入当前开放轮（配对结构由调用方的 tool_call_id 保证）。"""
        if self._open is not None:
            self._open["tool_idxs"].append(len(self._messages))
        self._messages.append(msg)

    # ------------------------------------------------------------------
    # 装配面
    # ------------------------------------------------------------------
    @property
    def round_count(self) -> int:
        return len(self._rounds)

    async def wire_messages(self) -> list[dict[str, Any]]:
        """装配时折叠：窗口外旧轮次压成【结论】＋【依据】两件套。

        折叠关（或缺轮次、或无一轮折成）→ 原列表原样返回。
        """
        if not fold_enabled() or not self._rounds:
            return self._messages
        for r in self._rounds[:-KEEP_WINDOW] if len(self._rounds) > KEEP_WINDOW else []:
            if r["folded"] is None and not r["fold_error"]:
                await self._fold_round(r)
            # 已失败轮不重试：fold_error 留痕、该轮全文保留（V5；不静默）
        if not any(r["folded"] is not None for r in self._rounds):
            return self._messages
        replacements: dict[int, dict[str, Any]] = {}
        for r in self._rounds:
            if r["folded"] is None:
                continue
            replacements[r["assistant_idx"]] = r["folded"]["assistant"]
            for idx, content in zip(r["tool_idxs"], r["folded"]["tool_contents"]):
                replacements[idx] = {**self._messages[idx], "content": content}
        return [replacements.get(i, m) for i, m in enumerate(self._messages)]

    # ------------------------------------------------------------------
    # 折叠
    # ------------------------------------------------------------------
    async def _fold_round(self, r: dict[str, Any]) -> None:
        am = self._messages[r["assistant_idx"]]
        tms = [self._messages[i] for i in r["tool_idxs"]]
        round_no = self._rounds.index(r) + 1  # 轮号＝入册位次（1 起），非实时轮数
        tcs = am.get("tool_calls") or []
        tool_names = [(tc.get("function") or {}).get("name", "") for tc in tcs]
        original_bytes = _round_bytes(am, tms)
        if not tms or not tcs:
            # 结构异常防御：无入参或无返回的「轮」不构成可折单元，全文保留
            r["fold_error"] = "round_shape_anomaly"
            logger.warning("工具轮结构异常不折叠 round=%s tool_calls=%s results=%s",
                           round_no, len(tcs), len(tms))
            self.fold_ops.append({"round": round_no, "tools": tool_names,
                                  "original_bytes": original_bytes,
                                  "folded_bytes": original_bytes, "ok": False,
                                  "error": r["fold_error"]})
            return
        try:
            conclusion = await _compress_round(am, tms)
            calls_by_id = {tc.get("id", ""): tc for tc in tcs}
            folded_calls = []
            evidences: dict[int, str] = {}
            for i, tc in enumerate(tcs):
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                ev = _evidence_for(name, fn.get("arguments", ""),
                                   name in READONLY_TOOLS)
                evidences[i] = ev
                folded_calls.append({
                    "id": tc.get("id", ""),
                    "type": tc.get("type", "function"),
                    "function": {"name": name,
                                 "arguments": json.dumps(
                                     {"folded": True, "tool": name, "依据": ev},
                                     ensure_ascii=False)},
                })
            folded_assistant = {"role": "assistant",
                                "content": am.get("content", ""),
                                "tool_calls": folded_calls}
            tool_contents = []
            for i, m in enumerate(tms):
                # 依据按 tool_call_id 对回原调用；对不上时机械记空 id 的调用事实
                tc = calls_by_id.get(m.get("tool_call_id", ""))
                ev = (evidences.get(i) if tc is not None
                      else _evidence_for("", "", False))
                tool_contents.append(f"【结论】{conclusion}\n【依据】{ev}")
            folded_bytes = _round_bytes(
                folded_assistant,
                [{**m, "content": c} for m, c in zip(tms, tool_contents)])
            r["folded"] = {"assistant": folded_assistant,
                           "tool_contents": tool_contents}
            self.fold_ops.append({"round": round_no, "tools": tool_names,
                                  "original_bytes": original_bytes,
                                  "folded_bytes": folded_bytes, "ok": True})
            logger.info("工具轮已折叠 round=%s tools=%s %s→%s 字节",
                        round_no, tool_names, original_bytes, folded_bytes)
        except Exception as exc:  # noqa: BLE001
            r["fold_error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("工具轮折叠失败（该轮全文保留、不静默）round=%s: %s",
                           round_no, exc)
            self.fold_ops.append({"round": round_no, "tools": tool_names,
                                  "original_bytes": original_bytes,
                                  "folded_bytes": original_bytes, "ok": False,
                                  "error": r["fold_error"]})
