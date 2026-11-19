# -*- coding: utf-8 -*-
"""弱模型四组对照实验（2026-11-19 · 任务书「弱模型对照」节的可复跑实现）。

设计（逐条对准任务书）：
- 固定场景、样本数与重复次数：6 话术 × 4 组 × N 次重复（--repeat，缺省 3）。
- 每组相同端点（--model 指定的弱模型，隔离 work 的模型配置表钉住）、相同
  工具清单（同一张受试卡的权限面）、等价的隔离资源（独立 work＋固定测试数据）。
- 循环变量只在首轮模型输出之后介入：A/B 共用基线（兼容代执行＋无交付闸），
  C/D 开按回执交付；B/D 加当前轮短提示。首轮请求的可对照性经 prompt 日志
  快照核对（logs/prompt_*.json）。
- 执行与判定全部用系统事实（事件流/执行记录/台账/资源终态），不用模型自述。

组矩阵：
    A  hint=0  gate=0   （共用基线）
    B  hint=1  gate=0   （仅加当前轮提示）
    C  hint=0  gate=1   （仅按回执交付）
    D  hint=1  gate=1   （两者合用）

指标（按组按工具类别）：
    正确首调率 / 零调用率 / 正文伪调用率 / 原生协议错误率 /
    实际派发与最终效果率 / 虚报成功率 / 误派发率 / 请求体积（首轮 prompt 字节）

用法：
    python scripts/receipt_ab_experiment.py --model ds-v41-flash-oc3 --repeat 3
    （限制 LLM 调用：每回合 1 次起轮；不设 ECO_NO_AUTO_DELIVER 时 direct 投递
    亦为一次内投——本脚本固定关投递防意外唤起，direct 效果核验只看落信。）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# key 注入：服务进程由 start.bat 灌 .env；本脚本进程自己加载（只读键值，
# 不打印任何值）。已存在的环境变量不覆盖（显式 env 优先）。
_ENV_FILE = ROOT / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

# 固定测试数据（模型无法从提示词猜到——任务书确定性验收要求）
TEST_TOKEN = "B7X-2291-CAL"
TEST_FILE_BODY = (
    "校准台账 v3\n"
    f"编号 {TEST_TOKEN} 归档于 2026-11-19 16:00（封闭数据，仅本实验引用）\n"
    "下一行是核对锚点：ANCHOR=4F2A-99\n"
)

GROUPS = {
    "A": {"TOOL_TURN_HINT": "0", "TOOL_RECEIPT_GATE": "0", "TOOL_CALL_STRICT": ""},
    "B": {"TOOL_TURN_HINT": "1", "TOOL_RECEIPT_GATE": "0", "TOOL_CALL_STRICT": ""},
    "C": {"TOOL_TURN_HINT": "0", "TOOL_RECEIPT_GATE": "1", "TOOL_CALL_STRICT": ""},
    "D": {"TOOL_TURN_HINT": "1", "TOOL_RECEIPT_GATE": "1", "TOOL_CALL_STRICT": ""},
}

#: 话术 → (工具类别, 期望工具集, 是否控制样本)
SCRIPTS = [
    ("lookup",  "查证",   {"read_card"},   False,
     "读一下你这棵树里子卡卡面上现在写的内容，把卡面标题行原样告诉我。"),
    ("search",  "搜索",   {"search_files"}, False,
     f"在工作区文件里搜一下哪个文件包含 {TEST_TOKEN} 这个编号，告诉我文件名。"),
    ("write",   "写入",   {"edit_card", "update_shadow", "edit_shadow"}, False,
     f"在子卡的现状区追加一行「{TEST_TOKEN} 校准完成」。"),
    ("send",    "卡间指令", {"direct"},    False,
     "给子卡发一条指令，让它准备校准；指令必须带验收判据。"),
    ("explain", "控制·解释", set(),        True,
     "解释一下 msg_id 4692 这种编号的一般格式含义，不用做任何操作。"),
    ("vague",   "控制·弱话术", set(),     True,
     "顺便看看卡上现在是什么情况。"),
]

_DONE_WORDS = ("已发", "已写入", "已送达", "已执行", "已追加", "已完成", "已唤醒",
               "已更新", "已发指令", "已发送")


async def _build_env(model: str) -> dict:
    """隔离 work＋两张卡＋测试文件＋模型配置表钉弱模型。"""
    from src.api import card_tools
    from src.storage import card_store, work_store

    w = await work_store.create_work("工具回执四组对照实验")
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    # 受试卡（所有话术发给它）＋指令目标卡（与受试卡有父子默认线）
    subj = (await card_store.create_card(
        wid, "受试卡", "模型生成", parent=root))["meta"]["card_id"]
    target = (await card_store.create_card(
        wid, "指令目标卡", "模型生成", parent=root))["meta"]["card_id"]
    # 测试文件（固定数据）
    froot = Path(os.environ.get("RECEIPT_AB_FILE_ROOT") or (ROOT / "trading-workspace"))
    fdir = froot / f"_receipt_ab_{wid}"
    fdir.mkdir(parents=True, exist_ok=True)
    fpath = fdir / "calib_ledger.txt"
    fpath.write_text(TEST_FILE_BODY, encoding="utf-8")
    # 模型配置表：受试卡钉弱模型
    mtab = Path(ROOT) / "works" / wid / "model_config_table.json"
    mtab.parent.mkdir(parents=True, exist_ok=True)
    mtab.write_text(json.dumps({
        "_meta": {"version": 2},
        "cards": {subj: {"model": model}},
        "policy": {"types": {}},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"work_id": wid, "root": root, "subject": subj,
            "target": target, "file": str(fpath), "model": model}


def _fill(placeholders: str, env: dict) -> str:
    return (placeholders
            .replace("{target}", env["target"])
            .replace("{file}", env["file"]))


def _events_of(wid: str, card: str) -> list[dict]:
    p = ROOT / "works" / wid / "events" / f"{card}.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


async def _run_one(env: dict, group: str, key: str, cat: str, expected: set,
                   control: bool, prompt: str) -> dict:
    """跑一个样本回合，返回该样本的事实读数（全部系统事实，零模型自述）。"""
    from src.api import card_tools, message_runner, routes, tool_receipt

    # 组开关（循环变量——首轮请求之后才影响纠正/交付；首轮请求内容只受
    # TOOL_TURN_HINT 影响，这正是 B/D 与 A/C 的差异轴）
    for k, v in GROUPS[group].items():
        if v == "":
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    ev_before = len(_events_of(env["work_id"], env["subject"]))
    ledger_before = _ledger_len()
    res = await routes._run_message(env["work_id"], env["subject"], prompt)
    if "error" in res:
        return {"group": group, "key": key, "cat": cat, "error": str(res["error"])}

    # 事件面（本回合新增）
    events = _events_of(env["work_id"], env["subject"])[ev_before:]
    kinds = [e.get("type") for e in events]
    te = res.get("tool_events") or []
    ok_tools = [e.get("tool") for e in te if e.get("ok") is True]
    first_round_dispatched = [e.get("tool") for e in te
                              if e.get("ok") is True and e.get("round") == 1]

    # 首调判定：台账里 round==1 的派发行（台账带 round，事件面不带）
    led = _read_ledger()[ledger_before:]
    first_round_calls = [r for r in led
                         if r.get("round") == 1 and r.get("dispatched")]
    first_native = [r for r in first_round_calls if r.get("origin") == "native"]

    # 资源终态核验（最终效果——不只看回执）
    effect = await _verify_effect(env, key, ok_tools)

    reply = str(res.get("reply") or "")
    claims_done = any(w in reply for w in _DONE_WORDS)
    false_success = bool(claims_done and not ok_tools and not effect["done"])
    misfire = bool(control and (te or first_round_calls))

    # 请求体积：prompt 日志（本回合）首轮 messages 字节
    prompt_bytes = _latest_prompt_bytes(env["work_id"], env["subject"])

    return {
        "group": group, "key": key, "cat": cat, "control": control,
        "expected": sorted(expected),
        "first_call_native": len(first_native) > 0,
        "first_call_any": len(first_round_calls) > 0,
        "zero_call": len(te) == 0 and len(first_round_calls) == 0,
        "pseudo_rescue": kinds.count("rescue") + kinds.count("pseudo_call_refused")
                         + kinds.count("pseudo_call_unparsed"),
        "proto_error": kinds.count("native_protocol_error"),
        "dispatched": len(te),
        "ok_count": len(ok_tools),
        "effect_done": effect["done"], "effect_detail": effect["detail"],
        "claims_done": claims_done,
        "false_success": false_success,
        "misfire": misfire,
        "action_not_executed": kinds.count("action_not_executed"),
        "prompt_bytes": prompt_bytes,
        "reply_head": reply[:120],
    }


def _ledger_len() -> int:
    from src.api import tool_receipt
    p = tool_receipt._LEDGER_PATH
    if not p.exists():
        return 0
    return len([l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()])


def _read_ledger() -> list[dict]:
    from src.api import tool_receipt
    p = tool_receipt._LEDGER_PATH
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()]


async def _verify_effect(env: dict, key: str, ok_tools: list) -> dict:
    """资源终态核验（系统事实）：写入看卡面、指令看收件卡会话、查证/搜索
    的「效果」＝回执本身（读与搜无副作用，效果核验并入 ok_count）。"""
    from src.storage import card_store, session_facade
    if key == "write":
        meta = (await card_store.read_card(env["work_id"], env["target"])).get("meta") or {}
        blob = json.dumps(meta, ensure_ascii=False)
        done = TEST_TOKEN in blob
        return {"done": done, "detail": "target 卡面含测试编号" if done else "target 卡面无测试编号"}
    if key == "send":
        blocks = await session_facade.read(env["work_id"], env["target"])
        msg_blks = [b for b in blocks
                    if str(b.get("actor") or "").startswith("card:")
                    or (b.get("content") and "msg_directive" in str(b.get("data") or ""))]
        # 收件卡会话里出现来自受试卡的卡间消息块 ＝ 落信事实
        has_msg = any(str(b.get("actor") or "").startswith("card:") for b in blocks)
        return {"done": has_msg, "detail": f"收件卡卡间消息块 {len(msg_blks)} 条"}
    # lookup/search/explain/vague：效果即回执（无独立资源终态）
    return {"done": bool(ok_tools), "detail": "回执即效果"}


def _latest_prompt_bytes(wid: str, card: str) -> int:
    logs = ROOT / "logs"
    cands = sorted(logs.glob(f"prompt_{card}_*.json"))
    if not cands:
        return 0
    try:
        payload = json.loads(cands[-1].read_text(encoding="utf-8"))
        msgs = payload.get("prior_messages") or []
        body = json.dumps(msgs, ensure_ascii=False) + str(payload.get("user_prompt") or "")
        return len(body.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return 0


def _metrics(rows: list[dict]) -> dict:
    """按组汇总：正确首调率、零调用率、正文伪调用率、协议错误率、
    最终实际完成率、虚报成功率、误派发率、请求体积均值。分母＝该组非控制
    样本（误派发率分母＝控制样本）。"""
    out: dict[str, dict] = {}
    for g in GROUPS:
        rs = [r for r in rows if r.get("group") == g and not r.get("error")]
        ctrl = [r for r in rs if r.get("control")]
        task = [r for r in rs if not r.get("control")]
        out[g] = {
            "n_task": len(task), "n_control": len(ctrl),
            "first_call_rate": _rate(task, "first_call_native"),
            "zero_call_rate": _rate(task, "zero_call"),
            "pseudo_rate": _rate(task, "pseudo_rescue", any_hit=True),
            "proto_error_rate": _rate(task, "proto_error", any_hit=True),
            "final_effect_rate": _rate(task, "effect_done"),
            "false_success_rate": _rate(task, "false_success"),
            "misfire_rate": _rate(ctrl, "misfire"),
            "avg_prompt_bytes": (sum(r.get("prompt_bytes") or 0 for r in rs) // len(rs))
                                if rs else 0,
            "raw": rs,
        }
    return out


def _rate(rows: list[dict], key: str, any_hit: bool = False) -> float:
    if not rows:
        return 0.0
    if any_hit:
        hits = sum(1 for r in rows if (r.get(key) or 0) > 0)
    else:
        hits = sum(1 for r in rows if r.get(key))
    return round(hits / len(rows), 3)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="ds-v41-flash-eco-amr")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    # 实验纪律：关投递防意外唤起（direct 只验落信）；台账随 work 走
    os.environ["ECO_NO_AUTO_DELIVER"] = "1"
    os.environ["ECO_NO_AUTO_REVIEW"] = "1"
    from src.api import tool_receipt
    from src.api.routes import _run_message  # noqa: F401 预热 import

    env = await _build_env(args.model)
    print(f"[receipt-ab] work={env['work_id']} subject={env['subject']} "
          f"target={env['target']} model={args.model} repeat={args.repeat}")

    rows: list[dict] = []
    for g in ("A", "B", "C", "D"):
        for rep in range(1, args.repeat + 1):
            for key, cat, expected, control, tpl in SCRIPTS:
                prompt = _fill(tpl, env)
                t0 = time.monotonic()
                try:
                    r = await _run_one(env, g, key, cat, expected, control, prompt)
                except Exception as exc:  # noqa: BLE001 单样本失败不毁全局
                    r = {"group": g, "key": key, "cat": cat, "error": repr(exc)}
                r["seconds"] = round(time.monotonic() - t0, 1)
                rows.append(r)
                print(f"[receipt-ab] {g} r{rep} {key:<8} "
                      f"first={r.get('first_call_native')} zero={r.get('zero_call')} "
                      f"ok={r.get('ok_count')} effect={r.get('effect_done')} "
                      f"false={r.get('false_success')} mis={r.get('misfire')} "
                      f"({r.get('seconds')}s)")

    report = {"meta": {"model": args.model, "repeat": args.repeat,
                       "work_id": env["work_id"], "subject": env["subject"],
                       "token": TEST_TOKEN},
              "metrics": _metrics(rows)}
    outp = Path(args.out) if args.out else (
        ROOT / "logs" / f"receipt_ab_{time.strftime('%Y%m%d_%H%M%S')}.json")
    outp.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"[receipt-ab] 报告 → {outp}")
    for g, m in report["metrics"].items():
        print(f"  组{g}: 首调={m['first_call_rate']} 零调={m['zero_call_rate']} "
              f"伪调={m['pseudo_rate']} 协议错={m['proto_error_rate']} "
              f"效果={m['final_effect_rate']} 虚报={m['false_success_rate']} "
              f"误派={m['misfire_rate']} 体积={m['avg_prompt_bytes']}")


if __name__ == "__main__":
    asyncio.run(main())
