# -*- coding: utf-8 -*-
"""卡间消息 · 类型注册表读取层与正文头行纯函数（迭代四 K1）。

两半职责：
1. **类型注册表读取层**：`config/message_type_registry.json` 的唯一读取口。
   每次调用现读（照 `src/roles.py` `_load_table()` 热加载先例），
   「加类型＝加条目、零代码」靠这一点成立。坏表/缺文件 → `logger.warning` ＋
   返回 `{}`（fail-open 到空值域，由调用方拒绝），绝不抛出。
2. **正文头行纯函数**：卡间消息块正文首行的结构化头
   `[[card-msg id=<msg_id> type=<type_code> from=<from_card> reply_to=<msg_id|空>]]`
   的构建／解析／剥离（语法与键值规则见《迭代四方案-卡间通信接口与底座》§4.2）。

约束（任务书 K1⑤）：本模块**禁 IO 副作用**（`load_registry` 读表除外）；
不注册任何工具；不改 `session_facade` 的 `BLOCK_FIELDS` 等常量。
解析失败不是错误，是数据形态之一种——`parse_header` 不匹配返 `None`，不抛异常。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger("api.card_msg")

BASE_DIR = Path(__file__).resolve().parents[2]
REGISTRY_PATH = BASE_DIR / "config" / "message_type_registry.json"

# 头行正则（卡间册 §4.2）：只匹配**首行**、允许首尾空白、`]]` 后不得再有内容。
_HEADER_RE = re.compile(r"^\s*\[\[card-msg\s+(.+?)\]\]\s*$")
# 键：小写 ASCII 下划线（`id`/`type`/`from`/`reply_to`…）；中文或其它字符＝非法 → 解析失败。
_KEY_RE = re.compile(r"^[a-z_]+$")
# 值：标识符安全集（卡 id／msg_* 类型码／uuid hex 均在其内）；空值仅 reply_to 合法。
_VALUE_RE = re.compile(r"^[A-Za-z0-9_\-:.]+$")


def load_registry() -> dict[str, dict[str, str]]:
    """读类型注册表并返回 `types` 子表。**每次调用现读**（热加载先例）。

    坏表/缺文件 → `logger.warning` ＋ 返 `{}`（fail-open，由调用方拒绝）。
    """
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("消息类型注册表不存在: %s", REGISTRY_PATH)
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("消息类型注册表读取失败 %s: %s", REGISTRY_PATH, exc)
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("types"), dict):
        logger.warning("消息类型注册表形态非法（缺 types 或非对象）: %s", REGISTRY_PATH)
        return {}
    return data["types"]


def type_codes() -> tuple[str, ...]:
    """注册表全部类型码（保持注册表文件内的声明序）。"""
    return tuple(load_registry().keys())


def display_of(code: str) -> str:
    """类型码 → 显示名；**未命中返回 code 本身**（前端容错：不认识的类型显示原文而非空白）。"""
    entry = load_registry().get(code)
    if isinstance(entry, dict) and entry.get("display"):
        return str(entry["display"])
    return code


# ---------------------------------------------------------------------------
# [迭代五 S1] 应对规则读取函数（零行为变化·先行件）。
# 注册表是应对规则的唯一事实源：when_to_use（何时用/收件方应对说明的数据源）、
# reply_expected（收下后是否须作答并自动回发）、reply_type（回发用的类型码）。
# 四个函数全部**现读注册表**（同 load_registry 热加载语义）、fail-open 到缺省值、
# 绝不抛出——消费方（提示层 S3 / 回发流程 S4）据此零类型字面量。
# ---------------------------------------------------------------------------
def entry_of(code: str) -> dict | None:
    """类型码 → 注册表 entry 原样 dict；未命中/形态非对象返回 `None`。"""
    entry = load_registry().get(code)
    return entry if isinstance(entry, dict) else None


def when_to_use_of(code: str) -> str:
    """类型码 → when_to_use 文案；缺省（未命中/缺键/非字符串）返回 `""`。"""
    entry = entry_of(code)
    value = (entry or {}).get("when_to_use")
    return str(value) if value else ""


def reply_expected_of(code: str) -> bool:
    """类型码 → 是否须作答并回发；缺省（未命中/缺键）返回 `False`。"""
    entry = entry_of(code)
    return bool((entry or {}).get("reply_expected", False))


def reply_type_of(code: str) -> str:
    """类型码 → 回发用的类型码；缺省（未命中/缺键/空串）返回 `""`＝不回发。"""
    entry = entry_of(code)
    value = (entry or {}).get("reply_type")
    return str(value) if value else ""


def build_header(msg_id: str, type_code: str, from_card: str, reply_to: str = "") -> str:
    """拼结构化头行（`reply_to` 空串时落 `reply_to=`，与 `parse_header` 互逆）。"""
    return f"[[card-msg id={msg_id} type={type_code} from={from_card} reply_to={reply_to}]]"


def parse_header(content: str) -> dict[str, str] | None:
    """解析块正文首行的结构化头；不匹配/形态非法返 `None`，**不抛异常**。

    返回 `{"id","type","from","reply_to"}`（`reply_to` 缺键/空值归一为 `""`）。
    判失败的情形：首行不是头行形态／键名非 ASCII 小写下划线／值域越界（如中文）／
    裸 token（无 `=`）／重复键／必需键（`id`/`type`/`from`）缺失或为空。
    """
    if not isinstance(content, str) or not content:
        return None
    first_line = content.split("\n", 1)[0]
    m = _HEADER_RE.match(first_line)
    if not m:
        return None
    fields: dict[str, str] = {}
    for token in m.group(1).split():
        key, sep, value = token.partition("=")
        if not sep or not _KEY_RE.match(key):
            return None
        if value and not _VALUE_RE.match(value):
            return None
        if key in fields:
            return None
        fields[key] = value
    for required in ("id", "type", "from"):
        if not fields.get(required):
            return None
    return {
        "id": fields["id"],
        "type": fields["type"],
        "from": fields["from"],
        "reply_to": fields.get("reply_to", ""),
    }


def strip_header(content: str) -> str:
    """剥去首行头行（含其后的换行符）；无头行时原样返回。"""
    if not isinstance(content, str) or not content:
        return content if isinstance(content, str) else ""
    first_line, sep, rest = content.partition("\n")
    if _HEADER_RE.match(first_line):
        return rest
    return content


def make_msg_id(from_card: str) -> str:
    """`{from_card}-{uuid4().hex[:8]}`——唯一性靠 uuid、可读性靠前缀。"""
    return f"{from_card}-{uuid4().hex[:8]}"


def find_pending(blocks: list[dict]) -> dict | None:
    """待处理推导：返回**最后一条** `actor` 以 `card:` 开头的块之后**没有 assistant 块**
    时的那条卡间块；否则 `None`（从尾向前扫，先见 assistant 即非 pending）。
    """
    for block in reversed(blocks or []):
        actor = str(block.get("actor") or "")
        if actor.startswith("card:"):
            return block
        if actor == "assistant":
            return None
    return None


# ---------------------------------------------------------------------------
# 调起意图登记（迭代四 K2 写入 / K3 消费）。
# 键＝(work_id, target_card_id)，值＝本回合记下的 msg_id 列表。
# 生命周期＝进程内、请求级收集：`schedule_card_message_triggers` 读完即 pop 清键
# （不累积）。丢失不影响持久保证——信已在收件方档案，随时可由档案推导
# （find_pending）。单进程 asyncio 下 setdefault/pop 为原子操作，无锁需求；
# 若将来多进程部署，此处需改集中式（卡间册 §4.3 登记的已知边界）。
TRIGGERS: dict[tuple[str, str], list[str]] = {}

# 已消费去重（迭代四 K4，端点侧打点用）：同一 msg_id 只让调用方打一次
# `card_msg_consumed`。纯进程内存、无 IO；重启清零仅致重打一次（ops 是
# append-only 诊断流，可接受）——不为它新增任何持久存储（K4 约束）。
_CONSUMED_SEEN: set[str] = set()


def mark_consumed(msg_id: str) -> bool:
    """首次标记该 msg_id 已消费 → True（调用方应打 ops）；重复 → False。"""
    if msg_id in _CONSUMED_SEEN:
        return False
    _CONSUMED_SEEN.add(msg_id)
    return True
