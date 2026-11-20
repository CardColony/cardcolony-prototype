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
import logging
import os
import re
import time
import weakref
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

logger = logging.getLogger("infra.procs")

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
    # [席位会话 2026-11-18] 复数端点＝对话顶部 tab 数据源，与上条同族——
    # 读 cards.sessions 列的本地无锁读。不进白名单就会被逐请求转发（基线
    # 2026-11-19：会话标签列表中位 1.01s 全耗在转发层现建客户端上）。
    "/api/works/{}/cards/{}/sessions",
    "/api/works/{}/card-messages/unread",
    "/api/works/{}/cards/{}/exec-backend",
    "/api/works/{}/cards/{}/review-status",
    "/api/works/{}/rule",
    "/api/settings/model-overrides/{}",
    # [窄投影 2026-11-20 · 性能批次一] 「当前卡生效模型」的口。
    # 原来只有 ``/model-overrides/{}``（整项目每卡一行）进白名单；徽标要的是
    # **一张卡的一个模型键**，走 ``/effective?card_id=``。
    # 不进白名单 ⇒ 每次徽标刷新都被转发到 runner-0（多一跳）。
    # 它是**磁盘读**（模型配置表 ＋ models.yaml）⇒ 适合本地答。
    "/api/settings/model-overrides/{}/effective",
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


def is_condense() -> bool:
    return _ROLE == "condense"


def condense_home() -> bool:
    """[凝练独立进程 2026-11-10 · 用户令「凝练应该独立一个runner」]
    凝练只发生在 condense 进程（生产）或 all 单进程（测试/旧模式）；
    runner/web 一律 False。跨进程互斥由「只有一个进程能凝」天然成立，
    不再需要文件锁/分片判据——memory_store 内 _CONDENSING 照旧兜同进程并发。"""
    return _ROLE in ("all", "condense")


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


# ---------------------------------------------------------------
# [回环连接复用 2026-11-19 · 性能批次一]
# 病：_forward / _fanin_get_merge / _fanin_sse_stream / forward_ctl 四个入口
# **逐请求现建** httpx.AsyncClient。本机实测默认构造 ~1.0s/次（Windows CA
# 装载，进程内不缓存：连建 3 次 = 1453/953/1000ms）；2026-11-19 18:00–20:26
# 访问日志 nav/running 中位 1.21s、会话标签 1.01s、进度 0.92s 的主体就是它。
# 药：每**事件循环**一套回环客户端族（web 服务循环 B／runner 主环与分槽环
# 各自一份，首用即建、随循环存续）——客户端创建次数只随「进程、循环、用途」
# 变化，连续请求稳定复用：
#   short  ＝ 状态扇入短查询（30s 超时语义照旧；池容量按 runner 数配，
#            SSE/长跑占满 stream 池也挤不着它）；
#   stream ＝ 请求转发中继 + SSE 扇入泵（timeout=None 长流语义照旧）；
#   ctl    ＝ runner 控制转发（可能承载完整模型回合，timeout=None）。
# 回环目标全部来自 _URLS 且是 http:// 明文 ⇒ 构造传 verify=False 跳过 CA
# 装载（实测 1453ms → 2.6ms）。verify 只作用于 https 握手，对纯 http 回环
# 零安全影响；若未来 _URLS 配了 https 目标则自动回退默认校验。
# keepalive_expiry=25s < uvicorn timeout_keep_alive=30s：空闲连接由**我们**
# 先关，不在服务端 30s 关闭的竞态窗口里捡到死连接。
# ---------------------------------------------------------------

_LOOP_CLIENTS: "dict[int, tuple[weakref.ref, dict[str, httpx.AsyncClient]]]" = {}
_LOOP_CLIENTS_GUARD = __import__("threading").Lock()

_LOOPBACK_PLAIN = all(str(u).lower().startswith("http://") for u in _URLS)

# 卡级 SSE 直通端点标记（与 access_log._SSE_MARKERS 同族；/events 是工作级
# 扇入口、走 sse 池不经 _forward，故这里只列卡级两条）。
_CARD_SSE_MARK = "/stream"
_TOKENS_MARK = "/tokens"


