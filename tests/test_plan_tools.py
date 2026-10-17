# -*- coding: utf-8 -*-
"""工具层六件单测（迭代二 S2 原型；[思维链真实落地 2026-10-11] 判据化重写）。

验收原文＋判据化扩展：
- 单测逐工具一正一负（负用例验拒绝文案与幂等行为）；
- 注册后工具清单数量 +6（escalate_to_plan 随直答席机制退役）；
- step_complete 六条防护逐条测试桩制造：①缺板 ②重复交 ③参数空
  ④板已清理 ⑤断尾快照重建 ⑥锁超时——四态零异常零静默；
- criteria_met 缺失→「未推进」明文（判据自述必填，C2）；
- create_plan 返回值与参数中 steps[0].prompt 逐字相等（代码零加工证据），
  且带 criteria/deliverable 与附录 C 下一步引导。
全链路走 execute_tool（查表调用＋信封），不 import main（conftest 铁律二）。
"""
from __future__ import annotations

import json

import pytest

from src.api.card_tools import TOOL_REGISTRY, execute_tool
from src.storage import session_facade, session_store  # noqa: F401
from src.thinkchain import board as board_mod
from src.thinkchain import plan_tools

# ===========================================================================
# [纯单模型模式 2026-10-17 · 用户令] 本模块用例全为思维链行为（判型/拆解/上板/
# 步推进/三席统合/步事件）。生产已把思维链拆出链路（thinkchain.ENABLED=False）
# ⇒ 整模块跳过；开关置 True 即全部自动恢复（判据同并行线逐函数 skipif，此处
# 收敛为模块级，语义不变）。
# ===========================================================================
pytestmark = pytest.mark.skipif(
    not __import__("src.thinkchain", fromlist=["enabled"]).enabled(),
    reason="纯单模型模式（2026-10-17 用户令）：思维链已拆出生产链路",
)

# [工具面补齐 2026-10-12] 卡表：思维链六件现在有 plan 开关的执行层关口，
# 「认不出卡 ⇒ 默认关门」。本文件测的是板逻辑（建板/改板/推进/六防护），
# 不是权限——生产链路上游 message_runner 恒定传真实卡表，不会出现空卡表，
# 故这里用一张「任意 card_id 都授 plan 开关」的卡表，让关口放行、聚焦板逻辑。
# 权限本身由 tests/test_cardperm_readopen.py 的 test_plan_tools_have_exec_gate
# 单独钉住（无 plan 开关 ⇒ 六件全拒、工具面全隐藏）。
class _AnyCardWithPlan(dict):
    """任意 card_id → 一张授 plan 开关的卡。

    ``__len__`` 必须给非零：关口里写的是 ``(cards or {}).get(...)``，
    ``dict`` 的真值走 ``__len__``，长度为 0 会让 ``cards or {}`` 短路成空
    dict ⇒ 关口看不见卡 ⇒ 全拒（本文件 15 条就是这么挂的）。
    """
    def __len__(self) -> int:
        return 1          # 「卡表里确实有卡」——只影响真值，不参与遍历
    def __contains__(self, key: object) -> bool:
        return bool(key)
    def get(self, key: object, default: object = None) -> object:
        return {"card_id": str(key), "card_type": "sub_project",
                "perms": ["read", "edit", "create_child", "link", "unlink",
                          "plan", "shadow_write"]}


EMPTY_CARDS: dict[str, dict] = _AnyCardWithPlan()


@pytest.fixture(autouse=True)
def _reset_board_registry():
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()
    yield
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()


def _steps(*prompts: str) -> list[dict]:
    # 新契约：criteria/deliverable 必填
    return [{"id": f"s{i + 1}", "prompt": p, "criteria": f"{p}的判据",
             "deliverable": f"{p}的产出", "type": "execute"}
            for i, p in enumerate(prompts)]


