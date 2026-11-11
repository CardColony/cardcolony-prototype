# -*- coding: utf-8 -*-
"""llm_devincli（Devin CLI 传输适配器）单测。

不真起 devin——``spawn`` 钩子注入假进程，验证：
    1. stdout 正文 / ATIF export 正文两路 → TextDelta + StopReason；
    2. ATIF 的 reasoning_content → ReasoningDelta；final_metrics → UsageChunk；
    3. 非零退出 / 无正文 / 认证措辞 → error 终帧（DEVINCLI_EXIT / _AUTH）；
    4. argv 契约：本地模式（无 --cloud）、--model/--prompt-file/--export/
       --permission-mode/--respect-workspace-trust；
    5. env 剔 ACP_BACKEND；prompt 文件含系统段+转录+工具协议段（带 tools 时）；
    6. signal abort → AbortedReason；conn.timeout → DEVINCLI_TIMEOUT。
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
from src.model_gateway.llm_devincli import (  # noqa: E402
    DevinCliAdapter,
    DevinCliAdapterOptions,
    DevinCliConnectionOptions,
)


class _FakePipe:
    def __init__(self, data: bytes = b""):
        self._data = data

    async def read(self) -> bytes:
        await asyncio.sleep(0)
        return self._data


class _FakeProc:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"",
                 returncode: int = 0, wait_s: float = 0.0):
        self.stdout = _FakePipe(stdout)
        self.stderr = _FakePipe(stderr)
        self.returncode: int | None = None
        self._rc = returncode
        self._wait_s = wait_s
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9

    async def wait(self) -> int:
        if self._wait_s:
            await asyncio.sleep(self._wait_s)
        else:
            await asyncio.sleep(0)
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode


def _atif(text: str = "ATIF正文", reasoning: str = "",
          inp: int = 123, out: int = 45) -> bytes:
    return json.dumps({
        "schema_version": "ATIF-v1.7", "session_id": "s-1",
        "steps": [{"source": "agent", "message": text,
                   "reasoning_content": reasoning}],
        "final_metrics": {"total_prompt_tokens": inp,
                          "total_completion_tokens": out},
    }, ensure_ascii=False).encode("utf-8")


def _conn(**kw) -> DevinCliConnectionOptions:
    base = dict(exe="devin.exe", work_dir="D:\\Temp\\scratch",
                permission_mode="auto", timeout=1800.0)
    base.update(kw)
    return DevinCliConnectionOptions(**base)


def _adapter(*, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0,
             atif: bytes | None = None, wait_s: float = 0.0,
             conn: DevinCliConnectionOptions | None = None,
             captured: dict | None = None):
    procs: list[_FakeProc] = []

    async def spawn(argv, env, cwd):
        if captured is not None:
            captured["argv"], captured["env"], captured["cwd"] = argv, env, cwd
            if "--prompt-file" in argv:
                captured["prompt"] = open(
                    argv[argv.index("--prompt-file") + 1],
                    encoding="utf-8").read()
        if "--export" in argv and atif is not None:
            open(argv[argv.index("--export") + 1], "wb").write(atif)
        p = _FakeProc(stdout=stdout, stderr=stderr,
                      returncode=returncode, wait_s=wait_s)
        procs.append(p)
        return p

    return DevinCliAdapter(DevinCliAdapterOptions(
        options=lambda _p, _m: conn or _conn(), spawn=spawn)), procs


def _opts(tools: tuple | None = None, signal=None) -> GenerateOptions:
    return GenerateOptions(
        provider="devin",
        model="swe-2-max",
        messages=({"role": "user", "content": "问好"},),
        system="卡系统提示词",
        tools=tools,
        signal=signal,
    )


async def _collect(adapter, options) -> list:
    return [c async for c in adapter.stream(options)]


class TestHappyPath(unittest.TestCase):
    def test_stdout_text_finish(self):
        adapter, _ = _adapter(stdout="PING-OK".encode())
        chunks = asyncio.run(_collect(adapter, _opts()))
        texts = [c for c in chunks if isinstance(c, TextDelta)]
        finishes = [c for c in chunks if isinstance(c, FinishChunk)]
        self.assertEqual("".join(c.text for c in texts), "PING-OK")
        self.assertEqual(finishes[-1].reason.kind, "stop")

    def test_atif_text_reasoning_usage(self):
        adapter, _ = _adapter(atif=_atif("ATIF答", reasoning="想过", inp=100, out=7),
                              stdout="stdout文".encode())
        chunks = asyncio.run(_collect(adapter, _opts()))
        texts = [c for c in chunks if isinstance(c, TextDelta)]
        reasons = [c for c in chunks if isinstance(c, ReasoningDelta)]
        usages = [c for c in chunks if isinstance(c, UsageChunk)]
        self.assertEqual("".join(c.text for c in texts), "ATIF答")
        self.assertEqual("".join(c.text for c in reasons), "想过")
        self.assertEqual(usages[-1].usage.input_tokens, 100)
        self.assertEqual(usages[-1].usage.output_tokens, 7)


class TestFailures(unittest.TestCase):
    def test_nonzero_exit(self):
        adapter, _ = _adapter(returncode=1, stderr=b"boom detail")
        chunks = asyncio.run(_collect(adapter, _opts()))
        fin = [c for c in chunks if isinstance(c, FinishChunk)][-1]
        self.assertEqual(fin.reason.kind, "error")
        self.assertEqual(fin.reason.failure.code, "DEVINCLI_EXIT")
        self.assertIn("boom detail", fin.reason.failure.message)

    def test_no_text_is_error(self):
        adapter, _ = _adapter(stdout=b"")
        chunks = asyncio.run(_collect(adapter, _opts()))
        fin = [c for c in chunks if isinstance(c, FinishChunk)][-1]
        self.assertEqual(fin.reason.kind, "error")

    def test_auth_error_code(self):
        adapter, _ = _adapter(returncode=1, stderr=b"Not logged in")
        chunks = asyncio.run(_collect(adapter, _opts()))
        fin = [c for c in chunks if isinstance(c, FinishChunk)][-1]
        self.assertEqual(fin.reason.failure.code, "DEVINCLI_AUTH")

    def test_timeout(self):
        adapter, _ = _adapter(wait_s=60.0, conn=_conn(timeout=0.3))
        chunks = asyncio.run(_collect(adapter, _opts()))
        fin = [c for c in chunks if isinstance(c, FinishChunk)][-1]
        self.assertEqual(fin.reason.kind, "error")
        self.assertEqual(fin.reason.failure.code, "DEVINCLI_TIMEOUT")

    def test_abort(self):
        class _Sig:
            aborted = True
        adapter, procs = _adapter(wait_s=60.0)
        chunks = asyncio.run(_collect(adapter, _opts(signal=_Sig())))
        fin = [c for c in chunks if isinstance(c, FinishChunk)][-1]
        self.assertEqual(fin.reason.kind, "aborted")
        self.assertTrue(procs[0].killed)


class TestArgvEnvPrompt(unittest.TestCase):
    def test_argv_contract(self):
        cap: dict = {}
        adapter, _ = _adapter(stdout=b"ok", captured=cap)
        asyncio.run(_collect(adapter, _opts()))
        argv = cap["argv"]
        self.assertIn("-p", argv)
        self.assertIn("--prompt-file", argv)
        self.assertIn("--export", argv)
        self.assertIn("--model", argv)
        self.assertIn("swe-2-max", argv)
        self.assertIn("--permission-mode", argv)
        self.assertIn("auto", argv)
        self.assertIn("--respect-workspace-trust", argv)
        self.assertNotIn("--cloud", argv)  # 本地模式红线
        self.assertEqual(cap["cwd"], "D:\\Temp\\scratch")

    def test_env_drops_acp_backend(self):
        import os
        os.environ["ACP_BACKEND"] = "windsurf"
        try:
            cap: dict = {}
            adapter, _ = _adapter(stdout=b"ok", captured=cap)
            asyncio.run(_collect(adapter, _opts()))
            self.assertNotIn("ACP_BACKEND", cap["env"])
        finally:
            os.environ.pop("ACP_BACKEND", None)

    def test_prompt_file_layout(self):
        cap: dict = {}
        adapter, _ = _adapter(stdout=b"ok", captured=cap)
        asyncio.run(_collect(adapter, _opts(tools=(ToolSchema(
            name="read_file", description="读文件",
            parameters={"type": "object"}),))))
        prompt = cap["prompt"]
        self.assertIn("【工具调用协议", prompt)          # 协议段置顶
        self.assertIn("<antml:invoke", prompt)
        self.assertIn("内置工具", prompt)                # devin 专用禁令
        self.assertIn("【系统·宿主注入】", prompt)
        self.assertIn("卡系统提示词", prompt)
        self.assertIn("【用户】", prompt)               # 转录段
        self.assertIn("问好", prompt)
        # 顺序断言：协议段 < 系统段 < 转录段
        self.assertLess(prompt.index("工具调用协议"),
                        prompt.index("卡系统提示词"))
        self.assertLess(prompt.index("卡系统提示词"),
                        prompt.index("问好"))


class TestRuntimeWiring(unittest.TestCase):
    def test_registered_and_failure_normalized(self):
        runtime = LlmRuntime()
        adapter, _ = _adapter(returncode=9, stderr=b"boom")
        runtime.register_adapter(["devin"], adapter)
        self.assertIn("devin", runtime.registered_providers())

        async def go():
            return [c async for c in runtime.stream(_opts())]
        chunks = asyncio.run(go())
        fin = [c for c in chunks if isinstance(c, FinishChunk)][-1]
        self.assertEqual(fin.reason.kind, "error")


if __name__ == "__main__":
    unittest.main()
