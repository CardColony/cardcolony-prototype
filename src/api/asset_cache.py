# -*- coding: utf-8 -*-
"""静态资源缓存策略（画布验收 2026-10-19）。

## 为什么需要这个

原来前端缓存全靠模板里手写的 `?v=NN` 升号来破。两个后果：

1. **改了不生效**：改了 canvas.css 忘了升号，浏览器吃旧文件，看半天还是旧样子。
   这不是"偶发"，是结构性陷阱——验收时看到的是旧代码，等于没验收。
2. **发版即坏**：用户浏览器里存着的旧 canvas.css 会把新版 DOM 结构撑坏，
   只能让用户自己手动强刷。

## 做法

文本资源（css/js/mjs/html/json/svg/map）一律发 `Cache-Control: no-cache`：
**可以缓存，但每次必须带 ETag 回源校验**。文件没变 → 304，传输成本约 0；
文件变了 → 立刻拿到新内容。图片/字体这类内容寻址资产不进这个名单，仍走长缓存。

`?v=NN` 的手写戳因此变成"可选的显式标记"，而不是正确性的唯一保障。
"""
from __future__ import annotations

from fastapi import FastAPI

#: 需要回源校验的文本类资产后缀。
TEXT_ASSET_SUFFIX = (".css", ".js", ".mjs", ".html", ".json", ".svg", ".map")


def install_asset_cache(app: FastAPI) -> None:
    """给 app 挂上静态资源回源校验中间件（幂等，重复调用无副作用）。"""

    @app.middleware("http")
    async def _static_revalidate(request, call_next):  # noqa: ANN001
        response = await call_next(request)
        path = request.url.path
        # [画布 2026-10-19 · 事故第二层] **HTML 也必须 no-store**。
        # 只给 /static/* 加头是不够的：页面 HTML 本身没有任何缓存头时，浏览器会
        # 按启发式缓存它 —— 于是你拿到的是几小时前的 HTML，里面写死的是
        # `main.js?v=62` / `canvas.css?v=14` 这些旧 URL，后面再怎么升号都够不着。
        # 这就是"升了号、用户刷新还是旧界面"的真正闭环。
        ctype = str(response.headers.get("content-type", ""))
        if ctype.startswith("text/html") or (path.startswith("/static/") and path.endswith(TEXT_ASSET_SUFFIX)):
            # [画布 2026-10-19] no-cache → **no-store**。
            # no-cache 只是"用之前回源校验"，但 ES module 的 import 图在同一文档内
            # 会被浏览器缓存、部分浏览器对"同一 URL 的模块"连校验都省；而且
            # `canvas.js?v=62` 这个号只要没人手工升，URL 就永远不变。
            # 结果是：改了十几轮 canvas.js，用户刷新看到的还是旧界面。
            # 本地单人工具，300KB 本地文件读一次的成本可以忽略，直接不存。
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response
