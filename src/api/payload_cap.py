# -*- coding: utf-8 -*-
"""响应层载荷裁剪（2026-10-26 · 治「切卡卡死」）

## 为什么需要

实测（生产访问日志 2026-10-26 00:45）：打开一张卡拉 `timeline?tail=400`，
响应体 **36.9 MB** —— 其中 **31.8 MB 是**同一条 `tool_result`：
`read_linked` 把**另一张卡的整条事件流**（`value` 是 **36661 项的数组**，深嵌套）
回灌进了 `data.result`。

后果：① 服务端序列化 36.9MB（实测 7124ms）；② 浏览器要 `JSON.parse` 36.9MB；
③ 客户端 15 秒超时 ⇒ 弹「请求超时」。

## 为什么可以裁（前端口径核对过）

`stream.js` 的工具行**默认折叠**：摘要行只用 `d.content` 的**第一行**
（clip 到 100 字）与几个标量字段，**完整内容点开才渲染**。
⇒ 那 31.8MB 下载了但**根本没渲染** ⇒ 裁掉它对用户**零可见变化**。

## 裁剪判据（关键：第一版判错了）

第一版判据是「记录里最长的字符串」⇒ **只削掉了 142KB**（响应 36.9→36.28MB）——
因为巨型载荷不是长字符串，是**深嵌套的容器**（36661 项的数组）。
⇒ 判据改成「**结构规模**」：
  · 节点数（dict/list 的元素总数）与字符串总长，**一次遍历、不序列化**即可得；
  · 超限 ⇒ 把 `data.result` **整块**换成一个小摘要（tool/target/ok 仍在
    `data` 的外层 ⇒ 摘要行与状态点**完全不变**）。

## 口径

- **只裁响应，不动落盘**（事件日志保持完整，审计/回放不受影响）。
- 记录上标 `_truncated: true`（前端可识别；当前前端不读，无副作用）。
- 超长但**单串**的载荷仍按字符串截断（带「…（已截断，原 N 字节）」提示）。

## 网

`tests/test_payload_cap.py`：巨型**嵌套**记录被换成摘要｜摘要所需的首行保留
（前端摘要口径不变）｜普通记录原样不动｜落盘文件不被改｜单串超长也被截断。
"""
from __future__ import annotations

import math
from typing import Any


def scrub_nonfinite(o: Any) -> Any:
    """递归把非有限浮点（inf/-inf/nan）换成同名字符串，返回 JSON 安全结构。

    模型会吐 JS 字面量（card_507 实录：``"verbose": Infinity`` 的 tool_call
    arguments）——Python ``json.loads`` 宽容读成 ``float('inf')``，落盘
    ``dumps`` 默认 ``allow_nan=True`` 又宽容写回裸 ``Infinity``，到响应层
    ``allow_nan=False`` 才炸成 ``ValueError``（card_507 timeline 500）。
    换**同名字符串**而非 None：值本身是模型垃圾，但「这里原本是个 Infinity」
    这个事实留着，排障有据。
    """
    if isinstance(o, float):
        if math.isnan(o):
            return "NaN"
        if math.isinf(o):
            return "Infinity" if o > 0 else "-Infinity"
        return o
    if isinstance(o, dict):
        for k, v in o.items():
            nv = scrub_nonfinite(v)
            if nv is not v:
                o[k] = nv
        return o
    if isinstance(o, list):
        for i, v in enumerate(o):
            nv = scrub_nonfinite(v)
            if nv is not v:
                o[i] = nv
        return o
    if isinstance(o, tuple):
        return [scrub_nonfinite(v) for v in o]
    return o

#: 单个字符串的上限（64KB）——远大于前端摘要/展开的实际需要（摘要只要第一行）。
DEFAULT_CAP = 64 * 1024

#: 记录的结构规模上限：节点数 / 字符串总长。超了 ⇒ 整块换成摘要。
MAX_NODES = 5000
MAX_CHARS = 256 * 1024


def _measure(o: Any, nodes: int = 0, chars: int = 0) -> tuple[int, int]:
    """数结构规模：(节点数, 字符串总长)。**一次遍历，不序列化**（巨型记录也能算）。"""
    stack = [o]
    while stack:
        cur = stack.pop()
        nodes += 1
        if isinstance(cur, str):
            chars += len(cur)
        elif isinstance(cur, dict):
            for v in cur.values():
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return nodes, chars


def _find_longest(o: Any, path: tuple = ()) -> tuple[int, tuple]:
    """找结构里**最长**的字符串：返回 (长度, 路径)。"""
    best_len = 0
    best_path: tuple = ()
    stack = [(o, path)]
    while stack:
        cur, p = stack.pop()
        if isinstance(cur, str):
            if len(cur) > best_len:
                best_len = len(cur)
                best_path = p
        elif isinstance(cur, dict):
            for k, v in cur.items():
                stack.append((v, p + (k,)))
        elif isinstance(cur, list):
            for i, v in enumerate(cur):
                stack.append((v, p + (i,)))
    return best_len, best_path


def _get_path(o: Any, path: tuple) -> Any:
    cur = o
    for key in path:
        cur = cur[key]
    return cur


def _set_path(o: Any, path: tuple, value: Any) -> None:
    cur = o
    for key in path[:-1]:
        cur = cur[key]
    cur[path[-1]] = value


def cap_record(rec: dict, cap: int = DEFAULT_CAP,
               max_nodes: int = MAX_NODES, max_chars: int = MAX_CHARS) -> tuple[dict, bool]:
    """裁剪单条记录。返回 (记录, 是否被裁过)。**原地改**（就地省内存）。"""
    changed = False
    nodes, chars = _measure(rec)
    if nodes > max_nodes or chars > max_chars:
        # 结构规模超限 ⇒ 整块换成摘要（外层的 tool/target/ok 不动 ⇒ 摘要行不变）
        data = rec.get("data")
        if isinstance(data, dict) and "result" in data:
            data["result"] = {
                "_capped": True,
                "note": (f"结果过大已省略（原始 {nodes} 个节点 / {chars} 字符）"
                         f"——完整内容请看被读取的那张卡"),
            }
            changed = True
    # 单串超长也截断（巨型载荷常是「短字符串堆」⇒ 上面那条会先命中；
    # 这里兜住「就一个超长字符串」的情况）
    for _ in range(8):
        length, path = _find_longest(rec)
        if length <= cap or not path:
            break
        old = _get_path(rec, path)
        _set_path(rec, path, old[:cap] + f"…（已截断，原 {length} 字节）")
        changed = True
    if changed:
        rec["_truncated"] = True
    return rec, changed


def cap_records(items: list[dict], cap: int = DEFAULT_CAP) -> list[dict]:
    """裁一批记录（响应层）。普通记录**原样返回**（不新建对象 ⇒ 零额外开销）。"""
    for rec in items:
        if isinstance(rec, dict):
            cap_record(rec, cap)
    return items
