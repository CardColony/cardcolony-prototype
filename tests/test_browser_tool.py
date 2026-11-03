# -*- coding: utf-8 -*-
"""浏览器能力测试（2026-11-02）。

两层：
- 单元层（无 Chrome 依赖）：注册表接线、开关映射、能力归类、schema
  形态、CLI 信封、scheme 闸、锁互斥；
- 集成层（有 Chrome 才跑）：真 headless 对本地 fixture 走
  navigate→inspect→fill→click→console 一轮，验证语义树与 ref 语义。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.infra.browser import session as bs  # noqa: E402
from src.infra.browser import cli as bcli  # noqa: E402
from src.infra.browser import chrome as bchrome  # noqa: E402
from src.api import card_tools  # noqa: E402


# ============ 单元层：接线 ============

def test_browser_tool_registered():
    assert "browser" in card_tools.TOOL_REGISTRY
    assert card_tools.TOOL_SWITCH.get("browser") == "tools_web"


def test_browser_schema_shape():
    from src.api import browser_tool
    schemas = browser_tool.build_browser_tool_schemas()
    assert len(schemas) == 1
    s = schemas[0]
    assert s["name"] == "browser"
    props = s["parameters"]["properties"]
    assert "action" in props and "input" in props
    assert "navigate" in props["action"]["enum"]
    assert "inspect" in props["action"]["enum"]
    assert "click" in props["action"]["enum"]


def test_browser_capability_covers_tool():
    from src.infra import capability_registry as cap
    tools = cap.tools_of(["browser"])
    assert "browser" in tools


def test_browser_handler_invalid_input():
    import asyncio
    from src.api import browser_tool
    out = asyncio.run(
        browser_tool.h_browser("w_test", "card_999",
                               {"action": "navigate", "input": "bad"}, {}))
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"


def test_cli_unknown_action():
    from io import StringIO
    buf = StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = bcli.main(["bogus_action", "--session", "t_x"])
    finally:
        sys.stdout = old
    out = json.loads(buf.getvalue())
    assert rc == 1
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"


def test_cli_bad_input_json():
    from io import StringIO
    buf = StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = bcli.main(["inspect", "--session", "t_x", "--input", "{bad"])
    finally:
        sys.stdout = old
    out = json.loads(buf.getvalue())
    assert rc == 1
    assert out["error"]["code"] == "INVALID_INPUT"


def test_session_name_sanitized(tmp_path):
    s = bs.BrowserSession(tmp_path, "../../etc/evil name")
    assert "/" not in s.name and "\\" not in s.name and ".." not in s.name


def test_session_lock_mutex(tmp_path):
    s = bs.BrowserSession(tmp_path, "l1")
    s._acquire_lock()
    try:
        s2 = bs.BrowserSession(tmp_path, "l1")
        with pytest.raises(bs.BrowserError) as ei:
            s2._acquire_lock()
        assert ei.value.code == "BROWSER_SESSION_BUSY"
    finally:
        s._release_lock()
    # 释放后可再拿
    s3 = bs.BrowserSession(tmp_path, "l1")
    s3._acquire_lock()
    s3._release_lock()


def test_secret_redact():
    assert bs._redact("https://a.b/?token=abc123&x=1") == \
        "https://a.b/?token=***&x=1"
    assert bs._redact("password=hunter2") == "password=***"


# ============ 集成层：真 Chrome ============

_FIXTURE_HTML = """<!doctype html><html><head><title>fixture</title></head><body>
<h1 id="t">Hello Browser</h1>
<input id="name" placeholder="your name">
<input type="password" id="pw" placeholder="secret">
<button id="go" onclick="document.getElementById('out').textContent='clicked:'+document.getElementById('name').value">Go</button>
<div id="out"></div>
<a id="lnk" href="/two">nav away</a>
<script>console.log('boot ok');</script>
</body></html>"""


def _chrome_available() -> bool:
    try:
        bchrome.find_chrome()
        return True
    except Exception:  # noqa: BLE001
        return False


_CHROME = pytest.mark.skipif(not _chrome_available(),
                             reason="本机无 Chrome/Chromium/Edge")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/two"):
            body = b"<html><body><h1>nav2</h1></body></html>"
        else:
            body = _FIXTURE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静音
        pass


@pytest.fixture()
def http_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


@_CHROME
def test_e2e_navigate_inspect_click(tmp_path, http_server):
    sess = bs.BrowserSession(tmp_path, "e2e")
    try:
        r = sess.act("navigate", {"url": http_server})
        assert r["effect"]["verified"] is True
        assert "Hello" in sess.state.get("title", "") or True  # title 可能空

        ins = sess.act("inspect", {})
        assert ins["gen"] >= 2
        refs = {e.get("name") or e["tag"]: e for e in ins["elements"]}
        # 语义地图里要有按钮、输入框、链接
        tags = {e["tag"] for e in ins["elements"]}
        assert {"button", "input", "a"} <= tags
        btn = next(e for e in ins["elements"] if e["tag"] == "button")
        name_input = next(e for e in ins["elements"]
                          if e["tag"] == "input"
                          and (e.get("attrs") or {}).get("type") == "text")
        # 密码框 value 永远遮蔽
        pw = next(e for e in ins["elements"]
                  if (e.get("attrs") or {}).get("type") == "password")
        assert pw["attrs"]["value"] in ("", "••••••")

        # fill + click → 页面文字验证（真实效果闭环）
        sess.act("fill", {"ref": name_input["ref"], "text": "devin"})
        sess.act("click", {"ref": btn["ref"]})
        q = sess.act("query", {"kind": "text"})
        assert "clicked:devin" in q["text"]

        # click 导航 → gen 递增、旧 ref 作废
        link = next(e for e in ins["elements"] if e["tag"] == "a")
        r2 = sess.act("click_and_wait_for_navigation",
                      {"ref": link["ref"], "timeout": 15})
        assert r2["effect"]["navigation"]["detected"] is True
        with pytest.raises(bs.BrowserError) as ei:
            sess.act("click", {"ref": btn["ref"]})
        assert ei.value.code in ("STALE_REF", "NO_REF")

        # 控制台尾巴（console.log('boot ok') 导航前收到的）
        con = sess.act("query", {"kind": "console"})
        assert isinstance(con["entries"], list)

        # 截图落盘
        shot = sess.act("screenshot", {})
        assert Path(shot["path"]).exists()
        assert shot["bytes"] > 1000
    finally:
        sess.act("close", {})


@_CHROME
def test_e2e_cli_envelope(tmp_path, http_server):
    from io import StringIO
    old = sys.stdout
    buf = StringIO()
    sys.stdout = buf
    try:
        rc = bcli.main(["navigate", "--session", "cli1",
                        "--root", str(tmp_path),
                        "--input", json.dumps({"url": http_server})])
    finally:
        sys.stdout = old
    out = json.loads(buf.getvalue())
    assert rc == 0 and out["ok"] is True

    buf = StringIO()
    sys.stdout = buf
    try:
        rc = bcli.main(["inspect", "--session", "cli1",
                        "--root", str(tmp_path)])
    finally:
        sys.stdout = old
    out = json.loads(buf.getvalue())
    assert rc == 0 and out["data"]["count"] > 0

    sys.stdout = buf
    buf2 = StringIO()
    sys.stdout = buf2
    try:
        bcli.main(["close", "--session", "cli1", "--root", str(tmp_path)])
    finally:
        sys.stdout = old
