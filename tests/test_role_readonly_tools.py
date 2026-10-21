# -*- coding: utf-8 -*-
"""[只读文件开关 2026-10-21 · card_004] 职能角色能核实交付物，但拿不到写权和执行权。

**这条为什么必须有（不是洁癖，是边界）**
`tools_file` 一个开关管着 11 件工具：四个只读工具（read_file / list_files /
search_files / glob_files）之外，还有 **write_file / edit_file / delete_file /
move_file / rename_file / bash / python**。
直接把 tools_file 发给离树的职能角色，等于同时给写盘权和 shell 执行权——
与「第二层只说话、不动手」的结构定义正面冲突。

**判据是权限语义本身（结构事实）**
不是"角色有没有调用成功"，而是「给定一组 perms，各开关判出什么」。
这一层对了，工具面挂载才谈得上正确。
"""
from __future__ import annotations

import json

from src.permissions import policy


READER = {"perms": ["read", "tools_read_file"]}
WRITER = {"perms": ["read", "tools_file"]}
NEITHER = {"perms": ["read"]}


def test_readonly_card_can_read_but_cannot_write_or_exec():
    """核心断言：只读卡能读，但拿不到「读写」这档（因此也拿不到 bash/python）。"""
    assert policy.has_perm(READER, policy.SWITCH_TOOLS_READ_FILE) is True
    assert policy.has_perm(READER, policy.SWITCH_TOOLS_FILE) is False


def test_writer_implies_reader_one_way():
    """单向包含：有读写权者当然能读；但只读权不反推写权。"""
    assert policy.has_perm(WRITER, policy.SWITCH_TOOLS_FILE) is True
    assert policy.has_perm(WRITER, policy.SWITCH_TOOLS_READ_FILE) is True
    assert policy.has_perm(READER, policy.SWITCH_TOOLS_FILE) is False   # 反向不成立


def test_card_without_perms_gets_nothing():
    """默认关门：没配就是没有，不回落、不猜。"""
    assert policy.has_perm(NEITHER, policy.SWITCH_TOOLS_READ_FILE) is False
    assert policy.has_perm(NEITHER, policy.SWITCH_TOOLS_FILE) is False
    assert policy.has_perm(None, policy.SWITCH_TOOLS_READ_FILE) is False


def test_unknown_switch_still_denied():
    """词汇表外的开关一律拒（不存在的开关谈不上授权）。"""
    assert policy.has_perm(WRITER, "tools_something_not_defined") is False


def test_switch_registered_in_vocabulary():
    """新开关必须在 SWITCHES 里，否则 has_perm 第一步就拒它。"""
    assert policy.SWITCH_TOOLS_READ_FILE in policy.SWITCHES
    assert policy.SWITCH_TOOLS_READ_FILE == "tools_read_file"


def test_role_perms_in_config_include_readonly_but_not_tools_file():
    """配置真源核对：角色行拿到只读开关，且**没有** tools_file（否则等于给写权）。"""
    import pathlib

    cfg = json.loads(
        (pathlib.Path(__file__).resolve().parents[1]
         / "config" / "permissions.json").read_text(encoding="utf-8"))
    for cls in ("rd_project", "research_project"):
        role_perms = cfg["permissions"][cls]["role"]
        assert "tools_read_file" in role_perms, f"{cls}.role 缺 tools_read_file"
        assert "tools_file" not in role_perms, \
            f"{cls}.role 不该有 tools_file——那会连写盘与 shell 一起开给离树角色"


def test_coder_keeps_full_file_rights():
    """回归保护：拆开关不能削弱编程卡（它原来就有 tools_file，必须仍然全权）。"""
    import pathlib

    cfg = json.loads(
        (pathlib.Path(__file__).resolve().parents[1]
         / "config" / "permissions.json").read_text(encoding="utf-8"))
    coder_perms = cfg["permissions"]["rd_project"]["coder"]
    assert "tools_file" in coder_perms
    assert policy.has_perm({"perms": coder_perms}, policy.SWITCH_TOOLS_FILE) is True
    assert policy.has_perm({"perms": coder_perms}, policy.SWITCH_TOOLS_READ_FILE) is True