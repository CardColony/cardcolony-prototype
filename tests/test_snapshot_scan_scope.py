# -*- coding: utf-8 -*-
"""快照的**扫描作用域**网（〔2026-10-31 · 治「全项目扫描」〕）。

## 病根（实测）

`data_snapshot._snapshot_sync_impl` 里写的是::

    files = _iter_protected_files()      # ＝_iter_state_files(None) ⇒ 扫全项目

而 P26 的注释早就写着「**现在只扫被改的那一个 work**」——**代码没跟上**：
每次写操作都把整个 ``works/`` 树枚举一遍（实测 65 个 work / 2209 个文件 / 35ms），
然后只留被改的那一个（其余 98% 白扫）。

⇒ 修法：**作用域**（`_scan_scope`）——入口名与**零参签名**保持不变
（旧调用点与测试都按零参 mock 它），作用域在入口内部生效。

铁律一：只用 conftest 的沙箱，不碰真实 works。
"""
from __future__ import annotations

from pathlib import Path

from src.storage import data_snapshot


def _mk_state(base: Path, wid: str, name: str, content: str = "x") -> Path:
    d = base / "works" / wid
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(content, encoding="utf-8")
    return p


def test_s1_scope_limits_scan_to_one_work(sandbox):
    """S1：作用域给了一个 work ⇒ 只枚举那一个 work 的文件。"""
    base = sandbox.base
    _mk_state(base, "work_001", "a.md")
    _mk_state(base, "work_002", "b.md")
    _mk_state(base, "work_003", "c.md")

    with data_snapshot._scan_scope("work_002"):
        files = data_snapshot._iter_protected_files()
    assert files, "作用域内应有文件"
    assert all("work_002" in str(f) for f in files), \
        f"作用域外泄了：{[str(f) for f in files]}"
    assert len(files) == 1, files


def test_s2_no_scope_scans_all(sandbox):
    """S2：不带作用域 ⇒ 仍扫全部（兼容「总备份/首次使用」）。"""
    base = sandbox.base
    _mk_state(base, "work_001", "a.md")
    _mk_state(base, "work_002", "b.md")
    files = data_snapshot._iter_protected_files()
    works = {f.relative_to(base).parts[1] for f in files}
    assert works == {"work_001", "work_002"}, works


def test_s3_scope_is_restored(sandbox):
    """S3：作用域退出后复原（不泄漏到后续调用）。"""
    base = sandbox.base
    _mk_state(base, "work_001", "a.md")
    _mk_state(base, "work_002", "b.md")
    with data_snapshot._scan_scope("work_001"):
        pass
    files = data_snapshot._iter_protected_files()
    works = {f.relative_to(base).parts[1] for f in files}
    assert works == {"work_001", "work_002"}, f"作用域泄漏了：{works}"


def test_s4_snapshot_sync_scopes_enumeration(sandbox, monkeypatch):
    """S4：真拍一次 ⇒ 枚举走作用域（只碰被改的那一个 work）。"""
    base = sandbox.base
    _mk_state(base, "work_001", "a.md")
    _mk_state(base, "work_002", "b.md")
    seen: list[str | None] = []
    real = data_snapshot._iter_state_files

    def spy(work_id=None):
        seen.append(work_id)
        return real(work_id)

    monkeypatch.setattr(data_snapshot, "_iter_state_files", spy)
    monkeypatch.setattr(data_snapshot, "_FINGERPRINT_FILE",
                        base / ".snapshot_fingerprints.json")
    data_snapshot.snapshot_sync("scope-probe", work_id="work_001")
    assert seen == ["work_001"], f"枚举应带 work_id，实得 {seen}"


def test_s5_snapshot_sync_without_work_id_scans_all(sandbox, monkeypatch):
    """S5：不传 work_id ⇒ 仍然全扫（契约不变）。"""
    base = sandbox.base
    _mk_state(base, "work_001", "a.md")
    seen: list[str | None] = []
    real = data_snapshot._iter_state_files

    def spy(work_id=None):
        seen.append(work_id)
        return real(work_id)

    monkeypatch.setattr(data_snapshot, "_iter_state_files", spy)
    monkeypatch.setattr(data_snapshot, "_FINGERPRINT_FILE",
                        base / ".snapshot_fingerprints.json")
    data_snapshot.snapshot_sync("scope-probe-all")
    assert seen == [None], f"无 work_id 时应全扫，实得 {seen}"