async def _create(work_id: str, card_id: str, *prompts: str) -> dict:
    r = await execute_tool(work_id, card_id, "create_plan",
                           {"name": "测试计划", "steps": _steps(*prompts)}, EMPTY_CARDS)
    assert r["ok"] is True, r
    return r["data"]


# ------------------------------------------------------------- 注册 +6

def test_registry_plus_six():
    names = set(TOOL_REGISTRY.keys())
    six = {"create_plan", "plan_reorder", "plan_insert",
           "plan_remove", "plan_modify", "step_complete"}
    assert six <= names  # 全部注册在册
    assert "escalate_to_plan" not in names  # 逃生口随直答席机制退役
    assert not (six & _readonly_names())  # 六件全为写工具，不进只读白名单


def _readonly_names() -> set[str]:
    from src.api.card_tools import READONLY_TOOLS
    return set(READONLY_TOOLS)


# ------------------------------------------------------------- create_plan 一正一负

async def test_create_plan_returns_first_prompt_verbatim(work):
    wid, cid = work["work_id"], work["work_card_id"]
    data = await _create(wid, cid, "第一步要求原文", "第二步")
    # 验收逐字：返回值与参数中 steps[0].prompt 相等（代码零加工）
    assert data["prompt"] == "第一步要求原文"
    assert data["criteria"] == "第一步要求原文的判据"
    assert data["deliverable"] == "第一步要求原文的产出"
    assert data["total"] == 2
    # 板已落盘
    assert board_mod.plan_path(wid, cid).exists()


async def test_create_plan_negative(work):
    wid, cid = work["work_id"], work["work_card_id"]
    r0 = await execute_tool(wid, cid, "create_plan", {"name": "x", "steps": []}, EMPTY_CARDS)
    assert r0["ok"] is False and "非空" in r0["error"]["message"]
    dup = await execute_tool(wid, cid, "create_plan",
                             {"name": "x", "steps": [{"id": "a", "prompt": "1", "criteria": "c",
                                                      "deliverable": "d", "type": "execute"},
                                                     {"id": "a", "prompt": "2", "criteria": "c",
                                                      "deliverable": "d", "type": "execute"}]}, EMPTY_CARDS)
    assert dup["ok"] is False and "重复" in dup["error"]["message"]
    nocrit = await execute_tool(wid, cid, "create_plan",
                                {"name": "x", "steps": [{"id": "a", "prompt": "1",
                                                         "deliverable": "d", "type": "execute"}]},
                                EMPTY_CARDS)
    assert nocrit["ok"] is False and "criteria" in nocrit["error"]["message"]


# ------------------------------------------------------------- 改板四动作 一正一负

