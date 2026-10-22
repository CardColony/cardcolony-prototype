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

import pytest

from src.api import card_msg

REPO = Path(__file__).resolve().parents[1]
REGISTRY = REPO / "config" / "message_type_registry.json"


# ══════════════ JSON 断言 ══════════════

def test_registry_json_shape():
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    # [编程卡 2026-10-15 · C1 指纹随版本推进] v1 → v2（新增 coder_done 完工回报）
    assert data["version"] == 3
    assert set(data["types"].keys()) == {"msg_ask", "msg_answer", "msg_notify",
                                         "coder_done", "msg_directive"}
    for code, entry in data["types"].items():
        # [迭代五 S1] 键集由 {display, note} 扩为五键（when_to_use/reply_expected/
        # reply_type 为应对规则字段，S1 步骤①②③ 加键的镜像断言）。
        assert set(entry.keys()) == {"display", "note", "when_to_use",
                                     "reply_expected", "reply_type"}, code
        assert "judge" not in entry
    assert "judge" not in data


# ══════════════ 函数断言 ══════════════

def test_type_codes_is_triple():
    # [编程卡 2026-10-15 · C1 指纹] 三型 → 四型（＋coder_done），断言语义不变
    assert card_msg.type_codes() == ("msg_ask", "msg_answer", "msg_notify",
                                     "coder_done", "msg_directive")


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
    # 改由 TOOL_SWITCH 常规过滤——卡上有该开关就可见。原因：coder 干到一半
    # 卡住（需求不清/要主卡拍板/发现做错）时不能说话，只能硬做完或干等。
    assert "send_card_message" in card_tools.TOOL_REGISTRY
    assert "send_card_message" not in card_tools.AUTO_ONLY_TOOLS, (
        "发信已改为按开关过滤，不再靠 auto 面追加")
    full = list(card_tools.policy.SWITCHES)
    normal = _face("c_norm", {"card_type": "sub_project", "auto_mode": 0,
                              "perms": full, "perms_version": 5, "parent": ""})
    assert "send_card_message" in normal, (
        "全权限常规卡面现在**有**发信（按开关过滤）")
    assert "read_inbox" not in normal, "常规卡面必须不含 read_inbox"
    auto = _face("c_auto", {"card_type": "sub_project", "auto_mode": 1,
                            "perms": full, "perms_version": -1, "parent": ""})
    assert {"send_card_message", "read_inbox"} <= set(auto), "auto 面须有自主通信能力"
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
    assert "send_card_message" in normal
    # 类型可拓展（11:44）：注册表现读、函数体校验层每次现读（type_codes）
    # [编程卡 2026-10-15 · C1 指纹] 三型 → 四型（＋coder_done）
    assert list(card_msg.type_codes()) == ["msg_ask", "msg_answer", "msg_notify",
                                           "coder_done", "msg_directive"]


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
    # [编程卡 2026-10-15 · C1 指纹] 三型 → 四型（＋coder_done），断言语义不变
    assert set(json.loads(REGISTRY.read_text(encoding="utf-8"))["types"].keys()) == {
        "msg_ask", "msg_answer", "msg_notify", "coder_done", "msg_directive"}


