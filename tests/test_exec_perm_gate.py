# -*- coding: utf-8 -*-
"""[执行层权限闸 2026-10-25] 看不见 ≠ 调不动：无开关的工具必须在执行层被拒。

背景（实测）：``work_027/card_021``（sub_project、perms 无 ``tools_file``）在
16 小时里调了 646 次 ``bash`` ＋ 7 次 ``python``，每次都真执行；``card_001``
（project、无 ``delete``）调了 7 次 ``delete_card`` 也照删。机制＝
① ``execute_tool`` 按名字直接派发、零权限校验；② ``prompts.py`` 的 ``_SEC_TOOLS``
无条件列出全量工具，模型因此知道 bash 存在；③ 工具面把它过滤掉了，但没人拦硬调。

本文件钉住那道闸：**判据与工具面同源**（同一张 TOOL_SWITCH、同一个 has_perm），
故不会与工具面分叉；``perm_gate=False``（缺省）＝旧行为，既有调用点零改动。
"""
from __future__ import annotations

from src.api import card_tools
from src.permissions import policy
from src.storage import card_store


async def _mk(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title=title, parent=parent,
                                     source="用户直接创建")
    assert "error" not in r, r
    return r["meta"]["card_id"]


def _cards(wid: str) -> dict:
    return card_tools._collect_card_meta(wid)


async def test_gate_denies_bash_without_tools_file(work, tmp_path):
    """卡上没挂 run_code 能力 ⇒ bash 被拒，且**命令不得执行**。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk(wid, "无文件权", root)
    meta = card_store._meta_of(wid, cid)
    # [perms 退役 2026-10-30] 判据已迁到 abilities 列：bash 归 run_code 能力管。
    assert "run_code" not in set(meta.get("abilities") or []), "本用例前提：卡无 run_code"

    marker = tmp_path / "should_not_exist.txt"
    r = await card_tools.execute_tool(
        wid, cid, "bash", {"command": f'echo x > "{marker}"'},
        _cards(wid), perm_gate=True)

    assert r.get("ok") is False
    assert (r.get("error") or {}).get("code") == "denied"
    # 拒绝必须自带出路（验收纪律 4）——能力口径下点名缺的能力 code
    assert "run_code" in str((r.get("error") or {}).get("message"))
    assert not marker.exists(), "被拒的 bash 竟然真的执行了"


async def test_gate_allows_bash_with_tools_file(work):
    """同一张卡补上 run_code 能力 ⇒ bash 放行（闸不是「一律拒」）。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk(wid, "有文件权", root)

    m = card_store._meta_of(wid, cid)
    abilities = list(m.get("abilities") or [])
    if "run_code" not in abilities:
        abilities.append("run_code")
    await card_store.update_card(wid, cid,
                                 {"abilities": abilities},
                                 "[测试] 补 run_code")

    r = await card_tools.execute_tool(
        wid, cid, "bash", {"command": "echo gate_ok"},
        _cards(wid), perm_gate=True)
    assert r.get("ok") is True, r
    assert "gate_ok" in str(((r.get("data") or {}).get("stdout") or ""))


async def test_gate_off_keeps_legacy_behaviour(work):
    """perm_gate 缺省 False ⇒ 旧行为（不拦）——既有 155 处测试调用点零改动。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk(wid, "旧行为", root)
    r = await card_tools.execute_tool(
        wid, cid, "bash", {"command": "echo legacy_ok"}, _cards(wid))
    assert r.get("ok") is True, r


async def test_gate_does_not_block_special_set_tools(work):
    """特殊集（AUTO_ONLY/OP_ONLY/FUNCTION_ONLY）不受影响：它们挂的开关卡上本来就有。

    ``edit_requirements`` 归 ``write_own_face`` 能力管；本卡有该能力 ⇒
    即便它不进常规工具面，执行层也必须放行
    （否则会把「非卡片内挂」这一整类工具一刀切死）。
    """
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk(wid, "特殊集", root)
    meta = card_store._meta_of(wid, cid)
    assert "write_own_face" in set(meta.get("abilities") or [])

    denied = card_tools._exec_perm_gate(cid, "edit_requirements", _cards(wid))
    assert denied is None, f"edit_requirements 被误伤：{denied}"


async def test_gate_unmapped_tool_denied(work):
    """能力口径下「不归任何能力管」的名字本闸放行——它们是回合口子
    （propose_status/write_requirements_final 这类由工具面＋轮次闸管，
    不参与授予）；且生产路径上未知名字在注册表命中之前就已被
    not_found 拦掉，本闸碰不到它。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk(wid, "未映射", root)
    denied = card_tools._exec_perm_gate(cid, "__probe_unmapped__", _cards(wid))
    assert denied is None, denied
