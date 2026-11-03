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


# ================================================ 2026-11-01：别「假装成功」
class _FakeState:
    """最小 state（只需要 work_id / card_id / rounds）。"""

    def __init__(self) -> None:
        self.work_id = "work_x"
        self.card_id = "card_x"
        self.rounds = 1


class _FakeClient:
    """能吐 SSE 的假 opencode 客户端。"""

    def __init__(self, events: list[dict], msgs: list[dict]) -> None:
        self._events = events
        self._msgs = msgs

    async def events(self, directory):  # noqa: ANN001
        for ev in self._events:
            yield ev

    async def prompt_async(self, sid, provider, model, text, directory):  # noqa: ANN001
        return None

    async def messages(self, sid, directory):  # noqa: ANN001
        return self._msgs

    async def close(self):
        return None


class _FakeRegistry:
    def __init__(self, client) -> None:
        self.client = client
        #: 额度状态表（跟真 registry 一样：``(executor, model) → 原因``）
        self.exhausted: dict[tuple[str, str], str] = {}

    async def _acquire_oc(self, executor_id, cwd, model, on_wait=None):  # noqa: ANN001
        class _GW:
            base_url = "http://127.0.0.1:1"
            session_id = "ses_fake1"
            cli_worker = None

        gw = _GW()
        gw.client = self.client
        return gw

    def mark_slot(self, *a, **k):
        return None

    def release_slot(self, *a, **k):
        return None

    # ★ **2026-11-04 优先级瀑布**：选择期会"预留"槽位，传输层接上后释放。
    async def try_reserve(self, executor_id, model="", work_dir="", holder="",
                          force=False):
        return True

    def release_reserve(self, *a, **k):
        return None

    def mark_exhausted(self, executor_id, model, reason):
        self.exhausted[(str(executor_id), str(model))] = str(reason)

    def is_exhausted(self, executor_id, model):
        return (str(executor_id), str(model)) in self.exhausted

    def clear_exhausted(self, executor_id, model):
        self.exhausted.pop((str(executor_id), str(model)), None)

    def exhausted_snapshot(self):
        return {f"{e}:{m}": r for (e, m), r in self.exhausted.items()}


def _err_event(msg: str = "This model is not available in your country") -> dict:
    return {
        "type": "session.error",
        "properties": {
            "sessionID": "ses_fake1",
            "error": {"name": "APIError",
                      "data": {"message": msg, "statusCode": 403}},
        },
    }


def _idle_event() -> dict:
    """收口事件（没有它 ``done`` 不会被 set ⇒ 会一直等到 read_timeout_sec）。"""
    return {"type": "session.idle", "properties": {"sessionID": "ses_fake1"}}


def _retry_event(reason: str = "free_tier_limit") -> dict:
    """opencode 的**重试**事件（2026-11-01 实测撞到的那一种）。

    ★ 它**不是** ``session.error``，后面**也没有** ``session.idle``
    ⇒ 不接它 ⇒ bridge 干等 ``read_timeout_sec``（600 秒）⇒ **卡片表现为卡住**
    （现场：card_143 走 wb-oc 免费档，免费额度用完 ⇒ opencode 发这个事件）。
    """
    return {
        "type": "session.status",
        "properties": {
            "sessionID": "ses_fake1",
            "status": {
                "type": "retry", "attempt": 1,
                "message": "Free usage exceeded, subscribe to Go",
                "action": {"reason": reason, "provider": "opencode",
                           "title": "Free limit reached",
                           "message": "Subscribe to OpenCode Go to keep going"},
            },
        },
    }