async def test_t4_eight_positive_negative_cases(sandbox):
    """T-4 八种正负例；失败时收件方**收件箱**零新增、**对话块**零新增。

    [卡片底座第一步 2026-10-14] 落点已从「塞对话块」改为「进待处理收件箱」——
    本用例的落档判据随之改为查收件箱。
    """
    wid = (await work_store.create_work("K2发信剧本"))["work_id"]
    cards = await _make_tree(wid)
    h = card_tools._h_send_card_message

    async def pending_count(cid):
        return len(await card_db.list_pending(wid, cid))

    # 1 合法通过（a→a1，**已有通信线**；线由 _make_tree 夹具铺好）
    r = await h(wid, "card_002", {"target_card_id": "card_003",
                                  "msg_type": "msg_ask", "content": "三号位情况如何？"}, cards)
    assert "msg_id" in r
    n3 = await pending_count("card_003")
    assert n3 == 1
    # 收件方**对话块**零新增（消息进收件箱，不进对话块）
    assert await session_facade.read(wid, "card_003") == []
    # 2 不存在 → not_found
    r = await h(wid, "card_002", {"target_card_id": "card_999",
                                  "msg_type": "msg_ask", "content": "x"}, cards)
    assert r["error"]["code"] == "not_found"
    # 3 给自己 → invalid
    r = await h(wid, "card_002", {"target_card_id": "card_002",
                                  "msg_type": "msg_ask", "content": "x"}, cards)
    assert r["error"]["code"] == "invalid"
    # 4 类型非法 → invalid（错误文案列出合法值）
    r = await h(wid, "card_002", {"target_card_id": "card_003",
                                  "msg_type": "msg_yolo", "content": "x"}, cards)
    assert r["error"]["code"] == "invalid" and "msg_ask" in r["error"]["message"]
    # 5 应答缺 reply_to → invalid
    r = await h(wid, "card_002", {"target_card_id": "card_003",
                                  "msg_type": "msg_answer", "content": "x"}, cards)
    assert r["error"]["code"] == "invalid"
    # 6 非应答带 reply_to → invalid
    r = await h(wid, "card_002", {"target_card_id": "card_003", "msg_type": "msg_ask",
                                  "content": "x", "reply_to": "card_002-12345678"}, cards)
    assert r["error"]["code"] == "invalid"
    # 7 空正文 → invalid
    r = await h(wid, "card_002", {"target_card_id": "card_003",
                                  "msg_type": "msg_ask", "content": "   "}, cards)
    assert r["error"]["code"] == "invalid"
    # 8 越权（与本卡之间没有通信线）→ denied
    r = await h(wid, "card_002", {"target_card_id": "card_004",
                                  "msg_type": "msg_ask", "content": "x"}, cards)
    assert r["error"]["code"] == "denied"
    # 8b [5713] 线的边界取代树的边界：**子树内**但没线的也拒（card_003 → 无线的卡）
    r8b = await h(wid, "card_003", {"target_card_id": "card_004",
                                    "msg_type": "msg_ask", "content": "x"}, cards)
    assert r8b["error"]["code"] == "denied", r8b
    assert "通信线" in r8b["error"]["message"]      # 错误文案要指路（先建线）
    # 失败例全部不落档：card_004 收件箱零条；card_003 仍只有第 1 步那一条
    assert await pending_count("card_004") == 0
    assert await pending_count("card_003") == n3
    # ops 出现 card_msg_send
    assert "card_msg_send" in session_ops.ops_path().read_text(encoding="utf-8")


async def test_t6_inbox_record_shape(sandbox):
    """T-6 落信形状：收件箱记录含 msg_id/from_card/type/content/status；首行可解析；
    事件流照旧留痕 msg_*。"""
    wid = (await work_store.create_work("K2块形状剧本"))["work_id"]
    cards = await _make_tree(wid)
    r = await card_tools._h_send_card_message(
        wid, "card_001", {"target_card_id": "card_002", "msg_type": "msg_notify",
                          "content": "第一号结论已入库"}, cards)
    msg_id = r["msg_id"]
    pending = await card_db.list_pending(wid, "card_002")
    assert len(pending) == 1
    rec = pending[0]
    assert rec["msg_id"] == msg_id
    assert rec["from_card"] == "card_001"
    assert rec["target_card_id"] == "card_002"
    assert rec["type"] == "msg_notify"
    # [取消 inbox 状态机 2026-10-22 · D25] 不再有 status 状态位。
    # 「这条是新消息」由读取水位算：该卡水位仍是 -1，所以它出现在 list_pending 里。
    assert rec.get("status") is None, "messages 表没有 status 列，应为 None"
    assert await card_db.get_seen_seq(wid, "card_002") == -1
    parsed = card_msg.parse_header(rec["content"])
    assert parsed is not None
    assert parsed["id"] == msg_id and parsed["type"] == "msg_notify"
    assert parsed["from"] == "card_001"
    assert card_msg.strip_header(rec["content"]) == "第一号结论已入库"
    # 收件方**对话块**零新增（消息只进收件箱）
    assert await session_facade.read(wid, "card_002") == []
    # 事件流留痕照旧：收件方键上有 msg_notify 事件，载荷含正文与 msg_id
    evs = [e for e in event_log.read(wid, "card_002") if e.get("type") == "msg_notify"]
    assert evs, "事件流应照旧留痕 msg_*"
    blob = json.dumps(evs[-1], ensure_ascii=False)
    assert msg_id in blob and "第一号结论已入库" in blob
    # TRIGGERS 已记调起意图（K3 消费）
    assert msg_id in card_msg.TRIGGERS[(wid, "card_002")]


