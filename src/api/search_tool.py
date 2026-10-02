# -*- coding: utf-8 -*-
"""联网搜索工具（中性能力 2026-10-02；双通道版 17:39 改造）。

- 主模型可用的读能力：查询网页（web_search）、获取网页文本（fetch_web）。
- 结果只作为工具返回进入当前调用（prior_messages→prompt 日志）；
  不写卡、不进任何常设上下文、不回流后续调用。
- 失败返回 {"error": ...}——失败是事实状态，不是闸。
- 无次数限制、无返回长度截断；网络层超时仅作失败检测。

通道（2026-10-02 17:39 实测改版，智谱 429 余额耗尽后全线切换）：
- 主通道 SearxNG 本机元搜（localhost:8888，docker 容器 searxng）：
  真实 URL/标题/摘要，中英文各实测 20 条结果（17:38）。
- 备用通道 阿里云 enable_search（qwen3.8-max 联网检索）：
  返回模型改写文本而非原始链接，实测可用（常识 5.3s / 推演类 42s）。
  仅在 SearxNG 失败时启用，返回体注明通道性质。
- 智谱 web_search：429 余额不足（17:32 复测），已停用。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

import requests

_SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888")
_ALIYUN_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
_ALIYUN_MODEL = "qwen3.8-max"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BLANK_RE = re.compile(r"[ \t]*\n[ \t\n]*")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _searxng(query: str, count: int) -> dict[str, Any] | None:
    """SearxNG 主通道。成功返回结果 dict；不可用/空结果返回 None（转备用）。"""
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
                "source": it.get("url", "").split("/")[2] if "://" in it.get("url", "") else "",
                "engine": ", ".join(it.get("engines") or []),
                "date": it.get("publishedDate", ""),
            })
        if not results:
            return None
        return {"query": query, "fetched_at": _now(), "channel": "searxng", "results": results}
    except Exception:
        return None


def _aliyun(query: str) -> dict[str, Any] | None:
    """阿里云 enable_search 备用通道：模型联网检索后返回改写文本（非原始链接）。"""
    key = os.environ.get("ALIYUN_TOKEN_PLAN_API_KEY", "")
    if not key:
        return None
    try:
        r = requests.post(
            _ALIYUN_URL,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": _ALIYUN_MODEL,
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


def web_search(query: str, count: int = 10) -> dict[str, Any]:
    """查询网页，返回结果列表。主通道 SearxNG，备用阿里云；永不抛异常。"""
    q = (query or "").strip()
    if not q:
        return {"error": "query 为空"}

    primary = _searxng(q, count)
    if primary is not None:
        return primary

    backup = _aliyun(q)
    if backup is not None:
        return backup

    return {"error": "搜索失败：SearxNG 不可用且阿里云备用通道失败（两通道均未返回结果）"}


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
