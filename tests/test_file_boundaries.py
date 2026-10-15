# -*- coding: utf-8 -*-
"""不变量三：文件工具的四道机械兜底（card_012 工作项 4）。

边界与兜底全在 `src/api/file_tools.py`；步骤三要收紧边界与凭证处理，
先把现有判据钉死——否则「收紧」可能变成「悄悄改判据」，而没有任何东西报警。
"""
from __future__ import annotations

from pathlib import Path

from src.api import file_tools


# ---------------------------------------------------------------- 路径校验

def test_resolve_blocks_escape(sandbox):
    base = sandbox.base

    p, err = file_tools._resolve("trading-workspace/../../escaped.txt")
    assert p is None and "越界" in err["error"]

    p, err = file_tools._resolve(str(sandbox.tmp / "outside.txt"))
    assert p is None and "越界" in err["error"]

    p, err = file_tools._resolve("")
    assert p is None and "不能为空" in err["error"]

    p, err = file_tools._resolve("trading-workspace/a.txt")
    assert err is None
    assert p == (base / "trading-workspace" / "a.txt").resolve()


# ---------------------------------------------------------------- 读写与备份

def test_write_creates_then_backs_up_on_overwrite(sandbox):
    r = file_tools.write_file("trading-workspace/note.md", "第一版")
    assert r["ok"] is True and r["status"] == "created" and r["backup"] == ""

    r2 = file_tools.write_file("trading-workspace/note.md", "第二版")
    assert r2["status"] == "overwritten"
    assert r2["backup"]
    backup = Path(r2["backup"])
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == "第一版"
    assert str(backup).startswith(str(file_tools.BACKUP_DIR))

    assert file_tools.read_file("trading-workspace/note.md")["content"] == "第二版"


def test_read_file_line_ranges(sandbox):
    file_tools.write_file("trading-workspace/lines.txt", "l1\nl2\nl3\nl4")
    r = file_tools.read_file("trading-workspace/lines.txt", 2, 3)
    assert r["content"] == "l2\nl3"
    assert r["total_lines"] == 4 and r["read_lines"] == "2-3"

    # 类型错配要给机械事实，不要抛异常
    # [T-0001 §6-1] 两行关键词原先互换：read_file 遇目录报「不是文件」、
    # list_files 遇文件报「不是目录」（file_tools.py:169/128 原文）
    assert "不是文件" in file_tools.read_file("trading-workspace")["error"]
    assert "不是目录" in file_tools.list_files("trading-workspace/lines.txt")["error"]


# ---------------------------------------------------------------- 凭证类

# [销红 2026-10-13] 护栏已按用户 2026-10-04 21:31 裁定撤销（file_tools.py:251/323/373）；
# 本测试改为钉住"全盘可读可写"这一裁定本身，防将来被悄悄加回。
# （裁定2：src/api/file_tools.py:251「凭证拦截已撤（用户裁定全盘可读可写）」；
#   :323 点明该拦截原本系诸葛自行添加；:373「凭证拦截与依赖目录写入拦截已撤」。）
def test_credentials_readable_and_writable_per_2026_10_04_ruling(sandbox):
    base = sandbox.base
    (base / "trading-workspace" / ".env").write_text("NEEDLE_IN_ENV", encoding="utf-8")
    (base / "trading-workspace" / "server.pem").write_text("k", encoding="utf-8")

    # 凭证出现在 list_files 里（隔离探针实测：条目 = ['.env', 'server.pem']）
    names = [e["name"] for e in file_tools.list_files("trading-workspace")["entries"]]
    assert ".env" in names and "server.pem" in names

    # 凭证内容可被 search_files 命中（file_tools.py:323 检索侧拦截已撤）。
    # 注意顺序：必须在下方 write_file 覆盖内容之前检索，否则 needle 已被冲掉。
    assert file_tools.search_files("NEEDLE_IN_ENV", "trading-workspace")["hits"]
    # 对 .env 直接 search 不报错
    assert "error" not in file_tools.search_files("x", "trading-workspace/.env")

    # 凭证文件可读、可写（file_tools.py:251 读侧拦截已撤、:373 写侧拦截已撤）
    for name in (".env", "server.pem"):
        assert "error" not in file_tools.read_file(f"trading-workspace/{name}")
        assert "error" not in file_tools.write_file(f"trading-workspace/{name}", "y")


# ---------------------------------------------------------------- 依赖目录

# [销红 2026-10-13] 护栏已按用户 2026-10-04 21:31 裁定撤销（file_tools.py:251/323/373）；
# 本测试改为钉住"全盘可读可写"这一裁定本身，防将来被悄悄加回。
# 精确边界（按失败行号分界实证）：依赖目录 list_files 隐藏与 search 剪枝
# （_walk_files/PRUNE_DIRS/_is_pruned）**仍生效**，只有**写入拦截**已撤（:373）。
def test_prune_dirs_invisible_but_writable_per_2026_10_04_ruling(sandbox):
    base = sandbox.base
    venv = base / "trading-workspace" / ".venv"
    venv.mkdir()
    (venv / "lib.py").write_text("PRUNE_NEEDLE", encoding="utf-8")

    # 以下两条原样保留（它们本来就对，且护栏仍生效）
    names = [e["name"] for e in file_tools.list_files("trading-workspace")["entries"]]
    assert ".venv" not in names
    assert file_tools.search_files("PRUNE_NEEDLE", "trading-workspace")["hits"] == []
    # 依赖目录写入拦截已撤（file_tools.py:373）：写入放行
    assert file_tools.write_file("trading-workspace/.venv/x.py", "y")["ok"] is True


# ---------------------------------------------------------------- 二进制与体量

def test_binary_and_oversize_guards(sandbox):
    base = sandbox.base
    blob = base / "trading-workspace" / "blob.bin"
    blob.write_bytes(b"\x00\x01\xff\xfe")

    assert "二进制" in file_tools.read_file("trading-workspace/blob.bin")["error"]
    assert "二进制" in file_tools.write_file("trading-workspace/blob.bin", "文本")["error"]

    oversize = "x" * (file_tools.MAX_WRITE_BYTES + 1)
    r = file_tools.write_file("trading-workspace/big.txt", oversize)
    assert "过大" in r["error"]
    assert not (base / "trading-workspace" / "big.txt").exists()


# ---------------------------------------------------------------- 检索

def test_search_files_reports_file_and_line(sandbox):
    file_tools.write_file("trading-workspace/app.py", "a = 1\nTARGET = 2\n")
    r = file_tools.search_files("target", "trading-workspace")
    assert r["truncated"] is False
    assert r["hits"] == [{"file": str(Path("trading-workspace") / "app.py"),
                          "line": 2, "text": "TARGET = 2"}]
    assert "不能为空" in file_tools.search_files("", "trading-workspace")["error"]


# ---------------------------------------------------------------- schema

def test_tool_schema_surface_is_stable():
    """工具清单是模型的可见接口面，条数与名称属回归项。"""
    names = [s["name"] for s in file_tools.build_file_tool_schemas()]
    assert names == ["list_files", "read_file", "search_files", "write_file"]