def _new_loop_client(kind: str) -> httpx.AsyncClient:
    """按用途建一份回环客户端（容量按短查询/普通转发/常驻流分池）。

    〔2026-11-19 返工〕relay 与 sse **分池**：原来两者共用 stream 池——
    工作级 SSE 扇入泵是常驻连接（每订阅者 × 每 runner 一条），订阅者一多
    就把池占满 ⇒ 普通转发（进度/exec-backend 等经 _forward 的请求）排队
    饿死。分池后：sse 池被常驻流占满，不影响 relay（普通转发/卡级流直通）
    与 short（状态扇入）、ctl（控制转发）。
    """
    kw: dict[str, Any]
    if kind == "short":
        n = max(2 * _RUNNER_COUNT, 8)
        kw = dict(timeout=httpx.Timeout(30.0),
                  limits=httpx.Limits(max_connections=n,
                                      max_keepalive_connections=n,
                                      keepalive_expiry=25.0))
    elif kind == "ctl":
        n = max(_RUNNER_COUNT + 2, 6)
        kw = dict(timeout=None,
                  limits=httpx.Limits(max_connections=max(n, 16),
                                      max_keepalive_connections=n,
                                      keepalive_expiry=25.0))
    elif kind == "sse":
        # 工作级 SSE 扇入泵：**常驻**连接（不设 keepalive 过期——连接
        # 该由泵退出时的 async with 关闭，不该被空闲过期拆掉重连）。
        # 32 ≈ 8 个并发订阅者 × 4 runner；超出的订阅者排队等连接释放。
        kw = dict(timeout=None,
                  limits=httpx.Limits(max_connections=32,
                                      max_keepalive_connections=32,
                                      keepalive_expiry=None))
    elif kind == "card_stream":
        # 〔2026-11-19 返工2〕卡级 SSE（/stream、/tokens）经 _forward 的
        # **直通长流**——常驻连接与普通转发 relay 池隔离：卡级流占满
        # card_stream 池不影响 progress/exec-backend 等普通转发（relay）
        # 与状态扇入（short）。32 ≈ 8 个并发订阅 × 4 runner。
        kw = dict(timeout=None,
                  limits=httpx.Limits(max_connections=32,
                                      max_keepalive_connections=32,
                                      keepalive_expiry=None))
    else:  # relay：浏览器请求转发中继（普通 POST/GET + 卡级 SSE 直通）
        kw = dict(timeout=None,
                  limits=httpx.Limits(max_connections=64,
                                      max_keepalive_connections=16,
                                      keepalive_expiry=25.0))
    if _LOOPBACK_PLAIN:
        kw["verify"] = False
    # [2026-11-19 · 502 根因] 回环调用**永不读代理配置**：
    # httpx 默认 trust_env=True 会读注册表系统代理 / HTTP(S)_PROXY，连
    # 127.0.0.1 也塞给代理 ⇒ 代理拒转发本地地址就回 502 Bad Gateway
    # （实测：直连 200 / 走代理 502）。内部调用与代理无关，硬关。
    kw["trust_env"] = False
    return httpx.AsyncClient(**kw)


def _loop_clients() -> "dict[str, httpx.AsyncClient]":
    """当前事件循环名下的回环客户端族（每循环一份，逐请求零创建）。"""
    loop = asyncio.get_running_loop()
    key = id(loop)
    with _LOOP_CLIENTS_GUARD:
        ent = _LOOP_CLIENTS.get(key)
        if ent is not None and ent[0]() is loop:
            return ent[1]
        for dead in [k for k, (r, _f) in _LOOP_CLIENTS.items() if r() is None]:
            _LOOP_CLIENTS.pop(dead, None)   # 循环已死的旧族：引用清掉
        fam = {kind: _new_loop_client(kind)
               for kind in ("short", "relay", "sse", "card_stream", "ctl")}
        _LOOP_CLIENTS[key] = (weakref.ref(loop), fam)
        logger.info("[procs] 回环客户端族已建 loop=0x%x role=%s "
                    "kinds=short/relay/sse/card_stream/ctl verify_bypass=%s",
                    key, _ROLE, _LOOPBACK_PLAIN)
        # [2026-11-19 返工·预热] fanin 客户端建好即对每台 runner 各发一次
        # 轻量 GET，把 keepalive 连接**预先**建进池——否则「SSE 连接风暴之后
        # 第一次 fanin 现场新建 TCP」会撞 Windows loopback 的 SYN 丢弃窗口
        # （实测：三片 0ms 到达、第四片 SYN 重传 ~17s 后才到 runner；两次
        # flood 复验均复现）。预热失败零影响（连接该建时自然建）。
        if _ROLE == "web" and _RUNNER_COUNT > 0:
            async def _warm() -> None:
                try:
                    await asyncio.gather(
                        *(fam["short"].get(runner_url(i) + "/api/health",
                                           timeout=5.0)
                          for i in range(_RUNNER_COUNT)),
                        return_exceptions=True)
                except Exception:  # noqa: BLE001 预热失败不影响服务
                    pass
            loop.create_task(_warm())
        return fam


