# -*- coding: utf-8 -*-
"""联网搜索工具（中性能力 2026-10-02）。

- 主模型可用的读能力：查询网页（web_search）、获取网页文本（fetch_web）。
- 结果只作为工具返回进入当前调用（prior_messages→prompt 日志）；
  不写卡、不进任何常设上下文、不回流后续调用。
- 失败返回 {"error": ...}——失败是事实状态，不是闸。
- 无次数限制、无墙钟、无返回长度截断；网络层超时仅作失败检测。

通道：智谱 web_search API（open.bigmodel.cn，ZHIPU_CODING_API_KEY，
2026-10-02 实测 200）。备用通道：阿里云 enable_search（已实测 200，
但走 chat 接口、返回为模型改写文本，非原始结果——不启用）。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

import requests

_ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BLANK_RE = re.compile(r"[ \t]*\n[ \t\n]*")


def web_search(query: str, count: int = 10) -> dict[str, Any]:
    """查询网页，返回结果列表。永不抛异常，失败转 error dict。"""
    q = (query or "").strip()
    if not q:
        return {"error": "query 为空"}
    key = os.environ.get("ZHIPU_CODING_API_KEY", "")
    if not key:
        return {"error": "未配置 ZHIPU_CODING_API_KEY"}
    try:
        r = requests.post(
            _ZHIPU_URL,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"search_engine": "search_std", "search_query": q, "count": count},
            timeout=30,
        )
        if r.status_code != 200:
            return {"error": f"搜索服务返回 {r.status_code}: {r.text[:200]}"}
        data = r.json()
        raw = data.get("search_result") or []
        results = []
        for it in raw:
            if not isinstance(it, dict):
                continue
            results.append({
                "title": it.get("title", ""),
                "url": it.get("link") or it.get("url", ""),
                "snippet": it.get("content") or it.get("snippet", ""),
                "source": it.get("media", ""),
                "date": it.get("publish_date", ""),
            })
        return {
            "query": q,
            "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "results": results,
        }
    except requests.Timeout:
        return {"error": "搜索服务超时"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"搜索失败: {type(exc).__name__}: {exc}"}


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
                "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            }
        return {
            "url": u,
            "text": r.text,
            "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        }
    except requests.Timeout:
        return {"error": "网页获取超时"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"获取失败: {type(exc).__name__}: {exc}"}
