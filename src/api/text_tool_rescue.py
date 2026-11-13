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
# 形态D：根元素即工具名的 XML 伪调用（2026-11-11 card_061 实录——模型把
#   调用写成 <read_card><card_id>…</card_id></read_card> 这种裸块，常跟在
#   </think> 泄漏后面）。
#   判别唯一依据：根标签名 ∈ TOOL_REGISTRY——注册表是权威名单，不在册的
#   标签不是调用（正文中出现 <计划>/<思考> 之类不会被误收）。
#   body 里须含 ≥1 个 <k>v</k> 参数对、或是空壳（<x></x> / <x/>）才算完整
#   调用；夹叙述正文的 body 不算——防「讨论格式」的正文被当调用剥走。
#   </?think> 是 DeepSeek 系思维标签泄漏，纯家具：剥掉，但**不**计入
#   suspect（它不是调用标记，不该触发「未识别调用」反馈）。
# ---------------------------------------------------------------------------
_THINK_TAG = re.compile(r"</?\s*think\s*>|</?\s*antml:thinking\s*>",
                        re.IGNORECASE)
_D_CACHE: dict = {}


def _d_names() -> frozenset:
    """注册表工具名集合（延迟导入破环；空集＝注册表读不到）。"""
    if "names" in _D_CACHE:
        return _D_CACHE["names"]
    names: frozenset = frozenset()
    try:
        from . import card_tools  # 延迟导入：模块级 import 会撞环
        names = frozenset(card_tools.TOOL_REGISTRY)
    except Exception:  # noqa: BLE001
        pass
    _D_CACHE["names"] = names
    return names


def _d_patterns():
    """(call_re, selfclose_re, suspect_re)，按注册表现建现缓存。

    注册表读不到 ⇒ 三个 None（本形态关闭，不阻断其他形态）。
    """
    if "call" in _D_CACHE:
        return _D_CACHE["call"], _D_CACHE["self"], _D_CACHE["suspect"]
    call = self = suspect = None
    try:
        names = sorted(_d_names(), key=len, reverse=True)
        if names:
            alt = "|".join(re.escape(n) for n in names)
            call = re.compile(
                rf"<(?P<name>{alt})(?:\s[^>]*)?>"
                rf"(?P<body>.*?)"
                rf"</\s*(?P=name)\s*>",
                re.IGNORECASE | re.DOTALL)
            self = re.compile(rf"<(?P<name>{alt})\s*/>", re.IGNORECASE)
            suspect = re.compile(rf"</?(?:{alt})\b", re.IGNORECASE)
    except Exception:  # noqa: BLE001
        call = self = suspect = None
    _D_CACHE["call"], _D_CACHE["self"], _D_CACHE["suspect"] = call, self, suspect
    return call, self, suspect


# ---------------------------------------------------------------------------
# 兜底网（2026-11-11 · <confirm> 实录）：伪调用格式空间不可枚举——模型
# 还能自造「意图标签」：<confirm>先确认目录里有什么文件</confirm> 这种
# 块里连工具名/参数都没有，任何方言解析器都无 call 可救。兜底规则只判
# 机械事实：**成对**出现的未知标签（非工具名、非良性标签）＝疑似未执行，
# 由调用方回注「未识别、未执行」——不开第二个等待黑洞。
#   · 只看成对标签：正文里单个 <div> 提及不算（讨论 HTML 是 UI 卡日常）；
#   · 含 '-' 的标签不算：连字符是 Web Components 自定义元素约定，UI 卡
#     正文里成对出现自定义元素是合法的；
#   · 良性标签清单＝HTML/排版全集，模型正文里成对写它们是正常排版。
# ---------------------------------------------------------------------------
_E_PAIR = re.compile(
    r"<([A-Za-z][A-Za-z0-9_.]{2,})\b[^>]*>(.*?)</\s*\1\s*>",
    re.IGNORECASE | re.DOTALL)
_E_BENIGN = frozenset("""
    html head body title meta link script style base
    div span p br hr wbr
    h1 h2 h3 h4 h5 h6
    ul ol li dl dt dd
    table thead tbody tfoot tr td th caption colgroup col
    form input button select option optgroup label textarea fieldset legend datalist output
    a img picture source figure figcaption video audio track map area
    code pre em strong b i u s small sub sup mark abbr cite q blockquote kbd samp var time data
    section article header footer main nav aside address
    details summary dialog template slot
    iframe embed object param
    svg path circle rect line polyline polygon ellipse g defs use text tspan
    math mi mn mo ms mrow msup msub mfrac msqrt
    ruby rt rp bdi bdo
    """.split())


def _e_unknown_pair(content: str, tool_names: frozenset) -> bool:
    """正文里有无成对未知标签（既非工具名、也非良性 HTML/排版标签）。"""
    for m in _E_PAIR.finditer(content or ""):
        tag = m.group(1).lower()
        if tag in _E_BENIGN or tag in tool_names:
            continue
        if ":" in tag or "-" in tag:
            continue          # 命名空间形态 A/C 已判；连字符＝自定义元素约定
        return True
    return False


def _d_params(body: str) -> dict:
    """形态D参数：命名 <ns:parameter name=k> 优先，裸元素 <k>v</k> 兜底。"""
    args: dict = {}
    named_spans: list[tuple[int, int]] = []
    for pm in _C_PARAM_NAMED.finditer(body):
        val = pm.group("val").strip()
        try:
            args[pm.group("key").strip()] = json.loads(val)
        except Exception:  # noqa: BLE001
            args[pm.group("key").strip()] = val
        named_spans.append(pm.span())
    if named_spans:
        mask = [True] * len(body)
        for s, e in named_spans:
            for i in range(s, e):
                mask[i] = False
        rest = "".join(ch for i, ch in enumerate(body) if mask[i])
    else:
        rest = body
    for bm in _C_PARAM_BARE.finditer(rest):
        val = bm.group("val").strip()
        try:
            args[bm.group("key").strip()] = json.loads(val)
        except Exception:  # noqa: BLE001
            args[bm.group("key").strip()] = val
    return args


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

    # ---- 形态D：根元素即工具名（<read_card><card_id>…</read_card>）----
    _d_call, _d_self, _d_sus = _d_patterns()
    if _d_call is not None:
        for m in _d_call.finditer(content):
            if any(m.start() < e and m.end() > s for s, e in spans):
                continue          # 与已收编的调用块重叠 ⇒ 不二次吞
            body = m.group("body") or ""
            args = _d_params(body)
            # 空壳 ⇒ 零参数调用（全可选工具合法，缺参走标准链报 validation，
            # 也是闭环反馈）；夹叙述的 body 零参数对 ⇒ 当正文跳过、不剥。
            if body.strip() and not args:
                continue
            calls.append({"name": (m.group("name") or "").strip(),
                          "arguments": args, "_origin": "textD"})
            spans.append(m.span())
        if _d_self is not None:
            for m in _d_self.finditer(content):
                if any(m.start() < e and m.end() > s for s, e in spans):
                    continue
                calls.append({"name": (m.group("name") or "").strip(),
                              "arguments": {}, "_origin": "textD"})
                spans.append(m.span())

    # ---- 思维标签家具：泄漏的 </think>/<antml:thinking> 纯噪声，剥 ----
    for tm in _THINK_TAG.finditer(content):
        spans.append(tm.span())

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
    unresolved = (not calls) and (
        bool(_SUSPECT.search(content))
        or (_d_sus is not None and bool(_d_sus.search(content)))
        or _e_unknown_pair(content, _d_names()))

    return calls, unresolved, stripped
