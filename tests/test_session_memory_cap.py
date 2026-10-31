# -*- coding: utf-8 -*-
"""会话解析缓存的**内存封顶**网（〔2026-10-31 · 治「疯狂占内存」〕）。

## 病根（实测，Windows 事件 Resource-Exhaustion-Detector/2004 佐证）

`_PARSE_CACHE` 原来只有**条数**上限（512 条）⇒ 而**单条**体积随会话线性增长：

| 会话文件 | 解析后**常驻** | 解析**峰值** |
|---|---|---|
| work_027 `session_021.md` 117MB | **323MB** | 909MB |
| work_027 `session_fn_006.md` 61MB | 147MB | 428MB |
| work_027 前 12 张卡合计 | **2.96GB** | — |

⇒ 512 条最坏 **123GB 常驻** ⇒ 进程 commit 被打穿 ⇒ 整机「低虚拟内存」
（7 天内 28 次，每次 python.exe 16~42.8GB，**dwm 每次都在前三**）⇒
**桌面黑、鼠标失灵** ＋ 服务 `MemoryError` 崩溃。

## 网里钉的不变量

| 编号 | 不变量 |
|---|---|
| Q1 | 缓存有**字节**预算（容量封顶，不是条数、更不是时间） |
| Q2 | 缓存实际占用**不超过**字节预算（超了按插入序淘汰最旧） |
| Q3 | 单条超份额 ⇒ **不入缓存**（不把其余全挤掉） |
| Q4 | **投影读**只返回请求字段、``content`` 截断 ⇒ 内存按投影后算 |
| Q5 | 投影读与全量读**互不污染**（缓存键不同；全量读仍给完整正文） |
| Q6 | ``content`` **在解析期**就截断（不先造整份正文再丢） |
| Q7 | 投影读也吃字节封顶（记账口径一致） |

铁律一：只用 conftest 的沙箱，不碰真实 works。
"""
from __future__ import annotations

from src.storage import session_facade


def _mk_session(work_id: str, card_id: str, blocks: int = 1,
                body: str = "内容 %d") -> None:
    """在沙箱里铺一个 V2 会话文件（n 个块）。"""
    p = session_facade._session_path(work_id, card_id)
    text = session_facade._render_v2_header(session_facade._default_header(card_id))
    for i in range(blocks):
        text += session_facade._render_v2_block({
            "seq": i + 1, "actor": "human", "from": "", "to": "",
            "role": "user", "content": body % i,
            "timestamp": "2026-10-31T00:00:00",
            "model": "", "pair_id": "", "turn": 0, "type": "",
            "behavior": "", "end": "", "format": "v1",
        })
    session_facade._atomic_write_text(p, text)


def test_q1_byte_budget_exists():
    """Q1：缓存有**字节**预算（容量封顶，不是条数、更不是时间）。"""
    assert session_facade._PARSE_CACHE_MAX_BYTES > 0, "解析缓存必须有字节预算"
    assert session_facade._PARSE_CACHE_MAX > 0, "条数上限也得留着（双闸）"
    assert isinstance(session_facade._PARSE_CACHE_BYTES, dict)


async def test_q2_cache_within_byte_budget(sandbox, work, monkeypatch):
    """Q2：缓存实际占用不超过字节预算（超了按插入序淘汰最旧）。"""
    wid = work["work_id"]
    session_facade.reset_cache()
    # 把预算压到「6 条必然装不下」（单条估约 44KB ⇒ 6 条约 264KB > 150KB）
    monkeypatch.setattr(session_facade, "_PARSE_CACHE_MAX_BYTES", 150 * 1024)
    monkeypatch.setattr(session_facade, "_PARSE_CACHE_ENTRY_MAX_RATIO", 0.9)
    for i in range(6):
        cid = "card_%03d" % (900 + i)
        _mk_session(wid, cid, blocks=30, body="内容占位 %d " + "x" * 200)
        await session_facade.read(wid, cid)

    total = sum(session_facade._PARSE_CACHE_BYTES.values())
    assert total <= session_facade._PARSE_CACHE_MAX_BYTES, \
        f"缓存驻留 {total} 字节 超预算 {session_facade._PARSE_CACHE_MAX_BYTES}"
    assert len(session_facade._PARSE_CACHE) < 6, \
        f"超预算却没淘汰：缓存里 {len(session_facade._PARSE_CACHE)} 条"
    session_facade.reset_cache()


async def test_q3_oversized_entry_not_cached(sandbox, work, monkeypatch):
    """Q3：单条超份额 ⇒ 不入缓存（不把其余全挤掉）。"""
    wid = work["work_id"]
    session_facade.reset_cache()
    monkeypatch.setattr(session_facade, "_PARSE_CACHE_MAX_BYTES", 64 * 1024)
    # 份额闸默认 25% ⇒ 16KB ⇒ 一条 30KB 的会话必然超份额
    _mk_session(wid, "card_950", blocks=30, body="大块 %d " + "y" * 2000)
    await session_facade.read(wid, "card_950")
    assert f"{wid}/card_950" not in session_facade._PARSE_CACHE, \
        "超大条目不该进缓存（它会把其余全挤掉）"
    session_facade.reset_cache()