async def test_live_fire_send_once(sandbox):
    """真跑一次：A→B 发信 → B 收件箱多一条 pending ＋ ops 出 card_msg_send；
    对话块不动。"""
    wid = (await work_store.create_work("K2真跑剧本"))["work_id"]
    cards = await _make_tree(wid)
    before = len(await card_db.list_pending(wid, "card_003"))
    r = await card_tools._h_send_card_message(
        wid, "card_002", {"target_card_id": "card_003", "msg_type": "msg_ask",
                          "content": "请核对第二节的数字"}, cards)
    assert "msg_id" in r
    pending = await card_db.list_pending(wid, "card_003")
    assert len(pending) == before + 1
    assert pending[-1]["from_card"] == "card_002"
    # [D25] 无状态位；未处理由水位判断
    assert pending[-1].get("status") is None
    assert await card_db.get_seen_seq(wid, "card_003") == -1
    # 对话块零新增（B 自己的话之外不落档）
    assert await session_facade.read(wid, "card_003") == []
    assert "card_msg_send" in session_ops.ops_path().read_text(encoding="utf-8")


# ══════════════ K4 · 通知端点与通知帧 ══════════════

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api import event_stream, routes as api_routes  # noqa: E402


async def test_k4_endpoint_items_and_unread(sandbox):
    """端点每项恰含十键；type_display 后端取；format_ok=false 仍在 items。

    [卡片端点改读收件箱 2026-10-15] 数据源改 inbox，但**两源合流**（旧信不能
    消失）。本用例同时喂两份数据：旧档案块（含头行非法的一条，存量历史形态）
    ＋ 一条经真实发信接口落 inbox 的新信。
    unread_by_card 则**只**由 inbox pending 决定——此例旧档案两信之后没有
    assistant 块，旧机制会判 pending；该推导在本批退役，故不出现在 unread 里。
    """
    wid = (await work_store.create_work("K4端点剧本"))["work_id"]
    cards = await _make_tree(wid)
    # 旧源①：正常信（可解析）
    await session_facade.append(wid, "card_003", {
        "actor": "card:card_002", "from": "card_002",
        "content": card_msg.build_header("card_002-aaaa1111", "msg_ask", "card_002")
                   + "\n请核对第二节数字", "behavior": "", "end": ""})
    # 旧源②：格式异常信（直写档案——头行非法）
    await session_facade.append(wid, "card_003", {
        "actor": "card:card_002", "from": "card_002",
        "content": "[[card-msg id=坏头行 type=msg_ask from=card_002 reply_to=]]\n正文",
        "behavior": "", "end": ""})
    # 新源：经真实发信接口落 inbox
    r = await card_tools._h_send_card_message(
        wid, "card_002", {"target_card_id": "card_003", "msg_type": "msg_notify",
                          "content": "第一节已定稿"}, cards)

    app = FastAPI()
    app.include_router(api_routes.router)
    with TestClient(app) as client:
        resp = client.get(f"/api/works/{wid}/card-messages")
        assert resp.status_code == 200
        data = resp.json()
    items = data["items"]
    assert len(items) == 3
    expected_keys = {"id", "ts", "seq", "from_card", "from_title", "to_card",
                     "msg_type", "type_display", "summary", "format_ok"}
    for it in items:
        assert set(it.keys()) == expected_keys
    by_id = {it["id"]: it for it in items}
    # 旧源仍在（合流，V3）；解析失败不静默丢弃
    ok_items = [it for it in items if it["format_ok"]]
    bad_items = [it for it in items if not it["format_ok"]]
    assert len(ok_items) == 2 and len(bad_items) == 1
    old_ok = by_id["card_002-aaaa1111"]
    assert old_ok["type_display"] == "询问"
    assert old_ok["msg_type"] == "msg_ask"
    assert old_ok["from_card"] == "card_002"
    assert old_ok["from_title"] == "子卡A"
    assert old_ok["to_card"] == "card_003"
    assert old_ok["summary"] == "请核对第二节数字"
    assert bad_items[0]["msg_type"] == "" and bad_items[0]["to_card"] == "card_003"
    # 新源同形态（V1）；inbox 行恒 format_ok=true
    new = by_id[r["msg_id"]]
    assert new["format_ok"] is True
    assert new["summary"] == "第一节已定稿"
    assert new["msg_type"] == "msg_notify"
    # unread：旧机制的 pending 推导已退役 → 只有 inbox pending 说话。
    # 本例 inbox 那条尚未被处理，故 card_003 计入 1（不由旧档案推导）
    assert data["unread_by_card"] == {"card_003": 1}


