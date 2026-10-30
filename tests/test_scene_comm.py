# -*- coding: utf-8 -*-
"""[四场景通信+批次评审 2026-10-25] 测试。

钉七件事:
T1 review_request 开批次:在职职能入应到名单;**无去重闸**——
   模型再拉就再开新批次(要不要重拉由模型判断,代码不拦 2026-10-25 裁定)
T2 submit_review 入批次:未齐不唤醒;missing 正确
T3 收齐判定(纯状态):最后一人提交 ⇒ complete=True,批次关闭,
   总裁被唤醒一次(汇总投递)
T4 全员失能 ⇒ 批次作废(void),回执发起方——当次判定非超时
T5 sync:不唤醒(收件箱入信,无 TRIGGERS/事件),多收件人/缺省沿连线
T6 direct:子树权限(兄弟拒/总裁全树),acceptance 随信
T7 send_card_message 退役:任何卡的可见面都没有它
"""
from __future__ import annotations

import pytest

from src.api import card_tools, function_room
from src.storage import card_store, function_store, review_batches, work_store


def _use_repo_catalog(monkeypatch) -> None:
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH,
                       str(function_room._DEFAULT_PATH))
    function_room.reset_cache()


async def _seed(monkeypatch, title="四场景"):
    _use_repo_catalog(monkeypatch)
    w = await work_store.create_work(title)
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    await function_room.seed_work(wid)
    return wid, root


async def _sub(wid, root, parent):
    return (await card_store.create_card(
        wid, "干活卡", "模型生成", parent=parent))["meta"]["card_id"]


# ------------------------------------------------------- T1



# ------------------------------------------------------- T1b(线上事故钉)



# ------------------------------------------------------- T2



# ------------------------------------------------------- T3



# ------------------------------------------------------- T4



# ------------------------------------------------------- T5

async def test_sync_no_wake_inbox_only(sandbox, monkeypatch):
    wid, root = await _seed(monkeypatch)
    sub = await _sub(wid, root, root)
    peer = (await card_store.create_card(wid, "同伴", "模型生成",
                                        parent=root))["meta"]["card_id"]
    # [2026-10-27 · 用户裁定「统一用通信线」] 兄弟之间没有默认线，先建一条。
    # 改前显式 card_ids 可以绕过线，现在不行。
    await card_store.update_card(
        wid, sub, {"links": [{"target": peer, "type": "关联"}]}, "建通信线")
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools._h_scene_sync(
        wid, sub, {"content": "我改了接口:X 现在返回两个字段",
                   "card_ids": [peer]}, cards)
    assert out.get("ok") is True and len(out["delivered"]) == 1
    # [2026-10-26 · 用户裁定] 同步消息落成**会话块**（永久在上下文里），
    # 不再走收件箱——收件箱是水位消费，读一次就永远查不到。
    from src.storage import session_facade
    blocks = await session_facade.read(wid, peer) or []
    texts = [str(b.get("content") or "") for b in blocks]
    assert any("改了接口" in t for t in texts), texts
    # 不唤醒：没有起轮（收件箱也不该有残留）
    from src.storage import card_db as _cdb
    assert await _cdb.list_pending(wid, peer) == []


# ------------------------------------------------------- T5b（块形状全链路）