async def test_q4_projection_returns_only_requested(sandbox, work):
    """Q4：投影读只返回请求字段、content 截断。"""
    wid = work["work_id"]
    session_facade.reset_cache()
    _mk_session(wid, "card_940", blocks=3, body="正文 %d " + "z" * 5000)
    full = await session_facade.read(wid, "card_940")
    assert len(full[0]["content"]) > 5000, "全量读必须给完整正文（对照基准）"

    proj = await session_facade.read(wid, "card_940",
                                     project=("actor", "content", "seq", "ts"))
    assert len(proj) == len(full)
    for b in proj:
        assert set(b) <= {"actor", "content", "seq", "ts", "turn"}, b.keys()
        assert len(b["content"]) <= session_facade._PROJECT_CONTENT_CHARS
    session_facade.reset_cache()


async def test_q5_projection_and_full_do_not_mix(sandbox, work):
    """Q5：投影读与全量读互不污染（全量读仍给完整正文）。"""
    wid = work["work_id"]
    session_facade.reset_cache()
    _mk_session(wid, "card_930", blocks=2, body="正文 %d " + "w" * 5000)
    await session_facade.read(wid, "card_930", project=("actor", "content"))
    full = await session_facade.read(wid, "card_930")
    assert len(full[0]["content"]) > 5000, "投影读把全量读的正文截短了（缓存串味）"
    keys = set(session_facade._PARSE_CACHE.keys())
    assert f"{wid}/card_930" in keys
    assert any(k.startswith(f"{wid}/card_930|p:") for k in keys), \
        f"投影读应有独立的缓存条目，实得 {keys}"
    session_facade.reset_cache()


def test_q6_parse_truncates_content_at_parse_time():
    """Q6：content 在**解析期**就截断（不先造整份正文再丢）。"""
    lines = ["x" * 100 for _ in range(50)]
    kept = session_facade._truncate_content_lines(lines, 250)
    assert sum(len(x) + 1 for x in kept) <= 250 + 101, kept
    assert len(kept) < len(lines), "没截断"
    assert session_facade._truncate_content_lines(lines, 0) is lines, \
        "limit=0 ⇒ 原样返回（旧行为）"


async def test_q7_projection_is_accounted(sandbox, work, monkeypatch):
    """Q7：投影读也吃字节封顶（记账口径一致）。"""
    wid = work["work_id"]
    session_facade.reset_cache()
    monkeypatch.setattr(session_facade, "_PARSE_CACHE_MAX_BYTES", 400 * 1024)
    monkeypatch.setattr(session_facade, "_PARSE_CACHE_ENTRY_MAX_RATIO", 0.9)
    for i in range(6):
        cid = "card_%03d" % (800 + i)
        _mk_session(wid, cid, blocks=30, body="内容 %d " + "x" * 200)
        await session_facade.read(wid, cid, project=("actor", "content"))
    total = sum(session_facade._PARSE_CACHE_BYTES.values())
    assert total <= session_facade._PARSE_CACHE_MAX_BYTES, \
        f"投影缓存驻留 {total} 超预算"
    session_facade.reset_cache()


def test_q8_stream_parser_equals_original(sandbox, work):
    """Q8：字节流解析与原解析器**逐字一致**（含 CRLF 与孤立 \\r 的换行边界）。

    投影读换了实现（按字节、不整读）⇒ 语义必须一字不差。
    这里用「关掉截断」的口径对比 ⇒ 块、字段、值、顺序全等。
    """
    wid = work["work_id"]
    cid = "card_920"
    p = session_facade._session_path(wid, cid)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = session_facade._render_v2_header(session_facade._default_header(cid))
    text += session_facade._render_v2_block({
        "seq": 1, "actor": "human", "from": "", "to": "",
        "role": "user", "content": "第一行\r\n第二行\r第三行",
        "timestamp": "2026-10-31T00:00:00",
        "model": "", "pair_id": "", "turn": 0, "type": "",
        "behavior": "", "end": "", "format": "v1",
    })
    # 写成 **CRLF** 文件（Windows 文本模式的默认产物）
    p.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))

    old = session_facade._parse_v2(p.read_text(encoding="utf-8"))["blocks"]
    new = session_facade._project_v2_lines(
        p.read_bytes().splitlines(), 1 << 40)
    assert len(old) == len(new) == 1, (len(old), len(new))
    assert old == new, "字节流解析与原解析器不一致"


def test_q9_projection_truncates_content(sandbox, work):
    """Q9：投影读**截断**生效（正文只留头部，不整份常驻）。"""
    wid = work["work_id"]
    cid = "card_910"
    p = session_facade._session_path(wid, cid)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = session_facade._render_v2_header(session_facade._default_header(cid))
    text += session_facade._render_v2_block({
        "seq": 1, "actor": "card:card_001", "from": "", "to": "",
        "role": "user", "content": "头行" + "长" * 20000,
        "timestamp": "2026-10-31T00:00:00",
        "model": "", "pair_id": "", "turn": 0, "type": "",
        "behavior": "", "end": "", "format": "v1",
    })
    p.write_bytes(text.encode("utf-8"))

    raw = p.read_bytes()
    blocks = session_facade._project_v2_lines(raw.splitlines(),
                                              session_facade._PROJECT_CONTENT_BYTES)
    assert len(blocks) == 1
    content = blocks[0]["content"]
    assert len(content) < 20000, f"正文没截断：{len(content)} 字符"
    assert content.startswith("头行"), content[:20]
    # 头字段必须完整（投影读要的就是这些）
    assert blocks[0]["actor"] == "card:card_001"
    assert blocks[0]["seq"] == 1