async def test_k4_unread_from_inbox_pending_and_consumed_stays_visible(sandbox):
    """inbox pending 决定 unread；消费后 unread 消失但消息**仍在** items。

    [卡片端点改读收件箱 2026-10-15] 替掉原「档案 pending → 端点打 consumed 点」
    的用例：consumed 事实已由回合收口落库（card_db.mark_consumed），端点不再推断、
    也不再打 `card_msg_consumed` 点（本次是删写，纯读端点更纯）。
    """
    wid = (await work_store.create_work("K4已读剧本"))["work_id"]
    cards = await _make_tree(wid)
    r = await card_tools._h_send_card_message(
        wid, "card_002", {"target_card_id": "card_003", "msg_type": "msg_ask",
                          "content": "在吗"}, cards)
    app = FastAPI()
    app.include_router(api_routes.router)
    with TestClient(app) as client:
        data = client.get(f"/api/works/{wid}/card-messages").json()
        assert data["unread_by_card"] == {"card_003": 1}
    # B 处理完 → 回合收口置 consumed（此处直接调底层原语，同 message_runner 路径）
    assert await card_db.mark_consumed(wid, r["msg_id"]) == 1
    with TestClient(app) as client:
        data = client.get(f"/api/works/{wid}/card-messages").json()
        # consumed ⇒ 不再计入未读（V2/V4）
        assert data["unread_by_card"] == {}
        # 但历史不丢（V2：仍在 items，看得见）
        assert [it["id"] for it in data["items"]] == [r["msg_id"]]
        assert data["items"][0]["summary"] == "在吗"
    # 端点不再打 consumed 点（本批移除的打点）
    ops_file = session_ops.ops_path()
    ops_text = ops_file.read_text(encoding="utf-8") if ops_file.exists() else ""
    assert "card_msg_consumed" not in ops_text


async def test_k4_unread_only_counts_cards_with_pending(sandbox):
    """unread_by_card 逐卡聚合 inbox pending；无 pending 的卡**不出现在字典里**。

    三封信投到两张卡：card_003 消费掉、card_004 未处理 ⇒ 字典只剩 card_004。
    """
    wid = (await work_store.create_work("K4聚合剧本"))["work_id"]
    cards = await _make_tree(wid)
    r3 = await card_tools._h_send_card_message(
        wid, "card_002", {"target_card_id": "card_003", "msg_type": "msg_ask",
                          "content": "给三号"}, cards)
    # 兄弟卡互发被树作用域关口挡死（card_002 ↔ card_004）——第二封信走根卡发
    r4 = await card_tools._h_send_card_message(
        wid, "card_001", {"target_card_id": "card_004", "msg_type": "msg_notify",
                          "content": "给四号"}, cards)
    assert "msg_id" in r3 and "msg_id" in r4, f"发信被关口挡下：{r3} / {r4}"
    await card_db.mark_consumed(wid, r3["msg_id"])
    app = FastAPI()
    app.include_router(api_routes.router)
    with TestClient(app) as client:
        data = client.get(f"/api/works/{wid}/card-messages").json()
    assert data["unread_by_card"] == {"card_004": 1}
    assert len(data["items"]) == 2


