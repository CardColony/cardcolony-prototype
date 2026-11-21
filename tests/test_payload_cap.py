# -*- coding: utf-8 -*-
"""响应层载荷裁剪的网（[2026-10-26 · 治「切卡卡死」]）

## 背景（实测）

生产：`GET .../card_021/timeline?tail=400` 响应体 **36.9MB**
（其中 **31.8MB 是**一条 `tool_result`：`read_linked` 把**另一张卡的整条事件流**
回灌进了 `data.result` —— `value` 是 **36661 项的数组**，深嵌套）
⇒ 服务端序列化 7124ms ⇒ 客户端 15 秒超时 ⇒ 「请求超时」＋ 内存态建不起来。

前端工具行**默认折叠**（摘要只用 `content` 的第一行）⇒ 裁掉对用户零可见变化。

## 网里钉的不变量

| 编号 | 不变量 |
|---|---|
| C1 | 巨型**嵌套**载荷被整块换成摘要（不是只削字符串——第一版判错了） |
| C2 | 外层 `tool/target/ok` **不动** ⇒ 前端摘要行与状态点完全不变 |
| C3 | 普通记录**原样不动**（零行为变化：不加标记、不改内容） |
| C4 | 被裁过的记录标 `_truncated`；摘要里带 `_capped` 与原始规模 |
| C5 | 裁剪只动**响应**（落盘文件字节不变） |
| C6 | 单个超长**字符串**也会被截断（带提示）——兜住「就一个巨串」的情况 |
"""
from __future__ import annotations

import json
from pathlib import Path

from src.api import payload_cap


def _rec(seq: int, content: str) -> dict:
    return {"seq": seq, "card_id": "card_x", "run_id": "r1", "round": 1,
            "type": "tool_result",
            "data": {"tool": "read_linked", "target": "fn_006", "ok": True,
                     "result": {"ok": True, "content": content}},
            "model": "", "ts": "2026-10-26T00:00:00+08:00"}


def _nested_rec(seq: int, n: int) -> dict:
    """模拟那条真实记录：result.data.value 是 **n 项的深嵌套数组**。"""
    value = [{"seq": i, "type": "msg_notify",
              "data": {"result": {"data": {"value": [{"text": "片段 %d" % i}]}}}} for i in range(n)]
    return {"seq": seq, "card_id": "card_x", "run_id": "r1", "round": 1,
            "type": "tool_result",
            "data": {"tool": "read_linked", "target": "fn_006", "ok": True,
                     "result": {"ok": True, "data": {"card_id": "fn_006",
                                                    "part": "对话", "value": value}}},
            "model": "", "ts": "2026-10-26T00:00:00+08:00"}


def test_c1_giant_nested_payload_is_replaced_by_summary():
    """C1：巨型**嵌套**载荷被整块换成摘要（第一版只削字符串 ⇒ 只小 0.7MB）。"""
    rec = _nested_rec(1, 6000)
    payload_cap.cap_record(rec)
    res = rec["data"]["result"]
    assert res.get("_capped") is True, res
    assert "note" in res, res
    # 体积必须真的降下来
    size = len(json.dumps(rec, ensure_ascii=False))
    assert size < 20000, size


def test_c2_outer_fields_untouched():
    """C2：外层 tool/target/ok 不动 ⇒ 前端摘要行与状态点完全不变。"""
    rec = _nested_rec(1, 6000)
    payload_cap.cap_record(rec)
    d = rec["data"]
    assert d["tool"] == "read_linked"
    assert d["target"] == "fn_006"
    assert d["ok"] is True


def test_c3_normal_record_untouched():
    """C3：普通记录原样不动（零行为变化）。"""
    rec = _rec(1, "短内容")
    before = json.dumps(rec, ensure_ascii=False)
    payload_cap.cap_record(rec)
    assert json.dumps(rec, ensure_ascii=False) == before
    assert "_truncated" not in rec


def test_c4_truncated_flag():
    """C4：被裁过的记录标 `_truncated`；摘要里带 `_capped` 与原始规模。"""
    rec = _nested_rec(1, 6000)
    payload_cap.cap_record(rec)
    assert rec.get("_truncated") is True
    assert rec["data"]["result"]["_capped"] is True


def test_c5_disk_untouched(tmp_path: Path):
    """C5：裁剪只动响应，落盘文件字节不变。"""
    p = tmp_path / "card_x.jsonl"
    original = json.dumps(_nested_rec(1, 6000), ensure_ascii=False)
    p.write_text(original + "\n", encoding="utf-8")
    raw_before = p.read_bytes()

    items = [json.loads(original)]
    payload_cap.cap_records(items)
    assert items[0]["data"]["result"].get("_capped") is True   # 响应被裁了
    assert p.read_bytes() == raw_before                          # 落盘没动


def test_c6_single_huge_string_is_truncated():
    """C6：单个超长**字符串**也会被截断（带提示）。"""
    rec = _rec(1, "摘要第一行\n" + "z" * 200000)
    payload_cap.cap_record(rec, cap=4096)
    content = rec["data"]["result"]["content"]
    assert "已截断" in content, content[-80:]
    assert content.split("\n")[0] == "摘要第一行"


