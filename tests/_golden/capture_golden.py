# -*- coding: utf-8 -*-
"""T-0009 阶段一/三 · 编排金样本抓取与回放（只读观测，不改src）。

用途：`_run_message` 拆分前抓「编排行为」的金样本，拆分后回放逐字节 diff。
**pytest 绿数证不了编排等价**（同一批断言可由不同编排顺序满足），
故另建本判据：SSE 事件序列＋最终 body ＋落盘副作用三件套。

三条剧本（覆盖 _run_message 全部分支）：
  S1 纯文本一轮交付  —— 快车道命中、无工具、单模型直答（跳三席与统合官）
  S2 工具循环         —— 快车道 ＋ 工具调用：tool_call/tool_result/card_op 埋点、
                        轮槽 append_tool_result、user_prompt 续跳占位、T14 拼接
  S3 三席后置＋统合   —— chain 路径：工具查证 → seats_enter → 三席观点 →
                        统合轮 → 交付（T14 拼接＋viewpoints 追加）

确定性处理（拆分前后**必须**用同一套归一，否则 diff 无意义）：
  pair_id(uuid4) / 事件ts / 卡 created_at·updated_at / 日志文件名时间戳 / 时间戳正文
⇒ 归一为占位符。归一表见 NORMALIZE_NOTE。

用法：
  $PY tests/_golden/capture_golden.py <输出目录>     # 抓三件套（每剧本跑 2 次验一致性）
  $PY tests/_golden/capture_golden.py <输出目录> --check-only   # 只重抓不比对
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

NORMALIZE_NOTE = (
    "归一字段：pair_id(uuid4)→<PAIR_ID>；事件 ts→<TS>、seq→本请求内相对序号；"
    "卡created_at/updated_at 与 history 行时间戳→<ISO>；"
    "content_hash（对含时间戳的内容求值故逐次不同）→<CONTENT_HASH>；"
    "prompt 日志 timestamp→<ISO>；日志文件名 %Y%m%d%H%M%S → <STAMP>"
)


# ---------------------------------------------------------------- 确定性替身

class ScriptedGateway:
    """确定性网关替身（签名与真网关逐参数对齐，参照 tests/test_offline_e2e.FakeGateway）。

    与用例里的 FakeGateway 同族，差别只在**内容按剧本参数化**，便于金样本复现。
    """

    def __init__(self, main_texts: list[str] | None = None,
                 vp_text: str = "[替身观点] 建议先补依据再定论。",
                 tool_script: list[dict[str, Any]] | None = None):
        self.config = {
            "models": {
                "main": {"provider": "openai", "model": "golden-main", "temperature": 0.3},
                "vp1": {"provider": "openai", "model": "golden-vp", "temperature": 0.7},
            },
            "simple": {"enabled": True, "viewpoints": ["vp1"]},
        }
        # main 按调用序取文本；用尽后重复最后一条（保证不限轮次也能收敛）
        self.main_texts = list(main_texts or ["金样本回执。"])
        self.vp_text = vp_text
        self.tool_script = tool_script or []
        self.calls: list[dict[str, Any]] = []
        self._tool_issued = False

    async def call(self, model_name, system_prompt, user_prompt, temperature=0.5,
                   max_tokens=None, timeout=None, skip_completion_marker=False,
                   tools=None, on_status=None, stream=False, on_chunk=None,
                   prior_messages=None):
        self.calls.append({
            "model": model_name,
            "tools": [t["name"] for t in (tools or [])],
            "user_prompt": user_prompt,
            "prior_len": len(prior_messages or []),
        })
        if on_status:
            on_status({"phase": "start", "model": model_name})
            on_status({"phase": "success", "model": model_name})
        tool_calls = []
        if model_name == "main" and self.tool_script and tools and not self._tool_issued:
            tool_calls = [self.tool_script[0]]
            self._tool_issued = True
        if model_name == "main":
            idx = min(len([c for c in self.calls if c["model"] == "main"]) - 1,
                      len(self.main_texts) - 1)
            content = self.main_texts[idx]
        else:
            content = self.vp_text
        return {
            "content": content,
            "tool_calls": tool_calls,
            "truncated_tool_call": None,
            "finish_reason": "stop",
            "tokens_used": len(content) // 4 or 1,
            "latency_ms": 1,
        }

    async def stream_call(self, model_name, system_prompt, user_prompt, temperature=0.5,
                          max_tokens=None, timeout=None, on_status=None, tools=None,
                          on_reasoning=None, prior_messages=None):
        result = await self.call(
            model_name, system_prompt, user_prompt, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout, skip_completion_marker=True,
            tools=tools, on_status=on_status, prior_messages=prior_messages,
        )
        for seg in re.findall(r".{1,8}", result.get("content") or ""):
            if seg:
                yield {"chunk_type": "content", "chunk": seg, "text": seg}
        yield result


# 剧本定义：name → (请求内容, 网关参数)
SCENARIOS: dict[str, dict[str, Any]] = {
    "s1": {
        "title": "纯文本一轮交付（快车道·无工具·跳三席）",
        "content": "查看一下卡片",
        "gateway": {"main_texts": ["根卡现状已核对，无需改动。"]},
    },
    "s2": {
        "title": "工具循环（快车道＋read_card，含埋点与续跳）",
        "content": "查看一下卡片",
        "gateway": {
            "main_texts": ["正在查证根卡…", "查证完成，根卡结构如上。"],
            "tool_script": [{"name": "read_card", "arguments": {"card_id": "card_001"}}],
        },
    },
    "s3": {
        "title": "三席后置＋统合交付（chain 路径·含 viewpoints 与 T14 拼接）",
        "content": "帮我分析一下根卡当前的定论是否需要收敛",
        "gateway": {
            "main_texts": ["已读完根卡与工具结果。", "综合三席意见，收敛为一条定论。"],
            "tool_script": [{"name": "read_card", "arguments": {"card_id": "card_001"}}],
        },
    },
}


# ---------------------------------------------------------------- 归一

_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2})?")
_STAMP_RE = re.compile(r"_\d{8}\d{6}(?:_\d+)?")
_HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")


def normalize_text(text: str) -> str:
    """文本归一：ISO 时间戳 → <ISO>；content_hash 类64 位十六进制 → <CONTENT_HASH>。

    content_hash 必须归一：它是对**含时间戳的全文**求值来的，同一份内容
    在不同运行天然得到不同哈希（实测两次 run 的 content_hash 不同）。
    """
    s = _ISO_RE.sub("<ISO>", text)
    s = re.sub(r"prompt_(card_\d+)_<ISO>", r"prompt_\1_<STAMP>", s)
    s = _HASH_RE.sub("<CONTENT_HASH>", s)
    return s


def normalize(obj: Any) -> Any:
    """递归归一：pair_id / ts / seq / ISO 时间戳 / 64 位哈希。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "pair_id" and isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v):
                out[k] = "<PAIR_ID>"
            elif k == "ts":
                out[k] = "<TS>"
            elif k == "seq" and isinstance(v, int):
                # 进程级单调计数器（非本请求内序号）——金样本只关心本请求的事件序
                out[k] = "<SEQ>"
            elif k == "sha256_normalized":
                out[k] = "<CONTENT_HASH>"
            else:
                out[k] = normalize(v)
        return out
    if isinstance(obj, list):
        return [normalize(x) for x in obj]
    if isinstance(obj, str):
        return normalize_text(obj)
    return obj


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 沙箱装配

