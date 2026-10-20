# -*- coding: utf-8 -*-
"""5713 · M2 附加条件②(a)：原子写改名重试的**确定性**判据。

背景（实测，非推断）：``board._atomic_write_json`` 的 rename 原为裸 ``os.replace``。
Windows 上该 rename 撞到瞬时共享冲突会回 ``WinError 5/32``——而这次调用生在
``_flush()`` 这个 fire-and-forget 任务里，既无重试、又无人 await，异常被吞成一条
"Task exception was never retrieved"：**一次瞬时冲突 ＝ 一次静默丢写**，盘上状态
落后于内存而无人知晓。

本条 bug 本身是毫秒级偶发（诊断包装器实测：48 次写 1 次瞬时冲突，首次重试 3.1ms
即过），造不出「真环境下的确定性复现」。故按裁决②选 **(a)**：把偶发**注入**成必然，
让三条用例在**旧码上必然失败**——

1. 注入「第一次必失败、之后成功」：旧码当场上抛（必红）；新码重试一次即过。
2. 注入「一直失败」：新码尝试满 ``_REPLACE_ATTEMPTS`` 次后上抛，**且**留下
   ERROR 级留痕（路径＋尝试次数）——即裁决①「重试耗尽不得继续静默」。
3. 注入「非锁竞态的 PermissionError」（无 winerror）：**不得**被吞进重试——
   必须第一次就抛、只尝试 1 次（即裁决③「不误伤」：该立刻抛的没被拖进重试）。

判据口径：断言的是「尝试次数／异常是否上抛／日志是否留痕」，不是耗时。
"""
from __future__ import annotations

import json
import logging
import os

import pytest

from src.thinkchain import board


class _OsShim:
    """只替换 ``board.os`` 的替身：换掉 ``replace``，其余（fsync/name）照旧。

    为什么不直接 monkeypatch ``os.replace``：那会改到**全局** os 模块，牵连同进程
    其它用例。``board`` 里对 os 的使用只有 ``os.replace``／``os.fsync``／``os.name``
    三处，替身足够。
    """

    name = "nt"
    fsync = staticmethod(os.fsync)

    def __init__(self, replace):
        self.replace = replace


def test_transient_sharing_violation_is_retried(tmp_path, monkeypatch):
    """一次瞬时 WinError 5 之后必须重试成功（旧码此处必红）。"""
    calls = []
    real_replace = os.replace

    def flaky(src, dst):
        calls.append((str(src), str(dst)))
        if len(calls) == 1:
            raise PermissionError(13, "拒绝访问。", str(src), 5)  # winerror=5
        return real_replace(src, dst)

    monkeypatch.setattr(board, "os", _OsShim(flaky))
    monkeypatch.setattr(board, "_REPLACE_BASE_SLEEP", 0)  # 退避置零：判据不看耗时

    target = tmp_path / "plans" / "plan_card_001.json"
    board._atomic_write_json(target, {"ok": 1, "revisions": [1, 2, 3]})

    assert target.exists(), "重试后必须真的落盘"
    assert json.loads(target.read_text(encoding="utf-8"))["ok"] == 1
    assert len(calls) == 2, "第一次撞冲突、第二次成功 ⇒ 恰好两次尝试，实 %d" % len(calls)


def test_exhausted_retry_raises_and_logs_error(tmp_path, monkeypatch, caplog):
    """重试耗尽：必须上抛，且留 ERROR（路径＋尝试次数）——不许静默丢写。"""
    calls = []

    def always_busy(src, dst):
        calls.append(1)
        raise PermissionError(13, "拒绝访问。", str(src), 5)

    monkeypatch.setattr(board, "os", _OsShim(always_busy))
    monkeypatch.setattr(board, "_REPLACE_BASE_SLEEP", 0)

    target = tmp_path / "plans" / "plan_card_001.json"
    with caplog.at_level(logging.ERROR, logger="thinkchain.board"):
        with pytest.raises(PermissionError):
            board._atomic_write_json(target, {"ok": 1})

    assert len(calls) == board._REPLACE_ATTEMPTS, \
        "必须尝试满 %d 次，实 %d" % (board._REPLACE_ATTEMPTS, len(calls))
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errs, "重试耗尽必须留 ERROR 级痕迹（否则又是一次静默丢写）"
    msg = errs[-1].getMessage()
    assert "attempts=%d" % board._REPLACE_ATTEMPTS in msg, msg
    assert str(target) in msg, "痕迹必须带路径：%s" % msg


def test_non_lock_race_permission_error_not_retried(tmp_path, monkeypatch):
    """不误伤：真无权限（无 winerror 的 PermissionError）必须第一次就抛，不重试。"""
    calls = []

    def denied(src, dst):
        calls.append(1)
        raise PermissionError(13, "Permission denied", str(dst))  # winerror=None

    monkeypatch.setattr(board, "os", _OsShim(denied))

    with pytest.raises(PermissionError):
        board._atomic_write_json(tmp_path / "plans" / "plan_card_001.json", {"ok": 1})

    assert len(calls) == 1, "非锁竞态不得重试，实尝试 %d 次" % len(calls)