async def test_plan_reorder_positive_and_negative(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await _create(wid, cid, "a", "b", "c")
    r = await execute_tool(wid, cid, "plan_reorder",
                           {"order": ["s3", "s1", "s2"], "reason": "先做第三件"}, EMPTY_CARDS)
    assert r["ok"] is True, r
    assert [s["id"] for s in r["data"]["board"]["steps"]] == ["s3", "s1", "s2"]
    # 负例：order 不是全排列
    bad = await execute_tool(wid, cid, "plan_reorder", {"order": ["s1"]}, EMPTY_CARDS)
    assert bad["ok"] is False and "全排列" in bad["error"]["message"]


async def test_plan_insert_remove_modify(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await _create(wid, cid, "a", "b")
    # insert 正例
    r = await execute_tool(wid, cid, "plan_insert",
                           {"after_id": "s1", "step": {"id": "s9", "prompt": "插入步",
                                                       "criteria": "插入步判据",
                                                       "deliverable": "插入步产出", "type": "think"}},
                           EMPTY_CARDS)
    assert r["ok"] is True
    assert [s["id"] for s in r["data"]["board"]["steps"]] == ["s1", "s9", "s2"]
    assert r["data"]["board"]["steps"][1]["type"] == "think"
    # insert 负例：id 重复
    dup = await execute_tool(wid, cid, "plan_insert",
                             {"step": {"id": "s9", "prompt": "再插", "criteria": "c",
                                       "deliverable": "d", "type": "execute"}}, EMPTY_CARDS)
    assert dup["ok"] is False and "已存在" in dup["error"]["message"]
    # modify 正例（reason 留空不拒；新字段 criteria/deliverable/status）
    m = await execute_tool(wid, cid, "plan_modify",
                           {"id": "s9", "prompt": "改后的要求", "criteria": "改后的判据",
                            "status": "doing"}, EMPTY_CARDS)
    assert m["ok"] is True
    assert m["data"]["board"]["steps"][1]["prompt"] == "改后的要求"
    assert m["data"]["board"]["steps"][1]["criteria"] == "改后的判据"
    assert m["data"]["board"]["steps"][1]["status"] == "doing"
    # remove 正例（删非当前步）＋负例（当前步不可删）
    rm = await execute_tool(wid, cid, "plan_remove", {"id": "s9"}, EMPTY_CARDS)
    assert rm["ok"] is True
    cur = await execute_tool(wid, cid, "plan_remove", {"id": "s1"}, EMPTY_CARDS)
    assert cur["ok"] is False and cur["error"]["message"] == "当前步不可删"


async def test_revise_without_board_is_plain_text(work):
    wid, cid = work["work_id"], work["work_card_id"]
    r = await execute_tool(wid, cid, "plan_reorder", {"order": ["s1"]}, EMPTY_CARDS)
    assert r["ok"] is True and r["data"]["plan_active"] is False
    assert "已终结" in r["data"]["message"]


# ------------------------------------------------------------- step_complete 正常流

async def test_step_complete_advances_and_returns_next_prompt(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await _create(wid, cid, "做第一件事", "做第二件事")
    r = await execute_tool(wid, cid, "step_complete",
                           {"result": "第一件事做完了，结论是 X",
                            "criteria_met": "判据已满足：产出 X 在 result 中"}, EMPTY_CARDS)
    assert r["ok"] is True, r
    assert r["data"]["advanced"] is True
    assert r["data"]["prompt"] == "做第二件事"  # 下一步要求原文
    assert r["data"]["criteria"] == "做第二件事的判据"
    # 盘上指针同步＋result 落板
    disk = json.loads(board_mod.plan_path(wid, cid).read_text(encoding="utf-8"))
    assert disk["current"] == 1 and disk["steps"][0]["status"] == "done"
    assert disk["steps"][0]["result"] == "第一件事做完了，结论是 X"
    # step 块已落档（含判据满足自述）
    blocks = await session_facade.read(wid, cid)
    step_blocks = [b for b in blocks if b.get("behavior") == "execute_step"]
    assert step_blocks and "第一件事做完了" in step_blocks[-1]["content"]
    assert "判据满足" in step_blocks[-1]["content"]


async def test_step_complete_last_step_reports_all_done(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await _create(wid, cid, "唯一一步")
    r = await execute_tool(wid, cid, "step_complete",
                           {"result": "done", "criteria_met": "判据满足"}, EMPTY_CARDS)
    assert r["ok"] is True and r["data"]["all_steps_done"] is True


async def test_step_complete_missing_criteria_met_not_advanced(work):
    """criteria_met 缺失 → 未推进明文（判据自述必填）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await _create(wid, cid, "a", "b")
    r = await execute_tool(wid, cid, "step_complete", {"result": "x"}, EMPTY_CARDS)
    assert r["ok"] is True
    assert r["data"]["advanced"] is False
    assert "criteria_met" in r["data"]["message"]
    disk = json.loads(board_mod.plan_path(wid, cid).read_text(encoding="utf-8"))
    assert disk["current"] == 0  # 指针未动


# ------------------------------------------------------------- step_complete 六防护

async def test_guard_1_no_board_plain_text(work):
    wid, cid = work["work_id"], work["work_card_id"]
    r = await execute_tool(wid, cid, "step_complete",
                           {"result": "x", "criteria_met": "y"}, EMPTY_CARDS)
    assert r["ok"] is True  # 零异常：明文返回
    assert r["data"]["plan_active"] is False
    assert "本计划已终结，无可推进" in r["data"]["message"]


async def test_guard_2_duplicate_submit_idempotent(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]
    await _create(wid, cid, "a", "b")

    async def fake_mark(self, result_summary=""):
        return {"ok": True, "advanced": False, "idempotent": True,
                "prompt": "a", "all_steps_done": False}

    monkeypatch.setattr(board_mod.PlanBoard, "mark_done_and_advance", fake_mark)
    r = await execute_tool(wid, cid, "step_complete",
                           {"result": "重复交", "criteria_met": "y"}, EMPTY_CARDS)
    assert r["ok"] is True
    assert r["data"]["idempotent"] is True and r["data"]["advanced"] is False
    assert "已收过" in r["data"]["message"]


async def test_guard_3_empty_result_not_advanced(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await _create(wid, cid, "a", "b")
    r = await execute_tool(wid, cid, "step_complete",
                           {"result": "   ", "criteria_met": "y"}, EMPTY_CARDS)
    assert r["ok"] is True
    assert r["data"]["advanced"] is False
    assert "未推进" in r["data"]["message"]
    assert r["data"]["current_prompt"] == "a"  # 催促文带本步要求原文


async def test_guard_4_board_cleaned_stale_reference(work, monkeypatch):
    """防护④：handler 拿到的板在推进前被清理——明文已终结，绝不复活。"""
    wid, cid = work["work_id"], work["work_card_id"]
    stale = board_mod.PlanBoard(wid, cid, {
        "card_id": cid, "name": "n",
        "steps": [{"id": "s1", "prompt": "a", "criteria": "c", "deliverable": "d",
                   "type": "", "status": "pending", "result": ""}],
        "current": 0, "revisions": [], "created_at": "",
    })  # 不注册进 _boards → _require_live 必拒

    async def fake_get(w, c):
        return stale

    monkeypatch.setattr(board_mod, "get_board", fake_get)
    r = await execute_tool(wid, cid, "step_complete",
                           {"result": "x", "criteria_met": "y"}, EMPTY_CARDS)
    assert r["ok"] is True  # 零异常
    assert "本计划已终结" in r["data"]["message"]


async def test_guard_5_truncated_board_plain_error(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await _create(wid, cid, "a")
    board_mod._boards.clear()  # 模拟重启
    board_mod.plan_path(wid, cid).write_text('{"steps": [ 断尾', encoding="utf-8")
    r = await execute_tool(wid, cid, "step_complete",
                           {"result": "x", "criteria_met": "y"}, EMPTY_CARDS)
    assert r["ok"] is False  # 明报（err 信封带路径文案）
    assert r["error"]["code"] == "board_broken"
    assert str(board_mod.plan_path(wid, cid)) in r["error"]["message"]


async def test_guard_6_lock_timeout_plain_error(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await _create(wid, cid, "a")
    lock = board_mod._get_lock((wid, cid))
    await lock.acquire()  # 人工占锁
    try:
        r = await execute_tool(wid, cid, "step_complete",
                               {"result": "x", "criteria_met": "y"}, EMPTY_CARDS)
        assert r["ok"] is False
        assert r["error"]["code"] == "board_busy"
        assert "稍后重试" in r["error"]["message"]
    finally:
        lock.release()


# ------------------------------------------------------------- 档案块族（behavior 常量）

async def test_plan_and_revision_blocks_landed(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await _create(wid, cid, "a", "b")
    await execute_tool(wid, cid, "plan_modify", {"id": "s1", "prompt": "改"}, EMPTY_CARDS)
    blocks = await session_facade.read(wid, cid)
    behaviors = [b.get("behavior") for b in blocks]
    assert "plan" in behaviors and "revision" in behaviors
    # actor=tool、end 空串（不碰轮界）
    tool_blocks = [b for b in blocks if b.get("actor") == "tool"]
    assert tool_blocks and all(b.get("end") == "" for b in tool_blocks)