def build_sandbox(tmp: Path, monkey: dict) -> dict[str, Path]:
    """复刻 tests/conftest.py 的 sandbox fixture（逐个常量对齐，不 import conftest）。"""
    from src.api import file_tools, model_service, prompt_log
    from src.storage import (
        canvas_store, card_store, data_snapshot, manuscript_store,
        session_store, work_store,
    )

    base = tmp / "app_home"
    works = base / "works"
    works.mkdir(parents=True)
    (base / "trading-workspace").mkdir()
    (base / "推演agent").mkdir()
    logs = base / "logs"
    logs.mkdir()

    for mod in (card_store, work_store, canvas_store, session_store, manuscript_store):
        monkey[mod] = mod.WORKS_DIR
        mod.WORKS_DIR = works
    monkey["ds_base"] = data_snapshot.BASE_DIR
    data_snapshot.BASE_DIR = base
    monkey["ds_snap"] = data_snapshot.SNAPSHOT_ROOT
    data_snapshot.SNAPSHOT_ROOT = tmp / "snapshots"
    monkey["ds_fp"] = data_snapshot._FINGERPRINT_FILE
    data_snapshot._FINGERPRINT_FILE = base / ".snapshot_fingerprints.json"
    monkey["pl_logs"] = prompt_log.LOGS_DIR
    prompt_log.LOGS_DIR = logs
    monkey["ft_base"] = file_tools.BASE_DIR
    file_tools.BASE_DIR = base
    monkey["ft_roots"] = file_tools.ALLOWED_ROOTS
    file_tools.ALLOWED_ROOTS = ((base / "trading-workspace").resolve(),
                                (base / "推演agent").resolve())
    monkey["ft_backup"] = file_tools.BACKUP_DIR
    file_tools.BACKUP_DIR = base / "推演agent" / "_backups"
    monkey["gw"] = model_service._gateway
    model_service._gateway = None
    return {"base": base, "works": works, "logs": logs}


