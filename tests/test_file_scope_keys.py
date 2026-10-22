# -*- coding: utf-8 -*-
"""[FILE_SCOPE 键位修复 2026-10-15] 路径锁键集与 card_type 全集对齐（红测转绿）。

**为什么有这份测试**：FILE_SCOPE 原表误抄权限表的五键，其中两个是 project_class
工牌码（永不可能作为 card_type 查到），而根卡真实 card_type=`project` 与编程卡
`coder` 都不在表内 → `FILE_SCOPE.get("project", [])` 返空列表 → `any([])` 恒 False
→ **11 张 project 根卡 tools_file 开关明明已授却被路径锁第三段拒死**（用户 2026-10-15
17:20 报 work_009/card_001 list_files「越权：无权访问 D:\AI_station\推演agent」）。
该错在 10-08 已被预判上报（THINK-ECO-permgmt-p1 调研纪要 T2「project 根卡不在表内、
落默认全拒」），因本表未复核故漏至今。这份测试把该预判固化为常态检查。

**沙箱纪律**：conftest.sandbox 是 autouse，把 BASE_DIR/ALLOWED_ROOTS/BACKUP_DIR
搬进 tmp_path。故本文件**不硬编码 D:\\AI_station**，一律用 file_tools.BASE_DIR
现场拼路径——否则第二段（ALLOWED_ROOTS）必然越界，测的就不是第三段了。
"""

from __future__ import annotations

from src.api import file_tools
from src.storage import card_store


def _registry_card_types() -> set[str]:
    """从类型注册表实读 card_type 全集（含 root scope 的 project）。

    不硬编码类型清单——与「卡片职责区」同一纪律：新增一种卡即自动进入本断言，
    逼 FILE_SCOPE 补键，而不是让它静默落进「查不到 → 全拒」。
    """
    reg = card_store.load_type_registry()
    types: set[str] = set()
    for vocab in (reg.get("l1_vocabularies") or {}).values():
        for t in vocab.get("types") or []:
            code = (t or {}).get("code")
            if code:
                types.add(code)
    return types


def _card(card_type: str, perms: list[str]) -> dict:
    return {"card_id": "c", "card_type": card_type, "perms": perms}


# [规则卡 2026-10-15 · 随版本推进翻新] rule 卡有意排除在路径锁键集对账之外：
# 权限底座 v5 给 rule 的 perms 只有 read（用户裁定·红线2「模型永远不能直接写
# 规则」的同源纪律）——它永无 tools_file 开关，路径锁第三段对它是恒拒死路径
# （缺键＝拒＝安全方向，零生产影响）；FILE_SCOPE 属 §四 白名单外文件，零触碰。
_NO_FILE_SCOPE_TYPES = {"rule", "role"}


class TestFileScopeKeys:
    def test_keys_equal_card_type_registry(self):
        """**注册表**里的 card_type 一个不许漏（－无文件权限的 rule）。不等即红灯。

        [只读档 2026-10-22] 从「完全相等」改为「注册类型必须全覆盖」：
        FILE_SCOPE 现在多了 ``role``——角色卡不在 l1_vocabularies 里（它是离树的
        职能类型，5710 引入时未进注册表），但它 2026-10-21 配了 ``tools_read_file``
        （「能核实交付物，但不给它写权和执行权」）。旧断言用相等，等于**逼着**
        把 role 从表里删掉、让那份只读权限作废（实测就是这样：能看见读工具、
        一调就被第三段拒死）。不变量本意是「新注册类型不许漏抄」，子集断言才准。
        """
        missing = (_registry_card_types() - _NO_FILE_SCOPE_TYPES) - set(file_tools.FILE_SCOPE)
        assert not missing, f"这些 card_type 漏了 FILE_SCOPE 键（会全拒）: {missing}"

    def test_no_project_class_keys(self):
        """工牌码不得混入：FILE_SCOPE 查的是 card_type，工牌键永不可达＝死键。

        死键比缺键更隐蔽——它让表「看起来」覆盖了两种分类，实际一种都拦不住。
        """
        reg = card_store.load_type_registry()
        classes = {c.get("code") for c in (reg.get("project_classes") or [])}
        assert not (set(file_tools.FILE_SCOPE) & classes)

    def test_every_card_type_reaches_file(self):
        """每个已注册 card_type（－rule，见文件头排除说明）都能过路径锁第三段。"""
        path = str(file_tools.BASE_DIR / "推演agent")
        for ct in sorted(_registry_card_types() - _NO_FILE_SCOPE_TYPES):
            card = _card(ct, ["read", "tools_file"])
            assert file_tools.check_file_access(path, card), \
                f"{ct} 型卡的路径锁被拒（FILE_SCOPE 缺键）"

    def test_project_root_card_not_denied(self):
        """本 bug 的直接回归点：project 根卡带 tools_file 必须放行。

        路径取 ``BASE_DIR/推演agent``——沙箱下 ``ALLOWED_ROOTS`` 被 conftest 收窄为
        两个子目录（10-03 那版 A 档口径），BASE_DIR 根本身不在其内，故不可拿它当
        「应放行」的样本。
        """
        base = file_tools.BASE_DIR / "推演agent"
        card = _card("project", ["read", "tools_file"])
        assert file_tools.check_file_access(str(base), card)
        assert file_tools.check_file_access(str(base / "config"), card)

    def test_relative_path_also_allowed(self):
        """相对路径（以 BASE_DIR 为基准）同样过。"""
        card = _card("project", ["read", "tools_file"])
        assert file_tools.check_file_access("推演agent", card)

    def test_no_tools_file_still_denied(self):
        """无 tools_file 的卡照旧被第一段拒（本修复不得放宽权限位本身）。"""
        card = _card("project", ["read", "shadow_write"])
        assert file_tools.check_file_access(
            str(file_tools.BASE_DIR / "推演agent"), card) is False

    def test_out_of_allowed_roots_still_denied(self):
        """越出 ALLOWED_ROOTS 仍拒（第二段未被本修复动过）。"""
        card = _card("project", ["read", "tools_file"])
        assert file_tools.check_file_access(r"C:\Windows\System32", card) is False

    def test_credential_still_excluded(self):
        """凭证排除层未受影响。"""
        card = _card("project", ["read", "tools_file"])
        assert file_tools.check_file_access(
            str(file_tools.BASE_DIR / ".env"), card) is False