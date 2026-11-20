# -*- coding: utf-8 -*-
"""批二验收（2026-11-20 · 成本优化任务书 §三）：清单/子卡现状走当轮输入口。

判据（任务书 §三.1/§三.3）：
- 同一份材料从 system 挪进 tail 当轮事实消息——文本一字不动、带来源标记；
- 每个逻辑回合同版本只出现一次（tail 里恰一条）；工具续跳仍看得到
  （续跳 compose 同 ctx ⇒ 同一份消息原样再发）；
- 快照更新 ⇒ 下一个跳次拿到新版本（ctx 重装配即新版本，无需通知）；
- 反向闸：system_prompt 不再含两区正文（稳定前缀不再随卡面变化分叉）。
"""
from __future__ import annotations

from src.api import context_regions as cr


def _ctx(children=None, cards=None):
    cards = cards if cards is not None else [
        {"card_id": "card_001", "title": "根", "parent": "", "mission": "m"},
        {"card_id": "card_002", "title": "子", "parent": "card_001", "mission": "k"},
    ]
    return cr.RegionCtx(
        work_id="w", card_id="card_001", work_card_id="card_001",
        cards=cards,
        cur_meta={"card_id": "card_001", "card_type": "sub_project",
                  "mission": "m", "auto_mode": 0},
        direct_child_status=children if children is not None else [
            {"card_id": "card_002", "title": "子", "activity": "已暂停",
             "mission": "k", "status_block": "半"},
        ],
    )


def _tail_texts(out):
    return [m["content"] for m in out["turn_tail"]]


def test_regions_moved_out_of_system():
    """两区不再进 system_prompt；tail 各一条 assistant 消息、带来源标记。"""
    out = cr.compose_model_context(_ctx(), profile="main")
    sp = out["system_prompt"]
    assert "## 卡片清单" not in sp and "## 直接子卡" not in sp
    texts = _tail_texts(out)
    cl = [t for t in texts if t.startswith("〔卡片清单〕")]
    cs = [t for t in texts if t.startswith("〔直接子卡现状〕")]
    assert len(cl) == 1 and len(cs) == 1, "同版本只出现一次"
    # 材料一字不动：区头、卡行、活性行原样在
    assert "## 卡片清单" in cl[0] and "card_002 · 子" in cl[0]
    assert "## 直接子卡（活性·使命·现状）" in cs[0]
    assert "**活性**：已暂停" in cs[0]
    assert all(m["role"] == "assistant" for m in out["turn_tail"])


def test_tail_order_facts_before_injections():
    """tail 内排序：清单/子卡（order 100/170）先于工作规范注入（1010）。"""
    out = cr.compose_model_context(
        _ctx(), profile="main",
        turn_tail=[{"role": "assistant", "content": "〔工作规范〕\n流程"}])
    texts = _tail_texts(out)
    i_list = next(i for i, t in enumerate(texts) if t.startswith("〔卡片清单〕"))
    i_kid = next(i for i, t in enumerate(texts) if t.startswith("〔直接子卡现状〕"))
    i_spec = next(i for i, t in enumerate(texts) if t.startswith("〔工作规范〕"))
    assert i_list < i_kid < i_spec


def test_continuation_compose_same_version():
    """续跳复用：同一 ctx 再 compose ⇒ tail 消息逐字一致（同一份随每跳下发）。"""
    ctx = _ctx()
    a = cr.compose_model_context(ctx, profile="main")
    b = cr.compose_model_context(ctx, profile="main")
    assert _tail_texts(a) == _tail_texts(b)


def test_updated_snapshot_new_version():
    """子卡状态更新 ⇒ 新装配快照携带新版本（更新可见，不靠通知）。"""
    old = cr.compose_model_context(_ctx(), profile="main")
    new_ctx = _ctx(children=[{"card_id": "card_002", "title": "子",
                              "activity": "运行中 · tool_call",
                              "mission": "k", "status_block": "新版现状"}])
    new = cr.compose_model_context(new_ctx, profile="main")
    cs_new = next(t for t in _tail_texts(new) if t.startswith("〔直接子卡现状〕"))
    assert "新版现状" in cs_new and "运行中 · tool_call" in cs_new
    assert _tail_texts(old) != _tail_texts(new)


def test_empty_child_status_no_message():
    """无子卡/全空 ⇒ 子卡区消息不出现（「没有来源就不出现」口径不变）。"""
    out = cr.compose_model_context(_ctx(children=[]), profile="main")
    assert not any(t.startswith("〔直接子卡现状〕") for t in _tail_texts(out))


def test_viewpoint_profile_also_tail():
    """三席同一份材料走 tail（席位共享卡片上下文，通道一致）。"""
    out = cr.compose_model_context(
        _ctx(), profile="viewpoint",
        seat_identity=[{"role": "assistant", "content": "〔三席身份〕\n席"}])
    texts = _tail_texts(out)
    assert any(t.startswith("〔卡片清单〕") for t in texts)
    assert any(t.startswith("〔三席身份〕") for t in texts)
    assert "## 卡片清单" not in out["system_prompt"]
