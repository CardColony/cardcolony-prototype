"""系统陈述校验：每条「系统是什么样」的话，都要在代码里成立。

这是「系统说明书用机制保证」的落地：

- 说明书的每一句都来自 `system_claims.CLAIMS`（`render_claims()` 只输出 text）
- 每句配一个 `verify`，**跑代码现状**；代码不成立 ⇒ 红
- 无法机械验证的登记在 `MANUAL`，**不许悄悄新增**（新增人工条目＝放弃机制）

预期会红的两条（截至 2026-10-27）是真实架构问题，不是测试写错：

- `parent_is_permission_line`：`_h_send_card_message` 里还有 `_is_in_subtree` 退路
- `functions_head_tail_only`：旧 `review_request` handler 里还在召集职能
"""

from __future__ import annotations

from src.api import system_claims


def test_all_verifiable_claims_hold():
    """可验证的陈述必须成立。不成立且未登记 `UNMET` ⇒ 红。

    `UNMET` 是「陈述是目标态、代码还没做到」的登记表——它让差距可见，
    但不让新增差距蒙混过关。
    """
    bad = [(k, d) for k, ok, d in system_claims.verify_all() if not ok]
    new = [(k, d) for k, d in bad if k not in system_claims.UNMET]
    assert not new, "以下陈述与代码不符且未登记 UNMET：\n" + "\n".join(
        f"  - {k}：{d}" for k, d in new)


def test_unmet_is_honest():
    """`UNMET` 里的必须真的还不成立——修好了不删也红（否则表会烂掉）。"""
    bad = {k for k, ok, _ in system_claims.verify_all() if not ok}
    fixed = set(system_claims.UNMET) - bad
    assert not fixed, (
        "这些陈述代码已经做到了，请从 UNMET 删掉：\n"
        + "\n".join(f"  - {k}" for k in sorted(fixed)))


def test_unmet_has_reason():
    """每条未达成都要写清「什么时候能清掉」，否则它会烂在那。"""
    for key, why in system_claims.UNMET.items():
        assert len(str(why).strip()) >= 10, f"{key} 的 UNMET 说明太短"


def test_manual_list_is_honest():
    """`MANUAL` 里只能放真的没有 verify 的条目，不许拿它兜失败。"""
    by_key = {c.key: c for c in system_claims.claims()}
    for key in system_claims.MANUAL:
        assert key in by_key, f"MANUAL 里的 {key} 不存在于 CLAIMS"
        assert by_key[key].verify is None, (
            f"{key} 已经有 verify 了，请从 MANUAL 删掉")
    no_verify = {c.key for c in system_claims.claims() if c.verify is None}
    assert no_verify == set(system_claims.MANUAL), (
        f"有人加了无验证的陈述却没登记 MANUAL：{no_verify - set(system_claims.MANUAL)}")


def test_every_claim_has_evidence():
    """每条陈述都要写清它对应代码的哪里——没有归属的陈述不许进说明书。"""
    for c in system_claims.claims():
        assert c.evidence.strip(), f"{c.key} 缺 evidence"
        assert len(c.text.strip()) >= 12, f"{c.key} 陈述太短"


def test_render_contains_every_claim():
    """每条陈述的原文都必须出现在渲染结果里（不许写了不渲染）。"""
    text = system_claims.render_claims()
    for c in system_claims.claims():
        assert c.text.strip() in text, f"{c.key} 的原文没出现在渲染结果里"


def test_render_has_no_truncation():
    """任何一行都不许以省略号结尾（用户铁律：截断是最低级机制）。"""
    for line in system_claims.render_claims().split("\n"):
        s = line.rstrip()
        assert not s.endswith(("…", "...")), f"出现截断：{s[:60]}"


def test_no_charter_dump():
    """别的卡的提示词里不许整段倒 charter（那是职能自己的职责书）。"""
    from src.api import prompt_facts

    text = system_claims.render_claims()
    for f in prompt_facts.functions():
        charter = str(f.get("charter") or "")
        if len(charter) > 80:
            assert charter[:80] not in text, (
                f"职能 {f.get('code')} 的 charter 被整段倒进说明书了")


def test_size_sane():
    n = len(system_claims.render_claims())
    assert 800 <= n <= 4000, f"说明书 {n} 字，超出合理区间"


def test_sections_all_present():
    text = system_claims.render_claims()
    for sec in system_claims.SECTIONS:
        assert system_claims.SECTION_TITLE[sec] in text, f"缺章节：{sec}"


def test_claim_keys_unique():
    keys = [c.key for c in system_claims.claims()]
    assert len(keys) == len(set(keys)), f"claim key 重复：{keys}"


# ---------------------------------------------------------------- 反查自测
def test_coverage_catches_new_region(monkeypatch):
    """反查自身有效性：凭空加一个上下文区，必须被抓出来。

    没有这条，`_v_context_regions_present` 一旦退化成恒真，没人会发现。
    """
    from src.api import context_regions as cr

    fake = dict(cr.REGION_TABLE[0])
    fake["key"] = "brand_new_region"
    monkeypatch.setattr(cr, "REGION_TABLE", list(cr.REGION_TABLE) + [fake])
    ok, detail = system_claims._v_context_regions_present()
    assert not ok and "brand_new_region" in detail, detail


def test_coverage_catches_unbound_kind(monkeypatch):
    """加了一类信息但没写对应陈述 ⇒ 必须被抓出来。"""
    monkeypatch.setitem(system_claims.REGION_KIND, "instruction", "weird_kind")
    ok, detail = system_claims._v_context_regions_present()
    assert not ok and "weird_kind" in detail, detail


def test_coverage_catches_stale_region(monkeypatch):
    """REGION_KIND 里有已删掉的区 ⇒ 必须报（否则表会烂掉）。"""
    monkeypatch.setitem(system_claims.REGION_KIND, "ghost_region", "rule")
    ok, detail = system_claims._v_context_regions_present()
    assert not ok and "ghost_region" in detail, detail