def _restore_all(restore: dict[str, Any]) -> None:
    """还原 build_sandbox 改过的全部模块常量（显式逐项，避免键混淆）。"""
    from src.api import file_tools, prompt_log
    from src.storage import (
        canvas_store, card_store, data_snapshot, manuscript_store,
        session_store, work_store,
    )
    for mod in (card_store, work_store, canvas_store, session_store, manuscript_store):
        mod.WORKS_DIR = restore[mod]
    data_snapshot.BASE_DIR = restore["ds_base"]
    data_snapshot.SNAPSHOT_ROOT = restore["ds_snap"]
    data_snapshot._FINGERPRINT_FILE = restore["ds_fp"]
    prompt_log.LOGS_DIR = restore["pl_logs"]
    file_tools.BASE_DIR = restore["ft_base"]
    file_tools.ALLOWED_ROOTS = restore["ft_roots"]
    file_tools.BACKUP_DIR = restore["ft_backup"]


# ---------------------------------------------------------------- 单剧本执行

async def run_one(tag: str, sc: dict[str, Any], run_idx: int) -> dict[str, Any]:
    import httpx
    from fastapi import FastAPI

    from src.api import event_stream, model_service, routes

    tmp = REPO.parent / f"_t0009_golden_tmp_{tag}_{run_idx}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    restore: dict[str, Any] = {}
    sandbox = build_sandbox(tmp, restore)
    gw = ScriptedGateway(**sc["gateway"])
    model_service._gateway = gw

    # uuid4 定桩：pair_id 归一前的真实值也保持可复现（防日志文件名/事件序漂移）
    real_uuid4 = uuid.uuid4
    seq = {"n": 0}

    def fake_uuid4():
        seq["n"] += 1
        return uuid.UUID(int=seq["n"], version=4)

    routes.uuid.uuid4 = fake_uuid4

    app = FastAPI()
    app.include_router(routes.router)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://golden"
        ) as client:
            wid = (await client.post("/api/works", json={"title": f"金样本剧本{tag}"})).json()["work_id"]
            root = (await client.get(f"/api/works/{wid}/canvas")).json()["cards"][0]["card_id"]
            r = await client.post(f"/api/works/{wid}/cards/{root}/messages",
                                  json={"content": sc["content"]})
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") \
                else {"raw": r.text}
            status = r.status_code

            journal = event_stream.journal_of(wid, root)
            tokens = event_stream.token_journal_of(wid, root)
            cards_after = (await client.get(f"/api/works/{wid}/canvas")).json()["cards"]
            session = (await client.get(f"/api/works/{wid}/cards/{root}/session")).json()
    finally:
        # 还原：模块常量 + gateway + uuid4（拆分前抓样与拆分后回放必须同环境）
        routes.uuid.uuid4 = real_uuid4
        _restore_all(restore)
        model_service._gateway = restore["gw"]

    # ---- 三件套 ----
    # 1) SSE 事件流：事件类型序列 ＋ payload 逐字（归一 ts/pair_id/seq/hash）
    sse_lines = []
    for i, evt in enumerate(journal, start=1):
        sse_lines.append(
            f"[{i:03d}] {evt['type']} "
            f"{json.dumps(normalize(evt['data']), ensure_ascii=False, sort_keys=True)}"
        )
    for i, evt in enumerate(tokens, start=1):
        sse_lines.append(
            f"[TOK{i:03d}] {evt.get('kind', '')} {evt.get('type', '')} "
            f"{json.dumps(normalize(evt.get('data', {})), ensure_ascii=False, sort_keys=True)}"
        )
    sse_log = "\n".join(sse_lines) + "\n"

    # 2) 最终 body 全字段（归一）
    body_json = json.dumps(normalize(body), ensure_ascii=False, indent=2, sort_keys=True)

    # 3) 落盘副作用：卡文件路径 ＋ 内容 hash（内容先归一再hash）
    side = {"cards": [], "session": [], "card_count": len(cards_after)}
    for c in sorted(cards_after, key=lambda x: x["card_id"]):
        p = sandbox["works"] / wid / "cards" / f"{c['card_id']}.md"
        raw = p.read_text(encoding="utf-8") if p.exists() else ""
        side["cards"].append({
            "card_id": c["card_id"],
            "path": f"works/{wid}/cards/{c['card_id']}.md",
            "sha256_normalized": sha256_text(normalize_text(raw)),
        })
    sp = sandbox["works"] / wid / "sessions"
    if sp.exists():
        for p in sorted(sp.glob("*.md")):
            raw = p.read_text(encoding="utf-8")
            side["session"].append({
                "path": f"works/{wid}/sessions/{p.name}",
                "sha256_normalized": sha256_text(normalize_text(raw)),
            })
    side["session_messages"] = [
        {"role": m.get("role"), "len": len(m.get("content") or ""),
         "pair_id": "<PAIR_ID>" if m.get("pair_id") else None}
        for m in session
    ]
    side_json = json.dumps(normalize(side), ensure_ascii=False, indent=2, sort_keys=True)

    meta = {
        "tag": tag,
        "title": sc["title"],
        "request_content": sc["content"],
        "http_status": status,
        "model_calls": len(gw.calls),
        "main_calls": len([c for c in gw.calls if c["model"] == "main"]),
        "vp_calls": len([c for c in gw.calls if c["model"] == "vp1"]),
        "event_types": [e["type"] for e in journal],
        "event_count": len(journal),
        "token_event_count": len(tokens),
    }
    return {
        "meta": meta,
        "sse": sse_log,
        "body": body_json,
        "side": side_json,
        "tmp": str(tmp),
    }


