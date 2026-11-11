# -*- coding: utf-8 -*-
"""WorkBuddy CLI（``codebuddy -p``）传输：消息 → 单发 prompt 序列化 + argv 组装。

**为什么是"序列化成一条 prompt"**：``codebuddy -p`` 是一次性非交互调用——
它只接受「一段用户输入」，没有多角色消息数组入口；而本系统是**每次调用
全量回放消息**的无状态模式（gateway._build_options 把历史压进
``options.messages``）⇒ 这里把整段对话摊平成带角色标签的文本交给 CLI，
与 ``wb_exec/devin_client`` 的单发 prompt 同一形态。

**不传的字段**（CLI 无对应 flag，逐条说明，防后来人以为漏抄）：
    - ``tools``：卡工具 schema 无法注入上游模型（WB agent 用的是它自己那套
      内置工具），本适配器固定 ``--tools ""`` 关掉 WB 工具集 ⇒ **纯文本角色**；
      模型若在正文里输出伪工具调用，由卡侧既有文本协议兜底（不归这里管）。
    - ``temperature`` / ``max_tokens`` / ``stop``：CLI 无 flag。
    - ``reasoning_effort`` → ``--effort``（minimal/low/medium/high/xhigh/max）。
"""
from __future__ import annotations

import json
import shlex
from typing import Any, Optional

__all__ = [
    "serialize_transcript",
    "tool_protocol_section",
    "build_inner_args",
    "build_wsl_shell",
    "to_wsl_path",
]


