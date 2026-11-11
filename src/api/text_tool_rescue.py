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
#   形态A1：带竖线（全角/半角 1-2 条）
#     <｜tool_calls｜> / <｜｜tool_calls｜｜>
#     <｜invoke name="X"｜> <｜parameter name="k"｜>v<｜/parameter｜>
#   形态A2：纯 ASCII 无竖线（2026-10-03 07:42 实录第四次泄漏）
#     <tool_calls> <invoke name="X"> <parameter name="k">v</parameter>
#   → 竖线统一按 0-2 条匹配（{0,2}），两种形态一套正则接住
#   闭合变体：竖线顺序在前或斜杠在前都接住（泄漏形态不可穷举，宽匹配）
# ---------------------------------------------------------------------------
_PIPE = r"[|｜]{0,2}"
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
# 形态C：antml/Devin 系 XML 元素伪调用（deepseek-v4.1-flash-eco 反复发作，
#   card_061 实录 80+ 次）——调用块整个写进正文。两变体一套正则：
#   ① antml 命名空间（模型自述的"真格式"）：
#      <antml:function_calls><antml:invoke name="X">
#        <antml:parameter name="k">v</antml:parameter></antml:invoke>
#   ② 中文壳变体（实录）：<执行><function_calls>
#        <function name="X"><path>v</path><limit>5</limit></function>
#   参数两形态：<ns:parameter name="k">v</ns:parameter> 或 裸元素 <k>v</k>。
#   工具名与注册表同面（list_files/search_files/read_file 皆真名），
#   名不符自然走标准链的未知工具路径，这里不做映射。
# ---------------------------------------------------------------------------
_NS = r"(?:[A-Za-z_][\w.-]*:)?"
_C_CALL = re.compile(
    rf"<{_NS}(?:invoke|function)\s+name=\"(?P<name>[^\"]+)\"\s*>"
    rf"(?P<body>.*?)"
    rf"</\s*{_NS}(?:invoke|function)\s*>",
    re.IGNORECASE | re.DOTALL)
_C_PARAM_NAMED = re.compile(
    rf"<{_NS}parameter\s+name=\"(?P<key>[^\"]+)\"\s*>"
    rf"(?P<val>.*?)"
    rf"</\s*{_NS}parameter\s*>",
    re.IGNORECASE | re.DOTALL)
_C_PARAM_BARE = re.compile(
    r"<(?P<key>[A-Za-z_][\w.-]*)>"
    rf"(?P<val>.*?)"
    rf"</\s*(?P=key)\s*>",
    re.DOTALL)
_C_WRAPPER = re.compile(
    rf"</?\s*{_NS}function_calls\s*>|</?执行\s*>",
    re.IGNORECASE)

