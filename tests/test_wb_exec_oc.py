# -*- coding: utf-8 -*-
"""[wb_exec · opencode 执行体 2026-10-25] 测试。

**覆盖**：
- ``_parse_pinned``：两段/三段都认（前端 options 的值带 ``wb:`` 前缀）；
- ``routes._wb_exec_options``：生成的 value **必须**带 ``wb:`` 前缀
  （否则 ``put_exec_backend`` 的校验会 400 ⇒ **卡面钉死执行体一直是失效的**）；
- ``oc_pool.write_provider_config``：**自定义 provider + 浏览器 UA**
  （否则 opencode 自己被 Cloudflare 1010 拦 ⇒ 连已付费订阅内的模型也挂）；
- ``oc_client``：建会话/发任务/取正文/事件过滤（**假 server**，不联网）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.api.wb_exec import acp_client, bridge, config as wb_config
from src.api.wb_exec import oc_client as oc_mod
from src.api.wb_exec import oc_pool


# ================================================================ 钉死格式
def test_parse_pinned_accepts_both_formats():
    """``wb:<版本>:<模型>``（新）**与** ``<版本>:<模型>``（旧、前端早先发的）都认。

    ★ 2026-10-25 实测：前端 options 的 value 是 ``<executor>:<model>``（两段），
    而 ``_parse_pinned`` 早先只认三段 ⇒ **卡面手动钉死执行体一直是失效的**。
    """
    assert bridge._parse_pinned("wb:wb-oc:deepseek-v4.1-flash") == (
        "wb-oc", "deepseek-v4.1-flash")
    assert bridge._parse_pinned("wb-oc:deepseek-v4.1-flash") == (
        "wb-oc", "deepseek-v4.1-flash")
    assert bridge._parse_pinned("wb-cn:hy3") == ("wb-cn", "hy3")
    # 不是钉死格式 ⇒ 落回 auto（不是 None 的"错误"，而是"没钉"）
    assert bridge._parse_pinned("auto") is None
    assert bridge._parse_pinned("system") is None
    assert bridge._parse_pinned("") is None
    assert bridge._parse_pinned("garbage") is None


def test_exec_backend_options_carry_wb_prefix():
    """options 的 value **必须**带 ``wb:``（与 PUT 的校验一致）。

    ★ 2026-10-25 实测：早先生成的是 ``<executor>:<model>``（无前缀），
    而 ``put_exec_backend`` 只接受 ``auto``/``system``/``wb:*``
    ⇒ 前端下拉选任何一项都 400 ⇒ **钉死功能一直是坏的**。

    ⚠ 这里**读源码文本**而不 import ``routes``：``routes`` 会 import
    ``card_tools``（并行施工线正在改、当前语法不完整）⇒ 不能连带依赖。
    """
    src = (Path(__file__).resolve().parents[1] / "src" / "api" / "routes.py")
    text = src.read_text(encoding="utf-8")
    marker = "def _wb_exec_options("
    i = text.find(marker)
    assert i > 0, "routes.py 里找不到 _wb_exec_options"
    body = text[i:i + 900]
    assert 'f"wb:{c.executor}:{c.model}"' in body, (
        "options 的 value 必须带 wb: 前缀，否则 PUT 会被 400 拒"
        "（钉死执行体一直是失效的）")
    # 钉死的值必须能被后端解析
    assert bridge._parse_pinned("wb:wb-oc:deepseek-v4.1-flash") == (
        "wb-oc", "deepseek-v4.1-flash")


# ================================================================ provider 配置
def test_provider_config_has_browser_ua(tmp_path):
    """自定义 provider **必须带浏览器 UA**（2026-10-25 实测的关键）。

    opencode 自己的 Bun fetch 不带 UA ⇒ 被 Cloudflare 的 Browser Integrity
    Check 拦 ⇒ 403「error code: 1010」⇒ **连用户已付费的 Go 订阅内的模型
    也一起挂**。⇒ 自定义 provider 里显式塞 UA。
    """
    path = oc_pool.write_provider_config(
        str(tmp_path), provider_id="wb-direct",
        base_url="https://opencode.ai/zen/go/v1", api_key="KEY",
        models=["deepseek-v4.1-flash"])
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    prov = data["provider"]["wb-direct"]
    assert prov["options"]["baseURL"] == "https://opencode.ai/zen/go/v1"
    ua = prov["options"]["headers"]["User-Agent"]
    assert "Mozilla/5.0" in ua, ua
    assert "deepseek-v4.1-flash" in prov["models"]
    assert data["model"] == "wb-direct/deepseek-v4.1-flash"


def test_browser_ua_is_a_real_browser_string():
    """UA 得是**像浏览器的**（不是 python-httpx/urllib 那种）。"""
    assert "Mozilla/5.0" in oc_pool.BROWSER_UA
    assert "Chrome/" in oc_pool.BROWSER_UA


# ================================================================ 客户端契约
class _FakeOcServer:
    """最小 opencode server（**不联网**）：只实现我们要用的那几件事。"""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.prompts: list[dict] = []
        self.events: list[dict] = []
        self.aborted: list[str] = []

    def handle(self, method: str, path: str, body: dict | None):
        if method == "POST" and "/abort" in path:
            sid = path.split("/session/")[1].split("/")[0]
            self.aborted.append(sid)
            return 200, {}
        if method == "POST" and "/message" in path:
            sid = path.split("/session/")[1].split("/")[0]
            self.prompts.append({"sid": sid, "body": body})
            return 200, {"info": {"role": "assistant"}, "parts": []}
        if method == "POST" and "/prompt_async" in path:
            sid = path.split("/session/")[1].split("/")[0]
            self.prompts.append({"sid": sid, "body": body})
            return 204, {}
        if method == "GET" and "/message" in path:
            return 200, [{
                "info": {"role": "assistant", "id": "m1"},
                "parts": [
                    {"type": "step-start"},
                    {"type": "text", "text": "正文在此"},
                    {"type": "step-finish"},
                ],
            }]
        if method == "POST" and path.startswith("/session"):
            sid = "ses_fake1"
            self.sessions[sid] = {"id": sid, "directory": "D:/x"}
            return 200, {"id": sid, "directory": "D:/x"}
        return 404, {}


@pytest.mark.asyncio
async def test_oc_client_session_prompt_and_text(monkeypatch):
    """建会话（**一卡一目录**：directory 走 query）⇒ 发任务 ⇒ 取正文。"""
    fake = _FakeOcServer()

    async def _json(self, path, method="GET", body=None, timeout=180):
        st, data = fake.handle(method, path, body)
        if st >= 400:
            from src.api.wb_exec.oc_client import OcError
            raise OcError(f"{method} {path} → {st}")
        return st, data

    monkeypatch.setattr(oc_mod.OcClient, "_json", _json)
    c = oc_mod.OcClient("http://127.0.0.1:1")
    sid = await c.create_session("D:/card_dir",
                                 permission=oc_mod.FULL_ALLOW_PERMISSION)
    assert sid == "ses_fake1"
    await c.prompt(sid, "wb-direct", "deepseek-v4.1-flash", "干活", "D:/card_dir")
    assert fake.prompts and fake.prompts[0]["body"]["model"]["providerID"] == "wb-direct"
    msgs = await c.messages(sid, "D:/card_dir")
    assert oc_mod.session_text(msgs) == "正文在此"
    await c.abort(sid, "D:/card_dir")
    assert fake.aborted == ["ses_fake1"]


def test_full_allow_permission_shape():
    """全放行规则集：**数组**，三个字段都必填（写成对象 ⇒ 400 BadRequest）。"""
    p = oc_mod.FULL_ALLOW_PERMISSION
    assert isinstance(p, list) and p
    for rule in p:
        assert set(("permission", "pattern", "action")) <= set(rule)
        assert rule["action"] == "allow"


def test_session_text_picks_last_assistant_text():
    msgs = [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "问题"}]},
        {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "第一段"}]},
        {"info": {"role": "assistant"},
         "parts": [{"type": "text", "text": "最后一段"}]},
    ]
    assert oc_mod.session_text(msgs) == "最后一段"
    assert oc_mod.session_text([]) == ""