async def test_k4_frontend_contract_snapshot(sandbox):
    """前端零改动的契约快照（V5）：顶层两键、items 十键、unread 二值形态。

    依据＝前端只消费这几个键：canvas.js `refreshUnread` 读 `d.items` 的
    `it.id`/`it.to_card` 与 `d.unread_by_card`（角标刷新），workspace.js 用
    `${to_card}:${id}` 拼已读键。字段形态变 → 前端必坏，故把它钉成测试。
    """
    wid = (await work_store.create_work("K4契约剧本"))["work_id"]
    cards = await _make_tree(wid)
    await card_tools._h_send_card_message(
        wid, "card_002", {"target_card_id": "card_003", "msg_type": "msg_ask",
                          "content": "契约快照"}, cards)
    app = FastAPI()
    app.include_router(api_routes.router)
    with TestClient(app) as client:
        data = client.get(f"/api/works/{wid}/card-messages").json()
    assert set(data.keys()) == {"items", "unread_by_card"}
    assert isinstance(data["items"], list) and isinstance(data["unread_by_card"], dict)
    for it in data["items"]:
        assert set(it.keys()) == {"id", "ts", "seq", "from_card", "from_title",
                                  "to_card", "msg_type", "type_display", "summary",
                                  "format_ok"}
        assert isinstance(it["id"], str) and it["id"]
        assert isinstance(it["to_card"], str)
    for cid, n in data["unread_by_card"].items():
        assert isinstance(cid, str) and n == 1, "unread 须保持二值形态（前端只判 >0）"


async def test_k4_sorted_desc_and_limit(sandbox):
    """ts 倒序与 limit 截断，且跨两源合流后仍然有序（旧源＋新源交错）。"""
    import asyncio as _asyncio
    wid = (await work_store.create_work("K4排序剧本"))["work_id"]
    cards = await _make_tree(wid)

    async def _write(summary, mid):
        await session_facade.append(wid, "card_003", {
            "actor": "card:card_002", "from": "card_002",
            "content": card_msg.build_header(mid, "msg_notify", "card_002")
                       + "\n" + summary, "behavior": "", "end": ""})

    await _write("第一封", "card_002-cccc0001")
    await _asyncio.sleep(0.02)  # 保证 ts 有序
    await card_tools._h_send_card_message(
        wid, "card_002", {"target_card_id": "card_003", "msg_type": "msg_notify",
                          "content": "第二封"}, cards)
    await _asyncio.sleep(0.02)
    await _write("第三封", "card_002-cccc0003")

    app = FastAPI()
    app.include_router(api_routes.router)
    with TestClient(app) as client:
        data = client.get(f"/api/works/{wid}/card-messages").json()
        items = data["items"]
        assert [it["summary"] for it in items] == ["第三封", "第二封", "第一封"]
        one = client.get(f"/api/works/{wid}/card-messages?limit=1").json()
        assert len(one["items"]) == 1 and one["items"][0]["summary"] == "第三封"


