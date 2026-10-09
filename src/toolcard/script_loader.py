# -*- coding: utf-8 -*-
"""T1 · 剧本装载（EC-028 / EC-029 / EC-030 / EC-031）。

**剧本＝一份数据文件；提示词另立文本文件，剧本存路径引用**（EC-029）。
新增一张工具卡＝放一份剧本文件＋一份提示词文件，**零代码改动**。

本模块只做两件事：**读文件** 与 **解析 JSON**。因此：

- **不判剧本内容对不对**（scenario 写得通不通、步骤该不该这么多）——EC-030
- **不判步骤数合不合理**——EC-030
- **不判提示词好坏**（提示词是原文，代码不读它的意思）——EC-030
- 文件不存在／JSON 解析失败 → **抛错**。抛的是**解析器的事实**（文件读不到／
  这段文本不是 JSON），不是「剧本写得不好」的判断——EC-031

剧本存放目录（下达方裁定第 2 条：由执行方选定并报明）：
    ``works/toolcard_scripts/``
选它的理由（三条，均可核）：① 落在 ``works/`` 下，而 ``works/`` 在
``.gitignore`` 第 16 行 ⇒ **不进 git 运行态**（任务书对目录落点的唯一硬要求）；
② 剧本是**每个 work 的数据**不是代码，随 works 一起备份/迁移，不进代码仓历史；
③ 与产物落点 ``works/{work_id}/toolcards/`` 同属一棵运行态树，删 work 即删干净。
``SCRIPTS_ROOT`` 是模块级常量 ⇒ 测试可 monkeypatch（与 ``card_store.WORKS_DIR``
同一套隔离手法，见 tests/conftest.py）。

剧本文件形态（一份 JSON）::

    {
      "name": "方案链",
      "scenario": "从任务书到可施工方案",
      "steps": [
        {"id": "p1", "prompt_ref": "prompts/方案链-p1.txt"},
        {"id": "p2", "prompt_ref": "prompts/方案链-p2.txt"}
      ],
      "artifact_naming": "{seq}-{step}-{name}-{ver}{ext}"
    }

``steps`` 里除 ``id``/``prompt_ref`` 外的键**原样透传**（机械搬运，不解释、不丢弃）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("toolcard.script_loader")

# 剧本根目录（模块级常量 ⇒ 测试可 monkeypatch；见 conftest 的 WORKS_DIR 手法）
SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "works" / "toolcard_scripts"


class ScriptError(RuntimeError):
    """剧本装载失败（文件读不到／不是合法 JSON／结构取不到必需键）。

    这是**解析器的事实**，不是对剧本质量的评价——EC-031。
    """


@dataclass(frozen=True)
class StepDef:
    """一步的机械定义。``id`` 与 ``prompt_ref`` 是必需键，其余键原样透传。"""

    id: str
    prompt_ref: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScriptDefinition:
    """一份剧本的解析结果（原样数据，无加工）。"""

    name: str
    scenario: str
    steps: list[StepDef]
    artifact_naming: str
    path: Path = Path("")

    def step_ids(self) -> list[str]:
        """步骤 id 列表（**顺序即剧本书写顺序**）。"""
        return [s.id for s in self.steps]

    def find_step(self, step_id: str) -> StepDef | None:
        """按 id 取一步；**取不到返回 None**（供 driver 作结构性拒绝用，不作评价用）。"""
        for s in self.steps:
            if s.id == step_id:
                return s
        return None


def _resolve_in_root(rel: str) -> Path:
    """把剧本内的相对引用解析成剧本根内的绝对路径。

    **结构性护栏**（与「目标步不存在」同类，不是对内容的判断）：引用不允许
    逃出剧本根。不逃则正常读；逃则抛 ScriptError（路径不是合法引用）。
    """
    root = SCRIPTS_ROOT.resolve()
    p = (root / rel).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise ScriptError(f"剧本引用越出剧本根: {rel}") from None
    return p


def _script_file(name: str) -> Path:
    """剧本名 → 剧本文件路径（``{name}.json``）。**不判名字对不对**：读不到就抛。"""
    return SCRIPTS_ROOT / f"{name}.json"


def load_script(name: str) -> ScriptDefinition:
    """按名装载一份剧本（EC-028）。

    只读只解析。文件不存在 → ScriptError；JSON 解析失败 → ScriptError；
    ``steps`` 缺失或不是列表 → ScriptError（结构取不到必需键，也是解析事实）。

    **不判**：剧本好不好、步骤该不该这么多、scenario 通不通、提示词质量。
    """
    p = _script_file(name)
    if not p.exists():
        raise ScriptError(f"剧本不存在: {name}（{p}）")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScriptError(f"剧本不是合法 JSON: {name}（{exc}）") from None
    if not isinstance(data, dict):
        raise ScriptError(f"剧本根不是对象: {name}")

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        raise ScriptError(f"剧本缺 steps 列表: {name}")

    steps: list[StepDef] = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            raise ScriptError(f"剧本 steps 元素不是对象: {name}")
        sid = str(raw.get("id", "") or "")
        if not sid:
            raise ScriptError(f"剧本步骤缺 id: {name}")
        # prompt_ref 缺省＝该步没有独立提示词文件（取空串，不判其好坏）
        pref = str(raw.get("prompt_ref", "") or "")
        extra = {k: v for k, v in raw.items() if k not in ("id", "prompt_ref")}
        steps.append(StepDef(id=sid, prompt_ref=pref, extra=extra))

    return ScriptDefinition(
        name=str(data.get("name", "") or name),
        scenario=str(data.get("scenario", "") or ""),
        steps=steps,
        artifact_naming=str(data.get("artifact_naming", "") or ""),
        path=p,
    )


def load_prompt(script_name: str, prompt_ref: str) -> str:
    """取一步的提示词全文（**原样返回，一个字不改、不摘、不评**）。

    ``prompt_ref`` 缺省 → 返回空串（该步没配提示词文件，是事实不是缺陷）。
    文件读不到 → ScriptError（读盘事实）。
    """
    if not prompt_ref:
        return ""
    p = _resolve_in_root(prompt_ref)
    if not p.exists():
        raise ScriptError(f"提示词文件不存在: {prompt_ref}")
    return p.read_text(encoding="utf-8")


def list_scripts() -> list[str]:
    """列出可用剧本名（按文件名排序）。目录不存在返回空列表。"""
    if not SCRIPTS_ROOT.exists():
        return []
    return sorted(p.stem for p in SCRIPTS_ROOT.glob("*.json"))


def script_exists(name: str) -> bool:
    """剧本文件在不在——**纯 IO 事实**。

    调用方用它来「找不到就报错」是**允许的**（EC-059/T7：找不到＝报错）；
    但**不得**由它推出任何关于剧本质量的结论。
    """
    return _script_file(name).exists()
