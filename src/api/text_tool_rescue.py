# -*- coding: utf-8 -*-
"""文本形态工具调用抢救器（2026-10-02 等待黑洞修复）

背景（三次同族发作定案）：
统合官 deepseek-v4.1-flash 批量发多个工具调用时，会把调用以 DeepSeek
原生标记的文本形态写进 content（全角双竖线 ｜｜ 是特殊 token 的可打印
近似），而非 API 标准 tool_calls 字段。routes.py 原逻辑只认标准字段 →
正文里的调用零执行、零反馈 → 模型永远在等一个不会来的结果（等待黑洞）。

本模块只做机械模式匹配，不判内容、不拦产出、不引导方向（无闸原则）：
1. extract_text_tool_calls(content) —— 从正文中解析文本形态调用
   形态A：DeepSeek 原生标记（全角/半角竖线变体，单条或多条 invoke）
   形态B：标准围栏 JSON 数组 [{"name":..., "arguments":{...}}]
2. strip_text_tool_calls(content) —— 把已解析出的调用文本从正文剥离
3. 若匹配到标记但解析不成完整调用 → unresolved=True，由调用方
   把「未识别为调用、未执行」这个机械事实回注模型上下文（反馈兜底，
   关闭等待黑洞——模型不再傻等结果）。
"""
import json
import re

# ---------------------------------------------------------------------------
# 形态A：DeepSeek 原生标记泄漏
#   <｜tool_calls｜> / <｜｜tool_calls｜｜> 开头（竖线 1-2 条、全半角）
#   <｜invoke name="X"｜> <｜parameter name="k"｜>v<｜/parameter｜> ... <｜/invoke｜>
#   闭合变体：竖线顺序在前或斜杠在前都接住（泄漏形态不可穷举，宽匹配）
# ---------------------------------------------------------------------------
_PIPE = r"[|｜]{1,2}"
_A_BLOCK_OPEN = re.compile(
    rf"<{_PIPE}\s*tool_calls\s*{_PIPE}?>", re.IGNORECASE)
_A_INVOKE = re.compile(
    rf"<{_PIPE}\s*invoke\s+name=\"(?P<name>[^\"]+)\"\s*{_PIPE}?>"
    rf"(?P<body>.*?)"
    rf"<{_PIPE}\s*/?\s*invoke\s*{_PIPE}?>",
    re.IGNORECASE | re.DOTALL)
_A_PARAM = re.compile(
    rf"<{_PIPE}\s*parameter\s+name=\"(?P<key>[^\"]+)\"\s*{_PIPE}?>"
    rf"(?P<val>.*?)"
    rf"<{_PIPE}\s*/?\s*parameter\s*{_PIPE}?>",
    re.IGNORECASE | re.DOTALL)

# ---------------------------------------------------------------------------
# 形态B：标准围栏 JSON（模型有时把调用写成 ```json [{name, arguments}]）
# ---------------------------------------------------------------------------
_B_BLOCK = re.compile(
    r"```(?:json|tool[_ ]?calls)?\s*(\[\s*\{.*?\}\s*\])\s*```",
    re.DOTALL)

# ---------------------------------------------------------------------------
# 疑似标记探测（反馈兜底用）：正文里出现了泄漏标记的痕迹但没能解析出
# 完整调用 → 调用方应把「未识别、未执行」这个事实回注模型
# ---------------------------------------------------------------------------
_SUSPECT = re.compile(
    rf"<{_PIPE}\s*(?:tool_calls|invoke|parameter)", re.IGNORECASE)


def _parse_invoke_params(body: str) -> dict:
    args: dict = {}
    for m in _A_PARAM.finditer(body):
        key = m.group("key").strip()
        val = m.group("val").strip()
        # 参数值尝试 JSON 还原（数字/布尔/嵌套）；失败则保持字符串
        try:
            args[key] = json.loads(val)
        except Exception:  # noqa: BLE001
            args[key] = val
    return args


def extract_text_tool_calls(content: str) -> tuple[list[dict], bool, str]:
    """解析正文里的文本形态工具调用。

    返回 (calls, unresolved, stripped_content)：
    - calls: [{name, arguments, _origin: "textA"|"textB"}]
    - unresolved: 正文有疑似标记但没解析出任何完整调用（反馈兜底信号）
    - stripped_content: 把已解析的调用块剥离后的正文
    """
    if not content:
        return [], False, content or ""

    calls: list[dict] = []
    spans: list[tuple[int, int]] = []

    # ---- 形态A：原生标记 ----
    if _A_BLOCK_OPEN.search(content) or _A_INVOKE.search(content):
        for m in _A_INVOKE.finditer(content):
            name = (m.group("name") or "").strip()
            if not name:
                continue
            calls.append({
                "name": name,
                "arguments": _parse_invoke_params(m.group("body")),
                "_origin": "textA",
            })
            spans.append(m.span())
        # 整段 tool_calls 外壳（开标记到文本中最后一个 invoke 结束）一并剥
        open_m = _A_BLOCK_OPEN.search(content)
        if open_m and spans:
            last_end = max(e for _, e in spans)
            spans.append((open_m.start(), min(last_end, len(content))))
        # 外壳可能还带闭合尾标记
        for tail in re.finditer(
                rf"<{_PIPE}\s*/?\s*tool_calls\s*{_PIPE}?>", content, re.IGNORECASE):
            spans.append(tail.span())

    # ---- 形态B：围栏 JSON ----
    for m in _B_BLOCK.finditer(content):
        raw = m.group(1)
        try:
            arr = json.loads(raw)
        except Exception:  # noqa: BLE001
            # 逐候选重试：字符串内裸换行修复（同建卡解析器先例）
            fixed = raw.replace("\n", "\\n")
            try:
                arr = json.loads(fixed)
            except Exception:  # noqa: BLE001
                continue
        if not isinstance(arr, list):
            continue
        ok_items = [it for it in arr
                    if isinstance(it, dict) and it.get("name")]
        if not ok_items:
            continue
        for it in ok_items:
            args = it.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:  # noqa: BLE001
                    args = {"_raw": args}
            calls.append({"name": it["name"], "arguments": args,
                          "_origin": "textB"})
        spans.append(m.span())

    # ---- 剥离 ----
    stripped = content
    if spans:
        keep = [True] * len(content)
        for s, e in spans:
            for i in range(s, min(e, len(content))):
                keep[i] = False
        stripped = "".join(ch for i, ch in enumerate(content) if keep[i])
        stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()

    # ---- 疑似标记但零解析（反馈兜底信号）----
    unresolved = bool(_SUSPECT.search(stripped)) and not calls

    return calls, unresolved, stripped


def feedback_note() -> str:
    """生成回注模型的机械事实句（不含任何方向性引导）。

    调用方已用 unresolved 守卫；本函数无条件返回反馈句。
    （修复记录：初版带 n_unresolved=0 默认参数，0 时返回空串，
    回归测试抓到该 bug——反馈兜底将永不触发，等于没修。）
    """
    return ("[系统提示] 你上一轮正文中出现了工具调用样式的标记，"
            "但未被系统识别为可执行调用，未执行。"
            "如需执行工具调用，请使用标准函数调用格式重发。")
