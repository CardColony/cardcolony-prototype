# -*- coding: utf-8 -*-
"""迭代四 K1 · 消息类型注册表与读取层 验收网。

覆盖：JSON 断言／八函数单测／头行解析固定向量集七类
（正常／缺字段／多余空格／嵌套方括号／中文内容／无头行／头行非首行——
后四类必须返 None 且不抛异常）。
K2/K3/K4 的工具侧与调起侧断言随后续件追加进本文件。
"""
from __future__ import annotations

import json
from pathlib import Path

import asyncio
import inspect

import pytest

from src.api import (card_msg, card_tools, event_stream, message_runner,
                     model_service)
from src.api import routes
from src.api import routes
from src.storage import work_store

REPO = Path(__file__).resolve().parents[1]
REGISTRY = REPO / "config" / "message_type_registry.json"


# ══════════════ JSON 断言 ══════════════

def test_registry_json_shape():
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    # [worker 卡 2026-10-15 · C1 指纹随版本推进] v1 → v2（新增 worker_done 完工回报）
    # [2026-10-26 · 系统消息] v3 → v4：＋msg_sync（同步）/ msg_report（汇报）——
    # 「同步过」与「汇报过」必须是不同事件，否则判据分不清（用户令：判断要准确）
    assert data["version"] == 4
    assert set(data["types"].keys()) == {"msg_ask", "msg_answer", "msg_notify",
                                         "worker_done", "msg_directive",
                                         "msg_sync", "msg_report"}
    for code, entry in data["types"].items():
        # [迭代五 S1] 键集由 {display, note} 扩为五键（when_to_use/reply_expected/
        # reply_type 为应对规则字段，S1 步骤①②③ 加键的镜像断言）。
        # [通信收拢 2026-10-28] 允许可选 `deprecated` 键（退役类型保留在册，
        # 历史消息还要渲染；清退≠删行，与 card_type_registry 的 legacy 同手法）。
        _base = {"display", "note", "when_to_use", "reply_expected", "reply_type"}
        assert set(entry.keys()) in (_base, _base | {"deprecated"}), code
        assert "judge" not in entry
    assert "judge" not in data


# ══════════════ 函数断言 ══════════════
# [通信收拢 2026-10-28] 本文件删掉 11 个用例（t4/t6/live_fire/k3×2/k4×4/s3×2）：
# 它们钉的是 **收件箱账本 + send_card_message 工具**这条已退役的链路
# （用户裁定「inbox 直接取消」「接口只有两个」）。新链路的钉子见
# `tests/test_comm_surface.py`（注册表 / direct 必带判据 / report 只到父卡）。
# 另删 k4 前端契约与 SSE 帧两条：它们钉的是 `/card-messages` 未读面——
# 该接口读消息账本，而账本自「inbox 取消」后不再有新行（空结果）。

def test_type_codes_is_triple():
    # [worker 卡 2026-10-15 · C1 指纹] 三型 → 四型（＋worker_done），断言语义不变
    # [2026-10-26] 再加 msg_sync / msg_report（同步与汇报必须是不同事件）
    assert card_msg.type_codes() == ("msg_ask", "msg_answer", "msg_notify",
                                     "worker_done", "msg_directive",
                                     "msg_sync", "msg_report")


def test_display_of_hit_and_miss():
    assert card_msg.display_of("msg_ask") == "询问"
    assert card_msg.display_of("msg_nope") == "msg_nope"


# ══════════════ [迭代五 S1] 应对规则读取函数（四新函数） ══════════════

def test_s1_entry_of_hit_and_miss():
    """三类型 entry 正确值＋未命中 None。"""
    ask = card_msg.entry_of("msg_ask")
    assert isinstance(ask, dict)
    assert ask["when_to_use"] == "你需要对方的信息或判断才能继续时发；发完等对方回话。"
    assert ask["reply_expected"] is True
    assert ask["reply_type"] == "msg_answer"
    assert card_msg.entry_of("msg_answer")["reply_expected"] is False
    assert card_msg.entry_of("msg_notify")["reply_type"] == ""
    assert card_msg.entry_of("msg_nope") is None


def test_s1_when_to_use_of_three_types_and_default():
    """三类型 when_to_use 正确值＋缺省路径 ""。"""
    assert card_msg.when_to_use_of("msg_ask") == \
        "你需要对方的信息或判断才能继续时发；发完等对方回话。"
    assert card_msg.when_to_use_of("msg_answer") == \
        "回应一条收到的询问时发；reply_to 必填、指向原询问。"
    assert card_msg.when_to_use_of("msg_notify") == \
        "告知对方一件事、不需要对方回话时发。"
    assert card_msg.when_to_use_of("msg_nope") == ""
    assert card_msg.when_to_use_of("") == ""


