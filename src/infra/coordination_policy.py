"""Project coordination's authoring surface for native Harness policies."""
from __future__ import annotations

import hashlib
import json
import re
import time
from math import isfinite
from typing import Any

from . import event_hub, fact_store, policy_config, policy_set, policy_tools

MODE = "inspect"
TARGET = "coordination"
_SUBJECT = {"card_stopped": "target", "worker_done": "from", "msg_report": "from",
            "msg_sync": "from", "coder_done": "from", "msg_directive": "from"}


def policies(work_id: str, assignment_id: str) -> list[dict[str, Any]]:
    return [raw for raw in policy_config.load_raw()
            if raw.get("work") == work_id and (then := raw.get("then") or {}).get("mode") == MODE
            and then.get("assignment_id") == assignment_id]


def binding(then: dict) -> list:
    return [then.get("signature"), then.get("saved_at")]


def bindings_current(work_id: str, assignment_id: str, bindings: dict) -> bool:
    dispatcher = policy_set.dispatcher()
    for pid, expected in bindings.items():
        policy = dispatcher.policy(pid)
        if (not policy or not policy.active or policy.work != work_id
                or policy.then.get("assignment_id") != assignment_id or binding(policy.then) != expected):
            return False
    return True


def tool_schema() -> dict[str, Any]:
    events = sorted(name for name, spec in event_hub.load_catalog().items()
                    if spec.get("triggerable"))
    return {"name": policy_tools.TOOL_NAME, "description": (
        "读、预览或保存一条正式 Harness 策略。read 看已安装的规则，可用 trigger 查事件含义；"
        "preview 抽样回放真实历史，零保存、零唤醒；save 只新增或覆盖一个 id，从保存后的新事件开始记账。"
        "先用 preview 核对实际命中的卡和事件，save 使用相同参数。active=false 停用指定 id。"
        "命中只开弱模型巡检子会话。弱模型需要强模型时调用接手工具。"
        "目标完工用 card_goal_completed；普通对话结束 turn_end 会命中每次结束；"
        "实际停手用 card_halted、card_stopped 或 card_stopped_self；持续静默才用 card_idle 加 idle_for。"
        '例如预览 {"op":"preview","id":"s1-done","trigger":"worker_done",'
        '"card_ids":["card_011"],"check":"核对正式复跑回执和责任卡下一步",'
        '"wake_when":"确认正式路径存在跨域阻塞，责任卡已无法推进，需要统筹确定接手人或验收口径时接手"}。'
    ), "parameters": {"type": "object", "required": ["op"], "properties": {
        "op": {"type": "string", "enum": ["read", "preview", "save"]},
        "id": {"type": "string", "description": "本项目内的规则名，保存其他 ID 不影响已有规则。"},
        "trigger": {"type": "string", "enum": events,
                    "description": "哪一种事件有权发起这次巡检。read 可用它查看主体字段与含义。"},
        "idle_for": {"type": "string", "description": "仅配 card_idle 时必填，例如 12h。由已有静默扫描检出，同一段静默最多起一次。"},
        "card_ids": {"type": "array", "items": {"type": "string"},
                     "description": "哪些卡的动作触发。汇报和完工回执按发件卡，卡片被停止按被停的卡。"},
        "all_cards": {"type": "boolean", "description": "只有用户明确要全项目监听时才用 true 代替 card_ids。"},
        "check": {"type": "string", "description": "起轮后弱模型需要查什么；这句话不参与触发。"},
        "wake_when": {"type": "string", "description": "弱模型查到什么变化时，需要强模型作何裁定或处置。它只参与是否接手的判断。"},
        "observe": {"type": "array", "items": {"type": "string"},
                    "description": "复杂条件要看哪些额外事实。它们参与记账，单独出现时不会起轮。"},
        "when": policy_tools.SCHEMA["parameters"]["properties"]["when"],
        "window": policy_tools.SCHEMA["parameters"]["properties"]["window"],
        "active": {"type": "boolean", "description": "设 false 停用指定规则；旧的未执行命中也不再启动。"},
    }}}


