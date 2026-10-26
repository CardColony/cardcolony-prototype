# -*- coding: utf-8 -*-
"""会话解析结果的**内存缓存**网（[P25 · 2026-10-26 · 「页面的内容存进内存态」]）

## 病根（实测）

旧档案扫描（`/card-messages` 的源二）每次都要把**每张卡的整份会话文件**读一遍＋
解析一遍：work_027 的 20 张卡 / 16427 块 ⇒ **读 1308ms（磁盘慢）＋ 解析 802ms**，
而且**每次刷新都重算** ⇒ 画布页首屏那条 `card-messages?limit=200` 实测 **4417ms**，
把同一时刻的 `nav/version`（4239ms）、`rule`（2262ms）全拖慢 ⇒ 整页卡。

⇒ 用户原话：「**每次刷新，页面的布局都是重新算的？页面的内容应该也存进内存态**」。

## 网里钉的不变量

| 编号 | 不变量 |
|---|---|
| P1 | 第二次读**命中缓存**（不再读文件、不再解析）⇒ 毫秒级 |
| P2 | **文件一改，缓存立刻失效**（失效判据＝(mtime_ns, size)）⇒ 写后读不会拿到旧内容 |
| P3 | 缓存**有上限**（内存封顶，不是时间闸） |
| P4 | 过滤参数（from_seq/to_seq/actor/turn/limit）在缓存之上**仍然生效** |

铁律一：只用 conftest 的沙箱，不碰真实 works。
"""
from __future__ import annotations

import time

from src.storage import session_facade


def _mk_session(work_id: str, card_id: str, blocks: int = 1) -> None:
    """在沙箱里铺一个 V2 会话文件（n 个块）。"""
    p = session_facade._session_path(work_id, card_id)
    text = session_facade._render_v2_header(session_facade._default_header(card_id))
    for i in range(blocks):
        text += session_facade._render_v2_block({
            "seq": i + 1, "actor": "human", "from": "", "to": "",
            "role": "user", "content": "内容 %d" % i, "timestamp": "2026-10-26T00:00:00",
            "model": "", "pair_id": "", "turn": 0, "type": "",
            "behavior": "", "end": "", "format": "v1",
        })
    session_facade._atomic_write_text(p, text)


async def _a_card(work: dict) -> tuple[str, str]:
    """在沙箱剧本里造一张子卡，返回 (work_id, card_id)。"""
    from src.storage import card_store
    wid = work["work_id"]
    root = work["work_card_id"]
    r = await card_store.create_card(wid, "缓存网子卡", "用户直接创建", parent=root)
    assert "error" not in r, r
    for cid, meta in card_store.iter_card_metas(wid).items():
        if meta.get("title") == "缓存网子卡":
            return wid, cid
    raise AssertionError("卡没建成")


async def test_p1_second_read_hits_cache(sandbox, work):
    """P1：第二次读命中缓存（不再读文件、不再解析）。"""
    wid, cid = await _a_card(work)
    _mk_session(wid, cid, blocks=20)

    session_facade._PARSE_CACHE.clear()
    t0 = time.perf_counter()
    first = await session_facade.read(wid, cid)
    t1 = time.perf_counter()
    second = await session_facade.read(wid, cid)
    t2 = time.perf_counter()
    assert len(first) == 20
    assert len(second) == 20
    assert (t2 - t1) * 1000 < (t1 - t0) * 1000 + 5, \
        f"第二次读应命中缓存（第一次 {(t1-t0)*1000:.1f}ms，第二次 {(t2-t1)*1000:.1f}ms）"
    assert f"{wid}/{cid}" in session_facade._PARSE_CACHE


async def test_p2_file_change_invalidates_cache(sandbox, work):
    """P2：文件一改，缓存立刻失效（写后读拿到新内容）。"""
    wid, cid = await _a_card(work)
    _mk_session(wid, cid, blocks=3)
    await session_facade.read(wid, cid)

    # 改文件（内容变长 ⇒ size 变；mtime 也变）
    _mk_session(wid, cid, blocks=9)
    blocks = await session_facade.read(wid, cid)
    assert len(blocks) == 9, f"文件改了却读到缓存里的旧内容（{len(blocks)} 条）"


async def test_p3_cache_is_bounded(sandbox, work):
    """P3：缓存有上限（内存封顶，不是时间闸）。"""
    assert session_facade._PARSE_CACHE_MAX > 0
    assert session_facade._PARSE_CACHE_MAX <= 4096, "缓存上限得是个有界的数"


async def test_p4_filters_still_apply_over_cache(sandbox, work):
    """P4：过滤参数在缓存之上仍然生效（from_seq/actor/limit）。"""
    wid, cid = await _a_card(work)
    _mk_session(wid, cid, blocks=10)

    allb = await session_facade.read(wid, cid)
    assert len(allb) == 10
    tail = await session_facade.read(wid, cid, from_seq=6)
    assert len(tail) == 5, tail
    lim = await session_facade.read(wid, cid, limit=3)
    assert len(lim) == 3, lim
    # 再来一次全量（走缓存）仍要对
    assert len(await session_facade.read(wid, cid)) == 10
