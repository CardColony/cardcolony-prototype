# -*- coding: utf-8 -*-
"""[规则节点 2026-10-28 · 用户令「一个项目一个文件」]

**规则 = 项目基建**：一个项目一个文件 ``works/<work_id>/rules.md``。
它不是业务卡——不占 card_id、不进 cards 表、不上画布/清单；它只是这个项目的
规则节点，随项目建立，按 ``## 区名`` 分区：

    ## all            总规则 → 进**所有**卡的上下文
    ## project        根卡
    ## sub_project    子项目卡
    ## tool / worker / function / review / rule / topic
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

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("storage.rule")

RULES_FILENAME = "rules.md"

#: [2026-11-03 草案驳回落痕] 用户在审批弹窗「拒绝/关闭」一条 draft_rule_edit
#: 草案时，前端把该草案在会话时间线里的 `card_id:seq` 记入本文件——否则驳回
#: 只活在页面内存里，刷新后同一条历史 tool_result 又被重扫成「待审批」反复弹。
DISMISSALS_FILENAME = "rule_dismissals.json"

#: 总规则的分区名。与 card_type code 同构（全小写英文），排在所有类型区之前。
ALL_SECTION = "all"

#: 分区显示名（面板/日志用）。未列出的按 code 原样显示。
SECTION_LABELS: dict[str, str] = {
    ALL_SECTION: "总规则（所有卡）",
    "project": "根卡",
    "sub_project": "子项目卡",
    "tool": "工具卡",
    "worker": "worker 卡",
    "ui": "UI卡",
    "qa_card": "验收卡",
    "pm_card": "执笔卡",
    "function": "全体职能",
    "review": "评审卡",
    "rule": "规则节点自身",
    "topic": "课题卡",
}


def section_label(name: str) -> str:
    """分区显示名。``function.<code>`` 是按职能区（[两源模型 2026-11-01]
    QA 的验收准则这类——只进对应职能的上下文），标签动态拼。"""
    n = str(name or "")
    if n.startswith("function."):
        return f"职能·{n.split('.', 1)[1]}"
    return SECTION_LABELS.get(n, n)

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
        "1. 职责书定义你是谁；本区是本项目对**全体职能**的附加规则。\n"
        "2. 你只在被叫到时在场：差事派你、评审叫你、用户找你——没有自动唤醒。\n"
        "3. 按职能加码：`## function.<职能 code>` 区只进对应职能的上下文"
        "（例 `## function.qa` 写验收准则）。\n"
        "4. 你的产出落在被叫到的场：评审投你的票、结论写在场卡面、答案发回"
        "问询方——不私记台账（卡面是唯一事实源）。"),
    "sub_project": (
        "1. 开工前先读自己的需求文档；**没有正稿**就 report_up(kind=ask) 请示。\n"
        "2. **阶段任务=新建子卡**：一件阶段性的活建一张子卡（需求+验收判据写清），"
        "不自己在当前卡上直接干；你的卡是管理者——拆阶段、建子卡、等完工。\n"
        "3. 工作细节你域内自决，不上报待裁；现状块只写能核实的事实。\n"
        "4. 完工调 update_goal(complete) 收口——这一刻职能会来做阶段验收。\n"
        "5. 阶段完工才向上汇报（report_up, kind=phase），日常不打扰上级。\n"
        "6. 派活分流：判据是「要不要跑命令」——要跑命令/通用编程（后端、脚本、"
        "测试、起服务）派 create_worker_card；界面活（写前端页面＋浏览器里看"
        "效果）派 create_ui_card——UI 卡没有 bash/python，跑不了命令。\n"
        "7. worker／差事卡子卡没有现状块——验收看 worker_done 回执与代码提交"
        "（差事看 report 回执）；不要派它「写现状块」——委派执行体没有写卡面"
        "工具，edit_status 对 worker 也直接拒。要把结论落到它卡面，用 "
        "edit_other_face 自己代写。"),
    "tool": "按剧本执行；产物与用途落清单。",
    "worker": (
        "1. 任务在使命块里；产出＝代码/文档/命令结果＋report_done 回执。\n"
        "2. 你没有现状块——进展由回执与产物承载。\n"
        "3. 除 UI 外的执行活都归你：编程、写改文档、跑命令、起服务、构建测试、"
        "脚本、查档（manuscript 三件套只读）。"),
    # [UI 卡 2026-11-02] ui＝内循环执行卡（系统内模型，不走 wb_exec）：
    # 手里有平台工具面（含 browser），产出＝界面代码＋浏览器验证结论。
    "ui": (
        "1. 你干**界面活**：写前端页面（HTML/CSS/JS）、开浏览器看自己写的"
        "页面验效果。**你没有 bash/python——跑不了命令、起不了服务、写不了"
        "后端**；任务里这类需求不是你的活，report_up 写明「此部分需worker 卡」。\n"
        "2. 浏览器循环：navigate 打开 → inspect 看语义地图 → 用 ref 操作 → "
        "再 inspect/query 验证效果。ref 在页面跳转后全部作废，click 只证明"
        "输入已派发——效果必须亲眼验证。\n"
        "3. 结论只写你在页面里核到的事实（可见文字、console 报错、截图资产）；"
        "没核到的不许写进汇报。完工走 report_up。"),
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


def find_section_header_lines(content: str) -> list[str]:
    """内容里以 `## ` 开头的行——**下次读取会被 split_sections 切成幻影区**。

    [2026-11-01 幻影区事故] work_027 的根卡把一篇带 `## 一、`…`## 七、` 标题
    的整篇文档起草进 ``project`` 区：区名合法、落盘逐字节正确，但下一次
    ``split_sections`` 把文档内部标题切成七个无人消费的区名——规则静默孤儿化。
    入口（draft_rule_edit/commit/PATCH）用本函数拦内容里的 `## ` 行并给出
    具体行号；`write_sections` 对绕过入口的写入打 WARNING（不拦，存储层不判）。
    """
    return [ln.strip() for ln in (content or "").splitlines()
            if ln.startswith("## ")]


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
            secs = split_sections(p.read_text(encoding="utf-8"))
            # [worker 改名 2026-11-05] 存量 rules.md 的 `## coder` 区读时改名为
            # `## worker`（worker 卡按新区名取区；下次 write_sections 落盘自愈）。
            # 两区并存时不动（内容取舍由人裁定，不静默合并）。
            if "coder" in secs and "worker" not in secs:
                secs["worker"] = secs.pop("coder")
            return secs
        except Exception as exc:  # noqa: BLE001 读坏了当作没有，不阻断装配
            logger.warning("读规则文件失败 %s: %s", p, exc)
            return {}
    return _migrate_from_work_json(work_id)


def write_sections(work_id: str, sections: dict[str, str]) -> None:
    """整份覆盖写（面板/审批通道落盘用）。"""
    # [2026-11-01 幻影区兜底告警] 入口已拦 `## ` 行；这里再验一遍——绕过入口的
    # 内部调用/迁移写入若带 `## ` 行，落盘即幻影区化。不拦（存储层不判内容），
    # 但必须留痕，否则又是静默孤儿。
    for name, content in (sections or {}).items():
        bad = find_section_header_lines(str(content or ""))
        if bad:
            logger.warning(
                "[rule] %s 区 %r 内容含 %d 行 `## `（将被切成幻影区）：%s",
                work_id, name, len(bad), " / ".join(bad))
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


# ---------------------------------------------------------------------------
# [2026-11-03 草案驳回落痕] rule_dismissals.json：被驳回草案的 card_id:seq 表
# ---------------------------------------------------------------------------

def dismissals_path(work_id: str) -> Path:
    """本项目的草案驳回表路径。与 rules.md 同目录——同为项目基建件。"""
    from . import work_store  # noqa: PLC0415 局部 import 取当前 WORKS_DIR

    return Path(work_store.WORKS_DIR) / work_id / DISMISSALS_FILENAME


def _read_dismissals(work_id: str) -> dict[str, dict[str, Any]]:
    """读驳回表 → {key: meta}。文件缺失/坏掉 ⇒ {}（驳回态丢失只多弹窗，不丢数据）。"""
    p = dismissals_path(work_id)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 读坏了当作没有
        logger.warning("读规则草案驳回表失败 %s: %s", p, exc)
        return {}
    dismissed = data.get("dismissed") if isinstance(data, dict) else None
    return {str(k): (v if isinstance(v, dict) else {})
            for k, v in dismissed.items()} if isinstance(dismissed, dict) else {}


def list_dismissals(work_id: str) -> list[str]:
    """已驳回草案的 key 列表（``"card_id:seq"``），供 GET /rule 下发给前端过滤。"""
    return sorted(_read_dismissals(work_id).keys())


def is_dismissed(work_id: str, card_id: str, seq: int) -> bool:
    return f"{card_id}:{int(seq)}" in _read_dismissals(work_id)


def add_dismissal(work_id: str, card_id: str, seq: int,
                  sections: list[str] | None = None) -> bool:
    """把一条草案记为已驳回。**幂等**：同 key 已在表内直接成功，不改 ts。
    返回 False＝文件坏到无法安全合并（宁可不覆写别人的驳回记录）。
    """
    p = dismissals_path(work_id)
    data: dict[str, Any] = {"dismissed": {}}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("规则草案驳回表损坏，拒绝覆写 %s: %s", p, exc)
            return False
        if not isinstance(data, dict):
            data = {"dismissed": {}}
    dismissed = data.get("dismissed")
    if not isinstance(dismissed, dict):
        dismissed = {}
    key = f"{card_id}:{int(seq)}"
    if key not in dismissed:
        dismissed[key] = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sections": [str(s) for s in (sections or []) if str(s).strip()],
        }
    data["dismissed"] = dismissed
    _atomic_write(p, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return True
