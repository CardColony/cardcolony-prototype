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
import json

import httpx
import pytest

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


def test_fanin_merge_reports_partial_coverage(monkeypatch):
    """[任务三·合源 §3.2.3] 一片 runner 掉线：合并回包必须带
    ``coverage={ok,total,missing}`` 与 ``X-Fanin-Missing`` 头——前端据此
    区分「真没有」与「属主掉了」，不再误清缺失分片的状态。"""
    import json as _json
    import httpx as _hx

    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self.headers = {"content-type": "application/json"}
            self._p = payload

        def json(self):
            return self._p

    class _Client:
        async def get(self, url):
            i = int(url.split("://r")[1].split("/")[0])
            if i == 1:
                raise _hx.ConnectError("refused")
            return _Resp({"running": [{"work_id": "w", "card_id": f"c{i}"}]})

    monkeypatch.setattr(procs, "_RUNNER_COUNT", 3)
    monkeypatch.setattr(procs, "runner_url", lambda i: f"http://r{i}")
    monkeypatch.setattr(procs, "_loop_clients", lambda: {"short": _Client()})
    resp = asyncio.run(procs._fanin_get_merge("/api/nav/running", ""))
    assert resp.status_code == 200
    body = _json.loads(resp.body)
    assert body["coverage"] == {"ok": 2, "total": 3, "missing": [1]}
    assert body["running"]  # 可确认分片的数据照常合并
    # [任务三·属主标] 每条 running 自报来源分片——前端据此区分
    # 「确认分片里缺席⇒删」与「缺失分片⇒待校准」。
    owners = {x["card_id"]: x["_runner"] for x in body["running"]}
    assert owners == {"c0": 0, "c2": 2}, owners
    assert resp.headers["x-fanin-missing"] == "1"


def test_fanin_merge_full_coverage_marks_missing_empty(monkeypatch):
    """全片到齐：coverage.missing 为空——前端把「空 running」当真值清状态。"""
    import json as _json

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"running": []}

    class _Client:
        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(procs, "_RUNNER_COUNT", 2)
    monkeypatch.setattr(procs, "runner_url", lambda i: f"http://r{i}")
    monkeypatch.setattr(procs, "_loop_clients", lambda: {"short": _Client()})
    resp = asyncio.run(procs._fanin_get_merge("/api/nav/running", ""))
    body = _json.loads(resp.body)
    assert body["coverage"]["missing"] == []
    assert body["coverage"]["ok"] == 2
    assert resp.headers["x-fanin-missing"] == ""


def test_fanin_merge_unusable_200_counts_as_missing(monkeypatch):
    """[任务三·覆盖修正] 「已覆盖」只认成功解码且符合接口结构的响应：
    - runner0：200 但 content-type=text/html（代理/错误页）
    - runner1：200 json 但正文是坏 JSON（r.json() 抛错——旧实现整次扇入带崩）
    - runner2：200 json 且结构合法 ⇒ 唯一算覆盖的一片
    断言：coverage.missing=[0,1]、坏 JSON 不再让整次请求 500。"""
    import json as _json

    class _Resp:
        def __init__(self, status=200, ct="application/json", payload=None,
                     raise_json=False):
            self.status_code = status
            self.headers = {"content-type": ct}
            self._p = payload
            self._raise = raise_json

        def json(self):
            if self._raise:
                raise ValueError("bad json")
            return self._p

    class _Client:
        async def get(self, url):
            i = int(url.split("://r")[1].split("/")[0])
            if i == 0:
                return _Resp(ct="text/html", payload=None)
            if i == 1:
                return _Resp(raise_json=True)
            return _Resp(payload={"running": [{"work_id": "w",
                                               "card_id": "c2"}]})

    monkeypatch.setattr(procs, "_RUNNER_COUNT", 3)
    monkeypatch.setattr(procs, "runner_url", lambda i: f"http://r{i}")
    monkeypatch.setattr(procs, "_loop_clients", lambda: {"short": _Client()})
    resp = asyncio.run(procs._fanin_get_merge("/api/nav/running", ""))
    assert resp.status_code == 200          # 坏 JSON 没有带崩整次扇入
    body = _json.loads(resp.body)
    assert body["coverage"] == {"ok": 1, "total": 3, "missing": [0, 1]}
    assert [x["card_id"] for x in body["running"]] == ["c2"]
    assert body["running"][0]["_runner"] == 2
    assert resp.headers["x-fanin-missing"] == "0,1"


@pytest.mark.parametrize("path, valid, invalid", [
    ("/api/nav/running", {"running": []}, {"error": "not ready"}),
    ("/api/nav/running", {"running": []}, []),
    ("/api/nav/running", {"running": []}, {"running": [{"card_id": "c"}]}),
    ("/api/works/w/pulse", {"running": [], "recent": []}, {"running": {}}),
    ("/api/works/w/pulse", {"running": [], "recent": []}, {"running": [{}]}),
    ("/api/observe/calls", {"inflight": []}, {"running": []}),
])
def test_fanin_merge_requires_endpoint_snapshot(monkeypatch, path, valid, invalid):
    class _Client:
        async def get(self, url):
            payload = invalid if url.startswith("http://r1/") else valid
            return httpx.Response(200, json=payload)

    monkeypatch.setattr(procs, "_RUNNER_COUNT", 2)
    monkeypatch.setattr(procs, "runner_url", lambda i: f"http://r{i}")
    monkeypatch.setattr(procs, "_loop_clients", lambda: {"short": _Client()})
    resp = asyncio.run(procs._fanin_get_merge(path, ""))
    assert resp.status_code == 200
    assert resp.headers["x-fanin-missing"] == "1"
    assert json.loads(resp.body) == {
        **valid, "coverage": {"ok": 1, "total": 2, "missing": [1]},
    }
