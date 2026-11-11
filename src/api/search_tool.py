# -*- coding: utf-8 -*-
"""联网搜索工具（中性能力 2026-10-02；provider 注册表版 2026-11-09）。

- 主模型可用的读能力：查询网页（web_search）、获取网页文本（fetch_web）。
- 结果只作为工具返回进入当前调用（prior_messages→prompt 日志）；
  不写卡、不进任何常设上下文、不回流后续调用。
- 失败返回 {"error": ...}——失败是事实状态，不是闸。
- 无次数限制、无返回长度截断；网络层超时仅作失败检测。

provider 链（2026-11-09 探针实测排序；每家一个函数，返回 None 即走下一家）：

  1. ``_minimax_srvtool`` —— MiniMax 平台 Anthropic /anthropic/v1/messages +
     ``web_search_20250305`` server tool（实测 OK；item 直带 content 摘要
     + page_age，形状最富）。MINIMAX_API_KEY。
  2. ``_mavis_srvtool`` —— MiniMax Code 的 mavis 网关（broker 租约令牌，
     ``mavis_lease``）+ 同款 server tool。MiniMax Code 没在跑就跳过
     （令牌铸不出 ⇒ None），跑时边际成本为零。
  3. ``_deepseek_srvtool`` —— DeepSeek 官方 Anthropic 兼容端点 + 同款
     server tool（dsh 实测同款；snippet 要从 text citations 拼）。
     DEEPSEEK_API_KEY。
  4. ``_searxng`` —— 本机元搜容器（localhost:8888；不靠供应商，但容器
     得活着——2026-11-09 实测未跑）。
  5. ``_aliyun`` —— 阿里云 enable_search（模型联网改写文本，非原始链接；
     永远垫底）。

  已实测不可用（别再加回）：kimi ``$web_search`` builtin——tool_call 发得出
  但 ``search_id`` 引用解不回正文，模型自述"tool call didn't return actual
  results"（半残比死更糟）；volcengine coding/v3 不认 ``{type:web_search}``
  tools 形状；tavily keyless 每日限额秒满；智谱 web_search 429 余额不足。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

_SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888")
_ALIYUN_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"

#: Anthropic server tool 各家的端点/模型（env 可覆盖——端点换代改环境变量不改码）
_MINIMAX_BASE = os.environ.get(
    "MINIMAX_SEARCH_BASE", "https://api.minimaxi.com/anthropic/v1")
_MINIMAX_MODEL = os.environ.get("MINIMAX_SEARCH_MODEL", "MiniMax-M2.5")
_MAVIS_BASE = os.environ.get(
    "MAVIS_SEARCH_BASE", "https://agent.minimax.cn/mavis/api/v1/llm/v1")
_MAVIS_MODEL = os.environ.get("MAVIS_SEARCH_MODEL", "MiniMax-M3.1-Flash")
_DEEPSEEK_BASE = os.environ.get(
    "DEEPSEEK_SEARCH_BASE", "https://api.deepseek.com/anthropic/v1")
_DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_SEARCH_MODEL", "deepseek-v4-flash")

_ANTHROPIC_TOOL = {"type": "web_search_20250305", "name": "web_search",
                   "max_uses": 5}


def _search_model() -> str:
    """搜索查询改写用哪一档 —— **从模型目录取**（端点真实 id）。

    〔5715 · 模型名收口〕改前是模块常量 ``_ALIYUN_MODEL = "qwen3.8-max"``：
    **模型名写死在代码里** ⇒ 端点换代/改名就悄悄失配（要么 404、要么悄悄用了
    别的模型）。现在由 models.yaml 用 ``use: search`` 标出那一档，代码只取
    目录里的 ``model:`` 字段（端点真实 id）⇒ 换代只改配置。

    取不到 ⇒ 空串（调用方**如实报错**，不猜模型名）。
    """
    from . import model_config  # 延迟 import：api 层内互相引用，避免装载期成环

    return model_config.model_for_purpose("search")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BLANK_RE = re.compile(r"[ \t]*\n[ \t\n]*")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:  # noqa: BLE001 非法 URL 不挡格式化
        return ""


# ============================================================ Anthropic server tool 公共件

def _anthropic_search_blocks(base_url: str, key: str, model: str,
                             query: str, *,
                             extra_headers: dict[str, str] | None = None,
                             timeout: int = 60) -> list[dict[str, Any]] | None:
    """POST ``{base}/messages`` 带 ``web_search_20250305`` server tool。

    返回响应的 ``content`` blocks（未解析）；非 200/异常返回 None。
    """
    headers = {"x-api-key": key, "Authorization": f"Bearer {key}",
               "anthropic-version": "2023-06-01",
               "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    try:
        r = requests.post(
            f"{base_url}/messages", headers=headers,
            json={"model": model, "max_tokens": 4096,
                  "messages": [{"role": "user",
                                "content": [{"type": "text",
                                             "text": "Perform a web search for the "
                                                     f"query: {query}"}]}],
                  "tools": [dict(_ANTHROPIC_TOOL)]},
            timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json().get("content") or []
    except Exception:
        return None


def _blocks_to_sources(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``web_search_tool_result`` blocks → 归一化 sources 列表。

    snippet 两个来源：item 自带 ``content``（MiniMax 形状），或 text block 的
    ``citations[].cited_text`` 按 url 回填（DeepSeek 形状——dsh 的
    citationSnippets 同款）。都没有就留空串——不编摘要。
    """
    cited: dict[str, str] = {}
    for b in blocks:
        if b.get("type") != "text":
            continue
        for c in (b.get("citations") or []):
            u, t = str(c.get("url") or ""), str(c.get("cited_text") or "")
            if u and t and u not in cited:
                cited[u] = t
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for b in blocks:
        if b.get("type") != "web_search_tool_result":
            continue
        for it in (b.get("content") or []):
            url = str(it.get("url") or "")
            if it.get("type") != "web_search_result" or not url or url in seen:
                continue
            seen.add(url)
            out.append({
                "title": str(it.get("title") or ""),
                "url": url,
                "snippet": str(it.get("content") or cited.get(url) or ""),
                "source": _host(url),
                "date": str(it.get("page_age") or it.get("publishedAt") or ""),
            })
    return out