def test_s1_reply_expected_of_bool():
    """ask=True、answer/notify=False、未命中缺省 False。"""
    assert card_msg.reply_expected_of("msg_ask") is True
    assert card_msg.reply_expected_of("msg_answer") is False
    assert card_msg.reply_expected_of("msg_notify") is False
    assert card_msg.reply_expected_of("msg_nope") is False


def test_s1_reply_type_of_str():
    """ask→msg_answer、answer/notify→""、未命中缺省 ""。"""
    assert card_msg.reply_type_of("msg_ask") == "msg_answer"
    assert card_msg.reply_type_of("msg_answer") == ""
    assert card_msg.reply_type_of("msg_notify") == ""
    assert card_msg.reply_type_of("msg_nope") == ""


def test_s1_fail_open_bad_registry(monkeypatch, tmp_path):
    """坏表 fail-open：四新函数在坏表/缺文件/坏 entry 下返缺省值、不抛出。"""
    # 坏表形态（缺 types）
    bad = tmp_path / "bad.json"
    bad.write_text('{"version": 1, "no_types": {}}', encoding="utf-8")
    monkeypatch.setattr(card_msg, "REGISTRY_PATH", bad)
    assert card_msg.entry_of("msg_ask") is None
    assert card_msg.when_to_use_of("msg_ask") == ""
    assert card_msg.reply_expected_of("msg_ask") is False
    assert card_msg.reply_type_of("msg_ask") == ""
    # 缺文件
    monkeypatch.setattr(card_msg, "REGISTRY_PATH", tmp_path / "missing.json")
    assert card_msg.entry_of("msg_ask") is None
    assert card_msg.when_to_use_of("msg_ask") == ""
    assert card_msg.reply_expected_of("msg_ask") is False
    assert card_msg.reply_type_of("msg_ask") == ""


