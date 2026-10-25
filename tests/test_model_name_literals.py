# -*- coding: utf-8 -*-
"""**模型名收口**回归网（[5715 · 用户令「所有的需要模型名判定的地方，应该是一个变量，
变量取自端点模型名，而不是靠写死」]）。

## 为什么有这条网

模型名写死在代码里 ⇒ **模型换代时必然漏改**：漏改的那一处要么 404、要么悄悄
用了别的模型、要么让「补 reasoning_content」这类判据失效（不补 ⇒ 400）。
本仓已经吃过两次亏：

* ``model_config.DEFAULT_MODEL = "haiku"`` —— 而目录里根本没有 ``haiku`` 这个键
  （真键是 ``anthropic-claude-haiku-55``）⇒ 一旦 ``system.default_model`` 缺失就
  回落成**不存在的模型名** ⇒ 调用即崩。
* ``gateway._is_deepseek_like()`` 用「模型名含 deepseek / vendor ∈ {opencode,
  deepseek}」这种**名字启发式**判「要不要补 reasoning_content」⇒ 模型改名就
  悄悄改判 ⇒ 400。

## 本网钉住三件事

1. **代码里不许出现模型名字面量**（判据**自动跟随模型目录**：目录里现有的每个
   模型键名与 wire 名，代码里都不许写；另附一份「目录外的历史名」黑名单）。
2. **判据必须来自配置**：凡是要按模型做特殊处理的，配置里必须有显式标记
   （``reasoning_echo_fill`` / ``use``），代码只读配置。
3. **新增模型忘了标 ⇒ 网红**：重算一遍「哪些档按旧启发式需要补 reasoning_content」，
   与目录里的显式标记逐条比对 ⇒ 少标即红（这就是「换代没改全」的那道闸）。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
STATIC = ROOT / "static"
MODELS_YAML = ROOT / "config" / "models.yaml"

#: 另一施工线**未提交**的新模块（src/api/wb_exec/ 整目录是 untracked）。
#: 那里的模型名默认值由 config/wb_exec.json 驱动（配置是真值源），代码里只是兜底；
#: 本网**先放过**它，等那条线提交后一并收口。
_ALLOWED_DIRS = ("src/api/wb_exec/",)

#: 目录外的历史模型名（已不在 models.yaml 里，但代码里仍可能残留）⇒ 一律禁。
_HISTORICAL_NAMES = {
    "hy3", "k3", "k3.1-pro", "k3-256k", "kimi-for-coding", "kimi-for-coding-highspeed",
    "k3.1-pro-highspeed", "beam-free", "qwen3.8-max", "qwen3.8-max-free",
    "minimax-m3.1-flash", "minimax-m3.1-flash:free", "minimax/minimax-m3.1-flash",
    "minimax/minimax-m3.1-flash:free", "deepseek-v4-pro", "deepseek-v4.1-flash",
    "deepseek-v4-flash", "deepseek-flash", "glm-5.3", "glm-5.3-flash",
    "MiniMax-M3.1-Flash", "MiniMax-M3", "ark-code-latest", "step-5-preview",
    "deepseek/deepseek-v4.1-flash", "reflection/beam:free", "claude-haiku-5.5",
}


def _catalog() -> tuple[dict, dict, set[str], set[str]]:
    """读模型目录：原始三层、编译后 models 段、键名集、wire 名集。"""
    raw = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8"))
    models = raw.get("models") or {}
    keys = {str(k) for k in models}
    wires = {str(v.get("model")) for k, v in models.items() if isinstance(v, dict) and v.get("model")}
    return raw, models, keys, wires


def _iter_py_strings() -> list[tuple[Path, int, str]]:
    """(文件, 行号, 字面量) —— 只取**字符串常量**（注释/docstring 里的举例不算）。"""
    out: list[tuple[Path, int, str]] = []
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if any(rel.startswith(d) for d in _ALLOWED_DIRS):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.append((p, node.lineno, node.value))
    return out


def _iter_js_strings() -> list[tuple[Path, int, str]]:
    """(文件, 行号, 字面量) —— JS/前端：扫引号包裹的简单字面量。

    **跳过注释行**（``//`` / ``*`` 开头）：注释不是代码（但注释里也不该写型号，
    那是「不漂」的另一层要求，单独由人负责）。
    """
    out: list[tuple[Path, int, str]] = []
    pat = re.compile(r"""["'`]([A-Za-z0-9][A-Za-z0-9._/\-]{2,})["'`]""")
    for p in sorted(list(STATIC.rglob("*.js")) + list(STATIC.rglob("*.mjs"))):
        rel = p.relative_to(ROOT).as_posix()
        if any(rel.startswith(d) for d in _ALLOWED_DIRS):
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
                continue
            for m in pat.finditer(line):
                out.append((p, i, m.group(1)))
    return out


# ================================================================ 1 · 代码里不许有模型名字面量

def test_no_model_name_literals_in_python():
    """Python 侧：字符串常量里不得出现模型名（目录里的键名/wire 名 + 历史黑名单）。"""
    _raw, _models, keys, wires = _catalog()
    banned = keys | wires | _HISTORICAL_NAMES
    hits = []
    for p, ln, s in _iter_py_strings():
        v = s.strip()
        if v in banned:
            hits.append(f"{p.relative_to(ROOT).as_posix()}:{ln} 字面量 {v!r}")
    assert not hits, ("代码里出现了模型名字面量（模型换代必漏改 ⇒ 崩）：\n  "
                      + "\n  ".join(hits[:20]))


def test_no_model_name_literals_in_frontend():
    """前端侧：不得出现模型名（settings.js 的供应商模板曾写着 "qwen3.8-max" 等）。"""
    _raw, _models, keys, wires = _catalog()
    banned = keys | wires | _HISTORICAL_NAMES
    hits = []
    for p, ln, s in _iter_js_strings():
        v = s.strip()
        if v in banned:
            hits.append(f"{p.relative_to(ROOT).as_posix()}:{ln} 字面量 {v!r}")
    assert not hits, ("前端里出现了模型名字面量（显示会过期、判定会错）：\n  "
                      + "\n  ".join(hits[:20]))


def test_default_model_constant_is_not_a_literal():
    """``DEFAULT_MODEL`` 不许再是字面量（改前 "haiku" 在目录里根本不存在）。"""
    from src.api import model_config
    assert not str(model_config.DEFAULT_MODEL or "").strip(), (
        "DEFAULT_MODEL 必须是空串（兜底走模型目录），不许再写死一个名字")
    _raw, models, keys, _w = _catalog()
    assert "anthropic-claude-haiku-55" in keys, "目录里 haiku 的真键应当是 anthropic-claude-haiku-55"


# ================================================================ 2 · 判据来自配置

def test_search_model_comes_from_catalog():
    """搜索用的模型必须**从目录取**（``use: search`` 标记的那档），不许写死。"""
    from src.api import model_config
    _raw, models, _keys, _w = _catalog()
    tagged = [k for k, v in models.items()
              if isinstance(v, dict) and str(v.get("use") or "") == "search"]
    assert tagged, "models.yaml 里得有档标 use: search（搜索查询改写用哪一档）"
    got = model_config.model_for_purpose("search")
    want = str(models[tagged[0]].get("model") or "")
    assert got == want, f"model_for_purpose('search') = {got!r}，应为目录那一档的 {want!r}"


def test_reasoning_echo_fill_marker_covers_old_heuristic():
    """**新增模型忘了标 ⇒ 网红**（这就是「换代没改全」那道闸）。

    重算一遍「按旧启发式（模型名含 deepseek / vendor ∈ {opencode,deepseek}）需要补
    reasoning_content」的档，与目录里的显式标记逐条比对 ⇒ 少标即红。
    """
    _raw, models, _keys, _w = _catalog()
    OPENCODE_PREFIX = "https://opencode.ai/"

    def old_heuristic(key: str, e: dict) -> bool:
        vendor = str(e.get("vendor") or e.get("provider") or "")
        return (
            "deepseek" in key.lower()
            or "deepseek" in str(e.get("model") or "").lower()
            or vendor in ("opencode", "deepseek")
            or str(e.get("base_url") or "").startswith(OPENCODE_PREFIX)
        )

    missing = []
    for k, e in models.items():
        if not isinstance(e, dict):
            continue
        if not old_heuristic(k, e):
            continue
        # 注意：``reasoning_echo_fill: ""`` 是**有效标记**（补空串），
        # 只有**整个键都没有**才是漏标。
        if "reasoning_echo_fill" not in e:
            missing.append(k)
    assert not missing, (
        "这些档按旧启发式需要补 reasoning_content，但目录里**没有显式标记** "
        "（reasoning_echo_fill）⇒ 收口后行为会变（该补的不补 ⇒ 400）：\n  "
        + "\n  ".join(missing))


# ================================================================ 3 · 判据函数自身

def test_echo_fill_kind_reads_config_only():
    """gateway 的回传判据只认配置标记，不再看模型名。"""
    from src.model_gateway import gateway
    assert gateway._echo_fill_kind({"reasoning_echo_fill": "placeholder"}) == "placeholder"
    assert gateway._echo_fill_kind({"reasoning_echo_fill": ""}) == "empty"
    assert gateway._echo_fill_kind({}) == ""
    # 旧字段仍认（兼容旧配置）
    assert gateway._echo_fill_kind({"echo_reasoning": True}) == "placeholder"
    # **不认模型名**（改前 "deepseek" in model_name 决定一切）
    assert gateway._echo_fill_kind({"model": "deepseek-v4-pro"}) == ""
    assert not hasattr(gateway, "_is_deepseek_like"), (
        "名字启发式判定应当移除（模型换代/改名就悄悄失效）")