# ============================================================ provider 链（按探针实测排序）

def _minimax_srvtool(query: str, count: int) -> dict[str, Any] | None:
    """MiniMax 平台 Anthropic 端点 + web_search server tool（item 直带摘要）。"""
    key = os.environ.get("MINIMAX_API_KEY", "")
    if not key:
        return None
    blocks = _anthropic_search_blocks(_MINIMAX_BASE, key, _MINIMAX_MODEL, query)
    if not blocks:
        return None
    sources = _blocks_to_sources(blocks)
    if not sources:
        return None
    return {"query": query, "fetched_at": _now(), "channel": "minimax_srvtool",
            "results": sources[:count]}


def _mavis_srvtool(query: str, count: int) -> dict[str, Any] | None:
    """MiniMax Code 的 mavis 网关 + 同款 server tool（broker 租约令牌）。

    MiniMax Code 没在跑 ⇒ 令牌铸不出 ⇒ 响亮 None 走下一家（不是错误，
    是这条座位不存在——与 mavis 模型座位的失败哲学一致）。
    """
    try:
        from ..model_gateway import mavis_lease
        token = str(mavis_lease.mint_lease_token()["accessToken"])
    except Exception:
        return None
    blocks = _anthropic_search_blocks(_MAVIS_BASE, token, _MAVIS_MODEL, query)
    if not blocks:
        return None
    sources = _blocks_to_sources(blocks)
    if not sources:
        return None
    return {"query": query, "fetched_at": _now(), "channel": "mavis_srvtool",
            "results": sources[:count]}