def test_s1_fail_open_malformed_entry(monkeypatch, tmp_path):
    """entry 形态坏（值非对象/字段类型怪）→ fail-open 缺省，不抛出。"""
    ext = tmp_path / "ext.json"
    ext.write_text(json.dumps({
        "version": 1,
        "types": {
            "msg_broken": "not-a-dict",
            "msg_odd": {"display": 123, "when_to_use": None,
                        "reply_expected": "yes", "reply_type": 7},
        },
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(card_msg, "REGISTRY_PATH", ext)
    assert card_msg.entry_of("msg_broken") is None
    assert card_msg.when_to_use_of("msg_odd") == ""      # None → ""
    assert card_msg.reply_expected_of("msg_odd") is True  # "yes" 真值 → True（bool 收敛）
    assert card_msg.reply_type_of("msg_odd") == "7"       # 非空 → str(7)


def test_make_msg_id_shape():
    mid = card_msg.make_msg_id("card_007")
    assert mid.startswith("card_007-")
    assert len(mid) == 17


def test_load_registry_bad_form_returns_empty(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"version": 1, "no_types": {}}', encoding="utf-8")
    monkeypatch.setattr(card_msg, "REGISTRY_PATH", bad)
    assert card_msg.load_registry() == {}
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(card_msg, "REGISTRY_PATH", missing)
    assert card_msg.load_registry() == {}


def test_type_codes_follows_registry(monkeypatch, tmp_path):
    """加类型＝加条目零代码：注册表加条目 → type_codes/display_of 同步生效。"""
    ext = tmp_path / "ext.json"
    ext.write_text(json.dumps({
        "version": 1,
        "types": {"msg_ask": {"display": "询问", "note": ""},
                  "msg_test": {"display": "测试", "note": ""}},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(card_msg, "REGISTRY_PATH", ext)
    assert "msg_test" in card_msg.type_codes()
    assert card_msg.display_of("msg_test") == "测试"


# ══════════════ 头行解析 · 固定向量集七类 ══════════════

V_OK = "[[card-msg id=card_007-3f9a2b17 type=msg_ask from=card_007 reply_to=]]"


def test_vec1_normal():
    r = card_msg.parse_header(V_OK + "\n正文第一行")
    assert r == {"id": "card_007-3f9a2b17", "type": "msg_ask",
                 "from": "card_007", "reply_to": ""}


def test_vec2_missing_field():
    # 可缺键：reply_to（ask/notify 本就不带）→ 解析成功、归一空串
    r = card_msg.parse_header("[[card-msg id=card_007-1a2b3c4d type=msg_notify from=card_007]]")
    assert r == {"id": "card_007-1a2b3c4d", "type": "msg_notify",
                 "from": "card_007", "reply_to": ""}
    # 必需键缺失 → None
    assert card_msg.parse_header("[[card-msg type=msg_ask from=card_007 reply_to=]]") is None
    assert card_msg.parse_header("[[card-msg id=x-1 type=msg_ask reply_to=]]") is None


def test_vec3_extra_spaces():
    r = card_msg.parse_header("  [[card-msg   id=x-11111111   type=msg_ask   from=card_007   reply_to=  ]]  ")
    assert r == {"id": "x-11111111", "type": "msg_ask",
                 "from": "card_007", "reply_to": ""}


def test_vec4_nested_brackets():
    # 头行后尾随内容 / 值内含方括号 → None
    assert card_msg.parse_header("[[card-msg id=x-1 type=msg_ask from=c_1]] 尾随]]") is None
    assert card_msg.parse_header("[[card-msg id=x[y] type=msg_ask from=c_1]]") is None


def test_vec5_chinese_in_header():
    # 头行值域含中文（标识符域必须 ASCII）→ None
    assert card_msg.parse_header("[[card-msg id=卡007 type=msg_ask from=card_007]]") is None
    assert card_msg.parse_header("[[card-msg id=x-1 类型=询问 from=card_007]]") is None


def test_vec6_no_header():
    assert card_msg.parse_header("普通正文第一行\n[[card-msg id=x-1 type=msg_ask from=c_1]]") is None
    assert card_msg.parse_header("") is None


def test_vec7_header_not_first_line():
    assert card_msg.parse_header("正文在前\n[[card-msg id=x-1 type=msg_ask from=c_1]]") is None


def test_parse_header_never_raises():
    for weird in (None, 123, "[[card-msg]]", "[[card-msg =]]", "[[card-msg", "]]]][["):
        try:
            card_msg.parse_header(weird)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"parse_header({weird!r}) raised {exc!r}")


# ══════════════ build/strip 往返 ══════════════

def test_build_header_roundtrip():
    h = card_msg.build_header("card_007-3f9a2b17", "msg_answer", "card_007",
                              "card_001-abcdef01")
    assert h == ("[[card-msg id=card_007-3f9a2b17 type=msg_answer from=card_007"
                 " reply_to=card_001-abcdef01]]")
    assert card_msg.parse_header(h) == {
        "id": "card_007-3f9a2b17", "type": "msg_answer",
        "from": "card_007", "reply_to": "card_001-abcdef01"}


def test_build_header_empty_reply_to_roundtrip():
    h = card_msg.build_header("card_007-3f9a2b17", "msg_notify", "card_007")
    assert h.endswith("reply_to=]]")
    assert card_msg.parse_header(h)["reply_to"] == ""


def test_strip_header():
    assert card_msg.strip_header(V_OK + "\n正文行") == "正文行"
    assert card_msg.strip_header("无头行正文") == "无头行正文"
    assert card_msg.strip_header("") == ""


# ══════════════ find_pending 三态（T-7 前置） ══════════════

def test_find_pending_three_states():
    human = {"seq": 1, "actor": "human", "turn": 1}
    card_block = {"seq": 2, "actor": "card:card_001", "turn": 1,
                  "content": "[[card-msg id=card_001-aaa type=msg_ask from=card_001 reply_to=]]\n问"}
    asst = {"seq": 3, "actor": "assistant", "turn": 1, "end": "model_done"}
    # 三态一：卡间块后无 assistant → pending（返回该卡间块）
    assert card_msg.find_pending([human, card_block]) is card_block
    # 三态二：卡间块后有 assistant → 非 pending
    assert card_msg.find_pending([human, card_block, asst]) is None
    # 三态三：无卡间块 → 非 pending
    assert card_msg.find_pending([human, asst]) is None
    assert card_msg.find_pending([]) is None
    assert card_msg.find_pending(None) is None


def test_find_pending_latest_card_block_wins():
    early = {"seq": 1, "actor": "card:card_001", "turn": 1}
    late = {"seq": 3, "actor": "card:card_002", "turn": 2}
    asst = {"seq": 2, "actor": "assistant", "turn": 1, "end": "model_done"}
    # 早期卡间块已被回应，但最后一条卡间块之后无 assistant → pending 且是最后那条
    assert card_msg.find_pending([early, asst, late]) is late


# ══════════════ K2 · 工具侧断言（send_card_message） ══════════════

from src.api import card_tools  # noqa: E402
from src.storage import (  # noqa: E402
    card_db, card_store, event_log, session_facade, session_ops, work_store,
)
from conftest import prime_card_session  # noqa: E402


async def _make_tree(wid):
    """create_work 已自带根卡 card_001（project/rd_project）。再建：
    002=子卡A(父001) → 003=孙卡A1(父002)；004=兄弟卡B(父001，越权例用)。
    显式 card_type="sub_project"（POLICY 表内：READ+TREE_SCOPE 在授权）。"""
    await card_store.create_card(wid, "子卡A", "用户直接创建",
                                 card_type="sub_project", parent="card_001")
    await card_store.create_card(wid, "孙卡A1", "用户直接创建",
                                 card_type="sub_project", parent="card_002")
    await card_store.create_card(wid, "兄弟卡B", "用户直接创建",
                                 card_type="sub_project", parent="card_001")
    # 按生产形态铺会话（建卡即铺；测试直建卡不带）——见 conftest.prime_card_session
    for _cid in ("card_002", "card_003", "card_004"):
        prime_card_session(wid, _cid)
    # [5713 通道试走 2026-10-20] **卡间通信走专门的通信线**——发信的前提从
    #   "同一棵任务树" 改为 "两卡之间已有通信线（links）"。故夹具负责铺线：
    #   只铺本文件用例真正要发信的那几条，**刻意不铺 002→004**（越权例要用无线）。
    #   注意：update_card({"links": ...}) 是**整块覆盖**，同一源卡的多条线必须
    #   一次写完（分两次写会互相冲掉——首版就在这踩过一脚）。
    _wires = {
        "card_001": ["card_002", "card_004"],   # 根 → 子卡A（T6）／根 → 兄弟卡B（K4）
        "card_002": ["card_003"],               # 子卡A → 孙卡A1（主线）
    }
    for _src, _dsts in _wires.items():
        await card_store.update_card(
            wid, _src,
            {"links": [{"target": _d, "type": "关联"} for _d in _dsts]},
            f"测试夹具：建立通信线 {_src} → {'/'.join(_dsts)}")
    return card_store.iter_card_metas(wid)


def _face(card_id: str, meta: dict) -> list[str]:
    """按合成卡面取工具面（钉「常规面 vs auto 面」的分叉，不依赖沙箱）。"""
    return [t["name"] for t in card_tools.build_tool_schemas(
        card_id, "work_x", {card_id: dict(meta, card_id=card_id)})]


async def test_t1_tool_registered():
    # [12:34 定性·语义保留] 发信**不是通用工具**：常规模型工具面不含它——
    # 普通卡（含全权限的 sub_project）依旧无权发信，流程仍是发信主体。
    # [自动模式 2026-10-17 · 回灌 5700 G2 · 愿景「模型要能用卡片间通信完成
    #  任务」] 自动模式卡是**显式例外**：作为 AUTO_ONLY_TOOLS 成员，只在
    #  auto_mode=1 的卡上条件下发（同 update_goal 手法），执行层仍走
    #  _h_send_card_message 六道门槛。
    # ⇒ 判据从「不在注册表」改为「不在常规面／只在 auto 面」，钉的是同一条
    #   不变量：**非自动模式的模型无权发信**；同时新增钉住 auto 面确有此能力。
    # [v12 2026-10-22 · 用户裁定] send_card_message **已移出 AUTO_ONLY_TOOLS**，
    # 改由 TOOL_SWITCH 常规过滤——卡上有该开关就可见。原因：worker 干到一半
    # 卡住（需求不清/要主卡拍板/发现做错）时不能说话，只能硬做完或干等。
    assert "sync" in card_tools.TOOL_REGISTRY
    assert "send_card_message" not in card_tools.AUTO_ONLY_TOOLS, (
        "发信已改为按开关过滤，不再靠 auto 面追加")
    full = list(card_tools.policy.SWITCHES)
    normal = _face("c_norm", {"card_type": "sub_project", "auto_mode": 0,
                              "perms": full, "perms_version": 5, "parent": ""})
    assert "sync" in normal, (
        "全权限常规卡面现在**无**发信(send_card_message 已退役,场景件 sync 替代)")
    assert "read_inbox" not in normal, "常规卡面必须不含 read_inbox"
    auto = _face("c_auto", {"card_type": "sub_project", "auto_mode": 1,
                            "perms": full, "perms_version": -1, "parent": ""})
    # [通信收拢 2026-10-28] read_inbox 已退役（收件箱取消）：auto 面只剩 sync。
    assert "sync" in auto, "auto 面须有自主通信能力(场景件)"
    assert "read_inbox" not in card_tools.TOOL_REGISTRY, "read_inbox 必须已从注册表退役"
    # 薄壳内部接口存在且可调（流程代码的发信入口）
    assert callable(getattr(card_tools, "send_card_message", None))


def test_t2_schema_enum_reads_registry():
    # [12:34 定性·语义保留] 常规模型面不含发信 schema（见 t1 的常规/auto 分叉断言）；
    # 「加类型零代码」语义保留在函数体校验层
    # [v12 2026-10-22] 发信改按开关过滤，不再经 AUTO_ONLY_TOOLS
    assert "send_card_message" not in card_tools.AUTO_ONLY_TOOLS
    full = list(card_tools.policy.SWITCHES)
    normal = _face("c_norm2", {"card_type": "sub_project", "auto_mode": 0,
                               "perms": full, "perms_version": 5, "parent": ""})
    assert "sync" in normal
    # 类型可拓展（11:44）：注册表现读、函数体校验层每次现读（type_codes）
    # [worker 卡 2026-10-15 · C1 指纹] 三型 → 四型（＋worker_done）
    assert list(card_msg.type_codes()) == ["msg_ask", "msg_answer", "msg_notify",
                                           "worker_done", "msg_directive",
                                           "msg_sync", "msg_report"]


def test_t3_add_type_zero_code(monkeypatch, tmp_path):
    """临时副本注册表加 msg_test → 函数体校验层认识它（type_codes 现读）→ 复原。"""
    ext = tmp_path / "ext_registry.json"
    base = json.loads(REGISTRY.read_text(encoding="utf-8"))
    base["types"]["msg_test"] = {"display": "测试", "note": ""}
    ext.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(card_msg, "REGISTRY_PATH", ext)
    # 加类型零代码的语义保留在函数体校验层：新增类型直接可发
    assert "msg_test" in card_msg.type_codes()
    # 仓库最终态类型集（monkeypatch 只动内存指针，不落盘）
    # [worker 卡 2026-10-15 · C1 指纹] 三型 → 四型（＋worker_done），断言语义不变
    assert set(json.loads(REGISTRY.read_text(encoding="utf-8"))["types"].keys()) == {
        "msg_ask", "msg_answer", "msg_notify", "worker_done", "msg_directive",
        "msg_sync", "msg_report"}


def test_append_user_default_true_regression():
    """append_user 默认值回归：14 处既有调用方全部三参形态不传该参 → 行为逐字不变。"""
    sig = inspect.signature(routes._run_message)
    assert sig.parameters["append_user"].default is True
    sig_inner = inspect.signature(routes._run_message_inner)
    assert sig_inner.parameters["append_user"].default is True


async def test_k3_pending_recovery_on_next_close(sandbox, monkeypatch):
    """进程级补齐：信在**收件箱**而 B 未被调起（模拟任务丢失）→ 下次任一回合收口补齐。"""
    from src.storage import work_store
    wid = (await work_store.create_work("K3补齐剧本"))["work_id"]
    cards = await _make_tree(wid)
    # 直落一封信进**收件箱**（不经收口——模拟 create_task 丢失后的遗留）
    await card_db.append_inbox(
        wid, msg_id="card_002-deadbeef", target_card_id="card_003",
        from_card="card_002", type="msg_ask",
        content=card_msg.build_header("card_002-deadbeef", "msg_ask", "card_002")
                + "\n补齐路径核对")
    b_reply = "补齐收到，已处理。"

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None, tools=None, **_):
        return ({"content": b_reply, "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")
    # 任一回合收口（TRIGGERS 空则 schedule 直接 return）——补齐走的是
    # 「收件箱推导」：这里显式登记 trigger 模拟发现方。
    card_msg.TRIGGERS.setdefault((wid, "card_003"), []).append("card_002-deadbeef")
    state = message_runner.RunState(work_id=wid, card_id="card_002", content="x")
    await message_runner.schedule_card_message_triggers(state)
    for _ in range(60):
        await asyncio.sleep(0.05)
        blocks_b = await session_facade.read(wid, "card_003")
        if any(b.get("actor") == "assistant" for b in blocks_b):
            break
    blocks_b = await session_facade.read(wid, "card_003")
    assert any(b.get("actor") == "assistant" for b in blocks_b), "pending 信应在收口时补齐调起"


# ══════════════ [迭代五 S3] 激励文随消息提供应对说明 ══════════════