# ═══════════════════════════════════════════════════════════════════════
# [窄投影 2026-11-21 · 性能批次二] view=brief 的投影层测试
# ═══════════════════════════════════════════════════════════════════════

def _tr(seq: int, result_chars: int = 20000) -> dict:
    return {"seq": seq, "type": "tool_result",
            "data": {"tool": "read_card", "target": "card_x", "ok": True,
                     "step_id": "s1", "origin": "main",
                     "result": {"ok": True, "content": "x" * result_chars}}}


def test_brief_tool_result_result_stubbed():
    """tool_result.data.result → {\\"_brief\\":true,ok} 占位；外层标量不动。"""
    rec = _tr(1)
    payload_cap.brief_record(rec)
    assert rec["_brief"] is True
    assert rec["data"]["result"]["_brief"] is True
    assert rec["data"]["result"]["ok"] is True
    assert rec["data"]["result"]["_bytes"] > 2048   # 有界测量：下限账提示体量
    assert rec["data"]["tool"] == "read_card"      # 摘要字段原样
    assert rec["data"]["step_id"] == "s1"


def test_brief_tool_result_small_result_untouched():
    """小结果不值得详情往返——不窄化。"""
    rec = _tr(1, result_chars=500)
    payload_cap.brief_record(rec)
    assert "_brief" not in rec
    assert rec["data"]["result"]["content"] == "x" * 500


def test_brief_tool_call_long_args_previewed():
    """tool_call 的超长参数值 → 预览（类型仍是字符串，摘要行零差异）。"""
    rec = {"seq": 2, "type": "tool_call",
           "data": {"tool": "edit_card", "tool_call_id": "c1",
                    "arguments": {"card_id": "card_x", "path": "/a/b.md",
                                  "new_string": "y" * 5000, "old_string": "z" * 20}}}
    payload_cap.brief_record(rec)
    assert rec["_brief"] is True
    args = rec["data"]["arguments"]
    assert args["new_string"].endswith("…") and len(args["new_string"]) < 500
    assert args["old_string"] == "z" * 20          # 短值原样
    assert args["path"] == "/a/b.md"


def test_brief_reasoning_main_previewed_seat_kept():
    """主模型思考 → 预览；带 model 的席内思考（常驻可见）不窄化。"""
    main = {"seq": 3, "type": "reasoning", "data": {"text": "第一行摘要\n" + "t" * 2000}}
    payload_cap.brief_record(main)
    assert main["_brief"] is True
    assert main["data"]["text"].endswith("…")
    assert main["data"]["text"].startswith("第一行摘要")

    seat = {"seq": 4, "type": "reasoning", "model": "seat-a",
            "data": {"text": "x" * 2000}}
    payload_cap.brief_record(seat)
    assert "_brief" not in seat
    assert seat["data"]["text"] == "x" * 2000


def test_brief_card_block_edited_old_new_previewed():
    rec = {"seq": 5, "type": "card_block_edited",
           "data": {"card_type": "sub_project", "block": "现状", "by": "card_007",
                    "old_len": 5000, "new_len": 5000,
                    "old": "o" * 5000, "new": "n" * 5000}}
    payload_cap.brief_record(rec)
    assert rec["_brief"] is True
    assert rec["data"]["old"].endswith("…") and len(rec["data"]["old"]) < 500
    assert rec["data"]["by"] == "card_007" and rec["data"]["old_len"] == 5000


def test_brief_visible_content_untouched():
    """可见正文类型整体豁免——一个字不动。"""
    for t in ("user_message", "assistant_text", "msg_report", "plan_created",
              "system_result", "seat_end", "coder_done"):
        rec = {"seq": 9, "type": t,
               "data": {"content": "c" * 90000, "text": "c" * 90000,
                        "steps": ["s" * 90000]}}
        before = json.dumps(rec, ensure_ascii=False)
        payload_cap.brief_record(rec)
        assert json.dumps(rec, ensure_ascii=False) == before, t
        assert "_brief" not in rec


def test_brief_fallback_only_top_level_strings():
    """通用兜底：>8KB 的记录只预览顶层超长串；容器/短字段一律不碰。"""
    rec = {"seq": 10, "type": "harness_message",
           "data": {"to": "card_x", "kind": "task",
                    "regions": ["a", "b", "c"],            # 数组不动（行首 .join 用）
                    "text": "w" * 20000}}
    payload_cap.brief_record(rec)
    assert rec["_brief"] is True
    assert rec["data"]["text"].endswith("…")
    assert rec["data"]["regions"] == ["a", "b", "c"]
    assert rec["data"]["kind"] == "task"


def test_brief_fallback_small_record_untouched():
    rec = {"seq": 11, "type": "harness_message",
           "data": {"to": "x", "text": "w" * 1000}}
    payload_cap.brief_record(rec)
    assert "_brief" not in rec


def test_brief_records_mixed_list():
    items = [_tr(1), {"seq": 2, "type": "user_message", "data": {"content": "hi"}},
         {"seq": 3, "type": "reasoning", "data": {"text": "short"}}]
    out = payload_cap.brief_records(items)
    assert out is items                                    # 就地
    assert items[0]["_brief"] is True
    assert "_brief" not in items[1] and "_brief" not in items[2]
