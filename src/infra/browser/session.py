# -*- coding: utf-8 -*-
"""BrowserSession：会话状态 + 动作派发 + 效果校验（纯标准库）。

一个会话＝一个独立 Chrome profile + 一个活动页 target。状态落
``<root>/<name>/session.json``，CLI 每次调用重连仍在跑的 Chrome——
WB 执行体逐条 Bash 调、平台工具逐次工具调用，走的都是同一份持久态。

ref 代次（对齐 minimax）：每次成功导航 gen+1；页面自行跳走或 reload
时 window.__cb_refs 消失，旧 ref 解析直接 STALE——不透明 ref 只在
签发它的那次快照上有效。

串行纪律（对齐 minimax BROWSER_SESSION_BUSY）：session.lock 原子创建，
持锁期间拒绝并发；锁龄 >120s 视为死锁清掉。

效果校验（对齐 minimax）：click 只证明「输入已派发」——返回
effect.verified=false + verificationRequired，页面是否如愿要再
inspect 看；fill/check/select/scroll 回报可验证的权威 effect。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from . import cdp, chrome, serializer

ACTIONS = (
    "navigate", "inspect", "query", "screenshot",
    "click", "double_click", "click_and_wait_for_navigation",
    "hover", "fill", "type", "press_key", "check", "uncheck",
    "select_option", "scroll", "scroll_into_view",
    "wait", "wait_for_text", "wait_for_selector", "wait_for_navigation",
    "back", "forward", "reload", "new_tab", "tabs", "switch_tab",
    "status", "close",
)

_ALLOWED_SCHEMES = ("http://", "https://", "file://", "about:", "data:")
_SECRET_RE = re.compile(
    r"((?:key|token|secret|password|passwd|pwd|auth)[\w\-]*=)[^&\s\"']+",
    re.IGNORECASE)

_LOCK_STALE_S = 120.0
_DEFAULT_TIMEOUT_S = 30.0
_MAX_TIMEOUT_S = 120.0
_ELEMENT_PAGE = 100
_TEXT_LIMIT = 16000
_DOM_LIMIT = 32000
_TAIL_CAP = 200


class BrowserError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _redact(text: str) -> str:
    return _SECRET_RE.sub(r"\1***", str(text))


def _clip(text: str, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def _now() -> float:
    return time.time()


class BrowserSession:
    """一个浏览器会话（懒连接：connect() 时才起 Chrome/重连）。"""

    def __init__(self, root: str | Path, name: str):
        # 名字只留 \w 和 -（点号也不放——杜绝 .. 路径段的一切解释空间）
        self.name = re.sub(r"[^\w\-]", "_", str(name)) or "default"
        self.dir = Path(root) / self.name
        self.profile = self.dir / "profile"
        self.artifacts = self.dir / "artifacts"
        self._state_path = self.dir / "session.json"
        self._lock_path = self.dir / "session.lock"
        self._conn: cdp.CDPConnection | None = None
        self._session_id = ""
        self.state = self._load_state()

    # ---- 持久态 ----

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"name": self.name, "pid": 0, "port": 0, "ws_url": "",
                    "target_id": "", "gen": 1, "url": "about:blank",
                    "title": "", "created_at": _now(),
                    "console_tail": [], "net_tail": [], "shot_n": 0,
                    "snap_n": 0}

    def _save_state(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self._state_path)

    # ---- 锁 ----

    def _acquire_lock(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            st = self._lock_path.stat()
            if _now() - st.st_mtime > _LOCK_STALE_S:
                self._lock_path.unlink()
        except OSError:
            pass
        try:
            fd = os.open(str(self._lock_path),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {_now()}".encode())
            os.close(fd)
        except FileExistsError:
            raise BrowserError(
                "BROWSER_SESSION_BUSY",
                f"会话 {self.name} 正被另一个动作占用（串行纪律）")

    def _release_lock(self) -> None:
        try:
            self._lock_path.unlink()
        except OSError:
            pass

    # ---- 连接 ----

    def _alive(self) -> bool:
        port = int(self.state.get("port") or 0)
        host = str(self.state.get("dbg_host") or "127.0.0.1")
        return port > 0 and chrome.port_alive(port, host=host)

    def _connect(self) -> None:
        """attach 到活动页 target；Chrome 不在则拉起。"""
        if not self._alive():
            chrome_path = chrome.find_chrome()
            self.state["chrome_path"] = chrome_path
            proc, prof_used, dbg_host = chrome.launch(
                chrome_path, self.profile, headless=True)
            port, ws_url = chrome.read_devtools_ws(prof_used, host=dbg_host)
            self.state.update({"pid": proc.pid, "port": port,
                               "ws_url": ws_url, "dbg_host": dbg_host})
        if self._conn is None:
            self._conn = cdp.CDPConnection(self.state["ws_url"])
        self._attach_page()

    def _attach_page(self) -> None:
        """attach 到已记录 target；找不到就挑第一个 page 或新建。"""
        tid = str(self.state.get("target_id") or "")
        targets = [t for t in self._conn.list_targets()
                   if t.get("type") == "page"]
        have = any(t.get("targetId") == tid for t in targets)
        if not have:
            if targets:
                tid = str(targets[0].get("targetId"))
            else:
                tid = self._conn.create_target()
            self.state["target_id"] = tid
        self._session_id = self._conn.attach(tid)
        self._conn.enable_session_domains(self._session_id)
        self._save_state()

    def _send(self, method: str, params: dict | None = None,
              timeout: float = 30.0) -> dict:
        assert self._conn is not None and self._session_id
        return self._conn.send(method, params,
                               session_id=self._session_id, timeout=timeout)

    def _eval(self, expr: str, timeout: float = 15.0) -> object:
        assert self._conn is not None
        return self._conn.evaluate(self._session_id, expr, timeout=timeout)

    # ---- 事件尾巴（跨 CLI 调用留存）----

    _CONSOLE_METHODS = ("Runtime.consoleAPICalled", "Log.entryAdded",
                        "Runtime.exceptionThrown")
    _NET_METHODS = ("Network.requestWillBeSent",
                    "Network.responseReceived", "Network.loadingFailed")

    def _drain_to_tails(self) -> None:
        if self._conn is None:
            return
        self._conn.pump(timeout=0.05)
        events = self._conn.drain_events(self._session_id)
        console = self.state.setdefault("console_tail", [])
        net = self.state.setdefault("net_tail", [])
        for ev in events:
            m, p = ev["method"], ev["params"]
            if m == "Runtime.consoleAPICalled":
                args = []
                for a in (p.get("args") or [])[:6]:
                    v = a.get("value", a.get("description", ""))
                    args.append(_redact(_clip(v, 300)))
                console.append({"kind": "console",
                                "level": str(p.get("type") or "log"),
                                "text": _clip(" ".join(map(str, args)), 500),
                                "ts": _now()})
            elif m == "Log.entryAdded":
                e = p.get("entry") or {}
                console.append({"kind": "log",
                                "level": str(e.get("level") or "info"),
                                "text": _redact(_clip(e.get("text", ""), 500)),
                                "ts": _now()})
            elif m == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails") or {}
                desc = ((d.get("exception") or {}).get("description")
                        or d.get("text") or "")
                console.append({"kind": "exception", "level": "error",
                                "text": _redact(_clip(desc, 500)),
                                "ts": _now()})
            elif m == "Network.requestWillBeSent":
                r = p.get("request") or {}
                net.append({"id": str(p.get("requestId") or ""),
                            "url": _redact(_clip(r.get("url", ""), 300)),
                            "method": r.get("method", "GET"),
                            "status": None, "failed": None, "ts": _now()})
            elif m == "Network.responseReceived":
                rid = str(p.get("requestId") or "")
                status = (p.get("response") or {}).get("status")
                for e in reversed(net):
                    if e.get("id") == rid:
                        e["status"] = status
                        break
            elif m == "Network.loadingFailed":
                rid = str(p.get("requestId") or "")
                err = _clip(p.get("errorText", ""), 120)
                for e in reversed(net):
                    if e.get("id") == rid:
                        e["failed"] = err
                        break
                else:
                    net.append({"id": rid, "url": "", "method": "",
                                "status": None, "failed": err, "ts": _now()})
        del console[: max(0, len(console) - _TAIL_CAP)]
        del net[: max(0, len(net) - _TAIL_CAP)]

    # ---- 工具 ----

    def _ready_wait(self, timeout: float = 20.0) -> None:
        deadline = _now() + timeout
        while _now() < deadline:
            try:
                rs = self._eval("document.readyState", timeout=5.0)
            except cdp.CDPError:
                rs = None
            if rs == "complete":
                return
            time.sleep(0.15)
        # 超时不算硬错——慢页面照样可 inspect
        return

    def _refresh_meta(self) -> None:
        try:
            self.state["url"] = str(self._eval("location.href", 5.0) or "")
            self.state["title"] = str(self._eval("document.title", 5.0) or "")
        except cdp.CDPError:
            pass

    def _bump_gen(self) -> None:
        self.state["gen"] = int(self.state.get("gen") or 1) + 1
        self.state["console_tail"] = []
        self.state["net_tail"] = []

    def _detect_navigation(self, before_url: str, wait_s: float = 0.6) -> bool:
        """动作后短等收导航事件；URL 变了或 frameNavigated 即算。"""
        time.sleep(wait_s)
        self._conn.pump(timeout=0.05) if self._conn else None
        try:
            now = str(self._eval("location.href", 5.0) or "")
        except cdp.CDPError:
            now = ""
        nav = bool(now and now != before_url)
        if self._conn:
            for ev in self._conn.drain_events(self._session_id):
                if ev["method"] == "Page.frameNavigated":
                    nav = True
                else:
                    self._pushback_event(ev)
        return nav

    def _pushback_event(self, ev: dict) -> None:
        # drain 是把全部缓冲取走；非导航事件要塞回尾巴通道
        m, p = ev["method"], ev["params"]
        if m in self._CONSOLE_METHODS or m in self._NET_METHODS:
            self._conn._events.setdefault(self._session_id, []).append(ev)

    def _resolve(self, ref: str) -> dict:
        if not isinstance(ref, str) or not ref:
            raise BrowserError("INVALID_INPUT", "缺少 ref")
        out = self._eval(
            f"({serializer.RESOLVER_JS})({json.dumps({'ref': ref, 'gen': int(self.state.get('gen') or 1)})})",
            timeout=10.0)
        if not isinstance(out, dict):
            raise BrowserError("INTERNAL", "ref 解析返回异常")
        if not out.get("ok"):
            reason = out.get("reason")
            if reason == "STALE":
                raise BrowserError(
                    "STALE_REF",
                    "页面已变化（导航/reload/快照过期），旧 ref 作废——重新 inspect")
            if reason == "NO_REF":
                raise BrowserError("NO_REF", f"快照里没有 ref={ref}——重新 inspect")
            raise BrowserError("GONE", f"ref={ref} 对应元素已不在页面上——重新 inspect")
        if not out.get("visible"):
            raise BrowserError("NOT_VISIBLE",
                               f"ref={ref} 元素当前不可见（display/零尺寸）")
        if out.get("disabled"):
            raise BrowserError("DISABLED", f"ref={ref} 元素处于 disabled")
        return out

    def _scroll_el_into_view(self, ref: str) -> dict:
        """把 ref 元素滚进视口，回传新鲜坐标。"""
        gen = int(self.state.get("gen") or 1)
        self._eval(
            f"""(function(){{
  var st=window.__cb_refs; if(!st||st.__gen!=={gen}) return;
  var rec=st[{json.dumps(ref)}]; if(!rec) return;
  var el=document.evaluate(rec.xpath,document,null,
    XPathResult.FIRST_ORDERED_NODE_TYPE,null).singleNodeValue;
  if(el&&el.scrollIntoView) el.scrollIntoView({{block:'center',inline:'center'}});
}})()""", timeout=10.0)
        time.sleep(0.05)
        return self._resolve(ref)

    def _mouse(self, mtype: str, x: float, y: float,
               button: str = "left", click_count: int = 1) -> None:
        self._send("Input.dispatchMouseEvent", {
            "type": mtype, "x": round(x, 1), "y": round(y, 1),
            "button": button, "clickCount": click_count}, timeout=10.0)

    def _click_at(self, r: dict, click_count: int = 1) -> None:
        x, y = float(r["cx"]), float(r["cy"])
        self._mouse("mouseMoved", x, y, button="none", click_count=0)
        self._mouse("mousePressed", x, y, click_count=click_count)
        self._mouse("mouseReleased", x, y, click_count=click_count)

    # ---- 动作 ----

    def act(self, action: str, inp: dict | None = None) -> dict:
        inp = inp or {}
        if action not in ACTIONS:
            raise BrowserError("INVALID_INPUT",
                               f"未知动作 {action}；可用: {', '.join(ACTIONS)}")
        self._acquire_lock()
        try:
            if action == "close":
                return self._a_close()
            self._connect()
            handler = getattr(self, "_a_" + action)
            out = handler(inp)
            self._drain_to_tails()
            self._refresh_meta()
            self._save_state()
            return out
        finally:
            self._release_lock()
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._session_id = ""

    # -- 读 --

    def _xlate_url(self, url: str) -> str:
        """WSL+Windows Chrome 模式下的 file:// 路径翻译（与 chrome.launch 同规）。"""
        win_bin = (self.state.get("chrome_path") or "").startswith("/mnt/")
        if win_bin and url.startswith("file://"):
            from urllib.parse import quote
            return ("file:///" + quote(
                chrome._to_win_path(url[7:].lstrip("/")).replace("\\", "/"),
                safe="/:._-~"))
        return url

    def _a_navigate(self, inp: dict) -> dict:
        url = str(inp.get("url") or "")
        if not any(url.startswith(s) for s in _ALLOWED_SCHEMES):
            raise BrowserError("SCHEME_DENIED",
                               f"只允许 {'/'.join(_ALLOWED_SCHEMES)}，拒: {_clip(url,120)}")
        timeout = min(float(inp.get("timeout", _DEFAULT_TIMEOUT_S)),
                      _MAX_TIMEOUT_S)
        self._send("Page.navigate", {"url": self._xlate_url(url)},
                   timeout=timeout)
        self._ready_wait(min(timeout, 20.0))
        self._bump_gen()
        self._drain_to_tails()
        self._refresh_meta()
        return {"url": self.state["url"], "title": self.state["title"],
                "gen": self.state["gen"],
                "effect": {"verified": True, "navigation": True}}

    def _a_inspect(self, inp: dict) -> dict:
        max_els = int(inp.get("maxEls", 400))
        snap_id = str(inp.get("snapshotId") or "")
        offset = int(inp.get("offset") or 0)
        gen = int(self.state.get("gen") or 1)
        if snap_id:
            # 续页：读已存快照（同一份 elements 接着切）
            snap_path = self.dir / f"{snap_id}.json"
            try:
                snap = json.loads(snap_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                raise BrowserError("STALE_SNAPSHOT",
                                   f"快照 {snap_id} 不存在或已过期——重新 inspect")
            if int(snap.get("gen") or -1) != gen:
                raise BrowserError("STALE_SNAPSHOT",
                                   f"快照 {snap_id} 是 gen={snap.get('gen')} 的，"
                                   f"当前 gen={gen}——重新 inspect")
        else:
            raw = self._eval(
                f"({serializer.SERIALIZER_JS})({json.dumps({'gen': gen, 'maxEls': max_els})})",
                timeout=20.0)
            if not isinstance(raw, dict) or "elements" not in raw:
                raise BrowserError("INTERNAL",
                                   f"序列化失败: {_clip(raw, 200)}")
            self.state["snap_n"] = int(self.state.get("snap_n") or 0) + 1
            snap_id = f"s{gen}-{self.state['snap_n']}"
            snap = raw
            (self.dir / f"{snap_id}.json").write_text(
                json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            self._save_state()
            offset = 0
        elements = list(snap.get("elements") or [])
        page = elements[offset: offset + _ELEMENT_PAGE]
        next_off = offset + len(page)
        return {
            "url": snap.get("url"), "title": snap.get("title"),
            "gen": gen, "snapshotId": snap_id,
            "viewport": snap.get("viewport"),
            "count": len(elements), "offset": offset,
            "nextOffset": next_off if next_off < len(elements) else None,
            "truncated": bool(snap.get("truncated")),
            "elements": page,
            "note": "ref 只对本快照代次有效；页面导航后必须重新 inspect",
        }

    def _a_query(self, inp: dict) -> dict:
        kind = str(inp.get("kind") or "text")
        selector = str(inp.get("selector") or "")
        if kind == "text":
            expr = ("document.body ? document.body.innerText : ''"
                    if not selector else
                    f"(document.querySelector({json.dumps(selector)})||{{}}).innerText||''")
            return {"kind": "text", "text": _clip(
                self._eval(expr, 15.0) or "", int(inp.get("limit", _TEXT_LIMIT)))}
        if kind == "dom":
            expr = ("document.documentElement.outerHTML"
                    if not selector else
                    f"(document.querySelector({json.dumps(selector)})||{{}}).outerHTML||''")
            return {"kind": "dom", "dom": _clip(
                self._eval(expr, 15.0) or "", int(inp.get("limit", _DOM_LIMIT)))}
        if kind == "console":
            entries = self.state.get("console_tail") or []
            level = str(inp.get("level") or "")
            if level:
                entries = [e for e in entries
                           if str(e.get("level")) == level]
            return {"kind": "console",
                    "entries": entries[-int(inp.get("limit", 50)):],
                    "count": len(entries)}
        if kind == "network":
            entries = self.state.get("net_tail") or []
            failed_only = bool(inp.get("failedOnly"))
            if failed_only:
                entries = [e for e in entries if e.get("failed")]
            return {"kind": "network",
                    "entries": entries[-int(inp.get("limit", 50)):],
                    "count": len(entries)}
        if kind == "url":
            return {"kind": "url", "url": self.state.get("url"),
                    "title": self.state.get("title"),
                    "gen": self.state.get("gen")}
        raise BrowserError("INVALID_INPUT",
                           f"query.kind 只认 text/dom/console/network/url，拿到 {kind}")

    def _a_screenshot(self, inp: dict) -> dict:
        params: dict = {"format": "png"}
        if inp.get("fullPage"):
            params["captureBeyondViewport"] = True
        res = self._send("Page.captureScreenshot", params, timeout=30.0)
        import base64
        data = base64.b64decode(res.get("data") or "")
        self.state["shot_n"] = int(self.state.get("shot_n") or 0) + 1
        name = str(inp.get("path") or f"shot_{self.state['shot_n']:03d}.png")
        safe = re.sub(r"[^\w\-.]", "_", name)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        path = self.artifacts / safe
        path.write_bytes(data)
        return {"path": str(path), "bytes": len(data),
                "note": "截图落盘为证据资产；模型侧证据链以 inspect 语义树为准"}

    # -- 交互 --

    def _a_click(self, inp: dict) -> dict:
        r = self._scroll_el_into_view(str(inp.get("ref") or ""))
        before = str(self.state.get("url") or "")
        self._click_at(r)
        nav = self._detect_navigation(before)
        if nav:
            self._ready_wait(15.0)
            self._bump_gen()
            self._refresh_meta()
        return {"ref": inp.get("ref"), "dispatched": True,
                "effect": {"verified": False, "verificationRequired": True,
                           "navigation": {"detected": nav,
                                          "url": self.state.get("url")}},
                "gen": self.state["gen"],
                "note": "已派发≠已生效；预期中的变化要再 inspect/query 验证"
                        + ("；导航已发生，旧 ref 全部作废" if nav else "")}

    def _a_double_click(self, inp: dict) -> dict:
        r = self._scroll_el_into_view(str(inp.get("ref") or ""))
        before = str(self.state.get("url") or "")
        self._click_at(r, click_count=2)
        nav = self._detect_navigation(before)
        if nav:
            self._ready_wait(15.0)
            self._bump_gen()
            self._refresh_meta()
        return {"ref": inp.get("ref"), "dispatched": True,
                "effect": {"verified": False, "verificationRequired": True,
                           "navigation": {"detected": nav}},
                "gen": self.state["gen"]}

    def _a_click_and_wait_for_navigation(self, inp: dict) -> dict:
        r = self._scroll_el_into_view(str(inp.get("ref") or ""))
        before = str(self.state.get("url") or "")
        timeout = min(float(inp.get("timeout", _DEFAULT_TIMEOUT_S)),
                      _MAX_TIMEOUT_S)
        self._click_at(r)
        deadline = _now() + timeout
        nav = False
        while _now() < deadline and not nav:
            nav = self._detect_navigation(before, wait_s=0.25)
        if not nav:
            raise BrowserError(
                "ACTION_TIMEOUT",
                "点击未触发主文档导航——若本就不该导航，用普通 click")
        self._ready_wait(15.0)
        self._bump_gen()
        self._refresh_meta()
        return {"ref": inp.get("ref"), "dispatched": True,
                "effect": {"verified": True,
                           "navigation": {"detected": True,
                                          "url": self.state.get("url")}},
                "gen": self.state["gen"]}

    def _a_hover(self, inp: dict) -> dict:
        r = self._scroll_el_into_view(str(inp.get("ref") or ""))
        self._mouse("mouseMoved", float(r["cx"]), float(r["cy"]),
                    button="none", click_count=0)
        time.sleep(0.15)
        return {"ref": inp.get("ref"), "dispatched": True,
                "tag": r.get("tag"),
                "effect": {"verified": False, "verificationRequired": True},
                "note": "hover 效果（弹层/title）要再 inspect 看"}

    def _a_fill(self, inp: dict) -> dict:
        ref = str(inp.get("ref") or "")
        text = str(inp.get("text") or "")
        self._resolve(ref)
        gen = int(self.state.get("gen") or 1)
        # 原生 setter 设值 + input/change 事件（React/Vue 受控组件兼容写法）
        ok = self._eval(
            f"""(function(){{
  var st=window.__cb_refs; if(!st||st.__gen!=={gen}) return 'STALE';
  var rec=st[{json.dumps(ref)}]; if(!rec) return 'NO_REF';
  var el=document.evaluate(rec.xpath,document,null,
    XPathResult.FIRST_ORDERED_NODE_TYPE,null).singleNodeValue;
  if(!el) return 'GONE';
  el.focus();
  var proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
             : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
             : HTMLInputElement.prototype;
  var setter = Object.getOwnPropertyDescriptor(proto,'value');
  if(setter&&setter.set) setter.set.call(el,{json.dumps(text)});
  else el.value={json.dumps(text)};
  el.dispatchEvent(new Event('input',{{bubbles:true}}));
  el.dispatchEvent(new Event('change',{{bubbles:true}}));
  return el.value==={json.dumps(text)};
}})()""", timeout=10.0)
        if ok in ("STALE", "NO_REF", "GONE"):
            raise BrowserError("STALE_REF", f"ref 失效({ok})——重新 inspect")
        return {"ref": ref, "dispatched": True,
                "effect": {"verified": bool(ok), "value": _clip(text, 200)}}

    def _a_type(self, inp: dict) -> dict:
        ref = str(inp.get("ref") or "")
        text = str(inp.get("text") or "")
        self._scroll_el_into_view(ref)
        gen = int(self.state.get("gen") or 1)
        self._eval(
            f"""(function(){{
  var rec=(window.__cb_refs||{{}})[{json.dumps(ref)}]; if(!rec) return;
  var el=document.evaluate(rec.xpath,document,null,
    XPathResult.FIRST_ORDERED_NODE_TYPE,null).singleNodeValue;
  if(!el) return;
  el.focus();
  if({str(bool(inp.get('clear'))).lower()}) {{
    var proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
    var s=Object.getOwnPropertyDescriptor(proto,'value');
    if(s&&s.set) s.set.call(el,''); else el.value='';
    el.dispatchEvent(new Event('input',{{bubbles:true}}));
  }}
}})()""", timeout=10.0)
        if text:
            self._send("Input.insertText", {"text": text}, timeout=10.0)
        return {"ref": ref, "dispatched": True,
                "effect": {"verified": False, "verificationRequired": True},
                "note": "insertText 走的是真实输入管线；要回读值请 query text 或 inspect"}

    def _a_press_key(self, inp: dict) -> dict:
        key = str(inp.get("key") or "")
        KEYMAP = {
            "Enter": {"windowsVirtualKeyCode": 13, "key": "Enter", "code": "Enter", "text": "\r"},
            "Tab": {"windowsVirtualKeyCode": 9, "key": "Tab", "code": "Tab"},
            "Escape": {"windowsVirtualKeyCode": 27, "key": "Escape", "code": "Escape"},
            "Backspace": {"windowsVirtualKeyCode": 8, "key": "Backspace", "code": "Backspace"},
            "Delete": {"windowsVirtualKeyCode": 46, "key": "Delete", "code": "Delete"},
            "ArrowUp": {"windowsVirtualKeyCode": 38, "key": "ArrowUp", "code": "ArrowUp"},
            "ArrowDown": {"windowsVirtualKeyCode": 40, "key": "ArrowDown", "code": "ArrowDown"},
            "ArrowLeft": {"windowsVirtualKeyCode": 37, "key": "ArrowLeft", "code": "ArrowLeft"},
            "ArrowRight": {"windowsVirtualKeyCode": 39, "key": "ArrowRight", "code": "ArrowRight"},
            "Home": {"windowsVirtualKeyCode": 36, "key": "Home", "code": "Home"},
            "End": {"windowsVirtualKeyCode": 35, "key": "End", "code": "End"},
            "PageUp": {"windowsVirtualKeyCode": 33, "key": "PageUp", "code": "PageUp"},
            "PageDown": {"windowsVirtualKeyCode": 34, "key": "PageDown", "code": "PageDown"},
            " ": {"windowsVirtualKeyCode": 32, "key": " ", "code": "Space", "text": " "},
        }
        spec = KEYMAP.get(key)
        if spec is None and len(key) == 1:
            spec = {"key": key, "text": key}
        if spec is None:
            raise BrowserError("INVALID_INPUT",
                               f"不认识的 key={key}；支持 Enter/Tab/Escape/"
                               "Backspace/Delete/方向键/Home/End/PageUp/PageDown/单字符")
        for phase in ("keyDown", "keyUp"):
            params = {"type": "rawKeyDown" if phase == "keyDown" else "keyUp",
                      "key": spec.get("key", ""), "code": spec.get("code", "")}
            if spec.get("windowsVirtualKeyCode"):
                params["windowsVirtualKeyCode"] = spec["windowsVirtualKeyCode"]
                params["nativeVirtualKeyCode"] = spec["windowsVirtualKeyCode"]
            if spec.get("text") and phase == "keyDown":
                params["type"] = "keyDown"
                params["text"] = spec["text"]
            self._send("Input.dispatchKeyEvent", params, timeout=10.0)
        return {"key": key, "dispatched": True,
                "effect": {"verified": False, "verificationRequired": True}}

    def _a_check(self, inp: dict) -> dict:
        return self._set_checked(inp, True)

    def _a_uncheck(self, inp: dict) -> dict:
        return self._set_checked(inp, False)

    def _set_checked(self, inp: dict, want: bool) -> dict:
        ref = str(inp.get("ref") or "")
        gen = int(self.state.get("gen") or 1)
        cur = self._eval(
            f"""(function(){{
  var st=window.__cb_refs; if(!st||st.__gen!=={gen}) return {{err:'STALE'}};
  var rec=st[{json.dumps(ref)}]; if(!rec) return {{err:'NO_REF'}};
  var el=document.evaluate(rec.xpath,document,null,
    XPathResult.FIRST_ORDERED_NODE_TYPE,null).singleNodeValue;
  if(!el) return {{err:'GONE'}};
  return {{checked:!!el.checked, tag:el.tagName.toLowerCase()}};
}})()""", timeout=10.0)
        if isinstance(cur, dict) and cur.get("err"):
            raise BrowserError("STALE_REF" if cur["err"] == "STALE" else "GONE",
                               f"ref 失效({cur['err']})——重新 inspect")
        if not isinstance(cur, dict) or "checked" not in cur:
            raise BrowserError("INVALID_INPUT", f"ref={ref} 不是可勾选控件")
        if cur["checked"] == want:
            return {"ref": ref, "dispatched": False,
                    "effect": {"verified": True, "checked": want,
                               "note": "已是目标状态（边界 noop，算成功不算失败）"}}
        r = self._scroll_el_into_view(ref)
        self._click_at(r)
        time.sleep(0.1)
        after = self._eval(
            f"""(function(){{
  var rec=(window.__cb_refs||{{}})[{json.dumps(ref)}]; if(!rec) return null;
  var el=document.evaluate(rec.xpath,document,null,
    XPathResult.FIRST_ORDERED_NODE_TYPE,null).singleNodeValue;
  return el? !!el.checked : null;
}})()""", timeout=10.0)
        return {"ref": ref, "dispatched": True,
                "effect": {"verified": after is want, "checked": after}}

    def _a_select_option(self, inp: dict) -> dict:
        ref = str(inp.get("ref") or "")
        want = str(inp.get("value") or inp.get("label") or "")
        gen = int(self.state.get("gen") or 1)
        out = self._eval(
            f"""(function(){{
  var st=window.__cb_refs; if(!st||st.__gen!=={gen}) return {{err:'STALE'}};
  var rec=st[{json.dumps(ref)}]; if(!rec) return {{err:'NO_REF'}};
  var el=document.evaluate(rec.xpath,document,null,
    XPathResult.FIRST_ORDERED_NODE_TYPE,null).singleNodeValue;
  if(!el) return {{err:'GONE'}};
  if(el.tagName!=='SELECT') return {{err:'NOT_SELECT'}};
  var want={json.dumps(want)}, hit=null;
  for(var i=0;i<el.options.length;i++){{
    var o=el.options[i];
    if(o.value===want||o.text===want||o.text.trim()===want){{hit=o;break;}}
  }}
  if(!hit) return {{err:'NO_OPTION',
    options:[].map.call(el.options,function(o){{return o.text}}).slice(0,40)}};
  el.value=hit.value;
  el.dispatchEvent(new Event('input',{{bubbles:true}}));
  el.dispatchEvent(new Event('change',{{bubbles:true}}));
  return {{value:el.value}};
}})()""", timeout=10.0)
        if isinstance(out, dict) and out.get("err"):
            code = {"STALE": "STALE_REF", "NO_REF": "NO_REF",
                    "GONE": "GONE", "NOT_SELECT": "NOT_SELECT",
                    "NO_OPTION": "NO_OPTION"}[out["err"]]
            msg = f"select_option 失败({out['err']})"
            if out.get("options"):
                msg += f"；可选项: {_clip(out['options'], 300)}"
            raise BrowserError(code, msg)
        return {"ref": ref, "dispatched": True,
                "effect": {"verified": bool(out and out.get("value") == want
                                          or (out or {}).get("value")),
                           "value": (out or {}).get("value")}}

    def _a_scroll(self, inp: dict) -> dict:
        ref = str(inp.get("ref") or "")
        direction = str(inp.get("direction") or "down")
        amount = int(inp.get("amount") or 600)
        dx, dy = {"down": (0, amount), "up": (0, -amount),
                  "right": (amount, 0), "left": (-amount, 0)}.get(
            direction, (0, amount))
        gen = int(self.state.get("gen") or 1)
        out = self._eval(
            f"""(function(){{
  var el=document.scrollingElement||document.documentElement;
  var rec=null;
  var st=window.__cb_refs;
  if({json.dumps(ref)}&&st&&st.__gen==={gen}) rec=st[{json.dumps(ref)}];
  if(rec){{
    var r=document.evaluate(rec.xpath,document,null,
      XPathResult.FIRST_ORDERED_NODE_TYPE,null).singleNodeValue;
    if(r) el=r;
  }}
  var x0=el.scrollLeft,y0=el.scrollTop;
  el.scrollBy({dx},{dy});
  return {{dx:el.scrollLeft-x0,dy:el.scrollTop-y0,
    atTop:el.scrollTop<=0,
    atBottom:el.scrollTop+el.clientHeight>=el.scrollHeight-1,
    atLeft:el.scrollLeft<=0,
    atRight:el.scrollLeft+el.clientWidth>=el.scrollWidth-1}};
}})()""", timeout=10.0)
        if not isinstance(out, dict):
            raise BrowserError("INTERNAL", "scroll 返回异常")
        moved = bool(out.get("dx") or out.get("dy"))
        return {"dispatched": True,
                "effect": {"verified": True, "moved": moved,
                           "actualDeltaX": out.get("dx"),
                           "actualDeltaY": out.get("dy"),
                           "atStart": bool(out.get("atTop") or out.get("atLeft")),
                           "atEnd": bool(out.get("atBottom") or out.get("atRight")),
                           "code": None if moved else "NO_SCROLL_EFFECT"},
                "note": "moved=false 且 atStart/atEnd 是边界 noop（成功非失败）"}

    def _a_scroll_into_view(self, inp: dict) -> dict:
        self._scroll_el_into_view(str(inp.get("ref") or ""))
        return {"ref": inp.get("ref"), "dispatched": True,
                "effect": {"verified": True}}

    # -- 等待 --

    def _a_wait(self, inp: dict) -> dict:
        ms = min(int(inp.get("ms") or 1000), 60000)
        time.sleep(ms / 1000.0)
        self._drain_to_tails()
        return {"waited": ms, "url": self.state.get("url")}

    def _a_wait_for_text(self, inp: dict) -> dict:
        text = str(inp.get("text") or "")
        timeout = min(float(inp.get("timeout", _DEFAULT_TIMEOUT_S)),
                      _MAX_TIMEOUT_S)
        deadline = _now() + timeout
        while _now() < deadline:
            found = self._eval(
                f"(document.body?document.body.innerText:'').includes({json.dumps(text)})",
                10.0)
            if found:
                return {"found": True, "text": text}
            time.sleep(0.3)
            self._drain_to_tails()
        return {"found": False, "text": text, "timedOut": True}

    def _a_wait_for_selector(self, inp: dict) -> dict:
        sel = str(inp.get("selector") or "")
        timeout = min(float(inp.get("timeout", _DEFAULT_TIMEOUT_S)),
                      _MAX_TIMEOUT_S)
        deadline = _now() + timeout
        while _now() < deadline:
            found = self._eval(
                f"!!document.querySelector({json.dumps(sel)})", 10.0)
            if found:
                return {"found": True, "selector": sel}
            time.sleep(0.3)
        return {"found": False, "selector": sel, "timedOut": True}

    def _a_wait_for_navigation(self, inp: dict) -> dict:
        before = str(self.state.get("url") or "")
        timeout = min(float(inp.get("timeout", _DEFAULT_TIMEOUT_S)),
                      _MAX_TIMEOUT_S)
        deadline = _now() + timeout
        while _now() < deadline:
            if self._detect_navigation(before, wait_s=0.25):
                self._ready_wait(15.0)
                self._bump_gen()
                self._refresh_meta()
                return {"navigation": {"detected": True,
                                       "url": self.state.get("url")},
                        "gen": self.state["gen"]}
        return {"navigation": {"detected": False, "url": before},
                "timedOut": True}

    # -- 历史/标签 --

    def _a_back(self, inp: dict) -> dict:
        return self._history_go(-1, inp)

    def _a_forward(self, inp: dict) -> dict:
        return self._history_go(1, inp)

    def _history_go(self, delta: int, inp: dict) -> dict:
        hist = self._send("Page.getNavigationHistory", timeout=10.0)
        entries = hist.get("entries") or []
        idx = int(hist.get("currentIndex") or 0)
        want = idx + delta
        if not (0 <= want < len(entries)):
            raise BrowserError("NO_HISTORY",
                               f"没有可{'后退' if delta < 0 else '前进'}的历史")
        self._send("Page.navigateToHistoryEntry",
                   {"entryId": entries[want].get("id")}, timeout=15.0)
        self._ready_wait(15.0)
        self._bump_gen()
        self._refresh_meta()
        return {"url": self.state.get("url"),
                "gen": self.state["gen"],
                "effect": {"verified": True, "navigation": True}}

    def _a_reload(self, inp: dict) -> dict:
        self._send("Page.reload",
                   {"ignoreCache": bool(inp.get("hard"))}, timeout=15.0)
        self._ready_wait(15.0)
        self._bump_gen()
        self._refresh_meta()
        return {"url": self.state.get("url"), "gen": self.state["gen"],
                "effect": {"verified": True}}

    def _a_tabs(self, inp: dict) -> dict:
        pages = [{"targetId": t.get("targetId"), "url": t.get("url"),
                  "title": t.get("title"),
                  "active": t.get("targetId") == self.state.get("target_id")}
                 for t in self._conn.list_targets() if t.get("type") == "page"]
        return {"tabs": pages}

    def _a_new_tab(self, inp: dict) -> dict:
        url = str(inp.get("url") or "about:blank")
        if not any(url.startswith(s) for s in _ALLOWED_SCHEMES):
            raise BrowserError("SCHEME_DENIED", f"拒: {_clip(url, 120)}")
        tid = self._conn.create_target(self._xlate_url(url))
        self.state["target_id"] = tid
        self._session_id = self._conn.attach(tid)
        self._conn.enable_session_domains(self._session_id)
        self._ready_wait(15.0)
        self._bump_gen()
        self._refresh_meta()
        return {"targetId": tid, "url": self.state.get("url"),
                "gen": self.state["gen"]}

    def _a_switch_tab(self, inp: dict) -> dict:
        tid = str(inp.get("targetId") or "")
        pages = [t for t in self._conn.list_targets() if t.get("type") == "page"]
        if not any(t.get("targetId") == tid for t in pages):
            raise BrowserError("NO_TARGET", f"没有 page target {tid}")
        self.state["target_id"] = tid
        self._session_id = self._conn.attach(tid)
        self._conn.enable_session_domains(self._session_id)
        self._bump_gen()
        self._refresh_meta()
        return {"targetId": tid, "url": self.state.get("url"),
                "gen": self.state["gen"]}

    # -- 管理 --

    def _a_status(self, inp: dict) -> dict:
        return {"alive": True, "name": self.name,
                "url": self.state.get("url"), "title": self.state.get("title"),
                "gen": self.state.get("gen"), "pid": self.state.get("pid"),
                "profile": str(self.profile),
                "dir": str(self.dir)}

    def _a_close(self) -> dict:
        pid = int(self.state.get("pid") or 0)
        if self._conn is not None:
            try:
                if self.state.get("target_id"):
                    self._conn.close_target(str(self.state["target_id"]))
            except Exception:  # noqa: BLE001
                pass
            self._conn.close()
            self._conn = None
        chrome.kill_process(pid)
        for k in ("pid", "port", "ws_url", "target_id"):
            self.state[k] = 0 if k in ("pid", "port") else ""
        self._save_state()
        return {"closed": True, "name": self.name,
                "note": "profile 目录仍在（证据/缓存）；整目录删了才彻底"}