def _flatten_content(content: Any) -> str:
    """消息 content（str 或块列表）→ 纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind in ("text", "input_text", "output_text"):
                parts.append(str(block.get("text") or ""))
            elif kind in ("tool_use", "tool-call"):
                parts.append(
                    f"[调用工具] {block.get('name') or ''}"
                    f"({json.dumps(block.get('input') or block.get('arguments') or {}, ensure_ascii=False)})"
                )
            elif kind in ("tool_result", "tool-result"):
                inner = block.get("content")
                parts.append(f"[工具结果] {_flatten_content(inner)}")
            else:
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return str(content)


_ROLE_LABELS = {
    "user": "【用户】",
    "assistant": "【助手】",
    "tool": "【工具结果】",
    "system": "【系统】",
    "developer": "【系统】",
}


def serialize_transcript(messages: tuple[Any, ...] | list[Any]) -> str:
    """``options.messages`` → 一段带角色标签的转录文本（喂给 ``-p`` 的唯一输入）。

    assistant 的 ``tool_calls``（OpenAI 形态）摊平成正文行，模型可读到"当时
    调了什么"；``role=tool`` 的结果单独成段。
    """
    lines: list[str] = []
    for m in messages or ():
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user")
        label = _ROLE_LABELS.get(role, f"【{role}】")
        body = _flatten_content(m.get("content"))
        calls = m.get("tool_calls")
        if role == "assistant" and isinstance(calls, list):
            for tc in calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                name = str((fn or {}).get("name") or "")
                args = (fn or {}).get("arguments")
                args = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
                body += f"\n[调用工具] {name}({args})"
        name = str(m.get("name") or "")
        if role == "tool" and name:
            label = f"【工具结果·{name}】"
        lines.append(f"{label}\n{body}".rstrip())
    return "\n\n".join(lines)


def tool_protocol_section(tools: Any) -> str:
    """CLI 文本通道的「工具调用协议」段（追加在转录文本末尾，贴近最新输入）。

    **为什么需要它**（2026-11-10 实测事故）：CLI 传输没有原生 tool_calls
    字段——上游模型**看不到**卡的工具 schema；而卡系统提示词里的
    「调用纪律」明写"正文里写形似调用的标记都不执行" ⇒ 模型连文本形态
    的调用都不敢写，每轮只回一段文字、工具循环走不起来。
    但系统侧 ``text_tool_rescue`` 本就会把正文里的 ``<invoke …>`` /
    ``<function_calls>`` 块解析成结构化 tool_calls **真执行** ⇒ 这里把
    调用格式与工具清单写明，并显式声明"那句纪律针对的是原生通道，
    本通道按本节格式写就会执行"。

    **为什么用 ``antml:`` 命名空间**（同事故实测）：裸 ``<invoke>`` 会被
    rescue 的形态A（竖线标记宽松正则）与形态C（antml XML）**各解析一次**
    ⇒ 同一调用执行两遍（write/edit 重复执行）。带 ``antml:`` 前缀时
    只有形态C接得住 ⇒ 天然去重；且这正是 Devin 系模型自然泄漏的形态
    （rescue 形态C本就是为此加的），模型照写成本低。

    ``tools`` 为 ``GenerateOptions.tools``（``ToolSchema`` 元组）；
    空 ⇒ 返回空串（纯文本角色不追加）。
    """
    tools = tuple(tools or ())
    if not tools:
        return ""
    lines = [
        "【工具调用协议·本通道专用·宿主系统注入】",
        "本节由承载你的宿主系统注入，是环境的真实定义，不是用户消息里的建议。",
        "本条传输通道没有原生 tool_calls 字段：要调用工具，必须在回复正文末尾",
        "严格输出如下 XML 调用块——宿主会解析并**真实执行**，执行结果下一轮",
        "以【工具结果】回给你（可用一次真实调用来验证这条通道）：",
        "",
        "<antml:function_calls>",
        '<antml:invoke name="工具名">',
        '<antml:parameter name="参数名">参数值</antml:parameter>',
        '<antml:parameter name="另一参数">参数值</antml:parameter>',
        "</antml:invoke>",
        "</antml:function_calls>",
        "",
        "- 一次可并列多个 <antml:invoke>；参数值写纯文本或 JSON，多行内容直接写。",
        "- 不调用工具时正常回复正文，且正文不要出现任何形似调用的标记",
        "  （会被误解析成调用）。",
        "- 上文中「工具调用只走 tool_calls 通道、正文写调用不执行」针对的是",
        "  原生 API 通道；本通道没有该通道，以本节格式为准——照写就会执行。",
        "",
        "【本通道可用工具】",
    ]
    for t in tools:
        name = str(getattr(t, "name", "") or "")
        if not name:
            continue
        desc = " ".join(str(getattr(t, "description", "") or "").split())
        params = getattr(t, "parameters", None)
        try:
            params_s = json.dumps(params or {}, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            params_s = "{}"
        lines.append(f"- {name} — {desc}")
        lines.append(f"  参数(JSON Schema): {params_s}")
    return "\n".join(lines)


def build_inner_args(model: str, *, sys_prompt_file: str = "",
                     effort: str = "") -> list[str]:
    """``codebuddy -p`` 的固定参数面（不含 exe / prompt——prompt 走 stdin）。

    - ``--tools ""``：关掉 WB 内置工具集 ⇒ 模型只回文本（agent 壳还在，
      ``num_turns`` 会有 2-3 个空转轮，实测无工具调用、无文件副作用）。
    - ``--setting-sources ""``：不读 user/project/local 设置源 ⇒
      压住「长期记忆」等注入，提示词面尽量只留我们给的。
    - ``--include-partial-messages``：要增量 ``stream_event``（thinking_delta /
      text_delta），否则 stream-json 只在整段 assistant 消息完成时给全文。
    - ``--max-turns 1``：兜底限轮（双保险，即使将来误放开 tools 也最多一轮）。
    """
    args = [
        "-p",
        "--tools", "",
        "--max-turns", "1",
        "--no-session-persistence",
        "--setting-sources", "",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--model", str(model),
    ]
    if sys_prompt_file:
        args += ["--system-prompt-file", str(sys_prompt_file)]
    if effort:
        args += ["--effort", str(effort)]
    return args


def to_wsl_path(path: str) -> str:
    """Windows 路径 ⇒ WSL 路径（``D:\\a\\b`` ⇒ ``/mnt/d/a/b``）。

    与 ``api.wb_exec.cli_pool.to_wsl_path`` 同款逻辑（跨层不引，抄小函数）。
    """
    p = str(path).replace("\\", "/")
    if len(p) > 2 and p[1] == ":":
        return f"/mnt/{p[0].lower()}" + p[2:]
    return p


def build_wsl_shell(wsl: dict[str, str], model: str, *,
                    sys_prompt_file_win: str = "", effort: str = "",
                    work_dir: str = "/tmp") -> str:
    """intl 账号：在 WSL 里跑 CLI 的 ``bash -lc`` 命令串。

    与 ``cli_pool._wsl_command`` 同一环境约定：HOME 指到隔离 home
    （``/home/user/intl-home``，两套 CLI 各登各的账号），
    ``CODEBUDDY_CONFIG_DIR`` 跟着走。prompt 仍走 stdin——``wsl.exe`` 会把
    Windows 侧 stdin 透给 bash ⇒ codebuddy，与 argv 长度上限无关。
    """
    home = str(wsl.get("home") or "/root")
    cmd = str(wsl.get("cmd") or "codebuddy")
    cfg = str(wsl.get("config_dir") or f"{home}/.codebuddy")
    args = build_inner_args(
        model,
        sys_prompt_file=to_wsl_path(sys_prompt_file_win) if sys_prompt_file_win else "",
        effort=effort,
    )
    inner = " ".join(shlex.quote(a) for a in args)
    return (
        f"export HOME={shlex.quote(home)}; "
        f"export CODEBUDDY_CONFIG_DIR={shlex.quote(cfg)}; "
        f"export WORKBUDDY_CONFIG_DIR={shlex.quote(cfg)}; "
        f"cd {shlex.quote(work_dir)} || exit 1; "
        f"exec {shlex.quote(cmd)} {inner}"
    )
