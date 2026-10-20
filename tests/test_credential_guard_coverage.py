# -*- coding: utf-8 -*-
"""值闸的**采集覆盖**机械用例（card_013 返工）。

背景（已取证的现场）：值闸的过滤与单文件解析都是对的，断在**覆盖**——
旧实现用「全盘共用一个 400 个值的上限」，而 os.walk 从 `D:\\AI_station` 顶层开始，
扫到本项目之前就被 `.workbuddy` 的证书包吃满（实测那一个目录 12672 个可采值），
于是本项目 `.env` 的 key **从未进过采集集**，`redact_output` 命中 0 处、
组合绕过 `open('.en'+'v').read()` 的明文原样回显。

本文件三条用例分别钉住三件不同的事：

1. `test_project_env_survives_cert_noise_under_old_cap` —— **真实布局压力**：
   同一棵树里既有大量证书包噪声（自身就超过旧上限 400），又有项目 `.env`；
   项目 `.env` 的假值必须进采集集、且脱敏真的生效。**这条在旧代码上必失败。**
2. `test_project_tier_not_starved_by_station_budget` —— **结构性保证**：
   噪声多到吃满 station 层的补充预算（2000 个值）也挤不掉项目层的值
   ——按来源分层，而不是按「先到先得」封顶。
3. `test_real_project_env_values_are_collected` —— **回归红线**：拿本项目**真实**
   的 `.env` 与真实布局跑，只断言布尔与计数，**不把明文值写进测试文件或输出**。

沙箱：conftest 的 autouse fixture 把 `file_tools.BASE_DIR` 搬进 tmp_path；
前两条只在沙箱里摊假值，第 3 条显式把 BASE_DIR 指回真实路径**只读**一遍。
"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from src.api import card_tools, credential_guard, file_tools

OLD_GLOBAL_CAP = 400          # 旧实现的全局个数上限（本文件用它当"压力基准"）
FAKE_KEY = "sk-fake-5713-noisecup-0001"   # 假值：绝不写真 key


def _pem(index: int, body_lines: int = 12) -> str:
    """造一份"证书包"：每行 64 字符长串，`_LONG_TOKEN_RE` 会逐个采成候选值。

    每行内容必须**各不相同**（首版从固定的 seed 前缀里切前 64 字符，12 行全一样，
    采集集 set 一去重就只剩 1 个值/file，压力根本没造出来）。
    """
    lines = []
    for i in range(body_lines):
        blob = (f"noise-{index}-{i}-cert-body-" * 8).encode("utf-8")
        lines.append(base64.b64encode(blob).decode("ascii")[:64])
    return ("-----BEGIN CERTIFICATE-----\n" + "\n".join(lines)
            + "\n-----END CERTIFICATE-----\n")


def _plant_noise(base: Path, files: int, body_lines: int = 12) -> Path:
    """在 `base/.workbuddy/certs` 摊噪声证书（名字刻意模仿真实 station 的布局）。"""
    noise = base / ".workbuddy" / "certs"
    noise.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (noise / f"cert_{i:04d}.pem").write_text(_pem(i, body_lines), encoding="utf-8")
    return noise


# ------------------------------------------------- 1 真实布局压力（旧代码必失败）
def test_project_env_survives_cert_noise_under_old_cap(sandbox):
    """项目 `.env` 的值必须在证书包噪声之下活下来（值与脱敏两件一起断言）。"""
    base = sandbox.base
    noise = _plant_noise(base, files=40)          # 40 × 12 行 ≈ 480 个可采值
    proj = base / "推演agent"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / ".env").write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    credential_guard.reset_cache()

    # 先自证"压力成立"：噪声自身的可采值就已经超过旧上限 ⇒ 旧实现必在扫到
    # 项目 .env 之前被吃满（os.walk 会先走到 .workbuddy，与真实 station 同序）。
    noise_values = sum(len(credential_guard._harvest_file(p))
                       for p in noise.iterdir())
    assert noise_values > OLD_GLOBAL_CAP, (
        f"噪声只造出 {noise_values} 个可采值，压不住旧上限 {OLD_GLOBAL_CAP}；"
        "用例失去意义")

    values = credential_guard.secret_values()
    assert FAKE_KEY in values, (
        f"项目 .env 的值没进采集集（采到 {len(values)} 个值，噪声 {noise_values} 个）"
        "——覆盖断了，组合绕过会明文回显")

    text, hits = credential_guard.redact_output(f"DEEPSEEK_API_KEY={FAKE_KEY}")
    assert hits >= 1, f"脱敏命中 {hits} 处"
    assert FAKE_KEY not in text and credential_guard.REDACTED in text, text


# --------------------------------------- 2 结构性保证：补充预算不许挤掉项目层
def test_project_tier_not_starved_by_station_budget(sandbox, monkeypatch):
    """噪声吃满 station 层预算（>2000 个值），项目层的值仍必须在内。"""
    base = sandbox.base
    proj = base / "推演agent"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / ".env").write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    noise = _plant_noise(base, files=200)         # 200 × 12 ≈ 2400 个值
    credential_guard.reset_cache()

    # 把"项目根"钉成沙箱里的 推演agent，复现真实层级（BASE_DIR 之上的 station 根
    # 与项目根是两个目录）；不钉的话沙箱里推导不出项目根。
    monkeypatch.setattr(credential_guard, "project_root", lambda: proj)

    tiers = credential_guard._scan_tiers(base)
    assert tiers[0][0] == "project", [t[0] for t in tiers]
    assert all(proj in p.parents for p in tiers[0][1]), "项目层必须是项目树里的文件"
    assert any(noise in p.parents for p in tiers[1][1]), "噪声应落在 station 层"

    values = credential_guard.secret_values()
    assert FAKE_KEY in values, (
        f"station 层预算把项目层的值挤掉了（共 {len(values)} 个值）")


# --------------------------------------------------- 3 端到端：exec 路径同一段绕法
async def test_python_tool_output_redacted_under_real_layout_noise(sandbox):
    """`python` 工具真的跑出来一段绕法代码，返回值必须已脱敏（端到端，非只测采集集）。

    这就是任务书点名的组合绕过：`open('.en' + 'v')` 能过静态闸，只能靠值闸兜住。
    与上面两条的区别：这条走完 `_h_python` 全链路（落临时 .py → 子进程 → 输出截断
    → 值脱敏），断言的是**工具返回值**而不是内部集合。
    """
    base = sandbox.base
    _plant_noise(base, files=40)
    proj = base / "推演agent"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / ".env").write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    credential_guard.reset_cache()

    code = "print(open('推演agent/.en' + 'v', encoding='utf-8').read())"
    assert credential_guard.find_credential_refs(code) == [], "静态闸不该看见它（这就是缺口）"

    cards = {"c1": {"card_id": "c1", "card_type": "sub_project",
                    "perms": ["read", "edit", "tools_file"]}}
    r = await card_tools._h_python("w", "c1", {"code": code}, cards)
    blob = str(r.get("stdout", "")) + str(r.get("stderr", ""))
    assert r.get("exit_code") == 0, r
    assert FAKE_KEY not in blob, f"明文回显了：{blob[:200]}"
    assert credential_guard.REDACTED in blob, blob


# --------------------------------------------------- 4 回归红线：真 .env、真布局
def test_real_project_env_values_are_collected(monkeypatch):
    """本项目真实 `.env` 的值必须出现在采集集里（只断言布尔与计数，不落明文）。"""
    real_base = Path(r"D:\AI_station")
    env = real_base / "推演agent" / ".env"
    if not env.is_file():
        pytest.skip("本机没有真实 .env，跳过红线用例")

    monkeypatch.setattr(file_tools, "BASE_DIR", real_base)
    credential_guard.reset_cache()
    try:
        expected = credential_guard._harvest_file(env)
        assert expected, "真实 .env 应当至少解析出一个可采值"
        got = set(credential_guard.secret_values())
        missing = expected - got
        # 只比计数：断言失败时 pytest 会把被比较的对象打出来，而这里装的是真值。
        assert len(missing) == 0, (
            f"真实 .env 有 {len(missing)}/{len(expected)} 个值未被采集（覆盖断了）")

        # 掩码断言：脱敏真的生效，但只报长度与前缀，不落明文
        sample = max(expected, key=len)
        text, hits = credential_guard.redact_output(f"KEY={sample}")
        assert hits >= 1, "真实 .env 的值脱敏命中 0 处"
        assert sample not in text
        assert f"len={len(sample)}"  # 掩码形态仅供人看，断言只认布尔
    finally:
        credential_guard.reset_cache()