def close_all_loop_clients(timeout: float = 3.0) -> int:
    """优雅关停：关掉全部循环名下的回环客户端族（尽力而为，不抛）。"""
    with _LOOP_CLIENTS_GUARD:
        entries = list(_LOOP_CLIENTS.items())
        _LOOP_CLIENTS.clear()

    async def _aclose(fam: "dict[str, httpx.AsyncClient]") -> None:
        for c in fam.values():
            try:
                await c.aclose()
            except Exception:  # noqa: BLE001
                pass

    closed = 0
    for _key, (ref, fam) in entries:
        lp = ref()
        try:
            if lp is None or lp.is_closed():
                pass        # 循环已死：socket 随进程回收
            elif lp.is_running():
                asyncio.run_coroutine_threadsafe(_aclose(fam), lp).result(timeout)
            else:
                lp.run_until_complete(_aclose(fam))
        except Exception:  # noqa: BLE001 关停路径零阻断
            pass
        closed += 1
    return closed


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
    # 〔2026-11-19 返工2〕卡级 SSE（/stream、/tokens）直通长流走
    # **card_stream** 池（与普通转发 relay 隔离）；其余请求走 relay。
    _p = request.url.path
    client = _loop_clients()["card_stream" if (_CARD_SSE_MARK in _p
                                               or _TOKENS_MARK in _p)
                            else "relay"]
    upstream = client.build_request(
        request.method, url, content=body, headers=headers)
    # [启动窗口 2026-11-19] 整组重启时 runner 可能还在建索引/绑端口，此刻
    # 转发会 ConnectError ⇒ 一次 502 把前端打断。内部转发是幂等的（请求体已
    # 备好），对**连接类**错误重试一次（0.6s）即可跨过启动窗口；业务错误不重试。
    for attempt in range(2):
        try:
            resp = await client.send(upstream, stream=True)
            break
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            if attempt == 0:
                await asyncio.sleep(0.6)
                continue
            return Response(
                f"runner 不可达（{url}）：{exc}", status_code=502,
                media_type="text/plain; charset=utf-8")
        except httpx.HTTPError as exc:
            return Response(
                f"runner 不可达（{url}）：{exc}", status_code=502,
                media_type="text/plain; charset=utf-8")

    async def _relay():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            # 客户端取消/流断开 ⇒ 归还当次上游连接（连接回池复用；
            # 客户端本身随事件循环存续，这里绝不关它）
            await resp.aclose()

    return StreamingResponse(
        _relay(),
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items()
                 if k.lower() not in ("transfer-encoding", "connection",
                                      "content-length")},
        media_type=resp.headers.get("content-type"),
    )


async def _fanin_get_one(client: "httpx.AsyncClient", url: str) -> tuple:
    """一片 runner 的短查询（带耗时）。幂等 GET 遇传输层错误单次重试——
    兜「池内空闲连接恰被对端关闭」的竞态（keepalive_expiry 25s < 服务端
    30s 已把窗口压到极小，这里再兜一层，可用性与逐请求建客户端时期一致）。"""
    t0 = time.perf_counter()
    for attempt in (0, 1):
        try:
            r = await client.get(url)
            return r, (time.perf_counter() - t0) * 1000.0, None
        except httpx.TransportError as exc:
            if attempt:     # 第二次仍失败：如实上抛语义
                return None, (time.perf_counter() - t0) * 1000.0, exc
    return None, (time.perf_counter() - t0) * 1000.0, RuntimeError("unreachable")


