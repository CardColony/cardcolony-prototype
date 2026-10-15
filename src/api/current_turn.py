# -*- coding: utf-8 -*-
"""当前轮注入消息事件（[当前轮注入消息事件 2026-10-14]）。

把 160（工具卡步骤）与 165（思维链步骤）从**常驻 system 文本**改成
**当前轮消息事件**：两类事件 —— ``context_preamble``（固定总领）与
``context_injection``（动态步骤）—— 按发生顺序进入当前轮消息序列，只注入
一次、续调不重贴。设计与验收见 docs/当前轮注入消息事件-施工方案-20261014.md。

本模块**只做三件事**（不持状态、不做 IO 决策）：
1. ``build_injection_events``：按**现行互斥选择**（工具卡优先）向来源模块取
   结构化 data，返回本卡当前可发的事件列表；
2. ``project_message``：事件 data → 模型可见消息（合法 role + 文本）；
3. ``injection_key``：动态注入的幂等键。

**投影纪律**（任务书 v2 §0/§2）：
- 事件 type（context_preamble/context_injection）**绝不**当 role 下发；
- 投影 role 取 ``INJECTION_ROLE``（＝``assistant``），抬头取 〔工作规范〕/
  〔当前流程〕，正文为**第一人称自述**；事件本体/会话文件/timeline 身份不变
  （仍是注入类，不冒充用户发言、也不冒充助手回复）。

**来源选择**（沿用改造前 ``context_assembler`` 的 160/165 互斥）：
工具卡有运行时 → 只发工具卡注入；否则思维链有活板 → 发固定总领＋动态步骤。
两者皆无 → 空列表（不发任何事件）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import prompts as _prompts
from ..thinkchain import step_region as thinkchain_step_region
from ..toolcard import step_prompt as toolcard_step_prompt

# 投影 role（单一常量，一处改一处测）。
# [当前轮注入改assistant自述 2026-10-14] user → assistant：注入投影为「助手自述的
# 工作规范/流程」（用户 10:44 裁定）。末条即 assistant、**不补任何合成 user 消息**
# （用户 10:55；v1 自创的「补一条合成 user 收尾」已撤回）；该末条行为本项目未验证，
# 须实测并如实记录，若被端点拒绝则停手呈报、不自行改结构。
INJECTION_ROLE = "assistant"

# 投影抬头：让模型与审计都能一眼分辨「这是注入，不是用户发言」。
# [当前轮注入改assistant自述 2026-10-14] 由〔上下文注入·…〕改为用户原话用词
# 〔工作规范〕/〔当前流程〕（用户 10:44 原话）。
PREAMBLE_MARK = "〔工作规范〕"
INJECTION_MARK = "〔当前流程〕"

# 事件类型常量
TYPE_PREAMBLE = "context_preamble"
TYPE_INJECTION = "context_injection"

# [判断协议改注入 2026-10-14] 判断协议注入：单列一类事件与抬头——判断协议是
# 「入口判断协议」，语义不同于「工作规范」（context_preamble），故不复用后者
# （否则前端会把判断协议渲染成「上下文注入·固定总领」——标签是错的）。
TYPE_JUDGMENT = "judgment_protocol"
JUDGMENT_MARK = "〔判断协议〕"


def build_judgment_event() -> dict[str, Any]:
    """判断协议注入事件（无来源——协议是常量文本，只在轮首且本卡无活板时发一次）。

    data 结构与既有注入事件同族：``source_kind`` ＋ ``content``（协议**原文**，
    供审计/落盘）；模型可见投影（第一人称自述）见 ``project_judgment_message``。
    """
    return {"type": TYPE_JUDGMENT, "data": {
        "source_kind": "judgment_protocol",
        "content": _prompts.JUDGMENT_PROTOCOL,
    }}


def project_judgment_message() -> dict[str, Any]:
    """判断协议事件 → 模型可见消息（role＝INJECTION_ROLE，正文＝第一人称自述）。

    抬头〔判断协议〕＋ ``JUDGMENT_PROTOCOL_SELF_NARRATIVE``（只改三处人称）。
    """
    return {"role": INJECTION_ROLE,
            "content": f"{JUDGMENT_MARK}\n{_prompts.JUDGMENT_PROTOCOL_SELF_NARRATIVE}"}


def build_injection_events(work_id: str, card_id: str) -> list[dict[str, Any]]:
    """本卡当前可发的注入事件 ``[{"type", "data"}, ...]``（无来源 → 空列表）。

    互斥口径与改造前一致：工具卡运行时在 → 只发工具卡注入（工具卡先到先得）；
    否则思维链有当前步 → 发固定总领（思维链固定纪律）＋动态步骤。
    """
    toolcard = toolcard_step_prompt.build_injection_data(work_id, card_id)
    if toolcard is not None:
        return [{"type": TYPE_INJECTION, "data": toolcard}]

    injection = thinkchain_step_region.build_injection_data(work_id, card_id)
    if injection is None:
        return []   # 无活板/无当前步——不发（没有来源就不发注入事件）
    events: list[dict[str, Any]] = []
    preamble = thinkchain_step_region.build_preamble_data(work_id, card_id)
    if preamble is not None:
        events.append({"type": TYPE_PREAMBLE, "data": preamble})
    events.append({"type": TYPE_INJECTION, "data": injection})
    return events


def project_message(ev_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """事件 data → 模型可见消息（``{"role", "content"}``）。"""
    kind = str(data.get("source_kind") or "")
    if ev_type == TYPE_PREAMBLE:
        body = thinkchain_step_region.render_preamble(data)
        mark = PREAMBLE_MARK
    elif kind == "toolcard":
        body = toolcard_step_prompt.render_injection(data)
        mark = INJECTION_MARK
    else:
        body = thinkchain_step_region.render_injection(data)
        mark = INJECTION_MARK
    return {"role": INJECTION_ROLE, "content": f"{mark}\n{body}"}


def injection_key(ev_type: str, data: dict[str, Any]) -> str:
    """动态注入的幂等键：``{type}|{source_kind}|{step_identity}|{content_ver}``。

    - ``step_identity`` 定位「哪一步」；
    - ``content_ver``＝该步内容（提示词/产物；当前步字段＋已完成步成果）的指纹
      ——同一身份内容变（改板修订）＝新键＝发新注入（有意为之，不是重复）。
    只做「同键不重发」，**不靠『当前轮最后一条』判断**（任务书 §八.3）。
    """
    kind = str(data.get("source_kind") or "")
    if kind == "toolcard":
        ref = data.get("source_ref") or {}
        identity = f"{ref.get('script_name', '')}:{data.get('step_id', '')}"
        ver_payload: dict[str, Any] = {"content": data.get("content")}
    else:
        identity = (f"{data.get('plan_name', '')}:"
                    f"{data.get('index')}:{data.get('step_id', '')}")
        ver_payload = {"current_step": data.get("current_step"),
                       "completed_steps": data.get("completed_steps")}
    ver = hashlib.sha1(
        json.dumps(ver_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"{ev_type}|{kind}|{identity}|{ver}"