def _deepseek_srvtool(query: str, count: int) -> dict[str, Any] | None:
    """DeepSeek 官方 Anthropic 兼容端点 + web_search server tool。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return None
    blocks = _anthropic_search_blocks(_DEEPSEEK_BASE, key, _DEEPSEEK_MODEL, query)
    if not blocks:
        return None
    sources = _blocks_to_sources(blocks)
    if not sources:
        return None
    return {"query": query, "fetched_at": _now(), "channel": "deepseek_srvtool",
            "results": sources[:count]}


def _searxng(query: str, count: int) -> dict[str, Any] | None:
    """SearxNG 本机元搜。成功返回结果 dict；不可用/空结果返回 None（转备用）。"""
    try:
        r = requests.get(
            f"{_SEARXNG_URL}/search",
            params={"q": query, "format": "json"},
            headers={"User-Agent": _UA},
            timeout=30,
        )
        if r.status_code != 200:
            return None
        raw = r.json().get("results") or []
        results = []
        for it in raw[:count]:
            if not isinstance(it, dict):
                continue
            results.append({
                "title": it.get("title", ""),
                "url": it.get("url", ""),
                "snippet": it.get("content", ""),
                "source": _host(str(it.get("url") or "")),
                "engine": ", ".join(it.get("engines") or []),
                "date": it.get("publishedDate", ""),
            })
        if not results:
            return None
        return {"query": query, "fetched_at": _now(), "channel": "searxng", "results": results}
    except Exception:
        return None


def _aliyun(query: str, count: int) -> dict[str, Any] | None:
    """阿里云 enable_search 备用通道：模型联网检索后返回改写文本（非原始链接）。

    ``count`` 不适用（这条通道只回一段改写文本，无链接列表可截）——
    收参只为 provider 签名统一。
    """
    key = os.environ.get("ALIYUN_TOKEN_PLAN_API_KEY", "")
    if not key:
        return None
    # 〔5715 · 模型名收口〕用哪一档从**模型目录**取（use: search 标记的那档），
    # 代码里不写模型名。取不到 ⇒ 这条备用通道**如实不可用**（返回 None），
    # 不猜一个模型名去撞端点。
    model = _search_model()
    if not model:
        return None
    try:
        r = requests.post(
            _ALIYUN_URL,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "user",
                                "content": f"搜索并回答（附来源与日期）：{query}"}],
                  "enable_search": True},
            timeout=120,
        )
        if r.status_code != 200:
            return None  # 备用通道失败就是失败，不把错误体伪装成结果
        ch = (r.json().get("choices") or [{}])
        text = ((ch[0].get("message") or {}).get("content") or "").strip()
        if not text:
            return None
        return {"query": query, "fetched_at": _now(),
                "channel": "aliyun_enable_search",
                "note": "模型联网检索后的改写文本，非原始链接列表",
                "text": text}
    except Exception:
        return None


#: provider 链顺序（改顺序改这一处；每家自报 channel 名，返回体能溯源）
_PROVIDERS = (
    _minimax_srvtool,
    _mavis_srvtool,
    _deepseek_srvtool,
    _searxng,
    _aliyun,
)


def web_search(query: str, count: int = 10) -> dict[str, Any]:
    """查询网页，返回结果列表。按 provider 链逐家试，全灭才回 error。"""
    q = (query or "").strip()
    if not q:
        return {"error": "query 为空"}

    tried: list[str] = []
    for fn in _PROVIDERS:
        tried.append(fn.__name__.lstrip("_"))
        hit = fn(q, count)
        if hit is not None:
            return hit

    return {"error": f"搜索失败：provider 链全灭（试过 {', '.join(tried)}）",
            "tried": tried}


def fetch_web(url: str) -> dict[str, Any]:
    """获取网页文本内容。HTML 机械转文本（剥 script/style/标签），无长度截断。
    永不抛异常，失败转 error dict。"""
    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return {"error": "url 非法（仅支持 http/https）"}
    try:
        r = requests.get(u, headers={"User-Agent": _UA}, timeout=30)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        if not r.encoding:
            r.encoding = r.apparent_encoding or "utf-8"
        content_type = r.headers.get("Content-Type", "")
        if "html" in content_type.lower():
            html = _COMMENT_RE.sub("", r.text)
            title_m = _TITLE_RE.search(html)
            html = _SCRIPT_RE.sub("", html)
            text = _TAG_RE.sub("\n", html)
            text = _BLANK_RE.sub("\n", text)
            return {
                "url": u,
                "title": (title_m.group(1).strip() if title_m else ""),
                "text": text.strip(),
                "fetched_at": _now(),
            }
        return {
            "url": u,
            "text": r.text,
            "fetched_at": _now(),
        }
    except requests.Timeout:
        return {"error": "网页获取超时"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"获取失败: {type(exc).__name__}: {exc}"}