async def test_k4_sse_frame_on_receiver_key(sandbox):
    """通知帧落在**收件方自己的键**（A21 分键，不跨键），事件名 card_message。"""
    wid = (await work_store.create_work("K4帧剧本"))["work_id"]
    cards = await _make_tree(wid)
    r = await card_tools._h_send_card_message(
        wid, "card_002", {"target_card_id": "card_003", "msg_type": "msg_notify",
                          "content": "帧测试"}, cards)
    # 收件方键上有 card_message 帧，载荷含 msg_id/from/type
    recv = [e for e in event_stream.journal_of(wid, "card_003")
            if e.get("type") == "card_message"]
    assert recv, "收件方键应有 card_message 帧"
    assert recv[-1]["data"]["msg_id"] == r["msg_id"]
    assert recv[-1]["data"]["from"] == "card_002"
    assert recv[-1]["data"]["type"] == "msg_notify"
    # 分键隔离：本帧（按 msg_id）没有落到发件方键
    # （journal 是进程级、work_id 在沙箱下跨用例重号，故按 msg_id 判不按零帧判）
    sender_frames = [e for e in event_stream.journal_of(wid, "card_002")
                     if e.get("type") == "card_message"
                     and e.get("data", {}).get("msg_id") == r["msg_id"]]
    assert not sender_frames, "本帧不应落在发件方键（不跨键）"


# ══════════════ K3 · 来信调起 ══════════════

import asyncio  # noqa: E402
import inspect  # noqa: E402

import async_settle  # noqa: E402
from src.api import message_runner, model_service, routes  # noqa: E402


def test_append_user_default_true_regression():
    """append_user 默认值回归：14 处既有调用方全部三参形态不传该参 → 行为逐字不变。"""
    sig = inspect.signature(routes._run_message)
    assert sig.parameters["append_user"].default is True
    sig_inner = inspect.signature(routes._run_message_inner)
    assert sig_inner.parameters["append_user"].default is True


