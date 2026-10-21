# -*- coding: utf-8 -*-
"""记忆归档层（`memory_archive`）回归。

判据：
A1 有描述层（process/docPaths/docRefs）的节点才写行，没有的不写
A2 幂等：同一 (anchor, role) 重复写不产生第二行
A3 可按锚点（seq）过滤读回，docRefs 原样保留
"""
from __future__ import annotations

from src.storage import card_store, memory_domain as md, work_store


async def test_archive_wiring(sandbox):
    wdata = await work_store.create_work("归档剧本")
    wid = wdata["work_id"]
    await card_store.create_card(wid, "根卡", "用户直接创建")
    chain = [
        md.new_node("s1", 1, "有描述层", kind="decision",
                    process="过程正文", docPaths=["a.md"],
                    docRefs=[{"path": "a.md", "seq": 1}]),
        md.new_node("s1", 2, "无描述层", kind="fact"),
    ]
    assert md.save_archive(wid, "card_001", chain) == 1, "只有带描述层的节点写行"
    md.save_archive(wid, "card_001", chain)               # 幂等
    got = md.load_archive(wid, "card_001")
    assert list(got) == [(1, "gist")]
    assert got[(1, "gist")]["process"] == "过程正文"
    assert got[(1, "gist")]["docPaths"] == ["a.md"]
    one = md.load_archive(wid, "card_001", seq=1)
    assert one[(1, "gist")]["docRefs"] == [{"path": "a.md", "seq": 1}]