async def _fanin_get_merge(path: str, query: str) -> Response:
    """GET 扇入：并发问全部 runner，JSON 合并（list 去重并、dict 逐键并）。

    [2026-11-19 性能批次一] 复用循环名下 short 客户端（原来每次现建
    AsyncClient，~1s 全花在构造上）；逐片计时入日志——慢 runner/部分失败
    时能指出是哪一片、各自等了多久。
    """
    url = lambda i: runner_url(i) + path + (("?" + query) if query else "")
    client = _loop_clients()["short"]
    t0 = time.perf_counter()
    results = await asyncio.gather(
        *(_fanin_get_one(client, url(i)) for i in range(_RUNNER_COUNT)))
    total_ms = (time.perf_counter() - t0) * 1000.0
    per_ms = [round(ms, 1) for _r, ms, _e in results]
    errs = [f"runner{i}:{type(e).__name__}" for i, (_r, _ms, e) in enumerate(results)
            if e is not None]
    payloads = [r.json() for (r, _ms, _e) in results
                if r is not None and r.status_code == 200
                and "application/json" in r.headers.get("content-type", "")]
    if errs or total_ms > 1000.0:
        logger.warning("[procs] fanin GET %s total=%.1fms per_runner_ms=%s "
                       "ok=%d/%d err=%s", path, total_ms, per_ms,
                       len(payloads), _RUNNER_COUNT, errs or "-")
    else:
        logger.debug("[procs] fanin GET %s total=%.1fms per_runner_ms=%s ok=%d/%d",
                     path, total_ms, per_ms, len(payloads), _RUNNER_COUNT)
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
    """工作级 SSE 扇入：向全部 runner 各开一条 /events 流，行级合并转发。

    [2026-11-19 性能批次一/返工] 泵复用循环名下 **sse 池**客户端（常驻
    连接与普通转发 relay 池隔离——订阅者占满 sse 池不影响 relay/short）；
    退出时只 cancel 泵任务（async with 退出即归还连接），客户端随循环存续。
    """
    url = lambda i: (runner_url(i)
                     + f"/api/works/{work_id}/events"
                     + (("?" + query) if query else ""))
    sse_cli = _loop_clients()["sse"]

    async def _merged():
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)

        async def _pump(i: int) -> None:
            # [2026-11-19 返工·错峰] 泵按序号错开 50ms 启动——一次性 4×订阅
            # 条连接同时 SYN 会撞 Windows loopback 的丢弃窗口（实测一次
            # 40 泵风暴后 fanin 现建连接 SYN 重传 ~17s）。错峰后建立平滑。
            if i:
                await asyncio.sleep(0.05 * i)
            try:
                async with sse_cli.stream("GET", url(i)) as resp:
                    async for line in resp.aiter_lines():
                        await q.put(line)
            except asyncio.CancelledError:
                raise
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

    return StreamingResponse(_merged(), media_type="text/event-stream")


# ---------------------------------------------------------------
# runner 间调用：跨片执行转发（_run_message / deliver_now 入口自查）
# ---------------------------------------------------------------

async def forward_ctl(work_id: str, card_id: str, ctl_path: str,
                      payload: dict[str, Any]) -> dict[str, Any]:
    """把一个卡级执行请求投给属主 runner。返回对端 JSON 或 {"error": ...}。

    [2026-11-08 · 评审热循环实弹] 两个洞一起堵：
    - 路径带 ``/api`` 前缀——api_router 挂的是 /api/ctl/*，裸 /ctl/* 恒 404；
      改前全部转发请求 404 ⇒ 下游把 {"detail":"Not Found"} 当正常回包
      （无 error 键），评审席位回合"秒完不投票"⇒ 每秒一圈热循环。
    - 非 2xx 一律折成 ``{"error": {...}}``——错误必须长成错误的样子，
      下游的 err 判据才能接住、防死循环闸才有刹车（404 的 {"detail"} 正是
      靠"不像错误"蒙混过关的）。
    """
    url = (runner_url(owner_index_of(work_id, card_id))
           + "/api" + str(ctl_path or ""))
    try:
        # [2026-11-19 性能批次一] ctl 客户端随循环复用（每调用现建 ~1s）；
        # timeout=None 照旧——控制调用可能承载完整模型回合，不套短查询超时。
        client = _loop_clients()["ctl"]
        r = await client.post(url, json=payload)
        if r.status_code >= 400:
            return {"error": {"code": "runner_http",
                              "message": f"runner HTTP {r.status_code}（{url}）："
                                         f"{(r.text or '')[:200]}"}}
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return {"error": {"code": "runner_bad_json",
                              "message": f"runner 回包非 JSON: {r.status_code}"}}
    except httpx.HTTPError as exc:
        return {"error": {"code": "runner_unreachable",
                          "message": f"runner 不可达（{url}）：{exc}"}}


async def forward_run(work_id: str, card_id: str,
                      payload: dict[str, Any]) -> dict[str, Any]:
    return await forward_ctl(work_id, card_id, "/ctl/run_message", payload)


async def forward_review_step(work_id: str, review_id: str,
                              payload: dict[str, Any]) -> dict[str, Any]:
    """评审一步投给场卡属主 runner——循环只在场卡属主进程跑（事件/seq 单空间）。"""
    return await forward_ctl(work_id, review_id, "/ctl/review_step", payload)
