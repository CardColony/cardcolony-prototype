# -*- coding: utf-8 -*-
"""[多进程拆分 2026-11-06 · 用户令「加进程，把不同的任务分拆到不同的进程里，我cpu根本跑不满」
→「不止切双进程，尽可能多拆进程」]

单进程时一切挤一个 GIL——双环分流只是分两条队，CPU 还是那个核。
``ECO_ROLE`` 把一个代码库拆成多进程拓扑（同一 app、零接口变更）：

- ``all``（缺省）——单进程全量：行为与拆分前逐字一致，测试/开发照跑；
- ``web``——**服务进程**（5700）：白名单只读 GET 本地答（读文件/DB，
  无锁读本来就跨进程安全）；写/唤起/跑卡**按卡寻址**转属主 runner；
  runner 进程内状态读口（running/pulse/observe）扇入全 runner 合并；
  工作级 SSE 向全 runner 各开一条流扇入。
- ``runner``——**执行进程**（5701+INDEX，只绑回环）：完整 app。卡按
  **子树根**稳定哈希分片（同 ``loops._branch_key``，父子必同片⇒单卡
  单写者天然成立）。``_run_message``/``deliver_now`` 入口自查分片，
  跨片调用透明 POST 到属主 ``/ctl/*``。驱动器/续跑只在 INDEX=0 跑
  （goal install() 除外——监听是本进程内的，每个 runner 都装）。

环境：
  ECO_ROLE=all|web|runner        ECO_RUNNER_COUNT=K（缺省 1）
  ECO_RUNNER_INDEX=i             ECO_RUNNER_BASE_PORT=5701
  ECO_RUNNER_URLS=逗号分隔覆盖    ECO_RUNNER_URL=单目标兼容（COUNT=1 时）

推式链路语义（用户令「web必须靠事件流推」）：活事件流是 runner 扇出
→ runner SSE → web 中继 → 浏览器 SSE，端到端全推；web 不轮询事件文件。
历史/现状页（nav/session/timeline）是盘上数据，web 本地直读。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

#: web 进程本地答的 GET 模板（{} ＝一段路径参数）——全部是「读文件/DB」
#: 的无锁读口。凡不在此列的请求一律转发。
_WEB_LOCAL_GET_TPLS: tuple[str, ...] = (
    "/",
    "/settings",
    "/work/{}",
    "/work/{}/card/{}",
    "/api/nav",
    "/api/nav/children",
    "/api/nav/version",
    "/api/health",
    "/api/works",
    "/api/works/{}/canvas",
    "/api/works/{}/cards",
    "/api/works/{}/cards/{}",
    "/api/works/{}/cards/{}/timeline",
    "/api/card/{}/timeline",
    "/api/works/{}/cards/{}/session",
    "/api/works/{}/card-messages/unread",
    "/api/works/{}/cards/{}/exec-backend",
    "/api/works/{}/cards/{}/review-status",
    "/api/works/{}/rule",
    "/api/settings/model-overrides/{}",
    "/api/settings/models",
)

#: runner 进程内状态读口——web 扇入全 runner 合并（列表 concat 去重，
#: dict 逐键：list 并、标量取首个非空）。
_FANIN_GET_TPLS: tuple[str, ...] = (
    "/api/nav/running",
    "/api/observe/calls",
    "/api/works/{}/pulse",
)

#: 工作级 SSE——每 runner 都可能发帧，web 向全部开流扇入。
_FANIN_SSE_TPLS: tuple[str, ...] = (
    "/api/works/{}/events",
)


def _tpl_re(t: str) -> "re.Pattern":
    return re.compile("^" + re.escape(t).replace(
        re.escape("{}"), r"([^/]+)") + "$")


_LOCAL_RES = [_tpl_re(t) for t in _WEB_LOCAL_GET_TPLS]
_FANIN_GET_RES = [_tpl_re(t) for t in _FANIN_GET_TPLS]
_FANIN_SSE_RES = [_tpl_re(t) for t in _FANIN_SSE_TPLS]

#: 路径里抽 work/card（卡级口）。/api/card/{c}/* 只带卡不带 work——
#: 那类全是白名单本地口，到不了转发层，这里只管 works 形态。
_CARD_PATH_RE = re.compile(r"^/api/works/([^/]+)/cards/([^/]+)(?:/|$)")

_ROLE = str(os.environ.get("ECO_ROLE") or "all").strip().lower()
_RUNNER_COUNT = max(1, int(os.environ.get("ECO_RUNNER_COUNT") or 1))
_RUNNER_INDEX = max(0, int(os.environ.get("ECO_RUNNER_INDEX") or 0))
_BASE_PORT = int(os.environ.get("ECO_RUNNER_BASE_PORT") or 5701)
_URLS = [u.rstrip("/") for u in
         str(os.environ.get("ECO_RUNNER_URLS") or "").split(",") if u.strip()]
if not _URLS:
    _single = str(os.environ.get("ECO_RUNNER_URL") or "").strip()
    _URLS = ([_single] if _single else
             [f"http://127.0.0.1:{_BASE_PORT + i}"
              for i in range(_RUNNER_COUNT)])


def role() -> str:
    return _ROLE


def is_web() -> bool:
    return _ROLE == "web"


def is_runner() -> bool:
    return _ROLE == "runner"


def runner_side() -> bool:
    """该进程要不要跑卡域活（驱动器/回合/唤起）：all 与 runner 都跑，web 不跑。"""
    return _ROLE in ("all", "runner")


def runner_count() -> int:
    return _RUNNER_COUNT


def runner_index() -> int:
    return _RUNNER_INDEX if _ROLE == "runner" else 0


def is_primary_runner() -> bool:
    """驱动器/续跑等「全库一份」的 startup 钩子只在 runner-0 跑。"""
    return _ROLE == "all" or (_ROLE == "runner" and _RUNNER_INDEX == 0)


def runner_url(i: int) -> str:
    return _URLS[min(max(0, i), len(_URLS) - 1)]


def _is_local_get(method: str, path: str) -> bool:
    if method not in ("GET", "HEAD", "OPTIONS"):
        return False
    return any(rx.match(path) for rx in _LOCAL_RES)


#: 静态资源/页面壳——浏览器壳不走 API 路由，web 一律本地供。
_LOCAL_PREFIXES = ("/static/", "/favicon")


# ---------------------------------------------------------------
# 卡分片：子树根稳定哈希（跨进程必须确定性 ⇒ 不用内建 hash()）
# ---------------------------------------------------------------

def _branch_key(work_id: str, card_id: str) -> str:
    """复用 loops 的子树根求法（meta parent 链，创建后不可变）。"""
    try:
        from . import loops as _loops
        return _loops._branch_key(work_id, card_id)
    except Exception:  # noqa: BLE001
        return str(card_id)


def owner_index_of(work_id: str, card_id: str) -> int:
    """卡的属主 runner 序号。单 runner/串行模式/无卡 ⇒ 0。

    串行模式语义＝一次一卡：跨进程无法共用回合锁，全部归 0 号保语义。
    """
    if _RUNNER_COUNT <= 1 or not card_id:
        return 0
    try:
        from ..api import serial_gate as _sg
        if _sg.is_serial():
            return 0
    except Exception:  # noqa: BLE001
        pass
    bk = _branch_key(work_id, card_id) or str(card_id)
    h = int(hashlib.md5(bk.encode("utf-8")).hexdigest()[:8], 16)
    return h % _RUNNER_COUNT


def _pick_runner(path: str) -> str:
    """web 路由：路径带卡 ⇒ 属主 runner；不带卡 ⇒ 0 号（协调者）。"""
    m = _CARD_PATH_RE.match(path)
    if m:
        return runner_url(owner_index_of(m.group(1), m.group(2)))
    return runner_url(0)


def _fanin_get(path: str) -> bool:
    return any(rx.match(path) for rx in _FANIN_GET_RES)


def _fanin_sse(path: str) -> "re.Match | None":
    for rx in _FANIN_SSE_RES:
        m = rx.match(path)
        if m:
            return m
    return None


def install_web_proxy(app: Any) -> None:
    """web 角色专用：中间件路由——本地口放行，扇入口合并，其余按卡转发。

    放在路由**之前**（ASGI 中间件）：非本地请求根本不进 FastAPI 路由表。
    """
    if not is_web():
        return

    @app.middleware("http")
    async def _dispatch(request: Request, call_next):  # noqa: ANN001
        path = request.url.path
        if request.method in ("GET", "HEAD", "OPTIONS"):
            m = _fanin_sse(path)
            if m:
                return await _fanin_sse_stream(m.group(1),
                                               str(request.url.query))
            if _fanin_get(path):
                return await _fanin_get_merge(path, str(request.url.query))
        if (path.startswith(_LOCAL_PREFIXES)
                or _is_local_get(request.method, path)):
            return await call_next(request)
        return await _forward(request)


async def _forward(request: Request, base: str = "") -> Response:
    """把整个请求原样转给属主 runner，响应流式转回（SSE 直通：推链路不断）。"""
    url = (base or _pick_runner(request.url.path)) + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "connection",
                             "transfer-encoding")
    }
    client = httpx.AsyncClient(timeout=None)
    upstream = client.build_request(
        request.method, url, content=body, headers=headers)
    try:
        resp = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        return Response(
            f"runner 不可达（{url}）：{exc}", status_code=502,
            media_type="text/plain; charset=utf-8")

    async def _relay():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        _relay(),
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items()
                 if k.lower() not in ("transfer-encoding", "connection",
                                      "content-length")},
        media_type=resp.headers.get("content-type"),
    )


async def _fanin_get_merge(path: str, query: str) -> Response:
    """GET 扇入：并发问全部 runner，JSON 合并（list 去重并、dict 逐键并）。"""
    url = lambda i: runner_url(i) + path + (("?" + query) if query else "")
    async with httpx.AsyncClient(timeout=30) as client:
        results = await asyncio.gather(
            *(client.get(url(i)) for i in range(_RUNNER_COUNT)),
            return_exceptions=True)
    payloads = [r.json() for r in results
                if isinstance(r, httpx.Response)
                and r.status_code == 200
                and "application/json" in r.headers.get("content-type", "")]
    if not payloads:
        return Response("全部 runner 不可达", status_code=502,
                        media_type="text/plain; charset=utf-8")
    return Response(json.dumps(_merge_payloads(payloads),
                               ensure_ascii=False),
                    media_type="application/json")


def _merge_payloads(payloads: list) -> Any:
    if all(isinstance(p, list) for p in payloads):
        seen, out = set(), []
        for p in payloads:
            for item in p:
                k = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if k not in seen:
                    seen.add(k)
                    out.append(item)
        return out
    if all(isinstance(p, dict) for p in payloads):
        merged: dict = {}
        for p in payloads:
            for k, v in p.items():
                cur = merged.get(k)
                if isinstance(cur, list) and isinstance(v, list):
                    have = {json.dumps(x, sort_keys=True, ensure_ascii=False)
                            for x in cur}
                    cur.extend(x for x in v
                               if json.dumps(x, sort_keys=True,
                                             ensure_ascii=False) not in have)
                elif cur in (None, "", [], {}) and v not in (None, "", []):
                    merged[k] = v
        return merged
    return payloads[0]


async def _fanin_sse_stream(work_id: str, query: str) -> StreamingResponse:
    """工作级 SSE 扇入：向全部 runner 各开一条 /events 流，行级合并转发。"""
    url = lambda i: (runner_url(i)
                     + f"/api/works/{work_id}/events"
                     + (("?" + query) if query else ""))

    async def _merged():
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        clients: list[httpx.AsyncClient] = []

        async def _pump(i: int) -> None:
            c = httpx.AsyncClient(timeout=None)
            clients.append(c)
            try:
                async with c.stream("GET", url(i)) as resp:
                    async for line in resp.aiter_lines():
                        await q.put(line)
            except Exception:  # noqa: BLE001 单 runner 断流不影响其他
                pass

        tasks = [asyncio.create_task(_pump(i))
                 for i in range(_RUNNER_COUNT)]
        try:
            while True:
                line = await q.get()
                yield (line + "\n").encode("utf-8")
        finally:
            for t in tasks:
                t.cancel()
            for c in clients:
                await c.aclose()

    return StreamingResponse(_merged(), media_type="text/event-stream")


# ---------------------------------------------------------------
# runner 间调用：跨片执行转发（_run_message / deliver_now 入口自查）
# ---------------------------------------------------------------

async def forward_ctl(work_id: str, card_id: str, ctl_path: str,
                      payload: dict[str, Any]) -> dict[str, Any]:
    """把一个卡级执行请求投给属主 runner。返回对端 JSON 或 {"error": ...}。"""
    url = runner_url(owner_index_of(work_id, card_id)) + ctl_path
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            r = await client.post(url, json=payload)
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return {"error": f"runner 回包非 JSON: {r.status_code}"}
    except httpx.HTTPError as exc:
        return {"error": f"runner 不可达（{url}）：{exc}"}


async def forward_run(work_id: str, card_id: str,
                      payload: dict[str, Any]) -> dict[str, Any]:
    return await forward_ctl(work_id, card_id, "/ctl/run_message", payload)
