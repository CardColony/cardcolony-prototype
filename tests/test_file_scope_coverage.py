"""FILE_SCOPE 覆盖性护栏。

**为什么有这个测试**：同一个 bug 已经犯过三次——

  · 2026-10-15 补 `project` / `coder`（漏抄致根卡与编程卡全被拒）
  · 2026-10-22 补 `role`（配了 tools_read_file，FILE_SCOPE 无键 ⇒ 权限等于没给）
  · 2026-10-26 补 `function`（职能卡 fn_NNN 同上，一调 read_file 就 denied）

机制：`check_file_access` 最后一步是
``hit = any(_prefix_hit(target, p) for p in FILE_SCOPE.get(card_type, []))``
——**类型不在表里 ⇒ 取到空列表 ⇒ any([]) 恒 False ⇒ 一律拒**。
所以「配了文件权限但表里没这个类型」＝「权限发了却用不了」，且**不报错、
只是 denied**，极难从现象反推。

本测试把这条隐含契约钉住：permissions.json 里凡是配了 `tools_file` /
`tools_read_file` 的卡类型，FILE_SCOPE 必须有对应键，且值不得为空列表。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.api import file_tools  # noqa: E402

PERMISSIONS_PATH = BASE_DIR / "config" / "permissions.json"
FILE_SWITCHES = {"tools_file", "tools_read_file"}


def _load_permissions() -> dict:
    """读仓库里的权限表本体（不走 conftest 的沙箱重定向——测的就是这份）。"""
    with PERMISSIONS_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _types_with_file_perms() -> dict[str, list[str]]:
    """{卡类型: 它拿到的文件类开关}（合并所有 project_class 下的同名类型）。"""
    data = _load_permissions()
    table = data.get("permissions") or {}
    out: dict[str, list[str]] = {}
    for _pc, by_type in table.items():
        if not isinstance(by_type, dict):
            continue
        for card_type, switches in by_type.items():
            if not isinstance(switches, list):
                continue
            hit = [s for s in switches if s in FILE_SWITCHES]
            if hit:
                out.setdefault(card_type, [])
                out[card_type] = sorted(set(out[card_type]) | set(hit))
    return out


def test_every_file_perm_type_has_scope_key():
    """配了文件权限的每个卡类型，FILE_SCOPE 必须有键。

    缺键 ⇒ check_file_access 恒拒（权限发了却用不了）。
    """
    missing = [t for t in _types_with_file_perms() if t not in file_tools.FILE_SCOPE]
    assert not missing, (
        "以下卡类型在 permissions.json 里配了文件权限，但 FILE_SCOPE 没有对应键，"
        f"导致一调就 denied：{missing}\n"
        "修法：在 src/api/file_tools.py 的 FILE_SCOPE 里补上（值为 [\"\"] 全放行哨兵）。"
    )


def test_scope_values_are_not_empty_lists():
    """键存在但值是空列表，效果与缺键完全相同（any([]) 恒 False）——一并挡掉。"""
    empty = [k for k, v in file_tools.FILE_SCOPE.items() if not v]
    assert not empty, f"FILE_SCOPE 以下键的值是空列表（等于全拒）：{empty}"


def test_function_type_is_covered():
    """职能卡（function）显式覆盖——2026-10-26 的回归点。"""
    assert "function" in file_tools.FILE_SCOPE, (
        "FILE_SCOPE 缺 'function' 键：职能卡（fn_NNN）的 tools_read_file 会失效"
    )


@pytest.mark.parametrize("card_type", ["project", "sub_project", "topic", "coder",
                                       "tool", "role", "function"])
def test_all_known_card_types_present(card_type):
    """已知卡类型逐个在位（类型词表扩容时这条会红，提醒同步 FILE_SCOPE）。"""
    assert card_type in file_tools.FILE_SCOPE