# ---------------------------------------------------------------------------
# 疑似标记探测（反馈兜底用）：正文里出现了泄漏标记的痕迹但没能解析出
# 完整调用 → 调用方应把「未识别、未执行」这个事实回注模型
# [形态C 2026-11-09] 覆盖面同步放宽：antml:invoke/function_calls/执行 壳
# 也计入疑似（解析不出完整调用时照样喂反馈，不开第二个黑洞）。
# ---------------------------------------------------------------------------
_SUSPECT = re.compile(
    rf"<{_PIPE}\s*(?:{_NS})?(?:tool_calls|function_calls|invoke|function|parameter|执行)",
    re.IGNORECASE)


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

    # ---- 形态C：antml/function_calls XML 元素伪调用 ----
    for m in _C_CALL.finditer(content):
        name = (m.group("name") or "").strip()
        if not name:
            continue
        body = m.group("body") or ""
        args: dict = {}
        named_spans: list[tuple[int, int]] = []
        for pm in _C_PARAM_NAMED.finditer(body):
            key = pm.group("key").strip()
            val = pm.group("val").strip()
            try:
                args[key] = json.loads(val)
            except Exception:  # noqa: BLE001
                args[key] = val
            named_spans.append(pm.span())
        # 裸元素参数（<k>v</k>）：先遮罩 named 参数占区，防止
        # <ns:parameter> 被裸元素正则二次吞入。
        if named_spans:
            mask = [True] * len(body)
            for s, e in named_spans:
                for i in range(s, e):
                    mask[i] = False
            rest = "".join(ch for i, ch in enumerate(body) if mask[i])
        else:
            rest = body
        for bm in _C_PARAM_BARE.finditer(rest):
            key = bm.group("key").strip()
            val = bm.group("val").strip()
            try:
                args[key] = json.loads(val)
            except Exception:  # noqa: BLE001
                args[key] = val
        calls.append({"name": name, "arguments": args, "_origin": "textC"})
        spans.append(m.span())
    # 外壳家具一并剥：function_calls 开闭壳、<执行></执行> 壳。
    # <计划> 是模型写的正文内容，不是调用家具——留。
    for wm in _C_WRAPPER.finditer(content):
        spans.append(wm.span())

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
    # 在剥离前的原文上探：calls 为空时 stripped 里外壳标记可能已被
    # _C_WRAPPER 剥光 ⇒ 漏报 ⇒ 等待黑洞重开。零调用时查原文更严。
    unresolved = (not calls) and bool(_SUSPECT.search(content))

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


# ---------------------------------------------------------------------------
# [断言检出 · harness 化 2026-11-10] 断言词表＋断言点名实体（card_NNN／绝对
#   路径）提取——**只产机械事实**：断言词有哪些、点名对象在不在本回合
#   调用参数串里出现过。判据与处置全部挪进 harness_policies（旧版在同一
#   钩位强制续轮，过于敏感被用户拆掉；这版只供事件发射面取用）。
# ---------------------------------------------------------------------------
#: 断言词：声称「已做核实动作」的完成式表述（「我要核实」不算，「核实过」算）。
_CLAIM_RE = re.compile(
    r"已核实|核实过|实测|实读|已确认|确认过|核对过|核查过|验证过|"
    r"检查过|查证过|看过了|读过了|对照过|比对过|点开过|打开过|"
    r"跑过了|试过了|盘[上中]")
#: 点名实体：卡号＋绝对盘符路径（相对路径/文件名不取——经由目录列举核实
#: 会误报；卡号与绝对路径在调用参数里基本逐字出现，精度优先）。
_ENT_CARD = re.compile(r"\bcard_\d{2,}\b")
_ENT_PATH = re.compile(
    r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"'“”‘’，。；：、（）【】《》<>\[\]]+")


def _norm_entity(s: str) -> str:
    """路径归一：斜杠方向/重复斜杠压平、小写——参数里 D:\\x 与正文 D:/x 同面。"""
    return re.sub(r"[\\/]+", "/", s).lower()


def audit_claims(content: str, args_text: str,
                 turn_tool_calls: int) -> tuple[list[str], list[str]]:
    """断言检出。返回 (断言词命中表, 未被调用参数触碰的实体表)。

    - tokens：正文里命中的完成式断言词（命中即返，供发射面判"要不要发事实"）；
    - strays：断言点名的实体不在本回合调用参数串里的清单（零调用时为空表——
      "零回执"本身由策略拿 tool_calls 判，这里不重复表达）。
    """
    tokens = sorted(set(_CLAIM_RE.findall(content or "")))
    if not tokens:
        return [], []
    if turn_tool_calls <= 0:
        return tokens, []
    args_norm = _norm_entity(args_text or "")
    seen: set[str] = set()
    strays: list[str] = []
    for ent in (_ENT_CARD.findall(content)
                + _ENT_PATH.findall(content)):
        ent = ent.rstrip(".,;:!?\"'，。；：")
        if len(ent) < 4 or ent in seen:
            continue
        seen.add(ent)
        if _norm_entity(ent) not in args_norm:
            strays.append(ent)
        if len(strays) >= 6:
            break
    return tokens, strays