def _compile(work_id: str, aid: str, args: dict, allowed: set[str]) -> dict:
    name = args.get("id")
    if not isinstance(name, str) or not re.fullmatch(r"[\w-]+", name):
        raise ValueError("请给规则一个只含文字、数字、下划线或短横线的 id。")
    pid = f"inspection/{work_id}/{name}"
    current = next((p for p in policies(work_id, aid) if p["id"] == pid), None)
    if "active" in args and type(args["active"]) is not bool:
        raise ValueError("active 须为布尔值。")
    if args.get("active") is False and not args.get("trigger") and current:
        if set(args) - {"op", "id", "active"}:
            raise ValueError("只停用已有规则时，只需 op、id、active=false。")
        return {**current, "active": False}
    catalog = event_hub.load_catalog()
    trigger = args.get("trigger")
    if not isinstance(trigger, str) or not catalog.get(trigger, {}).get("triggerable"):
        raise ValueError("请选择已登记、可触发的 trigger 事件。")
    ids = args.get("card_ids")
    all_cards = args.get("all_cards") is True
    if all_cards and ids is not None:
        raise ValueError("card_ids 与 all_cards=true 选其中一个。")
    if not all_cards and (not isinstance(ids, list) or not ids
                          or any(not isinstance(cid, str) or cid not in allowed for cid in ids)):
        raise ValueError("请给出要观察的有效 card_ids，明确全项目时用 all_cards=true。")
    check, wake_when = (args.get(k) for k in ("check", "wake_when"))
    if any(not isinstance(value, str) or not value.strip() for value in (check, wake_when)):
        raise ValueError("请写明弱模型查什么（check）和何时交给强模型（wake_when）。")
    watch = args.get("observe", [])
    if not isinstance(watch, list) or any(not isinstance(v, str) or v not in catalog for v in watch):
        raise ValueError("observe 须为已登记的事件类型数组。")
    when = [{"field": "trigger_data.type", "value": trigger}]
    subject = _SUBJECT.get(trigger, "card")
    if ids:
        when.append({"field": f"trigger_data.{subject}", "cmp": "in", "value": sorted(set(ids))})
    if "when" in args:
        when.append(args["when"])
    if trigger == "card_idle":
        seconds = policy_set.parse_window(args.get("idle_for"))
        if seconds is None or not isfinite(seconds) or seconds <= 0:
            raise ValueError("card_idle 必须给出静默多久后检查，例如 idle_for=12h。")
        when.extend(({"field": "trigger_data.silent_sec", "cmp": "ge", "value": seconds},
                     {"field": "trigger_data.silent_since", "cmp": "gt", "value": 0}))
    elif "idle_for" in args:
        raise ValueError("idle_for 只用于 card_idle。")
    raw = policy_tools._build_policy(work_id, {**args, "id": pid, "desc": check.strip(),
        "scope": "work", "watch": list(dict.fromkeys([trigger, *watch])), "when": {"all": when},
        "target": f"card:{TARGET}", "message": check.strip()})
    then = {**raw["then"], "mode": MODE, "assignment_id": aid, "trigger": trigger,
            "subject": subject, "card_ids": sorted(set(ids or ())), "wake_when": wake_when.strip()}
    if trigger == "card_idle":
        then.update(idle_for=args["idle_for"], dedup_fields=["card", "silent_since"])
    signature = hashlib.sha256(json.dumps({**raw, "then": then}, sort_keys=True).encode()).hexdigest()
    raw["then"] = {**then, "signature": signature}
    errors = policy_set.PolicySet().load([raw])
    if errors:
        raise ValueError("；".join(errors))
    return raw


def _preview(raw: dict) -> dict:
    """Replay native dispatch without installing policies or delivering any action."""
    clock = 0.0
    probe = policy_set.PolicySet(now_fn=lambda: clock)
    probe.load([raw])
    work_id = raw["work"]
    sample = fact_store.store().query(work_id, types=raw["watch"], newest_first=True, limit=101)
    matches = []
    seen = set()
    for fact in reversed(sample[:100]):
        clock = fact.get("_t") or clock
        for hit in probe.dispatch(work_id, fact):
            fields = raw["then"].get("dedup_fields") or []
            key = tuple(fact.get(field) for field in fields)
            if not key or key not in seen:
                subject = raw["then"]["subject"]
                matches.append({**hit["basis"], "recorded_on_card": fact.get("card"),
                                "matched_card": fact.get(subject), "matched_on": subject})
                if key:
                    seen.add(key)
    return {"scanned": min(len(sample), 100), "sample_partial": len(sample) > 100,
            "would_fire": len(matches), "recent_matches": matches[-5:]}


def handle(work_id: str, aid: str, args: dict, allowed: set[str]) -> dict[str, Any]:
    op = args.get("op")
    keys = {"op", "id", "trigger"} if op == "read" else tool_schema()["parameters"]["properties"]
    if unknown := set(args) - set(keys):
        raise ValueError("工具没有这些参数：" + "、".join(sorted(unknown)))
    if op == "read":
        if event := args.get("trigger"):
            spec = event_hub.load_catalog().get(event)
            if spec is None:
                raise ValueError("该事件未登记。")
            return {"op": op, "event": event, "catalog": spec, "subject": _SUBJECT.get(event, "card"),
                    "may_start_inspection": bool(spec.get("triggerable")),
                    **({"requires": "idle_for；同一段静默只起一次，沿用现有静默扫描。"} if event == "card_idle" else {})}
        installed = policies(work_id, aid)
        if name := args.get("id"):
            installed = [raw for raw in installed if raw["id"].rsplit("/", 1)[-1] == name]
        return {"op": op, "policies": installed}
    if op not in ("preview", "save"):
        raise ValueError("op 请选择 read、preview 或 save。")
    raw = _compile(work_id, aid, args, allowed)
    trial = _preview(raw)
    previous = next((p for p in policies(work_id, aid) if p["id"] == raw["id"]), None)
    result = {"op": op, "policy": raw, "previous": previous, "trial": trial, "saved": op == "save"}
    if op == "save":
        raw["then"] = {**raw["then"], "saved_at": time.time()}
        result["replaced"] = policy_tools.save_policy(raw)
    return result