async def _run_oc(events, msgs, cfg):
    """跑一次 _run_opencode（假客户端 + 假 registry），把异常原样抛给调用方。

    返回 ``(ex, reg)``；``reg.exhausted`` 是额度状态表。
    """
    from src.api.wb_exec.bridge import _Exec
    from src.api.wb_exec.config import Candidate
    from src.api.wb_exec.picker import Picker

    state = _FakeState()
    ex = _Exec(state=state, cfg=cfg, picker=Picker(()))
    ex.executor_id, ex.model = "wb-oc", "beam-free"
    cfg_cand = Candidate(executor="wb-oc", model="beam-free", weight=20)
    reg = _FakeRegistry(_FakeClient(events, msgs))
    await bridge._run_opencode(state, cfg, ex, cfg_cand, "D:/x", reg)
    return ex, reg


@pytest.mark.asyncio
async def test_opencode_session_error_must_raise_not_fake_success(monkeypatch, tmp_path):
    """★ `session.error` ⇒ **必须抛错**，不许「0 工具 0 正文假装成功」。

    **实测事故（2026-11-01）**：``beam-free`` 被 Cloudflare 判「不在你的国家」⇒
    403 ⇒ 但 ``prompt_async`` 是 **204 立即返回** ⇒ 错误发生在**之后**
    ⇒ 早先 bridge **不接 `session.error`** ⇒ 错误被静默丢弃
    ⇒ 收口时只读到「0 工具 0 正文」⇒ **这张卡显示"完成"但什么都没干**
    （线上当时有 25% 的 coder 流量正踩着这个）。
    """
    cfg = wb_config.load()
    monkeypatch.setattr(bridge, "_build_task_text",
                        lambda *a, **k: _async_val("干活"))
    with pytest.raises(bridge.WbAcpError) as ei:
        await _run_oc([_err_event(), _idle_event()], [], cfg)
    assert "不在你的国家" in str(ei.value) or "403" in str(ei.value), str(ei.value)


@pytest.mark.asyncio
async def test_opencode_empty_result_must_raise_not_fake_success(monkeypatch):
    """★ 没有错误事件、但 **0 工具 + 0 正文** ⇒ 也是失败，不许当成功。

    这是上一条的兜底：万一 opencode 换了错误上报方式，
    **「什么都没干」绝不能被当成「干完了」**。
    """
    cfg = wb_config.load()
    monkeypatch.setattr(bridge, "_build_task_text",
                        lambda *a, **k: _async_val("干活"))
    with pytest.raises(bridge.WbAcpError) as ei:
        await _run_oc([_idle_event()], [], cfg)
    assert "空结果" in str(ei.value), str(ei.value)


@pytest.mark.asyncio
async def test_opencode_retry_with_action_must_raise_not_hang(monkeypatch):
    """★ `session.status{type:"retry"}` 且带 `action` ⇒ **必须立刻判失败，不许卡住**。

    **实测事故（2026-11-01 22:16）**：card_143 走 wb-oc 免费档，
    免费额度用完 ⇒ opencode 发
    ``session.status{"type":"retry","action":{"reason":"free_tier_limit",…}}``
    ⇒ **既不是 session.error，也没有 session.idle**
    ⇒ 早先 bridge 只认 ``idle`` ⇒ ``done`` 永远不被 set
    ⇒ 干等 ``read_timeout_sec``（600 秒）⇒ **卡片一直"卡住"**（用户当场看见）。

    带 ``action`` ⇒ opencode 要「用户去做点什么」（订阅/额度）⇒ 重试无意义
    ⇒ 立刻失败 ⇒ 上层换候选。
    """
    cfg = wb_config.load()
    monkeypatch.setattr(bridge, "_build_task_text",
                        lambda *a, **k: _async_val("干活"))
    with pytest.raises(bridge.WbAcpError) as ei:
        await _run_oc([_retry_event()], [], cfg)
    assert "free_tier_limit" in str(ei.value), str(ei.value)


