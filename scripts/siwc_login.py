# -*- coding: utf-8 -*-
"""Sign in with ChatGPT（SIWC）授权脚本 —— 一次性拿 token，之后由 gateway 复用。

走的是 OpenAI 官方「开源 / 本地工具」自助流程：
  client_id=dynamic_agent_client + agent_name_hint + 持久化的 ext_agent_host_id
  OAuth 2.0 Authorization Code + PKCE + OIDC，回调 127.0.0.1 环回。
授权成功后拿到 access_token（1 小时）+ refresh_token（30 天），
用它打 https://api.openai.com/v1/responses，消耗 ChatGPT 套餐额度。

用法：
    ./.venv/Scripts/python.exe scripts/siwc_login.py

产物（都不进 git）：
    config/siwc_host_id.txt   每个宿主一份，urn:uuid:...，跨重启复用
    config/siwc_tokens.json   access/refresh token 与过期时间
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import socketserver
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
HOST_ID_FILE = ROOT / "config" / "siwc_host_id.txt"
TOKENS_FILE = ROOT / "config" / "siwc_tokens.json"

AUTH_ENDPOINT = "https://auth.openai.com/api/accounts/authorize"
TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
CLIENT_ID = "dynamic_agent_client"
APP_NAME = "CardColony 推演agent"
# resource.invoke + chatgpt.tokens.use.direct 是官方文档里申请「用套餐额度」的 scope
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"

# api.openai.com / auth.openai.com 在本机必须走 Clash，直连会 TLS 卡死
PROXY = os.environ.get("SIWC_PROXY", "http://127.0.0.1:7890")

CALLBACK_PORT = 1455


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _host_id() -> str:
    """每个宿主一份稳定 ID（urn:uuid:...），跨重启复用；不是认证凭据。"""
    if HOST_ID_FILE.exists():
        v = HOST_ID_FILE.read_text(encoding="utf-8").strip()
        if v:
            return v
    v = "urn:uuid:" + str(uuid.uuid4())
    HOST_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOST_ID_FILE.write_text(v, encoding="utf-8")
    return v


class _Handler(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        _Handler.result = {k: v[0] for k, v in qs.items()}
        body = (
            "<html><meta charset='utf-8'><body style='font-family:system-ui;padding:40px'>"
            "<h2>授权已返回</h2><p>可以关闭本页，回到终端看结果。</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *a):  # 静音
        pass


def _jwt_claims(tok: str) -> dict:
    """只解 payload 看 claims（授权阶段不验签，验签留给正式适配器）。"""
    try:
        part = tok.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def main() -> int:
    host_id = _host_id()
    verifier, challenge = _pkce()
    state = _b64url(secrets.token_bytes(24))
    nonce = _b64url(secrets.token_bytes(24))
    redirect_uri = f"http://127.0.0.1:{CALLBACK_PORT}/callback"

    params = {
        "client_id": CLIENT_ID,
        "agent_name_hint": APP_NAME,
        "ext_agent_host_id": host_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)

    print("=" * 72)
    print("请在浏览器里完成授权（若没自动打开，复制下面链接）：")
    print(url)
    print("=" * 72)

    with socketserver.TCPServer(("127.0.0.1", CALLBACK_PORT), _Handler) as srv:
        t = threading.Thread(target=srv.handle_request, daemon=True)
        t.start()
        # [2026-11-19 用户令「不要弹到默认浏览器」] 只打印链接，由用户自选浏览器打开
        deadline = time.time() + 300
        while time.time() < deadline and not _Handler.result:
            time.sleep(0.3)
        got = _Handler.result

    if not got:
        print("× 5 分钟内没收到回调（授权超时）")
        return 2
    if got.get("error"):
        print("× 授权被拒:", got.get("error"), got.get("error_description"))
        return 3
    if got.get("state") != state:
        print("× state 不匹配，疑似 CSRF，已中止")
        return 4
    code = got.get("code")
    client_id = got.get("client_id") or CLIENT_ID
    if not code:
        print("× 回调里没有 code:", got)
        return 5
    print("· 收到授权码，client_id =", client_id[:14] + "…")

    body = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "resource": RESOURCE,
    }
    with httpx.Client(proxy=PROXY, timeout=60) as c:
        r = c.post(TOKEN_ENDPOINT, data=body,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code != 200:
        print("× 换 token 失败:", r.status_code, r.text[:400])
        return 6

    tok = r.json()
    at = tok.get("access_token") or ""
    rt = tok.get("refresh_token") or ""
    idt = tok.get("id_token") or ""
    if not at:
        print("× 响应里没有 access_token（可能没批 resource.invoke）:", json.dumps(tok)[:300])
        return 7

    claims = _jwt_claims(at) or _jwt_claims(idt)
    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_FILE.write_text(json.dumps({
        "access_token": at,
        "refresh_token": rt,
        "id_token": idt,
        "token_type": tok.get("token_type"),
        "scope": tok.get("scope"),
        "expires_at": time.time() + int(tok.get("expires_in") or 3600),
        "client_id": client_id,
        "host_id": host_id,
        "account": claims.get("email") or claims.get("sub"),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("√ token 已保存:", TOKENS_FILE.relative_to(ROOT))
    print("  scope =", tok.get("scope"))
    print("  账号  =", claims.get("email") or claims.get("sub") or "（未解析）")
    print("  有效期 =", int(tok.get("expires_in") or 0), "秒")

    # 立刻打一发 /v1/responses 验证套餐额度是否真的可用
    print("\n· 验证 /v1/responses …")
    with httpx.Client(proxy=PROXY, timeout=120) as c:
        r = c.post("https://api.openai.com/v1/responses",
                   headers={"Authorization": f"Bearer {at}",
                            "Content-Type": "application/json"},
                   json={"model": "gpt-5.4-mini", "input": "回复两个字：收到"})
    print("  HTTP", r.status_code)
    if r.status_code == 200:
        d = r.json()
        txt = d.get("output_text")
        if not txt:
            for item in d.get("output") or []:
                for part in item.get("content") or []:
                    if part.get("type") in ("output_text", "text"):
                        txt = (txt or "") + str(part.get("text") or "")
        print("  √ 出活:", repr((txt or "")[:60]))
        print("  usage:", d.get("usage"))
    else:
        print("  × ", r.text[:500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
