"""统一 JSON 解析工具：绝不崩溃，坏输入兜底。

集中存放 ``strip_code_fences`` / ``extract_first_json`` / ``parse_json_array``，
供 base / plan_pipeline / condenser_pipeline 等各处复用，消除重复定义。

设计原则：
- 任何坏输入都返回兜底值（None / [] / ""），绝不抛异常；
- 解析失败降级而非崩溃，保证流水线不被单个坏 JSON 阻塞。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional


def strip_code_fences(text: str) -> str:
    """移除 ```json ... ``` 等代码围栏。"""
    if not text:
        return ""
    cleaned = re.sub(r"```[a-zA-Z]*\s*\n", "", text)
    cleaned = cleaned.replace("```", "")
    return cleaned.strip()


def extract_first_json(text: str) -> Optional[dict[str, Any]]:
    """从文本中提取首个完整 JSON 对象（定位第一个 { 到匹配 }）。失败返回 None。"""
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except Exception:  # noqa: BLE001
                    return None
    return None


def parse_json_array(text: str) -> list[Any]:
    """从文本中提取首个完整 JSON 数组（定位第一个 [ 到匹配 ]）。失败返回 []。"""
    if not text:
        return []
    cleaned = strip_code_fences(text)
    start = cleaned.find("[")
    if start == -1:
        return []
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                candidate = cleaned[start : i + 1]
                try:
                    result = json.loads(candidate)
                except Exception:  # noqa: BLE001
                    return []
                return result if isinstance(result, list) else []
    return []


__all__ = ["strip_code_fences", "extract_first_json", "parse_json_array"]
