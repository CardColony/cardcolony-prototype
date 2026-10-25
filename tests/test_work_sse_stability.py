# -*- coding: utf-8 -*-
"""[P23 · 治「推送不推送」] 工作级 SSE 的稳定性网

## 为什么有这张网

访问日志（`logs/access_log.jsonl`）抓到：`/api/works/{w}/events` 的连接
**每 18 秒重连一次**（15s 心跳 + 3s 重连）⇒ **这条流一直在断**。

根因：工作级生成器用了

```python
evt = await asyncio.wait_for(it.__anext__(), timeout=15.0)
```

`wait_for` 超时会**取消**挂起的 `__anext__` ⇒ 异步生成器被打断，
下一轮 `__anext__` 直接抛 ⇒ 流死 ⇒ 浏览器重连 ⇒ 再死……

而另两条流（`/stream`、`/tokens`）用的是**手动 task 模式**（超时不取消挂起的
anext）⇒ 它们稳定。⇒ 「推送为主」其实一直靠兜底轮询撑着（画布页甚至为此加了
4 秒兜底探测）。

实测修复后：同一条连接**连续 50 秒不断**（心跳 15/30/45s 正常）。

## 网里钉的不变量

| 编号 | 不变量 |
|---|---|
| E1 | 工作级 SSE **不许**调 `asyncio.wait_for`（超时会取消挂起的 anext ⇒ 流死） |
| E2 | 必须用手动 task 模式（`create_task` + `asyncio.wait`）：超时**不取消**挂起的 anext |
| E3 | 退出时挂起的 task 要取消、迭代器要关闭（不漏资源） |

判据用 **AST**（只看代码，不看注释/文档字符串）——不然解释根因的注释本身
就会把断言判红（本轮就踩了这个）。

铁律一：只用 conftest 的沙箱，不碰真实 works。
"""
from __future__ import annotations

import ast
import inspect

from src.api import routes


def _fn_ast(fn) -> ast.FunctionDef:
    """取某个函数的 AST（**只看代码**，注释与文档字符串不在里面）。"""
    src = inspect.getsource(fn)
    tree = ast.parse(inspect.cleandoc(src) if False else src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    raise AssertionError("没找到函数体")


def _called_names(fn) -> set[str]:
    """函数体里调用到的**属性名**（如 wait_for / create_task / wait / cancel / aclose）。"""
    node = _fn_ast(fn)
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                out.add(f.attr)
            elif isinstance(f, ast.Name):
                out.add(f.id)
    return out


def test_e1_work_sse_must_not_use_wait_for_on_anext(sandbox):
    """E1：不许调 asyncio.wait_for——它会在心跳超时时把流打死。"""
    called = _called_names(routes.stream_work_events)
    assert "wait_for" not in called, (
        "工作级 SSE 不许用 asyncio.wait_for(it.__anext__(), timeout=…)："
        "超时会**取消**挂起的 anext ⇒ 异步生成器被打断 ⇒ 流每 15 秒死一次"
        "（访问日志实测：连接每 18 秒重连一次）")


def test_e2_work_sse_uses_manual_task_pattern(sandbox):
    """E2：手动 task 模式（与 /stream、/tokens 同款）。"""
    called = _called_names(routes.stream_work_events)
    assert "create_task" in called, "工作级 SSE 应用 create_task 挂起 __anext__"
    assert "wait" in called, "工作级 SSE 应用 asyncio.wait 等那个 task（超时只是保活，不取消它）"
    src = inspect.getsource(routes.stream_work_events)
    assert '": ping' in src, "工作级 SSE 应有 15s 心跳保活（否则中间代理会掐断长连）"


def test_e3_work_sse_cleans_up_pending_task(sandbox):
    """E3：退出时取消挂起的 task ＋ 关掉迭代器（不漏资源）。"""
    called = _called_names(routes.stream_work_events)
    assert "cancel" in called, "退出时应取消挂起的 anext task"
    assert "aclose" in called, "退出时应关闭异步迭代器"
