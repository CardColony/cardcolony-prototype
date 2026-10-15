# -*- coding: utf-8 -*-
"""[迭代一·会话底座] session_facade 单测：S1 格式层 ＋ S2 门面层。

任务书验收对照：
- S1 验收 1：五类样本（合法 V2／合法 V1／缺 seq 块／白名单外字段／未闭合块）；
- S1 验收 2：V1 五键与旧 read_session 逐条等价（diff=0）；
- S1 验收 3：转义对拍（---MSG---／---END---／---SESSION--- 写读往返逐字不变）；
- S2 验收 1：四接口冒烟（空数据／正常数据）＋ create_with_session 事务；
- S2 验收 3：create_with_session 故障注入（会话败→卡不落盘；卡败→无孤儿会话）；
- S2 验收 4：投影担保（全删自动重建；写路径永不读投影——故障注入钉死）。
门面级旧读对账（S2 验收 2，70 个真实文件副本）由 scripts/reconcile_session_facade.py 承载。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.storage import card_store, session_facade, session_store

FIXTURES = Path(__file__).parent / "_fixtures" / "session_facade_samples"

V1_LAST_TS = "2026-10-01T10:01:10+08:00"


def _sample(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


async def _mkcard(work, title="对账卡") -> str:
    r = await card_store.create_card(work["work_id"], title, "用户直接创建")
    assert "error" not in r, r
    return r["meta"]["card_id"]


def _sess(work, card_id) -> Path:
    return session_store._session_path(work["work_id"], card_id)


def _five_keys(blocks):
    return [{k: b.get(k) for k in ("role", "content", "timestamp", "model", "pair_id")}
            for b in blocks]


# ============================================================ S1 · 五类样本
async def test_s1_v1_legal_five_keys_equivalence(work):
    """S1 验收 1(V1)+2：合法 V1 样本——五键与旧解析器逐条等价 diff=0。"""
    cid = await _mkcard(work)
    _sess(work, cid).write_text(_sample("v1_legal.md"), encoding="utf-8")
    old = await session_store.read_session(work["work_id"], cid)
    new = await session_facade.read(work["work_id"], cid)
    assert len(old) == len(new) == 4
    assert _five_keys(old) == _five_keys(new)  # diff=0
    # 派生字段钉死（T067：seq 现算／actor 映射／turn·pair·end 按块序推）
    assert [b["actor"] for b in new] == ["human", "assistant", "human", "assistant"]
    assert [b["turn"] for b in new] == [0, 0, 1, 1]
    assert [b["pair"] for b in new] == ["0", "0", "2", "2"]
    assert [b["end"] for b in new] == ["", "model_done", "", "model_done"]
    assert [b["seq"] for b in new] == [0, 1, 2, 3]
    assert new[3]["pair_id"] == ""  # 旧块无 pair_id 行 → 空串（向后兼容 V7）


async def test_s1_v2_legal(work):
    """S1 验收 1(V2)：合法 V2 样本——头与块字段逐项符合规格。"""
    cid = await _mkcard(work)
    _sess(work, cid).write_text(_sample("v2_legal.md"), encoding="utf-8")
    blocks = await session_facade.read(work["work_id"], cid)
    assert len(blocks) == 3
    assert all("damaged" not in b for b in blocks)
    assert [b["seq"] for b in blocks] == [0, 1, 2]
    assert [b["actor"] for b in blocks] == ["human", "assistant", "human"]
    assert [b["turn"] for b in blocks] == [0, 0, 1]
    assert [b["end"] for b in blocks] == ["", "model_done", ""]
    assert blocks[1]["model"] == "m2"
    assert blocks[0]["content"] == "问题甲"
    hdr = await session_facade.read_header(work["work_id"], cid)
    assert hdr["version"] == 2 and hdr["session_id"] == "card_999"
    assert hdr["identity_owner"] == "human" and hdr["inbox_ref"] == ""


async def test_s1_v2_missing_seq_damaged_placeholder(work):
    """S1 验收 1(缺 seq)＋T064：必字段缺失→damaged 占位、不吞并。"""
    cid = await _mkcard(work)
    _sess(work, cid).write_text(_sample("v2_missing_seq.md"), encoding="utf-8")
    blocks = await session_facade.read(work["work_id"], cid)
    assert len(blocks) == 2  # 占位不吞并（T117 承重语义）
    assert blocks[1]["damaged"] is True
    assert blocks[1]["damaged_reason"] == "missing:seq"
    assert blocks[1]["seq"] is None
    assert blocks[1]["content"].startswith("缺seq块")  # 原文保留
    assert "damaged" not in blocks[0]  # 完好块不带 damaged 标记


async def test_s1_v2_unknown_field_kept(work):
    """S1 验收 1(白名单外)+T063：保留原值＋unknown_field 标记，不炸不吞。"""
    cid = await _mkcard(work)
    _sess(work, cid).write_text(_sample("v2_unknown_field.md"), encoding="utf-8")
    blocks = await session_facade.read(work["work_id"], cid)
    assert len(blocks) == 1 and "damaged" not in blocks[0]
    assert blocks[0]["unknown_fields"] == ["priority"]
    assert blocks[0]["unknown_values"] == {"priority": "high"}


async def test_s1_v2_unclosed_damaged(work):
    """S1 验收 1(未闭合)+T065：未闭合块→damaged 占位交体检（体检器 S3 落地）。"""
    cid = await _mkcard(work)
    _sess(work, cid).write_text(_sample("v2_unclosed.md"), encoding="utf-8")
    blocks = await session_facade.read(work["work_id"], cid)
    assert len(blocks) == 2
    assert blocks[1]["damaged"] is True and blocks[1]["damaged_reason"] == "unclosed"
    assert blocks[1]["content"] == "未闭合块正文（EOF 截断→damaged 占位，交体检）"


# ============================================================ S1 · 转义对拍
async def test_s1_escape_roundtrip_v2(work):
    """S1 验收 3：正文含 ---MSG---／---END---／---SESSION--- 写读往返逐字不变。"""
    cid = await _mkcard(work)
    content = "第一行\n---MSG---\n中间\n---END---\n---SESSION---\n末行"
    blk = await session_facade.append(work["work_id"], cid,
                                      {"role": "user", "content": content})
    assert blk["content"] == content
    blocks = await session_facade.read(work["work_id"], cid)
    assert len(blocks) == 1
    assert blocks[0]["content"] == content  # 逐字不变
    # V1 路径转义对拍：\---MSG--- 转义行按旧语义还原
    cid2 = await _mkcard(work, "对卡二")
    _sess(work, cid2).write_text(_sample("v1_legal.md"), encoding="utf-8")
    old = await session_store.read_session(work["work_id"], cid2)
    new = await session_facade.read(work["work_id"], cid2)
    assert old[2]["content"] == new[2]["content"]


# ============================================================ S2 · 四接口冒烟
async def test_s2_read_empty_and_missing(work):
    cid = await _mkcard(work)  # 会话文件仅注释行（V1 形态、零块）
    assert await session_facade.read(work["work_id"], cid) == []
    assert await session_facade.read(work["work_id"], "card_999") == []
    assert await session_facade.read_header(work["work_id"], cid) is None  # V1 无头


async def test_s2_append_semantics_v2(work):
    """S2 冒烟＋T024：自动分配 seq/turn、填 actor、闭合轮界。"""
    cid = await _mkcard(work)
    a0 = await session_facade.append(work["work_id"], cid,
                                     {"role": "user", "content": "问一"})
    a1 = await session_facade.append(work["work_id"], cid,
                                     {"role": "assistant", "content": "答一", "model": "m1"})
    a2 = await session_facade.append(work["work_id"], cid,
                                     {"actor": "human", "content": "问二"})
    assert (a0["seq"], a0["turn"], a0["pair"], a0["end"], a0["actor"]) == (0, 0, "0", "", "human")
    assert (a1["seq"], a1["turn"], a1["pair"], a1["end"]) == (1, 0, "0", "model_done")
    assert (a2["seq"], a2["turn"], a2["pair"], a2["end"]) == (2, 1, "2", "")
    assert a1["role"] == "assistant" and a1["model"] == "m1"
    blocks = await session_facade.read(work["work_id"], cid)
    assert [b["seq"] for b in blocks] == [0, 1, 2]
    assert _sess(work, cid).read_text(encoding="utf-8").startswith("---SESSION---")
    hdr = await session_facade.read_header(work["work_id"], cid)
    assert hdr["version"] == 2 and hdr["session_id"] == cid


async def test_s2_append_upgrades_v1(work):
    """4.8 现场升格：V1 文件被追加→先转 V2（逐块一一对应），五键语义等价。"""
    cid = await _mkcard(work)
    await session_store.append_message(work["work_id"], cid, "user", "旧问一", pair_id="p1")
    await session_store.append_message(work["work_id"], cid, "assistant", "旧答一",
                                       model="m0", pair_id="p1")
    before = _five_keys(await session_store.read_session(work["work_id"], cid))
    blk = await session_facade.append(work["work_id"], cid,
                                      {"role": "user", "content": "新问二"})
    assert blk["seq"] == 2 and blk["turn"] == 1 and blk["pair"] == "2"
    blocks = await session_facade.read(work["work_id"], cid)
    assert len(blocks) == 3
    assert _five_keys(blocks[:2]) == before  # 升格后旧块五键等价（uuid 在内）
    assert blocks[0]["pair_id"] == "p1" and blocks[1]["pair_id"] == "p1"  # uuid 跨升格存活
    assert "pair_id" in blocks[0]["unknown_fields"]  # 经 T063 前向兼容通道（S6b 衔接桥）
    hdr = await session_facade.read_header(work["work_id"], cid)
    assert hdr["version"] == 2
    text = _sess(work, cid).read_text(encoding="utf-8")
    assert text.startswith("---SESSION---") and text.count("---END---") == 3


async def test_s2_scan_tail(work):
    """T025/T073：V1 委托 last_ts 快路径；V2 尾部直读 ts＋seq；CRLF 归一吸收。"""
    missing = session_facade.scan_tail(work["work_id"], "card_888")
    assert missing == {"ts": "", "seq": None, "format": None}
    cid1 = await _mkcard(work, "扫卡一")
    _sess(work, cid1).write_text(_sample("v1_legal.md"), encoding="utf-8")
    st1 = session_facade.scan_tail(work["work_id"], cid1)
    assert st1 == {"ts": V1_LAST_TS, "seq": None, "format": "v1"}
    assert st1["ts"] == session_store.last_ts(work["work_id"], cid1)  # 同源
    cid2 = await _mkcard(work, "扫卡二")
    await session_facade.append(work["work_id"], cid2, {"role": "user", "content": "一"})
    await session_facade.append(work["work_id"], cid2, {"role": "assistant", "content": "二"})
    st2 = session_facade.scan_tail(work["work_id"], cid2)
    assert st2["format"] == "v2" and st2["seq"] == 1 and st2["ts"]
    # CRLF 归一化（:200-202 吸收）
    cid3 = await _mkcard(work, "扫卡三")
    raw = _sample("v2_legal.md").replace("\n", "\r\n")
    _sess(work, cid3).write_bytes(raw.encode("utf-8"))
    st3 = session_facade.scan_tail(work["work_id"], cid3)
    assert st3["seq"] == 2 and st3["ts"] == "2026-10-02T09:01:00+08:00"


# ============================================================ S2 · create_with_session 事务
async def test_s2_create_with_session_ok(work):
    r = await session_facade.create_with_session(work["work_id"], "事务卡", "用户直接创建")
    assert "error" not in r
    cid = r["meta"]["card_id"]
    await card_store.read_card(work["work_id"], cid)  # 卡在
    text = _sess(work, cid).read_text(encoding="utf-8")
    assert text.startswith("---SESSION---")  # V2 头落位（T051）
    hdr = await session_facade.read_header(work["work_id"], cid)
    assert hdr["session_id"] == cid and hdr["identity_owner"] == "human"
    assert hdr["identity_purpose"] == "discussion" and hdr["inbox_ref"] == ""
    assert await session_facade.read(work["work_id"], cid) == []  # 空会话可读


async def test_s2_create_with_session_rollback_on_session_fail(work, monkeypatch):
    """S2 验收 3a：会话创建失败→卡片不落盘（移入 damaged/ 留档，不删除字节）。"""
    def _boom(path, text):
        raise RuntimeError("注入：V2 头写盘失败")
    monkeypatch.setattr(session_facade, "_atomic_write_text", _boom)
    with pytest.raises(RuntimeError):
        await session_facade.create_with_session(work["work_id"], "回滚卡", "用户直接创建")
    from src.storage import card_store as cs
    # [卡片存储迁移 SQLite] 卡在库内：回滚后库内不得留半事务卡（damaged/ 有留档）
    assert not cs.card_exists(work["work_id"], "card_002")
    damaged = list((session_store.WORKS_DIR / work["work_id"] / "damaged").glob("*"))
    assert len(damaged) == 2  # 卡留档 ＋ 会话占位文件，均留档


async def test_s2_create_with_session_card_fail_no_orphan(work, monkeypatch):
    """S2 验收 3b：卡片创建失败→无孤儿会话文件。"""
    from src.storage import card_db

    def _boom(*a, **kw):
        raise RuntimeError("注入：卡片写库失败")
    monkeypatch.setattr(card_db, "insert_card", _boom)
    sdir = session_store.WORKS_DIR / work["work_id"] / "sessions"
    before = {p.name for p in sdir.glob("*")}
    r = await session_facade.create_with_session(work["work_id"], "败卡", "用户直接创建")
    assert "error" in r
    assert {p.name for p in sdir.glob("*")} == before  # 无孤儿会话
    assert not (session_store.WORKS_DIR / work["work_id"] / "damaged").exists()


# ============================================================ S2 · 投影担保
async def test_s2_projection_guarantees(work, monkeypatch):
    """S2 验收 4：投影全删→自动重建；写路径永不读投影（故障注入钉死，T076）。"""
    cid = await _mkcard(work, "投影卡")
    await session_facade.append(work["work_id"], cid, {"role": "user", "content": "一"})
    await session_facade.append(work["work_id"], cid, {"role": "assistant", "content": "二"})
    proj = session_facade.project(work["work_id"])
    assert proj["derived"] is True and proj["generated_at"]
    mine = [it for it in proj["items"] if it["card"] == cid]
    assert len(mine) == 1 and mine[0]["seq"] == 1 and mine[0]["ts"]
    pfile = session_facade._projection_path(work["work_id"])
    assert pfile.exists()
    pfile.unlink()  # 全删
    proj2 = session_facade.project(work["work_id"])
    assert proj2["derived"] is True and pfile.exists()  # 自动重建
    # 写路径隔离：投影函数全炸，写路径照常
    async def _boom_async(*a, **kw):
        raise RuntimeError("投影被禁用")
    def _boom_sync(*a, **kw):
        raise RuntimeError("投影被禁用")
    monkeypatch.setattr(session_facade, "project", _boom_sync)
    monkeypatch.setattr(session_facade, "rebuild", _boom_sync)
    blk = await session_facade.append(work["work_id"], cid, {"role": "user", "content": "三"})
    assert blk["seq"] == 2
    st = session_facade.scan_tail(work["work_id"], cid)
    assert st["seq"] == 2


# ============================================================ S2 · read 过滤
async def test_s2_read_filters(work):
    cid = await _mkcard(work, "过滤卡")
    for role, text in (("user", "q1"), ("assistant", "a1"),
                       ("user", "q2"), ("assistant", "a2")):
        await session_facade.append(work["work_id"], cid, {"role": role, "content": text})
    r = lambda **kw: session_facade.read(work["work_id"], cid, **kw)  # noqa: E731
    assert [b["seq"] for b in await r(from_seq=2)] == [2, 3]
    assert [b["seq"] for b in await r(to_seq=1)] == [0, 1]
    assert [b["seq"] for b in await r(actor="human")] == [0, 2]
    assert [b["seq"] for b in await r(turn=1)] == [2, 3]
    assert len(await r(limit=1)) == 1
    assert [b["seq"] for b in await r(from_seq=2, actor="assistant")] == [3]
