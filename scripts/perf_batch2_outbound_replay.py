# -*- coding: utf-8 -*-
"""用冻结副本回放指定原话与关键工具回合，捕获实际适配器出站。

每组从同一只读基线复制。模型回包、057 子卡外部首跑及 007 的状态更新时点
由回放控制，正式消息入口、卡工具、存储以及网关仍走真实链路。
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from outbound_probe import (CHILD_MARK, LIST_MARK, OutboundRecorder,  # noqa: E402
                            frame_finish, frame_text, frame_tool, sse)

WORK = "work_073"
OUT_DIR = ROOT / "logs" / "batch2"
ISO_ROOT = OUT_DIR / "iso"

SAMPLES = (
    ("card_007", "094329", "card_007", "jurisdiction"),
    ("card_057", "101337", "card_057", "card"),
    ("card_009", "094647", "card_007", "card"),
)


# ============================================================
# 固定副本：生产库只被**读**一次（拷贝），之后全部读写都在副本里
# ============================================================

#: ``works/{wid}/`` 下**装配与回合要读**的目录/文件（其余产物目录不搬：
#: 产物树动辄上 GB，回放只需要会话/事件/规则/模型表/画布）。
COPY_WORK_ENTRIES = ("sessions", "events", "memory", "rules.md",
                     "model_config_table.json", "canvas.json", "canvas.dirty",
                     "work.json", "_ops")

#: 复制到副本里的**卡面表**（其余表不进副本：``work_events`` 三百万行＝5 GB、
#: ``card_history``/``card_revisions`` 是历史，facts/harness 库与装配无关）。
COPY_DB_TABLES = ("cards", "meta", "card_parent", "card_links", "card_relations",
                   "shadows", "memory", "memory_nodes", "context_board",
                   "function_profiles", "function_deployments", "role_profiles",
                   "role_verdicts", "messages", "session_blocks")

#: 只搬这几张卡的会话与事件日志（其余卡的全量日志动辄几个 GB）。
SAMPLE_CARDS = tuple(spec[0] for spec in SAMPLES)


def _copy_db_subset(src_db: Path, dst_db: Path) -> None:
    """把生产库的**卡面表**拷进副本库（不整拷：生产库十几 GB）。"""
    import sqlite3
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(dst_db, uri=True)
    try:
        dst.execute("ATTACH DATABASE ? AS source", (f"{src_db.resolve().as_uri()}?mode=ro",))
        dst.execute("PRAGMA main.synchronous=OFF")
        dst.execute("BEGIN")
        tables = list(dst.execute(
            "SELECT name, sql FROM source.sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"))
        names = {t[0] for t in tables}
        # ① **所有表都建空壳**：读路径可能碰到任何一张表（缺表＝
        #    「只读路径出现写操作」这类硬错）——空表不花钱。
        for name, sql in tables:
            if sql:
                dst.execute(sql)
        # ② 只搬**装配要读**的表的行
        for name in COPY_DB_TABLES:
            if name not in names:
                continue
            dst.execute(f"INSERT INTO main.{name} SELECT * FROM source.{name}")
        dst.commit()
    finally:
        dst.close()


def make_fixed_copy(work_id: str) -> Path:
    """把该 work 的**卡面**与样本卡的日志拷到**固定副本**目录（可复查、可重复用）。

    ⚠ 生产库在这之后**零读写**（包括 ``take_pending_inject_sync`` 那种
    「取走并清空」的写）——上一版就是在这里出的事。
    生产 work_073 的库有 15 GB（facts.db-wal 就 9.6 GB）、事件日志 4.2 GB ⇒
    **整拷不现实**：这里只搬卡面表 ＋ 三张样本卡的会话/事件日志。
    """
    ISO_ROOT.mkdir(parents=True, exist_ok=True)
    iso = Path(tempfile.mkdtemp(prefix=f"{work_id}-baseline-", dir=ISO_ROOT))
    (iso / "works_data" / work_id).mkdir(parents=True, exist_ok=True)
    (iso / "works" / work_id).mkdir(parents=True, exist_ok=True)
    src_db = ROOT / "works_data" / work_id / "cards.db"
    _copy_db_subset(src_db, iso / "works_data" / work_id / "cards.db")
    src_ws = ROOT / "works" / work_id
    if src_ws.is_dir():
        # ⚠ Windows 保留设备名（nul/aux/con…）在盘上是真文件 ⇒ 拷贝会炸；
        #   跳过它们（回放不需要）。
        ignore = shutil.ignore_patterns("nul", "aux", "con", "prn",
                                        "com1", "com2", "lpt1", "lpt2")
        for name in COPY_WORK_ENTRIES:
            p = src_ws / name
            try:
                if name == "events":
                    # 只搬样本卡的日志（其余卡的全量日志几个 GB）
                    dst = iso / "works" / work_id / "events"
                    dst.mkdir(parents=True, exist_ok=True)
                    for card in SAMPLE_CARDS:
                        ev = p / f"{card}.jsonl"
                        if ev.exists():
                            shutil.copy2(ev, dst / f"{card}.jsonl")
                    continue
                if name == "sessions":
                    dst = iso / "works" / work_id / "sessions"
                    dst.mkdir(parents=True, exist_ok=True)
                    for card in SAMPLE_CARDS:
                        for sf in p.glob(f"*{card}*"):
                            if sf.is_file():
                                shutil.copy2(sf, dst / sf.name)
                    continue
                if p.is_dir():
                    shutil.copytree(p, iso / "works" / work_id / name,
                                    dirs_exist_ok=True, ignore=ignore)
                elif p.is_file():
                    shutil.copy2(p, iso / "works" / work_id / name)
            except (OSError, shutil.Error) as exc:  # noqa: BLE001 拷贝失败不阻断
                print(f"  （副本跳过 {name}：{exc}）")
    return iso


def point_roots_at(iso: Path, replay_logs: Path | None = None) -> None:
    """存储、观测和提示日志都在本组副本。"""
    from src.api import prompt_log, tool_receipt
    from src.infra import fact_store, intervention_store, observe
    from src.storage import (canvas_store, card_db, card_store, manuscript_store,
                             session_facade, session_store, work_store)
    works = iso / "works"
    for mod in (card_store, work_store, canvas_store, session_store,
                manuscript_store):
        mod.WORKS_DIR = works
    prompt_log.LOGS_DIR = iso / "logs"
    # 观测异步队列在每组退出后仍可能写入，整个回放共用独立日志目录。
    observe.LOGS_DIR = replay_logs or (iso / "logs")
    tool_receipt._LEDGER_PATH = iso / "logs" / "tool_dispatch_ledger.jsonl"
    card_db.reset_caches()
    session_facade.reset_cache()
    fact_store.reset(fact_store.FactStore(works_dir=works))
    intervention_store.reset(intervention_store.InterventionStore(works_dir=works))


def _sample(iso: Path, card_id: str, stamp: str, target: str, scope: str) -> dict:
    """从原始提示日志和同 pair 的工具事件取回放样本。"""
    import sqlite3
    from src.api import doc_locator

    prompt = ROOT / "logs" / f"prompt_{card_id}_20261120{stamp}.json"
    saved = json.loads(prompt.read_bytes())
    pair_id = saved["pair_id"]
    trace_file = iso / "works" / WORK / "events" / f"{card_id}.jsonl"
    calls = []
    with trace_file.open(encoding="utf-8") as handle:
        for line in handle:
            if pair_id not in line:
                continue
            event = json.loads(line)
            if event.get("type") == "tool_call" and event.get("run_id") == pair_id:
                data = event["data"]
                calls.append((data["tool"], data["arguments"]))
    extra = None
    if card_id == "card_057":
        calls = [call for call in calls if call[0] == "create_worker_card"]
    elif card_id == "card_007":
        calls = calls[:2]  # 原轮 get_goal、紧接的 S9 现状回读。
    else:
        calls = [call for call in calls if call[0] == "read_card"
                 and call[1].get("card_id") in ("card_007", "card_564")][:2]
        db_path = iso / "works_data" / WORK / "cards.db"
        db = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            req = db.execute("SELECT requirements FROM cards WHERE card_id=?", (target,)).fetchone()[0]
        finally:
            db.close()
        clause = doc_locator.locate_clauses(req, ["A3"])[0]
        extra = {"card_id": target, "block": "需求文档",
                 "para_start": clause.start, "para_end": clause.end}
        calls.append(("read_card", extra))
    expected_names = {"card_057": ["create_worker_card"], "card_007": ["get_goal", "read_card"],
                      "card_009": ["read_card"] * 3}
    assert [name for name, _ in calls] == expected_names[card_id], (card_id, calls)
    return {"card_id": card_id, "pair_id": pair_id, "prompt_file": prompt.name,
            "user": saved["user_prompt"], "model": saved["model_requested"],
            "snapshot_time": datetime.fromtimestamp((iso / "works_data" / WORK / "cards.db")
                                                    .stat().st_mtime).astimezone(),
            "task": {"task_id": pair_id, "target": target, "scope": scope,
                     **({"accept": "A3"} if extra else {})},
            "calls": calls, "extra_read": extra}


STATUS_MARK = "〔批二复验：S9 状态在首跳后更新〕"
CALL_PREFIX = "batch2-"


def _reply(body: dict, call: tuple[str, dict] | None, turn: int) -> bytes:
    model, call_id = body["model"], f"{CALL_PREFIX}{turn}"
    closing = "隔离回放结束。"
    if "input" not in body:
        if call is None:
            return sse(frame_text(closing, model), frame_finish("stop", model))
        name, args = call
        return sse(frame_tool(call_id, name, args, model), frame_finish("tool_calls", model))
    if call is None:
        return sse({"type": "response.output_text.delta", "delta": closing},
                   {"type": "response.completed"})
    name, args = call
    return sse({"type": "response.output_item.added",
                "item": {"type": "function_call", "id": call_id, "call_id": call_id, "name": name}},
               {"type": "response.function_call_arguments.done", "item_id": call_id,
                "arguments": json.dumps(args, ensure_ascii=False)},
               {"type": "response.completed"})


def _messages(body: dict) -> list[dict]:
    return body.get("messages") or body.get("input") or []


def _outputs(body: dict, pair_id: str) -> dict[str, dict]:
    prefix = f"call_{pair_id[:8]}_"
    paired_calls = set()
    results = {}
    for msg in _messages(body):
        if msg.get("type") == "function_call":
            paired_calls.add(msg["call_id"])
        paired_calls.update(tc["id"] for tc in msg.get("tool_calls") or [])
        if msg.get("role") == "tool" or msg.get("type") == "function_call_output":
            call_id = msg.get("tool_call_id") or msg.get("call_id") or ""
            if call_id.startswith(prefix):
                assert call_id in paired_calls, call_id
                content = msg.get("output") or msg["content"]
                if content.startswith("[tool_error code="):
                    code = content.split("]", 1)[0].removeprefix("[tool_error code=")
                    results[call_id] = {"ok": False, "error": {"code": code}}
                else:
                    results[call_id] = {"ok": True, "data": json.loads(content)}
    return results


def _wire_facts(req: dict, user: str, delivery: str) -> dict:
    messages = _messages(req["body"])
    system = next(m["content"] for m in messages if m.get("role") == "system")
    pos = max(i for i, m in enumerate(messages)
              if m.get("role") == "user" and m.get("content") == user)
    tail = [m["content"] for m in messages[pos + 1:]
            if m.get("role") == "assistant" and isinstance(m.get("content"), str)]
    facts = {mark: [s for s in tail if s.startswith(mark)] for mark in (LIST_MARK, CHILD_MARK)}
    counts = [len(facts[mark]) for mark in (LIST_MARK, CHILD_MARK)]
    assert counts == ([0, 0] if delivery == "system" else [1, 1]), counts
    dep_start = system.find("## 依赖关联")
    dep_end = system.find("\n## ", dep_start + 1) if dep_start >= 0 else -1
    dependency = system[dep_start:dep_end if dep_end >= 0 else None] if dep_start >= 0 else ""
    def region_bytes(mark: str, title: str) -> int:
        if delivery == "tail":
            return len(facts[mark][0].encode("utf-8"))
        offset = system.find(title)
        if offset < 0:
            return 0
        end = system.find("\n## ", offset + 1)
        return len(system[offset:end if end >= 0 else None].encode("utf-8"))

    return {"list_count": counts[0], "child_count": counts[1],
            "list_bytes": region_bytes(LIST_MARK, "## 卡片清单"),
            "child_bytes": region_bytes(CHILD_MARK, "## 直接子卡"),
            "dep_bytes": len(dependency.encode("utf-8")),
            "relation_edges": [s for s in dependency.splitlines()
                               if s.startswith(("- 我依赖/关联：", "- 依赖/关联我的："))],
            "system": system, "tail": facts}


async def _run_group(work_id: str, case: dict, delivery: str, scope_on: bool, bind: bool) -> dict:
    import httpx
    from src.api import card_tools, context_assembler, context_regions as cr, routes
    from src.goal import driver as goal_driver
    from src.model_gateway.llm_siwc.adapter import _TokenStore
    from src.storage import card_store, model_override

    card_id, calls = case["card_id"], case["calls"]
    pair_id = ""
    send_count = 0
    retry = card_id == "card_057"
    response_errors = []

    class SnapshotClock(datetime):
        @classmethod
        def now(cls, tz=None):
            snapshot_time = case["snapshot_time"]
            if tz is None:
                return snapshot_time.replace(tzinfo=None)
            return snapshot_time.astimezone(tz)

    def respond(index: int, body: dict):
        nonlocal send_count
        if retry and index == 0:
            return httpx.ConnectError("isolated first attempt")
        try:
            returned = _outputs(body, pair_id)
            assert len(returned) == send_count, (card_id, index, send_count, list(returned))
        except (AssertionError, ValueError) as exc:
            response_errors.append(exc)
            return _reply(body, None, send_count)
        upcoming = calls[send_count] if send_count < len(calls) else None
        result = _reply(body, upcoming, send_count)
        send_count += 1
        return result

    rec = OutboundRecorder(respond)

    async def send(client, request, **kwargs):
        http_response = await rec.send(client, request, **kwargs)
        if card_id == "card_007" and len(rec.requests) == 1:
            child = await card_store.read_card(work_id, "card_057")
            await card_store.update_card(
                work_id, "card_057",
                {"status_block": STATUS_MARK + "\n" + child["body"]["status_block"],
                 "goal_state": "卡住了"}, "隔离复验写入")
        return http_response

    run_message = routes._run_message
    resolve_scope = context_assembler._resolve_task_scope
    emit = routes.event_stream.emit
    child_launches = []
    scopes = []
    receipts = []

    def capture_event(wid, cid, kind, data=None, **kwargs):
        nonlocal pair_id
        if wid == work_id and cid == card_id:
            if kind == "run_start":
                pair_id = str(data["pair_id"])
            elif kind == "tool_result":
                receipts.append(data)
        return emit(wid, cid, kind, data, **kwargs)

    def capture_scope(*args, **kwargs):
        scope = resolve_scope(*args, **kwargs)
        scopes.append(scope)
        return scope

    async def run_only_sample(wid, cid, content, *args, **kwargs):
        if cid == card_id:
            return await run_message(wid, cid, content, *args, **kwargs)
        # 057 的 handler 真实建卡、安排首跑，此处接住子卡任务。
        child_launches.append((cid, content))

    async def access_token(_self):
        return "isolated-replay-token"

    model_override.put_row(work_id, card_id, {"model": case["model"]}, src="manual")
    patched = []
    for spec in cr.REGION_TABLE:
        if spec["key"] in ("card_list", "child_status"):
            patched.append((spec, spec["delivery"], spec["render"]))
            spec["delivery"] = delivery
            if delivery == "system":
                def as_system(ctx, render=spec["render"]):
                    rows = render(ctx)
                    return rows[0]["content"].split("\n", 1)[-1] if rows else ""
                spec["render"] = as_system
    try:
        with (patch.dict(os.environ, {"CONTEXT_SCOPE": "1" if scope_on else "0"}),
              patch.object(httpx.AsyncClient, "send", send),
              patch.object(card_tools, "datetime", SnapshotClock),
              patch.object(_TokenStore, "access_token", access_token),
              patch.object(routes, "_run_message", run_only_sample),
              patch.object(routes.event_stream, "emit", capture_event),
              patch.object(context_assembler, "_resolve_task_scope", capture_scope),
              patch.object(goal_driver, "notify_idle"), patch.object(goal_driver, "notify_parent_idle")):
            await routes.post_work_message(work_id, card_id, routes.PostMessageRequest(
                content=case["user"], task=case["task"] if bind else None))
    finally:
        for spec, previous_delivery, previous_render in patched:
            spec["delivery"], spec["render"] = previous_delivery, previous_render

    if response_errors:
        raise response_errors[0]
    assert len(rec.requests) == len(calls) + 1 + retry, (card_id, len(rec.requests))
    expected_mode = "unknown"
    if scope_on and bind:
        expected_mode = "global" if case["task"]["scope"] == "jurisdiction" else "single"
    assert {scope.mode for scope in scopes} == {expected_mode}, scopes
    if retry:
        assert rec.requests[0]["raw"] == rec.requests[1]["raw"]
    facts = [_wire_facts(r, case["user"], delivery) for r in rec.requests]
    results = list(_outputs(rec.requests[-1]["body"], pair_id).values())
    assert [r["tool"] for r in receipts] == [name for name, _ in calls], receipts
    assert [r["ok"] for r in receipts] == [r["ok"] for r in results], receipts
    data = [entry.get("data") or {} for entry in results]
    business = {"tools": [{"name": name, "ok": result["ok"],
                           "error": (result.get("error") or {}).get("code")}
                          for (name, _), result in zip(calls, results, strict=True)]}
    if card_id == "card_057":
        assert results[0]["ok"], results[0]
        new_id = data[0]["card_id"]
        worker = await card_store.read_card(work_id, new_id)
        assert worker["meta"]["parent"] == card_id
        assert worker["body"]["mission"].strip() == calls[0][1]["task"].strip()
        assert len(child_launches) == 1 and child_launches[0][0] == new_id, child_launches
        business.update(created_card=new_id, original_task_preserved=True)
    elif card_id == "card_007":
        assert all(entry["ok"] for entry in results), results
        assert STATUS_MARK in data[-1]["content"]
        visible = [(row["system"] + str(row["tail"])).count(STATUS_MARK) for row in facts]
        if delivery == "tail":
            assert visible == [0, 1, 1], visible
        business["status_snapshot_counts"] = visible
    else:
        assert business["tools"][0]["error"] == "denied", business
        assert all(entry["ok"] for entry in results[1:]), business
        extra = case["extra_read"]
        start, end = extra["para_start"], extra["para_end"]
        target = await card_store.read_card(work_id, extra["card_id"])
        expected = "\n".join(target["body"]["requirements"].split("\n")[start - 1:end])
        assert data[-1]["content"] == expected
        if scope_on and bind:
            hint = f'para_start={start}, para_end={end}'
            assert hint in facts[0]["system"], hint
        business["readback"] = {"card_id": extra["card_id"], "lines": [start, end],
                                "matched_original": True}

    payloads = []
    for i, (req, row) in enumerate(zip(rec.requests, facts, strict=True)):
        payloads.append({k: v for k, v in row.items() if k not in ("system", "tail", "relation_edges")}
                        | {"hop": max(0, i - retry), "attempt": 2 if retry and i == 1 else 1,
                           "endpoint": req["url"], "seat": case["model"], "model": req["body"]["model"],
                           "bytes": req["bytes"], "messages": len(_messages(req["body"])),
                           "raw": req["raw"].decode("utf-8")})
    return {"pair_id": pair_id, "requests": len(payloads),
            "total_bytes": sum(r["bytes"] for r in payloads), "per_request": payloads,
            "scope_mode": scopes[0].mode, "scope_basis": scopes[0].basis,
            "relation_edges": facts[0]["relation_edges"], "business": business}


def main() -> int:
    from src.storage import card_db
    from src.infra import fact_store, intervention_store

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", nargs="?", default=WORK, choices=(WORK,))
    parser.add_argument("--baseline", type=Path, help="复用本脚本已经冻结的基线目录")
    args = parser.parse_args()
    for env in ("OPENDESIGN_API_KEY", "VOLCENGINE_CODING_API_KEY",
                "ZHIPU_CODING_API_KEY", "MINIMAX_API_KEY", "OPENCODE_API_KEY", "ARCEE_API_KEY"):
        os.environ[env] = "isolated-replay"
    if args.baseline:
        iso = args.baseline.resolve()
        if not iso.is_relative_to(ISO_ROOT.resolve()) or not iso.name.startswith(f"{WORK}-baseline-"):
            parser.error("--baseline 须指向本脚本生成的基线目录")
    else:
        iso = make_fixed_copy(args.work)
    print(f"冻结基线：{iso}", flush=True)
    replay_logs = Path(tempfile.mkdtemp(prefix="replay-logs-", dir=ISO_ROOT))
    rows = []
    for spec in SAMPLES:
        case = _sample(iso, *spec)
        row = {k: case[k] for k in ("card_id", "prompt_file", "pair_id", "user", "calls", "extra_read")}
        for name, delivery, scope_on, bind in (
            ("A_batch1_only", "system", False, True),
            ("B_tail_only", "tail", False, True),
            ("C_tail_and_narrowed", "tail", True, True),
            ("C_unbound_full_material", "tail", True, False),
        ):
            with tempfile.TemporaryDirectory(prefix="group-", dir=ISO_ROOT,
                                             ignore_cleanup_errors=True) as path:
                staging = Path(path)
                shutil.copytree(iso, staging, dirs_exist_ok=True)
                point_roots_at(staging, replay_logs)
                try:
                    row[name] = asyncio.run(_run_group(WORK, case, delivery, scope_on, bind))
                finally:
                    card_db.reset_caches()
                    fact_store.store().close()
                    fact_store.reset(fact_store.FactStore(works_dir=iso / "works"))
                    intervention_store.reset(intervention_store.InterventionStore(works_dir=iso / "works"))
                    gc.collect()
            result = row[name]
            print(case["card_id"], name, f'{result["requests"]} 次 / {result["total_bytes"]:,} B', flush=True)
        a, b, c, unbound = (row[k] for k in ("A_batch1_only", "B_tail_only",
                                            "C_tail_and_narrowed", "C_unbound_full_material"))
        assert a["relation_edges"] == b["relation_edges"] == c["relation_edges"] == unbound["relation_edges"]
        b_first = b["per_request"][0]["raw"]
        unbound_first = unbound["per_request"][0]["raw"]
        if b_first != unbound_first:
            index = next((i for i, (x, y) in enumerate(zip(b_first, unbound_first)) if x != y),
                         min(len(b_first), len(unbound_first)))
            raise AssertionError((case["card_id"], index, b_first[index - 24:index + 48],
                                  unbound_first[index - 24:index + 48]))
        row["tail_to_narrowed_delta_bytes"] = c["total_bytes"] - b["total_bytes"]
        rows.append(row)

    out = OUT_DIR / "batch2_outbound.json"
    pending = out.with_suffix(".json.tmp")
    pending.write_text(json.dumps({"work_id": WORK, "iso_copy": str(iso), "rows": rows},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(out)
    print(f"证据：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
