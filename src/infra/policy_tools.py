# -*- coding: utf-8 -*-
"""模型面工具：让根卡自己设计 harness 策略（[2026-10-28 用户令]）。

## 用户口径

> 还需要给根卡一个工具，根卡可以利用 harness。它可以设计组合条件，以及触发的效果。

也就是说：**条件不是只有人能配**。根卡在自己的回合里发现「这件事需要盯」，
就能当场配一条：什么条件、盯哪些事件、多久的窗、凑够了提醒谁、说什么。

## 这个工具只做三件事

    ① 校验（复用 PolicySet._build，与配置文件同一套规则）
    ② 落盘（写进 config/harness_policies.json，带 `work` 字段只对自己项目生效）
    ③ 热加载（PolicySet.reload，改完立刻生效，不用重启）

**不做**：不自己去判断条件、不自己去发提醒——那是运行时的事，
本工具只负责「把规则写进表里」。

## 权限

只给根卡（`card_type == "project"`）。理由：策略是项目级的口径，
子卡各配各的会互相打架，且很难排查。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("infra.policy_tools")

#: 工具名（注册用）
TOOL_NAME = "design_policy"

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "把事件组合成自定义触发条件；条件成立时，用消息 / 唤醒 / 提示词注入影响卡片内模型的行为。\n"
        "目的是让协作更顺、卡片更聪明、活干得更准。\n"
        "\n"
        "配一条规则：\n"
        "· watch   看哪些事件\n"
        "· when    这些事件组成什么条件\n"
        "· target  影响哪张卡里的模型\n"
        "· message 写进它的对话历史，之后每轮都看得到\n"
        "· wake    要不要立刻叫醒它跑一轮\n"
        "\n"
        "配完立刻生效；同一 id 再调一次就是改它。喊过一次要重新攒条件，不会一直重复。\n"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string",
                   "description": "策略名（本项目内唯一）。重复即覆盖。"},
            "desc": {"type": "string",
                     "description": "人话说明：这条在盯什么。触发记录里会带上。"},
            "scope": {"type": "string", "enum": ["card", "relation", "work"],
                      "description": "本子挂在谁身上：card＝一张卡一本；"
                                     "relation＝**一对父子**一本（盯父子互动时用）；"
                                     "work＝整个项目一本。"},
            "watch": {"type": "array", "items": {"type": "string"},
                      "description": ("只关心哪几类事件：填已登记的事件类型名数组。"
                                      "拼错会当场报错并列出全部合法类型名。")},
            "window": {"type": "string",
                       "description": "时间窗：24h / 7d / 90m / none。超窗的旧账自动划掉。"},
            "when": {"type": "object",
                     "description": (
                         "条件树。对象，键决定节点类型，可用 all/any/not 任意嵌套："
                         "event＝{types: [类型名], where: {字段: 值}}，账里至少一条满足；"
                         "count＝{types, where} 同层再加 cmp 与 value，满足的条数与 value 比较；"
                         "field＝同层给 path / cmp / value，取上下文 work_id / card_id / parent 的值比较；"
                         "after＝{anchor: spec, expect: spec}，最近一条 anchor 之后没有 expect"
                         "（anchor 从未出现算不满足）；"
                         "all / any＝节点数组，全部 / 任一满足；not＝单个节点，取反。"
                         "spec = {types: [...], where: {...}}；types 空＝不限类型，where 空＝不限字段。"
                         "cmp: eq/ne/gt/ge/lt/le/in/contains/exists/matches，也认 >= <= > < == !=。")},
            "target": {"type": "string",
                       "description": "提醒发给谁：self（自己）/ parent（父卡）/ card:<id>。"},
            "message": {"type": "string",
                        "description": "提醒正文。可用 {card_id} {card_title} {parent} 占位。"},
            "mode": {"type": "string", "enum": ["message", "inject"],
                     "description": "用哪种手段：message＝写进它的会话历史，永久，之后每轮都看得到；"
                                    "inject＝只进它**下一轮**的上下文，整轮有效、不进历史、不唤醒"
                                    "（等它下次自己跑时生效）。默认 message。"},
            "wake": {"type": "boolean",
                     "description": "要不要把它叫起来跑一轮。true＝立刻处理；"
                                    "false＝只留一句话，等它下次跑时看到。"
                                    "（mode=inject 时无效——注入不唤醒。）"},
            "active": {"type": "boolean",
                       "description": "是否启用（默认 true）。设 false 等于先存着不生效。"},
        },
        "required": ["id", "watch", "when", "message"],
    },
}


def _config_path() -> Path:
    from . import policy_config
    return policy_config.config_path()


def _atomic_write(path: Path, doc: dict[str, Any]) -> None:
    """原子写：先写临时文件再替换，避免半截 JSON 让整张策略表失效。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".policy_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _build_policy(work_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """把工具参数组装成一条策略（带 work，只对本项目生效）。"""
    scope = str(args.get("scope") or "card").strip()
    return {
        "id": str(args.get("id") or "").strip(),
        "desc": str(args.get("desc") or "").strip(),
        "work": str(work_id or ""),
        "active": bool(args.get("active", True)),
        "scope": {"kind": scope},
        "watch": [str(x) for x in (args.get("watch") or []) if str(x).strip()],
        "window": str(args.get("window") or "none"),
        "when": args.get("when"),
        "then": {
            "target": str(args.get("target") or "self"),
            "message": str(args.get("message") or ""),
            "wake": bool(args.get("wake", True)),
            # [harness 三手段 2026-10-28 · 用户定义第三道接口]
            # message＝写进会话历史（永久，之后每轮都看得到）
            # inject ＝只进目标卡**下一轮**的上下文，整轮有效、不进历史、不唤醒
            "mode": str(args.get("mode") or "message"),
        },
    }


async def handle(work_id: str, current_id: str, arguments: dict[str, Any],
                 cards: dict[str, Any]) -> dict[str, Any]:
    """`design_policy` 的 handler。"""
    from . import policy_set

    def _err(code: str, msg: str) -> dict[str, Any]:
        return {"ok": False, "error": {"code": code, "message": msg}, "data": None}

    # ① 权限：只给根卡
    meta = (cards or {}).get(current_id) or {}
    if str(meta.get("card_type") or "") != "project":
        return _err("denied", "只有根卡能设计盯梢策略（项目级口径，子卡各配各的会打架）")

    raw = _build_policy(work_id, arguments)
    if not raw["id"]:
        return _err("validation", "id 不能为空")

    # ② 校验形状：用与配置文件同一套规则，坏条件当场拒（不要等运行时才发现）
    probe = policy_set.PolicySet()
    errs = probe.load([raw])
    if errs:
        return _err("validation", "策略不合法：" + "；".join(errs))

    # ③ 校验事件类型：watch 里写错一个字，这条策略就永远不触发，而形状校验看不出来
    bad_types = _unknown_event_types(raw["watch"])
    if bad_types:
        known = _known_event_types()
        return _err(
            "validation",
            "watch 里有未登记的事件类型：" + "、".join(bad_types)
            + "。拼错一个字母这条策略就永远不会触发。"
              "可用的事件类型：" + "、".join(known[:40])
            + (f"（共 {len(known)} 个，其余见 config/event_catalog.json）"
               if len(known) > 40 else ""))

    # ④ 落盘（同 id 覆盖，其余保留）
    path = _config_path()
    try:
        doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"policies": []}
    except (ValueError, OSError) as exc:
        return _err("internal", f"读策略表失败：{exc}")
    if not isinstance(doc, dict):
        doc = {"policies": []}
    items = [x for x in (doc.get("policies") or []) if isinstance(x, dict)]
    replaced = False
    for i, x in enumerate(items):
        if str(x.get("id")) == raw["id"]:
            items[i] = raw
            replaced = True
            break
    if not replaced:
        items.append(raw)
    doc["policies"] = items
    try:
        _atomic_write(path, doc)
    except OSError as exc:
        return _err("internal", f"写策略表失败：{exc}")

    # ⑤ 热加载：立刻生效
    errs = policy_set.dispatcher().reload()
    if errs:
        logger.warning("[policy_tools] 热加载有错（其余策略照常）：%s", errs)

    # ⑥ 试算：拿现有数据回放一遍，告诉它「按现在的样子会不会触发」
    #    这一步是**反馈够不够**的关键——配完只说"保存成功"，它没法知道这条到底
    #    是死是活（条件写太严就永远不触发，写太松就一直吵）。
    trial = _dry_run(work_id, raw)

    return {
        "ok": True,
        "data": {
            "id": raw["id"],
            "work": raw["work"],
            "active": raw["active"],
            "scope": raw["scope"]["kind"],
            "watch": raw["watch"],
            "window": raw["window"],
            "replaced": replaced,
            "trial": trial,
            "note": ("已生效，只盯本项目。同一 id 再调一次就是改这条；"
                     "active=false 可先停着。"
                     "trial 是按现有数据回放的结果——would_fire=0 且 scanned>0 说明"
                     "条件偏严（或这段时间确实没发生），scanned=0 说明这段时间没有可看的数据。"),
        },
    }


def _known_event_types() -> list[str]:
    """已登记的事件类型名（拼错时回给模型，省得它去猜）。"""
    try:
        from . import event_hub
        return sorted((event_hub.load_catalog() or {}).keys())
    except Exception:  # noqa: BLE001
        return []


def _unknown_event_types(watch: list[str]) -> list[str]:
    """watch 里哪些类型是 catalog 没登记的（拼错就永远不触发，必须当场拦）。

    注意不能用 ``fields_of()`` 判：它对**未登记**的类型也返回一份空契约
    （``{"part": [], "optional": [], "payload": []}``），非空 dict ⇒ 判不出来。
    要直接看目录里有没有这个键。
    """
    bad: list[str] = []
    try:
        from . import event_hub
        cat = event_hub.load_catalog() or {}
    except Exception:  # noqa: BLE001 拿不到目录就不拦（宁可放过）
        return bad
    for t in watch:
        if str(t) not in cat:
            bad.append(str(t))
    return bad


#: 试算最多回放多少条事实（7 天窗能到 24 万条，全跑要好几秒）
_TRIAL_MAX_FACTS = 20000


def _dry_run(work_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """拿数据集里的现有事实回放一遍，返回「会不会触发」。

    **不污染全局状态**：用独立的 PolicySet 和独立的本子，跑完就丢。
    """
    from . import fact_store, policy_set

    out: dict[str, Any] = {"scanned": 0, "would_fire": 0, "skipped": ""}
    try:
        win = policy_set.parse_window(raw.get("window"))
        now = __import__("time").time()
        since = (now - win) if win else (now - 7 * 86400)
        facts = fact_store.store().query(work_id, since=since)
        if len(facts) > _TRIAL_MAX_FACTS:
            out["skipped"] = f"数据太多（{len(facts)} 条），未试算；上线后照常生效"
            return out
        probe = policy_set.PolicySet()
        if probe.load([raw]):
            return out
        probe.seed(work_id, facts)
        out["scanned"] = len(facts)
        out["would_fire"] = len(probe.check_all(work_id))
    except Exception as exc:  # noqa: BLE001 试算失败不影响策略本身
        logger.warning("[policy_tools] 试算失败 %s: %s", raw.get("id"), exc)
        out["skipped"] = f"试算失败：{exc}"
    return out
