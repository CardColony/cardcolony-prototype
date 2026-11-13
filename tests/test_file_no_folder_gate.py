# -*- coding: utf-8 -*-
"""[文件夹闸退役 2026-11-15] 文件访问新契约：能力闸＋凭证黑名单，路径不设限。

**为什么有这份测试**：原机制在「卡型开关」之上另叠了两层文件夹闸——
ALLOWED_ROOTS（限 D:\\AI_station）＋ FILE_SCOPE（card_type→路径前缀，
缺键即全拒）。后者同一 bug 犯过六次（project/worker/role/function/review/
ui·qa·pm/align——每次新卡型漏抄键，文件权限就静默失效）。
2026-11-15 用户令：「取消文件夹访问的机械闸，读写都不限文件夹」——
卡型的文件读写开关（tools_file/tools_read_file）管身份，路径不设限。
本文件钉住新契约，防文件夹闸被悄悄加回。
"""

from __future__ import annotations

from src.api import file_tools
from src.storage import card_store


def _registry_card_types() -> set[str]:
    """从类型注册表实读 card_type 全集（含 root scope 的 project）。"""
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


class TestNoFolderGate:
    def test_every_card_type_reaches_anywhere(self):
        """每个已注册 card_type 带 tools_file ⇒ 任意路径放行（含基准目录外）。

        这是第六次 FILE_SCOPE 缺键 bug 的根治点：不再有按卡型查的路径表，
        新卡型不可能再因「漏抄键」被拒。
        """
        outside = r"C:\Windows\System32"  # 基准目录外，旧机制必拒
        for ct in sorted(_registry_card_types()):
            card = _card(ct, ["read", "tools_file"])
            assert file_tools.check_file_access(outside, card), \
                f"{ct} 型卡被拒——文件夹闸疑似复活"

    def test_project_root_card_reads_anywhere(self):
        """根卡（project）带 tools_file：项目根与基准目录外都放行。"""
        card = _card("project", ["read", "tools_file"])
        assert file_tools.check_file_access(
            str(file_tools.BASE_DIR), card)
        assert file_tools.check_file_access(
            str(file_tools.BASE_DIR / "推演agent" / "config"), card)
        assert file_tools.check_file_access(r"D:\Temp", card)
        assert file_tools.check_file_access(r"C:\Windows", card)

    def test_relative_path_still_anchored(self):
        """相对路径仍以 BASE_DIR 为锚（语义不是边界，是"相对"的基准）。"""
        card = _card("project", ["read", "tools_file"])
        assert file_tools.check_file_access("推演agent", card)
        p, err = file_tools._resolve("推演agent/config")
        assert err is None and p == (file_tools.BASE_DIR / "推演agent/config").resolve()

    def test_no_file_switch_still_denied(self):
        """无文件开关的卡照旧被拒——能力闸不动，只撤文件夹闸。"""
        card = _card("project", ["read", "shadow_write"])
        assert file_tools.check_file_access(
            str(file_tools.BASE_DIR / "推演agent"), card) is False
        assert file_tools.check_file_access(r"D:\Temp", card) is False

    def test_read_switch_cannot_write(self):
        """只读档照旧：tools_read_file 读放行、写仍拒。"""
        card = _card("sub_project", ["read", "tools_read_file"])
        assert file_tools.check_file_access(r"D:\Temp", card) is True
        assert file_tools.check_file_access(r"D:\Temp", card, write=True) is False

    def test_credential_still_excluded(self):
        """凭证文件名黑名单不动（它是文件闸不是文件夹闸）。"""
        card = _card("project", ["read", "tools_file"])
        assert file_tools.check_file_access(
            str(file_tools.BASE_DIR / ".env"), card) is False
        assert file_tools.check_file_access(r"D:\x\id_rsa", card) is False

    def test_retired_symbols_gone(self):
        """闸体本身不复存在——防将来悄悄加回。"""
        assert not hasattr(file_tools, "ALLOWED_ROOTS")
        assert not hasattr(file_tools, "FILE_SCOPE")
        assert not hasattr(file_tools, "_prefix_hit")

    def test_resolve_outside_base_dir(self):
        """_resolve 不再判边界：基准外绝对路径与 .. 逃逸都解析放行。"""
        p, err = file_tools._resolve(r"C:\Windows\System32")
        assert err is None and p is not None
        p, err = file_tools._resolve("../outside.txt")
        assert err is None  # resolve 后是真实路径，无越界概念