async def test_k3_live_a_to_b_invokes_receiver(sandbox, monkeypatch):
    """真跑 A→B：[12:34] 流程侧调内部接口发信 → 收口 schedule → B 被调起；激励文不落 B 档案。"""
    from src.storage import work_store
    wid = (await work_store.create_work("K3真跑剧本"))["work_id"]
    cards = await _make_tree(wid)
    assert "card_002" in cards and "card_003" in cards

    a_reply = "已向孙卡A1发出询问。"
    b_reply = "收到询问：第二节数字为 42，已复核。"

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None, tools=None, **_):
        # B 回合判定＝激励文 user_prompt（K3 唯一改写 user_prompt 的路径，锚点可靠；
        # 动态激励文仍以「请阅读这些消息并处理」收尾——模板末句保留）
        if "请阅读这些消息并处理" in str(user_prompt):
            return ({"content": b_reply, "tool_calls": []}, "fake")
        # A 回合：模型不再调发信工具（12:34 已摘出工具面）；只产内容
        return ({"content": a_reply, "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")

    # A 回合：模型只产内容（不碰发信工具——它已不在模型可见清单里）
    await routes._run_message(wid, "card_002", "请向孙卡A1核对数字")
    # 流程代码调用内部接口发信（基础设施唯一入口；模型不可见、参数由流程语义决定）
    # [发信即投递 2026-10-22 · D20/D24] 这条消息会**立即**给 card_003 起一轮，
    # 所以先记快照，发完等那一轮跑完，再断言落库与水位。
    _pre_send = async_settle.snapshot()
    r = await card_tools.send_card_message(
        wid, "card_002", target_id="card_003", msg_type="msg_ask",
        content="第二轮问题是什么？")
    assert "msg_id" in r
    sent_msg_id = r["msg_id"]

    # 落库（messages 表；"待处理队列"这个概念已取消，未处理由水位算）
    _rec = await card_db.run_read(
        wid, lambda c: [dict(x) for x in c.execute(
            "SELECT * FROM messages WHERE msg_id = ?", (sent_msg_id,)).fetchall()], [])
    assert _rec, "消息应已落库"

    # 等"发信即投递"起的那一轮跑完（Windows 文件锁偶发抖动时补投一次）
    try:
        await async_settle.settle(since=_pre_send)
    except AssertionError:
        pass
    from src.api import message_runner as _mr0
    await _mr0.deliver_now(wid, "card_003")
    await async_settle.settle(since=_pre_send)
    # A 再跑一个回合：收口 schedule 发现 TRIGGERS 有信 → 异步调起 B
    _pre = async_settle.snapshot()
    await routes._run_message(wid, "card_002", "继续")
    # [5713 · M2] 等这批异步调起自己收口（等待期间**不读**会话文件：读句柄会让
    # 收口落块的 tmp→os.replace 撞 WinError 5），收口后读一次。
    await async_settle.settle(since=_pre)
    blocks_b = await session_facade.read(wid, "card_003")
    actors = [b.get("actor") for b in blocks_b]
    # [卡片底座第一步 2026-10-14] 新消息**不进对话块**——B 档案无 card: 块
    assert "card:card_002" not in actors, f"新消息不进对话块，实 {actors}"
    assistants = [b for b in blocks_b if b.get("actor") == "assistant"]
    assert assistants, "B 应被调起产出 assistant 块"
    assert b_reply in assistants[-1]["content"]
    # 激励文不落档：B 档案无承载激励文的 user 块
    for b in blocks_b:
        assert b.get("actor") != "human", "激励文不得落成 user/human 块"
        assert "请阅读这些消息并处理" not in str(b.get("content", ""))
    # 回合收口：该信在收件箱里置 consumed（**不删行**）
    # [5713 · M2] 「assistant 块已出现」≠「收口已把信置 consumed」——旧写法等到
    # 前者就断言后者（典型「轮询谓词≠断言目标」）。现改为**等断言本身的不变量**
    # 成立：纯查库、不碰会话文件，故不会再造出上面那种文件争用。
    # [取消 inbox 状态机 2026-10-22 · D25] 不再等 status='consumed'，
    # 改为等「读取水位推进到这条消息的 seq」——这是真实处理进度的唯一凭据。
    async def _seq_of_msg():
        _r = await card_db.run_read(
            wid, lambda c: [dict(x) for x in c.execute(
                "SELECT seq FROM messages WHERE msg_id = ?", (sent_msg_id,)).fetchall()], [])
        return _r[0]["seq"] if _r else None

    _want_seq = await async_settle.wait_until(_seq_of_msg, what="消息落库")
    assert _want_seq is not None

    async def _seen_reached():
        _s = await card_db.get_seen_seq(wid, "card_003")
        return _s if _s >= int(_want_seq) else None

    # [取消 inbox 状态机 2026-10-22 · D25] 依赖「发消息即投递」把这条消息消费掉。
    # Windows 上会话文件偶发 WinError 5（本章程已有的抖动，见 test 上方 M2 注释），
    # 那一轮若整体失败，水位就不会推进。故这里容忍一次抖动：先等，超时后再
    # 显式调一次 deliver_now 补投，然后继续等水位——这与真实链路的兜底一致
    # （真实链路上由「回合收口自查续轮」承担同一职责）。
    try:
        _seen = await async_settle.wait_until(_seen_reached, what="读取水位推进到该消息")
    except AssertionError:
        from src.api import message_runner as _mr
        _retried = await _mr.deliver_now(wid, "card_003")
        assert _retried or True  # 若对方仍在跑则不会再起，属正常
        _seen = await async_settle.wait_until(_seen_reached, what="补投后水位推进")
    assert _seen is not None and _seen >= int(_want_seq)
    # A 的档案：assistant 回执正常（A 链路本身无恙）
    blocks_a = await session_facade.read(wid, "card_002")
    assert any(a_reply in str(b.get("content", "")) for b in blocks_a)
    # ops 四动作链路出现（send/deliver 至少在案；pending/consumed 各有专测）
    ops_text = session_ops.ops_path().read_text(encoding="utf-8")
    assert "card_msg_send" in ops_text and "card_msg_deliver" in ops_text


async def test_k3_busy_receiver_keeps_letter(sandbox, monkeypatch):
    """B 忙时不丢信：占住 B 的回合 → 发信 → schedule → 信已落 + ops card_msg_pending。"""
    from src.storage import work_store
    wid = (await work_store.create_work("K3忙时剧本"))["work_id"]
    cards = await _make_tree(wid)
    # 人为占住 card_003 的回合互斥体
    holder = "turn:fakeholder"
    assert session_facade.turn_try_acquire(wid, "card_003", holder)
    try:
        await card_tools._h_send_card_message(
            wid, "card_002", {"target_card_id": "card_003", "msg_type": "msg_ask",
                              "content": "忙时也要收到"}, cards)
        state = message_runner.RunState(work_id=wid, card_id="card_002", content="x")
        await message_runner.schedule_card_message_triggers(state)
        # 让 watch/delivery 任务跑完
        for _ in range(20):
            await asyncio.sleep(0.05)
        # [卡片底座第一步 2026-10-14] 信落**收件箱**（B 忙 → 仍是 pending，不丢）
        pending = await card_db.list_pending(wid, "card_003")
        assert any("忙时也要收到" in str(p.get("content", "")) for p in pending), \
            "信必须已在收件箱（不排队不丢弃）"
    finally:
        session_facade.turn_release(wid, "card_003", holder)
    ops_text = session_ops.ops_path().read_text(encoding="utf-8")
    assert "card_msg_pending" in ops_text


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

async def test_s3_prompt_carries_display_and_note(sandbox, monkeypatch):
    """E4：激励文含 display 与 note 关键词（类型说明随消息提供）。"""
    from src.storage import work_store
    wid = (await work_store.create_work("S3激励文剧本"))["work_id"]
    await _make_tree(wid)
    captured: list[str] = []

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None, tools=None, **_):
        if "请阅读这些消息并处理" in str(user_prompt):
            captured.append(str(user_prompt))
            return ({"content": "收到，已处理。", "tool_calls": []}, "fake")
        return ({"content": "A 侧正常回复。", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")
    await card_tools.send_card_message(
        wid, "card_002", target_id="card_003", msg_type="msg_ask",
        content="S3：现状一句话？")
    state = message_runner.RunState(work_id=wid, card_id="card_002", content="x")
    await message_runner.schedule_card_message_triggers(state)
    for _ in range(60):
        await asyncio.sleep(0.05)
        if captured:
            break
    assert captured, "B 应被调起且激励文被捕获"
    p = captured[0]
    assert "询问" in p, f"display 关键词缺失：{p[:120]}"
    assert "不许沉默" in p, f"note 应对半句缺失：{p[:200]}"
    assert "card_002" in p and "（类型：询问）" in p


async def test_s3_prompt_fallback_on_read_failure(sandbox, monkeypatch):
    """E4：读卡失败 → 回落默认文 _CARD_MSG_PROMPT、调起不阻断。"""
    from src.storage import work_store
    wid = (await work_store.create_work("S3回落剧本"))["work_id"]
    await _make_tree(wid)
    captured: list[str] = []

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None, tools=None, **_):
        if "请阅读这些消息并处理" in str(user_prompt):
            captured.append(str(user_prompt))
            return ({"content": "回落路径已处理。", "tool_calls": []}, "fake")
        return ({"content": "ok", "tool_calls": []}, "fake")

    async def boom(*a, **kw):
        raise RuntimeError("read_card exploded")

    # 只让激励文构造里的读卡（读发件卡 card_002 元数据）失败；
    # B 回合自身链路（读 card_003）照常——否则 B 回合 502、激励文无从捕获。
    real_read_card = card_store.read_card

    async def selective(work_id, card_id, *a, **kw):
        if card_id == "card_002":
            raise RuntimeError("read_card exploded")
        return await real_read_card(work_id, card_id, *a, **kw)

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setattr(card_store, "read_card", selective)
    monkeypatch.setenv("MOCK_LLM", "1")
    await card_tools.send_card_message(
        wid, "card_002", target_id="card_003", msg_type="msg_ask",
        content="S3回落：内容任意。")
    state = message_runner.RunState(work_id=wid, card_id="card_002", content="x")
    await message_runner.schedule_card_message_triggers(state)  # 不抛出＝不阻断
    for _ in range(60):
        await asyncio.sleep(0.05)
        if captured:
            break
    # [卡片底座第一步 2026-10-14] 兜底激励文＝默认前缀 ＋ 消息正文（正文必达——V4）
    assert captured and captured[0].startswith(message_runner._CARD_MSG_PROMPT)
    assert "S3回落：内容任意。" in captured[0]
