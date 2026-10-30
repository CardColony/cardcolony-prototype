# -*- coding: utf-8 -*-
"""[规则节点 2026-10-28 · 用户令「一个项目一个文件」]

**规则 = 项目基建**：一个项目一个文件 ``works/<work_id>/rules.md``。
它不是业务卡——不占 card_id、不进 cards 表、不上画布/清单；它只是这个项目的
规则节点，随项目建立，按 ``## 区名`` 分区：

    ## all            总规则 → 进**所有**卡的上下文
    ## project        根卡
    ## sub_project    子项目卡
    ## tool / coder / function / review / rule / topic
                      → 只进对应 card_type 的卡的上下文

分区名跟类型注册表走（``context_regions.rule_section_names``）；某区空着就是
「这一类没有附加规则」，机制照常工作（用户：规则「可以不用，但需要按这个分区」）。

**为什么是独立文件，而不是 work.json 的一个字段**（2026-10-25 的形态）：
规则是基建件，应当像 .gitignore / package.json 那样跟着项目目录走——人能拿文本
编辑器直接改、能 diff、能随项目一起备份；混进 work.json 它只是众多键中的一个，
看不出它是基建。work.json 的 ``rules`` 字段从此只作**迁移来源**（首次读到就搬进
本文件，之后不再读它）。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("storage.rule")

RULES_FILENAME = "rules.md"

#: 总规则的分区名。与 card_type code 同构（全小写英文），排在所有类型区之前。
ALL_SECTION = "all"

#: 分区显示名（面板/日志用）。未列出的按 code 原样显示。
SECTION_LABELS: dict[str, str] = {
    ALL_SECTION: "总规则（所有卡）",
    "project": "根卡",
    "sub_project": "子项目卡",
    "tool": "工具卡",
    "coder": "编程卡",
    "function": "职能卡",
    "review": "评审卡",
    "rule": "规则节点自身",
    "topic": "课题卡",
}

#: 新建项目的默认规则（通用工作流，不含任何任务事实）。
#: [2026-10-28] 两处旧口径已修：①「总裁策展」→ 需求走评审+正稿（curate_face 时代已结束）
#: ②「只追加不改」→ 块是快照、改那一条（块不是账本）。
DEFAULT_SECTIONS: dict[str, str] = {
    ALL_SECTION: (
        "1. 动手前先读本卡的面：使命说为什么存在、需求文档说做到什么算完成、"
        "现状说现在到哪。三者冲突时以需求文档为验收判据。\n"
        "2. 块是**快照**不是账本：过时的读数换成新的，作废的删掉，不往后堆段落。\n"
        "3. 结果写能核实的事实（交付物在哪、跑什么命令能验、跑出什么结果），"
        "不写「我打算怎么做」。"),
    "project": (
        "1. 拆解用户意图；没有来源的前提列出来向用户确认（confirm_with_user），"
        "不默认成立。\n"
        "2. 子卡开工的前提是它的需求文档**已评审成正稿**（见做事方法「评审需求」）。\n"
        "3. 派工后等职能评审观点；分歧未消带上分歧问用户。\n"
        "4. 阶段完成向用户汇报（report_to_user），不等被问。"),
    "function": (
        "1. 职责书定义你是谁、什么情况开口；本区是本项目对你的附加规则。\n"
        "2. 你只在阶段收口被叫醒——对整个阶段表态（speak），沉默不落账等于失职。\n"
        "3. 与父卡统合判断相左，写分歧（write_ledger, ledger=divergence）。"),
    "sub_project": (
        "1. 开工前先读自己的需求文档；**没有正稿**就 report_up(kind=ask) 请示。\n"
        "2. **阶段任务=新建子卡**：一件阶段性的活建一张子卡（需求+验收判据写清），"
        "不自己在当前卡上直接干；你的卡是管理者——拆阶段、建子卡、等完工。\n"
        "3. 工作细节你域内自决，不上报待裁；现状块只写能核实的事实。\n"
        "4. 完工调 update_goal(complete) 收口——这一刻职能会来做阶段验收。\n"
        "5. 阶段完工才向上汇报（report_up, kind=phase），日常不打扰上级。"),
    "tool": "按剧本执行；产物与用途落清单。",
    "coder": (
        "1. 任务在使命块里；产出＝代码＋report_done 回执。\n"
        "2. 你没有现状块——进展由回执与代码承载。"),
    # [规则节点 2026-10-28 · 用户令「规则卡不挂模型和对话，不需要工具」]
    # 规则节点没有模型、没有对话、没有工具面 ⇒ 没有"它自己该怎么做事"可言。
    # 分区保留（分区＝卡类型列表，跟着注册表长），内容留空。
    "rule": "",
    "topic": (
        "1. 开工前先读自己的需求文档；没有正稿就 report_up(kind=ask) 请示。\n"
        "2. 研究结论写需求文档块，只保留现行结论（过时的换成新的，不追加）。"),
}


def rules_path(work_id: str) -> Path:
    """本项目的规则文件路径。**每次现取 WORKS_DIR**：测试会 monkeypatch 它。"""
    from . import work_store  # noqa: PLC0415 局部 import 取当前 WORKS_DIR

    return Path(work_store.WORKS_DIR) / work_id / RULES_FILENAME


def split_sections(body: str) -> dict[str, str]:
    """按 `## 区名` 切区：{区名: 区内容}。区头行不进内容；同名后出现者覆盖先出现者。

    区头前若有游离正文（无归属行）归入 key ``""``——面板与注入都不消费它，
    只作存储完整性（人不小心在文件开头写了几句话，不该整份规则报废）。
    """
    sections: dict[str, str] = {}
    if not body:
        return sections
    current: str | None = None
    buf: list[str] = []
    for ln in body.splitlines():
        if ln.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip("\n").rstrip()
            current = ln[3:].strip()
            buf = []
        else:
            if current is None:
                current = ""
            buf.append(ln)
    if current is not None:
        sections[current] = "\n".join(buf).strip("\n").rstrip()
    return sections


def join_sections(sections: dict[str, str]) -> str:
    """把 {区名: 内容} 拼回文件正文：`## 区名` ＋ 内容，区间空一行。"""
    parts: list[str] = []
    for name, content in (sections or {}).items():
        if not str(name).strip():
            continue
        parts.append(f"## {name}\n{str(content or '').rstrip()}")
    return "\n\n".join(parts) + ("\n" if parts else "")


def _atomic_write(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件再 os.replace（Windows 同盘可替换）。

    Windows 上 os.replace 偶发 WinError 5（目标文件正被杀软/索引器短暂占用），
    短暂重试即可；重试仍失败就抛——不吞错，否则规则悄悄没写进去。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    last: Exception | None = None
    for delay in (0.0, 0.05, 0.12, 0.25):
        if delay:
            time.sleep(delay)
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:   # WinError 5：短暂占用
            last = exc
    raise last if last is not None else RuntimeError("规则文件写入失败")


def _migrate_from_work_json(work_id: str) -> dict[str, str]:
    """把旧形态（work.json 的 rules 字段）搬进文件。**只在文件不存在时调用**。

    2026-10-25 ～ 2026-10-28 之间规则存在 work.json.rules；用户 2026-10-28 定
    「一个项目一个文件」⇒ 首次读到就搬过来，之后 work.json 那份不再被读
    （也不删，留作历史痕迹——删别人的数据要有明确指令）。
    """
    try:
        from . import work_store  # noqa: PLC0415

        data = work_store.read_work(work_id) or {}
    except Exception as exc:  # noqa: BLE001 读不到就当没有旧数据
        logger.debug("读 work.json 取旧规则失败 %s: %s", work_id, exc)
        return {}
    legacy = data.get("rules")
    if not isinstance(legacy, dict):
        return {}
    out = {str(k): str(v or "") for k, v in legacy.items() if str(k).strip()}
    return out


def read_sections(work_id: str) -> dict[str, str]:
    """读本项目的规则分区（区名→正文）。文件不存在 ⇒ 返回 {}（不在这里造文件）。"""
    p = rules_path(work_id)
    if p.exists():
        try:
            return split_sections(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 读坏了当作没有，不阻断装配
            logger.warning("读规则文件失败 %s: %s", p, exc)
            return {}
    return _migrate_from_work_json(work_id)


def write_sections(work_id: str, sections: dict[str, str]) -> None:
    """整份覆盖写（面板/审批通道落盘用）。"""
    _atomic_write(rules_path(work_id),
                  join_sections({str(k): str(v or "") for k, v in (sections or {}).items()}))


def write_section(work_id: str, section: str, content: str) -> None:
    """只替换一个区，其余区**字节不动**（面板直改即存）。"""
    cur = read_sections(work_id)
    cur[str(section)] = str(content or "")
    write_sections(work_id, cur)


def ensure(work_id: str, defaults: dict[str, str] | None = None) -> bool:
    """确保本项目有规则文件（建项目时调用）。**幂等，且只在缺失时创建**。

    文件已存在 ⇒ **一个字节都不动**。不"补缺区"：某区空着是合法状态
    （用户令「规则按卡类型分区没问题，可以不用」），拿默认值把它填回去等于
    覆盖人的意图——文件是人的东西，不是模板。
    返回 True＝本次创建了文件，False＝文件已存在。
    """
    p = rules_path(work_id)
    if p.exists():
        return False
    base = dict(defaults if defaults is not None else DEFAULT_SECTIONS)
    cur = _migrate_from_work_json(work_id)
    write_sections(work_id, {**base, **cur})   # 旧内容优先于默认值
    return True
