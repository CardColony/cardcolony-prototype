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
    rf"<{_PIPE}\s*(?:{_NS})?(?:tool_calls?|function_calls|invoke|function|parameter|执行"
    rf"|函数调用|函數調用|関数呼び出し|调用|工具名|ツール名|参数|パラメータ)",
    re.IGNORECASE)


# ---------------------------------------------------------------------------
# 形态E：DSML（DeepSeek V4.1 公开语法，｜｜DSML｜｜ 竖线外壳）——
# card_582 09:56 实录：`<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="X">
# <｜｜DSML｜｜ parameter name="k" string="true">v</｜｜DSML｜｜ parameter>
# </｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>`；单/双竖线、开闭标签内空白
# 均有现场变体（</｜｜DSML｜｜invoke> 无空格、</｜DSML｜ parameter> 单竖线）。
# [按路由启用 §四] 只在 models.yaml 声明 text_protocols 含 dsml 时解析
# （protocols 参数）；未声明路由的 DSML 壳只走疑似标记/隔离，不出调用。
# ---------------------------------------------------------------------------
_DSML = r"[｜|]+\s*DSML\s*[｜|]+"
_F_DSML_BLOCK = re.compile(
    rf"<\s*{_DSML}\s*(?P<tag>calls|tool_calls)\s*>(?P<body>.*?)"
    rf"<\s*/\s*{_DSML}\s*(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL)
_F_DSML = re.compile(
    rf"<\s*{_DSML}\s*invoke\s+name=\"(?P<name>[^\"]+)\"\s*>"
    rf"(?P<body>.*?)"
    rf"<\s*/\s*{_DSML}\s*invoke\s*>",
    re.IGNORECASE | re.DOTALL)
_F_DSML_PARAM = re.compile(
    rf"<\s*{_DSML}\s*parameter\s+name=\"(?P<key>[^\"]+)\""
    rf"(?P<attrs>[^>]*)>"
    rf"(?P<val>.*?)"
    rf"<\s*/\s*{_DSML}\s*parameter\s*>",
    re.IGNORECASE | re.DOTALL)

# ---------------------------------------------------------------------------
# 形态F：<call><name>X</name><parameter name="k">v</parameter></call>
# （§四 用户补充样本）——一块只读一个工具名；工具名缺失/重复、参数名
# 重复、外壳不闭合均不产生调用（失败事实由疑似标记/家具行兜底记录）。
# ---------------------------------------------------------------------------
_F_CALL = re.compile(
    r"<\s*call\s*>(?P<body>.*?)<\s*/\s*call\s*>",
    re.IGNORECASE | re.DOTALL)
_F_CALL_TAG = re.compile(r"<\s*/?\s*call\b", re.IGNORECASE)
_F_CALL_NAME = re.compile(
    r"<\s*name\s*>(?P<v>.*?)<\s*/\s*name\s*>", re.IGNORECASE | re.DOTALL)
_F_CALL_PARAM = re.compile(
    r"<\s*parameter\s+name=\"(?P<key>[^\"]+)\"\s*>"
    rf"(?P<val>.*?)<\s*/\s*parameter\s*>",
    re.IGNORECASE | re.DOTALL)

# ---------------------------------------------------------------------------
# 形态G：中日文标签伪调用（card_049 实录 2026-11-20，13:27/13:30/13:39 末跳）
#   模型把 DeepSeek 系标签译成中日文再写一遍：
#     <函数调用><工具名>X</工具名><参数>{json}</参数></函数调用>      （中文）
#     <関数呼び出し><ツール名>X</ツール名><パラメータ>{json}</パラメータ>…（日文）
#     <调用><工具名>report_up</工具名><参数>{json}</工具名></function_calls>
#   开闭错配是常态（参数拿 </工具名> 收尾、外壳拿 </function_calls> 收尾）。
#   非精确形态只识别、只清理：整段先收编
#   进剥离区（壳内夹带的 invoke/call 元素随之出执行链），错配闭标签照收
#   ——它们就是异常的一部分；「形似调用但未派发」由 unresolved 反馈兜底。
#   方括号声明行「[工具调用：X，…]」不是块，走家具行/疑似标记。
# ---------------------------------------------------------------------------
_G_WRAPPER = r"函数调用|函數調用|関数呼び出し|调用"
_G_CLOSE = r"函数调用|函數調用|関数呼び出し|调用|function_calls|function_results"
_G_OPEN = re.compile(rf"<\s*(?:{_G_WRAPPER})\s*>")
_G_CLOSE_RE = re.compile(rf"<\s*/\s*(?:{_G_CLOSE})\s*>")
_G_BRACKET_DECL = re.compile(
    r"^\s*[\[［【〔]\s*(?:工具调用|ツール呼び出し|関数呼び出し)[：:]",
    re.MULTILINE)
_G_CALL_OPEN = r"<\s*调用\s*>(?=\s*<\s*工具名\s*>)"
_G_CALL_HEADER = re.compile(
    r"\s*<\s*工具名\s*>\s*(?P<name>[A-Za-z_]\w*)\s*<\s*/\s*工具名\s*>"
    r"\s*<\s*参数\s*>\s*")
_G_CALL_END = re.compile(r"\s*<\s*/\s*参数\s*>\s*<\s*/\s*调用\s*>")

# 形态H：<invoke><tool>X</tool><param name="k">v</param></invoke>。
# 裸 invoke 开标签与形态 A/C 的 <invoke name="X"> 分开。
_H_INVOKE_OPEN = r"<\s*invoke\s*>"
_H_INVOKE_NAME = re.compile(
    r"<\s*tool\s*>\s*(?P<v>[A-Za-z_]\w*)\s*<\s*/\s*tool\s*>", re.IGNORECASE)
# 形态I：function_calls 外壳以 tool_name 开始才进入调用解析。
_I_FUNCTION_OPEN = r"<\s*function_calls\s*>(?=\s*<\s*tool_name\s*>)"
_I_FUNCTION_NAME = re.compile(
    r"<\s*tool_name\s*>\s*(?P<v>[A-Za-z_]\w*)\s*<\s*/\s*tool_name\s*>", re.IGNORECASE)


def _xml_parameter(tag: str) -> re.Pattern:
    return re.compile(
        rf"""<\s*{tag}\s+name\s*=\s*(?P<quote>["'])(?P<key>[^"'<>]+)(?P=quote)\s*>"""
        rf"(?P<val>[^<]*)<\s*/\s*{tag}\s*>", re.IGNORECASE)


_XML_FIELDS = {
    "invoke": (_H_INVOKE_NAME, _xml_parameter("param")),
    "function_calls": (_I_FUNCTION_NAME, _xml_parameter("parameter")),
}
_COMPAT_ORIGINS = {
    "cjk_call": "textG_cjk",
    "invoke": "textH_invoke",
    "function_calls": "textI_function_calls",
}
READONLY_PROTOCOL_ORIGINS = frozenset({"textE_dsml", "textF_call", *_COMPAT_ORIGINS.values()})


# ---------------------------------------------------------------------------
# 仿回执（假结果）——模型自写的 <function_results>…</function_results>
# 整段（card_049 实录 8 段假 JSON）与 <function_calls>{json}</function_calls>
# 结果壳：外壳+未经执行器确认的数据退出投影；后者 content 字段是模型
# 报告文字，按普通助手陈述留下。仿系统行只在模型原始正文上判，落存
# 正文还可能有宿主追加的回执行。
# ---------------------------------------------------------------------------
_RESULT_TAG = re.compile(r"<\s*(?P<close>/)?\s*function_results\s*>", re.IGNORECASE)
_JSON_REGION_END = {
    "report": re.compile(r"\s*</\s*function_calls\s*>", re.IGNORECASE),
    "legacy": re.compile(r"\s*</tool_call>", re.IGNORECASE),
}
_REFERENCE = r"```.*?(?:```|\Z)|^[ \t]*>[^\n]*"
_INLINE_REFERENCE = r"`[^`\r\n]+`"
# ---------------------------------------------------------------------------
# [异常正文跨度 2026-11-20 · card_007/card_648 实录] 供方协议残片——不是
# 调用、不是正文，但会触发「未知标签对」兜底把整跳误判成伪调用未派发：
#   · <ds_safety>…</ds_safety> 整段（含内部安全判定正文）——card_007 23:00/
#     23:07 两跳实录，判成 pseudo_call_unparsed 还引来重发提醒；
#   · CDATAEND> 残片——与 ds_safety 区块相邻出现，独立出现同处理；
#   · <｜begin▁of▁sentence｜> 起始标记——card_648 23:11 实录（BOS 泄漏
#     进正文，后面跟着真调用同跳）。
# 判定只用可证明的跨度：块有结构边界（开闭标签），标记是确定 token 本身。
# 围栏/引用里的同形文本仍是引用身份——区表顺序保证先判引用区。
# ---------------------------------------------------------------------------
_ANOMALY_OPEN = r"<\s*ds_safety\s*>"
# 引用里的标签不参与配对。遇到下一个裸开标签时，前块只留下开头残片。
_ANOMALY_EDGE = re.compile(
    rf"{_REFERENCE}|{_INLINE_REFERENCE}"
    r'|"(?:\\[^\r\n]|[^"\\\r\n])*"'
    r"|(?<![A-Za-z0-9_])'(?:\\[^\r\n]|[^'\\\r\n])*'"
    r"|“[^”\r\n]*”|‘[^’\r\n]*’|「[^」\r\n]*」|『[^』\r\n]*』"
    rf"|(?P<open>{_ANOMALY_OPEN})"
    r"|(?P<close></\s*ds_safety\s*>)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE)
_CDATAEND = r"CDATAEND>"
_ANOMALY_LINE_END = r"(?=[ \t]*\r?$)"
_ANOMALY_SUFFIX = re.compile(
    rf"[ \t]*{_CDATAEND}{_ANOMALY_LINE_END}", re.IGNORECASE | re.MULTILINE)
_BOS_TOKEN = r"<[｜|]\s*begin[▁_\s]*of[▁_\s]*sentence\s*[｜|]>"
_CONTEXT_REGION = re.compile(
    rf"(?P<reference>{_REFERENCE})"
    rf"|(?P<inline>{_INLINE_REFERENCE})"
    r"|(?P<result><\s*function_results\s*>)"
    rf"|(?P<function_calls>{_I_FUNCTION_OPEN})"
    r"|(?P<report><\s*function_calls\s*>\s*(?=\{))"
    r"|(?P<reportshell><\s*function_calls\s*>)"
    r"|(?P<legacy><tool_call>\s*(?=\{))"
    rf"|(?P<cjk_call>{_G_CALL_OPEN})"
    rf"|(?P<invoke>{_H_INVOKE_OPEN})"
    rf"|(?P<anomaly_open>^[ \t]*(?P<anomaly_start>(?:{_CDATAEND}[ \t]*)?{_ANOMALY_OPEN}))"
    rf"|(?P<anomaly_cdata>^[ \t]*(?P<cdata_start>{_CDATAEND}){_ANOMALY_LINE_END})"
    rf"|(?P<anomaly_bos>{_BOS_TOKEN})",
    re.IGNORECASE | re.DOTALL | re.MULTILINE)
_XML_EDGES = {
    tag: re.compile(
        rf"{_REFERENCE}|{_INLINE_REFERENCE}"
        rf"|(?P<open><\s*{tag}(?:\s+[^<>]*)?\s*>)"
        rf"|(?P<close><\s*/\s*{tag}\s*>)",
        re.IGNORECASE | re.DOTALL | re.MULTILINE)
    for tag in _XML_FIELDS
}
_Q_SYS_LINE = re.compile(r"^[ \t]*〔\s*(?:系统|工具)[^〕\r\n]*〕", re.MULTILINE)
_JSON_START = re.compile(r"\s*(?=[{\[])")
_JSON_DECODER = json.JSONDecoder()
_MASKED_CHAR = "\x00"


def _xml_call_region(content: str, start: int, tag: str) -> tuple[int, str | None]:
    depth = 1
    for edge in _XML_EDGES[tag].finditer(content, start):
        if edge.group("open") and not edge[0].endswith("/>"):
            depth += 1
        elif edge.group("close"):
            depth -= 1
            if depth == 0:
                return edge.end(), content[start:edge.start()]
    return len(content), None


def _cjk_call_region(content: str, start: int) -> tuple[int, dict | None]:
    header = _G_CALL_HEADER.match(content, start)
    if header:
        try:
            args, start = _JSON_DECODER.raw_decode(content, header.end())
        except (ValueError, RecursionError):
            pass
        else:
            end = _G_CALL_END.match(content, start)
            if end and isinstance(args, dict):
                return end.end(), {"name": header["name"], "arguments": args}
    close = _G_CLOSE_RE.search(content, start)
    return (close.end() if close else len(content)), None


def _context_regions(content: str, *, scan_anomalies: bool = True):
    """按原文位置枚举引用区、结果区、报告壳和需按整段处理的调用块。"""
    cursor = 0
    while match := _CONTEXT_REGION.search(content, cursor):
        kind = match.lastgroup
        start = match.start()
        end = match.end()
        payload = None
        if not scan_anomalies and kind.startswith("anomaly_"):
            cursor = end
            continue
        if kind == "result":
            # JSON 值中可能含字面闭合标签，优先从完整数据之后找外壳结尾。
            value_start = _JSON_START.match(content, end)
            if value_start:
                try:
                    _, end = _JSON_DECODER.raw_decode(content, value_start.end())
                except (ValueError, RecursionError):
                    pass
            depth = 1
            for tag in _RESULT_TAG.finditer(content, end):
                depth += -1 if tag.group("close") else 1
                if not depth:
                    end = tag.end()
                    break
            else:
                end = len(content)
        elif kind == "reportshell":
            # <function_calls> 非 JSON 内容：报告文字或结果字段的混合壳——
            # 方案判据「只有报告内容或结果字段时按结果壳处理」。壳内文字作
            # content 传入（剥壳留报告），零派发随 result/report 同口径。
            close = _JSON_REGION_END["report"].search(content, end)
            inner_end = close.start() if close else len(content)
            inner = content[end:inner_end].strip()
            end = close.end() if close else len(content)
            if re.search(r"<\s*(?:[A-Za-z_][\w.-]*:)?(?:invoke|function)\s+name=",
                         inner, re.IGNORECASE):
                # 壳里装的是调用元素 ⇒ 不是结果壳，交回形态 C 处理——
                # 不产出 region（内部调用照常可解析、可遮蔽判定）。
                cursor = match.end()
                continue
            if inner and not re.search(
                    r"<\s*(?:result|output|return)\b|\"(?:ok|result|output|status)\"\s*:",
                    inner, re.IGNORECASE):
                # 结果字段（<result>…/"ok":…）⇒ 整壳丢弃；纯报告文字 ⇒ 剥壳留文
                payload = {"content": inner}
        elif kind in _JSON_REGION_END:
            # JSON 字符串里的闭合标签、引用和结果示例仍属于该 JSON 值。
            closing = _JSON_REGION_END[kind]
            try:
                value, after = _JSON_DECODER.raw_decode(content, end)
            except (ValueError, RecursionError):
                close = closing.search(content, end) if kind == "legacy" else None
                end = close.end() if close else len(content)
            else:
                close = closing.match(content, after)
                end = close.end() if close else len(content)
                if close and isinstance(value, dict):
                    payload = value
        elif kind in _XML_FIELDS:
            end, payload = _xml_call_region(content, end, kind)
        elif kind == "cjk_call":
            end, payload = _cjk_call_region(content, end)
        elif kind == "anomaly_open":
            # 行首可先有 CDATAEND>。闭合确认前，跨度只覆盖起始残片。
            start = match.start("anomaly_start")
            for edge in _ANOMALY_EDGE.finditer(content, end):
                if edge["open"]:
                    break
                if edge["close"]:
                    end = edge.end()
                    if suffix := _ANOMALY_SUFFIX.match(content, end):
                        end = suffix.end()
                    break
            kind = "anomaly"
        elif kind == "anomaly_cdata":
            start = match.start("cdata_start")
            kind = "anomaly"
        elif kind == "anomaly_bos":
            # BOS 起始标记可以紧贴正文，648 的实录就是这种形态。
            line_head = content.rfind("\n", 0, start) + 1
            line_tail = content.find("\n", end)
            if line_tail < 0:
                line_tail = len(content)
            if content[line_head:start].strip() and content[end:line_tail].strip():
                cursor = end
                continue
            kind = "anomaly"
        yield start, end, kind, payload
        cursor = end


def detect_pseudo_receipt(content: str, *, include_sys_lines: bool = True) -> int:
    """非围栏区仿回执计数（检测端：围栏内是代码示例，保留引用身份）。

    include_sys_lines：〔系统结果〕/〔工具结果〕行是否计仿写。
    逐跳模型原始正文上判 ⇒ True（模型正文里这些行只能是仿写）；
    已落存会话块上判 ⇒ False——宿主生成的真实系统结果行会随
    clean_text 落进块正文（复现修复：真回执被误标 pseudo_receipt），
    存量分类只信 function_results/结果壳这两类永不合法的硬家具。
    """
    content = content or ""
    n, cursor = 0, 0
    for start, end, kind, _ in _context_regions(content):
        if include_sys_lines:
            n += len(_Q_SYS_LINE.findall(content, cursor, start))
        n += kind in {"result", "report", "reportshell"}
        cursor = end
    if include_sys_lines:
        n += len(_Q_SYS_LINE.findall(content, cursor))
    return n


def parse_legacy_text_tool_calls(content: str) -> list[dict]:
    """读取顶层 `<tool_call>` JSON，工具许可仍由调用者核对。"""
    calls = []
    for _, _, kind, payload in _context_regions(content):
        if kind != "legacy" or payload is None:
            continue
        name = str(payload.get("name") or "").strip()
        if name:
            args = payload.get("arguments")
            calls.append({"name": name, "arguments": args if isinstance(args, dict) else {}})
    return calls


# ---------------------------------------------------------------------------
# 引用区/结果区遮蔽（2026-11-20 复现修复）：围栏代码、<function_results>…
# </function_results> 区内的调用样例只供审计，不参与解析派发、不计疑似。
# 遮蔽保留坐标，标记用于拒绝跨越引用区或结果区的候选块；
# 未闭合区（``` 无收尾 / function_results 无闭合）遮蔽到文末——保守口径。
# ---------------------------------------------------------------------------
def _scan_call_text(content: str) -> tuple[
        str, list[tuple[int, int, str, dict | str | None]],
        list[tuple[int, int]]]:
    """保留原文坐标，整块遮蔽引用、结果、供方残片和由专用解析器处理的调用。

    三元组：(遮蔽文本, 兼容调用区, 异常跨度)。异常跨度（anomaly——
    ds_safety 块/CDATAEND>/begin▁of▁sentence）单列返回：同一跨度
    既做调用识别遮蔽（其内永不出候选），也做出站剥离名单（§二.2）。
    """
    pieces = []
    xml_calls = []
    anomaly_spans = []
    cursor = 0
    for start, end, kind, payload in _context_regions(content):
        pieces.extend((content[cursor:start], _MASKED_CHAR * (end - start)))
        if kind in _COMPAT_ORIGINS:
            xml_calls.append((start, end, kind, payload))
        elif kind == "anomaly":
            anomaly_spans.append((start, end))
        cursor = end
    pieces.append(content[cursor:])
    return "".join(pieces), xml_calls, anomaly_spans


def anomaly_fragments(content: str) -> list[tuple[int, int]]:
    """可证明的供方残片跨度列表（ds_safety 块 / CDATAEND> /
    begin▁of▁sentence 标记）。空＝无——供收口判定「只剩残片」。"""
    _, _, spans = _scan_call_text(content or "")
    return spans


def _unmasked_matches(pattern: re.Pattern, scan: str):
    return (match for match in pattern.finditer(scan) if _MASKED_CHAR not in match[0])


def detect_protocol_hint(content: str) -> str:
    """可识别但未启用的协议族提示（空＝无）——关闭路由兼容时
    只记识别事实，供 anomaly 事件 detail 携带。"""
    scan, xml_calls, _ = _scan_call_text(content or "")
    if xml_calls:
        return xml_calls[0][2]
    if _F_DSML.search(scan) or re.search(_DSML, scan):
        return "dsml"
    if _F_CALL_TAG.search(scan):
        return "call"
    return ""


_DSML_PARAM_TYPE = re.compile(r'\s*(string|number)\s*=\s*"(true|false)"\s*',
                              re.IGNORECASE)


def _dsml_args(body: str) -> dict | None:
    args: dict = {}
    cursor = 0
    for pm in _F_DSML_PARAM.finditer(body):
        if body[cursor:pm.start()].strip():
            return None
        cursor = pm.end()
        key = pm.group("key").strip()
        if not key or key in args:
            return None
        val = pm.group("val")
        attrs = pm.group("attrs").strip()
        declared = _DSML_PARAM_TYPE.fullmatch(attrs) if attrs else None
        if attrs and not declared:
            return None
        kind, flag = (declared.group(1).lower(), declared.group(2).lower()) if declared else ("", "")
        if kind == "string" and flag == "true":
            args[key] = val
            continue
        try:
            value = json.loads(val)
        except (ValueError, RecursionError):
            if declared:
                return None
            value = val.strip()
        if kind == "number" and (flag != "true" or type(value) not in (int, float)):
            return None
        args[key] = value
    return args if not body[cursor:].strip() else None


def _dsml_calls(body: str) -> list[dict]:
    calls: list[dict] = []
    cursor = 0
    for match in _F_DSML.finditer(body):
        if body[cursor:match.start()].strip():
            return []
        name = match.group("name").strip()
        args = _dsml_args(match.group("body"))
        if not name or args is None:
            return []
        calls.append({"name": name, "arguments": args, "_origin": "textE_dsml"})
        cursor = match.end()
    return calls if not body[cursor:].strip() else []


def _named_call(body: str, name_tag: re.Pattern, param_tag: re.Pattern) -> dict | None:
    names = list(name_tag.finditer(body))
    if len(names) != 1:
        return None
    name_tag_match = names[0]
    name = name_tag_match["v"].strip()
    parts = (body[:name_tag_match.start()], body[name_tag_match.end():])
    if not name or any(param_tag.sub("", part).strip() for part in parts):
        return None
    args: dict = {}
    for pm in param_tag.finditer(body):
        key = pm.group("key").strip()
        if not key or key in args:
            return None
        val = (pm.group("val") or "").strip()
        try:
            args[key] = json.loads(val)
        except Exception:  # noqa: BLE001
            args[key] = val
    return {"name": name, "arguments": args}


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


# ---------------------------------------------------------------------------
# [异常正文隔离 2026-11-20 · card_057 实录] 隔离块供模型使用的视图。
#
# 背景：原生端点把调用写进正文且解析不出（pseudo_call_unparsed）时，
# 异常正文（<func …>/</invoke>/</parameter>/｜｜DSML｜｜ 等调用家具）会随
# 回复块进会话 → 下轮历史原样投影 → 模型照着抄（10:11 两万字符末跳、
# 10:11:57 历史带入数百个闭合标签）。治理口径（施工方案 §3）：
#   · 原文留在会话/事件/界面档案面——审计照查；
#   · 供模型使用的内容换成 ctx_content：剥掉坏片段后的可采信残部 +
#     宿主一行状态；残部为空 ⇒ 只剩状态行。
# 残部切分规则：stripped（已解析跨度）之外再剔「纯调用家具行」——
# 行首是已知调用族标签的行整行剔除（`<parameter name="x">值</parameter>`
# 这种单行参数随标签一起剔）。叙述行即使含内嵌标签也不以标签起始 ⇒
# 不误伤；判据只用已知调用族，不做关键词/外观泛化（方案红线）。
# ---------------------------------------------------------------------------
_Q_LINE = re.compile(
    r"^\s*(?:"
    # 已知调用族标签（开/闭/截断形；含 funccall 复合变体——card_057:3754
    # 实录 <funccall name="x">）；function_results/中日文调用族同家具处理
    r"</?\s*(?:funccall|func|invoke|call|parameter|tool_calls|"
    r"function_calls?|tool_call|function_results|antml:[\w.-]+|executing|执行|confirm"
    r"|函数调用|函數調用|関数呼び出し|调用|工具名|ツール名|参数|パラメータ)\b[^>]*>?"
    # 方括号声明行：[工具调用：X，…]／〔工具调用：…〕（card_049 13:36 实录）
    r"|[\[［【〔]\s*(?:工具调用|ツール呼び出し|関数呼び出し)[：:][^\n]*"
    # DSML/竖线记号：｜｜DSML｜｜、<｜…｜>、</｜DSML｜ parameter>、｜<…>｜
    # ——开闭都收（card_057:3697 实录 </｜DSML｜ invoke> 闭合变体）
    r"|[｜|]+\s*DSML\s*[｜|]+"
    r"|</?\s*[｜|]+\s*[^>\n]*[｜|]?\s*>?"
    r")",
    re.IGNORECASE)
#: 纯标签碎片行（`</`、`<`、多余 `>`、孤立竖线——截断标签的残骸）
_Q_FRAG = re.compile(r"^\s*[<>\s/｜|]+$")
#: 行尾连续家具串：剥 `…</system_warning></｜DSML｜ parameter>` 这类
# 挂在正常行尾的孤立闭标签（card_057:3697 实录——剥尾后正常内容保留）
_Q_TAIL = re.compile(
    r"(?:\s*(?:"
    r"</?\s*(?:funccall|func|invoke|call|parameter|tool_calls|"
    r"function_calls?|tool_call|function_results|antml:[\w.-]+|executing|执行|confirm"
    r"|函数调用|函數調用|関数呼び出し|调用|工具名|ツール名|参数|パラメータ)\b[^>]*>?"
    r"|[｜|]+\s*DSML\s*[｜|]+"
    r"|</?\s*[｜|]+\s*[^>\n]*[｜|]?\s*>?"
    r"))+\s*$",
    re.IGNORECASE)

ANOMALY_NOTE = "〔系统〕该回复含调用格式文本，未派发执行（原文见会话档案）。"
_MIXED_ANOMALY_NOTE = "〔系统〕该回复中的部分调用文本或回执未经执行确认（原文见会话档案）。"
_FRAGMENT_ANOMALY_NOTE = "〔系统〕该回复含供方协议残片（非正文、非调用），已从上下文移除（原文见会话档案）。"

def _quarantine_text(content: str, *, unwrap_report: bool = True) -> str:
    _, _, stripped = extract_text_tool_calls(content, protocols=frozenset({"dsml", "call"}))
    parts = []
    cursor = 0
    # 残片只按原文位置剥一次。去掉前面的调用后，原来的行内文本可能来到行首。
    for start, end, kind, payload in _context_regions(stripped, scan_anomalies=False):
        if kind == "inline":
            continue  # 保留整行语境，仿系统行中的行内代码也随该行剔除。
        parts.append(_strip_furniture_lines(stripped[cursor:start]))
        if kind == "reference":
            parts.append(stripped[start:end])
        elif kind in ("report", "reportshell") and unwrap_report and payload:
            report = payload.get("content")
            if isinstance(report, str):
                parts.append(_quarantine_text(report, unwrap_report=False))
        cursor = end
    parts.append(_strip_furniture_lines(stripped[cursor:]))
    return "".join(parts)


def _strip_furniture_lines(content: str) -> str:
    kept = []
    for line in content.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        tail = line[len(body):]
        body = _Q_TAIL.sub("", body)
        if not (_Q_LINE.match(body) or _Q_FRAG.match(body) or _Q_SYS_LINE.match(body)):
            kept.append(body + tail)
    return "".join(kept)


def quarantine_view(content: str, *, has_dispatch: bool = False,
                    fragment: bool = False) -> str:
    """隔离块供模型使用的内容（ctx_content 候选值）。

    只切"能对应到坏片段"的部分：已解析跨度经 stripped 剥；仿回执整段
    剔除（外壳+假数据），function_calls 结果壳剥壳留 content 报告；
    未解析的孤立标签按行剔除（行首调用族判据）；供方协议残片
    （ds_safety 块/CDATAEND>/begin▁of▁sentence）随 anomaly 跨度退出。
    残部非空 ⇒ 残部＋宿主状态行；空 ⇒ 只剩状态行（方案：必要时只留
    宿主生成的一行状态）。围栏代码块内不做任何剔除（代码示例保留
    引用身份）。``fragment``＝本块唯一异常是供方残片 ⇒ 用残片口径
    状态行（不提"调用文本"——那不是它的罪名）。
    """
    residue = _quarantine_text(content or "").strip()
    if fragment:
        note = _FRAGMENT_ANOMALY_NOTE
    else:
        note = _MIXED_ANOMALY_NOTE if has_dispatch else ANOMALY_NOTE
    return f"{residue}\n\n{note}" if residue else note


def extract_text_tool_calls(
        content: str,
        *,
        protocols: frozenset = frozenset(),
) -> tuple[list[dict], bool, str]:
    """解析正文里的文本形态工具调用。

    返回 (calls, unresolved, stripped_content)：
    - calls: [{name, arguments, _origin}]
    - unresolved: 正文有疑似标记但没解析出任何完整调用（反馈兜底信号）
    - stripped_content: 把已解析的调用块剥离后的正文
    - protocols: 路由声明支持的正文协议集（models.yaml text_protocols）。
      invoke / cjk_call / function_calls 即使未声明，也按整块剥离。
    """
    if not content:
        return [], False, content or ""

    calls: list[dict] = []
    spans: list[tuple[int, int]] = []
    # 解析/疑似判定跑遮蔽文本（引用区/结果区不参与）；span 与原文等长
    # 对齐，stripped 仍从原文算——围栏样例不进 spans、不被派发。
    scan, xml_calls, anomaly_spans = _scan_call_text(content)
    # [异常跨度 2026-11-20] 供方残片随候选剥离——它们从遮蔽文本里就已
    # 消失（其内永不出候选/疑似），这里把跨度并进 stripped 名单让
    # 「供模型使用的内容」同样不含它们（§二.2 同一跨度两用）。
    spans.extend(anomaly_spans)

    # ---- 形态G 预扫描：中日文标签块（<函数调用>/<関数呼び出し>/<调用> 壳）----
    # [2026-11-20 返修 · 用户裁定] G 只识别、只清理、**零派发**——错配闭标签
    # 的完整块曾把 direct 真发进收件卡（隔离复验实录）。整段先收编进 spans，
    # 壳内夹带的 <invoke>/<call> 等可见元素也随之出执行链（A/C/D 的匹配
    # 一律跳过与既有 span 重叠的区段）。unresolved 兜底给「未执行」反馈。
    # 开闭标签都在遮蔽文本上找——行内引用/围栏里的标签是 \x00 不可见，
    # 不会成为假开标签（13:32 实录：`` `関数呼び出し` `` 引用若当开标签，
    # 会惰性吞掉 1100 字正文再撞上真块闭标签）。块 = 开标签到第一个可见
    # 闭标签；无闭标签 ⇒ 只剥开标签行（截断头，残部走家具行剔除）。
    g_regions = []
    for om in _G_OPEN.finditer(scan):
        cm = _G_CLOSE_RE.search(scan, om.end())
        spans.append((om.start(), cm.end() if cm else om.end()))
        g_regions.append((om.start(), cm.end() if cm else len(content)))

    for start, end, kind, payload in xml_calls:
        spans.append((start, end))
        if kind not in protocols or any(start < e and end > s for s, e in g_regions):
            continue
        parsed = (_named_call(payload, *_XML_FIELDS[kind])
                  if isinstance(payload, str) else payload)
        if parsed:
            calls.append({**parsed, "_origin": _COMPAT_ORIGINS[kind]})

    # ---- 形态A：原生标记 ----
    if _A_BLOCK_OPEN.search(scan) or _A_INVOKE.search(scan):
        a_spans: list[tuple[int, int]] = []
        for m in _unmasked_matches(_A_INVOKE, scan):
            if any(m.start() < e and m.end() > s for s, e in spans):
                continue          # 落在已收编的 G 壳内 ⇒ 不派、不剥第二遍
            name = (m.group("name") or "").strip()
            if not name:
                continue
            calls.append({
                "name": name,
                "arguments": _parse_invoke_params(m.group("body")),
                "_origin": "textA",
            })
            a_spans.append(m.span())
        spans.extend(a_spans)
        # 整段 tool_calls 外壳（开标记到文本中最后一个 invoke 结束）一并剥
        open_m = _A_BLOCK_OPEN.search(scan)
        if open_m and a_spans:
            last_end = max(e for _, e in a_spans)
            if _MASKED_CHAR not in scan[open_m.start():last_end]:
                spans.append((open_m.start(), last_end))
        # 外壳可能还带闭合尾标记
        for tail in re.finditer(
                rf"<{_PIPE}\s*/?\s*tool_calls\s*{_PIPE}?>", scan, re.IGNORECASE):
            spans.append(tail.span())

    # ---- 形态C：antml/function_calls XML 元素伪调用 ----
    for m in _unmasked_matches(_C_CALL, scan):
        name = (m.group("name") or "").strip()
        if not name:
            continue
        if any(m.start() < e and m.end() > s for s, e in spans):
            continue  # <invoke name=> 已随形态A收编——同一跨度不二次派发
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
    for wm in _C_WRAPPER.finditer(scan):
        spans.append(wm.span())

    # ---- 形态E：完整 DSML calls 区（路由声明 dsml 时启用）----
    if "dsml" in protocols:
        for m in _unmasked_matches(_F_DSML_BLOCK, scan):
            if any(m.start() < e and m.end() > s for s, e in spans):
                continue          # 与已收编跨度重叠 ⇒ 不二次吞
            block_calls = _dsml_calls(m.group("body"))
            if not block_calls:
                continue
            calls.extend(block_calls)
            spans.append(m.span())

    # ---- 形态F：<call><name>X</name><parameter name="k">v</parameter></call>
    # （路由声明 call 时启用；一块一个工具名，同级多块顺序各算一条）----
    if "call" in protocols:
        for m in _unmasked_matches(_F_CALL, scan):
            if any(m.start() < e and m.end() > s for s, e in spans):
                continue
            parsed = _named_call(m.group("body"), _F_CALL_NAME, _F_CALL_PARAM)
            if parsed is None:
                continue
            calls.append({**parsed, "_origin": "textF_call"})
            spans.append(m.span())

    # ---- 形态D：根元素即工具名（<read_card><card_id>…</read_card>）----
    _d_call, _d_self, _d_sus = _d_patterns()
    if _d_call is not None:
        for m in _unmasked_matches(_d_call, scan):
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
            for m in _unmasked_matches(_d_self, scan):
                if any(m.start() < e and m.end() > s for s, e in spans):
                    continue
                calls.append({"name": (m.group("name") or "").strip(),
                              "arguments": {}, "_origin": "textD"})
                spans.append(m.span())

    # ---- 思维标签家具：泄漏的 </think>/<antml:thinking> 纯噪声，剥 ----
    for tm in _THINK_TAG.finditer(scan):
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
        bool(g_regions or xml_calls)           # 已识别的中日文壳及兼容块
        or bool(_SUSPECT.search(scan))
        or bool(re.search(_DSML, scan))         # DSML 壳（含未启用路由）
        or bool(_F_CALL_TAG.search(scan))       # <call> 壳（含截断和未启用）
        or bool(_G_BRACKET_DECL.search(scan))   # [工具调用：…] 声明行
        or (_d_sus is not None and bool(_d_sus.search(scan)))
        or _e_unknown_pair(scan, _d_names()))

    return calls, unresolved, stripped
