"""工具说明唯一源（`tool_doc`）的事实校验。

三条判据：

1. **对照事实**：填了 `doc` 的工具，`effect` 声明必须与 handler 实现一致
   ——AST 扫 handler 源码，声明落会话块就必须找得到 `append_card_message`。
   说明不能撒谎，这条是硬约束。
2. **模型要知道场景与用法**：渲染出的说明必须同时含「什么时候用」与「怎么用」。
3. **不许新增未填**：未填 `doc` 的工具只能是 `PENDING` 里登记的存量——
   新加一件工具没填 doc，本测试红。填完一件从 `PENDING` 删一件。
"""

from __future__ import annotations

import pytest

from src.api import card_tools
from src.api.tool_doc import (GROUP_DUTY, GROUP_ORDER, ToolDoc, ToolEffect,
                              render_description, render_section, verify_effect)

#: 尚未填 doc 的存量（过渡期登记表）。**只能减，不能增。**
#: 填完一件就删一行——删空之后本测试改为「必须为空」。
PENDING: frozenset[str] = frozenset({
    "approve",
    "archive_card",
    "bash",
    "confirm_with_user",
    "continue_task",
    
    "create_card",
    "create_worker_card",
    "create_function",
    "create_goal",
    "curate_face",
    "decide",
    "delete_card",
    "delete_file",
    "design_policy",
    "draft_rule_edit",
    "edit_card",
    "edit_file",
    "fetch_web",
    "function_watch_cards",
    "get_goal",
    "glob_files",
    "link_cards",
    "link_rel",
    "list_files",
    "list_functions",
    "list_manuscript",
    "memory_expand",
    "move_file",
    "open_review",
    "promote_draft",
    "propose_status",
    "python",
    
    "read_archive",
    "read_card",
    "read_file",
    "read_linked",
    "read_manuscript",
    "read_memory",
    "read_shadow",
    
    "rename_file",
    "reparent_card",
    "report_done",
    "report_to_user",
    "reject",
    "resume_child",
    "revoke_approve",
    "search_files",
    "search_manuscript",
    "set_child_mission",
    "set_criteria",
    "set_function_duty",
    "silence_card",
    "stage_open",
    "stop_card",
    "unarchive_card",
    "unlink_cards",
    "unlink_rel",
    "update_function_profile",
    "update_goal",
    "web_search",
    "write_file",
})


def _docs() -> dict[str, ToolDoc]:
    out: dict[str, ToolDoc] = {}
    for name, d in card_tools.TOOL_REGISTRY.items():
        doc = getattr(d, "doc", None)
        if doc is not None:
            out[name] = doc
    return out


def _handler(name: str):
    return card_tools.TOOL_REGISTRY[name].handler


# ---------------------------------------------------------------- 判据 1
def test_effects_match_implementation():
    """声明了的效果，handler 里必须找得到对应调用（说明不许撒谎）。"""
    docs = _docs()
    assert docs, "至少要有已填 doc 的工具，否则本测试空转"
    bad: list[str] = []
    for name, doc in docs.items():
        bad.extend(verify_effect(name, doc, _handler(name)))
    assert not bad, "声明与实现不符：\n" + "\n".join(bad)


def test_verifier_catches_a_lie():
    """校验器自身有效性：故意声明错的落点，必须被抓出来。

    没有这条，`verify_effect` 一旦退化成恒返回空，上面那条会一直绿。
    """
    lie = ToolDoc(group="通信", scene="x", usage="y",
                  effect=ToolEffect(sink="inbox"))
    problems = verify_effect("sync", lie, card_tools.TOOL_REGISTRY["sync"].handler)
    assert problems, "sync 实际落会话块，声明成 inbox 却没被抓出来"


def test_verifier_catches_missing_wake():
    """声明会唤醒，但 handler 里没有 deliver_now ⇒ 必须报。"""
    lie = ToolDoc(group="通信", scene="x", usage="y",
                  effect=ToolEffect(sink="session_block", wake=True))
    problems = verify_effect("sync", lie, card_tools.TOOL_REGISTRY["sync"].handler)
    assert any("wake" in p for p in problems), problems


# ---------------------------------------------------------------- 判据 2
def test_rendered_doc_has_scene_and_usage():
    """渲染结果必须同时回答「什么时候用」和「怎么用」。"""
    for name, doc in _docs().items():
        text = render_description(name, doc)
        assert "什么时候用" in text, name
        assert "怎么用" in text, name
        assert len(text) >= 40, f"{name} 说明太薄：{text}"


def test_group_must_be_known():
    for name, doc in _docs().items():
        assert doc.group in GROUP_ORDER, f"{name} 的 group={doc.group} 不在 GROUP_ORDER"
        assert GROUP_DUTY.get(doc.group), f"{name} 的 group 缺职责说明"


# ---------------------------------------------------------------- 判据 3
def test_no_new_undocumented_tool():
    """新加工具必须同时写 doc——没写就红。"""
    missing = set(card_tools.TOOL_REGISTRY) - set(_docs())
    new = missing - PENDING
    assert not new, (
        "这些工具没有 doc（新增工具必须同时写 group/scene/usage/effect）：\n"
        + "\n".join(f"  - {n}" for n in sorted(new))
    )


def test_pending_list_is_honest():
    """PENDING 里不许留已经填过的名字（否则表会烂掉）。"""
    stale = set(PENDING) & set(_docs())
    assert not stale, f"这些已经填了 doc，请从 PENDING 删掉：{sorted(stale)}"


# ---------------------------------------------------------------- 渲染整节
def test_render_section_only_lists_given_tools():
    """工具节只渲染**传入的工具**——结构性防「列了拿不到的」。"""
    docs = _docs()
    if not docs:
        pytest.skip("还没有填 doc 的工具")
    one = next(iter(docs))
    text = render_section([one], docs)
    assert one in text
    for other in docs:
        if other != one:
            assert f"**{other}**" not in text, "不该渲染没传进来的工具"
