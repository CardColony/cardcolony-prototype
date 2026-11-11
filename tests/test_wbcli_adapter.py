# -*- coding: utf-8 -*-
"""llm_wbcli（WorkBuddy CLI 传输适配器）单测。

不真起 codebuddy —— ``spawn`` 钩子注入假进程，回放实测抓到的 stream-json
行（``system/init`` → ``stream_event``×N → ``result``），验证：
    1. 文本 delta → TextDelta；thinking delta → ReasoningDelta；
    2. ``result.is_error`` / 顶层 ``{"type":"error"}`` / 非零退出码 → error 终帧；
    3. 无 finish 事件时 ``result`` 成功补 ``StopReason``；
    4. 消息序列化把 tool_calls / tool 结果摊平成可读文本；
    5. argv/WSL 壳按 config_dir·wsl 段正确组装。
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.model_gateway.llm.runtime import LlmRuntime  # noqa: E402
from src.model_gateway.llm.types import (  # noqa: E402
    FinishChunk,
    GenerateOptions,
    ReasoningDelta,
    TextDelta,
    ToolSchema,
    UsageChunk,
)
from src.model_gateway.llm_wbcli import (  # noqa: E402
    WbCliAdapter,
    WbCliAdapterOptions,
    WbCliConnectionOptions,
)
from src.model_gateway.llm_wbcli.serialize import (  # noqa: E402
    build_wsl_shell,
    serialize_transcript,
    tool_protocol_section,
)


class _FakePipe:
    """假 stdout/stderr：readline 逐行吐、read 一次收齐。"""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        return self._chunks.pop(0) if self._chunks else b""

    async def read(self) -> bytes:
        await asyncio.sleep(0)
        return b"".join(self._chunks)


class _FakeStdin:
    def __init__(self):
        self.data = b""

    def write(self, b: bytes) -> None:
        self.data += b

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProc:
    def __init__(self, lines: list[str], *, returncode: int = 0,
                 stderr: bytes = b""):
        self.stdout = _FakePipe([l.encode("utf-8") + b"\n" for l in lines])
        self.stderr = _FakePipe([stderr])
        self.stdin = _FakeStdin()
        self.returncode: int | None = None
        self._rc = returncode
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        await asyncio.sleep(0)
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode


def _ev(block_type: str = "text", index: int = 0) -> dict:
    block = {"type": "text", "text": ""} if block_type == "text" \
        else {"type": "thinking", "thinking": ""}
    return {"type": "content_block_start", "index": index, "content_block": block}


def _delta(kind: str, text: str, index: int = 0) -> dict:
    return {"type": "content_block_delta", "index": index,
            "delta": {"type": kind, "thinking" if kind == "thinking_delta" else "text": text}}


def _wrap(event: dict) -> str:
    return json.dumps({"type": "stream_event", "event": event}, ensure_ascii=False)


def _ok_lines() -> list[str]:
    return [
        json.dumps({"type": "system", "subtype": "init"}),
        _wrap({"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}}),
        _wrap(_ev("thinking")),
        _wrap(_delta("thinking_delta", "想")),
        _wrap({"type": "content_block_stop", "index": 0}),
        _wrap(_ev("text", index=1)),
        _wrap(_delta("text_delta", "回", index=1)),
        _wrap(_delta("text_delta", "答", index=1)),
        _wrap({"type": "content_block_stop", "index": 1}),
        _wrap({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
               "usage": {"input_tokens": 20, "output_tokens": 2}}),
        _wrap({"type": "message_stop"}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "result": "回答", "usage": {"input_tokens": 20, "output_tokens": 2}}),
    ]


def _adapter(lines: list[str], *, returncode: int = 0, stderr: bytes = b"",
             conn: WbCliConnectionOptions | None = None) -> tuple[WbCliAdapter, list[_FakeProc]]:
    procs: list[_FakeProc] = []

    async def spawn(argv, env, cwd):
        p = _FakeProc(lines, returncode=returncode, stderr=stderr)
        p._argv = argv
        procs.append(p)
        return p

    return WbCliAdapter(WbCliAdapterOptions(
        options=lambda _p, _m: conn or WbCliConnectionOptions(
            exe="codebuddy.exe", config_dir="C:\\cfg"),
        spawn=spawn,
    )), procs


def _opts(tools: tuple | None = None) -> GenerateOptions:
    return GenerateOptions(
        provider="workbuddy-cn",
        model="hy3",
        messages=({"role": "user", "content": "问好"},),
        system="系提示词",
        tools=tools,
    )


async def _collect(adapter: WbCliAdapter, options: GenerateOptions) -> list:
    return [c async for c in adapter.stream(options)]


class TestStreamHappyPath(unittest.TestCase):
    def test_deltas_and_finish(self):
        adapter, procs = _adapter(_ok_lines())
        chunks = asyncio.run(_collect(adapter, _opts()))
        texts = [c for c in chunks if isinstance(c, TextDelta)]
        reasons = [c for c in chunks if isinstance(c, ReasoningDelta)]
        finishes = [c for c in chunks if isinstance(c, FinishChunk)]
        usages = [c for c in chunks if isinstance(c, UsageChunk)]
        self.assertEqual("".join(c.text for c in texts), "回答")
        self.assertEqual("".join(c.text for c in reasons), "想")
        self.assertTrue(finishes, "必须出终帧")
        self.assertEqual(finishes[-1].reason.kind, "stop")
        self.assertTrue(usages, "usage 至少一条（stats 记账）")
        # prompt 走了 stdin，不挂 argv
        self.assertIn("问好", procs[0].stdin.data.decode("utf-8"))
        # --tools "" 与 --model 进 argv
        argv = procs[0]._argv
        self.assertIn("--tools", argv)
        self.assertIn("hy3", argv)


class TestStreamFailures(unittest.TestCase):
    def test_result_is_error(self):
        lines = _ok_lines()[:-1] + [
            json.dumps({"type": "result", "subtype": "error_during_execution",
                        "is_error": True, "result": "额度已用完"}),
        ]
        adapter, _ = _adapter(lines)
        chunks = asyncio.run(_collect(adapter, _opts()))
        finishes = [c for c in chunks if isinstance(c, FinishChunk)]
        self.assertEqual(finishes[-1].reason.kind, "error")
        self.assertIn("额度已用完", finishes[-1].reason.failure.message)

    def test_top_level_error_no_result(self):
        lines = [json.dumps({"type": "error", "error": "当前部署未启用 PTC/极简模式"})]
        adapter, _ = _adapter(lines, returncode=1)
        chunks = asyncio.run(_collect(adapter, _opts()))
        finishes = [c for c in chunks if isinstance(c, FinishChunk)]
        self.assertEqual(finishes[-1].reason.kind, "error")
        self.assertIn("PTC", finishes[-1].reason.failure.message)

    def test_nonzero_exit_without_result(self):
        adapter, _ = _adapter([], returncode=3, stderr=b"Authentication required")
        chunks = asyncio.run(_collect(adapter, _opts()))
        finishes = [c for c in chunks if isinstance(c, FinishChunk)]
        self.assertEqual(finishes[-1].reason.kind, "error")
        self.assertIn("Authentication required", finishes[-1].reason.failure.message)

    def test_success_without_finish_event(self):
        # 事件流里没有 message_delta/message_stop，只有 result.success
        lines = [_wrap(_ev()), _wrap(_delta("text_delta", "答")),
                 json.dumps({"type": "result", "subtype": "success",
                             "is_error": False, "result": "答"})]
        adapter, _ = _adapter(lines)
        chunks = asyncio.run(_collect(adapter, _opts()))
        finishes = [c for c in chunks if isinstance(c, FinishChunk)]
        self.assertEqual(finishes[-1].reason.kind, "stop")


class TestSerialize(unittest.TestCase):
    def test_transcript_flattens_roles_and_tool_calls(self):
        text = serialize_transcript([
            {"role": "system", "content": "规则"},
            {"role": "user", "content": [{"type": "text", "text": "甲"}]},
            {"role": "assistant", "content": "好",
             "tool_calls": [{"function": {"name": "write_file",
                                         "arguments": '{"p":1}'}}]},
            {"role": "tool", "name": "write_file", "content": "ok"},
        ])
        self.assertIn("【系统】", text)
        self.assertIn("【用户】", text)
        self.assertIn("[调用工具] write_file", text)
        self.assertIn("【工具结果·write_file】", text)

    def test_tool_protocol_section_lists_tools_and_format(self):
        sec = tool_protocol_section((
            ToolSchema(name="write_file", description="写文件\n到磁盘",
                       parameters={"type": "object",
                                   "properties": {"path": {"type": "string"}}}),
        ))
        self.assertIn("<antml:function_calls>", sec)
        self.assertIn('<antml:invoke name="工具名">', sec)
        self.assertIn("write_file", sec)
        self.assertIn('"path"', sec)
        # 描述折成一行（防把 schema/说明换行打断协议段）
        self.assertIn("写文件 到磁盘", sec)
        # 空工具面不追加段
        self.assertEqual(tool_protocol_section(()), "")
        self.assertEqual(tool_protocol_section(None), "")

    def test_tools_section_goes_to_system_file_not_stdin(self):
        # [2026-11-10] 协议段挪进 --system-prompt-file（用户转录里会被
        # agent 模型判为"用户声称"而拒绝）；stdin 里不应再出现。
        conn = WbCliConnectionOptions(exe="codebuddy.exe")
        procs: list[_FakeProc] = []
        sysfile_text = ""

        async def spawn(argv, env, cwd):
            nonlocal sysfile_text
            p = _FakeProc(_ok_lines())
            p._argv = argv
            if "--system-prompt-file" in argv:
                sysfile_text = open(
                    argv[argv.index("--system-prompt-file") + 1],
                    encoding="utf-8").read()
            procs.append(p)
            return p

        adapter = WbCliAdapter(WbCliAdapterOptions(
            options=lambda _p, _m: conn, spawn=spawn))
        opts = _opts(tools=(ToolSchema(
            name="read_file", description="读文件",
            parameters={"type": "object"}),))
        asyncio.run(_collect(adapter, opts))
        self.assertIn("【工具调用协议", sysfile_text)
        self.assertIn("<antml:invoke", sysfile_text)
        self.assertIn("read_file", sysfile_text)
        self.assertIn("系提示词", sysfile_text)  # 卡系提示词仍在前面
        self.assertNotIn("工具调用协议",
                         procs[0].stdin.data.decode("utf-8"))

    def test_no_tools_means_no_protocol_section(self):
        adapter, procs = _adapter(_ok_lines())
        asyncio.run(_collect(adapter, _opts()))
        fed = procs[0].stdin.data.decode("utf-8")
        self.assertNotIn("工具调用协议", fed)

    def test_wsl_shell_env_and_cmd(self):
        shell = build_wsl_shell(
            {"distro": "Ubuntu-22.04", "home": "/home/user/intl-home",
             "config_dir": "/home/user/intl-home/.codebuddy",
             "cmd": "/home/user/.local/bin/codebuddy"},
            "hy3", sys_prompt_file_win="D:\\Temp\\x.txt")
        self.assertIn("HOME=/home/user/intl-home", shell)
        self.assertIn("CODEBUDDY_CONFIG_DIR=/home/user/intl-home/.codebuddy", shell)
        self.assertIn("/mnt/d/Temp/x.txt", shell)
        self.assertIn("/home/user/.local/bin/codebuddy", shell)


class TestRuntimeWiring(unittest.TestCase):
    def test_registered_and_failure_normalized(self):
        runtime = LlmRuntime()
        adapter, _ = _adapter([], returncode=9, stderr=b"boom")
        runtime.register_adapter(["workbuddy-cn"], adapter)
        self.assertIn("workbuddy-cn", runtime.registered_providers())

        async def go():
            return [c async for c in runtime.stream(_opts())]
        chunks = asyncio.run(go())
        finishes = [c for c in chunks if isinstance(c, FinishChunk)]
        self.assertEqual(finishes[-1].reason.kind, "error")


if __name__ == "__main__":
    unittest.main()