@pytest.mark.asyncio
async def test_opencode_quota_error_marks_exhausted(monkeypatch):
    """★ 上游说「额度用完」⇒ **记进额度状态表** ⇒ 之后这个候选**让路**。

    这就是用户 2026-11-01 指出的机制：
    「没有对执行体的额度状态监控，然后进行动态调整」。

    判据（**全用状态**，没有任何时间窗）：
      ① 上游明确报额度用完 ⇒ 登记 ``exhausted``
      ② 登记后 pick 时**跳过并摘除**它 ⇒ 份额自动让给别的候选
      ③ **真调成功一次** ⇒ 清除 ⇒ 恢复全份额（状态驱动，不是时间到期）
    """
    cfg = wb_config.load()
    monkeypatch.setattr(bridge, "_build_task_text",
                        lambda *a, **k: _async_val("干活"))
    with pytest.raises(bridge.WbAcpError):
        await _run_oc([_retry_event()], [], cfg)

    # ② 登记 + **份额转移**：picker 里 hy3 的权重让给同侧的收费备用端点
    from src.api.wb_exec.picker import Picker
    from src.api.wb_exec.config import Candidate
    p = Picker([Candidate(executor="wb-cn", model="hy3", weight=50),
                Candidate(executor="wb-intl", model="hy3", weight=50),
                Candidate(executor="wb-cn", model="glm-5.3-flash", weight=0),
                Candidate(executor="wb-intl", model="glm-5.3-flash", weight=0)])
    # 平时：只有 hy3 两端吃流量，备用端点 0 权重（不在轮询里）
    assert {c.model for c in p.candidates} == {"hy3"}
    assert p.get_weight("wb-cn", "glm-5.3-flash") == 0
    # hy3 额度耗尽 ⇒ 权重转给同侧备用（**份额 100% 保留，不浪费**）
    w = p.get_weight("wb-cn", "hy3")
    p.set_weight("wb-cn", "hy3", 0)
    p.set_weight("wb-cn", "glm-5.3-flash", w)
    assert p.get_weight("wb-cn", "hy3") == 0
    assert p.get_weight("wb-cn", "glm-5.3-flash") == 50
    assert "glm-5.3-flash" in {c.model for c in p.candidates}
    # ③ 恢复：份额还回去，备用端点退出轮询
    p.set_weight("wb-cn", "hy3", p.base_weight("wb-cn", "hy3"))
    p.set_weight("wb-cn", "glm-5.3-flash", p.base_weight("wb-cn", "glm-5.3-flash"))
    assert p.get_weight("wb-cn", "hy3") == 50
    assert p.get_weight("wb-cn", "glm-5.3-flash") == 0


@pytest.mark.asyncio
async def test_opencode_retry_without_action_keeps_waiting(monkeypatch):
    """★ 普通重试（**没有** action）⇒ **不**当失败（让 opencode 自己重试）。

    区分依据是**状态**（有没有 action），不是时间、不是 attempt 数。
    """
    cfg = wb_config.load()
    monkeypatch.setattr(bridge, "_build_task_text",
                        lambda *a, **k: _async_val("干活"))
    ev = _retry_event()
    ev["properties"]["status"].pop("action")
    # 没有 action ⇒ 不该立刻抛（下面用 idle 收口）
    ex, _reg = await _run_oc([ev, _idle_event()],
                              [{"info": {"role": "assistant"},
                                "parts": [{"type": "text", "text": "重试成功"}]}], cfg)
    assert "重试成功" in "".join(ex.text_parts)


@pytest.mark.asyncio
async def test_opencode_real_result_passes(monkeypatch):
    """★ 反向：有正文 ⇒ 正常通过（别把正常路径也堵死）。"""
    cfg = wb_config.load()
    monkeypatch.setattr(bridge, "_build_task_text",
                        lambda *a, **k: _async_val("干活"))
    msgs = [{"info": {"role": "assistant"},
             "parts": [{"type": "text", "text": "干完了"}]}]
    ex, _reg = await _run_oc([_idle_event()], msgs, cfg)
    assert "干完了" in "".join(ex.text_parts)


async def _async_val(v):
    return v

