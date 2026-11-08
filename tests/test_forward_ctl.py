# -*- coding: utf-8 -*-
"""[多进程拆分 2026-11-08 · 评审热循环实弹钉死]

``forward_ctl`` 两个洞的回归：

1. URL 必须带 ``/api`` 前缀——api_router 挂 ``/api/ctl/*``，裸 ``/ctl/*``
   恒 404；改前返回的 ``{"detail": "Not Found"}`` 无 ``error`` 键，
   ``_seat_turn`` 判不出失败 ⇒ 席位回合"秒完不投票"⇒ 每秒一圈热循环
   （work_027 三场 66 万 seq 实证）。
2. 非 2xx 一律折成 ``{"error": {...}}``——错误必须长成错误的样子，
   下游 err 判据 + 防死循环闸才有刹车。
"""
import asyncio

from src.infra import procs


class _FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _FakeClient:
    """记 URL、回预设响应的 httpx.AsyncClient 替身。"""

    last_url = ""
    resp = _FakeResp()
    raise_http = None

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        type(self).last_url = url
        if type(self).raise_http is not None:
            raise type(self).raise_http
        return type(self).resp


def _patch(monkeypatch):
    monkeypatch.setattr(procs.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(procs, "owner_index_of", lambda w, c: 0)


def test_forward_url_has_api_prefix(monkeypatch):
    _patch(monkeypatch)
    _FakeClient.resp = _FakeResp(200, {"ok": True})
    r = asyncio.run(procs.forward_ctl("w", "c", "/ctl/run_message", {}))
    assert r == {"ok": True}
    assert _FakeClient.last_url.endswith("/api/ctl/run_message"), _FakeClient.last_url


def test_forward_404_folds_to_error_dict(monkeypatch):
    _patch(monkeypatch)
    _FakeClient.resp = _FakeResp(404, {"detail": "Not Found"}, "Not Found")
    r = asyncio.run(procs.forward_ctl("w", "c", "/ctl/run_message", {}))
    assert isinstance(r.get("error"), dict)
    assert "404" in str(r["error"].get("message") or "")


def test_forward_unreachable_is_error_dict(monkeypatch):
    import httpx as _hx
    _patch(monkeypatch)
    _FakeClient.raise_http = _hx.ConnectError("refused")
    r = asyncio.run(procs.forward_ctl("w", "c", "/ctl/deliver", {}))
    assert isinstance(r.get("error"), dict)
    _FakeClient.raise_http = None


def test_seat_turn_error_extraction_handles_string_and_dict():
    """_seat_turn 的 error 提取对 str/dict 两种形态都得出非空 err。"""
    src = open("src/api/review_runner.py", encoding="utf-8").read()
    # 钉的是实现形态：字符串 error 不能再走 .get("message")（会崩）。
    assert "isinstance(_e, dict)" in src
    for res in ({"error": "boom"}, {"error": {"message": "boom"}},
                {"error": {"code": "x", "message": "boom"}}):
        _e = res.get("error")
        err = str(_e.get("message") if isinstance(_e, dict) else _e or "")
        assert err == "boom"