async def test_card_message_block_shape(sandbox, monkeypatch):
    """卡间消息块的三面必须同时成立（缺一面就出问题）：

    ① 会话块 `actor=card:<发信卡>` —— 前端 `routes.py:748` 认这个前缀，按消息块
       渲染，**不会冒充成用户说的话**；
    ② 会话块 `role=user` —— 上下文装配（`_materialize_v2_block`）把 card: 映射成
       user，模型侧照样当外部输入看得到；
    ③ 事件流 type = **msg_sync**（不是 user_message）—— 判据据此区分同步/汇报。
    """
    from src.storage import event_log, session_facade
    wid, root = await _seed(monkeypatch)
    sub = await _sub(wid, root, root)
    peer = (await card_store.create_card(wid, "同伴", "模型生成",
                                        parent=root))["meta"]["card_id"]
    await session_facade.append_card_message(
        wid, peer, from_card=sub, text="我改了接口:X 返回两个字段",
        msg_type="msg_sync")
    await event_log.drain()

    blocks = await session_facade.read(wid, peer) or []
    b = [x for x in blocks if "我改了接口" in str(x.get("content"))][-1]
    assert str(b.get("actor") or "").startswith("card:"), b
    assert str(b.get("role") or "") == "user", b
    # 首行是结构化头（前端解析 id/type/from）
    from src.api import card_msg
    hdr = card_msg.parse_header(str(b.get("content") or ""))
    assert hdr and hdr.get("type") == "msg_sync" and hdr.get("from") == sub, hdr

    evs = event_log.timeline(wid, peer, tail=30)
    assert any(str(e.get("type")) == "msg_sync" for e in evs), \
        [e.get("type") for e in evs]


# ------------------------------------------------------- T6

async def test_direct_scope_and_acceptance(sandbox, monkeypatch):
    wid, root = await _seed(monkeypatch)
    mid = (await card_store.create_card(wid, "父卡", "模型生成",
                                        parent=root))["meta"]["card_id"]
    child = (await card_store.create_card(wid, "子卡", "模型生成",
                                          parent=mid))["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)
    ok = await card_tools._h_scene_direct(
        wid, mid, {"card_id": child,
                   "instruction": "补错误处理",
                   "acceptance": "单测全绿"}, cards)
    assert ok.get("ok") is True
    # 兄弟拒
    other = (await card_store.create_card(wid, "别家", "模型生成",
                                          parent=root))["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)
    denied = await card_tools._h_scene_direct(
        wid, other, {"card_id": child, "instruction": "x"}, cards)
    assert denied.get("ok") is False
    # 总裁全树：**但要先有通信线**（[2026-10-27] 统一用通信线，
    # 改前根卡靠"全树"就能直接给孙子卡下指令，现在不行）
    await card_store.update_card(
        wid, root, {"links": [{"target": child, "type": "关联"}]}, "根卡建线")
    cards = card_tools._collect_card_meta(wid)
    # [通信收拢 2026-10-28] acceptance 必填（没有判据，收令方无法自判做完没有）
    ok2 = await card_tools._h_scene_direct(
        wid, root, {"card_id": child, "instruction": "复查一遍",
                    "acceptance": "逐条回读并给出结论"}, cards)
    assert ok2.get("ok") is True
    # [2026-10-26 · 记录补全] 指令改落**会话块 + msg_directive 事件**（带 from），
    # 不再只进收件箱——收件箱是水位消费，且原来连事件都没有。
    from src.storage import session_facade
    blocks = await session_facade.read(wid, child) or []
    texts = [str(b.get("content") or "") for b in blocks]
    assert any("复查一遍" in t for t in texts), texts


# ------------------------------------------------------- T7

async def test_send_card_message_retired(sandbox, monkeypatch):
    wid, root = await _seed(monkeypatch)
    sub = await _sub(wid, root, root)
    cards = card_tools._collect_card_meta(wid)
    for cid in (root, sub):
        tools = card_tools.build_tool_schemas(card_id=cid, work_id=wid,
                                              cards=cards)
        names = {t.get("name") for t in tools}
        assert "send_card_message" not in names, names
    # 场景件在
    # [通信收拢 2026-10-28] scene_report 已退役（与 report_up 同一件事）：
    # 作业卡横向只剩 sync，向上只剩 report_up。
    tools = card_tools.build_tool_schemas(card_id=sub, work_id=wid, cards=cards)
    names = {t.get("name") for t in tools}
    assert {"sync", "report_up"} <= names
    assert "scene_report" not in names, names
    # [伴生评审卡 2026-10-27] approve 只给职能（评审成员），子卡看不见。
    assert "approve" not in names, "approve 不进基础池"