# ---------------------------------------------------------------- 主流程

async def main() -> int:
    outdir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 \
        else REPO / "tests" / "_golden"
    outdir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict[str, Any]] = []
    unstable: list[str] = []

    for tag, sc in SCENARIOS.items():
        runs = []
        for i in (1, 2):
            print(f"[抓取] {tag} run{i} ...", flush=True)
            runs.append(await run_one(tag, sc, i))

        # 阶段一闸门：同剧本两轮三件套逐字节一致
        same_sse = runs[0]["sse"] == runs[1]["sse"]
        same_body = runs[0]["body"] == runs[1]["body"]
        same_side = runs[0]["side"] == runs[1]["side"]
        deterministic = same_sse and same_body and same_side
        if not deterministic:
            unstable.append(tag)
            for label, ok in (("sse", same_sse), ("body", same_body), ("side", same_side)):
                if not ok:
                    (outdir / f"_t0009_unstable_{tag}_{label}_run1.txt").write_text(
                        runs[0][label], encoding="utf-8")
                    (outdir / f"_t0009_unstable_{tag}_{label}_run2.txt").write_text(
                        runs[1][label], encoding="utf-8")

        r = runs[0]
        (outdir / f"{tag}_sse.log").write_text(r["sse"], encoding="utf-8")
        (outdir / f"{tag}_body.json").write_text(r["body"], encoding="utf-8")
        (outdir / f"{tag}_side_effects.json").write_text(r["side"], encoding="utf-8")
        (outdir / f"{tag}_meta.json").write_text(
            json.dumps(r["meta"], ensure_ascii=False, indent=2), encoding="utf-8")

        manifest.append({
            "tag": tag,
            "title": sc["title"],
            "deterministic_2runs": deterministic,
            "http_status": r["meta"]["http_status"],
            "model_calls": r["meta"]["model_calls"],
            "event_count": r["meta"]["event_count"],
            "event_types": r["meta"]["event_types"],
            "files": {
                "sse": f"{tag}_sse.log",
                "body": f"{tag}_body.json",
                "side_effects": f"{tag}_side_effects.json",
            },
            "sha256": {
                "sse": sha256_text(r["sse"]),
                "body": sha256_text(r["body"]),
                "side_effects": sha256_text(r["side"]),
            },
        })
        print(f"[抓取] {tag}: 两轮一致={deterministic} "
              f"events={r['meta']['event_count']} calls={r['meta']['model_calls']}", flush=True)

    (outdir / "_t0009_golden_manifest.json").write_text(
        json.dumps({"normalize_note": NORMALIZE_NOTE, "items": manifest},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 阶段一闸门 ===")
    print(f"不稳定剧本 = {unstable if unstable else '（无，三剧本两轮均逐字节一致）'}")
    return 1 if unstable else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))